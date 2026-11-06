# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Minimal RFC 8785 (JCS) canonicalization — internal module.

The ``request_hash`` bound into ACT tokens is computed over the JCS canonical
form of the AI call payload, so every ID Side SDK (Python today, Node later)
produces bit-identical hashes for the same payload and any auditor can
recompute them. Implemented locally: the JSON subset the SDK needs is small
enough to keep in-tree, fully tested, and free of runtime dependencies.

Scope: JSON-compatible values only (``dict``/``list``/``str``/``int``/
``float``/``bool``/``None``). Anything else must be normalized *before*
calling :func:`canonicalize` (see ``ProviderAdapter.extract_request_metadata``).
"""

from __future__ import annotations

import json
import math
from typing import Any

__all__ = ["canonicalize"]

#: Largest integer magnitude exactly representable as an IEEE-754 double (2^53).
#: Beyond it, JavaScript SDKs would parse a different value and hashes diverge.
_MAX_SAFE_INT = 2**53


def canonicalize(value: Any) -> bytes:
    """Serialize ``value`` per RFC 8785 and return the canonical UTF-8 bytes.

    Raises ``ValueError`` for anything outside the JCS-serializable domain:
    non-JSON types, non-string dict keys, NaN/Infinity, integers beyond
    IEEE-754 safe range, or strings containing lone surrogates.
    """
    out: list[str] = []
    _write(value, out)
    # UnicodeEncodeError (a ValueError subclass) surfaces lone surrogates here.
    return "".join(out).encode("utf-8")


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif isinstance(value, bool):
        out.append("true" if value else "false")
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, int):
        out.append(_format_int(value))
    elif isinstance(value, float):
        out.append(_format_float(value))
    elif isinstance(value, list):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        for i, (key, item) in enumerate(_sorted_items(value)):
            if i:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _write(item, out)
        out.append("}")
    else:
        raise ValueError(f"type '{type(value).__name__}' is not JSON-serializable")


def _sorted_items(value: dict[Any, Any]) -> list[tuple[str, Any]]:
    for key in value:
        if not isinstance(key, str):
            raise ValueError(
                f"object key {key!r} is not a string (JSON object keys must be)"
            )
    # RFC 8785 §3.2.3: keys sort by UTF-16 code units, not Unicode code points
    # (an emoji, encoded as a surrogate pair, sorts before U+E000..U+FFFF).
    return sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))


def _format_int(value: int) -> str:
    if abs(value) > _MAX_SAFE_INT:
        raise ValueError(
            f"integer {value} exceeds the IEEE-754 safe range (±2^53); "
            "it would not hash identically across SDK languages"
        )
    return str(value)


def _format_float(value: float) -> str:
    """Format per ECMAScript ``Number::toString`` (RFC 8785 §3.2.2.3)."""
    if math.isnan(value) or math.isinf(value):
        raise ValueError("NaN and Infinity are not valid JSON numbers")
    if value == 0.0:
        return "0"  # ECMAScript serializes both +0 and -0 as "0"
    sign = ""
    if value < 0:
        sign = "-"
        value = -value
    # repr() yields the shortest digits that round-trip, same digits as ES6.
    text = repr(value)
    if "e" in text:
        mantissa, _, exp_text = text.partition("e")
        exp10 = int(exp_text)
    else:
        mantissa, exp10 = text, 0
    int_part, _, frac_part = mantissa.partition(".")
    digits_value = int(int_part + frac_part)
    exponent = exp10 - len(frac_part)
    while digits_value % 10 == 0:
        digits_value //= 10
        exponent += 1
    digits = str(digits_value)
    k = len(digits)
    n = k + exponent  # value == 0.<digits> * 10^n
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        head = digits[0] + ("." + digits[1:] if k > 1 else "")
        exp_out = n - 1
        body = f"{head}e{'+' if exp_out >= 0 else '-'}{abs(exp_out)}"
    return sign + body
