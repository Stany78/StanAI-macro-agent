# streamlit_app.py — UI Streamlit per te_macro_agent_final_multi.py
# Non modifica la logica: usa SOLO classi/funzioni esistenti nel file beta.

import os
import sys
import asyncio
import logging
from datetime import datetime
from pathlib import Path

import streamlit as st

# ---- Fix event loop (Windows + Playwright) ----
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# ---- Importa SOLO dal file beta (non cambiare nomi) ----
import te_macro_agent_final_multi as beta
# ci servono questi simboli:
# - beta.Config
# - beta.TEStreamScraper
# - beta.MacroSummarizer
# - beta.build_selection
# - beta.save_report
# - beta.db_init, beta.db_upsert, beta.db_load_recent, beta.db_prune
# - beta.get_or_prompt_anthropic_key (non verrà usata in Cloud)

# ----------------- UI base -----------------
st.set_page_config(page_title="StanAI Macro Agent", page_icon="📈", layout="wide")
st.title("📈 StanAI Macro Agent")

# Colonne impostazioni
left, right = st.columns([1, 2], gap="large")

with left:
    days = st.number_input(
        "Giorni da mostrare nella SELEZIONE",
        min_value=1, max_value=30, value=5, step=1
    )
    run_btn = st.button("Esegui pipeline")

with right:
    st.markdown("**Seleziona i Paesi (flag):**")
    countries_all = [
        "United States", "Euro Area", "Germany", "United Kingdom",
        "Italy", "France", "China", "Japan", "Spain", "Netherlands", "European Union"
    ]

    c1, c2, c3 = st.columns([1, 1, 3])
    with c1:
        select_all = st.button("Seleziona tutti")
    with c2:
        deselect_all = st.button("Deseleziona tutti")
    with c3:
        st.caption("Nota: nel motore, “European Union” viene normalizzato su “Euro Area” dove serve.")

    if "country_flags" not in st.session_state:
        st.session_state.country_flags = {c: False for c in countries_all}

    if select_all:
        for c in countries_all:
            st.session_state.country_flags[c] = True
    if deselect_all:
        for c in countries_all:
            st.session_state.country_flags[c] = False

    # Checkbox a due colonne
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

# ----------------- Esecuzione -----------------
if run_btn:
    # Log base (INFO), coerente con la CLI
    beta.setup_logging(logging.INFO)

    # Config dalla beta (usa st.secrets/.env secondo la tua implementazione nel file beta)
    cfg = beta.Config()

    # Controllo API key: in Cloud va messa in Secrets; in locale va in .env
    if not cfg.ANTHROPIC_API_KEY:
        st.error(
            "❌ Nessuna ANTHROPIC_API_KEY trovata.\n\n"
            "• In LOCALE: aggiungi la chiave nel file `.env` (ANTHROPIC_API_KEY=...)\n"
            "• In STREAMLIT CLOUD: apri Settings → Secrets e inserisci:\n"
            '  ANTHROPIC_API_KEY = "la-tua-chiave"\n'
        )
        st.stop()

    if not chosen_countries:
        st.warning("Seleziona almeno un Paese prima di eseguire.")
        st.stop()

    st.write(
        f"▶ **Contesto ES:** {cfg.CONTEXT_DAYS} giorni | "
        f"**Selezione:** {int(days)} giorni | "
        f"**Paesi:** {', '.join(chosen_countries)}"
    )

    # ========== PIPELINE DATI con DB/Delta Mode (identica alla CLI) ==========
    items_ctx = []

    try:
        with st.status("Inizializzo database locale…", expanded=False) as st_status:
            conn = beta.db_init(cfg.DB_PATH)
            st_status.update(label="Database pronto", state="complete")
    except Exception as e:
        st.exception(e)
        st.stop()

    warm, fresh = [], []
    try:
        for c in chosen_countries:
            cnt = beta.db_count_by_country(conn, c)
            (warm if cnt >= cfg.WARMUP_NEW_COUNTRY_MIN else fresh).append(c)
    except Exception as e:
        st.exception(e)
        st.stop()

    scraper = beta.TEStreamScraper(cfg)

    try:
        with st.status("Scraping TradingEconomics…", expanded=False) as st_status:
            items_new = []
            if fresh:
                st_status.update(label=f"Scrape iniziale (<= {cfg.CONTEXT_DAYS} gg) per: {', '.join(fresh)}")
                items_new += scraper.scrape_30d(fresh, max_days=cfg.CONTEXT_DAYS)
            if warm:
                st_status.update(label=f"Scrape delta (<= {cfg.SCRAPE_HORIZON_DAYS} gg) per: {', '.join(warm)}")
                items_new += scraper.scrape_30d(warm, max_days=min(cfg.SCRAPE_HORIZON_DAYS, cfg.CONTEXT_DAYS))

            if items_new:
                beta.db_upsert(conn, items_new)
                beta.db_prune(conn, max_age_days=cfg.PRUNE_DAYS)

            items_ctx = beta.db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)

            # Fallback: se base scarsa, fai scrape pieno della finestra ES
            if len(items_ctx) < 20:
                st_status.update(label=f"Base DB scarsa ({len(items_ctx)}). Scrape completo (<= {cfg.CONTEXT_DAYS} gg) …")
                items_all = scraper.scrape_30d(chosen_countries, max_days=cfg.CONTEXT_DAYS)
                if items_all:
                    beta.db_upsert(conn, items_all)
                    items_ctx = beta.db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)

            st_status.update(label=f"Scraping+DB completati. Notizie nel contesto: {len(items_ctx)}", state="complete")
    except Exception as e:
        st.exception(e)
        st.stop()

    if not items_ctx:
        st.error("❌ Nessuna notizia disponibile entro la finestra selezionata.")
        st.stop()

    # Anteprima grezza dal contesto (opzionale)
    with st.expander("Anteprima contesto (prime 50 righe, 60gg)"):
        try:
            import pandas as pd
            prev = [{
                "time": x.get("time", ""),
                "age_days": round(float(x.get("age_days", 0)), 2) if x.get("age_days") is not None else "",
                "country": x.get("country", ""),
                "importance": x.get("importance", 0),
                "category_raw": x.get("category_raw", ""),
                "title": (x.get("title", "") or "")[:140],
            } for x in items_ctx[:50]]
            st.dataframe(pd.DataFrame(prev), use_container_width=True)
        except Exception:
            st.info("Anteprima non disponibile (pandas non presente).")

    # ========== ES (60gg) ==========
    st.info("Genero l’Executive Summary (60gg)…")
    try:
        summarizer = beta.MacroSummarizer(cfg.ANTHROPIC_API_KEY, cfg.MODEL, cfg.MODEL_TEMP, cfg.MAX_TOKENS)
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

    # ========== Selezione ultimi N giorni (con fill-up) ==========
    st.info(f"Costruisco la selezione (ultimi {int(days)} giorni)…")
    try:
        selection_items = beta.build_selection(items_ctx, int(days), cfg, expand1_days=10, expand2_days=30)
    except TypeError as e:
        # Firma diversa? messaggio chiaro:
        st.error(
            "Errore chiamando build_selection. "
            "Assicurati che la firma sia: build_selection(items_ctx, days, cfg, expand1_days=10, expand2_days=30)."
        )
        st.exception(e)
        st.stop()
    except Exception as e:
        st.exception(e)
        st.stop()

    # ========== Traduzione titoli + Riassunti IT ==========
    st.info("Traduco i titoli e genero i riassunti in italiano…")
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

    st.success("✅ Pipeline completata.")

    # Tabella Selezione (anteprima)
    with st.expander("Anteprima Selezione (ordinata per impatto → score → recency)"):
        try:
            import pandas as pd
            prev_sel = [{
                "time": it.get("time",""),
                "age_days": round(float(it.get("age_days", 0)), 2) if it.get("age_days") is not None else "",
                "country": it.get("country",""),
                "importance": it.get("importance",0),
                "score": it.get("score",0),
                "category": it.get("category_mapped",""),
                "title_it": (it.get("title_it","") or "")[:120],
            } for it in selection_items]
            st.dataframe(pd.DataFrame(prev_sel), use_container_width=True)
        except Exception:
            st.info("Anteprima non disponibile (pandas non presente).")

    # ========== Report DOCX ==========
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"MacroAnalysis_AutoSelect_{int(days)}days_{ts}.docx"
        out_path = beta.save_report(
            filename=filename,
            es_text=es_text,
            selection=selection_items,
            countries=chosen_countries,
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

    # Riepilogo finale
    st.write("---")
    st.write(f"**Notizie totali (Contesto {cfg.CONTEXT_DAYS} gg):** {len(items_ctx)}")
    st.write(f"**Notizie selezionate (ultimi {int(days)} gg + fill-up):** {len(selection_items)}")
    st.caption(f"Esecuzione: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
