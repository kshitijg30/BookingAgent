"""Money as integer paise. Floats never cross this boundary."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import NewType

Paise = NewType("Paise", int)


def to_paise(value: str | int | float | Decimal) -> Paise:
    """Parse rupees ("₹1,200", "1200", 1199.999) into integer paise, rounding half up."""
    if isinstance(value, str):
        cleaned = value.replace("₹", "").replace(",", "").replace("Rs.", "").replace("Rs", "").strip()
    else:
        cleaned = str(value)
    try:
        rupees = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {value!r}") from exc
    if rupees < 0:
        raise ValueError(f"negative amount: {value!r}")
    return Paise(int((rupees * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def fmt_rupees(amount: int) -> str:
    rupees, rem = divmod(amount, 100)
    whole = f"₹{rupees:,}"
    return whole if rem == 0 else f"{whole}.{rem:02d}"
