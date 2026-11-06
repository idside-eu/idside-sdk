# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Local, offline verification of ACT tokens.

An ACT token is the cryptographic proof ID Side issues for each AI call. This
module lets anyone holding a token — an auditor, a customer's compliance team,
a provider, a data protection authority — check it **without calling ID Side**:

    from idside import verify_act

    proof = verify_act(token, jwks=jwks, request_hash=request_hash)

Verification only. This package never signs and holds no private key (see
docs/concepts.md §6, "The SDK cannot forge one"): the ECDSA signing keys live
exclusively in the ID Side cloud. Everything here works from **public** key
material.

Every check fails closed. ``alg`` is pinned to ``ES256`` and ``typ`` to
``act+jwt`` — a token declaring ``none`` or an HMAC algorithm is a forgery
attempt, not something to negotiate — and an unknown ``kid`` is never worked
around by trying another key.

Dependency decision
-------------------
ES256 (ECDSA P-256) verification is the one thing the standard library cannot
do. ``cryptography`` was **not** a dependency of this SDK (it appears in the
lockfile only transitively, behind the optional ``gemini`` extra), and the core
SDK — which sits on the AI request path of every user — must not grow a
mandatory C-extension dependency for a feature most callers never use. So:

* everything except the signature check (JWS structure, base64url, JSON, claim
  types, time window, issuer, constant-time binding comparisons) is pure
  standard library;
* the ECDSA check delegates to ``cryptography``, installed on demand with
  ``pip install idside[verify]``, imported lazily, and raising an actionable
  :class:`~idside.exceptions.ActVerificationUnavailableError` when absent.

Hand-rolling P-256 arithmetic was rejected: signature verification touches no
secret, so a pure-Python implementation would be side-channel-safe, but a
consent product cannot credibly ship bespoke curve code that auditors would
then have to review.

Spec: ``docs/spec/act-token.md``.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal

import httpx

from idside._transport import _USER_AGENT, DEFAULT_TIMEOUT_SECONDS
from idside.exceptions import (
    ActAlgorithmError,
    ActBindingMismatchError,
    ActExpiredError,
    ActIssuerError,
    ActKeyNotFoundError,
    ActMalformedError,
    ActNotYetValidError,
    ActPrivateKeyInKeySetError,
    ActSignatureError,
    ActTypeMismatchError,
    ActVerificationUnavailableError,
)

__all__ = [
    "ACT_ALGORITHM",
    "ACT_TYPE",
    "DEFAULT_LEEWAY_SECONDS",
    "PRODUCTION_ISSUER",
    "TEST_ISSUER",
    "VerifiedAct",
    "verify_act",
]

#: The only algorithm an ACT token may declare. Pinned, never negotiated.
ACT_ALGORITHM: Final = "ES256"

#: The only ``typ`` an ACT token may declare (a UBT is ``ubt+jwt``).
ACT_TYPE: Final = "act+jwt"

#: Issuer of production proofs.
PRODUCTION_ISSUER: Final = "idside.eu"

#: Issuer of development proofs — accepted only on explicit opt-in.
TEST_ISSUER: Final = "test.idside.eu"

#: Clock-skew tolerance applied to ``exp``, ``iat`` and ``nbf``.
DEFAULT_LEEWAY_SECONDS: Final = 60.0

#: Maximum lifetime declared by an ACT, independent of clock-skew leeway.
_MAX_ACT_TTL_SECONDS: Final = 3_600

#: An ACT is at most 3,072 characters by design (the ``deviations`` claim is
#: what can take it past 2 KB); this cap only keeps absurd input from being
#: parsed.
#: Mirrors MAX_TOKEN_CHARS in the Node SDK.
MAX_TOKEN_CHARS: Final = 8_192

_COORDINATE_BYTES: Final = 32
_SIGNATURE_BYTES: Final = 2 * _COORDINATE_BYTES

#: Order of the P-256 base point: a valid ECDSA ``(r, s)`` lies in ``[1, n-1]``.
_P256_ORDER: Final = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

_B64URL_RE: Final = re.compile(r"[A-Za-z0-9_-]*")
_REQUEST_HASH_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")

#: JWK members that hold private key material: ``d`` is the EC/OKP/RSA private
#: exponent or scalar, ``k`` a symmetric key. None of them belongs in a key set.
_PRIVATE_JWK_MEMBERS: Final = ("d", "k")

#: Claims every ACT token must carry, as non-empty strings (see spec §3).
_STRING_CLAIMS: Final = (
    "iss",
    "sub",
    "app_id",
    "jti",
    "legal_basis",
    "source",
    "provider",
    "model",
    "request_hash",
    "nonce",
    "binding",
)

#: The same deadline as the authorize hop: verification is the same
#: fail-closed wait on the same kind of outage, so a caller does not wait a
#: different length of time depending on which of the two it is doing.
_JWKS_TIMEOUT: Final = httpx.Timeout(DEFAULT_TIMEOUT_SECONDS)

_MISSING_EXTRA = (
    "ES256 verification needs the optional backend: "
    "install it with `pip install idside[verify]` "
    "(or `uv add 'idside[verify]'`). The token was NOT accepted."
)


@dataclass(frozen=True)
class VerifiedAct:
    """A verified ACT token — what the proof actually attests.

    Returned only when the signature, the token type, the algorithm, the issuer,
    the time window and every binding the caller pinned have all checked out.
    Its existence is the success signal; a failure is always an exception, never
    a falsy return.

    ``binding`` records how the end user was tied to the request — ``"ubt"``
    (the ID Side widget bound the identity, the strong link) or ``"user_id"``
    (the developer asserted it). It is the evidentiary weight of the proof.
    """

    issuer: str
    subject: str
    app_id: str
    token_id: str
    issued_at: int
    expires_at: int
    consent_state: Mapping[str, Any]
    legal_basis: str
    source: str
    provider: str
    model: str
    request_hash: str
    nonce: str
    binding: str
    mode: Literal["test", "live"] | None
    key_id: str
    claims: Mapping[str, Any]


def verify_act(
    token: str,
    *,
    jwks: Mapping[str, Any] | None = None,
    jwks_url: str | None = None,
    issuer: str | Iterable[str] | None = None,
    allow_test_issuer: bool = False,
    allow_legacy_without_mode: bool = False,
    leeway: float = DEFAULT_LEEWAY_SECONDS,
    now: float | None = None,
    request_hash: str | None = None,
    nonce: str | None = None,
    subject: str | None = None,
    app_id: str | None = None,
) -> VerifiedAct:
    """Verify an ACT token locally and return its claims.

    :param token: the raw ``X-AI-Consent-Token`` value (a three-part JWS).
    :param jwks: a JWKS mapping (``{"keys": [...]}``) or a single JWK. Exactly
        one of ``jwks`` / ``jwks_url`` is required — there is no ambient key
        source and no default endpoint to fall back on.
    :param jwks_url: an ``https://`` URL serving the JWKS (ID Side publishes it
        at ``/.well-known/jwks.json``). Fetched on every call: for repeated
        verification, fetch once yourself and pass ``jwks=`` instead.
    :param issuer: accepted issuer, or several. Defaults to ``idside.eu`` only.
    :param allow_test_issuer: also accept ``test.idside.eu``. Off by default so
        a development token can never silently pass as a production proof.
    :param allow_legacy_without_mode: accept proofs minted before the signed
        ``mode`` claim existed. Off by default; when enabled, ``VerifiedAct.mode``
        is ``None`` for such a proof. Invalid present values are always rejected.
    :param leeway: clock-skew tolerance in seconds for ``exp``/``iat``/``nbf``.
    :param now: verification time, as an epoch in seconds. Defaults to the real
        clock. Set it to audit a past call — *"was this proof valid when the
        call was made?"* — instead of catching :class:`ActExpiredError` on every
        archived token. It moves **only** the time window: the signature, the
        algorithm, the type, the issuer and the bindings are unaffected, so a
        generous ``now`` can never turn a forgery into a proof.
    :param request_hash: pin the request the token must be bound to
        (``"sha256:<hex>"``, RFC 8785/JCS canonical — see ``idside._jcs``).
        **One token = one request**: this is the check that makes a stolen but
        perfectly signed token useless on another payload.
    :param nonce: pin the token's anti-replay ``nonce``.
    :param subject: pin the ``sub`` claim. Note that ``sub`` is the app-scoped
        *pseudonym* the cloud derived from the end user — the raw ``user_id``
        you passed to ``consent.wrap()`` deliberately never enters the token,
        so it cannot be pinned here.
    :param app_id: pin the application the call was authorized for.

    :raises ActVerificationError: (or one of its subclasses) whenever the proof
        does not check out. There is no "valid enough": every failure path
        raises, and a caller that catches nothing is already fail-closed.
    :raises ValueError: for caller mistakes — no key source, two key sources, a
        non-HTTPS ``jwks_url``, a negative ``leeway``, a non-finite ``now``, an
        empty issuer set.

    Replay across *different* calls is not detectable offline (a verifier has
    no history); it is the cloud ledger's job. What a local verifier proves is
    that this signed token belongs to this exact request.
    """
    if (jwks is None) == (jwks_url is None):
        raise ValueError(
            "verify_act() needs exactly one key source: pass jwks= (a JWKS or a "
            "single JWK) or jwks_url= (an https:// URL), not both and not neither."
        )
    if leeway < 0:
        raise ValueError("verify_act() leeway must be zero or positive seconds.")
    if now is not None and not math.isfinite(now):
        raise ValueError("verify_act() now must be a finite epoch in seconds.")
    verification_time = time.time() if now is None else now
    expected_issuers = _expected_issuers(issuer, allow_test_issuer=allow_test_issuer)

    header_segment, payload_segment, signature_segment = _split(token)
    header = _json_object(
        _b64url_decode(header_segment, what="JOSE header"), what="JOSE header"
    )
    _check_algorithm(header)
    _check_type(header)
    _check_no_critical_extensions(header)
    key_id = _key_id(header)

    # Decoded (not parsed) now: this validates the payload character set before
    # it is fed to the signature check. Its content stays untrusted until then.
    payload_bytes = _b64url_decode(payload_segment, what="payload")
    signature = _b64url_decode(signature_segment, what="signature")

    if jwks_url is not None:
        key_set = _fetch_jwks(jwks_url)
    elif jwks is not None:
        key_set = jwks
    else:  # pragma: no cover - the XOR check above already rejected this
        raise ValueError("verify_act() needs a jwks= or jwks_url= key source.")
    jwk = _select_key(key_set, key_id)

    _check_signature(
        signing_input=f"{header_segment}.{payload_segment}".encode("ascii"),
        signature=signature,
        jwk=jwk,
    )

    # Claims are read only now: nothing above this line trusted the payload.
    claims = _json_object(payload_bytes, what="payload")
    strings = {name: _require_string(claims, name) for name in _STRING_CLAIMS}
    issued_at = _require_timestamp(claims, "iat")
    expires_at = _require_timestamp(claims, "exp")
    consent_state = _require_object(claims, "consent_state")
    if not _REQUEST_HASH_RE.fullmatch(strings["request_hash"]):
        raise ActMalformedError(
            "Claim 'request_hash' is not a canonical 'sha256:<64 hex chars>' "
            "digest; the token does not follow the ACT specification."
        )
    if expires_at <= issued_at:
        raise ActMalformedError(
            "Claim 'exp' is not after 'iat': the token declares an empty or "
            "inverted validity window."
        )
    if expires_at - issued_at > _MAX_ACT_TTL_SECONDS:
        raise ActMalformedError(
            "Claims 'iat' and 'exp' declare a validity window longer than "
            "3,600 seconds; an ACT cannot live longer than one hour."
        )
    raw_mode = claims.get("mode")
    mode: Literal["test", "live"] | None
    if raw_mode is None and "mode" not in claims:
        if not allow_legacy_without_mode:
            raise ActMalformedError(
                "Missing required claim 'mode'. For a proof minted before this "
                "claim existed, pass allow_legacy_without_mode=True explicitly."
            )
        mode = None
    elif raw_mode == "test":
        mode = "test"
    elif raw_mode == "live":
        mode = "live"
    else:
        raise ActMalformedError("Claim 'mode' must be the string 'test' or 'live'.")

    if strings["iss"] not in expected_issuers:
        raise ActIssuerError(
            f"Issuer {strings['iss']!r} is not accepted "
            f"(expected {' or '.join(sorted(expected_issuers))}). "
            "A token from another environment is not a proof for this one."
        )
    _check_time_window(
        claims,
        issued_at=issued_at,
        expires_at=expires_at,
        leeway=leeway,
        now=verification_time,
    )

    for name, expected in (
        ("request_hash", request_hash),
        ("nonce", nonce),
        ("sub", subject),
        ("app_id", app_id),
    ):
        if expected is not None and not _constant_time_equal(strings[name], expected):
            raise ActBindingMismatchError(
                f"Claim {name!r} does not match the value pinned by the caller: "
                "this proof was issued for a different request, user or app."
            )

    return VerifiedAct(
        issuer=strings["iss"],
        subject=strings["sub"],
        app_id=strings["app_id"],
        token_id=strings["jti"],
        issued_at=issued_at,
        expires_at=expires_at,
        consent_state=MappingProxyType(dict(consent_state)),
        legal_basis=strings["legal_basis"],
        source=strings["source"],
        provider=strings["provider"],
        model=strings["model"],
        request_hash=strings["request_hash"],
        nonce=strings["nonce"],
        binding=strings["binding"],
        mode=mode,
        key_id=key_id,
        claims=MappingProxyType(dict(claims)),
    )


# ── JWS structure ──────────────────────────────────────────────────────────


def _split(token: str) -> tuple[str, str, str]:
    """Split a compact JWS into its three segments."""
    if not isinstance(token, str):
        raise ActMalformedError(
            f"An ACT token is a string; got {type(token).__name__}."
        )
    # Node caps at the same figure. Without it, `verify_act("A" * 400_000_000 +
    # ".eyJ9.x")` was split, regex-scanned, base64-decoded into ~300 MB and JSON
    # parsed — all before the signature check. A verifier taking tokens from
    # untrusted callers was memory-exhaustible, and the two SDKs disagreed on
    # the same input, which the shared vectors exist to prevent.
    if len(token) > MAX_TOKEN_CHARS:
        # Word for word what Node says: the cap that refused it, and the bound
        # the spec sets, which is not the cap.
        raise ActMalformedError(
            f"The token is implausibly large ({len(token)} characters, limit "
            f"{MAX_TOKEN_CHARS}); an ACT token is at most 3,072 characters. "
            "Refusing to parse it."
        )
    segments = token.split(".")
    if len(segments) != 3 or not all(segments[:2]):
        raise ActMalformedError(
            f"Not a compact JWS: expected 3 non-empty segments, got {len(segments)}. "
            "Pass the raw X-AI-Consent-Token value, untruncated."
        )
    return segments[0], segments[1], segments[2]


def _b64url_decode(segment: str, *, what: str) -> bytes:
    """Decode one JWS segment. Strict: unpadded base64url only, and canonical.

    A segment a compliant encoder would not have produced is a rejection, never
    something to clean up. The last character of a segment whose length is not a
    multiple of 4 carries bits that encode nothing; Python's decoder ignores
    them, so without this check one byte string would have several spellings —
    and a defect in the *encoding* would surface later as a signature failure,
    which names the wrong culprit and pays for a curve operation to say so.
    """
    if not _B64URL_RE.fullmatch(segment):
        raise ActMalformedError(
            f"The {what} is not unpadded base64url (JOSE forbids '=', '+' and '/')."
        )
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ActMalformedError(f"The {what} is not decodable base64url.") from exc
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != segment:
        raise ActMalformedError(
            f"The {what} is not canonical base64url: it carries unused trailing bits."
        )
    return decoded


def _json_object(raw: bytes, *, what: str) -> Mapping[str, Any]:
    """Parse a JWS segment as a JSON object."""
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ActMalformedError(f"The {what} is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise ActMalformedError(f"The {what} is not a JSON object.")
    return value


# ── header checks ──────────────────────────────────────────────────────────


def _check_algorithm(header: Mapping[str, Any]) -> None:
    """Pin ``alg`` to ES256 — the defence against algorithm confusion."""
    algorithm = header.get("alg")
    if algorithm != ACT_ALGORITHM:
        raise ActAlgorithmError(
            f"Refusing algorithm {algorithm!r}: an ACT token is always "
            f"{ACT_ALGORITHM}. A token declaring anything else — 'none', an HMAC "
            "algorithm, another curve — is a forgery attempt, not a negotiation."
        )


def _check_type(header: Mapping[str, Any]) -> None:
    """Pin ``typ`` to ``act+jwt`` — no other ID Side token stands in for a proof."""
    token_type = header.get("typ")
    if token_type != ACT_TYPE:
        raise ActTypeMismatchError(
            f"Refusing token type {token_type!r}: expected {ACT_TYPE!r}. "
            "A User Binding Token ('ubt+jwt') or any other ID Side token is not "
            "a proof that an AI call was authorized."
        )


def _check_no_critical_extensions(header: Mapping[str, Any]) -> None:
    """Reject ``crit``: this verifier understands no JOSE extension."""
    if "crit" in header:
        raise ActMalformedError(
            "The JOSE header declares 'crit' extensions this verifier does not "
            "understand; RFC 7515 requires rejecting the token."
        )


def _key_id(header: Mapping[str, Any]) -> str:
    """Read ``kid``. Absent means no key selection is possible — fail closed."""
    key_id = header.get("kid")
    if not isinstance(key_id, str) or not key_id:
        raise ActMalformedError(
            "The JOSE header carries no 'kid', so no verification key can be "
            "selected. ID Side always names the signing key."
        )
    return key_id


# ── key material ───────────────────────────────────────────────────────────


def _jwks_http_client() -> httpx.Client:
    """Build the client used to fetch a JWKS (seam for tests)."""
    return httpx.Client(timeout=_JWKS_TIMEOUT, follow_redirects=False)


def _fetch_jwks(url: str) -> Mapping[str, Any]:
    """Fetch a JWKS over HTTPS. Anything unusable fails closed."""
    if not url.lower().startswith("https://"):
        raise ValueError(
            "verify_act() only fetches a JWKS over https:// — public keys "
            f"retrieved over an unauthenticated channel prove nothing; got {url!r}."
        )
    try:
        with _jwks_http_client() as client:
            response = client.get(url, headers={"User-Agent": _USER_AGENT})
    except httpx.HTTPError as exc:
        raise ActVerificationUnavailableError(
            f"The JWKS at {url} is unreachable, so the token could not be "
            "verified; it was NOT accepted."
        ) from exc
    if response.status_code != httpx.codes.OK:
        raise ActVerificationUnavailableError(
            f"The JWKS at {url} answered HTTP {response.status_code}; the token "
            "could not be verified and was NOT accepted."
        )
    try:
        document = response.json()
    except ValueError as exc:
        raise ActVerificationUnavailableError(
            f"The JWKS at {url} is not valid JSON; the token was NOT accepted."
        ) from exc
    if not isinstance(document, dict):
        raise ActVerificationUnavailableError(
            f"The JWKS at {url} is not a JSON object; the token was NOT accepted."
        )
    return document


def _select_key(key_set: Any, key_id: str) -> Mapping[str, Any]:
    """Pick the JWK named by ``kid``. No fallback: unknown key ⇒ rejection."""
    # `verify_act(token, jwks="path/to/keys.json")` is a plausible mistake, and
    # it used to escape the typed hierarchy entirely: `"keys" in key_set` is a
    # SUBSTRING test on a str, so it passed, and `key_set["keys"]` then raised a
    # bare TypeError past every `except ActVerificationError` a fail-closed
    # caller had written. Node rejects both shapes here; so does this now.
    if not isinstance(key_set, Mapping):
        raise ActVerificationUnavailableError(
            "The key set is neither a JWKS object ({'keys': [...]}) nor a single JWK; "
            f"got {type(key_set).__name__}. The token was NOT accepted."
        )
    if "keys" in key_set:
        keys = key_set["keys"]
        if not isinstance(keys, list):
            raise ActVerificationUnavailableError(
                "The JWKS 'keys' member is not an array; the token was NOT accepted."
            )
    else:
        keys = [key_set]  # a bare JWK is accepted as a one-key set
    matches = [
        key for key in keys if isinstance(key, Mapping) and key.get("kid") == key_id
    ]
    if not matches:
        raise ActKeyNotFoundError(
            f"No key with kid={key_id!r} in the supplied key set. The signing key "
            "may have been rotated: refresh the JWKS from /.well-known/jwks.json. "
            "No other key was tried."
        )
    if len(matches) > 1:
        raise ActVerificationUnavailableError(
            f"The key set is ambiguous: {len(matches)} keys share kid={key_id!r}. "
            "Verification needs one unmistakable key; the token was NOT accepted."
        )
    return _check_key_usable(matches[0], key_id)


def _check_key_usable(jwk: Mapping[str, Any], key_id: str) -> Mapping[str, Any]:
    """Reject a matching JWK that cannot verify an ES256 signature.

    Private key material is checked first, before anything else that may be
    wrong with the key: when someone's signing key is sitting in a key set, that
    is the headline, not the fact that it also has the wrong curve.
    """
    leaked = next((member for member in _PRIVATE_JWK_MEMBERS if member in jwk), None)
    if leaked is not None:
        raise ActPrivateKeyInKeySetError(
            f"The key with kid={key_id!r} cannot verify an ACT signature: it "
            f"carries private key material ({leaked!r}), which never belongs in "
            "a key set. A key set is public by definition, so this is either a "
            "signing key published by mistake or a poisoned key set: remove the "
            "private members from the JWKS and rotate the key. The token was "
            "NOT accepted."
        )
    reason: str | None = None
    if jwk.get("kty") != "EC":
        reason = f"key type {jwk.get('kty')!r} is not 'EC'"
    elif jwk.get("crv") != "P-256":
        reason = f"curve {jwk.get('crv')!r} is not 'P-256'"
    # `jwk.get(x)` cannot tell an absent member from an explicit JSON null, and
    # the two SDKs disagreed because of it: `{"alg": null}` was accepted here and
    # refused by Node. Absence is permissive, a stated null is not.
    elif "alg" in jwk and jwk["alg"] != ACT_ALGORITHM:
        reason = f"the key declares alg {jwk.get('alg')!r}"
    elif "use" in jwk and jwk["use"] != "sig":
        reason = f"the key is marked for {jwk.get('use')!r}, not signatures"
    elif "key_ops" in jwk and (
        not isinstance(key_ops := jwk["key_ops"], list) or "verify" not in key_ops
    ):
        reason = "the key does not allow the 'verify' operation"
    if reason is not None:
        raise ActKeyNotFoundError(
            f"The key with kid={key_id!r} cannot verify an ACT signature: {reason}."
        )
    return jwk


def _coordinate(jwk: Mapping[str, Any], name: str, key_id: str) -> int:
    """Decode a P-256 affine coordinate from its fixed-width JWK member."""
    value = jwk.get(name)
    if not isinstance(value, str):
        raise ActKeyNotFoundError(
            f"The key with kid={key_id!r} has no {name!r} coordinate."
        )
    try:
        raw = _b64url_decode(value, what=f"key coordinate {name!r}")
    except ActMalformedError as exc:
        raise ActKeyNotFoundError(
            f"The key with kid={key_id!r} has an undecodable {name!r} coordinate."
        ) from exc
    if len(raw) != _COORDINATE_BYTES:
        raise ActKeyNotFoundError(
            f"The key with kid={key_id!r} has a {len(raw)}-byte {name!r} "
            f"coordinate; P-256 requires exactly {_COORDINATE_BYTES}."
        )
    return int.from_bytes(raw, "big")


# ── signature ──────────────────────────────────────────────────────────────


def _load_es256_backend() -> Callable[[bytes, bytes, int, int], bool]:
    """Import the optional ES256 backend and return a verification callable.

    Imported lazily and isolated in one function: callers who never verify a
    token do not pay for ``cryptography``, and tests can simulate the extra
    being absent. Raises ``ImportError`` when ``idside[verify]`` is missing.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    def verify(signing_input: bytes, signature: bytes, x: int, y: int) -> bool:
        r = int.from_bytes(signature[:_COORDINATE_BYTES], "big")
        s = int.from_bytes(signature[_COORDINATE_BYTES:], "big")
        if not (0 < r < _P256_ORDER and 0 < s < _P256_ORDER):
            return False  # outside [1, n-1]: not an ECDSA signature at all
        try:
            public_key = ec.EllipticCurvePublicNumbers(
                x, y, ec.SECP256R1()
            ).public_key()
        except ValueError as exc:
            raise ActKeyNotFoundError(
                "The public key in the JWKS is not a valid P-256 point."
            ) from exc
        try:
            public_key.verify(
                encode_dss_signature(r, s),
                signing_input,
                ec.ECDSA(hashes.SHA256()),
            )
        except InvalidSignature:
            return False
        return True

    return verify


def _check_signature(
    *, signing_input: bytes, signature: bytes, jwk: Mapping[str, Any]
) -> None:
    """Verify the ES256 signature over ``header.payload``."""
    try:
        verify = _load_es256_backend()
    except ImportError as exc:
        raise ActVerificationUnavailableError(_MISSING_EXTRA) from exc
    if len(signature) != _SIGNATURE_BYTES:
        raise ActSignatureError(
            f"An ES256 signature is {_SIGNATURE_BYTES} bytes (r||s); this token "
            f"carries {len(signature)}."
        )
    key_id = str(jwk.get("kid"))
    if not verify(
        signing_input,
        signature,
        _coordinate(jwk, "x", key_id),
        _coordinate(jwk, "y", key_id),
    ):
        raise ActSignatureError(
            f"The signature does not match the token's header and payload under "
            f"the key kid={key_id!r}: the token was altered after signing, or was "
            "never signed by ID Side."
        )


# ── claims ─────────────────────────────────────────────────────────────────


def _require_string(claims: Mapping[str, Any], name: str) -> str:
    value = _require_present(claims, name)
    if not isinstance(value, str) or not value:
        raise ActMalformedError(
            f"Claim {name!r} must be a non-empty string; got {value!r}."
        )
    return value


def _require_timestamp(claims: Mapping[str, Any], name: str) -> int:
    value = _require_present(claims, name)
    # bool is an int subclass — True is not a point in time.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ActMalformedError(
            f"Claim {name!r} must be an integer epoch timestamp; got {value!r}."
        )
    return value


def _require_object(claims: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = _require_present(claims, name)
    if not isinstance(value, dict):
        raise ActMalformedError(f"Claim {name!r} must be a JSON object.")
    return value


def _require_present(claims: Mapping[str, Any], name: str) -> Any:
    if name not in claims:
        raise ActMalformedError(
            f"The token carries no {name!r} claim, so it is not a complete ACT "
            "token (see docs/spec/act-token.md §3)."
        )
    return claims[name]


def _check_time_window(
    claims: Mapping[str, Any],
    *,
    issued_at: int,
    expires_at: int,
    leeway: float,
    now: float,
) -> None:
    """Enforce ``exp``, ``iat`` and — when present — ``nbf`` as of ``now``."""
    if now > expires_at + leeway:
        raise ActExpiredError(
            f"The token expired at epoch {expires_at} "
            f"({int(now - expires_at)} s ago): it proves nothing about a call "
            "made now. ACT tokens live one hour."
        )
    if now < issued_at - leeway:
        raise ActNotYetValidError(
            f"Claim 'iat' is {int(issued_at - now)} s in the future. Check this "
            "machine's clock before suspecting the token."
        )
    if "nbf" in claims:
        not_before = _require_timestamp(claims, "nbf")
        if now < not_before - leeway:
            raise ActNotYetValidError(
                f"Claim 'nbf' is {int(not_before - now)} s in the future: the "
                "token is not valid yet."
            )


def _expected_issuers(
    issuer: str | Iterable[str] | None, *, allow_test_issuer: bool
) -> frozenset[str]:
    """Resolve the accepted issuers; the test issuer needs an explicit opt-in."""
    if issuer is None:
        expected = {PRODUCTION_ISSUER}
    elif isinstance(issuer, str):
        expected = {issuer}
    else:
        expected = set(issuer)
    if allow_test_issuer:
        expected.add(TEST_ISSUER)
    if not expected or not all(expected):
        raise ValueError(
            "verify_act() needs at least one non-empty expected issuer; "
            "an empty issuer= would accept a token from anywhere."
        )
    return frozenset(expected)


def _constant_time_equal(claimed: str, expected: str) -> bool:
    """Compare two claim values without leaking a match position by timing."""
    return hmac.compare_digest(claimed.encode("utf-8"), expected.encode("utf-8"))
