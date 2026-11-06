# Providers

Which AI provider clients the SDKs support, which methods they intercept, and
where the consent enforcement is injected. Everything on this page is derived
from the shared descriptors in
[`packages/descriptors/descriptors/`](../packages/descriptors/descriptors/) —
the single source of truth both SDKs bundle byte-identical copies of.

---

## 1. First, what the SDK does *not* decide

**The SDK never chooses which parameters to force.** For each call,
`POST /v1/authorize` returns a `params_forced` object and the SDK applies it
verbatim (minus a protected denylist, §4).

The descriptors do publish the mapping the cloud applies — each `enforcement`
entry says which answer triggers a parameter (`when`), what value it carries
(`value`) and which models it may be forced on (`models.only`, or
`models.except` where the excluded family is closed and known). That is
documentation of a provider's facts, readable by anyone auditing us. Neither SDK
reads those fields: the decision still arrives from `/v1/authorize`, per call,
per user, and an SDK that computed it locally would be deciding on your users'
behalf with a consent state it is not allowed to hold.

That split is deliberate:

| | The **what** — which params to force | The **how** — where to put them |
| --- | --- | --- |
| Decided by | the ID Side cloud | a JSON descriptor, in this repo |
| Changes when | a provider changes its privacy API | a provider changes its client library |
| Costs you | nothing, no SDK release | an SDK upgrade |

So when OpenAI renames a retention flag, ID Side updates a row and your
deployment keeps working unchanged. What follows describes the *how*.

## 2. Supported providers

| Provider | Python client | Node client | Auto-detected by |
| --- | --- | --- | --- |
| OpenAI | `openai` | `openai` | module `openai` / constructor `OpenAI`, `AzureOpenAI` |
| Anthropic | `anthropic` | `@anthropic-ai/sdk` | module `anthropic` / constructor `Anthropic`, `AnthropicBedrock`, `AnthropicVertex` |
| Google Gemini | `google-genai` | `@google/genai` | module `google.genai` / constructor `GoogleGenAI` |
| Mistral | `mistralai` | `@mistralai/mistralai` | module `mistralai` / constructor `Mistral` |
| DeepSeek | `openai` | `openai` | base URL `api.deepseek.com` |

Python detects the provider from the client's module path; Node.js has no module
path at runtime, so it matches the constructor name first and falls back to
structurally probing the descriptor's call sites. If detection fails, Node lets
you be explicit — `consent.wrap(client, { provider: "openai" })` — while Python
takes an `adapter=` instance instead (see §7).

### OpenAI-compatible providers

Groq, Together, xAI, vLLM and DeepSeek are all reached through the `openai`
client pointed at a different `base_url`. The *mechanics* are OpenAI's, so the
`openai` descriptor covers them with no extra work — but the class name says
nothing about who receives the request, and the provider name is what tells the
ID Side cloud which rules to apply.

So a descriptor may claim base-URL hosts of its own, and one that matches wins
over the module's default. DeepSeek does; the others do not yet, and are
recorded as `provider: "openai"`. That is a deliberate default rather than an
oversight: the mechanics really are OpenAI's, and being wrong in the other
direction would apply one vendor's parameters to another vendor's server.

```python
from openai import OpenAI
from idside import consent

client = consent.wrap(OpenAI(base_url="https://api.deepseek.com", api_key=...))
# the proof records provider: "deepseek", not "openai"
```

If you route several providers through one client class, wrap each client
instance separately — detection reads the instance, not the class.

## 3. Intercepted call sites

Enforcement is three layers, and they do different jobs. The declared call sites
(§3.1) are *enforced*: authorized, parameterized, token-carrying. Derived
clients (§3.2) keep that enforcement instead of losing it. And the net (§3.3)
*refuses* everything else that would leave a wrapped client carrying a body,
because a list of declared endpoints can never be exhaustive.

### 3.1 Declared call sites

Every method listed here is wrapped when it exists on the client. Declared sites
that the installed client version does not expose are skipped; if *none* exists,
`wrap()` raises `UnsupportedProviderError` rather than returning a client that
silently enforces nothing.

| Provider | Python call sites | Node call sites | Aliases |
| --- | --- | --- | --- |
| OpenAI | `chat.completions.create`, `responses.create` | same | `parse` on each; `stream` on each, in Python only |
| Anthropic | `messages.create` | same | `parse`; `stream` in Python only |
| Gemini | `models.generate_content`, `models.generate_content_stream` | `models.generateContent`, `models.generateContentStream` | — |
| Mistral | `chat.complete`, `chat.stream`, `fim.complete`, `fim.stream`, `agents.complete`, `agents.stream` | same | — |
| DeepSeek | `chat.completions.create` | same | `parse`; `stream` in Python only |

An **alias** is another method name on the *same* object that reaches the *same*
HTTP endpoint. `chat.completions.parse` posts to `/v1/chat/completions` exactly
as `create` does — but the client library implements it with its own internal
POST rather than by calling `create`, so wrapping `create` alone would leave it
unenforced. An alias is patched under the **same `call_site` id** as the site it
belongs to: the cloud is told `chat.completions.create` either way, and no new
identifier appears on the wire. An alias the installed client does not expose is
skipped, exactly like an absent call site.

One asymmetry is deliberate, and it covers every `stream()` helper —
Anthropic's `messages.stream()`, OpenAI's `chat.completions.stream()` and
`responses.stream()`. Node does **not** patch them: they return their stream
object synchronously while an enforced wrapper returns a promise, so patching
would change the type callers write against. Node does not need to: a JS helper
forwards one body object, identity included, to the patched `create`. Python's
helpers take a fixed keyword signature instead — `user_id=` is a `TypeError` on
them, so the identity cannot reach `create` that way and the helper has to be a
call site of its own. Both SDKs enforce every streaming call; only the route
differs, and each SDK has tests that watch the wire to prove it.

In Python the helper is never the call that sends, on either client shape: it
returns something you `with` (or `async with`), and the request happens when you
enter it. So the SDK splits along the seam the helper already has — it consumes
the identity and hashes the request at the call, where a mistake belongs, and
asks the cloud inside `__enter__`/`__aenter__`, next to the request it
authorizes. A stream nobody enters made no request, so it needed no proof, and
gets no ledger entry: the same number on a blocking client and an async one, for
the same abandoned stream. (Mistral's `chat.stream()` is a *call site*, not one
of these helpers, and it sends when you call it — so it is authorized when you
call it.)

One consequence, on one endpoint, and it is handled rather than inherited.
`responses.stream()` is overloaded: called with `response_id=` (or
`starting_after=`) and no `input`/`model`, it *resumes* a response that already
exists, over `GET /v1/responses/{id}` — the same request `responses.retrieve()`
makes. Enforced like the creating form it demanded an identity for a call that
creates nothing, authorized with an empty model, and signed a token asserting an
AI call that never happened. So the openai descriptor names those two arguments
in `resuming_params`, and a call carrying either is passed straight through:
**no identity required, no ACT token, no ledger entry.** Your `user_id=`, if you
pass one out of habit, is consumed like anywhere else and never reaches the
provider.

Passing a call through is not a bypass and cannot be turned into one. Nothing
there decides that no request will be made — only that this SDK will not certify
one — and a call that goes through carries no ACT token, so the net (§3.3)
refuses it the moment it turns out to carry a body. The Node SDK reaches the
same outcome from the other side: it never patches `stream()`, and that helper's
resuming form calls `responses.retrieve()`, which is not a call site either.

An alias may itself call the patched method (Anthropic's `parse` does). The
outer wrapper creates a private, context-local permit after authorization. A
nested patched method is passed through only when that one-shot permit matches
the wrapper instance, call site and exact ACT token; a public header by itself is
never proof that enforcement happened. The context remains active across async
work and deferred stream entry, while the token continues to travel with the
provider request.

Each endpoint carries **its own** list of parameters the cloud may force there,
and the SDK tells the cloud which one it is about to call. Endpoints of the same
provider do not accept the same parameters: OpenAI's `background` is a real
retention control on `/v1/responses` — it keeps the response for about ten
minutes so a client can poll for it — and does not exist on
`/v1/chat/completions`. A single per-provider list could only ever be their
intersection, losing the lever, or their union, breaking every
`chat.completions` call for exactly the users who opted out.

An SDK old enough not to send the endpoint keeps working: it receives what is
safe on every endpoint of that provider.

Both SDKs patch the method **on the client's resource object**, not through a
proxy wrapper. Convenience methods that internally route through a patched
method are therefore enforced too — Mistral's `chat.parse` funnels into
`chat.complete`, which is why it is not listed separately.

### 3.2 Derived clients

`client.with_options(...)`, `client.copy(...)` and `client.withOptions(...)`
build a fresh client object, whose methods would be unpatched. Both SDKs patch
those methods as well, so the client handed back is already wrapped — same
adapter, same consent client, `credentials` re-resolved against it — and so is
anything derived from *it*, recursively.

A derivation is checked before it is handed back, because it is a one-line way to
rebuild the client around everything a per-call option is refused for. Timeouts
and retry counts pass. A different provider or a different host is refused
([`UnsupportedProviderError`](./errors.md#unsupportedprovidererror)): the derived
client inherits the origin `wrap()` fixed on its parent, it never fixes a new
one. So is a transport, a default query or a middleware — in Python, middleware
is judged on what it would *change*, so `with_middleware()` handed none derives
normally while `with_middleware(m)` is refused. A new key, token,
organization or project, or a reserved header in the default headers, is refused
as a [`CredentialRoutingError`](./errors.md#credentialroutingerror): moving calls
onto another credential is what `credentials=` declares. To change any of these,
build a client with it and wrap that one.

```python
fast = client.with_options(timeout=5.0)      # still enforced
fast.chat.completions.create(..., user_id="usr_marie_456")
```

### 3.3 The net: what a wrapped client refuses to send

A list of declared endpoints is a snapshot of one provider's API on one day. A
new resource ships, a helper posts to the transport directly, a namespace nobody
declared sits on the same client — and before, each of those reached the
provider with no authorization, no ACT token and no ledger entry, silently.

So both SDKs also watch the client's own HTTP transport — **every** transport it
owns, not the first one found. **Any request that did not go through enforcement
and is not a `GET`, `HEAD`, `OPTIONS` or `DELETE` is refused before it leaves the
process**, with [`UnenforcedCallError`](./errors.md#unenforcedcallerror).

"Every" is load-bearing in Python, where `httpx` splits blocking and async into
two unrelated classes and one client can hold both: a `google-genai` `Client`
owns an HTTP transport *and* an async one, and exposes the async methods at
`client.aio.*`. Guarding one of two routes out guards nothing.

That is where the line is drawn, and it is drawn on the HTTP method for two
reasons. A body-bearing method (`POST`, `PUT`, `PATCH`) is an inference call
until proven otherwise — it carries something to infer on. The bodyless methods
are the management surface: `models.list`, `files.retrieve`, `files.delete`,
`batches.retrieve`, polling `GET /v1/responses/{id}`. They carry no prompt, and
`/v1/authorize` could not decide them in any case, because it requires a `model`
they do not have.

The guard is a **detector, not an enforcer**, and it is deliberately narrow. It
reads the HTTP method, the URL's origin and path, and the named `token_header`.
The header value must match the private permit for this wrapped client; the
permit is only good for the **origin** `wrap()` fixed from the client's own base
URL, and the first body-bearing request binds it to one method and path. A copied
header therefore cannot authorize another client, endpoint or host; retries and
streaming continuation on the same route still pass. An enforced request is sent
**without following redirects**: a `307` or `308` would otherwise re-send the
prompt, the token and the key to a host nobody checked, so it surfaces as the
provider library's status error instead. The guard never reads the body, query
string or any other header. It hashes nothing, logs nothing and sends nothing
anywhere.

What the guard cannot see is a network layer you install yourself when you
build the client — a custom `http_client` / `fetch`, `httpx` event hooks, a
Mistral `before_request` hook, an Anthropic client-level middleware. Those run
after it, with the request in hand. They are your code, not an option the SDK
offers, and no library running inside your process can police them; the ledger
then records what was authorized, not what that layer chose to send. What is
*declared* rather than coded is refused, because it can be read: `wrap()` refuses
a client built with a body override (`http_options.extra_body` on `google-genai`,
`httpOptions.extraBody` on `@google/genai`, a `fetchOptions` carrying `body`,
`method` or `headers` on the Node `openai` and `anthropic` clients) or with a
reserved header in its construction options. Per-call options able to do the same thing are
refused before authorization — see
[`UnenforceableOptionError`](./errors.md#unenforceableoptionerror).

**What this now refuses on a wrapped client:**

| | |
| --- | --- |
| `embeddings.create` · `images.*` · `audio.*` · `moderations.create` | body-bearing, not declared anywhere |
| `batches.create` · `files.create` (uploads) | body-bearing management calls |
| `beta.*` namespaces | previews, by definition not in a descriptor |
| Gemini `chats.create(...).send_message` | the chat session posts on its own |
| Gemini `client.aio.*` (Python) | the async surface of that same client, declared by no descriptor |
| Gemini `interactions` · `agents` · `triggers` · `webhooks` · `environments` (Node) | the NextGen surfaces: a second API on the same client, each building its own HTTP client, declared by no descriptor |
| anything a provider adds after this release | which is the point |

**And what passes because it is already enforced: the response wrappers.**
`with_raw_response.*` and `with_streaming_response.*` are not a parallel
resource tree. On the `openai` and `anthropic` clients they are
`functools.cached_property` objects on the resource, so the wrapper is built on
first *access* — which happens after `wrap()` has patched the instance — and it
closes over the patched method. A call through either one authorizes, carries
the forced parameters and the ACT token, and reaches the ledger like any other.

They inherit the enforcement of the method they wrap, in both directions: on a
declared call site (`chat.completions.with_raw_response.create`,
`messages.with_streaming_response.create`) the call is enforced, and on an
endpoint no descriptor declares (`embeddings.with_raw_response.create`) it is
refused exactly like the plain method. So read a rate-limit header or stream a
response byte by byte on the wrapped client — there is nothing to move
elsewhere, and moving it would cost you the proof.

**The remedy for what the table above refuses is a second, unwrapped client**
for that traffic:

```python
enforced = consent.wrap(OpenAI())          # inference
plain    = OpenAI()                        # embeddings, uploads, batches, beta

enforced.chat.completions.create(..., user_id="usr_marie_456")
plain.embeddings.create(model="text-embedding-3-small", input="…")
```

There is deliberately **no bypass option on `wrap()`** — no
`allow_unenforced=`, no per-call escape, and there will not be one. A flag that
let a caller send an unproven AI request from an enforced client would be the
opaque tunnel defeated through the back door.

If a refused endpoint really does carry inference, that is a missing call site,
not a missing flag: [open an issue](https://github.com/idside-eu/idside-sdk/issues)
and it becomes a descriptor change.

**Where the net cannot be installed, and how you find out.** The SDK finds the
transport by walking the client's own data attributes — never evaluating a
property, which is arbitrary code that could open a connection or raise from
somewhere unrelated — to a bounded depth, and touching a bounded number of
objects. That covers the clients above (`openai`, `anthropic`, `google-genai`,
`mistralai`); Node also guards both request routes exposed by the real
`@google/genai` `GoogleGenAI` client, and the HTTP clients its NextGen surfaces
build on demand — those are made after `wrap()` has run, and a new one per
`api_version`, so what is guarded is each surface's `getClient()` and
`getNextGenClient()` rather than a transport that does not exist yet. Reading a
surface is what builds it, so nothing is read on your behalf and the library's
own experimental-usage warnings stay yours to trigger. A client that exposes no supported
transport — the intentional fake/custom-client shape used in integrations and
tests — records `net=false`, while its declared call sites remain enforced.

If the bounded scan cannot finish, one of several transports refuses the patch,
or the real Gemini request boundary is incomplete, `wrap()` raises
`UnsupportedProviderError` before patching any call site. The low-level scanner
calls this result `partial` and rolls back transport changes from that attempt,
without removing a guard installed earlier by another wrapped client. No
successfully wrapped client can journal `net=partial`. Use a supported
provider-client version rather than accepting an open route.

## 4. Where consent is injected

Two things are injected on each call: the cloud-decided parameters (Type 1) and
the ACT token (Type 2). The descriptor's `param_target` says where the first go;
`headers_kwarg` says how per-request headers reach the provider's HTTP layer.

| Provider | `param_target` | Forced params land in | ACT token travels in |
| --- | --- | --- | --- |
| OpenAI | `kwargs` | the request body | Python `extra_headers=` · Node `options.headers` |
| Anthropic | `kwargs` | the request body | Python `extra_headers=` · Node `options.headers` |
| Gemini | `kwargs` (merged into `config`) | `config` | Python `config.http_options.headers` · Node `config.httpOptions.headers` |
| Mistral | `kwargs` | the request body | Python `http_headers=` · Node `options.headers` |
| DeepSeek | `kwargs` | the request body | Python `extra_headers=` · Node `options.headers` |

The header name is `X-AI-Consent-Token` for every provider
(`token_header` in the descriptor).

### Constraints: what the SDK *refuses*, as opposed to what it sets

Beyond parameters, the cloud can return a **constraint** — today only
`{"tools": "refuse"}` or `{"tools": "strip"}`. It is a different kind of
instruction, and deliberately not a parameter: forcing a value writes your
request, which ID Side is never allowed to do to `tools`. Refusing a call is a
separate authority and does not get to borrow that mechanism.

When `agentic_tools` is not accepted for a user and your call hands the model a
tool, the SDK raises [`ToolUseRefusedError`](errors.md#tooluserefusederror)
**before anything reaches the provider** — or removes the tool definitions where
the constraint says `strip`. A call carrying no tool is untouched, and an empty
`tools` list carries none.

"A tool" is read wide, because a refusal is only as wide as what the SDK looks
at. Besides the provider's `tools_param`, each call site declares its
`tool_fields`: `functions` and `web_search_options` on OpenAI chat;
`conversation`, `previous_response_id`, `prompt` and tool-carrying `input` items
on Responses; `mcp_servers` on Anthropic; `cached_content` on Gemini; `agent_id`
on Mistral agents and conversations. The ones that point at something stored
server-side — an agent, a cache, a conversation — are refused even under
`strip`, since the SDK cannot see whether they hold tools and cannot remove them
without changing the call. *Not accepted* covers cases
that resolve identically: the user refused, the user was never asked, or an
organization policy refused for them. What nobody accepted is refused, so an
application that hands the model tools with no Preference Center running is
refused on every call, for every user.

This is the only lever that works on every provider, because it does not come
from any of them: every zero-retention arrangement excludes agentic features
while the APIs themselves do not block them, and the SDK is the only component
that sees the outgoing request.

Note what is **not** sent to ID Side to make this work: nothing. The constraint
follows from the user's consent state alone. You never tell us whether a given
call carries tools, and we never ask — the SDK answers that on your machine.

Gemini needs a dedicated adapter class rather than the generic engine:
`generate_content` accepts exactly `(model, contents, config)` and no free-form
keyword arguments, so there is no top-level place to inject anything — the
adapter merges forced params into `config` and nests the token in
`config.http_options.headers`, normalizing a `GenerateContentConfig` object to
its dict form without mutating the caller's object.

### Protected parameters

`params_forced` is filtered before it is applied: the descriptor's
`protected_params` is a hard denylist the cloud can never override. It exists so
that a compromised or buggy authorization response cannot rewrite the request
itself — swap the model, replace the prompt, or silently switch a call to
streaming.

| Provider | `protected_params` |
| --- | --- |
| OpenAI | `model`, `messages`, `stream` |
| Anthropic | `model`, `messages`, `system`, `stream`, `cache_control`, `container` |
| Gemini | `model`, `contents`, `config`, `http_options` |
| Mistral | `model`, `messages`, `prompt`, `agent_id`, `stream`, `http_headers` |

### What actually gets forced, in practice

The values below are what the cloud is expected to return; they are policy, not
SDK constants, and they can change without an SDK release.

| Provider | Typical `params_forced` | Why |
| --- | --- | --- |
| OpenAI | `{"store": false}`, plus `{"background": false}` on `responses.create` and `{"prompt_cache_retention": "in_memory"}` | the only provider with several per-request retention levers, on the wire today |
| Anthropic | `{}` | its per-request lever, `inference_geo`, chooses between `global` and `us` — neither of which answers *inside my region*, so no option forces it and the allowlist is empty (see the [privacy matrix](./providers-privacy-matrix.md)) |
| Gemini | `{}` | the Gemini API exposes no per-request no-training parameter — retention and training controls are account-level |
| Mistral | `{"store": false}` on `beta.conversations.start`, `{}` everywhere else | `chat.complete` is stateless and has no such parameter; this is why the allowlist is per endpoint |
| DeepSeek | `{}` | its terms reserve training on API input and it exposes no per-request control |

Where the column reads `{}` the enforcement value is entirely in Type 2: the ACT
token and its ledger entry. That is not a degraded mode — it is the normal state
of the ecosystem today, and precisely the gap the
[token spec](./spec/act-token.md) aims to close.

## 5. The descriptor format

A descriptor is a small JSON file validated against
[`schema/provider-descriptor.schema.json`](../packages/descriptors/schema/provider-descriptor.schema.json)
(a closed schema — unknown fields are rejected).

```jsonc
{
  "descriptor_version": "2",
  "name": "openai",
  "client_module": "openai",
  "call_sites": [
    {
      "id": "chat.completions.create",
      "path": ["chat", "completions", "create"],
      "aliases": ["parse", "stream"],
      "forceable_params": ["prompt_cache_retention", "store"]
    },
    {
      "id": "responses.create",
      "path": ["responses", "create"],
      "aliases": ["parse", "stream"],
      "forceable_params": ["background", "prompt_cache_retention", "store"]
    }
  ],
  "identity_param": "user_id",
  "token_header": "X-AI-Consent-Token",
  "param_target": "kwargs",
  "protected_params": ["model", "messages", "stream"],
  "tools_param": ["tools"],
  "forceable_params": ["background", "prompt_cache_retention", "store"]
}
```

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `descriptor_version` | yes | — | version of the descriptor *format* (currently `"2"`) |
| `name` | yes | — | canonical provider name, also the adapter registry key |
| `client_module` | yes | — | client library module, used for Python auto-detection |
| `client_base_url_hosts` | no | `[]` | hosts identifying this provider when several descriptors share a `client_module` (see §2) |
| `call_sites` | yes | — | every AI call method to intercept, each with its own `id`, `path` and `forceable_params` |
| `call_sites[].aliases` | no | `[]` | other method names on the same object reaching the same endpoint (`parse`, `stream`), intercepted under that site's `id` (see §3.1) |
| `identity_param` | no | `user_id` | kwarg carrying the developer-asserted identity |
| `ubt_param` | no | `ubt` | kwarg carrying the widget-issued User Binding Token |
| `token_header` | no | `X-AI-Consent-Token` | header carrying the ACT token |
| `param_target` | no | `kwargs` | `kwargs` or `headers` |
| `headers_kwarg` | no | `extra_headers` | kwarg through which the client accepts per-request headers |
| `protected_params` | no | `[]` | fields the cloud may never override |
| `tools_param` | no | — | path to the agentic tools argument, read to honour a `tools` constraint |
| `reserved_headers` | no | `[]` | headers that select a credential, account or project (`Authorization`, `X-Api-Key`…): a per-call header with one of these names is refused, case-insensitively; `token_header` is always reserved |
| `forceable_params` | no | `[]` | the union of every call site's list — a derived convenience, pinned to the sites by a test |
| `call_sites[].tool_fields` | no | `[]` | every other argument of that endpoint that can hand the model a tool: `path` (Python spelling), optional `js_path`, and `kind` — `definitions`, `enabler` or `input_items` (with `item_types`) |
| `enforcement` | no | `{}` | per consent option, how far this provider lets us honour the answer (`level`: `enforced`, `contractual`, `account`, `absent` — the API does not do the processing at all — or `unsupported`; for an `enforced` one the parameters with their `when` / `value` / `models.only` or `models.except`; for a `contractual` one, `protective`: whether the terms rule the processing out or allow it). Read by the cloud and by the privacy matrix; in an SDK only by the consent helpers (`decidable_options()` / `disclosures()`, which read `level`, `protective` and `routable`), never on the enforcement path |
| `credential_routing` | no | — | how a developer provisions one credential per privacy posture; its **presence** is what lets an SDK accept `credentials=` (§6) |
| `notes` | no | — | human-readable quirks; SDKs must not interpret it |

A call site's `id` is the **dotted Python path**, and it is what travels to the
cloud. It is fixed in the Python spelling on purpose: the Node client may name
the same endpoint differently (`generateContent` vs `generate_content`), and one
endpoint has to have one name on the wire.

`identity_param`, `ubt_param` and `headers_kwarg` are all consumed by the SDK,
never forwarded to the provider, and excluded from the `request_hash`.
`tools_param` is only ever **read** — the SDK never writes it, and a tool
refusal removes the argument rather than emptying it, because several providers
reject an empty `tools` list.

## 6. Credential routing

No provider lets you switch training off per request. The setting lives on the
**credential**, and each vendor scopes it differently:

| Provider | The posture belongs to a | Switched per request by | So routing means |
| --- | --- | --- | --- |
| OpenAI | project | **`OpenAI-Project`**, a header the API validates | a project id per posture |
| Anthropic | workspace | nothing | a second client, built with that workspace's key |
| Gemini | project | nothing | a second client (the billing tier of the project behind the key is what decides whether prompts train Google's models, except for a developer in the EEA, Switzerland or the UK, whose unpaid quota gets the Paid Services data-use terms too) |
| Mistral | organisation | nothing | a second client (the training toggle is an organisation setting in the console) |
| DeepSeek | — | — | **nothing**. Its terms reserve training on API input and no account-level posture exists to route between, so `credentials` is refused rather than accepted and ignored |

One key therefore means one posture for every user of your app at once, however
carefully each of them answered. Provision one credential per posture and the
SDK sends each call on the one that user's answer requires — an account-level
control becomes a personal one. It is the only route by which a training opt-out
can ever be honoured per user.

At Gemini, an app offered to users in the EEA, Switzerland or the UK may use
only Paid Services, so for those users there is no unpaid project to route to.
At Mistral, a residency posture is a second client built on the regional
endpoint, `server_url="https://api.eu.mistral.ai"` (`serverURL` in Node):
before `mistralai` 2.7.0, `server="eu"` still names the global
`api.mistral.ai`.

> **Which calls are routed.** The SDK labels your credentials, sends each call
> on the one a decision names, and fails closed (`MissingCredentialError`) when
> none carries it. The cloud names a posture only where a provider rule asks for
> one: at Mistral, a training refusal names `no_training`, a key on a Mistral
> organisation whose console has training turned off. With `routed`,
> `decidableOptions()` / `decidable_options()` lists what your credentials
> *would* make decidable, not what the consent widget asks: the widget does not
> know which credentials you hold.

```python
from idside import consent

client = consent.wrap(
    OpenAI(),
    credentials={
        "default": "proj_medhelp_default",      # → OpenAI-Project
        "zero_retention": "proj_medhelp_zdr",
    },
)

claude = consent.wrap(
    Anthropic(),                                # no such header at Anthropic
    credentials={"no_training": Anthropic(api_key=PRIVATE_WORKSPACE_KEY)},
)
```

```ts
const client = consent.wrap(new OpenAI(), {
  credentials: { default: "proj_medhelp_default", zero_retention: "proj_medhelp_zdr" },
});
```

**You label; you do not choose.** Which posture a given consent state requires
is policy: ID Side resolves it and returns the label, the SDK matches it against
what you provisioned. There is no callback to override it — that would hand you
the user's preferences, which is the one thing this architecture never does.

**A posture you have not provisioned stops the call**
([`MissingCredentialError`](./errors.md#missingcredentialerror)), it does not
fall back. On an application that serves employees, the cloud reads the labels
you declare first: when the policy refuses an option only a posture you have
not labelled can carry, Observe names no posture — the call leaves on your
ordinary credential, and its proof records the refusal as not applied — and
Enforce holds the call (`policy_hold`). Either way the SDK never picks another
credential itself. And the credential never leaves your process: ID Side is
told which *labels* exist, never their values. A per-call header that would
override the credential — `Authorization`, `X-Api-Key`, `x-goog-api-key`,
`OpenAI-Project` — is refused before authorization, so a key can never ride
along on a call and never reach the `request_hash`. (Other per-call header
values are excluded from the hash where they travel in `headers_kwarg`; on
Gemini they travel inside `config`, and are hashed with it.)

**What it does to your consent dialog.** An option the descriptor marks
`routable` is a disclosure while you hold one credential and a real question
once you hold two — nobody can be routed away from a posture that is the app's
only one. Pass what you provisioned to get the honest list:

```python
options = decidable_options(["mistral"], routed={"mistral": list(CREDENTIALS)})
# training.foundation is now decidable; option.routed_by == ("mistral",)
```

```ts
const options = decidableOptions(["mistral"], { mistral: Object.keys(CREDENTIALS) });
// option.routedBy === ["mistral"]
```

## 7. Adding a provider

For a client that fits the declarative model, adding a provider is a JSON file
— no Python, no TypeScript, no cloud change. Outside pull requests are not
accepted yet — [`CONTRIBUTING.md`](../CONTRIBUTING.md#license-of-contributions)
says why; to propose a provider, write to contact@idside.eu. The steps are the
ones a maintainer follows:

1. Add `packages/descriptors/descriptors/<name>.json` and make it validate
   against the schema.
2. List **every** AI entry point in `call_sites`, and every convenience method
   that reaches the same endpoint on its own in that site's `aliases`. A missing
   site is not a silent hole any more — the net (§3.3) refuses the call — but it
   is a working endpoint your users cannot reach through a wrapped client.
3. Copy the file byte-identically into both bundled directories,
   `packages/python/src/idside/providers/` and `packages/node/src/providers/`
   (a sync test in each SDK fails on any divergence).
4. Run both test suites.

For a client whose API does not fit — no per-request headers, no free-form
arguments — override the engine instead. Gemini is the shipped example.

```python
# Python
from idside.adapters import ProviderAdapter, register_adapter

class MyProviderAdapter(ProviderAdapter):
    def apply(self, kwargs, params_forced, token, credential_headers=None):
        ...  # place the forced params, the token and the routed credential
        ...  # (§6) where this client accepts them — dropping the last one
        ...  # fails the call rather than sending it on the wrong credential

register_adapter("myprovider", MyProviderAdapter)  # name matches the descriptor
```

```ts
// Node.js
import { GenericAdapter, registerAdapter } from "@idside/sdk";

class MyProviderAdapter extends GenericAdapter {
  override buildEnforcedArgs(args, paramsForced, token, credentialHeaders) {
    /* … forward credentialHeaders (§6): a call that leaves without it would
       go out on the credential the user's answer excluded */
  }
}

registerAdapter("myprovider", MyProviderAdapter);
```

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the process a pull request will
follow once outside contributions are accepted, and
[`packages/descriptors/README.md`](../packages/descriptors/README.md) for the
descriptor conventions in detail.

## Next

- [Concepts](./concepts.md) — why Type 1 and Type 2 injection are different things
- [Error catalogue](./errors.md) — including `UnsupportedProviderError`
- [Architecture](./ARCHITECTURE.md#5-provider-descriptors-plugin-system)
