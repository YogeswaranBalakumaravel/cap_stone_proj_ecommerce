"""Plausible-but-wrong logic (BLOCKING, named human attestation).

No tool decides this row. The script catches the definitive cases (references the PR breaks,
indexing past the end, identity comparison with literals, conditions that are always true) and
raises signals for the classic AI boundary slips (naive datetimes, float equality, Unicode case
folding, loose equality in JS). The agent traces two to three inputs through each non-trivial
changed function; a human attests to the row.
"""

from __future__ import annotations

import ast
import re

import repo_index
from cr_common import Ctx, finding, line_of

from . import _ast

CHECK = "plausible_logic"
TEMPLATE_REF = re.compile(r"\{%-?\s*(?:extends|include|import|from)\s+['\"]([^'\"]+)['\"]")
LOOSE_EQ = re.compile(r"[^=!<>]==(?!=)\s*(?!null\b)|!=(?!=)")


def _template_exists(ctx: Ctx, name: str) -> bool:
    return any(
        (d.rstrip("/") + "/" + name) in set(ctx.tracked) for d in ctx.cfg["paths"]["template_dirs"]
    )


def run(ctx: Ctx, idx: dict) -> list[dict]:
    out: list[dict] = []
    endpoints = set(idx["endpoints"])

    # References this PR breaks: endpoints and templates that existed at base but not at head.
    removed_endpoints: set[str] = set()
    for f in ctx.files:
        if f["path"].endswith(".py"):
            removed_endpoints |= repo_index.endpoints_of(
                f["path"], ctx.base_text(f["path"])
            ) - repo_index.endpoints_of(f["path"], ctx.head(f["path"]))
    removed_endpoints -= endpoints
    removed_templates = {
        f.get("old_path") or f["path"]
        for f in ctx.files
        if f["status"] == "deleted" or f.get("old_path")
    }

    for ref in idx["url_for"]:
        ep, path, n = ref["endpoint"], ref["file"], ref["line"]
        on_added = n in ctx.added.get(path, set())
        if ep in endpoints:
            continue
        if on_added or ep in removed_endpoints:
            out.append(
                finding(
                    ctx,
                    CHECK,
                    "PL001",
                    path,
                    n,
                    line_of(ctx.head(path) or "", n),
                    severity="blocker",
                    hard=True,
                    pr_caused=not on_added,
                    title=f"url_for('{ep}') points at an endpoint that doesn't exist",
                    explanation="Flask raises BuildError when this renders; the page 500s."
                    + (" The PR removed or renamed this endpoint." if not on_added else ""),
                    suggestion=f"Use one of the registered endpoints, or keep '{ep}'.",
                )
            )

    for path in ctx.tracked:
        if not (
            path.endswith(".py") or path.endswith((".html", ".htm", ".jinja", ".jinja2", ".j2"))
        ):
            continue
        text = ctx.head(path) or ""
        for n, line in enumerate(text.splitlines(), 1):
            refs = (
                repo_index.RENDER.findall(line) if path.endswith(".py") else []
            ) + TEMPLATE_REF.findall(line)
            for name in refs:
                on_added = n in ctx.added.get(path, set())
                gone = any(t.endswith("/" + name) for t in removed_templates)
                if (on_added or gone) and not _template_exists(ctx, name):
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "PL002",
                            path,
                            n,
                            line,
                            severity="blocker",
                            hard=True,
                            pr_caused=not on_added,
                            title=f"Template '{name}' doesn't exist",
                            explanation="Jinja raises TemplateNotFound at request time.",
                            suggestion="Add the template or fix the name.",
                        )
                    )

    for f in ctx.changed("backend"):
        path = f["path"]
        if not path.endswith(".py"):
            continue
        text = ctx.head(path)
        tree = _ast.parse(text, path)
        if not tree:
            continue
        added = ctx.added[path]

        def hit(rule, node, added=added, path=path, text=text, **kw):
            if node.lineno in added:
                out.append(
                    finding(ctx, CHECK, rule, path, node.lineno, line_of(text, node.lineno), **kw)
                )

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Call)
                and _ast.call_name(node.slice) == "len"
                and node.slice.args
                and ast.dump(node.slice.args[0]) == ast.dump(node.value)
            ):
                hit(
                    "PL003",
                    node,
                    severity="high",
                    hard=True,
                    title="Indexing one past the end",
                    explanation="x[len(x)] always raises IndexError.",
                    suggestion="Use x[-1] or x[len(x) - 1].",
                )
            elif isinstance(node, ast.Call) and _ast.call_name(node) == "range" and node.args:
                a = node.args[-1] if len(node.args) == 1 else node.args[1]
                if (
                    isinstance(a, ast.BinOp)
                    and isinstance(a.op, ast.Add)
                    and isinstance(a.left, ast.Call)
                    and _ast.call_name(a.left) == "len"
                    and _ast.is_const(a.right, 1)
                ):
                    hit(
                        "PL003",
                        node,
                        severity="medium",
                        hard=False,
                        title="range(len(x) + 1)",
                        explanation="Iterates one past the last index; correct only for positions "
                        "*between* items.",
                        suggestion="Trace the last iteration by hand and confirm the boundary.",
                    )
            elif isinstance(node, ast.Call):
                name = _ast.call_name(node)
                if name.endswith(("datetime.utcnow", "datetime.utcfromtimestamp")) or (
                    name.endswith("datetime.now") and not node.args and _ast.kw(node, "tz") is None
                ):
                    hit(
                        "PL004",
                        node,
                        severity="medium",
                        hard=False,
                        title="Naive datetime",
                        explanation="No time zone: comparisons and storage silently mix local "
                        "time and UTC.",
                        suggestion="Use datetime.now(timezone.utc) and keep datetimes aware end "
                        "to end.",
                    )
            elif isinstance(node, ast.Compare):
                pairs = list(
                    zip(
                        [node.left] + node.comparators[:-1], node.ops, node.comparators, strict=True
                    )
                )
                for left, op, right in pairs:
                    if isinstance(op, ast.Is | ast.IsNot) and any(
                        isinstance(x, ast.Constant) and x.value not in (None, True, False, Ellipsis)
                        for x in (left, right)
                    ):
                        hit(
                            "PL006",
                            node,
                            severity="high",
                            hard=True,
                            title="'is' comparison with a literal",
                            explanation="Identity of str/int literals is an implementation "
                            "detail; this is "
                            "sometimes False for equal values.",
                            suggestion="Use == / !=.",
                        )
                    elif isinstance(op, ast.Eq | ast.NotEq) and any(
                        isinstance(x, ast.Constant) and isinstance(x.value, float)
                        for x in (left, right)
                    ):
                        hit(
                            "PL005",
                            node,
                            severity="medium",
                            hard=False,
                            title="Float compared with ==",
                            explanation="0.1 + 0.2 != 0.3; equality on floats fails on rounding.",
                            suggestion="Use math.isclose, or Decimal / integer cents for money.",
                        )
                    elif (
                        isinstance(op, ast.Eq | ast.NotEq | ast.Lt | ast.Gt | ast.LtE | ast.GtE)
                        and not isinstance(left, ast.Constant)
                        and ast.dump(left) == ast.dump(right)
                        and not any(isinstance(c, ast.Call) for c in ast.walk(left))
                    ):
                        hit(
                            "PL010",
                            node,
                            severity="high",
                            hard=True,
                            title="Value compared with itself",
                            explanation="The condition is constant; the intended operand is "
                            "probably missing.",
                            suggestion="Compare against the value you meant.",
                        )
                    elif (
                        isinstance(left, ast.Call)
                        and _ast.call_name(left) == "len"
                        and _ast.is_const(right, 0)
                        and isinstance(op, ast.GtE | ast.Lt)
                    ):
                        hit(
                            "PL008",
                            node,
                            severity="high",
                            hard=True,
                            title="len(...) >= 0 is always true"
                            if isinstance(op, ast.GtE)
                            else "len(...) < 0 is always false",
                            explanation="Length is never negative, so this condition does nothing.",
                            suggestion="Did you mean > 0 or == 0?",
                        )
                    elif isinstance(op, ast.Eq | ast.NotEq) and any(
                        isinstance(x, ast.Call)
                        and isinstance(x.func, ast.Attribute)
                        and x.func.attr == "lower"
                        for x in (left, right)
                    ):
                        hit(
                            "PL012",
                            node,
                            severity="low",
                            hard=False,
                            title="Case-insensitive match with lower()",
                            explanation="lower() misses Unicode folds such as 'ß' vs 'SS'.",
                            suggestion="Use casefold() on both sides.",
                        )
            elif (
                isinstance(node, ast.If)
                and node.orelse
                and not (len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If))
            ):
                if [ast.dump(s) for s in node.body] == [ast.dump(s) for s in node.orelse]:
                    hit(
                        "PL011",
                        node,
                        severity="high",
                        hard=False,
                        title="if and else do the same thing",
                        explanation="The branch makes no difference; one side probably has the "
                        "wrong body.",
                        suggestion="Fix the branch that should differ, or remove the condition.",
                    )

    # Frontend: ids scripts look up that no markup defines, and loose equality.
    defined = set(idx["html_ids"])
    for ref in idx["js_id_refs"]:
        path, n = ref["file"], ref["line"]
        if n in ctx.added.get(path, set()) and ref["id"] not in defined:
            out.append(
                finding(
                    ctx,
                    CHECK,
                    "PL014",
                    path,
                    n,
                    line_of(ctx.head(path) or "", n),
                    severity="medium",
                    hard=False,
                    title=f"Script looks up #{ref['id']}, which no markup defines",
                    explanation="getElementById returns null and the next property access throws.",
                    suggestion="Fix the id, or create the element before looking it up.",
                )
            )
    for f in ctx.changed("frontend"):
        if not f["path"].endswith((".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx")):
            continue
        text = ctx.head(f["path"]) or ""
        for n in sorted(ctx.added[f["path"]]):
            line = line_of(text, n)
            if LOOSE_EQ.search(line) and not line.strip().startswith(("//", "*")):
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "PL015",
                        f["path"],
                        n,
                        line,
                        severity="low",
                        hard=False,
                        title="Loose equality in script",
                        explanation="== coerces types ('0' == 0, '' == 0).",
                        suggestion="Use === / !==.",
                    )
                )
    return out
