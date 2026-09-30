#!/usr/bin/env python3
"""Systematic mutants on the changed lines of Python source files. Python standard library only.

Complements the agent's probes the way a mutation-testing tool would: it swaps operators and
nudges constants on every changed line, without anyone choosing which edits to try. Uses the
tokenizer, so strings and comments are never touched, and every mutant must still compile.
Lines containing "pragma: no mutate" are skipped.
"""

from __future__ import annotations

import io
import tokenize

SWAPS = {
    "<": "<=",
    "<=": "<",
    ">": ">=",
    ">=": ">",
    "==": "!=",
    "!=": "==",
    "+": "-",
    "-": "+",
    "*": "/",
    "/": "*",
    "and": "or",
    "or": "and",
    "True": "False",
    "False": "True",
}
CATEGORY = {
    "<": "boundary",
    "<=": "boundary",
    ">": "boundary",
    ">=": "boundary",
    "==": "condition",
    "!=": "condition",
    "and": "condition",
    "or": "condition",
    "not": "condition",
    "True": "condition",
    "False": "condition",
    "+": "arithmetic",
    "-": "arithmetic",
    "*": "arithmetic",
    "/": "arithmetic",
}
PRIORITY = {"boundary": 0, "condition": 1, "arithmetic": 2, "constant": 3}


def candidates(text: str, changed_ranges) -> list[dict]:
    """Every single-token mutant on the changed lines, in source order."""
    wanted = {n for a, b in changed_ranges for n in range(a, b + 1)}
    source_lines = text.splitlines()
    found = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return []
    for tok in tokens:
        (line, col), (end_line, end_col) = tok.start, tok.end
        if line != end_line or line not in wanted or "pragma: no mutate" in source_lines[line - 1]:
            continue
        s = tok.string
        if tok.type == tokenize.OP and s in SWAPS:
            replacement, category = SWAPS[s], CATEGORY[s]
        elif tok.type == tokenize.NAME and s in ("and", "or", "True", "False"):
            replacement, category = SWAPS[s], CATEGORY[s]
        elif tok.type == tokenize.NAME and s == "not":
            replacement, category = "", "condition"
        elif tok.type == tokenize.NUMBER and s.isdigit():
            replacement, category = str(int(s) + 1), "constant"
        else:
            continue
        found.append(
            {
                "line": line,
                "col": col,
                "end_col": end_col,
                "original": s,
                "replacement": replacement,
                "category": category,
            }
        )
    return found


def apply(text: str, mutant: dict) -> str:
    lines = text.splitlines(keepends=True)
    src = lines[mutant["line"] - 1]
    lines[mutant["line"] - 1] = (
        src[: mutant["col"]] + mutant["replacement"] + src[mutant["end_col"] :]
    )
    return "".join(lines)


def select(text: str, changed_ranges, limit: int, skip=()) -> list[dict]:
    """Pick up to `limit` mutants that compile: one per changed line first, then a second...

    `skip` holds (line, edited line text) pairs the agent's probes already produced, so the same
    edit is never run twice."""
    skip = set(skip)
    by_line: dict[int, list[dict]] = {}
    for m in candidates(text, changed_ranges):
        mutated = apply(text, m)
        if (m["line"], mutated.splitlines()[m["line"] - 1]) in skip:
            continue
        try:
            compile(mutated, "<mutant>", "exec")
        except (SyntaxError, ValueError):
            continue
        by_line.setdefault(m["line"], []).append(m)
    for group in by_line.values():
        group.sort(key=lambda m: (PRIORITY[m["category"]], m["col"]))
    chosen, round_no = [], 0
    while len(chosen) < limit and any(len(g) > round_no for g in by_line.values()):
        for line in sorted(by_line):
            if round_no < len(by_line[line]) and len(chosen) < limit:
                chosen.append(by_line[line][round_no])
        round_no += 1
    return chosen
