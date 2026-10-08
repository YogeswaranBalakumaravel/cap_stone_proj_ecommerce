"""The QA agents pass their output schemas to `claude --json-schema`. The CLI rejects a schema that
declares "$schema": draft 2020-12 ("not a valid JSON Schema"), and the agent pass then fails with no
result, which the gates treat as "agent unavailable" without failing the job.
"""

import json
from pathlib import Path

import pytest

QA = Path(__file__).resolve().parents[1] / ".qa"
SCHEMAS = sorted(QA.glob("*/schemas/*.schema.json"))
# Keywords that only exist from draft 2019-09/2020-12 onwards; the CLI's default draft lacks them.
DRAFT_2020_ONLY = {
    "$schema",
    "$defs",
    "$dynamicRef",
    "$dynamicAnchor",
    "prefixItems",
    "unevaluatedProperties",
    "unevaluatedItems",
    "dependentRequired",
    "dependentSchemas",
}


def keywords(node):
    """Every key used anywhere in the schema, except the names declared under `properties`."""
    if isinstance(node, list):
        for item in node:
            yield from keywords(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            if key == "properties" and isinstance(value, dict):
                for sub in value.values():
                    yield from keywords(sub)
            else:
                yield from keywords(value)


def test_both_agents_ship_their_schemas():
    names = {(p.parent.parent.name, p.name) for p in SCHEMAS}

    assert {
        ("agent", "plan.schema.json"),
        ("agent", "review.schema.json"),
        ("code-review", "understand.schema.json"),
        ("code-review", "oracle.schema.json"),
        ("code-review", "review.schema.json"),
    } <= names


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: f"{p.parent.parent.name}/{p.name}")
def test_schema_uses_no_draft_2020_only_keywords(path):
    schema = json.loads(path.read_text(encoding="utf-8"))

    assert schema["type"] == "object"
    assert schema["required"], "the agent output must have required fields"
    assert not DRAFT_2020_ONLY & set(keywords(schema))
    assert all(ref.startswith("#") for ref in refs(schema)), "the CLI can't fetch remote $refs"


def refs(node):
    """Every $ref value in the schema."""
    if isinstance(node, list):
        for item in node:
            yield from refs(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from refs(value)


def test_ref_scan_finds_remote_refs():
    schema = {"type": "object", "properties": {"a": {"$ref": "#/x"}, "b": [{"$ref": "http://y"}]}}

    assert list(refs(schema)) == ["#/x", "http://y"]


def test_keyword_scan_finds_nested_draft_2020_keywords():
    schema = {
        "type": "object",
        "properties": {
            "prefixItems": {"type": "string"},  # a property *named* like a keyword is fine
            "items": {"type": "array", "items": {"$defs": {}}},
        },
    }

    found = DRAFT_2020_ONLY & set(keywords(schema))

    assert found == {"$defs"}
