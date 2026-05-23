from __future__ import annotations

import sqlite3
from typing import Any

from .crypto import decrypt_text, encrypt_text, is_encrypted_text, passphrase_from_env


RAW_JSON_COLUMNS = [
    ("prs", "id", "raw_json"),
    ("events", "id", "raw_json"),
    ("pr_files", "id", "raw_json"),
    ("check_runs", "id", "raw_json"),
]


def raw_json_status(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = []
    totals = {"encrypted": 0, "plain": 0, "empty": 0}
    for table, id_column, value_column in RAW_JSON_COLUMNS:
        encrypted = plain = empty = 0
        rows = conn.execute(f"select {id_column} as id, {value_column} as value from {table}").fetchall()
        for row in rows:
            value = row["value"]
            if not value:
                empty += 1
            elif is_encrypted_text(str(value)):
                encrypted += 1
            else:
                plain += 1
        tables.append({"table": table, "column": value_column, "encrypted": encrypted, "plain": plain, "empty": empty})
        totals["encrypted"] += encrypted
        totals["plain"] += plain
        totals["empty"] += empty
    return {"tables": tables, "totals": totals}


def encrypt_raw_json(conn: sqlite3.Connection, config: dict[str, Any], *, prompt: bool = False) -> dict[str, int]:
    passphrase = passphrase_from_env(config, prompt=prompt)
    encrypted = skipped = empty = 0
    for table, id_column, value_column in RAW_JSON_COLUMNS:
        rows = conn.execute(f"select {id_column} as id, {value_column} as value from {table}").fetchall()
        for row in rows:
            value = row["value"]
            if not value:
                empty += 1
                continue
            if is_encrypted_text(str(value)):
                skipped += 1
                continue
            encrypted_value = encrypt_text(str(value), passphrase)
            conn.execute(f"update {table} set {value_column}=? where {id_column}=?", (encrypted_value, row["id"]))
            encrypted += 1
    conn.commit()
    return {"encrypted": encrypted, "skipped": skipped, "empty": empty}


def decrypt_raw_json(conn: sqlite3.Connection, config: dict[str, Any], *, prompt: bool = False) -> dict[str, int]:
    passphrase = passphrase_from_env(config, prompt=prompt)
    decrypted = skipped = empty = 0
    for table, id_column, value_column in RAW_JSON_COLUMNS:
        rows = conn.execute(f"select {id_column} as id, {value_column} as value from {table}").fetchall()
        for row in rows:
            value = row["value"]
            if not value:
                empty += 1
                continue
            if not is_encrypted_text(str(value)):
                skipped += 1
                continue
            decrypted_value = decrypt_text(str(value), passphrase)
            conn.execute(f"update {table} set {value_column}=? where {id_column}=?", (decrypted_value, row["id"]))
            decrypted += 1
    conn.commit()
    return {"decrypted": decrypted, "skipped": skipped, "empty": empty}

