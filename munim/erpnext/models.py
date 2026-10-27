from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class DocumentRef(BaseModel):
    doctype: str
    name: str


class ERPDocument(BaseModel):
    doctype: str
    name: str | None = None
    docstatus: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class MoneyView(BaseModel):
    label: str
    value: str
    currency: str = "INR"


class PayrollComponent(BaseModel):
    employee: str
    component: str
    expected: Decimal
    actual: Decimal

    @property
    def delta(self) -> Decimal:
        return self.actual - self.expected
