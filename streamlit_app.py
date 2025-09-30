import os
import sys
import time
import json
import logging
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# Robust bootstrap: ensure Playwright + Chromium are present at runtime
# (idempotente: se già installati non fa nulla)
# ──────────────────────────────────────────────────────────────────────────────

def ensure_playwright_chromium():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    try:
        import playwright  # noqa: F401
    except ModuleNotFoundError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright==1.48.0"])  # light-weight
    # assicura i binari del browser (con fallback senza --with-deps)
    try:
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"]) 
    except subprocess.CalledProcessError:
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"]) 


# ──────────────────────────────────────────────────────────────────────────────
# Importa l'agente macro SENZA MODIFICARLO
# ──────────────────────────────────────────────────────────────────────────────
from te_macro_agent_final_multi import (
    Config,
    TEStreamScraper,
    MacroSummarizer,
    db_init,
    db_upsert,
    db_prune,
    db_load_recent,
    build_selection,
    save_report,
)

# ──────────────────────────────────────────────────────────────────────────────
# Utility UI
# ──────────────────────────────────────────────────────────────────────────────

def get_api_key_from_env_or_ui() -> str:
    # Priorità: secrets → env → input utente
    key = st.secrets.get("ANTHROPIC_API_KEY", None) if hasattr(st, "secrets") else None
    if not key:
        key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        with st.sidebar:
            key = st.text_input("ANTHROPIC_API_KEY", type="password", help="Inserisci la tua API key Anthropic")
    return key or ""


def countries_menu(cfg: Config) -> List[str]:
    default = ["United States", "Euro Area"]
    all_countries = cfg.DEFAULT_COUNTRIES_MENU
    return st.multiselect(
        "Seleziona i Paesi (finestra ES = 60 giorni)",
        options=all_countries,
        default=default,
    )


def render_selection_table(rows: List[Dict[str, Any]]):
    import pandas as pd
    if not rows:
        st.info("Nessun elemento nella selezione.")
        return
    # colonne utili e ordine leggibile
    cols = [
        "country", "time", "category_mapped", "importance", "score",
        "title_it", "title", "summary_it", "age_days"
    ]
    # normalizza
    norm = []
    for r in rows:
        d = {k: r.get(k) for k in cols}
        norm.append(d)
    df = pd.DataFrame(norm)
    st.dataframe(df, use_container_width=True, hide_index=True)


# ──────────────────────────────────────────────────────────────────────────────
# App Streamlit
# ──────────────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="TE Macro Agent — AutoSelect", layout="wide")
    st.title("🏦 Macro Markets Analysis — AutoSelect (ES 60gg + Delta DB)")

    ensure_playwright_chromium()

    # Config di base dal modulo
    cfg = Config()
    cfg.HEADLESS = True

    # Sidebar: chiave API, modalità delta, cartella output
    with st.sidebar:
        st.header("Impostazioni")
        api_key = get_api_key_from_env_or_ui()
        cfg.ANTHROPIC_API_KEY = api_key

        cfg.DELTA_MODE = st.toggle("Delta Mode (DB + stream)", value=True, help="Velocizza aggiornando solo i paesi già presenti in DB")
        cfg.SCRAPE_HORIZON_DAYS = st.slider("Scrape orizzonte (giorni)", 3, 14, value=7)
        cfg.PRUNE_DAYS = st.slider("Prune DB (giorni)", 30, 90, value=60)

        # path DB e output nella working dir dell'app
        workdir = Path.cwd()
        cfg.DB_PATH = str(workdir / "news_cache.sqlite")
        cfg.OUTPUT_DIR = str(workdir)
        st.caption(f"DB Path: {cfg.DB_PATH}")

    # Parametri run
    selection_days = st.slider("Mostra notizie degli ultimi N giorni", min_value=1, max_value=30, value=7)
    chosen_countries = countries_menu(cfg)

    col1, col2, col3 = st.columns([1,1,1])
    go = col1.button("🚀 Esegui analisi")
    do_scrape_only = col2.button("🧹 Solo aggiorna DB (scrape)")
    do_clear = col3.button("🗑️ Pulisci DB (prune immediato)")

    # Se l'utente vuole solo pulire il DB
    if do_clear:
        conn = db_init(cfg.DB_PATH)
        db_prune(conn, max_age_days=cfg.PRUNE_DAYS)
        conn.close()
        st.success("DB ripulito.")
        st.stop()

    if not (go or do_scrape_only):
        st.info("Imposta i parametri e premi **Esegui analisi** oppure **Solo aggiorna DB**.")
        st.stop()

    if not cfg.ANTHROPIC_API_KEY:
        st.error("ANTHROPIC_API_KEY mancante. Inseriscila nei Secrets o nella sidebar.")
        st.stop()

    # Log area
    log_area = st.empty()
    def log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        log_area.info(f"[{ts}] {msg}")

    # Avvio
    start_ts = time.time()
    log("Inizializzo DB…")
    conn = db_init(cfg.DB_PATH)

    try:
        # Warm/Delta logic + scraping
        from te_macro_agent_final_multi import TEStreamScraper, db_count_by_country
        scraper = TEStreamScraper(cfg)

        if do_scrape_only:
            log("Eseguo scraping completo (DeltaMode rispetta la finestra ridotta per paesi caldi)…")
        else:
            log("Preparo contesto ES e selezione…")

        items_new: List[Dict[str, Any]] = []
        items_ctx: List[Dict[str, Any]] = []

        if cfg.DELTA_MODE:
            # determinazione paesi "caldi" (già in DB) vs "freschi"
            warm, fresh = [], []
            for c in chosen_countries:
                cnt = db_count_by_country(conn, c)
                (warm if cnt >= cfg.WARMUP_NEW_COUNTRY_MIN else fresh).append(c)

            if fresh:
                log(f"Scrape iniziale (<= {cfg.CONTEXT_DAYS} gg) per nuovi paesi: {', '.join(fresh)}")
                items_new += scraper.scrape_30d(fresh, max_days=cfg.CONTEXT_DAYS)
            if warm:
                log(f"Delta scrape (<= {cfg.SCRAPE_HORIZON_DAYS} gg) per paesi caldi: {', '.join(warm)}")
                items_new += scraper.scrape_30d(warm, max_days=min(cfg.SCRAPE_HORIZON_DAYS, cfg.CONTEXT_DAYS))

            if items_new:
                db_upsert(conn, items_new)
                db_prune(conn, max_age_days=cfg.PRUNE_DAYS)

            items_ctx = db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)

            if not items_ctx or len(items_ctx) < 20:
                log("Base DB scarsa: fallback a scrape completo finestra ES per tutti i paesi scelti…")
                items_all = scraper.scrape_30d(chosen_countries, max_days=cfg.CONTEXT_DAYS)
                if items_all:
                    db_upsert(conn, items_all)
                    items_ctx = db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)
        else:
            log("Scrape diretto (no DB delta)…")
            items_ctx = scraper.scrape_30d(chosen_countries, max_days=cfg.CONTEXT_DAYS)

        if not items_ctx:
            st.error("Nessuna notizia disponibile nella finestra temporale.")
            st.stop()

        if do_scrape_only:
            st.success(f"DB aggiornato. Notizie in contesto: {len(items_ctx)}")
            render_selection_table(items_ctx)
            st.stop()

        # Executive Summary
        log("Genero Executive Summary (Anthropic)…")
        summarizer = MacroSummarizer(cfg.ANTHROPIC_API_KEY, cfg.MODEL, cfg.MODEL_TEMP, cfg.MAX_TOKENS)
        es_text = summarizer.executive_summary(items_ctx, cfg, chosen_countries)

        # Selezione ultimi N giorni + fill-up
        log("Costruisco selezione (ultimi N giorni + fill-up)…")
        selection_items = build_selection(items_ctx, selection_days, cfg, expand1_days=10, expand2_days=30)

        # Traduzioni & riassunti IT per i selezionati
        log("Traduco titoli e creo riassunti brevi in IT…")
        for it in selection_items:
            try:
                it["title_it"] = summarizer.translate_it(it.get("title", ""), cfg)
            except Exception:
                it["title_it"] = it.get("title", "")
            try:
                it["summary_it"] = summarizer.summarize_item_it(it, cfg)
            except Exception:
                it["summary_it"] = (it.get("description", "") or "")

        # Report DOCX + download
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"MacroAnalysis_AutoSelect_{selection_days}days_{ts}.docx"
        out_path = save_report(filename, es_text, selection_items, chosen_countries, selection_days, len(items_ctx), cfg.OUTPUT_DIR)

        # Output UI
        st.subheader("Executive Summary")
        st.write(es_text or "(non disponibile)")

        st.subheader("Notizie selezionate")
        render_selection_table(selection_items)

        # Download del report
        with open(out_path, "rb") as f:
            st.download_button(
                label="📄 Scarica report DOCX",
                data=f.read(),
                file_name=Path(out_path).name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

        elapsed = time.time() - start_ts
        st.success(f"Completato in {elapsed:.1f}s — elementi in contesto: {len(items_ctx)} · selezione: {len(selection_items)}")

    except Exception as e:
        st.exception(e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
