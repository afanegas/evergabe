import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    published_at TEXT,
    online_since TEXT,
    procedure TEXT,
    location TEXT,
    deadline TEXT,
    deadline_label TEXT,
    authority TEXT,
    contractor TEXT,
    subject TEXT,
    service_type TEXT,
    execution_period TEXT,
    description TEXT,
    cpv_codes TEXT NOT NULL DEFAULT '[]',
    documents TEXT NOT NULL DEFAULT '[]',
    feed_fields TEXT NOT NULL DEFAULT '{}',
    detail_fields TEXT NOT NULL DEFAULT '{}',
    detail_status TEXT NOT NULL DEFAULT 'offen',
    detail_attempts INTEGER NOT NULL DEFAULT 0,
    detail_error TEXT,
    detail_fetched_at TEXT,
    auto_status TEXT NOT NULL DEFAULT 'nicht_interessant',
    cpv_result TEXT,
    cpv_matches TEXT NOT NULL DEFAULT '[]',
    keyword_result TEXT,
    keyword_matches TEXT NOT NULL DEFAULT '[]',
    exclusion_matches TEXT NOT NULL DEFAULT '[]',
    priority_matches TEXT NOT NULL DEFAULT '[]',
    important INTEGER NOT NULL DEFAULT 0,   -- als wichtig markiert
    lowprio_matches TEXT NOT NULL DEFAULT '[]',
    low_priority INTEGER NOT NULL DEFAULT 0,   -- als niedrige Priorität markiert
    priority_level INTEGER NOT NULL DEFAULT 0,   -- Sortierung in der Gruppe: 2 wichtig, 1 beides, 0 normal, -1 niedrig
    manual_status TEXT,
    note TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    seen_at TEXT,
    source TEXT NOT NULL DEFAULT 'berlin',   -- Quelle, aus der der Eintrag zuerst kam
    region TEXT NOT NULL DEFAULT 'berlin'    -- berlin | brandenburg | rest
);
CREATE INDEX IF NOT EXISTS idx_tenders_category ON tenders(category);
CREATE INDEX IF NOT EXISTS idx_tenders_published ON tenders(published_at);

-- Ein Eintrag kann aus mehreren Quellen stammen (zusammengeführte Dubletten)
CREATE TABLE IF NOT EXISTS tender_sources (
    id INTEGER PRIMARY KEY,
    tender_id INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    uid TEXT NOT NULL UNIQUE,
    url TEXT,
    first_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_tender ON tender_sources(tender_id);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,          -- cpv | keyword | priority | lowprio | exclusion
    value TEXT NOT NULL,
    label TEXT,
    active INTEGER NOT NULL DEFAULT 1,   -- abgeschaltete Regeln bleiben gespeichert, wirken aber nicht
    scope TEXT NOT NULL DEFAULT 'alles', -- Stichwörter: alles (Titel + Beschreibung) | titel
    UNIQUE(kind, value)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    new_items INTEGER NOT NULL DEFAULT 0,
    updated_items INTEGER NOT NULL DEFAULT 0,
    details_fetched INTEGER NOT NULL DEFAULT 0,
    errors TEXT NOT NULL DEFAULT '[]'
);
"""


def now() -> str:
    """Lokale Berliner Zeit ohne Zeitzone – so liefern auch die Feeds ihre Zeiten."""
    return datetime.now(ZoneInfo(config.TIMEZONE)).replace(tzinfo=None).isoformat(timespec="seconds")


@contextmanager
def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        _migrate_columns(conn)
        conn.executescript(SCHEMA)
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_tenders_region_category ON tenders(region, category);
            -- Einträge aus der Zeit vor der Quellen-Tabelle stammen alle aus dem Berlin-Feed
            INSERT OR IGNORE INTO tender_sources(tender_id, source, uid, url, first_seen)
                SELECT id, 'berlin', uid, url, first_seen FROM tenders
                WHERE id NOT IN (SELECT tender_id FROM tender_sources);
            """
        )


def _migrate_columns(conn) -> None:
    """Spalten nachrüsten, die in älteren Datenbanken fehlen."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rules'").fetchone():
        rule_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rules)")}
        if "active" not in rule_columns:
            conn.execute("ALTER TABLE rules ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if "scope" not in rule_columns:
            conn.execute("ALTER TABLE rules ADD COLUMN scope TEXT NOT NULL DEFAULT 'alles'")
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tenders'").fetchone()
    if not exists:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tenders)")}
    for name, definition in (
        ("source", "TEXT NOT NULL DEFAULT 'berlin'"),
        ("region", "TEXT NOT NULL DEFAULT 'berlin'"),
        ("priority_matches", "TEXT NOT NULL DEFAULT '[]'"),
        ("important", "INTEGER NOT NULL DEFAULT 0"),
        ("lowprio_matches", "TEXT NOT NULL DEFAULT '[]'"),
        ("low_priority", "INTEGER NOT NULL DEFAULT 0"),
        ("priority_level", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE tenders ADD COLUMN {name} {definition}")


def row_to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in ("cpv_codes", "documents", "cpv_matches", "keyword_matches", "exclusion_matches", "priority_matches",
                "lowprio_matches"):
        if key in item:
            item[key] = json.loads(item[key] or "[]")
    for key in ("feed_fields", "detail_fields"):
        if key in item:
            item[key] = json.loads(item[key] or "{}")
    if "manual_status" in item:
        item["status"] = item["manual_status"] or item["auto_status"]
    return item
