from __future__ import annotations

import hashlib
import json
from typing import Any


EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "lesson": {"type": "string"},
                    "prevention_rule": {"type": "string"},
                    "mistake_pattern": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "confidence": {"type": "number"},
                    "learning_type": {
                        "type": "string",
                        "enum": [
                            "bug_prevention",
                            "test_gap",
                            "review_feedback",
                            "ci_failure",
                            "architecture",
                            "security",
                            "performance",
                            "maintainability",
                            "process",
                        ],
                    },
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "evidence_chunk_ids": {"type": "array", "items": {"type": "string"}},
                    "evidence_quotes": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "title",
                    "lesson",
                    "prevention_rule",
                    "mistake_pattern",
                    "tags",
                    "severity",
                    "confidence",
                    "learning_type",
                    "evidence_ids",
                    "evidence_chunk_ids",
                    "evidence_quotes",
                ],
            },
        },
    },
    "required": ["candidates"],
}


def schema_hash(schema: dict[str, Any] = EXTRACTION_SCHEMA) -> str:
    payload = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
