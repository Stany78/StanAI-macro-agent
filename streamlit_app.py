# streamlit_app.py — robusto anche se nel modulo non c'è la class TEStreamScraper

import os
import sys
import re
import time
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Tuple

import streamlit as st

# Fix event loop Playwright su Windows
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# ===== Import dal tuo motore =====
try:
    from te_macro_agent_final_multi import (
        Config,
        setup_logging,
        ensure_dir,                       # deve esistere nel tuo file (altrimenti definiremo un fallback)
        split_selection_and_context,
        MacroSummarizer,
        save_word_report,
        parse_age_days_from_text,         # ci serve nel fallback
    )
except Exception as e:
    st.error(f"Errore import dal modulo motore: {e}")
    st.stop()

# Fallback locale per ensure_dir se mancasse nel modulo
def _ensure_dir_local(p: Optional[str]) -> str:
    q = Path(p or ".").expanduser().absolute()
    q.mkdir(parents=True, exist_ok=True)
    return str(q)

if 'ensure_dir' not in globals() or not callable(ensure_dir):  # type: ignore
    ensure_dir = _ensure_dir_local  # type: ignore

# Proviamo a importare TEStreamScraper: se non esiste, definiremo un fallback.
try:
    from te_macro_agent_final_multi import TEStreamScraper  # type: ignore
    _HAS_CLASS_SCRAPER = True
except Exception:
    TEStreamScraper = None  # type: ignore
    _HAS_CLASS_SCRAPER = False

# ===== Fallback TEStreamScraper (se nel tuo file non esiste alcuna class) =====
if not _HAS_CLASS_SCRAPER:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        sync_playwright = None  # gestito a runtime

    def _ensure_playwright():
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright non è disponibile. Installa ed inizializza:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )

    class TEStreamScraperFallback:
        """
        Scraper minimale compatibile con la pipeline:
        espone .scrape(context_days: int, chosen_countries: List[str]) -> (items_selection, items_context)
        """
        def __init__(self, cfg):
            self.cfg = cfg
            self.BASE_URL = getattr(cfg, "BASE_URL", "https://tradingeconomics.com/stream?i=economy")

        def _build_country_union(self, chosen: List[str]) -> Tuple[List[str], List[str]]:
            sel = list(dict.fromkeys([c for c in chosen]))
            ctx = sel.copy()
            # alias Euro Area <-> European Union
            if ("Euro Area" in sel) and ("European Union" not in sel):
                sel.append("European Union")
            if ("European Union" in sel) and ("Euro Area" not in sel):
                sel.append("Euro Area")
            # contesto allargato per ES
            EU_CONTEXT_ALSO = ["Germany", "France", "Italy", "Spain", "Netherlands"]
            if ("Euro Area" in sel) or ("European Union" in sel):
                for c in EU_CONTEXT_ALSO:
                    if c not in ctx:
                        ctx.append(c)
            return sel, ctx

        def _age_days(self, s: str) -> Optional[float]:
            try:
                return parse_age_days_from_text(s)
            except Exception:
                return None

        def _collect_for(self, countries: List[str], deep: bool, context_days: int) -> List[Dict]:
            _ensure_playwright()
            items: List[Dict] = []
            with sync_playwright() as p:  # type: ignore
                browser = p.chromium.launch(
                    headless=getattr(self.cfg, "HEADLESS", True),
                    slow_mo=getattr(self.cfg, "SLOW_MO", 0),
                    args=["--disable-blink-features=AutomationControlled", "--disable-gpu"]
                )
                context = browser.new_context(
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/126.0.0.0 Safari/537.36"),
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                )
                try:
                    # blocco risorse pesanti
                    try:
                        def _block(route):
                            try:
                                url = route.request.url
                                if any(x in url for x in (".png",".jpg",".jpeg",".gif",".webp",".svg",".woff",".woff2",".ttf")):
                                    return route.abort()
                            except Exception:
                                pass
                            return route.continue_()
                        context.route("**/*", _block)
                    except Exception:
                        pass

                    page = context.new_page()
                    page.goto(self.BASE_URL, wait_until="domcontentloaded",
                              timeout=getattr(self.cfg, "NAV_TIMEOUT", 120000))

                    # Cookie
                    for sel in ['#onetrust-accept-btn-handler', 'button:has-text("Accept")', '[class*="cookie"] button']:
                        try:
                            btn = page.locator(sel).first
                            if btn and btn.is_visible():
                                btn.click(timeout=1200)
                                page.wait_for_timeout(300)
                                break
                        except Exception:
                            pass

                    MAX_SCROLL = 44 if deep else 32
                    stagnant = 0
                    prev_count = 0
                    t0 = time.time()
                    hard_timeout_s = 120 if deep else 90

                    for _ in range(MAX_SCROLL):
                        if time.time() - t0 > hard_timeout_s:
                            break
                        page.mouse.wheel(0, 1700)
                        page.wait_for_timeout(420)
                        try:
                            more = page.locator('button:has-text("More"), a:has-text("More")')
                            if more and more.count() > 0:
                                more.nth(0).click(timeout=1000)
                                page.wait_for_timeout(500)
                        except Exception:
                            pass

                        cur_count = page.evaluate(
                            """(countries) => {
                                const all = document.querySelectorAll('li.te-stream-item, div.stream-item, article');
                                let cnt = 0;
                                for (const el of all) {
                                    const n = el.querySelector('a.te-stream-country');
                                    const t = n ? (n.textContent || '').trim() : '';
                                    if (countries.includes(t)) cnt++;
                                }
                                return cnt;
                            }""",
                            countries
                        )
                        stagnant = stagnant + 1 if cur_count <= prev_count else 0
                        prev_count = max(prev_count, cur_count)
                        if stagnant >= (5 if deep else 4):
                            break

                    raw_list = page.evaluate(
                        """(countries) => {
                            const nodes = Array.from(document.querySelectorAll('li.te-stream-item, div.stream-item, article'))
                              .filter(el => {
                                  const c = el.querySelector('a.te-stream-country');
                                  const t = c ? (c.textContent || '').trim() : '';
                                  return countries.includes(t);
                              });
                            return nodes.map((el) => {
                                const getTxt = (q) => {
                                    const n = el.querySelector(q);
                                    return n ? (n.textContent || "").trim() : "";
                                };
                                const firstTxt = (qs) => { for (const q of qs) { const t = getTxt(q); if (t) return t; } return ""; };
                                const small = el.querySelector("small");
                                const countryNode = el.querySelector("a.te-stream-country");
                                const catNode = el.querySelector("a.te-stream-category");
                                const titleNode = el.querySelector("a.te-stream-title");
                                let desc = firstTxt(["span.te-stream-item-description",".desc","p",".summary","td"]);
                                if (!desc) desc = (el.textContent || "").trim();
                                if (desc.length > 2000) desc = desc.slice(0, 2000);
                                return {
                                    country: countryNode ? countryNode.textContent.trim() : "",
                                    title: (titleNode ? titleNode.textContent : firstTxt(["h3","h2","a",".te-title","strong"])).trim(),
                                    description: desc,
                                    time_text: small ? small.textContent.trim() : "",
                                    category_raw: catNode ? catNode.textContent.trim() : ""
                                };
                            });
                        }""",
                        countries
                    ) or []

                    for r in raw_list:
                        country = (r.get("country") or "").strip()
                        if country not in countries:
                            continue
                        age_days = self._age_days(r.get("time_text","")) or self._age_days(r.get("description",""))
                        if age_days is None or age_days > float(context_days):
                            continue

                        title = (r.get("title") or "").strip() or f"Update — {country}"
                        desc_full = (r.get("description") or "").strip()

                        items.append({
                            "country": country,
                            "title": title[:220],
                            "description": desc_full,
                            "time": r.get("time_text","") or f"{age_days:.2f} days ago",
                            "age_days": age_days,
                            "category_raw": (r.get("category_raw") or "").strip(),
                            "important": False,             # fallback minimal
                            "values": "",
                            "allowed_numbers": [],
                        })
                finally:
                    browser.close()

            return items

        def scrape(self, context_days: int, chosen_countries: List[str]):
            sel_countries, ctx_countries = self._build_country_union(chosen_countries)
            items_selection = self._collect_for(sel_countries, deep=False, context_days=context_days)
            items_context  = self._collect_for(ctx_countries, deep=True,  context_days=context_days)
            logging.info("Scraped raw (selection scope): %d | (context union): %d",
                         len(items_selection), len(items_context))
            return items_selection, items_context

# Scegli quale scraper usare
_ScraperClass = TEStreamScraper if _HAS_CLASS_SCRAPER else TEStreamScraperFallback


# ================== UI ==================
st.set_page_config(page_title="TE Macro Agent (Streamlit)", page_icon="📈", layout="wide")
st.title("📈 TE Macro Agent — Interfaccia Streamlit")

# Parametri
col1, col2 = st.columns([1, 2], gap="large")
with col1:
    selection_days = st.number_input("Giorni da analizzare (SELEZIONE) [1–30]", 1, 30, 5, 1)

with col2:
    st.markdown("**Paesi (seleziona con i flag)**")
    countries_all = [
        "United States", "Euro Area", "Germany", "United Kingdom",
        "Italy", "France", "China", "Japan"
    ]
    c_a, c_b = st.columns(2)
    with c_a: btn_all = st.button("Seleziona tutti")
    with c_b: btn_none = st.button("Deseleziona tutti")

    if "country_flags" not in st.session_state:
        st.session_state.country_flags = {c: False for c in countries_all}
    if btn_all:
        for c in countries_all: st.session_state.country_flags[c] = True
    if btn_none:
        for c in countries_all: st.session_state.country_flags[c] = False

    cols = st.columns(2)
    for i, country in enumerate(countries_all):
        col = cols[i % 2]
        st.session_state.country_flags[country] = col.checkbox(
            country,
            value=st.session_state.country_flags.get(country, False),
            key=f"chk_{country}"
        )
    chosen_countries = [c for c, v in st.session_state.country_flags.items() if v]

log_debug = st.checkbox("Log DEBUG", value=False)
run = st.button("Esegui report")

# ================== Esecuzione ==================
if run:
    setup_logging(logging.DEBUG if log_debug else logging.INFO)

    cfg = Config()
    if not getattr(cfg, "ANTHROPIC_API_KEY", ""):
        st.error("❌ Nessuna ANTHROPIC_API_KEY trovata nel tuo .env.")
        st.stop()

    if not chosen_countries:
        st.warning("Seleziona almeno un Paese.")
        st.stop()

    st.write(f"▶ Selezione: **{selection_days}** giorni | Contesto ES: **30** giorni | Paesi: {', '.join(chosen_countries)}")

    # Scraping
    try:
        with st.status("Scraping TradingEconomics…", expanded=False) as s:
            scraper = _ScraperClass(cfg)
            items_selection, items_context = scraper.scrape(context_days=30, chosen_countries=chosen_countries)
            s.update(label="Scraping completato", state="complete")
    except Exception as e:
        st.exception(e)
        st.stop()

    if not items_selection and not items_context:
        st.error("❌ Nessuna notizia trovata.")
        st.stop()

    # Split selezione/contesto (dal tuo modulo)
    try:
        selected, context = split_selection_and_context(items_selection, items_context, int(selection_days), cfg.FUNDAMENTALS)
    except Exception as e:
        st.exception(e)
        st.stop()

    # Anteprima
    with st.expander("Anteprima notizie selezionate (prima dei riassunti)"):
        import pandas as pd
        preview = [{
            "time": it.get("time",""),
            "country": it.get("country",""),
            "category_mapped": it.get("category_mapped",""),
            "important": "RED" if it.get("important") else "-",
            "score": it.get("score", 0),
            "title": (it.get("title","") or "")[:160],
        } for it in selected[:50]]
        st.dataframe(pd.DataFrame(preview), use_container_width=True)

    # Summaries
    try:
        summarizer = MacroSummarizer(cfg.ANTHROPIC_API_KEY, cfg.MODEL, cfg.TEMP, cfg.MAX_TOKENS)
    except Exception as e:
        st.error(f"Errore inizializzazione MacroSummarizer: {e}")
        st.stop()

    st.info("Genero i riassunti (≤100 parole)…")
    prog = st.progress(0.0)
    for i, it in enumerate(selected, 1):
        try:
            it["summary"] = summarizer.summarize_item(it, cfg)
        except Exception as e:
            logging.error("Errore summarizer '%s': %s", it.get("title","")[:90], e)
            it["summary"] = "(riassunto non disponibile)"
        prog.progress(i / max(1, len(selected)))

    # Executive Summary (dal tuo modulo)
    st.info("Genero l’Executive Summary…")
    try:
        exec_sum, es_changed = summarizer.executive_summary(selected, context, cfg, chosen_countries)  # v3: ritorna (testo, bool)
    except Exception as e:
        logging.error("Errore Executive Summary: %s", e)
        exec_sum, es_changed = "(executive summary non disponibile)", False

    st.success("✅ Pipeline completata.")
    st.subheader("Executive Summary")
    st.write(exec_sum)

    # DOCX
    try:
        out_dir = ensure_dir(getattr(cfg, "OUTPUT_DIR", "."))
        path = save_word_report(selected, int(selection_days), out_dir, exec_sum, chosen_countries, context, es_sanitized_changed=es_changed)
        st.info(f"Report salvato: `{path}`")
        try:
            data = Path(path).read_bytes()
            st.download_button(
                "📥 Scarica report DOCX",
                data=data,
                file_name=Path(path).name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
        except Exception as e:
            st.warning(f"Report creato ma non scaricabile ora: {e}")
    except Exception as e:
        st.error(f"Errore nella generazione/salvataggio DOCX: {e}")

    # Riepilogo
    try:
        from collections import Counter
        by_country_sel = Counter([it["country"] for it in selected])
        by_country_ctx = Counter([it["country"] for it in context])
        by_cat = {}
        for it in selected:
            by_cat[it["category_mapped"]] = by_cat.get(it["category_mapped"], 0) + 1

        st.write("---")
        st.write("**Riepilogo:**")
        st.write(f"• Notizie selezionate: {len(selected)} | Distribuzione Paese (Selezione): {dict(by_country_sel)}")
        st.write(f"• Distribuzione Paese (Contesto 30gg): {dict(by_country_ctx)}")
        st.write(f"• Distribuzione Categoria (Selezione): {by_cat}")
        st.caption(f"Esecuzione: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    except Exception:
        pass
