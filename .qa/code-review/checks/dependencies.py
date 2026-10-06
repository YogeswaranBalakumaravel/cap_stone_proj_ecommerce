"""Dependency hygiene (BLOCKING): new dependencies are pinned, exist, are free of known
vulnerabilities, aren't typo-adjacent to a popular package, and are actually needed.

Registry and OSV lookups use urllib against pypi.org, registry.npmjs.org and api.osv.dev.
A lookup that fails is reported as inconclusive, never as a pass.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from cr_common import STDLIB, Ctx, finding

from . import _ast

CHECK = "dependency_hygiene"
POPULAR_PY = """
requests flask django numpy pandas sqlalchemy flask-sqlalchemy jinja2 werkzeug click pytest
urllib3 boto3 botocore pyyaml setuptools six python-dateutil certifi idna charset-normalizer
cryptography pydantic fastapi uvicorn gunicorn celery redis psycopg2 psycopg2-binary pillow
scipy matplotlib scikit-learn beautifulsoup4 lxml aiohttp httpx attrs packaging pip wheel
markupsafe itsdangerous flask-cors flask-login flask-wtf flask-migrate alembic marshmallow
pyjwt bcrypt passlib paramiko selenium playwright openai anthropic tqdm rich typer colorama
toml tomli jsonschema protobuf grpcio pytz tzdata sentry-sdk stripe twilio sendgrid python-
dotenv black ruff mypy bandit coverage
""".split()
POPULAR_NPM = """
react react-dom vue svelte angular axios lodash express next nuxt vite webpack babel
typescript jest mocha chai eslint prettier moment dayjs date-fns jquery bootstrap
tailwindcss chart.js d3 three socket.io redux zustand uuid dotenv cors body-parser
jsonwebtoken bcrypt mongoose nodemon esbuild rollup
""".split()
REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;#]*)")


def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 1:
        return 2
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def python_deps(path: str, text: str | None) -> dict[str, dict]:
    """name -> {spec, line, raw} for requirements files and pyproject.toml."""
    out: dict[str, dict] = {}
    if not text:
        return out
    if path.endswith("pyproject.toml"):
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return out
        project = data.get("project", {})
        entries = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            entries += extra
        lines = text.splitlines()
        for entry in entries:
            m = REQ_LINE.match(entry)
            if m:
                n = next((i for i, ln in enumerate(lines, 1) if entry in ln), 1)
                out[pep503(m.group(1))] = {
                    "name": m.group(1),
                    "spec": m.group(3).strip(),
                    "line": n,
                    "raw": entry,
                }
        return out
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-", "git+", "http")):
            continue
        m = REQ_LINE.match(stripped)
        if m:
            out[pep503(m.group(1))] = {
                "name": m.group(1),
                "spec": m.group(3).strip(),
                "line": i,
                "raw": stripped,
            }
    return out


def npm_deps(text: str | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not text:
        return out
    try:
        data = json.loads(text)
    except ValueError:
        return out
    lines = text.splitlines()
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (data.get(section) or {}).items():
            n = next((i for i, ln in enumerate(lines, 1) if f'"{name}"' in ln), 1)
            out[name] = {"name": name, "spec": str(spec), "line": n, "raw": f'"{name}": "{spec}"'}
    return out


def _get(url: str, timeout: int):
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "cr-agent"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def pinned_version(spec: str) -> str | None:
    m = re.fullmatch(r"===?\s*([A-Za-z0-9.+!_-]+)", spec.strip())
    return m.group(1) if m else None


def registry(eco: str, name: str, version: str | None, timeout: int) -> tuple[str, str]:
    """('ok' | 'missing' | 'missing_version' | 'error', detail)."""
    url = (
        f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json"
        if eco == "PyPI"
        else f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}"
    )
    try:
        data = _get(url, timeout)
    except urllib.error.HTTPError as exc:
        return ("missing", f"{url} returned {exc.code}") if exc.code == 404 else ("error", str(exc))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return "error", str(exc)
    versions = data.get("releases") if eco == "PyPI" else data.get("versions")
    if version and isinstance(versions, dict) and version not in versions:
        return "missing_version", f"{name} has no release {version}"
    return "ok", ""


def osv(eco: str, name: str, version: str, timeout: int) -> tuple[list[str] | None, str]:
    body = json.dumps({"package": {"name": name, "ecosystem": eco}, "version": version}).encode()
    req = urllib.request.Request(
        "https://api.osv.dev/v1/query", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, str(exc)
    return [v.get("id", "?") for v in data.get("vulns", [])], ""


def _import_name(dist: str, cfg: dict) -> str:
    return cfg["dependencies"]["import_names"].get(pep503(dist), pep503(dist).replace("-", "_"))


def run(ctx: Ctx, idx: dict) -> list[dict]:
    cfg, dcfg = ctx.cfg, ctx.cfg["dependencies"]
    timeout = int(dcfg.get("network_timeout_seconds", 15))
    out: list[dict] = []
    manifests = [
        p
        for p in ctx.tracked
        if re.search(r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|package\.json)$", p)
    ]
    changed = {f["path"] for f in ctx.changed()}
    lock_changed = any(
        p.endswith(("package-lock.json", "yarn.lock", "pnpm-lock.yaml")) for p in changed
    )

    declared_py: set[str] = set()
    for path in manifests:
        if not path.endswith("package.json"):
            declared_py |= set(python_deps(path, ctx.head(path)))

    all_imports = {i["module"].split(".")[0] for imps in idx["imports"].values() for i in imps}
    for path in manifests:
        if path not in changed:
            continue
        head, base = ctx.head(path), ctx.base_text(path)
        npm = path.endswith("package.json")
        new, old = (
            (npm_deps(head), npm_deps(base))
            if npm
            else (python_deps(path, head), python_deps(path, base))
        )
        eco = "npm" if npm else "PyPI"
        lines = (head or "").splitlines()
        for key, dep in new.items():
            if key in old and old[key]["spec"] == dep["spec"]:
                continue
            n, raw = (
                dep["line"],
                lines[dep["line"] - 1] if 0 < dep["line"] <= len(lines) else dep["raw"],
            )

            def add(rule, n=n, raw=raw, path=path, **kw):
                out.append(finding(ctx, CHECK, rule, path, n, raw, area="backend", **kw))

            name, spec = dep["name"], dep["spec"]
            version = (
                pinned_version(spec)
                if not npm
                else (spec if re.fullmatch(r"\d[\w.+-]*", spec) else None)
            )
            if not version and not (npm and lock_changed):
                add(
                    "DP001",
                    severity="high",
                    hard=True,
                    title=f"{name} isn't pinned",
                    explanation=f"'{spec or 'any version'}' lets a different release install "
                    "tomorrow.",
                    suggestion="Pin an exact version (==X.Y.Z) and keep a hashed lockfile "
                    "(--require-hashes)."
                    if not npm
                    else "Pin an exact version or commit the updated lockfile.",
                )
            if dcfg.get("registry_checks", True):
                status, detail = registry(eco, name, version, timeout)
                if status == "missing":
                    add(
                        "DP002",
                        severity="blocker",
                        hard=True,
                        title=f"{name} doesn't exist on {eco}",
                        explanation="Possible hallucinated package; anyone can register this name "
                        "later.",
                        suggestion="Remove it, or use the real package name from the official "
                        "registry.",
                    )
                elif status == "missing_version":
                    add(
                        "DP002",
                        severity="blocker",
                        hard=True,
                        title=f"{name}=={version} doesn't exist",
                        explanation=detail,
                        suggestion="Pin a version that is published on the registry.",
                    )
                elif status == "error":
                    add(
                        "DP000",
                        severity="info",
                        hard=False,
                        title=f"Registry check for {name} inconclusive",
                        explanation=detail[:200],
                        suggestion="Re-run, or verify the package by hand.",
                    )
            if version and dcfg.get("osv_checks", True):
                vulns, err = osv(eco, name, version, timeout)
                if vulns:
                    add(
                        "DP003",
                        severity="high",
                        hard=True,
                        title=f"{name}=={version} has known vulnerabilities",
                        explanation="OSV lists: " + ", ".join(vulns[:8]),
                        suggestion="Upgrade to a release that fixes them.",
                    )
                elif vulns is None:
                    add(
                        "DP000",
                        severity="info",
                        hard=False,
                        title=f"OSV lookup for {name} inconclusive",
                        explanation=err[:200],
                        suggestion="Re-run, or check osv.dev by hand.",
                    )
            popular = POPULAR_NPM if npm else POPULAR_PY
            normal = name.lower() if npm else pep503(name)
            near = [p for p in popular + list(old) if p != normal and distance(normal, p) == 1]
            if near and normal not in popular:
                add(
                    "DP004",
                    severity="high",
                    hard=False,
                    title=f"{name} is one edit away from {near[0]}",
                    explanation="Typo-adjacent names are how slopsquatting attacks get installed.",
                    suggestion=f"Confirm you meant {name} and not {near[0]}.",
                )
            if not npm:
                imp = _import_name(name, cfg)
                if pep503(name) not in dcfg.get("not_imported_ok", []) and imp not in all_imports:
                    add(
                        "DP005",
                        severity="high",
                        hard=False,
                        title=f"{name} is declared but never imported",
                        explanation=f"No module imports '{imp}'. AI often adds libraries a few "
                        "lines would replace.",
                        suggestion="Remove it, or add its import name to "
                        "dependencies.import_names in config.",
                    )

    # Imports added in this PR that no manifest provides.
    provided = {_import_name(d, cfg) for d in declared_py} | {
        d.replace("-", "_") for d in declared_py
    }
    transitive = {
        m
        for d, mods in cfg["dependencies"].get("transitive", {}).items()
        if d in declared_py
        for m in mods
    }
    for f in ctx.changed("backend", "test"):
        if not f["path"].endswith(".py"):
            continue
        text = ctx.head(f["path"])
        tree = _ast.parse(text, f["path"])
        if not tree:
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Import | ast.ImportFrom)
                or node.lineno not in ctx.added[f["path"]]
            ):
                continue
            if isinstance(node, ast.ImportFrom) and node.level:
                continue
            mods = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for mod in mods:
                top = mod.split(".")[0]
                if not top or top in STDLIB or top in ctx.local_top_modules or top in provided:
                    continue
                if top in ("pytest", "_pytest") and f["area"] == "test":
                    continue
                raw = lines[node.lineno - 1]
                out.append(
                    finding(
                        ctx,
                        CHECK,
                        "DP006",
                        f["path"],
                        node.lineno,
                        raw,
                        severity="medium" if top in transitive else "high",
                        hard=False,
                        title=f"'{top}' is imported but no manifest declares it",
                        explanation=(
                            "It only arrives transitively; a parent upgrade can remove it."
                            if top in transitive
                            else "Missing dependency, or a hallucinated module that only exists in "
                            "the model's imagination."
                        ),
                        suggestion="Declare and pin the package that provides it, or remove the "
                        "import.",
                    )
                )
    return out
