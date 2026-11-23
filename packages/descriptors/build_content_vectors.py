#!/usr/bin/env python3
"""Generate `testdata/content-cleaning-vectors.json`, the prompt-cleaning vectors.

Both SDKs replay every case of the generated file and must produce exactly the
output, counts, blocked counts and attachment count it states. The expected
values are written HERE, by hand, as templates: an input and the text it must
become. They are never computed by either engine, so a vector is a statement
about what cleaning does, not a recording of what one implementation happened
to do.

`packages/python/tests/test_content_vectors.py` fails when the checked-in file
no longer matches this script. Every identifier, key and number below is
fictitious: public test card numbers, documentation IBANs, and check digits
computed here for numbers nobody holds.

Run: `python packages/descriptors/build_content_vectors.py`
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import string
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT = HERE / "testdata" / "content-cleaning-vectors.json"
RULES = json.loads((HERE / "content-rules.json").read_text(encoding="utf-8"))

ALL_IDS = [detector["id"] for detector in RULES["detectors"]]

# ── Check digits, computed independently of either engine ─────────────────


def luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def luhn_complete(prefix: str) -> str:
    for last in "0123456789":
        if luhn_ok(prefix + last):
            return prefix + last
    raise AssertionError(prefix)


def nir_key(first13: str) -> str:
    number = first13.replace("2A", "19").replace("2B", "18")
    return f"{97 - int(number) % 97:02d}"


def iban_ok(iban: str) -> bool:
    compact = iban.replace(" ", "").upper()
    moved = compact[4:] + compact[:4]
    return int("".join(str(int(char, 36)) for char in moved)) % 97 == 1


def vat_key(siren: str) -> str:
    return f"{int(siren + '12') % 97:02d}"


# ── Fictitious values ──────────────────────────────────────────────────────

RNG = random.Random(20261116)
ALNUM = string.ascii_letters + string.digits


def rand(alphabet: str, size: int) -> str:
    return "".join(RNG.choice(alphabet) for _ in range(size))


HEX = "0123456789abcdef"
KEY_OPENAI = "sk-proj-" + rand(ALNUM + "_-", 56)
KEY_OPENAI_LEGACY = "sk-" + rand(ALNUM, 48)
KEY_ANTHROPIC = "sk-ant-api03-" + rand(ALNUM + "_-", 86)
KEY_GOOGLE = "AIza" + rand(ALNUM + "_-", 35)
KEY_MISTRAL = rand(ALNUM, 32)
KEY_DEEPSEEK = "sk-" + rand(HEX, 32)
KEY_IDSIDE = "sk_live_" + rand(ALNUM, 32)
KEY_IDSIDE_TEST = "sk_test_" + rand(ALNUM, 32)
KEY_AWS = "AKIA" + rand(string.ascii_uppercase + string.digits, 16)
KEY_GITHUB = "ghp_" + rand(ALNUM, 36)
KEY_GITHUB_PAT = "github_pat_" + rand(ALNUM + "_", 82)
KEY_GITLAB = "glpat-" + rand(ALNUM + "_-", 20)
KEY_SLACK = "xoxb-" + rand(string.digits, 12) + "-" + rand(ALNUM, 24)
SLACK_HOOK = "https://hooks.slack.com/services/T0000EXAMP/B0000EXAMP/" + rand(ALNUM, 24)
KEY_STRIPE = "sk_live_" + rand(ALNUM, 99)
KEY_STRIPE_RESTRICTED = "rk_test_" + rand(ALNUM, 24)
KEY_STRIPE_WEBHOOK = "whsec_" + rand(ALNUM, 32)
KEY_TWILIO = "SK" + rand(HEX, 32)
KEY_SENDGRID = "SG." + rand(ALNUM + "_-", 22) + "." + rand(ALNUM + "_-", 43)
KEY_NPM = "npm_" + rand(ALNUM, 36)
KEY_PYPI = "pypi-" + "AgEIcHlwaS5vcmc" + rand(ALNUM + "_-", 64)
AZURE_ACCOUNT_KEY = rand(ALNUM + "+/", 86) + "=="
AZURE_HEX = rand(HEX, 32)
# The header and claims of a fictitious token, base64url-encoded here so that
# no line of this file is a token.
JWT = (
    ".".join(
        base64.urlsafe_b64encode(json.dumps(part, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
        for part in ({"alg": "HS256"}, {"sub": "usr_marie_456"})
    )
    + "."
    + rand(ALNUM + "_-", 43)
)
BEARER = rand(ALNUM + "._-", 40)
PEM_BODY = (
    "\n".join(rand(ALNUM + "+/", 64) for _ in range(4)) + "\n" + rand(ALNUM, 20) + "=="
)
PEM = (
    "-----BEGIN RSA "
    + "PRIVATE KEY-----\n"
    + PEM_BODY
    + "\n-----END RSA "
    + "PRIVATE KEY-----"
)
OPENSSH = (
    "-----BEGIN OPENSSH "
    + "PRIVATE KEY-----\n"
    + PEM_BODY
    + "\n-----END OPENSSH "
    + "PRIVATE KEY-----"
)

for card in (
    "4111111111111111",
    "5555555555554444",
    "378282246310005",
    "6011111111111117",
    "3530111333300000",
    "30569309025904",
    "6200000000000005",
    "2223000048400011",
):
    assert luhn_ok(card), card
assert not luhn_ok("4111111111111112")

for iban in (
    "FR7630006000011234567890189",
    "DE89370400440532013000",
    "GB82WEST12345698765432",
    "BE68539007547034",  # pragma: allowlist secret
    "NO9386011117947",
):
    assert iban_ok(iban), iban
assert not iban_ok("FR7630006000011234567890188")

NIR_BASE = "1850578006084"
NIR = NIR_BASE + nir_key(NIR_BASE)
NIR_CORSICA = "269052A123456" + nir_key("269052A123456")  # pragma: allowlist secret
NIR_CORSICA_B = "178122B045012" + nir_key("178122B045012")
NIR_BAD = NIR_BASE + f"{(int(nir_key(NIR_BASE)) % 97) + 1:02d}"
assert NIR_BAD != NIR

SIREN = luhn_complete("12345678")
SIREN_BAD = SIREN[:-1] + str((int(SIREN[-1]) + 1) % 10)
assert not luhn_ok(SIREN_BAD)
SIRET = luhn_complete(SIREN + "0001")
SIRET_BAD = SIRET[:-1] + str((int(SIRET[-1]) + 1) % 10)
SIRET_LA_POSTE = "35600000009075"  # La Poste rule: digit sum a multiple of 5
assert sum(map(int, SIRET_LA_POSTE)) % 5 == 0 and not luhn_ok(SIRET_LA_POSTE)
VAT = "FR" + vat_key(SIREN) + SIREN
VAT_BAD = "FR" + f"{(int(vat_key(SIREN)) + 1) % 97:02d}" + SIREN


def spaced(digits: str, sizes: list[int], sep: str = " ") -> str:
    out, at = [], 0
    for size in sizes:
        out.append(digits[at : at + size])
        at += size
    return sep.join(out)


# ── Policies ───────────────────────────────────────────────────────────────

REFERENCE = {
    "schema": 1,
    "app_id": "app_medhelp",
    "revision": 0,
    "detectors": {detector: "mask" for detector in ALL_IDS},
    "attachments": "allow",
    "custom": [],
}

POLICIES: dict[str, Any] = {
    "reference": REFERENCE,
    "off": {
        "schema": 1,
        "app_id": "app_medhelp",
        "revision": 1,
        "detectors": {},
        "attachments": "allow",
        "custom": [],
    },
    "email_only": {
        "schema": 1,
        "app_id": "app_medhelp",
        "revision": 2,
        "detectors": {"email": "mask"},
        "attachments": "allow",
        "custom": [],
    },
    "block_card": {
        "schema": 1,
        "app_id": "app_medhelp",
        "revision": 3,
        "detectors": {"email": "mask", "credit_card": "block", "iban": "block"},
        "attachments": "allow",
        "custom": [],
    },
    "attachments_block": {
        "schema": 1,
        "app_id": "app_medhelp",
        "revision": 4,
        "detectors": {"email": "mask"},
        "attachments": "block",
        "custom": [],
    },
    "custom": {
        "schema": 1,
        "app_id": "app_medhelp",
        "revision": 5,
        "detectors": {"email": "mask", "phone": "mask"},
        "attachments": "allow",
        "custom": [
            {
                "kind": "term",
                "value": "Projet Atlas",
                "action": "mask",
                "label": "PROJECT",
            },
            {
                "kind": "term",
                "value": "Compagnie Générale d'Exemple",
                "action": "mask",
                "label": "CLIENT",
            },
            {
                "kind": "domain",
                "value": "acme.example",
                "action": "mask",
                "label": "DOMAIN",
            },
            {"kind": "pattern", "value": "PRJ-####", "action": "mask", "label": "REF"},
            {
                "kind": "pattern",
                "value": "CASE-@@#*",
                "action": "mask",
                "label": "CASE",
            },
            {
                "kind": "term",
                "value": "Atlas Corp",
                "action": "mask",
                "label": "PARTNER",
            },
            {"kind": "pattern", "value": "(##)", "action": "mask", "label": "PAIR"},
        ],
    },
    "custom_block": {
        "schema": 1,
        "app_id": "app_medhelp",
        "revision": 6,
        "detectors": {"email": "mask"},
        "attachments": "allow",
        "custom": [
            {
                "kind": "domain",
                "value": "@rival.example",
                "action": "block",
                "label": "RIVAL",
            },
            {
                "kind": "term",
                "value": "Opération Nordlys",
                "action": "block",
                "label": "CODENAME",
            },
        ],
    },
    "future": {
        "schema": 1,
        "app_id": "app_medhelp",
        "revision": 7,
        "detectors": {"email": "mask", "fr_iban_v9": "block", "phone": "warn"},
        "attachments": "allow",
        "custom": [
            {"kind": "regex", "value": "x+", "action": "mask", "label": "FUTURE"},
            {
                "kind": "term",
                "value": "Projet Atlas",
                "action": "quarantine",
                "label": "PROJECT",
            },
        ],
    },
}

CASES: list[dict[str, Any]] = []


def _parts(value: Any) -> Any:
    """`{"$parts": [...]}` (large inputs) becomes the file's `$concat` form."""
    if isinstance(value, dict) and "$parts" in value:
        return {"$concat": value["$parts"]}
    return value


#: A run of letters, digits and base64 punctuation long enough to look like a
#: credential to a secret scanner.
_SECRETISH = re.compile(r"[A-Za-z0-9+/=_.-]{12,}")


def _looks_secret(text: str) -> bool:
    if "PRIVATE KEY" in text or ("://" in text and "@" in text):
        return True
    if re.search(r"(?i)pass(word|wd)?\s*[:=]|secret|token\s*[:=]", text):
        return True
    return any(
        re.search(r"[A-Za-z]", run) and re.search(r"[0-9]", run)
        for run in _SECRETISH.findall(text)
    )


def _split_secrets(value: Any) -> Any:
    """Write every secret-shaped string as `{"$concat": [chunks of 8]}`.

    No tracked file may hold a whole key, token or private key, even a
    fictitious one: a secret scanner cannot tell, and one taught to ignore this
    file would ignore a real leak in it too. Both suites join the chunks back.
    """
    if isinstance(value, str):
        if not _looks_secret(value):
            return value
        return {"$concat": [value[i : i + 8] for i in range(0, len(value), 8)]}
    if isinstance(value, list):
        return [_split_secrets(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$concat"}:
            return value
        return {key: _split_secrets(item) for key, item in value.items()}
    return value


def add(
    name: str,
    value: Any,
    expected: Any = None,
    *,
    policy: str | dict = "reference",
    provider: str | None = None,
    counts: dict | None = None,
    blocked: dict | None = None,
    attachments: int = 0,
    note: str | None = None,
    error: str | None = None,
) -> None:
    case: dict[str, Any] = {"name": name, "policy": policy, "provider": provider}
    case["input"] = _parts(value)
    if error is None and not blocked:
        case["output"] = _parts(value if expected is None else expected)
    case["counts"] = counts or {}
    case["blocked"] = blocked or {}
    case["attachments"] = attachments
    if error:
        case["error"] = error
    if note:
        case["note"] = note
    assert not any(other["name"] == name for other in CASES), name
    CASES.append(case)


#: Every value a `masked()` case says one catalogue detector takes whole, under
#: the reference policy: what the property test in both suites puts in
#: generated contexts (`positives` in the file).
POSITIVES: list[dict[str, str]] = []


def masked(
    name: str,
    before: str,
    value: str,
    after: str,
    label: str,
    detector: str,
    **kwargs: Any,
) -> None:
    policy = kwargs.get("policy", "reference")
    alone = isinstance(policy, dict) and policy.get("detectors") == {detector: "mask"}
    if (policy == "reference" or alone) and detector in ALL_IDS:
        positive = {
            "detector": detector,
            "lead": before,
            "value": value,
            "trail": after,
        }
        if positive not in POSITIVES:
            POSITIVES.append(positive)
    add(
        name,
        before + value + after,
        before + f"[{label}_1]" + after,
        counts={detector: 1},
        **kwargs,
    )


def kept(name: str, text: str, **kwargs: Any) -> None:
    add(name, text, text, **kwargs)


# ── email ──────────────────────────────────────────────────────────────────
masked(
    "email: plain", "Write to ", "marie.dupont@example.com", " today.", "EMAIL", "email"
)
masked(
    "email: plus and subdomain",
    "cc ",
    "j.martin+test@mail.example.org",
    ".",
    "EMAIL",
    "email",
)
masked(
    "email: at start", "", "contact@acme.example", " is the address", "EMAIL", "email"
)
masked(
    "email: in angle brackets", "Marie <", "marie@example.com", ">", "EMAIL", "email"
)
masked("email: mailto", '<a href="mailto:', "marie@example.com", '">', "EMAIL", "email")
masked(
    "email: after a label and colon",
    "Email:",
    "marie@example.com",
    "",
    "EMAIL",
    "email",
)
masked("email: uppercase", "", "MARIE.DUPONT@EXAMPLE.COM", "", "EMAIL", "email")
kept("email: no domain dot", "marie@localhost is not an address here")
kept("email: one-letter tld", "marie@example.c is cut short")
kept("email: bare at sign", "meet @ noon, or @marie on chat")
kept(
    "email: written out (not caught)",
    "marie dot dupont at example dot com",
    note="Spelled-out addresses are not detected.",
)
kept(
    "email: bracketed at (not caught)",
    "marie [at] example.com",
    note="Obfuscated at-signs are not detected.",
)
kept(
    "email: spaced at (not caught)",
    "marie @ example.com",
    note="An at-sign surrounded by spaces is not an address to the engine.",
)
masked(
    "email: fullwidth characters",
    "Mail: ",
    "ｍａｒｉｅ＠ｅｘａｍｐｌｅ．ｃｏｍ",
    "",
    "EMAIL",
    "email",
    note="NFKD maps fullwidth forms to ASCII before matching; the original characters are replaced.",
)
masked(
    "email: zero-width space inside",
    "Mail: ",
    "mar\u200bie@example.com",
    "",
    "EMAIL",
    "email",
    note="Invisible characters are dropped from the shadow and replaced with the value.",
)
masked(
    "email: soft hyphen inside",
    "Mail: ",
    "marie\u00ad.dupont@example.com",
    "",
    "EMAIL",
    "email",
)
masked(
    "email: accented local part",
    "Mail: ",
    "andré@example.com",
    "",
    "EMAIL",
    "email",
    note="Accents are stripped in the shadow, so the whole original address is replaced.",
)
masked(
    "email: combining accent", "Mail: ", "rene\u0301@example.com", "", "EMAIL", "email"
)
masked(
    "email: cyrillic homoglyph",
    "Mail: ",
    "m\u0430rie@example.com",
    "",
    "EMAIL",
    "email",
    note="Latin, Greek and Cyrillic letters are read in addresses: a Cyrillic letter no longer cuts one.",
)
add(
    "email: two addresses",
    "a: marie@example.com, b: paul@example.com",
    "a: [EMAIL_1], b: [EMAIL_2]",
    counts={"email": 2},
)
add(
    "email: same address twice",
    "marie@example.com wrote to marie@example.com",
    "[EMAIL_1] wrote to [EMAIL_1]",
    counts={"email": 2},
)
add(
    "email: same address, other case",
    "Marie@Example.com and marie@example.com",
    "[EMAIL_1] and [EMAIL_1]",
    counts={"email": 2},
    note="Values are compared lowered and normalised: the same address keeps one placeholder.",
)
add(
    "email: adjacent addresses",
    "marie@example.com;paul@example.com",
    "[EMAIL_1];[EMAIL_2]",
    counts={"email": 2},
)
masked(
    "email: in a url path",
    "https://example.com/contact/",
    "marie@example.com",
    "",
    "EMAIL",
    "email",
)
masked(
    "email: user of a url is a credential",
    "https://",
    "marie",
    "@acme.example/x",
    "CREDENTIALS",
    "url_credentials",
)
masked(
    "email: trailing dot of a sentence",
    "Contact ",
    "marie@example.com",
    ".",
    "EMAIL",
    "email",
)
masked(
    "email: newline around", "line\n", "marie@example.com", "\nnext", "EMAIL", "email"
)
add(
    "email: emoji before",
    "\U0001f600 marie@example.com",
    "\U0001f600 [EMAIL_1]",
    counts={"email": 1},
    note="A character outside the BMP counts as one character on both sides.",
)
add(
    "email: email_only policy ignores a phone",
    "marie@example.com 06 12 34 56 78",
    "[EMAIL_1] 06 12 34 56 78",
    policy="email_only",
    counts={"email": 1},
)

# ── phone ──────────────────────────────────────────────────────────────────
for label, value in (
    ("spaces", "06 12 34 56 78"),
    ("contiguous", "0612345678"),
    ("dots", "06.12.34.56.78"),
    ("dashes", "06-12-34-56-78"),
    ("landline", "01 23 45 67 89"),
    ("+33", "+33 6 12 34 56 78"),
    ("+33 contiguous", "+33612345678"),
    ("0033", "0033 6 12 34 56 78"),
    ("+33 (0)", "+33 (0)6 12 34 56 78"),
    ("international US", "+1 415 555 0123"),
    ("international UK", "+44 20 7946 0958"),
    ("international contiguous", "+4915112345678"),
    ("international dashes", "+49-151-1234-5678"),
):
    masked(f"phone: {label}", "Call ", value, " now", "PHONE", "phone")
masked(
    "phone: nbsp separators",
    "Tél : ",
    "06\u00a012\u00a034\u00a056\u00a078",
    "",
    "PHONE",
    "phone",
    note="A no-break space decomposes to a space.",
)
masked("phone: in parentheses", "(", "06 12 34 56 78", ")", "PHONE", "phone")
kept("phone: too short", "Call 06 12 34 56 now")
kept("phone: eleven digits", "Ref 06123456789 now")
kept("phone: starts with 00 not 0033", "Ref 0012345678 now")
kept(
    "phone: mixed separators (not caught)",
    "Call 06 12.34 56 78 now",
    note="A French number must use one separator throughout.",
)
kept("phone: glued to letters", "ID A0612345678B")
kept("phone: plus with too few digits", "score +12 345")
kept(
    "phone: spelled out (not caught)",
    "zero six twelve thirty-four fifty-six seventy-eight",
)
add(
    "phone: two numbers",
    "06 12 34 56 78 or 07 98 76 54 32",
    "[PHONE_1] or [PHONE_2]",
    counts={"phone": 2},
)

# ── ip_address ─────────────────────────────────────────────────────────────
for label, value in (
    ("ipv4", "192.168.1.10"),
    ("ipv4 cidr", "10.0.0.0/24"),
    ("ipv4 zeros", "0.0.0.0"),
    ("ipv6 compressed", "2001:db8::1"),
    ("ipv6 full", "2001:0db8:0000:0000:0000:ff00:0042:8329"),
    ("ipv6 link local", "fe80::1ff:fe23:4567:890a"),
    ("ipv6 mapped ipv4", "::ffff:192.0.2.128"),
):
    masked(f"ip_address: {label}", "host ", value, " up", "IP_ADDRESS", "ip_address")
kept("ip_address: octet over 255", "host 256.1.1.1 up")
kept("ip_address: three parts", "version 1.2.3 up")
kept("ip_address: five parts", "oid 1.2.3.4.5 up")
kept("ip_address: prefixed version", "release v1.2.3.4 up")
masked(
    "ip_address: a prefix over 32 is taken with its address",
    "net ",
    "10.0.0.0/33",
    " up",
    "IP_ADDRESS",
    "ip_address",
)
kept("ip_address: a time is not ipv6", "at 12:30:45 sharp")
kept("ip_address: double colon alone", "a :: b")
kept("ip_address: two double colons", "x 2001::db8::1 y")
add(
    "ip_address: ipv6 with nine groups",
    "x 1:2:3:4:5:6:7:8:9 y",
    "x [IP_ADDRESS_1]:9 y",
    counts={"ip_address": 1},
    note="Eight groups are an address; a colon and a group after them read like a port.",
)
kept("ip_address: ipv6 group too long", "x 2001:db8:12345::1 y")
kept("ip_address: ipv6 eight groups and double colon", "x 1:2:3:4:5:6:7::8 y")
kept("ip_address: cpp scope", "use std::vector here")
kept(
    "ip_address: ipv6 loopback",
    "host ::1 up",
    note="A compressed IPv6 address is read only with a group of three or more hex digits (or an IPv4 tail): ::1 and the slices of code are not addresses.",
)
kept("ip_address: hex-like scope without a digit", "use abc::def here")

# ── mac_address ────────────────────────────────────────────────────────────
for label, value in (
    ("colons", "00:1A:2B:3C:4D:5E"),
    ("dashes", "00-1a-2b-3c-4d-5e"),
    ("dotted", "001a.2b3c.4d5e"),
):
    masked(f"mac_address: {label}", "nic ", value, " ok", "MAC_ADDRESS", "mac_address")
kept("mac_address: five groups", "nic 00:1A:2B:3C:4D ok")
kept("mac_address: mixed separators", "nic 00:1A-2B:3C:4D:5E ok")
kept("mac_address: seven groups", "nic 00:1A:2B:3C:4D:5E:6F ok")

# ── credit_card ────────────────────────────────────────────────────────────
for label, value in (
    ("visa spaced", "4111 1111 1111 1111"),
    ("visa contiguous", "4111111111111111"),
    ("visa dashes", "4111-1111-1111-1111"),
    ("visa dots", "4111.1111.1111.1111"),
    ("mastercard", "5555 5555 5555 4444"),
    ("mastercard 2-series", "2223000048400011"),
    ("amex grouped", "3782 822463 10005"),
    ("amex contiguous", "378282246310005"),
    ("discover", "6011 1111 1111 1117"),
    ("jcb", "3530111333300000"),
    ("diners", "30569309025904"),
    ("unionpay", "6200000000000005"),
):
    masked(f"credit_card: {label}", "Card ", value, " please", "CARD", "credit_card")
kept("credit_card: luhn fails", "Card 4111 1111 1111 1112 please")
kept("credit_card: no network", "Card 9111 1111 1111 1113 please")
kept("credit_card: visa wrong length", "Card 411111111111113 please")
kept("credit_card: glued to letters", "Order X4111111111111111 please")
kept("credit_card: twenty digits", "Order 41111111111111110000 please")
masked(
    "credit_card: followed by a cvv",
    "Card ",
    "4111 1111 1111 1111",
    " 123 cvv",
    "CARD",
    "credit_card",
)
masked(
    "credit_card: after an invalid group",
    "Ref 1234 ",
    "4111 1111 1111 1111",
    "",
    "CARD",
    "credit_card",
    note="A rejected candidate does not hide a valid one starting inside it.",
)
masked(
    "credit_card: combining mark inside",
    "Card ",
    "4111\u0301 1111 1111 1111",
    "",
    "CARD",
    "credit_card",
)
masked(
    "credit_card: fullwidth digits",
    "Card ",
    "４１１１ １１１１ １１１１ １１１１",
    "",
    "CARD",
    "credit_card",
)
kept("credit_card: spelled out (not caught)", "four one one one, then twelve ones")
kept(
    "credit_card: split over two lines (not caught)",
    "Card 4111 1111\n1111 1111",
    note="A number broken by a newline is two numbers.",
)
add(
    "credit_card: blocked",
    "Card 4111 1111 1111 1111 and marie@example.com",
    policy="block_card",
    blocked={"credit_card": 1},
)
add(
    "credit_card: two blocked",
    "4111111111111111 / 5555555555554444",
    policy="block_card",
    blocked={"credit_card": 2},
)
add(
    "credit_card: block policy, nothing found",
    "Mail marie@example.com",
    "Mail [EMAIL_1]",
    policy="block_card",
    counts={"email": 1},
)

# ── iban ───────────────────────────────────────────────────────────────────
for label, value in (
    ("fr spaced", "FR76 3000 6000 0112 3456 7890 189"),
    ("fr contiguous", "FR7630006000011234567890189"),
    ("de", "DE89 3704 0044 0532 0130 00"),
    ("gb letters", "GB82 WEST 1234 5698 7654 32"),
    ("be", "BE68 5390 0754 7034"),
    ("no shortest", "NO93 8601 1117 947"),
    ("lowercase", "fr76 3000 6000 0112 3456 7890 189"),
):
    masked(f"iban: {label}", "IBAN ", value, " thanks", "IBAN", "iban")
masked(
    "iban: words after a full group",
    "IBAN ",
    "BE68 5390 0754 7034",
    " ABCD",
    "IBAN",
    "iban",
    note="The IBAN is cut at its country length when the rest starts at a space.",
)
kept(
    "iban: glued after a full group",
    "IBAN BE68539007547034ABCD",
    note="An IBAN glued to more letters is not cut.",
)
kept("iban: bad check digits", "IBAN FR76 3000 6000 0112 3456 7890 188 thanks")
kept("iban: unknown country", "IBAN XX76 3000 6000 0112 3456 7890 189 thanks")
kept("iban: too short for country", "IBAN FR76 3000 6000 0112 3456 7890 thanks")
add(
    "iban: blocked",
    "Pay FR7630006000011234567890189 now",
    policy="block_card",
    blocked={"iban": 1},
)

# ── bic ────────────────────────────────────────────────────────────────────
masked("bic: after BIC", "BIC : ", "EXMPFRPPXXX", "", "BIC", "bic")
masked("bic: after SWIFT", "code SWIFT ", "TESTDEFF", " please", "BIC", "bic")
masked("bic: keyword after", "", "TESTDEFF", " is the bic", "BIC", "bic")
kept("bic: no keyword", "TESTDEFF is a word here")
kept("bic: invalid country", "BIC: ABCDXX12")
kept("bic: keyword too far", "BIC" + " " * 70 + "TESTDEFF")
kept("bic: keyword too far after", "TESTDEFF" + " " * 70 + "bic")
kept("bic: lowercase (not caught)", "bic: exmpfrppxxx")

# ── fr_nir ─────────────────────────────────────────────────────────────────
masked("fr_nir: contiguous", "NIR ", NIR, "", "NIR", "fr_nir")
masked(
    "fr_nir: spaced",
    "Sécurité sociale : ",
    spaced(NIR, [1, 2, 2, 2, 3, 3, 2]),
    ".",
    "NIR",
    "fr_nir",
)
masked(
    "fr_nir: dotted",
    "n° ",
    spaced(NIR, [1, 2, 2, 2, 3, 3, 2], "."),
    "",
    "NIR",
    "fr_nir",
)
masked("fr_nir: key apart", "n° ", NIR[:13] + " " + NIR[13:], "", "NIR", "fr_nir")
masked("fr_nir: corsica 2A", "NIR ", NIR_CORSICA, "", "NIR", "fr_nir")
masked(
    "fr_nir: corsica 2B spaced",
    "NIR ",
    spaced(NIR_CORSICA_B, [1, 2, 2, 2, 3, 3, 2]),
    "",
    "NIR",
    "fr_nir",
)
masked("fr_nir: corsica lowercase", "NIR ", NIR_CORSICA.lower(), "", "NIR", "fr_nir")
kept("fr_nir: wrong key", "NIR " + NIR_BAD)
kept("fr_nir: month 13", "NIR 1851378006084" + nir_key("1851378006084"))
kept("fr_nir: first digit 9", "NIR 9850578006084" + nir_key("9850578006084"))
kept(
    "fr_nir: mixed separators (not caught)",
    "NIR " + NIR[0] + " " + NIR[1:3] + "." + NIR[3:],
)

# ── fr_siren / fr_siret / fr_vat ───────────────────────────────────────────
masked("fr_siren: keyword before", "SIREN : ", SIREN, "", "SIREN", "fr_siren")
masked(
    "fr_siren: spaced", "RCS Paris ", spaced(SIREN, [3, 3, 3]), "", "SIREN", "fr_siren"
)
masked(
    "fr_siren: keyword after",
    "",
    spaced(SIREN, [3, 3, 3]),
    " (siren)",
    "SIREN",
    "fr_siren",
)
masked("fr_siren: la poste", "SIREN ", "356 000 000", "", "SIREN", "fr_siren")
kept("fr_siren: no keyword", "Order " + SIREN + " shipped")
kept("fr_siren: luhn fails", "SIREN " + SIREN_BAD)
masked(
    "fr_siret: keyword", "SIRET ", spaced(SIRET, [3, 3, 3, 5]), "", "SIRET", "fr_siret"
)
masked("fr_siret: contiguous", "siret:", SIRET, "", "SIRET", "fr_siret")
masked(
    "fr_siret: la poste rule",
    "SIRET ",
    spaced(SIRET_LA_POSTE, [3, 3, 3, 5]),
    "",
    "SIRET",
    "fr_siret",
)
masked(
    "fr_siret: la poste head office",
    "SIRET ",
    "35600000000048",
    "",
    "SIRET",
    "fr_siret",
)
kept("fr_siret: la poste rule broken", "SIRET 35600000049838")
kept("fr_siret: luhn fails", "SIRET " + SIRET_BAD)
kept("fr_siret: no keyword", "Ref " + SIRET)
add(
    "fr_siret: both keywords, siret wins",
    "SIREN/SIRET " + spaced(SIRET, [3, 3, 3, 5]),
    "SIREN/SIRET [SIRET_1]",
    counts={"fr_siret": 1},
    note="The SIREN inside a SIRET is shorter: the longest match wins.",
)
masked("fr_vat: compact", "TVA ", VAT, "", "VAT_NUMBER", "fr_vat")
masked(
    "fr_vat: spaced",
    "VAT ",
    "FR " + VAT[2:4] + " " + spaced(SIREN, [3, 3, 3]),
    "",
    "VAT_NUMBER",
    "fr_vat",
)
masked("fr_vat: lowercase prefix", "", "fr" + VAT[2:], "", "VAT_NUMBER", "fr_vat")
masked("fr_vat: monaco", "TVA ", "FR53000004605", "", "VAT_NUMBER", "fr_vat")
kept("fr_vat: wrong key", "TVA " + VAT_BAD)
kept("fr_vat: siren fails", "TVA FR" + vat_key(SIREN_BAD) + SIREN_BAD)
add(
    "fr_vat: wins over the siren inside",
    "SIREN et TVA : " + VAT,
    "SIREN et TVA : [VAT_NUMBER_1]",
    counts={"fr_vat": 1},
)

# ── fr_plate ───────────────────────────────────────────────────────────────
for label, value in (
    ("dashes", "AB-123-CD"),
    ("spaces", "AB 123 CD"),
    ("glued", "AB123CD"),
    ("en dashes", "AB\u2013123\u2013CD"),
):
    masked(f"fr_plate: {label}", "plaque ", value, " garée", "PLATE", "fr_plate")
kept("fr_plate: 000", "plaque AB-000-CD")
kept("fr_plate: letter I", "plaque IB-123-CD")
masked(
    "fr_plate: mixed separators", "plaque ", "AB-123 CD", " garée", "PLATE", "fr_plate"
)
kept("fr_plate: lowercase (not caught)", "plaque ab-123-cd")
kept("fr_plate: old format (not caught)", "plaque 1234 AB 75")

# ── keys and tokens ────────────────────────────────────────────────────────
for detector, label, value in (
    ("key_openai", "project key", KEY_OPENAI),
    ("key_openai", "legacy key", KEY_OPENAI_LEGACY),
    ("key_anthropic", "key", KEY_ANTHROPIC),
    ("key_google", "key", KEY_GOOGLE),
    ("key_deepseek", "key", KEY_DEEPSEEK),
    ("key_idside", "live key", KEY_IDSIDE),
    ("key_idside", "test key", KEY_IDSIDE_TEST),
    ("key_aws", "access key id", KEY_AWS),
    ("key_github", "classic token", KEY_GITHUB),
    ("key_github", "fine-grained token", KEY_GITHUB_PAT),
    ("key_gitlab", "token", KEY_GITLAB),
    ("key_slack", "bot token", KEY_SLACK),
    ("key_slack", "webhook", SLACK_HOOK),
    ("key_stripe", "secret key", KEY_STRIPE),
    ("key_stripe", "restricted key", KEY_STRIPE_RESTRICTED),
    ("key_stripe", "webhook secret", KEY_STRIPE_WEBHOOK),
    ("key_twilio", "api key", KEY_TWILIO),
    ("key_sendgrid", "key", KEY_SENDGRID),
    ("key_npm", "token", KEY_NPM),
    ("key_pypi", "token", KEY_PYPI),
):
    masked(f"{detector}: {label}", "key ", value, " end", "API_KEY", detector)
    masked(f"{detector}: {label} quoted", '{"k": "', value, '"}', "API_KEY", detector)
masked(
    "key_mistral: with keyword",
    "MISTRAL_API_KEY=",
    KEY_MISTRAL,
    "",
    "API_KEY",
    "key_mistral",
)
masked(
    "key_mistral: keyword after",
    "",
    KEY_MISTRAL,
    " is my mistral key",
    "API_KEY",
    "key_mistral",
)
kept("key_mistral: no keyword", "id " + KEY_MISTRAL)
masked(
    "key_azure: account key",
    "DefaultEndpointsProtocol=https;AccountName=demo;AccountKey=",
    AZURE_ACCOUNT_KEY,
    ";EndpointSuffix=core.windows.net",
    "API_KEY",
    "key_azure",
)
masked(
    "key_azure: hex with keyword",
    "AZURE_OPENAI_KEY=",
    AZURE_HEX,
    "",
    "API_KEY",
    "key_azure",
)
kept("key_azure: hex without keyword", "digest " + AZURE_HEX)
kept("key_openai: too short", "key sk-abc123 end")
kept("key_aws: lowercase", "key " + KEY_AWS.lower() + " end")
kept("key_github: too short", "key ghp_abc end")
kept("key_npm: glued", "key x" + KEY_NPM + " end")
add(
    "key_stripe: idside shape counts as idside",
    "key " + "sk_live_" + KEY_IDSIDE[8:] + " end",
    "key [API_KEY_1] end",
    counts={"key_idside": 1},
    note="sk_live_ or sk_test_ and 32 letters and digits is the ID Side key format, and nothing in a value tells it apart from a Stripe key of that length: it is counted as an ID Side key. Masked or blocked either way.",
)
masked("jwt: token", "token ", JWT, " end", "TOKEN", "jwt")
add(
    "jwt: after bearer counts as jwt",
    "Authorization: Bearer " + JWT,
    "Authorization: Bearer [TOKEN_1]",
    counts={"jwt": 1},
)
masked(
    "bearer_token: opaque token",
    "Authorization: Bearer ",
    BEARER,
    "",
    "TOKEN",
    "bearer_token",
)
masked(
    "bearer_token: lowercase",
    "authorization: bearer ",
    BEARER,
    "",
    "TOKEN",
    "bearer_token",
)
masked(
    "bearer_token: basic",
    "Authorization: Basic ",
    "dXNlcjpwYXNzd29yZDEyMw==",
    "",
    "TOKEN",
    "bearer_token",
)
kept("bearer_token: too short", "Bearer abc123")
kept("bearer_token: prose", "bear with me")
masked("private_key: pem", "key:\n", PEM, "\nthanks", "PRIVATE_KEY", "private_key")
masked("private_key: openssh", "", OPENSSH, "", "PRIVATE_KEY", "private_key")
add(
    "private_key: no end line",
    "-----BEGIN " + "PRIVATE KEY-----\n" + PEM_BODY + "\n?",
    "[PRIVATE_KEY_1]",
    counts={"private_key": 1},
    note="Without its END line, a BEGIN line followed by key material is masked with everything after it, to the end of the text: too much, never half.",
)
masked(
    "private_key: encrypted pem headers",
    "",
    "-----BEGIN RSA "
    + "PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0A1B2C\n\n"
    + PEM_BODY
    + "\n-----END RSA "
    + "PRIVATE KEY-----",
    "",
    "PRIVATE_KEY",
    "private_key",
)
kept(
    "private_key: public key",
    "-----BEGIN PUBLIC KEY-----\nMIIBIjANBg\n-----END PUBLIC KEY-----",
)
masked(
    "connection_string_password: postgres",
    "DATABASE_URL=postgres://",
    "admin:S3cretPass",
    "@db.internal:5432/app",
    "PASSWORD",
    "connection_string_password",
)
masked(
    "connection_string_password: mongodb+srv",
    "mongodb+srv://",
    "app:p4ss",
    "@cluster0.example.net/db",
    "PASSWORD",
    "connection_string_password",
)
masked(
    "connection_string_password: at sign in password",
    "redis://",
    ":p@ss@w0rd",
    "@cache:6379",
    "PASSWORD",
    "connection_string_password",
)
masked(
    "connection_string_password: key value",
    "Server=db;Database=app;User Id=sa;Password=",
    "S3cret!",
    ";",
    "PASSWORD",
    "connection_string_password",
)
masked(
    "connection_string_password: user only is a url credential",
    "postgres://",
    "admin",
    "@db.internal/app",
    "CREDENTIALS",
    "url_credentials",
)
masked(
    "url_credentials: https",
    "git clone https://",
    "user:tok3n",
    "@git.example.com/repo.git",
    "CREDENTIALS",
    "url_credentials",
)
masked(
    "url_credentials: token as user",
    "https://",
    "ghtoken123",
    "@git.example.com/x",
    "CREDENTIALS",
    "url_credentials",
)
kept("url_credentials: no userinfo", "see https://example.com/path?x=1")
masked(
    "generic_secret: equals",
    "password=",
    "hunter2hunter",
    "",
    "SECRET",
    "generic_secret",
)
masked(
    "generic_secret: json",
    '{"client_secret": "',
    "abcdef123456",  # pragma: allowlist secret
    '"}',
    "SECRET",
    "generic_secret",
)
masked(
    "generic_secret: prefixed name",
    "DB_PASSWORD = '",
    "Tr0ub4dor&3",
    "'",
    "SECRET",
    "generic_secret",
)
masked(
    "generic_secret: arrow",
    "'auth_token' => '",
    "q1w2e3r4t5",
    "'",
    "SECRET",
    "generic_secret",
)
kept("generic_secret: too short", "password=abc")
kept("generic_secret: prose", "the password is in the vault")
add(
    "generic_secret: off by default but on in the reference",
    "api_key=" + "zz" * 10,
    "api_key=[SECRET_1]",
    counts={"generic_secret": 1},
)

# ── overlaps ───────────────────────────────────────────────────────────────
add(
    "overlap: email inside url credentials is not an email",
    "https://marie:secret@acme.example/x",  # pragma: allowlist secret
    "https://[CREDENTIALS_1]@acme.example/x",
    counts={"url_credentials": 1},
)
add(
    "overlap: url credentials and email side by side",
    "https://bob:pw123@git.example.com and bob@example.com",  # pragma: allowlist secret
    "https://[CREDENTIALS_1]@git.example.com and [EMAIL_1]",
    counts={"url_credentials": 1, "email": 1},
)
add(
    "overlap: key in a generic assignment counts as the key",
    "api_key = '" + KEY_OPENAI + "'",
    "api_key = '[API_KEY_1]'",
    counts={"key_openai": 1},
)
add(
    "overlap: mistral key in an api_key assignment",
    "MISTRAL_API_KEY=" + KEY_MISTRAL,
    "MISTRAL_API_KEY=[API_KEY_1]",
    counts={"key_mistral": 1},
)
add(
    "overlap: card digits are not a phone",
    "4111111111111111",
    "[CARD_1]",
    counts={"credit_card": 1},
)
add(
    "overlap: iban digits are not a card",
    "FR7630006000011234567890189",
    "[IBAN_1]",
    counts={"iban": 1},
)
add(
    "overlap: custom pattern inside an email loses to the email",
    "prj-1234@example.com",
    "[EMAIL_1]",
    policy="custom",
    counts={"email": 1},
)
add(
    "overlap: longer custom term wins and takes the other with it",
    "Projet Atlas Corp",
    "[PROJECT_1]",
    policy="custom",
    counts={"custom:PROJECT": 1},
    note="Projet Atlas and Atlas Corp overlap: the longer names the placeholder, and covers the other, so no part of a value found stays in the clear.",
)

# ── placeholders ───────────────────────────────────────────────────────────
add("placeholder: existing placeholder untouched", "Hello [EMAIL_1]", "Hello [EMAIL_1]")
add(
    "placeholder: numbering starts above existing",
    "[EMAIL_3] and marie@example.com",
    "[EMAIL_3] and [EMAIL_4]",
    counts={"email": 1},
)
add(
    "placeholder: other labels do not count",
    "[PHONE_7] and marie@example.com",
    "[PHONE_7] and [EMAIL_1]",
    counts={"email": 1},
)
add(
    "placeholder: label with underscores",
    "[IP_ADDRESS_2] host 10.1.2.3",
    "[IP_ADDRESS_2] host [IP_ADDRESS_3]",
    counts={"ip_address": 1},
)
add(
    "placeholder: fullwidth placeholder counts",
    "［ＥＭＡＩＬ＿５］ and marie@example.com",
    "［ＥＭＡＩＬ＿５］ and [EMAIL_6]",
    counts={"email": 1},
)
add(
    "placeholder: keyword inside a placeholder is ignored",
    "[BIC_1] TESTDEFF",
    "[BIC_1] TESTDEFF",
    note="The word BIC inside a placeholder is not a keyword.",
)
add(
    "placeholder: generic secret never takes a placeholder",
    "password=[SECRET_1]",
    "password=[SECRET_1]",
)
add(
    "placeholder: not a placeholder without number",
    "[EMAIL] marie@example.com",
    "[EMAIL] [EMAIL_1]",
    counts={"email": 1},
)
add(
    "placeholder: custom pattern next to a placeholder",
    "[REF_1]PRJ-1234",
    "[REF_1][REF_2]",
    policy="custom",
    counts={"custom:REF": 1},
)

# ── idempotence through keyword windows ────────────────────────────────────
LONG_LOCAL = "a" * 60
add(
    "idempotence: masking brings a keyword in reach",
    "SIREN " + LONG_LOCAL + "@example.com " + SIREN,
    "SIREN [EMAIL_1] [SIREN_1]",
    counts={"email": 1, "fr_siren": 1},
    note="The SIREN is out of the keyword window until the address is masked; the engine "
    "runs again until nothing changes, so cleaning the output changes nothing.",
)

# ── custom entries ─────────────────────────────────────────────────────────
for label, text, expected, counts in (
    ("term exact", "About Projet Atlas.", "About [PROJECT_1].", {"custom:PROJECT": 1}),
    (
        "term uppercase",
        "PROJET ATLAS starts",
        "[PROJECT_1] starts",
        {"custom:PROJECT": 1},
    ),
    (
        "term lowercase",
        "the projet atlas plan",
        "the [PROJECT_1] plan",
        {"custom:PROJECT": 1},
    ),
    ("term two spaces", "Projet  Atlas", "[PROJECT_1]", {"custom:PROJECT": 1}),
    ("term newline", "Projet\nAtlas", "[PROJECT_1]", {"custom:PROJECT": 1}),
    ("term nbsp", "Projet\u00a0Atlas", "[PROJECT_1]", {"custom:PROJECT": 1}),
    ("term not a prefix of a word", "Projet Atlasien", "Projet Atlasien", {}),
    ("term not inside a word", "SuperProjet Atlas", "SuperProjet Atlas", {}),
    (
        "term accents ignored",
        "chez compagnie generale d'exemple",
        "chez [CLIENT_1]",
        {"custom:CLIENT": 1},
    ),
    (
        "term accents kept",
        "Compagnie Générale d'Exemple",
        "[CLIENT_1]",
        {"custom:CLIENT": 1},
    ),
    ("term in french quotes", "«Projet Atlas»", "«[PROJECT_1]»", {"custom:PROJECT": 1}),
    (
        "term words joined by an underscore (not caught)",
        "projet_atlas",
        "projet_atlas",
        {},
    ),
    (
        "term typographic apostrophe",
        "chez Compagnie Générale d\u2019Exemple",
        "chez [CLIENT_1]",
        {"custom:CLIENT": 1},
    ),
    (
        "domain email",
        "write to bob@acme.example",
        "write to [DOMAIN_1]",
        {"custom:DOMAIN": 1},
    ),
    (
        "domain subdomain email",
        "x@mail.acme.example",
        "[DOMAIN_1]",
        {"custom:DOMAIN": 1},
    ),
    (
        "domain url",
        "see https://www.acme.example/path?q=1 now",
        "see [DOMAIN_1] now",
        {"custom:DOMAIN": 1},
    ),
    (
        "domain url with credentials",
        "https://u:p@acme.example/x",
        "[DOMAIN_1]",
        {"custom:DOMAIN": 1},
    ),
    (
        "domain bare",
        "the acme.example site",
        "the [DOMAIN_1] site",
        {"custom:DOMAIN": 1},
    ),
    (
        "domain bare subdomain",
        "api.acme.example is up",
        "[DOMAIN_1] is up",
        {"custom:DOMAIN": 1},
    ),
    ("domain uppercase", "ACME.EXAMPLE", "[DOMAIN_1]", {"custom:DOMAIN": 1}),
    ("domain other domain", "notacme.example", "notacme.example", {}),
    ("domain suffix of a longer host", "acme.example.org", "acme.example.org", {}),
    (
        "domain with port",
        "http://acme.example:8080/x y",
        "[DOMAIN_1] y",
        {"custom:DOMAIN": 1},
    ),
    ("pattern match", "ticket PRJ-1234 open", "ticket [REF_1] open", {"custom:REF": 1}),
    (
        "pattern lowercase",
        "ticket prj-1234 open",
        "ticket [REF_1] open",
        {"custom:REF": 1},
    ),
    ("pattern too many digits", "PRJ-12345", "PRJ-12345", {}),
    ("pattern glued before", "XPRJ-1234", "XPRJ-1234", {}),
    ("pattern too few digits", "PRJ-123", "PRJ-123", {}),
    ("pattern letters and run", "CASE-AB1x9z", "[CASE_1]", {"custom:CASE": 1}),
    (
        "pattern punctuation at both ends",
        "see (12), x(34) and (123)",
        "see [PAIR_1], x[PAIR_2] and (123)",
        {"custom:PAIR": 2},
    ),
    ("pattern run needs one character", "CASE-AB1", "CASE-AB1", {}),
    (
        "pattern same value twice",
        "PRJ-1234 and PRJ-1234 and PRJ-9999",
        "[REF_1] and [REF_1] and [REF_2]",
        {"custom:REF": 3},
    ),
    (
        "custom and detector together",
        "Projet Atlas: marie@example.com, 06 12 34 56 78",
        "[PROJECT_1]: [EMAIL_1], [PHONE_1]",
        {"custom:PROJECT": 1, "email": 1, "phone": 1},
    ),
):
    add(f"custom: {label}", text, expected, policy="custom", counts=counts)
add("custom: run longer than 64 is not a run", "CASE-AB1" + "x" * 65, policy="custom")
add(
    "custom: run of 64",
    "CASE-AB1" + "x" * 64,
    "[CASE_1]",
    policy="custom",
    counts={"custom:CASE": 1},
)
add(
    "custom: domain blocked in an email",
    "send to ceo@rival.example",
    policy="custom_block",
    blocked={"custom:RIVAL": 1},
)
add(
    "custom: term blocked",
    "OPERATION NORDLYS starts",
    policy="custom_block",
    blocked={"custom:CODENAME": 1},
)
add(
    "custom: block wins over a longer mask",
    "ceo.rival.example@example.com",
    policy="custom_block",
    blocked={"custom:RIVAL": 1},
    note="A blocking rule blocks even inside a longer masked value.",
)
add(
    "custom: blocked counts several",
    "a@rival.example b@rival.example Opération Nordlys",
    policy="custom_block",
    blocked={"custom:RIVAL": 2, "custom:CODENAME": 1},
)
add(
    "custom: nothing to block",
    "write to bob@example.com",
    "write to [EMAIL_1]",
    policy="custom_block",
    counts={"email": 1},
)
add(
    "future: unknown ids, actions and kinds are ignored",
    "marie@example.com 06 12 34 56 78 xxx Projet Atlas",
    "[EMAIL_1] 06 12 34 56 78 xxx Projet Atlas",
    policy="future",
    counts={"email": 1},
)

# ── policies the engine refuses ────────────────────────────────────────────
BAD_POLICIES = {
    "schema 2": {"schema": 2, "detectors": {}, "attachments": "allow", "custom": []},
    "no schema": {"detectors": {}, "attachments": "allow", "custom": []},
    "detectors not an object": {
        "schema": 1,
        "detectors": [],
        "attachments": "allow",
        "custom": [],
    },
    "custom not a list": {
        "schema": 1,
        "detectors": {},
        "attachments": "allow",
        "custom": {},
    },
    "custom entry not an object": {"schema": 1, "detectors": {}, "custom": ["x"]},
    "label lowercase": {
        "schema": 1,
        "detectors": {},
        "custom": [{"kind": "term", "value": "x", "action": "mask", "label": "proj"}],
    },
    "label too long": {
        "schema": 1,
        "detectors": {},
        "custom": [{"kind": "term", "value": "x", "action": "mask", "label": "A" * 25}],
    },
    "value too long": {
        "schema": 1,
        "detectors": {},
        "custom": [
            {"kind": "term", "value": "x" * 201, "action": "mask", "label": "A"}
        ],
    },
    "value empty": {
        "schema": 1,
        "detectors": {},
        "custom": [{"kind": "term", "value": "", "action": "mask", "label": "A"}],
    },
    "term only invisible": {
        "schema": 1,
        "detectors": {},
        "custom": [
            {"kind": "term", "value": "\u200b \u200b", "action": "mask", "label": "A"}
        ],
    },
    "pattern double star": {
        "schema": 1,
        "detectors": {},
        "custom": [
            {"kind": "pattern", "value": "A-**", "action": "mask", "label": "A"}
        ],
    },
    "pattern star before a letter": {
        "schema": 1,
        "detectors": {},
        "custom": [
            {"kind": "pattern", "value": "CLI-*A", "action": "mask", "label": "A"}
        ],
    },
    "pattern star before a digit wildcard": {
        "schema": 1,
        "detectors": {},
        "custom": [
            {"kind": "pattern", "value": "*#-X", "action": "mask", "label": "A"}
        ],
    },
    "domain without dot": {
        "schema": 1,
        "detectors": {},
        "custom": [
            {"kind": "domain", "value": "localhost", "action": "mask", "label": "A"}
        ],
    },
    "domain with path": {
        "schema": 1,
        "detectors": {},
        "custom": [
            {
                "kind": "domain",
                "value": "acme.example/x",
                "action": "mask",
                "label": "A",
            }
        ],
    },
    "too many entries": {
        "schema": 1,
        "detectors": {},
        "custom": [
            {"kind": "term", "value": f"t{i}", "action": "mask", "label": "A"}
            for i in range(501)
        ],
    },
    "action not a string": {"schema": 1, "detectors": {"email": 1}, "custom": []},
    "detectors null": {"schema": 1, "detectors": None},
    "custom null": {"schema": 1, "custom": None},
    "attachments null": {"schema": 1, "attachments": None},
    "schema true": {"schema": True},
    "schema as a string": {"schema": "1"},
    "attachments not a string": {
        "schema": 1,
        "detectors": {},
        "attachments": True,
        "custom": [],
    },
}
for label, bad in BAD_POLICIES.items():
    add(f"policy refused: {label}", "marie@example.com", policy=bad, error="policy")
add(
    "policy: unknown attachments value blocks attachments",
    {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://img.example.com/a.png"},
                    }
                ],
            }
        ]
    },
    policy={"schema": 1, "detectors": {}, "attachments": "quarantine", "custom": []},
    provider="openai",
    blocked={"attachments": 1},
    attachments=1,
)
add(
    "policy: missing optional fields",
    "marie@example.com",
    "[EMAIL_1]",
    policy={"schema": 1, "detectors": {"email": "mask"}},
    counts={"email": 1},
)
add(
    "policy: off changes nothing", "marie@example.com 4111 1111 1111 1111", policy="off"
)

# ── provider bodies ────────────────────────────────────────────────────────
add(
    "openai chat: system, user, tool call and tool result",
    {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Support for marie@example.com only."},
            {
                "role": "user",
                "name": "marie",
                "content": [
                    {"type": "text", "text": "My card is 4111 1111 1111 1111."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://img.example.com/receipt.png",
                            "detail": "auto",
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_06123456789",
                        "type": "function",
                        "function": {
                            "name": "lookup_customer",
                            "arguments": '{"email": "marie@example.com", "phone": "06 12 34 56 78"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_06123456789",
                "content": "IBAN FR7630006000011234567890189",
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup_customer",
                    "description": "Find a customer; escalate to ops@example.com.",
                    "parameters": {
                        "type": "object",
                        "properties": {"email": {"type": "string"}},
                    },
                },
            }
        ],
        "metadata": {"ticket": "contact paul@example.com"},
        "user": "marie@example.com",
    },
    {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Support for [EMAIL_1] only."},
            {
                "role": "user",
                "name": "marie",
                "content": [
                    {"type": "text", "text": "My card is [CARD_1]."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://img.example.com/receipt.png",
                            "detail": "auto",
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_06123456789",
                        "type": "function",
                        "function": {
                            "name": "lookup_customer",
                            "arguments": '{"email": "[EMAIL_1]", "phone": "[PHONE_1]"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_06123456789",
                "content": "IBAN [IBAN_1]",
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup_customer",
                    "description": "Find a customer; escalate to [EMAIL_3].",
                    "parameters": {
                        "type": "object",
                        "properties": {"email": {"type": "string"}},
                    },
                },
            }
        ],
        "metadata": {"ticket": "contact [EMAIL_2]"},
        "user": "[EMAIL_1]",
    },
    provider="openai",
    counts={"email": 5, "credit_card": 1, "phone": 1, "iban": 1},
    attachments=1,
    note="Walked in sorted key order: messages, then metadata, then tools, then user.",
)
add(
    "openai chat: structural fields are never changed",
    {
        "model": "marie@example.com",
        "messages": [{"role": "user", "name": "06 12 34 56 78", "content": "hi"}],
        "tool_choice": "marie@example.com",
        "service_tier": "default",
    },
    provider="openai",
)
add(
    "openai chat: string content",
    {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "IP 10.1.2.3, MAC 00:1A:2B:3C:4D:5E"}],
    },
    {
        "model": "gpt-4o",
        "messages": [
            {"role": "user", "content": "IP [IP_ADDRESS_1], MAC [MAC_ADDRESS_1]"}
        ],
    },
    provider="openai",
    counts={"ip_address": 1, "mac_address": 1},
)
add(
    "openai chat: data uri image counts as an attachment",
    {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                    },
                ],
            }
        ],
    },
    provider="openai",
    attachments=1,
)
add(
    "openai chat: attachment blocked",
    {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "marie@example.com"},
                    {"type": "file", "file": {"file_id": "file-abc123"}},
                ],
            }
        ],
    },
    provider="openai",
    policy="attachments_block",
    blocked={"attachments": 1},
    attachments=1,
)
add(
    "openai chat: empty attachment fields are not attachments",
    {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "file_id": None,
                        "file_url": "",
                        "file_data": {},
                        "image_url": [],
                    }
                ],
            }
        ],
    },
    provider="openai",
    policy="attachments_block",
)
add(
    "openai responses: input, instructions, function output",
    {
        "model": "gpt-4.1",
        "instructions": "Never reveal sk_test_" + KEY_IDSIDE_TEST[8:] + ".",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Ship to AB-123-CD, call +33 6 12 34 56 78",
                    },
                    {
                        "type": "input_image",
                        "image_url": "https://img.example.com/x.png",
                    },
                    {
                        "type": "input_file",
                        "file_id": "file-xyz",
                        "filename": "invoice.pdf",
                    },
                ],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "get_order",
                "arguments": '{"order": "PRJ-1234"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": '{"owner": "paul@example.com"}',
            },
        ],
        "previous_response_id": "resp_0612345678",
    },
    {
        "model": "gpt-4.1",
        "instructions": "Never reveal [API_KEY_1].",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Ship to [PLATE_1], call [PHONE_1]"},
                    {
                        "type": "input_image",
                        "image_url": "https://img.example.com/x.png",
                    },
                    {
                        "type": "input_file",
                        "file_id": "file-xyz",
                        "filename": "invoice.pdf",
                    },
                ],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "get_order",
                "arguments": '{"order": "PRJ-1234"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": '{"owner": "[EMAIL_1]"}',
            },
        ],
        "previous_response_id": "resp_0612345678",
    },
    provider="openai",
    counts={"key_idside": 1, "fr_plate": 1, "phone": 1, "email": 1},
    attachments=2,
)
add(
    "openai responses: input as a string",
    {"model": "gpt-4.1", "input": "Mail marie@example.com"},
    {"model": "gpt-4.1", "input": "Mail [EMAIL_1]"},
    provider="openai",
    counts={"email": 1},
)
add(
    "anthropic: system blocks, image, document, tool use and result",
    {
        "model": "claude-sonnet-4-5",
        "max_tokens": 512,
        "system": [
            {
                "type": "text",
                "text": "The operator is ops@example.com",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgo=",
                        },
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "text",
                            "media_type": "text/plain",
                            "data": "Patient NIR " + NIR,
                        },
                        "title": "Note for marie@example.com",
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "url",
                            "url": "https://docs.example.com/a.pdf",
                        },
                    },
                    {"type": "text", "text": "Summarise."},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_06123456789",
                        "name": "send_mail",
                        "input": {
                            "to": "paul@example.com",
                            "cc": ["ops@example.com"],
                            "urgent": True,
                        },
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_06123456789",
                        "content": [
                            {"type": "text", "text": "sent to paul@example.com"}
                        ],
                    }
                ],
            },
        ],
        "stop_sequences": ["marie@example.com"],
    },
    {
        "model": "claude-sonnet-4-5",
        "max_tokens": 512,
        "system": [
            {
                "type": "text",
                "text": "The operator is [EMAIL_2]",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgo=",
                        },
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "text",
                            "media_type": "text/plain",
                            "data": "Patient NIR [NIR_1]",
                        },
                        "title": "Note for [EMAIL_1]",
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "url",
                            "url": "https://docs.example.com/a.pdf",
                        },
                    },
                    {"type": "text", "text": "Summarise."},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_06123456789",
                        "name": "send_mail",
                        "input": {
                            "to": "[EMAIL_3]",
                            "cc": ["[EMAIL_2]"],
                            "urgent": True,
                        },
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_06123456789",
                        "content": [{"type": "text", "text": "sent to [EMAIL_3]"}],
                    }
                ],
            },
        ],
        "stop_sequences": ["[EMAIL_1]"],
    },
    provider="anthropic",
    counts={"email": 6, "fr_nir": 1},
    attachments=2,
    note="In a content block, `source` sorts before `title`; in tool input, `cc` before `to`.",
)
add(
    "gemini: contents, inline data, file data, function call and response",
    {
        "model": "gemini-2.5-flash",
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": "Plate AB-123-CD and IBAN DE89 3704 0044 0532 0130 00"},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": "/9j/4AAQSkZJRg==",
                        }
                    },
                    {
                        "fileData": {
                            "mimeType": "application/pdf",
                            "fileUri": "https://files.example.com/x",
                        }
                    },
                ],
            },
            {
                "role": "model",
                "parts": [
                    {
                        "function_call": {
                            "name": "notify",
                            "args": {"email": "marie@example.com"},
                        }
                    }
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "function_response": {
                            "name": "notify",
                            "response": {"status": "sent to 06 12 34 56 78"},
                        }
                    }
                ],
            },
        ],
        "config": {
            "system_instruction": "Escalate to ops@example.com",
            "temperature": 0.2,
        },
    },
    {
        "model": "gemini-2.5-flash",
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": "Plate [PLATE_1] and IBAN [IBAN_1]"},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": "/9j/4AAQSkZJRg==",
                        }
                    },
                    {
                        "fileData": {
                            "mimeType": "application/pdf",
                            "fileUri": "https://files.example.com/x",
                        }
                    },
                ],
            },
            {
                "role": "model",
                "parts": [
                    {
                        "function_call": {
                            "name": "notify",
                            "args": {"email": "[EMAIL_2]"},
                        }
                    }
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "function_response": {
                            "name": "notify",
                            "response": {"status": "sent to [PHONE_1]"},
                        }
                    }
                ],
            },
        ],
        "config": {"system_instruction": "Escalate to [EMAIL_1]", "temperature": 0.2},
    },
    provider="gemini",
    counts={"fr_plate": 1, "iban": 1, "email": 2, "phone": 1},
    attachments=2,
    note="`config` sorts before `contents`.",
)
add(
    "gemini: contents as a plain string",
    {"model": "gemini-2.5-flash", "contents": "Mail marie@example.com"},
    {"model": "gemini-2.5-flash", "contents": "Mail [EMAIL_1]"},
    provider="gemini",
    counts={"email": 1},
)
add(
    "mistral: chat with image and document urls",
    {
        "model": "mistral-large-latest",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Contact 07 98 76 54 32"},
                    {"type": "image_url", "image_url": "https://img.example.com/a.png"},
                    {
                        "type": "document_url",
                        "document_url": "https://docs.example.com/b.pdf",
                    },
                ],
            }
        ],
    },
    {
        "model": "mistral-large-latest",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Contact [PHONE_1]"},
                    {"type": "image_url", "image_url": "https://img.example.com/a.png"},
                    {
                        "type": "document_url",
                        "document_url": "https://docs.example.com/b.pdf",
                    },
                ],
            }
        ],
    },
    provider="mistral",
    counts={"phone": 1},
    attachments=2,
)
add(
    "mistral: fim prompt and suffix",
    {
        "model": "codestral-latest",
        "prompt": "db = 'postgres://admin:pw@db'",  # pragma: allowlist secret
        "suffix": "# owner: marie@example.com",
    },
    {
        "model": "codestral-latest",
        "prompt": "db = 'postgres://[PASSWORD_1]@db'",
        "suffix": "# owner: [EMAIL_1]",
    },
    provider="mistral",
    counts={"connection_string_password": 1, "email": 1},
)
add(
    "deepseek: chat",
    {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Key " + KEY_DEEPSEEK},
            {"role": "user", "content": "hello"},
        ],
    },
    {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Key [API_KEY_1]"},
            {"role": "user", "content": "hello"},
        ],
    },
    provider="deepseek",
    counts={"key_deepseek": 1},
)
add(
    "walk: keys sort by utf-16 code units",
    {"b": "marie@example.com", "10": "paul@example.com", "9": "ops@example.com"},
    {"b": "[EMAIL_3]", "10": "[EMAIL_1]", "9": "[EMAIL_2]"},
    counts={"email": 3},
    note='Integer-like keys are not walked first: "10" < "9" < "b".',
)
add("walk: object keys are never scanned", {"marie@example.com": "x"})
add(
    "walk: non-string scalars untouched",
    {"n": 4111111111111111, "f": 0.5, "t": True, "z": None, "l": [1, 2]},
)
add(
    "walk: data uri as plain text value",
    "data:text/plain;base64,bWFyaWVAZXhhbXBsZS5jb20=",
    attachments=1,
    note="A data: URI is an attachment wherever it appears; its base64 content is not decoded.",
)
add(
    "walk: prose starting with data",
    "data: marie@example.com",
    "data: [EMAIL_1]",
    counts={"email": 1},
)
add(
    "walk: base64 of an email (not caught)",
    "bWFyaWVAZXhhbXBsZS5jb20=",
    note="Encoded text is not decoded.",
)
DEEP: Any = "deep"
for _ in range(130):
    DEEP = [DEEP]
add(
    "walk: depth limit",
    {"a": DEEP},
    blocked={"size_limit": 1},
    note="Nesting deeper than limits.max_depth is refused, never skipped.",
)
add(
    "walk: deep but within the limit",
    {"a": [[[[[[[[[["marie@example.com"]]]]]]]]]]},
    {"a": [[[[[[[[[["[EMAIL_1]"]]]]]]]]]]},
    counts={"email": 1},
)

# ── what two languages could read differently ───────────────────────────
add(
    "parity: ligature inside an address",
    "x \ufb01@example.com",
    "x [EMAIL_1]",
    counts={"email": 1},
    note="One original character, two shadow characters.",
)
add(
    "parity: astral keys sort by utf-16 code units",
    {"\uffff": "a@example.com", "\U0001f600": "b@example.com"},
    {"\uffff": "[EMAIL_2]", "\U0001f600": "[EMAIL_1]"},
    counts={"email": 2},
    note="U+1F600 is D83D DE00 in UTF-16, which sorts before U+FFFF.",
)
add(
    "parity: long s in a secret name",
    "pa\u017f\u017fword=hunter2hunter",
    "pa\u017f\u017fword=[SECRET_1]",
    counts={"generic_secret": 1},
    note="NFKD writes the long s as s before any pattern runs.",
)
add(
    "parity: kelvin sign in a key id",
    "id A\u212a" + KEY_AWS[2:],
    "id [API_KEY_1]",
    counts={"key_aws": 1},
)
add(
    "parity: custom value of 200 astral characters",
    "x",
    policy={
        "schema": 1,
        "custom": [
            {
                "kind": "term",
                "value": "\U0001f600" * 200,
                "action": "mask",
                "label": "E",
            }
        ],
    },
)
add(
    "parity: custom value of 201 astral characters",
    "x",
    policy={
        "schema": 1,
        "custom": [
            {
                "kind": "term",
                "value": "\U0001f600" * 201,
                "action": "mask",
                "label": "E",
            }
        ],
    },
    error="policy",
    note="Limits count code points, never UTF-16 units.",
)
add(
    "parity: astral characters count once toward the size limit",
    {"$parts": [{"repeat": "\U0001f600", "times": 4194305}, " marie@example.com"]},
    {"$parts": [{"repeat": "\U0001f600", "times": 4194305}, " [EMAIL_1]"]},
    counts={"email": 1},
    note="8 388 610 UTF-16 units, 4 194 306 code points: under the limit.",
)

# ── data URIs ──────────────────────────────────────────────────────────────
add(
    "walk: data uri that is not base64 is text",
    "data:a/b,my card 4111 1111 1111 1111",
    "data:a/b,my card [CARD_1]",
    counts={"credit_card": 1},
)
add(
    "walk: text after a base64 header is text",
    "data:image/png;base64,iVBOR then marie@example.com",
    "data:image/png;base64,iVBOR then [EMAIL_1]",
    counts={"email": 1},
)
add(
    "walk: base64 data uri with line breaks",
    "data:image/png;base64,iVBOR\nw0KGgo=",
    attachments=1,
)

# ── free-form data inside a structured body ────────────────────────────────
add(
    "free-form: tool input keys are text",
    {
        "model": "claude-sonnet-4-5",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_4111111111111111",
                        "name": "crm",
                        "input": {
                            "name": "marie@example.com",
                            "id": "4111111111111111",
                            "type": "note",
                            "source": {"type": "url", "url": "https://acme.example/x"},
                        },
                    }
                ],
            }
        ],
    },
    {
        "model": "claude-sonnet-4-5",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_4111111111111111",
                        "name": "crm",
                        "input": {
                            "name": "[EMAIL_1]",
                            "id": "[CARD_1]",
                            "type": "note",
                            "source": {"type": "url", "url": "https://acme.example/x"},
                        },
                    }
                ],
            }
        ],
    },
    provider="anthropic",
    counts={"email": 1, "credit_card": 1},
    note="Inside `input` a `source` object is data, not an attachment.",
)
add(
    "free-form: function call args",
    {
        "model": "gemini-2.5-flash",
        "contents": [
            {
                "role": "model",
                "parts": [
                    {
                        "function_call": {
                            "name": "notify",
                            "args": {
                                "name": "marie@example.com",
                                "mime_type": "06 12 34 56 78",
                            },
                        }
                    }
                ],
            }
        ],
    },
    {
        "model": "gemini-2.5-flash",
        "contents": [
            {
                "role": "model",
                "parts": [
                    {
                        "function_call": {
                            "name": "notify",
                            "args": {"name": "[EMAIL_1]", "mime_type": "[PHONE_1]"},
                        }
                    }
                ],
            }
        ],
    },
    provider="gemini",
    counts={"email": 1, "phone": 1},
)
add(
    "free-form: openai metadata",
    {"model": "gpt-4o", "messages": [], "metadata": {"id": "marie@example.com"}},
    {"model": "gpt-4o", "messages": [], "metadata": {"id": "[EMAIL_1]"}},
    provider="openai",
    counts={"email": 1},
)
add(
    "attachment objects only under their key",
    {
        "model": "claude-sonnet-4-5",
        "messages": [
            {"role": "user", "content": [{"type": "url", "text": "marie@example.com"}]}
        ],
    },
    {
        "model": "claude-sonnet-4-5",
        "messages": [
            {"role": "user", "content": [{"type": "url", "text": "[EMAIL_1]"}]}
        ],
    },
    provider="anthropic",
    counts={"email": 1},
)

# ── forms found by the end-to-end review (17/11) ──────────────────────────
# A filter must never count a value as masked while part of it left: a key
# whose lines are escaped or quoted is masked whole, BEGIN to END. Then the
# forms a deterministic rule can take without new false positives; what it
# cannot take is stated here, with a note, and in docs/prompt-cleaning.md.

PEM_ESCAPED = PEM.replace("\n", "\\n")
add(
    "private_key: escaped newlines (service-account JSON)",
    '"private_key": "' + PEM_ESCAPED + '\\n",\n"client_id": "1234"',
    '"private_key": "[PRIVATE_KEY_1]\\n",\n"client_id": "1234"',
    counts={"private_key": 1},
    note="Newlines written \\n inside a JSON string, as in a cloud service-account file.",
)
add(
    "private_key: escaped crlf",
    "KEY=" + PEM.replace("\n", "\\r\\n") + "\\r\\n",
    "KEY=[PRIVATE_KEY_1]\\r\\n",
    counts={"private_key": 1},
)
add(
    "private_key: escaped, no end line",
    '"private_key": "-----BEGIN '
    + "PRIVATE KEY-----\\n"
    + PEM_BODY.replace("\n", "\\n")
    + '\\n"',
    '"private_key": "[PRIVATE_KEY_1]',
    counts={"private_key": 1},
)
add(
    "private_key: quoted in an e-mail",
    "Forwarded:\n> " + PEM.replace("\n", "\n> ") + "\n> Thanks",
    "Forwarded:\n> [PRIVATE_KEY_1]\n> Thanks",
    counts={"private_key": 1},
)
add(
    "private_key: indented yaml block",
    "tls:\n  key: |\n    " + PEM.replace("\n", "\n    ") + "\n  cert: none",
    "tls:\n  key: |\n    [PRIVATE_KEY_1]\n  cert: none",
    counts={"private_key": 1},
)
add(
    "private_key: added lines of a diff",
    "+" + PEM.replace("\n", "\n+") + "\n context",
    "+[PRIVATE_KEY_1]\n context",
    counts={"private_key": 1},
)
add(
    "private_key: pipe-quoted",
    "| " + PEM.replace("\n", "\n| ") + "\n| end",
    "| [PRIVATE_KEY_1]\n| end",
    counts={"private_key": 1},
)
add(
    "private_key: on one line, spaces between the parts",
    "key " + PEM.replace("\n", " ") + " ok",
    "key [PRIVATE_KEY_1] ok",
    counts={"private_key": 1},
)
add(
    "private_key: no end line, prose after",
    "-----BEGIN " + "PRIVATE KEY-----\n" + PEM_BODY + "\nCan you check it?",
    "[PRIVATE_KEY_1]",
    counts={"private_key": 1},
    note="Without its END line, a BEGIN line followed by key material is masked with everything after it, to the end of the text: too much, never half.",
)
add(
    "private_key: pgp armor with a header and a blank line",
    "-----BEGIN PGP "
    + "PRIVATE KEY BLOCK-----\nVersion: GnuPG v2\n\n"
    + PEM_BODY
    + "\n=AbCd\n-----END PGP "
    + "PRIVATE KEY BLOCK-----\nok",
    "[PRIVATE_KEY_1]\nok",
    counts={"private_key": 1},
)

for label, before, after in (
    ("full stop", "The token is ", "."),
    ("ellipsis", "The token is ", "..."),
    ("unicode ellipsis", "The token is ", "\u2026"),
    ("comma", "The token is ", ", then"),
    ("closing quote and stop", 'The token is "', '".'),
    ("closing parenthesis", "The token is (", ")."),
):
    masked(f"jwt: before {label}", before, JWT, after, "TOKEN", "jwt")

for word in ("version", "Version:", "ver.", "release", "firmware", "révision"):
    kept(
        f"ip_address: after {word}",
        f"{word} 4.2.1.3 shipped",
        note="Four numbers after a version word are a version, not an address.",
    )
kept("ip_address: a fifth part", "build 3.2.1.15.2 done")
masked(
    "ip_address: after another word",
    "server ",
    "4.2.1.3",
    " down",
    "IP_ADDRESS",
    "ip_address",
)
masked(
    "ip_address: a word that contains version",
    "subversion ",
    "10.1.2.3",
    " mirror",
    "IP_ADDRESS",
    "ip_address",
)
masked(
    "ip_address: version word further back",
    "version 2 runs on ",
    "10.0.0.1",
    "",
    "IP_ADDRESS",
    "ip_address",
)
add(
    "ip_address: four numbers without context are taken",
    "Upgrade to 1.2.3.4 tonight",
    "Upgrade to [IP_ADDRESS_1] tonight",
    counts={"ip_address": 1},
    note="Without a version word just before them, four numbers of at most 255 are read as an address.",
)
kept("ip_address: python slice", "items[::2]")
kept("ip_address: python slice with a start", "items[1::2]")
masked(
    "ip_address: ipv6 in brackets with a port",
    "http://[",
    "2001:db8::1",
    "]:8080/",
    "IP_ADDRESS",
    "ip_address",
)

for label, value in (
    ("parenthesised +33", "(+33) 6 12 34 56 78"),
    ("slashes", "06/12/34/56/78"),
    ("double spaces", "06  12  34  56  78"),
    ("+33 double spaces", "+33  6  12  34  56  78"),
):
    masked(f"phone: {label}", "Call ", value, " now", "PHONE", "phone")
kept(
    "phone: an amount with a plus sign",
    "Revenue up +12 500 000 this year",
    note="A number written with + needs at least 9 digits, country code included: shorter ones are amounts.",
)
kept("phone: a date with slashes", "born 06/12/34 in Lyon")
add(
    "phone: an order number of ten digits starting with 0 is taken",
    "Order 0612202601 shipped",
    "Order [PHONE_1] shipped",
    counts={"phone": 1},
    note="Ten digits starting 0[1-9] have the form of a French number: they are taken.",
)

for label, value in (
    ("dashes", "FR76-3000-6000-0112-3456-7890-189"),
    ("country apart", "FR 76 3000 6000 0112 3456 7890 189"),
    ("double spaces", "FR76  3000  6000  0112  3456  7890  189"),
):
    masked(f"iban: {label}", "IBAN ", value, " thanks", "IBAN", "iban")
kept("iban: dashes, bad check digits", "IBAN FR76-3000-6000-0112-3456-7890-188 thanks")
kept(
    "iban: rib grouping (not caught)",
    "RIB 30006 00001 12345678901 89",
    note="A French RIB written as bank, branch, account and key is not an IBAN.",
)

masked(
    "credit_card: double spaces",
    "card ",
    "4111  1111  1111  1111",
    " ok",
    "CARD",
    "credit_card",
)
masked(
    "credit_card: amex double spaces",
    "card ",
    "3782  822463  10005",
    " ok",
    "CARD",
    "credit_card",
)
kept(
    "credit_card: groups of 4-8-4 (not caught)",
    "card 4111 11111111 1111 ok",
    note="Only 4-4-4-4, 4-4-4-4-3 and 4-6-5 groupings, or no grouping, are read.",
)

masked(
    "fr_nir: dashes",
    "NIR ",
    spaced(NIR, [1, 2, 2, 2, 3, 3, 2], "-"),
    "",
    "NIR",
    "fr_nir",
)
kept("fr_nir: dashes, wrong key", "NIR " + spaced(NIR_BAD, [1, 2, 2, 2, 3, 3, 2], "-"))
masked(
    "fr_siren: dashes", "SIREN ", spaced(SIREN, [3, 3, 3], "-"), "", "SIREN", "fr_siren"
)
masked(
    "fr_siren: dotted keyword",
    "R.C.S. Paris ",
    spaced(SIREN, [3, 3, 3]),
    "",
    "SIREN",
    "fr_siren",
)
masked("fr_siret: dotted keyword", "S.I.R.E.T. : ", SIRET, "", "SIRET", "fr_siret")
masked(
    "fr_siret: grouped 3-3-3-3-2",
    "SIRET ",
    spaced(SIRET, [3, 3, 3, 3, 2]),
    "",
    "SIRET",
    "fr_siret",
)
masked(
    "fr_siret: dashes",
    "SIRET ",
    spaced(SIRET, [3, 3, 3, 5], "-"),
    "",
    "SIRET",
    "fr_siret",
)
kept("fr_siret: dashes without keyword", "Ref " + spaced(SIRET, [3, 3, 3, 5], "-"))
masked("bic: dotted keyword", "B.I.C. : ", "EXMPFRPPXXX", "", "BIC", "bic")

masked("fr_plate: dots", "plaque ", "AB.123.CD", " garée", "PLATE", "fr_plate")
masked(
    "fr_plate: with its department",
    "plaque ",
    "AB-123-CD-69",
    " garée",
    "PLATE",
    "fr_plate",
)
masked(
    "fr_plate: with a corsican department",
    "plaque ",
    "AB-123-CD-2A",
    " garée",
    "PLATE",
    "fr_plate",
)
kept("fr_plate: 000 with a department", "plaque AB-000-CD-69")
add(
    "fr_plate: a number after a space is not a department",
    "plaque AB-123-CD 75 km",
    "plaque [PLATE_1] 75 km",
    counts={"fr_plate": 1},
)

masked(
    "key_slack: webhook without a scheme",
    "url ",
    SLACK_HOOK[len("https://") :],
    " end",
    "API_KEY",
    "key_slack",
)
masked(
    "key_slack: webhook over http",
    "url ",
    "http://" + SLACK_HOOK[len("https://") :],
    " end",
    "API_KEY",
    "key_slack",
)
masked(
    "key_gitlab: a suffix attached by a dot is masked with the token",
    "token ",
    KEY_GITLAB + ".01.0w1z2y3x4",
    " end",
    "API_KEY",
    "key_gitlab",
    note="Whatever is attached to the token by a dot goes with it: no part of it is left behind.",
)

masked(
    "bearer_token: proxy authorization",
    "Proxy-Authorization: Basic ",
    "dXNlcjpwYXNzd29yZDEyMw==",
    "",
    "TOKEN",
    "bearer_token",
)
masked(
    "bearer_token: basic in json",
    '{"Authorization": "Basic ',
    "dXNlcjpwYXNzd29yZDEyMw==",
    '"}',
    "TOKEN",
    "bearer_token",
)
masked(
    "bearer_token: basic in a js object",
    "headers: { Authorization: 'Basic ",
    "dXNlcjpwYXNz",
    "' }",
    "TOKEN",
    "bearer_token",
)
kept(
    "bearer_token: an english word after bearer",
    "the bearer responsibilities apply",
    note="After Bearer, a value made only of letters is a word, not a token: a token has a digit or a punctuation mark.",
)

masked(
    "connection_string_password: PGPASSWORD",
    "PGPASSWORD=",
    "S3cretPass",
    " psql -h db",
    "PASSWORD",
    "connection_string_password",
)
masked(
    "connection_string_password: POSTGRES_PASSWORD",
    "POSTGRES_PASSWORD=",
    "S3cretPass",
    "",
    "PASSWORD",
    "connection_string_password",
)
masked(
    "connection_string_password: POSTGRES_PASSWORD quoted with spaces",
    'POSTGRES_PASSWORD="',
    "my S3cret pass",
    '"',
    "PASSWORD",
    "connection_string_password",
)
masked(
    "connection_string_password: spring",
    "spring.datasource.password=",
    "S3cretPass",
    "",
    "PASSWORD",
    "connection_string_password",
)
masked(
    "connection_string_password: oracle thin",
    "jdbc:oracle:thin:",
    "scott/T1ger",
    "@//db.internal:1521/ORCL",
    "PASSWORD",
    "connection_string_password",
)
masked(
    "connection_string_password: quoted value",
    "Server=db;Database=app;User Id=sa;Password='",
    "S3cret pass",
    "';",
    "PASSWORD",
    "connection_string_password",
)
masked(
    "connection_string_password: double-quoted value",
    'Server=db;Database=app;User Id=sa;Password="',
    "S3cret pass",
    '";',
    "PASSWORD",
    "connection_string_password",
)
kept(
    "connection_string_password: a reference to the environment",
    "connect(host='db', database='app', password=os.environ['DB_PASSWORD'])",
    note="A value read from the environment in code is a name, not a password.",
)
masked(
    "key_azure: account key with spaces",
    "AccountKey = ",
    AZURE_ACCOUNT_KEY,
    ";",
    "API_KEY",
    "key_azure",
)
masked(
    "key_mistral: MistralClient",
    'MistralClient(api_key="',
    KEY_MISTRAL,
    '")',
    "API_KEY",
    "key_mistral",
)
masked(
    "key_mistral: CODESTRAL",
    "CODESTRAL_API_KEY=",
    KEY_MISTRAL,
    "",
    "API_KEY",
    "key_mistral",
)
masked(
    "key_mistral: MISTRALAI",
    "MISTRALAI_API_KEY=",
    KEY_MISTRAL,
    "",
    "API_KEY",
    "key_mistral",
)

masked(
    "generic_secret: french name",
    "Mot de passe : ",
    "Tr0ub4dor&3",
    "",
    "SECRET",
    "generic_secret",
)
masked("generic_secret: mdp", "mdp=", "Tr0ub4dor&3", "", "SECRET", "generic_secret")
masked(
    "generic_secret: pass in an ini file",
    "pass=",
    "Tr0ub4dor&3",
    "",
    "SECRET",
    "generic_secret",
)
masked(
    "generic_secret: a name ending in _KEY",
    "JWT_SIGNING_KEY=",
    "Tr0ub4dor&3xyz",
    "",
    "SECRET",
    "generic_secret",
)
masked(
    "generic_secret: quoted value with spaces is masked whole",
    'password: "',
    "correct horse battery staple",
    '"',
    "SECRET",
    "generic_secret",
)
masked(
    "generic_secret: single-quoted value with spaces",
    "secret = '",
    "correct horse battery staple",
    "'",
    "SECRET",
    "generic_secret",
)
for label, text in (
    ("a type name", "password: string;"),
    ("a fetch option", 'credentials: "include"'),
    ("an environment lookup", "api_key = os.getenv('API_KEY')"),
    ("a process.env lookup", "token: process.env.API_TOKEN,"),
    ("a template variable", "password: ${DB_PASSWORD}"),
    ("a lowercase key name", "primary_key: user_id"),
):
    kept(f"generic_secret: {label}", text)

kept("email: a retina image name", "Replace logo@2x.png by icon@3x.webp")
masked(
    "email: punycode domain", "Mail: ", "marie@exemple.xn--p1ai", "", "EMAIL", "email"
)
masked("email: cyrillic address", "Почта: ", "мария@пример.рф", "", "EMAIL", "email")
masked("email: greek address", "Mail: ", "μαρια@παραδειγμα.ελ", "", "EMAIL", "email")
kept(
    "email: other scripts (not caught)",
    "用户@例子.中国",
    note="Latin, Greek and Cyrillic addresses are read; addresses in other scripts are not.",
)
kept(
    "email: an ssh remote",
    "git clone git@github.com:acme/app.git",
    note="user@host followed by a colon and a path is a remote, not an address.",
)
masked(
    "email: a colon and a space after an address",
    "Write to ",
    "marie@example.com",
    ": she answers.",
    "EMAIL",
    "email",
)
kept("email: an image name in a path", 'src="img/icon@3x.webp"')
kept(
    "email: a cyrillic local part longer than 64 letters is never cut",
    "Почта: " + "а" * 70 + "@пример.рф",
    note="A local part has at most 64 characters: a longer run of letters is not an address, and no part of it is taken.",
)
add(
    "email: cyrillic letters glued before an address belong to it",
    "Письмо: письмомария@пример.рф",
    "Письмо: [EMAIL_1]",
    counts={"email": 1},
)

# Forms of a private key whose END line is present: everything from BEGIN to
# END is masked, whatever stands between the lines.
for label, before, joined, after in (
    ("javascript concatenation", "const key = '", "\\n' +\n  '", "\\n';"),
    ("html line breaks", "<p>", "<br>", "</p>"),
    ("double-escaped newlines", '"key": "', "\\\\n", '"'),
    ("a python list of lines", "lines = ['", "', '", "']"),
):
    add(
        f"private_key: {label}",
        before + PEM.replace("\n", joined) + after,
        before + "[PRIVATE_KEY_1]" + after,
        counts={"private_key": 1},
    )
add(
    "private_key: two keys, the first without its END line",
    "-----BEGIN " + "PRIVATE KEY-----\n" + PEM_BODY + "\n\n" + OPENSSH + "\nok",
    "[PRIVATE_KEY_1]",
    counts={"private_key": 1},
    note="The first key has no END line of its own: from its BEGIN line to the end of the text, the second key included.",
)
add(
    "private_key: one line, no end line",
    "key -----BEGIN " + "PRIVATE KEY----- " + PEM_BODY.replace("\n", " ") + " ok?",
    "key [PRIVATE_KEY_1]",
    counts={"private_key": 1},
)
# Without its END line, the parts after the BEGIN line are taken however they
# are joined, down to a short last part closed by markup or a quote.
PEM_BODY_UNPADDED = "\n".join(PEM_BODY.split("\n")[:4]) + "\nAbCdEfGh"
for label, before, joined, after in (
    ("html line breaks, no end line", "<p>", "<br>", "</p>"),
    ("a python list, no end line", "lines = ['", "', '", "']"),
    ("javascript concatenation, no end line", "const key = '", "\\n' +\n  '", "\\n';"),
):
    add(
        f"private_key: {label}",
        before
        + ("-----BEGIN " + "PRIVATE KEY-----\n" + PEM_BODY_UNPADDED).replace(
            "\n", joined
        )
        + after,
        before + "[PRIVATE_KEY_1]",
        counts={"private_key": 1},
    )
add(
    "private_key: quoted in an e-mail, no end line, a short last line",
    "> "
    + ("-----BEGIN " + "PRIVATE KEY-----\n" + PEM_BODY_UNPADDED).replace("\n", "\n> ")
    + "\n> Thanks, Marie",
    "> [PRIVATE_KEY_1]",
    counts={"private_key": 1},
)
add(
    "private_key: body glued to the BEGIN line, no end line",
    "-----BEGIN " + "PRIVATE KEY-----" + PEM_BODY + "\nok?",
    "[PRIVATE_KEY_1]",
    counts={"private_key": 1},
)
add(
    "private_key: no end line, the words after it go with it",
    "-----BEGIN " + "PRIVATE KEY-----\n" + PEM_BODY + "\n\nThanks, Marie",
    "[PRIVATE_KEY_1]",
    counts={"private_key": 1},
)
add(
    "private_key: prose between a BEGIN line and an END line is masked",
    "A key starts with -----BEGIN "
    + "PRIVATE KEY----- and ends with -----END "
    + "PRIVATE KEY-----.",
    "A key starts with [PRIVATE_KEY_1].",
    counts={"private_key": 1},
    note="Everything between a BEGIN line and the END line that follows it is masked, even prose.",
)

RNG_QA = random.Random(20261118)
KEY_SLACK_APP = (
    "xapp-1-"
    + "".join(RNG_QA.choice(string.ascii_uppercase + string.digits) for _ in range(11))
    + "-"
    + "".join(RNG_QA.choice(string.digits) for _ in range(13))
    + "-"
    + "".join(RNG_QA.choice(HEX) for _ in range(64))
)
KEY_GITLAB_FEATURE_FLAGS = "glffct-" + "".join(
    RNG_QA.choice(ALNUM + "_-") for _ in range(20)
)
masked(
    "key_slack: app-level token",
    "token ",
    KEY_SLACK_APP,
    " end",
    "API_KEY",
    "key_slack",
)
masked(
    "key_slack: rotating app-level token",
    "token ",
    "xoxe." + KEY_SLACK_APP,
    " end",
    "API_KEY",
    "key_slack",
)
masked(
    "key_gitlab: feature flags client token",
    "token ",
    KEY_GITLAB_FEATURE_FLAGS,
    " end",
    "API_KEY",
    "key_gitlab",
)
kept(
    "key_gitlab: runner registration token (not caught)",
    "token GR1348941" + "".join(RNG_QA.choice(ALNUM) for _ in range(20)) + " end",
    note="Legacy runner registration tokens have no documented prefix: they are not detected.",
)
kept(
    "phone: an amount in euros",
    "Revenue up +125 000 000 € this year",
    note="A number followed by a currency is an amount, not a phone number.",
)
kept("phone: an amount in dollars", "Budget +125 000 000 USD")
masked(
    "phone: a number followed by a word",
    "Call ",
    "+33 6 12 34 56 78",
    " today, for 5 euros",
    "PHONE",
    "phone",
    note="Only a currency sign or code right after the number rules it out, not a word further on.",
)
masked(
    "bearer_token: letters only after an Authorization header",
    "Authorization: Bearer ",
    "abcdefghijklmnopqrstuvwxyzABCDEF",
    "",
    "TOKEN",
    "bearer_token",
)
kept("ip_address: glued to v", "upgrade to v4.2.1.3 now")
kept("ip_address: after Version in capitals", "VERSION 4.2.1.3")
masked(
    "ip_address: an address after a version word and a sentence",
    "version 2. Server ",
    "10.0.0.1",
    "",
    "IP_ADDRESS",
    "ip_address",
)

# ── second pass after the end-to-end review (18/11) ───────────────────────
# A value is taken whole or not at all: no exclusion, no bound and no
# punctuation next to it ever shortens a value that is then counted. Each case
# below was half-masked and counted, missed, or taken wrongly before.


def only(detector: str) -> dict[str, Any]:
    """A policy enabling one detector, as the property test in both suites does."""
    return {
        "schema": 1,
        "app_id": "app_medhelp",
        "revision": 0,
        "detectors": {detector: "mask"},
        "attachments": "allow",
        "custom": [],
    }


def blocking(detector: str) -> dict[str, Any]:
    """A policy blocking on one detector alone."""
    return {**only(detector), "detectors": dict.fromkeys([detector], "block")}


def _chunks(text: str) -> list[str]:
    return [text[index : index + 8] for index in range(0, len(text), 8)]


# A private key without its END line: with key material after its BEGIN line,
# everything from there to the end of the text value; the header alone, quoted
# in prose, is all there is to mask.
PEM_HEAD = "-----BEGIN RSA " + "PRIVATE KEY-----"
PEM_CUT = PEM_HEAD + "\n" + PEM_BODY
ENCRYPTED_CUT = (
    PEM_HEAD
    + "\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0A1B2C3D4E5F\n\n"
    + PEM_BODY
)
PGP_CUT = (
    "-----BEGIN PGP "
    + "PRIVATE KEY BLOCK-----\nVersion: GnuPG v2\n\n"
    + PEM_BODY
    + "\n=AbCd"
)
for label, text in (
    ("newlines escaped twice", '{"k": "' + PEM_CUT.replace("\n", "\\\\n") + '"}'),
    (
        "indented by 20",
        "key: |\n" + "\n".join(" " * 20 + line for line in PEM_CUT.split("\n")),
    ),
    ("indented by 17", "\n".join(" " * 17 + line for line in PEM_CUT.split("\n"))),
    ("removed lines of a diff", "\n".join("-" + line for line in PEM_CUT.split("\n"))),
    ("a // comment", "\n".join("// " + line for line in PEM_CUT.split("\n"))),
    ("a # comment", "\n".join("# " + line for line in PEM_CUT.split("\n"))),
    ("encrypted, in JSON", '{"k": "' + ENCRYPTED_CUT.replace("\n", "\\n") + '"}'),
    ("encrypted, in javascript", "const k = `" + ENCRYPTED_CUT + "`;\nrun(k);"),
    ("encrypted, in a list", "['" + ENCRYPTED_CUT.replace("\n", "', '") + "']"),
    ("pgp with its checksum line", PGP_CUT + "\nThanks"),
):
    start = text.index("-----BEGIN")
    add(
        f"private_key: no end line, {label}",
        text,
        text[:start] + "[PRIVATE_KEY_1]",
        counts={"private_key": 1},
        policy=only("private_key"),
        note="Without its END line, a BEGIN line followed by key material is masked with everything after it, to the end of the text.",
    )
kept(
    "private_key: a quoted header alone",
    "A key file starts with -----BEGIN "
    + "PRIVATE KEY----- and ends with a dashed line.",
    policy=only("private_key"),
    note="No key material right after the header: it is quoted, not a key. Under Mask it is left as it is and not counted; under Block it blocks the call.",
)
add(
    "private_key: a quoted header alone, blocked",
    "A key file starts with -----BEGIN "
    + "PRIVATE KEY----- and ends with a dashed line.",
    blocked={"private_key": 1},
    policy=blocking("private_key"),
)
add(
    "private_key: no end line, then 100 kB of prose",
    {
        "$parts": _chunks(PEM_CUT)
        + [{"repeat": "lorem ipsum dolor sit amet ", "times": 4000}]
    },
    "[PRIVATE_KEY_1]",
    counts={"private_key": 1},
    policy=only("private_key"),
)
for _name, _words in (("far", " lorem ipsum" * 100), ("near", " lorem ipsum")):
    kept(
        f"private_key: key material after prose is not looked for, {_name}",
        "-----BEGIN " + "PRIVATE KEY-----" + _words + "\n" + PEM_BODY,
        policy=only("private_key"),
        note="Key material is read right after the header only: after a word of prose, the header is quoted and what follows is not read as a key.",
    )

# A phone number: an exclusion never shortens one; a French number in +33 is a
# number whatever follows it; a currency or a code is a whole word.
for label, value, after in (
    ("+33 followed by a euro sign", "+33 6 12 34 56 78", " €"),
    ("+33 glued to a shell prompt", "+33612345678", "$"),
    (
        "followed by a word starting like a currency",
        "+44 20 7946 0958",
        " European desk",
    ),
    ("followed by a word starting like a code", "+1 202 555 0143", " USDA office"),
):
    masked(
        f"phone: {label}", "Call ", value, after, "PHONE", "phone", policy=only("phone")
    )
for label, before, value, after in (
    (
        "an international number glued to a word is taken with it",
        "Call ",
        "+49-151-1234-5678abc",
        "",
    ),
    ("an international number before a currency", "Pay ", "+49 151 1234 5678", " €"),
    ("an amount of 12 digits before a euro sign", "Revenue ", "+123 456 789 012", " €"),
    ("a number that goes on is taken with it", "Call ", "+33 6 12 34 56 7890123", ""),
):
    masked(
        f"phone: {label}", before, value, after, "PHONE", "phone", policy=only("phone")
    )
for label, text in (
    ("an amount in k€", "Budget +125 000 000 k€"),
    ("an amount in round thousands", "Visitors +150 000 000 this year"),
):
    kept(f"phone: {label}", text, policy=only("phone"))
kept(
    "phone: a number of fewer than 9 digits (not caught)",
    "Stanley +500 12345",
    policy=only("phone"),
    note="An international number needs 9 digits or more: shorter numbers of small countries are not caught.",
)

# An e-mail address: followed by a port it is an address (a password glued
# after its colon is taken with it, below); a git remote is one only when the
# command, the scheme or .git says so.
for label, after in (
    ("followed by a port", ":993 refused"),
    ("followed by a colon and a dot", ":."),
    ("followed by a digit after its domain", ""),
):
    masked(
        f"email: {label}",
        "Account ",
        "marie.dupont@acme.example",
        after,
        "EMAIL",
        "email",
        policy=only("email"),
    )
masked(
    "email: a top-level domain run on is taken with it",
    "Write to ",
    "marie@mail.example.orgxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "",
    "EMAIL",
    "email",
    policy=only("email"),
)
kept(
    "email: a remote after git clone",
    "git clone deploy@git.acme.example:apps/site",
    policy=only("email"),
)
kept(
    "email: a remote after git remote add",
    "git remote add origin git@acme.example:team/app",
    policy=only("email"),
)
masked(
    "email: a remote after scp and an argument is taken",
    "scp build.tgz ",
    "ops@host.acme.example",
    ":/srv/releases",
    "EMAIL",
    "email",
    policy=only("email"),
    note="Only right after git clone, git remote add or set-url, scp or rsync is user@host:path a remote; in doubt, the address is masked.",
)
masked(
    "email: a path ending with .git, no command before it",
    "Push to ",
    "git@github.com",
    ":acme/app.git now",
    "EMAIL",
    "email",
    policy=only("email"),
)
masked(
    "email: git as a mailbox",
    "Write to ",
    "git@acme.example",
    " for access.",
    "EMAIL",
    "email",
    policy=only("email"),
)

# An IP address: only the unambiguous version words make four numbers a version.
for word in ("build", "fw", "rev", "ssh -v", "curl -v", "FW:"):
    masked(
        f"ip_address: after {word}",
        f"{word} ",
        "10.0.0.5",
        " down",
        "IP_ADDRESS",
        "ip_address",
        policy=only("ip_address"),
    )
kept(
    "ip_address: release joined by a dash",
    "release-1.2.3.4 shipped",
    policy=only("ip_address"),
)
kept("ip_address: firmware", "firmware 4.2.1.3 shipped", policy=only("ip_address"))
kept("ip_address: revision", "revision 4.2.1.3 shipped", policy=only("ip_address"))
masked(
    "ip_address: a prefix run on is taken with it",
    "net ",
    "10.0.0.0/244",
    " here",
    "IP_ADDRESS",
    "ip_address",
    policy=only("ip_address"),
)
masked(
    "ip_address: ipv6 followed by a colon",
    "host ",
    "::ffff:192.0.2.128",
    ": up",
    "IP_ADDRESS",
    "ip_address",
    policy=only("ip_address"),
)
masked(
    "ip_address: ipv6 with an ipv4 tail before a port",
    "host ",
    "::ffff:192.0.2.128",
    ":8080",
    "IP_ADDRESS",
    "ip_address",
    policy=only("ip_address"),
)
kept(
    "ip_address: an ipv6 address without a digit (not caught)",
    "host dead:beef::cafe up",
    policy=only("ip_address"),
    note="An IPv6 address written without a single digit reads like prose or code: it is not caught.",
)
kept(
    "ip_address: a mac address in the macOS form is no address",
    "mac 0:1a:2b:3c:4d:5e",
    policy=only("ip_address"),
)

# MAC addresses at the end of a sentence; a Diners card grouped 4-6-4; an IBAN
# grouped by four from its country; a BIC written spaced; Slack, Azure, Bearer.
masked(
    "mac_address: dotted form before a full stop",
    "mac ",
    "001a.2b3c.4d5e",
    ".",
    "MAC_ADDRESS",
    "mac_address",
    policy=only("mac_address"),
)
masked(
    "mac_address: dotted form before an ellipsis",
    "mac ",
    "001a.2b3c.4d5e",
    "...",
    "MAC_ADDRESS",
    "mac_address",
    policy=only("mac_address"),
)
masked(
    "mac_address: colons before a colon",
    "mac ",
    "00:1a:2b:3c:4d:5e",
    ": up",
    "MAC_ADDRESS",
    "mac_address",
    policy=only("mac_address"),
)
masked(
    "credit_card: diners 4-6-4",
    "card ",
    "3056 930902 5904",
    " ok",
    "CARD",
    "credit_card",
    policy=only("credit_card"),
)
masked(
    "iban: grouped by four after the country",
    "IBAN ",
    "FR 7630 0060 0001 1234 5678 9018 9",
    " thanks",
    "IBAN",
    "iban",
    policy=only("iban"),
)
kept(
    "iban: the rib grouping after FR76 (not caught)",
    "IBAN FR76 30006 00001 12345678901 89",
    policy=only("iban"),
    note="Bank, branch, account and key grouped as a French RIB are not read as an IBAN.",
)
masked(
    "bic: written spaced",
    "BIC ",
    "EXMP FR PP XXX",
    "",
    "BIC",
    "bic",
    policy=only("bic"),
)
masked(
    "key_slack: client token",
    "token ",
    "xoxc-" + "".join(RNG_QA.choice(ALNUM) for _ in range(40)),
    " end",
    "API_KEY",
    "key_slack",
    policy=only("key_slack"),
)
masked(
    "key_slack: cookie token",
    "token ",
    "xoxd-" + "".join(RNG_QA.choice(ALNUM) for _ in range(40)),
    " end",
    "API_KEY",
    "key_slack",
    policy=only("key_slack"),
)
masked(
    "key_slack: a dash before a rotating token",
    "-",
    "xoxe." + KEY_SLACK_APP,
    " end",
    "API_KEY",
    "key_slack",
    policy=only("key_slack"),
)
masked(
    "key_azure: hexadecimal in capitals",
    "Azure key ",
    AZURE_HEX.upper(),
    "",
    "API_KEY",
    "key_azure",
    policy=only("key_azure"),
)
masked(
    "bearer_token: a short token in an Authorization header",
    "Authorization: Bearer ",
    "short" + "tok3n",
    "",
    "TOKEN",
    "bearer_token",
    policy=only("bearer_token"),
)

masked(
    "key_azure: an account key run on is taken with it",
    "AccountKey=",
    AZURE_ACCOUNT_KEY + "AbCd",
    ";",
    "API_KEY",
    "key_azure",
    policy=only("key_azure"),
)
add(
    "connection_string_password: a password past every bound is taken to its last @",
    {"$parts": ["redis://:p@", {"repeat": "a", "times": 70000}, "@cache:6379"]},
    "redis://[PASSWORD_1]@cache:6379",
    counts={"connection_string_password": 1},
    policy=only("connection_string_password"),
    note="The user and password end at the last @ before the host, however far it stands.",
)
# Passwords: a value that starts with $ or reads %NAME% is a value; only a
# reference to the environment on its own is a name; a default is a value.
masked(
    "connection_string_password: a password starting with $",
    "Server=db;Database=app;User Id=sa;Password=",
    "$Jq4BRtx9",
    ";",
    "PASSWORD",
    "connection_string_password",
    policy=only("connection_string_password"),
)
masked(
    "connection_string_password: a password of the form %NAME%",
    "Server=db;Database=app;User Id=sa;Password=",
    "%Admin%S3c",
    ";",
    "PASSWORD",
    "connection_string_password",
    policy=only("connection_string_password"),
)
masked(
    "connection_string_password: POSTGRES_PASSWORD starting with $",
    "POSTGRES_PASSWORD=",
    "$Jq4BRtx9",
    "",
    "PASSWORD",
    "connection_string_password",
    policy=only("connection_string_password"),
)
masked(
    "connection_string_password: a default after ${NAME:",
    "spring.datasource.password=",
    "${DB_PASSWORD:S3cretPass}",
    "",
    "PASSWORD",
    "connection_string_password",
    policy=only("connection_string_password"),
)
masked(
    "connection_string_password: a name after $ is taken",
    "POSTGRES_PASSWORD=",
    "$DB_PASSWORD",
    "",
    "PASSWORD",
    "connection_string_password",
    policy=only("connection_string_password"),
    note="A value that starts with $ may be a password: only ${NAME} in braces is read as a reference.",
)
masked(
    "generic_secret: a password starting with $",
    "password: ",
    "$Jq4BRtx9",
    "",
    "SECRET",
    "generic_secret",
    policy=only("generic_secret"),
)
masked(
    "generic_secret: a french password starting with $",
    "Mot de passe : ",
    "$Jq4BRtx9",
    "",
    "SECRET",
    "generic_secret",
    policy=only("generic_secret"),
)
masked(
    "generic_secret: the default of os.environ.get",
    'password = os.environ.get("DB_PASSWORD", "',
    "S3cretPass",
    '")',
    "SECRET",
    "generic_secret",
    policy=only("generic_secret"),
)
masked(
    "generic_secret: the default after process.env",
    'const DB_PASS = process.env.DB_PASS || "',
    "S3cretPass",
    '";',
    "SECRET",
    "generic_secret",
    policy=only("generic_secret"),
)
LONG_SECRET = "".join(RNG_QA.choice(ALNUM) for _ in range(300))
masked(
    "generic_secret: a value of 300 characters",
    "password=",
    LONG_SECRET,
    "\nnext",
    "SECRET",
    "generic_secret",
    policy=only("generic_secret"),
)
kept(
    "url_credentials: a user name alone",
    "clone ssh://git@git.acme.example/app.git",
    policy=only("url_credentials"),
)
masked(
    "url_credentials: a token as the user",
    "clone https://",
    "ghp_" + "".join(RNG_QA.choice(ALNUM) for _ in range(36)),
    "@github.com/acme/app.git",
    "CREDENTIALS",
    "url_credentials",
    policy=only("url_credentials"),
)

# Placeholder numbers have at most nine digits: a text that already holds the
# last one cannot be numbered further, and is blocked rather than confused.
add(
    "placeholder: numbering past nine digits is blocked",
    "[EMAIL_999999999] and marie@example.com",
    blocked={"size_limit": 1},
    policy=only("email"),
)
add(
    "placeholder: the last nine-digit number is still given",
    "[EMAIL_999999998] and marie@example.com",
    "[EMAIL_999999998] and [EMAIL_999999999]",
    counts={"email": 1},
    policy=only("email"),
)

# ── third pass (19/11): a guard never rejects a valid value ──────────────
# What follows a value either stands apart from it (a delimiter, a word after a
# space: the value is taken as it is) or carries it on with no separator (the
# take extends over it: more is masked, never less). A value that runs to its
# delimiter or to the end of its text is followed there by the engine itself,
# in linear time. A value right after an escape written as two characters
# (\n, \t, \r in JSON or a tool result pasted as text) is read like one after a
# real line break.
LABEL = {detector["id"]: detector["label"] for detector in RULES["detectors"]}


def _place(lead: str, value: str, trail: str, prefix: str) -> str:
    """`prefix` right before the value, or before a lead that is its form."""
    form = lead and (
        lead[-1] not in " \t\n" or lead.lower().endswith(("bearer ", "basic "))
    )
    return (prefix + lead if form else lead + prefix) + value + trail


_FIRST: dict[str, dict[str, str]] = {}
for _positive in POSITIVES:
    _FIRST.setdefault(_positive["detector"], dict(_positive))
for _detector, _positive in _FIRST.items():
    for _escape, _escape_name in (("\\n", "n"), ("\\t", "t"), ("\\r\\n", "r n")):
        _lead, _value, _trail = (
            _positive["lead"],
            _positive["value"],
            _positive["trail"],
        )
        add(
            f"escape: {_detector} after \\{_escape_name} written in two characters",
            _place(_lead, _value, _trail, _escape),
            _place(_lead, f"[{LABEL[_detector]}_1]", _trail, _escape),
            counts={_detector: 1},
            policy=only(_detector),
        )
masked(
    "escape: an e-mail address does not take the letter of the escape",
    "mail\\n",
    "marie@example.com",
    "\\nend",
    "EMAIL",
    "email",
    policy=only("email"),
)
masked(
    "escape: PGPASSWORD in an environment pasted as a JSON string",
    '{"env": "A=1\\nPGPASSWORD=',
    "Sup3rS3cret\\nB=2",
    '"}',
    "PASSWORD",
    "connection_string_password",
    policy=only("connection_string_password"),
    note="A value read to its delimiter runs over an escape written in two characters: more is masked, never less.",
)
masked(
    "escape: a card after \\n in a tool result",
    "card\\n",
    "4111 1111 1111 1111",
    "\\nexp",
    "CARD",
    "credit_card",
    policy=only("credit_card"),
)

# The organization's own entries read an escape written in two characters the
# way the catalogue does.
for _name, _text, _expected, _label in (
    (
        "a term",
        "note:\\nProjet Atlas starts",
        "note:\\n[PROJECT_1] starts",
        "custom:PROJECT",
    ),
    (
        "a domain",
        "mail:\\nmarie@acme.example now",
        "mail:\\n[CLIENT_1] now",
        "custom:CLIENT",
    ),
    ("a pattern", "ref:\\nPRJ-1234 open", "ref:\\n[REF_1] open", "custom:REF"),
):
    add(
        f"escape: {_name} after \\n written in two characters",
        _text,
        _expected,
        counts={_label: 1},
        policy={
            "schema": 1,
            "custom": [
                {
                    "kind": "term",
                    "value": "Projet Atlas",
                    "action": "mask",
                    "label": "PROJECT",
                },
                {
                    "kind": "domain",
                    "value": "acme.example",
                    "action": "mask",
                    "label": "CLIENT",
                },
                {
                    "kind": "pattern",
                    "value": "PRJ-####",
                    "action": "mask",
                    "label": "REF",
                },
            ],
        },
    )
masked(
    "url credentials: a password holding @@",
    "redis://",
    ":p@@ss",
    "@cache:6379",
    "PASSWORD",
    "connection_string_password",
    policy=only("connection_string_password"),
)
masked(
    "url credentials: an Oracle thin password holding @",
    "jdbc:oracle:thin:",
    "scott/p@ss",
    "@db.example.com:1521:orcl",
    "PASSWORD",
    "connection_string_password",
    policy=only("connection_string_password"),
)

# Credentials in a URL end at the last @ before the host, whatever follows the
# host: a backtick, a backslash, a quote escaped in JSON, markdown, a query.
for _name, _before, _value, _after, _detector in (
    (
        "between backticks",
        "My cache is `redis://",
        "default:Sup3rS3cret",
        "@redis.example.com:6379` in prod",
        "connection_string_password",
    ),
    (
        "in JSON written as a string",
        'config: {\\"redis_url\\": \\"redis://',
        ":Sup3rS3cret",
        '@cache.internal:6379\\"}',
        "connection_string_password",
    ),
    (
        "before an escaped newline",
        "DATABASE_URL=postgres://",
        "admin:Sup3rS3cret",
        "@db.example.com:5432\\nREDIS_URL=x",
        "connection_string_password",
    ),
    (
        "in bold",
        "use **https://",
        "bob:s3cr3tpw",
        "@host.example.com** for the API",
        "url_credentials",
    ),
    (
        "before &",
        "url=https://",
        "bob:s3cr3tpw",
        "@host.example.com&mode=1",
        "url_credentials",
    ),
    (
        "between braces",
        "{{https://",
        "bob:s3cr3tpw",
        "@host.example.com}}",
        "url_credentials",
    ),
    (
        "before ]",
        "mongodb+srv://",
        "app:Sup3rS3cret",
        "@cluster0.abcde.mongodb.net] and more",
        "connection_string_password",
    ),
    (
        "between parentheses",
        "(amqp://",
        "guest:Sup3rS3cret",
        "@rabbit.example.com:5672)",
        "connection_string_password",
    ),
    (
        "a password holding @, before !",
        "redis://",
        ":p@ss@w0rd",
        "@cache:6379!",
        "connection_string_password",
    ),
    (
        "a password holding @, before a backslash",
        "redis://",
        ":p@ss@w0rd",
        "@cache:6379\\",
        "connection_string_password",
    ),
):
    masked(
        f"url credentials: host {_name}",
        _before,
        _value,
        _after,
        LABEL[_detector],
        _detector,
        policy=only(_detector),
    )
masked(
    "url credentials: the host alone after them",
    "https://",
    "bob:s3cr3tpw",
    "@host.example.com",
    "CREDENTIALS",
    "url_credentials",
    policy=only("url_credentials"),
)
masked(
    "url credentials: a connection string, the host alone after them",
    "redis://",
    "default:Sup3rS3cret",
    "@redis.example.com:6379",
    "PASSWORD",
    "connection_string_password",
    policy=only("connection_string_password"),
)

# A private key cut before its END line, key material following, is masked to
# the end of its text, placeholders already there included; with its END line,
# from BEGIN to END only.
_CUT_KEY = PEM_HEAD + "\n" + "\n".join(PEM_BODY.split("\n")[:2])
for _name, _after in (
    ("a placeholder after it", "\n\nCan you send it to [EMAIL_1] please?"),
    ("an address after it", "\n\nCan you send it to bob@example.com please?"),
    ("a step reference after it", "\n\nSee step [STEP_2] of the doc."),
):
    add(
        f"private_key: no end line, {_name}",
        _CUT_KEY + _after,
        "[PRIVATE_KEY_1]",
        counts={"private_key": 1},
        policy=only("private_key"),
    )
    add(
        f"private_key: no end line, {_name}, blocked",
        _CUT_KEY + _after,
        blocked={"private_key": 1},
        policy=blocking("private_key"),
    )
masked(
    "private_key: no end line, alone",
    "",
    _CUT_KEY,
    "",
    "PRIVATE_KEY",
    "private_key",
    policy=only("private_key"),
)
add(
    "private_key: an end line, then a placeholder",
    PEM + "\nmail [EMAIL_1]",
    "[PRIVATE_KEY_1]\nmail [EMAIL_1]",
    counts={"private_key": 1},
    policy=only("private_key"),
)
kept(
    "private_key: twelve BEGIN lines without key material",
    "\n".join([PEM_HEAD] * 12),
    policy=only("private_key"),
)
add(
    "private_key: twelve BEGIN lines without key material, blocked",
    "\n".join([PEM_HEAD] * 12),
    blocked={"private_key": 12},
    policy=blocking("private_key"),
)

# An international number of a valid form is taken; what follows it after a
# separator is not part of it. When the longest reading has too many digits,
# the longest reading of 9 to 15 digits that ends a group is taken.
for _name, _before, _value, _after in (
    ("then a street number", "John Doe ", "+14155552671", " 42 Main Street"),
    ("then 24/7", "Call ", "+447911123456", " 24/7"),
    ("then a count", "ring ", "+14155552671", " 2 times"),
    ("then a number glued by a dot", "Call ", "+4915112345678", ".2 now"),
    ("then a currency code", "Tel ", "+41 44 668 18 00", " CHF"),
    ("then the euro sign", "Tel ", "+44 20 7946 0958", " \u20ac"),
):
    masked(
        f"phone: international {_name}",
        _before,
        _value,
        _after,
        "PHONE",
        "phone",
        policy=only("phone"),
    )
add(
    "phone: international then a time",
    "Call +44 20 7946 0958 9am to 5pm",
    "Call [PHONE_1] to 5pm",
    counts={"phone": 1},
    policy=only("phone"),
    note="A group of digits read with the number carries it on when letters are glued to it: the reading never backs off, so 9am is masked with the number.",
)
add(
    "phone: the longest reading of at most 15 digits",
    "+44 20 7946 0958 2024-05-01",
    "[PHONE_1] 2024-05-01",
    counts={"phone": 1},
    policy=only("phone"),
    note="Twenty digits are no number: the longest reading that ends a group and holds 9 to 15 digits is taken.",
)
add(
    "phone: round thousands stay a quantity",
    "a budget of +150 000 000 and +12 500 000",
    policy=only("phone"),
)

# IPv4: a slash and digits after the address are taken with it; what follows
# a slash and a word is not. Colon-separated groups before an IPv4 address that
# do not make an IPv6 address do not hide it.
for _name, _before, _value, _after in (
    (
        "then a path starting with a year",
        "open http://",
        "192.168.1.10/2024",
        "/report.pdf now",
    ),
    ("then a slash and two digits", "open http://", "192.168.1.10/45", "/x now"),
    ("then a slash and three digits", "open http://", "192.168.1.10/123", " now"),
    ("then a slash and one digit", "open http://", "192.168.1.10/1", "/edit now"),
    ("after a name and a port", "bind db:5432:", "10.0.0.5", " now"),
    ("after a time", "at 12:30:", "192.168.1.10", " the host died"),
    ("then a slash and a word", "ping ", "1.2.3.4", "/abc"),
):
    masked(
        f"ip_address: {_name}",
        _before,
        _value,
        _after,
        "IP_ADDRESS",
        "ip_address",
        policy=only("ip_address"),
    )
add(
    "ip_address: version words, build and prefixes",
    "server 10.0.0.1/24 and 10.1.2.3/8/x and v1.2.3.4 and build 10.2.3.4",
    "server [IP_ADDRESS_1] and [IP_ADDRESS_2]/x and v1.2.3.4 and build [IP_ADDRESS_3]",
    counts={"ip_address": 3},
    policy=only("ip_address"),
)
for _name, _before, _value, _after in (
    (
        "compressed, then a colon",
        "my address is ",
        "fe80::1ff:fe23:4567:890a",
        ": can you ping it",
    ),
    (
        "full, then a colon",
        "my address is ",
        "2001:db8:0:0:0:0:0:1",
        ": can you ping it",
    ),
    ("with an IPv4 tail, then a port", "host ", "::ffff:192.0.2.128", ":8080 up"),
    ("between brackets, then a port", "[", "2001:db8::1", "]:8080"),
):
    masked(
        f"ip_address: IPv6 {_name}",
        _before,
        _value,
        _after,
        "IP_ADDRESS",
        "ip_address",
        policy=only("ip_address"),
    )
kept(
    "ip_address: a shortened MAC address is no IPv6 address",
    "mac 0:1a:2b:3c:4d:5e",
    policy=only("ip_address"),
)

add(
    "ip_address: IPv6 at the end, then a colon",
    "and 2001:db8::1:",
    "and [IP_ADDRESS_1]:",
    counts={"ip_address": 1},
    policy=only("ip_address"),
)

# A BIC spaced 4-2-2-3 is taken as it is when a word follows it.
for _name, _after in (("a currency code", " EUR account"), ("an IBAN", " IBAN FR76")):
    masked(
        f"bic: spaced, then {_name}",
        "BIC: ",
        "BNPA FR PP XXX",
        _after,
        "BIC",
        "bic",
        policy=only("bic"),
    )
masked(
    "bic: spaced 8, then a word",
    "BIC: ",
    "BNPA FR PP",
    " and EUR account",
    "BIC",
    "bic",
    policy=only("bic"),
)

# E-mail: a colon, a dotted number, a dash after an address leave it taken;
# a remote right after git clone, git remote, scp or rsync is not an address.
for _name, _before, _value, _after in (
    ("then .2", "", "bob@example.com.2", " copies"),
    (
        "then a colon, clone said before",
        "I asked him to clone it and email ",
        "alice@example.com",
        ": no answer",
    ),
    (
        "then a colon, scp said before",
        "use scp to send to ",
        "alice@example.com",
        ": she waits",
    ),
    ("then a dash", "cc ", "j.martin+test@mail.example.org-", "."),
    ("then an em dash and a word", "write to ", "bob@example.com\u2014he", " said"),
    ("a bare git remote", "", "git@github.com", ":acme/app.git"),
):
    masked(
        f"email: {_name}",
        _before,
        _value,
        _after,
        "EMAIL",
        "email",
        policy=only("email"),
    )
add(
    "email: after a slash at the start",
    "/contact@acme.example is the address",
    "/[EMAIL_1] is the address",
    counts={"email": 1},
    policy=only("email"),
)
for _name, _text in (
    ("git clone", "git clone git@github.com:org/repo.git"),
    ("git remote add", "git remote add origin git@github.com:org/repo.git"),
    ("scp", "scp bob@host.example.com:/tmp/x ."),
    ("rsync", "rsync bob@host.example.com:/srv/data ."),
):
    kept(f"email: a remote right after {_name}", _text, policy=only("email"))

# Azure: an account key carried on by base64 characters is taken with them.
for _name, _extra in (("=", "="), ("+", "+"), ("/path", "/path")):
    masked(
        f"key_azure: AccountKey then {_name}",
        "AccountKey=",
        AZURE_ACCOUNT_KEY + _extra,
        ";EndpointSuffix=core.windows.net",
        "API_KEY",
        "key_azure",
        policy=only("key_azure"),
    )

# ── fourth pass (20/11): key material comes right after the BEGIN line ────
# Without its END line, a BEGIN line takes the rest of its text only when key
# material comes right after it: nothing between them but blanks, line breaks
# (real or escaped), line prefixes, quotes, placeholders and PEM or PGP header
# lines, then 32 base64 characters, in one or two runs, the first of 16 or more.
# A BEGIN line quoted
# in prose is not a key: under Mask it is left as it is and not counted, under
# Block it blocks. What stands further down the text (a commit hash, a long
# path) is never read as key material.
_D5 = "-" * 5
_PK_HEAD = _D5 + "BEGIN " + "PRIVATE KEY" + _D5
_RSA_HEAD = _D5 + "BEGIN RSA " + "PRIVATE KEY" + _D5
_COMMIT = "3c9e1f7a5b2d4c6e8f0a1b3c5d7e9f1a2b4c6d8e"  # pragma: allowlist secret
_PROCEDURE = (
    "Procedure: rotating the TLS certificate of the portal (revision 4)\n\n"
    "Security reminder\n"
    "A private key shows itself by its first line, "
    + _PK_HEAD
    + " (or\n"
    + _RSA_HEAD
    + " for older keys). A file that starts this way must NEVER be\n"
    "pasted into a ticket, an e-mail, a chat tool or a Git repository.\n\n"
    "Steps\n"
    "1. On the bastion, create the key and its signing request:\n"
    "   openssl req -new -newkey rsa:3072 -nodes -keyout /etc/ssl/private/portal-2027.key"
    " -out /tmp/portal-2027.csr\n"
    "2. Commit the CSR (and it alone) to infra/certificates, branch rotation-2027, then\n"
    "   open the merge request; the reference commit of the previous procedure is\n"
    "   " + _COMMIT + ".\n"
    "3. Install the signed certificate in /etc/ssl/certs/portal-2027.crt, then run\n"
    "   nginx -t and systemctl reload nginx.\n"
)
_ONE_HEAD = _PROCEDURE.replace(" (or\n" + _RSA_HEAD + " for older keys)", "")
for _name, _text, _heads in (
    ("as written", _PROCEDURE, 2),
    ("a 12-character hash", _PROCEDURE.replace(_COMMIT, _COMMIT[:12]), 2),
    (
        "a long path instead of the hash",
        _PROCEDURE.replace(_COMMIT, "/srv/repos/infra/certificates/archives/rotation"),
        2,
    ),
    ("one BEGIN line", _ONE_HEAD, 1),
    (
        "between French quotation marks",
        _PROCEDURE.replace(
            _PK_HEAD + " (or\n" + _RSA_HEAD,
            "« " + _PK_HEAD + " » (or\n« " + _RSA_HEAD + " »",
        ),
        2,
    ),
    (
        "the hash past 1,024 characters",
        _PROCEDURE.replace(
            "Steps\n",
            "Steps\n" + "Read the whole procedure before you start. " * 25 + "\n",
        ),
        2,
    ),
):
    kept(
        f"private_key: a procedure that quotes the BEGIN line, {_name}",
        _text,
        policy=only("private_key"),
        note="A BEGIN line quoted in prose is not a key: a commit hash or a long path further down is not key material, and nothing is masked or counted under Mask.",
    )
    add(
        f"private_key: a procedure that quotes the BEGIN line, {_name}, blocked",
        _text,
        blocked={"private_key": _heads},
        policy=blocking("private_key"),
    )
kept(
    "private_key: a procedure that quotes the BEGIN line, every detector",
    _PROCEDURE,
)
for _name, _text in (
    (
        "the file starts with it, then a paragraph",
        "Check that the file starts with " + _PK_HEAD + " then read on.\n\n"
        "Next, open the team vault, copy the new key into the secret manager, restart "
        "the payroll service and check in its journal that TLS is back. The fix is "
        "commit " + _COMMIT + ".",
    ),
    (
        "the line alone, then a hash on the next line",
        _PK_HEAD + "\nis the first line; the fix is commit\n" + _COMMIT,
    ),
    (
        "a diff that changes the BEGIN line only",
        "diff --git a/docs/keys.md b/docs/keys.md\n"
        "index 3c9e1f7..8d2b4c6 100644\n"
        "--- a/docs/keys.md\n"
        "+++ b/docs/keys.md\n"
        "@@ -3,3 +3,3 @@\n"
        "-A key file starts with " + _PK_HEAD + "\n"
        "+A key file starts with " + _PK_HEAD + " and nothing else.\n"
        " Never paste one into a ticket: see commit " + _COMMIT + ".\n",
    ),
    (
        "a diff that adds the BEGIN line alone",
        "@@ -1,2 +1,3 @@\n+" + _PK_HEAD + "\n context line one\n context line two\n",
    ),
    ("in backquotes", "Look for `" + _PK_HEAD + "` in " + _COMMIT + "."),
    (
        "between parentheses",
        "(" + _PK_HEAD + ") " + "/srv/repos/infra/certificates/archives/rotation",
    ),
    (
        "then a placeholder and prose",
        _PK_HEAD + "\n[EMAIL_1] wrote this line, see " + _COMMIT,
    ),
):
    kept(
        f"private_key: a quoted BEGIN line, {_name}", _text, policy=only("private_key")
    )
add(
    "private_key: base64 characters right after the BEGIN line are key material",
    "See " + _PK_HEAD + " " + _COMMIT + " in the log.",
    "See [PRIVATE_KEY_1]",
    counts={"private_key": 1},
    policy=only("private_key"),
    note="Thirty-two letters, digits, + or / right after a BEGIN line (a commit hash, a fingerprint, a long path) read as key material: everything from the BEGIN line to the end of the text is masked.",
)
_RUN31 = _COMMIT[:31]
for _name, _text, _taken in (
    (
        "31 characters then a full stop",
        _PK_HEAD + " " + _RUN31 + ". Then prose.",
        False,
    ),
    (
        "20 characters then a short word",
        _PK_HEAD + " " + _COMMIT[:20] + " in the log.",
        False,
    ),
    ("31 characters then a word", _PK_HEAD + " " + _RUN31 + " in the log.", True),
    ("two lines of 16", _PK_HEAD + "\n" + _COMMIT[:16] + "\n" + _COMMIT[16:32], True),
    ("one line of 32", _PK_HEAD + "\n" + _COMMIT[:32] + "\n", True),
):
    if _taken:
        add(
            f"private_key: right after the BEGIN line, {_name}",
            "Log: " + _text,
            "Log: [PRIVATE_KEY_1]",
            counts={"private_key": 1},
            policy=only("private_key"),
            note="Key material is 32 base64 characters in one or two runs, the first of 16 or more.",
        )
    else:
        kept(
            f"private_key: right after the BEGIN line, {_name}",
            "Log: " + _text,
            policy=only("private_key"),
            note="Fewer than 32 base64 characters in one or two runs: the BEGIN line is quoted.",
        )


# A key cut before its END line stays masked to the end of its text however it
# is written: the forms a key takes when pasted, at the widths it is wrapped to.
def _cut_key_lines(
    width: int, head: str = PEM_HEAD, body: str = PEM_BODY.replace("\n", "")
) -> list[str]:
    return [head] + [
        body[index : index + width] for index in range(0, len(body), width)
    ]


_CUT_FORMS: dict[str, Any] = {
    "plain": lambda lines: "\n".join(lines),
    "crlf": lambda lines: "\r\n".join(lines),
    "escaped in JSON": lambda lines: (
        '{"private_key": ' + json.dumps("\n".join(lines)) + "}"
    ),
    "escaped twice": lambda lines: json.dumps(json.dumps("\n".join(lines))),
    "quoted once": lambda lines: "\n".join("> " + line for line in lines),
    "quoted twice": lambda lines: "\n".join("> > " + line for line in lines),
    "removed in a diff": lambda lines: "\n".join("-" + line for line in lines),
    "added in a diff": lambda lines: "\n".join("+" + line for line in lines),
    "yaml, indented by 2": lambda lines: (
        "key: |\n" + "\n".join("  " + line for line in lines)
    ),
    "yaml, indented by 17": lambda lines: (
        "key: |\n" + "\n".join(" " * 17 + line for line in lines)
    ),
    "yaml, indented by 200": lambda lines: (
        "key: |\n" + "\n".join(" " * 200 + line for line in lines)
    ),
    "a // comment": lambda lines: "\n".join("// " + line for line in lines),
    "a # comment": lambda lines: "\n".join("# " + line for line in lines),
    "a ; comment": lambda lines: "\n".join("; " + line for line in lines),
    "a block comment": lambda lines: (
        "/**\n" + "\n".join(" * " + line for line in lines)
    ),
    "html line breaks": lambda lines: "<br>".join(lines),
    "html line breaks, closed": lambda lines: "<br />".join(lines),
    "javascript concatenation": lambda lines: (
        "const k =\n" + "\n".join('  "' + line + '\\n" +' for line in lines)
    ),
    "a python list": lambda lines: (
        "K = [\n" + "\n".join('    "' + line + '",' for line in lines)
    ),
    "one line, spaces": lambda lines: " ".join(lines),
    "one line, glued": lambda lines: "".join(lines),
    "trailing spaces": lambda lines: "\n".join(line + "   " for line in lines),
    "tabs": lambda lines: "\n".join("\t" + line for line in lines),
    "a code block": lambda lines: "```\n" + "\n".join(lines),
    "a markdown cell": lambda lines: "| payroll | " + "<br>".join(lines),
    "csv": lambda lines: 'id,key\n1,"' + "\n".join(lines),
    "an env file": lambda lines: 'PRIVATE_KEY="' + "\\n".join(lines),
    "a shell continuation": lambda lines: "\\\n".join(lines),
}
for _width in (64, 31, 24, 16):
    for _form, _write in _CUT_FORMS.items():
        _text = "Here is the payroll key:\n" + _write(_cut_key_lines(_width))
        add(
            f"private_key: cut before its END line, {_form}, lines of {_width}",
            _text + "\nPlease rotate it before Friday.",
            _text[: _text.index(_D5 + "BEGIN")] + "[PRIVATE_KEY_1]",
            counts={"private_key": 1},
            policy=only("private_key"),
        )
# A short key (an Ed25519 key in PKCS #8 is 64 characters) cut in its middle
# leaves 32 characters: at width 24, 28 or 31 its second line is shorter than
# 16. It stays masked to the end of its text in every form.
_HALF = PEM_BODY.replace("\n", "")[64:96]
for _width in (32, 31, 24, 16):
    for _form, _write in _CUT_FORMS.items():
        _text = "Here is the payroll key:\n" + _write(
            _cut_key_lines(_width, body=_HALF)
        )
        add(
            f"private_key: a short key cut in its middle, {_form}, lines of {_width}",
            _text + "\nPlease rotate it before Friday.",
            _text[: _text.index(_D5 + "BEGIN")] + "[PRIVATE_KEY_1]",
            counts={"private_key": 1},
            policy=only("private_key"),
        )
for _name, _headers in (
    (
        "pgp, a comment with a placeholder",
        ["Version: GnuPG v2", "Comment: exported by [EMAIL_1]", ""],
    ),
    (
        "pgp, a comment with an address",
        ["Version: GnuPG v2", "Comment: exported by marie@example.com", ""],
    ),
    (
        "pgp, a comment with a URL",
        ["Version: GnuPG v2", "Comment: https://gpgtools.org", ""],
    ),
    (
        "encrypted pem",
        ["Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC,0A1B2C3D4E5F", ""],
    ),
):
    _lines = _cut_key_lines(
        64, "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----" if "pgp" in _name else PEM_HEAD
    )
    _lines = _lines[:1] + _headers + _lines[1:]
    for _form in (
        "plain",
        "escaped in JSON",
        "quoted once",
        "a python list",
        "yaml, indented by 200",
    ):
        _text = "Key:\n" + _CUT_FORMS[_form](_lines)
        add(
            f"private_key: cut before its END line, {_name}, {_form}",
            _text + "\nThanks.",
            _text[: _text.index(_D5 + "BEGIN")] + "[PRIVATE_KEY_1]",
            counts={"private_key": 1},
            policy=only("private_key"),
        )
_lines = _cut_key_lines(64)
add(
    "private_key: cut before its END line, a placeholder among its lines",
    "\n".join(_lines[:3] + ["[STEP_2]"] + _lines[3:]) + "\nThanks.",
    "[PRIVATE_KEY_1]",
    counts={"private_key": 1},
    policy=only("private_key"),
)
add(
    "private_key: cut before its END line, a placeholder right after the BEGIN line",
    "\n".join(_lines[:1] + ["[STEP_2]"] + _lines[1:]) + "\nThanks.",
    "[PRIVATE_KEY_1]",
    counts={"private_key": 1},
    policy=only("private_key"),
)
# A key cut across two text values: only the value that holds its BEGIN line
# is masked; the rest leaves as it is, and the key is counted as masked (the
# documentation says so, under what it does not catch).
_second = "\n".join(_lines[3:]) + "\n-----END RSA " + "PRIVATE KEY-----\nThanks."
add(
    "private_key: cut across two text values, the second is left (not caught)",
    ["Key:\n" + "\n".join(_lines[:3]), _second],
    ["Key:\n[PRIVATE_KEY_1]", _second],
    counts={"private_key": 1},
    policy=only("private_key"),
)
# A key cut before its END line, part of its first 32 base64 characters
# replaced by a marker that is not a placeholder: the BEGIN line reads as
# quoted, so under Mask the whole key is left and not counted, and under Block
# the call is blocked (the documentation says so, under what it does not
# catch). A marker further down leaves the key masked.
for _marker in ("[REDACTED]", "...", "<redacted>"):
    for _where, _at in (("the start of its body", 0), ("its first line", 20)):
        _first = _lines[1][:_at] + _marker + _lines[1][_at + len(_marker) :]
        _text = "Key:\n" + "\n".join([_lines[0], _first, *_lines[2:]]) + "\nThanks."
        kept(
            f"private_key: cut before its END line, {_marker} in {_where} (not caught)",
            _text,
            policy=only("private_key"),
        )
        add(
            f"private_key: cut before its END line, {_marker} in {_where}, blocked",
            _text,
            blocked={"private_key": 1},
            policy=blocking("private_key"),
        )
    _text = "Key:\n" + "\n".join([*_lines[:2], _marker, *_lines[3:]]) + "\nThanks."
    add(
        f"private_key: cut before its END line, {_marker} on its second line",
        _text,
        "Key:\n[PRIVATE_KEY_1]",
        counts={"private_key": 1},
        policy=only("private_key"),
    )

# Four numbers after v and a space are a version, as glued to v (v1.2.3.4); an
# option -v before them is not a version word.
for _text in ("upgrade to v 3.10.1.9 tonight", "Ship V 2.4.1.0 today", "(v  1.2.3.4)"):
    kept(f"ip_address: v and a space, {_text}", _text, policy=only("ip_address"))
masked(
    "ip_address: an option -v and a space",
    "ping -v ",
    "10.0.0.5",
    " now",
    "IP_ADDRESS",
    "ip_address",
    policy=only("ip_address"),
)

# A reference to a variable in braces (${NAME}) is a name, not a secret, in a
# URL as in an assignment: the user and the reference are left as they are.
for _detector in ("url_credentials", "connection_string_password", None):
    _policy = only(_detector) if _detector else "reference"
    _suffix = _detector or "every detector"
    kept(
        f"credentials: a reference to a variable as the password of a URL, {_suffix}",
        "url: postgres://billing_app:${DB_PASSWORD}@db.internal:5432/billing",
        policy=_policy,
    )
kept(
    "url_credentials: a reference to a variable as the token of a URL",
    "git clone https://${GITHUB_TOKEN}@github.com/org/repo.git",
    policy=only("url_credentials"),
)
kept(
    "url_credentials: references to variables as the user and the password",
    "https://${API_USER}:${API_PASSWORD}@api.example.com/v1",
    policy=only("url_credentials"),
)
masked(
    "connection_string_password: a default after ${NAME: in a URL",
    "postgres://",
    "billing_app:${DB_PASSWORD:-S3cretPass}",
    "@db.internal:5432/payroll",
    "PASSWORD",
    "connection_string_password",
    policy=only("connection_string_password"),
)
masked(
    "connection_string_password: a name after $ in a URL is taken",
    "postgres://",
    "billing_app:$DB_PASSWORD",
    "@db.internal:5432/payroll",
    "PASSWORD",
    "connection_string_password",
    policy=only("connection_string_password"),
)
masked(
    "url_credentials: a reference with more after it is taken",
    "https://",
    "app:${API_PASSWORD}x9",
    "@api.example.com",
    "CREDENTIALS",
    "url_credentials",
    policy=only("url_credentials"),
)

# A token written in a URL keeps its escapes (%2F, %3D): they are part of it,
# whichever detector reads it, and one in its first characters does not hide it.
_SLACK_D = "xoxd-" + "abcdefghij" * 4
masked(
    "key_slack: a token encoded in a URL",
    "d=",
    _SLACK_D + "%2F" + "klmnopqrst" * 2 + "%3D",
    "; path=/",
    "API_KEY",
    "key_slack",
    policy=only("key_slack"),
)
masked(
    "key_slack: an escape in the first ten characters",
    "d=",
    "xoxd-ab%2F" + "cdefghijkl" * 2,
    "; path=/",
    "API_KEY",
    "key_slack",
    policy=only("key_slack"),
)
masked(
    "key_github: a token encoded in a URL",
    "token=",
    KEY_GITHUB + "%2Fabcdefgh",
    "&next=1",
    "API_KEY",
    "key_github",
    policy=only("key_github"),
)
masked(
    "key_azure: an account key encoded in a URL",
    "AccountKey=",
    AZURE_ACCOUNT_KEY[:40] + "%2B" + AZURE_ACCOUNT_KEY[40:86] + "%3D%3D",
    "&x=1",
    "API_KEY",
    "key_azure",
    policy=only("key_azure"),
)
masked(
    "bearer_token: a token encoded in a URL",
    "Authorization: Bearer ",
    "abc.def%2Bghijklmnop%3D%3D",
    "",
    "TOKEN",
    "bearer_token",
    policy=only("bearer_token"),
)

# A JSON Web Token whose header or claims run past 8,192 characters is taken
# whole: no bound rules a long one out.
_JWT_HEAD, _JWT_CLAIMS, _JWT_SIG = JWT.split(".")
for _name, _token in (
    (
        "long claims",
        {
            "$concat": [
                _JWT_HEAD + "." + _JWT_CLAIMS,
                {"repeat": "a", "times": 9000},
                "." + _JWT_SIG,
            ]
        },
    ),
    (
        "long header",
        {
            "$concat": [
                _JWT_HEAD,
                {"repeat": "b", "times": 9000},
                "." + _JWT_CLAIMS + "." + _JWT_SIG,
            ]
        },
    ),
):
    # Strings cut in chunks of 8, as every secret-shaped string of this file.
    _parts_in = ["Log: "]
    for _part in _token["$concat"]:
        if isinstance(_part, str):
            _parts_in += [_part[i : i + 8] for i in range(0, len(_part), 8)]
        else:
            _parts_in.append(_part)
    _parts_in.append(" end")
    add(
        f"jwt: {_name} past 8,192 characters",
        {"$parts": _parts_in},
        "Log: [TOKEN_1] end",
        counts={"jwt": 1},
        policy=only("jwt"),
    )

# An IBAN whose country is followed by a dash, or by two spaces, is still an
# IBAN when it passes the mod-97 check.
masked(
    "iban: a dash after the country, groups of four from the check digits",
    "IBAN ",
    "FR-7630-0060-0001-1234-5678-9018-9",
    " ok",
    "IBAN",
    "iban",
    policy=only("iban"),
)
masked(
    "iban: two spaces after the country",
    "IBAN ",
    "FR  76 3000 6000 0112 3456 7890 189",
    " ok",
    "IBAN",
    "iban",
    policy=only("iban"),
)
kept(
    "iban: a dash after the country, a wrong check",
    "IBAN FR-7630-0060-0001-1234-5678-9018-8 ok",
    policy=only("iban"),
)

# An IPv6 address followed by :: (the end of a sentence, a separator) is read
# without them; ver without its dot is no version word.
masked(
    "ip_address: an IPv6 address followed by ::",
    "x ",
    "fe80::1ff:fe23:4567:890a",
    ":: y",
    "IP_ADDRESS",
    "ip_address",
    policy=only("ip_address"),
)
masked(
    "ip_address: ver without its dot",
    "server ver ",
    "10.0.0.8",
    " ok",
    "IP_ADDRESS",
    "ip_address",
    policy=only("ip_address"),
)
kept("ip_address: ver. and four numbers", "ver. 1.2.3.4", policy=only("ip_address"))

# An address glued to a colon and a run that is not a port number carries a
# password: it is taken with the address (counted as one address). After a
# colon and a space, a port, or a path starting with /, it is not.
for _text, _expected in (
    ("login marie@example.com:S3cr3t!pw now", "login [EMAIL_1] now"),
    ("login marie@example.com:S3cr3t!pw", "login [EMAIL_1]"),
    # A run that holds a / reads as a path (a remote), not as a password.
    ("login marie@example.com:S3cr3t/path now", "login [EMAIL_1]:S3cr3t/path now"),
    ("imap marie@example.com:993 ok", "imap [EMAIL_1]:993 ok"),
    ("Write to marie@example.com: she answers", "Write to [EMAIL_1]: she answers"),
    ("copy deploy@example.com:/var/www now", "copy [EMAIL_1]:/var/www now"),
    ('{"login":"marie@example.com:pw1","next":2}', '{"login":"[EMAIL_1]","next":2}'),
):
    add(
        f"email: after the colon, {_text}",
        _text,
        _expected,
        counts={"email": 1},
        policy=only("email"),
    )

# A French VAT number written with dashes or dots between its parts.
_VAT_KEY = VAT[2:4]
for _sep, _name in (("-", "dashes"), (".", "dots")):
    masked(
        f"fr_vat: {_name}",
        "TVA ",
        "FR" + _VAT_KEY + _sep + SIREN[:3] + _sep + SIREN[3:6] + _sep + SIREN[6:],
        " ok",
        "VAT_NUMBER",
        "fr_vat",
        policy=only("fr_vat"),
    )

# A user and password before an @ after // without a scheme (//user:pass@host).
masked(
    "url_credentials: //user:password@host without a scheme",
    "see //",
    "bob:Pa55word",
    "@host.example.com/x",
    "CREDENTIALS",
    "url_credentials",
    policy=only("url_credentials"),
)
kept(
    "url_credentials: a path with // and an @, no password",
    "see //bob@host.example.com/x and a//b@c",
    policy=only("url_credentials"),
)

# ── keys the caller names (descriptor `caller_keyed_keys`) ────────────────
# Under variables, metadata-like maps and JSON Schema `properties`, `$defs`,
# `definitions` and `patternProperties`, every key is a name the caller chose:
# a value under one named `name`, `id` or `type` is read like any other, with
# the usual rules (an image is still an attachment, a schema's `type` stays
# structural).

add(
    "caller keys: openai chat tools[].function.parameters",
    {
        "model": "gpt-4o",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "description": "Ask marie@example.com",
                            }
                        },
                        "required": ["type"],
                    },
                },
            }
        ],
    },
    {
        "model": "gpt-4o",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "description": "Ask [EMAIL_1]"}
                        },
                        "required": ["type"],
                    },
                },
            }
        ],
    },
    provider="openai",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: openai chat response_format.json_schema.schema",
    {
        "model": "gpt-4o",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "contact",
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Ask marie@example.com",
                        }
                    },
                    "required": ["name"],
                },
            },
        },
    },
    {
        "model": "gpt-4o",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "contact",
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Ask [EMAIL_1]"}
                    },
                    "required": ["name"],
                },
            },
        },
    },
    provider="openai",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: openai chat response_format.json_schema.schema $defs",
    {
        "model": "gpt-4o",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "contact",
                "schema": {
                    "$ref": "#/$defs/id",
                    "$defs": {
                        "id": {"type": "string", "description": "marie@example.com"}
                    },
                },
            },
        },
    },
    {
        "model": "gpt-4o",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "contact",
                "schema": {
                    "$ref": "#/$defs/id",
                    "$defs": {"id": {"type": "string", "description": "[EMAIL_1]"}},
                },
            },
        },
    },
    provider="openai",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: openai responses tools[].parameters",
    {
        "model": "gpt-4o",
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Ask marie@example.com"}
                    },
                    "required": ["id"],
                },
            }
        ],
    },
    {
        "model": "gpt-4o",
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Ask [EMAIL_1]"}
                    },
                    "required": ["id"],
                },
            }
        ],
    },
    provider="openai",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: openai responses text.format.schema",
    {
        "model": "gpt-4o",
        "text": {
            "format": {
                "type": "json_schema",
                "name": "c",
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Ask marie@example.com",
                        }
                    },
                    "required": ["name"],
                },
            }
        },
    },
    {
        "model": "gpt-4o",
        "text": {
            "format": {
                "type": "json_schema",
                "name": "c",
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Ask [EMAIL_1]"}
                    },
                    "required": ["name"],
                },
            }
        },
    },
    provider="openai",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: anthropic tools[].input_schema",
    {
        "model": "claude-sonnet-4-5",
        "tools": [
            {
                "name": "lookup",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Ask marie@example.com",
                        }
                    },
                    "required": ["name"],
                },
            }
        ],
    },
    {
        "model": "claude-sonnet-4-5",
        "tools": [
            {
                "name": "lookup",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Ask [EMAIL_1]"}
                    },
                    "required": ["name"],
                },
            }
        ],
    },
    provider="anthropic",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: anthropic tools[].input_examples",
    {
        "model": "claude-sonnet-4-5",
        "tools": [
            {
                "name": "lookup",
                "input_schema": {"type": "object"},
                "input_examples": [{"id": "marie@example.com"}],
            }
        ],
    },
    {
        "model": "claude-sonnet-4-5",
        "tools": [
            {
                "name": "lookup",
                "input_schema": {"type": "object"},
                "input_examples": [{"id": "[EMAIL_1]"}],
            }
        ],
    },
    provider="anthropic",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: anthropic output_config.format.schema",
    {
        "model": "claude-sonnet-4-5",
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "Ask marie@example.com",
                        }
                    },
                    "required": ["type"],
                },
            }
        },
    },
    {
        "model": "claude-sonnet-4-5",
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "description": "Ask [EMAIL_1]"}
                    },
                    "required": ["type"],
                },
            }
        },
    },
    provider="anthropic",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: gemini config.labels",
    {"model": "gemini-2.5-flash", "config": {"labels": {"name": "marie@example.com"}}},
    {"model": "gemini-2.5-flash", "config": {"labels": {"name": "[EMAIL_1]"}}},
    provider="gemini",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: gemini config.tools[].function_declarations[].parameters",
    {
        "model": "gemini-2.5-flash",
        "config": {
            "tools": [
                {
                    "function_declarations": [
                        {
                            "name": "lookup",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "description": "Ask marie@example.com",
                                    }
                                },
                                "required": ["id"],
                            },
                        }
                    ]
                }
            ]
        },
    },
    {
        "model": "gemini-2.5-flash",
        "config": {
            "tools": [
                {
                    "function_declarations": [
                        {
                            "name": "lookup",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "description": "Ask [EMAIL_1]",
                                    }
                                },
                                "required": ["id"],
                            },
                        }
                    ]
                }
            ]
        },
    },
    provider="gemini",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: gemini config.response_schema",
    {
        "model": "gemini-2.5-flash",
        "config": {
            "response_schema": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "Ask marie@example.com"}
                },
                "required": ["type"],
            }
        },
    },
    {
        "model": "gemini-2.5-flash",
        "config": {
            "response_schema": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "Ask [EMAIL_1]"}
                },
                "required": ["type"],
            }
        },
    },
    provider="gemini",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: gemini config.responseJsonSchema $defs",
    {
        "model": "gemini-2.5-flash",
        "config": {
            "responseJsonSchema": {
                "$ref": "#/$defs/name",
                "$defs": {
                    "name": {"type": "string", "description": "marie@example.com"}
                },
            }
        },
    },
    {
        "model": "gemini-2.5-flash",
        "config": {
            "responseJsonSchema": {
                "$ref": "#/$defs/name",
                "$defs": {"name": {"type": "string", "description": "[EMAIL_1]"}},
            }
        },
    },
    provider="gemini",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: mistral tools[].function.parameters",
    {
        "model": "mistral-large-latest",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Ask marie@example.com",
                            }
                        },
                        "required": ["name"],
                    },
                },
            }
        ],
    },
    {
        "model": "mistral-large-latest",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Ask [EMAIL_1]"}
                        },
                        "required": ["name"],
                    },
                },
            }
        ],
    },
    provider="mistral",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: mistral response_format.json_schema.schema",
    {
        "model": "mistral-large-latest",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "contact",
                "schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Ask marie@example.com"}
                    },
                    "required": ["id"],
                },
            },
        },
    },
    {
        "model": "mistral-large-latest",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "contact",
                "schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Ask [EMAIL_1]"}
                    },
                    "required": ["id"],
                },
            },
        },
    },
    provider="mistral",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: deepseek tools[].function.parameters",
    {
        "model": "deepseek-chat",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "description": "Ask marie@example.com",
                            }
                        },
                        "required": ["type"],
                    },
                },
            }
        ],
    },
    {
        "model": "deepseek-chat",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "description": "Ask [EMAIL_1]"}
                        },
                        "required": ["type"],
                    },
                },
            }
        ],
    },
    provider="deepseek",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: anthropic schema definitions",
    {
        "model": "claude-sonnet-4-5",
        "tools": [
            {
                "name": "lookup",
                "input_schema": {
                    "type": "object",
                    "definitions": {
                        "name": {"type": "string", "description": "marie@example.com"}
                    },
                },
            }
        ],
    },
    {
        "model": "claude-sonnet-4-5",
        "tools": [
            {
                "name": "lookup",
                "input_schema": {
                    "type": "object",
                    "definitions": {
                        "name": {"type": "string", "description": "[EMAIL_1]"}
                    },
                },
            }
        ],
    },
    provider="anthropic",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: mistral schema patternProperties",
    {
        "model": "mistral-large-latest",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "patternProperties": {
                            "name": {
                                "type": "string",
                                "description": "marie@example.com",
                            }
                        },
                    },
                },
            }
        ],
    },
    {
        "model": "mistral-large-latest",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "patternProperties": {
                            "name": {"type": "string", "description": "[EMAIL_1]"}
                        },
                    },
                },
            }
        ],
    },
    provider="mistral",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)
add(
    "caller keys: gemini schema defs",
    {
        "model": "gemini-2.5-flash",
        "config": {
            "tools": [
                {
                    "function_declarations": [
                        {
                            "name": "lookup",
                            "parameters": {
                                "type": "object",
                                "defs": {
                                    "name": {
                                        "type": "string",
                                        "description": "marie@example.com",
                                    }
                                },
                            },
                        }
                    ]
                }
            ]
        },
    },
    {
        "model": "gemini-2.5-flash",
        "config": {
            "tools": [
                {
                    "function_declarations": [
                        {
                            "name": "lookup",
                            "parameters": {
                                "type": "object",
                                "defs": {
                                    "name": {
                                        "type": "string",
                                        "description": "[EMAIL_1]",
                                    }
                                },
                            },
                        }
                    ]
                }
            ]
        },
    },
    provider="gemini",
    counts={"email": 1},
    note="A key the caller named (`name`, `id`, `type`) is a name: what it holds is read.",
)

add(
    "caller keys: openai responses prompt.variables",
    {
        "model": "gpt-4o",
        "prompt": {
            "id": "pmpt_1",
            "variables": {
                "name": "marie@example.com",
                "type": {
                    "type": "input_image",
                    "image_url": "https://img.example.com/a.png",
                },
            },
        },
    },
    {
        "model": "gpt-4o",
        "prompt": {
            "id": "pmpt_1",
            "variables": {
                "name": "[EMAIL_1]",
                "type": {
                    "type": "input_image",
                    "image_url": "https://img.example.com/a.png",
                },
            },
        },
    },
    provider="openai",
    counts={"email": 1},
    attachments=1,
    note="A key the caller named is a name: its value is read with the usual rules (a variable's image is still an attachment, a schema's type stays structural).",
)
add(
    "caller keys: openai a schema type under properties stays structural",
    {
        "model": "gpt-4o",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "f",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "description": "Ask marie@example.com",
                            }
                        },
                    },
                },
            }
        ],
    },
    {
        "model": "gpt-4o",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "f",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "description": "Ask [EMAIL_1]"}
                        },
                    },
                },
            }
        ],
    },
    provider="openai",
    policy={
        "schema": 1,
        "app_id": "app_medhelp",
        "revision": 0,
        "detectors": {"email": "mask"},
        "attachments": "allow",
        "custom": [
            {"kind": "term", "value": "string", "action": "mask", "label": "WORD"}
        ],
    },
    counts={"email": 1},
    note="A key the caller named is a name: its value is read with the usual rules (a variable's image is still an attachment, a schema's type stays structural).",
)


# Values a schema holds for the caller (`default`, `const`, `examples`,
# `enum`): what is inside them is data, read whole, whatever its keys; and the
# tables a schema keys by the caller's names (`dependentSchemas`,
# `dependencies`) read like `properties`. On each of the five providers.
def _schema_values(label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A JSON Schema whose values hold addresses under keys a schema also uses."""

    def schema(mail: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "contact": {
                    "type": "object",
                    "default": {"name": mail[0], "id": mail[1], "type": mail[2]},
                    "const": {"type": mail[3]},
                    "examples": [{"name": mail[4]}],
                    "enum": [{"id": mail[5]}],
                }
            },
            "dependentSchemas": {"name": {"description": mail[6]}},
            "dependencies": {"id": {"properties": {"type": {"description": mail[7]}}}},
        }

    before = schema([f"{name}@example.com" for name in "abcdefgh"])
    # Numbered in the order the body is read: keys sorted, depth first.
    after = schema([f"[EMAIL_{index}]" for index in (5, 4, 6, 3, 8, 7, 2, 1)])
    return before, after


for _provider, _where in (
    (
        "openai",
        lambda body: {
            "model": "gpt-4o",
            "tools": [
                {"type": "function", "function": {"name": "f", "parameters": body}}
            ],
        },
    ),
    (
        "deepseek",
        lambda body: {
            "model": "deepseek-chat",
            "tools": [
                {"type": "function", "function": {"name": "f", "parameters": body}}
            ],
        },
    ),
    (
        "mistral",
        lambda body: {
            "model": "mistral-large",
            "tools": [
                {"type": "function", "function": {"name": "f", "parameters": body}}
            ],
        },
    ),
    (
        "anthropic",
        lambda body: {
            "model": "claude",
            "max_tokens": 10,
            "tools": [{"name": "f", "input_schema": body}],
        },
    ),
    (
        "gemini",
        lambda body: {
            "model": "gemini-2.0",
            "contents": "hi",
            "config": {"responseSchema": body},
        },
    ),
):
    _before, _after = _schema_values(_provider)
    add(
        f"caller keys: {_provider} schema values and dependent schemas",
        _where(_before),
        _where(_after),
        provider=_provider,
        counts={"email": 8},
        policy=only("email"),
        note="What a schema holds for the caller (default, const, examples, enum) is data, read whole; dependentSchemas and dependencies are keyed by the caller's names.",
    )
    # `dependentRequired` is keyed by the caller's names too: a key spelled
    # `id` or `name` there holds the caller's words, read like any other.
    _required = {
        "type": "object",
        "dependentRequired": {
            "id": ["contact", "ask marie@example.com"],
            "name": ["ask paul@example.com"],
        },
    }
    _required_after = {
        "type": "object",
        "dependentRequired": {
            "id": ["contact", "ask [EMAIL_1]"],
            "name": ["ask [EMAIL_2]"],
        },
    }
    add(
        f"caller keys: {_provider} dependentRequired",
        _where(_required),
        _where(_required_after),
        provider=_provider,
        counts={"email": 2},
        policy=only("email"),
        note="dependentRequired is keyed by the caller's names: a key spelled id or name there holds the caller's words.",
    )
# The headers an OpenAI MCP tool sends are named by the caller: a header
# spelled `id` or `name` holds a value like any other.
add(
    "caller keys: openai mcp tool headers",
    {
        "model": "gpt-4o",
        "input": "hi",
        "tools": [
            {
                "type": "mcp",
                "server_label": "crm",
                "server_url": "https://mcp.example.com/sse",
                "headers": {"id": "marie@example.com", "name": "paul@example.com"},
            }
        ],
    },
    {
        "model": "gpt-4o",
        "input": "hi",
        "tools": [
            {
                "type": "mcp",
                "server_label": "crm",
                "server_url": "https://mcp.example.com/sse",
                "headers": {"id": "[EMAIL_1]", "name": "[EMAIL_2]"},
            }
        ],
    },
    provider="openai",
    counts={"email": 2},
    policy=only("email"),
    note="The headers of an MCP tool are named by the caller: a header spelled id or name holds a value.",
)

# ── large inputs (built from parts by both test suites) ────────────────────
add(
    "large: one megabyte of prose with an address in the middle",
    {
        "$parts": [
            {"repeat": "lorem ipsum dolor sit amet ", "times": 20000},
            "marie@example.com",
            {"repeat": " consectetur adipiscing elit", "times": 18000},
        ]
    },
    {
        "$parts": [
            {"repeat": "lorem ipsum dolor sit amet ", "times": 20000},
            "[EMAIL_1]",
            {"repeat": " consectetur adipiscing elit", "times": 18000},
        ]
    },
    counts={"email": 1},
)
add(
    "large: many addresses",
    {
        "$parts": [
            {"repeat": "write to marie@example.com and 06 12 34 56 78. ", "times": 5000}
        ]
    },
    {"$parts": [{"repeat": "write to [EMAIL_1] and [PHONE_1]. ", "times": 5000}]},
    counts={"email": 5000, "phone": 5000},
)
add(
    "large: accented prose takes the slow path",
    {
        "$parts": [
            {"repeat": "Élève à l'école, café crème. ", "times": 20000},
            "andré@example.com",
        ]
    },
    {
        "$parts": [
            {"repeat": "Élève à l'école, café crème. ", "times": 20000},
            "[EMAIL_1]",
        ]
    },
    counts={"email": 1},
)
add(
    "large: over the size limit is blocked",
    {"$parts": [{"repeat": "a", "times": 8388609}]},
    blocked={"size_limit": 1},
)

# ── challenge (reference policy, each input cleaned on its own) ────────────
CHALLENGE_INPUTS = [
    "Mail marie@example.com and paul@example.com",
    "Card 4111 1111 1111 1111",
    "Nothing to see here.",
    "Card 4111 1111 1111 1111 and marie@example.com",
]
CHALLENGE_OUTPUTS = [
    "Mail [EMAIL_1] and [EMAIL_2]",
    "Card [CARD_1]",
    "Nothing to see here.",
    "Card [CARD_1] and [EMAIL_1]",
]
TOKEN = "chl_example_token_0001"
CHALLENGES = [
    {
        "token": TOKEN,
        "inputs": CHALLENGE_INPUTS,
        "outputs": CHALLENGE_OUTPUTS,
        "response": "sha256:"
        + hashlib.sha256(
            (TOKEN + "\n" + "\n".join(CHALLENGE_OUTPUTS)).encode("utf-8")
        ).hexdigest(),
    }
]


def jcs(value: Any) -> str:
    """JCS for the shapes used here: string keys, no floats."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


DIGESTS = [
    {
        "policy": POLICIES[name],
        "digest": "sha256:"
        + hashlib.sha256(jcs(POLICIES[name]).encode("utf-8")).hexdigest(),
    }
    for name in ("reference", "custom", "off")
]

DOCUMENT = {
    "description": (
        "Prompt-cleaning vectors shared by the Python and Node SDKs. Generated by "
        "packages/descriptors/build_content_vectors.py; do not edit by hand."
    ),
    "engine": RULES["engine"],
    "definitions": {
        "case": (
            "Clean `input` under `policy` (a name in `policies`, or the policy itself) "
            "with the content block of the `provider` descriptor (none when null). "
            "Expect `output`, exactly `counts` (masked "
            "occurrences per detector id or custom:LABEL), exactly `blocked` (when not "
            "empty, the call is blocked and no output is stated) and `attachments`. "
            "`error: policy` means the policy itself must be refused. Every output must "
            "be a fixed point: cleaning it again changes nothing and counts nothing."
        ),
        "$concat": (
            "Anywhere in this file, an object whose only key is `$concat` stands for "
            "the string made by joining its parts, where a part {repeat, times} is "
            "`repeat` written `times` times. Secret-shaped strings are stored split "
            "this way so that no tracked file holds a whole key or token."
        ),
        "walk": "Object keys are visited in RFC 8785 order (UTF-16 code units), arrays in order; placeholders are numbered in that order, then by position.",
        "challenge": 'response = sha256(token + "\\n" + the cleaned inputs joined by "\\n"), each input cleaned on its own under the reference policy.',
        "policy_digest": "sha256 of the RFC 8785 canonical form of the policy document.",
        "positives": (
            "A value one catalogue detector takes whole, between `lead` and `trail` "
            "(the words a detector may need nearby). Both suites put each in "
            "generated contexts (prefixes, line prefixes, escaped newlines, suffixes, "
            "values lengthened past the engine's bounds, a private key cut before its "
            "END line) under a policy enabling that detector alone, and hold that "
            "whenever the detector counts something, no run of 4 characters of the "
            "value is left in the output: a value is taken whole or not at all."
        ),
    },
    "policies": POLICIES,
    "cases": CASES,
    "challenges": CHALLENGES,
    "policy_digests": DIGESTS,
    "positives": POSITIVES,
}


def render() -> str:
    document = dict(DOCUMENT)
    for key in ("policies", "cases", "challenges", "policy_digests", "positives"):
        document[key] = _split_secrets(DOCUMENT[key])
    return json.dumps(document, indent=1, ensure_ascii=True) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {len(CASES)} cases to {OUT.relative_to(HERE.parent.parent)}")
