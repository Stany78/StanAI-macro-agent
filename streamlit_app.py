# streamlit_app.py — UI Streamlit per te_macro_agent_final_multi.py (beta invariato)

import os
import sys
import asyncio
import logging
from datetime import datetime
from pathlib import Path

import streamlit as st

# --------------------- Ambiente & chiavi ---------------------

# Fix loop async Playwright su Windows
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# Carica .env in locale; in Cloud useremo st.secrets come fallback
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except Exception:
    pass

# Se la chiave non è in env ma esiste in st.secrets, copiala in os.environ
if "ANTHROPIC_API_KEY" not in os.environ:
    try:
        if "ANTHROPIC_API_KEY" in st.secrets:
            os.environ["ANTHROPIC_API_KEY"] = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass

# --------------------- Import del beta (non modificare la logica) ---------------------
import te_macro_agent_final_multi as beta
from te_macro_agent_final_multi import (
    Config,
    setup_logging,
    TEStreamScraper,
    MacroSummarizer,
    build_selection,
    save_report,
)

# --------------------- Bootstrap Playwright in Cloud ---------------------
import subprocess
import pathlib

@st.cache_resource(show_spinner=False)
def ensure_playwright_chromium():
    """
    In Cloud i browser di Playwright non sono persistenti.
    Questa funzione installa Chromium se manca. È idempotente.
    """
    cache_dir = pathlib.Path.home() / ".cache" / "ms-playwright"
    chromium_ok = False
    if cache_dir.exists():
        # presenza di una cartella chromium/headless_shell vale come installato
        for p in cache_dir.rglob("*"):
            try:
                if p.name.startswith("chromium"):
                    chromium_ok = True
                    break
            except Exception:
                pass
    if not chromium_ok:
        subprocess.run(
            ["python", "-m", "playwright", "install", "--with-deps", "chromium"],
            check=True
        )

# --------------------- UI ---------------------
st.set_page_config(page_title="StanAI Macro Agent", page_icon="📈", layout="wide")
st.title("📈 StanAI Macro Agent")

left, right = st.columns([1, 2], gap="large")

with left:
    days = st.number_input("Giorni da mostrare nella SELEZIONE", min_value=1, max_value=30, value=5, step=1)
    run_btn = st.button("Esegui pipeline")

with right:
    st.markdown("**Seleziona i Paesi:**")
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
    for i, c in enumerate(countries_all):
        col = cols[i % 2]
        st.session_state.country_flags[c] = col.checkbox(
            c,
            value=st.session_state.country_flags.get(c, False),
            key=f"chk_{c}"
        )

    chosen_countries = [c for c, v in st.session_state.country_flags.items() if v]

st.divider()

# --------------------- Esecuzione ---------------------
if run_btn:
    setup_logging(logging.INFO)
    cfg = Config()

    # Verifica chiave
    if not cfg.ANTHROPIC_API_KEY:
        st.error("❌ Nessuna ANTHROPIC_API_KEY trovata (.env o Secrets).")
        st.stop()

    if not chosen_countries:
        st.warning("Seleziona almeno un Paese prima di eseguire.")
        st.stop()

    st.write(f"▶ **Contesto ES:** {cfg.CONTEXT_DAYS} giorni | **Selezione:** {days} giorni | **Paesi:** {', '.join(chosen_countries)}")

    # Assicura i browser Playwright in Cloud
    with st.status("Preparazione ambiente browser…", expanded=False) as st_status:
        try:
            ensure_playwright_chromium()
            st_status.update(label="Browser pronto", state="complete")
        except Exception as e:
            st.exception(e)
            st.stop()

    # ---- Pipeline dati con DB (Delta Mode dal beta) ----
    st.info("Carico/aggiorno database locale (Delta Mode)…")
    items_ctx = []
    try:
        if cfg.DELTA_MODE:
            conn = beta.db_init(cfg.DB_PATH)

            warm, fresh = [], []
            for c in chosen_countries:
                cnt = beta.db_count_by_country(conn, c)
                (warm if cnt >= cfg.WARMUP_NEW_COUNTRY_MIN else fresh).append(c)

            items_new = []
            scraper = TEStreamScraper(cfg)

            if fresh:
                with st.status(f"Scraping iniziale (≤{cfg.CONTEXT_DAYS}gg): {', '.join(fresh)}", expanded=False) as s1:
                    items_new += scraper.scrape_30d(fresh, max_days=cfg.CONTEXT_DAYS)
                    s1.update(label="Scraping iniziale completato", state="complete")

            if warm:
                with st.status(f"Scraping delta (≤{cfg.SCRAPE_HORIZON_DAYS}gg): {', '.join(warm)}", expanded=False) as s2:
                    items_new += scraper.scrape_30d(warm, max_days=min(cfg.SCRAPE_HORIZON_DAYS, cfg.CONTEXT_DAYS))
                    s2.update(label="Scraping delta completato", state="complete")

            if items_new:
                beta.db_upsert(conn, items_new)
                beta.db_prune(conn, max_age_days=cfg.PRUNE_DAYS)

            items_ctx = beta.db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)

            # Fallback se base scarsa
            if len(items_ctx) < 20:
                with st.status(f"Base scarsa ({len(items_ctx)}). Scraping completo (≤{cfg.CONTEXT_DAYS}gg)…", expanded=False) as s3:
                    items_all = scraper.scrape_30d(chosen_countries, max_days=cfg.CONTEXT_DAYS)
                    if items_all:
                        beta.db_upsert(conn, items_all)
                        items_ctx = beta.db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)
                    s3.update(label="Scraping completo terminato", state="complete")
        else:
            with st.status(f"Scraping (≤{cfg.CONTEXT_DAYS}gg)…", expanded=False) as s4:
                scraper = TEStreamScraper(cfg)
                items_ctx = scraper.scrape_30d(chosen_countries, max_days=cfg.CONTEXT_DAYS)
                s4.update(label="Scraping completato", state="complete")
    except Exception as e:
        st.exception(e)
        st.stop()

    if not items_ctx:
        st.error("❌ Nessuna notizia disponibile nella finestra temporale.")
        st.stop()

    # Anteprima contesto
    with st.expander("Anteprima contesto (prime 50)"):
        try:
            import pandas as pd
            df_prev = [{
                "time": x.get("time",""),
                "age_days": x.get("age_days",""),
                "country": x.get("country",""),
                "importance": x.get("importance",0),
                "category_raw": x.get("category_raw",""),
                "title": (x.get("title","") or "")[:140],
            } for x in items_ctx[:50]]
            st.dataframe(pd.DataFrame(df_prev), use_container_width=True)
        except Exception:
            st.caption("Anteprima non disponibile (pandas non presente).")

    # ---- Executive Summary ----
    st.info("Genero l’Executive Summary…")
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

    # ---- Selezione ultimi N giorni (usa la firma del beta: richiede cfg) ----
    st.info(f"Costruisco la selezione (ultimi {int(days)} giorni)…")
    try:
        selection_items = build_selection(items_ctx, int(days), cfg, expand1_days=10, expand2_days=30)
    except TypeError:
        # retro-compatibilità se il beta avesse build_selection(items, days) senza cfg
        selection_items = build_selection(items_ctx, int(days))
    except Exception as e:
        st.exception(e)
        st.stop()

    # ---- Traduzione titoli + Riassunti IT ----
    st.info("Traduco i titoli e genero i riassunti in italiano…")
    prog = st.progress(0.0)
    total = max(1, len(selection_items))
    for i, it in enumerate(selection_items, 1):
        try:
            it["title_it"] = summarizer.translate_it(it.get("title",""), cfg)
        except Exception as e:
            logging.warning("Titolo non tradotto: %s", e)
            it["title_it"] = it.get("title","")

        try:
            it["summary_it"] = summarizer.summarize_item_it(it, cfg)
        except Exception as e:
            logging.warning("Riassunto IT non disponibile: %s", e)
            it["summary_it"] = (it.get("description","") or "")
        prog.progress(i / total)

    st.success("✅ Pipeline completata.")

    # ---- Anteprima selezione ----
    with st.expander("Anteprima selezione (ordinata per impatto, score, recency)"):
        try:
            import pandas as pd
            df_sel = [{
                "time": it.get("time",""),
                "age_days": it.get("age_days",""),
                "country": it.get("country",""),
                "importance": it.get("importance",0),
                "score": it.get("score",0),
                "category": it.get("category_mapped",""),
                "title_it": (it.get("title_it","") or "")[:120],
            } for it in selection_items]
            st.dataframe(pd.DataFrame(df_sel), use_container_width=True)
        except Exception:
            st.caption("Anteprima non disponibile (pandas non presente).")

    # ---- Report DOCX ----
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

    # ---- Riepilogo finale ----
    st.write("---")
    st.write(f"**Notizie totali (Contesto {cfg.CONTEXT_DAYS}gg, DB+stream):** {len(items_ctx)}")
    st.write(f"**Notizie selezionate (ultimi {int(days)} gg + fill-up):** {len(selection_items)}")
    st.caption(f"Esecuzione: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
