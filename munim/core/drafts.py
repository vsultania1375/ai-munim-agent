"""Draft previews: deterministic totals and the text shown on the approval card."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from munim.core.erp import Item, money
from munim.core.gst import TAX_INTER, TAX_INTRA, TAX_NONE
from munim.core.i18n import kind_label, t

KIND_LABEL = {
    "quotation": "Quotation",
    "sales_order": "Sales Order",
    "invoice": "Sales Invoice",
    "delivery_note": "Delivery Challan",
    "payment": "Payment Received",
    "purchase": "Purchase Bill",
    "credit_note": "Credit Note (Return)",
    "cancel": "Cancel",
    "expense": "Expense",
    "new_supplier": "New Supplier",
    "new_customer": "Naya Customer",
}

KIND_DOCTYPE = {
    "quotation": "Quotation",
    "sales_order": "Sales Order",
    "invoice": "Sales Invoice",
    "delivery_note": "Delivery Note",
}
DOCTYPE_KIND = {doctype: kind for kind, doctype in KIND_DOCTYPE.items()}


@dataclass(frozen=True)
class ComputedRow:
    item: Item
    qty: Decimal
    rate: Decimal
    amount: Decimal
    tax: Decimal


@dataclass(frozen=True)
class Computed:
    rows: tuple[ComputedRow, ...]
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    taxes: tuple[tuple[str, Decimal], ...] = ()  # (CGST, x), (SGST, y) or (IGST, z)
    mode: str = TAX_INTRA


def compute_lines(lines: list[tuple[Item, Decimal, Decimal]], mode: str = TAX_INTRA) -> Computed:
    rows = []
    for item, qty, rate in lines:
        amount = money(Decimal(qty) * Decimal(rate))
        tax = money(amount * item.gst_rate / Decimal(100))
        rows.append(ComputedRow(item, Decimal(qty), money(rate), amount, tax))
    subtotal = money(sum((r.amount for r in rows), Decimal(0)))
    # India Compliance: one tax row per component (CGST, SGST or IGST) over all items, each item at its own
    # rate; ERPNext adds the unrounded item amounts and rounds the row once.
    if mode == TAX_NONE:
        taxes: tuple[tuple[str, Decimal], ...] = ()
    elif mode == TAX_INTER:
        taxes = (("IGST", money(sum((r.amount * r.item.gst_rate / 100 for r in rows), Decimal(0)))),)
    else:
        half = money(sum((r.amount * r.item.gst_rate / 200 for r in rows), Decimal(0)))
        taxes = (("CGST", half), ("SGST", half))
    tax = money(sum((amount for _, amount in taxes), Decimal(0)))
    return Computed(tuple(rows), subtotal, tax, money(subtotal + tax), taxes, mode)


def fmt_qty(qty: Decimal) -> str:
    normalized = qty.normalize()
    return f"{normalized:f}"


def fmt_rs(value: Decimal) -> str:
    whole, _, paise = f"{money(value):.2f}".partition(".")
    sign = "-" if whole.startswith("-") else ""
    whole = whole.lstrip("-")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    text = f"{sign}₹{whole}"
    return text if paise == "00" else f"{text}.{paise}"


def render_sales_preview(
    kind: str, customer_name: str, computed: Computed, demo_label: str | None, lang: str | None = None,
    final: bool = False, source: str | None = None, hsn_missing: list[str] | None = None,
    replaces: str | None = None, discount: str | None = None, customer_discount: str | None = None,
) -> str:
    lines = [f"*{kind_label(kind, lang)} — {t('preview', lang)}*", f"Customer: {customer_name}"]
    if source:
        lines.append(t("preview.against" if kind == "credit_note" else "preview.from", lang, source=source))
    if replaces:
        lines.append(t("preview.replaces", lang, name=replaces))
    lines.append("")
    for n, row in enumerate(computed.rows, start=1):
        lines.append(
            f"{n}. {row.item.name}\n   {fmt_qty(row.qty)} {row.item.uom} × {fmt_rs(row.rate)} = {fmt_rs(row.amount)}"
        )
    lines += [
        "",
        f"Subtotal: {fmt_rs(computed.subtotal)}",
        _gst_line(computed, lang),
        f"*Total: {fmt_rs(computed.total)}*",
        "",
    ]
    if customer_discount:
        lines.insert(-5, t("preview.customer_discount", lang, pct=customer_discount))
    if discount:
        lines.insert(-5, t("preview.discount", lang, pct=discount))
    if kind == "credit_note" and source:
        lines += [t("credit_effect", lang, total=fmt_rs(computed.total), source=source), ""]
    if hsn_missing:
        lines += [t("hsn_missing", lang, items=", ".join(hsn_missing)), ""]
    lines.append(t("approve_note_final" if final else "approve_note", lang))
    if demo_label:
        lines.append(f"_{demo_label}_")
    return "\n".join(lines)


def _gst_line(computed: Computed, lang: str | None) -> str:
    if computed.mode == TAX_NONE:
        return t("gst_none", lang)
    parts = " + ".join(f"{name} {fmt_rs(amount)}" for name, amount in computed.taxes)
    return f"GST: {fmt_rs(computed.tax)} ({parts})"


def render_payment_preview(
    customer_name: str, amount: Decimal, mode: str, demo_label: str | None, lang: str | None = None,
    allocations: list[dict[str, str]] | None = None, final: bool = False,
) -> str:
    lines = [
        f"*{kind_label('payment', lang)} — {t('preview', lang)}*",
        f"Customer: {customer_name}",
        f"{t('label.amount', lang)}: {fmt_rs(amount)}",
        f"{t('label.mode', lang)}: {mode}",
    ]
    if allocations:
        lines += ["", t("pay.against", lang)]
        lines += [f"• {a['invoice']}: {fmt_rs(Decimal(a['amount']))}" for a in allocations]
    advance = money(amount - sum((Decimal(a['amount']) for a in allocations or []), Decimal(0)))
    if advance > 0 and allocations is not None:
        lines.append(t("pay.advance", lang, amount=fmt_rs(advance)))
    lines += ["", t("payment_note_final" if final else "payment_note", lang)]
    if demo_label:
        lines.append(f"_{demo_label}_")
    return "\n".join(lines)


def render_purchase_preview(
    supplier: str, computed: Computed, bill_no: str | None, bill_date: str | None, attached: bool,
    demo_label: str | None, lang: str | None = None, final: bool = False, hsn_missing: list[str] | None = None,
) -> str:
    lines = [f"*{kind_label('purchase', lang)} — {t('preview', lang)}*", f"{t('label.supplier', lang)}: {supplier}"]
    if bill_no or bill_date:
        lines.append(t("label.bill", lang, no=bill_no or "-", date=bill_date or "-"))
    lines.append("")
    for n, row in enumerate(computed.rows, start=1):
        lines.append(f"{n}. {row.item.name}\n   {fmt_qty(row.qty)} {row.item.uom} × {fmt_rs(row.rate)} = {fmt_rs(row.amount)}")
    gst = _gst_line(computed, lang) if computed.mode != TAX_NONE else t("gst_none_supplier", lang)
    lines += ["", f"Subtotal: {fmt_rs(computed.subtotal)}", gst, f"*Total: {fmt_rs(computed.total)}*", ""]
    if attached:
        lines.append(t("bill_attached", lang))
    if hsn_missing:
        lines.append(t("hsn_missing", lang, items=", ".join(hsn_missing)))
    lines.append(t("record_note_final" if final else "approve_note", lang))
    if demo_label:
        lines.append(f"_{demo_label}_")
    return "\n".join(lines)


def render_cancel_preview(doctype: str, name: str, customer: str | None, lang: str | None = None) -> str:
    lines = [f"*{t('cancel.title', lang)}*", f"{doctype} *{name}*" + (f" ({customer})" if customer else ""), "",
             t("cancel.effect", lang)]
    return "\n".join(lines)


def render_expense_preview(account: str, amount: Decimal, mode: str, note: str | None, demo_label: str | None,
                           lang: str | None = None, final: bool = False) -> str:
    lines = [f"*{kind_label('expense', lang)} — {t('preview', lang)}*", f"{t('label.head', lang)}: {account}",
             f"{t('label.amount', lang)}: {fmt_rs(amount)}", f"{t('label.paid_from', lang)}: {mode}"]
    if note:
        lines.append(f"{t('label.note', lang)}: {note}")
    lines += ["", t("record_note_final" if final else "approve_note", lang)]
    if demo_label:
        lines.append(f"_{demo_label}_")
    return "\n".join(lines)


def render_customer_preview(name: str, phone: str | None, gstin: str | None, lang: str | None = None,
                            kind: str = "new_customer") -> str:
    lines = [f"*{kind_label(kind, lang)} — {t('preview', lang)}*", f"{t('label.name', lang)}: {name}"]
    if phone:
        lines.append(f"Phone: {phone}")
    if gstin:
        lines.append(f"GSTIN: {gstin}")
    lines += ["", t("supplier_note" if kind == "new_supplier" else "customer_note", lang)]
    return "\n".join(lines)
