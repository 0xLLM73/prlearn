from __future__ import annotations

import array
import json
import math
import sqlite3
from typing import Iterable

from .util import content_hash, slug_key, utcnow


def vector_to_blob(values: Iterable[float]) -> bytes:
    floats = array.array("f", [float(value) for value in values])
    return floats.tobytes()


def blob_to_vector(blob: bytes) -> list[float]:
    floats = array.array("f")
    floats.frombytes(blob)
    return list(floats)


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    a_norm = math.sqrt(sum(x * x for x in a))
    b_norm = math.sqrt(sum(y * y for y in b))
    if not a_norm or not b_norm:
        return 0.0
    return dot / (a_norm * b_norm)


def store_embedding(
    conn: sqlite3.Connection,
    *,
    owner_type: str,
    owner_id: str,
    model_name: str,
    text: str,
    vector: list[float],
    metadata: dict[str, object] | None = None,
) -> str:
    text_hash = content_hash({"text": text})
    embedding_id = "emb_" + slug_key([owner_type, owner_id, model_name, text_hash])
    conn.execute(
        """
        insert into embeddings(id, owner_type, owner_id, model_name, text_hash, dimensions, vector_blob, metadata_json, created_at)
        values(?,?,?,?,?,?,?,?,?)
        on conflict(id) do nothing
        """,
        (
            embedding_id,
            owner_type,
            owner_id,
            model_name,
            text_hash,
            len(vector),
            vector_to_blob(vector),
            json.dumps(metadata or {}, sort_keys=True),
            utcnow(),
        ),
    )
    return embedding_id
