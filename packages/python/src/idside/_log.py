# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""The journal: enough to debug a call that did not go out, and nothing more.

A fail-closed SDK with no journal is undebuggable at a customer site. When a
call does not leave the process, the developer needs to learn which call site
was recognised, which parameters a user's answer forced, how long the cloud
took, and whether it was reached at all. Every one of those is a fact about
*their own* request.

Three rules shape everything here, and the first two are refusals.

**Nothing secret is ever emitted.** Not the ID Side key, not the provider key,
not the end user's identifier in the clear, not a UBT, not a prompt, not an ACT
token, not a credential. Invariant #7 says a credential is a secret — never
logged, never in an error message — and the person on the other end deserves the
same treatment. What is emitted is either a fact about the developer's own
configuration (the provider, the call site, the model, the posture *label*) or a
value that is already a digest.

**Silent by default.** This module attaches a ``NullHandler`` and nothing else.
It never calls ``basicConfig``, never touches the root logger, never writes to a
stream. Without that handler, Python's ``lastResort`` prints WARNING and above
to stderr, so "we only log at DEBUG" would not have been an answer on its own: a
library that prints uninvited is a library that gets removed.

**Observation is not influence** (invariant #8). A logging handler is arbitrary
application code running in the middle of the enforcement path, and formatters
routinely walk and normalise what they are handed. So a record is built from
scalars and fresh copies, never from the live objects the SDK is about to act
on — no ``ConsentDecision``, no kwargs dict, no route. A handler that mutates
everything it is shown changes nothing about the call that follows, and a test
pins exactly that. There is no hook, no callback and no ``on_event=`` here
either: a point where an application could watch *and* answer would be the
opaque tunnel through the back door.

The net (``_guard.py``) uses none of this. It stays blind by design, and
observability is not a reason to give it a first thing to record.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

__all__ = ["event", "logger", "pseudonym", "timer"]

_LOGGER_NAME = "idside"

#: Per-process salt for the subject pseudonym. Random, never persisted, never
#: emitted. A ``user_id`` is low-entropy on purpose — ``usr_marie_456`` is a
#: dictionary away — so an unsalted digest of one is not a pseudonym but an
#: encoding: anyone holding the log hashes their guesses and reads the names
#: back. Salting per process keeps the only property debugging actually needs,
#: correlating one user's calls *within one log*, and drops the one nobody
#: asked for, recognising that user across logs, machines or restarts.
_SALT = os.urandom(32)

#: Enough digest to tell users apart in a log, far too little to attack.
_PSEUDONYM_CHARS = 12

#: The request hash is already a digest and is what the ledger indexes, so it is
#: the honest answer to "which proof was minted for this call?" — the ACT token
#: itself is bearer material and is never emitted. Truncated anyway: a full hash
#: of a short prompt is a dictionary attack away from the prompt.
_HASH_CHARS = 12


def logger() -> logging.Logger:
    """The package logger. The application owns its handlers, level and format."""
    return logging.getLogger(_LOGGER_NAME)


def pseudonym(value: object) -> str:
    """A stable, unforgeable stand-in for one end user, for this process only.

    Accepts a ``user_id`` or a UBT and treats both the same way: what comes back
    names nobody, and the value that went in cannot be recovered from it or
    guessed into it.
    """
    digest = hashlib.sha256(_SALT + str(value).encode("utf-8")).hexdigest()
    return digest[:_PSEUDONYM_CHARS]


def short_hash(request_hash: object) -> str:
    """The correlating half of a ``sha256:<hex>`` request hash."""
    text = str(request_hash)
    _, _, hexpart = text.rpartition(":")
    return hexpart[:_HASH_CHARS]


class timer:
    """Milliseconds spent inside the block, for the one hop worth measuring.

    Monotonic: a journal that reports a negative authorize because NTP stepped
    the clock is a journal that gets distrusted for the one number it exists to
    report.
    """

    __slots__ = ("_started", "ms")

    def __init__(self) -> None:
        self.ms = 0

    def __enter__(self) -> timer:
        self._started = time.monotonic_ns()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.ms = (time.monotonic_ns() - self._started) // 1_000_000


def event(name: str, **fields: Any) -> None:
    """Emit one event at DEBUG, as a stable text line and a structured payload.

    Both, because applications read logs both ways: a terminal reads the
    message, an aggregator reads ``record.idside``. A field present in one and
    absent from the other would be a field somebody debugs without.

    Field order is the caller's, and stable, so two runs of the same path
    produce comparable lines — which is what lets the cross-SDK parity script
    diff Python's journal against Node's. ``None`` values are dropped rather
    than rendered: "the cloud named no posture" is said by the absence of
    ``posture=``, not by ``posture=None``.

    Callers pass scalars and copies. Nothing here reaches into a live object,
    and nothing a handler does to what it receives can travel back.
    """
    log = logger()
    if not log.isEnabledFor(logging.DEBUG):
        return  # do not pay for formatting nobody will read
    payload = {key: value for key, value in fields.items() if value is not None}
    rendered = " ".join(f"{key}={_render(value)}" for key, value in payload.items())
    log.debug(
        f"{name} {rendered}" if rendered else name,
        extra={"idside": {"event": name, **payload}},
    )


def _render(value: Any) -> str:
    """One value, in a form that survives a grep and never spans a line.

    A list becomes ``a+b`` rather than ``['a', 'b']``: the parameters a consent
    state forced are read far more often than they are parsed, and the sorted
    join is identical in both SDKs, which is what the parity script compares.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return (
            "+".join(str(item) for item in sorted(str(item) for item in value)) or "-"
        )
    return str(value).replace(" ", "_")


# Silent by default, and this line is what makes it true: with no handler at
# all, Python's `lastResort` prints WARNING and above straight to stderr. The
# application adds its own handler if it wants to read any of this.
logging.getLogger(_LOGGER_NAME).addHandler(logging.NullHandler())
