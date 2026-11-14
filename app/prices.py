"""Currency parsing and alert decision. Money is stored as integer rubles."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


def rubles(value: str | int | float | None) -> int | None:
    """Parse Ozon's displayed price, e.g. '3 690 ₽' or '3 690,00 ₽'.

    Prices with fractional kopecks are rounded down to avoid false alerts.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    raw = str(value).replace("\u00a0", " ").replace("\u202f", " ").strip()
    if not re.search(r"\d", raw):
        return None
    match = re.search(r"\d[\d\s.,]*", raw)
    if not match:
        return None
    s = re.sub(r"\s+", "", match.group(0))
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        whole, fraction = s.rsplit(",", 1)
        s = whole.replace(",", "") + ("." + fraction if len(fraction) == 2 else fraction)
    elif s.count(".") > 1:
        s = s.replace(".", "")
    elif "." in s and len(s.rsplit(".", 1)[1]) == 3:
        s = s.replace(".", "")
    try:
        n = int(Decimal(s))
        return n if n > 0 else None
    except (InvalidOperation, ValueError):
        return None


def should_alert(base: int | None, current: int | None, last_alert: int | None, threshold: int) -> bool:
    if base is None or current is None or threshold < 1:
        return False
    if base - current < threshold:
        return False
    return last_alert is None or last_alert - current >= threshold


def choose_card_price(card: dict, mode: str) -> int | None:
    if card.get("available") is False:
        return None
    if mode == "regular":
        return rubles(card.get("price_regular") or card.get("price"))
    return rubles(card.get("price"))
