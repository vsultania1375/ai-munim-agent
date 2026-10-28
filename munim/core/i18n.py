"""Fixed (non-LLM) user-facing text in the owner's language.

The LLM already mirrors the user's language. These deterministic messages (previews,
buttons, onboarding, errors) follow the language detected from the user's own messages:
"en" (English) or "hi" (Hinglish, Roman script). Devanagari users get "hi" for fixed text;
the LLM replies to them in Devanagari.
"""
from __future__ import annotations

import re

DEFAULT_LANG = "en"
_HINGLISH = {
    "hai", "hain", "karo", "kardo", "kar", "karna", "karni", "bana", "banao", "banado", "bnado", "bnao", "kya",
    "nahi", "nhi", "haan", "ji", "bhai", "dikhao", "bhejo", "kitna", "kitne", "wala", "wali", "aur", "chahiye",
    "mujhe", "mera", "meri", "apna", "kaise", "abhi", "kal", "ko", "ka", "ki", "ke", "mein", "hoga", "diya",
    "gaya", "raha", "rahe", "thik", "theek", "batao", "bata", "kitni", "sab", "saare", "liye", "wahi", "accha",
    "achha", "namaste", "bhaiya", "dukaan", "maal", "udhaar", "baaki", "hisaab", "paisa", "paise", "kab",
}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def detect_lang(text: str | None, current: str | None = None) -> str:
    """Sticky detection: only switch when the message carries enough signal."""
    value = (text or "").strip()
    if not value:
        return current or DEFAULT_LANG
    if _DEVANAGARI.search(value):
        return "hi"
    words = re.findall(r"\b[a-z]+\b", value.lower())  # whole words only; GSTINs, amounts and codes are ignored
    if len(words) < 2:
        return current or DEFAULT_LANG
    hits = sum(1 for w in words if w in _HINGLISH)
    if hits >= 2 or (hits == 1 and len(words) <= 4):
        return "hi"
    if hits == 0 and len(words) >= 3:
        return "en"
    return current or DEFAULT_LANG


STRINGS: dict[str, dict[str, str]] = {
    # ---- engine -------------------------------------------------------
    "error": {
        "en": "Sorry, something went wrong on my side. Please send it again in a minute.",
        "hi": "Maaf kijiye, abhi ek technical dikkat aa gayi. 1 minute baad dobara bhejein.",
    },
    "welcome": {
        "en": (
            "Hello! I'm AI Munim. You can ask me things like:\n"
            "• Make a quotation for Gupta Traders, 20 cement bags\n"
            "• Bill Sharma Enterprises for 10 kg steel bars\n"
            "• Show Verma Hardware's outstanding\n"
            "• Send a photo/PDF of a bill and I'll read it\n"
            "_This is a demo with sample data._"
        ),
        "hi": (
            "Namaste! Main AI Munim hoon. Aap mujhse aise kaam bol sakte hain:\n"
            "• Gupta ji ko 20 cement bag ka quotation bana do\n"
            "• Sharma ji ka bill bana do 10 saria\n"
            "• Verma ji ka outstanding dikhao\n"
            "• Bill ki photo/PDF bhejo, main padh lunga\n"
            "_Ye demo hai, data dummy hai._"
        ),
    },
    "welcome_business": {
        "en": "Hello! What would you like to do — a bill, quotation, challan, payment or outstanding?",
        "hi": "Namaste! Bataiye kya karna hai — bill, quotation, challan, payment ya outstanding?",
    },
    "reset_done": {"en": "Chat reset. ", "hi": "Chat reset ho gaya. "},
    "voice_unsupported": {
        "en": "Voice notes aren't supported yet. Please type your message 🙏",
        "hi": "Voice note abhi support nahi hai. Kripya text me likh dein 🙏",
    },
    "type_unsupported": {
        "en": "I can't read this message type yet. Please send text, a PDF or a photo.",
        "hi": "Ye message type main abhi nahi padh sakta. Text, PDF ya photo bhejein.",
    },
    "media_unsupported": {
        "en": "I can't read this file type. Please send a PDF or a JPG/PNG photo.",
        "hi": "Ye file type main nahi padh sakta. PDF ya JPG/PNG photo bhejein.",
    },
    "option_stale": {
        "en": "That option has expired. Please type what you need.",
        "hi": "Ye option purana ho gaya. Kripya apni baat dobara likh dein.",
    },
    "already_created": {"en": "This was already created: {label} {name}.", "hi": "Ye pehle hi ban chuka hai: {label} {name}."},
    "preview_closed": {
        "en": "That preview is no longer open. Please send a new request.",
        "hi": "Ye preview ab open nahi hai. Naya request bhej dein.",
    },
    "stale_preview": {
        "en": "That was an older preview. Here is the latest one — approve this:",
        "hi": "Ye purana preview tha. Latest preview ye hai, isse Approve karein:",
    },
    "edit_prompt": {
        "en": "Tell me what to change — e.g. *rate 470*, *qty 25*, *add a DVR* or *customer Sharma*.",
        "hi": "Batayein kya badalna hai — jaise *rate 470*, *qty 25*, *DVR bhi add karo* ya *customer Sharma ji*.",
    },
    "cancelled": {"en": "Okay, {label} preview cancelled. Anything else?", "hi": "Theek hai, {label} preview cancel kar diya. Kuch aur?"},
    "button_unknown": {
        "en": "I didn't understand that button. Please type your message.",
        "hi": "Ye button samajh nahi aaya. Kripya message likh dein.",
    },
    "approved_final": {
        "en": "✅ {label} *{name}* done ({customer}).",
        "hi": "✅ {label} *{name}* ban gaya ({customer}).",
    },
    "approved_staff": {
        "en": "✅ Draft {label} *{name}* created ({customer}). The owner will finalise it.",
        "hi": "✅ Draft {label} *{name}* ban gaya ({customer}). Owner ise final karenge.",
    },
    "approved_kept_draft": {
        "en": "⚠️ {label} *{name}* was created but ERPNext didn't allow it to be finalised (e.g. stock or a required field). It is saved as a draft — please check it in the ERP.",
        "hi": "⚠️ {label} *{name}* ban gaya, par ERPNext ne final nahi hone diya (jaise stock ya koi zaroori field). Ye draft me save hai — ERP me check kar lein.",
    },
    "erp_total": {"en": "ERP total: {total}", "hi": "ERP total: {total}"},
    "approved_payment": {
        "en": "✅ Draft Payment Entry *{name}* created ({customer}). The owner will finalise it.",
        "hi": "✅ Draft Payment Entry *{name}* ban gayi ({customer}). Owner ise final karenge.",
    },
    "customer_added": {
        "en": "✅ {name} added as a customer. What should I make for them?",
        "hi": "✅ {name} customer add ho gaye. Ab inke liye kya banana hai?",
    },
    "refusal": {
        "en": "Sorry, in this chat I can only help with your business accounts.",
        "hi": "Maaf kijiye, main is chat par sirf aapke business ke hisaab-kitaab me madad kar sakta hoon.",
    },
    "unclear": {
        "en": "I didn't quite get that. Could you add a bit more detail, e.g. quotation for Gupta Traders, 20 cement bags?",
        "hi": "Main thik se samajh nahi paaya. Thoda detail me likh dein, jaise: Gupta ji ko 20 cement bag ka quotation.",
    },
    # ---- previews -----------------------------------------------------
    "kind.quotation": {"en": "Quotation", "hi": "Quotation"},
    "kind.sales_order": {"en": "Sales Order", "hi": "Sales Order"},
    "preview.from": {"en": "From: {source}", "hi": "{source} se"},
    "queue_failed": {
        "en": "Done. I couldn't prepare the next one ({step}) — just tell me again.",
        "hi": "Ho gaya. Agla ({step}) taiyaar nahi kar paaya — ek baar fir bol dijiye.",
    },
    "via_order": {
        "en": "(ERPNext needs an order between a quotation and a challan, so Sales Order *{name}* was made too.)",
        "hi": "(ERPNext me quotation aur challan ke beech order zaroori hai, isliye Sales Order *{name}* bhi ban gaya.)",
    },
    "order_kept_draft": {
        "en": "⚠️ Sales Order *{name}* was made but ERPNext didn't allow it to be finalised, so the challan wasn't made. Please check it in the ERP.",
        "hi": "⚠️ Sales Order *{name}* bana par ERPNext ne final nahi hone diya, isliye challan nahi bana. ERP me check kar lein.",
    },
    "kind.invoice": {"en": "Sales Invoice", "hi": "Sales Invoice"},
    "kind.delivery_note": {"en": "Delivery Challan", "hi": "Delivery Challan"},
    "kind.payment": {"en": "Payment Received", "hi": "Payment Received"},
    "kind.new_customer": {"en": "New Customer", "hi": "Naya Customer"},
    "preview": {"en": "Preview", "hi": "Preview"},
    "mcp.no_business": {"en": "This WhatsApp number has no AI Munim business yet. Message the bot to set one up, then connect again.",
                        "hi": "Is WhatsApp number pe abhi koi AI Munim business nahi hai. Pehle bot ko message karke setup karein, phir connect karein."},
    "sum.head": {"en": "☀️ *Good morning — {day}*", "hi": "☀️ *Suprabhat — {day}*"},
    "sum.sales": {"en": "🧾 Yesterday's sales: {total} ({bills} bills)", "hi": "🧾 Kal ki bikri: {total} ({bills} bill)"},
    "sum.received": {"en": "💰 Received yesterday: {total} ({n} payments)", "hi": "💰 Kal aaya paisa: {total} ({n} payment)"},
    "sum.due": {"en": "📒 Due from customers: {total} ({n} customers)", "hi": "📒 Customers pe baaki: {total} ({n} customer)"},
    "sum.late": {"en": " — {days} days late", "hi": " — {days} din se late"},
    "sum.no_due": {"en": "📒 No dues pending 🎉", "hi": "📒 Koi baaki nahi 🎉"},
    "sum.pending": {"en": "⏳ {n} open quotations/orders/drafts — ask me 'kya pending hai'",
                    "hi": "⏳ {n} quotation/order/draft khule hain — 'kya pending hai' poochiye"},
    "sum.footer": {"en": "_Reply 'summary band' to stop this daily message._",
                   "hi": "_Ye roz ka message band karna ho toh 'summary band' likhein._"},
    "sum.off": {"en": "Okay, no more daily summary. 'summary chalu' brings it back.",
                "hi": "Theek hai, roz ki summary band. Wapas chahiye toh 'summary chalu' likhein."},
    "sum.on": {"en": "✅ Daily summary on — every morning around 9.", "hi": "✅ Roz ki summary chalu — har subah lagbhag 9 baje."},
    "erp_event.made": {"en": "🖥️ In the ERP: {doctype} *{name}* finalised — {party} {total}",
                       "hi": "🖥️ ERP me: {doctype} *{name}* final hua — {party} {total}"},
    "erp_event.repeat": {"en": "🔁 This month's recurring bill is made: {doctype} *{name}* — {party} {total}",
                         "hi": "🔁 Har mahine wala bill ban gaya: {doctype} *{name}* — {party} {total}"},
    "staff.draft_waiting": {"en": "👤 {who} made a draft {doctype} *{name}* ({customer}{total}). Finalise it?",
                            "hi": "👤 {who} ne draft {doctype} *{name}* banaya ({customer}{total}). Final karein?"},
    "staff.btn_final": {"en": "✅ Finalise", "hi": "✅ Final karo"},
    "staff.btn_later": {"en": "⏳ Later", "hi": "⏳ Baad me"},
    "staff.finalised": {"en": "✅ *{name}* is final.", "hi": "✅ *{name}* final ho gaya."},
    "staff.later": {"en": "Okay, it stays a draft.", "hi": "Theek hai, draft hi rahega."},
    "owner_only": {"en": "Only the owner can do that.", "hi": "Ye sirf owner kar sakte hain."},
    "kind.repeat": {"en": "Recurring bill", "hi": "Har baar ka bill"},
    "repeat.card": {
        "en": "*🔁 Recurring bill — Preview*\nCopy of Sales Invoice *{name}* ({customer}, {total})\nEvery: {freq}\nFirst new bill: {first}\n\nEach copy is made {final} and you get a WhatsApp. Tap Approve to set it up.",
        "hi": "*🔁 Har baar ka bill — Preview*\nSales Invoice *{name}* ki copy ({customer}, {total})\nKab: {freq}\nPehla naya bill: {first}\n\nHar copy {final} banegi aur aapko WhatsApp aayega. Set karne ke liye Approve dabaiye.",
    },
    "repeat.final": {"en": "final", "hi": "final"},
    "repeat.draft": {"en": "as a draft", "hi": "draft"},
    "repeat.done": {"en": "🔁 Set: a copy of *{name}* {freq}, first on {first} ({auto}).",
                    "hi": "🔁 Set ho gaya: *{name}* ki copy {freq}, pehli {first} ko ({auto})."},
    "pay.link": {"en": "💳 Pay link for *{name}* (forward it to the customer): {url}{test}",
                 "hi": "💳 *{name}* ka pay link (customer ko forward kar dein): {url}{test}"},
    "pay.test_mode": {"en": "\n_Razorpay test mode: no real money._", "hi": "\n_Razorpay test mode: asli paisa nahi katega._"},
    "pay.received": {"en": "💰 {customer} paid {amount} online ({method}) for *{invoice}*. Payment Entry *{entry}* is made; the bill is paid.",
                     "hi": "💰 {customer} ne *{invoice}* ke {amount} online ({method}) pay kiye. Payment Entry *{entry}* ban gayi, bill clear."},
    "pay.setup_link": {"en": "🔐 Connect your Razorpay here (link works once, 15 min): {url}\nKeys go on that page, never in chat.",
                       "hi": "🔐 Apna Razorpay yahan jodiye (link ek baar, 15 min): {url}\nKeys sirf us page pe daalein, chat me kabhi nahi."},
    "kind.credit_note": {"en": "Credit Note (Return)", "hi": "Credit Note (Maal wapsi)"},
    "kind.cancel": {"en": "Cancel", "hi": "Cancel"},
    "preview.against": {"en": "Against: {source}", "hi": "{source} ke against"},
    "preview.replaces": {"en": "Corrects *{name}* (it will be cancelled)", "hi": "*{name}* ki jagah (purana cancel hoga)"},
    "preview.customer_discount": {"en": "Their permanent {pct}% discount is in these rates",
                                  "hi": "Inka permanent {pct}% discount in rates me laga hai"},
    "voice_on": {"en": "🔊 Voice replies on: when you send a voice note, I answer in voice too.",
                 "hi": "🔊 Voice reply chalu: aap voice note bhejenge toh main bhi awaaz me jawab dunga."},
    "voice_off": {"en": "🔇 Voice replies off. 'voice reply chalu' turns them back on.",
                  "hi": "🔇 Voice reply band. Wapas chahiye toh 'voice reply chalu' likhein."},
    "bahi.title": {"en": "Bahi page — Preview", "hi": "Bahi ka panna — Preview"},
    "bahi.row": {"en": "given {given}, received {received} → due {due}", "hi": "diya {given}, jama {received} → baaki {due}"},
    "bahi.total": {"en": "To book as old dues: {total} ({n} parties)", "hi": "Purane baaki khaate me: {total} ({n} party)"},
    "bahi.skipped": {"en": "⚠️ {n} unclear lines left out — check the photo", "hi": "⚠️ {n} line saaf nahi thi, chhod di — photo dekh lein"},
    "bahi.note": {"en": "🆕 = new customer. Approve books each due with the photo attached.",
                  "hi": "🆕 = naya customer. Approve dabate hi har baaki photo ke saath khaate me chadhega."},
    "bahi.done": {"en": "📒 Booked {n} old dues ({new} new customers):", "hi": "📒 {n} purane baaki khaate me chadh gaye ({new} naye customer):"},
    "brand.card": {
        "en": "*Bill design — Preview*\nLogo: {logo}\nColours: {name} {primary} + accent {accent}{bg}\n{bank}\n\nTap Approve and every bill, quotation, order and challan uses this design.",
        "hi": "*Bill design — Preview*\nLogo: {logo}\nRang: {name} {primary} + accent {accent}{bg}\n{bank}\n\nApprove dabate hi har bill, quotation, order aur challan isi design me banega.",
    },
    "brand.sign": {"en": "Signature: {mark}", "hi": "Sign: {mark}"},
    "brand.terms": {"en": "Terms: {terms}", "hi": "Terms: {terms}"},
    "brand.footer": {"en": "Footer: {footer}", "hi": "Neeche ki line: {footer}"},
    "brand.bg": {"en": " (logo background {bg} left out)", "hi": " (logo ka background {bg} chhod diya)"},
    "brand.no_bank": {"en": "Bank/UPI: not added (tell me to add them)", "hi": "Bank/UPI: nahi daala (batayein toh jod dunga)"},
    "brand.done": {"en": "🎨 Done — your bills now carry your logo and colours.", "hi": "🎨 Ho gaya — ab aapke bills pe aapka logo aur rang aayega."},
    "brand.sample": {"en": "🎨 Here is {name} in the new design.", "hi": "🎨 Ye raha {name} naye design me."},
    "brand.failed": {"en": "Couldn't apply the design right now — please try again in a bit.", "hi": "Abhi design lag nahi paaya — thodi der baad fir koshish karein."},
    "pricing.card": {
        "en": "*Permanent discount — Preview*\nCustomer: {customer}\nEvery future bill: *{pct}%* off the price list (now: {before}%)\n\nThe web ERP applies it too. Tap Approve to save.",
        "hi": "*Permanent discount — Preview*\nCustomer: {customer}\nAage har bill pe price list se *{pct}%* kam (abhi: {before}%)\n\nWeb ERP me bhi lagega. Save karne ke liye Approve dabaiye.",
    },
    "pricing.done": {"en": "✅ {customer}: {pct}% off on every bill from now.", "hi": "✅ {customer}: ab se har bill pe {pct}% discount."},
    "pricing.off": {"en": "✅ {customer}: permanent discount switched off.", "hi": "✅ {customer}: permanent discount band kar diya."},
    "preview.discount": {"en": "Discount {pct}% applied to the rates", "hi": "Rates par {pct}% discount laga hai"},
    "credit_effect": {"en": "{total} will come off what is due on {source}.", "hi": "{total} {source} ke baaki me se kam ho jaayega."},
    "cancel.title": {"en": "Cancel this document?", "hi": "Ye document cancel karein?"},
    "cancel.effect": {
        "en": "Its entries are reversed; it stays in the ERP marked Cancelled (nothing is deleted). Tap Approve to cancel.",
        "hi": "Iski entries ulat jaayengi; ERP me ye Cancelled dikhega (kuch delete nahi hota). Cancel ke liye Approve dabaiye.",
    },
    "cancel.done": {"en": "❌ {doctype} *{name}* cancelled.", "hi": "❌ {doctype} *{name}* cancel ho gaya."},
    "cancel.failed": {"en": "ERPNext didn't allow cancelling *{name}*: {reason}", "hi": "ERPNext ne *{name}* cancel nahi hone diya: {reason}"},
    "corrected": {"en": "🔁 *{old}* cancelled and replaced by the corrected one.", "hi": "🔁 *{old}* cancel karke sahi wala bana diya."},
    "kind.purchase": {"en": "Purchase Bill", "hi": "Kharid Bill"},
    "kind.supplier_payment": {"en": "Payment to Supplier", "hi": "Supplier ko Payment"},
    "kind.debit_note": {"en": "Debit Note (Purchase Return)", "hi": "Debit Note (Kharid maal wapsi)"},
    "debit_effect": {"en": "This takes {total} off what you owe on {source}.", "hi": "Isse {source} pe aapka dena {total} kam hoga."},
    "kind.expense": {"en": "Expense", "hi": "Kharcha"},
    "kind.new_supplier": {"en": "New Supplier", "hi": "Naya Supplier"},
    "label.supplier": {"en": "Supplier", "hi": "Supplier"},
    "label.bill": {"en": "Bill no. {no}, dated {date}", "hi": "Bill no. {no}, tareekh {date}"},
    "label.head": {"en": "Head", "hi": "Khaata"},
    "label.paid_from": {"en": "Paid by", "hi": "Kaise diya"},
    "label.note": {"en": "Note", "hi": "Note"},
    "gst_none_supplier": {"en": "GST: none (supplier not GST-registered)", "hi": "GST: nahi (supplier GST me registered nahi)"},
    "bill_attached": {"en": "📎 The bill you sent will be attached.", "hi": "📎 Aapka bheja bill saath me attach hoga."},
    "record_note_final": {"en": "Tap Approve and it goes into your books.", "hi": "Approve dabate hi khaate me chadh jaayega."},
    "supplier_added": {"en": "✅ {name} added as a supplier.", "hi": "✅ {name} supplier add ho gaye."},
    "gst_none": {"en": "GST: none (business not GST-registered)", "hi": "GST: nahi (business GST me registered nahi)"},
    "hsn_missing": {
        "en": "⚠️ HSN code needed for: {items}. Tell me the code (4/6/8 digits) and I'll save it.",
        "hi": "⚠️ In items ka HSN code chahiye: {items}. Code (4/6/8 digit) bata dijiye, main save kar dunga.",
    },
    "approve_note_final": {
        "en": "Tap Approve and it's done — saved in your books, PDF comes here.",
        "hi": "Approve dabate hi ban jaayega — khaate me save, PDF yahin aa jaayegi.",
    },
    "payment_note_final": {
        "en": "Tap Approve to record the payment.",
        "hi": "Approve dabate hi payment khaate me chadh jaayegi.",
    },
    "approve_note": {
        "en": "Tap Approve to create a draft (it will not be submitted).",
        "hi": "Approve dabane par draft banega (submit nahi hoga).",
    },
    "payment_note": {
        "en": "Tap Approve to create a draft payment entry.",
        "hi": "Approve dabane par draft payment entry banegi.",
    },
    "customer_note": {"en": "Tap Approve to add the customer.", "hi": "Approve dabane par customer add ho jaayega."},
    "supplier_note": {"en": "Tap Approve to add the supplier.", "hi": "Approve dabane par supplier add ho jaayega."},
    "label.name": {"en": "Name", "hi": "Naam"},
    "label.amount": {"en": "Amount", "hi": "Amount"},
    "label.mode": {"en": "Mode", "hi": "Mode"},
    # ---- web access ---------------------------------------------------
    "web.head_new": {"en": "🖥️ *Your ERP also works on the web*", "hi": "🖥️ *Aapka ERP web pe bhi chalta hai*"},
    "web.head_reset": {"en": "🔑 *New password set*", "hi": "🔑 *Naya password set ho gaya*"},
    "web.body": {
        "en": (
            "Open it in any computer or phone browser. Please change the password after your first login "
            "(click your name → My Settings → Change Password).\n"
            "Forgot it? Type *password reset* here. Don't forward this message."
        ),
        "hi": (
            "Computer ya phone ke browser me kholein. Pehli baar login karke password badal lein "
            "(upar naam pe click → My Settings → Change Password).\n"
            "Password bhool jaayein toh yahan *password reset* likhein. Ye message kisi ko forward na karein."
        ),
    },
    "web.sso_link": {
        "en": "🖥️ *Open your ERP* — no password needed:\n{url}\n\nThe link works once, for 15 minutes. Need it again? Type *ERP login*. Please don't forward it.",
        "hi": "🖥️ *Apna ERP kholiye* — password ki zaroorat nahi:\n{url}\n\nLink ek baar, 15 minute tak chalega. Dobara chahiye? *ERP login* likhein. Kisi ko forward na karein.",
    },
    "web.demo_only": {
        "en": "🖥️ Web login comes with your own account. Type *setup* to create one in 2 minutes.",
        "hi": "🖥️ Web login aapke apne account ke saath milta hai. *setup* likh ke 2 minute me account banaiye.",
    },
    "web.not_ready": {
        "en": "Web login isn't ready for this account yet. Please try again shortly.",
        "hi": "Web login abhi is account ke liye ready nahi hai. Thodi der me try karein.",
    },
    "web.failed": {
        "en": "I couldn't set up the web login. Please type *ERP login* again in a while.",
        "hi": "Web login banane me dikkat aayi. Thodi der baad *ERP login* likh ke dobara try karein.",
    },
}


STRINGS.update({
    # ---- onboarding ---------------------------------------------------
    "ob.welcome": {
        "en": (
            "🙏 Hello{name}! I'm *AI Munim* — your accounts assistant on WhatsApp.\n\n"
            "Bills, quotations, challans, payments and dues — just type or say it. "
            "Setup takes *2 minutes*, no app to download."
        ),
        "hi": (
            "🙏 Namaste{name}! Main *AI Munim* hoon — aapka WhatsApp pe hisaab-kitaab wala munim.\n\n"
            "Bill, quotation, challan, payment aur udhaar — sab bas bol ke ya likh ke. "
            "Setup me sirf *2 minute* lagte hain, koi app download nahi."
        ),
    },
    "ob.btn_start": {"en": "🚀 Start setup", "hi": "🚀 Setup shuru"},
    "ob.btn_demo": {"en": "👀 See a demo", "hi": "👀 Pehle demo"},
    "ob.ask_identity": {
        "en": (
            "Let's begin 👍 Send *any one* of these:\n"
            "1️⃣ Your *GSTIN* (15 characters)\n"
            "2️⃣ A *photo/PDF* of your GST certificate, visiting card or one of your bills\n\n"
            "I'll pick up the details myself."
        ),
        "hi": (
            "Shuru karte hain 👍 Inme se koi *ek* bhejein:\n"
            "1️⃣ Apna *GSTIN* (15 akshar)\n"
            "2️⃣ GST certificate, visiting card ya apne bill ki *photo/PDF*\n\n"
            "Main details khud nikal lunga."
        ),
    },
    "ob.btn_nogst": {"en": "I don't have GST", "hi": "GST nahi hai"},
    "ob.demo_on": {
        "en": (
            "🙏 Hi! I'm *AI Munim*, an accountant on WhatsApp. You're in a *demo shop* (Gupta Traders, cement, CCTV...) — "
            "just type or speak, nothing to set up. Try:\n"
            "• Gupta Traders ko 20 cement bag ka bill bana do\n• Verma Hardware ka outstanding dikhao\n"
            "• This month's sales  • Send a voice note\n\n"
            "For your own business (with your GST), tap below any time."
        ),
        "hi": (
            "🙏 Namaste! Main *AI Munim* hoon, WhatsApp pe aapka munim. Aap ek *demo dukaan* me hain (Gupta Traders, "
            "cement, CCTV...) — bas likhiye ya boliye, kuch setup nahi. Try karein:\n"
            "• Gupta Traders ko 20 cement bag ka bill bana do\n• Verma ji ka outstanding dikhao\n"
            "• Is mahine ki sale  • Voice note bhejiye\n\n"
            "Apne business ke liye (apne GST ke saath) neeche dabaiye, kabhi bhi."
        ),
    },
    "demo.limit": {
        "en": "🙏 That was the demo ({n} messages). Set up your own business to keep going — your own books, with your GST, in 2 minutes.",
        "hi": "🙏 Demo ke {n} message ho gaye. Aage chalane ke liye apna business setup karein — apne GST ke saath, apni khud ki books, 2 minute me.",
    },
    "demo.few_left": {
        "en": "{left} demo messages left. Like it? Set up your own business any time.",
        "hi": "Demo ke {left} message bache hain. Pasand aaya? Apna business kabhi bhi setup karein.",
    },
    "ob.btn_own_setup": {"en": "🚀 Set up my own", "hi": "🚀 Apna setup karo"},
    "ob.error": {"en": "Sorry, something went wrong during setup. Try again?", "hi": "Maaf kijiye, setup me ek dikkat aa gayi. Dobara try karein?"},
    "ob.btn_retry": {"en": "🔄 Try again", "hi": "🔄 Dobara try"},
    "ob.edit_prompt": {
        "en": "Sure — type the correct details (name, city or GSTIN) and I'll update them.",
        "hi": "Theek hai — sahi details likh dein (naam, shehar ya GSTIN), main update kar dunga.",
    },
    "ob.busy": {"en": "⏳ Setup is still running, just a moment...", "hi": "⏳ Setup abhi chal raha hai, bas thoda sa intezaar..."},
    "ob.type_question": {
        "en": "What kind of business is it? (I'll set the right units and GST rates)",
        "hi": "Aapka business kis type ka hai? (Isse main sahi units aur GST rates set karunga)",
    },
    "ob.type_other": {"en": "Something else", "hi": "Kuch aur"},
    "ob.type_other_desc": {"en": "Any other business", "hi": "Koi bhi aur business"},
    "ob.list_button": {"en": "Choose", "hi": "Chuniye"},
    "ob.gstin_retry": {"en": "⚠️ {reason}\nPlease check and send it again, or send a photo.", "hi": "⚠️ {reason}\nDobara check karke bhejein, ya photo bhej dein."},
    "ob.gstin_ok_need_name": {
        "en": "✅ GSTIN looks valid — *{state}*, {holder}.\nNow just type your *shop/business name* and *city* (e.g. Sharma Hardware, Rohtak).",
        "hi": "✅ GSTIN sahi hai — *{state}*, {holder}.\nAb bas apni *dukaan/business ka naam* aur *shehar* likh dein (jaise: Sharma Hardware, Rohtak).",
    },
    "ob.no_gst_ok": {
        "en": "No problem 👍 Just type your *shop/business name* and *city* (e.g. Sharma Hardware, Rohtak).",
        "hi": "Koi baat nahi 👍 Bas apni *dukaan/business ka naam* aur *shehar* likh dein (jaise: Sharma Hardware, Rohtak).",
    },
    "ob.file_later": {
        "en": "I'll take that data file a little later 🙂 First, your business: GSTIN or a photo of your GST certificate/bill.",
        "hi": "Ye data file baad me lunga 🙂 Pehle business ki pehchaan: GSTIN ya GST certificate/bill ki photo bhejein.",
    },
    "ob.nothing_found": {
        "en": "I couldn't find business details in that. Send your GSTIN, or your business name + city.",
        "hi": "Mujhe isme business ki details nahi mili. GSTIN, ya naam + shehar likh dein.",
    },
    "ob.need_name": {
        "en": "I couldn't find the business name. Please type your *shop name* and *city*.",
        "hi": "Business ka naam nahi mila. Kripya *dukaan ka naam* aur *shehar* likh dein.",
    },
    "ob.confirm_head": {"en": "*Are these details right?*", "hi": "*Ye details sahi hain?*"},
    "ob.no_gstin": {"en": "🧾 GSTIN: none (you can add it later)", "hi": "🧾 GSTIN: nahi hai (baad me jod sakte hain)"},
    "ob.photo_gstin_bad": {"en": "⚠️ The GSTIN in the photo didn't check out: {reason}", "hi": "⚠️ Photo wala GSTIN sahi nahi laga: {reason}"},
    "ob.btn_ok": {"en": "✅ Correct", "hi": "✅ Sahi hai"},
    "ob.btn_edit": {"en": "✏️ Change", "hi": "✏️ Badlo"},
    "ob.waitlist": {
        "en": "🙏 New accounts aren't open right now. You can use the *demo* meanwhile; type *setup* again later to try.",
        "hi": "🙏 Abhi naye account nahi khul rahe. Tab tak *demo* use karein; thodi der baad *setup* likh ke dobara try karein.",
    },
    "ob.pool_full": {
        "en": "🙏 Today's new-account slots are full. Try the *demo* meanwhile and type *setup* again tomorrow.",
        "hi": "🙏 Aaj ke naye account slots bhar gaye. Tab tak *demo* try karein aur kal *setup* likh ke dobara shuru karein.",
    },
    "voice_heard": {"en": "🎤 I heard: _{text}_", "hi": "🎤 Maine suna: _{text}_"},
    "voice_unclear": {
        "en": "🎤 I couldn't make out that voice note. Please say it again a little slower, or type it.",
        "hi": "🎤 Voice note saaf samajh nahi aaya. Thoda dheere dobara bolein, ya likh dein.",
    },
    "video_unsupported": {"en": "I can't watch videos. Please send a photo or PDF instead.", "hi": "Video main nahi dekh sakta. Photo ya PDF bhej dein."},
    "reaction_card": {
        "en": "{emoji} noted — a reaction doesn't create anything. Tap ✅ Approve on the preview when it's right.",
        "hi": "{emoji} dekha — reaction se kuch nahi banta. Sahi hai toh preview pe ✅ Approve dabaiye.",
    },
    "number_changed": {
        "en": "📱 You changed your WhatsApp number — your business, chats and ERP access have moved to this number.",
        "hi": "📱 Aapne WhatsApp number badla hai — aapka business, chat aur ERP access is naye number pe aa gaya hai.",
    },
    "location_noted": {
        "en": "📍 Got the location. Tell me whose address this is and I'll note it with the customer.",
        "hi": "📍 Location mil gayi. Batayein ye kis customer ka pata hai, main unke saath note kar dunga.",
    },
    "sheet_own_account": {
        "en": "📊 Spreadsheet import works in your own account. Type *setup* to create one.",
        "hi": "📊 Excel/CSV import aapke apne account me hota hai. *setup* likh ke account banaiye.",
    },
    "file_too_big": {"en": "That file is too big (max {mb} MB). Please send a smaller one.", "hi": "File bahut badi hai (max {mb} MB). Chhoti file bhejein."},
    "contact_no_name": {"en": "That contact has no name. Please type the customer's name.", "hi": "Is contact me naam nahi hai. Customer ka naam likh dein."},
    "portal.login_ok": {
        "en": "✅ Verified. Go back to the browser — your ERP page is opening.",
        "hi": "✅ Verify ho gaya. Browser pe wapas jaayein — aapka ERP page khul raha hai.",
    },
    "portal.login_bad": {
        "en": "That login code has expired or was already used. Please start again on the website.",
        "hi": "Ye login code purana ho gaya ya use ho chuka hai. Website pe dobara shuru karein.",
    },
    "team.joined": {
        "en": "✅ You've joined *{company}* on AI Munim. Make bills, quotations or payments right here. Type *ERP login* for web access.",
        "hi": "✅ Aap *{company}* ki team me jud gaye. Yahin bill, quotation ya payment banaiye. Web ke liye *ERP login* likhein.",
    },
    "team.bad_code": {
        "en": "That invite code isn't valid any more. Please ask the owner for a new one.",
        "hi": "Ye invite code ab valid nahi hai. Owner se naya code maang lein.",
    },
    "team.already": {
        "en": "This number already runs *{company}*. A number can belong to one business.",
        "hi": "Ye number pehle se *{company}* se juda hai. Ek number ek hi business me ho sakta hai.",
    },
    "team.member_joined": {
        "en": "👥 +{phone} joined your team on AI Munim.",
        "hi": "👥 +{phone} aapki team me jud gaye.",
    },
    "team.invite": {
        "en": "👥 *Your team*\n{members}\n\nTo add someone, forward this to them (valid 24 h, one use):\n{link}\n\nOr they can send *JOIN {code}* to this number. Manage the team at https://{domain}",
        "hi": "👥 *Aapki team*\n{members}\n\nKisi ko jodne ke liye unhe ye forward karein (24 ghante, ek baar):\n{link}\n\nYa woh is number pe *JOIN {code}* bhej dein. Team ko https://{domain} pe manage karein.",
    },
    "team.none": {"en": "• only you so far", "hi": "• abhi sirf aap"},
    "team.owner_only": {
        "en": "Only the business owner can add team members.",
        "hi": "Team me log sirf business owner jod sakte hain.",
    },
    "btn.yes_submit": {"en": "✅ Yes, submit", "hi": "✅ Haan, submit"},
    "btn.no": {"en": "❌ No", "hi": "❌ Nahi"},
    "submit_confirm": {
        "en": "Submitting *{name}* makes it final in your books (it can only be cancelled later, not edited). Submit?",
        "hi": "Submit karne ke baad *{name}* final ho jaayega (baad me sirf cancel ho sakta hai, edit nahi). Submit karein?",
    },
    "submitted": {"en": "✅ *{name}* submitted.", "hi": "✅ *{name}* submit ho gaya."},
    "submit_later": {"en": "Okay, it stays a draft. You can submit it from the ERP any time.", "hi": "Theek hai, draft hi rahega. ERP se kabhi bhi submit kar sakte hain."},
    "submit_staff_only": {"en": "Only the owner can submit documents. Your draft is saved.", "hi": "Submit sirf owner kar sakte hain. Aapka draft save hai."},
    "submit_failed": {
        "en": "ERPNext didn't accept the submit for *{name}*. It is still a draft — please check it in the ERP.",
        "hi": "ERPNext ne *{name}* submit nahi hone diya. Ye abhi draft hai — ERP me check kar lein.",
    },
    "pay.against": {"en": "Against open bills (oldest first):", "hi": "Purane bills me adjust (sabse purana pehle):"},
    "pay.advance": {"en": "Advance (no open bill): {amount}", "hi": "Advance (koi khula bill nahi): {amount}"},
    "busy": {
        "en": "🙏 I'm at today's usage limit. Please try again tomorrow — your data is safe.",
        "hi": "🙏 Aaj ki limit poori ho gayi hai. Kal dobara try karein — aapka data safe hai.",
    },
    "ob.creating": {
        "en": "⏳ Creating your own separate books (ERPNext) — 30-60 seconds. Your data stays only yours 🔒",
        "hi": "⏳ Aapka apna alag hisaab-khata (ERPNext) bana raha hoon — 30-60 second. Aapka data sirf aapka rahega 🔒",
    },
    "ob.ready": {
        "en": "✅ *{company}* is ready!\n\nWant to bring your old data (customers, items/rate list, dues)? I'll set it up for you.",
        "hi": "✅ *{company}* ka account ready hai!\n\nKya purana data (customers, items/rate list, udhaar) laana hai? Main khud set kar dunga.",
    },
    "mg.btn_file": {"en": "📊 Send Excel/CSV", "hi": "📊 Excel/CSV bhejo"},
    "mg.btn_tally": {"en": "📒 From Tally/Vyapar", "hi": "📒 Tally/Vyapar se"},
    "mg.btn_skip": {"en": "⏭️ Later", "hi": "⏭️ Baad me"},
    "mg.send_file": {
        "en": "📎 Send an Excel (.xlsx) or CSV — a customer list, item/rate list, or a Tally closing-balance report. One at a time is fine. A photo of your register works too.",
        "hi": "📎 Excel (.xlsx) ya CSV bhej dein — customers ki list, items/rate list, ya Tally ka closing balance. Ek-ek karke bhi bhej sakte hain. Register ki photo bhi chalegi.",
    },
    "mg.tally_help": {
        "en": (
            "*How to export your old data:*\n\n"
            "*Tally Prime:* Display More Reports → List of Accounts (or Sundry Debtors) → Alt+E Export → Excel.\n"
            "For items: Stock Summary → Alt+E → Excel.\n\n"
            "*Vyapar:* Menu → Utilities → Export Items / Export Parties → Excel.\n\n"
            "*Khatabook:* Customer list → ⋮ → Download Report (PDF/Excel).\n\n"
            "Any Excel/CSV with names, phones, rates or balances works. Send it here 📎"
        ),
        "hi": (
            "*Purana data kaise nikaalein:*\n\n"
            "*Tally Prime:* Display More Reports → List of Accounts (ya Sundry Debtors) → Alt+E Export → Excel.\n"
            "Items ke liye: Stock Summary → Alt+E → Excel.\n\n"
            "*Vyapar:* Menu → Utilities → Export Items / Export Parties → Excel.\n\n"
            "*Khatabook:* Customer list → ⋮ → Download Report (PDF/Excel).\n\n"
            "Koi bhi Excel/CSV chalega jisme naam, phone, rate ya baaki ho. Yahin bhej dein 📎"
        ),
    },
    "mg.cancelled": {"en": "Okay, import cancelled. Anything else to send?", "hi": "Theek hai, import cancel. Aur kuch bhejna hai?"},
    "mg.btn_other_file": {"en": "📊 Another file", "hi": "📊 Doosri file"},
    "mg.btn_done": {"en": "✅ Done, let's start", "hi": "✅ Bas, shuru karo"},
    "mg.btn_start": {"en": "✅ Let's start", "hi": "✅ Shuru karo"},
    "mg.nudge": {
        "en": "If you want to bring old data, send a file/photo — otherwise let's start.",
        "hi": "Purana data bhejna hai to file/photo bhej dein, warna seedha shuru karte hain.",
    },
    "mg.send_any": {"en": "Send an Excel/CSV or a photo and I'll show an import preview.", "hi": "Excel/CSV ya photo bhejein, main import ka preview dikhaunga."},
    "mg.not_understood": {
        "en": "I couldn't find customer or item columns in this file 😕 The first row should have headings (Name, Phone, Rate, Balance...).",
        "hi": "Is file me customers ya items ke columns samajh nahi aaye 😕 Pehli row me headings (Name, Phone, Rate, Balance...) honi chahiye.",
    },
    "mg.btn_how_export": {"en": "📒 How to export", "hi": "📒 Export kaise karein"},
    "mg.preview_head": {"en": "*Import preview — {source}*", "hi": "*Import preview — {source}*"},
    "mg.balances_note": {
        "en": "\n\nEach customer's old due goes into their account (an opening invoice), so it shows as outstanding and payments settle it.",
        "hi": "\n\nHar customer ka purana baaki unke khaate me chadh jaayega (opening invoice) — outstanding me dikhega aur payment aane pe adjust hoga.",
    },
    "mg.done_balances": {"en": "💰 {n} old dues in the books (₹{total})", "hi": "💰 {n} purane baaki khaate me (₹{total})"},
    "mg.btn_import": {"en": "✅ Import", "hi": "✅ Import karo"},
    "mg.btn_cancel": {"en": "❌ Skip it", "hi": "❌ Rehne do"},
    "mg.nothing_pending": {"en": "There's no file waiting to import. Send one 📎", "hi": "Import ke liye koi file pending nahi hai. File bhej dein 📎"},
    "mg.done_head": {"en": "✅ *Import done*", "hi": "✅ *Import ho gaya*"},
    "mg.failed_rows": {"en": "⚠️ {n} rows were not added (duplicate/incomplete name).", "hi": "⚠️ {n} rows add nahi hui (naam duplicate/adhoora)."},
    "mg.btn_more": {"en": "📊 More files", "hi": "📊 Aur file"},
    "mg.sum_customers": {"en": "👥 {n} customers", "hi": "👥 {n} customers"},
    "mg.sum_items": {"en": "📦 {n} items (with rate: {r})", "hi": "📦 {n} items (rate ke saath: {r})"},
    "mg.sum_balances": {"en": "💰 old dues for {n} customers (total ₹{total})", "hi": "💰 {n} customers ka purana baaki (kul ₹{total})"},
    "mg.sum_empty": {"en": "Nothing usable found.", "hi": "Kuch kaam ka data nahi mila."},
    "mg.sum_no_gst": {
        "en": "⚠️ {n} items had no GST % — I'll add them without GST; you can change it later.",
        "hi": "⚠️ {n} items me GST % nahi mila — unhe bina GST ke daalunga, baad me badal sakte hain.",
    },
    "mg.sum_skipped": {"en": "Skipped {n} blank/incomplete rows.", "hi": "{n} khaali/adhoori rows chhod di."},
    "ob.live": {
        "en": (
            "🎉 *{company}* is live!\n\nJust type or say:\n"
            "• Bill Sharma for 10 bags of cement\n"
            "• Add a new customer: Gupta Traders\n"
            "• Who owes me money?\n"
            "• Send a bill photo — I'll read it\n\n"
            "I only create *drafts*; nothing goes into the ERP until you tap Approve. 🙏\n"
            "Want the full ERP on a computer? Type *ERP login*."
        ),
        "hi": (
            "🎉 *{company}* ab live hai!\n\nAise bolein ya likhein:\n"
            "• Sharma ji ko 10 bag cement ka bill bana do\n"
            "• Naya customer add karo: Gupta Traders\n"
            "• Kaun kaun udhaar pe hai?\n"
            "• Bill ki photo bhejo — main padh lunga\n\n"
            "Main sirf *draft* banata hoon; aap Approve dabate ho tabhi ERP me jaata hai. 🙏\n"
            "Computer pe poora ERP chahiye? *ERP login* likhein."
        ),
    },
    # ---- GSTIN check reasons -------------------------------------------
    "gst.len": {"en": "A GSTIN has 15 characters; yours has {n}.", "hi": "GSTIN 15 characters ka hota hai, aapke message me {n} hain."},
    "gst.format": {"en": "That doesn't look like a GSTIN (e.g. 06ABCDE1234F1Z5).", "hi": "GSTIN ka format sahi nahi lag raha (jaise 06ABCDE1234F1Z5)."},
    "gst.state": {"en": "The first 2 digits ({code}) aren't a state code.", "hi": "GSTIN ke pehle 2 ank ({code}) kisi state ka code nahi hain."},
    "gst.checksum": {
        "en": "The last character doesn't match — a letter was probably mistyped.",
        "hi": "GSTIN ka aakhri character match nahi kar raha — shayad koi akshar galat type hua hai.",
    },
})


def t(key: str, lang: str | None = None, **values: object) -> str:
    entry = STRINGS[key]
    template = entry.get(lang or DEFAULT_LANG) or entry[DEFAULT_LANG]
    return template.format(**values) if values else template


def kind_label(kind: str, lang: str | None = None) -> str:
    return t(f"kind.{kind}", lang)
