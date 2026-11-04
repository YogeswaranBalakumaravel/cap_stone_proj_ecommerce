#!/usr/bin/env python3
"""Run the project's tests the way the test quality agent's evidence job will. Stdlib only.

This is the one onboarding step that runs the repository's code, so the workflow runs it in a job
with no credentials, like the evidence job. It installs with .qa/agent/install_deps.py and runs
tests.command from .qa/agent/config.json, then says why it failed when it did: a failing
baseline makes every later check of the test quality agent "incomplete".

Writes .onboard-out/tests.json.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from ob_common import OB_OUT, REPO, TARGET_AGENT, read_json, write_json

# Credentials the tests must not see: the evidence job won't have them either.
STRIP = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|_KEY$|CREDENTIAL|^ACTIONS_ID_TOKEN)", re.I)
CAUSES = [
    ("missing_dependency", r"ModuleNotFoundError|No module named|Cannot find module|"
     r"ImportError|package .* does not exist|cannot find package"),
    ("service_unavailable", r"Connection refused|ECONNREFUSED|could not connect to server|"
     r"OperationalError|ConnectionError|Can't connect to MySQL|getaddrinfo|"
     r"Name or service not known|Temporary failure in name resolution"),
    ("missing_env", r"KeyError: '[A-Z][A-Z0-9_]+'|environment variable|is not set|"
     r"ImproperlyConfigured|Missing required env"),
    ("no_tests", r"no tests ran|collected 0 items|No tests found|No test files found"),
    ("runner_missing", r"command not found|is not recognized|No such file or directory: '"),
]


def classify(output: str, code: int | None) -> list[str]:
    found = [name for name, rx in CAUSES if re.search(rx, output)]
    if code == 5 and "no_tests" not in found:  # pytest: nothing collected
        found.append("no_tests")
    return found


def _run(command: list[str] | str, env: dict, timeout: int) -> tuple[int | None, str, float]:
    """A string runs through the shell, exactly as run_probes.py runs tests.command."""
    start = time.monotonic()
    try:
        done = subprocess.run(  # noqa: S602,S603 - the repo's own configured test command
            command, cwd=REPO, env=env, capture_output=True, text=True, errors="replace",
            timeout=timeout, shell=isinstance(command, str),
        )
        code, out = done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired as exc:
        code, out = None, f"{exc.stdout or ''}{exc.stderr or ''}\n[timed out after {timeout}s]"
    except FileNotFoundError as exc:
        code, out = 127, f"command not found: {exc.filename}"
    return code, out, round(time.monotonic() - start, 1)


def main() -> int:
    cfg = read_json(TARGET_AGENT / "config.json", {}) or {}
    tests = cfg.get("tests") or {}
    env = {k: v for k, v in os.environ.items() if not STRIP.search(k)}
    result = {"ran": False, "command": tests.get("command", ""), "install": None}

    installer = TARGET_AGENT / "install_deps.py"
    if installer.is_file() and os.environ.get("OB_SKIP_INSTALL") != "1":
        code, out, secs = _run([sys.executable, str(installer)], env, 1200)
        result["install"] = {"exit_code": code, "seconds": secs, "tail": out[-3000:],
                             "causes": classify(out, code) if code else []}
    if not result["command"]:
        result["status"] = "no_command"
    else:
        timeout = int(tests.get("timeout_seconds") or 600)
        code, out, secs = _run(result["command"], env, timeout)
        inconclusive = set(tests.get("inconclusive_exit_codes") or [])
        result.update(
            ran=True,
            exit_code=code,
            seconds=secs,
            timeout_seconds=timeout,
            status="timeout" if code is None else "passed" if code == 0
            else "inconclusive" if code in inconclusive else "failed",
            tail=out[-6000:],
            causes=classify(out, code) if code else [],
        )
    write_json(OB_OUT / "tests.json", result)
    print(f"Tests: {result.get('status')} ({result.get('seconds', 0)}s) "
          f"causes={result.get('causes', [])}")
    return 0  # findings, not failures: checks.py decides what this means


if __name__ == "__main__":
    sys.exit(main())
