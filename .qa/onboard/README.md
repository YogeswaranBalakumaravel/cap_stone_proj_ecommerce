# Gating onboarding agent

The gates in this repository (the test quality agent, the code review agent, the PR test gate,
Gitleaks and the gate findings commenter) were built on one Flask/pytest project. Copied into
another repository they all *run*, but some of them quietly skip, or pass without checking
anything: a branch the workflows don't list, a test command for the wrong runner, tests that
need a database, globs that match nothing. This agent finds those places and says exactly what
to change.

It's advisory. It never blocks a PR unless you ask it to (`strict`).

## How it runs

```
PR that adds or changes .qa/ or a workflow  ─or─  Actions → Gating onboarding → Run workflow
 │
 ├─ job: tests    (no credentials, runs repo code)
 │    onboard.py tests     install with install_deps.py and run tests.command exactly as the
 │                         evidence job will; classify any failure
 │
 └─ job: advise   (OIDC token allowed, never runs repo code)
      onboard.py inspect   inventory the repo and every workflow; run the deterministic checks
      onboard.py advise    agent: confirm or dispute each finding, find further skips, propose
                           exact edits (read-only; skipped with QA_PROVIDER=none)
      onboard.py report    one report: PR comment, run summary, artifact
```

The workflow has **no branch filter**: a wrong branch list is one of the things it looks for.
The checks judge the gating *as the PR has it* (`.qa/*/config.json` and the workflows in the
checkout), while the tooling itself loads from the base branch once it's there.

## What it checks

| Id | Severity | What it finds |
|---|---|---|
| OB001–OB002 | blocker | Gate workflows missing, or a workflow without the `.qa/` code it needs |
| OB010–OB013 | blocker | A gate with no `pull_request` trigger; a long-lived branch (main, master, develop, dev, staging, release/*, …) a gate doesn't cover; the code review job's branch condition out of step with its trigger |
| OB012 | info | Branches listed that the repo doesn't have |
| OB014 | warning | Public repo: fork PRs skip the agents, and a skipped required check counts as passed |
| OB020–OB023 | warning/info | The commenter waiting for workflow names that don't exist; a lint workflow without the `lint-security-report` artifact; the commenter not on the default branch; PR workflows it doesn't report |
| OB030–OB034 | blocker/warning | Installing from a `requirements.txt` that isn't there; setup-python's pip cache without a dependency file; the wrong Python version; a non-Python main language (no coverage or mutants); a monorepo |
| OB040–OB043 | blocker/warning | Source or test globs that match nothing; a pytest command on a repo without Python tests; `fail_closed` off |
| OB050–OB054 | blocker/warning | The tests didn't run, failed (missing dependency, service, env var, no tests, runner missing), timed out, or are too slow for the probe budget |
| OB060–OB063 | blocker/warning/info | Code review globs that match nothing; architecture contracts naming modules the repo lacks; framework-specific checks that won't apply; no context files |
| OB070–OB075 | warning/info | No PR template with acceptance criteria; CODEOWNERS not protecting the gates; required checks missing; no agent sign-in; no Jira |

The verdict is **Not ready** with any blocker, **Ready after changes** with warnings, else
**Ready**. The report also gives a suggested `.qa/agent/config.json` patch for the detected stack
(pytest, Jest, Vitest, npm test, Maven, Gradle, Go, .NET).

The agent adds what scripts can't see: how the project's existing CI runs its tests (services,
env vars, build steps, matrix versions, working directories), whether the globs fit the layout,
overlapping gates, and questions only the owner can answer. Its citations are checked; ones that
don't point at a real line are dropped.

## Running it

In CI: open a PR that adds the gating (it runs automatically), or **Actions → Gating onboarding →
Run workflow** (`run_tests`, `strict`). Sign-in is the test quality agent's `QA_PROVIDER`.

Locally, from the repository root:

```bash
python .qa/onboard/onboard.py all              # tests, inspect, advise, report
python .qa/onboard/onboard.py all --no-tests   # skip installing and running the tests
cat .onboard-out/report.md
```

`OB_SKIP_INSTALL=1` skips the install step, `OB_STRICT=1` exits 1 when the verdict is not ready,
and `OB_DEFAULT_BRANCH` / `OB_REPO_PRIVATE` stand in for what the workflow reads from GitHub.

## Files

| File | Purpose |
|---|---|
| `onboard.py` | The four steps, one at a time or `all` |
| `probe_tests.py` | Install and run the tests as the evidence job will, without credentials; classify failures |
| `inventory.py` | Languages, manifests, test and web frameworks, Python version, services and env vars the tests use, branches, every workflow, CODEOWNERS, PR template, required checks |
| `checks.py` | The OB checks above |
| `advise.py`, `prompts/advise.md`, `schemas/advise.schema.json` | The agent's pass |
| `report.py` | The report and the verdict |
| `config.json` | The agent's model, turns, budget and timeout |

## Limits

- Workflows are read with a small indentation-based reader, not a YAML library (the toolkit uses
  the standard library only). Unusual layouts (anchors, flow mappings spanning lines) may be
  misread; the agent double-checks.
- Required checks come from the rulesets API; classic branch protection needs admin access, so
  it's often reported as "couldn't read".
- It can't see repository variables or secrets, only what the workflow passes it
  (`QA_PROVIDER`, `JIRA_BASE_URL`).
