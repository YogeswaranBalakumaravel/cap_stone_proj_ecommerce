"""Turn each gate's raw output into findings, and findings into one PR comment. Stdlib only.

Every parser returns a list of findings with the same shape:

    {severity, blocking, file, line, title, detail, fix}

`severity` is one of SEVERITIES. `blocking` is True when the finding fails its gate. All text comes
from gate output, which on a fork PR is produced by the PR's own code, so `render()` escapes it and
never lets it open HTML, links or @-mentions. Gitleaks findings never carry the matched secret.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # nosec B405 - parses JUnit XML pytest wrote, no entities

MARKER = "<!-- gate-findings-commenter -->"
SEVERITIES = ("blocker", "high", "medium", "low", "info")
ICONS = {"blocker": "🛑", "high": "🔴", "medium": "🟠", "low": "🟡", "info": "🔵"}
MAX_ROWS = 25  # findings shown per gate before the rest collapse into a count
MAX_TEXT = 300  # characters kept from any one piece of gate text
MAX_BODY = 60000  # GitHub rejects comments over 65536 characters

# Workflow run conclusions, as the comment shows them.
RESULTS = {
    "success": "✅ Passed",
    "failure": "❌ Failed",
    "cancelled": "⚪ Cancelled",
    "timed_out": "❌ Timed out",
    "action_required": "⏸️ Waiting for approval",
    "skipped": "⚪ Skipped",
    "neutral": "⚪ Neutral",
    "stale": "⚪ Stale",
}


def finding(severity, blocking, file, line, title, detail="", fix="") -> dict:
    sev = str(severity or "").lower()
    return {
        "severity": sev if sev in SEVERITIES else "medium",
        "blocking": bool(blocking),
        "file": str(file or "").replace("\\", "/").removeprefix("./") or None,
        "line": line if isinstance(line, int) and line > 0 else None,
        "title": str(title or "").strip(),
        "detail": str(detail or "").strip(),
        "fix": str(fix or "").strip(),
    }


def _rel(path: str, root: str = "") -> str:
    """Strip the runner's checkout path so paths read as repo-relative."""
    path = str(path or "").replace("\\", "/")
    root = root.replace("\\", "/").rstrip("/")
    if root and path.startswith(root + "/"):
        path = path[len(root) + 1 :]
    return re.sub(r"^/home/runner/work/[^/]+/[^/]+/", "", path)


# ---- parsers, one per gate output ----


def parse_ruff(data) -> list[dict]:
    """`ruff check --output-format json`: any finding fails the lint step."""
    out = []
    for r in data if isinstance(data, list) else []:
        code = r.get("code") or "syntax"
        fix = (r.get("fix") or {}).get("message") or ""
        if r.get("fix") and not fix:
            fix = "Run `ruff check --fix`."
        out.append(
            finding(
                "high",
                True,
                _rel(r.get("filename", "")),
                (r.get("location") or {}).get("row"),
                f"{code}: {r.get('message', '')}",
                fix=fix,
            )
        )
    return out


def parse_bandit(data) -> list[dict]:
    """`bandit -f json`: the CI step fails on any reported issue."""
    sev = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
    out = []
    for r in (data or {}).get("results", []) if isinstance(data, dict) else []:
        extra = []
        if r.get("issue_confidence"):
            extra.append(f"confidence {str(r['issue_confidence']).lower()}")
        cwe = (r.get("issue_cwe") or {}).get("id")
        if cwe:
            extra.append(f"CWE-{cwe}")
        detail = r.get("issue_text", "") + (f" ({', '.join(extra)})" if extra else "")
        out.append(
            finding(
                sev.get(str(r.get("issue_severity", "")).upper(), "medium"),
                True,
                _rel(r.get("filename", "")),
                r.get("line_number"),
                f"{r.get('test_id', '')} {r.get('test_name', '')}".strip(),
                detail=detail,
                fix="Fix the call, or add `# nosec` with a comment saying why it's safe.",
            )
        )
    return out


def parse_junit(text: str) -> list[dict]:
    """pytest `--junitxml`: one finding per failed or errored test."""
    try:
        root = ET.fromstring(text)  # nosec B314 - see the import
    except ET.ParseError:
        return []
    out = []
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            classname = case.get("classname", "")
            parts = classname.split(".")
            # "tests.test_routes" -> tests/test_routes.py; a class name, if any, stays out.
            mod = [p for p in parts if not p[:1].isupper()]
            file = case.get("file") or ("/".join(mod) + ".py" if mod else None)
            # The traceback's last "path.py:N: Error" line in this file is where the test failed.
            line = None
            for m in re.finditer(r"(?m)^(\S+\.py):(\d+): ", node.text or ""):
                if file and m.group(1).replace("\\", "/").endswith(file):
                    line = int(m.group(2))
            if line is None and (case.get("line") or "").isdigit():
                line = int(case.get("line")) + 1  # pytest writes the def line, 0-based
            message = (node.get("message") or (node.text or "")).strip().splitlines()
            out.append(
                finding(
                    "blocker",
                    True,
                    file,
                    line,
                    f"{case.get('name', '?')} {'failed' if kind == 'failure' else 'errored'}",
                    detail=message[0] if message else "",
                    fix="Run `pytest " + (f"{file}::" if file else "-k ")
                    + f"{case.get('name', '')}` locally to reproduce.",
                )
            )
    return out


def parse_sarif(data) -> list[dict]:
    """Gitleaks SARIF. Only the rule and location are kept; the snippet holds the secret."""
    out = []
    runs = data.get("runs", []) if isinstance(data, dict) else []
    for run in runs:
        for r in run.get("results", []) or []:
            loc = ((r.get("locations") or [{}])[0].get("physicalLocation") or {})
            out.append(
                finding(
                    "blocker",
                    True,
                    (loc.get("artifactLocation") or {}).get("uri"),
                    (loc.get("region") or {}).get("startLine"),
                    f"Possible secret ({r.get('ruleId', 'unknown rule')})",
                    fix="Remove it, rotate the credential, and purge it from the branch history. "
                    "If it's a false positive, add its fingerprint to `.gitleaksignore`.",
                )
            )
    return out


def parse_code_review(data, titles: dict | None = None) -> list[dict]:
    """Code review agent `findings.json`. Status decides blocking; see .qa/code-review/gate.py.

    `titles` maps check ids to their names in .qa/code-review/config.json.
    """
    titles = titles or {}
    out = []
    for f in (data or {}).get("findings", []) if isinstance(data, dict) else []:
        status = f.get("status") or "REPORT"
        if status == "ATTESTED":
            continue
        check = titles.get(f.get("check"), str(f.get("check") or "").replace("_", " "))
        title = f.get("title", "")
        note = {
            "NEEDS_ATTESTATION": " A reviewer other than the author can approve with "
            f"`attest: {f.get('check', '')}`.",
            "MUST_REVIEW": " Needs a reviewer's look; doesn't block.",
        }.get(status, "")
        out.append(
            finding(
                f.get("severity"),
                status in ("BLOCK", "NEEDS_ATTESTATION"),
                f.get("file"),
                f.get("line"),
                f"[{check}] {title}" if check else title,
                detail=(f.get("explanation") or "") + note,
                fix=f.get("suggestion"),
            )
        )
    return out


def parse_test_quality(data) -> list[dict]:
    """Test quality agent `metrics.json`. Blocks when its dimension fails and is a blocking one."""
    if not isinstance(data, dict):
        return []
    dims = data.get("dimensions") or {}
    blocking_mode = data.get("mode", "blocking") == "blocking"
    out = []
    for f in data.get("findings") or []:
        d = dims.get(f.get("dimension"), {})
        dim = str(f.get("dimension", "")).replace("_", " ")
        out.append(
            finding(
                f.get("severity"),
                blocking_mode and d.get("can_block") and d.get("status") == "FAIL",
                f.get("file"),
                f.get("line"),
                f"[{dim}] {f.get('title', '')}" if dim else f.get("title"),
                detail=f.get("evidence"),
                fix=f.get("recommendation"),
            )
        )
    return out


# ---- rendering ----


def clean(text, limit: int = MAX_TEXT) -> str:
    """Make gate text safe for one Markdown table cell."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")
    # A zero-width joiner after @ stops GitHub turning @someone into a mention.
    return text.replace("@", "@‍")


def _code(text: str) -> str:
    """Inline code that survives backticks in the text."""
    text = clean(text, 120).replace("`", "'")
    return f"`{text}`" if text else ""


def _location(f: dict, blob_url: str) -> str:
    if not f["file"]:
        return "—"
    label = f["file"] + (f":{f['line']}" if f["line"] else "")
    if not blob_url or not re.fullmatch(r"[\w./\-]+", f["file"]) or ".." in f["file"]:
        return _code(label)
    anchor = f"#L{f['line']}" if f["line"] else ""
    return f"[{_code(label)}]({blob_url}/{f['file']}{anchor})"


def sort_key(f: dict):
    return (not f["blocking"], SEVERITIES.index(f["severity"]), f["file"] or "", f["line"] or 0)


def _table(rows: list[dict], blob_url: str) -> list[str]:
    lines = ["| | Severity | Where | Finding | How to fix |", "|---|---|---|---|---|"]
    for f in rows:
        what = f"**{clean(f['title'])}**"
        if f["detail"]:
            what += f"<br>{clean(f['detail'])}"
        lines.append(
            "| {} | {} {} | {} | {} | {} |".format(
                "🚫" if f["blocking"] else "",
                ICONS[f["severity"]],
                f["severity"],
                _location(f, blob_url),
                what,
                clean(f["fix"]) or "—",
            )
        )
    return lines


def result_label(gate: dict) -> str:
    if gate.get("status") is None:
        return "➖ Not run"
    if gate["status"] != "completed":
        return "⏳ Running"
    return RESULTS.get(gate.get("conclusion") or "", f"⚪ {gate.get('conclusion')}")


def render(gates: list[dict], sha: str, blob_url: str = "", updated: str = "") -> str:
    """One comment for every gate.

    Each gate is {name, status, conclusion, url, findings, note}. `status` is None when the gate's
    workflow didn't run for this commit, and `findings` is None when its findings couldn't be read.
    """
    total = sum(len(g["findings"] or []) for g in gates)
    blocking = sum(1 for g in gates for f in g["findings"] or [] if f["blocking"])
    failed = [g["name"] for g in gates if g.get("conclusion") in ("failure", "timed_out")]
    running = [g["name"] for g in gates if g.get("status") not in (None, "completed")]

    if failed:
        headline = f"❌ **{len(failed)} gate(s) failed:** " + ", ".join(failed) + "."
    elif running:
        headline = "⏳ **Gates still running.** This comment updates as each one finishes."
    else:
        headline = "✅ **Every gate that ran passed.**"
    if total:
        headline += f" {total} finding(s), {blocking} blocking."

    out = [
        MARKER,
        "## Gate findings",
        "",
        headline,
        "",
        f"Commit `{clean(sha[:7], 7)}`" + (f" · updated {clean(updated, 40)}" if updated else ""),
        "",
        "| Gate | Result | Findings | Blocking |",
        "|---|---|---|---|",
    ]
    for g in gates:
        name = f"[{g['name']}]({g['url']})" if g.get("url") else g["name"]
        found = g["findings"]
        out.append(
            f"| {name} | {result_label(g)} | {'?' if found is None else len(found)} | "
            f"{'?' if found is None else sum(1 for f in found if f['blocking'])} |"
        )

    for g in gates:
        found = sorted(g["findings"] or [], key=sort_key)
        note = g.get("note") or ""
        if g.get("conclusion") in ("failure", "timed_out") and not found and not note:
            note = "The gate failed without reporting findings. Open the run log for the reason."
        if not found and not note:
            continue
        out += ["", f"### {g['name']}"]
        if note:
            out += ["", clean(note, 500)]
        if found:
            out += [""] + _table(found[:MAX_ROWS], blob_url)
            if len(found) > MAX_ROWS:
                out += ["", f"…and {len(found) - MAX_ROWS} more in the run's evidence."]

    out += [
        "",
        "<sub>🚫 = fails the gate. Posted by the gate findings commenter (`.qa/commenter/`); "
        "it edits this comment as gates finish. Each gate's own report has the full detail.</sub>",
    ]
    body = "\n".join(out)
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY].rsplit("\n", 1)[0] + "\n\n…truncated. See each gate's run."
    return body
