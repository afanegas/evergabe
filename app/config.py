import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("EVC_DATA_DIR", BASE_DIR.parent / "data"))
DB_PATH = DATA_DIR / "ev-checker.db"

TIMEZONE = "Europe/Berlin"
USER_AGENT = "eV-Checker/0.1 (privater Ausschreibungs-Monitor)"
HTTP_TIMEOUT_SECONDS = 30.0

# Pause zwischen zwei Detailseiten-Abrufen, um die Server zu schonen
DETAIL_DELAY_SECONDS = float(os.environ.get("EVC_DETAIL_DELAY", "1.0"))
DETAIL_MAX_ATTEMPTS = 3

# Für Tests: kein Zeitplan und kein automatischer Abruf beim Start
SCHEDULER_ENABLED = os.environ.get("EVC_DISABLE_SCHEDULER") != "1"

# Die Feeds enthalten nur die letzten 50 Einträge (Bekanntmachungen ≈ 5 Tage).
# Größere Abstände zwischen zwei Abrufen können Einträge verlieren.
MAX_SAFE_GAP_HOURS = 48

PAGE_SIZE = 50

# oeffentlichevergabe.de: Tagesexporte, frühestens für gestern
OV_BACKFILL_DAYS = 7  # beim ersten Abruf so viele Tage rückwirkend
OV_MAX_DAYS_PER_RUN = 31  # nach längerer Pause höchstens so viele Tage auf einmal nachholen
OV_TIMEOUT_SECONDS = 180.0

# Warnung anzeigen, wenn eine Quelle so oft hintereinander nicht abgerufen werden konnte
SOURCE_FAILURE_WARN = 2

# Nicht interessante Einträge der Region „Rest“ ohne manuelle Einstufung/Notiz werden gelöscht
REST_RETENTION_DAYS = 90

# ---------- Betrieb (Docker/Server) – alle Werte kommen aus Umgebungsvariablen bzw. der .env ----------

# Öffentliche Adresse der App, z. B. https://evchecker.example.org – für Links in E-Mails
BASE_URL = os.environ.get("EVC_BASE_URL", "").rstrip("/")

# Tägliche Datensicherung der SQLite-Datei nach DATA_DIR/backups
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_TIME = os.environ.get("EVC_BACKUP_TIME") or "03:00"
BACKUP_KEEP = int(os.environ.get("EVC_BACKUP_KEEP") or 14)

# E-Mail-Versand (Zugangsdaten nur über Umgebungsvariablen, nie in der Datenbank)
SMTP_HOST = os.environ.get("EVC_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("EVC_SMTP_PORT") or 587)
SMTP_SECURITY = (os.environ.get("EVC_SMTP_SECURITY") or "starttls").lower()  # starttls | ssl | none
SMTP_USER = os.environ.get("EVC_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("EVC_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("EVC_SMTP_FROM", "") or SMTP_USER
MAIL_MAX_ITEMS_PER_REGION = 40  # längere Listen werden in der Mail gekürzt („… und N weitere“)
# Die Mail berichtet über alles, was seit der letzten Mail dazugekommen ist – höchstens aber über so viele Tage.
# Sonst kommt nach einer Pause (Mail aus, Versand fehlgeschlagen, tagelang nichts Interessantes) eine Riesen-Mail.
MAIL_MAX_LOOKBACK_DAYS = 3
# Bekanntmachungen, die bei ihrer Veröffentlichung schon älter als so viele Tage waren, kommen nicht in die Mail.
# Sie werden trotzdem gespeichert und stehen in der App – nur „neu“ sind sie nicht (Nachladen alter Tage von
# oeffentlichevergabe.de, siehe OV_BACKFILL_DAYS / OV_MAX_DAYS_PER_RUN).
MAIL_MAX_AGE_DAYS = 14
