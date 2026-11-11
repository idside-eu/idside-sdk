# FAQ

The questions a careful engineer asks before putting something on the request
path of every AI call in production.

- [What happens if ID Side is down?](#what-happens-if-id-side-is-down)
- [How much latency does this add?](#how-much-latency-does-this-add)
- [Is there a cache?](#is-there-a-cache)
- [What data does ID Side see — and not see?](#what-data-does-id-side-see--and-not-see)
- [What is the GDPR posture?](#what-is-the-gdpr-posture)
- [What happens when my quota runs out?](#what-happens-when-my-quota-runs-out)
- [How do I verify a token myself?](#how-do-i-verify-a-token-myself)
- [Can the SDK be bypassed?](#can-the-sdk-be-bypassed)
- [Why can't I set a user's preferences from my backend?](#why-cant-i-set-a-users-preferences-from-my-backend)
- [Does anyone have to see a popup?](#does-anyone-have-to-see-a-popup)
- [Do the AI providers actually read the ACT token?](#do-the-ai-providers-actually-read-the-act-token)
- [Does this work with streaming? With async? With my framework?](#does-this-work-with-streaming-with-async-with-my-framework)
- [What if my provider isn't supported?](#what-if-my-provider-isnt-supported)
- [Is this open source?](#is-this-open-source)

---

## What happens if ID Side is down?

**The AI call does not happen.** The SDK raises `ConsentUnreachableError` and
stops — no call to OpenAI, no partial state, nothing in flight. Same answer for a
timeout, an HTTP 5xx, or a 200 whose body the SDK cannot fully parse.

This is [fail-closed](./concepts.md#9-fail-closed), and it is the right default
for one reason: the alternative fails *silently*. A fail-open SDK would keep
serving traffic during an outage while quietly producing AI calls with no proof
attached — and you would discover the hole months later, in an audit, on exactly
the calls you can no longer reconstruct. A visible outage is a worse afternoon
and a better year.

What to do about it in your code:

- catch `ConsentUnreachableError` at your AI boundary and degrade the feature
  (queue the work, serve a non-AI answer, tell the user to retry);
- alert on its rate — it is your canary for a network path or a provider-side
  problem;
- decide about retries at your layer. The SDK deliberately does not retry:
  `authorize` is not idempotent, and a request that reached the cloud before the
  connection dropped has already minted a token and written a ledger entry.
  Retrying blindly would risk duplicate entries in an append-only audit log.

If the outage is on your side of the network, note that the SDK's timeout is
short by design — **5 seconds, the same number in both SDKs**: a half-open
connection must fail fast rather than hang your request behind the provider call
it is guarding.

## How much latency does this add?

One HTTPS round trip to `sdk.idside.eu`, before each AI call, on the same
critical path. There is no benchmark published here — anyone quoting you a
millisecond figure that does not include *your* region, *your* egress and *your*
connection reuse is selling something. Measure it yourself; the numbers you can
rely on from this repository are the ceilings:

| SDK | Authorize deadline | Retries |
| --- | --- | --- |
| Python | 5 s | none |
| Node.js | 5 s | none |

One number, deliberately, and the same one on every phase of the hop. On a
fail-closed path a timeout is not a tuning knob: it is the moment the SDK
declares the cloud unreachable and stops making AI calls. Two SDKs with two
numbers would mean one network incident cutting your Python service and sparing
its Node.js equivalent. `scripts/check_sdk_parity.sh` runs both and compares the
deadline each one actually arms.

Both SDKs keep a client alive across calls (`httpx.Client` / `fetch` keep-alive),
so the TLS handshake is paid once per connection, not once per call. In practice
the authorize hop is small next to the AI call it precedes — a model generating
tokens is orders of magnitude slower than a JSON round trip — but "small" is not
"free", and you should see it in your traces.

It is configurable in both: build the transport yourself and pass it in.

```python
from idside import HttpConsentClient, consent

client = consent.wrap(OpenAI(), consent_client=HttpConsentClient(timeout=3.0))
```

```ts
const client = consent.wrap(new OpenAI(), {
  consentClient: new HttpConsentClient(baseUrl, { apiKey, timeoutMs: 3_000 }),
});
```

Shortening it makes the SDK give up sooner; lengthening it makes your own
request wait longer on an outage. Neither ever lets an unauthorized AI call out.

## Is there a cache?

**No. Today every enforced AI call performs one `authorize` round trip.** No
decision is cached, in either SDK.

A short-TTL cache of signed proofs is a designed-for future option — it has to be
signed, short-lived and bound to a consent-state version, otherwise it would let
a stale "opt-in" outlive the user's opt-out, which is precisely the failure the
product exists to prevent. When it ships it will be documented here with its
exact semantics. Until then, assume one hop per call in your capacity planning.

## What data does ID Side see — and not see?

The authorize request is deliberately minimal. It contains exactly six fields,
five of them always:

| Sent | Not sent |
| --- | --- |
| `user_id` **or** `ubt` — exactly one | your prompts and messages |
| `provider` (`"openai"`…) | system prompts, tools, documents |
| `model` (`"gpt-4o-mini"`…) | the model's response |
| `request_hash` (`sha256:…`) | attachments, embeddings, user content of any kind |
| `credential_postures` — the *labels* of the credentials you provisioned, never their values; an empty list if you passed none | your API keys, ID Side's or the provider's |
| `call_site` (`"responses.create"`…), when the SDK knows which entry point it is on | which of your users, endpoints or features it came from |

`credential_postures` and `call_site` are there because the cloud has to decide
per endpoint: two endpoints of the same provider do not accept the same
parameters (`background` exists on `/v1/responses` and nowhere else), and the
posture the answer names has to be one you actually hold. Neither carries
content. The same six are listed in
[`ARCHITECTURE.md` §2](./ARCHITECTURE.md#2-the-two-circuits).

The SDK is *structurally* unable to send your prompt: the payload is built field
by field in
[`_transport.py`](../packages/python/src/idside/_transport.py) /
[`transport.ts`](../packages/node/src/transport.ts) — read them, they are short —
and the only thing derived from the request body is a one-way hash.

Two consequences worth being explicit about:

- **The hash is one-way but not anonymous.** Anyone who already holds a candidate
  request can check whether it produced a given hash. That is the entire point —
  it is what makes "this token belongs to this request" verifiable. It does not
  let ID Side reconstruct your prompt.
- **The identifier is yours.** ID Side stores a per-app pseudonymous form (`sub`
  in the token is a `user_pseudo_id`, scoped per app, never a raw identifier).
  Pass an opaque internal id rather than an email address, and the pseudonym
  chain stays clean end to end.

## What is the GDPR posture?

This is engineering documentation, not legal advice — but the design maps onto
the regulation deliberately, and here is how.

- **Roles.** You remain the controller for your AI processing. ID Side is your
  **processor** for the consent records it holds on your instructions; the widget
  path means the user's preference is collected by ID Side directly rather than
  relayed by you. For four narrow things done for its own purposes — your
  dashboard accounts, licence enforcement, aggregated de-identified analytics and
  security logging — it is an **independent controller** instead. The split is
  not cosmetic: it decides who answers a data-subject request, and the published
  SDK privacy policy sets it out in full.
- **Article 7(1) — demonstrating consent.** The controller must be able to
  *demonstrate* that consent was given. A row in your database is an assertion;
  a signed, request-bound token plus an append-only ledger entry is evidence.
  That gap is the product.
- **Article 30 — records of processing.** The ledger is a per-call record of
  which user, which provider, which model, on what basis, from what source.
- **Articles 15–17 — data subject requests.** Because the ledger is queryable per
  user, "what was done with my data and under what consent" has an answer that
  does not depend on your logging discipline.
- **Article 25 — data protection by design.** Your backend never being in the
  preference path is not a policy you enforce; it is a shape the API has.
- **Data minimization.** See the table above: identifier, provider, model, hash.
- **Data location and retention.** The public endpoint is `sdk.idside.eu`.
  Consent-event logs are kept up to 13 months by default, diagnostic and support
  data 30 days, crash data 180 days. Where the infrastructure sits, and on what
  basis anything leaves the EEA — an adequacy decision *as amended or replaced*,
  and failing one, Chapter V standard contractual clauses with whatever
  supplementary measures apply — is stated in the published policies, and those
  are what bind rather than this page:
  <https://sdk.idside.eu/legal/sdk-privacy> for the SDK, <https://sdk.idside.eu/privacy>
  for the rest, and <https://sdk.idside.eu/legal/sub-processors> for who else touches
  it. An adequacy decision is a time-limited instrument, so a page that named one
  without that qualifier could go on asserting a basis that had lapsed. What your
  own contract says still governs where it and the policies differ.

None of this makes an application compliant on its own. It makes one part you
cannot otherwise show — *which choices a specific call was authorized against,
and what was forced on it* — verifiable.

## What happens when my quota runs out?

`QuotaExceededError` (HTTP 402), and the AI call does not happen.

There is no silent degradation, and that is intentional: letting calls through
unproven because a billing counter rolled over would produce exactly the
untraceable gap the system exists to prevent. Practically:

- alert on `QuotaExceededError` — when it fires, calls are already failing;
- watch usage in the dashboard rather than waiting for the error;
- the fix is an upgrade or the next quota window, not a code change.

## How do I verify a token myself?

Two ways, neither requiring ID Side's cooperation for the important one.

**Locally, offline** — the strong check:

1. fetch the public keys from `GET /.well-known/jwks.json` and select the one
   matching the token header's `kid`;
2. verify the signature, **pinning `alg` to `ES256`**. A token declaring `none`
   or an HMAC algorithm is a forgery attempt, not a negotiation;
3. check `exp`, require `exp - iat <= 3600`, validate `mode` (`test` or `live`),
   and check `iss`;
4. recompute `request_hash` from the caller payload captured before enforcement
   (identity and request headers removed; forced parameters and constraints have
   not yet been applied), and
   compare. A token whose hash does not match its request is invalid however
   well it is signed — **one token = one request**.

Both SDKs package all four steps in one call:

```python
from idside import verify_act  # pip install 'idside[verify]'

proof = verify_act(
    token,
    jwks_url="https://sdk.idside.eu/.well-known/jwks.json",
    request_hash=expected_hash,  # idside.request_hash(payload), step 4
)
```

```ts
import { verifyAct } from "@idside/sdk"; // node:crypto, no extra dependency

const proof = await verifyAct(token, {
  jwksUrl: "https://sdk.idside.eu/.well-known/jwks.json",
  requestHash,
});
```

Anything short of a full pass raises — there is no boolean to misread, and a
token from a deployment of the cloud that is not the production service
(`iss: test.idside.eu`) is refused unless you ask for it explicitly. Proofs
without `mode` are also refused unless a verifier explicitly enables the legacy
option for a known historical archive. One thing a pass does not say is that the
proof is production evidence: a call made with an `sk_test_` key on the
production service carries `iss: idside.eu` and verifies, and only
`proof.mode` reads `"test"`. Require `proof.mode == "live"` yourself before you
count a proof. Full signatures in the quickstarts
([Python](./quickstart-python.md#7-verify-a-proof-yourself) ·
[Node.js](./quickstart-node.md#7-verify-a-proof-yourself)) and the failure modes
in the [verification errors](./errors.md#verification-errors).

Step 4 is where canonicalization matters, and where the SDKs help: both export
the function they hash every call with — `request_hash(payload)` in Python,
`requestHash(payload)` in Node — and both replay the
[shared hash vectors](../packages/descriptors/testdata/request-hash-vectors.json)
in CI so that a hash computed in Python, in Node, or by your auditor's own
implementation agrees byte for byte. A second set of
[verification vectors](../packages/descriptors/testdata/act-verification-vectors.json)
does the same for the verdict itself — `alg: none`, HS256 confusion, a UBT passed
off as an ACT, a tampered payload, an unknown `kid`, a development token — so
both SDKs accept and reject exactly the same tokens, with the same error class.

One thing offline verification cannot do is notice that a token was replayed
across *different* calls — a verifier has no history; that is the ledger's job.
What it proves is that this signed token belongs to this exact request, which is
the claim that matters when you are asked about one call.

**Remotely** — `GET /v1/verify/{token_id}`, public, no API key, no account. It
answers *"is this token genuine, and is it still valid?"* and deliberately omits
`sub` and `consent_state`: an endpoint that revealed a person's choices to anyone
holding a token id would be a privacy hole dressed as transparency. Full
procedure and response shape in
[`spec/act-token.md` §5](./spec/act-token.md#5-verification).

## Can the SDK be bypassed?

Yes — trivially, and the design says so out loud. The SDK runs in your process.
Nothing stops a developer from calling `openai.chat.completions.create()`
directly, or from constructing a second, unwrapped client.

The model is **deterrent and evidentiary**, not coercive:

- a bypassed call produces **no ACT token and no ledger entry**;
- so the developer has nothing to show an authority for that call;
- and the gap is visible as a discontinuity between traffic and proof.

Two mechanisms narrow the gap for the honest-but-imperfect case, which is the
common one. First, in-place patching: `wrap()` patches methods on the client's
resource objects, so a helper that internally calls `create()` on that object is
enforced too — you cannot accidentally route around it by using a convenience
method. Second, the [UBT](./concepts.md#8-naming-the-end-user-user_id-vs-ubt):
with a widget-issued binding, even the developer cannot substitute one user's
consent for another's, because the identity is signed by ID Side rather than
asserted by the caller.

Third, the [net](./providers.md#33-the-net-what-a-wrapped-client-refuses-to-send):
what remains outside the descriptors' declared call sites — `beta.*` namespaces,
`embeddings`, uploads, a helper posting straight to the transport — no longer
escapes quietly. A wrapped client refuses to send any body-bearing request that
did not go through enforcement, raising
[`UnenforcedCallError`](./errors.md#unenforcedcallerror). Non-inference traffic
belongs on a second, unwrapped client; there is no bypass option on a wrapped
one.

## Why can't I set a user's preferences from my backend?

Because if you could, the proof would be worth nothing.

A signed token attests to what the signer verified. If the SDK accepted
preferences from the caller, an ACT token would only ever certify *what the
developer said the user wanted* — and an auditor could not distinguish an honest
integration from one that collects an opt-out in a dialog and passes an opt-in to
the SDK. Every token in the ecosystem would inherit that doubt.

So the SDK exposes no `set_preferences()`, no `override_consent()`, and no getter
you could round-trip. Preferences travel from the user to ID Side on a path your
backend is not on. This is the [opaque tunnel](./concepts.md#3-the-opaque-tunnel),
and the narrowness is the feature.

If you need to *see* consent state for your own UI, that is a dashboard and
account-side concern, not an SDK one.

**"But my organization decides this for its staff."** That case exists and is
supported — it is the *imposed* mode
([Concepts §4](./concepts.md#4-two-modes-asked-and-imposed)): a policy set once,
applying to the organization's employees, with no popup and nothing an employee
can change. It is still not a setter you reach from your backend, and it has two
limits that follow from the same reasoning as above. A policy can only tighten,
never switch an option back on, so it cannot be used to overturn a refusal
someone already made. And a declared `legal_basis: "consent"` is the
organization's declaration, not an answer collected from the employee, which
is generally presumed not to be freely given at work. What is owed to the person instead is
transparency: a statement of what was decided, by whom, and on what basis.

Which is also why the imposed mode does not solve "I would rather not show my
visitors a popup". Pointed at people who are not your employees, it produces
proofs that record that no basis was established — see the worked case in
[Concepts §4](./concepts.md#the-mistake-worth-naming).

## Does anyone have to see a popup?

Somebody has to see the widget either way. What changes is whether it asks them
anything — and that depends on something you do not get to choose.

There are two modes ([Concepts §4](./concepts.md#4-two-modes-asked-and-imposed)),
and the SDK is identical in both:

- **Asked.** The [Preference Center](./widget.md) puts the question to the
  person. Their answer is theirs; the proof records `legal_basis: "consent"`.
- **Imposed.** An organization sets a policy for its employees. Nobody is asked
  and no employee can change anything — but the
  [same widget still renders](./widget.md#11-the-imposed-mode), as a read-only
  panel stating the legal basis and what was decided about them. Not being asked
  is precisely why they have to be told.

Which one applies is a fact about the relationship, not a setting. If the people
using your product are your users, the first one is the only one that collects
consent from them, and it costs a `<script>` tag. If they are your staff, the second is the
only honest one, because an answer an employee gives their employer is not freely
given.

There is a third possibility, and it is worth pricing rather than passing over:
run no widget at all for people you meant to ask. You can, and most calls still
go out — but **what nobody accepted is refused**, so every option in the
catalogue resolves to `false` for every visitor you name, on every provider. You
notice it first on tool use, because that refusal is applied by declining the
call ([`ToolUseRefusedError`](./errors.md#tooluserefusederror)) rather than by
rewriting a parameter: an agentic integration with no widget fails on every
request. A refusal that names a credential posture you have not provisioned —
a training refusal on Mistral, for one — fails the same way, with
[`MissingCredentialError`](./errors.md#missingcredentialerror). The rest is
quieter and worse — a year of proofs reading
`not_established`, over capabilities you are paying a provider for and nobody
ever agreed to.

The failure mode worth naming: choosing "imposed" to avoid a popup for ordinary
visitors. It does not even buy you that — the panel still renders, it just
stops asking. Nothing stops you, and nothing breaks — you simply end up with a year
of signed, unalterable proofs saying *no basis was established* — about people
you chose not to ask — in the exact document they were built to be read from.
[Concepts §4](./concepts.md#the-mistake-worth-naming) works it through.

## Do the AI providers actually read the ACT token?

Not today. No provider currently interprets the `X-AI-Consent-Token` header —
they ignore unknown headers, which is exactly why it is safe to send.

Enforcement therefore happens on two tracks
([Concepts §11](./concepts.md#11-where-the-enforcement-actually-happens)):
**Type 1**, the provider parameters the cloud forces (`store: false` and friends),
which providers do read and apply today; and **Type 2**, the token, which is
audit proof today and an interoperability bet for tomorrow. If providers adopt
the [ACT specification](./spec/act-token.md) — its stated goal is to become a
standard — Type 2 absorbs Type 1 and the provider enforces consent directly.

For Gemini and Mistral, which expose no per-request retention parameter at all,
Type 2 is the entire enforcement value today. See
[providers](./providers.md#what-actually-gets-forced-in-practice).

## Does this work with streaming? With async? With my framework?

**Streaming: yes**, where the descriptor declares the streaming entry point —
OpenAI's `stream=True` on the same `create` call, Mistral's `chat.stream`,
Gemini's `generate_content_stream`. Authorization happens before the stream
opens, so a denial stops the call before a single token is generated.

**Async: yes, in both SDKs.** The Node SDK is async throughout. In Python,
`consent.wrap(AsyncOpenAI())` gives you the same guarantees as the blocking
client — same forced parameters, same ACT token, same `request_hash`, same
errors — with no separate entry point and nothing to configure. The wait for
`/v1/authorize` is awaited rather than blocked, which is what makes the SDK
usable from FastAPI or any other ASGI application: your event loop keeps serving
every other request in flight.

One exception, and it fails loudly rather than quietly: `google-genai` exposes
its async methods at `client.aio.*`, which no descriptor declares. Those calls
are **refused** with `UnenforcedCallError`, not enforced. The async clients of
`openai` and `anthropic` are fully enforced.

**Frameworks (LangChain, LlamaIndex, agent runtimes):** the SDK patches methods
on the client instance you hand it. So a framework that you *give* a wrapped
client to will make enforced calls; a framework that constructs its own provider
client internally will not, unless you can inject yours. This follows from the
in-place patching design, but it is not covered by the test suite — verify it in
your own stack with the [dry-run script](./quickstart-python.md#6-run-it-offline-without-an-account),
which shows you exactly what reaches the wire.

## What if my provider isn't supported?

Four providers ship today: OpenAI, Anthropic, Gemini, Mistral — plus every
OpenAI-compatible API (Groq, Together, xAI, DeepSeek, vLLM…) through the `openai`
client at no extra cost.

Beyond those, adding a provider is usually a JSON file, not code: a
[descriptor](./providers.md#5-the-descriptor-format) declares which methods to
intercept and where to inject. Clients with unusual APIs get an adapter subclass
instead — Gemini is the shipped example. Outside contributions are not
accepted yet — [`CONTRIBUTING.md`](../CONTRIBUTING.md#license-of-contributions)
says why; to propose a provider, write to contact@idside.eu.

## Is this open source?

**No, and the distinction is load-bearing rather than pedantic.** This
repository is *source-available*, under the ID Side Source-Available License,
version 1.0 — [`LICENSE`](../LICENSE), also published at
<https://sdk.idside.eu/legal/sdk-licence>. You can read every line that touches
your requests. Clause 2 of the licence grants a royalty-free licence to
reproduce, execute and modify the SDK solely for non-production internal
evaluation, research, and non-commercial testing; production deployment and any
other commercial purpose require a paid commercial license.

Beyond the limited evaluation patent licence of clause 2 of `LICENSE`, rights
under the patents behind the SDK are conveyed only by an executed commercial
licence. The software is marked with the page that lists them:

Patents: https://sdk.idside.eu/patents

- U.S. Patent No. 12,413,397 B2;
- European Patent No. 4,100,905 B1, with unitary effect, and any national
  patent rights deriving from that European patent;
- Chinese Patent Application No. CN115151937A, pending — no patent rights exist
  in an application before it issues.

That is the point of the arrangement rather than a restriction bolted onto it —
a patent you have already licensed away to everyone who runs `git clone` is a
patent that protects nobody, and an invention published under terms that waive
it invites exactly the argument it exists to answer.

What you can check is unaffected, and that is what auditability actually
requires: every line that decides what leaves your process is here, the ACT token
format is specified in the open, and a token can be verified offline by anyone
you show it to — including a regulator who has never heard of us.

The ID Side cloud is a separate thing and is proprietary: the multi-tenant
consent database, the ACT signing infrastructure and its private keys, the
immutable ledger, the dashboard, the policy and fraud-detection engines. The
[Preference Center widget](./widget.md) is on that side too — not for commercial
reasons, but because it is served by ID Side and framed cross-origin so that
neither your page nor your backend can read or write what is inside it. A widget
you could vendor into your own page would be a widget your code sits behind, and
the proof at the other end would attest to nothing. Its interface is documented
in the open regardless. That boundary is drawn in
[`ARCHITECTURE.md` §4](./ARCHITECTURE.md#4-source-available--proprietary-boundary).

Commercial licensing: [`COMMERCIAL_LICENSE.md`](../COMMERCIAL_LICENSE.md).
