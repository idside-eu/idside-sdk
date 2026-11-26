"""The prompt-cleaning engine beyond the shared vectors: validators, limits, digests.

The vectors (`test_content_vectors.py`) say what cleaning does to a body. The
tests here hold what they cannot carry: each validator's arms one by one, the
shadow's offset map, the engine's limits, the digests, and linear time on
adversarial input. Identifiers below are fictitious; check digits are computed
for numbers nobody holds.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from idside import _cleaning
from idside._cleaning import (
    CATALOGUE_IDS,
    ContentCleaner,
    build_digest,
    catalogue_problem,
    challenge_response,
    clean,
    content_fields,
    content_policy_digest,
    reference_policy,
    rules_digest,
    verify_clean,
)
from idside._cleaning import engine as engine_module
from idside._cleaning import rules as rules_module
from idside._cleaning import validators as v
from idside._cleaning.digests import build_digest_of, challenge_digest
from idside._cleaning.shadow import fold, map_code_point, shadow_of
from idside.exceptions import ContentPolicyError, IDSideError

_RULES_FILE = Path(rules_module.__file__).resolve().parent.parent / "content-rules.json"
REFERENCE = reference_policy()


# ── validators ─────────────────────────────────────────────────────────────


def test_luhn_reference_numbers() -> None:
    assert v.luhn("79927398713")
    assert not v.luhn("79927398710")
    assert v.luhn("0")


@pytest.mark.parametrize(
    ("number", "ok"),
    [
        ("4111111111111111", True),
        ("4111111111111112", False),  # Luhn
        ("4222222222222", True),  # Visa 13
        ("411111111111116", False),  # Visa 15 is not a Visa length
        ("5555555555554444", True),
        ("2221000000000009", True),  # Mastercard 2-series, low bound
        ("2720999999999996", True),  # high bound
        ("2721000000000004", False),  # just past it
        ("378282246310005", True),  # Amex 15
        ("3782822463100050", False),  # Amex 16
        ("6011111111111117", True),
        ("9111111111111113", False),  # no network
    ],
)
def test_card(number: str, ok: bool) -> None:
    assert v.luhn(number) or not ok
    assert (v.card(number) is not None) is ok


def test_card_reads_digits_through_separators() -> None:
    assert v.card("4111 1111 1111 1111") == len("4111 1111 1111 1111")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("FR7630006000011234567890189", 27),
        ("fr7630006000011234567890189", 27),
        ("FR76 3000 6000 0112 3456 7890 189", 33),
        ("BE68 5390 0754 7034 ABCD", 19),  # cut at the country length
        ("BE68539007547034ABCD", None),  # glued: not cut  # pragma: allowlist secret
        ("FR7630006000011234567890188", None),  # mod 97
        ("XX7630006000011234567890189", None),  # no such country
        ("FR763000600001123456789", None),  # too short
        ("FR293000600001123456", None),  # passes mod 97, but short for FR
    ],
)
def test_iban(value: str, expected: int | None) -> None:
    assert v.iban(value) == expected


def test_bic_country() -> None:
    assert v.bic("EXMPFRPPXXX") == 11
    assert v.bic("EXMPXXPP") is None


def _nir(first13: str) -> str:
    body = first13.replace("2A", "19").replace("2B", "18")
    return first13 + f"{97 - int(body) % 97:02d}"


@pytest.mark.parametrize(
    "first13",
    ["1850578006084", "269052A123456", "178122B045012"],  # pragma: allowlist secret
)
def test_nir_key(first13: str) -> None:
    good = _nir(first13)
    assert v.nir(good) == 15
    assert v.nir(good[:-2] + f"{(int(good[-2:]) % 97) + 1:02d}") is None


def test_nir_corsica_substitution_is_not_symmetric() -> None:
    # 2A reads 19 and 2B reads 18: swapping them must break the key.
    corsica = _nir("269052A123456")  # pragma: allowlist secret
    assert v.nir(corsica.replace("2A", "2B")) is None


def test_nir_refuses_shape_errors() -> None:
    assert v.nir("1 85 05 78 006 084") is None  # 13 characters
    assert v.nir("185052C00608400") is None  # not a department
    assert v.nir("18505780060840A") is None  # letter in the key
    assert v.nir("185057800606909") == 15  # key 09
    assert v.nir("18505780060699") is None  # the same, written with key 9


def test_siren_and_siret() -> None:
    assert v.siren("123456782") == 9
    assert v.siren("123 456 782") == 11
    assert v.siren("123456783") is None
    assert v.siren("12345678") is None
    assert v.siret("12345678200010") == 14
    assert v.siret("12345678200011") is None
    assert v.siret("12345678300010") is None  # SIREN part fails
    assert v.luhn("12345678010005") and v.siret("12345678010005") is None


def test_siret_la_poste_rule() -> None:
    assert v.siret("35600000009075") == 14  # digit sum 30, not Luhn
    assert not v.luhn("35600000009075")
    assert v.siret("35600000049838") is None  # digit sum 46
    assert v.siret("35600000000048") == 14  # head office: Luhn
    assert v.siret("35600000000049") is None  # the head office is held to Luhn


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("FR11123456782", True),  # (123456782 * 100 + 12) % 97 == 11
        ("FR 11 123 456 782", True),
        ("FR12123456782", False),
        ("FR11123456783", False),  # SIREN fails Luhn
        ("FRA0123456782", True),  # letter first
        ("FR0L123456782", True),  # digit first, then a letter
        ("FR1M123456782", True),  # a non-zero digit first weighs 24
        ("FRA1123456782", False),
        ("FR53000004605", True),  # Monaco: no SIREN check
        ("FR1112345678", False),  # short
    ],
)
def test_fr_vat(value: str, ok: bool) -> None:
    assert (v.fr_vat(value) is not None) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("192.168.1.10", True),
        # A prefix is not the validator's: the engine takes it with the address.
        ("10.0.0.0/24", False),
        ("10.0.0.0/33", False),
        ("10.0.0.0/", False),
        ("256.1.1.1", False),
        ("1.2.3", False),
        ("2001:db8::1", True),
        ("::1", False),  # loopback, and a slice of code: no group of 3+ digits
        ("1::2", False),
        ("fe80::1", True),
        ("abc::1", True),
        ("::", False),  # no digit: prose, not an address
        ("abc::def", False),
        ("1:2:3:4:5:6:7:8", True),
        ("1:2:3:4:5:6:7", False),
        ("1:2:3:4:5:6:7::8", False),
        ("2001::db8::1", False),
        ("2001:db8:12345::1", False),
        ("::ffff:192.0.2.128", True),
        ("::ffff:192.0.2.256", False),
        ("1:2:3:4:5:6:1.2.3.4", True),
        ("1:2:3:4:5:6:7:1.2.3.4", False),
    ],
)
def test_ip(value: str, ok: bool) -> None:
    assert (v.ip(value) is not None) is ok


def test_phone_counts_digits_of_international_forms() -> None:
    assert v.phone("06 12 34 56 78") == 14
    assert v.phone("+12 345") is None
    assert v.phone("+1 415 555 0123") == 15
    assert v.phone("0033 6 12 34 56 78") == 18
    assert v.phone("00336123") is None  # six digits after 00
    assert v.phone("+1234567890123456") is None  # sixteen digits
    assert v.phone("+12 500 000") is None  # eight digits: an amount
    assert v.phone("+12 500 0001") == 12  # nine digits
    assert v.phone("0012 500 0001") == 13
    assert v.phone("0012 500 000") is None
    assert v.phone("(+33) 6 12 34 56 78") == 19


def test_plate() -> None:
    assert v.plate("AB-123-CD") == 9
    assert v.plate("AB-000-CD") is None
    assert v.plate("AB-123-CD-69") == 12
    assert v.plate("AB-000-CD-69") is None  # the department is not the plate
    assert v.plate("AB-001-CD") == 9


def test_iban_reads_spaces_and_dashes_as_separators() -> None:
    assert v.iban("FR76-3000-6000-0112-3456-7890-189") == 33
    assert v.iban("FR 76 3000 6000 0112 3456 7890 189") == 34
    assert v.iban("BE68-5390-0754-7034-ABCD") == 19  # cut at the country length
    assert v.iban("BE68-5390-0754-7034ABCD") is None  # glued: not cut


def test_nir_reads_dashes_as_separators() -> None:
    assert v.nir("1-85-05-78-006-084-91") == 21
    assert v.nir("1-85-05-78-006-084-92") is None


# ── shadow ─────────────────────────────────────────────────────────────────


def test_shadow_of_ascii_shares_offsets() -> None:
    shadow = shadow_of("Hello WORLD")
    assert shadow.text == "Hello WORLD"
    assert shadow.lower == "hello world"
    assert (shadow.start(3), shadow.end(5)) == (3, 5)


def test_shadow_maps_a_ligature_back_to_one_character() -> None:
    shadow = shadow_of("a\ufb01b\u200bc")
    assert shadow.text == "afibc"
    assert [shadow.start(index) for index in range(5)] == [0, 1, 1, 2, 4]
    assert [shadow.end(end) for end in range(1, 6)] == [1, 2, 2, 3, 5]


def test_shadow_aligned_non_ascii_shares_offsets() -> None:
    shadow = shadow_of("Élève \U0001f600")
    assert shadow.text == "Eleve \ufffd"
    assert shadow.lower == "eleve \ufffd"
    assert shadow.start(6) == 6


def test_shadow_lowers_ascii_only() -> None:
    assert fold("ÉCOLE Ωmega ẞ") == "ecole Ωmega ẞ"


def test_code_point_mapping() -> None:
    assert map_code_point(ord("\u200b")) == ""
    assert map_code_point(ord("é")) == "e"
    assert map_code_point(0x2013) == "-"
    assert map_code_point(0x2019) == "'"
    assert map_code_point(0x1F600) == "\ufffd"
    assert map_code_point(0xFF4D) == "m"
    # NFKD yields an astral code point here (mathematical bold A decomposes to
    # A, which is not astral); the combining mark of a precomposed letter is
    # dropped whatever block it is in.
    assert map_code_point(0x1D400) == "A"
    assert map_code_point(ord("ǖ")) == "u"


def test_resolution_judges_overlap_on_original_characters() -> None:
    # Two shadow spans ("f" and "i") that come from one original ligature
    # overlap in the original: only the first by order survives.
    shadow = shadow_of("\ufb01")
    first = engine_module._Candidate(0, 1, _rule("custom:A", 0))
    second = engine_module._Candidate(1, 2, _rule("custom:B", 1))
    kept = engine_module._resolve([second, first], shadow)
    assert [candidate.rule.key for candidate in kept] == ["custom:A"]


def test_an_overlap_is_absorbed_under_the_best_ranked_candidate() -> None:
    # A short candidate bridging two kept ones joins them into one span, under
    # the one ranked first (the longest), whichever side it stands on.
    shadow = shadow_of("x" * 30)
    left = engine_module._Candidate(0, 8, _rule("custom:LEFT", 0))
    right = engine_module._Candidate(10, 30, _rule("custom:RIGHT", 1))
    bridge = engine_module._Candidate(7, 11, _rule("custom:BRIDGE", 2))
    kept = engine_module._resolve([bridge, left, right], shadow)
    assert [(c.start, c.end, c.rule.key) for c in kept] == [(0, 30, "custom:RIGHT")]
    # Disjoint candidates stay apart, each its own.
    apart = engine_module._resolve([left, right], shadow)
    assert [(c.start, c.end) for c in apart] == [(0, 8), (10, 30)]
    assert apart[0] is left


def _rule(key: str, order: int) -> Any:
    from idside._cleaning.policy import Rule

    return Rule(key=key, label="X", action="mask", order=order, patterns=(),
                validator=None, lowered=False)  # fmt: skip


# ── policy errors and the public helpers ───────────────────────────────────


def test_policy_error_is_an_idside_error_and_names_no_value() -> None:
    entry = {"kind": "term", "value": "Opération Nordlys", "action": "mask"}
    policy = {"schema": 1, "custom": [dict(entry, label="bad label")]}
    with pytest.raises(ContentPolicyError) as raised:
        ContentCleaner(policy)
    assert isinstance(raised.value, IDSideError)
    assert "Nordlys" not in str(raised.value)
    assert "custom entry 0" in str(raised.value)


@pytest.mark.parametrize(
    "policy",
    [None, [], "x", {"schema": True}, {"schema": 2}, {"schema": 1, "detectors": None}],
)
def test_policy_that_is_not_a_schema_1_object_is_refused(policy: Any) -> None:
    with pytest.raises(ContentPolicyError):
        ContentCleaner(policy)


def test_value_length_counts_code_points() -> None:
    entry = {"kind": "term", "action": "mask", "label": "A"}
    ContentCleaner({"schema": 1, "custom": [dict(entry, value="\U0001f600" * 200)]})
    with pytest.raises(ContentPolicyError):
        ContentCleaner({"schema": 1, "custom": [dict(entry, value="\U0001f600" * 201)]})


def test_pattern_and_domain_normalisation() -> None:
    policy = {"schema": 1, "custom": [
        {"kind": "domain", "value": "*.Acme.Example.", "action": "mask", "label": "D"},
        {"kind": "pattern", "value": "N°#\u200b#", "action": "mask", "label": "P"},
        {"kind": "pattern", "value": "#-x", "action": "block", "label": "Q"},
        {"kind": "pattern", "value": "V2.#", "action": "mask", "label": "V"},
    ]}  # fmt: skip
    cleaner = ContentCleaner(policy)
    assert cleaner.clean("mail@ACME.example N°12").payload == "[D_1] [P_1]"
    assert cleaner.clean("ref 7-X").blocked == {"custom:Q": 1}
    assert cleaner.clean("v2.3 and V2.33").payload == "[V_1] and V2.33"
    with pytest.raises(ContentPolicyError):
        invisible = {
            "kind": "pattern",
            "value": "\u200b",
            "action": "mask",
            "label": "P",
        }
        ContentCleaner({"schema": 1, "custom": [invisible]})


def test_payload_must_be_json() -> None:
    with pytest.raises(ValueError, match="JSON"):
        clean({"a": b"bytes"}, REFERENCE, provider=None)
    with pytest.raises(ValueError, match="non-string key"):
        clean({1: "x"}, REFERENCE, provider=None)


def test_tuples_are_read_as_arrays() -> None:
    result = clean(("marie@example.com", "x"), REFERENCE, provider=None)
    assert result.payload == ["[EMAIL_1]", "x"]


def test_untouched_branches_are_the_same_objects() -> None:
    untouched = {"deep": ["a", "b"]}
    body = {
        "model": "m",
        "keep": untouched,
        "messages": [{"content": "marie@example.com"}],
    }
    result = clean(body, REFERENCE, provider="openai")
    assert result.payload["keep"] is untouched
    assert body["messages"][0]["content"] == "marie@example.com"  # not mutated
    assert clean(untouched, REFERENCE, provider="openai").payload is untouched


def test_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        content_fields("nope")


def test_verify_clean() -> None:
    assert verify_clean("hello", REFERENCE, provider=None)
    assert not verify_clean("marie@example.com", REFERENCE, provider=None)
    assert verify_clean("[EMAIL_1]", REFERENCE, provider=None)
    block = {"schema": 1, "detectors": {"credit_card": "block"}}
    assert not verify_clean("4111111111111111", block, provider=None)


def test_clean_accepts_a_policy_that_is_not_json_only_to_refuse_it() -> None:
    with pytest.raises(ContentPolicyError):
        clean("x", {"schema": 1, "detectors": {"email": float("nan")}}, provider=None)


def test_pass_limit_blocks_instead_of_leaving_a_non_fixed_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "SIREN " + "a" * 60 + "@example.com 123456782"
    assert clean(text, REFERENCE, provider=None).counts == {"email": 1, "fr_siren": 1}
    monkeypatch.setitem(engine_module.LIMITS, "max_passes", 2)
    result = ContentCleaner(REFERENCE).clean(text)
    assert result.blocked == {"size_limit": 1}


def test_a_result_never_shows_the_body_in_its_repr() -> None:
    # A blocked call's result still holds the body as it came in: printed in a
    # traceback or a debugger, its repr must not spell out what was blocked.
    block = {"schema": 1, "detectors": {"credit_card": "block", "email": "mask"}}
    for text in ("card 4111111111111111", "mail marie@example.com"):
        result = ContentCleaner(block).clean({"content": text})
        assert "4111" not in repr(result)
        assert "marie" not in repr(result)
        assert "EMAIL" not in repr(result)
    assert "credit_card" in repr(ContentCleaner(block).clean("4111111111111111"))


def test_reference_policy_masks_every_detector() -> None:
    assert reference_policy()["detectors"] == dict.fromkeys(CATALOGUE_IDS, "mask")
    assert len(CATALOGUE_IDS) == 34


def test_catalogue_is_for_this_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    assert catalogue_problem() is None
    monkeypatch.setitem(rules_module.RULES, "engine", "2")
    assert "engine '2'" in (catalogue_problem() or "")


# ── digests ────────────────────────────────────────────────────────────────


def test_rules_digest_is_the_file_bytes() -> None:
    expected = "sha256:" + hashlib.sha256(_RULES_FILE.read_bytes()).hexdigest()
    assert rules_digest() == expected


def test_build_digest_frames_each_file() -> None:
    assert build_digest_of([]) == "sha256:" + hashlib.sha256(b"").hexdigest()
    one = build_digest_of([("a", b"bc")])
    assert one == "sha256:" + hashlib.sha256(b"a\x002\x00bc").hexdigest()
    assert one != build_digest_of([("ab", b"c")])
    assert build_digest_of([("b", b"1"), ("a", b"2")]) == build_digest_of(
        [("a", b"2"), ("b", b"1")]
    )


def test_build_digest_covers_the_cleaning_code_and_the_descriptors() -> None:
    package = Path(_cleaning.__file__).resolve().parent.parent
    files = [
        (f"idside/{folder}/{path.name}", path.read_bytes())
        for folder, pattern in (("_cleaning", "*.py"), ("providers", "*.json"))
        for path in (package / folder).glob(pattern)
    ]
    assert {name for name, _ in files} >= {
        "idside/_cleaning/engine.py", "idside/_cleaning/validators.py",
        "idside/providers/openai.json", "idside/providers/gemini.json",
    }  # fmt: skip
    assert build_digest() == build_digest_of(files)


def test_build_digest_of_an_unreadable_package_is_the_empty_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from idside._cleaning import digests

    class Broken:
        def glob(self, _pattern: str) -> Any:
            raise OSError("frozen")

    monkeypatch.setattr(digests, "_PACKAGE", Broken())
    digests.build_digest.cache_clear()
    try:
        assert digests.build_digest() == build_digest_of([])
    finally:
        digests.build_digest.cache_clear()


def test_policy_digest_is_jcs() -> None:
    policy = {"schema": 1, "b": "é", "a": [1, 2]}
    expected = hashlib.sha256('{"a":[1,2],"b":"é","schema":1}'.encode()).hexdigest()
    assert content_policy_digest(policy) == "sha256:" + expected
    with pytest.raises(ValueError):
        content_policy_digest({"x": float("nan")})


def test_challenge_response() -> None:
    expected = challenge_digest("tok", ["[EMAIL_1]", "plain"])
    assert challenge_response("tok", ["marie@example.com", "plain"]) == expected
    assert expected == "sha256:" + hashlib.sha256(b"tok\n[EMAIL_1]\nplain").hexdigest()
    with pytest.raises(ValueError):
        challenge_response("tok", ["x", 1])  # type: ignore[list-item]


# ── linear time ────────────────────────────────────────────────────────────

#: Written in two pieces: no tracked file holds a private-key marker whole.
_PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
_ADVERSARIAL: dict[str, Callable[[int], str]] = {
    "letters": lambda n: "a" * n,
    "digits": lambda n: "1" * n,
    "digit groups": lambda n: "1111 " * (n // 5),
    "at signs": lambda n: "a@" * (n // 2),
    "dotted labels": lambda n: "a." * (n // 2),
    "email chain": lambda n: "a@a." * (n // 4),
    "colons": lambda n: "0:" * (n // 2),
    "hex groups": lambda n: "ab:" * (n // 3),
    "pem headers": lambda n: _PEM_BEGIN * (n // 27),
    "pem body": lambda n: _PEM_BEGIN + "A" * n,
    "key prefixes": lambda n: "sk-" * (n // 3),
    "jwt parts": lambda n: "eyJ" + "a." * (n // 2),
    "assignments": lambda n: "password=" * (n // 9),
    "urls": lambda n: "https://" * (n // 8),
    "connection": lambda n: "postgres://a:" * (n // 13),
    "plus groups": lambda n: "+33 " * (n // 4),
    "iban letters": lambda n: "FR76 ABCD " * (n // 10),
    "placeholder-ish": lambda n: "[A_1" * (n // 4),
    "keywords": lambda n: "siren 123 " * (n // 10),
    "accents": lambda n: "é1" * (n // 2),
    "invisible": lambda n: "a\u200b" * (n // 2),
    # Shapes of the rules widened after the end-to-end review.
    "pem escaped lines": lambda n: _PEM_BEGIN + ("\\n" + "A" * 62) * (n // 64),
    "pem quoted lines": lambda n: _PEM_BEGIN + ("\n> " + "A" * 61) * (n // 64),
    "pem dashes": lambda n: _PEM_BEGIN + "----a" * (n // 5),
    "pem short parts": lambda n: _PEM_BEGIN + (" " + "A" * 15) * (n // 16),
    "version words": lambda n: "version " * (n // 8),
    "version chains": lambda n: "version 1.2.3." * (n // 14),
    "open quotes": lambda n: ('password: "' + "a" * 2000) * (n // 2011),
    "escaped quotes": lambda n: 'password: "' + '\\"' * (n // 2),
    "bearer words": lambda n: ("Bearer " + "a" * 5000) * (n // 5007),
    "double-spaced digits": lambda n: "1111  " * (n // 6),
    "iban dashes": lambda n: "FR 76-" * (n // 6),
    "cyrillic at signs": lambda n: "\u044f@" * (n // 2),
    # Shapes of the second pass: a key cut before its END line, quoted
    # headers, passwords holding @, values past every bound.
    "pem cut then prose": lambda n: (
        _PEM_BEGIN + "\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC" + " lorem" * (n // 6)
    ),
    "pem quoted headers": lambda n: (_PEM_BEGIN + " lorem ipsum ") * (n // 40),
    "at chains": lambda n: "postgres://u:" + "p@" * (n // 2),
    "long assignment": lambda n: "password=" + "a" * n,
    "plus amounts": lambda n: "+150 000 000 " * (n // 13),
    "pem quoted lines with an end": lambda n: (
        _PEM_BEGIN
        + ("\n> " + "A" * 61) * (n // 64)
        + "\n> -----END "
        + "PRIVATE KEY-----"
    ),
    "pem one long line with an end": lambda n: (
        _PEM_BEGIN + "\n" + "A" * n + "\n-----END " + "PRIVATE KEY-----"
    ),
    # Shapes of the third pass: values followed by the engine to their end,
    # escapes written in two characters, readings that back off.
    "slack prefixes": lambda n: "xoxb-" * (n // 5),
    "escapes": lambda n: "\\n" * (n // 2),
    "escaped addresses": lambda n: "\\na@b.co " * (n // 9),
    "later at signs": lambda n: "postgres://u:p@" + "a@" * (n // 2),
    "long parts between at signs": lambda n: ("redis://:p@" + "a" * 5000) * (n // 5011),
    "domain labels run on": lambda n: "a@b.co" + ".x" * (n // 2),
    "ipv6 runs": lambda n: "1:2:3:4:5:6:7:8:" * (n // 16),
    "glued numbers": lambda n: "+33 6 12 34 56 78a " * (n // 19),
    "pem cut keys": lambda n: (_PEM_BEGIN + "\n" + "A" * 40 + "\n") * (n // 69),
    # Shapes of the fourth pass: header lines and blanks after a BEGIN line,
    # two short runs, escapes in tokens, glued passwords, // credentials.
    "pem header lines and blanks": lambda n: (
        (_PEM_BEGIN + ("\nComment: x" + " " * 512) * 4 + "\n") * (n // 2100)
    ),
    "pem header words": lambda n: (
        (_PEM_BEGIN + ("\nComment: " + "a b " * 60) * 4 + "\n") * (n // 1030)
    ),
    "pem two runs and blanks": lambda n: (
        (_PEM_BEGIN + " " + "A" * 20 + " " * 1000 + "B" * 11 + "\n") * (n // 1060)
    ),
    "url escapes": lambda n: "xoxd-" + "%2F" * (n // 3),
    "glued passwords": lambda n: ("m@example.com:" + "x" * 30 + " ") * (n // 45),
    "schemeless credentials": lambda n: "//a:b@ " * (n // 7),
}

_TIMING_POLICY = {
    "schema": 1,
    "detectors": dict.fromkeys(CATALOGUE_IDS, "mask"),
    "custom": [
        {"kind": "term", "value": "projet atlas", "action": "mask", "label": "T"},
        {"kind": "domain", "value": "acme.example", "action": "mask", "label": "D"},
        {"kind": "pattern", "value": "*-*-*-#*-@", "action": "mask", "label": "P"},
    ],
}


def _best_of(cleaner: ContentCleaner, text: str, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        began = time.perf_counter()
        cleaner.clean(text)
        best = min(best, time.perf_counter() - began)
    return best


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_cleaning_time_grows_linearly_on_adversarial_input(name: str) -> None:
    """Four times the input may cost at most eight times the time.

    Linear growth predicts four, a quadratic engine sixteen. Best of three runs,
    so a busy machine does not fail the build.
    """
    cleaner = ContentCleaner(_TIMING_POLICY)
    small, large = _ADVERSARIAL[name](16_000), _ADVERSARIAL[name](64_000)
    floor = 0.002  # below this a timer measures the machine, not the engine
    assert _best_of(cleaner, large) <= 8 * max(_best_of(cleaner, small), floor)


def test_one_megabyte_of_adversarial_input_stays_under_seconds() -> None:
    cleaner = ContentCleaner(_TIMING_POLICY)
    text = "".join(_ADVERSARIAL[name](40_000) for name in sorted(_ADVERSARIAL))
    assert len(text) > 800_000
    assert _best_of(cleaner, text, runs=1) < 20


_MEGABYTE = 1_048_576
_ONE_RULE: dict[str, dict[str, Any]] = {
    **{
        detector: {"schema": 1, "detectors": {detector: "mask"}}
        for detector in CATALOGUE_IDS
    },
    **{
        f"custom {entry['kind']}": {"schema": 1, "custom": [entry]}
        for entry in _TIMING_POLICY["custom"]
    },
}


@pytest.mark.parametrize("rule", sorted(_ONE_RULE))
def test_one_megabyte_of_each_adversarial_input_per_rule(rule: str) -> None:
    """Every rule alone, against a megabyte of each adversarial shape.

    A rule whose pattern backtracks would show here even if the full policy
    hid it behind another; a quadratic one would take hours on a megabyte, so
    a bound of seconds per run leaves a slow machine room and still fails it.

    Twenty seconds, as for the full policy above: the suite runs under line
    tracing for coverage, and on Python 3.11 tracing alone triples the time a
    validator spends on a megabyte of digit groups (5.4 s on a CI runner
    against 1.8 s untraced). The bound measures the engine, not the tracer.
    """
    cleaner = ContentCleaner(_ONE_RULE[rule])
    for name in sorted(_ADVERSARIAL):
        text = _ADVERSARIAL[name](_MEGABYTE)
        assert len(text) > _MEGABYTE * 0.9, name
        began = time.perf_counter()
        cleaner.clean(text)
        elapsed = time.perf_counter() - began
        assert elapsed < 20, f"{rule} on {name}: {elapsed:.2f} s"


def test_vectors_and_rules_agree_on_engine() -> None:
    rules = json.loads(_RULES_FILE.read_text("utf-8"))
    assert rules["engine"] == _cleaning.ENGINE_VERSION


# ── convergence on random input ────────────────────────────────────────────

_FUZZ_TOKENS = [
    "a", "Z", "1", "0", "@", ".", "-", ":", "/", "+", " ", "[", "]", "_", "=", "'",
    '"', "\n", "é", "\u200b", "sk-", "sk_live_", _PEM_BEGIN,
    "-----END " + "PRIVATE KEY-----", "x@y.zz", "4111111111111111", "4111 ", "FR76",
    "3000 ", "06 12 34 56 78", "+33", "siren", "123 456 782", "https://",
    "postgres://u:p@h", "Bearer ", "password=", "eyJ", "[EMAIL_1]", "[A_",
    "10.0.0.1", "::", "ab:", "AB-123-CD", "AKIA", "mistral", "bic", "TESTDEFF",
    "acme.example", "projet atlas", "PRJ-1-2-3a", "fe80::1", "00:1a:2b:3c:4d:5e",
    "GB82WEST12345698765432",
    # Shapes of the rules widened after the end-to-end review.
    "version ", "v", "4.2.1.3", "\\n", "> ", "-----END " + "PRIVATE KEY-----",
    "Authorization: ", "Basic ", "(+33) ", "06/12/", "  ", "\u20ac",
    "\u043c@\u0440\u0444",
    "\u044f", "git@github.com:", "@2x.png", "xapp-1-", "glffct-", "PGPASS" + "WORD=",
    "Password=\x27", "os.environ", "mot de passe : ", "_KEY=", "R.C.S.", "FR 76-",
]  # fmt: skip


@pytest.mark.parametrize(
    "policy", [REFERENCE, _TIMING_POLICY], ids=["reference", "custom"]
)
def test_random_text_converges_and_stays_put(policy: dict[str, Any]) -> None:
    """Cleaning random token soup always converges, and its output is a fixed point."""
    import random

    rng = random.Random(20261116)  # noqa: S311 - reproducible test data
    cleaner = ContentCleaner(policy)
    for _ in range(3000):
        text = "".join(rng.choice(_FUZZ_TOKENS) for _ in range(rng.randint(1, 40)))
        result = cleaner.clean(text)
        assert not result.blocked, repr(text)
        again = cleaner.clean(result.payload)
        assert (again.payload, dict(again.counts)) == (result.payload, {}), repr(text)


# ── patterns that run only under Block (`block_only`) ─────────────────────

_QUOTED_HEADER = (
    "A key file starts with -----BEGIN " + "PRIVATE KEY----- and a dashed line."
)


def test_a_block_only_pattern_runs_only_when_its_detector_blocks() -> None:
    from idside._cleaning.policy import compile_policy

    def patterns(action: str) -> list[bool]:
        compiled = compile_policy({"schema": 1, "detectors": {"private_key": action}})
        (rule,) = compiled.rules
        return [pattern.block_only for pattern in rule.patterns]

    assert patterns("block") == [False, False, True]
    assert patterns("mask") == [False, False]


def test_a_quoted_key_header_is_left_under_mask_and_blocks_under_block() -> None:
    masked = ContentCleaner({"schema": 1, "detectors": {"private_key": "mask"}})
    result = masked.clean(_QUOTED_HEADER)
    assert (result.payload, dict(result.counts), dict(result.blocked)) == (
        _QUOTED_HEADER,
        {},
        {},
    )
    blocking = ContentCleaner({"schema": 1, "detectors": {"private_key": "block"}})
    assert dict(blocking.clean(_QUOTED_HEADER).blocked) == {"private_key": 1}


def test_block_only_is_read_from_the_catalogue_as_written() -> None:
    spec = {"regex": "(a)", "group": 1, "prefilter": []}
    assert rules_module.compile_pattern(spec).block_only is False
    assert rules_module.compile_pattern({**spec, "block_only": True}).block_only
