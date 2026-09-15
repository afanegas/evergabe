import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from app import config, fetcher, settings
from app.categories import SOURCE_BERLIN, SOURCE_OV
from app.db import connect, init_db
from app.dedupe import authorities_match, normalize_title
from app.sources.oeffentlichevergabe import parse_export, parse_notice, region_for

FIXTURES = Path(__file__).parent / "fixtures" / "ov"


def notice(name: str) -> dict:
    return parse_notice((FIXTURES / f"{name}.xml").read_bytes())


# ---------- Parser ----------

def test_eforms_contract_notice_berlin():
    item = notice("cn_berlin_eforms")
    assert item["category"] == "bekanntmachung"
    assert item["region"] == "berlin"
    assert item["title"] == "ITDZ Berlin: RV Multifunktionsgeräte"
    assert item["authority"] == "IT-Dienstleistungszentrum Berlin (AöR)"
    assert item["deadline"] == "2026-09-25T11:00"
    assert item["deadline_label"] == "Angebotsfrist"
    assert item["procedure"] == "Offenes Verfahren"
    assert [c["code"] for c in item["cpv_codes"]] == ["30000000"]
    assert item["url"] == "https://oeffentlichevergabe.de/ui/de/notices/b86a169d-9114-4df0-9083-65e46dd1f126"
    assert item["detail_fields"]["Geschätzter Auftragswert"] == "42.027.000,00 EUR"
    assert item["uid"] == "ov:18bd45e9-7e72-4178-8955-a7683f78854d:bekanntmachung"


def test_national_short_format():
    item = notice("cn_berlin_national")
    assert item["region"] == "berlin"
    assert item["authority"].startswith("Technische Universität Berlin")
    assert item["procedure"] == "Öffentliche Ausschreibung"
    assert item["deadline"] == "2026-09-24T10:00"
    assert item["documents"][0]["url"].startswith("https://www.dtvp.de/")


def test_award_notice_with_winner_in_brandenburg():
    item = notice("can_winner_brandenburg")
    assert item["category"] == "vergeben"
    assert item["region"] == "brandenburg"
    assert item["contractor"] == "Deutsche Telekom Security GmbH"


def test_other_notice_types():
    assert notice("veat")["category"] == "direktvergabe"
    assert notice("can_modif")["category"] == "vertragsaenderung"
    assert notice("pin_only")["category"] == "vorinformation"


def test_intended_restricted_tender_from_national_format():
    xml = (FIXTURES / "cn_berlin_national.xml").read_text(encoding="utf-8")
    xml = xml.replace(">de-open<", ">beabsichtigte Beschränkte Ausschreibung<")
    item = parse_notice(xml.encode("utf-8"))
    assert item["category"] == "beschraenkt"
    assert item["uid"].endswith(":beschraenkt")


def test_region_prefers_place_of_performance_then_buyer():
    assert region_for(["DE111", "DE300"]) == "berlin"
    assert region_for(["DE403"]) == "brandenburg"
    assert region_for(["DE212"]) == "rest"
    assert region_for([]) is None
    # veat: Leistungsort nur als Text („Frankfurt am Main“) → Sitz des Auftraggebers (Berlin)
    assert notice("veat")["region"] == "berlin"


def test_parse_export_reads_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in FIXTURES.glob("*.xml"):
            archive.writestr(path.name, path.read_bytes())
        archive.writestr("kaputt.xml", b"<nicht-xml")
    items, skipped = parse_export(buffer.getvalue())
    assert len(items) == 6
    assert skipped == 1


def test_days_to_load():
    today = date(2026, 9, 15)
    assert fetcher.ov_days(None, today)[0] == date(2026, 9, 8)  # 7 Tage rückwirkend
    assert fetcher.ov_days(None, today)[-1] == date(2026, 9, 14)  # höchstens gestern
    assert fetcher.ov_days("2026-09-13", today) == [date(2026, 9, 13), date(2026, 9, 14)]
    assert len(fetcher.ov_days("2026-01-01", today)) == config.OV_MAX_DAYS_PER_RUN


# ---------- Speichern, Zusammenführen, Aufräumen ----------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    init_db()
    settings.seed_rules()


def berlin_item(**overrides) -> dict:
    item = {
        "uid": "https://meinauftrag.rib.de/public/DetailsByPlatformIdAndTenderId/platformId/2/tenderId/1",
        "category": "bekanntmachung",
        "title": "ITDZ Berlin: RV Multifunktionsgeräte",
        "url": "https://meinauftrag.rib.de/public/DetailsByPlatformIdAndTenderId/platformId/2/tenderId/1",
        "published_at": "2026-09-08T10:00",
        "authority": "IT-Dienstleistungszentrum Berlin",
        "feed_fields": {"Verfahrensart": "Offenes Verfahren (VgV)"},
    }
    item.update(overrides)
    return item


def sources_of(conn, tender_id):
    return sorted(r[0] for r in conn.execute("SELECT source FROM tender_sources WHERE tender_id = ?", (tender_id,)))


def test_duplicate_from_other_source_is_merged(db):
    with connect() as conn:
        fetcher.upsert_items(conn, [berlin_item()], SOURCE_BERLIN)
        stats = fetcher.upsert_items(conn, [notice("cn_berlin_eforms")], SOURCE_OV)
        assert (stats["new"], stats["merged"]) == (0, 1)
        row = conn.execute("SELECT * FROM tenders").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0] == 1
        assert sources_of(conn, row["id"]) == [SOURCE_BERLIN, SOURCE_OV]
        assert row["source"] == SOURCE_BERLIN
        assert row["deadline"] == "2026-09-25T11:00"  # fehlende Frist aus oeffentlichevergabe.de ergänzt
        assert row["authority"] == "IT-Dienstleistungszentrum Berlin"  # Berlin-Wert bleibt

        # erneuter Import derselben Tagesdatei ändert nichts
        stats = fetcher.upsert_items(conn, [notice("cn_berlin_eforms")], SOURCE_OV)
        assert (stats["new"], stats["updated"], stats["merged"]) == (0, 1, 0)


def test_berlin_item_merges_into_existing_ov_entry(db):
    with connect() as conn:
        fetcher.upsert_items(conn, [notice("cn_berlin_eforms")], SOURCE_OV)
        stats = fetcher.upsert_items(conn, [berlin_item()], SOURCE_BERLIN)
        assert stats["merged"] == 1
        row = conn.execute("SELECT * FROM tenders").fetchone()
        assert row["detail_status"] == "offen"  # CPV/Kurzbeschreibung von der RIB-Seite trotzdem nachladen


def test_different_authority_or_short_title_is_not_merged(db):
    with connect() as conn:
        fetcher.upsert_items(conn, [berlin_item(authority="Bezirksamt Pankow")], SOURCE_BERLIN)
        assert fetcher.upsert_items(conn, [notice("cn_berlin_eforms")], SOURCE_OV)["merged"] == 0

        short = berlin_item(uid="x", url="x", title="Malerarbeiten", authority=None)
        ov_short = {**notice("cn_berlin_national"), "title": "Malerarbeiten", "uid": "ov:y"}
        fetcher.upsert_items(conn, [short], SOURCE_BERLIN)
        assert fetcher.upsert_items(conn, [ov_short], SOURCE_OV)["merged"] == 0


def test_dedupe_helpers():
    assert normalize_title("ITDZ Berlin: RV Multifunktionsgeräte") == normalize_title("itdz berlin – RV multifunktionsgeräte")
    assert authorities_match("Bezirksamt Mitte von Berlin", "Bezirksamt Mitte") is True
    assert authorities_match("Bezirksamt Mitte", "Bezirksamt Pankow") is False
    assert authorities_match(None, "Gewobag") is None


def test_cleanup_removes_only_old_uninteresting_rest_entries(db):
    with connect() as conn:
        old = {**notice("can_modif"), "published_at": "2026-01-01T10:00"}
        kept_note = {**notice("can_modif"), "uid": "ov:note", "published_at": "2026-01-01T10:00"}
        berlin_old = {**notice("veat"), "published_at": "2026-01-01T10:00"}
        fetcher.upsert_items(conn, [old, kept_note, berlin_old], SOURCE_OV)
        conn.execute("UPDATE tenders SET note = 'merken' WHERE uid = 'ov:note'")
    fetcher.reclassify_all()
    assert fetcher.cleanup_rest() == 1
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM tender_sources").fetchone()[0] == 2
