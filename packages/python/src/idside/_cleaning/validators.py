# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Check-digit and structure validators named by the catalogue.

Each takes the matched value (as read in the shadow) and returns how many of
its characters are the value — its whole length, or for an IBAN the part up to
its country's length — or ``None`` when the value fails. The Node SDK has the
same functions in ``cleaning/validators.ts``; the shared vectors hold both.
"""

from __future__ import annotations

import string
from collections.abc import Callable

from idside._cleaning.rules import TABLES

_IBAN_LENGTHS: dict[str, int] = TABLES["iban_lengths"]
_COUNTRIES = frozenset(TABLES["country_codes"])
#: (lowest prefix, highest prefix, card lengths), prefixes of equal length.
_NETWORKS: tuple[tuple[str, str, frozenset[int]], ...] = tuple(
    (str(network["from"]), str(network["to"]), frozenset(network["lengths"]))
    for network in TABLES["card_networks"]
)
#: Digits, then the letters A to Z without I and O.
_VAT_ALPHABET = string.digits + "".join(
    letter for letter in string.ascii_uppercase if letter not in "IO"
)
_HEX = frozenset("0123456789abcdefABCDEF")


def _digits(value: str) -> str:
    return "".join(char for char in value if "0" <= char <= "9")


def _all_digits(text: str) -> bool:
    """ASCII digits only: ``str.isdigit`` also accepts other scripts' digits."""
    return text != "" and all("0" <= char <= "9" for char in text)


def luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _whole(ok: bool, value: str) -> int | None:
    return len(value) if ok else None


def card(value: str) -> int | None:
    digits = _digits(value)
    if not luhn(digits):
        return None
    for low, high, lengths in _NETWORKS:
        prefix = digits[: len(low)]
        if len(digits) in lengths and int(low) <= int(prefix) <= int(high):
            return len(value)
    return None


def _mod97(text: str) -> int:
    """``text`` (digits and letters, A=10…Z=35) read as a number, modulo 97."""
    remainder = 0
    for char in text:
        value = int(char, 36)
        remainder = (remainder * (100 if value > 9 else 10) + value) % 97
    return remainder


def iban(value: str) -> int | None:
    compact = value.replace(" ", "").replace("-", "").upper()
    length = _IBAN_LENGTHS.get(compact[:2])
    if length is None or len(compact) < length:
        return None
    candidate = compact[:length]
    if _mod97(candidate[4:] + candidate[:4]) != 1:
        return None
    # How many characters of the value hold those `length` letters and digits.
    held = [index for index, char in enumerate(value) if char not in " -"]
    cut = held[length - 1] + 1
    if cut == len(value) or value[cut] in " -":
        return cut
    return None


def bic(value: str) -> int | None:
    return _whole(value.replace(" ", "")[4:6] in _COUNTRIES, value)


def nir(value: str) -> int | None:
    compact = value.replace(" ", "").replace(".", "").replace("-", "").upper()
    if len(compact) != 15:
        return None
    department = compact[5:7]
    body = compact[:13]
    if department == "2A":
        body = body[:5] + "19" + body[7:]
    elif department == "2B":
        body = body[:5] + "18" + body[7:]
    if not _all_digits(body) or not _all_digits(compact[13:]):
        return None
    return _whole(97 - int(body) % 97 == int(compact[13:]), value)


def siren(value: str) -> int | None:
    digits = _digits(value)
    return _whole(len(digits) == 9 and luhn(digits), value)


def siret(value: str) -> int | None:
    digits = _digits(value)
    if len(digits) != 14 or not luhn(digits[:9]):
        return None
    if digits.startswith("356000000") and digits != "35600000000048":
        # La Poste: its establishments do not follow Luhn; their digits sum
        # to a multiple of 5 instead.
        return _whole(sum(ord(char) - 48 for char in digits) % 5 == 0, value)
    return _whole(luhn(digits), value)


def fr_vat(value: str) -> int | None:
    compact = value.replace(" ", "").replace(".", "").replace("-", "").upper()[2:]
    key, number = compact[:2], compact[2:]
    if len(number) != 9 or not _all_digits(number):
        return None
    if number[:3] != "000" and not luhn(number):
        return None
    if _all_digits(key):
        return _whole(int(key) == int(number + "12") % 97, value)
    if _all_digits(key[0]):
        check = _VAT_ALPHABET.index(key[0]) * 24 + _VAT_ALPHABET.index(key[1]) - 10
    else:
        check = _VAT_ALPHABET.index(key[0]) * 34 + _VAT_ALPHABET.index(key[1]) - 100
    return _whole((int(number) + 1 + check // 11) % 11 == check % 11, value)


def _ipv4(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 4 and all(
        _all_digits(part) and len(part) <= 3 and int(part) <= 255 for part in parts
    )


def _ipv6(value: str) -> bool:
    if value.count("::") > 1 or not any("0" <= char <= "9" for char in value):
        return False
    if "::" in value:
        head, tail = value.split("::")
        groups = (head.split(":") if head else []) + (tail.split(":") if tail else [])
        compressed = True
    else:
        groups = value.split(":")
        compressed = False
    count = len(groups)
    if groups and "." in groups[-1]:
        if not _ipv4(groups[-1]):
            return False
        groups = groups[:-1]
        count += 1
    if not all(1 <= len(group) <= 4 and set(group) <= _HEX for group in groups):
        return False
    if not compressed:
        return count == 8
    # `::1`, `::2` or `1::2` are as often a slice of code (`items[1::2]`) as an
    # address: a compressed address needs a group of three hex digits or more,
    # or an IPv4 tail.
    return count <= 7 and (
        count > len(groups) or any(len(group) >= 3 for group in groups)
    )


def ip(value: str) -> int | None:
    if ":" not in value:
        return _whole(_ipv4(value), value)
    if _ipv6(value):
        return len(value)
    # The reading before its last colon: `fe80::1:` ends a sentence,
    # `1::2:3:4:5:6:7:8080` carries a port.
    cut = value.rfind(":")
    if _ipv6(value[:cut]):
        return cut
    # The reading before a closing `::` (`fe80::1ff:fe23:4567:890a::`).
    bare = value.rstrip(":")
    return len(bare) if len(bare) < cut and _ipv6(bare) else None


def phone(value: str) -> int | None:
    if value.startswith("+"):
        skipped = 0
    elif value.startswith("00"):
        skipped = 2
    else:
        return len(value)  # a national French number: its pattern is exact
    # The longest reading that ends a group of digits and holds 9 to 15 of
    # them: what follows a number of a valid form after a separator is not
    # part of it, and the longest such reading masks the most.
    count = 0
    longest = None
    for index, char in enumerate(value):
        if "0" <= char <= "9":
            count += 1
            last = index + 1 == len(value) or not "0" <= value[index + 1] <= "9"
            if last and 9 <= count - skipped <= 15:
                longest = index + 1
    if longest is not None and value[longest : longest + 1] == ")":
        longest += 1
    return longest


def plate(value: str) -> int | None:
    # The three digits of the plate, not those of a department after it.
    return _whole(_digits(value)[:3] != "000", value)


VALIDATORS: dict[str, Callable[[str], int | None]] = {
    "bic": bic,
    "card": card,
    "fr_vat": fr_vat,
    "iban": iban,
    "ip": ip,
    "nir": nir,
    "phone": phone,
    "plate": plate,
    "siren": siren,
    "siret": siret,
}
