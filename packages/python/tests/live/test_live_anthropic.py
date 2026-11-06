"""Live checks against the real Anthropic Messages API.

Two descriptor claims are settled here.

1. ``param_target: "kwargs"`` — a forced parameter belongs in the JSON body,
   which is the only place Anthropic reads one. The body schema is strict, so
   anything sent there must be a real parameter of the endpoint; unknown
   *headers* are tolerated, which is the separate fact the ACT token rests on.
2. ``inference_geo`` is a real per-request parameter, and it is a **body** one.
   The descriptor injected it into headers until 2026-07-26, which sent it where
   Anthropic does not read it: the call succeeded and the residency choice did
   nothing, while the public matrix showed a ✅.

What is **not** claimed here any more: that ``data_residency`` is enforced. It
was downgraded to ``account`` on 2026-07-27 and ``forceable_params`` is now
empty, because ``inference_geo`` chooses between ``global`` and ``us`` and
neither answers *keep processing inside my region* for a user whose region we
never learn. So ``inference_geo`` appears below as the body parameter it is —
proof that ``param_target: "kwargs"`` reaches the right place — and not as an
enforcement of anyone's consent.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from idside import consent

from .conftest import (
    TIMEOUT,
    TINY_PROMPT,
    USER_ID,
    StubConsentClient,
    WireLog,
    assert_response_ok,
    require_key,
    skip_if_unaffordable,
)

MESSAGES_URL = "https://api.anthropic.com/v1/messages"

#: Cheapest model. Predates ``inference_geo``, which is Claude 4.6 and later.
MODEL = os.environ.get("IDSIDE_LIVE_ANTHROPIC_MODEL", "claude-haiku-4-5")

#: A model recent enough to accept ``inference_geo``.
GEO_MODEL = os.environ.get("IDSIDE_LIVE_ANTHROPIC_GEO_MODEL", "claude-sonnet-4-6")


def _client(wire: WireLog) -> Any:
    anthropic = pytest.importorskip("anthropic")
    return anthropic.Anthropic(
        api_key=require_key("ANTHROPIC_API_KEY"),
        http_client=wire.httpx_client(),
        max_retries=0,
    )


def _raw(body: dict[str, Any]) -> httpx.Response:
    """POST a body we control byte for byte, bypassing the SDK."""
    return httpx.post(
        MESSAGES_URL,
        headers={
            "x-api-key": require_key("ANTHROPIC_API_KEY"),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "max_tokens": 1,
            **body,
        },
        timeout=TIMEOUT,
    )


def test_anthropic_accepts_our_act_token_header(wire: WireLog) -> None:
    """The ACT token rides in an unknown header and the call still succeeds.

    This is the load-bearing fact behind ``param_target: "headers"``: Anthropic
    ignores headers it does not know instead of refusing the request.
    """
    client = consent.wrap(_client(wire), consent_client=StubConsentClient())
    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": TINY_PROMPT}],
            max_tokens=1,
            user_id=USER_ID,
        )
    except Exception as exc:
        skip_if_unaffordable(exc)
        raise

    assert response.id
    assert wire.last.header("X-AI-Consent-Token")
    assert "user_id" not in wire.last.body


def test_anthropic_rejects_unknown_body_params() -> None:
    """The body schema is strict — which is why the allowlist has to be right.

    ``param_target`` is ``kwargs``, so anything the cloud forces lands in this
    strict body. That is safe only because ``forceable_params`` is checked
    against the real client signature (``test_forceable_params.py``); a
    parameter that got past it would 400 every call for the users who opted out.
    The ACT token is unaffected — it rides in a header, and unknown headers are
    tolerated, which is the separate fact ``token_header`` rests on.
    """
    response = _raw({"idside_not_a_real_param": "x"})
    assert response.status_code == 400, response.text
    assert "not permitted" in response.text.lower()


def test_anthropic_inference_geo_is_a_real_per_request_lever() -> None:
    """``inference_geo`` genuinely pins where inference runs, and says so back.

    Sent in the **body**, on a model recent enough to support it, the answer
    echoes the geography in ``usage.inference_geo``. That echo is what makes it
    the only verifiable per-request privacy control across the five providers.
    """
    response = httpx.post(
        MESSAGES_URL,
        headers={
            "x-api-key": require_key("ANTHROPIC_API_KEY"),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": GEO_MODEL,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "max_tokens": 1,
            "inference_geo": "us",
        },
        timeout=TIMEOUT,
    )
    assert_response_ok(response)
    assert response.json()["usage"]["inference_geo"] == "us"


def test_anthropic_refuses_inference_geo_on_older_models() -> None:
    """The cheap model does not support it — a 400, not a silent no-op.

    Forcing ``inference_geo`` for a user who opted into EU-only processing would
    therefore break every call on Claude 4.5 and earlier. The cloud has to gate
    it on the model, not just on the provider.
    """
    response = _raw({"inference_geo": "us"})
    assert response.status_code == 400, response.text
    assert "inference_geo" in response.text


def test_anthropic_forced_inference_geo_reaches_the_body_and_applies(
    wire: WireLog,
) -> None:
    """A forced parameter reaches the body, and the provider acts on it.

    With ``param_target: "headers"`` this same call put ``inference_geo`` in a
    header Anthropic ignores: it left the process, the call succeeded, and
    inference still ran in the default geography — the worst possible outcome,
    because it *looked* enforced. The assertions below are the three things that
    were wrong, in order.

    ``inference_geo`` is the vehicle, not the point: no consent option forces it
    since 2026-07-27. What is pinned is that a value the cloud returns in
    ``params_forced`` arrives where the provider reads it — the property every
    future Anthropic lever will depend on.
    """
    client = consent.wrap(
        _client(wire), consent_client=StubConsentClient({"inference_geo": "us"})
    )
    try:
        response = client.messages.create(
            model=GEO_MODEL,
            messages=[{"role": "user", "content": TINY_PROMPT}],
            max_tokens=1,
            user_id=USER_ID,
        )
    except Exception as exc:
        skip_if_unaffordable(exc)
        raise

    assert wire.last.header("inference_geo") is None, "no longer a header"
    assert wire.last.body["inference_geo"] == "us", "reached the body"
    assert wire.last.header("X-AI-Consent-Token"), "the token still rides in one"
    assert response.usage.inference_geo == "us", (
        "Anthropic applied the forced value, and says so back"
    )


def test_anthropic_param_outside_the_allowlist_fails_before_the_call(
    wire: WireLog,
) -> None:
    """A parameter this endpoint does not have dies in the client, not on the wire.

    Anthropic has no ``store``. Under ``param_target: "headers"`` a cloud
    answering ``{"store": false}`` raised a bare ``TypeError`` from httpx
    ("Header value must be str or bytes") — fail-closed, but naming httpx rather
    than the misconfiguration. As a kwarg it is the provider's own client that
    refuses it, by name, which is the signal an operator can act on at three in
    the morning. Either way nothing reaches Anthropic.
    """
    client = consent.wrap(
        _client(wire), consent_client=StubConsentClient({"store": False})
    )
    with pytest.raises(TypeError, match="store"):
        client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": TINY_PROMPT}],
            max_tokens=1,
            user_id=USER_ID,
        )
    assert not wire.requests, "must fail closed: nothing may reach the provider"
