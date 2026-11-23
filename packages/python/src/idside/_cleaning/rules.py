# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""The prompt-cleaning catalogue, loaded once from the bundled rules file.

``content-rules.json`` is a byte-identical copy of
``packages/descriptors/content-rules.json``, shared with the Node SDK and the
ID Side cloud. Its SHA-256 is the ``rules`` digest every cleaned call declares.
The patterns in it are written in a subset both regex engines read alike, so
they are compiled here as they are, with ``re.ASCII`` and nothing else.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from idside._cleaning.material import private_key_material

#: The version of the engine in this package. A rules file written for another
#: engine is never applied by this one (see ``catalogue_problem``).
ENGINE_VERSION = "1"

RULES_PATH = Path(__file__).resolve().parent.parent / "content-rules.json"
RULES_BYTES = RULES_PATH.read_bytes()
RULES: dict[str, Any] = json.loads(RULES_BYTES.decode("utf-8"))

LIMITS: dict[str, int] = RULES["limits"]
NORMALIZATION: dict[str, Any] = RULES["normalization"]
TABLES: dict[str, Any] = RULES["tables"]
PLACEHOLDER = re.compile(RULES["placeholder"]["regex"], re.ASCII)


def _class_body(ranges: list[list[int]]) -> str:
    """A character-class body for code point ranges, as both engines write it."""
    return "".join(f"\\u{low:04x}-\\u{high:04x}" for low, high in ranges)


#: Letters and digits for whole-word custom terms, after normalisation.
WORD_CLASS = _class_body(NORMALIZATION["word"])


def boundary(outside: str) -> str:
    """What may stand before a value: the start of the text, an escape written
    in two characters (``\\n``, ``\\r``, ``\\t``: JSON or a tool result pasted as
    text, whose letter is not the value's) or a character outside ``outside``.
    The catalogue's patterns all begin this way; custom entries and keywords
    are compiled with it."""
    return r"(?:^|\\[nrt]|[^" + outside + "])"


@dataclass(frozen=True)
class Pattern:
    """One compiled pattern of a rule: the value is capture group ``group``."""

    regex: re.Pattern[str]
    group: int
    keywords: re.Pattern[str] | None
    #: Lowercase literals one of which must appear in the lowered shadow;
    #: ``#digit`` stands for any ASCII digit. Empty: always run.
    prefilter: tuple[str, ...]
    #: Applied again and again where an accepted value ends, while it takes a
    #: character: how a value that runs on is followed to its end.
    extend: re.Pattern[str] | None = None
    #: Run only when the policy blocks on the detector: under mask, what this
    #: pattern alone finds (a key's BEGIN line quoted in prose) is left as it
    #: is and not counted.
    block_only: bool = False
    #: What must follow a match for it to be one, read by the engine where the
    #: match ends (``text``, ``end``): where it fails, no match starts there.
    followed_by: Callable[[str, int], bool] | None = None


#: The checks a pattern may name in ``followed_by``.
FOLLOWED_BY: dict[str, Callable[[str, int], bool]] = {
    "private_key_material": private_key_material,
}


def followed_by(name: str) -> Callable[[str, int], bool]:
    """The check a pattern names. One this engine does not know is refused:
    leaving it out would take every match, what must follow it or not."""
    check = FOLLOWED_BY.get(name)
    if check is None:
        raise ValueError(f"unknown followed_by check {name!r}")
    return check


def keyword_regex(words: list[str]) -> re.Pattern[str] | None:
    """Whole-word context words, matched on the lowered shadow."""
    if not words:
        return None
    alternatives = "|".join(
        r"[ \t\r\n]{1,4}".join(re.escape(part) for part in word.split(" "))
        for word in sorted(words, key=lambda word: (-len(word), word))
    )
    return re.compile(
        boundary("a-z0-9") + "(" + alternatives + r")(?![a-z0-9])", re.ASCII
    )


def compile_pattern(spec: dict[str, Any]) -> Pattern:
    flags = re.ASCII | (re.IGNORECASE if spec.get("ignore_case") else 0)
    keywords = spec.get("keywords") or []
    prefilter = tuple(spec.get("prefilter") or ()) or tuple(keywords)
    extend = spec.get("extend")
    check = spec.get("followed_by")
    return Pattern(
        regex=re.compile(spec["regex"], flags),
        group=spec["group"],
        keywords=keyword_regex(keywords),
        prefilter=prefilter,
        extend=None if extend is None else re.compile(extend, flags),
        block_only=spec.get("block_only") is True,
        followed_by=None if check is None else followed_by(check),
    )


@dataclass(frozen=True)
class Detector:
    """A catalogue entry, compiled."""

    id: str
    label: str
    index: int
    patterns: tuple[Pattern, ...]
    validator: str | None


DETECTORS: dict[str, Detector] = {
    entry["id"]: Detector(
        id=entry["id"],
        label=entry["label"],
        index=index,
        patterns=tuple(compile_pattern(spec) for spec in entry["patterns"]),
        validator=entry.get("validator"),
    )
    for index, entry in enumerate(RULES["detectors"])
}

CATALOGUE_IDS: tuple[str, ...] = tuple(DETECTORS)


def catalogue_problem() -> str | None:
    """Why this package's rules cannot be applied, or ``None`` when they can."""
    if RULES.get("engine") != ENGINE_VERSION:
        return (
            "the bundled prompt-cleaning rules are for engine "
            f"{RULES.get('engine')!r}, this SDK runs engine {ENGINE_VERSION!r}"
        )
    return None
