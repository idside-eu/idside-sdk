# Quickstart — Python

From nothing to a consent-enforced AI call. Five minutes, three steps: install,
wrap, call.

> **Status.** Pre-release: `idside` is published on PyPI as `0.1.0.dev1`. A real
> call needs an ID Side API key — sign up at
> <https://sdk.idside.eu/dashboard/signup>, the free plan covers 10,000 calls a
> month, or take a 24-hour test key from the no-account sandbox
> ([step 3](#3-configure-and-call)). Step 3 shows the real code;
> [step 6](#6-run-it-offline-without-an-account) runs the whole mechanism end to
> end offline, with no account.

---

## 1. Requirements

- Python 3.10 or newer.
- The provider client library you already use — `openai`, `anthropic`,
  `google-genai` or `mistralai`. See [providers](./providers.md).
- The SDK's only required runtime dependency is `httpx`. Local token
  verification adds `cryptography`, through the optional `[verify]` extra.

## 2. Install

```bash
pip install idside
```

The SDK wraps the provider client library you already use; it does not replace
it, and it does not need you to choose a provider when you install it. That is
all an application needs.

**Starting from nothing**, an extra installs a provider's client library in the
same command:

```bash
pip install 'idside[openai]'
# extras: openai · anthropic · gemini · mistral · all — and verify, for step 7
```

**From a checkout instead** — to read the code, run its tests or try a change:

```bash
git clone https://github.com/idside-eu/idside-sdk.git
cd idside-sdk/packages/python
```

With [uv](https://docs.astral.sh/uv/) (what the repo uses):

```bash
uv sync --all-extras     # SDK + every provider client + dev tools
uv run pytest            # optional: the full suite, no network needed
```

Or into your own virtualenv, with the provider extra you need:

```bash
pip install -e '/path/to/idside-sdk/packages/python[openai]'
```

## 3. Configure and call

You need one ID Side API key and your usual provider key. The ID Side key comes
from one of two places:

- **the dashboard**, after signing up at
  <https://sdk.idside.eu/dashboard/signup> — the free plan covers 10,000 calls a
  month;
- **the no-account sandbox**, *Test without an account* on
  <https://sdk.idside.eu/sdk> — an `sk_test_` key valid for 24 hours and
  limited to 20 calls, to see a real proof before you sign up.

```bash
# IDSIDE_BASE_URL is optional: https://sdk.idside.eu is the default.
export IDSIDE_BASE_URL="https://sdk.idside.eu"
export IDSIDE_API_KEY="sk_test_your_idside_key"
export OPENAI_API_KEY="sk-your_openai_key"
```

```python
from openai import OpenAI
from idside import consent

client = consent.wrap(OpenAI())          # 1. wrap the provider client, once

response = client.chat.completions.create(
    model="gpt-4o-mini",                 # 2. call it exactly as before…
    messages=[{"role": "user", "content": "Summarize my last consultation."}],
    user_id="usr_marie_456",             # 3. …plus the end user's identity
)

print(response.choices[0].message.content)
```

That is the whole integration. `wrap()` returns the same client object with its
AI call methods patched in place, so anything already holding a reference to
`client` — or to `client.chat.completions` — is enforced too. Calling `wrap()`
twice on the same client is harmless: enforcement never stacks.

The default, `https://sdk.idside.eu`, is the ID Side service. Set
`IDSIDE_BASE_URL` only for a local or staging cloud: the origin alone, without
`/v1` or `/widget`. Use the same deployment for the dashboard, widget and
backend.

The call above uses `user_id`, which requires an application that permits that
identity path. Declare your providers in the dashboard before showing the
widget. If your visitor application requires bound tokens, first connect
the [widget and your backend session](./widget.md#8-from-a-recorded-decision-to-a-stronger-proof),
then pass `ubt` instead. Do not enable AI requests until the backend has stored
the visitor’s token; do not fall back to `user_id` when bound tokens are required.

### Configuration

| Setting | Argument | Environment variable | Default |
| --- | --- | --- | --- |
| ID Side API key | `consent.wrap(client, api_key=…)` | `IDSIDE_API_KEY` | none (no `Authorization` header) |
| ID Side base URL | `consent.wrap(client, base_url=…)` | `IDSIDE_BASE_URL` | `https://sdk.idside.eu` |

The explicit argument wins; an empty environment variable counts as unset. Two
further arguments, `consent_client=` and `adapter=`, exist for tests and custom
providers — see [step 6](#6-run-it-offline-without-an-account) and
[providers](./providers.md#7-adding-a-provider).

A fifth, `credentials=`, is what makes training opt-out per-user. The privacy
posture is not a request parameter anywhere — it lives on the credential (an
OpenAI *project*, an Anthropic *workspace*, a Mistral *organisation*), so one
key means one posture for all your users. Provision one per posture, label each,
and every call goes out on the one that user's answer requires:

```python
client = consent.wrap(
    OpenAI(),
    credentials={
        "default": "proj_medhelp_default",
        "zero_retention": "proj_medhelp_zdr",
    },
)
```

You label; ID Side chooses. A posture you have not provisioned stops the call
rather than falling back to the wrong one — except on an application that
serves employees and is set to Observe, where ID Side names no posture you did
not label: the call leaves on your ordinary credential and its proof records the
refusal as not applied (set to Enforce, the call is held instead). See
[credential routing](./providers.md#6-credential-routing).

**A cloud on your own machine.** `IDSIDE_BASE_URL` may point at an ID Side cloud
running locally, on `http://localhost:…`, and authorization works the same. One
thing does not: `verify_act()` refuses on purpose to fetch a key set over
`http://` — public keys fetched over an unauthenticated channel prove nothing —
so `jwks_url=` raises `ValueError` there. Fetch the key set yourself and pass
it as `jwks=` ([step 7](#7-verify-a-proof-yourself)). That cloud is not
production, so it signs as the test issuer, `test.idside.eu`, which
`verify_act()` refuses unless told otherwise — hence `allow_test_issuer=True` in
that branch, and only there (the *issuer* detail of step 7). The `https://`
branch assumes the production service; any other cloud served over `https://`
signs as the test issuer too, and needs `allow_test_issuer=True` as well. An
`sk_test_` key on the production service does not: its proofs carry the
production issuer, and `proof.mode` is what says they are test ones.

```python
import os

import httpx
from idside import verify_act

jwks_url = f"{os.environ['IDSIDE_BASE_URL']}/.well-known/jwks.json"
if jwks_url.startswith("https://"):
    proof = verify_act(token, jwks_url=jwks_url, request_hash=expected_hash)
else:  # a cloud on your own machine: its key set, and its test issuer
    jwks = httpx.get(jwks_url).json()
    proof = verify_act(
        token, jwks=jwks, request_hash=expected_hash, allow_test_issuer=True
    )
```

### Naming the end user

Every call needs exactly one of `user_id=` or `ubt=`; the SDK consumes it and
never forwards it to the provider. Use `ubt=` when your frontend runs the ID Side
widget — it is the stronger proof. The distinction is explained in
[Concepts §8](./concepts.md#8-naming-the-end-user-user_id-vs-ubt). Pass
`user_id=` as a string: it is sent as written, and a number the cloud accepts
does not always name the same user from both SDKs — `42.0` is another user than
`42` here, and the same one in JavaScript
([what a `user_id` may be](./errors.md#missingidentityerror)).

If nobody is signed in to your product, you have to invent the identifier
yourself, and the choice matters: an unstable one makes every visit a new
person, and a browser fingerprint makes ID Side complicit in the tracking it
exists to counter. Read
[what to send as `user_id`](./concepts.md#what-to-send-as-user_id) before you
pick one.

**Until the widget runs, a `user_id` names someone who has refused everything.**
The SDK cannot write a preference and never will — that is the
[opaque tunnel](./concepts.md#3-the-opaque-tunnel) — so what nobody accepted is
refused: every option resolves to `false`, on every provider, and each proof
reads `legal_basis: "not_established"`. Most calls still go out, with the
strictest parameters forced. Two refusals are louder than the rest. Pass
`tools=` and the call raises
[`ToolUseRefusedError`](./errors.md#tooluserefusederror) instead of reaching the
provider, on every request and for every user, because a refusal of
`agentic_tools` is applied by declining the call rather than by rewriting a
parameter. And where a refusal can only be honoured by the credential a call
goes out on — training on Mistral, for one — the call raises
[`MissingCredentialError`](./errors.md#missingcredentialerror) until you
provision and label that credential. Running the
[Preference Center](./widget.md) is what turns the first into a question
somebody can answer. It is one `<script>` tag — plus one line in your pages'
`Content-Security-Policy` if they send one, because a strict `frame-src` refuses
the widget's iframe
([widget §1](./widget.md#if-your-pages-send-a-content-security-policy)).

### Async clients

`AsyncOpenAI`, `AsyncAnthropic` and their equivalents are wrapped the same way,
with the same call:

```python
from openai import AsyncOpenAI
from idside import consent

client = consent.wrap(AsyncOpenAI())

response = await client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Summarize my last consultation."}],
    user_id="usr_marie_456",
)
```

Same forced parameters, same ACT token, same `request_hash`, same errors, same
messages. There is no `wrap_async()`, no flag and nothing to configure: `wrap()`
reads the shape of the client you hand it and enforces it accordingly.

Two consequences worth stating, because an ASGI application is where this
matters:

- **The hop to `/v1/authorize` is awaited, not blocked.** Your event loop is also
  serving every other request in flight; a blocking call to the ID Side cloud on
  the AI path would stall all of them.
- **`stream()` stays a context manager.** It is not a coroutine on an async
  client either — it returns something you `async with` — so the SDK checks the
  identity at the call and authorizes inside `__aenter__`, next to the request it
  authorizes. A stream nobody enters made no request, so it needed no proof.

## 4. What just happened

Between your call and OpenAI's servers, the SDK:

1. computed a `request_hash` — SHA-256 over the RFC 8785 canonical form of your
   caller payload, with identity and per-request headers excluded, before any
   forced parameter or local constraint transformation;
2. sent `POST /v1/authorize` to your configured ID Side service with the
   identifier or UBT, provider, model, call site, credential posture labels
   and request hash. The authorization request contains no prompt, messages,
   provider secret or model response; those conversation contents travel only
   between your backend and the provider;
3. applied the parameters the cloud decided (`store: false` for an OpenAI
   opt-out, for example) — the cloud decides, the SDK never has a policy table;
4. attached the cloud-signed ACT token as the `X-AI-Consent-Token` header;
5. called OpenAI.

Meanwhile the cloud appended an entry to its immutable, hash-chained ledger. If
any of steps 2–4 could not complete, step 5 does not happen at all —
[fail-closed](./concepts.md#9-fail-closed).

### What the ACT token proves

The token is an `ES256` JWT, valid one hour, signed with a key that exists only
in the ID Side cloud. Attached to *this* request, it attests that at this instant
the user's resolved consent state was checked, records where that consent came
from (`source`), on what legal basis, and how strongly the identity was bound
(`binding: "ubt" | "user_id"`).

It carries the `request_hash`, so it is worthless on any other request: **one
token = one request**. Full claim list in
[`spec/act-token.md`](./spec/act-token.md#3-claims).

### Where to see it

- **The ledger**, in the ID Side dashboard: one entry per authorized call, with
  its token id — the audit trail you show a data protection authority.
- **Publicly**, with no API key and no account:
  `GET https://sdk.idside.eu/v1/verify/{token_id}` answers *"is this token
  genuine?"* — deliberately without revealing what the user consented to. See
  [the spec](./spec/act-token.md#5-verification).
- **Locally**, with no help from ID Side at all: fetch the JWKS, verify the
  signature, recompute the hash. See [step 7](#7-verify-a-proof-yourself).

## 5. Handle the errors that matter

```python
from idside import ConsentDeniedError, ConsentUnreachableError

try:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        user_id="usr_marie_456",
    )
except ConsentDeniedError:
    # Not this user's choice: the account or the key behind your app cannot
    # authorize, or an organization's policy held the call. A user's refusal never
    # lands here. Do not retry.
    return "AI summaries are not available."
except ConsentUnreachableError:
    # Consent could not be verified, so no AI call was made. Fail-closed.
    return "Summaries are temporarily unavailable. Please try again shortly."
```

Both errors inherit `IDSideError`, which you can catch as a single boundary —
with one deliberate exception: `UnenforcedCallError` derives from
`BaseException`, so that neither `except IDSideError` nor a provider library's
blanket `except Exception` can swallow a consent stop
([why](./errors.md#catching-it-the-one-asymmetry-between-the-sdks)). The
[error catalogue](./errors.md) documents all nineteen enforcement types — plus
twelve more for local token verification — when each fires and what to do about
it.

### When a call did not go out, and you need to know why

The SDK writes to the standard `logging` module, on the `idside` logger, and
**writes nothing at all until your application asks it to**:

```python
import logging

logging.basicConfig(level=logging.DEBUG)
logging.getLogger("idside").setLevel(logging.DEBUG)
```

```
wrap provider=openai call_sites=6 mode=blocking net=true
authorize call_site=chat.completions.create model=gpt-4o-mini subject=8f1c2ab90d47 binding=ubt request_hash=ffb904460a38 duration_ms=41 forced=store
enforced call_site=chat.completions.create forced=store routed=false
```

Seven events: `wrap` (which descriptor matched, how many entry points were
patched, what the net came to), `authorize` (what the cloud answered and how
long it took), `authorize_failed`, `enforced` (the call is going out),
`refused` (a constraint stopped it locally), `passthrough` (an alias that
was already enforced upstream) and `resume` (an overloaded helper called in the
form that replays an existing response instead of creating one, so there was
nothing to authorize — see [`responses.stream()`](./providers.md#3-intercepted-call-sites)).
Each record also carries a structured payload at `record.idside`, for
applications that log as JSON.

The Node SDK emits the first six, and its `authorize` and `enforced` lines are
identical to these. Its `wrap` line differs on purpose, in two fields. `mode=`
is always `async` there, the only shape of client it has. `call_sites=` counts
fewer entry points for the same client — 4 for an OpenAI client, where this SDK
counts 6 — because Node never patches the `stream()` helpers: they reach the
`create` it did patch. Not patching `stream()` is also why it has no `resume`,
the resuming form being one of that helper's.

`net=` has three values and they are not two: `true` means every route out of
this client is guarded, `false` means it owns none to guard (a fake, a client
that sends through something else), and `partial` means one of them is — or may
be — unguarded, because the search for them was cut short or a transport would
not take the patch. Only the first is a covered client; enforcement on the
declared call sites holds in all three.

**Nothing secret is ever written.** Not your ID Side key, not your provider key,
not the end user's `user_id` or `ubt`, not the prompt, not the ACT token, not a
credential. `subject` is a pseudonym salted with a random per-process value: two
calls by one person share it, so you can follow one user through a log, and
nobody can work backwards from it to the identifier — not even for a guessable
one like `usr_marie_456`, which is why the salt is there. `request_hash` is the
value the ledger indexes, so it is how you find the proof for a given call.

Two deliberate absences. The **ACT token** is never logged: it is bearer
material, and the request hash already answers the question you would have
wanted it for. **`consent_state`** is never logged either — what a person
answered is theirs; what it forced on your request is `forced=`, which is the
part you are debugging.

The [net](./providers.md#33-the-net-what-a-wrapped-client-refuses-to-send)
writes nothing, ever, by design. If a call was refused there, you learn it from
the `UnenforcedCallError` and not from a log line: a guard that recorded the
paths it saw would be recording your requests.

## 6. Run it offline, without an account

You do not need an ID Side account — or an OpenAI bill — to watch the mechanism
work. What follows is one file to copy;
[`examples/python/`](../examples/python/) is the same thing as a project you can
clone and run, plus the async surface. `wrap()` accepts a `consent_client=`, which is how the test suite injects a
fake cloud; here we also point the OpenAI client at a stub transport so we can
read exactly what left the process.

```python
"""Everything below is fake except the ID Side SDK itself."""

import json
from dataclasses import dataclass
from typing import Any

import httpx
from openai import OpenAI

from idside import consent


@dataclass(frozen=True)
class FakeDecision:
    """Shaped like the ConsentDecision the real cloud returns."""

    consent_state: dict[str, Any]
    params_forced: dict[str, Any]
    constraints: dict[str, Any]
    act_token: str
    source: str
    legal_basis: str
    binding: str
    credential_posture: str | None


class FakeCloud:
    """Stands in for idside.eu. The real cloud signs the ACT token."""

    def authorize(
        self,
        *,
        provider,
        model,
        request_hash,
        user_id=None,
        ubt=None,
        call_site=None,
        credential_postures=(),
    ):
        print(f"[cloud] authorize: provider={provider} model={model} call_site={call_site}")
        print(f"[cloud] request_hash={request_hash}")
        print(f"[cloud] identity: user_id={user_id!r} ubt={ubt!r}  <- no prompt here")
        print(f"[cloud] credential_postures={list(credential_postures)}  <- labels, never keys")
        return FakeDecision(
            consent_state={"training": False},
            # Decided for THIS endpoint: the real cloud answers
            # `chat.completions.create` and `responses.create` differently,
            # because they do not accept the same parameters.
            params_forced={"store": False},
            # What to refuse, as opposed to what to set. {"tools": "refuse"}
            # here would make a call passing `tools` raise ToolUseRefusedError.
            constraints={},
            act_token="act.eyJhbGciOiJFUzI1NiJ9.fake.signature",
            source="widget",
            legal_basis="consent",
            binding="user_id",
            # Which of the developer's credentials must carry this call. Naming
            # one they did not provision raises MissingCredentialError instead
            # of quietly sending it on another. None here: this app has one key.
            credential_posture=None,
        )


def openai_stub(request: httpx.Request) -> httpx.Response:
    """Stands in for api.openai.com so we can read what actually left."""
    print(f"[openai] X-AI-Consent-Token: {request.headers.get('X-AI-Consent-Token')}")
    # Keys sorted: the order on the wire is the openai library's own, and it
    # differs between versions of that library. The content is what left.
    body = json.dumps(json.loads(request.content), sort_keys=True, separators=(",", ":"))
    print(f"[openai] body: {body}")
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        },
    )


client = consent.wrap(
    OpenAI(
        api_key="sk-test-not-a-real-key",
        http_client=httpx.Client(transport=httpx.MockTransport(openai_stub)),
    ),
    consent_client=FakeCloud(),
)

client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Hello!"}],
    user_id="usr_marie_456",
)
```

Save it as `dryrun.py` and run it where `idside[openai]` is installed:

```console
$ python dryrun.py
[cloud] authorize: provider=openai model=gpt-4o-mini call_site=chat.completions.create
[cloud] request_hash=sha256:3ff33a93d528feaaa859078c68af6a379567b262ed5600b5bd91300673f5cf76
[cloud] identity: user_id='usr_marie_456' ubt=None  <- no prompt here
[cloud] credential_postures=[]  <- labels, never keys
[openai] X-AI-Consent-Token: act.eyJhbGciOiJFUzI1NiJ9.fake.signature
[openai] body: {"messages":[{"content":"Hello!","role":"user"}],"model":"gpt-4o-mini","store":false}
```

The body is printed with its keys sorted, so the line is the same whichever
version of `openai` you have: the order on the wire is that library's, and it
has changed between versions. Three things are worth reading twice:

- the cloud saw a hash, not the prompt;
- `store: false` — the cloud's decision — reached OpenAI's request body;
- `user_id` did **not** reach OpenAI. It is consumed by the SDK.

Run the [equivalent Node.js script](./quickstart-node.md#6-run-it-offline-without-an-account)
and you get **the same `request_hash` for the same payload**. That is not a
coincidence: it is the property that lets an auditor recompute the hash from your
request, in whatever language they like, and check it against the token.

## 7. Verify a proof yourself

A proof nobody but the issuer can check is not a proof. `verify_act()` checks an
ACT token **offline**, with no call to ID Side: JWS structure, `alg` pinned to
`ES256`, `typ` = `act+jwt`, key selection by `kid`, the signature, the mandatory
claims, the issuer, the time window, and any binding you pin.

ES256 verification needs one extra dependency, kept out of the core on purpose —
the SDK sits on the AI request path and should not force a C extension on
callers who only wrap a client:

```bash
pip install 'idside[verify]'      # adds cryptography
```

```python
from idside import request_hash, verify_act

# The request this proof must belong to: the arguments of the call, without
# user_id / ubt and extra_headers, which the SDK never hashes.
expected_hash = request_hash(
    {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hello!"}]}
)
# → "sha256:3ff33a93d528feaaa859078c68af6a379567b262ed5600b5bd91300673f5cf76"

proof = verify_act(
    token,                          # the raw X-AI-Consent-Token value
    jwks=jwks,                      # or: jwks_url="https://sdk.idside.eu/.well-known/jwks.json"
    request_hash=expected_hash,     # pin the request this proof must belong to
)
if proof.mode != "live":            # a test key's proof verifies too — see below
    raise ValueError("not production evidence: minted with a test key")

print(proof.app_id, proof.provider, proof.model, proof.binding, proof.mode)
# app_medhelp openai gpt-4o-mini ubt live
print(dict(proof.consent_state), proof.source, proof.legal_basis)
# {'training': False} widget consent
```

`request_hash(payload)` is the function the SDK itself hashes every call with:
the lowercase SHA-256 of the RFC 8785 canonical form of `payload`, prefixed
`sha256:`. It is the same value Node's `requestHash()` returns for the same
payload. Leave out what the SDK leaves out — `user_id`, `ubt`, `extra_headers` —
and do not add what the cloud forced afterwards (`store=False`). Pass JSON
values (a dict of dicts, lists, strings, numbers, booleans and `None`); anything
else raises `ValueError`.

`verify_act(token, *, jwks=None, jwks_url=None, issuer=None,
allow_test_issuer=False, allow_legacy_without_mode=False, leeway=60.0, now=None,
request_hash=None, nonce=None, subject=None, app_id=None)` returns a frozen `VerifiedAct` — `issuer`,
`subject`, `app_id`, `token_id`, `issued_at`, `expires_at`, `consent_state`,
`legal_basis`, `source`, `provider`, `model`, `request_hash`, `nonce`,
`binding`, `mode`, `key_id`, and the raw `claims`. Anything that does not check out
raises an [`ActVerificationError`](./errors.md#verification-errors) subclass;
its existence *is* the success signal.

Which layer refused each option — the person, their silence, the organization,
a team or the application — is the `refused_by` claim, read from `claims`:

```python
refused_by = proof.claims.get("refused_by")   # None on a token older than the claim
# {'agentic_tools': ['person', 'organization'], 'memory': ['silence']}
```

It has no field of its own on purpose. Its values are opaque and a layer can be
added later: an SDK holding a closed list of layers would reject valid tokens
the day a new one appeared, and since the SDK is fail-closed every older copy
in production would stop its AI calls. Read the values as strings; the
[spec](./spec/act-token.md#refused_by--who-refused) lists today's.

Six details worth knowing:

- **Exactly one key source.** Pass `jwks=` (a JWK Set you already fetched) or
  `jwks_url=` (fetched over HTTPS on every call, no caching, no redirects; a
  cloud on your own machine needs `jwks=`, [see §3](#3-configure-and-call)).
  There is no ambient default: a verifier that silently reaches for a key source
  it was not given is a verifier you cannot reason about.
- **There is no `user_id=` parameter**, deliberately. The token's `sub` is the
  app-scoped *pseudonym* the cloud derived from the end user; the raw `user_id`
  you passed to `consent.wrap()` never enters the proof. Pin `subject=` if you
  have the pseudonym — pinning your own identifier would reject valid tokens.
- **`request_hash` is the check that matters.** A valid signature only says
  "ID Side issued this". Pinning the hash says "…for *this* request". Without
  it, a genuine token from another call would pass.
- **A test key's proof verifies: read `proof.mode`.** A call made with an
  `sk_test_` key against the ID Side service — the sandbox key included — is
  signed by the production key and carries `iss: idside.eu`, so it passes
  `verify_act()` with the default options. Only `proof.mode` tells it apart:
  `"test"` or `"live"`, the mode of the API key that minted it. Check
  `proof.mode == "live"` before you treat a proof as production evidence. A
  token without `mode` is malformed unless you explicitly set
  `allow_legacy_without_mode=True` while reading a known historical archive;
  that compatibility result has `proof.mode is None`.
- **The issuer check is about the cloud, not the key.** `test.idside.eu` is the
  issuer of a deployment of the ID Side cloud that is not the production service
  — a staging cloud, or one on your own machine — and its tokens are rejected
  unless you set `allow_test_issuer=True`.
- **Auditing an old call needs `now=`.** ACT tokens live one hour, so an archived
  proof is expired against the wall clock. Pass the instant you are auditing —
  `verify_act(token, jwks=jwks, now=ledger_entry["issued_at"])` — and the answer
  becomes "was this proof valid when the call was made?".

`now=` moves **only** the `exp` / `iat` / `nbf` window. The signature, `alg`,
`typ`, the issuer and every binding you pinned are checked exactly the same way,
so a generous `now` can turn an expired proof into a readable one but never a
forgery into a proof.

The verifier also rejects any token whose declared lifetime exceeds 3600
seconds, even when `now=` falls inside that oversized window.

> Where do you get a token to verify? From the ledger export or the dashboard,
> from whoever is auditing you, or by capturing the outgoing
> `X-AI-Consent-Token` header in your HTTP client. The enforcement path does not
> return the token to the caller today — `consent.wrap()` attaches it and calls
> the provider.

The Node.js SDK verifies the same tokens with the same rules, and the
[spec](./spec/act-token.md#5-verification) documents the procedure for anyone
implementing it in another language.

## Known limitations

- **`google-genai`'s async surface is refused, not enforced.** `genai.Client`
  exposes its async methods at `client.aio.*`, and no descriptor declares them —
  so they are not patched, and the net refuses them with
  [`UnenforcedCallError`](./errors.md#unenforcedcallerror) rather than letting an
  unproven call out. Use the blocking surface of that client, or a different
  provider, until a descriptor declares those call sites. The async clients of
  `openai` and `anthropic` are fully enforced.
- **Enforced entry points** are exactly those in each provider's
  [descriptor](./providers.md#3-intercepted-call-sites), plus their aliases
  (`parse` and `stream`, so `chat.completions.stream()`, `responses.stream()`
  and `messages.stream()` are enforced calls like any other), plus the
  `with_raw_response.*` and `with_streaming_response.*` views of those same call
  sites: those wrappers are built on first *access*, after the client has been
  patched, so they wrap the enforced method and inherit its authorize, its
  forced parameters and its token. Everything else on
  a wrapped client —
  `embeddings`, `images`, `audio`, `moderations`, `batches`, file uploads, the
  `beta.*` namespaces — is **refused**, not silently
  sent: any body-bearing request that did not go through enforcement raises
  [`UnenforcedCallError`](./errors.md#unenforcedcallerror) before it leaves the
  process. Build a second, **unwrapped** client for that traffic; there is no
  bypass option on `wrap()`. `GET`/`HEAD`/`OPTIONS`/`DELETE` (`models.list`,
  `files.delete`, polling a response id) pass through untouched.
- **Resuming a response is not enforced.** `responses.stream(response_id=...)`
  replays a response that already exists over `GET /v1/responses/{id}`: it
  creates nothing, so it needs no identity, mints no ACT token and leaves no
  ledger entry. Passing a `user_id=` anyway is harmless — it is consumed, not
  forwarded. Not a bypass: the call carries no token, so if it ever turned out
  to carry a body the net would refuse it. The creating form
  (`responses.stream(model=..., input=...)`) is enforced like any other AI call.
- **`client.with_options(...)` / `client.copy(...)` come back enforced**, and so
  does anything derived from those, recursively.

## Next

- [Concepts](./concepts.md) — the opaque tunnel, and why there is no preference setter
- [Error catalogue](./errors.md) — every error type, and how to catch each
- [Providers](./providers.md) — call sites and forced parameters per provider
- [FAQ](./faq.md) — latency, outages, what ID Side does and does not see
- [`packages/python/README.md`](../packages/python/README.md) — package-level reference
