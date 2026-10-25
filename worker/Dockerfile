# KI-Worker für den eV-Checker: holt offene Bewertungen bei der App ab und lässt sie von
# Claude Code beurteilen. Läuft als zweiter Dienst in docker-compose.yml (Profil „ki“).
#
# Node 22, weil das npm-Paket von Claude Code mindestens Node 22 verlangt; Python für das Skript
# kommt aus Debian dazu. Der Worker braucht keine weiteren Pakete.
FROM node:22-slim

# Feste Version statt „immer die neueste“: Ein Update ist eine bewusste Änderung an dieser Zeile.
# Liste der Versionen: https://www.npmjs.com/package/@anthropic-ai/claude-code?activeTab=versions
ARG CLAUDE_CODE_VERSION=2.1.300

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1 \
    HOME=/home/node

# Der letzte Befehl prüft, dass das Programm wirklich läuft: Fehlt die plattformabhängige Datei,
# scheitert der Bau hier statt erst im Betrieb. HOME auf /tmp, damit dabei nichts root-eigenes
# in /home/node landet (das Verzeichnis gehört dem Benutzer „node“).
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && HOME=/tmp claude --version

WORKDIR /app
COPY ki_worker.py .

# Läuft als normaler Benutzer (aus dem Node-Image), nicht als root. Claude Code schreibt seine
# Einstellungen nach /home/node – das liegt in docker-compose.yml auf einem eigenen Volume.
USER node

# Alle 5 Minuten nachsehen: Das Nachfragen kostet nichts, Claude wird nur gerufen, wenn wirklich
# etwas offen ist. So ist der Abruf um 7:00 bewertet, bevor um 7:30 die Mail rausgeht.
CMD ["python3", "/app/ki_worker.py", "--wiederholen", "5", "--runden", "4"]
