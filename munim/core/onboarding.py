"""WhatsApp onboarding for new businesses: identity -> confirm -> type -> own ERPNext -> old data.

Deterministic, button-driven state machine. The LLM is used only to read details out of a
photo/PDF or free text, and to map unknown spreadsheet columns.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from munim.core.engine import Inbound, Outbound, text
from munim.core.gstin import check_gstin, find_gstin
from munim.core.guards import BudgetExceeded
from munim.core.i18n import detect_lang, t
from munim.core.llm import AgentLLM
from munim.core.media import prepare_any
from munim.core.migration import ImportPlan, plan_from_file, plan_from_rows, run_import
from munim.core.provisioning import Provisioner
from munim.core.tenants import Tenant, TenantStore

log = logging.getLogger(__name__)
Notify = Callable[[Outbound], Awaitable[None]]

SPREADSHEET_MIMES = {
    "text/csv", "text/comma-separated-values", "application/csv", "text/plain",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
SETUP_WORDS = {"setup", "set up", "shuru", "onboard", "onboarding", "account banao", "start setup", "register"}
DEMO_WORDS = {"demo", "demo dikhao", "pehle demo"}

BUSINESS_TYPES = [
    ("hardware", "Hardware / Building", "Cement, saria, paint, sanitary"),
    ("electrical", "Electrical / CCTV", "Wire, switch, CCTV, inverter"),
    ("kirana", "Kirana / FMCG", "Grocery, distributor, wholesale"),
    ("medical", "Medical / Pharma", "Chemist, surgical, distributor"),
    ("other", "", ""),  # labelled per language in _type_list
]

RECORD_BUSINESS = {
    "name": "record_business",
    "description": "Record business details found in the message or image. Leave unknown fields out. Do not guess.",
    "input_schema": {
        "type": "object",
        "properties": {
            "legal_name": {"type": "string"},
            "trade_name": {"type": "string", "description": "Shop/brand name"},
            "gstin": {"type": "string"},
            "city": {"type": "string"},
            "state": {"type": "string"},
            "address": {"type": "string"},
            "owner_name": {"type": "string"},
            "phone": {"type": "string"},
        },
    },
}
MAP_COLUMNS = {
    "name": "map_columns",
    "description": "Map spreadsheet headers to fields. Use exact header text. Omit fields that are absent.",
    "input_schema": {
        "type": "object",
        "properties": {k: {"type": "string"} for k in ("customer", "item", "phone", "gstin", "balance", "rate", "uom", "gst", "hsn")},
    },
}
RECORD_ROWS = {
    "name": "record_rows",
    "description": "Copy customer/item rows exactly as written in the image (a register, rate list or ledger). No arithmetic.",
    "input_schema": {
        "type": "object",
        "properties": {
            "customers": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "phone": {"type": "string"}, "balance": {"type": "string"}}, "required": ["name"]}},
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "rate": {"type": "string"}, "unit": {"type": "string"}, "gst": {"type": "string"}},
                "required": ["name"]}},
        },
    },
}


def _btn(id_: str, title: str) -> dict[str, str]:
    return {"id": id_, "title": title[:20]}


def buttons(body: str, *items: tuple[str, str]) -> Outbound:
    return Outbound("buttons", body[:1024], buttons=[_btn(i, t) for i, t in items])


class Onboarding:
    def __init__(
        self,
        *,
        tenants: TenantStore,
        provisioner: Provisioner | None,
        llm: AgentLLM,
        on_activated: Callable[[Tenant], None] | None = None,
        pool_dir: Path | None = None,
        max_new_sites_per_day: int = 3,
    ) -> None:
        self.max_new_sites_per_day = max_new_sites_per_day
        self.tenants = tenants
        self.provisioner = provisioner
        self.llm = llm
        self.on_activated = on_activated
        self.pool_dir = pool_dir

    # ---- routing helpers ---------------------------------------------
    @staticmethod
    def wants_setup(msg: Inbound) -> bool:
        return msg.kind == "text" and (msg.text or "").strip().lower() in SETUP_WORDS

    @staticmethod
    def is_migration(msg: Inbound) -> bool:
        if msg.kind == "button" and (msg.button_id or "").startswith("mg:"):
            return True
        return msg.kind == "document" and _is_spreadsheet(msg)

    # ---- entry -------------------------------------------------------
    async def handle(self, msg: Inbound, tenant: Tenant, notify: Notify, profile_name: str | None = None) -> list[Outbound]:
        if profile_name and not tenant.data.get("owner_name"):
            tenant.data["owner_name"] = profile_name
        if msg.text:
            tenant.data["lang"] = detect_lang(msg.text, tenant.data.get("lang"))
        try:
            out = await self._route(msg, tenant, notify)
        except BudgetExceeded:
            out = [text(t("busy", tenant.data.get("lang")))]
        except Exception:
            log.exception("onboarding failed step=%s", tenant.step)
            lang = tenant.data.get("lang")
            out = [buttons(t("ob.error", lang), ("ob:retry", t("ob.btn_retry", lang)))]
        self.tenants.save(tenant)
        return out

    async def _route(self, msg: Inbound, tenant: Tenant, notify: Notify) -> list[Outbound]:
        bid = msg.button_id or ""
        lowered = (msg.text or "").strip().lower()
        lang = tenant.data.get("lang")
        if tenant.status == "new":
            tenant.status, tenant.step = "onboarding", "welcome"
            gstin = find_gstin(msg.text or "")
            if gstin:  # they led with a GSTIN: skip straight ahead
                return await self._identity_from_gstin(tenant, gstin)
            return [self._welcome(tenant)]
        if bid == "ob:demo" or lowered in DEMO_WORDS:
            tenant.status = "demo"
            return [buttons(t("ob.demo_on", lang), ("ob:start", t("ob.btn_own_setup", lang)))]
        if bid in {"ob:start", "ob:retry"} and tenant.step in {"welcome", "done"}:
            tenant.status, tenant.step = "onboarding", "identity"
            return [self._ask_identity(lang)]
        if bid == "ob:retry" and tenant.step == "provisioning":
            return await self._provision(tenant, notify)
        if tenant.status == "demo":  # "setup" typed from demo mode
            tenant.status, tenant.step = "onboarding", "identity"
            return [self._ask_identity(lang)]

        step = tenant.step
        if step == "welcome":
            gstin = find_gstin(msg.text or "")
            if gstin:
                return await self._identity_from_gstin(tenant, gstin)
            return [self._welcome(tenant)]
        if step in {"identity", "name", "edit"}:
            return await self._identity(msg, tenant)
        if step == "confirm":
            if bid == "ob:ok":
                tenant.step = "btype"
                return [self._type_list(lang)]
            if bid == "ob:edit":
                tenant.step = "edit"
                return [text(t("ob.edit_prompt", lang))]
            return await self._identity(msg, tenant)  # free text here = a correction
        if step == "btype":
            chosen = next((bt for bt in BUSINESS_TYPES if bid == f"bt:{bt[0]}"), None)
            if chosen is None:
                return [self._type_list(lang)]
            tenant.data["business_type"] = chosen[0]
            tenant.step = "provisioning"
            return await self._provision(tenant, notify)
        if step == "provisioning":
            return [text(t("ob.busy", lang))]
        if step == "migrate":
            return await self.migration(msg, tenant)
        return [self._welcome(tenant)]

    def _welcome(self, tenant: Tenant) -> Outbound:
        lang = tenant.data.get("lang")
        name = tenant.data.get("owner_name")
        honorific = " ji" if lang == "hi" else ""
        body = t("ob.welcome", lang, name=f" {name.split()[0]}{honorific}" if name else "")
        return buttons(body, ("ob:start", t("ob.btn_start", lang)), ("ob:demo", t("ob.btn_demo", lang)))

    @staticmethod
    def _ask_identity(lang: str | None) -> Outbound:
        return buttons(t("ob.ask_identity", lang), ("ob:nogst", t("ob.btn_nogst", lang)))

    @staticmethod
    def _type_list(lang: str | None) -> Outbound:
        rows = []
        for key, title, desc in BUSINESS_TYPES:
            if key == "other":
                title, desc = t("ob.type_other", lang), t("ob.type_other_desc", lang)
            rows.append({"id": f"bt:{key}", "title": title[:24], "description": desc[:72]})
        return Outbound("list", t("ob.type_question", lang), buttons=rows)

    # ---- identity ----------------------------------------------------
    async def _identity_from_gstin(self, tenant: Tenant, gstin: str) -> list[Outbound]:
        lang = tenant.data.get("lang")
        info, reason = check_gstin(gstin, lang)
        if info is None:
            tenant.step = "identity"
            return [buttons(t("ob.gstin_retry", lang, reason=reason), ("ob:nogst", t("ob.btn_nogst", lang)))]
        profile = tenant.data.setdefault("profile", {})
        profile.update({"gstin": info.gstin, "state": info.state, "pan": info.pan, "holder": info.holder_type})
        if not (profile.get("trade_name") or profile.get("legal_name")):
            tenant.step = "name"
            return [text(t("ob.gstin_ok_need_name", lang, state=info.state, holder=info.holder_type))]
        return [self._confirm(tenant)]

    async def _identity(self, msg: Inbound, tenant: Tenant) -> list[Outbound]:
        lang = tenant.data.get("lang")
        if msg.button_id == "ob:nogst":
            tenant.step = "name"
            tenant.data.setdefault("profile", {})["gstin"] = None
            return [text(t("ob.no_gst_ok", lang))]
        gstin = find_gstin(msg.text or "")
        if gstin and msg.kind == "text" and len((msg.text or "").split()) <= 3:
            return await self._identity_from_gstin(tenant, gstin)
        if msg.kind not in {"text", "document", "image"}:
            return [self._ask_identity(lang)]
        if msg.kind == "document" and _is_spreadsheet(msg):
            return [text(t("ob.file_later", lang))]
        found = await self._extract_business(msg)
        if not found:
            return [buttons(t("ob.nothing_found", lang), ("ob:nogst", t("ob.btn_nogst", lang)))]
        profile = tenant.data.setdefault("profile", {})
        if found.get("gstin"):
            info, reason = check_gstin(found["gstin"], lang)
            if info:
                profile.update({"gstin": info.gstin, "state": info.state, "pan": info.pan, "holder": info.holder_type})
            else:
                found.pop("gstin")
                profile["gstin_note"] = reason
        for key in ("legal_name", "trade_name", "city", "address", "owner_name"):
            if found.get(key):
                profile[key] = found[key].strip()[:120]
        if found.get("state") and not profile.get("state"):
            profile["state"] = found["state"]
        if not (profile.get("trade_name") or profile.get("legal_name")):
            tenant.step = "name"
            return [text(t("ob.need_name", lang))]
        return [self._confirm(tenant)]

    def _confirm(self, tenant: Tenant) -> Outbound:
        tenant.step = "confirm"
        lang = tenant.data.get("lang")
        p = tenant.data.get("profile", {})
        lines = [t("ob.confirm_head", lang), ""]
        lines.append(f"🏪 {p.get('trade_name') or p.get('legal_name')}")
        if p.get("legal_name") and p.get("trade_name") and p["legal_name"] != p["trade_name"]:
            lines.append(f"📄 Legal: {p['legal_name']}")
        lines.append(f"🧾 GSTIN: {p['gstin']}" if p.get("gstin") else t("ob.no_gstin", lang))
        place = ", ".join(x for x in (p.get("city"), p.get("state")) if x)
        if place:
            lines.append(f"📍 {place}")
        if p.get("gstin_note"):
            lines.append(t("ob.photo_gstin_bad", lang, reason=p["gstin_note"]))
        return buttons("\n".join(lines), ("ob:ok", t("ob.btn_ok", lang)), ("ob:edit", t("ob.btn_edit", lang)))

    async def _extract_business(self, msg: Inbound) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        async def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
            if name == "record_business":
                captured.update({k: v for k, v in args.items() if isinstance(v, str) and v.strip()})
            return {"ok": True}

        content: list[dict[str, Any]] = []
        if msg.media:
            prepared = await asyncio.to_thread(prepare_any, msg.media)
            if prepared is not None:
                content.extend(prepared.blocks)
        content.append({"type": "text", "text": f"Message: {msg.text or '(image only)'}\nCall record_business with what you can read."})
        await self.llm.run_turn(
            system="You extract Indian business details for account setup. Use only what is visible. Call record_business once.",
            messages=[{"role": "user", "content": content}],
            tools=[RECORD_BUSINESS],
            execute=execute,
        )
        return captured

    # ---- workspace ---------------------------------------------------
    async def _provision(self, tenant: Tenant, notify: Notify) -> list[Outbound]:
        lang = tenant.data.get("lang")
        if self.provisioner is not None and self.pool_dir is not None:
            self.provisioner.refresh(self.pool_dir)
        if self.provisioner is None or not self.provisioner.pool:
            tenant.status = "demo"
            tenant.data["waitlist"] = True
            log.warning("no provisioner/pool configured")
            return [text(t("ob.waitlist", lang))]
        if self.tenants.claims_today() >= self.max_new_sites_per_day and not self.tenants.has_claim(tenant.phone):
            tenant.status = "demo"
            log.warning("daily new-site cap reached")
            return [text(t("ob.pool_full", lang))]
        site = self.tenants.claim_site(tenant.phone, [x for x in self.provisioner.pool if x != "frontend"])
        if site is None:
            tenant.status = "demo"
            tenant.data["waitlist"] = True
            log.warning("tenant pool exhausted")
            return [text(t("ob.pool_full", lang))]
        await notify(text(t("ob.creating", lang)))
        result = await self.provisioner.setup_business(site, tenant.data.get("profile", {}))
        tenant.site, tenant.company = result["site"], result["company"]
        tenant.data["abbr"] = result["abbr"]
        tenant.step = "migrate"
        return [buttons(
            t("ob.ready", lang, company=tenant.company),
            ("mg:file", t("mg.btn_file", lang)), ("mg:tally", t("mg.btn_tally", lang)), ("mg:skip", t("mg.btn_skip", lang)),
        )]

    # ---- migration (also reachable after onboarding) -----------------
    async def migration(self, msg: Inbound, tenant: Tenant) -> list[Outbound]:
        bid = msg.button_id or ""
        if msg.text:
            tenant.data["lang"] = detect_lang(msg.text, tenant.data.get("lang"))
        lang = tenant.data.get("lang")
        if bid == "mg:file":
            return [text(t("mg.send_file", lang))]
        if bid == "mg:tally":
            return [text(t("mg.tally_help", lang))]
        if bid in {"mg:skip", "mg:done"}:
            return [self._finish(tenant)]
        if bid == "mg:cancel":
            tenant.data.pop("plan", None)
            return [buttons(t("mg.cancelled", lang), ("mg:file", t("mg.btn_other_file", lang)), ("mg:done", t("mg.btn_done", lang)))]
        if bid == "mg:import":
            return await self._run_import(tenant)
        if msg.kind == "document" and _is_spreadsheet(msg):
            plan = plan_from_file(msg.media or b"", msg.filename or "data.csv")
            if plan.empty:
                plan = await self._plan_with_llm_mapping(msg)
            return self._preview(tenant, plan)
        if msg.kind in {"image", "document"} and msg.media:
            plan = await self._plan_from_image(msg)
            return self._preview(tenant, plan)
        if tenant.step == "migrate":
            return [buttons(t("mg.nudge", lang), ("mg:file", t("mg.btn_file", lang)), ("mg:skip", t("mg.btn_start", lang)))]
        return [text(t("mg.send_any", lang))]

    def _preview(self, tenant: Tenant, plan: ImportPlan) -> list[Outbound]:
        lang = tenant.data.get("lang")
        if plan.empty:
            return [buttons(t("mg.not_understood", lang), ("mg:tally", t("mg.btn_how_export", lang)), ("mg:skip", t("mg.btn_skip", lang)))]
        tenant.data["plan"] = plan.to_dict()
        body = t("mg.preview_head", lang, source=plan.source) + "\n\n" + plan.summary(lang)
        if plan.balances:
            body += t("mg.balances_note", lang)
        return [buttons(body, ("mg:import", t("mg.btn_import", lang)), ("mg:cancel", t("mg.btn_cancel", lang)))]

    async def _run_import(self, tenant: Tenant) -> list[Outbound]:
        lang = tenant.data.get("lang")
        raw = tenant.data.get("plan")
        if not raw or not tenant.site or self.provisioner is None:
            return [text(t("mg.nothing_pending", lang))]
        plan = ImportPlan.from_dict(raw)
        result = await run_import(self.provisioner.api(tenant.site), plan, company=tenant.company or "",
                                  abbr=tenant.data.get("abbr", ""))
        tenant.data.pop("plan", None)
        lines = [t("mg.done_head", lang)]
        if result["customers"]:
            lines.append(f"👥 {result['customers']} customers")
        if result["items"]:
            lines.append(f"📦 {result['items']} items")
        if result["opening_invoices"]:
            lines.append(t("mg.done_balances", lang, n=result["opening_invoices"], total=f"{result['opening_total']:,.0f}"))
        if result["failed"]:
            lines.append(t("mg.failed_rows", lang, n=result["failed"]))
        if tenant.status == "active":
            return [text("\n".join(lines))]
        return [buttons("\n".join(lines), ("mg:file", t("mg.btn_more", lang)), ("mg:done", t("mg.btn_done", lang)))]

    async def _plan_with_llm_mapping(self, msg: Inbound) -> ImportPlan:
        import tempfile
        from pathlib import Path

        from munim.sheets import load_table

        suffix = ".xlsx" if (msg.filename or "").lower().endswith(".xlsx") else ".csv"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"u{suffix}"
            path.write_bytes(msg.media or b"")
            table = load_table(path, sample_rows=2000)
        if not table.headers:
            return ImportPlan(source=msg.filename or "file")
        mapping: dict[str, str] = {}

        async def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
            mapping.update({k: v for k, v in args.items() if v in table.headers})
            return {"ok": True}

        sample = "\n".join(str(row) for row in table.rows[:3])
        await self.llm.run_turn(
            system="Map spreadsheet columns for an Indian small-business data import. Call map_columns once.",
            messages=[{"role": "user", "content": f"Headers: {list(table.headers)}\nSample rows:\n{sample}"}],
            tools=[MAP_COLUMNS],
            execute=execute,
        )
        return plan_from_rows(list(table.rows), mapping, source=msg.filename or "file")

    async def _plan_from_image(self, msg: Inbound) -> ImportPlan:
        captured: dict[str, Any] = {}
        prepared = await asyncio.to_thread(prepare_any, msg.media or b"")
        if prepared is None:
            return ImportPlan(source="photo")

        async def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
            captured.update(args)
            return {"ok": True}

        await self.llm.run_turn(
            system="You copy customer and item rows from photos of registers or rate lists. Call record_rows once.",
            messages=[{"role": "user", "content": [*prepared.blocks, {"type": "text", "text": "Copy the rows."}]}],
            tools=[RECORD_ROWS],
            execute=execute,
        )
        rows: list[dict[str, str]] = []
        for c in captured.get("customers") or []:
            rows.append({"customer": c.get("name", ""), "phone": c.get("phone", ""), "balance": c.get("balance", "")})
        plan = plan_from_rows(rows, {"customer": "customer", "phone": "phone", "balance": "balance"}, source="photo")
        item_rows = [{"item": i.get("name", ""), "rate": i.get("rate", ""), "uom": i.get("unit", ""), "gst": i.get("gst", "")}
                     for i in captured.get("items") or []]
        items = plan_from_rows(item_rows, {"item": "item", "rate": "rate", "uom": "uom", "gst": "gst"}, source="photo")
        plan.items, plan.items_without_gst = items.items, items.items_without_gst
        return plan

    def _finish(self, tenant: Tenant) -> Outbound:
        tenant.status, tenant.step = "active", "done"
        tenant.data.pop("plan", None)
        if self.on_activated:
            self.on_activated(tenant)
        return text(t("ob.live", tenant.data.get("lang"), company=tenant.company))


def _is_spreadsheet(msg: Inbound) -> bool:
    mime = (msg.mime or "").split(";")[0].lower()
    name = (msg.filename or "").lower()
    return mime in SPREADSHEET_MIMES and (name.endswith((".csv", ".xlsx")) or mime != "text/plain") or name.endswith((".csv", ".xlsx"))

