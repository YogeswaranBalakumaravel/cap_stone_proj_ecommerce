"""Scope creep (ADVISORY, named human attestation): nothing is implemented that wasn't asked for.

Mechanical rules in the spirit of Danger: new endpoints without the agreed label, new config and
environment switches, files outside the declared scope, a diff-size budget, new code nothing
calls, and abstractions with a single implementation. The agent reverse-walks every changed file
against the criteria; intent needs a human.
"""

from __future__ import annotations

import ast
import re

import repo_index
from cr_common import Ctx, finding, line_of, match

from . import _ast

CHECK = "scope_creep"
ENV = re.compile(
    r"os\.(?:environ\.get|getenv)\(\s*['\"](\w+)['\"]|os\.environ\[\s*['\"](\w+)['\"]\s*\]"
)


def run(ctx: Ctx, idx: dict) -> list[dict]:
    out: list[dict] = []
    scfg = ctx.cfg["scope"]
    label = scfg.get("new_endpoint_label", "new-endpoint")
    labels = set(ctx.pr.get("labels") or [])

    for r in idx["routes"]:
        if r["line"] in ctx.added.get(r["file"], set()):
            if r["endpoint"] in repo_index.endpoints_of(r["file"], ctx.base_text(r["file"])):
                continue
            out.append(
                finding(
                    ctx,
                    CHECK,
                    "SC001",
                    r["file"],
                    r["line"],
                    line_of(ctx.head(r["file"]) or "", r["line"]),
                    severity="low" if label in labels else "medium",
                    hard=False,
                    title=f"New endpoint {','.join(r['methods'])} {r['rule']}",
                    explanation="Every new endpoint must trace to a criterion."
                    + ("" if label in labels else f" The PR has no '{label}' label."),
                    suggestion="Map it to a criterion, or move it to its own PR (label "
                    f"'{label}').",
                )
            )

    base_env: set[str] = set()
    for f in ctx.changed("backend"):
        base_env |= {a or b for a, b in ENV.findall(ctx.base_text(f["path"]) or "")}
    for f in ctx.changed("backend"):
        text = ctx.head(f["path"]) or ""
        for n in sorted(ctx.added[f["path"]]):
            for a, b in ENV.findall(line_of(text, n)):
                if (a or b) not in base_env:
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "SC002",
                            f["path"],
                            n,
                            line_of(text, n),
                            severity="low",
                            hard=False,
                            title=f"New configuration switch {a or b}",
                            explanation="New configuration is a common form of speculative scope.",
                            suggestion="Confirm a criterion needs it; document it in render.yaml "
                            "/ README.",
                        )
                    )

    declared = ctx.pr.get("declared_scope") or []
    if declared:
        for f in ctx.changed():
            if f["area"] in ("backend", "frontend", "test") and not match(f["path"], declared):
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "SC003",
                        f["path"],
                        1,
                        line_of(ctx.head(f["path"]) or "", 1),
                        severity="medium",
                        hard=False,
                        title="File outside the declared scope",
                        explanation=f"The PR's scope lists: {', '.join(declared)[:150]}.",
                        suggestion="Justify it against a criterion, or split it out.",
                    )
                )

    churn = sum(
        f.get("churn", 0) for f in ctx.files if f["area"] in ("backend", "frontend", "test")
    )
    if churn > int(scfg.get("max_changed_lines", 400)):
        out.append(
            finding(
                ctx,
                CHECK,
                "SC004",
                "",
                0,
                "",
                area="pr",
                severity="medium",
                hard=False,
                title=f"{churn} changed lines (budget {scfg.get('max_changed_lines', 400)})",
                explanation="Large AI-assisted diffs hide unrequested work and get skimmed in "
                "review.",
                suggestion="Split the PR by criterion.",
            )
        )

    corpus = {
        p: ctx.head(p) or ""
        for p in ctx.tracked
        if p.endswith((".py", ".html", ".htm", ".js", ".ts", ".jsx", ".tsx", ".j2", ".jinja"))
    }
    for f in ctx.changed("backend"):
        path = f["path"]
        if not path.endswith(".py"):
            continue
        text = ctx.head(path)
        tree = _ast.parse(text, path)
        if not tree:
            continue
        for fn in _ast.functions(tree) + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            if fn.lineno not in ctx.added[path] or fn.name.startswith("__") or fn.decorator_list:
                continue
            word = re.compile(rf"\b{re.escape(fn.name)}\b")
            uses = sum(len(word.findall(t)) for t in corpus.values())
            if uses <= 1:
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "SC006",
                        path,
                        fn.lineno,
                        line_of(text, fn.lineno),
                        severity="medium",
                        hard=False,
                        title=f"{fn.name} is never used",
                        explanation="New code nothing calls is speculative: it can't be required.",
                        suggestion="Remove it, or wire it to the criterion that needs it.",
                    )
                )
            elif isinstance(fn, ast.ClassDef) and any(
                getattr(b, "id", getattr(b, "attr", "")) in ("ABC", "Protocol") for b in fn.bases
            ):
                subs = len(
                    re.findall(
                        rf"class\s+\w+\(([^)]*\b{re.escape(fn.name)}\b[^)]*)\)",
                        "\n".join(corpus.values()),
                    )
                )
                if subs <= 1:
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "SC007",
                            path,
                            fn.lineno,
                            line_of(text, fn.lineno),
                            severity="low",
                            hard=False,
                            title=f"Abstraction {fn.name} has one implementation",
                            explanation="An interface with a single implementation is speculative "
                            "design.",
                            suggestion="Inline it until a second implementation exists.",
                        )
                    )
    return out
