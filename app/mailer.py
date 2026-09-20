"""Tägliche E-Mail mit neuen interessanten Treffern.

SMTP-Zugangsdaten kommen aus Umgebungsvariablen (siehe config.py / .env.example).
Empfänger, Uhrzeit und An/Aus werden in der App eingestellt.
Es wird nur verschickt, wenn es seit der letzten Mail neue interessante Einträge oder neue Einträge
beobachteter Auftraggeber gibt (eigener Abschnitt, auch nicht interessante).
"""

import logging
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config, settings
from .categories import CATEGORY_SHORT, REGION_LABELS, SOURCE_LABELS
from .db import connect, now, row_to_dict
from .tenders import REGIONS, attach_sources
from .urls import safe_url

log = logging.getLogger(__name__)

_env = Environment(loader=FileSystemLoader(config.BASE_DIR / "templates"), autoescape=select_autoescape(["html"]))


def _format_dt(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%d.%m.%Y %H:%M") if "T" in value and not value.endswith("T00:00") else dt.strftime("%d.%m.%Y")


_env.filters["dt"] = _format_dt


_env.filters["safe_url"] = safe_url


def smtp_configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_FROM)


def smtp_summary() -> str:
    """Kurzbeschreibung für die Einstellungsseite – ohne Passwort."""
    if not smtp_configured():
        return ""
    user = f" als {config.SMTP_USER}" if config.SMTP_USER else ""
    return f"{config.SMTP_HOST}:{config.SMTP_PORT} ({config.SMTP_SECURITY}){user}, Absender {config.SMTP_FROM}"


def parse_recipients(text: str) -> tuple[list[str], list[str]]:
    recipients, invalid = [], []
    for part in text.replace(";", ",").replace("\n", ",").split(","):
        address = part.strip()
        if not address:
            continue
        local, _, domain = address.partition("@")
        if local and "." in domain and " " not in address:
            if address.lower() not in (r.lower() for r in recipients):
                recipients.append(address)
        else:
            invalid.append(address)
    return recipients, invalid


def collect(since: str) -> list[dict]:
    """Interessante Einträge (inkl. manuell eingestufter), die seit `since` neu hinzugekommen sind."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tenders WHERE first_seen > ? AND COALESCE(manual_status, auto_status) = 'interessant' "
            "ORDER BY priority_level DESC, deadline IS NULL, deadline ASC, id",
            (since,),
        ).fetchall()
        items = [row_to_dict(r) for r in rows]
        attach_sources(conn, items)
    return items


def collect_favorites(since: str) -> list[dict]:
    """Neue Einträge beobachteter Auftraggeber (alle Einstufungen), je Auftraggeber-Muster gruppiert.
    Ein Eintrag, der mehrere Muster trifft, steht nur beim ersten."""
    patterns = settings.load_favorites()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tenders WHERE first_seen > ? AND favorite = 1 "
            "ORDER BY priority_level DESC, deadline IS NULL, deadline ASC, id",
            (since,),
        ).fetchall()
        items = [row_to_dict(r) for r in rows]
        attach_sources(conn, items)
    blocks = {p: [] for p in patterns}
    for item in items:
        first = next((p for p in patterns if p in item["authority_matches"]), None)
        if first is not None:
            blocks[first].append(item)
    return [{"pattern": p, "items": found} for p, found in blocks.items() if found]


def build_message(
    items: list[dict], recipients: list[str], test: bool = False, favorites: list[dict] | None = None
) -> EmailMessage:
    favorites = favorites or []
    favorite_count = sum(len(b["items"]) for b in favorites)
    regions = []
    for region in REGIONS:
        region_items = [i for i in items if i["region"] == region.key]
        if region_items:
            shown = region_items[: config.MAIL_MAX_ITEMS_PER_REGION]
            regions.append({"label": region.label, "items": shown, "more": len(region_items) - len(shown)})

    important = sum(1 for i in items if i["important"])
    if items:
        subject = f"eV-Checker: {len(items)} neue interessante Treffer"
        if important:
            subject += f" ({important} wichtig)"
        if favorite_count:
            subject += f", {favorite_count} von beobachteten Auftraggebern"
    else:
        subject = f"eV-Checker: {favorite_count} neue Einträge beobachteter Auftraggeber"
    if test:
        subject = "[Test] " + subject

    context = {
        "regions": regions,
        "favorites": favorites,
        "favorite_count": favorite_count,
        "total": len(items),
        "important": important,
        "base_url": config.BASE_URL,
        "test": test,
        "CATEGORY_SHORT": CATEGORY_SHORT,
        "REGION_LABELS": REGION_LABELS,
        "SOURCE_LABELS": SOURCE_LABELS,
    }
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("eV-Checker", config.SMTP_FROM))
    message["To"] = ", ".join(recipients)
    message["Message-ID"] = make_msgid(domain=config.SMTP_FROM.partition("@")[2] or None)
    message.set_content(_env.get_template("email_digest.txt").render(context))
    message.add_alternative(_env.get_template("email_digest.html").render(context), subtype="html")
    return message


def send(message: EmailMessage) -> None:
    if not smtp_configured():
        raise RuntimeError("SMTP ist nicht konfiguriert (EVC_SMTP_HOST / EVC_SMTP_FROM in der .env)")
    timeout = 30
    if config.SMTP_SECURITY == "ssl":
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=timeout, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=timeout)
    with server:
        if config.SMTP_SECURITY == "starttls":
            server.starttls(context=ssl.create_default_context())
        if config.SMTP_USER:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(message)


def run_digest(test: bool = False) -> dict:
    """Tägliche Mail verschicken. Test: an die eingestellten Empfänger, Treffer der letzten 24 Stunden,
    ohne den Zeitpunkt der letzten Mail zu verändern."""
    values = settings.get_all()
    recipients, _ = parse_recipients(values["mail_to"])
    if not test and not values["mail_enabled"]:
        return {"sent": False, "reason": "E-Mail ist ausgeschaltet"}
    if not recipients:
        return {"sent": False, "reason": "Keine Empfänger eingetragen"}
    if not smtp_configured():
        return {"sent": False, "reason": "SMTP ist nicht konfiguriert"}

    started = now()
    yesterday = (datetime.fromisoformat(started) - timedelta(days=1)).isoformat(timespec="seconds")
    since = yesterday if test else (values["mail_last_sent_at"] or yesterday)
    items = collect(since)
    favorites = collect_favorites(since)
    if not items and not favorites and not test:
        log.info("Tägliche E-Mail: keine neuen Treffer seit %s", since)
        settings.set_many({"mail_last_check": {"at": started, "sent": False, "count": 0, "error": None}})
        return {"sent": False, "reason": "Keine neuen interessanten Treffer", "count": 0}

    try:
        send(build_message(items, recipients, test=test, favorites=favorites))
    except Exception as exc:
        log.exception("E-Mail-Versand fehlgeschlagen")
        settings.set_many({"mail_last_check": {"at": started, "sent": False, "count": len(items), "error": str(exc)[:300]}})
        return {"sent": False, "reason": f"Versand fehlgeschlagen: {exc}", "count": len(items)}

    if not test:
        settings.set_many({"mail_last_sent_at": started})
    settings.set_many({"mail_last_check": {"at": started, "sent": True, "count": len(items), "error": None, "test": test}})
    log.info("E-Mail verschickt an %s: %d Treffer%s", ", ".join(recipients), len(items), " (Test)" if test else "")
    return {"sent": True, "count": len(items), "favorites": sum(len(b["items"]) for b in favorites), "recipients": recipients}
