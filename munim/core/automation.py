"""ERP -> WhatsApp: the morning summary, staff drafts waiting for the owner, and documents made in the web ERP.

WhatsApp only allows a free-form message within 24 h of the person's last message. Outside that window
we send one approved template per day if one is configured (MUNIM_TEMPLATE_NUDGE, no parameters, e.g.
"Aapke business ka naya update hai — dekhne ke liye 'summary' likhein"), otherwise nothing.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from munim.core.drafts import fmt_rs
from munim.core.erp import money
from munim.core.i18n import t
from munim.core.reports import build

WINDOW_SECONDS = 23.5 * 3600  # a little inside WhatsApp's 24 h customer-service window
SUMMARY_WORDS = {"summary", "aaj ka hisaab", "aaj ki summary", "daily summary", "aaj ka summary", "hisaab batao"}
SUMMARY_OFF = {"summary band", "summary band karo", "stop summary", "summary off"}
SUMMARY_ON = {"summary chalu", "summary chalu karo", "start summary", "summary on"}
WATCHED = ("Sales Invoice", "Payment Entry", "Purchase Invoice", "Delivery Note")


def window_open(last_inbound_at: float | None, now: float | None = None) -> bool:
    return bool(last_inbound_at) and (now or time.time()) - float(last_inbound_at) < WINDOW_SECONDS


def hook_token(secret: str, site: str) -> str:
    """Per-site token ERPNext sends with its webhooks (derived; nothing extra to store)."""
    return hmac.new(secret.encode(), f"erp-hook:{site}".encode(), hashlib.sha256).hexdigest()


async def summary_text(erp: Any, lang: str | None) -> str:
    """Yesterday's sales and money in, what is due, what is waiting: all figures from core/reports."""
    sales = await build(erp, "sales", "yesterday")
    received = await build(erp, "collections", "yesterday")
    due = await build(erp, "receivables", limit=3)
    pending = await build(erp, "pending")
    lines = [t("sum.head", lang, day=erp.today.strftime("%d %b"))]
    lines.append(t("sum.sales", lang, total=sales["total_billed"], bills=sales["bills"]))
    lines.append(t("sum.received", lang, total=received["received"], n=received["payments"]))
    if due.get("customers_with_dues"):
        lines.append(t("sum.due", lang, total=due["total_due"], n=due["customers_with_dues"]))
        for row in due["top"]:
            late = t("sum.late", lang, days=row["most_overdue_days"]) if row["most_overdue_days"] else ""
            lines.append(f"   • {row['customer']}: {row['due']}{late}")
    else:
        lines.append(t("sum.no_due", lang))
    if pending["pending"]:
        lines.append(t("sum.pending", lang, n=len(pending["pending"])))
    lines.append(t("sum.footer", lang))
    return "\n".join(lines)


def describe_erp_event(data: dict[str, Any], lang: str | None) -> str | None:
    doctype, name = data.get("doctype"), data.get("name")
    if doctype not in WATCHED or not name:
        return None
    party = data.get("party") or ""
    try:
        total = fmt_rs(money(abs(float(data.get("total") or 0))))
    except (TypeError, ValueError):
        total = ""
    key = "erp_event.repeat" if data.get("auto_repeat") else "erp_event.made"
    return t(key, lang, doctype=doctype, name=name, party=party, total=total)
