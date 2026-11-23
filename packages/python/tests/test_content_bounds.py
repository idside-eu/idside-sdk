"""A value that runs on is taken whole, to the engine's limit, in linear time.

`testdata/content-open-values.json` writes, for each rule whose value can run
on, a text that the value fills: 1 MiB, 5 MiB and exactly the engine's limit
(8,388,608 characters). Each must come out as what stood before the value, one
placeholder and what stood after it, counted once, within seconds; one
character more is blocked `size_limit`, never skipped. The Node suite
(`contentBounds.test.ts`) reads the same file: there, a pattern whose matcher
keeps a frame per repetition overflowed the stack past 5.6 million characters.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from idside._cleaning import ContentCleaner

_OPEN: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "descriptors"
        / "testdata"
        / "content-open-values.json"
    ).read_text(encoding="utf-8")
)
_PLACEHOLDER = re.compile(r"\[[A-Z0-9_]{1,24}_1\]")


def _cleaner(form: dict[str, Any]) -> ContentCleaner:
    if "custom" in form:
        return ContentCleaner({"schema": 1, "custom": [form["custom"]]})
    return ContentCleaner({"schema": 1, "detectors": {form["rule"]: "mask"}})


def _head(form: dict[str, Any]) -> str:
    """The start of the value, written in parts where a scanner would flag it."""
    head = form["head"]
    return head if isinstance(head, str) else "".join(head)


def _text(form: dict[str, Any], size: int) -> str:
    head = _head(form)
    fixed = len(form["before"]) + len(head) + len(form["after"])
    return form["before"] + head + form["fill"] * (size - fixed) + form["after"]


_FORMS = [
    pytest.param(form, id=f"{form['rule']}: {form['before'] + _head(form)!r}")
    for form in _OPEN["forms"]
]


@pytest.mark.parametrize("form", _FORMS)
def test_a_value_that_fills_the_text_is_taken_whole(form: dict[str, Any]) -> None:
    cleaner = _cleaner(form)
    for size in _OPEN["sizes"]:
        text = _text(form, size)
        assert len(text) == size
        began = time.perf_counter()
        result = cleaner.clean(text)
        elapsed = time.perf_counter() - began
        assert result.counts == {form["rule"]: 1}, size
        output = result.payload
        assert output.startswith(form["before"]) and output.endswith(form["after"])
        middle = output[len(form["before"]) : len(output) - len(form["after"])]
        assert _PLACEHOLDER.fullmatch(middle), (size, middle[:40])
        assert elapsed < 10, f"{size}: {elapsed:.2f} s"


@pytest.mark.parametrize("form", _FORMS)
def test_one_character_past_the_limit_is_blocked(form: dict[str, Any]) -> None:
    result = _cleaner(form).clean(_text(form, _OPEN["past_limit"]))
    assert result.blocked == {"size_limit": 1} and result.counts == {}


def test_every_rule_that_follows_a_value_has_a_form() -> None:
    rules = json.loads(
        (
            Path(__file__).resolve().parents[2] / "descriptors" / "content-rules.json"
        ).read_text(encoding="utf-8")
    )
    open_rules = {
        detector["id"]
        for detector in rules["detectors"]
        if any("extend" in pattern for pattern in detector["patterns"])
    }
    assert open_rules <= {form["rule"] for form in _OPEN["forms"]}


# ── slow shapes found before the release, read in linear time since ────────

#: A BEGIN line (written in parts, for the secret scanners) followed by four
#: header lines whose values hold `<br>` again and again, and no key: the
#: header value and the separators after it can both take `>` and `<br>`, so
#: each line can be split in as many places as it holds `<br>`.
_BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----\n"


def _best_of(runs: int, clean: Any, text: str) -> float:
    best = float("inf")
    for _ in range(runs):
        began = time.perf_counter()
        clean(text)
        best = min(best, time.perf_counter() - began)
    return best


def test_header_lines_full_of_line_breaks_are_read_in_linear_time() -> None:
    text = _BEGIN + "".join("Comment: " + "<br>" * 32 + "\n" for _ in range(4)) + "!!"
    cleaner = ContentCleaner({"schema": 1, "detectors": {"private_key": "mask"}})
    assert _best_of(1, cleaner.clean, text) < 0.25


def test_many_findings_are_resolved_in_linear_time() -> None:
    def text(size: int) -> str:
        return " 1.1.1.1" * (size // 16) + " 10.10.10.10" * (size // 24)

    cleaner = ContentCleaner({"schema": 1, "detectors": {"ip_address": "mask"}})
    small = _best_of(3, cleaner.clean, text(512 * 1024))
    large = _best_of(1, cleaner.clean, text(4 * 1024 * 1024))
    # Eight times the text: about eight times the time when linear, sixty-four
    # when quadratic.
    assert large / small < 16, f"{small:.2f} s, then {large:.2f} s"
