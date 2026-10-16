"""Turn each gate's raw output into findings, and findings into one PR comment. Stdlib only.

Every parser returns a list of findings with the same shape:

    {severity, blocking, file, line, title, detail, fix}

`severity` is one of SEVERITIES. `blocking` is True when the finding fails its gate. A parser raises
ValueError when its input isn't the format it expects, so the gate shows as unreadable rather than
as having no findings. All text comes from gate output, which on a fork PR is produced by the PR's
own code, so `render()` escapes it and never lets it open HTML, links or @-mentions. Gitleaks
findings never carry the matched secret, nor do bandit's hardcoded-password findings.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # nosec B405 - a DOCTYPE is refused before parsing (parse_junit)

MARKER = "<!-- gate-findings-commenter -->"
SEVERITIES = ("blocker", "high", "medium", "low", "info")
ICONS = {"blocker": "🛑", "high": "🔴", "medium": "🟠", "low": "🟡", "info": "🔵"}
MAX_ROWS = 25  # findings shown per gate before the rest collapse into a count
MAX_TEXT = 300  # characters kept from any one piece of gate text
MAX_BODY = 60000  # GitHub rejects comments over 65536 characters
# Bandit quotes the literal it found in these checks' issue_text, which may be a real password.
BANDIT_SECRET_TESTS = {"B105", "B106", "B107"}
ZWSP = "​"  # zero-width space: breaks autolinks without changing how the text reads

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


def repo_path(path) -> str | None:
    """A repo-relative path: no file:// scheme, runner checkout prefix, ./ or backslashes."""
    path = str(path or "").replace("\\", "/")
    path = re.sub(r"^file://", "", path)
    path = re.sub(r"^/home/runner/work/[^/]+/[^/]+/", "", path)
    return path.removeprefix("./") or None


def finding(severity, blocking, file, line, title, detail="", fix="") -> dict:
    sev = str(severity or "").lower()
    return {
        "severity": sev if sev in SEVERITIES else "medium",
        "blocking": bool(blocking),
        "file": repo_path(file),
        "line": line if isinstance(line, int) and not isinstance(line, bool) and line > 0 else None,
        "title": str(title or "").strip(),
        "detail": str(detail or "").strip(),
        "fix": str(fix or "").strip(),
    }


def _obj(value) -> dict:
    return value if isinstance(value, dict) else {}


def _items(value, what: str) -> list[dict]:
    """The dict entries of a list; anything but a list means the file isn't what we expect."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{what} isn't a list")
    return [v for v in value if isinstance(v, dict)]


def _top(data, what: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError(f"{what} isn't a JSON object")
    return data


# ---- parsers, one per gate output ----


def parse_ruff(data) -> list[dict]:
    """`ruff check --output-format json`: any finding fails the lint step."""
    if not isinstance(data, list):
        raise ValueError("ruff output isn't a JSON list")
    out = []
    for r in _items(data, "ruff output"):
        code = r.get("code") or "syntax"
        fix = _obj(r.get("fix")).get("message") or ""
        if r.get("fix") and not fix:
            fix = "Run `ruff check --fix`."
        out.append(
            finding(
                "high",
                True,
                r.get("filename"),
                _obj(r.get("location")).get("row"),
                f"{code}: {r.get('message', '')}",
                fix=fix,
            )
        )
    return out


def parse_bandit(data) -> list[dict]:
    """`bandit -f json`: the CI step fails on any reported issue."""
    sev = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
    out = []
    for r in _items(_top(data, "bandit output").get("results"), "bandit results"):
        test_id = str(r.get("test_id", ""))
        text = r.get("issue_text", "")
        if test_id in BANDIT_SECRET_TESTS:
            text = "Possible hardcoded password (the value is left out of this comment)."
        extra = []
        if r.get("issue_confidence"):
            extra.append(f"confidence {str(r['issue_confidence']).lower()}")
        cwe = _obj(r.get("issue_cwe")).get("id")
        if cwe:
            extra.append(f"CWE-{cwe}")
        out.append(
            finding(
                sev.get(str(r.get("issue_severity", "")).upper(), "medium"),
                True,
                r.get("filename"),
                r.get("line_number"),
                f"{test_id} {r.get('test_name', '')}".strip(),
                detail=str(text) + (f" ({', '.join(extra)})" if extra else ""),
                fix="Fix the call, or add `# nosec` with a comment saying why it's safe.",
            )
        )
    return out


def parse_junit(text: str) -> list[dict]:
    """pytest `--junitxml`: one finding per failed or errored test."""
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise ValueError("JUnit XML with a DOCTYPE or entity isn't parsed")
    try:
        root = ET.fromstring(text)  # nosec B314 - DOCTYPE refused above
    except ET.ParseError as exc:
        raise ValueError(f"JUnit XML doesn't parse: {exc}") from exc
    out = []
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            parts = case.get("classname", "").split(".")
            # "tests.test_routes" -> tests/test_routes.py; a class name, if any, stays out.
            mod = [p for p in parts if p and not p[:1].isupper()]
            file = repo_path(case.get("file") or ("/".join(mod) + ".py" if mod else None))
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
    for run in _items(_top(data, "SARIF").get("runs"), "SARIF runs"):
        for r in _items(run.get("results"), "SARIF results"):
            locations = _items(r.get("locations"), "SARIF locations")
            loc = _obj((locations or [{}])[0].get("physicalLocation"))
            out.append(
                finding(
                    "blocker",
                    True,
                    _obj(loc.get("artifactLocation")).get("uri"),
                    _obj(loc.get("region")).get("startLine"),
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
    for f in _items(_top(data, "findings.json").get("findings"), "code review findings"):
        status = f.get("status") or "REPORT"
        if status == "ATTESTED":
            continue
        check_id = str(f.get("check") or "")
        check = titles.get(check_id, check_id.replace("_", " "))
        title = f.get("title", "")
        note = {
            "NEEDS_ATTESTATION": " A reviewer other than the author can approve with "
            f"`attest: {check_id}`.",
            "MUST_REVIEW": " Needs a reviewer's look; doesn't block.",
        }.get(status, "")
        out.append(
            finding(
                f.get("severity"),
                status in ("BLOCK", "NEEDS_ATTESTATION"),
                f.get("file"),
                f.get("line"),
                f"[{check}] {title}" if check else title,
                detail=str(f.get("explanation") or "") + note,
                fix=f.get("suggestion"),
            )
        )
    return out


def parse_test_quality(data) -> list[dict]:
    """Test quality agent `metrics.json`. Blocks when its dimension fails and is a blocking one."""
    data = _top(data, "metrics.json")
    dims = _obj(data.get("dimensions"))
    blocking_mode = data.get("mode", "blocking") == "blocking"
    out = []
    for f in _items(data.get("findings"), "test quality findings"):
        d = _obj(dims.get(f.get("dimension")))
        dim = str(f.get("dimension") or "").replace("_", " ")
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
    # Bare URLs autolink on GitHub; a zero-width space after the scheme or "www." stops that.
    text = re.sub(r"(?i)(\w)(://)", rf"\1:{ZWSP}//", text)
    text = re.sub(r"(?i)\bwww\.", f"www{ZWSP}.", text)
    # A zero-width joiner after @ stops GitHub turning @someone into a mention.
    return text.replace("@", "@‍")


def _code(text: str) -> str:
    """Inline code that survives backticks in the text."""
    text = clean(text, 120).replace("`", "'")
    return f"`{text}`" if text else ""


def linkable(path: str | None) -> bool:
    """Only plain repo-relative paths become links to the file."""
    return bool(
        path
        and re.fullmatch(r"[\w.\-]+(/[\w.\-]+)*", path)
        and not any(part in (".", "..") for part in path.split("/"))
    )


def _location(f: dict, blob_url: str) -> str:
    if not f["file"]:
        return "—"
    label = f["file"] + (f":{f['line']}" if f["line"] else "")
    if not blob_url or not linkable(f["file"]):
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


def _counts(gate: dict) -> tuple[str, str]:
    """Finding and blocking counts. Unknown is never shown as 0."""
    if gate.get("status") is None:
        return "—", "—"
    found = gate.get("findings")
    if found is None:
        return "?", "?"
    return str(len(found)), str(sum(1 for f in found if f["blocking"]))


def render(gates: list[dict], sha: str, blob_url: str = "", updated: str = "") -> str:
    """One comment for every gate.

    Each gate is {name, status, conclusion, url, findings, note}. `status` is None when the gate's
    workflow didn't run for this commit. `findings` is None when they aren't known: the gate is
    still running, kept no findings file, or its file couldn't be read.
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
        found, blocks = _counts(g)
        out.append(f"| {name} | {result_label(g)} | {found} | {blocks} |")

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
        "<sub>🚫 = fails the gate. ? = not known yet or couldn't be read. Posted by the gate "
        "findings commenter (`.qa/commenter/`); it edits this comment as gates finish. Each "
        "gate's own report has the full detail.</sub>",
    ]
    body = "\n".join(out)
    if len(body) > MAX_BODY:
        # Cut at a line break so no table row is left half written.
        body = body[:MAX_BODY].rsplit("\n", 1)[0] + "\n\n…truncated. See each gate's run."
    return body
