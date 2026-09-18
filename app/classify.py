"""Klassifizierung interessant / nicht interessant aus zwei getrennten Prüfungen.

CPV-Prüfung:      Treffer, wenn ein CPV-Code des Eintrags unter einen Code der CPV-Liste fällt.
                  Die CPV-Hierarchie steckt in den Stellen: abschließende Nullen sind Platzhalter,
                  daher deckt 71314000 auch 71314300 ab.
Stichwort-Prüfung: Treffer, wenn ein Stichwort im Text vorkommt und kein Ausschluss-Stichwort.
Wichtige Stichwörter: zählen wie normale Stichwörter. Trifft eines (und die Stichwort-Prüfung ist nicht
                  ausgeschlossen), wird der Eintrag zusätzlich als wichtig markiert und oben einsortiert.
Niedrige Priorität: eigene Stichwort-Liste, wirkt nur auf Markierung und Sortierung (nicht auf interessant /
                  nicht interessant) und nur bei interessanten Einträgen. Geprüft wird im Titel (Standard) oder im
                  ganzen Text (Einstellung). Trifft ein Eintrag
                  zugleich ein wichtiges Stichwort, entscheidet die Einstellung "Konflikt".
Verknüpfung:      "oder" = eine aktive Prüfung reicht, "und" = alle aktiven Prüfungen müssen treffen.
                  Einträge ohne CPV-Codes werden nur über Stichwörter bewertet.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache

INTERESSANT = "interessant"
NICHT_INTERESSANT = "nicht_interessant"

# Ergebniswerte der Einzelprüfungen
TREFFER = "treffer"
KEIN_TREFFER = "kein_treffer"
KEINE_CODES = "keine_codes"
AUSGESCHLOSSEN = "ausgeschlossen"
AUS = "aus"

# Sortierstufen innerhalb einer Gruppe (höher = weiter oben)
LEVEL_WICHTIG = 2
LEVEL_BEIDE = 1
LEVEL_NORMAL = 0
LEVEL_NIEDRIG = -1


@dataclass
class Rules:
    cpv_codes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    priority_keywords: list[str] = field(default_factory=list)
    lowprio_keywords: list[str] = field(default_factory=list)
    # Stichwort-Listen: Strings oder (Wert, Bereich "alles"|"titel") – siehe match_terms
    lowprio_scope: str = "titel"  # Standardbereich für Niedrig-Stichwörter ohne eigenen Bereich
    priority_conflict: str = "wichtig"  # wichtig | niedrig | beide
    cpv_enabled: bool = True
    keyword_enabled: bool = True
    combine: str = "oder"


@dataclass
class Result:
    status: str
    cpv: str
    cpv_matches: list[str]
    keyword: str
    keyword_matches: list[str]
    exclusion_matches: list[str]
    priority_matches: list[str] = field(default_factory=list)
    important: bool = False
    lowprio_matches: list[str] = field(default_factory=list)
    low_priority: bool = False
    level: int = LEVEL_NORMAL


def cpv_digits(code: str) -> str:
    return re.sub(r"\D", "", code or "")[:8]


@lru_cache(maxsize=4096)
def cpv_prefix(code: str) -> str:
    digits = cpv_digits(code)
    prefix = digits.rstrip("0")
    return prefix if len(prefix) >= 2 else digits[:2]


def match_cpv(item_codes: list[str], rule_codes: list[str]) -> list[str]:
    """Liefert die Regel-Codes, unter die mindestens ein Code des Eintrags fällt."""
    digits = [cpv_digits(c) for c in item_codes if cpv_digits(c)]
    return [rule for rule in rule_codes if any(d.startswith(cpv_prefix(rule)) for d in digits)]


# Platzhalter *: beliebige Buchstaben/Ziffern, höchstens über eine Trennstelle hinweg (1–3 Leerzeichen,
# Bindestriche oder Gedankenstriche, z. B. "- " oder doppeltes Leerzeichen aus den Feeds).
# "Energie*contracting" trifft "Energiesparcontracting", "Energiespar-Contracting", "Energiespar  Contracting",
# aber nicht "Energiemanagement und Contracting" (zwei Trennstellen).
WILDCARD_REGEX = r"\w*(?:[ \-‐‑–]{1,3}\w*)?"


@lru_cache(maxsize=4096)
def keyword_pattern(term: str) -> re.Pattern | None:
    """Stichwörter mit bis zu 3 Zeichen oder in Anführungszeichen nur als ganzes Wort
    ("PV" trifft "PV-Anlage", aber nicht "PVC"). Längere Stichwörter auch als Wortteil
    ("Klimaschutz" trifft "Klimaschutzkonzept"). * ist ein Platzhalter (siehe WILDCARD_REGEX)."""
    term = term.strip()
    whole_word = len(term) >= 2 and term[0] == term[-1] == '"'
    if whole_word:
        term = term[1:-1].strip()
    if not term.replace("*", "").strip():
        return None
    escaped = WILDCARD_REGEX.join(re.escape(part) for part in term.split("*"))
    if whole_word or len(term.replace("*", "")) <= 3:
        return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


@lru_cache(maxsize=16)
def _any_term_pattern(terms: tuple[str, ...]) -> re.Pattern | None:
    patterns = [p.pattern for p in map(keyword_pattern, terms) if p]
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


SCOPE_ALL = "alles"  # Titel und Beschreibung
SCOPE_TITLE = "titel"  # nur Titel


def term_parts(term: str) -> list[str]:
    """Kombiniertes Stichwort: Teile mit + verbunden ("Konzept + Energie") – alle Teile müssen vorkommen."""
    return [part.strip() for part in term.split("+") if part.strip()]


@lru_cache(maxsize=4096)
def term_patterns(term: str) -> tuple[re.Pattern, ...]:
    patterns = tuple(keyword_pattern(part) for part in term_parts(term))
    return patterns if patterns and all(patterns) else ()


def _normalize_terms(terms, default_scope: str) -> list[tuple[str, str]]:
    """Stichwörter als (Wert, Bereich); einfache Strings gelten für den Standardbereich."""
    return [(t, default_scope) if isinstance(t, str) else (t[0], t[1] or default_scope) for t in terms]


def match_terms(text: str, terms, title: str | None = None, default_scope: str = SCOPE_ALL) -> list[str]:
    """Getroffene Stichwörter (in Reihenfolge der Liste). Bereich "titel" prüft nur `title`."""
    normalized = _normalize_terms(terms, default_scope)
    if not normalized:
        return []
    # Vorprüfung mit einem gemeinsamen Ausdruck: die meisten Texte enthalten gar kein Stichwort
    combined = _any_term_pattern(tuple(part for value, _ in normalized for part in term_parts(value)))
    if combined is None or not combined.search(f"{text}\n{title or ''}"):
        return []
    hits = []
    for value, scope in normalized:
        target = (title or "") if scope == SCOPE_TITLE else text
        patterns = term_patterns(value)
        if patterns and all(p.search(target) for p in patterns):
            hits.append(value)
    return hits


def resolve_priority(important: bool, low: bool, conflict: str) -> tuple[bool, bool, int]:
    """(als wichtig markieren, als niedrig markieren, Sortierstufe)"""
    if important and low:
        if conflict == "niedrig":
            return False, True, LEVEL_NIEDRIG
        if conflict == "beide":
            return True, True, LEVEL_BEIDE
        return True, False, LEVEL_WICHTIG
    if important:
        return True, False, LEVEL_WICHTIG
    if low:
        return False, True, LEVEL_NIEDRIG
    return False, False, LEVEL_NORMAL


def classify(
    text: str, item_codes: list[str], rules: Rules, title: str | None = None, manual_status: str | None = None
) -> Result:
    cpv_matches: list[str] = []
    if not rules.cpv_enabled:
        cpv = AUS
    elif not item_codes:
        cpv = KEINE_CODES
    else:
        cpv_matches = match_cpv(item_codes, rules.cpv_codes)
        cpv = TREFFER if cpv_matches else KEIN_TREFFER

    keyword_matches: list[str] = []
    exclusion_matches: list[str] = []
    priority_matches: list[str] = []
    if not rules.keyword_enabled:
        keyword = AUS
    else:
        priority_matches = match_terms(text, rules.priority_keywords, title)
        seen = {m.lower() for m in priority_matches}
        keyword_matches = priority_matches + [
            m for m in match_terms(text, rules.keywords, title) if m.lower() not in seen
        ]
        exclusion_matches = match_terms(text, rules.exclusions, title)
        if keyword_matches and exclusion_matches:
            keyword = AUSGESCHLOSSEN
        else:
            keyword = TREFFER if keyword_matches else KEIN_TREFFER

    active = [result == TREFFER for result in (cpv, keyword) if result not in (AUS, KEINE_CODES)]
    if not active:
        interesting = False
    elif rules.combine == "und":
        interesting = all(active)
    else:
        interesting = any(active)

    status = INTERESSANT if interesting else NICHT_INTERESSANT
    # Niedrige Priorität nur für interessante Einträge (eine manuelle Einstufung hat Vorrang)
    lowprio_matches = match_terms(text, rules.lowprio_keywords, title, default_scope=rules.lowprio_scope)
    is_interesting = (manual_status or status) == INTERESSANT
    important, low, level = resolve_priority(
        keyword == TREFFER and bool(priority_matches), is_interesting and bool(lowprio_matches), rules.priority_conflict
    )

    return Result(
        status=status,
        cpv=cpv,
        cpv_matches=cpv_matches,
        keyword=keyword,
        keyword_matches=keyword_matches,
        exclusion_matches=exclusion_matches,
        priority_matches=priority_matches,
        important=important,
        lowprio_matches=lowprio_matches,
        low_priority=low,
        level=level,
    )
