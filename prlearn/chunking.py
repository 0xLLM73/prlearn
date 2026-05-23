from __future__ import annotations

import re
from typing import Iterable

from .util import slug_key


MAX_CHARS = 1800


def stable_chunk_id(evidence_id: str, index: int, text: str) -> str:
    return "chk_" + slug_key([evidence_id, str(index), text])


def chunks_for_text(text: str, *, kind: str) -> list[str]:
    value = (text or "").strip()
    if not value:
        return []
    if kind == "patch_hunk":
        return patch_hunks(value)
    if len(value) <= MAX_CHARS:
        return [value]
    return split_long_text(value)


def patch_hunks(text: str) -> list[str]:
    parts = re.split(r"(?m)(?=^@@ )", text)
    hunks = [part.strip() for part in parts if part.strip()]
    if not hunks:
        return split_long_text(text)
    result: list[str] = []
    for hunk in hunks:
        result.extend(split_long_text(hunk))
    return result


def split_long_text(text: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs(text):
        if current and size + len(paragraph) + 2 > MAX_CHARS:
            chunks.append("\n\n".join(current).strip())
            current = []
            size = 0
        if len(paragraph) > MAX_CHARS:
            if current:
                chunks.append("\n\n".join(current).strip())
                current = []
                size = 0
            chunks.extend(paragraph[i : i + MAX_CHARS] for i in range(0, len(paragraph), MAX_CHARS))
            continue
        current.append(paragraph)
        size += len(paragraph) + 2
    if current:
        chunks.append("\n\n".join(current).strip())
    return chunks


def paragraphs(text: str) -> Iterable[str]:
    parts = re.split(r"\n\s*\n", text)
    for part in parts:
        stripped = part.strip()
        if stripped:
            yield stripped
