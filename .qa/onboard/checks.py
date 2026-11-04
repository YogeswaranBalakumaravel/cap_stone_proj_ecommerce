"""Judge the inventory against the gates' configuration: where would a gate skip, or pass without
checking anything? Each finding says which gate, what happens, and the exact change that fixes it.

Severities:
    blocker   a gate won't run, or will pass without checking anything (a silent skip)
    warning   a gate runs but checks less than it should, or isn't enforced
    info      worth knowing; nothing is skipped
"""

from __future__ import annotations

import fnmatch
import json
import os
import re

from ob_common import (
    GATE_FILES,
    REPO,
    TARGET_AGENT,
    TARGET_COMMENTER,
    TARGET_REVIEW,
    WORKFLOWS,
    git,
    match,
    read_json,
    read_text,
)

SEVERITY_ORDER = {"blocker": 0, "warning": 1, "info": 2}
PR_GATES = ("test-quality-agent.yml", "code-review-agent.yml", "pr-tests.yml", "gitleaks.yml")
CAUSE_FIX = {
    "missing_dependency": (
        "The install step didn't bring in everything the tests import. Declare the missing "
        "package in the project's dependency file, or set the repository variable "
        "`QA_INSTALL_COMMAND` to the command your developers use (e.g. `poetry install`)."
    ),
    "service_unavailable": (
        "The tests need a running service. Add it under `services:` on the `evidence` job in "
        "`test-quality-agent.yml` (and on `pr-tests.yml`), with non-secret settings in `env:`; "
        "or make those tests skip cleanly when the service isn't there."
    ),
    "missing_env": (
        "The tests read environment variables the CI job doesn't set. Give them non-secret "
        "test values with `env:` on the `evidence` job. Never pass secrets: that job runs the "
        "PR's code and deliberately has none."
    ),
    "no_tests": "The test command collected no tests: fix `tests.command` or where tests live.",
    "runner_missing": "The test runner isn't installed: add it to the project's dev dependencies "
    "or to `QA_INSTALL_COMMAND`.",
}


def finding(fid, severity, gate, title, what_happens, fix, evidence=None) -> dict:
    return {"id": fid, "severity": severity, "gate": gate, "title": title,
            "what_happens": what_happens, "fix": fix, "evidence": evidence or []}


def _line(path: str, pattern: str) -> int | None:
    for i, line in enumerate(read_text(REPO / path).splitlines(), 1):
        if re.search(pattern, line):
            return i
    return None


def _ev(path: str, pattern: str = "") -> list[dict]:
    if not (REPO / path).exists():
        return []
    return [{"file": path, "line": _line(path, pattern) if pattern else None}]


def _covers(patterns, branch: str) -> bool:
    return patterns is None or any(fnmatch.fnmatchcase(branch, p) for p in patterns)


def _commenter_workflows() -> list[str]:
    text = read_text(TARGET_COMMENTER / "commenter.py")
    return [n for group in re.findall(r'"workflows":\s*\[([^\]]*)\]', text)
            for n in re.findall(r'"([^"]+)"', group)]


def _version_ok(have: str, spec: str) -> bool:
    """Does Python `have` (e.g. 3.12) satisfy `spec` (>=3.10,<3.13 / 3.11 / ~=3.11)?"""
    def tup(v):
        return tuple(int(x) for x in re.findall(r"\d+", v)[:2])
    h = tup(have)
    if not h:
        return True
    for part in spec.split(","):
        m = re.match(r"\s*(>=|<=|==|~=|>|<|!=)?\s*([\d.]+)", part)
        if not m:
            continue
        op, v = m.group(1) or "==", tup(m.group(2))
        ok = {">=": h >= v, "<=": h <= v, ">": h > v, "<": h < v, "!=": h != v,
              "==": h == v, "~=": h >= v}[op]
        if not ok:
            return False
    return True


# ------------------------------------------------------------------ the checks


def installation(inv: dict) -> list[dict]:
    out = []
    names = {w["file"]: w for w in inv["workflows"]}
    present = [f for f in PR_GATES if f in names]
    if not present:
        out.append(finding(
            "OB001", "blocker", "all", "No gate workflows are installed",
            "Nothing runs on pull requests.",
            "Copy the gate workflows into `.github/workflows/`: `test-quality-agent.yml`, "
            "`code-review-agent.yml`, `pr-tests.yml`, `gitleaks.yml` and "
            "`gate-findings-commenter.yml`."))
    needs = {
        "test-quality-agent.yml": [".qa/agent"],
        "code-review-agent.yml": [".qa/code-review", ".qa/agent"],
        "gate-findings-commenter.yml": [".qa/commenter"],
    }
    for wf, dirs in needs.items():
        for d in dirs:
            if wf in names and not (REPO / d).is_dir():
                out.append(finding(
                    "OB002", "blocker", GATE_FILES.get(wf, wf),
                    f"`{wf}` is installed but `{d}/` isn't",
                    "The workflow fails on its first step"
                    + (" (the code review agent signs in through .qa/agent)."
                       if d == ".qa/agent" and wf == "code-review-agent.yml" else "."),
                    f"Copy `{d}/` from the gating toolkit.", _ev(f".github/workflows/{wf}")))
    return out



def branch_coverage(inv: dict) -> list[dict]:
    out = []
    long_lived = inv["branches"]["long_lived"]
    known = set(inv["branches"]["all"])
    for wf in inv["workflows"]:
        if wf["file"] not in PR_GATES:
            continue
        gate = GATE_FILES[wf["file"]]
        pr = wf["triggers"].get("pull_request")
        path = f".github/workflows/{wf['file']}"
        if pr is None:
            out.append(finding(
                "OB010", "blocker", gate, f"{gate} has no `pull_request` trigger",
                "It never runs on a PR.", f"Add a `pull_request:` trigger to `{path}`.",
                _ev(path, r"^on:")))
            continue
        missing = [b for b in long_lived if not _covers(pr["branches"], b)]
        if missing:
            out.append(finding(
                "OB011", "blocker", gate,
                f"PRs into {', '.join(f'`{b}`' for b in missing)} skip the {gate}",
                f"`{path}` only runs for PRs into {pr['branches']}, so PRs into "
                f"{', '.join(missing)} merge without this gate.",
                f"Add {', '.join(missing)} to `on.pull_request.branches` in `{path}`"
                + (" and to the job's `contains(fromJSON(...))` branch list."
                   if wf["file"] == "code-review-agent.yml" else "."),
                _ev(path, r"branches:")))
        stale = [b for b in (pr["branches"] or []) if known and "*" not in b and b not in known]
        if stale:
            out.append(finding(
                "OB012", "info", gate, f"{gate} lists branches this repo doesn't have: "
                + ", ".join(f"`{b}`" for b in stale),
                "Harmless, but usually a sign the list was copied from another repo.",
                f"Trim `on.pull_request.branches` in `{path}` to the branches you use.",
                _ev(path, r"branches:")))
        if wf["file"] == "code-review-agent.yml":
            text = read_text(WORKFLOWS / wf["file"])
            m = re.search(r"contains\(fromJSON\('\[([^\]]*)\]'\)", text)
            job_list = re.findall(r'"([^"]+)"', m.group(1)) if m else None
            if job_list is not None and pr["branches"] is not None and set(job_list) != set(
                pr["branches"]
            ):
                out.append(finding(
                    "OB013", "blocker", gate,
                    "The code review job's branch condition doesn't match its trigger",
                    f"The workflow triggers for {pr['branches']} but the job only runs for "
                    f"{job_list}; the rest show as skipped, and a skipped required check counts "
                    "as passed.",
                    f"Make the `contains(fromJSON('[...]'), ...)` list in `{path}` the same as "
                    "`on.pull_request.branches`.", _ev(path, r"contains\(fromJSON")))
        text = read_text(WORKFLOWS / wf["file"])
        if "head.repo.full_name == github.repository" in text and inv["private"] == "false":
            out.append(finding(
                "OB014", "warning", gate, f"Fork PRs skip the {gate}",
                "This repo is public, and the agents don't run on fork PRs (they can't get a "
                "sign-in token). GitHub treats a skipped required check as passed, so a fork "
                "PR can merge without this gate.",
                "Require maintainer approval to run workflows from forks (Settings → Actions), "
                "and have a maintainer re-push fork changes to a branch in this repo before "
                "merging.", _ev(path, r"head\.repo\.full_name")))
    return out


def commenter(inv: dict) -> list[dict]:
    out = []
    names = {w["name"] for w in inv["workflows"]}
    by_file = {w["file"]: w for w in inv["workflows"]}
    trig = by_file.get("gate-findings-commenter.yml")
    if not trig:
        return out
    path = ".github/workflows/gate-findings-commenter.yml"
    listed = (trig["triggers"].get("workflow_run") or {}).get("workflows") or []
    for n in sorted(set(listed + _commenter_workflows())):
        if n not in names:
            out.append(finding(
                "OB020", "warning", "Gate findings commenter",
                f"The commenter waits for a workflow named `{n}` that doesn't exist",
                "That gate always shows as \"didn't run\" in the PR comment.",
                f"Rename a workflow to `{n}`, or replace `{n}` with your workflow's `name:` in "
                f"`{path}` (`workflows:`) and in `.qa/commenter/commenter.py` (`GATES`).",
                _ev(path, re.escape(n)) or _ev(".qa/commenter/commenter.py", re.escape(n))))
    lint_names = next((g for g in re.findall(
        r'"workflows":\s*\[([^\]]*)\][^}]*"artifact":\s*"lint-security-report"',
        read_text(TARGET_COMMENTER / "commenter.py"), re.S)), "")
    for n in re.findall(r'"([^"]+)"', lint_names):
        wf = next((w for w in inv["workflows"] if w["name"] == n), None)
        if wf and "lint-security-report" not in wf["uploads"]:
            out.append(finding(
                "OB021", "warning", "Gate findings commenter",
                f"`{wf['file']}` doesn't upload the `lint-security-report` artifact",
                "Lint and security findings show as unknown in the PR comment.",
                "Add the \"Save lint and security findings\" and \"Upload lint and security "
                "findings\" steps (see the toolkit's ci.yml), writing your linter's and scanner's "
                "JSON to `.gate-out/`.", _ev(f".github/workflows/{wf['file']}")))
    default = inv["branches"]["default"]
    on_default = git("ls-tree", "--name-only", f"origin/{default}", "--", path).strip()
    has_default = bool(git("rev-parse", "--verify", "-q", f"origin/{default}").strip())
    if default and has_default and not on_default:
        out.append(finding(
            "OB022", "warning", "Gate findings commenter",
            f"The commenter isn't on the default branch (`{default}`) yet",
            "`workflow_run` workflows only run from the default branch, so no PR gets the "
            "combined comment until it's merged there.",
            f"Merge the gating into `{default}` first.", _ev(path)))
    not_gates = (*PR_GATES, "gate-findings-commenter.yml", "gating-onboarding.yml")
    pr_names = {w["name"] for w in inv["workflows"]
                if "pull_request" in w["triggers"] and w["file"] not in not_gates}
    unreported = sorted(pr_names - set(listed))
    if unreported:
        out.append(finding(
            "OB023", "info", "Gate findings commenter",
            "Existing PR workflows the commenter doesn't report: "
            + ", ".join(f"`{n}`" for n in unreported),
            "They still run and still block; their findings just aren't in the combined comment.",
            "If they're gates, add them to `workflows:` in the commenter workflow and to "
            "`GATES` in `.qa/commenter/commenter.py` with the artifact they upload.", _ev(path)))
    return out


def install_and_runtime(inv: dict) -> list[dict]:
    out = []
    has_req = any(p.startswith("requirements") and p.endswith(".txt") and "/" not in p
                  for p in inv["manifests"])
    has_py_meta = any(p in ("pyproject.toml", "setup.py", "setup.cfg") for p in inv["manifests"])
    for wf in inv["workflows"]:
        path = f".github/workflows/{wf['file']}"
        if wf["requirements_txt"] and not (REPO / "requirements.txt").is_file():
            out.append(finding(
                "OB030", "blocker", GATE_FILES.get(wf["file"], wf["name"]),
                f"`{wf['file']}` installs from `requirements.txt`, which this repo doesn't have",
                "The install step fails, so every later step is skipped.",
                "Replace it with `python3 .qa/agent/install_deps.py` (reads requirements*.txt, "
                "pyproject.toml, setup.py, package.json, or `QA_INSTALL_COMMAND`).",
                _ev(path, r"requirements\.txt")))
        if wf["pip_cache"] and not (has_req or has_py_meta):
            out.append(finding(
                "OB031", "blocker", GATE_FILES.get(wf["file"], wf["name"]),
                f"`{wf['file']}` uses setup-python's pip cache without a dependency file",
                "setup-python fails when it finds no requirements file to key the cache on.",
                "Remove `cache: \"pip\"`, or set `cache-dependency-path`.", _ev(path, r"cache:")))
        wanted = inv["python_version"]
        for v in wf["python_versions"] if wf["file"] in PR_GATES else []:
            bad = {src: spec for src, spec in wanted.items() if not _version_ok(v, spec)}
            if bad:
                out.append(finding(
                    "OB032", "warning", GATE_FILES.get(wf["file"], wf["name"]),
                    f"`{wf['file']}` runs Python {v}, but the project asks for "
                    + "; ".join(f"{s}: `{x}`" for s, x in bad.items()),
                    "Tests may fail or behave differently on the wrong version.",
                    f"Set `python-version` in `{path}` to a version the project supports.",
                    _ev(path, r"python-version")))
    langs = inv["languages"]
    primary = next(iter(langs), "")
    if primary and primary != "Python":
        out.append(finding(
            "OB033", "warning", "Test quality agent",
            f"The main language is {primary}; coverage and systematic mutants are Python-only",
            "Checks 1 (meaningful tests) and 6 (changed lines run) get no measured data; only the "
            "agent's probes and the change-reverted run apply. The code review agent's deep "
            "checks also parse Python only.",
            "Set `tests.command`, `paths.source_globs` and `paths.test_globs` for your stack "
            "(see the suggested config below), and expect those checks to show n/a."))
    root = {p for p in inv["manifests"] if "/" not in p}
    nested = sorted({p.split("/")[0] for p in inv["manifests"] if "/" in p})
    if nested and not root:
        out.append(finding(
            "OB034", "warning", "all",
            "This looks like a monorepo: dependency files only in " + ", ".join(nested),
            "The gates install and test from the repository root, so they find nothing to "
            "install and their test command runs in the wrong place.",
            "Set `QA_INSTALL_COMMAND` and `tests.command` to commands that work from the root "
            "(e.g. `npm --prefix web ci`, `pytest service/tests`), and narrow the globs."))
    return out


def test_config(inv: dict, probe: dict | None) -> list[dict]:
    out = []
    cfg = read_json(TARGET_AGENT / "config.json", {}) or {}
    paths, tests = cfg.get("paths") or {}, cfg.get("tests") or {}
    files = [p for p in inv["tracked"] if not match(p, paths.get("exclude_globs"))]
    is_test = [p for p in files if match(p, paths.get("test_globs"))]
    source = [p for p in files if match(p, paths.get("source_globs")) and p not in is_test]
    suggested = inv["tests"]["suggested"]
    cfg_path = ".qa/agent/config.json"
    hint = ("\n\nSuggested for this repo:\n```json\n"
            + json.dumps({k: v for k, v in suggested.items()}, indent=2) + "\n```"
            if suggested else "")
    if not source:
        out.append(finding(
            "OB040", "blocker", "Test quality agent",
            "`paths.source_globs` matches no file in this repo",
            "No change ever counts as source, so the agent never probes, mutates or measures "
            "coverage: checks 1 and 6 always show n/a and nothing can block.",
            f"Set `paths.source_globs` in `{cfg_path}` to where the code lives." + hint,
            _ev(cfg_path, r"source_globs")))
    if not is_test:
        out.append(finding(
            "OB041", "blocker", "Test quality agent",
            "`paths.test_globs` matches no file in this repo",
            "The PR's tests are treated as source, so they're probed and mutated instead of "
            "being judged, and the assertion scan sees nothing.",
            f"Set `paths.test_globs` in `{cfg_path}` to where tests live." + hint,
            _ev(cfg_path, r"test_globs")))
    command = tests.get("command", "")
    fw = inv["tests"]["frameworks"]
    if "pytest" in command and not ({"pytest", "unittest"} & set(fw)):
        out.append(finding(
            "OB042", "blocker", "Test quality agent",
            "The test command runs pytest, but this repo has no Python tests"
            + (f" (it uses {', '.join(fw)})" if fw else ""),
            "The baseline run fails or collects nothing, so the run is incomplete and every "
            "measured check is skipped.",
            f"Set `tests.command` and `tests.targeted_command` in `{cfg_path}`." + hint,
            _ev(cfg_path, r'"command"')))
    if not cfg.get("fail_closed"):
        out.append(finding(
            "OB043", "warning", "Test quality agent",
            "`fail_closed` is false: an incomplete run passes",
            "When the tests can't run (baseline fails, agent unavailable), the check goes "
            "green with \"Incomplete run\" instead of blocking.",
            f"Set `\"fail_closed\": true` in `{cfg_path}` once the probe below passes.",
            _ev(cfg_path, r"fail_closed")))
    out += _probe(inv, probe, tests, cfg)
    return out


def _probe(inv: dict, probe: dict | None, tests: dict, cfg: dict) -> list[dict]:
    out = []
    needs = inv["test_needs"]
    if not probe:
        hints = []
        if needs["required_env"]:
            hints.append("environment variables " + ", ".join(needs["required_env"][:8]))
        if needs["compose_images"] or needs["service_clients"]:
            hints.append("services " + ", ".join(needs["compose_images"]
                                                 or needs["service_clients"]))
        out.append(finding(
            "OB050", "warning" if hints else "info", "Test quality agent",
            "The tests weren't run during onboarding",
            "Whether the evidence job can run them is unknown"
            + (f"; they look like they need {' and '.join(hints)}" if hints else "") + ".",
            "Run onboarding with tests (the workflow's default, or `onboard.py tests`)."))
        return out
    inst = probe.get("install") or {}
    if inst and inst.get("exit_code"):
        out.append(finding(
            "OB051", "blocker", "Test quality agent / PR test gate",
            "Installing dependencies failed",
            "The evidence job stops before any test runs.",
            "Check the install log below; set `QA_INSTALL_COMMAND` if the project installs "
            "some other way.\n\n```\n" + inst.get("tail", "")[-1500:] + "\n```"))
    status = probe.get("status")
    if status == "passed":
        secs = probe.get("seconds") or 0
        budget = (cfg.get("probes") or {}).get("time_budget_seconds") or 1500
        wanted = ((cfg.get("probes") or {}).get("max_probes") or 10) + (
            (cfg.get("probes") or {}).get("systematic_max") or 15)
        fits = int(budget // max(secs, 1))
        if fits < wanted and not tests.get("targeted_command"):
            out.append(finding(
                "OB052", "warning", "Test quality agent",
                f"Only about {fits} of {wanted} probe and mutant runs fit the time budget",
                f"A full test run takes {secs}s and the budget is {budget}s, so the rest are "
                "skipped and the mutation score rests on fewer runs.",
                "Set `tests.targeted_command` (runs only the tests a probe needs), or raise "
                "`probes.time_budget_seconds`."))
        else:
            out.append(finding(
                "OB053", "info", "Test quality agent", f"The tests pass in CI conditions ({secs}s)",
                "No credentials, no secrets, installed by install_deps.py: the evidence job "
                "will run them the same way.", "Nothing to do."))
        return out
    causes = probe.get("causes") or []
    fixes = [CAUSE_FIX[c] for c in causes if c in CAUSE_FIX]
    if status == "timeout":
        fixes.append(f"Raise `tests.timeout_seconds` (now {probe.get('timeout_seconds')}s), or "
                     "set a faster `tests.command`.")
    if needs["compose_images"] and "service_unavailable" in causes:
        fixes.append("Services from your compose files: " + ", ".join(needs["compose_images"]))
    out.append(finding(
        "OB054", "blocker", "Test quality agent / PR test gate",
        {"failed": "The tests fail in CI conditions", "timeout": "The tests time out",
         "inconclusive": "The test command ran no tests", "no_command": "No test command is set"
         }.get(status, f"The test run ended with status {status}"),
        "The test quality agent's baseline fails, so coverage, the change-reverted run, probes "
        "and mutants are all skipped and the run is marked incomplete (which passes unless "
        "`fail_closed` is true). The PR test gate fails on every PR.",
        "\n".join(f"- {f}" for f in fixes or ["Read the output below and fix the cause."])
        + "\n\n```\n" + (probe.get("tail") or "")[-2500:] + "\n```"))
    return out


def review_config(inv: dict) -> list[dict]:
    out = []
    cfg = read_json(TARGET_REVIEW / "config.json", {}) or {}
    if not cfg:
        return out
    cfg_path = ".qa/code-review/config.json"
    paths = cfg.get("paths") or {}
    files = [p for p in inv["tracked"] if not match(p, paths.get("exclude_globs"))]
    areas = {a: sum(match(p, paths.get(f"{a}_globs")) for p in files)
             for a in ("backend", "frontend", "test")}
    if not (areas["backend"] or areas["frontend"]):
        out.append(finding(
            "OB060", "blocker", "Code review agent",
            "The code review agent's backend and frontend globs match no file",
            "Every PR looks like it changes nothing reviewable, so the agent skips its passes "
            "and the gate passes.",
            f"Set `paths.backend_globs` and `paths.frontend_globs` in `{cfg_path}`.",
            _ev(cfg_path, r"backend_globs")))
    for contract in (cfg.get("architecture") or {}).get("contracts") or []:
        mods = [contract.get("source", "")] + list(contract.get("forbidden") or [])
        gone = [m for m in mods if m and not any(
            (REPO / (m.replace(".", "/") + suffix)).exists() for suffix in (".py", "/__init__.py"))]
        if gone:
            out.append(finding(
                "OB061", "warning", "Code review agent",
                f"Architecture contract \"{contract.get('name')}\" names modules this repo "
                f"doesn't have: {', '.join(gone)}",
                "The layer check never fires, so layering drift isn't caught.",
                f"Rewrite `architecture.contracts` in `{cfg_path}` for this repo's layers, or "
                "remove them.", _ev(cfg_path, re.escape(contract.get("name", "")))))
    frameworks = inv["web_frameworks"]
    others = [f for f in frameworks if f != "flask"]
    if others:
        out.append(finding(
            "OB062", "info", "Code review agent",
            f"Web framework: {', '.join(frameworks)}",
            "The Flask/Jinja-specific checks (broken `url_for` endpoints, missing templates, "
            "route parameters as taint sources) don't apply; the generic checks still do.",
            "Nothing to configure. Lean on the agent's review for framework-specific logic."))
    if not [p for p in cfg.get("context_files") or [] if (REPO / p).is_file()]:
        out.append(finding(
            "OB063", "info", "Code review agent",
            "None of the agent's `context_files` exist",
            "The understand pass has no written conventions or architecture to compare "
            "against, so drift findings are weaker.",
            f"Add a `CLAUDE.md` or `AGENTS.md`, or list your docs in `context_files` in "
            f"`{cfg_path}`."))
    return out


def governance(inv: dict) -> list[dict]:
    out = []
    gov = inv["governance"]
    if not gov["pr_template_has_criteria"]:
        out.append(finding(
            "OB070", "warning", "Test quality agent / Code review agent",
            "No PR template with an \"Acceptance criteria\" section",
            "PRs arrive without criteria, so business-scenario coverage can't be checked "
            "(check 2 shows no data) unless the PR links a Jira ticket.",
            "Add `.github/pull_request_template.md` with `## Acceptance criteria` (`- AC-1: ...`) "
            "and `## Ticket` sections."))
    if not all(gov["codeowners_covers"].values()):
        out.append(finding(
            "OB071", "warning", "all",
            "CODEOWNERS doesn't protect the gates",
            "Anyone can change `.qa/` or the workflows; after merge, that changes how every "
            "later PR is judged.",
            "Add to CODEOWNERS:\n```\n/.qa/                @your-org/qa-team\n"
            "/.github/workflows/  @your-org/qa-team\n```"))
    gh = inv["github"]
    present = {w["file"] for w in inv["workflows"]}
    expected = [c for f, c in (("test-quality-agent.yml", "review"),
                               ("code-review-agent.yml", "code-review"),
                               ("pr-tests.yml", "pr-test-gate")) if f in present]
    if gh.get("available"):
        for b, info in gh["required_checks"].items():
            if info["errors"] and not info["checks"]:
                out.append(finding(
                    "OB072", "info", "all", f"Couldn't read the required checks on `{b}`",
                    "Reading branch protection needs admin access; rulesets are readable.",
                    f"Check Settings → Branches/Rules for `{b}` by hand: require "
                    + ", ".join(f"`{c}`" for c in expected) + "."))
                continue
            missing = [c for c in expected if c not in info["checks"]]
            if missing:
                out.append(finding(
                    "OB073", "warning", "all",
                    f"`{b}` doesn't require " + ", ".join(f"`{c}`" for c in missing),
                    "The gates run and comment, but a PR can merge while they fail.",
                    f"Add them as required status checks for `{b}` (the picker lists checks by "
                    "job name)."))
    provider = (os.environ.get("QA_PROVIDER") or "none").strip().lower()
    if provider in ("", "none"):
        out.append(finding(
            "OB074", "warning", "Test quality agent / Code review agent",
            "No agent sign-in is configured (`QA_PROVIDER` unset or `none`)",
            "Both agents run scripts only: the test quality agent's checks 2-4 show no data and "
            "only change validation can block; the code review agent skips its understand, "
            "oracle and review passes.",
            "Set the repository variable `QA_PROVIDER` (`anthropic`, `bedrock`, `vertex`, "
            "`foundry` or `oauth`) and its settings; see `.qa/agent/README.md`."))
    if not os.environ.get("JIRA_BASE_URL"):
        out.append(finding(
            "OB075", "info", "Test quality agent / Code review agent",
            "Jira isn't configured",
            "Tickets linked from PRs aren't read, and the full-project audit stops (it reads "
            "its requirements from Jira).",
            "Set the `JIRA_BASE_URL` and `JIRA_EMAIL` variables and the `JIRA_API_TOKEN` secret."))
    return out


def run(inv: dict, probe: dict | None) -> list[dict]:
    found = (installation(inv) + branch_coverage(inv) + commenter(inv)
             + install_and_runtime(inv) + test_config(inv, probe) + review_config(inv)
             + governance(inv))
    return sorted(found, key=lambda f: (SEVERITY_ORDER[f["severity"]], f["id"]))


def verdict(found: list[dict]) -> str:
    sev = {f["severity"] for f in found}
    if "blocker" in sev:
        return "not_ready"
    return "ready_after_changes" if "warning" in sev else "ready"
