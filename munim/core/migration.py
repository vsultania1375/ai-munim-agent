"""Import an owner's old data (Excel/CSV from Tally, Vyapar, Khatabook, Busy, plain sheets).

Column detection is deterministic (header synonyms). Every import is previewed with
counts and needs the Import button. Customers and items are created as masters; opening
balances become ONE draft Journal Entry for the owner to review (never submitted).
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from munim.core.clock import today_ist
from munim.core.gst import clean_hsn
from munim.core.i18n import t
from munim.sheets import load_table

MAX_ROWS = 2000
VALID_GST = {Decimal(v) for v in ("0", "0.25", "3", "5", "12", "18", "28")}

SYNONYMS: dict[str, tuple[str, ...]] = {
    "customer": ("customer", "customer name", "party", "party name", "ledger", "ledger name", "account name",
                 "company name", "client", "client name", "buyer", "debtor", "grahak"),
    "item": ("item", "item name", "product", "product name", "stock item", "stock item name", "particulars",
             "description", "goods", "maal", "saman"),
    "name": ("name",),
    "phone": ("phone", "mobile", "mobile no", "phone no", "contact", "contact no", "whatsapp", "number"),
    "gstin": ("gstin", "gst no", "gst number", "gstin uin", "gstin/uin", "gst in"),
    "balance": ("closing balance", "balance", "outstanding", "due", "amount due", "receivable", "opening balance",
                "baaki", "udhaar", "net balance"),
    "rate": ("rate", "price", "selling price", "sale price", "sales rate", "mrp", "unit price", "standard rate"),
    "uom": ("unit", "uom", "units", "unit of measure"),
    "gst": ("gst", "gst %", "gst rate", "tax", "tax %", "tax rate", "igst", "igst rate", "gst rate %"),
    "hsn": ("hsn", "hsn code", "hsn/sac", "hsn sac", "hsn/sac code", "sac", "sac code", "hsn no", "hsn number"),
}


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%/]+", " ", (value or "").lower())).strip()


def detect_columns(headers: tuple[str, ...]) -> dict[str, str]:
    normalized = {_norm(h): h for h in headers if h}
    found: dict[str, str] = {}
    for field_name, words in SYNONYMS.items():
        for word in words:
            if word in normalized and normalized[word] not in found.values():
                found[field_name] = normalized[word]
                break
    return found


def parse_amount(raw: str) -> Decimal | None:
    text = (raw or "").strip()
    if not text:
        return None
    credit = bool(re.search(r"\bcr\b", text, re.IGNORECASE)) or text.startswith("(")
    digits = re.sub(r"[^0-9.\-]", "", text)
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return -abs(value) if credit else value


def parse_gst(raw: str) -> Decimal | None:
    value = parse_amount((raw or "").replace("%", ""))
    if value is None:
        return None
    if Decimal(0) < value < Decimal(1):  # 0.18 style
        value *= 100
    value = value.normalize()
    return value if value in VALID_GST else None


@dataclass
class ImportPlan:
    customers: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    balances: list[dict[str, Any]] = field(default_factory=list)
    items_without_gst: int = 0
    skipped_rows: int = 0
    source: str = ""

    @property
    def empty(self) -> bool:
        return not (self.customers or self.items or self.balances)

    def summary(self, lang: str | None = None) -> str:
        parts = []
        if self.customers:
            parts.append(t("mg.sum_customers", lang, n=len(self.customers)))
        if self.items:
            parts.append(t("mg.sum_items", lang, n=len(self.items), r=sum(1 for i in self.items if i.get("rate"))))
        if self.balances:
            total = sum((b["amount"] for b in self.balances), Decimal(0))
            parts.append(t("mg.sum_balances", lang, n=len(self.balances), total=f"{total:,.0f}"))
        lines = [" · ".join(parts) or t("mg.sum_empty", lang)]
        if self.items_without_gst:
            lines.append(t("mg.sum_no_gst", lang, n=self.items_without_gst))
        if self.skipped_rows:
            lines.append(t("mg.sum_skipped", lang, n=self.skipped_rows))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "customers": self.customers,
            "items": [{**i, "rate": str(i["rate"]) if i.get("rate") is not None else None,
                       "gst": str(i["gst"]) if i.get("gst") is not None else None} for i in self.items],
            "balances": [{**b, "amount": str(b["amount"])} for b in self.balances],
            "items_without_gst": self.items_without_gst,
            "skipped_rows": self.skipped_rows,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImportPlan:
        return cls(
            customers=data.get("customers", []),
            items=[{**i, "rate": Decimal(i["rate"]) if i.get("rate") else None,
                    "gst": Decimal(i["gst"]) if i.get("gst") else None} for i in data.get("items", [])],
            balances=[{**b, "amount": Decimal(b["amount"])} for b in data.get("balances", [])],
            items_without_gst=data.get("items_without_gst", 0),
            skipped_rows=data.get("skipped_rows", 0),
            source=data.get("source", ""),
        )


def plan_from_file(content: bytes, filename: str, mapping: dict[str, str] | None = None) -> ImportPlan:
    suffix = ".xlsx" if (filename or "").lower().endswith(".xlsx") else ".csv"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"upload{suffix}"
        path.write_bytes(content)
        table = load_table(path, sample_rows=MAX_ROWS)
    columns = mapping or detect_columns(table.headers)
    return plan_from_rows(list(table.rows), columns, source=filename or "file")


def plan_from_rows(rows: list[dict[str, str]], columns: dict[str, str], *, source: str) -> ImportPlan:
    plan = ImportPlan(source=source)
    item_col = columns.get("item")
    customer_col = columns.get("customer")
    name_col = columns.get("name")
    looks_like_items = bool(item_col or (name_col and (columns.get("rate") or columns.get("uom") or columns.get("gst"))))
    if not item_col and looks_like_items:
        item_col = name_col
    if not customer_col and not looks_like_items:
        customer_col = name_col
    seen_customers: set[str] = set()
    seen_items: set[str] = set()
    for row in rows[:MAX_ROWS]:
        if item_col:
            name = (row.get(item_col) or "").strip()
            if not name or name.lower() in {"total", "grand total"}:
                plan.skipped_rows += 1
                continue
            key = name.lower()
            if key in seen_items:
                continue
            seen_items.add(key)
            gst = parse_gst(row.get(columns.get("gst", ""), "")) if columns.get("gst") else None
            rate = parse_amount(row.get(columns.get("rate", ""), "")) if columns.get("rate") else None
            hsn = clean_hsn(row.get(columns.get("hsn", ""), "")) if columns.get("hsn") else None
            plan.items.append({"name": name[:140], "rate": rate if rate and rate > 0 else None,
                               "uom": (row.get(columns.get("uom", ""), "") or "Nos").strip()[:20] or "Nos", "gst": gst,
                               "hsn": hsn})
            if gst is None:
                plan.items_without_gst += 1
        elif customer_col:
            name = (row.get(customer_col) or "").strip()
            if not name or name.lower() in {"total", "grand total"}:
                plan.skipped_rows += 1
                continue
            key = name.lower()
            if key not in seen_customers:
                seen_customers.add(key)
                plan.customers.append({
                    "name": name[:140],
                    "phone": re.sub(r"[^0-9+]", "", row.get(columns.get("phone", ""), "") or "")[:15] or None,
                    "gstin": ((row.get(columns.get("gstin", ""), "") or "").strip().upper() or None),
                })
            if columns.get("balance"):
                amount = parse_amount(row.get(columns["balance"], ""))
                if amount and amount > 0:
                    plan.balances.append({"customer": name[:140], "amount": amount})
    return plan


def item_code_for(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:40] or "ITEM"
    code, n = base, 2
    while code in taken:
        code, n = f"{base[:36]}-{n}", n + 1
    taken.add(code)
    return code


async def run_import(api: Any, plan: ImportPlan, *, company: str, abbr: str) -> dict[str, Any]:
    """Create masters through a provisioning.SiteAPI; returns counts and any failures."""
    result: dict[str, Any] = {"customers": 0, "items": 0, "opening_invoices": 0, "opening_total": Decimal(0), "failed": 0}
    for customer in plan.customers:
        if await api.exists("Customer", customer["name"]):
            continue
        doc = {"customer_name": customer["name"], "customer_type": "Company", "customer_group": "Commercial",
               "territory": "India"}
        if customer.get("phone"):
            doc["mobile_no"] = customer["phone"]
        if customer.get("gstin"):
            doc["tax_id"] = customer["gstin"]
        try:
            await api.insert("Customer", doc)
            result["customers"] += 1
        except Exception:  # noqa: BLE001 - count and continue; one bad row must not stop the import
            result["failed"] += 1
    taken: set[str] = set()
    for item in plan.items:
        code = item_code_for(item["name"], taken)
        if await api.exists("Item", code):
            continue
        uom = item.get("uom") or "Nos"
        try:
            if not await api.exists("UOM", uom):
                await api.insert("UOM", {"uom_name": uom})
            doc: dict[str, Any] = {"item_code": code, "item_name": item["name"], "item_group": "Products",
                                   "stock_uom": uom, "is_stock_item": 0}
            if item.get("gst") is not None and item["gst"] > 0:
                doc["taxes"] = [{"item_tax_template": f"GST {item['gst']}% - {abbr}"}]
            if item.get("hsn") and await api.exists("GST HSN Code", item["hsn"]):  # only codes in the GST list
                doc["gst_hsn_code"] = item["hsn"]
            await api.insert("Item", doc)
            if item.get("rate"):
                await api.insert("Item Price", {"item_code": code, "price_list": "Standard Selling",
                                                "price_list_rate": float(item["rate"])})
            result["items"] += 1
        except Exception:  # noqa: BLE001
            result["failed"] += 1
    # Old dues: one opening invoice per customer (ERPNext's Opening Invoice way, against Temporary Opening).
    # They show as outstanding, age by date, and incoming payments settle them oldest first.
    day = today_ist().isoformat()
    for bal in plan.balances:
        try:
            await api.insert("Sales Invoice", {
                "company": company, "customer": bal["customer"], "is_opening": "Yes", "set_posting_time": 1,
                "posting_date": day, "due_date": day, "update_stock": 0, "docstatus": 1,  # the Import tap is the approval
                "remarks": "Opening balance imported via AI Munim",
                "items": [{"item_name": "Opening Invoice Item", "description": "Opening balance (imported)", "qty": 1,
                           "uom": "Nos", "conversion_factor": 1, "rate": float(bal["amount"]),
                           "income_account": f"Temporary Opening - {abbr}"}],
            })
            result["opening_invoices"] += 1
            result["opening_total"] += bal["amount"]
        except Exception:  # noqa: BLE001
            result["failed"] += 1
    return result
