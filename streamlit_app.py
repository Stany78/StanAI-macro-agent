# streamlit_app.py — UI Streamlit per te_macro_agent_final_multi.py (versione DB/Delta Mode)
# - Nessuna modifica alla logica: usa funzioni/classi del file beta
# - Pipeline identica al CLI: DB init → delta scrape → upsert → prune → load → fallback
# - ES 60gg, selezione con build_selection(..., cfg)
# - UI minimale: giorni + selezione Paesi, senza log debug né suggerimenti

import os
import sys
import asyncio
from datetime import datetime
from pathlib import Path

import streamlit as st

# Fix loop async Playwright su Windows
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# Import esplicito del modulo beta (per usare tutte le funzioni così come sono)
import te_macro_agent_final_multi as beta
from te_macro_agent_final_multi import (
    Config,
    setup_logging,
    TEStreamScraper,
    MacroSummarizer,
    build_selection,
    save_report,
    # DB helpers
    db_init, db_upsert, db_count_by_country, db_load_recent, db_prune,
)

# ---------- UI ----------
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
    # Menu paesi coerente con il file beta
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

# ---------- Esecuzione ----------
if run_btn:
    # Log base (INFO), senza toggle debug
    setup_logging()

    # Config dal file beta
    cfg = Config()

    # API Key: usiamo .env, non chiediamo input
    if not cfg.ANTHROPIC_API_KEY:
        st.error("❌ Nessuna ANTHROPIC_API_KEY trovata nel file .env. Aggiungila e riprova.")
        st.stop()

    if not chosen_countries:
        st.warning("Seleziona almeno un Paese prima di eseguire.")
        st.stop()

    # Normalizza “European Union” → “Euro Area” (coerente con beta)
    chosen_norm = ["Euro Area" if x == "European Union" else x for x in chosen_countries]

    st.write(
        f"▶ **Contesto ES:** {cfg.CONTEXT_DAYS} giorni | **Selezione:** {days} giorni | **Paesi:** {', '.join(chosen_norm)}"
    )

    # === Pipeline dati con DB (Delta Mode), identica al CLI ===
    items_ctx = []

    with st.status("Aggiornamento cache locale e caricamento notizie…", expanded=False) as st_status:
        try:
            if cfg.DELTA_MODE:
                conn = db_init(cfg.DB_PATH)

                warm, fresh = [], []
                for c in chosen_norm:
                    cnt = db_count_by_country(conn, c)
                    (warm if cnt >= cfg.WARMUP_NEW_COUNTRY_MIN else fresh).append(c)

                scraper = TEStreamScraper(cfg)
                items_new = []

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
                # Se mai disattivassi il DB nel .env
                scraper = TEStreamScraper(cfg)
                items_ctx = scraper.scrape_30d(chosen_norm, max_days=cfg.CONTEXT_DAYS)

            st_status.update(label=f"Cache aggiornata. Notizie disponibili (finestra {cfg.CONTEXT_DAYS}gg): {len(items_ctx)}", state="complete")
        except Exception as e:
            st.exception(e)
            st.stop()

    if not items_ctx:
        st.error("❌ Nessuna notizia disponibile nella finestra temporale selezionata.")
        st.stop()

    # === Executive Summary (60gg) ===
    st.info("Genero l’Executive Summary…")
    try:
        summarizer = MacroSummarizer(cfg.ANTHROPIC_API_KEY, cfg.MODEL, cfg.MODEL_TEMP, cfg.MAX_TOKENS)
        es_text = summarizer.executive_summary(items_ctx, cfg, chosen_norm)
    except Exception as e:
        st.exception(e)
        es_text = "Executive Summary non disponibile per errore di generazione."

    st.subheader("Executive Summary")
    st.write(es_text)

    # === Selezione ultimi N giorni (+ fill-up) ===
    st.info(f"Costruisco la selezione (ultimi {int(days)} giorni, con fill-up se necessario)…")
    try:
        selection_items = build_selection(items_ctx, int(days), cfg, expand1_days=10, expand2_days=30)
    except TypeError:
        # Safety net nel caso tu cambiassi la firma in futuro
        selection_items = build_selection(items_ctx, int(days), cfg)
    except Exception as e:
        st.exception(e)
        st.stop()

    # === Traduzione titoli + Riassunti IT ===
    st.info("Traduco titoli e genero riassunti in italiano…")
    prog = st.progress(0.0)
    total = max(1, len(selection_items))

    for i, it in enumerate(selection_items, 1):
        try:
            it["title_it"] = summarizer.translate_it(it.get("title", ""), cfg)
        except Exception:
            it["title_it"] = it.get("title", "") or ""

        try:
            it["summary_it"] = summarizer.summarize_item_it(it, cfg)
        except Exception:
            it["summary_it"] = (it.get("description", "") or "")

        prog.progress(i / total)

    st.success("✅ Pipeline completata.")

    # === Anteprima Selezione ===
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

    # === Report DOCX ===
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

    # === Riepilogo ===
    st.write("---")
    st.write(f"**Notizie totali nel DB (ultimi {cfg.CONTEXT_DAYS} gg):** {len(items_ctx)}")
    st.write(f"**Notizie selezionate (ultimi {int(days)} gg + fill-up):** {len(selection_items)}")
    st.caption(f"Esecuzione: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
