"""Live checks against the real DeepSeek API.

DeepSeek is reached through the ``openai`` client pointed at another host, and
``deepseek.json`` records that its ``forceable_params`` are "NOT VERIFIED LIVE
(the test account has no balance)". These tests settle the question anyway,
without spending anything, by exploiting the order in which DeepSeek validates a
request: the JSON body is deserialised **before** the balance is checked.

So a malformed *known* field answers 400, while an *unknown* field sails through
to the 402. That difference is the proof that DeepSeek does not reject unknown
top-level fields — it drops them. Which is the worst behaviour of the five for a
consent SDK: injecting ``store: false`` here would look like it worked.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from idside import consent
from idside.adapters import get_adapter_for

from .conftest import (
    TIMEOUT,
    TINY_PROMPT,
    USER_ID,
    StubConsentClient,
    WireLog,
    require_key,
    skip_if_unaffordable,
)

BASE_URL = "https://api.deepseek.com"
CHAT_URL = f"{BASE_URL}/chat/completions"

#: Cheapest model. DeepSeek retires model names abruptly — ``deepseek-chat`` and
#: ``deepseek-reasoner`` were withdrawn in July 2026 — so keep this overridable.
MODEL = os.environ.get("IDSIDE_LIVE_DEEPSEEK_MODEL", "deepseek-v4-flash")


def _client(wire: WireLog) -> Any:
    openai = pytest.importorskip("openai")
    return openai.OpenAI(
        api_key=require_key("DEEPSEEK_API_KEY"),
        base_url=BASE_URL,
        http_client=wire.httpx_client(),
        max_retries=0,
    )


def _raw(body: dict[str, Any]) -> httpx.Response:
    return httpx.post(
        CHAT_URL,
        headers={"Authorization": f"Bearer {require_key('DEEPSEEK_API_KEY')}"},
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "max_tokens": 1,
            **body,
        },
        timeout=TIMEOUT,
    )


def test_deepseek_is_reachable_and_advertises_models() -> None:
    """A free call that proves the key and the OpenAI-compatible surface work."""
    response = httpx.get(
        f"{BASE_URL}/models",
        headers={"Authorization": f"Bearer {require_key('DEEPSEEK_API_KEY')}"},
        timeout=TIMEOUT,
    )
    assert response.status_code == 200, response.text
    ids = [entry["id"] for entry in response.json()["data"]]
    assert ids, "DeepSeek advertised no models"


def test_deepseek_is_detected_by_base_url_not_by_client_class(wire: WireLog) -> None:
    """An ``OpenAI`` instance pointed at DeepSeek resolves the DeepSeek descriptor.

    The class is ``openai.OpenAI`` either way, so only the host can tell the two
    apart. Getting this wrong would file DeepSeek traffic under OpenAI's registry
    rules — an ACT token claiming a no-training guarantee that does not exist
    here, for data processed in China.
    """
    adapter = get_adapter_for(_client(wire))
    assert adapter.name == "deepseek"


def test_deepseek_validates_the_body_before_it_checks_the_balance() -> None:
    """The control that makes the next test meaningful.

    A *known* field with the wrong type is refused at deserialisation, ahead of
    any billing check. So a request that reaches the balance check is a request
    whose body DeepSeek already accepted.
    """
    response = _raw({"temperature": "definitely-not-a-float"})
    assert response.status_code == 400, response.text
    assert "deserialize" in response.text.lower()


def test_deepseek_silently_ignores_parameters_it_does_not_implement() -> None:
    """``store`` is accepted and dropped — never refused.

    Contrast every other provider in this suite: OpenAI 400s, Gemini 400s,
    Mistral 422s, Anthropic 400s. DeepSeek alone takes the parameter and does
    nothing with it, so an unverified ``forceable_params`` entry here would
    produce a green test suite, a clean ledger, and no enforcement whatsoever.
    This is why ``deepseek.json`` declares nothing forceable.
    """
    response = _raw({"store": False, "idside_not_a_real_param": {"nested": [1]}})
    assert response.status_code != 400, (
        f"DeepSeek refused an unknown field: {response.text[:300]}"
    )
    assert response.status_code != 422, (
        f"DeepSeek refused an unknown field: {response.text[:300]}"
    )
    if response.status_code == 402:
        pytest.skip(
            "confirmed as far as a dry key allows: the unknown fields passed "
            "deserialisation and the request died on billing, not on validation"
        )
    assert response.status_code == 200, response.text


def test_deepseek_sends_the_act_token_to_the_right_host(wire: WireLog) -> None:
    """What leaves the process, asserted without needing a balance.

    The request is built and sent regardless of billing, so the wire log states
    the contract: DeepSeek's host, the ACT token attached, the identity kwarg
    consumed, and no forced parameter invented.
    """
    client = consent.wrap(_client(wire), consent_client=StubConsentClient())
    try:
        client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": TINY_PROMPT}],
            max_tokens=1,
            user_id=USER_ID,
        )
    except Exception as exc:
        skip_if_unaffordable(exc)

    sent = wire.last
    assert sent.url.startswith(BASE_URL)
    assert sent.header("X-AI-Consent-Token")
    assert "user_id" not in sent.body
    assert "store" not in sent.body, "nothing is forceable on DeepSeek"


def test_deepseek_reports_itself_under_its_own_name_to_the_cloud(
    wire: WireLog,
) -> None:
    """``/v1/authorize`` is told ``deepseek``, so the ledger records the truth.

    The provider name on the proof is what an auditor reads. It has to be the
    company that received the data, not the client library that carried it.
    """
    cloud = StubConsentClient()
    client = consent.wrap(_client(wire), consent_client=cloud)
    try:
        client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": TINY_PROMPT}],
            max_tokens=1,
            user_id=USER_ID,
        )
    except Exception as exc:
        skip_if_unaffordable(exc)

    assert cloud.calls, "the cloud was never asked to authorize"
    assert cloud.calls[-1]["provider"] == "deepseek"
    assert cloud.calls[-1]["call_site"] == "chat.completions.create"
