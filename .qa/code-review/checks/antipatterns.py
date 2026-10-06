"""LLM-typical anti-patterns (BLOCKING): the insecure patterns models reproduce from training
data. String-built SQL and shell, hard-coded secrets, disabled TLS verification, weak crypto,
debug left on, unsafe deserialisation, over-broad permissions and CORS, and the frontend
equivalents. Python is checked on the AST; markup and scripts line by line.
"""

from __future__ import annotations

import ast
import re

from cr_common import Ctx, finding, line_of

from . import _ast

CHECK = "llm_antipatterns"
SECRET_NAME = re.compile(
    r"(secret|passw(or)?d|pwd|token|api_?key|private_?key|access_?key|client_?secret)", re.I
)
PLACEHOLDER = re.compile(
    r"^(changeme|change-me|dev|test|testing|example|dummy|x+|\*+|<.*>|\$\{.*\}|your[-_].*)$", re.I
)
SECRET_LITERAL = re.compile(
    r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,}"
    r"|-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|AIza[0-9A-Za-z_-]{35}"
)
JS_SECRET = re.compile(
    r"(api_?key|secret|token|passw(or)?d)\s*[:=]\s*['\"`][A-Za-z0-9_\-./+=]{12,}['\"`]", re.I
)
WEAK_HASH = ("hashlib.md5", "hashlib.sha1", "md5", "sha1")
RANDOMS = (
    "random.random",
    "random.randint",
    "random.choice",
    "random.choices",
    "random.randrange",
    "random.getrandbits",
    "random.sample",
)
SENSITIVE = re.compile(r"token|secret|passw|otp|nonce|salt|key|code|session", re.I)

FRONTEND = [
    (
        "FE002",
        "high",
        True,
        "Autoescape turned off",
        re.compile(r"\{%-?\s*autoescape\s+(false|False)\b"),
        "Remove it; escape per value instead.",
    ),
    (
        "FE004",
        "high",
        True,
        "Code evaluated from a string",
        re.compile(r"\beval\(|\bnew\s+Function\(|set(Timeout|Interval)\(\s*['\"`]"),
        "Pass a function, never a string.",
    ),
    (
        "FE006",
        "high",
        True,
        "javascript: URL",
        re.compile(r"(href|src|action)\s*=\s*['\"]\s*javascript:", re.I),
        "Attach a handler in script; strict CSPs block javascript: URLs.",
    ),
    (
        "FE012",
        "high",
        True,
        "Credential kept in browser storage",
        re.compile(
            r"(local|session)Storage\.setItem\(\s*['\"`][^'\"`]*(token|jwt|passw|secret)", re.I
        ),
        "Keep it in an HttpOnly, Secure cookie.",
    ),
    (
        "FE009",
        "medium",
        False,
        "Resource loaded over plain http://",
        re.compile(r"(src|href)\s*=\s*['\"]http://(?!localhost|127\.0\.0\.1)", re.I),
        "Use https://.",
    ),
    (
        "FE015",
        "high",
        True,
        "TLS verification disabled",
        re.compile(r"rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED"),
        "Remove it; fix the certificate.",
    ),
    (
        "FE010",
        "low",
        False,
        "Debug output left in",
        re.compile(r"\bconsole\.(log|debug)\(|^\s*debugger;?\s*$"),
        "Remove console.log / debugger.",
    ),
]
MARKUP = (".html", ".htm", ".jinja", ".jinja2", ".j2", ".vue", ".svelte", ".jsx", ".tsx")


def _target_name(t) -> str:
    if isinstance(t, ast.Name):
        return t.id
    if isinstance(t, ast.Attribute):
        return t.attr
    if (
        isinstance(t, ast.Subscript)
        and isinstance(t.slice, ast.Constant)
        and isinstance(t.slice.value, str)
    ):
        return t.slice.value
    return ""


def python(ctx: Ctx, path: str, out: list[dict]) -> None:
    text = ctx.head(path)
    tree = _ast.parse(text, path)
    if not tree:
        return
    added, guard, classes = (
        ctx.added[path],
        _ast.main_guard_lines(tree, text),
        _ast.enclosing_class_names(tree),
    )

    def hit(rule, n, **kw):
        if n in added:
            out.append(finding(ctx, CHECK, rule, path, n, line_of(text, n), **kw))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name, short = _ast.call_name(node), _ast.call_name(node).rsplit(".", 1)[-1]
            if (
                short
                in ("execute", "exec_driver_sql", "text", "raw", "executemany", "executescript")
                and node.args
                and _ast.is_dynamic_str(node.args[0])
            ):
                hit(
                    "AP001",
                    node.lineno,
                    severity="high",
                    hard=True,
                    title="SQL built with string formatting",
                    explanation="Values spliced into SQL text are an injection hole and break on "
                    "quotes.",
                    suggestion="Use bound parameters: text('... WHERE x = :x').bindparams(x=...) "
                    "or ORM filters.",
                )
            elif name in ("os.system", "os.popen") or (
                short
                in (
                    "run",
                    "call",
                    "check_call",
                    "check_output",
                    "Popen",
                    "getoutput",
                    "getstatusoutput",
                )
                and (_ast.is_const(_ast.kw(node, "shell"), True) or short.startswith("get"))
            ):
                hit(
                    "AP002",
                    node.lineno,
                    severity="high",
                    hard=True,
                    title="Shell command built at runtime",
                    explanation="The shell interprets metacharacters in any interpolated value.",
                    suggestion="subprocess.run([...args], shell=False, check=True, timeout=...).",
                )
            elif _ast.is_const(_ast.kw(node, "verify"), False) or name.endswith(
                "_create_unverified_context"
            ):
                hit(
                    "AP004",
                    node.lineno,
                    severity="high",
                    hard=True,
                    title="TLS verification disabled",
                    explanation="Any man-in-the-middle can read and alter the traffic.",
                    suggestion="Remove verify=False; point verify= at the right CA bundle if "
                    "needed.",
                )
            elif (
                name in WEAK_HASH
                or name == "hashlib.new"
                and node.args
                and _ast.is_const(node.args[0])
                and str(node.args[0].value).lower() in ("md5", "sha1")
            ) and not _ast.is_const(_ast.kw(node, "usedforsecurity"), False):
                hit(
                    "AP005",
                    node.lineno,
                    severity="high",
                    hard=True,
                    title=f"Weak hash ({name})",
                    explanation="MD5/SHA-1 are broken for security use (and too fast for "
                    "passwords).",
                    suggestion="hashlib.sha256 for integrity; werkzeug.security / bcrypt / argon2 "
                    "for passwords; "
                    "or usedforsecurity=False if it truly isn't security.",
                )
            elif short == "run" and _ast.is_const(_ast.kw(node, "debug"), True):
                hit(
                    "AP006",
                    node.lineno,
                    severity="medium" if node.lineno in guard else "high",
                    hard=node.lineno not in guard,
                    title="Debug mode enabled in code",
                    explanation="The Werkzeug debugger allows remote code execution."
                    + (
                        " It's under the __main__ guard, so only local runs are affected."
                        if node.lineno in guard
                        else ""
                    ),
                    suggestion="Drive debug from configuration, never a literal True.",
                )
            elif name in (
                "pickle.loads",
                "pickle.load",
                "marshal.loads",
                "dill.loads",
                "jsonpickle.decode",
                "shelve.open",
            ) or (name in ("yaml.load", "yaml.load_all") and _ast.kw(node, "Loader") is None):
                hit(
                    "AP007",
                    node.lineno,
                    severity="high",
                    hard=True,
                    title="Unsafe deserialisation",
                    explanation="Deserialising untrusted bytes executes code.",
                    suggestion="Use json, or yaml.safe_load.",
                )
            elif (
                name == "os.chmod"
                and len(node.args) > 1
                and _ast.is_const(node.args[1])
                and isinstance(node.args[1].value, int)
                and node.args[1].value & 0o002
            ):
                hit(
                    "AP008",
                    node.lineno,
                    severity="high",
                    hard=True,
                    title="World-writable file permissions",
                    explanation="Any local user can modify the file.",
                    suggestion="Use 0o600 / 0o640 / 0o750.",
                )
            elif (
                short == "CORS"
                and any(_ast.is_const(_ast.kw(node, k), "*") for k in ("origins", "resources"))
                and _ast.is_const(_ast.kw(node, "supports_credentials"), True)
            ):
                hit(
                    "AP008",
                    node.lineno,
                    severity="high",
                    hard=True,
                    title="CORS: any origin with credentials",
                    explanation="Any site can make authenticated requests as your users.",
                    suggestion="List the allowed origins explicitly.",
                )
            elif name in RANDOMS:
                parent = next(
                    (
                        p
                        for p in ast.walk(tree)
                        if isinstance(p, ast.Assign | ast.Return)
                        and any(c is node for c in ast.walk(p))
                    ),
                    None,
                )
                label = " ".join(_target_name(t) for t in getattr(parent, "targets", []))
                fn = next(
                    (
                        x.name
                        for x in _ast.functions(tree)
                        if x.lineno <= node.lineno <= (x.end_lineno or x.lineno)
                    ),
                    "",
                )
                if SENSITIVE.search(label) or SENSITIVE.search(fn):
                    hit(
                        "AP005",
                        node.lineno,
                        severity="high",
                        hard=True,
                        title="random used for a secret value",
                        explanation="The random module is predictable; tokens made with it can be "
                        "guessed.",
                        suggestion="Use secrets.token_urlsafe / secrets.choice.",
                    )
            elif (
                _ast.call_name(node).endswith("decode")
                and "jwt" in name
                and (
                    _ast.is_const(_ast.kw(node, "verify"), False)
                    or "verify_signature" in ast.unparse(node)
                )
            ):
                hit(
                    "AP004",
                    node.lineno,
                    severity="high",
                    hard=True,
                    title="JWT signature not verified",
                    explanation="Anyone can forge a token.",
                    suggestion="Verify with the key and algorithms=[...].",
                )
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            in_test_cfg = "test" in classes.get(node.lineno, "").lower()
            for t in targets:
                tname = _target_name(t)
                if tname == "DEBUG" and _ast.is_const(value, True) and not in_test_cfg:
                    hit(
                        "AP006",
                        node.lineno,
                        severity="high",
                        hard=True,
                        title="DEBUG = True in code",
                        explanation="Debug mode in a deployed config exposes the interactive "
                        "debugger.",
                        suggestion="Read it from the environment, defaulting to False.",
                    )
                elif (
                    SECRET_NAME.search(tname)
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and len(value.value) >= 8
                    and not PLACEHOLDER.match(value.value)
                    and not in_test_cfg
                ):
                    hit(
                        "AP003",
                        node.lineno,
                        severity="high",
                        hard=True,
                        redact=True,
                        title="Hard-coded credential",
                        explanation="Secrets in code end up in every clone, fork and log.",
                        suggestion="Read it from the environment / the platform's secret store; "
                        "rotate this one.",
                    )
                elif (
                    tname
                    in ("SESSION_COOKIE_SECURE", "SESSION_COOKIE_HTTPONLY", "WTF_CSRF_ENABLED")
                    and _ast.is_const(value, False)
                    and not in_test_cfg
                ):
                    hit(
                        "AP008",
                        node.lineno,
                        severity="high",
                        hard=True,
                        title=f"{tname} = False",
                        explanation="Turns off a protection the framework enables by default.",
                        suggestion="Leave it enabled outside the testing config.",
                    )
                elif (
                    tname == "check_hostname"
                    and _ast.is_const(value, False)
                    or (tname == "verify_mode" and "CERT_NONE" in ast.unparse(value))
                ):
                    hit(
                        "AP004",
                        node.lineno,
                        severity="high",
                        hard=True,
                        title="TLS verification disabled",
                        explanation="Any man-in-the-middle can read and alter the traffic.",
                        suggestion="Keep hostname and certificate checks on.",
                    )
        elif isinstance(node, ast.Assert) and node.lineno in added and "test" not in path:
            hit(
                "AP010",
                node.lineno,
                severity="medium",
                hard=False,
                title="assert used for runtime validation",
                explanation="Asserts are stripped with python -O, so the check disappears in "
                "optimised runs.",
                suggestion="Raise a proper exception (ValueError, abort(400), ...).",
            )
    for n in sorted(added):
        line = line_of(text, n)
        if SECRET_LITERAL.search(line):
            hit(
                "AP003",
                n,
                severity="blocker",
                hard=True,
                redact=True,
                title="Credential-shaped literal",
                explanation="Matches a known key or token format.",
                suggestion="Remove it, rotate the credential, and load it from the secret store.",
            )


def frontend(ctx: Ctx, path: str, out: list[dict]) -> None:
    text = ctx.head(path) or ""
    markup, has_csrf = path.endswith(MARKUP), "csrf" in text.lower()
    for n in sorted(ctx.added[path]):
        line = line_of(text, n)
        for rule, sev, hard, title, rx, fix in FRONTEND:
            if rx.search(line):
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        rule,
                        path,
                        n,
                        line,
                        severity=sev,
                        hard=hard,
                        title=title,
                        explanation="A pattern models reproduce from training data.",
                        suggestion=fix,
                    )
                )
        if SECRET_LITERAL.search(line) or JS_SECRET.search(line):
            out.append(
                finding(
                    ctx,
                    CHECK,
                    "AP003",
                    path,
                    n,
                    line,
                    severity="blocker",
                    hard=True,
                    redact=True,
                    title="Credential in frontend code",
                    explanation="Everything shipped to the browser is public.",
                    suggestion="Keep it server-side; rotate it.",
                )
            )
        if markup:
            for tag in re.findall(r"<[^>]*>", line):
                low = tag.lower()
                if 'target="_blank"' in low.replace("'", '"') and "noopener" not in low:
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "FE007",
                            path,
                            n,
                            line,
                            severity="medium",
                            hard=False,
                            title='target="_blank" without rel="noopener"',
                            explanation="The opened page can navigate yours (reverse tabnabbing).",
                            suggestion='Add rel="noopener noreferrer".',
                        )
                    )
                if (
                    low.startswith("<script")
                    and re.search(r"src\s*=\s*['\"]https?://", low)
                    and "integrity" not in low
                ):
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "FE011",
                            path,
                            n,
                            line,
                            severity="medium",
                            hard=False,
                            title="Third-party script without integrity",
                            explanation="A compromised CDN serves code into your page.",
                            suggestion='Add integrity="sha384-..." crossorigin="anonymous", '
                            "or self-host the file.",
                        )
                    )
                if (
                    low.startswith("<form")
                    and re.search(r"method\s*=\s*['\"]?post", low)
                    and not has_csrf
                ):
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "FE013",
                            path,
                            n,
                            line,
                            severity="high",
                            hard=False,
                            title="POST form without a CSRF token",
                            explanation="Missing input/request validation: another site can "
                            "submit it.",
                            suggestion="Add {{ csrf_token() }} (Flask-WTF) or an equivalent check.",
                        )
                    )


def run(ctx: Ctx, idx: dict) -> list[dict]:
    out: list[dict] = []
    for f in ctx.changed("backend", "frontend"):
        if f["path"].endswith(".py"):
            python(ctx, f["path"], out)
        elif f["area"] == "frontend":
            frontend(ctx, f["path"], out)
        else:
            text = ctx.head(f["path"]) or ""
            for n in sorted(ctx.added[f["path"]]):
                if SECRET_LITERAL.search(line_of(text, n)):
                    out.append(
                        finding(
                            ctx,
                            CHECK,
                            "AP003",
                            f["path"],
                            n,
                            line_of(text, n),
                            severity="blocker",
                            hard=True,
                            redact=True,
                            title="Credential-shaped literal",
                            explanation="Matches a known key or token format.",
                            suggestion="Remove it, rotate it, and load it from the secret store.",
                        )
                    )
    return out
