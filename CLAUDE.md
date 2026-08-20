# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Flagship Phones Showcase — a small Flask app cataloging current Apple and Samsung
flagship phones (filter by brand, sort by price/release date, per-phone detail page).
It's a capstone project whose real purpose is exercising a CI gating pipeline
(lint → security scan → tests → deploy), so keep changes consistent with that
pipeline passing cleanly. See `spec.md` for the original design spec.

## Commands

```bash
# activate venv first (Windows): .venv\Scripts\activate
pytest --maxfail=1                 # run all tests, stop at first failure
pytest tests/test_routes.py        # run one test file
pytest tests/test_routes.py::test_index_returns_200   # run one test

ruff check .                       # lint (also run in CI)
bandit -r app/ -x app/static        # security scan (also run in CI)

python wsgi.py                     # run dev server at http://127.0.0.1:5000
```

There's no separate build/format step — ruff (`select = ["E", "F", "W", "I"]`,
line-length 100, target py312) is lint-only here.

## Architecture

**App factory pattern**: `app/__init__.py:create_app()` builds the Flask app,
initializes the shared `db` (`app/extensions.py`, a bare `SQLAlchemy()` instance
kept in its own module to avoid circular imports), registers the single blueprint
from `app/routes.py`, and — inside `app_context()` — calls `db.create_all()` +
`seed_data.seed_if_empty()` on every startup, not just first run.

**Reseed-on-boot is deliberate, not incidental**: Render's free tier doesn't
guarantee disk persistence across deploys/restarts, so instead of a migration/seed
step, the app just reseeds SQLite from `app/seed_data.py` every time it boots if
the `phones` table is empty (`seed_if_empty()` no-ops otherwise). Don't "fix" this
into a one-time seed — it's the intended persistence strategy for this deployment
target.

**Config selection**: `create_app(config_object=None)` defaults to `config.Config`
(SQLite file at `instance/phones.db`) but tests pass `config.TestingConfig`
(in-memory SQLite). Because in-memory SQLite is per-connection, `TestingConfig`
pins `poolclass=StaticPool` so the same connection — and seeded data — is visible
across requests within a test. Keep this in mind if you ever touch DB config:
breaking the shared-connection pin will make seeded rows invisible to test
requests without an obvious error.

**Single blueprint, single model**: all routes live in `app/routes.py`
(`main_bp`), all data in the one `Phone` model (`app/models.py`). `_query_phones()`
in `routes.py` is the shared filter/sort logic behind both the HTML route (`/`)
and the JSON route (`/api/phones`) — extend that one function rather than
duplicating filter logic between the two.

**Routes**: `/` (catalog, `?brand=`, `?sort=`), `/phone/<id>` (detail, 404 if
missing), `/api/phones` (same filters as `/`, JSON via `Phone.to_dict()`),
`/healthz` (used by Render's health check, and by `render.yaml`/`config.py`
which both assume it exists).

**Seed data** (`app/seed_data.py`): a plain list of `dict(...)` phone records —
hand-edited, not fetched from any API, and explicitly *not* meant to track real
pricing/specs over time. When adding phones, follow the existing field shape
(`storage_options_gb` as a comma-separated string, parsed via
`Phone.storage_options_list`).

## CI/CD

`.github/workflows/ci.yml`: on every push/PR to `main`, runs
lint (ruff) → security scan (bandit) → test (pytest), then on push to `main`
only, triggers a Render deploy hook (skipped if `RENDER_DEPLOY_HOOK_URL` secret
is unset). There's a commented-out `ai-review` job stubbed in for a future
non-blocking AI PR review gate — leave it commented unless asked to wire it up.

## Notes / non-goals (v1)

No auth/accounts/reviews, no live price-tracking/scraping, no purchase flow —
this is a read-only showcase catalog, not a store. Don't add these speculatively.
