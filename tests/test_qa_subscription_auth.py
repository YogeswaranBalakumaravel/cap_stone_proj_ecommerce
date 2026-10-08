"""AC-168: the test quality agent's experimental `subscription` sign-in (.qa/agent/run_agent.py).

The seat token must reach the Claude CLI only for QA_PROVIDER=subscription, never for the OIDC
providers, and must never survive in the agent output that becomes the PR comment.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

# run_agent.py is QA tooling, not a package: load it by path. It imports its sibling `common`.
QA_AGENT = Path(__file__).resolve().parents[1] / ".qa" / "agent"
sys.path.insert(0, str(QA_AGENT))
_spec = importlib.util.spec_from_file_location("qa_run_agent", QA_AGENT / "run_agent.py")
run_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_agent)

TOKEN = "sk-ant-oat01-test-only-not-a-real-token"


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        *run_agent.FORBIDDEN,
        run_agent.SUBSCRIPTION_SECRET,
        "GITHUB_ACTIONS",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


CFG = {"agent": {}}


def test_subscription_passes_the_seat_token_to_the_cli(clean_env, tmp_path):
    clean_env.setenv(run_agent.SUBSCRIPTION_SECRET, TOKEN)

    env, audience, token_file, _ = run_agent.build_env("subscription", CFG, tmp_path)

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert run_agent.SUBSCRIPTION_SECRET not in env
    assert audience is None and token_file is None  # no OIDC token is fetched


def test_subscription_trims_whitespace_from_a_pasted_token(clean_env, tmp_path):
    clean_env.setenv(run_agent.SUBSCRIPTION_SECRET, f"  {TOKEN}\n")

    env, *_ = run_agent.build_env("subscription", CFG, tmp_path)

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN


@pytest.mark.parametrize("value", [None, "", "   "])
def test_subscription_without_the_secret_fails_setup(clean_env, tmp_path, value):
    if value is not None:
        clean_env.setenv(run_agent.SUBSCRIPTION_SECRET, value)

    with pytest.raises(run_agent.SetupError, match=run_agent.SUBSCRIPTION_SECRET):
        run_agent.build_env("subscription", CFG, tmp_path)


def test_subscription_ignores_a_directly_set_oauth_token(clean_env, tmp_path):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", "someone-elses-token")

    with pytest.raises(run_agent.SetupError):
        run_agent.build_env("subscription", CFG, tmp_path)


def test_oidc_providers_never_receive_the_seat_token(clean_env, tmp_path):
    clean_env.setenv(run_agent.SUBSCRIPTION_SECRET, TOKEN)
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    clean_env.setenv("ANTHROPIC_FEDERATION_RULE_ID", "rule")
    clean_env.setenv("ANTHROPIC_ORGANIZATION_ID", "org")

    env, *_ = run_agent.build_env("anthropic", CFG, tmp_path)

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert run_agent.SUBSCRIPTION_SECRET not in env
    assert TOKEN not in env.values()


def test_unknown_provider_lists_subscription(clean_env, tmp_path):
    with pytest.raises(run_agent.SetupError, match="subscription"):
        run_agent.build_env("seat", CFG, tmp_path)


def test_scrub_secret_blanks_the_token_out_of_agent_output(tmp_path):
    envelope = tmp_path / "review.envelope.json"
    envelope.write_text(f'{{"result": "env shows {TOKEN} twice: {TOKEN}"}}', encoding="utf-8")
    clean = tmp_path / "review.stderr.log"
    clean.write_text("nothing secret here", encoding="utf-8")

    run_agent.scrub_secret([envelope, clean, tmp_path / "missing.log"], TOKEN)

    assert TOKEN not in envelope.read_text(encoding="utf-8")
    assert envelope.read_text(encoding="utf-8").count("***") == 2
    assert clean.read_text(encoding="utf-8") == "nothing secret here"


@pytest.mark.parametrize("secret", [None, ""])
def test_scrub_secret_leaves_files_alone_without_a_secret(tmp_path, secret):
    log = tmp_path / "plan.stderr.log"
    log.write_text("unchanged", encoding="utf-8")

    run_agent.scrub_secret([log], secret)

    assert log.read_text(encoding="utf-8") == "unchanged"
