---
name: python-coding-standard
description: This project's Python coding standard (PEP 8 baseline enforced via ruff + bandit, plus naming/docstring/structure conventions specific to this repo). Use when writing new Python code, reviewing a diff, or when asked to check/apply/enforce coding standards or style in this codebase.
---

# Python coding standard — Flagship Phones Showcase

This repo's standard is PEP 8 as a baseline, enforced by tooling rather than
manual review, plus a small set of project-specific conventions tooling can't
check. When writing or reviewing Python code here, apply both.

## Enforced by tooling (must pass)

```bash
ruff check .                    # lint — the actual standard, see pyproject.toml
ruff check . --fix              # auto-fix what's fixable
bandit -r app/ -x app/static    # security scan
pytest --maxfail=1              # tests must stay green
```

Ruff config (`pyproject.toml`): line-length 100, target py312, rule set
`E, F, W, I, B, UP`:
- `E`/`W` — pycodestyle (PEP 8 errors/warnings)
- `F` — pyflakes (unused imports/names, undefined names)
- `I` — isort (import ordering: stdlib, then third-party, then local `.`-relative,
  each group alphabetized, one blank line between groups)
- `B` — flake8-bugbear (common footguns: mutable default args, bare `except`,
  loop-variable closures, etc.)
- `UP` — pyupgrade (modern py312 syntax — no need for `from __future__ import`,
  prefer builtin generics like `list[str]` over `typing.List[str]`)

Bandit covers what ruff doesn't: hardcoded secrets, `eval`/`exec`, unsafe
deserialization, SQL string concatenation, etc. If bandit flags something and
it's a genuine false positive, suppress with `# nosec` **and a comment saying
why** — don't silence it silently.

Never loosen these configs (removing a rule code, raising line-length, adding
a broad exclude) to make a diff pass. Fix the code instead.

## Conventions ruff won't catch (follow the existing code)

- **Module docstrings**: every module opens with a one-line `"""Summary."""`
  docstring describing its role (see `app/extensions.py`, `app/models.py`,
  `config.py`). Keep new modules consistent.
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes
  (`Phone`, `Config`, `TestingConfig`), `UPPER_SNAKE_CASE` for module-level
  constants (`VALID_BRANDS`, `VALID_SORTS`, `PHONES`). Blueprints are named
  `<area>_bp` (`main_bp`).
- **No type hints currently in `app/`** — this is a deliberate simplicity
  choice for a small Flask app, not an oversight. Don't introduce partial
  type-hinting in one function/module; if the project moves to type hints,
  it should be done as a full pass with `mypy`/`pyright` wired into CI, not
  piecemeal.
- **Flask app factory pattern**: don't create module-level `Flask(__name__)`
  instances or import-time DB access. All app/DB setup happens inside
  `create_app()` (`app/__init__.py`); extensions live in `app/extensions.py`
  to avoid circular imports — follow that split for any new extension.
- **Blueprints over app routes**: new routes go in a blueprint (extend
  `app/routes.py` or add a new blueprint module), never `@app.route` directly
  on the app instance.
- **Query/filter logic stays centralized**: shared logic like
  `_query_phones()` in `routes.py` should have one implementation reused by
  both the HTML and JSON (`/api/...`) routes — don't fork filter/sort logic
  per route.
- **Tests**: flat `test_*` functions per module (`test_<module>.py`), using
  the `app`/`client` fixtures from `tests/conftest.py` — no test classes, no
  new test dependencies (mocking libs, factories) beyond plain `pytest`.

## How to apply this standard to a change

1. Run `ruff check . --fix` and `bandit -r app/ -x app/static` before
   considering Python work done — not just at PR/CI time.
2. Diff-review new/changed code against the conventions above (docstrings,
   naming, factory pattern, centralized query logic) since ruff won't flag
   these.
3. Run `pytest --maxfail=1` and don't report work as finished if it fails.
4. If a new rule violation is a real improvement but touches unrelated code,
   fix it in the same pass rather than leaving a partially-compliant codebase
   — a rule is either enforced repo-wide or not enabled yet.
