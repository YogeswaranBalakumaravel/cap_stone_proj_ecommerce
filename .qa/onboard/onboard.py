#!/usr/bin/env python3
"""Gating onboarding: check that every gate will run, and actually check something, on this repo.

    onboard.py tests     install and run the tests as the evidence job will (runs repo code)
    onboard.py inspect   inventory the repo and run the deterministic checks
    onboard.py advise    the agent's pass (skipped with QA_PROVIDER=none)
    onboard.py report    write .onboard-out/report.md and the verdict
    onboard.py all       all four, in that order (`--no-tests` skips the first)

Run from the repository root. Output goes to .onboard-out/. Advisory: the exit code is 0 unless
OB_STRICT=1 and the verdict is "not_ready".
"""

from __future__ import annotations

import os
import sys

import advise
import checks
import inventory
import probe_tests
import report
from ob_common import OB_OUT, read_json, write_json

STEPS = ("tests", "inspect", "advise", "report")


def inspect() -> int:
    inv = inventory.collect()
    found = checks.run(inv, read_json(OB_OUT / "tests.json"))
    write_json(OB_OUT / "inventory.json", {k: v for k, v in inv.items() if k != "tracked"})
    write_json(OB_OUT / "findings.json",
               {"verdict": checks.verdict(found), "findings": found})
    print(f"Inspect: {checks.verdict(found)}; "
          + ", ".join(f"{s} {sum(f['severity'] == s for f in found)}"
                      for s in ("blocker", "warning", "info")))
    return 0


def write_report() -> int:
    inv = read_json(OB_OUT / "inventory.json")
    data = read_json(OB_OUT / "findings.json")
    if not (inv and data):
        print("::error::Run `onboard.py inspect` first.")
        return 1
    text = report.write(inv, data["findings"], data["verdict"])
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text.replace(report.MARKER, "") + "\n")
    print(f"Report: {OB_OUT / 'report.md'} ({data['verdict']})")
    strict = os.environ.get("OB_STRICT") == "1"
    return 1 if strict and data["verdict"] == "not_ready" else 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in (*STEPS, "all"):
        print(__doc__, file=sys.stderr)
        return 2
    OB_OUT.mkdir(parents=True, exist_ok=True)
    steps = STEPS if argv[0] == "all" else (argv[0],)
    if "--no-tests" in argv:
        steps = tuple(s for s in steps if s != "tests")
    run = {"tests": probe_tests.main, "inspect": inspect, "advise": advise.main,
           "report": write_report}
    code = 0
    for step in steps:
        code = run[step]() or code
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
