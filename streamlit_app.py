# streamlit_app.py — Streamlit UI robusta con preflight Playwright e retry Anthropic
# Mantiene la logica del modulo beta, con adattatori per differenze di versione.

import os
import sys
import asyncio
import subprocess
import time
import logging
import shutil
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import streamlit as st
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_message

# ────────────────────────────────────────────────────────────────────────────────
# Compatibilità event loop Windows (no-op su Linux/Cloud)
# ────────────────────────────────────────────────────────────────────────────────
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# ────────────────────────────────────────────────────────────────────────────────
# Import modulo beta con diagnostica chiara (senza vincolare i nomi subito)
# ────────────────────────────────────────────────────────────────────────────────
import importlib
from types import ModuleType
from pathlib import Path as _P

_THIS_DIR = _P(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

def _import_beta() -> ModuleType:
    try:
        return importlib.import_module("te_macro_agent_final_multi")
    except Exception as e:
        st.error(f"""
### ❌ Errore durante l'import del modulo `te_macro_agent_final_multi.py`

Assicurati che:
- il file sia nella stessa cartella dell'app Streamlit
- tutte le dipendenze del modulo siano installate

**Dettagli:** {type(e).__name__}: {e}
""")
        st.stop()

beta = _import_beta()

# Essenziali (verifica presenza)
for _name in ["Config","setup_logging","TEStreamScraper","MacroSummarizer","save_report","db_init","db_upsert","db_prune"]:
    if not hasattr(beta, _name):
        st.error(f"Nel modulo mancano simboli essenziali: {_name}")
        st.stop()

# Bind essenziali
Config = getattr(beta, "Config")
setup_logging = getattr(beta, "setup_logging")
TEStreamScraper = getattr(beta, "TEStreamScraper")
MacroSummarizer = getattr(beta, "MacroSummarizer")
save_report = getattr(beta, "save_report")
db_init = getattr(beta, "db_init")
db_upsert = getattr(beta, "db_upsert")
db_prune = getattr(beta, "db_prune")

# Flag su simboli opzionali (useremo adapter dopo aver creato cfg)
_HAS_BUILD_SELECTION = hasattr(beta, "build_selection")
_HAS_BUILD_SELECTION_FRESHFIRST = hasattr(beta, "build_selection_freshfirst")
_HAS_DB_COUNT = hasattr(beta, "db_count_by_country")
_HAS_DB_LOAD_RECENT = hasattr(beta, "db_load_recent")
_HAS_DB_LOAD = hasattr(beta, "db_load")

# ────────────────────────────────────────────────────────────────────────────────
# Playwright preflight
# ────────────────────────────────────────────────────────────────────────────────
REQUIRED_APT = [
    "libnss3","libnspr4","libatk1.0-0","libatk-bridge2.0-0","libcups2","libdrm2",
    "libxkbcommon0","libxcomposite1","libxdamage1","libxfixes3","libxrandr2",
    "libgbm1","libpango-1.0-0","libcairo2","libasound2","libatspi2.0-0",
]

@st.cache_resource(show_spinner=False)
def ensure_playwright_chromium() -> None:
    # Linux: verifica librerie di sistema
    if sys.platform.startswith("linux") and shutil.which("apt-get") and shutil.which("dpkg"):
        missing = []
        for pkg in REQUIRED_APT:
            rc = subprocess.call(["dpkg","-s",pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if rc != 0:
                missing.append(pkg)
        if missing:
            try:
                is_root = (os.geteuid() == 0)
            except Exception:
                is_root = False
            if is_root:
                with st.status("Installazione librerie di sistema Playwright…", expanded=False):
                    subprocess.check_call(["apt-get","update"])
                    subprocess.check_call(["apt-get","install","-y"] + missing)
            else:
                st.error(f"""
Playwright non può avviare Chromium perché mancano librerie di sistema sull'host.

Esegui sul terminale:

```bash
sudo apt-get update && sudo apt-get install \\
{' '.join(missing)}
```

Oppure:
```bash
sudo playwright install-deps
```
""")
                st.stop()

    # pacchetto Python
    try:
        import playwright  # noqa: F401
    except ModuleNotFoundError:
        subprocess.check_call([sys.executable,"-m","pip","install","playwright==1.48.0"])

    # browsers path + chromium
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home()/".cache"/"ms-playwright"))
    base = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    has_chromium = base.exists() and any(p.name.startswith("chromium") for p in base.glob("chromium-*"))
    if not has_chromium:
        try:
            subprocess.check_call([sys.executable,"-m","playwright","install","--with-deps","chromium"])
        except subprocess.CalledProcessError:
            subprocess.check_call([sys.executable,"-m","playwright","install","chromium"])

# ────────────────────────────────────────────────────────────────────────────────
# Anthropic: retry 429 + pacing + log filter
# ────────────────────────────────────────────────────────────────────────────────
def _retryable(fn, *args, **kwargs):
    @retry(
        reraise=True,
        retry=retry_if_exception_message(match=r"(?i)(429|rate[_\\s-]?limit|acceleration limit)"),
        wait=wait_exponential(multiplier=1.5, min=2, max=30),
        stop=stop_after_attempt(5),
    )
    def _call():
        return fn(*args, **kwargs)
    return _call()

def call_once_per_run(key: str, caller):
    if "once_cache" not in st.session_state:
        st.session_state.once_cache = {}
    if key in st.session_state.once_cache:
        return st.session_state.once_cache[key]
    val = caller()
    st.session_state.once_cache[key] = val
    return val

class _DropRateLimit(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        m = record.getMessage().lower()
        return not ("rate_limit" in m or "429" in m or "acceleration limit" in m)

@contextmanager
def suppress_rate_limit_logs():
    root = logging.getLogger()
    flt = _DropRateLimit()
    root.addFilter(flt)
    try:
        yield
    finally:
        root.removeFilter(flt)

def _rough_token_estimate(items):
    total = 0
    for it in items:
        total += len(it.get("title","") or "") + len(it.get("description","") or "")
    return max(1, total//4)

def pace_before_big_request(items, label="Preparazione Executive Summary…"):
    est = _rough_token_estimate(items)
    wait_s = 0
    if est >= 90000: wait_s = 30
    elif est >= 60000: wait_s = 18
    elif est >= 30000: wait_s = 8
    if wait_s <= 0: return
    with st.status(f"{label} (attendo {wait_s}s)…", expanded=False) as s:
        for sec in range(wait_s, 0, -1):
            s.update(label=f"{label} (attendo {sec}s)…")
            time.sleep(1)
        s.update(label="Invio ora la richiesta…", state="complete")

# ────────────────────────────────────────────────────────────────────────────────
# UI
# ────────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="StanAI Macro Agent", page_icon="📈", layout="wide")
st.title("📈 StanAI Macro Agent")

left, right = st.columns([1,2], gap="large")
with left:
    days = st.number_input("Giorni da mostrare nella SELEZIONE", min_value=1, max_value=30, value=5, step=1)
    run_btn = st.button("Esegui pipeline")

with right:
    st.markdown("**Seleziona i Paesi:**")
    countries_all = [
        "United States","Euro Area","Germany","United Kingdom",
        "Italy","France","China","Japan","Spain","Netherlands","European Union"
    ]
    c1, c2 = st.columns(2)
    with c1: select_all = st.button("Seleziona tutti")
    with c2: deselect_all = st.button("Deseleziona tutti")
    if "country_flags" not in st.session_state:
        st.session_state.country_flags = {c: False for c in countries_all}
    if select_all:
        for c in countries_all: st.session_state.country_flags[c] = True
    if deselect_all:
        for c in countries_all: st.session_state.country_flags[c] = False
    cols = st.columns(2)
    for i, country in enumerate(countries_all):
        col = cols[i%2]
        st.session_state.country_flags[country] = col.checkbox(
            country, value=st.session_state.country_flags.get(country, False), key=f"chk_{country}"
        )
    chosen = [c for c,v in st.session_state.country_flags.items() if v]

st.divider()

# ────────────────────────────────────────────────────────────────────────────────
# Pipeline
# ────────────────────────────────────────────────────────────────────────────────
if run_btn:
    setup_logging()
    cfg = Config()

    # Defaults compat
    WARMUP_NEW_COUNTRY_MIN = getattr(cfg, 'WARMUP_NEW_COUNTRY_MIN', 20)
    SCRAPE_HORIZON_DAYS = getattr(cfg, 'SCRAPE_HORIZON_DAYS', min(int(getattr(cfg, 'CONTEXT_DAYS_ES', 30)), 7))

    if not getattr(cfg, "ANTHROPIC_API_KEY", None):
        st.error("❌ Nessuna ANTHROPIC_API_KEY trovata nel file .env o nei Secrets.")
        st.stop()

    if not chosen:
        st.warning("Seleziona almeno un Paese prima di eseguire.")
        st.stop()

    chosen_norm = ["Euro Area" if x == "European Union" else x for x in chosen]
    st.write(f"▶ **Contesto ES:** {cfg.CONTEXT_DAYS_ES} giorni | **Selezione:** {days} giorni | **Paesi:** {', '.join(chosen_norm)}")

    # Costruisci adapter ora che cfg esiste
    # build_selection
    if _HAS_BUILD_SELECTION:
        build_selection = getattr(beta, "build_selection")
    elif _HAS_BUILD_SELECTION_FRESHFIRST:
        st.info("Adapter: uso build_selection_freshfirst del modulo.")
        def build_selection(items_ctx, days, cfg, expand1_days=10, expand2_days=30):
            max_news = getattr(cfg, "MAX_NEWS", 12)
            return beta.build_selection_freshfirst(items_stream=items_ctx, items_cache=[], days_N=int(days), max_news=max_news)
    else:
        st.warning("Fallback attivo: `build_selection` non trovato — uso selezione base ultimi N giorni.")
        from datetime import datetime, timezone
        def _parse_time(ts):
            if isinstance(ts, (int, float)):
                try:
                    return datetime.fromtimestamp(ts, tz=timezone.utc)
                except Exception:
                    return None
            if not ts:
                return None
            for fmt in ("%Y-%m-%dT%H:%M:%S%z","%Y-%m-%dT%H:%M:%S.%f%z","%Y-%m-%d %H:%M:%S%z",
                        "%Y-%m-%dT%H:%M:%S","%Y-%m-%d %H:%M:%S","%Y-%m-%d"):
                try:
                    from datetime import datetime as _dt
                    dt = _dt.strptime(ts, fmt)
                    if "%z" not in fmt:
                        from datetime import timezone as _tz
                        dt = dt.replace(tzinfo=_tz.utc)
                    return dt
                except Exception:
                    continue
            return None
        def build_selection(items_ctx, days, cfg, expand1_days=10, expand2_days=30):
            now = datetime.now(timezone.utc)
            out = []
            for it in items_ctx:
                age = it.get("age_days")
                if isinstance(age, (int, float)):
                    if age <= days: out.append(it); continue
                ts = _parse_time(it.get("time"))
                if ts is None or (now - ts).days <= days:
                    out.append(it)
            def _key(it):
                ts = _parse_time(it.get("time"))
                return ts or now
            out.sort(key=_key, reverse=True)
            return out

    # DB adapters
    if _HAS_DB_COUNT:
        db_count_by_country = getattr(beta, "db_count_by_country")
    elif _HAS_DB_LOAD:
        def db_count_by_country(conn, country: str) -> int:
            return len(getattr(beta, "db_load")(conn, [country], max_age_days=getattr(cfg, "CONTEXT_DAYS_ES", 30)))
    else:
        st.info("Fallback: `db_count_by_country` non presente — considero i paesi sempre 'fresh'.")
        def db_count_by_country(conn, country: str) -> int: return 0

    if _HAS_DB_LOAD_RECENT:
        db_load_recent = getattr(beta, "db_load_recent")
    elif _HAS_DB_LOAD:
        def db_load_recent(conn, countries, max_age_days: int):
            return getattr(beta, "db_load")(conn, countries, max_age_days=max_age_days)
    else:
        st.info("Fallback: `db_load_recent` non presente — userò solo i risultati appena scaricati.")
        def db_load_recent(conn, countries, max_age_days: int): return []

    # Preflight Playwright
    with st.status("Preparazione browser…", expanded=False):
        ensure_playwright_chromium()

    # Delta mode attivo solo se helper DB disponibili (ora che adapter sono creati)
    delta_ok = (db_count_by_country is not None) and (db_load_recent is not None)
    if not delta_ok:
        cfg.DELTA_MODE = False

    items_ctx: List[Dict[str,Any]] = []

    with st.status("Caricamento notizie…", expanded=False) as s:
        try:
            scraper = TEStreamScraper(cfg)
            if cfg.DELTA_MODE:
                conn = db_init(cfg.DB_PATH)
                warm, fresh = [], []
                for c in chosen_norm:
                    cnt = db_count_by_country(conn, c)
                    (warm if cnt >= WARMUP_NEW_COUNTRY_MIN else fresh).append(c)
                items_new: List[Dict[str,Any]] = []
                if fresh:
                    items_new += scraper.scrape_stream(fresh, horizon_days=cfg.CONTEXT_DAYS_ES)
                if warm:
                    items_new += scraper.scrape_stream(warm, horizon_days=min(SCRAPE_HORIZON_DAYS, cfg.CONTEXT_DAYS_ES))
                if items_new:
                    db_upsert(conn, items_new)
                    db_prune(conn, max_age_days=cfg.PRUNE_DAYS)
                items_ctx = db_load_recent(conn, chosen_norm, max_age_days=cfg.CONTEXT_DAYS_ES)
                if len(items_ctx) < 20:
                    all_new = scraper.scrape_stream(chosen_norm, horizon_days=cfg.CONTEXT_DAYS_ES)
                    if all_new:
                        db_upsert(conn, all_new)
                        items_ctx = db_load_recent(conn, chosen_norm, max_age_days=cfg.CONTEXT_DAYS_ES)
            else:
                items_ctx = scraper.scrape_stream(chosen_norm, horizon_days=cfg.CONTEXT_DAYS_ES)
            s.update(label=f"Notizie disponibili (finestra {cfg.CONTEXT_DAYS_ES}gg): {len(items_ctx)}", state="complete")
        except Exception as e:
            msg = str(e)
            if "Host system is missing dependencies to run browsers" in msg:
                st.error("""
Playwright non può avviare il browser perché mancano librerie di sistema sull'host.

Esegui:

```bash
sudo apt-get update && sudo apt-get install \
libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0
```

Oppure:
```bash
sudo playwright install-deps
```
""")
            else:
                st.exception(e)
            st.stop()

    if not items_ctx:
        st.error("❌ Nessuna notizia disponibile nella finestra temporale selezionata.")
        st.stop()

    # Executive Summary
    st.info("Genero l’Executive Summary…")
    try:
        summarizer = MacroSummarizer(cfg.ANTHROPIC_API_KEY, cfg.MODEL, cfg.MODEL_TEMP, cfg.MAX_TOKENS)
        pace_before_big_request(items_ctx, label="Preparazione Executive Summary…")
        cache_key = f"es::{len(items_ctx)}::{','.join(chosen_norm)}::{cfg.CONTEXT_DAYS_ES}"
        with suppress_rate_limit_logs():
            es_text = call_once_per_run(cache_key, lambda: _retryable(
                summarizer.executive_summary, items_ctx, cfg
            ))
    except Exception as e:
        st.exception(e)
        es_text = "Executive Summary non disponibile per errore di generazione."

    st.subheader("Executive Summary")
    st.write(es_text)

    # Selezione
    st.info(f"Costruisco la selezione (ultimi {int(days)} giorni)…")
    try:
        try:
            selection_items = build_selection(items_ctx, int(days), cfg, expand1_days=10, expand2_days=30)
        except TypeError:
            selection_items = build_selection(items_ctx, int(days), cfg)
    except Exception as e:
        st.exception(e)
        st.stop()

    # Traduzioni IT
    st.info("Traduco titoli e genero riassunti in italiano…")
    prog = st.progress(0.0)
    total = max(1, len(selection_items))
    for i, it in enumerate(selection_items, 1):
        try:
            it["title_it"] = call_once_per_run(f"ti::{hash(it.get('title',''))}", lambda: _retryable(
                summarizer.translate_it, it.get("title","")
            ))
        except Exception:
            it["title_it"] = it.get("title","") or ""
        try:
            it["summary_it"] = call_once_per_run(f"si::{hash((it.get('title',''), it.get('time','')))}", lambda: _retryable(
                summarizer.summarize_it, it
            ))
        except Exception:
            it["summary_it"] = (it.get("description","") or "")
        time.sleep(0.2)
        prog.progress(i/total)

    st.success("✅ Pipeline completata.")

    # Anteprima
    with st.expander("Anteprima Selezione"):
        try:
            import pandas as pd
            prev_sel = [{
                "time": it.get("time",""),
                "age_days": it.get("age_days",""),
                "country": it.get("country",""),
                "importance": it.get("importance",0),
                "score": it.get("score",0),
                "category": it.get("category_mapped",""),
                "title_it": (it.get("title_it","") or "")[:120],
            } for it in selection_items]
            st.dataframe(pd.DataFrame(prev_sel), use_container_width=True)
        except Exception:
            st.info("Anteprima non disponibile (pandas mancante).")

    # Report
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"MacroAnalysis_AutoSelect_{int(days)}days_{ts}.docx"
        out_path = save_report(
            filename=filename,
            es_text=es_text,
            selection=selection_items,
            countries=chosen_norm,
            days=int(days),
            context_count=len(items_ctx),
            output_dir=cfg.OUTPUT_DIR,
        )
        st.info(f"Report salvato su disco: `{out_path}`")
        try:
            data = Path(out_path).read_bytes()
            st.download_button(
                "📥 Scarica report DOCX",
                data=data,
                file_name=Path(out_path).name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        except Exception as e:
            st.warning(f"Report creato ma non scaricabile ora: {e}")
    except Exception as e:
        st.error(f"Errore nella generazione/salvataggio DOCX: {e}")

    st.write("---")
    st.write(f"**Notizie totali nel DB (ultimi {cfg.CONTEXT_DAYS_ES} gg):** {len(items_ctx)}")
    st.write(f"**Notizie selezionate (ultimi {int(days)} gg):** {len(selection_items)}")
    st.caption(f"Esecuzione: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
