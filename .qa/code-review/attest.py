#!/usr/bin/env python3
"""Named human attestations, read from GitHub pull request reviews. Standard library only.

A reviewer attests by submitting an **approving** review whose body contains a line such as

    attest: logic, tests
    attest: all

It counts only when the reviewer isn't the PR author or a bot, the review is on the current head
commit (`attestation.require_same_commit`), and the reviewer has write access to the repository
(`attestation.require_write_permission`). Check ids and their aliases are both accepted.

Outside GitHub Actions (or for tests) set CR_ATTESTATIONS_FILE to a JSON list of
{"user", "checks": [...], "commit", "state"} objects instead.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request


def _alias_map(cfg: dict) -> dict[str, str]:
    out = {}
    for cid, c in cfg["checks"].items():
        out[cid.lower()] = cid
        out[cid.replace("_", "-").lower()] = cid
        for a in c.get("aliases", []):
            out[a.lower()] = cid
    return out


def parse(body: str, cfg: dict) -> set[str]:
    keyword = re.escape(cfg["attestation"].get("keyword", "attest:"))
    aliases, out = _alias_map(cfg), set()
    for m in re.finditer(rf"(?im)^\s*{keyword}\s*(.+)$", body or ""):
        for token in re.split(r"[,\s]+", m.group(1).strip().lower()):
            if token == "all":
                out |= set(cfg["checks"])
            elif token in aliases:
                out.add(aliases[token])
    return out


def _api(path: str, token: str):
    url = f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def collect(cfg: dict, head_sha: str, author: str) -> tuple[dict[str, list[str]], list[str]]:
    """Return ({check_id: [attesting users]}, notes)."""
    acfg, notes = cfg["attestation"], []
    reviews = []
    if os.environ.get("CR_ATTESTATIONS_FILE"):
        try:
            for r in json.loads(open(os.environ["CR_ATTESTATIONS_FILE"], encoding="utf-8").read()):
                reviews.append(
                    {
                        "user": r["user"],
                        "body": "attest: " + ", ".join(r["checks"]),
                        "commit": r.get("commit", head_sha),
                        "state": r.get("state", "APPROVED"),
                        "bot": False,
                        "permission": r.get("permission", "write"),
                    }
                )
        except (OSError, ValueError, KeyError) as exc:
            notes.append(f"CR_ATTESTATIONS_FILE unreadable: {exc}")
    else:
        repo, pr = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("PR_NUMBER")
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not (repo and pr and token):
            return {}, ["no pull request context, so attestations weren't read"]
        try:
            page = 1
            while True:
                batch = _api(f"/repos/{repo}/pulls/{pr}/reviews?per_page=100&page={page}", token)
                for r in batch:
                    user = r.get("user") or {}
                    reviews.append(
                        {
                            "user": user.get("login", ""),
                            "body": r.get("body") or "",
                            "commit": r.get("commit_id"),
                            "state": r.get("state"),
                            "bot": user.get("type") == "Bot",
                            "permission": None,
                        }
                    )
                if len(batch) < 100:
                    break
                page += 1
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {}, [f"couldn't read reviews: {exc}"]
        perms: dict[str, str] = {}
        if acfg.get("require_write_permission", True):
            for r in reviews:
                if r["user"] and r["user"] not in perms:
                    try:
                        perms[r["user"]] = _api(
                            f"/repos/{repo}/collaborators/{r['user']}/permission", token
                        ).get("permission", "none")
                    except (urllib.error.URLError, OSError, ValueError):
                        perms[r["user"]] = "unknown"
                r["permission"] = perms.get(r["user"])
    # The latest review per user wins: a later "changes requested" withdraws an earlier attestation.
    latest: dict[str, dict] = {}
    for r in reviews:
        if r["state"] in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"):
            if r["state"] != "COMMENTED" or r["user"] not in latest:
                latest[r["user"]] = r
    out: dict[str, list[str]] = {}
    for user, r in latest.items():
        checks = parse(r["body"], cfg)
        if not checks:
            continue
        why = []
        if acfg.get("require_approval", True) and r["state"] != "APPROVED":
            why.append("the review isn't an approval")
        if not user or user == author or r["bot"]:
            why.append("authors and bots can't attest")
        if acfg.get("require_same_commit", True) and r["commit"] and r["commit"] != head_sha:
            why.append("it was given on an earlier commit")
        if acfg.get("require_write_permission", True) and r["permission"] not in (
            "admin",
            "maintain",
            "write",
        ):
            why.append("the reviewer has no write access")
        if why:
            notes.append(f"@{user}'s attestation ignored: {'; '.join(why)}")
            continue
        for c in checks:
            out.setdefault(c, []).append(user)
    return out, notes
