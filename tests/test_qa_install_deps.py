"""How the QA gates install a project's dependencies (.qa/agent/install_deps.py).

The gates used to run `pip install -r requirements.txt`, which only works for Python projects that
keep that one file. The installer now works out the commands from what the repo declares, or takes
the project's own command, and never needs a shell.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

QA_AGENT = Path(__file__).resolve().parents[1] / ".qa" / "agent"
sys.path.insert(0, str(QA_AGENT))
_spec = importlib.util.spec_from_file_location("qa_install_deps", QA_AGENT / "install_deps.py")
install_deps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_deps)

PIP = [sys.executable, "-m", "pip", "install"]


def cfg(command="python -m pytest -q", install=""):
    return {"tests": {"command": command, "install_command": install}}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(install_deps, "REPO", tmp_path)
    monkeypatch.delenv("QA_INSTALL_COMMAND", raising=False)
    return tmp_path


def write(repo, name, text=""):
    (repo / name).write_text(text, encoding="utf-8")


def test_every_root_requirements_file_is_installed_in_name_order(repo):
    write(repo, "requirements.txt", "flask\n")
    write(repo, "requirements-dev.txt", "pytest\n")
    (repo / "docs").mkdir()
    write(repo, "docs/requirements.txt", "sphinx\n")  # not the project's: not at the root
    assert install_deps.plan(cfg()) == [
        PIP + ["-r", "requirements-dev.txt"],
        PIP + ["-r", "requirements.txt"],
    ]


def test_a_project_without_requirements_txt_installs_from_pyproject_with_its_test_extra(repo):
    write(
        repo,
        "pyproject.toml",
        '[project]\nname = "shop"\nversion = "1"\n'
        '[project.optional-dependencies]\ndocs = ["sphinx"]\ntests = ["pytest"]\ndev = ["ruff"]\n',
    )
    assert install_deps.plan(cfg()) == [PIP + ["-e", ".[tests]"]]


def test_setup_py_installs_the_package_without_extras(repo):
    write(repo, "setup.py", "from setuptools import setup\nsetup()\n")
    assert install_deps.plan(cfg()) == [PIP + ["-e", "."]]


def test_tool_only_or_broken_pyproject_installs_nothing(repo):
    write(repo, "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    assert install_deps.plan(cfg()) == []
    write(repo, "pyproject.toml", "[project\nname=")
    assert install_deps.plan(cfg()) == []


@pytest.mark.parametrize(("lockfile", "verb"), [(True, "ci"), (False, "install")])
def test_node_projects_use_npm(repo, lockfile, verb):
    write(repo, "package.json", "{}")
    if lockfile:
        write(repo, "package-lock.json", "{}")
    assert install_deps.plan(cfg("npx jest --bail")) == [["npm", verb]]


def test_the_projects_own_command_wins_and_the_variable_beats_the_config(repo, monkeypatch):
    write(repo, "requirements.txt", "flask\n")
    assert install_deps.plan(cfg(install="poetry install --with 'test tools'")) == [
        ["poetry", "install", "--with", "test tools"]
    ]
    monkeypatch.setenv("QA_INSTALL_COMMAND", "uv sync --frozen")
    assert install_deps.plan(cfg(install="poetry install")) == [["uv", "sync", "--frozen"]]


@pytest.mark.parametrize(
    ("command", "on_path", "needed"),
    [
        ("python -m pytest -q -x", None, True),
        ("pytest --maxfail=1", None, True),
        ("pytest --maxfail=1", "/usr/bin/pytest", False),
        ("npx jest --bail", None, False),
        ("mvn -q test -Dtest=pytest", None, False),
    ],
)
def test_pytest_is_added_only_when_the_test_command_needs_it(monkeypatch, command, on_path, needed):
    monkeypatch.setattr(install_deps.shutil, "which", lambda _: on_path)
    assert install_deps.needs_pytest(cfg(command)) is needed


def _record(monkeypatch, codes):
    ran = []

    def fake_run(step, **kw):
        ran.append(step)
        return subprocess.CompletedProcess(step, codes.pop(0) if codes else 0)

    monkeypatch.setattr(install_deps.subprocess, "run", fake_run)
    return ran


def test_a_failed_install_stops_and_fails_the_step(repo, monkeypatch, capsys):
    write(repo, "requirements.txt", "flask\n")
    write(repo, "package.json", "{}")
    monkeypatch.setattr(install_deps, "load_config", cfg)
    ran = _record(monkeypatch, [3])
    assert install_deps.main() == 3
    assert ran == [PIP + ["-r", "requirements.txt"]]  # npm never ran
    assert "failed with exit code 3" in capsys.readouterr().out


def test_an_empty_repo_still_gets_pytest_and_says_nothing_was_declared(repo, monkeypatch, capsys):
    monkeypatch.setattr(install_deps, "load_config", cfg)
    monkeypatch.setattr(install_deps.shutil, "which", lambda _: None)
    ran = _record(monkeypatch, [])
    assert install_deps.main() == 0
    assert ran == [PIP + ["pytest"]]
    assert "No dependency files found" in capsys.readouterr().out


def test_the_gate_workflows_no_longer_need_requirements_txt():
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for path in workflows.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "requirements.txt" not in text, path.name
        assert 'cache: "pip"' not in text, path.name  # setup-python's pip cache needs that file
    for name in ("ci.yml", "ci-dev.yml", "ci-uat.yml", "pr-tests.yml", "test-quality-agent.yml"):
        assert "install_deps.py" in (workflows / name).read_text(encoding="utf-8"), name
