"""Tägliche Datensicherung der SQLite-Datenbank.

Die Sicherung nutzt die Backup-Funktion von SQLite – die Kopie ist konsistent, auch wenn die App gerade schreibt.
Es werden die letzten BACKUP_KEEP Sicherungen behalten.
"""

import logging
import sqlite3
from datetime import datetime

from . import config
from .db import now

log = logging.getLogger(__name__)

PREFIX = "ev-checker-"


def create_backup() -> dict:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(now()).strftime("%Y%m%d-%H%M%S")
    target = config.BACKUP_DIR / f"{PREFIX}{stamp}.db"
    source = sqlite3.connect(config.DB_PATH)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    removed = prune_backups()
    log.info("Datensicherung erstellt: %s (%d alte entfernt)", target.name, removed)
    return {"name": target.name, "size": target.stat().st_size, "removed": removed}


def list_backups() -> list[dict]:
    if not config.BACKUP_DIR.exists():
        return []
    files = sorted(config.BACKUP_DIR.glob(f"{PREFIX}*.db"), reverse=True)
    return [
        {
            "name": f.name,
            "size": f.stat().st_size,
            "created": datetime.strptime(f.stem[len(PREFIX):], "%Y%m%d-%H%M%S").isoformat(timespec="seconds"),
        }
        for f in files
    ]


def prune_backups() -> int:
    files = sorted(config.BACKUP_DIR.glob(f"{PREFIX}*.db"), reverse=True)
    old = files[max(config.BACKUP_KEEP, 1):]
    for f in old:
        f.unlink(missing_ok=True)
    return len(old)
