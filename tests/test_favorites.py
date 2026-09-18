"""Abgelaufene Fristen ausblenden und beobachtete Auftraggeber (Reiter „♥ Auftraggeber“)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, fetcher, mailer, main, settings
from app.db import connect
from app.sources.berlin import parse_feed
from app.sources.details import parse_rib

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSMTP:
    sent: list = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        FakeSMTP.sent.append(message)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    with TestClient(main.app) as c:
        items = parse_feed((FIXTURES / "bekanntmachungen.rss").read_bytes(), "bekanntmachung")
        items += parse_feed((FIXTURES / "info-19-20.rss").read_bytes(), "vergeben")
        with connect() as conn:
            fetcher.upsert_items(conn, items)
            first_id = conn.execute("SELECT id FROM tenders ORDER BY id LIMIT 1").fetchone()[0]
            fetcher.apply_detail(conn, first_id, parse_rib((FIXTURES / "rib_detail.html").read_text(encoding="utf-8")))
            # Auftraggeber, wie sie nach dem Laden der RIB-Detailseiten aussehen
            conn.execute("UPDATE tenders SET authority = 'GESOBAU AG' WHERE title = 'Rahmenvertrag Schädlingsbekämpfung 2026'")
            conn.execute("UPDATE tenders SET authority = 'HOWOGE Wohnungsbaugesellschaft mbH' WHERE title = 'Glasfaserausbau und Signallieferung'")
        fetcher.reclassify_all()
        yield c


def set_now(monkeypatch, value: str):
    from app import tenders
    monkeypatch.setattr(tenders, "now", lambda: value)


# ---------- Fristen ----------

def test_expired_deadlines_hidden_by_default(client, monkeypatch):
    with connect() as conn:
        conn.execute("UPDATE tenders SET deadline = '2026-09-01T10:00' WHERE title = 'Glasfaserausbau und Signallieferung'")
        conn.execute("UPDATE tenders SET deadline = '2026-09-15' WHERE title = 'Rahmenvertrag Schädlingsbekämpfung 2026'")
    set_now(monkeypatch, "2026-09-15T18:00:00")

    html = client.get("/?status=alle").text
    assert "1 mit abgelaufener Frist ausgeblendet" in html
    assert "frist=alle" in html
    all_html = client.get("/?status=alle&frist=alle&anzahl_berlin-ausschreibungen=100").text
    default_html = client.get("/?status=alle&anzahl_berlin-ausschreibungen=100").text
    assert "Glasfaserausbau und Signallieferung" in all_html
    assert "Glasfaserausbau und Signallieferung" not in default_html
    assert "Rahmenvertrag Schädlingsbekämpfung 2026" in default_html  # Frist ohne Uhrzeit gilt bis Tagesende

    # Einträge ohne Frist (vergebene Aufträge) bleiben sichtbar, außer bei „nur offene Fristen“
    assert "Trinkwasseruntersuchungen" in client.get("/gruppe/berlin/vergeben?status=alle").text
    assert "Trinkwasseruntersuchungen" not in client.get("/gruppe/berlin/vergeben?status=alle&frist=offen").text


# ---------- Beobachtete Auftraggeber ----------

def watch(client, name, next_url="/auftraggeber"):
    return client.post("/auftraggeber/beobachten", data={"name": name, "next": next_url}, follow_redirects=False)


def test_watch_from_detail_page_marks_entries(client):
    detail = client.get("/eintrag/1").text
    assert "♥ Auftraggeber beobachten" in detail  # Auftraggeber vorhanden (aus Detailseite)

    response = watch(client, "GESOBAU", "/eintrag/1")  # Eintrag 1: GESOBAU AG
    assert response.status_code == 303
    assert settings.load_favorites() == ["GESOBAU"]
    with connect() as conn:
        row = conn.execute("SELECT favorite, authority_matches FROM tenders WHERE id = 1").fetchone()
    assert row["favorite"] == 1 and "GESOBAU" in row["authority_matches"]

    assert "♥ beobachtet" in client.get("/eintrag/1").text
    list_html = client.get("/?status=alle").text
    assert 'class="pill-fav"' in list_html

    # nochmal beobachten legt kein Duplikat an
    watch(client, "gesobau")
    assert settings.load_favorites() == ["GESOBAU"]


def test_favorites_page_grouped_by_authority_default_all(client):
    empty = client.get("/auftraggeber").text
    assert "Noch keine Auftraggeber beobachtet" in empty

    watch(client, "GESOBAU")
    watch(client, "Bezirksamt Mitte")
    html = client.get("/auftraggeber").text
    assert 'class="tab active" href="/auftraggeber?status=alle' in html  # Standard-Reiter „Alle“
    assert html.index("♥ GESOBAU") < html.index("♥ Bezirksamt Mitte")
    assert "Gefundene Namen: GESOBAU AG" in html
    assert "Rahmenvertrag Schädlingsbekämpfung 2026" in html  # nicht interessant, aber sichtbar

    interesting = client.get("/auftraggeber?status=interessant").text
    assert "Rahmenvertrag Schädlingsbekämpfung 2026" not in interesting

    # nachgeladene Gruppe
    partial = client.get("/auftraggeber/gruppe/ausschreibungen?status=alle&ag=GESOBAU").text
    assert "Rahmenvertrag Schädlingsbekämpfung 2026" in partial
    assert client.get("/auftraggeber/gruppe/ausschreibungen?ag=Unbekannt").status_code == 404


def test_unwatch_and_settings_panel(client):
    watch(client, "GESOBAU")
    response = client.post("/auftraggeber/entfernen", data={"pattern": "GESOBAU", "next": "/auftraggeber"})
    assert "Beobachtung von „GESOBAU“ beendet" in response.text
    assert settings.load_favorites() == []
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tenders WHERE favorite = 1").fetchone()[0] == 0

    # Einstellungen: Liste speichern (mit + und abgeschaltetem Eintrag)
    from tests.test_app import rules_form
    data = rules_form(keyword=["Glasfaser"])
    data.update({"rule-authority-0-value": "HOWOGE", "rule-authority-0-active": "1",
                 "rule-authority-1-value": "Polizei + Berlin"})  # ohne active = abgeschaltet
    client.post("/einstellungen/klassifizierung", data=data)
    assert settings.load_favorites() == ["HOWOGE"]
    assert "♥ Beobachtete Auftraggeber" in client.get("/einstellungen").text
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tenders WHERE favorite = 1").fetchone()[0] >= 1


def test_favorites_are_not_cleaned_up(client):
    with connect() as conn:
        conn.execute(
            "UPDATE tenders SET region = 'rest', auto_status = 'nicht_interessant', published_at = '2025-01-01T10:00' "
            "WHERE authority LIKE 'GESOBAU%' OR title = 'Maler- und Lackierarbeiten'"
        )
    watch(client, "GESOBAU")
    with connect() as conn:
        conn.execute("UPDATE tenders SET auto_status = 'nicht_interessant' WHERE region = 'rest'")
    deleted = fetcher.cleanup_rest()
    with connect() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM tenders WHERE region = 'rest' AND favorite = 1").fetchone()[0]
    assert deleted >= 1 and remaining >= 1


def test_mail_has_favorites_section(client, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.org")
    monkeypatch.setattr(config, "SMTP_FROM", "evchecker@example.org")
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    settings.set_many({"mail_enabled": True, "mail_to": "a@example.org", "mail_last_sent_at": "2000-01-01T00:00:00"})
    settings.replace_rules({"keyword": [], "priority": [], "cpv": []})  # nichts interessant
    fetcher.reclassify_all()

    assert not mailer.run_digest()["sent"]  # ohne beobachtete Auftraggeber keine Mail
    watch(client, "GESOBAU")
    result = mailer.run_digest()
    assert result["sent"] and result["favorites"] >= 1
    message = FakeSMTP.sent[-1]
    assert "neue Einträge beobachteter Auftraggeber" in message["Subject"]
    html = message.get_body(("html",)).get_content()
    assert "♥ Beobachtete Auftraggeber" in html and "Rahmenvertrag Schädlingsbekämpfung 2026" in html
    assert 'href="/auftraggeber"' not in html and "eV-Checker öffnen" not in html  # ohne App-Adresse keine App-Links
    monkeypatch.setattr(config, "BASE_URL", "https://evchecker.example.org")
    html = mailer.build_message([], ["a@example.org"], favorites=mailer.collect_favorites("2000-01-01T00:00:00")) \
        .get_body(("html",)).get_content()
    assert 'href="https://evchecker.example.org/auftraggeber"' in html
    assert "nicht interessant" in message.get_body(("plain",)).get_content()
