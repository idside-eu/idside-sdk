"""A consent-enforced OpenAI call, from `wrap()` to the wire.

Runs anywhere, with no ID Side account and no OpenAI bill. Two things are
replaced by stand-ins, and only two: the ID Side cloud, which would sign the
proof, and OpenAI's endpoint, which would answer it. Everything between them is
the real SDK doing the real work — finding the call site the descriptor
declares, hashing the request, consuming the identity, forcing the retention
parameter the decision names, attaching the proof, and refusing anything that
did not go through enforcement.

Run it:  uv run wrapped_call.py
Live:    see ../README.md#going-live — it is a two-line diff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from idside import consent
from openai import OpenAI

# Fictitious throughout, as everything in this repository's examples is.
USER = "usr_marie_456"
MODEL = "gpt-4o-mini"
PROMPT = "Summarize my last consultation."


@dataclass(frozen=True)
class FakeDecision:
    """The same fields as the cloud's decision, and no behaviour.

    The SDK reads a decision; it does not construct one, so a fake only has to
    carry the same attributes. The real one arrives as JSON from
    `POST /v1/authorize` and its `act_token` is signed by a key that exists only
    in the ID Side cloud — which is the reason this file cannot make one.
    """

    consent_state: dict[str, Any]
    params_forced: dict[str, Any]
    constraints: dict[str, Any]
    act_token: str
    source: str
    legal_basis: str
    binding: str
    credential_posture: str | None


class FakeCloud:
    """Stands in for idside.eu."""

    def authorize(
        self,
        *,
        provider: str,
        model: str,
        request_hash: str,
        user_id: str | None = None,
        ubt: str | None = None,
        call_site: str | None = None,
        credential_postures: Any = (),
    ) -> FakeDecision:
        print("2. What ID Side was asked")
        print(f"   provider={provider} model={model} call_site={call_site}")
        print(f"   request_hash={request_hash}")
        print(f"   user_id={user_id!r} ubt={ubt!r}")
        print(f"   credential_postures={list(credential_postures)}")
        print("   ...and nothing else. The prompt is not in this request.")
        print()
        print("3. What ID Side answered")
        print("   consent_state={'training': False, 'retention.conversation': False}")
        print("   params_forced={'store': False}   <- the SDK applies it, not you")
        print()
        return FakeDecision(
            # What this person answered. The SDK reads it; it can never write it.
            consent_state={"training": False, "retention.conversation": False},
            # What that answer forces on THIS endpoint. The cloud derives it
            # from the descriptor, so `chat.completions.create` and
            # `responses.create` do not get the same list.
            params_forced={"store": False},
            # What to refuse rather than set — `{"tools": "refuse"}` would make
            # a call carrying tools raise ToolUseRefusedError.
            constraints={},
            act_token="act.eyJhbGciOiJFUzI1NiJ9.fake.signature",
            source="widget",
            legal_basis="consent",
            binding="user_id",
            # Which of the developer's credentials must carry the call. None
            # here: this app holds one key, so one posture for everyone.
            credential_posture=None,
        )


def openai_stub(request: httpx.Request) -> httpx.Response:
    """Stands in for api.openai.com, and prints what actually left."""
    print("4. What actually left this process")
    print(f"   POST {request.url}")
    print(f"   X-AI-Consent-Token: {request.headers.get('X-AI-Consent-Token')}")
    # Keys sorted: the order on the wire is the openai library's own, and it
    # differs between versions of that library. The content is what left.
    body = json.dumps(
        json.loads(request.content), sort_keys=True, separators=(",", ":")
    )
    print(f"   body: {body}")
    print()
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "created": 0,
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        },
    )


def main() -> None:
    client = consent.wrap(
        OpenAI(
            api_key="sk-test-not-a-real-key",
            http_client=httpx.Client(transport=httpx.MockTransport(openai_stub)),
        ),
        consent_client=FakeCloud(),
    )

    print("1. The call the developer writes")
    print("   client.chat.completions.create(")
    print(f"       model={MODEL!r},")
    print(f"       messages=[{{'role': 'user', 'content': {PROMPT!r}}}],")
    print(f"       user_id={USER!r},")
    print("   )")
    print()

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": PROMPT}],
        user_id=USER,
    )

    print("5. What the provider replied")
    print(f"   {response.choices[0].message.content!r}")
    print()
    print("Three things to read twice:")
    print("   - the cloud was given a hash, never the prompt;")
    print("   - store=false — ID Side's decision — reached OpenAI's request body;")
    print("   - user_id did NOT. The SDK consumed it; the provider never sees it.")


if __name__ == "__main__":
    main()
