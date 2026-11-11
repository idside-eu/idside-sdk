# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Exception hierarchy for the ID Side SDK.

Every error below inherits :class:`IDSideError`, with one deliberate exception:
:class:`UnenforcedCallError` derives from ``BaseException`` so a provider
library's blanket ``except Exception:`` cannot swallow a security stop. None of
them is ever a signal to call the AI provider anyway: the SDK is fail-closed, so
when authorization did not succeed, the AI call simply does not happen.
"""

from __future__ import annotations


class IDSideError(Exception):
    """Base class for all ID Side SDK errors."""


class ConsentUnreachableError(IDSideError):
    """Consent could not be verified: cloud unreachable or answer unusable.

    Raised on network failures, timeouts, 5xx responses, undetermined 4xx, and
    on 200 responses whose body is malformed (the SDK never guesses a
    decision). In line with the *fail-closed* principle, the SDK then makes NO
    call to the AI provider. What to do: treat it as a transient outage —
    retry later or degrade your product flow; if it persists, check your
    network path to sdk.idside.eu.
    """


class ConsentDeniedError(IDSideError):
    """The requested AI usage was denied (HTTP 403).

    ``reason`` distinguishes the deny motive when the cloud provides one:

    - ``"fraud_suspended"`` — this app is suspended by fraud detection; contact
      ID Side support through the dashboard.
    - ``"organization_suspended"`` — the whole account behind the app is
      stopped. Billing or compliance, not your app: it is not the same person
      you talk to, which is why it is not the same reason.
    - ``"policy_hold"`` — when the organization's policy cannot apply to a
      call (a provider with no such setting, a provider the organization has
      not allowed, an employee who is not identified), Observe lets the call
      through and flags it, Enforce holds it; this is Enforce's hold. No option
      in your code lets a held call out: the provider, endpoint, model and
      identifier the call names, and the credentials you label, are your
      code's to change, the policy is its owner's.
    - ``None`` — a 403 whose reason this SDK does not qualify. Today that is a
      secret key minted without the ``authorize`` scope; the message carries
      the cloud's detail, which names the missing scope.

    A user's own refusal never arrives here, and neither does their silence:
    both resolve to an authorization that SUCCEEDS and carries the refusals —
    forced provider parameters, :class:`ToolUseRefusedError` where the only
    lever is the call itself, and :class:`MissingCredentialError` where the
    refusal names a credential posture nobody provisioned.

    Either way, do not retry, and do not route around it: surface the denial in
    your product flow.
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class ToolUseRefusedError(IDSideError):
    """The call carries agentic tools, and this user has not accepted them.

    Not an outage and not a bug: it is the user's answer — or the silence that
    counts as one — arriving at the one place it can be applied. Every
    provider's zero-retention arrangement excludes agentic features while the
    API itself does not block them, so a call that hands the model tools falls
    outside the retention regime the user was promised — and the SDK is the only
    component that can see it happening, because it is the only one that sees
    the outgoing request.

    Three situations raise it. The user refused agentic tools, or nobody ever
    put the question to them: what nobody accepted is refused, so an option a
    person never answered is ``False`` before anything is enforced or signed. An
    integration that passes tools and runs no Preference Center therefore raises
    this on every call, for every user — the mechanism asking to be asked, not a
    misconfiguration to work around. Or an organization policy refuses them,
    which no answer in the widget can lift: a policy only tightens.

    Refusing rather than silently dropping the tools is deliberate: an agent
    stripped of its tools does not fail, it answers wrongly, and the developer
    would have no way to know. What to do: retry without the tools, or route the
    user to a non-agentic path; for a visitor nobody asked, run the widget so
    there is an answer to get. Never retry the same call, it will be refused
    again. The message says the same whoever refused: the SDK cannot tell.
    """


class UnenforceableOptionError(IDSideError):
    """The call passes an option that would undo what the SDK enforces on it.

    The SDK authorizes a request, forces the parameters the user's answer
    requires, and the cloud signs a proof describing that request. Some options
    of the provider libraries act *after* all of that: ``extra_body`` rewrites
    the body (``store``, ``tools``, even ``model``), ``extra_query`` and
    ``server_url`` or ``http_options.base_url`` change where the request goes, a
    header such as ``Authorization`` or ``X-Api-Key`` changes the credential it
    goes out on. Any of them makes the request that leaves the process a
    different one from the request that was proven — and nothing would record
    the difference. So they are refused, before the cloud is asked: no
    authorization, no ledger entry, no AI call.

    The list is closed. An option is let through only if it is known to change
    nothing a proof covers — a timeout, a retry policy, a header that selects no
    credential — so an option a provider library adds tomorrow is refused until
    someone has looked at it. Google's automatic function calling is refused
    for a related reason: it would send several requests under a single ACT
    token.

    The message names the option or the header, never its value, which may be a
    key. What to do: pass the parameter as a named argument of the call, where
    it is part of the request that is authorized and hashed; set credentials
    and the destination on the client when you build it, or through
    ``consent.wrap(credentials=…)`` for routing; and for Gemini, declare
    functions and run the tool loop yourself, so each turn is authorized and
    proven.
    """


class CredentialRoutingError(IDSideError):
    """Credential routing is configured in a way the SDK cannot honour.

    Routing is how an account-level privacy setting becomes a per-user one: no
    provider exposes training opt-out as a request parameter, but the setting
    lives on the credential (a project at OpenAI and Gemini, a workspace at
    Anthropic, an organisation at Mistral), so a developer who provisions one
    credential per posture lets the SDK send each call on the one matching that
    user's answer.

    This error means the ``credentials=`` mapping given to ``consent.wrap()``
    cannot do that: the provider declares no routing at all, a bare identifier
    was given where only a second client will do, an identifier is not of the
    shape the provider issues, or an alternative client does not expose the call
    site being wrapped. All of them are configuration mistakes, raised at
    ``wrap()`` time where they can still be fixed — except the last resort check
    that a routed credential really reached the outgoing request, which fails
    the call rather than let it leave on the wrong one.
    """


class MissingCredentialError(CredentialRoutingError):
    """This user's answer needs a credential the developer has not provisioned.

    The cloud resolved the user's preferences and named the posture this call
    must go out on; nothing in ``credentials=`` carries that posture. The SDK
    refuses the call rather than send it on the credential it happens to hold,
    which would quietly place the user under a privacy regime they declined —
    and leave no trace that it happened.

    ``posture`` is what was required, ``provider`` who for, and ``provisioned``
    the labels that do exist. What to do: provision a credential for that
    posture (the message names the unit the provider uses) and add it to
    ``credentials=`` under that exact label. Never a retry: the answer will be
    the same until the configuration changes.
    """

    def __init__(
        self,
        message: str,
        *,
        posture: str,
        provider: str,
        provisioned: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.posture = posture
        self.provider = provider
        self.provisioned = provisioned


class AuthenticationError(IDSideError):
    """The ID Side API rejected the credentials (HTTP 401).

    A configuration problem, not an outage. What to do: check the ``api_key``
    passed to ``consent.wrap()`` (or your key management) against the keys
    listed in the ID Side dashboard; make sure you are not sending a test key
    against production or vice versa. Retrying without fixing the key is
    pointless.

    Two 401s are about the visitor rather than the key, and raise a
    :class:`BindingError` subclass instead: :class:`InvalidBindingError` and
    :class:`BindingRequiredError`.
    """


class BindingError(AuthenticationError):
    """The cloud refused how this call named its visitor (HTTP 401), not the key.

    The secret key was accepted; the User Binding Token (UBT) was not, or there
    was none where the application requires one. The cure is the widget, never
    the key, and never a fallback to ``user_id``: catch this class before
    :class:`AuthenticationError` so a binding problem does not page whoever
    owns the keys. It stays an :class:`AuthenticationError`, so a handler
    written before it existed still catches it.
    """


class InvalidBindingError(BindingError):
    """The visitor's UBT no longer verifies (HTTP 401, ``ubt_invalid``).

    It expired — a UBT lives 24 hours, so every visitor who comes back the next
    day with a server session still open produces this — or it was issued for
    another application, was altered, or is not a UBT at all. What to do:
    discard the UBT stored in this visitor's session, stop the AI feature, and
    let the widget issue a new one (``IDSide.showPreferences()`` then *Confirm
    these choices* renews it without changing any choice). Retrying with the
    same token cannot succeed.
    """


class BindingRequiredError(BindingError):
    """The application requires a UBT; the call had none (401, ``ubt_required``).

    The application is set to accept only widget-bound identities, and the call
    named its user with ``user_id``. What to do: have the visitor go through the
    widget before any AI call, store the UBT it issues in the server session, and
    pass it as ``ubt``. ``user_id`` is not a fallback for a strict application.
    """


class ConsentRefreshRequiredError(IDSideError):
    """The visitor must review their choices before this call (HTTP 409).

    Raised on ``consent_refresh_required``: the visitor's UBT still verifies,
    but the choices it is bound to no longer cover what this application now
    requires — the policy or the required questions changed since they
    answered, typically because a provider was added. Not an outage, and
    deliberately not a :class:`ConsentUnreachableError`: retrying later
    changes nothing. What to do: stop this AI request and show the visitor the
    widget again; the call can be made once they have recorded their current
    choices. The cloud's detail, appended to the message, says when the widget
    cannot help (a provider ID Side holds no questions for).
    """


class QuotaExceededError(IDSideError):
    """The app's ID Side plan quota is exhausted (HTTP 402).

    There is never a silent degradation: AI calls stop until quota is
    available again. What to do: upgrade the plan or wait for the quota window
    to reset (see the usage page in the ID Side dashboard).
    """


class RateLimitedError(IDSideError):
    """Too many authorize requests in a short window (HTTP 429).

    What to do: slow down and retry after a backoff. If you hit this in
    normal operation, review your call patterns or ask for a higher rate
    limit via the dashboard.
    """


class ContractError(IDSideError):
    """The cloud rejected the request payload as invalid (HTTP 422).

    This indicates an SDK/cloud contract mismatch — most likely an SDK bug or
    a badly outdated SDK, never something end users can trigger. What to do:
    upgrade the ``idside`` package; if the error persists, report it at
    https://github.com/idside-eu/idside-sdk/issues with the error message.
    """


class ConsentTokenError(IDSideError):
    """An ACT token or its request binding is invalid.

    Raised when a token is invalid, expired, replayed, or malformed — and when
    a deterministic ``request_hash`` cannot be computed from the call payload
    (e.g. a value is not JSON-serializable). What to do: for hashing errors,
    pass JSON-compatible values or Pydantic models (``model_dump()``) in the
    provider call; the error message names the offending parameter.
    """


class ActVerificationError(ConsentTokenError):
    """Base class for failures of local ACT verification (:func:`idside.verify_act`).

    Every subclass means the same operational thing: **the proof did not check
    out**, so it must not be accepted as evidence that consent was verified.
    The subclasses exist so an auditor can tell a forgery attempt from an
    expired token or an outdated key set. Catching this class alone is enough
    to fail closed; catch the subclasses when the reason drives the response.
    """


class ActMalformedError(ActVerificationError):
    """The token is not a well-formed ACT: bad JWS structure, or bad claims.

    Covers a value that is not a three-part JWS, base64url that does not decode
    or is not canonical (a segment carrying unused trailing bits, which a
    compliant encoder never produces), JSON that does not parse, a header
    without ``kid``, a missing mandatory claim and a claim of the wrong type.
    What to do: check you passed the raw ``X-AI-Consent-Token`` header value,
    untruncated and unmodified.
    """


class ActAlgorithmError(ActVerificationError):
    """The token's ``alg`` header is not ``ES256``.

    ACT verification pins the algorithm: ``none``, ``HS256`` and every other
    value are rejected outright, never negotiated. A token arriving with
    another algorithm is an algorithm-confusion forgery attempt, not a
    compatibility issue. What to do: do not accept the token; treat repeated
    occurrences as an attack signal worth logging.
    """


class ActTypeMismatchError(ActVerificationError):
    """The token's ``typ`` header is not ``act+jwt``.

    ID Side signs several token types with the same key material — a User
    Binding Token is ``ubt+jwt``. Accepting one where an ACT is expected would
    let a token minted for another purpose stand in as a proof of consent
    (token substitution). What to do: pass the ACT token, not the UBT.
    """


class ActKeyNotFoundError(ActVerificationError):
    """No usable public key matches the token's ``kid`` in the supplied JWKS.

    Also raised when the ``kid`` matches an entry that cannot verify an ES256
    signature (wrong key type, wrong curve, encryption-only, unusable
    coordinates). Verification is fail-closed: no other key is tried. What to
    do: refresh the JWKS from ``/.well-known/jwks.json`` — the signing key may
    have been rotated since your copy was taken.
    """


class ActPrivateKeyInKeySetError(ActVerificationError):
    """The key set contains **private** key material (``d``, ``k``).

    Deliberately **not** a subclass of :class:`ActKeyNotFoundError`, even though
    both are about the key set, because the two call for opposite responses. A
    key that is merely missing says *"refresh the JWKS, it may have rotated"*,
    and ``except ActKeyNotFoundError: refresh_and_retry()`` is the natural
    handler for it. Against a key set carrying a private key, that handler would
    loop — hammering the JWKS through the exact incident it should be escalating.
    The class is the contract a caller writes against; it has to separate the
    two.

    A key set is public by definition, so private material in one means either a
    signing key was published by mistake or the key set was poisoned. Both are
    incidents, and verification is the moment where they can still be caught.
    What to do: do **not** retry. Remove the private members from the JWKS and
    rotate the key — assume anything that key signed can be forged.
    """


class ActSignatureError(ActVerificationError):
    """The ES256 signature does not match the token's header and payload.

    The token was altered after signing, or was never signed by the key it
    claims. What to do: reject it. This is not a transient condition and must
    never be retried into acceptance.
    """


class ActExpiredError(ActVerificationError):
    """The token is past its ``exp`` (plus the allowed clock leeway).

    ACT tokens live one hour: an expired one proves nothing about a call made
    now. What to do: for a live call, obtain a fresh token (the SDK does this
    per request). For an audit of a past call, this is expected — verify the
    signature and the ledger entry instead of the time window.
    """


class ActNotYetValidError(ActVerificationError):
    """The token is not valid yet: ``iat`` or ``nbf`` is in the future.

    Usually a clock skew between the verifier and the ID Side cloud beyond the
    allowed leeway. What to do: check the verifying machine's clock (NTP)
    before suspecting the token.
    """


class ActIssuerError(ActVerificationError):
    """The token's ``iss`` is not an accepted issuer.

    Production proofs are issued by ``idside.eu``. Development tokens
    (``test.idside.eu``) are rejected unless the caller explicitly opts in with
    ``allow_test_issuer=True`` — a dev token must never silently pass as a real
    one. What to do: verify against the environment you meant to audit.
    """


class ActBindingMismatchError(ActVerificationError):
    """A claim the caller pinned does not match the token.

    **One token = one request**: a token whose ``request_hash`` does not match
    the request it is attached to is invalid, however well it is signed. The
    same applies to a pinned ``nonce``, ``sub`` or ``app_id``. What to do:
    reject the proof — it belongs to a different call, user or application.
    """


class ActVerificationUnavailableError(ActVerificationError):
    """Verification could not be performed, so the token is not accepted.

    Raised when the optional ES256 backend is missing (install
    ``idside[verify]``), when the JWKS URL cannot be fetched or returns
    something unusable, and when the JWKS is ambiguous (two keys sharing a
    ``kid``). Fail-closed: "could not check" is never "valid". What to do: fix
    the environment or the key source, then verify again.
    """


class MissingIdentityError(IDSideError):
    """No ``user_id`` and no ``ubt`` was provided for a consent-enforced AI call.

    What to do: pass exactly one of ``user_id="usr_..."`` or ``ubt="eyJ..."``
    on every AI call made through a wrapped client — the SDK consumes it and
    never forwards it to the provider. Use ``ubt`` when your frontend runs the
    ID Side widget (strongest identity binding); use ``user_id`` for
    backend-only integrations without the widget.
    """


class ConflictingIdentityError(IDSideError):
    """Both ``user_id`` and ``ubt`` were provided for the same AI call.

    The two identity forms are exclusive: the User Binding Token already names
    the user (it is a JWT signed by ID Side, bound to user, app, session and
    consent state), so a separate ``user_id`` alongside it is at best redundant
    and at worst a spoofing attempt the cloud would have to arbitrate. What to
    do: pass ``ubt`` alone when you have the widget, ``user_id`` alone when
    you do not.
    """


class UnenforcedCallError(BaseException):
    """A request was about to leave a wrapped client without consent enforcement.

    The SDK patches the endpoints its provider descriptor declares, and that can
    never be exhaustive: a provider ships a new endpoint, a helper posts to the
    API directly, a resource nobody has wrapped yet (``embeddings``, ``images``,
    ``audio``, ``moderations``, ``batches``, ``files``, ``beta.*``) is reachable
    from the same client. Every one of those would otherwise reach the provider
    with no authorize, no ACT token and no ledger entry — silently, which is the
    only failure mode this product cannot have. A guard on the client's HTTP
    transport catches them and refuses the request instead.

    The guard is a detector, not an enforcer, and it is deliberately narrow: it
    reads the HTTP method, URL path and ACT header. The header must match private
    authorization provenance for this wrapped client and route. It never reads
    the body, query string or another header, and hashes, logs and sends nothing.
    ``GET``,
    ``HEAD``, ``OPTIONS`` and ``DELETE`` are allowed through: they carry no
    prompt to infer on and are the management surface (``models.list``,
    ``files.delete``, polling a response id).

    What to do: an endpoint that carries no inference does not belong on a
    consent-enforced client. Build a **second, unwrapped client** for that
    traffic. There is no bypass flag on the wrapped one and there will not be:
    it would be the *opaque tunnel* invariant defeated through the back door.
    If the endpoint *is* an inference endpoint, it needs a call site in the
    provider descriptor — please report it.

    **Deliberately not an** :class:`IDSideError`, and not even an ``Exception``.
    ``openai._base_client`` wraps any ``Exception`` raised while sending into an
    ``APIConnectionError`` *and retries it*, which would turn a consent refusal
    into a misleading, repeatedly retried network error. A blanket
    ``except Exception:`` in a provider library must not be able to swallow a
    security stop, so catch this class by name.
    """


class UnsupportedProviderError(IDSideError):
    """No ID Side adapter is registered for the given provider client.

    What to do: check the provider is supported (or OpenAI-compatible), or
    register a custom adapter with ``idside.adapters.register_adapter()``.
    """
