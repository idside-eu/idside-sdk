# Error catalogue

Every error the SDKs raise, when it happens, and what to do about it. The
Python and Node.js SDKs expose the **same types with the same names** —
nineteen for enforcement, plus twelve more for [local token
verification](#verification-errors); where behavior differs, it is called out.

**Read this first:** on the enforcement path, no error ever means "call the AI
provider anyway". The SDK is [fail-closed](./concepts.md#9-fail-closed) — when
authorization did not succeed, the provider call simply did not happen. There is
no partial state to clean up and no request in flight. The verification errors
follow the same rule one level up: a proof that does not check out is not
accepted.

```python
from idside import IDSideError, ConsentDeniedError   # Python
```

```ts
import { IDSideError, ConsentDeniedError } from "@idside/sdk"; // Node.js
```

---

## At a glance

| Error | Trigger | Retry? | Fix |
| --- | --- | --- | --- |
| [`ConsentUnreachableError`](#consentunreachableerror) | network failure, timeout, 5xx, undetermined 4xx, unparseable 200 | later, with backoff | usually nothing — it is an outage |
| [`ConsentDeniedError`](#consentdeniederror) | HTTP 403 — the usage is refused | no | surface the denial in your product |
| [`AuthenticationError`](#authenticationerror) | HTTP 401 — the API key is refused | no | fix the key |
| [`InvalidBindingError`](#invalidbindingerror) | HTTP 401 `ubt_invalid` — the visitor's UBT did not verify: expired, issued for another application, altered, or not a UBT at all | with a new UBT | discard the stored UBT; the widget issues a new one |
| [`BindingRequiredError`](#bindingrequirederror) | HTTP 401 `ubt_required` — the application requires a UBT and the call passed `user_id` | once the visitor is bound | show the widget before any AI call; pass its UBT as `ubt` |
| [`BindingError`](#bindingerror) | the parent of the two above: the visitor's binding was refused, not the key | once the visitor is bound | show the widget — one handler for both |
| [`ConsentRefreshRequiredError`](#consentrefreshrequirederror) | HTTP 409 `consent_refresh_required` — the visitor's choices no longer cover what the application requires | once the visitor has answered again | show the widget again — not an outage |
| [`QuotaExceededError`](#quotaexceedederror) | HTTP 402 — plan quota exhausted | after reset | upgrade or wait |
| [`RateLimitedError`](#ratelimitederror) | HTTP 429 — too many authorize calls | yes, backoff | slow down |
| [`ContractError`](#contracterror) | HTTP 422 — payload rejected | no | fix the value the cloud's detail names; otherwise upgrade the SDK or report it |
| [`ConsentTokenError`](#consenttokenerror) | the request cannot be hashed deterministically | no | pass JSON-compatible values |
| [`MissingIdentityError`](#missingidentityerror) | neither `user_id` nor `ubt` | no | pass exactly one |
| [`ConflictingIdentityError`](#conflictingidentityerror) | both `user_id` and `ubt` | no | pass exactly one |
| [`UnenforceableOptionError`](#unenforceableoptionerror) | the call passes an option that could make it leave in a state enforcement cannot vouch for — a body override, a reserved header, another host | no | drop the option |
| [`UnsupportedProviderError`](#unsupportedprovidererror) | no adapter matches the client, or the client cannot be guarded | no | check support, or register an adapter |
| [`ToolUseRefusedError`](#tooluserefusederror) | the call hands the model a tool — `tools` or any other tool field — and this user has not accepted agentic tools: refused, never asked, or refused by a policy | no | retry without the tools, or route the user to a non-agentic path |
| [`CredentialRoutingError`](#credentialroutingerror) | the `credentials` given to `wrap()` cannot route | no | fix the mapping — raised at `wrap()` time |
| [`MissingCredentialError`](#missingcredentialerror) | this user's answer needs a credential you have not provisioned | no | provision it and label it |
| [`UnenforcedCallError`](#unenforcedcallerror) | a body-bearing request was about to leave a wrapped client without going through enforcement | no | send that traffic on a second, unwrapped client |
| [`IDSideError`](#idsideerror) | base class — catch-all | — | — |
| [`ActVerificationError`](#verification-errors) + 11 subclasses | a proof did not check out during local verification | no | reject the proof |

Four of them (`ConsentTokenError`, `MissingIdentityError`,
`ConflictingIdentityError`, `UnenforceableOptionError`) and most
`UnsupportedProviderError` cases are raised **before any network I/O**: nothing
was sent to ID Side either.

`ToolUseRefusedError` is the one entry in this table that is not a failure. It
is a user's answer — or the silence that counts as one — arriving where it can
be applied, and your code should expect it the way it expects a 402 from a
payment provider.

`CredentialRoutingError` is raised by `wrap()` itself, at startup, not on the
request path — with one exception noted in its section.

`UnenforcedCallError` is the one entry that is **not** an `IDSideError` in
Python (it derives from `BaseException`), and the one raised by the transport
guard rather than by enforcement. Its [section](#unenforcedcallerror) explains
why, and how to catch it in each SDK.

---

## `IDSideError`

The base class of every error below **except
[`UnenforcedCallError`](#unenforcedcallerror)**, which sits outside the
hierarchy in Python on purpose. Catch it when you want a single boundary around
consent enforcement and do not need to distinguish cases:

```python
from idside import IDSideError

try:
    client.chat.completions.create(..., user_id="usr_marie_456")
except IDSideError as exc:
    logger.warning("AI call not made: %s", exc)
    return fallback_response()
```

Catching `Exception` around a wrapped call is a mistake worth avoiding: it hides
the difference between "consent denied" (a product decision) and "provider
returned a 500" (an infrastructure problem).

In Node, `error.name` is set to the concrete class name, and `instanceof` works
as expected.

### Finding it in a chain of causes

How deep the ID Side error sits in a chain of causes depends on the provider
client, not on the SDK. So do not count links, and do not walk to the root
cause: **search the chain for the first ID Side error.**

- [`ConsentUnreachableError`](#consentunreachableerror) is at the **top**. The
  SDK raises it before the provider client is involved, and it has causes of
  its own: the socket error, and that error's cause. Walking to the root returns
  something like `Error: bad port`, and a clean fail-closed stop gets filed as
  an unknown error.
- [`UnenforcedCallError`](#unenforcedcallerror) is **one level down** in Node:
  `openai-node` catches it in its transport and throws its own
  `APIConnectionError`, with `cause` set to the refusal. Another provider
  library may wrap it differently, or not at all.

```ts
import { IDSideError } from "@idside/sdk";

/** The first ID Side error in a cause chain, wherever the provider put it. */
export function findIDSideError(error: unknown): IDSideError | undefined {
  const seen = new Set<unknown>(); // a cause chain can loop
  for (let link = error; link instanceof Error && !seen.has(link); link = link.cause) {
    if (link instanceof IDSideError) return link;
    seen.add(link);
  }
  return undefined;
}
```

`undefined` means no ID Side error anywhere in the chain: the failure is the
provider's or the network's, and the SDK did not stop the call.

In Python the supported provider libraries do not wrap these errors —
`UnenforcedCallError` derives from `BaseException` precisely so that
`openai-python` cannot, and the others are raised by the SDK before the call
reaches the provider client's transport — so they reach you at the top, the
original exception chained under `__cause__`. A layer of your own between the call and the handler can change
that, and the same rule applies: search `__cause__` and `__context__`, and
include `UnenforcedCallError`, which is not an `IDSideError` in Python.

```python
from idside import IDSideError, UnenforcedCallError


def find_idside_error(exc: BaseException) -> BaseException | None:
    """The first ID Side error in an exception chain, wherever it was raised."""
    seen: set[int] = set()  # a chain can loop
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        if isinstance(link, (IDSideError, UnenforcedCallError)):
            return link
        seen.add(id(link))
        link = link.__cause__ or link.__context__
    return None
```

---

## `ConsentUnreachableError`

**Consent could not be verified.** The most important error in the catalogue,
because it is the one that expresses the fail-closed guarantee.

Raised when:

- the HTTP request to `sdk.idside.eu` fails (DNS, TLS, connection reset, proxy);
- it times out — 5 seconds in both SDKs, and configurable in both;
- the cloud answers `5xx`;
- the cloud answers a `4xx` that is not one of the qualified ones below;
- the cloud answers `200` with a body that is not JSON, is not a JSON object,
  or lacks any of `decision`, `consent_state`, `source`, `legal_basis`, `binding`,
  `params_forced`, `mode`, `act_token`, `token_id` and `expires_at`;
- a successful decision is not exactly `allow`, its `mode` is not `test` or
  `live`, or `consent_state` / `params_forced` are not objects;
- `act_token` is not a bounded three-part compact token, `token_id` is not an
  ACT identifier, or `expires_at` is not a calendar-valid RFC 3339 date-time;
- `source` / `legal_basis` are not non-empty printable strings, `binding` is not
  `ubt` or `user_id`, or `constraints` contains anything except the supported
  `tools: "refuse" | "strip"` instruction.

That last case matters: a partially understood answer is treated as no answer.
The SDK never infers a decision from a malformed response.

**There is no retry inside the SDK, by design.** `authorize` is not safely
idempotent — a request that reached the cloud before the connection dropped has
already minted a token and appended a ledger entry, so a blind retry risks a
duplicate entry in an append-only audit log. Retrying is your decision, at your
layer, where you know whether the user is still waiting.

What to do: treat it as a transient outage. Retry later with backoff, or degrade
the product path (queue the task, offer a non-AI answer). If it persists, check
your network path to `sdk.idside.eu`.

When neither `IDSIDE_BASE_URL` nor a base URL argument is set, the SDK talks to
its default, `https://sdk.idside.eu`, and a network failure or an unexpected status
ends with one more sentence saying so — the class is the same. If you run a
local or staging cloud, set `IDSIDE_BASE_URL` to its origin.

The original exception is chained — `__cause__` in Python, `error.cause` in Node
— so your logs keep the underlying socket or timeout error. That is also why a
helper that walks to the root cause loses this error: search the chain instead
([how](#finding-it-in-a-chain-of-causes)).

---

## `ConsentDeniedError`

**The requested AI usage was refused** (HTTP 403) — the account behind your
application is stopped, by billing, compliance or fraud review; the secret key
you passed to `wrap()` was minted without the `authorize` scope; or the
organization's policy could not apply to this call and held it. This is about your
account, your key or the organization's policy, never about what one user chose.

A user's own refusal does **not** arrive here, and neither does silence. Both
resolve to an authorization that succeeds and carries the refusals with it: the
provider parameters come back forced, where the only lever is the call itself
you get [`ToolUseRefusedError`](#tooluserefusederror) locally, and where the
refusal names a credential posture you have not provisioned you get
[`MissingCredentialError`](#missingcredentialerror). So if you are looking for
where "this person said no" lands, it is not this exception.

The `reason` attribute qualifies the denial when the cloud provides one:

| `reason` | Meaning | What to do |
| --- | --- | --- |
| `"fraud_suspended"` | the app is suspended by fraud detection | contact ID Side support through the dashboard |
| `"organization_suspended"` | your organization's whole account is stopped | this is billing or compliance, not your app — talk to whoever owns the account |
| `"policy_hold"` | the organization's policy cannot apply to this call, and the application is set to hold such calls — [below](#policy_hold--a-call-the-policy-held) | no option in your code lets a held call out: change what the call names, or ask whoever sets the policy. Changing what the call names is a change to your integration, never a fallback in an error handler: catching `policy_hold` to retry the call elsewhere would route around the organization's decision |
| `None` / `undefined` | a 403 whose reason the SDK does not qualify — today, a secret key without the `authorize` scope (`insufficient_scope`) | honor it, and read the message: the cloud's detail names the missing scope. Mint a key that carries it |

Those are the only three qualified reasons, and they are deliberately distinct:
sending a developer to a fraud review when their organization's account is
stopped sends them to the wrong person, and so does sending them to billing
when the account is fine and its policy held one call. Any other `type` —
including one from our own namespace that this version of the SDK does not
know — leaves `reason` unset.
**The denial still lands**; only the explanation is withheld, so a branch on
`reason` is never what stands between a refused call and the provider.

The value is read from the problem document's `type`, which is a URI
(`https://idside.eu/errors/<name>`). Only that namespace can name a reason — a
`type` from anywhere else is left unqualified rather than interpreted.

```python
from idside import ConsentDeniedError

try:
    summary = client.chat.completions.create(..., user_id="usr_marie_456")
except ConsentDeniedError as exc:
    if exc.reason in ("fraud_suspended", "organization_suspended"):
        alert_operations(exc.reason)          # your problem, not this user's
    elif exc.reason == "policy_hold":
        report_policy_hold(str(exc))          # the call or the policy has to change
    return "AI summarization is not available."
```

Note what the branch does *not* do: it never retries and never picks another
route. The three qualified reasons say who to tell; the answer to the user is
the same in all four cases.

In Node the field is `error.reason` (`string | undefined`).

**Never retry a denial**, and never route around it — a second call with a
different identity is exactly the pattern fraud detection is watching for.

### `policy_hold` — a call the policy held

Only on an application that imposes an organization's policy on its employees
([Concepts §4](./concepts.md#enforce-and-observe--when-the-policy-cannot-apply)).
When your policy cannot apply to a call (a provider with no such setting, a
provider your organization has not allowed, an employee who is not identified),
Observe lets the call through and flags it, Enforce holds it. This is Enforce:
`/v1/authorize` answers 403, no token is signed, and the provider is never
called.

```json
{
  "type": "https://idside.eu/errors/policy_hold",
  "title": "Policy hold",
  "status": 403,
  "detail": "…",
  "deviations": [{ "kind": "option_not_enforced", "option": "training.foundation" }]
}
```

`deviations` names what held the call, with the same kinds a flagged proof
carries ([spec](./spec/act-token.md#deviations--what-the-policy-could-not-apply)):
kinds and option names, never a person. The SDK surfaces `reason` and, in the
message, the cloud's `title` and `detail`; every held call is also listed on the
organization's ID Side dashboard.

A developer can never bypass a wrapped client: a deviation never comes from a
setting your code turned off, it comes from what the call names — the provider,
the endpoint, the model or the identity, and the credentials you labelled — set
against what the organization has set up. So no option in your code lets a held
call out, but what the call names is your code's, and the policy is its owner's:

| `kind` | What it means | What your code controls | What whoever sets the policy controls |
| --- | --- | --- | --- |
| `provider_not_allowed` | the organization allows a list of providers, and this call's is not on it | the provider it calls: one on the list | the list: allowing the provider |
| `option_not_enforced` | the policy refuses `option`, and nothing applies that refusal to this provider, endpoint or model — or only a credential you have not labelled could | the provider, endpoint and model it calls: one that applies the refusal, such as a model the provider's descriptor covers; and, where the refusal needs a dedicated credential, labelling one that carries it in `credentials` ([credential routing](./providers.md#6-credential-routing)) | the policy, and the provider account setting declared to ID Side that applies it, where one exists |
| `basis_not_established` | the call would be signed `legal_basis: "not_established"` — the organization declared no basis, or the employee is not identified | the identifier it passes: the work email or external reference the organization registered for that employee, as `user_id` | the basis, and the employees the organization registers |

What your code controls is a change to your integration, not a fallback in an
error handler: retrying the same call is held again. Whether calls like it are
held or let through and flagged is the organization's choice, on the ID Side
dashboard — never an option in your code. An SDK released before `policy_hold`
existed raises the same `ConsentDeniedError`, with `reason` unset: the call is
held all the same.

---

## `AuthenticationError`

**ID Side rejected your API credentials** (HTTP 401). A configuration problem,
not an outage; retrying without changing anything cannot succeed.

Common causes: no key at all (neither the `api_key` / `apiKey` option nor the
`IDSIDE_API_KEY` environment variable), a revoked key, or a test key used against
production (or the reverse).

What to do: check the key against the ID Side dashboard, and check that your
process actually sees the environment variable it thinks it does.

A key that an HTTP header cannot carry never gets that far. Whitespace around
it — the line break a secret file usually ends with — is dropped, in both SDKs.
Any other space, tab or line break inside it, or a character outside printable
ASCII, stops the transport where it is built, before any request: `ValueError`
in Python, `TypeError` in Node, with a message naming `IDSIDE_API_KEY` and the
key argument but never repeating the key.

Not every 401 is about the key, though, and the two that are not have classes
of their own.

### Three 401s, three different answers

The cloud answers 401 when it cannot accept *who is calling*, and that is either
your backend or the visitor it names. The problem document's `type` says which,
and the SDK raises a different class for each:

| Class | The cloud's `type` | Cause | What your code should do |
| --- | --- | --- | --- |
| `AuthenticationError` | `invalid_api_key`, or anything else | the secret key is missing, revoked, or of the wrong kind | **alert whoever operates the service**: every AI call fails until the key is fixed |
| [`InvalidBindingError`](#invalidbindingerror) | `ubt_invalid` | the visitor's UBT did not verify: it expired — it lives 24 hours —, was issued for another application, was altered, or is not a UBT at all | **purge the UBT from this visitor's session**, stop the AI feature for them, and let the widget issue a new one |
| [`BindingRequiredError`](#bindingrequirederror) | `ubt_required` | the application requires bound tokens, and the call named its user with `user_id` | **show the widget before any AI call**, and pass the UBT it issues as `ubt` |

The second is the one a production application meets every day: any visitor
who comes back after 24 hours with a server session still open sends a UBT
that has expired. Your key is fine, and regenerating it would change nothing.
The widget renews a binding without touching a single choice —
`IDSide.showPreferences()`, then **Confirm these choices** — and its `onUBT`
callback hands your backend the new token
([widget §8](./widget.md#8-from-a-recorded-decision-to-a-stronger-proof)).

The third is the one met the day an application is switched to bound tokens:
the setting changed, and the backend still names visitors with `user_id`. That
is not a broken key either. `user_id` is never a fallback for a strict
application, so the only way through is a visitor who went through the widget.

Both binding classes derive from `BindingError`, which derives from
`AuthenticationError`: a handler written before they existed still catches
them, so put the specific ones first.

```python
from idside import AuthenticationError, BindingRequiredError, InvalidBindingError

try:
    answer = client.chat.completions.create(
        model="gpt-4o-mini", messages=messages, ubt=session["idside_ubt"]
    )
except InvalidBindingError:
    session.pop("idside_ubt", None)    # stale: the widget will issue a new one
    return WIDGET_REQUIRED
except BindingRequiredError:
    return WIDGET_REQUIRED             # this app takes no user_id: bind first
except AuthenticationError:
    alert_operations("ID Side refused the API key")
    raise
```

In Node the same `instanceof` tests apply, most specific first.

The class is read from the problem `type` alone, against a closed list: a
`type` this version of the SDK does not know leaves the error an
`AuthenticationError`, never one of its subclasses. The message names the cause
and the remedy, and ends with the cloud's own detail — `UBT is invalid or
expired (Signature has expired).`, for instance — but branch on the class, never
on the message.

---

## `BindingError`

**The visitor's binding was refused, not your key** (HTTP 401). The parent of
[`InvalidBindingError`](#invalidbindingerror) and
[`BindingRequiredError`](#bindingrequirederror), and itself an
`AuthenticationError`. Both visitor cases call for the same gesture — show the
widget before the next AI call — so this is the handler most applications want:
one branch covers a UBT that no longer verifies and, the day the application is
switched to bound tokens, a call still made with `user_id`.

```python
from idside import AuthenticationError, BindingError

try:
    answer = client.chat.completions.create(
        model="gpt-4o-mini", messages=messages, ubt=session["idside_ubt"]
    )
except BindingError:
    session.pop("idside_ubt", None)   # stale or missing: the widget issues a new one
    return WIDGET_REQUIRED
except AuthenticationError:
    alert_operations("ID Side refused the API key")
    raise
```

```ts
import { AuthenticationError, BindingError } from "@idside/sdk";

try {
  return await client.chat.completions.create({
    model: "gpt-4o-mini",
    messages,
    ubt: session.idsideUbt,
  });
} catch (error) {
  if (error instanceof BindingError) {
    delete session.idsideUbt; // stale or missing: the widget issues a new one
    return WIDGET_REQUIRED;
  }
  if (error instanceof AuthenticationError) alertOperations("ID Side refused the API key");
  throw error;
}
```

Test for it before `AuthenticationError`, which also catches it: the order is
what keeps a visitor's expired binding from paging whoever owns the keys.

---

## `InvalidBindingError`

**The visitor's UBT did not verify** (HTTP 401, `ubt_invalid`): it expired — a
UBT lives 24 hours —, was issued for another application, was altered, or is not
a UBT at all. A [`BindingError`](#bindingerror). Every visitor who comes back the
next day with a server session still open produces it; your key was accepted.

What to do: purge the UBT from this visitor's session, stop the AI feature for
them, and let the widget issue a new one. `IDSide.showPreferences()`, then
**Confirm these choices**, renews a binding without changing any choice, and
`onUBT` hands your backend the new token
([widget §8](./widget.md#8-from-a-recorded-decision-to-a-stronger-proof)). Do
not retry with the same token, and do not fall back to `user_id`. The message
ends with the cloud's detail, which names the actual cause.

---

## `BindingRequiredError`

**The application requires a UBT, and the call passed `user_id`** (HTTP 401,
`ubt_required`). A [`BindingError`](#bindingerror). It is met the day an
application is switched to bound tokens while its backend still names visitors
with `user_id`; your key was accepted.

What to do: show the widget before any AI call, store the UBT it issues in the
server session, and pass it as `ubt`. `user_id` is never a fallback for a strict
application, so there is no retry that succeeds without the visitor going
through the widget.

---

## `ConsentRefreshRequiredError`

**The visitor has to review their choices before this call** (HTTP 409,
`consent_refresh_required`). Only an application that requires bound tokens
meets it: the visitor's UBT still verifies, but the choices it is bound to no
longer cover what the application now requires — its policy changed, or a
provider was added and brings questions they were never asked.

It is not an outage, which is why it is **not** a
[`ConsentUnreachableError`](#consentunreachableerror): retried later, the call
gets the same answer until the visitor answers again. A retry loop that catches
outages must not catch it.

What to do: stop this AI request and show the visitor the widget. Once they have
recorded their current choices, the widget's `onUBT` callback hands your backend
a binding that covers them
([widget §8](./widget.md#8-from-a-recorded-decision-to-a-stronger-proof)).
Declare every provider the application calls in the dashboard before showing the
widget: a provider it does not know about is a question it cannot ask.

One case the widget cannot fix: a provider ID Side holds no questions for. The
cloud's detail, appended to the message, says so; the fix is a supported
provider, not another visit to the widget.

Any other 409 is still a `ConsentUnreachableError`, and the class is read from
the problem `type` against a closed list, as for the
[401s](#three-401s-three-different-answers).

---

## `QuotaExceededError`

**The app's plan quota is exhausted** (HTTP 402). AI calls through the SDK stop
until quota is available again.

There is deliberately **no silent degradation**: the SDK will not let calls
through unproven because you ran out of quota. See the
[FAQ](./faq.md#what-happens-when-my-quota-runs-out) for how to see this coming.

What to do: upgrade the plan, or wait for the quota window to reset. Alert on
this error — by the time it fires, calls are already failing.

---

## `RateLimitedError`

**Too many authorize requests in a short window** (HTTP 429).

What to do: back off and retry. Unlike `ConsentUnreachableError`, a 429 means the
request was rejected before doing any work, so retrying is safe. If you hit this
in normal operation, review your call patterns (are you authorizing inside a
loop that could be batched?) or request a higher limit.

---

## `ContractError`

**The cloud rejected the authorize payload as invalid** (HTTP 422). Two causes
answer the same way, and the cloud's detail, appended to the message, tells them
apart: a value your code passed — the detail names it — or an SDK/cloud contract
mismatch, most likely a badly outdated SDK or an SDK bug. The cloud's problem
`type` does not separate the two (`invalid_request` covers both), so the class
does not either.

What to do: if the detail names a value your code passed, fix that value. If it
does not, upgrade the SDK; if it persists on the latest version, report it at
https://github.com/idside-eu/idside-sdk/issues with the full error message —
the cloud's `title`/`detail` are appended to it (see [below](#reading-the-message)).

---

## `ConsentTokenError`

**A deterministic `request_hash` could not be computed from the call payload.**

The hash is the SHA-256 of the [RFC 8785 (JCS)](https://www.rfc-editor.org/rfc/rfc8785)
canonical form of your request. If a value in the payload cannot be canonicalized,
the SDK refuses to guess — a `str()` / `String()` fallback would embed
interpreter-dependent text (at worst a memory address) into the hash and destroy
the property that an auditor can recompute it.

What each SDK accepts in a call payload:

| | Python | Node.js |
| --- | --- | --- |
| Accepted as-is | `dict`, `list`, `tuple`, `str`, `int`, `float`, `bool`, `None` | plain objects, arrays, `string`, `number`, `boolean`, `null` |
| Accepted after conversion | objects exposing `model_dump()` (Pydantic v2) | objects exposing `toJSON()` — including `Date`, which becomes its ISO string |
| Rejected | `datetime`, `set`, `Decimal`, dataclasses, any other class instance | `Map`, `Set`, class instances, `bigint`, functions, symbols |

It is also raised for a non-string object key, for `NaN` or `Infinity`, and for
a lone surrogate in a string (not encodable as UTF-8).

The message names the offending path, for example
`… value of type 'Decimal' at 'metadata.price' is not JSON-serializable`.

> Note the one asymmetry in that table: a `Date` is hashed in Node (via
> `toJSON()`), while the Python equivalent `datetime` is rejected. Keep dates as
> ISO strings in your payloads and both SDKs agree.

The Gemini adapter also raises it when the `config` argument is neither a
plain object/dict nor a model exposing `model_dump()` / `toJSON()`, since it
cannot be normalized for injection.

What to do: pass JSON-compatible values. Convert Pydantic models with
`model_dump()`, dataclasses with `dataclasses.asdict()`, `Decimal` to `float` or
`str`, `datetime` to an ISO string.

> `ConsentTokenError` is also the parent of the whole
> [verification error family](#verification-errors) below: on the enforcement
> path it means "this request cannot be hashed", on the verification path it
> means "this proof does not hold". Catching it covers both.

### Related: `CanonicalizationError` (Node only)

The Node SDK exports the low-level `canonicalize()` / `requestHash()` helpers,
which throw `CanonicalizationError` — a plain `Error` subclass, **not** an
`IDSideError`. Inside `wrap()` it is always converted to `ConsentTokenError`; you
only meet the raw form if you call those helpers yourself (for instance to
recompute a hash during an audit). Python's equivalent is internal (`idside._jcs`)
and has no public surface.

---

## `MissingIdentityError`

**No `user_id` and no `ubt` was passed** to a consent-enforced AI call. Raised
before any I/O — nothing was sent to ID Side or to the provider.

```python
client.chat.completions.create(model="gpt-4o-mini", messages=[...])
# MissingIdentityError: user_id or ubt is required for consent-enforced AI calls.
```

What to do: pass exactly one identity on every call through a wrapped client.
The SDK consumes it and never forwards it to the provider — see
[Concepts §8](./concepts.md#8-naming-the-end-user-user_id-vs-ubt).

Note that an empty string, `None`/`undefined`, `0`, `false` and an empty list or
dict (array or object) all count as absent in both SDKs, and so does a string
made only of blanks — whitespace as the cloud counts it, which would otherwise
come back as a 422. Nothing else is trimmed: an identifier with spaces around it
is sent as written, because its pseudonym is derived from those exact
characters. A `user_id` that is empty because a lookup failed produces this
error rather than an anonymous call, which is the intended behavior.

**What a `user_id` may be is the cloud's to judge.** Neither SDK converts it:
it leaves as you wrote it, and the cloud accepts a non-blank string or a finite
number. Anything else — `true`, an object, a list — comes back as a 422, a
[`ContractError`](#contracterror) whose detail names `user_id`, and the provider
is not called; passing `req.user` where you meant `req.user.id` fails there
instead of merging your users into one. A number is read as written, so `42`
and `"42"` are the same user. The float form is where the two SDKs part: in
JavaScript `42.0` *is* the number `42`, so the Node SDK sends `42`, the same
user as `42`, while Python sends `42.0` as `42.0`, which the cloud reads as
another user. Pass the identifier as a string — the one the widget is given
for the same person, since the two must match ([Concepts
§8](./concepts.md#what-to-send-as-user_id)). In TypeScript, `Enforced<T>` asks
for one.

---

## `ConflictingIdentityError`

**Both `user_id` and `ubt` were passed** for the same call. Also raised before
any I/O.

The two forms are exclusive: the UBT already names the user, cryptographically.
A `user_id` next to it is at best redundant and at worst a spoofing attempt the
cloud would have to arbitrate — so the SDK refuses instead of picking one.

What to do: pass `ubt` alone when your frontend runs the widget, `user_id` alone
otherwise.

---

## `UnenforceableOptionError`

**The call passes an option that would let it leave in a state enforcement
cannot vouch for, so nothing was sent** — not to ID Side, not to the provider.
Raised before `/v1/authorize`, which is why no ledger entry exists for it.

Every provider library ships its own escape hatches, and each one undid
enforcement *after* the SDK had applied it:

| Option | What it did before it was refused |
| --- | --- |
| `extra_body` (Python); a second argument's `body` or `fetchOptions` (Node); `http_options.extra_body` / `httpOptions.extraBody` (Gemini) | merged over the request, or replaced it: `store: false` became `true`, tools a user refused went out, another model was called — while the signed proof described the request as authorized |
| a per-call `middleware` or `fallbackState` (Anthropic, Node) | ran between enforcement and the network, free to rewrite the body |
| `server_url` / `serverURL` (Mistral), `http_options.base_url` / `api_version` (Gemini), a second argument's `path`, `defaultBaseURL`, `query` (Node), `extra_query` (Python) | sent the prompt, the ACT token and your API key to another host |
| a per-call `Authorization`, `X-Api-Key`, `x-goog-api-key`, `OpenAI-Project`… | moved the call onto another credential, defeating the posture the user's answer required — invisibly, since header values are not part of the request hash |
| a local tool on Gemini: a Python function, an MCP session, a Node `CallableTool` | turned one authorized call into up to ten requests with different bodies under **one** token, which is exactly what a token must never cover |
| `automatic_continuation=True` / `automaticContinuation: true` (Gemini) | made the library answer a `CONTINUATION` response by sending the request again, by itself, under the same token. It is on by default in `google-genai` 2.30+ and `@google/genai` 3.x, so the SDK turns it off on every enforced call, and refuses a call that asks for it |
| `workspace_id`, `user_profile_id` (Anthropic) | look like body fields and are sent as the `anthropic-workspace-id` and `anthropic-user-profile-id` headers, outside the request hash: the first moves the call to another workspace — the unit [credential routing](./providers.md#6-credential-routing) chooses — and the second attributes it to a user profile whose programs can require retention and human review. Both headers are reserved too |
| `prompt_cache_options` (OpenAI) | sets a prompt-cache lifetime (`ttl`, up to `12h`) that the forced `prompt_cache_retention` does not bound, so a user's refusal of extended caching could be recorded beside a twelve-hour cache |
| `generation_output_config` / `generationOutputConfig` (Gemini) | had the provider write the generated media to a `gs://` bucket of the caller's choosing — a copy of the output kept somewhere the proof never names |
| a per-call `fetch` in `httpOptions` (`@google/genai`) | sent the request on a transport nobody wrapped |

So on an enforced call the SDK accepts a **closed list** of per-call options:
timeouts, retry settings, an abort signal, an idempotency key, and headers that
are not reserved (tracing headers, `anthropic-beta`). Everything else is refused
— including an option a future version of the provider library adds, which is
refused until someone has checked what it does. The message names the option or
the header, never its value: a value that found its way into a header is too
often a key.

The body itself stays open: a request parameter a newer provider library adds
is hashed, proven and sent. The exceptions are the fields above that the
library declares in its body schema and that an enforced call still may not
carry — because the library sends them somewhere the hash does not reach, or
because they undo a refusal from inside the body where nothing can force them
back.

What to do: drop the option. If you need a request parameter your provider
library does not type yet, upgrade the library rather than smuggling it through
the body. If you need a different host, credential or proxy, build a client
with it and wrap *that* client — `wrap()` fixes the host it will allow when it
wraps, and [credential routing](./providers.md#6-credential-routing) is how a
call moves onto another key. For Gemini tools, declare the functions and run the
calling loop yourself: each turn is then a call of its own, authorized and
proven. The same goes for a Gemini answer that stops with
`finish_reason=CONTINUATION`: resume it with a new enforced call carrying the
answer's `continuation_token` in its config. For an Anthropic workspace, wrap a
client whose key belongs to it, or declare one per posture to `credentials=`.

---

## `UnsupportedProviderError`

**No adapter matches this client.** Raised by `wrap()`, before any call is
enforced, in these cases:

| Case | Both SDKs |
| --- | --- |
| The client's library is not one of the four supported providers | yes |
| The client exposes none of the descriptor's declared call sites (e.g. a very old or very new provider version) | yes |
| A supported HTTP transport is found but every route cannot be guarded | yes |
| A transport whose `send` will not accept `follow_redirects`, so an authorized request could not be sent with redirects refused | Python only |
| An explicit `{ provider: "…" }` names a descriptor that does not exist | Node only |
| A real provider client whose transport the net cannot reach — `new Mistral({ apiKey })` built without an `httpClient` in Node, a `google-genai` client on Vertex mutual TLS or on `aiohttp` in Python | yes (the cases differ per library) |
| A client whose base URL does not name a server the SDK can bind the call to | yes |
| A client derived from a wrapped one (`with_options` / `withOptions`, `copy`) that points at another provider or another host, or brings its own transport, query or middleware | yes |

**Being async is not one of these cases.** Python's `wrap()` used to reject
`AsyncOpenAI` and friends with this error; it enforces them now, with the same
guarantees as their blocking counterparts. If you are reading this after
upgrading, an `UnsupportedProviderError` on an async client means what it means
on any other client: no descriptor matches it.

What to do: check the provider is supported ([providers](./providers.md)), pass
`{ provider: "openai" }` in Node if auto-detection failed on a wrapped or proxied
client, or register a custom adapter (`register_adapter()` / `registerAdapter()`).
For a transport-guard error, use a supported provider-client version, or give the
client a transport the net can see: `new Mistral({ apiKey, httpClient: new HTTPClient() })`
in Node, `http_options=HttpOptions(httpx_async_client=httpx.AsyncClient())` for
`google-genai` with `aiohttp` installed. `wrap()` refuses before patching its call
sites, so there is no half-wrapped client. For a derived client, wrap a new
client built with what you wanted to change: a new host is a new binding, and
it has to be made by `wrap()`, not inherited.

---

## `ToolUseRefusedError`

**Raised when** your call hands the model a tool and `agentic_tools` is not
accepted for this user. The provider is **not** called.

A tool is not only the `tools` argument. Every argument through which a call can
bring one counts, and each call site's descriptor names them (`tool_fields`):

| Provider | Tool fields besides `tools` / `config.tools` |
| --- | --- |
| OpenAI chat, DeepSeek | `functions`; `web_search_options`, which switches search on with no `tools` key at all |
| OpenAI Responses | `conversation`, `previous_response_id`, `prompt`; `input` items of type `additional_tools`, `tool_search_output`, `item_reference`, `mcp_approval_response` |
| Anthropic | `mcp_servers` |
| Gemini | `config.cached_content` / `cachedContent` |
| Mistral agents and conversations | `agent_id` / `agentId` |

Some of these are **references to something stored on the provider's side** — an
agent, a cache, a conversation, a reusable prompt — which may carry tools the SDK
cannot see. They are refused on sight, because the SDK cannot tell an agent with
tools from one without. The consequence is deliberate and worth planning for:
for a user who has not accepted agentic tools, Mistral agents, Gemini caches and
chained OpenAI Responses calls are refused. An empty `tools` list carries no
tool and is not refused, in either SDK.

Not an outage, and not a bug in your integration. Every provider's
zero-retention arrangement excludes agentic features while the APIs themselves
do not block them, so a call that hands the model tools falls outside the
retention regime the user was promised. The SDK is the only component that can
see that happening, because it is the only one that sees the outgoing request.

**Three situations produce it, and the second is the one that surprises people.**
The user refused agentic tools — or nobody ever put the question to them, which
comes to the same answer: what nobody accepted is refused, so an option a person
never answered is `false` before anything is enforced or signed. An integration
that hands the model tools and runs no [Preference Center](./widget.md)
therefore raises this on *every* call, for *every* user. That is not a
misconfiguration to work around and not an outage to escalate — it is the
mechanism asking you to ask. Install the widget, and the users who accept stop
raising it.

The third is an organization policy that refuses agentic tools — for its
employees in the imposed mode, or on top of a visitor's own answer. A policy only
tightens, so no answer anybody gives in the widget lifts it; only the
organization can.

```python
from idside import ToolUseRefusedError

try:
    answer = client.chat.completions.create(
        model="gpt-5", messages=messages, tools=tools, user_id="usr_marie_456"
    )
except ToolUseRefusedError:
    # Applied, whether she refused or was never asked. Answer without the
    # tools, or tell her what she is giving up and let her settle it in the
    # preference centre.
    answer = client.chat.completions.create(
        model="gpt-5", messages=messages, user_id="usr_marie_456"
    )
```

**Why it raises instead of quietly dropping the tools.** An agent stripped of
its tools does not fail — it answers wrongly, confidently, and you have no way
to know. Refusing hands you the decision.

The SDK also implements the other behaviour: if the instruction says `strip`,
the tool definitions are removed and the call goes out. A stored reference or a
tool-carrying `input` item is still refused under `strip`: removing it would
change what the call means, not merely take a capability away. Which of the two applies is decided
by ID Side, not by a flag in your code — it changes what the user's answer
means, so it does not belong on the calling side. Refusal is what is configured
today; there is no self-service control for it yet.

**Do not retry** the same call: it will be refused again, and a user nobody
asked cannot answer a question that was never put to them. Retry without the
fields the message names — a stored reference such as `conversation` included,
not only `tools` — route the user elsewhere, or — unless a policy is what
refused — run the widget so there is an answer to get.

**What ID Side is told to make this work: nothing.** The instruction comes from
the user's consent state alone. You never tell us whether a given call carries
tools, and we never ask — the SDK answers that question on your machine and
acts on it there. The ledger entry records that agentic tools were refused for
this user on this call, which is exactly as much as we know.

---

## `CredentialRoutingError`

**Raised when** the `credentials` mapping you gave `consent.wrap()` cannot do
what it promises. Almost always from `wrap()` itself, before a single AI call —
a configuration mistake belongs at startup, not in production.

[Credential routing](./providers.md#6-credential-routing) is how an
account-level privacy setting becomes a per-user one: no provider exposes
training opt-out as a request parameter, but the posture lives on the credential
(a project at OpenAI and Gemini, a workspace at Anthropic, an organisation at
Mistral), so provisioning one credential per posture lets the SDK send each call
on the one the user's answer requires.

The cases:

| What you did | Why it is refused |
| --- | --- |
| `credentials` for a provider whose descriptor has no `credential_routing` (DeepSeek) | it has no account-level posture a second credential could carry |
| a bare string for a provider with no routing header (Anthropic, Gemini, Mistral) | there is no header to put it in; the key is chosen when the client is built. Pass a second client |
| a string that is not an identifier of that provider's shape | it goes into an outgoing HTTP header. This is what stops an API key pasted into a project field, and CRLF injection with it |
| a client that does not expose the call sites being wrapped | a call routed to it would have nothing to call |
| a client of a different provider | routing between two vendors would send the prompt to a company the user's answer never mentioned |
| a client that is already `wrap()`ped | it would authorize twice and stack two ACT tokens on one request |
| an empty posture label | the label is what the cloud names when it routes a call |
| a client whose default headers already carry the routing header (Python: `OpenAI-Project` in `default_headers`) | the provider library would send both values, and which one the provider honours is not something to leave to chance |
| a derived client (`with_options` / `withOptions`, `copy`) that changes the key, token, organization or project, or adds a reserved header to its defaults | a credential change is exactly what routing declares; one made on the side keeps the parent's posture labels on a client that no longer holds them |

The message never repeats the credential itself: if what you pasted was a
secret, an exception is the last place it should end up.

**The one case raised on the request path**: if the adapter did not carry the
routed credential onto the outgoing call, the call is refused instead of sent.
Only a custom adapter that overrides `apply()` / `buildEnforcedArgs()` without
forwarding its credential-headers argument can trigger it — the built-in engines
are covered by tests. Failing here is the point: the alternative is a call
leaving on the very credential routing was meant to move it off.

---

## `MissingCredentialError`

**Raised when** ID Side resolved this user's preferences, answered that the call
must go out on a given credential posture, and nothing in your `credentials`
mapping carries it. A subclass of `CredentialRoutingError`. The provider is
**not** called.

```python
from idside import MissingCredentialError

try:
    answer = client.chat.completions.create(
        model="gpt-5", messages=messages, user_id="usr_marie_456"
    )
except MissingCredentialError as exc:
    # exc.posture     → "zero_retention"
    # exc.provider    → "openai"
    # exc.provisioned → ("default",)
    alert_ops(f"no {exc.provider} credential for posture {exc.posture}")
    raise
```

```ts
import { MissingCredentialError } from "@idside/sdk";
// error.posture · error.provider · error.provisioned
```

**Why it is not a fallback.** Sending the call on the credential you happen to
hold would place this user under a privacy regime they declined, produce a
perfectly valid-looking answer, and leave nothing anywhere to say it happened.
The refusal is the feature: it is the only moment at which the gap between what
the user chose and what your account is configured for becomes visible.

**Do not retry** — the answer will be identical until the configuration
changes. Provision a credential with that posture (the message names the unit
the provider uses: project, workspace, organisation), add it to `credentials`
under exactly the label ID Side named, and redeploy.

**A label typo looks exactly like a missing credential**, because from the SDK's
side it is one. The labels come from your ID Side dashboard; the SDK never
invents or normalises them.

---

## `UnenforcedCallError`

**A request was about to leave a wrapped client without going through consent
enforcement, so it was not sent.** Raised by the transport guard both SDKs
install on the provider client — the [net](./providers.md#33-the-net-what-a-wrapped-client-refuses-to-send).

Enforcement covers the call sites each provider's descriptor declares, and that
list can never be exhaustive: a provider ships a new resource, a helper posts to
the transport directly, a namespace nobody declared sits on the same client.
Rather than let those reach the provider unproven and untraced, a wrapped client
refuses any request that is not a `GET`, `HEAD`, `OPTIONS` or `DELETE` and does
not carry an ACT token. Nothing was sent to the provider, and nothing was sent
to ID Side either — the guard reads the HTTP method, the URL's origin and path,
and one header, and does no I/O of its own.

It is also what you hit when an enforced request is about to leave for **another
host** than the one the client was wrapped with — a base URL changed after
`wrap()`, for instance. `wrap()` fixes the origin (scheme, host, port) from the
client itself, and a token is only good for that origin. For the same reason an
enforced request never follows a redirect: a `307` or `308` would re-send the
prompt, the token and the key to a host nobody checked, so it comes back to you
as the provider library's own status error instead.

In practice this is what you hit when you call `embeddings`, `images`, `audio`,
`moderations`, `batches`, a file upload or a `beta.*` preview on a client you
passed to `consent.wrap()`.

It is **not** what you hit on Python's `with_raw_response.*` or
`with_streaming_response.*`. Those wrappers are built on first access, after the
client has been patched, so they wrap the enforced method: on a declared call
site they authorize and carry the token like any other call. Reaching one on an
*undeclared* endpoint (`embeddings.with_raw_response.create`) is refused, but
that is the endpoint's refusal, not the wrapper's. Node's `.withResponse()` and
`.asResponse()` behave the same way: they are carried through onto an enforced
call, so reading the raw response of a declared call site is enforced like any
other call.

It is also what you hit on `google-genai`'s async surface. One `genai.Client`
owns both an HTTP transport and an async one, and exposes the async methods at
`client.aio.*` — which no descriptor declares. The net guards **every** transport
a client owns, precisely so that surface cannot slip out unproven; the honest
consequence is that `client.aio.*` is refused rather than enforced. Use the
blocking surface of that client until a descriptor declares those call sites.

The same holds for `@google/genai`'s **NextGen surfaces** in Node —
`interactions`, `webhooks`, `agents`, `triggers`, `environments`, and the client
`getNextGenClient()` returns. They are a second API on the same object, each
building its own HTTP client, and no descriptor declares a call site on any of
their routes. They are guarded rather than left open, so a retrieval on them
still works and a body-bearing call is refused with a message naming the
surface. Use a second, unwrapped `GoogleGenAI` client for that API.

What to do: **build a second, unwrapped client** for non-inference traffic.

```python
enforced = consent.wrap(OpenAI())
plain    = OpenAI()

plain.embeddings.create(model="text-embedding-3-small", input=chunk)
```

There is no bypass option on the wrapped client — no `allow_unenforced=`, no
per-call escape — and there will not be one; it would be the opaque tunnel
defeated through the back door. If the endpoint you were calling *does* carry
inference, it needs a call site in the descriptor: report it at
https://github.com/idside-eu/idside-sdk/issues.

### Catching it: the one asymmetry between the SDKs

**In Python it derives from `BaseException`, not from `IDSideError`.** That is
deliberate and pinned by a test. `openai._base_client` wraps *any* `Exception`
raised while sending into its own `APIConnectionError` **and retries it** — so
an `IDSideError` here would come out of the client looking like a flaky network,
several attempts later. A blanket `except Exception:` in a provider library must
not be able to swallow a consent stop.

The consequence for you: `except IDSideError` does **not** catch it, and neither
does `except Exception`. Catch it by name.

```python
from idside import UnenforcedCallError

try:
    resp = client.embeddings.create(model="text-embedding-3-small", input=chunk)
except UnenforcedCallError as exc:
    logger.error("not sent: %s", exc)      # move this call to an unwrapped client
```

**In Node it extends `IDSideError`** like every other error — there is no
`BaseException` equivalent to reach for. But `openai-node` still wraps it into
its own `APIConnectionError`, with `cause` set to the `UnenforcedCallError`, so
a Node caller searches the cause chain rather than reading the top-level type —
with [`findIDSideError()`](#finding-it-in-a-chain-of-causes), not by counting
links:

```ts
import { UnenforcedCallError } from "@idside/sdk";

if (findIDSideError(error) instanceof UnenforcedCallError) {
  // never left the process: move this call to an unwrapped client
}
```

A connection error from a wrapped client with **no** `UnenforcedCallError` in
its cause chain is a real network failure; one with it is a request that never
left.

Both SDKs export the class (`from idside import UnenforcedCallError` /
`import { UnenforcedCallError } from "@idside/sdk"`).

**Do not retry.** The answer is identical until the call moves to an unwrapped
client or the endpoint gains a call site.

---

## Verification errors

These are raised only by local ACT verification — `verify_act()` in Python,
`verifyAct()` in Node.js — never on the enforcement path. They all inherit
`ActVerificationError`, which inherits `ConsentTokenError`, which inherits
`IDSideError`.

The design rule is the one that runs through the whole product: **a proof is
valid only when the function returns.** There is no "valid enough", no partial
result, no falsy return — every failure raises. A caller that catches nothing is
already failing closed. The subclasses exist so that an auditor can tell a
forgery attempt from an expired token or a stale key set.

The names below are not a convention the two SDKs happen to share: which class
comes out for a given token is pinned by
[shared vectors](../packages/descriptors/testdata/act-verification-vectors.json)
that both suites replay. An auditor checking a token in Node and a customer
checking the same token in Python must reach the same verdict, or the proof is
an opinion about which library you installed.

| Error | Raised when | What it usually means |
| --- | --- | --- |
| `ActMalformedError` | not a three-part JWS; base64url or JSON that will not decode; no `kid`; a `crit` header; a mandatory claim missing or of the wrong type; `mode` outside `test` / `live` (or missing without the explicit legacy option); a `request_hash` that is not `sha256:<64 hex>`; `exp` not after `iat` or more than 3600 seconds after it | the token was truncated, re-encoded or is not an ACT at all |
| `ActAlgorithmError` | `alg` is not `ES256` | an algorithm-confusion forgery attempt (`none`, `HS256`). Worth logging when it repeats |
| `ActTypeMismatchError` | `typ` is not `act+jwt` | token substitution — a UBT (`ubt+jwt`) passed where an ACT was expected |
| `ActKeyNotFoundError` | no key in the JWKS matches the `kid`, or the match cannot verify ES256 (wrong `kty`/`crv`, encryption-only, unusable coordinates) | your JWKS copy predates a key rotation. Refresh it |
| `ActPrivateKeyInKeySetError` | a key in the set carries **private** material (`d`, `k`) | an incident, not a retry — see below |
| `ActSignatureError` | the signature does not match header+payload | the token was altered after signing, or never signed by that key. Never retry into acceptance |
| `ActExpiredError` | past `exp`, beyond the leeway | tokens live one hour. Auditing an archived proof? Pass `now` / `now=` — the instant you are auditing — instead of the wall clock |
| `ActNotYetValidError` | `iat` or `nbf` in the future, beyond the leeway | clock skew. Check the verifying machine's clock before suspecting the token |
| `ActIssuerError` | `iss` is not an accepted issuer | a `test.idside.eu` token — from a deployment of the cloud that is not the production service — verified without `allow_test_issuer` / `allowTestIssuer`. A test key's proof from the production service is signed `idside.eu` and does not raise this: read `proof.mode` |
| `ActBindingMismatchError` | a pinned `request_hash`, `nonce`, `sub` or `app_id` does not match | **one token = one request** — the proof belongs to a different call, user or app |
| `ActVerificationUnavailableError` | the ES256 backend is missing (Python: `pip install 'idside[verify]'`); the JWKS URL cannot be fetched or returns something unusable; two keys share a `kid` | verification could not be performed — which is never the same as "valid" |

### Why a private key in the JWKS gets its own class

`ActPrivateKeyInKeySetError` is deliberately **not** a subclass of
`ActKeyNotFoundError`, even though both are about the key set, because the two
call for opposite responses.

A `kid` that is missing means "your copy of the key set is stale" — and
`except ActKeyNotFoundError: refresh_jwks_and_retry()` is the natural handler.
A key set carrying private material means the opposite: stop, do not retry,
escalate. If the second were catchable by the first, that handler would hammer
the JWKS endpoint through exactly the incident it should be reporting. The class
is the contract callers write against, so the two are separate classes rather
than one class with a different message.

A JWKS is public by definition, so private material in one means either a signing
key was published by mistake or the key set was poisoned. What to do: do not
"clean" the key set and carry on — **rotate the key**, and treat everything it
signed as forgeable. Checked before anything else about that key, so it takes
precedence over "wrong `kty`", "wrong curve" and the rest.

### Caller mistakes are not verification failures

Passing neither or both key sources, a non-`https` JWKS URL, a negative leeway,
a non-finite `now`, or an empty issuer set is a bug in your call, not a verdict
on the token. Those raise `ValueError` in Python and `TypeError` in Node.js —
deliberately outside
the `ActVerificationError` hierarchy, so `except ActVerificationError` /
`catch (e instanceof ActVerificationError)` never swallows them into a
"the proof is invalid" branch.

```python
from idside import ActBindingMismatchError, ActVerificationError, verify_act

try:
    proof = verify_act(token, jwks=jwks, request_hash=expected_hash)
except ActBindingMismatchError:
    audit_log.error("proof does not belong to this request")
except ActVerificationError as exc:
    audit_log.error("proof rejected: %s", exc)
else:
    audit_log.info("verified %s %s (%s)", proof.provider, proof.model, proof.binding)
```

See the quickstarts for the full call signatures:
[Python](./quickstart-python.md#7-verify-a-proof-yourself) ·
[Node.js](./quickstart-node.md#7-verify-a-proof-yourself).

---

## Reading the message

When the cloud returns an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)
`problem+json` body, both SDKs append its `title` and `detail` to the exception
message:

```
ID Side plan quota exceeded (HTTP 402). Upgrade the plan or wait for the quota
window to reset; the AI call was not made. Cloud says: Quota exceeded; 50000/50000
authorize calls used this month.
```

Log the full message. It is the difference between "402" and knowing which quota,
on which plan, ran out.

## Next

- [FAQ](./faq.md) — what happens if ID Side is down, quota exhaustion, latency
- [Concepts](./concepts.md) — why fail-closed is the default
- [Python quickstart](./quickstart-python.md) · [Node.js quickstart](./quickstart-node.md)
