# streamlit_app.py — UI Streamlit per te_macro_agent_final_multi.py (versione beta)
# - NON modifica la logica del programma: usa SOLO funzioni/classi esistenti nel file beta.
# - Installa SEMPRE i browser Playwright (Chromium) al primo avvio su Streamlit Cloud.
# - Legge ANTHROPIC_API_KEY da st.secrets (fallback a .env / variabili d’ambiente).

import os
import sys
import asyncio
import logging
from datetime import datetime
from pathlib import Path
import subprocess

import streamlit as st

# ===== Fix event loop Playwright su Windows =====
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# ===== Import SOLO dal file beta (non modifichiamo la logica) =====
import te_macro_agent_final_multi as beta
# Usiamo:
# - beta.Config
# - beta.setup_logging
# - beta.TEStreamScraper
# - beta.MacroSummarizer
# - beta.build_selection(items_ctx, days, cfg, expand1_days=10, expand2_days=30)
# - beta.save_report

# ===== Config pagina =====
st.set_page_config(page_title="StanAI Macro Agent", page_icon="📈", layout="wide")
st.title("📈 StanAI Macro Agent")

# ===== Installazione Playwright/Chromium (una sola volta per sessione) =====
@st.cache_resource(show_spinner=False)
def ensure_playwright_browsers_installed() -> str:
    """
    Scarica Chromium per Playwright se non presente.
    Esegue: python -m playwright install chromium --with-deps
    Ritorna l'output (stdout/stderr) per diagnostica.
    """
    try:
        # Alcuni ambienti richiedono il path esplicito, lo lasciamo predefinito.
        env = os.environ.copy()
        # Esecuzione install
        proc = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        return proc.stdout or "playwright install: ok"
    except subprocess.CalledProcessError as e:
        # Ritorno l'output per visualizzarlo in UI
        return (e.stdout or "") + "\n[install returned non-zero exit]"

# ========== UI parametri ==========
left, right = st.columns([1, 2], gap="large")

with left:
    days = st.number_input("Giorni da mostrare nella SELEZIONE", min_value=1, max_value=30, value=5, step=1)
    run_btn = st.button("Esegui pipeline")

with right:
    st.markdown("**Seleziona i Paesi (flag):**")
    countries_all = [
        "United States", "Euro Area", "Germany", "United Kingdom",
        "Italy", "France", "China", "Japan", "Spain", "Netherlands", "European Union"
    ]

    c1, c2, _ = st.columns([1, 1, 4])
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

# ========== Esecuzione ==========
if run_btn:
    beta.setup_logging(logging.INFO)
    cfg = beta.Config()

    # 1) API KEY da st.secrets (fallback env/.env)
    api_from_secrets = st.secrets.get("ANTHROPIC_API_KEY") if hasattr(st, "secrets") else None
    if api_from_secrets:
        os.environ["ANTHROPIC_API_KEY"] = api_from_secrets
        cfg.ANTHROPIC_API_KEY = api_from_secrets

    if not cfg.ANTHROPIC_API_KEY:
        st.error("❌ Nessuna ANTHROPIC_API_KEY trovata in `st.secrets` o `.env`/env. Aggiungila e riprova.")
        st.stop()

    if not chosen_countries:
        st.warning("Seleziona almeno un Paese prima di eseguire.")
        st.stop()

    st.write(
        f"▶ **Contesto ES:** {cfg.CONTEXT_DAYS} giorni | **Selezione:** {days} giorni | **Paesi:** {', '.join(chosen_countries)}"
    )

    # 2) Installazione Playwright/Chromium PRIMA di qualsiasi scrape (obbligatoria su Cloud)
    with st.status("Preparazione ambiente (Playwright + Chromium)…", expanded=False) as s:
        install_log = ensure_playwright_browsers_installed()
        s.update(label="Ambiente pronto (browser installati o già presenti).", state="complete")
    # (facoltativo) Mostra log di installazione in expander per diagnosi
    with st.expander("Log installazione Playwright (diagnostica)"):
        st.code(install_log or "Nessun output")

    # 3) Scraping/Caricamento base (il tuo beta gestisce ES 60gg & DB/Delta Mode internamente)
    try:
        with st.status("Caricamento notizie (DB/stream)…", expanded=False) as st_status:
            scraper = beta.TEStreamScraper(cfg)
            items_ctx = scraper.scrape_30d(chosen_countries, max_days=cfg.CONTEXT_DAYS)
            st_status.update(
                label=f"Base reperita. Notizie disponibili entro {cfg.CONTEXT_DAYS} gg: {len(items_ctx)}",
                state="complete"
            )
    except Exception as e:
        # Se qualcosa va storto qui, mostriamo l’errore completo.
        st.exception(e)
        st.stop()

    if not items_ctx:
        st.error("❌ Nessuna notizia disponibile entro la finestra.")
        st.stop()

    # 4) Executive Summary (60gg)
    st.info("Genero l’Executive Summary (contesto)…")
    try:
        summarizer = beta.MacroSummarizer(cfg.ANTHROPIC_API_KEY, cfg.MODEL, cfg.MODEL_TEMP, cfg.MAX_TOKENS)
        es_text = summarizer.executive_summary(items_ctx, cfg, chosen_countries)
    except Exception as e:
        logging.error("Errore ES: %s", e)
        es_text = "Executive Summary non disponibile per errore di generazione."

    st.subheader("Executive Summary")
    st.write(es_text)

    # 5) Selezione ultimi N giorni (firma del beta richiede anche cfg)
    st.info(f"Costruisco la selezione (ultimi {int(days)} giorni)…")
    try:
        selection_items = beta.build_selection(items_ctx, int(days), cfg, expand1_days=10, expand2_days=30)
    except Exception as e:
        st.exception(e)
        st.stop()

    # 6) Traduzione titoli + Riassunti IT
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

    # 7) Anteprima selezione
    with st.expander("Anteprima Selezione (ordinata per impatto → score → recency)"):
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
            st.info("Anteprima non disponibile (pandas mancante nel requirements).")

    # 8) Report DOCX (firma invariata del beta)
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
            output_dir=beta.Config().OUTPUT_DIR,
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

    # 9) Riepilogo finale
    st.write("---")
    st.write(f"**Notizie totali (ultimi {cfg.CONTEXT_DAYS} gg):** {len(items_ctx)}")
    st.write(f"**Notizie selezionate (ultimi {int(days)} gg + fill-up):** {len(selection_items)}")
    st.caption(f"Esecuzione: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
