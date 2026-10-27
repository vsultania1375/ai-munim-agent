"""Remote MCP end to end, as Claude/ChatGPT do it: discovery, registration, WhatsApp-code login, PKCE token,
tools over /mcp, preview -> confirm. Demo ERP and fake WhatsApp; no model call anywhere."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from tests.test_server_signals import SECRET, TESTER, post, wait_for

DOMAIN = "aimunim.duckdns.org"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
RPC = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}


@pytest.fixture
def mcp_app(tmp_path: Path, monkeypatch):
    from munim.whatsapp import client as wa_client

    monkeypatch.chdir(tmp_path)
    for key, value in {
        "WA_PHONE_NUMBER_ID": "P1", "WA_APP_SECRET": SECRET, "WA_ACCESS_TOKEN": "x", "ANTHROPIC_API_KEY": "x",
        "MUNIM_V2_TESTERS": TESTER, "MUNIM_STT": "none", "ERPNEXT_URL": "", "MUNIM_V2_POOL_DIR": str(tmp_path / "pool"),
        "MUNIM_BOT_NUMBER": "917428051112", "JEV_API_KEY": "", "MUNIM_SUMMARY": "off", "MUNIM_PUBLIC_DOMAIN": DOMAIN,
    }.items():
        monkeypatch.setenv(key, value)
    sent: list[tuple[str, str]] = []

    async def send_text(self, to, text):
        sent.append((to, text))
        return {"messages": [{"id": f"wamid.OUT{len(sent)}"}]}

    async def noop(self, *a, **k):
        return {}

    monkeypatch.setattr(wa_client.WhatsAppClient, "send_text", send_text)
    monkeypatch.setattr(wa_client.WhatsAppClient, "start_typing", noop)
    from munim.core.server import create_app

    with TestClient(create_app(env_file=tmp_path / "none.env")) as client:
        yield client, sent


def connect(client: TestClient, sent: list) -> str:
    """The whole OAuth dance; returns an access token bound to the tester's business."""
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"].endswith("/register") and "S256" in meta["code_challenge_methods_supported"]
    assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
    reg = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude", "token_endpoint_auth_method": "none",
                                         "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                                         "scope": "munim"}).json()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    auth = client.get("/authorize", params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
                                            "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
                                            "scope": "munim", "resource": f"https://{DOMAIN}/mcp"}, follow_redirects=False)
    assert auth.status_code in (302, 307)
    req = parse_qs(urlparse(auth.headers["location"]).query)["req"][0]
    page = client.get("/mcp/connect", params={"req": req})
    assert "Connect Claude" in page.text and "Verify on WhatsApp" in page.text
    assert client.post("/mcp/connect/start", json={"req": req}).status_code == 403  # not from our own page
    start = client.post("/mcp/connect/start", json={"req": req}, headers={"origin": f"https://{DOMAIN}"}).json()
    assert client.get("/mcp/connect/status", params={"req": req}).json() == {"waiting": True}
    post(client, {"messages": [{"from": TESTER, "id": "wamid.L1", "timestamp": str(int(time.time())), "type": "text",
                                "text": {"body": f"LOGIN {start['code']}"}}]})
    wait_for(lambda: sent)
    back = client.get("/mcp/connect/status", params={"req": req}).json()["redirect"]
    query = parse_qs(urlparse(back).query)
    assert back.startswith(REDIRECT) and query["state"] == ["xyz"]
    token = client.post("/token", data={"grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
                                        "client_id": reg["client_id"], "code_verifier": verifier}).json()
    assert token["token_type"].lower() == "bearer" and token["refresh_token"]
    again = client.post("/token", data={"grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
                                        "client_id": reg["client_id"], "code_verifier": verifier})
    assert again.status_code == 400  # a code works once
    return token["access_token"]


def rpc(client: TestClient, token: str | None, method: str, params: dict | None = None) -> dict:
    headers = {**RPC, **({"Authorization": f"Bearer {token}"} if token else {})}
    r = client.post("/mcp", headers={**headers, "Host": DOMAIN},
                    content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}))
    return {"status": r.status_code, **(r.json() if r.headers.get("content-type", "").startswith("application/json") else {})}


def call(client: TestClient, token: str, name: str, args: dict | None = None) -> dict:
    out = rpc(client, token, "tools/call", {"name": name, "arguments": args or {}})
    return json.loads(out["result"]["content"][0]["text"])


def test_connect_with_whatsapp_and_work_with_the_books(mcp_app):
    client, sent = mcp_app
    assert client.get("/health").json()["ok"] and "AI Munim" in client.get("/").text  # the portal still owns its routes
    assert rpc(client, None, "tools/list")["status"] == 401  # no token, no books
    token = connect(client, sent)
    rpc(client, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "1"}})
    tools = {tool["name"]: tool for tool in rpc(client, token, "tools/list")["result"]["tools"]}
    assert {"about_my_business", "business_report", "propose_sales_document", "confirm_preview"} <= set(tools)
    assert tools["confirm_preview"]["annotations"]["destructiveHint"] and tools["business_report"]["annotations"]["readOnlyHint"]
    assert "ask_user" not in tools and "save_document_facts" not in tools  # the person's own AI does the talking

    about = call(client, token, "about_my_business")
    assert about["role"] == "owner" and about["writes"] == "final documents" and about["open_preview"] is None
    gupta = call(client, token, "find_customer", {"name": "Gupta ji"})["candidates"][0]["customer_id"]
    preview = call(client, token, "propose_sales_document", {"kind": "invoice", "customer_id": gupta, "lines": [
        {"item_code": "CEM-50", "qty": 10, "rate_source": "price_list"}]})
    assert "₹5,760" in preview["preview_sent_to_user"]  # computed by our code, not the caller's AI
    stale = call(client, token, "confirm_preview", {"draft_id": preview["draft_id"], "version": preview["version"] + 1})
    assert "changed" in stale["error"]
    done = call(client, token, "confirm_preview", {"draft_id": preview["draft_id"], "version": preview["version"]})
    assert done["documents"][0]["doctype"] == "Sales Invoice" and done["documents"][0]["submitted"] is True
    report = call(client, token, "business_report", {"report": "receivables"})
    assert report["total_due"].startswith("₹")
    pdf_url = done["documents"][0]["pdf_url"]
    path = pdf_url.split(DOMAIN, 1)[1]
    got = client.get(path)
    assert got.status_code == 200 and got.headers["content-type"] == "application/pdf" and got.content.startswith(b"%PDF")
    assert client.get(path[:-3] + "zzz").status_code == 403  # tampered link
    again = call(client, token, "get_pdf", {"doctype": "Sales Invoice", "name": done["documents"][0]["doc"]})
    assert again["pdf_url"].startswith(f"https://{DOMAIN}/pdf/")
    assert "not found" in call(client, token, "get_pdf", {"doctype": "Sales Invoice", "name": "NOPE-1"})["error"]
    assert rpc(client, "not-a-token", "tools/list")["status"] == 401


def test_claude_and_chatgpt_extras(mcp_app):
    client, sent = mcp_app
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["client_id_metadata_document_supported"] is True and "none" in meta["token_endpoint_auth_methods_supported"]
    assert client.get("/munim-icon.svg").headers["content-type"].startswith("image/svg+xml")
    token = connect(client, sent)
    init = rpc(client, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "test", "version": "1"}})["result"]
    assert init["serverInfo"]["name"] == "AI Munim" and init.get("instructions")
    tools = {tool["name"]: tool for tool in rpc(client, token, "tools/list")["result"]["tools"]}
    card = tools["propose_sales_document"]
    assert card["title"] == "Prepare bill / quotation / order / challan"
    assert card["_meta"]["ui"]["resourceUri"] == "ui://munim/card.html" and card["_meta"]["openai/outputTemplate"]
    assert "_meta" not in tools["find_customer"] or not tools["find_customer"].get("_meta")
    resources = {r["uri"]: r for r in rpc(client, token, "resources/list")["result"]["resources"]}
    assert resources["ui://munim/card.html"]["mimeType"] == "text/html;profile=mcp-app"
    assert "ui://munim/card-skybridge.html" not in resources  # ChatGPT-only template: not offered as an attachment
    sky = rpc(client, token, "resources/read", {"uri": "ui://munim/card-skybridge.html"})["result"]["contents"][0]
    assert sky["mimeType"] == "text/html+skybridge"
    html = rpc(client, token, "resources/read", {"uri": "ui://munim/card.html"})["result"]["contents"][0]
    assert "ui/initialize" in html["text"] and "confirm_preview" in html["text"]
    prompts = {p["name"] for p in rpc(client, token, "prompts/list")["result"]["prompts"]}
    assert {"aaj_ka_hisaab", "udhaar_list", "naya_bill", "mahine_ka_review"} <= prompts
    got = rpc(client, token, "prompts/get", {"name": "naya_bill", "arguments": {"customer": "Gupta ji", "items": "10 cement"}})
    assert "Gupta ji" in got["result"]["messages"][0]["content"]["text"]
    report = rpc(client, token, "tools/call", {"name": "business_report", "arguments": {"report": "receivables"}})["result"]
    assert report["structuredContent"]["report"] == "receivables"  # the card reads this


def test_cimd_client_signs_in_with_its_published_identity(mcp_app, monkeypatch):
    from munim.core import mcp_server

    client, sent = mcp_app
    client_id = "https://claude.ai/oauth/mcp-client-metadata.json"

    async def published(url, http=None):
        return {"client_id": url, "client_name": "Claude", "redirect_uris": [REDIRECT],
                "grant_types": ["authorization_code", "refresh_token"]} if url == client_id else None

    monkeypatch.setattr(mcp_server, "fetch_client_document", published)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    auth = client.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
                                            "code_challenge": challenge, "code_challenge_method": "S256", "state": "s1",
                                            "scope": "munim"}, follow_redirects=False)
    req = parse_qs(urlparse(auth.headers["location"]).query)["req"][0]
    start = client.post("/mcp/connect/start", json={"req": req}, headers={"origin": f"https://{DOMAIN}"}).json()
    post(client, {"messages": [{"from": TESTER, "id": "wamid.C1", "timestamp": str(int(time.time())), "type": "text",
                                "text": {"body": f"LOGIN {start['code']}"}}]})
    wait_for(lambda: sent)
    code = parse_qs(urlparse(client.get("/mcp/connect/status", params={"req": req}).json()["redirect"]).query)["code"][0]
    token = client.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                        "client_id": client_id, "code_verifier": verifier}).json()
    assert token["access_token"]
    bad = client.get("/authorize", params={"response_type": "code", "client_id": "https://evil.example/c.json", "redirect_uri": REDIRECT,
                                           "code_challenge": challenge, "code_challenge_method": "S256"}, follow_redirects=False)
    assert bad.status_code == 400  # unknown published identity: no login page


async def test_client_documents_are_only_fetched_from_public_https(monkeypatch):
    from munim.core.mcp_server import fetch_client_document

    assert await fetch_client_document("http://claude.ai/x.json") is None
    assert await fetch_client_document("https://localhost/x.json") is None
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.0.0.5", 443))])
    assert await fetch_client_document("https://internal.example/x.json") is None  # private address


def test_pdf_file_short_link_details_upload_and_disconnect(mcp_app):
    from munim.core.mcp_server import OAuthStore

    client, sent = mcp_app
    token = connect(client, sent)
    gupta = call(client, token, "find_customer", {"name": "Gupta Traders"})["candidates"][0]["customer_id"]
    preview = call(client, token, "propose_sales_document", {"kind": "invoice", "customer_id": gupta, "lines": [
        {"item_code": "CEM-50", "qty": 2, "rate_source": "price_list"}]})
    out = rpc(client, token, "tools/call", {"name": "confirm_preview", "arguments": {"draft_id": preview["draft_id"],
                                                                                     "version": preview["version"]}})["result"]
    pdf_part = next(c for c in out["content"] if c["type"] == "resource")["resource"]
    assert pdf_part["mimeType"] == "application/pdf" and base64.b64decode(pdf_part["blob"]).startswith(b"%PDF")
    doc = out["structuredContent"]["documents"][0]

    path = doc["pdf_url"].split(DOMAIN, 1)[1]
    assert client.get(path).status_code == 200 and client.get(path).status_code == 200  # viewers refetch: fine
    store = OAuthStore(Path(".run/v2/mcp_oauth.db"))
    store.put("code", f"use:{path.rsplit('/', 1)[1]}", {"at": time.time() - 3600})
    assert client.get(path).status_code == 410  # minutes after its first open the link is dead

    details = call(client, token, "get_document", {"doctype": "Sales Invoice", "name": doc["doc"]})
    assert details["status"] == "Submitted" and details["items"][0]["qty"] == "2" and details["party"] == "Gupta Traders"
    assert "not a draft" in call(client, token, "finalise_draft", {"doctype": "Sales Invoice", "name": doc["doc"]})["error"]

    link = call(client, token, "get_upload_link")["upload_url"].split(DOMAIN, 1)[1]
    assert "Upload the bill" in client.get(link).text
    assert client.post(link, content=b"not an image").status_code == 400
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 200
    assert client.post(link, content=jpeg, headers={"Content-Type": "image/jpeg", "X-Filename": "bill.jpg"}).status_code == 200
    assert client.post(link, content=jpeg).status_code == 410  # one use
    bill = call(client, token, "propose_purchase_bill", {"supplier_id": "SUP-0001", "lines": [
        {"item_code": "CEM-50", "qty": 1, "rate": 380, "rate_source": "document"}]})
    assert "📎" in bill["preview_sent_to_user"]  # the uploaded file goes with the purchase

    apps = store.apps_for(TESTER)
    assert [a["name"] for a in apps] == ["Claude"] and apps[0]["last_used"]
    store.revoke_subject(TESTER, apps[0]["client_id"])
    assert rpc(client, token, "tools/list")["status"] == 401  # disconnected from the portal: stops at once


async def test_owner_finalises_a_staff_draft(tmp_path: Path):
    from datetime import date
    from decimal import Decimal
    from types import SimpleNamespace

    from munim.core.erp import DemoERP, DraftLine
    from munim.core.mcp_server import MunimMCP
    from munim.core.tenants import Tenant

    erp = DemoERP(artifact_dir=tmp_path, today=date(2026, 10, 27))
    draft = await erp.create_sales_draft("Sales Invoice", "CUST-0001", [DraftLine("CEM-50", Decimal(1), Decimal(450))], "r", final=False)
    owner, staff = Tenant(phone="1", role="owner", status="active"), Tenant(phone="2", role="staff", status="active")
    files: list = []
    engine = SimpleNamespace(erp=erp)
    server = MunimMCP.__new__(MunimMCP)
    server.pdf_link = lambda site, doctype, name: f"https://x/pdf/{name}"
    args = {"doctype": "Sales Invoice", "name": draft.name}
    assert "only the owner" in (await server._finalise(staff, engine, args, "demo", files))["error"]
    done = await server._finalise(owner, engine, args, "demo", files)
    assert done["documents"][0]["submitted"] and erp.documents[0]["docstatus"] == 1 and files and files[0][2].startswith(b"%PDF")
