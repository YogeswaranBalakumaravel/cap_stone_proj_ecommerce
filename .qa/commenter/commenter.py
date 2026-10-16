#!/usr/bin/env python3
"""Collect every gate's findings for a PR's head commit and keep them in one PR comment.

Runs from `.github/workflows/gate-findings-commenter.yml` whenever a gate workflow finishes. It
reads each gate's latest run for the commit, pulls the findings out of that run's artifact, and
creates or edits the one comment marked `<!-- gate-findings-commenter -->`. Standard library only.

Artifacts from a fork PR are built by the PR's own code, so they're treated as data only: the zip is
read in memory, only the named files are parsed, and nothing in it is extracted or executed.

Environment: GH_TOKEN, GITHUB_REPOSITORY, GITHUB_EVENT_PATH (a workflow_run event), and optionally
GITHUB_API_URL, GITHUB_SERVER_URL, GITHUB_STEP_SUMMARY, COMMENTER_DRY_RUN=1 (print, don't post).
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import findings as F

HERE = Path(__file__).resolve().parent
BOT = "github-actions[bot]"
PR_EVENTS = ("pull_request", "pull_request_review")
MAX_FILE = 5 * 1024 * 1024  # bytes read from any one artifact file

# Gates in the order the comment lists them. `files` maps a file-name pattern inside the artifact to
# the parser for it; `artifact` is a pattern for the artifact's name. Without an artifact a gate's
# findings show as unknown, unless `clean_pass_keeps_nothing` and the run passed.
GATES = [
    {
        "name": "Lint and security scan (ruff, bandit)",
        "workflows": ["CI", "CI - Dev", "CI - UAT"],
        "artifact": "lint-security-report",
        "files": {"ruff.json": "ruff", "bandit.json": "bandit"},
    },
    {
        "name": "PR tests (pytest)",
        "workflows": ["PR Test Gate"],
        "artifact": "pytest-report",
        "files": {"pytest-report.xml": "junit"},
    },
    {
        "name": "Secret scan (Gitleaks)",
        "workflows": ["Gitleaks Secret Scan"],
        "artifact": "gitleaks*",
        "files": {"*.sarif": "sarif"},
        # The action uploads its SARIF only when it finds a leak, so a clean pass keeps nothing.
        "clean_pass_keeps_nothing": True,
    },
    {
        "name": "Test quality agent",
        "workflows": ["Test quality agent"],
        "artifact": "test-quality-evidence",
        "files": {"metrics.json": "test_quality"},
    },
    {
        "name": "Code review agent",
        "workflows": ["Code review agent"],
        "artifact": "code-review-evidence",
        "files": {"findings.json": "code_review"},
    },
]


def check_titles() -> dict:
    """Check names from the code review agent's config, when it's checked out next to us."""
    try:
        cfg = json.loads((HERE.parent / "code-review" / "config.json").read_text(encoding="utf-8"))
        return {cid: c.get("title", cid) for cid, c in cfg.get("checks", {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def parse(kind: str, raw: bytes, titles: dict) -> list[dict]:
    text = raw.decode("utf-8-sig", errors="replace")
    if kind == "junit":
        return F.parse_junit(text)
    data = json.loads(text)
    if kind == "ruff":
        return F.parse_ruff(data)
    if kind == "bandit":
        return F.parse_bandit(data)
    if kind == "sarif":
        return F.parse_sarif(data)
    if kind == "test_quality":
        return F.parse_test_quality(data)
    if kind == "code_review":
        return F.parse_code_review(data, titles)
    raise ValueError(f"unknown findings kind {kind!r}")


def read_artifact(zip_bytes: bytes, files: dict, titles: dict) -> list[dict]:
    """Parse the wanted files out of an artifact zip without extracting it.

    Raises ValueError when the zip holds none of them, so the gate shows as unreadable, not clean.
    """
    out, matched = [], 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for info in z.infolist():
            base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            kinds = [k for p, k in files.items() if fnmatch.fnmatch(base, p)]
            if info.is_dir() or not kinds:
                continue
            if info.file_size > MAX_FILE:
                raise ValueError(f"{base} is over the {MAX_FILE // (1024 * 1024)} MB limit")
            for kind in kinds:
                out += parse(kind, z.read(info), titles)
                matched += 1
    if not matched:
        raise ValueError("the artifact has no " + " or ".join(files))
    return out


def latest_runs(runs: list[dict], workflows: list[str]) -> list[dict]:
    """This gate's PR runs, newest first."""
    mine = [r for r in runs if r.get("name") in workflows and r.get("event") in PR_EVENTS]
    return sorted(mine, key=lambda r: (r.get("created_at") or "", r.get("id") or 0), reverse=True)


# ---- GitHub REST, through urllib ----


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token
        self.api = os.environ.get("GITHUB_API_URL", "https://api.github.com")

    def _request(self, method: str, path: str, body=None, opener=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            path if path.startswith("https://") else f"{self.api}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        return (opener or urllib.request.build_opener()).open(req, timeout=30)  # nosec B310

    def get(self, path: str):
        with self._request("GET", path) as resp:
            return json.load(resp)

    def pages(self, path: str, key: str | None = None, limit: int = 10) -> list:
        out, sep = [], "&" if "?" in path else "?"
        for page in range(1, limit + 1):
            batch = self.get(f"{path}{sep}per_page=100&page={page}")
            items = batch.get(key, []) if key else batch
            out += items
            if len(items) < 100:
                break
        return out

    def download(self, url: str) -> bytes:
        """Artifact zips redirect to storage that rejects our auth header, so follow by hand."""
        try:
            with self._request("GET", url, opener=urllib.request.build_opener(_NoRedirect)) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise
            location = exc.headers["Location"]
        if not location.startswith("https://"):
            raise ValueError("artifact redirect isn't https")
        with urllib.request.urlopen(location, timeout=60) as resp:  # nosec B310 - https only
            return resp.read()

    def run_artifacts(self, run_id: int) -> list[dict]:
        return self.pages(f"/repos/{self.repo}/actions/runs/{run_id}/artifacts", "artifacts")

    def runs_for(self, sha: str) -> list[dict]:
        return self.pages(f"/repos/{self.repo}/actions/runs?head_sha={sha}", "workflow_runs", 3)

    def find_pr(self, event_run: dict) -> int | None:
        for pr in event_run.get("pull_requests") or []:
            if pr.get("number"):
                return int(pr["number"])
        # Fork PRs come without pull_requests; match the head commit instead.
        sha = event_run.get("head_sha")
        for pr in self.pages(f"/repos/{self.repo}/pulls?state=open", limit=5):
            if (pr.get("head") or {}).get("sha") == sha:
                return int(pr["number"])
        return None

    def upsert_comment(self, pr: int, body: str) -> str:
        comments = self.pages(f"/repos/{self.repo}/issues/{pr}/comments")
        mine = [
            c
            for c in comments
            if F.MARKER in (c.get("body") or "") and (c.get("user") or {}).get("login") == BOT
        ]
        if mine:
            cid = mine[-1]["id"]
            if mine[-1].get("body") == body:
                return f"comment {cid} already up to date"
            self._request(
                "PATCH", f"/repos/{self.repo}/issues/comments/{cid}", {"body": body}
            ).close()
            return f"updated comment {cid}"
        self._request("POST", f"/repos/{self.repo}/issues/{pr}/comments", {"body": body}).close()
        return "created a comment"


def gate_findings(gh: GitHub, spec: dict, mine: list[dict], titles: dict) -> tuple:
    """(findings or None, note) for one gate whose latest run has completed.

    A code review re-run on a review event keeps no artifact, so this falls back to the newest
    completed run for the commit that kept an unexpired one.
    """
    latest = mine[0]
    for run in (r for r in mine if r.get("status") == "completed"):
        arts = [
            a
            for a in gh.run_artifacts(run["id"])
            if fnmatch.fnmatch(a.get("name", ""), spec["artifact"]) and not a.get("expired")
        ]
        if not arts:
            continue
        found = []
        for a in arts:
            found += read_artifact(gh.download(a["archive_download_url"]), spec["files"], titles)
        if run is latest:
            return found, ""
        return found, ("Findings are from an earlier run for this commit; the latest run "
                       "didn't keep its own.")
    if spec.get("clean_pass_keeps_nothing") and latest.get("conclusion") == "success":
        return [], ""
    return None, "No run for this commit kept a findings file. Open the run log for the result."


def collect(gh: GitHub, sha: str, titles: dict) -> list[dict]:
    runs = gh.runs_for(sha)
    gates = []
    for spec in GATES:
        mine = latest_runs(runs, spec["workflows"])
        # findings stays None (shown as ?) until it's known.
        gate = {"name": spec["name"], "status": None, "conclusion": None, "url": None,
                "findings": None, "note": ""}
        gates.append(gate)
        if not mine:
            continue
        latest = mine[0]
        gate.update(status=latest.get("status"), conclusion=latest.get("conclusion"),
                    url=latest.get("html_url"))
        if latest.get("status") != "completed":
            continue
        try:
            gate["findings"], gate["note"] = gate_findings(gh, spec, mine, titles)
        except Exception as exc:  # noqa: BLE001 - one gate's bad artifact mustn't hide the rest
            gate["findings"], gate["note"] = None, f"Couldn't read this gate's findings: {exc}"
    return gates


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not (repo and token and event_path):
        print("Needs GITHUB_REPOSITORY, GH_TOKEN and GITHUB_EVENT_PATH.", file=sys.stderr)
        return 1
    event_run = json.loads(Path(event_path).read_text(encoding="utf-8")).get("workflow_run") or {}
    sha = event_run.get("head_sha", "")
    if event_run.get("event") not in PR_EVENTS or not sha:
        print("Not a pull request run; nothing to comment on.")
        return 0

    gh = GitHub(repo, token)
    pr = gh.find_pr(event_run)
    if not pr:
        print(f"No open pull request has head {sha}; nothing to comment on.")
        return 0

    gates = collect(gh, sha, check_titles())
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    head_repo = (event_run.get("head_repository") or {}).get("full_name") or repo
    body = F.render(
        gates,
        sha,
        blob_url=f"{server}/{head_repo}/blob/{sha}",
        updated=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(body.replace(F.MARKER, "") + "\n")
    if os.environ.get("COMMENTER_DRY_RUN") == "1":
        print(body)
        return 0
    print(f"PR #{pr}: {gh.upsert_comment(pr, body)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
