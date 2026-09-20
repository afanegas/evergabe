from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, fetcher, main
from app.db import connect
from app.sources.berlin import parse_feed
from app.sources.details import parse_rib

FIXTURES = Path(__file__).parent / "fixtures"


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
    assert "Vergabeplattform Berlin" in partial  # Quelle sichtbar
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
