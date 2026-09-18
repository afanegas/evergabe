"""Prüfung von Adressen aus Fremddaten (Dokumente, Quellenlinks aus Feed und Export)."""

from urllib.parse import urlparse

ALLOWED_SCHEMES = ("http", "https")


def safe_url(url: str | None) -> str:
    """Die Adresse, wenn sie http/https ist – sonst "". „javascript:“ oder „data:“ würden im Browser
    beim Anklicken ausgeführt und sind deshalb in Links nicht erlaubt."""
    if not url:
        return ""
    value = url.strip()
    try:
        scheme = urlparse(value).scheme.lower()
    except ValueError:  # kaputte Adresse, z. B. mit ungültigem Port
        return ""
    return value if scheme in ALLOWED_SCHEMES else ""
