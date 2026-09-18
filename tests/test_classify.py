import pytest

from app.classify import (
    AUS,
    AUSGESCHLOSSEN,
    INTERESSANT,
    KEIN_TREFFER,
    KEINE_CODES,
    NICHT_INTERESSANT,
    TREFFER,
    Rules,
    classify,
    cpv_prefix,
    match_cpv,
    match_terms,
)


def rules(**kwargs) -> Rules:
    base = dict(
        cpv_codes=["71314000", "09330000", "45261215"],
        keywords=["Klimaschutz", "PV", "Energieberatung", '"Solar"'],
        exclusions=["Reinigung"],
    )
    base.update(kwargs)
    return Rules(**base)


def test_cpv_prefix_strips_placeholder_zeros():
    assert cpv_prefix("71314000") == "71314"
    assert cpv_prefix("09330000-1") == "0933"
    assert cpv_prefix("45000000") == "45"
    assert cpv_prefix("45261215") == "45261215"


def test_cpv_hierarchy_match():
    assert match_cpv(["71314300-5"], ["71314000"]) == ["71314000"]
    assert match_cpv(["71315000-0"], ["71314000"]) == []
    assert match_cpv(["09331200-0", "45000000-7"], ["09330000", "71314000"]) == ["09330000"]


def test_keywords_substring_and_whole_word():
    text = "Erstellung eines Klimaschutzkonzepts inkl. PV-Anlage"
    assert match_terms(text, ["klimaschutz", "PV"]) == ["klimaschutz", "PV"]
    assert match_terms("PVC-Bodenbelag erneuern", ["PV"]) == []
    assert match_terms("Solaranlage", ['"Solar"']) == []
    assert match_terms("Solar und Wind", ['"Solar"']) == ['"Solar"']


def test_or_combination():
    r = classify("Malerarbeiten", ["71314300-5"], rules())
    assert (r.cpv, r.keyword, r.status) == (TREFFER, KEIN_TREFFER, INTERESSANT)


def test_and_combination_requires_both():
    r = classify("Malerarbeiten", ["71314300-5"], rules(combine="und"))
    assert r.status == NICHT_INTERESSANT
    r = classify("Energieberatung für Schulen", ["71314300-5"], rules(combine="und"))
    assert r.status == INTERESSANT


def test_items_without_cpv_use_keywords_only():
    r = classify("Energieberatung", [], rules(combine="und"))
    assert (r.cpv, r.keyword, r.status) == (KEINE_CODES, TREFFER, INTERESSANT)


def test_cpv_disabled():
    r = classify("Malerarbeiten", ["71314300-5"], rules(cpv_enabled=False))
    assert (r.cpv, r.status) == (AUS, NICHT_INTERESSANT)


def test_keywords_disabled_and_no_codes_is_not_interesting():
    r = classify("Energieberatung", [], rules(keyword_enabled=False))
    assert (r.cpv, r.keyword, r.status) == (KEINE_CODES, AUS, NICHT_INTERESSANT)


def test_exclusion_cancels_keyword_hit_but_not_cpv():
    r = classify("Reinigung der PV-Anlage", [], rules())
    assert (r.keyword, r.status) == (AUSGESCHLOSSEN, NICHT_INTERESSANT)
    assert r.exclusion_matches == ["Reinigung"]
    r = classify("Reinigung der PV-Anlage", ["09331200-0"], rules())
    assert r.status == INTERESSANT


def test_priority_keyword_counts_as_keyword_and_marks_important():
    r = classify("Kommunale Wärmeplanung Stadt X", [], rules(priority_keywords=["Wärmeplanung"]))
    assert (r.keyword, r.status, r.important) == (TREFFER, INTERESSANT, True)
    assert r.priority_matches == ["Wärmeplanung"]
    assert r.keyword_matches == ["Wärmeplanung"]


def test_priority_keyword_follows_normal_rules():
    # Ausschluss-Stichwort hebt auch wichtige Stichwörter auf
    r = classify("Reinigung Wärmenetz", [], rules(priority_keywords=["Wärmenetz"]))
    assert (r.keyword, r.status, r.important) == (AUSGESCHLOSSEN, NICHT_INTERESSANT, False)
    # UND-Verknüpfung: wichtig markiert, aber ohne CPV-Treffer nicht interessant
    r = classify("Wärmeplanung", ["45000000-7"], rules(priority_keywords=["Wärmeplanung"], combine="und"))
    assert (r.status, r.important) == (NICHT_INTERESSANT, True)
    # Stichwort-Prüfung aus: keine Markierung
    r = classify("Wärmeplanung", [], rules(priority_keywords=["Wärmeplanung"], keyword_enabled=False))
    assert r.important is False


def test_normal_and_priority_duplicates_are_listed_once():
    r = classify("Klimaschutz", [], rules(priority_keywords=["klimaschutz"]))
    assert r.keyword_matches == ["klimaschutz"]


@pytest.mark.parametrize("text, expected", [
    ("Energiesparcontracting", True),
    ("Energiespar-Contracting", True),
    ("Energiespar Contracting", True),
    ("Energieeinsparcontracting", True),
    ("Energieeinspar-Contracting", True),
    ("Energieeinspar Contracting", True),
    ("Energieliefer-Contracting", False),
    ("Energiemanagement und Contracting", False),  # nicht über mehrere Wörter hinweg
    ("energiespar contracting", True),
    ("Energiespar  Contracting", True),  # doppeltes Leerzeichen (kommt in Feeds vor)
    ("Energiespar- Contracting", True),
    ("Energiespar – Contracting", True),  # Gedankenstrich
    ("Energie-Einspar-Contracting", True),
])
def test_wildcard_keyword(text, expected):
    assert bool(match_terms(f"Ausschreibung {text} Schule", ["Energie*spar*contracting"])) is expected


def test_wildcard_edge_cases():
    assert match_terms("Wärmepumpen und Wärmenetze", ["Wärme*netz"]) == ["Wärme*netz"]
    assert match_terms("PV-Anlage", ['"PV*"']) == ['"PV*"']
    assert match_terms("GPV-Anlage", ['"PV*"']) == []  # ganzes Wort: nicht mitten im Wort
    assert match_terms("irgendwas", ["*"]) == []  # nur Platzhalter wirkt nicht
    r = classify("Energieeinspar-Contracting Rathaus", [], rules(priority_keywords=["Energie*spar*contracting"]))
    assert (r.important, r.priority_matches) == (True, ["Energie*spar*contracting"])


def test_low_priority_only_for_interesting_entries():
    title = "Energieberatung Planungsleistungen HOAI"
    r = classify(title, [], rules(lowprio_keywords=["HOAI"]), title=title)
    assert (r.status, r.low_priority, r.level, r.lowprio_matches) == (INTERESSANT, True, -1, ["HOAI"])
    # nicht interessant: Stichwort wird gefunden, aber nicht markiert
    r = classify("Malerarbeiten HOAI", [], rules(lowprio_keywords=["HOAI"]), title="Malerarbeiten HOAI")
    assert (r.status, r.low_priority, r.level, r.lowprio_matches) == (NICHT_INTERESSANT, False, 0, ["HOAI"])
    # manuelle Einstufung hat Vorrang
    r = classify("Malerarbeiten HOAI", [], rules(lowprio_keywords=["HOAI"]), title="Malerarbeiten HOAI",
                 manual_status="interessant")
    assert r.low_priority is True
    r = classify(title, [], rules(lowprio_keywords=["HOAI"]), title=title, manual_status="nicht_interessant")
    assert r.low_priority is False


def test_low_priority_scope_title_is_default():
    text = "Energieberatung Schule\nLeistungen nach HOAI"
    assert rules().lowprio_scope == "titel"
    r = classify(text, [], rules(lowprio_keywords=["HOAI"]), title="Energieberatung Schule")
    assert r.low_priority is False
    r = classify(text, [], rules(lowprio_keywords=["HOAI"], lowprio_scope="alles"), title="Energieberatung Schule")
    assert r.low_priority is True


@pytest.mark.parametrize("conflict, expected", [
    ("wichtig", (True, False, 2)),
    ("niedrig", (False, True, -1)),
    ("beide", (True, True, 1)),
])
def test_conflict_between_important_and_low_priority(conflict, expected):
    title = "Wärmeplanung – Planungsleistungen"
    r = classify(title, [], rules(priority_keywords=["Wärmeplanung"], lowprio_keywords=["Planungsleistungen"],
                                  priority_conflict=conflict), title=title)
    assert (r.important, r.low_priority, r.level) == expected
    assert r.lowprio_matches == ["Planungsleistungen"]  # Hinweis bleibt auch, wenn wichtig gewinnt


@pytest.mark.parametrize("text, expected", [
    ("Erstellung eines Konzepts zur Energieversorgung", True),
    ("Energiekonzept für die Grundschule", True),  # Wortteile zählen, Reihenfolge egal
    ("Konzept für den Schulhof", False),  # nur ein Teil
    ("Energieausweis", False),
])
def test_combined_keyword_requires_all_parts(text, expected):
    assert bool(match_terms(text, ["Konzept + Energie"])) is expected


def test_combined_keyword_with_wildcard_and_short_part():
    assert match_terms("PV-Anlage mit Speicherkonzept", ["PV + Speicher*konzept"]) == ["PV + Speicher*konzept"]
    assert match_terms("PVC-Boden, Speicherkonzept", ["PV + Speicher*konzept"]) == []  # PV nur als ganzes Wort
    assert match_terms("irgendwas", ["+"]) == []


def test_scope_per_keyword():
    title = "Neubau Grundschule"
    text = f"{title}\nInklusive Energiekonzept und Photovoltaik"
    terms = [("Energiekonzept", "titel"), ("Photovoltaik", "alles")]
    assert match_terms(text, terms, title) == ["Photovoltaik"]
    assert match_terms(text, [("Neubau + Energiekonzept", "titel")], title) == []  # alle Teile im Titel nötig
    r = classify(text, [], rules(keywords=terms, exclusions=[("Photovoltaik", "titel")]), title=title)
    assert (r.keyword, r.keyword_matches) == (TREFFER, ["Photovoltaik"])  # Ausschluss nur im Titel greift nicht
