"""Which Jira tickets hold a run's requirements. Python standard library only.

A PR names its tickets: Jira links anywhere in its description, keys in the description's "Ticket"
section, and keys in its title or branch name. A run with no PR (the full-project audit) has
nothing to read them from, so it has to be given them: QA_JIRA_LINKS (the workflows' `jira`
input), or a prompt when it's run by hand in a terminal. Shared with the code review agent.

Credentials only ever go to JIRA_BASE_URL. A link to any other host names no ticket, so a PR
can't send the Jira token somewhere else by linking there.
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlsplit

AUDIT_ENV = "QA_JIRA_LINKS"
MAX_KEYS = 10
KEY = r"[A-Z][A-Z0-9]+-[0-9]+"
# A browse link (Cloud and Data Center), or a board or search link with ?selectedIssue=KEY.
LINK = re.compile(
    r"(https?://[^/\s<>()\[\]\"'`]+)[^\s<>()\[\]\"'`]*?(?:/browse/|[?&]selectedIssue=)"
    rf"({KEY})(?![A-Za-z0-9-])"
)
COMMENT = re.compile(r"<!--.*?-->", re.S)
TICKET_HEADING = re.compile(r"^\s*#{1,6}\s*.*\b(ticket|jira|issue)s?\b", re.I)
# Criteria ids look like keys (AC-1), and so do a few standards; none of them is a ticket.
NOT_TICKETS = re.compile(r"^(AC|UTF|SHA|ISO|RFC|CVE|GHSA)-")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _from_links(text: str) -> list[str]:
    """Keys of the Jira links in text that point at JIRA_BASE_URL (any host when it's unset)."""
    base = _host(os.environ.get("JIRA_BASE_URL", ""))
    found = []
    for url, key in LINK.findall(text or ""):
        if base and _host(url) != base:
            print(f"::warning::Ignoring {key}: its link isn't on JIRA_BASE_URL ({base}).")
            continue
        found.append(key)
    return found


def _bare(text: str, key_regex: str) -> list[str]:
    return [m.group(0) for m in re.finditer(key_regex or KEY, text or "")]


def _ticket_section(body: str) -> str:
    out, inside = [], False
    for line in body.splitlines():
        if re.match(r"^\s*#{1,6}\s", line):
            inside = bool(TICKET_HEADING.match(line))
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def _distinct(keys: list[str]) -> list[str]:
    out = []
    for k in keys:
        if k not in out and not NOT_TICKETS.match(k):
            out.append(k)
    return out[:MAX_KEYS]


def pr_keys(title: str, body: str, branch: str, key_regex: str = KEY) -> list[str]:
    """The tickets a PR names, links first, in the order they appear."""
    body = COMMENT.sub("", body or "")  # the template's comments hold example keys
    return _distinct(
        _from_links(body)
        + _bare(_ticket_section(body), key_regex)
        + _bare(title, key_regex)
        + _bare(branch, key_regex)
    )


def audit_keys(raw: str, key_regex: str = KEY) -> list[str]:
    """The tickets given to a run with no PR: links or bare keys, separated by commas or spaces."""
    keys = []
    for part in re.split(r"[\s,;]+", raw or ""):
        if not part:
            continue
        linked = _from_links(part)
        if linked or "://" in part:
            keys += linked
        else:
            keys += _bare(part, key_regex)
    return _distinct(keys)


def ask() -> str:
    """The audit's Jira links: QA_JIRA_LINKS, or a prompt when run by hand in a terminal."""
    raw = os.environ.get(AUDIT_ENV, "").strip()
    if raw or os.environ.get("GITHUB_ACTIONS") or not sys.stdin.isatty():
        return raw
    try:
        return input("Jira links or keys whose requirements the audit checks: ").strip()
    except EOFError:
        return ""


def require_for_audit(keys: list[str], read: list[str]) -> None:
    """Stop a full-project audit that has no requirements: it has no PR to fall back on."""
    if not keys:
        sys.exit(
            "::error::A full-project audit has no PR to read requirements from. Run it again with "
            "the Jira links (or keys) of the tickets it should check in the `jira` input "
            f"(locally: {AUDIT_ENV}='https://your-org.atlassian.net/browse/PROJ-1 PROJ-2')."
        )
    if not (os.environ.get("JIRA_BASE_URL") and os.environ.get("JIRA_API_TOKEN")):
        sys.exit(
            "::error::A full-project audit reads its requirements from Jira, but JIRA_BASE_URL or "
            "JIRA_API_TOKEN isn't set (repository variable and secret)."
        )
    if not read:
        sys.exit(f"::error::Couldn't read any of {', '.join(keys)} from Jira; see the warnings.")
