"""Who may set up online payments for which business, and what the portal and the agent see.

Owners set up their own business. On the shared demo business only testers can, so a stranger trying the demo
can never point its bills at their own (or anyone's) Razorpay account.
"""
from __future__ import annotations

from typing import Any

from munim.core.tenants import DEMO_SITE, Tenant
from munim.payments.razorpay import Account, PaymentAccounts, RazorpayLinks, clean_keys


class PaymentService:
    def __init__(self, accounts: PaymentAccounts, links: RazorpayLinks, base_domain: str) -> None:
        self.accounts, self.links, self.base_domain = accounts, links, base_domain

    @staticmethod
    def site_of(tenant: Tenant) -> str | None:
        """The business this person may set payments up for, or None."""
        if tenant.role == "tester":
            return DEMO_SITE
        if tenant.role == "owner" and tenant.status == "active" and tenant.site and tenant.site != DEMO_SITE:
            return tenant.site
        return None

    def webhook_url(self, site: str) -> str:
        return f"https://{self.base_domain}/razorpay/webhook/{site}"

    def status(self, tenant: Tenant) -> dict[str, Any] | None:
        site = self.site_of(tenant)
        if site is None:
            return None
        account = self.accounts.get(site)
        if account is None:
            return {"connected": False}
        return {"connected": True, "enabled": account.enabled, "mode": account.mode,
                "key": account.key_id[:13] + "…" + account.key_id[-4:], "webhook_url": self.webhook_url(site),
                "webhook_secret": account.webhook_secret}

    async def connect(self, tenant: Tenant, key_id: str, key_secret: str) -> dict[str, Any]:
        site = self.site_of(tenant)
        if site is None:
            raise PermissionError("only the business owner can connect Razorpay")
        key_id, key_secret = clean_keys(key_id, key_secret)
        if not await self.links.keys_work(key_id, key_secret):
            raise ValueError("Razorpay did not accept these keys")
        self.accounts.connect(site, key_id, key_secret, by=tenant.phone)
        return self.status(tenant) or {}

    def set_enabled(self, tenant: Tenant, enabled: bool) -> bool:
        site = self.site_of(tenant)
        return site is not None and self.accounts.set_enabled(site, enabled)

    def disconnect(self, tenant: Tenant) -> bool:
        site = self.site_of(tenant)
        if site is None:
            return False
        self.accounts.disconnect(site)
        return True

    def account_for_bill(self, tenant: Tenant, site: str) -> Account | None:
        """The account a bill made by this person on this site gets a pay link from, if payments are on."""
        account = self.accounts.get(site)
        if account is None or not account.enabled:
            return None
        if site == DEMO_SITE and tenant.role != "tester":
            return None  # demo visitors' bills never get the tester's links
        return account
