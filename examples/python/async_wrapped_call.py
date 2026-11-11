"""The same enforcement, on an async client, for two users at once.

`consent.wrap()` asks the client what shape it is — an `AsyncOpenAI` answers by
having coroutine call sites — and that one answer decides everything: each call
site gets an async wrapper, and the hop to the ID Side cloud is awaited instead
of blocking. It matters because of where this code runs. An ASGI application
serves every other request in flight on the same event loop, so one blocking
call to the cloud per AI request would stall all of them — worse than the gap
async support closes.

This example proves the hops really do overlap rather than asserting it: the
stand-in cloud records when each authorize started and finished.

Run it:  uv run async_wrapped_call.py
Live:    see ../README.md#going-live — it is a two-line diff.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from idside import consent
from openai import AsyncOpenAI

MODEL = "gpt-4o-mini"
# Two fictitious users of the same fictitious app, asking different things.
CONVERSATIONS = {
    "usr_marie_456": "Summarize my last consultation.",
    "usr_paul_789": "When is my next appointment?",
}

# What the stand-ins observed, printed after the fact so that two concurrent
# calls cannot interleave their output and make the run unreadable.
hops: dict[str, tuple[float, float, str]] = {}
wire: list[str] = []


@dataclass(frozen=True)
class FakeDecision:
    """The cloud's decision, reduced to the attributes the SDK reads."""

    consent_state: dict[str, Any]
    params_forced: dict[str, Any]
    constraints: dict[str, Any]
    act_token: str
    source: str
    legal_basis: str
    binding: str
    credential_posture: str | None


class FakeAsyncCloud:
    """Stands in for idside.eu, over an awaited hop.

    `authorize` is `async def` — that is the whole difference from the blocking
    example, and the SDK requires it: an async client is wrapped with an async
    consent transport, decided once at `wrap()` time and never on the request
    path.
    """

    async def authorize(
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
        started = time.perf_counter()
        # A real hop to the cloud costs a round trip; without one here the two
        # calls would finish too fast for overlapping to mean anything.
        await asyncio.sleep(0.05)
        hops[str(user_id)] = (started, time.perf_counter(), request_hash)
        return FakeDecision(
            consent_state={"training": False},
            params_forced={"store": False},
            constraints={},
            act_token="act.eyJhbGciOiJFUzI1NiJ9.fake.signature",
            source="widget",
            legal_basis="consent",
            binding="user_id",
            credential_posture=None,
        )


def openai_stub(request: httpx.Request) -> httpx.Response:
    """Stands in for api.openai.com, and keeps what left."""
    # Keys sorted: the order on the wire is the openai library's own, and it
    # differs between versions of that library. The content is what left.
    wire.append(
        json.dumps(json.loads(request.content), sort_keys=True, separators=(",", ":"))
    )
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


async def main() -> None:
    client = consent.wrap(
        AsyncOpenAI(
            api_key="sk-test-not-a-real-key",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(openai_stub)),
        ),
        consent_client=FakeAsyncCloud(),
    )

    print("1. wrap() saw an AsyncOpenAI, and chose the async path for everything")
    print("   the call sites are awaited, and so is the hop to ID Side")
    print()

    await asyncio.gather(
        *(
            client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                user_id=user,
            )
            for user, prompt in CONVERSATIONS.items()
        )
    )

    print("2. Two users, two calls, one event loop")
    for user in sorted(hops):
        print(f"   {user:<14} request_hash={hops[user][2]}")
    print()

    latest_start = max(start for start, _end, _hash in hops.values())
    earliest_end = min(end for _start, end, _hash in hops.values())
    print(f"3. The two authorize hops overlapped: {latest_start < earliest_end}")
    print("   a blocking client would have made them queue, one round trip each")
    print()

    print("4. What left this process (sorted, because the loop decides the order)")
    for body in sorted(wire):
        print(f"   {body}")
    print()
    print("   Both carry store=false, which ID Side decided and the SDK applied.")
    print("   Neither carries a user_id: it never reaches the provider, so these")
    print("   two requests cannot be told apart here except by what was asked.")


if __name__ == "__main__":
    asyncio.run(main())
