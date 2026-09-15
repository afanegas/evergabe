"""Detailseiten: CPV-Codes, Kurzbeschreibung und Vergabestelle nachladen.

- meinauftrag.rib.de (iTWO tender): Bekanntmachungen und beabsichtigte Beschränkte Ausschreibungen
- berlin.de: Vergebene Aufträge und Vorinformationen (ohne CPV-Codes)
"""

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .berlin import clean_text, parse_german_datetime

_CPV = re.compile(r"^(\d{8}-\d)\s*(.*)$")
_BR = re.compile(r"<br\s*/?>|</?(?:p|div|li)[^>]*>", re.I)

# RIB liefert je nach Sprache deutsche oder englische Überschriften
_RIB_SECTIONS = {
    "kurzbeschreibung der leistung": "description",
    "short description": "description",
    "termine und fristen": "dates",
    "dates and deadlines": "dates",
    "vergabestelle": "authority",
    "contracting authority": "authority",
    "vergabe": "award",
    "awarded": "award",
    "cpv codes": "cpv",
}


def detail_kind(url: str | None) -> str | None:
    host = urlparse(url or "").netloc.lower()
    if host == "meinauftrag.rib.de":
        return "rib"
    if host.endswith("berlin.de") and "/vergabeplattform/veroeffentlichungen/" in (url or ""):
        return "berlin"
    return None


def _lines(node: Tag, skip: Tag | None = None) -> list[str]:
    """Text eines Blocks zeilenweise (Zeilen = <br>), ohne die Überschrift."""
    markup = str(node)
    if skip is not None:
        markup = markup.replace(str(skip), "", 1)
    return [line for line in (clean_text(part) for part in _BR.split(markup)) if line]


def _list_pairs(node: Tag) -> dict[str, str]:
    pairs = {}
    for li in node.select("li"):
        divs = li.find_all("div", recursive=False)
        if len(divs) >= 2:
            value = divs[1]
            for small in value.find_all("small"):
                small.extract()
            pairs[clean_text(divs[0].get_text())] = clean_text(value.get_text(" "))
    return pairs


def parse_rib(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(".tender-details") or soup
    result: dict = {"cpv_codes": [], "fields": {}}

    for h6 in root.find_all("h6"):
        heading = clean_text(h6.get_text())
        if heading.lower().startswith(("maßnahme:", "action:")):
            measure = heading.split(":", 1)[1].strip()
            if measure and measure != "- ohne -":
                result["fields"]["Maßnahme"] = measure
            continue
        section = _RIB_SECTIONS.get(heading.lower())
        container = h6.find_parent("div")
        if not section or container is None:
            continue

        if section == "cpv":
            for span in container.find_all("span"):
                m = _CPV.match(clean_text(span.get_text()))
                if m:
                    result["cpv_codes"].append({"code": m.group(1), "label": m.group(2)})
        elif section == "description":
            lines = _lines(container, skip=h6)
            if lines:
                result["description"] = "\n".join(lines)
        elif section == "authority":
            lines = _lines(container, skip=h6)
            if lines:
                result["authority"] = lines[0]
                result["fields"]["Vergabestelle (Anschrift)"] = ", ".join(lines)
        elif section in ("dates", "award"):
            pairs = _list_pairs(container)
            result["fields"].update(pairs)
            for key, value in pairs.items():
                low = key.lower()
                if low in ("vergabeverfahren", "tender procedures"):
                    result["procedure"] = value
                elif "angebotsfrist" in low or "teilnahmefrist" in low:
                    result.setdefault("deadline", parse_german_datetime(value))
                    result.setdefault("deadline_label", key)

    return result


def parse_berlin(html: str, base_url: str = "https://www.berlin.de") -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result: dict = {"cpv_codes": [], "fields": {}, "documents": []}
    dl = soup.select_one("dl.list--horizontal")
    if dl is None:
        return result

    for dt in dl.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue
        key = clean_text(dt.get_text())
        links = dd.find_all("a", href=True)
        if links and not key.lower().startswith("auftraggeber"):
            for a in links:
                result["documents"].append({"title": clean_text(a.get_text()), "url": urljoin(base_url, a["href"])})
            continue
        if key.lower().startswith("auftraggeber"):
            first = dd.find("li")
            value = clean_text((first or dd).get_text(" "))
            result["authority"] = value
        else:
            value = clean_text(dd.get_text(" "))
        result["fields"][key] = value

        low = key.lower()
        if low == "vergabestelle":
            result["authority"] = value
        elif low == "vergabeverfahren":
            result["procedure"] = value
        elif low in ("ort der ausführung", "ort der maßnahme"):
            result["location"] = value
        elif low == "beauftragtes unternehmen":
            result["contractor"] = value
        elif low == "art der leistung":
            result["service_type"] = value
        elif low == "auftragsgegenstand":
            result["subject"] = value
        elif low in ("voraussichtliche ausführungsfrist", "zeitraum der ausführung"):
            result["execution_period"] = value
        elif low == "maßnahme":
            result["description"] = value

    return result


def parse_detail(url: str, html: str) -> dict | None:
    kind = detail_kind(url)
    if kind == "rib":
        return parse_rib(html)
    if kind == "berlin":
        return parse_berlin(html)
    return None
