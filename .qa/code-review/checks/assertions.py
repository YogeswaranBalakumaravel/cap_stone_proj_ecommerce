"""Assertions that can never fail (BLOCKING): no constant assertions, assertions on the mock
itself, assertion-free tests, try/except that swallows the failure, or tautologies.

Static half only. The objective half, a mutation spot-check, is the test quality agent's job
(.qa/agent): its probes and systematic mutants break the code and confirm a test notices.
"""

from __future__ import annotations

import ast
import re

from cr_common import Ctx, finding, line_of

from . import _ast

CHECK = "assertions_never_fail"
ASSERT_HELPERS = re.compile(r"^(assert|check|verify|expect|ensure)_?\w*$", re.I)
MOCK_FACTORIES = (
    "Mock",
    "MagicMock",
    "AsyncMock",
    "patch",
    "patch.object",
    "mocker.patch",
    "mocker.MagicMock",
    "create_autospec",
    "PropertyMock",
)
JS_TEST = re.compile(r"\b(it|test)\(\s*['\"`]")
JS_LITERAL_EXPECT = re.compile(
    r"expect\(\s*(true|false|null|undefined|\d+|['\"][^'\"]*['\"])\s*\)\s*\.\s*"
    r"(toBe|toEqual|toStrictEqual)\(\s*\1\s*\)"
)


def _is_test(fn) -> bool:
    return fn.name.startswith("test")


def _asserts(fn) -> list:
    """Asserts plus assert-like calls (pytest.raises, self.assertX, mock.assert_*, helpers)."""
    found = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Assert):
            found.append(n)
        elif isinstance(n, ast.Call):
            short = _ast.call_name(n).rsplit(".", 1)[-1]
            if short in ("raises", "warns", "deprecated_call", "fail") or ASSERT_HELPERS.match(
                short
            ):
                found.append(n)
        elif isinstance(n, ast.With):
            for item in n.items:
                if isinstance(item.context_expr, ast.Call) and _ast.call_name(
                    item.context_expr
                ).rsplit(".", 1)[-1] in ("raises", "warns"):
                    found.append(item.context_expr)
    return found


def _mock_names(fn) -> set[str]:
    names = {a.arg for a in fn.args.args if a.arg.startswith("mock")}
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Call)
            and any(_ast.call_name(n.value).endswith(m) for m in MOCK_FACTORIES)
        ):
            names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif (
            isinstance(n, ast.withitem)
            and n.optional_vars is not None
            and isinstance(n.context_expr, ast.Call)
            and any(_ast.call_name(n.context_expr).endswith(m) for m in MOCK_FACTORIES)
        ):
            names |= _ast.names_in(n.optional_vars)
    return names


def _never_fails(test) -> str | None:
    if isinstance(test, ast.Constant):
        return "asserts a constant" if test.value else None
    if isinstance(test, ast.List | ast.Tuple | ast.Dict | ast.Set) and (
        getattr(test, "elts", None) or getattr(test, "keys", None)
    ):
        return "asserts a non-empty literal, which is always truthy"
    if (
        isinstance(test, ast.BoolOp)
        and isinstance(test.op, ast.Or)
        and any(isinstance(v, ast.Constant) and v.value for v in test.values)
    ):
        return "'or True' makes it always pass"
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, op, right = test.left, test.ops[0], test.comparators[0]
        if isinstance(op, ast.Eq | ast.Is | ast.LtE | ast.GtE) and ast.dump(left) == ast.dump(
            right
        ):
            return "compares a value with itself"
        if (
            isinstance(left, ast.Call)
            and _ast.call_name(left) == "len"
            and _ast.is_const(right, 0)
            and isinstance(op, ast.GtE)
        ):
            return "len(...) >= 0 is always true"
        if isinstance(left, ast.Constant) and isinstance(right, ast.Constant):
            return "compares two literals"
    if (
        isinstance(test, ast.Call)
        and _ast.call_name(test) == "isinstance"
        and len(test.args) == 2
        and getattr(test.args[1], "id", "") == "object"
    ):
        return "isinstance(x, object) is always true"
    return None


def run(ctx: Ctx, idx: dict) -> list[dict]:
    out: list[dict] = []
    for f in ctx.changed("test"):
        path, text = f["path"], ctx.head(f["path"]) or ""
        added = ctx.added[path]
        if path.endswith(".py"):
            tree = _ast.parse(text, path)
            if not tree:
                continue
            for fn in _ast.functions(tree):
                if not _is_test(fn) or not _ast.touches(fn, added):
                    continue
                at = line_of(text, fn.lineno)
                asserts = _asserts(fn)
                if _ast.body_is_stub(fn):
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "AS008",
                            path,
                            fn.lineno,
                            at,
                            severity="high",
                            hard=True,
                            title=f"{fn.name} has no body",
                            explanation="An empty test always passes.",
                            suggestion="Write the test or delete it.",
                        )
                    )
                    continue
                if not asserts:
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "AS003",
                            path,
                            fn.lineno,
                            at,
                            severity="high",
                            hard=True,
                            title=f"{fn.name} asserts nothing",
                            explanation="It passes unless the code raises; that's fake coverage.",
                            suggestion="Assert the outcome the requirement describes.",
                        )
                    )
                for a in asserts:
                    if isinstance(a, ast.Assert) and a.lineno in added:
                        why = _never_fails(a.test)
                        if why:
                            out.append(
                                finding(
                                    ctx,
                                    CHECK,
                                    "AS001",
                                    path,
                                    a.lineno,
                                    line_of(text, a.lineno),
                                    severity="high",
                                    hard=True,
                                    title="Assertion that can never fail",
                                    explanation=f"It {why}.",
                                    suggestion="Assert against an independently known expected "
                                    "value.",
                                )
                            )
                mocks = _mock_names(fn)
                real = [
                    a
                    for a in asserts
                    if not (
                        (
                            isinstance(a, ast.Assert)
                            and _ast.names_in(a.test)
                            and _ast.names_in(a.test) <= mocks
                        )
                        or (isinstance(a, ast.Call) and _ast.names_in(a.func) & mocks)
                    )
                ]
                if mocks and asserts and not real:
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "AS004",
                            path,
                            fn.lineno,
                            at,
                            severity="high",
                            hard=True,
                            title=f"{fn.name} only asserts on its mocks",
                            explanation="It proves the mock was called, not that the code under "
                            "test produced the right result.",
                            suggestion="Assert on the return value, response or persisted state.",
                        )
                    )
                for node in ast.walk(fn):
                    if isinstance(node, ast.Try) and any(
                        isinstance(x, ast.Assert | ast.Call) for s in node.body for x in ast.walk(s)
                    ):
                        for h in node.handlers:
                            name = (
                                getattr(h.type, "id", getattr(h.type, "attr", "")) if h.type else ""
                            )
                            silent = all(
                                isinstance(s, ast.Pass | ast.Continue)
                                or (
                                    isinstance(s, ast.Expr)
                                    and isinstance(s.value, ast.Constant | ast.Call)
                                    and not ASSERT_HELPERS.match(
                                        _ast.call_name(s.value).rsplit(".", 1)[-1]
                                        if isinstance(s.value, ast.Call)
                                        else ""
                                    )
                                )
                                for s in h.body
                            )
                            if silent and (
                                h.type is None
                                or name in ("Exception", "BaseException", "AssertionError")
                            ):
                                out.append(
                                    finding(
                                        ctx,
                                        CHECK,
                                        "AS005",
                                        path,
                                        h.lineno,
                                        line_of(text, h.lineno),
                                        severity="high",
                                        hard=True,
                                        title="try/except swallows the test's failure",
                                        explanation="Any assertion error inside the try is caught.",
                                        suggestion="Remove the try, or use pytest.raises for the "
                                        "exception you expect.",
                                    )
                                )
                    elif (
                        isinstance(node, ast.Assert)
                        and node.lineno in added
                        and isinstance(node.test, ast.Compare)
                        and len(node.test.ops) == 1
                        and isinstance(node.test.ops[0], ast.IsNot)
                        and _ast.is_const(node.test.comparators[0], None)
                        and len(asserts) == 1
                    ):
                        out.append(
                            finding(
                                ctx,
                                CHECK,
                                "AS006",
                                path,
                                node.lineno,
                                line_of(text, node.lineno),
                                severity="medium",
                                hard=False,
                                title="Only checks 'is not None'",
                                explanation="Almost any wrong result also passes.",
                                suggestion="Assert the actual value the requirement specifies.",
                            )
                        )
                for dec in fn.decorator_list:
                    src = ast.unparse(dec)
                    if (
                        dec.lineno in added
                        and re.fullmatch(
                            r"(pytest\.)?mark\.skip(\(.*\))?|unittest\.skip\(.*\)", src
                        )
                        and "skipif" not in src
                    ):
                        out.append(
                            finding(
                                ctx,
                                CHECK,
                                "AS007",
                                path,
                                dec.lineno,
                                line_of(text, dec.lineno),
                                severity="medium",
                                hard=False,
                                title=f"{fn.name} is skipped",
                                explanation="A skipped test asserts nothing but still reads as "
                                "coverage.",
                                suggestion="Fix it, or use skipif with a real condition and a "
                                "ticket.",
                            )
                        )
        else:
            for n in sorted(added):
                line = line_of(text, n)
                if JS_LITERAL_EXPECT.search(line):
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "AS001",
                            path,
                            n,
                            line,
                            severity="high",
                            hard=True,
                            title="Assertion that can never fail",
                            explanation="It compares a literal with itself.",
                            suggestion="Assert on the value the code under test produces.",
                        )
                    )
            if JS_TEST.search(text) and "expect(" not in text and "assert" not in text:
                n = next((i for i, ln in enumerate(text.splitlines(), 1) if JS_TEST.search(ln)), 1)
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "AS003",
                        path,
                        n,
                        line_of(text, n),
                        severity="high",
                        hard=True,
                        title="Tests with no expect()",
                        explanation="They pass unless something throws.",
                        suggestion="Assert the outcome the requirement describes.",
                    )
                )
    return out
