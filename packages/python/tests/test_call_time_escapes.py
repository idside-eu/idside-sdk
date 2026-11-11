"""What a single call can do to a wrapped client, and what it may no longer do.

Every test here started as an experiment that worked: a real provider client,
wrapped, and an option passed on one call that undid what the SDK had just
enforced. ``extra_body={"store": True}`` re-enabled the retention the user had
refused, after the cloud had signed a proof saying it was off. An
``extra_headers={"Authorization": …}`` sent the call on a credential routing
had moved it off. A ``server_url`` sent the prompt, the ACT token and the API
key to a host nobody wrapped. A 307 did the same without any option at all.

The proof describes the request the SDK authorized. Each of those made the
request that left the process a different one, and nothing recorded it — so
each is now refused, and refused *before* the cloud is asked wherever the call
itself already shows the problem: no authorize, no ledger entry, nothing on
the wire. A test here counts what reached the cloud and what reached the
transport, because an exception alone proves neither.

Real clients throughout (``openai``, ``anthropic``, ``google-genai``,
``mistralai``), on an ``httpx.MockTransport`` that records what would have left
the process. No network.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
from typing import Any

import httpx
import pytest

import idside
from idside import consent
from idside._transport import ConsentDecision
from idside.exceptions import (
    CredentialRoutingError,
    IDSideError,
    ToolUseRefusedError,
    UnenforceableOptionError,
    UnenforcedCallError,
    UnsupportedProviderError,
)

openai = pytest.importorskip("openai")
anthropic = pytest.importorskip("anthropic")
genai = pytest.importorskip("google.genai")
genai_types = pytest.importorskip("google.genai.types")
mistral_client = pytest.importorskip("mistralai.client")
mistral_utils = pytest.importorskip("mistralai.client.utils")

_USER = "usr_marie_456"
_MESSAGES = [{"role": "user", "content": "hello"}]
_TOOLS = [{"type": "function", "function": {"name": "lookup_patient_record"}}]
_ATTACKER = "https://collector.attacker.example/v1"


# ── the cloud and the wire ───────────────────────────────────────────────────


class _Cloud:
    """A consent client that records what it was asked and answers as told."""

    def __init__(
        self,
        *,
        forced: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
        posture: str | None = None,
    ) -> None:
        self.forced = forced or {}
        self.constraints = constraints or {}
        self.posture = posture
        self.calls: list[dict[str, Any]] = []

    def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        return ConsentDecision(
            consent_state={"agentic_tools": False},
            params_forced=dict(self.forced),
            act_token=f"act.test.{len(self.calls)}",
            source="widget",
            legal_basis="consent",
            binding="user_id",
            constraints=dict(self.constraints),
            credential_posture=self.posture,
        )


class _AsyncCloud(_Cloud):
    async def authorize(self, **kwargs: Any) -> ConsentDecision:  # type: ignore[override]
        return _Cloud.authorize(self, **kwargs)


_CHAT_OK = {
    "id": "chatcmpl_1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-4o-mini",
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": '{"answer": "ok"}'},
        }
    ],
}
_CHAT_SSE = (
    b'data: {"id":"c","object":"chat.completion.chunk","created":0,'
    b'"model":"m","choices":[{"index":0,"delta":{"content":"ok"},'
    b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
)
_RESPONSE_OK = {
    "id": "resp_1",
    "object": "response",
    "created_at": 0,
    "model": "gpt-4o-mini",
    "status": "completed",
    "output": [],
    "parallel_tool_calls": False,
    "tool_choice": "auto",
    "tools": [],
}
_MESSAGE_OK = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5",
    "content": [{"type": "text", "text": '{"answer": "ok"}'}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
_GEMINI_OK = {
    "candidates": [
        {
            "content": {"parts": [{"text": "ok"}], "role": "model"},
            "finishReason": "STOP",
        }
    ]
}
_CONVERSATION_OK = {
    "object": "conversation.response",
    "conversation_id": "conv_1",
    "outputs": [],
    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
}


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    )


_MESSAGE_SSE = _sse(
    [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {**_MESSAGE_OK, "content": [], "stop_reason": None},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
)


def _answer(request: Any, http: Any = httpx) -> Any:
    """A plausible 200 for whichever endpoint was hit.

    ``http`` is the HTTP library the client sends on: ``httpx``, or ``httpx2``
    for ``anthropic>=2``, which refuses an ``httpx`` client outright.
    """
    path = request.url.path
    body = json.loads(request.content) if request.content else {}
    if path.endswith(":streamGenerateContent"):
        return http.Response(
            200,
            content=b"data: " + json.dumps(_GEMINI_OK).encode() + b"\r\n\r\n",
            headers={"content-type": "text/event-stream"},
        )
    if path.endswith(":generateContent"):
        return http.Response(200, json=_GEMINI_OK)
    if path.endswith("/messages"):
        if body.get("stream"):
            return http.Response(
                200, content=_MESSAGE_SSE, headers={"content-type": "text/event-stream"}
            )
        return http.Response(200, json=_MESSAGE_OK)
    if path.endswith("/responses"):
        return http.Response(200, json=_RESPONSE_OK)
    if path.endswith("/conversations"):
        return http.Response(200, json=_CONVERSATION_OK)
    if body.get("stream"):
        return http.Response(
            200, content=_CHAT_SSE, headers={"content-type": "text/event-stream"}
        )
    return http.Response(200, json=_CHAT_OK)


def _http_of(library: Any) -> Any:
    """``httpx2`` when the library's base client sends on it, else ``httpx``."""
    base = importlib.import_module(f"{library.__name__}._base_client")
    return getattr(base, "httpx2", httpx)


class _Wire:
    """Every request that would have left the process."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def transport(self, http: Any = httpx) -> Any:
        def handler(request: Any) -> Any:
            request.read()
            self.requests.append(request)
            return _answer(request, http)

        return http.MockTransport(handler)

    def client(self, library: Any, *, asynchronous: bool = False) -> Any:
        """An HTTP client of the kind ``library`` sends on, recording here."""
        http = _http_of(library)
        maker = http.AsyncClient if asynchronous else http.Client
        return maker(transport=self.transport(http))

    def body(self, index: int = -1) -> dict[str, Any]:
        return dict(json.loads(self.requests[index].content))


def _openai(wire: _Wire, **kwargs: Any) -> Any:
    return openai.OpenAI(
        api_key="sk-test-default",
        max_retries=0,
        http_client=httpx.Client(transport=wire.transport()),
        **kwargs,
    )


def _async_openai(wire: _Wire) -> Any:
    return openai.AsyncOpenAI(
        api_key="sk-test-default",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=wire.transport()),
    )


def _deepseek(wire: _Wire) -> Any:
    return _openai(wire, base_url="https://api.deepseek.com")


def _anthropic(wire: _Wire, key: str = "sk-ant-test-default") -> Any:
    return anthropic.Anthropic(
        api_key=key,
        max_retries=0,
        http_client=wire.client(anthropic),
    )


def _async_anthropic(wire: _Wire) -> Any:
    return anthropic.AsyncAnthropic(
        api_key="sk-ant-test-default",
        max_retries=0,
        http_client=wire.client(anthropic, asynchronous=True),
    )


def _gemini(wire: _Wire, key: str = "AIza-test-default") -> Any:
    return genai.Client(
        api_key=key,
        http_options=genai_types.HttpOptions(
            httpx_client=httpx.Client(transport=wire.transport()),
            httpx_async_client=httpx.AsyncClient(transport=wire.transport()),
        ),
    )


def _mistral(wire: _Wire, key: str = "mk-test-default") -> Any:
    return mistral_client.Mistral(
        api_key=key,
        client=httpx.Client(transport=wire.transport()),
        async_client=httpx.AsyncClient(transport=wire.transport()),
    )


def _refused_before_authorize(
    call: Any, cloud: _Cloud, wire: _Wire, error: type[BaseException]
) -> BaseException:
    """The call raises ``error``, and neither the cloud nor the wire saw it."""
    with pytest.raises(error) as caught:
        call()
    assert cloud.calls == [], "the cloud was asked about a call that was refused"
    assert wire.requests == [], "a refused call reached the transport"
    return caught.value


def _drain(stream: Any) -> None:
    for _chunk in stream:
        pass


def _drain_manager(manager: Any) -> None:
    with manager as stream:
        for _chunk in stream:
            pass


# ── the error itself ─────────────────────────────────────────────────────────


def test_the_error_is_an_idside_error_exported_from_the_package_root() -> None:
    """Raised before anything is sent, so an ordinary ``Exception`` is right.

    ``UnenforcedCallError`` derives from ``BaseException`` because a provider
    library catches exceptions raised inside its transport. This one is raised
    in the SDK's own frame, before the provider library runs at all.
    """
    assert issubclass(UnenforceableOptionError, IDSideError)
    assert idside.UnenforceableOptionError is UnenforceableOptionError
    assert "UnenforceableOptionError" in idside.__all__


# ── D1: an option that rewrites the body the proof describes ────────────────


@pytest.mark.parametrize(
    "extra_body",
    [
        {"store": True},
        {"prompt_cache_retention": "24h"},
        {"tools": _TOOLS},
        {"model": "gpt-5"},
    ],
    ids=["store", "prompt_cache_retention", "tools", "model"],
)
def test_openai_extra_body_is_refused_before_authorize(
    extra_body: dict[str, Any],
) -> None:
    wire, cloud = _Wire(), _Cloud(forced={"store": False})
    client = consent.wrap(_openai(wire), consent_client=cloud)

    refused = _refused_before_authorize(
        lambda: client.chat.completions.create(
            model="gpt-4o-mini",
            messages=_MESSAGES,
            user_id=_USER,
            extra_body=extra_body,
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )
    assert "extra_body" in str(refused)


def test_openai_responses_extra_body_is_refused_before_authorize() -> None:
    wire, cloud = _Wire(), _Cloud(forced={"store": False, "background": False})
    client = consent.wrap(_openai(wire), consent_client=cloud)

    _refused_before_authorize(
        lambda: client.responses.create(
            model="gpt-4o-mini",
            input="hello",
            user_id=_USER,
            extra_body={
                "store": True,
                "background": True,
                "tools": [{"type": "web_search"}],
            },
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )


def test_openai_extra_query_is_refused_before_authorize() -> None:
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_openai(wire), consent_client=cloud)

    refused = _refused_before_authorize(
        lambda: client.chat.completions.create(
            model="gpt-4o-mini",
            messages=_MESSAGES,
            user_id=_USER,
            extra_query={"api-version": "2099-01-01"},
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )
    assert "extra_query" in str(refused)
    assert "2099" not in str(refused), "the message must never repeat the value"


@pytest.mark.parametrize(
    "route",
    ["parse", "stream", "with_raw_response", "with_streaming_response"],
)
def test_openai_extra_body_is_refused_on_every_route_to_the_endpoint(
    route: str,
) -> None:
    wire, cloud = _Wire(), _Cloud(forced={"store": False})
    completions = consent.wrap(_openai(wire), consent_client=cloud).chat.completions
    kwargs = {
        "model": "gpt-4o-mini",
        "messages": _MESSAGES,
        "user_id": _USER,
        "extra_body": {"store": True},
    }

    def _call() -> None:
        if route == "parse":
            completions.parse(**kwargs)
        elif route == "stream":
            _drain_manager(completions.stream(**kwargs))
        elif route == "with_raw_response":
            completions.with_raw_response.create(**kwargs)
        else:
            with completions.with_streaming_response.create(**kwargs) as response:
                response.read()

    _refused_before_authorize(_call, cloud, wire, UnenforceableOptionError)


def test_async_openai_extra_body_is_refused_on_create_and_stream() -> None:
    wire, cloud = _Wire(), _AsyncCloud(forced={"store": False})
    client = consent.wrap(_async_openai(wire), consent_client=cloud)
    kwargs = {
        "model": "gpt-4o-mini",
        "messages": _MESSAGES,
        "user_id": _USER,
        "extra_body": {"tools": _TOOLS},
    }

    async def _stream() -> None:
        async with client.chat.completions.stream(**kwargs) as stream:
            async for _chunk in stream:
                pass

    _refused_before_authorize(
        lambda: asyncio.run(client.chat.completions.create(**kwargs)),
        cloud,
        wire,
        UnenforceableOptionError,
    )
    _refused_before_authorize(
        lambda: asyncio.run(_stream()), cloud, wire, UnenforceableOptionError
    )


def test_deepseek_extra_body_is_refused_before_authorize() -> None:
    wire, cloud = _Wire(), _Cloud(constraints={"tools": "refuse"})
    client = consent.wrap(_deepseek(wire), consent_client=cloud)

    _refused_before_authorize(
        lambda: client.chat.completions.create(
            model="deepseek-chat",
            messages=_MESSAGES,
            user_id=_USER,
            extra_body={"tools": _TOOLS},
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )


@pytest.mark.parametrize(
    "extra_body",
    [
        {"tools": [{"name": "f", "input_schema": {"type": "object"}}]},
        {"model": "claude-opus-4-1"},
        {"mcp_servers": [{"type": "url", "url": "https://mcp.example", "name": "x"}]},
    ],
    ids=["tools", "model", "mcp_servers"],
)
def test_anthropic_extra_body_is_refused_before_authorize(
    extra_body: dict[str, Any],
) -> None:
    wire, cloud = _Wire(), _Cloud(constraints={"tools": "refuse"})
    client = consent.wrap(_anthropic(wire), consent_client=cloud)

    _refused_before_authorize(
        lambda: client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=16,
            messages=_MESSAGES,
            user_id=_USER,
            extra_headers={"anthropic-beta": "mcp-client-2025-04-04"},
            extra_body=extra_body,
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )


@pytest.mark.parametrize("route", ["parse", "stream", "with_raw_response", "async"])
def test_anthropic_extra_body_is_refused_on_every_route(route: str) -> None:
    wire = _Wire()
    cloud: _Cloud = _AsyncCloud() if route == "async" else _Cloud()
    kwargs = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 16,
        "messages": _MESSAGES,
        "user_id": _USER,
        "extra_body": {"tools": [{"name": "f", "input_schema": {"type": "object"}}]},
    }

    def _call() -> None:
        if route == "async":
            client = consent.wrap(_async_anthropic(wire), consent_client=cloud)
            asyncio.run(client.messages.create(**kwargs))
            return
        messages = consent.wrap(_anthropic(wire), consent_client=cloud).messages
        if route == "parse":
            messages.parse(**kwargs)
        elif route == "stream":
            _drain_manager(messages.stream(**kwargs))
        else:
            messages.with_raw_response.create(**kwargs)

    _refused_before_authorize(_call, cloud, wire, UnenforceableOptionError)


@pytest.mark.parametrize(
    "config",
    [
        {"http_options": {"extra_body": {"tools": [{"functionDeclarations": []}]}}},
        {"httpOptions": {"extraBody": {"generationConfig": {"temperature": 1}}}},
        "pydantic",
    ],
    ids=["dict", "camelCase-dict", "HttpOptions-object"],
)
def test_gemini_extra_body_is_refused_before_authorize(config: Any) -> None:
    if config == "pydantic":
        config = genai_types.GenerateContentConfig(
            http_options=genai_types.HttpOptions(extra_body={"tools": []})
        )
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_gemini(wire), consent_client=cloud)

    refused = _refused_before_authorize(
        lambda: client.models.generate_content(
            model="gemini-2.0-flash", contents="hello", config=config, user_id=_USER
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )
    assert "extra_body" in str(refused)


def test_gemini_extra_body_is_refused_on_the_streaming_call_site() -> None:
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_gemini(wire), consent_client=cloud)

    _refused_before_authorize(
        lambda: _drain(
            client.models.generate_content_stream(
                model="gemini-2.0-flash",
                contents="hello",
                config={"http_options": {"extra_body": {"tools": []}}},
                user_id=_USER,
            )
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )


def test_gemini_takes_no_argument_besides_model_contents_and_config() -> None:
    """Refused before the cloud, rather than a TypeError after the proof."""
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_gemini(wire), consent_client=cloud)

    refused = _refused_before_authorize(
        lambda: client.models.generate_content(
            model="gemini-2.0-flash",
            contents="hello",
            user_id=_USER,
            extra_headers={"X-Trace": "1"},
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )
    assert "extra_headers" in str(refused)


# ── D2: every argument that hands the model a tool ──────────────────────────


def _refused_after_authorize(call: Any, cloud: _Cloud, wire: _Wire) -> BaseException:
    """A refusal the cloud's answer was needed for: asked once, nothing sent."""
    with pytest.raises(ToolUseRefusedError) as caught:
        call()
    assert len(cloud.calls) == 1
    assert wire.requests == [], "a refused call reached the transport"
    return caught.value


@pytest.mark.parametrize(
    ("provider", "extra", "named"),
    [
        ("openai", {"functions": [{"name": "f"}]}, "functions"),
        ("openai", {"web_search_options": {}}, "web_search_options"),
        ("deepseek", {"functions": [{"name": "f"}]}, "functions"),
        ("deepseek", {"web_search_options": {}}, "web_search_options"),
    ],
)
def test_chat_completion_tool_fields_are_refused(
    provider: str, extra: dict[str, Any], named: str
) -> None:
    wire, cloud = _Wire(), _Cloud(constraints={"tools": "refuse"})
    raw = _openai(wire) if provider == "openai" else _deepseek(wire)
    client = consent.wrap(raw, consent_client=cloud)

    refused = _refused_after_authorize(
        lambda: client.chat.completions.create(
            model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER, **extra
        ),
        cloud,
        wire,
    )
    assert named in str(refused)


_RESPONSES_TOOL_FIELDS = [
    ({"conversation": "conv_123"}, "conversation"),
    ({"previous_response_id": "resp_123"}, "previous_response_id"),
    ({"prompt": {"id": "pmpt_123"}}, "prompt"),
    (
        {
            "input": [
                {"type": "additional_tools", "tools": [{"type": "web_search"}]},
            ]
        },
        "input",
    ),
    ({"input": [{"type": "item_reference", "id": "fc_123"}]}, "input"),
    # `type` is optional on an item reference: the id alone points at the item.
    ({"input": [{"id": "fc_123"}]}, "input"),
    ({"input": [{"id": "fc_123", "type": None}]}, "input"),
]


@pytest.mark.parametrize("action", ["refuse", "strip"])
@pytest.mark.parametrize(
    ("extra", "named"),
    _RESPONSES_TOOL_FIELDS,
    ids=[
        "conversation",
        "previous_response_id",
        "prompt",
        "additional_tools",
        "ref",
        "untyped-ref",
        "null-typed-ref",
    ],
)
def test_responses_objects_that_may_carry_tools_are_refused_even_under_strip(
    extra: dict[str, Any], named: str, action: str
) -> None:
    """A stored conversation cannot be half-sent.

    ``strip`` removes tool DEFINITIONS, which the SDK can see. A reference to a
    server-side object whose tools it cannot see can only be sent whole or not
    at all, and removing it changes what the call means — so it is refused
    under both instructions.
    """
    wire, cloud = _Wire(), _Cloud(constraints={"tools": action})
    client = consent.wrap(_openai(wire), consent_client=cloud)
    kwargs: dict[str, Any] = {"model": "gpt-4o-mini", "input": "hello", **extra}

    refused = _refused_after_authorize(
        lambda: client.responses.create(**kwargs, user_id=_USER), cloud, wire
    )
    assert named in str(refused)


def test_strip_still_removes_legacy_function_definitions() -> None:
    wire, cloud = _Wire(), _Cloud(constraints={"tools": "strip"})
    client = consent.wrap(_openai(wire), consent_client=cloud)

    client.chat.completions.create(
        model="gpt-4o-mini",
        messages=_MESSAGES,
        functions=[{"name": "f"}],
        user_id=_USER,
    )

    assert "functions" not in wire.body()


@pytest.mark.parametrize(
    "extra",
    [
        {"tools": []},
        {"functions": []},
        {"web_search_options": openai.omit},
    ],
    ids=["empty-tools", "empty-functions", "omitted-enabler"],
)
def test_an_empty_or_omitted_tool_field_carries_no_tool(extra: dict[str, Any]) -> None:
    """``tools=[]`` is "no tools", in both SDKs; a sentinel is "not given"."""
    wire, cloud = _Wire(), _Cloud(constraints={"tools": "refuse"})
    client = consent.wrap(_openai(wire), consent_client=cloud)

    client.chat.completions.create(
        model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER, **extra
    )

    assert len(wire.requests) == 1


def test_responses_input_without_tool_items_is_not_refused() -> None:
    wire, cloud = _Wire(), _Cloud(constraints={"tools": "refuse"})
    client = consent.wrap(_openai(wire), consent_client=cloud)

    client.responses.create(
        model="gpt-4o-mini",
        input=[{"type": "message", "role": "user", "content": "hello"}],
        user_id=_USER,
    )
    client.responses.create(model="gpt-4o-mini", input="hello", user_id=_USER)
    # A message may omit its type too; naming no stored item, it references none.
    client.responses.create(
        model="gpt-4o-mini",
        input=[{"role": "user", "content": "hello"}],
        user_id=_USER,
    )

    assert len(wire.requests) == 3


@pytest.mark.parametrize(
    "config",
    [{"cached_content": "cachedContents/abc"}, {"cachedContent": "cachedContents/abc"}],
    ids=["snake_case", "camelCase"],
)
def test_gemini_cached_content_is_refused(config: dict[str, Any]) -> None:
    wire, cloud = _Wire(), _Cloud(constraints={"tools": "refuse"})
    client = consent.wrap(_gemini(wire), consent_client=cloud)

    refused = _refused_after_authorize(
        lambda: client.models.generate_content(
            model="gemini-2.0-flash", contents="hello", config=config, user_id=_USER
        ),
        cloud,
        wire,
    )
    assert "cached_content" in str(refused)


@pytest.mark.parametrize(
    "site",
    ["agents.complete", "agents.stream", "beta.conversations.start"],
)
def test_mistral_agents_are_refused_when_tools_are(site: str) -> None:
    """An agent carries its own tools, which the SDK cannot see."""
    wire, cloud = _Wire(), _Cloud(constraints={"tools": "refuse"})
    client = consent.wrap(_mistral(wire), consent_client=cloud)

    def _call() -> None:
        if site == "agents.complete":
            client.agents.complete(agent_id="ag_1", messages=_MESSAGES, user_id=_USER)
        elif site == "agents.stream":
            _drain(
                client.agents.stream(agent_id="ag_1", messages=_MESSAGES, user_id=_USER)
            )
        else:
            client.beta.conversations.start(
                inputs="hello", agent_id="ag_1", user_id=_USER
            )

    refused = _refused_after_authorize(_call, cloud, wire)
    assert "agent_id" in str(refused)


# ── D3: a header that picks the credential, or forges the token ─────────────


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer sk-test-other"},
        {"authorization": "Bearer sk-test-other"},
        {"AUTHORIZATION": "Bearer sk-test-other"},
        {"OpenAI-Project": "proj_other"},
        {"openai-organization": "org_other"},
        {"api-key": "sk-test-other"},
        {"X-AI-Consent-Token": "act.forged"},
        {"x-ai-consent-token": "act.forged"},
    ],
    ids=[
        "Authorization",
        "authorization",
        "AUTHORIZATION",
        "OpenAI-Project",
        "openai-organization",
        "api-key",
        "token",
        "token-lowercase",
    ],
)
@pytest.mark.parametrize("routed", [False, True], ids=["unrouted", "routed"])
def test_openai_reserved_headers_are_refused(
    headers: dict[str, str], routed: bool
) -> None:
    wire = _Wire()
    cloud = _Cloud(posture="zero_retention" if routed else None)
    credentials = (
        {"default": "proj_default", "zero_retention": "proj_zdr"} if routed else None
    )
    client = consent.wrap(_openai(wire), consent_client=cloud, credentials=credentials)

    refused = _refused_before_authorize(
        lambda: client.chat.completions.create(
            model="gpt-4o-mini",
            messages=_MESSAGES,
            user_id=_USER,
            extra_headers=headers,
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )
    (name, value), *_ = headers.items()
    assert name in str(refused)
    assert value not in str(refused), "a header value may be a secret"


@pytest.mark.parametrize(
    "headers",
    [
        httpx.Headers({"Authorization": "Bearer sk-test-other"}),
        [("Authorization", "Bearer sk-test-other")],
        {b"Authorization": b"Bearer sk-test-other"},
    ],
    ids=["httpx.Headers", "list-of-pairs", "bytes-names"],
)
def test_reserved_headers_are_found_in_every_shape_of_header_map(headers: Any) -> None:
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_openai(wire), consent_client=cloud)

    _refused_before_authorize(
        lambda: client.chat.completions.create(
            model="gpt-4o-mini",
            messages=_MESSAGES,
            user_id=_USER,
            extra_headers=headers,
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )


@pytest.mark.parametrize(
    "headers", [object(), ["Au", "th"]], ids=["object", "string-pairs"]
)
def test_a_header_map_the_sdk_cannot_read_is_refused(headers: Any) -> None:
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_openai(wire), consent_client=cloud)

    _refused_before_authorize(
        lambda: client.chat.completions.create(
            model="gpt-4o-mini",
            messages=_MESSAGES,
            user_id=_USER,
            extra_headers=headers,
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Api-Key": "sk-ant-test-default"},
        {"x-api-key": "sk-ant-test-default"},
        "omit-then-bearer",
        {"anthropic-workspace-id": "wrkspc_test_other"},
        {"Anthropic-User-Profile-Id": "uprof_test_other"},
    ],
    ids=[
        "X-Api-Key",
        "x-api-key",
        "omit-and-Authorization",
        "workspace",
        "user-profile",
    ],
)
@pytest.mark.parametrize("routed", [False, True], ids=["unrouted", "routed"])
def test_anthropic_reserved_headers_are_refused(headers: Any, routed: bool) -> None:
    if headers == "omit-then-bearer":
        headers = {"X-Api-Key": anthropic.omit, "Authorization": "Bearer sk-ant-other"}
    wire = _Wire()
    cloud = _Cloud(posture="no_training" if routed else None)
    credentials = (
        {"no_training": _anthropic(wire, "sk-ant-test-routed")} if routed else None
    )
    client = consent.wrap(
        _anthropic(wire), consent_client=cloud, credentials=credentials
    )

    _refused_before_authorize(
        lambda: client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=16,
            messages=_MESSAGES,
            user_id=_USER,
            extra_headers=headers,
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )


@pytest.mark.parametrize(
    ("argument", "header"),
    [
        ({"workspace_id": "wrkspc_test_other"}, "anthropic-workspace-id"),
        ({"user_profile_id": "uprof_test_other"}, "anthropic-user-profile-id"),
    ],
    ids=["workspace_id", "user_profile_id"],
)
@pytest.mark.parametrize("route", ["create", "parse", "stream"])
def test_anthropic_arguments_sent_as_headers_are_refused(
    argument: dict[str, str], header: str, route: str
) -> None:
    """Named like body fields, sent as headers: outside the hash, and routing.

    The first half shows where the library really puts them, so the refusal
    below is not of something that would have been hashed anyway.
    """
    kwargs: dict[str, Any] = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 16,
        "messages": _MESSAGES,
    }
    raw_wire = _Wire()
    _anthropic(raw_wire).messages.create(**kwargs, **argument)
    assert raw_wire.requests[0].headers[header] == next(iter(argument.values()))
    assert not set(argument) & set(raw_wire.body())

    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_anthropic(wire), consent_client=cloud)
    method = getattr(client.messages, route)

    refused = _refused_before_authorize(
        lambda: (
            _drain_manager(method(**kwargs, **argument, user_id=_USER))
            if route == "stream"
            else method(**kwargs, **argument, user_id=_USER)
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )
    assert f"`{next(iter(argument))}`" in str(refused)
    assert "_test_other" not in str(refused)


@pytest.mark.parametrize("site", ["chat", "responses"])
def test_openai_prompt_cache_options_are_refused(site: str) -> None:
    """A cache lifetime the forced ``prompt_cache_retention`` does not bound."""
    wire, cloud = _Wire(), _Cloud(forced={"prompt_cache_retention": "in_memory"})
    client = consent.wrap(_openai(wire), consent_client=cloud)
    options = {"ttl": "12h"}

    refused = _refused_before_authorize(
        lambda: (
            client.chat.completions.create(
                model="gpt-5.6",
                messages=_MESSAGES,
                prompt_cache_options=options,
                user_id=_USER,
            )
            if site == "chat"
            else client.responses.create(
                model="gpt-5.6",
                input="hello",
                prompt_cache_options=options,
                user_id=_USER,
            )
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )
    assert "`prompt_cache_options`" in str(refused)


@pytest.mark.parametrize(
    "headers",
    [
        {"x-goog-api-key": "AIza-test-other"},
        {"X-Goog-Api-Key": "AIza-test-other"},
        {"Authorization": "Bearer ya29.other"},
        {"x-goog-user-project": "other-project"},
        {"X-AI-Consent-Token": "act.forged"},
    ],
    ids=["x-goog-api-key", "X-Goog-Api-Key", "Authorization", "user-project", "token"],
)
@pytest.mark.parametrize("routed", [False, True], ids=["unrouted", "routed"])
def test_gemini_reserved_headers_are_refused(
    headers: dict[str, str], routed: bool
) -> None:
    wire = _Wire()
    cloud = _Cloud(posture="no_training" if routed else None)
    credentials = {"no_training": _gemini(wire, "AIza-test-routed")} if routed else None
    client = consent.wrap(_gemini(wire), consent_client=cloud, credentials=credentials)

    _refused_before_authorize(
        lambda: client.models.generate_content(
            model="gemini-2.0-flash",
            contents="hello",
            config={"http_options": {"headers": headers}},
            user_id=_USER,
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )


@pytest.mark.parametrize(
    "headers",
    [{"Authorization": "Bearer mk-test-other"}, {"authorization": "Bearer mk-other"}],
    ids=["Authorization", "authorization"],
)
@pytest.mark.parametrize("routed", [False, True], ids=["unrouted", "routed"])
def test_mistral_reserved_headers_are_refused(
    headers: dict[str, str], routed: bool
) -> None:
    wire = _Wire()
    cloud = _Cloud(posture="no_training" if routed else None)
    credentials = {"no_training": _mistral(wire, "mk-test-routed")} if routed else None
    client = consent.wrap(_mistral(wire), consent_client=cloud, credentials=credentials)

    _refused_before_authorize(
        lambda: client.chat.complete(
            model="mistral-small-latest",
            messages=_MESSAGES,
            user_id=_USER,
            http_headers=headers,
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )


# ── D4: a call sent somewhere else ───────────────────────────────────────────


_MISTRAL_SITES = [
    "chat.complete",
    "chat.stream",
    "fim.complete",
    "fim.stream",
    "agents.complete",
    "agents.stream",
    "beta.conversations.start",
]


def _mistral_call(client: Any, site: str, **extra: Any) -> None:
    if site.startswith("chat."):
        method = getattr(client.chat, site.split(".")[1])
        result = method(model="mistral-small-latest", messages=_MESSAGES, **extra)
    elif site.startswith("fim."):
        method = getattr(client.fim, site.split(".")[1])
        result = method(model="codestral-latest", prompt="def f():", **extra)
    elif site.startswith("agents."):
        method = getattr(client.agents, site.split(".")[1])
        result = method(agent_id="ag_1", messages=_MESSAGES, **extra)
    else:
        result = client.beta.conversations.start(
            inputs="hello", model="mistral-small-latest", **extra
        )
    if site.endswith("stream"):
        _drain(result)


@pytest.mark.parametrize("site", _MISTRAL_SITES)
def test_mistral_server_url_is_refused_on_every_call_site(site: str) -> None:
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_mistral(wire), consent_client=cloud)

    refused = _refused_before_authorize(
        lambda: _mistral_call(client, site, user_id=_USER, server_url=_ATTACKER),
        cloud,
        wire,
        UnenforceableOptionError,
    )
    assert "server_url" in str(refused)
    assert "attacker" not in str(refused)


@pytest.mark.parametrize(
    "http_options",
    [
        {"base_url": "https://collector.attacker.example/"},
        {"api_version": "v1beta/models/gemini-2.5-pro:generateContent#"},
        {"base_url_resource_scope": "COLLECTION"},
        {"client_args": {"base_url": "https://collector.attacker.example"}},
        {"async_client_args": {"base_url": "https://collector.attacker.example"}},
        "httpx_client",
        {"baseUrl": "https://collector.attacker.example/"},
    ],
    ids=[
        "base_url",
        "api_version-path-injection",
        "base_url_resource_scope",
        "client_args",
        "async_client_args",
        "httpx_client",
        "camelCase-baseUrl",
    ],
)
def test_gemini_destination_options_are_refused(http_options: Any) -> None:
    wire, cloud = _Wire(), _Cloud()
    if http_options == "httpx_client":
        http_options = {"httpx_client": httpx.Client(transport=wire.transport())}
    client = consent.wrap(_gemini(wire), consent_client=cloud)

    _refused_before_authorize(
        lambda: client.models.generate_content(
            model="gemini-2.0-flash",
            contents="hello",
            config={"http_options": http_options},
            user_id=_USER,
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )


@pytest.mark.parametrize("derive", ["with_options", "copy"])
@pytest.mark.parametrize("maker", [_openai, _deepseek], ids=["openai", "deepseek"])
def test_a_derived_client_sent_elsewhere_is_refused_at_derivation(
    derive: str, maker: Any
) -> None:
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(maker(wire), consent_client=cloud)

    with pytest.raises(UnsupportedProviderError):
        getattr(client, derive)(base_url=_ATTACKER)

    assert cloud.calls == []
    assert wire.requests == []


def test_a_deepseek_client_derived_onto_openai_is_refused() -> None:
    """The provider name tells the cloud whose rules apply: it must not go stale."""
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_deepseek(wire), consent_client=cloud)

    with pytest.raises(UnsupportedProviderError) as caught:
        client.with_options(base_url="https://api.openai.com/v1")

    assert "deepseek" in str(caught.value)


def test_a_derived_client_is_re_identified_even_when_its_origin_reads_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two checks, not one: the origin a client reports is not its identity.

    Today a provider change implies a host change, so the origin check alone
    would catch it. That is a fact about today's detection, not a guarantee —
    so the provider is re-detected on its own, and this pins it by making the
    origin agree while the provider does not.
    """
    from idside import _destination

    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_deepseek(wire), consent_client=cloud)
    monkeypatch.setattr(
        _destination,
        "client_origin",
        lambda _client: _destination.Origin("https", "api.deepseek.com", 443),
    )

    with pytest.raises(UnsupportedProviderError):
        client.with_options(base_url="https://api.openai.com/v1")


def test_a_derived_client_may_not_add_a_reserved_default_header() -> None:
    """The duplicate header case-sensitive merging used to put on the wire."""
    wire, cloud = _Wire(), _Cloud(posture="zero_retention")
    client = consent.wrap(
        _openai(wire),
        consent_client=cloud,
        credentials={"default": "proj_default", "zero_retention": "proj_zdr"},
    )

    with pytest.raises(CredentialRoutingError) as caught:
        client.with_options(default_headers={"openai-project": "proj_default"})

    assert "openai-project" in str(caught.value)
    assert "proj_default" not in str(caught.value)
    assert wire.requests == []


@pytest.mark.parametrize(
    "derive",
    [
        lambda client: client.with_options(set_default_headers={"X-Api-Key": "k"}),
        lambda client: client.copy(default_headers={b"Authorization": "Bearer k"}),
        lambda client: client.with_options(default_headers=object()),
        lambda client: client.with_options(default_headers=["Au", "th"]),
        lambda client: client.with_options(set_default_headers={"X-Trace": "1"}),
    ],
    ids=[
        "set-reserved",
        "bytes-reserved",
        "unreadable",
        "string-pairs",
        "drops-inherited-reserved",
    ],
)
def test_derived_default_headers_may_not_touch_a_reserved_header(derive: Any) -> None:
    """Adding, replacing or dropping one: each changes the credential in use."""
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(
        anthropic.Anthropic(
            api_key="sk-ant-test-default",
            default_headers={"X-Api-Key": "sk-ant-test-default"},
            http_client=wire.client(anthropic),
        ),
        consent_client=cloud,
    )

    with pytest.raises(CredentialRoutingError):
        derive(client)

    assert wire.requests == []


@pytest.mark.parametrize(
    ("maker", "options"),
    [
        (_openai, {"api_key": "sk-test-other"}),
        (_openai, {"admin_api_key": "sk-admin-other"}),
        (_openai, {"organization": "org_other"}),
        (_openai, {"project": "proj_other"}),
        (_anthropic, {"api_key": "sk-ant-test-other"}),
        (_anthropic, {"auth_token": "tok-other"}),
        (_anthropic, {"profile": "medhelp-staging"}),
    ],
    ids=[
        "openai-api_key",
        "openai-admin_api_key",
        "openai-organization",
        "openai-project",
        "anthropic-api_key",
        "anthropic-auth_token",
        "anthropic-profile",
    ],
)
def test_a_derived_client_may_not_change_its_credential(
    maker: Any, options: dict[str, Any]
) -> None:
    """Which credential carries a call is routing's to decide, not a derivation's."""
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(maker(wire), consent_client=cloud)

    with pytest.raises(CredentialRoutingError) as caught:
        client.with_options(**options)

    (name, value), *_ = options.items()
    assert name in str(caught.value)
    assert "credentials=" in str(caught.value)
    assert value not in str(caught.value)
    assert cloud.calls == [] and wire.requests == []


def _a_middleware(request: Any, call_next: Any) -> Any:
    return call_next(request)


#: What `openai>=3` takes as `provider=`: a third-party API that configures the
#: client's authentication and base URL. Refused on its arguments, so its shape
#: does not matter here.
_A_PROVIDER = object()


@pytest.mark.parametrize(
    ("maker", "derive"),
    [
        (_openai, lambda c: c.with_options(http_client=httpx.Client())),
        (_openai, lambda c: c.copy(default_query={"api-version": "2099-01-01"})),
        (_openai, lambda c: c.with_options(set_default_query={})),
        (_openai, lambda c: c.with_options(websocket_base_url="wss://relay.example")),
        (_openai, lambda c: c.with_options(_extra_kwargs={"base_url": _ATTACKER})),
        (_openai, lambda c: c.with_options(_enforce_credentials=False)),
        (_openai, lambda c: c.with_options(future_option=1)),
        (_openai, lambda c: c.with_options(data_residency="eu")),
        (_openai, lambda c: c.copy(provider=_A_PROVIDER)),
        (
            _anthropic,
            lambda c: c.with_options(http_client=_http_of(anthropic).Client()),
        ),
        (_anthropic, lambda c: c.with_options(middleware=[_a_middleware])),
        (_anthropic, lambda c: c.with_middleware(_a_middleware)),
        (_anthropic, lambda c: c.copy(default_query={"beta": "true"})),
    ],
    ids=[
        "openai-http_client",
        "openai-default_query",
        "openai-set_default_query",
        "openai-websocket_base_url",
        "openai-_extra_kwargs",
        "openai-_enforce_credentials",
        "openai-unknown",
        "openai-data_residency",
        "openai-provider",
        "anthropic-http_client",
        "anthropic-middleware",
        "anthropic-with_middleware",
        "anthropic-default_query",
    ],
)
def test_a_derived_client_may_not_change_how_or_where_it_sends(
    maker: Any, derive: Any
) -> None:
    """A transport, a query or a middleware of its own: wrap a new client instead."""
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(maker(wire), consent_client=cloud)

    with pytest.raises(UnsupportedProviderError) as caught:
        derive(client)

    assert "consent.wrap" in str(caught.value)
    assert "2099" not in str(caught.value)
    assert cloud.calls == [] and wire.requests == []


def test_with_middleware_adding_nothing_derives_an_enforced_client() -> None:
    """A derivation that changes nothing is not a derivation to refuse.

    ``with_middleware()`` with no middleware at all is a call a developer can
    write, and the library answers it by asking its own ``copy()`` for the
    middleware the client already runs. That inner ``copy(middleware=…)`` is
    this SDK's patched one, so the derivation was refused — a call that changes
    nothing, stopped on the strength of an argument the developer never wrote.
    """
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_anthropic(wire), consent_client=cloud)

    derived = client.with_middleware()
    derived.messages.create(
        model="claude-sonnet-4-5", max_tokens=16, messages=_MESSAGES, user_id=_USER
    )

    assert len(cloud.calls) == 1, "the derived client is still enforced"
    assert wire.requests[-1].headers["x-ai-consent-token"] == "act.test.1"
    with pytest.raises(UnenforcedCallError):
        # And the net came with it: an endpoint no descriptor declares.
        derived.messages.count_tokens(model="claude-sonnet-4-5", messages=_MESSAGES)


def test_a_refused_derivation_names_the_method_the_developer_called() -> None:
    """An error naming ``copy`` sends the reader to a line that does not exist.

    ``with_middleware(m)`` reached the refusal through the library's own
    ``copy()``, and said so. Both are still refused — middleware is code
    running between the request and the wire — but each names itself.
    """
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_anthropic(wire), consent_client=cloud)

    with pytest.raises(UnsupportedProviderError) as from_helper:
        client.with_middleware(_a_middleware)
    with pytest.raises(UnsupportedProviderError) as from_copy:
        client.copy(middleware=[_a_middleware])

    assert "`with_middleware(…)`" in str(from_helper.value)
    assert "copy(" not in str(from_helper.value)
    assert "`copy(middleware=…)`" in str(from_copy.value)
    assert cloud.calls == [] and wire.requests == []


def test_harmless_derivation_options_still_derive_an_enforced_client() -> None:
    wire, cloud = _Wire(), _Cloud(forced={"store": False})
    client = consent.wrap(_openai(wire), consent_client=cloud)

    derived = client.with_options(
        timeout=5.0,
        max_retries=1,
        webhook_secret="whsec_test",
        base_url="https://api.openai.com/v1/",
        default_headers={"X-Trace": "1"},
        api_key=None,
    )
    derived.chat.completions.create(
        model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER
    )
    anthropic_wire = _Wire()
    anthropic_client = consent.wrap(_anthropic(anthropic_wire), consent_client=_Cloud())
    anthropic_client.copy(
        webhook_key="whk_test", timeout=5.0, open_telemetry=False
    ).messages.create(
        model="claude-sonnet-4-5", max_tokens=16, messages=_MESSAGES, user_id=_USER
    )

    assert wire.body()["store"] is False
    assert wire.requests[-1].headers["x-trace"] == "1"
    assert len(anthropic_wire.requests) == 1


@pytest.mark.parametrize("spelling", ["openai-project", "OpenAI-Project"])
def test_a_routing_header_among_the_default_headers_is_refused_at_wrap(
    spelling: str,
) -> None:
    """Routing must be the only thing choosing the project, in any spelling.

    Merged case-sensitively, ``openai-project`` set at construction went out
    beside the routed ``OpenAI-Project``: two projects on one request, and the
    provider left to pick.
    """
    wire = _Wire()
    raw = _openai(wire, default_headers={spelling: "proj_default"})

    with pytest.raises(CredentialRoutingError) as caught:
        consent.wrap(
            raw,
            consent_client=_Cloud(posture="zero_retention"),
            credentials={"default": "proj_default", "zero_retention": "proj_zdr"},
        )

    assert spelling in str(caught.value)
    assert "proj_default" not in str(caught.value)
    assert not hasattr(raw.chat.completions.create, "__idside_consent_enforced__")


def test_a_default_header_routing_does_not_set_is_left_to_the_developer() -> None:
    """Only the header routing writes can collide with it; the rest is config."""
    wire, cloud = _Wire(), _Cloud(posture="zero_retention")
    client = consent.wrap(
        _openai(wire, default_headers={"OpenAI-Organization": "org_medhelp"}),
        consent_client=cloud,
        credentials={"default": "proj_default", "zero_retention": "proj_zdr"},
    )

    client.chat.completions.create(
        model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER
    )

    assert wire.requests[-1].headers.get_list("openai-project") == ["proj_zdr"]
    assert wire.requests[-1].headers["openai-organization"] == "org_medhelp"


def test_a_derived_client_keeps_the_reserved_headers_it_was_built_with() -> None:
    """Configuration made at construction is the developer's, and inherited."""
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(
        _openai(wire, default_headers={"OpenAI-Project": "proj_default"}),
        consent_client=cloud,
    )

    derived = client.with_options(max_retries=0, default_headers={"X-Trace": "1"})
    derived.chat.completions.create(
        model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER
    )

    assert wire.requests[-1].headers["openai-project"] == "proj_default"
    assert wire.requests[-1].headers["x-trace"] == "1"


def test_a_base_url_set_after_wrap_is_refused_by_the_net() -> None:
    """A setter is not an option the SDK sees, so the transport checks the origin."""
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_openai(wire), consent_client=cloud)
    client.base_url = _ATTACKER

    with pytest.raises(UnenforcedCallError):
        client.chat.completions.create(
            model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER
        )

    assert wire.requests == []


# ── D5: a redirect that re-sends the request elsewhere ──────────────────────


@pytest.mark.parametrize("status", [307, 308])
def test_an_enforced_request_does_not_follow_a_redirect(status: int) -> None:
    sent: list[httpx.Request] = []

    def _redirecting(request: httpx.Request) -> httpx.Response:
        request.read()
        sent.append(request)
        return httpx.Response(status, headers={"location": _ATTACKER + "/chat"})

    cloud = _Cloud()
    client = consent.wrap(
        openai.OpenAI(
            api_key="sk-test-default",
            max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(_redirecting), follow_redirects=True
            ),
        ),
        consent_client=cloud,
    )

    with pytest.raises(openai.APIStatusError) as caught:
        client.chat.completions.create(
            model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER
        )

    assert caught.value.status_code == status
    assert [request.url.host for request in sent] == ["api.openai.com"]


def test_an_async_enforced_request_does_not_follow_a_redirect() -> None:
    sent: list[str] = []

    def _redirecting(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.host)
        return httpx.Response(307, headers={"location": _ATTACKER + "/chat"})

    client = consent.wrap(
        openai.AsyncOpenAI(
            api_key="sk-test-default",
            max_retries=0,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(_redirecting), follow_redirects=True
            ),
        ),
        consent_client=_AsyncCloud(),
    )

    with pytest.raises(openai.APIStatusError):
        asyncio.run(
            client.chat.completions.create(
                model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER
            )
        )

    assert sent == ["api.openai.com"]


# ── D6: a client named in `credentials` sends what is routed to it ──────────
#
# A routed call goes out on the alternative client, not on the wrapped one, so
# everything the wrapped client is held to — a net on its transport, an origin
# frozen at wrap(), no redirect followed — has to hold there too, or the posture
# a user's answer requires is the one route nothing guards. Not the wrapped
# client's origin, though: a residency posture may live on another host.

_REGIONAL = "https://eu.api.openai.com/v1"


def _regional_openai(http_client: Any) -> Any:
    return openai.OpenAI(
        api_key="sk-test-regional",
        base_url=_REGIONAL,
        max_retries=0,
        http_client=http_client,
    )


def _chat(client: Any) -> Any:
    return client.chat.completions.create(
        model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER
    )


def _message(client: Any) -> Any:
    return client.messages.create(
        model="claude-sonnet-4-5", max_tokens=16, messages=_MESSAGES, user_id=_USER
    )


def test_a_call_routed_to_a_regional_host_goes_out_there() -> None:
    wire, cloud = _Wire(), _Cloud(posture="eu_residency")
    client = consent.wrap(
        _openai(wire),
        consent_client=cloud,
        credentials={
            "eu_residency": _regional_openai(httpx.Client(transport=wire.transport()))
        },
    )

    _chat(client)

    (request,) = wire.requests
    assert request.url.host == "eu.api.openai.com"
    assert request.headers["authorization"] == "Bearer sk-test-regional"
    assert request.headers["x-ai-consent-token"] == "act.test.1"


def test_an_async_call_routed_to_a_regional_host_goes_out_there() -> None:
    wire, cloud = _Wire(), _AsyncCloud(posture="eu_residency")
    regional = openai.AsyncOpenAI(
        api_key="sk-test-regional",
        base_url=_REGIONAL,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=wire.transport()),
    )
    client = consent.wrap(
        _async_openai(wire),
        consent_client=cloud,
        credentials={"eu_residency": regional},
    )

    asyncio.run(
        client.chat.completions.create(
            model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER
        )
    )

    assert [request.url.host for request in wire.requests] == ["eu.api.openai.com"]


@pytest.mark.parametrize(
    ("maker", "alternative", "call"),
    [
        (
            _openai,
            lambda wire: _regional_openai(httpx.Client(transport=wire.transport())),
            _chat,
        ),
        (_anthropic, lambda wire: _anthropic(wire, "sk-ant-test-routed"), _message),
    ],
    ids=["openai", "anthropic"],
)
def test_a_routed_client_moved_after_wrap_is_refused_at_the_transport(
    maker: Any, alternative: Any, call: Any
) -> None:
    """A setter is not an option the SDK sees, on the routed client either."""
    wire, cloud = _Wire(), _Cloud(posture="no_training")
    routed = alternative(wire)
    client = consent.wrap(
        maker(wire), consent_client=cloud, credentials={"no_training": routed}
    )
    routed.base_url = _ATTACKER

    with pytest.raises(UnenforcedCallError):
        call(client)

    assert len(cloud.calls) == 1, "the refusal belongs to the transport"
    assert wire.requests == []


def test_a_routed_call_does_not_follow_a_redirect() -> None:
    sent: list[str] = []

    def _redirecting(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.host)
        return httpx.Response(307, headers={"location": _ATTACKER + "/chat"})

    wire = _Wire()
    client = consent.wrap(
        _openai(wire),
        consent_client=_Cloud(posture="eu_residency"),
        credentials={
            "eu_residency": _regional_openai(
                httpx.Client(
                    transport=httpx.MockTransport(_redirecting), follow_redirects=True
                )
            )
        },
    )

    with pytest.raises(openai.APIStatusError) as caught:
        _chat(client)

    assert caught.value.status_code == 307
    assert sent == ["eu.api.openai.com"]
    assert wire.requests == []


def test_a_call_routed_back_to_the_wrapped_client_is_authorized_once() -> None:
    """``{"default": client}`` names the wrapped client's own key.

    Its ``stream()`` sends through the patched ``create``. That nested hop is
    recognised only by a permit minted for the wrapped client's own net: minted
    for any other, the hop would be authorized a second time.
    """
    wire, cloud = _Wire(), _Cloud(posture="default")
    raw = _openai(wire)
    client = consent.wrap(raw, consent_client=cloud, credentials={"default": raw})

    _drain_manager(
        client.chat.completions.stream(
            model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER
        )
    )

    assert len(cloud.calls) == 1
    assert len(wire.requests) == 1


def test_a_transport_shared_with_a_routed_client_holds_each_to_its_origin() -> None:
    """``copy()`` shares the HTTP client, and still makes two destinations.

    The routed client's origin is not a second origin the wrapped client may
    use: each client's calls are held to its own, even on one transport.
    """
    wire, cloud = _Wire(), _Cloud()
    raw = _openai(wire)
    alternative = raw.copy(api_key="sk-test-regional", base_url=_REGIONAL)
    client = consent.wrap(
        raw, consent_client=cloud, credentials={"eu_residency": alternative}
    )
    client.base_url = _REGIONAL

    with pytest.raises(UnenforcedCallError):
        _chat(client)

    assert wire.requests == []


def test_a_derived_client_holds_its_routed_clients_to_the_origin_frozen_at_wrap() -> (
    None
):
    wire, cloud = _Wire(), _Cloud(posture="eu_residency")
    alternative = _regional_openai(httpx.Client(transport=wire.transport()))
    client = consent.wrap(
        _openai(wire), consent_client=cloud, credentials={"eu_residency": alternative}
    )

    _chat(client.with_options(timeout=5))
    alternative.base_url = _ATTACKER
    derived = client.with_options(timeout=5)

    with pytest.raises(UnenforcedCallError):
        _chat(derived)

    assert [request.url.host for request in wire.requests] == ["eu.api.openai.com"]


def test_a_credentials_mapping_changed_after_wrap_reroutes_nothing() -> None:
    """Read at wrap(): a client derived later is routed as its parent was."""
    wire, cloud = _Wire(), _Cloud(posture="eu_residency")
    credentials = {
        "eu_residency": _regional_openai(httpx.Client(transport=wire.transport()))
    }
    client = consent.wrap(_openai(wire), consent_client=cloud, credentials=credentials)
    credentials["eu_residency"] = openai.OpenAI(
        api_key="sk-test-other",
        base_url=_ATTACKER,
        max_retries=0,
        http_client=httpx.Client(transport=wire.transport()),
    )

    _chat(client.with_options(timeout=5))

    assert [request.url.host for request in wire.requests] == ["eu.api.openai.com"]


def test_a_client_named_in_credentials_is_no_longer_an_unwrapped_client() -> None:
    """It holds a credential routed calls go out on; left open, it is the way round.

    Traffic that is not inference belongs on a client named nowhere in
    ``wrap()``, exactly as for the wrapped client itself.
    """
    wire = _Wire()
    alternative = _regional_openai(httpx.Client(transport=wire.transport()))
    consent.wrap(
        _openai(wire),
        consent_client=_Cloud(),
        credentials={"eu_residency": alternative},
    )

    with pytest.raises(UnenforcedCallError):
        alternative.chat.completions.create(model="gpt-4o-mini", messages=_MESSAGES)

    assert wire.requests == []


class _SealedClient(httpx.Client):
    """An HTTP client whose ``send`` cannot be reassigned."""

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "send":
            raise AttributeError("this transport does not take a patch")
        super().__setattr__(name, value)


def _plain_chat(client: Any) -> None:
    client.chat.completions.create(model="gpt-4o-mini", messages=_MESSAGES)


def _plain_generate(client: Any) -> None:
    client.models.generate_content(model="gemini-2.0-flash", contents="hello")


def _routed_without_a_transport(wire: _Wire, _patch: pytest.MonkeyPatch) -> Any:
    alternative = _regional_openai(httpx.Client(transport=wire.transport()))
    del alternative.__dict__["_client"]  # a library version that stopped holding it
    return _openai(wire), alternative, _plain_chat


def _routed_on_a_sealed_transport(wire: _Wire, _patch: pytest.MonkeyPatch) -> Any:
    alternative = _regional_openai(_SealedClient(transport=wire.transport()))
    return _openai(wire), alternative, _plain_chat


def _routed_without_an_origin(wire: _Wire, patch: pytest.MonkeyPatch) -> Any:
    from idside import _destination

    alternative = _regional_openai(httpx.Client(transport=wire.transport()))
    read = _destination.client_origin
    patch.setattr(
        _destination,
        "client_origin",
        lambda client: None if client is alternative else read(client),
    )
    return _openai(wire), alternative, _plain_chat


def _routed_outside_httpx(wire: _Wire, patch: pytest.MonkeyPatch) -> Any:
    alternative = _gemini(wire, "AIza-test-routed")
    patch.setattr(alternative._api_client, "_use_aiohttp", lambda: True)
    return _gemini(wire), alternative, _plain_generate


@pytest.mark.parametrize(
    "build",
    [
        _routed_without_a_transport,
        _routed_on_a_sealed_transport,
        _routed_without_an_origin,
        _routed_outside_httpx,
    ],
    ids=["no-transport", "sealed-transport", "no-origin", "outside-httpx"],
)
def test_a_routed_client_the_net_cannot_cover_is_not_wrapped(
    build: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapped client's criterion, and a refusal that leaves nothing behind.

    Some of these are only found by trying — the wrapped client's net is on by
    then — so the client the application still holds is used afterwards, as it
    was: no call site asks for an identity, no transport refuses the request.
    """
    wire = _Wire()
    raw, alternative, plain_call = build(wire, monkeypatch)

    with pytest.raises(UnsupportedProviderError) as caught:
        consent.wrap(
            raw, consent_client=_Cloud(), credentials={"no_training": alternative}
        )

    assert "'no_training'" in str(caught.value)
    plain_call(raw)
    assert len(wire.requests) == 1


# ── D7: a client whose every route the net cannot see ──────────────────────


def test_a_provider_client_whose_transport_cannot_be_found_is_not_wrapped() -> None:
    wire = _Wire()
    raw = _openai(wire)
    del raw.__dict__["_client"]  # a library version that stopped holding it

    with pytest.raises(UnsupportedProviderError):
        consent.wrap(raw, consent_client=_Cloud())

    assert not hasattr(raw.chat.completions.create, "__idside_consent_enforced__")


@pytest.mark.parametrize(
    "unguarded",
    ["_use_google_auth_sync", "_use_aiohttp", "_use_google_auth_async"],
    ids=["vertex-mtls", "aiohttp", "vertex-mtls-async"],
)
def test_a_gemini_client_sending_outside_httpx_is_not_wrapped(
    unguarded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mTLS on Vertex sends on ``requests``; ``aiohttp`` sends the async half.

    Neither is an HTTP client the net can recognise, so a client in either mode
    has a route out that nothing guards — the case ``"partial"`` exists for.
    """
    raw = _gemini(_Wire())
    monkeypatch.setattr(type(raw._api_client), unguarded, lambda _self: True)

    with pytest.raises(UnsupportedProviderError):
        consent.wrap(raw, consent_client=_Cloud())


#: ``google-genai``'s "NextGen" surfaces: whole Speakeasy-generated SDKs hanging
#: off the same client, reached through a ``property`` and built lazily. No
#: descriptor declares any of them, so every route they open is one the net must
#: be the thing that closes.
_NEXTGEN_SURFACES = ("interactions", "agents", "webhooks")


def _nextgen(client: Any, surface: str) -> Any:
    """One NextGen surface, built the way an application reaches it."""
    import warnings

    with warnings.catch_warnings():
        # `agents` warns that it is experimental. That is the library talking to
        # its user, and it is not this test's subject.
        warnings.simplefilter("ignore", UserWarning)
        return getattr(client, surface)


@pytest.mark.parametrize("surface", _NEXTGEN_SURFACES)
def test_a_gemini_nextgen_surface_sends_on_the_transport_the_net_guards(
    surface: str,
) -> None:
    """The net covers these today, and it covers them by one thread of luck.

    ``build_google_genai_client()`` hands the generated SDK
    ``getattr(api_client, "_httpx_client", None)``, so the NextGen client sends
    on the *same object* the parent does — the object the net patched. Nothing
    in this SDK asks for that, and nothing in ``google-genai`` promises it.

    The day that ``getattr`` yields ``None``, Speakeasy builds an HTTP client of
    its own and these surfaces leave the process with no authorize, no ACT token
    and no ledger entry, silently — which is the shape of the hole the Node SDK
    had on the same family of surfaces. This is the canary for that day, and it
    fails before any call is attempted so the failure names the mechanism rather
    than arriving as a connection error from somewhere else.
    """
    wrapped = consent.wrap(_gemini(_Wire()), consent_client=_Cloud())

    assert (
        _nextgen(wrapped, surface).sdk_configuration.client
        is wrapped._api_client._httpx_client
    ), (
        f"`{surface}` no longer sends on the transport the net guards. The "
        "generated SDK is built with the parent's `_httpx_client`; if that is "
        "gone, it makes its own HTTP client and this whole surface reaches the "
        "provider unenforced. The net needs to be taught to find it."
    )


@pytest.mark.parametrize("before_wrap", [False, True], ids=["lazy", "already-built"])
def test_a_gemini_nextgen_call_is_refused_before_it_reaches_the_provider(
    before_wrap: bool,
) -> None:
    """A surface no descriptor declares: refusal is the right answer, not a gap.

    ``interactions.create`` is a body-bearing POST to ``/v1beta/interactions``
    that goes through none of the patched call sites. Unwrapped it reaches the
    provider; wrapped, the net has to stop it — there is no consent state for an
    endpoint nobody described, so letting it through would be an AI call with no
    proof behind it. The remedy is a second, unwrapped client.

    Both access orders, because the surface is cached on first use: built before
    the wrap it holds the transport that is patched afterwards, built after it
    it is handed the already-patched one. Neither may be the one that escapes.
    """
    wire, cloud = _Wire(), _Cloud()
    raw = _gemini(wire)
    if before_wrap:
        _nextgen(raw, "interactions")
    wrapped = consent.wrap(raw, consent_client=cloud)
    surface = _nextgen(wrapped, "interactions")

    # Checked before the call, and not only for a clearer failure: an
    # unguarded transport here is a real `httpx.Client`, and the call below
    # would leave this machine for `generativelanguage.googleapis.com`.
    assert hasattr(
        surface.sdk_configuration.client.send, "__idside_transport_guarded__"
    ), (
        "this surface sends on a transport the net never patched, so the call "
        "below would reach the provider for real"
    )

    with pytest.raises(UnenforcedCallError):
        surface.create(model="gemini-2.0-flash", input="hello")

    assert cloud.calls == [], "no proof was signed for a call that was refused"
    assert wire.requests == [], "the request must not have left the process"


# ── D8: automatic function calling, one token for several requests ──────────


def _lookup_patient_record(patient_id: str) -> str:
    """A function google-genai would call on the model's behalf."""
    return patient_id


class _CallableTool:
    def __call__(self, city: str) -> str:
        return city


def _mcp_session() -> Any:
    session_type = type("ClientSession", (), {"__module__": "mcp.client.session"})
    return session_type()


@pytest.mark.parametrize(
    "tool",
    [_lookup_patient_record, _CallableTool(), _CallableTool().__call__, "mcp"],
    ids=["function", "callable-object", "bound-method", "mcp-session"],
)
@pytest.mark.parametrize("shape", ["dict", "pydantic"])
def test_gemini_automatic_function_calling_is_refused_whatever_the_consent(
    tool: Any, shape: str
) -> None:
    """Not a question of consent: one ACT token would cover up to ten requests."""
    if tool == "mcp":
        tool = _mcp_session()
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_gemini(wire), consent_client=cloud)
    config: Any = {"tools": [tool]}
    if shape == "pydantic" and not isinstance(tool, _CallableTool) and callable(tool):
        config = genai_types.GenerateContentConfig(tools=[tool])

    refused = _refused_before_authorize(
        lambda: client.models.generate_content(
            model="gemini-2.0-flash", contents="hello", config=config, user_id=_USER
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )
    assert "config.tools" in str(refused)


def test_gemini_declared_functions_are_not_automatic_function_calling() -> None:
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_gemini(wire), consent_client=cloud)

    client.models.generate_content(
        model="gemini-2.0-flash",
        contents="hello",
        config={
            "tools": [{"function_declarations": [{"name": "lookup"}]}],
            "automatic_function_calling": {"disable": True},
        },
        user_id=_USER,
    )

    assert len(wire.requests) == 1


#: A Gemini answer that stopped before the end and says how to resume it.
_GEMINI_CONTINUING = {
    "candidates": [
        {
            "content": {"parts": [{"text": "half of it"}], "role": "model"},
            "finishReason": "CONTINUATION",
            "continuationToken": base64.b64encode(b"opaque-resume-state").decode(),
        }
    ]
}


class _ContinuingWire(_Wire):
    """Answers the first request with ``CONTINUATION``, every later one in full."""

    def transport(self, http: Any = httpx) -> Any:
        def handler(request: Any) -> Any:
            request.read()
            self.requests.append(request)
            if len(self.requests) > 1:
                return _answer(request, http)
            if request.url.path.endswith(":streamGenerateContent"):
                event = b"data: " + json.dumps(_GEMINI_CONTINUING).encode()
                return http.Response(
                    200,
                    content=event + b"\r\n\r\n",
                    headers={"content-type": "text/event-stream"},
                )
            return http.Response(200, json=_GEMINI_CONTINUING)

        return http.MockTransport(handler)


def _generate(client: Any, stream: bool, **kwargs: Any) -> Any:
    if stream:
        return list(
            client.models.generate_content_stream(
                model="gemini-2.0-flash", contents="hello", **kwargs
            )
        )
    return client.models.generate_content(
        model="gemini-2.0-flash", contents="hello", **kwargs
    )


@pytest.mark.parametrize("stream", [False, True], ids=["generate", "stream"])
@pytest.mark.parametrize(
    "config",
    [None, {"temperature": 0.2}, "typed", {"automaticContinuation": False}],
    ids=["no-config", "dict", "typed", "camel-off"],
)
def test_gemini_does_not_resume_on_its_own_under_one_token(
    stream: bool, config: Any
) -> None:
    """google-genai resends a ``CONTINUATION`` answer by itself, token and all.

    Unwrapped, the library follows the continuation token with a second request
    — the first half of this test shows it, so the second is not passing for
    want of a loop. Wrapped, the call is one request under one token, and the
    answer comes back with its ``continuation_token`` for the developer to
    resume with: a new call, authorized and proven like the first.
    """
    turned_off = isinstance(config, dict) and "automaticContinuation" in config
    if config == "typed":
        config = genai_types.GenerateContentConfig(temperature=0.2)
    kwargs = {} if config is None else {"config": config}

    raw_wire = _ContinuingWire()
    _generate(_gemini(raw_wire), stream, **dict(kwargs))
    assert len(raw_wire.requests) == (1 if turned_off else 2), (
        "the library no longer resumes by itself"
    )

    wire, cloud = _ContinuingWire(), _Cloud()
    client = consent.wrap(_gemini(wire), consent_client=cloud)
    _generate(client, stream, user_id=_USER, **kwargs)

    assert len(cloud.calls) == 1
    assert len(wire.requests) == 1


def test_gemini_automatic_continuation_asked_for_is_refused_before_authorize() -> None:
    wire, cloud = _ContinuingWire(), _Cloud()
    client = consent.wrap(_gemini(wire), consent_client=cloud)

    refused = _refused_before_authorize(
        lambda: client.models.generate_content(
            model="gemini-2.0-flash",
            contents="hello",
            config={"automatic_continuation": True},
            user_id=_USER,
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )
    assert "config.automatic_continuation" in str(refused)


# ── what must keep working ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "timeout", [5.0, httpx.Timeout(10.0)], ids=["float", "httpx.Timeout"]
)
def test_openai_timeout_is_harmless(timeout: Any) -> None:
    wire, cloud = _Wire(), _Cloud(forced={"store": False})
    client = consent.wrap(_openai(wire), consent_client=cloud)

    client.chat.completions.create(
        model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER, timeout=timeout
    )

    assert wire.body()["store"] is False


def test_openai_tracing_headers_and_derived_retries_still_work() -> None:
    wire, cloud = _Wire(), _Cloud(forced={"store": False})
    client = consent.wrap(_openai(wire), consent_client=cloud)

    client.with_options(max_retries=2, timeout=5).chat.completions.create(
        model="gpt-4o-mini",
        messages=_MESSAGES,
        user_id=_USER,
        extra_headers={"X-Request-Id": "req_1"},
    )

    assert wire.requests[-1].headers["x-request-id"] == "req_1"
    assert wire.body()["store"] is False


def test_openai_helpers_and_async_still_work() -> None:
    wire, cloud = _Wire(), _Cloud(forced={"store": False})
    completions = consent.wrap(_openai(wire), consent_client=cloud).chat.completions
    kwargs = {"model": "gpt-4o-mini", "messages": _MESSAGES, "user_id": _USER}

    completions.with_raw_response.create(**kwargs)
    _drain_manager(completions.stream(**kwargs))
    async_wire, async_cloud = _Wire(), _AsyncCloud(forced={"store": False})
    async_client = consent.wrap(_async_openai(async_wire), consent_client=async_cloud)
    asyncio.run(async_client.chat.completions.create(**kwargs))

    assert len(wire.requests) == 2
    assert len(async_wire.requests) == 1
    assert all(json.loads(r.content)["store"] is False for r in wire.requests)


def test_openai_header_routing_still_lands() -> None:
    wire, cloud = _Wire(), _Cloud(posture="zero_retention")
    client = consent.wrap(
        _openai(wire),
        consent_client=cloud,
        credentials={"default": "proj_default", "zero_retention": "proj_zdr"},
    )

    client.chat.completions.create(
        model="gpt-4o-mini", messages=_MESSAGES, user_id=_USER
    )

    assert wire.requests[-1].headers.get_list("openai-project") == ["proj_zdr"]


def test_deepseek_is_still_detected_by_its_host() -> None:
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_deepseek(wire), consent_client=cloud)

    client.chat.completions.create(
        model="deepseek-chat", messages=_MESSAGES, user_id=_USER
    )

    assert cloud.calls[0]["provider"] == "deepseek"
    assert wire.requests[-1].url.host == "api.deepseek.com"


def test_anthropic_beta_header_parse_stream_and_routing_still_work() -> None:
    wire = _Wire()
    cloud = _Cloud(posture="no_training")
    client = consent.wrap(
        _anthropic(wire),
        consent_client=cloud,
        credentials={"no_training": _anthropic(wire, "sk-ant-test-routed")},
    )
    kwargs = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 16,
        "messages": _MESSAGES,
        "user_id": _USER,
        "extra_headers": {"anthropic-beta": "prompt-caching-2024-07-31"},
    }

    client.messages.create(**kwargs)
    client.messages.parse(**kwargs)
    _drain_manager(client.messages.stream(**kwargs))

    assert len(wire.requests) == 3
    for request in wire.requests:
        assert request.headers["anthropic-beta"] == "prompt-caching-2024-07-31"
        # `anthropic<2` sent the key as `X-Api-Key`, `anthropic>=2` as a bearer
        # token: either way, the routed key and only it.
        keys = request.headers.get_list("x-api-key") + [
            value.removeprefix("Bearer ")
            for value in request.headers.get_list("authorization")
        ]
        assert keys == ["sk-ant-test-routed"]


def test_mistral_retries_timeout_and_routing_still_work() -> None:
    wire = _Wire()
    cloud = _Cloud(posture="no_training")
    client = consent.wrap(
        _mistral(wire),
        consent_client=cloud,
        credentials={"no_training": _mistral(wire, "mk-test-routed")},
    )

    client.chat.complete(
        model="mistral-small-latest",
        messages=_MESSAGES,
        user_id=_USER,
        retries=mistral_utils.RetryConfig(
            "backoff", mistral_utils.BackoffStrategy(1, 10, 1.5, 100), False
        ),
        timeout_ms=5000,
        http_headers={"X-Trace": "1"},
    )

    assert wire.requests[-1].headers["authorization"] == "Bearer mk-test-routed"
    assert wire.requests[-1].headers["x-trace"] == "1"


@pytest.mark.parametrize(
    "config",
    [
        {
            "http_options": {
                "timeout": 30000,
                "retry_options": {"attempts": 2},
                "headers": {"X-Trace": "1"},
            }
        },
        {"httpOptions": {"timeout": 30000, "headers": {"X-Trace": "1"}}},
        "pydantic",
    ],
    ids=["dict", "camelCase-dict", "pydantic"],
)
def test_gemini_transport_options_still_work(config: Any) -> None:
    if config == "pydantic":
        config = genai_types.GenerateContentConfig(
            http_options=genai_types.HttpOptions(
                retry_options=genai_types.HttpRetryOptions(attempts=2),
                headers={"X-Trace": "1"},
            )
        )
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(_gemini(wire), consent_client=cloud)

    client.models.generate_content(
        model="gemini-2.0-flash", contents="hello", config=config, user_id=_USER
    )
    _drain(
        client.models.generate_content_stream(
            model="gemini-2.0-flash", contents="hello", config=config, user_id=_USER
        )
    )

    assert len(wire.requests) == 2
    for request in wire.requests:
        assert request.headers["x-trace"] == "1"
        assert request.headers["x-ai-consent-token"].startswith("act.test.")


def test_gemini_client_routing_still_lands() -> None:
    wire = _Wire()
    cloud = _Cloud(posture="no_training")
    client = consent.wrap(
        _gemini(wire),
        consent_client=cloud,
        credentials={"no_training": _gemini(wire, "AIza-test-routed")},
    )

    client.models.generate_content(
        model="gemini-2.0-flash", contents="hello", user_id=_USER
    )

    assert wire.requests[-1].headers.get_list("x-goog-api-key") == ["AIza-test-routed"]


# ── the proof of a call that succeeded before does not move ─────────────────


_PINNED_HASHES = {
    # Computed on 2026-09-16 by the SDK as it stood before these refusals.
    "openai-plain": (
        "sha256:ffb904460a38929a4aef4d3b9d09d0f2375d248aa03d3104e83c130a4302d716"
    ),
    "openai-timeout": (
        "sha256:cad2ccb6b93c9e18f2f1f344d9a2d9c50d0815608d794abfb4b63def79837f1b"
    ),
    "anthropic-beta": (
        "sha256:5bc82b4b73f9ac9cdc063c83fc3376afd1b0b17d34e8c7b0dc40d2259cf626f8"
    ),
    "mistral-timeout_ms": (
        "sha256:9e6f6aa2b3bbdf8579221e056f8fff5fee2a001dccc6906306d2fcd1993ba2a8"
    ),
    "gemini-http-dict": (
        "sha256:303a37d63dd200e4a12b451b6051a6d7eb5c01533f7e46d2b394a4d5bf6ada6e"
    ),
    # A TYPED config is hashed through `model_dump()`, which writes every field
    # the installed google-genai declares, `None` included. So this one value
    # follows the library rather than the SDK: google-genai 2.33 and 3.1 added
    # four fields to `GenerateContentConfig` (`store`, `continuation_token`,
    # `automatic_continuation`, `generation_output_config`) and moved it from
    # `sha256:aa1edc8c…5d8b63cd`, its value under 2.11, with this SDK
    # unchanged. Re-pinned on 2026-11-11 against both; a dict config does not
    # move, as `gemini-http-dict` shows.
    "gemini-retry-object": (
        "sha256:fe176f3ec42f4f1c9be58d1db9a3221ae65b7c5c45c4cd0e74910d8017cef997"
    ),
}


def test_the_hash_of_a_call_that_already_succeeded_is_unchanged() -> None:
    from idside.adapters import GeminiAdapter, ProviderAdapter
    from idside.providers import load_descriptors

    descriptors = load_descriptors()
    openai_adapter = ProviderAdapter(descriptors["openai"])
    gemini_adapter = GeminiAdapter(descriptors["gemini"])
    base = {"model": "gpt-4o-mini", "messages": _MESSAGES}

    def _hash(adapter: Any, **kwargs: Any) -> str:
        return str(adapter.extract_request_metadata(**kwargs)["request_hash"])

    assert _hash(openai_adapter, **base) == _PINNED_HASHES["openai-plain"]
    assert (
        _hash(openai_adapter, **base, extra_headers={"X-Request-Id": "req_1"})
        == _PINNED_HASHES["openai-plain"]
    )
    assert (
        _hash(openai_adapter, **base, timeout=5.0) == _PINNED_HASHES["openai-timeout"]
    )
    assert (
        _hash(
            ProviderAdapter(descriptors["anthropic"]),
            model="claude-sonnet-4-5",
            max_tokens=16,
            messages=_MESSAGES,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        == _PINNED_HASHES["anthropic-beta"]
    )
    assert (
        _hash(
            ProviderAdapter(descriptors["mistral"]),
            model="mistral-small-latest",
            messages=_MESSAGES,
            timeout_ms=5000,
        )
        == _PINNED_HASHES["mistral-timeout_ms"]
    )
    assert (
        _hash(
            gemini_adapter,
            model="gemini-2.0-flash",
            contents="hello",
            config={"http_options": {"timeout": 30000, "headers": {"X-Trace": "1"}}},
        )
        == _PINNED_HASHES["gemini-http-dict"]
    )
    assert (
        _hash(
            gemini_adapter,
            model="gemini-2.0-flash",
            contents="hello",
            config=genai_types.GenerateContentConfig(
                http_options=genai_types.HttpOptions(
                    retry_options=genai_types.HttpRetryOptions(attempts=2)
                )
            ),
        )
        == _PINNED_HASHES["gemini-retry-object"]
    )


def test_a_transport_object_that_used_to_break_the_hash_is_left_out_of_it() -> None:
    """``httpx.Timeout`` and ``RetryConfig`` raised; now they are simply not hashed.

    Leaving them out moves no existing hash, because no call carrying one ever
    got as far as having a hash.
    """
    from idside.adapters import ProviderAdapter
    from idside.providers import load_descriptors

    descriptors = load_descriptors()
    openai_adapter = ProviderAdapter(descriptors["openai"])
    mistral_adapter = ProviderAdapter(descriptors["mistral"])
    retries = mistral_utils.RetryConfig(
        "backoff", mistral_utils.BackoffStrategy(1, 10, 1.5, 100), False
    )

    assert (
        openai_adapter.extract_request_metadata(
            model="gpt-4o-mini", messages=_MESSAGES, timeout=httpx.Timeout(10.0)
        )["request_hash"]
        == _PINNED_HASHES["openai-plain"]
    )
    plain = mistral_adapter.extract_request_metadata(
        model="mistral-small-latest", messages=_MESSAGES
    )
    assert (
        mistral_adapter.extract_request_metadata(
            model="mistral-small-latest", messages=_MESSAGES, retries=retries
        )
        == plain
    )


# ── what a client was built to rewrite, found at wrap() ─────────────────────


def _gemini_built_with(wire: _Wire, **http_options: Any) -> Any:
    return genai.Client(
        api_key="AIza-test-default",
        http_options={
            "httpx_client": httpx.Client(transport=wire.transport()),
            **http_options,
        },
    )


def test_a_gemini_client_built_to_rewrite_every_body_is_not_wrapped() -> None:
    """Data, not code: google-genai merges it into every request after the guard."""
    wire = _Wire()
    raw = _gemini_built_with(wire, extra_body={"store": True})

    with pytest.raises(UnsupportedProviderError) as caught:
        consent.wrap(raw, consent_client=_Cloud())

    assert "extra_body" in str(caught.value)
    assert not hasattr(raw.models.generate_content, "__idside_consent_enforced__")


def test_a_credential_client_built_to_rewrite_every_body_is_refused_too() -> None:
    """The routed calls leave on the alternative client, rewrite included."""
    wire = _Wire()
    raw = _gemini(wire)
    alternative = _gemini_built_with(wire, extra_body={"store": True})

    with pytest.raises(UnsupportedProviderError) as caught:
        consent.wrap(
            raw, consent_client=_Cloud(), credentials={"no_training": alternative}
        )

    assert "extra_body" in str(caught.value)
    assert not hasattr(raw.models.generate_content, "__idside_consent_enforced__")


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Goog-Api-Key": "AIza-test-other"},
        {"x-goog-api-key": "AIza-test-other"},
        {"Authorization": "Bearer ya29.other"},
        {"x-goog-user-project": "other-project"},
        {"X-AI-Consent-Token": "act.forged"},
    ],
    ids=["X-Goog-Api-Key", "x-goog-api-key", "Authorization", "user-project", "token"],
)
def test_a_gemini_client_built_with_a_reserved_header_is_not_wrapped(
    headers: dict[str, str],
) -> None:
    wire = _Wire()
    raw = _gemini_built_with(wire, headers=headers)

    with pytest.raises(CredentialRoutingError) as caught:
        consent.wrap(raw, consent_client=_Cloud())

    (name, value), *_ = headers.items()
    assert name in str(caught.value)
    assert value not in str(caught.value)


def test_a_gemini_client_built_with_ordinary_headers_is_wrapped() -> None:
    """The key the library sets from `api_key` is its own, not a caller's header."""
    wire, cloud = _Wire(), _Cloud()
    client = consent.wrap(
        _gemini_built_with(wire, headers={"X-Trace": "1"}, timeout=30000),
        consent_client=cloud,
    )

    client.models.generate_content(
        model="gemini-2.0-flash", contents="hello", user_id=_USER
    )

    assert wire.requests[-1].headers.get_list("x-goog-api-key") == ["AIza-test-default"]
    assert wire.requests[-1].headers["x-trace"] == "1"


# ── a library this SDK has no classification for ───────────────────────────


class _AcmeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "acme_1"}


class _AcmeClient:
    """A client of a provider library this SDK ships no classification for."""

    __module__ = "acme_ai.client"

    def __init__(self, wire: _Wire) -> None:
        self.base_url = "https://api.acme.example/v1"
        self._client = httpx.Client(transport=wire.transport())
        self.chat = type("Chat", (), {})()
        self.chat.completions = _AcmeCompletions()


def _acme_adapter() -> Any:
    from idside.adapters import ProviderAdapter
    from idside.providers import ProviderDescriptor

    return ProviderAdapter(
        ProviderDescriptor.from_dict(
            {
                "name": "acme",
                "client_module": "acme_ai",
                "tools_param": ["tools"],
                "reserved_headers": ["Authorization"],
                "call_sites": [
                    {
                        "id": "chat.completions.create",
                        "path": ["chat", "completions", "create"],
                    }
                ],
            }
        )
    )


@pytest.mark.parametrize(
    "option",
    [
        {"extra_body": {"store": True}},
        {"extra_query": {"api-version": "x"}},
        {"server_url": _ATTACKER},
        {"base_url": _ATTACKER},
        {"http_options": {"base_url": _ATTACKER}},
        {"config": {"http_options": {"extra_body": {}}}},
        {"extra_headers": {"authorization": "Bearer other"}},
    ],
    ids=[
        "extra_body",
        "extra_query",
        "server_url",
        "base_url",
        "http_options",
        "config.http_options",
        "reserved-header",
    ],
)
def test_an_unknown_library_still_has_its_known_offenders_refused(
    option: dict[str, Any],
) -> None:
    """No schema tells body from options there, so no closed list — but not open."""
    wire, cloud = _Wire(), _Cloud()
    client = _AcmeClient(wire)
    wrapped = consent.wrap(client, adapter=_acme_adapter(), consent_client=cloud)

    _refused_before_authorize(
        lambda: wrapped.chat.completions.create(
            model="acme-1", messages=_MESSAGES, user_id=_USER, **option
        ),
        cloud,
        wire,
        UnenforceableOptionError,
    )
    assert client.chat.completions.calls == []


def test_an_unknown_library_passes_what_is_not_a_known_offender() -> None:
    wire, cloud = _Wire(), _Cloud()
    client = _AcmeClient(wire)
    wrapped = consent.wrap(client, adapter=_acme_adapter(), consent_client=cloud)

    wrapped.chat.completions.create(
        model="acme-1",
        messages=_MESSAGES,
        temperature=0.2,
        http_options={"timeout": 5},
        config={"temperature": 1, "http_options": None},
        extra_body=None,
        extra_headers={"X-Trace": "1"},
        user_id=_USER,
    )

    assert len(client.chat.completions.calls) == 1
