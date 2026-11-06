"""What the cloud may force must be something the endpoint actually accepts.

This file exists because of a real bug. The cloud's provider registry forced
``store: false`` for Gemini, copied across from the OpenAI rule. Gemini has no
such parameter: ``google-genai``'s ``generate_content()`` takes exactly
``model``, ``contents`` and ``config``, so the SDK raised ``TypeError:
unexpected keyword argument 'store'`` — on every Gemini call, for every user who
had opted out of training, and for nobody else. The feature broke precisely for
the people it exists to protect, which is why the default path never showed it.

Each descriptor declares ``forceable_params`` **per call site**: the allowlist of
what the cloud may inject on that endpoint. Per site, not per provider, because
the two OpenAI endpoints do not accept the same parameters — ``background``
exists on ``/v1/responses`` and nowhere else. A provider-wide allowlist could
only be their intersection, which loses the lever, or their union, which is the
Gemini bug again.

It is a contract *for the cloud* — the SDK deliberately does not enforce it at
runtime, because policy lives in the cloud and an old SDK silently dropping a
newly-legitimate parameter would defeat the whole point of that split. What the
SDK does instead is prove the allowlist is honest, here, against the real client
libraries: offline, by inspecting signatures, calling nothing and costing
nothing.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

_DESCRIPTOR_DIR = Path(__file__).resolve().parents[2] / "descriptors" / "descriptors"
_DESCRIPTORS: list[dict[str, Any]] = sorted(
    (
        json.loads(path.read_text(encoding="utf-8"))
        for path in _DESCRIPTOR_DIR.glob("*.json")
    ),
    key=lambda d: d["name"],
)
_IDS = [d["name"] for d in _DESCRIPTORS]

#: Every (descriptor, call site) pair, so a failure names the endpoint at fault.
_SITES: list[tuple[dict[str, Any], dict[str, Any]]] = [
    (descriptor, site)
    for descriptor in _DESCRIPTORS
    for site in descriptor["call_sites"]
]
_SITE_IDS = [f"{d['name']}:{s['id']}" for d, s in _SITES]


@pytest.mark.parametrize("descriptor", _DESCRIPTORS, ids=_IDS)
def test_every_descriptor_says_what_may_be_forced(descriptor: dict[str, Any]) -> None:
    """An absent allowlist is not "anything goes" — it is an unanswered question."""
    assert "forceable_params" in descriptor, (
        f"{descriptor['name']} does not say which parameters the cloud may force. "
        "An empty list is a valid and meaningful answer; silence is not."
    )
    assert isinstance(descriptor["forceable_params"], list)
    assert descriptor.get("forceable_params_note"), (
        f"{descriptor['name']} must record how its allowlist was verified, and when"
    )


@pytest.mark.parametrize(("descriptor", "site"), _SITES, ids=_SITE_IDS)
def test_every_call_site_says_what_may_be_forced_ON_IT(
    descriptor: dict[str, Any], site: dict[str, Any]
) -> None:
    """Inheriting the provider's list would reintroduce the bug this file guards."""
    assert isinstance(site.get("forceable_params"), list), (
        f"{descriptor['name']}:{site['id']} does not say what may be forced on it. "
        "An empty list is a valid answer; an absent one silently means 'whatever "
        "the provider allows somewhere', which is how a parameter reaches an "
        "endpoint that rejects it."
    )


@pytest.mark.parametrize(("descriptor", "site"), _SITES, ids=_SITE_IDS)
def test_forceable_never_overlaps_protected(
    descriptor: dict[str, Any], site: dict[str, Any]
) -> None:
    """The cloud may not force a parameter the SDK is sworn to protect."""
    protected = set(descriptor.get("protected_params", []))
    overlap = set(site["forceable_params"]) & protected

    assert not overlap, (
        f"{descriptor['name']}:{site['id']} declares {overlap} as both forceable "
        "and protected"
    )


@pytest.mark.parametrize(("descriptor", "site"), _SITES, ids=_SITE_IDS)
def test_forceable_params_are_accepted_by_the_real_call_site(
    descriptor: dict[str, Any], site: dict[str, Any]
) -> None:
    """The assertion that would have caught the Gemini bug before a customer did.

    Checked against the signature of THIS endpoint, not of the provider. The
    previous version of this test took the intersection across every call site,
    which was safe but blind: it could only ever prove a parameter was harmless
    everywhere, so a lever that exists on one endpoint had to be dropped to keep
    the test honest. Per site, the test is both stricter and more useful — it is
    what makes `background` on /v1/responses declarable at all.

    Only meaningful where parameters travel as keyword arguments. Where they
    travel as headers the signature says nothing, and an unknown header is
    tolerated by every provider we support.
    """
    if descriptor.get("param_target") != "kwargs":
        target = descriptor.get("param_target")
        pytest.skip(f"{descriptor['name']} injects into {target}")

    forceable = site["forceable_params"]
    if not forceable:
        pytest.skip(f"{descriptor['name']}:{site['id']} forces nothing")

    accepted = _accepted_keywords(_real_client(descriptor["name"]), site["path"])

    assert accepted is not None, (
        f"could not resolve {descriptor['name']}:{site['id']} on the real client — "
        "an unresolvable call site cannot be verified, and must not silently pass"
    )
    if accepted == "any":  # a **kwargs call site accepts anything
        return

    unknown = [param for param in forceable if param not in accepted]
    assert not unknown, (
        f"{descriptor['name']}:{site['id']} declares {unknown} as forceable, but "
        f"that endpoint accepts only {sorted(accepted)}. Forcing one of those "
        "breaks every call for the users who opted out — and only for them."
    )


def test_a_lever_that_exists_on_one_endpoint_only_is_declared_on_that_one() -> None:
    """The regression this whole card was built to make impossible.

    `background` is a genuine retention control on /v1/responses (it persists the
    response for about ten minutes so the client can poll it) and does not exist
    on /v1/chat/completions. Under the old provider-wide allowlist it could only
    be declared for both — breaking chat.completions — or for neither, which is
    what we did. This pins the third option now that it exists.
    """
    openai = next(d for d in _DESCRIPTORS if d["name"] == "openai")
    by_id = {site["id"]: set(site["forceable_params"]) for site in openai["call_sites"]}

    assert "background" in by_id["responses.create"]
    assert "background" not in by_id["chat.completions.create"]
    # …and both still refuse durable storage, which is not endpoint-specific.
    assert "store" in by_id["responses.create"]
    assert "store" in by_id["chat.completions.create"]


def _real_client(provider: str) -> Any:
    """A client instance, because call sites hang off the instance, not the module."""
    if provider in ("openai", "deepseek"):
        return pytest.importorskip("openai").OpenAI(api_key="sk-test")
    if provider == "anthropic":
        return pytest.importorskip("anthropic").Anthropic(api_key="sk-test")
    if provider == "gemini":
        return pytest.importorskip("google.genai").Client(api_key="test")
    return pytest.importorskip("mistralai.client").Mistral(api_key="test")


def _accepted_keywords(client: Any, path: list[str]) -> set[str] | str | None:
    """Keywords accepted by one endpoint: a set, ``"any"``, or None if unresolvable.

    ``"any"`` and None are deliberately different answers. A ``**kwargs`` site
    genuinely accepts anything and constrains nothing; a site that cannot be
    resolved at all was not checked, and a test that cannot check something must
    say so rather than pass.
    """
    target: Any = client
    for part in path:
        target = getattr(target, part, None)
        if target is None:
            return None
    if not callable(target):
        return None

    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):  # pragma: no cover — C extensions
        return None

    if any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
    ):
        return "any"
    return set(signature.parameters)
