import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import backup, classify, config, fetcher, mailer, scheduler, settings, tenders
from .db import init_db, now
from .categories import CATEGORY_LABELS, CATEGORY_SHORT, REGION_LABELS, SOURCE_LABELS, SOURCE_OV
from .sources.berlin import FEEDS, Feed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings.seed_rules()
    seeded = settings.seed_priority_rules()
    seeded = settings.seed_lowprio_rules() or seeded
    seeded = settings.migrate_rule_scopes() or seeded
    seeded = settings.apply_rule_additions() or seeded
    if seeded:
        fetcher.reclassify_all()  # bestehende Einträge mit neu angelegten Stichwort-Listen markieren
    if config.SCHEDULER_ENABLED:
        scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="eV-Checker", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=config.BASE_DIR / "static"), name="static")


@app.middleware("http")
async def reject_cross_site_posts(request: Request, call_next):
    """Schutz gegen fremde Seiten, die Formulare an die App schicken (CSRF). Wichtig hinter BasicAuth,
    weil der Browser die Zugangsdaten sonst automatisch mitsendet."""
    if request.method == "POST":
        fetch_site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            return HTMLResponse("Anfrage von fremder Seite abgelehnt", status_code=403)
        if origin and origin != "null" and origin.split("://", 1)[-1] != host:
            return HTMLResponse("Anfrage von fremder Seite abgelehnt", status_code=403)
    return await call_next(request)


templates = Jinja2Templates(directory=config.BASE_DIR / "templates")


# ---------- Template-Helfer ----------

def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_dt(value: str | None, with_time: bool = True) -> str:
    dt = _parse_dt(value)
    if dt is None:
        return value or ""
    if with_time and "T" in value and not value.endswith("T00:00"):
        return dt.strftime("%d.%m.%Y %H:%M")
    return dt.strftime("%d.%m.%Y")


def days_left(value: str | None) -> int | None:
    dt = _parse_dt(value)
    if dt is None:
        return None
    return (dt.date() - date.fromisoformat(now()[:10])).days


def highlight(text: str | None, terms: list[str]) -> Markup:
    if not text:
        return Markup("")
    patterns = [p.pattern for t in terms for p in classify.term_patterns(t)]
    escaped = str(escape(text))
    if not patterns:
        return Markup(escaped.replace("\n", "<br>"))
    combined = re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)
    # Auf den unescapten Text matchen, Teile einzeln escapen
    out, pos = [], 0
    for m in combined.finditer(text):
        out.append(str(escape(text[pos:m.start()])))
        out.append(f"<mark>{escape(m.group(0))}</mark>")
        pos = m.end()
    out.append(str(escape(text[pos:])))
    return Markup("".join(out).replace("\n", "<br>"))


def cpv_code_hit(code: str, rule_matches: list[str]) -> list[str]:
    """Welche Listen-Codes decken diesen CPV-Code des Eintrags ab?"""
    return classify.match_cpv([code], rule_matches)


def cpv_rule_labels() -> dict[str, str]:
    """Bezeichnungen aus der CPV-Liste der Einstellungen (Code -> Bezeichnung), für die Treffer-Spalte."""
    return {r["value"]: r["label"] for r in settings.list_rules("cpv") if r["label"]}


def source_warnings() -> list[dict]:
    """Aktive Quellen, die mehrmals hintereinander nicht abgerufen werden konnten (Banner auf allen Seiten)."""
    values = settings.get_all()
    labels = {feed.key: f"Vergabeplattform Berlin – {feed.label}" for feed in FEEDS}
    labels[SOURCE_OV] = "oeffentlichevergabe.de"
    warnings = []
    for key, label in labels.items():
        status = values["feed_status"].get(key) or {}
        if key not in values["feeds_disabled"] and status.get("failures", 0) >= config.SOURCE_FAILURE_WARN:
            warnings.append({
                "label": label,
                "failures": status["failures"],
                "since": status.get("failing_since"),
                "error": status.get("error"),
            })
    return warnings


def run_duration(run: dict) -> str:
    seconds = int((datetime.fromisoformat(run["finished_at"]) - datetime.fromisoformat(run["started_at"])).total_seconds())
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


def asset(path: str) -> str:
    """Statische Datei mit Änderungszeit als Version, damit der Browser nach Updates nicht die alte Fassung nutzt."""
    try:
        version = int((config.BASE_DIR / "static" / path).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{path}?v={version}"


templates.env.filters.update(dt=format_dt, days_left=days_left, highlight=highlight)
templates.env.globals.update(
    CATEGORY_LABELS=CATEGORY_LABELS,
    CATEGORY_SHORT=CATEGORY_SHORT,
    REGION_LABELS=REGION_LABELS,
    SOURCE_LABELS=SOURCE_LABELS,
    fetch_state=fetcher.state,
    cpv_code_hit=cpv_code_hit,
    cpv_rule_labels=cpv_rule_labels,
    source_warnings=source_warnings,
    run_duration=run_duration,
    asset=asset,
)


def _safe_next(url: str | None, default: str = "/") -> str:
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return default


def _redirect(url: str, **params) -> RedirectResponse:
    if params:
        path, hash_sign, fragment = url.partition("#")  # Parameter vor die Sprungmarke setzen
        url = path + ("&" if "?" in path else "?") + urlencode(params) + hash_sign + fragment
    return RedirectResponse(url, status_code=303)


# ---------- Liste ----------

def _filters_from(params: dict, default_status: str = "interessant") -> tenders.Filters:
    return tenders.Filters(
        status=params.get("status", default_status),
        kategorie=params.get("kategorie", ""),
        quelle=params.get("quelle", ""),
        q=params.get("q", "").strip(),
        vergabestelle=params.get("vergabestelle", "").strip(),
        treffer=params.get("treffer", ""),
        frist=params.get("frist", ""),
        neu=bool(params.get("neu")),
        sort=params.get("sort", "neueste"),
    )


def _limit(params: dict, key: str) -> int:
    try:
        return max(int(params.get(f"anzahl_{key}", config.PAGE_SIZE)), config.PAGE_SIZE)
    except ValueError:
        return config.PAGE_SIZE


def _load_group(
    filters: tenders.Filters, params: dict, g: dict, region_key: str = "", favorite_pattern: str = "", page: str = "/"
) -> None:
    """Einträge einer Gruppe laden und Links für „weitere anzeigen“ setzen."""
    limit = _limit(params, g["key"])
    g.update(tenders.search(filters, region_key, g["group"].categories, limit, favorite_pattern=favorite_pattern))
    g["loaded"] = True
    key = g["key"]
    more = {**params, f"anzahl_{key}": limit + config.PAGE_SIZE}
    g["more_url"] = f"{page}?{urlencode(more)}#gruppe-{key}"


def _prepare_groups(filters: tenders.Filters, params: dict, block: dict, block_open: bool, src: str, **load) -> None:
    """Auf-/Zugeklappt-Zustand setzen; sichtbare Gruppen sofort laden, alle anderen beim Aufklappen (JS)."""
    for g in block["groups"]:
        g["force_open"] = bool(filters.kategorie) or f"anzahl_{g['key']}" in params
        g["open"] = g["group"].open_by_default or g["force_open"]
        g["src"] = src.format(group=g["group"].key)
        g["loaded"] = False
        if block_open and g["open"] and g["total"]:
            _load_group(filters, params, g, **load)


def _page_context(params: dict, filters: tenders.Filters, page: str, **extra) -> dict:
    tab_params = {k: v for k, v in params.items() if k != "status" and not k.startswith("anzahl_")}
    context = {
        "filters": filters,
        "overview": tenders.overview(),
        "page_url": page,
        "base_query": urlencode(tab_params),
        "current_url": page + (f"?{urlencode(params)}" if params else ""),
        "show_expired_url": page + "?" + urlencode(
            {**{k: v for k, v in params.items() if not k.startswith("anzahl_")}, "status": filters.status, "frist": "alle"}
        ),
    }
    context.update(extra)
    return context


@app.get("/", response_class=HTMLResponse)
def list_view(request: Request, meldung: str = ""):
    params = {k: v for k, v in request.query_params.items() if k != "meldung"}
    filters = _filters_from(params)
    base_query = urlencode({k: v for k, v in params.items() if not k.startswith("anzahl_")})

    regions = tenders.structure(filters)
    for r in regions:
        region = r["region"]
        forced = bool(filters.kategorie) or any(f"anzahl_{g['key']}" in params for g in r["groups"])
        r["key"] = region.key
        r["force_open"] = forced
        r["open"] = region.open_by_default or forced
        src = f"/gruppe/{region.key}/{{group}}" + (f"?{base_query}" if base_query else "")
        _prepare_groups(filters, params, r, r["open"], src, region_key=region.key)

    return templates.TemplateResponse(
        request,
        "list.html",
        _page_context(
            params, filters, "/",
            regions=regions,
            total=sum(r["total"] for r in regions),
            counts=tenders.status_counts(filters),
            expired_hidden=tenders.expired_hidden(filters),
            meldung=meldung,
        ),
    )


@app.get("/gruppe/{region_key}/{group_key}", response_class=HTMLResponse)
def group_partial(request: Request, region_key: str, group_key: str):
    """Inhalt einer zugeklappten Gruppe, nachgeladen beim Aufklappen."""
    region = tenders.REGIONS_BY_KEY.get(region_key)
    group = tenders.GROUPS_BY_KEY.get(group_key)
    if region is None or group is None:
        raise HTTPException(404, "Gruppe nicht gefunden")
    params = dict(request.query_params)
    g = {"group": group, "key": f"{region.key}-{group.key}"}
    _load_group(_filters_from(params), params, g, region_key=region.key)
    return templates.TemplateResponse(
        request,
        "_group_body.html",
        {"g": g, "current_url": "/" + (f"?{urlencode(params)}" if params else "")},
    )


# ---------- Beobachtete Auftraggeber ----------

FAVORITES_PAGE = "/auftraggeber"


@app.get(FAVORITES_PAGE, response_class=HTMLResponse)
def favorites_view(request: Request, meldung: str = ""):
    params = {k: v for k, v in request.query_params.items() if k != "meldung"}
    filters = _filters_from(params, default_status="alle")
    base_query = urlencode({k: v for k, v in params.items() if not k.startswith("anzahl_")})
    patterns = settings.load_favorites()

    blocks = tenders.favorites_structure(filters, patterns)
    for b in blocks:
        forced = bool(filters.kategorie) or any(f"anzahl_{g['key']}" in params for g in b["groups"])
        b["force_open"] = forced
        b["open"] = True
        query = urlencode({**{k: v for k, v in params.items() if not k.startswith("anzahl_")}, "ag": b["pattern"]})
        src = f"{FAVORITES_PAGE}/gruppe/{{group}}?{query}"
        _prepare_groups(filters, params, b, True, src, favorite_pattern=b["pattern"], page=FAVORITES_PAGE)

    return templates.TemplateResponse(
        request,
        "favorites.html",
        _page_context(
            params, filters, FAVORITES_PAGE,
            blocks=blocks,
            total=tenders.status_counts(filters, favorites_only=True)[filters.status if filters.status in ("interessant", "nicht_interessant") else "alle"],
            counts=tenders.status_counts(filters, favorites_only=True),
            expired_hidden=tenders.expired_hidden(filters, favorites_only=True),
            meldung=meldung,
            has_inactive=any(not r["active"] for r in settings.list_rules("authority")),
        ),
    )


@app.get(FAVORITES_PAGE + "/gruppe/{group_key}", response_class=HTMLResponse)
def favorites_group_partial(request: Request, group_key: str, ag: str = ""):
    group = tenders.GROUPS_BY_KEY.get(group_key)
    if group is None or ag not in settings.load_favorites():
        raise HTTPException(404, "Gruppe nicht gefunden")
    params = {k: v for k, v in request.query_params.items() if k != "ag"}
    g = {"group": group, "key": f"{tenders.favorite_key(ag)}-{group.key}"}
    _load_group(_filters_from(params, default_status="alle"), params, g, favorite_pattern=ag, page=FAVORITES_PAGE)
    return templates.TemplateResponse(
        request,
        "_group_body.html",
        {"g": g, "current_url": FAVORITES_PAGE + (f"?{urlencode(params)}" if params else "")},
    )


@app.post(FAVORITES_PAGE + "/beobachten")
def watch_authority(name: str = Form(...), next: str = Form(FAVORITES_PAGE)):
    added = settings.watch_authority(name)
    fetcher.refresh_favorites()
    meldung = f"„{name}“ wird beobachtet." if added else f"„{name}“ wurde bereits beobachtet."
    return _redirect(_safe_next(next, FAVORITES_PAGE), meldung=meldung)


@app.post(FAVORITES_PAGE + "/entfernen")
async def unwatch_authority(request: Request):
    form = await request.form()
    patterns = [p for p in form.getlist("pattern") if p]
    settings.unwatch_authorities(patterns)
    fetcher.refresh_favorites()
    next_url = _safe_next(form.get("next"), FAVORITES_PAGE)
    names = ", ".join(f"„{p}“" for p in patterns)
    return _redirect(next_url, meldung=f"Beobachtung von {names} beendet.")


@app.post("/gesehen")
def mark_all_seen(next: str = Form("/")):
    tenders.mark_seen()
    return _redirect(_safe_next(next))


# ---------- Detail ----------

@app.get("/eintrag/{tender_id}", response_class=HTMLResponse)
def detail_view(request: Request, tender_id: int, zurueck: str = "/", meldung: str = ""):
    item = tenders.get(tender_id)
    if item is None:
        raise HTTPException(404, "Eintrag nicht gefunden")
    was_new = item["seen_at"] is None
    tenders.mark_seen([tender_id])
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "item": item,
            "was_new": was_new,
            "back_url": _safe_next(zurueck),
            "meldung": meldung,
            "extra_fields": {**item["feed_fields"], **item["detail_fields"]},
        },
    )


@app.post("/eintrag/{tender_id}/status")
def update_status(tender_id: int, status: str = Form(...), next: str = Form("/")):
    if status not in ("auto", "interessant", "nicht_interessant"):
        raise HTTPException(400, "Ungültiger Status")
    tenders.set_manual_status(tender_id, None if status == "auto" else status)
    fetcher.reclassify([tender_id])  # Markierung „niedrig“ hängt von der (manuellen) Einstufung ab
    return _redirect(_safe_next(next))


@app.post("/eintrag/{tender_id}/notiz")
def update_note(tender_id: int, note: str = Form(""), next: str = Form("/")):
    tenders.set_note(tender_id, note)
    return _redirect(_safe_next(next), meldung="Notiz gespeichert")


# ---------- Abruf ----------

@app.post("/abrufen")
def trigger_fetch(next: str = Form("/")):
    started = fetcher.start_fetch_in_background("manuell")
    return _redirect(_safe_next(next), meldung="Abruf gestartet" if started else "Es läuft bereits ein Abruf")


@app.get("/health")
def health():
    """Für den Docker-Healthcheck: App antwortet und die Datenbank ist lesbar."""
    from .db import connect

    with connect() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/api/status")
def fetch_status():
    return JSONResponse(fetcher.state)


# ---------- Einstellungen ----------

def _settings_context(request: Request, **extra) -> HTMLResponse:
    values = settings.get_all()
    counts = tenders.source_counts()
    ov = Feed(SOURCE_OV, "oeffentlichevergabe.de – alle Bekanntmachungen Deutschland (täglich, Vortag)",
              "https://oeffentlichevergabe.de/api/notice-exports?pubDay=…&format=eforms.zip")
    feeds = [
        {
            "feed": feed,
            "group": "Vergabeplattform Berlin" if feed in FEEDS else "Bund / Länder / Kommunen",
            "enabled": feed.key not in values["feeds_disabled"],
            "status": values["feed_status"].get(feed.key),
            "stored": counts.get(f"quelle:{SOURCE_OV}" if feed is ov else feed.key, 0),
        }
        for feed in [*FEEDS, ov]
    ]
    context = {
        "values": values,
        "feeds": feeds,
        "retention_days": config.REST_RETENTION_DAYS,
        "rules": settings.all_rules(),
        "authority_names": tenders.overview()["authorities"],
        "rules_dirty": False,
        "times_text": ", ".join(values["schedule_times"]),
        "gap_warning": settings.gap_warning(values),
        "next_runs": scheduler.next_runs(),
        "runs": fetcher.last_runs(),
        "smtp_summary": mailer.smtp_summary(),
        "mail_next_run": scheduler.next_run("mail"),
        "backups": backup.list_backups(),
        "backup_next_run": scheduler.next_run("backup"),
        "backup_keep": config.BACKUP_KEEP,
        "base_url": config.BASE_URL,
        "errors": [],
        "meldung": "",
    }
    context.update(extra)
    return templates.TemplateResponse(request, "settings.html", context)


@app.get("/einstellungen", response_class=HTMLResponse)
def settings_view(request: Request, meldung: str = ""):
    return _settings_context(request, meldung=meldung)


@app.post("/einstellungen/klassifizierung")
async def save_classification(request: Request):
    form = await request.form()
    if not form.get("rules_form"):
        raise HTTPException(400, "Formular unvollständig")
    rules, errors = settings.parse_rules_form(form)
    cpv_enabled, keyword_enabled = bool(form.get("cpv_enabled")), bool(form.get("keyword_enabled"))
    if not cpv_enabled and not keyword_enabled:
        errors.append("Mindestens eine Prüfung (CPV oder Stichwörter) muss aktiv sein.")
    if errors:
        # Eingaben behalten, damit nichts verloren geht
        return _settings_context(request, errors=errors, rules=rules, rules_dirty=True)

    settings.set_many({
        "cpv_enabled": cpv_enabled,
        "keyword_enabled": keyword_enabled,
        "combine": "und" if form.get("combine") == "und" else "oder",
        "priority_conflict": form.get("priority_conflict") if form.get("priority_conflict") in ("niedrig", "beide") else "wichtig",
    })
    settings.replace_rules(rules)
    counts = fetcher.reclassify_all()
    return _redirect(
        "/einstellungen",
        meldung=f"Gespeichert – {counts['gesamt']} Einträge neu klassifiziert, davon {counts['interessant']} interessant.",
    )


@app.post("/einstellungen/feeds")
async def save_feeds(request: Request):
    form = await request.form()
    keys = [feed.key for feed in FEEDS] + [SOURCE_OV]
    enabled = {key for key in keys if form.get(f"feed_{key}")}
    if not enabled:
        return _settings_context(request, errors=["Mindestens ein Feed muss aktiv sein."])
    settings.set_many({"feeds_disabled": [key for key in keys if key not in enabled]})
    return _redirect("/einstellungen", meldung=f"Feeds gespeichert – {len(enabled)} von {len(keys)} aktiv.")


@app.post("/einstellungen/mail")
def save_mail(
    request: Request,
    mail_enabled: str = Form(""),
    mail_to: str = Form(""),
    mail_time: str = Form("07:30"),
):
    recipients, invalid = mailer.parse_recipients(mail_to)
    times, bad_times = settings.parse_times(mail_time)
    errors = [f"Ungültige E-Mail-Adresse: {a}" for a in invalid]
    if bad_times or len(times) != 1:
        errors.append("Bitte genau eine Uhrzeit im Format HH:MM angeben.")
    if mail_enabled and not recipients:
        errors.append("Für den Versand mindestens einen Empfänger eintragen.")
    if errors:
        return _settings_context(request, errors=errors)
    settings.set_many({"mail_enabled": bool(mail_enabled), "mail_to": ", ".join(recipients), "mail_time": times[0]})
    scheduler.apply_schedule()
    hint = "" if mailer.smtp_configured() or not mail_enabled else " Achtung: SMTP ist noch nicht konfiguriert (.env)."
    return _redirect("/einstellungen#mail", meldung="E-Mail-Einstellungen gespeichert." + hint)


@app.post("/einstellungen/mail/test")
def send_test_mail():
    result = mailer.run_digest(test=True)
    if result["sent"]:
        meldung = f"Test-Mail an {', '.join(result['recipients'])} verschickt ({result['count']} Treffer der letzten 24 Stunden)."
    else:
        meldung = f"Test-Mail nicht verschickt: {result['reason']}"
    return _redirect("/einstellungen#mail", meldung=meldung)


@app.post("/einstellungen/backup")
def create_backup_now():
    info = backup.create_backup()
    return _redirect("/einstellungen#backup", meldung=f"Datensicherung erstellt: {info['name']}")


@app.post("/einstellungen/zeitplan")
def save_schedule(
    request: Request,
    schedule_mode: str = Form("zeiten"),
    times_text: str = Form(""),
    schedule_interval_hours: str = Form("12"),
):
    errors = []
    times, bad_times = settings.parse_times(times_text)
    try:
        interval = int(schedule_interval_hours)
    except ValueError:
        interval = 0
    if schedule_mode == "intervall":
        if not 1 <= interval <= 168:
            errors.append("Das Intervall muss zwischen 1 und 168 Stunden liegen.")
    else:
        if bad_times:
            errors.append(f"Ungültige Uhrzeit(en): {', '.join(bad_times)} (Format HH:MM)")
        if not times:
            errors.append("Bitte mindestens eine Uhrzeit angeben.")
    if errors:
        return _settings_context(request, errors=errors, times_text=times_text)

    values = {"schedule_mode": "intervall" if schedule_mode == "intervall" else "zeiten"}
    if values["schedule_mode"] == "intervall":
        values["schedule_interval_hours"] = interval
    else:
        values["schedule_times"] = times
    settings.set_many(values)
    scheduler.apply_schedule()
    return _redirect("/einstellungen", meldung="Zeitplan gespeichert.")
