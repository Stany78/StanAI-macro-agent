# TE Macro Agent

## Setup
```bash
git clone <URL_DEL_REPO>
cd te-macro-agent
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -r requirements.txt
playwright install chromium
copy .env.example .env    # poi inserisci la tua ANTHROPIC_API_KEY
