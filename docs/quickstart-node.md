# Quickstart — Node.js

From nothing to a consent-enforced AI call with `@idside/sdk`. Same three steps
as [Python](./quickstart-python.md): install, wrap, call.

> **Status.** Pre-release: `@idside/sdk` is published on npm under the `next`
> dist-tag. A real call needs an ID Side account — sign up at
> <https://sdk.idside.eu/dashboard/signup>, the free plan covers 10,000 calls a
> month. [Step 6](#6-run-it-offline-without-an-account) runs the whole mechanism
> offline, with no account.

---

## 1. Requirements

- **Node.js ≥ 22.12** to *use* the SDK — what ships is compiled JavaScript with
  type declarations, and 22.12 is the floor `package.json` declares. Running the
  TypeScript sources in place (checkout paths **b** and **c** below) needs
  **Node ≥ 23.6** instead, for native type stripping. CI covers both floors: the
  test suite on 24.x, the packed tarball on 22.12.
- One provider client — `openai`, `@anthropic-ai/sdk`, `@google/genai` or
  `@mistralai/mistralai`. See [providers](./providers.md).
- No runtime dependencies: the SDK uses the built-in `fetch` and `node:crypto`.

## 2. Install

```bash
npm install @idside/sdk@next openai
```

Pre-releases are published under the `next` dist-tag, so name it. That is all
an application needs.

**From a checkout instead** — to read the code, run its tests or try a change:

```bash
git clone https://github.com/idside-eu/idside-sdk.git
cd idside-sdk/packages/node
npm install          # provider clients + TypeScript, for the test suite
npm test             # optional: the full suite, no network needed
```

Then choose one of the following install paths:

```bash
# a) pack it and install the tarball — the same artifact a release uploads
cd /path/to/idside-sdk/packages/node
npm pack   # `prepack` compiles to dist/ → idside-sdk-0.1.0-dev.0.tgz
cd /path/to/your-application
npm install /path/to/idside-sdk/packages/node/idside-sdk-0.1.0-dev.0.tgz
```

```bash
# b) link the local checkout — npm symlinks it, no build step
npm install file:/path/to/idside-sdk/packages/node
```

```ts
// c) or import the entry point by path, from the TypeScript sources
import { consent } from "/path/to/idside-sdk/packages/node/src/index.ts";
```

**(a) is the honest rehearsal**, and it works: `npm pack` runs `prepack`, which
compiles to `dist/`, so the tarball carries JavaScript and declarations and
installs on any Node ≥ 22.12. It is what `scripts/check_publishable.sh` does on
every CI run — pack, install into a project outside the repository, import by
package name, exercise the paths that read the bundled JSON — so a break here
turns the build red rather than turning up in your app.

**(b) and (c) run the TypeScript sources in place**, which needs Node ≥ 23.6.
(b) works only because npm *symlinks* a `file:` dependency: Node refuses to
strip types for files under `node_modules`, so a copied `.ts` tree would fail on
the first import with `ERR_UNSUPPORTED_NODE_MODULES_TYPE_STRIPPING`. That is
exactly why the package ships `dist/` and never raw sources — and why (a) is
unaffected.

## 3. Configure and call

```bash
# IDSIDE_BASE_URL is optional: https://sdk.idside.eu is the default.
export IDSIDE_BASE_URL="https://sdk.idside.eu"
export IDSIDE_API_KEY="sk_test_your_idside_key"
export OPENAI_API_KEY="sk-your_openai_key"
```

```ts
import OpenAI from "openai";
import { consent } from "@idside/sdk";

const client = consent.wrap(new OpenAI()); // 1. wrap the provider client, once

const response = await client.chat.completions.create({
  model: "gpt-4o-mini", //                    2. call it exactly as before…
  messages: [{ role: "user", content: "Summarize my last consultation." }],
  user_id: "usr_marie_456", //                3. …plus the end user's identity
});

console.log(response.choices[0].message.content);
```

`wrap()` patches the call methods in place on the client's resource objects and
returns the same client, so existing references stay enforced. Wrapping twice is
harmless — enforcement never stacks.

That snippet compiles as written under `strict`. `wrap()` gives back the client
with the identity typed onto every enforced call site, so there is no cast — and
omitting `user_id` is a compile error rather than a run-time one. See
[TypeScript](#typescript) below for what that costs.

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

| Setting | Option | Environment variable | Default |
| --- | --- | --- | --- |
| ID Side API key | `consent.wrap(client, { apiKey })` | `IDSIDE_API_KEY` | none (no `Authorization` header) |
| ID Side base URL | `consent.wrap(client, { baseUrl })` | `IDSIDE_BASE_URL` | `https://sdk.idside.eu` |
| Credentials to route between | `consent.wrap(client, { credentials })` | — | none (one credential, one posture for everyone) |
| Provider (skip auto-detection) | `{ provider: "openai" }` | — | auto-detected |
| Transport override (tests, custom timeout) | `{ consentClient }` | — | `HttpConsentClient` |
| Adapter override | `{ adapter }` | — | from the descriptor |

Auto-detection matches the client's constructor name (`OpenAI`, `Anthropic`,
`Mistral`, `GoogleGenAI`, and the Azure/Bedrock/Vertex variants), then falls back
to probing the descriptor's call sites. Pass `{ provider }` if you wrap a proxied
or subclassed client.

`credentials` is what makes training opt-out per-user. The privacy posture is
not a request parameter anywhere — it lives on the credential (an OpenAI
*project*, an Anthropic *workspace*, a Mistral *organisation*), so one key means
one posture for all your users. Provision one per posture, label each, and every
call goes out on the one that user's answer requires:

```ts
const client = consent.wrap(new OpenAI(), {
  credentials: { default: "proj_medhelp_default", zero_retention: "proj_medhelp_zdr" },
});
```

You label; ID Side chooses. A posture you have not provisioned stops the call
rather than falling back to the wrong one — except on an application that
serves employees and is set to Observe, where ID Side names no posture you did
not label: the call leaves on your ordinary credential and its proof records the
refusal as not applied (set to Enforce, the call is held instead). See
[credential routing](./providers.md#6-credential-routing).

The authorize timeout is 5 seconds — the same number the Python SDK uses, so
one outage stops both alike. To change it, build the transport yourself:

```ts
import { consent, HttpConsentClient } from "@idside/sdk";

const client = consent.wrap(new OpenAI(), {
  consentClient: new HttpConsentClient(process.env.IDSIDE_BASE_URL, {
    apiKey: process.env.IDSIDE_API_KEY,
    timeoutMs: 3_000,
  }),
});
```

**A cloud on your own machine.** `IDSIDE_BASE_URL` may point at an ID Side cloud
running locally, on `http://localhost:…`, and authorization works the same. One
thing does not: `verifyAct()` refuses on purpose to fetch a key set over
`http://` — public keys fetched over an unauthenticated channel prove nothing —
so `jwksUrl` throws a `TypeError` there. Fetch the key set yourself and pass it
as `jwks` ([step 7](#7-verify-a-proof-yourself)). That cloud is not production,
so it signs as the test issuer, `test.idside.eu`, which `verifyAct()` refuses
unless told otherwise — hence `allowTestIssuer: true` in that branch, and only
there (the *test tokens* paragraph of step 7). The `https://` branch assumes
production; any other cloud served over `https://` signs as the test issuer too,
and needs `allowTestIssuer: true` as well:

```ts
import { verifyAct } from "@idside/sdk";

const jwksUrl = `${process.env.IDSIDE_BASE_URL}/.well-known/jwks.json`;
const proof = await verifyAct(token, {
  ...(jwksUrl.startsWith("https://")
    ? { jwksUrl }
    : // a cloud on your own machine: its key set, and its test issuer
      { jwks: await (await fetch(jwksUrl)).json(), allowTestIssuer: true }),
  requestHash: expectedHash,
});
```

### TypeScript

`consent.wrap()` returns `Enforced<T>`: your client, with `user_id` / `ubt`
typed onto every enforced call site and nothing else changed.

```ts
import OpenAI from "openai";
import { consent, type Enforced } from "@idside/sdk";

const client: Enforced<OpenAI> = consent.wrap(new OpenAI());

await client.chat.completions.create({
  model: "gpt-4o-mini",
  messages: [{ role: "user", content: "Hello!" }],
  user_id: "usr_marie_456",   // required — omitting it does not compile
});
```

Forgetting the identity is a compile error, not a `MissingIdentityError` on your
first request; so is naming the user twice. The provider's own overloads
survive — `stream: true` still narrows the return to a stream — and the surfaces
the SDK does not enforce, `models.list()` and the rest, keep the types the
provider gave them.

**A wrapped client is not an `OpenAI`.** `Enforced<OpenAI>` demands more than
`OpenAI` does, so it is not assignable to it, and a function that receives one
has to say so:

```ts
async function summarise(client: Enforced<OpenAI>, userId: string) { … }
```

That is the type telling the truth rather than an implementation detail leaking:
a function annotated `OpenAI` is a function whose author may write a call with
no identity, and that call throws. The alternative — a type that merely *allows*
the identity — would let the omission through to run time, which is the whole
problem.

**Where the typing is thinner.** `parse()` and `stream()` are generic over the
schema you hand them, and rewriting a signature erases its type parameters, so a
parsed payload arrives as `unknown` and you narrow it yourself. Nothing
regressed: the cast this replaces erased them too (it yielded `null`). Nothing
in TypeScript can map over a generic signature's own type parameters, so this
one is structural, and written down rather than worked around.

None of this reaches JavaScript callers, who have no compiler in the way: for
them the identity is still checked on the first call, and still raises
[`MissingIdentityError`](./errors.md#missingidentityerror).

### Naming the end user

Exactly one of `user_id` or `ubt` per call; both are consumed by the SDK and
never forwarded to the provider. See
[Concepts §8](./concepts.md#8-naming-the-end-user-user_id-vs-ubt). Pass
`user_id` as a string: it is sent as written, and a number the cloud accepts
does not always name the same user from both SDKs — in JavaScript `42.0` is
`42` ([what a `user_id` may be](./errors.md#missingidentityerror)).

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
strictest parameters forced. Two refusals are louder than the rest. Pass `tools`
and the call rejects with
[`ToolUseRefusedError`](./errors.md#tooluserefusederror) instead of reaching the
provider, on every request and for every user, because a refusal of
`agentic_tools` is applied by declining the call rather than by rewriting a
parameter. And where a refusal can only be honoured by the credential a call
goes out on — training on Mistral, for one — the call rejects with
[`MissingCredentialError`](./errors.md#missingcredentialerror) until you
provision and label that credential. Running the
[Preference Center](./widget.md) is what turns the first into a question
somebody can answer. It is one `<script>` tag — plus one line in your pages'
`Content-Security-Policy` if they send one, because a strict `frame-src` refuses
the widget's iframe
([widget §1](./widget.md#if-your-pages-send-a-content-security-policy)).

## 4. What just happened

Identical to the Python flow, and byte-compatible with it: hash the request, ask
`POST /v1/authorize` (identifier + provider + model + hash — never the prompt),
apply the parameters the cloud decided, attach the signed ACT token as
`X-AI-Consent-Token`, then call the provider. If any step fails, the provider
call does not happen at all.

See [Python quickstart §4](./quickstart-python.md#4-what-just-happened) for what
the token proves and where to find the ledger entry — none of it is
language-specific.

## 5. Handle the errors that matter

```ts
import { ConsentDeniedError, ConsentUnreachableError } from "@idside/sdk";

try {
  const response = await client.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: prompt }],
    user_id: "usr_marie_456",
  });
  return response.choices[0].message.content;
} catch (error) {
  if (error instanceof ConsentDeniedError) {
    // Not this user's choice: the account or the key behind your app cannot
    // authorize, or an organization's policy held the call. A user's refusal never
    // lands here. Do not retry.
    return "AI summaries are not available.";
  }
  if (error instanceof ConsentUnreachableError) {
    // Consent could not be verified, so no AI call was made. Fail-closed.
    return "Summaries are temporarily unavailable. Please try again shortly.";
  }
  throw error;
}
```

All nineteen enforcement error types inherit `IDSideError` and set `error.name`
to the concrete class name; local token verification adds twelve more. The
[error catalogue](./errors.md) covers each one — including why a wrapped client
surfaces `UnenforcedCallError` inside an `APIConnectionError`. How deep an ID
Side error sits in a chain of causes depends on the provider client, so search
the chain for the first `IDSideError` rather than walk it to the end
([how](./errors.md#finding-it-in-a-chain-of-causes)).

### When a call did not go out, and you need to know why

The SDK journals through `util.debuglog`, Node's own channel. It writes nothing
unless you ask, and asking is an environment variable:

```bash
NODE_DEBUG=idside node app.js
```

```
IDSIDE 4172: wrap provider=openai call_sites=4 mode=async net=true
IDSIDE 4172: authorize call_site=chat.completions.create model=gpt-4o-mini subject=8f1c2ab90d47 binding=ubt request_hash=ffb904460a38 duration_ms=41 forced=store
IDSIDE 4172: enforced call_site=chat.completions.create forced=store routed=false
```

Six events: `wrap` (which descriptor matched, how many entry points were
patched, what the net came to), `authorize` (what the cloud answered and how
long it took), `authorize_failed`, `enforced` (the call is going out),
`refused` (a constraint stopped it locally) and `passthrough` (an alias that was
already enforced upstream). They go to stderr, never to stdout. (Python has a
seventh, `resume`, for an overloaded helper it patches and this SDK does not.)

`net=` has three values and they are not two: `true` means the client's
transport is guarded, `false` means it owns none to guard, and `partial` means a
route out is — or may be — unguarded, because the search for it was cut short or
the transport would not take the patch. Only the first is a covered client;
enforcement on the declared call sites holds in all three.

The Python SDK emits the same events with the same fields through `logging`, and
`scripts/check_sdk_parity.sh` runs both and diffs the lines: a developer
debugging one SDK reads the same story in the other. The `authorize` and
`enforced` lines are identical; the `wrap` line differs on purpose, in two
fields. `mode=` is `blocking` or `async` in Python, which wraps both shapes of
client. `call_sites=` counts more entry points there for the same client — 6
for an OpenAI client, where this SDK counts 4 — because Python also patches the
`stream()` helpers, which this SDK leaves to reach the `create` it patched. The
same helper is why Python has `resume`.

**Nothing secret is ever written.** Not your ID Side key, not your provider key,
not the end user's `user_id` or `ubt`, not the prompt, not the ACT token, not a
credential. `subject` is a pseudonym salted with a random per-process value: two
calls by one person share it, so you can follow one user through a log, and
nobody can work backwards from it to the identifier — not even for a guessable
one like `usr_marie_456`, which is why the salt is there. `request_hash` is the
value the ledger indexes, so it is how you find the proof for a given call.

Two deliberate absences. The **ACT token** is never logged: it is bearer
material, and the request hash already answers the question you would have
wanted it for. **`consentState`** is never logged either — what a person
answered is theirs; what it forced on your request is `forced=`, which is the
part you are debugging.

The [net](./providers.md#33-the-net-what-a-wrapped-client-refuses-to-send)
writes nothing, ever, by design. If a call was refused there, you learn it from
the `UnenforcedCallError` and not from a log line: a guard that recorded the
paths it saw would be recording your requests.

## 6. Run it offline, without an account

No ID Side account, no OpenAI bill: inject a fake cloud through `consentClient`
and a stub `fetch` into the OpenAI client, then read what actually left. This is
one file to copy; [`examples/node/`](../examples/node/) is the same call as a
project you can clone and run, in TypeScript and with no cast.

```js
// dryrun.mjs — everything here is fake except the ID Side SDK itself.
import OpenAI from "openai";
import { consent } from "@idside/sdk";

// Stands in for idside.eu. The real cloud signs the ACT token.
const fakeCloud = {
  async authorize({ provider, model, requestHash, callSite, userId, ubt, credentialPostures }) {
    console.log(`[cloud] authorize: provider=${provider} model=${model} callSite=${callSite}`);
    console.log(`[cloud] requestHash=${requestHash}`);
    console.log(`[cloud] identity: userId=${userId} ubt=${ubt}  <- no prompt here`);
    console.log(`[cloud] credentialPostures=${JSON.stringify(credentialPostures)}  <- labels, never keys`);
    return {
      consentState: { training: false },
      // Decided for THIS endpoint: the real cloud answers `chat.completions.create`
      // and `responses.create` differently, because they do not accept the same
      // parameters.
      paramsForced: { store: false },
      // What to refuse, as opposed to what to set. `{ tools: "refuse" }` here
      // would make a call passing `tools` throw ToolUseRefusedError.
      constraints: {},
      actToken: "act.eyJhbGciOiJFUzI1NiJ9.fake.signature",
      source: "widget",
      legalBasis: "consent",
      binding: "user_id",
      // Which of the developer's credentials must carry this call. Naming one
      // they did not provision throws MissingCredentialError instead of quietly
      // sending it on another. Undefined here: this app has one key.
      credentialPosture: undefined,
    };
  },
};

// Stands in for api.openai.com so we can read what actually left.
const openaiStub = async (url, init) => {
  const headers = new Headers(init?.headers);
  console.log(`[openai] X-AI-Consent-Token: ${headers.get("X-AI-Consent-Token")}`);
  console.log(`[openai] body: ${init?.body}`);
  return new Response(
    JSON.stringify({
      id: "chatcmpl_1",
      object: "chat.completion",
      created: 0,
      model: "gpt-4o-mini",
      choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
};

const client = consent.wrap(
  new OpenAI({ apiKey: "sk-test-not-a-real-key", fetch: openaiStub, maxRetries: 0 }),
  { consentClient: fakeCloud },
);

await client.chat.completions.create({
  model: "gpt-4o-mini",
  messages: [{ role: "user", content: "Hello!" }],
  user_id: "usr_marie_456",
});
```

```console
$ node dryrun.mjs
[cloud] authorize: provider=openai model=gpt-4o-mini callSite=chat.completions.create
[cloud] requestHash=sha256:3ff33a93d528feaaa859078c68af6a379567b262ed5600b5bd91300673f5cf76
[cloud] identity: userId=usr_marie_456 ubt=undefined  <- no prompt here
[cloud] credentialPostures=[]  <- labels, never keys
[openai] X-AI-Consent-Token: act.eyJhbGciOiJFUzI1NiJ9.fake.signature
[openai] body: {"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hello!"}],"store":false}
```

The cloud saw a hash, not the prompt; `store: false` — the cloud's decision —
reached OpenAI; `user_id` did not. And the `requestHash` is character-for-character
the one the [Python script](./quickstart-python.md#6-run-it-offline-without-an-account)
prints for the same payload. That cross-language agreement is what makes the hash
inside an ACT token independently checkable; it is pinned by
[shared test vectors](../packages/descriptors/testdata/request-hash-vectors.json)
both SDKs replay in CI.

## 7. Verify a proof yourself

A proof nobody but the issuer can check is not a proof. `verifyAct()` checks an
ACT token **offline** — JWS structure, `alg` pinned to `ES256`, `typ` =
`act+jwt`, key selection by `kid`, the signature, the mandatory claims, the
issuer, the time window, and any binding you pin. It uses `node:crypto`, so it
needs no extra dependency.

```ts
import { verifyAct } from "@idside/sdk";

const proof = await verifyAct(token, {
  jwks, //                        or: jwksUrl: "https://sdk.idside.eu/.well-known/jwks.json"
  requestHash: expectedHash, //   pin the request this proof must belong to
});

console.log(proof.appId, proof.provider, proof.model, proof.binding, proof.mode);
// app_medhelp openai gpt-4o-mini ubt live
console.log(proof.consentState, proof.source, proof.legalBasis);
// { training: false } widget consent
```

`verifyAct(token, options)` is async (it may fetch a JWKS) and resolves to a
`VerifiedAct`: `issuer`, `subject`, `appId`, `tokenId`, `issuedAt`, `expiresAt`,
`consentState`, `legalBasis`, `source`, `provider`, `model`, `requestHash`,
`nonce`, `binding`, `mode`, `keyId`, and the raw `claims`. Anything that does not check
out throws an [`ActVerificationError`](./errors.md#verification-errors) subclass;
resolution *is* the success signal.

Which layer refused each option — the person, their silence, the organization,
a team or the application — is the `refused_by` claim, read from `claims`:

```ts
const refusedBy = proof.claims.refused_by; // `unknown`; undefined on a token older than the claim
// { agentic_tools: ["person", "organization"], memory: ["silence"] }
```

It has no field of its own on purpose. Its values are opaque and a layer can be
added later: an SDK holding a closed list of layers would reject valid tokens
the day a new one appeared, and since the SDK is fail-closed every older copy
in production would stop its AI calls. Read the values as strings; the
[spec](./spec/act-token.md#refused_by--who-refused) lists today's.

`VerifyActOptions`: `jwks` **or** `jwksUrl` (exactly one — there is no ambient
key source; the URL must be `https` and is fetched on every call, so a cloud on
your own machine needs `jwks`, [see §3](#3-configure-and-call)), `issuer`,
`allowTestIssuer`, `allowLegacyWithoutMode`, `leeway` (default 60 s), `now` (audit a past call — "was this
proof valid when the call was made?"), `requestHash`, `nonce`, `subject`,
`appId`, `timeoutMs` (JWKS fetch, default 5 s) and an injectable `fetch`.

Note there is no `userId` option: the token's `sub` is the app-scoped
*pseudonym* the cloud derived from the end user, so the `user_id` you passed to
`consent.wrap()` never enters the proof. Pin `subject` if you hold the
pseudonym.

`now` matters when auditing: ACT tokens live one hour, so an archived proof is
expired against the wall clock —
`verifyAct(token, { jwks, now: ledgerEntry.issuedAt })` asks "was this proof
valid when the call was made?" instead. It moves **only** the `exp` / `iat` /
`nbf` window; the signature, `alg`, `typ`, issuer and pinned bindings are checked
identically, so a generous `now` can never turn a forgery into a proof.
The declared `expiresAt - issuedAt` interval must also be at most 3600 seconds.

Test tokens do not pass as production: `test.idside.eu`, the issuer of every
cloud that is not production, is rejected unless you set
`allowTestIssuer: true`.

Current tokens must expose `proof.mode` as `"test"` or `"live"`. Missing mode is
malformed by default. Set `allowLegacyWithoutMode: true` only while reading a
known historical archive; the compatibility result then has
`proof.mode === undefined`.

Pinning `requestHash` is the check that matters: a valid signature only says
"ID Side issued this", while the hash says "…for *this* request". You can
compute it from a payload with the exported primitive:

```ts
import { requestHash } from "@idside/sdk";

// The caller payload before enforcement, minus identity and request headers.
requestHash({ model: "gpt-4o-mini", messages: [{ role: "user", content: "Hello!" }] });
// → "sha256:3ff33a93d528feaaa859078c68af6a379567b262ed5600b5bd91300673f5cf76"
```

This is the payload at the authorization boundary. Parameters later forced by
the cloud, local constraint transformations, routed credentials and the ACT
header itself are not added to the hash.

> Where do you get a token to verify? From the ledger export or the dashboard,
> from whoever is auditing you, or by capturing the outgoing
> `X-AI-Consent-Token` header. The enforcement path does not return the token to
> the caller today.

Python's `verify_act()` accepts the same tokens with the same rules
([quickstart](./quickstart-python.md#7-verify-a-proof-yourself)), and the
[spec](./spec/act-token.md#5-verification) documents the procedure for any other
language.

## Known limitations

- **Return type.** Because authorization is async, an enforced call returns a
  native `Promise`, not the provider's `APIPromise`. `.withResponse()` and
  `.asResponse()` are forwarded onto it — `{ data, response }` and the raw
  `Response`, one authorization and one request each — which is also what keeps
  Anthropic's `messages.stream()` working; the `APIPromise`'s other members are
  not. Plain `await`, `.then`, `.catch` and `.finally` are unaffected.
- **Enforced entry points** are exactly those in each provider's
  [descriptor](./providers.md#3-intercepted-call-sites), plus their `parse`
  aliases. The `stream()` helpers are enforced too, without being patched: they
  hand their body — identity included — to the patched `create`, which keeps
  them returning a stream object rather than a promise. Everything else on a
  wrapped client — `embeddings`, `images`,
  `audio`, `moderations`, `batches`, file uploads, the `beta.*` namespaces — is
  **refused**, not silently sent: any body-bearing request that did not go
  through enforcement raises
  [`UnenforcedCallError`](./errors.md#unenforcedcallerror) before it leaves the
  process, and `openai-node` surfaces it as an `APIConnectionError` whose
  `cause` is that error. Build a second, **unwrapped** client for that traffic;
  there is no bypass option on `wrap()`. `GET`/`HEAD`/`OPTIONS`/`DELETE`
  (`models.list`, `files.delete`, polling a response id) pass through untouched.
- **`client.withOptions(...)` / `client.copy(...)` come back enforced**, and so
  does anything derived from those, recursively.
- **A wrapped client is not the provider's own type.** `Enforced<OpenAI>` is not
  assignable to `OpenAI`, and `parse()` / `stream()` lose the generic that types
  their parsed payload. Both are explained under
  [TypeScript](#typescript); neither has a workaround that keeps the identity
  required at compile time.

## Next

- [Concepts](./concepts.md) — the opaque tunnel, and why there is no preference setter
- [Error catalogue](./errors.md) · [Providers](./providers.md) · [FAQ](./faq.md)
- [`packages/node/README.md`](../packages/node/README.md) — package-level reference
