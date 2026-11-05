"""The gating onboarding agent (.qa/onboard): after the gates are copied into a repository, does it
find every place a gate would be skipped or pass without checking, and say how to fix it?

Each end-to-end test builds a small repository in a temp folder with this repo's gates copied in,
changes one thing, and runs onboard.py there as the workflow does.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OB = ROOT / ".qa" / "onboard"
GATES = ["test-quality-agent.yml", "code-review-agent.yml", "pr-tests.yml", "gitleaks.yml",
         "gate-findings-commenter.yml", "ci.yml", "ci-dev.yml", "ci-uat.yml"]


def _load(name):
    if str(OB) not in sys.path:
        sys.path.append(str(OB))
    spec = importlib.util.spec_from_file_location(f"ob_{name}", OB / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checks = _load("checks")
inventory = _load("inventory")
probe = _load("probe_tests")
report = _load("report")


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path, files, branches=("main", "dev", "staging"), gates=GATES):
    repo = tmp_path / "repo"
    for d in ("agent", "code-review", "commenter"):
        (repo / ".qa" / d).mkdir(parents=True)
    shutil.copy(ROOT / ".qa/agent/config.json", repo / ".qa/agent/config.json")
    shutil.copy(ROOT / ".qa/code-review/config.json", repo / ".qa/code-review/config.json")
    shutil.copy(ROOT / ".qa/commenter/commenter.py", repo / ".qa/commenter/commenter.py")
    (repo / ".github/workflows").mkdir(parents=True)
    for g in gates:
        shutil.copy(ROOT / ".github/workflows" / g, repo / ".github/workflows" / g)
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            (repo / path).unlink(missing_ok=True)
        else:
            (repo / path).write_text(text, encoding="utf-8")
    git(repo, "init", "-q", "-b", branches[0])
    git(repo, "config", "user.email", "qa@example.com")
    git(repo, "config", "user.name", "qa")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "gating")
    for b in branches[1:]:
        git(repo, "branch", b)
    return repo


def gitleaks_without_main(repo):
    """Narrow the copied Gitleaks gate to dev and staging, so it misses `main`."""
    gl = repo / ".github/workflows/gitleaks.yml"
    gl.write_text(gl.read_text(encoding="utf-8").replace("[main, dev, staging]", "[dev, staging]"),
                  encoding="utf-8")
    git(repo, "commit", "-q", "-am", "gitleaks misses main")


def onboard(repo, *steps, **env):
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("QA_", "OB_", "JIRA_", "GITHUB_", "GH_"))}
    clean.update(OB_DEFAULT_BRANCH="main", **env)
    for step in steps:
        done = subprocess.run([sys.executable, str(OB / "onboard.py"), step], cwd=repo, env=clean,
                              capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout + done.stderr
    data = json.loads((repo / ".onboard-out/findings.json").read_text(encoding="utf-8"))
    return data["verdict"], {f["id"]: f for f in data["findings"]}, data["findings"]


PY_APP = {
    "app/__init__.py": "def total(a, b):\n    return a + b\n",
    "tests/test_app.py":
        "from app import total\n\n\ndef test_total():\n    assert total(1, 2) == 3\n",
    "requirements.txt": "pytest\n",
}


def config(repo, part, **changes):
    path = repo / ".qa" / part / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for dotted, value in changes.items():
        node = data
        *parents, leaf = dotted.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    path.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------------------------ branches


def test_a_gate_that_misses_a_long_lived_branch_is_a_blocker(tmp_path):
    repo = make_repo(tmp_path, PY_APP)
    gitleaks_without_main(repo)
    verdict, found, _ = onboard(repo, "inspect")
    assert verdict == "not_ready"
    f = found["OB011"]
    assert f["severity"] == "blocker" and f["gate"] == "Secret scan (Gitleaks)"
    assert "`main`" in f["title"] and "Add main to `on.pull_request.branches`" in f["fix"]
    assert f["evidence"] == [{"file": ".github/workflows/gitleaks.yml", "line": 11}]


def test_every_pr_gate_is_checked_against_every_long_lived_branch(tmp_path):
    repo = make_repo(tmp_path, PY_APP, branches=("main", "develop", "feature/x"))
    _, _, findings = onboard(repo, "inspect")
    missing = {(f["gate"], f["title"]) for f in findings if f["id"] == "OB011"}
    gates = {g for g, _ in missing}
    assert gates == {"Test quality agent", "Code review agent", "PR test gate",
                     "Secret scan (Gitleaks)"}
    assert all("`develop`" in t for _, t in missing)
    assert not any("feature/x" in t for _, t in missing)  # not a long-lived branch
    code_review = next(f for f in findings if f["id"] == "OB011" and "Code" in f["gate"])
    assert "contains(fromJSON" in code_review["fix"]  # the job condition needs it too


def test_code_review_job_condition_out_of_step_with_its_trigger(tmp_path):
    wf = (ROOT / ".github/workflows/code-review-agent.yml").read_text(encoding="utf-8")
    wf = wf.replace('fromJSON(\'["main","dev","staging"]\')', 'fromJSON(\'["main","dev"]\')')
    repo = make_repo(tmp_path, {**PY_APP, ".github/workflows/code-review-agent.yml": wf})
    _, found, _ = onboard(repo, "inspect")
    assert found["OB013"]["severity"] == "blocker"
    assert "skipped required check counts as passed" in found["OB013"]["what_happens"]


def test_fork_prs_are_flagged_only_on_public_repos(tmp_path):
    repo = make_repo(tmp_path, PY_APP)
    _, found, _ = onboard(repo, "inspect", OB_REPO_PRIVATE="false")
    assert found["OB014"]["severity"] == "warning"
    _, found, _ = onboard(repo, "inspect", OB_REPO_PRIVATE="true")
    assert "OB014" not in found


# ------------------------------------------------------------------ stack and test config


def test_js_repo_with_the_python_defaults_gets_blockers_and_a_jest_config(tmp_path):
    files = {
        "package.json": json.dumps({"scripts": {"test": "jest"},
                                    "devDependencies": {"jest": "29.7.0"}}),
        "src/cart.js": "export const total = (a, b) => a + b;\n",
        "src/__tests__/cart.test.js": "test('t', () => expect(1).toBe(1));\n",
    }
    repo = make_repo(tmp_path, files)
    verdict, found, _ = onboard(repo, "inspect", "report")
    assert verdict == "not_ready"
    for fid in ("OB040", "OB041", "OB042"):  # Python source globs, test globs and pytest
        assert found[fid]["severity"] == "blocker"
    assert found["OB033"]["severity"] == "warning"  # coverage and mutants are Python-only
    text = (repo / ".onboard-out/report.md").read_text(encoding="utf-8")
    assert '"command": "npx jest --bail"' in text and '"src/*"' in text


def test_installing_from_a_requirements_txt_the_repo_doesnt_have(tmp_path):
    wf = "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n  build:\n" \
         "    runs-on: ubuntu-24.04\n    steps:\n      - uses: actions/setup-python@v7\n" \
         "        with:\n          python-version: \"3.12\"\n          cache: \"pip\"\n" \
         "      - run: pip install -r requirements.txt\n"
    repo = make_repo(tmp_path, {**PY_APP, "requirements.txt": None,
                                ".github/workflows/ci.yml": wf})
    _, found, _ = onboard(repo, "inspect")
    assert found["OB030"]["severity"] == "blocker" and "install_deps.py" in found["OB030"]["fix"]
    assert found["OB031"]["severity"] == "blocker"


def test_python_version_the_project_rules_out(tmp_path):
    pyproject = '[project]\nname = "x"\nversion = "1"\nrequires-python = ">=3.13"\n'
    repo = make_repo(tmp_path, {**PY_APP, "pyproject.toml": pyproject})
    _, found, _ = onboard(repo, "inspect")
    assert "Python 3.12" in found["OB032"]["title"] and ">=3.13" in found["OB032"]["title"]


def test_monorepo_without_root_manifests(tmp_path):
    repo = make_repo(tmp_path, {**PY_APP, "requirements.txt": None,
                                "api/requirements.txt": "flask\n", "web/package.json": "{}"})
    _, found, _ = onboard(repo, "inspect")
    assert "api, web" in found["OB034"]["title"]


@pytest.mark.parametrize(
    ("version", "spec", "ok"),
    [("3.12", ">=3.10,<3.13", True), ("3.12", ">=3.13", False), ("3.12", "3.11", False),
     ("3.11", "3.11.4", True), ("3.12", "~=3.11", True), ("3.10", "!=3.10", False)],
)
def test_python_version_specs(version, spec, ok):
    assert checks._version_ok(version, spec) is ok


# ------------------------------------------------------------------ commenter and code review


def test_commenter_waiting_for_workflows_that_dont_exist(tmp_path):
    repo = make_repo(tmp_path, PY_APP, gates=[g for g in GATES if not g.startswith("ci")])
    _, _, findings = onboard(repo, "inspect")
    waiting = {f["title"] for f in findings if f["id"] == "OB020"}
    assert waiting == {f"The commenter waits for a workflow named `{n}` that doesn't exist"
                       for n in ("CI", "CI - Dev", "CI - UAT")}


def test_stale_architecture_contracts_and_review_globs_that_match_nothing(tmp_path):
    repo = make_repo(tmp_path, {"README.txt": "docs only\n"})
    _, found, findings = onboard(repo, "inspect")
    assert found["OB060"]["severity"] == "blocker"
    stale = [f["title"] for f in findings if f["id"] == "OB061"]
    assert len(stale) == 3  # every contract copied from this repo names modules it lacks
    assert any("app.models, app.routes" in t for t in stale)


# ------------------------------------------------------------------ running the tests


def test_tests_that_need_an_env_var_block_with_the_fix(tmp_path):
    files = {**PY_APP, "tests/test_db.py":
             "import os\n\n\ndef test_db():\n    assert os.environ['DATABASE_URL']\n"}
    repo = make_repo(tmp_path, files)
    config(repo, "agent", tests__command=f'"{sys.executable}" -m pytest -q -x -p no:cacheprovider')
    _, found, _ = onboard(repo, "tests", "inspect", OB_SKIP_INSTALL="1", SECRET_TOKEN="s3cr3t")
    tests = json.loads((repo / ".onboard-out/tests.json").read_text(encoding="utf-8"))
    assert tests["status"] == "failed" and tests["causes"] == ["missing_env"]
    f = found["OB054"]
    assert f["severity"] == "blocker" and "`env:` on the `evidence` job" in f["fix"]
    assert "KeyError: 'DATABASE_URL'" in f["fix"]


def test_probe_never_hands_credentials_to_the_tests(tmp_path):
    files = {**PY_APP, "tests/test_env.py":
             "import os\n\n\ndef test_env():\n    assert 'SECRET_TOKEN' not in os.environ\n"
             "    assert 'GH_TOKEN' not in os.environ\n    assert os.environ['KEEP'] == '1'\n"}
    repo = make_repo(tmp_path, files)
    config(repo, "agent", tests__command=f'"{sys.executable}" -m pytest -q -x -p no:cacheprovider')
    _, found, _ = onboard(repo, "tests", "inspect", OB_SKIP_INSTALL="1", SECRET_TOKEN="s",
                          GH_TOKEN="g", KEEP="1")
    assert "OB053" in found and "OB054" not in found


@pytest.mark.parametrize(
    ("output", "code", "causes"),
    [("E   ModuleNotFoundError: No module named 'flask'", 4, ["missing_dependency"]),
     ("psycopg2.OperationalError: Connection refused", 1, ["service_unavailable"]),
     ("collected 0 items", 5, ["no_tests"]),
     ("", 5, ["no_tests"]),
     ("sh: 1: jest: command not found", 127, ["runner_missing"]),
     ("1 failed: assert 2 == 3", 1, [])],
)
def test_test_failures_are_classified(output, code, causes):
    assert probe.classify(output, code) == causes


def test_slow_suite_without_a_targeted_command_skips_most_mutants():
    inv = {"test_needs": {"required_env": [], "compose_images": [], "service_clients": []}}
    cfg = {"probes": {"time_budget_seconds": 1500, "max_probes": 10, "systematic_max": 15}}
    slow = checks._probe(inv, {"status": "passed", "seconds": 300}, {}, cfg)
    assert [f["id"] for f in slow] == ["OB052"] and "about 5 of 25" in slow[0]["title"]
    fast = checks._probe(inv, {"status": "passed", "seconds": 300},
                         {"targeted_command": "pytest {tests}"}, cfg)
    assert [f["id"] for f in fast] == ["OB053"]


# ------------------------------------------------------------------ a ready repo, and the report


def test_a_fully_adjusted_repo_is_ready(tmp_path):
    files = {**PY_APP, "CODEOWNERS": "/.qa/ @org/qa\n/.github/workflows/ @org/qa\n",
             ".github/pull_request_template.md": "## Acceptance criteria\n\n- AC-1:\n",
             "CLAUDE.md": "# Conventions\n"}
    repo = make_repo(tmp_path, files)
    config(repo, "agent", fail_closed=True)
    config(repo, "code-review", architecture__contracts=[])
    (repo / ".onboard-out").mkdir()
    (repo / ".onboard-out/tests.json").write_text(json.dumps(
        {"ran": True, "status": "passed", "seconds": 2}), encoding="utf-8")
    verdict, found, _ = onboard(repo, "inspect", "report", QA_PROVIDER="anthropic",
                                JIRA_BASE_URL="https://acme.atlassian.net")
    assert {f["severity"] for f in found.values()} <= {"info"}, found.keys()
    # OB022: the commenter is on `main` here, so no warning about the default branch either.
    assert verdict == "ready"
    text = (repo / ".onboard-out/report.md").read_text(encoding="utf-8")
    assert text.startswith(report.MARKER) and "✅ **Ready**" in text
    assert "| Secret scan (Gitleaks) | yes | ✅ ready |" in text


def test_agent_advice_keeps_only_real_citations(tmp_path):
    repo = make_repo(tmp_path, PY_APP)
    onboard(repo, "inspect")
    advice = {
        "verdict": "not_ready", "summary": "Gitleaks misses main.", "confidence": "high",
        "finding_reviews": [{"id": "OB074", "agree": False, "note": "Provider set at org level."}],
        "adjustments": [{
            "title": "Cover main", "file": ".github/workflows/gitleaks.yml",
            "snippet": "branches: [main, dev, staging]", "why": "PRs into main skip it.",
            "resolves": ["OB011"],
            "evidence": [{"file": ".github/workflows/gitleaks.yml", "line": 11},
                         {"file": ".github/workflows/gitleaks.yml", "line": 9999},
                         {"file": "../outside.txt", "line": 1}]}],
        "extra_skips": [], "questions": ["Do PRs ever target `release/*`?"],
    }
    (repo / ".onboard-out/advise.json").write_text(json.dumps(advice), encoding="utf-8")
    onboard(repo, "report")
    text = (repo / ".onboard-out/report.md").read_text(encoding="utf-8")
    assert "**Cover main · `.github/workflows/gitleaks.yml` · resolves OB011** " \
           "(`.github/workflows/gitleaks.yml:11`)" in text
    assert "_2 citation(s) didn't point at a real line and were dropped._" in text
    assert "- OB074: Provider set at org level." in text
    assert "```yaml" in text and "Do PRs ever target `release/*`?" in text


def test_without_a_provider_the_report_says_the_agent_didnt_run(tmp_path):
    repo = make_repo(tmp_path, PY_APP)
    onboard(repo, "inspect", "advise", "report")
    text = (repo / ".onboard-out/report.md").read_text(encoding="utf-8")
    assert "Not available: QA_PROVIDER=none, so the report uses the deterministic checks only." \
           in text


def test_strict_mode_fails_only_when_not_ready(tmp_path):
    repo = make_repo(tmp_path, PY_APP)
    gitleaks_without_main(repo)
    onboard(repo, "inspect")
    env = {**{k: v for k, v in os.environ.items() if not k.startswith(("QA_", "OB_"))},
           "OB_STRICT": "1"}
    done = subprocess.run([sys.executable, str(OB / "onboard.py"), "report"], cwd=repo, env=env,
                          capture_output=True, text=True)
    assert done.returncode == 1  # not_ready: the Gitleaks blocker


def test_workflow_parser_reads_triggers_branches_and_steps():
    wf = inventory.parse_workflow(ROOT / ".github/workflows/code-review-agent.yml")
    assert wf["name"] == "Code review agent"
    assert wf["triggers"]["pull_request"]["branches"] == ["main", "dev", "staging"]
    assert "workflow_dispatch" in wf["triggers"] and wf["jobs"] == ["code-review"]
    commenter = inventory.parse_workflow(ROOT / ".github/workflows/gate-findings-commenter.yml")
    assert commenter["triggers"]["workflow_run"]["workflows"][:3] == ["CI", "CI - Dev", "CI - UAT"]
    onboarding = inventory.parse_workflow(ROOT / ".github/workflows/gating-onboarding.yml")
    assert onboarding["triggers"]["pull_request"]["branches"] is None  # no filter: every PR
