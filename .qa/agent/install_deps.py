#!/usr/bin/env python3
"""Install what the project's tests need, wherever it declares it. Python standard library only.

QA_INSTALL_COMMAND (a repository variable), or tests.install_command in config.json, wins: one
command, split like a shell would but run without one. Otherwise it's worked out from the repo:

    requirements*.txt at the root          pip install -r <each>
    pyproject.toml with a [project] or      pip install -e . (plus a test, tests or dev extra
    [build-system] table, setup.py/.cfg     when one is declared)
    package.json                           npm ci (npm install without a lockfile)

and when the test command runs pytest and none of that installed it, pip install pytest. A
project with none of these still runs; there's just nothing to install.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tomllib

from common import REPO, load_config

TEST_EXTRAS = ("test", "tests", "testing", "dev")


def _pip(*args: str) -> list[str]:
    return [sys.executable, "-m", "pip", "install", *args]


def _pyproject() -> dict:
    try:
        with open(REPO / "pyproject.toml", "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def plan(cfg: dict) -> list[list[str]]:
    """The commands to run, in order."""
    custom = (
        os.environ.get("QA_INSTALL_COMMAND") or cfg["tests"].get("install_command") or ""
    ).strip()
    if custom:
        return [shlex.split(custom)]

    steps = [_pip("-r", p.name) for p in sorted(REPO.glob("requirements*.txt")) if p.is_file()]
    meta = _pyproject()
    if "project" in meta or "build-system" in meta or any(
        (REPO / f).is_file() for f in ("setup.py", "setup.cfg")
    ):
        extras = meta.get("project", {}).get("optional-dependencies", {})
        extra = next((e for e in TEST_EXTRAS if e in extras), "")
        steps.append(_pip("-e", f".[{extra}]" if extra else "."))
    if (REPO / "package.json").is_file():
        steps.append(["npm", "ci" if (REPO / "package-lock.json").is_file() else "install"])
    return steps


def needs_pytest(cfg: dict) -> bool:
    """The test command runs pytest (`pytest ...` or `python -m pytest ...`) and it isn't there."""
    return "pytest" in shlex.split(cfg["tests"].get("command") or "")[:3] and not shutil.which(
        "pytest"
    )


def main() -> int:
    cfg = load_config()
    steps = plan(cfg)
    if not steps:
        print(
            "No dependency files found (requirements*.txt, pyproject.toml, setup.py, "
            "package.json). Set QA_INSTALL_COMMAND if the tests need more."
        )
    for step in steps:
        print("$ " + shlex.join(step), flush=True)
        code = subprocess.run(step, cwd=REPO, check=False).returncode  # noqa: S603 - argv, no shell
        if code:
            print(f"::error::`{shlex.join(step)}` failed with exit code {code}.")
            return code
    if needs_pytest(cfg):
        print("$ " + shlex.join(_pip("pytest")), flush=True)
        return subprocess.run(_pip("pytest"), cwd=REPO, check=False).returncode  # noqa: S603
    return 0


if __name__ == "__main__":
    sys.exit(main())
