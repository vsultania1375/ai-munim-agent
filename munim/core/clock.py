"""India time for everything the owner sees or the model reasons about (the VM runs in UTC)."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def ist(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, IST)


def today_ist() -> date:
    return datetime.now(IST).date()


def stamp(ts: float) -> str:
    """'Mon 26 Oct 13:05' — compact enough to prefix every history line."""
    return ist(ts).strftime("%a %d %b %H:%M")


def now_line(ts: float) -> str:
    return ist(ts).strftime("%A %d %B %Y, %H:%M IST")


def gap(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} days"
