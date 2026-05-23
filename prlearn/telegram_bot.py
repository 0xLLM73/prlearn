from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import urllib.error
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from .db import get_state, set_state
from .learn import accept_candidate, candidate_rows, candidate_to_dict, reject_candidate
from .usefulness import candidate_summary
from .util import utcnow


TOKEN_ENV = "PRLEARN_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "PRLEARN_TELEGRAM_CHAT_ID"
RATINGS = {"major", "minor", "not_important"}


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramSettings:
    token: str
    chat_id: str


class TelegramClient:
    def __init__(self, token: str, *, timeout: int = 30) -> None:
        self.token = token
        self.timeout = timeout

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        request = Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise TelegramError(f"Telegram {method} failed: HTTP {exc.code}: {message}") from exc
        except OSError as exc:
            raise TelegramError(f"Telegram {method} failed: {exc}") from exc
        try:
            result = json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            raise TelegramError(f"Telegram {method} returned invalid JSON") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            description = result.get("description") if isinstance(result, dict) else body
            raise TelegramError(f"Telegram {method} failed: {description}")
        return result

    def send_message(self, *, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def get_updates(
        self,
        *,
        offset: int | None,
        timeout: int,
        limit: int,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "limit": limit,
            "allowed_updates": allowed_updates or ["callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        data = self.call("getUpdates", payload)
        result = data.get("result") or []
        return result if isinstance(result, list) else []

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    def delete_message(self, *, chat_id: str, message_id: int) -> None:
        self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def settings_from_env() -> TelegramSettings:
    token = os.environ.get(TOKEN_ENV)
    chat_id = os.environ.get(CHAT_ID_ENV)
    if not token:
        raise TelegramError(f"missing bot token; set {TOKEN_ENV}")
    if not chat_id:
        raise TelegramError(f"missing chat id; set {CHAT_ID_ENV}")
    return TelegramSettings(token=token, chat_id=chat_id)


def token_from_env() -> str:
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise TelegramError(f"missing bot token; set {TOKEN_ENV}")
    return token


def callback_data_for(rating: str, candidate_id: str, nonce: str) -> str:
    return f"prlearn:{rating}:{candidate_id}:{nonce}"


def rating_keyboard(candidate_id: str, nonce: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Major learning", "callback_data": callback_data_for("major", candidate_id, nonce)},
                {"text": "Minor learning", "callback_data": callback_data_for("minor", candidate_id, nonce)},
            ],
            [{"text": "Not important", "callback_data": callback_data_for("not_important", candidate_id, nonce)}],
        ]
    }


def candidate_message(candidate: dict[str, Any]) -> str:
    tags = ", ".join(candidate.get("tags") or [])
    lines = [
        "Potential prlearn lesson",
        "",
        str(candidate["title"]),
        "",
        f"Lesson: {candidate['lesson']}",
        f"Prevention: {candidate['prevention_rule']}",
        f"Confidence: {candidate['confidence']} | Evidence: {candidate['evidence_count']} | Type: {candidate.get('learning_type') or 'unknown'}",
    ]
    if tags:
        lines.append(f"Tags: {tags}")
    lines.extend(["", f"Candidate ID: {candidate['id']}"])
    return "\n".join(lines)


def pending_candidate_summaries(conn: sqlite3.Connection, *, limit: int | None = None, ready: bool = False) -> list[dict[str, Any]]:
    items = [candidate_summary(candidate_to_dict(conn, row)) for row in candidate_rows(conn, status="pending", limit=limit)]
    if ready:
        items = [item for item in items if item["ready"]]
    return items


def send_pending_candidates(
    conn: sqlite3.Connection,
    client: TelegramClient,
    *,
    chat_id: str,
    limit: int | None = None,
    ready: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    candidates = pending_candidate_summaries(conn, limit=limit, ready=ready)
    sent = 0
    for candidate in candidates:
        nonce = secrets.token_urlsafe(6)
        if not dry_run:
            response = client.send_message(chat_id=chat_id, text=candidate_message(candidate), reply_markup=rating_keyboard(str(candidate["id"]), nonce))
            result = response.get("result") if isinstance(response, dict) else None
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if not isinstance(message_id, int):
                raise TelegramError("Telegram sendMessage did not return a message_id")
            record_candidate_message(conn, candidate_id=str(candidate["id"]), chat_id=chat_id, message_id=message_id, nonce=nonce)
        sent += 1
    return {"candidates": len(candidates), "sent": sent, "dry_run": int(dry_run)}


def discover_chats(client: TelegramClient, *, timeout: int = 0, limit: int = 20) -> list[dict[str, Any]]:
    updates = client.get_updates(offset=None, timeout=timeout, limit=limit, allowed_updates=["message", "callback_query"])
    chats: dict[str, dict[str, Any]] = {}
    for update in updates:
        chat = chat_from_update(update)
        if not chat:
            continue
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            continue
        chats[chat_id] = {
            "id": chat_id,
            "type": chat.get("type"),
            "title": chat.get("title") or chat.get("username") or chat.get("first_name"),
        }
    return list(chats.values())


def chat_from_update(update: dict[str, Any]) -> dict[str, Any] | None:
    message = update.get("message") if isinstance(update.get("message"), dict) else None
    if message and isinstance(message.get("chat"), dict):
        return message["chat"]
    callback = update.get("callback_query") if isinstance(update.get("callback_query"), dict) else None
    callback_message = callback.get("message") if callback and isinstance(callback.get("message"), dict) else None
    if callback_message and isinstance(callback_message.get("chat"), dict):
        return callback_message["chat"]
    return None


def parse_callback_data(value: str | None) -> tuple[str, str, str] | None:
    if not value:
        return None
    parts = value.split(":", 3)
    if len(parts) != 4 or parts[0] != "prlearn":
        return None
    rating, candidate_id, nonce = parts[1], parts[2], parts[3]
    if rating not in RATINGS or not candidate_id or not nonce:
        return None
    return rating, candidate_id, nonce


def record_candidate_message(conn: sqlite3.Connection, *, candidate_id: str, chat_id: str, message_id: int, nonce: str) -> None:
    conn.execute(
        """
        insert into telegram_candidate_messages(candidate_id, chat_id_hash, message_id, callback_nonce, created_at)
        values(?,?,?,?,?)
        on conflict(candidate_id, chat_id_hash, message_id) do update set
          callback_nonce=excluded.callback_nonce, created_at=excluded.created_at, reviewed_at=null
        """,
        (candidate_id, hash_chat_id(chat_id), message_id, nonce, utcnow()),
    )
    conn.commit()


def callback_is_registered(conn: sqlite3.Connection, *, candidate_id: str, chat_id: str, message_id: int, nonce: str) -> bool:
    row = conn.execute(
        """
        select id from telegram_candidate_messages
        where candidate_id=? and chat_id_hash=? and message_id=? and callback_nonce=? and reviewed_at is null
        """,
        (candidate_id, hash_chat_id(chat_id), message_id, nonce),
    ).fetchone()
    return row is not None


def mark_candidate_message_reviewed(conn: sqlite3.Connection, *, candidate_id: str, chat_id: str, message_id: int, nonce: str) -> None:
    conn.execute(
        """
        update telegram_candidate_messages
        set reviewed_at=?
        where candidate_id=? and chat_id_hash=? and message_id=? and callback_nonce=?
        """,
        (utcnow(), candidate_id, hash_chat_id(chat_id), message_id, nonce),
    )
    conn.commit()


def answer_callback_query_safely(client: TelegramClient, callback_query_id: object, text: str) -> bool:
    if not callback_query_id:
        return True
    try:
        client.answer_callback_query(str(callback_query_id), text)
        return True
    except TelegramError:
        return False


def apply_candidate_rating(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    rating: str,
    telegram_user_id: str | None = None,
    telegram_chat_id: str | None = None,
) -> dict[str, Any]:
    if rating not in RATINGS:
        raise TelegramError(f"unsupported rating: {rating}")
    row = conn.execute("select id, status from learning_candidates where id=?", (candidate_id,)).fetchone()
    if not row:
        return {"candidate_id": candidate_id, "rating": rating, "ok": False, "action": "not_found", "learning_id": None}
    if row["status"] != "pending":
        return {"candidate_id": candidate_id, "rating": rating, "ok": False, "action": f"already_{row['status']}", "learning_id": None}
    if rating == "not_important":
        ok = reject_candidate(conn, candidate_id, "telegram_rating:not_important")
        learning_id = None
        action = "rejected"
    else:
        learning_id = accept_candidate(conn, candidate_id)
        ok = learning_id is not None
        action = "accepted"
        if ok:
            severity = 5 if rating == "major" else 2
            conn.execute("update learning_cards set severity=?, updated_at=? where id=?", (severity, utcnow(), learning_id))
            conn.commit()
    if ok:
        conn.execute(
            """
            insert into learning_candidate_ratings(candidate_id, learning_id, rating, source, telegram_user_id, telegram_chat_id_hash, created_at)
            values(?,?,?,?,?,?,?)
            on conflict(candidate_id, source) do update set
              learning_id=excluded.learning_id, rating=excluded.rating, telegram_user_id=excluded.telegram_user_id,
              telegram_chat_id_hash=excluded.telegram_chat_id_hash, created_at=excluded.created_at
            """,
            (
                candidate_id,
                learning_id,
                rating,
                "telegram",
                telegram_user_id,
                hash_chat_id(telegram_chat_id),
                utcnow(),
            ),
        )
        conn.execute(
            "insert into learning_card_feedback(learning_id, candidate_id, action, reason, created_at) values(?,?,?,?,?)",
            (learning_id, candidate_id, f"telegram_{rating}", None, utcnow()),
        )
        conn.commit()
    return {"candidate_id": candidate_id, "rating": rating, "ok": bool(ok), "action": action if ok else "failed", "learning_id": learning_id}


def poll_ratings(
    conn: sqlite3.Connection,
    client: TelegramClient,
    *,
    chat_id: str,
    timeout: int = 0,
    limit: int = 100,
    delete_reviewed: bool = True,
) -> dict[str, Any]:
    offset_value = get_state(conn, "telegram_last_update_id")
    offset = int(offset_value) + 1 if offset_value else None
    updates = client.get_updates(offset=offset, timeout=timeout, limit=limit, allowed_updates=["callback_query"])
    processed: list[dict[str, Any]] = []
    ignored = 0
    deleted = 0
    delete_failed = 0
    answer_failed = 0
    last_update_id: int | None = None
    for update in updates:
        if isinstance(update.get("update_id"), int):
            last_update_id = int(update["update_id"])
        callback = update.get("callback_query") if isinstance(update.get("callback_query"), dict) else None
        parsed = parse_callback_data(str(callback.get("data") or "") if callback else None)
        if not callback or not parsed:
            ignored += 1
            continue
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        callback_chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        callback_chat_id = str(callback_chat.get("id") or "")
        user = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message_id = message.get("message_id")
        if not callback_chat_id or callback_chat_id != str(chat_id):
            ignored += 1
            if not answer_callback_query_safely(client, callback.get("id"), "This prlearn bot is locked to another chat."):
                answer_failed += 1
            continue
        rating, candidate_id, nonce = parsed
        if not isinstance(message_id, int) or not callback_is_registered(
            conn,
            candidate_id=candidate_id,
            chat_id=callback_chat_id,
            message_id=message_id,
            nonce=nonce,
        ):
            ignored += 1
            if not answer_callback_query_safely(client, callback.get("id"), "This review button is no longer valid."):
                answer_failed += 1
            continue
        result = apply_candidate_rating(
            conn,
            candidate_id=candidate_id,
            rating=rating,
            telegram_user_id=str(user.get("id")) if user.get("id") is not None else None,
            telegram_chat_id=callback_chat_id or str(chat_id),
        )
        processed.append(result)
        label = rating.replace("_", " ")
        message = f"Recorded: {label}" if result.get("ok") else str(result.get("action") or "not recorded").replace("_", " ")
        if not answer_callback_query_safely(client, callback.get("id"), message):
            answer_failed += 1
        mark_candidate_message_reviewed(conn, candidate_id=candidate_id, chat_id=callback_chat_id, message_id=message_id, nonce=nonce)
        if delete_reviewed and result.get("ok") and callback_chat_id and isinstance(message_id, int):
            try:
                client.delete_message(chat_id=callback_chat_id, message_id=message_id)
                deleted += 1
            except TelegramError:
                delete_failed += 1
    if last_update_id is not None:
        set_state(conn, "telegram_last_update_id", str(last_update_id))
        conn.commit()
    return {
        "updates": len(updates),
        "processed": len(processed),
        "ignored": ignored,
        "deleted": deleted,
        "delete_failed": delete_failed,
        "answer_failed": answer_failed,
        "ratings": processed,
    }


def run_poll_loop(
    conn: sqlite3.Connection,
    client: TelegramClient,
    *,
    chat_id: str,
    timeout: int = 25,
    interval: float = 1.0,
    once: bool = False,
) -> None:
    while True:
        poll_ratings(conn, client, chat_id=chat_id, timeout=timeout)
        if once:
            return
        time.sleep(interval)


def hash_chat_id(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]
