# Concepts

The mental model behind the ID Side SDK, for a developer who has never heard of
the product. Nothing here is required to make a first call — see the
[Python quickstart](./quickstart-python.md) or the
[Node.js quickstart](./quickstart-node.md) for that — but it explains why the
API looks the way it does, and in particular why it refuses to let you do
certain things.

---

## 1. The problem

Your application sends user data to an AI provider. Somewhere in your product,
a user told you something about how they want that to happen: do not train on my
data, delete my history, do not use provider X. Six months later a data
protection authority, a customer's legal team or a DSR (data subject request)
asks a simple question:

> Prove that this specific API call, made on this date for this user, honored
> what that user had chosen at that moment.

Most stacks cannot answer. The consent lives in a database row that has since
been updated in place; the AI call lives in an application log, if at all; and
nothing cryptographically ties the two together. A screenshot of a settings page
is not evidence. **Without a verifiable record, that question is very hard to
answer.**

ID Side exists to help you answer it, with a signed record produced at the
moment of the call.

## 2. Consent as infrastructure

The design borrows its shape from payments. Before Stripe, every merchant
handled raw card numbers and every merchant was therefore in PCI scope. Stripe's
insight was not a nicer API — it was moving the sensitive data *out* of the
merchant's reach, so the merchant could no longer leak it even by accident.

ID Side applies the same move to AI consent:

| | Payments (Stripe) | AI consent (ID Side) |
| --- | --- | --- |
| Sensitive material | the card number | the decision that governs each AI call: an organization's policy, or a person's answer |
| Who must never hold it | the merchant | the code that makes the call |
| What the app handles instead | a token | an API key — and, when it wants to, a `user_id` or a UBT |
| What comes back | a charge receipt | a signed **ACT token** + a ledger entry |

The consequence is the same in both cases: a whole class of mistakes becomes
structurally impossible rather than merely forbidden.

## 3. The opaque tunnel

This is the central invariant, and the one that surprises people.

**The SDK has no function that sets, overrides or even reads a policy or a
user's preferences.** There is no `set_preferences()`, no `override_consent()`,
no `get_preferences()` returning something you could edit and send back. You
make the call, with your API key — naming the person it is for if you want to;
the SDK returns an enforced call. The decision is made on ID Side, on a path the
code that makes the call is never on: an organization sets its policy on the
ID Side dashboard, and a person who is asked answers ID Side directly.

```
Circuit A — the decision               Circuit B — enforcement
────────────────────────               ───────────────────────
dashboard · user's browser             your backend
      │                                      │
      │ policy (dashboard), or               │ 1. API key + provider + model
      │ preferences (widget · GPC · account) │    + request hash (+ identity)
      ▼                                      ▼
  ┌─────────────────────────────────────────────────────┐
  │                 sdk.idside.eu                   │
  │  resolves consent · signs the ACT token · ledger    │
  └─────────────────────────────────────────────────────┘
                                             │
                                             │ 2. forced params + ACT token
                                             ▼
                                       AI provider call
```

The two circuits never cross on your side. Details and the exact payloads are in
[`ARCHITECTURE.md`](./ARCHITECTURE.md#2-the-two-circuits).

### Why the missing setter is the feature

Suppose the SDK let you pass the preferences you had collected. Then the certificate
it produces would attest to *what you told it*, not to *what the user chose*. A
developer could show an opt-out dialog, record the opt-out in their own database,
and still call the SDK with `training=true`. The signed token would faithfully
certify a lie, and every ACT token in the world would be worth exactly nothing —
because no auditor could distinguish an honest one from that.

Removing the setter is what makes the signature meaningful. The API is
deliberately narrower than you might want, and that narrowness is the product.

An organization's policy is not an exception. It is set on the ID Side
dashboard, not by the code that makes the call, it can only refuse, and the
certificate says it came from the organization (§4). The code path that sends the
prompt has no say in what applies to it.

### What the tunnel does *not* claim

It does not make cheating impossible. Nothing stops a developer from calling
`openai.chat.completions.create()` directly and skipping ID Side entirely — the
SDK runs in their process, on their machine.

The model is **deterrent and evidentiary**, not coercive. A developer who
bypasses the SDK has no ACT token and no ledger entry for that call, and
therefore nothing to show for that call. The model does not make violations
impossible; it makes them *visible as gaps in the record*. This is the
same reason double-entry bookkeeping works without preventing anyone from lying.

## 4. The policy: what applies to a call, and who decided

One product, one mechanism. The organization that holds the API keys sets a
policy on the ID Side dashboard — an employer equipping its staff with AI, or a
publisher whose application serves its own users — and that policy applies to
every call made with its keys. Holding one of its keys is acting under its
policy. On the dashboard its keys are grouped in **strainers**: each strainer
adds its own refusals to the organization's, and every call made with one of its
keys applies them. The SDK is byte-for-byte the same for everyone — same `wrap()`, same
authorize call, same signed certificate — and the certificate says what applied and who
decided:

| Claim | On a call under an organization's policy |
| --- | --- |
| `source` | `default_policy` — the decision is the organization's |
| `legal_basis` | the basis the organization declared: `legitimate_interest`, `contractual_obligation`, `public_task`, `legal_obligation`, `vital_interests` or `consent` — or `not_established` while it has declared none |
| `binding` | `api_key` when the call named nobody, `user_id` or `ubt` when it named someone ([§8](#8-naming-the-end-user-user_id-vs-ubt)) |
| `refused_by` | which layer refused each option: the organization, a team, the strainer |

Naming the person a call is for is optional, and most calls name nobody: the
certificate's subject is then the API key the call was made with. Name a person, or a
team, when you want the certificate to say whom the call was for (§8). The policy and
the declared basis apply either way.

Three properties keep a policy from being a preference setter in disguise:

- **It is set on the dashboard, never from code.** The SDK has no parameter for
  it ([§3](#3-the-opaque-tunnel)), so the code that sends a prompt cannot loosen
  what applies to that prompt.
- **It can only refuse.** A policy turns an option off, never on. A refusal is
  applied to the call wherever it can be — a forced provider parameter, a
  declined call, a dedicated credential — and the token names the layer that
  made it; where it cannot, the strainer's switch for the call's provider
  (below) decides what happens.
- **It signs what the organization declared, as the organization's.** The basis
  in the certificate is the one the organization declared on the dashboard, carried
  with `source: "default_policy"`, so that nobody reading it mistakes a
  declaration for an answer someone gave. ID Side does not assess the
  declaration. An organization may declare `consent` too; where it does, the
  token still shows that the decision came from the organization and that ID
  Side collected no answer — and consent is rarely a valid basis between an
  employer and an employee, who can seldom refuse freely.

What the organization owes the people its calls concern is **transparency**:
what was decided, by whom, on what basis. A policy is a statement, not a
question.

Nothing in your integration selects how a call is decided. `source` and
`legal_basis` are mandatory fields of the authorize response, copied into the
ACT token, and both SDKs treat them as opaque strings — they never branch on the
value. How an application decides is a property of the application in the ID
Side account the key belongs to. Your code is identical either way; read the
fields back off the certificate if you want to know what applied. Their lists of
values only grow, and a new value reaches your code with no SDK release —
`public_task`, `legal_obligation` and `vital_interests` arrived this way — so do
not close your own handling on the values above.

### Asking instead: the Preference Center

An application can also ask the people it serves rather than decide for them.
The [Preference Center](./widget.md) asks each person, in a surface ID Side
serves and you cannot read. They answer; the answer lands on ID Side's side of
the tunnel; from then on every AI call you make for that person — named with the
UBT the widget issues, or with your own `user_id` — is resolved against it. The
certificate carries `source: "widget"` (or `gpc`, `idside_account`) and
`legal_basis: "consent"`, because a person made a positive act.

That way of deciding needs the widget. **Without it, nobody ever writes a
preference** — the SDK cannot, by design (§3) — and one rule takes over: **what
nobody accepted is refused.** Every option in the catalogue, on every provider,
tool use included, resolves to a refusal for a person who has not answered, and
the certificate says why: `source: "app_default"`, `legal_basis: "not_established"`.
An option your users were never asked about is refused rather than unenforced;
add a provider next year that brings a new option with it, and everyone who
answered before it existed refuses it from day one, with nothing for you to
migrate. A refusal of `agentic_tools` is enforced by declining the call rather
than by rewriting a parameter, so an agentic integration on an application that
asks, with no widget running, raises `ToolUseRefusedError` on every request:
the mechanism asking you to put the question to your users.

An organization's policy is outside that rule: it decides, and an option the
policy does not mention is not refused on its behalf. On an application that
asks, a policy can still refuse options for everyone; it changes what applies
and stamps nothing — `source` and `legal_basis` stay what the person's own
answer, or their silence, made them.

Whichever way an application decides, every token says **who refused** each
option — the person, their silence, the organization, a team, or the
application — in its
[`refused_by`](./spec/act-token.md#refused_by--who-refused) claim. A refusal
your organization or your application adds is written under its own name,
never under the person's.

An application can also refuse options on its own, for everyone who uses it —
from its page on the ID Side dashboard, never from your code. It is a refusal
and nothing else: it cannot accept anything on anybody's behalf, it does not
change `source` or `legal_basis`, and the certificate writes it under `app`. On an
application that asks, describe it as what it is — "this application refuses X,
whatever the user answers". It decides nothing in the user's place: what the
application leaves open is still theirs to answer, the widget states the
refusal instead of asking a question whose answer could not count, and the
certificate still says what they answered — or that they never did.

### Blocking a provider — when the policy cannot apply

<a id="enforce-and-observe--when-the-policy-cannot-apply"></a>

A policy is not always something a call can carry out: a provider may have no
setting for an option the organization refuses, and while the organization has
declared no basis there is none to sign. **When your policy cannot apply to a
call (a provider without that setting, no basis declared): if the strainer
blocks that provider, the call is held; otherwise it goes through, with the
deviation signed in its certificate.** Each strainer has one switch per
provider, "Block the provider when it is not compliant":

- **Block**: `/v1/authorize` answers 403 and the SDK raises
  [`ConsentDeniedError`](./errors.md#policy_hold--a-call-the-policy-held) with
  `reason == "policy_hold"`, before the provider is called. A new strainer
  blocks every provider until the organization lets one through; one that
  existed before the switches kept, on each provider, the behaviour it had.
- **Let through**: the call goes out, and its certificate carries a
  [`deviations`](./spec/act-token.md#deviations--what-the-policy-could-not-apply)
  claim naming what did not apply; the organization sees the call in
  Monitoring.

The switch of the call's provider decides for every deviation of that call —
the ones that come from the provider and the ones that do not, such as a basis
nobody declared. The organization sets the switches, strainer by strainer, on
the ID Side dashboard — nothing in your code sets one, and your code is the
same either way. A developer can never bypass a wrapped client: a deviation
never comes from a setting your code turned off. It comes from what the call
names — the provider, the endpoint, the model — set against what the
organization has set up. What the call names is your code's choice: a provider
that applies what the policy refuses, and an endpoint and a model its
descriptor covers. The rest is the organization's: the same call is still held
or signed with its deviation while it has declared no basis, while a refusal
rests on an account setting that no current declaration covers, or while its
policy refuses an option nothing at that provider can apply. A refusal the
provider answers only in its own terms is applied when those terms rule the
processing out, and is a deviation when they leave it possible. A refusal that
only a dedicated credential can carry (see
[credential routing](./providers.md#6-credential-routing)) is a deviation while
you have labelled no credential for it: a provider let through gets the call on
your ordinary credential, with the deviation signed; a blocked one gets
nothing. Whatever the policy can apply to a call that goes out is applied, tool
refusals included, which the SDK enforces itself whichever way the switch is
set, since only it sees whether a call carries tools. An application that asks
its users has no such switch: its calls are never held or flagged.

### The mistake worth naming

**Declaring a basis you do not hold.** Not because a rule forbids it — because
the mechanism records exactly what you declared, and that is all it records.

The basis in every certificate is the one your organization declared on the
dashboard, signed with `source: "default_policy"`. ID Side does not check it
against anything: it is your statement, and the certificate carries it as yours, on
every call, in a form nobody can edit afterwards. Declare `consent` for people
nobody asked, and the file shows a year of calls signed `consent` beside a
source that says nobody was asked. Declare nothing, and every certificate reads
`not_established`, each one a deviation — signed in its certificate, or held
wherever the strainer blocks the provider.

Now picture that file in front of the authority it was built for. The certificate did
not decide anything for you; it wrote down what you decided, and when. That is
the shape of the product: it is **deterrent and evidentiary, never policing**.
Nothing stops you from configuring it carelessly. The system simply records what
happened, and a statement recorded faithfully can only be read as what it was.
Declare the basis you would defend, and refuse what you would not.

## 5. The three layers

ID Side's architecture has three layers. Only the last one has a public
specification today.

1. **Preference collection.** The organization sets its policy on the ID Side
   dashboard. On an application that asks, the user expresses choices directly
   to ID Side — through the [Preference Center widget](./widget.md) embedded in
   your frontend, through their own ID Side account, or implicitly through a
   browser signal such as GPC. Your DOM and your backend are not in this path:
   the widget is a cross-origin iframe ID Side serves, not a library running in
   your page.
2. **Authorization.** For each AI call, the ID Side cloud resolves the policy or
   consent state that applies, decides which provider parameters must be forced,
   mints a token and appends a ledger entry. This is the proprietary part; the
   SDK sees it as one HTTP endpoint, `POST /v1/authorize`.
3. **Certificate.** The **ACT token** (AI Consent Token) — a signed JWT bound to one
   specific request — plus its immutable ledger entry. Specified openly in
   [`spec/act-token.md`](./spec/act-token.md).

> The underlying patent, U.S. Patent No. 12,413,397 B2, names these layers
> PAS / AAM / ACT. It is one of the patents and applications the SDK may be
> covered by, with European Patent No. 4,100,905 B1, with unitary effect, and
> any national patent rights deriving from that European patent, and Chinese
> Patent Application No. CN115151937A, which is pending. All are listed at the
> address the software is marked with — Patents: https://sdk.idside.eu/patents.
>
> Only ACT has a published specification; this documentation describes the other
> two by what they do rather than by their patent names.

## 6. The ACT token

An ACT token is an `ES256`-signed JWT of at most 3,072 characters (the
`deviations` claim of a call let through is what can take it past 2 KB), with a
one-hour lifetime, attached to the provider call as the `X-AI-Consent-Token`
header. It records —
in a form nobody can retroactively alter — the pseudonymized subject (the person
the call named, or the API key it was made with), the app, the provider and
model, the resolved `consent_state`, the `legal_basis`, the `source` of the
decision, and the `request_hash`.

Two properties matter more than the rest:

**One token = one request.** The token carries a `request_hash`: the SHA-256 of
the [RFC 8785 (JCS)](https://www.rfc-editor.org/rfc/rfc8785) canonical form of
the caller payload at the authorization boundary, with identity and request
headers removed and before forced parameters or constraints are applied, plus a
nonce and a maximum one-hour TTL. A token detached from
its request and replayed on another is invalid, however well it is signed. This
is why canonicalization is not a detail: the SDK, the cloud and an independent
auditor must compute the same bytes from the same request. Cross-language test
vectors pin that agreement — see
[`packages/descriptors/testdata/`](../packages/descriptors/testdata/request-hash-vectors.json).

**The SDK cannot forge one.** Signing uses ECDSA P-256 private keys that exist
only in the ID Side cloud (a KMS in production). This repository contains no
private key and no signing code; it carries tokens and can verify them against
the public JWKS. That asymmetry is what makes a token produced on your
infrastructure worth anything to a third party.

What it *can* do is check one. Both SDKs verify a token offline — signature
against the public JWKS, `ES256` pinned, expiry, and the request binding — with
no call to ID Side: `verify_act()` in Python,
[`verifyAct()`](./quickstart-node.md#7-verify-a-certificate-yourself) in Node.js. A
certificate only its issuer can validate would prove nothing. And a certificate whose
verdict depended on which SDK you installed would prove nothing either, which is why
[shared vectors](../packages/descriptors/testdata/act-verification-vectors.json)
pin the outcome — down to the error raised — for both implementations.

The `binding` claim records *what* the subject is and how it was tied to the
call — see §8.

## 7. The ledger

Every authorization appends an entry to an append-only, hash-chained ledger held
by the ID Side cloud: each entry commits to the hash of the previous one, so
removing or editing a past entry breaks every entry after it. That is what makes
"this call was authorized on 14 July at 18:26 against this consent state" a checkable claim rather than a log line
anyone with database access could have written yesterday.

The ledger is cloud-side and proprietary. The SDK never writes to it; it is a
consequence of authorization, not a separate call — which also means there is no
way to make an AI call through the SDK and *not* leave a trace.

## 8. Naming the end user: `user_id` vs UBT

Naming the end user is **optional**. A call that names nobody is authorized with
the API key as the certificate's subject: the token's `sub` is then a pseudonymous
subject of that key, scoped per app exactly as a person's is, and its `binding`
reads `api_key`. Most calls name nobody, and the organization's policy applies
to them all the same.

Name the person a call is for — or a team, such as `team-marketing` — when you
want the certificate to say whom the call was for. Pass at most one of two mutually
exclusive fields:

| | nothing | `user_id="usr_marie_456"` | `ubt="eyJ…"` |
| --- | --- | --- | --- |
| The certificate is about | whoever holds the API key | the person or team you name | the person the widget saw |
| Origin | — | you assert it | the ID Side widget issued it |
| SDK behavior | no identity field is sent | sent as-is | forwarded opaquely, never decoded |
| Recorded as | `binding: "api_key"` | `binding: "user_id"` | `binding: "ubt"` |

Tell the three apart by `binding`, never by the shape of `sub`: each is a
pseudonym, and none of them is the raw value.

The `user_id` form has an honest gap: nothing ties the identifier you pass to
the person it names. On an application that asks its users, a developer could
collect Marie's opt-out and call the SDK with Paul's id. The UBT closes that gap
— the widget saw the user, ID Side signed the binding, and the cloud verifies the
signature. The certificate records which form you used, so an auditor can weigh it
accordingly.

Passing both raises `ConflictingIdentityError`. Passing one that names nobody —
an empty string, a string of blanks, `0`, `false`, an empty list or object —
raises `MissingIdentityError`: such a value is almost always a bug upstream (a
form field left empty, a header that was not set), and reading it as "nobody"
would quietly turn a call you meant to name into one about the key. To name
nobody, leave the field out, or pass `None` (Python), `undefined` or `null`
(JavaScript). In both error cases nothing is sent anywhere — no cloud query, no
AI call. See [`errors.md`](./errors.md).

### What to send as `user_id`

ID Side never stores it. It is hashed into a per-app pseudonym on arrival, with
a one-way function, and the pseudonym is what reaches the database, the ledger
and the token's `sub` — which is why `verify_act()` has no `user_id=` parameter
at all. That is a good property, and it has one consequence you have to plan
for: **ID Side cannot recognise a person you name differently, and cannot help
you name them consistently.** That part is yours, and only yours.

- **Under an organization's policy** ([§4](#4-the-policy-what-applies-to-a-call-and-who-decided))
  — any stable identifier you choose for the person or the team: your own
  primary key, an employee number, `team-marketing`. The policy and the declared
  basis apply whatever it is; the identifier only says, in the certificate, whom the
  call was for.
- **Signed in to your product** — send your own stable identifier: the primary
  key, the account id, whatever survives a password reset. Nothing else needed.
- **Not signed in**, on an application that asks — a marketing site, a public
  demo, a checkout before account creation — mint a random value once and keep
  it in *your* storage, under *your* domain
  ([the snippet](./widget.md#4-what-to-send-as-userid)). ID Side sets no cookie
  and never sees anything it could correlate across sites.
- **Never a browser fingerprint.** It is the tempting way to get a stable
  identifier while storing nothing, and it identifies a person across sites they
  never linked — the tracking this product exists to give people a lever
  against. A consent tool built on covert identification argues against itself,
  and the ledger would be the record of it.

On an application that asks, the identifier is also the join between the two
circuits: the answer given in the browser and the AI call made by your backend
belong to the same person only if you send the same string in both places. An
identifier that changes there — cleared storage, another browser, a new device —
makes the person a stranger again. That is a defined state, not a failure:
everything is refused for them, the certificate records `source: "app_default"` and
`legal_basis: "not_established"`, and the widget asks again. Nobody is ever
mistaken for having agreed to something. It fails in the safe direction, which
is what makes this a plumbing problem rather than a compliance one.

## 9. Fail-closed

If consent cannot be verified, **the AI call does not happen**. Cloud
unreachable, timeout, HTTP 5xx, an undetermined 4xx, a 200 whose body the SDK
cannot fully parse — all of these raise `ConsentUnreachableError` and stop there.
The SDK never guesses a decision, and never falls back to "call the provider
anyway".

This is a real trade-off and it is chosen deliberately: the alternative
(fail-open) would mean that the moments when your record has holes are
exactly the moments you cannot detect. An outage you can see beats a silent gap
in an audit trail. Practical consequences and how to handle them are in the
[FAQ](./faq.md#what-happens-if-id-side-is-down).

## 10. GPC

[Global Privacy Control](https://globalprivacycontrol.org/) is a browser-level
signal (`Sec-GPC: 1`) by which a user expresses a refusal once, for every site.
In the ID Side model it is just another *source* of preference: when it is
detected in Circuit A, the resolved consent state carries `source: "gpc"`, at the
same trust level as an explicit widget interaction — the user's own browser said
it, and the developer was not in the loop.

> Where it happens: GPC detection belongs to the
> [Preference Center widget](./widget.md), which reads both the `Sec-GPC: 1`
> request header and `navigator.globalPrivacyControl`. It is not in this
> repository, because the widget is not (§4 of
> [`ARCHITECTURE.md`](./ARCHITECTURE.md#4-source-available--proprietary-boundary)). The
> `source` value is part of the
> [token spec](./spec/act-token.md#4-source-trust-levels).

## 11. Where the enforcement actually happens

Two things are injected into your provider call, and they work on completely
different time horizons:

| | Type 1 — forced parameters | Type 2 — the ACT token |
| --- | --- | --- |
| What | existing provider knobs (e.g. OpenAI `store: false`, no-training headers) | the `X-AI-Consent-Token` header |
| Who reads it | the provider, today | nobody at the provider, today |
| Effect | real enforcement now | audit certificate now |
| Later | — | if providers adopt it, Type 2 absorbs Type 1 |

The SDK holds **no** table mapping consent states to provider parameters. It
receives `params_forced` from `/v1/authorize` and applies it. When a provider
renames a privacy flag, ID Side updates a row and nobody upgrades an SDK. What
*is* in this repository is the mechanical half — which methods to intercept, and
whether a given client takes parameters as arguments or as headers — expressed
as [JSON descriptors](./providers.md).

## Next

- [Python quickstart](./quickstart-python.md) · [Node.js quickstart](./quickstart-node.md)
- [Providers and forced parameters](./providers.md)
- [Error catalogue](./errors.md) · [FAQ](./faq.md)
- [Architecture](./ARCHITECTURE.md) · [ACT token specification](./spec/act-token.md)
