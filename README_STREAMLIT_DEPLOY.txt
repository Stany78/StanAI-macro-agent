StanAI Macro Agent — Deploy su Streamlit Cloud (senza modificare streamlit_app.py)

1) Metti questi file nella **root** del repo:
   - requirements.txt
   - packages.txt

2) In Streamlit Cloud:
   - Main file: streamlit_app.py
   - Secrets: aggiungi ANTHROPIC_API_KEY

3) Redeploy.
   - L'app installerà Chromium con `python -m playwright install --with-deps chromium`
     usando la cache ~/.cache/ms-playwright (già gestita dal tuo streamlit_app.py).

Note:
- `packages.txt` non accetta commenti: usa solo nomi di pacchetto, una riga ciascuno.
- `greenlet==3.1.1` forzerà una wheel precompilata, evitando l'errore di build.
