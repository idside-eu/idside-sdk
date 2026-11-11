# idside — AI Consent Infrastructure (Python SDK)

A signed proof of the AI consent each call was authorized against — for every AI
call your backend makes.

> ⚠️ **Pre-release (`0.1.0.dev0`).** Install with `pip install 'idside[openai]'`.
> End-to-end use needs an ID Side account: sign up at
> <https://sdk.idside.eu/dashboard/signup>. The free plan covers 10,000 calls a
> month.

## Quickstart

```python
from openai import OpenAI
from idside import consent

client = consent.wrap(OpenAI())          # 1. wrap your provider client

client.chat.completions.create(          # 2. call it as usual…
    model="gpt-4o",
    messages=[{"role": "user", "content": "..."}],
    user_id="usr_marie_456",             # 3. …naming the end user
)
```

On every call, the SDK asks the ID Side cloud to authorize the request, applies
the provider parameters the cloud decided (e.g. OpenAI `store=false`), attaches
a signed **ACT token** (an ECDSA JWT bound to this exact request — the audit
proof), and only then calls the provider. The cloud has already appended the
matching entry to an immutable ledger. You never see, store or relay the user's
consent preferences — that is the point.

**Fail-closed:** if consent cannot be verified — cloud unreachable, malformed
answer — the AI call **does not happen**. The SDK never guesses a decision.

## Supported providers

| Provider | Client library | Wrapped call sites | Token carried via |
| -------- | -------------- | ------------------ | ----------------- |
| OpenAI | `openai` | `chat.completions.create`, `responses.create` (+ `parse`) | `extra_headers` |
| Anthropic | `anthropic` | `messages.create` (+ `parse`, `stream`) | `extra_headers` |
| Google Gemini | `google-genai` | `models.generate_content(_stream)` | `config.http_options.headers` |
| Mistral | `mistralai` | `chat/fim/agents` `complete` & `stream` | `http_headers` |

OpenAI-compatible providers (Groq, Together, xAI…) work through the `openai`
client and its descriptor. DeepSeek is the exception: an `OpenAI` client pointed
at `api.deepseek.com` gets the `deepseek` descriptor, so its proofs say
`provider: "deepseek"` and it takes no `credentials=` (see `docs/providers.md`). Provider libraries are optional extras:
`pip install idside[openai]`, `[anthropic]`, `[gemini]`, `[mistral]` or `[all]`.

`client.with_options(...)` and `client.copy(...)` hand back an enforced client,
recursively, and so do `with_raw_response.*` and `with_streaming_response.*` on
a declared call site: those wrappers are built on first access, after the patch,
so they wrap the enforced method rather than bypassing it. Everything *else* on
a wrapped client — `embeddings`, `images`, `audio`, `moderations`, `batches`,
file uploads, `beta.*` — is **refused rather than sent**: a guard on the client's
`httpx` transport raises `UnenforcedCallError` for any body-bearing request that
did not go through enforcement, while `GET`/`HEAD`/`OPTIONS`/`DELETE` (the
management surface) pass through. Use a second, **unwrapped** client for that
traffic; there is no bypass option on a wrapped one. `UnenforcedCallError`
derives from `BaseException`, so a provider library's blanket `except
Exception:` cannot turn a consent stop into a retried connection error — catch
it by name. The guard also holds each authorized request to the origin the
client had when it was wrapped (so `client.base_url = …` afterwards is refused),
and never lets it follow a redirect: a 3xx comes back as the provider's answer.
A provider client the guard cannot cover — no HTTP transport found, Vertex AI
over mTLS, `google-genai` sending its async half on `aiohttp`, or a transport
whose `send` will not take `follow_redirects` — is not wrapped at all: `wrap()`
raises `UnsupportedProviderError` and says what to pass instead. That last one
is checked at `wrap()` rather than discovered later on purpose: refusing the
keyword mid-call would fail the request *after* ID Side had authorized it and
recorded the proof, while every bodyless call kept working.

## What a single call may not do

The proof describes the request the SDK authorized, so an option that would
change that request *after* authorization is refused with
`UnenforceableOptionError` — before ID Side is asked, with nothing sent:

- a body or query rewrite: `extra_body`, `extra_query`,
  `config.http_options.extra_body`;
- a per-call destination: `server_url`, `config.http_options.base_url`,
  `api_version`, `client_args`, `httpx_client` and the like;
- a header that picks the credential or carries the ACT token —
  `Authorization`, `X-Api-Key`, `x-goog-api-key`, `OpenAI-Project`,
  `X-AI-Consent-Token`… in any casing, including an `omit` that removes one;
- a Gemini tool the library would call itself (a Python function, an MCP
  session): automatic function calling would send several requests under one
  token. Declare the function and run the loop yourself.

The list is closed: only options known to change nothing a proof covers pass —
`timeout`, `timeout_ms`, `retries`, `http_options.timeout` and `retry_options`,
the `parse`/`stream` helper arguments, and header maps holding no reserved
header (`X-Request-Id`, `anthropic-beta`…). An option a newer library adds is
refused until it has been classified — including on the day that library *moves*
the schema the SDK tells options from parameters by: the fallback is then the
schema as this release read it, never a wider list. Request parameters are
unaffected: pass them as named arguments and they are hashed, and forced where
the user's answer requires it.

Deriving a client is a closed list too, checked before anything is sent.
`with_options()`, `copy()` and `with_middleware()` pass `timeout`, `max_retries`
and the webhook secrets; `base_url` passes only if the origin is unchanged,
`default_headers`/`set_default_headers` only if no reserved header is added,
replaced or dropped, and `middleware` only if it is the middleware the client
already runs — so `with_middleware()` given none, which changes nothing, still
derives. A new credential (`api_key`, `auth_token`, `organization`, `project`…)
raises `CredentialRoutingError`: route with `credentials=` instead. A new
transport, query or middleware, or any unclassified option, raises
`UnsupportedProviderError`: build that client and wrap it on its own. A derived
client must also still be detected as the same provider.

What a client was *built* to rewrite is read at `wrap()` too: a
`genai.Client(http_options={"extra_body": …})` merges that body into every
request and is refused (`UnsupportedProviderError`), and a reserved header in
its `http_options.headers` raises `CredentialRoutingError`. For a client library
this SDK has no classification for, the known offenders (`extra_body`,
`extra_query`, `server_url`, `base_url`, `http_options` beyond timeouts, retries
and ordinary headers, reserved headers) are still refused per call.

The boundary is the client you hand to `wrap()`: a transport you build yourself
(`http_client=` with request hooks, a custom `httpx` transport, Mistral SDK
hooks) runs after the guard and can rewrite what it sends. That is your code,
outside what a library in your process can police — and the proof records what
was authorized.

## `user_id` vs `ubt`

Every wrapped call names the end user with exactly **one** of two kwargs. Both
are consumed by the SDK and never forwarded to the AI provider:

- **`ubt="eyJ..."`** — use this when your frontend runs the ID Side Preference
  Center widget. The widget hands your frontend a **User Binding Token**: a JWT
  signed by ID Side that binds the user, your app, the session and the consent
  state. Forward it to your backend and pass it as-is — the SDK treats it as
  opaque; the cloud verifies it. This defeats identity spoofing (collecting an
  opt-out for one user, then calling the SDK with another), and the proof
  records `binding="ubt"` — the strong link.
- **`user_id="usr_..."`** — use this for backend-only integrations without the
  widget. You assert the identity yourself; accepted, but the proof records the
  weaker `binding="user_id"`.

Passing both raises `ConflictingIdentityError`; passing neither raises
`MissingIdentityError`. In both cases nothing is sent anywhere.

## One credential per posture — `credentials=`

No provider lets you switch training off per request: the privacy posture lives
on the **credential** (a *project* at OpenAI and Gemini, a *workspace* at
Anthropic, an *organisation* at Mistral). One key therefore means one posture for
every user of your app at once. Provision one credential per posture and the SDK
sends each call on the one that user's answer requires — the only route by which
a training opt-out can be honoured per person.

```python
client = consent.wrap(
    OpenAI(),                                    # OpenAI validates a per-request
    credentials={                                # header, so an id is enough
        "default": "proj_medhelp_default",
        "zero_retention": "proj_medhelp_zdr",
    },
)

claude = consent.wrap(
    Anthropic(),                                 # no such header exists here:
    credentials={                                # the key is chosen at build time
        "no_training": Anthropic(api_key=PRIVATE_WORKSPACE_KEY),
    },
)
```

**You label; you do not choose.** Which posture a consent state requires is
policy — ID Side resolves it and names the label, the SDK matches it. There is no
callback to override it. A posture you have not provisioned raises
`MissingCredentialError` and the call is never sent on another credential; a
mapping the SDK cannot honour raises `CredentialRoutingError` from `wrap()`
itself. Your credentials never leave the process: ID Side is told which *labels*
exist, never their values.

A client you pass as a credential sends the calls routed to it, so it is held to
what the wrapped client is held to: `wrap()` guards its HTTP transport, freezes
the origin it names *itself* — a residency posture may live on another host,
such as `https://eu.api.openai.com/v1` — and a routed call never follows a
redirect. A client whose transport cannot be guarded is refused by `wrap()` with
`UnsupportedProviderError`. It also stops being an unwrapped client: a request
sent on it directly is refused like one sent on the wrapped client, so keep
non-inference traffic on a client named nowhere in `wrap()`. The mapping is read
once: changing it after `wrap()` reroutes nothing, derived clients included.

## Typed errors and what to do about them

All errors inherit `IDSideError` except `UnenforcedCallError` (see above). None of them ever means "call the provider
anyway" — when authorization did not succeed, the AI call simply did not happen.

| Error | Meaning | What to do |
| ----- | ------- | ---------- |
| `ConsentUnreachableError` | Cloud unreachable, 5xx, or unusable answer | Treat as a transient outage: retry later, degrade your flow |
| `ConsentDeniedError` | Usage denied for the account or key, or held by the organization's policy (`.reason`: `fraud_suspended`, `organization_suspended`, `policy_hold`, or `None` = a denial the SDK cannot qualify, such as a key without the `authorize` scope). Never a user's own refusal | Do not retry; read the message and surface the denial in your product |
| `AuthenticationError` | Bad ID Side API key (HTTP 401) | Fix the key; retrying is pointless |
| `InvalidBindingError` | The visitor's UBT did not verify — expired, issued for another app, altered, or not a UBT at all (HTTP 401 `ubt_invalid`); a `BindingError`, so also an `AuthenticationError` | Not the key: discard the stored UBT and let the widget issue a new one |
| `BindingRequiredError` | The app requires a UBT and the call passed `user_id` (HTTP 401 `ubt_required`); a `BindingError` too | Show the widget before any AI call and pass its UBT as `ubt` |
| `ConsentRefreshRequiredError` | The visitor's choices no longer cover what the app requires (HTTP 409 `consent_refresh_required`). Not an outage, so not a `ConsentUnreachableError` | Stop the request and show the widget again; never retry with backoff |
| `QuotaExceededError` | Plan quota exhausted (HTTP 402) | Upgrade or wait for the window to reset |
| `RateLimitedError` | Too many authorize calls (HTTP 429) | Back off and retry |
| `ContractError` | The cloud refused the authorize payload (HTTP 422): a value your code passed, or an SDK/cloud mismatch | Fix the value the cloud's detail names; otherwise upgrade `idside` or report it |
| `ConsentTokenError` | Request hash cannot be computed (non-JSON value) or token binding invalid | Pass JSON-compatible values or Pydantic models |
| `UnenforceableOptionError` | The call carries an option that would undo enforcement: a body or query rewrite, a per-call destination, a reserved header, a Gemini tool run by the library (raised before authorize) | Pass request parameters as named arguments; set destination and credentials on the client, route with `credentials=` |
| `MissingIdentityError` / `ConflictingIdentityError` | No identity / both identities passed | Pass exactly one of `user_id`, `ubt` |
| `UnsupportedProviderError` | No descriptor matches the client; the guard cannot cover it; or a derivation changes the origin, the provider, the transport, the query or the middleware | Check support, or `register_adapter()`; follow the message; build and wrap a separate client |
| `ToolUseRefusedError` | The call can hand the model tools — `tools`, `functions`, `web_search_options`, a stored conversation or prompt, a Mistral agent, a Gemini cache — and agentic tools are not accepted for this user: refused, never asked, or refused by a policy | Retry without them, or route the user to a non-agentic path; never retry the same call |
| `CredentialRoutingError` | The `credentials=` mapping cannot route; the client already sets the routing header in its default headers; or a derivation changes the credential or a reserved header | Fix the mapping — the message says which entry and why; route with `credentials=` |
| `MissingCredentialError` | This user's answer needs a posture you did not provision (`.posture`, `.provider`, `.provisioned`) | Provision that credential and label it; never retry |
| `ActVerificationError` | A proof did not check out during `verify_act()` (see below) | Reject the proof; the subclass says why |

## Verifying a proof — `verify_act()`

Anyone holding an ACT token can check it **offline**, with no call to ID Side and
no account: an auditor, a customer's compliance team, a provider, a supervisory
authority.

```python
from idside import verify_act

proof = verify_act(
    token,                                    # the X-AI-Consent-Token value
    jwks_url="https://sdk.idside.eu/.well-known/jwks.json",
    request_hash=request_hash,                # one token = one request
)

proof.binding        # "ubt": the widget bound the identity — the strong proof
proof.consent_state  # {"training": False}
proof.app_id, proof.provider, proof.model, proof.token_id, proof.mode
```

What is checked, and never negotiated:

| Check | Rule |
| ----- | ---- |
| `alg` | pinned to `ES256`; `none`, `HS256` or anything else is a forgery attempt |
| `typ` | must be `act+jwt` — a User Binding Token (`ubt+jwt`) is not a proof |
| Signature | ES256 over `header.payload`, with the key named by `kid` |
| `kid` | must be in the JWKS; an unknown one is never worked around |
| Key set | must be public — a JWK carrying `d` or `k` is an incident, see below |
| Encoding | each segment must be *canonical* unpadded base64url |
| `exp` / `iat` / `nbf` | enforced, with 60 s of clock leeway (tune with `leeway=`) |
| Lifetime | `exp - iat` must not exceed 3600 seconds |
| Verification time | the real clock, or an epoch you pass as `now=` (see below) |
| `iss` | `idside.eu` only; `test.idside.eu` requires `allow_test_issuer=True` |
| `mode` | `test` or `live`; missing mode requires explicit `allow_legacy_without_mode=True` |
| Bindings | `request_hash=`, `nonce=`, `subject=`, `app_id=`, when you pass them |

Pass `jwks=` (a JWKS or a single JWK) instead of `jwks_url=` to verify without
any network access at all — that is the whole point of a local proof.

`proof.mode` is typed as `"test" | "live"`. When reading a known archive minted
before this claim existed, opt in with `allow_legacy_without_mode=True`; only
that compatibility path returns `proof.mode is None`.

### Auditing a past call

ACT tokens live one hour, so an archived proof is always expired against the
wall clock. `now=` is the verification time, as an epoch in seconds — set it to
ask *"was this proof valid when the call was made?"*:

```python
proof = verify_act(token, jwks=jwks, now=ledger_entry["issued_at"])
```

It moves **only** the time window. The signature, `alg`, `typ`, the issuer and
every binding are checked exactly as before, so a generous `now` can never turn
a forgery into a proof.

### If your JWKS contains a private key

A JWK carrying `d` (the EC private scalar) or `k` (a symmetric key) is refused
with **`ActPrivateKeyInKeySetError`**, before any other check on that key. A key
set is public by definition, so private material in one means either a signing
key was published by mistake or the key set was poisoned — and verification is
the one moment where that can still be caught. Do not work around it: remove the
private members from the JWKS and **rotate the key**, assuming anything that key
signed can be forged.

It is deliberately **not** a subclass of `ActKeyNotFoundError`, so this:

```python
except ActKeyNotFoundError:
    refresh_jwks_and_retry()      # right for a rotated key…
```

…does **not** swallow a poisoned key set and loop on your JWKS during the one
incident you need to escalate. The two conditions look alike and call for
opposite responses, so they are different classes rather than one class with a
different message.

This is also why non-canonical base64url is refused as `ActMalformedError`
rather than tolerated: one token has exactly one string form. Both SDKs agree
case by case — the shared vectors in
[`packages/descriptors/testdata/`](https://github.com/idside-eu/idside-sdk/tree/main/packages/descriptors/testdata)
pin the verdict
*and* the reason, so "verified" cannot come to mean two different things
depending on which library an auditor installed.

Every failure raises a subclass of `ActVerificationError`
(`ActAlgorithmError`, `ActTypeMismatchError`, `ActSignatureError`,
`ActExpiredError`, `ActNotYetValidError`, `ActIssuerError`,
`ActKeyNotFoundError`, `ActPrivateKeyInKeySetError`, `ActBindingMismatchError`,
`ActMalformedError`, `ActVerificationUnavailableError`). There is no boolean
return, so *"could not check"* is never mistaken for *"valid"*.

Verification needs one extra dependency — ES256 is the only thing the standard
library cannot do, and the core SDK stays dependency-light:

```bash
pip install 'idside[verify]'
```

Two things worth knowing:

- `subject=` pins the `sub` claim, which is the app-scoped **pseudonym** the
  cloud derived from your end user. The raw `user_id` you passed to
  `consent.wrap()` deliberately never enters the token, so it cannot be pinned.
- Replay of the *same* token on the *same* request is not detectable offline (a
  verifier holds no history); that is the ledger's job. What a local check
  proves is that this signed proof belongs to this exact request.

## How enforcement is extensible

Two concerns are kept separate:

- **What to force (the policy)** — which parameters correspond to a consent
  state — is decided by the **cloud** and returned per-request as
  `params_forced`. A provider renames a flag → ID Side updates a row, **no SDK
  release**.
- **How to inject it (the mechanics)** — which methods to intercept, kwargs vs
  headers, which kwarg carries per-request headers — is a small **JSON
  descriptor** per provider (`src/idside/providers/*.json`), validated against
  a shared schema in
  [`packages/descriptors/`](https://github.com/idside-eu/idside-sdk/tree/main/packages/descriptors).

Standard providers need no Python. Clients whose API does not fit the
declarative model override the engine — the bundled Gemini adapter is the
example: `google-genai` takes no free-form kwargs, so it carries the token
inside `config.http_options.headers`:

```python
from idside.adapters import ProviderAdapter, register_adapter

class MyProviderAdapter(ProviderAdapter):
    ...  # override apply() / get_call_sites() as the client requires

register_adapter("myprovider", MyProviderAdapter)  # name matches the descriptor
```

## Security model (short version)

- **Opaque tunnel** — the SDK has no API to read or write preferences; it only
  applies decisions the cloud already made.
- **One token = one request** — each ACT token is bound to a `request_hash`
  (RFC 8785/JCS canonical SHA-256 of the caller payload before enforcement,
  excluding identity and request headers) + nonce + short TTL. Forced params
  and constraint transformations happen afterward. An option that would change
  the request after that point is refused, and so is anything that would send
  several requests under one token.
  Spec:
  [`docs/spec/act-token.md`](https://github.com/idside-eu/idside-sdk/blob/main/docs/spec/act-token.md).
- **No secrets here** — tokens are signed in the ID Side cloud; this package
  holds no private key and cannot sign anything.
- **Privacy by structure** — the SDK sends ID Side an identifier, a provider, a
  model and a *hash*; prompts and completions never leave your backend.

## Development

```bash
uv sync --all-extras
uv run pytest                          # full suite — offline, free
uv run ruff check --fix && uv run ruff format
uv run mypy && uv run bandit -c pyproject.toml -r src
```

`pre-commit` (at the repo root) must pass before any commit. For tests, inject
a fake transport or adapter: `wrap(client, consent_client=..., adapter=...)`;
point at another API with `base_url=` / `api_key=`.

### Live provider tests (opt-in, billable)

`tests/live/` calls the five real provider APIs to check that each one actually
accepts what its descriptor says the SDK injects — and rejects what it says is
unforceable. It is the only way to keep `forceable_params` honest: a parameter
that exists in a client's signature but that the *server* ignores would pass
every offline test and enforce nothing.

They never run by default. Two independent guards see to that: `addopts` carries
`-m 'not live'`, and every test is skipped unless `IDSIDE_LIVE_PROVIDER_TESTS=1`.
CI runs the plain `uv run pytest`, so it never touches a provider.

```bash
export OPENAI_API_KEY=... ANTHROPIC_API_KEY=... GEMINI_API_KEY=...
export MISTRAL_API_KEY=... DEEPSEEK_API_KEY=...
IDSIDE_LIVE_PROVIDER_TESTS=1 uv run pytest -m live
```

Missing keys skip their provider rather than fail, as do a spent quota and a
provider outage — neither says anything about whether our parameters are
acceptable. A rejected *parameter* always fails.

**Cost.** Every call is the smallest the provider accepts: the prompt `"hi"`,
one output token, the cheapest model. A full run is a fraction of a cent. Keep
it that way — these tests verify parameter plumbing, never model output. Each
model id is overridable (`IDSIDE_LIVE_OPENAI_MODEL`, `IDSIDE_LIVE_GEMINI_MODEL`,
…) because providers retire cheap models on short notice.

The ID Side cloud is *not* involved: a stub consent client supplies the decision,
so what is under test is the provider half of the contract. Each test asserts on
the request that left the process as well as on the answer that came back.

## Licence

Source-available, not open source. This package is licensed under the ID Side
Source-Available License, version 1.0 — `LICENSE` in this package, also
published at https://sdk.idside.eu/legal/sdk-licence. Clause 2 grants a
royalty-free licence to reproduce, execute and modify the SDK solely for
non-production internal evaluation, research, and non-commercial testing;
production deployment and any other commercial purpose require a paid
commercial licence. Beyond the limited evaluation patent licence of clause 2 of
`LICENSE`, rights under the patents are conveyed only by an executed commercial
licence. See `LICENSE` and `NOTICE` in this package, and `COMMERCIAL_LICENSE.md`
in the repository.

Patents: https://sdk.idside.eu/patents
