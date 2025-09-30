# Auto-install Playwright browsers at interpreter startup (works on Streamlit Cloud).
# Put this file in the project root. Python auto-imports "sitecustomize" if present on sys.path.
import os, sys, subprocess, pathlib

# Where Playwright caches browsers on Streamlit Cloud
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(pathlib.Path.home() / ".cache" / "ms-playwright"))

def _chromium_installed(base: pathlib.Path) -> bool:
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
        # Playwright package not installed -> requirements.txt must include "playwright"
        return
    base = pathlib.Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    if _chromium_installed(base):
        return
    # Try with deps first; if the image already has deps, fallback without
    cmds = [
        [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
        [sys.executable, "-m", "playwright", "install", "chromium"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            break
        except Exception:
            continue

_ensure_playwright_chromium()
