# Flagship Phones Showcase

Capstone project to play and learn CI gating. A small Flask app that
catalogs current Apple and Samsung flagship phones — filter by brand, sort
by price or release date, and view a spec detail page per phone. Data is
seeded from `app/seed_data.py` into SQLite on startup, no external API
required.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
python wsgi.py                # http://127.0.0.1:5000
```

The SQLite file lives at `instance/phones.db` and is created (and reseeded
if empty) automatically on startup.

## Routes

| Route | Description |
|---|---|
| `/` | Catalog grid. `?brand=Apple\|Samsung`, `?sort=price\|release_date` |
| `/phone/<id>` | Detail page for one phone |
| `/api/phones` | JSON list (same filters as `/`) |
| `/healthz` | Health check |

## Tests, lint, security scan

```bash
pytest --maxfail=1
ruff check .
bandit -r app/ -x app/static
```

## CI/CD

`.github/workflows/ci.yml` runs lint → security scan → tests on every push
and PR to `main`, then triggers a Render deploy hook on push to `main` (set
the `RENDER_DEPLOY_HOOK_URL` repo secret to enable; the step is skipped if
it's unset). An optional, non-blocking AI review gate stage is stubbed out
in the workflow for later.

## Deploy to Render

**Option A — `render.yaml` (recommended):** push this repo to GitHub, then
in Render choose **New → Blueprint** and point it at the repo. Render will
read `render.yaml` and create the web service automatically.

**Option B — manual Web Service:**
1. New → Web Service → connect this GitHub repo.
2. Build command: `pip install -r requirements.txt`
3. Start command: `gunicorn wsgi:app`
4. Add env vars: `PYTHON_VERSION=3.12.4`, `FLASK_ENV=production`, `SECRET_KEY=<generate>`.
5. Health check path: `/healthz`.

Render's free-tier disk isn't guaranteed to persist across deploys/restarts,
so the app reseeds its SQLite data from `app/seed_data.py` on every boot —
no manual seeding step needed after a deploy.

## Project structure

```
app/
├── __init__.py       # app factory, db init, reseed on boot
├── extensions.py      # SQLAlchemy instance
├── models.py          # Phone model
├── routes.py          # index, detail, api, healthz
├── seed_data.py        # seed dict + seed_if_empty()
├── templates/
└── static/
tests/
├── conftest.py
├── test_models.py
└── test_routes.py
.github/workflows/ci.yml
requirements.txt
wsgi.py                # gunicorn entrypoint
config.py
render.yaml
```

## Notes / non-goals (v1)

- No auth, accounts, or reviews.
- No live price-tracking or scraping — seed data is illustrative and meant
  to be hand-edited over time.
- No purchase flow — this is a showcase catalog, not a store.
