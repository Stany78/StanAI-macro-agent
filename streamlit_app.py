# streamlit_app.py — UI Streamlit per te_macro_agent_final_multi.py
# - Non cambia la logica del file "beta"
# - Installa i browser Playwright al volo se mancano (solo la prima volta)
# - Usa SQLite/Delta Mode se le funzioni DB sono disponibili nel beta

import os
import sys
import asyncio
import logging
import subprocess
from datetime import datetime
from pathlib import Path

import streamlit as st

# Fix loop async Playwright su Windows (benigno su Linux cloud)
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# Import "beta" (non modifichiamo la sua logica)
import te_macro_agent_final_multi as beta

# Prova a importare le funzioni DB (se non esistono nel beta, faremo fallback)
_has_db = True
try:
    from te_macro_agent_final_multi import db_init, db_upsert, db_load_recent, db_prune
except Exception:
    _has_db = False

# ---------- Config pagina ----------
st.set_page_config(page_title="StanAI Macro Agent", page_icon="📈", layout="wide")
st.title("📈 StanAI Macro Agent")

# ---------- Helper: Playwright browsers ----------
@st.cache_resource(show_spinner=False)
def ensure_playwright_browsers():
    """
    Scarica il browser Chromium di Playwright se non presente.
    Esegue: python -m playwright install --with-deps chromium
    Cacheremo l’esito (Streamlit Cloud usa una cache per session/job).
    """
    # Evita ripetere se già c’è la cartella target
    default_cache = Path.home() / ".cache" / "ms-playwright"
    chromium_dir = list(default_cache.glob("chromium*"))
    if chromium_dir:
        return "ok:already"

    try:
        # Variabili d’ambiente utili per cloud
        env = os.environ.copy()
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(default_cache))

        cmd = [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        return "ok:installed"
    except Exception as e:
        return f"err:{e}"

# ---------- UI: parametri ----------
left, right = st.columns([1, 2], gap="large")

with left:
    days = st.number_input("Giorni da mostrare nella SELEZIONE", min_value=1, max_value=30, value=5, step=1)
    run_btn = st.button("Esegui")

with right:
    st.markdown("**Seleziona i Paesi:**")
    countries_all = [
        "United States", "Euro Area", "Germany", "United Kingdom",
        "Italy", "France", "China", "Japan", "Spain", "Netherlands", "European Union"
    ]

    c1, c2, _ = st.columns([1, 1, 3])
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

# ---------- RUN ----------
if run_btn:
    # Logging dal beta (INFO di default)
    beta.setup_logging(logging.INFO)

    # Carica config dal beta (prende .env locale e/o secrets)
    cfg = beta.Config()

    # Secrets su Streamlit Cloud hanno priorità: se presente, sovrascrive
    sec_key = st.secrets.get("ANTHROPIC_API_KEY") if hasattr(st, "secrets") else None
    if sec_key:
        os.environ["ANTHROPIC_API_KEY"] = sec_key
        cfg.ANTHROPIC_API_KEY = sec_key

    # Controllo chiave AI
    if not cfg.ANTHROPIC_API_KEY:
        st.error("❌ Nessuna ANTHROPIC_API_KEY trovata. Aggiungila in Streamlit → Settings → Secrets.")
        st.stop()

    if not chosen_countries:
        st.warning("Seleziona almeno un Paese.")
        st.stop()

    st.write(
        f"▶ **Contesto ES:** {cfg.CONTEXT_DAYS} giorni | **Selezione:** {days} giorni | **Paesi:** {', '.join(chosen_countries)}"
    )

    # 1) Assicura Playwright browsers (solo la prima volta sul cloud)
    with st.status("Verifica/Installazione browser Playwright…", expanded=False) as s:
        res = ensure_playwright_browsers()
        if res.startswith("err:"):
            s.update(label=f"Installazione browser fallita: {res[4:]}", state="error")
            st.stop()
        else:
            s.update(label="Browser Playwright pronto.", state="complete")

    # 2) Caricamento dati (preferisci DB se disponibile nel beta)
    with st.status("Carico dati (DB + scraping)…", expanded=True) as s:
        try:
            items_ctx = []
            used_db = False

            if getattr(cfg, "DELTA_MODE", False) and _has_db:
                used_db = True
                conn = db_init(cfg.DB_PATH)

                # Partiziona paesi in warm/fresh come nel beta main
                warm, fresh = [], []
                try:
                    from te_macro_agent_final_multi import db_count_by_country
                    for c in chosen_countries:
                        cnt = db_count_by_country(conn, c)
                        (warm if cnt >= cfg.WARMUP_NEW_COUNTRY_MIN else fresh).append(c)
                except Exception:
                    # Se non abbiamo db_count_by_country, consideriamo tutti fresh
                    fresh = list(chosen_countries)

                scraper = beta.TEStreamScraper(cfg)
                items_new = []

                if fresh:
                    s.write(f"Warm-up nuovi paesi (≤{cfg.CONTEXT_DAYS}gg): {', '.join(fresh)}")
                    items_new += scraper.scrape_30d(fresh, max_days=cfg.CONTEXT_DAYS)

                if warm:
                    s.write(f"Delta scrape paesi noti (≤{cfg.SCRAPE_HORIZON_DAYS}gg): {', '.join(warm)}")
                    items_new += scraper.scrape_30d(warm, max_days=min(cfg.SCRAPE_HORIZON_DAYS, cfg.CONTEXT_DAYS))

                if items_new:
                    db_upsert(conn, items_new)
                    db_prune(conn, max_age_days=cfg.PRUNE_DAYS)

                items_ctx = db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)

                # Fallback se DB troppo scarno
                if len(items_ctx) < 20:
                    s.write("Base DB scarsa — eseguo scrape completo finestra ES…")
                    items_all = scraper.scrape_30d(chosen_countries, max_days=cfg.CONTEXT_DAYS)
                    if items_all:
                        db_upsert(conn, items_all)
                        items_ctx = db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)
            else:
                scraper = beta.TEStreamScraper(cfg)
                items_ctx = scraper.scrape_30d(chosen_countries, max_days=cfg.CONTEXT_DAYS)

            if not items_ctx:
                s.update(label="Nessuna notizia disponibile nell’intervallo.", state="error")
                st.stop()

            s.update(label=f"Dati caricati ({'DB+' if used_db else ''}stream). Totale elementi: {len(items_ctx)}", state="complete")

        except Exception as e:
            s.update(label=f"Errore caricamento dati: {e}", state="error")
            st.stop()

    # 3) Executive Summary (60gg)
    st.info("Genero l’Executive Summary…")
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
    if es_text.lower().startswith("executive summary non disponibile"):
        st.warning("ES non generato. Verifica la chiave Anthropic, il modello e i limiti di token.")
    st.write(es_text)

    # 4) Selezione ultimi N giorni (nuova firma: items, days, cfg)
    st.info(f"Costruisco la selezione (ultimi {days} giorni)…")
    try:
        selection_items = beta.build_selection(items_ctx, int(days), cfg)
    except TypeError:
        # Fallback per vecchie versioni (firma a 2 argomenti)
        selection_items = beta.build_selection(items_ctx, int(days))
    except Exception as e:
        st.exception(e)
        st.stop()

    # 5) Traduzione titoli + Riassunti IT
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

    # 6) Anteprima selezione
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
            st.info("Anteprima non disponibile (pandas non installato).")

    # 7) Report DOCX
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

    # 8) Riepilogo
    st.write("---")
    st.write(f"**Notizie totali (Contesto {cfg.CONTEXT_DAYS} gg):** {len(items_ctx)}")
    st.write(f"**Notizie selezionate (ultimi {int(days)} gg):** {len(selection_items)}")
    st.caption(f"Esecuzione: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
