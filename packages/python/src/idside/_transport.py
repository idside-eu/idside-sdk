# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Transport to the ID Side cloud.

The SDK never signs ACT tokens itself (the private ECDSA keys live in the cloud).
It asks the cloud to authorize a request: the cloud resolves the user's
preferences AND returns a signed ACT token bound to this exact request.

This module is internal (``_transport``). The public entry point is
:func:`idside.consent.wrap`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import httpx

from idside import _content_policy as _policy
from idside.__about__ import __version__
from idside.exceptions import (
    AuthenticationError,
    BindingError,
    BindingRequiredError,
    ConflictingIdentityError,
    ConsentDeniedError,
    ConsentRefreshRequiredError,
    ConsentUnreachableError,
    ContractError,
    InvalidBindingError,
    MissingIdentityError,
    QuotaExceededError,
    RateLimitedError,
)

DEFAULT_BASE_URL = "https://sdk.idside.eu"
_AUTHORIZE_PATH = "/v1/authorize"

#: Appended to an outage-shaped failure when the address was the default: with
#: neither ``base_url=`` nor ``IDSIDE_BASE_URL``, a developer who forgot the
#: variable read ``Could not verify consent (HTTP 404)`` and went looking for a
#: network fault. Word for word the Node SDK's sentence.
_DEFAULT_ORIGIN_HINT = (
    f"No ID Side address was configured, so the SDK used its default, "
    f"{DEFAULT_BASE_URL}: set IDSIDE_BASE_URL to the address shown in your ID "
    f"Side dashboard, or pass that address explicitly."
)

#: Identifies the SDK to the cloud on every request (mirrors the Node SDK).
_USER_AGENT = f"idside-python/{__version__}"

#: The deadline the authorize hop gets, in seconds — one number, shared with the
#: Node SDK, which applies the same one to its `fetch`.
#:
#: On a fail-closed path a timeout is not a tuning knob: it is the moment the
#: product decides the cloud is dead and stops the caller's AI calls. Two SDKs
#: with two numbers is one network incident cutting a Python application and
#: sparing its Node equivalent, for no reason a customer could be told.
#:
#: Five, not ten, because waiting is what costs here: the call is a small JSON
#: POST (a lookup and a signature) whose healthy p99 is far under a second, and
#: the alternative to waiting is the documented refusal this SDK exists to
#: raise, never a silent pass. An authorize hop still open after five seconds is
#: already an incident.
#:
#: Every phase gets the same value rather than a tighter connect budget: a
#: budget that varies by phase makes the moment of refusal depend on *how* the
#: cloud is failing, and `fetch` cannot express the distinction, so keeping it
#: on this side was keeping half a contract the other SDK could not sign.
DEFAULT_TIMEOUT_SECONDS: Final = 5.0

_DEFAULT_TIMEOUT = httpx.Timeout(DEFAULT_TIMEOUT_SECONDS)

#: 403 problem+json ``type`` values the cloud uses to qualify a denial.
#:
#: Only what the cloud actually sends belongs here. ``provider_blocked`` sat in
#: this set for months and was never emitted by anything: a reason the SDK
#: names but nothing produces is a branch a caller writes and never takes, and
#: dead code that reads as tested behaviour. Adding one back is a cloud change
#: first, an SDK change second.
#:
#: ``policy_hold`` comes from an account in good standing: an organization's policy
#: could not apply to this one call, and the application is set to hold such
#: calls. An SDK that predates it still raises the denial, without the reason.
_DENY_REASONS = frozenset({"fraud_suspended", "organization_suspended", "policy_hold"})

#: RFC 9457 says `type` is a URI, and the cloud sends one. This SDK used to
#: compare it against the bare names above, so `reason` was ALWAYS None — for
#: `fraud_suspended` too — while the documentation showed callers branching on
#: it. The denial itself always worked; only the explanation was missing, which
#: is why it went unnoticed.
_PROBLEM_TYPE_PREFIX = "https://idside.eu/errors/"


def _problem_type_name(type_value: Any) -> str:
    """The error name a problem `type` carries, ``""`` if it is not a string.

    Callers compare the result against a CLOSED set of names, and that is what
    keeps the reading closed: a `type` from another host, a deeper path under
    our prefix (`…/errors/v2/<name>`) or a name this SDK does not know never
    equals one of them, so it qualifies nothing. The bare form is still
    accepted so an older cloud — or a test double — keeps working.
    """
    if not isinstance(type_value, str):
        return ""
    return type_value.removeprefix(_PROBLEM_TYPE_PREFIX)


def _deny_reason(type_value: Any) -> str | None:
    """Qualify a 403 from its problem `type`, or None if we cannot."""
    name = _problem_type_name(type_value)
    return name if name in _DENY_REASONS else None


#: The two 401s that are about the visitor, not the key, with what to do. Both
#: names are the cloud's (`ubt_invalid`: any UBT that does not verify — expired,
#: issued for another app, altered, or not a UBT; `ubt_required`: a strict app
#: called with `user_id`, or naming nobody). Any other
#: 401 — `invalid_api_key`, a `type` this SDK does not know, no body at all —
#: stays a plain AuthenticationError: a value nobody classified never moves a
#: refusal into a class that says the key is fine.
_BINDING_ERRORS: dict[str, tuple[type[BindingError], str]] = {
    "ubt_invalid": (
        InvalidBindingError,
        "ID Side refused this visitor's binding (HTTP 401): the UBT did not "
        "verify — it expired, was issued for another application, was altered, "
        "or is not a UBT at all. Your API key was accepted. "
        "Discard the UBT stored in this visitor's session and let the widget "
        "issue a new one before retrying; the AI call was not made.",
    ),
    "ubt_required": (
        BindingRequiredError,
        "This application requires a UBT (HTTP 401), and the call did not "
        "carry one. Your API key was accepted. Have the visitor go "
        "through the widget and pass the UBT it issues as ubt before any AI "
        "call; the AI call was not made.",
    ),
}


#: A posture is a label a developer types into their own configuration. Anything
#: longer is not one, and this value is quoted back in error messages.
_MAX_POSTURE_LENGTH = 200
_MAX_ACT_TOKEN_LENGTH = 8_192
_COMPACT_JWS = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_TOKEN_ID = re.compile(r"act_[0-9a-z]+")
_RFC3339 = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"[Tt](?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.\d+)?(?:[Zz]|(?P<offset_sign>[+-])(?P<offset_hour>\d{2}):"
    r"(?P<offset_minute>\d{2}))"
)
#: The binding a successful answer must carry is the identity the request was
#: made with: ``ubt`` when it carried a UBT (the widget bound the person and the
#: cloud verified it), ``user_id`` when it carried a ``user_id`` (the developer
#: asserted the identifier), ``api_key`` when it carried neither (the API key it
#: was made with is the proof's subject). Each is true of one request only, so
#: each is accepted only there: any other answer would put in the proof a
#: subject, or a link to one, the call did not have, and is refused like any
#: other malformed answer. The cloud echoes the identity it was sent.
_IDENTITY_FIELDS = ("ubt", "user_id")

#: What the cloud counts as blank — ActiveSupport's ``blank?``, the Unicode
#: White_Space characters — and nothing wider: the 25 code points below, the
#: same list the Node SDK holds. ``str.strip()`` would not do: it disagrees with
#: both the cloud and JavaScript's ``trim()`` on U+FEFF, U+0085 and U+001C-001F.
_BLANK = re.compile(
    r"[\t\n\v\f\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+"
)


def _is_blank(value: str) -> bool:
    """Whether ``value`` is made only of what the cloud counts as blank."""
    return _BLANK.fullmatch(value) is not None


def _named(identity: Any) -> bool:
    """Whether an identity argument names somebody.

    Empty, ``None``, ``0`` and ``False`` never did; a string made only of
    blanks does not either — the cloud refuses it with a 422. Nothing is
    trimmed: an identity that names somebody is sent exactly as written,
    because its pseudonym is derived from those characters.
    """
    return bool(identity) and not (isinstance(identity, str) and _is_blank(identity))


def _identity(user_id: Any, ubt: Any, *, conflict: str, empty: str) -> tuple[Any, Any]:
    """The identity a call carries, as ``(user_id, ubt)``: one of the two, or neither.

    Naming the end user is optional. An argument that is absent or ``None``
    names nobody, and a call on which neither names anybody goes out with no
    identity at all: the cloud then takes the API key as the proof's subject.

    An argument that is *present* but names nobody — ``""``, blanks, ``0``,
    ``False``, an empty container — is still refused, unless the other one
    names somebody, exactly as when an identity was required. Such a value is
    almost always a bug upstream (an empty form field, a header that was not
    set), and reading it as "nobody" would silently drop the per-person proof
    its caller meant to get. Two arguments that both name somebody are a
    contradiction, not a choice.
    """
    named_user, named_ubt = _named(user_id), _named(ubt)
    if named_user and named_ubt:
        raise ConflictingIdentityError(conflict)
    if not (named_user or named_ubt) and (user_id is not None or ubt is not None):
        raise MissingIdentityError(empty)
    return (user_id if named_user else None), (ubt if named_ubt else None)


_TOOL_CONSTRAINTS = frozenset({"refuse", "strip"})


def _problem_body(response: httpx.Response) -> dict[str, Any]:
    """Best-effort parse of an RFC 9457 problem+json body; ``{}`` if unusable."""
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _credential_posture(value: Any) -> str | None:
    """Validate the routed posture from an authorize response.

    Null and absent mean the same thing — the client as constructed is fine.
    Anything else that is not a plain, printable, reasonably short label is
    treated as a response the SDK cannot act on, and a decision it cannot act on
    is not a decision it may ignore: ignoring it would send the call on the
    credential routing existed to move it off.
    """
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_POSTURE_LENGTH
        or not value.isprintable()
    ):
        raise ConsentUnreachableError(
            "ID Side cloud named a credential posture the SDK cannot use "
            "(credential_posture must be a short, printable string); the AI call "
            "was not made."
        )
    return value


def _invalid_decision(field_name: str, expected: str) -> ConsentUnreachableError:
    """Describe an unusable successful response without echoing its value."""
    return ConsentUnreachableError(
        "ID Side cloud returned a malformed authorize response "
        f"(field {field_name} must be {expected}); the AI call was not made."
    )


def _decision_object(
    data: dict[str, Any], field_name: str, *, required: bool = False
) -> dict[str, Any]:
    """Read a JSON object field, defaulting only optional absent fields."""
    if field_name not in data:
        if required:
            raise KeyError(field_name)
        return {}
    value = data[field_name]
    if not isinstance(value, dict):
        raise _invalid_decision(field_name, "a JSON object")
    return value


def _decision_string(data: dict[str, Any], field_name: str) -> str:
    """Read a required, non-empty printable string field."""
    value = data[field_name]
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise _invalid_decision(field_name, "a non-empty printable string")
    return value


def _act_token(data: dict[str, Any]) -> str:
    """Read the compact-JWS-shaped token returned by authorize."""
    token = _decision_string(data, "act_token")
    if len(token) > _MAX_ACT_TOKEN_LENGTH or _COMPACT_JWS.fullmatch(token) is None:
        raise _invalid_decision("act_token", "a compact JWS")
    return token


def _token_id(data: dict[str, Any]) -> str:
    """Read the public identifier of the ACT token."""
    token_id = _decision_string(data, "token_id")
    if _TOKEN_ID.fullmatch(token_id) is None:
        raise _invalid_decision("token_id", "an ACT token identifier")
    return token_id


def _expires_at(data: dict[str, Any]) -> str:
    """Read a calendar-valid RFC 3339 timestamp."""
    value = _decision_string(data, "expires_at")
    match = _RFC3339.fullmatch(value)
    if match is None:
        raise _invalid_decision("expires_at", "an RFC 3339 date-time")
    parts = {
        name: int(raw)
        for name, raw in match.groupdict().items()
        if raw and name != "offset_sign"
    }
    year = parts["year"]
    month = parts["month"]
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    month_days = (
        31,
        29 if leap else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    )
    valid = (
        1 <= month <= 12
        and 1 <= parts["day"] <= month_days[month - 1]
        and parts["hour"] <= 23
        and parts["minute"] <= 59
        and parts["second"] <= 60
        and parts.get("offset_hour", 0) <= 23
        and parts.get("offset_minute", 0) <= 59
    )
    if not valid:
        raise _invalid_decision("expires_at", "an RFC 3339 date-time")
    return value


def _constraints(data: dict[str, Any]) -> dict[str, Any]:
    """Read only constraints this SDK version can apply safely."""
    constraints = _decision_object(data, "constraints")
    unknown = set(constraints) - {"tools"}
    if unknown:
        raise _invalid_decision("constraints", "an object understood by this SDK")
    if "tools" in constraints and (
        not isinstance(constraints["tools"], str)
        or constraints["tools"] not in _TOOL_CONSTRAINTS
    ):
        raise _invalid_decision("constraints.tools", "the string 'refuse' or 'strip'")
    return constraints


def _content_policy_answer(value: Any) -> dict[str, Any] | None:
    """Read the optional ``content_policy`` answer; anything unexpected is ignored.

    Advisory, so read leniently: it never changes what this call does, only
    when the SDK next fetches the policy. A shape this SDK does not know is
    read as no answer, as an older SDK reads the field at all.
    """
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        return None
    return {key: item for key, item in value.items() if key in ("status", "digest")}


def _with_problem_details(message: str, problem: dict[str, Any]) -> str:
    """Append the problem+json ``title``/``detail`` to ``message`` if present.

    The cloud writes its ``detail`` as a sentence, so it usually brings its own
    full stop: adding another printed ``…expired)..``.
    """
    parts = [
        value
        for key in ("title", "detail")
        if isinstance(value := problem.get(key), str) and value
    ]
    if not parts:
        return message
    said = "; ".join(parts)
    end = "" if said.endswith((".", "!", "?")) else "."
    return f"{message} Cloud says: {said}{end}"


@dataclass(frozen=True)
class ConsentDecision:
    """The cloud's authorization decision for a single AI request.

    ``binding`` records what the proof's subject is and how it was tied to this
    request — the evidentiary weight of the proof, visible to an auditor:

    - ``"api_key"`` — the call named nobody: the subject is the API key it was
      made with. The policy applied is the organization's, for whoever holds
      that key.
    - ``"ubt"`` — the identity came from a User Binding Token: the ID Side
      widget saw this user and the cloud verified the signed binding.
    - ``"user_id"`` — the developer asserted the identity; accepted, but the
      weaker link (nothing ties the id passed to the SDK to the person it
      names).
    """

    consent_state: dict[str, Any]
    params_forced: dict[str, Any]
    act_token: str
    source: str
    legal_basis: str
    binding: str
    #: Non-parameter restrictions the SDK must apply to the outgoing call —
    #: today only ``{"tools": "refuse" | "strip"}``. Separate from
    #: ``params_forced`` on purpose: a forced parameter sets a value, and the
    #: cloud is never allowed to write the developer's request. A constraint
    #: rejects a call instead, which is a different kind of authority.
    #:
    #: It is returned from the user's consent state alone: the cloud is not told
    #: whether this call carries tools, and could not be without learning
    #: something about the request.
    constraints: dict[str, Any] = field(default_factory=dict)
    #: The label of the credential this call must go out on, or ``None`` when
    #: the client as constructed is fine. It is the answer to a policy question
    #: — *which privacy posture does this user's consent require?* — which is
    #: why the cloud answers it and the SDK only matches the label against what
    #: the developer provisioned. Naming one that was not provisioned fails the
    #: call (``MissingCredentialError``): the cloud must name what the answer
    #: requires, never the best of what happens to be available.
    credential_posture: str | None = None
    #: What the cloud made of this call's prompt-cleaning declaration —
    #: ``{"status": "applied" | "outdated" | "anomaly" | "not_applied",
    #: "digest": "<the current policy's>"}`` — or ``None`` when the application
    #: cleans nothing. Advisory: it never changes the call; the SDK fetches a
    #: fresh policy when it says the one applied is not current.
    content_policy: dict[str, Any] | None = None


class ConsentClient(Protocol):
    """Interface the SDK uses to authorize a request against the cloud."""

    def authorize(
        self,
        *,
        provider: str,
        model: str,
        request_hash: str,
        user_id: str | None = None,
        ubt: str | None = None,
        call_site: str | None = None,
        credential_postures: Sequence[str] = (),
    ) -> ConsentDecision: ...


class AsyncConsentClient(Protocol):
    """The same interface, for the client an async provider client is wrapped with.

    Separate rather than a union member with an ``async`` flag: the two are told
    apart by which provider client was wrapped, at ``wrap()`` time, and never on
    the request path. An application serving other requests on the same event
    loop cannot afford a blocking hop to the cloud once per AI call — that is
    worse than the problem async support exists to solve.
    """

    async def authorize(
        self,
        *,
        provider: str,
        model: str,
        request_hash: str,
        user_id: str | None = None,
        ubt: str | None = None,
        call_site: str | None = None,
        credential_postures: Sequence[str] = (),
    ) -> ConsentDecision: ...


#: Whitespace the HTTP layer drops around a header value — what ``fetch`` drops
#: in the Node SDK — and the characters a key may hold once it is dropped.
_HTTP_WHITESPACE = "\t\n\r "
_SENDABLE_KEY = re.compile(r"[\x21-\x7e]+")

_UNSENDABLE_KEY = (
    "The ID Side API key cannot be sent: it contains a character an HTTP header "
    "cannot carry (a space, a tab or a line break inside it, or a character "
    "outside printable ASCII), often left by the file or variable it was read "
    "from. Check the api_key passed to consent.wrap() and IDSIDE_API_KEY; the "
    "key itself is not repeated here."
)


def _sendable_key(api_key: str | None) -> str | None:
    """The key as it will be sent, ``None`` when there is none.

    Whitespace around it is dropped. Anything else a header cannot carry is
    refused here, before a request exists: sent as it was, the HTTP layer
    rejected it with an error quoting the whole header — key included — chained
    under an "unreachable" that sent the developer looking for an outage.
    """
    key = (api_key or "").strip(_HTTP_WHITESPACE)
    if not key:
        return None
    if _SENDABLE_KEY.fullmatch(key) is None:
        raise ValueError(_UNSENDABLE_KEY)
    return key


def _resolved_config(
    base_url: str | None, api_key: str | None
) -> tuple[str, dict[str, str], str]:
    """Resolve base URL and headers from args then environment.

    ``base_url``: explicit argument, else ``IDSIDE_BASE_URL``, else the default.
    ``api_key``: explicit argument, else ``IDSIDE_API_KEY``, else none (no
    ``Authorization`` header). An empty-string env var is treated as absent,
    and so is a key made only of whitespace; whitespace around a key is
    dropped, and a key a header cannot carry raises ``ValueError`` here.
    The third value is the sentence an outage-shaped failure carries when the
    default was used, ``""`` when an address was given.
    """
    configured = base_url or os.environ.get("IDSIDE_BASE_URL")
    resolved_api_key = _sendable_key(api_key or os.environ.get("IDSIDE_API_KEY"))
    headers = {"User-Agent": _USER_AGENT}
    if resolved_api_key:
        headers["Authorization"] = f"Bearer {resolved_api_key}"
    if configured:
        return configured, headers, ""
    return DEFAULT_BASE_URL, headers, _DEFAULT_ORIGIN_HINT


def _authorize_payload(
    *,
    provider: str,
    model: str,
    request_hash: str,
    user_id: str | None,
    ubt: str | None,
    call_site: str | None,
    credential_postures: Sequence[str],
    content_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``/v1/authorize`` body, refusing an identity that is not one.

    Shared by the blocking and the async client, and deliberately: what the
    cloud is told about a request must not depend on which shape of provider
    client the developer happens to hold. A call that names nobody carries no
    identity field at all — not an empty one, which the cloud would refuse.
    """
    user_id, ubt = _identity(
        user_id,
        ubt,
        conflict="Pass either user_id or ubt to authorize(), not both.",
        empty=(
            "authorize() was given an identity that names nobody: pass a "
            "user_id or ubt that names somebody, or neither."
        ),
    )
    identity: dict[str, Any] = {}
    if ubt is not None:
        identity["ubt"] = ubt
    elif user_id is not None:
        identity["user_id"] = user_id
    payload: dict[str, Any] = {
        **identity,
        "provider": provider,
        "model": model,
        "request_hash": request_hash,
        "credential_postures": list(credential_postures),
    }
    if call_site:
        payload["call_site"] = call_site
    # Only when the application cleans prompts: an application that cleans
    # nothing sends exactly what an SDK without cleaning sends.
    if content_policy is not None:
        payload["content_policy"] = dict(content_policy)
    return payload


def _binding_for(payload: dict[str, Any]) -> str:
    """The one binding an answer to this authorize body may carry.

    The identity field the body carries, or ``api_key`` when it carries none.
    It never carries both: :func:`_authorize_payload` refuses two identities.
    """
    return next((name for name in _IDENTITY_FIELDS if name in payload), "api_key")


def _decision_from(
    response: httpx.Response, origin_hint: str, *, binding: str
) -> ConsentDecision:
    """Turn one authorize response into a decision, or fail closed.

    Also shared: a 403 that denies a blocking call must deny the async one, and
    a body the SDK cannot fully parse is "consent not verified" on both paths.
    ``origin_hint`` is the sentence an unexpected status carries when the
    address was the default, ``""`` otherwise. ``binding`` is the one the answer
    must carry, decided by the request (:func:`_binding_for`).
    """
    status = response.status_code
    if status >= httpx.codes.BAD_REQUEST:
        _raise_for_status(status, response, origin_hint)

    # Defensive validation: a 200 the SDK cannot fully parse is treated as
    # "consent not verified" (fail-closed) — a decision is never guessed.
    try:
        data = response.json()
    except ValueError as exc:
        raise ConsentUnreachableError(
            "ID Side cloud returned a malformed authorize response "
            "(invalid JSON); the AI call was not made."
        ) from exc
    if not isinstance(data, dict):
        raise ConsentUnreachableError(
            "ID Side cloud returned a malformed authorize response "
            "(not a JSON object); the AI call was not made."
        )
    try:
        if data["decision"] != "allow":
            raise _invalid_decision("decision", "'allow'")
        consent_state = data["consent_state"]
        if not isinstance(consent_state, dict):
            raise _invalid_decision("consent_state", "a JSON object")
        if _decision_string(data, "binding") != binding:
            raise _invalid_decision(
                "binding",
                "'api_key' on a call that names nobody"
                if binding == "api_key"
                else f"'{binding}' on a call made with {binding}",
            )
        mode = data["mode"]
        if not isinstance(mode, str) or mode not in {"test", "live"}:
            raise _invalid_decision("mode", "'test' or 'live'")
        source = _decision_string(data, "source")
        legal_basis = _decision_string(data, "legal_basis")
        _token_id(data)
        _expires_at(data)
        return ConsentDecision(
            consent_state=consent_state,
            params_forced=_decision_object(data, "params_forced", required=True),
            act_token=_act_token(data),
            source=source,
            legal_basis=legal_basis,
            binding=binding,
            constraints=_constraints(data),
            credential_posture=_credential_posture(data.get("credential_posture")),
            content_policy=_content_policy_answer(data.get("content_policy")),
        )
    except KeyError as exc:
        raise ConsentUnreachableError(
            f"ID Side cloud returned an incomplete authorize response "
            f"(missing field {exc}); the AI call was not made."
        ) from exc


def _raise_for_status(
    status: int,
    response: httpx.Response,
    origin_hint: str,
    *,
    undetermined: str | None = None,
    route: str = "",
) -> None:
    """Map an error response to the typed exception hierarchy.

    A configuration problem (401/402/422/429) must be distinguishable from
    an outage; everything genuinely undetermined stays fail-closed
    (``ConsentUnreachableError``), and says ``undetermined`` when the route
    has words of its own; ``route`` leads every other message when the route
    is not ``/v1/authorize``, so the class stays and the message names it. No AI
    call goes out in any of these cases.
    """
    problem = _problem_body(response)

    def said(message: str) -> str:
        return _with_problem_details(route + message, problem)

    if status == httpx.codes.UNAUTHORIZED:
        binding = _BINDING_ERRORS.get(_problem_type_name(problem.get("type")))
        if binding is not None:
            error_class, message = binding
            raise error_class(said(message))
        raise AuthenticationError(
            said(
                "ID Side rejected the API credentials (HTTP 401). Check the "
                "api_key passed to consent.wrap() against the ID Side "
                "dashboard; the AI call was not made."
            )
        )
    if status == httpx.codes.PAYMENT_REQUIRED:
        raise QuotaExceededError(
            said(
                "ID Side plan quota exceeded (HTTP 402). Upgrade the plan or "
                "wait for the quota window to reset; the AI call was not made."
            )
        )
    if status == httpx.codes.FORBIDDEN:
        type_value = problem.get("type")
        reason = _deny_reason(type_value)
        raise ConsentDeniedError(
            said(
                "The requested AI usage was denied (HTTP 403); "
                "the AI call was not made."
            ),
            reason=reason,
        )
    if (
        status == httpx.codes.CONFLICT
        and _problem_type_name(problem.get("type")) == "consent_refresh_required"
    ):
        # Not an outage: the visitor's binding still verifies, and what it
        # covers is out of date. Any other 409 stays undetermined, below.
        raise ConsentRefreshRequiredError(
            said(
                "This visitor's recorded choices no longer cover this "
                "application's AI use (HTTP 409): the policy or the questions "
                "it requires changed since they answered. Show them the widget "
                "again before retrying; the AI call was not made."
            )
        )
    if status == httpx.codes.UNPROCESSABLE_ENTITY:
        raise ContractError(
            said(
                "ID Side refused this call's payload (HTTP 422). If the cloud's "
                "detail names a value your code passed, fix that value; "
                "otherwise the SDK and the cloud disagree on the contract — "
                "upgrade the SDK or report it; the AI call was not made."
            )
        )
    if status == httpx.codes.TOO_MANY_REQUESTS:
        raise RateLimitedError(
            said(
                "ID Side rate limit hit (HTTP 429). Retry after a backoff; "
                "the AI call was not made."
            )
        )
    # 5xx or any undetermined 4xx: consent could not be verified → fail-closed.
    if undetermined is None:
        undetermined = (
            f"Could not verify consent (HTTP {status}); the AI call was not made."
        )
    raise ConsentUnreachableError(
        _with_hint(_with_problem_details(undetermined, problem), origin_hint)
    )


def _with_hint(message: str, origin_hint: str) -> str:
    return f"{message} {origin_hint}" if origin_hint else message


_UNREACHABLE = "ID Side cloud is unreachable; the AI call was not made."


def _policy_json(response: httpx.Response, origin_hint: str) -> Any:
    """The body of a ``/v1/content-policy`` answer, or the typed error it maps to.

    A qualified refusal keeps the class it has on ``/v1/authorize``; any other
    error says the policy could not be fetched, with its status — never "could
    not verify consent", which would send the developer to an authorize route
    that may be answering well.
    """
    status = response.status_code
    if status >= httpx.codes.BAD_REQUEST:
        _raise_for_status(
            status,
            response,
            origin_hint,
            undetermined=_policy.unfetched(status),
            route=_policy.POLICY_ROUTE_LEAD,
        )
    try:
        return response.json()
    except ValueError:
        return None  # read_answer refuses it: "is not a JSON object"


def _event_body(
    digest: str,
    engine: str,
    blocked: Mapping[str, int],
    provider: str | None,
    model: str | None,
) -> dict[str, Any]:
    """The JSON of a blocked call's report: what is not given is not written."""
    body: dict[str, Any] = {
        "digest": digest,
        "engine": engine,
        "blocked": dict(blocked),
    }
    if provider is not None:
        body["provider"] = provider
    if model is not None:
        body["model"] = model
    return body


def _jwks_json(response: httpx.Response, origin_hint: str) -> Any:
    """The cloud's key set, or unreachable: without it no policy can be checked."""
    try:
        if response.status_code == httpx.codes.OK:
            return response.json()
    except ValueError:
        pass
    raise _policy.unreachable(origin_hint)


class HttpConsentClient:
    """Default :class:`ConsentClient` backed by ``idside.eu`` (httpx)."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        timeout: httpx.Timeout | float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Build the transport, resolving config from args then environment.

        ``base_url``: explicit argument, else ``IDSIDE_BASE_URL``, else the
        default. ``api_key``: explicit argument, else ``IDSIDE_API_KEY``, else
        none (no ``Authorization`` header). An empty-string env var is treated
        as absent. Every request carries a ``User-Agent: idside-python/<ver>``
        header. ``timeout`` overrides the shared 5 s default
        (:data:`DEFAULT_TIMEOUT_SECONDS`, the same number the Node SDK uses).
        """
        resolved_base_url, headers, self._origin_hint = _resolved_config(
            base_url, api_key
        )
        self._client = httpx.Client(
            base_url=resolved_base_url,
            timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT,
            transport=transport,
            headers=headers,
        )
        self._policy = _policy.PolicyCache()
        # One fetch of the policy (and of the key set it needs) in flight at a
        # time: every caller that has no valid policy waits for that one.
        self._flight_lock = threading.Lock()
        self._flight: Future[_policy.PolicyState] | None = None

    def content_policy(self) -> _policy.PolicyState:
        """The application's prompt-cleaning policy, verified and cached.

        Fetched from ``/v1/content-policy`` on first use and again after
        ``refresh_after`` — at most once per ten seconds while a valid policy is
        held, whatever the last attempt gave. A policy that does not verify
        raises :class:`ContentPolicyError`; a cloud that cannot be reached
        raises :class:`ConsentUnreachableError` unless the policy held is still
        valid.

        Callers on several threads share one fetch: while it is in flight, a
        caller that holds a valid policy applies it, and the others wait for
        that fetch and receive what it gave — its failure included, which is
        never kept: the next call starts a fetch of its own.
        """
        with self._flight_lock:
            now, mono = _policy.clock(), _policy.monotonic()
            current = self._policy.held(now, mono)
            if current is not None:
                return current
            flight = self._flight
            if flight is not None:
                joined = True
            else:
                flight = self._flight = Future()
                self._policy.attempted_at = mono
                joined = False
        if joined:
            return flight.result()
        try:
            state = self._attempt(now, mono)
        except BaseException as exc:
            # Even an interrupt: a waiter must never wait for nothing.
            self._land()
            flight.set_exception(exc)
            raise
        self._land()
        flight.set_result(state)
        return state

    def _land(self) -> None:
        """Clear the slot before the waiters wake: the next caller starts afresh."""
        with self._flight_lock:
            self._flight = None

    def _attempt(self, now: float, mono: float) -> _policy.PolicyState:
        cache = self._policy
        try:
            state = self._fetch_policy(now, mono)
        except (ConsentUnreachableError, RateLimitedError):
            # Read again: the fetch took time, and the policy held may have
            # run out while it was failing.
            held = cache.usable(_policy.clock(), _policy.monotonic())
            if held is None:
                raise
            return held
        # Under the lock an authorize answer reads and rewrites the state with
        # (`authorize()`): written outside it, this policy could land between
        # that read and that write, and be replaced by the older one.
        with self._flight_lock:
            cache.state = state
        return state

    def _fetch_policy(self, now: float, mono: float) -> _policy.PolicyState:
        response = self._get_policy()
        delay = self._policy.retry_delay(
            response.status_code, response.headers.get("Retry-After"), now, mono
        )
        if delay is not None:
            _policy.sleep(delay)
            response = self._get_policy()
        pending = _policy.read_answer(_policy_json(response, self._origin_hint))
        if pending.enabled and not self._policy.knows(pending.key_id):
            self._policy.jwks = self._fetch_jwks()
        return _policy.verify_pending(
            pending, self._policy.jwks, _policy.clock(), self._policy.state
        )

    def _get_policy(self) -> httpx.Response:
        try:
            return self._client.get(_policy.CONTENT_POLICY_PATH)
        except httpx.HTTPError as exc:
            raise _policy.unreachable(self._origin_hint) from exc

    def _fetch_jwks(self) -> Any:
        request = self._client.build_request("GET", _policy.JWKS_PATH)
        request.headers.pop("Authorization", None)  # a key set needs no key
        try:
            response = self._client.send(request)
        except httpx.HTTPError as exc:
            raise _policy.unreachable(self._origin_hint) from exc
        return _jwks_json(response, self._origin_hint)

    def content_events(
        self,
        *,
        digest: str,
        engine: str,
        blocked: Mapping[str, int],
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Report a call cleaning blocked: rule ids and counts, never a value.

        ``provider`` and ``model`` say which provider and model the call was
        for; each is written only when given (see
        :func:`idside._content_policy.event_labels`), never as ``null``.

        Best effort: the call is already refused locally, so a report that
        cannot be delivered is dropped rather than turned into a second error.
        """
        with contextlib.suppress(Exception):  # best effort, by contract
            self._client.post(
                _policy.CONTENT_EVENTS_PATH,
                json=_event_body(digest, engine, blocked, provider, model),
                timeout=_policy.EVENTS_TIMEOUT_SECONDS,
            )

    def authorize(
        self,
        *,
        provider: str,
        model: str,
        request_hash: str,
        user_id: str | None = None,
        ubt: str | None = None,
        call_site: str | None = None,
        credential_postures: Sequence[str] = (),
        content_policy: Mapping[str, Any] | None = None,
    ) -> ConsentDecision:
        """Ask the cloud to authorize one AI request.

        At most one of ``user_id`` or ``ubt`` names the end user, and neither
        has to: a call that names nobody is authorized with the API key as the
        proof's subject, and the answer says so with ``binding="api_key"``. A
        UBT (User Binding Token) is opaque to the SDK: it is forwarded as-is
        and the cloud alone verifies its signature and binding.

        ``call_site`` names the endpoint about to be called (``responses.create``,
        ``chat.completions.create``…). It is *mechanics*, not policy: the cloud
        needs it because a provider's endpoints do not accept the same
        parameters, and forcing one where it does not exist breaks the call for
        exactly the users who opted out. Omitted when unknown, in which case the
        cloud falls back to what is safe on every endpoint of that provider.

        ``credential_postures`` lists the posture labels the developer
        provisioned for this provider (see ``credentials=`` on
        :func:`idside.consent.wrap`). It is sent on every request, empty list
        included, because its *presence* is what tells the cloud this SDK
        understands routing at all: an older one sends no such field and would
        ignore a posture it was handed, which is the one case where the cloud
        must not count on the call being routed. Labels are app configuration,
        never credentials — the credential itself never leaves the process.

        ``content_policy`` is what this call's prompt cleaning declares (the
        policy's digest, the engine's, the challenge answer and the counts of
        what was masked), sent only when the application cleans prompts.
        """
        payload = _authorize_payload(
            provider=provider,
            model=model,
            request_hash=request_hash,
            user_id=user_id,
            ubt=ubt,
            call_site=call_site,
            credential_postures=credential_postures,
            content_policy=content_policy,
        )
        try:
            # No retry: authorize is not safely idempotent. A request that
            # reached the cloud before the connection dropped would already have
            # minted an ACT token and appended a ledger entry, so resending it
            # risks a duplicate entry. Fail closed instead (as the Node SDK does).
            response = self._client.post(_AUTHORIZE_PATH, json=payload)
        except httpx.HTTPError as exc:
            # Network failure → fail-closed: do not let the caller proceed.
            raise ConsentUnreachableError(
                _with_hint(_UNREACHABLE, self._origin_hint)
            ) from exc
        decision = _decision_from(
            response, self._origin_hint, binding=_binding_for(payload)
        )
        # Read and rewritten in one step, under the lock a refresh writes the
        # state under: another thread's newer policy can never be overwritten
        # with the older one read here. (The async client needs no lock: on
        # one event loop, nothing runs between that read and that write.)
        with self._flight_lock:
            self._policy.note_answer(decision.content_policy, _policy.monotonic())
        return decision

    def close(self) -> None:
        self._client.close()


class AsyncHttpConsentClient:
    """:class:`AsyncConsentClient` backed by ``idside.eu`` (httpx, async).

    The same conversation as :class:`HttpConsentClient` — same path, same
    payload, same fail-closed reading of the answer, because they share the
    functions that build and interpret both. What differs is the one hop that
    has to: ``httpx.AsyncClient`` instead of ``httpx.Client``, so the wait for
    the cloud yields the event loop instead of stopping it. On an ASGI
    application that loop is also serving every other request in flight.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        timeout: httpx.Timeout | float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Build the transport, resolving config from args then environment.

        Identical resolution to the blocking client: explicit argument, else
        ``IDSIDE_BASE_URL`` / ``IDSIDE_API_KEY``, else the defaults. An
        empty-string env var is treated as absent. Every request carries a
        ``User-Agent: idside-python/<ver>`` header. ``timeout`` overrides the
        shared 5 s default (:data:`DEFAULT_TIMEOUT_SECONDS`).
        """
        resolved_base_url, headers, self._origin_hint = _resolved_config(
            base_url, api_key
        )
        self._client = httpx.AsyncClient(
            base_url=resolved_base_url,
            timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT,
            transport=transport,
            headers=headers,
        )
        self._policy = _policy.PolicyCache()
        # The fetch in flight, as a task of the loop that started it.
        self._flight: asyncio.Task[_policy.PolicyState] | None = None

    async def content_policy(self) -> _policy.PolicyState:
        """The blocking client's :meth:`HttpConsentClient.content_policy`, awaited.

        Concurrent callers share one fetch, run as a task of its own: a caller
        cancelled while waiting (a request its client gave up on) never cancels
        it for the others. A fetch started on another event loop is never
        awaited from this one: this loop starts its own.
        """
        now, mono = _policy.clock(), _policy.monotonic()
        current = self._policy.held(now, mono)
        if current is not None:
            return current
        loop = asyncio.get_running_loop()
        flight = self._flight
        if flight is None or flight.get_loop() is not loop:
            self._policy.attempted_at = mono
            flight = self._flight = loop.create_task(self._fly(now, mono))
        # A waiter cancelled here leaves the fetch running; were they all
        # cancelled, `shield` still marks a failure of it as retrieved.
        return await asyncio.shield(flight)

    async def _fly(self, now: float, mono: float) -> _policy.PolicyState:
        try:
            return await self._attempt(now, mono)
        finally:
            # Before the waiters wake: the next caller starts afresh, and a
            # failure is never kept. Another loop's fetch may hold the slot.
            if self._flight is asyncio.current_task():
                self._flight = None

    async def _attempt(self, now: float, mono: float) -> _policy.PolicyState:
        cache = self._policy
        try:
            state = await self._fetch_policy(now, mono)
        except (ConsentUnreachableError, RateLimitedError):
            # Read again: the fetch took time, and the policy held may have
            # run out while it was failing.
            held = cache.usable(_policy.clock(), _policy.monotonic())
            if held is None:
                raise
            return held
        cache.state = state
        return state

    async def _fetch_policy(self, now: float, mono: float) -> _policy.PolicyState:
        response = await self._get_policy()
        delay = self._policy.retry_delay(
            response.status_code, response.headers.get("Retry-After"), now, mono
        )
        if delay is not None:
            await _policy.async_sleep(delay)
            response = await self._get_policy()
        pending = _policy.read_answer(_policy_json(response, self._origin_hint))
        if pending.enabled and not self._policy.knows(pending.key_id):
            request = self._client.build_request("GET", _policy.JWKS_PATH)
            request.headers.pop("Authorization", None)  # a key set needs no key
            try:
                jwks_response = await self._client.send(request)
            except httpx.HTTPError as exc:
                raise _policy.unreachable(self._origin_hint) from exc
            self._policy.jwks = _jwks_json(jwks_response, self._origin_hint)
        return _policy.verify_pending(
            pending, self._policy.jwks, _policy.clock(), self._policy.state
        )

    async def _get_policy(self) -> httpx.Response:
        try:
            return await self._client.get(_policy.CONTENT_POLICY_PATH)
        except httpx.HTTPError as exc:
            raise _policy.unreachable(self._origin_hint) from exc

    async def content_events(
        self,
        *,
        digest: str,
        engine: str,
        blocked: Mapping[str, int],
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """The blocking client's :meth:`HttpConsentClient.content_events`, awaited."""
        with contextlib.suppress(Exception):  # best effort, by contract
            await self._client.post(
                _policy.CONTENT_EVENTS_PATH,
                json=_event_body(digest, engine, blocked, provider, model),
                timeout=_policy.EVENTS_TIMEOUT_SECONDS,
            )

    async def authorize(
        self,
        *,
        provider: str,
        model: str,
        request_hash: str,
        user_id: str | None = None,
        ubt: str | None = None,
        call_site: str | None = None,
        credential_postures: Sequence[str] = (),
        content_policy: Mapping[str, Any] | None = None,
    ) -> ConsentDecision:
        """Ask the cloud to authorize one AI request, without blocking the loop.

        Every word of :meth:`HttpConsentClient.authorize` applies here: at most
        one of ``user_id`` or ``ubt`` names the end user, the UBT stays opaque to
        the SDK, ``call_site`` names the endpoint so the cloud can decide per
        endpoint, and ``credential_postures`` is sent on every request — empty
        list included — because its presence is what tells the cloud this SDK
        understands routing.
        """
        payload = _authorize_payload(
            provider=provider,
            model=model,
            request_hash=request_hash,
            user_id=user_id,
            ubt=ubt,
            call_site=call_site,
            credential_postures=credential_postures,
            content_policy=content_policy,
        )
        try:
            # No retry, for the same reason as the blocking client: a request
            # that reached the cloud before the connection dropped has already
            # minted an ACT token and appended a ledger entry.
            response = await self._client.post(_AUTHORIZE_PATH, json=payload)
        except httpx.HTTPError as exc:
            # Network failure → fail-closed: do not let the caller proceed.
            raise ConsentUnreachableError(
                _with_hint(_UNREACHABLE, self._origin_hint)
            ) from exc
        decision = _decision_from(
            response, self._origin_hint, binding=_binding_for(payload)
        )
        self._policy.note_answer(decision.content_policy, _policy.monotonic())
        return decision

    async def aclose(self) -> None:
        await self._client.aclose()
