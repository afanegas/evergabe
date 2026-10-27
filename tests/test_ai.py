"""KI-Auswahl: Warteschlange, Antwort auswerten und Wirkung auf Markierung bzw. Einstufung."""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ai, config, fetcher, mailer, main, scheduler, settings, tenders
from app.classify import AI_MARKIERUNG, AI_PRUEFUNG, INTERESSANT, NICHT_INTERESSANT
from app.db import connect
from app.sources.berlin import parse_feed

FIXTURES = Path(__file__).parent / "fixtures"

# Feste Uhr, damit die Fristen der Fixtures nicht ablaufen (siehe test_app.py)
HEUTE = "2026-09-01T08:00:00"

PROFIL = "Wir beraten zu Energieeffizienz, Photovoltaik und Wärmenetzen. Keine Bauleistungen."


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    # Neu-Einstufung sofort statt im Hintergrund, damit sie nach dem Speichern fertig ist
    monkeypatch.setattr(config, "BACKGROUND_ENABLED", False)
    monkeypatch.setattr(tenders, "now", lambda: HEUTE)
    monkeypatch.setattr(ai, "now", lambda: HEUTE)  # auch die Altersgrenze rechnet mit der festen Uhr
    with TestClient(main.app) as c:
        items = parse_feed((FIXTURES / "bekanntmachungen.rss").read_bytes(), "bekanntmachung")
        with connect() as conn:
            fetcher.upsert_items(conn, items)
            # Die Fixtures enthalten nichts aus dem Energiebereich; mit diesem Stichwort gibt es
            # auch interessante Einträge – nur die bewertet die Betriebsart „markierung“.
            conn.execute("INSERT OR IGNORE INTO rules(kind, value) VALUES('keyword', 'Beleuchtung')")
        fetcher.reclassify_all()
        yield c


def enable(mode: str = AI_MARKIERUNG, **extra) -> None:
    settings.set_many({"ai_enabled": True, "ai_mode": mode, "ai_profile": PROFIL, **extra})


def one(status: str) -> int:
    """Nummer eines Eintrags mit der gewünschten automatischen Einstufung."""
    with connect() as conn:
        return conn.execute(
            "SELECT id FROM tenders WHERE auto_status = ? AND (deadline IS NULL OR deadline >= ?) "
            "ORDER BY id LIMIT 1", (status, HEUTE[:10])
        ).fetchone()[0]


def judge(tender_id: int, verdict: str = "treffer", reason: str = "passt") -> None:
    """Ein Urteil speichern und den Eintrag neu einstufen – wie es die Seite /ki tut."""
    result = ai.apply_results([{"id": tender_id, "result": verdict, "reason": reason}])
    fetcher.reclassify(result["saved"])


def field(tender_id: int, *names: str) -> tuple:
    with connect() as conn:
        row = conn.execute(f"SELECT {', '.join(names)} FROM tenders WHERE id = ?", (tender_id,)).fetchone()
    return tuple(row[n] for n in names)


# ---------- Warteschlange ----------

def test_marking_mode_queues_only_interesting(client):
    enable(AI_MARKIERUNG)
    assert ai.mark_candidates() > 0
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {tenders.STATUS_SQL} AS status FROM tenders WHERE ai_state = 'offen'"
        ).fetchall()
    assert rows and all(row["status"] == INTERESSANT for row in rows)


def test_check_mode_queues_more_than_marking_mode(client):
    enable(AI_MARKIERUNG)
    marking = ai.mark_candidates()
    with connect() as conn:  # Warteschlange zurücksetzen, um beide Betriebsarten zu vergleichen
        conn.execute("UPDATE tenders SET ai_state = 'keine'")
    enable(AI_PRUEFUNG)
    assert ai.mark_candidates() > marking


def test_region_setting_limits_the_queue(client):
    enable(AI_PRUEFUNG, ai_regions=["brandenburg"])  # die Fixtures sind alle aus Berlin
    assert ai.mark_candidates() == 0


def test_verdict_is_not_asked_again(client):
    enable(AI_MARKIERUNG)
    ai.mark_candidates()
    tender_id = ai.pending()[0]["id"]
    judge(tender_id)
    assert field(tender_id, "ai_state") == (ai.STATE_OK,)
    assert ai.mark_candidates() == 0
    assert tender_id not in [item["id"] for item in ai.pending(limit=1000)]


def test_changed_profile_makes_verdict_stale(client):
    enable(AI_MARKIERUNG)
    ai.mark_candidates()
    tender_id = ai.pending()[0]["id"]
    judge(tender_id)
    settings.set_many({"ai_profile": PROFIL + " Auch Elektromobilität."})
    assert ai.requeue_stale() > 0
    assert field(tender_id, "ai_result", "ai_state") == (None, ai.STATE_OPEN)


# ---------- Wirkung auf Markierung und Einstufung ----------

def test_marking_mode_keeps_the_status(client):
    enable(AI_MARKIERUNG)
    tender_id = one(INTERESSANT)
    judge(tender_id)
    assert field(tender_id, "auto_status", "ai_match", "ai_reason") == (INTERESSANT, 1, "passt")


def test_check_mode_can_make_interesting(client):
    enable(AI_PRUEFUNG)
    tender_id = one(NICHT_INTERESSANT)
    judge(tender_id)
    assert field(tender_id, "auto_status", "ai_match") == (INTERESSANT, 1)


def test_unklar_does_not_make_interesting(client):
    enable(AI_PRUEFUNG)
    tender_id = one(NICHT_INTERESSANT)
    judge(tender_id, "unklar", "Beschreibung zu knapp")
    assert field(tender_id, "auto_status", "ai_match") == (NICHT_INTERESSANT, 0)


def test_filter_shows_only_marked_entries(client):
    enable(AI_MARKIERUNG)
    tender_id = one(INTERESSANT)
    judge(tender_id)
    text = client.get("/?status=alle&treffer=ki&neu=0&frist=alle").text
    assert "🤖 KI" in text
    with connect() as conn:
        other = conn.execute("SELECT title FROM tenders WHERE ai_match = 0 ORDER BY id LIMIT 1").fetchone()["title"]
    assert other not in text


# ---------- Antwort auswerten ----------

def test_parse_answer_accepts_fences_and_extra_text():
    raw = 'Hier das Ergebnis:\n```json\n[{"id": 7, "interessant": true, "begruendung": "Energieberatung"}]\n```\nViel Erfolg!'
    entries, errors = ai.parse_answer(raw)
    assert errors == []
    assert entries == [{"id": 7, "result": "treffer", "reason": "Energieberatung"}]


def test_parse_answer_maps_words_and_confidence():
    raw = json.dumps([
        {"id": 1, "interessant": "ja", "begruendung": "passt", "sicherheit": "niedrig"},
        {"id": 2, "interessant": "unklar", "begruendung": "unklar", "sicherheit": "hoch"},
        {"id": 3, "interessant": False, "begruendung": "Bauleistung"},
    ])
    entries, errors = ai.parse_answer(raw)
    assert errors == []
    assert [e["result"] for e in entries] == ["treffer", "unklar", "kein_treffer"]
    assert entries[0]["reason"] == "passt (Sicherheit: niedrig)"  # „hoch“ bleibt unerwähnt
    assert entries[1]["reason"] == "unklar"


def test_parse_answer_rejects_broken_json_without_saving_half():
    entries, errors = ai.parse_answer('[{"id": 1, "interessant": true,')
    assert entries == [] and errors and "JSON" in errors[0]


def test_parse_answer_reports_unusable_entries():
    entries, errors = ai.parse_answer('[{"id": "x"}, {"id": 2, "interessant": "weiß nicht"}]')
    assert entries == []
    assert len(errors) == 2


# ---------- Seite ----------

def test_page_shows_prompt_with_profile(client):
    enable(AI_MARKIERUNG)
    ai.mark_candidates()
    text = client.get("/ki").text
    assert "Keine Bauleistungen" in text  # das Profil steht im Prompt
    assert "Eintrag" in text and str(ai.pending()[0]["id"]) in text


def test_page_warns_when_switched_off(client):
    assert "ausgeschaltet" in client.get("/ki").text


def test_post_answer_saves_and_reclassifies(client):
    enable(AI_PRUEFUNG)
    ai.mark_candidates()
    tender_id = one(NICHT_INTERESSANT)
    answer = json.dumps([{"id": tender_id, "interessant": True, "begruendung": "Energiekonzept"}])
    response = client.post("/ki/ergebnis", data={"antwort": answer}, follow_redirects=False)
    assert response.status_code == 303
    assert field(tender_id, "ai_result", "auto_status") == ("treffer", INTERESSANT)


def test_post_broken_answer_changes_nothing(client):
    enable(AI_PRUEFUNG)
    ai.mark_candidates()
    tender_id = one(NICHT_INTERESSANT)
    response = client.post("/ki/ergebnis", data={"antwort": "kein JSON"})
    assert response.status_code == 200 and "JSON" in response.text
    assert field(tender_id, "ai_result", "auto_status") == (None, NICHT_INTERESSANT)


def test_post_unknown_id_is_skipped_with_note(client):
    enable(AI_PRUEFUNG)
    answer = json.dumps([{"id": 999999, "interessant": True, "begruendung": "gibt es nicht"}])
    response = client.post("/ki/ergebnis", data={"antwort": answer})
    assert response.status_code == 200 and "999999" in response.text


def test_settings_save_profile_and_requeue(client):
    data = {"ai_enabled": "1", "ai_mode": AI_PRUEFUNG, "ai_profile": PROFIL,
            "ai_region_berlin": "1", "ai_max_per_run": "10", "ai_max_age_days": "0"}
    response = client.post("/einstellungen/ki", data=data, follow_redirects=False)
    assert response.status_code == 303
    assert settings.get("ai_profile") == PROFIL
    assert ai.open_count() > 0
    assert len(ai.pending()) == 10  # Obergrenze je Durchgang


def test_settings_reject_enabling_without_profile(client):
    data = {"ai_enabled": "1", "ai_mode": AI_MARKIERUNG, "ai_profile": "  ",
            "ai_region_berlin": "1", "ai_max_per_run": "25", "ai_max_age_days": "30"}
    response = client.post("/einstellungen/ki", data=data)
    assert response.status_code == 200 and "Leistungsprofil" in response.text
    assert settings.get("ai_enabled") is False


# ---------- Sortierung und kombinierbare Filter ----------

def test_ai_hits_sort_to_the_top(client):
    enable(AI_MARKIERUNG)
    with connect() as conn:  # ein Eintrag weit unten in der Liste (ältester) bekommt den KI-Treffer
        tender_id = conn.execute(
            f"SELECT id FROM tenders WHERE {tenders.STATUS_SQL} = ? ORDER BY COALESCE(published_at, first_seen) ASC "
            "LIMIT 1", (INTERESSANT,)
        ).fetchone()[0]
    judge(tender_id)
    ergebnis = tenders.search(tenders.Filters(status=INTERESSANT, neu=False, frist="alle"), "berlin",
                              ("bekanntmachung", "vorinformation"))
    assert ergebnis["items"][0]["id"] == tender_id


def test_marks_can_be_combined(client):
    enable(AI_MARKIERUNG)
    tender_id = one(INTERESSANT)
    judge(tender_id)
    with connect() as conn:  # derselbe Eintrag zusätzlich als wichtig markiert
        conn.execute("UPDATE tenders SET important = 1 WHERE id = ?", (tender_id,))
        titel = conn.execute("SELECT title FROM tenders WHERE id = ?", (tender_id,)).fetchone()["title"]
        anderer = conn.execute(
            "SELECT title FROM tenders WHERE ai_match = 0 AND important = 0 ORDER BY id LIMIT 1"
        ).fetchone()["title"]
    text = client.get("/?status=alle&neu=0&frist=alle&t_ki=1&t_wichtig=1").text
    assert titel in text and anderer not in text


def test_low_priority_can_be_hidden(client):
    with connect() as conn:
        conn.execute("UPDATE tenders SET low_priority = 1 WHERE id = (SELECT MIN(id) FROM tenders)")
        titel = conn.execute("SELECT title FROM tenders ORDER BY id LIMIT 1").fetchone()["title"]
    assert titel in client.get("/?status=alle&neu=0&frist=alle&niedrig=nur").text
    assert titel not in client.get("/?status=alle&neu=0&frist=alle&niedrig=ohne").text


def test_old_single_value_links_still_work(client):
    """Frühere Adressen (Lesezeichen, alte Mails) mit einem einzelnen treffer-Wert."""
    enable(AI_MARKIERUNG)
    tender_id = one(INTERESSANT)
    judge(tender_id)
    with connect() as conn:
        titel = conn.execute("SELECT title FROM tenders WHERE id = ?", (tender_id,)).fetchone()["title"]
        conn.execute("UPDATE tenders SET low_priority = 1 WHERE id = ?", (tender_id,))
    assert titel in client.get("/?status=alle&neu=0&frist=alle&treffer=ki").text
    assert titel in client.get("/?status=alle&neu=0&frist=alle&treffer=niedrig").text


# ---------- Warteschlange bleibt zu den Einstellungen passend ----------

def test_switching_mode_clears_entries_that_are_no_longer_candidates(client):
    """Nach dem Wechsel von „dritte Prüfung“ auf „nur Markierung“ dürfen keine nicht interessanten
    Einträge mehr in der Anfrage stehen."""
    enable(AI_PRUEFUNG)
    assert ai.mark_candidates() > 0
    enable(AI_MARKIERUNG)
    ai.mark_candidates()
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {tenders.STATUS_SQL} AS status FROM tenders WHERE ai_state = 'offen'"
        ).fetchall()
    assert rows and all(row["status"] == INTERESSANT for row in rows)


def test_counters_agree_with_each_other(client):
    """Die Zahl im Menü, „offen“ auf der Seite und die Länge der Anfrage gehören zusammen."""
    enable(AI_PRUEFUNG)
    ai.mark_candidates()
    enable(AI_MARKIERUNG, ai_max_per_run=1000)
    ai.mark_candidates()
    assert ai.open_count() == ai.status()["offen"] == len(ai.pending())


def test_age_limit_excludes_old_announcements(client):
    enable(AI_MARKIERUNG, ai_max_age_days=30)
    with connect() as conn:  # ein interessanter Eintrag wird auf „vor einem Jahr“ gesetzt
        tender_id = one(INTERESSANT)
        conn.execute("UPDATE tenders SET published_at = '2025-09-01T08:00' WHERE id = ?", (tender_id,))
    ai.mark_candidates()
    assert tender_id not in [item["id"] for item in ai.pending(limit=1000)]
    enable(AI_MARKIERUNG, ai_max_age_days=0)  # 0 = ohne Grenze
    ai.mark_candidates()
    assert tender_id in [item["id"] for item in ai.pending(limit=1000)]


def test_stale_verdict_is_not_requeued_on_its_own(client):
    """Ein geändertes Profil macht Urteile veraltet – neu eingeholt werden sie nur auf Knopfdruck."""
    enable(AI_MARKIERUNG)
    ai.mark_candidates()
    tender_id = ai.pending()[0]["id"]
    judge(tender_id)
    settings.set_many({"ai_profile": PROFIL + " Auch Quartierskonzepte."})
    ai.mark_candidates()
    assert field(tender_id, "ai_state") == (ai.STATE_OK,)
    assert ai.requeue_stale() > 0
    assert field(tender_id, "ai_state") == (ai.STATE_OPEN,)


def test_saving_the_profile_alone_does_not_reclassify(client, monkeypatch):
    """Profil, Regionen und Obergrenzen ändern nicht die Einstufung – das Neu-Klassifizieren
    dauert bei vielen Einträgen zu lange, um es unnötig auszulösen."""
    enable(AI_MARKIERUNG)
    laeufe = []
    monkeypatch.setattr(fetcher, "reclassify_all", lambda *a, **k: laeufe.append(1) or {"gesamt": 0, "interessant": 0})
    daten = {"ai_enabled": "1", "ai_mode": AI_MARKIERUNG, "ai_profile": PROFIL + " Neuer Satz.",
             "ai_region_berlin": "1", "ai_max_per_run": "25", "ai_max_age_days": "0"}
    client.post("/einstellungen/ki", data=daten)
    assert laeufe == []
    client.post("/einstellungen/ki", data={**daten, "ai_mode": AI_PRUEFUNG})  # Betriebsart wirkt
    assert len(laeufe) == 1


def test_saving_rules_answers_at_once_and_reclassifies_in_the_background(client, monkeypatch):
    """Die Seite soll nicht warten: Das Speichern antwortet sofort, die Neu-Einstufung läuft
    danach weiter und meldet sich über /api/status."""
    monkeypatch.setattr(config, "BACKGROUND_ENABLED", True)
    langsam = threading.Event()
    original = fetcher.reclassify_all
    monkeypatch.setattr(fetcher, "reclassify_all", lambda *a, **k: (langsam.wait(5), original())[1])

    antwort = client.post("/einstellungen/klassifizierung",
                          data={"rules_form": "1", "keyword_enabled": "1", "rule-keyword-0-value": "Energie",
                                "rule-keyword-0-active": "1"},
                          follow_redirects=False)
    assert antwort.status_code == 303  # sofort zurück, obwohl die Einstufung noch läuft
    assert client.get("/api/status").json()["reclassify"]["running"] is True
    assert "neu eingestuft" in client.get("/einstellungen").text  # Hinweis-Leiste steht auf der Seite

    langsam.set()
    for _ in range(100):
        if not fetcher.reclassify_state["running"]:
            break
        time.sleep(0.1)
    assert fetcher.reclassify_state["running"] is False
    assert fetcher.reclassify_state["counts"]["gesamt"] > 0


# ---------- Schnittstelle für den Worker ----------

KOPF = {"X-EVC-Token": "geheim"}


@pytest.fixture
def mit_token(monkeypatch):
    monkeypatch.setattr(config, "AI_TOKEN", "geheim")


def test_api_is_absent_without_a_token_in_the_env(client):
    assert client.get("/api/ki/offen").status_code == 404
    assert client.post("/api/ki/ergebnis", json=[]).status_code == 404


def test_api_rejects_a_wrong_token(client, mit_token):
    assert client.get("/api/ki/offen").status_code == 401
    assert client.get("/api/ki/offen", headers={"X-EVC-Token": "falsch"}).status_code == 401
    assert client.get("/api/ki/offen", headers=KOPF).status_code == 200


def test_api_delivers_the_same_prompt_as_the_page(client, mit_token):
    enable(AI_PRUEFUNG)  # damit mehr als ein Eintrag in Frage kommt
    ai.mark_candidates()
    daten = client.get("/api/ki/offen?limit=2", headers=KOPF).json()
    assert daten["aktiv"] is True and daten["modus"] == AI_PRUEFUNG
    assert len(daten["eintraege"]) == 2 and daten["offen"] >= 2
    assert PROFIL in daten["prompt"]
    assert str(daten["eintraege"][0]["id"]) in daten["prompt"]


def test_api_reports_that_the_selection_is_switched_off(client, mit_token):
    daten = client.get("/api/ki/offen", headers=KOPF).json()
    assert daten == {"aktiv": False, "offen": 0, "eintraege": [], "prompt": ""}


def test_api_accepts_raw_text_and_reclassifies(client, mit_token):
    enable(AI_PRUEFUNG)
    ai.mark_candidates()
    tender_id = one(NICHT_INTERESSANT)
    roh = f'```json\n[{{"id": {tender_id}, "interessant": true, "begruendung": "Energiekonzept"}}]\n```'
    antwort = client.post("/api/ki/ergebnis", json={"antwort": roh}, headers=KOPF)
    assert antwort.status_code == 200
    assert antwort.json()["gespeichert"] == 1 and antwort.json()["ids"] == [tender_id]
    assert field(tender_id, "ai_result", "auto_status") == ("treffer", INTERESSANT)


def test_api_accepts_a_plain_list(client, mit_token):
    enable(AI_MARKIERUNG)
    tender_id = one(INTERESSANT)
    antwort = client.post("/api/ki/ergebnis", headers=KOPF,
                          json=[{"id": tender_id, "interessant": False, "begruendung": "Bauleistung"}])
    assert antwort.status_code == 200
    assert field(tender_id, "ai_result", "ai_match") == ("kein_treffer", 0)


def test_api_reports_errors_and_unknown_numbers(client, mit_token):
    enable(AI_MARKIERUNG)
    kaputt = client.post("/api/ki/ergebnis", json={"antwort": "kein JSON"}, headers=KOPF)
    assert kaputt.status_code == 400 and kaputt.json()["fehler"]
    unbekannt = client.post("/api/ki/ergebnis", headers=KOPF,
                            json=[{"id": 999999, "interessant": True, "begruendung": "gibt es nicht"}])
    assert unbekannt.status_code == 200 and unbekannt.json() | {"offen": 0} == {
        "gespeichert": 0, "ids": [], "unbekannt": [999999], "fehler": [], "offen": 0}


# ---------- Tägliche Mail und KI-Worker ----------

def test_mail_marks_ai_hits_first_with_reason(client):
    enable(AI_MARKIERUNG)
    with connect() as conn:  # ein zweiter interessanter Eintrag, damit die Reihenfolge etwas zeigt
        conn.execute("INSERT OR IGNORE INTO rules(kind, value) VALUES('keyword', 'Glasfaser')")
    fetcher.reclassify_all()
    with connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM tenders WHERE auto_status = 'interessant' ORDER BY id DESC")]
    assert len(ids) >= 2
    judge(ids[0], reason="Energiekonzept für Schulen")  # der „letzte“ wird KI-Treffer
    eintraege = mailer.collect("2000-01-01T00:00:00", "2000-01-01")
    assert eintraege[0]["id"] == ids[0]  # KI-Treffer steht oben

    nachricht = mailer.build_message(eintraege, ["a@example.org"])
    assert "1 KI-Auswahl" in nachricht["Subject"]
    text = nachricht.get_body(("plain",)).get_content()
    html = nachricht.get_body(("html",)).get_content()
    assert "[KI]" in text and "KI: Energiekonzept für Schulen" in text
    assert "🤖 KI" in html and "Energiekonzept für Schulen" in html


def test_mail_waits_only_for_a_worker_that_reports_in(client, monkeypatch):
    enable(AI_PRUEFUNG)
    ai.mark_candidates()
    monkeypatch.setattr(ai, "worker_state", {"last_seen": None})
    monkeypatch.setattr(config, "AI_TOKEN", "geheim")
    assert ai.mail_should_wait() == 0  # Worker hat sich nie gemeldet → pünktlich verschicken

    client.get("/api/ki/offen", headers={"X-EVC-Token": "geheim"})  # Worker fragt nach
    assert ai.mail_should_wait() == ai.open_count() > 0

    monkeypatch.setattr(config, "AI_TOKEN", "")  # ohne Token gibt es keinen Worker
    assert ai.mail_should_wait() == 0


class FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append(kwargs)


def test_daily_mail_waits_and_then_goes_out_anyway(monkeypatch):
    uhr = {"jetzt": "2026-10-26T07:30:00"}
    versandt = []
    fake = FakeScheduler()
    monkeypatch.setattr(scheduler, "_scheduler", fake)
    monkeypatch.setattr(scheduler, "now", lambda: uhr["jetzt"])
    monkeypatch.setattr(scheduler.mailer, "run_digest", lambda: versandt.append(uhr["jetzt"]) or {"sent": True})
    monkeypatch.setattr(scheduler.ai, "mail_should_wait", lambda: 7)  # 7 Bewertungen offen

    scheduler._run_digest()  # 7:30 – wartet
    assert versandt == [] and fake.jobs[-1]["kwargs"] == {"geplant": "2026-10-26T07:30:00"}

    uhr["jetzt"] = "2026-10-26T07:55:00"
    scheduler._run_digest(geplant="2026-10-26T07:30:00")  # nach 25 Minuten – wartet noch
    assert versandt == []

    uhr["jetzt"] = "2026-10-26T08:00:00"
    scheduler._run_digest(geplant="2026-10-26T07:30:00")  # nach 30 Minuten – geht trotzdem raus
    assert versandt == ["2026-10-26T08:00:00"]


def test_daily_mail_goes_out_at_once_when_nothing_is_open(monkeypatch):
    versandt = []
    fake = FakeScheduler()
    monkeypatch.setattr(scheduler, "_scheduler", fake)
    monkeypatch.setattr(scheduler.mailer, "run_digest", lambda: versandt.append(1) or {"sent": True})
    monkeypatch.setattr(scheduler.ai, "mail_should_wait", lambda: 0)
    scheduler._run_digest()
    assert versandt == [1] and fake.jobs == []


def test_list_shows_the_ai_reason_under_the_title(client):
    enable(AI_MARKIERUNG)
    tender_id = one(INTERESSANT)
    judge(tender_id, reason="Energiekonzept für eine Schule, passt zur Gebäudeberatung")
    text = client.get("/?status=alle&neu=0&frist=alle&t_ki=1").text
    assert "KI-Einschätzung: Energiekonzept für eine Schule, passt zur Gebäudeberatung" in text
    # steht zwischen Titel und Auftraggeber
    zeile = text[text.index("KI-Einschätzung"):]
    assert zeile.index("</div>") < zeile.index("class=\"sub\"")


def test_list_also_shows_the_reason_for_a_rejection(client):
    enable(AI_MARKIERUNG)
    tender_id = one(INTERESSANT)
    judge(tender_id, "kein_treffer", "Bauleistung nach VOB, keine Beratung")
    text = client.get("/?status=alle&neu=0&frist=alle").text
    assert "KI-Einschätzung: Bauleistung nach VOB, keine Beratung" in text


def test_list_stays_clean_without_a_verdict(client):
    enable(AI_MARKIERUNG)
    assert "KI-Einschätzung" not in client.get("/?status=alle&neu=0&frist=alle").text
