# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""``idside-verify-clean``: replay prompt cleaning on a request that went out.

    idside-verify-clean --policy policy.json --provider openai request.json

``request.json`` is the body the SDK sent to the provider; ``policy.json`` is
the cleaning policy the proof names (``--digest`` checks it is that one).
Exit 0: cleaning that body again under that policy changes nothing — what was
sent is a fixed point. Exit 1: it would still mask or block something, and the
counts say what, by detector, never which value. Exit 2: the files could not
be read, the policy cannot be applied, its digest is not the one given, or
an error stopped the check before it answered: nothing was checked.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from idside._cleaning import (
    ENGINE_VERSION,
    clean,
    content_policy_digest,
    rules_digest,
)
from idside._cleaning.rules import LIMITS
from idside.exceptions import ContentPolicyError
from idside.providers import load_descriptors


def _summary(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}: {counts[key]}" for key in sorted(counts))


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(
    argv: Sequence[str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        code = _check(argv, out, err)
        out.flush()  # a closed pipe or a full disk fails here, not at exit
    except Exception as exc:
        # Exit 1 is an answer: the body would still be cleaned. An error no
        # branch below foresaw (an output that fails, as a closed pipe does)
        # must never read as that answer. Usage errors and --help leave
        # through SystemExit, as argparse raises them: 2 and 0.
        _say(
            err,
            "idside-verify-clean: unexpected error, nothing was checked "
            f"({type(exc).__name__}: {exc})\n",
        )
        for stream in (out, err):
            _flush_or_discard(stream)
        return 2
    return code


def _say(stream: TextIO, text: str) -> None:
    try:
        stream.write(text)
        stream.flush()
    except OSError:
        _flush_or_discard(stream)


def _flush_or_discard(stream: TextIO) -> None:
    """Flush ``stream``; when it cannot be written, drop what it still holds.

    Otherwise the interpreter would try again at exit, fail again, and exit
    120 instead of the 2 that says the answer was not delivered.
    """
    try:
        stream.flush()
    except OSError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, stream.fileno())
        os.close(devnull)


def _check(argv: Sequence[str] | None, out: TextIO, err: TextIO) -> int:
    parser = argparse.ArgumentParser(
        prog="idside-verify-clean",
        description=(
            "Check that a request body is a fixed point of an ID Side prompt-cleaning "
            "policy: cleaning it again would mask nothing and block nothing."
        ),
    )
    parser.add_argument("request", help="the JSON body that was sent to the provider")
    parser.add_argument("--policy", required=True, help="the policy document (JSON)")
    parser.add_argument(
        "--provider",
        required=True,
        choices=sorted(load_descriptors()),
        help="the provider the request was sent to",
    )
    parser.add_argument("--digest", help="the policy digest the proof names")
    args = parser.parse_args(argv)

    try:
        payload = _load(args.request)
        policy = _load(args.policy)
    except (OSError, ValueError, RecursionError) as exc:
        # RecursionError: JSON nested deeper than Python's reader goes.
        err.write(f"idside-verify-clean: cannot read the input: {exc}\n")
        return 2
    if _deeper_than(payload, LIMITS["max_depth"]):
        # Nothing is cleaned past this depth, so nothing can be checked: the
        # same answer whatever depth a JSON reader stops at (Node's has none).
        err.write(
            "idside-verify-clean: cannot read the input: nested deeper than "
            f"{LIMITS['max_depth']} levels\n"
        )
        return 2
    try:
        digest = content_policy_digest(policy)
    except ValueError as exc:
        err.write(f"idside-verify-clean: the policy is not JSON: {exc}\n")
        return 2
    if args.digest is not None and args.digest != digest:
        err.write(
            f"idside-verify-clean: the policy's digest is {digest}, "
            f"not {args.digest}: this is not the policy the proof names\n"
        )
        return 2
    try:
        result = clean(payload, policy, provider=args.provider)
    except (ContentPolicyError, ValueError) as exc:
        err.write(f"idside-verify-clean: {exc}\n")
        return 2

    out.write(f"policy  {digest}\nengine  {ENGINE_VERSION}\nrules   {rules_digest()}\n")
    if result.blocked:
        blocked = _summary(dict(result.blocked))
        out.write(f"NOT CLEAN: this policy would block it ({blocked})\n")
        return 1
    if result.counts:
        masked = _summary(dict(result.counts))
        out.write(f"NOT CLEAN: this policy would still mask {masked}\n")
        return 1
    out.write("clean: nothing left to mask or block under this policy\n")
    return 0


def _deeper_than(value: Any, limit: int) -> bool:
    """Whether a JSON value nests past ``limit`` levels; Node's ``deeperThan()``."""
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > limit:
            return True
        if isinstance(node, dict):
            pending.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            pending.extend((item, depth + 1) for item in node)
    return False


if __name__ == "__main__":
    sys.exit(main())
