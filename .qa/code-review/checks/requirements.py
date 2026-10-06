"""Requirement coverage (BLOCKING): everything the ticket asks for is implemented, and no
placeholder stands in for behaviour.

The script catches the stand-ins: NotImplementedError, stub bodies, "not implemented" throws,
TODO/placeholder markers and lorem ipsum. Mapping each acceptance criterion to the code that
implements it is the agent's job; a criterion it can't map needs a human attestation.
"""

from __future__ import annotations

import ast
import re

from cr_common import Ctx, finding, line_of

from . import _ast

CHECK = "requirement_coverage"
TODO = re.compile(r"(#|//|/\*|<!--|\{#).*\b(TODO|FIXME|XXX|HACK|TBD)\b")
PLACEHOLDER = re.compile(
    r"(#|//|<!--|\{#).*\b(stub(bed)?|placeholder|dummy|fake|hard-?coded( for now)?|"
    r"temporary|for now|implement (this )?later)\b",
    re.I,
)
JS_NOT_IMPL = re.compile(r"throw\s+new\s+\w*Error\(\s*['\"`][^'\"`]*not\s+implemented", re.I)
LOREM = re.compile(r"lorem ipsum", re.I)


def run(ctx: Ctx, idx: dict) -> list[dict]:
    out: list[dict] = []
    if not ctx.pr.get("acceptance_criteria"):
        req = ctx.cfg["requirements"].get("require")
        out.append(
            finding(
                ctx,
                CHECK,
                "RQ005",
                "",
                0,
                "",
                area="pr",
                severity="high" if req else "info",
                hard=bool(req),
                title="No acceptance criteria found",
                explanation="Nothing to check the change against: no criteria in the PR "
                "description, "
                "requirement files or the linked Jira issue.",
                suggestion="Add an 'Acceptance criteria' section to the PR, or link the ticket.",
            )
        )

    for f in ctx.changed("backend", "frontend"):
        path, text = f["path"], ctx.head(f["path"]) or ""
        added = ctx.added[path]
        if path.endswith(".py"):
            tree = _ast.parse(text, path)
            if tree:
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Raise)
                        and node.lineno in added
                        and node.exc is not None
                    ):
                        target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                        if (
                            getattr(target, "id", getattr(target, "attr", ""))
                            == "NotImplementedError"
                        ):
                            fn = next(
                                (
                                    x
                                    for x in _ast.functions(tree)
                                    if x.lineno <= node.lineno <= (x.end_lineno or x.lineno)
                                ),
                                None,
                            )
                            if not (fn and _ast.is_abstract(fn, tree)):
                                out.append(
                                    finding(
                                        ctx,
                                        CHECK,
                                        "RQ001",
                                        path,
                                        node.lineno,
                                        line_of(text, node.lineno),
                                        severity="blocker",
                                        hard=True,
                                        title="NotImplementedError left in the change",
                                        explanation="The behaviour this function promises isn't "
                                        "there.",
                                        suggestion="Implement it, or take it out of this PR.",
                                    )
                                )
                for fn in _ast.functions(tree):
                    if (
                        _ast.touches(fn, added)
                        and _ast.body_is_stub(fn)
                        and not _ast.is_abstract(fn, tree)
                    ):
                        out.append(
                            finding(
                                ctx,
                                CHECK,
                                "RQ002",
                                path,
                                fn.lineno,
                                line_of(text, fn.lineno),
                                severity="high",
                                hard=True,
                                title=f"{fn.name}() has no body",
                                explanation="Only pass / ... / a docstring: a placeholder, not "
                                "behaviour.",
                                suggestion="Implement it or remove it.",
                            )
                        )
        for n in sorted(added):
            line = line_of(text, n)
            if JS_NOT_IMPL.search(line):
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "RQ001",
                        path,
                        n,
                        line,
                        severity="blocker",
                        hard=True,
                        title="'Not implemented' thrown in the change",
                        explanation="The behaviour this code path promises isn't there.",
                        suggestion="Implement it, or take it out of this PR.",
                    )
                )
            elif TODO.search(line):
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "RQ003",
                        path,
                        n,
                        line,
                        severity="medium",
                        hard=False,
                        title="TODO left in new code",
                        explanation="Check it isn't standing in for something a criterion asks "
                        "for.",
                        suggestion="Finish it, or link a follow-up ticket in the comment.",
                    )
                )
            elif PLACEHOLDER.search(line):
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "RQ004",
                        path,
                        n,
                        line,
                        severity="medium",
                        hard=False,
                        title="Placeholder or hard-coded stand-in",
                        explanation="The comment says this isn't the real behaviour yet.",
                        suggestion="Replace it with the real implementation before merging.",
                    )
                )
            elif LOREM.search(line) and f["area"] == "frontend":
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "RQ004",
                        path,
                        n,
                        line,
                        severity="medium",
                        hard=False,
                        title="Lorem ipsum in the UI",
                        explanation="Placeholder copy would ship.",
                        suggestion="Use the real text.",
                    )
                )
    return out
