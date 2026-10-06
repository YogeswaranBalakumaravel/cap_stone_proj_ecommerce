"""Tests assert requirements, not the implementation (BLOCKING, named human attestation).

When one model writes the code and its tests, the tests inherit its blind spots. Mechanical
signals of implementation-mirroring: the expected value is computed by the code under test, the
test re-implements the algorithm, imports private helpers or patches the unit's own internals,
and changed behaviour no test touches. The objective half comes from the separate-context oracle
pass (criteria + signatures only, never the implementation), which the review compares the
tests against.
"""

from __future__ import annotations

import ast
import re

from cr_common import Ctx, classify, finding, line_of

from . import _ast
from .duplication import _jaccard, _shingles

CHECK = "tests_assert_requirements"


def _prod_names(tree, ctx: Ctx) -> set[str]:
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and not n.level:
            top = n.module.split(".")[0]
            if top in ctx.local_top_modules and top not in ("tests", "test"):
                names |= {a.asname or a.name for a in n.names}
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in ctx.local_top_modules and not a.name.startswith("tests"):
                    names.add((a.asname or a.name).split(".")[0])
    return names


def _calls_prod(node, prod: set[str]) -> set[str]:
    out = set()
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            root = _ast.call_name(c).split(".")[0]
            if root in prod:
                out.add(_ast.call_name(c))
    return out


def run(ctx: Ctx, idx: dict) -> list[dict]:
    out: list[dict] = []
    changed_fns = []  # every function in a changed backend file
    for f in ctx.changed("backend"):
        if f["path"].endswith(".py"):
            tree = _ast.parse(ctx.head(f["path"]), f["path"])
            changed_fns += [(f["path"], fn) for fn in (_ast.functions(tree) if tree else [])]
    prod_fns = [(p, fn) for p, fn in changed_fns if (fn.end_lineno or fn.lineno) - fn.lineno >= 3]
    test_texts = {p: ctx.head(p) or "" for p in ctx.tracked if classify(p, ctx.cfg) == "test"}

    for f in ctx.changed("test"):
        path = f["path"]
        if not path.endswith(".py"):
            continue
        text = ctx.head(path)
        tree = _ast.parse(text, path)
        if not tree:
            continue
        added = ctx.added[path]
        prod = _prod_names(tree, ctx)

        for n in ast.walk(tree):
            if (
                isinstance(n, ast.ImportFrom)
                and n.lineno in added
                and n.module
                and n.module.split(".")[0] in ctx.local_top_modules
            ):
                private = [
                    a.name
                    for a in n.names
                    if a.name.startswith("_") and not a.name.startswith("__")
                ]
                if private:
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "TR002",
                            path,
                            n.lineno,
                            line_of(text, n.lineno),
                            severity="medium",
                            hard=False,
                            title=f"Test imports private {private[0]}",
                            explanation="Tests pinned to private helpers assert how the code "
                            "works, "
                            "not what the requirement says it must do.",
                            suggestion="Test through the public route or function the criterion "
                            "names.",
                        )
                    )
            elif (
                isinstance(n, ast.Call)
                and n.lineno in added
                and _ast.call_name(n).endswith(("patch", "patch.object"))
                and n.args
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)
            ):
                target = n.args[0].value
                if target.split(".")[0] in ctx.local_top_modules:
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "TR003",
                            path,
                            n.lineno,
                            line_of(text, n.lineno),
                            severity="medium",
                            hard=False,
                            title=f"Test patches {target}",
                            explanation="Patching the unit's own internals replaces the behaviour "
                            "the "
                            "test should verify.",
                            suggestion="Mock only true boundaries (network, clock, third-party "
                            "APIs).",
                        )
                    )

        for fn in _ast.functions(tree):
            if not fn.name.startswith("test") or not _ast.touches(fn, added):
                continue
            expected_from_prod = {}
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign) and _calls_prod(n.value, prod):
                    for t in n.targets:
                        for name in _ast.names_in(t):
                            if re.search(r"expect|want|wanted|should|correct", name, re.I):
                                expected_from_prod[name] = n.lineno
            for n in ast.walk(fn):
                if not (
                    isinstance(n, ast.Assert)
                    and isinstance(n.test, ast.Compare)
                    and n.lineno in added
                ):
                    continue
                left, right = n.test.left, n.test.comparators[0]
                same = _calls_prod(left, prod) & _calls_prod(right, prod)
                via_var = _ast.names_in(n.test) & set(expected_from_prod)
                if same or via_var:
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "TR001",
                            path,
                            n.lineno,
                            line_of(text, n.lineno),
                            severity="high",
                            hard=False,
                            title="Expected value computed by the code under test",
                            explanation=(
                                f"Both sides call {sorted(same)[0]}()"
                                if same
                                else f"'{sorted(via_var)[0]}' comes from production code"
                            )
                            + "; the assertion passes whatever that code returns.",
                            suggestion="Hard-code the expected value the acceptance criterion "
                            "states.",
                        )
                    )
            mine = _shingles(fn)
            for ppath, pfn in prod_fns:
                score = _jaccard(mine, _shingles(pfn))
                if score >= 0.6:
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "TR006",
                            path,
                            fn.lineno,
                            line_of(text, fn.lineno),
                            severity="high",
                            hard=False,
                            title=f"{fn.name} re-implements {pfn.name}() ({score:.0%} similar)",
                            explanation="A test that recomputes the answer the same way shares the "
                            "implementation's bugs.",
                            suggestion="Derive expected values from the requirement, not the "
                            "algorithm.",
                            related={"file": ppath, "line": pfn.lineno},
                        )
                    )
                    break

    # Changed behaviour that no test touches at all.
    corpus = "\n".join(test_texts.values())
    for ppath, pfn in changed_fns:
        if _ast.body_is_stub(pfn):
            continue  # reported as a placeholder under requirement coverage
        if pfn.decorator_list and not any(
            getattr(getattr(d, "func", d), "attr", "") == "route" for d in pfn.decorator_list
        ):
            continue  # error handlers, hooks, fixtures: exercised indirectly
        if pfn.lineno not in ctx.added.get(ppath, set()) and not any(
            pfn.lineno <= n <= (pfn.end_lineno or pfn.lineno) for n in ctx.added.get(ppath, set())
        ):
            continue
        rules = [
            r["rule"] for r in idx["routes"] if r["file"] == ppath and r["function"] == pfn.name
        ]
        hit = re.search(rf"\b{re.escape(pfn.name)}\b", corpus) or any(
            re.search(
                re.sub(
                    r"<[^>]+>",
                    r"[^'\"/?]+",
                    re.escape(rule).replace(r"\<", "<").replace(r"\>", ">"),
                )
                + r"(?:['\"?/]|$)",
                corpus,
            )
            for rule in rules
            if rule != "?"
        )
        if not hit and not pfn.name.startswith("_"):
            text = ctx.head(ppath) or ""
            out.append(
                finding(
                    ctx,
                    CHECK,
                    "TR005",
                    ppath,
                    pfn.lineno,
                    line_of(text, pfn.lineno),
                    severity="medium",
                    hard=False,
                    title=f"No test exercises {pfn.name}()" + (f" ({rules[0]})" if rules else ""),
                    explanation="Changed behaviour with no test can't be traced to a requirement.",
                    suggestion="Add a test written from the acceptance criterion.",
                )
            )
    return out
