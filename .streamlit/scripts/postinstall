#!/usr/bin/env bash
set -e

echo "▶ Updating pip & installing Playwright in the app venv"
python -m pip install --upgrade pip setuptools wheel
python -m pip install --upgrade playwright

echo "▶ Installing Chromium browser for Playwright"
python -m playwright install --with-deps chromium

echo "✅ Post-install done"
