"""A value is taken whole or not at all: the property both suites hold.

A detector that counts a value while part of it is left in the clear tells the
organization a secret was masked when it left. So, for every value the shared
vectors say a detector takes (`positives`), placed in generated contexts —
prefixes and suffixes, a prefix on every line, escaped newlines, the value
lengthened past the engine's bounds, a private key cut before its END line —
under a policy enabling that detector alone: whenever the detector counts
something, no run of 4 characters of the value is left in the output. The Node
suite (`contentWholeValues.test.ts`) generates the same contexts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from idside._cleaning import ContentCleaner

_VECTORS = (
    Path(__file__).resolve().parents[2]
    / "descriptors"
    / "testdata"
    / "content-cleaning-vectors.json"
)
_PLACEHOLDER = re.compile(r"\[[A-Z0-9_]{1,24}_[0-9]{1,9}\]")
_FLOOR = json.loads(
    (_VECTORS.parent / "content-oracle.json").read_text(encoding="utf-8")
)["floor"]


def _join(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$concat"}:
        return "".join(value["$concat"])
    return value


_POSITIVES = [
    {key: _join(item) for key, item in positive.items()}
    for positive in json.loads(_VECTORS.read_text(encoding="utf-8"))["positives"]
]

#: What stands before the text, before every line of a multi-line value, and
#: after the text. Written the same way in the Node suite.
PREFIXES = ["", ">", "> ", "-", "- ", "+", "+ ", "//", "// ", "#", "# ", "*", "* "]
PREFIXES += [";", " * "] + [" " * width for width in (1, 2, 4, 8, 16, 17, 20, 32, 64)]
SUFFIXES = [
    "", " ", ".", "...", ",", ")", '"', "'", ";", ":", "abc", "7", "_x",
    " €", "€", " EUR", " USD", " European", " USDA", ":8080",
    ":s3cretPass", "/path", "\n", "\\n",
]  # fmt: skip
#: Characters appended to a value to take it past an internal bound.
LENGTHS = [1, 64, 300, 5000]
#: The token and secret detectors whose every pattern follows a value to its
#: end (`extend`): a
#: value written in a URL keeps its escapes (%2F), which are part of it.
_RULES = json.loads((_VECTORS.parents[1] / "content-rules.json").read_text("utf-8"))
ESCAPED = {
    detector["id"]
    for detector in _RULES["detectors"]
    if detector["group"] in ("ai_keys", "tech_keys", "secrets")
    and all("extend" in pattern for pattern in detector["patterns"])
}


def _lengthened(value: str, count: int) -> str:
    """``value`` and ``count`` more of its own letters and digits, in turn.

    Only a value that ends with a letter or a digit is open-ended: a run of
    them can go on. One that ends with punctuation (``-----``, ``==``) has a
    closing mark, past which more text is not more of the value.
    """
    alnum = [char for char in value if char.isascii() and char.isalnum()]
    return value + "".join(alnum[index % len(alnum)] for index in range(count))


def _variants(value: str, escaped: bool) -> Iterator[tuple[str, str, str]]:
    """(name, the value's own text, the form written), for one value."""
    yield "as is", value, value
    if value[-1].isascii() and value[-1].isalnum():
        for count in LENGTHS:
            longer = _lengthened(value, count)
            yield f"lengthened by {count}", longer, longer
        if escaped:
            longer = value + "%2F" + _lengthened(value, 8)[len(value) :]
            yield "lengthened by an escape", longer, longer
    if "\n" not in value:
        return
    bodies = [("whole", value)]
    lines = value.split("\n")
    if len(lines) > 2:
        bodies.append(("cut before its last line", "\n".join(lines[:-1])))
    for body_name, body in bodies:
        for prefix in PREFIXES:
            if prefix:
                lined = "\n".join(prefix + line for line in body.split("\n"))
                yield f"{body_name}, lines after {prefix!r}", body, lined
        yield f"{body_name}, escaped newlines", body, body.replace("\n", "\\n")
        yield (
            f"{body_name}, newlines escaped twice",
            body,
            body.replace("\n", "\\\\n"),
        )
        yield f"{body_name}, CRLF escaped", body, body.replace("\n", "\\r\\n")
        yield f"{body_name}, trailing spaces", body, body.replace("\n", "  \n")
        yield f"{body_name}, HTML line breaks", body, body.replace("\n", "<br>")
        yield f"{body_name}, on one line", body, body.replace("\n", " ")
        # Past every internal bound: a line of 70,000 characters, 5,000 lines.
        head, _, rest = body.partition("\n")
        wide = head + "\n" + _lengthened("A", 70_000) + "\n" + rest
        yield f"{body_name}, a line past 65,536 characters", wide, wide
        line = max(rest.split("\n"), key=len)  # a line of the key itself
        tall = head + "\n" + "\n".join([line] * 5000) + "\n" + rest
        yield (
            f"{body_name}, past 4,096 lines",
            tall,
            "\n".join("> " + line for line in tall.split("\n")),
        )
        if body_name != "whole":
            yield body_name, body, body


def _contexts(
    lead: str, value: str, trail: str, escaped: bool = False
) -> Iterator[tuple[str, str, str, str]]:
    """(name, value's own text, text to clean, the frame around the value)."""
    frame = lead + "\x00" + trail
    for name, own, written in _variants(value, escaped):
        yield name, own, lead + written + trail, frame
    for prefix in PREFIXES[1:]:
        text = prefix + lead + value + trail
        yield f"after {prefix!r}", value, text, frame + "\x00" + prefix
    # Right after the value: what is written next to it is what may cut it. A
    # trail that belongs to the value's form (the @host of a connection
    # string) stays next to it, and the suffix comes after.
    joined = trail[:1] in ("@", ":")
    for suffix in SUFFIXES[1:]:
        for after in (trail, ""):
            if joined:
                text = lead + value + trail + suffix
            else:
                text = lead + value + suffix + after
            name = f"before {suffix!r}" + ("" if after else ", nothing after")
            yield name, value, text, frame + "\x00" + suffix


def _leaks(own: str, output: str, frame: str) -> list[str]:
    """Runs of 4 characters of the value left in the output, framing aside."""
    bare = _PLACEHOLDER.sub("\x00", output)
    left = {bare[start : start + 4] for start in range(len(bare) - 3)}
    framed = {frame[start : start + 4] for start in range(len(frame) - 3)}
    found = []
    for line in own.split("\n"):
        for start in range(len(line) - 3):
            gram = line[start : start + 4]
            if gram in left and gram not in framed and gram not in found:
                found.append(gram)
    return found


def _cases() -> Iterator[Any]:
    for positive in _POSITIVES:
        detector, value = positive["detector"], positive["value"]
        yield pytest.param(
            detector,
            positive["lead"],
            value,
            positive["trail"],
            id=f"{detector}: {value[:24]!r}",
        )


@pytest.mark.parametrize(("detector", "lead", "value", "trail"), list(_cases()))
def test_a_counted_value_leaves_nothing_behind(
    detector: str, lead: str, value: str, trail: str
) -> None:
    cleaner = ContentCleaner({"schema": 1, "detectors": {detector: "mask"}})
    failures = []
    for name, own, text, frame in _contexts(lead, value, trail, detector in ESCAPED):
        result = cleaner.clean(text)
        if not result.counts.get(detector):
            continue
        leaks = _leaks(own, result.payload, frame)
        if leaks:
            failures.append(f"{name}: {leaks[:3]}")
    assert not failures, failures[:8]


def place(lead: str, value: str, trail: str, prefix: str, suffix: str) -> str:
    """``value`` between ``prefix`` and ``suffix``, as `content-oracle.json` says.

    A lead that ends with anything but a blank (``postgres://``, ``Email:``), or
    with an authorization scheme (``Bearer ``), is part of the value's form:
    the prefix goes before it. A trail that starts with ``@`` or ``:`` is too.
    """
    form = lead and (
        lead[-1] not in " \t\n"
        or lead.rstrip()[-1:] in ("=", ":")
        or lead.lower().endswith(("bearer ", "basic "))
    )
    left = prefix + lead if form else lead + prefix
    right = trail + suffix if trail[:1] in ("@", ":") else suffix + trail
    return left + value + right


@pytest.mark.parametrize(("detector", "lead", "value", "trail"), list(_cases()))
def test_a_value_in_a_neutral_context_is_counted(
    detector: str, lead: str, value: str, trail: str
) -> None:
    """The floor: a context the detector skips is a failure, not a pass."""
    cleaner = ContentCleaner({"schema": 1, "detectors": {detector: "mask"}})
    skipped = []
    for prefix in _FLOOR["prefixes"]:
        for suffix in _FLOOR["suffixes"]:
            text = place(lead, value, trail, prefix, suffix)
            if not cleaner.clean(text).counts.get(detector):
                skipped.append(f"{prefix!r} + value + {suffix!r}")
    assert not skipped, f"{len(skipped)} contexts skipped: {skipped[:12]}"


def test_the_property_sees_every_detector_and_a_partial_mask() -> None:
    seen = {positive["detector"] for positive in _POSITIVES}
    rules = json.loads(
        (_VECTORS.parent.parent / "content-rules.json").read_text(encoding="utf-8")
    )
    assert seen == {detector["id"] for detector in rules["detectors"]}
    # The check itself: a mask that leaves a tail is a leak, one that does not
    # is none, and the framing is never mistaken for the value.
    assert _leaks("AbCdEfGh", "[X_1]EfGh", "") == ["EfGh"]
    assert _leaks("AbCdEfGh", "[X_1]", "") == []
    assert _leaks("AbCd", "key AbCd", "key AbCd") == []
