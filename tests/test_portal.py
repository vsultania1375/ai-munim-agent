from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from munim.core.portal import PortalStore, parse_command
from munim.core.portal_web import COOKIE, mount_portal
from munim.core.tenants import Tenant, TenantStore

OWNER, STAFF = "919800000011", "919800000022"
DOMAIN = "aimunim.example"


def test_parse_commands():
    assert parse_command("LOGIN AB12CD") == ("login", "AB12CD")
    assert parse_command("join: xy34zq please") == ("join", "XY34ZQ")
    assert parse_command("login karna hai") is None


def test_login_code_is_single_use_and_cookie_is_signed(tmp_path: Path):
    portal = PortalStore(tmp_path / "p.db", "secret")
    sid, code = portal.start_login()
    assert portal.login_phone(sid) is None
    assert portal.verify_login(code, OWNER) and not portal.verify_login(code, STAFF)
    assert portal.login_phone(sid) == OWNER
    cookie = portal.session_cookie(OWNER)
    assert portal.session_phone(cookie) == OWNER
    payload, sig = cookie.rsplit(".", 1)
    assert portal.session_phone(f"{payload}.{'0' * len(sig)}") is None  # tampered
    assert PortalStore(tmp_path / "q.db", "other-secret").session_phone(cookie) is None


def test_invite_redeems_once(tmp_path: Path):
    portal = PortalStore(tmp_path / "p.db", "secret")
    code = portal.create_invite(site="tenant-1", company="Sharma Hardware", created_by=OWNER)
    invite = portal.redeem_invite(code, STAFF)
    assert invite and invite.site == "tenant-1" and invite.created_by == OWNER
    assert portal.redeem_invite(code, "919800000033") is None


def test_members_and_remove(tmp_path: Path):
    tenants = TenantStore(tmp_path / "t.db")
    tenants.save(Tenant(OWNER, role="owner", status="active", site="tenant-1", company="Sharma Hardware"))
    tenants.save(Tenant(STAFF, role="staff", status="active", site="tenant-1", company="Sharma Hardware"))
    assert {m.phone for m in tenants.members("tenant-1")} == {OWNER, STAFF}
    assert not tenants.remove_member("tenant-1", OWNER)  # owner cannot be removed
    assert tenants.remove_member("tenant-1", STAFF)
    assert tenants.get(STAFF).status == "new" and tenants.get(STAFF).site is None


def make_client(tmp_path: Path):
    portal = PortalStore(tmp_path / "p.db", "secret")
    tenants = TenantStore(tmp_path / "t.db")
    tenants.save(Tenant(OWNER, role="owner", status="active", site="tenant-1", company="Sharma Hardware"))
    sent: list[str] = []

    async def send_password(tenant: Tenant) -> bool:
        sent.append(tenant.phone)
        return True

    app = FastAPI()
    mount_portal(app, portal=portal, tenants=tenants, bot_number="917400000000", base_domain=DOMAIN, send_password=send_password)
    return TestClient(app, base_url=f"https://{DOMAIN}"), portal, tenants, sent


def test_portal_login_flow_and_owner_page(tmp_path: Path):
    client, portal, _, sent = make_client(tmp_path)
    home = client.get("/").text
    assert "Open my ERP" in home and "data:image/svg+xml" in home
    assert client.post("/portal/login", headers={"origin": "https://evil.example"}).status_code == 403
    start = client.post("/portal/login", headers={"origin": f"https://{DOMAIN}"}).json()
    assert "LOGIN%20" in start["link"] and len(start["code"]) == 6
    assert start["qr"].startswith("data:image/svg+xml")
    assert start["web"].startswith("https://web.whatsapp.com/send?phone=917400000000&text=LOGIN%20")
    assert start["app"].startswith("whatsapp://send?phone=917400000000&text=LOGIN%20")
    assert client.get(f"/portal/login/{start['sid']}").json() == {"verified": False, "expired": False}
    portal.verify_login(start["code"], OWNER)
    assert client.get(f"/portal/login/{start['sid']}").json()["verified"] is True
    me = client.get("/portal/me")
    assert "Sharma Hardware" in me.text and "/portal/me/open" in me.text and "https://tenant-1.aimunim.example" in me.text and "Team numbers" in me.text
    ok = client.post("/portal/me/password", headers={"origin": f"https://{DOMAIN}"}).json()
    assert ok == {"ok": True} and sent == [OWNER]
    invite = client.post("/portal/me/invite", headers={"origin": f"https://{DOMAIN}"}).json()
    assert portal.redeem_invite(invite["code"], STAFF).site == "tenant-1"


def test_portal_me_requires_session(tmp_path: Path):
    client, *_ = make_client(tmp_path)
    response = client.get("/portal/me", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/#open"
    client.cookies.set(COOKIE, "forged.cookie")
    assert client.get("/portal/me", follow_redirects=False).status_code == 303


def test_bare_code_is_recognised_but_words_are_not():
    assert parse_command("2DQRR3") == ("code", "2DQRR3")
    assert parse_command("  2dqrr3. ") == ("code", "2DQRR3")
    assert parse_command("CEMENT") is None and parse_command("cement bag") is None
    assert parse_command("2DQRR3 ka bill") is None


def test_generated_codes_always_have_a_digit(tmp_path: Path):
    portal = PortalStore(tmp_path / "p.db", "secret")
    codes = [portal.start_login()[1] for _ in range(200)]
    assert all(any(ch.isdigit() for ch in c) for c in codes)
    assert all(parse_command(c) == ("code", c) for c in codes)


def test_verified_user_sees_welcome_back_and_erp_logout_clears_session(tmp_path: Path):
    client, portal, _, _ = make_client(tmp_path)
    client.cookies.set(COOKIE, portal.session_cookie(OWNER))
    home = client.get("/").text
    assert "Welcome back" in home and "/portal/me/open" in home
    out = client.get("/?from=erp")
    assert "logged out" in out.text and "Verify with WhatsApp" in out.text
    assert any(h.startswith(f"{COOKIE}=") and "Max-Age=0" in h for h in out.headers.get_list("set-cookie"))


def test_portal_shows_pixel_art_and_qr(tmp_path: Path):
    client, *_ = make_client(tmp_path)
    home = client.get("/").text
    assert 'shape-rendering="crispEdges"' in home and "Aapka hisaab-kitaab" in home and "data:image/svg+xml" in home
