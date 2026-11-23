# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""The prompt-cleaning engine: walk a call's body, mask or block what the policy names.

Deterministic and shared with the Node SDK (``cleaning/engine.ts``); the
vectors in ``packages/descriptors/testdata/content-cleaning-vectors.json`` hold
both to the same output, counts and blocks, byte for byte.

The engine never logs, raises or returns a matched value: a result carries the
cleaned body, counts per rule and blocks per rule. Counts are what leave the
process; the values they count never do.
"""

from __future__ import annotations

import re
from bisect import bisect_left, insort
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from idside._cleaning.policy import CompiledPolicy, Rule, compile_policy
from idside._cleaning.rules import LIMITS, PLACEHOLDER
from idside._cleaning.shadow import Shadow, shadow_of
from idside.providers import ContentFields

#: A base64 data: URI is an attachment wherever it stands; it is not decoded.
#: Only base64 to its last character: `data:text/plain,a sentence` is text,
#: and so is anything after a base64 header that base64 cannot hold.
_DATA_URI = re.compile(
    r"[Dd][Aa][Tt][Aa]:[A-Za-z0-9.+-]{1,64}/[A-Za-z0-9.+-]{1,128}"
    r"(?:;[A-Za-z0-9=._+-]{1,128}){0,8};[Bb][Aa][Ss][Ee]64,[A-Za-z0-9+/=\r\n]*",
    re.ASCII,
)
_DIGIT = re.compile(r"[0-9]")

#: The id under which a call blocked by the engine's own limits is reported.
SIZE_LIMIT = "size_limit"
#: The id under which a call blocked for carrying an attachment is reported.
ATTACHMENTS = "attachments"


@dataclass(frozen=True)
class CleanResult:
    """What cleaning one call produced.

    ``payload`` is the body that may leave (the input itself when nothing
    changed, and when the call is blocked — so it may hold what was blocked,
    and is kept out of the repr). ``counts``: masked occurrences per detector
    id or ``custom:LABEL``. ``blocked``: when not empty, the call must not be
    sent; occurrences per rule, ``attachments`` or ``size_limit``.
    """

    payload: Any = field(repr=False)
    counts: Mapping[str, int]
    blocked: Mapping[str, int]
    attachments: int


class _Limit(Exception):
    """The body is past the engine's limits: blocked, never skipped."""


class _Candidate(NamedTuple):
    start: int  # shadow offsets
    end: int
    rule: Rule


class _Spans:
    """Sorted, non-overlapping intervals."""

    __slots__ = ("ends", "starts")

    def __init__(self) -> None:
        self.starts: list[int] = []
        self.ends: list[int] = []

    def overlaps(self, start: int, end: int) -> bool:
        index = bisect_left(self.starts, start)
        if index > 0 and self.ends[index - 1] > start:
            return True
        return index < len(self.starts) and self.starts[index] < end

    def add(self, start: int, end: int) -> None:
        index = bisect_left(self.starts, start)
        self.starts.insert(index, start)
        self.ends.insert(index, end)


def _utf16(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def _present(value: Any) -> bool:
    return not (value is None or value == "" or value == [] or value == {})


class ContentCleaner:
    """A compiled policy and a provider's content fields, ready to clean bodies."""

    def __init__(self, policy: Any, fields: ContentFields | None = None) -> None:
        self._policy: CompiledPolicy = compile_policy(policy)
        self._fields = fields or ContentFields()

    @classmethod
    def reference(cls) -> ContentCleaner:
        """Every catalogue detector masking: the policy challenges are answered with."""
        return cls(reference_policy())

    # ── walk ──────────────────────────────────────────────────────────────

    def _walk(
        self,
        node: Any,
        path: tuple[Any, ...],
        depth: int,
        texts: list[tuple[tuple[Any, ...], str]],
        attachments: list[int],
        free: bool,
    ) -> None:
        if depth > LIMITS["max_depth"]:
            raise _Limit
        if isinstance(node, str):
            if _DATA_URI.fullmatch(node):
                attachments[0] += 1
            else:
                texts.append((path, node))
        elif isinstance(node, dict):
            fields = self._fields
            for key in sorted(node, key=_key_order):
                value = node[key]
                inner = free or key in fields.free_form_keys
                if not free:
                    if key in fields.structural_keys:
                        continue
                    if key in fields.attachment_keys or fields.attachment_object(
                        key, value
                    ):
                        attachments[0] += _present(value)
                        continue
                self._walk(value, (*path, key), depth + 1, texts, attachments, inner)
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                self._walk(item, (*path, index), depth + 1, texts, attachments, free)
        elif node is not None and not isinstance(node, (bool, int, float)):
            raise ValueError(
                f"cannot clean a value of type {type(node).__name__!r}: "
                "the body must be JSON (dict, list, str, int, float, bool, None)"
            )

    # ── one text ──────────────────────────────────────────────────────────

    def _find(
        self, shadow: Shadow, placeholders: _Spans
    ) -> tuple[list[_Candidate], list[_Candidate]]:
        masks: list[_Candidate] = []
        blocks: list[_Candidate] = []
        has_digit: bool | None = None
        window = LIMITS["keyword_window"]
        for rule in self._policy.rules:
            source = shadow.lower if rule.lowered else shadow.text
            for pattern in rule.patterns:
                if pattern.prefilter:
                    wanted = False
                    for literal in pattern.prefilter:
                        if literal == "#digit":
                            if has_digit is None:
                                has_digit = _DIGIT.search(shadow.text) is not None
                            wanted = has_digit
                        else:
                            wanted = literal in shadow.lower
                        if wanted:
                            break
                    if not wanted:
                        continue
                hits: tuple[list[int], list[int]] | None = None
                position = 0
                while True:
                    match = pattern.regex.search(source, position)
                    if match is None:
                        break
                    start, end = match.span(pattern.group)
                    if start < 0:
                        # The value group took no part: the pattern matched the
                        # context that rules a value out. Skip it whole.
                        position = max(match.end(), match.start() + 1)
                        continue
                    accepted = end > start and not placeholders.overlaps(start, end)
                    if accepted and pattern.keywords is not None:
                        if hits is None:
                            hits = _keyword_hits(pattern.keywords, shadow, placeholders)
                        accepted = _near(hits, start, end, window)
                    if accepted and rule.validator is not None:
                        length = rule.validator(source[start:end])
                        accepted = length is not None
                        if length is not None:
                            end = start + length
                    if accepted:
                        found = _Candidate(start, end, rule)
                        (blocks if rule.action == "block" else masks).append(found)
                        position = end
                    else:
                        position = start + 1
        return masks, blocks

    # ── a whole call ──────────────────────────────────────────────────────

    def clean(self, payload: Any) -> CleanResult:
        texts: list[tuple[tuple[Any, ...], str]] = []
        attachments = [0]
        try:
            self._walk(payload, (), 0, texts, attachments, False)
            if sum(len(text) for _, text in texts) > LIMITS["max_text_chars"]:
                raise _Limit
        except _Limit:
            return CleanResult(payload, {}, {SIZE_LIMIT: 1}, attachments[0])

        blocked: dict[str, int] = {}
        if attachments[0] and self._policy.block_attachments:
            blocked[ATTACHMENTS] = attachments[0]

        current = [text for _, text in texts]
        shadows = [shadow_of(text) for text in current]
        floors: dict[str, int] = {}
        for shadow in shadows:
            for match in PLACEHOLDER.finditer(shadow.text):
                label, _, number = match.group()[1:-1].rpartition("_")
                floors[label] = max(floors.get(label, 0), int(number))

        counts: dict[str, int] = {}
        assigned: dict[tuple[str, str], str] = {}
        following: dict[str, int] = {}
        todo = list(range(len(current)))
        for round_ in range(LIMITS["max_passes"]):
            changed: list[int] = []
            found_blocks: dict[str, int] = {}
            for index in todo:
                shadow = shadows[index] if round_ == 0 else shadow_of(current[index])
                placeholders = _Spans()
                for match in PLACEHOLDER.finditer(shadow.text):
                    placeholders.add(match.start(), match.end())
                masks, blocks = self._find(shadow, placeholders)
                for candidate in _resolve(blocks, shadow):
                    key = candidate.rule.key
                    found_blocks[key] = found_blocks.get(key, 0) + 1
                if found_blocks or not masks:
                    continue
                replaced = self._replace(
                    current[index], shadow, _resolve(masks, shadow),
                    assigned, following, floors, counts,
                )  # fmt: skip
                current[index] = replaced
                changed.append(index)
            if found_blocks:
                for key, count in found_blocks.items():
                    blocked[key] = blocked.get(key, 0) + count
                break
            if not changed:
                break
            todo = changed
        else:
            return CleanResult(payload, {}, {SIZE_LIMIT: 1}, attachments[0])

        if blocked:
            return CleanResult(payload, {}, blocked, attachments[0])
        replacements = {
            path: current[index]
            for index, (path, original) in enumerate(texts)
            if current[index] != original
        }
        return CleanResult(_rebuild(payload, replacements), counts, {}, attachments[0])

    @staticmethod
    def _replace(
        text: str,
        shadow: Shadow,
        masks: list[_Candidate],
        assigned: dict[tuple[str, str], str],
        following: dict[str, int],
        floors: dict[str, int],
        counts: dict[str, int],
    ) -> str:
        pieces: list[str] = []
        position = 0
        for candidate in masks:
            label = candidate.rule.label
            value = (label, shadow.lower[candidate.start : candidate.end])
            placeholder = assigned.get(value)
            if placeholder is None:
                number = following.get(label, floors.get(label, 0) + 1)
                following[label] = number + 1
                placeholder = f"[{label}_{number}]"
                assigned[value] = placeholder
            key = candidate.rule.key
            counts[key] = counts.get(key, 0) + 1
            start = shadow.start(candidate.start)
            pieces.append(text[position:start])
            pieces.append(placeholder)
            position = shadow.end(candidate.end)
        pieces.append(text[position:])
        return "".join(pieces)


def _key_order(key: Any) -> bytes:
    if not isinstance(key, str):
        raise ValueError(f"cannot clean an object with a non-string key {key!r}")
    return _utf16(key)


def _keyword_hits(
    keywords: re.Pattern[str], shadow: Shadow, placeholders: _Spans
) -> tuple[list[int], list[int]]:
    starts: list[int] = []
    ends: list[int] = []
    for match in keywords.finditer(shadow.lower):
        start, end = match.span(1)
        if not placeholders.overlaps(start, end):
            starts.append(start)
            ends.append(end)
    return starts, ends


def _near(hits: tuple[list[int], list[int]], start: int, end: int, window: int) -> bool:
    """Whether a keyword hit lies within ``window`` characters of [start, end)."""
    starts, ends = hits
    index = bisect_left(starts, end + window)
    return index > 0 and ends[index - 1] > start - window


def _resolve(candidates: list[_Candidate], shadow: Shadow) -> list[_Candidate]:
    """Longest first, then rule order, then position; overlaps are dropped.

    Lengths are shadow lengths, which are the same in both SDKs; overlaps are
    judged on the original characters, since two shadow spans can come from
    one original character (a ligature).
    """
    taken = _Spans()
    kept: list[tuple[int, _Candidate]] = []
    for candidate in sorted(
        candidates, key=lambda c: (c.start - c.end, c.rule.order, c.start)
    ):
        start, end = shadow.start(candidate.start), shadow.end(candidate.end)
        if taken.overlaps(start, end):
            continue
        taken.add(start, end)
        insort(kept, (start, candidate), key=lambda item: item[0])
    return [candidate for _, candidate in kept]


def _rebuild(payload: Any, replacements: dict[tuple[Any, ...], str]) -> Any:
    """A copy of ``payload`` with strings replaced, sharing every untouched branch."""
    if not replacements:
        return payload
    tree: dict[Any, Any] = {}
    for path, text in replacements.items():
        if not path:
            return text
        node = tree
        for step in path[:-1]:
            node = node.setdefault(step, {})
        node[path[-1]] = text
    return _apply(payload, tree)


def _apply(node: Any, tree: dict[Any, Any]) -> Any:
    copy: Any = dict(node) if isinstance(node, dict) else list(node)
    for step, change in tree.items():
        copy[step] = change if isinstance(change, str) else _apply(node[step], change)
    return copy


def reference_policy() -> dict[str, Any]:
    from idside._cleaning.rules import CATALOGUE_IDS

    return {
        "schema": 1,
        "app_id": "",
        "revision": 0,
        "detectors": dict.fromkeys(CATALOGUE_IDS, "mask"),
        "attachments": "allow",
        "custom": [],
    }
