# streamlit_app.py — versione definitiva con bootstrap Playwright integrato
# - Non modifica la logica del modulo beta: usa le funzioni/classi esistenti
# - Aggiunge un bootstrap robusto per Playwright (libreria + browser Chromium)
# - Mantiene la pipeline DB/Delta Mode, Executive Summary e report DOCX

import os
import sys
import asyncio
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import streamlit as st

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
import te_macro_agent_final_multi as beta
from te_macro_agent_final_multi import (
    Config,
    setup_logging,
    TEStreamScraper,
    MacroSummarizer,
    build_selection,
    save_report,
    db_init, db_upsert, db_count_by_country, db_load_recent, db_prune,
)

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
    # Installa la libreria se assente
    try:
        import playwright  # noqa: F401
    except ModuleNotFoundError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright==1.48.0"])  # pin stabile

    # Imposta la stessa path vista nei log di errore
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))

    base = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    chromium_present = base.exists() and any(p.name.startswith("chromium") for p in base.glob("chromium-*"))

    if not chromium_present:
        # Primo tentativo: con deps di sistema
        try:
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
        except subprocess.CalledProcessError:
            # Fallback: senza --with-deps (su container che hanno già le deps APT)
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


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

    # Executive Summary
    st.info("Genero l’Executive Summary…")
    try:
        summarizer = MacroSummarizer(cfg.ANTHROPIC_API_KEY, cfg.MODEL, cfg.MODEL_TEMP, cfg.MAX_TOKENS)
        es_text = summarizer.executive_summary(items_ctx, cfg, chosen_norm)
    except Exception as e:
        st.exception(e)
        es_text = "Executive Summary non disponibile per errore di generazione."

    st.subheader("Executive Summary")
    st.write(es_text)

    # Selezione ultimi N giorni (+ fill-up)
    st.info(f"Costruisco la selezione (ultimi {int(days)} giorni, con fill-up se necessario)…")
    try:
        selection_items = build_selection(items_ctx, int(days), cfg, expand1_days=10, expand2_days=30)
    except TypeError:
        selection_items = build_selection(items_ctx, int(days), cfg)
    except Exception as e:
        st.exception(e)
        st.stop()

    # Traduzione titoli + Riassunti IT
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

    # Anteprima selezione
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
