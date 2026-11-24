"""Prompt cleaning through the REAL provider libraries: what reaches the wire.

Each provider client is real, its HTTP transport a mock that records the bytes
it would have sent. The cloud is the signing stand-in. What is asserted is the
request body the provider would have received, and the hash the cloud was
asked to sign: the cleaned body, both.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import httpx
import pytest

from _signed_policy import NOW, Cloud, policy_document
from idside import _content_policy, consent
from idside._transport import HttpConsentClient
from idside.adapters import get_adapter_for
from idside.exceptions import ContentBlockedError

POLICY = policy_document(
    detectors={"email": "mask", "phone": "mask", "credit_card": "block"},
    custom=[],
)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_content_policy, "clock", lambda: float(NOW))


def _cloud_client(cloud: Cloud) -> HttpConsentClient:
    return HttpConsentClient(
        "https://cloud.example.test", transport=httpx.MockTransport(cloud.handle)
    )


class _Wire:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.bodies: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json=self.answer)


_CHAT = {
    "id": "c",
    "object": "chat.completion",
    "created": 0,
    "model": "m",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "ok"},
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_ANTHROPIC = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-x",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
_GEMINI = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "ok"}]},
            "finish_reason": "STOP",
        }
    ]
}


def _openai(wire: _Wire, base_url: str | None = None) -> Any:
    openai = pytest.importorskip("openai")
    return openai.OpenAI(
        api_key="sk-test",  # pragma: allowlist secret
        base_url=base_url,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(wire.handle)),
    )


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_openai_compatible_chat(provider: str) -> None:
    cloud, wire = Cloud(policy=POLICY), _Wire(_CHAT)
    client = consent.wrap(
        _openai(wire, "https://api.deepseek.com" if provider == "deepseek" else None),
        consent_client=_cloud_client(cloud),
    )
    client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "user", "content": "Call 06 12 34 56 78 or marie@example.com"}
        ],
        user_id="usr_marie_456",
    )
    (sent,) = wire.bodies
    assert sent["messages"][0]["content"] == "Call [PHONE_1] or [EMAIL_1]"
    (authorize,) = cloud.bodies("/v1/authorize")
    assert authorize["provider"] == provider
    assert authorize["content_policy"]["counts"] == {"email": 1, "phone": 1}


def test_openai_responses_with_typed_input() -> None:
    cloud, wire = (
        Cloud(policy=POLICY),
        _Wire({"id": "resp_1", "object": "response", "output": []}),
    )
    client = consent.wrap(_openai(wire), consent_client=_cloud_client(cloud))
    # The stub answer may not parse as a response; the request has left by then.
    with contextlib.suppress(Exception):
        client.responses.create(
            model="gpt-4.1",
            instructions="Escalate to ops@example.com",
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi marie@example.com"}],
                }
            ],
        )
    (sent,) = wire.bodies
    # `input` sorts before `instructions`: it is numbered first.
    assert sent["instructions"] == "Escalate to [EMAIL_2]"
    assert sent["input"][0]["content"][0]["text"] == "hi [EMAIL_1]"


def test_openai_a_reply_object_passed_back_is_written_by_the_library() -> None:
    answer = {
        **_CHAT,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Sure, noted."},
            }
        ],
    }
    cloud, wire = Cloud(policy=POLICY), _Wire(answer)
    client = consent.wrap(_openai(wire), consent_client=_cloud_client(cloud))
    messages: list[Any] = [
        {"role": "user", "content": "My address is marie@example.com"}
    ]
    first = client.chat.completions.create(model="gpt-4o", messages=messages)
    # The documented way to carry a conversation: the reply object itself.
    messages.append(first.choices[0].message)
    messages.append({"role": "user", "content": "Write to marie@example.com again"})
    client.chat.completions.create(model="gpt-4o", messages=messages)
    sent = wire.bodies[1]["messages"]
    # No `function_call: null`, `tool_calls: null`, `audio: null`: fields the
    # reply never set, which the library leaves out and the provider refuses.
    assert sent[1] == {"content": "Sure, noted.", "role": "assistant"}
    assert sent[2] == {"role": "user", "content": "Write to [EMAIL_1] again"}


def test_openai_a_reply_object_holding_a_value_is_masked_and_stays_a_reply() -> None:
    answer = {
        **_CHAT,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "Ask support@example.com.",
                },
            }
        ],
    }
    cloud, wire = Cloud(policy=POLICY), _Wire(answer)
    client = consent.wrap(_openai(wire), consent_client=_cloud_client(cloud))
    first = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "Who do I ask?"}]
    )
    reply = first.choices[0].message
    client.chat.completions.create(
        model="gpt-4o", messages=[reply, {"role": "user", "content": "Thanks."}]
    )
    assert wire.bodies[1]["messages"][0] == {
        "content": "Ask [EMAIL_1].",
        "role": "assistant",
    }
    assert reply.content == "Ask support@example.com."  # never mutated


def test_openai_block_sends_nothing() -> None:
    cloud, wire = Cloud(policy=POLICY), _Wire(_CHAT)
    client = consent.wrap(_openai(wire), consent_client=_cloud_client(cloud))
    with pytest.raises(ContentBlockedError):
        client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "4111 1111 1111 1111"}],
        )
    assert wire.bodies == []


def test_openai_stream_helper_is_cleaned() -> None:
    cloud = Cloud(policy=POLICY)
    sse = (
        b'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"m",'
        b'"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}'
        b"\n\ndata: [DONE]\n\n"
    )
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"}
        )

    openai = pytest.importorskip("openai")
    raw = openai.OpenAI(
        api_key="sk-test",  # pragma: allowlist secret
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    client = consent.wrap(raw, consent_client=_cloud_client(cloud))
    with client.chat.completions.stream(
        model="gpt-4o", messages=[{"role": "user", "content": "mail marie@example.com"}]
    ) as stream:
        for _ in stream:
            pass
    for _ in client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "mail paul@example.com"}],
        stream=True,
    ):
        pass
    assert [body["messages"][0]["content"] for body in bodies] == [
        "mail [EMAIL_1]",
        "mail [EMAIL_1]",
    ]


def test_anthropic_system_and_messages() -> None:
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    cloud, wire = Cloud(policy=POLICY), _Wire(_ANTHROPIC)

    def handle(request: Any) -> Any:
        wire.bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=_ANTHROPIC)

    raw = anthropic.Anthropic(
        api_key="sk-test",  # pragma: allowlist secret
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handle)),
    )
    client = consent.wrap(raw, consent_client=_cloud_client(cloud))
    client.messages.create(
        model="claude-x",
        max_tokens=10,
        system="Operator: ops@example.com",
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "call 06 12 34 56 78"}],
            }
        ],
    )
    (sent,) = wire.bodies
    assert sent["system"] == "Operator: [EMAIL_1]"
    assert sent["messages"][0]["content"][0]["text"] == "call [PHONE_1]"


def test_gemini_contents_and_a_typed_config() -> None:
    genai = pytest.importorskip("google.genai")
    types = pytest.importorskip("google.genai.types")
    cloud, wire = Cloud(policy=POLICY), _Wire(_GEMINI)
    raw = genai.Client(
        api_key="test",  # pragma: allowlist secret
        http_options=types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(wire.handle))
        ),
    )
    client = consent.wrap(raw, consent_client=_cloud_client(cloud))
    client.models.generate_content(
        model="gemini-2.5-flash",
        contents="mail marie@example.com",
        config=types.GenerateContentConfig(
            system_instruction="ops@example.com decides", temperature=0.1
        ),
    )
    (sent,) = wire.bodies
    text = json.dumps(sent)
    assert "marie@example.com" not in text and "ops@example.com" not in text
    assert "[EMAIL_1]" in text and "[EMAIL_2]" in text
    assert sent["generationConfig"]["temperature"] == 0.1


def test_gemini_a_config_naming_a_class_still_answers_with_that_class() -> None:
    genai = pytest.importorskip("google.genai")
    pydantic = pytest.importorskip("pydantic")

    class Contact(pydantic.BaseModel):  # type: ignore[name-defined,misc]
        email: str

    cloud = Cloud(policy=POLICY)
    wire = _Wire(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": '{"email": "x"}'}]},
                    "finish_reason": "STOP",
                }
            ]
        }
    )
    raw = genai.Client(
        api_key="gemini-test-key",  # pragma: allowlist secret
        http_options=genai.types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(wire.handle))
        ),
    )
    client = consent.wrap(raw, consent_client=_cloud_client(cloud))
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents="hello",
        config=genai.types.GenerateContentConfig(
            system_instruction="Escalate to ops@example.com",
            response_mime_type="application/json",
            response_schema=Contact,
        ),
    )
    (sent,) = wire.bodies
    assert sent["systemInstruction"]["parts"][0]["text"] == "Escalate to [EMAIL_1]"
    # The instruction was masked; the config still names the class, so the
    # answer is still read into it — not into a dict, as its JSON form gives.
    assert isinstance(response.parsed, Contact)


def test_mistral_chat() -> None:
    mistral = pytest.importorskip("mistralai.client")
    cloud, wire = Cloud(policy=POLICY), _Wire(_CHAT)
    raw = mistral.Mistral(
        api_key="test", client=httpx.Client(transport=httpx.MockTransport(wire.handle))
    )
    client = consent.wrap(raw, consent_client=_cloud_client(cloud))
    client.chat.complete(
        model="mistral-small",
        messages=[{"role": "user", "content": "mail marie@example.com"}],
    )
    (sent,) = wire.bodies
    assert sent["messages"][0]["content"] == "mail [EMAIL_1]"


def test_every_wrapped_provider_hashes_what_left() -> None:
    from idside import request_hash

    cloud, wire = Cloud(policy=POLICY), _Wire(_CHAT)
    client = consent.wrap(_openai(wire), consent_client=_cloud_client(cloud))
    client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "marie@example.com"}]
    )
    (authorize,) = cloud.bodies("/v1/authorize")
    assert authorize["request_hash"] == request_hash(
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "[EMAIL_1]"}]}
    )
    assert get_adapter_for(_openai(wire)).name == "openai"
