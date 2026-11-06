"""Tests for the internal RFC 8785 (JCS) canonicalization module."""

from __future__ import annotations

import json
import math
import struct

import pytest

from idside import _jcs


def _canon(value: object) -> str:
    return _jcs.canonicalize(value).decode("utf-8")


def _double(hex_bits: str) -> float:
    return struct.unpack(">d", bytes.fromhex(hex_bits))[0]  # type: ignore[no-any-return]


def test_rfc8785_appendix_a_example() -> None:
    data = json.loads(
        '{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, '
        "0.000000000000000000000000001], "
        '"string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/", '
        '"literals": [null, true, false]}'
    )
    expected = (
        '{"literals":[null,true,false],'
        '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )
    assert _canon(data) == expected


def test_keys_sorted_by_utf16_code_units() -> None:
    # RFC 8785 §3.2.3: sorting operates on UTF-16 code units, so an emoji
    # (surrogate pair, first unit 0xD83D) sorts before U+FB33.
    data = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\x80": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }
    ordered = list(json.loads(_canon(data)))
    assert ordered == ["\r", "1", "\x80", "ö", "€", "\U0001f600", "דּ"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "0"),
        (-0.0, "0"),
        (1.0, "1"),
        (4.5, "4.5"),
        (0.002, "0.002"),
        (1e30, "1e+30"),
        (1e21, "1e+21"),
        (1e-27, "1e-27"),
        (0.000001, "0.000001"),
        (1e-7, "1e-7"),
        (100.0, "100"),
        (-5.5, "-5.5"),
        (-1e-7, "-1e-7"),
        (9007199254740992.0, "9007199254740992"),
        (_double("0000000000000001"), "5e-324"),
        (_double("7fefffffffffffff"), "1.7976931348623157e+308"),
        (_double("44b52d02c7e14af6"), "1e+23"),
        (_double("3eb0c6f7a0b5ed8c"), "9.999999999999997e-7"),
        (_double("444b1ae4d6e2ef4f"), "999999999999999900000"),
        (_double("41b3de4355555555"), "333333333.3333333"),
    ],
)
def test_es6_number_formatting(value: float, expected: str) -> None:
    assert _canon(value) == expected


def test_every_emitted_float_round_trips() -> None:
    # Determinism guarantee: the emitted decimal must parse back to the exact
    # same IEEE-754 double (shortest round-trip representation).
    import random

    rng = random.Random(87)  # noqa: S311 — deterministic fuzz seed, not crypto
    checked = 0
    while checked < 5000:
        bits = rng.getrandbits(64)
        value = struct.unpack(">d", struct.pack(">Q", bits))[0]
        if math.isnan(value) or math.isinf(value):
            continue
        assert float(_canon(value)) == value or value == 0.0
        checked += 1


def test_integers_serialize_as_plain_decimals() -> None:
    assert _canon({"a": 42, "b": -7, "c": 0}) == '{"a":42,"b":-7,"c":0}'


@pytest.mark.parametrize("value", [2**53 + 1, -(2**53) - 1, 10**20])
def test_integer_beyond_ieee754_safe_range_is_rejected(value: int) -> None:
    with pytest.raises(ValueError, match="IEEE-754"):
        _jcs.canonicalize(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        _jcs.canonicalize(value)


def test_non_json_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="set"):
        _jcs.canonicalize({"a": {1, 2}})


def test_non_string_dict_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="key"):
        _jcs.canonicalize({1: "one"})


def test_lone_surrogate_is_rejected() -> None:
    with pytest.raises(ValueError):
        _jcs.canonicalize({"bad": "\ud800"})


def test_unicode_strings_stay_raw_utf8() -> None:
    assert _canon("Café 日本語 😀") == '"Café 日本語 😀"'
