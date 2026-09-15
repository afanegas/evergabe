"""Zeitplan: Abruf (Uhrzeiten oder Intervall), tägliche E-Mail und nächtliche Datensicherung."""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import backup, config, mailer, settings
from .db import connect, now
from .fetcher import run_fetch, start_fetch_in_background

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

FETCH_PREFIX = "abruf-"


def start() -> None:
    global _scheduler
    _scheduler = BackgroundScheduler(timezone=config.TIMEZONE)
    _scheduler.start()
    apply_schedule()
    catch_up()


def shutdown() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)


def _cron(time_str: str) -> CronTrigger:
    hour, minute = time_str.split(":")
    return CronTrigger(hour=int(hour), minute=int(minute))


def _run_digest() -> None:
    result = mailer.run_digest()
    log.info("Tägliche E-Mail: %s", result)


def _run_backup() -> None:
    try:
        backup.create_backup()
    except Exception:
        log.exception("Datensicherung fehlgeschlagen")


def apply_schedule() -> None:
    if _scheduler is None:
        return
    for job in _scheduler.get_jobs():
        job.remove()
    values = settings.get_all()
    if values["schedule_mode"] == "intervall":
        _scheduler.add_job(
            run_fetch, IntervalTrigger(hours=values["schedule_interval_hours"]), id=f"{FETCH_PREFIX}intervall",
            kwargs={"trigger": "zeitplan"}, coalesce=True, max_instances=1,
        )
    else:
        for time_str in values["schedule_times"]:
            _scheduler.add_job(
                run_fetch, _cron(time_str), id=f"{FETCH_PREFIX}{time_str}",
                kwargs={"trigger": "zeitplan"}, coalesce=True, max_instances=1, misfire_grace_time=3600,
            )
    if values["mail_enabled"]:
        _scheduler.add_job(_run_digest, _cron(values["mail_time"]), id="mail", coalesce=True, max_instances=1,
                           misfire_grace_time=3600)
    _scheduler.add_job(_run_backup, _cron(config.BACKUP_TIME), id="backup", coalesce=True, max_instances=1,
                       misfire_grace_time=6 * 3600)
    log.info("Zeitplan: %s", {job.id: str(job.trigger) for job in _scheduler.get_jobs()})


def next_runs() -> list[datetime]:
    """Nächste automatische Abrufe."""
    if _scheduler is None:
        return []
    return sorted(
        job.next_run_time for job in _scheduler.get_jobs() if job.id.startswith(FETCH_PREFIX) and job.next_run_time
    )


def next_run(job_id: str) -> datetime | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(job_id)
    return job.next_run_time if job else None


def catch_up() -> None:
    """Beim Start nachholen, wenn seit dem letzten Abruf mehr Zeit als der Plan-Abstand vergangen ist
    (z. B. weil der Rechner aus war)."""
    with connect() as conn:
        row = conn.execute("SELECT MAX(started_at) AS last FROM fetch_runs WHERE finished_at IS NOT NULL").fetchone()
    last = datetime.fromisoformat(row["last"]) if row["last"] else None
    gap = timedelta(hours=min(settings.max_gap_hours(settings.get_all()), 24))
    if last is None or datetime.fromisoformat(now()) - last > gap:
        log.info("Letzter Abruf: %s – hole Abruf beim Start nach", last)
        start_fetch_in_background("start")
