# Architecture — ID Side SDK

Architecture document for the source-available SDK and its boundary with the
proprietary cloud infrastructure. The invariants every change here is held to —
and which no version number may trade away — are listed in
[`docs/versioning.md` §3](./versioning.md#3-what-no-version-number-can-change).

## 1. Core principle: the opaque tunnel

Like Stripe for card numbers, ID Side guarantees that **the developer is never in
the loop of consent preferences**. Preferences flow directly from the user (via
the widget or their ID Side account) to ID Side's servers. The developer supplies
an identifier and receives a proof; they can neither read nor alter the
preferences.

**Why this is vital:** if the developer could modify preferences, the ACT token's
cryptographic proof would be worthless (they could collect an opt-out via the
popup, then pass an opt-in to the SDK). The opaque tunnel eliminates that risk.

**Trust model — deterrent + evidentiary:** the tunnel does not make cheating
technically impossible, but a developer who bypasses the SDK (calling the
provider directly) has **no proof** of what was authorized: no ACT token, no ledger
entry, nothing to show an authority (DPA) for that call.

### 1.1 Two modes, one mechanism

The tunnel is used two ways, and the SDK cannot tell them apart — deliberately.
Both produce the same call, the same forced parameters and the same signed
proof. What differs is **who decided**, and therefore what the proof records:

| | **Asked** | **Imposed** |
| --- | --- | --- |
| Who decides | the person, for themselves | the organization, for its employees |
| Surface | the Preference Center — a popup that asks | the same widget, as a read-only panel that states |
| `source` | `widget`, `gpc`, `idside_account` | `default_policy` |
| `legal_basis` | `consent` | `legitimate_interest`, `contractual_obligation`, `public_task`, `legal_obligation`, `vital_interests`, or `not_established` |
| Typical case | a product with end users | a workforce told to use AI at work |

Nothing in the SDK switches between them: `source` and `legal_basis` are
mandatory fields of the authorize response, copied into the ACT token, and both
SDKs carry them as opaque strings. The mode is a property of the account, not of
the integration. A value the cloud adds needs no SDK release: `public_task`,
`legal_obligation` and `vital_interests` were added that way.

**An imposed policy can only tighten.** It turns an option off, never on. A
policy able to switch an option back on would let a customer overturn their own
users' refusals from a dashboard — invariant #1 through the front door. So a
person's own refusal survives a policy that permits, and `source` becomes
`default_policy` only where the policy actually changed something: a policy that
agrees with the user decided nothing, and saying otherwise would misattribute
their decision in the ledger.

**And an imposed policy signs only what the organization declared.** The bases
available are the six Article 6 bases and `not_established`. An organization may
declare `consent`, which the GDPR generally presumes not to be freely given at
work; ID Side collects no answer from the employee, and the token's `source`
shows that the decision was the organization's. This
is the technical reason the imposed mode is not a shortcut past the popup —
[`concepts.md` §4](./concepts.md#4-two-modes-asked-and-imposed) works through
what a developer actually gets if they try it on visitors.

## 2. The two circuits

```
Circuit A — preference collection (frontend → ID Side)
  [User browser] ──HTTPS──▶ [ID Side]
     Preference Center widget: a <script> tag from ID Side that frames
     a CROSS-origin iframe ID Side serves — so the embedding page can
     neither read nor write what is inside it
     detects GPC (Sec-GPC: 1 and navigator.globalPrivacyControl)
  ⇒ the developer's backend sees nothing.        [see widget.md]

Circuit B — AI call (developer backend → SDK → provider)
  [Dev backend] ──consent.wrap(provider)──▶ [SDK]
     1. compute request_hash from the caller payload (before enforcement)
        POST idside.eu/v1/authorize
          → { user_id (or ubt), provider, model, request_hash,
              call_site, credential_postures }
          ← { decision, consent_state, source, legal_basis, binding,
              params_forced, mode, act_token, token_id, expires_at,
              [constraints], [credential_posture] }
     2. apply the params the cloud decided (store=false…)   [Type 1]
     3. attach the signed ACT token, X-AI-Consent-Token header   [Type 2]
     4. the cloud has already appended the hash-chained ledger entry
  ⇒ the developer relays no preference.
```

The request payload is deliberately minimal: an identifier, a provider, a model,
a **hash** of the request, the `call_site` about to be called (two endpoints of
the same provider do not accept the same parameters), and the *labels* of the
credentials the developer provisioned — never their values. Prompts, messages and
completions never leave the developer's backend for ID Side — the SDK is
structurally unable to send them.

The response fields above are the ones both SDKs actually read. Ten are
mandatory: `decision`, `consent_state`, `source`, `legal_basis`, `binding`,
`params_forced`, `mode`, `act_token`, `token_id` and `expires_at`. The SDKs
require `decision: "allow"`, a `test` or `live` mode, object-shaped consent and
forced parameters, the supported binding and constraint values, a bounded
compact-JWS-shaped token, an ACT token identifier and a calendar-valid RFC 3339
expiry. Required strings must be non-empty and printable. `constraints` may be
absent and then means no local restriction; `credential_posture` may be absent
or null and then means the client as constructed is suitable. Any missing or
malformed value is treated as no answer at all (`ConsentUnreachableError`),
before a provider call can start. `source` and `legal_basis` remain opaque
metadata: the SDK validates their string shape but never branches on their
value, so a future provenance or legal basis does not change local enforcement.

On an application that asks its visitors, one property of `consent_state` is
settled before the SDK ever sees it: it arrives **complete**. The cloud fills it
from the option catalogue as the last step of resolution — after the person's
answer and any organization policy, before enforcement and signing — because
what nobody accepted is refused, so an option a person never answered is
`false` rather than absent. That is why neither SDK carries a table of what to
assume about the answers it did not get, and why an option a provider gains next
year is refused from the moment it exists for everyone who answered before it
did: there is nothing to migrate and no list to keep.

The imposed mode is the deliberate exception. There an organization's policy is
the only thing that speaks, expressed as refusals over the application's base
state, and an option the policy does not mention is left unanswered rather than
refused: a base that already refused everything would make every policy
identical. A state signed for an employee can therefore be partial.

The record the cloud *stores* is the other half of the pair and keeps the gap —
what somebody said and what applies to them are two different facts, which is
what lets the Preference Center re-ask. The signed `consent_state` is what
applied, not what was said: read it together with `source` and `legal_basis`,
which say whether anybody decided at all.

The two circuits never cross on the developer's side.

## 3. The two injection types

| | Type 1 — Provider params (UNDERSTOOD) | Type 2 — ACT token (PROOF) |
| --- | --- | --- |
| Mechanism | Forces **existing** parameters (OpenAI `store=false` and `prompt_cache_retention`, Mistral `store=false` on conversations) | Attaches a signed ECDSA P-256 JWT, `X-AI-Consent-Token` header |
| Effect | The provider **reads and applies** → real enforcement, day one | The provider ignores it today → **audit proof**, day one |
| Future | — | If providers adopt it as a protocol, Type 2 absorbs Type 1 |

## 4. Source-available / proprietary boundary

The line below is about WHERE code runs and who can read it, and it is not a
line between free and paid: everything in this repository is readable, and
everything on the other side of the line is not. Nothing here is open source.
It is licensed under the ID Side Source-Available License, version 1.0
([`LICENSE`](../LICENSE)): reproducing, executing and modifying it solely for
non-production internal evaluation, research, and non-commercial testing is
free of charge; production and any other commercial purpose need a paid
commercial licence. Beyond the limited evaluation patent licence of clause 2 of
`LICENSE`, rights under the patents are conveyed only by an executed commercial
licence.

| Source-available (this repo) | The ID Side cloud (proprietary) |
| ----------------------------------- | ---------------------------------- |
| Python SDK (`consent.wrap()`)       | Multi-tenant Consent DB |
| Node.js SDK                         | ACT signing infrastructure (ECDSA keys) |
| Provider adapters + JSON descriptors | Immutable hash-chained ledger |
| The enforcement guard (`UnenforcedCallError`) | Dashboard (Starter + Governance) |
| Token **verification** (`verify_act()`) | Policy Engine (Enterprise) |
| Contributor plugin interface        | Preference Center widget + GPC detection |
| | Fraud Detection Engine |
| | Automated DSR Handler |

**The widget is on the right-hand side, and that is not an accident of
packaging.** It is served by ID Side and framed cross-origin precisely so that
the page embedding it cannot read or write what is inside — the same isolation
that makes the tunnel opaque makes the widget un-vendorable. Shipping it as a
library the developer runs in their own page would put their code between the
user and the answer, and the signature on the other end would attest to
nothing. Its interface is documented in the open all the same:
[`widget.md`](./widget.md).

The *moat* is not in the SDK code; it is in the **multi-tenant immutable ledger**
and the network of consent records behind it. Making the SDK auditable by the
community **strengthens** trust.

## 5. Provider descriptors (plugin system)

Enforcement splits along two axes, and keeping them apart is what makes the SDK
both small and future-proof:

| | **What** to force | **How** to inject it |
| --- | --- | --- |
| Lives in | the **cloud** | a **JSON descriptor**, in this repo |
| Content | consent state → provider params (`store=false`…) | call sites, injection target, protected params |
| Changes when | a provider changes its privacy API | a provider changes its client library |

The SDK holds **no** consent→parameter table. It receives `params_forced` from
`/v1/authorize` and applies them. So when a provider renames a flag, ID Side
updates a row — nobody has to upgrade their SDK.

A descriptor is plain JSON. The canonical copy lives in
[`packages/descriptors/descriptors/`](../packages/descriptors/descriptors/) and
each SDK bundles a byte-identical one, kept in step by a sync test; the shape is
enforced by
[`packages/descriptors/schema/provider-descriptor.schema.json`](../packages/descriptors/schema/provider-descriptor.schema.json),
which is the authority if this page and it ever disagree. Abridged
(`descriptors/openai.json` — the real file also carries a `note` on every call
site and a full `enforcement` map):

```jsonc
{
  "descriptor_version": "2",
  "name": "openai",
  "client_module": "openai",

  // Each call site is an OBJECT, not a path array. `id` is the contract: it is
  // what travels to the cloud as `call_site`, so the cloud can answer per
  // endpoint. `aliases` are extra method names on the same target reaching the
  // same endpoint, patched under the same `id`.
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

  "identity_param": "user_id",           // + optional `ubt_param`, `headers_kwarg`
  "token_header": "X-AI-Consent-Token",
  "param_target": "kwargs",              // "kwargs" | "headers"
  "protected_params": ["model", "messages", "stream"],
  "tools_param": ["tools"],

  // What this provider actually lets a refusal reach, per catalogue option:
  // `enforced`, `contractual`, `account`, `absent` (the API does not do the
  // processing at all) or `unsupported`. Read by the CLOUD, which derives
  // `params_forced` from `when` + `value`; the SDKs never read those, and only
  // their consent helpers read `level`, `protective` and `routable`. It is here
  // because a provider's facts belong in the descriptor, and a second copy in a
  // database drifted from it.
  "enforcement": {
    "retention.conversation": {
      "level": "enforced",
      "params": [
        {
          "name": "store",
          "call_sites": ["chat.completions.create", "responses.create"],
          "when": false,
          "value": false
        }
      ]
    },
    "training.foundation": { "level": "contractual", "protective": false, "note": "…" },
    "memory": { "level": "absent", "note": "…" }
  }
}
```

`forceable_params` is declared **per call site** because the sites differ:
forcing `background` on `chat.completions.create` would raise a `TypeError` for
every user who opted out, and for nobody else.

The generic engine (`ProviderAdapter`) drives it:

- `get_call_sites(client)` — every declared entry point present on the client.
  **All** of them are enforced: a provider exposing two APIs (OpenAI's Chat
  Completions *and* Responses) must not leave one unguarded.
- `extract_request_metadata(**kwargs)` — the model, plus `request_hash`:
  SHA-256 over the RFC 8785 (JCS) canonical form of the caller's pre-enforcement
  payload, with identity and per-request transport headers removed. Forced
  parameters, constraint transformations, routed credentials and the ACT header
  are added later. Canonicalization lets the SDK, cloud and an auditor compute
  the same value.
- `apply_constraints(kwargs, constraints)` — honours the cloud's *non-parameter*
  restrictions, today only `{"tools": "refuse" | "strip"}`: raise
  `ToolUseRefusedError`, or drop the tools. The cloud is never told whether the
  call carried any; it answers from the consent state alone.
- `apply(kwargs, params_forced, token, credential_headers=None)` — merges the
  forced params, attaches the token, and adds the header that selects the routed
  credential. An adapter overriding this **must** forward that last argument, or
  credential routing silently stops working for its provider.
  `protected_params` is a hard denylist: the cloud can never overwrite the
  request itself (`model`, `messages`…).

Descriptors also declare the *identity plumbing*: `identity_param` (default
`user_id`) and `ubt_param` (default `ubt`) name the two exclusive kwargs that
carry the end-user identity, and `headers_kwarg` (default `extra_headers`)
names the kwarg through which the client accepts per-request HTTP headers —
`http_headers` for the native `mistralai` client, for instance. All three are
consumed by the SDK, never forwarded to the provider, and excluded from the
request hash.

Adding a provider (e.g. Cohere) = adding a JSON file. No Python, no core
change, no cloud change; outside pull requests are not accepted yet
([`CONTRIBUTING.md`](../CONTRIBUTING.md#license-of-contributions)). Clients whose API does not fit the
declarative model register a subclass instead (`register_adapter`). The bundled
Gemini adapter is the canonical example: `google-genai` accepts no free-form
kwargs, so the adapter merges `params_forced` into `config` and carries the ACT
token in `config.http_options.headers` (which the google-genai transport merges
over client-level headers).

## 6. Security (summary)

- **UBT (User Binding Token)**: a signed JWT binding `user_id`, `app_id`,
  `session_id`, `consent_state_hash`, issued to the frontend by the ID Side
  widget. The backend passes it to the SDK (`ubt="eyJ..."`) *instead of* a
  `user_id`; the SDK forwards it **opaque** (it never decodes or validates it)
  and the cloud verifies signature and app binding. This closes the spoofing
  gap where a developer collects consent for one user and calls the SDK with
  another. Every authorize response and ACT token records which identity form
  was used (`binding: "ubt" | "user_id"`), so an auditor can weigh the proof.
- **Anti-replay**: `request_hash` + `nonce` + 1h TTL; one token = one request.
- **Fail-closed**: cloud unreachable ⇒ no AI call.
- **Fraud Detection** (cloud): per-`app_id` anomaly analysis.

The public token spec is [`spec/act-token.md`](./spec/act-token.md), and what
about it can never change is
[`versioning.md` §1.2](./versioning.md#12-the-act-token-is-versioned-by-forever).
