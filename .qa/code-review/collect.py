#!/usr/bin/env python3
"""Stage 1: understand the PR mechanically, and run the ten checklist checks. Standard library only.

Never runs the PR's code: files are read as text and Python is parsed with `ast`, not imported.

Writes to .cr-out/:
    context.json        the ask (PR text, acceptance criteria with ids, Jira), changed files by area
                        with added-line ranges, a map of the existing code, and the script findings
    index.json          the full index of the existing code (repo_index.py)
    oracle_input.json   criteria + public signatures only, for the separate-context oracle pass
    diff.patch          the PR's diff for reviewable files (truncated to agent.max_diff_chars)
"""

from __future__ import annotations

import ast
import base64
import glob
import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request

import repo_index
from checks import ALL
from checks import _ast as pyast
from cr_common import (
    CR_OUT,
    REPO,
    SEV_RANK,
    Ctx,
    base_ref,
    classify,
    full_audit,
    git,
    ignored,
    line_of,
    load_config,
    merge_base,
    parse_added_lines,
    to_ranges,
    write_json,
)

BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.*\S)")


def section(body: str, heading_regex: str) -> list[str]:
    heading = re.compile(rf"^\s*#{{1,6}}\s*.*({heading_regex})", re.I)
    out, inside = [], False
    for line in (body or "").splitlines():
        if re.match(r"^\s*#{1,6}\s", line):
            inside = bool(heading.match(line))
            continue
        m = BULLET.match(line) if inside else None
        if m:
            out.append(m.group(1).strip())
    return out


def criteria(items: list[str], source: str, start: int = 1) -> list[dict]:
    out = []
    for i, text in enumerate(items, start):
        m = re.match(r"^((?:AC|[A-Z][A-Z0-9]+)-\d+)\s*[:.)-]\s*(.*)$", text)
        cid, body = (m.group(1), m.group(2).strip()) if m else (f"AC-{i}", text)
        if body:
            out.append({"id": cid, "text": body, "source": source})
    return out


def adf_lines(node, out=None) -> list[str]:
    """Flatten Atlassian Document Format (Jira v3) to lines, keeping headings and list items."""
    out = [] if out is None else out
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "text":
            if out:
                out[-1] += node.get("text", "")
            else:
                out.append(node.get("text", ""))
            return out
        if kind == "heading":
            out.append("#" * int((node.get("attrs") or {}).get("level", 2)) + " ")
        elif kind == "listItem":
            out.append("- ")
        elif kind in ("paragraph", "codeBlock") and not (out and out[-1] in ("- ",)):
            out.append("")
        for child in node.get("content", []) or []:
            adf_lines(child, out)
    elif isinstance(node, list):
        for child in node:
            adf_lines(child, out)
    return out


def jira(key: str, cfg: dict) -> tuple[str, list[dict]]:
    base, token, email = (
        os.environ.get("JIRA_BASE_URL", "").rstrip("/"),
        os.environ.get("JIRA_API_TOKEN", ""),
        os.environ.get("JIRA_EMAIL", ""),
    )
    if not (base and token and key):
        return "", []
    fields = ["summary", "description"] + (
        [cfg["requirements"]["jira_ac_field"]] if cfg["requirements"].get("jira_ac_field") else []
    )
    req = urllib.request.Request(
        f"{base}/rest/api/3/issue/{urllib.parse.quote(key)}?fields={','.join(fields)}",
        headers={"Accept": "application/json"},
    )
    if email:
        req.add_header(
            "Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        )
    else:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            values = json.load(resp).get("fields") or {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"::warning::Could not read {key} from Jira: {exc}")
        return "", []
    text_parts = [f"Summary: {values.get('summary') or ''}"]
    found: list[dict] = []
    for name in fields[1:]:
        value = values.get(name)
        if not value:
            continue
        text = value if isinstance(value, str) else "\n".join(adf_lines(value))
        text_parts.append(text)
        items = (
            section(text, cfg["requirements"]["ac_heading_regex"])
            if name == "description"
            else [m.group(1) for ln in text.splitlines() if (m := BULLET.match(ln))]
        )
        found += criteria(items, f"jira:{key}", len(found) + 1)
    return "\n\n".join(text_parts), found


def changed_files(cfg: dict, base: str) -> list[dict]:
    files = []
    churn = {}
    for row in git("diff", "--numstat", "-M", base, "HEAD").splitlines():
        a, d, *rest = row.split("\t")
        path = rest[-1].split(" => ")[-1].replace("}", "") if rest else ""
        churn[path] = (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    for row in git("diff", "--name-status", "-M", "--no-color", base, "HEAD").splitlines():
        parts = row.split("\t")
        status, path = parts[0][0], parts[-1]
        entry = {
            "path": path,
            "area": classify(path, cfg),
            "churn": churn.get(path, 0),
            "status": {"A": "added", "D": "deleted", "R": "renamed"}.get(status, "modified"),
        }
        if status == "R":
            entry["old_path"] = parts[1]
        files.append(entry)
    return files


def oracle_input(ctx: Ctx, idx: dict, crit: list[dict]) -> dict:
    """What the separate-context pass may see: the ask and the interface, never a function body."""
    sigs = []
    for f in ctx.changed("backend"):
        if not f["path"].endswith(".py"):
            continue
        tree = pyast.parse(ctx.head(f["path"]), f["path"])
        for fn in pyast.functions(tree) if tree else []:
            if not pyast.touches(fn, ctx.added[f["path"]]):
                continue
            doc = (ast.get_docstring(fn) or "").strip()
            routes = [
                f"{','.join(r['methods'])} {r['rule']}"
                for r in idx["routes"]
                if r["file"] == f["path"] and r["function"] == fn.name
            ]
            sigs.append(
                {
                    "file": f["path"],
                    "signature": repo_index._signature(fn),
                    "doc": doc[:400],
                    "routes": routes,
                }
            )
    return {
        "title": ctx.pr.get("title", ""),
        "criteria": crit,
        "interface": sigs,
        "changed_templates": [f["path"] for f in ctx.changed("frontend")],
    }


def main() -> int:
    cfg = load_config()
    CR_OUT.mkdir(parents=True, exist_ok=True)
    base = merge_base()
    files = changed_files(cfg, base)
    reviewable = [
        f["path"]
        for f in files
        if f["area"] in ("backend", "frontend", "test") and f["status"] != "deleted"
    ]
    patch = (
        git("diff", "-U3", "-M", "--no-color", base, "HEAD", "--", *reviewable)
        if reviewable
        else ""
    )
    added = parse_added_lines(patch)
    for f in files:
        f["added"] = sorted(added.get(f["path"], []))
        f["added_ranges"] = to_ranges(f["added"])

    body = os.environ.get("PR_BODY", "")
    crit = criteria(section(body, cfg["requirements"]["ac_heading_regex"]), "pr")
    for pattern in cfg["requirements"].get("files") or []:
        for path in sorted(glob.glob(str(REPO / pattern), recursive=True)):
            text = open(path, encoding="utf-8", errors="replace").read()
            crit += criteria(
                section(text, cfg["requirements"]["ac_heading_regex"])
                or [m.group(1) for ln in text.splitlines() if (m := BULLET.match(ln))],
                os.path.relpath(path, REPO),
                len(crit) + 1,
            )
    key_rx = re.compile(cfg["requirements"]["jira_key_regex"])
    key_m = key_rx.search(os.environ.get("HEAD_REF", "")) or key_rx.search(
        os.environ.get("PR_TITLE", "")
    )
    jira_text, jira_crit = jira(key_m.group(0), cfg) if key_m else ("", [])
    crit += jira_crit
    try:
        labels = json.loads(os.environ.get("PR_LABELS") or "[]")
    except ValueError:
        labels = []
    pr = {
        "number": os.environ.get("PR_NUMBER") or None,
        "title": os.environ.get("PR_TITLE", ""),
        "body": body,
        "author": os.environ.get("PR_AUTHOR", ""),
        "head_ref": os.environ.get("HEAD_REF", ""),
        "labels": labels,
        "jira_key": key_m.group(0) if key_m else None,
        "jira_text": jira_text,
        "acceptance_criteria": crit,
        "declared_scope": [
            s.strip("` ") for s in section(body, cfg["requirements"]["scope_heading_regex"])
        ],
    }

    ctx = Ctx(cfg, base, files, pr)
    idx = repo_index.build(ctx)
    write_json(CR_OUT / "index.json", idx)

    found, errors = [], []
    for module in ALL:
        try:
            found += module.run(ctx, idx)
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
            errors.append(f"{module.__name__}: {exc}")
            print(f"::warning::Check {module.__name__} failed: {exc}")
            traceback.print_exc()

    # Suppressions, then de-duplication (same rule on the same line once; keep the worst).
    best: dict[tuple, dict] = {}
    suppressed = []
    for fnd in found:
        if fnd["file"] and fnd["line"]:
            text = ctx.head(fnd["file"]) or ""
            skip, reason = ignored(
                line_of(text, fnd["line"]), line_of(text, fnd["line"] - 1), fnd["rule"]
            )
            if skip and reason:
                suppressed.append({**fnd, "suppressed": reason})
                continue
            if skip:
                fnd["explanation"] += " (A review: ignore without a reason isn't honoured.)"
        key = (fnd["rule"], fnd["file"], fnd["line"])
        if key not in best or SEV_RANK[fnd["severity"]] < SEV_RANK[best[key]["severity"]]:
            best[key] = fnd
    findings = sorted(
        best.values(), key=lambda f: (SEV_RANK[f["severity"]], f["check"], f["file"], f["line"])
    )

    limit = int(cfg["agent"].get("max_diff_chars", 150000))
    (CR_OUT / "diff.patch").write_text(
        patch[:limit]
        + ("\n\n[diff truncated: read the files directly]\n" if len(patch) > limit else ""),
        encoding="utf-8",
    )
    write_json(CR_OUT / "oracle_input.json", oracle_input(ctx, idx, crit))
    areas = {
        a: sum(1 for f in files if f["area"] == a and f["status"] != "deleted")
        for a in ("backend", "frontend", "test")
    }
    context = {
        "pr": pr,
        "full_audit": full_audit(),
        "base": "empty tree (full audit)" if full_audit() else base_ref(),
        "context_files": [p for p in cfg.get("context_files", []) if (REPO / p).is_file()],
        "checks": {k: {"title": v["title"], "row": v["row"]} for k, v in cfg["checks"].items()},
        "files": [{k: v for k, v in f.items() if k != "added"} for f in files],
        "summary": {
            **{f"{a}_files": n for a, n in areas.items()},
            "diff_truncated": len(patch) > limit,
            "criteria": len(crit),
        },
        "existing_code": repo_index.summary(idx),
        "script_findings": findings,
        "suppressed": suppressed,
        "check_errors": errors,
    }
    write_json(CR_OUT / "context.json", context)
    print(
        f"Collected: {areas['backend']} backend, {areas['frontend']} frontend, {areas['test']} "
        "test file(s); "
        f"{len(crit)} criteria; {len(findings)} script finding(s), {len(suppressed)} suppressed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
