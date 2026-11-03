"""Where the QA gates get their requirements (.qa/agent/jira_refs.py, used by both collectors).

A PR's requirements come from its description, plus the Jira tickets it links or names. A
full-project audit has no PR, so it must be given Jira links and stops without them. The Jira token
only ever goes to JIRA_BASE_URL, whatever a PR links to.
"""

import builtins
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
QA_AGENT = ROOT / ".qa" / "agent"
CR = ROOT / ".qa" / "code-review"


def _load(name, path, *paths):
    for p in paths:
        if str(p) not in sys.path:
            sys.path.append(str(p))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jira_refs = _load("qa_jira_refs", QA_AGENT / "jira_refs.py")
cr_collect = _load("qa_cr_collect", CR / "collect.py", CR)

JIRA_ENV = ("JIRA_BASE_URL", "JIRA_API_TOKEN", "JIRA_EMAIL", "QA_JIRA_LINKS", "GITHUB_ACTIONS")


@pytest.fixture
def env(monkeypatch):
    for name in JIRA_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ------------------------------------------------------------------ tickets a PR names


def test_pr_links_anywhere_in_the_description_name_tickets(env):
    body = (
        "## What changed\n\nImplements https://acme.atlassian.net/browse/SHOP-12 and see\n"
        "https://acme.atlassian.net/jira/software/projects/SHOP/boards/1?selectedIssue=SHOP-40.\n"
    )
    assert jira_refs.pr_keys("Add checkout", body, "feat/checkout") == ["SHOP-12", "SHOP-40"]


def test_pr_bare_keys_count_in_the_ticket_section_title_and_branch_only(env):
    body = (
        "## What changed\n\nUnlike OPS-9, this is new.\n\n"
        "## Acceptance criteria\n\n- AC-1: works\n- AC-2: UTF-8 names\n\n"
        "## Ticket\n\nSHOP-3, SHOP-4\n"
    )
    keys = jira_refs.pr_keys("SHOP-5 Add checkout", body, "feature/SHOP-6-checkout")
    assert keys == ["SHOP-3", "SHOP-4", "SHOP-5", "SHOP-6"]  # not OPS-9, AC-1, AC-2 or UTF-8


def test_pr_template_example_key_is_not_a_ticket(env):
    body = "## Ticket\n\n<!-- e.g. PROJ-123. The agent can read the ticket. -->\nNone.\n"
    assert jira_refs.pr_keys("Fix", body, "fix/thing") == []


def test_pr_keys_are_distinct_in_order_and_capped(env):
    body = "## Ticket\n\n" + " ".join(f"SHOP-{i}" for i in range(1, 15)) + "\n"
    keys = jira_refs.pr_keys("SHOP-1 again", body, "SHOP-2")
    assert keys == [f"SHOP-{i}" for i in range(1, jira_refs.MAX_KEYS + 1)]


def test_link_to_another_host_never_names_a_ticket(env, capsys):
    env.setenv("JIRA_BASE_URL", "https://acme.atlassian.net")
    body = "See https://evil.example/browse/SHOP-1 and https://acme.atlassian.net/browse/SHOP-2"
    assert jira_refs.pr_keys("Fix", body, "fix") == ["SHOP-2"]
    assert "Ignoring SHOP-1" in capsys.readouterr().out


# ------------------------------------------------------------------ tickets given to an audit


def test_audit_takes_links_and_keys_separated_by_commas_or_spaces(env):
    raw = "https://acme.atlassian.net/browse/SHOP-1, SHOP-2\nSHOP-3;SHOP-1"
    assert jira_refs.audit_keys(raw) == ["SHOP-1", "SHOP-2", "SHOP-3"]


def test_audit_drops_foreign_links_and_text_that_isnt_a_key(env):
    env.setenv("JIRA_BASE_URL", "https://acme.atlassian.net")
    raw = "https://evil.example/browse/SHOP-1 https://acme.atlassian.net/wiki/x shop-2 SHOP-3"
    assert jira_refs.audit_keys(raw) == ["SHOP-3"]


def test_audit_input_wins_and_ci_never_prompts(env):
    env.setattr(builtins, "input", lambda _: pytest.fail("prompted"))
    env.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    env.setenv("QA_JIRA_LINKS", " SHOP-1 ")
    assert jira_refs.ask() == "SHOP-1"
    env.delenv("QA_JIRA_LINKS")
    env.setenv("GITHUB_ACTIONS", "true")
    assert jira_refs.ask() == ""


def test_audit_run_by_hand_asks_for_the_links(env):
    env.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    env.setattr(builtins, "input", lambda prompt: "SHOP-8" if "Jira" in prompt else "")
    assert jira_refs.ask() == "SHOP-8"


@pytest.mark.parametrize(
    ("keys", "config", "read", "message"),
    [
        ([], True, [], "no PR to read requirements from"),
        (["SHOP-1"], False, [], "JIRA_BASE_URL or JIRA_API_TOKEN isn't set"),
        (["SHOP-1", "SHOP-2"], True, [], "Couldn't read any of SHOP-1, SHOP-2"),
    ],
)
def test_audit_without_requirements_stops(env, keys, config, read, message):
    if config:
        env.setenv("JIRA_BASE_URL", "https://acme.atlassian.net")
        env.setenv("JIRA_API_TOKEN", "t")
    with pytest.raises(SystemExit) as stop:
        jira_refs.require_for_audit(keys, read)
    assert message in str(stop.value)


def test_audit_with_one_ticket_read_carries_on(env):
    env.setenv("JIRA_BASE_URL", "https://acme.atlassian.net")
    env.setenv("JIRA_API_TOKEN", "t")
    assert jira_refs.require_for_audit(["SHOP-1", "SHOP-2"], ["SHOP-2"]) is None


# ------------------------------------------------------------------ criteria ids across tickets


def test_ticket_criteria_ids_stay_unique():
    pr = [{"id": "AC-1", "text": "pr one", "source": "pr"}]
    first = [{"id": "AC-1", "text": "t1", "source": "jira:SHOP-1"},
             {"id": "AC-2", "text": "t2", "source": "jira:SHOP-1"}]
    second = [{"id": "AC-2", "text": "u2", "source": "jira:SHOP-2"},
              {"id": "SHOP-2-9", "text": "u9", "source": "jira:SHOP-2"}]
    crit = cr_collect.add_ticket(cr_collect.add_ticket(pr, "SHOP-1", first), "SHOP-2", second)
    assert [c["id"] for c in crit] == ["AC-1", "SHOP-1/AC-1", "AC-2", "SHOP-2/AC-2", "SHOP-2-9"]
    assert [c["text"] for c in crit] == ["pr one", "t1", "t2", "u2", "u9"]


# ------------------------------------------------------------------ both collectors, end to end


class _Jira(BaseHTTPRequestHandler):
    issues = {
        "SHOP-7": "## Acceptance criteria\n\n- AC-1: A guest can pay by card\n"
        "- AC-2: A declined card shows an error\n",
    }
    seen: list = []

    def do_GET(self):  # noqa: N802 - http.server's name
        self.seen.append((self.path, self.headers.get("Authorization")))
        key = self.path.split("?")[0].rsplit("/", 1)[-1]
        if key not in self.issues:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps({"fields": {"summary": "Checkout", "description": self.issues[key]}})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data.encode())

    def log_message(self, *_):
        pass


@pytest.fixture
def jira_server():
    _Jira.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Jira)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def repo(tmp_path):
    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "qa@example.com")
    run("config", "user.name", "qa")
    (tmp_path / "app.py").write_text("def total(a, b):\n    return a + b\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    (tmp_path / "app.py").write_text("def total(a, b):\n    return a + b + 0\n", encoding="utf-8")
    run("commit", "-q", "-am", "change")
    return tmp_path, base


def _collect(which, repo, **extra):
    path, base = repo
    clean = {k: v for k, v in os.environ.items() if k not in JIRA_ENV and "AUDIT" not in k}
    clean.update(QA_BASE_REF=base, CR_BASE_REF=base, QA_REPO=str(path), CR_REPO=str(path))
    clean.update(QA_PROVIDER="none", GITHUB_ACTIONS="true", **extra)
    script = QA_AGENT / "collect_context.py" if which == "test" else CR / "collect.py"
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=path,
        env=clean,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )


AUDIT = {"test": "QA_FULL_AUDIT", "review": "CR_FULL_AUDIT"}
OUT = {"test": ".qa-out", "review": ".cr-out"}


@pytest.mark.parametrize("which", ["test", "review"])
def test_audit_without_jira_links_asks_for_them_and_stops(repo, which):
    result = _collect(which, repo, **{AUDIT[which]: "1"})
    assert result.returncode != 0
    assert "no PR to read requirements from" in result.stdout + result.stderr
    assert not (repo[0] / OUT[which] / "context.json").exists()


@pytest.mark.parametrize("which", ["test", "review"])
def test_audit_reads_requirements_from_the_given_jira_links(repo, jira_server, which):
    result = _collect(
        which,
        repo,
        **{AUDIT[which]: "1"},
        QA_JIRA_LINKS=f"{jira_server}/browse/SHOP-7",
        JIRA_BASE_URL=jira_server,
        JIRA_API_TOKEN="pat",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    ctx = json.loads((repo[0] / OUT[which] / "context.json").read_text(encoding="utf-8"))
    if which == "test":
        assert ctx["jira_keys"] == ["SHOP-7"]
        assert ctx["acceptance_criteria_sources"] == ["jira"]
        md = (repo[0] / ".qa-out" / "context.md").read_text(encoding="utf-8")
        assert "### Jira SHOP-7" in md and "A declined card shows an error" in md
    else:
        assert ctx["pr"]["jira_keys"] == ["SHOP-7"]
        assert [(c["id"], c["text"], c["source"]) for c in ctx["pr"]["acceptance_criteria"]] == [
            ("AC-1", "A guest can pay by card", "jira:SHOP-7"),
            ("AC-2", "A declined card shows an error", "jira:SHOP-7"),
        ]
    assert _Jira.seen and all(auth == "Bearer pat" for _, auth in _Jira.seen)


@pytest.mark.parametrize("which", ["test", "review"])
def test_audit_with_unreadable_tickets_stops(repo, jira_server, which):
    result = _collect(
        which,
        repo,
        **{AUDIT[which]: "1"},
        QA_JIRA_LINKS="SHOP-404",
        JIRA_BASE_URL=jira_server,
        JIRA_API_TOKEN="pat",
    )
    assert result.returncode != 0
    assert "Couldn't read any of SHOP-404" in result.stdout + result.stderr


@pytest.mark.parametrize("which", ["test", "review"])
def test_pr_criteria_combine_the_description_and_the_linked_ticket(repo, jira_server, which):
    body = (
        "## Acceptance criteria\n\n- AC-1: Totals add up\n\n"
        f"## Ticket\n\n{jira_server}/browse/SHOP-7\n"
    )
    result = _collect(
        which, repo, PR_BODY=body, PR_TITLE="Fix totals", HEAD_REF="fix/totals",
        JIRA_BASE_URL=jira_server, JIRA_API_TOKEN="pat",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    ctx = json.loads((repo[0] / OUT[which] / "context.json").read_text(encoding="utf-8"))
    if which == "test":
        assert ctx["acceptance_criteria_sources"] == ["pr_description", "jira"]
    else:
        assert [c["id"] for c in ctx["pr"]["acceptance_criteria"]] == [
            "AC-1", "SHOP-7/AC-1", "AC-2",
        ]


@pytest.mark.parametrize("which", ["test", "review"])
def test_pr_without_tickets_never_calls_jira(repo, jira_server, which):
    result = _collect(
        which, repo, PR_BODY="## Acceptance criteria\n\n- AC-1: Totals add up\n",
        PR_TITLE="Fix totals", HEAD_REF="fix/totals",
        JIRA_BASE_URL=jira_server, JIRA_API_TOKEN="pat",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _Jira.seen == []
