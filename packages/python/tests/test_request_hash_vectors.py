"""Replays the shared request-hash test vectors against the Python engine.

The vectors live in ``packages/descriptors/testdata/request-hash-vectors.json``
and are shared by every SDK: one payload, one RFC 8785 (JCS) canonical form, one
SHA-256. Python and the future Node SDK must produce bit-identical hashes, or the
``request_hash`` bound into ACT tokens loses its evidential value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from idside.adapters import ProviderAdapter
from idside.providers import load_descriptors

_VECTORS_PATH = (
    Path(__file__).resolve().parents[2]
    / "descriptors"
    / "testdata"
    / "request-hash-vectors.json"
)


def _vectors() -> list[dict[str, Any]]:
    data = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))
    return data["vectors"]  # type: ignore[no-any-return]


def test_vector_suite_is_rich_enough() -> None:
    vectors = _vectors()
    assert len(vectors) >= 8
    for vector in vectors:
        assert vector["description"], "every vector must be documented"
        assert vector["expected_hash"].startswith("sha256:")
        assert len(vector["expected_hash"]) == len("sha256:") + 64


@pytest.mark.parametrize(
    "vector", _vectors(), ids=lambda v: v["description"][:60].replace(" ", "-")
)
def test_request_hash_matches_shared_vector(vector: dict[str, Any]) -> None:
    adapter = ProviderAdapter(load_descriptors()["openai"])
    meta = adapter.extract_request_metadata(**vector["payload"])
    assert meta["request_hash"] == vector["expected_hash"]
