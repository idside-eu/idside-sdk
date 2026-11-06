// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Exception hierarchy for the ID Side SDK.
 *
 * Every error below inherits {@link IDSideError}. None of them is ever a signal
 * to call the AI provider anyway: the SDK is fail-closed, so when authorization
 * did not succeed, the AI call simply does not happen.
 */

export class IDSideError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = new.target.name;
  }
}

/**
 * Consent could not be verified: cloud unreachable or answer unusable.
 *
 * Raised on network failures, timeouts, 5xx responses, undetermined 4xx, and on
 * 200 responses whose body is malformed (the SDK never guesses a decision). In
 * line with the fail-closed principle, the SDK then makes NO call to the AI
 * provider.
 */
export class ConsentUnreachableError extends IDSideError {}

/**
 * The requested AI usage was denied (HTTP 403).
 *
 * `reason` distinguishes the deny motive when the cloud provides one:
 *
 * - `"fraud_suspended"` — this app is suspended by fraud detection; contact ID
 *   Side support through the dashboard.
 * - `"organization_suspended"` — the whole account behind the app is stopped.
 *   Billing or compliance, not your app: it is not the same person you talk to,
 *   which is why it is not the same reason.
 * - `"policy_hold"` — when the organization's policy cannot apply to a call (a
 *   provider with no such setting, a provider the organization has not
 *   allowed, an employee who is not identified), Observe lets the call through
 *   and flags it, Enforce holds it; this is Enforce's hold. No option in your
 *   code lets a held call out: the provider, endpoint, model and identifier
 *   the call names, and the credentials you label, are your code's to change,
 *   the policy is its owner's.
 * - `undefined` — a 403 whose reason this SDK does not qualify. Today that is a
 *   secret key minted without the `authorize` scope; the message carries the
 *   cloud's detail, which names the missing scope.
 *
 * A user's own refusal never arrives here, and neither does their silence: both
 * resolve to an authorization that SUCCEEDS and carries the refusals — forced
 * provider parameters, {@link ToolUseRefusedError} where the only lever is the
 * call itself, and {@link MissingCredentialError} where the refusal names a
 * credential posture nobody provisioned.
 *
 * Either way, do not retry, and do not route around it.
 */
export class ConsentDeniedError extends IDSideError {
  readonly reason: string | undefined;
  constructor(message: string, options: { reason?: string } = {}) {
    super(message);
    this.reason = options.reason;
  }
}

/**
 * Credential routing is configured in a way the SDK cannot honour.
 *
 * Routing is how an account-level privacy setting becomes a per-user one: no
 * provider exposes training opt-out as a request parameter, but the setting
 * lives on the credential (a project at OpenAI and Gemini, a workspace at
 * Anthropic, an organisation at Mistral), so a developer who provisions one
 * credential per posture lets the SDK send each call on the one matching that
 * user's answer.
 *
 * This means the `credentials` mapping given to `consent.wrap()` cannot do that:
 * the provider declares no routing at all, a bare identifier was given where
 * only a second client will do, an identifier is not of the shape the provider
 * issues, or an alternative client does not expose the call site being wrapped.
 * All configuration mistakes, thrown at `wrap()` time where they can still be
 * fixed — except the last-resort check that a routed credential really reached
 * the outgoing request, which fails the call rather than let it leave on the
 * wrong one.
 */
export class CredentialRoutingError extends IDSideError {}

/**
 * This user's answer needs a credential the developer has not provisioned.
 *
 * The cloud resolved the user's preferences and named the posture this call must
 * go out on; nothing in `credentials` carries that posture. The SDK refuses the
 * call rather than send it on the credential it happens to hold, which would
 * quietly place the user under a privacy regime they declined — and leave no
 * trace that it happened.
 *
 * `posture` is what was required, `provider` who for, and `provisioned` the
 * labels that do exist. Provision a credential for that posture (the message
 * names the unit the provider uses) and add it under that exact label. Never a
 * retry: the answer stays the same until the configuration changes.
 */
export class MissingCredentialError extends CredentialRoutingError {
  readonly posture: string;
  readonly provider: string;
  readonly provisioned: readonly string[];
  constructor(
    message: string,
    options: { posture: string; provider: string; provisioned?: readonly string[] },
  ) {
    super(message);
    this.posture = options.posture;
    this.provider = options.provider;
    this.provisioned = options.provisioned ?? [];
  }
}

/**
 * The ID Side API rejected the credentials (HTTP 401). A config problem, not an
 * outage: check the `apiKey` passed to `consent.wrap()` against the dashboard.
 *
 * Two 401s are about the visitor rather than the key, and throw a
 * {@link BindingError} subclass instead: {@link InvalidBindingError} and
 * {@link BindingRequiredError}.
 */
export class AuthenticationError extends IDSideError {}

/**
 * The cloud refused how this call named its visitor (HTTP 401), not the key.
 *
 * The secret key was accepted; the User Binding Token (UBT) was not, or there
 * was none where the application requires one. The cure is the widget, never
 * the key, and never a fallback to `user_id`: test for this class before
 * {@link AuthenticationError} so a binding problem does not page whoever owns
 * the keys. It stays an `AuthenticationError`, so a handler written before it
 * existed still catches it.
 */
export class BindingError extends AuthenticationError {}

/**
 * The visitor's UBT no longer verifies (HTTP 401, `ubt_invalid`).
 *
 * It expired — a UBT lives 24 hours, so every visitor who comes back the next
 * day with a server session still open produces this — or it was issued for
 * another application, was altered, or is not a UBT at all. Discard the UBT
 * stored in this visitor's session, stop the AI feature, and let the widget
 * issue a new one (`IDSide.showPreferences()` then *Confirm these choices*
 * renews it without changing any choice). Retrying with the same token cannot
 * succeed.
 */
export class InvalidBindingError extends BindingError {}

/**
 * This application requires a UBT and the call had none (HTTP 401,
 * `ubt_required`).
 *
 * The application is set to accept only widget-bound identities, and the call
 * named its user with `user_id`. Have the visitor go through the widget before
 * any AI call, store the UBT it issues in the server session, and pass it as
 * `ubt`. `user_id` is not a fallback for a strict application.
 */
export class BindingRequiredError extends BindingError {}

/**
 * The visitor must review their choices before this call (HTTP 409).
 *
 * Raised on `consent_refresh_required`: the visitor's UBT still verifies, but
 * the choices it is bound to no longer cover what this application now
 * requires — the policy or the required questions changed since they answered,
 * typically because a provider was added. Not an outage, and deliberately not a
 * {@link ConsentUnreachableError}: retrying later changes nothing. Stop this AI
 * request and show the visitor the widget again; the call can be made once they
 * have recorded their current choices. The cloud's detail, appended to the
 * message, says when the widget cannot help (a provider ID Side holds no
 * questions for).
 */
export class ConsentRefreshRequiredError extends IDSideError {}

/** The app's ID Side plan quota is exhausted (HTTP 402). AI calls stop until quota returns. */
export class QuotaExceededError extends IDSideError {}

/** Too many authorize requests in a short window (HTTP 429). Slow down and back off. */
export class RateLimitedError extends IDSideError {}

/** The cloud rejected the request payload as invalid (HTTP 422): an SDK/cloud contract mismatch. */
export class ContractError extends IDSideError {}

/**
 * An ACT token or its request binding is invalid, or a deterministic
 * `request_hash` cannot be computed from the call payload (e.g. a value is not
 * JSON-serializable). The error message names the offending parameter.
 */
export class ConsentTokenError extends IDSideError {}

/** No `user_id` and no `ubt` was provided for a consent-enforced AI call. */
export class MissingIdentityError extends IDSideError {}

/**
 * Both `user_id` and `ubt` were provided for the same AI call. The two identity
 * forms are exclusive: the UBT already names the user, so a separate `user_id`
 * alongside it is at best redundant and at worst a spoofing attempt.
 */
export class ConflictingIdentityError extends IDSideError {}

/** No ID Side adapter is registered for the given provider client. */
export class UnsupportedProviderError extends IDSideError {}

/**
 * A request was about to leave a wrapped client without consent enforcement.
 *
 * `wrap()` patches the endpoints the descriptor declares, which can never be
 * exhaustive: a provider ships new resources, a helper posts straight to the
 * transport, a derived client is built from another one. This error is the net
 * under that: the SDK watches the client's own HTTP transport and refuses any
 * body-bearing request (POST/PUT/PATCH) whose ACT token does not match private
 * authorization provenance for this wrapped client and route. The request is
 * stopped *before* it goes out.
 *
 * The check is deliberately narrow. It reads the HTTP method, URL path and ACT
 * header, comparing that value locally with the private permit. It never reads
 * the body, query string or another header, and logs, hashes and sends nothing.
 *
 * `GET`, `HEAD`, `OPTIONS` and `DELETE` are let through: they carry no prompt to
 * infer on and are the management surface (`models.list`, `files.delete`,
 * polling a response). Everything else is an inference call until proven
 * otherwise — `embeddings`, `images`, `audio`, `moderations`, `batches`, file
 * uploads, `beta.*` and any endpoint invented after this release now fail loudly
 * instead of escaping silently.
 *
 * The remedy is a second, **unwrapped** client for non-inference traffic. There
 * is no option to let a call out of a wrapped one, and there will not be: a
 * bypass flag is the opaque-tunnel invariant through the back door.
 *
 * Note the one honest asymmetry with the Python SDK, where this class derives
 * from `BaseException` so a provider's blanket `except Exception:` cannot
 * swallow a security stop. Node has no equivalent, so this extends
 * {@link IDSideError} like everything else — and `openai-node` will wrap it in
 * an `APIConnectionError` whose `cause` is this error. Inspect `cause`.
 */
export class UnenforcedCallError extends IDSideError {}

/**
 * The call carries agentic tools, and this user has not accepted them.
 *
 * Not an outage and not a bug: it is the user's answer — or the silence that
 * counts as one — arriving at the one place it can be applied. Every provider's
 * zero-retention arrangement excludes agentic features while the API itself
 * does not block them, so a call that hands the model tools falls outside the
 * retention regime the user was promised — and the SDK is the only component
 * that can see it happening, because it is the only one that sees the outgoing
 * request.
 *
 * Three situations raise it. The user refused agentic tools, or nobody ever put
 * the question to them: what nobody accepted is refused, so an option a person
 * never answered is `false` before anything is enforced or signed. An
 * integration that passes tools and runs no Preference Center therefore raises
 * this on every call, for every user — the mechanism asking to be asked, not a
 * misconfiguration to work around. Or an organization policy refuses them,
 * which no answer in the widget can lift: a policy only tightens.
 *
 * Refusing rather than silently dropping the tools is deliberate: an agent
 * stripped of its tools does not fail, it answers wrongly, and the developer
 * would have no way to know. What to do: retry without the tools, or route the
 * user to a non-agentic path; for a visitor nobody asked, run the widget so
 * there is an answer to get. Never retry the same call, it will be refused
 * again. The message says the same whoever refused: the SDK cannot tell.
 */
export class ToolUseRefusedError extends IDSideError {}

/**
 * The call passes an option the SDK cannot enforce, so it was not made.
 *
 * Every provider library accepts, next to the request body, a second set of
 * per-request options — and several of them undo what enforcement just did
 * without touching the body the SDK hashed: `options.body` replaces it,
 * `fetchOptions` and `middleware` rewrite the request on its way out, `path`,
 * `serverURL` and `httpOptions.baseUrl` send it to another server, an
 * `Authorization` or `OpenAI-Project` header moves it onto another credential.
 * The proof would then describe a request that never went out. So the list is
 * closed: timeouts, retries, cancellation, idempotency keys and headers that do
 * not choose a credential pass, and everything else is refused — including an
 * option a future library version adds, until this SDK knows what it does.
 *
 * Raised before anything happens: the ID Side cloud is not asked, nothing is
 * logged in the ledger, the provider is not called. The message names the
 * option or header, never its value — a header value is often a key.
 *
 * It is also raised for an executable Gemini tool (anything with `callTool`,
 * `mcpToTool()` included), for a reason of its own: the library would call the
 * model again by itself, up to ten requests under one authorization, and one
 * ACT token covers exactly one request. Declare the function instead and run
 * the loop yourself, so that every turn is authorized and proved.
 *
 * Not a retry candidate: the same call fails the same way. Move request
 * parameters into the body, configure credentials and base URLs on the client
 * — or route postures with `consent.wrap({ credentials })` — and send the call
 * again without the option.
 */
export class UnenforceableOptionError extends IDSideError {}

/**
 * Base class for failures of local ACT verification ({@link verifyAct}).
 *
 * Every subclass means the same operational thing: **the proof did not check
 * out**, so it must not be accepted as evidence that consent was verified. The
 * subclasses exist so an auditor can tell a forgery attempt from an expired
 * token or an outdated key set. Catching this class alone is enough to fail
 * closed; catch the subclasses when the reason drives the response.
 */
export class ActVerificationError extends ConsentTokenError {}

/**
 * The token is not a well-formed ACT: bad JWS structure, or bad claims.
 *
 * Covers a value that is not a three-part JWS, base64url or JSON that does not
 * decode, a header without `kid` or carrying `crit`, a missing mandatory claim
 * and a claim of the wrong type. Check you passed the raw
 * `X-AI-Consent-Token` header value, untruncated and unmodified.
 */
export class ActMalformedError extends ActVerificationError {}

/**
 * The token's `alg` header is not `ES256`.
 *
 * Verification pins the algorithm: `none`, `HS256` and every other value are
 * rejected outright, never negotiated. A token arriving with another algorithm
 * is an algorithm-confusion forgery attempt, not a compatibility issue — worth
 * logging when it repeats.
 */
export class ActAlgorithmError extends ActVerificationError {}

/**
 * The token's `typ` header is not `act+jwt`.
 *
 * ID Side signs several token types with the same key material — a User Binding
 * Token is `ubt+jwt`. Accepting one where an ACT is expected would let a token
 * minted for another purpose stand in as a proof of consent (token
 * substitution).
 */
export class ActTypeMismatchError extends ActVerificationError {}

/**
 * No usable public key matches the token's `kid` in the supplied JWKS.
 *
 * Also raised when the `kid` matches an entry that cannot verify an ES256
 * signature (wrong curve, encryption-only, unusable coordinates, a key type
 * that is not EC). Verification is fail-closed: no other key is tried. Refresh
 * the JWKS from `/.well-known/jwks.json` — the signing key may have been
 * rotated. Refreshing and retrying is the right reflex for everything this
 * class covers; a key set holding private material is deliberately **not** one
 * of them (see {@link ActPrivateKeyInKeySetError}).
 */
export class ActKeyNotFoundError extends ActVerificationError {}

/**
 * The key set carries private key material: an EC/RSA private scalar (`d`) or a
 * symmetric key (`k`), where only public verification material belongs.
 *
 * The token itself may be perfectly valid — the **key set** is not. Either a
 * signing key was published by mistake, or the set was poisoned. Rotate the
 * signing key now and treat the set as compromised; do not retry.
 *
 * Deliberately **not** a subclass of {@link ActKeyNotFoundError}, because the
 * two remediations are opposites. A missing `kid` means "refresh the key set,
 * it may have rotated" — the natural handler for that class refreshes and
 * retries, which against a poisoned key set loops during the exact incident it
 * should be escalating. Inheriting would let that handler catch this anyway,
 * and a doc comment cannot reach a handler somebody already wrote.
 */
export class ActPrivateKeyInKeySetError extends ActVerificationError {}

/**
 * The ES256 signature does not match the token's header and payload.
 *
 * The token was altered after signing, or was never signed by the key it
 * claims. Not a transient condition: it must never be retried into acceptance.
 */
export class ActSignatureError extends ActVerificationError {}

/**
 * The token is past its `exp` (plus the allowed clock leeway).
 *
 * ACT tokens live one hour: an expired one proves nothing about a call made
 * now. When auditing a past call this is expected — verify the signature and
 * the ledger entry rather than the time window.
 */
export class ActExpiredError extends ActVerificationError {}

/**
 * The token is not valid yet: `iat` or `nbf` is in the future.
 *
 * Usually a clock skew between the verifier and the ID Side cloud beyond the
 * allowed leeway. Check the verifying machine's clock before suspecting the
 * token.
 */
export class ActNotYetValidError extends ActVerificationError {}

/**
 * The token's `iss` is not an accepted issuer.
 *
 * Production proofs are issued by `idside.eu`. Development tokens
 * (`test.idside.eu`) are rejected unless the caller explicitly opts in — a dev
 * token must never silently pass as a real one.
 */
export class ActIssuerError extends ActVerificationError {}

/**
 * A claim the caller pinned does not match the token.
 *
 * **One token = one request**: a token whose `request_hash` does not match the
 * request it is attached to is invalid, however well it is signed. The same
 * applies to a pinned `nonce`, `sub` or `app_id` — the proof belongs to a
 * different call, user or application.
 */
export class ActBindingMismatchError extends ActVerificationError {}

/**
 * Verification could not be performed, so the token is not accepted.
 *
 * Raised when the JWKS URL cannot be fetched or returns something unusable, and
 * when the key set is ambiguous (two keys sharing a `kid`). Fail-closed:
 * "could not check" is never "valid".
 */
export class ActVerificationUnavailableError extends ActVerificationError {}
