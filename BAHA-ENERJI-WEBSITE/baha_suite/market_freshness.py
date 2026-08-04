"""Small helpers for deciding whether market values are actually published."""

from __future__ import annotations

import math
from typing import Any


def market_smf_is_published(value: Any) -> bool:
    """Return True only when an EPİAŞ SMF value looks like a real published value."""

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed) and abs(parsed) > 1e-9
