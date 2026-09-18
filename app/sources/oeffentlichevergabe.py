"""oeffentlichevergabe.de (Datenservice Öffentlicher Einkauf, Beschaffungsamt des BMI).

Offene Schnittstelle ohne Anmeldung. Tagesexporte aller deutschen Bekanntmachungen, frühestens für gestern:
  https://oeffentlichevergabe.de/api/notice-exports?pubDay=JJJJ-MM-TT&format=eforms.zip

Genutzt wird das eForms-Format, weil nur dieses die Angebotsfristen enthält. Die ZIP-Datei enthält je
Bekanntmachung eine UBL-XML-Datei. EU-Bekanntmachungen (eForms-DE) und nationale Kurzformate
(eforms-sdk-0.1) haben denselben Aufbau, aber unterschiedliche Namensraum-Kürzel – deshalb wird hier
nur über lokale Elementnamen gelesen.
"""

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date

from ..categories import SOURCE_OV
from . import parse_xml

# Grenzen beim Auspacken (Schutz vor überdimensionierten oder absichtlich aufgeblähten Archiven).
# Eine Bekanntmachung ist real deutlich unter 1 MB groß.
MAX_ENTRY_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024

EXPORT_URL = "https://oeffentlichevergabe.de/api/notice-exports?pubDay={day}&format=eforms.zip"
NOTICE_URL = "https://oeffentlichevergabe.de/ui/de/notices/{notice_id}"

# eForms-Bekanntmachungsart (BT-02) -> Kategorie; nicht aufgeführte Arten werden nicht importiert
_NOTICE_TYPES = {
    "pin-only": "vorinformation",
    "pin-buyer": "vorinformation",
    "pin-rtl": "vorinformation",
    "pin-tran": "vorinformation",
    "pin-cfc-standard": "bekanntmachung",
    "pin-cfc-social": "bekanntmachung",
    "qu-sy": "bekanntmachung",
    "cn-standard": "bekanntmachung",
    "cn-social": "bekanntmachung",
    "cn-desg": "bekanntmachung",
    "subco": "bekanntmachung",
    "veat": "direktvergabe",
    "can-standard": "vergeben",
    "can-social": "vergeben",
    "can-desg": "vergeben",
    "can-tran": "vergeben",
    "can-modif": "vertragsaenderung",
}

_NOTICE_TYPE_LABELS = {
    "vorinformation": "Vorinformation",
    "bekanntmachung": "Auftragsbekanntmachung",
    "direktvergabe": "Freiwillige Ex-ante-Transparenzbekanntmachung",
    "vergeben": "Bekanntmachung vergebener Auftrag",
    "vertragsaenderung": "Bekanntmachung einer Vertragsänderung",
}

_PROCEDURES = {
    "open": "Offenes Verfahren",
    "restricted": "Nichtoffenes Verfahren",
    "neg-w-call": "Verhandlungsverfahren mit Teilnahmewettbewerb",
    "neg-wo-call": "Verhandlungsverfahren ohne Teilnahmewettbewerb",
    "comp-dial": "Wettbewerblicher Dialog",
    "innovation": "Innovationspartnerschaft",
    "comp-tend": "Wettbewerb",
    "oth-single": "Sonstiges einstufiges Verfahren",
    "oth-mult": "Sonstiges mehrstufiges Verfahren",
    "exp-int-rail": "Aufruf zur Interessenbekundung",
    "de-open": "Öffentliche Ausschreibung",
    "de-restricted-w-call": "Beschränkte Ausschreibung mit Teilnahmewettbewerb",
    "de-restricted-wo-call": "Beschränkte Ausschreibung ohne Teilnahmewettbewerb",
    "de-comp-w-call": "Verhandlungsvergabe mit Teilnahmewettbewerb",
    "de-comp-wo-call": "Verhandlungsvergabe ohne Teilnahmewettbewerb",
    "de-comp-neg-w-call": "Verhandlungsvergabe mit Teilnahmewettbewerb",
    "de-comp-neg-wo-call": "Verhandlungsvergabe ohne Teilnahmewettbewerb",
    "us-open": "Offenes Verfahren (national)",
    "us-neg-w-call": "Verhandlungsverfahren mit Teilnahmewettbewerb (national)",
}

# Nationale Kurzformate melden beabsichtigte Beschränkte Ausschreibungen als Auftragsbekanntmachung
# mit Klartext statt Verfahrenscode – die gehören in dieselbe Kategorie wie beim Berlin-Feed.
_INTENDED_RESTRICTED = "beabsichtigte beschränkte ausschreibung"

_CONTRACT_NATURE = {"works": "Bauleistung", "services": "Dienstleistung", "supplies": "Lieferleistung"}

_EXT = "UBLExtensions/UBLExtension/ExtensionContent/EformsExtension"


# ---------- XML-Helfer über lokale Namen ----------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element | None, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name] if el is not None else []


def _all(el: ET.Element | None, path: str) -> list[ET.Element]:
    nodes = [el] if el is not None else []
    for part in path.split("/"):
        nodes = [child for node in nodes for child in _children(node, part)]
    return nodes


def _first(el: ET.Element | None, path: str) -> ET.Element | None:
    found = _all(el, path)
    return found[0] if found else None


def _text(el: ET.Element | None, path: str) -> str | None:
    node = _first(el, path)
    value = (node.text or "").strip() if node is not None else ""
    return value or None


def _texts(el: ET.Element | None, path: str) -> list[str]:
    return [t for t in ((n.text or "").strip() for n in _all(el, path)) if t]


def _date(value: str | None) -> str | None:
    """'2026-10-14+02:00' -> '2026-10-14'"""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", value or "")
    return m.group(1) if m else None


def _datetime(day: str | None, time: str | None) -> str | None:
    d = _date(day)
    if not d:
        return None
    m = re.match(r"(\d{2}):(\d{2})", time or "")
    return f"{d}T{m.group(1)}:{m.group(2)}" if m else d


def _german_date(iso: str | None) -> str:
    if not iso:
        return ""
    y, m, d = iso[:10].split("-")
    return f"{d}.{m}.{y}"


def region_for(codes: list[str]) -> str | None:
    """NUTS: DE3… = Berlin, DE4… = Brandenburg. Bei mehreren Orten gewinnt Berlin, dann Brandenburg."""
    codes = [c.upper() for c in codes if c]
    if any(c.startswith("DE3") for c in codes):
        return "berlin"
    if any(c.startswith("DE4") for c in codes):
        return "brandenburg"
    return "rest" if codes else None


# ---------- Bekanntmachung lesen ----------

def parse_notice(content: bytes) -> dict | None:
    root = parse_xml(content)
    category = _NOTICE_TYPES.get(_text(root, "NoticeTypeCode") or "")
    if category is None:
        return None

    notice_id = _text(root, "ID")
    if not notice_id:
        return None
    procedure_id = _text(root, "ContractFolderID") or notice_id

    organizations = {}
    for org in _all(root, f"{_EXT}/Organizations/Organization"):
        company = _first(org, "Company")
        org_id = _text(company, "PartyIdentification/ID")
        if org_id:
            organizations[org_id] = {
                "name": _text(company, "PartyName/Name"),
                "nuts": _text(company, "PostalAddress/CountrySubentityCode"),
                "city": _text(company, "PostalAddress/CityName"),
            }
    tendering_parties = {}
    for party in _all(root, f"{_EXT}/NoticeResult/TenderingParty"):
        names = [organizations.get(i, {}).get("name") for i in _texts(party, "Tenderer/ID")]
        names = [n for n in names if n] or ([_text(party, "Name")] if _text(party, "Name") else [])
        tendering_parties[_text(party, "ID")] = names

    buyer_id = _text(root, "ContractingParty/Party/PartyIdentification/ID")
    buyer = organizations.get(buyer_id or "", {})
    authority = buyer.get("name") or _text(root, "ContractingParty/Party/PartyName/Name")

    project = _first(root, "ProcurementProject")
    lots = _all(root, "ProcurementProjectLot")
    lot_projects = [_first(lot, "ProcurementProject") for lot in lots]
    projects = [p for p in [project, *lot_projects] if p is not None]

    title = _text(project, "Name") or next((_text(p, "Name") for p in lot_projects if _text(p, "Name")), None)
    if not title:
        return None

    # Beschreibung: Verfahren + abweichende Los-Titel/-Beschreibungen (für die Stichwort-Prüfung)
    parts: list[str] = []
    for text in [_text(project, "Description")] + [
        t for p in lot_projects for t in (_text(p, "Name"), _text(p, "Description"))
    ]:
        if text and text != title and text not in parts:
            parts.append(text)

    cpv_codes, seen = [], set()
    for p in projects:
        for code in _texts(p, "MainCommodityClassification/ItemClassificationCode") + _texts(
            p, "AdditionalCommodityClassification/ItemClassificationCode"
        ):
            digits = re.sub(r"\D", "", code)[:8]
            if len(digits) >= 2 and digits not in seen:
                seen.add(digits)
                cpv_codes.append({"code": code, "label": ""})

    place_codes = [c for p in projects for c in _texts(p, "RealizedLocation/Address/CountrySubentityCode")]
    region = region_for(place_codes) or region_for([buyer.get("nuts") or ""]) or "rest"
    cities = []
    for p in projects:
        for address in _all(p, "RealizedLocation/Address"):
            city = " ".join(filter(None, [_text(address, "PostalZone"), _text(address, "CityName")]))
            if city and city not in cities:
                cities.append(city)
        for description in _texts(p, "RealizedLocation/Description"):
            if description not in cities:
                cities.append(description)

    deadlines = sorted(filter(None, (
        _datetime(_text(lot, "TenderingProcess/TenderSubmissionDeadlinePeriod/EndDate"),
                  _text(lot, "TenderingProcess/TenderSubmissionDeadlinePeriod/EndTime"))
        for lot in lots
    )))
    participation = sorted(filter(None, (
        _datetime(_text(lot, "TenderingProcess/ParticipationRequestReceptionPeriod/EndDate"),
                  _text(lot, "TenderingProcess/ParticipationRequestReceptionPeriod/EndTime"))
        for lot in lots
    )))
    if participation:
        deadline, deadline_label = participation[0], "Teilnahmefrist"
    elif deadlines:
        deadline, deadline_label = deadlines[0], "Angebotsfrist"
    else:
        deadline, deadline_label = None, None

    starts = sorted(filter(None, (_date(_text(p, "PlannedPeriod/StartDate")) for p in projects)))
    ends = sorted(filter(None, (_date(_text(p, "PlannedPeriod/EndDate")) for p in projects)))
    execution_period = (
        f"{_german_date(starts[0]) if starts else '…'} - {_german_date(ends[-1]) if ends else '…'}"
        if starts or ends else None
    )

    # Auftragnehmer: Angebote, zu denen ein Vertrag geschlossen wurde (sonst alle genannten Angebote)
    result = _first(root, f"{_EXT}/NoticeResult")
    won_tenders = set(_texts(result, "SettledContract/LotTender/ID"))
    winners: list[str] = []
    for lot_tender in _all(result, "LotTender"):
        if won_tenders and _text(lot_tender, "ID") not in won_tenders:
            continue
        for name in tendering_parties.get(_text(lot_tender, "TenderingParty/ID"), []):
            if name not in winners:
                winners.append(name)

    documents = []
    for uri in _texts(root, "ProcurementProjectLot/TenderingTerms/CallForTendersDocumentReference/Attachment/ExternalReference/URI"):
        if uri not in (d["url"] for d in documents):
            documents.append({"title": "Vergabeunterlagen", "url": uri})

    issue = _datetime(_text(root, "IssueDate") or _text(root, "RequestedPublicationDate"), _text(root, "IssueTime"))

    fields = {"Bekanntmachungsart": _NOTICE_TYPE_LABELS[category]}
    for label, value in (
        ("Vergabeordnung", _text(root, "TenderingTerms/ProcurementLegislationDocumentReference/ID")),
        ("Auftragsart", _CONTRACT_NATURE.get(_text(project, "ProcurementTypeCode") or "")),
        ("Geschäftszeichen", _text(project, "ID")),
        ("Geschätzter Auftragswert", _amount(_first(project, "RequestedTenderTotal/EstimatedOverallContractAmount"))),
        ("Gesamtwert der Zuschläge", _amount(_first(root, f"{_EXT}/NoticeResult/TotalAmount"))),
        ("Sitz Vergabestelle", buyer.get("city")),
        ("Leistungsort", "; ".join(cities) or None),
        ("Anzahl Lose", str(len(lots)) if len(lots) > 1 else None),
        ("Bekanntmachungs-ID", notice_id),
        ("Version", _text(root, "VersionID")),
    ):
        if value:
            fields[label] = value

    procedure_code = _text(root, "TenderingProcess/ProcedureCode")
    if category == "bekanntmachung" and (procedure_code or "").lower() == _INTENDED_RESTRICTED:
        category = "beschraenkt"
        fields["Bekanntmachungsart"] = "Beabsichtigte Beschränkte Ausschreibung"
    return {
        "source": SOURCE_OV,
        "uid": f"ov:{procedure_id}:{category}",
        "category": category,
        "region": region,
        "title": re.sub(r"\s+", " ", title).strip(),
        "url": NOTICE_URL.format(notice_id=notice_id),
        "published_at": issue,
        "online_since": _date(issue),
        "procedure": _PROCEDURES.get(procedure_code or "", procedure_code),
        "location": "; ".join(cities[:3]) or None,
        "deadline": deadline,
        "deadline_label": deadline_label,
        "authority": authority,
        "contractor": "; ".join(winners) or None,
        "subject": None,
        "service_type": None,
        "execution_period": execution_period,
        "description": "\n".join(parts) or None,
        "cpv_codes": cpv_codes,
        "documents": documents,
        "feed_fields": {},
        "detail_fields": fields,
    }


def _amount(el: ET.Element | None) -> str | None:
    if el is None or not (el.text or "").strip():
        return None
    try:
        value = float(el.text)
    except ValueError:
        return None
    if value <= 0:
        return None
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{formatted} {el.attrib.get('currencyID', 'EUR')}"


def parse_export(content: bytes) -> tuple[list[dict], int]:
    """Liest einen Tagesexport. Liefert (Einträge, Anzahl übersprungener Bekanntmachungen).
    Einzelne Dateien über MAX_ENTRY_BYTES werden übersprungen; ist das Archiv insgesamt größer
    als MAX_TOTAL_BYTES, wird abgebrochen."""
    items, skipped, total = [], 0, 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".xml"):
                continue
            with archive.open(name) as member:
                # eins mehr als erlaubt lesen: so fällt auch eine falsch angegebene Größe auf
                data = member.read(MAX_ENTRY_BYTES + 1)
            if len(data) > MAX_ENTRY_BYTES:
                skipped += 1
                continue
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ValueError(f"Export ist unerwartet groß (über {MAX_TOTAL_BYTES // 1024 // 1024} MB) – Abbruch")
            try:
                item = parse_notice(data)
            except (ET.ParseError, ValueError):
                item = None
            if item is None:
                skipped += 1
            else:
                items.append(item)
    return items, skipped


def export_url(day: date) -> str:
    return EXPORT_URL.format(day=day.isoformat())
