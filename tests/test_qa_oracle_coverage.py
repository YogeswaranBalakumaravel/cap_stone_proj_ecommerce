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

QA = Path(__file__).resolve().parents[1] / ".qa"
SCHEMA = json.loads((QA / "code-review" / "schemas" / "review.schema.json").read_text("utf-8"))
ITEM = SCHEMA["properties"]["oracle_coverage"]["items"]
TEST_FILE = "def test_ok():\n    assert compute(2) == 4\n"


def required_keys(item: dict) -> set[str]:
    """The keys the schema requires for this oracle_coverage item, following its if/then."""
    keys = set(ITEM["required"])
    cond = ITEM["if"]
    applies = all(k in item for k in cond.get("required", [])) and all(
        item.get(k) in spec["enum"] for k, spec in cond["properties"].items() if k in item
    )
    if applies:
        keys |= set(ITEM["then"]["required"])
    return keys


@pytest.mark.parametrize("status", ["asserted", "asserted_differently"])
def test_schema_requires_a_test_for_asserted_claims(status):
    assert "test" in required_keys({"behaviour_id": "AC-1.B1", "status": status})


def test_schema_lets_missing_go_without_a_test():
    assert required_keys({"behaviour_id": "AC-1.B1", "status": "missing"}) == {
        "behaviour_id",
        "status",
    }


def test_schema_test_citation_needs_file_line_and_evidence():
    assert set(ITEM["properties"]["test"]["required"]) == {"file", "line", "evidence"}


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
GOOD = {"file": "tests/test_x.py", "line": 2, "evidence": "assert compute(2) == 4"}


def rows_for(gate, coverage):
    review = {"oracle_coverage": coverage}
    rows, gaps = gate.oracle_coverage(review, BEHAVIOURS, gate.Verifier(3, {}))
    return {r["id"]: r for r in rows}, gaps


def test_asserted_with_a_verified_test_counts(gate):
    rows, gaps = rows_for(gate, [{"behaviour_id": "AC-1.B1", "status": "asserted", "test": GOOD}])

    assert rows["AC-1.B1"]["status"] == "asserted"
    assert rows["AC-1.B1"]["test"] == "tests/test_x.py:2"
    assert gaps == {}


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
