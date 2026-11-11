# Versioning, compatibility and deprecation

> **Status.** The current pre-release is `0.1.0.dev1` (`idside` on PyPI) /
> `0.1.0-dev.1` (`@idside/sdk` on npm); the first was `0.1.0.dev0` /
> `0.1.0-dev.0`. This page was written *before* that
> first publish on purpose: the moment a package is installable, the promise it
> makes has already been made, and the first unannounced break costs trust that
> is not won back.
> The release mechanics live in [`RELEASING.md`](../RELEASING.md).

---

## 1. Two things are versioned, and they are not the same thing

This is the part that catches people, so it comes first.

| | What it is | Who moves it | Can you pin it? |
| --- | --- | --- | --- |
| **The package** | `idside` / `@idside/sdk` — the code in your `requirements.txt` or `package.json` | this repository | **yes** |
| **The protocol** | what the SDK and the ID Side cloud agree on: `/v1/authorize`, the ACT token format, the provider descriptors, the consent catalogue | the ID Side cloud | **no** |

You pin the package. **You cannot pin the cloud** — there is one, it is shared,
and it is upgraded without asking you. So "compatibility" here is not one
promise but two, and the second one is the cloud's:

> **An SDK that worked yesterday keeps working tomorrow.** Any version of this
> SDK, however old, must keep functioning against the cloud as it is today.

That is not politeness. The SDK is [fail-closed](./concepts.md#9-fail-closed):
if it cannot
understand the cloud's answer, it does not make the AI call. A protocol change
that an older SDK cannot parse does not degrade — it stops every wrapped client
still running that version. The blast radius of a careless cloud change is
"nobody's AI features work", which is why the rules below are absolute rather
than aspirational.

What that promise covers is the **exchange**, not the **outcome**. It guarantees
that every SDK keeps understanding what the cloud sends and keeps applying it
correctly. It does not guarantee what the cloud decides for a given person —
that changes every time someone updates a choice, and it changes when the rule
that resolves an unanswered question changes, as it did when silence became a
refusal ([CHANGELOG](../CHANGELOG.md)). A call refused by a decision the SDK
already knows how to apply is the SDK working, not the promise broken.

### 1.1 What the cloud may and may not change under `/v1`

The SDK reads named fields out of the authorize response and ignores everything
else. Ten fields are mandatory to it — `decision`, `consent_state`, `source`,
`legal_basis`, `binding`, `params_forced`, `mode`, `act_token`, `token_id` and
`expires_at` — and two are optional: `constraints` and `credential_posture`.
The mandatory fields are not presence-only: `decision` must be `allow`, `mode`
must be `test` or `live`, and every value must have the runtime shape the `/v1`
contract declares. The SDK also closes the value domains it acts on (`binding`
and `constraints`). It deliberately treats `source` and `legal_basis` as opaque,
non-empty printable metadata because neither changes local enforcement; adding a
new provenance or legal basis must not stop older SDKs. From that, exactly:

| Change | Allowed under `/v1`? | Why |
| --- | --- | --- |
| **Add** a top-level response field | ✅ always | Older SDKs never look at it. |
| **Add** a 403 problem `type` | ✅ | An SDK that does not know it still raises `ConsentDeniedError`, with `reason` unset: the call is refused all the same, and only the explanation is missing. `policy_hold` arrived this way. |
| **Add** a `source` or `legal_basis` value | ✅ | Both SDKs carry these as opaque strings and never branch on them, so a new value reaches the proof and the developer's code untouched. `public_task`, `legal_obligation` and `vital_interests` arrived this way. Renaming one is not adding: a claim may be added, never redefined (§1.2). |
| **Add** a key inside `constraints` | ❌ under `/v1` | A constraint is an instruction to apply locally, so an SDK that does not know it cannot ignore it: it fails closed and stops every call. A new constraint ships with the SDK release that understands it. |
| Make an **optional** field mandatory | ✅ | The SDK already tolerates its presence. |
| **Remove or rename** a mandatory field | ❌ **never** | Every older SDK raises `ConsentUnreachableError` and stops calling the provider. |
| Make a **mandatory** field optional | ❌ | Same failure: the SDK requires it. |
| Change the **meaning** of a field | ❌ | Worse than an error, because nothing fails. |
| Require a **new request** field | ❌ | Older SDKs do not send it. |
| Stop accepting a request **without** `call_site` | ❌ | An SDK that does not know the endpoint omits it. |
| Treat a **missing** `credential_postures` as "route freely" | ❌ | Its absence means "this SDK cannot route"; see [credential routing](./providers.md#6-credential-routing). |

A change that is not allowed above is not made under `/v1`. It is made at
`/v2/authorize`, with both served side by side until the `/v1` traffic is gone.
A new SDK minor may start using `/v2`; the old ones keep talking to `/v1`.

### 1.2 The ACT token is versioned by "forever"

A proof outlives the SDK that obtained it. Tokens sit in the ledger, in
customers' logs, in an auditor's evidence bundle, and someone will verify one
years from now with whatever SDK they have. So:

- **The request-hash function is frozen.** `sha256:` + SHA-256 of the
  [JCS](https://www.rfc-editor.org/rfc/rfc8785) canonicalisation of the caller's
  pre-enforcement payload, excluding the identity parameter and per-request
  transport headers. Forced parameters and constraint transformations happen
  afterward and are not added to the hash
  ([spec §3.1](./spec/act-token.md#31-request_hash--canonicalization)). Changing
  it would mean an auditor recomputing the hash from a stored request gets a
  different answer than the token says — the proof becomes unfalsifiable in the
  wrong direction. The cross-language
  [test vectors](../packages/descriptors/testdata/request-hash-vectors.json) are
  what hold both SDKs and the cloud to it.
- **Claims may be added, never redefined**, and `alg` stays `ES256` with `typ`
  `act+jwt`. A verifier pins both and rejects anything else — that is a forgery
  check, not a negotiation.
- **`verify_act()` / `verifyAct()` must verify old tokens.** Auditing an
  archived call needs `now=` (the token expired long ago against the wall
  clock), and nothing else about the check may have moved.

If the format ever has to change incompatibly, it changes `typ` and the old one
stays verifiable — because the tokens that already exist do not get reissued.

### 1.3 Descriptors and the consent catalogue

Both are shared data with their own version fields, and both cross the boundary
to the cloud:

- **Provider descriptors** (`descriptor_version`, currently `2`) declare where
  to intercept and what may be forced. The `id` of a call site is what travels
  to the cloud as `call_site` and is recorded in the ledger, so **a call-site
  `id` is renamed only by adding the new one and keeping the old** — the ledger
  already has entries under it.
- **The consent catalogue** (`catalogue_version`, currently `2`) names the
  options (`training.foundation`, `retention.conversation`, …). Those names
  appear inside `consent_state` in **signed** tokens. A signed claim cannot be
  migrated: renaming an option would leave old proofs describing an option
  nobody can look up. Options are therefore added, and retired ones keep their
  name reserved.

Both are bundled into each SDK, kept byte-identical to
[`packages/descriptors/`](../packages/descriptors/) by a test, and updated in
the same release in both languages.

---

## 2. What the package version promises

[Semantic Versioning](https://semver.org/), with the `0.x` clause spelled out
rather than left to the reader.

### While the major version is `0`

- **A patch bump (`0.1.0` → `0.1.1`) never breaks anything.** Fixes, docs,
  internals.
- **A minor bump (`0.1.x` → `0.2.0`) may break the public surface**, and every
  break is listed in [`CHANGELOG.md`](../CHANGELOG.md) under `Changed` or
  `Removed`, with what to write instead.
- There is no other size of change. `0.x` does not mean "anything can happen
  whenever"; it means breaks land on a minor boundary and are written down.

Pin accordingly, and note that the two ecosystems already spell this rule the
same way:

```toml
idside ~= 0.1.0      # >= 0.1.0, < 0.2.0
```

```json
"@idside/sdk": "^0.1.0"    // >= 0.1.0, < 0.2.0 — npm's caret does this for 0.x
```

### From `1.0.0` onwards

Breaks move to the major version, and a major is announced through the
deprecation path in §4 before it ships. `1.0.0` is cut when the cloud is
publicly available and the API has been used by people who are not us —
not on a date.

### The public surface, precisely

What the promise covers:

| | Python | Node |
| --- | --- | --- |
| Entry point | `consent.wrap()` and its keyword arguments | `consent.wrap()` / `wrap()` and `WrapOptions` |
| The call contract | `user_id=` / `ubt=` on an enforced call, and the closed list of per-call options it accepts — adding to that list is not a break, removing from it is | `user_id` / `ubt`, the `Enforced<T>` type, and the same closed list |
| Verification | `verify_act()` and every field of `VerifiedAct`; `request_hash()` | `verifyAct()`, `VerifiedAct`; `requestHash()` |
| Errors | every exported exception **class name** and its place in the hierarchy | the same, plus `error.name` |
| Options | `catalogue()`, `decidable_options()`, `disclosures()` and the shape they return | `catalogue()`, `decidableOptions()`, `disclosures()` |
| Providers | `ProviderAdapter` and the three methods an adapter implements | `ProviderAdapter`, `RequestMetadata` |
| Importable names | `idside` and its public submodules (`idside.consent`, `idside.verify`, `idside.exceptions`, `idside.options`, `idside.adapters`, `idside.providers`) | the `@idside/sdk` root export |
| Extras / packaging | the extra names `openai`, `anthropic`, `gemini`, `mistral`, `verify`, `all`; `py.typed` | ESM entry point, bundled `dist/*.json`, shipped types |
| Configuration | `IDSIDE_API_KEY`, `IDSIDE_BASE_URL` | the same two |
| The wire | the `X-AI-Consent-Token` header name | the same |

Deliberately **not** covered, so that the SDK can keep moving:

- Anything named with a leading underscore in Python (`idside._transport`,
  `idside._guard`, `idside._log`, …) and anything under `dist/` that the
  package's `exports` map does not name. A name re-exported from the `idside`
  root is covered by the row above whatever module it happens to live in —
  `ConsentDecision` and `HttpConsentClient`, for instance, are supported under
  `from idside import …` and unsupported under `from idside._transport import …`.
- Exception **messages**. Catch the class; do not match the prose.
- The **journal** line format (`NODE_DEBUG=idside`, the `idside` Python logger).
  It is a debugging aid, not an interface: parse it in a script and a patch
  release may move it. Its *event names* are kept stable across the two SDKs by
  `scripts/check_sdk_parity.sh`, which is a parity promise, not an API one.
- Which provider methods are patched *beyond* those the descriptors declare.

### The supported-runtime floor

Python `>= 3.10`, Node `>= 22.12` for the published package (running the
repository's TypeScript sources in place needs Node `>= 23.6`, which concerns
contributors, not consumers). **Raising a floor is a breaking change** and lands
on a minor while the major is `0`, on a major afterwards. Lowering one never
is, so it can happen in a patch.

---

## 3. What no version number can change

These are not part of the compatibility surface, because they are not features
to be traded off against convenience:

- no way to set or modify a user's preferences from the SDK
  ([the opaque tunnel](./concepts.md#3-the-opaque-tunnel));
- [fail-closed](./concepts.md#9-fail-closed) — no AI call goes out without a
  verified decision;
- [no bypass option](./providers.md#33-the-net-what-a-wrapped-client-refuses-to-send),
  no per-call escape, no transport opt-out;
- one token, one request ([ACT spec](./spec/act-token.md)).

A release that weakened one of these would not be a major version of this SDK.
It would be a different product wearing its name, and the proofs it produced
would be worth less than none. If you find something that looks like an
exception, that is a bug and a
[security report](../SECURITY.md), not a feature request.

---

## 4. Deprecation

**Nothing in the SDK is deprecated today.** This is the procedure for when
something is, so that it is not improvised under pressure.

A deprecation is announced in three places at once, in the release that
introduces it:

1. **`CHANGELOG.md`**, under `Deprecated`, naming the replacement and the
   release the removal is scheduled for. An entry with no replacement is not a
   deprecation, it is a removal wearing a softer word.
2. **The documentation page** where the thing is explained, at the top.
3. **At runtime**, once per process, on the caller's own channel:
   `DeprecationWarning` through `warnings.warn` in Python,
   `process.emitWarning(…, "DeprecationWarning")` in Node. Not through the
   journal — the journal is silent unless asked, and a deprecation nobody sees
   is not announced. Never on the AI request path more than once: a warning per
   call is noise that gets filtered, and then the removal is a surprise anyway.

### How long it lasts

| | Minimum before removal |
| --- | --- |
| While the major is `0` | one full minor release **and** 90 days |
| From `1.0.0` | two minor releases **and** 6 months, removal at the next major |

Shortened only for a security fix, and then the CHANGELOG says which
vulnerability forced it.

### What deprecation is *not*

`npm deprecate` and a PyPI yank act on a **release**, not on an API. They mean
"this build is broken, do not use it", and they belong to the
[release process](../RELEASING.md#5-when-a-release-goes-wrong). Deprecating an
API is the paragraph above; withdrawing a build is that one.

---

## 5. If you are pinning today

While only pre-releases exist, pin the exact version: `== 0.1.0.dev1` /
`0.1.0-dev.1`. From `0.1.0` onwards:

- pin `~= 0.1.0` / `^0.1.0` and read the CHANGELOG on a minor bump;
- pin the **cloud** nowhere — it is a service, and §1 is the promise that
  covers it;
- keep the ACT tokens you receive. They are the proof, they verify offline
  forever, and they are the one artefact that outlives every version number on
  this page.
