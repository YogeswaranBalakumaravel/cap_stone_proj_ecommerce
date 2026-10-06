# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Flagship Phones Showcase — a small Flask app cataloging current Apple and Samsung
flagship phones (filter by brand, sort by price/release date, per-phone detail page),
plus a second catalog page for wireless earbuds (Apple AirPods so far).
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
step, the app just reseeds SQLite from `app/seed_data.py` every time it boots,
filling each of the `phones` and `earbuds` tables only if it's empty
(`seed_if_empty()` checks them separately and no-ops for a non-empty table). Don't "fix" this
into a one-time seed — it's the intended persistence strategy for this deployment
target.

**Config selection**: `create_app(config_object=None)` defaults to `config.Config`
(SQLite file at `instance/phones.db`) but tests pass `config.TestingConfig`
(in-memory SQLite). Because in-memory SQLite is per-connection, `TestingConfig`
pins `poolclass=StaticPool` so the same connection — and seeded data — is visible
across requests within a test. Keep this in mind if you ever touch DB config:
breaking the shared-connection pin will make seeded rows invisible to test
requests without an obvious error.

**Single blueprint, one model per catalog**: all routes live in `app/routes.py`
(`main_bp`); data lives in `Phone` and `Earbud` (`app/models.py`), kept as separate
models because phones and earbuds share few spec fields (see `Specification/tws.md`).
`_query_catalog(model, brand, sort)` in `routes.py` is the shared filter/sort logic
behind every HTML and JSON catalog route (`_query_phones()`/`_query_earbuds()` just
pick the model) — extend that one function rather than duplicating filter logic.
`base.html` points its brand tabs at whichever catalog the page passes as
`catalog_endpoint` (default: the phone catalog).

**Routes**: `/` (catalog, `?brand=`, `?sort=`), `/phone/<id>` (detail, 404 if
missing), `/api/phones` (same filters as `/`, JSON via `Phone.to_dict()`),
`/healthz` (used by Render's health check, and by `render.yaml`/`config.py`
which both assume it exists). `/compare?ids=a,b` and `/api/compare?ids=a,b` show two phones side
by side; both use `_compare_phones()` in `routes.py` (400 for bad `ids`, 404 for an
unknown phone), and `/?compare=<id>` opens the catalog with that phone ticked.
`/earbuds` (same `?brand=`/`?sort=`), `/earbuds/<id>` (404 if missing) and `/api/earbuds`
are the earbuds catalog; only Apple AirPods are seeded so far.

**Seed data** (`app/seed_data.py`): a plain list of `dict(...)` phone records —
hand-edited, not fetched from any API, and explicitly *not* meant to track real
pricing/specs over time. When adding phones, follow the existing field shape
(`storage_options_gb` as a comma-separated string, parsed via
`Phone.storage_options_list`). `EARBUDS` works the same way; for over-ear headphones
with no charging case (AirPods Max), both battery fields hold the single-charge figure.

## CI/CD

`.github/workflows/ci.yml`: on every push/PR to `main`, runs
lint (ruff) → security scan (bandit) → test (pytest), then on push to `main`
only, triggers a Render deploy hook (skipped if `RENDER_DEPLOY_HOOK_URL` secret
is unset). There's a commented-out `ai-review` job stubbed in for a future
non-blocking AI PR review gate — leave it commented unless asked to wire it up.

`.github/workflows/test-quality-agent.yml`: on PRs to `main`, `dev` and `staging`,
a headless Claude Code agent reviews the tests the PR brings (meaningful? business
scenarios covered? sunny/rainy days? edge cases? trivial assertions? validates the
change?). The code lives in `.qa/agent/` (standard-library Python only; see
`.qa/agent/README.md`). It signs in with the job's GitHub OIDC token, never an API
key or OAuth token; with the `QA_PROVIDER` repo variable unset it runs its
scripts-only checks. The workflow loads `.qa/agent/` from the PR's base branch, so
changes to it take effect only after they merge. It's the mandatory PR gate for
`main`, `dev` and `staging`: blocking by default (`QA_MODE=advisory` repo variable
to only comment), and a required status check (**Test quality agent / review**).
The earlier Mutmut and OSV-Scanner gates were removed; the agent's systematic
mutants cover mutation testing.

`.github/workflows/code-review-agent.yml`: on PRs to `main`, `dev` and `staging`, a code
review agent checks the PR's backend, frontend and test code against the ten Stream A checks
of the *Checklist for AI-Assisted Applications*. The code lives in `.qa/code-review/` (standard
library only; see `.qa/code-review/README.md`). It reuses the test quality agent's OIDC sign-in
from `.qa/agent/` and the same `QA_PROVIDER`; with that unset, its deterministic checks still
run and can still block. It loads from the PR's base branch too. Only lines the PR adds can block;
judgement calls wait for a reviewer (not the author) to approve with `attest: <check>`.
`CODE_REVIEW_MODE=advisory` makes it comment-only. Its job is `code-review`, not `review`, so it
doesn't share a required-check name with the test quality agent.

## Notes / non-goals (v1)

No auth/accounts/reviews, no live price-tracking/scraping, no purchase flow —
this is a read-only showcase catalog, not a store. Don't add these speculatively.
