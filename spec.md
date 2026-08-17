# Spec: Flagship Phones Showcase (Flask App)

## 1. Overview

A small Python/Flask web application that displays current flagship smartphones from **Apple** and **Samsung** in a clean, browsable catalog. The goal is a self-contained app that's simple enough to build in a day, but structured well enough to plug into a CI/CD pipeline later (lint, tests, AI code review gate, deploy to Render or similar).

## 2. Goals

- Show Apple flagship phones (iPhone Pro/Pro Max/Air tier) and Samsung flagship phones (Galaxy S Ultra/Plus tier) side by side.
- Support filtering by brand and sorting by price/release date.
- Show a detail page per phone (specs, price, image, release date).
- Keep data source swappable (start with a local JSON/SQLite seed, no external API dependency required).
- Be deployable to a free-tier PaaS (Render) with a GitHub Actions pipeline.

## 3. Non-Goals (v1)

- No user accounts, auth, or reviews.
- No live price-tracking or scraping of retailer sites.
- No payment/purchase flow — this is a showcase/catalog, not a store.

## 4. Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | |
| Web framework | Flask 3.x | Jinja2 templates, Blueprints for routes |
| Data storage | SQLite (via SQLAlchemy) | Easy to seed, zero external infra; swap for Postgres later if needed |
| Frontend | Server-rendered Jinja2 + minimal CSS (or Bootstrap/Tailwind CDN) | No SPA needed for v1 |
| Testing | `pytest` | Unit tests for routes and data layer |
| Lint/format | `ruff` | Fast, single tool for lint + format checks |
| Security scan | `bandit` | Static analysis for common issues |
| CI/CD | GitHub Actions | Lint → test → (optional AI review gate) → deploy |
| Hosting | Render (free tier) | Web Service from GitHub repo, `gunicorn` as WSGI server |

## 5. Data Model

### Phone

| Field | Type | Notes |
|---|---|---|
| `id` | int (PK) | |
| `brand` | str | `"Apple"` or `"Samsung"` |
| `model_name` | str | e.g. `"iPhone 17 Pro Max"`, `"Galaxy S26 Ultra"` |
| `tier` | str | e.g. `"Pro"`, `"Pro Max"`, `"Ultra"`, `"Plus"`, `"Standard"` |
| `release_date` | date | |
| `price_usd` | decimal | Starting price |
| `screen_size_in` | float | |
| `chip` | str | e.g. `"A19 Pro"`, `"Snapdragon 8 Elite Gen 5"` |
| `ram_gb` | int | |
| `storage_options_gb` | str (comma list) | e.g. `"256,512,1024"` |
| `camera_summary` | str | Short human-readable summary |
| `image_url` | str | Local static path or external URL |
| `is_current` | bool | Whether it's an actively sold current-gen flagship |

### Seed data (illustrative, as of Aug 2026)

**Apple current flagships:** iPhone 17, iPhone 17 Pro, iPhone 17 Pro Max, iPhone Air (iPhone 18 Pro / Pro Max expected fall 2026 — treat as upcoming, not seeded as current).

**Samsung current flagships:** Galaxy S26, Galaxy S26+, Galaxy S26 Ultra.

> Note: phone specs/prices change frequently — treat the seed file as a starting point to edit, not a maintained live feed.

## 6. Application Structure

```
flagship-phones/
├── app/
│   ├── __init__.py          # app factory, db init
│   ├── models.py            # SQLAlchemy Phone model
│   ├── routes.py            # Blueprint: index, brand filter, detail page
│   ├── seed_data.py         # seed script/dict of phones
│   ├── templates/
│   │   ├── base.html
│   │   ├── index.html       # catalog grid, filter by brand
│   │   └── detail.html      # single phone spec page
│   └── static/
│       ├── css/style.css
│       └── images/          # phone images
├── tests/
│   ├── test_routes.py
│   └── test_models.py
├── .github/
│   └── workflows/
│       └── ci.yml
├── requirements.txt
├── wsgi.py                  # entrypoint for gunicorn
├── config.py
└── README.md
```

## 7. Routes

| Route | Method | Description |
|---|---|---|
| `/` | GET | Catalog grid of all phones. Query params: `?brand=Apple\|Samsung`, `?sort=price\|release_date` |
| `/phone/<int:id>` | GET | Detail page for one phone |
| `/api/phones` | GET | JSON list endpoint (optional, for future frontend or testing) |
| `/healthz` | GET | Simple health check for deploy platform |

## 8. UI/Pages

- **Catalog page (`/`)**: grid of phone cards (image, name, tier badge, price). Toggle/tabs for "All / Apple / Samsung". Sort dropdown.
- **Detail page (`/phone/<id>`)**: larger image, full spec table, back link.
- Keep styling minimal — a single `style.css` with a card grid layout is enough for v1; no build step needed (avoids adding Node tooling unless you want it).

## 9. Testing Plan

- `test_models.py`: Phone model creation, default values.
- `test_routes.py`:
  - `/` returns 200 and contains both brands.
  - `/?brand=Apple` only returns Apple phones.
  - `/phone/<id>` returns 200 for valid id, 404 for invalid id.
  - `/healthz` returns 200.

## 10. CI/CD Pipeline (GitHub Actions)

Stages, run on push/PR to `main`:

1. **Setup** — checkout, set up Python, cache pip deps.
2. **Lint** — `ruff check .`
3. **Security scan** — `bandit -r app/`
4. **Test** — `pytest --maxfail=1`
5. **(Optional) AI review gate** — call an LLM with the diff, require structured JSON output (`{"approved": bool, "issues": [...]}`) before allowing merge/deploy. Fail the job if `approved` is `false`.
6. **Deploy** — on merge to `main`, trigger Render deploy (via Render's GitHub auto-deploy, or a deploy hook URL called from the workflow).

## 11. Deployment (Render free tier)

- Render "Web Service" connected to the GitHub repo.
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn wsgi:app`
- Environment: `PYTHON_VERSION` pinned, `FLASK_ENV=production`.
- SQLite file either seeded at build time or the app falls back to an in-memory/on-disk seed on first boot (Render's free-tier disk isn't guaranteed persistent across deploys, so don't rely on writes surviving restarts — reseed from `seed_data.py` on startup).

## 12. Open Questions / Decisions Needed

- Do you want phone images bundled as static assets, or linked to external URLs (simpler, but dependent on external hosting)?
- Should the AI review gate block deploys, or just annotate PRs (non-blocking) for v1?
- Any interest in a `/api/phones` JSON endpoint for a future JS-driven frontend, or is server-rendered HTML enough?

## 13. Suggested Build Order

1. Scaffold Flask app factory + SQLAlchemy model + seed script.
2. Build `/` and `/phone/<id>` routes with templates, no styling.
3. Add CSS/layout.
4. Add tests, wire up `ruff`/`bandit`.
5. Add GitHub Actions workflow (lint + test only first).
6. Deploy to Render manually once, confirm it works.
7. Add AI review gate to the pipeline last, once the base pipeline is green.
