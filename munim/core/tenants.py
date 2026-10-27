"""Tenant registry: which WhatsApp number belongs to which business and ERPNext site."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEMO_SITE = "frontend"


@dataclass
class Tenant:
    phone: str
    role: str = "owner"  # owner | tester
    status: str = "new"  # new | onboarding | demo | active
    step: str = "welcome"  # onboarding step
    site: str | None = None
    company: str | None = None
    data: dict[str, Any] = field(default_factory=dict)  # onboarding answers
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def erp_site(self) -> str | None:
        if self.status == "active" and self.site:
            return self.site
        if self.status == "demo" or self.role == "tester":
            return DEMO_SITE
        return None


class TenantStore:
    def __init__(self, path: Path | str, *, testers: set[str] | None = None) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS tenant (phone TEXT PRIMARY KEY, body TEXT)")
        self._db.execute("CREATE TABLE IF NOT EXISTS site_claim (site TEXT PRIMARY KEY, phone TEXT, at REAL)")
        self._db.commit()
        self.testers = testers or set()

    def get(self, phone: str) -> Tenant:
        with self._lock:
            row = self._db.execute("SELECT body FROM tenant WHERE phone=?", (phone,)).fetchone()
        if row:
            tenant = Tenant(**json.loads(row[0]))
        else:
            tenant = Tenant(phone=phone)
        if phone in self.testers:
            tenant.role, tenant.status = "tester", "active"
            tenant.site = tenant.site or DEMO_SITE
            tenant.company = tenant.company or "AI Munim Demo Traders"
        return tenant

    def save(self, tenant: Tenant) -> None:
        tenant.updated_at = time.time()
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO tenant VALUES (?, ?)", (tenant.phone, json.dumps(asdict(tenant))))
            self._db.commit()

    def move(self, old: str, new: str) -> Tenant | None:
        """The person changed their WhatsApp number: carry the account over."""
        with self._lock:
            row = self._db.execute("SELECT body FROM tenant WHERE phone=?", (old,)).fetchone()
            if not row or self._db.execute("SELECT 1 FROM tenant WHERE phone=?", (new,)).fetchone():
                return None  # nothing to move, or the new number already has its own account
            data = json.loads(row[0])
            data["phone"] = new
            data.setdefault("data", {}).pop("erp_pw", None)  # the ERP login is per number; re-made on next sign-in
            data["data"]["previous_phone"] = old
            self._db.execute("INSERT INTO tenant VALUES (?, ?)", (new, json.dumps(data)))
            self._db.execute("DELETE FROM tenant WHERE phone=?", (old,))
            self._db.execute("UPDATE site_claim SET phone=? WHERE phone=?", (new, old))
            self._db.commit()
        return self.get(new)

    def claim_site(self, phone: str, available: list[str]) -> str | None:
        """Atomically give this phone a free pool site (or the one it already holds)."""
        with self._lock:
            held = self._db.execute("SELECT site FROM site_claim WHERE phone=?", (phone,)).fetchone()
            if held:
                return held[0]
            taken = {r[0] for r in self._db.execute("SELECT site FROM site_claim")}
            for site in sorted(available):
                if site not in taken:
                    self._db.execute("INSERT INTO site_claim VALUES (?, ?, ?)", (site, phone, time.time()))
                    self._db.commit()
                    return site
        return None

    def all_active(self) -> list[Tenant]:
        with self._lock:
            rows = self._db.execute("SELECT body FROM tenant").fetchall()
        return [t for t in (Tenant(**json.loads(r[0])) for r in rows) if t.status == "active"]

    def members(self, site: str) -> list[Tenant]:
        """Everyone (owner and staff) whose number is active on this ERPNext site."""
        with self._lock:
            rows = self._db.execute("SELECT body FROM tenant").fetchall()
        found = [Tenant(**json.loads(r[0])) for r in rows]
        return [t for t in found if t.site == site and t.status == "active"]

    def remove_member(self, site: str, phone: str) -> bool:
        """Detach a staff number from a site (owners cannot be removed this way)."""
        tenant = self.get(phone)
        if tenant.site != site or tenant.role != "staff":
            return False
        tenant.status, tenant.role, tenant.site, tenant.company, tenant.step = "new", "owner", None, None, "welcome"
        tenant.data.pop("owner_phone", None)
        self.save(tenant)
        return True

    def claims_today(self) -> int:
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM site_claim WHERE at >= ?", (midnight,)).fetchone()[0])

    def has_claim(self, phone: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM site_claim WHERE phone=?", (phone,)).fetchone() is not None

    def claimed_sites(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT site FROM site_claim")}

    def free_sites(self, available: list[str]) -> int:
        with self._lock:
            taken = {r[0] for r in self._db.execute("SELECT site FROM site_claim")}
        return len([s for s in available if s not in taken])
