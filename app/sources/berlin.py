"""RSS-Feeds der Vergabeplattform des Landes Berlin."""

import html
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from . import parse_xml

BASE_URL = "https://www.berlin.de/vergabeplattform/veroeffentlichungen"


@dataclass(frozen=True)
class Feed:
    key: str
    label: str
    url: str


FEEDS = [
    Feed("bekanntmachung", "Bekanntmachungen", f"{BASE_URL}/bekanntmachungen/feed.rss"),
    Feed("beschraenkt", "Beabsichtigte Beschränkte Ausschreibungen", f"{BASE_URL}/info-19/feed.rss"),
    Feed("vergeben", "Vergebene Aufträge", f"{BASE_URL}/info-19-20/feed.rss"),
    Feed("vorinformation", "Vorinformationen / Markterkundungen", f"{BASE_URL}/vorinformationen/feed.rss"),
]


_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
_KEY_VALUE = re.compile(r"^([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß /().-]{1,50}):\s*(.*)$", re.S)
_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:,?\s*(\d{1,2}):(\d{2}))?")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", value))).strip()


def parse_german_datetime(value: str | None) -> str | None:
    """'14.10.2026, 11:00 Uhr' -> '2026-10-14T11:00'; '11.09.2026' -> '2026-09-11'."""
    if not value:
        return None
    m = _DATE.search(value)
    if not m:
        return None
    day, month, year, hour, minute = m.groups()
    date = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    if hour is None:
        return date
    return f"{date}T{int(hour):02d}:{minute}"


def parse_description(description: str) -> dict[str, str]:
    """Zerlegt 'Schlüssel: Wert<br>…' in ein Dictionary."""
    fields: dict[str, str] = {}
    last_key = None
    for part in _BR.split(description or ""):
        text = clean_text(part)
        if not text:
            continue
        m = _KEY_VALUE.match(text)
        if m:
            last_key = m.group(1).strip()
            fields[last_key] = m.group(2).strip()
        elif last_key:
            fields[last_key] = f"{fields[last_key]} {text}".strip()
    return fields


def _normalize(category: str, title: str, link: str, guid: str, pub_date: str, fields: dict) -> dict:
    deadline_label, deadline_raw = None, None
    for key, value in fields.items():
        if key.startswith("Ablauf") and "frist" in key.lower():
            deadline_label, deadline_raw = key, value
            break

    published_at = None
    if pub_date:
        try:
            published_at = parsedate_to_datetime(pub_date).replace(tzinfo=None).isoformat(timespec="minutes")
        except (TypeError, ValueError):
            published_at = None

    return {
        "uid": guid or link,
        "category": category,
        "title": title,
        "url": link,
        "published_at": published_at,
        "online_since": parse_german_datetime(fields.get("Online seit")),
        "procedure": fields.get("Verfahrensart"),
        "location": fields.get("Ausführungsort"),
        "deadline": parse_german_datetime(deadline_raw),
        "deadline_label": deadline_label,
        "authority": fields.get("Vergabestelle"),
        "contractor": fields.get("Beauftragtes Unternehmen"),
        "subject": fields.get("Auftragsgegenstand"),
        "service_type": fields.get("Art der Leistung"),
        "execution_period": fields.get("Voraussichtliche Ausführungsfrist"),
        "feed_fields": fields,
    }


def parse_feed(content: bytes | str, category: str) -> list[dict]:
    root = parse_xml(content)
    items = []
    for item in root.iter("item"):
        title = clean_text(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        if not (guid or link):
            continue
        fields = parse_description(item.findtext("description") or "")
        items.append(_normalize(category, title, link, guid, (item.findtext("pubDate") or "").strip(), fields))
    return items
