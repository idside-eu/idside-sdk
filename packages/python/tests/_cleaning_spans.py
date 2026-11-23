"""Which characters of a text an engine masks: the oracle's measure.

`masked()` replays an engine's own passes (its `_find`, `_resolve` and shadow)
and follows each character of the original text through them, placeholders
included, so that what a later pass masks is traced back to the original. The
text it rebuilds is checked against the engine's own `clean()`: the replay is
the engine, or the test fails. Written the same way in the Node suite
(`test/cleaningSpans.ts`).
"""

from __future__ import annotations

import types
from typing import Any


class _Spans:
    """Sorted, non-overlapping intervals (what the engines' `_find` reads)."""

    def __init__(self) -> None:
        self.items: list[tuple[int, int]] = []

    def add(self, start: int, end: int) -> None:
        self.items.append((start, end))

    def overlaps(self, start: int, end: int) -> bool:
        return any(low < end and start < high for low, high in self.items)


def masked(
    module: types.ModuleType, cleaner: Any, text: str
) -> tuple[bytearray | None, str]:
    """(one byte per original character, 1 where masked; the cleaned text).

    ``None`` when the engine blocks the text (a block rule, or its own limits).
    """
    limits = module.LIMITS
    placeholder = module.PLACEHOLDER
    floors: dict[str, int] = {}
    for match in placeholder.finditer(module.shadow_of(text).text):
        label, _, number = match.group()[1:-1].rpartition("_")
        floors[label] = max(floors.get(label, 0), int(number))
    assigned: dict[tuple[str, str], str] = {}
    following: dict[str, int] = {}
    hidden = bytearray(len(text))
    # For each character of the current text, the original interval it stands for.
    low = list(range(len(text)))
    high = [index + 1 for index in range(len(text))]
    current = text
    for _ in range(limits["max_passes"]):
        shadow = module.shadow_of(current)
        holders = _Spans()
        for match in placeholder.finditer(shadow.text):
            holders.add(match.start(), match.end())
        masks, blocks = cleaner._find(shadow, holders)
        if module._resolve(blocks, shadow):
            return None, text
        if not masks:
            break
        pieces: list[str] = []
        new_low: list[int] = []
        new_high: list[int] = []
        position = 0
        for candidate in module._resolve(masks, shadow):
            start, end = shadow.start(candidate.start), shadow.end(candidate.end)
            label = candidate.rule.label
            value = (label, shadow.lower[candidate.start : candidate.end])
            if value not in assigned:
                number = following.get(label, floors.get(label, 0) + 1)
                following[label] = number + 1
                assigned[value] = f"[{label}_{number}]"
            stands = assigned[value]
            first, last = min(low[start:end]), max(high[start:end])
            hidden[first:last] = b"\x01" * (last - first)
            pieces.append(current[position:start])
            new_low += low[position:start]
            new_high += high[position:start]
            pieces.append(stands)
            new_low += [first] * len(stands)
            new_high += [last] * len(stands)
            position = end
        pieces.append(current[position:])
        new_low += low[position:]
        new_high += high[position:]
        current, low, high = "".join(pieces), new_low, new_high
    else:
        return None, text
    return hidden, current
