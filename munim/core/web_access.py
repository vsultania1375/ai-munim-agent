"""Browser access to the owner's own ERPNext: one login per business owner.

WhatsApp is one way to run the ERP; the web desk is the other. The owner logs in with
their 10-digit mobile number. Passwords are generated here, set through the site's
Administrator API, and sent once in the owner's own WhatsApp chat.
"""
from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from typing import Any

from munim.core.i18n import t

OWNER_ROLES = (
    "System Manager", "Accounts Manager", "Accounts User", "Sales Manager", "Sales User",
    "Sales Master Manager", "Stock Manager", "Stock User", "Purchase Manager", "Purchase User", "Item Manager",
)
STAFF_ROLES = ("Accounts User", "Sales User", "Sales Master Manager", "Stock User", "Purchase User")
RESET_HINT = "password reset"
LOGIN_WORDS = {
    "erp login", "web login", "login", "erp", "erp kholna hai", "website", "computer pe", "laptop pe",
    "erp link", "login details", "dashboard", "erpnext",
}
RESET_WORDS = {"password reset", "reset password", "password bhul gaya", "naya password", "password"}


@dataclass(frozen=True)
class WebLogin:
    url: str
    login_id: str
    password: str


def web_host(site: str, base_domain: str, demo_site: str) -> str:
    return f"demo.{base_domain}" if site == demo_site else f"{site}.{base_domain}"


def login_id_for(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def new_password() -> str:
    # readable on a phone: no 0/O/1/l confusion; letters + digits + one symbol
    alphabet = "".join(ch for ch in string.ascii_letters + string.digits if ch not in "0O1lI")
    core = "".join(secrets.choice(alphabet) for _ in range(10))
    return f"{core[:5]}-{core[5:]}"


def wants_login(text: str | None) -> bool:
    return (text or "").strip().lower() in LOGIN_WORDS


def wants_reset(text: str | None) -> bool:
    return (text or "").strip().lower() in RESET_WORDS


def user_email(phone: str) -> str:
    return f"{login_id_for(phone)}@owner.aimunim.in"


async def ensure_user(api: Any, *, phone: str, name: str | None, roles: tuple[str, ...], password: str) -> str:
    """Create this person's ERPNext user if missing and set the given password. Returns the user id."""
    login_id = login_id_for(phone)
    email = user_email(phone)
    await api.http.put(
        f"{api.base_url}/api/resource/System Settings/System Settings",
        headers=api.headers,
        json={"allow_login_using_mobile_number": 1, "allow_login_using_user_name": 1},
    )
    if not await api.exists("User", email):
        await api.insert(
            "User",
            {
                "email": email,
                "first_name": (name or "Owner").split()[0][:40],
                "username": login_id,
                "mobile_no": login_id,
                "send_welcome_email": 0,
                "user_type": "System User",
                "roles": [{"role": role} for role in roles],
            },
        )
    response = await api.http.put(
        f"{api.base_url}/api/resource/User/{email}", headers=api.headers, json={"new_password": password, "enabled": 1}
    )
    response.raise_for_status()
    return email


async def ensure_owner_login(
    api: Any, *, phone: str, name: str | None, url: str, roles: tuple[str, ...] = OWNER_ROLES
) -> WebLogin:
    """Reset to a fresh human-readable password (for typing on another device)."""
    password = new_password()
    await ensure_user(api, phone=phone, name=name, roles=roles, password=password)
    return WebLogin(url, login_id_for(phone), password)


def login_message(login: WebLogin, *, first_time: bool, lang: str | None = None) -> str:
    head = t("web.head_new" if first_time else "web.head_reset", lang)
    return (
        f"{head}\n\n"
        f"🌐 {login.url}\n"
        f"👤 Login ID: *{login.login_id}*\n"
        f"🔒 Password: *{login.password}*\n\n"
        + t("web.body", lang)
    )
