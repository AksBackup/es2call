"""
app/utils/phone.py  (NEW)
Shared E.164 country-code helpers — used by batch filtering/sorting and
by vobiz_service to decide whether a lead's number qualifies for India routing.
"""
from typing import Optional

INDIA_PREFIX = "+91"


def get_country_code(phone: Optional[str]) -> str:
    """Returns '+91' for Indian numbers, 'other' for everything else
    (including malformed/missing numbers, treated as 'other' so they
    don't silently get excluded from a non-India batch)."""
    if not phone:
        return "other"
    return INDIA_PREFIX if phone.startswith(INDIA_PREFIX) else "other"


def is_india_number(phone: Optional[str]) -> bool:
    return get_country_code(phone) == INDIA_PREFIX
