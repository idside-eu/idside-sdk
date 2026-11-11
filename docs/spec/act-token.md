# AI Consent Token (ACT) — Public specification (draft)

> Status: **draft**. This specification is published openly and aims to become an
> interoperable standard (submission envisaged as an IETF Internet-Draft or a W3C
> Community Group proposal). ID Side is its reference implementation.
>
> Patents: https://sdk.idside.eu/patents
>
> The format and its reference implementation may be covered by U.S. Patent
> No. 12,413,397 B2; European Patent No. 4,100,905 B1, with unitary effect, and
> any national patent rights deriving from that European patent; and Chinese
> Patent Application No. CN115151937A, which is pending. **Beyond the limited
> evaluation patent licence of clause 2 of `LICENSE`, rights under the patents
> are conveyed only by an executed commercial licence** — see `NOTICE`.
> Publishing the format so that anyone can verify a token is a different act
> from licensing the invention that produces one, and the two are deliberately
> kept apart: a proof nobody outside can check is not a proof.

## 1. Purpose

The ACT token is **cryptographic proof**, attached to every AI API call,
attesting that the user's consent preferences were verified **before** sending.
It is designed to be independently verifiable (audit, data protection authority,
DSR response).

## 2. Format

- **Type**: signed JWT.
- **Algorithm**: `ES256` (ECDSA on the P-256 curve).
- **Size**: at most 3,072 characters. The [`deviations`](#deviations--what-the-policy-could-not-apply)
  claim is why it is no longer under 2 KB: a call flagged on every deviation it
  can carry is the longest token there is.
- **TTL**: 1 hour.
- **Transport**: HTTP header `X-AI-Consent-Token`.

> **Signing** (the private ECDSA key) is performed exclusively by the ID Side
> cloud infrastructure. This source-available SDK only **verifies** tokens — format,
> claims and signature — using **public** keys, and can never issue one.

## 3. Claims

JOSE header: `{ "alg": "ES256", "typ": "act+jwt", "kid": "act-2026q3" }`.

| Claim | Type | Description |
| ----- | ---- | ----------- |
| `iss` | string | Issuer — `idside.eu` (`test.idside.eu` for non-production keys) |
| `sub` | string | `user_pseudo_id` — pseudonymized, scoped per app. Never a raw identifier |
| `app_id` | string | The application the call was authorized for |
| `iat` | number | Issued-at (epoch) |
| `exp` | number | Expiry; the validity interval `exp - iat` is at most 3600 seconds |
| `jti` | string | Unique token id. Also the public `token_id` used by §5 |
| `consent_state` | object | The consent state that applied to this call (e.g. `{ "training.foundation": false, "agentic_tools": false, … }`). Outside the imposed mode it is complete: an option the person never accepted reads `false`, so a value records what applied, not necessarily what they said — `source` and `legal_basis` say whether anybody decided |
| `legal_basis` | string | `consent`, `legitimate_interest`, `contractual_obligation`, `public_task`, `legal_obligation`, `vital_interests`, `not_established` — see below. Read it as opaque: the list only grows |
| `source` | string | `widget`, `widget_override_gpc`, `gpc`, `idside_account`, `default_policy`, `app_default` — see §4 |
| `provider` | string | Target provider (`openai`, `anthropic`…) |
| `model` | string | Target model |
| `request_hash` | string | `sha256:<hex>` over the canonical request (§3.1) |
| `nonce` | string | Unique non-reusable value (anti-replay) |
| `binding` | string | How the identity was established: `ubt` (the widget bound it) or `user_id` (the developer asserted it). Makes the **strength of the proof** visible to an auditor |
| `mode` | string | `test` or `live` — see below |
| `refused_by` | object | Which layer refused each option this call refuses — see below |
| `deviations` | array | What the organization's policy could not apply to this call, on an application that lets such calls through — see below. Absent when the call is compliant |

### `mode: "test" | "live"`

The mode of the **API key** that minted the proof, not of the application. That
distinction is the whole point: an application moving to production must not
retroactively relabel the proofs its test key collected during a rehearsal, and
a key does not change mode.

It is a signed claim rather than a question for the issuer because offline
verification — a JWKS and any JWT library — is a supported path, and a proof
whose seriousness can only be established by calling the issuer is not
self-contained. A verifier deciding whether a proof counts should read `mode`
before anything else.

### `refused_by` — who refused

Added 2026-10-08. An object with one entry for **every** option of the
catalogue whose value in `consent_state` is `false`, and for no other:

```json
"refused_by": {
  "agentic_tools": ["person", "organization"],
  "memory": ["silence"],
  "retention.files": ["silence", "app"]
}
```

Each entry lists the layers that refused the option — at least one, always in
this order:

| Value | Meaning |
| --- | --- |
| `person` | the person refused it: in the widget, on their ID Side account, or through a browser signal (`source: "gpc"`, in the same token, tells that one apart) |
| `silence` | nobody answered, and what nobody accepted is refused |
| `organization` | the organization's policy refuses it for everyone |
| `team` | a team the person belongs to, or one above it, refuses it (imposed mode only) |
| `app` | the application refuses it for everyone who uses it |

`person` and `silence` never share an entry, and appear only when the
application asks; `team` appears only when it imposes. The object is `{}` when
nothing is refused, and it is present on every token issued since it was
introduced — its absence means an older token, never "nothing was refused".

`consent_state` also carries the catalogue's historical aliases — `training`,
the original single boolean, for one — and an alias is not an option, so it
never has an entry here. A check that every `false` in `consent_state` has an
entry in `refused_by` must exclude the aliases first, or it reports a violation
on a valid token: compare only the keys the catalogue lists under `options`.
The aliases are the keys of its `legacy_mapping`, in
[`packages/descriptors/consent-options.json`](../../packages/descriptors/consent-options.json)
— `catalogue()["legacy_mapping"]` in the Python SDK.

It names layers, never a person, a team or an application: the token travels to
the provider in a header, and no free text goes with it. The claim name and its
values are signed, so they are never renamed or redefined
([`versioning.md`](../versioning.md) §1.2); a layer added later is a new value,
so read them as opaque. Both SDKs keep this claim in `claims` without examining
it, like any claim they do not name.

### `deviations` — what the policy could not apply

Added 2026-10-11, and only ever on a token issued where an organization imposes
its policy on its employees. When your policy cannot apply to a call (a provider
with no such setting, a provider your organization has not allowed, an employee
who is not identified), Observe lets the call through and flags it, Enforce
holds it. This claim is the flag: an array with one entry per deviation, on a
token for a call that went out under Observe.

```json
"deviations": [
  { "kind": "provider_not_allowed" },
  { "kind": "option_not_enforced", "option": "training.foundation" }
]
```

| `kind` | Meaning |
| --- | --- |
| `provider_not_allowed` | the organization allows a list of providers, and this call's provider is not on it |
| `option_not_enforced` | the policy refuses `option`, and nothing applies that refusal to this call: the provider has no parameter for it, only its terms answer it and they leave the processing possible, it rests on an account setting no current declaration covers, only a credential the application has not labelled could carry it, or the endpoint or model is one the provider's descriptor does not cover |
| `basis_not_established` | the call is signed `legal_basis: "not_established"`: the organization declared no basis, or the employee is not identified |

`option` names an option of the catalogue and appears only on
`option_not_enforced`. Like `refused_by`, the claim names kinds and options,
never a person, a team or an application.

**The claim is absent when the call is compliant** — and on every token of an
application that asks its visitors, which has no such setting. On a token issued
before it was introduced, its absence says nothing either way. Under Enforce the
same call gets no token at all: `/v1/authorize` answers 403
[`policy_hold`](../errors.md#policy_hold--a-call-the-policy-held), and the
provider is never called.

The claim name and its values are signed, so they are never renamed or
redefined ([`versioning.md`](../versioning.md) §1.2); a kind added later is a
new value, so read them as opaque. Both SDKs keep this claim in `claims` without
examining it.

### `legal_basis` — what each value asserts

| Value | What the proof asserts |
| --- | --- |
| `consent` | Article 6(1)(a). With a `widget` or `idside_account` source, the person made a positive act: a widget answer, or their own ID Side account. With `source: "default_policy"`, the basis the organization's policy declared — ID Side collected no answer from the employee |
| `contractual_obligation` | the basis the organization's policy declared: Article 6(1)(b) |
| `legal_obligation` | the basis the organization's policy declared: Article 6(1)(c) |
| `vital_interests` | the basis the organization's policy declared: Article 6(1)(d) |
| `public_task` | the basis the organization's policy declared: Article 6(1)(e), a task carried out in the public interest |
| `legitimate_interest` | the basis the organization's policy declared: Article 6(1)(f) |
| `not_established` | no basis was established here — see below |

A basis a policy declares is only ever signed where an organization imposes
its policy on its employees, with `source: "default_policy"`, and only for an
employee who is identified; any other call under that policy reads
`not_established`. Since 2026-11-08 an organization may declare `consent` too.
The GDPR generally presumes consent not to be freely given at work, and ID Side
collects no answer from the employee: the token's `source` shows that the
decision was the organization's.

`public_task`, `legal_obligation` and `vital_interests` were added on
2026-10-13; no value was renamed. Neither SDK enumerates `legal_basis`: both
read it as an opaque, non-empty string, so a value added later reaches
`verify_act()` / `verifyAct()` and your code unchanged, with no SDK release. A
verifier of your own should read it the same way.

### `legal_basis: "not_established"`

Added 2026-07-26. It is deliberately **not** an Article 6 basis: it states that
none was established *here*, for this call.

It is stamped on the `app_default` branch — the one defined by the person
having done nothing, where every option resolves to a refusal. No widget answer, no linked
account, not even a GPC signal. Before this value existed, that branch was
stamped `consent`, which asserted in a signed proof that someone had agreed
when nobody had. Under GDPR, consent requires a positive act; there was none.

None of the other bases would have been honest either. `legitimate_interest`
and `contractual_obligation`, the only others then, are positive assertions, as
are the three added since; and the balancing test behind the first belongs to
the customer as controller — ID Side is a processor and cannot sign it on their
behalf. The customer may well hold a valid basis; we did not witness it, and a
proof should not claim to have.

A verifier that treats `legal_basis` as an opaque string is unaffected. One that
enumerates the three previous values will not recognise this one, which is the
correct outcome: it means the proof does not assert a basis, and reading it as
`consent` would be exactly the error being corrected.

### 3.1 `request_hash` — canonicalization

`request_hash = "sha256:" + hex(SHA-256(JCS(request)))`, where `JCS` is the JSON
Canonicalization Scheme ([RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)):
keys sorted by UTF-16 code unit, no insignificant whitespace, numbers rendered
the way ECMAScript renders them.

The hashed object is the caller's provider payload at the authorization
boundary, after removing the identity parameter (`user_id` / `ubt`) and the
per-request transport headers. The SDK computes it **before** applying the
cloud's `params_forced`, local constraint transformations, credential-routing
headers, or the ACT header itself. The provider payload can therefore differ
from the hashed object. This ordering is part of the existing proof format and
must not be changed when reconstructing an archived proof.

Canonicalization is not a detail: without it, the SDK, the cloud and an auditor
would each compute a different hash from the same request, and no proof could be
independently checked. Implementations must agree byte for byte — the reference
test vectors live in
[`packages/descriptors/testdata/request-hash-vectors.json`](../../packages/descriptors/testdata/request-hash-vectors.json).

### 3.2 Keys and rotation

Tokens are signed with ECDSA P-256 keys held only by the ID Side cloud (a KMS in
production). Public keys are published at
**`GET /.well-known/jwks.json`**, each carrying the `kid` that appears in the
token header. Keys are rotated periodically; a retired public key stays in the
JWKS until the last token signed with it has expired.

Verifiers **must** pin `alg` to `ES256` and reject any other value — a token
declaring `none` or an HMAC algorithm is a forgery attempt, not a negotiation.

## 4. Source trust levels

| `source` | Trust | Explanation |
| -------- | ----- | ----------- |
| `widget` | MAXIMUM | Opaque tunnel — preference expressed directly to ID Side |
| `widget_override_gpc` | MAXIMUM | An explicit widget answer taken over a GPC signal for this site — which the GPC specification itself provides for |
| `gpc` | MAXIMUM | Browser signal `Sec-GPC: 1` detected automatically |
| `idside_account` | MAXIMUM | User's ID Side account — full opaque tunnel |
| `default_policy` | STRONG | An organization's policy, applied to its employees without asking them. May carry `legal_basis: "consent"` since 2026-11-08, as a basis the organization declared: no answer was collected from the employee |
| `app_default` | NONE | Nobody said anything, so nothing is allowed: every option in the catalogue resolves to `false` for a person the system has never met — always paired with `legal_basis: "not_established"` |

The last two are the *imposed* and *unknown-subject* cases. A verifier weighing
a proof should read `source` and `legal_basis` together: `default_policy` says a
decision was made *about* the person, and `app_default` says none was made at
all.

## 5. Verification

**Locally**, with no help from ID Side: fetch the JWKS (§3.2), verify the
signature, check `exp`, and recompute `request_hash` from the request you have
(§3.1). **One token = one request** — a token whose `request_hash` does not match
the request it is attached to is invalid, however well it is signed.

Both SDKs implement this check in the open:

```python
from idside import verify_act  # pip install 'idside[verify]'

proof = verify_act(token, jwks=jwks, request_hash=request_hash)
```

```ts
import { verifyAct } from "@idside/sdk";

const proof = await verifyAct(token, { jwks, requestHash });
```

They pin `alg` and `typ` as required above, select the key strictly by `kid`,
enforce `exp > iat` with a maximum 3600-second validity interval, and reject
`test.idside.eu` tokens unless the caller opts in — so a development proof can
never pass as a production one.

Current proofs must carry `mode: "test" | "live"`, exposed as `proof.mode` in
both SDKs. A proof minted before the claim existed is accepted only through the
explicit compatibility option `allow_legacy_without_mode=True` in Python or
`allowLegacyWithoutMode: true` in Node; `proof.mode` is then `None` /
`undefined`. Keep that option confined to a known historical archive.

**Remotely**, `GET /v1/verify/{token_id}` — public, no API key, no account:

```json
{
  "status": "valid",              // valid | expired | revoked | unknown
  "mode": "live",                 // test | live | null (minted before the claim existed)
  "issued_at": "2026-07-14T18:26:59Z",
  "expires_at": "2026-07-14T19:26:59Z",
  "revoked_at": null,             // set only when the proof was withdrawn
  "revocation_reason": null,      // then `api_key_compromised` — see below
  "app_id": "app_medhelp",
  "provider": "openai",
  "model": "gpt-4o-mini",
  "source": "widget",
  "legal_basis": "consent",
  "binding": "ubt"
}
```

The response deliberately omits `sub` and `consent_state`. It answers *"is this
token genuine?"*, not *"what did this person consent to?"* — an endpoint that
disclosed a user's choices to anyone holding a token id would be a privacy hole
wearing the costume of transparency.

### `status: "revoked"` — and why you must read the reason

`revoked` never stands alone. Read `revocation_reason` with it, always.

There is exactly one reason in the vocabulary today, and a second one has to be
added deliberately:

| `revocation_reason` | Means |
| --- | --- |
| `api_key_compromised` | The customer reported that the key which minted this proof had leaked, and withdrew the proofs it produced. |

That value says **nothing** about whether the call was consented. A verifier
that branches on `status == "revoked"` alone cannot tell *"this proof was a
lie"* from *"the customer rotated a leaked key an hour later"* — and those mean
opposite things. Without the reason, an honest customer's own response to a
compromise reads exactly like an admission. Treat a missing or unrecognized
reason as *unknown*, not as *unconsented*.

Four properties of revocation follow from what it is — a statement made later,
not a kill switch (the SDK already holds the JWT and the AI call has already
gone out):

- **Opt-in.** Revoking a key does *not* withdraw its proofs. Most revocations
  are ordinary rotations, where the key was never at risk; withdrawing an hour
  of proofs of calls that really were consented, every time somebody rotates,
  would destroy evidence to no end and could not be undone. The caller has to
  state that the key actually leaked.
- **Per key, not per application.** Only the proofs that key minted. Another
  key's proofs, for calls that really happened, stand.
- **Never retroactive over `expired`.** `expired` is terminal. A proof that
  aged out is not restamped `revoked`, because that would tell an auditor a
  proof was withdrawn when it merely got old.
- **Not a ledger event.** The ledger says the call *was* authorized, at that
  time, under those preferences, and that stays true whatever later happens to
  the key. It is append-only precisely so nothing later reaches back into it;
  the revocation is its own dated record on the token's own row, and the hash
  chain is untouched.

## 6. AI Consent String (future direction)

Beyond the JWT, a compact format readable by providers (the equivalent of IAB's
TCF string) is envisaged:

```
ai_consent=tr=0;rt=none;src=gpc;lb=consent;ts=1711972320
```

This lightweight format would pave the way for ecosystem-scale adoption, where
the provider reads consent directly (Type 2 absorbing Type 1 — see
`docs/ARCHITECTURE.md` §3).
