# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""The ``request_hash`` formula — the one the enforcement path and a verifier share.

Exported from the package root as :func:`idside.request_hash`, the Python twin of
Node's ``requestHash()``. The enforcement path calls this same function, so a
verifier who recomputes a hash with it gets the value the cloud signed, and the
two can never drift apart.
"""

from __future__ import annotations

import hashlib
from typing import Any

from idside import _jcs

__all__ = ["request_hash"]


def request_hash(payload: Any) -> str:
    """Compute the ``request_hash`` an ACT token is bound to.

    The lowercase SHA-256 of the RFC 8785 (JCS) canonical UTF-8 encoding of
    ``payload``, prefixed ``sha256:`` — the value to pass as
    ``verify_act(..., request_hash=...)``.

    ``payload`` is the caller's call arguments as JSON values, **without** the
    identity (``user_id`` / ``ubt``) and the per-request headers argument
    (``extra_headers``): the SDK leaves those out before hashing, so leave them
    out too. Parameters the cloud forced afterwards (``store=False``…) are not
    part of it either.

    Raises ``ValueError`` for anything outside JSON — a non-string key, NaN or
    Infinity, an integer beyond ±2^53, a lone surrogate, any other type. There is
    no ``str()`` fallback: a hash nobody else can reproduce proves nothing.
    """
    return "sha256:" + hashlib.sha256(_jcs.canonicalize(payload)).hexdigest()
