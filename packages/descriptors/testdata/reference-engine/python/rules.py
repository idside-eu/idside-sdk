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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class Pattern:
    """One compiled pattern of a rule: the value is capture group ``group``."""

    regex: re.Pattern[str]
    group: int
    keywords: re.Pattern[str] | None
    #: Lowercase literals one of which must appear in the lowered shadow;
    #: ``#digit`` stands for any ASCII digit. Empty: always run.
    prefilter: tuple[str, ...]


def keyword_regex(words: list[str]) -> re.Pattern[str] | None:
    """Whole-word context words, matched on the lowered shadow."""
    if not words:
        return None
    alternatives = "|".join(
        r"[ \t\r\n]{1,4}".join(re.escape(part) for part in word.split(" "))
        for word in sorted(words, key=lambda word: (-len(word), word))
    )
    return re.compile(r"(?:^|[^a-z0-9])(" + alternatives + r")(?![a-z0-9])", re.ASCII)


def compile_pattern(spec: dict[str, Any]) -> Pattern:
    flags = re.ASCII | (re.IGNORECASE if spec.get("ignore_case") else 0)
    keywords = spec.get("keywords") or []
    prefilter = tuple(spec.get("prefilter") or ()) or tuple(keywords)
    return Pattern(
        regex=re.compile(spec["regex"], flags),
        group=spec["group"],
        keywords=keyword_regex(keywords),
        prefilter=prefilter,
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
