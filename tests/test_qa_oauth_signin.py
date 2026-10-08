"""The QA agents' `oauth` sign-in (.qa/agent/run_agent.py): a Claude seat's OAuth token stored as
the repository secret CLAUDE_CODE_OAUTH_TOKEN.

The token must reach the Claude CLI only for QA_PROVIDER=oauth, never for the OIDC providers, and
must never survive in the agent output that becomes the PR comment.
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
CFG = {"agent": {}}


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        *run_agent.FORBIDDEN,
        "GITHUB_ACTIONS",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_oauth_passes_the_stored_token_to_the_cli(clean_env, tmp_path):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)

    env, audience, token_file, _ = run_agent.build_env("oauth", CFG, tmp_path)

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert audience is None and token_file is None  # no OIDC token is fetched


def test_oauth_trims_whitespace_from_a_pasted_token(clean_env, tmp_path):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", f"  {TOKEN}\n")

    env, *_ = run_agent.build_env("oauth", CFG, tmp_path)

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN


def test_oauth_still_strips_api_keys(clean_env, tmp_path):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-not-allowed")
    clean_env.setenv("ANTHROPIC_AUTH_TOKEN", "not-allowed")

    env, *_ = run_agent.build_env("oauth", CFG, tmp_path)

    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env


@pytest.mark.parametrize("value", [None, "", "   "])
def test_oauth_without_the_secret_fails_setup(clean_env, tmp_path, value):
    if value is not None:
        clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", value)

    with pytest.raises(run_agent.SetupError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        run_agent.build_env("oauth", CFG, tmp_path)


def test_oauth_refuses_an_api_key_saved_under_the_token_name(clean_env, tmp_path):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-api03-an-api-key")

    with pytest.raises(run_agent.SetupError, match="API key"):
        run_agent.build_env("oauth", CFG, tmp_path)


@pytest.mark.parametrize("provider", ["anthropic", "bedrock", "vertex"])
def test_oidc_providers_never_receive_the_oauth_token(clean_env, tmp_path, provider):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    clean_env.setenv("ANTHROPIC_FEDERATION_RULE_ID", "rule")
    clean_env.setenv("ANTHROPIC_ORGANIZATION_ID", "org")
    clean_env.setenv("AWS_REGION", "us-east-1")
    clean_env.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "project")

    env, *_ = run_agent.build_env(provider, CFG, tmp_path)

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert TOKEN not in env.values()


def test_unknown_provider_lists_oauth(clean_env, tmp_path):
    with pytest.raises(run_agent.SetupError, match="oauth"):
        run_agent.build_env("api-key", CFG, tmp_path)


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
