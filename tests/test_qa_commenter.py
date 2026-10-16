"""The gate findings commenter (.qa/commenter) turns every gate's output into one PR comment.

Covers each gate's parser against the output its tool really writes, what counts as blocking,
telling "no findings" apart from "not known", the comment's escaping of PR-controlled text, keeping
secrets out of the comment, reading artifacts, picking each gate's latest run, and editing only the
commenter's own comment.
"""

import importlib.util
import io
import json
import re
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

HOME = Path(__file__).resolve().parents[1] / ".qa" / "commenter"


def load(name: str):
    """Load a commenter module by path; commenter.py imports findings.py by its bare name."""
    spec = importlib.util.spec_from_file_location(name, HOME / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


F = load("findings")
C = load("commenter")

RUNNER = "/home/runner/work/cap_stone_proj_ecommerce/cap_stone_proj_ecommerce"

RUFF = [
    {
        "code": "F401",
        "message": "`os` imported but unused",
        "filename": f"{RUNNER}/app/routes.py",
        "location": {"row": 3, "column": 8},
        "fix": {"applicability": "safe", "message": "Remove unused import: `os`", "edits": []},
    },
    {
        "code": None,
        "message": "SyntaxError: Expected an expression",
        "filename": f"{RUNNER}/app/models.py",
        "location": {"row": 12, "column": 1},
        "fix": None,
    },
]

BANDIT = {
    "errors": [],
    "results": [
        {
            "filename": "app/routes.py",
            "line_number": 40,
            "issue_severity": "HIGH",
            "issue_confidence": "HIGH",
            "issue_cwe": {"id": 78, "link": "https://cwe.mitre.org/data/definitions/78.html"},
            "issue_text": "subprocess call with shell=True identified, security issue.",
            "test_id": "B602",
            "test_name": "subprocess_popen_with_shell_equals_true",
        },
        {
            "filename": "app/seed_data.py",
            "line_number": 2,
            "issue_severity": "LOW",
            "issue_confidence": "HIGH",
            "issue_cwe": {"id": 78},
            "issue_text": "Consider possible security implications of the subprocess module.",
            "test_id": "B404",
            "test_name": "blacklist",
        },
    ],
}

JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">
<testsuite name="pytest" errors="1" failures="1" skipped="0" tests="3">
<testcase classname="tests.test_routes" name="test_index_returns_200" time="0.01" />
<testcase classname="tests.test_routes" name="test_unknown_phone_404" time="0.01">
<failure message="assert 200 == 404&#10; +  where 200 = response.status_code">def test_x():
&gt;       assert resp.status_code == 404
E       assert 200 == 404

tests/test_routes.py:57: AssertionError</failure></testcase>
<testcase classname="tests.test_models.TestPhone" name="test_to_dict" time="0.01">
<error message="failed on setup with &quot;fixture 'db' not found&quot;">fixture missing</error>
</testcase>
</testsuite></testsuites>"""

SECRET = "ghp_EXAMPLEexampleEXAMPLEexample1234"
SARIF = {
    "runs": [
        {
            "results": [
                {
                    "ruleId": "github-pat",
                    "message": {"text": "github-pat has detected secret for file config.py."},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "config.py"},
                                "region": {"startLine": 9, "snippet": {"text": SECRET}},
                            }
                        }
                    ],
                    "partialFingerprints": {"commitSha": "abc", "author": "someone"},
                }
            ]
        }
    ]
}

CODE_REVIEW = {
    "findings": [
        {"check": "injection_surfaces", "status": "BLOCK", "severity": "high",
         "file": "app/routes.py", "line": 30, "title": "Raw SQL built from the query string",
         "explanation": "brand is formatted into SQL.", "suggestion": "Use a bound parameter."},
        {"check": "plausible_logic", "status": "NEEDS_ATTESTATION", "severity": "high",
         "file": "app/routes.py", "line": 44, "title": "Sort order reversed",
         "explanation": "price_desc sorts ascending.", "suggestion": "Use .desc()."},
        {"check": "scope_creep", "status": "MUST_REVIEW", "severity": "medium",
         "file": "app/models.py", "line": 5, "title": "Unrelated model change",
         "explanation": "Not in the ticket.", "suggestion": "Split it out."},
        {"check": "plausible_logic", "status": "ATTESTED", "severity": "high",
         "file": "app/routes.py", "line": 50, "title": "Already approved", "explanation": "",
         "suggestion": ""},
    ]
}
TITLES = {
    "injection_surfaces": "Injection surfaces",
    "plausible_logic": "Plausible-but-wrong logic",
}


def tq_metrics(mode="blocking", status="FAIL", can_block=True):
    return {
        "mode": mode,
        "dimensions": {
            "edge_cases": {"status": status, "can_block": can_block},
            "trivial_assertions": {"status": "PASS", "can_block": True},
        },
        "findings": [
            {"dimension": "edge_cases", "severity": "high", "file": "tests/test_routes.py",
             "line": 20, "title": "No test for an unknown brand",
             "evidence": "Only apple and samsung are requested.",
             "recommendation": "Request ?brand=nokia and assert the full catalog."},
            {"dimension": "trivial_assertions", "severity": "low", "file": "tests/test_models.py",
             "line": 8, "title": "assert True", "evidence": "", "recommendation": ""},
        ],
    }


def gate(name="Gate", status="completed", conclusion="success", found=(), note="", url=None):
    """A collected gate; found=None means its findings couldn't be read."""
    return {"name": name, "status": status, "conclusion": conclusion, "url": url,
            "findings": None if found is None else list(found), "note": note}


def zipped(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


# ---- parsers ----


def test_ruff_findings_are_blocking_with_repo_relative_paths_and_fixes():
    out = F.parse_ruff(RUFF)
    assert [(f["file"], f["line"]) for f in out] == [("app/routes.py", 3), ("app/models.py", 12)]
    assert out[0]["title"] == "F401: `os` imported but unused"
    assert out[0]["fix"] == "Remove unused import: `os`"
    assert out[1]["title"].startswith("syntax: SyntaxError")
    assert out[1]["fix"] == ""
    assert all(f["blocking"] and f["severity"] == "high" for f in out)


def test_bandit_maps_severity_and_keeps_confidence_and_cwe():
    out = F.parse_bandit(BANDIT)
    assert [f["severity"] for f in out] == ["high", "low"]
    assert out[0]["title"] == "B602 subprocess_popen_with_shell_equals_true"
    assert out[0]["detail"].endswith("(confidence high, CWE-78)")
    assert (out[0]["file"], out[0]["line"]) == ("app/routes.py", 40)
    assert all(f["blocking"] for f in out)


@pytest.mark.parametrize("data", [{}, {"results": []}])
def test_bandit_with_no_results_reports_nothing(data):
    assert F.parse_bandit(data) == []


@pytest.mark.parametrize(
    "parser, data",
    [
        (F.parse_bandit, None),
        (F.parse_bandit, ["not", "an", "object"]),
        (F.parse_bandit, {"results": "not a list"}),
        (F.parse_ruff, {"not": "a list"}),
        (F.parse_sarif, "text"),
        (F.parse_code_review, []),
        (F.parse_code_review, {"findings": {"a": 1}}),
        (F.parse_test_quality, None),
    ],
)
def test_output_in_an_unexpected_shape_is_unreadable_not_clean(parser, data):
    with pytest.raises(ValueError):
        parser(data)


def test_entries_that_arent_objects_are_skipped_not_fatal():
    out = F.parse_ruff(["junk", 3, None, RUFF[0]])
    assert [f["title"] for f in out] == ["F401: `os` imported but unused"]
    metrics = tq_metrics()
    metrics["dimensions"] = ["not", "a", "dict"]  # every finding then counts as non-blocking
    metrics["findings"].append("junk")
    assert [f["blocking"] for f in F.parse_test_quality(metrics)] == [False, False]


@pytest.mark.parametrize("test_id", ["B105", "B106", "B107"])
def test_bandit_hardcoded_password_findings_never_carry_the_password(test_id):
    password = "Sup3rS3cretValue!"
    data = {"results": [{
        "filename": "config.py", "line_number": 4, "issue_severity": "LOW",
        "issue_confidence": "MEDIUM", "issue_cwe": {"id": 259}, "test_id": test_id,
        "test_name": "hardcoded_password_string",
        "issue_text": f"Possible hardcoded password: '{password}'",
    }]}
    out = F.parse_bandit(data)
    assert out[0]["detail"].startswith("Possible hardcoded password (the value is left out")
    body = F.render([gate("Lint", conclusion="failure", found=out)], "abc1234")
    assert password not in json.dumps(out) and password not in body


def test_junit_reports_failed_and_errored_tests_at_the_failing_line():
    out = F.parse_junit(JUNIT)
    assert len(out) == 2  # the passing test isn't a finding
    failed, errored = out
    assert failed["title"] == "test_unknown_phone_404 failed"
    assert (failed["file"], failed["line"]) == ("tests/test_routes.py", 57)
    assert failed["detail"] == "assert 200 == 404"
    assert failed["fix"] == (
        "Run `pytest tests/test_routes.py::test_unknown_phone_404` locally to reproduce."
    )
    # A test class in the classname isn't part of the file path.
    assert errored["file"] == "tests/test_models.py"
    assert errored["title"] == "test_to_dict errored"
    assert all(f["blocking"] and f["severity"] == "blocker" for f in out)


def test_truncated_junit_is_unreadable_not_clean():
    with pytest.raises(ValueError, match="doesn't parse"):
        F.parse_junit("<testsuites><testcase")


def test_junit_with_a_doctype_is_refused_before_parsing():
    bomb = '<?xml version="1.0"?><!DOCTYPE t [<!ENTITY a "aaaa">]><testsuites>&a;</testsuites>'
    with pytest.raises(ValueError, match="DOCTYPE"):
        F.parse_junit(bomb)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (f"file://{RUNNER}/config.py", "config.py"),
        ("file://config.py", "config.py"),
        (f"{RUNNER}/tests/test_routes.py", "tests/test_routes.py"),
        (".\\app\\routes.py", "app/routes.py"),
    ],
)
def test_paths_from_every_gate_are_made_repo_relative(raw, expected):
    sarif = json.loads(json.dumps(SARIF))
    sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"][
        "uri"] = raw
    assert F.parse_sarif(sarif)[0]["file"] == expected
    xml = JUNIT.replace('classname="tests.test_routes" name="test_unknown_phone_404"',
                        f'classname="tests.test_routes" name="test_unknown_phone_404" file="{raw}"')
    assert F.parse_junit(xml)[0]["file"] == expected


def test_gitleaks_finding_never_carries_the_secret():
    out = F.parse_sarif(SARIF)
    assert len(out) == 1
    assert out[0]["title"] == "Possible secret (github-pat)"
    assert (out[0]["file"], out[0]["line"]) == ("config.py", 9)
    assert out[0]["blocking"]
    body = F.render([gate("Secret scan (Gitleaks)", conclusion="failure", found=out)], "a" * 40)
    assert SECRET not in json.dumps(out)
    assert SECRET not in body


def test_code_review_blocks_on_block_and_needs_attestation_and_drops_attested():
    out = F.parse_code_review(CODE_REVIEW, TITLES)
    assert [f["title"] for f in out] == [
        "[Injection surfaces] Raw SQL built from the query string",
        "[Plausible-but-wrong logic] Sort order reversed",
        "[scope creep] Unrelated model change",  # no title in config: the id, readable
    ]
    assert [f["blocking"] for f in out] == [True, True, False]
    assert "`attest: plausible_logic`" in out[1]["detail"]
    assert "doesn't block" in out[2]["detail"]
    assert out[0]["fix"] == "Use a bound parameter."


def test_test_quality_finding_blocks_only_when_its_dimension_fails_and_can_block():
    out = F.parse_test_quality(tq_metrics())
    assert [f["blocking"] for f in out] == [True, False]  # trivial_assertions passed
    assert out[0]["title"] == "[edge cases] No test for an unknown brand"
    assert out[0]["fix"] == "Request ?brand=nokia and assert the full catalog."


@pytest.mark.parametrize(
    "kwargs", [{"mode": "advisory"}, {"can_block": False}, {"status": "CONCERN"}]
)
def test_test_quality_finding_does_not_block_in_advisory_cases(kwargs):
    out = F.parse_test_quality(tq_metrics(**kwargs))
    # The same edge_cases finding that blocks by default is still reported, just not blocking.
    assert [f["title"] for f in out][0] == "[edge cases] No test for an unknown brand"
    assert [f["blocking"] for f in out] == [False, False]


def test_code_review_status_outside_the_known_set_is_reported_but_never_blocks():
    data = {"findings": [{"check": "logic", "status": "SOMETHING_NEW", "severity": "high",
                          "file": "app/x.py", "line": 1, "title": "t"}]}
    out = F.parse_code_review(data)
    assert len(out) == 1 and out[0]["blocking"] is False


def test_unknown_severity_is_treated_as_medium():
    assert F.finding("critical", False, "a.py", 1, "t")["severity"] == "medium"


# ---- rendering ----


def test_comment_headline_names_failed_gates_and_counts_blocking_findings():
    lint = F.parse_ruff(RUFF)
    cr = F.parse_code_review(CODE_REVIEW, TITLES)
    body = F.render(
        [
            gate("Lint", conclusion="failure", found=lint, url="https://github.com/o/r/runs/1"),
            gate("Code review agent", conclusion="success", found=cr),
            gate("Secret scan", status=None, conclusion=None),
        ],
        "0123456789abcdef",
        blob_url="https://github.com/o/r/blob/0123456789abcdef",
        updated="2026-10-15 10:00 UTC",
    )
    assert body.startswith(F.MARKER)
    assert "❌ **1 gate(s) failed:** Lint. 5 finding(s), 4 blocking." in body
    assert "Commit `0123456` · updated 2026-10-15 10:00 UTC" in body
    assert "| [Lint](https://github.com/o/r/runs/1) | ❌ Failed | 2 | 2 |" in body
    assert "| Secret scan | ➖ Not run | — | — |" in body  # nothing to count, not "0 found"
    link = "https://github.com/o/r/blob/0123456789abcdef/app/routes.py#L3"
    assert f"[`app/routes.py:3`]({link})" in body
    # A gate with nothing to say gets no section of its own.
    assert "### Secret scan" not in body


def test_comment_says_gates_are_running_until_they_finish():
    body = F.render(
        [gate("A"), gate("B", status="in_progress", conclusion=None, found=None)], "abc1234"
    )
    assert "⏳ **Gates still running.**" in body
    assert "| A | ✅ Passed | 0 | 0 |" in body  # a finished gate with no findings: a real 0
    assert "| B | ⏳ Running | ? | ? |" in body


def test_comment_when_everything_passed_has_no_sections():
    body = F.render([gate("A"), gate("B")], "abc1234")
    assert "✅ **Every gate that ran passed.**" in body
    assert "###" not in body


def test_failed_gate_without_findings_points_to_the_log():
    body = F.render([gate("CI", conclusion="failure")], "abc1234")
    assert "### CI" in body
    assert "failed without reporting findings" in body


def test_unreadable_findings_show_as_unknown_not_zero():
    body = F.render([gate("CI", conclusion="failure", found=None, note="Couldn't read it")], "abc")
    assert "| CI | ❌ Failed | ? | ? |" in body
    assert "Couldn't read it" in body


def test_pr_controlled_text_cannot_inject_html_links_mentions_or_break_the_table():
    hostile = F.finding(
        "high", True, "app/x.py", 1,
        "<img src=x onerror=alert(1)> | [click](javascript:alert(1)) @admin",
        detail="line one\n| --- |\nline two https://evil.example/login www.evil.example",
        fix="<details><summary>",
    )
    body = F.render([gate("CI", conclusion="failure", found=[hostile])], "abc1234")
    row = next(line for line in body.splitlines() if "app/x.py" in line)
    assert "<img" not in body and "<details>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in row
    assert "\\[click\\](javascript:" in row  # both brackets escaped, so no link forms
    assert not re.search(r"(?<!\\)\]\(javascript:", body)
    # Bare URLs would autolink; a zero-width space keeps them as plain text.
    assert "https://evil" not in body and "www.evil" not in body
    assert f"https:{F.ZWSP}//evil.example/login" in row and f"www{F.ZWSP}.evil" in row
    assert "@admin" not in body and "@‍admin" in row
    assert row.count(" | ") == 4  # only the five columns' own separators; the text adds none


@pytest.mark.parametrize(
    "path",
    ["../../etc/passwd", "/etc/passwd", "app/./x.py", "app/x.py) [y](https://evil.example"],
)
def test_odd_paths_are_shown_but_never_linked(path):
    body = F.render(
        [gate("CI", conclusion="failure", found=[F.finding("high", True, path, 1, "t")])],
        "abc1234", blob_url="https://github.com/o/r/blob/abc1234",
    )
    assert "https://github.com/o/r/blob/abc1234/" not in body
    assert "evil.example)" not in body


def test_blocking_findings_list_first_and_the_rest_collapse_past_the_row_limit():
    rows = [F.finding("info", False, f"app/f{i}.py", i + 1, f"note {i}") for i in range(30)]
    rows.append(F.finding("low", True, "app/z.py", 1, "the blocker"))
    body = F.render([gate("CI", conclusion="failure", found=rows)], "abc1234")
    table = [line for line in body.splitlines() if line.startswith("| 🚫") or "| 🔵" in line]
    assert "the blocker" in table[0]
    assert len(table) == F.MAX_ROWS
    assert f"…and {31 - F.MAX_ROWS} more" in body


@pytest.mark.parametrize("count, more", [(25, None), (26, 1)])
def test_row_limit_boundary(count, more):
    rows = [F.finding("info", False, f"app/f{i}.py", 1, f"note {i}") for i in range(count)]
    body = F.render([gate("CI", found=rows)], "abc1234")
    assert sum(1 for line in body.splitlines() if "| 🔵 info |" in line) == F.MAX_ROWS
    if more is None:
        assert "more in the run's evidence" not in body
    else:
        assert f"…and {more} more in the run's evidence." in body


def test_more_blocking_findings_than_rows_still_shows_only_blocking_ones_and_counts_the_rest():
    rows = [F.finding("high", True, f"app/b{i:02}.py", 1, f"blocker {i}") for i in range(30)]
    rows += [F.finding("info", False, "app/n.py", 1, "note")]
    body = F.render([gate("CI", conclusion="failure", found=rows)], "abc1234")
    shown = [line for line in body.splitlines() if line.startswith("| 🚫 |")]
    assert len(shown) == F.MAX_ROWS and "| 🔵 info |" not in body
    assert "…and 6 more in the run's evidence." in body
    assert "| CI | ❌ Failed | 31 | 30 |" in body  # counts cover everything, not just what's shown


def test_ties_are_ordered_by_file_then_line():
    rows = [F.finding("high", True, "app/b.py", 9, "b9"), F.finding("high", True, "app/a.py", 5,
            "a5"), F.finding("high", True, "app/b.py", 2, "b2")]
    assert [f["title"] for f in sorted(rows, key=F.sort_key)] == ["a5", "b2", "b9"]


def test_finding_without_a_line_links_the_file_and_one_without_a_file_shows_a_dash():
    body = F.render(
        [gate("CI", conclusion="failure", found=[F.finding("high", True, "app/x.py", None, "a"),
                                                 F.finding("high", True, None, None, "b")])],
        "abc1234", blob_url="https://github.com/o/r/blob/abc1234",
    )
    assert "[`app/x.py`](https://github.com/o/r/blob/abc1234/app/x.py) |" in body
    assert "| — | **b** |" in body


def test_paths_in_code_spans_read_as_written():
    f = F.finding("high", True, "app/a&b [1].py", 3, "t")
    body = F.render([gate("CI", conclusion="failure", found=[f])], "abc1234")
    assert "`app/a&b [1].py:3`" in body  # not &amp; or \[ inside the code span


def test_comment_stays_under_githubs_size_limit():
    rows = [F.finding("high", True, "a.py", 1, "x" * 400, "y" * 400, "z" * 400)] * 25
    gates = [gate(f"Gate {i}", conclusion="failure", found=rows) for i in range(40)]
    body = F.render(gates, "abc1234")
    assert len(body) <= F.MAX_BODY + 100
    assert body.endswith("…truncated. See each gate's run.")
    # The cut falls between lines: every table row that's left is whole.
    kept = body.rsplit("\n\n…truncated", 1)[0].splitlines()
    assert all(line.endswith("|") for line in kept if line.startswith("|"))
    assert "| Gate 0 | ❌ Failed | 25 | 25 |" in body  # the summary table survives intact


# ---- commenter: artifacts, runs, comments ----


def test_artifact_files_are_parsed_by_name_in_any_folder_and_others_ignored():
    blob = zipped(
        {
            "ruff.json": json.dumps(RUFF),
            "nested/bandit.json": "﻿" + json.dumps(BANDIT),  # a BOM doesn't break it
            "../../evil.json": "{}",
            "report.md": "ignored",
        }
    )
    out = C.read_artifact(blob, {"ruff.json": "ruff", "bandit.json": "bandit"}, {})
    assert sorted(f["title"].split(":")[0].split()[0] for f in out) == [
        "B404", "B602", "F401", "syntax"
    ]


def test_artifact_file_over_the_size_cap_is_unreadable_not_clean(monkeypatch):
    monkeypatch.setattr(C, "MAX_FILE", 10)
    with pytest.raises(ValueError, match="over the"):
        C.read_artifact(zipped({"ruff.json": json.dumps(RUFF)}), {"ruff.json": "ruff"}, {})


def test_artifact_without_the_expected_file_is_unreadable_not_clean():
    with pytest.raises(ValueError, match="no metrics.json"):
        C.read_artifact(zipped({"report.md": "x"}), {"metrics.json": "test_quality"}, {})


def test_latest_runs_keeps_this_gates_pr_runs_newest_first():
    runs = [
        {"id": 1, "name": "CI - Dev", "event": "pull_request", "created_at": "2026-10-15T09:00Z"},
        {"id": 2, "name": "CI - Dev", "event": "push", "created_at": "2026-10-15T11:00Z"},
        {"id": 3, "name": "CI - Dev", "event": "pull_request", "created_at": "2026-10-15T10:00Z"},
        {"id": 4, "name": "PR Test Gate", "event": "pull_request", "created_at": "2026-10-15T12Z"},
    ]
    assert [r["id"] for r in C.latest_runs(runs, ["CI", "CI - Dev"])] == [3, 1]


class FakeGitHub(C.GitHub):
    """GitHub with canned runs, artifacts and comments, recording what it would send."""

    def __init__(self, runs=(), artifacts=None, blobs=None, comments=()):
        super().__init__("o/r", "token")
        self.runs, self.artifacts, self.blobs = list(runs), artifacts or {}, blobs or {}
        self.comments, self.sent, self.paged = list(comments), [], []

    def runs_for(self, sha):
        return self.runs

    def run_artifacts(self, run_id):
        if isinstance(self.artifacts.get(run_id), Exception):
            raise self.artifacts[run_id]
        return self.artifacts.get(run_id, [])

    def download(self, url):
        if isinstance(self.blobs[url], Exception):
            raise self.blobs[url]
        return self.blobs[url]

    def pages(self, path, key=None, limit=10):
        self.paged.append(path)
        return self.comments

    def _request(self, method, path, body=None, opener=None):
        self.sent.append((method, path, body))
        return io.BytesIO(b"{}")


def run(rid, name, event="pull_request", status="completed", conclusion="success", at="T1"):
    return {"id": rid, "name": name, "event": event, "status": status,
            "conclusion": conclusion, "created_at": at, "html_url": f"https://x/runs/{rid}"}


def test_collect_falls_back_to_the_earlier_run_when_the_review_rerun_kept_no_artifact():
    gh = FakeGitHub(
        runs=[
            run(1, "Code review agent", at="T1", conclusion="failure"),
            run(2, "Code review agent", event="pull_request_review", at="T2"),
        ],
        artifacts={1: [{"name": "code-review-evidence", "archive_download_url": "u1"}]},
        blobs={"u1": zipped({"findings.json": json.dumps(CODE_REVIEW)})},
    )
    cr = next(g for g in C.collect(gh, "sha", TITLES) if g["name"] == "Code review agent")
    assert cr["conclusion"] == "success" and cr["url"] == "https://x/runs/2"  # result: latest run
    assert len(cr["findings"]) == 3  # findings: the run that kept them
    assert "earlier run" in cr["note"]


def test_collect_marks_gates_that_didnt_run_running_or_couldnt_be_read():
    gh = FakeGitHub(
        runs=[
            run(1, "PR Test Gate", status="in_progress", conclusion=None),
            run(2, "CI - Dev", conclusion="failure"),
            run(3, "Test quality agent", conclusion="failure"),
        ],
        artifacts={
            2: [{"name": "lint-security-report", "archive_download_url": "u2"}],
            3: [{"name": "test-quality-evidence", "archive_download_url": "u3"}],
        },
        blobs={"u2": OSError("connection reset"), "u3": b"not a zip"},
    )
    gates = {g["name"]: g for g in C.collect(gh, "sha", {})}
    assert gates["Secret scan (Gitleaks)"]["status"] is None
    assert gates["PR tests (pytest)"]["status"] == "in_progress"
    assert gates["PR tests (pytest)"]["findings"] is None  # not known yet, not "none found"
    lint = gates["Lint and security scan (ruff, bandit)"]
    assert lint["findings"] is None and "connection reset" in lint["note"]
    assert gates["Test quality agent"]["findings"] is None
    assert "Couldn't read" in gates["Test quality agent"]["note"]


@pytest.mark.parametrize("conclusion", ["failure", "success"])
def test_a_run_that_kept_no_findings_file_shows_unknown_counts(conclusion):
    gh = FakeGitHub(runs=[run(1, "CI", conclusion=conclusion)])
    lint = C.collect(gh, "sha", {})[0]
    assert lint["findings"] is None and "kept a findings file" in lint["note"]
    body = F.render([lint], "abc1234")
    assert f"| [{lint['name']}](https://x/runs/1) | " in body and " | ? | ? |" in body


def test_gitleaks_passing_without_a_sarif_is_a_real_zero_but_failing_without_one_is_unknown():
    clean = C.collect(FakeGitHub(runs=[run(1, "Gitleaks Secret Scan")]), "sha", {})[2]
    assert clean["name"] == "Secret scan (Gitleaks)" and clean["findings"] == []
    assert clean["note"] == ""
    leak = FakeGitHub(runs=[run(1, "Gitleaks Secret Scan", conclusion="failure")])
    assert C.collect(leak, "sha", {})[2]["findings"] is None


def test_a_passing_gitleaks_rerun_never_shows_an_earlier_runs_leaks():
    gh = FakeGitHub(
        runs=[run(1, "Gitleaks Secret Scan", at="T1", conclusion="failure"),
              run(2, "Gitleaks Secret Scan", at="T2", conclusion="success")],
        artifacts={1: [{"name": "gitleaks-results.sarif", "archive_download_url": "g"}]},
        blobs={"g": zipped({"results.sarif": json.dumps(SARIF)})},
    )
    leak = C.collect(gh, "sha", {})[2]
    assert leak["conclusion"] == "success" and leak["findings"] == [] and leak["note"] == ""


def test_the_result_comes_from_the_newest_run_even_when_an_older_one_passed():
    gh = FakeGitHub(
        runs=[run(1, "PR Test Gate", at="T1", conclusion="success"),
              run(2, "PR Test Gate", at="T2", conclusion="failure")],
        artifacts={2: [{"name": "pytest-report", "archive_download_url": "p"}]},
        blobs={"p": zipped({"pytest-report.xml": JUNIT})},
    )
    tests = C.collect(gh, "sha", {})[1]
    assert tests["conclusion"] == "failure" and tests["url"] == "https://x/runs/2"
    assert len(tests["findings"]) == 2 and tests["note"] == ""


def test_runs_started_at_the_same_moment_are_ordered_by_id():
    runs = [run(7, "CI", at="T1"), run(9, "CI", at="T1"), run(8, "CI", at="T1")]
    assert [r["id"] for r in C.latest_runs(runs, ["CI"])] == [9, 8, 7]


def test_an_unreadable_artifact_with_a_secret_in_it_leaves_the_secret_out_of_the_note():
    gh = FakeGitHub(
        runs=[run(1, "Gitleaks Secret Scan", conclusion="failure")],
        artifacts={1: [{"name": "gitleaks-results.sarif", "archive_download_url": "g"}]},
        blobs={"g": zipped({"results.sarif": '{"runs": [' + SECRET})},
    )
    leak = C.collect(gh, "sha", {})[2]
    assert leak["findings"] is None and "Couldn't read" in leak["note"]
    assert SECRET not in leak["note"] and SECRET not in F.render([leak], "abc1234")


def test_gitleaks_sarif_artifact_is_read_under_its_own_name():
    gh = FakeGitHub(
        runs=[run(1, "Gitleaks Secret Scan", conclusion="failure")],
        artifacts={1: [{"name": "gitleaks-results.sarif", "archive_download_url": "g"}]},
        blobs={"g": zipped({"results.sarif": json.dumps(SARIF)})},
    )
    leak = C.collect(gh, "sha", {})[2]
    assert [f["title"] for f in leak["findings"]] == ["Possible secret (github-pat)"]


def test_fallback_to_an_earlier_run_is_noted_even_when_it_found_nothing():
    gh = FakeGitHub(
        runs=[run(1, "Code review agent", at="T1"),
              run(2, "Code review agent", event="pull_request_review", at="T2")],
        artifacts={1: [{"name": "code-review-evidence", "archive_download_url": "u1"}]},
        blobs={"u1": zipped({"findings.json": json.dumps({"findings": []})})},
    )
    cr = C.collect(gh, "sha", {})[4]
    assert cr["findings"] == [] and "earlier run" in cr["note"]


def test_expired_artifact_is_passed_over_for_an_earlier_run():
    gh = FakeGitHub(
        runs=[run(1, "Test quality agent", at="T1"), run(2, "Test quality agent", at="T2")],
        artifacts={
            2: [{"name": "test-quality-evidence", "archive_download_url": "old", "expired": True}],
            1: [{"name": "test-quality-evidence", "archive_download_url": "u1"}],
        },
        blobs={"u1": zipped({"metrics.json": json.dumps(tq_metrics())})},
    )
    tq = C.collect(gh, "sha", {})[3]
    assert len(tq["findings"]) == 2 and "earlier run" in tq["note"]


def test_one_gate_failing_to_list_its_artifacts_doesnt_stop_the_others():
    gh = FakeGitHub(
        runs=[run(1, "CI - Dev", conclusion="failure"), run(2, "PR Test Gate")],
        artifacts={
            1: urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None),
            2: [{"name": "pytest-report", "archive_download_url": "p"}],
        },
        blobs={"p": zipped({"pytest-report.xml": JUNIT})},
    )
    lint, tests = C.collect(gh, "sha", {})[:2]
    assert lint["findings"] is None and "503" in lint["note"]
    assert len(tests["findings"]) == 2


def test_pages_follows_every_page_so_a_comment_past_the_first_100_is_found(monkeypatch):
    gh = C.GitHub("o/r", "t")
    filler = [{"id": i, "user": {"login": "someone"}, "body": "hi"} for i in range(100)]
    ours = {"id": 500, "user": {"login": C.BOT}, "body": F.MARKER + " old"}
    served = {1: filler, 2: [ours]}
    asked = []

    def get(path):
        asked.append(path)
        return served[int(path.rsplit("page=", 1)[1])]

    monkeypatch.setattr(gh, "get", get)
    sent = []
    monkeypatch.setattr(gh, "_request", lambda *a, **k: sent.append(a[:2]) or io.BytesIO(b"{}"))
    assert gh.upsert_comment(5, F.MARKER + " new") == "updated comment 500"
    assert asked == ["/repos/o/r/issues/5/comments?per_page=100&page=1",
                     "/repos/o/r/issues/5/comments?per_page=100&page=2"]
    assert sent == [("PATCH", "/repos/o/r/issues/comments/500")]  # edited, not duplicated


def test_comment_is_edited_in_place_and_only_the_bots_own():
    quoted = {"id": 7, "user": {"login": "someone"}, "body": f"look: {F.MARKER}"}
    ours = {"id": 9, "user": {"login": C.BOT}, "body": F.MARKER + " old"}
    gh = FakeGitHub(comments=[ours, quoted])
    assert gh.upsert_comment(5, F.MARKER + " new") == "updated comment 9"
    assert gh.sent == [("PATCH", "/repos/o/r/issues/comments/9", {"body": F.MARKER + " new"})]


def test_unchanged_comment_is_not_rewritten_and_a_missing_one_is_created():
    same = FakeGitHub(comments=[{"id": 9, "user": {"login": C.BOT}, "body": F.MARKER}])
    assert same.upsert_comment(5, F.MARKER) == "comment 9 already up to date"
    assert same.sent == []
    fresh = FakeGitHub(comments=[{"id": 7, "user": {"login": "someone"}, "body": F.MARKER}])
    assert fresh.upsert_comment(5, "body") == "created a comment"
    assert fresh.sent == [("POST", "/repos/o/r/issues/5/comments", {"body": "body"})]


def test_another_bot_comment_without_the_marker_is_left_alone():
    other = FakeGitHub(comments=[{"id": 3, "user": {"login": C.BOT}, "body": "Deploy preview"}])
    assert other.upsert_comment(5, F.MARKER) == "created a comment"
    assert [m for m, *_ in other.sent] == ["POST"]


def test_find_pr_matches_a_fork_pr_by_its_head_commit():
    gh = FakeGitHub(comments=[{"number": 3, "head": {"sha": "other"}},
                              {"number": 8, "head": {"sha": "abc"}}])
    assert gh.find_pr({"pull_requests": [{"number": 4}], "head_sha": "abc"}) == 4
    assert gh.find_pr({"pull_requests": [], "head_sha": "abc"}) == 8
    assert gh.find_pr({"pull_requests": [], "head_sha": "zzz"}) is None
    # Only open PRs are searched, so a closed PR with the same head gets no comment.
    assert gh.paged and all("state=open" in path for path in gh.paged)


def test_main_ignores_runs_that_werent_for_a_pull_request(monkeypatch, tmp_path, capsys):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"workflow_run": {"event": "push", "head_sha": "abc"}}))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setattr(C, "GitHub", lambda *a: pytest.fail("shouldn't call GitHub"))
    assert C.main() == 0
    assert "Not a pull request run" in capsys.readouterr().out


def test_main_without_credentials_fails_loudly(monkeypatch):
    for name in ("GITHUB_REPOSITORY", "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_EVENT_PATH"):
        monkeypatch.delenv(name, raising=False)
    assert C.main() == 1


def test_main_dry_run_prints_the_comment_and_writes_the_job_summary(monkeypatch, tmp_path, capsys):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"workflow_run": {
        "event": "pull_request", "head_sha": "abc1234def", "pull_requests": [{"number": 5}],
        "head_repository": {"full_name": "o/r"}}}))
    summary = tmp_path / "summary.md"
    gh = FakeGitHub(runs=[run(1, "CI - Dev", conclusion="failure")],
                    artifacts={1: [{"name": "lint-security-report", "archive_download_url": "u"}]},
                    blobs={"u": zipped({"ruff.json": json.dumps(RUFF)})})
    env = {"GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "t", "COMMENTER_DRY_RUN": "1",
           "GITHUB_EVENT_PATH": str(event), "GITHUB_STEP_SUMMARY": str(summary)}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(C, "GitHub", lambda *a: gh)
    assert C.main() == 0
    out = capsys.readouterr().out
    assert out.startswith(F.MARKER) and "F401" in out
    assert gh.sent == []  # dry run: nothing posted
    assert "F401" in summary.read_text(encoding="utf-8")
    assert F.MARKER not in summary.read_text(encoding="utf-8")
