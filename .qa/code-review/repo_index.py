#!/usr/bin/env python3
"""A map of the existing code, built without importing or running it. Standard library only.

Gives the agent (and the checks) the shape of the system before it reads the change:
Python symbols with signatures, Flask routes and endpoints, templates and who renders them,
url_for references, element ids in markup and the ids scripts look up, and module imports.
"""

from __future__ import annotations

import ast
import re

from cr_common import Ctx

URL_FOR = re.compile(r"url_for\(\s*['\"]([\w.]+)['\"]")
RENDER = re.compile(r"render_template\(\s*['\"]([^'\"]+)['\"]")
HTML_ID = re.compile(r"\bid\s*=\s*['\"]([\w\-:.]+)['\"]")
JS_ID_REF = re.compile(
    r"getElementById\(\s*['\"]([\w\-:.]+)['\"]\)|querySelector(?:All)?\(\s*['\"]#([\w\-]+)['\"]"
)
MARKUP = (".html", ".htm", ".jinja", ".jinja2", ".j2", ".vue", ".svelte", ".jsx", ".tsx")
SCRIPT = (".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".vue", ".svelte", ".html", ".htm")


def _dotted(module_path: str) -> str:
    return module_path[:-3].replace("/", ".").removesuffix(".__init__")


def _signature(fn) -> str:
    try:
        return f"{fn.name}({ast.unparse(fn.args)})" + (
            f" -> {ast.unparse(fn.returns)}" if fn.returns else ""
        )
    except Exception:  # noqa: BLE001 - unparse can fail on odd syntax; the name is enough
        return fn.name


def python_index(path: str, text: str) -> dict:
    out = {
        "symbols": [],
        "routes": [],
        "blueprints": {},
        "imports": [],
        "url_for": [],
        "renders": [],
    }
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        return out
    module = _dotted(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if (
                getattr(fn, "id", None) or getattr(fn, "attr", None)
            ) == "Blueprint" and node.value.args:
                name = node.value.args[0]
                for t in node.targets:
                    if isinstance(t, ast.Name) and isinstance(name, ast.Constant):
                        out["blueprints"][t.id] = name.value
        elif isinstance(node, ast.Import):
            out["imports"] += [{"module": a.name, "line": node.lineno} for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative import: resolve against this module's package
                pkg = module.split(".") if path.endswith("__init__.py") else module.split(".")[:-1]
                pkg = pkg[: len(pkg) - (node.level - 1)]
                base = ".".join(pkg + ([node.module] if node.module else []))
            out["imports"].append(
                {"module": base, "names": [a.name for a in node.names], "line": node.lineno}
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            doc = (ast.get_docstring(node) or "").strip().splitlines()
            out["symbols"].append(
                {
                    "name": node.name,
                    "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                    "file": path,
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "signature": node.name if isinstance(node, ast.ClassDef) else _signature(node),
                    "doc": doc[0][:120] if doc else "",
                }
            )
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "route":
                        owner = getattr(dec.func.value, "id", "")
                        rule = (
                            dec.args[0].value
                            if dec.args and isinstance(dec.args[0], ast.Constant)
                            else "?"
                        )
                        endpoint = next(
                            (
                                k.value.value
                                for k in dec.keywords
                                if k.arg == "endpoint" and isinstance(k.value, ast.Constant)
                            ),
                            node.name,
                        )
                        methods = ["GET"]
                        for k in dec.keywords:
                            if k.arg == "methods":
                                try:
                                    methods = list(ast.literal_eval(k.value))
                                except ValueError:
                                    methods = ["?"]
                        out["routes"].append(
                            {
                                "owner": owner,
                                "rule": rule,
                                "function": node.name,
                                "endpoint_local": endpoint,
                                "methods": methods,
                                "file": path,
                                "line": dec.lineno,
                            }
                        )
    for i, line in enumerate(text.splitlines(), 1):
        out["url_for"] += [{"endpoint": m, "file": path, "line": i} for m in URL_FOR.findall(line)]
        out["renders"] += [{"template": m, "file": path, "line": i} for m in RENDER.findall(line)]
    for r in out["routes"]:
        bp = out["blueprints"].get(r["owner"])
        r["endpoint"] = f"{bp}.{r['endpoint_local']}" if bp else r["endpoint_local"]
    return out


def endpoints_of(path: str, text: str | None) -> set[str]:
    return {r["endpoint"] for r in python_index(path, text or "")["routes"]} if text else set()


def build(ctx: Ctx) -> dict:
    idx = {
        "symbols": [],
        "routes": [],
        "imports": {},
        "url_for": [],
        "renders": [],
        "templates": [],
        "html_ids": {},
        "js_id_refs": [],
        "modules": [],
    }
    tdirs = [d.rstrip("/") + "/" for d in ctx.cfg["paths"].get("template_dirs", [])]
    for path in ctx.tracked:
        if any(path.startswith(d) for d in tdirs):
            idx["templates"].append(path)
        if path.endswith(".py"):
            text = ctx.head(path) or ""
            p = python_index(path, text)
            idx["modules"].append(_dotted(path))
            idx["symbols"] += p["symbols"]
            idx["routes"] += p["routes"]
            idx["imports"][_dotted(path)] = p["imports"]
            idx["url_for"] += p["url_for"]
            idx["renders"] += p["renders"]
        elif path.endswith(MARKUP + SCRIPT):
            text = ctx.head(path) or ""
            for i, line in enumerate(text.splitlines(), 1):
                idx["url_for"] += [
                    {"endpoint": m, "file": path, "line": i} for m in URL_FOR.findall(line)
                ]
                if path.endswith(MARKUP):
                    for m in HTML_ID.findall(line):
                        idx["html_ids"].setdefault(m, []).append(f"{path}:{i}")
                if path.endswith(SCRIPT):
                    for a, b in JS_ID_REF.findall(line):
                        idx["js_id_refs"].append({"id": a or b, "file": path, "line": i})
    idx["endpoints"] = sorted({r["endpoint"] for r in idx["routes"]} | {"static"})
    return idx


def summary(idx: dict, limit: int = 400) -> dict:
    """The compact version the agent gets: enough to navigate, small enough to read."""
    return {
        "routes": [
            f"{','.join(r['methods'])} {r['rule']} -> {r['endpoint']} ({r['file']}:{r['line']})"
            for r in idx["routes"]
        ],
        "templates": idx["templates"],
        "renders": [f"{r['file']}:{r['line']} renders {r['template']}" for r in idx["renders"]],
        "symbols": [
            f"{s['file']}:{s['line']} {s['kind']} {s['signature']}"
            + (f"  # {s['doc']}" if s["doc"] else "")
            for s in idx["symbols"]
        ][:limit],
    }
