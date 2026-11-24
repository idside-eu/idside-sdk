# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""The organization's prompt-cleaning policy, as signed by the cloud, applied here.

``GET /v1/content-policy`` answers ``{"enabled": false}`` when the application
cleans nothing, or the policy as a compact JWS (ES256, ``typ:
content-policy+jwt``), a challenge, and how long to keep it. The SDK applies a
policy only after checking, before any AI call relies on it:

- the signature, under the key the JWS names in ``/.well-known/jwks.json`` —
  the key set ACT tokens are verified with — and that this key is a policy key:
  the cloud names each signing key after its purpose (``content-policy-g1``,
  ``act-g1``…), so a key that signs proofs never signs what a call is cleaned
  by. The check is ``cryptography``'s when the ``[verify]`` extra is installed,
  and :mod:`idside._p256`'s otherwise, so a bare install verifies too;
- ``typ``, ``alg``, ``iss`` (an ID Side issuer), ``exp`` and ``iat``;
- that ``digest`` is the SDK's own RFC 8785 digest of ``policy``, and that
  ``app_id`` is the policy's.

Any failure is a :class:`ContentPolicyError` and the AI call is not made: a
policy that does not check out is never replaced by no policy. A cloud that
cannot be reached, or answers an error, is :class:`ConsentUnreachableError` —
unless the policy already held is still within its validity, in which case it
is kept until it is not. That validity is ``exp - iat`` seconds from the
policy's reception, counted on the monotonic clock, and never past ``exp`` by
the wall clock: a machine running behind keeps it no longer than the cloud
meant.

Nothing here reads or sends a prompt. The challenge is answered from the
cloud's own texts, cleaned under the reference policy (see ``_cleaning``).
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from idside import _p256
from idside._cleaning import (
    ENGINE_VERSION,
    ContentCleaner,
    build_digest,
    challenge_response,
    content_policy_digest,
    rules_digest,
)
from idside.exceptions import (
    ActVerificationError,
    ConsentUnreachableError,
    ContentPolicyError,
)
from idside.providers import ContentFields


# `idside.verify` imports the transport, which imports this module: its helpers
# are read at call time, once both are loaded.
def _verify() -> Any:
    from idside import verify

    return verify


CONTENT_POLICY_PATH = "/v1/content-policy"
CONTENT_EVENTS_PATH = "/v1/content-events"
JWKS_PATH = "/.well-known/jwks.json"
POLICY_TYPE = "content-policy+jwt"
#: How the cloud names a key of the policy purpose (``content-policy-g1``, with
#: ``-staging`` or ``-dev`` outside production). One key set holds every
#: purpose; this prefix is what tells them apart.
POLICY_KEY_PREFIX = "content-policy-"
#: An ES256 signature is r‖s, 32 bytes each.
SIGNATURE_BYTES = 64
#: Seconds a clock may be ahead of the cloud's when reading ``iat``.
CLOCK_SKEW_SECONDS = 300
#: How long a disabled answer stands when it cannot be refreshed: the window
#: a signed policy has (``exp`` is ``iat`` + 900 s).
DISABLED_VALIDITY_SECONDS = 900
#: While a valid policy is held, the cloud is asked for a fresh one at most
#: once per this many seconds — whether the last attempt succeeded or failed,
#: and whatever an authorize answer says.
MIN_REFRESH_INTERVAL_SECONDS = 10
DEFAULT_REFRESH_AFTER_SECONDS = 300
#: A 429 on the policy route while no valid policy is held is retried once,
#: after the ``Retry-After`` the cloud sent — delta-seconds only — capped here.
RETRY_AFTER_CAP_SECONDS = 2.0
_DELTA_SECONDS = re.compile(r"[0-9]+")
#: The report of a blocked call is best effort: it waits this long at most.
EVENTS_TIMEOUT_SECONDS = 2.0
#: What ``POST /v1/content-events`` keeps as a provider's name (``openai``).
EVENT_PROVIDER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
#: What it keeps as a model: 1 to 200 printable characters, no blank at
#: either end. ASCII only — a subset of what the service reads as printable,
#: so that no Unicode table, in either SDK or in the service, can disagree.
EVENT_MODEL = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]{0,198}[\x21-\x7e])?")
#: The statuses of an authorize answer that ask for a fresh policy.
REFRESH_STATUSES = frozenset({"outdated", "not_applied", "anomaly"})


def _refuse(what: str) -> ContentPolicyError:
    return ContentPolicyError(
        f"The prompt-cleaning policy from the ID Side cloud {what}; "
        "the AI call was not made."
    )


def _out_of_step(where: str, cause: str, check: str) -> ContentPolicyError:
    """A policy that checks out but not by this machine's clock: say what to check.

    Only ever raised for a policy just received, once its signature, issuer,
    digest and application have checked out — so the date is what is wrong:
    this machine's clock, or, for an answer already expired, a cache that
    served an old one. Names no value of the policy; Node's ``outOfStep()``
    says the same words.
    """
    return ContentPolicyError(
        "The prompt-cleaning policy just received from the ID Side cloud is "
        f"signed and intact, but this machine's clock puts it {where}: {cause}. "
        f"Check that this machine's clock is set (NTP){check}; the AI call was "
        "not made."
    )


@dataclass(frozen=True)
class PolicyState:
    """One verified answer of ``/v1/content-policy``, ready to apply.

    ``cleaner`` is ``None`` when the application cleans nothing: the call then
    goes out exactly as it would without this feature, with no
    ``content_policy`` field. ``declaration`` is that field, minus the counts.
    """

    # Out of the repr: custom entries are the organization's own words, and a
    # state printed in a traceback or a debugger must not spell them out.
    policy: dict[str, Any] | None = field(repr=False)
    declaration: dict[str, Any] | None = field(repr=False)
    digest: str | None
    #: Monotonic seconds (:func:`monotonic`): refresh once past, keep using
    #: until ``valid_until``.
    refresh_at: float
    valid_until: float
    #: Wall-clock seconds: the signed ``exp``, which also bounds the use of a
    #: policy held (an answer that cleans nothing has none).
    expires_at: float = math.inf
    cleaners: dict[str, ContentCleaner] = field(default_factory=dict, repr=False)

    @property
    def enabled(self) -> bool:
        return self.policy is not None

    def cleaner_for(self, provider: str, fields: ContentFields) -> ContentCleaner:
        """The policy compiled with this provider's content fields, once."""
        cleaner = self.cleaners.get(provider)
        if cleaner is None:
            cleaner = ContentCleaner(self.policy, fields)
            self.cleaners[provider] = cleaner
        return cleaner


@dataclass(frozen=True)
class PendingPolicy:
    """A ``/v1/content-policy`` answer read, its signature not yet checked."""

    enabled: bool
    refresh_after: float
    key_id: str = ""
    signing_input: bytes = b""
    signature: bytes = b""
    claims: Mapping[str, Any] = field(default_factory=dict)
    challenge: Mapping[str, Any] = field(default_factory=dict)


def read_answer(data: Any) -> PendingPolicy:
    """Read one answer of the endpoint; refuse anything malformed."""
    if not isinstance(data, dict):
        raise _refuse("is not a JSON object")
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise _refuse("does not say whether cleaning is enabled")
    refresh_after = data.get("refresh_after", DEFAULT_REFRESH_AFTER_SECONDS)
    if (
        isinstance(refresh_after, bool)
        or not isinstance(refresh_after, (int, float))
        or not 0 <= refresh_after <= 86_400
    ):
        raise _refuse("has a `refresh_after` that is not a number of seconds")
    if not enabled:
        return PendingPolicy(enabled=False, refresh_after=float(refresh_after))
    token = data.get("policy")
    challenge = data.get("challenge")
    if not isinstance(token, str) or len(token) > 1_000_000:
        raise _refuse("carries no signed policy")
    if (
        not isinstance(challenge, dict)
        or not isinstance(challenge.get("token"), str)
        or not challenge["token"]
        or not isinstance(challenge.get("inputs"), list)
        or not all(isinstance(text, str) for text in challenge["inputs"])
        or len(challenge["inputs"]) > 64
    ):
        raise _refuse("carries no usable challenge")
    jws = _verify()
    try:
        header_segment, claims_segment, signature_segment = _split_policy(token)
        header = jws._json_object(
            jws._b64url_decode(header_segment, what="header"), what="header"
        )
        claims = jws._json_object(
            jws._b64url_decode(claims_segment, what="claims"), what="claims"
        )
        signature = jws._b64url_decode(signature_segment, what="signature")
        jws._check_no_critical_extensions(header)
        key_id = jws._key_id(header)
    except ActVerificationError as exc:
        raise _refuse("is not a well-formed JWS") from exc
    if header.get("alg") != "ES256":
        raise _refuse(f"is signed with {header.get('alg')!r}, not ES256")
    if header.get("typ") != POLICY_TYPE:
        raise _refuse(f"is of type {header.get('typ')!r}, not {POLICY_TYPE!r}")
    if not key_id.startswith(POLICY_KEY_PREFIX):
        raise _refuse("is not signed with a prompt-cleaning policy key")
    return PendingPolicy(
        enabled=True,
        refresh_after=float(refresh_after),
        key_id=key_id,
        signing_input=f"{header_segment}.{claims_segment}".encode("ascii"),
        signature=signature,
        claims=claims,
        challenge=challenge,
    )


def _split_policy(token: str) -> tuple[str, str, str]:
    """Split a compact JWS; the ACT size cap does not apply to a policy."""
    segments = token.split(".")
    if len(segments) != 3 or not all(segments):
        raise _refuse("is not a compact JWS")
    return segments[0], segments[1], segments[2]


def verify_pending(
    pending: PendingPolicy, jwks: Any, now: float, held: PolicyState | None = None
) -> PolicyState:
    """Check a signed answer against the key set and the clock; build its state.

    ``now`` is the wall clock, which the signed dates are read against; how
    long the state is then held is counted from here on :func:`monotonic`.
    ``held`` is the state this answer refreshes, if any: when it names the
    same digest, the policy is the same and its compiled cleaners carry over —
    checked and timed afresh, compiled once per digest.
    """
    received = monotonic()
    if not pending.enabled:
        return PolicyState(
            policy=None,
            declaration=None,
            digest=None,
            refresh_at=received + pending.refresh_after,
            valid_until=received
            + max(pending.refresh_after, DISABLED_VALIDITY_SECONDS),
        )
    jws = _verify()
    try:
        jwk = jws._select_key(jwks, pending.key_id)
        x = jws._coordinate(jwk, "x", pending.key_id)
        y = jws._coordinate(jwk, "y", pending.key_id)
    except ActVerificationError as exc:
        raise _refuse(f"names a key the key set cannot verify with ({exc})") from exc
    if not _p256.on_curve(x, y):
        raise _refuse("names a key that is not a point of P-256")
    if not _signature_holds(pending.signing_input, pending.signature, x, y):
        raise _refuse("does not carry a valid ID Side signature")

    claims = pending.claims
    if claims.get("iss") not in (jws.PRODUCTION_ISSUER, jws.TEST_ISSUER):
        raise _refuse("was not issued by ID Side")
    issued = _whole_number(claims.get("iat"))
    expires = _whole_number(claims.get("exp"))
    if issued is None or expires is None:
        raise _refuse("has no integer `iat` and `exp`")
    if expires <= issued:
        raise _refuse("expires before it was issued")
    policy = claims.get("policy")
    digest = claims.get("digest")
    # A string, first: a policy the canonical form cannot write has no digest,
    # and "none" must not equal a claim that is not there.
    if not isinstance(digest, str) or content_policy_digest_or_none(policy) != digest:
        raise _refuse("does not match its own digest")
    if (
        not isinstance(policy, dict)
        or not isinstance(claims.get("app_id"), str)
        or policy.get("app_id") != claims["app_id"]
    ):
        raise _refuse("is not the policy of the application it was issued for")
    # Last: everything else has checked out, so a date out of step with this
    # machine is its clock (the bounds themselves do not move).
    if expires <= now:
        raise _out_of_step(
            "past its expiry",
            "either the clock looks ahead, or a cache between this machine and "
            "the cloud served an old answer",
            " and that no proxy caches /v1/content-policy",
        )
    if issued > now + CLOCK_SKEW_SECONDS:
        raise _out_of_step("before its date of issue", "the clock looks behind", "")

    same = held is not None and held.digest == digest
    if not same:
        # Compiled once per digest: ContentPolicyError if it cannot apply.
        ContentCleaner(policy)
    inputs = list(pending.challenge["inputs"])
    declaration = {
        "digest": digest,
        "engine": ENGINE_VERSION,
        "rules": rules_digest(),
        "build": build_digest(),
        "challenge": {
            "token": pending.challenge["token"],
            "response": challenge_response(pending.challenge["token"], inputs),
        },
    }
    lifetime = expires - issued
    return PolicyState(
        policy=policy,
        declaration=declaration,
        digest=digest,
        refresh_at=received + min(pending.refresh_after, lifetime),
        valid_until=received + lifetime,
        expires_at=expires,
        cleaners=held.cleaners if same and held is not None else {},
    )


def _es256_verifier() -> Callable[[bytes, bytes, int, int], bool]:
    """``cryptography``'s ES256 check when installed, else the pure-Python one."""
    try:
        verifier: Callable[[bytes, bytes, int, int], bool] = (
            _verify()._load_es256_backend()
        )
    except ImportError:
        return _p256.verify
    return verifier


def _signature_holds(signing_input: bytes, signature: bytes, x: int, y: int) -> bool:
    """Whether ``signature`` (raw r‖s) signs ``signing_input`` under (x, y).

    The length and the key are checked here, before either backend is asked,
    so both judge exactly the same inputs: a signature of the wrong size or a
    key that is not a curve point is refused whichever is installed.
    """
    if len(signature) != SIGNATURE_BYTES or not _p256.on_curve(x, y):
        return False
    return _es256_verifier()(signing_input, signature, x, y)


def _whole_number(value: Any) -> float | None:
    """An integer as JSON means it (``1.0`` included, as Node reads it), or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if float(value).is_integer() else None


def content_policy_digest_or_none(policy: Any) -> str | None:
    try:
        return content_policy_digest(policy)
    except ValueError:
        return None


#: What leads the message of a refusal the policy route qualifies (401, 402,
#: 403, 409, 422, 429): its class is the one it has on ``/v1/authorize``, and
#: its words say which route answered. Node's ``POLICY_ROUTE_LEAD`` is the same.
POLICY_ROUTE_LEAD = "While fetching the prompt-cleaning policy: "


def unfetched(status: int | None = None) -> str:
    """Why a call stops when no policy could be fetched; Node's ``unfetched()`` twin."""
    said = "" if status is None else f" (HTTP {status})"
    return (
        f"The ID Side cloud's prompt-cleaning policy could not be fetched{said}, "
        "and none is held that is still valid; the AI call was not made."
    )


def unreachable(origin_hint: str) -> ConsentUnreachableError:
    message = unfetched()
    return ConsentUnreachableError(
        f"{message} {origin_hint}" if origin_hint else message
    )


def retry_after(value: str | None) -> float | None:
    """The wait a ``Retry-After`` asks for, capped; None when it says none.

    Only delta-seconds (ASCII digits, spaces and tabs around) are read: a date,
    a fraction or a sign asks for nothing this SDK will wait for, and the call
    then fails closed at once. Node's ``retryAfter()`` reads the same.
    """
    text = (value or "").strip(" \t")
    if not _DELTA_SECONDS.fullmatch(text):
        return None
    return min(float(text), RETRY_AFTER_CAP_SECONDS)


class PolicyCache:
    """What a consent client holds between calls: the state, the key set, timing.

    Shared by the blocking and the async client: which one fetches is theirs to
    decide; when to fetch, and what to do when fetching fails, is decided here
    once.
    """

    __slots__ = ("attempted_at", "forced_at", "jwks", "state")

    def __init__(self) -> None:
        self.state: PolicyState | None = None
        self.jwks: Any = None
        #: Monotonic seconds, both: when an authorize answer last asked for a
        #: fresh policy, and when the cloud was last asked, whatever it answered.
        self.forced_at = float("-inf")
        self.attempted_at = float("-inf")

    def knows(self, key_id: str) -> bool:
        """Whether the key set held names ``key_id`` (else fetch it first)."""
        keys = self.jwks.get("keys") if isinstance(self.jwks, dict) else None
        return isinstance(keys, list) and any(
            isinstance(key, dict) and key.get("kid") == key_id for key in keys
        )

    def held(self, now: float, mono: float) -> PolicyState | None:
        """The state to apply without asking the cloud, or None to ask it now.

        The state held until its refresh is due; then, while it is still valid,
        the cloud is asked at most once per ``MIN_REFRESH_INTERVAL_SECONDS``, so
        an incident does not make every AI call pay a fetch that will fail.
        Once nothing valid is held, never: an attempt may unblock the call.
        ``now`` is the wall clock, ``mono`` the monotonic one.
        """
        state = self.usable(now, mono)
        if state is None:
            return None
        if mono < state.refresh_at:
            return state
        if mono - self.attempted_at < MIN_REFRESH_INTERVAL_SECONDS:
            return state
        return None

    def usable(self, now: float, mono: float) -> PolicyState | None:
        """The held state if it may still be applied after a failed refresh."""
        state = self.state
        if state is None or mono >= state.valid_until or now >= state.expires_at:
            return None
        return state

    def retry_delay(
        self, status: int, header: str | None, now: float, mono: float
    ) -> float | None:
        """Seconds to wait before the one retry a 429 earns, or None: no retry.

        Only while no valid policy is held: with one held, the call applies it
        at once and waits for nothing.
        """
        if status != 429 or self.usable(now, mono) is not None:
            return None
        return retry_after(header)

    def note_answer(self, answer: Mapping[str, Any] | None, mono: float) -> None:
        """React to an authorize answer's ``content_policy``: refresh when asked."""
        status = answer.get("status") if answer is not None else None
        if (
            isinstance(status, str)
            and status in REFRESH_STATUSES
            and self.state is not None
            and mono - self.forced_at >= MIN_REFRESH_INTERVAL_SECONDS
        ):
            self.forced_at = mono
            self.state = replace(self.state, refresh_at=mono)


def event_labels(provider: Any, model: Any) -> dict[str, str]:
    """Which provider and model a blocked call was for, as the service keeps them.

    Each is left out — never sent empty or null — unless it is a string of
    the shape the service accepts: a member it cannot read makes it refuse
    the whole report, and the blocked call would go uncounted. The model is
    the one the caller wrote, never converted: ``4`` is not ``"4"``.
    """
    labels: dict[str, str] = {}
    if isinstance(provider, str) and EVENT_PROVIDER.fullmatch(provider):
        labels["provider"] = provider
    if isinstance(model, str) and EVENT_MODEL.fullmatch(model):
        labels["model"] = model
    return labels


def clock() -> float:
    """The wall clock, as a seam for tests."""
    return time.time()


def monotonic() -> float:
    """A clock no one can set: how long a held answer has been held, measured here."""
    return time.monotonic()


def sleep(seconds: float) -> None:
    """The blocking client's wait before its one retry after a 429 (a test seam)."""
    time.sleep(seconds)


async def async_sleep(seconds: float) -> None:
    """The async client's wait before its one retry after a 429 (a test seam)."""
    await asyncio.sleep(seconds)
