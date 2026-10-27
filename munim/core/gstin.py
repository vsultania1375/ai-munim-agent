"""Offline GSTIN checks: format, official mod-36 checksum, state and PAN.

This proves a GSTIN is well-formed. It does not prove it is registered or active;
that needs a GST verification provider (paid), which the demo does not use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from munim.core.i18n import t

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

STATES: dict[str, str] = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}

PAN_HOLDER = {"P": "Proprietor", "C": "Company", "F": "Firm/LLP", "H": "HUF", "A": "AOP", "T": "Trust"}


@dataclass(frozen=True)
class GSTINInfo:
    gstin: str
    state_code: str
    state: str
    pan: str
    holder_type: str


def checksum_char(first14: str) -> str:
    total = 0
    for index, char in enumerate(first14):
        value = _CHARS.index(char) * (2 if index % 2 else 1)
        total += value // 36 + value % 36
    return _CHARS[(36 - total % 36) % 36]


def find_gstin(text: str) -> str | None:
    """Pull a GSTIN-looking token out of free text ('mera gst 06ABCDE1234F1Z5 hai')."""
    compact = re.sub(r"[\s-]", "", (text or "").upper())
    found = re.search(r"(?=([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]))", compact)
    return found.group(1) if found else None


def check_gstin(raw: str, lang: str | None = None) -> tuple[GSTINInfo | None, str | None]:
    """Return (info, None) when valid, else (None, reason in the owner's language)."""
    gstin = (raw or "").strip().upper().replace(" ", "")
    if len(gstin) != 15:
        return None, t("gst.len", lang, n=len(gstin))
    if not GSTIN_RE.match(gstin):
        return None, t("gst.format", lang)
    state = STATES.get(gstin[:2])
    if state is None:
        return None, t("gst.state", lang, code=gstin[:2])
    if checksum_char(gstin[:14]) != gstin[14]:
        return None, t("gst.checksum", lang)
    return GSTINInfo(gstin, gstin[:2], state, gstin[2:12], PAN_HOLDER.get(gstin[5], "Business")), None
