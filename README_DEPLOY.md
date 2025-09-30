# StanAI Macro Agent — Deploy Notes

## Files you should add to the repo (root):
- `requirements.txt` — pinned to avoid compiling `greenlet` from source and to include Playwright, pandas, python-docx, etc.
- `packages.txt` — minimal OS libraries for Chromium/Playwright on Streamlit Cloud.
- `startup.sh` (optional but recommended) — runs Playwright browser install at build-time.

## Streamlit Cloud
- Make sure your app entrypoint is `streamlit_app.py`.
- Add your `ANTHROPIC_API_KEY` in Streamlit Secrets.
- The app already sets `PLAYWRIGHT_BROWSERS_PATH` and includes an idempotent installer in code.
