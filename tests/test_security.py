"""Absicherung: Links aus Fremddaten, Weiterleitungsziele, Sicherheitsheader, Grenzen beim Abruf."""

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, fetcher, main
from app.db import connect, now
from app.sources import parse_xml
from app.sources.details import detail_kind
from app.sources.oeffentlichevergabe import MAX_ENTRY_BYTES, parse_export
from app.urls import safe_url

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    with TestClient(main.app) as c:
        yield c


def add_tender(documents: list[dict], url: str = "https://www.berlin.de/x") -> int:
    ts = now()
    with connect() as conn:
        tender_id = conn.execute(
            "INSERT INTO tenders(uid, category, title, url, documents, first_seen, last_seen) "
            "VALUES('t1', 'bekanntmachung', 'Testeintrag', ?, ?, ?, ?)",
            (url, json.dumps(documents), ts, ts),
        ).lastrowid
        conn.execute("INSERT INTO tender_sources(tender_id, source, uid, url, first_seen) "
                     "VALUES(?, 'berlin', 't1', ?, ?)", (tender_id, url, ts))
    return tender_id


# ---------- 3) Links aus Fremddaten ----------

def test_safe_url_laesst_nur_http_und_https_durch():
    assert safe_url("https://example.org/a") == "https://example.org/a"
    assert safe_url(" http://example.org/a ") == "http://example.org/a"
    for bad in ("javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,<script>alert(1)</script>",
                "vbscript:x", "/relativ", "", None):
        assert safe_url(bad) == ""


def test_detailseite_verlinkt_kein_javascript(client):
    tender_id = add_tender([{"title": "Vergabeunterlagen", "url": "javascript:alert(document.domain)"},
                            {"title": "Echtes Dokument", "url": "https://example.org/datei.pdf"}])
    html = client.get(f"/eintrag/{tender_id}").text
    assert 'href="javascript:' not in html and "javascript:alert" not in html.split("Adresse nicht übernommen")[0]
    assert 'href="https://example.org/datei.pdf"' in html
    assert "Adresse nicht übernommen" in html


def test_quellenlink_mit_fremdem_schema_wird_nicht_verlinkt(client):
    tender_id = add_tender([], url="javascript:alert(1)")
    html = client.get(f"/eintrag/{tender_id}").text
    assert 'href="javascript:' not in html


# ---------- 4) Weiterleitungsziele ----------

def test_next_parameter_bleibt_in_der_app(client):
    tender_id = add_tender([])
    for ziel in ("//fremd.example", "/\\fremd.example", "https://fremd.example", "/\tfremd.example",
                 "/\r\n//fremd.example"):
        response = client.post(f"/eintrag/{tender_id}/notiz", data={"note": "x", "next": ziel},
                               follow_redirects=False)
        assert response.headers["location"].startswith("/?") or response.headers["location"] == "/", ziel
    ok = client.post(f"/eintrag/{tender_id}/notiz", data={"note": "x", "next": "/?status=alle"},
                     follow_redirects=False)
    assert ok.headers["location"].startswith("/?status=alle")


# ---------- 6) Sicherheitsheader ----------

def test_sicherheitsheader_auf_jeder_seite(client):
    response = client.get("/")
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_keine_skripte_oder_stile_im_html(client):
    """Die CSP erlaubt keine eingebetteten Skripte/Stile – sonst wäre die Seite kaputt."""
    tender_id = add_tender([])
    for url in ("/", "/einstellungen", "/auftraggeber", f"/eintrag/{tender_id}"):
        html = client.get(url).text
        assert "<script>" not in html and "style=" not in html, url
        assert "onsubmit=" not in html and "onclick=" not in html, url


# ---------- 7) Seitengröße ----------

def test_seitengroesse_ist_gedeckelt():
    assert main._limit({"anzahl_a": "999999999"}, "a") == config.MAX_PAGE_SIZE
    assert main._limit({"anzahl_a": "1"}, "a") == config.PAGE_SIZE
    assert main._limit({"anzahl_a": "kaputt"}, "a") == config.PAGE_SIZE
    assert main._limit({"anzahl_a": str(config.PAGE_SIZE * 2)}, "a") == config.PAGE_SIZE * 2


# ---------- 8) Erlaubte Hosts für Detailseiten ----------

def test_detail_kind_prueft_den_host_genau():
    assert detail_kind("https://meinauftrag.rib.de/x") == "rib"
    assert detail_kind("https://www.berlin.de/vergabeplattform/veroeffentlichungen/x") == "berlin"
    assert detail_kind("https://berlin.de/vergabeplattform/veroeffentlichungen/x") == "berlin"
    for bad in ("https://boeseberlin.de/vergabeplattform/veroeffentlichungen/x",
                "https://berlin.de.fremd.example/vergabeplattform/veroeffentlichungen/x",
                "https://www.berlin.de@fremd.example/vergabeplattform/veroeffentlichungen/x",
                "file:///etc/passwd", "http://127.0.0.1:8000/vergabeplattform/veroeffentlichungen/x"):
        assert detail_kind(bad) is None, bad


def test_weiterleitung_nur_zu_erlaubten_hosts():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "meinauftrag.rib.de" and request.url.path == "/um":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/intern"})
        if request.url.path == "/weiter":
            return httpx.Response(302, headers={"location": "https://meinauftrag.rib.de/ziel"})
        return httpx.Response(200, text="<html>Ziel</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert b"Ziel" in fetcher.fetch_detail_page(c, "https://meinauftrag.rib.de/weiter")
        with pytest.raises(RuntimeError, match="nicht erlaubte Adresse"):
            fetcher.fetch_detail_page(c, "https://meinauftrag.rib.de/um")


# ---------- 9) Grenzen für XML, ZIP und Downloads ----------

def test_xml_mit_dtd_wird_abgelehnt():
    bombe = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">
        <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]><rss>&lol2;</rss>"""
    with pytest.raises(ValueError, match="DTD"):
        parse_xml(bombe)
    assert parse_xml(b"<rss><item>ok</item></rss>").tag == "rss"


def test_feed_mit_dtd_laesst_den_abruf_nicht_abstuerzen(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from app.db import init_db
    init_db()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss/>')

    from app.sources.berlin import FEEDS
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        totals, errors = fetcher.fetch_berlin_feeds(c, FEEDS[:1], {})
    assert totals["new"] == 0 and errors and "DTD" in errors[0]


def test_zu_grosse_datei_im_export_wird_uebersprungen():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bombe.xml", b"<a>" + b"x" * (MAX_ENTRY_BYTES + 100) + b"</a>")
        archive.writestr("egal.txt", b"kein XML")
    items, skipped = parse_export(buffer.getvalue())
    assert items == [] and skipped == 1


def test_zu_grosse_antwort_wird_abgebrochen():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert len(fetcher._get(c, "https://example.org/a", 5000)[0]) == 5000
        with pytest.raises(RuntimeError, match="größer als"):
            fetcher._get(c, "https://example.org/a", 4000)


def test_detailseite_wird_weiterhin_vollstaendig_ausgewertet(client, monkeypatch):
    """Die Detailseite kommt jetzt als Bytes aus dem begrenzten Download – die Auswertung
    (CPV-Codes, Vergabestelle, Beschreibung) muss unverändert funktionieren."""
    from app.sources.berlin import parse_feed
    items = parse_feed((FIXTURES / "bekanntmachungen.rss").read_bytes(), "bekanntmachung")
    with connect() as conn:
        fetcher.upsert_items(conn, items)
        row = conn.execute("SELECT t.id, s.url FROM tenders t JOIN tender_sources s ON s.tender_id = t.id "
                           "WHERE t.detail_status = 'offen' LIMIT 1").fetchone()
    assert row, "Testdaten enthalten keine abzurufende Detailseite"

    seiten = (FIXTURES / "rib_detail.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=seiten, headers={"content-type": "text/html; charset=utf-8"})

    monkeypatch.setattr(config, "DETAIL_DELAY_SECONDS", 0)
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        fetched, errors = fetcher.fetch_details(c)
    assert fetched >= 1 and not errors
    with connect() as conn:
        item = conn.execute("SELECT cpv_codes, authority, description, detail_status FROM tenders WHERE id = ?",
                            (row["id"],)).fetchone()
    assert item["detail_status"] == "ok"
    assert json.loads(item["cpv_codes"]) and item["authority"] and item["description"]
