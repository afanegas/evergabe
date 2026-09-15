# eV-Checker – Offene Todos & Ideen

Stand: 13.09.2026 (Phase 1 fertig)

## Offene Todos

### Phase 1 – MVP (lokal, ohne Docker) ✅ umgesetzt am 13.09.2026
- [x] Projektgerüst anlegen (Python, FastAPI, SQLite, Jinja2 – ohne HTMX, schlichte Formulare)
- [x] Import der 4 Feeds der Vergabeplattform Berlin
  - [x] Bekanntmachungen – `https://www.berlin.de/vergabeplattform/veroeffentlichungen/bekanntmachungen/feed.rss`
  - [x] Beabsichtigte Beschränkte Ausschreibungen (§ 20 Abs. 4 VOB/A) – `…/info-19/feed.rss`
  - [x] Vergebene Aufträge (§ 20 Abs. 3 VOB/A, § 30 Abs. 1 UVgO) – `…/info-19-20/feed.rss`
  - [x] Vorinformationen / Markterkundungen – `…/vorinformationen/feed.rss`
- [x] Automatischer Abruf mit **anpassbarer Aktualisierungsrate** (Start: 2× täglich, z. B. 07:00 und 13:00)
  - [x] Warnung in den Einstellungen, wenn der Abstand zu groß wird (Feeds enthalten nur die letzten 50 Einträge ≈ 5 Tage)
- [x] Button „Jetzt abrufen“ + Anzeige „zuletzt abgerufen“
- [x] Duplikate über GUID/Link erkennen
- [x] CPV-Codes von den RIB-Detailseiten (meinauftrag.rib.de) nachladen – nur für neue Einträge, mit Pause zwischen Abrufen
- [x] Klassifizierung **interessant / nicht interessant** aus zwei getrennten Prüfungen:
  - [x] **CPV-Prüfung:** Treffer, wenn ein CPV-Code des Eintrags in der CPV-Liste liegt (hierarchisch: `71314000` deckt `71314300` ab)
  - [x] **Stichwort-Prüfung:** Treffer, wenn ein Stichwort in Titel/Beschreibung vorkommt und kein Ausschluss-Stichwort
  - [x] Jede Prüfung einzeln an-/abschaltbar (CPV weglassen möglich)
  - [x] Verknüpfung einstellbar: **ODER** (eine Prüfung reicht, Standard) / **UND** (beide nötig)
  - [x] Einträge ohne CPV-Codes (Vergebene Aufträge, Vorinformationen, fehlende CPV) → nur Stichwort-Prüfung
  - [x] Pro Eintrag anzeigen: CPV-Treffer ✓/✗, Stichwort-Treffer ✓/✗ und welche Codes/Wörter gegriffen haben
- [x] Listenansicht: Filter (Kategorie, Status, CPV-Treffer, Stichwort-Treffer, Frist, Vergabestelle), Volltextsuche, Sortierung, Markierung „neu“
- [x] Detailansicht mit Link zur Originalbekanntmachung
- [x] Status manuell überschreiben + Notiz
- [x] Einstellungsseite:
  - [x] CPV-Liste pflegen (hinzufügen/löschen)
  - [x] Stichwörter und Ausschluss-Stichwörter pflegen
  - [x] CPV-Prüfung / Stichwort-Prüfung an/aus, Verknüpfung ODER/UND
  - [x] Aktualisierungsrate (Uhrzeiten oder Intervall)
  - [x] Alle Einträge neu klassifizieren
- [x] Start-Regeln (siehe unten) beim ersten Start in die Datenbank laden

### Nach Phase 1 – Beobachtungen aus dem ersten Abruf
- [ ] Regeln mit echten Daten schärfen (erster Abruf: 5 von 153 interessant), z. B.
  - „Gebäudeautomation“ / „MSR-Technik“ als Stichwort? (Bekanntmachung „Gebäudeautomation“ wurde nicht erkannt)
  - CPV 71241000 (Machbarkeitsstudien) trifft auch allgemeine Architekten-/Ingenieurleistungen → ggf. entfernen
  - CPV 45331000 (Heizung/Lüftung/Klima) trifft reine Bauleistungen wie „Austausch Kälteanlage“ → prüfen
- [ ] Ca. 7 % der RIB-Detailseiten haben keine CPV-Codes → werden nur über Stichwörter bewertet
- [ ] Taucht dieselbe Ausschreibung in zwei Feeds auf (z. B. erst beabsichtigte Beschränkte Ausschreibung, dann Bekanntmachung), wird die Kategorie überschrieben → ggf. Verlauf speichern
- [ ] Autostart auf dem Test-PC (z. B. Windows-Aufgabenplanung), solange noch kein Homeserver-Betrieb

### Erweiterungen 14.09.2026 ✅
- [x] Stichwörter Energiespar-/Energieeinspar-Contracting in allen Schreibweisen
- [x] Liste gruppiert: **Ausschreibungen** (Bekanntmachungen + Vorinformationen) oben aufgeklappt, **Beabsichtigte Beschränkte Ausschreibungen** und **Vergebene Aufträge** darunter zugeklappt; Aufklapp-Zustand bleibt im Browser-Tab erhalten; „weitere anzeigen“ je Gruppe
- [x] Einstellungen: Feeds einzeln an-/abschaltbar, mit URL, Anzahl gespeicherter Einträge und Status des letzten Abrufs
- [x] CSS/JS mit Versionskennung, damit der Browser nach Updates keine alte Fassung aus dem Cache nutzt
- [x] App im Heimnetz erreichbar (`--host 0.0.0.0`, Port 8765)

### Erweiterung oeffentlichevergabe.de ✅ (14.09.2026)
- [x] Gesamtliste von oeffentlichevergabe.de (Datenservice Öffentlicher Einkauf) als zweite Quelle
  - Tagesexporte im eForms-Format (`/api/notice-exports?pubDay=JJJJ-MM-TT&format=eforms.zip`) – nur eForms enthält Angebotsfristen (OCDS/CSV nicht)
  - EU-Bekanntmachungen und nationale Kurzformate, ca. 800–1.000 pro Werktag, CPV bei ~97 %
  - Daten immer erst für den Vortag; erster Abruf lädt 7 Tage rückwirkend, danach jeweils den letzten Tag erneut + neue Tage (max. 31 auf einmal)
  - importiert: Ausschreibungen, Vorinformationen, beabsichtigte Beschränkte Ausschreibungen (nationaler Klartext), vergebene Aufträge, Vertragsänderungen, angekündigte Direktvergaben (veat)
- [x] Übergruppierung **Berlin** (aufgeklappt) / **Brandenburg** / **Rest** (zugeklappt), darin die Gruppen Ausschreibungen / Beabsichtigte Vergaben / Vergebene Aufträge; zugeklappte Gruppen werden beim Aufklappen nachgeladen
- [x] Region nach Leistungsort (NUTS DE3 = Berlin, DE4 = Brandenburg), fehlt er: Sitz des Auftraggebers; mehrere Orte: Berlin > Brandenburg > Rest
- [x] Quelle je Eintrag sichtbar (Liste + Detail mit Links zu beiden Quellen), Filter „Quelle“
- [x] Dubletten Berlin-Feed ↔ oeffentlichevergabe.de zusammenführen (gleicher Titel + ähnliche Vergabestelle, max. 30 Tage auseinander); fehlende Werte (z. B. Fristen) werden ergänzt
- [x] Nicht interessante „Rest“-Einträge ohne manuelle Einstufung/Notiz nach 90 Tagen löschen
- [x] Kopfleiste auf schmalen Bildschirmen (Handy) umbrechend

### Nach oeffentlichevergabe.de – Beobachtungen aus dem ersten Import (7 Tage, 5.078 Einträge)
- [ ] Regeln schärfen: Titel mit „Bundesministerium für Umwelt, **Klimaschutz**, …“ werden über das Stichwort „Klimaschutz“ interessant (reine Bauleistungen am Ministerium) → z. B. Ausschluss-Stichwort „Bundesministerium für Umwelt, Klimaschutz“
- [ ] „Rest“ hat viele Treffer (283 interessant in 7 Tagen) → ggf. strengere Regeln nur für „Rest“ (z. B. UND-Verknüpfung) oder eigene Einstellung je Region
- [ ] Leistungsort manchmal nur als Text ohne NUTS-Code (z. B. „Frankfurt am Main“) → dann zählt der Sitz des Auftraggebers (DB InfraGO → Berlin); ggf. Postleitzahl/Ortsname auswerten
- [ ] Dublettenerkennung nur bei exakt gleichem Titel; abweichende Schreibweisen/Lose werden nicht erkannt (erster Import: 19 zusammengeführt)
- [ ] Nutzungsbedingungen/Lizenz der Open-Data-Schnittstelle offiziell bestätigen (Drittanbieter nennt CC0)
- [ ] Zuschläge mit Auftragnehmer und Auftragswert als Datenbasis für die Marktbeobachtung nutzen
- [ ] Verworfen: service.bund.de – robots.txt verbietet den automatischen Abruf der Feed-Pfade, Feed ohne Verfahrensart/CPV

### Wichtige Stichwörter ✅ (14.09.2026)
- [x] Neue Regel-Liste „★ Wichtige Stichwörter“ in den Einstellungen
- [x] Wirken wie normale Stichwörter (Ausschluss-Stichwörter und ODER/UND gelten weiter); Treffer werden als „★ WICHTIG“ markiert und stehen in jeder Gruppe oben
- [x] Anzahl wichtiger Treffer in Regions- und Gruppenüberschriften, Filter „★ Wichtige Treffer“
- [x] Listenspalte „Treffer“ statt „Prüfungen“: zeigt die getroffenen CPV-Codes (mit Bezeichnung aus der CPV-Liste), Stichwörter (★ = wichtig) und – falls ein Treffer aufgehoben wurde – durchgestrichen mit dem Ausschluss-Stichwort
- [x] Start-Liste: Energiespar-/Energieeinspar-Contracting (6 Schreibweisen), Wärmeplanung, Wärmenetz, Sanierungsfahrplan, iSFP, Energiekonzept, Quartierskonzept (aus der normalen Liste verschoben)

### Regel-Listen statt Textfelder ✅ (14.09.2026)
- [x] CPV-Codes, Stichwörter, ★ wichtige Stichwörter und Ausschluss-Stichwörter als Listen mit Checkbox (an/aus), Hinzufügen und Löschen
- [x] „alle an / alle aus“ je Liste, Zähler „x von y aktiv“, Hinweis auf ungespeicherte Änderungen (inkl. Warnung beim Verlassen der Seite)
- [x] Abgeschaltete Regeln bleiben in der Datenbank (`rules.active`), wirken aber nicht auf die Klassifizierung
- [x] Prüfung beim Hinzufügen: CPV-Code 8 Ziffern (Prüfziffer wird entfernt), keine Dubletten; bei Fehlern gehen Eingaben nicht verloren

### Niedrige Priorität ✅ (14.09.2026)
- [x] Neue Regel-Liste „↓ Niedrige Priorität“ (Start: Planungsleistungen, HOAI) – ändert nicht die Einstufung, markiert „↓ NIEDRIG“ und sortiert in der Gruppe nach unten
- [x] Einstellung „Prüfen in“: nur Titel (Standard) / Titel und Beschreibung
- [x] Markierung nur bei interessanten Einträgen (manuelle Einstufung zählt; wird beim Umstufen sofort neu berechnet)
- [x] Einstellung Konflikt mit ★ wichtig: Wichtig gewinnt (Standard) / Niedrig gewinnt / Beides markieren (Mitte)
- [x] Treffer-Spalte zeigt Niedrig-Stichwörter (↓), Filter „↓ Niedrige Priorität“, Markierung auf der Detailseite
- [x] Nach Korrektur (nur interessante, nur Titel): 25 Einträge niedrig (Brandenburg 2, Rest 23)

### Platzhalter & Quellen-Warnung ✅ (14.09.2026)
- [x] Platzhalter `*` in allen Stichwort-Listen: beliebige Buchstaben, höchstens über einen Bindestrich/ein Leerzeichen hinweg (`Energie*spar*contracting` trifft alle 6 Contracting-Schreibweisen, nicht „Energieliefer-Contracting“)
- [x] Warnung (Banner auf allen Seiten), wenn eine aktive Quelle 2× oder öfter hintereinander nicht abgerufen werden konnte – mit Anzahl, „seit“ und letzter Fehlermeldung; verschwindet nach erfolgreichem Abruf
- [x] Die 6 Contracting-Schreibweisen in den ★ wichtigen Stichwörtern durch `Energie*spar*contracting` ersetzt (auch in der Start-Liste für neue Installationen)

### Kombinierte Stichwörter & Bereich je Stichwort ✅ (15.09.2026)
- [x] Kombinierte Stichwörter mit `+` („Konzept + Energie“): alle Teile müssen im gewählten Bereich vorkommen, Reihenfolge egal; kombinierbar mit `*`
- [x] Bereich je Stichwort (alle Stichwort-Listen): Titel + Beschreibung oder nur Titel; gemeinsame Einstellung „Prüfen in“ bei ↓ Niedrig entfällt (Wert wurde auf die Niedrig-Stichwörter übertragen)
- [x] „Energiemanagementsystem“ als Stichwort und ★ wichtiges Stichwort (einmalige Ergänzung auch in bestehenden Datenbanken)

### Vorschläge (14.09.2026, noch nicht umgesetzt)
- [ ] Treffer-Statistik je Regel (Treffer in 30 Tagen, davon manuell als nicht interessant markiert)
- [ ] Vorschau vor dem Speichern der Regeln („+12 interessant, −3 niedrig“)
- [ ] Merkliste / Beobachten mit Hinweis bei Änderungen (Frist verschoben, Zuschlag)
- [ ] Startseite „Diese Woche“: neue wichtige Treffer + Fristen der nächsten 7/14 Tage
- [ ] Abgelaufene Fristen standardmäßig ausblenden
- [ ] Filter nach Auftragswert
- [ ] Gespeicherte Filter als Schnellauswahl
- [ ] Regeln exportieren / importieren (JSON)

### Phase 2 – Homeserver ✅ (Basis erstellt 14.09.2026, Übertragung auf den Server durch den Nutzer)
- [x] Dockerfile (python:3.12-slim, läuft als PUID/PGID via gosu, Healthcheck `/health`) + `docker-compose.yml` mit Traefik-3-Labels (`traefik_proxy`, `websecure`, `le`)
- [x] Update-Weg: `git pull && docker compose up -d --build` (Build auf dem Server)
- [x] Persönliche Werte nur in `.env` (Domain, BasicAuth-Hash, SMTP); `.env.example` als Vorlage; `.env`, `data/`, `.claude/` in `.gitignore`
- [x] Zugangsschutz über Traefik BasicAuth; zusätzlich Ablehnung von Formular-Absendungen fremder Seiten (CSRF)
- [x] Datenbank im Volume `./data`; tägliche Datensicherung 03:00 nach `data/backups`, letzte 14 behalten, „Jetzt sichern“ in den Einstellungen
- [x] Tägliche E-Mail mit neuen interessanten Treffern (SMTP aus `.env`; Empfänger, Uhrzeit, An/Aus und Test-Mail in den Einstellungen)
- [ ] Auf dem Server testen: Image-Build, Traefik-Routing, BasicAuth, SMTP-Versand (lokal ohne Docker nicht prüfbar)
- [ ] Git-Repository anlegen und nach GitHub übertragen

### Phase 3 – Erweiterungen
- [ ] KI-Einschätzung (Claude) für Grenzfälle mit kurzer Begründung
- [ ] Bearbeitungsstatus (in Prüfung / Angebot abgegeben / abgesagt / gewonnen)

## Start-Regeln (in der App anpassbar)

**CPV-Liste:**
71314000 Energie u. zugehörige Dienstleistungen · 71314200 Energiemanagement · 71314300 Energieeffizienzberatung · 90712000 Umweltplanung · 90713000 Umweltberatung · 09330000 Solarenergie · 09323000 Fernwärme · 71321000 Techn. Planung Gebäudeanlagen · 71321200 Heizungsplanung · 71313000 Beratung Umwelttechnik · 45261215 Solardächer · 45331000 Heizung/Lüftung/Klima · 71241000 Machbarkeitsstudien · 71335000 Technische Studien

**Stichwörter:** Energieberatung, Energieaudit, Energiemanagement, Energieeffizienz, Klimaschutz, Contracting, Photovoltaik, PV, Solar, BHKW, KWK, Wärmepumpe, Mieterstrom, Dekarbonisierung, CO2-Bilanz, klimaneutral, Ladeinfrastruktur

**★ Wichtige Stichwörter:** Energie\*spar\*contracting, Wärmeplanung, Wärmenetz, Sanierungsfahrplan, iSFP, Energiekonzept, Quartierskonzept

**↓ Niedrige Priorität:** Planungsleistungen, HOAI (Prüfen in: nur Titel · Konflikt: Wichtig gewinnt)

**Ausschluss-Stichwörter:** Malerarbeiten, Tischler, Gerüst, Pflaster, Reinigung, Schädlingsbekämpfung, Garten- und Landschaftsbau, Winterdienst, Kurier, Bewachung, Catering

**Einstellungen:** CPV-Prüfung an · Stichwort-Prüfung an · Verknüpfung ODER · Abruf 07:00 und 13:00

## Ideen

### Marktbeobachtung (auf Basis „Vergebene Aufträge“)
Die Bekanntmachungen vergebener Aufträge nennen Vergabestelle und beauftragtes Unternehmen. Über die Zeit gesammelt ergibt das eine Datenbasis für Auswertungen:

- **Wettbewerber-Übersicht:** Welche Unternehmen gewinnen Aufträge in BEA-relevanten Themen? Anzahl Zuschläge je Unternehmen, Zeitverlauf
- **Auftraggeber-Profil:** Welche Vergabestellen (Bezirksämter, Senatsverwaltungen, SILB, BIM …) vergeben häufig Energie-/Klimaschutzleistungen? → Akquise-Ziele
- **Themen-Trends:** Entwicklung der Anzahl interessanter Ausschreibungen je Thema (Wärmeplanung, PV, Contracting …) pro Monat/Quartal
- **Saisonalität:** Wann im Jahr wird besonders viel ausgeschrieben?
- **Verfahrensarten:** Verteilung Offenes Verfahren / Beschränkte Ausschreibung / Verhandlungsverfahren bei relevanten Aufträgen
- **Verknüpfung Ausschreibung → Zuschlag:** Bekanntmachung und spätere Vergabeinfo über Titel/Vergabestelle zusammenführen (wer hat gewonnen, wie lange hat es gedauert)
- **Merkliste Unternehmen:** Wettbewerber markieren und bei neuen Zuschlägen hervorheben
- **Dashboard** mit Diagrammen und Export (CSV/Excel)

### Weitere Ideen
- Gewichtung/Punkte je CPV-Code oder Stichwort statt reinem Treffer/kein Treffer
- Zwischenstatus „prüfen“, z. B. wenn nur eine der beiden Prüfungen greift
- Getrennte Stichwort-Listen je Themenfeld (Wärme, PV, Beratung …) mit Themen-Tag am Eintrag
- Fristen-Erinnerung (z. B. 7 Tage vor Angebotsfrist)
- Kalender-Export (ICS) der Fristen interessanter Ausschreibungen
- Volltext der Vergabeunterlagen/Leistungsbeschreibung auswerten (sofern ohne Registrierung abrufbar)
- Regel-Vorschläge: Stichwörter/CPV aus manuell als interessant markierten Einträgen vorschlagen
- Teams-Benachrichtigung per Webhook
- Export der Treffer als CSV/Excel
