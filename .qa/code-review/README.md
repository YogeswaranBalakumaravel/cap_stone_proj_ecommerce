# Code review agent

A headless review of every pull request — backend, frontend and tests — against the ten Stream A
checks of the organisation's
[Checklist for AI-Assisted Applications](https://celestialsys.atlassian.net/wiki/spaces/DA/pages/3846176779).
It sits next to the test quality agent (`.qa/agent/`) and shares its sign-in.

Ground rules, unchanged: Python standard library only, no API keys (keyless OIDC via `QA_PROVIDER`,
or `QA_PROVIDER=oauth` with the `CLAUDE_CODE_OAUTH_TOKEN` secret, see `.qa/agent/README.md`), loaded from the base branch so a PR can't weaken its own review, and blocking
on `main`, `dev` and `staging`.

## How it reviews

The agent doesn't start from the diff. It first builds a picture of the system, then of the ask,
then of the change, and only then judges it.

```
1. collect.py                 script   The ask: PR description, acceptance criteria (with ids), Jira (v3 / ADF).
                                       The existing code: routes and endpoints, templates and who renders them,
                                       url_for references, every symbol with its signature, element ids, imports.
                                       The change: files by area (backend / frontend / test), added lines.
                                       The ten deterministic checks.
2a. run_passes.py understand  agent    Cited map: how the relevant part works today, the conventions,
                                       the existing helpers the change should reuse, what each changed unit
                                       does and which criterion it serves, and the unchanged code it affects.
2b. run_passes.py oracle      agent    Separate context, no tools, run outside the repository: from the
                                       criteria and the public signatures only, the behaviours a correct
                                       implementation must show (positive, negative, edge).
2c. run_passes.py review      agent    The ten checks over the new code and the impacted existing code,
                                       using 2a, 2b and the script findings.
3. gate.py                    script   Verifies every citation, applies attestations, decides, reports.
```

The PR's code is never executed: files are read as text and Python is parsed with `ast`.

## The ten checks

| # | Check (row) | What the script proves | What the agent adds |
|---|---|---|---|
| 1 | **Dependency hygiene** (BLOCKING) | New dependencies are pinned (DP001), exist on PyPI / npm at that version (DP002), have no OSV advisories (DP003); typo-adjacent names (DP004), declared-but-never-imported (DP005), imported-but-undeclared or transitive-only (DP006) | Whether each dependency was needed at all; hallucinated *APIs* in real packages |
| 2 | **Plausible-but-wrong logic** (BLOCKING, attestation) | Broken references the PR introduces or causes: `url_for` to a missing or renamed endpoint, missing templates (PL001–2); `x[len(x)]`, `is` with literals, self-comparison, `len() >= 0` (hard); naive datetimes, float `==`, `lower()` vs `casefold()`, identical branches, unknown element ids, loose `==` in JS (signals) | Hand traces of 2–3 inputs per non-trivial function; a `high` finding without a trace showing expected ≠ actual is downgraded |
| 3 | **Requirement coverage** (BLOCKING) | `NotImplementedError`, stub bodies, "not implemented" throws (hard); TODO / placeholder markers, lorem ipsum; no criteria at all (RQ005) | Each criterion mapped to the code that implements it and the tests that check it; missing or partial ones need attestation |
| 4 | **Error handling** (STANDARD) | Swallowed or log-and-ignore exceptions, exception text returned to clients, network calls without timeout, `return` in `finally`, `commit()` without `rollback()`, `fetch()` without error handling | One failure path traced end to end per changed I/O operation |
| 5 | **Scope creep** (ADVISORY, attestation) | New endpoints without the `new-endpoint` label, new env/config switches, files outside a declared `## Scope`, a diff-size budget, new code nothing calls, one-implementation abstractions | Every changed file reverse-walked against the criteria (`scope_map`) |
| 6 | **Duplication and drift** (STANDARD) | Functions structurally identical to existing ones (renamed variables don't hide it), copied blocks in any language, layer contracts (`architecture.contracts`), same-name reimplementations | Comparison with the conventions in `CLAUDE.md` / `AGENTS.md` and the existing helpers from pass 2a |
| 7 | **LLM-typical anti-patterns** (BLOCKING) | String-built SQL / shell, hard-coded secrets and credential-shaped literals (redacted in every output), `verify=False` and friends, MD5/SHA-1 and `random` for secrets, debug on, unsafe deserialisation, world-writable files, CORS `*` with credentials, disabled cookie/CSRF protections; frontend: autoescape off, `eval`, `javascript:` URLs, tokens in `localStorage`, missing SRI and CSRF | Missing input validation, verbose errors, over-broad permissions |
| 8 | **Injection surfaces** (BLOCKING) | Every interpreter sink the PR adds (SQL, shell, eval, templates, regex, deserialisation, paths, redirects, outbound URLs, XML, DOM, `\|safe`), plus intra-function taint from route parameters and `request.*`: tainted sinks are hard failures (INJ001), untraceable dynamic sinks need a verdict (INJ002) | A `tainted` / `safe` / `unclear` verdict for each INJ002, citing the source line (the gate checks it reads untrusted input); sinks across functions |
| 9 | **Assertions that can never fail** (BLOCKING) | Constant and tautological asserts, assertion-free and empty tests, tests that only assert on their own mocks, try/except that swallows the failure (hard); `is not None` as the only check, unconditional skips | Assertions too weak to catch a realistic wrong result |
| 10 | **Tests assert requirements, not the implementation** (BLOCKING, attestation) | Expected values computed by the code under test, tests that re-implement the algorithm, private imports, patching the unit's own internals, changed behaviour no test touches | The separate-context oracle compared with the PR's tests: each behaviour `asserted`, `asserted_differently` (a test written from the code) or `missing` |

The mutation spot-check that the checklist names for row 9 is the test quality agent's job
(`.qa/agent`, probes and systematic mutants). Run both.

## How the gate decides

| Status | When | Effect |
|---|---|---|
| ❌ **Block** | BLOCKING row, `blocker`/`high`, on a line the PR adds or an existing line it breaks, found by a *definitive* script rule | Fails the check. Fix it, or suppress it on the line with a reason |
| 🖊️ **Needs attestation** | BLOCKING row, `blocker`/`high`, in scope, but a judgement: a script signal or a verified agent finding | Fails the check until a named human attests |
| ⚠️ **Must review** | STANDARD row, `high` or worse | Shown first; doesn't block |
| ℹ️ **Report** | ADVISORY rows, lower severities, low-confidence or out-of-scope claims | Listed |

**The agent can never fail the build on its own.** A claim without a valid citation is dropped (the
quoted evidence must really be on the cited line). A verified claim waits for a human. Corroboration
(a taint source that really reads input, an existing symbol that really exists, an oracle behaviour
that really exists) is shown to that human; it doesn't make the claim definitive. That matches the
checklist, which says rows 2, 5 and 10 need a named human attestation.

### Attesting

A reviewer with write access, who isn't the author, submits an **approving** review on the current
commit containing a line like:

```
attest: logic, tests
```

Aliases: `deps`, `logic`, `requirements`, `errors`, `scope`, `duplication`, `antipatterns`,
`injection`, `assertions`, `tests`, or `all`. The review event re-runs only the gate on the stored
evidence (no agent cost). A later "changes requested" from the same reviewer withdraws it; a new
push needs a new attestation (`attestation.require_same_commit`). The report names who attested
what, which is the evidence record the checklist asks for.

To make rows 2 and 10 always require attestation, as the checklist strictly reads, set
`attestation.always_required` to `["plausible_logic", "tests_assert_requirements"]`.

### Suppressing a script finding

On the line, or the line above: `# review: ignore[EH001] best-effort ping, failure is harmless`.
Without a reason the suppression is ignored. Every suppression is listed in the report, and
CODEOWNERS on the changed files decides whether it stands.

## Setup

1. Copy `.qa/code-review/` and `.github/workflows/code-review-agent.yml`. `.qa/agent/` must be
   present (shared sign-in). Remove the old `.qa/code-review/static_checks.py` and `run_review.py`
   if you had the first version. Add `.cr-out/` to `.gitignore`.
2. Merge to `main`, `dev` and `staging` first; the workflow loads the agent from the base branch.
3. Sign-in: nothing new if the test quality agent works (same `QA_PROVIDER`, variables and trust
   rule). With `QA_PROVIDER` unset or `none`, the scripts still run and can block.
4. Make **Code review agent / code-review** (check name `code-review`) a required status check
   on the three branches.
5. CODEOWNERS: `/.qa/` and `/.github/workflows/` owned by the QA team.
6. Write acceptance criteria under an "Acceptance criteria" heading, one per line, ideally with ids
   (`AC-1: ...`). Optionally a `## Scope` section listing path globs. Jira works as in the test
   agent (`JIRA_BASE_URL`, `JIRA_EMAIL`, secret `JIRA_API_TOKEN`; v3 API, ADF parsed).
7. Network: the dependency check calls `pypi.org`, `registry.npmjs.org` and `api.osv.dev`. A failed
   lookup is reported as inconclusive (DP000), never as a pass.

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `mode`, `fail_closed` | `blocking`/`advisory` (`CODE_REVIEW_MODE` overrides); fail when the review pass produced nothing |
| `checks.*.row` | BLOCKING / STANDARD / ADVISORY per check, as on the checklist. Changing a row changes what blocks |
| `attestation.*` | Keyword, approval, same-commit and write-permission requirements, and `always_required` |
| `paths.*` | Exclusions, then test, frontend and backend globs; `template_dirs` |
| `requirements.*` | Criteria heading, `## Scope` heading, Jira key pattern and AC field, requirement files |
| `dependencies.*` | Registry/OSV switches, packages fine to declare without importing, dist → import names, known transitive imports |
| `architecture.contracts` | Layer rules: `source` module must not import any of `forbidden` |
| `duplication.*` | Block size, function similarity threshold, minimum function length |
| `scope.*` | Diff-size budget, label that marks an intended new endpoint |
| `context_files` | What pass 2a reads first for architecture and conventions |
| `agent.*` | Model, turns and budget per pass, timeout, diff size |
| `review.*` | Citation tolerance, report and annotation caps |

## Files

| File | Purpose |
|---|---|
| `collect.py` | Stage 1: the ask, the change, runs the checks, applies suppressions, writes `context.json`, `index.json`, `oracle_input.json`, `diff.patch` |
| `repo_index.py` | Map of the existing code, built with `ast` and regexes |
| `checks/*.py` | One module per checklist row; `_ast.py` holds shared helpers |
| `run_passes.py` | Stage 2: the three agent passes; imports the keyless sign-in from `.qa/agent/run_agent.py` |
| `prompts/`, `schemas/` | Instructions and output schema for each pass |
| `attest.py` | Reads attestations from approving reviews |
| `gate.py` | Stage 3: verification, decisions, report, annotations, `metrics.json`, exit code |

## Running locally

```bash
export CR_BASE_REF=origin/main PR_AUTHOR=me PR_BODY="$(cat pr.md)"
export QA_PROVIDER=none        # or bedrock / vertex / foundry / anthropic with local sign-in
python3 .qa/code-review/collect.py
for p in understand oracle review; do python3 .qa/code-review/run_passes.py $p; done
python3 .qa/code-review/gate.py          # .cr-out/report.md, findings.json, metrics.json
```

`CR_FULL_AUDIT=1` reviews every tracked file as new; audits are always report-only.
`CR_ATTESTATIONS_FILE` (a JSON list of `{"user", "checks", "commit"}`) simulates attestations.

## Calibration

Run in advisory mode on 15–20 past PRs your senior reviewers have judged, including planted
defects for each row. Track per check: agreement with the human verdict, the false-positive rate
of Block and Needs-attestation findings (aim under 10% before blocking), the miss rate on planted
defects, and `citation_validity` in `metrics.json` (a drop is the first sign a prompt or model
change hurt). Prompts, checks and thresholds are behaviour: re-run the set after changing them.

## Known limits

- Taint tracking is intra-function; cross-function flows rely on the agent and an attester.
- Frontend rules are line-based; a construct split across lines can be missed.
- Duplicate detection compares against the head revision of the repository only.
- Dependency checks cover `requirements*.txt`, `pyproject.toml` and `package.json`.
- The oracle is only as good as the criteria. With none, row 10 falls back to the script signals.
