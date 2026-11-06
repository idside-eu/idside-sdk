"""Shared plumbing for the opt-in live provider tests.

These tests are **not** part of the normal suite. Two independent guards keep
them out of a default ``uv run pytest`` and out of CI:

1. ``addopts = "... -m 'not live'"`` in ``pyproject.toml`` deselects them;
2. every test collected under ``tests/live/`` is skipped unless
   ``IDSIDE_LIVE_PROVIDER_TESTS=1``.

Both are applied here, in one place, by
:func:`pytest_collection_modifyitems` — a live test cannot forget its marker.

**What these tests do and do not check.** They deliberately do *not* talk to the
ID Side cloud: a stub :class:`~idside._transport.ConsentClient` supplies a fixed
decision. What is under test is the *provider half* of the contract — that a real
provider API accepts (or rejects) exactly what the descriptors say the SDK
injects, and that the bytes leaving the process are the bytes we intended. Each
test asserts on the **request we sent** (captured off the wire) as well as on the
provider's answer, so it states the contract even when a key has no quota left.

**Cost.** Every call is the smallest one the provider accepts — prompt ``"hi"``,
one output token, the cheapest model. A full run is a fraction of a cent. Keep it
that way: these tests verify parameter plumbing, never model output.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from idside._transport import ConsentDecision

#: Set this to "1" to actually run against the real provider APIs.
LIVE_ENV_VAR = "IDSIDE_LIVE_PROVIDER_TESTS"

#: Per-call HTTP timeout. Generous: some free tiers are slow under load.
TIMEOUT = 60.0

#: The smallest prompt that still exercises the full request path.
TINY_PROMPT = "hi"

#: Fictitious identity, per the repo-wide example-data rule.
USER_ID = "usr_marie_456"

#: Fictitious ACT token: these tests never ask the cloud to sign anything.
FAKE_ACT_TOKEN = "act_live_test_placeholder"

#: Provider answers that mean "this account cannot pay", not "our params are
#: wrong". They must skip, never fail: a dry test key says nothing about whether
#: the provider accepts our consent parameters.
_BILLING_MARKERS = (
    "insufficient_quota",
    "insufficient balance",
    "exceeded your current quota",
    "billing",
)

#: Transient provider-side failures. Also a skip: an outage is not a contract
#: violation.
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Mark everything in this directory ``live``, and skip it unless opted in."""
    opted_in = os.environ.get(LIVE_ENV_VAR) == "1"
    skip = pytest.mark.skip(
        reason=(
            f"live provider tests are opt-in: set {LIVE_ENV_VAR}=1 and export the "
            "provider API keys to run them (they cost real money)"
        )
    )
    here = os.path.dirname(__file__)
    for item in items:
        if not str(item.fspath).startswith(here):
            continue
        item.add_marker(pytest.mark.live)
        if not opted_in:
            item.add_marker(skip)


# ── capturing what we actually sent ────────────────────────────────────────


@dataclass
class SentRequest:
    """One outbound HTTP request, as it left the process."""

    method: str
    url: str
    headers: dict[str, str]
    body: Any

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup (HTTP headers are case-insensitive)."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


@dataclass
class WireLog:
    """Every request an instrumented provider client sent, in order."""

    requests: list[SentRequest] = field(default_factory=list)

    def record(self, request: httpx.Request) -> None:
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = "<non-json body>"
        self.requests.append(
            SentRequest(
                method=request.method,
                url=str(request.url),
                headers=dict(request.headers),
                body=body,
            )
        )

    @property
    def last(self) -> SentRequest:
        assert self.requests, "no HTTP request was sent"
        return self.requests[-1]

    def httpx_client(self, **kwargs: Any) -> httpx.Client:
        """An ``httpx.Client`` that reports every request into this log."""
        return httpx.Client(
            event_hooks={"request": [self.record]}, timeout=TIMEOUT, **kwargs
        )


@pytest.fixture
def wire() -> Iterator[WireLog]:
    """Records the raw HTTP requests an instrumented provider client sends."""
    yield WireLog()


# ── the cloud, stubbed ─────────────────────────────────────────────────────


class StubConsentClient:
    """A :class:`ConsentClient` that answers from memory, never over the network.

    Live tests exercise the *provider* side of the SDK. Standing up the ID Side
    cloud to do so would test the wrong half and make the suite unrunnable
    offline-of-the-cloud. ``params_forced`` is whatever the test wants the cloud
    to have decided.
    """

    def __init__(
        self,
        params_forced: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> None:
        self.params_forced = params_forced if params_forced is not None else {}
        self.constraints = constraints if constraints is not None else {}
        self.calls: list[dict[str, Any]] = []

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
    ) -> ConsentDecision:
        self.calls.append(
            {
                "provider": provider,
                "model": model,
                "request_hash": request_hash,
                "user_id": user_id,
                "ubt": ubt,
                "call_site": call_site,
                "credential_postures": tuple(credential_postures),
            }
        )
        return ConsentDecision(
            consent_state={"training.foundation": False},
            params_forced=dict(self.params_forced),
            act_token=FAKE_ACT_TOKEN,
            source="widget",
            legal_basis="consent",
            binding="user_id",
            constraints=dict(self.constraints),
        )


# ── environment helpers ────────────────────────────────────────────────────


def require_key(name: str) -> str:
    """Return the provider API key from the environment, or skip the test."""
    key = os.environ.get(name)
    if not key:
        pytest.skip(f"{name} is not set: export the provider test keys to run this")
    return key


def skip_if_unaffordable(exc: BaseException) -> None:
    """Skip when a provider refused for billing reasons, else re-raise.

    A dry key produces a 402/429 *before* the request body is validated on both
    OpenAI and DeepSeek, so it tells us nothing about whether our consent
    parameters are acceptable. Failing the test on it would report a billing
    problem as a broken contract; silently passing would hide a real rejection.
    Skipping is the only honest outcome.
    """
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status in _TRANSIENT_STATUSES:
        pytest.skip(f"provider is temporarily unavailable ({status}): {exc}")
    if status in (402, 429) or any(marker in text for marker in _BILLING_MARKERS):
        pytest.skip(f"provider key has no quota or balance, cannot verify: {exc}")
    raise exc


@contextlib.contextmanager
def tolerate_billing() -> Iterator[None]:
    """Run a provider call, swallowing only a billing or outage refusal.

    Lets a test assert on **what it sent** even when the key cannot pay for an
    answer — the request still goes out. Any other exception propagates, so a
    rejected *parameter* still fails the test rather than hiding here.
    """
    try:
        yield
    except Exception as exc:
        skip_if_unaffordable(exc)


def assert_response_ok(response: httpx.Response) -> None:
    """Assert a raw provider response succeeded, skipping on billing/outage."""
    if response.status_code == 200:
        return
    body = response.text.lower()
    if response.status_code in _TRANSIENT_STATUSES:
        pytest.skip(f"provider is temporarily unavailable: {response.status_code}")
    if response.status_code in (402, 429) or any(
        marker in body for marker in _BILLING_MARKERS
    ):
        pytest.skip(f"provider key has no quota or balance: {response.text[:200]}")
    raise AssertionError(
        f"provider rejected the request: {response.status_code} {response.text[:400]}"
    )
