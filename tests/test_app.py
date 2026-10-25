from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, fetcher, main, tenders
from app.db import connect
from app.sources.berlin import parse_feed
from app.sources.details import parse_rib

FIXTURES = Path(__file__).parent / "fixtures"

# Die Fixtures haben feste Fristen (früheste 03.09.2026). Ohne feste Uhr würden sie mit der Zeit
# alle ablaufen und vom Standardfilter ausgeblendet – die Tests hängen sonst am Tagesdatum.
HEUTE = "2026-09-01T08:00:00"


def rules_form(cpv=(), keyword=(), priority=(), exclusion=(), lowprio=(), inactive=(), **options) -> dict:
    """Formulardaten wie von der Einstellungsseite: je Regel eine Zeile mit Wert, Checkbox und
    (CPV) Bezeichnung bzw. (Stichwort) Bereich. Tupel: (Code, Bezeichnung) oder (Stichwort, Bereich)."""
    data = {"rules_form": "1", "combine": "oder", "keyword_enabled": "1", **options}
    for kind, entries in (("cpv", cpv), ("keyword", keyword), ("priority", priority), ("exclusion", exclusion),
                          ("lowprio", lowprio)):
        for i, entry in enumerate(entries):
            value, extra = entry if isinstance(entry, tuple) else (entry, "")
            data[f"rule-{kind}-{i}-value"] = value
            if kind == "cpv":
                data[f"rule-{kind}-{i}-label"] = extra
            elif extra:
                data[f"rule-{kind}-{i}-scope"] = extra
            if value not in inactive:
                data[f"rule-{kind}-{i}-active"] = "1"
    return data


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(tenders, "now", lambda: HEUTE)
    with TestClient(main.app) as c:
        for name, category in [("bekanntmachungen", "bekanntmachung"), ("info-19-20", "vergeben")]:
            items = parse_feed((FIXTURES / f"{name}.rss").read_bytes(), category)
            with connect() as conn:
                fetcher.upsert_items(conn, items)
        with connect() as conn:
            first_id = conn.execute("SELECT id FROM tenders ORDER BY id LIMIT 1").fetchone()[0]
            detail = parse_rib((FIXTURES / "rib_detail.html").read_text(encoding="utf-8"))
            fetcher.apply_detail(conn, first_id, detail)
        fetcher.reclassify_all()
        yield c


def test_upsert_is_idempotent(client):
    items = parse_feed((FIXTURES / "bekanntmachungen.rss").read_bytes(), "bekanntmachung")
    with connect() as conn:
        stats = fetcher.upsert_items(conn, items)
        total = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
    assert (stats["new"], stats["updated"], total) == (0, 50, 100)


def test_pages_render(client):
    for url in ("/", "/?status=alle", "/?status=nicht_interessant&q=Maler&sort=frist&frist=offen&neu=1", "/einstellungen", "/eintrag/1"):
        response = client.get(url)
        assert response.status_code == 200, url
    assert "Gartenbau" in client.get("/eintrag/1").text


def test_manual_status_override(client):
    response = client.post("/eintrag/2/status", data={"status": "interessant", "next": "/"}, follow_redirects=False)
    assert response.status_code == 303
    with connect() as conn:
        row = conn.execute("SELECT manual_status FROM tenders WHERE id = 2").fetchone()
    assert row["manual_status"] == "interessant"


def test_open_redirect_is_blocked(client):
    response = client.post("/eintrag/2/status", data={"status": "auto", "next": "//evil.example"}, follow_redirects=False)
    assert response.headers["location"] == "/"


def test_settings_save_reclassifies(client):
    response = client.post(
        "/einstellungen/klassifizierung",
        data=rules_form(keyword=["Tischlerarbeiten"]),
    )
    assert response.status_code == 200
    assert "neu klassifiziert" in response.text
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM tenders WHERE auto_status = 'interessant'").fetchone()[0]
    assert n > 0


def test_list_is_grouped_by_region_then_group(client):
    html = client.get("/?status=alle").text
    keys = ["berlin", "berlin-ausschreibungen", "berlin-beabsichtigt", "berlin-vergeben", "brandenburg", "rest"]
    positions = [html.index(f'id="gruppe-{key}"') for key in keys]
    assert positions == sorted(positions)
    assert 'id="gruppe-berlin" data-group="berlin" open' in html
    assert 'id="gruppe-berlin-ausschreibungen" data-group="berlin-ausschreibungen" open' in html
    assert 'id="gruppe-brandenburg" data-group="brandenburg" open' not in html
    # Zugeklappte Gruppen werden nachgeladen statt mitgeschickt
    vergeben = html.split('id="gruppe-berlin-vergeben"')[1].split("</details>")[0]
    assert 'data-src="/gruppe/berlin/vergeben?status=alle"' in vergeben
    assert "Trinkwasseruntersuchungen" not in vergeben
    partial = client.get("/gruppe/berlin/vergeben?status=alle").text
    assert "Trinkwasseruntersuchungen" in partial
    # Die Quelle steht nicht mehr in der Zeile, nur noch auf der Detailseite
    assert "Vergabeplattform Berlin" not in partial
    assert "Vergabeplattform Berlin" in client.get("/eintrag/51").text
    # Kategorie-Filter zeigt nur die passende Gruppe, aufgeklappt
    html = client.get("/?status=alle&kategorie=vergeben").text
    assert 'id="gruppe-berlin-ausschreibungen"' not in html
    assert 'id="gruppe-berlin-vergeben" data-group="berlin-vergeben" open' in html


def test_more_link_raises_group_limit(client, monkeypatch):
    # frist=alle, weil die Fixture feste Fristen hat: sonst hängt die Anzahl davon ab, wie viele davon
    # am Tag des Testlaufs schon abgelaufen sind. Hier geht es nur um „weitere anzeigen“.
    monkeypatch.setattr(config, "PAGE_SIZE", 20)
    html = client.get("/?status=alle&frist=alle").text
    first_group = html.split('id="gruppe-berlin-beabsichtigt"')[0]
    assert "20 von 50" in first_group
    assert "anzahl_berlin-ausschreibungen=40#gruppe-berlin-ausschreibungen" in first_group
    html = client.get("/?status=alle&frist=alle&anzahl_berlin-ausschreibungen=60").text
    assert "weitere anzeigen" not in html.split('id="gruppe-berlin-beabsichtigt"')[0]


def test_disabled_feed_is_not_fetched(client, monkeypatch):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=(FIXTURES / "vorinformationen.rss").read_bytes())

    monkeypatch.setattr(fetcher, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(fetcher, "fetch_details", lambda client: (0, []))
    response = client.post("/einstellungen/feeds", data={"feed_vorinformation": "1"})
    assert "1 von 5 aktiv" in response.text

    assert fetcher.run_fetch("test")
    assert requested == ["https://www.berlin.de/vergabeplattform/veroeffentlichungen/vorinformationen/feed.rss"]
    assert "3 im Feed, 3 neu" in client.get("/einstellungen").text


def test_settings_validation(client):
    response = client.post(
        "/einstellungen/klassifizierung",
        data=rules_form(cpv=["abc"]),
    )
    assert "Nicht gespeichert" in response.text
    response = client.post("/einstellungen/zeitplan", data={"schedule_mode": "zeiten", "times_text": "25:00"})
    assert "Ungültige Uhrzeit" in response.text


def test_important_entries_are_on_top_and_marked(client):
    response = client.post(
        "/einstellungen/klassifizierung",
        data=rules_form(keyword=["Tischlerarbeiten"], priority=["Maler- und Lackierarbeiten"]),
    )
    assert "neu klassifiziert" in response.text
    assert "Maler- und Lackierarbeiten" in client.get("/einstellungen").text  # Liste gespeichert

    html = client.get("/?status=interessant").text
    group = html.split('id="gruppe-berlin-ausschreibungen"')[1].split("</details>")[0]
    assert "★ 1 WICHTIG" in group
    rows = group.split("<tr class=")[1:]  # [0] = alles vor der ersten Datenzeile
    assert "is-important" in rows[0] and "Maler- und Lackierarbeiten" in rows[0]
    assert "Tischlerarbeiten" in rows[1] and "is-important" not in rows[1]
    assert "Maler- und Lackierarbeiten" in client.get("/?status=alle&treffer=wichtig").text


def test_priority_rules_are_seeded_and_moved_from_keywords(client):
    from app import settings
    priority = [r["value"] for r in settings.list_rules("priority")]
    keywords = [r["value"].lower() for r in settings.list_rules("keyword")]
    assert "Energiekonzept" in priority and "Energie*spar*contracting" in priority
    assert "energiekonzept" not in keywords


def test_hits_column_lists_matched_cpv_codes_and_keywords(client):
    client.post(
        "/einstellungen/klassifizierung",
        data=rules_form(cpv=[("77300000", "Dienstleistungen im Gartenbau")], keyword=["Tischlerarbeiten"],
                        priority=["Glasfaser"], exclusion=["Bestandsinnentüren"], cpv_enabled="1"),
    )
    html = client.get("/?status=alle").text
    group = html.split('id="gruppe-berlin-ausschreibungen"')[1].split("</details>")[0]
    assert "<th>Treffer</th>" in group
    assert "77300000" in group and "Dienstleistungen im Gartenbau" in group  # CPV mit Bezeichnung aus der Liste
    assert "★ Glasfaser" in group  # wichtiges Stichwort
    assert "chip-struck" in group and "⊘ Bestandsinnentüren" in group  # aufgehobener Treffer + Ausschluss


def test_rules_can_be_deactivated_added_and_deleted(client):
    from app import settings
    # Tischlerarbeiten aktiv, Malerarbeiten nur abgeschaltet gespeichert
    client.post("/einstellungen/klassifizierung",
                data=rules_form(keyword=["Tischlerarbeiten", "Maler- und Lackierarbeiten"], inactive=["Maler- und Lackierarbeiten"]))
    keywords = settings.list_rules("keyword")
    assert [(r["value"], r["active"]) for r in keywords] == [("Tischlerarbeiten", 1), ("Maler- und Lackierarbeiten", 0)]
    assert settings.load_rules().keywords == [("Tischlerarbeiten", "alles")]  # abgeschaltete wirken nicht
    with connect() as conn:
        maler = conn.execute("SELECT auto_status FROM tenders WHERE title = 'Maler- und Lackierarbeiten'").fetchall()
    assert maler and all(r["auto_status"] == "nicht_interessant" for r in maler)

    # Seite zeigt Checkboxen statt Textfelder
    html = client.get("/einstellungen").text
    assert "<textarea" not in html.split("Feeds")[0]
    assert 'name="rule-keyword-0-active" value="1" checked' in html
    assert 'name="rule-keyword-1-active" value="1" >' in html or 'name="rule-keyword-1-active" value="1">' in html

    # Löschen (Zeile fehlt) + Hinzufügen (neue Nummer) + Duplikat + CPV mit Prüfziffer
    client.post("/einstellungen/klassifizierung",
                data={**rules_form(keyword=["Tischlerarbeiten"]),
                      "rule-keyword-1000-value": "Glasfaser", "rule-keyword-1000-active": "1",
                      "rule-keyword-1001-value": "tischlerarbeiten", "rule-keyword-1001-active": "1",
                      "rule-cpv-1000-value": "45421000-4", "rule-cpv-1000-label": "Bautischlerarbeiten",
                      "rule-cpv-1000-active": "1", "cpv_enabled": "1"})
    assert [r["value"] for r in settings.list_rules("keyword")] == ["Tischlerarbeiten", "Glasfaser"]
    assert settings.list_rules("cpv") == [{"value": "45421000", "label": "Bautischlerarbeiten", "active": 1, "scope": "alles"}]


def test_invalid_rules_keep_entered_values(client):
    response = client.post("/einstellungen/klassifizierung", data=rules_form(cpv=["123"], keyword=["Neu eingetragen"]))
    assert "Nicht gespeichert" in response.text and "123" in response.text
    assert "Neu eingetragen" in response.text  # Eingaben gehen nicht verloren
    assert client.post("/einstellungen/klassifizierung", data={"combine": "oder"}).status_code == 400


def test_low_priority_entries_are_at_the_bottom_and_marked(client):
    from app import settings
    assert [r["value"] for r in settings.list_rules("lowprio")] == ["Planungsleistungen", "HOAI"]  # Start-Liste

    client.post("/einstellungen/klassifizierung", data=rules_form(
        keyword=["arbeiten"], priority=["Tischlerarbeiten"], lowprio=["Tischler", "Maler"], priority_conflict="beide"))
    assert settings.get_all()["priority_conflict"] == "beide"
    assert {r["scope"] for r in settings.list_rules("lowprio")} == {"titel"}  # Standard für Niedrig: nur Titel

    html = client.get("/?status=interessant").text
    group = html.split('id="gruppe-berlin-ausschreibungen"')[1].split("</details>")[0]
    rows = group.split("<tr class=")[1:]
    classes = [row.split(">", 1)[0] for row in rows]
    # Tischlerarbeiten: wichtig + niedrig -> Mitte (beide Markierungen); Maler: nur niedrig -> ganz unten
    assert "is-important" in classes[0] and "is-low" in classes[0] and "★ WICHTIG" in rows[0] and "↓ NIEDRIG" in rows[0]
    assert "is-low" in classes[-1] and "Maler" in rows[-1] and "is-important" not in classes[-1]
    normal = [i for i, c in enumerate(classes) if "is-low" not in c and "is-important" not in c]
    lows = [i for i, c in enumerate(classes) if "is-low" in c and "is-important" not in c]
    assert normal and lows and max(normal) < min(lows)
    assert "↓ Maler" in group  # Treffer-Spalte
    assert "Maler" in client.get("/?status=alle&treffer=niedrig").text

    # Nicht interessante Einträge mit Niedrig-Stichwort werden nicht markiert – außer nach manueller Einstufung
    client.post("/einstellungen/klassifizierung", data=rules_form(keyword=["Glasfaser"], lowprio=["Maler"]))
    with connect() as conn:
        maler = conn.execute("SELECT id, auto_status, low_priority FROM tenders WHERE title LIKE 'Maler%' LIMIT 1").fetchone()
    assert (maler["auto_status"], maler["low_priority"]) == ("nicht_interessant", 0)
    client.post(f"/eintrag/{maler['id']}/status", data={"status": "interessant", "next": "/"})
    with connect() as conn:
        assert conn.execute("SELECT low_priority FROM tenders WHERE id = ?", (maler["id"],)).fetchone()[0] == 1
    client.post(f"/eintrag/{maler['id']}/status", data={"status": "auto", "next": "/"})
    with connect() as conn:
        assert conn.execute("SELECT low_priority FROM tenders WHERE id = ?", (maler["id"],)).fetchone()[0] == 0


def test_wildcard_keywords_can_be_saved(client):
    from app import settings
    response = client.post("/einstellungen/klassifizierung", data=rules_form(keyword=["Glas*ausbau"], priority=["*"]))
    assert "Nicht gespeichert" in response.text and "enthält keine Buchstaben" in response.text
    client.post("/einstellungen/klassifizierung", data=rules_form(keyword=["Glas*ausbau"]))
    assert [r["value"] for r in settings.list_rules("keyword")] == ["Glas*ausbau"]
    with connect() as conn:
        row = conn.execute("SELECT auto_status, keyword_matches FROM tenders WHERE title = 'Glasfaserausbau und Signallieferung'").fetchone()
    assert row["auto_status"] == "interessant" and "Glas*ausbau" in row["keyword_matches"]


def test_warning_after_repeated_source_failures(client, monkeypatch):
    fail = {"on": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if fail["on"]:
            return httpx.Response(503, text="Wartung")
        return httpx.Response(200, content=(FIXTURES / "vorinformationen.rss").read_bytes())

    monkeypatch.setattr(fetcher, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(fetcher, "fetch_details", lambda client: (0, []))
    client.post("/einstellungen/feeds", data={"feed_vorinformation": "1"})

    fetcher.run_fetch("test")
    assert "nicht erreichbar" not in client.get("/").text  # ein einzelner Fehler warnt noch nicht
    fetcher.run_fetch("test")
    html = client.get("/").text
    assert "nicht erreichbar" in html and "Vorinformationen / Markterkundungen" in html and "2× hintereinander" in html
    assert "503" in html
    assert "2× hintereinander" in client.get("/einstellungen").text

    fail["on"] = False
    fetcher.run_fetch("test")
    assert "nicht erreichbar" not in client.get("/").text  # Erfolg setzt zurück

    # abgeschaltete Quellen warnen nicht
    fail["on"] = True
    fetcher.run_fetch("test")
    fetcher.run_fetch("test")
    assert "nicht erreichbar" in client.get("/").text
    client.post("/einstellungen/feeds", data={"feed_bekanntmachung": "1"})  # Vorinformationen abgeschaltet
    assert "nicht erreichbar" not in client.get("/").text


def test_combined_keywords_and_scope_are_saved_and_applied(client):
    from app import settings
    client.post("/einstellungen/klassifizierung", data=rules_form(
        keyword=[("Glasfaser+Signallieferung", "titel"), ("Schädlingsbekämpfung", "alles")],
        lowprio=[("Rahmenvertrag", "alles")]))
    rules = {r["value"]: r["scope"] for r in settings.list_rules("keyword")}
    assert rules == {"Glasfaser + Signallieferung": "titel", "Schädlingsbekämpfung": "alles"}  # „+“ vereinheitlicht
    assert settings.list_rules("lowprio")[0]["scope"] == "alles"
    with connect() as conn:
        row = conn.execute("SELECT auto_status, keyword_matches FROM tenders WHERE title = 'Glasfaserausbau und Signallieferung'").fetchone()
    assert row["auto_status"] == "interessant" and "Glasfaser + Signallieferung" in row["keyword_matches"]

    html = client.get("/einstellungen").text
    assert '<option value="titel" selected>nur Titel</option>' in html
    response = client.post("/einstellungen/klassifizierung", data=rules_form(keyword=["+ *"]))
    assert "enthält keine Buchstaben" in response.text


def test_rule_migrations_on_start(tmp_path, monkeypatch):
    """Bestehende Datenbank: gemeinsamer Niedrig-Bereich wird übernommen, Energiemanagementsystem ergänzt – je nur einmal."""
    from app import settings
    from app.db import init_db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.db")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    init_db()
    settings.seed_rules()
    settings.seed_priority_rules()
    settings.seed_lowprio_rules()
    settings.set_many({"lowprio_scope": "alles"})  # alte gemeinsame Einstellung

    with TestClient(main.app):
        pass
    assert {r["scope"] for r in settings.list_rules("lowprio")} == {"alles"}
    assert "Energiemanagementsystem" in [r["value"] for r in settings.list_rules("keyword")]
    assert "Energiemanagementsystem" in [r["value"] for r in settings.list_rules("priority")]

    # gelöschter Eintrag kommt beim nächsten Start nicht wieder
    settings.replace_rules({"priority": [r for r in settings.list_rules("priority") if r["value"] != "Energiemanagementsystem"]})
    with TestClient(main.app):
        pass
    assert "Energiemanagementsystem" not in [r["value"] for r in settings.list_rules("priority")]


# ---------- „Diese Ansicht als gesehen markieren“ ----------

def status_counts_from_db(where: str = "1") -> dict:
    with connect() as conn:
        return {
            row[0]: row[1] for row in conn.execute(
                f"SELECT COALESCE(manual_status, auto_status), COUNT(*) FROM tenders WHERE {where} GROUP BY 1"
            )
        }


def test_mark_seen_keeps_other_tab_new(client):
    """Der Reiter wird mitgeschickt: „Interessant“ abhaken lässt „Nicht interessant“ neu."""
    total = status_counts_from_db()
    assert total.get("interessant") and total.get("nicht_interessant")

    client.post("/gesehen", data={"next": "/", "status": "interessant", "frist": "alle"}, follow_redirects=False)
    assert status_counts_from_db("seen_at IS NOT NULL") == {"interessant": total["interessant"]}

    client.post("/gesehen", data={"next": "/", "status": "nicht_interessant", "frist": "alle"}, follow_redirects=False)
    assert status_counts_from_db("seen_at IS NULL") == {}


def test_mark_seen_respects_filters(client):
    with connect() as conn:
        conn.execute("UPDATE tenders SET auto_status = 'interessant', manual_status = NULL, deadline = NULL")
        titles = [r[0] for r in conn.execute("SELECT title FROM tenders ORDER BY id LIMIT 2")]
        conn.execute("UPDATE tenders SET authority = 'Teststelle Berlin' WHERE title = ?", (titles[0],))

    response = client.post(
        "/gesehen", data={"next": "/", "status": "interessant", "vergabestelle": "Teststelle"}, follow_redirects=False
    )
    assert response.status_code == 303 and "meldung=1+Eintrag" in response.headers["location"]
    with connect() as conn:
        seen = [r[0] for r in conn.execute("SELECT title FROM tenders WHERE seen_at IS NOT NULL")]
    assert seen == [titles[0]]


def test_mark_seen_leaves_expired_entries_new(client):
    """Beim Standardfilter sind abgelaufene Fristen ausgeblendet – also werden sie auch nicht abgehakt."""
    with connect() as conn:
        conn.execute("UPDATE tenders SET auto_status = 'interessant', manual_status = NULL, deadline = NULL")
        expired = conn.execute("SELECT id FROM tenders ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("UPDATE tenders SET deadline = '2000-01-01' WHERE id = ?", (expired,))

    client.post("/gesehen", data={"next": "/", "status": "interessant"}, follow_redirects=False)
    with connect() as conn:
        still_new = [r[0] for r in conn.execute("SELECT id FROM tenders WHERE seen_at IS NULL")]
    assert still_new == [expired]


def test_mark_seen_button_follows_the_view(client):
    assert "Diese Ansicht als gesehen markieren" in client.get("/?frist=alle").text
    client.post("/gesehen", data={"next": "/", "status": "interessant", "frist": "alle"}, follow_redirects=False)
    assert "Diese Ansicht als gesehen markieren" not in client.get("/?frist=alle").text
    assert "Diese Ansicht als gesehen markieren" in client.get("/?status=nicht_interessant&frist=alle").text


# ---------- Suchbereich und CPV-Bezeichnungen ----------

def test_search_scope_limits_the_searched_fields(client):
    with connect() as conn:
        conn.execute("UPDATE tenders SET title = ?, description = ? WHERE id = 1",
                     ("Titelbegriff Quittenkuchen", "Textbegriff Zwetschge"))

    def page(**params):
        return client.get("/", params={"status": "alle", "frist": "alle", **params}).text

    assert "Titelbegriff Quittenkuchen" in page(q="Zwetschge")  # Standard: überall
    assert "Titelbegriff Quittenkuchen" in page(q="Zwetschge", suchfeld="beschreibung")
    assert "Titelbegriff Quittenkuchen" not in page(q="Zwetschge", suchfeld="titel")
    assert "Titelbegriff Quittenkuchen" in page(q="Quittenkuchen", suchfeld="titel")
    assert "Titelbegriff Quittenkuchen" not in page(q="Quittenkuchen", suchfeld="cpv")
    assert 'name="suchfeld"' in page() and "suche nur Titel" in page()


def test_cpv_title_is_shown_with_the_code(client):
    with connect() as conn:
        conn.execute("""UPDATE tenders SET cpv_codes = '[{"code": "45421000-4", "label": ""}]' WHERE id = 2""")
    client.post("/einstellungen/klassifizierung",
                data=rules_form(cpv=[("45421000", "Bautischlerarbeiten")], keyword=["arbeiten"], cpv_enabled="1"))

    # Bezeichnung aus der CPV-Liste, obwohl die Quelle keine mitgeliefert hat
    assert "Bautischlerarbeiten" in client.get("/eintrag/2").text

    with connect() as conn:
        conn.execute("""UPDATE tenders SET cpv_codes = '[{"code": "99999999-9", "label": ""}]' WHERE id = 2""")
    assert "nicht im CPV-Katalog" in client.get("/eintrag/2").text


def test_row_names_only_the_secondary_category(client):
    """Die Gruppenüberschrift nennt die leitende Kategorie – in der Zeile steht nur die zweite."""
    with connect() as conn:
        conn.execute("UPDATE tenders SET category = 'vorinformation' WHERE id = 1")
    group = client.get("/gruppe/berlin/ausschreibungen?status=alle&frist=alle").text
    assert "Vorinformation" in group  # zweite Kategorie der Gruppe wird benannt
    assert "Bekanntmachung" not in group  # leitende Kategorie nicht


def test_cpv_catalog_supplies_the_official_titles(client):
    """Codes ohne Bezeichnung in den Daten bekommen den amtlichen Titel aus dem CPV-Katalog."""
    from app import cpv
    assert len(cpv.titles()) > 9000
    assert cpv.title("45421000-4") == "Bautischlerarbeiten"  # Prüfziffer wird ignoriert
    assert cpv.title("09323000") == "Fernwärme"
    assert cpv.title("99999999") == ""

    with connect() as conn:
        conn.execute("""UPDATE tenders SET cpv_codes = '[{"code": "30192000", "label": ""}]' WHERE id = 2""")
    assert "Bürobedarf" in client.get("/eintrag/2").text


def test_own_label_wins_over_the_catalog(client):
    with connect() as conn:
        conn.execute("""UPDATE tenders SET cpv_codes = '[{"code": "30192000", "label": "Unsere Schreibwaren"}]'
                        WHERE id = 2""")
    html = client.get("/eintrag/2").text
    assert "Unsere Schreibwaren" in html and "Bürobedarf" not in html


def test_only_new_is_the_default_in_the_tender_area(client):
    """Standard im Ausschreibungsbereich: nur ungesehene Einträge. Haken raus zeigt wieder alle."""
    with connect() as conn:
        titel = conn.execute("SELECT title FROM tenders WHERE id = 1").fetchone()[0]

    assert titel in client.get("/?status=alle&frist=alle").text
    client.get("/eintrag/1")  # Öffnen markiert als gesehen
    assert titel not in client.get("/?status=alle&frist=alle").text
    assert titel in client.get("/?status=alle&frist=alle&neu=0").text

    html = client.get("/?status=alle&frist=alle").text
    assert 'name="neu" value="1" checked' in html  # Haken ist gesetzt
    assert 'name="neu" value="0"' in html  # und lässt sich abwählen
    assert "auch gesehene anzeigen" in html


def test_empty_view_explains_that_nothing_is_new(client):
    client.post("/gesehen", data={"next": "/", "status": "alle", "frist": "alle"}, follow_redirects=False)
    html = client.get("/?status=alle&frist=alle").text
    assert "Nichts Neues" in html and "auch gesehene anzeigen" in html
    assert "Noch keine Daten" not in html


def test_favorites_tab_still_shows_seen_entries(client):
    """Der Reiter „♥ Auftraggeber“ behält „alles anzeigen“ – dort geht es nicht um den Tagesdurchlauf."""
    client.get("/eintrag/1")
    html = client.get("/auftraggeber").text
    assert 'name="neu" value="1" checked' not in html


def test_cpv_search_finds_code_and_title(client):
    from app import cpv
    assert [t["code"] for t in cpv.search("09323")] == ["09323000"]
    assert {"code": "09323000", "title": "Fernwärme"} in cpv.search("fernwärme")
    assert cpv.search("z") == []  # zu kurz

    treffer = client.get("/api/cpv", params={"q": "solarenergie"}).json()
    assert {"code": "09330000", "title": "Solarenergie"} in treffer


def test_new_cpv_rule_takes_its_title_from_the_catalog(client):
    from app import settings
    # Hinzufügen ohne Bezeichnung: Titel kommt aus dem Katalog
    client.post("/einstellungen/klassifizierung",
                data={**rules_form(keyword=["arbeiten"], cpv_enabled="1"),
                      "rule-cpv-1000-value": "45421000", "rule-cpv-1000-active": "1"})
    assert settings.list_rules("cpv") == [
        {"value": "45421000", "label": "Bautischlerarbeiten", "active": 1, "scope": "alles"}
    ]

    # Eine schon eingetragene eigene Bezeichnung bleibt beim Speichern erhalten
    client.post("/einstellungen/klassifizierung",
                data={**rules_form(cpv=[("45421000", "Unsere Tischler")], keyword=["arbeiten"], cpv_enabled="1")})
    assert settings.list_rules("cpv")[0]["label"] == "Unsere Tischler"


def test_unknown_cpv_code_is_rejected(client):
    response = client.post("/einstellungen/klassifizierung",
                           data={**rules_form(keyword=["arbeiten"], cpv_enabled="1"),
                                 "rule-cpv-1000-value": "99999999", "rule-cpv-1000-active": "1"})
    assert "steht nicht im CPV-Katalog" in response.text


def test_cpv_add_field_searches_the_catalog(client):
    html = client.get("/einstellungen").text
    assert 'list="cpv-katalog"' in html and '<datalist id="cpv-katalog">' in html
    assert "Bezeichnung (optional)" not in html  # keine freie Texteingabe mehr
