"""Replays the shared ACT verification vectors against the Python verifier.

The vectors live in ``packages/descriptors/testdata/act-verification-vectors.json``
and are shared by every SDK. They exist because "the token verifies" must mean
the same thing everywhere: an auditor who checks a proof with the Node SDK and a
customer who checks the same proof with the Python SDK have to reach the same
verdict, or the proof is not a proof — it is an opinion that depends on the
library you happened to install.

Each case pins the *reason* too, not merely accept/reject. Both SDKs use the
same error class names on purpose, so a divergence in taxonomy shows up here as
a failure rather than as a surprise in someone's incident review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from idside import DEFAULT_LEEWAY_SECONDS, PRODUCTION_ISSUER, verify_act

_VECTORS_PATH = (
    Path(__file__).resolve().parents[2]
    / "descriptors"
    / "testdata"
    / "act-verification-vectors.json"
)

_VECTORS: dict[str, Any] = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))
_CASES: list[dict[str, Any]] = _VECTORS["cases"]

# The vectors are language-neutral (snake_case); this is the only place that
# knows how they map onto this SDK's keywords. Python happens to use the vector
# spelling verbatim, which is precisely why the check below is not redundant —
# it would be very easy to add a camelCase-only option and never notice.
_OPTION_MAP = {
    "allow_test_issuer": "allow_test_issuer",
    "request_hash": "request_hash",
    "nonce": "nonce",
    "app_id": "app_id",
    "subject": "subject",
}


# Every field a case may carry. An unknown one means this harness is stale.
_CASE_FIELDS = frozenset(
    {"name", "token", "options", "expect", "why", "jwks", "claims"}
)


def _call(case: dict[str, Any]) -> Any:
    # These fixed tokens were minted before `mode` entered the ACT claim set.
    # Their legacy status is declared in the shared file rather than silently
    # weakening the verifier's default for newly minted proofs.
    options: dict[str, Any] = {
        "allow_legacy_without_mode": _VECTORS["options_defaults"][
            "allow_legacy_without_mode"
        ]
    }

    for field in case:
        # Same reasoning as the option guard below, one level up: a case field
        # this harness does not know about would be ignored in silence, and the
        # case would still run — pinning something other than what it was
        # written to pin.
        assert field in _CASE_FIELDS, (
            f"vector {case['name']!r} carries field {field!r}, "
            "which this harness ignores"
        )
    # Pinned claims are read back only from a token that verifies: on a case
    # expected to fail they would be ignored in silence, like an unknown field.
    assert "claims" not in case or case["expect"] == "valid", (
        f"vector {case['name']!r} pins claims but is expected to fail, "
        "so this harness never reads them"
    )

    for name, value in case["options"].items():
        # An option this SDK does not know about must not be dropped on the
        # floor: the case would then still run, quite possibly still pass, and
        # pin something other than what it claims to pin. That is exactly the
        # failure the vectors exist to prevent, and it would be invisible.
        keyword = _OPTION_MAP.get(name)
        assert keyword is not None, (
            f"vector {case['name']!r} uses option {name!r}, which this SDK does not map"
        )
        options[keyword] = value

    # A case may bring its own key set — that is how the poisoned-JWKS case says
    # "the token is fine, the keys are not".
    jwks = case.get("jwks", _VECTORS["jwks"])

    return verify_act(case["token"], jwks=jwks, now=_VECTORS["now"], **options)


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_vector(case: dict[str, Any]) -> None:
    if case["expect"] == "valid":
        verified = _call(case)
        # The key set actually used, not the file-level one: a valid case is
        # allowed to bring its own, and then that is the kid to expect.
        expected_jwks = case.get("jwks", _VECTORS["jwks"])
        assert verified.key_id == expected_jwks["keys"][0]["kid"]
        # A case may pin claims too: what the verifier hands back must be what
        # was signed, nested values included.
        for name, expected in case.get("claims", {}).items():
            assert verified.claims[name] == expected, case["why"]
        return

    # The exact class IS the assertion below, so catching broadly here is the point.
    with pytest.raises(Exception) as raised:
        _call(case)

    assert type(raised.value).__name__ == case["expect"], case["why"]


def test_the_shared_defaults_are_this_sdks_defaults() -> None:
    """Every case is written against these, and no case states them explicitly.

    If the shared file moved its default leeway or issuer and this SDK did not,
    the cases would still pass here while meaning something different in each
    SDK — precisely the divergence the vectors exist to catch, hiding in the one
    field nothing asserts.
    """
    defaults = _VECTORS["options_defaults"]

    assert defaults["issuer"] == PRODUCTION_ISSUER
    assert defaults["leeway"] == DEFAULT_LEEWAY_SECONDS
    assert defaults["allow_test_issuer"] is False
    assert defaults["allow_legacy_without_mode"] is True


def test_the_vector_file_is_not_silently_empty() -> None:
    """A file that fails to load would make every case above vacuously pass."""
    assert len(_CASES) >= 20
    assert {case["expect"] for case in _CASES} > {"valid"}


# Members that make a JWK a *private* key. `d` is the EC private scalar; the
# rest are the RSA and symmetric equivalents.
_PRIVATE_JWK_MEMBERS = frozenset({"d", "k", "p", "q", "dp", "dq", "qi"})
_PUBLIC_JWK_MEMBERS = frozenset(
    {"kty", "crv", "x", "y", "kid", "use", "alg", "key_ops"}
)


def test_the_verification_key_set_is_public_only() -> None:
    """This repository never contains a signing key, test vectors included.

    The key that produced these tokens was generated to write the file and then
    destroyed. Anything private in the file-level key set would mean the tokens
    here could be minted at will by anyone who cloned the repo.
    """
    assert "PRIVATE KEY" not in _VECTORS_PATH.read_text(encoding="utf-8")

    for jwk in _VECTORS["jwks"]["keys"]:
        assert set(jwk) <= _PUBLIC_JWK_MEMBERS
        assert not set(jwk) & _PRIVATE_JWK_MEMBERS


def test_private_material_appears_only_in_cases_that_must_be_rejected() -> None:
    """One vector deliberately carries a poisoned key set — and only that shape.

    Refusing a JWKS that contains a private key is itself a rule worth pinning,
    so a vector has to be able to present one. What must never happen is private
    material sitting in a case the SDKs are expected to ACCEPT: that would be a
    usable signing key wearing a test's clothes. So the rule is not "no `d`
    anywhere" but "`d` only where the expected outcome is rejection".
    """
    for case in _CASES:
        # A case may carry a JWK SET or a single bare JWK — both verifiers accept
        # the second shape as a one-key set. Looking only inside "keys" let a
        # bare JWK carrying `d` through, and `.gitleaks.toml` exempts this whole
        # file on the strength of this check, so it was the only control left.
        supplied = case.get("jwks")
        candidates = (
            supplied.get("keys", [])
            if isinstance(supplied, dict) and "keys" in supplied
            else ([supplied] if isinstance(supplied, dict) else [])
        )
        for jwk in candidates:
            if set(jwk) & _PRIVATE_JWK_MEMBERS:
                assert case["expect"] != "valid", (
                    f"vector {case['name']!r} carries private key material but is "
                    "expected to verify — that is a signing key in the repository"
                )
