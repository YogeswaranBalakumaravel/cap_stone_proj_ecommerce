#!/usr/bin/env python3
"""Stage 3: verify, decide, report. Python standard library only.

Every agent claim is checked before it counts: the cited file exists in the PR head, the line is
in range, and the quoted evidence really is on (or within `line_tolerance` of) that line.

How a finding is decided, per the checklist row it belongs to:

    BLOCK             BLOCKING row, blocker/high, on a line this PR adds (or breaks), and found by a
                      definitive script rule. Fix it, or suppress it on the line with a reason.
    NEEDS ATTESTATION BLOCKING row, blocker/high, in scope, but a judgement: a script signal or a
                      verified agent finding (confidence medium or high), corroborated or not.
                      Blocks until a reviewer with write access, other than the author, approves
                      with "attest: <check>".
    MUST REVIEW       STANDARD row, high or worse. Shown prominently; doesn't block.
    REPORT            everything else, including ADVISORY rows and low-confidence claims.

So the agent alone can never fail the build: its unverifiable claims are dropped, and its
verified ones wait on a named human, as the checklist requires. Corroboration (a taint source
that really reads input, an existing symbol that really exists) is shown to that human; it raises
confidence, it doesn't make the agent's claim definitive.
"""

from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict

import attest
from cr_common import (
    CR_OUT,
    REPO,
    REQUEST_SOURCE,
    SEV_RANK,
    as_int,
    classify,
    load_config,
    norm,
    read_json,
    repo_path,
    write_json,
)

MARKER = "<!-- code-review-agent -->"
RELOCATE_MIN_CHARS = 12  # shortest quote that may move a citation to its one matching line
ICON = {"blocker": "🛑", "high": "🔴", "medium": "🟠", "low": "🟡", "info": "🔵"}
STATUS_ICON = {
    "BLOCK": "❌",
    "NEEDS_ATTESTATION": "🖊️",
    "ATTESTED": "✍️",
    "MUST_REVIEW": "⚠️",
    "REPORT": "ℹ️",
}


class Verifier:
    def __init__(self, tol: int, added: dict[str, set[int]]):
        self.tol, self.added = tol, added
        self._cache: dict[str, list[str] | None] = {}
        self.stats = Counter()

    def lines(self, rel):
        if rel not in self._cache:
            try:
                self._cache[rel] = (
                    (REPO / rel).read_text(encoding="utf-8", errors="replace").splitlines()
                )
            except OSError:
                self._cache[rel] = None
        return self._cache[rel]

    def check(self, cite, kind: str) -> tuple[str | None, int | None, str]:
        self.stats[f"{kind}_total"] += 1
        if not isinstance(cite, dict):
            return None, None, "no citation"
        rel, n = repo_path(cite.get("file")), as_int(cite.get("line"))
        if rel is None:
            return None, None, "path is empty or outside the repository"
        text = self.lines(rel)
        if text is None:
            return None, None, "file not found in the PR head"
        if n is None:
            return None, None, f"line {cite.get('line')} is out of range"
        want = norm(cite.get("evidence", "")).rstrip(".…").strip()
        if len(want) < 4:
            return None, None, "evidence is missing or too short to check"
        for off in sorted(range(-self.tol, self.tol + 1), key=abs):
            k = n + off
            if 1 <= k <= len(text) and want in norm(text[k - 1]):
                self.stats[f"{kind}_valid"] += 1
                return rel, k, ""
        # Agents sometimes give the line's position in the diff they read (diff.patch) instead of
        # its line in the file. Quoted text that occurs on exactly one line of the file still
        # pins the citation down, so move it there; ambiguous or short quotes are still rejected.
        hits = [k for k, line in enumerate(text, 1) if want in norm(line)]
        if len(want) >= RELOCATE_MIN_CHARS and len(hits) == 1:
            self.stats[f"{kind}_valid"] += 1
            self.stats[f"{kind}_relocated"] += 1
            return rel, hits[0], ""
        if not 1 <= n <= max(1, len(text)):
            return None, None, f"line {cite.get('line')} is out of range"
        return None, None, "quoted evidence isn't on or near that line"

    def changed(self, rel: str, n: int) -> bool:
        return any(abs(n - a) <= 1 for a in self.added.get(rel, ()))


def esc_data(s) -> str:
    return str(s).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def esc_prop(s) -> str:
    return esc_data(s).replace(":", "%3A").replace(",", "%2C")


def code(s) -> str:
    return "`" + str(s).replace("`", "'").replace("\n", " ")[:160] + "`"


def cell(s) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def main() -> int:
    cfg = load_config()
    checks = cfg["checks"]
    ctx = read_json(CR_OUT / "context.json", {}) or {}
    metas = {
        p: read_json(CR_OUT / f"{p}.meta.json", {}) or {}
        for p in ("understand", "oracle", "review")
    }
    out_of = {
        p: (read_json(CR_OUT / f"{p}.json", {}) or {}) if metas[p].get("ok") else {} for p in metas
    }
    understanding, oracle, review = out_of["understand"], out_of["oracle"], out_of["review"]
    agent_off = all(m.get("disabled") for m in metas.values()) if metas["review"] else True
    incomplete = not agent_off and not metas["review"].get("ok")

    added = {
        f["path"]: {n for a, b in f.get("added_ranges", []) for n in range(a, b + 1)}
        for f in ctx.get("files", [])
        if f.get("added_ranges")
    }
    v = Verifier(int(cfg["review"].get("line_tolerance", 3)), added)
    rejected: list[dict] = []

    # ---------------------------------------------------------------- script findings
    findings = []
    for f in ctx.get("script_findings", []):
        f = dict(f)
        f["in_scope"] = (not f["file"]) or f.get("pr_caused") or v.changed(f["file"], f["line"])
        findings.append(f)
    by_id = {f["id"]: f for f in findings}

    # ---------------------------------------------------------------- understanding (citations)
    understood = {"implementation": [], "impacted": [], "existing": [], "hotspots": []}
    for key, src in (
        ("implementation", "implementation"),
        ("impacted", "impacted_existing_code"),
        ("hotspots", "risk_hotspots"),
    ):
        for item in understanding.get(src) or []:
            rel, n, why = v.check(item, "understanding")
            if rel:
                understood[key].append({**item, "file": rel, "line": n})
            else:
                rejected.append(
                    {
                        "pass": "understand",
                        "file": item.get("file"),
                        "line": item.get("line"),
                        "title": item.get("unit") or item.get("why", "")[:60],
                        "reason": why,
                    }
                )
    for item in (understanding.get("existing_system") or {}).get("relevant_existing_code") or []:
        rel, n, why = v.check(item, "understanding")
        if rel:
            understood["existing"].append({**item, "file": rel, "line": n})

    # ---------------------------------------------------------------- oracle
    behaviours = {
        b["id"]: {**b, "criterion": c.get("id")}
        for c in oracle.get("criteria") or []
        for b in c.get("behaviours") or []
        if isinstance(b, dict) and b.get("id")
    }

    # ---------------------------------------------------------------- agent findings
    def add_agent(raw: dict, *, synthesized: bool = False):
        check = raw.get("check")
        if check not in checks:
            rejected.append(
                {
                    "pass": "review",
                    "file": raw.get("file"),
                    "line": raw.get("line"),
                    "title": raw.get("title", ""),
                    "reason": f"unknown check '{check}'",
                }
            )
            return
        if synthesized and not raw.get("file"):
            rel, n = "", 0
        else:
            rel, n, why = v.check(raw, "finding")
            if not rel:
                rejected.append(
                    {
                        "pass": "review",
                        "file": raw.get("file"),
                        "line": raw.get("line"),
                        "title": raw.get("title", ""),
                        "reason": why,
                    }
                )
                return
        sev = raw.get("severity") if raw.get("severity") in SEV_RANK else "info"
        conf = (
            raw.get("confidence") if raw.get("confidence") in ("high", "medium", "low") else "low"
        )
        f = {
            "id": f"agent:{check}:{rel}:{n}:{len(findings)}",
            "check": check,
            "rule": None,
            "source": "agent",
            "area": "pr"
            if not rel
            else (raw.get("area") if raw.get("area") == "cross_layer" else classify(rel, cfg)),
            "severity": sev,
            "confidence": conf,
            "hard": False,
            "file": rel,
            "line": n,
            "evidence": norm(raw.get("evidence", ""))[:160],
            "title": str(raw.get("title", ""))[:120],
            "explanation": str(raw.get("explanation", "")),
            "suggestion": str(raw.get("suggestion", "")),
            "in_scope": (not rel) or v.changed(rel, n),
            "notes": [],
        }
        if rel and not f["in_scope"] and isinstance(raw.get("caused_by"), dict):
            crel, cn, cwhy = v.check(raw["caused_by"], "finding")
            if crel and v.changed(crel, cn):
                f["in_scope"], f["anchored_by"] = True, f"{crel}:{cn}"
            else:
                f["notes"].append(f"caused_by not accepted ({cwhy or 'not a changed line'})")
        if check == "plausible_logic" and SEV_RANK[sev] <= SEV_RANK["high"]:
            traces = [
                t
                for t in raw.get("trace") or []
                if isinstance(t, dict)
                and norm(t.get("expected"))
                and norm(t.get("expected")) != norm(t.get("actual"))
            ]
            if traces:
                f["trace"] = traces[:3]
            else:
                f["severity"] = "medium"
                f["notes"].append("downgraded: no hand trace showing expected ≠ actual")
        corr = raw.get("corroboration")
        if isinstance(corr, dict):
            kind = corr.get("kind")
            if kind == "taint_source":
                crel, cn, _ = v.check(corr, "corroboration")
                if crel and REQUEST_SOURCE.search(v.lines(crel)[cn - 1]):
                    f["corroborated"] = f"taint source {crel}:{cn} reads untrusted input"
                else:
                    f["notes"].append("taint source not verified")
            elif kind == "existing_symbol":
                crel, cn, _ = v.check(corr, "corroboration")
                if crel and not v.changed(crel, cn):
                    f["corroborated"] = f"existing code {crel}:{cn}"
                    f["related"] = {"file": crel, "line": cn}
            elif kind == "oracle_behaviour" and corr.get("behaviour_id") in behaviours:
                f["corroborated"] = f"oracle {corr['behaviour_id']}"
        # Same spot as a script finding of the same check: merge, keep the script's hardness.
        twin = next(
            (
                s
                for s in findings
                if s["source"] == "script"
                and s["check"] == check
                and s["file"] == rel
                and rel
                and abs(s["line"] - n) <= 1
            ),
            None,
        )
        if twin:
            twin["source"] = "script+agent"
            if SEV_RANK[f["severity"]] < SEV_RANK[twin["severity"]]:
                twin["severity"] = f["severity"]
            twin["explanation"] += " Agent: " + f["explanation"]
            return
        findings.append(f)

    for raw in review.get("findings") or []:
        if isinstance(raw, dict):
            add_agent(raw)

    # Injection verdicts on the sinks the script couldn't trace.
    for sv in review.get("sink_verdicts") or []:
        target = by_id.get(sv.get("finding_id")) if isinstance(sv, dict) else None
        if not target or target["rule"] != "INJ002":
            continue
        target["agent_verdict"] = sv.get("verdict")
        target["agent_reason"] = str(sv.get("reason", ""))[:300]
        if sv.get("verdict") == "tainted":
            crel, cn, why = v.check(sv.get("source"), "corroboration")
            if crel and REQUEST_SOURCE.search(v.lines(crel)[cn - 1]):
                target.update(
                    severity="blocker",
                    corroborated=f"taint source {crel}:{cn} reads untrusted input",
                    title=target["title"].replace(
                        "source not proven safe", "agent traced untrusted input"
                    ),
                )
            else:
                target["agent_reason"] += (
                    f" (source not verified: {why or 'line does not read untrusted input'})"
                )
    for adj in review.get("script_adjudications") or []:
        target = by_id.get(adj.get("finding_id")) if isinstance(adj, dict) else None
        if target:
            target["adjudication"] = f"{adj.get('verdict')}: {str(adj.get('reason', ''))[:240]}"

    # Requirement map -> synthesized requirement findings.
    crit = {c["id"]: c for c in (ctx.get("pr") or {}).get("acceptance_criteria", [])}
    req_rows = []
    for item in review.get("requirement_map") or []:
        if not isinstance(item, dict):
            continue
        impl = [
            f"{r}:{n}"
            for c in item.get("implemented_at") or []
            for r, n, _ in [v.check(c, "requirement")]
            if r
        ]
        tests = [
            f"{r}:{n}"
            for c in item.get("tested_by") or []
            for r, n, _ in [v.check(c, "requirement")]
            if r
        ]
        status = item.get("status", "unclear")
        if status == "implemented" and not impl:
            status = "unclear"
        req_rows.append(
            {
                "id": item.get("criterion_id"),
                "text": crit.get(item.get("criterion_id"), {}).get("text", ""),
                "status": status,
                "implemented_at": impl,
                "tested_by": tests,
                "note": item.get("note", ""),
            }
        )
        if status in ("missing", "partial"):
            first = (item.get("implemented_at") or [None])[0]
            add_agent(
                {
                    "check": "requirement_coverage",
                    "severity": "high",
                    "confidence": "medium",
                    "title": f"{item.get('criterion_id')} is {status}",
                    "explanation": item.get("note")
                    or f"The agent mapped this criterion as {status}.",
                    "suggestion": "Implement the criterion fully, or narrow it with the product "
                    "owner.",
                    **(
                        {
                            "file": first.get("file"),
                            "line": first.get("line"),
                            "evidence": first.get("evidence"),
                        }
                        if isinstance(first, dict) and status == "partial"
                        else {}
                    ),
                },
                synthesized=not (isinstance(first, dict) and status == "partial"),
            )
    for cid, c in crit.items():
        if review and cid not in {r["id"] for r in req_rows}:
            req_rows.append(
                {
                    "id": cid,
                    "text": c["text"],
                    "status": "unmapped",
                    "implemented_at": [],
                    "tested_by": [],
                    "note": "the review didn't map this criterion",
                }
            )

    # Oracle coverage -> tests that don't assert the requirement.
    oracle_rows, gaps = [], defaultdict(list)
    for item in review.get("oracle_coverage") or []:
        if not isinstance(item, dict) or item.get("behaviour_id") not in behaviours:
            continue
        b = behaviours[item["behaviour_id"]]
        trel, tn, _ = v.check(item.get("test"), "oracle") if item.get("test") else (None, None, "")
        status = item.get("status")
        if status in ("asserted", "asserted_differently") and not trel:
            status = "missing"
        oracle_rows.append(
            {
                **b,
                "status": status,
                "test": f"{trel}:{tn}" if trel else "",
                "note": item.get("note", ""),
            }
        )
        if status != "asserted" and not b.get("assumption"):
            gaps[b["criterion"]].append((b, status, trel, tn))
    for cid, items in gaps.items():
        differently = [x for x in items if x[1] == "asserted_differently"]
        core = [x for x in items if x[0]["kind"] in ("positive", "negative")]
        sev = "high" if differently or core else "medium"
        anchor = differently[0] if differently else None
        lines = v.lines(anchor[2]) if anchor else None
        add_agent(
            {
                "check": "tests_assert_requirements",
                "severity": sev,
                "confidence": "medium",
                "title": f"{cid}: {len(items)} oracle behaviour(s) not asserted"
                + (f", {len(differently)} asserted differently" if differently else ""),
                "explanation": "; ".join(
                    f"{b['id']} ({b['kind']}): then {b['then']}" for b, *_ in items[:4]
                ),
                "suggestion": "Write these tests from the criterion; where a test expects "
                "something else, "
                "check whether the test or the code is wrong.",
                **(
                    {
                        "file": anchor[2],
                        "line": anchor[3],
                        "evidence": lines[anchor[3] - 1].strip()[:160],
                    }
                    if anchor and lines
                    else {}
                ),
            },
            synthesized=not anchor,
        )

    # ---------------------------------------------------------------- attestations and decisions
    attested, att_notes = attest.collect(
        cfg, os.environ.get("HEAD_SHA", ""), (ctx.get("pr") or {}).get("author", "")
    )
    for f in findings:
        row = checks[f["check"]]["row"]
        serious = SEV_RANK[f["severity"]] <= SEV_RANK["high"]
        if row == "BLOCKING" and serious and f["in_scope"] and f["confidence"] != "low":
            if f["hard"]:
                f["status"] = "BLOCK"
            else:
                f["status"] = "ATTESTED" if f["check"] in attested else "NEEDS_ATTESTATION"
        elif row == "STANDARD" and serious and f["in_scope"]:
            f["status"] = "MUST_REVIEW"
        else:
            f["status"] = "REPORT"
    always = set(cfg["attestation"].get("always_required") or [])
    code_changed = any(
        (ctx.get("summary") or {}).get(f"{a}_files") for a in ("backend", "frontend", "test")
    )
    per_check = {}
    for cid, c in checks.items():
        fs = [f for f in findings if f["check"] == cid]
        statuses = Counter(f["status"] for f in fs)
        if statuses["BLOCK"]:
            st = "BLOCK"
        elif statuses["NEEDS_ATTESTATION"] or (
            cid in always and code_changed and cid not in attested
        ):
            st = "NEEDS_ATTESTATION"
        elif statuses["ATTESTED"] or (cid in always and cid in attested):
            st = "ATTESTED"
        elif statuses["MUST_REVIEW"]:
            st = "MUST_REVIEW"
        elif fs:
            st = "NOTES"
        else:
            st = "PASS"
        per_check[cid] = {
            "title": c["title"],
            "row": c["row"],
            "status": st,
            "count": len(fs),
            "by_severity": dict(Counter(f["severity"] for f in fs)),
            "attested_by": attested.get(cid, []),
            "summary": (review.get("check_summaries") or {}).get(cid, ""),
        }

    if ctx.get("full_audit"):
        cfg["mode"] = "advisory"  # an audit of existing code reports; it isn't a merge gate
    blocking_mode = cfg["mode"] == "blocking"
    blockers = [f for f in findings if f["status"] == "BLOCK"]
    waiting = sorted(
        {f["check"] for f in findings if f["status"] == "NEEDS_ATTESTATION"}
        | {cid for cid, pc in per_check.items() if pc["status"] == "NEEDS_ATTESTATION"}
    )
    fail = blocking_mode and (
        bool(blockers) or bool(waiting) or (incomplete and cfg.get("fail_closed"))
    )
    would_fail = bool(blockers) or bool(waiting)

    findings.sort(
        key=lambda f: (
            list(STATUS_ICON).index(f["status"]),
            SEV_RANK[f["severity"]],
            f["check"],
            f["file"],
            f["line"],
        )
    )
    metrics = {
        "mode": cfg["mode"],
        "result": "fail" if fail else "pass",
        "agent": {
            p: ("off" if m.get("disabled") else "ok" if m.get("ok") else "failed")
            for p, m in metas.items()
        },
        "cost_usd": round(sum(m.get("cost_usd") or 0 for m in metas.values()), 4),
        "files": ctx.get("summary", {}),
        "findings": len(findings),
        "by_source": dict(Counter(f["source"] for f in findings)),
        "by_status": dict(Counter(f["status"] for f in findings)),
        "checks": {cid: pc["status"] for cid, pc in per_check.items()},
        "citations": dict(v.stats),
        "rejected_claims": len(rejected),
        "citation_validity": round(v.stats["finding_valid"] / v.stats["finding_total"], 4)
        if v.stats["finding_total"]
        else None,
        "requirements": dict(Counter(r["status"] for r in req_rows)),
        "oracle": dict(Counter(r["status"] for r in oracle_rows)),
        "suppressed": len(ctx.get("suppressed", [])),
    }
    write_json(
        CR_OUT / "findings.json",
        {
            "findings": findings,
            "rejected": rejected,
            "requirements": req_rows,
            "oracle": oracle_rows,
            "checks": per_check,
            "understanding": understood,
            "attestation_notes": att_notes,
        },
    )
    write_json(CR_OUT / "metrics.json", metrics)

    # ---------------------------------------------------------------- annotations
    shown = 0
    for f in findings:
        if shown >= int(cfg["review"].get("max_annotations", 20)) or not f["file"]:
            continue
        if f["status"] in ("BLOCK", "NEEDS_ATTESTATION", "MUST_REVIEW"):
            level = "error" if f["status"] in ("BLOCK", "NEEDS_ATTESTATION") else "warning"
            title = f"[{checks[f['check']]['title']}] {f['title']}"
            print(
                f"::{level} "
                f"file={esc_prop(f['file'])},line={f['line']},title={esc_prop(title[:200])}::"
                f"{esc_data(f['explanation'][:400] + ' Fix: ' + f['suggestion'][:300])}"
            )
            shown += 1

    # ---------------------------------------------------------------- report
    if fail:
        head = "❌ Blocked" if blockers else "🖊️ Waiting for attestation"
    elif would_fail:
        head = "⚠️ Would block (advisory mode)"
    else:
        head = "✅ Pass"
    s = ctx.get("summary", {})
    passes = " · ".join(f"{p} {metrics['agent'][p]}" for p in ("understand", "oracle", "review"))
    out = [
        MARKER,
        f"## 🔍 Code review agent: {head}",
        "",
        f"**Mode:** {cfg['mode']} · **Agent:** {passes} · **Changed:** "
        f"{s.get('backend_files', 0)} backend, "
        f"{s.get('frontend_files', 0)} frontend, {s.get('test_files', 0)} test file(s) · "
        f"**Criteria:** {s.get('criteria', 0)}"
        + (" · **Full-project audit**" if ctx.get("full_audit") else ""),
    ]
    if incomplete:
        out += [
            "",
            f"> ⚠️ The review pass didn't produce a usable result "
            f"({(metas['review'].get('error') or 'unknown error').rstrip('.')[:200]}). "
            "The deterministic checks still applied.",
        ]
    if review.get("summary"):
        out += ["", review["summary"].strip()]

    out += ["", "| # | Check | Row | Status | Findings |", "|---:|---|---|---|---|"]
    label = {
        "BLOCK": "❌ Blocked",
        "NEEDS_ATTESTATION": "🖊️ Needs attestation",
        "ATTESTED": "✍️ Attested",
        "MUST_REVIEW": "⚠️ Must review",
        "NOTES": "ℹ️ Notes",
        "PASS": "✅ Pass",
    }
    for i, pc in enumerate(per_check.values(), 1):
        sev = " ".join(
            f"{ICON[k]}{n}"
            for k, n in sorted(pc["by_severity"].items(), key=lambda x: SEV_RANK[x[0]])
        )
        who = (
            f" by @{', @'.join(pc['attested_by'])}"
            if pc["status"] == "ATTESTED" and pc["attested_by"]
            else ""
        )
        out.append(
            f"| {i} | {pc['title']} | {pc['row']} | {label[pc['status']]}{who} | {sev or '—'} |"
        )

    ask = understanding.get("ask") or {}
    if ask or understood["implementation"]:
        out += ["", "<details><summary><b>What the agent understood</b></summary>", ""]
        if ask.get("restated"):
            out += [f"**The ask:** {ask['restated'].strip()}", ""]
        es = understanding.get("existing_system") or {}
        if es.get("summary"):
            out += [f"**Existing system:** {es['summary'].strip()}", ""]
        if understood["implementation"]:
            out += ["**How it's implemented:**", ""]
            out += [
                f"- `{x['file']}:{x['line']}` {x.get('unit', '')} ({x.get('kind', '')}) — "
                f"{x.get('what_it_does', '')}"
                + (
                    f" → {', '.join(x.get('serves') or [])}"
                    if x.get("serves")
                    else " → *no criterion*"
                )
                for x in understood["implementation"][:20]
            ]
        if understood["impacted"]:
            out += ["", "**Existing code this change affects:**", ""]
            out += [
                f"- `{x['file']}:{x['line']}` {x.get('why', '')}"
                for x in understood["impacted"][:15]
            ]
        out += ["</details>"]

    def render(f):
        where = f"`{f['file']}:{f['line']}`" if f["file"] else "*PR-level*"
        bits = [
            checks[f["check"]]["title"],
            f["area"],
            f"confidence {f['confidence']}",
            f["source"],
        ]
        if f.get("rule"):
            bits.append(f["rule"])
        lines = [
            f"- {STATUS_ICON[f['status']]} {ICON[f['severity']]} **{f['title']}** {where}"
            + (" *(existing code this PR breaks)*" if f.get("pr_caused") else "")
            + (f" *(caused by `{f['anchored_by']}`)*" if f.get("anchored_by") else "")
            + ("" if f["in_scope"] else " *(not on a changed line, so it can't block)*")
            + "  ",
            f"  <sub>{' · '.join(bits)}</sub>  ",
        ]
        if f["evidence"]:
            lines.append(f"  {code(f['evidence'])}  ")
        lines.append(f"  {f['explanation'].strip()}  ")
        for t in f.get("trace") or []:
            lines.append(
                f"  ↳ trace: input {code(t['input'])} → expected {code(t['expected'])}, "
                f"actual {code(t['actual'])}  "
            )
        if f.get("related") and (f["related"]["file"], f["related"]["line"]) != (
            f["file"],
            f["line"],
        ):
            lines.append(f"  ↳ see `{f['related']['file']}:{f['related']['line']}`  ")
        if f.get("corroborated"):
            lines.append(f"  ↳ corroborated: {f['corroborated']}  ")
        if f.get("agent_verdict"):
            lines.append(
                f"  ↳ agent verdict: **{f['agent_verdict']}** — {f.get('agent_reason', '')}  "
            )
        if f.get("adjudication"):
            lines.append(f"  ↳ agent: {f['adjudication']}  ")
        for note in f.get("notes") or []:
            lines.append(f"  ↳ {note}  ")
        lines += [f"  **Fix:** {f['suggestion'].strip()}", ""]
        return lines

    cap = int(cfg["review"].get("max_findings", 60))
    listed = 0
    for status, heading in (
        ("BLOCK", "Blocking"),
        ("NEEDS_ATTESTATION", "Needs a named human attestation"),
        ("MUST_REVIEW", "Must review (STANDARD rows)"),
    ):
        group = [f for f in findings if f["status"] == status]
        if group:
            out += ["", f"### {STATUS_ICON[status]} {heading} ({len(group)})", ""]
            if status == "NEEDS_ATTESTATION":
                ids = sorted({f["check"] for f in group} | set(waiting))
                alias = {cid: (checks[cid].get("aliases") or [cid])[0] for cid in ids}
                out += [
                    "These are judgements, not proofs. Fix them, or have a reviewer with write "
                    "access "
                    "(not the author) submit an **approving** review on this commit containing:",
                    "",
                    f"```\nattest: {', '.join(alias[c] for c in ids)}\n```",
                    "",
                ]
            for f in group:
                if listed < cap:
                    out += render(f)
                    listed += 1
    others = [f for f in findings if f["status"] in ("REPORT", "ATTESTED")]
    if others:
        out += ["", f"<details><summary><b>Other findings ({len(others)})</b></summary>", ""]
        for f in others:
            if listed < cap:
                out += render(f)
                listed += 1
        out += ["</details>"]
    if len(findings) > listed:
        out += ["", f"_{len(findings) - listed} more in `findings.json` (evidence artifact)._"]

    if req_rows:
        mark = {
            "implemented": "✅",
            "partial": "🟠",
            "missing": "❌",
            "not_applicable": "➖",
            "unclear": "❔",
            "unmapped": "❔",
        }
        out += [
            "",
            "### Requirement coverage",
            "",
            "| Criterion | Status | Implemented at | Tested by |",
            "|---|---|---|---|",
        ]
        for r in req_rows:
            out.append(
                f"| **{cell(r['id'])}** {cell(r['text'])[:120]} | {mark.get(r['status'], '❔')} "
                f"{r['status']} | "
                f"{', '.join(f'`{x}`' for x in r['implemented_at'][:3]) or '—'} | "
                f"{', '.join(f'`{x}`' for x in r['tested_by'][:3]) or '—'} |"
            )
    if oracle_rows:
        mark = {"asserted": "✅", "asserted_differently": "⚠️", "missing": "❌"}
        out += [
            "",
            f"<details><summary><b>Independent oracle vs the PR's tests</b> "
            f"({sum(r['status'] == 'asserted' for r in oracle_rows)}/{len(oracle_rows)} "
            "asserted)</summary>",
            "",
            "Behaviours derived from the criteria alone, without the implementation.",
            "",
            "| Behaviour | Kind | Then | Status | Test |",
            "|---|---|---|---|---|",
        ]
        for r in oracle_rows:
            out.append(
                f"| {cell(r['id'])}{' *(assumption)*' if r.get('assumption') else ''} | "
                f"{r['kind']} | "
                f"{cell(r['then'])[:120]} | {mark.get(r['status'], '')} {r['status']} | "
                f"{('`' + r['test'] + '`') if r['test'] else '—'} |"
            )
        out += ["</details>"]
    scope_rows = [x for x in review.get("scope_map") or [] if isinstance(x, dict)]
    unjustified = [x for x in scope_rows if not x.get("justified_by")]
    if unjustified:
        out += ["", "### Scope: changes no criterion asks for", ""]
        out += [f"- `{x.get('file')}` {x.get('note', '')}" for x in unjustified[:15]]
    if ctx.get("suppressed"):
        out += [
            "",
            f"<details><summary>Suppressed on the line ({len(ctx['suppressed'])})</summary>",
            "",
        ]
        out += [
            f"- `{x['file']}:{x['line']}` {x['rule']} {x['title']} — *{x['suppressed']}*"
            for x in ctx["suppressed"]
        ]
        out += ["</details>"]
    if rejected:
        out += [
            "",
            f"<details><summary>Rejected agent claims ({len(rejected)})</summary>",
            "",
            "These failed the citation check and were ignored.",
            "",
        ]
        out += [
            f"- {r['pass']}: `{r['file']}:{r['line']}` {r['title']} — {r['reason']}"
            for r in rejected[:30]
        ]
        out += ["</details>"]
    notes = att_notes + [f"check error: {e}" for e in ctx.get("check_errors", [])]
    lims = [str(x) for x in review.get("limitations") or []]
    if notes or lims:
        out += [
            "",
            "<sub>" + " · ".join(notes + [f"agent limitation: {x}" for x in lims[:4]]) + "</sub>",
        ]
    env = os.environ
    run = (
        f"{env.get('GITHUB_SERVER_URL', '')}/{env.get('GITHUB_REPOSITORY', '')}"
        f"/actions/runs/{env.get('GITHUB_RUN_ID', '')}"
    )
    moved = sum(n for k, n in v.stats.items() if k.endswith("_relocated"))
    out += [
        "",
        f"<sub>Commit {env.get('HEAD_SHA', '')[:7] or 'local'} · citations valid "
        f"{v.stats['finding_valid']}/{v.stats['finding_total']}"
        + (f" ({moved} moved from a diff position to the quoted line)" if moved else "")
        + f" · cost ${metrics['cost_usd']:.2f}"
        + (f" · [run]({run})" if env.get("GITHUB_RUN_ID") else "")
        + f" · checks from [{cfg['checklist']['source']}]({cfg['checklist']['url']})</sub>",
    ]
    (CR_OUT / "report.md").write_text("\n".join(out) + "\n", encoding="utf-8")

    code_ = 1 if fail else 0
    (CR_OUT / "gate_exit_code").write_text(str(code_), encoding="utf-8")
    print(
        f"Gate: {metrics['result']} — {len(blockers)} blocking, {len(waiting)} check(s) awaiting "
        "attestation, "
        f"{len(findings)} findings, {len(rejected)} rejected claims (mode {cfg['mode']})."
    )
    return code_


if __name__ == "__main__":
    sys.exit(main())
