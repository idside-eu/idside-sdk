"""The prompt-cleaning catalogue: one file, three copies, one portable regex subset.

`packages/descriptors/content-rules.json` is bundled byte-identical in both SDKs
(and copied to the ID Side cloud). Its SHA-256 is the `rules` digest every call
declares, so a copy that drifted would be reported as an unknown engine.

Every pattern is written in a subset Python's `re` and JavaScript's `RegExp`
read alike, and in which matching cannot backtrack without bound: explicit
ASCII classes, bounded quantifiers, repeated groups that always consume, no
lookbehind. The lint below parses each pattern and holds it to that subset; the
timing test in `test_content_engine.py` holds the engine to linear time on
adversarial input.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

_PYTHON_PKG = Path(__file__).resolve().parents[1]
_DESCRIPTORS = _PYTHON_PKG.parent / "descriptors"
_CANONICAL = _DESCRIPTORS / "content-rules.json"
_SCHEMA = _DESCRIPTORS / "schema" / "content-rules.schema.json"
_COPIES = {
    "python": _PYTHON_PKG / "src" / "idside" / "content-rules.json",
    "node": _PYTHON_PKG.parent / "node" / "src" / "content-rules.json",
}
_RULES: dict[str, Any] = json.loads(_CANONICAL.read_text(encoding="utf-8"))
_PATTERNS = [
    (f"{detector['id']}#{index}", pattern)
    for detector in _RULES["detectors"]
    for index, pattern in enumerate(detector["patterns"])
]

#: The ids an organization's policy names and counts are reported under. They
#: are signed into policies and stored in statistics: added, never renamed.
_V1_IDS = [
    "email", "phone", "ip_address", "mac_address", "credit_card", "iban", "bic",
    "fr_nir", "fr_siren", "fr_siret", "fr_vat", "fr_plate", "key_openai",
    "key_anthropic", "key_google", "key_mistral", "key_deepseek", "key_idside",
    "key_aws", "key_github", "key_gitlab", "key_slack", "key_stripe", "key_twilio",
    "key_sendgrid", "key_npm", "key_pypi", "key_azure", "jwt", "bearer_token",
    "private_key", "connection_string_password", "url_credentials", "generic_secret",
]  # fmt: skip


@pytest.mark.parametrize("sdk", sorted(_COPIES))
def test_bundled_rules_are_byte_identical(sdk: str) -> None:
    assert _COPIES[sdk].read_bytes() == _CANONICAL.read_bytes(), (
        f"the {sdk} copy of content-rules.json diverged from "
        "packages/descriptors/content-rules.json; copy the canonical file over it."
    )


def test_rules_validate_against_their_schema() -> None:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(_RULES)


def test_schema_refuses_an_unknown_field() -> None:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    rules = json.loads(_CANONICAL.read_text(encoding="utf-8"))
    rules["detectors"][0]["regex_flags"] = "s"
    assert not Draft202012Validator(schema).is_valid(rules)


def test_v1_ids_are_all_present_in_catalogue_order() -> None:
    ids = [detector["id"] for detector in _RULES["detectors"]]
    assert ids[: len(_V1_IDS)] == _V1_IDS
    assert len(ids) == len(set(ids))


def test_every_group_is_declared_and_only_generic_secret_is_off_by_default() -> None:
    groups = {group["id"] for group in _RULES["groups"]}
    assert {detector["group"] for detector in _RULES["detectors"]} <= groups
    off = [d["id"] for d in _RULES["detectors"] if not d["default_enabled"]]
    assert off == ["generic_secret"]


def test_tables_hold_what_the_validators_read() -> None:
    lengths = _RULES["tables"]["iban_lengths"]
    assert lengths["FR"] == 27 and lengths["DE"] == 22 and lengths["NO"] == 15
    assert set(lengths) <= set(_RULES["tables"]["country_codes"])
    assert "XK" in _RULES["tables"]["country_codes"]


# ── The portable subset ────────────────────────────────────────────────────

_ESCAPES = set(".-+()[]/\\?*{}|^$\"'") | {"t", "r", "n"}
_FORBIDDEN_ESCAPES = set("bBdDwWsSAZzpPkK")


class _Lint:
    """A small recursive-descent reader of the subset; computes minimum lengths."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.at = 0
        self.groups: list[str] = []  # capture-group bodies, by number
        self.backrefs: list[int] = []
        self.repeat = 1  # the most repetitions nested in what was read
        self.low = 1  # the low bound of the quantifier just read

    def fail(self, why: str) -> None:
        raise AssertionError(f"{why} at {self.at} in {self.source!r}")

    def peek(self) -> str:
        return self.source[self.at] if self.at < len(self.source) else ""

    def alternation(self) -> int:
        lengths = [self.sequence()]
        while self.peek() == "|":
            self.at += 1
            lengths.append(self.sequence())
        return min(lengths)

    def sequence(self) -> int:
        total = 0
        while self.peek() not in ("", "|", ")"):
            total += self.quantified()
        return total

    #: The most times nested quantifiers may repeat what they hold: a group
    #: repeated M times around a run of N repeats N x M, and JavaScript's
    #: matcher keeps a frame per repetition: past this, a long enough text
    #: overflows its stack. A value that runs on is the engine's (`extend`).
    MAX_REPEAT = 65536

    def quantified(self) -> int:
        start = self.at
        outer = self.repeat
        self.repeat = 1
        atom_min, repeatable = self.atom()
        inner = self.repeat
        self.repeat = outer
        high = self.bound(atom_min, repeatable, start)
        if high * inner > self.MAX_REPEAT:
            self.fail(f"nested repetition of {high * inner}")
        self.repeat = max(self.repeat, high * inner)
        return atom_min * self.low

    def bound(self, atom_min: int, repeatable: bool, start: int) -> int:
        """The quantifier after an atom: its high bound (1 without one)."""
        self.low = 1
        char = self.peek()
        if char in ("*", "+"):
            self.fail("unbounded quantifier")
        if char == "?":
            self.at += 1
            if self.peek() == "?":
                self.fail("lazy quantifier")
            self.low = 0
            return 1
        if char == "{":
            match = re.match(r"\{(\d+)(?:,(\d+))?\}", self.source[self.at :])
            if not match:
                self.fail("unbounded or malformed {}")
            assert match is not None
            low = int(match.group(1))
            high = int(match.group(2)) if match.group(2) else low
            if high < low or high > 65536:
                self.fail("bad bounds")
            self.at += match.end()
            if self.peek() == "?":
                self.fail("lazy quantifier")
            if repeatable and atom_min == 0 and high > 1:
                repeated = self.source[start : self.at]
                self.fail(f"repeated group that can match empty ({repeated})")
            self.low = low
            return high
        return 1

    def atom(self) -> tuple[int, bool]:
        char = self.peek()
        if char == "(":
            return self.group()
        if char == "[":
            self.char_class()
            return 1, False
        if char == "\\":
            self.at += 1
            escaped = self.peek()
            if escaped in _FORBIDDEN_ESCAPES:
                self.fail(
                    f"escape \\{escaped} reads differently in Python and JavaScript"
                )
            if escaped.isdigit():
                self.backrefs.append(int(escaped))
                self.at += 1
                return 0, False
            if escaped == "u":
                if not re.match(r"u[0-9a-f]{4}", self.source[self.at :]):
                    self.fail("bad \\u escape")
                self.at += 5
                return 1, False
            if escaped not in _ESCAPES:
                self.fail(f"escape \\{escaped} outside the subset")
            self.at += 1
            return 1, False
        if char == ".":
            self.fail("`.` differs on newlines between engines")
        if char in ("^", "$"):
            self.fail("anchor outside `(?:^|...)`")
        self.at += 1
        return 1, False

    def group(self) -> tuple[int, bool]:
        self.at += 1  # "("
        rest = self.source[self.at :]
        if rest.startswith("?:^|"):
            self.at += 4
            body_min = min(0, self.alternation())
            kind = "boundary"
        elif rest.startswith("?:"):
            self.at += 2
            body_min = self.alternation()
            kind = "group"
        elif rest.startswith(("?=", "?!")):
            self.at += 2
            self.alternation()
            body_min = 0
            kind = "lookahead"
        elif rest.startswith("?"):
            self.fail("lookbehind, named group or inline flag")
            raise AssertionError  # pragma: unreachable
        else:
            index = len(self.groups)
            self.groups.append("")
            body_start = self.at
            body_min = self.alternation()
            self.groups[index] = self.source[body_start : self.at]
            kind = "capture"
        if self.peek() != ")":
            self.fail("unclosed group")
        self.at += 1
        return body_min, kind in ("group", "capture")

    def char_class(self) -> None:
        self.at += 1
        if self.peek() == "^":
            self.at += 1
        first = True
        while True:
            char = self.peek()
            if char == "":
                self.fail("unclosed class")
            if char == "]" and not first:
                self.at += 1
                return
            first = False
            if char == "\\":
                self.at += 1
                escaped = self.peek()
                if escaped in _FORBIDDEN_ESCAPES:
                    self.fail(f"class escape \\{escaped}")
                if escaped == "u":
                    self.at += 5
                    continue
                if escaped not in _ESCAPES:
                    self.fail(f"class escape \\{escaped} outside the subset")
            elif char == "[":
                self.fail("nested class syntax")
            self.at += 1


def _lint(source: str) -> _Lint:
    lint = _Lint(source)
    lint.alternation()
    if lint.at != len(source):
        lint.fail("unbalanced parenthesis")
    for number in lint.backrefs:
        body = lint.groups[number - 1]
        # One class, optional or repeated at most twice: a separator such as
        # one or two spaces, written the same way between every group.
        if not re.fullmatch(r"\[[^\]]+\](?:\?|\{[0-2],[12]\})?", body):
            lint.fail(f"backreference to a group that is not one class: {body!r}")
    return lint


@pytest.mark.parametrize(("name", "pattern"), _PATTERNS, ids=[n for n, _ in _PATTERNS])
def test_pattern_is_in_the_portable_subset(name: str, pattern: dict[str, Any]) -> None:
    lint = _lint(pattern["regex"])
    assert len(lint.groups) >= pattern["group"], (
        f"{name}: no capture group {pattern['group']}"
    )
    value = lint.groups[pattern["group"] - 1]
    value_lint = _Lint(value)
    assert value_lint.alternation() > 0, f"{name}: the value group can match empty"
    if "extend" in pattern:
        # Applied again and again where a value ends: each step must take a
        # character, or the engine would stop there.
        _lint(pattern["extend"])
        assert _Lint(pattern["extend"]).alternation() > 0, f"{name}: empty extend"


@pytest.mark.parametrize(("name", "pattern"), _PATTERNS, ids=[n for n, _ in _PATTERNS])
def test_every_boundary_reads_an_escape_written_in_two_characters(
    name: str, pattern: dict[str, Any]
) -> None:
    regex = pattern["regex"]
    for match in re.finditer(re.escape("(?:^|"), regex):
        assert regex.startswith("\\\\[nrt]|", match.end()), (
            f"{name}: a boundary at {match.start()} does not accept \\n, \\r or "
            "\\t written in two characters before the value"
        )


def test_placeholder_pattern_is_in_the_subset() -> None:
    _lint(_RULES["placeholder"]["regex"])


@pytest.mark.parametrize(
    "bad",
    [
        r"a.b",
        r"\d{3}",
        r"\bword",
        r"\s+",
        r"a*",
        r"a+",
        r"a{2,}",
        r"(?<=x)a",
        r"(?P<n>a)",
        r"(?i)a",
        r"(?:a?){1,9}",
        r"(?:x|){2,3}",
        r"a$",
        r"^a",
        r"([a-z]{2})\1",
        r"([ .-]{1,3})[0-9]\1",
        r"a{1,3}?",
        r"[\w]",
        r"(a",
        r"(?:[a-z]{1,65536}){1,128}",
        r"(?:(?:ab){1,300}c){1,300}",
    ],
)
def test_lint_refuses_what_is_outside_the_subset(bad: str) -> None:
    with pytest.raises(AssertionError):
        _lint(bad)


def test_lint_accepts_the_shapes_the_catalogue_uses() -> None:
    for good in (
        r"(?:^|[^A-Za-z0-9])([0-9]{4}([ .-])[0-9]{4}\2[0-9]{4})(?![A-Za-z0-9])",
        r"(?:[A-Za-z0-9-]{1,63}\.){1,8}",
        r"(?:[ab]|-(?=[a-z])){0,10}",
        r"([ .-]{0,2})[0-9]\1[0-9]",
        r"À",
    ):
        _lint(good)
