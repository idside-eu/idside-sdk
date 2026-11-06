"""Tests for the consent.wrap() orchestration."""

from __future__ import annotations

import asyncio
import functools
import json
from types import SimpleNamespace
from typing import Any

import pytest

from _cloud_problems import POLICY_HOLD
from idside import consent
from idside._transport import (
    AsyncHttpConsentClient,
    ConsentDecision,
    HttpConsentClient,
)
from idside.adapters import ProviderAdapter
from idside.exceptions import (
    AuthenticationError,
    BindingRequiredError,
    ConflictingIdentityError,
    ConsentDeniedError,
    ConsentRefreshRequiredError,
    ConsentUnreachableError,
    IDSideError,
    InvalidBindingError,
    MissingIdentityError,
    UnsupportedProviderError,
)
from idside.providers import load_descriptors

_OPENAI = load_descriptors()["openai"]
_ANTHROPIC = load_descriptors()["anthropic"]

_DECISION = ConsentDecision(
    consent_state={"training": False},
    params_forced={"store": False},
    act_token="tok_1",
    source="widget",
    legal_basis="consent",
    binding="user_id",
)


class _RecordingCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "chatcmpl_fake"}


def _fake_openai() -> tuple[Any, _RecordingCompletions]:
    completions = _RecordingCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


def _fake_openai_with_responses() -> tuple[
    Any, _RecordingCompletions, _RecordingCompletions
]:
    """Fake OpenAI client exposing BOTH declared call sites."""
    completions = _RecordingCompletions()
    responses = _RecordingCompletions()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions), responses=responses
    )
    return client, completions, responses


class _FakeConsent:
    def __init__(
        self, decision: ConsentDecision | None = None, exc: Exception | None = None
    ) -> None:
        self.decision = decision
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        assert self.decision is not None
        return self.decision


def _wrap(client: Any, consent_client: _FakeConsent) -> Any:
    return consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=consent_client
    )


def test_wrap_applies_cloud_params_and_attaches_token() -> None:
    client, completions = _fake_openai()
    _wrap(client, _FakeConsent(_DECISION)).chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        user_id="usr_marie_456",
    )
    call = completions.calls[-1]
    assert call["store"] is False  # params_forced applied
    assert call["extra_headers"]["X-AI-Consent-Token"] == "tok_1"
    assert "user_id" not in call  # stripped before reaching the provider


def test_wrap_queries_cloud_with_provider_model_and_hash() -> None:
    client, _ = _fake_openai()
    consent_client = _FakeConsent(_DECISION)
    _wrap(client, consent_client).chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], user_id="usr_x"
    )
    query = consent_client.calls[-1]
    assert query["provider"] == "openai"
    assert query["model"] == "gpt-4o"
    assert query["user_id"] == "usr_x"
    assert query["request_hash"].startswith("sha256:")
    assert len(query["request_hash"]) == len("sha256:") + 64


def test_a_200_deny_response_never_reaches_the_provider() -> None:
    import httpx

    client, completions = _fake_openai()
    body = {
        "decision": "deny",
        "consent_state": {},
        "params_forced": {},
        "mode": "live",
        "act_token": "act.jwt.token",
        "token_id": "act_01k4f9h2m3",
        "expires_at": "2026-09-12T12:00:00Z",
        "source": "widget",
        "legal_basis": "consent",
        "binding": "user_id",
    }
    cloud = HttpConsentClient(
        "https://api.example.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body)),
    )

    with pytest.raises(ConsentUnreachableError, match="decision"):
        consent.wrap(
            client, adapter=ProviderAdapter(_OPENAI), consent_client=cloud
        ).chat.completions.create(model="gpt-4o", messages=[], user_id="usr_marie_456")

    assert completions.calls == []


# Every refusal the cloud can qualify, and the ones it cannot, end the call
# before the provider: a class that tells the developer what to do is worth
# nothing if the call it describes went out anyway. An organization policy's hold is
# one of them, with the reason that says it was the policy and not the account.
_REFUSALS = [
    pytest.param(401, {}, AuthenticationError, None, id="401-key"),
    pytest.param(
        401,
        {"type": "https://idside.eu/errors/ubt_invalid"},
        InvalidBindingError,
        None,
        id="401-ubt-invalid",
    ),
    pytest.param(
        401,
        {"type": "https://idside.eu/errors/ubt_required"},
        BindingRequiredError,
        None,
        id="401-ubt-required",
    ),
    pytest.param(
        401,
        {"type": "https://idside.eu/errors/ubt_expired"},
        AuthenticationError,
        None,
        id="401-unknown-type",
    ),
    pytest.param(
        403, POLICY_HOLD, ConsentDeniedError, "policy_hold", id="403-policy-hold"
    ),
    pytest.param(
        409,
        {"type": "https://idside.eu/errors/consent_refresh_required"},
        ConsentRefreshRequiredError,
        None,
        id="409-refresh-required",
    ),
    pytest.param(
        409,
        {"type": "https://idside.eu/errors/consent_not_collected"},
        ConsentUnreachableError,
        None,
        id="409-other",
    ),
]


@pytest.mark.parametrize(("status", "problem", "expected", "reason"), _REFUSALS)
def test_a_qualified_refusal_never_reaches_the_provider(
    status: int,
    problem: dict[str, Any],
    expected: type[IDSideError],
    reason: str | None,
) -> None:
    import httpx

    client, completions = _fake_openai()
    cloud = HttpConsentClient(
        "https://api.example.test",
        transport=httpx.MockTransport(lambda _r: httpx.Response(status, json=problem)),
    )
    wrapped = consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=cloud
    )

    with pytest.raises(expected) as caught:
        wrapped.chat.completions.create(
            model="gpt-4o", messages=[], user_id="usr_marie_456"
        )

    assert type(caught.value) is expected
    assert getattr(caught.value, "reason", None) == reason
    assert completions.calls == []


@pytest.mark.parametrize(("status", "problem", "expected", "reason"), _REFUSALS)
def test_a_qualified_refusal_never_reaches_an_async_provider(
    status: int,
    problem: dict[str, Any],
    expected: type[IDSideError],
    reason: str | None,
) -> None:
    import httpx

    client, completions = _fake_async_openai()
    cloud = AsyncHttpConsentClient(
        "https://api.example.test",
        transport=httpx.MockTransport(lambda _r: httpx.Response(status, json=problem)),
    )
    wrapped = consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=cloud
    )

    with pytest.raises(expected) as caught:
        asyncio.run(
            wrapped.chat.completions.create(
                model="gpt-4o", messages=[], user_id="usr_marie_456"
            )
        )

    assert type(caught.value) is expected
    assert getattr(caught.value, "reason", None) == reason
    assert completions.calls == []


def test_wrap_returns_provider_response() -> None:
    client, _ = _fake_openai()
    result = _wrap(client, _FakeConsent(_DECISION)).chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], user_id="u"
    )
    assert result == {"id": "chatcmpl_fake"}


def test_wrap_is_fail_closed_when_cloud_unreachable() -> None:
    client, completions = _fake_openai()
    consent_client = _FakeConsent(exc=ConsentUnreachableError("cloud down"))
    with pytest.raises(ConsentUnreachableError):
        _wrap(client, consent_client).chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}], user_id="u"
        )
    assert completions.calls == []  # provider NOT called


def test_wrap_requires_user_id() -> None:
    client, completions = _fake_openai()
    consent_client = _FakeConsent(_DECISION)
    with pytest.raises(MissingIdentityError):
        _wrap(client, consent_client).chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
    assert completions.calls == []
    assert consent_client.calls == []  # cloud never queried without identity


def test_wrap_unknown_client_raises() -> None:
    with pytest.raises(UnsupportedProviderError):
        consent.wrap(object())


# ── UBT: the caller may pass a User Binding Token instead of a user_id ───────


def test_wrap_accepts_ubt_instead_of_user_id() -> None:
    client, completions = _fake_openai()
    consent_client = _FakeConsent(_DECISION)
    _wrap(client, consent_client).chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        ubt="eyJhbGciOiJFUzI1NiJ9.fake.ubt",
    )
    query = consent_client.calls[-1]
    assert query["ubt"] == "eyJhbGciOiJFUzI1NiJ9.fake.ubt"
    assert query["user_id"] is None
    call = completions.calls[-1]
    assert "ubt" not in call  # stripped before reaching the provider
    assert "user_id" not in call


def test_wrap_excludes_ubt_from_request_hash() -> None:
    # The UBT is identity plumbing, not payload: two calls that differ only by
    # their identity form must bind to the same request_hash.
    client, _ = _fake_openai()
    consent_client = _FakeConsent(_DECISION)
    wrapped = _wrap(client, consent_client)
    wrapped.chat.completions.create(model="gpt-4o", messages=[], user_id="usr_a")
    wrapped.chat.completions.create(model="gpt-4o", messages=[], ubt="eyJ.fake.ubt")
    hashes = {call["request_hash"] for call in consent_client.calls}
    assert len(hashes) == 1


def test_wrap_rejects_user_id_and_ubt_together() -> None:
    client, completions = _fake_openai()
    consent_client = _FakeConsent(_DECISION)
    with pytest.raises(ConflictingIdentityError):
        _wrap(client, consent_client).chat.completions.create(
            model="gpt-4o", messages=[], user_id="usr_marie_456", ubt="eyJ.fake.ubt"
        )
    assert completions.calls == []  # provider NOT called
    assert consent_client.calls == []  # cloud NOT queried either


# ── S0.1: every declared call site is enforced ──────────────────────────────


def test_wrap_enforces_all_declared_call_sites() -> None:
    # openai.json declares chat.completions.create AND responses.create: a call
    # through responses.create must never bypass consent enforcement.
    client, _, responses = _fake_openai_with_responses()
    consent_client = _FakeConsent(_DECISION)
    _wrap(client, consent_client).responses.create(
        model="gpt-4o", input="hi", user_id="usr_marie_456"
    )
    assert len(consent_client.calls) == 1  # consent WAS verified
    call = responses.calls[-1]
    assert call["store"] is False  # params_forced applied
    assert call["extra_headers"]["X-AI-Consent-Token"] == "tok_1"
    assert "user_id" not in call


def test_wrap_enforces_each_call_site_independently() -> None:
    client, completions, responses = _fake_openai_with_responses()
    consent_client = _FakeConsent(_DECISION)
    wrapped = _wrap(client, consent_client)
    wrapped.chat.completions.create(model="gpt-4o", messages=[], user_id="usr_a")
    wrapped.responses.create(model="gpt-4o", input="hi", user_id="usr_b")
    assert len(consent_client.calls) == 2
    assert completions.calls[-1]["extra_headers"]["X-AI-Consent-Token"] == "tok_1"
    assert responses.calls[-1]["extra_headers"]["X-AI-Consent-Token"] == "tok_1"


def test_each_enforced_call_names_the_endpoint_it_is_about_to_call() -> None:
    """P1.12: the cloud decides per endpoint, so it has to be told which one.

    Without this the cloud can only answer per provider, and a parameter that
    exists on one endpoint has to be dropped everywhere (losing the lever) or
    forced everywhere (breaking the call for exactly the users who opted out).
    """
    client, _, _ = _fake_openai_with_responses()
    consent_client = _FakeConsent(_DECISION)
    wrapped = _wrap(client, consent_client)

    wrapped.chat.completions.create(model="gpt-4o", messages=[], user_id="usr_a")
    wrapped.responses.create(model="gpt-4o", input="hi", user_id="usr_b")

    assert [call["call_site"] for call in consent_client.calls] == [
        "chat.completions.create",
        "responses.create",
    ]


def test_the_call_site_travels_to_the_cloud_in_the_authorize_payload() -> None:
    """It is worth nothing in the SDK's memory: it has to be on the wire."""
    import httpx

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "decision": "allow",
                "consent_state": {},
                "params_forced": {},
                "mode": "test",
                "act_token": "act.jwt.token",
                "token_id": "act_01k4f9h2m3",
                "expires_at": "2026-09-12T12:00:00Z",
                "source": "widget",
                "legal_basis": "consent",
                "binding": "user_id",
            },
        )

    client = HttpConsentClient(
        "https://api.example.test", transport=httpx.MockTransport(handler)
    )
    client.authorize(
        provider="openai",
        model="gpt-4o",
        request_hash="sha256:" + "0" * 64,
        user_id="usr_marie_456",
        call_site="responses.create",
    )

    assert seen["call_site"] == "responses.create"


def test_an_unknown_call_site_is_omitted_rather_than_sent_empty() -> None:
    """A caller with no endpoint to name must not assert one.

    An empty `call_site` on the wire would look like a real answer; its absence
    tells the cloud to fall back to what is safe on every endpoint.
    """
    import httpx

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "decision": "allow",
                "consent_state": {},
                "params_forced": {},
                "mode": "test",
                "act_token": "act.jwt.token",
                "token_id": "act_01k4f9h2m3",
                "expires_at": "2026-09-12T12:00:00Z",
                "source": "widget",
                "legal_basis": "consent",
                "binding": "user_id",
            },
        )

    HttpConsentClient(
        "https://api.example.test", transport=httpx.MockTransport(handler)
    ).authorize(
        provider="openai",
        model="gpt-4o",
        request_hash="sha256:" + "0" * 64,
        user_id="usr_marie_456",
    )

    assert "call_site" not in seen


def test_wrap_ignores_declared_but_absent_call_sites() -> None:
    # The client only exposes chat.completions.create; the declared-but-absent
    # responses.create must be skipped without error.
    client, completions = _fake_openai()
    _wrap(client, _FakeConsent(_DECISION)).chat.completions.create(
        model="gpt-4o", messages=[], user_id="usr_marie_456"
    )
    assert completions.calls[-1]["store"] is False


def test_wrap_enforces_single_call_site_provider() -> None:
    messages = _RecordingCompletions()
    client = SimpleNamespace(messages=messages)
    consent_client = _FakeConsent(_DECISION)
    consent.wrap(
        client, adapter=ProviderAdapter(_ANTHROPIC), consent_client=consent_client
    ).messages.create(model="claude-sonnet-5", max_tokens=64, user_id="usr_marie_456")
    assert len(consent_client.calls) == 1
    headers = messages.calls[-1]["extra_headers"]
    assert headers["X-AI-Consent-Token"] == "tok_1"


def test_wrap_is_idempotent() -> None:
    # Wrapping twice must not stack enforcement (nor break the call).
    client, completions, responses = _fake_openai_with_responses()
    consent_client = _FakeConsent(_DECISION)
    _wrap(_wrap(client, consent_client), consent_client)
    client.chat.completions.create(model="gpt-4o", messages=[], user_id="usr_a")
    client.responses.create(model="gpt-4o", input="hi", user_id="usr_b")
    assert len(consent_client.calls) == 2  # one authorize per AI call, not two
    assert len(completions.calls) == 1
    assert len(responses.calls) == 1


# ── a wrap that did not finish, and the retry that does ─────────────────────


class _NotReadyOnce:
    """A resource that refuses the patch once, then accepts it.

    A wrap can fail after it has already patched something — a resource behind a
    strict proxy, an attribute that is not materialised yet, a client half
    through its own construction. What the retry must not do is enforce the
    already-patched call site a second time: two wrappers on one method means
    two authorizations, two ACT tokens and two ledger entries for one AI call.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._refused = False

    def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "resp_fake"}

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "create" and not self.__dict__.get("_refused", False):
            object.__setattr__(self, "_refused", True)
            raise AttributeError("this resource is not ready to be patched")
        object.__setattr__(self, name, value)


def test_a_wrap_interrupted_partway_is_completed_by_a_retry() -> None:
    """The second attempt finishes the first; it does not redo it.

    The first `wrap()` patches `chat.completions.create` and then dies on
    `responses.create`, leaving the client enforced in one place and not the
    other — and, crucially, marked as incomplete, so idempotence does not
    short-circuit the retry and hand back a half-wrapped client.
    """
    completions = _RecordingCompletions()
    responses = _NotReadyOnce()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions), responses=responses
    )
    consent_client = _FakeConsent(_DECISION)

    with pytest.raises(AttributeError):
        _wrap(client, consent_client)

    _wrap(client, consent_client)

    client.chat.completions.create(model="gpt-4o", messages=[], user_id="usr_a")
    client.responses.create(model="gpt-4o", input="hi", user_id="usr_b")

    assert len(consent_client.calls) == 2, "one authorize per AI call, not two"
    assert len(completions.calls) == 1
    assert len(responses.calls) == 1
    assert _OPENAI.identity_param not in completions.calls[0]


class _SwallowsThePatch:
    """A resource that accepts the patch and quietly drops it.

    The call-site twin of `_SwallowingTransport` in tests/test_interception_net:
    `setattr` raises nothing, so the write looks like it worked. Nothing can
    make such a resource enforceable; what must not happen is this SDK recording
    it as enforced and never looking at it again.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.patch_attempts = 0

    def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "resp_fake"}

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "create":
            object.__setattr__(self, "patch_attempts", self.patch_attempts + 1)
            return
        object.__setattr__(self, name, value)


def test_a_call_site_whose_patch_was_dropped_is_tried_again_not_assumed() -> None:
    """Idempotence rests on a completion bit, and this is what that bit is for.

    A method carrying no state from this SDK was never wrapped, whatever the
    call site next to it says. Reading `state.complete` off it would be reading
    an attribute of `None`; treating its absence as "done" would be worse — the
    client would be handed back and never re-examined.
    """
    completions = _RecordingCompletions()
    responses = _SwallowsThePatch()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions), responses=responses
    )
    consent_client = _FakeConsent(_DECISION)

    _wrap(client, consent_client)
    _wrap(client, consent_client)

    assert responses.patch_attempts == 2, "the second wrap() short-circuited"
    client.chat.completions.create(model="gpt-4o", messages=[], user_id="usr_a")
    assert len(consent_client.calls) == 1, "the wrapped site was enforced twice"


# ── what a call site may be hiding behind ───────────────────────────────────


def _validating(fn: Any) -> Any:
    """The decorator shape both provider libraries put on their entrypoints.

    A plain `def` returning the coroutine, with `functools.wraps` leaving the
    real function on `__wrapped__` — which is the only place the async-ness is
    still visible.
    """

    @functools.wraps(fn)
    def _checked(**kwargs: Any) -> Any:
        return fn(**kwargs)

    return _checked


def test_a_call_site_behind_a_partial_is_still_recognised_as_async() -> None:
    """`functools.partial` is peeled before the decorator chain is followed.

    Neither half does this alone. `inspect.iscoroutinefunction` sees through a
    partial but not through `functools.wraps`; `inspect.unwrap` sees through
    `functools.wraps` but finds no `__wrapped__` on a partial and stops. A
    decorated entrypoint with an argument bound onto it — which is what an
    application's own plumbing produces — is invisible to both, and the client
    would be handed the blocking wrapper: the SDK would pass a coroutine along
    as if it were the provider's answer, and never await it.
    """
    inner = _AsyncCompletions()
    completions = SimpleNamespace(create=functools.partial(_validating(inner.create)))
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    consent_client = _FakeAsyncConsent(_DECISION)

    assert consent._is_async_call_site(completions.create) is True

    wrapped = _wrap(client, consent_client)
    result = asyncio.run(
        wrapped.chat.completions.create(model="gpt-5", messages=[], user_id="usr_x")
    )

    assert result == {"id": "chatcmpl_fake"}, "a coroutine was handed back unawaited"
    assert len(consent_client.calls) == 1
    assert len(inner.calls) == 1


def test_a_decorator_loop_answers_no_rather_than_raising() -> None:
    """A `__wrapped__` cycle is a decorator bug, not a reason to refuse a client.

    `inspect.unwrap` raises on one rather than spinning, and that exception
    would come out of `consent.wrap()` — from a line asking a question about the
    method, with nothing in the message about decorators. The honest answer is
    that a method whose chain cannot be followed is not known to be async.
    """
    calls: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> dict[str, str]:
        calls.append(kwargs)
        return {"id": "chatcmpl_fake"}

    create.__wrapped__ = create  # type: ignore[attr-defined]
    assert consent._is_async_call_site(create) is False

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    consent_client = _FakeConsent(_DECISION)

    _wrap(client, consent_client).chat.completions.create(
        model="gpt-4o", messages=[], user_id="usr_marie_456"
    )

    assert len(consent_client.calls) == 1
    assert len(calls) == 1


# ── the resuming form, on an async client ───────────────────────────────────


class _AsyncResponses:
    """`responses.create` as an async client writes it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "resp_fake"}


def test_the_resuming_form_of_an_async_call_site_mints_no_proof() -> None:
    """The async twin of the overloaded helper: retrieving creates nothing.

    `responses.create(response_id=…)` replays a response that already exists.
    There is no prompt and no model, so authorizing it would sign a token
    asserting an AI call that never happened. The identity kwargs are still the
    SDK's own and are still consumed — an application that passes `user_id=` on
    every call must not hand the provider a keyword it has never heard of.
    """
    responses = _AsyncResponses()
    client = SimpleNamespace(responses=responses)
    consent_client = _FakeAsyncConsent(_DECISION)

    wrapped = _wrap(client, consent_client)
    result = asyncio.run(
        wrapped.responses.create(response_id="resp_123", user_id="usr_marie_456")
    )

    assert result == {"id": "resp_fake"}, "a coroutine was handed back unawaited"
    assert consent_client.calls == [], "a call that creates nothing was authorized"
    assert responses.calls == [{"response_id": "resp_123"}]


# ── S1.8: async clients are enforced, not rejected ──────────────────────────


class _AsyncCompletions:
    """Call site whose `create` is a coroutine function (like AsyncOpenAI)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "chatcmpl_fake"}


def _fake_async_openai() -> tuple[Any, _AsyncCompletions]:
    completions = _AsyncCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


def test_wrap_enforces_an_async_client() -> None:
    """What used to raise UnsupportedProviderError now enforces.

    The full async path lives in tests/test_async_support.py, including the one
    thing that had to be built rather than allowed: the net on an async
    transport. This is the orchestration end of it — wrap() accepts the client,
    patches its call site, and the call goes through the cloud.
    """
    client, completions = _fake_async_openai()
    consent_client = _FakeAsyncConsent(_DECISION)

    wrapped = _wrap(client, consent_client)
    asyncio.run(
        wrapped.chat.completions.create(
            model="gpt-5", messages=[{"role": "user", "content": "hi"}], user_id="usr_x"
        )
    )

    assert len(consent_client.calls) == 1
    assert len(completions.calls) == 1
    assert completions.calls[0]["store"] is False
    assert completions.calls[0][_OPENAI.headers_kwarg][_OPENAI.token_header] == "tok_1"
    assert _OPENAI.identity_param not in completions.calls[0]


def test_an_async_call_site_is_patched_where_a_sync_one_would_be() -> None:
    """The patch lands on the instance, whichever shape the method has."""
    client, completions = _fake_async_openai()
    _wrap(client, _FakeAsyncConsent(_DECISION))
    assert "create" in completions.__dict__


class _FakeAsyncConsent:
    """The async half of _FakeConsent: what wrap() builds for an async client."""

    def __init__(self, decision: ConsentDecision) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    async def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        return self.decision


# ── An identity made of blanks names nobody ─────────────────────────────────
#
# `user_id="   "` used to leave: only the empty string counted as absent. The
# cloud refused it with a 422, and the SDK's sentence sent the developer to
# upgrade the package. Blank means what the cloud means by it — the 25 code
# points ActiveSupport's `blank?` matches, nothing more — so the SDK stops
# exactly the values the cloud would refuse. Nothing else is trimmed: a
# `user_id` with spaces around it is sent as written, because its pseudonym is
# derived from those exact characters. The Node suite pins the same list.

#: The cloud's blank set, as code points.
_BLANK_CODES = [
    *range(0x09, 0x0E),
    0x20,
    0x85,
    0xA0,
    0x1680,
    *range(0x2000, 0x200B),
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
]
_BLANKS = [chr(c) for c in _BLANK_CODES]


def test_the_blank_set_is_the_clouds_and_no_wider() -> None:
    from idside._transport import _is_blank

    blank = [c for c in range(0x10000) if _is_blank(chr(c))]
    assert blank == _BLANK_CODES
    assert len(blank) == 25
    assert not _is_blank("")  # empty is already absent, and is not a blank


class _Untouched:
    """A cloud that must never be asked."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        raise AssertionError("a blank identity reached the cloud")


@pytest.mark.parametrize("param", ["user_id", "ubt"])
@pytest.mark.parametrize(
    "blank",
    [
        "   ",
        "\t\n",
        chr(0xA0),
        chr(0x3000) + chr(0x2028),
        "".join(_BLANKS),
    ],
)
def test_an_identity_made_of_blanks_is_absent(param: str, blank: str) -> None:
    client, completions = _fake_openai()
    cloud = _Untouched()

    with pytest.raises(MissingIdentityError):
        _wrap(client, cloud).chat.completions.create(  # type: ignore[arg-type]
            model="gpt-4o", messages=[], **{param: blank}
        )

    assert cloud.calls == []
    assert completions.calls == []


def test_a_blank_user_id_beside_a_ubt_is_no_conflict() -> None:
    """Blank is absent, as the empty string already was: the UBT names the user."""
    client, _ = _fake_openai()
    consent_client = _FakeConsent(_DECISION)

    _wrap(client, consent_client).chat.completions.create(
        model="gpt-4o", messages=[], user_id="   ", ubt="eyJ.u.b"
    )

    assert consent_client.calls[-1]["ubt"] == "eyJ.u.b"
    assert consent_client.calls[-1]["user_id"] is None


@pytest.mark.parametrize(
    "value",
    [
        " usr_marie_456 ",
        "\t usr_marie_456",
        chr(0xFEFF),
        chr(0x200B),
        chr(0x1C),
    ],
)
def test_nothing_else_is_trimmed(value: str) -> None:
    """A value the cloud accepts as present leaves exactly as written."""
    client, _ = _fake_openai()
    consent_client = _FakeConsent(_DECISION)

    _wrap(client, consent_client).chat.completions.create(
        model="gpt-4o", messages=[], user_id=value
    )

    assert consent_client.calls[-1]["user_id"] == value
