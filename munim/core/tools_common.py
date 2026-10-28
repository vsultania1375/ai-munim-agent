"""What the agent's tools share: limits, input errors, turn effects and small helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from munim.core.state import PendingQuestion

SALES_KINDS = ("quotation", "sales_order", "invoice", "delivery_note")


SOURCE_DOCTYPES = ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice")


BAHI_TYPES = ("sale", "udhaar", "received")


REPEAT_FREQUENCIES = ("Daily", "Weekly", "Monthly", "Quarterly", "Half-yearly", "Yearly")


CANCELLABLE = (*SOURCE_DOCTYPES, "Payment Entry", "Purchase Invoice", "Journal Entry")


RATE_SOURCES = ("price_list", "user_stated", "document")


PAYMENT_MODES = ("Cash", "UPI", "Bank Transfer", "Cheque")


MAX_QTY = Decimal(100000)


USUAL_BILLS = 8  # how far back "the usual" looks


GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


_LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "item_code": {"type": "string", "description": "item_code from find_item"},
        "qty": {"type": "number"},
        "rate": {"type": "number", "description": "Only when rate_source is user_stated or document. Omit it to keep "
                 "the rate of a line that came from a source document."},
        "rate_source": {"type": "string", "enum": list(RATE_SOURCES)},
    },
    "required": ["item_code", "qty", "rate_source"],
}


class ToolInputError(ValueError):
    pass


@dataclass
class TurnEffects:
    """What the engine must send after the agent finishes this turn."""

    card: str | None = None  # preview text for Approve/Edit/Cancel buttons
    question: PendingQuestion | None = None
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)  # things asked for that no tool can do yet
    messages: list[str] = field(default_factory=list)  # fixed text sent as is (links the model must not retype)


def _decimal(value: Any, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ToolInputError(f"{name} must be a number")
    if not number.is_finite() or number <= 0:
        raise ToolInputError(f"{name} must be greater than 0")
    return number


def _next_date(start: date, frequency: str) -> date:
    """When ERPNext makes the first copy: one period after the start (end of month clamps)."""
    import calendar
    from datetime import timedelta

    if frequency == "Daily":
        return start + timedelta(days=1)
    if frequency == "Weekly":
        return start + timedelta(days=7)
    months = {"Monthly": 1, "Quarterly": 3, "Half-yearly": 6, "Yearly": 12}[frequency]
    total = start.month - 1 + months
    year, month = start.year + total // 12, total % 12 + 1
    return start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))


def _pct(value: Decimal) -> str | None:
    return f"{value.normalize():f}" if value else None
