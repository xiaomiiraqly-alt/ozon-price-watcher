"""Currency parsing and alert decision. Money is stored as integer rubles."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
import math


def rubles(value: str | int | float | None) -> int | None:
    """Parse Ozon's displayed price, e.g. '3 690 ₽' or '3 690,00 ₽'.

    Fractional rubles are truncated to match the integer-ruble storage model.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        n=int(value)
        return n if 0<n<2**63 else None
    raw = str(value).replace("\u00a0", " ").replace("\u202f", " ").strip()
    if not re.search(r"\d", raw):
        return None
    match = re.search(r"[+-]?[0-9][0-9\s.,]*", raw)
    if not match:
        return None
    s = re.sub(r"\s+", "", match.group(0))
    separators = {c for c in s if c in ",."}
    if len(separators) == 2:
        # The last separator is decimal: 1.234,50 and 1,234.50.
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        grouping = "." if decimal == "," else ","
        whole, fraction = s.rsplit(decimal, 1)
        if not re.fullmatch(r"[0-9]{1,2}", fraction):
            return None
        if not re.fullmatch(rf"[+-]?[0-9]{{1,3}}(?:\{grouping}[0-9]{{3}})+", whole):
            return None
        s = whole.replace(grouping, "") + "." + fraction
    elif separators:
        separator = next(iter(separators))
        parts = s.split(separator)
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            s = parts[0] + "." + parts[1]
        elif re.fullmatch(rf"[+-]?[0-9]{{1,3}}(?:\{separator}[0-9]{{3}})+", s):
            s = s.replace(separator, "")
        else:
            return None
    try:
        n = int(Decimal(s))
        return n if 0<n<2**63 else None
    except (InvalidOperation, ValueError, OverflowError):
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
