"""Tools that change what is already in the books: returns, cancel, correct, repeat, discount."""
from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from typing import Any

from munim.core.drafts import (
    DOCTYPE_KIND,
)
from munim.core.erp import ConversionError, money
from munim.core.state import Draft
from munim.core.tools_common import (
    CANCELLABLE,
    REPEAT_FREQUENCIES,
    SALES_KINDS,
    SOURCE_DOCTYPES,
    ToolInputError,
    _decimal,
    _next_date,
)


class CorrectionTools:
    """Tools that change what is already in the books: returns, cancel, correct, repeat, discount."""

    async def _t_propose_return(self, invoice: str) -> dict[str, Any]:
        try:
            mapped = await self.erp.map_return(invoice.strip())
        except ConversionError as exc:
            raise ToolInputError(f"ERPNext: {exc}") from exc
        built = await self._lines_from(mapped, "source")
        note = self._replaced_note()
        self.state.draft = Draft(kind="credit_note", customer_id=mapped.customer_id, customer_name=mapped.customer_name, lines=built,
                                 source={"doctype": "Sales Invoice", "name": mapped.source_name})
        return {**await self._publish(), **note, "returnable_only": True}

    async def _t_propose_cancel(self, doctype: str, name: str) -> dict[str, Any]:
        if not self.state.can_submit:
            raise ToolInputError("only the owner can cancel documents")
        if doctype not in CANCELLABLE:
            raise ToolInputError(f"doctype must be one of {', '.join(CANCELLABLE)}")
        try:
            mapped = await self.erp.document_lines(doctype, name.strip()) if doctype in SOURCE_DOCTYPES else None
        except ConversionError as exc:
            raise ToolInputError(f"ERPNext: {exc}") from exc
        note = self._replaced_note()
        self.state.draft = Draft(kind="cancel", customer_name=mapped.customer_name if mapped else None,
                                 source={"doctype": doctype, "name": name.strip()})
        return {**await self._publish(), **note}

    async def _t_propose_correction(self, doctype: str, name: str) -> dict[str, Any]:
        if not self.state.can_submit:
            raise ToolInputError("only the owner can correct a final document")
        kind = DOCTYPE_KIND.get(doctype)
        if kind is None:
            raise ToolInputError("doctype must be Quotation, Sales Order, Delivery Note or Sales Invoice")
        try:
            mapped = await self.erp.document_lines(doctype, name.strip())
        except ConversionError as exc:
            raise ToolInputError(f"ERPNext: {exc}") from exc
        built = await self._lines_from(mapped, "source")
        note = self._replaced_note()
        self.state.draft = Draft(kind=kind, customer_id=mapped.customer_id, customer_name=mapped.customer_name, lines=built,
                                 amend={"doctype": doctype, "name": mapped.source_name})
        return {**await self._publish(), **note, "next": "apply the user's change with edit_draft"}

    async def _t_propose_repeat(self, invoice: str, frequency: str = "Monthly", start_date: str | None = None) -> dict[str, Any]:
        if frequency not in REPEAT_FREQUENCIES:
            raise ToolInputError(f"frequency must be one of {', '.join(REPEAT_FREQUENCIES)}")
        try:
            mapped = await self.erp.document_lines("Sales Invoice", invoice.strip())
        except ConversionError as exc:
            raise ToolInputError(f"ERPNext: {exc}") from exc
        today = getattr(self.erp, "today", None) or date.today()
        try:
            start = date.fromisoformat(start_date) if start_date else today
        except ValueError as exc:
            raise ToolInputError("start_date must be YYYY-MM-DD") from exc
        if start < today:
            raise ToolInputError("start_date can't be in the past")
        note = self._replaced_note()
        self.state.draft = Draft(kind="repeat", customer_id=mapped.customer_id, customer_name=mapped.customer_name,
                                 source={"doctype": "Sales Invoice", "name": mapped.source_name}, note=frequency,
                                 bill_date=_next_date(start, frequency).isoformat(),
                                 payment_amount=str(sum((x.qty * x.rate for x in mapped.lines), Decimal(0))))
        return {**await self._publish(), **note}

    async def _t_apply_discount(self, percent: Any, item_codes: list[str] | None = None) -> dict[str, Any]:
        draft = self.state.draft
        if draft is None or draft.kind not in (*SALES_KINDS, "purchase"):
            raise ToolInputError("no open bill/quotation/order/challan preview")
        pct = _decimal(percent, "percent")
        if pct >= 100:
            raise ToolInputError("percent must be below 100")
        codes = set(item_codes or [line.item_code for line in draft.lines])
        for line in draft.lines:
            if line.item_code in codes:  # rate x (100 - p) / 100, rounded the ERPNext way; the base is the current rate
                line.rate = str(money(Decimal(line.rate) * (100 - pct) / 100))
                line.rate_source = "discount"
        draft.discount = f"{pct.normalize():f}"
        draft.version += 1
        draft.created_at = time.time()
        return await self._publish()
