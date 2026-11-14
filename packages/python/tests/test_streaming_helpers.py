"""The streaming helpers: ``chat.completions.stream()`` and ``responses.stream()``.

A streaming call is an inference call. It carries the same prompt, creates the
same retention and needs the same proof — but it is reached through a *helper*
that returns a context manager and defers the real request to ``__enter__``.

In Python those helpers take a fixed keyword signature, so the identity kwarg
this SDK reads (``user_id`` / ``ubt``) is a ``TypeError`` unless the helper is
itself a call site. Without one, the only two outcomes were a ``TypeError`` at
the call, or — for the developer who dropped the identity to make the
``TypeError`` go away, when an identity was still required — a
``MissingIdentityError`` from the deferred ``create``, raised at ``__enter__``
rather than at the call that caused it. Fail-closed, and unusable. These tests
pin the third outcome: the helper is enforced.

The Node SDK reaches the same place by a different route (its helpers forward
one body object, identity included, to the patched ``create``), which is why
``stream`` is declared as an alias here and skipped there — see the descriptor
schema's note on aliases whose interception would change a return type.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from idside import consent
from idside._transport import ConsentDecision
from idside.adapters import ProviderAdapter
from idside.consent import _INTERNAL_CALL
from idside.exceptions import (
    ConsentUnreachableError,
    MissingIdentityError,
    UnenforcedCallError,
)
from idside.providers import load_descriptors

openai = pytest.importorskip("openai")

_OPENAI = load_descriptors()["openai"]
_TOKEN_HEADER = _OPENAI.token_header

_DECISION = ConsentDecision(
    consent_state={"retention.conversation": False},
    params_forced={"store": False},
    act_token="tok_1",
    source="widget",
    legal_basis="consent",
    binding="user_id",
)

_MESSAGES = [{"role": "user", "content": "hello"}]


class _FakeConsent:
    """A consent client that counts authorizations and never touches a network."""

    def __init__(self, decision: ConsentDecision = _DECISION) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        return self.decision


class _Wire:
    """What actually reached the HTTP transport: method, path, token present."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bool]] = []


def _closed_port() -> int:
    """A port nothing listens on, so a real connect fails immediately."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return port


def _probe(
    descriptor: Any = _OPENAI, consent_client: Any = None
) -> tuple[Any, Any, _Wire]:
    """A REAL OpenAI client, wired to a closed port, recorded at the transport."""
    client = openai.OpenAI(
        api_key="sk-test",  # pragma: allowlist secret
        base_url=f"http://127.0.0.1:{_closed_port()}/v1",
        max_retries=0,
    )
    wire = _Wire()
    transport = client._client  # a test may reach in here; the SDK finds it by BFS
    original_send = transport.send

    def _record(request: Any, *args: Any, **kwargs: Any) -> Any:
        wire.requests.append(
            (request.method, request.url.path, _TOKEN_HEADER in request.headers)
        )
        return original_send(request, *args, **kwargs)

    transport.send = _record
    fake = _FakeConsent() if consent_client is None else consent_client
    wrapped = consent.wrap(
        client, adapter=ProviderAdapter(descriptor), consent_client=fake
    )
    return wrapped, fake, wire


# ── the descriptor declares them ─────────────────────────────────────────────


def test_stream_is_a_declared_alias_of_both_openai_call_sites() -> None:
    """Both entrypoints have one, and both must be enforced under the site's id.

    ``chat.completions.stream`` and ``responses.stream`` post to the very
    endpoints ``create`` posts to, so they are the same call site as far as the
    cloud is concerned and add no new wire id.
    """
    resolved = ProviderAdapter(_OPENAI).get_call_sites(
        openai.OpenAI(api_key="sk-test")  # pragma: allowlist secret
    )
    pairs = {(s.site.id, s.method) for s in resolved}
    assert ("chat.completions.create", "stream") in pairs
    assert ("responses.create", "stream") in pairs


# ── the real client, recorded at the transport ───────────────────────────────


def test_chat_completions_stream_is_enforced_and_the_token_reaches_the_wire() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(openai.APIConnectionError):  # noqa: SIM117 — two distinct blocks
        with wrapped.chat.completions.stream(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        ):
            pass  # __enter__ is where the request is made, and where it fails

    assert len(fake.calls) == 1
    assert fake.calls[0]["call_site"] == "chat.completions.create"
    assert wire.requests == [("POST", "/v1/chat/completions", True)]


def test_responses_stream_is_enforced_and_the_token_reaches_the_wire() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(openai.APIConnectionError):  # noqa: SIM117 — two distinct blocks
        with wrapped.responses.stream(
            model="gpt-5", input="hello", user_id="usr_marie_456"
        ):
            pass

    assert len(fake.calls) == 1
    assert fake.calls[0]["call_site"] == "responses.create"
    assert wire.requests == [("POST", "/v1/responses", True)]


def test_a_stream_with_an_empty_identity_never_reaches_the_wire() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(MissingIdentityError):
        wrapped.chat.completions.stream(model="gpt-5", messages=_MESSAGES, user_id="")

    assert fake.calls == []
    assert wire.requests == []


def test_the_stream_helper_authorizes_once_not_twice() -> None:
    """The helper defers to ``create``, which this SDK also patched.

    One ACT token per request: the second pass must recognise the token already
    on the call and let it through untouched.
    """
    wrapped, fake, _wire = _probe()

    with pytest.raises(openai.APIConnectionError):  # noqa: SIM117 — two distinct blocks
        with wrapped.chat.completions.stream(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        ):
            pass

    assert len(fake.calls) == 1


# ── what the forced parameters do to a streaming call ────────────────────────


class _FakeStreamManager:
    """What every provider's ``stream()`` hands back: a manager, not a result.

    The shape is the point. The provider's own helper builds one of these and
    sends nothing until it is entered, which is exactly why the SDK waits too.
    """

    def __init__(self, entered: list[bool]) -> None:
        self._entered = entered

    def __enter__(self) -> _FakeStreamManager:
        self._entered.append(True)
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


class _RecordingCompletions:
    """An openai-shaped resource recording what each entrypoint received."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered: list[bool] = []

    def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("create", kwargs))
        return {"id": "chatcmpl_fake"}

    def stream(self, **kwargs: Any) -> _FakeStreamManager:
        self.calls.append(("stream", kwargs))
        return _FakeStreamManager(self.entered)


def test_forced_params_and_the_token_land_on_the_stream_call_itself() -> None:
    """A streaming call is refused the same retention as a blocking one.

    And it is refused it at the moment the stream is opened: the helper is not
    called at all until then, so the forced parameters reach the request that is
    actually being made rather than one that might never be.
    """
    completions = _RecordingCompletions()
    fake = _FakeConsent()
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=fake,
    )

    manager = client.chat.completions.stream(
        model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
    )
    assert completions.calls == [], "the helper ran before the stream was opened"

    with manager as opened:
        assert opened is not None

    assert [name for name, _kwargs in completions.calls] == ["stream"]
    assert completions.entered == [True], "the provider's own manager was not entered"
    _name, kwargs = completions.calls[0]
    assert kwargs["store"] is False
    assert kwargs[_OPENAI.headers_kwarg][_TOKEN_HEADER] == "tok_1"
    assert _OPENAI.identity_param not in kwargs


# ── the proof is minted when the stream is entered, and never before ─────────


def test_a_blocking_stream_nobody_enters_is_never_authorized() -> None:
    """A stream nobody enters made no request, so it needed no proof.

    Building the manager sends nothing: the provider's helper defers its request
    to ``__enter__``. Authorizing at the call would put a signed ACT token and a
    ledger entry behind a request that may never happen — a proof of something
    that did not occur, in the one document this product exists to produce.

    The second half of the test is what makes the first half mean anything: the
    nominal path must still mint exactly one proof, or "nothing was authorized"
    would merely be saying that nothing at all happened.
    """
    wrapped, fake, wire = _probe()

    manager = wrapped.chat.completions.stream(
        model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
    )

    assert fake.calls == [], "a stream that was never entered was authorized"
    assert wire.requests == []

    with pytest.raises(openai.APIConnectionError), manager:
        pass

    assert len(fake.calls) == 1
    assert wire.requests == [("POST", "/v1/chat/completions", True)]


def test_a_blocking_responses_stream_nobody_enters_is_never_authorized() -> None:
    """The same, on the other entrypoint: both helpers defer, so both wait."""
    wrapped, fake, wire = _probe()

    manager = wrapped.responses.stream(
        model="gpt-5", input="hello", user_id="usr_marie_456"
    )

    assert fake.calls == []
    assert wire.requests == []

    with pytest.raises(openai.APIConnectionError), manager:
        pass

    assert len(fake.calls) == 1
    assert wire.requests == [("POST", "/v1/responses", True)]


def test_the_two_client_shapes_count_the_same_streams() -> None:
    """One customer, two applications, and the ledger must agree with itself.

    The async path has always waited for ``__aenter__``. A blocking path that
    authorized at the call would make the same ten abandoned streams cost ten
    ledger entries in one application and none in the other — a gap a customer
    comparing their two deployments would find, and that no explanation covers.
    """
    blocking, blocking_cloud, _wire = _probe()
    async_client = openai.AsyncOpenAI(
        api_key="sk-test",  # pragma: allowlist secret
        base_url=f"http://127.0.0.1:{_closed_port()}/v1",
        max_retries=0,
    )
    async_cloud = _FakeConsent()
    wrapped_async = consent.wrap(
        async_client, adapter=ProviderAdapter(_OPENAI), consent_client=async_cloud
    )

    blocking.chat.completions.stream(
        model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
    )
    wrapped_async.chat.completions.stream(
        model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
    )

    assert len(blocking_cloud.calls) == len(async_cloud.calls) == 0


# ── a helper reached from inside an authorized call ──────────────────────────


class _ParseOnTopOfStream(_RecordingCompletions):
    """A resource whose ``parse`` is implemented on top of its own ``stream``.

    Providers build one entrypoint on another all the time, and this SDK patched
    both: `parse` and `stream` are declared aliases of the same call site.
    Nothing in the public request proves that the inner hop came from the outer
    one — the ACT header is a header, and a caller can put one there.
    """

    def parse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("parse", kwargs))
        with self.stream(**kwargs) as opened:
            return {"id": "chatcmpl_fake", "opened": opened is not None}


def test_a_stream_helper_reached_from_inside_an_authorized_call_is_not_re_asked() -> (
    None
):
    """One AI call is one authorization, whichever entrypoint it went in by.

    The inner `stream()` is the same call site as the outer `parse()` and is
    carrying the token the cloud just issued for it. Asking again would mint a
    second ACT token and a second ledger entry for one request — and, because
    the wrapper consumes the identity on the way in, the second ask would name
    nobody, which a call in progress may not do: it would be refused, rather
    than proven about the API key instead of the person the call named.
    """
    completions = _ParseOnTopOfStream()
    fake = _FakeConsent()
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=fake,
    )

    result = client.chat.completions.parse(
        model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
    )

    assert result["opened"] is True
    assert len(fake.calls) == 1, "the nested helper was authorized a second time"
    assert [name for name, _kwargs in completions.calls] == ["parse", "stream"]
    assert completions.entered == [True], "the provider's own manager was not entered"


class _ParseOnTopOfAHeaderlessStream(_RecordingCompletions):
    """The same, with a ``parse`` that drops the headers it was given."""

    def parse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("parse", kwargs))
        kwargs.pop(_OPENAI.headers_kwarg, None)
        with self.stream(**kwargs) as opened:
            return {"id": "chatcmpl_fake", "opened": opened is not None}


def test_a_stream_helper_reached_without_the_permit_names_nobody_and_is_refused() -> (
    None
):
    """The deferred wrapper refuses the nested hop at the call, like the others.

    Without the token on it, the inner `stream()` is a call of its own, made
    from inside one in progress and naming nobody: refused before its manager
    is built, and long before the cloud would have been asked at entry.
    """
    completions = _ParseOnTopOfAHeaderlessStream()
    fake = _FakeConsent()
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=fake,
    )

    with pytest.raises(MissingIdentityError, match="inside another"):
        client.chat.completions.parse(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        )

    assert len(fake.calls) == 1, "the nested helper was authorized about the key"
    assert [name for name, _kwargs in completions.calls] == ["parse"]
    assert completions.entered == []


# ── the developer's own code, between two chunks ─────────────────────────────
#
# An agent reads a stream and, between two chunks, makes calls of its own — often
# naming nobody, on the same client or another one. The permit that lets a
# provider helper's nested hop through is in force only while the provider does
# its own work: around each step of a lazily produced stream, never across the
# loop body that consumes it. So those calls are top-level calls, and a call
# naming nobody is allowed there.

_CHAT_OK = {
    "id": "chatcmpl_1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-5",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "ok"},
        }
    ],
}
_CHAT_SSE = (
    b'data: {"id":"c","object":"chat.completion.chunk","created":0,'
    b'"model":"gpt-5","choices":[{"index":0,"delta":{"content":"ok"},'
    b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
)


def _answer(request: Any) -> Any:
    """A stream when one was asked for, a completion otherwise."""
    if json.loads(request.content).get("stream"):
        return httpx.Response(
            200, content=_CHAT_SSE, headers={"content-type": "text/event-stream"}
        )
    return httpx.Response(200, json=_CHAT_OK)


def _real(fake: _FakeConsent, *, asynchronous: bool = False) -> Any:
    """A REAL openai client that answers every call, wrapped."""
    if asynchronous:
        client: Any = openai.AsyncOpenAI(
            api_key="sk-test",  # pragma: allowlist secret
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_answer)),
        )
    else:
        client = openai.OpenAI(
            api_key="sk-test",  # pragma: allowlist secret
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(_answer)),
        )
    return consent.wrap(client, adapter=ProviderAdapter(_OPENAI), consent_client=fake)


def test_calls_naming_nobody_between_two_chunks_of_a_real_stream_are_allowed() -> None:
    fake = _FakeConsent()
    client, other = _real(fake), _real(fake)

    for _chunk in client.chat.completions.create(
        model="gpt-5", messages=_MESSAGES, stream=True, user_id="usr_marie_456"
    ):
        other.chat.completions.create(model="gpt-5", messages=_MESSAGES)
        client.chat.completions.create(model="gpt-5", messages=_MESSAGES)
    with client.chat.completions.stream(
        model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
    ) as stream:
        for _event in stream:
            other.chat.completions.create(model="gpt-5", messages=_MESSAGES)

    named = [call["user_id"] for call in fake.calls if call["user_id"] is not None]
    assert named == ["usr_marie_456", "usr_marie_456"]
    assert len(fake.calls) > 4, "the loop bodies made no call at all"


def test_calls_naming_nobody_between_chunks_of_a_real_async_stream_are_allowed() -> (
    None
):
    fake = _FakeConsent()
    client, other = _real(fake, asynchronous=True), _real(fake, asynchronous=True)

    async def agent() -> None:
        async for _chunk in await client.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, stream=True, user_id="usr_marie_456"
        ):
            await other.chat.completions.create(model="gpt-5", messages=_MESSAGES)
            await client.chat.completions.create(model="gpt-5", messages=_MESSAGES)
        async with client.chat.completions.stream(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        ) as stream:
            async for _event in stream:
                await other.chat.completions.create(model="gpt-5", messages=_MESSAGES)

    asyncio.run(agent())

    named = [call["user_id"] for call in fake.calls if call["user_id"] is not None]
    assert named == ["usr_marie_456", "usr_marie_456"]
    assert len(fake.calls) > 4, "the loop bodies made no call at all"


class _LazyCompletions(_RecordingCompletions):
    """A call site whose stream is produced lazily, as ``google-genai``'s is.

    Its request happens on the first ``next()``, so the permit is re-established
    around each step. ``held`` records whether it was, step by step: a test that
    never ran under the permit would prove nothing about where it ends.
    """

    def __init__(self) -> None:
        super().__init__()
        self.held: list[bool] = []

    def _chunks(self) -> Any:
        for chunk in ("a", "b"):
            self.held.append(_INTERNAL_CALL.get() is not None)
            yield chunk

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(("create", kwargs))
        return self._chunks()

    def stream(self, **kwargs: Any) -> Any:
        self.calls.append(("stream", kwargs))
        return _LazyManager(self._chunks())


class _LazyManager:
    """A stream manager whose entry hands back a generator still to be driven."""

    def __init__(self, chunks: Any) -> None:
        self._chunks = chunks

    def __enter__(self) -> Any:
        return self._chunks

    def __exit__(self, *_exc: Any) -> None:
        return None


def test_a_lazily_produced_stream_holds_the_permit_only_inside_the_provider() -> None:
    completions = _LazyCompletions()
    fake = _FakeConsent()
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=fake,
    )
    other = _RecordingCompletions()
    wrapped_other = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=other)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=fake,
    )
    seen_in_the_loop: list[bool] = []

    for _chunk in client.chat.completions.create(
        model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
    ):
        seen_in_the_loop.append(_INTERNAL_CALL.get() is not None)
        wrapped_other.chat.completions.create(model="gpt-5", messages=_MESSAGES)
    with client.chat.completions.stream(
        model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
    ) as chunks:
        for _chunk in chunks:
            seen_in_the_loop.append(_INTERNAL_CALL.get() is not None)
            wrapped_other.chat.completions.create(model="gpt-5", messages=_MESSAGES)

    assert completions.held == [True] * 4, "the provider's steps ran without a permit"
    assert seen_in_the_loop == [False] * 4
    assert [call["user_id"] for call in fake.calls] == [
        "usr_marie_456",
        None,
        None,
        "usr_marie_456",
        None,
        None,
    ]
    assert len(other.calls) == 4


class _AsyncLazyCompletions:
    """The async half: an awaited call site and a helper, both lazily produced."""

    def __init__(self) -> None:
        self.held: list[bool] = []

    async def _chunks(self) -> Any:
        for chunk in ("a", "b"):
            await asyncio.sleep(0)
            self.held.append(_INTERNAL_CALL.get() is not None)
            yield chunk

    async def create(self, **_kwargs: Any) -> Any:
        return self._chunks()

    def stream(self, **_kwargs: Any) -> Any:
        return _AsyncLazyManager(self._chunks())


class _AsyncLazyManager:
    def __init__(self, chunks: Any) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> Any:
        return self._chunks

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _AsyncRecordingCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "chatcmpl_fake"}


def test_a_lazily_produced_async_stream_holds_the_permit_only_inside_the_provider() -> (
    None
):
    completions = _AsyncLazyCompletions()
    fake = _FakeConsent()
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=fake,
    )
    other = _AsyncRecordingCompletions()
    wrapped_other = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=other)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=fake,
    )
    seen_in_the_loop: list[bool] = []

    async def agent() -> None:
        async for _chunk in await client.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        ):
            seen_in_the_loop.append(_INTERNAL_CALL.get() is not None)
            await wrapped_other.chat.completions.create(
                model="gpt-5", messages=_MESSAGES
            )
        async with client.chat.completions.stream(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        ) as chunks:
            async for _chunk in chunks:
                seen_in_the_loop.append(_INTERNAL_CALL.get() is not None)
                await wrapped_other.chat.completions.create(
                    model="gpt-5", messages=_MESSAGES
                )

    asyncio.run(agent())

    assert completions.held == [True] * 4, "the provider's steps ran without a permit"
    assert seen_in_the_loop == [False] * 4
    assert [call["user_id"] for call in fake.calls] == [
        "usr_marie_456",
        None,
        None,
        "usr_marie_456",
        None,
        None,
    ]
    assert len(other.calls) == 4


# ── the cloud is unreachable when the stream is opened ───────────────────────


class _UnreachableConsent:
    """A cloud that cannot be reached, at the moment the stream is opened."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        raise ConsentUnreachableError("the ID Side cloud could not be reached")


def test_an_unreachable_cloud_stops_a_blocking_stream_at_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail-closed applies where the request is, and for a helper that is entry.

    The call itself made no request, so there was nothing to stop there. The
    cloud is asked when the manager is entered, and that is the moment the
    provider's own helper would have sent — so a failure there has to come out
    as a refusal rather than as a stream that quietly opens unproven.

    And it has to be *said*. A stream that raises out of `__enter__` with
    nothing in the journal is the least debuggable shape this SDK can take at a
    customer site: the application sees an error from a `with` statement and the
    log shows a healthy wrap and then silence.
    """
    unreachable = _UnreachableConsent()

    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped, _fake, wire = _probe(consent_client=unreachable)
        manager = wrapped.chat.completions.stream(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        )
        assert unreachable.calls == [], "the cloud was asked before the stream opened"

        with pytest.raises(ConsentUnreachableError), manager:
            pass

    assert len(unreachable.calls) == 1
    assert wire.requests == [], "the request left the process without a proof"
    logged = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.split(".")[0] == "idside"
    )
    assert "authorize_failed" in logged, "the refusal left no trace in the journal"


# ── the overloaded helper: resuming a response is not making one ─────────────


def test_resuming_a_response_asks_no_identity_and_mints_no_proof() -> None:
    """``responses.stream(response_id=…)`` retrieves; it creates nothing.

    The helper is overloaded: with `response_id` (and no `input`/`model`) it
    reaches `GET /v1/responses/{id}` to replay a response that already exists.
    There is no prompt, no model and nothing new to consent to — the request is
    the same one `responses.retrieve()` makes, which the net lets through
    untouched because a bodyless method carries nothing to infer on.

    Enforcing it demanded an identity for a call that creates nothing, and then
    authorized with `model: ""` — asking the cloud to decide about a model that
    does not exist, and signing a token that says an AI call happened. A proof
    of something that did not occur is the defect this SDK exists to prevent.
    """
    wrapped, fake, wire = _probe()

    with (
        pytest.raises(openai.APIConnectionError),
        wrapped.responses.stream(response_id="resp_123"),
    ):
        pass

    assert fake.calls == [], "a call that creates nothing was authorized"
    assert wire.requests == [("GET", "/v1/responses/resp_123", False)]


def test_the_creating_form_of_the_same_helper_is_still_enforced() -> None:
    """One method, two forms, and only one of them is an AI call.

    Named next to the test above so the pair reads as one statement: what is
    stepped over is the retrieval, never the creation.
    """
    wrapped, fake, wire = _probe()

    # Naming nobody: the call is authorized with the API key as its subject,
    # and leaves with its proof like any other.
    with pytest.raises(openai.APIConnectionError):  # noqa: SIM117 — two distinct blocks
        with wrapped.responses.stream(model="gpt-5", input="hello"):
            pass

    assert len(fake.calls) == 1
    assert fake.calls[0]["call_site"] == "responses.create"
    assert fake.calls[0]["user_id"] is None
    assert wire.requests == [("POST", "/v1/responses", True)]


def test_an_identity_passed_to_the_resuming_form_never_reaches_the_provider() -> None:
    """Identity kwargs are the SDK's own and are consumed, here as everywhere.

    An application that passes `user_id=` on every call must not get a
    `TypeError` from the provider's helper on the one form that needs no
    identity — and the provider must not be handed a keyword it has never
    heard of.
    """
    wrapped, fake, wire = _probe()

    with (
        pytest.raises(openai.APIConnectionError),
        wrapped.responses.stream(response_id="resp_123", user_id="usr_marie_456"),
    ):
        pass

    assert fake.calls == []
    assert wire.requests == [("GET", "/v1/responses/resp_123", False)]


def test_a_call_let_through_is_still_refused_if_it_turns_out_to_post() -> None:
    """Stepping aside is not a licence to send. The net decides that.

    Nothing here rests on the descriptor being right about which form of a call
    creates something: a call let through carries no ACT token, so the moment it
    turns out to be body-bearing the net refuses it before it leaves the
    process. This descriptor is deliberately wrong — it names a resuming
    parameter on an endpoint that always POSTs — and the request still does not
    go out.
    """
    site = _OPENAI.call_site("chat.completions.create")
    assert site is not None
    misdeclared = replace(
        _OPENAI,
        call_sites=(replace(site, resuming_params=("extra_body",)),),
    )
    wrapped, fake, wire = _probe(misdeclared)

    with pytest.raises(UnenforcedCallError):
        wrapped.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, extra_body={"prompt_cache_key": "k"}
        )

    assert fake.calls == []
    assert wire.requests == [], "the request left the process"
