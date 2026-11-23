"""Never less than before: the non-regression oracle both suites hold.

A frozen reference engine and its catalogue (`testdata/reference-engine/`, never
edited) clean a generated corpus — every positive value of the shared vectors
between every prefix and suffix of `testdata/content-oracle.json`, and every
string input of the shared cases — and so does the engine under test. Every
character the reference masked must still be masked: the engine may mask more,
never less, except where a named exclusion of that file stands. A pattern
changed so that it takes a value only in some contexts shows here, whichever
context it forgot. The Node suite (`contentOracle.test.ts`) builds the same
corpus, checked by its digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import _reference_engine
import idside._cleaning.engine as engine_module
from _cleaning_spans import masked
from idside._cleaning import ContentCleaner
from idside.exceptions import ContentPolicyError

_TESTDATA = Path(__file__).resolve().parents[2] / "descriptors" / "testdata"
_ORACLE: dict[str, Any] = json.loads(
    (_TESTDATA / "content-oracle.json").read_text(encoding="utf-8")
)
_VECTORS: dict[str, Any] = json.loads(
    (_TESTDATA / "content-cleaning-vectors.json").read_text(encoding="utf-8")
)

#: The reference engine's files, as they were frozen: never edited. The SHA-256 of
#: each file name, a zero byte, its length, a zero byte and its bytes, by name.
FROZEN_FILES = (
    "content-rules.json",
    *(f"python/{name}.py" for name in _reference_engine.MODULES),
    *(f"node/{name}.ts" for name in _reference_engine.MODULES),
)
FROZEN = (
    "43899082580b47afe19b7ec4f2115773"  # pragma: allowlist secret
    "8c2607973ee3c2f8dc0f070616cf4728"  # pragma: allowlist secret
)


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$concat"}:
            return "".join(
                part if isinstance(part, str) else part["repeat"] * part["times"]
                for part in value["$concat"]
            )
        return {key: _decode(item) for key, item in value.items()}
    return value


def compose(positive: dict[str, str], prefix: str, suffix: str) -> str:
    """A positive value between ``prefix`` and ``suffix`` (``placement``)."""
    lead, value, trail = positive["lead"], positive["value"], positive["trail"]
    form = lead and (
        lead[-1] not in " \t\n"
        or lead.rstrip()[-1:] in ("=", ":")
        or lead.lower().endswith(("bearer ", "basic "))
    )
    left = prefix + lead if form else lead + prefix
    right = trail + suffix if trail[:1] in ("@", ":") else suffix + trail
    return left + value + right


def corpus() -> Iterator[tuple[str, str]]:
    """(detector, text), in the order the file states; each text once."""
    seen: set[tuple[str, str]] = set()
    for positive in _decode(_VECTORS["positives"]):
        for prefix in _ORACLE["prefixes"]:
            for suffix in _ORACLE["suffixes"]:
                item = (positive["detector"], compose(positive, prefix, suffix))
                if item not in seen:
                    seen.add(item)
                    yield item


_CORPUS = list(corpus())
_DETECTORS = sorted({detector for detector, _ in _CORPUS})


def _exclusions(detector: str | None) -> list[tuple[str, re.Pattern[str]]]:
    """The named exclusions of ``detector`` (of every detector: ``None``)."""
    return [
        (
            item["name"],
            re.compile(item["regex"], re.ASCII | (re.I if item["ignore_case"] else 0)),
        )
        for item in _ORACLE["exclusions"]
        if detector in (None, item["detector"])
    ]


def _lost(
    text: str, before: bytearray | None, after: bytearray | None
) -> list[int] | None:
    """Original indices the reference masked and the engine under test did not.

    ``None``: nothing lost (or the engine under test blocks, which sends less).
    """
    if after is None:
        return None
    if before is None:
        return list(range(len(text)))  # the reference blocked: send nothing more
    lost = [index for index, flag in enumerate(before) if flag and not after[index]]
    return lost or None


def _excused(text: str, lost: list[int], rules: list[tuple[str, Any]]) -> str | None:
    for name, regex in rules:
        spans = [match.span() for match in regex.finditer(text)]
        if all(any(start <= index < end for start, end in spans) for index in lost):
            return name
    return None


def frozen_digest() -> str:
    digest = hashlib.sha256()
    for name in sorted(FROZEN_FILES):
        data = (_reference_engine.ROOT / name).read_bytes()
        digest.update(f"{name}\x00{len(data)}\x00".encode() + data)
    return digest.hexdigest()


def test_the_reference_engine_is_the_frozen_one() -> None:
    assert frozen_digest() == FROZEN


def test_the_corpus_is_the_one_both_suites_build() -> None:
    digest = hashlib.sha256()
    for detector, text in _CORPUS:
        digest.update(f"{detector}\x00{text}\x00".encode())
    stated = _ORACLE["corpus"]
    # The first 48 bits of the digest, as a number.
    head = int(digest.hexdigest()[:12], 16)
    assert (len(_CORPUS), head) == (stated["texts"], stated["digest"])
    assert len(_CORPUS) >= 100_000


_USED: dict[str, int] = {}


@pytest.mark.parametrize("detector", _DETECTORS)
def test_the_engine_masks_whatever_the_reference_masked(detector: str) -> None:
    reference_module = _reference_engine.load()
    policy = {"schema": 1, "detectors": {detector: "mask"}}
    reference = reference_module.ContentCleaner(policy)
    engine = ContentCleaner(policy)
    rules = _exclusions(detector)
    failures = []
    for item_detector, text in _CORPUS:
        if item_detector != detector:
            continue
        before, _ = masked(reference_module, reference, text)
        after, cleaned = masked(engine_module, engine, text)
        lost = _lost(text, before, after)
        if lost is None:
            continue
        name = _excused(text, lost, rules)
        if name is not None:
            _USED[name] = _USED.get(name, 0) + 1
            continue
        failures.append(f"{text!r} -> {cleaned!r}")
    assert not failures, f"{len(failures)} regressions: {failures[:12]}"


def test_the_engine_masks_whatever_the_reference_masked_in_the_cases() -> None:
    reference_module = _reference_engine.load()
    policies = _decode(_VECTORS["policies"])
    failures = []
    for case in _decode(_VECTORS["cases"]):
        if case.get("provider") or not isinstance(case["input"], str):
            continue
        policy = case["policy"]
        policy = policies[policy] if isinstance(policy, str) else policy
        try:
            reference = reference_module.ContentCleaner(policy)
        except Exception:  # noqa: S112 - a policy the reference did not know
            continue
        try:
            engine = ContentCleaner(policy)
        except ContentPolicyError:
            continue  # refused: the call is not made at all, which sends less
        text = case["input"]
        before, _ = masked(reference_module, reference, text)
        after, cleaned = masked(engine_module, engine, text)
        lost = _lost(text, before, after)
        if lost is not None and _excused(text, lost, _exclusions(None)) is None:
            failures.append(f"{case['name']}: {text!r} -> {cleaned!r}")
    assert not failures, f"{len(failures)} regressions: {failures[:12]}"


def test_the_replay_is_the_engine() -> None:
    """`masked()` rebuilds exactly what `clean()` returns, for both engines."""
    reference_module = _reference_engine.load()
    for detector in _DETECTORS:
        policy = {"schema": 1, "detectors": {detector: "mask"}}
        pairs = (
            (reference_module, reference_module.ContentCleaner(policy)),
            (engine_module, ContentCleaner(policy)),
        )
        texts = [text for item, text in _CORPUS if item == detector][::97]
        for module, cleaner in pairs:
            for text in texts:
                hidden, cleaned = masked(module, cleaner, text)
                result = cleaner.clean(text)
                if hidden is None:
                    assert result.blocked
                else:
                    assert cleaned == result.payload, (detector, text)
