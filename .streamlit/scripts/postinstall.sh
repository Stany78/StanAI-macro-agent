#!/usr/bin/env bash
set -euxo pipefail

# Usa il python corretto del venv
PYBIN="$(python -c 'import sys; print(sys.executable)')"

# Ripulisce la cache corrotta di Playwright (se esiste)
rm -rf /home/appuser/.cache/ms-playwright || true

# Installa Playwright dentro il venv
"$PYBIN" -m pip install --upgrade --no-cache-dir playwright

# Installa Chromium e le dipendenze
export PLAYWRIGHT_BROWSERS_PATH=0
"$PYBIN" -m playwright install --with-deps chromium
