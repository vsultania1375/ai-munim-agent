"""Tools for the buying side: suppliers, purchase bills, expenses, paying suppliers, goods sent back."""
from __future__ import annotations

from datetime import date
from typing import Any

from munim.core.erp import ConversionError, DraftLine, money
from munim.core.state import Draft, DraftLineState
from munim.core.tools_common import GSTIN_RE, PAYMENT_MODES, ToolInputError, _decimal


class PurchaseTools:
    """Tools for the buying side: suppliers, purchase bills, expenses, paying suppliers, goods sent back."""

    async def _t_find_supplier(self, name: str) -> dict[str, Any]:
        match = await self.erp.find_supplier(name)
        return {"status": match.status, "candidates": list(match.candidates)}

    async def _t_propose_new_supplier(self, name: str, phone: str | None = None, gstin: str | None = None) -> dict[str, Any]:
        name = (name or "").strip()
        if len(name) < 2:
            raise ToolInputError("name too short")
        existing = await self.erp.find_supplier(name)
        if existing.status == "resolved":
            return {"error": "supplier already exists", "existing": list(existing.candidates)}
        if gstin:
            gstin = gstin.strip().upper()
            if not GSTIN_RE.match(gstin):
                raise ToolInputError("GSTIN format invalid (15 characters)")
        note = self._replaced_note()
        self.state.draft = Draft(kind="new_supplier", new_customer={"name": name, "phone": phone, "gstin": gstin})
        return {**await self._publish(), **note}

    async def _t_propose_purchase_bill(self, supplier_id: str, lines: list[dict[str, Any]], bill_no: str | None = None,
                                       bill_date: str | None = None, attach_uploaded_document: bool = True) -> dict[str, Any]:
        supplier = await self.erp.get_supplier(supplier_id)
        if supplier is None:
            raise ToolInputError("unknown supplier_id; call find_supplier first")
        if any(raw.get("rate_source") == "price_list" for raw in lines):
            raise ToolInputError("a purchase uses the rate on the supplier's bill (rate_source document) or the one the user said")
        built = await self._build_lines(lines)
        if bill_date:
            try:
                bill_date = date.fromisoformat(bill_date).isoformat()
            except ValueError as exc:
                raise ToolInputError("bill_date must be YYYY-MM-DD") from exc
        document = self.state.document if attach_uploaded_document else None
        note = self._replaced_note()
        self.state.draft = Draft(kind="purchase", customer_id=supplier.id, customer_name=supplier.name, lines=built,
                                 bill_no=(bill_no or "").strip()[:60] or None, bill_date=bill_date,
                                 attachment=document.media_path if document else None)
        return {**await self._publish(), **note}

    async def _t_propose_expense(self, account: str, amount: Any, mode: str, note: str | None = None) -> dict[str, Any]:
        heads = await self.erp.expense_accounts()
        wanted = (account or "").split(" - ")[0].strip().lower()
        head = next((h for h in heads if h.lower() == wanted), None)
        if head is None:
            raise ToolInputError("account must be one of: " + ", ".join(heads))
        if mode not in PAYMENT_MODES:
            raise ToolInputError(f"mode must be one of {', '.join(PAYMENT_MODES)}")
        value = money(_decimal(amount, "amount"))
        note_text = self._replaced_note()
        self.state.draft = Draft(kind="expense", payment_amount=str(value), payment_mode=mode, expense_account=head,
                                 note=(note or "").strip()[:120] or None)
        return {**await self._publish(), **note_text}

    async def _t_propose_supplier_payment(self, supplier_id: str, amount: Any, mode: str) -> dict[str, Any]:
        supplier = await self.erp.get_supplier(supplier_id)
        if supplier is None:
            raise ToolInputError("unknown supplier_id; call find_supplier first")
        if mode not in PAYMENT_MODES:
            raise ToolInputError(f"mode must be one of {', '.join(PAYMENT_MODES)}")
        value = money(_decimal(amount, "amount"))
        bills = sorted(await self.erp.supplier_outstanding(supplier.id), key=lambda i: (i.due_date, i.posting_date))
        allocations, left = [], value
        for bill in bills:  # oldest bills first; all arithmetic in code
            if left <= 0:
                break
            take = min(left, bill.outstanding)
            allocations.append({"invoice": bill.name, "amount": str(take)})
            left = money(left - take)
        note = self._replaced_note()
        self.state.draft = Draft(kind="supplier_payment", customer_id=supplier.id, customer_name=supplier.name, payment_amount=str(value),
                                 payment_mode=mode, allocations=allocations)
        return {**await self._publish(), **note}

    async def _t_propose_purchase_return(self, invoice: str, lines: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        try:
            mapped = await self.erp.map_purchase_return(invoice.strip())
        except ConversionError as exc:
            raise ToolInputError(f"ERPNext: {exc}") from exc
        returnable = {x.item_code: x for x in mapped.lines}
        chosen = list(mapped.lines)
        if lines:
            chosen = []
            for raw in lines:
                code = str(raw.get("item_code", ""))
                if code not in returnable:
                    raise ToolInputError(f"{code} is not on {mapped.source_name} or already fully returned")
                qty = _decimal(raw.get("qty"), "qty")
                if qty > returnable[code].qty:
                    raise ToolInputError(f"only {returnable[code].qty} of {code} can still go back on {mapped.source_name}")
                chosen.append(DraftLine(code, qty, returnable[code].rate))
        built = []
        for line in chosen:
            item = await self.erp.get_item(line.item_code)
            if item is None:
                raise ToolInputError(f"unknown item {line.item_code}")
            built.append(DraftLineState(item.code, item.name, item.uom, str(line.qty), str(money(line.rate)), "source"))
        note = self._replaced_note()
        self.state.draft = Draft(kind="debit_note", customer_id=mapped.customer_id, customer_name=mapped.customer_name, lines=built,
                                 source={"doctype": "Purchase Invoice", "name": mapped.source_name})
        return {**await self._publish(), **note}
