"""Tests for the ID Side cloud transport (ConsentClient)."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from functools import partial
from typing import Any

import httpx
import pytest

from _cloud_problems import POLICY_HOLD
from idside.__about__ import __version__
from idside._transport import (
    DEFAULT_TIMEOUT_SECONDS,
    AsyncHttpConsentClient,
    ConsentDecision,
    HttpConsentClient,
)
from idside.exceptions import (
    AuthenticationError,
    BindingError,
    BindingRequiredError,
    ConflictingIdentityError,
    ConsentDeniedError,
    ConsentRefreshRequiredError,
    ConsentUnreachableError,
    ContractError,
    IDSideError,
    InvalidBindingError,
    MissingIdentityError,
    QuotaExceededError,
    RateLimitedError,
)


@pytest.fixture(autouse=True)
def _clear_idside_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize ambient IDSIDE_* env vars so config tests are deterministic."""
    monkeypatch.delenv("IDSIDE_API_KEY", raising=False)
    monkeypatch.delenv("IDSIDE_BASE_URL", raising=False)


_OK_BODY = {
    "decision": "allow",
    "consent_state": {"training": False},
    "params_forced": {"store": False},
    "mode": "test",
    "act_token": "eyJ.signed.token",
    "token_id": "act_01k4f9h2m3",
    "expires_at": "2026-09-12T12:00:00Z",
    "source": "widget",
    "legal_basis": "consent",
    "binding": "user_id",
}


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpConsentClient:
    return HttpConsentClient(
        base_url="https://api.test", transport=httpx.MockTransport(handler)
    )


def _authorize(client: HttpConsentClient) -> ConsentDecision:
    return client.authorize(
        user_id="usr_marie_456",
        provider="openai",
        model="gpt-4o",
        request_hash="abc123",
    )


def test_authorize_returns_decision_on_200() -> None:
    decision = _authorize(_client(lambda req: httpx.Response(200, json=_OK_BODY)))
    assert isinstance(decision, ConsentDecision)
    assert decision.consent_state == {"training": False}
    assert decision.params_forced == {"store": False}
    assert decision.act_token == "eyJ.signed.token"
    assert decision.source == "widget"
    assert decision.legal_basis == "consent"
    assert decision.binding == "user_id"


@pytest.mark.parametrize(
    "legal_basis",
    [
        "legitimate_interest",
        "contractual_obligation",
        "public_task",
        "legal_obligation",
        "vital_interests",
        "not_established",
        "a_basis_added_later",
    ],
)
def test_every_legal_basis_reaches_the_decision_as_the_cloud_wrote_it(
    legal_basis: str,
) -> None:
    """`legal_basis` is opaque: a value the cloud adds needs no SDK release.

    `public_task`, `legal_obligation` and `vital_interests` arrived that way. A
    closed list here would fail closed on every call an imposed policy signs on
    a basis added after this release — on a field the SDK only carries.
    """
    body = {**_OK_BODY, "source": "default_policy", "legal_basis": legal_basis}

    blocking, concurrent = _both(lambda req: httpx.Response(200, json=body))

    assert blocking[0] == "returned", blocking
    assert blocking[1].legal_basis == legal_basis
    assert blocking[1].source == "default_policy"
    assert concurrent == blocking


def test_authorize_posts_expected_payload_to_v1_authorize() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_OK_BODY)

    _authorize(_client(handler))
    assert str(captured["url"]).endswith("/v1/authorize")
    assert captured["body"] == {
        "user_id": "usr_marie_456",
        "provider": "openai",
        "model": "gpt-4o",
        "request_hash": "abc123",
        # Always present, empty included: it says this SDK understands credential
        # routing. An older one omits the field, and would ignore a posture it
        # was handed — the one case the cloud must not route for.
        "credential_postures": [],
    }


# ── Credential routing: what the SDK advertises, and what it accepts back ───


def test_authorize_advertises_the_provisioned_postures() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_OK_BODY)

    _client(handler).authorize(
        user_id="usr_marie_456",
        provider="openai",
        model="gpt-4o",
        request_hash="abc123",
        credential_postures=("default", "zero_retention"),
    )

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["credential_postures"] == ["default", "zero_retention"]


def test_authorize_reads_the_credential_posture() -> None:
    decision = _authorize(
        _client(
            lambda req: httpx.Response(
                200, json={**_OK_BODY, "credential_posture": "zero_retention"}
            )
        )
    )

    assert decision.credential_posture == "zero_retention"


def test_an_absent_credential_posture_means_the_client_as_built_is_fine() -> None:
    decision = _authorize(_client(lambda req: httpx.Response(200, json=_OK_BODY)))

    assert decision.credential_posture is None


def test_an_explicit_null_credential_posture_is_the_same_as_absent() -> None:
    body = {**_OK_BODY, "credential_posture": None}
    decision = _authorize(_client(lambda req: httpx.Response(200, json=body)))

    assert decision.credential_posture is None


@pytest.mark.parametrize(
    "posture",
    [
        42,
        "",
        "   ",
        ["zero_retention"],
        "x" * 201,
        "zero\nretention",
        "zero\u0085retention",
    ],
)
def test_a_posture_the_sdk_cannot_use_fails_closed(posture: object) -> None:
    """Ignoring it would send the call on the credential routing exists to avoid."""
    with pytest.raises(ConsentUnreachableError):
        _authorize(
            _client(
                lambda req: httpx.Response(
                    200, json={**_OK_BODY, "credential_posture": posture}
                )
            )
        )


def test_network_error_raises_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ConsentUnreachableError):
        _authorize(_client(handler))


def test_server_error_raises_unreachable() -> None:
    with pytest.raises(ConsentUnreachableError):
        _authorize(_client(lambda req: httpx.Response(503)))


def test_unmapped_4xx_raises_unreachable() -> None:
    # Cannot verify consent (undetermined 4xx) → fail-closed.
    with pytest.raises(ConsentUnreachableError):
        _authorize(_client(lambda req: httpx.Response(418)))


def test_forbidden_raises_consent_denied() -> None:
    with pytest.raises(ConsentDeniedError):
        _authorize(_client(lambda req: httpx.Response(403, json={"error": "opt_out"})))


# ── UBT: the identity can be a User Binding Token instead of a user_id ───────


def test_authorize_with_ubt_posts_ubt_and_no_user_id() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={**_OK_BODY, "binding": "ubt"})

    decision = _client(handler).authorize(
        ubt="eyJhbGciOiJFUzI1NiJ9.fake.ubt",
        provider="openai",
        model="gpt-4o",
        request_hash="abc123",
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["ubt"] == "eyJhbGciOiJFUzI1NiJ9.fake.ubt"
    assert "user_id" not in body  # the two identity forms are exclusive
    assert decision.binding == "ubt"


def test_authorize_rejects_user_id_and_ubt_together() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request must be made with an ambiguous identity")

    with pytest.raises(ConflictingIdentityError):
        _client(handler).authorize(
            user_id="usr_marie_456",
            ubt="eyJ.fake.ubt",
            provider="openai",
            model="gpt-4o",
            request_hash="abc123",
        )


@pytest.mark.parametrize("param", ["user_id", "ubt"])
def test_authorize_treats_a_blank_identity_as_absent(param: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request must be made for a blank identity")

    with pytest.raises(MissingIdentityError):
        _client(handler).authorize(
            provider="openai",
            model="gpt-4o",
            request_hash="abc123",
            **{param: " \t\u3000"},
        )


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param(4242101, id="int"),
        pytest.param(4242.5, id="float"),
        pytest.param(True, id="true"),
        pytest.param({"id": "usr_marie_456"}, id="dict"),
        pytest.param(["usr_marie_456"], id="list"),
    ],
)
def test_authorize_sends_an_identity_as_written(identity: Any) -> None:
    """The cloud judges an identity, not this SDK, so nothing converts it.

    A number names somebody; any other value that is not a string is the
    cloud's 422 to give. The Node SDK once turned every value into text on the
    way, and so authorized ``True`` as the user ``"true"`` and every object as
    one person; it is held to this test's answer now.
    """
    sent: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["user_id"])
        return httpx.Response(200, json=_OK_BODY)

    _client(handler).authorize(
        user_id=identity, provider="openai", model="gpt-4o", request_hash="abc123"
    )
    assert sent == [identity]
    assert type(sent[0]) is type(identity)


@pytest.mark.parametrize("param", ["user_id", "ubt"])
@pytest.mark.parametrize("empty", [[], {}], ids=["list", "dict"])
def test_authorize_treats_an_empty_container_as_absent(param: str, empty: Any) -> None:
    """Falsy, as ``""`` is; the Node SDK reads an empty array or object alike."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request must be made for an empty identity")

    with pytest.raises(MissingIdentityError):
        _client(handler).authorize(
            provider="openai", model="gpt-4o", request_hash="abc123", **{param: empty}
        )


def test_422_says_the_value_may_be_the_callers_own() -> None:
    """The cloud answers 422 for a value the caller passed, too.

    The sentence used to name one cause — a contract mismatch, upgrade or
    report a bug — and then quote a cloud detail saying the caller's own value
    was the problem. It now says both are possible, and which one the detail
    decides; the class stays ContractError.
    """
    with pytest.raises(ContractError) as caught:
        _authorize(
            _client(
                lambda req: _problem(
                    422,
                    "https://idside.eu/errors/invalid_request",
                    "Invalid request",
                    "Provide the user this call is for: `user_id` "
                    "(or `ubt`, if you use the widget).",
                )
            )
        )

    message = str(caught.value)
    assert message.startswith(
        "ID Side refused this call's payload (HTTP 422). If the cloud's detail "
        "names a value your code passed, fix that value; otherwise the SDK and "
        "the cloud disagree on the contract — upgrade the SDK or report it; the "
        "AI call was not made. Cloud says: Invalid request; Provide the user"
    ), message
    assert "contract mismatch — upgrade" not in message


def test_authorize_requires_some_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request must be made without an identity")

    with pytest.raises(MissingIdentityError):
        _client(handler).authorize(
            provider="openai", model="gpt-4o", request_hash="abc123"
        )


# ── S0.4: typed error mapping (a config problem must not look like an outage) ─


def _problem(status: int, type_: str, title: str, detail: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"type": type_, "title": title, "detail": detail},
        headers={"Content-Type": "application/problem+json"},
    )


def test_401_raises_authentication_error() -> None:
    with pytest.raises(AuthenticationError):
        _authorize(
            _client(
                lambda req: _problem(
                    401, "invalid_api_key", "Invalid API key", "Key sk_test_… unknown"
                )
            )
        )


def test_402_raises_quota_exceeded_error() -> None:
    with pytest.raises(QuotaExceededError):
        _authorize(
            _client(
                lambda req: _problem(
                    402, "quota_exceeded", "Quota exceeded", "Monthly quota reached"
                )
            )
        )


def test_403_provider_blocked_is_not_a_reason_this_sdk_recognises() -> None:
    """The cloud has never sent it, and the SDK used to name it anyway.

    A denial reason the SDK documents but nothing emits is worse than a missing
    one: a caller writes a branch for it, the branch is never taken, and the
    dead code reads as tested behaviour. The 403 is still a denial — only the
    explanation is withheld, which is what "we could not qualify this" means.
    """
    handler = lambda req: _problem(  # noqa: E731
        403, "provider_blocked", "Provider blocked", "App policy blocks openai"
    )
    with pytest.raises(ConsentDeniedError) as exc_info:
        _authorize(_client(handler))
    assert exc_info.value.reason is None


def test_403_fraud_suspended_exposes_reason() -> None:
    handler = lambda req: _problem(  # noqa: E731
        403, "fraud_suspended", "App suspended", "Fraud detection suspended the app"
    )
    with pytest.raises(ConsentDeniedError) as exc_info:
        _authorize(_client(handler))
    assert exc_info.value.reason == "fraud_suspended"


def test_403_with_unknown_type_has_no_reason() -> None:
    with pytest.raises(ConsentDeniedError) as exc_info:
        _authorize(
            _client(lambda req: _problem(403, "user_opt_out", "Denied", "Opt-out"))
        )
    assert exc_info.value.reason is None


def test_403_with_non_json_body_still_raises_denied() -> None:
    with pytest.raises(ConsentDeniedError) as exc_info:
        _authorize(_client(lambda req: httpx.Response(403, text="Forbidden")))
    assert exc_info.value.reason is None


def test_422_raises_contract_error() -> None:
    with pytest.raises(ContractError):
        _authorize(
            _client(
                lambda req: _problem(
                    422, "validation_error", "Invalid payload", "request_hash missing"
                )
            )
        )


def test_429_raises_rate_limited_error() -> None:
    handler = lambda req: _problem(  # noqa: E731
        429, "rate_limited", "Too many requests", "Slow down"
    )
    with pytest.raises(RateLimitedError):
        _authorize(_client(handler))


def test_problem_json_details_reach_the_error_message() -> None:
    with pytest.raises(AuthenticationError) as exc_info:
        _authorize(
            _client(
                lambda req: _problem(
                    401, "invalid_api_key", "Invalid API key", "Key sk_test_… unknown"
                )
            )
        )
    message = str(exc_info.value)
    assert "Invalid API key" in message
    assert "Key sk_test_… unknown" in message


def test_error_with_non_json_body_still_has_actionable_message() -> None:
    with pytest.raises(AuthenticationError) as exc_info:
        _authorize(_client(lambda req: httpx.Response(401, text="nope")))
    assert "401" in str(exc_info.value)


@pytest.mark.parametrize("ending", [".", "!", "?"])
def test_a_cloud_detail_that_ends_a_sentence_gets_no_second_full_stop(
    ending: str,
) -> None:
    """The cloud writes its `detail` as a sentence, full stop included.

    Appending one regardless printed `…(Signature has expired)..` at the end
    of the message a developer reads first when a call does not go out.
    """
    detail = f"50000/50000 authorize calls used this month{ending}"
    with pytest.raises(QuotaExceededError) as exc_info:
        _authorize(
            _client(lambda req: _problem(402, "quota_exceeded", "Quota", detail))
        )
    assert str(exc_info.value).endswith(f"Cloud says: Quota; {detail}")


def test_a_cloud_detail_without_a_full_stop_is_given_one() -> None:
    with pytest.raises(RateLimitedError) as exc_info:
        _authorize(
            _client(
                lambda req: _problem(
                    429, "rate_limited", "Too many requests", "Slow down"
                )
            )
        )
    assert str(exc_info.value).endswith("Cloud says: Too many requests; Slow down.")


# ── A 401 is not always the key: the visitor's binding, told apart ──────────
#
# The cloud answers 401 for three different reasons — the secret key is refused
# (`invalid_api_key`), the visitor's UBT no longer verifies (`ubt_invalid`), or a
# strict application was called with `user_id` (`ubt_required`) — and all three
# used to read "Check the api_key". The second is what every visitor coming back
# after 24 hours produces, and it sent developers to regenerate a key that was
# fine. The two binding cases call for the opposite of an operator alert, so
# they get their own classes; both stay `AuthenticationError`s, so a handler
# written against the old hierarchy still catches them.

_UBT_INVALID = "https://idside.eu/errors/ubt_invalid"
_UBT_REQUIRED = "https://idside.eu/errors/ubt_required"


def _unauthorized(problem: Any) -> AuthenticationError:
    with pytest.raises(AuthenticationError) as caught:
        _authorize(_client(lambda req: httpx.Response(401, json=problem)))
    return caught.value


def test_an_expired_binding_is_not_reported_as_a_rejected_key() -> None:
    error = _unauthorized(
        {
            "type": _UBT_INVALID,
            "title": "Ubt invalid",
            "status": 401,
            "detail": "UBT is invalid or expired (Signature has expired).",
        }
    )

    assert type(error) is InvalidBindingError
    assert isinstance(error, BindingError)
    message = str(error)
    assert "api_key" not in message
    assert "widget" in message
    assert "the AI call was not made." in message
    assert message.endswith(
        "Cloud says: Ubt invalid; UBT is invalid or expired (Signature has expired)."
    )


def test_an_invalid_binding_does_not_claim_two_causes_are_the_only_ones() -> None:
    """The cloud answers `ubt_invalid` for any UBT that does not verify.

    An edited payload, an ACT presented as a UBT and a truncated token all get
    it, and none of them is expired or foreign: a message naming those two as
    the causes is false for them, then quotes a cloud detail saying otherwise.
    """
    error = _unauthorized(
        {
            "type": _UBT_INVALID,
            "detail": "UBT is invalid or expired (Signature verification failed).",
        }
    )

    message = str(error)
    assert "did not verify" in message
    for cause in ("expired", "another application", "altered", "not a UBT"):
        assert cause in message, cause
    assert "has expired, or was issued for another application." not in message


def test_a_strict_app_called_without_a_ubt_says_what_it_needs() -> None:
    error = _unauthorized(
        {
            "type": _UBT_REQUIRED,
            "title": "Ubt required",
            "status": 401,
            "detail": "This app requires a User Binding Token.",
        }
    )

    assert type(error) is BindingRequiredError
    assert isinstance(error, BindingError)
    message = str(error)
    assert "api_key" not in message
    assert "widget" in message
    assert "the AI call was not made." in message


def test_the_bare_binding_names_are_read_too() -> None:
    """An older cloud, or a test double, may send the short name."""
    assert type(_unauthorized({"type": "ubt_invalid"})) is InvalidBindingError
    assert type(_unauthorized({"type": "ubt_required"})) is BindingRequiredError


@pytest.mark.parametrize(
    "problem",
    [
        pytest.param({}, id="no-type"),
        pytest.param({"type": "https://idside.eu/errors/invalid_api_key"}, id="key"),
        pytest.param({"type": "https://idside.eu/errors/ubt_expired"}, id="unknown"),
        pytest.param({"type": "https://evil.example/errors/ubt_invalid"}, id="foreign"),
        pytest.param({"type": "https://idside.eu/errors/v2/ubt_invalid"}, id="nested"),
        pytest.param({"type": "https://idside.eu/other/ubt_required"}, id="elsewhere"),
        pytest.param({"type": "UBT_INVALID"}, id="case"),
        pytest.param({"type": " ubt_invalid"}, id="padded"),
        pytest.param({"type": 7}, id="not-a-string"),
        pytest.param({"type": ["ubt_invalid"]}, id="a-list"),
        pytest.param(["ubt_invalid"], id="body-not-an-object"),
    ],
)
def test_any_other_401_stays_a_rejected_key(problem: Any) -> None:
    """Closed on purpose: a value this SDK does not know never moves the class."""
    error = _unauthorized(problem)

    assert type(error) is AuthenticationError
    assert "api_key" in str(error)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, ConsentDeniedError),
        (409, ConsentUnreachableError),
        (422, ContractError),
        (500, ConsentUnreachableError),
    ],
)
@pytest.mark.parametrize("type_", [_UBT_INVALID, _UBT_REQUIRED])
def test_a_binding_type_qualifies_a_401_and_nothing_else(
    status: int, expected: type[Exception], type_: str
) -> None:
    with pytest.raises(expected) as caught:
        _authorize(_client(lambda req: httpx.Response(status, json={"type": type_})))

    assert not isinstance(caught.value, BindingError)


# ── A 409 that asks for the widget is not an outage ─────────────────────────
#
# `consent_refresh_required`: the visitor's UBT still verifies, but the choices
# it is bound to no longer cover what this application now asks — the policy or
# the required questions changed since they answered. It used to fall into the
# default branch and come out as ConsentUnreachableError, whose remedy is
# "retry later". Retrying changes nothing; showing the widget does.

_REFRESH = "https://idside.eu/errors/consent_refresh_required"


def _conflict(problem: Any) -> IDSideError:
    with pytest.raises(IDSideError) as caught:
        _authorize(_client(lambda req: httpx.Response(409, json=problem)))
    return caught.value


def test_a_refresh_required_asks_for_the_widget_not_a_retry() -> None:
    error = _conflict(
        {
            "type": _REFRESH,
            "title": "Consent refresh required",
            "status": 409,
            "detail": "The consent bound to this UBT no longer covers this "
            "app's current AI use.",
        }
    )

    assert type(error) is ConsentRefreshRequiredError
    assert not isinstance(error, ConsentUnreachableError)
    message = str(error)
    assert "widget" in message
    assert "the AI call was not made." in message
    assert message.endswith("no longer covers this app's current AI use.")


def test_the_bare_refresh_name_is_read_too() -> None:
    error = _conflict({"type": "consent_refresh_required"})
    assert type(error) is ConsentRefreshRequiredError


@pytest.mark.parametrize(
    "problem",
    [
        pytest.param({}, id="no-type"),
        pytest.param(
            {"type": "https://idside.eu/errors/consent_not_collected"}, id="other"
        ),
        pytest.param(
            {"type": "https://idside.eu/errors/account_link_not_collected"},
            id="link",
        ),
        pytest.param(
            {"type": "https://evil.example/errors/consent_refresh_required"},
            id="foreign",
        ),
        pytest.param(
            {"type": "https://idside.eu/errors/v2/consent_refresh_required"},
            id="nested",
        ),
        pytest.param({"type": "CONSENT_REFRESH_REQUIRED"}, id="case"),
        pytest.param({"type": 409}, id="not-a-string"),
        pytest.param(["consent_refresh_required"], id="body-not-an-object"),
    ],
)
def test_any_other_409_keeps_failing_closed_as_before(problem: Any) -> None:
    error = _conflict(problem)

    assert type(error) is ConsentUnreachableError
    assert "Could not verify consent (HTTP 409)" in str(error)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthenticationError),
        (403, ConsentDeniedError),
        (422, ContractError),
        (500, ConsentUnreachableError),
    ],
)
def test_a_refresh_type_qualifies_a_409_and_nothing_else(
    status: int, expected: type[Exception]
) -> None:
    with pytest.raises(expected) as caught:
        _authorize(_client(lambda req: httpx.Response(status, json={"type": _REFRESH})))

    assert type(caught.value) is expected


# ── S0.5 (folded in): a malformed 200 is never guessed into a decision ───────


def test_200_with_malformed_json_raises_unreachable() -> None:
    with pytest.raises(ConsentUnreachableError):
        _authorize(_client(lambda req: httpx.Response(200, text="not-json{")))


def test_200_with_missing_fields_raises_unreachable() -> None:
    body = {"consent_state": {"training": False}}  # act_token & friends missing
    with pytest.raises(ConsentUnreachableError):
        _authorize(_client(lambda req: httpx.Response(200, json=body)))


def test_200_with_non_object_body_raises_unreachable() -> None:
    with pytest.raises(ConsentUnreachableError):
        _authorize(_client(lambda req: httpx.Response(200, json=["not", "a", "dict"])))


def test_200_without_binding_raises_unreachable() -> None:
    body = {k: v for k, v in _OK_BODY.items() if k != "binding"}
    with pytest.raises(ConsentUnreachableError):
        _authorize(_client(lambda req: httpx.Response(200, json=body)))


@pytest.mark.parametrize(
    "field",
    [
        "decision",
        "consent_state",
        "source",
        "legal_basis",
        "binding",
        "params_forced",
        "mode",
        "act_token",
        "token_id",
        "expires_at",
    ],
)
def test_every_openapi_required_decision_field_is_required(field: str) -> None:
    body = {key: value for key, value in _OK_BODY.items() if key != field}
    blocking, concurrent = _both(lambda req: httpx.Response(200, json=body))
    assert blocking[0] is ConsentUnreachableError
    assert concurrent[0] is ConsentUnreachableError


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("consent_state", [], id="consent-state-array"),
        pytest.param("params_forced", [], id="forced-params-array"),
        pytest.param("decision", "deny", id="decision-deny"),
        pytest.param("decision", True, id="decision-boolean"),
        pytest.param("decision", {}, id="decision-object"),
        pytest.param("mode", "bogus", id="unknown-mode"),
        pytest.param("mode", None, id="null-mode"),
        pytest.param("mode", [], id="array-mode"),
        pytest.param("mode", {}, id="object-mode"),
        pytest.param("act_token", "", id="empty-act-token"),
        pytest.param("act_token", "not-a-compact-jws", id="malformed-act-token"),
        pytest.param("act_token", "abc.$.def", id="non-base64url-act-token"),
        pytest.param("token_id", "token_1", id="malformed-token-id"),
        pytest.param("token_id", {}, id="object-token-id"),
        pytest.param("expires_at", "tomorrow", id="malformed-expiry"),
        pytest.param(
            "expires_at", "2026-02-30T12:00:00Z", id="invalid-calendar-expiry"
        ),
        pytest.param("expires_at", False, id="boolean-expiry"),
        pytest.param("source", 7, id="numeric-source"),
        pytest.param("source", "", id="empty-source"),
        pytest.param("source", "   ", id="blank-source"),
        pytest.param("legal_basis", False, id="boolean-legal-basis"),
        pytest.param("legal_basis", "", id="empty-legal-basis"),
        pytest.param("legal_basis", "   ", id="blank-legal-basis"),
        pytest.param("binding", {}, id="object-binding"),
        pytest.param("binding", "cookie", id="unknown-binding"),
        pytest.param("constraints", [], id="constraints-array"),
        pytest.param("constraints", {"tools": "allow"}, id="unknown-tools-action"),
        pytest.param(
            "constraints", {"tools": ["refuse"]}, id="non-string-tools-action"
        ),
        pytest.param(
            "constraints",
            {"tools": {"action": "refuse"}},
            id="object-tools-action",
        ),
        pytest.param("constraints", {"tools": False}, id="boolean-tools-action"),
        pytest.param("constraints", {"tools": None}, id="null-tools-action"),
        pytest.param("constraints", {"future": "refuse"}, id="unknown-constraint"),
    ],
)
def test_200_with_an_invalid_decision_field_fails_closed(
    field: str, value: object
) -> None:
    body = {**_OK_BODY, field: value}

    blocking, concurrent = _both(lambda req: httpx.Response(200, json=body))

    assert blocking[0] is ConsentUnreachableError
    assert concurrent[0] is ConsentUnreachableError


# ── S1.5: env-var config, User-Agent header, and request timeouts ────────────


def _capturing_client(captured: dict[str, Any], **kwargs: Any) -> HttpConsentClient:
    """Client whose transport records the outgoing request's url and headers."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["user_agent"] = request.headers.get("User-Agent")
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_OK_BODY)

    return HttpConsentClient(transport=httpx.MockTransport(handler), **kwargs)


def test_authorize_sends_versioned_user_agent() -> None:
    captured: dict[str, Any] = {}
    _authorize(_capturing_client(captured, base_url="https://api.test"))
    assert captured["user_agent"] == f"idside-python/{__version__}"


def test_explicit_api_key_sets_bearer_authorization() -> None:
    captured: dict[str, Any] = {}
    _authorize(
        _capturing_client(captured, base_url="https://api.test", api_key="sk_explicit")
    )
    assert captured["authorization"] == "Bearer sk_explicit"


def test_api_key_from_env_sets_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IDSIDE_API_KEY", "sk_from_env")
    captured: dict[str, Any] = {}
    _authorize(_capturing_client(captured, base_url="https://api.test"))
    assert captured["authorization"] == "Bearer sk_from_env"


def test_explicit_api_key_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IDSIDE_API_KEY", "sk_from_env")
    captured: dict[str, Any] = {}
    _authorize(
        _capturing_client(captured, base_url="https://api.test", api_key="sk_explicit")
    )
    assert captured["authorization"] == "Bearer sk_explicit"


def test_no_api_key_sends_no_authorization() -> None:
    captured: dict[str, Any] = {}
    _authorize(_capturing_client(captured, base_url="https://api.test"))
    assert captured["authorization"] is None


def test_empty_env_api_key_is_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IDSIDE_API_KEY", "")
    captured: dict[str, Any] = {}
    _authorize(_capturing_client(captured, base_url="https://api.test"))
    assert captured["authorization"] is None


def test_base_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IDSIDE_BASE_URL", "https://api.env.test")
    captured: dict[str, Any] = {}
    _authorize(_capturing_client(captured))
    assert str(captured["url"]).startswith("https://api.env.test")


def test_explicit_base_url_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IDSIDE_BASE_URL", "https://api.env.test")
    captured: dict[str, Any] = {}
    _authorize(_capturing_client(captured, base_url="https://api.explicit"))
    assert str(captured["url"]).startswith("https://api.explicit")


@pytest.mark.parametrize("configured", [None, ""])
def test_missing_base_url_uses_the_public_service_origin(
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
) -> None:
    if configured is not None:
        monkeypatch.setenv("IDSIDE_BASE_URL", configured)
    captured: dict[str, Any] = {}
    _authorize(_capturing_client(captured))
    assert captured["url"] == "https://sdk.idside.eu/v1/authorize"


def test_every_phase_of_the_authorize_hop_shares_one_deadline() -> None:
    """One number, and the same one on every phase.

    On a fail-closed path a timeout is not a tuning knob: it is the moment the
    product decides the cloud is dead and stops the caller's AI calls. A connect
    budget tighter than the read budget makes that moment depend on *how* the
    cloud is failing — and Node's `fetch` has no way to express the distinction,
    so keeping it here was keeping half a contract the other SDK could not sign.
    """
    timeout = HttpConsentClient(base_url="https://api.test")._client.timeout
    assert timeout.connect == DEFAULT_TIMEOUT_SECONDS
    assert timeout.read == DEFAULT_TIMEOUT_SECONDS
    assert timeout.write == DEFAULT_TIMEOUT_SECONDS
    assert timeout.pool == DEFAULT_TIMEOUT_SECONDS


def test_the_shared_deadline_is_the_one_both_sdks_publish() -> None:
    """`check_sdk_parity.sh` compares this against the Node client's, in ms."""
    assert DEFAULT_TIMEOUT_SECONDS == 5.0


def test_the_jwks_fetch_uses_the_same_deadline() -> None:
    """Verification is the same fail-closed hop, so it gets the same number.

    A caller checking a proof against a published key set is waiting on the same
    kind of outage, and used to wait a different length of time for it.
    """
    from idside.verify import _jwks_http_client

    timeout = _jwks_http_client().timeout
    assert timeout.connect == DEFAULT_TIMEOUT_SECONDS
    assert timeout.read == DEFAULT_TIMEOUT_SECONDS


def test_timeout_object_override_is_respected() -> None:
    client = HttpConsentClient(base_url="https://api.test", timeout=httpx.Timeout(1.0))
    assert client._client.timeout.read == 1.0


def test_float_timeout_override_is_respected() -> None:
    client = HttpConsentClient(base_url="https://api.test", timeout=3.0)
    assert client._client.timeout.read == 3.0


# ── The reason on a denial, which never once arrived ────────────────────────
#
# The cloud emits RFC 9457 `type` as a URI — `https://idside.eu/errors/<name>` —
# and both SDKs compared it against the bare name. So `exc.reason` was always
# None, including for `fraud_suspended`, while docs/errors.md documented a table
# of values and showed callers branching on them. Nobody noticed because the
# denial itself worked: only the explanation was missing.


def _denied(problem: dict[str, Any]) -> ConsentDeniedError:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=problem)

    client = HttpConsentClient(
        "https://api.example.test", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ConsentDeniedError) as caught:
        client.authorize(
            provider="openai", model="gpt-4o", request_hash="sha256:x", user_id="u"
        )
    return caught.value


def test_the_reason_survives_the_uri_form_the_cloud_actually_sends() -> None:
    error = _denied({"type": "https://idside.eu/errors/fraud_suspended"})

    assert error.reason == "fraud_suspended"


def test_an_organization_suspension_is_told_apart_from_a_fraud_review() -> None:
    # Sending a developer to a fraud review when their organization's account is
    # stopped sends them to the wrong person.
    error = _denied({"type": "https://idside.eu/errors/organization_suspended"})

    assert error.reason == "organization_suspended"


def test_a_policy_hold_is_told_apart_from_a_suspension() -> None:
    # Nothing is wrong with the account: its policy held this one call. A
    # developer sent to billing or to a fraud review is sent to the wrong
    # person; whoever sets the organization's policy is the one who can change it.
    error = _denied(POLICY_HOLD)

    assert type(error) is ConsentDeniedError
    assert error.reason == "policy_hold"
    assert str(error).startswith(
        "The requested AI usage was denied (HTTP 403); the AI call was not made."
    )
    assert "Policy hold" in str(error)
    # The cloud's sentence names what held the call; the docs promise it reaches
    # the developer in the message, whole.
    assert POLICY_HOLD["detail"] in str(error)


def test_the_bare_form_still_works() -> None:
    """An older cloud, or a test double, may send the short name."""
    assert _denied({"type": "fraud_suspended"}).reason == "fraud_suspended"


def test_a_reason_we_do_not_know_stays_unqualified() -> None:
    # A plain opt-out. `reason` is None, and the denial is honoured all the same.
    assert _denied({"type": "https://idside.eu/errors/something_new"}).reason is None
    assert _denied({}).reason is None


def test_only_our_own_error_namespace_may_name_a_reason() -> None:
    """A `type` from anywhere else is not ours to interpret."""
    foreign = _denied({"type": "https://evil.example/errors/fraud_suspended"})
    assert foreign.reason is None
    assert _denied({"type": "https://idside.eu/other/fraud_suspended"}).reason is None


def test_a_deeper_path_under_our_own_namespace_is_not_an_error_name() -> None:
    """`.../errors/<name>` names a reason; `.../errors/<anything>/<else>` does not.

    Stripping the prefix and accepting whatever is left would let a future
    namespace — `/errors/billing/fraud_suspended`, say — be read as the flat
    reason of the same name. The denial is honoured either way; what must not
    happen is `reason` asserting something the cloud did not say.
    """
    nested = _denied({"type": "https://idside.eu/errors/team/fraud_suspended"})

    assert nested.reason is None


def test_the_blocking_client_releases_its_connection_when_closed() -> None:
    """A pooled connection to the cloud is a socket, and it is the caller's to free.

    Long-lived processes build one of these per application; a short-lived one
    (a task, a test, a CLI) builds and drops them, and an unclosed pool is a
    resource warning at best and an exhausted file table at worst.
    """
    client = HttpConsentClient("https://api.example.test")

    client.close()

    assert client._client.is_closed


# ── The async half: the same conversation, or a different product ────────────
#
# `AsyncHttpConsentClient` is what an async provider client is given, and it is
# the only thing on the async path that talks to the cloud. Everything above
# this line was written for its blocking twin, and none of it ran against it.
#
# So these tests do not restate the assertions: they run the SAME scenario
# through BOTH clients and demand the same outcome — same exception type, same
# message, same `reason`, same decision. A behaviour that is only asserted for
# one of the two is a behaviour that can drift on the other, and the async half
# is the one that would drift silently, because it is the newer one.


def _async_client(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any
) -> AsyncHttpConsentClient:
    kwargs.setdefault("base_url", "https://api.test")
    return AsyncHttpConsentClient(transport=httpx.MockTransport(handler), **kwargs)


_AUTHORIZE_ARGS: dict[str, Any] = {
    "user_id": "usr_marie_456",
    "provider": "openai",
    "model": "gpt-4o",
    "request_hash": "abc123",
    "call_site": "chat.completions.create",
    "credential_postures": ["default", "zero_retention"],
}


def _outcome(call: Callable[[], Any]) -> Any:
    """Run one authorize and describe what came back, exception included.

    Comparable across the two clients on purpose: `==` on the result is the
    whole parity assertion, and an exception's message is part of it. A caller
    reads that message in a log and acts on it; two different ones for one
    situation is two different products.
    """
    try:
        value = call()
    except BaseException as exc:
        return (type(exc), str(exc), getattr(exc, "reason", None))
    return ("returned", value)


def _both(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any
) -> tuple[Any, Any]:
    """The same scenario put to the blocking client and to the async one."""
    blocking = _outcome(
        lambda: HttpConsentClient(
            transport=httpx.MockTransport(handler),
            **{"base_url": "https://api.test", **kwargs},
        ).authorize(**_AUTHORIZE_ARGS)
    )
    client = _async_client(handler, **kwargs)
    concurrent = _outcome(lambda: asyncio.run(client.authorize(**_AUTHORIZE_ARGS)))
    return blocking, concurrent


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(lambda req: httpx.Response(200, json=_OK_BODY), id="200"),
        pytest.param(_refused, id="unreachable"),
        pytest.param(lambda req: httpx.Response(503), id="503"),
        pytest.param(lambda req: httpx.Response(418), id="unmapped-4xx"),
        pytest.param(lambda req: httpx.Response(401), id="401"),
        pytest.param(
            lambda req: httpx.Response(401, json={"type": _UBT_INVALID}),
            id="401-ubt-invalid",
        ),
        pytest.param(
            lambda req: httpx.Response(401, json={"type": _UBT_REQUIRED}),
            id="401-ubt-required",
        ),
        pytest.param(lambda req: httpx.Response(402), id="402"),
        pytest.param(
            lambda req: httpx.Response(
                403, json={"type": "https://idside.eu/errors/fraud_suspended"}
            ),
            id="403-with-reason",
        ),
        pytest.param(
            lambda req: httpx.Response(403, json=POLICY_HOLD), id="403-policy-hold"
        ),
        pytest.param(
            lambda req: httpx.Response(409, json={"type": _REFRESH}),
            id="409-refresh-required",
        ),
        pytest.param(lambda req: httpx.Response(409), id="409"),
        pytest.param(lambda req: httpx.Response(422), id="422"),
        pytest.param(lambda req: httpx.Response(429), id="429"),
        pytest.param(
            lambda req: httpx.Response(200, content=b"not json"), id="200-malformed"
        ),
        pytest.param(
            lambda req: httpx.Response(200, json=["not", "an", "object"]),
            id="200-not-an-object",
        ),
        pytest.param(
            lambda req: httpx.Response(200, json={"consent_state": {}}),
            id="200-incomplete",
        ),
    ],
)
def test_the_async_client_answers_exactly_as_the_blocking_one(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    blocking, concurrent = _both(handler)
    assert blocking == concurrent


def test_an_unreachable_cloud_fails_closed_on_the_async_client() -> None:
    """Stated on its own, because it is invariant #3 and not a parity detail.

    No decision is guessed and no default is assumed: the caller is stopped
    with the error that means the AI call was not made.
    """
    client = _async_client(_refused)
    with pytest.raises(ConsentUnreachableError, match="the AI call was not made"):
        asyncio.run(client.authorize(**_AUTHORIZE_ARGS))


def test_the_async_client_posts_the_same_body_to_the_same_path() -> None:
    """What the cloud is told must not depend on the shape of client held."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(200, json=_OK_BODY)

    HttpConsentClient(
        base_url="https://api.test", transport=httpx.MockTransport(handler)
    ).authorize(**_AUTHORIZE_ARGS)
    asyncio.run(_async_client(handler).authorize(**_AUTHORIZE_ARGS))

    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert seen[0]["path"] == "/v1/authorize"


def test_an_identity_that_is_not_one_is_refused_before_any_async_request() -> None:
    """Refused where the blocking client refuses it, and with no I/O either way."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=_OK_BODY)

    client = _async_client(handler)
    args = {k: v for k, v in _AUTHORIZE_ARGS.items() if k != "user_id"}
    with pytest.raises(MissingIdentityError):
        asyncio.run(client.authorize(**args))
    with pytest.raises(ConflictingIdentityError):
        asyncio.run(client.authorize(**args, user_id="usr_marie_456", ubt="eyJ.ubt.x"))
    assert sent == []


def test_the_async_client_sends_the_same_headers_and_resolves_config_alike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["user_agent"] = request.headers.get("User-Agent")
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_OK_BODY)

    monkeypatch.setenv("IDSIDE_BASE_URL", "https://api.env.test")
    monkeypatch.setenv("IDSIDE_API_KEY", "sk_from_env")
    client = AsyncHttpConsentClient(transport=httpx.MockTransport(handler))
    asyncio.run(client.authorize(**_AUTHORIZE_ARGS))

    assert captured["user_agent"] == f"idside-python/{__version__}"
    assert captured["authorization"] == "Bearer sk_from_env"
    assert str(captured["url"]).startswith("https://api.env.test")


def test_the_async_client_carries_the_same_timeouts() -> None:
    """A slower budget on the async path would fail closed later, not less."""
    default = AsyncHttpConsentClient(base_url="https://api.test")
    assert default._client.timeout.connect == DEFAULT_TIMEOUT_SECONDS
    assert default._client.timeout.read == DEFAULT_TIMEOUT_SECONDS
    assert (
        AsyncHttpConsentClient(
            base_url="https://api.test", timeout=3.0
        )._client.timeout.read
        == 3.0
    )


def test_the_async_client_never_holds_a_blocking_transport() -> None:
    """The one thing that cannot be asserted by comparing answers.

    Both clients would return the same decision through a blocking hop; only
    the loop would notice, and only under load. So the transport itself is
    pinned: an httpx.AsyncClient, whose send is awaited.
    """
    client = AsyncHttpConsentClient(base_url="https://api.test")
    assert isinstance(client._client, httpx.AsyncClient)
    assert inspect.iscoroutinefunction(client.authorize)


def test_the_async_client_releases_its_connection_when_closed() -> None:
    """The async half of the same obligation, and it is awaited.

    `aclose()` on an `httpx.AsyncClient` is a coroutine: forgetting the `await`
    here would leave the pool open and produce a "coroutine was never awaited"
    warning somewhere unrelated to the client that owned it.
    """
    client = AsyncHttpConsentClient(base_url="https://api.test")

    asyncio.run(client.aclose())

    assert client._client.is_closed


# ── An unconfigured address says so, instead of looking like an outage ──────
#
# With neither `base_url=` nor IDSIDE_BASE_URL the SDK talks to its default,
# https://sdk.idside.eu — a contract, and kept. But a developer who forgot the
# variable met `Could not verify consent (HTTP 404)` and went looking for a
# network fault or a cloud incident, when one line of configuration was
# missing. Where the address was the default, the transport failure and the
# unexpected status now say so; where it was given, nothing changes. Every
# request below is answered by a mock transport: none reaches idside.eu.

_DEFAULT_ORIGIN_SENTENCE = (
    "No ID Side address was configured, so the SDK used its default, "
    "https://sdk.idside.eu: set IDSIDE_BASE_URL to the address shown in your ID "
    "Side dashboard, or pass that address explicitly."
)


def _not_found(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, text="<html>Not Found</html>")


def _failure(call: Callable[[], Any]) -> str:
    with pytest.raises(ConsentUnreachableError) as caught:
        call()
    assert type(caught.value) is ConsentUnreachableError
    return str(caught.value)


def _failures(base_url: str | None) -> list[str]:
    """What each client says when the address does not serve the API."""
    messages = []
    for handler in (_refused, _not_found):
        blocking = HttpConsentClient(base_url, transport=httpx.MockTransport(handler))
        concurrent = AsyncHttpConsentClient(
            base_url, transport=httpx.MockTransport(handler)
        )
        messages.append(_failure(partial(blocking.authorize, **_AUTHORIZE_ARGS)))
        messages.append(
            _failure(lambda: asyncio.run(concurrent.authorize(**_AUTHORIZE_ARGS)))  # noqa: B023
        )
    return messages


def test_a_failure_on_the_default_address_names_the_missing_setting() -> None:
    for message in _failures(None):
        assert "the AI call was not made." in message
        assert message.endswith(_DEFAULT_ORIGIN_SENTENCE), message


def test_a_failure_on_an_address_that_was_given_is_unchanged() -> None:
    for message in _failures("https://api.test"):
        assert _DEFAULT_ORIGIN_SENTENCE not in message, message


def test_the_default_address_given_explicitly_counts_as_given() -> None:
    """The sentence is about a missing setting, not about the host."""
    for message in _failures("https://sdk.idside.eu"):
        assert _DEFAULT_ORIGIN_SENTENCE not in message, message


def test_an_address_from_the_environment_counts_as_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IDSIDE_BASE_URL", "https://cloud.env.test")
    for message in _failures(None):
        assert _DEFAULT_ORIGIN_SENTENCE not in message, message


def test_an_empty_environment_variable_still_means_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IDSIDE_BASE_URL", "")
    for message in _failures(None):
        assert message.endswith(_DEFAULT_ORIGIN_SENTENCE), message


@pytest.mark.parametrize(
    ("status", "problem"),
    [
        (401, {}),
        (401, {"type": _UBT_INVALID}),
        (402, {}),
        (403, {}),
        (409, {"type": _REFRESH}),
        (422, {}),
        (429, {}),
    ],
)
def test_a_qualified_refusal_on_the_default_address_is_left_alone(
    status: int, problem: dict[str, Any]
) -> None:
    """Only the outage-shaped failures carry it: a typed refusal already says
    what to do, and the class never changes either way."""
    client = HttpConsentClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(status, json=problem))
    )
    with pytest.raises(IDSideError) as caught:
        client.authorize(**_AUTHORIZE_ARGS)

    assert not isinstance(caught.value, ConsentUnreachableError)
    assert _DEFAULT_ORIGIN_SENTENCE not in str(caught.value)


@pytest.mark.parametrize(
    ("argument", "configured", "expected_url", "named"),
    [
        pytest.param(
            None,
            "https://cloud.env.test",
            "https://cloud.env.test/v1/authorize",
            False,
            id="environment",
        ),
        pytest.param(
            None, None, "https://sdk.idside.eu/v1/authorize", True, id="neither"
        ),
        pytest.param(
            None, "", "https://sdk.idside.eu/v1/authorize", True, id="empty-environment"
        ),
        pytest.param(
            "https://cloud.arg.test",
            "https://cloud.env.test",
            "https://cloud.arg.test/v1/authorize",
            False,
            id="argument-wins",
        ),
        pytest.param(
            "",
            "https://cloud.env.test",
            "https://cloud.env.test/v1/authorize",
            False,
            id="empty-argument",
        ),
    ],
)
@pytest.mark.parametrize("shape", ["blocking", "async"])
def test_a_hand_built_client_resolves_its_address_as_the_node_one_does(
    monkeypatch: pytest.MonkeyPatch,
    argument: str | None,
    configured: str | None,
    expected_url: str,
    named: bool,
    shape: str,
) -> None:
    """The argument, then IDSIDE_BASE_URL, then the default, in the constructor.

    The same table runs in the Node suite: the two transports must agree on the
    host a hand-built client talks to, and on when a failure names the setting.
    """
    if configured is not None:
        monkeypatch.setenv("IDSIDE_BASE_URL", configured)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, text="<html>Not Found</html>")

    if shape == "blocking":
        client = HttpConsentClient(argument, transport=httpx.MockTransport(handler))
        message = _failure(partial(client.authorize, **_AUTHORIZE_ARGS))
    else:
        aclient = AsyncHttpConsentClient(
            argument, transport=httpx.MockTransport(handler)
        )
        message = _failure(lambda: asyncio.run(aclient.authorize(**_AUTHORIZE_ARGS)))

    assert seen == [expected_url]
    assert (_DEFAULT_ORIGIN_SENTENCE in message) is named


_ARG_KEY = "sk_test_arg_key"  # pragma: allowlist secret
_ENV_KEY = "sk_test_env_key"  # pragma: allowlist secret


@pytest.mark.parametrize(
    ("argument", "configured", "expected"),
    [
        pytest.param(_ARG_KEY, _ENV_KEY, f"Bearer {_ARG_KEY}", id="argument-wins"),
        pytest.param(None, _ENV_KEY, f"Bearer {_ENV_KEY}", id="environment"),
        pytest.param(None, None, None, id="neither"),
        pytest.param(None, "", None, id="empty-environment"),
        pytest.param("", _ENV_KEY, f"Bearer {_ENV_KEY}", id="empty-argument"),
    ],
)
@pytest.mark.parametrize("shape", ["blocking", "async"])
def test_a_hand_built_client_resolves_its_key_as_the_node_one_does(
    monkeypatch: pytest.MonkeyPatch,
    argument: str | None,
    configured: str | None,
    expected: str | None,
    shape: str,
) -> None:
    """The argument, then IDSIDE_API_KEY, in the constructor — the Node table."""
    if configured is not None:
        monkeypatch.setenv("IDSIDE_API_KEY", configured)
    sent: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("authorization"))
        return httpx.Response(503)

    if shape == "blocking":
        client = HttpConsentClient(
            "https://api.test", api_key=argument, transport=httpx.MockTransport(handler)
        )
        _failure(partial(client.authorize, **_AUTHORIZE_ARGS))
    else:
        aclient = AsyncHttpConsentClient(
            "https://api.test", api_key=argument, transport=httpx.MockTransport(handler)
        )
        _failure(lambda: asyncio.run(aclient.authorize(**_AUTHORIZE_ARGS)))

    assert sent == [expected]


_SECRET_KEY = "sk_idside_supersecret_key"  # pragma: allowlist secret

#: Every way an authorize can fail: the network, each qualified refusal, an
#: unqualified status and an unreadable 200.
_EVERY_FAILURE: list[Callable[[httpx.Request], httpx.Response]] = [
    _refused,
    *(
        (lambda req, s=status, b=body: httpx.Response(s, json=b))
        for status, body in [
            (401, {}),
            (401, {"type": _UBT_INVALID}),
            (401, {"type": _UBT_REQUIRED}),
            (402, {}),
            (403, {"type": "https://idside.eu/errors/fraud_suspended"}),
            (409, {"type": _REFRESH}),
            (409, {}),
            (422, {}),
            (429, {}),
            (500, {}),
            (404, {}),
        ]
    ),
    lambda req: httpx.Response(200, content=b"not json"),
]


def _chain_text(exc: BaseException) -> str:
    """The message and repr of an exception and of everything chained to it."""
    parts: list[str] = []
    link: BaseException | None = exc
    while link is not None:
        parts += [str(link), repr(link)]
        link = link.__cause__ or link.__context__
    return "\n".join(parts)


@pytest.mark.parametrize("source", ["argument", "environment"])
def test_the_key_appears_in_no_error_message(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """Whatever the cloud answers, no message — nor anything chained to it —
    quotes the key, wherever the key came from."""
    if source == "environment":
        monkeypatch.setenv("IDSIDE_API_KEY", _SECRET_KEY)
    key = _SECRET_KEY if source == "argument" else None
    for handler in _EVERY_FAILURE:
        blocking = HttpConsentClient(
            "https://api.test", api_key=key, transport=httpx.MockTransport(handler)
        )
        concurrent = AsyncHttpConsentClient(
            "https://api.test", api_key=key, transport=httpx.MockTransport(handler)
        )
        for call in (
            partial(blocking.authorize, **_AUTHORIZE_ARGS),
            lambda: asyncio.run(concurrent.authorize(**_AUTHORIZE_ARGS)),  # noqa: B023
        ):
            with pytest.raises(IDSideError) as caught:
                call()
            assert _SECRET_KEY not in _chain_text(caught.value)


# ── a key a header cannot carry is refused before it is sent, unquoted ──────
#
# A key read from a secret file usually ends with a line break, and one pasted
# by hand can carry a space or a non-ASCII character. Sent as it was, the
# request failed inside the HTTP layer: the SDK reported an unreachable cloud —
# an outage, for a configuration mistake — and chained an error whose message
# quoted the whole header, key included (`Illegal header value b'Bearer
# sk_…\n'`), into every log that records a cause. Whitespace around the key is
# dropped, as `fetch` drops it in the Node SDK; anything else that is not
# printable ASCII stops the transport where it is built, and the message names
# the setting without repeating the key. The Node suite holds the same table.

_KEY_HALVES = ("sk_live_unsendable", "keytail")  # pragma: allowlist secret
_UNSENDABLE_SEPARATORS = ["\n", "\r\n", " ", "\t", "\x00", "\x7f", "\xe9"]


@pytest.mark.parametrize("separator", _UNSENDABLE_SEPARATORS)
@pytest.mark.parametrize("source", ["argument", "environment"])
@pytest.mark.parametrize("client_class", [HttpConsentClient, AsyncHttpConsentClient])
def test_a_key_a_header_cannot_carry_is_refused_unquoted(
    monkeypatch: pytest.MonkeyPatch,
    separator: str,
    source: str,
    client_class: type[HttpConsentClient] | type[AsyncHttpConsentClient],
) -> None:
    key = separator.join(_KEY_HALVES)
    if source == "environment":
        if separator == "\x00":
            pytest.skip("an environment variable cannot hold a NUL")
        monkeypatch.setenv("IDSIDE_API_KEY", key)

    with pytest.raises(ValueError, match="IDSIDE_API_KEY") as caught:
        client_class("https://api.test", api_key=key if source == "argument" else None)

    text = _chain_text(caught.value)
    assert all(half not in text for half in _KEY_HALVES), text
    assert "api_key" in str(caught.value)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        pytest.param("sk_test_trimmed\n", "Bearer sk_test_trimmed", id="newline"),
        pytest.param(" \tsk_test_trimmed\r\n", "Bearer sk_test_trimmed", id="around"),
        pytest.param(" \n", None, id="only-whitespace"),
    ],
)
def test_whitespace_around_the_key_is_dropped(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected: str | None
) -> None:
    monkeypatch.setenv("IDSIDE_API_KEY", configured)
    captured: dict[str, Any] = {}
    _authorize(_capturing_client(captured, base_url="https://api.test"))
    assert captured["authorization"] == expected
