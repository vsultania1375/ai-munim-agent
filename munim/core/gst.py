"""GST the way India Compliance applies it: CGST + SGST inside the state, IGST across states, none when
the business itself is not GST registered. Place of supply follows the customer's GSTIN, else their
billing address state, else the business's own state (a walk-in customer)."""
from __future__ import annotations

import re

from munim.core.gstin import STATES

TAX_NONE, TAX_INTRA, TAX_INTER = "none", "intra", "inter"
STATE_CODE = {name.lower(): code for code, name in STATES.items()}
HSN_RE = re.compile(r"^\d{4}(?:\d{2}){0,2}$")  # 4, 6 or 8 digits


def state_code(gstin: str | None = None, state: str | None = None) -> str | None:
    if gstin and len(gstin) >= 2 and gstin[:2] in STATES:
        return gstin[:2]
    if state:
        return STATE_CODE.get(state.strip().lower())
    return None


def tax_mode(company_gstin: str | None, customer_gstin: str | None = None, customer_state: str | None = None) -> str:
    home = state_code(gstin=company_gstin)
    if not home:
        return TAX_NONE  # an unregistered business may not charge GST
    other = state_code(gstin=customer_gstin) or state_code(state=customer_state) or home
    return TAX_INTRA if other == home else TAX_INTER


def purchase_tax_mode(company_gstin: str | None, supplier_gstin: str | None, supplier_state: str | None = None) -> str:
    """Input GST only on a GST-registered supplier's bill (India Compliance refuses it otherwise)."""
    if not supplier_gstin:
        return TAX_NONE
    return tax_mode(company_gstin, supplier_gstin, supplier_state)


def purchase_template(mode: str, abbr: str) -> str | None:
    if mode == TAX_INTRA:
        return f"Input GST In-state - {abbr}"
    if mode == TAX_INTER:
        return f"Input GST Out-state - {abbr}"
    return None


def sales_template(mode: str, abbr: str) -> str | None:
    """India Compliance's own sales tax templates for the company."""
    if mode == TAX_INTRA:
        return f"Output GST In-state - {abbr}"
    if mode == TAX_INTER:
        return f"Output GST Out-state - {abbr}"
    return None


def clean_hsn(raw: str | None) -> str | None:
    code = re.sub(r"[\s.]", "", str(raw or ""))
    return code if HSN_RE.match(code) else None
