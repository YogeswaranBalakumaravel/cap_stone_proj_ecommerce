"""What the repository is: languages, test setup, branches, existing workflows and governance.

Facts only, no judgement (that's checks.py). Files are read as text; nothing in the repo is run.
Workflows are read with a small indentation-based reader instead of a YAML library, which is
enough for the keys it needs (name, triggers, branches, steps) on ordinary workflow files.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from ob_common import GATE_FILES, LONG_LIVED, REPO, WORKFLOWS, git, read_text

LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java",
    ".kt": "Kotlin", ".go": "Go", ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".rs": "Rust",
    ".swift": "Swift", ".scala": "Scala",
}
SKIP_DIRS = re.compile(r"(^|/)(\.qa|\.github|node_modules|vendor|dist|build|\.venv|venv)/")
MANIFESTS = (
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "poetry.lock",
    "uv.lock", "package.json", "pom.xml", "build.gradle", "build.gradle.kts", "go.mod",
    "Cargo.toml", "Gemfile", "composer.json",
)
TOOLS = {
    "pytest": r"\bpytest\b", "unittest": r"-m unittest\b", "jest": r"\bjest\b",
    "vitest": r"\bvitest\b", "mocha": r"\bmocha\b", "maven": r"\bmvnw?\b", "gradle": r"gradlew?\b",
    "go test": r"\bgo test\b", "dotnet test": r"\bdotnet test\b",
    "npm test": r"\bnpm (run )?test\b",
    "ruff": r"\bruff\b", "flake8": r"\bflake8\b", "pylint": r"\bpylint\b", "mypy": r"\bmypy\b",
    "black": r"\bblack\b", "bandit": r"\bbandit\b", "eslint": r"\beslint\b", "tsc": r"\btsc\b",
    "gitleaks": r"gitleaks", "codeql": r"codeql-action", "semgrep": r"semgrep", "sonar": r"sonar",
    "trivy": r"\btrivy\b", "snyk": r"\bsnyk\b", "coverage": r"\bcoverage\b|--cov\b",
}
SERVICE_DEPS = {
    "postgres": r"psycopg|asyncpg|\bpg\b|postgres", "mysql": r"pymysql|mysqlclient|mysql2?\b",
    "redis": r"\bredis\b|ioredis", "mongodb": r"pymongo|motor|mongoose|mongodb",
    "rabbitmq": r"\bpika\b|amqplib|celery", "kafka": r"kafka", "elasticsearch": r"elasticsearch",
}
ENV_REQUIRED = re.compile(
    r"os\.environ\[\s*['\"](\w+)['\"]\s*\]|os\.getenv\(\s*['\"](\w+)['\"]\s*\)"
    r"|System\.getenv\(\s*\"(\w+)\"\s*\)"
)


# ------------------------------------------------------------------ files and languages


def tracked() -> list[str]:
    return [p for p in git("ls-files", "-z").split("\0") if p]


def languages(files: list[str]) -> dict[str, int]:
    counts = Counter(
        LANGUAGES[Path(p).suffix.lower()]
        for p in files
        if Path(p).suffix.lower() in LANGUAGES and not SKIP_DIRS.search(p)
    )
    return dict(counts.most_common())


def manifests(files: list[str]) -> list[str]:
    return [p for p in files if Path(p).name in MANIFESTS and not SKIP_DIRS.search(p)]


def _pyproject() -> dict:
    try:
        return tomllib.loads(read_text(REPO / "pyproject.toml"))
    except tomllib.TOMLDecodeError:
        return {}


def _package_json() -> dict:
    try:
        data = json.loads(read_text(REPO / "package.json") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _python_deps_text(files: list[str]) -> str:
    names = ("requirements", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile")
    return "\n".join(
        read_text(REPO / p) for p in files if "/" not in p and Path(p).name.startswith(names)
    ).lower()


# ------------------------------------------------------------------ test setup


def test_setup(files: list[str], langs: dict[str, int]) -> dict:
    """The test frameworks in use, and what the gates' config would need to run them."""
    py_tests = [p for p in files if re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.py$", p)]
    pkg = _package_json()
    js_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    py_deps = _python_deps_text(files)
    meta = _pyproject()
    frameworks = []
    if py_tests or "conftest.py" in {Path(p).name for p in files}:
        uses_pytest = (
            "pytest" in py_deps
            or "pytest" in (meta.get("tool") or {})
            or any(Path(p).name in ("pytest.ini", "conftest.py") for p in files)
        )
        frameworks.append("pytest" if uses_pytest else "unittest")
    for name in ("jest", "vitest", "mocha", "jasmine"):
        if name in js_deps:
            frameworks.append(name)
    if (REPO / "pom.xml").is_file():
        frameworks.append("maven")
    if any((REPO / f).is_file() for f in ("build.gradle", "build.gradle.kts")):
        frameworks.append("gradle")
    if (REPO / "go.mod").is_file():
        frameworks.append("go test")
    if any(p.endswith((".csproj", ".sln")) for p in files):
        frameworks.append("dotnet test")

    primary = next(iter(langs), "")
    suggestion = _suggest(frameworks, primary, pkg, files)
    return {
        "frameworks": frameworks,
        "python_test_files": len(py_tests),
        "package_test_script": (pkg.get("scripts") or {}).get("test", ""),
        "suggested": suggestion,
    }


def _suggest(frameworks: list[str], primary: str, pkg: dict, files: list[str]) -> dict:
    """The gates' test command and globs for the repo's main stack, or {} if unknown."""
    js_src = ["src/*", "lib/*", "app/*", "*.js", "*.ts", "*.jsx", "*.tsx"]
    js_tests = ["*.test.*", "*.spec.*", "__tests__/*", "*/__tests__/*", "test/*", "tests/*"]
    if primary == "Python" and frameworks[:1] in (["pytest"], ["unittest"]):
        return {
            "command": "python -m pytest -q -x -p no:cacheprovider",
            "targeted_command": "python -m pytest -q -x -p no:cacheprovider {tests}",
            "source_globs": ["*.py"],
            "test_globs": ["tests/*", "*/tests/*", "test_*.py", "*/test_*.py", "*_test.py",
                           "conftest.py", "*/conftest.py"],
        }
    if primary in ("JavaScript", "TypeScript"):
        if "vitest" in frameworks:
            cmd = "npx vitest run --bail 1"
        elif "jest" in frameworks:
            cmd = "npx jest --bail"
        elif (pkg.get("scripts") or {}).get("test"):
            cmd = "npm test --silent"
        else:
            return {}
        return {"command": cmd, "targeted_command": cmd + " {tests}", "source_globs": js_src,
                "test_globs": js_tests}
    if primary in ("Java", "Kotlin"):
        if "maven" in frameworks:
            cmd = "mvn -q -B test"
        elif "gradle" in frameworks:
            cmd = "./gradlew test" if "gradlew" in files else "gradle test"
        else:
            return {}
        return {"command": cmd, "targeted_command": "", "source_globs": ["src/main/*"],
                "test_globs": ["src/test/*"]}
    if primary == "Go" and "go test" in frameworks:
        return {"command": "go test ./... -failfast", "targeted_command": "",
                "source_globs": ["*.go"], "test_globs": ["*_test.go"]}
    if primary == "C#" and "dotnet test" in frameworks:
        return {"command": "dotnet test", "targeted_command": "", "source_globs": ["*.cs"],
                "test_globs": ["*Tests/*", "*.Tests/*", "*Test.cs", "*Tests.cs"]}
    return {}


def web_frameworks(files: list[str]) -> list[str]:
    py = _python_deps_text(files)
    pkg = _package_json()
    js = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    found = [n for n in ("flask", "django", "fastapi", "starlette", "tornado") if n in py]
    found += [n for n in ("express", "next", "react", "vue", "@angular/core", "nestjs") if n in js]
    if read_text(REPO / "pom.xml").find("spring-boot") >= 0:
        found.append("spring-boot")
    return found


def python_version() -> dict:
    """The Python the project asks for, wherever it says so."""
    meta = _pyproject()
    sources = {
        "pyproject.toml requires-python": (meta.get("project") or {}).get("requires-python", ""),
        ".python-version": read_text(REPO / ".python-version").strip(),
        "runtime.txt": read_text(REPO / "runtime.txt").strip().removeprefix("python-"),
    }
    return {k: v for k, v in sources.items() if v}


def test_needs(files: list[str]) -> dict:
    """Signs that the tests need services or environment variables a CI job won't have."""
    test_like = [
        p for p in files
        if re.search(r"(^|/)(tests?/|conftest\.py$|test_[^/]*$|[^/]*_test\.\w+$|"
                     r"[^/]*\.(test|spec)\.\w+$|settings[^/]*\.py$)", p)
    ]
    env = set()
    for p in test_like[:400]:
        for groups in ENV_REQUIRED.findall(read_text(REPO / p, 200_000)):
            env.update(g for g in groups if g)
    compose = [
        p for p in files if re.search(r"(^|/)(docker-)?compose[^/]*\.ya?ml$", p)
    ]
    images = sorted({
        m.split(":")[0].split("/")[-1]
        for p in compose
        for m in re.findall(r"(?m)^\s*image:\s*['\"]?([\w./:-]+)", read_text(REPO / p))
    })
    deps = (_python_deps_text(files) + json.dumps(_package_json())).lower()
    services = sorted({name for name, rx in SERVICE_DEPS.items() if re.search(rx, deps)})
    return {"required_env": sorted(env), "compose_files": compose, "compose_images": images,
            "service_clients": services}


# ------------------------------------------------------------------ branches


def branches() -> dict:
    remote = [
        b.strip().removeprefix("origin/")
        for b in git("branch", "-r", "--format=%(refname:short)").splitlines()
        if b.strip().startswith("origin/") and not b.strip().endswith("/HEAD")
    ]
    local = [b.strip() for b in git("branch", "--format=%(refname:short)").splitlines()]
    names = sorted(set(remote or local))
    default = os.environ.get("OB_DEFAULT_BRANCH") or git(
        "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
    ).strip().removeprefix("origin/")
    return {
        "all": names,
        "long_lived": [b for b in names if LONG_LIVED.match(b)],
        "default": default or ("main" if "main" in names else "master" if "master" in names
                               else ""),
    }


# ------------------------------------------------------------------ workflows


def _block(lines: list[str], start: int) -> list[str]:
    """The lines nested under lines[start], by indentation."""
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = []
    for line in lines[start + 1:]:
        if line.strip() and not line.lstrip().startswith("#"):
            if len(line) - len(line.lstrip()) <= indent:
                break
        out.append(line)
    return out


def _find(lines: list[str], key: str, indent: int | None = None) -> int:
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if re.match(rf"['\"]?{re.escape(key)}['\"]?\s*:", stripped) and (
            indent is None or len(line) - len(stripped) == indent
        ):
            return i
    return -1


def _list_value(lines: list[str], key: str) -> list[str] | None:
    """`key: [a, b]`, `key: a`, or a dash list under key, from the first match in lines."""
    i = _find(lines, key)
    if i < 0:
        return None
    rest = lines[i].split(":", 1)[1].split("#")[0].strip()
    if rest.startswith("["):
        return [v.strip().strip("'\"") for v in rest.strip("[]").split(",") if v.strip()]
    if rest:
        return [rest.strip("'\"")]
    return [
        m.group(1).strip().strip("'\"")
        for line in _block(lines, i)
        if (m := re.match(r"\s*-\s*(.+?)\s*(#.*)?$", line))
    ]


def parse_workflow(path: Path) -> dict:
    text = read_text(path)
    lines = text.splitlines()
    name = re.search(r"(?m)^name:\s*['\"]?(.+?)['\"]?\s*$", text)
    on_i = _find(lines, "on", 0)
    if on_i < 0:
        on_i = _find(lines, "true", 0)  # `on` written in a way some tools read as a boolean
    on_lines = _block(lines, on_i) if on_i >= 0 else []
    inline = lines[on_i].split(":", 1)[1].strip() if on_i >= 0 else ""
    triggers = {}
    if inline:
        for t in inline.strip("[]").split(","):
            if t.strip():
                triggers[t.strip()] = {"branches": None}
    else:
        base = min((len(x) - len(x.lstrip()) for x in on_lines if x.strip()), default=2)
        for i, line in enumerate(on_lines):
            m = re.match(rf"^ {{{base}}}([\w-]+)\s*:", line)
            if m:
                sub = _block(on_lines, i)
                triggers[m.group(1)] = {
                    "branches": _list_value(sub, "branches"),
                    "workflows": _list_value(sub, "workflows"),
                    "paths": _list_value(sub, "paths"),
                }
    jobs_i = _find(lines, "jobs", 0)
    jobs = [
        m.group(1)
        for line in (_block(lines, jobs_i) if jobs_i >= 0 else [])
        if (m := re.match(r"^  ([\w-]+):\s*$", line))
    ]
    return {
        "file": path.name,
        "name": name.group(1) if name else path.name,
        "triggers": triggers,
        "jobs": jobs,
        "steps": re.findall(r"(?m)^\s*-\s*name:\s*(.+?)\s*$", text),
        "tools": sorted(t for t, rx in TOOLS.items() if re.search(rx, text)),
        "python_versions": re.findall(r"python-version:\s*['\"]?([\w.]+)", text),
        "node_versions": re.findall(r"node-version:\s*['\"]?([\w.]+)", text),
        "requirements_txt": "requirements.txt" in text,
        "pip_cache": bool(re.search(r"cache:\s*['\"]?pip", text)),
        "services": bool(re.search(r"(?m)^\s{4}services:\s*$", text)),
        "secrets": sorted(set(re.findall(r"secrets\.(\w+)", text)) - {"GITHUB_TOKEN"}),
        "vars": sorted(set(re.findall(r"vars\.(\w+)", text))),
        "uploads": re.findall(r"(?ms)upload-artifact.*?\n\s+name:\s*([\w.-]+)", text),
        "gate": GATE_FILES.get(path.name),
    }


def workflows() -> list[dict]:
    if not WORKFLOWS.is_dir():
        return []
    return [parse_workflow(p) for p in sorted(WORKFLOWS.glob("*.y*ml"))]


# ------------------------------------------------------------------ governance


def governance(files: list[str]) -> dict:
    owners_path = next(
        (p for p in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS") if p in files), ""
    )
    owners = read_text(REPO / owners_path) if owners_path else ""
    template_path = next(
        (p for p in files if re.match(r"(?i)(\.github/|docs/)?pull_request_template\.md$", p)), ""
    )
    template = read_text(REPO / template_path) if template_path else ""
    return {
        "codeowners": owners_path,
        "codeowners_covers": {
            ".qa/": bool(re.search(r"(?m)^/?\.qa/?\S*\s+@", owners)),
            ".github/workflows/": bool(re.search(r"(?m)^/?\.github/(workflows/?)?\S*\s+@", owners)),
        },
        "pr_template": template_path,
        "pr_template_has_criteria": bool(re.search(r"(?im)^#+\s*acceptance criteria", template)),
        "context_files": [p for p in ("CLAUDE.md", "AGENTS.md") if p in files],
    }


def github_rules(branch_names: list[str]) -> dict:
    """Required status checks per branch from rulesets and classic protection; best effort."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN")
    if not (repo and token):
        return {"available": False, "reason": "no GITHUB_REPOSITORY/GH_TOKEN (local run)"}
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    out = {"available": True, "required_checks": {}}
    for b in branch_names:
        checks, errors = set(), []
        for path in (f"/repos/{repo}/rules/branches/{urllib.parse.quote(b, safe='')}",
                     f"/repos/{repo}/branches/{urllib.parse.quote(b, safe='')}/protection"):
            req = urllib.request.Request(api + path, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed API host
                    data = json.load(resp)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                errors.append(str(getattr(exc, "code", exc)))
                continue
            for rule in data if isinstance(data, list) else []:
                for c in ((rule.get("parameters") or {}).get("required_status_checks") or []):
                    checks.add(c.get("context", ""))
            if isinstance(data, dict):
                for c in ((data.get("required_status_checks") or {}).get("checks") or []):
                    checks.add(c.get("context", ""))
        out["required_checks"][b] = {"checks": sorted(c for c in checks if c), "errors": errors}
    return out


def collect() -> dict:
    files = tracked()
    langs = languages(files)
    br = branches()
    return {
        "repo": os.environ.get("GITHUB_REPOSITORY", REPO.name),
        "private": os.environ.get("OB_REPO_PRIVATE", ""),
        "files": len(files),
        "languages": langs,
        "manifests": manifests(files),
        "tests": test_setup(files, langs),
        "web_frameworks": web_frameworks(files),
        "python_version": python_version(),
        "test_needs": test_needs(files),
        "branches": br,
        "workflows": workflows(),
        "governance": governance(files),
        "github": github_rules(br["long_lived"] or ([br["default"]] if br["default"] else [])),
        "tracked": files,
    }
