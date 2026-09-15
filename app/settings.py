"""Einstellungen und Regeln (in der Datenbank, über die Einstellungsseite änderbar)."""

import json
import re

from . import config
from .classify import Rules
from .db import connect

DEFAULTS = {
    "cpv_enabled": True,
    "keyword_enabled": True,
    "combine": "oder",
    "schedule_mode": "zeiten",  # zeiten | intervall
    "schedule_times": ["07:00", "13:00"],
    "schedule_interval_hours": 12,
    "feeds_disabled": [],  # Schlüssel abgeschalteter Feeds (neue Feeds sind automatisch aktiv)
    "feed_status": {},  # je Feed: Zeitpunkt, Einträge im Feed, davon neu, Fehler des letzten Abrufs
    "ov_last_day": None,  # zuletzt vollständig geladener Tag von oeffentlichevergabe.de (JJJJ-MM-TT)
    "lowprio_scope": "titel",  # veraltet: früherer gemeinsamer Bereich der Niedrig-Stichwörter (nur für Migration)
    "priority_conflict": "wichtig",  # wichtig + niedrig zugleich: wichtig | niedrig | beide
    "mail_enabled": False,  # tägliche E-Mail mit neuen interessanten Treffern
    "mail_to": "",  # Empfänger, durch Komma getrennt
    "mail_time": "07:30",
    "mail_last_sent_at": None,  # Zeitpunkt der letzten verschickten Mail (neue Treffer seitdem)
    "mail_last_check": None,  # Ergebnis des letzten Versandversuchs
}

START_CPV = [
    ("71314000", "Energie und zugehörige Dienstleistungen"),
    ("71314200", "Energiemanagement"),
    ("71314300", "Energieeffizienzberatung"),
    ("90712000", "Umweltplanung"),
    ("90713000", "Umweltberatung"),
    ("09330000", "Solarenergie"),
    ("09323000", "Fernwärme"),
    ("71321000", "Technische Planung für Gebäudeanlagen"),
    ("71321200", "Heizungsplanung"),
    ("71313000", "Beratung im Bereich Umwelttechnik"),
    ("45261215", "Deckung von Dächern mit Solarzellen"),
    ("45331000", "Installation von Heizungs-, Lüftungs- und Klimaanlagen"),
    ("71241000", "Durchführbarkeitsstudien"),
    ("71335000", "Technische Studien"),
]

START_KEYWORDS = [
    "Energieberatung", "Energieaudit", "Energiemanagement", "Energieeffizienz",
    "Klimaschutz", "Contracting", "Photovoltaik", "PV", "Solar", "BHKW", "KWK", "Wärmepumpe", "Mieterstrom",
    "Dekarbonisierung", "CO2-Bilanz", "klimaneutral", "Ladeinfrastruktur",
]

START_PRIORITY = [
    "Energie*spar*contracting",  # Energiespar-/Energieeinspar-Contracting in allen Schreibweisen
    "Wärmeplanung", "Wärmenetz", "Sanierungsfahrplan", "iSFP", "Energiekonzept", "Quartierskonzept",
]

START_LOWPRIO = ["Planungsleistungen", "HOAI"]

START_EXCLUSIONS = [
    "Malerarbeiten", "Tischler", "Gerüst", "Pflaster", "Reinigung", "Schädlingsbekämpfung",
    "Garten- und Landschaftsbau", "Winterdienst", "Kurier", "Bewachung", "Catering",
]

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_CPV_CODE = re.compile(r"^\s*(\d{8})(?:-\d)?\s*$")


def get_all() -> dict:
    values = dict(DEFAULTS)
    with connect() as conn:
        for row in conn.execute("SELECT key, value FROM settings"):
            values[row["key"]] = json.loads(row["value"])
    return values


def get(key: str):
    return get_all().get(key)


def set_many(values: dict) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items()],
        )


def seed_rules() -> None:
    """Start-Regeln nur beim allerersten Start laden (danach gelten die Änderungen des Nutzers)."""
    if get("rules_seeded"):
        return
    with connect() as conn:
        conn.executemany("INSERT OR IGNORE INTO rules(kind, value, label) VALUES('cpv', ?, ?)", START_CPV)
        conn.executemany("INSERT OR IGNORE INTO rules(kind, value) VALUES('keyword', ?)", [(k,) for k in START_KEYWORDS])
        conn.executemany("INSERT OR IGNORE INTO rules(kind, value) VALUES('exclusion', ?)", [(k,) for k in START_EXCLUSIONS])
    set_many({"rules_seeded": True})


def seed_priority_rules() -> bool:
    """Wichtige Stichwörter einmalig anlegen (auch in bestehenden Datenbanken). Gleichlautende normale
    Stichwörter werden dabei in die neue Liste verschoben. Gibt True zurück, wenn etwas geändert wurde."""
    if get("priority_seeded"):
        return False
    with connect() as conn:
        conn.executemany("INSERT OR IGNORE INTO rules(kind, value) VALUES('priority', ?)", [(k,) for k in START_PRIORITY])
        conn.executemany(
            "DELETE FROM rules WHERE kind = 'keyword' AND lower(value) = lower(?)", [(k,) for k in START_PRIORITY]
        )
    set_many({"priority_seeded": True})
    return True


RULE_KINDS = ("cpv", "keyword", "priority", "lowprio", "exclusion")
_RULE_FIELD = re.compile(r"^rule-(cpv|keyword|priority|lowprio|exclusion)-(\d+)-value$")


def seed_lowprio_rules() -> bool:
    """Niedrig-Stichwörter einmalig anlegen (auch in bestehenden Datenbanken)."""
    if get("lowprio_seeded"):
        return False
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO rules(kind, value, scope) VALUES('lowprio', ?, 'titel')", [(k,) for k in START_LOWPRIO]
        )
    set_many({"lowprio_seeded": True})
    return True


def migrate_rule_scopes() -> bool:
    """Einmalig: Früher galt ein gemeinsamer Bereich für alle Niedrig-Stichwörter (Einstellung lowprio_scope).
    Jetzt hat jedes Stichwort einen eigenen Bereich – der alte Wert wird auf die Niedrig-Stichwörter übertragen."""
    if get("rule_scopes_migrated"):
        return False
    scope = "alles" if get("lowprio_scope") == "alles" else "titel"
    with connect() as conn:
        conn.execute("UPDATE rules SET scope = ? WHERE kind = 'lowprio'", (scope,))
    set_many({"rule_scopes_migrated": True})
    return True


# Nachträglich ergänzte Regeln: werden in bestehenden Datenbanken einmalig hinzugefügt (je Kennung nur einmal,
# damit ein später gelöschter Eintrag nicht wiederkommt). Neue Installationen bekommen sie ebenfalls.
RULE_ADDITIONS = [
    ("add-energiemanagementsystem", [("keyword", "Energiemanagementsystem"), ("priority", "Energiemanagementsystem")]),
]


def apply_rule_additions() -> bool:
    done = set(get("rule_additions_done") or [])
    changed = False
    for key, entries in RULE_ADDITIONS:
        if key in done:
            continue
        with connect() as conn:
            for kind, value in entries:
                exists = conn.execute(
                    "SELECT 1 FROM rules WHERE kind = ? AND lower(value) = lower(?)", (kind, value)
                ).fetchone()
                if not exists:
                    conn.execute("INSERT INTO rules(kind, value) VALUES(?, ?)", (kind, value))
                    changed = True
        done.add(key)
    set_many({"rule_additions_done": sorted(done)})
    return changed


def list_rules(kind: str, only_active: bool = False) -> list[dict]:
    query = "SELECT value, label, active, scope FROM rules WHERE kind = ?" + (" AND active = 1" if only_active else "")
    with connect() as conn:
        return [dict(r) for r in conn.execute(query + " ORDER BY id", (kind,))]


def all_rules() -> dict[str, list[dict]]:
    return {kind: list_rules(kind) for kind in RULE_KINDS}


def load_rules() -> Rules:
    """Nur aktive Regeln wirken auf die Klassifizierung."""
    values = get_all()
    return Rules(
        cpv_codes=[r["value"] for r in list_rules("cpv", only_active=True)],
        keywords=[(r["value"], r["scope"]) for r in list_rules("keyword", only_active=True)],
        exclusions=[(r["value"], r["scope"]) for r in list_rules("exclusion", only_active=True)],
        priority_keywords=[(r["value"], r["scope"]) for r in list_rules("priority", only_active=True)],
        lowprio_keywords=[(r["value"], r["scope"]) for r in list_rules("lowprio", only_active=True)],
        lowprio_scope=values["lowprio_scope"],
        priority_conflict=values["priority_conflict"],
        cpv_enabled=bool(values["cpv_enabled"]),
        keyword_enabled=bool(values["keyword_enabled"]),
        combine=values["combine"],
    )


def default_scope(kind: str) -> str:
    """Bereich für neue Stichwörter: Niedrig-Stichwörter nur im Titel, alle anderen in Titel und Beschreibung."""
    return "titel" if kind == "lowprio" else "alles"


def normalize_cpv(value: str) -> str | None:
    """'71314000', '71314000-2' -> '71314000'; alles andere ungültig."""
    m = _CPV_CODE.match(value or "")
    return m.group(1) if m else None


def parse_rules_form(form) -> tuple[dict[str, list[dict]], list[str]]:
    """Regel-Listen aus dem Einstellungsformular lesen.

    Jede Zeile hat die Felder rule-<art>-<nr>-value, -label (nur CPV), -scope (nur Stichwörter) und -active.
    Gelöschte Zeilen fehlen im Formular. Doppelte Einträge (ohne Groß-/Kleinschreibung) werden zusammengefasst.
    """
    rows: dict[str, list[tuple[int, dict]]] = {kind: [] for kind in RULE_KINDS}
    errors: list[str] = []
    for key in form.keys():
        m = _RULE_FIELD.match(key)
        if not m:
            continue
        kind, index = m.group(1), int(m.group(2))
        prefix = f"rule-{kind}-{index}"
        value = (form.get(key) or "").strip()
        label = (form.get(f"{prefix}-label") or "").strip()
        scope = form.get(f"{prefix}-scope")
        if kind == "cpv":
            scope = "alles"
        elif scope not in ("alles", "titel"):
            scope = default_scope(kind)
        entry = {"value": value, "label": label if kind == "cpv" else None, "active": bool(form.get(f"{prefix}-active")),
                 "scope": scope}
        if not value:
            continue
        if kind != "cpv":
            value = " + ".join(part.strip() for part in value.split("+") if part.strip())  # „A+B“ -> „A + B“
            entry["value"] = value
            if not any(ch.isalnum() for ch in value):
                errors.append(f"Ungültiges Stichwort „{value}“ (enthält keine Buchstaben)")
                continue
        if kind == "cpv":
            code = normalize_cpv(value)
            if code is None:
                errors.append(f"Ungültiger CPV-Code „{value}“ (erwartet: 8 Ziffern, z. B. 71314000)")
            else:
                entry["value"] = code
        rows[kind].append((index, entry))

    rules: dict[str, list[dict]] = {}
    for kind, entries in rows.items():
        seen, unique = set(), []
        for _, entry in sorted(entries, key=lambda pair: pair[0]):
            if entry["value"].lower() not in seen:
                seen.add(entry["value"].lower())
                unique.append(entry)
        rules[kind] = unique
    return rules, errors


def replace_rules(rules: dict[str, list[dict]]) -> None:
    """Die übergebenen Regel-Arten vollständig ersetzen (andere Arten bleiben unverändert)."""
    with connect() as conn:
        for kind, entries in rules.items():
            conn.execute("DELETE FROM rules WHERE kind = ?", (kind,))
            conn.executemany(
                "INSERT INTO rules(kind, value, label, active, scope) VALUES(?, ?, ?, ?, ?)",
                [
                    (kind, e["value"], e.get("label") or None, int(e.get("active", True)),
                     e.get("scope") or default_scope(kind))
                    for e in entries
                ],
            )


def parse_times(text: str) -> tuple[list[str], list[str]]:
    times, errors = set(), []
    for part in re.split(r"[,;\s]+", text.strip()):
        if not part:
            continue
        m = _TIME.match(part)
        if m:
            times.add(f"{int(m.group(1)):02d}:{m.group(2)}")
        else:
            errors.append(part)
    return sorted(times), errors


def max_gap_hours(values: dict) -> float:
    """Größter Abstand zwischen zwei automatischen Abrufen."""
    if values["schedule_mode"] == "intervall":
        return float(values["schedule_interval_hours"])
    minutes = sorted(int(t[:2]) * 60 + int(t[3:]) for t in values["schedule_times"])
    if not minutes:
        return float("inf")
    gaps = [b - a for a, b in zip(minutes, minutes[1:])] + [minutes[0] + 24 * 60 - minutes[-1]]
    return max(gaps) / 60


def gap_warning(values: dict) -> str | None:
    gap = max_gap_hours(values)
    if gap == float("inf"):
        return "Kein automatischer Abruf eingestellt – neue Einträge kommen nur über „Jetzt abrufen“."
    if gap > config.MAX_SAFE_GAP_HOURS:
        return (
            f"Abstand von {gap:.0f} Stunden zwischen zwei Abrufen. Die Feeds enthalten nur die letzten "
            f"50 Einträge (bei Bekanntmachungen ca. 5 Tage) – dabei können Einträge verloren gehen."
        )
    return None
