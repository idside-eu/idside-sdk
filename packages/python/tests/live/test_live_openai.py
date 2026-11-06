"""Live checks against the real OpenAI API.

OpenAI is the only one of the five whose public API exposes genuine per-request
retention levers, so it is the only provider where ``params_forced`` changes what
the provider does rather than merely what the ledger records. ``openai.json``
declares ``store`` and ``prompt_cache_retention`` forceable on
``chat.completions.create``; both are confirmed here against the live API.

Two independent guards stop the cloud inventing a parameter, and both are pinned
below: the pinned client refuses unknown keyword arguments before building a
request, and the API refuses unknown fields with a 400.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from idside import consent
from idside.providers import load_descriptors

from .conftest import (
    TIMEOUT,
    TINY_PROMPT,
    USER_ID,
    StubConsentClient,
    WireLog,
    assert_response_ok,
    require_key,
    skip_if_unaffordable,
    tolerate_billing,
)

CHAT_URL = "https://api.openai.com/v1/chat/completions"

#: Cheapest non-reasoning chat model. Deliberately not a ``gpt-5`` model: those
#: spend reasoning tokens before emitting any, so ``max_tokens: 1`` fails with
#: "model output limit was reached" and the smallest honest request stops being
#: the smallest. Override when OpenAI retires it.
MODEL = os.environ.get("IDSIDE_LIVE_OPENAI_MODEL", "gpt-4.1-nano")


def _client(wire: WireLog) -> Any:
    openai = pytest.importorskip("openai")
    return openai.OpenAI(
        api_key=require_key("OPENAI_API_KEY"),
        http_client=wire.httpx_client(),
        max_retries=0,  # one call per test: keeps cost and the wire log honest
    )


def _call(client: Any) -> Any:
    return client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": TINY_PROMPT}],
        max_tokens=1,
        user_id=USER_ID,
    )


def _raw(body: dict[str, Any]) -> httpx.Response:
    return httpx.post(
        CHAT_URL,
        headers={"Authorization": f"Bearer {require_key('OPENAI_API_KEY')}"},
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "max_tokens": 1,
            **body,
        },
        timeout=TIMEOUT,
    )


@pytest.mark.parametrize(
    ("param", "value"),
    [("store", False), ("prompt_cache_retention", "24h")],
)
def test_openai_accepts_every_param_the_descriptor_declares_forceable(
    wire: WireLog, param: str, value: Any
) -> None:
    """Each declared lever is a parameter the live API really implements.

    An unrecognised field answers 400 here (pinned below), so a 200 is the
    signal that the allowlist in ``openai.json`` names something real rather
    than something plausible.
    """
    client = consent.wrap(
        _client(wire), consent_client=StubConsentClient({param: value})
    )
    try:
        response = _call(client)
    except Exception as exc:
        skip_if_unaffordable(exc)
        raise

    assert response.id
    assert wire.last.body[param] == value


def test_openai_declared_forceable_params_match_what_we_tested() -> None:
    """Guard against this file drifting from the descriptor it is verifying.

    If a parameter is added to the allowlist without a live check, this fails
    and asks for one — which is the whole point of the allowlist being an
    allowlist.
    """
    site = load_descriptors()["openai"].call_site("chat.completions.create")
    assert site is not None
    assert site.forceable_params == frozenset({"store", "prompt_cache_retention"})


def test_openai_receives_exactly_the_params_we_intended(wire: WireLog) -> None:
    """What leaves the process: forced params in the body, ACT token in a header."""
    client = consent.wrap(
        _client(wire), consent_client=StubConsentClient({"store": False})
    )
    with tolerate_billing():
        _call(client)

    sent = wire.last
    assert sent.url.startswith("https://api.openai.com/")
    assert sent.body["store"] is False
    assert sent.body["model"] == MODEL
    assert sent.header("X-AI-Consent-Token")
    # The identity kwarg is consumed by the SDK and must never reach OpenAI.
    assert "user_id" not in sent.body


def test_openai_protected_params_are_dropped_before_the_call(wire: WireLog) -> None:
    """The cloud cannot repoint the request at another model.

    ``model`` is in the descriptor's ``protected_params``; a cloud that tries to
    override it must be ignored. That denylist is why a compromised or buggy
    policy engine cannot rewrite the request it is only meant to annotate.
    """
    client = consent.wrap(
        _client(wire),
        consent_client=StubConsentClient(
            {"model": "idside-not-a-real-model", "store": False}
        ),
    )
    with tolerate_billing():
        _call(client)

    sent = wire.last
    assert sent.body["model"] == MODEL, "protected param 'model' was overridden"
    assert sent.body["store"] is False, "non-protected param should still be applied"


def test_openai_client_refuses_a_forced_param_outside_its_signature(
    wire: WireLog,
) -> None:
    """First guard: the pinned client has a closed signature, so nothing is sent.

    ``Completions.create()`` takes a fixed set of keyword arguments. A cloud
    forcing anything outside it raises ``TypeError`` locally — fail-closed, and
    before a single byte reaches OpenAI.
    """
    client = consent.wrap(
        _client(wire),
        consent_client=StubConsentClient({"idside_not_a_real_param": True}),
    )
    with pytest.raises(TypeError, match="idside_not_a_real_param"):
        _call(client)
    assert not wire.requests, "must fail closed: nothing may reach the provider"


def test_openai_api_rejects_a_parameter_it_does_not_know() -> None:
    """Second guard: even bypassing the client, the API refuses the field.

    Checked over raw HTTP because the client never lets such a request out. It
    matters independently: it is what would happen to any SDK, in any language,
    that trusted a bad allowlist — a loud 400, not silent non-enforcement.
    """
    response = _raw({"idside_not_a_real_param": True})
    assert response.status_code == 400, response.text
    assert "Unrecognized request argument" in response.text


def test_openai_store_false_is_the_default_but_still_worth_asserting() -> None:
    """``store`` accepts both values, so the cloud can express either answer.

    Pinned because "the default is already false" is a tempting reason to skip
    sending it — and a wrong one: a default is a provider's choice, revocable
    without notice, while a parameter on the request is the app's instruction
    and is what the request hash commits to.
    """
    for value in (False, True):
        response = _raw({"store": value})
        assert_response_ok(response)
