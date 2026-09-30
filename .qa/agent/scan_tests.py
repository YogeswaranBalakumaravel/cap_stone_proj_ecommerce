#!/usr/bin/env python3
"""Static scan of the assertions in the tests a PR adds or changes. Python standard library only.

Deterministic backstop for "are the tests merely checking trivial assertions?". It flags:
  - no_assertion: the test has no assert, assert* call or raises-check at all,
                  so it can't fail on a wrong result
  - weak_only:    every assertion is a weak form (is not None, bare truthiness,
                  isinstance, "was called")
Only Python test files are scanned. For other languages the agent's judgement is used alone.
"""

from __future__ import annotations

import ast

WEAK_METHODS = {
    "assertIsNotNone",
    "assertIsNone",
    "assertIsInstance",
    "assertNotIsInstance",
    "assert_called",
    "assert_called_once",
    "assert_any_call",
}
RAISES_METHODS = {
    "assertRaises",
    "assertRaisesRegex",
    "assertWarns",
    "assertWarnsRegex",
    "raises",
    "warns",
}
# assertTrue / assertFalse are strong only when they wrap a comparison.
TRUTHY_METHODS = {"assertTrue", "assertFalse"}


def _is_none(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _strong_expr(node) -> bool:
    """A comparison that pins a value, rather than mere truthiness or not-None."""
    if isinstance(node, ast.Compare):
        return not all(
            isinstance(op, ast.Is | ast.IsNot) and _is_none(c)
            for op, c in zip(node.ops, node.comparators, strict=False)
        )
    if isinstance(node, ast.BoolOp):
        return any(_strong_expr(v) for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _strong_expr(node.operand)
    return False


def _classify_test(func) -> tuple[int, int]:
    """Return (assertions, strong assertions) in one test function."""
    total = strong = 0
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            total += 1
            strong += _strong_expr(node.test)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute | ast.Name):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            if name in RAISES_METHODS:
                total += 1
                strong += 1
            elif name in TRUTHY_METHODS:
                total += 1
                strong += bool(node.args) and _strong_expr(node.args[0])
            elif name in WEAK_METHODS:
                total += 1
            elif name.startswith("assert"):
                total += 1  # assertEqual, assertIn, assert_called_with, assert_frame_equal ...
                strong += 1
    return total, strong


def scan_file(rel: str, text: str, changed_ranges) -> list[dict]:
    try:
        tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError):
        return []
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or not node.name.startswith(
            "test"
        ):
            continue
        start = min([d.lineno for d in node.decorator_list] + [node.lineno])
        end = getattr(node, "end_lineno", None) or node.lineno
        if changed_ranges is not None and not any(
            a <= end and b >= start for a, b in changed_ranges
        ):
            continue  # only tests this PR added or changed
        total, strong = _classify_test(node)
        verdict = "no_assertion" if total == 0 else ("weak_only" if strong == 0 else "ok")
        found.append(
            {
                "file": rel,
                "line": node.lineno,
                "end_line": end,
                "name": node.name,
                "assertions": total,
                "strong_assertions": strong,
                "verdict": verdict,
            }
        )
    return sorted(found, key=lambda t: t["line"])


def scan(repo, changed_files: list[dict]) -> list[dict]:
    results = []
    for f in changed_files:
        if f.get("category") != "test" or f.get("status") == "D" or not f["path"].endswith(".py"):
            continue
        try:
            text = (repo / f["path"]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        ranges = f.get("changed_lines") if f.get("status") != "A" else None
        results += scan_file(f["path"], text, ranges)
    return results
