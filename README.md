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

- Zugang nur über Traefik BasicAuth; die App selbst hat keinen Login. Deshalb darf die App nie ohne
  Traefik davor erreichbar sein – das Port-Mapping in der `docker-compose.yml` bleibt zu.
- Formular-Absendungen von fremden Seiten werden abgelehnt (Schutz gegen CSRF).
- Die Seite setzt eine strenge Content-Security-Policy (nur eigene Dateien, keine Skripte im HTML)
  sowie `X-Content-Type-Options`, `X-Frame-Options` und `Referrer-Policy`; HSTS kommt von Traefik.
- Links aus den Quelldaten (Dokumente, Quellenlinks) werden nur als `http`/`https` verlinkt.
- Ziele von „zurück“/`next` bleiben innerhalb der App (kein Weiterleiten auf fremde Adressen).
- Detailseiten werden nur von `meinauftrag.rib.de` und `*.berlin.de` geladen, Weiterleitungen nur
  innerhalb dieser Hosts – eine umgeleitete Adresse kann nicht auf interne Dienste zeigen.
- Antworten aus dem Netz sind in der Größe begrenzt (`FEED_MAX_BYTES`, `DETAIL_MAX_BYTES`,
  `OV_MAX_BYTES` in `config.py`), XML mit DTD/Entitäten wird abgelehnt, einzelne Dateien im
  Tagesexport sind auf 16 MB begrenzt.
- Der Container läuft nicht als root (`PUID`/`PGID`, Standard 1000), mit schreibgeschütztem
  Dateisystem (außer `/data` und `/tmp`), ohne zusätzliche Rechte (`no-new-privileges`, `cap_drop`)
  und mit Speicherobergrenze. Wird der Container mit „OOMKilled“ beendet, `mem_limit` erhöhen.
- `.env` und `data/` sind in `.gitignore` – Domain, Passwörter und Daten gelangen nicht ins Repository.
  Die `.env` enthält SMTP-Passwort und BasicAuth-Hash: `chmod 600 .env`.
- Regelmäßig aktualisieren: `docker compose build --pull --no-cache && docker compose up -d` holt
  Sicherheitsaktualisierungen für Basis-Image und Python-Pakete.

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
  Beabsichtigte Vergaben und Vergebene Aufträge. In der Zeile stehen Auftraggeber, Verfahren und die
  CPV-Treffer mit Bezeichnung; die Quelle und die Kategorie „Bekanntmachung“ nur auf der Detailseite.
- **Suche**: Suchbegriff und daneben der Suchbereich – überall (Standard), nur Titel, nur Beschreibung,
  nur Auftraggeber oder nur CPV-Code.
- **Markierungs-Filter**: Kästchen für 🤖 KI-Treffer, ★ Wichtig, ♥ Beobachtet, CPV-Treffer, Stichwort-Treffer
  und Manuell eingestuft. Mehrere lassen sich kombinieren – dann muss alles zutreffen („nur KI **und**
  wichtig“). Niedrige Priorität hat eine eigene Auswahl: anzeigen (Standard), ausblenden oder nur niedrige.
  Frühere Adressen mit einem einzelnen `treffer=…` funktionieren weiterhin.
- **Auftraggeber**: überall als Link – öffnet in einem neuen Tab die Suche nach dieser Vergabestelle
  (Reiter „Alle“).
- **Gesehen**: Im Ausschreibungsbereich ist „nur neue“ voreingestellt – die Liste zeigt also nur, was noch
  nicht gesehen wurde, und abgehakte Einträge verschwinden daraus. „auch gesehene anzeigen“ (bzw. der Haken)
  zeigt wieder alles; der Reiter „♥ Auftraggeber“ zeigt weiterhin alles. Ein Eintrag gilt als gesehen,
  sobald er geöffnet wurde, oder über „Diese Ansicht als gesehen markieren“ – das wirkt nur auf die
  gerade gefilterte Ansicht (Reiter und Filter).
- **Fristen**: Einträge mit abgelaufener Frist sind standardmäßig ausgeblendet (Hinweis „N ausgeblendet – anzeigen“);
  Auswahl im Filter: abgelaufene ausblenden / nur offene Fristen / alle. Einträge ohne Frist bleiben sichtbar.
- **♥ Auftraggeber**: eigener Reiter für beobachtete Auftraggeber – je Auftraggeber ein Block mit Ausschreibungen,
  Beabsichtigten Vergaben und Vergebenen Aufträgen, Standard-Reiter „Alle“ (auch nicht interessante). Beobachten per
  „♥ Auftraggeber beobachten“ auf der Detailseite oder in den Einstellungen (Name oder Namensteil, `*` und `+`).
  Einträge beobachteter Auftraggeber tragen ♥, werden nicht automatisch gelöscht und stehen in der täglichen E-Mail
  in einem eigenen Abschnitt.
- **🤖 KI-Auswahl**: eigener Reiter. Eine KI liest Titel und Beschreibung und entscheidet anhand des
  Leistungsprofils, ob die Bekanntmachung zum Angebot passt. Die Seite zeigt den fertigen Prompt zum Kopieren
  (Profil + die offenen Einträge); die Antwort wird als JSON wieder eingefügt. Die Zahl im Menü ist die Anzahl
  der offenen Bewertungen. Treffer tragen in der Liste „🤖 KI“, stehen in ihrer Gruppe ganz oben (noch vor
  ★ wichtig) und lassen sich über die Markierungs-Filter einzeln anzeigen; die Begründung steht auf der
  Detailseite.
- **Detailansicht**: Prüfergebnisse (welche CPV-Codes/Stichwörter gegriffen haben, Urteil der KI mit Begründung),
  Beschreibung mit Markierungen, Notiz
- **Einstellungen**:
  - CPV-Prüfung und Stichwort-Prüfung einzeln an/aus, Verknüpfung ODER / UND
  - E-Mail: mehrere Empfänger (pro Zeile oder durch Komma getrennt), alle bekommen dieselbe Mail
  - CPV-Codes werden im amtlichen Katalog gesucht (nach Code oder Bezeichnung) und mit ihrer Bezeichnung
    übernommen – Code und Titel gehören zusammen und werden nicht getippt. Eigene Bezeichnungen, die schon
    in der Liste stehen, bleiben erhalten.
  - CPV-Codes, Stichwörter, ★ wichtige Stichwörter, Ausschluss-Stichwörter als Listen: jeder Eintrag per Checkbox an/aus,
    „+ Hinzufügen“ (auch mit Enter) und × zum Löschen; „alle an / alle aus“. Änderungen werden gesammelt und mit
    „Speichern & neu klassifizieren“ übernommen. Abgeschaltete Einträge bleiben gespeichert, wirken aber nicht.
    Das Neu-Einstufen geht über alle Einträge und dauert bei zehntausenden eine halbe Minute; es läuft deshalb
    im Hintergrund. Die Seite kommt sofort zurück und zeigt oben einen Hinweis, bis es fertig ist – danach lädt
    sie sich einmal neu. Beim Speichern der KI-Einstellungen passiert das nur, wenn sich Schalter oder
    Betriebsart ändern: Profil, Regionen, Obergrenze und Höchstalter ändern die Einstufung nicht.
  - ↓ Niedrige Priorität: eigene Stichwort-Liste, ändert nichts an interessant / nicht interessant. Nur interessante
    Einträge (auch manuell eingestufte) werden als „↓ NIEDRIG“ markiert und in ihrer Gruppe nach unten sortiert.
    Einstellbar: prüfen nur im Titel (Standard) oder in Titel und Beschreibung; bei gleichzeitigem ★ wichtigem
    Stichwort: Wichtig gewinnt (Standard) / Niedrig gewinnt / Beides (Mitte).
    Wichtige Stichwörter zählen wie normale, markieren Treffer aber als „★ WICHTIG“ und sortieren sie in jeder Gruppe nach oben.
  - 🤖 KI-Auswahl: an/aus, Betriebsart (nur Markierung / dritte Prüfung), Regionen, Obergrenze je Durchgang,
    Höchstalter der Bekanntmachungen (Standard 30 Tage, 0 = ohne Grenze – so sammelt sich nach einer Pause
    kein Berg alter Einträge an) und das Leistungsprofil als freier Text. „Veraltete Urteile neu bewerten“ verwirft Urteile, die nicht mehr
    zum Profil oder zum Text passen, und reiht die Einträge wieder ein.
  - Feeds einzeln an-/abschalten (neue Quellen brauchen eine eigene Anbindung im Code)
  - Aktualisierung: feste Uhrzeiten (Start: 07:00, 13:00) oder Intervall
  - Abrufprotokoll

## Regeln der Klassifizierung

| Prüfung | Treffer, wenn … |
|---|---|
| CPV | ein CPV-Code des Eintrags unter einen Listen-Code fällt (71314000 deckt 71314300 ab) |
| Stichwort | ein Stichwort in Titel/Auftragsgegenstand/Art der Leistung/Kurzbeschreibung vorkommt und kein Ausschluss-Stichwort |
| 🤖 KI | die KI den Eintrag am Leistungsprofil für passend hält (nur in der Betriebsart „dritte Prüfung“) |

Einträge ohne CPV-Codes (Vergebene Aufträge, Vorinformationen) werden nur über Stichwörter bewertet.

Die KI-Auswahl verhält sich anders als die beiden anderen Prüfungen, weil eine Bewertung Zeit und Geld kostet:
Das Urteil wird gespeichert und bei einer Neu-Klassifizierung nur abgelesen, nie erneut erfragt. Bewertet wird ein
Eintrag nur einmal – erst wenn sich der bewertete Text oder das Leistungsprofil ändert, gilt das Urteil als veraltet,
und auch dann wird es nur auf Knopfdruck neu eingeholt. Bewertet wird außerdem nur, was gerade in Frage kommt:
passende Region, Frist nicht abgelaufen, nicht älter als eingestellt – und in der Betriebsart „nur Markierung“ nur,
was ohnehin interessant ist. Ändert sich eine dieser Einstellungen, räumt die App die Warteschlange entsprechend auf. In der Betriebsart „nur Markierung“ (Standard) bekommt die KI
ausschließlich Einträge zu sehen, die über CPV oder Stichwörter schon interessant sind, und ihr Urteil ändert die
Einstufung nicht; in der Betriebsart „dritte Prüfung“ bewertet sie alle Einträge der gewählten Regionen und zählt
wie CPV und Stichwörter mit (ODER/UND). Antwortet sie „unklar“, gilt das als kein Treffer.

Zu jedem CPV-Code zeigt die App eine Bezeichnung: zuerst die Angabe der Quelle am Eintrag, dann eine eigene
Bezeichnung aus der CPV-Liste, sonst den amtlichen Titel aus `app/cpv_de.json.gz`. Das ist der deutsche Teil des
offiziellen Vokabulars CPV 2008 (Verordnung (EG) Nr. 213/2008, 9454 Codes), aus der Datei `cpv_2008_xml` von
<https://ted.europa.eu/en/simap/cpv> erzeugt. Er ändert sich nur, wenn die EU ihn überarbeitet.
Stichwörter mit bis zu 3 Zeichen (z. B. `PV`) oder in Anführungszeichen zählen nur als ganzes Wort.
`*` ist ein Platzhalter für beliebige Buchstaben, auch über eine Trennstelle hinweg (bis zu drei Leerzeichen,
Bindestriche oder Gedankenstriche, z. B. „- “ oder ein doppeltes Leerzeichen): `Energie*spar*contracting` trifft
„Energiespar-Contracting“, „Energieeinspar Contracting“, „Energiespar  Contracting“ usw., aber nicht
„Energiemanagement und Contracting“.
Mit `+` verbundene Begriffe müssen alle vorkommen, egal wo und in welcher Reihenfolge (`Konzept + Energie`).
Für jedes Stichwort ist wählbar, ob im Titel und in der Beschreibung oder nur im Titel gesucht wird
(Standard: Titel + Beschreibung, bei ↓ Niedrig nur Titel).

Regeln und Einstellungen liegen in der Datenbank (`data/ev-checker.db`), nicht im Code: Updates (`git pull`, neues Image)
überschreiben sie nicht. Die Start-Regeln werden nur beim allerersten Start angelegt; nachträglich ergänzte Standard-Regeln
(z. B. „Energiemanagementsystem“) kommen einmalig hinzu und bleiben gelöscht, wenn man sie entfernt.

Dubletten zwischen den Quellen werden über Titel und Auftraggeber erkannt. Dabei gelten „für“ und „fuer“,
„Mobilität“ und „Mobilitaet“ als gleich – oeffentlichevergabe.de schreibt Umlaute teilweise aus, die
Vergabeplattform Berlin nicht. Die Angleichung wirkt beim Abruf, bereits doppelt gespeicherte Einträge
bleiben getrennt.

Eingestuft wird direkt beim Abruf, und zwar je Feed bzw. je geladenem Tag. Bricht ein Abruf mittendrin ab
(Netz weg, Neustart), bleibt höchstens ein Tag ohne Einstufung – und auch der wird beim nächsten Start der
App und am Ende des nächsten Abrufs nachgeholt. Das ist wichtig, weil ein nicht eingestufter Eintrag
stillschweigend als „nicht interessant“ gilt und sonst niemandem auffällt.

Kann eine aktive Quelle 2× oder öfter hintereinander nicht abgerufen werden, zeigt jede Seite oben eine Warnung.

## Daten

SQLite-Datei unter `data/ev-checker.db` (Pfad änderbar über Umgebungsvariable `EVC_DATA_DIR`).

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

Offene Punkte und Ideen: siehe [TODO.md](TODO.md).
