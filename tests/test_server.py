"""Phase 2: Health-Check, Schutz gegen fremde Formular-Absendungen, E-Mail und Datensicherung."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import backup, config, fetcher, mailer, main, settings
from app.db import connect
from app.sources.berlin import parse_feed

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, message):
        FakeSMTP.sent.append(message)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.org")
    monkeypatch.setattr(config, "SMTP_FROM", "evchecker@example.org")
    monkeypatch.setattr(config, "SMTP_USER", "evchecker@example.org")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "geheim")
    monkeypatch.setattr(config, "BASE_URL", "https://evchecker.example.org")
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    with TestClient(main.app) as c:
        items = parse_feed((FIXTURES / "bekanntmachungen.rss").read_bytes(), "bekanntmachung")
        with connect() as conn:
            fetcher.upsert_items(conn, items)
        settings.replace_rules({"keyword": [{"value": "Glasfaser", "active": True}],
                                "priority": [{"value": "Signallieferung", "active": True}]})
        fetcher.reclassify_all()
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_cross_site_posts_are_rejected(client):
    data = {"status": "interessant", "next": "/"}
    assert client.post("/eintrag/1/status", data=data, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.post("/eintrag/1/status", data=data, headers={"origin": "https://boese.example"}).status_code == 403
    ok = client.post("/eintrag/1/status", data=data, follow_redirects=False,
                     headers={"sec-fetch-site": "same-origin", "origin": "http://testserver"})
    assert ok.status_code == 303


def test_mail_settings_validation_and_save(client):
    response = client.post("/einstellungen/mail", data={"mail_enabled": "1", "mail_to": "kaputt", "mail_time": "7 Uhr"})
    assert "Ungültige E-Mail-Adresse: kaputt" in response.text and "genau eine Uhrzeit" in response.text
    client.post("/einstellungen/mail", data={"mail_enabled": "1", "mail_to": "a@example.org; b@example.org", "mail_time": "7:05"})
    values = settings.get_all()
    assert (values["mail_enabled"], values["mail_to"], values["mail_time"]) == (True, "a@example.org, b@example.org", "07:05")
    html = client.get("/einstellungen").text
    assert "smtp.example.org:587" in html and "geheim" not in html  # Passwort wird nie angezeigt


def test_digest_contains_new_interesting_entries_and_only_once(client):
    settings.set_many({"mail_enabled": True, "mail_to": "a@example.org"})
    settings.set_many({"mail_last_sent_at": "2000-01-01T00:00:00"})

    result = mailer.run_digest()
    assert result["sent"] and result["count"] == 1
    message = FakeSMTP.sent[-1]
    assert message["Subject"] == "eV-Checker: 1 neue interessante Treffer (1 wichtig)"
    assert message["To"] == "a@example.org"
    html = message.get_body(("html",)).get_content()
    text = message.get_body(("plain",)).get_content()
    assert "Glasfaserausbau und Signallieferung" in html and "★ WICHTIG" in html
    assert "https://evchecker.example.org/eintrag/" in html and "[WICHTIG]" in text

    # zweiter Lauf ohne neue Einträge: keine Mail
    result = mailer.run_digest()
    assert not result["sent"] and len(FakeSMTP.sent) == 1


def test_digest_disabled_or_unconfigured(client, monkeypatch):
    settings.set_many({"mail_enabled": False, "mail_to": "a@example.org"})
    assert mailer.run_digest()["reason"] == "E-Mail ist ausgeschaltet"
    monkeypatch.setattr(config, "SMTP_HOST", "")
    assert "nicht konfiguriert" in mailer.run_digest(test=True)["reason"]


def test_test_mail_button(client):
    settings.set_many({"mail_to": "a@example.org"})
    response = client.post("/einstellungen/mail/test")
    assert "Test-Mail an a@example.org verschickt" in response.text
    assert FakeSMTP.sent[-1]["Subject"].startswith("[Test] ")
    assert settings.get_all()["mail_last_sent_at"] is None  # Test verändert den Stand nicht


def test_backup_is_consistent_and_pruned(client, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_KEEP", 2)
    names = []
    for i in range(3):
        monkeypatch.setattr(backup, "now", lambda i=i: f"2026-09-1{i}T03:00:00")
        names.append(backup.create_backup()["name"])
    listed = [b["name"] for b in backup.list_backups()]
    assert listed == [names[2], names[1]]  # neueste zuerst, älteste gelöscht
    copy = sqlite3.connect(config.BACKUP_DIR / names[2])
    assert copy.execute("SELECT COUNT(*) FROM tenders").fetchone()[0] == 50
    copy.close()

    response = client.post("/einstellungen/backup")
    assert "Datensicherung erstellt" in response.text
    assert "ev-checker-" in client.get("/einstellungen").text


def test_redirect_keeps_message_before_fragment(client):
    response = client.post("/einstellungen/backup", follow_redirects=False)
    location = response.headers["location"]
    assert location.startswith("/einstellungen?meldung=") and location.endswith("#backup")
