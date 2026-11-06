"""Tests for local, offline ACT token verification (``idside.verify_act``).

All key material here is generated in memory, per test session, and never
written to disk: it is throwaway test material, not a secret. The repository
holds no signing key — see docs/concepts.md §6, "The SDK cannot forge one".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import string
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import httpx
import pytest

from idside import VerifiedAct, verify_act
from idside import verify as verify_module
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
    ActVerificationError,
    ActVerificationUnavailableError,
    ConsentTokenError,
    IDSideError,
)

pytest.importorskip("cryptography", reason="ES256 verification needs idside[verify]")

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
)

# ── throwaway test keys ────────────────────────────────────────────────────

_KEY = ec.generate_private_key(ec.SECP256R1())
_OTHER_KEY = ec.generate_private_key(ec.SECP256R1())
_KID = "act-2026q3"
_OTHER_KID = "act-2026q2"


def _b64u(data: bytes) -> str:
    """Unpadded base64url, as JOSE requires."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwk(kid: str, key: ec.EllipticCurvePrivateKey, **overrides: Any) -> dict[str, Any]:
    numbers = key.public_key().public_numbers()
    jwk: dict[str, Any] = {
        "kty": "EC",
        "crv": "P-256",
        "kid": kid,
        "alg": "ES256",
        "use": "sig",
        "x": _b64u(numbers.x.to_bytes(32, "big")),
        "y": _b64u(numbers.y.to_bytes(32, "big")),
    }
    jwk.update(overrides)
    return jwk


def _jwks(*keys: Mapping[str, Any]) -> dict[str, Any]:
    return {"keys": list(keys or (_jwk(_KID, _KEY), _jwk(_OTHER_KID, _OTHER_KEY)))}


def _header(**overrides: Any) -> dict[str, Any]:
    header = {"alg": "ES256", "typ": "act+jwt", "kid": _KID}
    header.update(overrides)
    return header


def _claims(**overrides: Any) -> dict[str, Any]:
    """A well-formed ACT payload — fictitious identifiers only."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "idside.eu",
        "sub": "psu_marie_9f2c1a",  # app-scoped pseudonym, never a raw user id
        "app_id": "app_medhelp",
        "iat": now,
        "exp": now + 3600,
        "jti": "act_01hxy7kq9r",
        "consent_state": {"training": False},
        "legal_basis": "consent",
        "source": "widget",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "request_hash": "sha256:" + "ab" * 32,
        "nonce": "nce_7c4e21",
        "binding": "ubt",
        "mode": "live",
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not _ABSENT}


class _Absent:
    """Sentinel: pass ``_ABSENT`` to drop a claim entirely."""


_ABSENT = _Absent()


def _signing_input(header: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    parts = [
        _b64u(json.dumps(part, separators=(",", ":")).encode("utf-8"))
        for part in (header, payload)
    ]
    return ".".join(parts)


def _es256(
    header: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    *,
    key: ec.EllipticCurvePrivateKey = _KEY,
) -> str:
    """Mint an ES256 JWS. Test-only signing with an ephemeral key."""
    signing_input = _signing_input(
        _header() if header is None else header,
        _claims() if payload is None else payload,
    )
    der = key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{_b64u(raw)}"


def _verify(token: str, **kwargs: Any) -> VerifiedAct:
    kwargs.setdefault("jwks", _jwks())
    return verify_act(token, **kwargs)


# ── happy path ─────────────────────────────────────────────────────────────


def test_valid_token_returns_the_verified_claims() -> None:
    verified = _verify(_es256())

    assert isinstance(verified, VerifiedAct)
    assert verified.issuer == "idside.eu"
    assert verified.subject == "psu_marie_9f2c1a"
    assert verified.app_id == "app_medhelp"
    assert verified.token_id == "act_01hxy7kq9r"
    assert verified.consent_state == {"training": False}
    assert verified.legal_basis == "consent"
    assert verified.source == "widget"
    assert verified.provider == "openai"
    assert verified.model == "gpt-4o-mini"
    assert verified.request_hash == "sha256:" + "ab" * 32
    assert verified.nonce == "nce_7c4e21"
    assert verified.binding == "ubt"
    assert verified.mode == "live"
    assert verified.key_id == _KID


def test_test_mode_is_typed_on_the_verified_proof() -> None:
    assert _verify(_es256(payload=_claims(mode="test"))).mode == "test"


@pytest.mark.parametrize("mode", ["production", "", 7, False, None, {}])
def test_an_invalid_mode_is_rejected(mode: object) -> None:
    with pytest.raises(ActMalformedError, match="mode"):
        _verify(_es256(payload=_claims(mode=mode)))


def test_a_legacy_proof_without_mode_needs_an_explicit_opt_in() -> None:
    token = _es256(payload=_claims(mode=_ABSENT))

    with pytest.raises(ActMalformedError, match="mode"):
        _verify(token)

    assert _verify(token, allow_legacy_without_mode=True).mode is None


def test_an_act_validity_window_cannot_exceed_one_hour() -> None:
    issued = int(time.time())
    token = _es256(payload=_claims(iat=issued, exp=issued + 3601))

    with pytest.raises(ActMalformedError, match="3,600"):
        _verify(token)


def test_verified_act_exposes_the_full_claim_set_and_is_frozen() -> None:
    verified = _verify(_es256())

    assert verified.claims["jti"] == "act_01hxy7kq9r"
    assert verified.expires_at == verified.issued_at + 3600
    with pytest.raises(AttributeError):
        verified.issuer = "evil.example"  # type: ignore[misc]
    with pytest.raises(TypeError):
        verified.claims["iss"] = "evil.example"  # type: ignore[index]


def test_a_single_jwk_is_accepted_in_place_of_a_jwks() -> None:
    assert _verify(_es256(), jwks=_jwk(_KID, _KEY)).key_id == _KID


def test_the_right_key_is_picked_out_of_a_multi_key_jwks() -> None:
    token = _es256(_header(kid=_OTHER_KID), key=_OTHER_KEY)

    assert _verify(token).key_id == _OTHER_KID


def test_unknown_extra_claims_are_preserved_not_rejected() -> None:
    verified = _verify(_es256(payload=_claims(ledger_seq=41)))

    assert verified.claims["ledger_seq"] == 41


def test_a_proof_flagged_by_its_organization_verifies_with_its_deviations() -> None:
    """An Observe proof: the call went out, and the token says what did not apply.

    `deviations` is a claim added under `/v1`, so a verifier released before it
    must accept it and keep it: refusing it would make every flagged call
    unverifiable, and dropping it would hide the one thing an auditor reads
    such a proof for.
    """
    deviations = [
        {"kind": "provider_not_allowed"},
        {"kind": "option_not_enforced", "option": "training.foundation"},
        {"kind": "basis_not_established"},
    ]
    token = _es256(
        payload=_claims(
            source="default_policy",
            legal_basis="not_established",
            binding="user_id",
            deviations=deviations,
        )
    )

    verified = _verify(token)

    assert verified.claims["deviations"] == deviations
    assert verified.legal_basis == "not_established"


@pytest.mark.parametrize(
    "legal_basis",
    ["public_task", "legal_obligation", "vital_interests", "a_basis_added_later"],
)
def test_a_legal_basis_added_later_verifies_as_signed(legal_basis: str) -> None:
    """The verifier reads `legal_basis` as an opaque string, as the spec says.

    A proof signed on a basis added after this release must still verify: an
    enumeration here would make every such proof unverifiable, with no SDK
    release able to arrive first.
    """
    token = _es256(
        payload=_claims(
            source="default_policy", legal_basis=legal_basis, binding="user_id"
        )
    )

    assert _verify(token).legal_basis == legal_basis


# ── algorithm pinning (JWT confusion attacks) ──────────────────────────────


def test_alg_none_is_rejected() -> None:
    unsigned = _signing_input(_header(alg="none"), _claims())

    with pytest.raises(ActAlgorithmError, match="none"):
        _verify(f"{unsigned}.")


def test_alg_hs256_signed_with_the_public_key_is_rejected() -> None:
    """The classic confusion attack: forge an HMAC with the *public* key."""
    public_pem = _KEY.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signing_input = _signing_input(_header(alg="HS256"), _claims())
    forged = hmac.new(public_pem, signing_input.encode("ascii"), hashlib.sha256)

    with pytest.raises(ActAlgorithmError, match="HS256"):
        _verify(f"{signing_input}.{_b64u(forged.digest())}")


@pytest.mark.parametrize("alg", ["ES384", "RS256", "EdDSA", "es256", "ES256 ", ""])
def test_any_algorithm_other_than_es256_is_rejected(alg: str) -> None:
    with pytest.raises(ActAlgorithmError):
        _verify(_es256(_header(alg=alg)))


def test_a_missing_alg_header_is_rejected() -> None:
    with pytest.raises(ActAlgorithmError):
        _verify(_es256({"typ": "act+jwt", "kid": _KID}))


def test_a_non_string_alg_header_is_rejected() -> None:
    with pytest.raises(ActAlgorithmError):
        _verify(_es256(_header(alg=256)))


# ── token type ─────────────────────────────────────────────────────────────


def test_a_ubt_presented_as_an_act_is_rejected() -> None:
    with pytest.raises(ActTypeMismatchError, match="ubt\\+jwt"):
        _verify(_es256(_header(typ="ubt+jwt")))


@pytest.mark.parametrize("typ", ["JWT", "act+JWT", "", "widget+jwt"])
def test_any_type_other_than_act_jwt_is_rejected(typ: str) -> None:
    with pytest.raises(ActTypeMismatchError):
        _verify(_es256(_header(typ=typ)))


def test_a_missing_typ_header_is_rejected() -> None:
    with pytest.raises(ActTypeMismatchError):
        _verify(_es256({"alg": "ES256", "kid": _KID}))


def test_a_crit_header_is_rejected_because_no_extension_is_understood() -> None:
    with pytest.raises(ActMalformedError, match="crit"):
        _verify(_es256(_header(crit=["exp"])))


# ── key selection (fail-closed) ────────────────────────────────────────────


def test_an_unknown_kid_is_rejected_without_trying_other_keys() -> None:
    with pytest.raises(ActKeyNotFoundError, match="act-2029q1"):
        _verify(_es256(_header(kid="act-2029q1")))


def test_a_missing_kid_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(ActMalformedError, match="kid"):
        _verify(_es256({"alg": "ES256", "typ": "act+jwt"}))


def test_a_kid_matching_a_non_ec_key_is_rejected() -> None:
    jwks = {"keys": [{"kty": "RSA", "kid": _KID, "n": "AQAB", "e": "AQAB"}]}

    with pytest.raises(ActKeyNotFoundError, match="EC"):
        verify_act(_es256(), jwks=jwks)


def test_a_kid_matching_the_wrong_curve_is_rejected() -> None:
    jwks = _jwks(_jwk(_KID, _KEY, crv="P-384"))

    with pytest.raises(ActKeyNotFoundError, match="P-256"):
        verify_act(_es256(), jwks=jwks)


def test_a_jwk_declaring_another_algorithm_is_rejected() -> None:
    jwks = _jwks(_jwk(_KID, _KEY, alg="ES384"))

    with pytest.raises(ActKeyNotFoundError):
        verify_act(_es256(), jwks=jwks)


def test_an_encryption_only_jwk_is_rejected() -> None:
    jwks = _jwks(_jwk(_KID, _KEY, use="enc"))

    with pytest.raises(ActKeyNotFoundError):
        verify_act(_es256(), jwks=jwks)


def test_a_jwk_with_malformed_coordinates_is_rejected() -> None:
    jwks = _jwks(_jwk(_KID, _KEY, x="!!not base64!!"))

    with pytest.raises(ActKeyNotFoundError):
        verify_act(_es256(), jwks=jwks)


def test_a_jwk_whose_point_is_not_on_the_curve_is_rejected() -> None:
    jwks = _jwks(_jwk(_KID, _KEY, y=_b64u((7).to_bytes(32, "big"))))

    with pytest.raises(ActKeyNotFoundError):
        verify_act(_es256(), jwks=jwks)


def test_a_duplicated_kid_is_ambiguous_and_rejected() -> None:
    jwks = _jwks(_jwk(_KID, _KEY), _jwk(_KID, _OTHER_KEY))

    with pytest.raises(ActVerificationUnavailableError, match="ambiguous"):
        verify_act(_es256(), jwks=jwks)


@pytest.mark.parametrize("jwks", [{}, {"keys": {}}, {"keys": [None]}, {"keys": []}])
def test_an_unusable_jwks_is_rejected(jwks: Any) -> None:
    with pytest.raises(ActVerificationError):
        verify_act(_es256(), jwks=jwks)


@pytest.mark.parametrize(
    "jwks", ["/etc/idside/jwks.json", [{"kid": _KID}], 7], ids=["path", "list", "int"]
)
def test_a_key_set_that_is_not_a_mapping_is_rejected_in_the_typed_hierarchy(
    jwks: Any,
) -> None:
    """`jwks="path/to/keys.json"` is a plausible mistake with an untyped escape.

    `"keys" in key_set` is a SUBSTRING test on a str, so a path passed by
    mistake got that far and then raised a bare `TypeError` past every `except
    ActVerificationError` a fail-closed caller had written. Whatever is wrong
    with a key source, the caller must learn it as an ID Side error.
    """
    with pytest.raises(ActVerificationUnavailableError, match="NOT accepted"):
        verify_act(_es256(), jwks=jwks)


def test_a_jwk_that_forbids_verification_is_not_used_to_verify() -> None:
    """`key_ops` is the key owner saying what the key may do; it is obeyed.

    A key published for signing only is not a key for checking signatures, and
    a verifier that reads `use` but ignores `key_ops` obeys half of what the
    JWKS says about it.
    """
    for key_ops in (["sign"], "verify", None):
        jwks = _jwks(_jwk(_KID, _KEY, key_ops=key_ops))

        with pytest.raises(ActKeyNotFoundError, match="verify"):
            verify_act(_es256(), jwks=jwks)


def test_a_jwk_missing_a_coordinate_is_rejected() -> None:
    """A key with no point on it cannot verify anything; say which member.

    `None` rather than a dropped member on purpose: an explicit JSON null is
    what a key-set generator emits when it has nothing to put there, and it must
    not reach the curve arithmetic as a coordinate.
    """
    for coordinate in ("x", "y"):
        jwks = _jwks(_jwk(_KID, _KEY, **{coordinate: None}))

        with pytest.raises(ActKeyNotFoundError, match=coordinate):
            verify_act(_es256(), jwks=jwks)


def test_a_jwk_coordinate_of_the_wrong_width_is_rejected() -> None:
    """P-256 coordinates are 32 bytes, and a shorter one is not zero-padded.

    Decodable, canonical base64url and still not a P-256 coordinate. Accepting
    it would mean reading a different number than the key's owner published —
    `int.from_bytes` is happy with any width — and verifying against a point
    nobody signed with.
    """
    jwks = _jwks(_jwk(_KID, _KEY, x=_b64u((1).to_bytes(31, "big"))))

    with pytest.raises(ActKeyNotFoundError, match="31-byte"):
        verify_act(_es256(), jwks=jwks)


# ── private key material in a key set ──────────────────────────────────────
#
# The checks below key on the *presence* of a private member, never its value,
# so the fixtures use obvious placeholders rather than a real private scalar.


def test_a_leaked_key_is_not_caught_by_the_refresh_and_retry_handler() -> None:
    """The property this class exists for — a refactor could silently undo it.

    ``except ActKeyNotFoundError: refresh_and_retry()`` is the natural handler
    for a rotated key. If a poisoned key set were catchable by it, that handler
    would loop on the JWKS through the very incident it should escalate.
    """
    assert not issubclass(ActPrivateKeyInKeySetError, ActKeyNotFoundError)
    assert issubclass(ActPrivateKeyInKeySetError, ActVerificationError)

    leaked = _jwk(_KID, _KEY, d=_b64u(bytes(32)))
    with pytest.raises(ActPrivateKeyInKeySetError):
        verify_act(_es256(), jwks=_jwks(leaked))


def test_a_jwk_carrying_a_private_scalar_is_rejected() -> None:
    """A key set is public by definition: a private scalar in it is a leak."""
    leaked = _jwk(_KID, _KEY, d=_b64u(bytes(32)))

    with pytest.raises(ActPrivateKeyInKeySetError, match="private key material"):
        verify_act(_es256(), jwks=_jwks(leaked))


def test_a_jwk_carrying_symmetric_key_material_is_rejected() -> None:
    leaked = _jwk(_KID, _KEY, k=_b64u(bytes(32)))

    with pytest.raises(ActPrivateKeyInKeySetError, match="private key material"):
        verify_act(_es256(), jwks=_jwks(leaked))


def test_private_material_is_reported_before_any_other_key_defect() -> None:
    """The leak is the headline, not 'wrong key type' — whatever else is wrong.

    DO NOT delete this as redundant with the other private-key tests. It is the
    only one that can detect the *ordering* of the check inside
    ``_check_key_usable``: every other fixture is an otherwise-valid EC P-256
    key, so no ``reason`` is ever set and the check fires wherever it sits. This
    key is both leaked *and* the wrong type, which is the one shape that tells
    the two apart. Verified by mutation — relocating the check past the
    ``kty``/``crv`` branches turns this test red, and only this one.

    What the mutation produces is the whole point: "key type 'RSA' is not 'EC'",
    telling an operator to check their key type while their private key sits in
    a public endpoint.
    """
    leaked = {"kty": "RSA", "kid": _KID, "n": "AQAB", "e": "AQAB", "d": "AQAB"}

    with pytest.raises(ActPrivateKeyInKeySetError, match="private key material"):
        verify_act(_es256(), jwks={"keys": [leaked]})


def test_the_private_key_message_says_what_to_do() -> None:
    leaked = _jwk(_KID, _KEY, d=_b64u(bytes(32)))

    with pytest.raises(ActPrivateKeyInKeySetError) as caught:
        verify_act(_es256(), jwks=_jwks(leaked))

    message = str(caught.value)
    assert "rotate" in message
    assert "NOT accepted" in message


def test_a_leaked_key_is_refused_even_though_the_signature_would_verify() -> None:
    """The token is genuine; the key set is the problem. Refuse anyway."""
    public_only = _jwk(_KID, _KEY)
    assert verify_act(_es256(), jwks=_jwks(public_only)).key_id == _KID

    with pytest.raises(ActPrivateKeyInKeySetError):
        verify_act(_es256(), jwks=_jwks({**public_only, "d": _b64u(bytes(32))}))


# ── base64url canonicality ─────────────────────────────────────────────────

#: The RFC 4648 §5 URL-safe alphabet, derived rather than spelled out — a
#: 64-character literal is indistinguishable from a leaked key to a scanner.
_B64URL_ALPHABET = (
    string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
)


def _with_unused_trailing_bits(segment: str) -> str:
    """Set an unused trailing bit: same bytes, non-canonical encoding.

    The last character of a base64url segment whose length is not a multiple of
    4 carries bits that encode nothing. A compliant encoder leaves them at zero;
    a decoder that ignores them lets one byte string have several spellings.
    """
    unused_bits = {0: 0, 2: 4, 3: 2}[len(segment) % 4]
    assert unused_bits, f"segment of length {len(segment)} has no unused bits"
    value = _B64URL_ALPHABET.index(segment[-1])
    assert value % 2 == 0, "canonical segment should have its low bit clear"
    return segment[:-1] + _B64URL_ALPHABET[value | 1]


def test_the_fixture_only_changes_bits_that_encode_nothing() -> None:
    """Guard the guard: the mutation must not alter the decoded bytes."""
    segment = _b64u(b"idside-act")  # 10 bytes: not a whole number of triplets
    mutated = _with_unused_trailing_bits(segment)

    assert mutated != segment
    assert base64.urlsafe_b64decode(mutated + "==") == b"idside-act"


@pytest.mark.parametrize(("index", "what"), [(0, "header"), (2, "signature")])
def test_a_non_canonical_segment_is_malformed_not_a_bad_signature(
    index: int, what: str
) -> None:
    """Name the real defect, and fail before spending any crypto."""
    segments = _es256().split(".")
    segments[index] = _with_unused_trailing_bits(segments[index])

    with pytest.raises(ActMalformedError, match="canonical"):
        _verify(".".join(segments))


def test_a_non_canonical_jwk_coordinate_is_rejected() -> None:
    jwks = _jwks(_jwk(_KID, _KEY, x=_with_unused_trailing_bits(_jwk(_KID, _KEY)["x"])))

    with pytest.raises(ActKeyNotFoundError):
        verify_act(_es256(), jwks=jwks)


# ── signature ──────────────────────────────────────────────────────────────


def test_a_tampered_payload_is_rejected() -> None:
    header, _, signature = _es256().split(".")
    tampered = _b64u(
        json.dumps(_claims(consent_state={"training": True})).encode("utf-8")
    )

    with pytest.raises(ActSignatureError):
        _verify(f"{header}.{tampered}.{signature}")


def test_a_token_signed_by_another_key_is_rejected() -> None:
    with pytest.raises(ActSignatureError):
        _verify(_es256(key=_OTHER_KEY))


def test_a_flipped_signature_bit_is_rejected() -> None:
    header, payload, signature = _es256().split(".")
    raw = bytearray(base64.urlsafe_b64decode(signature + "=="))
    raw[0] ^= 0x01

    with pytest.raises(ActSignatureError):
        _verify(f"{header}.{payload}.{_b64u(bytes(raw))}")


@pytest.mark.parametrize("size", [0, 63, 65, 70])
def test_a_signature_that_is_not_64_bytes_is_rejected(size: int) -> None:
    header, payload, _ = _es256().split(".")

    with pytest.raises(ActSignatureError, match="64"):
        _verify(f"{header}.{payload}.{_b64u(bytes(size))}")


def test_a_zeroed_signature_is_rejected() -> None:
    header, payload, _ = _es256().split(".")

    with pytest.raises(ActSignatureError):
        _verify(f"{header}.{payload}.{_b64u(bytes(64))}")


# ── structure ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "token",
    ["", "not-a-jwt", "a.b", "a.b.c.d", "a.b.c.d.e", ".."],
)
def test_a_token_that_is_not_a_three_part_jws_is_rejected(token: str) -> None:
    with pytest.raises(ActMalformedError):
        _verify(token)


def test_a_non_string_token_is_rejected() -> None:
    with pytest.raises(ActMalformedError):
        _verify(_es256().encode("ascii"))  # type: ignore[arg-type]


def test_a_token_far_longer_than_an_act_is_refused_before_it_is_decoded() -> None:
    """A verifier taking tokens from untrusted callers must not be exhaustible.

    An ACT is at most 3,072 characters by design. Without the cap, a 400 MB
    first segment was split, regex-scanned, base64-decoded into ~300 MB and
    JSON-parsed — all before the signature check, which is the only thing that
    would have rejected it. The cap is the same figure in the Node SDK, so the
    two answer alike on an input neither should have accepted — and in the same
    words: the message names the cap it hit, and the bound the spec sets, which
    is not the cap.
    """
    oversized = "A" * (verify_module.MAX_TOKEN_CHARS + 1) + ".eyJ9.sig"

    with pytest.raises(ActMalformedError) as caught:
        _verify(oversized)

    assert str(caught.value) == (
        f"The token is implausibly large ({len(oversized)} characters, limit "
        f"{verify_module.MAX_TOKEN_CHARS}); an ACT token is at most 3,072 "
        "characters. Refusing to parse it."
    )


def test_a_segment_of_an_impossible_length_is_malformed_not_a_crash() -> None:
    """Base64url in the right alphabet can still encode nothing at all.

    A segment whose length is one more than a multiple of four carries a
    leftover character that no encoder produces: the alphabet check passes and
    the decoder then raises. Left uncaught, a caller's `except
    ActVerificationError` would be walked past by a `binascii.Error` — the same
    class of escape the string-JWKS mistake used to produce.
    """
    _, payload, signature = _es256().split(".")

    with pytest.raises(ActMalformedError, match="decodable"):
        _verify(f"AAAAA.{payload}.{signature}")


def test_padded_base64_is_rejected_because_jose_forbids_it() -> None:
    header, payload, signature = _es256().split(".")

    with pytest.raises(ActMalformedError):
        _verify(f"{header}==.{payload}.{signature}")


def test_standard_base64_alphabet_is_rejected() -> None:
    header, payload, signature = _es256().split(".")

    with pytest.raises(ActMalformedError):
        _verify(f"{header.replace('_', '/').replace('-', '+')}+.{payload}.{signature}")


def test_a_header_that_is_not_json_is_rejected() -> None:
    _, payload, signature = _es256().split(".")

    with pytest.raises(ActMalformedError):
        _verify(f"{_b64u(b'{not json')}.{payload}.{signature}")


def test_a_payload_that_is_not_a_json_object_is_rejected() -> None:
    header, _, _ = _es256().split(".")
    payload_list: Any = ["not", "an", "object"]
    signing_input = f"{header}.{_b64u(json.dumps(payload_list).encode())}"
    der = _KEY.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")

    with pytest.raises(ActMalformedError, match="object"):
        _verify(f"{signing_input}.{_b64u(raw)}")


@pytest.mark.parametrize(
    "claim",
    [
        "iss",
        "sub",
        "app_id",
        "iat",
        "exp",
        "jti",
        "consent_state",
        "legal_basis",
        "source",
        "provider",
        "model",
        "request_hash",
        "nonce",
        "binding",
    ],
)
def test_every_mandatory_claim_is_required(claim: str) -> None:
    with pytest.raises(ActMalformedError, match=claim):
        _verify(_es256(payload=_claims(**{claim: _ABSENT})))


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", 42),
        ("sub", ""),
        ("app_id", None),
        ("iat", "1750000000"),
        ("exp", True),  # bool is an int subclass — still not a timestamp
        ("exp", 1.5),
        ("consent_state", "training=false"),
        ("binding", 1),
    ],
)
def test_a_claim_of_the_wrong_type_is_rejected(claim: str, value: Any) -> None:
    with pytest.raises(ActMalformedError, match=claim):
        _verify(_es256(payload=_claims(**{claim: value})))


def test_a_request_hash_that_is_not_sha256_hex_is_rejected() -> None:
    with pytest.raises(ActMalformedError, match="request_hash"):
        _verify(_es256(payload=_claims(request_hash="md5:deadbeef")))


def test_an_expiry_that_precedes_issuance_is_rejected() -> None:
    now = int(time.time())

    with pytest.raises(ActMalformedError, match="exp"):
        _verify(_es256(payload=_claims(iat=now, exp=now - 1)))


# ── time window ────────────────────────────────────────────────────────────


def test_an_expired_token_is_rejected() -> None:
    now = int(time.time())

    with pytest.raises(ActExpiredError):
        _verify(_es256(payload=_claims(iat=now - 7200, exp=now - 3600)))


def test_a_token_just_past_expiry_is_tolerated_within_the_leeway() -> None:
    now = int(time.time())
    token = _es256(payload=_claims(iat=now - 3630, exp=now - 30))

    assert _verify(token).expires_at == now - 30


def test_the_leeway_can_be_switched_off() -> None:
    now = int(time.time())
    token = _es256(payload=_claims(iat=now - 3630, exp=now - 30))

    with pytest.raises(ActExpiredError):
        _verify(token, leeway=0)


def test_a_negative_leeway_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="leeway"):
        _verify(_es256(), leeway=-1)


def test_a_token_issued_in_the_future_is_rejected() -> None:
    now = int(time.time())

    with pytest.raises(ActNotYetValidError, match="iat"):
        _verify(_es256(payload=_claims(iat=now + 600, exp=now + 4200)))


def test_a_not_before_in_the_future_is_rejected() -> None:
    now = int(time.time())

    with pytest.raises(ActNotYetValidError, match="nbf"):
        _verify(_es256(payload=_claims(nbf=now + 600)))


def test_a_not_before_already_reached_is_accepted() -> None:
    now = int(time.time())

    assert _verify(_es256(payload=_claims(nbf=now - 10))).claims["nbf"] == now - 10


def test_a_non_numeric_not_before_is_rejected() -> None:
    with pytest.raises(ActMalformedError, match="nbf"):
        _verify(_es256(payload=_claims(nbf="soon")))


# ── auditing a past call (now=) ────────────────────────────────────────────


def test_an_expired_token_is_valid_as_of_a_time_inside_its_window() -> None:
    """The audit case: 'was this proof valid when the call was made?'"""
    issued = int(time.time()) - 90 * 86400
    token = _es256(payload=_claims(iat=issued, exp=issued + 3600))

    verified = _verify(token, now=issued + 1800)

    assert verified.issued_at == issued
    assert verified.token_id == "act_01hxy7kq9r"


def test_a_quarter_old_token_is_still_expired_as_of_the_real_clock() -> None:
    issued = int(time.time()) - 90 * 86400
    token = _es256(payload=_claims(iat=issued, exp=issued + 3600))

    with pytest.raises(ActExpiredError):
        _verify(token)


def test_a_token_is_not_yet_valid_as_of_a_time_before_its_nbf() -> None:
    issued = int(time.time()) - 86400
    token = _es256(payload=_claims(iat=issued, exp=issued + 3600, nbf=issued + 600))

    with pytest.raises(ActNotYetValidError, match="nbf"):
        _verify(token, now=issued + 60)

    assert _verify(token, now=issued + 900).claims["nbf"] == issued + 600


def test_a_token_is_not_yet_valid_as_of_a_time_before_its_iat() -> None:
    issued = int(time.time()) - 86400
    token = _es256(payload=_claims(iat=issued, exp=issued + 3600))

    with pytest.raises(ActNotYetValidError, match="iat"):
        _verify(token, now=issued - 600)


def test_now_and_leeway_combine_on_expiry() -> None:
    issued = int(time.time()) - 86400
    expires = issued + 3600
    token = _es256(payload=_claims(iat=issued, exp=expires))

    assert _verify(token, now=expires + 30).expires_at == expires  # inside leeway
    with pytest.raises(ActExpiredError):
        _verify(token, now=expires + 90)  # past the 60 s leeway
    with pytest.raises(ActExpiredError):
        _verify(token, now=expires + 1, leeway=0)


def test_now_and_leeway_combine_on_issuance() -> None:
    issued = int(time.time()) - 86400
    token = _es256(payload=_claims(iat=issued, exp=issued + 3600))

    assert _verify(token, now=issued - 30).issued_at == issued  # inside leeway
    with pytest.raises(ActNotYetValidError):
        _verify(token, now=issued - 90)
    with pytest.raises(ActNotYetValidError):
        _verify(token, now=issued - 1, leeway=0)


def test_now_defaults_to_the_real_clock() -> None:
    assert _verify(_es256(), now=None).issuer == "idside.eu"


def test_a_fractional_now_is_accepted() -> None:
    issued = int(time.time()) - 86400
    token = _es256(payload=_claims(iat=issued, exp=issued + 3600))

    assert _verify(token, now=issued + 1800.5).issued_at == issued


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_now_is_a_programming_error(now: float) -> None:
    with pytest.raises(ValueError, match="now"):
        _verify(_es256(), now=now)


def test_now_does_not_relax_anything_but_the_time_window() -> None:
    """A convenient `now` must not turn a forgery into a proof."""
    issued = int(time.time()) - 86400

    with pytest.raises(ActSignatureError):
        _verify(
            _es256(payload=_claims(iat=issued, exp=issued + 3600), key=_OTHER_KEY),
            now=issued + 60,
        )
    with pytest.raises(ActAlgorithmError):
        _verify(
            _es256(
                _header(alg="HS256"),
                _claims(iat=issued, exp=issued + 3600),
            ),
            now=issued + 60,
        )


# ── issuer ─────────────────────────────────────────────────────────────────


def test_a_foreign_issuer_is_rejected() -> None:
    with pytest.raises(ActIssuerError, match=r"evil\.example"):
        _verify(_es256(payload=_claims(iss="evil.example")))


def test_the_test_issuer_is_rejected_by_default() -> None:
    """A dev token must never silently pass as a production proof."""
    with pytest.raises(ActIssuerError, match=r"test\.idside\.eu"):
        _verify(_es256(payload=_claims(iss="test.idside.eu")))


def test_the_test_issuer_needs_an_explicit_opt_in() -> None:
    token = _es256(payload=_claims(iss="test.idside.eu"))

    assert _verify(token, allow_test_issuer=True).issuer == "test.idside.eu"


def test_the_production_issuer_still_passes_with_the_test_opt_in() -> None:
    assert _verify(_es256(), allow_test_issuer=True).issuer == "idside.eu"


def test_an_explicit_issuer_can_be_pinned() -> None:
    token = _es256(payload=_claims(iss="idside.example"))

    assert _verify(token, issuer="idside.example").issuer == "idside.example"


def test_an_explicit_issuer_replaces_the_default() -> None:
    with pytest.raises(ActIssuerError):
        _verify(_es256(), issuer="idside.example")


def test_several_issuers_can_be_accepted() -> None:
    issuers: Iterable[str] = ("idside.example", "idside.eu")

    assert _verify(_es256(), issuer=issuers).issuer == "idside.eu"


@pytest.mark.parametrize("issuer", ["", [], ("",)])
def test_an_empty_issuer_expectation_is_a_programming_error(issuer: Any) -> None:
    with pytest.raises(ValueError, match="issuer"):
        _verify(_es256(), issuer=issuer)


# ── optional binding checks ────────────────────────────────────────────────


def test_a_matching_request_hash_passes() -> None:
    expected = "sha256:" + "ab" * 32

    assert _verify(_es256(), request_hash=expected).request_hash == expected


def test_a_request_hash_bound_to_another_request_is_rejected() -> None:
    with pytest.raises(ActBindingMismatchError, match="request_hash"):
        _verify(_es256(), request_hash="sha256:" + "cd" * 32)


def test_a_nonce_mismatch_is_rejected() -> None:
    with pytest.raises(ActBindingMismatchError, match="nonce"):
        _verify(_es256(), nonce="nce_other")


def test_a_subject_mismatch_is_rejected() -> None:
    with pytest.raises(ActBindingMismatchError, match="sub"):
        _verify(_es256(), subject="psu_someone_else")


def test_an_app_id_mismatch_is_rejected() -> None:
    with pytest.raises(ActBindingMismatchError, match="app_id"):
        _verify(_es256(), app_id="app_other")


def test_all_bindings_can_be_checked_at_once() -> None:
    verified = _verify(
        _es256(),
        request_hash="sha256:" + "ab" * 32,
        nonce="nce_7c4e21",
        subject="psu_marie_9f2c1a",
        app_id="app_medhelp",
    )

    assert verified.token_id == "act_01hxy7kq9r"


def test_a_non_ascii_binding_value_compares_without_blowing_up() -> None:
    token = _es256(payload=_claims(sub="psu_josé_9f2c1a"))

    assert _verify(token, subject="psu_josé_9f2c1a").subject == "psu_josé_9f2c1a"
    with pytest.raises(ActBindingMismatchError):
        _verify(token, subject="psu_jose_9f2c1a")


# ── JWKS sourcing ──────────────────────────────────────────────────────────


def _mock_jwks_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[], httpx.Client]:
    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    return factory


def test_a_jwks_is_fetched_from_the_supplied_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_jwks())

    monkeypatch.setattr(verify_module, "_jwks_http_client", _mock_jwks_client(handler))
    url = "https://idside.example/.well-known/jwks.json"

    assert verify_act(_es256(), jwks_url=url).key_id == _KID
    assert str(seen[0].url) == url
    assert seen[0].headers["User-Agent"].startswith("idside-python/")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(404),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
)
def test_an_unusable_jwks_response_fails_closed(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    monkeypatch.setattr(
        verify_module,
        "_jwks_http_client",
        _mock_jwks_client(lambda request: response),
    )

    with pytest.raises(ActVerificationUnavailableError):
        verify_act(_es256(), jwks_url="https://idside.example/jwks.json")


def test_an_unreachable_jwks_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    monkeypatch.setattr(verify_module, "_jwks_http_client", _mock_jwks_client(handler))

    with pytest.raises(ActVerificationUnavailableError, match="unreachable"):
        verify_act(_es256(), jwks_url="https://idside.example/jwks.json")


@pytest.mark.parametrize(
    "url",
    ["http://idside.example/jwks.json", "file:///etc/passwd", "idside.example"],
)
def test_a_jwks_url_must_be_https(url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        verify_act(_es256(), jwks_url=url)


def test_a_key_source_is_mandatory() -> None:
    with pytest.raises(ValueError, match="jwks"):
        verify_act(_es256())


def test_two_key_sources_are_ambiguous() -> None:
    with pytest.raises(ValueError, match="jwks"):
        verify_act(_es256(), jwks=_jwks(), jwks_url="https://idside.example/jwks.json")


# ── optional dependency ────────────────────────────────────────────────────


def test_a_missing_verify_extra_fails_closed_with_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> Any:
        raise ImportError("No module named 'cryptography'")

    monkeypatch.setattr(verify_module, "_load_es256_backend", unavailable)

    with pytest.raises(ActVerificationUnavailableError, match=r"idside\[verify\]"):
        _verify(_es256())


# ── exception hierarchy & public surface ───────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
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
    ],
)
def test_every_verification_error_stays_inside_the_hierarchy(
    error: type[Exception],
) -> None:
    assert issubclass(error, ActVerificationError)
    assert issubclass(error, ConsentTokenError)
    assert issubclass(error, IDSideError)


def test_the_verify_module_exposes_verification_only() -> None:
    """Invariant #2: this repository verifies proofs, it never issues them."""
    assert set(verify_module.__all__) == {
        "ACT_ALGORITHM",
        "ACT_TYPE",
        "DEFAULT_LEEWAY_SECONDS",
        "PRODUCTION_ISSUER",
        "TEST_ISSUER",
        "VerifiedAct",
        "verify_act",
    }
    names = dir(verify_module)
    assert not [name for name in names if name.lower().startswith("sign")]
    assert not [name for name in names if name.lower().startswith("issue")]


def test_verify_act_is_exported_from_the_package_root() -> None:
    import idside

    assert idside.verify_act is verify_act
    assert {"verify_act", "VerifiedAct"} <= set(idside.__all__)
