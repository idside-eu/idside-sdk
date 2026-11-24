"""Prompt cleaning on enforced calls: the signed policy, the hash, the declaration.

A stand-in cloud (``_signed_policy.Cloud``) signs policies with a key made for
the test run, answers the routes the SDK uses and records what it receives.
The provider is a recording fake: what it receives is what would have left.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from _signed_policy import (
    CHALLENGE,
    NOW,
    Cloud,
    SigningKey,
    b64url,
    claims_for,
    jws,
    policy_document,
)
from idside import _content_policy, _p256, consent, request_hash
from idside._cleaning import (
    ENGINE_VERSION,
    build_digest,
    challenge_response,
    content_policy_digest,
    rules_digest,
)
from idside._transport import (
    AsyncHttpConsentClient,
    HttpConsentClient,
    _content_policy_answer,
)
from idside.adapters import ProviderAdapter
from idside.exceptions import (
    ActVerificationError,
    AuthenticationError,
    ConsentDeniedError,
    ConsentUnreachableError,
    ContentBlockedError,
    ContentPolicyError,
    ContractError,
    IDSideError,
    QuotaExceededError,
    RateLimitedError,
    UnenforceableOptionError,
)
from idside.providers import load_descriptors

_OPENAI = load_descriptors()["openai"]


#: The real seams, kept before the fixture below replaces them.
_REAL_MONOTONIC = _content_policy.monotonic
_REAL_SLEEP = _content_policy.sleep
_REAL_ASYNC_SLEEP = _content_policy.async_sleep


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    # Both clocks read the same value unless a test sets `_monotonic` apart:
    # moving this one moves both, as time passing does.
    now = [float(NOW)]
    monkeypatch.setattr(_content_policy, "clock", lambda: now[0])
    monkeypatch.setattr(_content_policy, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def _monotonic(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The monotonic clock on its own, for a test that moves the wall clock alone."""
    reading = [5_000.0]
    monkeypatch.setattr(_content_policy, "monotonic", lambda: reading[0])
    return reading


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "chatcmpl_fake"}


class _AsyncRecorder(_Recorder):
    async def create(self, **kwargs: Any) -> dict[str, str]:  # type: ignore[override]
        self.calls.append(kwargs)
        return {"id": "chatcmpl_fake"}


def _wrapped(cloud: Cloud) -> tuple[Any, _Recorder]:
    recorder = _Recorder()
    client = SimpleNamespace(chat=SimpleNamespace(completions=recorder))
    consent_client = HttpConsentClient(
        "https://cloud.example.test",
        api_key="sk_test_cleaning",  # pragma: allowlist secret
        # Read late, as the async stand-in does: a test may swap `cloud.handle`.
        transport=httpx.MockTransport(lambda request: cloud.handle(request)),
    )
    return consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=consent_client
    ), recorder


def _wrapped_async(cloud: Cloud) -> tuple[Any, _AsyncRecorder]:
    recorder = _AsyncRecorder()
    client = SimpleNamespace(chat=SimpleNamespace(completions=recorder))
    consent_client = AsyncHttpConsentClient(
        "https://cloud.example.test",
        api_key="sk_test_cleaning",  # pragma: allowlist secret
        transport=httpx.MockTransport(cloud.handle_async),
    )
    return consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=consent_client
    ), recorder


MESSAGES = [
    {"role": "system", "content": "About Projet Atlas."},
    {"role": "user", "content": "Write to marie@example.com"},
]
CLEANED = [
    {"role": "system", "content": "About [PROJECT_1]."},
    {"role": "user", "content": "Write to [EMAIL_1]"},
]


# ── cleaning off: exactly the call an SDK without cleaning makes ─────────────


def test_cleaning_off_sends_no_field_and_moves_no_hash() -> None:
    cloud = Cloud(policy=None)
    client, provider = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    (authorize,) = cloud.bodies("/v1/authorize")
    assert "content_policy" not in authorize
    assert authorize["request_hash"] == request_hash(
        {"model": "gpt-4o", "messages": MESSAGES}
    )
    assert provider.calls[0]["messages"] is MESSAGES  # the caller's own object
    assert cloud.paths() == ["/v1/content-policy", "/v1/authorize"]


def test_a_consent_client_that_cannot_fetch_a_policy_cleans_nothing() -> None:
    class Bare:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def authorize(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            from idside._transport import ConsentDecision

            return ConsentDecision(
                {}, {}, "act.jwt.token", "widget", "consent", "user_id"
            )

    recorder = _Recorder()
    bare = Bare()
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=recorder)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=bare,
    )
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert "content_policy" not in bare.calls[0]
    assert recorder.calls[0]["messages"] is MESSAGES


# ── cleaning on ─────────────────────────────────────────────────────────────


def test_cleaning_masks_before_the_hash_and_declares_what_it_applied() -> None:
    cloud = Cloud()
    client, provider = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )

    assert provider.calls[0]["messages"] == CLEANED
    (authorize,) = cloud.bodies("/v1/authorize")
    # The hash is of what left: the cleaned body, by the same function.
    assert authorize["request_hash"] == request_hash(
        {"model": "gpt-4o", "messages": CLEANED}
    )
    declared = authorize["content_policy"]
    assert declared == {
        "digest": content_policy_digest(policy_document()),
        "engine": ENGINE_VERSION,
        "rules": rules_digest(),
        "build": build_digest(),
        "challenge": {
            "token": CHALLENGE["token"],
            "response": challenge_response(CHALLENGE["token"], CHALLENGE["inputs"]),
        },
        "counts": {"email": 1, "custom:PROJECT": 1},
    }
    # The key set is public: it is fetched without the API key.
    (jwks_request,) = [
        r for r in cloud.requests if r.url.path == "/.well-known/jwks.json"
    ]
    assert "authorization" not in jwks_request.headers
    policy_request = cloud.requests[0]
    assert policy_request.headers["authorization"] == "Bearer sk_test_cleaning"


def test_a_call_with_nothing_to_mask_still_declares_the_policy() -> None:
    cloud = Cloud()
    client, provider = _wrapped(cloud)
    messages = [{"role": "user", "content": "hello"}]
    client.chat.completions.create(
        model="gpt-4o", messages=messages, user_id="usr_marie_456"
    )
    (authorize,) = cloud.bodies("/v1/authorize")
    assert authorize["content_policy"]["counts"] == {}
    assert authorize["request_hash"] == request_hash(
        {"model": "gpt-4o", "messages": messages}
    )
    assert provider.calls[0]["messages"] is messages


def test_only_changed_arguments_are_replaced() -> None:
    cloud = Cloud()
    client, provider = _wrapped(cloud)
    tools = [
        {
            "type": "function",
            "function": {"name": "f", "parameters": {"type": "object"}},
        }
    ]
    client.chat.completions.create(
        model="gpt-4o",
        messages=MESSAGES,
        tools=tools,
        temperature=0.2,
        user_id="usr_marie_456",
    )
    sent = provider.calls[0]
    assert sent["tools"] is tools
    assert sent["messages"] == CLEANED
    assert MESSAGES[1]["content"] == "Write to marie@example.com"  # never mutated


# ── what cleaning writes back into the call ─────────────────────────────────


class _Typed:
    """What a provider library's typed argument looks like to the SDK.

    ``model_dump()`` writes it as JSON, ``model_copy(update=…)`` copies it with
    some fields replaced, as a Pydantic v2 model does. Only the fields that
    were set are in ``fields_set``: what the library itself would write.
    """

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)
        self.fields_set = set(fields)

    def model_dump(self) -> dict[str, Any]:
        return {"refusal": None, **{k: self.__dict__[k] for k in self.fields_set}}

    def model_copy(self, *, update: dict[str, Any]) -> _Typed:
        return type(self)(**{k: self.__dict__[k] for k in self.fields_set} | update)


def _sent_with(policy: dict[str, Any] | None = None, **arguments: Any) -> Any:
    cloud = Cloud() if policy is None else Cloud(policy=policy)
    client, provider = _wrapped(cloud)
    client.chat.completions.create(model="gpt-4o", **arguments)
    (authorize,) = cloud.bodies("/v1/authorize")
    return provider.calls[0], authorize


def test_a_branch_cleaning_did_not_touch_leaves_as_the_callers_own_object() -> None:
    earlier = {"role": "assistant", "content": "Noted."}
    asked = {"role": "user", "content": "Write to marie@example.com"}
    sent, _ = _sent_with(messages=(earlier, asked))
    assert sent["messages"][0] is earlier
    assert sent["messages"][1] == {"role": "user", "content": "Write to [EMAIL_1]"}
    assert asked["content"] == "Write to marie@example.com"  # never mutated


def test_a_typed_argument_keeps_its_type_and_only_its_masked_field_changes() -> None:
    earlier = _Typed(role="assistant", content="Noted.")
    asked = _Typed(role="user", content="Write to marie@example.com")
    sent, authorize = _sent_with(messages=[earlier, asked])
    # The object the library was going to write is still the one it is given:
    # written as JSON here instead, it would carry `refusal: null`, a field the
    # caller never set and the provider may refuse.
    assert sent["messages"][0] is earlier
    masked = sent["messages"][1]
    assert isinstance(masked, _Typed) and masked is not asked
    assert masked.content == "Write to [EMAIL_1]"
    assert masked.fields_set == {"role", "content"}
    assert asked.content == "Write to marie@example.com"  # never mutated
    assert authorize["request_hash"] == request_hash(
        {
            "model": "gpt-4o",
            "messages": [
                {"refusal": None, "role": "assistant", "content": "Noted."},
                {"refusal": None, "role": "user", "content": "Write to [EMAIL_1]"},
            ],
        }
    )


def test_a_masked_string_is_written_at_any_depth_of_a_typed_argument() -> None:
    kept = _Typed(text="hello")
    turn = _Typed(
        role="user",
        parts=[kept, _Typed(text="Write to marie@example.com")],
        meta={"note": "about Projet Atlas", "ref": {"id": "r1"}},
    )
    sent, _ = _sent_with(messages=[turn])
    (written,) = sent["messages"]
    assert isinstance(written, _Typed) and written is not turn
    assert written.parts[0] is kept
    assert isinstance(written.parts[1], _Typed)
    assert written.parts[1].text == "Write to [EMAIL_1]"
    assert written.meta == {"note": "about [PROJECT_1]", "ref": {"id": "r1"}}
    assert written.meta["ref"] is turn.meta["ref"]
    assert turn.parts[1].text == "Write to marie@example.com"  # never mutated


def test_a_real_pydantic_model_is_copied_with_its_masked_field() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Message(pydantic.BaseModel):  # type: ignore[name-defined,misc]
        role: str
        content: str
        name: str | None = None

    asked = Message(role="user", content="Write to marie@example.com")
    sent, _ = _sent_with(messages=[asked])
    (written,) = sent["messages"]
    assert isinstance(written, Message)
    assert written.content == "Write to [EMAIL_1]"
    # What the library writes, unset fields left out, is unchanged but for it.
    assert written.model_dump(exclude_unset=True) == {
        "role": "user",
        "content": "Write to [EMAIL_1]",
    }
    assert asked.content == "Write to marie@example.com"  # never mutated


def test_a_class_standing_for_its_schema_leaves_as_its_cleaned_schema() -> None:
    class Contact:
        @classmethod
        def model_json_schema(cls) -> dict[str, Any]:
            return {"title": "Contact", "description": "As for marie@example.com"}

        @classmethod
        def model_copy(cls, **_: Any) -> Any:  # a class has the method too
            raise AssertionError("a class is not copied")

    sent, _ = _sent_with(
        messages=[{"role": "user", "content": "hello"}], response_format=Contact
    )
    assert sent["response_format"] == {
        "title": "Contact",
        "description": "As for [EMAIL_1]",
    }


class _Renamed:
    """Writes itself under a name that is not one of its attributes."""

    def __init__(self, body: str) -> None:
        self.body = body

    def model_dump(self) -> dict[str, Any]:
        return {"role": "user", "content": self.body}

    def model_copy(self, *, update: dict[str, Any]) -> Any:
        raise AssertionError("nothing here can be copied by field")


class _Uncopiable(_Typed):
    def model_copy(self, *, update: dict[str, Any]) -> _Typed:
        raise TypeError("frozen")


class _Reshaped(_Typed):
    """Its dump does not have the shape of what it holds."""

    def model_dump(self) -> dict[str, Any]:
        return {"role": "user", "parts": [{"text": part} for part in self.parts]}


class _BadCopy(_Typed):
    """Its copy is nothing the SDK can read back."""

    def model_copy(self, *, update: dict[str, Any]) -> Any:
        return object()


class _DumpOnly:
    def model_dump(self) -> dict[str, Any]:
        return {"role": "user", "content": "Write to marie@example.com"}


@pytest.mark.parametrize(
    "argument",
    [
        _Renamed("Write to marie@example.com"),
        _Uncopiable(role="user", content="Write to marie@example.com"),
        _DumpOnly(),
    ],
    ids=lambda argument: type(argument).__name__,
)
def test_an_object_that_cannot_be_written_into_leaves_as_its_cleaned_json(
    argument: Any,
) -> None:
    kept = {"role": "system", "content": "Be brief."}
    sent, authorize = _sent_with(messages=[kept, argument])
    assert sent["messages"][0] is kept
    written = sent["messages"][1]
    assert isinstance(written, dict)
    assert "marie@example.com" not in json.dumps(written)
    assert "[EMAIL_1]" in json.dumps(written)
    assert authorize["request_hash"] == request_hash(
        {"model": "gpt-4o", "messages": [kept, written]}
    )


@pytest.mark.parametrize(
    ("argument", "written"),
    [
        # Copied by field, it would write `parts` wrapped twice.
        (
            _Reshaped(role="user", parts=("Write to marie@example.com",)),
            {"role": "user", "parts": [{"text": "Write to [EMAIL_1]"}]},
        ),
        # Its copy cannot be read back at all.
        (
            _BadCopy(role="user", content="Write to marie@example.com"),
            {"refusal": None, "role": "user", "content": "Write to [EMAIL_1]"},
        ),
    ],
    ids=["reshaped", "unreadable"],
)
def test_a_copy_that_does_not_write_what_was_cleaned_is_not_what_leaves(
    argument: Any, written: dict[str, Any]
) -> None:
    kept = {"role": "system", "content": "Be brief."}
    sent, authorize = _sent_with(messages=[kept, argument])
    # Caught by reading the call once more: every argument then leaves as the
    # JSON that was cleaned.
    assert sent["messages"] == [kept, written]
    assert authorize["request_hash"] == request_hash(
        {"model": "gpt-4o", "messages": sent["messages"]}
    )


def test_an_object_that_writes_itself_differently_each_time_leaves_as_cleaned() -> None:
    class Shifting:
        written = 0

        def model_dump(self) -> dict[str, Any]:
            self.written += 1
            return {"role": "user", "content": f"reading {self.written}"}

    asked = {"role": "user", "content": "Write to marie@example.com"}
    sent, authorize = _sent_with(messages=[asked, Shifting()])
    # The reading that was cleaned is the one that leaves, as plain JSON: the
    # object itself would have written another one on its way out.
    assert sent["messages"] == [
        {"role": "user", "content": "Write to [EMAIL_1]"},
        {"role": "user", "content": "reading 2"},
    ]
    assert authorize["request_hash"] == request_hash(
        {"model": "gpt-4o", "messages": sent["messages"]}
    )


def test_a_block_rule_stops_the_call_before_anything_is_sent() -> None:
    cloud = Cloud()
    client, provider = _wrapped(cloud)
    with pytest.raises(ContentBlockedError) as raised:
        client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "card 4111 1111 1111 1111"}],
            user_id="usr_marie_456",
        )
    assert raised.value.blocked == {"credit_card": 1}
    assert isinstance(raised.value, IDSideError)
    assert "4111" not in str(raised.value)
    assert "credit_card (1)" in str(raised.value)
    assert provider.calls == []
    assert "/v1/authorize" not in cloud.paths()
    (event,) = cloud.bodies("/v1/content-events")
    assert event == {
        "digest": content_policy_digest(policy_document()),
        "engine": ENGINE_VERSION,
        "blocked": {"credit_card": 1},
        "provider": "openai",
        "model": "gpt-4o",
    }


_BLOCKED = [{"role": "user", "content": "card 4111 1111 1111 1111"}]


@pytest.mark.parametrize(
    ("model", "sent"),
    [
        ("gpt-4o-mini-2024-07-18", True),
        ("ft:gpt-4o:org:atlas:abc123", True),
        ("a", True),
        ("x" * 200, True),
        ("my model v2", True),  # a space inside is printable
        ("x" * 201, False),  # longer than the service keeps
        ("", False),
        (" gpt-4o", False),
        ("gpt-4o ", False),
        ("gpt\n4o", False),
        ("gpt\t4o", False),
        ("gpt-4o\x7f", False),
        ("modèle", False),  # ASCII only: no Unicode table to disagree on
        (4, False),  # not written as it would be read: left out, not "4"
        (None, False),
        (["gpt-4o"], False),
    ],
)
def test_a_blocked_call_reports_its_model_only_as_the_service_keeps_it(
    model: Any, sent: bool
) -> None:
    cloud = Cloud()
    client, _ = _wrapped(cloud)
    with pytest.raises(ContentBlockedError):
        client.chat.completions.create(model=model, messages=_BLOCKED)
    (event,) = cloud.bodies("/v1/content-events")
    # Left out, never null: the service refuses a member it cannot read, and
    # the whole report with it.
    assert event == {
        "digest": content_policy_digest(policy_document()),
        "engine": ENGINE_VERSION,
        "blocked": {"credit_card": 1},
        "provider": "openai",
        **({"model": model} if sent else {}),
    }


def test_a_blocked_call_without_a_model_reports_the_provider_alone() -> None:
    cloud = Cloud()
    client, _ = _wrapped(cloud)
    with pytest.raises(ContentBlockedError):
        client.chat.completions.create(messages=_BLOCKED)
    (event,) = cloud.bodies("/v1/content-events")
    assert "model" not in event
    assert event["provider"] == "openai"


@pytest.mark.parametrize(
    ("provider", "sent"),
    [
        ("openai", True),
        ("my_provider-2", True),
        ("0" + "x" * 63, True),
        ("0" + "x" * 64, False),
        ("Open AI", False),
        ("OpenAI", False),
        ("-openai", False),
        ("openai\n", False),
        ("", False),
        (None, False),
    ],
)
def test_a_report_names_the_provider_only_as_the_service_keeps_it(
    provider: Any, sent: bool
) -> None:
    from idside.consent import _report_blocked

    reported: list[dict[str, Any]] = []

    class Reporting:
        def content_events(self, **report: Any) -> None:
            reported.append(report)

    state = SimpleNamespace(digest="sha256:x", declaration={"engine": "1"})
    _report_blocked(
        Reporting(), state, {"email": 1}, {"provider": provider, "model": "m"}
    )
    expected: dict[str, Any] = {"digest": "sha256:x", "engine": "1"}
    expected["blocked"] = {"email": 1}
    if sent:
        expected["provider"] = provider
    expected["model"] = "m"
    assert reported == [expected]


def test_the_transport_writes_only_the_members_it_is_given() -> None:
    bodies: list[Any] = []

    def answer(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(204)

    client = HttpConsentClient(
        "https://cloud.example.test", transport=httpx.MockTransport(answer)
    )
    client.content_events(digest="sha256:x", engine="1", blocked={"email": 1})
    client.content_events(
        digest="sha256:x", engine="1", blocked={"email": 1}, provider="openai"
    )
    client.content_events(
        digest="sha256:x", engine="1", blocked={"email": 1}, model="gpt-4o"
    )
    async_client = AsyncHttpConsentClient(
        "https://cloud.example.test", transport=httpx.MockTransport(answer)
    )

    async def run() -> None:
        await async_client.content_events(
            digest="sha256:x", engine="1", blocked={"email": 1}
        )
        await async_client.content_events(
            digest="sha256:x",
            engine="1",
            blocked={"email": 1},
            provider="openai",
            model="gpt-4o",
        )

    asyncio.run(run())
    base = {"digest": "sha256:x", "engine": "1", "blocked": {"email": 1}}
    assert bodies == [
        base,
        {**base, "provider": "openai"},
        {**base, "model": "gpt-4o"},
        base,
        {**base, "provider": "openai", "model": "gpt-4o"},
    ]


@pytest.mark.parametrize("status", [500, 0])
def test_a_report_that_cannot_be_delivered_does_not_change_the_error(
    status: int,
) -> None:
    cloud = Cloud(events_status=status or 204)
    original = cloud.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if status == 0 and request.url.path == "/v1/content-events":
            raise httpx.ConnectError("down")
        return original(request)

    cloud.handle = handle  # type: ignore[method-assign]
    client, _ = _wrapped(cloud)
    with pytest.raises(ContentBlockedError):
        client.chat.completions.create(
            model="gpt-4o",
            messages=[{"content": "4111111111111111"}],
            user_id="usr_marie_456",
        )


def test_a_consent_client_whose_report_raises_still_blocks() -> None:
    from idside.consent import _report_blocked

    class Broken:
        def content_events(self, **_: Any) -> None:
            raise RuntimeError("down")

    state = SimpleNamespace(digest="sha256:x", declaration={"engine": "1"})
    request = {"provider": "openai", "model": "gpt-4o"}
    assert _report_blocked(Broken(), state, {"email": 1}, request) is None
    assert _report_blocked(object(), state, {"email": 1}, request) is None


def test_an_async_report_that_raises_does_not_change_the_error() -> None:
    inner = AsyncHttpConsentClient(
        "https://cloud.example.test",
        transport=httpx.MockTransport(Cloud().handle_async),
    )

    class Reporting:
        authorize = inner.authorize
        content_policy = inner.content_policy

        async def content_events(self, **_: Any) -> None:
            raise RuntimeError("down")

    recorder = _AsyncRecorder()
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=recorder)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=Reporting(),
    )

    async def run() -> None:
        with pytest.raises(ContentBlockedError):
            await client.chat.completions.create(
                model="gpt-4o", messages=[{"content": "4111111111111111"}]
            )

    asyncio.run(run())
    assert recorder.calls == []


def test_attachments_can_be_blocked() -> None:
    cloud = Cloud(policy=policy_document(attachments="block"))
    client, provider = _wrapped(cloud)
    with pytest.raises(ContentBlockedError) as raised:
        client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://img.example.com/a.png"},
                        }
                    ],
                }
            ],
            user_id="usr_marie_456",
        )
    assert raised.value.blocked == {"attachments": 1}
    assert provider.calls == []


def test_the_journal_names_counts_never_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cloud = Cloud()
    client, _ = _wrapped(cloud)
    with caplog.at_level(logging.DEBUG, logger="idside"):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
        with pytest.raises(ContentBlockedError):
            client.chat.completions.create(
                model="gpt-4o",
                messages=[{"content": "4111111111111111"}],
                user_id="usr_marie_456",
            )
    text = "\n".join(record.getMessage() for record in caplog.records)
    digest = content_policy_digest(policy_document()).removeprefix("sha256:")[:12]
    site = "call_site=chat.completions.create"
    assert f"cleaned {site} policy={digest} masked=custom:PROJECT:1+email:1" in text
    assert f"content_blocked {site} policy={digest} blocked=credit_card:1" in text
    for value in ("marie", "Atlas", "4111"):
        assert value not in text


def test_a_call_naming_nobody_is_cleaned_too() -> None:
    cloud = Cloud()
    client, provider = _wrapped(cloud)
    client.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    assert provider.calls[0]["messages"] == CLEANED
    assert cloud.bodies("/v1/authorize")[0]["content_policy"]["counts"]["email"] == 1


# ── no way around it ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "escape",
    [
        {"content_policy": False},
        {"skip_cleaning": True},
        {"cleaning": False},
        {"idside_clean": False},
    ],
)
def test_no_per_call_option_turns_cleaning_off(escape: dict[str, Any]) -> None:
    cloud = Cloud()
    client, provider = _wrapped(cloud)
    with pytest.raises(UnenforceableOptionError):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456", **escape
        )
    assert provider.calls == []


def test_an_unset_option_spelled_like_one_is_not_a_switch_either() -> None:
    # `None` is how a library spells "not given"; it is let through as such,
    # and cleaning applies all the same.
    cloud = Cloud()
    client, provider = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456", content_policy=None
    )
    assert provider.calls[0]["messages"] == CLEANED
    assert cloud.bodies("/v1/authorize")[0]["content_policy"]["counts"]["email"] == 1


@pytest.mark.parametrize(
    "hatch", ["content_policy", "cleaning", "clean", "skip_cleaning"]
)
def test_wrap_takes_no_cleaning_option(hatch: str) -> None:
    client = SimpleNamespace(chat=SimpleNamespace(completions=_Recorder()))
    with pytest.raises(TypeError):
        consent.wrap(client, adapter=ProviderAdapter(_OPENAI), **{hatch: False})


# ── a policy that does not check out is never applied ───────────────────────


def _answer_with(token: str) -> dict[str, Any]:
    return {
        "enabled": True,
        "policy": token,
        "challenge": CHALLENGE,
        "refresh_after": 300,
    }


def _refused(cloud: Cloud, match: str) -> None:
    client, provider = _wrapped(cloud)
    with pytest.raises(ContentPolicyError, match=match):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
    assert provider.calls == []
    assert "/v1/authorize" not in cloud.paths()


def test_a_tampered_signature_is_refused() -> None:
    key = SigningKey()
    _refused(
        Cloud(
            key=key,
            answer=_answer_with(jws(key, claims_for(policy_document()), tamper=True)),
        ),
        "signature",
    )


def test_a_policy_signed_by_another_key_is_refused() -> None:
    cloud = Cloud()
    other = SigningKey(kid=cloud.key.kid)
    cloud.answer = _answer_with(jws(other, claims_for(policy_document())))
    _refused(cloud, "signature")


@pytest.mark.parametrize(
    ("header", "match"),
    [
        (
            {"alg": "HS256", "typ": "content-policy+jwt", "kid": "content-policy-g1"},
            "not ES256",
        ),
        ({"alg": "ES256", "typ": "act+jwt", "kid": "content-policy-g1"}, "of type"),
        ({"alg": "ES256", "typ": "content-policy+jwt"}, "well-formed"),
        (
            {
                "alg": "ES256",
                "typ": "content-policy+jwt",
                "kid": "content-policy-g1",
                "crit": ["x"],
            },
            "well-formed",
        ),
        (
            {"alg": "ES256", "typ": "content-policy+jwt", "kid": "content-policy-g9"},
            "names a key",
        ),
        (
            {"alg": "ES256", "typ": "content-policy+jwt", "kid": "unknown-kid"},
            "not signed with a prompt-cleaning policy key",
        ),
    ],
)
def test_a_header_that_is_not_a_policy_header_is_refused(
    header: dict[str, Any], match: str
) -> None:
    key = SigningKey()
    _refused(
        Cloud(
            key=key,
            answer=_answer_with(jws(key, claims_for(policy_document()), header=header)),
        ),
        match,
    )


@pytest.mark.parametrize(
    "kid",
    ["act-g1", "ubt-g1", "act-g1-staging", "content-policy", "Content-policy-g1"],
)
def test_a_key_of_another_purpose_never_signs_a_policy(kid: str) -> None:
    # The cloud names each signing key after its purpose (`act-g1`,
    # `content-policy-g1`, with `-staging` or `-dev` outside production) and
    # publishes them in one key set. The signature below is valid and its key
    # is in that set: only its purpose is wrong. An ACT key must not be able to
    # sign what a call is cleaned by.
    cloud = Cloud(key=SigningKey(kid=kid))
    _refused(cloud, "not signed with a prompt-cleaning policy key")
    assert "/.well-known/jwks.json" not in cloud.paths()  # refused before any lookup


@pytest.mark.parametrize("kid", ["content-policy-g2", "content-policy-g1-staging"])
def test_any_generation_of_the_policy_key_verifies(kid: str) -> None:
    cloud = Cloud(key=SigningKey(kid=kid))
    client, provider = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert provider.calls[0]["messages"] == CLEANED


@pytest.mark.parametrize(
    ("claims", "match"),
    [
        ({"iss": "evil.example"}, "issued by ID Side"),
        ({"exp": NOW}, "past its expiry"),
        ({"iat": NOW + 301, "exp": NOW + 900}, "before its date of issue"),
        ({"iat": NOW, "exp": NOW - 1 + 1}, "expires before it was issued"),
        ({"iat": NOW + 10, "exp": NOW + 5}, "expires before it was issued"),
        ({"iat": "1", "exp": NOW + 900}, "integer"),
        ({"exp": NOW + 890.5}, "integer"),
        ({"iat": True, "exp": NOW + 900}, "integer"),
        ({"digest": "sha256:" + "0" * 64}, "digest"),
        ({"app_id": "app_other"}, "application"),
        ({"app_id": None}, "application"),
    ],
)
def test_claims_that_do_not_check_out_are_refused(
    claims: dict[str, Any], match: str
) -> None:
    key = SigningKey()
    _refused(
        Cloud(
            key=key,
            answer=_answer_with(jws(key, claims_for(policy_document(), **claims))),
        ),
        match,
    )


CLOCK_AHEAD = (
    "The prompt-cleaning policy just received from the ID Side cloud is signed "
    "and intact, but this machine's clock puts it past its expiry: either the "
    "clock looks ahead, or a cache between this machine and the cloud served an "
    "old answer. Check that this machine's clock is set (NTP) and that no proxy "
    "caches /v1/content-policy; the AI call was not made."
)
CLOCK_BEHIND = (
    "The prompt-cleaning policy just received from the ID Side cloud is signed "
    "and intact, but this machine's clock puts it before its date of issue: the "
    "clock looks behind. Check that this machine's clock is set (NTP); the AI call "
    "was not made."
)


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        # The policy is valid 900 s from `iat`, and `iat` may be up to 300 s
        # ahead of this machine: past either bound, the clock is what is wrong.
        (900, CLOCK_AHEAD),
        (3600, CLOCK_AHEAD),
        (-301, CLOCK_BEHIND),
        (-86_400, CLOCK_BEHIND),
    ],
)
def test_a_policy_out_of_step_with_this_clock_says_so(
    offset: int, expected: str, _frozen_clock: list[float]
) -> None:
    # The cloud signs at its own time; this machine reads it at `now`. A
    # developer must read "check your clock", not an outage of ID Side.
    cloud = Cloud()
    _frozen_clock[0] = NOW - 10 + offset  # the policy was issued at NOW - 10
    client, provider = _wrapped(cloud)
    with pytest.raises(ContentPolicyError) as raised:
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
    assert str(raised.value) == expected
    for value in ("app_medhelp", "Projet Atlas", "sha256", CHALLENGE["token"], "18"):
        assert value not in str(raised.value)
    assert provider.calls == []


@pytest.mark.parametrize("offset", [889, -300])
def test_the_bounds_are_unchanged(offset: int, _frozen_clock: list[float]) -> None:
    cloud = Cloud()
    _frozen_clock[0] = NOW - 10 + offset
    client, provider = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert provider.calls[0]["messages"] == CLEANED


def test_the_clock_is_named_only_for_a_policy_that_checks_out_otherwise(
    _frozen_clock: list[float],
) -> None:
    # Expired AND tampered, or expired AND for another application: the clock
    # is not the problem worth naming first.
    key = SigningKey()
    expired = claims_for(policy_document(), exp=NOW)
    _refused(
        Cloud(key=key, answer=_answer_with(jws(key, expired, tamper=True))),
        "signature",
    )
    _refused(
        Cloud(key=key, answer=_answer_with(jws(key, {**expired, "app_id": "x"}))),
        "application",
    )
    _refused(
        Cloud(key=key, answer=_answer_with(jws(key, {**expired, "digest": "d"}))),
        "digest",
    )


def test_a_policy_this_engine_cannot_apply_is_refused() -> None:
    key = SigningKey()
    policy = policy_document(schema=2)
    _refused(
        Cloud(key=key, answer=_answer_with(jws(key, claims_for(policy)))), "schema"
    )


def test_a_policy_that_is_not_an_object_is_refused() -> None:
    key = SigningKey()
    claims = claims_for(policy_document())
    claims["policy"] = ["not", "an", "object"]
    claims["digest"] = content_policy_digest(claims["policy"])
    _refused(Cloud(key=key, answer=_answer_with(jws(key, claims))), "application")


def test_a_signed_policy_that_names_no_digest_is_refused_whatever_it_holds() -> None:
    key = SigningKey()
    claims = claims_for(policy_document())
    del claims["digest"]
    # A policy the canonical form cannot write has no digest either: "none"
    # must not equal a claim that is not there.
    claims["policy"] = {**policy_document(), "note": "\ud800"}
    _refused(Cloud(key=key, answer=_answer_with(jws(key, claims))), "digest")


def test_a_policy_answer_with_no_body_is_refused_as_not_a_json_object() -> None:
    cloud = Cloud()
    original = cloud.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/content-policy":
            cloud.requests.append(request)
            return httpx.Response(204)
        return original(request)

    cloud.handle = handle  # type: ignore[method-assign]
    _refused(cloud, "is not a JSON object")


def test_a_policy_holding_a_value_json_cannot_hash_is_refused() -> None:
    key = SigningKey()
    claims = claims_for(policy_document())
    signing_input = (
        b64url(
            json.dumps(
                {"alg": "ES256", "typ": "content-policy+jwt", "kid": key.kid}
            ).encode()
        )
        + "."
        + b64url(json.dumps({**claims, "policy": {"x": float("nan")}}).encode())
    )
    token = signing_input + "." + b64url(key.sign(signing_input.encode()))
    _refused(Cloud(key=key, answer=_answer_with(token)), "digest")


@pytest.mark.parametrize(
    ("answer", "match"),
    [
        ([], "not a JSON object"),
        ({}, "whether cleaning is enabled"),
        ({"enabled": "yes"}, "whether cleaning is enabled"),
        ({"enabled": False, "refresh_after": -1}, "refresh_after"),
        ({"enabled": False, "refresh_after": True}, "refresh_after"),
        ({"enabled": False, "refresh_after": 86_401}, "refresh_after"),
        ({"enabled": True, "challenge": CHALLENGE}, "no signed policy"),
        ({"enabled": True, "policy": "a.b.c"}, "no usable challenge"),
        (
            {
                "enabled": True,
                "policy": "a.b.c",
                "challenge": {"token": "", "inputs": []},
            },
            "no usable challenge",
        ),
        (
            {
                "enabled": True,
                "policy": "a.b.c",
                "challenge": {"token": "t", "inputs": [1]},
            },
            "no usable challenge",
        ),
        (
            {
                "enabled": True,
                "policy": "a.b.c",
                "challenge": {"token": "t", "inputs": ["x"] * 65},
            },
            "no usable challenge",
        ),
        ({"enabled": True, "policy": "a.b", "challenge": CHALLENGE}, "compact JWS"),
        ({"enabled": True, "policy": "a..c", "challenge": CHALLENGE}, "compact JWS"),
        ({"enabled": True, "policy": "!.b.c", "challenge": CHALLENGE}, "well-formed"),
    ],
)
def test_an_answer_that_is_not_one_is_refused(answer: Any, match: str) -> None:
    _refused(Cloud(answer=answer), match)


def test_an_answer_that_is_not_json_is_refused() -> None:
    cloud = Cloud()
    original = cloud.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/content-policy":
            cloud.requests.append(request)
            return httpx.Response(200, content=b"<html>")
        return original(request)

    cloud.handle = handle  # type: ignore[method-assign]
    _refused(cloud, "not a JSON object")


# ── a cloud that does not answer ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (404, ConsentUnreachableError),
        (503, ConsentUnreachableError),
        (401, AuthenticationError),
        (402, QuotaExceededError),
        (403, ConsentDeniedError),
        (422, ContractError),
        (429, RateLimitedError),
    ],
)
def test_a_policy_endpoint_error_fails_closed(
    status: int, error: type[Exception]
) -> None:
    cloud = Cloud(policy_status=status)
    client, provider = _wrapped(cloud)
    with pytest.raises(error) as raised:
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
    assert provider.calls == []
    # Whatever its class, the message names the route that answered.
    assert "prompt-cleaning policy" in str(raised.value)
    assert "verify consent" not in str(raised.value)


@pytest.mark.parametrize("status", [404, 409, 418, 500, 503])
def test_a_policy_endpoint_error_names_the_policy_and_the_status(status: int) -> None:
    # Not "Could not verify consent": /v1/authorize may be answering well, and
    # the developer must look at the policy route, not at authorization. A
    # qualified refusal (401, 402, 403, 422, 429) keeps its own class.
    cloud = Cloud(policy_status=status)
    client, provider = _wrapped(cloud)
    with pytest.raises(ConsentUnreachableError) as raised:
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
    assert str(raised.value) == (
        f"The ID Side cloud's prompt-cleaning policy could not be fetched (HTTP "
        f"{status}), and none is held that is still valid; the AI call was not "
        "made. Cloud says: nope."
    )
    assert provider.calls == []


def test_a_policy_endpoint_error_on_the_default_cloud_names_the_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IDSIDE_BASE_URL", raising=False)
    cloud = Cloud(policy_status=503)
    consent_client = HttpConsentClient(transport=httpx.MockTransport(cloud.handle))
    with pytest.raises(ConsentUnreachableError) as raised:
        consent_client.content_policy()
    assert "(HTTP 503)" in str(raised.value)
    assert "IDSIDE_BASE_URL" in str(raised.value)


@pytest.mark.parametrize("status", [404, 0])
def test_a_key_set_that_cannot_be_fetched_fails_closed(status: int) -> None:
    cloud = Cloud(jwks_status=status or 200)
    original = cloud.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if status == 0 and request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, content=b"not json")
        return original(request)

    cloud.handle = handle  # type: ignore[method-assign]
    client, provider = _wrapped(cloud)
    with pytest.raises(ConsentUnreachableError):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
    assert provider.calls == []


@pytest.mark.parametrize("route", ["/v1/content-policy", "/.well-known/jwks.json"])
def test_a_network_failure_fails_closed(route: str) -> None:
    cloud = Cloud()
    original = cloud.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == route:
            raise httpx.ConnectError("down")
        return original(request)

    cloud.handle = handle  # type: ignore[method-assign]
    client, _ = _wrapped(cloud)
    with pytest.raises(ConsentUnreachableError):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )


# ── caching and refreshing ──────────────────────────────────────────────────


def test_the_policy_is_fetched_once_then_refreshed_after_refresh_after(
    _frozen_clock: list[float],
) -> None:
    cloud = Cloud()
    client, _ = _wrapped(cloud)
    for _ in range(3):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
    assert cloud.paths().count("/v1/content-policy") == 1
    assert cloud.paths().count("/.well-known/jwks.json") == 1
    _frozen_clock[0] += 300
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert cloud.paths().count("/v1/content-policy") == 2
    assert cloud.paths().count("/.well-known/jwks.json") == 1  # the key is known


def test_a_failed_refresh_keeps_a_policy_still_valid_then_fails(
    _frozen_clock: list[float],
) -> None:
    cloud = Cloud()
    client, provider = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    cloud.policy_status = 503
    _frozen_clock[0] += 600  # refresh due, policy valid until NOW + 890
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert provider.calls[-1]["messages"] == CLEANED
    _frozen_clock[0] = NOW + 890  # expired
    with pytest.raises(ConsentUnreachableError):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )


@pytest.mark.parametrize("is_async", [False, True])
def test_a_policy_that_ran_out_while_its_refresh_was_failing_is_not_applied(
    is_async: bool, _frozen_clock: list[float]
) -> None:
    cloud = Cloud()
    original = cloud.handle
    late = False

    def handle(request: httpx.Request) -> httpx.Response:
        if late and request.url.path == "/v1/content-policy":
            # The refresh takes its time, and fails: by then the policy held
            # has expired.
            _frozen_clock[0] = NOW + 890
            raise httpx.ConnectError("down")
        return original(request)

    cloud.handle = handle  # type: ignore[method-assign]
    client, provider = _wrapped_async(cloud) if is_async else _wrapped(cloud)

    async def call() -> None:
        made = client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
        if is_async:
            await made

    async def run() -> None:
        nonlocal late
        await call()
        _frozen_clock[0] += 600  # refresh due, policy valid until NOW + 890
        late = True
        with pytest.raises(ConsentUnreachableError):
            await call()

    asyncio.run(run())
    assert len(provider.calls) == 1


@pytest.mark.parametrize("is_async", [False, True])
def test_a_policy_that_does_not_check_out_is_not_swallowed_by_a_held_one(
    is_async: bool, _frozen_clock: list[float]
) -> None:
    # Only an outage falls back on the policy held: a refresh the cloud answered
    # with something that does not check out is refused, not papered over.
    cloud = Cloud()
    client, provider = _wrapped_async(cloud) if is_async else _wrapped(cloud)

    async def call() -> Any:
        return await client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )

    def run() -> Any:
        if is_async:
            return asyncio.run(call())
        return client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )

    run()
    _frozen_clock[0] += 600  # refresh due, the policy held still valid
    cloud.answer = {"enabled": "maybe"}
    with pytest.raises(ContentPolicyError):
        run()
    assert len(provider.calls) == 1


def test_a_disabled_answer_stands_for_its_window_when_it_cannot_be_refreshed(
    _frozen_clock: list[float],
) -> None:
    cloud = Cloud(policy=None)
    client, _ = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    cloud.policy_status = 503
    _frozen_clock[0] += 600
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    _frozen_clock[0] = NOW + 900
    with pytest.raises(ConsentUnreachableError):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )


@pytest.mark.parametrize("is_async", [False, True])
def test_a_refresh_that_brings_the_same_policy_compiles_nothing_again(
    is_async: bool, _frozen_clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled: list[object] = []
    real = _content_policy.ContentCleaner

    class Counting(real):  # type: ignore[valid-type,misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            compiled.append(self)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(_content_policy, "ContentCleaner", Counting)
    cloud = Cloud(policy=policy_document(custom=[]))
    client, provider = _wrapped_async(cloud) if is_async else _wrapped(cloud)

    async def call() -> None:
        made = client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
        if is_async:
            await made

    async def run() -> None:
        await call()
        once = len(compiled)
        assert once > 0
        # Due for refresh: fetched and verified again, the same digest.
        _frozen_clock[0] += 400
        await call()
        assert cloud.paths().count("/v1/content-policy") == 2
        assert len(compiled) == once
        # A new digest is a new policy: compiled, and applied.
        cloud.policy = policy_document()
        _frozen_clock[0] += 400
        await call()
        assert cloud.paths().count("/v1/content-policy") == 3
        assert len(compiled) == 2 * once

    asyncio.run(run())
    assert [sent["messages"] for sent in provider.calls] == [
        [MESSAGES[0], CLEANED[1]],
        [MESSAGES[0], CLEANED[1]],
        CLEANED,
    ]


class _WatchedLock:
    """A lock that says when a thread had to wait for it."""

    def __init__(self, contended: threading.Event) -> None:
        self._lock = threading.Lock()
        self._contended = contended

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self._contended.set()
            self._lock.acquire()

    def __exit__(self, *_: object) -> None:
        self._lock.release()


def test_an_answer_heard_during_a_refresh_never_brings_back_the_older_policy(
    _frozen_clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Thread B refreshes the policy while thread A's authorize answer asks for
    # a fresh one. A read the policy held before B's arrived; whatever the
    # order, what A writes must never put that older policy back.
    cloud = Cloud(
        authorize_extra={"content_policy": {"status": "outdated", "digest": "d"}}
    )
    fetching, go, a_inside, b_waits = (threading.Event() for _ in range(4))
    original = cloud.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/content-policy" and fetching.is_set():
            go.wait(5)  # B's refresh, held until A has read the policy
        return original(request)

    client = HttpConsentClient(
        "https://cloud.example.test", transport=httpx.MockTransport(handle)
    )
    older = client.content_policy()
    client._flight_lock = _WatchedLock(b_waits)  # type: ignore[assignment]
    cloud.policy = policy_document(revision=8)
    _frozen_clock[0] += 400  # refresh due, the older policy still valid
    real_replace = _content_policy.replace

    def replace(state: Any, **changes: Any) -> Any:
        if threading.current_thread().name == "answer":
            a_inside.set()
            b_waits.wait(5)  # until B waits for the lock, or has written
        return real_replace(state, **changes)

    monkeypatch.setattr(_content_policy, "replace", replace)
    fetching.set()
    refresh = threading.Thread(target=client.content_policy, name="refresh")
    refresh.start()
    answer = threading.Thread(
        target=lambda: client.authorize(
            provider="openai", model="gpt-4o", request_hash="sha256:" + "0" * 64
        ),
        name="answer",
    )
    answer.start()
    assert a_inside.wait(5)
    go.set()
    refresh.join(5)
    b_waits.set()
    answer.join(5)
    assert not refresh.is_alive() and not answer.is_alive()
    held = client._policy.state
    assert held is not None
    assert held.digest == content_policy_digest(policy_document(revision=8))
    assert held.digest != older.digest


@pytest.mark.parametrize("status", ["outdated", "not_applied", "anomaly"])
def test_an_answer_asking_for_a_fresh_policy_is_heard(
    status: str, _frozen_clock: list[float]
) -> None:
    cloud = Cloud(
        authorize_extra={"content_policy": {"status": status, "digest": "sha256:new"}}
    )
    client, _ = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    # Fetched again on the next call — once ten seconds have passed since the
    # last fetch: while a policy is held, the cloud is asked at most that often.
    _frozen_clock[0] += 9
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert cloud.paths().count("/v1/content-policy") == 1
    _frozen_clock[0] += 1
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert cloud.paths().count("/v1/content-policy") == 2
    # ...and at most once every ten seconds, whatever the cloud keeps saying.
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert cloud.paths().count("/v1/content-policy") == 2
    _frozen_clock[0] += 10
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert cloud.paths().count("/v1/content-policy") == 3


def test_not_applied_while_cleaning_is_off_here_fetches_the_policy_at_once(
    _frozen_clock: list[float],
) -> None:
    # The cloud says cleaning is on while the answer held says off (switched on
    # since): the next call fetches it, rather than waiting `refresh_after` —
    # at once when the last fetch is ten seconds old, as it almost always is.
    cloud = Cloud(
        policy=None,
        authorize_extra={"content_policy": {"status": "not_applied", "digest": "d"}},
    )
    client, provider = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert provider.calls[0]["messages"] is MESSAGES
    cloud.policy = policy_document()
    _frozen_clock[0] += 10
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert cloud.paths().count("/v1/content-policy") == 2
    assert provider.calls[1]["messages"] == CLEANED
    assert "content_policy" in cloud.bodies("/v1/authorize")[1]


def _failing_policy(cloud: Cloud, how: str) -> None:
    """Make every later ``/v1/content-policy`` fetch fail, the way ``how`` says."""
    if how in ("503", "404"):
        cloud.policy_status = int(how)
        return
    if how == "refused":
        cloud.answer = {"enabled": "maybe"}
        return
    original = cloud.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/content-policy":
            cloud.requests.append(request)
            raise httpx.ConnectError("down")
        return original(request)

    cloud.handle = handle  # type: ignore[method-assign]


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("how", ["503", "404", "network", "refused"])
def test_a_failed_refresh_is_retried_at_most_every_ten_seconds_while_a_policy_is_held(
    how: str, is_async: bool, _frozen_clock: list[float]
) -> None:
    # During an incident, every AI call would otherwise pay the fetch's timeout
    # and double the cloud's load: the policy held, still valid, is applied
    # between two attempts.
    cloud = Cloud()
    client, provider = _wrapped_async(cloud) if is_async else _wrapped(cloud)

    def run() -> None:
        async def call() -> Any:
            return await client.chat.completions.create(
                model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
            )

        if is_async:
            asyncio.run(call())
        else:
            client.chat.completions.create(
                model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
            )

    def fetches() -> int:
        return cloud.paths().count("/v1/content-policy")

    run()
    _failing_policy(cloud, how)
    _frozen_clock[0] += 300  # refresh due; the policy held is valid to NOW + 890
    if how == "refused":
        with pytest.raises(ContentPolicyError):  # an attempt still says what failed
            run()
    else:
        run()
    assert fetches() == 2
    for _ in range(3):
        run()  # no attempt: the policy held is applied
    _frozen_clock[0] += 9.5
    run()
    assert fetches() == 2
    assert all(call["messages"] == CLEANED for call in provider.calls)
    _frozen_clock[0] += 0.5
    if how == "refused":
        with pytest.raises(ContentPolicyError):
            run()
    else:
        run()
    assert fetches() == 3


@pytest.mark.parametrize("is_async", [False, True])
def test_once_no_valid_policy_is_held_every_call_tries_again(
    is_async: bool, _frozen_clock: list[float]
) -> None:
    # Never delay an attempt that could unblock the call: past the policy's
    # expiry nothing may be applied, so each call asks, and fails closed.
    cloud = Cloud()
    client, provider = _wrapped_async(cloud) if is_async else _wrapped(cloud)

    def run() -> None:
        async def call() -> Any:
            return await client.chat.completions.create(
                model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
            )

        if is_async:
            asyncio.run(call())
        else:
            client.chat.completions.create(
                model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
            )

    run()
    cloud.policy_status = 503
    _frozen_clock[0] = NOW + 885
    run()  # an attempt, failed; the policy held is still valid
    _frozen_clock[0] = NOW + 890  # expired, five seconds after that attempt
    for attempt in range(3):
        with pytest.raises(ConsentUnreachableError):
            run()
        assert cloud.paths().count("/v1/content-policy") == 3 + attempt
    cloud.policy_status = 200
    # The cloud is back, signing at its own time: the very next call recovers.
    cloud.answer = _answer_with(
        jws(cloud.key, claims_for(policy_document(), now=NOW + 890))
    )
    run()
    assert len(provider.calls) == 3


def test_a_clock_running_behind_keeps_a_policy_no_longer_than_its_lifetime(
    _frozen_clock: list[float], _monotonic: list[float]
) -> None:
    # Four minutes behind, within the tolerance: by this machine's wall clock
    # the policy would stand four minutes past its real expiry. Its lifetime
    # counts from its reception, on the monotonic clock: `exp - iat` seconds.
    cloud = Cloud()
    _frozen_clock[0] = NOW - 240
    client, provider = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    cloud.policy_status = 503
    _frozen_clock[0] += 899
    _monotonic[0] += 899
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert provider.calls[-1]["messages"] == CLEANED
    _frozen_clock[0] += 1
    _monotonic[0] += 1
    with pytest.raises(ConsentUnreachableError):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )


def _aged_answer(cloud: Cloud, age: int) -> dict[str, Any]:
    """A policy signed ``age`` seconds before it arrives, as a short cache serves it."""
    claims = claims_for(policy_document(), iat=NOW - age, exp=NOW - age + 900)
    return {
        "enabled": True,
        "policy": jws(cloud.key, claims),
        "challenge": CHALLENGE,
        "refresh_after": 300,
    }


def test_an_aged_policy_on_a_clock_behind_ends_at_the_earlier_bound(
    _frozen_clock: list[float], _monotonic: list[float]
) -> None:
    # Signed 30 s before it arrives, on a machine four minutes behind. By this
    # machine's wall clock it would stand four minutes past its real `exp`; from
    # its reception, `exp - iat` stands 30 s past it. Held: the earlier of the
    # two — never applied once the real `exp` is 30 s gone.
    cloud = Cloud()
    cloud.answer = _aged_answer(cloud, 30)
    _frozen_clock[0] = NOW - 240
    client, provider = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    cloud.policy_status = 503
    _frozen_clock[0] += 899  # real time: `exp` + 29 s
    _monotonic[0] += 899
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert provider.calls[-1]["messages"] == CLEANED
    _frozen_clock[0] += 1  # real time: `exp` + 30 s
    _monotonic[0] += 1
    with pytest.raises(ConsentUnreachableError):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )


def test_an_aged_policy_on_a_clock_ahead_ends_at_its_signed_expiry(
    _frozen_clock: list[float], _monotonic: list[float]
) -> None:
    # Signed 30 s before it arrives, on a machine two minutes ahead: from its
    # reception it would stand 30 s past `exp`, but this machine's wall clock
    # reaches `exp` two minutes early, and that ends it — the earlier bound.
    cloud = Cloud()
    cloud.answer = _aged_answer(cloud, 30)
    _frozen_clock[0] = NOW + 120
    client, provider = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    cloud.policy_status = 503
    _frozen_clock[0] += 749  # this machine: `exp` - 1 s
    _monotonic[0] += 749
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert provider.calls[-1]["messages"] == CLEANED
    _frozen_clock[0] += 1  # this machine: `exp`
    _monotonic[0] += 1
    with pytest.raises(ConsentUnreachableError):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )


def test_the_wall_clock_expiry_still_bounds_a_held_policy(
    _frozen_clock: list[float], _monotonic: list[float]
) -> None:
    # A clock set forward past `exp` while the process runs: the policy is no
    # longer applied, whatever the monotonic clock says.
    cloud = Cloud()
    client, _ = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    cloud.policy_status = 503
    _frozen_clock[0] = NOW + 890
    with pytest.raises(ConsentUnreachableError):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )


def test_a_policy_asked_to_refresh_keeps_its_wall_clock_expiry(
    _frozen_clock: list[float], _monotonic: list[float]
) -> None:
    # An `outdated` answer makes the policy held due at once; it must still not
    # be applied past its signed `exp`.
    cloud = Cloud(
        authorize_extra={"content_policy": {"status": "outdated", "digest": "d"}}
    )
    client, _ = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    cloud.policy_status = 503
    _frozen_clock[0] = NOW + 890
    with pytest.raises(ConsentUnreachableError):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )


@pytest.mark.parametrize("is_async", [False, True])
def test_refresh_and_its_spacing_count_on_the_monotonic_clock(
    is_async: bool, _monotonic: list[float]
) -> None:
    # The wall clock stands still: time passing is read on the monotonic one.
    cloud = Cloud()
    client, _ = _wrapped_async(cloud) if is_async else _wrapped(cloud)

    def run() -> None:
        async def call() -> Any:
            return await client.chat.completions.create(
                model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
            )

        if is_async:
            asyncio.run(call())
        else:
            client.chat.completions.create(
                model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
            )

    def fetches() -> int:
        return cloud.paths().count("/v1/content-policy")

    run()
    _monotonic[0] += 10  # past the spacing, not yet due: nothing is asked
    run()
    assert fetches() == 1
    _monotonic[0] += 290
    run()
    assert fetches() == 2
    cloud.policy_status = 503
    _monotonic[0] += 300
    run()
    assert fetches() == 3
    _monotonic[0] += 9.5
    run()
    assert fetches() == 3
    _monotonic[0] += 0.5
    run()
    assert fetches() == 4


def test_a_held_policy_never_shows_its_entries_in_a_repr() -> None:
    # Custom entries are the organization's own words; a state printed in a
    # traceback or a debugger must not spell them out.
    cloud = Cloud()
    consent_client = HttpConsentClient(
        "https://cloud.example.test", transport=httpx.MockTransport(cloud.handle)
    )
    state = consent_client.content_policy()
    assert state.enabled
    assert "Projet Atlas" not in repr(state)
    assert "chl_test_0001" not in repr(state)


def test_an_applied_answer_changes_nothing() -> None:
    cloud = Cloud(
        authorize_extra={"content_policy": {"status": "applied", "digest": "sha256:x"}}
    )
    client, _ = _wrapped(cloud)
    for _ in range(3):
        client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
    assert cloud.paths().count("/v1/content-policy") == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("applied", None),
        ({"digest": "x"}, None),
        ({"status": 1}, None),
        (
            {"status": "applied", "digest": "d", "extra": 1},
            {"status": "applied", "digest": "d"},
        ),
    ],
)
def test_the_answer_field_is_read_leniently(value: Any, expected: Any) -> None:
    assert _content_policy_answer(value) == expected


def test_the_decision_carries_the_answer() -> None:
    cloud = Cloud(
        authorize_extra={"content_policy": {"status": "applied", "digest": "d"}}
    )
    consent_client = HttpConsentClient(
        "https://cloud.example.test", transport=httpx.MockTransport(cloud.handle)
    )
    decision = consent_client.authorize(
        provider="openai", model="m", request_hash="sha256:" + "0" * 64
    )
    assert decision.content_policy == {"status": "applied", "digest": "d"}


# ── async, and the helpers that defer their request ─────────────────────────


def test_async_cleaning_masks_declares_and_blocks() -> None:
    cloud = Cloud()
    client, provider = _wrapped_async(cloud)

    async def run() -> None:
        await client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
        with pytest.raises(ContentBlockedError):
            await client.chat.completions.create(
                model="gpt-4o",
                messages=[{"content": "4111111111111111"}],
                user_id="usr_marie_456",
            )

    asyncio.run(run())
    assert provider.calls[0]["messages"] == CLEANED
    assert cloud.bodies("/v1/authorize")[0]["content_policy"]["counts"] == {
        "email": 1,
        "custom:PROJECT": 1,
    }
    assert cloud.bodies("/v1/content-events") == [
        {
            "digest": content_policy_digest(policy_document()),
            "engine": "1",
            "blocked": {"credit_card": 1},
            "provider": "openai",
            "model": "gpt-4o",
        }
    ]
    assert cloud.paths().count("/v1/content-policy") == 1


def test_async_failures_fail_closed_and_a_held_policy_is_kept(
    _frozen_clock: list[float],
) -> None:
    cloud = Cloud()
    client, provider = _wrapped_async(cloud)

    async def run() -> None:
        await client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
        cloud.policy_status = 503
        _frozen_clock[0] += 600
        await client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
        )
        _frozen_clock[0] = NOW + 890
        with pytest.raises(ConsentUnreachableError):
            await client.chat.completions.create(
                model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
            )

    asyncio.run(run())
    assert len(provider.calls) == 2


@pytest.mark.parametrize(
    "route", ["/v1/content-policy", "/.well-known/jwks.json", "/v1/content-events"]
)
def test_async_network_failures(route: str) -> None:
    cloud = Cloud()
    original = cloud.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == route:
            raise httpx.ConnectError("down")
        return original(request)

    cloud.handle = handle  # type: ignore[method-assign]
    client, _ = _wrapped_async(cloud)

    async def run() -> None:
        expected = (
            ContentBlockedError
            if route == "/v1/content-events"
            else ConsentUnreachableError
        )
        with pytest.raises(expected):
            await client.chat.completions.create(
                model="gpt-4o",
                messages=[{"content": "4111111111111111"}],
                user_id="usr_marie_456",
            )

    asyncio.run(run())


class _Stream:
    def __init__(self, sink: list[dict[str, Any]], kwargs: dict[str, Any]) -> None:
        sink.append(kwargs)

    def __enter__(self) -> str:
        return "stream"

    def __exit__(self, *_: Any) -> None:
        return None

    async def __aenter__(self) -> str:
        return "stream"

    async def __aexit__(self, *_: Any) -> None:
        return None


class _Streaming(_Recorder):
    def stream(self, **kwargs: Any) -> _Stream:
        return _Stream(self.calls, kwargs)


@pytest.mark.parametrize("is_async", [False, True])
def test_a_stream_is_cleaned_when_it_is_entered(is_async: bool) -> None:
    cloud = Cloud()
    recorder = _Streaming()
    completions = recorder
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    transport: Any = httpx.MockTransport(
        cloud.handle_async if is_async else cloud.handle
    )
    consent_client: Any = (AsyncHttpConsentClient if is_async else HttpConsentClient)(
        "https://cloud.example.test", transport=transport
    )
    if is_async:

        async def create(**kwargs: Any) -> Any:
            return kwargs

        recorder.create = create  # type: ignore[method-assign]
    wrapped = consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=consent_client
    )
    manager = wrapped.chat.completions.stream(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert cloud.paths() == []  # nothing asked until it is entered
    blocked = wrapped.chat.completions.stream(
        model="gpt-4o",
        messages=[{"content": "4111111111111111"}],
        user_id="usr_marie_456",
    )
    if is_async:

        async def run() -> None:
            async with manager as stream:
                assert stream == "stream"
            with pytest.raises(ContentBlockedError):
                async with blocked:
                    pass

        asyncio.run(run())
    else:
        with manager as stream:
            assert stream == "stream"
        with pytest.raises(ContentBlockedError), blocked:
            pass
    assert recorder.calls[0]["messages"] == CLEANED
    assert len(recorder.calls) == 1


# ── one fetch in flight per client ──────────────────────────────────────────
#
# A fresh process under load: every call in flight needs the policy at once.
# The stand-in cloud holds its policy answer until every caller is inside
# `content_policy()`, so a client that fetched once per caller would be seen
# fetching once per caller — without the hold, the first answer could land
# before the others ask, and the test would pass whatever the client does.

N_CALLS = 200
_HOLD_SECONDS = 10.0


def _policy_gets(cloud: Cloud) -> int:
    return cloud.paths().count("/v1/content-policy")


def _jwks_gets(cloud: Cloud) -> int:
    return cloud.paths().count("/.well-known/jwks.json")


def _create(client: Any) -> Any:
    return client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )


def _threads(cloud: Cloud, callers: int = N_CALLS) -> SimpleNamespace:
    """``callers`` calls at once, each on its own thread, on a fresh client."""
    entered = [0]
    lock = threading.Lock()
    all_in = threading.Event()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/content-policy":
            assert all_in.wait(_HOLD_SECONDS), f"only {entered[0]} callers entered"
            time.sleep(0.05)  # the last ones in reach their wait
        return cloud.handle(request)

    consent_client = HttpConsentClient(
        "https://cloud.example.test",
        api_key="sk_test_cleaning",  # pragma: allowlist secret
        transport=httpx.MockTransport(handle),
    )
    fetch = consent_client.content_policy

    def counted() -> _content_policy.PolicyState:
        with lock:
            entered[0] += 1
            if entered[0] == callers:
                all_in.set()
        return fetch()

    consent_client.content_policy = counted  # type: ignore[method-assign]
    recorder = _Recorder()
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=recorder)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=consent_client,
    )
    with ThreadPoolExecutor(max_workers=callers) as pool:
        futures = [pool.submit(_create, client) for _ in range(callers)]
    errors = [f.exception() for f in futures if f.exception() is not None]
    return SimpleNamespace(client=client, errors=errors, provider=recorder)


async def _gathered(
    cloud: Cloud, callers: int = N_CALLS, *, cancel_first: bool = False
) -> SimpleNamespace:
    """``callers`` calls at once with ``asyncio.gather``, on a fresh client."""
    entered = [0]
    all_in = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/content-policy":
            await asyncio.wait_for(all_in.wait(), _HOLD_SECONDS)
            if cancel_first:
                tasks[0].cancel()  # the caller that started the fetch
            for _ in range(10):
                await asyncio.sleep(0)
        return cloud.handle(request)

    consent_client = AsyncHttpConsentClient(
        "https://cloud.example.test",
        api_key="sk_test_cleaning",  # pragma: allowlist secret
        transport=httpx.MockTransport(handle),
    )
    fetch = consent_client.content_policy

    async def counted() -> _content_policy.PolicyState:
        entered[0] += 1
        if entered[0] == callers:
            all_in.set()
        return await fetch()

    consent_client.content_policy = counted  # type: ignore[method-assign]
    recorder = _AsyncRecorder()
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=recorder)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=consent_client,
    )
    tasks.extend(asyncio.create_task(_create(client)) for _ in range(callers))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    return SimpleNamespace(client=client, errors=errors, provider=recorder)


@pytest.mark.parametrize("cleaning", [True, False])
def test_simultaneous_calls_on_a_fresh_client_share_one_fetch_in_threads(
    cleaning: bool,
) -> None:
    cloud = Cloud() if cleaning else Cloud(policy=None)
    ran = _threads(cloud)
    assert ran.errors == []
    assert _policy_gets(cloud) == 1, f"{_policy_gets(cloud)} policy GETs"
    assert _jwks_gets(cloud) == (1 if cleaning else 0), f"{_jwks_gets(cloud)} JWKS"
    assert len(ran.provider.calls) == N_CALLS
    expected = CLEANED if cleaning else MESSAGES
    assert all(call["messages"] == expected for call in ran.provider.calls)


@pytest.mark.parametrize("cleaning", [True, False])
def test_simultaneous_calls_on_a_fresh_client_share_one_fetch_with_asyncio(
    cleaning: bool,
) -> None:
    cloud = Cloud() if cleaning else Cloud(policy=None)
    ran = asyncio.run(_gathered(cloud))
    assert ran.errors == []
    assert _policy_gets(cloud) == 1, f"{_policy_gets(cloud)} policy GETs"
    assert _jwks_gets(cloud) == (1 if cleaning else 0), f"{_jwks_gets(cloud)} JWKS"
    assert len(ran.provider.calls) == N_CALLS
    expected = CLEANED if cleaning else MESSAGES
    assert all(call["messages"] == expected for call in ran.provider.calls)


def test_a_failed_shared_fetch_reaches_every_waiter_then_is_tried_again() -> None:
    cloud = Cloud(policy_status=503)
    ran = _threads(cloud)
    assert len(ran.errors) == N_CALLS
    assert all(isinstance(e, ConsentUnreachableError) for e in ran.errors)
    assert _policy_gets(cloud) == 1
    # A failure is never kept: the next call asks again, and recovers.
    cloud.policy_status = 200
    _create(ran.client)
    assert _policy_gets(cloud) == 2
    assert ran.provider.calls[0]["messages"] == CLEANED


def test_a_failed_shared_fetch_reaches_every_waiter_then_is_tried_again_async() -> None:
    cloud = Cloud(policy_status=503)

    async def run() -> SimpleNamespace:
        ran = await _gathered(cloud)
        assert len(ran.errors) == N_CALLS
        assert all(isinstance(e, ConsentUnreachableError) for e in ran.errors)
        assert _policy_gets(cloud) == 1
        cloud.policy_status = 200
        await _create(ran.client)
        return ran

    ran = asyncio.run(run())
    assert _policy_gets(cloud) == 2
    assert ran.provider.calls[0]["messages"] == CLEANED


def test_the_caller_that_started_the_fetch_may_be_cancelled_alone() -> None:
    # One request cancelled (a client hung up) must not cancel the fetch the
    # 199 others are waiting on.
    cloud = Cloud()
    ran = asyncio.run(_gathered(cloud, cancel_first=True))
    assert len(ran.errors) == 1
    assert isinstance(ran.errors[0], asyncio.CancelledError)
    assert _policy_gets(cloud) == 1
    assert len(ran.provider.calls) == N_CALLS - 1


def test_a_fetch_in_flight_on_another_event_loop_is_never_awaited() -> None:
    # One client used from two event loops (two threads, or `asyncio.run`
    # twice): a task of one loop cannot be awaited from the other, so each
    # loop fetches for itself.
    cloud = Cloud()
    first_in = threading.Event()
    release = threading.Event()
    held_once: list[bool] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/content-policy" and not held_once:
            held_once.append(True)
            first_in.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
        return cloud.handle(request)

    consent_client = AsyncHttpConsentClient(
        "https://cloud.example.test", transport=httpx.MockTransport(handle)
    )
    states: dict[str, Any] = {}

    def other_loop() -> None:
        states["other"] = asyncio.run(consent_client.content_policy())

    thread = threading.Thread(target=other_loop)
    thread.start()
    try:
        assert first_in.wait(_HOLD_SECONDS)
        states["here"] = asyncio.run(
            asyncio.wait_for(consent_client.content_policy(), _HOLD_SECONDS)
        )
    finally:
        release.set()
        thread.join(_HOLD_SECONDS)
    assert states["here"].enabled
    assert states["other"].enabled
    assert _policy_gets(cloud) == 2


# ── a 429 on the policy route ───────────────────────────────────────────────


@pytest.fixture
def _slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """What the SDK waited, in seconds, instead of waiting it."""
    slept: list[float] = []

    async def async_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(_content_policy, "sleep", slept.append)
    monkeypatch.setattr(_content_policy, "async_sleep", async_sleep)
    return slept


def _rate_limited(cloud: Cloud, *headers: dict[str, str] | None) -> None:
    """The next ``len(headers)`` policy fetches answer 429, with these headers."""
    queue = list(headers)
    original = cloud.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/content-policy" and queue:
            cloud.requests.append(request)
            return httpx.Response(429, headers=queue.pop(0), json={"title": "Slow"})
        return original(request)

    cloud.handle = handle  # type: ignore[method-assign]


def _runner(cloud: Cloud, is_async: bool) -> tuple[Any, _Recorder]:
    client, provider = _wrapped_async(cloud) if is_async else _wrapped(cloud)

    def run() -> None:
        async def call() -> Any:
            return await _create(client)

        if is_async:
            asyncio.run(call())
        else:
            _create(client)

    return run, provider


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize(
    ("header", "waited"),
    [("1", 1.0), ("0", 0.0), ("2", 2.0), ("60", 2.0), (" 1\t", 1.0), ("9" * 400, 2.0)],
    ids=["1", "0", "2", "60", "spaced", "huge"],
)
def test_a_429_with_no_policy_held_waits_its_retry_after_then_asks_once_more(
    header: str, waited: float, is_async: bool, _slept: list[float]
) -> None:
    cloud = Cloud()
    _rate_limited(cloud, {"Retry-After": header})
    run, provider = _runner(cloud, is_async)
    run()
    assert _slept == [waited]  # delta-seconds, at most two
    assert _policy_gets(cloud) == 2
    assert provider.calls[0]["messages"] == CLEANED


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "1.5",
        "-1",
        "+1",
        "1_0",
        "1 2",
        "0x10",
        "1e1",
        "Wed, 21 Oct 2026 07:28 GMT",
    ],
)
def test_a_429_without_a_usable_retry_after_fails_closed_at_once(
    header: str | None, is_async: bool, _slept: list[float]
) -> None:
    cloud = Cloud()
    _rate_limited(cloud, {} if header is None else {"Retry-After": header})
    run, provider = _runner(cloud, is_async)
    with pytest.raises(RateLimitedError):
        run()
    assert _slept == []
    assert _policy_gets(cloud) == 1
    assert provider.calls == []


@pytest.mark.parametrize("is_async", [False, True])
def test_a_429_is_retried_once_only(is_async: bool, _slept: list[float]) -> None:
    cloud = Cloud()
    _rate_limited(cloud, {"Retry-After": "1"}, {"Retry-After": "1"})
    run, provider = _runner(cloud, is_async)
    with pytest.raises(RateLimitedError):
        run()
    assert _slept == [1.0]
    assert _policy_gets(cloud) == 2
    assert provider.calls == []


@pytest.mark.parametrize("is_async", [False, True])
def test_a_429_while_a_valid_policy_is_held_waits_for_nothing(
    is_async: bool, _slept: list[float], _frozen_clock: list[float]
) -> None:
    cloud = Cloud()
    run, provider = _runner(cloud, is_async)
    run()
    _rate_limited(cloud, {"Retry-After": "1"})
    _frozen_clock[0] += 300  # refresh due; the policy held is still valid
    run()
    assert _slept == []
    assert _policy_gets(cloud) == 2
    assert provider.calls[-1]["messages"] == CLEANED


def test_a_429_is_retried_once_per_fetch_not_once_per_caller(
    _slept: list[float],
) -> None:
    cloud = Cloud()
    _rate_limited(cloud, {"Retry-After": "1"})
    ran = _threads(cloud)
    assert ran.errors == []
    assert _slept == [1.0]
    assert _policy_gets(cloud) == 2


def test_a_429_is_retried_once_per_fetch_not_once_per_caller_async(
    _slept: list[float],
) -> None:
    cloud = Cloud()
    _rate_limited(cloud, {"Retry-After": "1"})
    ran = asyncio.run(_gathered(cloud))
    assert ran.errors == []
    assert _slept == [1.0]
    assert _policy_gets(cloud) == 2


def test_the_default_clocks_and_pauses_are_the_real_ones() -> None:
    first = _REAL_MONOTONIC()
    assert abs(first - time.monotonic()) < 5
    assert _REAL_MONOTONIC() >= first
    _REAL_SLEEP(0)
    asyncio.run(_REAL_ASYNC_SLEEP(0))


# ── the pure-Python ES256 check ─────────────────────────────────────────────


def _vectors() -> dict[str, Any]:
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "descriptors"
        / "testdata"
        / "act-verification-vectors.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_p256_agrees_with_the_shared_act_vectors() -> None:
    from idside import verify

    vectors = _vectors()
    jwk = vectors["jwks"]["keys"][0]
    x = verify._coordinate(jwk, "x", jwk["kid"])
    y = verify._coordinate(jwk, "y", jwk["kid"])
    checked = 0
    for case in vectors["cases"]:
        token = case["token"]
        segments = token.split(".")
        if len(segments) != 3:
            continue
        try:
            header = json.loads(verify._b64url_decode(segments[0], what="h"))
            signature = verify._b64url_decode(segments[2], what="s")
        except ActVerificationError:
            continue  # a malformed case: there is no signature to check
        if header.get("kid") != jwk["kid"] or case["expect"] in (
            "ActMalformedError",  # not a signature over what was signed
            "ActAlgorithmError",
            "ActKeyNotFoundError",
            "ActPrivateKeyInKeySetError",
        ):
            continue
        # Every other case is signed by the key; only these two do not verify.
        expected = case["expect"] != "ActSignatureError"
        assert (
            _p256.verify(f"{segments[0]}.{segments[1]}".encode(), signature, x, y)
            is expected
        ), case["name"]
        checked += 1
    assert checked >= 10


def _crypto_backend() -> Any:
    from idside import verify

    return verify._load_es256_backend()


def _es256_cases() -> list[tuple[str, bytes, bytes, int, int, bool]]:
    """Generated positive and negative ES256 cases.

    Each is (name, message, signature, x, y, valid); a fresh key each run, and
    every case is put to both verifiers below.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    x, y = numbers.x, numbers.y
    message = b"header-segment.claims-segment"
    r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    n = _p256.N

    def raw(r: int, s: int) -> bytes:
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    good = raw(r, s)
    r384, s384 = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA384())))
    other = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    return [
        ("valid", message, good, x, y, True),
        # ECDSA verification accepts both s and n - s; the cloud's signer does
        # not normalise s, so neither may this.
        ("high s", message, raw(r, n - s), x, y, True),
        ("another message", message + b"!", good, x, y, False),
        ("signed with SHA-384", message, raw(r384, s384), x, y, False),
        ("another key", message, good, other.x, other.y, False),
        ("r zero", message, raw(0, s), x, y, False),
        ("s zero", message, raw(r, 0), x, y, False),
        ("r = n", message, raw(n, s), x, y, False),
        ("s = n", message, raw(r, n), x, y, False),
        (
            "r past n",
            message,
            (2**256 - 1).to_bytes(32, "big") + good[32:],
            x,
            y,
            False,
        ),
        (
            "s past n",
            message,
            good[:32] + (2**256 - 1).to_bytes(32, "big"),
            x,
            y,
            False,
        ),
        ("r flipped", message, raw(r ^ 1, s), x, y, False),
        ("truncated", message, good[:63], x, y, False),
        ("over-long", message, good + b"\x00", x, y, False),
        ("over-long, leading zero", message, b"\x00" + good, x, y, False),
        # r, then s written on 33 bytes: the same numbers, read naively.
        ("over-long, s padded", message, good[:32] + b"\x00" + good[32:], x, y, False),
        (
            "DER, not raw",
            message,
            key.sign(message, ec.ECDSA(hashes.SHA256())),
            x,
            y,
            False,
        ),
        ("empty", message, b"", x, y, False),
        ("key off the curve", message, good, x, (y + 1) % _p256.P, False),
        ("key at infinity (0, 0)", message, good, 0, 0, False),
        ("key coordinate past p", message, good, x + _p256.P, y, False),
        ("key with y = 0", message, good, x, 0, False),
    ]


@pytest.mark.parametrize("backend", ["cryptography", "pure python"])
def test_es256_cases_are_judged_alike_by_both_verifiers(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from idside import verify

    with_cryptography = _crypto_backend()
    if backend == "pure python":

        def absent() -> Any:
            raise ImportError("no cryptography")

        monkeypatch.setattr(verify, "_load_es256_backend", absent)
    for name, message, signature, x, y, valid in _es256_cases():
        assert _content_policy._signature_holds(message, signature, x, y) is valid, name
        if len(signature) == 64 and _p256.on_curve(x, y):
            # The two backends themselves agree wherever both can be asked.
            assert _p256.verify(message, signature, x, y) is valid, name
            assert with_cryptography(message, signature, x, y) is valid, name


def test_cryptography_verifies_the_policy_when_it_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    used: list[str] = []
    real = _p256.verify
    monkeypatch.setattr(
        _p256, "verify", lambda *args: used.append("p256") or real(*args)
    )
    cloud = Cloud()
    client, provider = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert provider.calls[0]["messages"] == CLEANED
    assert used == []


def test_without_cryptography_the_policy_is_verified_in_pure_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from idside import verify

    def absent() -> Any:
        raise ImportError("no cryptography")

    used: list[str] = []
    real = _p256.verify
    monkeypatch.setattr(verify, "_load_es256_backend", absent)
    monkeypatch.setattr(
        _p256, "verify", lambda *args: used.append("p256") or real(*args)
    )
    cloud = Cloud()
    client, provider = _wrapped(cloud)
    client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, user_id="usr_marie_456"
    )
    assert provider.calls[0]["messages"] == CLEANED
    assert used == ["p256"]
    # ...and refuses a forgery all the same.
    key = SigningKey()
    _refused(
        Cloud(
            key=key,
            answer=_answer_with(jws(key, claims_for(policy_document()), tamper=True)),
        ),
        "signature",
    )


def test_a_key_set_key_off_the_curve_is_refused() -> None:
    key = SigningKey()
    cloud = Cloud(key=key)
    original = cloud.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jwks.json":
            cloud.requests.append(request)
            jwk = key.jwk()
            y = int.from_bytes(verify_b64(jwk["y"]), "big")
            jwk["y"] = b64url(((y + 1) % _p256.P).to_bytes(32, "big"))
            return httpx.Response(200, json={"keys": [jwk]})
        return original(request)

    cloud.handle = handle  # type: ignore[method-assign]
    _refused(cloud, "not a point of P-256")


def verify_b64(segment: str) -> bytes:
    from idside import verify

    return verify._b64url_decode(segment, what="test")


def test_p256_agrees_with_cryptography_on_random_keys() -> None:
    for _ in range(40):
        key = ec.generate_private_key(ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        message = secrets.token_bytes(secrets.randbelow(200))
        r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        assert _p256.verify(message, signature, numbers.x, numbers.y)
        assert not _p256.verify(message + b"!", signature, numbers.x, numbers.y)
        flipped = (s ^ 1).to_bytes(32, "big")
        assert not _p256.verify(message, signature[:32] + flipped, numbers.x, numbers.y)


def test_p256_refuses_what_is_not_a_signature_or_a_key() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    r, s = decode_dss_signature(key.sign(b"m", ec.ECDSA(hashes.SHA256())))
    good = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    assert _p256.verify(b"m", good, numbers.x, numbers.y)
    assert not _p256.verify(b"m", good[:63], numbers.x, numbers.y)
    zero = bytes(32)
    order = _p256.N.to_bytes(32, "big")
    assert not _p256.verify(b"m", zero + good[32:], numbers.x, numbers.y)
    assert not _p256.verify(b"m", good[:32] + zero, numbers.x, numbers.y)
    assert not _p256.verify(b"m", order + good[32:], numbers.x, numbers.y)
    assert not _p256.verify(b"m", good[:32] + order, numbers.x, numbers.y)
    assert not _p256.verify(b"m", good, numbers.x, (numbers.y + 1) % _p256.P)
    assert not _p256.verify(b"m", good, _p256.P, numbers.y)
    assert _p256.on_curve(*_p256.G)


def test_p256_point_arithmetic_edges() -> None:
    gx, gy = _p256.G
    g = (gx, gy, 1)
    assert _p256._add(_p256._INFINITY, g) == g
    assert _p256._add(g, _p256._INFINITY) == g
    assert _p256._double(_p256._INFINITY)[2] == 0
    assert _p256._add(g, (gx, (-gy) % _p256.P, 1))[2] == 0  # G + (-G)
    doubled = _p256._double(g)
    assert _p256._add(g, g) == doubled
    # n·G is the point at infinity: a signature leading there is refused.
    assert _p256._multiply_add(_p256.N, _p256.G, 0, _p256.G)[2] == 0
    assert _p256._double((gx, 0, 1))[2] == 0


def test_p256_refuses_a_signature_that_lands_on_infinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_p256, "_multiply_add", lambda *_: _p256._INFINITY)
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    r, s = decode_dss_signature(key.sign(b"m", ec.ECDSA(hashes.SHA256())))
    assert not _p256.verify(
        b"m", r.to_bytes(32, "big") + s.to_bytes(32, "big"), numbers.x, numbers.y
    )


# ── the journal on each failure ─────────────────────────────────────────────


def _failing_authorize(cloud: Cloud) -> None:
    original = cloud.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/authorize":
            cloud.requests.append(request)
            return httpx.Response(503)
        return original(request)

    cloud.handle = handle  # type: ignore[method-assign]


@pytest.mark.parametrize("shape", ["blocking", "async", "stream", "async stream"])
def test_a_policy_that_cannot_be_fetched_is_journaled_by_class(
    shape: str, caplog: pytest.LogCaptureFixture
) -> None:
    cloud = Cloud(policy_status=503)
    _check_journal(
        cloud, shape, caplog, "content_policy_failed", ConsentUnreachableError
    )


@pytest.mark.parametrize("shape", ["blocking", "async", "stream", "async stream"])
def test_an_authorize_failure_after_a_good_policy_is_journaled(
    shape: str, caplog: pytest.LogCaptureFixture
) -> None:
    cloud = Cloud()
    _failing_authorize(cloud)
    _check_journal(cloud, shape, caplog, "authorize_failed", ConsentUnreachableError)


def _check_journal(
    cloud: Cloud,
    shape: str,
    caplog: pytest.LogCaptureFixture,
    event: str,
    error: type[Exception],
) -> None:
    is_async = "async" in shape
    recorder = _Streaming()
    client = SimpleNamespace(chat=SimpleNamespace(completions=recorder))
    transport: Any = httpx.MockTransport(
        cloud.handle_async if is_async else cloud.handle
    )
    consent_client: Any = (AsyncHttpConsentClient if is_async else HttpConsentClient)(
        "https://cloud.example.test", transport=transport
    )
    if is_async:

        async def create(**kwargs: Any) -> Any:
            return kwargs

        recorder.create = create  # type: ignore[method-assign]
    wrapped = consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=consent_client
    )
    method = (
        wrapped.chat.completions.stream
        if "stream" in shape
        else wrapped.chat.completions.create
    )
    kwargs = {"model": "gpt-4o", "messages": MESSAGES, "user_id": "usr_marie_456"}

    async def run_async() -> None:
        if "stream" in shape:
            async with method(**kwargs):
                pass
        else:
            await method(**kwargs)

    with caplog.at_level(logging.DEBUG, logger="idside"), pytest.raises(error):
        if is_async:
            asyncio.run(run_async())
        elif "stream" in shape:
            with method(**kwargs):
                pass
        else:
            method(**kwargs)
    lines = [record.getMessage() for record in caplog.records]
    assert any(
        line.startswith(f"{event} call_site=chat.completions.create")
        and f"error={error.__name__}" in line
        for line in lines
    ), lines
    assert recorder.calls == []


def test_a_blocked_error_lists_its_rules_sorted_with_their_counts() -> None:
    error = ContentBlockedError({"email": 2, "attachments": 1, "custom:CODENAME": 3})
    assert list(error.blocked) == ["attachments", "custom:CODENAME", "email"]
    assert "found attachments (1), custom:CODENAME (3), email (2)." in str(error)
