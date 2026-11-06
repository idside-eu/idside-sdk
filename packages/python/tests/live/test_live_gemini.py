"""Live checks against the real Gemini Developer API.

The ``gemini`` descriptor declares ``forceable_params: []`` on the grounds that
"the REST API rejects unknown fields (400). Forcing anything here breaks every
call." The first half is confirmed below. The second half is too strong, and the
last two tests say why: ``store`` is **not** an unknown field — it is a
documented top-level parameter the API accepts — and the pinned ``google-genai``
client can reach it through ``http_options.extra_body``, which is the one route
that bypasses the strict ``GenerateContentConfig`` model.
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

#: Cheapest model still open to new keys. Google retires these briskly: older
#: `flash-lite` generations answer 404 "no longer available to new users".
MODEL = os.environ.get("IDSIDE_LIVE_GEMINI_MODEL", "gemini-3.1-flash-lite")

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _client(wire: WireLog) -> Any:
    genai = pytest.importorskip("google.genai")
    types = pytest.importorskip("google.genai.types")
    return genai.Client(
        api_key=require_key("GEMINI_API_KEY"),
        http_options=types.HttpOptions(
            client_args={"event_hooks": {"request": [wire.record]}}
        ),
    )


def _raw(body: dict[str, Any]) -> httpx.Response:
    return httpx.post(
        f"{_BASE}/{MODEL}:generateContent",
        headers={
            "x-goog-api-key": require_key("GEMINI_API_KEY"),
            "Content-Type": "application/json",
        },
        json={
            "contents": [{"parts": [{"text": TINY_PROMPT}]}],
            "generationConfig": {"maxOutputTokens": 1},
            **body,
        },
        timeout=TIMEOUT,
    )


def test_gemini_rejects_unknown_top_level_fields() -> None:
    """protojson refuses any field it cannot name — the request never runs.

    This fires *before* the API key is even validated, so a bad injection here
    is a hard failure for every user, not a degraded mode.
    """
    response = _raw({"idside_not_a_real_param": "x"})
    assert response.status_code == 400, response.text
    assert "Unknown name" in response.text


def test_gemini_rejects_unknown_fields_nested_in_the_config() -> None:
    """Strictness reaches into ``generationConfig`` too.

    Which matters because that is exactly where the Gemini adapter merges
    ``params_forced`` — see the next test.
    """
    response = httpx.post(
        f"{_BASE}/{MODEL}:generateContent",
        headers={
            "x-goog-api-key": require_key("GEMINI_API_KEY"),
            "Content-Type": "application/json",
        },
        json={
            "contents": [{"parts": [{"text": TINY_PROMPT}]}],
            "generationConfig": {"maxOutputTokens": 1, "idside_x": 1},
        },
        timeout=TIMEOUT,
    )
    assert response.status_code == 400, response.text
    assert "generation_config" in response.text


def test_gemini_accepts_our_act_token_header(wire: WireLog) -> None:
    """The ACT token rides in ``config.http_options.headers`` and is tolerated."""
    types = pytest.importorskip("google.genai.types")
    client = consent.wrap(_client(wire), consent_client=StubConsentClient())
    try:
        client.models.generate_content(
            model=MODEL,
            contents=TINY_PROMPT,
            config=types.GenerateContentConfig(max_output_tokens=1),
            user_id=USER_ID,
        )
    except Exception as exc:
        skip_if_unaffordable(exc)
        raise

    assert wire.last.header("X-AI-Consent-Token")


def test_gemini_adapter_cannot_carry_a_forced_param_into_the_config(
    wire: WireLog,
) -> None:
    """Forcing a non-config parameter dies client-side, in Pydantic.

    ``GenerateContentConfig`` forbids extra fields, so the adapter's
    ``config.update(params_forced)`` cannot smuggle a top-level parameter in.
    This is the mechanical reason ``forceable_params`` is empty today — not the
    absence of a lever at the API.
    """
    types = pytest.importorskip("google.genai.types")
    client = consent.wrap(
        _client(wire), consent_client=StubConsentClient({"store": False})
    )
    with pytest.raises(Exception, match=r"[Ee]xtra inputs are not permitted"):
        client.models.generate_content(
            model=MODEL,
            contents=TINY_PROMPT,
            config=types.GenerateContentConfig(max_output_tokens=1),
            user_id=USER_ID,
        )
    assert not wire.requests, "must fail closed: nothing may reach the provider"


def test_gemini_store_is_a_real_parameter_reachable_via_extra_body(
    wire: WireLog,
) -> None:
    """``store`` IS accepted at top level, through ``http_options.extra_body``.

    The route the adapter does not currently use: ``extra_body`` is merged into
    the request body by the google-genai transport *after* the config model has
    been validated, so it lands top-level where the API expects it. The call
    succeeds and ``store`` is on the wire — meaning Gemini's per-request logging
    control is enforceable, and ``forceable_params: []`` understates it.

    Scope, so nobody over-reads this: ``store`` governs the developer's own
    opt-in request logging. It does not touch the 55-day abuse-monitoring
    retention, and it does not opt out of free-tier training.
    """
    genai = pytest.importorskip("google.genai")
    types = pytest.importorskip("google.genai.types")
    client = genai.Client(
        api_key=require_key("GEMINI_API_KEY"),
        http_options=types.HttpOptions(
            client_args={"event_hooks": {"request": [wire.record]}}
        ),
    )
    try:
        client.models.generate_content(
            model=MODEL,
            contents=TINY_PROMPT,
            config=types.GenerateContentConfig(
                max_output_tokens=1,
                http_options=types.HttpOptions(extra_body={"store": False}),
            ),
        )
    except Exception as exc:
        skip_if_unaffordable(exc)
        raise

    assert wire.last.body["store"] is False, "store must land at the TOP level"
    assert "store" not in wire.last.body.get("generationConfig", {})


def test_gemini_rest_api_accepts_store_directly() -> None:
    """Same fact, independent of the client: the REST API takes ``store``.

    Kept separate so a future google-genai regression cannot be mistaken for the
    API having dropped the parameter.
    """
    response = _raw({"store": False})
    assert_response_ok(response)
