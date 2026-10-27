"""Web portal state: WhatsApp-verified login and team invites.

Login without OTP templates: the portal shows a short code and a wa.me link that pre-fills
"LOGIN <code>". When that message arrives from a number, the number is proven, the
browser session is marked verified, and the portal can show that number's ERP.
Team invites work the same way with "JOIN <code>", sent from the new member's phone.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

LOGIN_TTL = 10 * 60
INVITE_TTL = 24 * 3600
SESSION_TTL = 12 * 3600
MAGIC_TTL = 15 * 60
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
COMMAND_RE = re.compile(r"^\s*(LOGIN|JOIN)[\s:-]*([A-Z0-9]{6})\b", re.IGNORECASE)


BARE_CODE_RE = re.compile(r"^\s*(?=[A-Z0-9]*[2-9])([A-Z0-9]{6})\s*[.!]?\s*$", re.IGNORECASE)


def parse_command(text: str | None) -> tuple[str, str] | None:
    """("login"|"join", CODE), or ("code", CODE) for a bare code people paste without the word."""
    match = COMMAND_RE.match(text or "")
    if match:
        return match.group(1).lower(), match.group(2).upper()
    bare = BARE_CODE_RE.match(text or "")  # needs a digit, so plain words like CEMENT never match
    return ("code", bare.group(1).upper()) if bare else None


def wa_link(bot_number: str, message: str) -> str:
    return f"https://wa.me/{bot_number}?text={quote(message)}"


def wa_web_link(bot_number: str, message: str) -> str:
    """WhatsApp Web on this computer, chat open with the message typed."""
    return f"https://web.whatsapp.com/send?phone={bot_number}&text={quote(message)}"


def wa_app_link(bot_number: str, message: str) -> str:
    """WhatsApp Desktop app (Windows/Mac) via its URL scheme."""
    return f"whatsapp://send?phone={bot_number}&text={quote(message)}"


def qr_data_uri(link: str) -> str:
    """QR for a wa.me link as an inline SVG data URI (scan on phone -> chat opens with the text typed)."""
    import segno

    return segno.make(link, error="m").svg_data_uri(scale=6, border=2, dark="#111", light="#fff")


@dataclass(frozen=True)
class Invite:
    code: str
    site: str
    company: str
    role: str
    created_by: str


class PortalStore:
    def __init__(self, path: Path | str, secret: str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.secret = secret.encode()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS login (sid TEXT PRIMARY KEY, code TEXT UNIQUE, at REAL, phone TEXT)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS invite (code TEXT PRIMARY KEY, site TEXT, company TEXT, role TEXT,"
            " created_by TEXT, at REAL, used_by TEXT)"
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS magic (token TEXT PRIMARY KEY, phone TEXT, at REAL, used INTEGER)")
        self._db.commit()

    @staticmethod
    def _code() -> str:
        while True:  # always include a digit so a bare code can be told apart from a word
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            if any(ch.isdigit() for ch in code):
                return code

    # ---- login -------------------------------------------------------
    def start_login(self) -> tuple[str, str]:
        sid = secrets.token_urlsafe(24)
        with self._lock:
            self._db.execute("DELETE FROM login WHERE at < ?", (time.time() - LOGIN_TTL,))
            while True:
                code = self._code()
                try:
                    self._db.execute("INSERT INTO login VALUES (?, ?, ?, NULL)", (sid, code, time.time()))
                    break
                except sqlite3.IntegrityError:
                    continue
            self._db.commit()
        return sid, code

    def verify_login(self, code: str, phone: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE login SET phone=? WHERE code=? AND phone IS NULL AND at >= ?", (phone, code, time.time() - LOGIN_TTL)
            )
            self._db.commit()
            return cur.rowcount == 1

    def login_phone(self, sid: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT phone FROM login WHERE sid=? AND at >= ?", (sid, time.time() - LOGIN_TTL)).fetchone()
        return row[0] if row else None

    def login_pending(self, sid: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM login WHERE sid=? AND at >= ?", (sid, time.time() - LOGIN_TTL)).fetchone()
        return row is not None

    # ---- signed browser session ---------------------------------------
    def session_cookie(self, phone: str) -> str:
        payload = base64.urlsafe_b64encode(json.dumps({"p": phone, "e": int(time.time()) + SESSION_TTL}).encode()).decode()
        sig = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def session_phone(self, cookie: str | None) -> str | None:
        if not cookie or "." not in cookie:
            return None
        payload, sig = cookie.rsplit(".", 1)
        expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return data["p"] if data.get("e", 0) > time.time() else None

    # ---- one-time sign-in link, sent to the person's own WhatsApp -----------
    def magic_link(self, phone: str) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._db.execute("DELETE FROM magic WHERE at < ?", (time.time() - MAGIC_TTL,))
            self._db.execute("INSERT INTO magic VALUES (?, ?, ?, 0)", (token, phone, time.time()))
            self._db.commit()
        return token

    def redeem_magic(self, token: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT phone FROM magic WHERE token=? AND used=0 AND at >= ?",
                                   (token, time.time() - MAGIC_TTL)).fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE magic SET used=1 WHERE token=?", (token,))
            self._db.commit()
        return row[0]

    # ---- team invites --------------------------------------------------
    def create_invite(self, *, site: str, company: str, created_by: str, role: str = "staff") -> str:
        with self._lock:
            while True:
                code = self._code()
                try:
                    self._db.execute("INSERT INTO invite VALUES (?, ?, ?, ?, ?, ?, NULL)",
                                     (code, site, company, role, created_by, time.time()))
                    break
                except sqlite3.IntegrityError:
                    continue
            self._db.commit()
        return code

    def invite_open(self, code: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM invite WHERE code=? AND used_by IS NULL AND at >= ?", (code, time.time() - INVITE_TTL)
            ).fetchone()
        return row is not None

    def redeem_invite(self, code: str, phone: str) -> Invite | None:
        with self._lock:
            row = self._db.execute(
                "SELECT code, site, company, role, created_by FROM invite WHERE code=? AND used_by IS NULL AND at >= ?",
                (code, time.time() - INVITE_TTL),
            ).fetchone()
            if not row:
                return None
            self._db.execute("UPDATE invite SET used_by=? WHERE code=?", (phone, code))
            self._db.commit()
        return Invite(*row)
