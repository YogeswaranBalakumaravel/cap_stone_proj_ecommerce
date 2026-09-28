#!/usr/bin/env python3
"""Stage 5: turn the evidence and the agent's verdict into pass or fail. Standard library only.

- Drops every agent claim whose file:line citation doesn't point at real code.
- Computes the metrics, compares them with the thresholds in config.json, and decides each of
  the six checks.
- Only evidence can fail a check: a missed threshold, a surviving probe, or a verified
  high-severity finding.
- Writes .qa-out/report.md (the PR comment), .qa-out/metrics.json and .qa-out/gate_exit_code,
  and emits GitHub annotations. Only checks in blocking_dimensions can fail the job, and only
  in blocking mode.
"""

from __future__ import annotations

import os
import re
import sys
import traceback
import urllib.parse

from common import DIMENSIONS, QA_OUT, RefChecker, as_int, load_config, ratio, read_json, write_json

SEVERITIES = ("high", "medium", "low")
ORDER = {"N/A": 0, "PASS": 1, "CONCERN": 2, "FAIL": 3}
LABEL = {"PASS": "Pass", "CONCERN": "Concern", "FAIL": "Fail", "N/A": "n/a", "NO DATA": "No data"}
BEHAVIOUR_CHANGES = {"new_feature", "behaviour_change", "bug_fix"}
PROBE_DIMENSION = {
    "business_rule": "business_scenarios",
    "integration_contract": "meaningful_tests",
    "boundary": "edge_cases",
    "constant": "edge_cases",
    "error_path": "sunny_rainy",
    "condition": "meaningful_tests",
    "return_value": "meaningful_tests",
    "arithmetic": "meaningful_tests",
}
MARKER = "<!-- test-quality-agent -->"
MAX_FINDINGS_IN_REPORT = 25
MAX_REPORT_CHARS = 60000


# ---------------------------------------------------------------- formatting helpers


def pct(value) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def code(text, table: bool = False) -> str:
    text = str(text if text is not None else "")
    if table:
        text = text.replace("|", "\\|")
    if not text:
        return "*(removed)*"
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def cell(text) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def line_ranges(lines) -> list[tuple[int, int]]:
    """[3, 4, 5, 9] -> [(3, 5), (9, 9)], treating a gap of one line as continuous."""
    out = []
    for n in sorted(lines):
        if out and n - out[-1][1] <= 2:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def span(a: int, b: int) -> str:
    return str(a) if a == b else f"{a}-{b}"


def location(file, line, head_sha: str, table: bool = False) -> str:
    label = f"{file}:{line}" if line else str(file)
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and head_sha and file:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        url = f"{server}/{repo}/blob/{head_sha}/{urllib.parse.quote(str(file))}" + (
            f"#L{line}" if line else ""
        )
        return f"[{code(label, table)}]({url})"
    return code(label, table)


def annotate(level: str, finding: dict) -> None:
    def data(s):
        return str(s).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    def prop(s):
        return data(s).replace(":", "%3A").replace(",", "%2C")

    title = prop(f"Test quality: {DIMENSIONS.get(finding['dimension'], finding['dimension'])}")
    line = f",line={finding['line']}" if finding.get("line") else ""
    print(f"::{level} file={prop(finding['file'])}{line},title={title}::{data(finding['title'])}")


# ---------------------------------------------------------------- inputs


def validated_review(review, refs: RefChecker) -> dict:
    r = review if isinstance(review, dict) else {}

    def rows(key):
        value = r.get(key)
        return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []

    def tests_of(value):
        out = []
        for t in value if isinstance(value, list) else []:
            if isinstance(t, dict):
                rel = refs.check(
                    t.get("file"), t.get("line"), str(t.get("name") or ""), want="test"
                )
                if rel:
                    out.append(
                        {
                            "file": rel,
                            "line": as_int(t.get("line")),
                            "name": str(t.get("name") or ""),
                        }
                    )
        return out

    tests = []
    for t in rows("tests"):
        rel = refs.check(t.get("file"), t.get("line"), str(t.get("name") or ""), want="test")
        if rel:
            tests.append({**t, "file": rel})
    criteria = []
    for c in rows("criteria"):
        found = tests_of(c.get("tests"))
        status = (
            c.get("status") if c.get("status") in ("covered", "partial", "missing") else "missing"
        )
        downgraded = status != "missing" and not found
        criteria.append(
            {
                **c,
                "tests": found,
                "status": "missing" if downgraded else status,
                "downgraded": downgraded,
            }
        )
    units = []
    for u in rows("changed_units"):
        rel = refs.check(u.get("file"), u.get("line"))
        if rel:
            units.append({**u, "file": rel, "tests": tests_of(u.get("tests"))})
    error_paths = []
    for e in rows("error_paths"):
        rel = refs.check(e.get("file"), e.get("line"))
        if rel:
            error_paths.append(
                {**e, "file": rel, "negative_tests": tests_of(e.get("negative_tests"))}
            )
    boundaries = []
    for b in rows("boundaries"):
        rel = refs.check(b.get("file"), b.get("line"))
        if rel:
            found = tests_of(b.get("tests"))
            boundaries.append(
                {**b, "file": rel, "tests": found, "tested": bool(b.get("tested")) and bool(found)}
            )
    findings = []
    for f in rows("findings"):
        if f.get("dimension") not in DIMENSIONS or f.get("severity") not in SEVERITIES:
            refs.rejected.append(
                {
                    "file": f.get("file"),
                    "line": f.get("line"),
                    "name": "",
                    "reason": "finding has an unknown dimension or severity",
                }
            )
            continue
        rel = refs.check(f.get("file"), f.get("line"))
        if rel:
            findings.append(
                {
                    "dimension": f["dimension"],
                    "severity": f["severity"],
                    "file": rel,
                    "line": as_int(f.get("line")),
                    "title": str(f.get("title") or "").strip(),
                    "evidence": str(f.get("evidence") or "").strip(),
                    "recommendation": str(f.get("recommendation") or "").strip(),
                    "source": "agent",
                    "probe_ids": [],
                }
            )
    equivalent = {
        str(e.get("probe_id")): str(e.get("reason") or "")
        for e in rows("equivalent_probes")
        if e.get("probe_id")
    }
    dims = r.get("dimensions") if isinstance(r.get("dimensions"), dict) else {}
    limitations = (
        [str(x).strip() for x in r.get("limitations") or [] if str(x).strip()]
        if isinstance(r.get("limitations"), list)
        else []
    )
    confidence = r.get("confidence") if r.get("confidence") in ("high", "medium", "low") else ""
    return {
        "present": bool(r),
        "summary": str(r.get("summary") or "").strip(),
        "dimensions": dims,
        "tests": tests,
        "criteria": criteria,
        "units": units,
        "error_paths": error_paths,
        "boundaries": boundaries,
        "findings": findings,
        "equivalent": equivalent,
        "limitations": limitations,
        "confidence": confidence,
    }


# ---------------------------------------------------------------- deterministic findings


def probe_findings(probes, equivalent: dict) -> list[dict]:
    """Every surviving agent probe is a finding. Surviving systematic mutants are grouped per line.

    If the agent says a survivor may be equivalent, the finding stays (a person decides) but drops
    to medium, so a disputed probe can't block on its own."""
    out = []
    by_line: dict[tuple, list] = {}
    for p in (probes or {}).get("probes") or []:
        if p.get("status") != "survived":
            continue
        pid = str(p.get("id"))
        disputed = pid in equivalent
        dispute = (
            f" The agent says this may be equivalent: {equivalent[pid] or 'no reason given'}. "
            "Please confirm."
            if disputed
            else ""
        )
        if p.get("origin") == "systematic":
            by_line.setdefault((p.get("file"), as_int(p.get("line"))), []).append((p, dispute))
            continue
        category = p.get("category") or ""
        high = category in ("business_rule", "integration_contract") or bool(
            p.get("related_criteria")
        )
        out.append(
            {
                "dimension": PROBE_DIMENSION.get(category, "meaningful_tests"),
                "severity": "high" if high and not disputed else "medium",
                "file": p.get("file"),
                "line": as_int(p.get("line")),
                "title": f"Probe {pid} survived: {code(p.get('original'))} → "
                f"{code(p.get('replacement'))}",
                "evidence": (
                    "Every test still passes with this line changed. "
                    + str(p.get("rationale") or "")
                    + dispute
                ).strip(),
                "recommendation": "Given the input this line handles, when the code runs, then "
                "assert the behaviour "
                f"this probe breaks, so the test fails if `{p.get('original')}` changes.",
                "source": "probe",
                "probe_ids": [pid],
            }
        )
    for (file, line), group in by_line.items():
        categories = sorted(
            {p.get("category") or "" for p, _ in group},
            key=lambda c: ["boundary", "constant", "condition", "arithmetic"].index(c)
            if c in ("boundary", "constant", "condition", "arithmetic")
            else 9,
        )
        edits = "; ".join(
            f"{code(p.get('original'))} → {code(p.get('replacement'))}" for p, _ in group
        )
        ids = [str(p.get("id")) for p, _ in group]
        out.append(
            {
                "dimension": PROBE_DIMENSION.get(categories[0], "meaningful_tests"),
                "severity": "medium",
                "file": file,
                "line": line,
                "title": f"{'Mutant' if len(ids) == 1 else 'Mutants'} {', '.join(ids)} survived: "
                f"{edits}",
                "evidence": (
                    "Every test still passes with this systematic edit to a changed line."
                    if len(ids) == 1
                    else "Every test still passes with each of these systematic edits to a changed "
                    "line."
                )
                + "".join(d for _, d in group),
                "recommendation": "Given inputs on each side of this line's condition or value, "
                "when the code runs, "
                "then assert the exact result, so any of these edits makes a test fail.",
                "source": "probe",
                "probe_ids": ids,
            }
        )
    return out


def scan_findings(ctx: dict) -> list[dict]:
    """Findings from the static assertion scan (Python tests only)."""
    out = []
    for t in ctx.get("static_scan") or []:
        if t.get("verdict") == "no_assertion":
            out.append(
                {
                    "dimension": "meaningful_tests",
                    "severity": "medium",
                    "file": t["file"],
                    "line": t["line"],
                    "title": f"{t['name']} has no assertion",
                    "source": "scan",
                    "probe_ids": ["static-scan"],
                    "mentions": [t["name"]],
                    "end_line": t.get("end_line"),
                    "evidence": "The test asserts nothing, so it passes whatever the code returns.",
                    "recommendation": "Given the same setup, when the code runs, then assert the "
                    "expected "
                    "result, or the expected error with a raises check.",
                }
            )
        elif t.get("verdict") == "weak_only":
            out.append(
                {
                    "dimension": "trivial_assertions",
                    "severity": "medium",
                    "file": t["file"],
                    "line": t["line"],
                    "title": f"{t['name']} only uses weak assertions",
                    "source": "scan",
                    "probe_ids": ["static-scan"],
                    "mentions": [t["name"]],
                    "end_line": t.get("end_line"),
                    "evidence": (
                        "Its only assertion is a"
                        if t["assertions"] == 1
                        else f"All {t['assertions']} assertions are"
                    )
                    + " not-None, truthiness, isinstance or was-called check"
                    + ("" if t["assertions"] == 1 else "s")
                    + ", which a wrong result would still pass.",
                    "recommendation": "Given the same input, when the code runs, then compare the "
                    "result with "
                    "the exact expected value.",
                }
            )
    return out


def coverage_findings(probes) -> list[dict]:
    """Changed lines that no test executed, grouped into ranges."""
    cov = (probes or {}).get("coverage") or {}
    out = []
    for file, info in (cov.get("files") or {}).items():
        ranges = line_ranges(info.get("missed") or [])
        for a, b in ranges[:5]:
            out.append(
                {
                    "dimension": "change_validation",
                    "severity": "medium",
                    "file": file,
                    "line": a,
                    "title": f"Changed line{'s' if a != b else ''} {span(a, b)} never run by any "
                    "test",
                    "evidence": "The tests ran under a line tracer and never executed "
                    + (
                        "this changed line, so nothing validates it."
                        if a == b
                        else "these changed lines, so nothing validates them."
                    ),
                    "recommendation": "Given an input that reaches this code, when it runs, then "
                    "assert its effect.",
                    "source": "coverage",
                    "probe_ids": ["coverage"],
                }
            )
        if len(ranges) > 5:
            rest = ", ".join(span(a, b) for a, b in ranges[5:])
            out.append(
                {
                    "dimension": "change_validation",
                    "severity": "medium",
                    "file": file,
                    "line": ranges[5][0],
                    "title": f"More changed lines never run: {rest}",
                    "evidence": "",
                    "recommendation": "",
                    "source": "coverage",
                    "probe_ids": ["coverage"],
                }
            )
    return out


def _same_spot(f: dict, df: dict, mentions: list) -> bool:
    """Same file, line and dimension, or the agent's finding names this evidence (lines ±2)."""
    if f["file"] != df["file"] or not f.get("line") or not df.get("line"):
        return False
    text = f"{f.get('title', '')} {f.get('evidence', '')}"
    if df.get("end_line") and df["line"] <= f["line"] <= df["end_line"]:  # inside the same test
        return f["dimension"] == df["dimension"] or any(m.search(text) for m in mentions)
    if any(m.search(text) for m in mentions):
        return abs(f["line"] - df["line"]) <= 2
    return f["line"] == df["line"] and f["dimension"] == df["dimension"]


def merge_findings(agent: list[dict], deterministic: list[dict]) -> list[dict]:
    """Fold each deterministic finding into the agent finding about the same spot, if there is one.

    A folded finding keeps counting towards its own dimension and severity (`folded`), so merging
    for readability never removes a piece of evidence from the decision."""
    merged = [dict(f) for f in agent]
    for df in deterministic:
        mentions = [
            re.compile(rf"\b{re.escape(term)}\b")
            for term in df["probe_ids"] + df.get("mentions", [])
        ]
        match = next(
            (f for f in merged if f.get("source") == "agent" and _same_spot(f, df, mentions)),
            None,
        )
        if match:
            match["probe_ids"] = match.get("probe_ids", []) + df["probe_ids"]
            if df["dimension"] == match["dimension"]:
                if SEVERITIES.index(df["severity"]) < SEVERITIES.index(match["severity"]):
                    match["severity"] = df["severity"]
            else:
                match.setdefault("folded", []).append(
                    {
                        "dimension": df["dimension"],
                        "severity": df["severity"],
                        "title": df["title"],
                        "file": df["file"],
                        "line": df.get("line"),
                    }
                )
        else:
            merged.append(df)
    dim_order = list(DIMENSIONS)
    merged.sort(
        key=lambda f: (
            SEVERITIES.index(f["severity"]),
            dim_order.index(f["dimension"]),
            str(f["file"]),
            f.get("line") or 0,
        )
    )
    return merged


def first_changed_source_line(ctx: dict):
    files = [
        f
        for f in ctx.get("changed_files") or []
        if f.get("category") == "source" and f.get("changed_lines")
    ]
    files.sort(key=lambda f: f.get("status") != "M")
    return (files[0]["path"], files[0]["changed_lines"][0][0]) if files else (None, None)


# ---------------------------------------------------------------- decision


def evaluate(cfg: dict, ctx: dict, plan, probes, rv: dict, agent_off: bool = False) -> dict:
    th = cfg["thresholds"]
    block_severity = set(cfg.get("block_on_severity") or ["high"])
    blocking = set(cfg.get("blocking_dimensions") or [])
    change_type = (plan or {}).get("change_type", "") if isinstance(plan, dict) else ""
    behaviour_units = [u for u in rv["units"] if u.get("behaviour_changed")]
    behaviour = change_type in BEHAVIOUR_CHANGES or bool(behaviour_units)

    deterministic = (
        probe_findings(probes, rv["equivalent"]) + scan_findings(ctx) + coverage_findings(probes)
    )
    changed = ctx.get("changed_files") or []
    source_changed = [
        f for f in changed if f.get("category") == "source" and f.get("status") != "D"
    ]
    tests_changed = [f for f in changed if f.get("category") == "test"]
    if source_changed and not tests_changed and change_type not in ("refactor_only", "non_code"):
        file, line = first_changed_source_line(ctx)
        if file:
            deterministic.append(
                {
                    "dimension": "change_validation",
                    "severity": "high" if behaviour else "medium",
                    "file": file,
                    "line": line,
                    "title": "This PR changes source code but no tests",
                    "evidence": f"{len(source_changed)} source file(s) changed and no test file "
                    "was added or modified.",
                    "recommendation": "Given the new behaviour's input, when the changed code "
                    "runs, then assert the new "
                    "result, in a new or updated test.",
                    "source": "diff",
                    "probe_ids": ["no-test-change"],
                }
            )
    findings = merge_findings(rv["findings"], deterministic)

    # Deterministic: probe and mutant results. Disputed ones still count; the report shows why.
    probe_rows = (probes or {}).get("probes") or []
    counted = [p for p in probe_rows if p.get("status") in ("killed", "timeout", "survived")]
    detected = sum(1 for p in counted if p["status"] in ("killed", "timeout"))
    minimum = int(cfg["probes"].get("min_valid_for_score", 3))
    mutation_score = round(detected / len(counted), 4) if len(counted) >= minimum else None
    by_origin = {}
    for origin in ("agent", "systematic"):
        rows = [p for p in counted if p.get("origin", "agent") == origin]
        by_origin[origin] = (
            sum(1 for p in rows if p["status"] in ("killed", "timeout")),
            len(rows),
        )
    fail_on_base = ((probes or {}).get("fail_on_base") or {}).get("status", "not_applicable")
    cov = (probes or {}).get("coverage") or {}
    diff_coverage = cov.get("diff_coverage")

    must = [c for c in rv["criteria"] if c.get("importance") != "should"]
    must_covered = sum(1 for c in must if c["status"] == "covered")
    must_partial = sum(1 for c in must if c["status"] == "partial")
    units_tested = sum(1 for u in behaviour_units if u["tests"])
    paths_with_negative = sum(1 for e in rv["error_paths"] if e["negative_tests"])
    boundaries_tested = sum(1 for b in rv["boundaries"] if b["tested"])
    negative_tests = sum(1 for t in rv["tests"] if t.get("polarity") in ("negative", "mixed"))

    # Trivial tests: the agent's judgement plus the static scan, counted once per test
    def key(t):
        return (t["file"], str(t.get("name") or "").split("::")[-1].split(".")[-1])

    judged = {key(t): t.get("trivial") is True for t in rv["tests"]}
    scanned = {
        key(t): t.get("verdict") in ("no_assertion", "weak_only")
        for t in ctx.get("static_scan") or []
    }
    all_tests = set(judged) | set(scanned)
    trivial_keys = {k for k in all_tests if judged.get(k) or scanned.get(k)}
    trivial = len(trivial_keys)
    trivial_by_scan_only = sum(1 for k in trivial_keys if scanned.get(k) and not judged.get(k))

    metrics = {
        "mutation_score": mutation_score,
        "probes_counted": len(counted),
        "probes_detected": detected,
        "agent_probes": {"detected": by_origin["agent"][0], "counted": by_origin["agent"][1]},
        "systematic_mutants": {
            "detected": by_origin["systematic"][0],
            "counted": by_origin["systematic"][1],
        },
        "probes_disputed": sum(
            1
            for p in probe_rows
            if p.get("status") == "survived" and str(p.get("id")) in rv["equivalent"]
        ),
        "fail_on_base": fail_on_base,
        "diff_coverage": diff_coverage,
        "coverage_status": cov.get("status", "not_run"),
        "changed_executable_lines": cov.get("changed_executable"),
        "changed_lines_covered": cov.get("covered"),
        "ac_coverage": ratio(must_covered, len(must)),
        "must_criteria": len(must),
        "must_covered": must_covered,
        "must_partial": must_partial,
        "acceptance_criteria_found": bool(ctx.get("acceptance_criteria_found")),
        "negative_path_coverage": ratio(paths_with_negative, len(rv["error_paths"])),
        "error_paths": len(rv["error_paths"]),
        "error_paths_with_negative_test": paths_with_negative,
        "boundary_coverage": ratio(boundaries_tested, len(rv["boundaries"])),
        "boundaries": len(rv["boundaries"]),
        "boundaries_tested": boundaries_tested,
        "trivial_test_ratio": ratio(trivial, len(all_tests)),
        "tests_classified": len(all_tests),
        "tests_trivial": trivial,
        "tests_trivial_scan_only": trivial_by_scan_only,
        "static_scan": {
            v: sum(1 for t in ctx.get("static_scan") or [] if t.get("verdict") == v)
            for v in ("ok", "weak_only", "no_assertion")
        },
        "negative_test_share": ratio(negative_tests, len(rv["tests"])),
        "change_coverage": ratio(units_tested, len(behaviour_units)),
        "behaviour_units": len(behaviour_units),
        "behaviour_units_tested": units_tested,
        "change_type": change_type,
        "source_files_changed": len(source_changed),
        "test_files_changed": len(tests_changed),
    }

    if fail_on_base == "passed" and behaviour and th.get("require_fail_on_base", True):
        file, line = first_changed_source_line(ctx)
        if file:
            findings.insert(
                0,
                {
                    "dimension": "change_validation",
                    "severity": "high",
                    "file": file,
                    "line": line,
                    "title": "No test fails when this PR's source change is reverted",
                    "evidence": "The PR's tests were run against the base-branch version of the "
                    "changed source files "
                    "and all of them passed, so nothing pins the new behaviour.",
                    "recommendation": "Given the input the change affects, when the new code runs, "
                    "then assert the new "
                    "result, so the test fails on the base branch's code.",
                    "source": "probe",
                    "probe_ids": ["change-reverted"],
                },
            )

    dims: dict = {}

    def dim_findings(dim):
        out = []
        for f in findings:
            if f["dimension"] == dim:
                out.append((f["severity"], f))
            out += [(x["severity"], x) for x in f.get("folded", []) if x["dimension"] == dim]
        return out

    def decide(dim, checks, measured, threshold):
        """checks: (state, reason) pairs; the reason explains a FAIL."""
        states = [s for s, _ in checks if s]
        reasons = [r for s, r in checks if s == "FAIL" and r]
        mine = dim_findings(dim)
        blockers = [(s, f) for s, f in mine if s in block_severity]
        if blockers:
            states.append("FAIL")
            for s, f in blockers[:3]:
                reasons.append(f"{s}-severity finding at {f['file']}:{f.get('line')}: {f['title']}")
            if len(blockers) > 3:
                reasons.append(f"{len(blockers) - 3} more blocking findings")
        elif mine:
            states.append("CONCERN")
        agent = (rv["dimensions"].get(dim) or {}) if rv["present"] else {}
        agent = agent if isinstance(agent, dict) else {}
        agent_state = {
            "pass": "PASS",
            "concern": "CONCERN",
            "fail": "CONCERN",  # the agent alone can't fail a check
            "not_applicable": "N/A",
        }.get(agent.get("status"))
        if agent_state:
            states.append(agent_state)
        dims[dim] = {
            "status": max(states, key=ORDER.get) if states else "NO DATA",
            "measured": measured,
            "threshold": threshold,
            "can_block": dim in blocking,
            "reasons": reasons,
            "agent_status": agent.get("status", ""),
            "agent_rationale": str(agent.get("rationale", "")),
            "findings": len(mine),
        }

    def against(value, minimum_value=None, maximum_value=None):
        if value is None:
            return None
        if minimum_value is not None:
            return "PASS" if value >= minimum_value else "FAIL"
        return "PASS" if value <= maximum_value else "FAIL"

    no_review = "n/a (agent off)" if agent_off else "n/a (the review pass didn't run)"

    # 1. Meaningful
    if mutation_score is None:
        measured = (
            f"n/a ({len(counted)} valid probes, at least {minimum} needed)"
            if probes
            else "n/a (the probes didn't run)"
        )
    else:
        parts = [
            f"{name} {d} of {c}"
            for name, (d, c) in (
                ("agent probes", by_origin["agent"]),
                ("systematic mutants", by_origin["systematic"]),
            )
            if c
        ]
        measured = (
            f"{pct(mutation_score)} mutation score "
            f"({detected} of {len(counted)} caught: {', '.join(parts)})"
        )
    decide(
        "meaningful_tests",
        [
            (
                against(mutation_score, th["mutation_score_min"]),
                f"mutation score {pct(mutation_score)} is below {pct(th['mutation_score_min'])}",
            )
        ],
        measured,
        f"≥ {pct(th['mutation_score_min'])}",
    )

    # 2. Business scenarios
    if not rv["present"]:
        state, measured, why = None, no_review, ""
    elif not ctx.get("acceptance_criteria_found"):
        state = "FAIL" if cfg["requirements"].get("require") else "CONCERN"
        measured = "no acceptance criteria provided" + (
            f" ({must_covered} of {len(must)} inferred scenarios covered)" if must else ""
        )
        why = "no acceptance criteria were provided, and config requires them"
    elif not must:
        state, measured, why = "CONCERN", "no must-have criteria identified", ""
    else:
        state = against(must_covered / len(must), th["ac_coverage_min"])
        measured = f"{must_covered} of {len(must)} must-have criteria covered" + (
            f", {must_partial} partly" if must_partial else ""
        )
        missing = [str(c.get("id") or "?") for c in must if c["status"] != "covered"]
        why = (
            f"{len(missing)} must-have {'criterion' if len(missing) == 1 else 'criteria'} without "
            "a verified test "
            f"({', '.join(missing[:6])})"
        )
    decide(
        "business_scenarios",
        [(state, why)],
        measured,
        f"{pct(th['ac_coverage_min'])} of must-have criteria",
    )

    # 3. Sunny and rainy days
    if not rv["present"]:
        state, measured = None, no_review
    elif not rv["error_paths"]:
        state = (
            "CONCERN" if behaviour else None
        )  # a behaviour change with no unhappy path deserves a look
        measured = "the agent listed no error paths" + (
            " for a behaviour change" if behaviour else ""
        )
    else:
        state = against(metrics["negative_path_coverage"], th["negative_path_coverage_min"])
        measured = (
            f"{paths_with_negative} of {len(rv['error_paths'])} error paths have a negative test"
        )
    decide(
        "sunny_rainy",
        [(state, f"{measured} (needs ≥ {pct(th['negative_path_coverage_min'])})")],
        measured,
        f"≥ {pct(th['negative_path_coverage_min'])}",
    )

    # 4. Edge cases
    if not rv["present"]:
        state, measured = None, no_review
    elif not rv["boundaries"]:
        state = "CONCERN" if behaviour else None  # almost every behaviour change has an edge case
        measured = "the agent listed no edge cases" + (
            " for a behaviour change" if behaviour else ""
        )
    else:
        state = against(metrics["boundary_coverage"], th["boundary_coverage_min"])
        measured = f"{boundaries_tested} of {len(rv['boundaries'])} edge cases tested"
    decide(
        "edge_cases",
        [(state, f"{measured} (needs ≥ {pct(th['boundary_coverage_min'])})")],
        measured,
        f"≥ {pct(th['boundary_coverage_min'])}",
    )

    # 5. Trivial assertions (agent judgement + static scan)
    if not all_tests:
        state, measured = None, (no_review if not rv["present"] else "no tests classified")
    else:
        state = against(metrics["trivial_test_ratio"], maximum_value=th["trivial_test_ratio_max"])
        measured = (
            f"{trivial} of {len(all_tests)} tests trivial ({pct(metrics['trivial_test_ratio'])})"
            + (
                f", {trivial_by_scan_only} found only by the static scan"
                if trivial_by_scan_only
                else ""
            )
        )
    decide(
        "trivial_assertions",
        [(state, f"{measured} (allowed ≤ {pct(th['trivial_test_ratio_max'])})")],
        measured,
        f"≤ {pct(th['trivial_test_ratio_max'])} trivial",
    )

    # 6. Change validation: unit mapping + changed-line coverage + change-reverted run
    checks, parts = [], []
    if rv["present"] and behaviour_units:
        checks.append(
            (
                against(units_tested / len(behaviour_units), th["change_coverage_min"]),
                f"{len(behaviour_units) - units_tested} changed unit(s) without a verified test",
            )
        )
        parts.append(f"{units_tested} of {len(behaviour_units)} changed units tested")
    elif rv["present"]:
        parts.append("no behaviour-changing units")
    if diff_coverage is not None:
        minimum_cov = th.get("diff_coverage_min", 0.9)
        checks.append(
            (
                against(diff_coverage, minimum_cov),
                f"only {cov['covered']} of {cov['changed_executable']} changed lines ran during "
                "the tests "
                f"({pct(diff_coverage)}, needs ≥ {pct(minimum_cov)})",
            )
        )
        parts.append(
            f"{pct(diff_coverage)} of changed lines run ({cov['covered']} of "
            f"{cov['changed_executable']})"
        )
    if fail_on_base == "failed":
        checks.append(("PASS", ""))
        parts.append("tests fail with the change reverted")
    elif fail_on_base == "passed":
        hard = behaviour and th.get("require_fail_on_base", True)
        checks.append(
            (
                "FAIL" if hard else "CONCERN",
                "the PR's tests still pass with the source change reverted",
            )
        )
        parts.append("tests still pass with the change reverted")
    elif fail_on_base == "inconclusive":
        parts.append("reverted-change run was inconclusive")
    threshold = (
        f"{pct(th['change_coverage_min'])} of changed units; "
        f"≥ {pct(th.get('diff_coverage_min', 0.9))} of changed lines run"
    )
    if th.get("require_fail_on_base", True):
        threshold += "; tests must fail with the change reverted"
    decide("change_validation", checks, "; ".join(parts) or no_review, threshold)

    return {"dimensions": dims, "findings": findings, "metrics": metrics}


# ---------------------------------------------------------------- report


def render(
    cfg, ctx, plan, probes, rv, result, refs, metas, headline, incomplete, agent_off=False
) -> str:
    head_sha = os.environ.get("HEAD_SHA") or ctx.get("head", "")
    mode = cfg["mode"]
    dims = result["dimensions"]
    out = [MARKER, f"## Test quality agent: {headline}", ""]
    if rv["summary"]:
        out += [rv["summary"][:800], ""]
    if agent_off:
        out += [
            "The agent is off (`QA_PROVIDER=none`), so these results come from the scripts alone: "
            "coverage, "
            "the change-reverted run, systematic mutants and the assertion scan. Checks 2 to 4 "
            "need the agent.",
            "",
        ]
    if mode == "advisory":
        out += [
            "Mode: **advisory**. The checks marked *can block* will fail the job once blocking "
            "mode is on.",
            "",
        ]
    else:
        out += ["Mode: **blocking**. A failed check marked *can block* fails this job.", ""]
    out += ["| Check | Result | Measured | Threshold | Can block |", "|---|---|---|---|---|"]
    for i, (dim, label) in enumerate(DIMENSIONS.items(), 1):
        d = dims[dim]
        out.append(
            f"| {i}. {label} | **{LABEL[d['status']]}** | {cell(d['measured'])} | "
            f"{cell(d['threshold'])} | "
            f"{'yes' if d['can_block'] else 'no'} |"
        )
    out.append("")

    failing = [(dim, d) for dim, d in dims.items() if d["status"] == "FAIL"]
    if failing:
        verb = "Blocks" if mode == "blocking" else "Would block"
        out += ["### Gate reasons", ""]
        for dim, d in sorted(failing, key=lambda x: not x[1]["can_block"]):
            tag = verb if d["can_block"] else "Advisory"
            for reason in d["reasons"] or ["see the findings below"]:
                out.append(f"- **{tag}**: {DIMENSIONS[dim]}: {reason}")
        out.append("")
    if incomplete:
        out += ["**Incomplete run:**", *[f"- {r}" for r in incomplete], ""]

    findings = result["findings"]
    out += [f"### Findings ({len(findings)})", ""]
    if not findings:
        out += ["No findings.", ""]
    shown = findings[:MAX_FINDINGS_IN_REPORT]
    for sev in SEVERITIES:
        group = [f for f in shown if f["severity"] == sev]
        if not group:
            continue
        out += [f"**{sev.capitalize()}**", ""]
        for f in group:
            extra = [pid for pid in f.get("probe_ids", []) if pid]
            confirmed = (
                f" *(confirmed by {', '.join(extra)})*"
                if f.get("source") == "agent" and extra
                else ""
            )
            out.append(
                f"- {location(f['file'], f.get('line'), head_sha)} · {DIMENSIONS[f['dimension']]} "
                "· "
                f"{f['title']}{confirmed}"
            )
            if f.get("evidence"):
                out.append(f"  {f['evidence']}")
            if f.get("recommendation"):
                out.append(f"  **Suggested test:** {f['recommendation']}")
        out.append("")
    if len(findings) > len(shown):
        out += [
            f"...and {len(findings) - len(shown)} more in `metrics.json` in the evidence artifact.",
            "",
        ]

    if rv["limitations"]:
        out += [
            "### Limitations reported by the agent",
            "",
            *[f"- {cell(x)}" for x in rv["limitations"][:10]],
            "",
        ]

    if rv["criteria"]:
        out += [
            "<details><summary>Traceability: acceptance criteria to tests</summary>",
            "",
            "| Criterion | Importance | Status | Tests |",
            "|---|---|---|---|",
        ]
        for c in rv["criteria"]:
            tests = (
                ", ".join(
                    location(t["file"], t["line"], head_sha, True) + f" {code(t['name'], True)}"
                    for t in c["tests"]
                )
                or "none"
            )
            status = c["status"] + (" (cited tests not found)" if c.get("downgraded") else "")
            out.append(
                f"| {cell(c.get('id', ''))}: {cell(c.get('text', ''))[:200]} | "
                f"{cell(c.get('importance', 'must'))} | "
                f"{cell(status)} | {tests} |"
            )
        out += ["", "</details>", ""]

    probe_rows = (probes or {}).get("probes") or []
    if probes:
        out += [
            "<details><summary>"
            "Evidence runs: baseline, coverage, change reverted, probes</summary>",
            "",
        ]
        base = probes.get("baseline") or {}
        out.append(f"- Baseline (PR head): tests **{base.get('outcome', 'not run')}**")
        cov = probes.get("coverage") or {}
        if cov.get("diff_coverage") is not None:
            missed = "; ".join(
                f"{code(f)} {', '.join(span(a, b) for a, b in line_ranges(i['missed']))}"
                for f, i in (cov.get("files") or {}).items()
                if i.get("missed")
            )
            out.append(
                f"- Changed-line coverage: **{cov['covered']} of {cov['changed_executable']}** "
                "changed lines ran "
                f"({pct(cov['diff_coverage'])})" + (f". Not run: {missed}" if missed else "")
            )
        else:
            reason = {
                "not_applicable": "no changed Python source lines",
                "disabled": "turned off in config",
                "no_data": "no coverage data (the test command may not run Python)",
            }.get(cov.get("status"), cov.get("status", "not run"))
            out.append(f"- Changed-line coverage: {reason}")
        fob = probes.get("fail_on_base") or {}
        meaning = {
            "failed": "tests **fail** with the change reverted (good: they pin the change)",
            "passed": "tests **still pass** with the change reverted (nothing pins the change)",
            "inconclusive": "reverted-change run was inconclusive",
            "not_applicable": "no modified source files to revert",
        }
        out.append(f"- Change reverted: {meaning.get(fob.get('status'), fob.get('status'))}")
        for note in probes.get("notes") or []:
            out.append(f"- Note: {note}")
        if probe_rows:
            out += [
                "",
                "| Probe | Origin | Location | Change | Category | Result |",
                "|---|---|---|---|---|---|",
            ]
            for p in probe_rows:
                status = p.get("status", "")
                if status == "survived" and str(p.get("id")) in rv["equivalent"]:
                    reason = rv["equivalent"][str(p.get("id"))] or "no reason"
                    status = f"survived (agent says possibly equivalent: {reason})"
                elif status in ("invalid", "skipped"):
                    status = f"{status}: {p.get('reason', '')}"
                elif p.get("detected_by"):
                    status = f"{status} ({p['detected_by']})"
                out.append(
                    f"| {cell(p.get('id'))} | {cell(p.get('origin', 'agent'))} | "
                    f"{location(p.get('file'), p.get('line'), head_sha, True)} | "
                    f"{code(p.get('original'), True)} → {code(p.get('replacement'), True)} | "
                    f"{cell(p.get('category'))} | {cell(status)} |"
                )
        out += ["", "</details>", ""]

    if refs.rejected:
        out += [
            "<details><summary>Agent claims dropped because the citation didn't check out "
            f"({len(refs.rejected)})</summary>",
            "",
        ]
        for r in refs.rejected[:20]:
            out.append(
                f"- {code(r.get('file'))}:{r.get('line')} "
                f"{code(r['name']) if r.get('name') else ''}: {r['reason']}"
            )
        out += ["", "</details>", ""]

    th = cfg["thresholds"]
    out += [
        "<details><summary>How the metrics are calculated</summary>",
        "",
        "- **Mutation score** = (killed + timed out) ÷ (killed + timed out + survived), over the "
        "agent's probes and "
        "the systematic mutants. Survivors the agent disputes as equivalent still count, and are "
        "flagged for a "
        "person to confirm.",
        "- **Criteria coverage** = must-have criteria with a verified test ÷ must-have criteria.",
        "- **Negative-path coverage** = error paths with a verified negative test ÷ error paths.",
        "- **Edge-case coverage** = applicable edge cases with a verified test ÷ applicable edge "
        "cases.",
        "- **Trivial-test ratio** = tests flagged trivial (by the agent or the static assertion "
        "scan) ÷ tests classified.",
        "- **Change coverage** = behaviour-changing units with a verified test ÷ "
        "behaviour-changing units.",
        "- **Changed-line coverage** = changed executable lines the tests ran ÷ changed executable "
        "lines "
        "(Python, measured with the built-in tracer).",
        "- **Change reverted**"
        + (
            ": the PR's tests must fail when its source change is reverted."
            if th.get("require_fail_on_base", True)
            else ": reported, not required."
        ),
        "- A citation is verified when the file exists, the line is in range and, for tests, the "
        "test name "
        "is in a test file. Claims that fail this are dropped before anything is counted.",
        "",
        "</details>",
        "",
    ]

    cost = sum(m.get("cost_usd") or 0 for m in metas if m)
    turns = [
        f"{m['pass']} {m.get('turns') or 0} turns" for m in metas if m and not m.get("disabled")
    ]
    footer = [
        f"Agent: {'off' if agent_off else (', '.join(turns) or 'did not run')}"
        + (f", ${cost:.2f}" if cost else "")
    ]
    if rv["confidence"]:
        footer.append(f"agent confidence: {rv['confidence']}")
    footer += [
        f"citations verified: {refs.valid} of {refs.total}",
        "full evidence in the `test-quality-evidence` artifact",
    ]
    out.append("<sub>" + " · ".join(footer) + "</sub>")
    text = "\n".join(out) + "\n"
    if len(text) > MAX_REPORT_CHARS:
        text = text[:MAX_REPORT_CHARS] + "\n\n*(report truncated, see the evidence artifact)*\n"
    return text


# ---------------------------------------------------------------- main


def main() -> int:
    cfg = load_config()
    ctx = read_json(QA_OUT / "context.json", {}) or {}
    plan = read_json(QA_OUT / "plan.json")
    probes = read_json(QA_OUT / "probes.json")
    metas = [read_json(QA_OUT / f"{p}.meta.json") for p in ("plan", "review")]
    refs = RefChecker(cfg)
    rv = validated_review(read_json(QA_OUT / "review.json"), refs)
    agent_off = any(m and m.get("disabled") for m in metas)
    result = evaluate(cfg, ctx, plan, probes, rv, agent_off)

    incomplete = []
    errors = {
        m["pass"]: m.get("error") for m in metas if m and m.get("error") and not m.get("disabled")
    }
    if not isinstance(plan, dict) and not agent_off:
        incomplete.append(
            "The planning pass produced no usable output, so only systematic mutants were run."
            + (f" Cause: {errors['plan']}" if errors.get("plan") else "")
        )
    if not probes:
        incomplete.append("The probe step didn't run.")
    elif probes.get("status") == "baseline_failed":
        incomplete.append(
            "The tests fail on the PR head, so the evidence runs were skipped. Fix the failing "
            "tests first."
        )
    if probes and probes.get("workspace_restored") is False:
        incomplete.append("The probe step couldn't confirm the workspace was restored.")
    if not rv["present"] and not agent_off:
        incomplete.append(
            "The review pass produced no usable output, so only the scripts' evidence was used."
            + (f" Cause: {errors['review']}" if errors.get("review") else "")
        )

    dims = result["dimensions"]
    blocking_fail = [d for d, v in dims.items() if v["can_block"] and v["status"] == "FAIL"]
    mode = cfg["mode"]
    if blocking_fail:
        headline = "Blocked" if mode == "blocking" else "Would block (advisory mode)"
    elif incomplete:
        headline = "Incomplete"
    elif any(v["status"] in ("FAIL", "CONCERN") for v in dims.values()):
        headline = "Passed with findings"
    else:
        headline = "Passed"
    if agent_off:
        headline += " (scripts only, agent off)"
    if ctx.get("full_audit"):
        headline += ", full-project audit"
    exit_code = (
        1
        if mode == "blocking" and (blocking_fail or (incomplete and cfg.get("fail_closed")))
        else 0
    )

    report = render(
        cfg, ctx, plan, probes, rv, result, refs, metas, headline, incomplete, agent_off
    )
    (QA_OUT / "report.md").write_text(report, encoding="utf-8")
    write_json(
        QA_OUT / "metrics.json",
        {
            "headline": headline,
            "mode": mode,
            "exit_code": exit_code,
            "blocking_failures": blocking_fail,
            "incomplete": incomplete,
            "dimensions": dims,
            "metrics": result["metrics"],
            "citations": {
                "total": refs.total,
                "verified": refs.valid,
                "validity": ratio(refs.valid, refs.total),
                "rejected": refs.rejected,
            },
            "agent": [m for m in metas if m],
            "agent_confidence": rv["confidence"],
            "agent_limitations": rv["limitations"],
            "findings": result["findings"],
            "head": ctx.get("head"),
            "base": ctx.get("merge_base"),
            "pr_number": ctx.get("pr_number"),
        },
    )
    (QA_OUT / "gate_exit_code").write_text(str(exit_code), encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(report.replace(MARKER, "") + "\n")

    for f in result["findings"][:20]:
        if f["severity"] in ("high", "medium") and f.get("file"):
            level = "error" if mode == "blocking" and f["dimension"] in blocking_fail else "warning"
            annotate(level, f)
    print(f"Gate: {headline}. " + ", ".join(f"{d}={v['status']}" for d, v in dims.items()))
    return exit_code


if __name__ == "__main__":
    try:
        main()
    except Exception:  # never leave the pipeline without a result
        detail = traceback.format_exc()
        print(detail, file=sys.stderr)
        try:
            cfg = load_config()
            code_ = 1 if cfg["mode"] == "blocking" and cfg.get("fail_closed") else 0
        except Exception:
            code_ = 0
        QA_OUT.mkdir(parents=True, exist_ok=True)
        (QA_OUT / "report.md").write_text(
            f"{MARKER}\n## Test quality agent: gate error\n\nThe gate script failed. "
            f"See the job log.\n\n```\n{detail[-3000:]}\n```\n",
            encoding="utf-8",
        )
        (QA_OUT / "gate_exit_code").write_text(str(code_), encoding="utf-8")
        print(f"::error::Test quality gate crashed: {detail.splitlines()[-1]}")
    sys.exit(0)  # the "Enforce the gate" step reads gate_exit_code
