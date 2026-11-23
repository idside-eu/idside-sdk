# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""The digests a cleaned call declares: policy, rules, build, challenge.

- ``digest``: SHA-256 of the RFC 8785 canonical form of the policy document —
  the same canonicaliser as the request hash.
- ``rules``: SHA-256 of the bundled ``content-rules.json``, as its bytes.
- ``build``: SHA-256 over what decides cleaning in this package as installed:
  the cleaning code and the provider descriptors, whose ``content`` blocks say
  which strings are read. For each file, sorted by path: the path
  (``idside/_cleaning/engine.py``, ``idside/providers/openai.json``, UTF-8), a
  NUL, the file's length in bytes written in decimal, a NUL, then its bytes.
- ``challenge.response``: SHA-256 of the challenge token, a newline, then the
  challenge inputs, each cleaned on its own under the reference policy, joined
  by newlines.

The cloud compares ``rules`` and ``build`` with those of the published
releases. A build that cannot be read (a frozen application) is declared as
the digest of no file at all: the cloud then flags the engine, nothing blocks.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any

from idside import _jcs
from idside._cleaning.rules import RULES_BYTES

_PACKAGE = Path(__file__).resolve().parent.parent
#: What the build digest covers, relative to the ``idside`` package.
BUILD_FILES = ("_cleaning/*.py", "providers/*.json")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def content_policy_digest(policy: Any) -> str:
    """The digest of a policy document, as the cloud signs it and a proof carries it.

    Raises ``ValueError`` for anything outside JSON, like ``request_hash()``.
    """
    return _sha256(_jcs.canonicalize(policy))


@cache
def rules_digest() -> str:
    return _sha256(RULES_BYTES)


def build_digest_of(files: Sequence[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for path, data in sorted(files):
        digest.update(path.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0")
        digest.update(data)
    return "sha256:" + digest.hexdigest()


@cache
def build_digest() -> str:
    try:
        files = [
            ("idside/" + path.relative_to(_PACKAGE).as_posix(), path.read_bytes())
            for pattern in BUILD_FILES
            for path in _PACKAGE.glob(pattern)
        ]
    except OSError:
        files = []
    return build_digest_of(files)


def challenge_digest(token: str, cleaned: Sequence[str]) -> str:
    return _sha256((token + "\n" + "\n".join(cleaned)).encode("utf-8"))
