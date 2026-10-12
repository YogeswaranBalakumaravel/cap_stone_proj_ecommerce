#!/usr/bin/env python3
"""Stages 2 and 4: run one headless pass of the test-quality agent. Python standard library only.

    python3 run_agent.py plan      design the probes
    python3 run_agent.py review    answer the six questions

No API keys. ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN and CLAUDE_CODE_OAUTH_TOKEN are removed from
the agent's environment even if they are set, except that QA_PROVIDER=oauth keeps
CLAUDE_CODE_OAUTH_TOKEN. The agent signs in through one provider (repository variable QA_PROVIDER,
or agent.provider in config.json):

    oauth       a Claude seat's OAuth token from `claude setup-token`, stored as the repository
                secret CLAUDE_CODE_OAUTH_TOKEN. Long-lived and stored, so not OIDC.

The other providers use the CI job's own short-lived OIDC identity:

    anthropic   Anthropic workload identity federation: ANTHROPIC_FEDERATION_RULE_ID and
                ANTHROPIC_ORGANIZATION_ID (+ optional ANTHROPIC_SERVICE_ACCOUNT_ID,
                ANTHROPIC_WORKSPACE_ID)
    bedrock     Amazon Bedrock with an IAM role: AWS_ROLE_ARN and AWS_REGION
    vertex      Google Vertex AI with workload identity federation: ANTHROPIC_VERTEX_PROJECT_ID,
                CLOUD_ML_REGION, GCP_WORKLOAD_IDENTITY_PROVIDER (+ optional GCP_SERVICE_ACCOUNT)
    foundry     Microsoft Foundry with Entra workload identity: ANTHROPIC_FOUNDRY_RESOURCE,
                AZURE_CLIENT_ID, AZURE_TENANT_ID, and QA_MODEL (the deployment name)
    none        don't run the agent; the gate uses the scripts' evidence on its own

In GitHub Actions the job needs `permissions: id-token: write`. The script fetches a GitHub OIDC
token for the provider's audience, writes it to a private file, and replaces it every two minutes
while the agent runs, because GitHub tokens expire after about five minutes and Anthropic accepts
each token only once. Outside GitHub Actions no token is fetched, and the provider's normal
credential chain is used (AWS profile, gcloud ADC, az login, or an Anthropic WIF profile).

The agent is read-only: Read, Grep, Glob and three git commands. Repository settings, hooks and
MCP servers are ignored, so a PR can't change what the agent may do.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from common import QA_HOME, QA_OUT, REPO, extract_agent_output, load_config, write_json

FORBIDDEN = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"  # kept only for QA_PROVIDER=oauth
PROVIDER_SWITCHES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
PROVIDER_VARS = {
    "anthropic": (
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_SERVICE_ACCOUNT_ID",
        "ANTHROPIC_WORKSPACE_ID",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_PROFILE",
    ),
    "bedrock": (
        "AWS_ROLE_ARN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_PROFILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_BEDROCK_BASE_URL",
    ),
    "vertex": (
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        "GCP_WORKLOAD_IDENTITY_PROVIDER",
        "GCP_SERVICE_ACCOUNT",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "ANTHROPIC_VERTEX_BASE_URL",
    ),
    "foundry": (
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_FEDERATED_TOKEN_FILE",
        "AZURE_AUTHORITY_HOST",
    ),
    "oauth": (),
}
AUDIENCE = {
    "anthropic": "https://api.anthropic.com",
    "bedrock": "sts.amazonaws.com",
    "foundry": "api://AzureADTokenExchange",
    # vertex: "https://iam.googleapis.com/" + the workload identity provider's resource name
}
READ_ONLY_TOOLS = "Read,Grep,Glob,Bash(git diff *),Bash(git show *),Bash(git log *)"
REFRESH_SECONDS = int(os.environ.get("QA_OIDC_REFRESH_SECONDS", "120"))


class SetupError(Exception):
    pass


# ---------------------------------------------------------------- GitHub OIDC


def in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def fetch_oidc_token(audience: str) -> str:
    url, bearer = (
        os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"),
        os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN"),
    )
    if not url or not bearer:
        raise SetupError(
            "No GitHub OIDC token is available. Give this job `permissions: id-token: write`."
        )
    sep = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{sep}audience={urllib.parse.quote(audience, safe='')}",
        headers={"Authorization": f"bearer {bearer}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        token = json.load(resp).get("value")
    if not token:
        raise SetupError("The GitHub OIDC endpoint returned no token.")
    print(f"::add-mask::{token}")
    return token


def write_private(path: Path, text: str) -> None:
    """Write atomically with mode 0600, so a reader never sees a half-written token."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


class TokenRotator(threading.Thread):
    """Keeps a fresh OIDC token in the file while the agent runs."""

    def __init__(self, audience: str, path: Path):
        super().__init__(daemon=True)
        self.audience, self.path, self.stopped = audience, path, threading.Event()

    def run(self):
        while not self.stopped.wait(REFRESH_SECONDS):
            try:
                write_private(self.path, fetch_oidc_token(self.audience))
            except Exception as exc:  # noqa: BLE001 - keep the agent running on the current token
                print(f"::warning::Could not refresh the OIDC token: {exc}")


# ---------------------------------------------------------------- providers


def build_env(
    provider: str, cfg: dict, private_dir: Path
) -> tuple[dict, str | None, Path | None, list[Path]]:
    """Return (environment, OIDC audience or None, token file or None, files to delete)."""
    if provider not in PROVIDER_VARS:
        raise SetupError(
            f"Unknown QA_PROVIDER '{provider}'. "
            "Use anthropic, bedrock, vertex, foundry, oauth or none."
        )
    env = {k: v for k, v in os.environ.items() if k not in FORBIDDEN and k not in PROVIDER_SWITCHES}
    for name in FORBIDDEN:
        if os.environ.get(name) and not (provider == "oauth" and name == OAUTH_TOKEN):
            print(f"::notice::{name} is set but ignored by QA_PROVIDER={provider}.")
    for other, names in PROVIDER_VARS.items():  # settings for other providers must not leak in
        if other != provider:
            for name in names:
                env.pop(name, None)
    for name in PROVIDER_VARS[provider]:  # empty repository variables arrive as empty strings
        if not env.get(name):
            env.pop(name, None)

    ci = in_github_actions()
    token_file = private_dir / "oidc-token.jwt"
    cleanup = [token_file]
    model = os.environ.get("QA_MODEL") or cfg["agent"].get("model") or ""

    def need(*names):
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise SetupError(
                f"QA_PROVIDER={provider} needs these repository variables: {', '.join(missing)}"
            )

    if provider == "anthropic":
        need("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID")
        audience = os.environ.get("QA_OIDC_AUDIENCE") or AUDIENCE["anthropic"]
        if ci:
            env["ANTHROPIC_IDENTITY_TOKEN_FILE"] = str(token_file)
        return env, (audience if ci else None), (token_file if ci else None), cleanup

    if provider == "bedrock":
        need("AWS_REGION")
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        if ci:
            need("AWS_ROLE_ARN")
            env["AWS_WEB_IDENTITY_TOKEN_FILE"] = str(token_file)
            env["AWS_ROLE_SESSION_NAME"] = f"qa-agent-{os.environ.get('GITHUB_RUN_ID', 'local')}"
        return env, (AUDIENCE["bedrock"] if ci else None), (token_file if ci else None), cleanup

    if provider == "vertex":
        need("ANTHROPIC_VERTEX_PROJECT_ID")
        env.setdefault("CLOUD_ML_REGION", "global")
        env["CLAUDE_CODE_USE_VERTEX"] = "1"
        if not ci:
            return env, None, None, cleanup
        need("GCP_WORKLOAD_IDENTITY_PROVIDER")
        wip = env["GCP_WORKLOAD_IDENTITY_PROVIDER"].strip().strip("/")
        if not wip.startswith("projects/"):
            raise SetupError(
                "GCP_WORKLOAD_IDENTITY_PROVIDER must look like "
                "projects/NUMBER/locations/global/workloadIdentityPools/POOL/providers/PROVIDER"
            )
        creds = {
            "type": "external_account",
            "audience": f"//iam.googleapis.com/{wip}",
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "token_url": "https://sts.googleapis.com/v1/token",
            "credential_source": {"file": str(token_file), "format": {"type": "text"}},
        }
        if env.get("GCP_SERVICE_ACCOUNT"):
            creds["service_account_impersonation_url"] = (
                "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                f"{env['GCP_SERVICE_ACCOUNT']}:generateAccessToken"
            )
        creds_file = private_dir / "gcp-credentials.json"
        write_private(creds_file, json.dumps(creds, indent=2))
        env["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds_file)
        cleanup.append(creds_file)
        return env, f"https://iam.googleapis.com/{wip}", token_file, cleanup

    if provider == "foundry":
        if not (env.get("ANTHROPIC_FOUNDRY_RESOURCE") or env.get("ANTHROPIC_FOUNDRY_BASE_URL")):
            raise SetupError(
                "QA_PROVIDER=foundry needs ANTHROPIC_FOUNDRY_RESOURCE "
                "(or ANTHROPIC_FOUNDRY_BASE_URL)"
            )
        if not model:
            raise SetupError(
                "QA_PROVIDER=foundry needs QA_MODEL set to your Claude deployment name"
            )
        env["CLAUDE_CODE_USE_FOUNDRY"] = "1"
        for tier in ("OPUS", "SONNET", "HAIKU"):  # Foundry doesn't pick models for you
            env.setdefault(f"ANTHROPIC_DEFAULT_{tier}_MODEL", model)
        if ci:
            need("AZURE_CLIENT_ID", "AZURE_TENANT_ID")
            env["AZURE_FEDERATED_TOKEN_FILE"] = str(token_file)
        return env, (AUDIENCE["foundry"] if ci else None), (token_file if ci else None), cleanup

    if provider == "oauth":
        token = os.environ.get(OAUTH_TOKEN, "").strip()
        if not token:
            raise SetupError(
                f"QA_PROVIDER=oauth needs the repository secret {OAUTH_TOKEN} "
                "(run `claude setup-token` with the seat's account)"
            )
        if ci:
            print(f"::add-mask::{token}")
        if token.startswith("sk-ant-api"):
            raise SetupError(
                f"{OAUTH_TOKEN} holds an API key, not an OAuth token. "
                "Store the token that `claude setup-token` prints (it starts with sk-ant-oat)."
            )
        print("::notice::Signing in with the stored CLAUDE_CODE_OAUTH_TOKEN secret, not OIDC.")
        env[OAUTH_TOKEN] = token
        return env, None, None, cleanup

    raise SetupError(f"Unhandled provider '{provider}'.")


# ---------------------------------------------------------------- the agent call


def build_prompt(pass_name: str, cfg: dict) -> tuple[str, str]:
    prompt = (QA_HOME / "prompts" / f"{pass_name}.md").read_text(encoding="utf-8")
    prompt = prompt.replace("{{MAX_PROBES}}", str(cfg["probes"]["max_probes"]))
    prompt = prompt.replace("{{QA_OUT}}", os.environ.get("QA_OUT", ".qa-out").rstrip("/"))
    schema = json.dumps(
        json.loads((QA_HOME / "schemas" / f"{pass_name}.schema.json").read_text(encoding="utf-8")),
        separators=(",", ":"),
    )
    prompt += (
        "\n\n## Output format\n\nReturn one JSON object that matches this JSON Schema:\n\n```json\n"
        + schema
        + "\n```\n"
    )
    return prompt, schema


def scrub_secret(paths: list[Path], secret: str | None) -> None:
    """Blank a stored secret out of the agent's output. Log masking doesn't cover the PR comment
    or the uploaded evidence, and a PR could try to talk the agent into echoing the token."""
    if not secret:
        return
    needle = secret.encode("utf-8")
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if needle in data:
            path.write_bytes(data.replace(needle, b"***"))
            print(f"::warning::Removed the OAuth token from {path.name}.")


def record_failure(pass_name: str, subtype: str, message: str) -> int:
    write_json(
        QA_OUT / f"{pass_name}.envelope.json",
        {"type": "result", "is_error": True, "subtype": subtype, "result": message},
    )
    return extract_agent_output(pass_name)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("plan", "review"):
        print("usage: run_agent.py plan|review", file=sys.stderr)
        return 2
    pass_name = sys.argv[1]
    cfg = load_config()
    QA_OUT.mkdir(parents=True, exist_ok=True)
    provider = (
        (os.environ.get("QA_PROVIDER") or cfg["agent"].get("provider") or "none").strip().lower()
    )

    if provider == "none":
        write_json(
            QA_OUT / f"{pass_name}.meta.json",
            {
                "pass": pass_name,
                "ok": False,
                "disabled": True,
                "error": "the agent is turned off (QA_PROVIDER=none)",
            },
        )
        print(
            f"Agent pass '{pass_name}' skipped: QA_PROVIDER=none, "
            "so the gate uses the scripts' evidence only."
        )
        return 0

    # When the probes ran in this workspace, make sure the review sees the PR exactly as committed.
    if (
        pass_name == "review"
        and subprocess.run(["git", "diff", "--quiet"], cwd=REPO).returncode != 0
    ):
        print(
            "::warning::Tracked files changed after the probe step; "
            "restoring them before the review."
        )
        subprocess.run(["git", "checkout", "--", "."], cwd=REPO)

    private_dir = Path(
        tempfile.mkdtemp(prefix="qa-agent-", dir=os.environ.get("RUNNER_TEMP") or None)
    )
    rotator = None
    cleanup: list[Path] = []
    try:
        try:
            env, audience, token_file, cleanup = build_env(provider, cfg, private_dir)
            if audience and token_file:
                write_private(token_file, fetch_oidc_token(audience))
                rotator = TokenRotator(audience, token_file)
                rotator.start()
        except (SetupError, OSError, ValueError) as exc:
            print(f"::error::Agent setup failed: {exc}")
            return record_failure(pass_name, "setup_failed", str(exc))

        prompt, schema = build_prompt(pass_name, cfg)
        (QA_OUT / f"{pass_name}.prompt.md").write_text(prompt, encoding="utf-8")
        agent = cfg["agent"]
        max_turns = str((agent.get("max_turns") or {}).get(pass_name, 25))
        args = [
            os.environ.get("QA_CLAUDE_BIN", "claude"),
            "-p",
            prompt,
            "--output-format",
            "json",
            "--max-turns",
            max_turns,
            "--tools",
            "Read,Grep,Glob,Bash",
            "--allowedTools",
            READ_ONLY_TOOLS,
            "--setting-sources",
            "user",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        if agent.get("use_json_schema", True):
            args += ["--json-schema", schema]
        if agent.get("max_budget_usd"):
            args += ["--max-budget-usd", str(agent["max_budget_usd"])]
        model = os.environ.get("QA_MODEL") or agent.get("model")
        if model:
            args += ["--model", model]

        print(
            f"Agent pass '{pass_name}' via {provider}: max {max_turns} turns"
            + (f", model {model}" if model else "")
        )
        timeout = int(agent.get("timeout_seconds", 1200))
        with (
            open(QA_OUT / f"{pass_name}.envelope.json", "wb") as out,
            open(QA_OUT / f"{pass_name}.stderr.log", "wb") as err,
        ):
            try:
                code = subprocess.run(
                    args, stdout=out, stderr=err, env=env, timeout=timeout
                ).returncode
            except subprocess.TimeoutExpired:
                code = None
                print(f"::warning::Agent pass '{pass_name}' timed out after {timeout}s.")
            except FileNotFoundError:
                return record_failure(
                    pass_name, "cli_missing", "The Claude Code CLI isn't installed on this runner."
                )
        scrub_secret(
            [QA_OUT / f"{pass_name}.envelope.json", QA_OUT / f"{pass_name}.stderr.log"],
            env.get(OAUTH_TOKEN),
        )
        if code:
            print(
                f"::warning::The agent CLI exited with code {code} "
                f"(details in {QA_OUT.name}/{pass_name}.stderr.log)."
            )
        return extract_agent_output(pass_name)
    finally:
        if rotator:
            rotator.stopped.set()
        for path in cleanup:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            private_dir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
