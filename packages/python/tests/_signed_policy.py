"""A stand-in ID Side cloud that signs prompt-cleaning policies, for tests only.

The key is generated in memory for each test run and never written anywhere:
no private key lives in this repository. Verification in the SDK is pure
Python; ``cryptography`` (a dev dependency) only signs here.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from idside._cleaning import content_policy_digest

NOW = 1_800_000_000


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass
class SigningKey:
    kid: str = "content-policy-g1"
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    def jwk(self) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()
        return {
            "kty": "EC",
            "crv": "P-256",
            "kid": self.kid,
            "use": "sig",
            "alg": "ES256",
            "x": b64url(numbers.x.to_bytes(32, "big")),
            "y": b64url(numbers.y.to_bytes(32, "big")),
        }

    def sign(self, signing_input: bytes) -> bytes:
        r, s = decode_dss_signature(
            self.key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        )
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def jws(
    key: SigningKey,
    claims: dict[str, Any],
    *,
    header: dict[str, Any] | None = None,
    tamper: bool = False,
) -> str:
    head = (
        header
        if header is not None
        else {"alg": "ES256", "typ": "content-policy+jwt", "kid": key.kid}
    )
    signing_input = (
        b64url(json.dumps(head).encode()) + "." + b64url(json.dumps(claims).encode())
    )
    signature = key.sign(signing_input.encode())
    if tamper:
        signature = signature[:-1] + bytes([signature[-1] ^ 1])
    return signing_input + "." + b64url(signature)


def policy_document(**overrides: Any) -> dict[str, Any]:
    document = {
        "schema": 1,
        "app_id": "app_medhelp",
        "revision": 7,
        "detectors": {"email": "mask", "credit_card": "block"},
        "attachments": "allow",
        "custom": [
            {
                "kind": "term",
                "value": "Projet Atlas",
                "action": "mask",
                "label": "PROJECT",
            }
        ],
    }
    document.update(overrides)
    return document


def claims_for(
    policy: dict[str, Any], now: int = NOW, **overrides: Any
) -> dict[str, Any]:
    claims = {
        "iss": "idside.eu",
        "app_id": policy.get("app_id"),
        "iat": now - 10,
        "exp": now + 890,
        "digest": content_policy_digest(policy),
        "policy": policy,
    }
    claims.update(overrides)
    return claims


CHALLENGE = {"token": "chl_test_0001", "inputs": ["Mail marie@example.com", "nothing"]}


@dataclass
class Cloud:
    """Answers the four routes an SDK uses, and records what it was sent."""

    key: SigningKey = field(default_factory=SigningKey)
    policy: dict[str, Any] | None = field(default_factory=policy_document)
    answer: dict[str, Any] | None = None  # the whole /v1/content-policy body, when set
    policy_status: int = 200
    jwks_status: int = 200
    events_status: int = 204
    authorize_extra: dict[str, Any] = field(default_factory=dict)
    refresh_after: int = 300
    requests: list[httpx.Request] = field(default_factory=list)

    def policy_answer(self) -> dict[str, Any]:
        if self.answer is not None:
            return self.answer
        if self.policy is None:
            return {"enabled": False, "refresh_after": self.refresh_after}
        return {
            "enabled": True,
            "policy": jws(self.key, claims_for(self.policy)),
            "challenge": CHALLENGE,
            "refresh_after": self.refresh_after,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/content-policy":
            if self.policy_status != 200:
                return httpx.Response(self.policy_status, json={"title": "nope"})
            return httpx.Response(200, json=self.policy_answer())
        if path == "/.well-known/jwks.json":
            if self.jwks_status != 200:
                return httpx.Response(self.jwks_status)
            return httpx.Response(200, json={"keys": [self.key.jwk()]})
        if path == "/v1/content-events":
            return httpx.Response(self.events_status)
        body = json.loads(request.content)
        answer = {
            "decision": "allow",
            "consent_state": {},
            "params_forced": {},
            "mode": "test",
            "act_token": "act.jwt.token",
            "token_id": "act_01k4f9h2m3",
            "expires_at": "2026-09-12T12:00:00Z",
            "source": "default_policy",
            "legal_basis": "legitimate_interest",
            "binding": "user_id" if "user_id" in body else "api_key",
            **self.authorize_extra,
        }
        return httpx.Response(200, json=answer)

    async def handle_async(self, request: httpx.Request) -> httpx.Response:
        return self.handle(request)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]
