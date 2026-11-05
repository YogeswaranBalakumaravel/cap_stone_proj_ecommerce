"""Shared helpers for the gating onboarding agent. Python standard library only."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path

OB_HOME = Path(__file__).resolve().parent
# The tooling that runs (loaded from the base branch in CI); only its sign-in code is used.
QA_HOME = Path(os.environ.get("QA_AGENT_HOME") or OB_HOME.parent / "agent").resolve()
REPO = Path(os.environ.get("OB_REPO") or os.getcwd()).resolve()
# The gating being onboarded, as this checkout has it: its config is what gets judged.
TARGET_AGENT = REPO / ".qa" / "agent"
TARGET_REVIEW = REPO / ".qa" / "code-review"
TARGET_COMMENTER = REPO / ".qa" / "commenter"
OB_OUT = (REPO / os.environ.get("OB_OUT", ".onboard-out")).resolve()
WORKFLOWS = REPO / ".github" / "workflows"

# The gate workflows this toolkit ships, by file name.
GATE_FILES = {
    "test-quality-agent.yml": "Test quality agent",
    "code-review-agent.yml": "Code review agent",
    "pr-tests.yml": "PR test gate",
    "gitleaks.yml": "Secret scan (Gitleaks)",
    "gate-findings-commenter.yml": "Gate findings commenter",
}
# Branches that usually take PRs and so need the gates.
LONG_LIVED = re.compile(
    r"^(main|master|trunk|develop|development|dev|staging|stage|uat|qa|test|preprod|prod|"
    r"production|release|releases?/.+|hotfix/.+)$"
)


def git(*args: str, check: bool = False) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, encoding="utf-8", errors="replace",
        check=check,
    )
    return result.stdout if result.returncode == 0 else ""


def match(path: str, globs) -> bool:
    """Same semantics as the gates: fnmatch, where `*` also matches `/`."""
    return any(fnmatch.fnmatchcase(path, g) for g in globs or [])


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def read_text(path: Path, limit: int = 400_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""
