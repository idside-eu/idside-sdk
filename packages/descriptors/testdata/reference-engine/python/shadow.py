# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""The normalised shadow of a text, and the map back to the original.

Rules never read the text itself. They read its *shadow*: each code point
mapped on its own — dropped when it is invisible (``ignorable``), otherwise
decomposed (NFKD), stripped of combining marks, its typographic dashes and
quotes written as ASCII, and anything outside the Basic Multilingual Plane
written U+FFFD. A match in the shadow is mapped back to whole original
characters, which are what get replaced.

Mapping code points one at a time, never the whole string, is what makes the
two SDKs agree: the offset map stays trivial, and every shadow character has
one length in both languages (U+FFFD for an astral code point, which is one
UTF-16 unit as it is one Python character). The lowered shadow lowers ASCII
letters only: Python's ``str.lower`` and JavaScript's ``toLowerCase`` do not
agree on every non-ASCII letter, and a placeholder decided by one of them
would not be the other's.
"""

from __future__ import annotations

import re
import string
import unicodedata
from bisect import bisect_right

from idside._cleaning.rules import NORMALIZATION

_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _in(ranges: list[list[int]], code: int) -> bool:
    return any(low <= code <= high for low, high in ranges)


_IGNORABLE: list[list[int]] = NORMALIZATION["ignorable"]
_COMBINING: list[list[int]] = NORMALIZATION["combining"]
_EQUIVALENTS: dict[int, str] = dict(NORMALIZATION["equivalents"])


def map_code_point(code: int) -> str:
    """The shadow of one code point (possibly empty, possibly several characters)."""
    out = []
    for char in unicodedata.normalize("NFKD", chr(code)):
        point = ord(char)
        if _in(_COMBINING, point) or _in(_IGNORABLE, point):
            continue
        if point in _EQUIVALENTS:
            out.append(_EQUIVALENTS[point])
        elif point > 0xFFFF:
            out.append("\ufffd")
        else:
            out.append(char)
    return "".join(out)


class _ShadowTable(dict[int, str]):
    """``str.translate`` table filled on first sight of each code point."""

    def __missing__(self, code: int) -> str:
        value = map_code_point(code)
        self[code] = value
        return value


class _OddTable(dict[int, str]):
    """Maps a code point to itself when its shadow is not exactly one character."""

    def __missing__(self, code: int) -> str:
        value = "" if len(_SHADOW[code]) == 1 else chr(code)
        self[code] = value
        return value


_SHADOW = _ShadowTable()
_ODD = _OddTable()


class Shadow:
    """A text's shadow, its ASCII-lowered twin, and the map back to the text.

    ``segments`` is ``None`` when every character maps to exactly one shadow
    character (all of ASCII, most accented text): offsets are then shared.
    Otherwise it lists ``(shadow_start, original_start, aligned)`` runs.
    """

    __slots__ = ("_segments", "_starts", "lower", "text")

    def __init__(
        self, text: str, lower: str, segments: list[tuple[int, int, bool]] | None
    ) -> None:
        self.text = text
        self.lower = lower
        self._segments = segments
        self._starts = [start for start, _, _ in segments] if segments else []

    def start(self, index: int) -> int:
        """The original offset where the shadow character ``index`` comes from."""
        if self._segments is None:
            return index
        start, origin, aligned = self._segments[bisect_right(self._starts, index) - 1]
        return origin + (index - start) if aligned else origin

    def end(self, end: int) -> int:
        """The original offset just past the character the shadow ends inside."""
        if self._segments is None:
            return end
        last = end - 1
        start, origin, aligned = self._segments[bisect_right(self._starts, last) - 1]
        return origin + (last - start) + 1 if aligned else origin + 1


def shadow_of(text: str) -> Shadow:
    if text.isascii():
        return Shadow(text, text.lower(), None)
    shadow = text.translate(_SHADOW)
    lower = shadow.translate(_ASCII_LOWER)
    odd = text.translate(_ODD)
    if not odd:
        return Shadow(shadow, lower, None)
    finder = re.compile("[" + "".join(re.escape(char) for char in set(odd)) + "]")
    segments: list[tuple[int, int, bool]] = []
    position = 0
    shadow_at = 0
    for match in finder.finditer(text):
        at = match.start()
        if at > position:
            segments.append((shadow_at, position, True))
            shadow_at += at - position
        mapped = _SHADOW[ord(text[at])]
        if mapped:
            segments.append((shadow_at, at, False))
            shadow_at += len(mapped)
        position = at + 1
    if position < len(text):
        segments.append((shadow_at, position, True))
    return Shadow(shadow, lower, segments)


def fold(text: str) -> str:
    """The lowered shadow of ``text``: how custom terms and domains are compared."""
    return shadow_of(text).lower
