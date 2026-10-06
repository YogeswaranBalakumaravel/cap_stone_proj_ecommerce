#!/usr/bin/env python3
"""Shared helpers for the code review agent. Python standard library only."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from functools import cached_property
from pathlib import Path

CR_HOME = Path(__file__).resolve().parent
REPO = Path(os.environ.get("CR_REPO") or os.getcwd()).resolve()
CR_OUT = (REPO / os.environ.get("CR_OUT", ".cr-out")).resolve()
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's well-known empty tree

SEVERITIES = ("blocker", "high", "medium", "low", "info")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
AREAS = ("backend", "frontend", "test", "cross_layer", "pr")
STDLIB = set(sys.stdlib_module_names)

# Where untrusted input enters (used to corroborate taint claims and by the local taint check).
REQUEST_SOURCE = re.compile(
    r"\brequest\.(args|form|values|json|data|files|cookies|headers|get_json|get_data|view_args|stream)"
    r"|\binput\(|\bsys\.argv|\bos\.environ\b"
    r"|\blocation\.(hash|search|href|pathname)|document\.(cookie|referrer|URL)|URLSearchParams"
    r"|\.value\b|event\.data|\.innerText|\.responseText|await\s+\w+\.(json|text)\("
)
IGNORE = re.compile(r"review:\s*ignore\[([A-Z0-9, ]+)\]\s*(.*)")


def load_config() -> dict:
    cfg = json.loads((CR_HOME / "config.json").read_text(encoding="utf-8"))
    mode = os.environ.get("CODE_REVIEW_MODE", "").strip().lower()
    if mode:
        cfg["mode"] = mode
    if cfg.get("mode") not in ("advisory", "blocking"):
        raise SystemExit(f"config.json: mode must be 'advisory' or 'blocking', got {cfg['mode']!r}")
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


def full_audit() -> bool:
    return os.environ.get("CR_FULL_AUDIT", "").strip().lower() in ("1", "true", "yes")


def base_ref() -> str:
    if os.environ.get("CR_BASE_REF"):
        return os.environ["CR_BASE_REF"]
    if os.environ.get("GITHUB_BASE_REF"):
        return f"origin/{os.environ['GITHUB_BASE_REF']}"
    return "origin/main"


def merge_base() -> str:
    if full_audit():
        return EMPTY_TREE
    return git("merge-base", "HEAD", base_ref()).strip()


def match(path: str, globs) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in globs or [])


def classify(path: str, cfg: dict) -> str:
    """'test', 'frontend', 'backend', 'excluded' or 'other'. In the globs, * also matches /."""
    p = cfg["paths"]
    if match(path, p.get("exclude_globs")):
        return "excluded"
    if match(path, p.get("test_globs")):
        return "test"
    if match(path, p.get("frontend_globs")):
        return "frontend"
    if match(path, p.get("backend_globs")):
        return "backend"
    return "other"


def repo_path(p) -> str | None:
    """Normalise a path the agent gave us. None if it is empty or escapes the repository."""
    if not isinstance(p, str) or not p.strip() or "\x00" in p:
        return None
    full = (REPO / p.strip().replace("\\", "/")).resolve()
    try:
        rel = full.relative_to(REPO)
    except ValueError:
        return None
    if rel.as_posix() in ("", ".") or rel.parts[0] == ".git":
        return None
    if CR_OUT == full or CR_OUT in full.parents:
        return None
    return rel.as_posix()


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_added_lines(patch: str) -> dict[str, list[int]]:
    """Map each file in a unified diff to the line numbers it adds in the new version."""
    added: dict[str, list[int]] = {}
    current, new_line = None, 0
    for raw in patch.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            current = None if target == "/dev/null" else re.sub(r"^b/", "", target)
            if current is not None:
                added.setdefault(current, [])
            continue
        m = HUNK.match(raw)
        if m:
            new_line = int(m.group(1))
            continue
        if current is None or raw.startswith(("--- ", "diff --git", "index ", "\\")):
            continue
        if raw.startswith("+"):
            added[current].append(new_line)
            new_line += 1
        elif raw.startswith(" "):
            new_line += 1
    return added


def to_ranges(lines) -> list[list[int]]:
    ranges: list[list[int]] = []
    for n in sorted(set(lines)):
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    return ranges


class Ctx:
    """What every check sees: the config, the changed files and a way to read either revision."""

    def __init__(self, cfg: dict, base: str, files: list[dict], pr: dict):
        self.cfg, self.base, self.files, self.pr = cfg, base, files, pr
        self.added = {f["path"]: set(f.get("added", [])) for f in files}
        self._head: dict[str, str | None] = {}
        self._base: dict[str, str | None] = {}

    def changed(self, *areas: str) -> list[dict]:
        return [
            f for f in self.files if f["status"] != "deleted" and (not areas or f["area"] in areas)
        ]

    def head(self, path: str) -> str | None:
        if path not in self._head:
            try:
                self._head[path] = (REPO / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._head[path] = None
        return self._head[path]

    def base_text(self, path: str) -> str | None:
        if path not in self._base:
            if self.base == EMPTY_TREE:
                self._base[path] = None
            else:
                res = subprocess.run(
                    ["git", "show", f"{self.base}:{path}"],
                    cwd=REPO,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self._base[path] = res.stdout if res.returncode == 0 else None
        return self._base[path]

    @cached_property
    def tracked(self) -> list[str]:
        return [p for p in git("ls-files").splitlines() if p and (REPO / p).is_file()]

    @cached_property
    def local_top_modules(self) -> set[str]:
        tops = set()
        for p in self.tracked:
            first = p.split("/")[0]
            if p.endswith(".py"):
                tops.add(Path(first).stem if "/" not in p else first)
        return tops


def ignored(line: str, prev: str, rule: str) -> tuple[bool, str]:
    """`review: ignore[RULE] reason` on the line or the one above. Returns (ignored, reason)."""
    for text in (line, prev):
        m = IGNORE.search(text or "")
        if m and rule in m.group(1).replace(" ", "").split(","):
            return True, m.group(2).strip()
    return False, ""


def finding(
    ctx: Ctx | None,
    check: str,
    rule: str,
    path: str,
    line: int,
    text: str,
    *,
    title: str,
    explanation: str,
    suggestion: str,
    severity: str,
    hard: bool,
    area: str | None = None,
    redact: bool = False,
    pr_caused: bool = False,
    related: dict | None = None,
) -> dict:
    """A script finding. `hard` = definitive: on a BLOCKING row it blocks without attestation."""
    evidence = (text or "").strip()[:160]
    if redact:
        head = re.split(r"[:=]", evidence, maxsplit=1)
        evidence = (head[0].strip()[:60] + " = <redacted>") if len(head) == 2 else "<redacted>"
    return {
        "id": f"{rule}:{path}:{line}",
        "check": check,
        "rule": rule,
        "source": "script",
        "area": area or (classify(path, ctx.cfg) if ctx and path else "pr"),
        "severity": severity,
        "confidence": "high" if hard else "medium",
        "hard": hard,
        "file": path,
        "line": line,
        "evidence": evidence,
        "title": title,
        "explanation": explanation,
        "suggestion": suggestion,
        "pr_caused": pr_caused,
        "related": related,
    }


def line_of(text: str, n: int) -> str:
    lines = text.splitlines()
    return lines[n - 1] if 1 <= n <= len(lines) else ""


def parse_json_text(text: str):
    """Pull a JSON object out of model text (plain, fenced, or surrounded by prose)."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidates = ([fenced.group(1)] if fenced else []) + [
        text,
        text[text.find("{") : text.rfind("}") + 1],
    ]
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
    meta = {"pass": pass_name, "ok": False, "error": "", "cost_usd": None, "turns": None}
    data = None
    try:
        raw = (CR_OUT / f"{pass_name}.envelope.json").read_text(encoding="utf-8").strip()
        envelope = json.loads(raw) if raw else None
        if isinstance(envelope, list):
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
        meta.update(cost_usd=envelope.get("total_cost_usd"), turns=envelope.get("num_turns"))
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
    write_json(CR_OUT / f"{pass_name}.meta.json", meta)
    if data is not None:
        write_json(CR_OUT / f"{pass_name}.json", data)
    if not meta["ok"]:
        print(f"::warning::Agent pass '{pass_name}' produced no usable output: {meta['error']}")
    return 0 if meta["ok"] else 1
