"""One module per checklist row. Each exposes `run(ctx, idx) -> list[finding]`.

Rules report only on lines the PR adds (or, with `pr_caused`, on existing lines the PR breaks),
so pre-existing code never blocks a PR. Silence a deliberate case on the line, or the line above,
with `review: ignore[RULE] <reason>`; a suppression without a reason isn't honoured.
"""

from . import (
    antipatterns,
    assertions,
    dependencies,
    duplication,
    errors,
    injection,
    logic,
    requirements,
    scope,
    tests_vs_requirements,
)

ALL = [
    dependencies,
    logic,
    requirements,
    errors,
    scope,
    duplication,
    antipatterns,
    injection,
    assertions,
    tests_vs_requirements,
]
