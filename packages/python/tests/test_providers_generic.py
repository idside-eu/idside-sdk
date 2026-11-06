"""Generic provider conformance suite.

Every bundled provider (openai, anthropic, gemini, mistral) must pass the SAME
assertions: cloud-decided params applied, protected params never overridden,
identity stripped, input never mutated, deterministic request hash, ACT token
attached where the provider's client can carry it, fail-closed on outage.

Fake clients mimic the shape of each real client library; the real call sites
are pinned separately in ``test_real_call_sites.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from idside import consent
from idside._transport import ConsentDecision
from idside.adapters import get_adapter_for
from idside.exceptions import (
    ConflictingIdentityError,
    ConsentUnreachableError,
    MissingIdentityError,
)

_TOKEN_HEADER = "X-AI-Consent-Token"


class _Recorder:
    """Callable standing in for one provider method; records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "fake_response"}


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


def _decision(params_forced: dict[str, Any]) -> ConsentDecision:
    return ConsentDecision(
        consent_state={"training": False},
        params_forced=params_forced,
        act_token="tok_1",
        source="widget",
        legal_basis="consent",
        binding="user_id",
    )


@dataclass
class Case:
    """One provider's shape: fake client, example call, extraction helpers."""

    provider: str
    #: builds (fake client, recorder for the primary call site)
    build: Callable[[], tuple[Any, _Recorder]]
    #: invokes the primary call site with kwargs
    call: Callable[[Any, dict[str, Any]], Any]
    #: a realistic minimal payload for this provider
    base_kwargs: dict[str, Any]
    #: a plausible non-protected params_forced the cloud could return
    forced: dict[str, Any]
    #: reads back the applied forced params from the recorded provider kwargs
    forced_view: Callable[[dict[str, Any]], dict[str, Any]]
    #: reads back the HTTP headers that will reach the provider
    headers_view: Callable[[dict[str, Any]], dict[str, Any]]
    #: a protected param the cloud must never override, and a poisoned value
    protected_key: str = "model"
    protected_value: Any = "evil-model"
    extra_ignored_kwargs: dict[str, Any] = field(default_factory=dict)


def _openai_case() -> Case:
    def build() -> tuple[Any, _Recorder]:
        recorder = _Recorder()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=recorder))
        )
        return client, recorder

    return Case(
        provider="openai",
        build=build,
        call=lambda c, kw: c.chat.completions.create(**kw),
        base_kwargs={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
        },
        forced={"store": False},
        forced_view=lambda kw: {"store": kw.get("store")},
        headers_view=lambda kw: kw["extra_headers"],
        extra_ignored_kwargs={"extra_headers": {"X-Trace": "1"}},
    )


def _anthropic_case() -> Case:
    def build() -> tuple[Any, _Recorder]:
        recorder = _Recorder()
        client = SimpleNamespace(messages=SimpleNamespace(create=recorder))
        return client, recorder

    return Case(
        provider="anthropic",
        build=build,
        call=lambda c, kw: c.messages.create(**kw),
        base_kwargs={
            "model": "claude-sonnet-5",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
        forced={"inference_geo": "us"},
        forced_view=lambda kw: {"inference_geo": kw.get("inference_geo")},
        headers_view=lambda kw: kw["extra_headers"],
        extra_ignored_kwargs={"extra_headers": {"X-Trace": "1"}},
    )


def _gemini_case() -> Case:
    def build() -> tuple[Any, _Recorder]:
        recorder = _Recorder()
        client = SimpleNamespace(models=SimpleNamespace(generate_content=recorder))
        return client, recorder

    return Case(
        provider="gemini",
        build=build,
        call=lambda c, kw: c.models.generate_content(**kw),
        base_kwargs={
            "model": "gemini-2.0-flash",
            "contents": "hi",
            "config": {"temperature": 0.5},
        },
        # google-genai takes no free-form kwargs: forced params land in config.
        forced={"seed": 7},
        forced_view=lambda kw: {"seed": kw["config"].get("seed")},
        headers_view=lambda kw: kw["config"]["http_options"]["headers"],
    )


def _mistral_case() -> Case:
    def build() -> tuple[Any, _Recorder]:
        recorder = _Recorder()
        client = SimpleNamespace(chat=SimpleNamespace(complete=recorder))
        return client, recorder

    return Case(
        provider="mistral",
        build=build,
        call=lambda c, kw: c.chat.complete(**kw),
        base_kwargs={
            "model": "mistral-large-latest",
            "messages": [{"role": "user", "content": "hi"}],
        },
        forced={"safe_prompt": True},
        forced_view=lambda kw: {"safe_prompt": kw.get("safe_prompt")},
        # mistralai carries per-request headers in the http_headers kwarg.
        headers_view=lambda kw: kw["http_headers"],
        extra_ignored_kwargs={"http_headers": {"X-Trace": "1"}},
    )


_CASES = [_openai_case(), _anthropic_case(), _gemini_case(), _mistral_case()]


@pytest.fixture(params=_CASES, ids=lambda case: case.provider)
def case(request: pytest.FixtureRequest) -> Case:
    return request.param  # type: ignore[no-any-return]


def _adapter_for(case: Case) -> Any:
    from idside.adapters import _OVERRIDES, ProviderAdapter
    from idside.providers import load_descriptors

    descriptor = load_descriptors()[case.provider]
    return _OVERRIDES.get(case.provider, ProviderAdapter)(descriptor)


def _wrap(case: Case, client: Any, consent_client: _FakeConsent) -> Any:
    return consent.wrap(
        client, adapter=_adapter_for(case), consent_client=consent_client
    )


def test_token_attached_where_the_client_can_carry_it(case: Case) -> None:
    client, recorder = case.build()
    _wrap(case, client, _FakeConsent(_decision(case.forced)))
    case.call(client, {**case.base_kwargs, "user_id": "usr_marie_456"})
    headers = case.headers_view(recorder.calls[-1])
    assert headers[_TOKEN_HEADER] == "tok_1"


def test_cloud_decided_params_are_applied(case: Case) -> None:
    client, recorder = case.build()
    _wrap(case, client, _FakeConsent(_decision(case.forced)))
    case.call(client, {**case.base_kwargs, "user_id": "usr_marie_456"})
    assert case.forced_view(recorder.calls[-1]) == case.forced


def test_protected_params_are_never_overridden(case: Case) -> None:
    poisoned = {**case.forced, case.protected_key: case.protected_value}
    client, recorder = case.build()
    _wrap(case, client, _FakeConsent(_decision(poisoned)))
    case.call(client, {**case.base_kwargs, "user_id": "usr_marie_456"})
    call = recorder.calls[-1]
    assert call[case.protected_key] == case.base_kwargs[case.protected_key]


def test_identity_is_stripped_before_the_provider(case: Case) -> None:
    client, recorder = case.build()
    _wrap(case, client, _FakeConsent(_decision(case.forced)))
    case.call(client, {**case.base_kwargs, "user_id": "usr_marie_456"})
    case.call(client, {**case.base_kwargs, "ubt": "eyJ.fake.ubt"})
    for call in recorder.calls:
        assert "user_id" not in call
        assert "ubt" not in call


def test_apply_does_not_mutate_the_input_kwargs(case: Case) -> None:
    adapter = _adapter_for(case)
    import copy

    original = copy.deepcopy(case.base_kwargs)
    adapter.apply(case.base_kwargs, case.forced, "tok_1")
    assert case.base_kwargs == original


def test_request_hash_is_deterministic_and_identity_blind(case: Case) -> None:
    adapter = _adapter_for(case)
    plain = adapter.extract_request_metadata(**case.base_kwargs)
    again = adapter.extract_request_metadata(**case.base_kwargs)
    with_identity = adapter.extract_request_metadata(
        **case.base_kwargs,
        user_id="usr_marie_456",
        **case.extra_ignored_kwargs,
    )
    with_ubt = adapter.extract_request_metadata(**case.base_kwargs, ubt="eyJ.fake")
    assert plain["request_hash"].startswith("sha256:")
    assert plain == again == with_identity == with_ubt


def test_fail_closed_when_cloud_unreachable(case: Case) -> None:
    client, recorder = case.build()
    _wrap(case, client, _FakeConsent(exc=ConsentUnreachableError("down")))
    with pytest.raises(ConsentUnreachableError):
        case.call(client, {**case.base_kwargs, "user_id": "usr_marie_456"})
    assert recorder.calls == []  # the AI call never went out


def test_missing_identity_is_rejected_before_any_io(case: Case) -> None:
    client, recorder = case.build()
    consent_client = _FakeConsent(_decision(case.forced))
    _wrap(case, client, consent_client)
    with pytest.raises(MissingIdentityError):
        case.call(client, dict(case.base_kwargs))
    assert recorder.calls == []
    assert consent_client.calls == []


def test_conflicting_identity_is_rejected_before_any_io(case: Case) -> None:
    client, recorder = case.build()
    consent_client = _FakeConsent(_decision(case.forced))
    _wrap(case, client, consent_client)
    with pytest.raises(ConflictingIdentityError):
        case.call(
            client,
            {**case.base_kwargs, "user_id": "usr_marie_456", "ubt": "eyJ.fake"},
        )
    assert recorder.calls == []
    assert consent_client.calls == []


def test_ubt_is_forwarded_opaque_to_the_cloud(case: Case) -> None:
    client, _ = case.build()
    consent_client = _FakeConsent(_decision(case.forced))
    _wrap(case, client, consent_client)
    case.call(client, {**case.base_kwargs, "ubt": "eyJhbGciOiJFUzI1NiJ9.fake.ubt"})
    query = consent_client.calls[-1]
    assert query["ubt"] == "eyJhbGciOiJFUzI1NiJ9.fake.ubt"
    assert query["user_id"] is None
    assert query["provider"] == case.provider


def test_auto_detection_resolves_the_right_adapter(case: Case) -> None:
    real = pytest.importorskip(
        {
            "openai": "openai",
            "anthropic": "anthropic",
            "gemini": "google.genai",
            "mistral": "mistralai.client",
        }[case.provider]
    )
    client = {
        "openai": lambda: real.OpenAI(api_key="sk-test"),
        "anthropic": lambda: real.Anthropic(api_key="sk-test"),
        "gemini": lambda: real.Client(api_key="test"),
        "mistral": lambda: real.Mistral(api_key="test"),
    }[case.provider]()
    assert get_adapter_for(client).name == case.provider
