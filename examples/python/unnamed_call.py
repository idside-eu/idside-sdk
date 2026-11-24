"""A call that names nobody: the organization's policy, proven against the API key.

Naming the end user is optional. Most calls name nobody — a back-office job, an
assistant every employee shares, a feature whose users the organization does
not track — and the policy the organization set on the ID Side dashboard
applies to them all the same. The certificate's subject is then the API key the call
was made with, and the answer says so: `binding: "api_key"`.

This example uses the SDK's real transport to the cloud, so what it prints as
"what ID Side was asked" is the JSON body the SDK actually sent: there is no
`user_id` in it, not even an empty one. Only the answer comes from a stand-in,
because no ACT token can be signed outside the ID Side cloud. The SDK checks
that answer as it would a real one — including that `api_key` is a binding it
accepts on a call that named nobody, and on no other.

Run it:  uv run unnamed_call.py
Live:    see ../README.md#going-live — it is a two-line diff.
"""

from __future__ import annotations

import json

import httpx
from idside import HttpConsentClient, consent
from openai import OpenAI

# Fictitious throughout, as everything in this repository's examples is.
MODEL = "gpt-4o-mini"
PROMPT = "Draft a polite reply to this supplier."


def cloud_stub(request: httpx.Request) -> httpx.Response:
    """Stands in for sdk.idside.eu, and prints what the SDK sent it."""
    if request.url.path == "/v1/content-policy":
        # Before its first call the SDK asks for the application's prompt-
        # cleaning policy; this application cleans nothing (see
        # replay_cleaning.py and docs/prompt-cleaning.md).
        return httpx.Response(200, json={"enabled": False})
    sent = json.loads(request.content)
    print("2. What ID Side was asked")
    print(f"   POST {request.url.path}")
    print(f"   body: {json.dumps(sent, sort_keys=True, separators=(',', ':'))}")
    print("   ...no user_id, no ubt: the call names nobody.")
    print()
    answer = {
        "decision": "allow",
        "consent_state": {"training": False, "retention.conversation": False},
        "params_forced": {"store": False},
        "mode": "test",
        "act_token": "eyJhbGciOiJFUzI1NiJ9.unsigned-placeholder.signature",
        "token_id": "act_01k4f9h2m3",
        "expires_at": "2026-09-12T12:00:00Z",
        # The organization's policy decided, on the basis it declared.
        "source": "default_policy",
        "legal_basis": "legitimate_interest",
        # The certificate's subject is the API key the call was made with.
        "binding": "api_key",
    }
    print("3. What ID Side answered")
    print(f"   source={answer['source']!r} legal_basis={answer['legal_basis']!r}")
    print(f"   binding={answer['binding']!r}   <- the subject is the API key")
    print(f"   params_forced={answer['params_forced']}")
    print()
    return httpx.Response(200, json=answer)


def openai_stub(request: httpx.Request) -> httpx.Response:
    """Stands in for api.openai.com, and prints what actually left."""
    print("4. What actually left this process")
    print(f"   POST {request.url}")
    print(f"   X-AI-Consent-Token: {request.headers.get('X-AI-Consent-Token')}")
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
        consent_client=HttpConsentClient(
            "https://sdk.idside.eu", transport=httpx.MockTransport(cloud_stub)
        ),
    )

    print("1. The call the developer writes — naming nobody")
    print("   client.chat.completions.create(")
    print(f"       model={MODEL!r},")
    print(f"       messages=[{{'role': 'user', 'content': {PROMPT!r}}}],")
    print("   )")
    print()

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": PROMPT}],
    )

    print("5. What the provider replied")
    print(f"   {response.choices[0].message.content!r}")
    print()
    print("Two things to read twice:")
    print("   - no identity left this process, and none was needed: the policy")
    print("     applies to every call made with the key;")
    print("   - the certificate still carries the basis the organization declared.")


if __name__ == "__main__":
    main()
