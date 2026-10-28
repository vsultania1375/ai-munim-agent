"""Tools for how the business runs: bill design, standing discounts, bahi import, memory, online payments, requests."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from munim.core.erp import ConversionError, money
from munim.core.state import Draft
from munim.core.tools_common import BAHI_TYPES, USUAL_BILLS, ToolInputError, _decimal


class BusinessTools:
    """Tools for how the business runs: bill design, standing discounts, bahi import, memory, online payments, requests."""

    async def _t_set_customer_discount(self, customer_id: str, percent: Any) -> dict[str, Any]:
        if not self.state.can_submit:
            raise ToolInputError("only the owner can set a permanent discount")
        customer = await self.erp.get_customer(customer_id)
        if customer is None:
            raise ToolInputError("unknown customer_id; call find_customer first")
        pct = Decimal(str(percent)) if percent not in (None, "") else Decimal(-1)
        if not pct.is_finite() or pct < 0 or pct >= 50:
            raise ToolInputError("percent must be 0 (switch off) up to 50")
        current = await self.erp.customer_discount(customer.id)
        note = self._replaced_note()
        self.state.draft = Draft(kind="pricing", customer_id=customer.id, customer_name=customer.name,
                                 payment_amount=str(pct), note=str(current))
        return {**await self._publish(), **note}

    async def _t_propose_bahi_import(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Entries copied from a bahi page; per-party dues are summed here, never by the model."""
        if not self.state.can_submit:
            raise ToolInputError("only the owner can book old dues")
        if not entries or len(entries) > 80:
            raise ToolInputError("give 1-80 entries copied from the page")
        parties: dict[str, dict[str, Any]] = {}
        skipped = 0
        for raw in entries:
            name = str(raw.get("party") or "").strip()
            kind = raw.get("type")
            if not name or kind not in BAHI_TYPES:
                skipped += 1
                continue
            amount = money(_decimal(raw.get("amount"), "amount"))
            party = parties.setdefault(name.lower(), {"name": name, "given": Decimal(0), "received": Decimal(0), "lines": 0})
            party["given" if kind in {"sale", "udhaar"} else "received"] += amount
            party["lines"] += 1
        rows = []
        for party in parties.values():
            match = await self.erp.find_customer(party["name"])
            found = match.candidates[0] if match.status == "resolved" else None
            rows.append({"party": party["name"], "customer_id": found["customer_id"] if found else None,
                         "customer": found["name"] if found else party["name"], "lines": party["lines"],
                         "given": str(party["given"]), "received": str(party["received"]),
                         "due": str(money(party["given"] - party["received"]))})
        if not rows:
            raise ToolInputError("no usable entries (each needs party, type sale/udhaar/received and amount)")
        note = self._replaced_note()
        self.state.draft = Draft(kind="bahi", entries=rows, note=str(skipped))
        result = {**await self._publish(), **note}
        ambiguous = [r["party"] for r in rows if r["customer_id"] is None]
        if ambiguous:
            result["new_customers_on_approve"] = ambiguous
        return result

    async def _t_set_brand(self, use_uploaded_logo: bool = False, bank_name: str | None = None, account_name: str | None = None,
                           account_no: str | None = None, ifsc: str | None = None, upi_id: str | None = None,
                           tagline: str | None = None, use_uploaded_signature: bool = False, terms: str | None = None,
                           footer: str | None = None, remove_logo: bool = False, remove_signature: bool = False) -> dict[str, Any]:
        from munim.core import brand as branding

        if not self.state.can_submit:
            raise ToolInputError("only the owner can change the bill design")
        brand_get = getattr(self, "brand_get", None)
        if brand_get is None:
            raise ToolInputError("bill design is not available for this business yet")
        current = await brand_get() or {}
        details, problems = branding.clean_bank({"bank_name": bank_name, "account_name": account_name, "account_no": account_no,
                                                 "ifsc": ifsc, "upi_id": upi_id, "tagline": tagline, "terms": terms,
                                                 "footer": footer})
        if problems:
            raise ToolInputError("; ".join(problems))
        details = {**(current.get("details") or {}), **details}
        doc = self.state.document
        image = doc if doc is not None and doc.mime.startswith("image/") else None
        if (use_uploaded_logo or use_uploaded_signature) and image is None:
            raise ToolInputError("no image uploaded in this chat yet; ask the person to send the picture first")
        if use_uploaded_logo and use_uploaded_signature:
            raise ToolInputError("one picture is either the logo or the signature; ask which")
        if use_uploaded_logo:
            palette = branding.from_logo(Path(doc.media_path).read_bytes()).as_dict()  # colours by code, not the model
        elif current:
            palette = {k: v for k, v in current.items() if k != "details"}
        else:
            palette = branding.neutral().as_dict()
        palette.setdefault("signature_png_b64", current.get("signature_png_b64"))
        if use_uploaded_signature:
            try:
                palette["signature_png_b64"] = branding.signature_from_image(Path(image.media_path).read_bytes())
            except ValueError as exc:
                raise ToolInputError(f"{exc}; ask for a clearer photo on plain paper") from exc
        if remove_signature:
            palette["signature_png_b64"] = None
        if remove_logo:
            palette["logo_png_b64"] = None
        note = self._replaced_note()
        self.state.draft = Draft(kind="brand", brand={**palette, "details": details})
        return {**await self._publish(), **note}

    async def _t_remember(self, fact: str, about: str | None = None) -> dict[str, Any]:
        if not self.state.can_submit:
            raise ToolInputError("only the owner can add to the business memory")
        memory = getattr(self, "memory", None)
        if memory is None:
            raise ToolInputError("memory is not available here")
        fact = " ".join((fact or "").split())[:200]
        if len(fact) < 4:
            raise ToolInputError("fact is empty")
        return {"remembered": True, "memory_id": memory[1](fact, (about or "").strip()[:80] or None)}

    async def _t_forget(self, memory_id: Any) -> dict[str, Any]:
        if not self.state.can_submit:
            raise ToolInputError("only the owner can change the business memory")
        memory = getattr(self, "memory", None)
        try:
            ok = memory is not None and memory[2](int(memory_id))
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise ToolInputError("no such memory id; see business_memory in <state>")
        return {"forgotten": True}

    async def _t_customer_usual(self, customer_id: str) -> dict[str, Any]:
        customer = await self.erp.get_customer(customer_id)
        if customer is None:
            raise ToolInputError("unknown customer_id; call find_customer first")
        docs = [d for d in await self.erp.recent_documents(customer_id, limit=20)
                if d.get("status") == "submitted" or d.get("docstatus") == 1]
        bills = [d for d in docs if d["doctype"] == "Sales Invoice"] or [d for d in docs if d["doctype"] == "Sales Order"]
        bills = bills[:USUAL_BILLS]  # newest first; invoices only, so order -> challan -> bill isn't counted three times
        seen: dict[str, dict[str, Any]] = {}
        counted = []
        for doc in bills:
            try:
                lines = (await self.erp.document_lines(doc["doctype"], doc["name"])).lines
            except ConversionError:
                continue
            counted.append(doc)
            for line in lines:
                row = seen.setdefault(line.item_code, {"qtys": [], "last_rate": str(line.rate), "last_date": doc.get("date")})
                row["qtys"].append(line.qty)
        if not seen:
            return {"customer": customer.name, "bills_seen": 0, "usual": [], "note": "no final bills yet"}
        bills = counted
        need = 1 if len(bills) < 3 else 2  # with a few bills, anything bought twice is a habit
        usual = []
        for code, row in sorted(seen.items(), key=lambda kv: -len(kv[1]["qtys"])):
            if len(row["qtys"]) < need:
                continue
            item = await self.erp.get_item(code)
            usual.append({"item_code": code, "item_name": item.name if item else code, "usual_qty": format(Decimal(median(row["qtys"])).normalize(), "f"),
                          "in_bills": f"{len(row['qtys'])} of {len(bills)}", "last_rate": row["last_rate"], "last_date": row["last_date"]})
        dates = sorted({date.fromisoformat(str(d["date"])[:10]) for d in bills if d.get("date")})
        gap = round((dates[-1] - dates[0]).days / (len(dates) - 1)) if len(dates) > 1 else None
        return {"customer": customer.name, "bills_seen": len(bills), "usual": usual[:12], "buys_every_days": gap,
                "last_bill": str(dates[-1]) if dates else None}

    async def _t_online_payments(self, action: str) -> dict[str, Any]:
        if not self.state.can_submit:
            raise ToolInputError("only the owner can manage online payments")
        payments = getattr(self, "payments", None)
        if payments is None:
            raise ToolInputError("online payments are not available here")
        if action not in ("status", "on", "off", "connect"):
            raise ToolInputError("action is status, on, off or connect")
        result = dict(await payments(action))
        if "error" in result:
            raise ToolInputError(result["error"])
        if "send" in result:
            self.effects.messages.append(result.pop("send"))
            result["setup_link_sent"] = "sent as a separate message; do not repeat the link"
        return result

    async def _t_note_request(self, summary: str) -> dict[str, Any]:
        text_ = (summary or "").strip()[:300]
        if not text_:
            raise ToolInputError("summary is empty")
        self.effects.requests.append(text_)
        return {"noted": True, "say": "noted for the team; offer the closest thing that works today"}
