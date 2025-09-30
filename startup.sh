#!/usr/bin/env bash
set -euxo pipefail

# Ensure modern build tooling (and match the versions pinned in requirements)
python -m pip install --upgrade pip setuptools wheel

# Install Playwright browser binaries + system dependencies
python -m playwright install --with-deps chromium
