"""Turn the findings and the agent's advice into one report. Python standard library only.

The verdict comes from the deterministic findings; the agent's advice is added beside it, with
every citation checked (a file that doesn't exist, or a line past its end, is dropped).
"""

from __future__ import annotations

import json
import re

from ob_common import GATE_FILES, OB_OUT, REPO, TARGET_AGENT, read_json

MARKER = "<!-- gating-onboarding -->"
ICON = {"blocker": "🛑", "warning": "⚠️", "info": "ℹ️"}
VERDICT = {
    "not_ready": "🛑 **Not ready**: at least one gate would be skipped or pass without checking.",
    "ready_after_changes": "⚠️ **Ready after changes**: every gate runs, but some check less "
    "than they should or aren't enforced.",
    "ready": "✅ **Ready**: every gate runs and checks what it should.",
}


def _line_count(path: str) -> int | None:
    p = (REPO / path).resolve()
    if REPO not in p.parents or not p.is_file():
        return None
    return sum(1 for _ in p.open(encoding="utf-8", errors="replace"))


def verify(evidence: list[dict]) -> list[dict]:
    """Keep only citations of real lines in real repository files."""
    kept = []
    for e in evidence or []:
        n = _line_count(str(e.get("file", "")))
        line = e.get("line")
        if n is not None and isinstance(line, int) and 1 <= line <= n:
            kept.append({"file": e["file"], "line": line})
    return kept


def _cite(evidence: list[dict]) -> str:
    return ", ".join(f"`{e['file']}:{e['line']}`" if e.get("line") else f"`{e['file']}`"
                     for e in evidence)


def gate_table(inv: dict, findings: list[dict]) -> list[str]:
    files = {w["file"] for w in inv["workflows"]}
    rows = ["| Gate | Installed | Status |", "|---|---|---|"]
    for file, label in GATE_FILES.items():
        mine = [f for f in findings if label in f["gate"] or f["gate"] == "all"]
        sev = {f["severity"] for f in mine}
        if file not in files:
            status = "not installed"
        elif "blocker" in sev:
            status = "🛑 will skip or pass unchecked: " + ", ".join(
                f["id"] for f in mine if f["severity"] == "blocker")
        elif "warning" in sev:
            status = "⚠️ runs, checks less: " + ", ".join(
                f["id"] for f in mine if f["severity"] == "warning")
        else:
            status = "✅ ready"
        rows.append(f"| {label} | {'yes' if file in files else 'no'} | {status} |")
    return rows


def suggested_config(inv: dict) -> dict:
    """The test quality agent's settings for this repo, where they differ from the current ones."""
    cfg = read_json(TARGET_AGENT / "config.json", {}) or {}
    suggestion = inv["tests"].get("suggested") or {}
    out = {}
    for key in ("command", "targeted_command"):
        if suggestion.get(key) and suggestion[key] != (cfg.get("tests") or {}).get(key):
            out.setdefault("tests", {})[key] = suggestion[key]
    for key in ("source_globs", "test_globs"):
        if suggestion.get(key) and suggestion[key] != (cfg.get("paths") or {}).get(key):
            out.setdefault("paths", {})[key] = suggestion[key]
    if not cfg.get("fail_closed"):
        out["fail_closed"] = True
    return out


def build(inv: dict, findings: list[dict], verdict: str, advice: dict | None,
          advice_meta: dict | None) -> str:
    counts = {s: sum(f["severity"] == s for f in findings) for s in ICON}
    langs = ", ".join(f"{k} ({v})" for k, v in list(inv["languages"].items())[:4]) or "none found"
    out = [
        MARKER,
        "## Gating onboarding report",
        "",
        VERDICT[verdict],
        "",
        f"{counts['blocker']} blocker(s), {counts['warning']} warning(s), {counts['info']} note(s)."
        f" Repository: languages {langs}; test frameworks "
        f"{', '.join(inv['tests']['frameworks']) or 'none found'}; long-lived branches "
        f"{', '.join(inv['branches']['long_lived']) or 'none found'} (default "
        f"`{inv['branches']['default'] or '?'}`).",
        "",
        "### Gates",
        "",
        *gate_table(inv, findings),
        "",
    ]
    for sev, heading in (("blocker", "Blockers: fix these first"),
                         ("warning", "Warnings"), ("info", "Notes")):
        items = [f for f in findings if f["severity"] == sev]
        if not items:
            continue
        out += [f"### {heading}", ""]
        for f in items:
            ev = f" ({_cite(f['evidence'])})" if f.get("evidence") else ""
            out += [f"#### {ICON[sev]} {f['id']} · {f['gate']} · {f['title']}{ev}", "",
                    f"**What happens:** {f['what_happens']}", "", f"**Fix:** {f['fix']}", ""]
    suggestion = suggested_config(inv)
    if suggestion:
        out += ["### Suggested `.qa/agent/config.json` changes", "",
                "Merge these keys into the existing file:", "", "```json",
                json.dumps(suggestion, indent=2), "```", ""]
    out += agent_section(advice, advice_meta)
    return "\n".join(out).rstrip() + "\n"


def agent_section(advice: dict | None, meta: dict | None) -> list[str]:
    if not advice:
        why = ((meta or {}).get("error") or "the agent didn't run").rstrip(".")
        return ["### Agent review", "", f"Not available: {why}. The findings above come from "
                "the deterministic checks alone.", ""]
    out = ["### Agent review", "", f"**Agent verdict:** `{advice.get('verdict')}` "
           f"(confidence {advice.get('confidence')}).", "", advice.get("summary", ""), ""]
    disputed = [r for r in advice.get("finding_reviews") or [] if not r.get("agree")]
    if disputed:
        out += ["**Findings the agent disputes:**", ""]
        out += [f"- {r['id']}: {r.get('note', '')}" for r in disputed]
        out += [""]
    for title, key in (("Adjustments", "adjustments"), ("Further skips found", "extra_skips")):
        items = advice.get(key) or []
        if not items:
            continue
        out += [f"#### {title}", ""]
        for i, a in enumerate(items, 1):
            ev = verify(a.get("evidence"))
            dropped = len(a.get("evidence") or []) - len(ev)
            head = a.get("title", "")
            if key == "adjustments":
                head += f" · `{a.get('file')}`"
                if a.get("resolves"):
                    head += f" · resolves {', '.join(a['resolves'])}"
            else:
                head += f" · {a.get('gate', '')}"
            out += [f"{i}. **{head}**" + (f" ({_cite(ev)})" if ev else "")]
            body = a.get("why") if key == "adjustments" else a.get("what_happens")
            out += [f"   {body}"]
            if key == "extra_skips":
                out += [f"   **Fix:** {a.get('fix', '')}"]
            snippet = a.get("snippet")
            if snippet:
                lang = "json" if str(a.get("file", "")).endswith(".json") else (
                    "yaml" if re.search(r"\.ya?ml$", str(a.get("file", ""))) else "")
                out += ["", f"   ```{lang}", *[f"   {ln}" for ln in snippet.splitlines()],
                        "   ```"]
            if dropped:
                out += [f"   _{dropped} citation(s) didn't point at a real line and were dropped._"]
            out += [""]
    if advice.get("questions"):
        out += ["#### Questions for the repository owner", ""]
        out += [f"- {q}" for q in advice["questions"]] + [""]
    return out


def write(inv: dict, findings: list[dict], verdict: str) -> str:
    advice = read_json(OB_OUT / "advise.json")
    meta = read_json(OB_OUT / "advise.meta.json")
    text = build(inv, findings, verdict, advice if isinstance(advice, dict) else None, meta)
    (OB_OUT / "report.md").write_text(text, encoding="utf-8")
    return text
