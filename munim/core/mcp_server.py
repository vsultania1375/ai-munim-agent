"""Connect your own Claude or ChatGPT to AI Munim: a remote MCP server (Streamable HTTP at /mcp).

Login is the WhatsApp code, as on the portal: the AI app's OAuth "Connect" opens /mcp/connect, the person
sends LOGIN <code> to the bot on WhatsApp, and the token is bound to that number's business (site) and
role. Their own AI does the talking, so no model call is made on our side. The tools are the WhatsApp
agent's closed set and money is still computed by our code. Writes are two-step: a propose_* tool
returns the preview; confirm_preview makes the document (final for the owner, a draft for staff).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from mcp.server.apps import Apps
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources import TextResource
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, Icon, TextContent, ToolAnnotations
from mcp.types import Tool as MCPTool

from munim.core import pixel
from munim.core.i18n import t
from munim.core.portal import PortalStore, qr_data_uri, wa_app_link, wa_link, wa_web_link
from munim.core.state import StateStore
from munim.core.tenants import DEMO_SITE, Tenant, TenantStore
from munim.core.tools import TOOL_SPECS, ToolExecutor

log = logging.getLogger(__name__)

SCOPE = "munim"
CODE_TTL = 5 * 60
ACCESS_TTL = 24 * 3600
REFRESH_TTL = 90 * 24 * 3600
CONNECT_TTL = 10 * 60

READ_TOOLS = ("find_customer", "find_item", "find_supplier", "customer_outstanding", "recent_documents", "business_report")
PREVIEW_TOOLS = ("propose_sales_document", "propose_from_document", "edit_draft", "apply_discount", "set_item_hsn",
                 "propose_payment", "propose_new_customer", "propose_new_supplier", "propose_purchase_bill",
                 "propose_expense", "propose_return", "propose_cancel", "propose_correction", "propose_repeat",
                 "cancel_draft", "save_alias", "set_customer_discount", "set_brand", "propose_bahi_import")

UI_URI = "ui://munim/card.html"  # MCP Apps (Claude and other hosts)
SKY_URI = "ui://munim/card-skybridge.html"  # the same card for ChatGPT's Apps SDK
WIDGET = Path(__file__).with_name("mcp_widget.html").read_text(encoding="utf-8")
UI_TOOLS = (set(PREVIEW_TOOLS) - {"cancel_draft", "save_alias"}) | {"business_report", "confirm_preview"}
CIMD_TTL = 3600
PDF_TTL = 15 * 60  # a short link; the PDF itself also comes with the tool result
PDF_REUSE = 5 * 60  # after the first open the link works a few minutes more (viewers refetch), then dies
UPLOAD_TTL = 15 * 60
MAX_UPLOAD = 10 * 1024 * 1024
MAX_INLINE_PDF = 3 * 1024 * 1024
DETAIL_DOCTYPES = ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "Payment Entry", "Purchase Invoice", "Journal Entry")
PRINTABLE = ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "Payment Entry", "Purchase Invoice")
TITLES = {
    "about_my_business": "About my business", "find_customer": "Find customer", "find_item": "Find item",
    "find_supplier": "Find supplier", "customer_outstanding": "Customer dues", "recent_documents": "Recent documents",
    "business_report": "Business report", "propose_sales_document": "Prepare bill / quotation / order / challan",
    "propose_from_document": "Next document from an existing one", "edit_draft": "Edit the preview",
    "apply_discount": "Apply discount", "set_customer_discount": "Permanent customer discount", "set_brand": "Bill design (logo, colours, bank)", "propose_bahi_import": "Bahi page to old dues", "set_item_hsn": "Set HSN code", "propose_payment": "Record payment received",
    "propose_new_customer": "Add customer", "propose_new_supplier": "Add supplier", "propose_purchase_bill": "Record purchase bill",
    "propose_expense": "Record expense", "propose_return": "Goods returned (credit note)", "propose_cancel": "Cancel a document",
    "propose_correction": "Correct a final document", "propose_repeat": "Recurring bill", "cancel_draft": "Discard the preview",
    "save_alias": "Remember a nickname", "confirm_preview": "Confirm and make it", "get_pdf": "Get the PDF",
    "get_document": "Open a document", "finalise_draft": "Finalise a draft", "get_upload_link": "Upload a bill photo/PDF",
}

INSTRUCTIONS = """AI Munim is this person's own accounting books (ERPNext) for their Indian shop or business.
Rules:
- Never compute money yourself. Previews and reports give exact figures (GST included); quote them as returned.
- Resolve names with find_customer / find_item / find_supplier first; never invent IDs. If several match, ask.
- Every write is two steps: a propose_* tool returns a preview card; show it to the person and call
  confirm_preview only after they clearly say yes. Owners' documents are made final; staff make drafts.
- Use business_report for questions about sales, dues, money received, profit, stock or pending work.
- Dates are India dates; the financial year starts on 1 April.
- confirm_preview and get_pdf return the PDF itself and a short-lived pdf_url (15 min); give the person the link.
- get_document shows what is on a quotation/order/challan/bill/payment. Staff drafts are finalised by the owner with
  finalise_draft after they agree.
- To attach a supplier's bill photo/PDF to a purchase, call get_upload_link, ask the person to upload there, then
  propose_purchase_bill (you read the bill yourself from what they share in this chat).
- Reply in the person's language (Hindi/Hinglish/English)."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class OAuthStore:
    """Clients, pending WhatsApp logins, codes and tokens. Tokens are stored only as hashes."""

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        for table in ("client (id TEXT PRIMARY KEY, body TEXT)", "pending (id TEXT PRIMARY KEY, body TEXT, at REAL)",
                      "code (id TEXT PRIMARY KEY, body TEXT, at REAL)", "token (id TEXT PRIMARY KEY, kind TEXT, body TEXT, at REAL)"):
            self._db.execute(f"CREATE TABLE IF NOT EXISTS {table}")
        self._db.commit()

    def put(self, table: str, key: str, body: dict[str, Any], kind: str | None = None) -> None:
        with self._lock:
            if table == "token":
                self._db.execute("INSERT OR REPLACE INTO token VALUES (?, ?, ?, ?)", (key, kind, json.dumps(body), time.time()))
            elif table == "client":
                self._db.execute("INSERT OR REPLACE INTO client VALUES (?, ?)", (key, json.dumps(body)))
            else:
                self._db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?)", (key, json.dumps(body), time.time()))
            self._db.commit()

    def get(self, table: str, key: str, kind: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if table == "token":
                row = self._db.execute("SELECT body FROM token WHERE id=? AND kind=?", (key, kind)).fetchone()
            else:
                row = self._db.execute(f"SELECT body FROM {table} WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, table: str, key: str) -> None:
        with self._lock:
            self._db.execute(f"DELETE FROM {table} WHERE id=?", (key,))
            self._db.commit()

    def apps_for(self, subject: str) -> list[dict[str, Any]]:
        """AI apps connected for this phone: one entry per app, from its live refresh tokens."""
        found: dict[str, dict[str, Any]] = {}
        with self._lock:
            rows = self._db.execute("SELECT kind, body, at FROM token").fetchall()
        for kind, body, at in rows:
            data = json.loads(body)
            if data.get("subject") != subject:
                continue
            if data["client_id"] not in found:  # older tokens didn't keep the app's name: take it from its registration
                registered = self.get("client", data["client_id"]) or {}
                found[data["client_id"]] = {"client_id": data["client_id"], "connected_at": at, "last_used": None,
                                            "name": data.get("client_name") or registered.get("client_name") or "AI app"}
            app = found[data["client_id"]]
            app["connected_at"] = min(app["connected_at"], at)
            if kind == "access" and data.get("last_used"):
                app["last_used"] = max(app["last_used"] or 0, data["last_used"])
        return sorted(found.values(), key=lambda a: a["connected_at"])

    def touch(self, key: str) -> None:
        with self._lock:
            row = self._db.execute("SELECT body FROM token WHERE id=? AND kind='access'", (key,)).fetchone()
            if row:
                data = json.loads(row[0])
                if time.time() - (data.get("last_used") or 0) > 60:
                    data["last_used"] = time.time()
                    self._db.execute("UPDATE token SET body=? WHERE id=?", (json.dumps(data), key))
                    self._db.commit()

    def first_use(self, key: str) -> float:
        """When a one-time link was first opened (records now on the first call)."""
        body = self.get("code", f"use:{key}")
        if body is None:
            self.put("code", f"use:{key}", {"at": time.time()})
            return time.time()
        return float(body["at"])

    def revoke_subject(self, subject: str, client_id: str) -> None:
        with self._lock:
            for key, body in self._db.execute("SELECT id, body FROM token").fetchall():
                data = json.loads(body)
                if data.get("subject") == subject and data.get("client_id") == client_id:
                    self._db.execute("DELETE FROM token WHERE id=?", (key,))
            self._db.commit()


class WhatsAppOAuthProvider:
    """OAuth 2.1 authorization server whose login step is the WhatsApp code (PKCE + dynamic registration)."""

    def __init__(self, store: OAuthStore, tenants: TenantStore, base_url: str, http: httpx.AsyncClient | None = None) -> None:
        self.store, self.tenants, self.base_url = store, tenants, base_url.rstrip("/")
        self.http = http
        self.cimd: dict[str, tuple[float, OAuthClientInformationFull]] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if client_id.startswith("https://"):  # Client ID Metadata Document (e.g. Claude's published identity)
            return await self._metadata_client(client_id)
        body = self.store.get("client", client_id)
        return OAuthClientInformationFull.model_validate(body) if body else None

    async def _metadata_client(self, url: str) -> OAuthClientInformationFull | None:
        cached = self.cimd.get(url)
        if cached and time.time() - cached[0] < CIMD_TTL:
            return cached[1]
        doc = await fetch_client_document(url, self.http)
        if doc is None:
            return None
        try:
            info = OAuthClientInformationFull(
                client_id=url, redirect_uris=doc["redirect_uris"], client_name=doc.get("client_name"),
                client_uri=doc.get("client_uri"), logo_uri=doc.get("logo_uri"), token_endpoint_auth_method="none",
                grant_types=doc.get("grant_types") or ["authorization_code", "refresh_token"], response_types=["code"], scope=SCOPE)
        except (KeyError, ValueError):
            return None
        self.cimd[url] = (time.time(), info)
        return info

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put("client", client_info.client_id or "", client_info.model_dump(mode="json"))

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req = secrets.token_urlsafe(24)
        self.store.put("pending", req, {"client_id": client.client_id, "client_name": client.client_name or "",
                                        "params": params.model_dump(mode="json")})
        return f"{self.base_url}/mcp/connect?req={req}"

    def issue_code(self, req: str, phone: str) -> str | None:
        """The person verified on WhatsApp: turn their pending request into a one-time authorization code."""
        pending = self.store.get("pending", req)
        if pending is None:
            return None
        self.store.delete("pending", req)
        params = AuthorizationParams.model_validate(pending["params"])
        code = secrets.token_urlsafe(32)
        self.store.put("code", code, AuthorizationCode(
            code=code, scopes=params.scopes or [SCOPE], expires_at=time.time() + CODE_TTL, client_id=pending["client_id"],
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource, subject=phone,
        ).model_dump(mode="json"))
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        body = self.store.get("code", authorization_code)
        if body is None:
            return None
        code = AuthorizationCode.model_validate(body)
        return code if code.client_id == client.client_id and code.expires_at > time.time() else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        self.store.delete("code", authorization_code.code)  # single use
        return self._issue(client.client_id or "", authorization_code.scopes, authorization_code.subject or "",
                           authorization_code.resource, client.client_name)

    def _issue(self, client_id: str, scopes: list[str], subject: str, resource: str | None, client_name: str | None = None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
        now = int(time.time())
        self.store.put("token", _hash(access), AccessToken(token="", client_id=client_id, scopes=scopes, expires_at=now + ACCESS_TTL,
                                                            resource=resource, subject=subject).model_dump(mode="json")
                       | {"client_name": client_name}, "access")
        self.store.put("token", _hash(refresh), RefreshToken(token="", client_id=client_id, scopes=scopes, expires_at=now + REFRESH_TTL,
                                                              resource=resource, subject=subject).model_dump(mode="json")
                       | {"client_name": client_name}, "refresh")
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL, refresh_token=refresh, scope=" ".join(scopes))

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        body = self.store.get("token", _hash(refresh_token), "refresh")
        if body is None:
            return None
        token = RefreshToken.model_validate({**body, "token": refresh_token})
        if token.client_id != client.client_id or (token.expires_at and token.expires_at < time.time()):
            return None
        return token

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        if not self._active(refresh_token.subject):
            raise TokenError(error="invalid_grant", error_description="this WhatsApp number no longer has a business")
        self.store.delete("token", _hash(refresh_token.token))  # rotate
        return self._issue(client.client_id or "", scopes or refresh_token.scopes, refresh_token.subject or "", refresh_token.resource,
                           client.client_name)

    async def load_access_token(self, token: str) -> AccessToken | None:
        body = self.store.get("token", _hash(token), "access")
        if body is None:
            return None
        access = AccessToken.model_validate({**body, "token": token})
        if (access.expires_at and access.expires_at < time.time()) or not self._active(access.subject):
            return None  # expired, or the number was removed from the team
        self.store.touch(_hash(token))
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke_subject(token.subject or "", token.client_id)

    def _active(self, phone: str | None) -> bool:
        if not phone:
            return False
        tenant = self.tenants.get(phone)
        return tenant.status == "active" and bool(tenant.erp_site)


class MunimMCP(MCPServer):
    """Serves the WhatsApp agent's tool set to the person's own AI, on their own business only."""

    def __init__(self, *, engine_for: Callable[[str, str | None], Any], store: StateStore, tenants: TenantStore,
                 audit: Callable[[str, dict[str, Any]], None], pdf_link: Callable[[str, str, str], str],
                 upload_link: Callable[[str, str], str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._prompts()
        self.engine_for, self.state_store, self.tenants, self.audit = engine_for, store, tenants, audit
        self.pdf_link, self.upload_link = pdf_link, upload_link
        specs = {s["name"]: s for s in TOOL_SPECS}
        self.specs: dict[str, dict[str, Any]] = {name: specs[name] for name in (*READ_TOOLS, *PREVIEW_TOOLS) if name in specs}
        self.specs["about_my_business"] = {"name": "about_my_business", "description": (
            "Who is connected: business name, the person's role (owner/staff), GST registration, today's India date "
            "and any preview still waiting for confirmation. Call this first."),
            "input_schema": {"type": "object", "properties": {}}}
        self.specs["confirm_preview"] = {"name": "confirm_preview", "description": (
            "Make the document shown in the open preview, after the person said yes to it. Pass the draft_id and "
            "version the preview returned; an older version is refused."),
            "input_schema": {"type": "object", "properties": {"draft_id": {"type": "string"}, "version": {"type": "integer"}},
                             "required": ["draft_id", "version"]}}
        self.specs["get_pdf"] = {"name": "get_pdf", "description": (
            "The PDF of a document in these books (quotation, order, challan, bill, payment or purchase bill), as a file "
            "and a download link valid 15 minutes. Give the link to the person."),
            "input_schema": {"type": "object", "properties": {"doctype": {"type": "string", "enum": list(PRINTABLE)},
                                                           "name": {"type": "string"}}, "required": ["doctype", "name"]}}

        doc_args = {"type": "object", "properties": {"doctype": {"type": "string", "enum": list(DETAIL_DOCTYPES)},
                                                    "name": {"type": "string"}}, "required": ["doctype", "name"]}
        self.specs["get_document"] = {"name": "get_document", "description": (
            "What is on a document: party, date, status, items with qty/rate/amount, GST rows, total, what is still due, "
            "and what it was made from. Use recent_documents to find names."), "input_schema": doc_args}
        self.specs["finalise_draft"] = {"name": "finalise_draft", "description": (
            "Owner only: make a DRAFT document final (submitted), e.g. one a staff member prepared. Ask the person first."),
            "input_schema": doc_args}
        self.specs["get_upload_link"] = {"name": "get_upload_link", "description": (
            "A one-time page (15 minutes) where the person uploads a supplier bill photo or PDF; it is attached to the "
            "next propose_purchase_bill. You cannot send file bytes yourself."), "input_schema": {"type": "object", "properties": {}}}

    def _prompts(self) -> None:
        """Ready-made asks that show in the AI app's prompt menu."""
        @self.prompt(name="aaj_ka_hisaab", title="Aaj ka hisaab", description="Yesterday's sales, money in, dues and pending work")
        def today() -> str:
            return ("From my AI Munim books: yesterday's sales and money received, who owes how much (oldest first), "
                    "and what is still pending. Keep it short, in Hinglish, with exact figures from the tools.")

        @self.prompt(name="udhaar_list", title="Udhaar list", description="Who owes money, by age, and whom to call first")
        def dues() -> str:
            return ("Show my receivables by age from AI Munim and suggest whom to call first today and what to say. "
                    "Use only the figures the report returns.")

        @self.prompt(name="naya_bill", title="Naya bill", description="Prepare a bill; you confirm it in the chat")
        def new_bill(customer: str, items: str) -> str:
            return (f"Prepare a sales invoice in AI Munim for {customer}: {items}. Find the customer and items first, "
                    "show me the preview and wait for my yes before confirming.")

        @self.prompt(name="mahine_ka_review", title="Mahine ka review", description="This month vs last: sales, top items, profit")
        def month() -> str:
            return ("Review my business in AI Munim: this month's sales vs last month, top items and customers, profit, "
                    "dues. Give 3 practical suggestions. Only quote figures the tools return.")

    def _tool_input_schema(self, name: str) -> dict[str, Any] | None:
        spec = self.specs.get(name)
        return spec["input_schema"] if spec else None

    async def list_tools(self) -> list[MCPTool]:
        tools = []
        for name, spec in self.specs.items():
            read_only = name in READ_TOOLS or name in {"about_my_business", "get_pdf", "get_document", "get_upload_link"}
            meta = None
            if name in UI_TOOLS:  # the card renders the preview/report in the chat, with Confirm / Cancel
                meta = {"ui": {"resourceUri": UI_URI}, "openai/outputTemplate": SKY_URI, "openai/widgetAccessible": True,
                        "openai/toolInvocation/invoking": "Checking your books…", "openai/toolInvocation/invoked": "Ready"}
            tools.append(MCPTool(
                name=name, title=TITLES.get(name), description=spec["description"], input_schema=spec["input_schema"], _meta=meta,
                annotations=ToolAnnotations(read_only_hint=read_only, destructive_hint=name in {"confirm_preview", "finalise_draft"},
                                            idempotent_hint=read_only, open_world_hint=False)))
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> CallToolResult:
        access = get_access_token()
        tenant = self.tenants.get(access.subject) if access and access.subject else None
        if tenant is None or tenant.status != "active" or not tenant.erp_site:
            return _result({"error": "not connected to a business; reconnect AI Munim"}, error=True)
        if name not in self.specs:
            return _result({"error": f"unknown tool {name}"}, error=True)
        site = tenant.erp_site
        engine = self.engine_for(site, tenant.company)
        sender = f"mcp:{tenant.phone}"  # its own conversation state, apart from the WhatsApp chat
        state = self.state_store.load(site, sender)
        state.can_submit = tenant.role != "staff"
        files: list[tuple[str, str, bytes]] = []  # PDFs to hand over with the result
        try:
            if name == "about_my_business":
                result = await self._about(tenant, engine, state)
            elif name == "get_document":
                doctype, doc_name = str(arguments.get("doctype")), str(arguments.get("name") or "").strip()
                details = await engine.erp.document_details(doctype, doc_name) if doctype in DETAIL_DOCTYPES else None
                result = details or {"error": f"{doctype} {doc_name} was not found in these books"}
            elif name == "finalise_draft":
                result = await self._finalise(tenant, engine, arguments, site, files)
            elif name == "get_upload_link":
                result = {"upload_url": self.upload_link(site, tenant.phone), "valid_minutes": 15,
                          "next": "ask the person to upload there, then call propose_purchase_bill"}
            elif name == "confirm_preview":
                result = await self._confirm(engine, state, arguments, site, files)
            elif name == "get_pdf":
                result = await self._pdf(engine, site, arguments, files)
            else:
                executor = ToolExecutor(
                    engine.erp, state, demo_label=getattr(engine.erp, "label", None),
                    aliases=(lambda k, a: self.state_store.alias_get(site, k, a), lambda k, a, v: self.state_store.alias_put(site, k, a, v)),
                )
                result = await executor.execute(name, arguments or {})
        finally:
            self.state_store.save(site, state)
        self.audit("mcp_tool", {"site": site, "role": tenant.role, "tool": name, "client": access.client_id if access else None,
                                "ok": "error" not in result})
        return _result(result, error="error" in result, files=files)

    async def _finalise(self, tenant: Tenant, engine: Any, arguments: dict[str, Any], site: str,
                        files: list[tuple[str, str, bytes]]) -> dict[str, Any]:
        if tenant.role == "staff":
            return {"error": "only the owner can finalise a draft"}
        doctype, name = str(arguments.get("doctype")), str(arguments.get("name") or "").strip()
        if doctype not in DETAIL_DOCTYPES:
            return {"error": f"doctype must be one of {', '.join(DETAIL_DOCTYPES)}"}
        details = await engine.erp.document_details(doctype, name)
        if details is None:
            return {"error": f"{doctype} {name} was not found in these books"}
        if details.get("status") != "Draft":
            return {"error": f"{name} is not a draft (status: {details.get('status')})"}
        try:
            await engine.erp.submit_document(doctype, name)
        except Exception as exc:  # noqa: BLE001 - ERPNext validation; it stays a draft
            return {"error": f"ERPNext did not allow it: {str(exc)[:200]}"}
        out: dict[str, Any] = {"done": [f"✅ {doctype} *{name}* is final."], "documents": [{"doc": name, "doctype": doctype, "submitted": True}]}
        await self._attach_pdfs(engine, site, out["documents"], files)
        return out

    async def _attach_pdfs(self, engine: Any, site: str, docs: list[dict[str, Any]], files: list[tuple[str, str, bytes]]) -> None:
        for doc in docs:
            if doc.get("doctype") not in PRINTABLE:
                continue
            doc["pdf_url"] = self.pdf_link(site, doc["doctype"], doc["doc"])
            content = await engine.erp.document_pdf(doc["doctype"], doc["doc"])
            if content and len(content) <= MAX_INLINE_PDF:
                files.append((doc["doctype"], doc["doc"], content))

    async def _about(self, tenant: Tenant, engine: Any, state: Any) -> dict[str, Any]:
        draft = state.draft
        return {"business": tenant.company or ("Demo business" if tenant.erp_site == DEMO_SITE else tenant.erp_site),
                "role": "owner" if tenant.role in {"owner", "tester"} else "staff",
                "writes": "final documents" if tenant.role != "staff" else "drafts for the owner to finalise",
                "today": engine.erp.today.isoformat(),
                "gst_registered": await engine.erp.tax_mode("") != "none",  # a walk-in sale tells us
                "open_preview": {"draft_id": draft.id, "version": draft.version, "kind": draft.kind} if draft else None}

    async def _pdf(self, engine: Any, site: str, arguments: dict[str, Any], files: list[tuple[str, str, bytes]]) -> dict[str, Any]:
        doctype, name = str(arguments.get("doctype") or ""), str(arguments.get("name") or "").strip()
        if doctype not in PRINTABLE or not name:
            return {"error": f"doctype must be one of {', '.join(PRINTABLE)}"}
        content = await engine.erp.document_pdf(doctype, name)
        if content is None:
            return {"error": f"{doctype} {name} was not found in these books"}
        if len(content) <= MAX_INLINE_PDF:
            files.append((doctype, name, content))
        return {"doctype": doctype, "name": name, "pdf_url": self.pdf_link(site, doctype, name), "valid_minutes": 15}

    async def list_resources(self) -> list[Any]:
        """The ChatGPT template stays readable but is not offered as an attachment."""
        return [r for r in await super().list_resources() if str(r.uri) != SKY_URI]

    async def _confirm(self, engine: Any, state: Any, arguments: dict[str, Any], site: str,
                       files: list[tuple[str, str, bytes]]) -> dict[str, Any]:
        draft = state.draft
        if draft is None:
            return {"error": "no preview is open; propose one first"}
        if draft.id != str(arguments.get("draft_id")) or draft.version != int(arguments.get("version") or 0):
            return {"error": "that preview changed or closed; show the latest version and ask again",
                    "open_preview": {"draft_id": draft.id, "version": draft.version}}
        out = await engine.approve(state)
        docs = [{k: v for k, v in (o.meta or {}).items() if k in {"doc", "doctype", "customer", "submitted", "total"}}
                for o in out if (o.meta or {}).get("doc")]
        await self._attach_pdfs(engine, site, docs, files)
        return {"done": [o.text for o in out if o.text], "documents": docs}


def _result(data: dict[str, Any], *, error: bool = False, files: list[tuple[str, str, bytes]] | None = None) -> CallToolResult:
    text = json.dumps(data, ensure_ascii=False, default=str)
    content: list[Any] = [TextContent(type="text", text=text)]
    for doctype, name, pdf in files or []:  # the PDF itself, for apps that show or read files
        content.append(EmbeddedResource(type="resource", resource=BlobResourceContents(
            uri=f"munim://pdf/{doctype.replace(' ', '-')}/{name}.pdf", mime_type="application/pdf", blob=base64.b64encode(pdf).decode())))
    return CallToolResult(content=content, structured_content=json.loads(text), is_error=error)


async def fetch_client_document(url: str, http: httpx.AsyncClient | None = None) -> dict[str, Any] | None:
    """A client's published OAuth metadata (CIMD). Public https hosts only, small, no redirects, client_id must match."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path in {"", "/"}:
        return None
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, 443)
        if not infos or any(not ipaddress.ip_address(info[4][0]).is_global for info in infos):
            return None  # never fetch internal addresses
        client = http or httpx.AsyncClient(timeout=5, follow_redirects=False)
        response = await client.get(url, headers={"Accept": "application/json"})
        if response.status_code != 200 or len(response.content) > 20_000:
            return None
        doc = response.json()
    except (OSError, ValueError, httpx.HTTPError):
        return None
    if not isinstance(doc, dict) or doc.get("client_id") != url or not isinstance(doc.get("redirect_uris"), list):
        return None
    return doc


CONNECT_SCRIPT = """<script>
const req=new URLSearchParams(location.search).get('req');const flow=document.getElementById('flow');
document.getElementById('go').onclick=async()=>{
  const r=await fetch('/mcp/connect/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({req})});
  const d=await r.json();if(d.error){flow.innerHTML='<p>'+d.error+'</p>';return;}
  flow.innerHTML=`<div class="qrbox"><img class="qr desk" src="${d.qr}" alt="Scan to send the code">
  <div><p class="desk"><b>WhatsApp on this computer?</b></p><div class="cta desk"><a class="btn" href="${d.web}" target="_blank" rel="noopener">WhatsApp Web ▸</a>
  <a class="btn ghost" href="${d.app}">WhatsApp app</a></div><a class="btn mob" href="${d.link}">Open WhatsApp ▸</a>
  <div class="code">LOGIN ${d.code}</div><div class="status" id="st"><span class="dots"><i></i><i></i><i></i></span> Waiting for your message</div></div></div>`;
  const tm=setInterval(async()=>{const s=await (await fetch('/mcp/connect/status?req='+encodeURIComponent(req))).json();
    if(s.redirect){clearInterval(tm);flow.innerHTML='<p class="ok">✓ CONNECTED</p><p>Going back…</p>';location.replace(s.redirect);}
    if(s.error){clearInterval(tm);document.getElementById('st').textContent=s.error;}},1500);
};
</script>"""


def mount_mcp(app: FastAPI, *, engine_for: Callable[[str, str | None], Any], store: StateStore, tenants: TenantStore,
              portal: PortalStore, bot_number: str, base_domain: str, db_path: Path,
              audit: Callable[[str, dict[str, Any]], None], page: Callable[..., HTMLResponse],
              secret: str = "", oauth_store: OAuthStore | None = None,
              media_dir: Path = Path(".run/v2/media")) -> Callable[[], Awaitable[None]]:
    """Mount /mcp, the OAuth endpoints and the WhatsApp connect page. Returns the startup hook to run."""
    base = f"https://{base_domain}"
    oauth = WhatsAppOAuthProvider(oauth_store or OAuthStore(db_path), tenants, base)
    link_key = (secret or secrets.token_hex(32)).encode()

    def sign(payload: list[Any]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"{body}.{hmac.new(link_key, body.encode(), hashlib.sha256).hexdigest()[:32]}"

    def unsign(token: str) -> list[Any] | None:
        body, _, sig = token.partition(".")
        if not hmac.compare_digest(hmac.new(link_key, body.encode(), hashlib.sha256).hexdigest()[:32], sig):
            return None
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))

    def pdf_link(site: str, doctype: str, name: str) -> str:
        """A signed link to one document of one business: 15 minutes, and a few minutes after its first open."""
        return f"{base}/pdf/{sign(['pdf', site, doctype, name, int(time.time()) + PDF_TTL, secrets.token_hex(4)])}"

    def upload_link(site: str, phone: str) -> str:
        return f"{base}/upload/{sign(['upload', site, phone, int(time.time()) + UPLOAD_TTL, secrets.token_hex(4)])}"

    apps = Apps()
    apps.add_html_resource(UI_URI, WIDGET, name="AI Munim card", title="AI Munim card",
                           description="Preview with Confirm / Cancel, and report tables", prefers_border=False)
    server = MunimMCP(
        extensions=[apps], icons=[Icon(src=f"{base}/munim-icon.svg", mime_type="image/svg+xml", sizes=["any"])],
        name="AI Munim", title="AI Munim — your WhatsApp munim's books", instructions=INSTRUCTIONS, website_url=base,
        auth_server_provider=oauth, engine_for=engine_for, pdf_link=pdf_link, upload_link=upload_link, store=store, tenants=tenants, audit=audit,
        auth=AuthSettings(issuer_url=base, resource_server_url=f"{base}/mcp", validate_token_resource=False,
                          client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE],
                                                                                default_scopes=[SCOPE]),
                          revocation_options=RevocationOptions(enabled=True), required_scopes=[SCOPE]),
    )

    server.add_resource(TextResource(uri=SKY_URI, name="AI Munim card (ChatGPT)", mime_type="text/html+skybridge", text=WIDGET))

    @app.get("/.well-known/oauth-authorization-server")
    async def authorization_server_metadata() -> JSONResponse:
        """The SDK's metadata plus CIMD support and public (no secret) clients."""
        meta = build_metadata(base, None, ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
                              RevocationOptions(enabled=True))
        meta.client_id_metadata_document_supported = True
        meta.token_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]
        return JSONResponse(meta.model_dump(mode="json", exclude_none=True))

    @app.get("/pdf/{token}")
    async def pdf(token: str) -> Response:
        data = unsign(token)
        if not data or data[0] != "pdf":
            return Response("link not valid", status_code=403)
        _, site, doctype, name, expires, _nonce = data
        if time.time() > expires or time.time() - oauth.store.first_use(token) > PDF_REUSE:
            return Response("link expired — ask for the PDF again", status_code=410)
        company = next((m.company for m in tenants.members(site) if m.company), None)
        content = await engine_for(site, company).erp.document_pdf(doctype, name)
        if content is None:
            return Response("not found", status_code=404)
        return Response(content, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{name}.pdf"', "Cache-Control": "private, no-store"})

    @app.get("/upload/{token}", response_class=HTMLResponse)
    async def upload_page(token: str) -> Response:
        data = unsign(token)
        if not data or data[0] != "upload" or time.time() > data[3] or oauth.store.get("code", f"used:{token}"):
            return page('<section class="wrap"><div class="frame card"><h2 class="px">Link expired</h2>'
                        '<p>Ask your AI for a new upload link.</p></div></section>')
        return page(f'''<section class="wrap"><div class="frame card">{pixel.svg(pixel.BILL, px=6)}
<h2 class="px">Upload the bill</h2><p>Photo or PDF of the supplier's bill (up to 10 MB). It will be attached to the
purchase entry in your books.</p><input type="file" id="f" accept="image/*,application/pdf"><div class="cta" style="margin-top:14px">
<button class="btn" id="go">Upload ▸</button></div><p id="out"></p></div></section>
<script>document.getElementById('go').onclick=async()=>{{const f=document.getElementById('f').files[0];if(!f)return;
const out=document.getElementById('out');out.textContent='Uploading…';
const r=await fetch(location.pathname,{{method:'POST',headers:{{'Content-Type':f.type||'application/octet-stream','X-Filename':encodeURIComponent(f.name)}},body:f}});
out.innerHTML=r.ok?'<span class="ok">✓ UPLOADED</span> — go back to your AI chat.':'Upload failed: '+await r.text();}};</script>''')

    @app.post("/upload/{token}")
    async def upload(token: str, request: Request) -> Response:
        from munim.core.media import sniff
        from munim.core.state import DocumentContext

        data = unsign(token)
        if not data or data[0] != "upload" or time.time() > data[3]:
            return Response("link expired", status_code=410)
        if oauth.store.get("code", f"used:{token}"):
            return Response("already used", status_code=410)
        content = await request.body()
        kind = sniff(content) if content else ""
        if len(content) > MAX_UPLOAD or kind not in {"pdf", "jpeg", "png", "webp"}:
            return Response("only a photo or PDF up to 10 MB", status_code=400)
        _, site, phone, _exp, nonce = data
        from urllib.parse import unquote

        name = unquote(request.headers.get("x-filename") or f"bill.{kind}")[:80]
        folder = media_dir / "mcp" / phone
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{int(time.time())}-{nonce}.{'pdf' if kind == 'pdf' else 'jpg' if kind == 'jpeg' else kind}"
        path.write_bytes(content)
        state = store.load(site, f"mcp:{phone}")
        state.document = DocumentContext(str(path), "application/pdf" if kind == "pdf" else f"image/{kind}", name, time.time())
        store.save(site, state)
        oauth.store.put("code", f"used:{token}", {"at": time.time()})
        return Response("ok")

    @app.get("/munim-icon.svg")
    async def icon() -> Response:
        return Response(pixel.svg(pixel.MUNIM, px=8, title="AI Munim"), media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/mcp/connect", response_class=HTMLResponse)
    async def connect_page(req: str = "") -> Response:
        pending = oauth.store.get("pending", req)
        if pending is None:
            return page('<section class="wrap"><div class="frame card"><h2 class="px">Link expired</h2>'
                        '<p>Start "Connect" again from your AI app.</p></div></section>')
        who = pending.get("client_name") or "Your AI app"
        body = (f'<section class="wrap"><div class="frame card" id="open">{pixel.svg(pixel.LOCK, px=6)}'
                f'<h2 class="px">Connect {_esc(who)}</h2><p><b>{_esc(who)}</b> wants to use your AI Munim books: read reports and '
                'prepare bills that you confirm in the chat. Verify with the WhatsApp number of your business.</p>'
                '<div id="flow"><button class="btn" id="go">Verify on WhatsApp ▸</button></div></div></section>')
        return page(body + CONNECT_SCRIPT)

    @app.post("/mcp/connect/start")
    async def connect_start(request: Request) -> JSONResponse:
        if not (request.headers.get("origin") or request.headers.get("referer") or "").startswith(base):
            return JSONResponse({"error": "origin"}, status_code=403)
        req = str((await request.json()).get("req") or "")
        pending = oauth.store.get("pending", req)
        if pending is None:
            return JSONResponse({"error": "This link expired. Start Connect again from your AI app."})
        sid, code = portal.start_login()
        oauth.store.put("pending", req, {**pending, "sid": sid})
        message = f"LOGIN {code}"
        link = wa_link(bot_number, message)
        return JSONResponse({"code": code, "link": link, "qr": qr_data_uri(link), "web": wa_web_link(bot_number, message),
                             "app": wa_app_link(bot_number, message)})

    @app.get("/mcp/connect/status")
    async def connect_status(req: str = "") -> JSONResponse:
        pending = oauth.store.get("pending", req)
        if pending is None or not pending.get("sid"):
            return JSONResponse({"error": "Expired — start Connect again."})
        phone = portal.login_phone(pending["sid"])
        if phone is None:
            return JSONResponse({"waiting": True} if portal.login_pending(pending["sid"]) else {"error": "Code expired — start again."})
        tenant = tenants.get(phone)
        if tenant.status != "active" or not tenant.erp_site:
            return JSONResponse({"error": t("mcp.no_business", tenant.data.get("lang"))})
        return JSONResponse({"redirect": oauth.issue_code(req, phone)})

    mcp_app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=[base_domain, f"{base_domain}:443", "testserver"],
                                                     allowed_origins=[base, "https://claude.ai", "https://chatgpt.com"]),
    )
    app.mount("/", mcp_app)  # last: /mcp, /authorize, /token, /register, /revoke, /.well-known/...

    async def start() -> None:
        from contextlib import AsyncExitStack

        stack = AsyncExitStack()
        await stack.enter_async_context(server.session_manager.run())
        app.state.mcp_stack = stack

    return start


def _esc(value: str) -> str:
    import html

    return html.escape(value)[:80]
