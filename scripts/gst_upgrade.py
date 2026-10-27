"""Bring existing sites onto India Compliance GST (idempotent; safe to re-run).

Run inside the bot container after India Compliance is installed on every site:
    docker exec -w /app aimunim-bot-1 python scripts/gst_upgrade.py
It applies provisioning's GST setup to the demo site and every claimed business site, using the
GSTIN/address the owner gave at onboarding. The demo also gets sample HSN codes and one
out-of-state (Rajasthan) registered customer, so IGST can be shown.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from munim.core.provisioning import Provisioner, load_pool
from munim.core.tenants import DEMO_SITE, TenantStore

DEMO_PROFILE = {"gstin": "27AAICA1234B1ZA", "city": "Mumbai", "address": "Shop 1, Main Road, Mumbai 400001"}  # sample GSTIN
DEMO_HSN = {"CEM-50": "2523", "TMT-12": "7214", "SAND-T": "2505", "CCTV-2MP": "8525"}  # DVR-8 left out: the bot asks
DEMO_INTERSTATE = ("Sharma Enterprises", "08AABCG5678K1Z4", "Rajasthan", "Jaipur")  # sample GSTIN


async def main() -> None:
    pool = load_pool(Path(os.environ.get("MUNIM_V2_POOL_DIR", "/app/pool")))
    provisioner = Provisioner(os.environ["ERPNEXT_URL"], pool, hook_secret=os.environ.get("MUNIM_PORTAL_SECRET") or os.environ["WA_APP_SECRET"],
                              bot_url=os.environ.get("MUNIM_INTERNAL_URL", "http://aimunim-bot-1:8800"))
    tenants = TenantStore(Path(".run/v2/tenants.db"))
    targets = {DEMO_SITE: DEMO_PROFILE}
    for site in sorted(tenants.claimed_sites()):
        owner = next((m for m in tenants.members(site) if m.role == "owner"), None)
        targets[site] = (owner.data.get("profile") if owner else None) or {}
    for site, profile in targets.items():
        if site not in pool:
            print(f"{site}: no credentials in the pool, skipped")
            continue
        api = provisioner.api(site)
        company = await api.first("Company", {})
        if not company:
            print(f"{site}: setup wizard not run yet, skipped")
            continue
        abbr = (await api.get(f"/api/resource/Company/{company}"))["data"]["abbr"]
        await provisioner._gst_masters(api, company, abbr, profile)
        await provisioner.automation_setup(api, site)
        gstin = (await api.get(f"/api/resource/Company/{company}"))["data"].get("gstin")
        print(f"{site}: {company} GST ready ({'registered ' + gstin if gstin else 'not registered: no GST charged'})")
        if site == DEMO_SITE:
            await seed_demo(api)


async def seed_demo(api) -> None:
    for code, hsn in DEMO_HSN.items():
        if await api.exists("Item", code):
            await api.put(f"/api/resource/Item/{code}", {"gst_hsn_code": hsn})
    name, gstin, state, city = DEMO_INTERSTATE
    if await api.exists("Customer", name):
        await api.put(f"/api/resource/Customer/{name}", {"gstin": gstin, "tax_id": gstin, "gst_category": "Registered Regular"})
        linked = [["Dynamic Link", "link_doctype", "=", "Customer"], ["Dynamic Link", "link_name", "=", name]]
        if not await api.first("Address", linked):
            await api.insert("Address", {
                "address_title": name, "address_type": "Billing", "address_line1": "As per GST registration", "city": city,
                "state": state, "country": "India", "gstin": gstin, "gst_category": "Registered Regular", "is_primary_address": 1,
                "links": [{"link_doctype": "Customer", "link_name": name}]})
    print("demo: HSN codes + an out-of-state registered customer")


if __name__ == "__main__":
    asyncio.run(main())
