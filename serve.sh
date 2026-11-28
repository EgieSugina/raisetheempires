#!/usr/bin/env bash
# Run the Raise the Empires server from source.
#
#   ./serve.sh                 # start on http://127.0.0.1:5005/
#   ./serve.sh --port 8080     # any empires-server.py flag is passed through
#   SKIP_INSTALL=1 ./serve.sh  # skip the dependency check (faster restarts)
#
# Creates/reuses a .venv in the repo root and installs requirements.txt there.
# Binds to 127.0.0.1 only. Do NOT pass --host 0.0.0.0 on an untrusted network:
# the server has no authentication and several state-changing GET routes.

set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

VENV="${VENV:-.venv}"
PYTHON="${PYTHON:-python3}"

if [ ! -d "$VENV" ]; then
    echo ">> creating virtualenv in $VENV"
    "$PYTHON" -m venv "$VENV"
    NEED_INSTALL=1
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

if [ -z "${SKIP_INSTALL:-}" ] && { [ -n "${NEED_INSTALL:-}" ] || ! python -c "import flask, flask_socketio, pyamf" 2>/dev/null; }; then
    echo ">> installing dependencies from requirements.txt"
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
fi

# When running from source the save.db lives in instance/, but the server only
# creates that folder while migrating an older save — a fresh checkout would die
# with "sqlite3.OperationalError: unable to open database file".
mkdir -p instance

# Report the right URL even when the caller overrides the port.
PORT=5005
prev=""
for arg in "$@"; do
    case "$prev" in --port) PORT="$arg" ;; esac
    case "$arg" in --port=*) PORT="${arg#--port=}" ;; esac
    prev="$arg"
done

echo ">> starting server:    http://127.0.0.1:${PORT}/"
echo ">> Ruffle (no Flash):  http://127.0.0.1:${PORT}/ruffle.html"
echo

# --no-popup: don't auto-launch a browser (the bundled chromium/ is Windows-only).
exec python empires-server.py --no-popup "$@"
