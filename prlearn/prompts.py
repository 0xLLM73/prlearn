from __future__ import annotations


PROMPT_VERSION = "model-extract-v6"


SYSTEM_PROMPT = """You extract durable personal coding lessons from redacted GitHub PR evidence.
Only use the provided evidence IDs and chunk IDs. Do not invent facts, secrets, files, or evidence.
Return raw JSON only, with no markdown fences and no prose.
If the evidence is self-status, bot noise, or generic CI chatter, return {"candidates":[]}.

The top-level JSON object must be exactly:
{"candidates":[...]}

Each candidate object must use these exact quoted JSON keys:
title, lesson, prevention_rule, mistake_pattern, tags, severity, confidence, learning_type, evidence_ids, evidence_chunk_ids, evidence_quotes.

Do not use these keys: trigger_conditions, prevention_action.
Every evidence_id and evidence_chunk_id must be copied exactly from the input.
Use numeric confidence values like 0.8, not words like high.
Set evidence_quotes to [] to keep the JSON short."""


SYSTEM_PROMPT_UNREDACTED = SYSTEM_PROMPT.replace("redacted GitHub PR evidence", "GitHub PR evidence")


def extraction_user_prompt(payload_json: str, *, redacted: bool = True) -> str:
    evidence_label = "redacted evidence chunks" if redacted else "evidence chunks"
    return (
        f"Review these {evidence_label} and propose at most 1 specific, reusable lesson that is directly supported.\n"
        "Each candidate needs concrete trigger conditions and a concrete prevention action.\n"
        "Use the exact candidate keys required by the system prompt.\n"
        "Set severity to one of: low, medium, high, critical.\n"
        "Set learning_type to one of: bug_prevention, test_gap, review_feedback, ci_failure, architecture, security, performance, maintainability, process.\n"
        "Copy evidence_ids from evidence_chunks[].evidence_id and evidence_chunk_ids from evidence_chunks[].chunk_id.\n"
        "Use at most 4 evidence_ids and at most 4 evidence_chunk_ids per candidate.\n"
        "Keep each field concise; do not include long diffs or logs inside lesson text.\n"
        "Set evidence_quotes to [] and do not include quote text.\n"
        "Do not output generic lessons like just add tests.\n\n"
        f"{payload_json}"
    )
