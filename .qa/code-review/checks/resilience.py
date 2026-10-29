"""Performance and resilience (STANDARD): query counts, pagination, rate limits, timeouts, retries.

Not a Stream A row of the checklist; a local eleventh check. Every rule is a signal for the
reviewer (`hard=False`): the script sees one file at a time and can't know about app-wide limits,
eager loading set elsewhere, or a gateway in front of the service.
"""

from __future__ import annotations

import ast
import re

from cr_common import Ctx, finding, line_of

from . import _ast

CHECK = "performance_resilience"
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
# One database round trip each: Model.query.*, db.session.get/execute/scalar(s)/query, *_or_404.
DB_CALL = re.compile(
    r"(^|\.)query\.\w+$|(^|\.)session\.(get|execute|scalars?|query)$"
    r"|(^|\.)(get_or_404|first_or_404|one_or_404)$|(^|\.)cursor\.execute$"
)
LIST_CALL = re.compile(
    r"(^|\.)query(\..*)?\.all$|(^|\.)session\.(execute|scalars)\(\)(\..*)?\.all$|\.fetchall$"
)
PAGED = re.compile(r"(^|\.)(limit|paginate|offset|fetchmany|slice)$")
ROUTE_DECORATORS = ("route", "get", "post", "put", "patch", "delete")
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
AUTH_ROUTE = re.compile(
    r"log[-_]?in|sign[-_]?in|sign[-_]?up|register|auth|token|password|passwd|reset|otp|2fa|mfa",
    re.I,
)
CART_ROUTE = re.compile(r"cart|basket|checkout|order", re.I)
RATE_LIMIT = re.compile(r"limit|throttl", re.I)
# Payment and shipping providers, and the words their integrations use.
PAY_SHIP = re.compile(
    r"(?<![a-z])(pay|payments?|payouts?|paypal|stripe|razorpay|braintree|adyen|charges?|refunds?"
    r"|checkout|ship|shipping|shipments?|fedex|ups|dhl|usps|easypost|shippo|couriers?|delivery)"
    r"(?![a-z])",
    re.I,
)
SDK_CALL = re.compile(
    r"^(stripe|razorpay|braintree|paypalrestsdk|adyen|easypost|shippo)\.", re.I
)
RESILIENT = re.compile(r"retr(y|ies)|backoff|tenacity|stamina|circuit|breaker", re.I)


def _is_net(name: str) -> bool:
    return any(name == c or name.endswith("." + c) for c in NET_CALLS)


def _route(fn) -> tuple[str, set[str], int] | None:
    """(rule, methods, decorator line) for a Flask view, else None."""
    for dec in fn.decorator_list:
        if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
            continue
        kind = dec.func.attr
        if kind not in ROUTE_DECORATORS:
            continue
        rule = dec.args[0].value if dec.args and _ast.is_const(dec.args[0]) else ""
        if not isinstance(rule, str):
            continue
        if kind != "route":
            return rule, {kind.upper()}, dec.lineno
        methods = _ast.kw(dec, "methods")
        try:
            names = {str(m).upper() for m in ast.literal_eval(methods)} if methods else {"GET"}
        except ValueError:
            names = {"?"}
        return rule, names, dec.lineno
    return None


def _loop_bodies(tree):
    """Nodes that run once per iteration: loop bodies and comprehension elements."""
    for node in ast.walk(tree):
        if isinstance(node, ast.For | ast.AsyncFor | ast.While):
            yield from node.body
        elif isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp):
            yield node.elt
        elif isinstance(node, ast.DictComp):
            yield node.key
            yield node.value


def _calls(node) -> list[ast.Call]:
    return [c for c in ast.walk(node) if isinstance(c, ast.Call)]


def _reachable(fn, fns: dict) -> list:
    """fn and the functions in the same file it calls by name, directly or through others."""
    seen, todo = {fn.name: fn}, [fn]
    while todo:
        for c in _calls(todo.pop()):
            name = c.func.id if isinstance(c.func, ast.Name) else None
            if name in fns and name not in seen:
                seen[name] = fns[name]
                todo.append(fns[name])
    return list(seen.values())


def run(ctx: Ctx, idx: dict) -> list[dict]:
    out: list[dict] = []
    limits_app_wide = any(
        "default_limits" in (ctx.head(p) or "") for p in ctx.tracked if p.endswith(".py")
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

        def hit(rule, n, added=added, path=path, text=text, **kw):
            if n in added:
                out.append(finding(ctx, CHECK, rule, path, n, line_of(text, n), **kw))

        # PF001: a query per loop iteration (N+1).
        for body in _loop_bodies(tree):
            for c in _calls(body):
                name = _ast.call_name(c)
                if DB_CALL.search(name):
                    hit(
                        "PF001",
                        c.lineno,
                        severity="medium",
                        hard=False,
                        title=f"{name}() inside a loop",
                        explanation="One database query per item: the request makes N+1 queries "
                        "and slows down as the data grows.",
                        suggestion="Fetch everything in one query (filter with in_(), join, or "
                        "selectinload/joinedload) before the loop.",
                    )

        fns = {fn.name: fn for fn in _ast.functions(tree)}
        for fn in fns.values():
            route = _route(fn)
            # PF004 / PF005: outbound calls.
            nets = [c for c in _calls(fn) if _is_net(_ast.call_name(c))]
            for c in nets:
                if _ast.kw(c, "timeout") is None:
                    hit(
                        "PF004",
                        c.lineno,
                        severity="medium",
                        hard=False,
                        title=f"{_ast.call_name(c)}() without a timeout",
                        explanation="A slow upstream hangs this worker indefinitely.",
                        suggestion="Pass timeout= (connect and read).",
                    )
            src = ast.get_source_segment(text, fn) or ""
            sdk = [c for c in _calls(fn) if SDK_CALL.search(_ast.call_name(c))]
            if (sdk or (nets and PAY_SHIP.search(src))) and not RESILIENT.search(text):
                c = (sdk or nets)[0]
                hit(
                    "PF005",
                    c.lineno,
                    severity="medium",
                    hard=False,
                    title="Payment or shipping call without retry or circuit breaker",
                    explanation="A brief provider outage fails the order outright, and a "
                    "provider that stays down ties up every worker waiting on it.",
                    suggestion="Retry transient errors with backoff (with an idempotency key for "
                    "charges), and stop calling for a while after repeated failures.",
                )
            if not route:
                continue
            rule, methods, dec_line = route

            # PF002: a GET list endpoint that returns every row.
            if "GET" in methods and "<" not in rule:
                scanned = _reachable(fn, fns)
                lists = [
                    c for s in scanned for c in _calls(s) if LIST_CALL.search(_ast.call_name(c))
                ]
                paged = any(
                    PAGED.search(_ast.call_name(c)) for s in scanned for c in _calls(s)
                )
                if lists and not paged:
                    new = [c for c in lists if c.lineno in added]
                    hit(
                        "PF002",
                        new[0].lineno if new else dec_line,
                        severity="medium",
                        hard=False,
                        title=f"List endpoint {rule} isn't paginated",
                        explanation="It returns every row: response size and query time grow "
                        "with the table.",
                        suggestion="Accept page/per_page (or a cursor) with a capped page size, "
                        "and apply limit()/paginate().",
                    )

            # PF003: login and cart writes without a rate limit.
            if methods & WRITE_METHODS and not limits_app_wide:
                target = f"{rule} {fn.name}"
                auth = AUTH_ROUTE.search(target)
                if (auth or CART_ROUTE.search(target)) and not any(
                    RATE_LIMIT.search(ast.get_source_segment(text, d) or "")
                    for d in fn.decorator_list
                ):
                    hit(
                        "PF003",
                        dec_line,
                        severity="high" if auth else "medium",
                        hard=False,
                        title=f"{'/'.join(sorted(methods))} {rule} has no rate limit",
                        explanation=(
                            "Credential stuffing and brute force"
                            if auth
                            else "Bots can flood the cart and hold stock"
                        )
                        + "; nothing caps requests per client.",
                        suggestion="Add a per-client limit (e.g. Flask-Limiter "
                        "@limiter.limit('5 per minute')) and return 429 when it's hit.",
                    )
    return out
