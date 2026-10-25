"""Offizielle deutsche CPV-Bezeichnungen.

Quelle: Gemeinsames Vokabular für öffentliche Aufträge (CPV 2008, Verordnung (EG) Nr. 213/2008),
veröffentlicht von der EU unter https://ted.europa.eu/en/simap/cpv (Datei cpv_2008_xml).
Aus der mehrsprachigen XML-Datei sind nur die deutschen Bezeichnungen übernommen und gepackt
abgelegt (9454 Codes, rund 100 KB). Der Katalog ändert sich nur, wenn die EU ihn überarbeitet –
deshalb liegt er als Datei im Projekt und wird nicht zur Laufzeit geladen.
"""

import gzip
import json
import logging
import re
from functools import lru_cache

from . import config

log = logging.getLogger(__name__)

CATALOG_PATH = config.BASE_DIR / "cpv_de.json.gz"


@lru_cache(maxsize=1)
def titles() -> dict[str, str]:
    """Code (8 Ziffern, ohne Prüfziffer) -> deutsche Bezeichnung."""
    try:
        with gzip.open(CATALOG_PATH, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        log.exception("CPV-Katalog konnte nicht gelesen werden: %s", CATALOG_PATH)
        return {}


def title(code: str) -> str:
    """Bezeichnung zu einem CPV-Code – leer, wenn der Code nicht im Katalog steht."""
    return titles().get(re.sub(r"\D", "", code or "")[:8], "")


def search(query: str, limit: int = 20) -> list[dict[str, str]]:
    """Codes und Bezeichnungen zur Eingabe suchen – nach Ziffern am Anfang des Codes oder nach Wörtern
    in der Bezeichnung. Treffer am Wortanfang stehen vorn, danach wird nach Code sortiert."""
    begriff = " ".join((query or "").split()).lower()
    if len(begriff) < 2:
        return []
    ziffern = re.sub(r"\D", "", begriff)
    katalog = titles()

    if ziffern and ziffern == begriff.replace("-", "").strip():
        treffer = [(0, code) for code in katalog if code.startswith(ziffern)]
    else:
        treffer = []
        for code, text in katalog.items():
            stelle = text.lower().find(begriff)
            if stelle < 0:
                continue
            # Wortanfang zuerst, dann Vorkommen mitten im Wort
            am_wortanfang = stelle == 0 or not text[stelle - 1].isalnum()
            treffer.append((0 if am_wortanfang else 1, code))

    treffer.sort()
    return [{"code": code, "title": katalog[code]} for _, code in treffer[:limit]]
