# streamlit_app.py — allineato a te_macro_agent_final_multi.py
# - Usa SOLO API esposte nel macro agent che mi hai inviato
# - Executive Summary: usa signature (context_items, cfg, chosen_countries)
# - Selezione: build_selection(items_ctx, days, cfg)
# - Riassunti in IT con summarize_item_it; titoli in IT con translate_it
# - DB: db_count_by_country, db_load_recent, db_upsert, db_prune
# - Scraper: scrape_30d
# - Report: save_report(filename, es_text, selection, countries, days, context_count, output_dir)
# - Legge ANTHROPIC_API_KEY/DB_PATH/OUTPUT_DIR dai Secrets → env PRIMA di istanziare Config()

import os
import time
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import streamlit as st
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_message

# ──────────────────────────────────────────────────────────────────────────────
# Import dal macro agent (come definito nel file che mi hai dato)
# ──────────────────────────────────────────────────────────────────────────────
import te_macro_agent_final_multi as ag

Config = ag.Config
setup_logging = ag.setup_logging
TEStreamScraper = ag.TEStreamScraper
MacroSummarizer = ag.MacroSummarizer
build_selection = ag.build_selection
db_init = ag.db_init
db_upsert = ag.db_upsert
db_prune = ag.db_prune
db_count_by_country = ag.db_count_by_country
db_load_recent = ag.db_load_recent
save_report = ag.save_report

# ──────────────────────────────────────────────────────────────────────────────
# Secrets → env (per far sì che Config() trovi la chiave e i path giusti)
# ──────────────────────────────────────────────────────────────────────────────
def _apply_secrets_to_env():
    for k in ("ANTHROPIC_API_KEY", "DB_PATH", "OUTPUT_DIR"):
        if k in st.secrets:
            os.environ[k] = str(st.secrets[k]).strip()
_apply_secrets_to_env()

# ──────────────────────────────────────────────────────────────────────────────
# Playwright bootstrap (il macro usa playwright sync API)
# ──────────────────────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def ensure_playwright_chromium() -> None:
    try:
        import playwright  # noqa: F401
    except ModuleNotFoundError:
        subprocess.check_call([os.sys.executable, "-m", "pip", "install", "playwright==1.48.0"])
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))
    base = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    chromium_present = base.exists() and any(p.name.startswith("chromium") for p in base.glob("chromium-*"))
    if not chromium_present:
        try:
            subprocess.check_call([os.sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
        except subprocess.CalledProcessError:
            subprocess.check_call([os.sys.executable, "-m", "playwright", "install", "chromium"])

# ──────────────────────────────────────────────────────────────────────────────
# Retry helper per le chiamate al modello (409/429 tipici)
# ──────────────────────────────────────────────────────────────────────────────
def _retryable(fn, *args, **kwargs):
    @retry(
        reraise=True,
        retry=retry_if_exception_message(match=r"(?i)(429|rate[_\s-]?limit|Too Many Requests|acceleration limit)"),
        wait=wait_exponential(multiplier=1.5, min=2, max=30),
        stop=stop_after_attempt(5),
    )
    def _call():
        return fn(*args, **kwargs)
    return _call()

def call_once_per_run(cache_key: str, caller):
    if "api_once_cache" not in st.session_state:
        st.session_state.api_once_cache = {}
    if cache_key in st.session_state.api_once_cache:
        return st.session_state.api_once_cache[cache_key]
    val = caller()
    st.session_state.api_once_cache[cache_key] = val
    return val

# ──────────────────────────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="StanAI Macro Agent", page_icon="📈", layout="wide")
st.title("📈 StanAI Macro Agent — UI Streamlit (compatibile)")

# Diagnostica chiave (mascherata) per capire se l'app sta usando i Secrets giusti
_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
masked = f"{_key[:6]}…{_key[-4:]} (len={len(_key)})" if _key else "—"
st.caption(f"ANTHROPIC_API_KEY caricata: {masked}")
if not _key or not _key.startswith("sk-ant-"):
    st.error("Chiave Anthropic mancante/non valida (attesa: 'sk-ant-…'). Correggi i Secrets e poi Rerun.")
    st.stop()

left, right = st.columns([1, 2], gap="large")
with left:
    run_btn = st.button("Esegui pipeline")
    days = st.number_input("Giorni per la SELEZIONE (1–30)", min_value=1, max_value=30, value=5, step=1)
with right:
    cfg_tmp = Config()  # solo per leggere il menu paesi di default dal modulo
    st.markdown("**Seleziona i Paesi (coerenti col macro agent):**")
    countries_all = cfg_tmp.DEFAULT_COUNTRIES_MENU
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
        col = cols[i % 2]
        st.session_state.country_flags[country] = col.checkbox(
            country, value=st.session_state.country_flags.get(country, False), key=f"chk_{country}"
        )
    chosen_countries = [c for c, v in st.session_state.country_flags.items() if v]

st.divider()

# ──────────────────────────────────────────────────────────────────────────────
# RUN
# ──────────────────────────────────────────────────────────────────────────────
if run_btn:
    setup_logging()
    cfg = Config()  # ora che env è popolato dai Secrets, qui trovi anche OUTPUT_DIR/DB_PATH/chiave ecc.

    if not chosen_countries:
        st.warning("Seleziona almeno un Paese.")
        st.stop()

    st.write(f"▶ **Contesto ES (giorni)**: {cfg.CONTEXT_DAYS} | **Selezione**: {days} giorni")
    st.write(f"▶ **Paesi**: {', '.join(chosen_countries)}")

    # Browser pronto (il macro aprirà Playwright in scraper.scrape_30d)
    with st.status("Preparazione browser…", expanded=False) as sst:
        ensure_playwright_chromium()
        sst.update(label="Browser pronto", state="complete")

    # ==== Pipeline dati con DB (Delta Mode) — fedele al macro agent ====
    items_ctx: List[Dict[str, Any]] = []
    with st.status("Carico/aggiorno notizie (DB + stream)…", expanded=False) as sst:
        try:
            scraper = TEStreamScraper(cfg)
            conn = db_init(cfg.DB_PATH)

            warm, fresh = [], []
            for c in chosen_countries:
                cnt = db_count_by_country(conn, c)
                (warm if cnt >= cfg.WARMUP_NEW_COUNTRY_MIN else fresh).append(c)

            items_new = []
            if fresh:
                items_new += scraper.scrape_30d(fresh, max_days=cfg.CONTEXT_DAYS)
            if warm:
                items_new += scraper.scrape_30d(warm, max_days=min(cfg.SCRAPE_HORIZON_DAYS, cfg.CONTEXT_DAYS))

            if items_new:
                db_upsert(conn, items_new)
                db_prune(conn, max_age_days=cfg.PRUNE_DAYS)

            items_ctx = db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)

            # fallback se base DB è scarsa (come nel main del macro)
            if len(items_ctx) < 20:
                items_all = scraper.scrape_30d(chosen_countries, max_days=cfg.CONTEXT_DAYS)
                if items_all:
                    db_upsert(conn, items_all)
                    items_ctx = db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)

            sst.update(label=f"Base pronta: {len(items_ctx)} notizie nel contesto (≤{cfg.CONTEXT_DAYS}gg).", state="complete")
        except Exception as e:
            st.exception(e)
            st.stop()

    if not items_ctx:
        st.error("❌ Nessuna notizia disponibile entro la finestra.")
        st.stop()

    # ==== Executive Summary (firma: (context_items, cfg, chosen_countries)) ====
    st.info("Genero l’Executive Summary…")
    es_error = None
    es_text = ""
    try:
        summarizer = MacroSummarizer(cfg.ANTHROPIC_API_KEY, cfg.MODEL, cfg.MODEL_TEMP, cfg.MAX_TOKENS)
        es_text = call_once_per_run(
            f"es::{len(items_ctx)}::{','.join(chosen_countries)}::{cfg.CONTEXT_DAYS}",
            lambda: _retryable(summarizer.executive_summary, items_ctx, cfg, chosen_countries)
        )
    except Exception as e:
        es_error = str(e)
        es_text = "Executive Summary non disponibile per errore di generazione."

    st.subheader("Executive Summary")
    if es_error:
        st.error(f"Motivo errore ES: {es_error}")
    st.write(es_text)

    # ==== Selezione (ultimi N giorni) ====
    st.info(f"Costruisco la selezione (ultimi {int(days)} giorni)…")
    try:
        selection_items = build_selection(items_ctx, int(days), cfg, expand1_days=10, expand2_days=30)
    except Exception as e:
        st.exception(e)
        st.stop()

    # ==== Traduzione titoli + riassunti IT (signature del macro agent) ====
    st.info("Traduco titoli e genero riassunti in italiano…")
    prog = st.progress(0.0)
    total = max(1, len(selection_items))
    for i, it in enumerate(selection_items, 1):
        # Piccolo delay per “levigare” il rate
        if i > 1:
            time.sleep(0.4)

        # Titolo IT
        try:
            it["title_it"] = call_once_per_run(
                f"ti::{hash(it.get('title',''))}",
                lambda: _retryable(summarizer.translate_it, it.get("title",""), cfg)
            )
        except Exception:
            it["title_it"] = it.get("title","") or ""

        # Riassunto IT (se fallisce, fallback: description originale)
        try:
            it["summary_it"] = call_once_per_run(
                f"si::{hash((it.get('title',''), it.get('time','')))}",
                lambda: _retryable(summarizer.summarize_item_it, it, cfg)
            )
            if not it["summary_it"] or len(it["summary_it"].strip()) < 30:
                raise RuntimeError("Riassunto troppo corto")
        except Exception:
            it["summary_it"] = (it.get("description","") or "")

        prog.progress(i / total)

    st.success("✅ Pipeline completata.")

    # ==== Anteprima tabellare
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
                "summary_it": (it.get("summary_it","") or "")[:160],
            } for it in selection_items]
            st.dataframe(pd.DataFrame(prev_sel), use_container_width=True)
        except Exception:
            st.info("Anteprima non disponibile (pandas mancante).")

    # ==== Report DOCX (firma con context_count)
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"MacroAnalysis_AutoSelect_{int(days)}days_{ts}.docx"
        out_path = save_report(
            filename, es_text, selection_items, chosen_countries,
            int(days), len(items_ctx), cfg.OUTPUT_DIR
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

    # Riepilogo
    st.write("---")
    st.write(f"**Notizie totali nel contesto (≤{cfg.CONTEXT_DAYS} gg):** {len(items_ctx)}")
    st.write(f"**Notizie selezionate (ultimi {int(days)} gg):** {len(selection_items)}")
    st.caption(f"Esecuzione: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
