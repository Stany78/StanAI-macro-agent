# Auto-setup for Playwright browsers on Streamlit Cloud without touching app code.
# Python auto-imports this module if present on sys.path (see `site` docs).
import os, sys, subprocess, pathlib

def _chromium_installed() -> bool:
    base = pathlib.Path.home() / ".cache" / "ms-playwright"
    if not base.exists():
        return False
    try:
        for p in base.rglob("*"):
            if p.name.startswith("chromium"):
                return True
    except Exception:
        pass
    return False

def _ensure_playwright_chromium():
    try:
        import playwright  # noqa: F401
    except Exception:
        return  # playwright non installato via pip -> nessuna azione
    if _chromium_installed():
        return
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    except Exception:
        # Fallback senza --with-deps (su ambienti dove le deps APT sono già presenti)
        try:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        except Exception:
            pass  # come ultima spiaggia, lascia proseguire: l'app potrà gestire l'errore

# Evita di fare lavoro inutile in dev locale ripetutamente
if os.environ.get("STREAMLIT_CLOUD", "1") == "1" or os.environ.get("WEBSERVICE_PLATFORM","") == "streamlit":
    _ensure_playwright_chromium()
