"""WhatsApp event log: every inbound message, every outbound message and its delivery status.

It answers "what was that message?" for swipe-replies (the user replied to a preview, a
PDF, a question, or one of their own photos), and keeps delivery outcomes so failures
are never silent.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LoggedMessage:
    message_id: str
    direction: str  # in | out
    chat: str  # the customer-side phone number
    kind: str
    text: str
    meta: dict[str, Any]
    at: float
    status: str | None = None


class WALog:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS msg (id TEXT PRIMARY KEY, direction TEXT, chat TEXT, kind TEXT, text TEXT,"
            " meta TEXT, at REAL, status TEXT, status_at REAL, error TEXT)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS msg_chat ON msg (chat, at)")
        self._db.commit()

    def record(self, *, message_id: str, direction: str, chat: str, kind: str, text: str = "",
               meta: dict[str, Any] | None = None, at: float | None = None) -> None:
        if not message_id:
            return
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO msg (id, direction, chat, kind, text, meta, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_id, direction, chat, kind, (text or "")[:2000], json.dumps(meta or {}, ensure_ascii=False),
                 at if at is not None else time.time()),
            )
            self._db.commit()

    def update_meta(self, message_id: str, **values: Any) -> None:
        found = self.get(message_id)
        if found is None:
            return
        meta = {**found.meta, **values}
        with self._lock:
            self._db.execute("UPDATE msg SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), message_id))
            self._db.commit()

    def status(self, message_id: str, status: str, at: float | None = None, error: str | None = None) -> LoggedMessage | None:
        """Record a delivery status; statuses only move forward (sent < delivered < read), failures always stick."""
        rank = {"sent": 1, "delivered": 2, "read": 3, "failed": 9}
        found = self.get(message_id)
        if found is None:
            return None
        if status != "failed" and rank.get(found.status or "", 0) >= rank.get(status, 0):
            return found
        with self._lock:
            self._db.execute("UPDATE msg SET status=?, status_at=?, error=? WHERE id=?",
                             (status, at or time.time(), error, message_id))
            self._db.commit()
        return self.get(message_id)

    def get(self, message_id: str | None) -> LoggedMessage | None:
        if not message_id:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT id, direction, chat, kind, text, meta, at, status FROM msg WHERE id=?", (message_id,)
            ).fetchone()
        if not row:
            return None
        return LoggedMessage(row[0], row[1], row[2], row[3], row[4], json.loads(row[5] or "{}"), row[6], row[7])

    def last_inbound_at(self, chat: str) -> float | None:
        """When this person last wrote to us (WhatsApp's 24 h window starts there)."""
        with self._lock:
            row = self._db.execute("SELECT MAX(at) FROM msg WHERE chat=? AND direction='in'", (chat,)).fetchone()
        return row[0] if row and row[0] else None

    def knows_doc(self, name: str) -> bool:
        """True if the bot itself sent this ERP document to someone (so a web-ERP event is not news)."""
        with self._lock:
            row = self._db.execute("SELECT 1 FROM msg WHERE direction='out' AND meta LIKE ? LIMIT 1",
                                   (f'%"doc": "{name}"%',)).fetchone()
        return row is not None

    def last_outbound(self, chat: str) -> LoggedMessage | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM msg WHERE chat=? AND direction='out' ORDER BY at DESC LIMIT 1", (chat,)
            ).fetchone()
        return self.get(row[0]) if row else None

    def move_chat(self, old: str, new: str) -> None:
        with self._lock:
            self._db.execute("UPDATE msg SET chat=? WHERE chat=?", (new, old))
            self._db.commit()
