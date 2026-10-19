# Gate findings commenter

Keeps **one** comment on each pull request listing what every gate found for the PR's head commit,
so the author can see every problem in one place instead of opening each run.

| Gate | Workflow(s) | Where its findings come from |
|---|---|---|
| Lint and security scan | `CI`, `CI - Dev`, `CI - UAT` | `lint-security-report` artifact: `ruff.json`, `bandit.json` |
| PR tests | `PR Test Gate` | `pytest-report` artifact: JUnit XML |
| Secret scan | `Gitleaks Secret Scan` | `gitleaks*` artifact: SARIF (rule and location only; never the secret) |
| Test quality agent | `Test quality agent` | `test-quality-evidence` artifact: `metrics.json` |
| Code review agent | `Code review agent` | `code-review-evidence` artifact: `findings.json` |

## How it runs

`.github/workflows/gate-findings-commenter.yml` runs on `workflow_run` each time one of those
workflows completes for a PR. `commenter.py` then:

1. finds the PR (from the event, or for a fork PR by matching its head commit);
2. lists the workflow runs for that commit and takes each gate's latest PR run for its result;
3. reads the findings files out of that run's artifact (falling back to an earlier run for the same
   commit when the latest kept none, e.g. a code review re-run on a review event);
4. renders them with `findings.py` and edits the comment marked `<!-- gate-findings-commenter -->`,
   or creates it. It only ever edits a comment `github-actions[bot]` wrote.

Each finding shows its severity, a link to the file and line at that commit, what's wrong and how
to fix it. 🚫 marks findings that fail their gate:

- ruff, bandit, pytest and Gitleaks: every finding, since any one fails the step.
- Code review agent: status `BLOCK` or `NEEDS_ATTESTATION`.
- Test quality agent: findings in a dimension that is `FAIL` and blocking, in blocking mode.

Counts are never a guessed 0. A gate that is still running, kept no findings file, or wrote one
that doesn't parse shows `?` with a note saying why. A gate whose workflow didn't run shows `—`.
Gitleaks is the one exception: it uploads its SARIF only when it finds a leak, so a passing run with
no artifact counts as 0.

The commenter only reports. It never fails a PR; each gate enforces its own result. The agents'
own comments are unchanged and still hold their full reports.

## Safety

- `workflow_run` runs this workflow and its code from the default branch, never from the PR. So
  changes here take effect only once they reach `main`.
- Artifacts from fork PRs are produced by the PR's code, so they're data only. Zips are read in
  memory, only the named files are parsed (5 MB cap each), and nothing is extracted or executed.
- All gate text is escaped before it reaches the comment: no raw HTML, links (bare URLs included),
  table breaks or @-mentions. File links are only made for plain repo-relative paths.
- Secrets stay out: Gitleaks findings keep only the rule and location, and bandit's
  hardcoded-password checks (B105, B106, B107) get a fixed description instead of their text.
- JUnit XML with a DOCTYPE or entity is refused before parsing (no entity-expansion attacks).
- One gate's bad artifact or failed API call marks only that gate unreadable; the rest still post.
- The token has `actions: read`, `contents: read` and `pull-requests: write`, nothing else.

## Trying it locally

```bash
GITHUB_REPOSITORY=owner/repo GH_TOKEN=... GITHUB_EVENT_PATH=event.json COMMENTER_DRY_RUN=1 \
  python .qa/commenter/commenter.py
```

`event.json` needs a `workflow_run` object with `event`, `head_sha` and `pull_requests`.
`COMMENTER_DRY_RUN=1` prints the comment instead of posting it. Tests: `tests/test_qa_commenter.py`.

To cover a new gate, add an entry to `GATES` in `commenter.py`, a parser in `findings.py`, and the
workflow's name to `gate-findings-commenter.yml`. The workflow has to upload its findings as an
artifact.
