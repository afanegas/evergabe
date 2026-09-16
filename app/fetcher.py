"""Abruf der Quellen, Nachladen der Detailseiten, Klassifizierung und Aufräumen."""

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta

import httpx

from . import config, settings
from .categories import SOURCE_BERLIN, SOURCE_OV
from .classify import Rules, classify, match_terms
from .db import connect, now, row_to_dict
from .dedupe import find_duplicate
from .sources import oeffentlichevergabe
from .sources.berlin import FEEDS, parse_feed
from .sources.details import detail_kind, parse_detail

log = logging.getLogger(__name__)

_lock = threading.Lock()
state = {"running": False, "phase": "", "done": 0, "total": 0}

FEED_COLUMNS = (
    "category", "title", "url", "published_at", "online_since", "procedure", "location", "deadline",
    "deadline_label", "authority", "contractor", "subject", "service_type", "execution_period",
)
# Werte von der Detailseite ergänzen die Feed-Daten nur, wenn diese fehlen
DETAIL_FILL_COLUMNS = (
    "procedure", "location", "deadline", "deadline_label", "authority", "contractor", "subject",
    "service_type", "execution_period",
)


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": config.USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"},
        timeout=config.HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
    )


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _merge_codes(existing: list[dict], extra: list[dict]) -> list[dict]:
    """CPV-Codes zusammenführen; Bezeichnungen (vom Berlin-Detail) haben Vorrang."""
    by_digits: dict[str, dict] = {}
    for entry in [*existing, *extra]:
        key = "".join(ch for ch in entry["code"] if ch.isdigit())[:8]
        if key not in by_digits or (entry.get("label") and not by_digits[key].get("label")):
            by_digits[key] = entry
    return list(by_digits.values())


def _merge_documents(existing: list[dict], extra: list[dict]) -> list[dict]:
    urls = {d["url"] for d in existing}
    return existing + [d for d in extra if d["url"] not in urls]


# ---------- Speichern ----------

def upsert_items(conn, items: list[dict], source: str = SOURCE_BERLIN) -> dict:
    """Einträge einer Quelle speichern. Bekannte Einträge (gleiche uid) werden aktualisiert,
    eindeutige Dubletten aus der anderen Quelle zusammengeführt."""
    stats = {"new": 0, "updated": 0, "merged": 0, "ids": set()}
    ts = now()
    for item in items:
        item.setdefault("region", "berlin")
        known = conn.execute("SELECT tender_id FROM tender_sources WHERE uid = ?", (item["uid"],)).fetchone()
        if known:
            primary = conn.execute("SELECT source FROM tenders WHERE id = ?", (known["tender_id"],)).fetchone()
            _update(conn, known["tender_id"], item, primary["source"] == source, ts)
            stats["updated"] += 1
            stats["ids"].add(known["tender_id"])
            continue

        duplicate = find_duplicate(conn, item, source)
        if duplicate:
            conn.execute(
                "INSERT INTO tender_sources(tender_id, source, uid, url, first_seen) VALUES(?, ?, ?, ?, ?)",
                (duplicate, source, item["uid"], item.get("url"), ts),
            )
            _update(conn, duplicate, item, False, ts)
            if source == SOURCE_BERLIN and detail_kind(item.get("url")):
                conn.execute(
                    "UPDATE tenders SET detail_status = 'offen', detail_attempts = 0 WHERE id = ?", (duplicate,)
                )
            stats["merged"] += 1
            stats["ids"].add(duplicate)
            continue

        tender_id = _insert(conn, item, source, ts)
        stats["new"] += 1
        stats["ids"].add(tender_id)
    return stats


def _insert(conn, item: dict, source: str, ts: str) -> int:
    values = {col: item.get(col) for col in FEED_COLUMNS}
    values.update(
        uid=item["uid"],
        source=source,
        region=item["region"],
        feed_fields=_dumps(item.get("feed_fields", {})),
        detail_fields=_dumps(item.get("detail_fields", {})),
        cpv_codes=_dumps(item.get("cpv_codes", [])),
        documents=_dumps(item.get("documents", [])),
        description=item.get("description"),
        detail_status="offen" if source == SOURCE_BERLIN and detail_kind(item.get("url")) else "keine",
        first_seen=ts,
        last_seen=ts,
    )
    tender_id = conn.execute(
        f"INSERT INTO tenders({', '.join(values)}) VALUES({', '.join('?' * len(values))})", list(values.values())
    ).lastrowid
    conn.execute(
        "INSERT INTO tender_sources(tender_id, source, uid, url, first_seen) VALUES(?, ?, ?, ?, ?)",
        (tender_id, source, item["uid"], item.get("url"), ts),
    )
    return tender_id


def _update(conn, tender_id: int, item: dict, primary: bool, ts: str) -> None:
    """Primäre Quelle: neue Werte übernehmen (z. B. geänderte Fristen), aber nicht mit leeren überschreiben.
    Zusätzliche Quelle: nur fehlende Werte ergänzen."""
    row = row_to_dict(conn.execute("SELECT * FROM tenders WHERE id = ?", (tender_id,)).fetchone())
    columns = FEED_COLUMNS if primary else tuple(c for c in FEED_COLUMNS if c not in ("category", "url"))
    updates = {}
    for col in columns:
        value = item.get(col)
        if value and (primary or not row[col]):
            updates[col] = value

    if item.get("feed_fields"):
        updates["feed_fields"] = _dumps(item["feed_fields"] if primary else {**item["feed_fields"], **row["feed_fields"]})
    if item.get("detail_fields"):
        merged = {**row["detail_fields"], **item["detail_fields"]} if primary else {**item["detail_fields"], **row["detail_fields"]}
        updates["detail_fields"] = _dumps(merged)
    if item.get("cpv_codes"):
        updates["cpv_codes"] = _dumps(_merge_codes(row["cpv_codes"], item["cpv_codes"]))
    if item.get("documents"):
        updates["documents"] = _dumps(_merge_documents(row["documents"], item["documents"]))
    if item.get("description") and (primary or not row["description"]):
        updates["description"] = item["description"]
    if primary and item.get("region"):
        updates["region"] = item["region"]
    updates["last_seen"] = ts

    assignments = ", ".join(f"{col} = ?" for col in updates)
    conn.execute(f"UPDATE tenders SET {assignments} WHERE id = ?", [*updates.values(), tender_id])


def apply_detail(conn, tender_id: int, detail: dict) -> None:
    row = row_to_dict(conn.execute("SELECT * FROM tenders WHERE id = ?", (tender_id,)).fetchone())
    description = row["description"]
    if detail.get("description") and detail["description"] not in (description or ""):
        description = f"{detail['description']}\n{description}" if description else detail["description"]
    updates = {
        "cpv_codes": _dumps(_merge_codes(detail.get("cpv_codes", []), row["cpv_codes"])),
        "documents": _dumps(_merge_documents(row["documents"], detail.get("documents", []))),
        "detail_fields": _dumps({**row["detail_fields"], **detail.get("fields", {})}),
        "description": description,
        "detail_status": "ok",
        "detail_error": None,
        "detail_fetched_at": now(),
    }
    for col in DETAIL_FILL_COLUMNS:
        if not row[col] and detail.get(col):
            updates[col] = detail[col]
    assignments = ", ".join(f"{col} = ?" for col in updates)
    conn.execute(f"UPDATE tenders SET {assignments} WHERE id = ?", [*updates.values(), tender_id])


# ---------- Abruf je Quelle ----------

def fetch_berlin_feeds(client: httpx.Client, feeds, feed_status: dict) -> tuple[dict, list[str]]:
    totals = {"new": 0, "updated": 0, "merged": 0, "ids": set()}
    errors = []
    state.update(phase="Vergabeplattform Berlin", done=0, total=len(feeds))
    for i, feed in enumerate(feeds):
        status = {"at": now(), "items": 0, "new": 0, "error": None}
        try:
            response = client.get(feed.url)
            response.raise_for_status()
            items = parse_feed(response.content, feed.key)
            with connect() as conn:
                stats = upsert_items(conn, items, SOURCE_BERLIN)
            _add(totals, stats)
            status.update(items=len(items), new=stats["new"])
            log.info("Feed %s: %d Einträge, %d neu", feed.key, len(items), stats["new"])
        except Exception as exc:
            log.exception("Feed %s fehlgeschlagen", feed.key)
            errors.append(f"Feed {feed.label}: {exc}")
            status["error"] = str(exc)[:300]
        feed_status[feed.key] = track_failures(feed_status.get(feed.key), status)
        state["done"] = i + 1
    return totals, errors


def track_failures(previous: dict | None, status: dict) -> dict:
    """Fehlgeschlagene Abrufe einer Quelle in Folge zählen (für die Warnung); ein Erfolg setzt zurück."""
    previous = previous or {}
    if status.get("error"):
        status["failures"] = previous.get("failures", 0) + 1
        status["failing_since"] = previous.get("failing_since") if previous.get("failures") else status["at"]
    else:
        status["failures"] = 0
        status["failing_since"] = None
    return status


def ov_days(last_day: str | None, today: date) -> list[date]:
    """Zu ladende Tage: der zuletzt geladene Tag noch einmal (falls der Export später ergänzt wurde)
    bis gestern. Beim ersten Mal die letzten OV_BACKFILL_DAYS Tage."""
    yesterday = today - timedelta(days=1)
    if last_day:
        start = date.fromisoformat(last_day)
    else:
        start = yesterday - timedelta(days=config.OV_BACKFILL_DAYS - 1)
    start = max(start, yesterday - timedelta(days=config.OV_MAX_DAYS_PER_RUN - 1))
    return [start + timedelta(days=i) for i in range((yesterday - start).days + 1)]


def fetch_oeffentlichevergabe(client: httpx.Client, last_day: str | None) -> tuple[dict, dict, list[str]]:
    totals = {"new": 0, "updated": 0, "merged": 0, "ids": set()}
    status = {"at": now(), "items": 0, "new": 0, "merged": 0, "day": last_day, "error": None}
    errors = []
    days = ov_days(last_day, date.fromisoformat(now()[:10]))
    state.update(phase="oeffentlichevergabe.de", done=0, total=len(days))
    for i, day in enumerate(days):
        state["phase"] = f"oeffentlichevergabe.de – {day:%d.%m.%Y}"
        try:
            response = client.get(oeffentlichevergabe.export_url(day), timeout=config.OV_TIMEOUT_SECONDS)
            response.raise_for_status()
            items, skipped = oeffentlichevergabe.parse_export(response.content)
            with connect() as conn:
                stats = upsert_items(conn, items, SOURCE_OV)
            _add(totals, stats)
            status.update(items=status["items"] + len(items), new=totals["new"], merged=totals["merged"], day=day.isoformat())
            settings.set_many({"ov_last_day": day.isoformat()})
            log.info("oeffentlichevergabe.de %s: %d Einträge, %d neu, %d zusammengeführt, %d übersprungen",
                     day, len(items), stats["new"], stats["merged"], skipped)
        except Exception as exc:
            # Tag beim nächsten Abruf erneut versuchen
            log.exception("oeffentlichevergabe.de %s fehlgeschlagen", day)
            errors.append(f"oeffentlichevergabe.de {day:%d.%m.%Y}: {exc}")
            status["error"] = str(exc)[:300]
            break
        state["done"] = i + 1
    return totals, status, errors


def fetch_details(client: httpx.Client) -> tuple[int, list[str]]:
    with connect() as conn:
        pending = conn.execute(
            "SELECT t.id, s.url FROM tenders t JOIN tender_sources s ON s.tender_id = t.id AND s.source = ? "
            "WHERE t.detail_status IN ('offen', 'fehler') AND t.detail_attempts < ? ORDER BY t.published_at DESC",
            (SOURCE_BERLIN, config.DETAIL_MAX_ATTEMPTS),
        ).fetchall()

    fetched, errors, touched = 0, [], []
    state.update(phase="Detailseiten", done=0, total=len(pending))
    for i, row in enumerate(pending):
        if i:
            time.sleep(config.DETAIL_DELAY_SECONDS)
        try:
            response = client.get(row["url"])
            response.raise_for_status()
            detail = parse_detail(row["url"], response.text)
            with connect() as conn:
                if detail is None:
                    conn.execute("UPDATE tenders SET detail_status = 'keine' WHERE id = ?", (row["id"],))
                else:
                    apply_detail(conn, row["id"], detail)
                    fetched += 1
                    touched.append(row["id"])
        except Exception as exc:  # einzelne Detailseite darf den Lauf nicht abbrechen
            log.warning("Detailseite %s: %s", row["url"], exc)
            with connect() as conn:
                conn.execute(
                    "UPDATE tenders SET detail_status = 'fehler', detail_attempts = detail_attempts + 1, "
                    "detail_error = ? WHERE id = ?",
                    (str(exc)[:500], row["id"]),
                )
            errors.append(f"Detailseite {row['url']}: {exc}")
        state["done"] = i + 1
        if len(touched) >= 25:
            reclassify(touched)  # Liste füllt sich schon während des Abrufs mit CPV-Treffern
            touched = []
    if touched:
        reclassify(touched)
    return fetched, errors


def _add(totals: dict, stats: dict) -> None:
    for key in ("new", "updated", "merged"):
        totals[key] += stats[key]
    totals["ids"] |= stats["ids"]


# ---------- Klassifizierung & Aufräumen ----------

def classification_text(item: dict) -> str:
    detail_fields = item.get("detail_fields") or {}
    parts = [
        item.get("title"), item.get("subject"), item.get("service_type"), item.get("description"),
        detail_fields.get("Maßnahme"),
    ]
    return "\n".join(p for p in parts if p)


def reclassify(ids=None, rules: Rules | None = None) -> dict:
    """Einträge neu einstufen – alle (ids=None) oder nur die angegebenen."""
    rules = rules or settings.load_rules()
    counts = {"gesamt": 0, "interessant": 0}
    favorites = settings.load_favorites()
    columns = (
        "id, title, subject, service_type, description, detail_fields, cpv_codes, manual_status, auto_status, authority"
    )
    with connect() as conn:
        if ids is None:
            batches = [conn.execute(f"SELECT {columns} FROM tenders").fetchall()]
        else:
            ids = list(ids)
            batches = [
                conn.execute(
                    f"SELECT {columns} FROM tenders WHERE id IN ({', '.join('?' * len(chunk))})", chunk
                ).fetchall()
                for chunk in (ids[i:i + 900] for i in range(0, len(ids), 900))
            ]
        updates = []
        for rows in batches:
            for row in rows:
                item = row_to_dict(row)
                result = classify(
                    classification_text(item), [c["code"] for c in item["cpv_codes"]], rules,
                    title=item["title"], manual_status=row["manual_status"],
                )
                updates.append((
                    result.status, result.cpv, _dumps(result.cpv_matches), result.keyword,
                    _dumps(result.keyword_matches), _dumps(result.exclusion_matches),
                    _dumps(result.priority_matches), int(result.important),
                    _dumps(result.lowprio_matches), int(result.low_priority), result.level,
                    *_favorite_values(row["authority"], favorites), row["id"],
                ))
                counts["gesamt"] += 1
                if (row["manual_status"] or result.status) == "interessant":
                    counts["interessant"] += 1
        conn.executemany(
            "UPDATE tenders SET auto_status = ?, cpv_result = ?, cpv_matches = ?, keyword_result = ?, "
            "keyword_matches = ?, exclusion_matches = ?, priority_matches = ?, important = ?, "
            "lowprio_matches = ?, low_priority = ?, priority_level = ?, favorite = ?, authority_matches = ? WHERE id = ?",
            updates,
        )
    return counts


def _favorite_values(authority: str | None, favorites: list[str]) -> tuple[int, str]:
    matches = match_terms(authority or "", favorites) if authority else []
    return int(bool(matches)), _dumps(matches)


def refresh_favorites() -> int:
    """Nur die Markierung „beobachteter Auftraggeber“ neu berechnen (schnell, ohne Neu-Klassifizierung)."""
    favorites = settings.load_favorites()
    with connect() as conn:
        rows = conn.execute("SELECT id, authority FROM tenders").fetchall()
        updates = [(*_favorite_values(row["authority"], favorites), row["id"]) for row in rows]
        conn.executemany("UPDATE tenders SET favorite = ?, authority_matches = ? WHERE id = ?", updates)
    return sum(1 for u in updates if u[0])


def reclassify_all(rules: Rules | None = None) -> dict:
    return reclassify(None, rules)


def cleanup_rest() -> int:
    """Nicht interessante Einträge der Region „Rest“ nach REST_RETENTION_DAYS löschen –
    außer sie wurden manuell eingestuft, haben eine Notiz oder stammen von einem beobachteten Auftraggeber."""
    cutoff = (datetime.fromisoformat(now()) - timedelta(days=config.REST_RETENTION_DAYS)).isoformat()
    condition = (
        "region = 'rest' AND manual_status IS NULL AND note IS NULL AND auto_status = 'nicht_interessant' AND favorite = 0 "
        "AND COALESCE(published_at, first_seen) < ?"
    )
    with connect() as conn:
        conn.execute(
            f"DELETE FROM tender_sources WHERE tender_id IN (SELECT id FROM tenders WHERE {condition})", (cutoff,)
        )
        deleted = conn.execute(f"DELETE FROM tenders WHERE {condition}", (cutoff,)).rowcount
    if deleted:
        log.info("Aufgeräumt: %d alte, nicht interessante Einträge (Rest) gelöscht", deleted)
    return deleted


# ---------- Gesamtlauf ----------

def run_fetch(trigger: str = "zeitplan") -> bool:
    """Kompletter Abruf. Gibt False zurück, wenn bereits ein Abruf läuft."""
    if not _lock.acquire(blocking=False):
        return False
    state.update(running=True, phase="Start", done=0, total=0)
    errors: list[str] = []
    totals = {"new": 0, "updated": 0, "merged": 0, "ids": set()}
    details = 0
    with connect() as conn:
        run_id = conn.execute(
            "INSERT INTO fetch_runs(trigger, started_at) VALUES(?, ?)", (trigger, now())
        ).lastrowid
    try:
        values = settings.get_all()
        feed_status = dict(values["feed_status"])
        with _client() as client:
            active_feeds = [feed for feed in FEEDS if feed.key not in values["feeds_disabled"]]
            if active_feeds:
                stats, feed_errors = fetch_berlin_feeds(client, active_feeds, feed_status)
                _add(totals, stats)
                errors.extend(feed_errors)
                reclassify(stats["ids"])  # neue Einträge sofort einstufen

            if SOURCE_OV not in values["feeds_disabled"]:
                stats, status, ov_errors = fetch_oeffentlichevergabe(client, values.get("ov_last_day"))
                _add(totals, stats)
                errors.extend(ov_errors)
                feed_status[SOURCE_OV] = track_failures(feed_status.get(SOURCE_OV), status)
                state.update(phase="Klassifizierung", done=0, total=0)
                reclassify(stats["ids"])
            settings.set_many({"feed_status": feed_status})

            details, detail_errors = fetch_details(client)
            errors.extend(detail_errors)

        state.update(phase="Aufräumen", done=0, total=0)
        cleanup_rest()
    except Exception as exc:
        log.exception("Abruf fehlgeschlagen")
        errors.append(str(exc))
    finally:
        with connect() as conn:
            conn.execute(
                "UPDATE fetch_runs SET finished_at = ?, new_items = ?, updated_items = ?, details_fetched = ?, "
                "errors = ? WHERE id = ?",
                (now(), totals["new"], totals["updated"] + totals["merged"], details, _dumps(errors), run_id),
            )
        state.update(running=False, phase="", done=0, total=0)
        _lock.release()
    return True


def start_fetch_in_background(trigger: str) -> bool:
    if _lock.locked():
        return False
    # sofort sichtbar machen, damit die nächste Seite schon den Fortschritt zeigt
    state.update(running=True, phase="Start", done=0, total=0)
    threading.Thread(target=run_fetch, args=(trigger,), daemon=True, name="fetch").start()
    return True


def last_runs(limit: int = 10) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM fetch_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    runs = []
    for row in rows:
        run = dict(row)
        run["errors"] = json.loads(run["errors"] or "[]")
        runs.append(run)
    return runs
