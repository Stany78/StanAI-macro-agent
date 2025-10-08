#!/usr/bin/env python3
# te_macro_agent_auto_select_freshfirst.py
# Fresh-first: pool = ultime N×24h; selezione colore-first hard; ES 30gg (invariato).
#
# pip install python-docx playwright python-dotenv python-dateutil anthropic
# playwright install chromium

import os, re, time, logging, unicodedata, sqlite3, hashlib
from dataclasses import dataclass
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any

from dotenv import load_dotenv
from dateutil import parser as dtparser
from playwright.sync_api import sync_playwright
from docx import Document
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT

# =============== Setup ===============
BASE_DIR = Path(__file__).parent.absolute()
load_dotenv(BASE_DIR / ".env")

def setup_logging(level=logging.INFO):
    logging.basicConfig(level=level,
                        format="%(asctime)s | %(levelname)s | %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

@dataclass
class Config:
    HEADLESS: bool = True
    NAV_TIMEOUT: int = 120_000
    SLOW_MO: int = 0
    OUTPUT_DIR: str = str(BASE_DIR)

    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-3-haiku-20240307")
    MODEL_TEMP: float = float(os.getenv("AI_TEMP", "0.2"))
    MAX_TOKENS: int = 1500

    CONTEXT_DAYS_ES: int = 30
    BASE_URL: str = "https://www.tradingeconomics.com/stream?i=economy"

    USE_DB: bool = True
    DB_PATH: str = str(BASE_DIR / "news_cache.sqlite")
    PRUNE_DAYS: int = 60

    MAX_NEWS: int = 12

# =============== Time parsing (ITA/EN) ===============
REL_EN = re.compile(r"\b(\d+)\s*(minute|hour|day|week|month)s?\s+ago\b", re.I)
REL_IT = re.compile(r"\b(\d+)\s*(minut[oi]|ora|ore|giorn[oi]|settimana[e]?|mes[ei])\s+fa\b", re.I)
REL_H  = re.compile(r"\b(\d+)\s*h(?:ours?)?\s*ago\b", re.I)

def parse_age_days(time_text: str) -> Optional[float]:
    if not time_text: return None
    t = (time_text or "").strip().lower()
    if "oggi" in t or "today" in t: return 0.0
    if "ieri" in t or "yesterday" in t: return 1.0
    m = REL_H.search(t)
    if m: return int(m.group(1)) / 24.0
    m = REL_EN.search(t)
    if m:
        q = int(m.group(1)); u = m.group(2).lower()
        return q/1440.0 if u.startswith("minute") else q/24.0 if u.startswith("hour") else float(q) if u.startswith("day") else float(q)*7.0 if u.startswith("week") else 30.0
    m = REL_IT.search(t)
    if m:
        q = int(m.group(1)); u = m.group(2)
        return q/1440.0 if u.startswith("minut") else q/24.0 if u in ("ora","ore") else float(q) if u.startswith("giorn") else float(q)*7.0 if u.startswith("settimana") else 30.0
    try:
        dt = dtparser.parse(time_text, fuzzy=True)
        return max(0.0, (datetime.now() - dt).total_seconds()/86400.0)
    except Exception:
        return None

# =============== Country normalize (IT↔EN) ===============
COUNTRY_MAP = {
    "United States":"United States","US":"United States","USA":"United States","U.S.":"United States",
    "Stati Uniti":"United States",
    "Euro Area":"Euro Area","Eurozone":"Euro Area","European Union":"Euro Area",
    "Area Euro":"Euro Area","Eurozona":"Euro Area","UE":"Euro Area","Ue":"Euro Area","Unione Europea":"Euro Area",
    "Germany":"Germany","Germania":"Germany",
    "United Kingdom":"United Kingdom","UK":"United Kingdom","Regno Unito":"United Kingdom",
    "Italy":"Italy","Italia":"Italy",
    "France":"France","Francia":"France",
    "China":"China","Cina":"China",
    "Japan":"Japan","Giappone":"Japan",
    "Spain":"Spain","Spagna":"Spain",
    "Netherlands":"Netherlands","Paesi Bassi":"Netherlands",
}
def normalize_country(n: str) -> str:
    return COUNTRY_MAP.get((n or "").strip(), (n or "").strip())

# =============== DB cache (SQLite) ===============
def _norm(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[–—\-:;,\.!?\(\)\[\]\{\}]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()

def _fp(it: dict) -> str:
    base = f"{it.get('country','')}|{_norm(it.get('title',''))}|{_norm((it.get('description') or '')[:200])}"
    return hashlib.sha1(base.encode("utf-8","ignore")).hexdigest()

def db_init(path: str):
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute("""
      CREATE TABLE IF NOT EXISTS te_items(
        key TEXT PRIMARY KEY,
        country TEXT, title TEXT, description TEXT,
        time_text TEXT, importance INTEGER, category_raw TEXT,
        first_seen_ts REAL, last_seen_ts REAL
      )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_seen ON te_items(country,last_seen_ts)")
    conn.commit()
    return conn

def db_upsert(conn, items: list):
    now = time.time()
    c = conn.cursor()
    for it in items:
        k = _fp(it)
        c.execute("""
          INSERT INTO te_items(key,country,title,description,time_text,importance,category_raw,first_seen_ts,last_seen_ts)
          VALUES(?,?,?,?,?,?,?, ?, ?)
          ON CONFLICT(key) DO UPDATE SET
            last_seen_ts=excluded.last_seen_ts,
            time_text=excluded.time_text,
            importance=excluded.importance
        """, (k, it.get("country",""), it.get("title",""), it.get("description",""),
              it.get("time",""), int(it.get("importance",0)), it.get("category_raw",""),
              now, now))
    conn.commit()

def db_load(conn, countries: List[str], max_age_days: int) -> List[Dict[str,Any]]:
    if not countries: return []
    cutoff = time.time() - max_age_days*86400
    qs = ",".join("?"*len(countries))
    c = conn.cursor()
    c.execute(f"""
      SELECT country,title,description,time_text,importance,category_raw,last_seen_ts
      FROM te_items
      WHERE last_seen_ts >= ? AND country IN ({qs})
    """, [cutoff, *countries])
    now = time.time()
    out=[]
    for country,title,desc,tt,imp,cat,seen in c.fetchall():
        age_tt = parse_age_days(tt or "")
        age_db = max(0.0,(now-float(seen))/86400.0)
        age_days = age_tt if (age_tt is not None and age_tt <= 120) else age_db
        out.append({
            "country": country, "title": title or "", "description": desc or "",
            "time": tt or "", "importance": int(imp or 0), "category_raw": cat or "",
            "age_days": age_days
        })
    return out

def db_prune(conn, max_age_days: int):
    cutoff = time.time() - max_age_days*86400
    c = conn.cursor()
    c.execute("DELETE FROM te_items WHERE last_seen_ts < ?", (cutoff,))
    conn.commit()

# =============== Scraper TE ===============
class TEStreamScraper:
    def __init__(self, cfg: Config): self.cfg = cfg

    @staticmethod
    def _map_color(class_text: str, style_text: str) -> int:
        t = f"{class_text} {style_text}".lower()
        # ROSSO / high
        tokens_red = [
            "high-impact","impact-high","impatto alto",
            "alert-danger","bg-danger","text-bg-danger","badge-danger","text-danger","border-danger",
            "danger-subtle","#dc3545","rgb(220, 53, 69)","rgb(248, 215, 218)","rgba(248, 215, 218, 1)"
        ]
        if any(tok in t for tok in tokens_red): return 3
        # BLU / medium
        tokens_blue = ["alert-primary","bg-primary","text-bg-primary","badge-primary","text-primary","#0d6efd","primary"]
        if any(tok in t for tok in tokens_blue): return 2
        # AZZURRO / low
        tokens_cyan = ["alert-info","bg-info","text-bg-info","badge-info","text-info","#0dcaf0","info"]
        if any(tok in t for tok in tokens_cyan): return 1
        return 0

    def scrape_stream(self, countries: List[str], horizon_days: int=30) -> List[Dict[str,Any]]:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.cfg.HEADLESS, slow_mo=self.cfg.SLOW_MO,
                args=["--disable-blink-features=AutomationControlled","--disable-gpu"])
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36)"),
                viewport={"width":1440,"height":900}, locale="en-US")
            try:
                ctx.route("**/*", lambda r: r.abort() if any(x in r.request.url for x in
                    [".png",".jpg",".jpeg",".gif",".webp",".svg",".woff",".woff2",".ttf",".mp4",".avi",".webm",".css?","doubleclick","googletag","analytics"])
                    else r.continue_())
            except Exception: pass
            page = ctx.new_page()

            def safe_goto():
                attempts = [self.cfg.BASE_URL]
                if "://www." not in self.cfg.BASE_URL:
                    attempts.append(self.cfg.BASE_URL.replace("://","://www.",1))
                else:
                    attempts.append(self.cfg.BASE_URL.replace("://www.","://",1))
                last = None
                for u in dict.fromkeys(attempts):
                    try:
                        page.goto(u, wait_until="domcontentloaded", timeout=self.cfg.NAV_TIMEOUT)
                        return
                    except Exception as e:
                        last = e
                raise last or RuntimeError("Impossibile aprire TradingEconomics")
            try:
                safe_goto()
            except Exception as e:
                logging.error("Navigazione fallita: %s", e)
                try: browser.close()
                except Exception: pass
                return []

            for sel in ['#onetrust-accept-btn-handler','button:has-text("Accept")','[class*="cookie"] button']:
                try:
                    btn = page.locator(sel).first
                    if btn and btn.is_visible(): btn.click(timeout=1000); page.wait_for_timeout(200); break
                except Exception: pass

            try:
                page.wait_for_selector('li.te-stream-item, div.stream-item, article', timeout=10_000)
            except Exception:
                logging.warning("Nessuna card visibile entro 10s.")

            older_hits=0
            for _ in range(100):
                try:
                    if page.is_closed(): break
                    page.evaluate("window.scrollBy(0, 1600);")
                except Exception: break
                page.wait_for_timeout(350)
                for m in ['#stream-btn:has-text("More")','button:has-text("More")','a:has-text("More")']:
                    try:
                        mb = page.locator(m).first
                        if mb and mb.is_visible(): mb.click(); page.wait_for_timeout(420)
                    except Exception: pass
                try:
                    tails = page.evaluate("""() => {
                        const nodes = Array.from(document.querySelectorAll('li.te-stream-item, div.stream-item, article'));
                        return nodes.slice(-25).map(n => (n.querySelector('small')?.textContent || '').trim());
                    }""") or []
                    ages=[]
                    for tx in tails:
                        a = parse_age_days(str(tx))
                        if a is not None: ages.append(a)
                    if ages and min(ages) > float(horizon_days):
                        older_hits += 1
                    else:
                        older_hits = 0
                    if older_hits >= 2: break
                except Exception: pass

            def _extract_all():
                return page.evaluate("""() => {
                    const pick = (el, sels) => { for (const s of sels){ const n=el.querySelector(s); if(n){ const t=(n.textContent||'').trim(); if(t) return t; } } return ''; };
                    const selsCountry = ['a.te-stream-country','.te-stream-country','a[href*="/country/"]','a[href*="/countries/"]','.country a','[data-entity="country"]','[data-country]'];
                    const selsTitle   = ['a.te-stream-title','h3','h2','a','.te-title','strong'];
                    const nodes = Array.from(document.querySelectorAll('li.te-stream-item, div.stream-item, article'));
                    return nodes.map(el => {
                        const country = pick(el, selsCountry);
                        const title   = pick(el, selsTitle);
                        const desc    = (el.querySelector('span.te-stream-item-description')?.textContent
                                         || el.querySelector('.desc')?.textContent
                                         || el.querySelector('p')?.textContent
                                         || el.textContent || '').trim();
                        const time    = (el.querySelector('small')?.textContent || '').trim();
                        const impactEl= el.querySelector('.te-stream-impact, .impact, [data-impact]');
                        const impact_txt = (impactEl?.textContent || '').trim().toLowerCase();
                        const cls     = [el.getAttribute('class')||'',
                                         el.querySelector('.te-stream-impact')?.getAttribute('class')||'',
                                         el.querySelector('small')?.getAttribute('class')||'',
                                         el.querySelector('.te-stream-title')?.getAttribute('class')||'',
                                         impactEl?.getAttribute('class')||''].join(' ');
                        const sty     = [el.getAttribute('style')||'',
                                         el.querySelector('.te-stream-impact')?.getAttribute('style')||'',
                                         el.querySelector('small')?.getAttribute('style')||'',
                                         impactEl?.getAttribute('style')||''].join(' ');
                        const bg_color = (globalThis.getComputedStyle ? getComputedStyle(el).backgroundColor : '') || '';
                        const cat     = (el.querySelector('a.te-stream-category')?.textContent||'').trim();
                        return {country, title, description: desc.slice(0,2000), time_text: time,
                                class_blob: cls, style_blob: sty, bg_color, impact_txt, category_raw: cat};
                    });
                }""") or []

            raw = _extract_all()
            if not raw:
                for _ in range(6):
                    try: page.evaluate("window.scrollBy(0, 1800);")
                    except Exception: break
                    page.wait_for_timeout(380)
                raw = _extract_all()

            browser.close()

        chosen = set(normalize_country(c) for c in countries)
        items=[]
        for r in raw:
            country = normalize_country((r.get("country") or "").strip())
            if not country or country not in chosen: continue
            age = parse_age_days(r.get("time_text","")) or parse_age_days(r.get("description",""))
            if age is None or age > float(horizon_days): continue
            importance = self._map_color(
                r.get("class_blob",""),
                " ".join([r.get("style_blob",""), r.get("bg_color",""), r.get("impact_txt","")])
            )
            items.append({
                "country": country,
                "title": (r.get("title","") or "").strip(),
                "description": (r.get("description","") or "").strip(),
                "time": (r.get("time_text","") or "").strip(),
                "age_days": age,
                "importance": importance,
                "category_raw": (r.get("category_raw","") or "").strip(),
            })

        # Debug colore (prime 3 più recenti)
        for it in sorted(items, key=lambda x: x.get("age_days",999))[:3]:
            logging.info("DEBUG colore: title='%s' age=%.2f imp=%s", it["title"], it["age_days"], it["importance"])

        logging.info("Scraper: raccolte %d card (<=%s gg) per %s", len(items), horizon_days, ",".join(countries))
        return items

# =============== Classificazione & rating ===============
GDP_RX  = re.compile(r"\b(gdp|gross domestic product|gdp growth rate|growth)\b", re.I)
INFL_RX = re.compile(r"\b(cpi|pce|ppi|inflation|deflator|core|prezzi|price)\b", re.I)
LAB_RX  = re.compile(
    r"\b(nonfarm|payrolls?|unemployment|jobless|claims|wage|earnings|"
    r"job\s+openings|jolts|quits|layoffs|discharges|hires|separations|"
    r"offerte\s+di\s+lavoro|dimissioni|licenziamenti|assunzioni)\b", re.I
)
PMI_RX  = re.compile(r"\b(pmi|ism|ifo|nbs|caixin|s&p\s*global|composite|manufacturing|services)\b", re.I)
CONF_RX = re.compile(r"\b(fidu(?:cia|ce)\s+dei\s+consumatori|clima\s+di\s+fiducia|consumer\s+confidence|consumer\s+sentiment|ecfin|gfk|conference\s+board|michigan|cci)\b", re.I)
HOU_RX  = re.compile(r"\b(housing|home\s+sales|mortgage|building\s+permits|housing\s+starts|existing\s+home|new\s+home|home\s+prices?|property\s+investment)\b", re.I)
RETAIL_RX = re.compile(r"\b(retail\s+sales|vendite\s+al\s+dettaglio)\b", re.I)
IP_RX     = re.compile(r"\b(industrial\s+production|produzione\s+industriale)\b", re.I)
PMI_REGIONAL = re.compile(r"\b(Richmond|Kansas\s*City|Dallas|Philadelphia|Philly|Empire|New\s*York|NY\s*Fed|Chicago|Atlanta|Cleveland)\b", re.I)
RENDITI_KEYS = re.compile(r"\b(rendimento|rendimenti|treasury|decennale|yield|t-?note|t-?bond|curve|fomc|powell|minutes|dot\s*plot|sep|jackson\s*hole)\b", re.I)

CATEGORY_POINTS = {"inflazione":20,"lavoro":18,"fiducia":18,"crescita":16,"pmi":14,"housing":10,"rendimenti":8,"altro":6}

def has_numbers(text: str) -> bool:
    return bool(re.search(r"\d+(?:[.,]\d+)?\s*%?", text or ""))

RESULT_EN_RX = re.compile(
    r"\b(increased|rose|rises|rising|fell|falling|declined|decreased|"
    r"edged\s+(up|down)|was\s+little\s+changed|little\s+changed|unchanged|"
    r"revised|rebounded|accelerated|slowed|stabilized|stabilised|"
    r"stood\s+at|reached|hit|came\s+in\s+at|grew|contracted)\b", re.I
)
RESULT_IT_RX = re.compile(
    r"\b(e|e')\s+(salit[oaie]|sc[eè]s[oaie]|aumentat[oaie]|diminuit[oaie]|"
    r"accelerat[oaie]|rallentat[oaie]|rivist[oaie]|stabilizzat[oaie]|"
    r"pubblicat[oaie]|attestat[oaie])\b", re.I
)

def is_preview(text: str) -> bool:
    return bool(re.search(r"\b(ahead of|before the|outlook|preview|in vista di|settimana prossima|week ahead|calendario|agenda|atteso|previsioni|previsto)\b", (text or "").lower()))

def is_result(text: str) -> bool:
    if is_preview(text): return False
    t = (text or "")
    tl = t.lower()
    if has_numbers(t) and re.search(r"\b(yoy|y/y|mom|m/m|qoq|q/q|annuo|mensile|trimestrale|bps|punti|%)\b", tl): return True
    if RESULT_EN_RX.search(tl) or RESULT_IT_RX.search(tl): return True
    return False

def detect_category(text: str) -> str:
    t=(text or "").lower()
    if RENDITI_KEYS.search(t): return "rendimenti"   # PRIMA (cap=1)
    if CONF_RX.search(t):      return "fiducia"      # separata dai PMI
    if PMI_RX.search(t):       return "pmi"
    if GDP_RX.search(t) or RETAIL_RX.search(t) or IP_RX.search(t) or re.search(r"\b(factory\s+orders|trade\s+balance|export|import|current\s+account)\b", t): return "crescita"
    if INFL_RX.search(t):      return "inflazione"
    if LAB_RX.search(t):       return "lavoro"
    if HOU_RX.search(t):       return "housing"
    return "altro"

def enrich_item(it: Dict[str,Any]) -> Dict[str,Any]:
    text = f"{it.get('title','')} {it.get('description','')}"
    cat = detect_category(text)
    res = is_result(text)
    nums = has_numbers(text)
    score = CATEGORY_POINTS.get(cat,6) + (10 if res else 0) + (5 if nums else 0)
    out = dict(it)
    out["category_mapped"] = cat
    out["is_result"] = res
    out["has_numbers"] = nums
    out["score"] = max(0, min(100, score))
    out["is_pmi_regional"] = bool(PMI_REGIONAL.search(text)) if cat=="pmi" else False
    out["is_yields_or_fed"] = bool(RENDITI_KEYS.search(text)) if cat=="rendimenti" else False
    return out

# =============== ES (30 gg, invariato) ===============
def _normalize_spaces_in_perc(text: str) -> str:
    if not text: return text
    t = re.sub(r"\s+%", "%", text)
    t = re.sub(r"%(?=[A-Za-zÀ-ÖØ-öø-ÿ])", "% ", t)
    t = re.sub(r"(\d)[\s]+([.,])[\s]*(\d)", r"\1\2\3", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()

def build_es_input_text(items: List[Dict[str,Any]]) -> str:
    def is_gdp(it): 
        return bool(re.search(r"\b(gdp|gross domestic product|gdp growth rate)\b",
                              (it.get("title","")+" "+it.get("description","")).lower()))
    gdp  = [x for x in items if is_gdp(x)]
    rest = [x for x in items if not is_gdp(x)]
    def k(x): return (-int(x.get("importance",0)), x.get("age_days",999), (x.get("title","") or "")[:60])
    gdp.sort(key=k); rest.sort(key=k)
    ordered = gdp + rest
    return "\n".join(f"{it.get('country','')}: {it.get('title','')}. {it.get('description','')}" for it in ordered)

PROMPT_ES = (
  "Sei un analista macroeconomico e devi scrivere un report narrativo e coerente, "
  "mantenendo tutti i dati numerici forniti e senza introdurne di nuovi. "
  "Dai priorità a GDP/crescita, inflazione (CPI/PCE/PPI), lavoro e PMI. "
  "Non usare elenchi: 3–5 paragrafi fluidi; collega i dati a politica monetaria e crescita; chiudi con rischi e prospettive."
)

def _strip_intro(text: str) -> str:
    if not text: return text
    t = text.strip()
    t = re.sub(r"^\s*(ecco\s+(un|il)\s+(report|executive\s*summary)[^:\n]*[:.\-–—]\s+)", "", t, flags=re.I)
    t = re.sub(r"^\s*(executive\s*summary\s*:?\s*)", "", t, flags=re.I)
    return t.strip()

class MacroSummarizer:
    def __init__(self, api_key, model, temp, max_tokens):
        import anthropic
        if not api_key: raise RuntimeError("ANTHROPIC_API_KEY mancante")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model, self.temp, self.max_tokens = model, temp, max_tokens

    def executive_summary(self, context_items: List[Dict[str,Any]], cfg: Config) -> str:
        ctx = [x for x in context_items if x.get("age_days") is not None and x["age_days"] <= cfg.CONTEXT_DAYS_ES]
        txt = build_es_input_text(ctx) if ctx else "Nessun contenuto."
        try:
            r = self.client.messages.create(
                model=self.model, temperature=self.temp, max_tokens=min(cfg.MAX_TOKENS,1600),
                messages=[{"role":"user","content":f"{PROMPT_ES}\n\nTESTO DA RIELABORARE:\n{txt}"}]
            )
            out = (r.content[0].text if r and r.content else "").strip()
            return _strip_intro(_normalize_spaces_in_perc(out))
        except Exception as e:
            logging.error("Errore ES: %s", e)
            return "Executive Summary non disponibile."

    def translate_it(self, title: str) -> str:
        if not title: return ""
        try:
            r = self.client.messages.create(
                model=self.model, temperature=min(self.temp,0.2), max_tokens=120,
                messages=[{"role":"user","content":"Traduci in ITALIANO questo titolo. Rispondi SOLO col testo tradotto.\n\n"+title}]
            )
            out = (r.content[0].text if r and r.content else "").strip()
            out = re.sub(r"^\s*(titolo|traduzione|ecco).*?:\s*","",out,flags=re.I)
            return _normalize_spaces_in_perc(out)
        except Exception:
            return title

    def summarize_it(self, item: Dict[str,Any]) -> str:
        title = (item.get("title","") or "").strip()
        desc  = (item.get("description","") or "").strip()
        country = (item.get("country","") or "").strip()
        payload = f"TITOLO: {title}\nPAESE: {country}\nTESTO: {desc}"
        prompt = (
          "Scrivi un riassunto in ITALIANO della seguente notizia economica. "
          "Usa 100–120 parole, tono professionale e chiaro, senza elenchi. "
          "Mantieni tutti i numeri presenti senza introdurne di nuovi. Inizia subito.\n\n"
          f"{payload}"
        )
        try:
            r = self.client.messages.create(
                model=self.model, temperature=min(self.temp,0.3), max_tokens=480,
                messages=[{"role":"user","content":prompt}]
            )
            out = (r.content[0].text if r and r.content else "").strip()
            return _normalize_spaces_in_perc(out)
        except Exception:
            return (desc or title)[:180]

# =============== Selezione (Fresh-first, color-first hard) ===============
def _similar(a: str, b: str) -> float:
    def _n(s): return re.sub(r"\s+"," ",unicodedata.normalize("NFKD",s or "").lower()).strip()
    return SequenceMatcher(None, _n(a), _n(b)).ratio()

def is_duplicate_safe(it: dict, final_list: list) -> bool:
    # Mai dedup se ROSSA ≤24h
    if int(it.get("importance",0)) == 3 and (it.get("age_days") or 999) <= 1.0:
        return False
    for x in final_list:
        if x.get("country","") != it.get("country",""): continue
        sim = _similar(it.get("title",""), x.get("title",""))
        age_i = it.get("age_days",999); age_x = x.get("age_days",999)
        if sim >= 0.90 and abs((age_i or 999) - (age_x or 999)) <= 2.0:
            return True
    return False

def build_selection_freshfirst(items_stream: List[Dict[str,Any]],
                               items_cache: List[Dict[str,Any]],
                               days_N: int, max_news: int) -> List[Dict[str,Any]]:
    hours_thr = int(round(days_N)) * 24
    def to_hours(d): return (d or 0.0) * 24.0

    combined, seen_fp = [], set()
    for it in (items_stream + items_cache):
        if it.get("age_days") is None: continue
        if to_hours(it["age_days"]) > hours_thr: continue
        k = _fp(it)
        if k in seen_fp: continue
        seen_fp.add(k)
        combined.append(it)
    if not combined:
        logging.info("Pool vuoto entro %sh (≈%s gg).", hours_thr, days_N)
        return []

    pool = [enrich_item(it) for it in combined]

    has_nat_pmi = any(i["category_mapped"]=="pmi" and not i["is_pmi_regional"] for i in pool)
    if has_nat_pmi:
        pool = [i for i in pool if not (i["category_mapped"]=="pmi" and i["is_pmi_regional"])]

    conf_pool = [i for i in pool if CONF_RX.search((i.get('title','')+' '+i.get('description','')).lower())]
    logging.info("Fiducia nel pool (<=%sgg): %d", days_N, len(conf_pool))

    buckets = {3: [], 2: [], 1: [], 0: []}
    for i in pool:
        buckets[int(i.get("importance",0))].append(i)

    def key_in_bucket(i): return (-int(i.get("score",0)), i.get("age_days",9999))
    for k in buckets:
        buckets[k].sort(key=key_in_bucket)

    final=[]
    def already_yields(lst): return any(x.get("category_mapped")=="rendimenti" for x in lst)

    for color in (3, 2, 1, 0):
        for it in buckets[color]:
            if len(final) >= max_news: break
            if is_duplicate_safe(it, final): continue
            if it["category_mapped"]=="rendimenti" and already_yields(final): continue
            final.append(it)
        if len(final) >= max_news: break

    conf_sel = [i for i in final if i.get("category_mapped")=="fiducia"]
    logging.info("Fiducia selezionata: %d", len(conf_sel))
    by_color = {3:0,2:0,1:0,0:0}
    for i in final: by_color[int(i.get("importance",0))]+=1
    logging.info("Selezione per colore: rosse=%d, blu=%d, azzurre=%d, neutre=%d",
                 by_color[3],by_color[2],by_color[1],by_color[0])
    logging.info("Preview incluse (info): %d", sum(1 for i in final if not i.get("is_result")))
    return final[:max_news]

# =============== Report DOCX ===============
def trim_words(text: str, max_words: int=120) -> str:
    if not text: return ""
    ws = text.strip().split()
    if len(ws) <= max_words: return text.strip()
    cut = " ".join(ws[:max_words]).rstrip()
    if "." in cut:
        tmp = ".".join(cut.split(".")[:-1]).strip()
        if tmp: return tmp + "."
    return cut

def save_report(filename: str, es_text: str, sel: List[Dict[str,Any]],
                countries: List[str], days_N: int, out_dir: str) -> str:
    doc = Document()
    h = doc.add_heading('MACRO MARKETS ANALYSIS — Selezione Automatica', 0)
    h.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER

    doc.add_paragraph("Executive Summary: contesto ultimi 30 giorni (stream + cache).")
    doc.add_paragraph(f"Paesi: {', '.join(countries)}")
    doc.add_paragraph(f"Selezione notizie: ultime {days_N*24} ore (≈ {days_N} giorni) — ordine: Colore ↓, Rating ↓, Recency ↑")
    doc.add_paragraph(f"Data Report: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    doc.add_paragraph("Fonte: TradingEconomics (Stream) + cache locale")
    doc.add_paragraph("_"*60)

    doc.add_heading('EXECUTIVE SUMMARY', level=1)
    doc.add_paragraph(es_text or "(non disponibile)")
    doc.add_paragraph("_"*60)

    doc.add_heading('NOTIZIE SELEZIONATE', level=1)
    by_country={}
    for it in sel:
        by_country.setdefault(it.get("country","Unknown"), []).append(it)

    for country, arr in by_country.items():
        doc.add_heading(country.upper(), level=2)
        for i, it in enumerate(arr,1):
            head = (it.get("title_it") or it.get("title","")).strip()
            p = doc.add_paragraph(); p.add_run(f"{i}. {head}").bold=True

            meta=[]
            if it.get("time"): meta.append(f"⏰ {it['time']}")
            if it.get("category_mapped"): meta.append(f"🏷 {it['category_mapped']}")
            if it.get("score") is not None: meta.append(f"⭐ {it['score']}")
            if it.get("importance") is not None: meta.append(f"🎯 {it['importance']}")
            if meta: doc.add_paragraph("   " + "  ·  ".join(meta))

            doc.add_paragraph(trim_words(it.get("summary_it") or it.get("description",""), 120))
            doc.add_paragraph("")

    outp = Path(out_dir); outp.mkdir(parents=True, exist_ok=True)
    path = outp / filename
    doc.save(path)
    logging.info("Report salvato: %s", path)
    return str(path)

# =============== Input & Main ===============
def prompt_days() -> int:
    while True:
        s = input("Giorni da mostrare nella SELEZIONE (1-30): ").strip()
        if s.isdigit():
            d = int(s)
            if 1 <= d <= 30: return d
        print("Valore non valido. Inserisci 1…30.")

def prompt_countries() -> List[str]:
    mapping = {"1":"United States","2":"Euro Area","3":"Germany","4":"United Kingdom",
               "5":"Italy","6":"France","7":"China","8":"Japan","9":"Spain","10":"Netherlands","11":"European Union"}
    print("\nSeleziona i Paesi (invio = United States, Euro Area):")
    print("  1) United States   2) Euro Area   3) Germany   4) United Kingdom   5) Italy")
    print("  6) France          7) China       8) Japan     9) Spain            10) Netherlands")
    print("  11) European Union")
    s = input("Codici separati da virgola (es. 1,2,4) oppure invio: ").strip()
    if not s: return ["United States","Euro Area"]
    chosen=[]
    for tok in s.split(","):
        k = tok.strip()
        if k in mapping and mapping[k] not in chosen:
            chosen.append(mapping[k])
    if not chosen: return ["United States","Euro Area"]
    return ["Euro Area" if x=="European Union" else x for x in chosen]

def ensure_api_key(cfg: Config) -> str:
    if cfg.ANTHROPIC_API_KEY: return cfg.ANTHROPIC_API_KEY
    print("\n🔑 Inserisci la tua API Key Anthropic (salvata in .env):")
    k = input("API Key: ").strip()
    if not k: raise RuntimeError("API Key necessaria per ES/riassunti.")
    try:
        with open(BASE_DIR / ".env","a",encoding="utf-8") as f:
            f.write(f"\nANTHROPIC_API_KEY={k}\n")
        os.environ["ANTHROPIC_API_KEY"]=k
    except Exception as e:
        logging.warning("Impossibile aggiornare .env: %s", e)
    return k

def main():
    setup_logging(logging.INFO)
    cfg = Config()
    print("="*96)
    print("🏦 TE Macro Agent — Fresh-First (N×24h) | Colore-first hard | ES 30gg (DB)")
    print("="*96)

    try:
        ensure_api_key(cfg)
    except Exception as e:
        print(f"\n❌ {e}"); return

    N_days = prompt_days()
    countries = prompt_countries()
    print(f"\n▶ ES: 30 giorni | SELEZIONE: ultime {N_days*24} ore (≈ {N_days} gg) | Paesi: {', '.join(countries)}")

    scraper = TEStreamScraper(cfg)

    # Scrape per aggiornare DB (fino a 30 gg per ES)
    items_stream_30d = scraper.scrape_stream(countries, horizon_days=max(cfg.CONTEXT_DAYS_ES, N_days))
    if cfg.USE_DB:
        conn = db_init(cfg.DB_PATH)
        if items_stream_30d:
            db_upsert(conn, items_stream_30d)
            db_prune(conn, cfg.PRUNE_DAYS)
        items_cache_60d = db_load(conn, countries, max_age_days=cfg.PRUNE_DAYS)
    else:
        items_cache_60d = []

    # ES (30 gg)
    es_ctx = items_cache_60d if items_cache_60d else items_stream_30d
    summarizer = MacroSummarizer(cfg.ANTHROPIC_API_KEY, cfg.MODEL, cfg.MODEL_TEMP, cfg.MAX_TOKENS)
    es_text = summarizer.executive_summary(es_ctx, cfg)

    # Selezione Fresh-first (N×24h, color-first hard)
    selection = build_selection_freshfirst(
        items_stream=items_stream_30d,
        items_cache=items_cache_60d,
        days_N=N_days,
        max_news=cfg.MAX_NEWS
    )
    if not selection:
        print("\n❌ Nessuna notizia entro la finestra richiesta.")
        return

    # Traduzioni/riassunti IT
    for it in selection:
        try: it["title_it"] = summarizer.translate_it(it.get("title",""))
        except Exception as e: logging.warning("Titolo non tradotto: %s", e); it["title_it"]=it.get("title","")
        try: it["summary_it"] = summarizer.summarize_it(it)
        except Exception as e: logging.warning("Riassunto non disponibile: %s", e); it["summary_it"]=it.get("description","")

    # Report
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"MacroAnalysis_FreshFirst_{N_days}days_{ts}.docx"
    out_path = save_report(filename, es_text, selection, countries, N_days, cfg.OUTPUT_DIR)

    # Log diagnostici
    in_24h = [x for x in (items_stream_30d+items_cache_60d) if x.get("age_days") is not None and x["age_days"] <= 1.0]
    in_Nd  = [x for x in (items_stream_30d+items_cache_60d) if x.get("age_days") is not None and x["age_days"] <= float(N_days)]
    print("\n" + "-"*72)
    print(f"Report: {out_path}")
    print(f"Card ≤24h disponibili: {len(in_24h)} | Card ≤{N_days} gg: {len(in_Nd)} | Selezionate: {len(selection)}")
    print("-"*72)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n(Interrotto dall’utente)")
    except Exception as e:
        logging.exception("ERRORE FATALE: %s", e)
        print(f"\n❌ ERRORE: {e}")
