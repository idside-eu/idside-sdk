# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""An organization's prompt-cleaning policy, read and compiled.

The policy says which catalogue detectors run and whether each masks or blocks,
whether attachments may leave, and which custom terms, domains and patterns the
organization added. What a newer cloud may add is ignored by this engine, the
way a newer catalogue id is: an unknown detector id, action or custom kind. A
policy that is malformed — a field of the wrong type, a custom entry whose
label or value breaks the rules — is refused, never half-applied: the SDK is
fail-closed, and a policy it cannot read is not "no policy".

Errors name the field or the entry's position, never a value: custom entries
are the organization's own words, kept encrypted on the cloud and shown to its
administrators only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from idside._cleaning.rules import (
    DETECTORS,
    LIMITS,
    WORD_CLASS,
    Pattern,
    boundary,
)
from idside._cleaning.shadow import fold, map_code_point
from idside._cleaning.validators import VALIDATORS
from idside.exceptions import ContentPolicyError

_ACTIONS = ("mask", "block")
_KINDS = ("term", "domain", "pattern")
_LABEL = re.compile(r"[A-Z0-9_]{1,24}")
_DOMAIN = re.compile(r"[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){1,8}")
_WORD = re.compile("[" + WORD_CLASS + "]")
_SPACES = re.compile(r"[ \t\r\n\f\v]+")
_RUN = LIMITS["pattern_run"]


@dataclass(frozen=True)
class Rule:
    """One rule as the engine runs it: a detector, or a custom entry."""

    #: What counts and blocks are reported under: a detector id or ``custom:LABEL``.
    key: str
    label: str
    action: str
    #: Tie-break after length: custom entries first, in policy order, then the
    #: catalogue in its own order.
    order: int
    patterns: tuple[Pattern, ...]
    validator: Callable[[str], int | None] | None
    #: Read on the ASCII-lowered shadow (custom terms and domains).
    lowered: bool


@dataclass(frozen=True)
class CompiledPolicy:
    rules: tuple[Rule, ...]
    block_attachments: bool


def _refuse(what: str) -> ContentPolicyError:
    return ContentPolicyError(
        f"The prompt-cleaning policy cannot be applied: {what}. "
        "The AI call was not made."
    )


def _is_word(char: str) -> bool:
    return _WORD.match(char) is not None


def _pattern(
    regex: str, prefilter: tuple[str, ...], extend: str | None = None
) -> Pattern:
    return Pattern(
        regex=re.compile(regex, re.ASCII),
        group=1,
        keywords=None,
        prefilter=prefilter,
        extend=None if extend is None else re.compile(extend, re.ASCII),
    )


def _term(value: str, where: str) -> tuple[Pattern, ...]:
    words = [word for word in _SPACES.split(fold(value)) if word]
    if not words:
        raise _refuse(f"{where} has no visible character")
    body = r"[ \t\r\n]{1,16}".join(re.escape(word) for word in words)
    lead = boundary(WORD_CLASS) if _is_word(words[0][0]) else ""
    trail = "(?![" + WORD_CLASS + "])" if _is_word(words[-1][-1]) else ""
    return (_pattern(lead + "(" + body + ")" + trail, (max(words, key=len),)),)


def _domain(value: str, where: str) -> tuple[Pattern, ...]:
    domain = fold(value).strip(" \t\r\n\f\v")
    for prefix in ("@", "*."):
        domain = domain.removeprefix(prefix)
    domain = domain.strip(".")
    if not _DOMAIN.fullmatch(domain):
        raise _refuse(f"{where} is not a domain name")
    name = re.escape(domain)
    host = r"(?:[a-z0-9-]{1,63}\.){0,8}" + name + r"(?![a-z0-9-]|\.[a-z0-9])"
    url = (
        boundary("a-z0-9+.-")
        + r"([a-z][a-z0-9+.-]{1,30}://(?:[^/@ \t\r\n\"'<>]{0,256}@)?"
        + host
    )
    return (
        # An e-mail address at the domain or one of its subdomains.
        _pattern(
            boundary("a-z0-9._%+-") + "([a-z0-9._%+-]{1,64}@" + host + ")", (domain,)
        ),
        # A URL whose host is the domain, and with a port, a path, a query or
        # a fragment, all of it to the next blank or quote.
        _pattern(url + ")", (domain,)),
        _pattern(url + r"[:/?#])", (domain,), extend=r"[^ \t\r\n\"'<>]{1,4096}"),
        # The host name alone.
        _pattern(boundary("a-z0-9.@/_-") + "(" + host + ")", (domain,)),
    )


def _custom_pattern(value: str, where: str) -> tuple[Pattern, ...]:
    if "**" in value:
        raise _refuse(f"{where} has two `*` in a row")
    tokens: list[tuple[str, bool]] = []  # (regex, can match a letter or digit)
    literal = ""
    literals: list[str] = []
    stars = 0
    for char in value:
        if char in "#@*":
            literals.append(literal)
            literal = ""
        if char == "#":
            tokens.append(("[0-9]", True))
        elif char == "@":
            tokens.append(("[A-Za-z]", True))
        elif char == "*":
            stars += 1
            # A run is atomic: it takes every letter and digit there is (up to
            # the limit), so nothing ever backtracks into it.
            tokens.append(
                (f"(?=(?P<run{stars}>[A-Za-z0-9]{{1,{_RUN}}}))(?P=run{stars})", True)
            )
        else:
            for mapped in map_code_point(ord(char)):
                if mapped.isascii() and mapped.isalpha():
                    tokens.append((f"[{mapped.upper()}{mapped.lower()}]", True))
                    literal += mapped.lower()
                else:
                    tokens.append(
                        (re.escape(mapped), mapped.isascii() and mapped.isdigit())
                    )
                    if mapped.isascii() and mapped.isdigit():
                        literal += mapped
                    else:
                        literals.append(literal)
                        literal = ""
    literals.append(literal)
    if not tokens:
        raise _refuse(f"{where} has no visible character")
    for (regex, _), (_, alnum) in pairwise(tokens):
        # A run takes every letter and digit there is: one right after it could
        # never be matched, so the pattern never would be.
        if regex.startswith("(?=(?P<run") and alnum:
            raise _refuse(
                f"{where} has a `*` followed by a letter, a digit, `#`, `@` or `*`"
            )
    lead = boundary("A-Za-z0-9") if tokens[0][1] else ""
    trail = "(?![A-Za-z0-9])" if tokens[-1][1] else ""
    longest = max(literals, key=len)
    prefilter = (longest,) if len(longest) >= 2 else ()
    return (
        _pattern(
            lead + "(" + "".join(regex for regex, _ in tokens) + ")" + trail, prefilter
        ),
    )


_BUILDERS = {"term": _term, "domain": _domain, "pattern": _custom_pattern}


def _length(value: str) -> int:
    """Length in code points, as Node counts it too."""
    return len(value)


def compile_policy(policy: Any) -> CompiledPolicy:
    if not isinstance(policy, dict):
        raise _refuse("it is not an object")
    schema = policy.get("schema")
    # 1.0 is 1 to JSON, and to the Node SDK: only a boolean is not a number here.
    if isinstance(schema, bool) or schema != 1:
        raise _refuse("its `schema` is not 1, the only one this SDK reads")

    detectors = policy.get("detectors", {})
    if not isinstance(detectors, dict):
        raise _refuse("`detectors` is not an object")
    attachments = policy.get("attachments", "allow")
    if not isinstance(attachments, str):
        raise _refuse("`attachments` is not a string")
    custom = policy.get("custom", [])
    if not isinstance(custom, list):
        raise _refuse("`custom` is not a list")
    if len(custom) > LIMITS["custom_entries"]:
        raise _refuse(f"`custom` has more than {LIMITS['custom_entries']} entries")

    rules: list[Rule] = []
    for position, entry in enumerate(custom):
        where = f"custom entry {position}"
        if not isinstance(entry, dict):
            raise _refuse(f"{where} is not an object")
        kind, action = entry.get("kind"), entry.get("action")
        if kind not in _KINDS or action not in _ACTIONS:
            continue  # written by a newer cloud for a newer engine
        label, value = entry.get("label"), entry.get("value")
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            raise _refuse(f"{where} has a label outside [A-Z0-9_]{{1,24}}")
        if not isinstance(value, str) or not (
            1 <= _length(value) <= LIMITS["custom_value_chars"]
        ):
            raise _refuse(
                f"{where} has a value that is not 1 to "
                f"{LIMITS['custom_value_chars']} characters"
            )
        rules.append(
            Rule(
                key=f"custom:{label}",
                label=label,
                action=action,
                order=position,
                patterns=_BUILDERS[kind](value, where),
                validator=None,
                lowered=kind != "pattern",
            )
        )

    for detector_id, action in detectors.items():
        if not isinstance(action, str):
            raise _refuse(f"the action of detector {detector_id!r} is not a string")
        detector = DETECTORS.get(detector_id)
        if detector is None or action not in _ACTIONS:
            continue  # a newer catalogue id, or a newer action
        rules.append(
            Rule(
                key=detector.id,
                label=detector.label,
                action=action,
                order=LIMITS["custom_entries"] + detector.index,
                patterns=detector.patterns
                if action == "block"
                else tuple(p for p in detector.patterns if not p.block_only),
                validator=VALIDATORS[detector.validator]
                if detector.validator
                else None,
                lowered=False,
            )
        )
    rules.sort(key=lambda rule: rule.order)
    return CompiledPolicy(rules=tuple(rules), block_attachments=attachments != "allow")
