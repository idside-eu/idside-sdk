"""Key material after a private key's BEGIN line: the check `followed_by` names.

The check replaced a look-ahead of the catalogue that a backtracking matcher
read in time exponential in the number of header lines. It must answer exactly
what that look-ahead answered: the shared vectors state the answers at every
bound by hand (`testdata/content-material-vectors.json`, replayed by the Node
suite too), the look-ahead itself is kept below as the specification and run on
random text against the check, and the engine is held to linear time on the
shapes that were slow.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from idside._cleaning import ContentCleaner
from idside._cleaning import rules as rules_module
from idside._cleaning.engine import _Candidate, _resolve
from idside._cleaning.material import private_key_material
from idside._cleaning.shadow import shadow_of

_TESTDATA = Path(__file__).resolve().parents[2] / "descriptors" / "testdata"
_VECTORS: dict[str, Any] = json.loads(
    (_TESTDATA / "content-material-vectors.json").read_text(encoding="utf-8")
)


def _decode(value: Any) -> str:
    if isinstance(value, str):
        return value
    return "".join(
        part if isinstance(part, str) else part["repeat"] * part["times"]
        for part in value["$concat"]
    )


_BEGIN = _decode(_VECTORS["begin"])
_KEYS = {"schema": 1, "detectors": {"private_key": "mask"}}  # pragma: allowlist secret
_BOTH = {"schema": 1, "detectors": {**_KEYS["detectors"], "email": "mask"}}
_CASES = [
    (case["name"], _decode(case["after"]), case["material"])
    for case in _VECTORS["cases"]
]

# ── The look-ahead the check replaced, as the specification ────────────────

_B64 = r"[A-Za-z0-9+/]"
_B64_FIRST = r"[A-Za-z0-9/]"
_SKIP = (
    r"(?:[ \t\r\n>+#;*|\"',«»-]|\\[nrt]?|//|<[Bb][Rr] ?/?>"
    r"|\[[A-Z0-9_]{1,24}_[0-9]{1,9}\])"
)
_NAMES = ("Proc-Type", "DEK-Info", "Version", "Comment", "Hash", "Charset", "MessageID")
_NOT_A_NAME = "(?!" + "|".join(name + ":" for name in _NAMES) + ")"
_ARMOR = (
    "(?:" + "|".join(_NAMES) + "):"
    "(?:(?:"
    + _NOT_A_NAME
    + r"[^\r\n\\]){0,255}"
    + _NOT_A_NAME
    + r"[^\r\n\\ \t>+#;*|\"',«»<-])?"
)
_TWO_RUNS = "".join(
    "|" + _B64_FIRST + _B64 + "{" + str(n - 1) + "}" + _SKIP + "{1,1024}" + _B64_FIRST
    + (_B64 + "{" + str(31 - n) + "}" if n < 31 else "")
    for n in range(16, 32)
)  # fmt: skip
_LOOK_AHEAD = re.compile(
    "(?=" + _SKIP + "{0,1024}(?:" + _ARMOR + _SKIP + "{0,512}){0,4}"
    "(?:" + _B64_FIRST + _B64 + "{31}" + _TWO_RUNS + "))",
    re.ASCII,
)


def _look_ahead(text: str, at: int) -> bool:
    return _LOOK_AHEAD.match(text, at) is not None


# ── the vectors ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "after", "material"), _CASES, ids=[c[0] for c in _CASES]
)
def test_the_check_answers_each_vector(name: str, after: str, material: bool) -> None:
    text = _BEGIN + after
    assert private_key_material(text, len(_BEGIN)) is material


@pytest.mark.parametrize(
    ("name", "after", "material"), _CASES, ids=[c[0] for c in _CASES]
)
def test_each_vector_is_what_the_look_ahead_answered(
    name: str, after: str, material: bool
) -> None:
    assert _look_ahead(_BEGIN + after, len(_BEGIN)) is material


@pytest.mark.parametrize(
    ("name", "after", "material"), _CASES, ids=[c[0] for c in _CASES]
)
def test_a_key_without_its_end_line_is_masked_only_before_key_material(
    name: str, after: str, material: bool
) -> None:
    text = "Here it is: " + _BEGIN + after
    result = ContentCleaner(_KEYS).clean(text)
    if material:
        assert (result.payload, dict(result.counts)) == (
            "Here it is: [PRIVATE_KEY_1]",
            {"private_key": 1},
        )
    else:
        assert (result.payload, dict(result.counts)) == (text, {})


def test_the_pattern_names_the_check_and_no_longer_looks_ahead() -> None:
    (pattern,) = [
        p
        for p in rules_module.DETECTORS["private_key"].patterns
        if p.followed_by is not None
    ]
    assert pattern.followed_by is private_key_material
    assert "(?=" not in pattern.regex.pattern


def test_followed_by_is_read_from_the_catalogue_as_written() -> None:
    spec = {"regex": "(a)", "group": 1, "prefilter": []}
    assert rules_module.compile_pattern(spec).followed_by is None
    named = {**spec, "followed_by": "private_key_material"}
    assert rules_module.compile_pattern(named).followed_by is private_key_material
    with pytest.raises(ValueError, match="unknown followed_by check 'nothing'"):
        rules_module.compile_pattern({**spec, "followed_by": "nothing"})


def test_a_failed_check_moves_on_to_the_next_begin_line() -> None:
    # The first BEGIN line is only quoted; the second has its key material.
    text = _BEGIN + " is how a key starts.\n" + _BEGIN + "\n" + "A" * 64
    cleaner = ContentCleaner(_KEYS)
    result = cleaner.clean(text)
    assert result.payload == _BEGIN + " is how a key starts.\n[PRIVATE_KEY_1]"


# ── the check against the look-ahead, on random text ───────────────────────

_SEPARATORS = [
    *" \t\r\n>+#;*|\"',«»-", "\\n", "\\", "//", "/", "<br>", "<br >", "<br/>",
    "<br />", "<BR>", "<", "[EMAIL_1]", "[A_1]", "[", "]",
]  # fmt: skip
_HEADERS = [*(name + ":" for name in _NAMES), "comment:", "Comment"]


def _random_after(rng: random.Random) -> str:
    def run(size: int) -> str:
        return "".join(rng.choice("ABCabc019+/") for _ in range(size))

    def skip(count: int) -> str:
        return "".join(rng.choice(_SEPARATORS) for _ in range(count))

    parts = [skip(rng.choice([0, 1, 2, 5]))]
    for _ in range(rng.choice([0, 1, 1, 2, 3, 4, 5])):
        value = "".join(
            rng.choice(["x", " ", "<br>", "[A_1]", "//", "a@b.fr", run(4), "Hash:"])
            for _ in range(rng.choice([0, 1, 3, 6]))
        )
        parts.append(rng.choice(_HEADERS) + value + skip(rng.choice([0, 1, 2, 4])))
    first = rng.choice([8, 15, 16, 20, 31, 32])
    parts.append(run(first))
    if rng.random() < 0.6:
        parts.append(skip(rng.choice([0, 1, 2])) + run(rng.choice([1, 8, 15, 16, 17])))
    return "".join(parts)


def test_the_check_answers_what_the_look_ahead_answered_on_random_text() -> None:
    rng = random.Random(20261123)  # noqa: S311 - reproducible test data
    accepted = 0
    for _ in range(6000):
        text = _BEGIN + _random_after(rng)
        expected = _look_ahead(text, len(_BEGIN))
        accepted += expected
        assert private_key_material(text, len(_BEGIN)) is expected, repr(text)
    # Both answers are well represented.
    assert 1000 < accepted < 5000


# ── time ───────────────────────────────────────────────────────────────────


def _blocks(block: str, size: int) -> str:
    return block * (size // len(block) + 1)


#: Shapes that the look-ahead read in time exponential in the header lines,
#: or quadratic in what follows a value: one block each, repeated.
_SLOW_SHAPES = {
    "four lines of <br>": _BEGIN + ("\nComment: " + "<br>" * 63) * 4 + "\n!!\n",
    "four lines of placeholders": (
        _BEGIN + ("\nComment: " + "[A_1]" * 50) * 4 + "\n!!\n"
    ),
    "four lines of addresses": _BEGIN + ("\nComment: " + "a@b.fr " * 40) * 4 + "\n!!\n",
    "a value ending in a <br>, then blanks": (
        _BEGIN + "\nComment: " + "<br>" * 63 + "\n" * 449 + "!!\n"
    ),
    "64 header lines of 255": _BEGIN + ("\nComment: " + "." * 254) * 64 + "\n!!\n",
    "values of every length": _BEGIN + "".join(
        "\nComment: " + ("a <br>[A_1]//" * 20)[:size] for size in range(1, 256, 17)
    ) + "\n!!\n",
    "separators of every sort": _BEGIN + (
        "\nComment: x" + " \t>+#;*|\"',«»-\\n\\//<br><br ><br/><br />[A_1]" * 12
    ) * 4 + "\n!!\n",
    "four comment lines then blanks": (
        _BEGIN + ("\nComment: x" + " " * 512) * 4 + "\n!!\n"
    ),
}  # fmt: skip


@pytest.mark.parametrize("shape", sorted(_SLOW_SHAPES))
def test_one_block_of_each_slow_shape_takes_milliseconds(shape: str) -> None:
    # One block took up to 24 s with the look-ahead (four lines of <br>); the
    # addresses, masked first, become placeholders the second pass reads.
    cleaner = ContentCleaner(_BOTH)
    began = time.perf_counter()
    result = cleaner.clean(_SLOW_SHAPES[shape])
    assert time.perf_counter() - began < 0.5
    assert "private_key" not in result.counts  # pragma: allowlist secret


@pytest.mark.parametrize("lines", [1, 2, 3, 4, 5, 8, 16, 32, 64])
def test_one_block_of_header_lines_of_br_takes_milliseconds(lines: int) -> None:
    # Only four header lines are read; the look-ahead's time grew with each.
    block = _BEGIN + ("\nComment: " + "<br>" * 63) * lines + "\n!!\n"
    cleaner = ContentCleaner(_BOTH)
    began = time.perf_counter()
    result = cleaner.clean(block)
    assert time.perf_counter() - began < 0.5
    assert "private_key" not in result.counts  # pragma: allowlist secret


@pytest.mark.parametrize("shape", sorted(_SLOW_SHAPES))
def test_a_megabyte_of_each_slow_shape_grows_linearly(shape: str) -> None:
    cleaner = ContentCleaner(_BOTH)

    def best(size: int) -> float:
        text = _blocks(_SLOW_SHAPES[shape], size)
        times = []
        for _ in range(2):
            began = time.perf_counter()
            cleaner.clean(text)
            times.append(time.perf_counter() - began)
        return min(times)

    small, large = best(262_144), best(1_048_576)
    assert large <= 8 * max(small, 0.002)
    assert large < 5


def _alternating(size: int) -> str:
    half = size // 2
    return (" 1.1.1.1" * (half // 8 + 1))[:half] + (" 10.10.10.10" * (half // 12 + 1))[
        :half
    ]


def test_many_findings_of_two_lengths_resolve_in_linear_time() -> None:
    # Inserting each kept finding into a sorted list took quadratic time: 28 s
    # for 8 MiB of alternating addresses.
    cleaner = ContentCleaner({"schema": 1, "detectors": {"ip_address": "mask"}})

    def best(size: int) -> float:
        text = _alternating(size)
        times = []
        for _ in range(2):
            began = time.perf_counter()
            cleaner.clean(text)
            times.append(time.perf_counter() - began)
        return min(times)

    small, large = best(1_048_576), best(4_194_304)
    assert large <= 8 * small


@dataclass(frozen=True)
class _Rule:
    order: int
    key: str


def test_resolving_keeps_each_group_of_overlapping_findings_under_its_first() -> None:
    first, second = _Rule(0, "a"), _Rule(1, "b")
    shadow = shadow_of("abcdefghij")
    resolved = _resolve(
        [
            _Candidate(0, 2, second),  # joins the group through (1, 4)
            _Candidate(1, 4, first),  # the longest: wins the group
            _Candidate(6, 8, second),  # alone
            _Candidate(4, 5, first),  # touches (1, 4), does not overlap it
        ],
        shadow,
    )
    assert [(c.start, c.end, c.rule.key) for c in resolved] == [
        (0, 4, "a"),
        (4, 5, "a"),
        (6, 8, "b"),
    ]
