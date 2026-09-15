from pathlib import Path

from app.sources.berlin import parse_description, parse_feed, parse_german_datetime
from app.sources.details import detail_kind, parse_berlin, parse_rib

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_german_datetime():
    assert parse_german_datetime("14.10.2026, 11:00 Uhr") == "2026-10-14T11:00"
    assert parse_german_datetime("24.09.2026 09:30 Uhr") == "2026-09-24T09:30"
    assert parse_german_datetime("11.09.2026") == "2026-09-11"
    assert parse_german_datetime("unbekannt") is None


def test_description_continuation_lines():
    fields = parse_description("Auftragsgegenstand: Zeile eins<br>weiter geht's<br>Online seit: 01.02.2026")
    assert fields == {"Auftragsgegenstand": "Zeile eins weiter geht's", "Online seit": "01.02.2026"}


def test_feed_bekanntmachungen():
    items = parse_feed((FIXTURES / "bekanntmachungen.rss").read_bytes(), "bekanntmachung")
    assert len(items) == 50
    first = items[0]
    assert first["title"] == "Rahmenvertrag Schädlingsbekämpfung 2026"
    assert first["procedure"] == "Offenes Verfahren (VgV)"
    assert first["deadline"] == "2026-10-14T11:00"
    assert first["deadline_label"] == "Ablauf Angebotsfrist"
    assert first["published_at"] == "2026-09-13T10:33"
    assert detail_kind(first["url"]) == "rib"


def test_feed_vergeben_and_vorinformation():
    vergeben = parse_feed((FIXTURES / "info-19-20.rss").read_bytes(), "vergeben")
    assert vergeben[0]["contractor"] == "Aqua Service Schwerin"
    assert vergeben[0]["authority"].startswith("Land Berlin")
    assert detail_kind(vergeben[0]["url"]) == "berlin"

    vorinfo = parse_feed((FIXTURES / "vorinformationen.rss").read_bytes(), "vorinformation")
    assert len(vorinfo) == 3
    assert vorinfo[0]["execution_period"] == "01.01.2028 - 31.12.2031"


def test_rib_detail():
    detail = parse_rib(read("rib_detail.html"))
    assert [c["code"] for c in detail["cpv_codes"]] == ["03100000-2", "45112712-9", "45236230-1", "77300000-3"]
    assert detail["cpv_codes"][3]["label"] == "Dienstleistungen im Gartenbau"
    assert detail["authority"] == "Bezirksamt Mitte von Berlin"
    assert detail["description"].splitlines()[:2] == ["Gesamtsanierung der Senioren-Begegnungsstätte Otawi-Treff", "Otawistraße 46"]
    assert detail["procedure"] == "Öffentliche Ausschreibung"
    assert detail["fields"]["Vergabeordnung"] == "VOB/A"


def test_berlin_details():
    vergeben = parse_berlin(read("berlin_vergeben.html"))
    assert vergeben["contractor"] == "Aqua Service Schwerin"
    assert vergeben["procedure"] == "Beschränkte Ausschreibung"
    assert vergeben["documents"][0]["url"].startswith("https://my.vergabeplattform.berlin.de/")

    vorinfo = parse_berlin(read("berlin_vorinfo.html"))
    assert vorinfo["authority"] == "Museum für Naturkunde Berlin"
    assert vorinfo["cpv_codes"] == []
