#!/usr/bin/env python3
"""Stage 3: run the evidence checks with the project's own test command. Standard library only.

1. Baseline: the tests must pass on the PR head, or nothing else here means anything.
2. Changed-line coverage: one run under Python's built-in tracer (coverage/sitecustomize.py) that
   records which changed lines the tests execute. Python projects only.
3. Change reverted: put the base-branch version of each modified source file back and run the
   PR's tests. If they still pass, no test pins the new behaviour.
4. Agent probes: apply each probe the agent designed (one small edit at a time), run the tests,
   restore the file. A probe the tests don't catch "survived".
5. Systematic mutants: the same, for operator and constant swaps on every changed Python line
   (mutants.py), so the evidence doesn't depend only on which probes the agent chose.

The agent never edits code. This script applies and reverts every change itself, so the agent
can't misreport a result. Writes .qa-out/probes.json and logs under .qa-out/runs/.
"""

from __future__ import annotations

import atexit
import glob
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

import mutants
from common import (
    QA_HOME,
    QA_OUT,
    REPO,
    SECRET_ENV_VARS,
    as_int,
    classify,
    git,
    git_bytes,
    load_config,
    read_json,
    repo_path,
    write_json,
)

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Words a probe may add without counting as a new identifier.
ALLOWED_NEW_WORDS = {
    "True",
    "False",
    "None",
    "not",
    "and",
    "or",
    "is",
    "in",
    "if",
    "else",
    "return",
    "pass",
    "null",
    "true",
    "false",
    "nil",
    "undefined",
    "break",
    "continue",
}
SAFE_TEST_PATH = re.compile(r"[\w./-]+")
RUNS = QA_OUT / "runs"
_mutated: dict[str, bytes] = {}  # file -> original bytes, while a change is applied
_running: list[int] = []  # process group of the test run in progress


# ---------------------------------------------------------------- file handling


def write_source(rel: str, data: bytes) -> None:
    path = REPO / rel
    path.write_bytes(data)
    if rel.endswith(".py"):  # stale bytecode could hide a same-size edit
        for pyc in (path.parent / "__pycache__").glob(f"{path.stem}.*.pyc"):
            try:
                pyc.unlink()
            except OSError:
                pass


def apply_change(rel: str, new: bytes, original: bytes) -> None:
    if rel in _mutated:  # never stack edits on top of each other
        restore(rel)
    _mutated[rel] = original
    write_source(rel, new)


def restore(rel: str) -> None:
    if rel in _mutated:
        write_source(rel, _mutated.pop(rel))


def restore_all(*_args) -> None:
    for rel in list(_mutated):
        restore(rel)


def _kill_running() -> None:
    for pgid in list(_running):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    _running.clear()


def _on_signal(signum, _frame):
    _kill_running()
    restore_all()
    sys.exit(128 + signum)


atexit.register(restore_all)
signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


# ---------------------------------------------------------------- running tests


def run_tests(command: str, cfg: dict, label: str, extra_env: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_VARS}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra_env or {})
    RUNS.mkdir(parents=True, exist_ok=True)
    log_path = RUNS / f"{label}.log"
    timeout = int(cfg["tests"].get("timeout_seconds", 600))
    started = time.monotonic()
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=REPO,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        _running.append(proc.pid)
        timed_out = False
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out, code = True, None
        _kill_running()  # also stops anything the tests left running
        if timed_out:
            proc.wait()
    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    if timed_out:
        outcome = "timeout"
    elif code == 0:
        outcome = "passed"
    elif code in (cfg["tests"].get("inconclusive_exit_codes") or []):
        outcome = "inconclusive"
    else:
        outcome = "failed"
    return {
        "command": command,
        "outcome": outcome,
        "exit_code": code,
        "seconds": round(time.monotonic() - started, 1),
        "log": log_path.relative_to(REPO).as_posix() if REPO in log_path.parents else str(log_path),
        "log_tail": "\n".join(tail),
    }


def valid_test_paths(paths, cfg) -> list[str]:
    out = []
    for p in paths or []:
        rel = repo_path(str(p).split("::")[0])
        if (
            rel
            and SAFE_TEST_PATH.fullmatch(rel)
            and (REPO / rel).is_file()
            and classify(rel, cfg) == "test"
        ):
            if rel not in out:
                out.append(rel)
    return out


# ---------------------------------------------------------------- changed-line coverage


def changed_line_coverage(cfg: dict, sources: list[dict], full_cmd: str) -> dict:
    """Run the tests once under the built-in tracer; compare executed lines with changed lines."""
    targets = {
        str((REPO / f["path"]).resolve()): f
        for f in sources
        if f["status"] != "D" and f["path"].endswith(".py") and f.get("changed_lines")
    }
    if not cfg.get("coverage", {}).get("enabled", True):
        return {"status": "disabled"}
    if not targets:
        return {"status": "not_applicable", "reason": "no changed Python source lines"}
    out_dir = RUNS / "coverage"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("cov-*.json"):
        old.unlink()
    shim = str(QA_HOME / "coverage")
    pythonpath = os.pathsep.join(p for p in (shim, os.environ.get("PYTHONPATH", "")) if p)
    run = run_tests(
        full_cmd,
        cfg,
        "coverage",
        {
            "PYTHONPATH": pythonpath,
            "QA_COV_OUT": str(out_dir),
            "QA_COV_TARGETS": json.dumps(sorted(targets)),
        },
    )
    executed: dict[str, set] = {t: set() for t in targets}
    executable: dict[str, set] = {t: set() for t in targets}
    files_read = 0
    for path in glob.glob(str(out_dir / "cov-*.json")):
        data = read_json(path, {}) or {}
        files_read += 1
        for t in targets:
            executed[t].update(data.get("executed", {}).get(t, []))
            executable[t].update(data.get("executable", {}).get(t, []))
    if not files_read:
        return {
            "status": "no_data",
            "run": {k: run[k] for k in ("outcome", "exit_code", "seconds", "log")},
            "reason": "the test command didn't start a Python process that reported coverage",
        }
    pragma = cfg.get("coverage", {}).get("exclude_pragma", "pragma: no cover")
    per_file, total, covered = {}, 0, 0
    for t, f in targets.items():
        lines = (REPO / f["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
        changed = {n for a, b in f["changed_lines"] for n in range(a, b + 1)}
        relevant = sorted(
            n for n in changed & executable[t] if not (pragma and pragma in lines[n - 1])
        )
        hit = [n for n in relevant if n in executed[t]]
        missed = [n for n in relevant if n not in executed[t]]
        per_file[f["path"]] = {
            "changed_executable": len(relevant),
            "covered": len(hit),
            "missed": missed,
        }
        total += len(relevant)
        covered += len(hit)
    return {
        "status": "ok" if run["outcome"] == "passed" else "partial",
        "run": {k: run[k] for k in ("outcome", "exit_code", "seconds", "log")},
        "changed_executable": total,
        "covered": covered,
        "diff_coverage": round(covered / total, 4) if total else None,
        "files": per_file,
    }


# ---------------------------------------------------------------- probe preparation


def prepare_probe(probe: dict, cfg: dict, changed: dict) -> tuple[dict | None, str]:
    """Check an agent probe and build the edited file. Returns (edit, '') or (None, reason)."""
    rel = repo_path(probe.get("file"))
    if rel is None:
        return None, "path is empty or outside the repository"
    if classify(rel, cfg) != "source":
        return None, "not a source file (tests, config and excluded paths are never probed)"
    if rel not in changed:
        return None, "file was not changed in this PR"
    path = REPO / rel
    if not path.is_file():
        return None, "file not found"
    original, replacement = probe.get("original"), probe.get("replacement")
    if not isinstance(original, str) or not isinstance(replacement, str) or not original:
        return None, "original and replacement text are required"
    if "\n" in original or "\n" in replacement or "\r" in original or "\r" in replacement:
        return None, "only single-line edits are allowed"
    if original == replacement:
        return None, "replacement is identical to the original"
    max_chars = int(cfg["probes"].get("max_replacement_chars", 160))
    if len(original) > max_chars or len(replacement) > max_chars:
        return None, f"edit is longer than {max_chars} characters"
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "file is not UTF-8 text"
    lines = text.splitlines(keepends=True)
    want = as_int(probe.get("line"))
    if want is None:
        return None, "line number is missing"
    tolerance = int(cfg["probes"].get("line_tolerance", 3))
    window = range(max(1, want - tolerance), min(len(lines), want + tolerance) + 1)
    hits = [n for n in window if lines[n - 1].count(original) == 1]
    if not hits:
        return None, f"original text not found exactly once on or near line {want}"
    hits.sort(key=lambda n: (abs(n - want), n))
    if len(hits) > 1 and abs(hits[0] - want) == abs(hits[1] - want):
        return None, f"original text is ambiguous around line {want}"
    line_no = hits[0]
    src = lines[line_no - 1]
    new_words = (
        set(IDENTIFIER.findall(replacement)) - set(IDENTIFIER.findall(src)) - ALLOWED_NEW_WORDS
    )
    if new_words:
        return None, f"replacement introduces new identifiers: {', '.join(sorted(new_words))}"
    lines[line_no - 1] = src.replace(original, replacement, 1)
    return finish_edit(rel, line_no, raw, "".join(lines), changed)


def finish_edit(
    rel: str, line_no: int, raw: bytes, new_text: str, changed: dict
) -> tuple[dict | None, str]:
    if rel.endswith(".py"):
        try:
            compile(new_text, rel, "exec")
        except (SyntaxError, ValueError) as exc:
            return None, f"edited file is not valid Python ({exc.__class__.__name__})"
    on_changed = any(a <= line_no <= b for a, b in changed.get(rel, []))
    return {
        "rel": rel,
        "line": line_no,
        "original_bytes": raw,
        "new_bytes": new_text.encode("utf-8"),
        "on_changed_line": on_changed,
    }, ""


# ---------------------------------------------------------------- probe execution


class ProbeRunner:
    def __init__(self, cfg: dict, full_cmd: str):
        self.cfg, self.full_cmd = cfg, full_cmd
        self.targeted_tpl = cfg["tests"].get("targeted_command") or ""
        self.targeted_ok: dict[tuple, bool] = {}
        self.started = time.monotonic()
        self.budget = float(cfg["probes"].get("time_budget_seconds", 1500))

    def out_of_time(self) -> bool:
        return time.monotonic() - self.started > self.budget

    def run(self, record: dict, edit: dict, suggested_tests) -> None:
        label = re.sub(r"[^\w.-]", "_", f"probe_{record['id']}")
        tests = valid_test_paths(suggested_tests, self.cfg) if self.targeted_tpl else []
        targeted_cmd = (
            self.targeted_tpl.replace("{tests}", " ".join(shlex.quote(t) for t in tests))
            if tests
            else ""
        )
        if (
            targeted_cmd and tuple(tests) not in self.targeted_ok
        ):  # must pass on unedited code first
            base = run_tests(targeted_cmd, self.cfg, f"{label}_targeted_baseline")
            self.targeted_ok[tuple(tests)] = base["outcome"] == "passed"
        apply_change(edit["rel"], edit["new_bytes"], edit["original_bytes"])
        try:
            run, detected_by = None, ""
            if targeted_cmd and self.targeted_ok.get(tuple(tests)):
                run = run_tests(targeted_cmd, self.cfg, f"{label}_targeted")
                detected_by = "targeted tests"
            if run is None or run["outcome"] not in ("failed", "timeout"):
                run = run_tests(self.full_cmd, self.cfg, label)
                detected_by = "full suite"
        finally:
            restore(edit["rel"])
        status = {"failed": "killed", "timeout": "timeout", "passed": "survived"}.get(
            run["outcome"], "inconclusive"
        )
        record.update(
            file=edit["rel"],
            line=edit["line"],
            on_changed_line=edit["on_changed_line"],
            status=status,
            detected_by=detected_by if status in ("killed", "timeout") else "",
            run={k: run[k] for k in ("outcome", "exit_code", "seconds", "log")},
            log_tail=run["log_tail"],
        )
        print(
            f"{'Probe' if record['origin'] == 'agent' else 'Mutant'} {record['id']} "
            f"{edit['rel']}:{edit['line']}: {status}"
        )


# ---------------------------------------------------------------- main


def main() -> None:
    cfg = load_config()
    ctx = read_json(QA_OUT / "context.json")
    if not ctx:
        raise SystemExit("context.json is missing: run collect_context.py first")
    plan = read_json(QA_OUT / "plan.json")
    full_cmd = cfg["tests"]["command"]
    result: dict = {
        "status": "ok",
        "test_command": full_cmd,
        "baseline": None,
        "coverage": {"status": "not_run"},
        "fail_on_base": {"status": "not_applicable", "files": []},
        "probes": [],
        "notes": [],
    }

    before = git("status", "--porcelain", "--untracked-files=no")
    if before.strip():
        result["notes"].append(
            "Tracked files were already modified before the probes ran: " + before.strip()[:500]
        )

    # 1. Baseline
    base_run = run_tests(full_cmd, cfg, "baseline")
    result["baseline"] = base_run
    print(f"Baseline: {base_run['outcome']} in {base_run['seconds']}s")
    if base_run["outcome"] != "passed":
        result["status"] = "baseline_failed"
        result["notes"].append(
            "The tests don't pass on the PR head, so the other runs were skipped."
        )
        finish(result, cfg, before)
        return

    sources = [f for f in ctx["changed_files"] if f["category"] == "source"]

    # 2. Changed-line coverage
    result["coverage"] = changed_line_coverage(cfg, sources, full_cmd)
    cov = result["coverage"]
    if cov.get("diff_coverage") is not None:
        print(
            f"Changed-line coverage: {cov['covered']} of {cov['changed_executable']} "
            "changed lines ran"
        )
    else:
        print(f"Changed-line coverage: {cov['status']}")

    # 3. Change reverted
    modified = [f["path"] for f in sources if f["status"] == "M"]
    if modified:
        try:
            for rel in modified:
                apply_change(
                    rel, git_bytes("show", f"{ctx['merge_base']}:{rel}"), (REPO / rel).read_bytes()
                )
            run = run_tests(full_cmd, cfg, "change_reverted")
            status = {"failed": "failed", "passed": "passed"}.get(run["outcome"], "inconclusive")
            result["fail_on_base"] = {"status": status, "files": modified, "run": run}
            print(f"Change reverted: tests {run['outcome']}")
        except (OSError, RuntimeError) as exc:
            result["fail_on_base"] = {
                "status": "inconclusive",
                "files": modified,
                "error": str(exc),
            }
        finally:
            restore_all()

    changed = {f["path"]: f.get("changed_lines", []) for f in sources if f["status"] != "D"}
    runner = ProbeRunner(cfg, full_cmd)

    # 4. Agent probes
    probes = (plan or {}).get("probes") or []
    if plan is None:
        off = (read_json(QA_OUT / "plan.meta.json") or {}).get("disabled")
        result["notes"].append(
            "The agent is off, so only systematic mutants were run."
            if off
            else "No plan from the agent, so no agent probes were run."
        )
    max_probes = int(cfg["probes"].get("max_probes", 10))
    agent_edits = set()
    suggested_by_file: dict[str, list] = {}
    for index, probe in enumerate(probes):
        if not isinstance(probe, dict):
            continue
        record = {
            k: probe.get(k)
            for k in (
                "id",
                "unit_id",
                "file",
                "line",
                "original",
                "replacement",
                "category",
                "rationale",
                "related_criteria",
            )
        }
        record["id"] = str(record.get("id") or f"P{index + 1}")
        record["origin"] = "agent"
        result["probes"].append(record)
        if index >= max_probes:
            record.update(status="skipped", reason=f"over the limit of {max_probes} probes")
            continue
        if runner.out_of_time():
            record.update(status="skipped", reason="time budget used up")
            continue
        edit, reason = prepare_probe(probe, cfg, changed)
        if edit is None:
            record.update(status="invalid", reason=reason)
            print(f"Probe {record['id']}: invalid ({reason})")
            continue
        agent_edits.add(
            (
                edit["rel"],
                edit["line"],
                edit["new_bytes"].decode("utf-8").splitlines()[edit["line"] - 1],
            )
        )
        suggested_by_file.setdefault(edit["rel"], []).extend(probe.get("suggested_tests") or [])
        runner.run(record, edit, probe.get("suggested_tests"))

    # 5. Systematic mutants
    limit = (
        int(cfg["probes"].get("systematic_max", 15)) if cfg["probes"].get("systematic", True) else 0
    )
    count = 0
    for rel, ranges in changed.items():
        if count >= limit or not rel.endswith(".py") or not ranges:
            continue
        raw = (REPO / rel).read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        skip = {(line, text_) for f, line, text_ in agent_edits if f == rel}
        for m in mutants.select(text, ranges, limit - count, skip):
            count += 1
            record = {
                "id": f"S{count}",
                "origin": "systematic",
                "unit_id": "",
                "file": rel,
                "line": m["line"],
                "original": m["original"],
                "replacement": m["replacement"],
                "category": m["category"],
                "rationale": (
                    f"Systematic mutant: `{m['original']}` becomes "
                    f"`{m['replacement'] or '(removed)'}`."
                ),
                "related_criteria": [],
            }
            result["probes"].append(record)
            if runner.out_of_time():
                record.update(status="skipped", reason="time budget used up")
                continue
            edit, reason = finish_edit(rel, m["line"], raw, mutants.apply(text, m), changed)
            if edit is None:
                record.update(status="invalid", reason=reason)
                continue
            runner.run(record, edit, suggested_by_file.get(rel))

    finish(result, cfg, before)


def finish(result: dict, cfg: dict, before: str) -> None:
    restore_all()
    after = git("status", "--porcelain", "--untracked-files=no")
    result["workspace_restored"] = after == before
    if after != before:
        result["notes"].append(
            "Tracked files differ from their state before the probes: " + after.strip()[:500]
        )
    statuses = ("killed", "timeout", "survived", "invalid", "inconclusive", "skipped")

    def summarise(rows):
        counts = {s: sum(1 for p in rows if p.get("status") == s) for s in statuses}
        detected = counts["killed"] + counts["timeout"]
        valid = detected + counts["survived"]
        return {**counts, "valid": valid, "detected": detected}

    minimum = int(cfg["probes"].get("min_valid_for_score", 3))
    total = summarise(result["probes"])
    total["mutation_score"] = (
        round(total["detected"] / total["valid"], 4) if total["valid"] >= minimum else None
    )
    total["agent"] = summarise([p for p in result["probes"] if p.get("origin") == "agent"])
    total["systematic"] = summarise(
        [p for p in result["probes"] if p.get("origin") == "systematic"]
    )
    result["summary"] = total
    write_json(QA_OUT / "probes.json", result)
    s = result["summary"]
    print(
        f"Probes and mutants: {s['killed']} killed, {s['timeout']} timed out, "
        f"{s['survived']} survived, {s['invalid']} invalid, {s['skipped']} skipped; mutation score "
        f"{'n/a' if s['mutation_score'] is None else format(s['mutation_score'], '.0%')}"
    )


if __name__ == "__main__":
    main()
