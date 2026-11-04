#!/usr/bin/env python3
"""The onboarding agent's one headless pass: review the findings, find the skips the scripts can't,
and say exactly what to change. Python standard library only.

Sign-in is the test quality agent's (.qa/agent/run_agent.py, QA_PROVIDER = anthropic | bedrock |
vertex | foundry | oauth | none). With QA_PROVIDER=none the pass is skipped and the report rests
on the deterministic checks. The agent is read-only and never runs the repository's code.

Writes .onboard-out/advise.json (and advise.meta.json, advise.envelope.json).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ob_common import OB_HOME, OB_OUT, QA_HOME, REPO, read_json, write_json

READ_ONLY = "Read,Grep,Glob,Bash(git diff *),Bash(git show *),Bash(git log *)"


def skip(reason: str, disabled: bool = False) -> int:
    write_json(OB_OUT / "advise.meta.json", {"pass": "advise", "ok": False, "disabled": disabled,
                                             "error": reason})
    print(f"Onboarding agent skipped: {reason}" if disabled else f"::warning::{reason}")
    return 0


def main() -> int:
    cfg = read_json(OB_HOME / "config.json", {"agent": {}})
    provider = (os.environ.get("QA_PROVIDER") or cfg["agent"].get("provider") or "none")
    provider = provider.strip().lower()
    if provider == "none":
        return skip("QA_PROVIDER=none, so the report uses the deterministic checks only.", True)
    if not (OB_OUT / "findings.json").is_file():
        return skip("run `onboard.py inspect` first: there are no findings to review.")

    # The shared sign-in and output parsing write to QA_OUT: point it at the onboarding output.
    os.environ["QA_OUT"] = str(OB_OUT)
    sys.path.insert(0, str(QA_HOME))
    try:
        from common import extract_agent_output
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
        return skip(f"can't load the shared sign-in from {QA_HOME} ({exc}).")

    prompt = (OB_HOME / "prompts" / "advise.md").read_text(encoding="utf-8")
    prompt = prompt.replace("{{OB_OUT}}", os.environ.get("OB_OUT", ".onboard-out").rstrip("/"))
    schema = json.dumps(
        json.loads((OB_HOME / "schemas" / "advise.schema.json").read_text(encoding="utf-8")),
        separators=(",", ":"),
    )
    prompt += ("\n\n## Output format\n\nReturn one JSON object that matches this JSON Schema:"
               "\n\n```json\n" + schema + "\n```\n")
    (OB_OUT / "advise.prompt.md").write_text(prompt, encoding="utf-8")

    agent = cfg["agent"]
    private_dir = Path(tempfile.mkdtemp(prefix="ob-agent-",
                                        dir=os.environ.get("RUNNER_TEMP") or None))
    rotator, cleanup, env = None, [], {}
    try:
        try:
            env, audience, token_file, cleanup = build_env(provider, {"agent": agent}, private_dir)
            if audience and token_file:
                write_private(token_file, fetch_oidc_token(audience))
                rotator = TokenRotator(audience, token_file)
                rotator.start()
        except (SetupError, OSError, ValueError) as exc:
            print(f"::error::Agent setup failed: {exc}")
            write_json(OB_OUT / "advise.envelope.json",
                       {"type": "result", "is_error": True, "subtype": "setup_failed",
                        "result": str(exc)})
            extract_agent_output("advise")
            return 0
        args = [
            os.environ.get("QA_CLAUDE_BIN", "claude"), "-p", prompt,
            "--output-format", "json",
            "--max-turns", str(max(1, int(agent.get("max_turns") or 40))),
            "--tools", "Read,Grep,Glob,Bash", "--allowedTools", READ_ONLY,
            "--setting-sources", "user", "--strict-mcp-config", "--no-session-persistence",
        ]
        if agent.get("use_json_schema", True):
            args += ["--json-schema", schema]
        if agent.get("max_budget_usd"):
            args += ["--max-budget-usd", str(agent["max_budget_usd"])]
        model = os.environ.get("QA_MODEL") or agent.get("model")
        if model:
            args += ["--model", model]
        print(f"Onboarding agent via {provider}" + (f", model {model}" if model else ""))
        timeout = int(agent.get("timeout_seconds") or 1200)
        with (open(OB_OUT / "advise.envelope.json", "wb") as out,
              open(OB_OUT / "advise.stderr.log", "wb") as err):
            try:
                subprocess.run(args, stdout=out, stderr=err, env=env, cwd=REPO,  # noqa: S603
                               timeout=timeout)
            except subprocess.TimeoutExpired:
                print(f"::warning::The onboarding agent timed out after {timeout}s.")
            except FileNotFoundError:
                out.write(json.dumps({"type": "result", "is_error": True,
                                      "subtype": "cli_missing",
                                      "result": "The Claude Code CLI isn't installed."}).encode())
        scrub_secret([OB_OUT / "advise.envelope.json", OB_OUT / "advise.stderr.log"],
                     env.get(OAUTH_TOKEN))
        extract_agent_output("advise")
        return 0  # advisory: a failed pass leaves the deterministic report
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
