# Changelog

Both SDKs in this repository — `idside` (Python) and `@idside/sdk` (Node.js) —
are released together and **share one version number**, spelled the way each
ecosystem spells it: `0.1.0.dev0` on PyPI, `0.1.0-dev.0` on npm. One entry below
therefore covers both unless it says otherwise.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). What a
version number promises — and what it deliberately does not — is
[`docs/versioning.md`](./docs/versioning.md); how a release is cut is
[`RELEASING.md`](./RELEASING.md).

---

## [Unreleased]

Nothing yet.

## [0.1.0.dev2] — 2026-11-11

**Upgrade from `0.1.0.dev0` or `0.1.0.dev1`**, whichever provider library you
use: with the versions a fresh install gets today, both let through calls this
release refuses or corrects — the first two entries under *Changed* say
which.

The per-call option lists are brought up to the provider libraries a fresh
install gets today, and CI now runs against them: `openai` 3.49 (Python) and
7.55 (Node), `anthropic` 2.8 and `@anthropic-ai/sdk` 1.8, `google-genai` 3.1
(2.33 on Python 3.10, which 3.x no longer supports) and `@google/genai` 3.1,
`mistralai` 3.7 and `@mistralai/mistralai` 3.5. No protocol change: the request
hash function, the call-site ids and `/v1/authorize` are unchanged. One
descriptor change, additive: two more `reserved_headers` on Anthropic. One
thing the hash function already did shows with these libraries: in Python, a
Gemini `config` passed as a typed `GenerateContentConfig` is hashed through its
`model_dump()`, which writes every field the installed `google-genai` declares,
`None` included — so the same call hashes differently under `google-genai` 2.11
and 2.33 or later, which added four fields. A `config` passed as a dict does not
move.

### Changed

- **Gemini: automatic continuation is turned off on every enforced call.**
  `google-genai` 2.30+ and `@google/genai` 3.x answer a response that stops with
  `finish_reason` `CONTINUATION` by sending the request again, by themselves,
  with its continuation token — on by default, and under the same ACT token,
  which proves one request. With an older SDK and a current library, one
  authorized call could become several requests. The SDK now forwards
  `automatic_continuation=False` (`automaticContinuation: false`) after the
  request hash is computed, so no hash moves; such an answer comes back with its
  `continuation_token`, and you resume with a new enforced call passing it in
  `config`. Asking for it (`automatic_continuation=True`) raises
  `UnenforceableOptionError` before `/v1/authorize`.
- **Refused on an enforced call, with `UnenforceableOptionError` before
  `/v1/authorize`:**
  - Anthropic `workspace_id=` and `user_profile_id=` (new in `anthropic` 2.x /
    `@anthropic-ai/sdk` 1.x). They look like body fields and are sent as the
    `anthropic-workspace-id` and `anthropic-user-profile-id` headers, outside
    the request hash: the first picks the workspace a credential acts in — the
    unit credential routing chooses, with its own retention and region — and
    the second attributes the call to a user profile whose programs can require
    retention and human review. Both headers are now in the Anthropic
    descriptor's `reserved_headers`, so `extra_headers` / `options.headers`
    cannot set them either. Instead: wrap a client whose key belongs to the
    workspace, or declare one per posture to `credentials=`.
  - OpenAI `prompt_cache_options=` (new in `openai` 3.x / 7.x). Its `ttl` sets a
    minimum cache lifetime, up to `12h`, that the forced `prompt_cache_retention`
    does not bound and that cannot be forced yet. Leave it out;
    `prompt_cache_retention` and `prompt_cache_key` still pass.
  - Gemini `generation_output_config` / `generationOutputConfig`: the provider
    would write the generated media to the `gs://` location it names.
  - Node: `config.httpOptions.fetch` (`@google/genai` 3.x) and the openai 7
    internal `__streamMode`.
- **Accepted**, after reading what each does: Python `parse_as=` and Node
  `parseAs` on `responses.parse()` (the type the output is parsed into, never
  sent); Node `openTelemetry` per call on Anthropic (a local span attribute);
  the options openai 7's and `@anthropic-ai/sdk` 1.x's own `stream()` helpers
  now pass to `create()` (`__metadata.helperMethod`, a response-parser hook that
  is handed a read-only view of the body, and `__span`); every new body field
  the libraries added (OpenAI `access_programs`, `image`; Anthropic
  `diagnostics`, `speed`; Mistral `service_tier`, `top_k`, `min_tokens`,
  `repetition_penalty` and the log-probability fields; Gemini `store` and
  `continuation_token`), which are hashed and proven as written.
- **Derived clients**: `with_options()` / `copy()` / `withOptions()` with
  OpenAI's `data_residency` / `dataResidency`, `provider` or `x509Transport`
  raise `UnsupportedProviderError` (another destination: build that client and
  wrap it), a Node `credential` raises `CredentialRoutingError`, and
  Anthropic's `open_telemetry` / `openTelemetry` derives normally. Unclassified,
  all of these were already refused.
- **Node: the Gemini NextGen surfaces `credentials` and `voices`**
  (`@google/genai` 3.x) are under the net like the other five.
- **Python: `anthropic` 2.x no longer accepts `temperature`, `top_k` or `top_p`
  on `messages.create`.** An enforced call passing one is refused with
  `UnenforceableOptionError` before `/v1/authorize`, where the library alone
  would raise a `TypeError`.
- **The docs no longer state the free plan's allowance**: it is the pricing
  page's to say, <https://sdk.idside.eu/pricing>, and the figure the READMEs of
  `0.1.0.dev1` gave is no longer the one in force.

## [0.1.0.dev1] — 2026-11-11

`idside` `0.1.0.dev1` on PyPI and `@idside/sdk` `0.1.0-dev.1` on npm, under the
`next` dist-tag. Mostly documentation, from a first rehearsal against the
published packages and the live service. **Nothing breaks**: no name is
removed or renamed, the request hash and the proofs are unchanged, and code
written against `0.1.0.dev0` runs as it did.

### Added

- **`request_hash(payload)` in Python**, exported from `idside` — the twin of
  Node's `requestHash()`. It returns the `sha256:` hash an ACT token is bound
  to, so a verifier can compute the `request_hash=` it pins `verify_act()` to;
  until now the Python quickstart used a value it gave no way to compute. It is
  the very function the SDK hashes every call with, not a copy: pass the call's
  arguments without `user_id` / `ubt` and `extra_headers`, as JSON values.
  Anything else raises `ValueError`. The shared cross-SDK vectors pin it.

### Changed

- **Install commands lead with the bare package**: `pip install idside` and
  `npm install @idside/sdk`. The SDK wraps the provider client you already use
  and needs no provider chosen at install time; `idside[openai]` and
  `npm install @idside/sdk openai` remain, for starting from nothing. Checked:
  both packages import with no provider library installed.
- **Where an API key comes from**: both quickstarts now name the no-account
  sandbox, *Test without an account* on <https://sdk.idside.eu/sdk>, which hands
  out an `sk_test_` key valid 24 hours and limited to 20 calls, beside signing
  up.
- **npm staged publishing.** The publish workflow now runs `npm stage publish`:
  a release reaches npm staged, and becomes installable once a maintainer
  approves it with 2FA ([`RELEASING.md`](./RELEASING.md) §2).

### Fixed

- **A test key's proof is not refused by default, and the docs said it was.**
  A call made with an `sk_test_` key on the ID Side service is signed as
  `idside.eu`, carries `mode: "test"`, and passes `verify_act()` /
  `verifyAct()` with the default options. `test.idside.eu`, which the verifiers
  refuse without `allow_test_issuer` / `allowTestIssuer`, is the issuer of a
  deployment of the cloud that is not the production service — not of a test
  key. The quickstarts, both READMEs, the FAQ, the error catalogue and the
  [ACT spec](./docs/spec/act-token.md) now say so, and tell a verifier to require
  `proof.mode == "live"` before counting a proof as production evidence. The
  verification itself is unchanged.
- **`npm install @idside/sdk` works without `@next`.** The docs said a
  pre-release had to be installed as `@next`; npm set `latest` to the first
  pre-release too, so a plain install works while only pre-releases exist.
  `@next` always names the newest pre-release.
- **The Python quickstart's dry run prints the same body with every `openai`
  version.** The order of keys on the wire is the `openai` library's and changed
  between its versions, so the documented line no longer matched a fresh
  install; the example now prints the body with sorted keys.

## [0.1.0.dev0] — 2026-11-11

The first pre-release: `idside` `0.1.0.dev0` on PyPI and `@idside/sdk`
`0.1.0-dev.0` on npm, under the `next` dist-tag. This section is not a list of
changes against a previous release — it is the inventory the first release
carries. The detailed history before it is the git log; from here on, this file
is the record and the git log is the detail.

### Added

- **Three more `legal_basis` values on an imposed policy** (protocol, issued by
  the cloud): `public_task`, `legal_obligation` and `vital_interests` (GDPR
  Article 6(1)(e), (c) and (d)), beside `legitimate_interest`,
  `contractual_obligation` and `not_established`. Values added, none renamed.
  Since 2026-11-08 an organization's policy may also declare `consent`: ID Side
  collects no answer from the employee, and `source: "default_policy"` shows
  that the decision was the organization's
  ([spec](./docs/spec/act-token.md#legal_basis--what-each-value-asserts)).
  Neither SDK changes: both carry `legal_basis` as an opaque string, in the
  authorize response and in what `verify_act()` / `verifyAct()` return.
- **`absent`, a fifth enforcement level in the descriptors** (descriptor data,
  no protocol change): the provider does not perform the processing on the API
  the descriptor wraps, so there is nothing to refuse. It is kept apart from
  `unsupported`, where the processing may happen and cannot be switched off,
  and said only where the entry's note states why the wrapped API performs no
  such processing: OpenAI `training.personalisation` and `memory`, and
  `memory` at DeepSeek, Gemini and Mistral. The enforcement path never reads
  `level`; the consent helpers do, so `disclosures()` now reports `"absent"`
  for those five in `by_provider` / `byProvider`, with the same `kind` as before
  (`not_offered`), and `EnforcementLevel` gains `"absent"` in both SDKs.
- **Enforce and Observe: `policy_hold` and `deviations`** (protocol, issued by
  the cloud, on an application where an organization imposes its policy on its
  employees). When your policy cannot apply to a call (a provider with no such
  setting, a provider your organization has not allowed, an employee who is not
  identified), Observe lets the call through and flags it, Enforce holds it
  ([concepts](./docs/concepts.md#enforce-and-observe--when-the-policy-cannot-apply)).
  Held, `/v1/authorize` answers 403 `policy_hold`, which both SDKs now name as
  `ConsentDeniedError.reason == "policy_hold"`; an SDK that predates it raises
  the same error with `reason` unset, and the provider is not called either
  way. Flagged, the token carries a `deviations` claim
  ([spec](./docs/spec/act-token.md#deviations--what-the-policy-could-not-apply)),
  which `verify_act()` / `verifyAct()` keep in `claims`, unexamined. The
  bound the cloud holds every token to rises from 2 KB to 3,072 characters,
  since this claim is what can take one past 2 KB; the verifiers'
  8,192-character cap is unchanged. An application that asks its visitors
  sees neither.
- **`refused_by` in the ACT token** (protocol, issued by the cloud). Every token
  now says which layer refused each option — `person`, `silence`,
  `organization`, `team` or `app`
  ([spec](./docs/spec/act-token.md#refused_by--who-refused)). Neither SDK
  changes: `verify_act()` / `verifyAct()` keep a claim they do not name in
  `claims`, unexamined, and a new shared verification vector,
  `valid_with_refused_by`, pins that both return it exactly as signed.
- **`UnenforceableOptionError`**, in both SDKs — raised before `/v1/authorize`
  when an enforced call passes an option that would let it leave in a state
  enforcement cannot vouch for. See *Changed* below for what it closes.
- **Descriptor fields `tool_fields` (per call site) and `reserved_headers`** —
  additive, `descriptor_version` stays `2`. The first names every argument that
  can hand the model a tool; the second, the headers that select a credential.
- **`consent.wrap()`** — wraps a provider client in place and enforces consent on
  every declared call site: resolve the user's preferences from the ID Side
  cloud, force the retention parameters the decision names, attach the signed
  ACT token as `X-AI-Consent-Token`, then call the provider. Fail-closed: if the
  decision cannot be obtained, no AI call is made. Idempotent, and a client
  derived through `with_options()` / `withOptions()` / `copy()` comes back
  enforced too.
- **Five provider descriptors across four client libraries** — OpenAI,
  Anthropic, Google Gemini, Mistral, and DeepSeek through the OpenAI client
  (told apart by base URL, since the class name proves nothing). Adding a
  standard provider is a JSON descriptor, not code.
- **Async support in Python.** `wrap()` reads the shape of the client once, at
  wrap time; an `AsyncOpenAI` or `AsyncAnthropic` gets async wrappers and an
  awaited hop to the cloud, so an ASGI application is not stalled once per AI
  call. The Node SDK has always been async.
- **Streaming helpers are enforced, and authorized when the stream is opened.**
  Python's `chat.completions.stream()`, `responses.stream()` and
  `messages.stream()` hand back a context manager and send nothing until it is
  entered, so that is where the SDK asks the cloud — on a blocking client and on
  an async one alike. The identity and the request hash are still taken at the
  call, where a mistake belongs. A stream nobody enters makes no request, so it
  mints no ACT token and leaves no ledger entry, and the same abandoned stream
  costs the same nothing whichever client shape an application uses.
- **The transport guard.** Enforcement is not only the declared call sites: a
  guard on the client's own HTTP transport refuses any body-bearing request
  whose ACT token does not match private authorization provenance for this
  wrapped client and route (`UnenforcedCallError`). A header the caller brought
  with it authorizes nothing. It reads the HTTP method, the URL path and that
  one header, and nothing else — never the body, never the query string — it
  compares locally, and it records nothing, ever.
- **Credential routing.** `wrap(credentials={…})` lets an application *label* the
  credentials it holds (an OpenAI project, an Anthropic workspace, a Mistral
  organisation); the cloud names which posture a call requires and the SDK
  matches the label. A posture nobody provisioned fails the call
  (`MissingCredentialError`) rather than falling back onto another credential.
- **Tool refusal.** A `{"tools": "refuse"}` constraint stops a call carrying
  agentic tool definitions (`ToolUseRefusedError`) without the cloud ever being
  told whether a call carried tools.
- **Offline proof verification** — `verify_act()` / `verifyAct()`: JWS structure,
  `alg` pinned to `ES256`, `typ` `act+jwt`, key selection by `kid`, signature,
  mandatory claims, issuer, time window, and any binding the caller pins. No
  call to ID Side, no private key anywhere in this repository. Python needs the
  `[verify]` extra (`cryptography`); Node uses `node:crypto`.
- **32 exported error classes** in each SDK, with identical names on both sides:
  `IDSideError` and the nineteen enforcement types (all under it, except
  Python's `UnenforcedCallError`, a `BaseException` on purpose),
  `ActVerificationError` and the eleven verification types under it. `error.name`
  carries the class name in Node. Catch the class, never the message.
- **`InvalidBindingError` and `BindingRequiredError`, under a new
  `BindingError`**, in both SDKs. A 401 from `/v1/authorize` used to be
  `AuthenticationError` — "Check the api_key" — whatever its cause, so a
  visitor whose UBT had expired, which is every visitor coming back after 24
  hours, sent developers to regenerate a key that was fine. The problem `type`
  now picks the class: `ubt_invalid` (the UBT did not verify — it expired, was
  issued for another application, was altered, or is not a UBT at all) raises
  `InvalidBindingError`, whose remedy is a new UBT from the widget;
  `ubt_required` (an application that requires bound tokens was called with
  `user_id`) raises `BindingRequiredError`, whose remedy is the widget before
  any AI call. Any other 401 — the key, or a `type` this
  SDK does not know — stays `AuthenticationError`. `BindingError` derives from
  `AuthenticationError`, so an existing handler still catches both; see
  [the three 401s](./docs/errors.md#three-401s-three-different-answers).
- **`ConsentRefreshRequiredError`**, in both SDKs, directly under
  `IDSideError` — see *Changed* below for the 409 it now names.
- **The consent option catalogue** — `catalogue()`, `decidable_options()` /
  `decidableOptions()`, `disclosures()`: what an application may actually *ask*
  its users given the providers it wraps, and what it may only *state*.
- **A journal**, silent unless asked: the `idside` logger in Python,
  `NODE_DEBUG=idside` in Node. Six events with the same fields in both SDKs — a
  seventh, `resume`, exists only in Python, which patches the one overloaded
  helper Node does not — and nothing secret in any of them — no key, no `user_id`, no `ubt`, no prompt, no
  ACT token, no credential. The subject is a per-process salted pseudonym.
- **Typed identity in TypeScript.** `wrap()` returns `Enforced<T>`, so the
  identity is required by the type on every enforced call site: forgetting it is
  a compile error rather than a `MissingIdentityError` on the first request, and
  no cast is needed. See
  [quickstart §TypeScript](./docs/quickstart-node.md#typescript) for the one
  consequence — `Enforced<OpenAI>` is not assignable to `OpenAI`.
- **Cross-SDK contract tests.** Shared request-hash and verification vectors in
  [`packages/descriptors/testdata/`](./packages/descriptors/testdata/), a
  descriptor sync test in each SDK, and `scripts/check_sdk_parity.sh`, which runs
  both SDKs on one scenario and diffs the consent dialog, the credential
  routing, the disclosure kinds, the journal lines, the deadline each one
  arms on the hop to the cloud, and the error class each one raises for the
  same refusal from it — with the words, where a message is written once for
  both.
- **One unavailability threshold: 5 seconds, in both SDKs, on every phase of the
  authorize hop and of the key-set fetch.** On a fail-closed path a timeout is
  the moment the SDK declares the cloud dead and stops making AI calls, so two
  numbers meant one incident cutting a Python service and sparing its Node.js
  equivalent. Configurable in both (`timeout=` / `timeoutMs`).
- **Both SDKs name themselves to the cloud.** Every request carries
  `User-Agent: idside-python/<version>` or `idside-node/<version>`, on the
  authorize hop and on the key-set fetch, so an incident can be traced to a
  version.
- **The consent-client surface is public in Python too** — `ConsentClient`,
  `AsyncConsentClient`, `ConsentDecision`, `HttpConsentClient`,
  `AsyncHttpConsentClient` and `DEFAULT_BASE_URL` are exported from the `idside`
  root, matching the Node index. Injecting `consent_client=` no longer requires
  importing a private module.
- **Packaging proven end to end.** `scripts/check_publishable.sh` packs and
  builds both packages, installs each one outside the repository and exercises
  the paths that read the bundled JSON — the only place a missing descriptor
  shows up before a user's first AI call.
- **Runnable examples** in [`examples/`](./examples/), and quickstarts whose code
  is extracted and executed in CI, both diffed against the output the documents
  claim (`scripts/check_docs_examples.sh`).

### Changed

- **A `409 consent_refresh_required` is no longer a `ConsentUnreachableError`.**
  It raises `ConsentRefreshRequiredError`, which derives from `IDSideError` and
  deliberately not from `ConsentUnreachableError`, because it is not an outage:
  the visitor's UBT still verifies and the choices it covers are out of date —
  the policy or the required questions changed since they answered — so a retry
  gets the same answer until they review them. This is an observable break for
  code that caught it as an outage. What to write instead: catch
  `ConsentRefreshRequiredError` before `ConsentUnreachableError`, stop the AI
  request and show the visitor the widget again; never retry it with backoff.
  Any other 409 is unchanged.
- **Licensing — source-available, not open source.** The reasoning is in
  [`COMMERCIAL_LICENSE.md`](./COMMERCIAL_LICENSE.md), and why terms cannot be
  withdrawn from a version once it is published is in
  [`RELEASING.md` §7](./RELEASING.md#7-what-the-first-publish-freezes).
- **The ID Side Source-Available License, version 1.0, is in force.** `LICENSE`
  is counsel's text, byte for byte, in the root and in both packages, and
  `scripts/check_licence_claims.sh` pins its SHA-256. Clause 2 grants a
  royalty-free licence to reproduce, execute and modify the SDK solely for
  non-production internal evaluation, research, and non-commercial testing,
  with a patent licence limited to the same purpose; production and any other
  commercial purpose require a paid commercial licence. Beyond the limited
  evaluation patent licence of clause 2 of `LICENSE`, rights under the patents
  are conveyed only by an executed commercial licence. The same text is
  published at <https://sdk.idside.eu/legal/sdk-licence>.
- **Patents identified in full, and marked.** `NOTICE` and the documentation
  name U.S. Patent No. 12,413,397 B2; European Patent No. 4,100,905 B1, with
  unitary effect, and any national patent rights deriving from that European
  patent; and Chinese Patent Application No. CN115151937A, which is pending. A
  patent notice reads as a complete list whether or not it is one, so a number
  missing from it is worse than a notice nobody wrote — and an application
  named without saying it is one would be the opposite error. Every source file
  of both SDKs, and so every shipped `.py` and `.js` file, opens with the
  virtual patent marking `Patents: https://sdk.idside.eu/patents`; so do the
  READMEs and `NOTICE`, and both manifests carry the address. The page lists the
  patents and is kept current; the publish workflow refuses to run while it
  does not answer.

- **Per-call escapes are refused, not honoured.** Every provider library lets a
  call undo what enforcement applied after it applied it, and until now both SDKs
  let it: `extra_body={"store": True}` sent `store: true` past a refusal, a Node
  second argument `{ body }` or `{ fetchOptions }` replaced the request, a
  per-call `Authorization` moved the call onto another key, `server_url` or
  `http_options.base_url` sent the prompt and the ACT token to another host — and
  the signed proof went on describing the request as authorized. An enforced call
  now accepts a closed list of per-call options (timeouts, retries, an abort
  signal, an idempotency key, non-reserved headers) and raises
  `UnenforceableOptionError` for anything else, including options a future
  library version adds. What to do instead: drop the option; for another host,
  credential or transport, build a client with it and wrap that client. A header
  carrying the ACT token is now refused rather than silently overwritten. In
  Python the list telling options from body parameters is read from the installed
  library, so a parameter a newer version adds still goes out; when that version
  *moves* the schema out of reach, the fallback is the schema as this release
  read it, so what a call may carry only ever narrows.
- **A tool refusal reads every tool field, not `tools` alone.** `functions` and
  `web_search_options` (OpenAI chat, DeepSeek), `conversation`,
  `previous_response_id`, `prompt` and tool-carrying `input` items (Responses),
  `mcp_servers` (Anthropic), `cached_content` (Gemini) and `agent_id` (Mistral)
  now raise `ToolUseRefusedError` for a user who has not accepted agentic tools.
  References to server-side objects are refused under `strip` as well. The
  practical consequence: for such users, Mistral agents, Gemini caches and
  chained Responses calls are refused. An empty `tools` list is no tool, in both
  SDKs (Node used to refuse it).
- **An enforced call is bound to its host and follows no redirect.** `wrap()`
  fixes the client's origin; a request to another origin raises
  `UnenforcedCallError`, and a `307`/`308` comes back as the provider's status
  error instead of being re-sent elsewhere.
- **Derived clients are checked.** `with_options` / `withOptions` / `copy` pointing
  at another provider or host, or bringing a transport, query or middleware,
  raise `UnsupportedProviderError`; changing the key, token, organization or
  project raises `CredentialRoutingError`. Python judges middleware on what it
  would change, so `with_middleware()` given none — which the library answers
  with `copy(middleware=[…the same…])` — derives normally, and the refusal names
  the method the caller wrote rather than the one the library reached it through.
- **Gemini local tools are refused on an enforced call.** A Python function, an
  MCP session or a Node `CallableTool` made the library run up to ten requests
  under one ACT token. Declare the functions and run the loop yourself: each turn
  is then authorized and proven.
- **A client whose transport the net cannot reach is refused** instead of wrapped
  without a net: Node `new Mistral({ apiKey })` needs `httpClient: new HTTPClient()`,
  Python `google-genai` on Vertex mutual TLS or `aiohttp` is refused.
- **Breaking, Python: a transport whose `send` will not accept `follow_redirects`
  is refused at `wrap()`.** Every authorized request is sent with that keyword,
  and transports are recognised by shape rather than by distribution, so the
  shape alone does not say the keyword will be taken. A client that used to be
  wrapped now raises `UnsupportedProviderError` — in practice, one whose `send`
  was replaced on the instance by a recorder, a retry shim or other
  instrumentation with a narrow signature. **What to do:** make that wrapper
  forward what it is given — `def send(self, request, *args, **kwargs):
  return original(request, *args, **kwargs)` — or pass the client a stock
  `httpx` / `httpx2` client. A shim that drops keywords is already dropping the
  ones `openai` itself passes (`stream=`, `auth=`). Refusing to wrap is the only
  safe answer: sending without the keyword lets a `307` re-send the prompt, the
  ACT token and the key to a host nobody checked, and sending with it fails the
  call *after* ID Side has authorized it and recorded the proof — while every
  bodyless call keeps working, so the cause looks unrelated.
- `ProviderAdapter.apply_constraints` / `applyConstraints` take an optional call
  site, so a custom adapter can read that endpoint's `tool_fields`.
- Node: `.withResponse()` / `.asResponse()` and Anthropic `messages.stream()` work
  on an enforced call again.
- Use `https://sdk.idside.eu` as the default service origin in both SDKs.
  Explicit `base_url`/`baseUrl` and `IDSIDE_BASE_URL` overrides retain
  precedence.
- **`verify_act()` / `verifyAct()` now require the signed `mode` claim** and
  reject a validity window longer than 3600 seconds. A proof minted before the
  claim existed is accepted only through `allow_legacy_without_mode=True` /
  `allowLegacyWithoutMode: true`, and `VerifiedAct.mode` is then `None` /
  `undefined`. Write that option into an archive replay; never into a live
  verification path.
- **`wrap()` now refuses a client it cannot fully guard.** A transport discovery
  that runs out of budget, or a transport that will not take the patch, raises
  `UnsupportedProviderError` instead of wrapping the client and journalling
  `net=partial`. Nothing is patched when it refuses, so there is no half-wrapped
  client; put non-inference traffic on a second, unwrapped client, or use a
  provider-client version this SDK supports.
- **Five more authorize-response fields are mandatory to the SDK** —
  `decision` (which must be `allow`), `params_forced`, `mode` (`test` or
  `live`), `token_id` and `expires_at` — joining the original five, and the
  value domains the SDK acts on (`binding`, `constraints`) are now closed. A
  response missing one, or carrying a `constraints` key this SDK cannot apply,
  is `ConsentUnreachableError` and no AI call is made. See
  [`docs/versioning.md`](./docs/versioning.md) §1.1 for what `/v1` may still
  change.
- **What nobody accepted is refused.** A consent state is completed from the
  option catalogue once it is resolved, before it is enforced or signed: an
  option a person never answered now counts as `false` instead of counting as
  nothing. This is an observable break, and a deliberate one — a call passing
  `tools` for a user who has never answered used to go out and now raises
  `ToolUseRefusedError`, so an agentic integration with no Preference Center
  running is refused on every request, for every user. What to do instead: run
  the [widget](./docs/widget.md) so the question reaches your users; the ones
  who accept stop raising it, and a call carrying no tools still goes out for
  the ones who do not. The same completion can name a credential posture where
  none was named before — a training refusal on Mistral — and that call raises
  `MissingCredentialError` until the credential is provisioned and labelled.
  Nothing has to be migrated when a provider gains an option
  later — it is refused from the moment it exists, for everyone who answered
  before it did. The record the cloud *stores* is unchanged and still keeps the
  gap, which is what the Preference Center re-asks on: what somebody said and
  what applies to them are two different facts. The imposed mode is untouched —
  an organization's policy still decides for its employees. The rule arrives
  through `/v1/authorize` rather than through a package upgrade, so pinning an
  older SDK does not hold the previous behaviour; see
  [`docs/versioning.md`](./docs/versioning.md) §1 for why nobody can pin the
  protocol.
- **Node: the Gemini NextGen surfaces go out under the net.** `@google/genai`
  2.22 carries a second API on the same client — `interactions`, `webhooks`,
  `agents`, `triggers`, `environments` and `getNextGenClient()` — behind
  prototype accessors, each building its own HTTP client rather than sending
  through `ApiClient.apiCall` where the net sat. On a wrapped client
  `interactions.create({ model, input })` therefore left the process with the
  prompt and the API key, no ACT token, no `/v1/authorize` and nothing anywhere
  recording it. The net is now installed on every client those surfaces build,
  including the fresh one each `api_version` call makes. This is an observable
  break: no descriptor declares a call site on those routes, so a body-bearing
  request on them now raises `UnenforcedCallError` with a message naming the
  surface, while every retrieval still goes out. What to do instead: keep a
  second, unwrapped `GoogleGenAI` client for that API. Reading a surface is
  still what builds it, so `wrap()` triggers none of the library's own
  experimental-usage warnings.
- **Node: the `credentials` mapping is read once, at `wrap()`.** It used to be
  re-read every time a client was derived from a wrapped one, so a posture added
  to the developer's own object afterwards started routing calls — on a client
  nobody had wrapped, bound to no server, and announced to the cloud as if it had
  been declared. `withOptions({ timeout })` was enough to enrol it. Python has
  always copied the mapping; both SDKs now promise the same thing.
- **Node: an enforced call is a promise `Promise.prototype`'s own methods can be
  applied to.** `Promise.prototype.then.call(call, …)` threw "Promise resolve or
  reject function is not callable" and `util.inspect` printed
  `Promise { undefined }`, so instrumentation or any library holding a captured
  `then` broke on a wrapped client. `await`, `.then`, `.catch`, `.finally`,
  `.withResponse()` and `.asResponse()` behave exactly as before, the provider's
  own promise is still adopted only when the call is read, and `asResponse()`
  still hands back an unread body.
- **`disclosures()` says `guaranteed` only where the terms rule the processing
  out.** `contractual` says that a provider's written terms decide, not which
  way, and every `contractual` level used to be disclosed as `guaranteed` —
  "already true for everyone, in the user's favour" — including the review of
  flagged content that Anthropic's and OpenAI's terms allow, Anthropic's
  mandatory retention and OpenAI's training "unless the org opts in". A
  `contractual` entry now says which way in a required `protective` field
  (descriptor data, no protocol change), and `guaranteed` needs every provider
  that answers the option to rule it out. None does today: Anthropic's "may
  not train models on Customer Content" gives way to its Development Partner
  Program, which a customer joins by enabling a data opt-in setting (Service
  Specific Terms, section E), just as OpenAI's no-training rule gives way to an
  organization's opt-in. Every `contractual` entry is therefore disclosed as
  `beyond_user_control` in both SDKs. What to write instead:
  code that hid an option because it was `guaranteed` will now show it, and
  should. The privacy matrix marks each contractual cell *ruled out by the
  terms* or *terms do not rule it out*.
- **`human_review` is `contractual` at Gemini and Mistral, as at OpenAI and
  Anthropic, and routable nowhere** (descriptor data, no protocol change).
  OpenAI, Anthropic and Google write that their staff may review what abuse
  or safety detection flags; Mistral calls its abuse monitoring automated
  moderation and documents human access only to what a customer reports. None
  of the four has a request parameter for it, and none has a project,
  workspace or organisation setting that rules review out in writing:
  OpenAI's zero-retention projects come closest, and still leave flagged
  images to manual review. Gemini and Mistral were `account` and `routable`,
  so `decidable_options()` with two Gemini or two Mistral credentials asked
  users to refuse a review neither credential could stop: a billed Gemini
  project that shares no dataset with Google removes routine review but not
  the assessment of flagged content, and Mistral's console has no setting for
  it. With this, the question is never asked; it is disclosed as
  `beyond_user_control`, with an empty `routable_from` / `routableFrom`. What
  to write instead: an app that rendered `human_review` as a question because
  it passes two Gemini or Mistral credentials as `routed` should state it from
  `disclosures()` instead, like every other option no answer can change. Each
  `human_review` note now names what the provider writes and says when it was
  checked; OpenAI's names its controls as its documentation does today,
  Private Retention with Private Safety Processing included, and says why none
  is a posture to route on. DeepSeek, which documents nothing either way, stays
  `unsupported`. The descriptor schema now refuses `routable` on any level but
  `account`, so no descriptor can say it of such an entry again.
- **`by_provider` / `byProvider` can say `"absent"`, and `EnforcementLevel`
  has a fifth member** (descriptor data, no protocol change). The five entries
  the descriptors now call `absent` (see *Added*) used to come back from
  `disclosures()` as `"unsupported"`; their `kind` is still `not_offered`.
  What to write instead: code that reads a provider's level as `"unsupported"`
  to say that no control exists should read `"absent"` the same way, and tell
  the two apart only to say why — `absent` means there is nothing to refuse.
  In TypeScript, an exhaustive `switch` over `EnforcementLevel`, or a
  `Record<EnforcementLevel, …>`, needs an `"absent"` case to compile.

### Removed

- **`provider_blocked` is no longer a `ConsentDeniedError.reason`.** The cloud
  has never emitted it. A denial reason an SDK names but nothing produces is a
  branch a caller writes and never takes — dead code that reads as tested
  behaviour. The two the cloud does send, `fraud_suspended` and
  `organization_suspended`, are documented in both SDKs and in
  [`docs/errors.md`](./docs/errors.md); anything else leaves `reason` unset. The
  denial itself is unaffected: it lands either way, and only the explanation is
  withheld.

### Fixed

- **An identity made only of blanks is absent.** `user_id="   "` used to leave,
  since only falsy values counted as absent; the cloud refused it with a 422,
  and the message sent the developer to upgrade the SDK. In both SDKs a
  `user_id` or `ubt` made only of blanks now raises `MissingIdentityError`
  with no request, as the empty string did — "blank" being exactly what the
  cloud counts as blank, the 25 Unicode white-space code points, so the two
  SDKs and the cloud agree. Nothing else is trimmed: an identifier with spaces
  around it is sent as written, and its pseudonym does not change.
- **A 422 no longer names a single cause.** `ContractError` said "SDK/cloud
  contract mismatch — upgrade the SDK or report a bug" also when the cloud's
  detail, quoted right after, named a value the caller passed. Its message now
  says both are possible and that the detail decides; the class is unchanged,
  since the cloud's `type` (`invalid_request`) does not separate the two.
- **An error message no longer ends in two full stops.** Both SDKs append the
  cloud's `title` and `detail` to the message they raise, and put a full stop
  after a `detail` that already ended a sentence — `…(Signature has expired)..`.
  A `detail` ending in `.`, `!` or `?` now ends the message as the cloud wrote
  it.
- **A failure on the default address names the missing setting.** With neither
  `base_url` / `baseUrl` nor `IDSIDE_BASE_URL`, both SDKs talk to their default,
  `https://sdk.idside.eu`, and a developer who forgot the variable read
  `Could not verify consent (HTTP 404)` with nothing pointing at the
  configuration. A network failure or an unexpected status on the default
  address now ends with the same sentence in both SDKs, naming the default and
  `IDSIDE_BASE_URL`; the class does not change,
  and an address that was given — the default spelled out included — adds
  nothing.
- **Node: a transport built by hand reads `IDSIDE_BASE_URL`, as Python's
  does.** `new HttpConsentClient()` without a base URL went to the default even
  with the variable set — only `wrap()` read it — while Python's
  `HttpConsentClient()` read it in the same configuration. The Node transport
  now resolves its address in its constructor, in Python's order: the argument,
  then `IDSIDE_BASE_URL`, then the default, an empty string counting as absent
  at each step. A hand-built client that relied on the variable being ignored
  goes to the configured address instead; pass the address explicitly to keep
  another one.
- **Node: a transport built by hand reads `IDSIDE_API_KEY`, as Python's does.**
  The same gap for the key: `new HttpConsentClient(url)` without `apiKey` sent
  no `Authorization` header with the variable set, and the cloud answered 401
  to a key that was configured, while Python's transport read it. The key is
  now resolved in the constructor, in Python's order — the `apiKey` option,
  then `IDSIDE_API_KEY`, an empty string counting as absent — and is sent in
  that header only: no error message and no journal line carries it, whichever
  way it was given, and both suites now check it.
- **Node: an identity leaves as the caller wrote it, as in Python.** `wrap()`
  passed `user_id` and `ubt` through `String()`: `true` was authorized as the
  user `"true"`, an array as its joined items, and every object as one person,
  `"[object Object]"` — a JavaScript caller passing `req.user` for
  `req.user.id` merged all their users into one, under one `sub`, with no
  error, where Python sent the value and the cloud refused it. Node now sends
  it untouched too, and the cloud judges it: a non-blank string or a finite
  number names somebody, anything else is a 422 `ContractError`, and the
  provider is not called. A number leaves as a JSON number (a `BigInt` as the
  integer it is); an empty array or object counts as absent, as Python's `[]`
  and `{}` do; and a value JSON cannot write, such as a circular object, fails
  with JSON's own `TypeError`, as Python's encoder fails with its own, instead
  of reporting an unreachable cloud. So does a value JSON would silently drop
  or turn into `null` — a function, a symbol, `Infinity` — which had sent an
  authorize request naming nobody, or `user_id: null`; it now throws a
  `TypeError` before any request, as Python does. `AuthorizeRequest.userId`
  and `ubt` are typed `unknown` for the same reason. What to write instead, in a
  `ConsentClient` of your own: narrow the value before using it as a string.
  `scripts/check_sdk_parity.sh` now runs both SDKs on identities that are not
  strings.
- **`ToolUseRefusedError` no longer points at a widget that may not exist.** Its
  message ended "run the Preference Center so the user can answer", which is
  false where an organization's policy refused: no answer lifts that, and its
  employees have no such popup. The SDK cannot tell which happened, so both SDKs
  now say what is true either way, in the same words, and name every field
  they read (Node named the first only). The remedy points at those fields —
  "Retry without them" — and not at "the tools": a call refused for
  `conversation` under `strip` goes through once the reference is dropped, and
  is refused again if only `tools` is. The class is unchanged; code that
  matched the old text must match the new one. The parity script compares the
  two messages.
- **An API key a header cannot carry is refused before it is sent, and never
  quoted.** A key read from a secret file usually ends with a line break, and
  one pasted by hand can hold a space or a non-ASCII character. Sent as it
  was, the request failed inside the HTTP layer: the SDK reported an
  unreachable cloud — an outage, for a configuration mistake — and chained an
  error whose message quoted the whole `Authorization` header, key included,
  into every log that records a cause (Python: `Illegal header value
  b'Bearer sk_…\n'`; Node: `Headers.append: "Bearer sk_…" is an invalid header
  value`). Both SDKs now drop the whitespace around the key, as `fetch`
  already did in Node (so Python accepts a key ending with a line break), and
  refuse any other character outside printable ASCII where the transport is
  built — `ValueError` in Python, `TypeError` in Node — with a message naming
  `IDSIDE_API_KEY` and the key argument, never the key. A key made only of
  whitespace counts as absent, as an empty one does.

### Known limitations

Carried into the first release, and stated here so that a reader does not have
to discover them:

- **`google-genai`'s async surface is refused, not enforced.** Its async methods
  live at `client.aio.*`, which no descriptor declares — so they are not patched,
  and the guard refuses them with `UnenforcedCallError` rather than letting an
  unproven call out. The async clients of `openai` and `anthropic` are fully
  enforced.
- **Enforced entry points are exactly the descriptors' call sites** plus their
  aliases — and, in Python, the `with_raw_response.*` /
  `with_streaming_response.*` views of those same sites, which wrap the patched
  method and inherit its enforcement. (Node's equivalent is on the call itself:
  an enforced call returns a native `Promise` onto which `.withResponse()` and
  `.asResponse()` are forwarded; the `APIPromise`'s other members are not.)
  Everything
  else on a wrapped client — embeddings, images, audio, moderations, batches,
  file uploads, `beta.*` — is refused rather than silently sent. Use a second,
  unwrapped client for that traffic; there is no bypass option on `wrap()`, by
  design.
- **`Enforced<OpenAI>` is not assignable to `OpenAI`**, and `parse()` / `stream()`
  lose the generic that types their parsed payload. Both are structural
  consequences of making the identity required at compile time.
- **Resuming a response is not enforced, because it creates nothing.**
  `responses.stream(response_id=…)` (and `starting_after=`) replays a response
  that already exists over `GET /v1/responses/{id}` — the request
  `responses.retrieve()` makes. On a wrapped client it goes out **without an ACT
  token and without a ledger entry**: there is no prompt and no model to decide
  about, and a signed proof for a call that created nothing would be a false
  statement in the ledger. It takes no `user_id=`/`ubt=` either, and passes one
  through harmlessly if your code always sends it. This is not a way out of
  enforcement: the call carries no token, so the transport guard refuses it the
  moment it turns out to carry a body. The *creating* form of the same helper —
  `responses.stream(model=…, input=…)` — is enforced like any other AI call.

---

## Release history

- `0.1.0.dev2` / `0.1.0-dev.2` — 2026-11-11, the option lists of the current
  provider libraries, and three calls that escaped enforcement.
- `0.1.0.dev1` / `0.1.0-dev.1` — 2026-11-11, documentation fixes and Python
  `request_hash()`.
- `0.1.0.dev0` / `0.1.0-dev.0` — 2026-11-11, the first pre-release.

[Unreleased]: https://github.com/idside-eu/idside-sdk/compare/v0.1.0.dev2...HEAD
[0.1.0.dev2]: https://github.com/idside-eu/idside-sdk/compare/v0.1.0.dev1...v0.1.0.dev2
[0.1.0.dev1]: https://github.com/idside-eu/idside-sdk/compare/v0.1.0.dev0...v0.1.0.dev1
[0.1.0.dev0]: https://github.com/idside-eu/idside-sdk/releases/tag/v0.1.0.dev0
