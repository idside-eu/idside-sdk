"""The shared prompt-cleaning vectors, replayed by the Python engine.

`packages/descriptors/testdata/content-cleaning-vectors.json` is replayed by both
SDKs; the Node suite runs the same file. Its expectations are hand-written
templates in `packages/descriptors/build_content_vectors.py`, never recordings
of an engine, and the first test below fails when the file and its generator
drift apart.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from idside._cleaning import (
    ContentCleaner,
    challenge_response,
    clean,
    content_policy_digest,
    verify_clean,
)
from idside.exceptions import ContentPolicyError

_ROOT = Path(__file__).resolve().parents[3]
_DESCRIPTORS = _ROOT / "packages" / "descriptors"
_VECTORS_PATH = _DESCRIPTORS / "testdata" / "content-cleaning-vectors.json"


def _decode(value: Any) -> Any:
    """Join every `{"$concat": [...]}` back into the string it stands for."""
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


_RAW: dict[str, Any] = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))
_VECTORS: dict[str, Any] = {
    key: _decode(value) if key != "definitions" else value
    for key, value in _RAW.items()
}


def _input(case: dict[str, Any]) -> Any:
    return case["input"]


def _output(case: dict[str, Any]) -> Any:
    return case["output"]


def _policy(case: dict[str, Any]) -> dict[str, Any]:
    policy = case["policy"]
    return _VECTORS["policies"][policy] if isinstance(policy, str) else policy


def test_vectors_file_matches_its_generator() -> None:
    builder = _DESCRIPTORS / "build_content_vectors.py"
    spec = importlib.util.spec_from_file_location("build_content_vectors", builder)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_content_vectors"] = module
    spec.loader.exec_module(module)
    assert module.render() == _VECTORS_PATH.read_text(encoding="utf-8"), (
        "content-cleaning-vectors.json is stale; "
        "run `python packages/descriptors/build_content_vectors.py`."
    )


def test_there_are_hundreds_of_cases_and_every_detector_has_a_positive() -> None:
    cases = _VECTORS["cases"]
    assert len(cases) >= 300
    rules = json.loads((_DESCRIPTORS / "content-rules.json").read_text("utf-8"))
    counted = {key for case in cases for key in case["counts"]}
    for detector in rules["detectors"]:
        assert detector["id"] in counted, f"no vector masks {detector['id']}"


@pytest.mark.parametrize("case", _VECTORS["cases"], ids=lambda case: case["name"])
def test_vector(case: dict[str, Any]) -> None:
    payload = _input(case)
    policy = _policy(case)
    provider = case["provider"]
    if case.get("error") == "policy":
        with pytest.raises(ContentPolicyError):
            clean(payload, policy, provider=provider)
        return
    result = clean(payload, policy, provider=provider)
    assert dict(result.blocked) == case["blocked"]
    assert result.attachments == case["attachments"]
    if case["blocked"]:
        assert not verify_clean(payload, policy, provider=provider)
        return
    assert result.payload == _output(case)
    assert dict(result.counts) == case["counts"]
    # The replay property: what left is a fixed point of the same policy.
    again = clean(result.payload, policy, provider=provider)
    assert again.payload == result.payload
    assert dict(again.counts) == {}
    assert verify_clean(result.payload, policy, provider=provider)
    assert verify_clean(payload, policy, provider=provider) is (not case["counts"])


@pytest.mark.parametrize("challenge", _VECTORS["challenges"])
def test_challenge_response(challenge: dict[str, Any]) -> None:
    assert (
        challenge_response(challenge["token"], challenge["inputs"])
        == challenge["response"]
    )
    cleaner = ContentCleaner.reference()
    assert [cleaner.clean(text).payload for text in challenge["inputs"]] == challenge[
        "outputs"
    ]


@pytest.mark.parametrize("entry", _VECTORS["policy_digests"])
def test_policy_digest(entry: dict[str, Any]) -> None:
    assert content_policy_digest(entry["policy"]) == entry["digest"]
    canonical = json.dumps(
        entry["policy"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert (
        entry["digest"]
        == "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )
