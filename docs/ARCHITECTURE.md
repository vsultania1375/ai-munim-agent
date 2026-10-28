# Architecture

## Request path

```
WhatsApp ──► Meta Cloud API ──► Caddy (HTTPS) ──► bot (FastAPI, munim.core.server)
                                                     │
   webhook: verify X-Hub-Signature-256 → dedupe wamid → per-sender queue → 200 OK
                                                     │
   per-sender worker (one message at a time per chat, 1.5 s debounce for bursts)
     route by number (core/tenants.py):
       new / onboarding ──► core/onboarding.py   (own ERPNext site from a pool)
       demo / tester    ──► agent on the shared demo site
       active owner     ──► agent on that business's own site
     agent turn (core/engine.py):
       1. button fast path: approve:<draft>:<version> / edit / cancel / option taps
       2. ConversationState: one JSON object per (site, sender)
       3. turn hint from Jev (optional, advisory)
       4. Claude with a closed tool set (core/tool_specs.py; executed by core/tools*.py)
       5. previews computed in code; Approve (one tap) → ERPNext document, submitted for the owner (draft for staff) + PDF
```

ERPNext (frappe_docker) runs as its own compose project on the same host. One bench
serves many sites; nginx selects the site from the `Host` header, so the bot and Caddy
set `Host: <site>` per business.

## ConversationState (core/state.py)

The single source of conversational context: last 12 turns, at most **one** open preview
(kind, customer, lines, version), the last uploaded document and its extracted facts, the
pending question with its button options, recently created documents, and the detected
language. One open preview by design removes "which preview do you mean?" loops.

## Money rule

The model never produces a computed amount. Tools take item, qty and an optional stated
rate with `rate_source` (`price_list`, `user_stated`, `document`). Code resolves price-list
rates and computes line amounts and GST with `Decimal` the way ERPNext + India Compliance do
(one row per CGST/SGST/IGST over all items, each item at its own rate, rounded once), so the
preview total equals the ERPNext document total to the paisa.

## GST (core/gst.py, India Compliance)

- Unregistered business → no GST at all. Registered → CGST+SGST when the place of supply is the
  business's state, IGST otherwise. Place of supply: customer GSTIN, else billing address state,
  else the business's state (walk-in).
- Bills carry IC's `Output GST In-state/Out-state` template with its rows (a server insert does
  not fetch them). Item rates come from the item's `GST n%` Item Tax Template.
- HSN: items without a code are flagged on the card; the code the owner confirms (`set_item_hsn`,
  checked against IC's HSN list) is written on the item at Approve. 4 digits are enough up to
  ₹5 Cr turnover. Migration reads an HSN column.
- A registered customer added from WhatsApp gets a billing address in their GSTIN's state.
- Live-verified (ERPNext 16.52 + IC 16.11): 3×452.35 @28% + 7×65.33 @18% → CGST 231.14 + SGST
  231.14 = 2,276.64 (Maharashtra); IGST 462.29 = 2,276.65 (Rajasthan).

## Closed tools

`find_customer`, `find_item`, `customer_outstanding`, `recent_documents`,
`propose_sales_document` (quotation | invoice | delivery_note), `edit_draft`,
`propose_payment`, `propose_new_customer`, `cancel_draft`, `ask_user` (2-3 buttons),
`save_document_facts`. No tool writes to the ERP; writes happen only from the Approve
button, and stale versions or repeated taps are rejected.

## Language

The model mirrors the user's language and script. Fixed text (previews, buttons,
onboarding, errors) comes from `core/i18n.py` in English or Hinglish, chosen from the
user's own messages (sticky; short messages like "ok" keep the current language).

## Onboarding (core/onboarding.py)

A new number that just says hi gets [Own setup | Demo]. A real request typed or spoken without choosing is
answered by the demo shop at once (with the own-setup button on top). Demo visitors get `MUNIM_DEMO_LIMIT`
messages (default 25; button taps don't count), a heads-up with 3 left, then the setup button. Own setup only
when asked for (button, "setup", a GSTIN, or the website's set-up message), straight to the GST step:

identity (GSTIN text, a photo/PDF of a GST certificate, bill or
visiting card read by Claude, or "no GST" → name + city) → confirm card → business type →
own ERPNext site from the pool (setup wizard + GST templates over REST, ~30 s) → old data
[Excel/CSV | Tally/Vyapar help | Later] → preview counts [Import | Skip] → live.

- GSTIN: offline format, mod-36 checksum, state and PAN (`core/gstin.py`). No live GST
  portal verification (needs a paid provider).
- Import (`core/migration.py`): deterministic header synonyms for Tally/Vyapar/Khatabook
  style exports, Claude fallback for unknown headers or register photos. Customers and
  items (with Standard Selling rate and GST template) are created; opening balances become
  one draft Journal Entry for the owner to review.
- Pool exhausted → demo mode with a waitlist flag; `infra/make_pool.sh N` adds sites.

## Web ERP (core/web_access.py)

Each business's ERP is at `https://<its-name>.<domain>` (a label made from the business name, unique, kept
once given; the older `<site>.<domain>` still works) and the demo at `demo.<domain>`. Sites keep their pool
names: Caddy asks the bot (`forward_auth` → `/caddy/site`) which ERPNext site serves a host and sets `Host`,
so a new business needs no Caddy reload. On-demand TLS: `/caddy/ask` approves only hosts we serve. Owners type *ERP login* (also sent when
onboarding finishes) to get a login for their own site (10-digit mobile number); *password
reset* rotates it. Demo-mode users get no login to the shared site.

## WhatsApp signals (whatsapp/webhook.py, core/walog.py)

Nothing WhatsApp sends is dropped. `WALog` (`.run/v2/walog.db`) records every inbound message,
every outbound message with what it was (`meta`: preview card + draft id/version, created document,
question), and each outbound's delivery status (sent → delivered → read, or failed).

| Signal | Where it goes |
|---|---|
| `timestamp` | Turns are stored with the user's send time. `<state>` carries `now` in IST, `since_last_message` after a 3 h idle gap, and `this_message_sent` when a message arrives late. User history lines are stamped `[Mon 26 Oct 13:05]` with `[— N later —]` markers. India date for ERP `today`. |
| `context.id` (swipe-reply) | `replied_to` in `<state>`: the current/older preview version, a created document (draft/submitted), our question, or the user's own earlier photo/PDF, which is re-selected as the uploaded document. |
| `forwarded`, `frequently_forwarded` | `forwarded:` line, so the model knows the content is from a supplier/customer. |
| `audio.voice` | Voice note vs audio file label; the transcript is stored with the message. |
| media `sha256` | A file already read is not downloaded again; its facts are reused (no pixels to the model). |
| `reaction` | Logged as context; on the open preview the bot points at ✅ Approve. A reaction never approves. |
| `referral` | First ad/post that brought the person, kept on the tenant. |
| `system` number change | Tenant, conversation and log move to the new number; the ERP login is re-made for it. |
| profile name | Kept on the tenant and used for the ERP user's name. |
| `statuses` | Stored per outbound message; failures are logged with Meta's error code and the next turn gets `delivery_problem`. |

## Document chain (tools.propose_from_document, erp.CONVERSIONS)

The next document is made with ERPNext's own mappers, so links stay and only the pending quantity
comes across. Supported: Quotation → Sales Order / Sales Invoice / Delivery Note; Sales Order →
Delivery Note / Sales Invoice; Delivery Note → Sales Invoice; Sales Invoice → Delivery Note.

- Quotation → challan goes through a final Sales Order for the **whole** quotation (ERPNext rule);
  the challan carries only what goes out now, the rest stays "to deliver" on the order.
- Partial challans/bills: the model lowers qty with `edit_draft`; the next conversion offers the rest.
- The source must be final; a sourced preview can't switch type or customer. ERPNext's validation
  message (e.g. "already invoiced", "quotation expired") is returned to the model as is.
- Live-verified on ERPNext 16: QTN 4 → SO 4 → DN 3 (SO 75 % delivered) → SI 3 with DN/SO links.

## Business questions (core/reports.py)

`business_report` answers sales (vs the previous period), top items, top customers, receivables by
age, collections by mode, profit (income − expenses from the ledger), stock on hand and pending
documents. The ERP adapter returns raw rows; every sum is Decimal in code, periods are India dates
resolved in code (financial year from 1 April). Read-only.

## Purchases and expenses

- Supplier bill (photo/PDF/forward) → `propose_purchase_bill` with the bill's own rates, number and
  date → Purchase Invoice with input GST by the supplier's state (none if the supplier has no
  GSTIN — India Compliance rule) and the original attached as a private file.
- Expenses → `propose_expense` against a checked expense head → Journal Entry from Cash or Bank.
- New suppliers on Approve. Live-verified: PI 8,307.14 (CGST/SGST 794.82 input), JE 500.
- Paying a supplier → `propose_supplier_payment`: a Payment Entry (Pay) split over their open purchase bills,
  oldest first, in code. Goods sent back → `propose_purchase_return`: a Debit Note via ERPNext's
  `make_debit_note`, part returns checked against what is left, today's input-GST template. Live-verified on
  ERPNext (1 bag back: debit 487.04 with GST; owed 8,307.14 → 7,820.10), then cancelled.
- Stock: per-warehouse stock when ERPNext tracks it; otherwise bought minus sold from final bills, labelled.

## Corrections

- Return: `propose_return` → Credit Note from ERPNext's `make_sales_return`, only what is still
  returnable, `update_outstanding_for_self = 0` so the credit comes off the original bill; today's
  GST template is applied (pre-IC bills used a retired one). Live: −576 (CGST/SGST −63), bill
  outstanding 0, "Credit Note Issued".
- Cancel: owner only, confirm card, ERPNext's reason shown if something linked blocks it.
- Correct a final document: Approve cancels it and makes the corrected copy with links and date;
  `amended_from` only when NAME-1 fits GST's 16 characters, else a "Corrects …" remark.
- Discount %: applied to the preview's rates in code.
- ERPNext rounds exact half paisa to even ("Banker's Rounding"); `money()` does the same.

## ERP -> WhatsApp (core/automation.py)

- Morning summary (09:00–12:00 IST, once a day) and the word `summary`: yesterday's sales and money
  in, dues by customer, open documents — from core/reports, no model call. `summary band/chalu`.
- Staff draft → the owner gets it with Finalise / Later buttons (`own:` / `ownno:`), which act only
  on the owner's own site.
- Recurring bills: `propose_repeat` → ERPNext Auto Repeat on a final Sales Invoice (Allow Auto
  Repeat is switched on for Sales Invoice at provisioning).
- ERPNext Webhooks (Sales Invoice, Payment Entry, Purchase Invoice, Delivery Note on submit) post to
  `/erp/hook/<site>` over the docker network with a per-site token (HMAC of the portal secret).
  Documents the bot itself sent are skipped (WA log).
- WhatsApp allows free text only within 24 h of the person's last message; outside it we send at
  most one approved nudge template a day (`MUNIM_TEMPLATE_NUDGE`), or nothing.

## Online payments: each business's own Razorpay (munim/payments/)

Money goes from the customer to the business's own Razorpay account and bank; AI Munim never holds funds
(so it is not a payment aggregator).

- Owner: "online payment chalu karo" -> `online_payments` tool -> a one-time sign-in link (15 min) to the
  portal. Keys are entered there, never in chat; checked against Razorpay with a read-only call; stored per
  ERP site, the secret encrypted (Fernet, key derived from the server secret). On/off/disconnect from chat
  or the portal. Staff can't; on the shared demo only testers can, and demo visitors' bills never get links.
- When on, every final bill gets one pay link (reused if asked again), sent to the owner to forward.
- Paid: the business's webhook `/razorpay/webhook/<site>` (its own secret) or, with no webhook, a check of
  open links every 2 minutes. Each link is recorded once, only for its own site: a Payment Entry against the
  bill, then a WhatsApp to the owner.
- Production path: Razorpay Partner OAuth ("Connect with Razorpay") replaces typing keys; the stored account
  shape stays the same.

## Your own Claude / ChatGPT (core/mcp_server.py)

Remote MCP at `https://<domain>/mcp` (Streamable HTTP, stateless JSON). OAuth 2.1 authorization server on
the same origin: discovery, dynamic client registration, PKCE, refresh rotation, revocation. The login
step is the WhatsApp code — `/authorize` sends the browser to `/mcp/connect`, the person sends
`LOGIN <code>` to the bot, and the token is bound to that number's business and role (hashed at rest,
dead once the number leaves the team). Tools: the WhatsApp agent's closed set (finds, reports,
propose_*) + `about_my_business` + `confirm_preview` (destructive hint). Money is still computed by our
code; every write is preview → confirm; owners make final documents, staff drafts. The caller's AI
does the conversation, so there is no model call on our side. Each call is written to
`.run/v2/mcp_audit.jsonl`.

- In-chat card (MCP Apps `ui://munim/card.html`, also as a ChatGPT skybridge template): preview with
  Confirm/Cancel, report tables, PDF buttons. Prompts, tool titles, icon. CIMD and DCR both work.
- PDFs: confirm_preview / get_pdf / finalise_draft return the PDF as an embedded resource and a signed
  link (`/pdf/<token>`, HMAC, 15 minutes and 5 minutes after its first open). Nothing is stored; the
  PDF is fetched from that business's ERPNext when the link is opened.
- get_document (what is on a document), finalise_draft (owner makes a staff draft final),
  get_upload_link (one-time page to attach a supplier bill photo/PDF to the next purchase entry —
  the model cannot send file bytes itself).
- Portal "Connected AI apps": each app with when it was connected and last used, and Disconnect,
  which revokes its tokens at once.

## Brand, discounts, bahi, voice

- Bill design (core/brand.py): the logo's colours are picked in code — the background (border colour or
  transparency) and near-white/black are left out; the most present colour that carries white text is
  primary, a different one accent; light colours get a charcoal header. `set_brand` (owner) adds
  bank/UPI (format-checked, masked on the card) and, on Approve, writes `AI Munim <doctype>` Jinja print
  formats (invoice, quotation, order, challan) as the defaults. PDFs use the default format.
- Permanent customer discount: `set_customer_discount` -> ERPNext Pricing Rule (the web ERP applies it).
  Bot previews take it off the list price in code and every bot document sets `ignore_pricing_rule`,
  because ERPNext otherwise rewrites the sent rate.
- Bahi page photo: the model copies each entry (party, sale/udhaar/received, amount); code nets every
  party; Approve books positive dues as opening invoices with the page attached.
- Voice replies: on a voice turn the model adds a `SPEAK:` line (Devanagari Hindi or English) that is
  never shown; Piper on the VM speaks it, ffmpeg makes an OGG/Opus voice note. `voice reply band/chalu`.
- Builds: `infra/fetch_wheels.sh` downloads all wheels with retries; the image installs from them.

## Business memory and style (core/state.py, core/style.py)

- `remember` / `forget` (owner only): standing facts per business, shown to the agent every turn as
  `business_memory`; ERP data and tool results win, and no rate or amount is taken from memory into a document.
- `customer_usual`: from the customer's recent final invoices (cancelled ones excluded, so the chain isn't
  counted three times): items, median qty, how often, last rate/date, buying gap. Code, not the model.
- Style: each message updates a fading score (emojis, Devanagari, joking, formal, length); the agent gets a
  `user_style` hint, and fixed messages lose their emojis in code for people who write without them.

## Evaluation

`tests/` runs every flow with a scripted fake LLM (free, every change).
`scripts/load_test.py` measures the pipeline and ERPNext without model calls (numbers in the README).
`scripts/smoke.py` replays real conversations against the live model and prints token
cost; run it only when the prompt or model changes.

## Cost and secrets

- Model spend is capped per day for the whole server (`MUNIM_DAILY_BUDGET_USD`), per business
  (`MUNIM_TENANT_DAILY_USD`) and for the shared demo shop (`MUNIM_DEMO_DAILY_USD`); the ledger records the business.
- Stored secrets (ERP sign-in passwords the bot uses for one-tap sign-in, Razorpay secrets) are sealed with the
  newest key in `MUNIM_DATA_KEYS` (`core/secretbox.py`); any listed key still reads. Rotate: add a key in front,
  restart (everything is re-sealed at start), then drop the old key.
