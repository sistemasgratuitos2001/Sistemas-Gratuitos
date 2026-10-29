#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$HOME/biblioteca-teste-dados"
export BIBLIO_DATA_DIR="$HOME/biblioteca-teste-dados"
export BIBLIO_KEY="${BIBLIO_KEY:-$(python -c 'import secrets; print(secrets.token_urlsafe(48))')}"

if pgrep -f 'waitress-serve.*app:app' >/dev/null; then
  exit 0
fi

nohup waitress-serve --listen=0.0.0.0:5000 --threads=8 app:app \
  > "$HOME/biblioteca-teste.log" 2>&1 < /dev/null &
