#!/usr/bin/env bash
set -euxo pipefail

# Aggiorna gli strumenti di build
python -m pip install --upgrade pip setuptools wheel

# Installa i browser Playwright
python -m playwright install --with-deps chromium
