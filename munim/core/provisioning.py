"""Give a new business its own ERPNext site from a pre-created pool and configure it.

Pool sites are created by infra/v2/make_pool.sh (empty, setup wizard not run). Each pool
file holds the site's Administrator API key; it never leaves the VM.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from munim.core.gstin import check_gstin

log = logging.getLogger(__name__)
GST_RATES = (5, 12, 18, 28)


@dataclass(frozen=True)
class SiteCreds:
    site: str
    api_key: str
    api_secret: str


class ProvisioningError(RuntimeError):
    pass


def load_pool(pool_dir: Path) -> dict[str, SiteCreds]:
    pool: dict[str, SiteCreds] = {}
    if not pool_dir.exists():
        return pool
    for path in sorted(pool_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        pool[data["site"]] = SiteCreds(data["site"], data["api_key"], data["api_secret"])
    return pool


def company_abbr(name: str) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
    abbr = "".join(w[0] for w in words).upper()[:5]
    return abbr if len(abbr) >= 2 else (name[:3].upper() or "BIZ")


def fiscal_year(today: date) -> tuple[str, str]:
    start_year = today.year if today.month >= 4 else today.year - 1
    return f"{start_year}-04-01", f"{start_year + 1}-03-31"


class SiteAPI:
    """Minimal Administrator REST client for one site (setup only)."""

    def __init__(self, base_url: str, creds: SiteCreds, http: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.creds = creds
        self.http = http or httpx.AsyncClient(timeout=120)
        self.headers = {"Authorization": f"token {creds.api_key}:{creds.api_secret}", "Host": creds.site}

    def _url(self, path: str) -> str:
        # document names carry "%" and spaces ("GST 18% - ABC"); encode everything but the slashes
        return f"{self.base_url}{quote(path, safe='/')}"

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        r = await self.http.get(self._url(path), headers=self.headers, params=params)
        r.raise_for_status()
        return r.json()

    async def post(self, path: str, *, json_body: dict | None = None, data: dict | None = None) -> dict[str, Any]:
        r = await self.http.post(self._url(path), headers=self.headers, json=json_body, data=data)
        if r.status_code >= 400:
            raise ProvisioningError(f"{path} -> {r.status_code}: {_server_message(r)}")
        return r.json()

    async def put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = await self.http.put(self._url(path), headers=self.headers, json=body)
        if r.status_code >= 400:
            raise ProvisioningError(f"{path} -> {r.status_code}: {_server_message(r)}")
        return r.json()

    async def exists(self, doctype: str, name: str) -> bool:
        r = await self.http.get(self._url(f"/api/resource/{doctype}/{name}"), headers=self.headers)
        return r.status_code == 200

    async def insert(self, doctype: str, doc: dict[str, Any]) -> dict[str, Any]:
        return (await self.post(f"/api/resource/{doctype}", json_body=doc))["data"]

    async def first(self, doctype: str, filters: dict[str, Any], field: str = "name") -> str | None:
        data = await self.get(
            f"/api/resource/{doctype}", filters=json.dumps(filters), fields=json.dumps([field]), limit_page_length=1
        )
        rows = data.get("data") or []
        return rows[0][field] if rows else None


def _server_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    messages = body.get("_server_messages")
    if messages:
        try:
            return json.loads(json.loads(messages)[0]).get("message", "")[:200]
        except (ValueError, KeyError, IndexError, TypeError):
            return str(messages)[:200]
    return str(body.get("exception") or body)[:200]


class Provisioner:
    def __init__(self, base_url: str, pool: dict[str, SiteCreds], *, hook_secret: str = "",
                 bot_url: str = "http://aimunim-bot-1:8800") -> None:
        self.base_url = base_url
        self.pool = pool
        self.hook_secret = hook_secret  # ERPNext webhooks carry a per-site token derived from it
        self.bot_url = bot_url.rstrip("/")  # reachable from the ERPNext containers (same docker network)

    def refresh(self, pool_dir: Path) -> None:
        """Pick up sites added by make_pool.sh without restarting (updates the dict in place)."""
        self.pool.update(load_pool(pool_dir))

    def api(self, site: str) -> SiteAPI:
        return SiteAPI(self.base_url, self.pool[site])

    async def setup_business(self, site: str, profile: dict[str, Any]) -> dict[str, str]:
        """Run the setup wizard and add GST masters. Idempotent per site."""
        api = self.api(site)
        company = (profile.get("trade_name") or profile.get("legal_name") or "My Business").strip()[:60]
        abbr = company_abbr(company)
        settings = await api.get("/api/resource/System Settings/System Settings")
        if not settings["data"].get("setup_complete"):
            start, end = fiscal_year(date.today())
            args = {
                "language": "English",
                "country": "India",
                "timezone": "Asia/Kolkata",
                "currency": "INR",
                "full_name": profile.get("owner_name") or "Owner",
                "email": f"owner@{site}.aimunim",
                "company_name": company,
                "company_abbr": abbr,
                "chart_of_accounts": "Standard",
                "fy_start_date": start,
                "fy_end_date": end,
                "setup_demo": 0,
            }
            await api.post(
                "/api/method/frappe.desk.page.setup_wizard.setup_wizard.setup_complete", data={"args": json.dumps(args)}
            )
            for _ in range(60):  # some versions finish the wizard in a background job
                settings = await api.get("/api/resource/System Settings/System Settings")
                if settings["data"].get("setup_complete"):
                    break
                await asyncio.sleep(3)
            else:
                raise ProvisioningError("setup wizard did not finish")
        company_name = await api.first("Company", {}, "name") or company
        abbr = (await api.get(f"/api/resource/Company/{company_name}"))["data"]["abbr"]
        await self._gst_masters(api, company_name, abbr, profile)
        await self._basics(api, company_name, abbr)
        await self.automation_setup(api, site)
        return {"site": site, "company": company_name, "abbr": abbr}

    async def _gst_masters(self, api: SiteAPI, company: str, abbr: str, profile: dict[str, Any]) -> None:
        """India Compliance GST: settings, the company's GSTIN + address (only if registered), item tax
        templates with CGST/SGST/IGST rows, and IC's In-state template as the default. Idempotent."""
        await api.put("/api/resource/GST Settings/GST Settings", {
            "enable_api": 0, "autofill_party_info": 0,  # no paid GST API; nothing leaves the server
            "enable_e_waybill": 0, "validate_hsn_code": 1, "min_hsn_digits": "4",  # 4 digits: turnover up to Rs 5 Cr
        })
        info, _ = check_gstin(profile.get("gstin") or "")
        if info is not None:
            await self._register_company(api, company, info, profile)
        await self._item_tax_templates(api, company, abbr)
        legacy = f"AI Munim GST - {abbr}"  # pre-India-Compliance template: keep for old documents, never default
        if await api.exists("Sales Taxes and Charges Template", legacy):
            await api.put(f"/api/resource/Sales Taxes and Charges Template/{legacy}", {"is_default": 0, "disabled": 1})
        in_state = f"Output GST In-state - {abbr}"
        if await api.exists("Sales Taxes and Charges Template", in_state):
            await api.put(f"/api/resource/Sales Taxes and Charges Template/{in_state}", {"is_default": 1})

    async def _register_company(self, api: SiteAPI, company: str, info: Any, profile: dict[str, Any]) -> None:
        await api.put(f"/api/resource/Company/{company}",
                      {"gstin": info.gstin, "tax_id": info.gstin, "gst_category": "Registered Regular"})
        linked = [["Dynamic Link", "link_doctype", "=", "Company"], ["Dynamic Link", "link_name", "=", company]]
        address = await api.first("Address", linked)
        pin = re.search(r"\b(\d{6})\b", profile.get("address") or "")
        body = {"address_title": company, "address_type": "Billing",
                "address_line1": (profile.get("address") or profile.get("city") or info.state)[:140],
                "city": profile.get("city") or info.state, "state": info.state, "country": "India",
                "gstin": info.gstin, "gst_category": "Registered Regular", "is_your_company_address": 1}
        if pin:
            body["pincode"] = pin.group(1)
        if address:
            await api.put(f"/api/resource/Address/{address}", {k: body[k] for k in ("gstin", "gst_category", "state")})
        else:
            await api.insert("Address", {**body, "links": [{"link_doctype": "Company", "link_name": company}]})

    async def _item_tax_templates(self, api: SiteAPI, company: str, abbr: str) -> None:
        """GST 5/12/18/28 templates shaped like India Compliance's (output, input, RCM rows per component)."""
        async def listed() -> list[dict[str, Any]]:
            data = await api.get("/api/resource/Item Tax Template", filters=json.dumps({"company": company}),
                                 fields=json.dumps(["name", "gst_rate"]), limit_page_length=50)
            return data.get("data") or []
        templates = await listed()
        if not any(float(t.get("gst_rate") or 0) > 0 for t in templates):
            await api.post("/api/method/india_compliance.gst_india.overrides.company.make_default_tax_templates",
                           json_body={"company": company})
            templates = await listed()
        reference = next((t for t in templates if float(t.get("gst_rate") or 0) > 0), None)
        if reference is not None:
            ref_rate = float(reference["gst_rate"])
            ref_rows = (await api.get(f"/api/resource/Item Tax Template/{reference['name']}"))["data"]["taxes"]
        else:  # our pre-IC templates took every name, so IC made none: build the rows from IC's GST accounts
            ref_rate, ref_rows = 1.0, await self._gst_account_rows(api, company)
        have = {t["name"]: float(t.get("gst_rate") or 0) for t in templates}
        for rate in GST_RATES:
            name = f"GST {rate}% - {abbr}"
            if have.get(name) == rate:
                continue
            body = {"gst_rate": rate, "gst_treatment": "Taxable",
                    "taxes": [{"tax_type": r["tax_type"], "tax_rate": float(r["tax_rate"]) * rate / ref_rate} for r in ref_rows]}
            if name in have:
                await api.put(f"/api/resource/Item Tax Template/{name}", body)
            else:
                await api.insert("Item Tax Template", {"title": f"GST {rate}%", "company": company, **body})

    @staticmethod
    async def _gst_account_rows(api: SiteAPI, company: str) -> list[dict[str, Any]]:
        """Rows for a 1% template: output and input CGST/SGST at half, IGST at full."""
        settings = (await api.get("/api/resource/GST Settings/GST Settings"))["data"]
        rows = []
        for acc in settings.get("gst_accounts") or []:
            if acc.get("company") != company or acc.get("account_type") not in {"Output", "Input"}:
                continue
            rows += [{"tax_type": acc["cgst_account"], "tax_rate": 0.5}, {"tax_type": acc["sgst_account"], "tax_rate": 0.5},
                     {"tax_type": acc["igst_account"], "tax_rate": 1.0}]
        if not rows:
            raise ProvisioningError("India Compliance GST accounts are missing")
        return rows

    async def apply_brand(self, site: str, brand: dict, details: dict) -> None:
        """This business's bill design as the default print format of its sales documents."""
        from munim.core.brand import BRANDED_DOCTYPES, render_print_format

        api = self.api(site)
        html_text = render_print_format(brand, details)
        for doctype, module in BRANDED_DOCTYPES.items():
            name = f"AI Munim {doctype}"
            body = {"doc_type": doctype, "module": module, "standard": "No", "custom_format": 1, "print_format_type": "Jinja",
                    "html": html_text, "disabled": 0, "font_size": 11, "margin_top": 12, "margin_bottom": 12,
                    "margin_left": 12, "margin_right": 12}
            if await api.exists("Print Format", name):
                await api.put(f"/api/resource/Print Format/{name}", body)
            else:
                await api.insert("Print Format", {"name": name, **body})
            setter = f"{doctype}-main-default_print_format"
            if await api.exists("Property Setter", setter):
                await api.put(f"/api/resource/Property Setter/{setter}", {"value": name})
            else:
                await api.insert("Property Setter", {"doctype_or_field": "DocType", "doc_type": doctype,
                                                     "property": "default_print_format", "value": name, "property_type": "Data"})

    async def brand_of(self, site: str) -> dict | None:
        from munim.core.brand import brand_from_print_format

        api = self.api(site)
        if not await api.exists("Print Format", "AI Munim Sales Invoice"):
            return None
        return brand_from_print_format((await api.get("/api/resource/Print Format/AI Munim Sales Invoice"))["data"].get("html"))

    async def automation_setup(self, api: SiteAPI, site: str) -> None:
        """Recurring bills on Sales Invoice, and webhooks that tell the bot about documents finalised in the web ERP."""
        if not await api.exists("Property Setter", "Sales Invoice-main-allow_auto_repeat"):
            await api.insert("Property Setter", {"doctype_or_field": "DocType", "doc_type": "Sales Invoice",
                                                 "property": "allow_auto_repeat", "value": "1", "property_type": "Check"})
        if not self.hook_secret:
            return
        from munim.core.automation import WATCHED, hook_token

        body_json = ('{"doctype": "{{ doc.doctype }}", "name": "{{ doc.name }}", '
                     '"party": {{ (doc.customer_name or doc.supplier_name or doc.party_name or "") | tojson }}, '
                     '"total": "{{ doc.grand_total or doc.paid_amount or 0 }}", "auto_repeat": "{{ doc.auto_repeat or "" }}"}')
        for doctype in WATCHED:
            name = f"AI Munim {doctype}"
            hook = {"webhook_doctype": doctype, "webhook_docevent": "on_submit", "enabled": 1,
                    "request_url": f"{self.bot_url}/erp/hook/{site}", "request_method": "POST", "request_structure": "JSON",
                    "webhook_json": body_json, "timeout": 5,
                    "webhook_headers": [{"key": "X-Munim-Token", "value": hook_token(self.hook_secret, site)}]}
            if await api.exists("Webhook", name):
                await api.put(f"/api/resource/Webhook/{name}", hook)
            else:
                await api.insert("Webhook", {"name": name, **hook})

    async def _basics(self, api: SiteAPI, company: str, abbr: str) -> None:
        bank_parent = await api.first("Account", {"company": company, "account_type": "Bank", "is_group": 1})
        if bank_parent and not await api.exists("Account", f"Bank - {abbr}"):
            await api.insert("Account", {"account_name": "Bank", "parent_account": bank_parent, "company": company,
                                         "account_type": "Bank"})
        for uom in ("Bag", "Kg", "Tonne", "Nos", "Box", "Litre", "Meter"):
            if not await api.exists("UOM", uom):
                await api.insert("UOM", {"uom_name": uom})
        await api.http.put(
            f"{api.base_url}/api/resource/Global Defaults/Global Defaults", headers=api.headers,
            json={"disable_rounded_total": 1},
        )
