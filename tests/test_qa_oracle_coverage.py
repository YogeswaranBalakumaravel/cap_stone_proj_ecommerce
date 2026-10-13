"""The code review agent's oracle coverage: an `asserted` (or `asserted_differently`) behaviour only
counts with a test citation. On PR #19 the agent marked 17 behaviours `asserted` without naming a
test, so the gate reported them all as "not asserted" with no explanation.

- The review schema now requires `test` for those two statuses, so the CLI rejects the claim and
  the agent has to add the test (checked live: the CLI enforces `if`/`then`).
- The gate's report says why a claim was dropped.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

QA = Path(__file__).resolve().parents[1] / ".qa"
SCHEMA = json.loads((QA / "code-review" / "schemas" / "review.schema.json").read_text("utf-8"))
TEST_FILE = "def test_ok():\n    assert compute(2) == 4\n"
CITATION = {"file": "tests/test_x.py", "line": 2, "evidence": "assert compute(2) == 4"}


def coverage_errors(*items) -> list[str]:
    """Validate items as the review's oracle_coverage against the whole real schema (Draft 7, as
    the Claude CLI does) and return the error messages for those items."""
    errors = Draft7Validator(SCHEMA).iter_errors({"oracle_coverage": list(items)})
    return [e.message for e in errors if list(e.absolute_path)[:1] == ["oracle_coverage"]]


def test_review_schema_is_a_valid_draft_7_schema():
    Draft7Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("status", ["asserted", "asserted_differently"])
def test_schema_rejects_an_asserted_claim_without_a_test(status):
    errors = coverage_errors({"behaviour_id": "AC-1.B1", "status": status})

    assert "'test' is a required property" in errors


@pytest.mark.parametrize("status", ["asserted", "asserted_differently"])
def test_schema_accepts_an_asserted_claim_with_a_test(status):
    assert coverage_errors({"behaviour_id": "AC-1.B1", "status": status, "test": CITATION}) == []


def test_schema_lets_missing_go_without_a_test():
    assert coverage_errors({"behaviour_id": "AC-1.B1", "status": "missing"}) == []


@pytest.mark.parametrize("dropped", ["file", "line", "evidence"])
def test_schema_test_citation_needs_file_line_and_evidence(dropped):
    test = {k: v for k, v in CITATION.items() if k != dropped}

    errors = coverage_errors({"behaviour_id": "AC-1.B1", "status": "asserted", "test": test})

    assert f"'{dropped}' is a required property" in errors


def test_review_prompt_says_asserted_needs_a_test():
    prompt = (QA / "code-review" / "prompts" / "review.md").read_text(encoding="utf-8")

    assert "`test` is required" in prompt


@pytest.fixture
def gate(monkeypatch, tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(TEST_FILE, encoding="utf-8")
    monkeypatch.setenv("CR_REPO", str(tmp_path))
    monkeypatch.syspath_prepend(str(QA / "agent"))
    monkeypatch.syspath_prepend(str(QA / "code-review"))
    for name in ("cr_common", "run_agent", "common"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location("qa_cr_gate", QA / "code-review" / "gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BEHAVIOURS = {
    "AC-1.B1": {"id": "AC-1.B1", "criterion": "AC-1", "kind": "positive", "then": "x"},
    "AC-1.B2": {"id": "AC-1.B2", "criterion": "AC-1", "kind": "edge", "then": "y"},
    "AC-2.B1": {"id": "AC-2.B1", "criterion": "AC-2", "kind": "negative", "then": "z",
                "assumption": True},
}
GOOD = CITATION


def rows_for(gate, coverage):
    review = {"oracle_coverage": coverage}
    rows, gaps = gate.oracle_coverage(review, BEHAVIOURS, gate.Verifier(3, {}))
    return {r["id"]: r for r in rows}, gaps


def test_asserted_with_a_verified_test_counts(gate):
    rows, gaps = rows_for(gate, [{"behaviour_id": "AC-1.B1", "status": "asserted", "test": GOOD}])

    assert rows["AC-1.B1"]["status"] == "asserted"
    assert rows["AC-1.B1"]["test"] == "tests/test_x.py:2"
    assert gaps == {}


def test_asserted_differently_with_a_verified_test_stays_a_gap(gate):
    claim = {"behaviour_id": "AC-1.B1", "status": "asserted_differently", "test": GOOD}

    rows, gaps = rows_for(gate, [claim])

    assert (rows["AC-1.B1"]["status"], rows["AC-1.B1"]["test"]) == (
        "asserted_differently",
        "tests/test_x.py:2",
    )
    assert [(b["id"], status, f"{rel}:{n}") for b, status, rel, n in gaps["AC-1"]] == [
        ("AC-1.B1", "asserted_differently", "tests/test_x.py:2")
    ]


@pytest.mark.parametrize("review", [{}, {"oracle_coverage": None}, {"oracle_coverage": []}])
def test_no_oracle_coverage_gives_no_rows_and_no_gaps(gate, review):
    assert gate.oracle_coverage(review, BEHAVIOURS, gate.Verifier(3, {})) == ([], {})


def test_dropped_claim_without_a_note_gets_only_the_reason(gate):
    rows, _ = rows_for(gate, [{"behaviour_id": "AC-1.B1", "status": "asserted"}])

    assert rows["AC-1.B1"]["note"] == "the agent said asserted, but it cited no test."


@pytest.mark.parametrize("status", ["asserted", "asserted_differently"])
def test_claim_without_a_test_is_missing_and_says_why(gate, status):
    rows, gaps = rows_for(gate, [{"behaviour_id": "AC-1.B1", "status": status, "note": "seen"}])

    row = rows["AC-1.B1"]
    assert row["status"] == "missing"
    assert row["note"] == f"the agent said {status}, but it cited no test. seen"
    assert [x[0]["id"] for x in gaps["AC-1"]] == ["AC-1.B1"]


def test_claim_with_a_bad_citation_names_the_reason(gate):
    bad = {**GOOD, "evidence": "assert nothing_like_this()"}

    rows, _ = rows_for(gate, [{"behaviour_id": "AC-1.B2", "status": "asserted", "test": bad}])

    assert rows["AC-1.B2"]["status"] == "missing"
    assert rows["AC-1.B2"]["note"] == (
        "the agent said asserted, but its test citation didn't check out "
        "(quoted evidence isn't on or near that line)."
    )


def test_missing_keeps_the_agents_note(gate):
    rows, _ = rows_for(gate, [{"behaviour_id": "AC-1.B2", "status": "missing", "note": "no test"}])

    assert (rows["AC-1.B2"]["status"], rows["AC-1.B2"]["note"]) == ("missing", "no test")


def test_assumptions_and_unknown_behaviours_are_not_gaps(gate):
    rows, gaps = rows_for(
        gate,
        [
            {"behaviour_id": "AC-2.B1", "status": "asserted"},  # an assumption, never a gap
            {"behaviour_id": "AC-9.B9", "status": "asserted"},  # not in the oracle: ignored
            "not a dict",
        ],
    )

    assert set(rows) == {"AC-2.B1"}
    assert rows["AC-2.B1"]["status"] == "missing"
    assert gaps == {}
