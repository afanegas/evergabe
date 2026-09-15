"""Erkennung von Dubletten zwischen Vergabeplattform Berlin und oeffentlichevergabe.de.

Zusammengeführt wird nur, wenn es eindeutig ist:
- gleiche Region (Berlin) und gleiche Kategorie-Familie,
- gleicher Titel (ohne Groß-/Kleinschreibung, Leer- und Satzzeichen),
- Vergabestellen ähnlich (gemeinsame markante Wörter). Fehlt eine Vergabestelle, muss der Titel
  lang genug sein, um nicht zufällig übereinzustimmen (z. B. „Malerarbeiten“),
- Veröffentlichung höchstens 30 Tage auseinander,
- der Kandidat hat noch keinen Eintrag aus derselben Quelle.
"""

import re
from datetime import datetime, timedelta

FAMILIES = {
    "bekanntmachung": "ausschreibung",
    "vorinformation": "ausschreibung",
    "beschraenkt": "beabsichtigt",
    "direktvergabe": "beabsichtigt",
    "vergeben": "vergeben",
    "vertragsaenderung": "vergeben",
}

MAX_DAYS_APART = 30
MIN_TITLE_LENGTH_WITHOUT_AUTHORITY = 25

_STOPWORDS = {
    "berlin", "land", "landes", "von", "der", "die", "das", "des", "und", "fur", "für", "gmbh", "mbh", "ag",
    "aör", "aor", "vertreten", "durch", "bezirksamt", "senatsverwaltung", "zentrale", "vergabestelle",
}


def normalize_title(title: str | None) -> str:
    return re.sub(r"[\W_]+", "", (title or "").lower())


def authority_tokens(name: str | None) -> set[str]:
    words = re.findall(r"[a-zäöüß0-9]+", (name or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def authorities_match(a: str | None, b: str | None) -> bool | None:
    """True/False bei zwei Namen, None wenn einer fehlt."""
    ta, tb = authority_tokens(a), authority_tokens(b)
    if not ta or not tb:
        return None
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.5


def _days_apart(a: str | None, b: str | None) -> float:
    try:
        return abs((datetime.fromisoformat(a[:10]) - datetime.fromisoformat(b[:10])) / timedelta(days=1))
    except (TypeError, ValueError):
        return 0.0


def find_duplicate(conn, item: dict, source: str) -> int | None:
    if item.get("region") != "berlin":
        return None
    title = normalize_title(item.get("title"))
    family = FAMILIES.get(item["category"])
    if not title or not family:
        return None
    categories = [c for c, f in FAMILIES.items() if f == family]

    rows = conn.execute(
        f"""
        SELECT id, title, authority, COALESCE(published_at, first_seen) AS published
        FROM tenders
        WHERE region = 'berlin'
          AND category IN ({', '.join('?' * len(categories))})
          AND NOT EXISTS (SELECT 1 FROM tender_sources s WHERE s.tender_id = tenders.id AND s.source = ?)
        """,
        [*categories, source],
    ).fetchall()

    item_published = item.get("published_at") or item.get("online_since")
    for row in rows:
        if normalize_title(row["title"]) != title:
            continue
        if _days_apart(row["published"], item_published) > MAX_DAYS_APART:
            continue
        same_authority = authorities_match(row["authority"], item.get("authority"))
        if same_authority is False:
            continue
        if same_authority is None and len(title) < MIN_TITLE_LENGTH_WITHOUT_AUTHORITY:
            continue
        return row["id"]
    return None
