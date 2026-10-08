#!/usr/bin/env python3
"""Stage 2: the agent's three headless passes. Python standard library only.

    run_passes.py understand   cited map of the existing code, the ask, the implementation
    run_passes.py oracle       separate context: criteria + signatures only, no tools, no repo
    run_passes.py review       the ten checks, with the understanding and the oracle as input

Sign-in is shared with the test quality agent: the provider setup is imported from
.qa/agent/run_agent.py (QA_PROVIDER = anthropic | bedrock | vertex | foundry | oauth | none).
API keys are stripped, and CLAUDE_CODE_OAUTH_TOKEN is kept only for oauth. With QA_PROVIDER=none
every pass is skipped and the gate runs on the scripts alone.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from cr_common import CR_HOME, CR_OUT, REPO, extract_agent_output, load_config, write_json

QA_AGENT_HOME = Path(os.environ.get("QA_AGENT_HOME") or CR_HOME.parent / "agent").resolve()
READ_ONLY = "Read,Grep,Glob,Bash(git diff *),Bash(git show *),Bash(git log *)"
PASSES = ("understand", "oracle", "review")


def skip(pass_name: str, reason: str, disabled: bool = False) -> int:
    write_json(
        CR_OUT / f"{pass_name}.meta.json",
        {"pass": pass_name, "ok": False, "disabled": disabled, "error": reason},
    )
    print(
        f"Pass '{pass_name}' skipped: {reason}"
        if disabled
        else f"::warning::Pass '{pass_name}': {reason}"
    )
    return 0 if disabled else 1


def build_prompt(pass_name: str, cfg: dict) -> tuple[str, str]:
    prompt = (CR_HOME / "prompts" / f"{pass_name}.md").read_text(encoding="utf-8")
    prompt = prompt.replace("{{CR_OUT}}", os.environ.get("CR_OUT", ".cr-out").rstrip("/"))
    prompt = prompt.replace("{{MAX_FINDINGS}}", str(cfg["review"]["max_findings"]))
    if pass_name == "oracle":
        oracle_in = (CR_OUT / "oracle_input.json").read_text(encoding="utf-8")
        prompt = prompt.replace("{{ORACLE_INPUT}}", oracle_in)
    schema = json.dumps(
        json.loads((CR_HOME / "schemas" / f"{pass_name}.schema.json").read_text(encoding="utf-8")),
        separators=(",", ":"),
    )
    prompt += (
        "\n\n## Output format\n\nReturn one JSON object that matches this JSON Schema:\n\n```json\n"
        + schema
        + "\n```\n"
    )
    return prompt, schema


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in PASSES:
        print(f"usage: run_passes.py {'|'.join(PASSES)}", file=sys.stderr)
        return 2
    pass_name = sys.argv[1]
    cfg = load_config()
    CR_OUT.mkdir(parents=True, exist_ok=True)
    provider = (
        (os.environ.get("QA_PROVIDER") or cfg["agent"].get("provider") or "none").strip().lower()
    )
    if provider == "none":
        return skip(
            pass_name, "QA_PROVIDER=none, so the gate uses the deterministic checks only.", True
        )
    ctx = json.loads((CR_OUT / "context.json").read_text(encoding="utf-8"))
    s = ctx["summary"]
    if not (s["backend_files"] or s["frontend_files"] or s["test_files"]):
        return skip(pass_name, "no reviewable files changed.", True)
    if pass_name == "oracle" and not ctx["pr"]["acceptance_criteria"]:
        return skip(
            pass_name, "no acceptance criteria, so there is nothing to derive an oracle from.", True
        )

    sys.path.insert(0, str(QA_AGENT_HOME))
    try:
        from run_agent import (
            OAUTH_TOKEN,
            SetupError,
            TokenRotator,
            build_env,
            fetch_oidc_token,
            scrub_secret,
            write_private,
        )
    except ImportError as exc:
        return skip(
            pass_name, f"can't load the shared sign-in from {QA_AGENT_HOME}/run_agent.py ({exc})."
        )

    private_dir = Path(
        tempfile.mkdtemp(prefix="cr-agent-", dir=os.environ.get("RUNNER_TEMP") or None)
    )
    sandbox = Path(tempfile.mkdtemp(prefix="cr-oracle-", dir=os.environ.get("RUNNER_TEMP") or None))
    rotator, cleanup = None, []
    try:
        try:
            env, audience, token_file, cleanup = build_env(provider, cfg, private_dir)
            if audience and token_file:
                write_private(token_file, fetch_oidc_token(audience))
                rotator = TokenRotator(audience, token_file)
                rotator.start()
        except (SetupError, OSError, ValueError) as exc:
            print(f"::error::Agent setup failed: {exc}")
            write_json(
                CR_OUT / f"{pass_name}.envelope.json",
                {"type": "result", "is_error": True, "subtype": "setup_failed", "result": str(exc)},
            )
            return extract_agent_output(pass_name)

        prompt, schema = build_prompt(pass_name, cfg)
        (CR_OUT / f"{pass_name}.prompt.md").write_text(prompt, encoding="utf-8")
        agent = cfg["agent"]
        turns = (agent.get("max_turns") or {}).get(pass_name, 30)
        budget = (agent.get("max_budget_usd") or {}).get(pass_name)
        if pass_name == "oracle":
            # Separate context: no tools at all, and run outside the repository.
            tools, cwd = ["--tools", ""], sandbox
        else:
            tools, cwd = ["--tools", "Read,Grep,Glob,Bash", "--allowedTools", READ_ONLY], REPO
        args = [
            os.environ.get("QA_CLAUDE_BIN", "claude"),
            "-p",
            prompt,
            "--output-format",
            "json",
            "--max-turns",
            str(max(1, int(turns))),
            *tools,
            "--setting-sources",
            "user",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        if agent.get("use_json_schema", True):
            args += ["--json-schema", schema]
        if budget:
            args += ["--max-budget-usd", str(budget)]
        model = os.environ.get("QA_MODEL") or agent.get("model")
        if model:
            args += ["--model", model]
        print(f"Pass '{pass_name}' via {provider}" + (f", model {model}" if model else ""))
        timeout = int(agent.get("timeout_seconds", 1200))
        with (
            open(CR_OUT / f"{pass_name}.envelope.json", "wb") as out,
            open(CR_OUT / f"{pass_name}.stderr.log", "wb") as err,
        ):
            try:
                code = subprocess.run(
                    args, stdout=out, stderr=err, env=env, cwd=cwd, timeout=timeout
                ).returncode
            except subprocess.TimeoutExpired:
                code = None
                print(f"::warning::Pass '{pass_name}' timed out after {timeout}s.")
            except FileNotFoundError:
                code = None
                out.write(
                    json.dumps(
                        {
                            "type": "result",
                            "is_error": True,
                            "subtype": "cli_missing",
                            "result": "The Claude Code CLI isn't installed.",
                        }
                    ).encode()
                )
        scrub_secret(
            [CR_OUT / f"{pass_name}.envelope.json", CR_OUT / f"{pass_name}.stderr.log"],
            env.get(OAUTH_TOKEN),
        )
        if code:
            print(f"::warning::The agent CLI exited with code {code} (see {pass_name}.stderr.log).")
        return extract_agent_output(pass_name)
    finally:
        if rotator:
            rotator.stopped.set()
        for path in cleanup:
            try:
                path.unlink()
            except OSError:
                pass
        for d in (private_dir, sandbox):
            try:
                d.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
