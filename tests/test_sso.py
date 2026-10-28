from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from munim.core.sso import SSOStore, mount_sso, site_from_host

DOMAIN = "aimunim.example"


def test_site_from_host():
    assert site_from_host("tenant-3.aimunim.example", DOMAIN, "frontend") == "tenant-3"
    assert site_from_host("demo.aimunim.example:443", DOMAIN, "frontend") == "frontend"
    assert site_from_host("aimunim.example", DOMAIN, "frontend") is None
    assert site_from_host("a.b.aimunim.example", DOMAIN, "frontend") is None
    assert site_from_host("tenant-3.evil.example", DOMAIN, "frontend") is None


def test_token_single_use_and_bound_to_site(tmp_path: Path):
    sso = SSOStore(tmp_path / "s.db")
    token = sso.mint("911", "tenant-1")
    assert sso.consume(token, "tenant-2") is None  # not valid on another business's host
    assert sso.consume(token, "tenant-1") == "911"
    assert sso.consume(token, "tenant-1") is None  # used


def make_client(tmp_path: Path, cookies):
    sso = SSOStore(tmp_path / "s.db")
    calls: list[tuple[str, str]] = []

    async def start_session(phone, site):
        calls.append((phone, site))
        return cookies

    app = FastAPI()
    mount_sso(app, sso=sso, base_domain=DOMAIN, demo_site="frontend", start_session=start_session)
    return TestClient(app, base_url=f"https://tenant-1.{DOMAIN}"), sso, calls


def test_sso_sets_erp_cookies_on_erp_host_and_redirects(tmp_path: Path):
    client, sso, calls = make_client(tmp_path, {"sid": "abc123", "user_id": "9800000011@owner.aimunim.in"})
    token = sso.mint("919800000011", "tenant-1")
    response = client.get(f"/munim-sso?t={token}", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/app"
    cookies = response.headers.get_list("set-cookie")
    sid = next(c for c in cookies if c.startswith("sid="))
    assert "HttpOnly" in sid and "Secure" in sid and "Domain" not in sid  # host-only on the ERP subdomain
    assert calls == [("919800000011", "tenant-1")]
    assert client.get(f"/munim-sso?t={token}", follow_redirects=False).status_code == 401  # replay


def test_sso_refuses_token_for_another_site(tmp_path: Path):
    client, sso, calls = make_client(tmp_path, {"sid": "abc"})
    token = sso.mint("919800000011", "tenant-2")
    assert client.get(f"/munim-sso?t={token}", follow_redirects=False).status_code == 401 and calls == []


def test_business_name_addresses(tmp_path):
    from munim.core.tenants import TenantStore, slugify
    from munim.core.web_access import web_host

    assert slugify("Sharma Hardware & Sons!") == "sharma-hardware-sons"
    tenants = TenantStore(tmp_path / "t.db")
    tenants.claim_site("919800000001", ["tenant-1", "tenant-2", "tenant-3"])
    tenants.claim_site("919800000002", ["tenant-1", "tenant-2", "tenant-3"])
    assert tenants.label_for("tenant-1", "Sharma Hardware") == "sharma-hardware"
    assert tenants.label_for("tenant-1", "Renamed Later") == "sharma-hardware"  # an address, once given, stays
    assert tenants.label_for("tenant-2", "Sharma Hardware") == "sharma-hardware-2"  # same name, another shop
    assert tenants.label_for("tenant-3", "AB") == "tenant-3" and tenants.label_for("tenant-3", "Demo") == "tenant-3"
    assert tenants.site_for_label("sharma-hardware") == "tenant-1" and tenants.site_for_label("tenant-2") == "tenant-2"
    assert tenants.site_for_label("nobody") is None and tenants.site_for_label("tenant-3") is None  # unclaimed
    resolve = tenants.site_for_label
    assert site_from_host("sharma-hardware-2.aimunim.example", DOMAIN, "frontend", resolve) == "tenant-2"
    assert site_from_host("demo.aimunim.example", DOMAIN, "frontend", resolve) == "frontend"
    assert web_host("tenant-1", DOMAIN, "frontend", "sharma-hardware") == "sharma-hardware.aimunim.example"
