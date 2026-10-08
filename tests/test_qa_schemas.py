"""The QA agents pass their output schemas to `claude --json-schema`. The CLI rejects a schema that
declares "$schema": draft 2020-12 ("not a valid JSON Schema"), and the agent pass then fails with no
result, which the gates treat as "agent unavailable" without failing the job.
"""

import json
from pathlib import Path

import pytest

QA = Path(__file__).resolve().parents[1] / ".qa"
SCHEMAS = sorted(QA.glob("*/schemas/*.schema.json"))


def test_both_agents_ship_their_schemas():
    names = {(p.parent.parent.name, p.name) for p in SCHEMAS}

    assert ("agent", "plan.schema.json") in names
    assert ("agent", "review.schema.json") in names
    assert ("code-review", "understand.schema.json") in names
    assert ("code-review", "oracle.schema.json") in names
    assert ("code-review", "review.schema.json") in names


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: f"{p.parent.parent.name}/{p.name}")
def test_schema_is_accepted_by_the_claude_cli(path):
    schema = json.loads(path.read_text(encoding="utf-8"))

    assert schema["type"] == "object"
    assert schema["required"], "the agent output must have required fields"
    assert "$schema" not in schema, "claude --json-schema rejects a draft 2020-12 $schema"
