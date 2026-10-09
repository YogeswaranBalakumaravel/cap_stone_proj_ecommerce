"""The QA gates verify every file:line citation the agents make. Agents sometimes give a line's
position in the diff they read (diff.patch, context.md) instead of its line in the file, which made
the gates drop real test evidence and block PRs whose tests were fine.

- Test quality gate (.qa/agent): a named test cited outside its body moves to its `def` line.
- Code review gate (.qa/code-review): a quote found on exactly one line of the file moves there.
Anything ambiguous or unverifiable is still rejected.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

QA = Path(__file__).resolve().parents[1] / ".qa"

TEST_FILE = '''\
import pytest


def test_first():
    value = 1
    assert value == 1


@pytest.mark.parametrize("n", [1, 2])
def test_second(n):
    assert n > 0


class TestGroup:
    def test_twice(self):
        assert True


class TestOther:
    def test_twice(self):
        assert True
'''
FIRST_DEF, FIRST_ASSERT, SECOND_DEF = 4, 6, 10


def load(monkeypatch, tmp_path, home: str, module: str, env: str, purge: tuple[str, ...]):
    """Load a QA module fresh, with its repository root pointing at tmp_path."""
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_sample.py").write_text(TEST_FILE, encoding="utf-8")
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "logic.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv(env, str(tmp_path))
    monkeypatch.syspath_prepend(str(QA / home))
    for name in purge:
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(f"qa_{home}_{module}", QA / home / f"{module}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- test quality gate


@pytest.fixture
def tq(monkeypatch, tmp_path):
    common = load(monkeypatch, tmp_path, "agent", "common", "QA_REPO", ("common",))
    return common, common.RefChecker(common.load_config())


def test_test_cited_at_a_diff_position_moves_to_its_def(tq):
    _, refs = tq

    hit = refs.check_test("tests/test_sample.py", 165, "test_first")

    assert hit == ("tests/test_sample.py", FIRST_DEF)
    assert refs.relocated == [
        {"file": "tests/test_sample.py", "name": "test_first", "from": 165, "to": FIRST_DEF}
    ]
    assert refs.rejected == []


def test_test_cited_inside_its_body_keeps_the_cited_line(tq):
    _, refs = tq

    hit = refs.check_test("tests/test_sample.py", FIRST_ASSERT, "test_first")

    assert hit == ("tests/test_sample.py", FIRST_ASSERT)
    assert refs.relocated == []


def test_test_cited_inside_another_test_moves_to_its_own_def(tq):
    _, refs = tq

    hit = refs.check_test("tests/test_sample.py", FIRST_ASSERT, "tests/test_sample.py::test_second")

    assert hit == ("tests/test_sample.py", SECOND_DEF)


@pytest.mark.parametrize(
    ("cited", "kept"),
    [
        (FIRST_DEF, FIRST_DEF),  # first line of the body: kept
        (SECOND_DEF - 2, SECOND_DEF - 2),  # last line before the next test's decorator: kept
        (SECOND_DEF - 1, FIRST_DEF),  # the next test's decorator: outside, moved to the def
        (FIRST_DEF - 1, FIRST_DEF),  # the line before the def: outside, moved to the def
    ],
)
def test_test_body_boundaries(tq, cited, kept):
    _, refs = tq

    assert refs.check_test("tests/test_sample.py", cited, "test_first") == (
        "tests/test_sample.py",
        kept,
    )


def test_test_name_not_defined_in_the_file_is_rejected(tq):
    _, refs = tq

    assert refs.check_test("tests/test_sample.py", 999, "test_missing") is None
    assert refs.rejected[-1]["reason"] == "line 999 is out of range"


def test_test_name_defined_twice_is_not_moved(tq):
    _, refs = tq

    assert refs.check_test("tests/test_sample.py", 999, "test_twice") is None
    assert refs.relocated == []


def test_named_def_in_a_non_test_file_is_rejected(tq):
    _, refs = tq

    assert refs.check_test("app/logic.py", 50, "f") is None
    assert refs.rejected[-1]["reason"] == "not a test file"


def test_def_span_ends_before_the_next_decorated_test(tq):
    common, _ = tq

    assert common.test_def_span("test_first", TEST_FILE) == (FIRST_DEF, SECOND_DEF - 2)
    assert common.test_def_span("test_twice", TEST_FILE) is None
    assert common.test_def_span("", TEST_FILE) is None


def test_criterion_keeps_its_tests_when_cited_at_diff_positions(monkeypatch, tmp_path):
    load(monkeypatch, tmp_path, "agent", "common", "QA_REPO", ("common",))
    gate = load(monkeypatch, tmp_path, "agent", "gate", "QA_REPO", ())
    refs = gate.RefChecker(gate.load_config())
    review = {
        "criteria": [
            {
                "id": "AC-1",
                "status": "covered",
                "tests": [{"file": "tests/test_sample.py", "line": 165, "name": "test_first"}],
            }
        ]
    }

    criterion = gate.validated_review(review, refs)["criteria"][0]

    assert criterion["status"] == "covered"
    assert criterion["downgraded"] is False
    assert criterion["tests"] == [
        {"file": "tests/test_sample.py", "line": FIRST_DEF, "name": "test_first"}
    ]


# ---------------------------------------------------------------- code review gate


@pytest.fixture
def verifier(monkeypatch, tmp_path):
    gate = load(
        monkeypatch, tmp_path, "code-review", "gate", "CR_REPO", ("cr_common", "run_agent")
    )
    return gate.Verifier(3, {})


def cite(line, evidence):
    return {"file": "tests/test_sample.py", "line": line, "evidence": evidence}


def test_quote_within_tolerance_is_verified_in_place(verifier):
    assert verifier.check(cite(FIRST_ASSERT + 2, "assert value == 1"), "finding") == (
        "tests/test_sample.py",
        FIRST_ASSERT,
        "",
    )
    assert verifier.stats["finding_relocated"] == 0


@pytest.mark.parametrize("cited", [FIRST_ASSERT + 6, 165])
def test_unique_quote_at_a_diff_position_moves_to_its_line(verifier, cited):
    result = verifier.check(cite(cited, "assert value == 1"), "oracle")

    assert result == ("tests/test_sample.py", FIRST_ASSERT, "")
    assert verifier.stats["oracle_valid"] == 1
    assert verifier.stats["oracle_relocated"] == 1


def test_quote_on_several_lines_is_not_moved(verifier):
    rel, line, reason = verifier.check(cite(1, "def test_twice(self):"), "oracle")

    assert (rel, line) == (None, None)
    assert reason == "quoted evidence isn't on or near that line"


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        ("n > 0", None),  # unique, but far under 12 characters
        ("ert value =", None),  # unique, 11 characters: one short of the minimum
        ("assert n > 0", ("tests/test_sample.py", SECOND_DEF + 1, "")),  # unique, exactly 12
    ],
)
def test_quote_must_be_at_least_12_characters_to_move(verifier, quote, expected):
    result = verifier.check(cite(1, quote), "oracle")

    assert (result if expected else result[0]) == expected


@pytest.mark.parametrize(
    ("cited", "reason"),
    [(999, "line 999 is out of range"), (1, "quoted evidence isn't on or near that line")],
)
def test_quote_not_in_the_file_is_rejected(verifier, cited, reason):
    assert verifier.check(cite(cited, "assert something_else()"), "finding") == (None, None, reason)
