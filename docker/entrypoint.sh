#!/bin/sh
# Datenordner anlegen, dem Benutzer PUID:PGID geben und die App ohne Root-Rechte starten.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA_DIR="${EVC_DATA_DIR:-/data}"

mkdir -p "$DATA_DIR/backups"

if [ "$(id -u)" = "0" ]; then
    chown -R "$PUID:$PGID" "$DATA_DIR"
    exec gosu "$PUID:$PGID" "$@"
fi

exec "$@"
