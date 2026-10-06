"""Injection surfaces (BLOCKING): wherever input can reach an interpreter (SQL, shell, eval,
templates, regular expressions, deserialisation, file paths, redirects, outbound URLs, XML, the
DOM), it is parameterised or escaped.

The script enumerates every interpreter sink the PR adds whose argument isn't a literal, and runs
a small intra-function taint pass: Flask route parameters and request.* values, followed through
assignments. A sink reached by tainted data is a hard failure. A dynamic sink whose source the
script can't prove is handed to the agent, which must say tainted (citing the source line, which
the gate checks) or safe (with a reason); without a verdict it needs a human attestation.
"""

from __future__ import annotations

import ast
import re

from cr_common import REQUEST_SOURCE, Ctx, finding, line_of

from . import _ast

CHECK = "injection_surfaces"
SAFE_CONVERTERS = ("int", "float", "uuid")
PY_SINKS = {
    "sql": ("execute", "exec_driver_sql", "text", "raw", "executemany", "executescript"),
    "shell": ("system", "popen", "run", "call", "check_call", "check_output", "Popen", "getoutput"),
    "code": ("eval", "exec", "compile"),
    "template": ("render_template_string", "Markup", "from_string", "Template"),
    "regex": ("compile", "match", "search", "fullmatch", "sub", "split", "findall", "finditer"),
    "deserialisation": ("loads", "load", "decode"),
    "path": ("open", "send_file", "send_from_directory"),
    "redirect": ("redirect",),
    "url": ("get", "post", "put", "delete", "request", "urlopen"),
    "xml": ("fromstring", "parse", "XML"),
}
JS_SINK = re.compile(
    r"(?P<sink>\.(?:inner|outer)HTML\s*\+?=|insertAdjacentHTML\(|document\.write(?:ln)?\(|\beval\(|new\s+Function\("
    r"|\blocation(?:\.href)?\s*=(?!=)|\.setAttribute\(\s*['\"](?:href|src|on\w+)['\"]|\$\([^)]*\)\.html\("
    r"|dangerouslySetInnerHTML|v-html=)(?P<rest>.*)"
)
SAFE_FILTER = re.compile(r"\{\{\s*([^}|]+?)\s*\|\s*safe\b")


def _kind(name: str) -> str | None:
    parts = name.split(".")
    short, owner = parts[-1], ".".join(parts[:-1])
    if short in PY_SINKS["sql"] and (short != "text" or owner in ("", "sqlalchemy", "sa", "db")):
        return "sql"
    if (
        owner in ("os",)
        and short in ("system", "popen")
        or owner == "subprocess"
        and short in PY_SINKS["shell"]
    ):
        return "shell"
    if not owner and short in PY_SINKS["code"]:
        return "code"
    if short in PY_SINKS["template"] and (short != "Template" or owner in ("jinja2", "")):
        return "template"
    if owner == "re" and short in PY_SINKS["regex"]:
        return "regex"
    if (
        owner in ("pickle", "marshal", "yaml", "jsonpickle", "dill")
        and short in PY_SINKS["deserialisation"]
    ):
        return "deserialisation"
    if short in PY_SINKS["path"] and owner in ("", "flask", "io", "builtins"):
        return "path"
    if short == "redirect":
        return "redirect"
    if (
        owner in ("requests", "httpx", "urllib.request", "session", "client")
        and short in PY_SINKS["url"]
        or short == "urlopen"
    ):
        return "url"
    if owner.endswith(("ElementTree", "etree", "ET", "minidom")) and short in PY_SINKS["xml"]:
        return "xml"
    return None


def _sink_arg(kind: str, node: ast.Call):
    if kind == "path" and _ast.call_name(node).endswith("send_from_directory"):
        return node.args[1] if len(node.args) > 1 else _ast.kw(node, "path")
    if kind == "url":
        return node.args[0] if node.args else _ast.kw(node, "url")
    return node.args[0] if node.args else None


def _wraps_same_kind(kind: str, arg) -> bool:
    """execute(text(f"...")) is one sink, reported on text(); skip the outer call."""
    return isinstance(arg, ast.Call) and _kind(_ast.call_name(arg)) == kind


def _route_params(fn) -> dict[str, int]:
    tainted = {}
    for dec in fn.decorator_list:
        if (
            isinstance(dec, ast.Call)
            and getattr(dec.func, "attr", "") == "route"
            and dec.args
            and isinstance(dec.args[0], ast.Constant)
        ):
            for conv, name in re.findall(r"<(?:(\w+):)?(\w+)>", str(dec.args[0].value)):
                if conv not in SAFE_CONVERTERS:
                    tainted[name] = dec.lineno
    return tainted


def _taint(fn, text: str) -> dict[str, int]:
    """name -> line where untrusted data entered it, within one function."""
    tainted = _route_params(fn)
    assigns = sorted(
        (n for n in ast.walk(fn) if isinstance(n, ast.Assign | ast.AnnAssign | ast.AugAssign)),
        key=lambda n: n.lineno,
    )
    for _ in range(3):  # small fixpoint: a, then b = f(a), then c = b + ...
        for node in assigns:
            value = node.value
            if value is None:
                continue
            src = ast.get_source_segment(text, value) or ""
            if REQUEST_SOURCE.search(src) or _ast.names_in(value) & set(tainted):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    for name in _ast.names_in(t):
                        tainted.setdefault(name, node.lineno)
    return tainted


def run(ctx: Ctx, idx: dict) -> list[dict]:
    out: list[dict] = []
    for f in ctx.changed("backend"):
        path = f["path"]
        if not path.endswith(".py"):
            continue
        text = ctx.head(path)
        tree = _ast.parse(text, path)
        if not tree:
            continue
        added = ctx.added[path]
        owner_fn = {}
        for fn in _ast.functions(tree):
            for n in range(fn.lineno, (fn.end_lineno or fn.lineno) + 1):
                owner_fn[n] = fn  # innermost wins because inner functions come later in ast.walk
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _ast.touches(node, added):
                continue
            kind = _kind(_ast.call_name(node))
            arg = _sink_arg(kind, node) if kind else None
            if (
                kind is None
                or arg is None
                or isinstance(arg, ast.Constant)
                or _wraps_same_kind(kind, arg)
            ):
                continue
            if (
                kind == "shell"
                and isinstance(arg, ast.List | ast.Tuple)
                and not _ast.is_const(_ast.kw(node, "shell"), True)
            ):
                continue  # argument vector without a shell: no interpreter
            fn = owner_fn.get(node.lineno)
            taint = _taint(fn, text) if fn else {}
            src = ast.get_source_segment(text, arg) or ""
            hits = sorted(_ast.names_in(arg) & set(taint))
            direct = REQUEST_SOURCE.search(src)
            line = line_of(text, node.lineno)
            if hits or direct:
                src_line = node.lineno if direct else taint[hits[0]]
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "INJ001",
                        path,
                        node.lineno,
                        line,
                        severity="blocker",
                        hard=True,
                        title=f"Untrusted input reaches a {kind} sink",
                        explanation=f"{'Request data' if direct else repr(hits[0])} flows into "
                        f"{_ast.call_name(node)}() unescaped"
                        + ("" if direct else f" (enters at line {src_line})")
                        + ".",
                        suggestion=_fix(kind),
                        related={"file": path, "line": src_line},
                    )
                )
                out[-1]["sink"] = {"kind": kind, "status": "tainted"}
            else:
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "INJ002",
                        path,
                        node.lineno,
                        line,
                        severity="high",
                        hard=False,
                        title=f"Dynamic {kind} sink: source not proven safe",
                        explanation=f"{_ast.call_name(node)}() receives a computed value the "
                        "script "
                        "can't trace. Needs a taint verdict.",
                        suggestion=_fix(kind),
                    )
                )
                out[-1]["sink"] = {"kind": kind, "status": "unknown"}

    for f in ctx.changed("frontend"):
        text = ctx.head(f["path"]) or ""
        for n in sorted(ctx.added[f["path"]]):
            line = line_of(text, n)
            m = JS_SINK.search(line)
            kind = "dom"
            if not m:
                s = SAFE_FILTER.search(line)
                if not s:
                    continue
                kind, rest = "template", s.group(1)
            else:
                rest = m.group("rest")
                if re.fullmatch(r"\s*(['\"])[^'\"$]*\1\s*;?\s*\)?\s*;?\s*", rest or ""):
                    continue  # a plain literal
            tainted = REQUEST_SOURCE.search(rest or "") or "request." in (rest or "")
            out.append(
                finding(
                    ctx,
                    CHECK,
                    "INJ001" if tainted else "INJ002",
                    f["path"],
                    n,
                    line,
                    severity="blocker" if tainted else "high",
                    hard=bool(tainted),
                    title=(
                        f"Untrusted input reaches a {kind} sink"
                        if tainted
                        else f"Dynamic {kind} sink: source not proven safe"
                    ),
                    explanation=(
                        "The value comes straight from the URL, a form field or a response."
                        if tainted
                        else "HTML is built from a value the script can't trace."
                    ),
                    suggestion=_fix(kind),
                )
            )
            out[-1]["sink"] = {"kind": kind, "status": "tainted" if tainted else "unknown"}
    return out


def _fix(kind: str) -> str:
    return {
        "sql": "Bind parameters (text(':x').bindparams / ORM filters); never splice values into "
        "SQL.",
        "shell": "Pass an argument list with shell=False; validate against an allowlist.",
        "code": "Remove eval/exec; parse with json or ast.literal_eval, or dispatch via a dict.",
        "template": "Render a template file with autoescaping; never mark user data safe.",
        "regex": "re.escape() the input, or match it as a plain string.",
        "deserialisation": "Use json / yaml.safe_load for anything not produced by this service.",
        "path": "Use send_from_directory with a fixed directory, or validate with werkzeug's "
        "safe_join.",
        "redirect": "Redirect only to url_for() targets or an allowlist of hosts.",
        "url": "Allowlist hosts and schemes before fetching (SSRF).",
        "xml": "Use defusedxml or disable entity resolution.",
        "dom": "Use textContent / createElement, or sanitise before inserting HTML.",
    }.get(kind, "Parameterise or escape the value.")
