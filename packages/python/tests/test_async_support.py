"""Async clients: the same guarantees, or none at all.

``AsyncOpenAI`` and its equivalents are what a FastAPI application holds, which
is most of the Python AI applications written today. Wrapping one has to mean
exactly what wrapping the blocking client means — same forced parameters, same
ACT token, same ``request_hash``, same errors — and, above all, the same net.

The net is the part that does not come for free. ``httpx`` splits its transport
in two: ``httpx.Client.send`` is a plain method and ``httpx.AsyncClient.send`` is
a coroutine, and ``AsyncClient`` is not a subclass of ``Client``. A guard that
recognises only the first is not a weaker guard on an async client — it is no
guard at all, and every endpoint the descriptor does not declare walks out
silently. That is invariant #3, so it is the first thing tested here.

No ``pytest-asyncio``: each test drives one coroutine with ``asyncio.run``. The
async path must not need a plugin to be exercised, or it will be exercised less.
"""

from __future__ import annotations

import asyncio
import inspect
import socket
import threading
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from idside import _guard, consent
from idside._transport import AsyncHttpConsentClient, ConsentDecision
from idside.adapters import ProviderAdapter
from idside.exceptions import (
    ConflictingIdentityError,
    ConsentUnreachableError,
    MissingIdentityError,
    ToolUseRefusedError,
    UnenforcedCallError,
)
from idside.providers import load_descriptors

openai = pytest.importorskip("openai")
pydantic = pytest.importorskip("pydantic")

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


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive one coroutine to completion."""
    return asyncio.run(coro)


class _Answer(pydantic.BaseModel):
    answer: str


class _FakeAsyncConsent:
    """An async consent client: what wrap() builds for an async provider client."""

    def __init__(self, decision: ConsentDecision = _DECISION) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    async def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        return self.decision


class _Wire:
    """What actually reached the HTTP transport: method, path, token present."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bool]] = []

    @property
    def unenforced(self) -> list[tuple[str, str, bool]]:
        return [r for r in self.requests if not r[2]]


def _closed_port() -> int:
    """A port nothing listens on, so a real connect fails immediately."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return port


def _probe() -> tuple[Any, _FakeAsyncConsent, _Wire]:
    """A REAL AsyncOpenAI client, wired to a closed port, recorded at its transport."""
    client = openai.AsyncOpenAI(
        api_key="sk-test",  # pragma: allowlist secret
        base_url=f"http://127.0.0.1:{_closed_port()}/v1",
        max_retries=0,
    )
    wire = _Wire()
    transport = client._client  # a test may reach in here; the SDK finds it by BFS
    original_send = transport.send

    async def _record(request: Any, *args: Any, **kwargs: Any) -> Any:
        wire.requests.append(
            (request.method, request.url.path, _TOKEN_HEADER in request.headers)
        )
        return await original_send(request, *args, **kwargs)

    transport.send = _record
    fake = _FakeAsyncConsent()
    wrapped = consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=fake
    )
    return wrapped, fake, wire


# ── the net, on an async transport ───────────────────────────────────────────


def test_an_unwrapped_post_endpoint_is_refused_on_an_async_client() -> None:
    """The one that decides whether async support is real.

    ``embeddings`` is not a declared call site on any descriptor. On the
    blocking client it is refused at the transport; on the async client the
    transport is a different class with a different method, and a net that
    misses it lets an unproven AI call out of a client the developer wrapped.
    """
    wrapped, fake, wire = _probe()

    with pytest.raises(UnenforcedCallError) as excinfo:
        _run(wrapped.embeddings.create(model="text-embedding-3-small", input="hello"))

    assert fake.calls == []
    assert wire.requests == [], "the request must not have left the process"
    message = str(excinfo.value)
    assert "POST" in message
    assert "/v1/embeddings" in message


def test_the_async_refusal_names_the_path_only_and_never_the_payload() -> None:
    wrapped, _fake, _wire = _probe()

    with pytest.raises(UnenforcedCallError) as excinfo:
        _run(wrapped.moderations.create(input="a secret prompt"))

    message = str(excinfo.value)
    assert "secret" not in message
    assert "?" not in message


def test_a_management_get_is_not_an_ai_call_on_an_async_client() -> None:
    """``models.list`` carries no prompt: allowed, and never authorized."""
    wrapped, fake, wire = _probe()

    async def _list() -> Any:
        # models.list() hands back a paginator, awaitable but not a coroutine.
        return await wrapped.models.list()

    with pytest.raises(openai.APIConnectionError) as excinfo:
        _run(_list())

    assert not isinstance(excinfo.value.__cause__, UnenforcedCallError)
    assert fake.calls == []
    assert wire.requests == [("GET", "/v1/models", False)]


def test_each_call_site_gets_the_wrapper_its_own_shape_can_carry() -> None:
    """One client, two shapes: the real mixed case, and the reason for the rule.

    ``AsyncOpenAI`` exposes ``create``/``parse`` as coroutine functions and
    ``stream`` as a plain one returning a context manager. Giving them the same
    wrapper would break one of the two, whichever was chosen.
    """
    wrapped, _fake, _wire = _probe()
    assert inspect.iscoroutinefunction(wrapped.chat.completions.create)
    assert inspect.iscoroutinefunction(wrapped.chat.completions.parse)
    assert not inspect.iscoroutinefunction(wrapped.chat.completions.stream)
    assert not inspect.iscoroutinefunction(wrapped.responses.stream)


def test_the_net_guards_every_transport_a_client_owns_not_just_the_first() -> None:
    """A client can own both transports, and then one of them was unguarded.

    ``google-genai`` is the shipped example: one ``genai.Client`` holds
    ``_httpx_client`` AND ``_async_httpx_client``, and exposes the async surface
    at ``client.aio``. A search that stopped at the first match guarded whichever
    attribute happened to come first and left the other one open — an
    unenforced route out of a client the developer wrapped.
    """
    client = SimpleNamespace(
        _api=SimpleNamespace(
            sync_transport=httpx.Client(),
            async_transport=httpx.AsyncClient(),
        )
    )
    assert _guard.install(client, _OPENAI) is True

    request = httpx.Request("POST", "https://api.example.test/v1/anything")
    with pytest.raises(UnenforcedCallError):
        client._api.sync_transport.send(request)
    with pytest.raises(UnenforcedCallError):
        _run(client._api.async_transport.send(request))


def test_the_async_net_lets_an_enforced_request_through() -> None:
    """The async guard accepts provenance for the exact authorized request."""
    transport = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200))
    )
    client = SimpleNamespace(_client=transport)
    assert (
        _guard.install(
            client,
            _OPENAI,
            lambda token, method, path, origin: (
                token == "tok_1"
                and method == "POST"
                and path == "/v1/chat/completions"
                and origin == ("https", "api.example.test", 443)
            ),
        )
        is True
    )

    async def _send() -> httpx.Response:
        request = transport.build_request(
            "POST", "https://api.example.test/v1/chat/completions"
        )
        request.headers[_TOKEN_HEADER] = "tok_1"
        return await transport.send(request)

    assert _run(_send()).status_code == 200


def test_the_async_guard_is_idempotent() -> None:
    transport = httpx.AsyncClient()
    client = SimpleNamespace(_client=transport)
    _guard.install(client, _OPENAI)
    once = transport.send
    _guard.install(client, _OPENAI)
    assert transport.send is once, "a second wrap() must not stack a second net"


# ── wrap() on an async client ────────────────────────────────────────────────


def test_an_async_call_is_authorized_forced_and_carries_the_token() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(openai.APIConnectionError):
        _run(
            wrapped.chat.completions.create(
                model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
            )
        )

    assert len(fake.calls) == 1
    assert fake.calls[0]["call_site"] == "chat.completions.create"
    assert fake.calls[0]["user_id"] == "usr_marie_456"
    assert wire.requests == [("POST", "/v1/chat/completions", True)]
    assert wire.unenforced == []


def test_an_async_call_without_an_identity_never_reaches_the_wire() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(MissingIdentityError):
        _run(wrapped.chat.completions.create(model="gpt-5", messages=_MESSAGES))

    assert fake.calls == []
    assert wire.requests == []


def test_an_async_call_with_two_identities_is_refused() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(ConflictingIdentityError):
        _run(
            wrapped.chat.completions.create(
                model="gpt-5",
                messages=_MESSAGES,
                user_id="usr_marie_456",
                ubt="eyJ.ubt.token",
            )
        )

    assert fake.calls == []
    assert wire.requests == []


def test_an_async_alias_is_enforced_under_the_same_call_site_id() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(openai.APIConnectionError):
        _run(
            wrapped.chat.completions.parse(
                model="gpt-5",
                messages=_MESSAGES,
                response_format=_Answer,
                user_id="usr_marie_456",
            )
        )

    assert len(fake.calls) == 1
    assert fake.calls[0]["call_site"] == "chat.completions.create"
    assert wire.requests == [("POST", "/v1/chat/completions", True)]


def test_an_async_stream_is_enforced_and_the_token_reaches_the_wire() -> None:
    """``stream()`` is sync even on an async client: it returns a context manager.

    So it cannot be patched with a coroutine wrapper without breaking
    ``async with``. The enforcement moves instead: the identity is consumed and
    the request hashed at the call, and the cloud is asked inside ``__aenter__``,
    which is where the request is actually made.
    """
    wrapped, fake, wire = _probe()

    async def _stream() -> None:
        async with wrapped.chat.completions.stream(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        ):
            pass  # __aenter__ is where the request is made, and where it fails

    with pytest.raises(openai.APIConnectionError):
        _run(_stream())

    assert len(fake.calls) == 1
    assert fake.calls[0]["call_site"] == "chat.completions.create"
    assert wire.requests == [("POST", "/v1/chat/completions", True)]


def test_an_async_stream_without_an_identity_fails_at_the_call_not_at_entry() -> None:
    """The identity check is pure, so it happens where the mistake was made."""
    wrapped, fake, wire = _probe()

    with pytest.raises(MissingIdentityError):
        wrapped.chat.completions.stream(model="gpt-5", messages=_MESSAGES)

    assert fake.calls == []
    assert wire.requests == []


def test_an_async_responses_stream_is_enforced() -> None:
    wrapped, fake, wire = _probe()

    async def _stream() -> None:
        async with wrapped.responses.stream(
            model="gpt-5", input="hello", user_id="usr_marie_456"
        ):
            pass

    with pytest.raises(openai.APIConnectionError):
        _run(_stream())

    assert len(fake.calls) == 1
    assert fake.calls[0]["call_site"] == "responses.create"
    assert wire.requests == [("POST", "/v1/responses", True)]


def test_wrapping_an_async_client_twice_does_not_stack_enforcement() -> None:
    wrapped, fake, wire = _probe()
    again = consent.wrap(wrapped, adapter=ProviderAdapter(_OPENAI), consent_client=fake)

    with pytest.raises(openai.APIConnectionError):
        _run(
            again.chat.completions.create(
                model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
            )
        )

    assert len(fake.calls) == 1, "one authorize per call, not one per wrap()"
    assert wire.requests == [("POST", "/v1/chat/completions", True)]


def test_a_derived_async_client_comes_back_enforced() -> None:
    wrapped, fake, wire = _probe()
    derived = wrapped.with_options(max_retries=0)

    with pytest.raises(UnenforcedCallError):
        _run(derived.embeddings.create(model="text-embedding-3-small", input="hello"))

    assert fake.calls == []
    assert wire.requests == []


# ── the same answers as the blocking client ──────────────────────────────────


class _RecordingAsync:
    """An openai-shaped async resource recording what each entrypoint received."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "chatcmpl_fake"}


class _RecordingSync:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "chatcmpl_fake"}


def test_the_request_hash_is_identical_sync_and_async() -> None:
    """One request, one hash, whichever client shape made it.

    The hash is what binds an ACT token to a request; if the two paths disagreed
    the proof of an async call would not verify against the call it describes.
    """
    payload = {"model": "gpt-5", "messages": _MESSAGES, "temperature": 0.2}

    sync_consent = _FakeSyncConsent()
    sync_client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=_RecordingSync())),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=sync_consent,
    )
    sync_client.chat.completions.create(**payload, user_id="usr_marie_456")

    async_consent = _FakeAsyncConsent()
    async_client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=_RecordingAsync())),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=async_consent,
    )
    _run(async_client.chat.completions.create(**payload, user_id="usr_marie_456"))

    assert (
        sync_consent.calls[0]["request_hash"] == async_consent.calls[0]["request_hash"]
    )


class _FakeSyncConsent:
    def __init__(self, decision: ConsentDecision = _DECISION) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        return self.decision


def test_forced_params_and_the_token_land_on_an_async_call() -> None:
    completions = _RecordingAsync()
    fake = _FakeAsyncConsent()
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=fake,
    )

    _run(
        client.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        )
    )

    kwargs = completions.calls[0]
    assert kwargs["store"] is False
    assert kwargs[_OPENAI.headers_kwarg][_TOKEN_HEADER] == "tok_1"
    assert _OPENAI.identity_param not in kwargs


def test_a_tools_refusal_stops_an_async_call_before_the_provider() -> None:
    """A constraint is applied on the caller's machine, async or not."""
    completions = _RecordingAsync()
    fake = _FakeAsyncConsent(
        ConsentDecision(
            consent_state={"agentic_tools": False},
            params_forced={},
            act_token="tok_1",
            source="widget",
            legal_basis="consent",
            binding="user_id",
            constraints={"tools": "refuse"},
        )
    )
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=fake,
    )

    with pytest.raises(ToolUseRefusedError):
        _run(
            client.chat.completions.create(
                model="gpt-5",
                messages=_MESSAGES,
                tools=[{"type": "function", "function": {"name": "f"}}],
                user_id="usr_marie_456",
            )
        )

    assert completions.calls == []


# ── the authorize hop is itself async ────────────────────────────────────────


def test_wrap_builds_an_async_consent_client_for_an_async_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocking on /v1/authorize once per AI call would stall the whole loop.

    An ASGI application serves every other request on that loop; a synchronous
    hop to the cloud on the AI path is worse than the problem async support was
    added to solve.
    """
    built: list[str] = []

    class _SpyAsync(_FakeAsyncConsent):
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            super().__init__()
            built.append("async")

    class _SpySync(_FakeSyncConsent):
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            super().__init__()
            built.append("sync")

    monkeypatch.setattr(consent, "AsyncHttpConsentClient", _SpyAsync)
    monkeypatch.setattr(consent, "HttpConsentClient", _SpySync)

    consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=_RecordingAsync())),
        adapter=ProviderAdapter(_OPENAI),
    )
    assert built == ["async"]

    built.clear()
    consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=_RecordingSync())),
        adapter=ProviderAdapter(_OPENAI),
    )
    assert built == ["sync"]


# ── no bypass, ever (invariant #8) ───────────────────────────────────────────


def test_async_support_adds_no_escape_hatch() -> None:
    """Nothing about being async earns a way out of enforcement."""
    client = openai.AsyncOpenAI(api_key="sk-test")  # pragma: allowlist secret
    for hatch in ("allow_unenforced", "unsafe_skip_guard", "sync_authorize"):
        with pytest.raises(TypeError):
            consent.wrap(client, consent_client=_FakeAsyncConsent(), **{hatch: True})


# ── fail-closed, and a loop that keeps turning ───────────────────────────────


class _SlowCloud(httpx.AsyncBaseTransport):
    """A cloud that answers only once someone else has had a turn on the loop."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._gate.wait()
        return httpx.Response(
            200,
            json={
                "decision": "allow",
                "consent_state": {},
                "params_forced": {"store": False},
                "mode": "test",
                "act_token": "act.jwt.token",
                "token_id": "act_01k4f9h2m3",
                "expires_at": "2026-09-12T12:00:00Z",
                "source": "widget",
                "legal_basis": "consent",
                "binding": "user_id",
            },
        )


def test_the_authorize_hop_does_not_block_the_event_loop() -> None:
    """The test that cannot pass if a blocking hop survives anywhere on the path.

    The cloud here answers only after another task has run, and nothing else
    can release it. So if the wait for /v1/authorize held the loop — a
    ``httpx.Client`` on the async path, an ``asyncio.run`` nested inside, a
    ``.result()`` on a future — the release would never be scheduled and this
    would sit until the timeout instead of returning.

    That is the whole reason async support exists. An ASGI application serves
    every other request in flight on this loop; stalling it once per AI call is
    worse than not supporting async at all.
    """
    completions = _RecordingAsync()

    async def _scenario() -> int:
        gate = asyncio.Event()
        client = consent.wrap(
            SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            adapter=ProviderAdapter(_OPENAI),
            consent_client=AsyncHttpConsentClient(
                base_url="https://api.test", transport=_SlowCloud(gate)
            ),
        )
        turns = 0

        async def _someone_else() -> None:
            nonlocal turns
            for _ in range(3):
                await asyncio.sleep(0)
                turns += 1
            gate.set()

        call = asyncio.create_task(
            client.chat.completions.create(
                model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
            )
        )
        await _someone_else()
        await call
        return turns

    turns = _on_its_own_thread(_scenario)
    assert turns == 3
    assert completions.calls[0]["store"] is False


def _on_its_own_thread(factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """Drive one coroutine with a deadline that survives a blocked loop.

    ``asyncio.wait_for`` cannot police this: a blocking hop holds the very loop
    its timer would have to run on, so the timeout meant to catch the
    regression never fires either and the suite hangs instead of failing. The
    deadline has to be outside the loop, which means another thread.
    """
    outcome: dict[str, Any] = {}

    def _target() -> None:
        try:
            outcome["value"] = asyncio.run(factory())
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(10.0)
    if thread.is_alive():
        raise AssertionError(
            "the event loop never came back: the wait for /v1/authorize is "
            "blocking it, which is what async support exists to avoid"
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def test_an_unreachable_cloud_stops_an_async_call_before_the_provider() -> None:
    """Fail-closed (invariant #3), end to end, on a real AsyncOpenAI client.

    The cloud transport is the one that fails; the assertion is that nothing
    reached the provider's wire either — not a request without a token, which
    the net would have caught, but no request at all.
    """
    wrapped = consent.wrap(
        openai.AsyncOpenAI(
            api_key="sk-test",  # pragma: allowlist secret
            base_url=f"http://127.0.0.1:{_closed_port()}/v1",
            max_retries=0,
        ),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=AsyncHttpConsentClient(
            base_url="https://api.test",
            transport=httpx.MockTransport(_connection_refused),
        ),
    )
    seen = _watch(wrapped)

    with pytest.raises(ConsentUnreachableError, match="the AI call was not made"):
        _run(
            wrapped.chat.completions.create(
                model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
            )
        )

    assert seen == []


def test_an_unreachable_cloud_stops_an_async_stream_at_entry() -> None:
    """A stream authorizes inside __aenter__, so that is where it must fail."""
    client = consent.wrap(
        openai.AsyncOpenAI(
            api_key="sk-test",  # pragma: allowlist secret
            base_url=f"http://127.0.0.1:{_closed_port()}/v1",
            max_retries=0,
        ),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=AsyncHttpConsentClient(
            base_url="https://api.test",
            transport=httpx.MockTransport(_connection_refused),
        ),
    )
    seen = _watch(client)

    async def _stream() -> None:
        async with client.chat.completions.stream(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        ):
            pass

    with pytest.raises(ConsentUnreachableError):
        _run(_stream())

    assert seen == []


def _connection_refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _watch(client: Any) -> list[tuple[str, str]]:
    """Record every request that reaches this client's transport."""
    seen: list[tuple[str, str]] = []
    transport = client._client
    original = transport.send

    async def _record(request: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append((request.method, request.url.path))
        return await original(request, *args, **kwargs)

    transport.send = _record
    return seen


# ── a second provider, and the decorator that hides an async method ──────────


anthropic = pytest.importorskip("anthropic")

_ANTHROPIC = load_descriptors()["anthropic"]


def _anthropic_probe() -> tuple[Any, _FakeAsyncConsent, list[tuple[str, str, bool]]]:
    client = anthropic.AsyncAnthropic(
        api_key="sk-test",  # pragma: allowlist secret
        base_url=f"http://127.0.0.1:{_closed_port()}",
        max_retries=0,
    )
    seen: list[tuple[str, str, bool]] = []
    transport = client._client
    original = transport.send

    async def _record(request: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(
            (
                request.method,
                request.url.path,
                _ANTHROPIC.token_header in request.headers,
            )
        )
        return await original(request, *args, **kwargs)

    transport.send = _record
    fake = _FakeAsyncConsent(
        ConsentDecision(
            consent_state={},
            params_forced={},
            act_token="tok_1",
            source="widget",
            legal_basis="consent",
            binding="user_id",
        )
    )
    wrapped = consent.wrap(
        client, adapter=ProviderAdapter(_ANTHROPIC), consent_client=fake
    )
    return wrapped, fake, seen


def test_a_second_provider_gets_the_same_async_treatment() -> None:
    """AsyncOpenAI is not the shape; it is one shape.

    Anthropic puts its token in a different header under a different kwarg, and
    its async ``create`` is reached through a different decorator. If async
    support were written to the one client it was developed against, this is
    where that would show.
    """
    wrapped, fake, seen = _anthropic_probe()

    with pytest.raises(anthropic.APIConnectionError):
        _run(
            wrapped.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=16,
                messages=_MESSAGES,
                user_id="usr_marie_456",
            )
        )

    assert len(fake.calls) == 1
    assert fake.calls[0]["call_site"] == "messages.create"
    assert fake.calls[0]["provider"] == "anthropic"
    assert seen == [("POST", "/v1/messages", True)]


def test_the_net_refuses_an_undeclared_endpoint_on_a_second_async_client() -> None:
    """``count_tokens`` posts the prompt and is on no descriptor."""
    wrapped, fake, seen = _anthropic_probe()

    with pytest.raises(UnenforcedCallError):
        _run(
            wrapped.messages.count_tokens(model="claude-sonnet-4-5", messages=_MESSAGES)
        )

    assert fake.calls == []
    assert seen == []


@pytest.mark.parametrize(
    ("client_factory", "method_path", "naive", "is_async"),
    [
        # The two that matter: a decorator hides the coroutine, so the naive
        # answer is wrong on the most common call in the ecosystem.
        pytest.param(
            lambda: openai.AsyncOpenAI(api_key="k"),  # pragma: allowlist secret
            ("chat", "completions", "create"),
            False,
            True,
            id="openai-async-create-is-hidden",
        ),
        pytest.param(
            lambda: anthropic.AsyncAnthropic(api_key="k"),  # pragma: allowlist secret
            ("messages", "create"),
            False,
            True,
            id="anthropic-async-create-is-hidden",
        ),
        # And the two that must NOT be dragged along by a looser rule.
        pytest.param(
            lambda: openai.OpenAI(api_key="k"),  # pragma: allowlist secret
            ("chat", "completions", "create"),
            False,
            False,
            id="blocking-create-stays-blocking",
        ),
        pytest.param(
            lambda: openai.AsyncOpenAI(api_key="k"),  # pragma: allowlist secret
            ("chat", "completions", "stream"),
            False,
            False,
            id="async-stream-is-still-not-a-coroutine",
        ),
    ],
)
def test_an_async_method_is_recognised_through_its_decorator(
    client_factory: Callable[[], Any],
    method_path: tuple[str, ...],
    naive: bool,
    is_async: bool,
) -> None:
    """The detection cannot be ``inspect.iscoroutinefunction`` alone, and this says so.

    Both provider libraries wrap their entrypoints in a decorator validating
    which argument combinations are legal. That wrapper is a plain ``def``
    returning the coroutine, so ``iscoroutinefunction`` answers False on
    ``AsyncOpenAI.chat.completions.create`` itself — the single most common call
    in the ecosystem. The naive answer is asserted here alongside the right one,
    because the gap between them is the whole reason the unwrapping exists:
    reading only that flag would hand ``create`` the wrapper meant for
    ``stream``, and every caller a context manager where they awaited a response.

    The last two cases are the other half of it. Unwrapping must not turn a
    blocking method async, or the SDK would await something that never was.
    """
    target: Any = client_factory()
    for name in method_path:
        target = getattr(target, name)
    assert inspect.iscoroutinefunction(target) is naive
    assert consent._is_async_call_site(target) is is_async


# ── the async surface nobody declared, on a real client ──────────────────────


def test_the_async_surface_of_a_blocking_client_does_not_leak() -> None:
    """The hole the net's second transport was written to close, on the real thing.

    ``google-genai`` ships ONE client that owns both transports and exposes its
    async surface at ``client.aio``. That surface is on no descriptor, so no
    method of it is patched, and the search that stopped at the first
    ``httpx.Client`` it found returned the blocking one — leaving every
    ``client.aio`` call to reach Google with no authorize, no ACT token and no
    ledger entry. Silently, and on a client the developer had wrapped.

    It is refused now. Refused, not enforced: what it would take to enforce it
    is a call site in the descriptor, which is a cross-SDK contract change, and
    invariant #8 says the answer to an endpoint the net refuses is never a way
    around the net. Until then the honest behaviour is the loud one, and this
    test is what says the behaviour is deliberate rather than merely current.
    """
    genai = pytest.importorskip("google.genai")
    client = consent.wrap(
        genai.Client(api_key="k", http_options={"base_url": "http://127.0.0.1:1/"}),
        consent_client=_FakeSyncConsent(),
    )
    fake = _FakeSyncConsent()
    client = consent.wrap(client, consent_client=fake)

    with pytest.raises(UnenforcedCallError) as excinfo:
        _run(
            client.aio.models.generate_content(model="gemini-2.5-flash", contents="hi")
        )

    assert fake.calls == []
    assert "POST" in str(excinfo.value)


def test_the_blocking_surface_of_that_same_client_still_works() -> None:
    """Guarding the second transport must not have cost the first one."""
    genai = pytest.importorskip("google.genai")
    fake = _FakeSyncConsent()
    client = consent.wrap(
        genai.Client(api_key="k", http_options={"base_url": "http://127.0.0.1:1/"}),
        consent_client=fake,
    )

    with pytest.raises(Exception) as excinfo:
        client.models.generate_content(
            model="gemini-2.5-flash", contents="hi", user_id="usr_marie_456"
        )

    assert not isinstance(excinfo.value, UnenforcedCallError)
    assert len(fake.calls) == 1
    assert fake.calls[0]["provider"] == "gemini"


# ── a consent transport the client's shape cannot carry ──────────────────────


def test_an_async_consent_client_on_a_blocking_client_is_refused_at_wrap() -> None:
    """Refused where the mistake was made, not on the first AI call.

    ``wrap(OpenAI(), consent_client=AsyncHttpConsentClient())`` used to fail on
    the first call, and fail unreadably: the blocking wrapper handed the
    coroutine that ``authorize()`` returned to code expecting a decision, so the
    developer got an ``AttributeError`` on a coroutine object plus a "coroutine
    was never awaited" warning from somewhere else entirely. Fail-closed, and
    undiagnosable.

    ``wrap()`` already holds both facts — the shape of the client and the
    transport it was handed — so it can say so at the line that got it wrong.
    """
    completions = _RecordingSync()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(TypeError) as raised:
        consent.wrap(
            client,
            adapter=ProviderAdapter(_OPENAI),
            consent_client=_FakeAsyncConsent(),
        )

    message = str(raised.value)
    assert "consent_client" in message
    assert "blocking" in message

    assert not consent._is_enforced(client.chat.completions.create), (
        "a refused wrap() must leave the client untouched, not half-patched"
    )


def test_a_blocking_consent_client_on_an_async_client_is_still_allowed() -> None:
    """The other direction is not a mistake, and stays supported.

    ``consent_client=`` is a documented override, and a test double or an
    application's own implementation is a plain callable more often than not.
    The async path awaits what is awaitable and takes the rest as it is, so
    nothing is lost by allowing it — while the blocking path has no way to wait
    for a coroutine at all.
    """
    completions = _RecordingAsync()
    sync_cloud = _FakeSyncConsent()

    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=sync_cloud,
    )
    _run(
        client.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        )
    )

    assert len(sync_cloud.calls) == 1
    assert len(completions.calls) == 1


# ── the async stream helper, on the way out ──────────────────────────────────


class _FakeAsyncStreamManager:
    """What an async ``stream()`` hands back: a manager, not a coroutine."""

    def __init__(self, opened: list[bool], exited: list[Any], suppress: bool) -> None:
        self._opened = opened
        self._exited = exited
        self._suppress = suppress

    async def __aenter__(self) -> _FakeAsyncStreamManager:
        self._opened.append(True)
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        self._exited.append(exc_info[0])
        return self._suppress


class _AsyncRecordingCompletions:
    """An ``AsyncOpenAI``-shaped resource: an awaited ``create``, a deferred
    ``stream``."""

    def __init__(self, suppress: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.opened: list[bool] = []
        self.exited: list[Any] = []
        self._suppress = suppress

    async def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("create", kwargs))
        return {"id": "chatcmpl_fake"}

    def stream(self, **kwargs: Any) -> _FakeAsyncStreamManager:
        self.calls.append(("stream", kwargs))
        return _FakeAsyncStreamManager(self.opened, self.exited, self._suppress)


def _async_stream_client(
    suppress: bool = False,
) -> tuple[Any, _AsyncRecordingCompletions, _FakeAsyncConsent]:
    completions = _AsyncRecordingCompletions(suppress)
    fake = _FakeAsyncConsent()
    wrapped = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=fake,
    )
    return wrapped, completions, fake


def test_an_async_stream_closes_the_provider_manager_it_opened() -> None:
    """Leaving the ``async with`` must reach the provider's own manager.

    This SDK stands in front of `stream()`, which means it stands in front of
    both ends of it. A manager that is entered and never exited leaves the
    provider holding an open HTTP response for the life of the process — and
    the application has no handle on it, because what it held was ours.
    """
    wrapped, completions, fake = _async_stream_client()

    async def _drive() -> None:
        async with wrapped.chat.completions.stream(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        ) as opened:
            assert opened is not None

    asyncio.run(_drive())

    assert len(fake.calls) == 1
    assert completions.opened == [True]
    assert completions.exited == [None], "the provider's manager was never exited"


def test_an_async_stream_hands_the_provider_manager_what_went_wrong() -> None:
    """An exception in the body is the manager's business, not ours to swallow.

    A streaming manager uses the exception to decide whether to abort the
    request or drain it. Passing `None` where an error happened, or exiting with
    nothing at all, changes what the provider does with a connection this SDK
    does not own.
    """
    wrapped, completions, _fake = _async_stream_client()

    async def _drive() -> None:
        async with wrapped.chat.completions.stream(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        ):
            raise ValueError("the application gave up on the stream")

    with pytest.raises(ValueError):
        asyncio.run(_drive())

    assert completions.exited == [ValueError]


def test_an_async_stream_manager_may_still_swallow_its_own_exception() -> None:
    """Whether an exception escapes is the provider manager's answer, unchanged.

    `__aexit__` returning a truthy value suppresses; dropping that answer on the
    floor would turn a manager that handles its own errors into one that does
    not, from the application's point of view, for no reason connected to
    consent.
    """
    wrapped, completions, _fake = _async_stream_client(suppress=True)

    async def _drive() -> None:
        async with wrapped.chat.completions.stream(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        ):
            raise ValueError("handled by the provider's own manager")

    asyncio.run(_drive())

    assert completions.exited == [ValueError]
