"""Error handling (STANDARD): no swallowed exceptions, failures propagate, I/O has timeouts.

Mirrors the Ruff BLE / TRY / S110 / S112 intent plus the timeout rule Ruff lacks, without
needing those rule sets enabled.
"""

from __future__ import annotations

import ast
import re

from cr_common import Ctx, finding, line_of

from . import _ast

CHECK = "error_handling"
NET_CALLS = (
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.patch",
    "requests.delete",
    "requests.head",
    "requests.request",
    "httpx.get",
    "httpx.post",
    "httpx.put",
    "httpx.delete",
    "httpx.request",
    "urlopen",
    "urllib.request.urlopen",
    "smtplib.SMTP",
    "smtplib.SMTP_SSL",
    "socket.create_connection",
    "http.client.HTTPConnection",
    "http.client.HTTPSConnection",
)
LOG_CALLS = re.compile(r"(^|\.)(debug|info|warning|warn|error|exception|critical|print|log)$")


def _broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    return t is None or (isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"))


def _swallows(handler: ast.ExceptHandler) -> str | None:
    """'silent', 'logged' (logs then carries on) or None (re-raises / returns an error)."""
    body = handler.body
    if any(isinstance(n, ast.Raise) for n in ast.walk(handler)):
        return None
    if all(
        isinstance(s, ast.Pass | ast.Continue)
        or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
        for s in body
    ):
        return "silent"
    if all(
        isinstance(s, ast.Expr)
        and isinstance(s.value, ast.Call)
        and LOG_CALLS.search(_ast.call_name(s.value))
        for s in body
    ):
        return "logged"
    if (
        len(body) == 1
        and isinstance(body[0], ast.Return)
        and (body[0].value is None or _ast.is_const(body[0].value, None))
    ):
        return "silent"
    return None


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

        def hit(rule, n, added=added, path=path, text=text, **kw):
            if n in added:
                out.append(finding(ctx, CHECK, rule, path, n, line_of(text, n), **kw))

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                how = _swallows(node)
                if how and (_broad(node) or how == "silent"):
                    hit(
                        "EH001",
                        node.lineno,
                        severity="high" if _broad(node) else "medium",
                        hard=False,
                        title="Exception swallowed"
                        if how == "silent"
                        else "Exception logged and ignored",
                        explanation="The failure disappears; the caller carries on with bad or "
                        "missing state.",
                        suggestion="Catch the specific exception, and re-raise or return a "
                        "deliberate error.",
                    )
                if node.name and any(
                    isinstance(r, ast.Return)
                    and r.value is not None
                    and node.name in _ast.names_in(r.value)
                    for r in ast.walk(node)
                ):
                    hit(
                        "EH004",
                        node.lineno,
                        severity="medium",
                        hard=False,
                        title="Exception text returned to caller",
                        explanation="Leaks internals (paths, SQL, stack details) to the client.",
                        suggestion="Log the exception; return a generic message and a proper "
                        "status code.",
                    )
            elif isinstance(node, ast.Call):
                name = _ast.call_name(node)
                if (
                    any(name == c or name.endswith("." + c) for c in NET_CALLS)
                    and _ast.kw(node, "timeout") is None
                ):
                    hit(
                        "EH002",
                        node.lineno,
                        severity="medium",
                        hard=False,
                        title=f"{name}() without a timeout",
                        explanation="A slow upstream hangs this worker indefinitely.",
                        suggestion="Pass timeout= (and retry with backoff where the call is "
                        "idempotent).",
                    )
            elif isinstance(node, ast.Try) and node.finalbody:
                for s in node.finalbody:
                    for r in ast.walk(s):
                        if isinstance(r, ast.Return | ast.Break | ast.Continue):
                            hit(
                                "EH005",
                                r.lineno,
                                severity="high",
                                hard=False,
                                title="return/break in finally",
                                explanation="It silently discards any exception raised in the try "
                                "block.",
                                suggestion="Move it out of finally.",
                            )
        for fn in _ast.functions(tree):
            if not _ast.touches(fn, added):
                continue
            commits = [
                c
                for c in ast.walk(fn)
                if isinstance(c, ast.Call) and _ast.call_name(c).endswith("session.commit")
            ]
            rollback = any(
                isinstance(c, ast.Call) and _ast.call_name(c).endswith("rollback")
                for c in ast.walk(fn)
            )
            if commits and not rollback:
                hit(
                    "EH003",
                    commits[0].lineno,
                    severity="medium",
                    hard=False,
                    title="commit() without rollback",
                    explanation="If the commit fails, the session stays in a failed state for the "
                    "next request.",
                    suggestion="Wrap in try/except: db.session.rollback(), then re-raise or "
                    "return an error.",
                )

    for f in ctx.changed("frontend"):
        text = ctx.head(f["path"]) or ""
        if "fetch(" not in text:
            continue
        for n in sorted(ctx.added[f["path"]]):
            line = line_of(text, n)
            if re.search(r"\bfetch\(", line):
                window = "\n".join(text.splitlines()[max(0, n - 15) : n + 15])
                if ".catch(" not in window and not re.search(r"\btry\s*\{", window):
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "EH006",
                            f["path"],
                            n,
                            line,
                            severity="medium",
                            hard=False,
                            title="fetch() without error handling",
                            explanation="A network error or non-2xx response leaves the UI stuck "
                            "with no message.",
                            suggestion="Check response.ok and handle failures (.catch or "
                            "try/catch).",
                        )
                    )
    return out
