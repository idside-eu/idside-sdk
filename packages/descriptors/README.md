# ID Side shared provider descriptors

Single source of truth for the provider descriptors consumed by every ID Side
SDK — Python and Node.js today, each bundling a copy a sync test keeps
byte-identical to this one. A descriptor declares the **mechanics** of
one AI provider — which client method to intercept, where to inject the
cloud-decided parameters, which header carries the ACT token. The **decision**
(which parameters to force for this call, for this user) is taken by the ID Side
cloud and returned as `params_forced`; no SDK ever computes it.

Since 2026-07-27 a descriptor also states the *mapping* the cloud applies: each
`enforcement` entry declared `enforced` carries, per parameter, the answer that
triggers it (`when`), the value to inject (`value`) and the models it may be
forced on (`models.only`, the preferred direction — `models.except` only where
the excluded family is closed and known). No SDK reads those three — the cloud
does, from its own copy of this package. They live here because they are facts
about a provider, and because the second copy of them, hand-kept in a database,
drifted from this one until three options were asked in the consent dialog and
forced nothing at all.

Every `enforcement` entry has a `level`: `enforced`, `contractual`, `account`,
`absent` or `unsupported`, each defined under `enforcement_levels` in
`consent-options.json`. `absent` — the provider does not perform the processing
on the API this descriptor wraps, so there is nothing to refuse — is written
only where the entry's note states why the wrapped API performs no such
processing; a lever that is merely missing stays `unsupported`. The cloud reads the difference: a refusal
of something that never happens is not a policy the call fails to apply.

A `contractual` entry also says which way its terms decide, in `protective`
(required there, refused anywhere else): `true` only where they rule the
processing out for every call without condition, `false` where they allow it,
impose it or leave it to the developer's organization. `contractual` alone used
to be read as a guarantee, and called a review of flagged content guaranteed to
the user; only `protective: true` lets a consent helper say `guaranteed`.

## Layout

```
descriptors/   canonical provider descriptors (openai.json, anthropic.json, ...)
schema/        JSON Schema (2020-12) every descriptor must validate against
testdata/      cross-SDK test vectors (request-hash, ACT verification)
```

## How SDKs consume this package

SDKs bundle **byte-identical copies** of `descriptors/*.json` so their build
artifacts stay self-contained. A synchronization test in each SDK's CI compares
the bundled copies against the canonical files here and fails on any
divergence. Never edit a bundled copy directly: edit the canonical file, then
copy it over.

## Adding a provider

1. Add `descriptors/<name>.json`. It must validate against
   `schema/provider-descriptor.schema.json` (the schema is closed:
   `additionalProperties: false`).
2. List **every** AI call entrypoint in `call_sites` — SDKs enforce consent on
   all sites declared here and present on the client. A missing site is a
   silent enforcement hole. Each entry is an object:

   ```json
   {
     "id": "responses.create",
     "path": ["responses", "create"],
     "forceable_params": ["background", "prompt_cache_retention", "store"]
   }
   ```

   `id` is the dotted **Python** path, and is what travels to the cloud as
   `call_site`. It is fixed in the Python spelling on purpose: the Node client
   may name the same endpoint differently (`generateContent` vs
   `generate_content`, remapped in `descriptors.ts`), and one endpoint has to
   have one name on the wire.

   `forceable_params` is declared **per endpoint**, not per provider, because a
   provider's endpoints do not accept the same parameters — OpenAI's
   `background` exists on `/v1/responses` and nowhere else. A provider-wide
   allowlist could only ever be their intersection (which loses the lever) or
   their union (which raises a TypeError on the endpoint that lacks it, for
   exactly the users who opted out, and for nobody else). The top-level
   `forceable_params` is the union, kept as a derived convenience and pinned to
   the sites by a test.
3. Copy the file into each SDK's bundled descriptor directory (for Python:
   `packages/python/src/idside/providers/`).
4. Run the SDK test suites; the synchronization and schema tests must pass.

4. Declare `tools_param` — the path to the argument carrying agentic tool
   definitions (`["tools"]`, or `["config", "tools"]` for google-genai). The SDK
   reads it to honour a `tools` constraint from the cloud: refusing the call, or
   stripping the tools. A provider without it cannot honour the constraint, and
   the SDK fails closed rather than letting the call through — a refusal it
   cannot apply is not a refusal it may ignore.

   `tools_param` is one key, and a refusal read from one key is only as wide as
   that key. Declare every OTHER argument of an endpoint that can hand the model
   a tool as that call site's `tool_fields` — the legacy `functions`, a
   `web_search_options` object that switches search on with no `tools` key, a
   Mistral `agent_id`, a Gemini `cached_content`, an OpenAI `conversation`:

   ```json
   "tool_fields": [
     { "path": ["functions"], "kind": "definitions" },
     { "path": ["agent_id"], "js_path": ["agentId"], "kind": "enabler" },
     { "path": ["input"], "kind": "input_items", "item_types": ["additional_tools"] }
   ]
   ```

   `path` is in the Python client's spelling; add `js_path` where the JS client
   names the argument differently. The `kind` decides how a refusal is applied.
   `definitions` is a list of tools: empty means none, and a `strip` constraint
   removes it. `enabler` is anything whose mere presence can bring tools — an
   object switching a hosted tool on, or a reference to a server-side object the
   SDK cannot see into; it cannot be removed without changing what the call
   means, so it is refused under `strip` too. `input_items` is an array whose
   items of the listed `item_types` carry tools, refused the same way. The rule
   to carry forward: any argument that delegates execution, or points at
   something that might, is a tool field, whatever it is called.

5. Declare `reserved_headers` — the headers that choose which credential,
   account or project a call goes out on (`Authorization`, `X-Api-Key`,
   `x-goog-api-key`, `OpenAI-Project`…). Every provider library lets a per-call
   header override the one it derives from the client's key, so a single
   `extra_headers={"Authorization": …}` would move an enforced call onto another
   key, defeating the credential posture the cloud required — and invisibly,
   because header values are not in the request hash. The SDKs refuse such a
   header, compared case-insensitively; the ACT `token_header` is always
   reserved and need not be listed.

6. If the provider is **OpenAI-compatible** — reached through the `openai`
   client against a different base URL, like DeepSeek — declare
   `client_base_url_hosts`. Auto-detection cannot use the client class there: an
   `OpenAI` instance is an `OpenAI` instance whoever receives the request, and
   the detected name is what tells the cloud which registry rules to apply. A
   descriptor without that field is its module's default.

7. If the provider scopes its privacy posture to something a developer can hold
   several of — a project, a workspace, an organisation — declare
   `credential_routing`. Its **presence** is what lets an SDK accept
   `consent.wrap(credentials=…)` at all; without it, credentials are refused
   rather than accepted and ignored, because there would be nothing to route
   between (see `deepseek.json`, which has none).

   ```json
   "credential_routing": {
     "header": "OpenAI-Project",
     "header_value_pattern": "^proj_[A-Za-z0-9_-]{1,120}$",
     "unit": "project"
   }
   ```

   Declare `header` **only** if the provider validates a per-request header that
   selects the credential — OpenAI's `OpenAI-Project` is the only one today.
   Without it, routing means a second client built with the other key, which is
   what the SDKs then require. `header_value_pattern` is not decoration: the
   value is written into an outgoing HTTP header, so it must be unable to carry
   CR/LF, and it must refuse a value of the wrong kind — a project id is
   `proj_…` where an API key is `sk-…`, and a developer who pastes the second
   has to be stopped before the secret leaves their process. Write it in the
   intersection of Python `re` and JavaScript `RegExp` syntax; both SDKs match it
   whole. `unit` is quoted back to the developer in errors, so it must be the
   word that provider uses.

   Mark each `enforcement` entry the credential carries with `routable: true`.
   That is what turns an account-level option into a question a consent dialog
   may ask — once the app actually provisions two credentials. So mark it only
   where some posture honours the refusal: routing between two credentials that
   both do the processing asks the user a question neither can answer. The
   schema refuses `routable` on any level but `account`.

If the provider's client accepts per-request headers through a kwarg other
than `extra_headers`, declare it as `headers_kwarg` (the native `mistralai`
client uses `http_headers`). Providers whose API does not fit the declarative
model register a custom adapter in the SDK — the descriptor still lives here,
with a `notes` field explaining the quirk (see `gemini.json`: `google-genai`
accepts no free-form kwargs, so its adapter nests the ACT token in
`config.http_options.headers`).

## Versioning

Each descriptor carries a `descriptor_version` (currently `"2"`), the version
of the descriptor *format*. SDKs report it to the cloud so the cloud knows what
each SDK understands. Any breaking change to the schema bumps the version;
additive optional fields do not.

`"2"` turned `call_sites` from bare paths into objects and replaced each
enforcement entry's single `param` with a `params` list scoped to endpoints.
Both were breaking, and both were made while nothing was published.

## Request-hash test vectors

`testdata/request-hash-vectors.json` pins down the `request_hash` computation
shared by all SDKs: `sha256:<hex>` over the RFC 8785 (JCS) canonical UTF-8
encoding of the caller's pre-enforcement payload (the identity parameters —
`identity_param` and `ubt_param` — and the per-request headers kwarg named by
`headers_kwarg` excluded). Forced parameters, constraint transformations,
routed credentials and the ACT header are applied afterward and are not part of
this hash. Every SDK replays these vectors in its test suite; Python and Node
must produce bit-identical hashes for each payload, otherwise the hash bound
into ACT tokens loses its evidential value. Each vector documents the edge case
it covers in its `description` field.

## ACT verification vectors

`testdata/act-verification-vectors.json` pins down the other half of the
contract: what it means for a proof to *verify*.

An auditor who checks a token with the Node SDK and a customer who checks the
same token with the Python one must reach the same verdict — otherwise the proof
is not a proof, it is an opinion that depends on which library you installed. So
each of the 22 cases carries a token and the expected outcome: either `valid` or
the **name of the error class** the SDK must raise. The Python and Node error
hierarchies use identical names on purpose, which makes a taxonomy divergence a
test failure rather than a surprise during someone's incident review.

The cases cover the attacks a token verifier exists to stop — `alg: none`, the
HS256 confusion where the public key is replayed as an HMAC secret, a UBT
presented as an ACT, a tampered payload, an unknown `kid`, a foreign issuer, a
development token passed off as a production proof — plus the binding checks
that make one token good for exactly one request.

A case may carry its own `jwks`, overriding the file-level key set — that is how
the poisoned-key-set case says "the token is fine, the keys are not". A harness
must honour that, and must fail loudly on any option key it does not map: an
option silently dropped would leave the case running, probably passing, and
pinning something other than what it claims to.

Verification is pinned to a fixed `now`, so the expiry and not-yet-valid cases
stay deterministic forever. The file is the artefact of record: edit it by hand,
never regenerate it (regeneration would mint a new ephemeral key and change all
22 tokens for no gain). Only the **public** key is in the file: the key that
produced these tokens was generated to write it and destroyed, and both SDKs
assert that no private material ever appears there.

These static vectors were minted before the `mode` claim became mandatory, so
their file-level defaults explicitly enable legacy-without-mode verification.
New proof fixtures must include `mode: "test" | "live"`; application code must
not enable the compatibility option unless it is reading a known historical
archive.

### When an error deserves its own class

The vectors pin error *classes*, not just accept/reject, so splitting or merging
one is a cross-SDK change. The bar, arrived at while ruling on a real case:

1. the remediations are **opposite**, not merely different;
2. the wrong remediation is **actively harmful**, not merely futile;
3. the wrong case is **rare enough not to be caught in development**.

`ActPrivateKeyInKeySetError` clears all three, which is why it exists rather
than reusing `ActKeyNotFoundError`. A missing `kid` says "refresh the key set,
it may have rotated"; a leaked key says "rotate now, do not retry". The obvious
handler — `except ActKeyNotFoundError: refresh_and_retry()` — would loop against
a poisoned key set during the exact incident it should be escalating, and it
would do so on the day of a leak, not on the developer's first run. Hence also
the rule that it must **not** inherit from `ActKeyNotFoundError`: a subclass
would be caught by that handler anyway, and the split would be decorative. Both
SDKs have a test pinning the non-inheritance.

For contrast, a case examined and **declined**: `ActVerificationUnavailableError`
covers both "the verification extra is not installed" and "the key set could not
be fetched". Those remediations differ — a deployment fix versus a retry — but
retrying a missing dependency is futile rather than harmful, and it fails on the
very first call in every environment, so criteria 2 and 3 both fail. It stays one
class.
