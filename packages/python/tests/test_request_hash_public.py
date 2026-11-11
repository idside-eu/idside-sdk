"""The public ``request_hash()`` — what a verifier pins ``verify_act()`` to.

Node has exported ``requestHash()`` since its first release; Python computed the
same value only inside the enforcement path, so a Python verifier had no
supported way to obtain the ``request_hash=`` its quickstart tells it to pin.
These tests hold the public function to three things: it is exported, it
reproduces the shared cross-SDK vectors exactly as Node does (identity and
headers stripped by the caller), and it is the very value the enforcement path
sends to ``/v1/authorize`` for the same payload.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import idside
from idside import request_hash
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


def test_request_hash_is_exported_from_the_package_root() -> None:
    assert "request_hash" in idside.__all__
    assert idside.request_hash is request_hash


@pytest.mark.parametrize(
    "vector", _vectors(), ids=lambda v: v["description"][:60].replace(" ", "-")
)
def test_request_hash_reproduces_the_shared_vectors(vector: dict[str, Any]) -> None:
    # Same contract as Node's `requestHash()`: the caller leaves out the
    # identity and the per-request headers, which the shared file marks.
    payload = dict(vector["payload"])
    for key in vector.get("sdk_strips", []):
        del payload[key]
    assert request_hash(payload) == vector["expected_hash"]


def test_the_quickstart_payload_hashes_to_the_documented_value() -> None:
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Hello!"}],
    }
    assert request_hash(payload) == (
        "sha256:3ff33a93d528feaaa859078c68af6a379567b262ed5600b5bd91300673f5cf76"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hi"}]},
        {"temperature": 0.2, "model": "gpt-4o", "max_tokens": 64, "stream": False},
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "Café 😀"}]},
    ],
)
def test_it_is_the_hash_the_enforcement_path_sends(payload: dict[str, Any]) -> None:
    """The value a verifier computes is the value the cloud signed.

    The enforcement path receives the call's kwargs, identity and headers
    included, and leaves those two out; the public function receives the payload
    without them. For the same request they must agree to the byte.
    """
    adapter = ProviderAdapter(load_descriptors()["openai"])
    sent = adapter.extract_request_metadata(
        **payload,
        user_id="usr_marie_456",
        extra_headers={"X-Request-Id": "req_123"},
    )["request_hash"]
    assert request_hash(payload) == sent


def test_key_order_does_not_move_the_hash() -> None:
    assert request_hash({"model": "m", "stream": True}) == request_hash(
        {"stream": True, "model": "m"}
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "m", "temperature": float("nan")},
        {"model": "m", "when": object()},
        {1: "a non-string key"},
    ],
)
def test_a_value_outside_json_raises_value_error(payload: Any) -> None:
    """No silent ``repr()``: a hash nobody else can reproduce is worse than none."""
    with pytest.raises(ValueError):
        request_hash(payload)
