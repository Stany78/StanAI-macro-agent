# streamlit_app.py — UI Streamlit per te_macro_agent_final_multi.py (beta, invariato)
# - Usa .env per ANTHROPIC_API_KEY (locale). In Cloud imposta ENV VAR.
# - Non modifica la logica: richiama le funzioni/classi già presenti nel file beta.
# - Mantiene DB SQLite automatico (Delta Mode) come nel main CLI.

import os
import sys
import asyncio
import logging
from datetime import datetime
from pathlib import Path

import streamlit as st

# Carica .env (solo locale). In Cloud usare ENV VAR app.
try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
except Exception:
    pass

# Fix loop async Playwright su Windows
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# Import DAL TUO FILE BETA (nessuna modifica alla logica)
from te_macro_agent_final_multi import (
    # Config & logging
    Config, setup_logging,
    # Scraper / Summarizer
    TEStreamScraper, MacroSummarizer,
    # Pipeline selezione & report
    build_selection, save_report,
    # DB helpers (Delta Mode)
    db_init, db_count_by_country, db_upsert, db_prune, db_load_recent,
)

# ------------------- UI -------------------
st.set_page_config(page_title="StanAI Macro Agent", page_icon="📈", layout="wide")
st.title("📈 StanAI Macro Agent")

# Parametri essenziali (UI minimale)
col_l, col_r = st.columns([1, 2], gap="large")

with col_l:
    days = st.number_input("Giorni da mostrare nella SELEZIONE", min_value=1, max_value=30, value=5, step=1)

with col_r:
    st.markdown("**Seleziona i Paesi:**")
    countries_all = [
        "United States", "Euro Area", "Germany", "United Kingdom",
        "Italy", "France", "China", "Japan", "Spain", "Netherlands", "European Union"
    ]

    b1, b2 = st.columns(2)
    with b1:
        select_all = st.button("Seleziona tutti")
    with b2:
        deselect_all = st.button("Deseleziona tutti")

    if "country_flags" not in st.session_state:
        st.session_state.country_flags = {c: (c in ["United States", "Euro Area"])}  # default come CLI

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
run_btn = st.button("Esegui pipeline")

# ------------------- ESECUZIONE -------------------
if run_btn:
    setup_logging(logging.INFO)
    cfg = Config()  # legge .env già in te_macro_agent_final_multi.py

    # Chiave Anthropic da .env/ENV
    if not cfg.ANTHROPIC_API_KEY:
        st.error("❌ Nessuna ANTHROPIC_API_KEY trovata (.env o variabile d’ambiente).")
        st.stop()

    if not chosen_countries:
        st.warning("Seleziona almeno un Paese prima di eseguire.")
        st.stop()

    # Normalizza come nel main CLI (EU -> Euro Area)
    chosen_countries = ["Euro Area" if x == "European Union" else x for x in chosen_countries]

    st.write(
        f"▶ **Contesto ES:** {cfg.CONTEXT_DAYS} giorni | **Selezione:** {days} giorni | **Paesi:** {', '.join(chosen_countries)}"
    )

    # ===== Pipeline dati identica al main (Delta Mode con DB) =====
    try:
        with st.status("Carico/aggiorno database locale (Delta Mode)…", expanded=False) as st_status:
            scraper = TEStreamScraper(cfg)

            if cfg.DELTA_MODE:
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

                # Fallback: base scarsa → scrape completo
                if len(items_ctx) < 20:
                    items_all = scraper.scrape_30d(chosen_countries, max_days=cfg.CONTEXT_DAYS)
                    if items_all:
                        db_upsert(conn, items_all)
                        items_ctx = db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)
            else:
                items_ctx = scraper.scrape_30d(chosen_countries, max_days=cfg.CONTEXT_DAYS)

            st_status.update(label=f"DB pronto. Notizie in base (ultimi {cfg.CONTEXT_DAYS} gg): {len(items_ctx)}", state="complete")
    except Exception as e:
        st.exception(e)
        st.stop()

    if not items_ctx:
        st.error("❌ Nessuna notizia disponibile entro la finestra.")
        st.stop()

    # ===== Executive Summary (identico al main) =====
    st.info("Genero l’Executive Summary (può richiedere qualche secondo)…")
    try:
        summarizer = MacroSummarizer(cfg.ANTHROPIC_API_KEY, cfg.MODEL, cfg.MODEL_TEMP, cfg.MAX_TOKENS)
    except Exception as e:
        st.error(f"Errore inizializzazione MacroSummarizer: {e}")
        st.stop()

    try:
        es_text = summarizer.executive_summary(items_ctx, cfg, chosen_countries)
    except Exception as e:
        logging.error("Errore ES: %s", e)
        es_text = "Executive Summary non disponibile per errore di generazione."

    st.subheader("Executive Summary")
    st.write(es_text)

    # ===== Selezione ultimi N giorni (con fill-up) =====
    st.info(f"Costruisco la selezione (ultimi {days} giorni, con regole e fill-up)…")
    try:
        selection_items = build_selection(items_ctx, int(days), cfg, expand1_days=10, expand2_days=30)
    except TypeError:
        # In caso di vecchia firma (senza cfg), fallback
        selection_items = build_selection(items_ctx, int(days))
    except Exception as e:
        st.exception(e)
        st.stop()

    # ===== Traduzioni & Riassunti IT =====
    st.info("Traduco titoli e genero riassunti in italiano…")
    prog = st.progress(0.0)
    total = max(1, len(selection_items))

    for i, it in enumerate(selection_items, 1):
        try:
            it["title_it"] = summarizer.translate_it(it.get("title", ""), cfg)
        except Exception as e:
            logging.warning("Titolo non tradotto: %s", e)
            it["title_it"] = it.get("title", "")

        try:
            it["summary_it"] = summarizer.summarize_item_it(it, cfg)
        except Exception as e:
            logging.warning("Riassunto IT non disponibile: %s", e)
            it["summary_it"] = (it.get("description", "") or "")

        prog.progress(i / total)

    # ===== Report DOCX =====
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"MacroAnalysis_AutoSelect_{int(days)}days_{ts}.docx"
        out_path = save_report(
            filename=filename,
            es_text=es_text,
            selection=selection_items,
            countries=chosen_countries,
            days=int(days),
            context_count=len(items_ctx),
            output_dir=cfg.OUTPUT_DIR,
        )
        st.success("✅ Pipeline completata.")
        st.info(f"Report salvato: `{out_path}`")

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

    # ===== Riepilogo =====
    st.write("---")
    st.write(f"**Notizie in base (ultimi {cfg.CONTEXT_DAYS} gg):** {len(items_ctx)}")
    st.write(f"**Notizie selezionate (ultimi {int(days)} gg + fill-up):** {len(selection_items)}")
    st.caption(f"Esecuzione: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
