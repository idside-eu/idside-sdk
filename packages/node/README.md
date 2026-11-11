# `@idside/sdk` — ID Side SDK for Node.js

AI **consent enforcement** for provider clients, at the wrap of a single call.
The developer never touches the end user's consent preferences — the SDK is an
opaque tunnel to the ID Side cloud.

> Strict parity with the
> [Python SDK](https://github.com/idside-eu/idside-sdk/tree/main/packages/python):
> same concepts, the same shared provider descriptors, the same `/v1/authorize`
> contract, the same invariants (opaque tunnel, fail-closed, no preference
> setter). The request hash is byte-identical across SDKs — proven against the
> shared vectors in
> [`packages/descriptors`](https://github.com/idside-eu/idside-sdk/tree/main/packages/descriptors).

> ⚠️ **Pre-release (`0.1.0-dev.0`).** End-to-end use needs an ID Side account:
> sign up at <https://sdk.idside.eu/dashboard/signup>. The free plan covers
> 10,000 calls a month.

## Install

```bash
npm install @idside/sdk@next openai
```

Pre-releases are published under the `next` dist-tag, so name it. To run the
SDK from a checkout of the repository instead, see the
[quickstart](https://github.com/idside-eu/idside-sdk/blob/main/docs/quickstart-node.md#2-install).

Requires **Node ≥ 22.12** once installed. The package ships compiled JavaScript
with type declarations and has **zero runtime dependencies**. It is ESM-only;
CommonJS callers can `require("@idside/sdk")` through Node's `require(esm)`
support.

## Usage

```ts
import { consent } from "@idside/sdk";
import OpenAI from "openai";

const client = consent.wrap(new OpenAI(), { apiKey: process.env.IDSIDE_API_KEY });

await client.chat.completions.create({
  model: "gpt-5",
  messages: [{ role: "user", content: "Hello!" }],
  user_id: "usr_marie_456", // or: ubt: "eyJ…" from the ID Side widget
});
```

On every intercepted AI call the wrapped client:

1. resolves the user's preferences from the ID Side cloud and obtains a signed
   **ACT token** bound to this exact request;
2. forces the provider's non-retention parameters (e.g. `store: false`);
3. attaches the ACT token as the `X-AI-Consent-Token` header;
4. calls the provider.

If consent cannot be verified, **the call does not happen** (fail-closed).

### Naming the end user

Pass exactly one of two fields on every AI call; both are consumed by the SDK and
never forwarded to the provider:

- `ubt` — a **User Binding Token** issued by the ID Side widget (strongest
  binding; the proof records `binding: "ubt"`).
- `user_id` — a developer-asserted identity for backend-only integrations
  (weaker; the proof records `binding: "user_id"`).

Passing both throws `ConflictingIdentityError`; passing neither throws
`MissingIdentityError` — in both cases no cloud query and no AI call is made.

### What an enforced call may carry

The SDK proves the request it authorized, so a per-request option that could
change what is sent afterwards is refused. The body is hashed and forced; the
options object next to it is a closed list per library:

| Accepted | Refused with `UnenforceableOptionError` |
| --- | --- |
| `timeout`, `maxRetries`, `signal`, `idempotencyKey` (openai, anthropic) | `body`, `fetchOptions`, `path`, `query`, `method`, `defaultBaseURL`, `stream`, `__*`, `middleware`, `fallbackState` |
| `timeoutMs`, `retries`, `retryCodes`, `signal` (mistral) | `serverURL`, `fetchOptions` and every `RequestInit` key (`dispatcher`, `redirect`, `keepalive`, …) |
| `config.httpOptions.timeout` / `retryOptions`, `config.abortSignal` (gemini) | `config.httpOptions.extraBody` / `baseUrl` / `apiVersion` / `baseUrlResourceScope` |
| any header that does not choose a credential (`X-Request-Id`, `anthropic-beta`, …) | `Authorization`, `X-Api-Key`, `OpenAI-Project`, `x-goog-api-key`, … and `X-AI-Consent-Token` itself, in any case, even set to `null` |

An option this SDK does not know is refused too, until it is classified. The
refusal happens before anything else: ID Side is not asked, nothing reaches the
ledger, nothing leaves for the provider. The message names the option or the
header, never its value. A Gemini tool the library would execute by itself
(anything with `callTool`, `mcpToTool()` included) is refused the same way: it
would make several requests under one ACT token.

When a user has not accepted agentic tools, `ToolUseRefusedError` covers every
field that can hand the model one, not only `tools`: `functions` and
`web_search_options`, `mcp_servers`, a Mistral `agentId`, a Gemini
`config.cachedContent`, and on `responses.create` a `conversation`,
`previous_response_id`, `prompt` or tool-bearing `input` items. An empty tool
list is no tool.

### Configuration

`apiKey` and `baseUrl` fall back to the `IDSIDE_API_KEY` and `IDSIDE_BASE_URL`
environment variables, so `consent.wrap(new OpenAI())` works with no options once
the env is set. A transport built by hand, `new HttpConsentClient()`, resolves both
the same way, as Python's does: the argument or option first, then the
environment variable, then the default address (and no key).

### One credential per posture — `credentials`

No provider lets you switch training off per request: the privacy posture lives
on the **credential** (a *project* at OpenAI and Gemini, a *workspace* at
Anthropic, an *organisation* at Mistral). One key therefore means one posture for
every user of your app at once. Provision one credential per posture and the SDK
sends each call on the one that user's answer requires — the only route by which
a training opt-out can be honoured per person.

```ts
const client = consent.wrap(new OpenAI(), {
  // OpenAI validates a per-request header, so an id is enough
  credentials: { default: "proj_medhelp_default", zero_retention: "proj_medhelp_zdr" },
});

const claude = consent.wrap(new Anthropic(), {
  // no such header exists here: the key is chosen when the client is built
  credentials: { no_training: new Anthropic({ apiKey: PRIVATE_WORKSPACE_KEY }) },
});
```

**You label; you do not choose.** Which posture a consent state requires is
policy — ID Side resolves it and names the label, the SDK matches it. There is no
callback to override it. A posture you have not provisioned throws
`MissingCredentialError` and the call is never sent on another credential; a
mapping the SDK cannot honour throws `CredentialRoutingError` from `wrap()`
itself. Your credentials never leave the process: ID Side is told which *labels*
exist, never their values.

**A client you pass is guarded like the one you wrap.** A call routed to it
leaves through that client's own transport, so `wrap()` puts the same net on it:
its `fetch` refuses a body-bearing request enforcement did not authorize, it is
bound to the origin of **its own** base URL — which may rightly differ from the
wrapped client's, as a residency posture does (`https://eu.api.openai.com/v1`) —
and a routed request is sent with `redirect: "manual"`. Keep another client for
anything else you send with that key. A client the net cannot cover is refused by
`wrap()` as the wrapped one would be, and nothing of that attempt stays patched.

**The mapping is read once**, when you call `consent.wrap()`: changing the object
afterwards reroutes nothing, derived clients included. A credential added later
was declared to nobody, guarded nowhere and bound to no server, so routing a call
onto it would prove something that did not happen.

### TypeScript

`wrap()` returns `Enforced<T>` — the same client, with `user_id` / `ubt` typed
onto every enforced call site. No cast, and omitting the identity is a compile
error rather than a `MissingIdentityError` on the first request:

```ts
import { consent, type Enforced } from "@idside/sdk";

const client: Enforced<OpenAI> = consent.wrap(new OpenAI());
```

`Enforced<OpenAI>` is not assignable to `OpenAI` — it demands more — so annotate
a wrapped client with it. See
[quickstart §TypeScript](https://github.com/idside-eu/idside-sdk/blob/main/docs/quickstart-node.md#typescript).

## Verifying a proof — `verifyAct()`

The ACT token attached to each call is a signed, independently checkable proof.
`verifyAct()` checks one **locally and offline**, with no call to ID Side — what
an auditor, a compliance team or a data protection authority needs:

```ts
import { verifyAct } from "@idside/sdk";

const proof = await verifyAct(token, {
  jwks, // the public JWK Set, or: jwksUrl: "https://sdk.idside.eu/.well-known/jwks.json"
  requestHash, // pin the request this proof must belong to
});

proof.provider; // "openai"
proof.consentState; // { training: false }
proof.binding; // "ubt" — the widget bound the identity (vs "user_id", asserted)
proof.mode; // "live" or "test"
proof.keyId; // the kid that verified the signature
```

It checks, in order: the compact JWS structure and strict unpadded base64url,
`alg` pinned to **ES256**, `typ` = `act+jwt`, no unknown `crit`, key selection by
`kid`, the signature, the mandatory claims, the issuer, the validity window
(60 s leeway by default), then every binding you pinned. Everything fails closed
— `alg: none` and HMAC forgeries die on the header, before the signature is even
looked at, and an unknown `kid` is never worked around by trying another key.

- **Key source** — exactly one of `jwks` (a JWK Set or a single JWK, kept fully
  offline) or `jwksUrl` (`https` only, fetched on each call: fetch it once
  yourself for repeated verification).
- **Bindings** — `requestHash` is the one that matters: *one token = one
  request*, so pinning it makes a stolen but perfectly signed token useless on
  another payload. `nonce`, `subject` and `appId` can be pinned too. Note that
  `sub` is the app-scoped **pseudonym** the cloud derived from the end user; the
  raw `user_id` never enters the token.
- **Issuers** — `idside.eu` only by default. Development tokens
  (`test.idside.eu`) need `allowTestIssuer: true`, so a dev token can never
  silently pass as a production proof.
- **Mode and lifetime** — `proof.mode` is typed as `"test" | "live"`, and the
  declared lifetime cannot exceed 3600 seconds. A missing mode is accepted only
  with `allowLegacyWithoutMode: true` for a known historical archive; that
  compatibility result has `proof.mode === undefined`.
- **Errors** — every failure throws an `ActVerificationError` subclass
  (`ActAlgorithmError`, `ActSignatureError`, `ActExpiredError`,
  `ActBindingMismatchError`, …), so catching the base class alone is already
  fail-closed. Caller mistakes (no key source, both, a non-https `jwksUrl`, a
  negative `leeway`) throw `TypeError`.
- **A key set carrying private material** (`d` or `k`) throws
  `ActPrivateKeyInKeySetError`, which is **not** an `ActKeyNotFoundError`. That
  is deliberate: the usual handler for a key-lookup failure refreshes the key
  set and retries, which is right when a key rotated and badly wrong when a
  signing key has leaked or the set was poisoned — it would loop during the very
  incident you should be escalating. Rotate the signing key; do not retry.

Replay across *different* calls is not detectable offline — a local verifier has
no history; that is the cloud ledger's job. What it proves is that this signed
token belongs to this exact request. Behaviour matches the Python SDK's
`verify_act()` check for check, including the error taxonomy.

## Supported providers

OpenAI, Anthropic, Gemini (`@google/genai`) and Mistral, driven by the shared
JSON descriptors. Auto-detection uses the client's constructor name with a
structural fallback; pass `{ provider: "openai" }` to be explicit, or register a
custom adapter with `registerAdapter()`.

## Invariants

- **Opaque tunnel** — no function reads or writes preferences.
- **No local signing** — the ACT token is signed by the cloud; the SDK only
  carries it and can verify it from **public** keys. No private key ever lives
  here, and there is no counterpart to `verifyAct()`.
- **Fail-closed** — cloud unreachable / 5xx / malformed answer ⇒
  `ConsentUnreachableError`, and no provider call.
- **One token = one request** — bound to a `request_hash` computed over the
  RFC 8785 (JCS) canonical caller payload before enforcement (identity fields
  and per-request headers excluded; forced params and constraint changes happen
  afterward).

## Known limitations

- **ESM-only.** The published build is ES modules. Node's `require(esm)` covers
  CommonJS callers on Node ≥ 22.12, but a bundler targeting an older CJS-only
  runtime will need to transpile it.
- **Return type.** Because authorization is async, an enforced call returns a
  native `Promise`, not the provider's `APIPromise`. `.withResponse()` and
  `.asResponse()` are forwarded onto it, which is also what keeps Anthropic's
  `messages.stream()` working; other `APIPromise` members are not.
- **Enforced entrypoints.** Only the call sites in each provider's shared
  descriptor are enforced (e.g. `chat.completions.create` + `responses.create`,
  `messages.create`), together with their declared aliases (`parse`) and any
  convenience method that routes through them (in-place patching). Everything
  else on the same client — `embeddings`, `images`, `audio`, `moderations`,
  `batches`, file uploads, the `beta.*` namespace, a helper posting straight to
  the transport — is **refused rather than sent**: a guard on the client's
  `fetch` throws `UnenforcedCallError` for any body-bearing request that did not
  go through enforcement (`GET`/`HEAD`/`OPTIONS`/`DELETE` pass). `openai-node`
  re-wraps it as an `APIConnectionError` whose `cause` is the
  `UnenforcedCallError`, so search the cause chain for the first `IDSideError`
  rather than counting links
  ([how](https://github.com/idside-eu/idside-sdk/blob/main/docs/errors.md#finding-it-in-a-chain-of-causes)).
  Use a second, **unwrapped**
  client for non-inference traffic — there is no bypass option on a wrapped one.
  A client whose transport the SDK cannot find is **not wrapped**
  (`UnsupportedProviderError`): build Mistral with
  `httpClient: new HTTPClient()`, whose default HTTP client is out of reach.
- **One server.** `wrap()` binds the client to the origin of its base URL, and
  the guard refuses an authorized request sent anywhere else — a `baseURL`
  changed after wrapping included. A client passed in `credentials` is bound
  the same way, to its own server. An authorized request is sent with
  `redirect: "manual"`, so a 3xx comes back as a provider error instead of
  being followed — for Gemini too, whose `ApiClient.apiCall` the net masks on
  the instance, since `@google/genai` calls the global `fetch` there.
- **Client copies.** `client.withOptions(...)` / `client.copy(...)` come back
  wrapped — same adapter, same consent client — and so does anything derived
  from those, recursively. Their options are a closed list, checked before the
  copy is even constructed: `timeout`, `maxRetries`, `logger`, `logLevel`,
  `dangerouslyAllowBrowser` and the webhook secret pass; `baseURL` passes only
  to the same server, `defaultHeaders` only without a reserved header. A
  credential option (`apiKey`, `authToken`, `project`, `organization`,
  `azureADTokenProvider`, …) throws
  `CredentialRoutingError`: declare credentials to `consent.wrap(client, {
  credentials })` instead. Anything else (`fetch`, `fetchOptions`, `middleware`,
  `defaultQuery`, `provider`, Azure's `apiVersion` / `deployment` / `endpoint`,
  an unknown key) throws `UnsupportedProviderError`:
  construct a new client with it and wrap that one. A copy that still detects as
  another provider is refused the same way, and so is a `defaultHeaders` that
  would drop a reserved header the parent was built with.
- **Construction options.** A client built with an option that rewrites every
  request is not wrapped: a client-level `fetchOptions.body`, `.method` or
  `.headers` (the libraries spread it over the request they built, so its
  headers replace every header, the ACT token included), and Gemini's
  client-level `httpOptions.extraBody`, throw `UnsupportedProviderError`; a
  reserved header in `fetchOptions.headers` or in Gemini's `httpOptions.headers`
  throws `CredentialRoutingError`. Transport settings such as a proxy
  `dispatcher` are accepted.
- **What the SDK cannot reach.** Code given to a client when it is constructed
  — its own `fetch`, client-level `middleware`, Mistral HTTP hooks — is the
  developer's network layer, outside what a library running in the same process
  can judge. The proof records what was authorized.

## Development

Zero runtime dependencies. The sources are TypeScript and run natively on
**Node ≥ 23.6** (type stripping, no build step); tests use the built-in
`node:test` runner.

```bash
npm install
npm test          # node --test
npm run typecheck # tsc --noEmit (strict)
npm run build     # dist/ — what actually gets published
```

Publishing compiles to `dist/` because Node refuses to strip types for files
under `node_modules`: a package shipping raw `.ts` fails on the consumer's first
import with `ERR_UNSUPPORTED_NODE_MODULES_TYPE_STRIPPING`. `npm pack` and
`npm publish` run the build automatically via `prepack`. From the repository
root, `scripts/check_publishable.sh` packs both SDKs and exercises them from
outside the repo.

## Licence

Source-available, not open source. This package is licensed under the ID Side
Source-Available License, version 1.0 — `LICENSE` in this package, also
published at https://sdk.idside.eu/legal/sdk-licence. Clause 2 grants a
royalty-free licence to reproduce, execute and modify the SDK solely for
non-production internal evaluation, research, and non-commercial testing;
production deployment and any other commercial purpose require a paid
commercial licence. Beyond the limited evaluation patent licence of clause 2 of
`LICENSE`, rights under the patents are conveyed only by an executed commercial
licence. See `LICENSE` and `NOTICE` in this package.

Patents: https://sdk.idside.eu/patents
