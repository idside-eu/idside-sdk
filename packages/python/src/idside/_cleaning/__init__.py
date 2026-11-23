# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Prompt cleaning: the engine, its digests and the replay check.

What the SDK applies to a call before it hashes and sends it, under the policy
the organization set on the dashboard. The public surface is re-exported from
the package root: :func:`idside.verify_clean` and
:func:`idside.content_policy_digest`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from idside import _jcs
from idside._cleaning.digests import (
    build_digest,
    challenge_digest,
    content_policy_digest,
    rules_digest,
)
from idside._cleaning.engine import (
    ATTACHMENTS,
    SIZE_LIMIT,
    CleanResult,
    ContentCleaner,
    reference_policy,
)
from idside._cleaning.rules import CATALOGUE_IDS, ENGINE_VERSION, catalogue_problem
from idside.providers import ContentFields, load_descriptors

__all__ = [
    "ATTACHMENTS",
    "CATALOGUE_IDS",
    "ENGINE_VERSION",
    "SIZE_LIMIT",
    "CleanResult",
    "ContentCleaner",
    "build_digest",
    "catalogue_problem",
    "challenge_response",
    "clean",
    "content_fields",
    "content_policy_digest",
    "reference_policy",
    "rules_digest",
    "verify_clean",
]


def content_fields(provider: str | None) -> ContentFields:
    """The content block of a bundled provider descriptor (none: read everything)."""
    if provider is None:
        return ContentFields()
    descriptor = load_descriptors().get(provider)
    if descriptor is None:
        raise ValueError(f"unknown provider {provider!r}")
    return descriptor.content


@lru_cache(maxsize=32)
def _cleaner(canonical_policy: str, provider: str | None) -> ContentCleaner:
    return ContentCleaner(json.loads(canonical_policy), content_fields(provider))


def clean(payload: Any, policy: Any, *, provider: str | None) -> CleanResult:
    """Clean ``payload`` under ``policy`` as the SDK does before a call leaves."""
    try:
        canonical = _jcs.canonicalize(policy).decode("utf-8")
    except ValueError:
        # Not JSON: compiled uncached, to raise the policy error it deserves.
        return ContentCleaner(policy, content_fields(provider)).clean(payload)
    return _cleaner(canonical, provider).clean(payload)


def verify_clean(payload: Any, policy: Any, *, provider: str | None) -> bool:
    """Whether ``payload`` is what cleaning under ``policy`` lets leave, unchanged.

    The replay check. A body the SDK sent is a fixed point of the policy it
    applied: cleaning it again masks nothing and blocks nothing. Whoever holds
    the request that went out (and the policy its proof names, by digest) can
    run this years later, without ID Side. ``True`` does not mean the body held
    nothing sensitive — only that this policy, with this engine, finds nothing
    more to do. ``provider`` is the descriptor name (``"openai"``…) whose
    structural fields are left alone; ``None`` reads every string.
    """
    result = clean(payload, policy, provider=provider)
    return not result.blocked and not result.counts and result.payload == payload


def challenge_response(token: str, inputs: list[str]) -> str:
    """The answer to a cloud challenge: each input cleaned alone, reference policy."""
    if not isinstance(token, str) or not all(isinstance(text, str) for text in inputs):
        raise ValueError("a challenge is a token and a list of strings")
    cleaner = ContentCleaner.reference()
    return challenge_digest(token, [cleaner.clean(text).payload for text in inputs])
