# streamlit_app.py — Playwright bootstrap + Anthropic retry + pacing + quiet 429 logs
# - NON modifica la logica del modulo beta
# - Executive Summary: input identico al locale (usa items_ctx, nessun filtro/troncatura)

import os
import sys
import asyncio
import subprocess
import time
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import streamlit as st
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_message

# ──────────────────────────────────────────────────────────────────────────────
# Compatibilità event loop Windows (no-op in Cloud)
# ──────────────────────────────────────────────────────────────────────────────
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────────────────────
# Import del modulo beta (NON MODIFICATO)
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Import del modulo beta (robusto, con diagnostica e fallback)
# ──────────────────────────────────────────────────────────────────────────────
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

# Essenziali
for _name in ["Config","setup_logging","TEStreamScraper","MacroSummarizer","save_report","db_init","db_upsert","db_prune"]:
    if not hasattr(beta, _name):
        st.error(f"Nel modulo mancano simboli essenziali: {_name}")
        st.stop()

Config = getattr(beta, "Config")
setup_logging = getattr(beta, "setup_logging")
TEStreamScraper = getattr(beta, "TEStreamScraper")
MacroSummarizer = getattr(beta, "MacroSummarizer")
save_report = getattr(beta, "save_report")
db_init = getattr(beta, "db_init")
db_upsert = getattr(beta, "db_upsert")
db_prune = getattr(beta, "db_prune")

# Opzionali con fallback
_has_build_selection = hasattr(beta, "build_selection")
_has_db_count = hasattr(beta, "db_count_by_country")
_has_db_load = hasattr(beta, "db_load_recent")

if _has_build_selection:
    build_selection = getattr(beta, "build_selection")
else:
    st.warning("Fallback attivo: `build_selection` non trovato — uso una selezione base ultimi N giorni.")
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

if _has_db_count:
    db_count_by_country = getattr(beta, "db_count_by_country")
else:
    st.info("Fallback: `db_count_by_country` non presente — considero i paesi sempre 'fresh'.")
    def db_count_by_country(conn, country: str) -> int:
        return 0

if _has_db_load:
    db_load_recent = getattr(beta, "db_load_recent")
else:
    st.info("Fallback: `db_load_recent` non presente — userò solo i risultati appena scaricati.")
    def db_load_recent(conn, countries, max_age_days: int):
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap Playwright (libreria + browser) — idempotente e cache-ato
# ──────────────────────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def ensure_playwright_chromium() -> None:
    """
    Garantisce che:
      1) il modulo Python 'playwright' sia disponibile
      2) i binari Chromium siano installati in ~/.cache/ms-playwright
    È idempotente e veloce ai run successivi.
    """
    # 1) Installa la libreria se assente
    try:
        import playwright  # noqa: F401
    except ModuleNotFoundError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright==1.48.0"])

    # 2) Imposta la path vista nei log di errore
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))

    base = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    chromium_present = base.exists() and any(p.name.startswith("chromium") for p in base.glob("chromium-*"))

    # 3) Se Chromium non è presente, scaricalo (prima con --with-deps, poi fallback)
    if not chromium_present:
        try:
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
        except subprocess.CalledProcessError:
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])

# ──────────────────────────────────────────────────────────────────────────────
# Helper: retry 429 Anthropic + single-flight + pacing + silenzia log 429
# ──────────────────────────────────────────────────────────────────────────────
def _retryable(fn, *args, **kwargs):
    """
    Esegue fn con retry/backoff sui classici errori 429 / rate limit di Anthropic.
    Non cambia l'input né l'output del modello: solo ritenta.
    """
    @retry(
        reraise=True,
        retry=retry_if_exception_message(match=r"(?i)(429|rate[_\s-]?limit|acceleration limit)"),
        wait=wait_exponential(multiplier=1.5, min=2, max=30),
        stop=stop_after_attempt(5),
    )
    def _call():
        return fn(*args, **kwargs)
    return _call()

def call_once_per_run(cache_key: str, caller):
    """
    Evita invii doppi dovuti a rerun di Streamlit.
    Se nel run corrente abbiamo già calcolato cache_key, ritorna il valore cached.
    """
    if "api_once_cache" not in st.session_state:
        st.session_state.api_once_cache = {}
    if cache_key in st.session_state.api_once_cache:
        return st.session_state.api_once_cache[cache_key]
    val = caller()
    st.session_state.api_once_cache[cache_key] = val
    return val

def _rough_token_estimate(items):
    """Stima grossolana dei token in input (~1 token ogni 4 caratteri)."""
    total_chars = 0
    for it in items:
        total_chars += len(it.get("title", "") or "")
        total_chars += len(it.get("description", "") or "")
    return max(1, total_chars // 4)

def pace_before_big_request(items, label="Preparazione Executive Summary…"):
    """
    Attesa adattiva prima di inviare richieste molto grandi, per rientrare
    nel rate limit 'acceleration' di Anthropic senza cambiare l'input.
    """
    est_tokens = _rough_token_estimate(items)
    if est_tokens < 30000:
        wait_s = 0
    elif est_tokens < 60000:
        wait_s = 8
    elif est_tokens < 90000:
        wait_s = 18
    else:
        wait_s = 30

    if wait_s <= 0:
        return

    with st.status(f"{label} (attendo {wait_s}s per evitare 429)…", expanded=False) as s:
        for sec in range(wait_s, 0, -1):
            s.update(label=f"{label} (attendo {sec}s)…")
            time.sleep(1)
        s.update(label="Invio ora la richiesta…", state="complete")

class _DropRateLimit(logging.Filter):
    """Filtra i log rumorosi di 429/rate limit durante i retry."""
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

# ──────────────────────────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="StanAI Macro Agent", page_icon="📈", layout="wide")
st.title("📈 StanAI Macro Agent")

left, right = st.columns([1, 2], gap="large")

with left:
    days = st.number_input(
        "Giorni da mostrare nella SELEZIONE",
        min_value=1, max_value=30, value=5, step=1
    )
    run_btn = st.button("Esegui pipeline")

with right:
    st.markdown("**Seleziona i Paesi:**")
    # Menu paesi coerente con il beta
    countries_all = [
        "United States", "Euro Area", "Germany", "United Kingdom",
        "Italy", "France", "China", "Japan", "Spain", "Netherlands", "European Union"
    ]

    c1, c2 = st.columns(2)
    with c1:
        select_all = st.button("Seleziona tutti")
    with c2:
        deselect_all = st.button("Deseleziona tutti")

    if "country_flags" not in st.session_state:
        st.session_state.country_flags = {c: False for c in countries_all}

    if select_all:
        for c in countries_all:
            st.session_state.country_flags[c] = True
    if deselect_all:
        for c in countries_all:
            st.session_state.country_flags[c] = False

    cols = st.columns(2)
    for i, country in enumerate(countries_all):
        col = cols[i % 2]
        st.session_state.country_flags[country] = col.checkbox(
            country,
            value=st.session_state.country_flags.get(country, False),
            key=f"chk_{country}"
        )

    chosen_countries = [c for c, v in st.session_state.country_flags.items() if v]

st.divider()

# ──────────────────────────────────────────────────────────────────────────────
# Esecuzione
# ──────────────────────────────────────────────────────────────────────────────
if run_btn:
    # Log base
    setup_logging()
    cfg = Config()

    # Adapters DB: se mancano i wrapper ma esiste beta.db_load, definiscili ora
    if (db_count_by_country is None) and hasattr(beta, "db_load"):
        def db_count_by_country(conn, country: str) -> int:
            return len(beta.db_load(conn, [country], max_age_days=getattr(cfg, "CONTEXT_DAYS_ES", 30)))
    if (db_load_recent is None) and hasattr(beta, "db_load"):
        def db_load_recent(conn, countries, max_age_days: int):
            return beta.db_load(conn, countries, max_age_days=max_age_days)


    # API Key: usiamo .env / secrets (il modulo beta la gestisce)
    if not cfg.ANTHROPIC_API_KEY:
        st.error("❌ Nessuna ANTHROPIC_API_KEY trovata nel file .env o nei Secrets.")
        st.stop()

    if not chosen_countries:
        st.warning("Seleziona almeno un Paese prima di eseguire.")
        st.stop()

    # Normalizza “European Union” → “Euro Area” per coerenza con il beta
    chosen_norm = ["Euro Area" if x == "European Union" else x for x in chosen_countries]

    st.write(
        f"▶ **Contesto ES:** {cfg.CONTEXT_DAYS} giorni | **Selezione:** {days} giorni | **Paesi:** {', '.join(chosen_norm)}"
    )

    # Assicura playwright+chromium PRIMA di qualunque launch()
    with st.status("Preparazione browser…", expanded=False) as st_status:
        ensure_playwright_chromium()
        st_status.update(label="Browser pronto", state="complete")

    # Pipeline DB/Delta Mode (identica alla logica CLI del beta)
    items_ctx: List[Dict[str, Any]] = []

    with st.status("Aggiornamento cache locale e caricamento notizie…", expanded=False) as st_status:
        try:
            if cfg.DELTA_MODE:
                conn = db_init(cfg.DB_PATH)

                warm, fresh = [], []
                for c in chosen_norm:
                    cnt = db_count_by_country(conn, c)
                    (warm if cnt >= cfg.WARMUP_NEW_COUNTRY_MIN else fresh).append(c)

                scraper = TEStreamScraper(cfg)
                items_new: List[Dict[str, Any]] = []

                # Fresh countries → scraping ampia finestra ES
                if fresh:
                    items_new += scraper.scrape_30d(fresh, max_days=cfg.CONTEXT_DAYS)

                # Warm countries → delta scrape corto
                if warm:
                    items_new += scraper.scrape_30d(warm, max_days=min(cfg.SCRAPE_HORIZON_DAYS, cfg.CONTEXT_DAYS))

                if items_new:
                    db_upsert(conn, items_new)
                    db_prune(conn, max_age_days=cfg.PRUNE_DAYS)

                # Carica dal DB
                items_ctx = db_load_recent(conn, chosen_norm, max_age_days=cfg.CONTEXT_DAYS)

                # Fallback: base scarsa → scrape completo finestra ES
                if len(items_ctx) < 20:
                    all_new = scraper.scrape_30d(chosen_norm, max_days=cfg.CONTEXT_DAYS)
                    if all_new:
                        db_upsert(conn, all_new)
                        items_ctx = db_load_recent(conn, chosen_norm, max_age_days=cfg.CONTEXT_DAYS)
            else:
                scraper = TEStreamScraper(cfg)
                items_ctx = scraper.scrape_30d(chosen_norm, max_days=cfg.CONTEXT_DAYS)

            st_status.update(label=f"Cache aggiornata. Notizie disponibili (finestra {cfg.CONTEXT_DAYS}gg): {len(items_ctx)}", state="complete")
        except Exception as e:
            st.exception(e)
            st.stop()

    if not items_ctx:
        st.error("❌ Nessuna notizia disponibile nella finestra temporale selezionata.")
        st.stop()

    # Executive Summary — **identico al locale** (stessi input items_ctx)
    st.info("Genero l’Executive Summary…")
    try:
        summarizer = MacroSummarizer(cfg.ANTHROPIC_API_KEY, cfg.MODEL, cfg.MODEL_TEMP, cfg.MAX_TOKENS)

        # Pacing adattivo (solo attesa; NON modifica l'input)
        pace_before_big_request(items_ctx, label="Preparazione Executive Summary…")

        # chiave cache solo anti-rerun; input invariato
        es_cache_key = f"es::{len(items_ctx)}::{','.join(chosen_norm)}::{cfg.CONTEXT_DAYS}"

        # Silenzia i log 429 "rumorosi" durante i retry: vedrai solo il risultato finale
        with suppress_rate_limit_logs():
            es_text = call_once_per_run(es_cache_key, lambda: _retryable(
                summarizer.executive_summary, items_ctx, cfg
            ))
    except Exception as e:
        st.exception(e)
        es_text = "Executive Summary non disponibile per errore di generazione."

    st.subheader("Executive Summary")
    st.write(es_text)

    # Selezione ultimi N giorni (+ fill-up) — come da beta
    st.info(f"Costruisco la selezione (ultimi {int(days)} giorni, con fill-up se necessario)…")
    try:
        selection_items = build_selection(items_ctx, int(days), cfg, expand1_days=10, expand2_days=30)
    except TypeError:
        selection_items = build_selection(items_ctx, int(days), cfg)
    except Exception as e:
        st.exception(e)
        st.stop()

    # Traduzione titoli + Riassunti IT — input identico, solo retry e piccola pausa
    st.info("Traduco titoli e genero riassunti in italiano…")
    prog = st.progress(0.0)
    total = max(1, len(selection_items))

    for i, it in enumerate(selection_items, 1):
        # Traduzione titolo (retry 429)
        try:
            it["title_it"] = call_once_per_run(f"ti::{hash(it.get('title',''))}", lambda: _retryable(
                summarizer.translate_it, it.get("title","")
            ))
        except Exception:
            it["title_it"] = it.get("title","") or ""

        # Riassunto IT (retry 429)
        try:
            it["summary_it"] = call_once_per_run(f"si::{hash((it.get('title',''), it.get('time','')))}", lambda: _retryable(
                summarizer.summarize_it, it
            ))
        except Exception:
            it["summary_it"] = (it.get("description","") or "")

        # Pausa “gentile” per smussare picchi (non cambia il contenuto)
        time.sleep(0.2)
        prog.progress(i / total)

    st.success("✅ Pipeline completata.")

    # Anteprima Selezione
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

    # Report DOCX
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

        # Download
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

    # Riepilogo
    st.write("---")
    st.write(f"**Notizie totali nel DB (ultimi {cfg.CONTEXT_DAYS} gg):** {len(items_ctx)}")
    st.write(f"**Notizie selezionate (ultimi {int(days)} gg + fill-up):** {len(selection_items)}")
    st.caption(f"Esecuzione: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
