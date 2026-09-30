"""Changed-line coverage for the test-quality agent. Python standard library only.

run_probes.py puts this folder first on PYTHONPATH for one test run, so every Python process
the test command starts imports it at startup. It records which lines of the PR's changed source
files execute, and writes them to QA_COV_OUT when the process exits. It does nothing unless
QA_COV_OUT and QA_COV_TARGETS are set, and it never raises.

Python 3.12+ uses sys.monitoring: each code location reports once and is then switched off,
so the overhead is small. Older versions fall back to sys.settrace, limited to the target files.
"""

import atexit
import dis
import json
import os
import sys


def _start():
    out_dir = os.environ.get("QA_COV_OUT")
    targets = set(json.loads(os.environ.get("QA_COV_TARGETS") or "[]"))
    if not out_dir or not targets:
        return
    hits = {t: set() for t in targets}
    real = {}

    def resolve(filename):
        path = real.get(filename)
        if path is None:
            path = real[filename] = (
                "" if not filename or filename.startswith("<") else os.path.realpath(filename)
            )
        return path

    monitoring = getattr(sys, "monitoring", None)
    if monitoring is not None:
        tool = None
        for candidate in (monitoring.COVERAGE_ID, 3, 4):
            try:
                monitoring.use_tool_id(candidate, "qa-line-coverage")
                tool = candidate
                break
            except ValueError:  # id already in use
                continue
        if tool is None:
            return

        def on_line(code, line):
            path = resolve(code.co_filename)
            if path in hits:
                hits[path].add(line)
            return monitoring.DISABLE  # report each location once

        monitoring.register_callback(tool, monitoring.events.LINE, on_line)
        monitoring.set_events(tool, monitoring.events.LINE)
    else:
        import threading

        def local_trace(frame, event, _arg):
            if event == "line":
                hits[resolve(frame.f_code.co_filename)].add(frame.f_lineno)
            return local_trace

        def global_trace(frame, _event, _arg):
            return local_trace if resolve(frame.f_code.co_filename) in hits else None

        sys.settrace(global_trace)
        threading.settrace(global_trace)

    def executable_lines(path):
        """Lines this interpreter can execute, so executed and executable lines agree."""
        try:
            with open(path, encoding="utf-8") as fh:
                code = compile(fh.read(), path, "exec")
        except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
            return []
        lines, stack = set(), [code]
        while stack:
            obj = stack.pop()
            lines.update(n for _, n in dis.findlinestarts(obj) if n)
            stack.extend(c for c in obj.co_consts if hasattr(c, "co_code"))
        return sorted(lines)

    def dump():
        try:
            data = {
                "pid": os.getpid(),
                "python": sys.version.split()[0],
                "executed": {t: sorted(h) for t, h in hits.items()},
                "executable": {t: executable_lines(t) for t in targets},
            }
            with open(
                os.path.join(out_dir, f"cov-{os.getpid()}.json"), "w", encoding="utf-8"
            ) as fh:
                json.dump(data, fh)
        except Exception:  # noqa: BLE001 - never break the tests
            pass

    atexit.register(dump)


try:
    _start()
except Exception:  # noqa: BLE001 - never break the tests
    pass
