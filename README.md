# eV-Checker

Überwacht die Veröffentlichungen der [Vergabeplattform Berlin](https://www.berlin.de/vergabeplattform/veroeffentlichungen/)
und alle deutschen Bekanntmachungen von [oeffentlichevergabe.de](https://oeffentlichevergabe.de) und stuft sie anhand von
CPV-Codes und Stichwörtern als **interessant / nicht interessant** ein.

Quellen (RSS):
- Bekanntmachungen
- Beabsichtigte Beschränkte Ausschreibungen (§ 20 Abs. 4 VOB/A)
- Vergebene Aufträge (§ 20 Abs. 3 VOB/A, § 30 Abs. 1 UVgO)
- Vorinformationen / Markterkundungen

CPV-Codes stehen nicht im Feed, sondern auf den Detailseiten (meinauftrag.rib.de). Sie werden für neue Einträge einmalig nachgeladen.

Zusätzlich: **oeffentlichevergabe.de** (Datenservice Öffentlicher Einkauf) – offene Schnittstelle ohne Anmeldung,
Tagesexporte im eForms-Format (immer erst für den Vortag). Einträge werden nach Leistungsort (sonst Sitz des Auftraggebers)
den Regionen **Berlin / Brandenburg / Rest** zugeordnet. Dubletten mit dem Berlin-Feed werden zusammengeführt.
Nicht interessante Einträge aus „Rest“ werden nach 90 Tagen gelöscht (außer mit manueller Einstufung oder Notiz).

## Server mit Docker Compose und Traefik

Voraussetzungen: Docker mit Compose-Plugin, Traefik 3 läuft bereits separat mit dem externen Netzwerk `traefik_proxy`,
dem Entrypoint `websecure` und dem Zertifikats-Resolver `le` (sonst die Labels in `docker-compose.yml` anpassen).

### Erstinstallation

```bash
git clone <URL-des-Repositorys> ev-checker
cd ev-checker
cp .env.example .env
nano .env        # Domain, BasicAuth-Hash, optional SMTP eintragen
docker compose up -d --build
docker compose logs -f ev-checker
```

- **BasicAuth-Hash** erzeugen: `docker run --rm httpd:2.4-alpine htpasswd -nbB meinname 'MeinPasswort'`
  und die Ausgabe in **einfachen Anführungszeichen** in `EVC_BASIC_AUTH` eintragen (der Hash enthält `$`).
- Beim ersten Start legt die App `./data` an (Datenbank + `backups/`), lädt die Start-Regeln und ruft sofort ab
  (oeffentlichevergabe.de 7 Tage rückwirkend, dauert einige Minuten).
- Ohne Port-Freigabe ist die App nur über Traefik erreichbar. Zum Testen im LAN kann in der Compose-Datei
  `ports: - 8765:8000` einkommentiert werden (dann ohne BasicAuth!).

### Update

```bash
cd ev-checker
git pull
docker compose up -d --build
```

Datenbank-Änderungen (neue Spalten, neue Regel-Listen) übernimmt die App beim Start automatisch.

### Daten, Datensicherung, Wiederherstellung

- Alle Daten liegen in `./data` (SQLite-Datenbank `ev-checker.db`). Für ein Server-Backup genügt dieser Ordner.
- Die App legt jede Nacht (Standard 03:00, `EVC_BACKUP_TIME`) eine konsistente Kopie nach `./data/backups`
  und behält die letzten 14 (`EVC_BACKUP_KEEP`). In den Einstellungen gibt es zusätzlich „Jetzt sichern“.
- Wiederherstellen:
  ```bash
  docker compose down
  cp data/backups/ev-checker-JJJJMMTT-HHMMSS.db data/ev-checker.db
  rm -f data/ev-checker.db-wal data/ev-checker.db-shm
  docker compose up -d
  ```

### Tägliche E-Mail

SMTP-Zugangsdaten stehen in der `.env` (`EVC_SMTP_*`), Empfänger, Uhrzeit und An/Aus in den Einstellungen der App.
Verschickt werden nur neue interessante Treffer seit der letzten Mail; ohne neue Treffer keine Mail.
„Test-Mail senden“ schickt die Treffer der letzten 24 Stunden. Links in der Mail zeigen auf `https://<EVC_DOMAIN>`.

### Sicherheit

- Zugang nur über Traefik BasicAuth; die App selbst hat keinen Login.
- Formular-Absendungen von fremden Seiten werden abgelehnt (Schutz gegen CSRF).
- Der Container läuft nicht als root (`PUID`/`PGID`, Standard 1000).
- `.env` und `data/` sind in `.gitignore` – Domain, Passwörter und Daten gelangen nicht ins Repository.

## Lokal starten (Windows, ohne Docker)

Voraussetzung: Python 3.12

```powershell
.\start.ps1
```

Das Skript legt beim ersten Mal die Umgebung `.venv` an, installiert die Abhängigkeiten und öffnet
http://127.0.0.1:8765. Beim ersten Start wird sofort abgerufen (ca. 3–4 Minuten wegen der Detailseiten).

Manuell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Wichtig: nur **ein** Prozess (kein `--reload`, keine mehreren Worker), da der Zeitplan im App-Prozess läuft.

## Bedienung

- **Ausschreibungen**: Tabs Interessant / Nicht interessant / Alle, Filter, Suche, Einstufung per Klick überschreiben.
  Gruppiert nach Region (Berlin aufgeklappt, Brandenburg und Rest zugeklappt), darin Ausschreibungen,
  Beabsichtigte Vergaben und Vergebene Aufträge. Die Quelle steht an jedem Eintrag.
- **Detailansicht**: Prüfergebnisse (welche CPV-Codes/Stichwörter gegriffen haben), Beschreibung mit Markierungen, Notiz
- **Einstellungen**:
  - CPV-Prüfung und Stichwort-Prüfung einzeln an/aus, Verknüpfung ODER / UND
  - CPV-Codes, Stichwörter, ★ wichtige Stichwörter, Ausschluss-Stichwörter als Listen: jeder Eintrag per Checkbox an/aus,
    „+ Hinzufügen“ (auch mit Enter) und × zum Löschen; „alle an / alle aus“. Änderungen werden gesammelt und mit
    „Speichern & neu klassifizieren“ übernommen. Abgeschaltete Einträge bleiben gespeichert, wirken aber nicht.
  - ↓ Niedrige Priorität: eigene Stichwort-Liste, ändert nichts an interessant / nicht interessant. Nur interessante
    Einträge (auch manuell eingestufte) werden als „↓ NIEDRIG“ markiert und in ihrer Gruppe nach unten sortiert.
    Einstellbar: prüfen nur im Titel (Standard) oder in Titel und Beschreibung; bei gleichzeitigem ★ wichtigem
    Stichwort: Wichtig gewinnt (Standard) / Niedrig gewinnt / Beides (Mitte).
    Wichtige Stichwörter zählen wie normale, markieren Treffer aber als „★ WICHTIG“ und sortieren sie in jeder Gruppe nach oben.
  - Feeds einzeln an-/abschalten (neue Quellen brauchen eine eigene Anbindung im Code)
  - Aktualisierung: feste Uhrzeiten (Start: 07:00, 13:00) oder Intervall
  - Abrufprotokoll

## Regeln der Klassifizierung

| Prüfung | Treffer, wenn … |
|---|---|
| CPV | ein CPV-Code des Eintrags unter einen Listen-Code fällt (71314000 deckt 71314300 ab) |
| Stichwort | ein Stichwort in Titel/Auftragsgegenstand/Art der Leistung/Kurzbeschreibung vorkommt und kein Ausschluss-Stichwort |

Einträge ohne CPV-Codes (Vergebene Aufträge, Vorinformationen) werden nur über Stichwörter bewertet.
Stichwörter mit bis zu 3 Zeichen (z. B. `PV`) oder in Anführungszeichen zählen nur als ganzes Wort.
`*` ist ein Platzhalter für beliebige Buchstaben, auch über einen Bindestrich oder ein Leerzeichen hinweg
(`Energie*spar*contracting` trifft „Energiespar-Contracting“, „Energieeinspar Contracting“ usw.).

Kann eine aktive Quelle 2× oder öfter hintereinander nicht abgerufen werden, zeigt jede Seite oben eine Warnung.

## Daten

SQLite-Datei unter `data/ev-checker.db` (Pfad änderbar über Umgebungsvariable `EVC_DATA_DIR`).

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

Offene Punkte und Ideen: siehe [TODO.md](TODO.md).
