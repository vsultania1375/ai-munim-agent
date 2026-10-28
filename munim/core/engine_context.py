"""What the model sees each turn besides the chat: state, time, style, memory, WhatsApp signals."""
from __future__ import annotations

import json

from munim.core import style as styles
from munim.core.clock import gap, now_line, stamp, today_ist
from munim.core.drafts import KIND_DOCTYPE, KIND_LABEL
from munim.core.engine_common import GAP_MARKER_SECONDS, Inbound
from munim.core.state import ConversationState, DocumentContext


class ContextMixin:
    """What the model sees each turn besides the chat: state, time, style, memory, WhatsApp signals."""

    def _state_block(self, state: ConversationState) -> str:
        now = self.clock()
        lines = [f"now: {now_line(now)}", f"today: {getattr(self.erp, 'today', today_ist()).isoformat()}",
                 f"user_style: {styles.hint(state.style)}",
                 f"reply_language: {reply_language(state)}"]
        facts = self.store.memory_list(self.tenant)
        if facts:  # what the owner asked us to keep in mind; ERP data and tool results still win
            lines.append("business_memory: " + " | ".join(
                f"#{f['id']} {f['fact']}" + (f" (about {f['about']})" if f.get("about") else "") for f in facts))
        draft = state.draft
        if draft is None:
            lines.append("open_preview: none")
        elif draft.kind in KIND_DOCTYPE:
            rows = "; ".join(
                f"{line.item_name} [{line.item_code}] qty {line.qty} rate {line.rate} ({line.rate_source})" for line in draft.lines
            )
            origin = f" from {draft.source['doctype']} {draft.source['name']}" if draft.source else ""
            if draft.amend:
                origin += f" correcting {draft.amend['doctype']} {draft.amend['name']}"
            if draft.discount:
                origin += f" with {draft.discount}% discount"
            lines.append(
                f"open_preview: {KIND_LABEL[draft.kind]}{origin} for {draft.customer_name} [{draft.customer_id}] — {rows} (version {draft.version})"
            )
        elif draft.kind == "payment":
            lines.append(
                f"open_preview: Payment from {draft.customer_name} [{draft.customer_id}] amount {draft.payment_amount} mode {draft.payment_mode}"
            )
        elif draft.kind == "purchase":
            rows = "; ".join(f"{x.item_name} [{x.item_code}] qty {x.qty} rate {x.rate} ({x.rate_source})" for x in draft.lines)
            lines.append(f"open_preview: Purchase bill from supplier {draft.customer_name} [{draft.customer_id}] bill {draft.bill_no or '-'} "
                         f"dated {draft.bill_date or '-'} — {rows} (version {draft.version})")
        elif draft.kind == "supplier_payment":
            lines.append(f"open_preview: Payment to supplier {draft.customer_name} [{draft.customer_id}] amount {draft.payment_amount} "
                         f"mode {draft.payment_mode}")
        elif draft.kind == "debit_note":
            rows = "; ".join(f"{x.item_name} [{x.item_code}] qty {x.qty} rate {x.rate}" for x in draft.lines)
            lines.append(f"open_preview: Debit Note (purchase return) against Purchase Invoice {draft.source['name']} to "
                         f"{draft.customer_name} — {rows} (version {draft.version})")
        elif draft.kind == "credit_note":
            rows = "; ".join(f"{x.item_name} [{x.item_code}] qty {x.qty} rate {x.rate}" for x in draft.lines)
            lines.append(f"open_preview: Credit Note (return) against Sales Invoice {draft.source['name']} for {draft.customer_name} "
                         f"— {rows} (version {draft.version})")
        elif draft.kind == "bahi":
            lines.append("open_preview: Bahi page dues " + "; ".join(f"{r['customer']} due {r['due']}" for r in draft.entries))
        elif draft.kind == "brand":
            lines.append(f"open_preview: Bill design {(draft.brand or {}).get('primary_name')} {(draft.brand or {}).get('primary')}")
        elif draft.kind == "pricing":
            lines.append(f"open_preview: Permanent discount {draft.payment_amount}% for {draft.customer_name} (was {draft.note}%)")
        elif draft.kind == "repeat":
            lines.append(f"open_preview: Recurring copy of Sales Invoice {draft.source['name']} {draft.note}, first {draft.bill_date}")
        elif draft.kind == "cancel":
            lines.append(f"open_preview: Cancel {draft.source['doctype']} {draft.source['name']} (waiting for Approve)")
        elif draft.kind == "expense":
            lines.append(f"open_preview: Expense {draft.expense_account} amount {draft.payment_amount} paid by {draft.payment_mode} "
                         f"note {draft.note or '-'}")
        else:
            who = "supplier" if draft.kind == "new_supplier" else "customer"
            lines.append(f"open_preview: New {who} {json.dumps(draft.new_customer, ensure_ascii=False)}")
        if state.question:
            options = " / ".join(o["title"] for o in state.question.options)
            lines.append(f"pending_question: {state.question.question} (options: {options})")
        else:
            lines.append("pending_question: none")
        doc = state.document
        if doc:
            age = int((now - doc.received_at) / 60)
            facts = json.dumps(doc.facts, ensure_ascii=False) if doc.facts else "not read yet"
            lines.append(f"uploaded_document: {doc.filename or doc.mime}, {age} min ago, facts: {facts}")
        else:
            lines.append("uploaded_document: none")
        if state.next_steps:
            lines.append("queued_after_approve: " + " | ".join(state.next_steps))
        if state.last_created:
            recent = "; ".join(
                f"{d.get('doctype') or d['label']} {d['name']} ({d['customer']}, {'final' if d.get('submitted') else 'draft'}"
                + (f", from {d['from']}" if d.get("from") else "") + ")"
                for d in state.last_created[:3]
            )
            lines.append(f"recently_created: {recent}")
        return "\n".join(lines)

    def _at(self, msg: Inbound | None) -> float:
        """When the user actually sent it; falls back to now (and never in the future)."""
        now = self.clock()
        return min(msg.sent_at, now) if msg and msg.sent_at else now

    def _signals(self, msg: Inbound | None, state: ConversationState) -> str:
        """Everything WhatsApp told us about this message that matters for meaning."""
        if msg is None:
            return ""
        lines: list[str] = []
        now = self.clock()
        last_at = next((turn.get("at") for turn in reversed(state.turns) if turn.get("at")), None)
        if last_at and self._at(msg) - last_at > GAP_MARKER_SECONDS:
            lines.append(f"since_last_message: {gap(self._at(msg) - last_at)} (last activity {stamp(last_at)} IST)")
        if msg.sent_at and now - msg.sent_at > 120:
            lines.append(f"this_message_sent: {stamp(msg.sent_at)} IST ({gap(now - msg.sent_at)} ago; delivered late)")
        if msg.reply_to:
            lines.append(f"replied_to: {self._replied_to(msg, state)}")
        if msg.forwarded:
            lines.append("forwarded: yes" + (" (frequently forwarded)" if msg.frequently_forwarded else "")
                         + " — likely from someone else (supplier/customer)")
        if msg.kind == "audio":
            lines.append("input: " + ("voice note, transcribed on our server" if msg.voice else "audio file, transcribed"))
        if self.walog:
            last = self.walog.last_outbound(msg.sender)
            if last and last.status == "failed":
                lines.append(f"delivery_problem: our last message ({last.meta.get('kind', last.kind)}) did not reach the user")
        return ("\n" + "\n".join(lines)) if lines else ""

    def _replied_to(self, msg: Inbound, state: ConversationState) -> str:
        rec = self.walog.get(msg.reply_to) if self.walog else None
        if rec is None:
            return "an older message we have no record of"
        meta = rec.meta
        if rec.direction == "in":
            if meta.get("media_path"):
                facts = self.store.media_get(self.tenant, meta.get("digest", ""))
                state.document = DocumentContext(meta["media_path"], meta.get("mime", ""), meta.get("filename"), self.clock(), facts=facts)
                return f"the user's own earlier document {meta.get('filename') or ''} (now the selected uploaded_document)".strip()
            said = meta.get("transcript") or rec.text
            return f"the user's own earlier message ({stamp(rec.at)}): \"{said[:200]}\""
        kind = meta.get("kind", rec.kind)
        if kind == "card":
            draft = state.draft
            if draft and draft.id == meta.get("draft_id"):
                if draft.version == meta.get("version"):
                    return "the CURRENT open preview — apply their words to it"
                return f"an OLDER version (v{meta.get('version')}) of the open preview; the open one is v{draft.version}"
            done = next((d for d in state.last_created if d.get("draft_id") == meta.get("draft_id")), None)
            if done:
                return f"the preview that became {done['label']} {done['name']} (already created)"
            return f"a preview that was cancelled or expired ({meta.get('draft_kind')} for {meta.get('customer')})"
        if kind in {"document", "submit_offer", "submit_confirm"}:
            done = next((d for d in state.last_created if d.get("name") == meta.get("doc")), None)
            status = "submitted" if done and done.get("submitted") else "draft"
            return f"{meta.get('doctype')} {meta.get('doc')} for {meta.get('customer') or 'the customer'} ({status})"
        if kind == "question":
            return f"our question: \"{meta.get('question', rec.text)[:200]}\""
        return f"our earlier message ({stamp(rec.at)}): \"{rec.text[:200]}\""


def reply_language(state: ConversationState) -> str:
    """What the person writes in, from code (their last messages), so the reply never drifts into another language."""
    if state.style.get("deva", 0) >= 0.5:
        return "Hindi in Devanagari"
    return "Hinglish (Hindi in Roman script)" if state.lang == "hi" else "English"
