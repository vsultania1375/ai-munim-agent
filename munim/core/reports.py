"""Business questions answered from the books: every figure is summed here in code (Decimal), the model
only words it. Periods are India dates resolved by code ("pichhle mahine" -> last_month)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from munim.core.drafts import fmt_qty, fmt_rs
from munim.core.erp import money

REPORTS = ("sales", "top_items", "top_customers", "receivables", "collections", "profit", "stock", "pending")
PERIODS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "last_7_days", "last_30_days",
           "this_quarter", "this_fy", "last_fy", "custom")
TIMED = {"sales", "top_items", "top_customers", "collections", "profit"}
AGE_BUCKETS = ((0, "not_due"), (30, "1_30_days"), (60, "31_60_days"), (90, "61_90_days"), (10**6, "over_90_days"))


class ReportError(ValueError):
    pass


def _fy_start(day: date) -> date:
    return date(day.year if day.month >= 4 else day.year - 1, 4, 1)  # Indian financial year: 1 April


def period_range(period: str, today: date, from_date: str | None = None, to_date: str | None = None) -> tuple[date, date]:
    month_start = today.replace(day=1)
    if period == "today":
        return today, today
    if period == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if period == "this_week":
        return today - timedelta(days=today.weekday()), today
    if period == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    if period == "this_month":
        return month_start, today
    if period == "last_month":
        end = month_start - timedelta(days=1)
        return end.replace(day=1), end
    if period == "last_7_days":
        return today - timedelta(days=6), today
    if period == "last_30_days":
        return today - timedelta(days=29), today
    if period == "this_quarter":
        first = ((today.month - 1) // 3) * 3 + 1
        return date(today.year, first, 1), today
    if period == "this_fy":
        return _fy_start(today), today
    if period == "last_fy":
        start = _fy_start(today)
        return date(start.year - 1, 4, 1), start - timedelta(days=1)
    if period == "custom":
        try:
            start, end = date.fromisoformat(str(from_date)), date.fromisoformat(str(to_date or from_date))
        except ValueError as exc:
            raise ReportError("custom period needs from_date/to_date as YYYY-MM-DD") from exc
        if start > end or end > today + timedelta(days=1) or (end - start).days > 3 * 366:
            raise ReportError("custom period must be a real past range of at most 3 years")
        return start, end
    raise ReportError(f"period must be one of {', '.join(PERIODS)}")


def _d(value: Any) -> Decimal:
    return money(Decimal(str(value or 0)))


def _when(start: date, end: date) -> str:
    return start.strftime("%d %b %Y") if start == end else f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"


def _change(now: Decimal, before: Decimal) -> str | None:
    if before == 0:
        return None
    pct = (now - before) * 100 / before
    return f"{pct:+.0f}%"


async def build(erp: Any, report: str, period: str = "this_month", *, from_date: str | None = None, to_date: str | None = None,
                item_code: str | None = None, limit: int = 5) -> dict[str, Any]:
    if report not in REPORTS:
        raise ReportError(f"report must be one of {', '.join(REPORTS)}")
    today = erp.today
    limit = max(1, min(int(limit or 5), 15))
    out: dict[str, Any] = {"report": report}
    if report in TIMED:
        start, end = period_range(period, today, from_date, to_date)
        out["period"] = _when(start, end)
    if report == "sales":
        rows = await erp.report_data("invoices", start, end)
        span = (end - start).days + 1
        before = await erp.report_data("invoices", start - timedelta(days=span), start - timedelta(days=1))
        total = sum((_d(r["total"]) for r in rows), Decimal(0))
        prev = sum((_d(r["total"]) for r in before), Decimal(0))
        out.update({"bills": len(rows), "sales_before_gst": fmt_rs(sum((_d(r["net"]) for r in rows), Decimal(0))),
                    "gst": fmt_rs(sum((_d(r["tax"]) for r in rows), Decimal(0))), "total_billed": fmt_rs(total),
                    "previous_same_length_period": fmt_rs(prev)})
        if (change := _change(total, prev)) is not None:
            out["change_vs_previous"] = change
    elif report == "top_items":
        rows = await erp.report_data("invoice_items", start, end)
        by: dict[str, dict[str, Any]] = {}
        for r in rows:
            entry = by.setdefault(r["item_code"], {"item": r.get("item_name") or r["item_code"], "qty": Decimal(0), "net": Decimal(0)})
            entry["qty"] += Decimal(str(r.get("qty") or 0))
            entry["net"] += _d(r.get("net"))
        ranked = sorted(by.values(), key=lambda e: e["net"], reverse=True)[:limit]
        out["items"] = [{"item": e["item"], "qty": fmt_qty(e["qty"]), "sales_before_gst": fmt_rs(e["net"])} for e in ranked]
    elif report == "top_customers":
        rows = await erp.report_data("invoices", start, end)
        totals: dict[str, list[Any]] = {}
        for r in rows:
            entry = totals.setdefault(r["customer"], [r.get("customer_name") or r["customer"], Decimal(0), 0])
            entry[1] += _d(r["total"])
            entry[2] += 1
        ranked = sorted(totals.values(), key=lambda e: e[1], reverse=True)[:limit]
        out["customers"] = [{"customer": n, "billed": fmt_rs(v), "bills": c} for n, v, c in ranked]
    elif report == "receivables":
        rows = await erp.report_data("open_invoices", today, today)
        buckets = {label: Decimal(0) for _, label in AGE_BUCKETS}
        per: dict[str, list[Any]] = {}
        for r in rows:
            amount = _d(r["outstanding"])
            late = (today - date.fromisoformat(str(r["due_date"]))).days
            label = next(lbl for limit_days, lbl in AGE_BUCKETS if late <= limit_days)
            buckets[label] += amount
            entry = per.setdefault(r["customer"], [r.get("customer_name") or r["customer"], Decimal(0), 0])
            entry[1] += amount
            entry[2] = max(entry[2], late)
        ranked = sorted(per.values(), key=lambda e: e[1], reverse=True)[:limit]
        out.update({"total_due": fmt_rs(sum(buckets.values(), Decimal(0))), "customers_with_dues": len(per),
                    "by_age": {k: fmt_rs(v) for k, v in buckets.items() if v},
                    "top": [{"customer": n, "due": fmt_rs(v), "most_overdue_days": max(d, 0)} for n, v, d in ranked]})
    elif report == "collections":
        rows = await erp.report_data("payments", start, end)
        modes: dict[str, Decimal] = {}
        for r in rows:
            modes[r.get("mode") or "Other"] = modes.get(r.get("mode") or "Other", Decimal(0)) + _d(r["amount"])
        out.update({"received": fmt_rs(sum(modes.values(), Decimal(0))), "payments": len(rows),
                    "by_mode": {k: fmt_rs(v) for k, v in sorted(modes.items(), key=lambda kv: kv[1], reverse=True)}})
    elif report == "profit":
        rows = await erp.report_data("gl", start, end)
        income = sum((_d(r["credit"]) - _d(r["debit"]) for r in rows if r["root_type"] == "Income"), Decimal(0))
        expense = sum((_d(r["debit"]) - _d(r["credit"]) for r in rows if r["root_type"] == "Expense"), Decimal(0))
        out.update({"income": fmt_rs(income), "expenses": fmt_rs(expense), "profit": fmt_rs(income - expense)})
        if expense == 0:
            out["note"] = "no purchases or expenses are recorded for this period, so this is income only"
    elif report == "stock":
        rows = await erp.report_data("stock", today, today, item_code)
        out["stock"] = [{"item": r.get("item_name") or r["item_code"], "warehouse": r.get("warehouse"), "qty": fmt_qty(Decimal(str(r["qty"])))}
                        for r in rows[:30]]
        if not rows:  # stock isn't moved per warehouse here: work it out from purchase and sales bills instead
            rows = await erp.report_data("bill_stock", today, today, item_code)
            out["stock"] = [{"item": r.get("item_name") or r["item_code"], "qty": fmt_qty(Decimal(str(r["qty"])))} for r in rows[:30]]
            out["basis"] = "bought minus sold, from final bills (no warehouse stock is kept); say so"
        if not rows:
            out["note"] = "no stock-tracked quantities found" + (f" for {item_code}" if item_code else "")
    elif report == "pending":
        rows = await erp.report_data("pending", today, today)
        out["pending"] = [{"doctype": r["doctype"], "name": r["name"], "customer": r.get("customer"), "status": r.get("status"),
                           "total": fmt_rs(_d(r.get("total")))} for r in rows[:25]]
    return out
