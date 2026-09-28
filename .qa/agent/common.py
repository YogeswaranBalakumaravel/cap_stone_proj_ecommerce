#!/usr/bin/env python3
"""Shared helpers for the test-quality agent. Python standard library only."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

QA_HOME = Path(__file__).resolve().parent
REPO = Path(os.environ.get("QA_REPO") or os.getcwd()).resolve()
QA_OUT = (REPO / os.environ.get("QA_OUT", ".qa-out")).resolve()

DIMENSIONS = {
    "meaningful_tests": "Tests are meaningful",
    "business_scenarios": "Business scenarios are covered",
    "sunny_rainy": "Sunny- and rainy-day paths are covered",
    "edge_cases": "Edge cases are considered",
    "trivial_assertions": "Assertions are not trivial",
    "change_validation": "Tests validate the change",
}

# Never passed to the project's tests.
SECRET_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "JIRA_API_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_URL",
)


def load_config() -> dict:
    cfg = json.loads((QA_HOME / "config.json").read_text(encoding="utf-8"))
    mode = os.environ.get("QA_MODE", "").strip().lower()
    if mode:
        cfg["mode"] = mode
    if os.environ.get("QA_TEST_COMMAND"):
        cfg["tests"]["command"] = os.environ["QA_TEST_COMMAND"]
    if "QA_TARGETED_TEST_COMMAND" in os.environ:
        cfg["tests"]["targeted_command"] = os.environ["QA_TARGETED_TEST_COMMAND"]
    if cfg.get("mode") not in ("advisory", "blocking"):
        raise SystemExit(
            f"config.json: mode must be 'advisory' or 'blocking', got {cfg.get('mode')!r}"
        )
    return cfg


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def git(*args: str, check: bool = True) -> str:
    res = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, encoding="utf-8", errors="replace"
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def git_bytes(*args: str) -> bytes:
    res = subprocess.run(["git", *args], cwd=REPO, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {res.stderr.decode('utf-8', 'replace').strip()}"
        )
    return res.stdout


def base_ref() -> str:
    if os.environ.get("QA_BASE_REF"):
        return os.environ["QA_BASE_REF"]
    if os.environ.get("GITHUB_BASE_REF"):
        return f"origin/{os.environ['GITHUB_BASE_REF']}"
    return "origin/main"


def merge_base() -> str:
    return git("merge-base", "HEAD", base_ref()).strip()


def _match(path: str, globs) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in globs or [])


def classify(path: str, cfg: dict) -> str:
    """'test', 'source', 'excluded' or 'other'. In the globs, * also matches /."""
    p = cfg["paths"]
    if _match(path, p.get("exclude_globs")):
        return "excluded"
    if _match(path, p.get("test_globs")):
        return "test"
    if _match(path, p.get("source_globs")):
        return "source"
    return "other"


def repo_path(p) -> str | None:
    """Normalise a path the agent gave us. None if it is empty or escapes the repository."""
    if not isinstance(p, str) or not p.strip() or "\x00" in p:
        return None
    p = p.strip().replace("\\", "/")
    full = (REPO / p).resolve()
    try:
        rel = full.relative_to(REPO)
    except ValueError:
        return None
    rel_s = rel.as_posix()
    if rel_s in ("", ".") or rel.parts[0] == ".git":
        return None
    if QA_OUT == full or QA_OUT in full.parents:
        return None
    return rel_s


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ratio(num, den):
    return None if not den else round(num / den, 4)


def test_name_in_text(name: str, text: str) -> bool:
    """Accept pytest ids (tests/x.py::TestA::test_b), dotted names or bare names."""
    short = re.split(r"::|\.|#", name.strip())[-1].strip("() ") if name else ""
    return not short or short in text


class RefChecker:
    """Checks that file:line citations from the agent point at real code in the repository."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._text: dict[str, str | None] = {}
        self.total = 0
        self.valid = 0
        self.rejected: list[dict] = []

    def _read(self, rel: str):
        if rel not in self._text:
            try:
                self._text[rel] = (REPO / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._text[rel] = None
        return self._text[rel]

    def check(self, file, line, name: str = "", want: str | None = None) -> str | None:
        """Return the normalised path when the citation is valid, otherwise None."""
        self.total += 1
        rel = repo_path(file)
        line_no = as_int(line)
        reason = ""
        if rel is None:
            reason = "path is empty or outside the repository"
        else:
            text = self._read(rel)
            if text is None:
                reason = "file not found"
            elif line_no is None or not 1 <= line_no <= max(1, len(text.splitlines())):
                reason = f"line {line} is out of range"
            elif want == "test" and classify(rel, self.cfg) != "test":
                reason = "not a test file"
            elif name and not test_name_in_text(name, text):
                reason = f"test '{name}' not found in the file"
        if reason:
            self.rejected.append({"file": file, "line": line, "name": name, "reason": reason})
            return None
        self.valid += 1
        return rel


def parse_json_text(text: str):
    """Pull a JSON object out of model text (plain, fenced, or surrounded by prose)."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates += [text, text[text.find("{") : text.rfind("}") + 1]]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def extract_agent_output(pass_name: str) -> int:
    """Turn the CLI's JSON envelope into <pass>.json plus <pass>.meta.json."""
    meta = {
        "pass": pass_name,
        "ok": False,
        "error": "",
        "cost_usd": None,
        "turns": None,
        "subtype": None,
    }
    data = None
    try:
        raw = (QA_OUT / f"{pass_name}.envelope.json").read_text(encoding="utf-8").strip()
        envelope = json.loads(raw) if raw else None
        if isinstance(envelope, list):  # stream-style output: keep the final result message
            envelope = next(
                (
                    m
                    for m in reversed(envelope)
                    if isinstance(m, dict) and m.get("type") == "result"
                ),
                None,
            )
        if not isinstance(envelope, dict):
            raise ValueError("the agent produced no result")
        meta.update(
            cost_usd=envelope.get("total_cost_usd"),
            turns=envelope.get("num_turns"),
            subtype=envelope.get("subtype"),
        )
        if envelope.get("is_error"):
            raise ValueError(
                f"the agent reported an error ({envelope.get('subtype')}): "
                f"{str(envelope.get('result') or '')[:300]}"
            )
        data = envelope.get("structured_output")
        if not isinstance(data, dict):
            data = parse_json_text(envelope.get("result") or "")
        if not isinstance(data, dict):
            raise ValueError("the agent did not return a JSON object")
        meta["ok"] = True
    except (OSError, ValueError) as exc:
        meta["error"] = str(exc)
    write_json(QA_OUT / f"{pass_name}.meta.json", meta)
    if data is not None:
        write_json(QA_OUT / f"{pass_name}.json", data)
    if not meta["ok"]:
        print(f"::warning::Agent pass '{pass_name}' produced no usable output: {meta['error']}")
    return 0 if meta["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "extract":
        sys.exit(extract_agent_output(sys.argv[2]))
    print("usage: common.py extract plan|review", file=sys.stderr)
    sys.exit(2)
