# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Whether key material follows a private key's BEGIN line: the check a pattern names.

A private key without its END line is taken only when key material comes right
after its BEGIN line (``content-rules.json``, ``private_key``): between them
stand only separators (blanks, line breaks real or escaped, line prefixes,
quotes, ``<br>``, placeholders), at most 1,024 of them, then up to four PEM or
PGP header lines (``Comment:`` and its value, at most 256 characters, then at
most 512 separators), then 32 base64 characters in one run or in two, the first
of 16 or more, with 1 to 1,024 separators between them.

That reading was a look-ahead in the pattern. A backtracking matcher tries
every place a header value may end, and every one of them again for each later
header line: four lines of ``<br>`` took a minute. Here the same reading is
done once: the places a value may end are read together, each position after
them is reached with the fewest separators any reading takes to get there, and
the separator runs themselves are walked by one bounded expression. Every
reading the look-ahead accepts is accepted here, and no other: the vectors and
the differential tests hold both to the same answer.

Written alike in the Node SDK (``cleaning/material.ts``).
"""

from __future__ import annotations

import re

#: One separator: a blank, a line break real or escaped, a line prefix, a
#: quote, ``//``, ``<br>`` or a placeholder.
_SEPARATOR = (
    r"(?:[ \t\r\n>+#;*|\"',«»-]|\\[nrt]?|//|<[Bb][Rr] ?/?>"
    r"|\[[A-Z0-9_]{1,24}_[0-9]{1,9}\])"
)
_TOKEN = re.compile(_SEPARATOR, re.ASCII)
#: The separators that are one character, every one of them.
_SINGLES = " \t\r\n>+#;*|\"',«»-"
_HEADER_NAMES = (
    "Proc-Type", "DEK-Info", "Version", "Comment", "Hash", "Charset", "MessageID",
)  # fmt: skip
_HEADER = re.compile("|".join(name + ":" for name in _HEADER_NAMES), re.ASCII)
#: The longest header name and its colon: how far past a position a header
#: that starts there reaches.
_HEADER_REACH = max(len(name) + 1 for name in _HEADER_NAMES)
#: A value never holds a line break, real or escaped (the backslash).
_LINE_END = re.compile(r"[\r\n\\]")
#: Characters a header value may not end on.
_NOT_LAST = frozenset("\r\n\\ \t>+#;*|\"',«»<-")
_BASE64 = re.compile(r"[A-Za-z0-9+/]{0,32}", re.ASCII)
_FIRST = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/")
#: A run of 16 base64 characters or more (key material starts in one).
_RUN = re.compile(r"[A-Za-z0-9+/]{16,}", re.ASCII)
_FIRST_IN_RUN = re.compile(r"[A-Za-z0-9/]", re.ASCII)

#: The bounds of the reading (see the module docstring).
_LEAD = 1024
_HEADERS = 4
_VALUE = 256
_AFTER_HEADER = 512
_MATERIAL = 32
_FIRST_RUN = 16
_BETWEEN_RUNS = 1024

_CHAINS: dict[int, re.Pattern[str]] = {}
_STARTS: dict[int, re.Pattern[str]] = {}


def _chain(most: int) -> re.Pattern[str]:
    """Up to ``most`` separators, one after another: the one way a run of them
    reads (an escape takes its letter; the letter alone is a side branch)."""
    pattern = _CHAINS.get(most)
    if pattern is None:
        pattern = _CHAINS[most] = re.compile(
            "(?:" + _SEPARATOR + "){0," + str(most) + "}", re.ASCII
        )
    return pattern


def _starts(run: int) -> re.Pattern[str]:
    """Where, inside a run of separators, a base64 run of ``run`` characters or
    more may start: on the letter of an escape (``\\n`` read as ``\\`` then
    ``n...``), or on ``//`` that no ``/`` comes before (a later ``/`` of the same
    slashes starts a shorter run)."""
    pattern = _STARTS.get(run)
    if pattern is None:
        pattern = _STARTS[run] = re.compile(
            r"\\(?=[nrt][A-Za-z0-9+/]{" + str(run - 1) + r"})"
            r"|(?<!/)(?=//[A-Za-z0-9+/]{" + str(max(run - 2, 0)) + "})",
            re.ASCII,
        )
    return pattern


def _run(text: str, at: int) -> int:
    """The base64 characters from ``at``, counted up to 32."""
    match = _BASE64.match(text, at)
    return 0 if match is None else match.end() - at


class _Reader:
    """One reading of the text after one BEGIN line."""

    __slots__ = ("seconds", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        #: (where the separators start, how long the second run must be) ->
        #: whether one is reached.
        self.seconds: dict[tuple[int, int], bool] = {}

    # ── key material ──────────────────────────────────────────────────────

    def material(self, at: int) -> bool:
        """Whether 32 base64 characters, in one run or two, start at ``at``.

        Two runs are read only where this run ends: a + or // inside it
        cannot split it, since a second run inside the same characters would
        make 33 of them, and one run of 32 is already key material."""
        text = self.text
        if at >= len(text) or text[at] not in _FIRST:
            return False
        run = _run(text, at)
        if run >= _MATERIAL:
            return True
        return run >= _FIRST_RUN and self._second(at + run, _MATERIAL - run)

    def _second(self, at: int, run: int) -> bool:
        """Whether 1 to 1,024 separators from ``at`` lead to ``run`` base64
        characters or more, the first a letter, a digit or /."""
        key = (at, run)
        found = self.seconds.get(key)
        if found is None:
            found = self.seconds[key] = self._find_second(at, run)
        return found

    def _find_second(self, at: int, run: int) -> bool:
        # `at` follows a run of base64, so it is not base64 itself.
        text = self.text
        end = _chain(_BETWEEN_RUNS).match(text, at).end()  # type: ignore[union-attr]
        if end == at:
            return False
        if end < len(text) and text[end] in _FIRST and _run(text, end) >= run:
            return True
        hit = _starts(run).search(text, at, end + _MATERIAL)
        return hit is not None and hit.start() < end

    # ── separators ────────────────────────────────────────────────────────

    def walk(self, at: int, most: int) -> tuple[bool, int]:
        """Follow at most ``most`` separators from ``at``: whether key material
        starts on the way, and where the separators end."""
        text = self.text
        end = _chain(most).match(text, at).end()  # type: ignore[union-attr]
        if self.material(at) or (end > at and self.material(end)):
            return True, end
        starts = _starts(_FIRST_RUN)
        hit = starts.search(text, at, end + _MATERIAL)
        while hit is not None and hit.start() < end:
            where = hit.start()
            # An escape's letter, or the first / of slashes.
            if self.material(where + 1 if text[where] == "\\" else where):
                return True, end
            hit = starts.search(text, where + 1, end + _MATERIAL)
        return False, end

    # ── a header line ─────────────────────────────────────────────────────

    def header(self, at: int) -> tuple[bool, list[int]]:
        """The header value starting at ``at`` and the separators after it:
        whether key material starts there, and where the value of a next
        header may start."""
        text = self.text
        limit = min(at + _VALUE, len(text))
        line_end = _LINE_END.search(text, at, limit)
        if line_end is not None:
            limit = line_end.start()
        header = _HEADER.search(text, at, limit + _HEADER_REACH)
        if header is not None and header.start() < limit:
            limit = header.start()
        # `limit`: the value ends at or before it. Every position up to it
        # that a character other than a separator or < precedes is the end of
        # a value, reached with no separator at all.
        # A run read up to 16 characters past the bound starts at or before it.
        for run in _RUN.finditer(text, at, limit + _FIRST_RUN):
            first = _FIRST_IN_RUN.search(text, run.start(), min(run.end(), limit + 1))
            # The first place in the run that is reached and may start a run of
            # base64: a later one starts a shorter run of the same characters.
            while first is not None:
                where = first.start()
                if self._reached(at, where) is not None:
                    if self.material(where):
                        return True, []
                    break
                first = _FIRST_IN_RUN.search(text, where + 1, min(run.end(), limit + 1))
        heads: list[tuple[int, int]] = []
        used = self._reached(at, limit)
        if used is not None:
            heads.append((limit, used))
        if limit == at + _VALUE:
            # A separator that starts before the bound and ends after it.
            for start in range(max(at, limit - 35), limit):
                if text[start] in "</[":
                    token = _TOKEN.match(text, start)
                    if token is not None and token.end() > limit:
                        used = self._reached(at, start)
                        if used is not None:
                            heads.append((token.end(), used + 1))
        found: list[int] = []
        for head, used in heads:
            material, end = self.walk(head, _AFTER_HEADER - used)
            if material:
                return True, []
            name = _HEADER.match(text, end)
            if name is not None:
                found.append(name.end())
        return False, found

    def _reached(self, at: int, where: int) -> int | None:
        """The fewest separators that reach ``where`` (``at`` <= ``where`` <=
        the end of the value) from the header value at ``at``, or ``None``.

        A position after a character a value may end on is reached with none.
        After separators of one character back to such a position (or to
        ``at``), with one per character, but for one shortcut: ``<br >`` whose
        ``<`` is itself reached with none. A ``<`` in between, which only
        ``<br>`` reads, leaves it unreached."""
        text = self.text
        if where == at or text[where - 1] not in _NOT_LAST:
            return 0
        kept = text[at:where].rstrip(_SINGLES)
        if kept.endswith("<"):
            return None
        start = at + len(kept)
        used = where - start
        tag = start - 3
        if (
            used >= 2
            and tag >= at
            and text[start : start + 2] == " >"
            and text[tag] == "<"
            and text[tag + 1] in "Bb"
            and text[tag + 2] in "Rr"
            and (tag == at or text[tag - 1] not in _NOT_LAST)
        ):
            used -= 1
        return used


def private_key_material(text: str, at: int) -> bool:
    """Whether key material follows the BEGIN line that ends at ``at``."""
    reader = _Reader(text)
    material, end = reader.walk(at, _LEAD)
    if material:
        return True
    name = _HEADER.match(text, end)
    values = [] if name is None else [name.end()]
    for _ in range(_HEADERS):
        following: list[int] = []
        for value in dict.fromkeys(values):
            material, after = reader.header(value)
            if material:
                return True
            following.extend(after)
        values = following
    return False
