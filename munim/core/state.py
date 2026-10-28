"""ConversationState: the single source of conversational context per chat."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAX_TURNS = 12
MAX_TURN_CHARS = 1200
MEMORY_SHOWN = 30  # remembered facts the agent sees each turn
MEMORY_KEPT = 100  # older ones drop off


@dataclass
class DraftLineState:
    item_code: str
    item_name: str
    uom: str
    qty: str
    rate: str
    rate_source: str  # price_list | user_stated | document


@dataclass
class Draft:
    kind: str  # quotation | sales_order | invoice | delivery_note | payment | new_customer | purchase | expense | new_supplier | credit_note | cancel | repeat | pricing | brand | bahi
    customer_id: str | None = None
    customer_name: str | None = None
    lines: list[DraftLineState] = field(default_factory=list)
    payment_amount: str | None = None
    payment_mode: str | None = None
    new_customer: dict[str, Any] | None = None
    allocations: list[dict[str, str]] = field(default_factory=list)  # payment -> invoices
    source: dict[str, str] | None = None  # {doctype, name}: made from this earlier document (links kept)
    hsn: dict[str, str] = field(default_factory=dict)  # item_code -> HSN to save on the item at Approve
    bill_no: str | None = None  # purchase: the supplier's bill number and date
    bill_date: str | None = None
    attachment: str | None = None  # purchase: path of the photo/PDF to attach
    expense_account: str | None = None
    note: str | None = None
    amend: dict[str, str] | None = None  # {doctype, name}: this preview corrects (replaces) a final document
    discount: str | None = None  # percent applied to the lines' rates, shown on the card
    customer_discount: str | None = None  # the customer's permanent % already in the price-list rates
    brand: dict[str, Any] | None = None  # bill design to apply: colours, logo, bank details
    entries: list[dict[str, Any]] = field(default_factory=list)  # bahi page: parties with their netted dues
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    version: int = 1
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Draft:
        data = dict(data)
        data["lines"] = [DraftLineState(**line) for line in data.get("lines", [])]
        return cls(**data)


@dataclass
class DocumentContext:
    media_path: str
    mime: str
    filename: str | None
    received_at: float
    facts: dict[str, Any] | None = None


@dataclass
class PendingQuestion:
    question: str
    options: list[dict[str, str]]  # [{"id": "q1", "title": "..."}]
    asked_at: float = field(default_factory=time.time)


@dataclass
class ConversationState:
    sender: str
    turns: list[dict[str, str]] = field(default_factory=list)
    draft: Draft | None = None
    document: DocumentContext | None = None
    question: PendingQuestion | None = None
    last_created: list[dict[str, str]] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)  # the rest of a multi-document request, done after each Approve
    can_submit: bool = True  # owner/tester: Approve finalises; staff: Approve makes a draft
    voice_replies: bool = True  # answer a voice note with a voice note too
    style: dict[str, Any] = field(default_factory=dict)  # how this person writes (core/style.py)
    lang: str | None = None  # en | hi (fixed text); the LLM mirrors the user itself
    updated_at: float = field(default_factory=time.time)

    def add_turn(self, role: str, text: str, at: float | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        text = text[:MAX_TURN_CHARS]
        at = at if at is not None else time.time()
        if self.turns and self.turns[-1]["role"] == role:
            self.turns[-1]["text"] = (self.turns[-1]["text"] + "\n" + text)[-MAX_TURN_CHARS:]
            self.turns[-1]["at"] = at
        else:
            self.turns.append({"role": role, "text": text, "at": at})
        self.turns = self.turns[-MAX_TURNS:]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> ConversationState:
        data = json.loads(raw)
        draft = data.get("draft")
        document = data.get("document")
        question = data.get("question")
        return cls(
            sender=data["sender"],
            turns=data.get("turns", []),
            draft=Draft.from_dict(draft) if draft else None,
            document=DocumentContext(**document) if document else None,
            question=PendingQuestion(**question) if question else None,
            last_created=data.get("last_created", []),
            next_steps=data.get("next_steps", []),
            lang=data.get("lang"),
            voice_replies=data.get("voice_replies", True),
            style=data.get("style", {}),
            updated_at=data.get("updated_at", time.time()),
        )


def _alias_key(value: str) -> str:
    return " ".join((value or "").lower().split())


class StateStore:
    """SQLite-backed store. One row per (tenant, sender)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS conversation (tenant TEXT, sender TEXT, state TEXT, updated REAL,"
            " PRIMARY KEY (tenant, sender))"
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS seen_message (id TEXT PRIMARY KEY, at REAL)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS alias (tenant TEXT, kind TEXT, alias TEXT, target TEXT, PRIMARY KEY (tenant, kind, alias))"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS media_cache (tenant TEXT, hash TEXT, facts TEXT, at REAL, PRIMARY KEY (tenant, hash))"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, sender TEXT, body TEXT, at REAL, done INTEGER DEFAULT 0)"
        )
        self._db.execute(  # business memory: what the owner told us to keep in mind, per business
            "CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT, fact TEXT, about TEXT, by TEXT, at REAL)"
        )
        self._db.commit()

    def load(self, tenant: str, sender: str) -> ConversationState:
        with self._lock:
            row = self._db.execute(
                "SELECT state FROM conversation WHERE tenant=? AND sender=?", (tenant, sender)
            ).fetchone()
        return ConversationState.from_json(row[0]) if row else ConversationState(sender=sender)

    def save(self, tenant: str, state: ConversationState) -> None:
        state.updated_at = time.time()
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO conversation VALUES (?, ?, ?, ?)",
                (tenant, state.sender, state.to_json(), state.updated_at),
            )
            self._db.commit()

    def reset(self, tenant: str, sender: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM conversation WHERE tenant=? AND sender=?", (tenant, sender))
            self._db.commit()

    def alias_get(self, tenant: str, kind: str, alias: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT target FROM alias WHERE tenant=? AND kind=? AND alias=?",
                                   (tenant, kind, _alias_key(alias))).fetchone()
        return row[0] if row else None

    def alias_put(self, tenant: str, kind: str, alias: str, target: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO alias VALUES (?, ?, ?, ?)", (tenant, kind, _alias_key(alias), target))
            self._db.commit()

    def memory_list(self, tenant: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT id, fact, about, at FROM memory WHERE tenant=? ORDER BY id DESC LIMIT ?",
                                    (tenant, MEMORY_SHOWN)).fetchall()
        return [{"id": r[0], "fact": r[1], "about": r[2], "at": r[3]} for r in reversed(rows)]

    def memory_add(self, tenant: str, fact: str, about: str | None, by: str) -> int:
        with self._lock:
            same = self._db.execute("SELECT id FROM memory WHERE tenant=? AND lower(fact)=lower(?)", (tenant, fact)).fetchone()
            if same:
                return int(same[0])
            cur = self._db.execute("INSERT INTO memory (tenant, fact, about, by, at) VALUES (?, ?, ?, ?, ?)",
                                   (tenant, fact, about, by, time.time()))
            self._db.execute("DELETE FROM memory WHERE tenant=? AND id NOT IN (SELECT id FROM memory WHERE tenant=? ORDER BY id DESC LIMIT ?)",
                             (tenant, tenant, MEMORY_KEPT))
            self._db.commit()
        return int(cur.lastrowid)

    def memory_forget(self, tenant: str, memory_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM memory WHERE tenant=? AND id=?", (tenant, memory_id))
            self._db.commit()
        return cur.rowcount == 1

    def move_sender(self, old: str, new: str) -> None:
        """A customer changed their WhatsApp number: keep their conversation."""
        with self._lock:
            rows = self._db.execute("SELECT tenant, state FROM conversation WHERE sender=?", (old,)).fetchall()
            for tenant, raw in rows:
                data = json.loads(raw)
                data["sender"] = new
                self._db.execute("INSERT OR REPLACE INTO conversation VALUES (?, ?, ?, ?)", (tenant, new, json.dumps(data), time.time()))
            self._db.execute("DELETE FROM conversation WHERE sender=?", (old,))
            self._db.commit()

    def media_get(self, tenant: str, digest: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT facts FROM media_cache WHERE tenant=? AND hash=?", (tenant, digest)).fetchone()
        return json.loads(row[0]) if row else None

    def media_put(self, tenant: str, digest: str, facts: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO media_cache VALUES (?, ?, ?, ?)",
                             (tenant, digest, json.dumps(facts, ensure_ascii=False), time.time()))
            self._db.commit()

    def inbox_add(self, message_id: str, sender: str, body: str) -> bool:
        """Persist an inbound message before the webhook ACKs. False if it was already stored."""
        with self._lock:
            try:
                self._db.execute("INSERT INTO inbox (id, sender, body, at) VALUES (?, ?, ?, ?)",
                                 (message_id, sender, body, time.time()))
                self._db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def inbox_done(self, message_ids: list[str]) -> None:
        with self._lock:
            self._db.executemany("UPDATE inbox SET done=1 WHERE id=?", [(m,) for m in message_ids])
            self._db.commit()

    def inbox_pending(self, max_age_seconds: float = 3600) -> list[tuple[str, str]]:
        """Messages accepted but never finished (e.g. a restart mid-turn), oldest first."""
        cutoff = time.time() - max_age_seconds
        with self._lock:
            rows = self._db.execute(
                "SELECT id, body FROM inbox WHERE done=0 AND at >= ? ORDER BY at", (cutoff,)
            ).fetchall()
            self._db.execute("DELETE FROM inbox WHERE at < ?", (cutoff - 7 * 86400,))
            self._db.commit()
        return [(r[0], r[1]) for r in rows]

    def first_time_seen(self, message_id: str) -> bool:
        """True exactly once per message id (webhook replay protection)."""
        with self._lock:
            try:
                self._db.execute("INSERT INTO seen_message VALUES (?, ?)", (message_id, time.time()))
                self._db.commit()
                return True
            except sqlite3.IntegrityError:
                return False
