"""One-tap ERP sign-in: WhatsApp-verified people never type an ERPNext password.

Flow: the bot (portal "Open ERP" or WhatsApp "ERP login") mints a single-use token bound
to one site -> the browser opens https://<site-host>/munim-sso?t=<token> (Caddy sends that
path to the bot) -> the bot signs in to ERPNext server-side with the user's stored random
password and answers with ERPNext's own session cookies, host-scoped to that ERP
subdomain -> redirect to /app.
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

SSO_TTL = 15 * 60
SESSION_COOKIES = ("sid", "system_user", "full_name", "user_id", "user_image", "user_lang")


def site_from_host(host: str, base_domain: str, demo_site: str) -> str | None:
    host = (host or "").split(":")[0].lower()
    suffix = f".{base_domain}"
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if not label or "." in label:
        return None
    return demo_site if label == "demo" else label


class SSOStore:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS sso (token TEXT PRIMARY KEY, phone TEXT, site TEXT, at REAL, used INTEGER DEFAULT 0)")
        self._db.commit()

    def mint(self, phone: str, site: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._db.execute("DELETE FROM sso WHERE at < ?", (time.time() - 86400,))
            self._db.execute("INSERT INTO sso (token, phone, site, at) VALUES (?, ?, ?, ?)", (token, phone, site, time.time()))
            self._db.commit()
        return token

    def consume(self, token: str, site: str) -> str | None:
        """Phone for a fresh, unused token minted for this very site; marks it used."""
        with self._lock:
            row = self._db.execute(
                "SELECT phone FROM sso WHERE token=? AND site=? AND used=0 AND at >= ?", (token, site, time.time() - SSO_TTL)
            ).fetchone()
            if not row:
                return None
            self._db.execute("UPDATE sso SET used=1 WHERE token=?", (token,))
            self._db.commit()
        return row[0]


async def erp_session(base_url: str, site: str, user: str, password: str, http: httpx.AsyncClient | None = None) -> dict[str, str] | None:
    """Log in to one ERPNext site server-side; returns its session cookies, or None if refused."""
    client = http or httpx.AsyncClient(timeout=20)
    try:
        response = await client.post(f"{base_url.rstrip('/')}/api/method/login", headers={"Host": site},
                                      data={"usr": user, "pwd": password})
    finally:
        if http is None:
            await client.aclose()
    if response.status_code != 200:
        return None
    cookies = {name: value for name, value in response.cookies.items() if name in SESSION_COOKIES}
    return cookies if cookies.get("sid") and cookies["sid"] != "Guest" else None


def mount_sso(
    app: FastAPI,
    *,
    sso: SSOStore,
    base_domain: str,
    demo_site: str,
    start_session: Callable[[str, str], Awaitable[dict[str, str] | None]],
) -> None:
    @app.get("/munim-sso")
    async def munim_sso(request: Request, t: str = "") -> Response:
        site = site_from_host(request.headers.get("host", ""), base_domain, demo_site)
        phone = sso.consume(t, site) if site and t else None
        cookies = await start_session(phone, site) if phone and site else None
        if not cookies:
            return HTMLResponse(
                "<meta name=viewport content='width=device-width'><p style='font:16px system-ui;padding:24px'>"
                f"This sign-in link has expired or was already used. Get a new one at "
                f"<a href='https://{base_domain}'>{base_domain}</a> or type <b>ERP login</b> on WhatsApp.</p>",
                status_code=401,
            )
        response = Response(status_code=303, headers={"Location": "/app", "Cache-Control": "no-store"})
        for name, value in cookies.items():
            response.set_cookie(name, value, max_age=12 * 3600, path="/", secure=True,
                                httponly=name == "sid", samesite="lax")
        return response
