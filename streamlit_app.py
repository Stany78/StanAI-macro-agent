#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

import streamlit as st

# -----------------------------
# Page Setup
# -----------------------------
st.set_page_config(
    page_title="Macro Agent – TE Scraper",
    page_icon="🕸️",
    layout="wide",
)

st.title("🕸️ Macro Agent – Setup & Scraper")
st.caption("Avvio robusto di Playwright e dipendenze di sistema per eseguire lo scraping in ambienti containerizzati.")

REQUIRED_APT_DEPS: List[str] = [
    "libnss3",
    "libnspr4",
    "libatk1.0-0",
    "libatk-bridge2.0-0",
    "libcups2",
    "libdrm2",
    "libxkbcommon0",
    "libxcomposite1",
    "libxdamage1",
    "libxfixes3",
    "libxrandr2",
    "libgbm1",
    "libpango-1.0-0",
    "libcairo2",
    "libasound2",
    "libatspi2.0-0",
]

PLAYWRIGHT_VERSION = os.environ.get("PLAYWRIGHT_PY_VERSION", "1.48.0")
BROWSERS_PATH = os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))


def is_root() -> bool:
    try:
        return os.geteuid() == 0
    except Exception:
        return False


def check_missing_apt() -> List[str]:
    if not (sys.platform.startswith("linux") and shutil.which("dpkg")):
        return []
    missing = []
    for pkg in REQUIRED_APT_DEPS:
        rc = subprocess.call(["dpkg", "-s", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if rc != 0:
            missing.append(pkg)
    return missing


def apt_install(pkgs: List[str]) -> Tuple[bool, str]:
    try:
        subprocess.check_call(["apt-get", "update"])
        subprocess.check_call(["apt-get", "install", "-y"] + pkgs)
        return True, "Installazione completata."
    except subprocess.CalledProcessError as e:
        return False, f"Installazione fallita: {e}"


def ensure_python_playwright_installed() -> None:
    try:
        import playwright  # noqa: F401
    except ModuleNotFoundError:
        with st.status("Installo il pacchetto Python 'playwright'…", expanded=False):
            subprocess.check_call([sys.executable, "-m", "pip", "install", f"playwright=={PLAYWRIGHT_VERSION}"])


def ensure_chromium_downloaded() -> None:
    base = Path(BROWSERS_PATH)
    chromium_present = base.exists() and any(p.name.startswith("chromium") for p in base.glob("chromium-*"))
    if chromium_present:
        return
    # Try with --with-deps (will no-op on non-apt systems), then fallback
    with st.status("Scarico Chromium per Playwright…", expanded=False):
        try:
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
            return
        except subprocess.CalledProcessError:
            # fallback without deps
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


def run_preflight() -> None:
    """
    Esegue tutti i controlli PRIMA di lanciare qualsiasi browser:
      1) Verifica dipendenze APT e tenta l'installazione se possibile (root + apt-get).
      2) Installa il pacchetto Python playwright se mancante.
      3) Scarica Chromium nei path utente.
    """
    if sys.platform.startswith("linux") and shutil.which("apt-get"):
        missing = check_missing_apt()
        if missing:
            if is_root():
                ok, msg = apt_install(missing)
                if not ok:
                    st.error(
                        "Installazione automatica delle librerie di sistema fallita.\n\n"
                        "Comando equivalente:\n\n"
                        f"```bash\napt-get update && apt-get install -y {' '.join(missing)}\n```"
                    )
                    st.stop()
            else:
                st.warning(
                    "Questo host Linux **non** ha alcune librerie necessarie per avviare i browser di Playwright "
                    "e il processo non dispone dei privilegi necessari per installarle.\n\n"
                    "Esegui sul tuo host (con privilegi elevati):\n\n"
                    f"```bash\nsudo apt-get update && sudo apt-get install {' '.join(missing)}\n```\n"
                    "In alternativa:\n\n"
                    "```bash\nsudo playwright install-deps\n```"
                )
                # Non possiamo procedere: evitiamo di far crashare l'app e chiediamo intervento.
                st.stop()

    # Garantiamo il pacchetto Python e i binari Chromium
    ensure_python_playwright_installed()
    ensure_chromium_downloaded()


def launch_browser_with_safe_flags(playwright, headless: bool = True, slow_mo: int = 0):
    """
    Fornisce una launch() robusta con flag sicuri per ambienti containerizzati.
    Nota: se il tuo modulo di scraping fa la launch internamente, non possiamo forzare questi flag da qui.
    """
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    return playwright.chromium.launch(headless=headless, slow_mo=slow_mo, args=args)


@st.cache_resource(show_spinner=False)
def import_agent_module():
    """
    Import lazy del modulo dell'agente esistente per non rompere gli ambienti dove manca.
    """
    try:
        import te_macro_agent_final_multi as agent
        return agent
    except Exception as e:
        return e  # restituisce l'eccezione per diagnostica


def run_agent_pipeline():
    """
    Esegue la pipeline di scraping già esistente nel tuo progetto, ma con gestione errori migliorata.
    """
    agent = import_agent_module()
    if isinstance(agent, Exception):
        st.error(
            "Impossibile importare il modulo dell'agente (`te_macro_agent_final_multi.py`).\n\n"
            f"Dettagli: {agent}"
        )
        st.stop()

    # Diamo un'opzione per HEADLESS/SLOW_MO direttamente dall'interfaccia
    st.sidebar.header("Impostazioni Browser")
    headless = st.sidebar.checkbox("Headless", value=True)
    slow_mo = st.sidebar.number_input("Slow motion (ms)", min_value=0, max_value=1000, value=0, step=50)
    context_days = st.sidebar.number_input("Giorni di contesto (max_days)", min_value=1, max_value=60, value=30, step=1)

    # Prova EAGER a far girare la tua funzione standard.
    st.subheader("Esecuzione scraping")
    try:
        # Assumiamo che il tuo codice esistente usi un oggetto config con attributi HEADLESS/SLOW_MO/CONTEXT_DAYS
        # Se non esiste, prova a impostare variabili d'ambiente che il tuo modulo possa leggere.
        os.environ["MACRO_AGENT_HEADLESS"] = "1" if headless else "0"
        os.environ["MACRO_AGENT_SLOW_MO_MS"] = str(slow_mo)
        os.environ["MACRO_AGENT_CONTEXT_DAYS"] = str(context_days)

        # La tua app originale probabilmente definisce una funzione "main()" o simili.
        # Se in streamlit_app.py originale c'era una chiamata diretta a scraper.scrape_30d, la lasciamo al modulo.
        if hasattr(agent, "main"):
            with st.status("Eseguo l'agente…", expanded=False):
                result = agent.main()
            st.success("Scraping completato.")
            st.write(result if result is not None else "Done.")
        else:
            # fallback: prova ad eseguire una funzione standardizzata
            if hasattr(agent, "run"):
                with st.status("Eseguo l'agente…", expanded=False):
                    result = agent.run()
                st.success("Scraping completato.")
                st.write(result if result is not None else "Done.")
            else:
                st.info("Non ho trovato `main()` né `run()` nel modulo agente. Controlla le funzioni esportate.")
    except Exception as e:
        msg = str(e)
        # Se è il classico errore di Playwright per dipendenze di sistema assenti, spiega la correzione
        banner = "Host system is missing dependencies to run browsers"
        if banner in msg:
            st.error(
                "Playwright non può avviare il browser perché mancano librerie di sistema sull'host.\n\n"
                "Soluzione consigliata sull'host:\n\n"
                "```bash\nsudo apt-get update && sudo apt-get install \\\n"
                "libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 "
                "libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 "
                "libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0\n```\n"
                "Oppure: `sudo playwright install-deps`"
            )
        else:
            st.exception(e)


def developer_tools():
    with st.expander("🔧 Strumenti sviluppatore / diagnostica", expanded=False):
        st.write("**Ambiente**")
        st.json({
            "python": sys.version,
            "platform": sys.platform,
            "is_root": is_root(),
            "apt_get": bool(shutil.which("apt-get")),
            "dpkg": bool(shutil.which("dpkg")),
            "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
        })
        missing = check_missing_apt()
        if missing:
            st.warning("Pacchetti APT mancanti: " + ", ".join(missing))
        else:
            st.success("Nessuna dipendenza APT mancante rilevata (o sistema non Debian-like).")

        if st.button("Riprova preflight"):
            run_preflight()
            st.success("Preflight completato.")


# -----------------------------
# UI Flow
# -----------------------------
with st.sidebar:
    st.header("Preflight")
    st.write("Esegue controlli e installazioni necessarie per Playwright.")

    if st.button("Esegui Preflight"):
        run_preflight()
        st.success("Preflight completato.")

    st.divider()
    st.header("Avvio rapido")
    st.caption("Seleziona per eseguire automaticamente il preflight prima dell'agente.")
    auto_preflight = st.checkbox("Esegui preflight automaticamente", value=True)

developer_tools()

if auto_preflight:
    run_preflight()

run_agent_pipeline()

st.caption("Se compaiono errori di permessi o pacchetti di sistema mancanti, segui le istruzioni mostrate sopra.")
