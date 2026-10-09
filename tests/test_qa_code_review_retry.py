"""The code review agent's passes (.qa/code-review/run_passes.py) get one fresh run when the CLI
gives up on structured output (error_max_structured_output_retries), and only then.

The CLI is replaced by a fake that answers each run with the next scripted envelope.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

QA = Path(__file__).resolve().parents[1] / ".qa"
GAVE_UP = {
    "type": "result",
    "is_error": True,
    "subtype": "error_max_structured_output_retries",
    "errors": ["Failed to provide valid structured output after 5 attempts"],
}
DONE = {
    "type": "result",
    "is_error": False,
    "subtype": "success",
    "structured_output": {"ask": {"restated": "x", "criteria": []}},
}
FAILED_OTHERWISE = {"type": "result", "is_error": True, "subtype": "error_max_turns"}


@pytest.fixture
def run_pass(tmp_path, monkeypatch):
    """Return run(pass_name, envelopes) -> (exit code, number of CLI runs, meta.json)."""
    out = tmp_path / ".cr-out"
    out.mkdir()
    (out / "context.json").write_text(
        json.dumps(
            {
                "summary": {"backend_files": 1, "frontend_files": 0, "test_files": 1},
                "pr": {"acceptance_criteria": [{"id": "AC-1", "text": "x"}]},
            }
        ),
        encoding="utf-8",
    )
    (out / "oracle_input.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CR_REPO", str(tmp_path))
    monkeypatch.setenv("CR_OUT", ".cr-out")
    monkeypatch.setenv("QA_PROVIDER", "oauth")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test-only")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("QA_AGENT_HOME", raising=False)
    monkeypatch.syspath_prepend(str(QA / "agent"))
    monkeypatch.syspath_prepend(str(QA / "code-review"))
    for name in ("cr_common", "run_agent", "common"):  # fresh copies that see this CR_OUT
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(
        "qa_run_passes", QA / "code-review" / "run_passes.py"
    )
    run_passes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_passes)

    def run(pass_name, envelopes):
        script = iter(envelopes)
        calls = []

        def fake_cli(args, stdout, **kwargs):
            calls.append(args)
            stdout.write(json.dumps(next(script)).encode())
            return subprocess.CompletedProcess(args, 0)

        monkeypatch.setattr(run_passes.subprocess, "run", fake_cli)
        monkeypatch.setattr(sys, "argv", ["run_passes.py", pass_name])
        code = run_passes.main()
        meta = json.loads((out / f"{pass_name}.meta.json").read_text(encoding="utf-8"))
        return code, len(calls), meta

    return run


def test_pass_that_gave_up_on_output_is_run_again_and_can_succeed(run_pass):
    code, runs, meta = run_pass("understand", [GAVE_UP, DONE])

    assert (code, runs) == (0, 2)
    assert meta["ok"] is True


def test_pass_is_run_again_only_once(run_pass):
    code, runs, meta = run_pass("understand", [GAVE_UP, GAVE_UP, DONE])

    assert (code, runs) == (1, 2)
    assert meta["ok"] is False
    assert "error_max_structured_output_retries" in meta["error"]


def test_successful_pass_runs_once(run_pass):
    code, runs, meta = run_pass("review", [DONE])

    assert (code, runs) == (0, 1)
    assert meta["ok"] is True


def test_other_errors_are_not_retried(run_pass):
    code, runs, meta = run_pass("understand", [FAILED_OTHERWISE, DONE])

    assert (code, runs) == (1, 1)
    assert "error_max_turns" in meta["error"]


def test_understand_prompt_asks_for_one_complete_submission():
    prompt = (QA / "code-review" / "prompts" / "understand.md").read_text(encoding="utf-8")
    schema = json.loads(
        (QA / "code-review" / "schemas" / "understand.schema.json").read_text(encoding="utf-8")
    )

    assert "in one call" in prompt
    for key in schema["required"]:  # every required key is named, so the model knows to send it
        assert f"`{key}`" in prompt
