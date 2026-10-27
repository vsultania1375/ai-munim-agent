# Run inside the ERPNext backend container:
#   cd /home/frappe/frappe-bench/sites && ../env/bin/python /tmp/seed.py
# Idempotent: completes the setup wizard once, then upserts demo masters and an API user.
import json

import frappe

frappe.init(site="frontend", sites_path=".")
frappe.connect()
frappe.set_user("Administrator")

from frappe.utils import add_days, getdate, nowdate

COMPANY = "AI Munim Demo Traders"
ABBR = "AMD"

if not frappe.db.get_single_value("System Settings", "setup_complete"):
    from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

    year = getdate(nowdate()).year
    start = f"{year}-04-01" if getdate(nowdate()).month >= 4 else f"{year - 1}-04-01"
    setup_complete(
        {
            "language": "English",
            "country": "India",
            "timezone": "Asia/Kolkata",
            "currency": "INR",
            "full_name": "AI Munim Owner",
            "email": "owner@aimunim.demo",
            "company_name": COMPANY,
            "company_abbr": ABBR,
            "chart_of_accounts": "Standard",
            "fy_start_date": start,
            "fy_end_date": str(add_days(getdate(start).replace(year=getdate(start).year + 1), -1)),
            "setup_demo": 0,
        }
    )
    frappe.db.commit()

company = frappe.get_doc("Company", COMPANY)
tax_parent = frappe.db.get_value("Account", {"company": COMPANY, "account_type": "Tax", "is_group": 1}, "name") or frappe.db.get_value(
    "Account", {"company": COMPANY, "account_name": "Duties and Taxes"}, "name"
)


def tax_account(rate):
    name = f"Output GST {rate}% - {ABBR}"
    if not frappe.db.exists("Account", name):
        frappe.get_doc(
            {"doctype": "Account", "account_name": f"Output GST {rate}%", "parent_account": tax_parent,
             "company": COMPANY, "account_type": "Tax", "tax_rate": rate}
        ).insert()
    return name


def tax_template(rate):
    title = f"GST {rate}%"
    name = f"{title} - {ABBR}"
    if not frappe.db.exists("Item Tax Template", name):
        frappe.get_doc(
            {"doctype": "Item Tax Template", "title": title, "company": COMPANY,
             "taxes": [{"tax_type": tax_account(rate), "tax_rate": rate}]}
        ).insert()
    return name


for rate in (5, 18, 28):
    tax_template(rate)

# Bank ledger for UPI/bank payments, and no rupee rounding so ERP totals equal the preview.
bank_parent = frappe.db.get_value("Account", {"company": COMPANY, "account_type": "Bank", "is_group": 1}, "name")
if bank_parent and not frappe.db.exists("Account", f"Bank - {ABBR}"):
    frappe.get_doc({"doctype": "Account", "account_name": "Bank", "parent_account": bank_parent,
                    "company": COMPANY, "account_type": "Bank"}).insert()
frappe.db.set_single_value("Global Defaults", "disable_rounded_total", 1)

stc = f"AI Munim GST - {ABBR}"
if not frappe.db.exists("Sales Taxes and Charges Template", stc):
    frappe.get_doc(
        {
            "doctype": "Sales Taxes and Charges Template",
            "title": "AI Munim GST",
            "company": COMPANY,
            "is_default": 1,
            "taxes": [
                {"charge_type": "On Net Total", "account_head": tax_account(r), "description": f"GST {r}%", "rate": 0}
                for r in (5, 18, 28)
            ],
        }
    ).insert()

for group in ("Building Material", "Security Systems"):
    if not frappe.db.exists("Item Group", group):
        frappe.get_doc({"doctype": "Item Group", "item_group_name": group, "parent_item_group": "All Item Groups"}).insert()
for uom in ("Bag", "Kg", "Tonne", "Nos"):
    if not frappe.db.exists("UOM", uom):
        frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert()

ITEMS = [
    ("CEM-50", "Cement Bag 50 KG", "Bag", 450, 28, "Building Material"),
    ("TMT-12", "TMT Steel Bar 12mm", "Kg", 65, 18, "Building Material"),
    ("SAND-T", "River Sand", "Tonne", 1800, 5, "Building Material"),
    ("CCTV-2MP", "CCTV Camera 2MP", "Nos", 1850, 18, "Security Systems"),
    ("DVR-8", "DVR 8 Channel", "Nos", 4200, 18, "Security Systems"),
]
for code, name, uom, rate, gst, group in ITEMS:
    if not frappe.db.exists("Item", code):
        frappe.get_doc(
            {"doctype": "Item", "item_code": code, "item_name": name, "item_group": group, "stock_uom": uom,
             "is_stock_item": 0, "taxes": [{"item_tax_template": tax_template(gst)}]}
        ).insert()
    if not frappe.db.exists("Item Price", {"item_code": code, "price_list": "Standard Selling"}):
        frappe.get_doc({"doctype": "Item Price", "item_code": code, "price_list": "Standard Selling", "price_list_rate": rate}).insert()

CUSTOMERS = ["Gupta Traders", "Sharma Enterprises", "Verma Hardware", "Gupta Electricals"]
for name in CUSTOMERS:
    if not frappe.db.exists("Customer", name):
        frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_type": "Company",
                        "customer_group": "Commercial", "territory": "India"}).insert()

# API user for the bot (key/secret printed once to the console for the operator to store)
api_user = "munim-bot@aimunim.demo"
if not frappe.db.exists("User", api_user):
    user = frappe.get_doc({"doctype": "User", "email": api_user, "first_name": "Munim Bot", "send_welcome_email": 0,
                           "user_type": "System User"})
    user.insert()
frappe.get_doc("User", api_user).add_roles("Accounts User", "Sales User", "Sales Manager", "Sales Master Manager",
                                          "Purchase User", "Purchase Master Manager", "Accounts Manager")  # cancel needs it
frappe.db.commit()
print(json.dumps({"seed": "ok", "company": COMPANY, "items": len(ITEMS), "customers": len(CUSTOMERS)}))

frappe.destroy()
