"""KI-Worker: Probelauf, Auswertung der Claude-Antwort und Fehlerfälle.

Der Worker spricht nur über HTTP mit der App; hier läuft er gegen eine Attrappe, damit die Tests
ohne Netz und ohne Claude auskommen.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import ki_worker  # noqa: E402


class FakeApp:
    """Ersetzt die beiden Adressen der App."""

    def __init__(self, eintraege, aktiv=True):
        self.offen = list(eintraege)
        self.aktiv = aktiv
        self.gesendet = []

    def __call__(self, url, token, daten=None):
        if daten is None:
            genommen = self.offen[:3]
            return {"aktiv": self.aktiv, "modus": "markierung", "offen": len(self.offen),
                    "eintraege": genommen,
                    "prompt": "Profil …\n" + "\n".join(f"--- Eintrag {e['id']} ---" for e in genommen)}
        self.gesendet.append(daten)
        urteile = json.loads(daten["antwort"])
        self.offen = self.offen[len(urteile):]
        return {"gespeichert": len(urteile), "ids": [u["id"] for u in urteile], "unbekannt": [],
                "fehler": [], "offen": len(self.offen)}


@pytest.fixture
def app(monkeypatch):
    fake = FakeApp([{"id": i, "titel": f"Eintrag {i}"} for i in range(1, 8)])
    monkeypatch.setattr(ki_worker, "_anfrage", fake)
    return fake


def test_dry_run_answers_everything_with_unklar(app):
    assert ki_worker.main(["--token", "t", "--trocken"]) == 0
    urteile = json.loads(app.gesendet[0]["antwort"])
    assert [u["interessant"] for u in urteile] == ["unklar"] * 3
    assert app.gesendet[0]["modell"] == "probelauf"
    assert app.offen == []  # in mehreren Runden abgearbeitet


def test_rounds_can_be_limited(app):
    assert ki_worker.main(["--token", "t", "--trocken", "--runden", "1"]) == 0
    assert len(app.gesendet) == 1 and len(app.offen) == 4


def test_without_a_token_it_stops_early(app):
    assert ki_worker.main(["--token", ""]) == 2
    assert app.gesendet == []


def test_switched_off_selection_is_reported(monkeypatch):
    monkeypatch.setattr(ki_worker, "_anfrage", FakeApp([], aktiv=False))
    assert ki_worker.main(["--token", "t", "--trocken"]) == 1


def test_claude_answer_is_taken_from_the_result_field(monkeypatch):
    fertig = subprocess.CompletedProcess([], 0, stdout=json.dumps(
        {"type": "result", "is_error": False, "result": '[{"id": 1, "interessant": true}]',
         "total_cost_usd": 0.01, "duration_ms": 1234}), stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fertig)
    assert ki_worker.claude_urteil("Prompt", "claude") == '[{"id": 1, "interessant": true}]'


def test_claude_errors_become_readable_messages(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "kaputt"))
    with pytest.raises(ki_worker.WorkerFehler, match="endete mit 1"):
        ki_worker.claude_urteil("Prompt", "claude")

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "kein json", ""))
    with pytest.raises(ki_worker.WorkerFehler, match="kein JSON"):
        ki_worker.claude_urteil("Prompt", "claude")

    def fehlt(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", fehlt)
    with pytest.raises(ki_worker.WorkerFehler, match="nicht gefunden"):
        ki_worker.claude_urteil("Prompt", "claude")


def test_claude_runs_without_tools_in_an_empty_folder(monkeypatch):
    aufrufe = {}

    def merken(befehl, **kwargs):
        aufrufe["befehl"] = befehl
        # der Ordner besteht nur während des Aufrufs, deshalb hier hineinsehen
        aufrufe["inhalt"] = list(Path(kwargs["cwd"]).iterdir())
        return subprocess.CompletedProcess(befehl, 0, json.dumps({"result": "ok", "is_error": False}), "")

    monkeypatch.setattr(subprocess, "run", merken)
    ki_worker.claude_urteil("Prompt", "claude")
    befehl = aufrufe["befehl"]
    assert befehl[befehl.index("--allowedTools") + 1] == ""  # keine Werkzeuge
    assert befehl[befehl.index("--permission-mode") + 1] == "dontAsk"  # keine Rückfragen
    assert aufrufe["inhalt"] == []  # leerer Ordner, kein Zugriff auf das Projekt
    assert befehl[befehl.index("--model") + 1] == ki_worker.STANDARD_MODELL == "claude-sonnet-5-5"


def test_continuous_mode_keeps_going_after_an_error(monkeypatch):
    """Im Container läuft der Worker dauerhaft. Ein Fehler (App weg, Claude am Limit) darf nur den
    einen Durchgang beenden, nicht den Dienst."""
    aufrufe = []

    def wackelig(url, token, daten=None):
        aufrufe.append(url)
        raise ki_worker.WorkerFehler("App nicht erreichbar")

    pausen = []

    def schlafen(sekunden):
        pausen.append(sekunden)
        if len(pausen) == 3:
            raise KeyboardInterrupt  # so endet docker stop / Strg+C

    monkeypatch.setattr(ki_worker, "_anfrage", wackelig)
    monkeypatch.setattr(ki_worker.time, "sleep", schlafen)
    assert ki_worker.main(["--token", "t", "--trocken", "--wiederholen", "60"]) == 0
    assert len(aufrufe) == 3  # dreimal versucht, trotz Fehler
    assert pausen == [3600, 3600, 3600]
