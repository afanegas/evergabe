#!/usr/bin/env python3
"""KI-Worker für den eV-Checker.

Holt offene Bewertungen bei der App ab, lässt sie von Claude Code beurteilen und schickt die
Urteile zurück – also genau das, was auf der Seite „🤖 KI-Auswahl“ von Hand passiert.

Der Worker kennt weder die Datenbank noch den Code der App. Er spricht nur über zwei Adressen
mit ihr und weist sich mit dem Token aus der .env aus:

    GET  /api/ki/offen?limit=N   offene Einträge und fertiger Prompt
    POST /api/ki/ergebnis        Urteile zurückgeben

Aufruf:

    python worker/ki_worker.py --trocken          Probelauf ohne Claude (antwortet „unklar“)
    python worker/ki_worker.py --runden 1         eine Runde mit Claude
    python worker/ki_worker.py                    bis nichts mehr offen ist

Umgebung: EVC_URL (Standard http://localhost:8765), EVC_AI_TOKEN (nötig),
CLAUDE_BIN (Standard „claude“), CLAUDE_MODEL (Standard claude-sonnet-5-5).

Das Modell ist fest vorgegeben statt „was Claude Code gerade eingestellt hat“: Sonst liefe der
Worker still auf dem teuersten Modell, und die Nutzungsgrenze des Abos wäre schneller erreicht.

Sicherheit: Die Ausschreibungstexte stammen ungeprüft aus dem Netz. Claude läuft deshalb ohne
Werkzeuge und in einem leeren Arbeitsverzeichnis – es gibt nichts, was eine Anweisung in einem
Ausschreibungstext anrichten könnte.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

log = logging.getLogger("ki-worker")

STANDARD_URL = "http://localhost:8765"
STANDARD_MODELL = "claude-sonnet-5-5"
CLAUDE_TIMEOUT_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 120


class WorkerFehler(RuntimeError):
    """Abbruchgrund, der dem Benutzer als Klartext gezeigt wird."""


# ---------- Verbindung zur App ----------

def _anfrage(url: str, token: str, daten=None) -> dict:
    rumpf = json.dumps(daten, ensure_ascii=False).encode("utf-8") if daten is not None else None
    kopf = {"X-EVC-Token": token}
    if rumpf is not None:
        kopf["Content-Type"] = "application/json; charset=utf-8"
    anfrage = urllib.request.Request(url, data=rumpf, headers=kopf, method="POST" if rumpf else "GET")
    try:
        with urllib.request.urlopen(anfrage, timeout=HTTP_TIMEOUT_SECONDS) as antwort:
            return json.loads(antwort.read().decode("utf-8"))
    except urllib.error.HTTPError as fehler:
        text = fehler.read().decode("utf-8", "replace")[:300]
        hinweis = {401: " – stimmt EVC_AI_TOKEN?", 404: " – ist EVC_AI_TOKEN in der .env der App gesetzt?"}
        raise WorkerFehler(f"{url} antwortete {fehler.code}{hinweis.get(fehler.code, '')}: {text}") from None
    except urllib.error.URLError as fehler:
        raise WorkerFehler(f"{url} ist nicht erreichbar: {fehler.reason}") from None
    except json.JSONDecodeError:
        raise WorkerFehler(f"{url} hat kein JSON geliefert") from None


def offene_holen(basis: str, token: str, limit: int | None) -> dict:
    url = f"{basis}/api/ki/offen" + (f"?limit={limit}" if limit else "")
    return _anfrage(url, token)


def urteile_senden(basis: str, token: str, antwort: str, modell: str) -> dict:
    return _anfrage(f"{basis}/api/ki/ergebnis", token, {"antwort": antwort, "modell": modell})


# ---------- Bewertung ----------

def trockenes_urteil(eintraege: list[dict]) -> str:
    """Probelauf: beantwortet jeden Eintrag mit „unklar“, ohne Claude zu fragen. Damit lässt sich
    die ganze Strecke prüfen – Token, Adressen, Auswertung, Neu-Einstufung – ohne eine einzige
    Anfrage an das Abo."""
    return json.dumps(
        [{"id": e["id"], "interessant": "unklar", "sicherheit": "niedrig", "begruendung": "Probelauf"}
         for e in eintraege],
        ensure_ascii=False,
    )


def claude_urteil(prompt: str, claude_bin: str, modell: str = STANDARD_MODELL) -> str:
    """Claude Code im Kopfmodus fragen: ohne Werkzeuge, ohne Rückfragen, in einem leeren Ordner."""
    befehl = [
        claude_bin, "-p", prompt,
        "--model", modell,
        "--output-format", "json",
        "--allowedTools", "",
        "--permission-mode", "dontAsk",
    ]
    with tempfile.TemporaryDirectory(prefix="ki-worker-") as leerer_ordner:
        try:
            lauf = subprocess.run(befehl, cwd=leerer_ordner, capture_output=True, text=True,
                                  encoding="utf-8", timeout=CLAUDE_TIMEOUT_SECONDS)
        except FileNotFoundError:
            raise WorkerFehler(f"„{claude_bin}“ wurde nicht gefunden – Claude Code installiert?") from None
        except subprocess.TimeoutExpired:
            raise WorkerFehler(f"Claude hat nach {CLAUDE_TIMEOUT_SECONDS} s nicht geantwortet") from None

    if lauf.returncode != 0:
        raise WorkerFehler(f"Claude endete mit {lauf.returncode}: {(lauf.stderr or '').strip()[:300]}")
    try:
        ergebnis = json.loads(lauf.stdout)
    except json.JSONDecodeError:
        raise WorkerFehler(f"Claude lieferte kein JSON: {lauf.stdout[:300]}") from None
    if isinstance(ergebnis, list):  # je nach Fassung kommt eine Liste von Meldungen
        ergebnis = next((m for m in reversed(ergebnis) if isinstance(m, dict) and m.get("type") == "result"), {})
    if ergebnis.get("is_error"):
        raise WorkerFehler(f"Claude meldet einen Fehler: {str(ergebnis.get('result'))[:300]}")
    antwort = ergebnis.get("result")
    if not isinstance(antwort, str) or not antwort.strip():
        raise WorkerFehler("Claude lieferte keine verwertbare Antwort")
    _grenzen_melden(ergebnis)
    return antwort


def _grenzen_melden(ergebnis: dict) -> dict:
    """Verbrauch melden, soweit die Antwort ihn enthält (Kosten, Dauer, Nutzungsfenster)."""
    teile = []
    if ergebnis.get("total_cost_usd") is not None:
        teile.append(f"Kosten {ergebnis['total_cost_usd']}")
    if ergebnis.get("duration_ms") is not None:
        teile.append(f"Dauer {round(ergebnis['duration_ms'] / 1000)} s")
    grenzen = ergebnis.get("rate_limits") or {}
    fenster = grenzen.get("five_hour") or {}
    if fenster.get("used_percentage") is not None:
        teile.append(f"5-Stunden-Fenster {fenster['used_percentage']} %")
    if teile:
        log.info("Claude: %s", ", ".join(teile))
    return fenster


# ---------- Ablauf ----------

def runde(basis: str, token: str, limit: int | None, trocken: bool, claude_bin: str,
          modell: str = STANDARD_MODELL) -> dict:
    daten = offene_holen(basis, token, limit)
    if not daten.get("aktiv"):
        raise WorkerFehler("Die KI-Auswahl ist in den Einstellungen ausgeschaltet")
    eintraege = daten.get("eintraege") or []
    if not eintraege:
        return {"bewertet": 0, "offen": daten.get("offen", 0)}

    log.info("%d Einträge zu bewerten (noch %d offen)", len(eintraege), daten.get("offen", 0))
    antwort = trockenes_urteil(eintraege) if trocken else claude_urteil(daten["prompt"], claude_bin, modell)
    ergebnis = urteile_senden(basis, token, antwort, "probelauf" if trocken else modell)
    for fehler in ergebnis.get("fehler") or []:
        log.warning("Antwort unvollständig: %s", fehler)
    if ergebnis.get("unbekannt"):
        log.warning("Unbekannte Nummern übersprungen: %s", ergebnis["unbekannt"])
    log.info("%d Urteile gespeichert, noch %d offen", ergebnis.get("gespeichert", 0), ergebnis.get("offen", 0))
    return {"bewertet": ergebnis.get("gespeichert", 0), "offen": ergebnis.get("offen", 0)}


def main(argv: list[str] | None = None) -> int:
    zerleger = argparse.ArgumentParser(description="Bewertet offene Ausschreibungen des eV-Checkers mit Claude Code.")
    zerleger.add_argument("--url", default=os.environ.get("EVC_URL", STANDARD_URL),
                          help=f"Adresse der App (Standard: {STANDARD_URL})")
    zerleger.add_argument("--token", default=os.environ.get("EVC_AI_TOKEN", ""),
                          help="Token aus der .env der App (sonst aus EVC_AI_TOKEN)")
    zerleger.add_argument("--limit", type=int, default=None,
                          help="Einträge je Runde (Standard: die Einstellung der App)")
    zerleger.add_argument("--runden", type=int, default=0,
                          help="Höchstens so viele Runden (0 = bis nichts mehr offen ist)")
    zerleger.add_argument("--trocken", action="store_true",
                          help="Probelauf ohne Claude: beantwortet alles mit „unklar“")
    zerleger.add_argument("--claude", default=os.environ.get("CLAUDE_BIN", "claude"),
                          help="Programm für Claude Code (Standard: claude)")
    zerleger.add_argument("--model", default=os.environ.get("CLAUDE_MODEL") or STANDARD_MODELL,
                          help=f"Modell für die Bewertung (Standard: {STANDARD_MODELL}, "
                               "z. B. claude-haiku-5-5 oder claude-opus-5-5)")
    zerleger.add_argument("--wiederholen", type=int, default=0, metavar="MINUTEN",
                          help="Dauerbetrieb (Container): nach jedem Durchgang so viele Minuten warten "
                               "und von vorn beginnen (0 = nur ein Durchgang)")
    args = zerleger.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not args.token:
        log.error("Kein Token: EVC_AI_TOKEN setzen oder --token angeben")
        return 2

    if args.wiederholen <= 0:
        return durchgang(args)
    log.info("Dauerbetrieb: alle %d Minuten, je Durchgang höchstens %s Runden, Modell %s",
             args.wiederholen, args.runden or "unbegrenzt viele", args.model)
    try:
        while True:
            # Ein Fehler (App nicht erreichbar, Claude am Limit) beendet nur diesen Durchgang;
            # der Dienst läuft weiter und versucht es beim nächsten Mal erneut.
            durchgang(args)
            time.sleep(args.wiederholen * 60)
    except KeyboardInterrupt:
        log.warning("Beendet.")
        return 0


def durchgang(args) -> int:
    """Ein Durchgang: Runden, bis nichts mehr offen ist oder --runden erreicht ist."""
    basis = args.url.rstrip("/")
    bewertet = 0
    try:
        gelaufen = 0
        while args.runden == 0 or gelaufen < args.runden:
            ergebnis = runde(basis, args.token, args.limit, args.trocken, args.claude, args.model)
            gelaufen += 1
            bewertet += ergebnis["bewertet"]
            if ergebnis["bewertet"] == 0:
                log.info("Nichts mehr zu bewerten.")
                break
            if ergebnis["offen"] == 0:
                break
    except WorkerFehler as fehler:
        log.error("%s", fehler)
        return 1
    except KeyboardInterrupt:
        log.warning("Abgebrochen.")
        return 1
    log.info("Fertig: %d Urteile in diesem Durchgang.", bewertet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
