import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import ai, backup, classify, config, cpv, fetcher, mailer, scheduler, settings, tenders
from .db import init_db, now
from .categories import CATEGORY_LABELS, CATEGORY_SHORT, REGION_LABELS, SOURCE_LABELS, SOURCE_OV
from .sources.berlin import FEEDS, Feed
from .urls import safe_url

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
    else:
        fetcher.reclassify_pending()  # nach einem abgebrochenen Abruf Liegengebliebenes nachholen
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


# Die Seite lädt ausschließlich eigene Dateien: keine fremden Skripte, keine eingebetteten Skripte,
# kein Einbinden in fremde Seiten. Damit bleibt ein Link mit „javascript:“ aus Fremddaten wirkungslos.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
    "script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


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


def cpv_title(item: dict, code: str, rule_labels: dict[str, str] | None = None) -> str:
    """Bezeichnung zu einem CPV-Code – überall dort, wo ein Code angezeigt wird, in dieser Reihenfolge:
    Angabe der Quelle am Eintrag, eigene Bezeichnung aus der CPV-Liste, offizieller CPV-Katalog und
    zuletzt die Bezeichnung eines kürzeren Listen-Codes, der diesen Code abdeckt."""
    wanted = classify.cpv_digits(code)
    for entry in item.get("cpv_codes") or []:
        if entry.get("label") and classify.cpv_digits(entry.get("code", "")) == wanted:
            return entry["label"]
    labels = cpv_rule_labels() if rule_labels is None else rule_labels
    if labels.get(code):
        return labels[code]
    amtlich = cpv.title(code)
    if amtlich:
        return amtlich
    for rule_code in classify.match_cpv([code], list(labels)):
        if labels.get(rule_code):
            return labels[rule_code]
    return ""


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


templates.env.filters.update(dt=format_dt, days_left=days_left, highlight=highlight, safe_url=safe_url)
templates.env.globals.update(
    CATEGORY_LABELS=CATEGORY_LABELS,
    CATEGORY_SHORT=CATEGORY_SHORT,
    REGION_LABELS=REGION_LABELS,
    SOURCE_LABELS=SOURCE_LABELS,
    SEARCH_FIELD_LABELS=tenders.SEARCH_FIELD_LABELS,
    TREFFER_FILTERS=tenders.TREFFER_FILTERS,
    UNLABELLED_CATEGORIES=tenders.UNLABELLED_CATEGORIES,
    fetch_state=fetcher.state,
    reclassify_state=fetcher.reclassify_state,
    cpv_code_hit=cpv_code_hit,
    cpv_rule_labels=cpv_rule_labels,
    cpv_title=cpv_title,
    cpv_katalog=cpv.title,
    source_warnings=source_warnings,
    run_duration=run_duration,
    asset=asset,
    ai_open=ai.open_count,
)


# Steuerzeichen werden von Browsern aus Adressen entfernt – „/\t/fremd.example“ würde so zu „//fremd.example“
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _safe_next(url: str | None, default: str = "/") -> str:
    r"""Nur Ziele innerhalb der App. Neben „//fremd.example“ deuten Browser auch „/\fremd.example“
    als fremde Adresse (WHATWG-URL: Rückwärtsschrägstrich zählt wie Schrägstrich)."""
    if not url or _CONTROL_CHARS.search(url):
        return default
    if url.startswith("/") and not url.startswith(("//", "/\\")):
        return url
    return default


def _redirect(url: str, **params) -> RedirectResponse:
    if params:
        path, hash_sign, fragment = url.partition("#")  # Parameter vor die Sprungmarke setzen
        url = path + ("&" if "?" in path else "?") + urlencode(params) + hash_sign + fragment
    return RedirectResponse(url, status_code=303)


# ---------- Liste ----------

def _filters_from(params: dict, default_status: str = "interessant", default_neu: bool = True) -> tenders.Filters:
    """Filter aus den Parametern der Seite. `neu` ist im Ausschreibungsbereich voreingestellt: Das Formular
    schickt darum immer einen Wert mit (0 oder 1), sonst wäre „Haken raus“ nicht von „nichts gewählt“
    zu unterscheiden."""
    # Markierungen sind kombinierbar und kommen als eigene Felder (t_ki, t_wichtig …). Frühere Adressen
    # mit einem einzelnen „treffer“-Wert (Lesezeichen, alte Links) gelten weiter.
    treffer = tuple(key for key in tenders.TREFFER_FILTERS if params.get(f"t_{key}"))
    einzeln = params.get("treffer", "")
    if einzeln in tenders.TREFFER_FILTERS and einzeln not in treffer:
        treffer += (einzeln,)
    niedrig = params.get("niedrig", "")
    if einzeln == "niedrig":
        niedrig = "nur"
    return tenders.Filters(
        status=params.get("status", default_status),
        kategorie=params.get("kategorie", ""),
        quelle=params.get("quelle", ""),
        q=params.get("q", "").strip(),
        suchfeld=params.get("suchfeld", ""),
        vergabestelle=params.get("vergabestelle", "").strip(),
        treffer=treffer,
        niedrig=niedrig if niedrig in ("nur", "ohne") else "",
        frist=params.get("frist", ""),
        neu=params.get("neu", "1" if default_neu else "") == "1",
        sort=params.get("sort", "neueste"),
    )


def _limit(params: dict, key: str) -> int:
    """Wie viele Einträge eine Gruppe zeigt – mindestens eine Seite, höchstens MAX_PAGE_SIZE."""
    try:
        wanted = int(params.get(f"anzahl_{key}", config.PAGE_SIZE))
    except ValueError:
        return config.PAGE_SIZE
    return min(max(wanted, config.PAGE_SIZE), config.MAX_PAGE_SIZE)


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
        "view_params": tab_params,
        "favorites_only": page == FAVORITES_PAGE,
        "current_url": page + (f"?{urlencode(params)}" if params else ""),
        "show_seen_url": page + "?" + urlencode(
            {**{k: v for k, v in params.items() if not k.startswith("anzahl_")}, "status": filters.status, "neu": "0"}
        ),
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
            new_total=sum(r["new"] for r in regions),
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
    filters = _filters_from(params, default_status="alle", default_neu=False)
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
            new_total=sum(b["new"] for b in blocks),
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
    _load_group(_filters_from(params, default_status="alle", default_neu=False), params, g,
                favorite_pattern=ag, page=FAVORITES_PAGE)
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
async def mark_view_seen(request: Request):
    """Nur die Einträge der abgeschickten Ansicht als gesehen markieren – das Formular schickt die
    Filter der Seite mit, damit andere Reiter, Regionen und Gruppen unberührt bleiben."""
    form = await request.form()
    params = {k: v for k, v in form.items() if k not in ("next", "favoriten")}
    favorites_only = bool(form.get("favoriten"))
    filters = _filters_from(params, default_status="alle" if favorites_only else "interessant",
                            default_neu=not favorites_only)
    count = tenders.mark_seen_view(filters, favorites_only=favorites_only)
    meldung = "1 Eintrag als gesehen markiert" if count == 1 else f"{count} Einträge als gesehen markiert"
    return _redirect(_safe_next(form.get("next")), meldung=meldung)


# ---------- Detail ----------

# ---------- KI-Auswahl ----------

AI_PAGE = "/ki"


def _ai_context(request: Request, **extra) -> HTMLResponse:
    values = settings.get_all()
    items = ai.pending(values=values) if values["ai_enabled"] else []
    context = {
        "values": values,
        "items": items,
        "prompt": ai.build_prompt(items, values["ai_profile"]) if items else "",
        "ai_status": ai.status(values),
        "judged": ai.recent(),
        "errors": [],
        "meldung": "",
        "antwort": "",
    }
    context.update(extra)
    return templates.TemplateResponse(request, "ki.html", context)


@app.get(AI_PAGE, response_class=HTMLResponse)
def ai_view(request: Request, meldung: str = ""):
    return _ai_context(request, meldung=meldung)


@app.post(AI_PAGE + "/aktualisieren")
def ai_refresh_queue():
    queued = ai.mark_candidates()
    return _redirect(AI_PAGE, meldung=(
        f"Warteschlange aktualisiert – {queued} Einträge dazugekommen." if queued
        else "Warteschlange aktualisiert – es wartet nichts Neues."
    ))


@app.post(AI_PAGE + "/ergebnis", response_class=HTMLResponse)
async def ai_apply(request: Request):
    """Die eingefügte Antwort auswerten und die Urteile speichern."""
    form = await request.form()
    antwort = form.get("antwort") or ""
    entries, errors = ai.parse_answer(antwort)
    if not entries:
        return _ai_context(request, errors=errors or ["Die Antwort enthielt keine Urteile."], antwort=antwort)
    result = ai.apply_results(entries)
    fetcher.reclassify(result["saved"])  # erst jetzt wirkt das Urteil auf Markierung bzw. Einstufung
    if result["unknown"]:
        errors.append("Übersprungen, weil es diese Nummern nicht gibt: "
                      + ", ".join(str(i) for i in result["unknown"]))
    count = len(result["saved"])
    meldung = f"{count} Urteil{'e' if count != 1 else ''} gespeichert."
    if errors:
        return _ai_context(request, errors=errors, meldung=meldung)
    return _redirect(AI_PAGE, meldung=meldung)


# ---------- Schnittstelle für den KI-Worker (Stufe 2) ----------
#
# Dieselben Funktionen wie die Seite /ki, nur ohne Formular: Der Worker holt die offenen Einträge
# samt fertigem Prompt und schickt die Antwort zurück. Ausgewertet wird sie mit derselben Prüfung
# wie beim Einfügen von Hand – es gibt keinen zweiten Weg mit eigenen Fehlern.

def _check_ai_token(request: Request) -> None:
    """Ohne Token in der .env gibt es die Schnittstelle nicht (404 statt 401, damit sie sich auch
    nicht verrät). Mit Token muss er genau stimmen."""
    if not config.AI_TOKEN:
        raise HTTPException(404, "Schnittstelle nicht eingerichtet")
    if not secrets.compare_digest(request.headers.get("x-evc-token", ""), config.AI_TOKEN):
        raise HTTPException(401, "Token fehlt oder stimmt nicht")


@app.get("/api/ki/offen")
def ai_api_pending(request: Request, limit: int | None = None):
    _check_ai_token(request)
    values = settings.get_all()
    if not values["ai_enabled"]:
        return JSONResponse({"aktiv": False, "offen": 0, "eintraege": [], "prompt": ""})
    if limit is not None:
        limit = min(max(limit, 1), config.AI_MAX_PER_REQUEST)
    items = ai.pending(limit=limit, values=values)
    return JSONResponse({
        "aktiv": True,
        "modus": values["ai_mode"],
        "offen": ai.open_count(values),
        "eintraege": [{"id": item["id"], "titel": item["title"]} for item in items],
        "prompt": ai.build_prompt(items, values["ai_profile"]) if items else "",
    })


@app.post("/api/ki/ergebnis")
async def ai_api_results(request: Request):
    """Nimmt die Antwort der KI entgegen – entweder als Rohtext ({"antwort": "…"}, so wie Claude sie
    liefert) oder direkt als Liste von Urteilen."""
    _check_ai_token(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Der Rumpf der Anfrage ist kein gültiges JSON")
    modell = "worker"
    if isinstance(body, dict) and "antwort" in body:
        roh = str(body["antwort"])
        # Woher das Urteil stammt, steht später auf der Detailseite
        modell = " ".join(str(body.get("modell") or "worker").split())[:40] or "worker"
    elif isinstance(body, list):
        roh = json.dumps(body, ensure_ascii=False)
    else:
        raise HTTPException(400, 'Erwartet wird {"antwort": "…"} oder eine Liste von Urteilen')

    entries, errors = ai.parse_answer(roh)
    result = ai.apply_results(entries, model=modell) if entries else {"saved": [], "unknown": []}
    if result["saved"]:
        fetcher.reclassify(result["saved"])
    return JSONResponse({
        "gespeichert": len(result["saved"]),
        "ids": result["saved"],
        "unbekannt": result["unknown"],
        "fehler": errors,
        "offen": ai.open_count(),
    }, status_code=200 if entries else 400)


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
    """Laufender Abruf (oberste Ebene, wie bisher) und laufende Neu-Einstufung."""
    return JSONResponse({**fetcher.state, "reclassify": fetcher.reclassify_state})


@app.get("/api/cpv")
def cpv_suggestions(q: str = ""):
    """Vorschläge für das CPV-Feld der Einstellungen: Code und amtliche Bezeichnung gehören zusammen."""
    return JSONResponse(cpv.search(q))


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
        "ai_status": ai.status(values),
        "authority_names": tenders.overview()["authorities"],
        "rules_dirty": False,
        "times_text": ", ".join(values["schedule_times"]),
        "gap_warning": settings.gap_warning(values),
        "next_runs": scheduler.next_runs(),
        "runs": fetcher.last_runs(),
        "smtp_summary": mailer.smtp_summary(),
        "mail_recipients": mailer.parse_recipients(values["mail_to"])[0],
        "mail_next_run": scheduler.next_run("mail"),
        "mail_lookback_days": config.MAIL_MAX_LOOKBACK_DAYS,
        "mail_max_age_days": config.MAIL_MAX_AGE_DAYS,
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
    fetcher.start_reclassify_in_background("regeln")
    return _redirect(
        "/einstellungen",
        meldung="Gespeichert. Die Einträge werden im Hintergrund neu eingestuft – das dauert einen Moment.",
    )


def _ai_classification_key(values: dict) -> tuple:
    """Die beiden Werte, die auf die Einstufung wirken. Profil, Regionen, Obergrenzen und Höchstalter
    ändern nur, *was* bewertet wird – dafür muss nichts neu klassifiziert werden (das dauert bei
    vielen Einträgen eine Weile)."""
    return (bool(values["ai_enabled"]) and bool((values["ai_profile"] or "").strip()), values["ai_mode"])


@app.post("/einstellungen/ki")
async def save_ai(request: Request):
    form = await request.form()
    values, errors = settings.parse_ai_form(form)
    if errors:
        # Eingaben behalten, damit das Profil nicht verloren geht
        return _settings_context(request, errors=errors, values={**settings.get_all(), **values})
    vorher = settings.get_all()
    settings.set_many(values)
    neu_einstufen = _ai_classification_key(vorher) != _ai_classification_key({**vorher, **values})
    if neu_einstufen:
        fetcher.start_reclassify_in_background("ki-einstellungen")
    queued = ai.mark_candidates()
    if not values["ai_enabled"]:
        meldung = "KI-Einstellungen gespeichert – die KI-Auswahl ist ausgeschaltet."
    else:
        meldung = f"KI-Einstellungen gespeichert – {queued} Einträge warten auf eine Bewertung."
    if neu_einstufen:
        meldung += " Die Einträge werden im Hintergrund neu eingestuft."
    return _redirect("/einstellungen#ki", meldung=meldung)


@app.post("/einstellungen/ki/neu-bewerten")
def ai_requeue():
    """Alle Urteile verwerfen, die nicht zum aktuellen Text und Profil passen, und neu einreihen."""
    queued = ai.requeue_stale()
    return _redirect("/einstellungen#ki", meldung=(
        f"{queued} Einträge warten auf eine neue Bewertung." if queued
        else "Alle Urteile sind aktuell – es wurde nichts neu eingereiht."
    ))


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
    wer = "1 Empfänger" if len(recipients) == 1 else f"{len(recipients)} Empfänger"
    return _redirect("/einstellungen#mail", meldung=f"E-Mail-Einstellungen gespeichert ({wer})." + hint)


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
