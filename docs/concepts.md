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
| Sensitive material | the card number | the user's consent preferences |
| Who must never hold it | the merchant | the developer |
| What the app handles instead | a token | a `user_id` or a UBT |
| What comes back | a charge receipt | a signed **ACT token** + a ledger entry |

The consequence is the same in both cases: a whole class of mistakes becomes
structurally impossible rather than merely forbidden.

## 3. The opaque tunnel

This is the central invariant, and the one that surprises people.

**The SDK has no function that sets, overrides or even reads a user's
preferences.** There is no `set_preferences()`, no `override_consent()`, no
`get_preferences()` returning something you could edit and send back. You pass
an identity; the SDK returns an enforced call. Preferences travel from the user
straight to ID Side, on a path your backend is never on.

```
Circuit A — collection                 Circuit B — enforcement
──────────────────────                 ───────────────────────
user's browser                         your backend
      │                                      │
      │ preferences                          │ 1. identity + provider
      │ (widget · GPC · account)             │    + model + request hash
      ▼                                      ▼
  ┌─────────────────────────────────────────────────────┐
  │                   idside.eu                     │
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

Suppose the SDK let you pass the preferences you had collected. Then the proof
it produces would attest to *what you told it*, not to *what the user chose*. A
developer could show an opt-out dialog, record the opt-out in their own database,
and still call the SDK with `training=true`. The signed token would faithfully
certify a lie, and every ACT token in the world would be worth exactly nothing —
because no auditor could distinguish an honest one from that.

Removing the setter is what makes the signature meaningful. The API is
deliberately narrower than you might want, and that narrowness is the product.

### What the tunnel does *not* claim

It does not make cheating impossible. Nothing stops a developer from calling
`openai.chat.completions.create()` directly and skipping ID Side entirely — the
SDK runs in their process, on their machine.

The model is **deterrent and evidentiary**, not coercive. A developer who
bypasses the SDK has no ACT token and no ledger entry for that call, and
therefore nothing to show for that call. The model does not make violations
impossible; it makes them *visible as gaps in the proof chain*. This is the
same reason double-entry bookkeeping works without preventing anyone from lying.

## 4. Two modes: asked and imposed

One mechanism, two ways to use it. The SDK is byte-for-byte the same in both —
same `wrap()`, same authorize call, same signed proof. What changes is **who
decided**, and the proof says which:

| | **Asked** | **Imposed** |
| --- | --- | --- |
| Who decides | the person, for themselves | the organization, for its employees |
| How they are reached | the Preference Center — a popup in your page | the same widget, as a read-only transparency panel: told, not asked |
| `source` in the proof | `widget`, `gpc`, `idside_account` | `default_policy` |
| `legal_basis` in the proof | `consent` | `legitimate_interest`, `contractual_obligation`, `public_task`, `legal_obligation`, `vital_interests`, or `not_established` |
| Typical case | a product with end users | a workforce told to use AI at work |

There is a third state, and it is neither mode: a person nobody has asked and no
policy covers. **Everything is refused for them** — every option in the
catalogue, on every provider, tool use included — and the proof says why:
`source: "app_default"`, `legal_basis: "not_established"`. That is where every
call lands before a preference has ever been written, so it is the state you
start in, and it is the strictest one the system has.

The rule behind it is worth stating on its own, because it also governs people
who *have* answered: **what nobody accepted is refused.** On an application that
asks its visitors, a consent state is completed from the catalogue before
anything is enforced or signed, so an option your users were never asked about
is refused rather than unenforced. Add a provider next year that brings a new
option with it, and everyone who answered before it existed refuses it from day
one, with nothing for you to migrate. The imposed mode is outside this rule: an
organization's policy decides for its employees, and an option the policy does not
mention is not refused on its behalf.

Nothing in your integration selects a mode. `source` and `legal_basis` are
mandatory fields of the authorize response, copied into the ACT token, and both
SDKs treat them as opaque strings — they never branch on the value. The mode is
a property of the ID Side account the key belongs to. Your code is identical
either way; read the two fields back off the proof if you want to know which
applied. Their lists of values only grow, and a new value reaches your code
with no SDK release — `public_task`, `legal_obligation` and `vital_interests`
arrived this way — so do not close your own handling on the values above.

### Asked — you run the widget

Someone visits your site or opens your app. The
[Preference Center](./widget.md) asks them, in a surface ID Side serves and you
cannot read. They answer; the answer lands on ID Side's side of the tunnel; from
then on every AI call you make for that person is resolved against it. The proof
carries `legal_basis: "consent"`, because a person made a positive act.

This is the mode that needs the widget. **Without it, nobody ever writes a
preference** — the SDK cannot, by design (§3) — so every call resolves to a total
refusal and every proof reads `not_established`. That state is honest, and it is
safe, but it is not a product: you are paying a provider for capabilities your
users are all refusing, and your evidence says nobody was ever asked.

It also has teeth. A refusal of `agentic_tools` is the one the SDK enforces by
declining the call rather than by rewriting a parameter, so an agentic
integration without the widget raises `ToolUseRefusedError` on every request.
That is not an outage and not a misconfiguration — it is the mechanism asking you
to put the question to your users.

### Imposed — the organization decides for its employees

An employer deploying AI to its staff does not ask each employee whether the
organization may use a model. It decides, and it owes the people concerned
**transparency**: what was decided, by whom, on what basis, and what the
employer can see. A statement rather than a question — and the
[widget](./widget.md#11-the-imposed-mode) is how the statement is delivered,
read-only, with no switch anywhere in it. Consent is rarely a valid basis here:
an employee can seldom refuse their employer freely. An organization may declare
it only where genuine freedom of choice can be demonstrated; ID Side collects no
answer from the employee and does not assess the declaration.

Two properties keep this from becoming a preference setter in disguise:

- **A policy can only tighten.** It turns an option off, never on. A person's
  own refusal survives a policy that permits. On an application that asks, a
  policy changes what applies and stamps nothing: `source` and `legal_basis`
  stay what the person's own answer — or their silence — made them. On an
  application that imposes, every call is signed `default_policy`, whether the
  policy refuses ten options or none — the decision came from the
  organization, and nobody was asked.
- **A policy signs only what the organization declared**:
  `legitimate_interest`, `contractual_obligation`, `public_task`,
  `legal_obligation`, `vital_interests` or `consent`, for an employee who is
  identified — and `not_established` where it declared none or the employee is
  not identified. A declared `consent` keeps its `source`: the token still shows
  that the decision came from the organization, and that nobody was asked.

Whichever mode applies, every token says **who refused** each option — the
person, their silence, the organization, a team, or the application — in its
[`refused_by`](./spec/act-token.md#refused_by--who-refused) claim. A refusal
your organization or your application adds is written under its own name,
never under the person's.

An application can also refuse options on its own, for everyone who uses it —
from its page on the ID Side dashboard, never from your code. It is a refusal
and nothing else: it cannot accept anything on anybody's behalf, it does not
change `source` or `legal_basis`, and the proof writes it under `app`. On an
application that asks, describe it as what it is — "this application refuses X,
whatever the user answers". It decides nothing in the user's place: what the
application leaves open is still theirs to answer, the widget states the
refusal instead of asking a question whose answer could not count, and the
proof still says what they answered — or that they never did.

### Enforce and Observe — when the policy cannot apply

A policy is not always something a call can carry out: a provider may have no
setting for an option the organization refuses, the organization may not have allowed the
provider at all, and an employee who is not identified leaves no basis to sign.
**When your policy cannot apply to a call (a provider with no such setting, a
provider your organization has not allowed, an employee who is not identified),
Observe lets the call through and flags it, Enforce holds it.**

- **Observe**, the default: the call goes out, and its token carries a
  [`deviations`](./spec/act-token.md#deviations--what-the-policy-could-not-apply)
  claim naming what did not apply.
- **Enforce**: `/v1/authorize` answers 403 and the SDK raises
  [`ConsentDeniedError`](./errors.md#policy_hold--a-call-the-policy-held) with
  `reason == "policy_hold"`, before the provider is called.

The organization chooses, application by application, on the ID Side
dashboard — nothing in your code selects either, and your code is the same
under both. A developer can never bypass a wrapped client: a deviation never
comes from a setting your code turned off. It comes from what the call names —
the provider, the endpoint, the model or the identity — set against what the
organization has set up. What the call names is your code's choice: a provider
the organization allows and that applies what the policy refuses, an endpoint
and a model its descriptor covers, and the identifier the organization
registered for the employee. The rest is the organization's: the same call is
still held or flagged while it has declared no basis, while a refusal rests on
an account setting that no current declaration covers, or while its policy
refuses an option nothing at that provider can apply. A refusal the provider
answers only in its own terms is applied when those terms rule the processing
out, and is a deviation when they leave it possible. A refusal that only a
dedicated credential can carry (see
[credential routing](./providers.md#6-credential-routing)) is a deviation while
you have labelled no credential for it: Observe sends the call on your ordinary
credential and flags it, Enforce holds it. Whatever the policy can apply to a
call that goes out is applied, tool refusals included, which the SDK enforces
itself under either setting since only it sees whether a call carries tools. An
application that asks its visitors has neither setting: its calls are never
held or flagged.

### The mistake worth naming

**You cannot impose preferences on your visitors.** Not because a rule forbids
it — because the mechanism will not produce what you were hoping for.

Say you sell a consumer app, you would rather not show a popup, and you set a
policy instead. Here is what you actually get. The basis you declared is signed
only for a person whose tie to your organization was **proven** — a mail
domain you verified, or a link we sent to their work address that they
opened — and your visitors are nobody's employees: every one of their calls
reads `not_established`. Keep them as visitors and skip the widget instead, and
the proofs say the same word: nobody was asked.

Now picture that file in front of the authority it was built for. A year of
calls, each one signed, each one saying *no basis was established* — about
people you chose not to ask. The proof did not protect you; it wrote the
finding. And it wrote it in a form nobody can edit afterwards, which was the
whole point of signing it.

That is the shape of the product: it is **deterrent and evidentiary, never
policing**. Nothing stops you from configuring it this way. The system simply
records what happened, and a lie recorded faithfully stops being readable as
anything but a lie. If you have end users, ask them — it costs one
[`<script>` tag](./widget.md).

## 5. The three layers

ID Side's architecture has three layers. Only the last one has a public
specification today.

1. **Preference collection.** The user expresses choices directly to ID Side —
   through the [Preference Center widget](./widget.md) embedded in your frontend,
   through their own ID Side account, or implicitly through a browser signal such
   as GPC. Your DOM and your backend are not in this path: the widget is a
   cross-origin iframe ID Side serves, not a library running in your page.
2. **Authorization.** For each AI call, the ID Side cloud resolves the
   applicable consent state, decides which provider parameters must be forced,
   mints a token and appends a ledger entry. This is the proprietary part; the
   SDK sees it as one HTTP endpoint, `POST /v1/authorize`.
3. **Proof.** The **ACT token** (AI Consent Token) — a signed JWT bound to one
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
`deviations` claim of a flagged call is what can take it past 2 KB), with a
one-hour lifetime, attached to the provider call as the `X-AI-Consent-Token`
header. It records —
in a form nobody can retroactively alter — the pseudonymized user, the app, the
provider and model, the resolved `consent_state`, the `legal_basis`, the
`source` of the preference, and the `request_hash`.

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
[`verifyAct()`](./quickstart-node.md#7-verify-a-proof-yourself) in Node.js. A
proof only its issuer can validate would not be a proof. And a proof whose
verdict depended on which SDK you installed would not be one either, which is why
[shared vectors](../packages/descriptors/testdata/act-verification-vectors.json)
pin the outcome — down to the error raised — for both implementations.

The `binding` claim records *how* the user was identified — see §8.

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

Every wrapped AI call must name exactly one end user, with exactly one of two
mutually exclusive fields:

| | `user_id="usr_marie_456"` | `ubt="eyJ…"` |
| --- | --- | --- |
| Origin | you assert it | the ID Side widget issued it |
| What it is | your identifier for the user | a JWT signed by ID Side, binding user, app, session and consent state |
| SDK behavior | sent as-is | forwarded opaquely, never decoded |
| Recorded as | `binding: "user_id"` | `binding: "ubt"` |
| Evidentiary weight | weaker | stronger |

The `user_id` form has an honest gap: nothing ties the identifier you pass to
the person who actually consented. A developer could collect Marie's opt-out and
call the SDK with Paul's id. The UBT closes that gap — the widget saw the user,
ID Side signed the binding, and the cloud verifies the signature. The proof
records which of the two you used, so an auditor can weigh it accordingly.

Passing both raises `ConflictingIdentityError`; passing neither raises
`MissingIdentityError`. In both cases nothing is sent anywhere — no cloud query,
no AI call. See [`errors.md`](./errors.md).

### What to send as `user_id`

The identifier is the join between the two circuits. The answer given in the
browser and the AI call made by your backend belong to the same person only if
you send the same string in both places; nothing else links them.

ID Side never stores it. It is hashed into a per-app pseudonym on arrival, with
a one-way function, and the pseudonym is what reaches the database, the ledger
and the token's `sub` — which is why `verify_act()` has no `user_id=` parameter
at all. That is a good property, and it has one consequence you have to plan
for: **ID Side cannot recognise a person you name differently, and cannot help
you name them consistently.** That part is yours, and only yours.

- **Signed in to your product** — send your own stable identifier: the primary
  key, the account id, whatever survives a password reset. Nothing else needed.
- **Not signed in** — a marketing site, a public demo, a checkout before
  account creation — mint a random value once and keep it in *your* storage,
  under *your* domain
  ([the snippet](./widget.md#4-what-to-send-as-userid)). ID Side sets no cookie
  and never sees anything it could correlate across sites.
- **Never a browser fingerprint.** It is the tempting way to get a stable
  identifier while storing nothing, and it identifies a person across sites they
  never linked — the tracking this product exists to give people a lever
  against. A consent tool built on covert identification argues against itself,
  and the ledger would be the record of it.
- **In the imposed mode** ([§4](#4-two-modes-asked-and-imposed)) — the person's
  work email address, or an identifier registered with ID Side. Anything else is
  a subject the system has never met: the policy still applies, because it always
  does, but no legal basis is signed for that call. The pseudonymisation runs one
  way, so nobody at ID Side can reconstruct the mapping on your behalf — which is
  the same property that protects your users.

An identifier that changes — cleared storage, another browser, a new device —
makes the person a stranger again. That is a defined state, not a failure:
everything is refused for them, the proof records `source: "app_default"` and
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
(fail-open) would mean that the moments when your proof chain has holes are
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
| Effect | real enforcement now | audit proof now |
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
