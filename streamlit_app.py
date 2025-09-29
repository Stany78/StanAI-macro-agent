# streamlit_app.py — UI Streamlit per te_macro_agent_final_multi.py (wrapper “safe”)
# - Non modifica la logica del file beta: usa SOLO entità già presenti lì.
# - Fix NameError in inizializzazione checkbox paesi
# - ES robusto: limita contesto per ES + retry/backoff su 429 (Anthropic)
# - Dedupe “European Union” -> “Euro Area”; se selezionati entrambi, unifica
# - Usa la cache SQLite del beta (db_init, db_upsert, db_load_recent, db_prune ecc.)

import os
import sys
import time
import asyncio
import logging
from datetime import datetime
from pathlib import Path

import streamlit as st

# Fix loop async Playwright su Windows (non fa danni altrove)
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# Import SOLO dal tuo file beta (nessuna logica nuova al di fuori di questo wrapper)
import te_macro_agent_final_multi as beta
from te_macro_agent_final_multi import (
    Config,
    setup_logging,
    TEStreamScraper,
    MacroSummarizer,
    build_selection,
    save_report,
    # funzioni DB/normalizer già presenti nel beta:
    db_init, db_upsert, db_prune, db_load_recent, db_count_by_country,
    normalize_country,
)

# ----------------- Helper locali del wrapper (non toccano la logica del beta) -----------------

def _sort_ctx_key(it):
    return (-int(it.get("importance", 0)),
            -int(it.get("score", 0) if it.get("score") is not None else 0),
            it.get("age_days", 999))

def limit_context_for_es(items_ctx, per_country_cap=140, hard_cap_total=400):
    """
    Riduce il CONTENUTO passato all'ES:
      - per ciascun Paese, prendi i migliori per (importanza ↓, score ↓, recency ↑)
      - cap totale per prudenza
    Non altera i dati nel DB né la selezione finale; serve solo a non esplodere i token in ES.
    """
    by_country = {}
    for it in items_ctx:
        c = it.get("country", "Unknown")
        by_country.setdefault(c, []).append(it)
    clipped = []
    for c, arr in by_country.items():
        # lo score potrebbe non essere valorizzato: lo calcolo con la funzione del beta, se serve
        enriched = []
        for x in arr:
            if "score" not in x or x["score"] is None:
                # Clono per non toccare l’originale
                tmp = dict(x)
                beta.score_item(tmp)
                enriched.append(tmp)
            else:
                enriched.append(x)
        enriched.sort(key=_sort_ctx_key)
        clipped.extend(enriched[: per_country_cap])
    # cap totale extra
    clipped.sort(key=_sort_ctx_key)
    return clipped[: hard_cap_total]

def resilient_es(summarizer, ctx_items_limited, cfg, chosen_countries):
    """
    Chiama executive_summary con retry e backoff.
    Se fallisce (429), riduce ulteriormente il per-country-cap e riprova.
    Ritorna sempre una stringa (anche fallback del beta).
    """
    # primi tentativi con cap "generoso"
    per_country_caps = [140, 100, 70, 50, 35]
    for i, cap in enumerate(per_country_caps, start=1):
        ctx_lim = limit_context_for_es(ctx_items_limited, per_country_cap=cap, hard_cap_total=300)
        try:
            text = summarizer.executive_summary(ctx_lim, cfg, chosen_countries)
            # se il beta ha restituito il fallback, provo un altro giro più corto
            if text.strip().lower().startswith("executive summary non disponibile"):
                # piccolo backoff ed un ulteriore tentativo
                time.sleep(0.8 * i)
                continue
            return text
        except Exception as e:
            # backoff progressivo
            time.sleep(0.8 * i)
            continue
    # ultima spiaggia: usa proprio pochissimo contesto
    try:
        tiny = limit_context_for_es(ctx_items_limited, per_country_cap=20, hard_cap_total=60)
        return summarizer.executive_summary(tiny, cfg, chosen_countries)
    except Exception:
        return "Executive Summary non disponibile per errore di generazione."

def dedupe_countries(selected):
    """
    Converte alias (EU -> Euro Area, US -> United States) e rimuove duplicati.
    Se l’utente ha selezionato sia 'European Union' che 'Euro Area', viene tenuta solo 'Euro Area'.
    """
    normed = [normalize_country(c) for c in selected]
    # dedupe mantenendo ordine di apparizione
    out = []
    seen = set()
    for c in normed:
        if c not in seen:
            out.append(c)
            seen.add(c)
    return out

# ----------------- UI -----------------

st.set_page_config(page_title="StanAI Macro Agent", page_icon="📈", layout="wide")
st.title("📈 StanAI Macro Agent")

left, right = st.columns([1, 2], gap="large")

with left:
    days = st.number_input("Giorni da mostrare nella SELEZIONE", min_value=1, max_value=30, value=5, step=1)
    run_btn = st.button("Esegui pipeline")

with right:
    st.markdown("**Seleziona i Paesi (flag):**")

    countries_all = [
        "United States", "Euro Area", "Germany", "United Kingdom",
        "Italy", "France", "China", "Japan", "Spain", "Netherlands", "European Union",
    ]

    c1, c2, c3 = st.columns([1, 1, 3])
    with c1:
        select_all = st.button("Seleziona tutti")
    with c2:
        deselect_all = st.button("Deseleziona tutti")
    with c3:
        st.caption("Nota: “European Union” è alias di “Euro Area” e verranno unificate.")

    # ✅ Fix NameError: inizializzazione corretta dello stato
    if "country_flags" not in st.session_state:
        st.session_state.country_flags = {name: (name in ["United States", "Euro Area"]) for name in countries_all}

    if select_all:
        for name in countries_all:
            st.session_state.country_flags[name] = True
    if deselect_all:
        for name in countries_all:
            st.session_state.country_flags[name] = False

    cols = st.columns(2)
    for i, country in enumerate(countries_all):
        col = cols[i % 2]
        st.session_state.country_flags[country] = col.checkbox(
            country,
            value=st.session_state.country_flags.get(country, False),
            key=f"chk_{country}"
        )

    chosen_raw = [c for c, v in st.session_state.country_flags.items() if v]
    chosen_countries = dedupe_countries(chosen_raw)

    # Messaggio se EU + Euro Area selezionati (dedupe effettuato)
    if "Euro Area" in chosen_raw and "European Union" in chosen_raw:
        st.info("Hai selezionato sia **Euro Area** che **European Union**: ho unificato in **Euro Area**.")

st.divider()

# ----------------- Esecuzione -----------------
if run_btn:
    # Log “normale”
    setup_logging(logging.INFO)

    cfg = Config()

    # Controllo chiave Anthropic (da .env o da Secrets cloud)
    if not cfg.ANTHROPIC_API_KEY:
        st.error("❌ Nessuna ANTHROPIC_API_KEY trovata. Aggiungila in **Streamlit → Settings → Secrets** come `ANTHROPIC_API_KEY` (senza virgolette).")
        st.stop()

    if not chosen_countries:
        st.warning("Seleziona almeno un Paese prima di eseguire.")
        st.stop()

    st.write(
        f"▶ **Contesto ES:** {cfg.CONTEXT_DAYS} giorni | **Selezione:** {days} giorni | **Paesi (dedupe):** {', '.join(chosen_countries)}"
    )

    # ----------------- Cache/DB: carico/aggiorno (Delta Mode) -----------------
    st.info("Carico/aggiorno **cache (SQLite)**…")
    try:
        conn = db_init(cfg.DB_PATH)
    except Exception as e:
        st.error(f"Errore inizializzazione DB: {e}")
        st.stop()

    # split warm/fresh in base al contenuto DB
    warm, fresh = [], []
    try:
        for c in chosen_countries:
            cnt = db_count_by_country(conn, c)
            if cnt >= cfg.WARMUP_NEW_COUNTRY_MIN:
                warm.append(c)
            else:
                fresh.append(c)
    except Exception as e:
        st.warning(f"Impossibile controllare lo stato DB per paese: {e}")

    scraper = TEStreamScraper(cfg)

    # Scrape “fresh” (finestra ES completa) + “warm” (orizzonte breve)
    try:
        with st.status("Scraping TradingEconomics…", expanded=False) as st_status:
            items_new = []
            if fresh:
                items_new += scraper.scrape_30d(fresh, max_days=cfg.CONTEXT_DAYS)
            if warm:
                items_new += scraper.scrape_30d(warm, max_days=min(cfg.SCRAPE_HORIZON_DAYS, cfg.CONTEXT_DAYS))

            if items_new:
                db_upsert(conn, items_new)
                db_prune(conn, max_age_days=cfg.PRUNE_DAYS)

            items_ctx = db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)

            # fallback se base è scarsa
            if len(items_ctx) < 20:
                st_status.update(label="Base cache scarsa: eseguo scraping completo finestra ES…")
                items_all = scraper.scrape_30d(chosen_countries, max_days=cfg.CONTEXT_DAYS)
                if items_all:
                    db_upsert(conn, items_all)
                    items_ctx = db_load_recent(conn, chosen_countries, max_age_days=cfg.CONTEXT_DAYS)

            st_status.update(label=f"Cache aggiornata. Notizie totali (<= {cfg.CONTEXT_DAYS}gg): {len(items_ctx)}", state="complete")
    except Exception as e:
        st.exception(e)
        st.stop()

    if not items_ctx:
        st.error("❌ Nessuna notizia disponibile nella finestra considerata.")
        st.stop()

    # Anteprima grezza
    with st.expander("Anteprima grezza (prime 60 dal contesto)"):
        try:
            import pandas as pd
            prev = [{
                "time": x.get("time", ""),
                "age_days": x.get("age_days", ""),
                "country": x.get("country", ""),
                "importance": x.get("importance", 0),
                "category_raw": x.get("category_raw", ""),
                "title": (x.get("title", "") or "")[:140],
            } for x in items_ctx[:60]]
            st.dataframe(pd.DataFrame(prev), width='stretch')
        except Exception:
            st.info("Anteprima non disponibile (pandas mancante).")

    # ----------------- Executive Summary (robusto con cap + retry) -----------------
    st.info("Genero l’Executive Summary…")
    try:
        summarizer = MacroSummarizer(cfg.ANTHROPIC_API_KEY, cfg.MODEL, cfg.MODEL_TEMP, cfg.MAX_TOKENS)
    except Exception as e:
        st.error(f"Errore inizializzazione MacroSummarizer: {e}")
        st.stop()

    # Limito il contesto per l’ES PRIMA di chiamare il beta, così evito il 429 sui casi “grassi” (USA)
    es_ctx_limited = limit_context_for_es(items_ctx, per_country_cap=140, hard_cap_total=400)

    es_text = resilient_es(summarizer, es_ctx_limited, cfg, chosen_countries)
    st.subheader("Executive Summary")
    st.write(es_text)

    # ----------------- Selezione (ultimi N giorni) -----------------
    st.info(f"Costruisco la selezione (ultimi {days} giorni)…")
    try:
        selection_items = build_selection(items_ctx, int(days), cfg)
    except TypeError:
        # compat vecchie versioni del beta dove la firma era (items, days)
        selection_items = build_selection(items_ctx, int(days))
    except Exception as e:
        st.exception(e)
        st.stop()

    # ----------------- Traduzione titoli + Riassunti IT -----------------
    st.info("Traduco i titoli e genero i riassunti in italiano…")
    prog = st.progress(0.0)
    total = max(1, len(selection_items))
    for i, it in enumerate(selection_items, 1):
        try:
            it["title_it"] = summarizer.translate_it(it.get("title", ""), cfg)
        except Exception:
            it["title_it"] = it.get("title", "")
        try:
            it["summary_it"] = summarizer.summarize_item_it(it, cfg)
        except Exception:
            it["summary_it"] = (it.get("description", "") or "")
        prog.progress(i / total)

    st.success("✅ Pipeline completata.")

    # ----------------- Anteprima selezione -----------------
    with st.expander("Anteprima Selezione (importanza → score → recency)"):
        try:
            import pandas as pd
            prev_sel = [{
                "time": it.get("time", ""),
                "age_days": it.get("age_days", ""),
                "country": it.get("country", ""),
                "importance": it.get("importance", 0),
                "score": it.get("score", 0),
                "category": it.get("category_mapped", ""),
                "title_it": (it.get("title_it", "") or "")[:120],
            } for it in selection_items]
            st.dataframe(pd.DataFrame(prev_sel), width='stretch')
        except Exception:
            st.info("Anteprima non disponibile (pandas mancante).")

    # ----------------- Report DOCX -----------------
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
    st.write(f"**Notizie totali (Cache+Stream, ultimi {cfg.CONTEXT_DAYS} gg):** {len(items_ctx)}")
    st.write(f"**Notizie selezionate (ultimi {int(days)} gg):** {len(selection_items)}")
    st.caption(f"Esecuzione: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
