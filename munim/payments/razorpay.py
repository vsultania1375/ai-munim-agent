"""Razorpay payment links, per business: each business connects its OWN Razorpay account, so money goes straight
to its bank. We never hold funds.

- Keys are entered on the web portal (never in chat), checked against Razorpay, and stored encrypted per ERP site.
- The owner switches online payments on or off; nothing is sent until they connect and switch on.
- A paid link is learnt two ways: the business's webhook (signed with its own webhook secret, per-site URL) or,
  when no webhook is set, by checking open links every couple of minutes. Each link is recorded once.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken

API = "https://api.razorpay.com/v1"
POLL_DAYS = 7  # open links older than this are not checked any more (the webhook still records them)


@dataclass(frozen=True)
class Account:
    site: str
    key_id: str
    key_secret: str
    webhook_secret: str
    enabled: bool
    connected_by: str
    connected_at: float

    @property
    def mode(self) -> str:
        return "test" if self.key_id.startswith("rzp_test_") else "live"

    @property
    def auth(self) -> tuple[str, str]:
        return self.key_id, self.key_secret


@dataclass(frozen=True)
class PaidLink:
    link_id: str
    site: str
    invoice: str
    customer_id: str
    customer_name: str
    amount: Decimal
    payment_id: str
    method: str


class PaymentAccounts:
    """One Razorpay account per ERP site; secrets encrypted at rest with a key derived from the server secret."""

    def __init__(self, path: Path | str, server_secret: str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(b"munim-razorpay-v1:" + server_secret.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS account (site TEXT PRIMARY KEY, key_id TEXT, secret TEXT, hook TEXT,"
                         " enabled INTEGER, connected_by TEXT, connected_at REAL)")
        self._db.commit()

    def connect(self, site: str, key_id: str, key_secret: str, by: str) -> Account:
        """Save (or replace) the site's keys, switched on, with a fresh webhook secret for its dashboard."""
        hook = secrets.token_urlsafe(24)
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO account VALUES (?, ?, ?, ?, 1, ?, ?)",
                             (site, key_id, self._fernet.encrypt(key_secret.encode()).decode(),
                              self._fernet.encrypt(hook.encode()).decode(), by, time.time()))
            self._db.commit()
        account = self.get(site)
        assert account is not None
        return account

    def get(self, site: str) -> Account | None:
        with self._lock:
            row = self._db.execute("SELECT site, key_id, secret, hook, enabled, connected_by, connected_at FROM account WHERE site=?",
                                   (site,)).fetchone()
        if row is None:
            return None
        try:
            secret, hook = (self._fernet.decrypt(v.encode()).decode() for v in (row[2], row[3]))
        except InvalidToken:  # the server secret changed: the owner connects again
            return None
        return Account(row[0], row[1], secret, hook, bool(row[4]), row[5], row[6])

    def set_enabled(self, site: str, enabled: bool) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE account SET enabled=? WHERE site=?", (int(enabled), site))
            self._db.commit()
        return cur.rowcount == 1

    def disconnect(self, site: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM account WHERE site=?", (site,))
            self._db.commit()

    def sites(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT site FROM account")]


def clean_keys(key_id: str, key_secret: str) -> tuple[str, str]:
    key_id, key_secret = (key_id or "").strip(), (key_secret or "").strip()
    if not key_id.startswith(("rzp_test_", "rzp_live_")) or len(key_id) > 40:
        raise ValueError("Key ID starts with rzp_test_ or rzp_live_")
    if not 10 <= len(key_secret) <= 64 or not key_secret.isalnum():
        raise ValueError("Key Secret looks wrong; copy it again from Razorpay")
    return key_id, key_secret


class RazorpayLinks:
    def __init__(self, store: Path | str, http: httpx.AsyncClient | None = None) -> None:
        self.http = http or httpx.AsyncClient(timeout=20)
        if str(store) != ":memory:":
            Path(store).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(store), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS paylink (id TEXT PRIMARY KEY, site TEXT, invoice TEXT, customer_id TEXT,"
                         " customer_name TEXT, amount TEXT, url TEXT, created REAL, paid_at REAL, payment_id TEXT)")
        self._db.commit()

    async def keys_work(self, key_id: str, key_secret: str) -> bool:
        """Razorpay accepts these keys (a read-only call)."""
        r = await self.http.get(f"{API}/payment_links", params={"count": 1}, auth=(key_id, key_secret))
        if r.status_code in (401, 403):
            return False
        r.raise_for_status()
        return True

    def link_for(self, site: str, invoice: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT url FROM paylink WHERE site=? AND invoice=? AND paid_at IS NULL", (site, invoice)).fetchone()
        return row[0] if row else None

    async def create(self, account: Account, *, invoice: str, customer_id: str, customer_name: str, amount: Decimal,
                     description: str) -> str:
        """A link for the invoice's grand total on the business's own account; one open link per invoice."""
        existing = self.link_for(account.site, invoice)
        if existing:
            return existing
        body = {
            "amount": int((amount * 100).to_integral_value()), "currency": "INR", "accept_partial": False,
            "description": description[:2048], "reference_id": f"{account.site}:{invoice}"[:40],
            "customer": {"name": customer_name[:100]}, "notify": {"sms": False, "email": False}, "reminder_enable": False,
            "notes": {"site": account.site, "invoice": invoice},
        }
        r = await self.http.post(f"{API}/payment_links", json=body, auth=account.auth)
        r.raise_for_status()
        data = r.json()
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO paylink (id, site, invoice, customer_id, customer_name, amount, url, created)"
                             " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (data["id"], account.site, invoice, customer_id, customer_name, str(amount), data["short_url"], time.time()))
            self._db.commit()
        return str(data["short_url"])

    @staticmethod
    def verify(account: Account, body: bytes, signature: str | None) -> bool:
        if not signature:
            return False
        expected = hmac.new(account.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def _mark_paid(self, site: str, link_id: str, payment_id: str, amount: Decimal, method: str) -> PaidLink | None:
        """Record a link as paid, once; None if it isn't this site's link or was already recorded."""
        with self._lock:
            row = self._db.execute("SELECT site, invoice, customer_id, customer_name, paid_at FROM paylink WHERE id=?",
                                   (link_id,)).fetchone()
            if row is None or row[0] != site or row[4]:
                return None
            self._db.execute("UPDATE paylink SET paid_at=?, payment_id=? WHERE id=?", (time.time(), payment_id, link_id))
            self._db.commit()
        return PaidLink(link_id, row[0], row[1], row[2], row[3] or row[2], amount, payment_id, method)

    def paid(self, site: str, body: bytes) -> PaidLink | None:
        """The paid link from a verified webhook of this site, once (a retried webhook returns None)."""
        event = json.loads(body)
        if event.get("event") != "payment_link.paid":
            return None
        link = ((event.get("payload") or {}).get("payment_link") or {}).get("entity") or {}
        payment = ((event.get("payload") or {}).get("payment") or {}).get("entity") or {}
        amount = Decimal(int(link.get("amount_paid") or payment.get("amount") or 0)) / 100
        return self._mark_paid(site, str(link.get("id") or ""), str(payment.get("id") or ""), amount, str(payment.get("method") or ""))

    async def check_open(self, account: Account) -> list[PaidLink]:
        """Ask Razorpay about this site's recent unpaid links (works without a webhook)."""
        with self._lock:
            rows = self._db.execute("SELECT id FROM paylink WHERE site=? AND paid_at IS NULL AND created >= ?",
                                    (account.site, time.time() - POLL_DAYS * 86400)).fetchall()
        found = []
        for (link_id,) in rows:
            r = await self.http.get(f"{API}/payment_links/{link_id}", auth=account.auth)
            if r.status_code != 200:
                continue
            data = r.json()
            if data.get("status") != "paid":
                continue
            payments = [p for p in data.get("payments") or [] if p.get("status") in ("captured", "authorized")] or [{}]
            amount = Decimal(int(data.get("amount_paid") or payments[-1].get("amount") or 0)) / 100
            paid = self._mark_paid(account.site, link_id, str(payments[-1].get("payment_id") or ""), amount,
                                   str(payments[-1].get("method") or ""))
            if paid:
                found.append(paid)
        return found
