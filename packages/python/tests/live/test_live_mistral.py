"""Live checks against the real Mistral API (La Plateforme).

The ``mistral`` descriptor declares ``forceable_params: []`` for every one of its
six call sites. These tests confirm the two independent reasons that is correct —
the server refuses unknown body fields (422), and the generated client refuses
unknown kwargs before a request is even built — and confirm that the ACT token
still gets through, which is what makes the provider usable at all.
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
    require_key,
    skip_if_unaffordable,
)

CHAT_URL = "https://api.mistral.ai/v1/chat/completions"

#: Cheapest hosted model.
MODEL = os.environ.get("IDSIDE_LIVE_MISTRAL_MODEL", "ministral-3b-latest")


def _client(wire: WireLog) -> Any:
    mistralai = pytest.importorskip("mistralai.client")
    return mistralai.Mistral(
        api_key=require_key("MISTRAL_API_KEY"), client=wire.httpx_client()
    )


def _raw(body: dict[str, Any]) -> httpx.Response:
    return httpx.post(
        CHAT_URL,
        headers={
            "Authorization": f"Bearer {require_key('MISTRAL_API_KEY')}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "max_tokens": 1,
            **body,
        },
        timeout=TIMEOUT,
    )


def test_mistral_accepts_our_act_token_header(wire: WireLog) -> None:
    """The realistic case: nothing forced, token in ``http_headers``, call works.

    With ``forceable_params: []`` this is the whole of Mistral enforcement — the
    proof and the ledger entry, not a provider-side behaviour change.
    """
    client = consent.wrap(_client(wire), consent_client=StubConsentClient())
    try:
        response = client.chat.complete(
            model=MODEL,
            messages=[{"role": "user", "content": TINY_PROMPT}],
            max_tokens=1,
            user_id=USER_ID,
        )
    except Exception as exc:
        skip_if_unaffordable(exc)
        raise

    assert response.model
    assert wire.last.header("X-AI-Consent-Token")
    assert "user_id" not in wire.last.body


def test_mistral_rejects_unknown_body_params() -> None:
    """422 ``extra_forbidden`` — the server refuses fields it does not know."""
    response = _raw({"idside_not_a_real_param": "x"})
    assert response.status_code == 422, response.text
    assert "extra_forbidden" in response.text


def test_mistral_rejects_openai_store_param() -> None:
    """``store`` is an OpenAI-ism and Mistral refuses it outright.

    The reason ``params_forced`` has to be decided per provider and never shared:
    the parameter that expresses "do not retain this" at OpenAI is a 422 here.
    """
    response = _raw({"store": False})
    assert response.status_code == 422, response.text
    assert "store" in response.text


def test_mistral_forced_param_dies_in_the_client_signature(wire: WireLog) -> None:
    """The generated client has a closed signature — no request is even built.

    A second, earlier guard than the server's 422: ``Chat.complete()`` takes a
    fixed set of keyword arguments, so a forced parameter outside it raises
    ``TypeError`` locally. Fails closed, but as a bare ``TypeError``.
    """
    client = consent.wrap(
        _client(wire), consent_client=StubConsentClient({"store": False})
    )
    with pytest.raises(TypeError, match="unexpected keyword argument 'store'"):
        client.chat.complete(
            model=MODEL,
            messages=[{"role": "user", "content": TINY_PROMPT}],
            max_tokens=1,
            user_id=USER_ID,
        )
    assert not wire.requests, "must fail closed: nothing may reach the provider"


def test_mistral_us_endpoint_is_gated_by_the_account() -> None:
    """Residency is a different host, and permission for it is account-level.

    Worth pinning because it is the shape of the general finding: a control that
    exists, but not as anything a request can assert. An unapproved account is
    refused at the door, so the SDK cannot offer per-user residency here.
    """
    response = httpx.post(
        "https://api.us.mistral.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {require_key('MISTRAL_API_KEY')}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "max_tokens": 1,
        },
        timeout=TIMEOUT,
    )
    assert response.status_code in (200, 403), response.text
    if response.status_code == 403:
        assert "regional" in response.text.lower()
