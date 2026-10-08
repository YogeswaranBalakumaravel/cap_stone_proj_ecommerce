# Test quality agent

A headless Claude Code agent that runs in your CI pipeline on every pull request. It reviews the unit and integration tests the PR brings and answers six questions:

1. Are the unit and integration tests meaningful?
2. Are important business scenarios covered?
3. Are sunny-day and rainy-day scenarios covered?
4. Have edge cases been considered?
5. Are the tests merely checking trivial assertions?
6. Do the tests validate the actual change being introduced?

No third-party QA libraries are used. The scripts use only the Python standard library and git. The agent is the Claude Code CLI. The only command it runs against your code is your project's own test command, the one your developers already use.

**No API keys.** By default the agent signs in with the CI job's own short-lived GitHub OIDC identity, through Anthropic workload identity federation or your cloud account (Amazon Bedrock, Google Vertex AI, Microsoft Foundry), and nothing long-lived is stored. The one exception is `QA_PROVIDER=oauth`, which signs in with a Claude seat's OAuth token stored as the repository secret `CLAUDE_CODE_OAUTH_TOKEN` (see [Claude seat OAuth token](#claude-seat-oauth-token-qa_provideroauth)). `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are always removed from the agent's environment, and `CLAUDE_CODE_OAUTH_TOKEN` is removed for every provider except `oauth`. With no provider configured, the pipeline runs in scripts-only mode.

## How it works

```
PR opened or updated
 │
 ├─ job: plan       (OIDC token allowed, never runs PR code)
 │    1. collect_context.py     script   PR text, acceptance criteria, changed files, diff, assertion scan
 │    2. run_agent.py plan      agent    finds the changed units, designs up to 10 probes     (read-only)
 │
 ├─ job: evidence   (no credentials at all, runs PR code)
 │    3. run_probes.py          script   your tests: baseline, changed-line coverage, change reverted,
 │                                       the agent's probes, systematic mutants
 │
 └─ job: review     (OIDC token allowed, never runs PR code)
      4. run_agent.py review    agent    answers the six questions with file:line evidence   (read-only)
      5. gate.py                script   verifies every citation, computes metrics, pass or fail
         → PR comment, job summary, inline annotations, evidence artifact, status check
```

The three jobs hand `.qa-out/` to each other as workflow artifacts. Splitting them this way means the PR's code never runs in a job that can mint cloud or Anthropic credentials.

The agent decides **what** to test, and the scripts do the running and the counting. The agent never edits code. It proposes small, single-line edits ("probes", the same idea as mutation testing), and `run_probes.py` applies each one, runs your tests, and restores the file. Because the agent can't touch the results, it can't misreport them. The gate trusts only two things: results it measured itself, and agent claims whose `file:line` citations check out.

The evidence stage runs these test runs:

| Run | What it does | What it proves |
|---|---|---|
| Baseline | Runs your tests on the PR head | The tests pass, so the other runs mean something |
| Changed-line coverage | Runs your tests once under Python's built-in tracer (`coverage/sitecustomize.py`) and records which changed lines execute | Changed lines that **no test runs** can't be validated by any test |
| Change reverted | Puts the base-branch version of each modified source file back, keeps the PR's tests, and runs them | If they still pass, **no test pins the new behaviour** |
| Agent probes | Applies one agent-designed edit at a time (for example `>=` → `>`, or dropping a validation) and runs the tests | A probe that **survives** shows the tests would pass with that behaviour broken |
| Systematic mutants | The same for operator and constant swaps (`<` ↔ `<=`, `==` ↔ `!=`, `and` ↔ `or`, `+` ↔ `-`, `10` → `11`...) on every changed Python line, chosen by `mutants.py` rather than the agent | Same as probes, without depending on which edits the agent picked |

## How each question is answered

| Question | Measured by the scripts | Judged by the agent | Metric (starting threshold) |
|---|---|---|---|
| 1. Meaningful tests | Probe and mutant results (killed or survived); static scan for tests with no assertion | Why each survivor survived; tests that can't fail; "integration" tests that mock the boundary they claim to test | Mutation score ≥ 70% |
| 2. Business scenarios | Business-rule probes (a surviving one is high severity) | Traceability: each acceptance criterion mapped to the tests that assert its outcome | Must-have criteria covered = 100% |
| 3. Sunny and rainy days | Error-path probes | Every unhappy path in the change, and its negative tests; each test labelled positive or negative | Negative-path coverage ≥ 80% |
| 4. Edge cases | Boundary probes and constant mutants | The edge cases that apply to this code, tested or not | Edge-case coverage ≥ 80% |
| 5. Trivial assertions | Static assertion scan of the PR's tests (Python); probe survivors | Tests whose assertions couldn't catch a realistic bug, with the reason | Trivial tests ≤ 20% |
| 6. Validates the change | Changed-line coverage; change-reverted run; "source changed but no test changed" | Each changed unit mapped to tests that would fail if it were reverted | Change coverage = 100%, changed lines run ≥ 90%, and the tests must fail with the change reverted |

The static scan (`scan_tests.py`) reads the tests the PR adds or changes with Python's `ast` module. It flags tests with no assertion at all, and tests whose only assertions are weak: `is not None`, bare truthiness, `isinstance`, or "was called". It gives the gate a deterministic signal even when the agent misses something.

If the agent's lists come back empty for a behaviour change (no edge cases, no error paths), the check is marked **Concern** rather than passing silently.

The thresholds are starting points. Tune them in `config.json` once you've calibrated the agent (see [Rollout](#rollout-and-calibration)).

## Setup

1. **Copy the files** into your repository: `.qa/agent/` and `.github/workflows/test-quality-agent.yml` (plus `.github/pull_request_template.md` if you want the acceptance-criteria section). Merge them to `main` first: the workflow loads the agent from the base branch, so a PR can't weaken its own review.
2. **Choose how the agent signs in** (see [Signing in without keys](#signing-in-without-keys)): set the repository variable `QA_PROVIDER` to `anthropic`, `bedrock`, `vertex` or `foundry`, plus that provider's variables, or to `oauth` with the secret `CLAUDE_CODE_OAUTH_TOKEN`. Leave it unset, or set it to `none`, to run the scripts only.
3. **Set your test command and paths** in `config.json`: `tests.command`, `tests.targeted_command`, and the `paths` globs. The defaults suit a Python repository that uses pytest.
4. **Edit the dependency step** in the workflow so that your tests can run.
5. **Protect the gate.** Add a CODEOWNERS rule so that changes to the agent need QA approval:
   ```
   /.qa/                @your-org/qa-team
   /.github/workflows/  @your-org/qa-team
   ```
6. **Write acceptance criteria in the PR description** under an "Acceptance criteria" heading (the PR template adds one). Alternatively, set `JIRA_BASE_URL` and `JIRA_EMAIL` as repository variables and `JIRA_API_TOKEN` as a secret; the agent then reads the ticket whose key appears in the branch name or PR title.
7. **Blocking is the default.** A PR fails the check when a blocking check fails. Make **Test quality agent / review** a required status check on `main`, `dev` and `staging` so it can't be merged around. Set the repository variable `QA_MODE=advisory` to only comment.

## Signing in without keys

The agent never uses `ANTHROPIC_API_KEY`, and the providers below never use `CLAUDE_CODE_OAUTH_TOKEN`. They use the job's GitHub OIDC token, exchanged for short-lived credentials by one of these providers. Pick one with the repository variable `QA_PROVIDER`. The IDs below are **repository variables** (Settings → Secrets and variables → Actions → Variables), not secrets.

A pull request's OIDC token has the subject `repo:ORG/REPO:pull_request`. Every trust rule below matches exactly that, so no other repository or event can use it.

### Anthropic: workload identity federation (`QA_PROVIDER=anthropic`)

1. In the Claude Console, go to **Settings → Workload identity → Connect workload** and choose **GitHub Actions**. You need the admin or owner role.
2. Issuer: `https://token.actions.githubusercontent.com`. Rule match: subject prefix `repo:ORG/REPO:pull_request`, audience `https://api.anthropic.com`, claim `repository_owner` = `ORG`. Scope: `workspace:developer`.
3. Set the variables `ANTHROPIC_FEDERATION_RULE_ID` (`fdrl_...`), `ANTHROPIC_ORGANIZATION_ID` and `ANTHROPIC_SERVICE_ACCOUNT_ID` (`svac_...`). Add `ANTHROPIC_WORKSPACE_ID` (`wrkspc_...`) only if the rule covers more than one workspace.

### Amazon Bedrock (`QA_PROVIDER=bedrock`)

1. In IAM, add the OpenID Connect provider `https://token.actions.githubusercontent.com` with the audience `sts.amazonaws.com`.
2. Create a role that trusts it:
   ```json
   {
     "Effect": "Allow",
     "Principal": { "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com" },
     "Action": "sts:AssumeRoleWithWebIdentity",
     "Condition": {
       "StringEquals": {
         "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
         "token.actions.githubusercontent.com:sub": "repo:ORG/REPO:pull_request"
       }
     }
   }
   ```
3. Allow the role `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`, `bedrock:ListInferenceProfiles` and `bedrock:GetInferenceProfile`, and enable Claude model access for the region in the Bedrock console.
4. Set the variables `AWS_ROLE_ARN` and `AWS_REGION`. Optionally, set `QA_MODEL` to a Bedrock model ID or inference profile.

### Google Vertex AI (`QA_PROVIDER=vertex`)

1. Create a workload identity pool with an OIDC provider for the issuer `https://token.actions.githubusercontent.com`. Use the attribute mapping `google.subject=assertion.sub`, `attribute.repository=assertion.repository`, and the condition `assertion.repository == "ORG/REPO"`. Leave the allowed audiences at the default.
2. Grant `roles/aiplatform.user` to the pool's principal set for the repository, either directly or through a service account the principal can impersonate (`roles/iam.workloadIdentityUser`). Enable the Claude models in Model Garden.
3. Set the variables `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION` (for example `global` or `us-east5`) and `GCP_WORKLOAD_IDENTITY_PROVIDER` (`projects/NUMBER/locations/global/workloadIdentityPools/POOL/providers/PROVIDER`). If you use a service account, also set `GCP_SERVICE_ACCOUNT`.

`run_agent.py` writes the `external_account` credential file for Google's standard sign-in chain itself. No gcloud step or third-party action is needed.

### Microsoft Foundry (`QA_PROVIDER=foundry`)

1. On an Entra ID app registration or a user-assigned managed identity, add a federated credential. Set the issuer to `https://token.actions.githubusercontent.com`, the subject to `repo:ORG/REPO:pull_request` and the audience to `api://AzureADTokenExchange`.
2. Assign that identity the **Azure AI User** role on the Foundry resource, and deploy a Claude model there.
3. Set the variables `ANTHROPIC_FOUNDRY_RESOURCE`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `QA_MODEL` (the deployment name).

### Claude seat OAuth token (`QA_PROVIDER=oauth`)

**This is not OIDC.** The token is long-lived and stored in GitHub, so it gives up the *nothing stored* property above, and usage counts against the seat's plan limits instead of API billing.

1. Sign in to claude.ai with the seat's account and run `claude setup-token` locally. Note the expiry date it prints. The token starts with `sk-ant-oat`. An API key (`sk-ant-api…`) is refused at setup with a clear error.
2. Store it under Settings → Secrets and variables → Actions → **New repository secret**, named `CLAUDE_CODE_OAUTH_TOKEN`. Then set the repository variable `QA_PROVIDER=oauth`.
3. The workflows give the secret only to the agent steps, and only while `QA_PROVIDER=oauth`: `plan` and `review` here, and the three agent passes of the code review agent. The `evidence` job, which runs PR code, never sees it. GitHub masks it in logs, and `run_agent.py` also blanks it out of the agent's output before that becomes the PR comment or the evidence artifact.
4. When the seat hits its usage limit or the token expires, the agent pass fails and the gate falls back to the scripts' evidence, just as when any provider is unavailable. Check the run log, because the PR comment won't say why.
5. To stop, set `QA_PROVIDER` back to `none` (or to an OIDC provider), delete the secret, and revoke the token in the account's claude.ai settings.

### Scripts only (`QA_PROVIDER=none`, the default)

The agent steps are skipped. The gate still runs coverage, the change-reverted check, systematic mutants and the assertion scan, and it can still block on them. Checks 2 to 4 show "agent off".

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `mode` | `advisory` (comment only) or `blocking`. The repository variable `QA_MODE` overrides it. |
| `fail_closed` | In blocking mode, fail the job when the run is incomplete (agent unavailable, tests failing on the PR head). The default is `false`. |
| `paths.source_globs`, `test_globs`, `exclude_globs` | Which changed files count as source or tests. `*` also matches `/`. Tests are never probed. |
| `tests.command` | Your full test command. Keep `-x` (stop at the first failure) so that probe runs stay fast. |
| `tests.targeted_command` | Optional faster command. `{tests}` is replaced with the test files the agent suggests for a probe. If a probe survives the targeted run, the full suite confirms it. |
| `tests.timeout_seconds` | Timeout for each test run. A probe that makes the tests hang counts as caught. |
| `tests.inconclusive_exit_codes` | Exit codes that mean "couldn't run" rather than "a test failed". For pytest, 4 is a usage error and 5 means no tests were collected. |
| `coverage.enabled`, `exclude_pragma` | Changed-line coverage on or off, and the comment that excludes a line (default `pragma: no cover`). Costs one extra test run. |
| `probes.max_probes`, `time_budget_seconds` | Limits for each PR. Each probe or mutant costs one test run; the budget covers both. |
| `probes.systematic`, `systematic_max` | Systematic mutants on or off, and how many per PR (default 15, one per changed line first). Lines with `pragma: no mutate` are skipped. |
| `probes.line_tolerance` | How far the script searches when the agent's line number is slightly off. |
| `probes.min_valid_for_score` | The minimum number of valid probes before a mutation score is reported. |
| `requirements.*` | Where acceptance criteria come from: the PR description heading, `files` (for example `docs/acceptance/*.md`), or Jira. Set `require: true` to fail check 2 when no criteria are given. |
| `agent.provider` | How the agent signs in: `anthropic`, `bedrock`, `vertex`, `foundry`, `oauth` or `none` (default). The repository variable `QA_PROVIDER` overrides it. |
| `agent.model`, `max_turns`, `max_budget_usd`, `timeout_seconds` | The model (or the `QA_MODEL` variable), and cost, turn and time limits for each pass. |
| `thresholds.*` | The pass marks for each metric. |
| `blocking_dimensions` | Which checks can fail the job in blocking mode. The default is `business_scenarios` and `change_validation`. |
| `block_on_severity` | Which verified finding severities fail a check. The default is `high`. |

For other stacks, change the globs and the commands. Some examples:

- Jest: `"command": "npx jest --bail"`, `"source_globs": ["src/*"]`, `"test_globs": ["*.test.*", "*.spec.*", "__tests__/*", "*/__tests__/*"]`
- Maven: `"command": "mvn -q test"`, `"source_globs": ["src/main/*"]`, `"test_globs": ["src/test/*"]`

## Metrics and how they're calculated

Every agent claim is verified before it counts. A citation is verified when the file exists, the line is in range, and, for tests, the test name appears in a test file. Claims that fail this check are dropped, and the report lists them.

| Metric | Formula |
|---|---|
| Mutation score | (killed + timed out) ÷ (killed + timed out + survived), over the agent's probes and the systematic mutants together. The report also splits it by origin. If the agent says a survivor may be equivalent, it still counts. The report shows the agent's reason for a person to confirm, and the finding drops to medium so it can't block on its own. |
| Criteria coverage | must-have criteria with a verified test ÷ must-have criteria |
| Negative-path coverage | error paths with a verified negative test ÷ error paths in the change |
| Edge-case coverage | applicable edge cases with a verified test ÷ applicable edge cases |
| Trivial-test ratio | tests flagged trivial, by the agent or the static scan ÷ tests classified (each test counted once) |
| Change coverage | behaviour-changing units with a verified test ÷ behaviour-changing units |
| Changed-line coverage | changed executable lines the tests ran ÷ changed executable lines (Python) |
| Change-reverted check | pass when the PR's tests fail against the base-branch source |
| Citation validity | verified citations ÷ all citations (a measure of agent quality) |

A check **fails** only on evidence: a missed threshold, a surviving probe on a business rule, the change-reverted run passing, or a verified high-severity finding. The agent's own opinion can raise a concern, but it can't fail a check on its own. It also can't clear evidence away: a disputed probe still counts.

The PR comment shows the six checks against their thresholds, a **Gate reasons** list (what blocks, or would block, and why), the findings with a **suggested test** for each (given / when / then), any **limitations** the agent reports and its **confidence**, plus collapsible traceability and evidence tables.

`metrics.json` in the evidence artifact holds every number for each PR. Collect these files to see trends.

## Rollout and calibration

1. **Calibrate in advisory mode if needed.** This repo ships in blocking mode. To tune thresholds without blocking PRs, set `QA_MODE=advisory` for a few weeks.
2. **Build a golden set** of 15 to 20 past PRs that your senior SDETs have already judged. Include some with deliberately weak tests: missing boundaries, a trivial assertion, an untested error path. Run the agent on the golden set after every change to a prompt or threshold.
3. **Measure the agent**:
   - Agreement rate = golden PRs where the agent's verdict matches the SDETs' ÷ golden PRs.
   - High-severity false-positive rate = high findings the SDETs reject ÷ all high findings.
   - Miss rate = planted weaknesses the agent didn't flag ÷ planted weaknesses.
   - Citation validity comes from `metrics.json`.
4. **Stay in blocking** (or switch back from advisory) once high-severity false positives are rare, for example under 10% with at least 85% agreement. Keep the `review` job (**Test quality agent / review**) a required status check in branch protection. Keep only `business_scenarios` and `change_validation` in `blocking_dimensions` at first; the other checks stay advisory.

## Full-project audit

To run the whole project through the gate, not just one PR's changes, open **Actions → Test quality agent → Run workflow** and pick a branch (`main`, `dev` or `staging`). The run treats every tracked file as new, runs changed-line coverage over all of it, tries up to 60 systematic mutants, and scans every test. The report goes to the run's summary page instead of a PR comment. The "change reverted" check doesn't apply, because there's no earlier version to revert to.

Locally, set `QA_FULL_AUDIT=1` (and optionally `QA_SYSTEMATIC_MAX=60`) and run the five scripts as below.

To run the agent on a past PR, run this from the repository root:

```bash
git checkout <pr-head-sha>
export QA_BASE_REF=<base-sha> QA_HOME=.qa/agent
export QA_PROVIDER=bedrock AWS_REGION=us-east-1 AWS_PROFILE=qa     # or vertex / foundry / anthropic, see below
python3 .qa/agent/collect_context.py
python3 .qa/agent/run_agent.py plan
python3 .qa/agent/run_probes.py
python3 .qa/agent/run_agent.py review
python3 .qa/agent/gate.py        # report in .qa-out/report.md, numbers in .qa-out/metrics.json
```

Outside GitHub Actions no OIDC token is fetched. Each provider uses its normal local sign-in instead: an AWS profile or SSO for Bedrock, `gcloud auth application-default login` for Vertex AI, `az login` for Foundry, or an Anthropic WIF profile (`ANTHROPIC_PROFILE`) or your own `ANTHROPIC_IDENTITY_TOKEN_FILE` for Anthropic.

## Security and data

- **The agent is read-only.** It has Read, Grep and Glob, plus `git diff`, `git show` and `git log` and nothing else: no editing, no other shell commands, no web access. Repository settings, hooks and MCP servers are ignored (`--setting-sources user`, `--strict-mcp-config`), so files in a PR can't change what the agent is allowed to do.
- **Probes are checked before they run.** Each probe must be a single-line edit to a changed source file, and it can't introduce new identifiers, calls or imports. Tests are never edited.
- **No stored credentials.** The agent jobs request a GitHub OIDC token (valid about five minutes), write it to a private temporary file, and replace it every two minutes while the agent runs, because Anthropic accepts each token only once. The file is deleted afterwards. API keys and OAuth tokens are stripped from the agent's environment.
- **Credentials never meet PR code.** Only the `plan` and `review` jobs have `id-token: write`. The `evidence` job, which runs the PR's code, has no OIDC permission and no secrets, and the probe runner also strips known credential variables from the test environment.
- **The workflow runs only on same-repository PRs.** Fork PRs are skipped. Scope every trust rule to `repo:ORG/REPO:pull_request` so that no other repository or event can use it.
- **The review can't be softened by the PR.** The prompts, thresholds and scripts load from the base branch.
- **Your code is sent to the model.** The diff and the files the agent reads go to the model. Mask any realistic customer data in test fixtures. The evidence artifact (diff, logs) is kept for 30 days.
- **PR text is treated as data.** Prompt injection in a PR can at most skew the agent's judgement. It can't change measured results, and unverifiable claims are dropped.

## Files

| File | Stage | Purpose |
|---|---|---|
| `collect_context.py` | 1 | Gathers the PR text, acceptance criteria (PR description, requirement files, optional Jira), changed files and line ranges, and the diff. Runs the static scan. |
| `scan_tests.py` | 1 | Static assertion scan of the PR's Python tests. |
| `run_agent.py` | 2, 4 | Signs in through the chosen provider with a GitHub OIDC token, then runs one headless Claude Code pass (`plan` or `review`) with read-only tools and a JSON schema. |
| `prompts/plan.md`, `prompts/review.md` | 2, 4 | The agent's instructions for each pass: the rubric for the six questions. |
| `schemas/*.schema.json` | 2, 4 | The JSON each pass must return. |
| `run_probes.py` | 3 | Baseline, changed-line coverage, change-reverted, probe and mutant runs with your test command. Restores every file, including on timeout or cancel. |
| `coverage/sitecustomize.py` | 3 | Line tracer loaded into the test process for the coverage run (`sys.monitoring` on Python 3.12+, `sys.settrace` before). |
| `mutants.py` | 3 | Picks the systematic operator and constant mutants for the changed Python lines. |
| `gate.py` | 5 | Verifies citations, computes the metrics, decides the six checks, and writes the report, `metrics.json` and the exit code. |
| `common.py` | all | Shared helpers: config, git, citation checks, parsing the agent's output. |
| `config.json` | all | Mode, paths, test commands, limits, thresholds. |

## Limitations

- Each probe or mutant is a test run, so 25 of them on a slow suite take time. Use `targeted_command`, lower `max_probes`, or move the probe step to a nightly job for large repositories.
- Checks 2 to 5 rely on the agent's judgement. They are only as good as your acceptance criteria and your calibration.
- Changed-line coverage and systematic mutants are Python only. For other languages, the agent's probes and the change-reverted run still apply.
- The coverage run puts its own `sitecustomize.py` first on `PYTHONPATH`. If your project ships its own `sitecustomize.py`, that one isn't loaded during the coverage run, and processes started with `python -I` or `-S` report no coverage.
- The syntax check and bytecode clean-up for probes apply to `.py` files only. Other languages work through your test command, which recompiles the code.
- The runner scripts need a Linux or macOS runner.
- In scripts-only mode (`QA_PROVIDER=none`), checks 2 to 4 have no data: acceptance criteria, error paths and edge cases need the agent.
