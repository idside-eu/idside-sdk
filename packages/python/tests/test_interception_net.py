"""The interception net: nothing reaches a provider without enforcement.

Patching methods on resource instances can never be exhaustive — a provider
adds an endpoint, a helper posts directly, ``with_options()`` hands back a fresh
unpatched client — and every gap is an AI call that reached the provider with no
authorize, no ACT token and no ledger entry. Silently.

These are *probe* tests: a real ``openai.OpenAI`` client pointed at a closed
port, with a recorder installed on its HTTP transport underneath the SDK. They
assert two things at once, and the second is the one that matters: that
authorize ran (or legitimately did not), AND what actually reached the wire.
A connection error without an ``UnenforcedCallError`` behind it means the
request left the process — the bug would still be there.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from idside import _guard, consent
from idside._transport import ConsentDecision
from idside.adapters import ProviderAdapter
from idside.exceptions import (
    IDSideError,
    MissingIdentityError,
    UnenforceableOptionError,
    UnenforcedCallError,
    UnsupportedProviderError,
)
from idside.providers import load_descriptors

openai = pytest.importorskip("openai")
pydantic = pytest.importorskip("pydantic")

_OPENAI = load_descriptors()["openai"]
_ANTHROPIC = load_descriptors()["anthropic"]
_TOKEN_HEADER = _OPENAI.token_header

_DECISION = ConsentDecision(
    consent_state={"training": False},
    params_forced={"store": False},
    act_token="tok_1",
    source="widget",
    legal_basis="consent",
    binding="user_id",
)

_MESSAGES = [{"role": "user", "content": "hello"}]


class _Answer(pydantic.BaseModel):
    answer: str


class _FakeConsent:
    """A consent client that counts authorizations and never touches a network."""

    def __init__(self, decision: ConsentDecision = _DECISION) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        return self.decision


class _FakeAsyncConsent:
    """The async half of :class:`_FakeConsent`: what wrap() builds for aio."""

    def __init__(self, decision: ConsentDecision = _DECISION) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    async def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        return self.decision


class _Wire:
    """What actually reached the HTTP transport: method, path, token present.

    ``headers`` and ``bodies`` are recorded alongside, for the few tests that
    need to prove a forced parameter or a merged header actually went out. The
    guard reads neither — see ``_guard.py``; a *test* may.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bool]] = []
        self.headers: list[dict[str, str]] = []
        self.bodies: list[bytes] = []

    @property
    def unenforced(self) -> list[tuple[str, str, bool]]:
        return [r for r in self.requests if not r[2]]


def _closed_port() -> int:
    """A port nothing listens on, so a real connect fails immediately."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return port


def _probe() -> tuple[Any, _FakeConsent, _Wire]:
    """A REAL OpenAI client, wired to a closed port, recorded at the transport."""
    client = openai.OpenAI(
        api_key="sk-test",
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
        wire.headers.append({k.lower(): v for k, v in request.headers.items()})
        wire.bodies.append(getattr(request, "content", b"") or b"")
        return original_send(request, *args, **kwargs)

    transport.send = _record
    fake = _FakeConsent()
    wrapped = consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=fake
    )
    return wrapped, fake, wire


# ── layer 1: call-site aliases ───────────────────────────────────────────────


def test_aliases_are_enforced_under_the_same_call_site_id() -> None:
    """``parse`` hits the same endpoint as ``create``, so it is the same site."""
    resolved = ProviderAdapter(_OPENAI).get_call_sites(openai.OpenAI(api_key="sk-test"))
    pairs = {(s.site.id, s.method) for s in resolved}
    assert ("chat.completions.create", "create") in pairs
    assert ("chat.completions.create", "parse") in pairs
    assert ("responses.create", "parse") in pairs


def test_absent_aliases_are_skipped_like_absent_call_sites() -> None:
    completions = SimpleNamespace(create=lambda **kw: kw)  # no `parse`
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    resolved = ProviderAdapter(_OPENAI).get_call_sites(client)
    assert {s.method for s in resolved} == {"create"}


def test_parse_is_enforced_and_the_token_reaches_the_wire() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(openai.APIConnectionError):
        wrapped.chat.completions.parse(
            model="gpt-5",
            messages=_MESSAGES,
            response_format=_Answer,
            user_id="usr_marie_456",
        )

    assert len(fake.calls) == 1
    assert fake.calls[0]["call_site"] == "chat.completions.create"
    assert wire.requests == [("POST", "/v1/chat/completions", True)]
    assert wire.unenforced == []


def test_parse_without_an_identity_never_reaches_the_wire() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(MissingIdentityError):
        wrapped.chat.completions.parse(
            model="gpt-5", messages=_MESSAGES, response_format=_Answer
        )

    assert fake.calls == []
    assert wire.requests == []


def test_rewrapping_does_not_retain_another_transport_provenance() -> None:
    """Idempotence includes the guard's private callback list, not only methods."""
    wrapped, fake, _wire = _probe()
    transport = wrapped._client
    guarded_send = transport.send
    provenances = guarded_send.__idside_transport_guarded__
    assert len(provenances) == 1

    consent.wrap(wrapped, adapter=ProviderAdapter(_OPENAI), consent_client=fake)

    assert transport.send is guarded_send
    assert len(provenances) == 1


# ── layer 2: re-entrancy pass-through ────────────────────────────────────────


class _ReentrantMessages:
    """An ``anthropic``-shaped resource whose ``parse`` defers to ``create``."""

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, str]:
        self.create_calls.append(kwargs)
        return {"id": "msg_fake"}

    def parse(self, **kwargs: Any) -> dict[str, str]:
        return self.create(**kwargs)


def test_an_alias_calling_the_patched_create_authorizes_once() -> None:
    messages = _ReentrantMessages()
    fake = _FakeConsent()
    client = consent.wrap(
        SimpleNamespace(messages=messages),
        adapter=ProviderAdapter(_ANTHROPIC),
        consent_client=fake,
    )

    client.messages.parse(model="claude-sonnet-5", messages=_MESSAGES, user_id="usr_x")

    assert len(fake.calls) == 1, "the second pass must not authorize again"
    assert len(messages.create_calls) == 1
    headers = messages.create_calls[0][_ANTHROPIC.headers_kwarg]
    assert headers[_ANTHROPIC.token_header] == "tok_1"


class _TwiceReentrantMessages(_ReentrantMessages):
    """An alias whose retry reaches the patched ``create`` a second time."""

    def parse(self, **kwargs: Any) -> dict[str, str]:
        first = self.create(**kwargs)
        self.create(**kwargs)
        return first


def test_one_permit_covers_one_nested_hop_and_not_the_next() -> None:
    """The permit is one-shot, and a second hop is a second AI call.

    A provider helper that retries reaches the patched method again, with the
    same ACT token still on it. Waving that through would put two inference
    calls behind one authorization and one ledger entry — the opposite of
    invariant #4, arrived at from inside. So the second hop re-enters
    enforcement on its own merits, and finds no identity: the wrapper consumed
    it on the way in, because the provider has never heard of it.
    """
    messages = _TwiceReentrantMessages()
    fake = _FakeConsent()
    client = consent.wrap(
        SimpleNamespace(messages=messages),
        adapter=ProviderAdapter(_ANTHROPIC),
        consent_client=fake,
    )

    with pytest.raises(MissingIdentityError):
        client.messages.parse(
            model="claude-sonnet-5", messages=_MESSAGES, user_id="usr_marie_456"
        )

    assert len(fake.calls) == 1, "the second hop rode the first hop's permit"
    assert len(messages.create_calls) == 1, "an unauthorized call reached the provider"


@pytest.mark.parametrize("token", ["", "tok_caller_supplied"])
def test_a_caller_supplied_token_header_cannot_skip_authorization(token: str) -> None:
    messages = _ReentrantMessages()
    fake = _FakeConsent()
    client = consent.wrap(
        SimpleNamespace(messages=messages),
        adapter=ProviderAdapter(_ANTHROPIC),
        consent_client=fake,
    )

    with pytest.raises(MissingIdentityError):
        client.messages.create(
            model="claude-sonnet-5",
            messages=_MESSAGES,
            extra_headers={_ANTHROPIC.token_header: token},
        )

    assert fake.calls == []
    assert messages.create_calls == []


# ── layer 3: the transport net ───────────────────────────────────────────────


def test_an_unwrapped_post_endpoint_is_refused_before_it_is_sent() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(UnenforcedCallError) as excinfo:
        wrapped.embeddings.create(model="text-embedding-3-small", input="hello")

    assert fake.calls == []
    assert wire.requests == [], "the request must not have left the process"
    message = str(excinfo.value)
    assert "POST" in message
    assert "/v1/embeddings" in message


@pytest.mark.parametrize("token", ["caller.forged.token", _DECISION.act_token])
def test_a_caller_supplied_token_header_cannot_open_the_transport_net(
    token: str,
) -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(UnenforcedCallError):
        wrapped.embeddings.create(
            model="text-embedding-3-small",
            input="a private sentence",
            extra_headers={_TOKEN_HEADER: token},
        )

    assert fake.calls == []
    assert wire.requests == [], "the request must not have left the process"


def test_a_caller_supplied_token_is_refused_whatever_its_case() -> None:
    """Refused, no longer quietly overwritten.

    Replacing it case-insensitively did keep the forged value off the wire, but
    it also let a call that tried to carry one be authorized and proven as if
    nothing had happened. Only the SDK sets this header, so a caller setting it
    is told so before the cloud is asked.
    """
    wrapped, fake, wire = _probe()

    with pytest.raises(UnenforceableOptionError):
        wrapped.chat.completions.create(
            model="gpt-5",
            messages=_MESSAGES,
            user_id="usr_marie_456",
            extra_headers={"x-ai-consent-token": "caller.forged.token"},
        )

    assert fake.calls == []
    assert wire.requests == []


def test_an_active_permit_cannot_authorize_another_client_or_endpoint() -> None:
    other_wire: list[httpx.Request] = []

    def other_handler(request: httpx.Request) -> httpx.Response:
        other_wire.append(request)
        return httpx.Response(200, json={}, request=request)

    other = consent.wrap(
        openai.OpenAI(
            api_key="sk-test",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(other_handler)),
        ),
        consent_client=_FakeConsent(),
    )

    paths: list[str] = []
    nested_errors: dict[str, BaseException] = {}
    wrapped: Any

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        token = request.headers[_TOKEN_HEADER]
        if request.url.path == "/v1/chat/completions":
            try:
                other.embeddings.create(
                    model="text-embedding-3-small",
                    input="private",
                    extra_headers={_TOKEN_HEADER: token},
                )
            except BaseException as exc:
                nested_errors["client"] = exc
            try:
                wrapped.embeddings.create(
                    model="text-embedding-3-small",
                    input="private",
                    extra_headers={_TOKEN_HEADER: token},
                )
            except BaseException as exc:
                nested_errors["endpoint"] = exc
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_1",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "gpt-5",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
                request=request,
            )
        return httpx.Response(200, json={"data": []}, request=request)

    fake = _FakeConsent()
    wrapped = consent.wrap(
        openai.OpenAI(
            api_key="sk-test",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        ),
        consent_client=fake,
    )

    wrapped.chat.completions.create(
        model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
    )

    assert isinstance(nested_errors.get("client"), UnenforcedCallError)
    assert isinstance(nested_errors.get("endpoint"), UnenforcedCallError)
    assert len(fake.calls) == 1
    assert other_wire == []
    assert paths == ["/v1/chat/completions"]


def test_a_provider_retry_keeps_the_same_authorized_transport_route() -> None:
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers[_TOKEN_HEADER])
        if len(attempts) == 1:
            return httpx.Response(
                500, json={"error": {"message": "retry"}}, request=request
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-5",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
            request=request,
        )

    fake = _FakeConsent()
    wrapped = consent.wrap(
        openai.OpenAI(
            api_key="sk-test",
            max_retries=1,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        ),
        consent_client=fake,
    )

    wrapped.chat.completions.create(
        model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
    )

    assert len(fake.calls) == 1
    assert attempts == [_DECISION.act_token, _DECISION.act_token]


def test_the_refusal_names_the_path_only_and_never_the_payload() -> None:
    wrapped, _fake, _wire = _probe()

    with pytest.raises(UnenforcedCallError) as excinfo:
        wrapped.moderations.create(input="a secret prompt")

    message = str(excinfo.value)
    assert "secret" not in message
    assert "?" not in message


def test_a_management_get_is_not_an_ai_call() -> None:
    """``models.list`` carries no prompt: it is allowed, and never authorized."""
    wrapped, fake, wire = _probe()

    with pytest.raises(openai.APIConnectionError) as excinfo:
        wrapped.models.list()

    assert not isinstance(excinfo.value.__cause__, UnenforcedCallError)
    assert fake.calls == []
    assert wire.requests == [("GET", "/v1/models", False)]


def test_a_client_without_an_http_transport_gets_no_net() -> None:
    """Every fake client in this suite: no transport to leak through, no error."""
    completions = SimpleNamespace(create=lambda **kw: kw, parse=lambda **kw: kw)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    assert (
        consent.wrap(
            client, adapter=ProviderAdapter(_OPENAI), consent_client=_FakeConsent()
        )
        is client
    )


# ── layer 4: derived clients ─────────────────────────────────────────────────


def test_with_options_hands_back_an_enforced_client() -> None:
    wrapped, fake, wire = _probe()

    derived = wrapped.with_options(max_retries=0)
    with pytest.raises(openai.APIConnectionError):
        derived.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        )

    assert len(fake.calls) == 1
    assert wire.requests == [("POST", "/v1/chat/completions", True)]
    assert wire.unenforced == []


def test_a_derived_client_still_demands_an_identity() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(MissingIdentityError):
        wrapped.copy(max_retries=0).chat.completions.create(
            model="gpt-5", messages=_MESSAGES
        )

    assert fake.calls == []
    assert wire.requests == []


def test_a_twice_derived_client_is_still_enforced() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(UnenforcedCallError):
        wrapped.with_options(max_retries=0).with_options(
            max_retries=0
        ).embeddings.create(model="text-embedding-3-small", input="hello")

    assert wire.requests == []
    assert fake.calls == []


# ── layer 5: the response wrappers ───────────────────────────────────────────
#
# ``with_raw_response`` and ``with_streaming_response`` are ``cached_property``
# objects on the resource. The wrapper is built on FIRST ACCESS — which happens
# after ``wrap()`` has patched the instance — so it closes over the *patched*
# method and inherits its enforcement. These calls authorize, carry the forced
# parameters and the token; they are not refused. Public documentation claimed
# the opposite, for want of these tests, and the remedy it prescribed ("use a
# second, unwrapped client") gave away enforcement the calls already had.


def test_with_raw_response_is_enforced_like_the_method_it_wraps() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(openai.APIConnectionError):
        wrapped.chat.completions.with_raw_response.create(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        )

    assert len(fake.calls) == 1
    assert fake.calls[0]["call_site"] == "chat.completions.create"
    assert wire.requests == [("POST", "/v1/chat/completions", True)]
    assert wire.unenforced == []
    assert json.loads(wire.bodies[0])["store"] is False


def test_with_streaming_response_is_enforced_like_the_method_it_wraps() -> None:
    wrapped, fake, wire = _probe()

    # The wrapper hands back a context manager; the request happens on entry.
    with (
        pytest.raises(openai.APIConnectionError),
        wrapped.chat.completions.with_streaming_response.create(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        ),
    ):
        pass  # pragma: no cover - the connection fails on entry

    assert len(fake.calls) == 1
    assert fake.calls[0]["call_site"] == "chat.completions.create"
    assert wire.requests == [("POST", "/v1/chat/completions", True)]
    assert wire.unenforced == []
    assert json.loads(wire.bodies[0])["store"] is False
    # The wrapper injects an ``extra_headers`` of its own before calling the
    # patched method, which must merge into it rather than replace it — or the
    # caller silently loses the very behaviour they asked for.
    assert "x-stainless-raw-response" in wire.headers[0]


def test_a_raw_response_call_without_an_identity_never_reaches_the_wire() -> None:
    wrapped, fake, wire = _probe()

    with pytest.raises(MissingIdentityError):
        wrapped.chat.completions.with_raw_response.create(
            model="gpt-5", messages=_MESSAGES
        )

    assert fake.calls == []
    assert wire.requests == []


def test_a_raw_response_on_an_undeclared_endpoint_is_still_refused() -> None:
    """The wrapper inherits enforcement; it does not confer it."""
    wrapped, fake, wire = _probe()

    with pytest.raises(UnenforcedCallError):
        wrapped.embeddings.with_raw_response.create(
            model="text-embedding-3-small", input="hello"
        )

    assert fake.calls == []
    assert wire.requests == []


# ── the error itself ─────────────────────────────────────────────────────────


def test_unenforced_call_error_is_not_an_idside_error() -> None:
    """Pinned: a blanket ``except Exception`` must not swallow a security stop.

    ``openai._base_client`` turns any ``Exception`` raised during send into an
    ``APIConnectionError`` and retries it, which would dress a consent refusal
    up as a flaky network and hammer it.
    """
    assert issubclass(UnenforcedCallError, BaseException)
    assert not issubclass(UnenforcedCallError, Exception)
    assert not issubclass(UnenforcedCallError, IDSideError)

    swallowed_by = None
    try:
        try:
            raise UnenforcedCallError("stop")
        except IDSideError:
            swallowed_by = "except IDSideError"
        except Exception:
            swallowed_by = "except Exception"
    except UnenforcedCallError:
        swallowed_by = None
    assert swallowed_by is None, f"a security stop was swallowed by {swallowed_by}"


# ── layer 5: what the journal's `net=` is allowed to mean ────────────────────
#
# `install()` feeds one field an operator reads as "is this client covered?".
# Three facts have to stay apart in it, because the third one used to be
# reported as the first: the search finished and everything is guarded; the
# search finished and there was nothing to guard; the search did not finish, or
# something it found refused the patch — so a route out of this client is, or
# may be, unguarded.


def _answering_transport() -> httpx.Client:
    """A transport that answers 200 rather than dialling anything."""
    return httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))


def test_a_finished_search_that_guarded_everything_reports_a_complete_net() -> None:
    client = SimpleNamespace(_client=httpx.Client())
    assert _guard.install(client, _OPENAI) is True


def test_a_finished_search_that_found_nothing_says_there_was_nothing() -> None:
    """Not an error, and not a lie: a client with no transport cannot leak."""
    assert _guard.install(SimpleNamespace(nothing="here"), _OPENAI) is False


def _two_transports_one_behind_the_budget() -> tuple[Any, httpx.Client]:
    """A client whose second route out sits past the walk's node budget.

    The shape is the one that exists: ``google-genai`` holds a blocking and an
    async transport on one client. Here the first is found and guarded, and the
    second is behind enough attributes that the walk gives up before reaching
    it — so the client has an unguarded route out, and used to be reported as
    fully guarded.
    """
    filler = {f"resource_{i}": SimpleNamespace() for i in range(_guard._MAX_NODES + 8)}
    missed = _answering_transport()
    return (
        SimpleNamespace(
            _client=_answering_transport(),
            _api=SimpleNamespace(**filler, late_transport=missed),
        ),
        missed,
    )


def test_a_search_cut_short_by_its_budget_never_reports_a_complete_net() -> None:
    """The walk is bounded, so it can stop before it has seen everything.

    Reporting ``True`` here told an operator the net was on while this very
    transport carried whatever the client sent through it — the assertion at the
    end is that unguarded route, made once, so ``partial`` is not taken on
    trust.
    """
    client, missed = _two_transports_one_behind_the_budget()

    assert _guard.install(client, _OPENAI) == "partial"

    request = missed.build_request("POST", "https://api.example.test/v1/anything")
    assert missed.send(request).status_code == 200, (
        "the test no longer demonstrates an unguarded route"
    )
    first = client._client.build_request("POST", "https://api.example.test/v1/x")
    assert client._client.send(first).status_code == 200, (
        "an incomplete scan must not leave the client partly mutated"
    )


def test_wrap_refuses_partial_before_writing_a_successful_wrap_journal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    graph, _missed = _two_transports_one_behind_the_budget()
    completions = SimpleNamespace(create=lambda **kw: kw, parse=lambda **kw: kw)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        _client=graph._client,
        _api=graph._api,
    )

    with (
        caplog.at_level("DEBUG", logger="idside"),
        pytest.raises(UnsupportedProviderError, match="transport guard"),
    ):
        consent.wrap(
            client, adapter=ProviderAdapter(_OPENAI), consent_client=_FakeConsent()
        )

    wrap_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("wrap ")
    ]
    assert wrap_lines == []
    assert "send" not in graph._client.__dict__, (
        "a failed wrap must restore transport changes made by that attempt"
    )


@pytest.mark.parametrize("factory", ["openai", "async_openai", "anthropic"])
def test_the_clients_this_sdk_ships_support_for_are_scanned_completely(
    factory: str,
) -> None:
    """The budget is a bound on a hostile shape, not a bound on real clients.

    Every resource is touched first, because they are built lazily: an untouched
    client is a small object graph, and a warm one is the graph the walk really
    has to cross. If a client library ever grows past the budget, this is where
    it is found — and the answer is the budget, not a quieter ``True``.
    """
    if factory == "anthropic":
        anthropic = pytest.importorskip("anthropic")
        client: Any = anthropic.Anthropic(api_key="sk-ant-test")
        for name in ("messages", "models", "beta", "completions"):
            getattr(client, name, None)
        descriptor = _ANTHROPIC
    else:
        maker = openai.AsyncOpenAI if factory == "async_openai" else openai.OpenAI
        client = maker(api_key="sk-test")
        for name in ("chat", "responses", "embeddings", "files", "beta", "models"):
            getattr(client, name, None)
        _ = client.chat.completions
        descriptor = _OPENAI

    assert _guard.install(client, descriptor) is True


# ── layer 5b: a transport that will not take the patch ───────────────────────


class _SealedTransport(httpx.Client):
    """A transport whose ``send`` cannot be reassigned.

    Nothing exotic: a client library is free to seal its attributes, and one
    that does turned ``wrap()`` into an exception raised AFTER the call sites
    had been patched — a half-wrapped client and a crash at construction.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "send":
            raise AttributeError("this transport does not take a patch")
        super().__setattr__(name, value)


def test_a_transport_that_refuses_the_patch_is_reported_not_raised() -> None:
    client = SimpleNamespace(_client=_SealedTransport())
    assert _guard.install(client, _OPENAI) == "partial"


def test_partial_rollback_keeps_a_previous_shared_transport_guard() -> None:
    shared = _answering_transport()

    def previous(token: str, method: str, path: str, _origin: Any) -> bool:
        return token == "tok_previous" and method == "POST" and path == "/v1/previous"

    assert _guard.install(SimpleNamespace(_client=shared), _OPENAI, previous) is True
    guarded_send = shared.send

    client = SimpleNamespace(shared=shared, sealed=_SealedTransport())
    assert (
        _guard.install(client, _OPENAI, lambda _token, _method, _path, _origin: True)
        == "partial"
    )
    assert shared.send is guarded_send

    request = shared.build_request(
        "POST",
        "https://api.example.test/v1/previous",
        headers={_TOKEN_HEADER: "tok_previous"},
    )
    assert shared.send(request).status_code == 200


def test_wrapping_refuses_a_client_with_an_incomplete_transport_guard() -> None:
    completions = SimpleNamespace(
        create=lambda **kw: kw, parse=lambda **kw: kw, stream=lambda **kw: kw
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions), _client=_SealedTransport()
    )
    fake = _FakeConsent()

    original = completions.create
    with pytest.raises(UnsupportedProviderError, match="transport guard"):
        consent.wrap(client, adapter=ProviderAdapter(_OPENAI), consent_client=fake)

    assert completions.create is original
    assert fake.calls == []


# ── layer 6: a transport this SDK never imported ─────────────────────────────
#
# `isinstance(node, httpx.Client | httpx.AsyncClient)` does not ask "is this an
# HTTP client". It asks which *distribution* the class was shipped by, and that
# question had one answer for years and now has two: `openai>=3` requires
# `httpx2`, a separate distribution whose clients are not `httpx` clients and
# never will be. Under it the walk matched nothing, `install()` returned
# `False`, and the journal printed `net=false` — "there was nothing to guard" —
# about a client holding a live, unguarded route to the provider. The worst
# shape a failure can take: silent, and reported as reassuring.
#
# So recognition is structural, and read off the CLASS: an object whose type
# carries `send`, `build_request` and `base_url` is an HTTP client, whoever
# packaged it. The class is inert — `getattr` on a type hands back the function
# or the property object without running either — which is the same reason the
# walk itself reads `__dict__` and never an instance attribute.


class _ForeignURL:
    def __init__(self, path: str) -> None:
        self.path = path


class _ForeignRequest:
    def __init__(self, method: str, path: str, headers: dict[str, str]) -> None:
        self.method = method
        self.url = _ForeignURL(path)
        self.headers = headers


class _ForeignTransport:
    """An httpx-shaped client from a distribution this SDK does not import.

    Same three marks, same call shape, no relation to `httpx` — which is exactly
    what `openai>=3` hands the SDK today.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []

    @property
    def base_url(self) -> str:
        return "https://api.example.test"

    def build_request(
        self, method: str, path: str, headers: dict[str, str] | None = None
    ) -> _ForeignRequest:
        return _ForeignRequest(method, path, dict(headers or {}))

    def send(self, request: _ForeignRequest, **_options: Any) -> str:
        self.sent.append(request.method)
        return "sent"


class _ForeignAsyncTransport(_ForeignTransport):
    """Its async half, split the way `httpx` splits its own."""

    async def send(  # type: ignore[override]
        self, request: _ForeignRequest, **_options: Any
    ) -> str:
        self.sent.append(request.method)
        return "sent"


class _NotATransport:
    """Two of the three marks, and no business being patched.

    A queue client, a mailer, an SDK shim: `send` plus a `base_url` is a common
    enough shape that recognising it would be a net that breaks working objects
    instead of guarding routes.
    """

    base_url = "https://example.test"

    def send(self, message: Any) -> str:
        return "delivered"


def test_a_transport_from_another_distribution_is_still_guarded() -> None:
    """The live case: `openai>=3` sends on `httpx2`, not on `httpx`."""
    transport = _ForeignTransport()

    assert _guard.install(SimpleNamespace(_client=transport), _OPENAI) is True

    with pytest.raises(UnenforcedCallError):
        transport.send(transport.build_request("POST", "/v1/chat/completions"))
    assert transport.sent == [], "the request must not have left the process"


def test_an_enforced_call_still_goes_through_a_foreign_transport() -> None:
    transport = _ForeignTransport()
    _guard.install(
        SimpleNamespace(_client=transport),
        _OPENAI,
        lambda token, method, path, _origin: (
            token == "tok_1" and method == "POST" and path == "/v1/chat/completions"
        ),
    )

    request = transport.build_request(
        "POST", "/v1/chat/completions", headers={_TOKEN_HEADER: "tok_1"}
    )
    assert transport.send(request) == "sent"
    assert transport.sent == ["POST"]


def test_a_foreign_async_transport_keeps_its_async_shape() -> None:
    """The half that would leak if the two ever answered differently.

    A blocking wrapper over a coroutine `send` mostly works by accident — the
    caller awaits what comes back. What does not survive is the shape: provider
    libraries branch on `iscoroutinefunction`, and one that saw a plain function
    here would take its blocking path against an async transport.
    """
    transport = _ForeignAsyncTransport()

    assert _guard.install(SimpleNamespace(_client=transport), _OPENAI) is True
    assert inspect.iscoroutinefunction(transport.send)

    async def _refused() -> None:
        await transport.send(transport.build_request("POST", "/v1/x"))

    with pytest.raises(UnenforcedCallError):
        asyncio.run(_refused())
    assert transport.sent == []


def test_an_object_that_merely_sends_is_not_mistaken_for_a_transport() -> None:
    """A false positive is not a harmless one: it breaks a working object."""
    mailer = _NotATransport()

    assert _guard.install(SimpleNamespace(mailer=mailer), _OPENAI) is False
    assert mailer.send("anything") == "delivered"


def test_the_transport_openai_3_actually_ships_is_recognised() -> None:
    """Not a stand-in: the real class, from the real distribution.

    `openai>=3` requires `httpx2<3,>=2.7`, so `pip install idside[openai]`
    resolves to it today. The two fakes above pin the rule; this pins the fact
    that the rule covers the client people actually hold.
    """
    httpx2 = pytest.importorskip("httpx2")

    for maker in (httpx2.Client, httpx2.AsyncClient):
        transport = maker()
        assert _guard.install(SimpleNamespace(_client=transport), _OPENAI) is True
        assert inspect.iscoroutinefunction(transport.send) is (
            maker is httpx2.AsyncClient
        ), "the wrapper must keep the shape the transport was written in"
        request = transport.build_request("POST", "https://api.example.test/v1/x")
        if maker is httpx2.Client:
            with pytest.raises(UnenforcedCallError):
                transport.send(request)


def test_on_httpx2_the_origin_is_read_and_a_redirect_is_not_followed() -> None:
    """The two facts the net added, on the transport `openai>=3` sends on."""
    httpx2 = pytest.importorskip("httpx2")
    hosts: list[str] = []
    seen: list[Any] = []

    def _redirecting(request: Any) -> Any:
        hosts.append(request.url.host)
        return httpx2.Response(307, headers={"location": "https://collector.example/"})

    transport = httpx2.Client(
        transport=httpx2.MockTransport(_redirecting), follow_redirects=True
    )

    def _provenance(token: str, method: str, path: str, origin: Any) -> bool:
        seen.append(origin)
        return token == "tok_1"

    assert _guard.install(SimpleNamespace(_client=transport), _OPENAI, _provenance)

    request = transport.build_request(
        "POST", "https://API.example.test/v1/x", headers={_TOKEN_HEADER: "tok_1"}
    )
    assert transport.send(request).status_code == 307
    assert hosts == ["api.example.test"], "the redirect was followed"
    assert seen == [("https", "api.example.test", 443)]


class _NarrowTransport(_ForeignTransport):
    """A transport of the right shape whose ``send`` takes nothing but a request.

    The net recognises transports by shape rather than by distribution, and it
    has to — ``openai>=3`` sends on ``httpx2``, whose clients are not ``httpx``
    clients. But the three marks say nothing about what ``send`` accepts, and
    the net adds a keyword to every authorized send.
    """

    def send(self, request: _ForeignRequest) -> str:  # type: ignore[override]
        self.sent.append(request.method)
        return "sent"


class _NarrowAsyncTransport(_ForeignTransport):
    """Its async half: the route that would be the one to leak."""

    async def send(self, request: _ForeignRequest) -> str:  # type: ignore[override]
        self.sent.append(request.method)
        return "sent"


def _client_sending_on(transport: Any) -> Any:
    """A client shaped like a provider's, whose only route out is ``transport``."""
    return SimpleNamespace(
        _client=transport,
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: kw)),
    )


@pytest.mark.parametrize(
    "transport", [_NarrowTransport, _NarrowAsyncTransport], ids=["blocking", "async"]
)
def test_a_transport_that_cannot_refuse_redirects_is_refused_at_wrap(
    transport: Any,
) -> None:
    """The keyword is added to every authorized send, so it is a wrap-time fact.

    Two outcomes had to be ruled out, and neither is the obvious one. Sending
    anyway, with redirects followed, is what the keyword exists to stop: a 307
    below the guard re-sends the prompt, the ACT token and the key to whatever
    host it names. And passing a keyword the transport will not take raises
    ``TypeError`` AFTER the cloud has authorized the call and written to the
    ledger — a proof of a request that never left the process, failing in a
    place that names nothing, while every bodyless call keeps working.

    So the client is refused where a client that cannot be guarded is always
    refused: at ``wrap()``, before anything is patched.
    """
    route = transport()

    with pytest.raises(UnsupportedProviderError) as caught:
        consent.wrap(
            _client_sending_on(route),
            adapter=ProviderAdapter(_OPENAI),
            consent_client=_FakeConsent(),
        )

    assert "follow_redirects" in str(caught.value)
    assert "send" not in route.__dict__, "a client that was refused was not patched"


class _UndescribableSend:
    """A ``send`` that will not say what it accepts.

    A ``send`` implemented in C, a callable object, an instrumentation shim
    built on a proxy: ``inspect.signature`` does not answer for all of them, and
    a transport whose signature cannot be read is one whose acceptance of
    ``follow_redirects`` cannot be established either.
    """

    @property
    def __signature__(self) -> Any:
        raise ValueError("this callable does not describe itself")

    def __call__(self, request: Any, **kwargs: Any) -> str:
        return "sent"


class _UndescribableTransport(_ForeignTransport):
    send = _UndescribableSend()  # type: ignore[assignment]


def test_a_transport_whose_send_cannot_be_read_is_refused_too() -> None:
    """Unknown is not permission: both remaining outcomes are unacceptable.

    ``**kwargs`` is read as acceptance because it is what instrumentation looks
    like — but a signature that answers nothing at all is not that. There is
    nothing to fall back on here, so this is where the fail-closed rule applies.
    """
    with pytest.raises(UnsupportedProviderError) as caught:
        consent.wrap(
            _client_sending_on(_UndescribableTransport()),
            adapter=ProviderAdapter(_OPENAI),
            consent_client=_FakeConsent(),
        )

    assert "follow_redirects" in str(caught.value)


def test_deriving_a_client_does_not_redo_the_redirect_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A derived client inherits the answer, exactly as it inherits the origin.

    It sends on its parent's transport and cannot bring another: `http_client=`
    is refused at the derivation itself. So whether that transport will take
    `follow_redirects` is a fact that cannot have changed since `wrap()` read
    it — and even a library that rebuilt the transport would rebuild it of the
    same class, with the same signature.

    Re-walking the object graph per derivation would therefore put that walk on
    the request path of an application that derives a client per call, to
    re-establish something already known.
    """
    wrapped, _fake, _wire = _probe()
    walks: list[Any] = []
    monkeypatch.setattr(
        _guard, "unredirectable_route", lambda client: walks.append(client)
    )

    derived = wrapped.with_options(timeout=5).with_options(timeout=7)

    assert walks == [], "the derivation re-ran a preflight its parent had passed"
    assert derived is not wrapped


def test_a_routed_credential_client_is_held_to_the_same_redirect_rule() -> None:
    """A routed call leaves on that client, so it is the same requirement."""
    with pytest.raises(UnsupportedProviderError) as caught:
        consent.wrap(
            _client_sending_on(_ForeignTransport()),
            adapter=ProviderAdapter(_OPENAI),
            consent_client=_FakeConsent(),
            credentials={"zero_retention": _client_sending_on(_NarrowTransport())},
        )

    assert "zero_retention" in str(caught.value)
    assert "follow_redirects" in str(caught.value)


def test_a_transport_that_takes_any_keyword_is_wrapped() -> None:
    """``**kwargs`` is an acceptance, and it is what instrumentation is written as.

    The suite's own ``_probe()`` replaces ``send`` with exactly that shape, and
    so does every retry shim and recorder an application installs. Reading it as
    a refusal would refuse the clients this SDK is most often handed.
    """
    route = _ForeignTransport()

    wrapped = consent.wrap(
        _client_sending_on(route),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent(),
    )

    assert (
        wrapped.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
        )["store"]
        is False
    )


class _SwallowingTransport(_ForeignTransport):
    """A transport that accepts the patch and quietly drops it.

    Not a hypothetical: an attribute proxy, a `__slots__` class with a
    permissive `__setattr__`, a recording double built on a mock. Nothing
    raises, so `setattr` looks like it worked — which is the one failure a
    `try`/`except` around the write cannot see.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "send":
            return
        super().__setattr__(name, value)


def test_a_transport_that_silently_drops_the_patch_is_reported_not_believed() -> None:
    transport = _SwallowingTransport()

    assert _guard.install(SimpleNamespace(_client=transport), _OPENAI) == "partial"

    # And what makes `partial` the honest word rather than a precaution: this
    # route out is open. Reporting `True` here would have described the net as
    # covering the very transport it failed to touch.
    assert transport.send(transport.build_request("POST", "/v1/x")) == "sent"
    assert transport.sent == ["POST"]


class _RefusingMeta(type):
    """A metaclass that answers no question about its instances' classes."""

    def __getattr__(cls, name: str) -> Any:
        raise RuntimeError(f"this class refuses to say whether it has {name!r}")


class _Unanswerable(metaclass=_RefusingMeta):
    """An object whose class raises on any attribute the class does not define.

    Arbitrary code runs when the net asks what an object is, and a proxy, a lazy
    importer or a strict ORM model may well raise rather than answer. The walk
    crosses a third-party object graph on the way to the transport, so it meets
    objects nobody wrote with it in mind.
    """


def test_an_object_that_refuses_to_say_what_it_is_is_not_patched() -> None:
    """A class that will not answer is not guessed at, and does not abort the walk.

    Two failures are possible and both are bad: patching something on the
    strength of an exception, and letting one hostile object stop the search
    before it reaches the real transport. The transport sitting next to it is
    what proves the second did not happen.
    """
    transport = _ForeignTransport()
    client = SimpleNamespace(strange=_Unanswerable(), _client=transport)

    assert _guard.install(client, _OPENAI) is True

    with pytest.raises(UnenforcedCallError):
        transport.send(transport.build_request("POST", "/v1/chat/completions"))
    assert transport.sent == [], "the request must not have left the process"


class _SealedTransport(_ForeignTransport):
    """A transport that will not take the patch: every write to `send` raises."""

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "send":
            raise AttributeError("send is read-only on this transport")
        super().__setattr__(name, value)


class _InstrumentedTransport(_ForeignTransport):
    """A transport whose `send` was already replaced on the INSTANCE.

    A recorder, a retry shim, an application's own instrumentation — whatever it
    is, it was there before this SDK arrived and has to be there after a wrap
    this SDK abandoned. The test suite's own `_probe()` does exactly this.
    """

    def __init__(self) -> None:
        super().__init__()
        self.send = self._own_send  # type: ignore[method-assign]

    def _own_send(self, request: _ForeignRequest) -> str:
        self.sent.append(f"instrumented:{request.method}")
        return "sent"


class _UnrollbackableTransport(_ForeignTransport):
    """A transport that takes the patch and then refuses to give it back."""

    def __delattr__(self, name: str) -> None:
        raise AttributeError("this transport does not un-patch")


def test_a_failed_install_puts_back_the_send_it_found() -> None:
    """A wrap that could not finish leaves no transport half-patched.

    `consent.wrap()` refuses the client on `partial`, so the application keeps
    using the object it already had. If this SDK's wrapper were still sitting on
    one of its transports, every request through that route would raise
    `UnenforcedCallError` from a client nobody wrapped.
    """
    instrumented = _InstrumentedTransport()
    own_send = instrumented.send
    client = SimpleNamespace(_client=instrumented, _fallback=_SealedTransport())

    assert _guard.install(client, _OPENAI) == "partial"

    assert instrumented.send is own_send, "the transport kept this SDK's wrapper"
    # And the restored route works, which is the half a marker check would miss.
    assert instrumented.send(instrumented.build_request("POST", "/v1/x")) == "sent"
    assert instrumented.sent == ["instrumented:POST"]


def test_a_failed_install_removes_a_patch_it_added_itself() -> None:
    """The other half of the rollback: nothing was there before, so nothing stays.

    Deleting the instance attribute is not the same as restoring one — it has to
    uncover the class's own `send` again, or the transport is left with a
    shadowing attribute that merely looks like the original.
    """
    plain = _ForeignTransport()
    client = SimpleNamespace(_client=plain, _fallback=_SealedTransport())

    assert _guard.install(client, _OPENAI) == "partial"

    assert "send" not in plain.__dict__, "a patch this install added was left behind"
    assert plain.send(plain.build_request("POST", "/v1/x")) == "sent"
    assert plain.sent == ["POST"]


def test_a_transport_that_refuses_rollback_is_still_reported_as_partial() -> None:
    """Perfect rollback can be impossible; claiming coverage never is.

    A hostile `__delattr__` leaves this SDK unable to undo what it did. The one
    thing that must not follow is `install()` answering `True` for a client it
    failed to cover — `consent.wrap()` reads that answer to decide whether the
    client is supported, and the journal prints it as `net=`. Admitting the gap
    is the whole difference.
    """
    stubborn = _UnrollbackableTransport()
    client = SimpleNamespace(_client=stubborn, _fallback=_SealedTransport())

    assert _guard.install(client, _OPENAI) == "partial"


# ── layer 5: a call site whose body runs later than the call ─────────────────


class _StreamingModels:
    """``models.generate_content_stream`` the way ``google-genai`` writes it.

    A **generator function**: calling it runs none of the body and sends
    nothing. The request happens on the first ``next()``, which is after the
    wrapper that authorized the call has returned.
    """

    def __init__(self, transport: httpx.Client) -> None:
        self._api_client = SimpleNamespace(_httpx_client=transport)
        self._transport = transport

    def generate_content_stream(self, **kwargs: Any) -> Any:
        request = self._transport.build_request(
            "POST",
            "/v1beta/models/x:streamGenerateContent",
            json={"contents": kwargs.get("contents")},
            headers=kwargs.get("extra_headers") or {},
        )
        self._transport.send(request)
        yield {"text": "hello"}


def test_a_generator_call_site_keeps_its_permit_until_the_request_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The net must not refuse the very request enforcement just authorized.

    ``models.generate_content_stream`` is a declared Gemini call site and a
    generator function, so the POST leaves the process one ``next()`` after the
    enforced wrapper returned. Provenance that lived only for the duration of
    the call would already be gone, and the guard would stop a call that
    carries a freshly minted ACT token.
    """
    monkeypatch.setattr(
        httpx.Client,
        "send",
        lambda self, request, *a, **k: httpx.Response(200, json={}, request=request),
    )
    transport = httpx.Client(base_url="https://gemini.invalid")
    client = SimpleNamespace(models=_StreamingModels(transport))
    fake = _FakeConsent()
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(load_descriptors()["gemini"]),
        consent_client=fake,
    )

    stream = wrapped.models.generate_content_stream(
        model="gemini-2.0-flash", contents="hello", user_id="usr_marie_456"
    )
    assert inspect.isgenerator(stream)
    assert fake.calls, "the cloud is asked at the call, not at the first chunk"

    assert list(stream) == [{"text": "hello"}]


def test_a_consumer_of_a_generator_call_site_never_holds_a_permit() -> None:
    """The permit is re-established per step, never handed to the caller.

    A permit still set between two chunks would be one an unrelated body-bearing
    request could ride on, for as long as the application took to consume the
    stream.
    """
    permit = consent._InternalCall("models.generate_content_stream", "tok_1", object())

    def _source() -> Any:
        assert consent._INTERNAL_CALL.get() is permit
        yield 1
        assert consent._INTERNAL_CALL.get() is permit
        yield 2

    wrapped = consent._lazily_permitted(permit, _source())
    for _chunk in wrapped:
        assert consent._INTERNAL_CALL.get() is None


class _AsyncStreamingModels:
    """``aio.models.generate_content_stream`` the way ``google-genai`` writes it.

    An ``async def`` that RETURNS an async generator. Awaiting the call runs
    none of the body below and sends nothing: the requests happen on each
    ``__anext__``, long after the wrapper that authorized the call returned.

    It sends once per chunk, which a single-request stream would not. That is
    the whole difference between a permit established once, around the first
    step, and one re-established around every step — and only the second is
    what the async half of the SDK promises.
    """

    def __init__(self, transport: httpx.AsyncClient) -> None:
        self._api_client = SimpleNamespace(_async_httpx_client=transport)
        self._transport = transport

    async def generate_content_stream(self, **kwargs: Any) -> Any:
        async def _chunks() -> Any:
            for part in ("hello", "world"):
                request = self._transport.build_request(
                    "POST",
                    "/v1beta/models/x:streamGenerateContent",
                    json={"contents": kwargs.get("contents")},
                    headers=kwargs.get("extra_headers") or {},
                )
                await self._transport.send(request)
                yield {"text": part}

        return _chunks()


def _async_stream_probe() -> tuple[Any, _FakeAsyncConsent, list[str]]:
    """A Gemini-shaped ASYNC client whose stream sends on every step."""
    sent: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(200, json={}, request=request)

    transport = httpx.AsyncClient(
        base_url="https://gemini.invalid",
        transport=httpx.MockTransport(_handler),
    )
    fake = _FakeAsyncConsent()
    wrapped = consent.wrap(
        SimpleNamespace(models=_AsyncStreamingModels(transport)),
        adapter=ProviderAdapter(load_descriptors()["gemini"]),
        consent_client=fake,
    )
    return wrapped, fake, sent


def test_an_async_generator_call_site_keeps_its_permit_at_every_step() -> None:
    """The async twin of the generator call site, and the same defect.

    The blocking half of this was a real production failure: a declared call
    site that is a generator function sends its request after the enforced
    wrapper has returned, so provenance that lived only for the duration of the
    call was already gone and the net refused the very request this SDK had
    just authorized. The async half is one keyword away from it and nothing
    covered it.

    Both halves of the assertion matter. The cloud is asked at the call, not at
    the first chunk — a stream nobody consumes must still be the thing that was
    authorized. And BOTH requests leave: a permit set once around the first
    step would let the first chunk out and refuse the second.
    """
    wrapped, fake, sent = _async_stream_probe()

    async def _drive() -> list[Any]:
        stream = await wrapped.models.generate_content_stream(
            model="gemini-2.0-flash", contents="hello", user_id="usr_marie_456"
        )
        assert inspect.isasyncgen(stream)
        assert fake.calls, "the cloud is asked at the call, not at the first chunk"
        return [chunk async for chunk in stream]

    assert asyncio.run(_drive()) == [{"text": "hello"}, {"text": "world"}]
    assert len(fake.calls) == 1, "one call, one authorization, however many chunks"
    assert sent == [
        "/v1beta/models/x:streamGenerateContent",
        "/v1beta/models/x:streamGenerateContent",
    ]


def test_a_consumer_of_an_async_generator_call_site_never_holds_a_permit() -> None:
    """The permit is re-established per step, never handed to the caller.

    The async twin of the blocking test above, and it pins the narrower half of
    the promise: a permit still set between two chunks would be one an
    unrelated body-bearing request could ride on, for as long as the
    application took to consume the stream — and on an event loop that window
    is shared with every other task in flight.
    """
    permit = consent._InternalCall("models.generate_content_stream", "tok_1", object())
    steps: list[Any] = []

    async def _source() -> Any:
        steps.append(consent._INTERNAL_CALL.get())
        yield 1
        steps.append(consent._INTERNAL_CALL.get())
        yield 2

    async def _drive() -> None:
        wrapped = consent._lazily_permitted(permit, _source())
        assert inspect.isasyncgen(wrapped)
        async for _chunk in wrapped:
            assert consent._INTERNAL_CALL.get() is None

    asyncio.run(_drive())
    assert steps == [permit, permit], "the permit lapsed before the second step"


def test_an_abandoned_async_stream_is_closed_rather_than_left_open() -> None:
    """The consumer may walk away; the provider still owns an open response.

    ``yield from`` would have done this for the blocking half and there is no
    async equivalent, so the async wrapper closes the source itself. Without
    it, a stream abandoned after one chunk leaves the provider's HTTP response
    unclosed until the garbage collector happens to notice.
    """
    closed: list[bool] = []

    async def _source() -> Any:
        try:
            yield 1
            yield 2
        finally:
            closed.append(True)

    async def _drive() -> None:
        # The source is held here on purpose: dropped, CPython would close it by
        # refcount and the assertion below would pass whatever the wrapper does.
        source = _source()
        wrapped = consent._lazily_permitted(
            consent._InternalCall("models.generate_content_stream", "tok_1", object()),
            source,
        )
        async for _chunk in wrapped:
            break
        await wrapped.aclose()
        assert closed == [True], "the provider's stream was left open"
        assert source is not None

    asyncio.run(_drive())


def test_an_enforced_call_does_not_wait_on_the_process_wide_predicate_lock() -> None:
    """One lock serialises every wrapped client in the process, so the request
    path must not want it.

    The prune took it on every body-bearing request of every client, and rebuilt
    the whole predicate list under it — so a server calling the AI from several
    threads serialised its own outgoing requests against each other, and paid an
    O(n) rewrite each time, for a list that changes only when a derived client is
    collected. Nothing had died on the normal path, and the work was done anyway.

    Stated the only way it can be: another thread holds the lock while a wrapped
    client makes a call. Under the old code that call waits for the lock, so the
    thread never finishes.
    """
    import threading

    wrapped, _fake, wire = _probe()
    done = threading.Event()
    raised: list[BaseException] = []

    def _call() -> None:
        try:
            wrapped.chat.completions.create(
                model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
            )
        except BaseException as exc:  # recorded here, asserted on below
            raised.append(exc)
        finally:
            done.set()

    with _guard._PROVENANCE_LOCK:
        threading.Thread(target=_call, daemon=True).start()
        assert done.wait(5), "the request path waited on a lock another thread held"

    assert isinstance(raised[0], openai.APIConnectionError), "the call ran to the wire"
    assert wire.requests == [("POST", "/v1/chat/completions", True)]


def test_abandoned_derived_clients_leave_no_provenance_behind() -> None:
    """`client.with_options(timeout=30)` per request must not grow the guard.

    Every derived client shares its parent's transport and adds its predicate
    to it. A predicate outlives nothing: once its client is collected, it is
    dropped — while a derived client still in use stays guarded.
    """
    import gc

    fake = _FakeConsent()
    sent: list[str] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "id": "c",
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
            },
            request=request,
        )

    wrapped = consent.wrap(
        openai.OpenAI(
            api_key="sk-test",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(_answer)),
        ),
        consent_client=fake,
    )
    provenances = wrapped._client.send.__idside_transport_guarded__
    alive = wrapped.with_options(timeout=30)

    for _ in range(50):
        wrapped.with_options(timeout=30)
    gc.collect()
    wrapped.with_options(timeout=30)  # an install prunes what died
    gc.collect()

    assert len(provenances) <= 3, "one predicate per client that still exists"

    for _ in range(20):
        wrapped.with_options(timeout=30)
    gc.collect()
    alive.chat.completions.create(
        model="gpt-5", messages=_MESSAGES, user_id="usr_marie_456"
    )  # a check prunes too

    assert len(provenances) == 2, "the parent and the derived client still in use"
    assert sent == ["/v1/chat/completions"]
    with pytest.raises(UnenforcedCallError):
        alive.embeddings.create(model="text-embedding-3-small", input="hello")
