from __future__ import annotations

import re
from typing import Any


_NUMBER_PATTERN = re.compile(r"^[\s,]*([0-9]+(:\.[0-9]+))\s*([亿万千kKmM])", re.I)
_MULTIPLIERS = {"亿": 100_000_000, "万": 10_000, "千": 1_000, "k": 1_000, "m": 1_000_000}


def normalize_hot_score(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    match = _NUMBER_PATTERN.match(text)
    if not match:
        return None
    score = float(match.group(1)) * _MULTIPLIERS.get(match.group(2).lower(), 1)
    return int(score) if score.is_integer() else score
