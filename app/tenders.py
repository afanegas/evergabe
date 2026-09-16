"""Abfragen für Liste und Detailansicht."""

import hashlib
import json
from dataclasses import dataclass

from . import config
from .db import connect, now, row_to_dict

STATUS_SQL = "COALESCE(manual_status, auto_status)"

# In jeder Gruppe zuerst nach Priorität (wichtig oben, niedrig unten), dann nach der gewählten Sortierung
SORTS = {
    "neueste": "COALESCE(published_at, first_seen) DESC",
    "frist": "deadline IS NULL, deadline ASC",
    "titel": "title COLLATE NOCASE ASC",
}


@dataclass(frozen=True)
class Region:
    key: str
    label: str
    open_by_default: bool


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    categories: tuple[str, ...]
    open_by_default: bool


# Reihenfolge = Reihenfolge in der Liste; weniger wichtige Regionen/Gruppen stehen unten und sind zugeklappt
REGIONS = [
    Region("berlin", "Berlin", True),
    Region("brandenburg", "Brandenburg", False),
    Region("rest", "Rest", False),
]
GROUPS = [
    Group("ausschreibungen", "Ausschreibungen", ("bekanntmachung", "vorinformation"), True),
    Group("beabsichtigt", "Beabsichtigte Vergaben", ("beschraenkt", "direktvergabe"), False),
    Group("vergeben", "Vergebene Aufträge", ("vergeben", "vertragsaenderung"), False),
]
REGIONS_BY_KEY = {r.key: r for r in REGIONS}
GROUPS_BY_KEY = {g.key: g for g in GROUPS}


@dataclass
class Filters:
    status: str = "interessant"  # interessant | nicht_interessant | alle
    kategorie: str = ""
    quelle: str = ""  # berlin | oeffentlichevergabe
    q: str = ""
    vergabestelle: str = ""
    treffer: str = ""  # cpv | stichwort | wichtig | niedrig | favorit | manuell
    frist: str = ""  # "" = abgelaufene ausblenden (Standard) | offen = nur mit offener Frist | alle
    neu: bool = False
    sort: str = "neueste"


def _not_expired() -> tuple[str, list]:
    """Frist nicht abgelaufen. Fristen ohne Uhrzeit (JJJJ-MM-TT) gelten bis Ende des Tages."""
    current = now()
    return (
        "((length(deadline) = 10 AND deadline >= ?) OR (length(deadline) > 10 AND deadline >= ?))",
        [current[:10], current[:16]],
    )


def favorite_needle(pattern: str) -> str:
    """So steht ein Auftraggeber-Muster in der JSON-Spalte authority_matches."""
    return json.dumps(pattern, ensure_ascii=False)


def favorite_key(pattern: str) -> str:
    return "ag-" + hashlib.md5(pattern.lower().encode("utf-8")).hexdigest()[:8]


def _where(
    filters: Filters,
    include_status: bool = True,
    region: str = "",
    categories: tuple[str, ...] = (),
    favorite_pattern: str = "",
    frist: str | None = None,
) -> tuple[str, list]:
    clauses, params = [], []
    if favorite_pattern:
        clauses.append("favorite = 1 AND instr(authority_matches, ?) > 0")
        params.append(favorite_needle(favorite_pattern))
    if region:
        clauses.append("region = ?")
        params.append(region)
    if categories:
        clauses.append(f"category IN ({', '.join('?' * len(categories))})")
        params.extend(categories)
    if include_status and filters.status in ("interessant", "nicht_interessant"):
        clauses.append(f"{STATUS_SQL} = ?")
        params.append(filters.status)
    if filters.kategorie:
        clauses.append("category = ?")
        params.append(filters.kategorie)
    if filters.quelle:
        clauses.append("EXISTS (SELECT 1 FROM tender_sources s WHERE s.tender_id = tenders.id AND s.source = ?)")
        params.append(filters.quelle)
    if filters.q:
        like = f"%{filters.q}%"
        cols = ("title", "subject", "service_type", "description", "authority", "contractor", "cpv_codes")
        clauses.append("(" + " OR ".join(f"{c} LIKE ?" for c in cols) + ")")
        params.extend([like] * len(cols))
    if filters.vergabestelle:
        clauses.append("authority LIKE ?")
        params.append(f"%{filters.vergabestelle}%")
    if filters.treffer == "cpv":
        clauses.append("cpv_result = 'treffer'")
    elif filters.treffer == "stichwort":
        clauses.append("keyword_result = 'treffer'")
    elif filters.treffer == "wichtig":
        clauses.append("important = 1")
    elif filters.treffer == "niedrig":
        clauses.append("low_priority = 1")
    elif filters.treffer == "favorit":
        clauses.append("favorite = 1")
    elif filters.treffer == "manuell":
        clauses.append("manual_status IS NOT NULL")
    frist = filters.frist if frist is None else frist
    if frist in ("", "offen", "abgelaufen"):
        condition, condition_params = _not_expired()
        if frist == "abgelaufen":  # nur für die Anzeige „N abgelaufene ausgeblendet“
            clauses.append(f"deadline IS NOT NULL AND deadline != '' AND NOT {condition}")
        elif frist == "offen":
            clauses.append(f"deadline IS NOT NULL AND deadline != '' AND {condition}")
        else:
            clauses.append(f"(deadline IS NULL OR deadline = '' OR {condition})")
        params.extend(condition_params)
    if filters.neu:
        clauses.append("seen_at IS NULL")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def attach_sources(conn, items: list[dict]) -> None:
    if not items:
        return
    by_id = {item["id"]: item for item in items}
    for item in items:
        item["sources"] = []
    rows = conn.execute(
        f"SELECT tender_id, source, url FROM tender_sources WHERE tender_id IN ({', '.join('?' * len(by_id))}) "
        "ORDER BY id",
        list(by_id),
    ).fetchall()
    for row in rows:
        by_id[row["tender_id"]]["sources"].append({"source": row["source"], "url": row["url"]})


def search(
    filters: Filters, region: str, categories: tuple[str, ...], limit: int = config.PAGE_SIZE, favorite_pattern: str = ""
) -> dict:
    where, params = _where(filters, region=region, categories=categories, favorite_pattern=favorite_pattern)
    order = SORTS.get(filters.sort, SORTS["neueste"])
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM tenders{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM tenders{where} ORDER BY priority_level DESC, {order}, id DESC LIMIT ?", [*params, limit]
        ).fetchall()
        items = [row_to_dict(r) for r in rows]
        attach_sources(conn, items)
    return {"items": items, "total": total}


def structure(filters: Filters) -> list[dict]:
    """Regionen mit Gruppen und Anzahlen (ohne Einträge) – eine Abfrage für alle Zähler.
    Bei gesetztem Kategorie-Filter nur die Gruppe, die diese Kategorie enthält."""
    where, params = _where(filters)
    counts: dict[tuple[str, str], tuple[int, int, int]] = {}
    with connect() as conn:
        for row in conn.execute(
            f"SELECT region, category, COUNT(*), COALESCE(SUM(seen_at IS NULL), 0), COALESCE(SUM(important), 0) "
            f"FROM tenders{where} GROUP BY 1, 2",
            params,
        ):
            counts[(row[0], row[1])] = (row[2], row[3], row[4])

    regions = []
    for region in REGIONS:
        groups = []
        for group in GROUPS:
            if filters.kategorie and filters.kategorie not in group.categories:
                continue
            numbers = [counts.get((region.key, c), (0, 0, 0)) for c in group.categories]
            groups.append({
                "group": group,
                "key": f"{region.key}-{group.key}",
                "total": sum(n[0] for n in numbers),
                "new": sum(n[1] for n in numbers),
                "important": sum(n[2] for n in numbers),
            })
        regions.append({
            "region": region,
            "groups": groups,
            "total": sum(g["total"] for g in groups),
            "new": sum(g["new"] for g in groups),
            "important": sum(g["important"] for g in groups),
        })
    return regions


def expired_hidden(filters: Filters, favorites_only: bool = False) -> int:
    """Anzahl der wegen abgelaufener Frist ausgeblendeten Einträge (nur beim Standard-Fristfilter)."""
    if filters.frist != "":
        return 0
    where, params = _where(filters, frist="abgelaufen")
    if favorites_only:
        where += (" AND " if where else " WHERE ") + "favorite = 1"
    with connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM tenders{where}", params).fetchone()[0]


def favorites_structure(filters: Filters, patterns: list[str]) -> list[dict]:
    """Beobachtete Auftraggeber mit Gruppen und Anzahlen (Reiter „♥ Auftraggeber“)."""
    blocks = []
    with connect() as conn:
        for pattern in patterns:
            where, params = _where(filters, favorite_pattern=pattern)
            counts = {
                row[0]: (row[1], row[2], row[3])
                for row in conn.execute(
                    f"SELECT category, COUNT(*), COALESCE(SUM(seen_at IS NULL), 0), COALESCE(SUM(important), 0) "
                    f"FROM tenders{where} GROUP BY category",
                    params,
                )
            }
            names_where, names_params = _where(Filters(status="alle", frist="alle"), favorite_pattern=pattern)
            names = [
                r[0] for r in conn.execute(
                    f"SELECT authority, COUNT(*) AS n FROM tenders{names_where} GROUP BY authority ORDER BY n DESC LIMIT 6",
                    names_params,
                )
            ]
            key = favorite_key(pattern)
            groups = []
            for group in GROUPS:
                if filters.kategorie and filters.kategorie not in group.categories:
                    continue
                numbers = [counts.get(c, (0, 0, 0)) for c in group.categories]
                groups.append({
                    "group": group,
                    "key": f"{key}-{group.key}",
                    "total": sum(n[0] for n in numbers),
                    "new": sum(n[1] for n in numbers),
                    "important": sum(n[2] for n in numbers),
                })
            blocks.append({
                "pattern": pattern,
                "key": key,
                "names": names,
                "groups": groups,
                "total": sum(g["total"] for g in groups),
                "new": sum(g["new"] for g in groups),
                "important": sum(g["important"] for g in groups),
            })
    return blocks


def status_counts(filters: Filters, favorites_only: bool = False) -> dict:
    where, params = _where(filters, include_status=False)
    if favorites_only:
        where += (" AND " if where else " WHERE ") + "favorite = 1"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {STATUS_SQL} AS status, COUNT(*) AS n FROM tenders{where} GROUP BY 1", params
        ).fetchall()
    counts = {"interessant": 0, "nicht_interessant": 0}
    counts.update({r["status"]: r["n"] for r in rows})
    counts["alle"] = counts["interessant"] + counts["nicht_interessant"]
    return counts


def overview() -> dict:
    with connect() as conn:
        unseen = conn.execute(
            f"SELECT COUNT(*) FROM tenders WHERE seen_at IS NULL AND {STATUS_SQL} = 'interessant'"
        ).fetchone()[0]
        last = conn.execute("SELECT * FROM fetch_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        authorities = [
            r[0] for r in conn.execute(
                # Vorschlagsliste nur für Berlin/Brandenburg – bundesweit wären es tausende Namen
                "SELECT DISTINCT authority FROM tenders WHERE authority IS NOT NULL AND region != 'rest' "
                "ORDER BY authority COLLATE NOCASE"
            )
        ]
    return {"unseen_interesting": unseen, "last_run": dict(last) if last else None, "authorities": authorities}


def source_counts() -> dict[str, int]:
    """Gespeicherte Einträge je Berlin-Feed (Kategorie) und je Quelle."""
    with connect() as conn:
        counts = {
            r[0]: r[1] for r in conn.execute(
                "SELECT t.category, COUNT(*) FROM tenders t JOIN tender_sources s ON s.tender_id = t.id "
                "WHERE s.source = 'berlin' GROUP BY t.category"
            )
        }
        for r in conn.execute("SELECT source, COUNT(*) FROM tender_sources GROUP BY source"):
            counts[f"quelle:{r[0]}"] = r[1]
    return counts


def get(tender_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tenders WHERE id = ?", (tender_id,)).fetchone()
        if row is None:
            return None
        item = row_to_dict(row)
        attach_sources(conn, [item])
    return item


def mark_seen(tender_ids: list[int] | None = None) -> None:
    with connect() as conn:
        if tender_ids is None:
            conn.execute("UPDATE tenders SET seen_at = ? WHERE seen_at IS NULL", (now(),))
        else:
            conn.executemany(
                "UPDATE tenders SET seen_at = ? WHERE id = ? AND seen_at IS NULL", [(now(), i) for i in tender_ids]
            )


def set_manual_status(tender_id: int, status: str | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE tenders SET manual_status = ? WHERE id = ?", (status, tender_id))


def set_note(tender_id: int, note: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE tenders SET note = ? WHERE id = ?", (note.strip() or None, tender_id))
