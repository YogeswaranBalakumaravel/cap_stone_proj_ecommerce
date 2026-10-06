"""Code duplication and drift (STANDARD): no reimplementation of what already exists, and the
layering rules hold.

Three mechanical signals, all from the standard library: functions whose normalised structure
matches an existing one (renamed variables don't hide it), copied blocks of six or more
significant lines in any language (jscpd-style), and import contracts between layers
(import-linter-style, configured in `architecture.contracts`).
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections import defaultdict

from cr_common import Ctx, finding, line_of

from . import _ast

CHECK = "duplication_drift"
TRIVIAL = re.compile(
    r"^([{}()\[\];,]*|#.*|//.*|/?\*.*|<!--.*|\{#.*|import .*|from .* import .*|@.*|"
    r"return|pass|else:|try:|finally:|\{%-?\s*end\w*\s*-?%\}|</\w+>)$"
)
CODE_EXT = (
    ".py",
    ".js",
    ".mjs",
    ".ts",
    ".jsx",
    ".tsx",
    ".vue",
    ".html",
    ".htm",
    ".j2",
    ".jinja",
    ".css",
    ".scss",
)


class _Normalise(ast.NodeTransformer):
    """Rename locals and arguments to placeholders so renamed copies still match."""

    def __init__(self):
        self.names: dict[str, str] = {}

    def _n(self, name):
        return self.names.setdefault(name, f"v{len(self.names)}")

    def visit_Name(self, node):
        return ast.copy_location(ast.Name(id=self._n(node.id), ctx=node.ctx), node)

    def visit_arg(self, node):
        node.arg = self._n(node.arg)
        node.annotation = None
        return node


def _shingles(fn) -> set[str]:
    clone = ast.parse(ast.unparse(fn)).body[0]
    clone.name = "f"
    clone.decorator_list = []
    if (
        clone.body
        and isinstance(clone.body[0], ast.Expr)
        and isinstance(getattr(clone.body[0], "value", None), ast.Constant)
    ):
        clone.body = clone.body[1:] or [ast.Pass()]
    tokens = re.findall(r"\w+|[^\w\s]", ast.unparse(_Normalise().visit(clone)))
    return {" ".join(tokens[i : i + 4]) for i in range(max(1, len(tokens) - 3))}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _significant(text: str) -> list[tuple[int, str]]:
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        s = re.sub(r"\s+", " ", raw.strip())
        if len(s) >= 4 and not TRIVIAL.match(s):
            out.append((n, s))
    return out


def run(ctx: Ctx, idx: dict) -> list[dict]:
    out: list[dict] = []
    dcfg = ctx.cfg["duplication"]
    min_fn, sim = (
        int(dcfg.get("min_function_lines", 5)),
        float(dcfg.get("function_similarity", 0.85)),
    )

    # 1. Functions that restate an existing function.
    existing = []
    for path in ctx.tracked:
        if path.endswith(".py") and not path.startswith(".qa/"):
            tree = _ast.parse(ctx.head(path), path)
            for fn in _ast.functions(tree) if tree else []:
                if (fn.end_lineno or fn.lineno) - fn.lineno + 1 >= min_fn:
                    existing.append((path, fn))
    cache = {}
    for f in ctx.changed("backend"):
        path = f["path"]
        for _, fn in [e for e in existing if e[0] == path]:
            if fn.lineno not in ctx.added[path]:
                continue
            mine = cache.setdefault((path, fn.lineno), _shingles(fn))
            best = None
            for p3, other in existing:
                if (p3, other.lineno) == (path, fn.lineno):
                    continue
                score = _jaccard(mine, cache.setdefault((p3, other.lineno), _shingles(other)))
                if score >= sim and (not best or score > best[0]):
                    best = (score, p3, other)
            if best:
                score, p3, other = best
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "DD001",
                        path,
                        fn.lineno,
                        line_of(ctx.head(path), fn.lineno),
                        severity="medium",
                        hard=False,
                        title=f"{fn.name}() duplicates {other.name}() ({score:.0%} similar)",
                        explanation=f"Same structure as {p3}:{other.lineno}. AI recreates what it "
                        "can't see in context, and the copies then drift apart.",
                        suggestion=f"Reuse or extend {other.name}() instead.",
                        related={"file": p3, "line": other.lineno},
                    )
                )

    # 2. Copied blocks of N significant lines, in any language.
    window = int(dcfg.get("min_block_lines", 6))
    seen: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path in ctx.tracked:
        if path.endswith(CODE_EXT) and ctx.cfg and not path.startswith((".qa/", ".github/")):
            sig = _significant(ctx.head(path) or "")
            for i in range(len(sig) - window + 1):
                key = hashlib.sha1(
                    "\n".join(s for _, s in sig[i : i + window]).encode()
                ).hexdigest()
                seen[key].append((path, sig[i][0]))
    pairs: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(
        list
    )  # (mine, theirs) -> [(my line, their line)]
    for f in ctx.changed("backend", "frontend"):
        path = f["path"]
        sig = _significant(ctx.head(path) or "")
        for i in range(len(sig) - window + 1):
            block = sig[i : i + window]
            if not all(n in ctx.added[path] for n, _ in block):
                continue
            key = hashlib.sha1("\n".join(s for _, s in block).encode()).hexdigest()
            others = [
                (p, n) for p, n in seen[key] if not (p == path and abs(n - block[0][0]) < window)
            ]
            if others:
                pairs[(path, others[0][0])].append((block[0][0], others[0][1]))
    reported = set()
    for (path, other), hits in sorted(pairs.items()):
        if frozenset((path, other)) in reported and path != other:
            continue  # a full audit sees both sides; report the pair once
        reported.add(frozenset((path, other)))
        copied = len({n + k for n, _ in hits for k in range(window)})
        first, theirs = hits[0]
        out.append(
            finding(
                ctx,
                CHECK,
                "DD002",
                path,
                first,
                line_of(ctx.head(path) or "", first),
                severity="medium",
                hard=False,
                title=f"~{copied} lines copied from {other}"
                + (f" ({len(hits)} blocks)" if len(hits) > 1 else ""),
                explanation=f"Starting here and at {other}:{theirs}. Duplicated logic means two "
                "places to fix, and they drift.",
                suggestion="Extract the shared part (a function, Jinja macro or partial, a shared "
                "script).",
                related={"file": other, "line": theirs},
            )
        )

    # 3. Layer contracts.
    by_module = idx["imports"]
    for c in ctx.cfg.get("architecture", {}).get("contracts", []):
        for module, imports in by_module.items():
            if module != c["source"] and not module.startswith(c["source"] + "."):
                continue
            path = module.replace(".", "/") + ".py"
            if path not in ctx.added:
                path = module.replace(".", "/") + "/__init__.py"
            for imp in imports:
                target = imp["module"]
                if imp["line"] in ctx.added.get(path, set()) and any(
                    target == fb or target.startswith(fb + ".") for fb in c["forbidden"]
                ):
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "DD003",
                            path,
                            imp["line"],
                            line_of(ctx.head(path) or "", imp["line"]),
                            severity="high",
                            hard=False,
                            title=f"Layer contract broken: {c['name']}",
                            explanation=f"{module} must not import {target}.",
                            suggestion="Move the shared piece down a layer, or invert the "
                            "dependency.",
                        )
                    )

    # 4. A new function with the same name as one elsewhere: reimplementation or confusing drift.
    names = defaultdict(list)
    for s in idx["symbols"]:
        if (
            s["kind"] == "function"
            and not s["name"].startswith("__")
            and not s["file"].startswith(("tests/", ".qa/"))
        ):
            names[s["name"]].append(s)
    for s in idx["symbols"]:
        if (
            s["kind"] == "function"
            and s["line"] in ctx.added.get(s["file"], set())
            and not s["name"].startswith("test")
        ):
            twins = [t for t in names[s["name"]] if t["file"] != s["file"]]
            if twins:
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "DD004",
                        s["file"],
                        s["line"],
                        line_of(ctx.head(s["file"]) or "", s["line"]),
                        severity="low",
                        hard=False,
                        title=f"{s['name']}() also exists in {twins[0]['file']}",
                        explanation="Two functions with one name usually means one reimplements "
                        "the other.",
                        suggestion="Reuse the existing one, or rename to make the difference "
                        "clear.",
                        related={"file": twins[0]["file"], "line": twins[0]["line"]},
                    )
                )
    return out
