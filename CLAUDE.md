# Working on AI Munim

Rules for anyone (human or coding agent) changing this repo.

1. **The model never computes money.** Amounts, GST and totals come from `core/drafts.py`
   / ERPNext. Tools accept item, qty and a stated rate with its `rate_source` only.
2. **Tools never write to the ERP.** Writes happen only in `core/engine.py` on the Approve
   button, for the current preview version. The preview card is the confirmation: the owner's one tap
   creates and submits; staff taps create drafts (docstatus 0) for the owner. If ERPNext refuses the
   submit, the document stays a draft and the user is told. No second "submit?" question.
3. **Never guess entities.** Customers/items come from `find_customer` / `find_item`;
   ambiguous → buttons, not found → ask.
4. **One conversation state.** New context goes into `ConversationState`, not into a new
   handler with its own store.
5. **Tenants are isolated by ERPNext site.** Never query one business's site with another
   tenant's credentials. Pool files and `.env` never leave the server or get committed.
6. **User-facing fixed text lives in `core/i18n.py`** (English + Hinglish). The model
   mirrors the user's language on its own.
7. **Tests first, live calls rarely.** `pytest` uses a scripted fake LLM and must pass on
   every change; run `scripts/smoke.py` (real model, costs money) only for prompt/model
   changes.
8. Allowed AI providers: Anthropic Claude and TypeSafe Jev. Ask before adding others.
