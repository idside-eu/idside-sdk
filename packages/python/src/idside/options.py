# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""The consent options an app can actually put to its users.

A developer wrapping OpenAI and Gemini needs to know what to render in their
consent UI, and the honest answer depends on which providers they wrap. This
module computes it from the shared catalogue, offline, with no network call.

The rule that shapes everything here:

    A question reaches an end user only if at least one of the app's providers
    can enforce it PER REQUEST.

An account-level control lives on the developer's API key and applies to all of
their users at once. Asking one person about it would manufacture the appearance
of a choice nobody has — two users answering differently would be treated
identically. Those become *disclosures* instead: facts about how this app is
configured, which the developer must be able to state and which the ledger
records, but which no consent dialog may present as a choice.

The exception is `routable`: an account-level control becomes a genuine per-user
one when the developer provisions several credentials with different privacy
postures, because the SDK can then pick the credential matching each answer.
That is the only route by which training opt-out can ever be honoured per user —
no provider exposes it as a request parameter.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "ConsentOption",
    "Disclosure",
    "catalogue",
    "decidable_options",
    "disclosures",
]

_CATALOGUE_PATH = Path(__file__).resolve().parent / "consent-options.json"
_PROVIDER_DIR = Path(__file__).resolve().parent / "providers"
#: The only names that may ever become a path. See _raw_enforcement.
_KNOWN_PROVIDERS = frozenset(p.stem for p in _PROVIDER_DIR.glob("*.json"))

EnforcementLevel = Literal[
    "enforced", "contractual", "account", "absent", "unsupported"
]

#: The levels at which no control exists, whatever the reason.
_NO_CONTROL = frozenset({"absent", "unsupported"})


@dataclass(frozen=True)
class ConsentOption:
    """One question, ready to render."""

    name: str
    group: str
    label_en: str
    label_fr: str
    description_en: str
    description_fr: str
    default: bool
    legal_basis: str
    gdpr: str
    #: Which of the app's providers can honour it, and how.
    enforced_by: tuple[str, ...] = ()
    #: Providers that will honour it by *routing* the call to the credential
    #: carrying that posture, rather than by a request parameter. The answer is
    #: as real to the user either way; the mechanism differs, and a consent UI
    #: that explains itself may want to say which.
    routed_by: tuple[str, ...] = ()

    def label(self, locale: str = "en") -> str:
        return self.label_fr if locale.startswith("fr") else self.label_en

    def description(self, locale: str = "en") -> str:
        return self.description_fr if locale.startswith("fr") else self.description_en


@dataclass(frozen=True)
class Disclosure:
    """Something the user cannot decide, and the developer must not hide.

    ``guaranteed`` — true for everyone already, and in the user's favour: the
    terms of every provider that answers it rule the processing out
    (``contractual`` and ``protective``). Terms that allow or impose it are no
    guarantee, and neither is one provider among several whose terms do.
    ``beyond_user_control`` — the app's own configuration decides, or the
    provider's terms do (``contractual`` terms that allow or impose it, or leave
    it to the developer's organization): no answer in a dialog could change it.
    If it is also ``routable``, the developer *could* give their users the choice
    by provisioning a second credential; until they do, they are making it for
    them.
    ``not_offered`` — no control for it is reachable through the endpoints the
    descriptors wrap, whether the processing is ``absent`` from the API or
    ``unsupported`` there. Nobody decided anything; there is nothing to decide.
    Saying "your app configured this" about a feature the API does not have
    would be plainly false.
    """

    option: ConsentOption
    kind: Literal["guaranteed", "beyond_user_control", "not_offered"]
    by_provider: dict[str, str]
    routable_from: tuple[str, ...] = ()

    @property
    def routable(self) -> bool:
        return bool(self.routable_from)


@lru_cache(maxsize=1)
def catalogue() -> dict[str, Any]:
    """The shared option catalogue, as shipped with this package."""
    loaded: dict[str, Any] = json.loads(_CATALOGUE_PATH.read_text(encoding="utf-8"))
    return loaded


def _option(
    raw: dict[str, Any],
    enforced_by: tuple[str, ...] = (),
    routed_by: tuple[str, ...] = (),
) -> ConsentOption:
    return ConsentOption(
        name=raw["name"],
        group=raw["group"],
        label_en=raw["label_en"],
        label_fr=raw["label_fr"],
        description_en=raw["description_en"],
        description_fr=raw["description_fr"],
        default=raw["default"],
        legal_basis=raw["legal_basis"],
        gdpr=raw["gdpr"],
        enforced_by=enforced_by,
        routed_by=routed_by,
    )


#: A posture nobody can be routed away from is not a choice. One credential is
#: the app's single posture, whatever it is labelled; it takes a second for two
#: users to be treated differently.
_MIN_POSTURES_FOR_A_CHOICE = 2


def _routing_providers(routed: Mapping[str, Sequence[str]] | None) -> frozenset[str]:
    """The providers where the developer really can send two users two ways."""
    if not routed:
        return frozenset()
    return frozenset(
        provider
        for provider, postures in routed.items()
        if len(set(postures)) >= _MIN_POSTURES_FOR_A_CHOICE
    )


def _enforcement(providers: list[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
    from idside.providers import load_descriptors

    descriptors = load_descriptors()
    known = {}
    for name in providers:
        descriptor = descriptors.get(name)
        if descriptor is None:
            continue
        known[name] = getattr(descriptor, "enforcement", None) or _raw_enforcement(name)
    return known


@cache
def _raw_enforcement(provider: str) -> dict[str, Any]:
    """Read the enforcement map straight from the bundled descriptor.

    The runtime ``ProviderDescriptor`` deliberately carries only what the SDK
    needs to make a call. The enforcement map is documentation for humans and
    input for a consent UI, so it is read here rather than widening that class.

    The name is checked against the bundled descriptors before it ever reaches a
    path. ``decidable_options`` is public API taking caller-supplied strings, and
    interpolating one into a filesystem path made
    ``decidable_options(["../../../etc/passwd"])`` read and parse an arbitrary
    file whose contents then flowed into the returned options.
    """
    if provider not in _KNOWN_PROVIDERS:
        return {}

    path = _PROVIDER_DIR / f"{provider}.json"
    if not path.exists():
        return {}
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    enforcement: dict[str, Any] = raw.get("enforcement", {})
    return enforcement


def decidable_options(
    providers: list[str] | tuple[str, ...],
    routed: Mapping[str, Sequence[str]] | None = None,
) -> list[ConsentOption]:
    """The questions this app may actually put to a user, in catalogue order.

    Ordered by the catalogue rather than by provider, so the dialog never
    depends on which client happened to be wrapped first, and each question is
    asked once however many providers share it.

    ``routed`` maps a provider to the posture labels the app provisioned for it
    — the keys of the ``credentials=`` mapping given to ``consent.wrap()``. An
    account-level option marked ``routable`` in the descriptor becomes a real
    question for that provider once there are **two** of them, because two users
    answering differently can then be treated differently. With one, the app has
    one posture and asking would manufacture a choice nobody has.
    """
    levels = _enforcement(providers)
    routing = _routing_providers(routed)
    options = []

    for raw in catalogue()["options"]:
        enforced_by = tuple(
            name
            for name, enforcement in levels.items()
            if enforcement.get(raw["name"], {}).get("level") == "enforced"
        )
        routed_by = tuple(
            name
            for name, enforcement in levels.items()
            if name in routing
            and name not in enforced_by
            and enforcement.get(raw["name"], {}).get("routable")
        )
        if enforced_by or routed_by:
            options.append(_option(raw, enforced_by, routed_by))

    return options


def _kind_for(
    entries: list[dict[str, Any]],
) -> Literal["guaranteed", "beyond_user_control", "not_offered"]:
    """Mirrors `kindFor` in the Node SDK — these values cross SDK boundaries.

    ``contractual`` alone says the terms decide, not which way: it used to be
    read as a guarantee, which called Anthropic's review of flagged content and
    OpenAI's "unless the org opts in" guaranteed to the user.
    """
    if all(
        entry["level"] == "contractual" and entry.get("protective") is True
        for entry in entries
    ):
        return "guaranteed"
    if {entry["level"] for entry in entries} <= _NO_CONTROL:
        return "not_offered"
    return "beyond_user_control"


def disclosures(
    providers: list[str] | tuple[str, ...],
    routed: Mapping[str, Sequence[str]] | None = None,
) -> list[Disclosure]:
    """What the user cannot decide — stated, never asked.

    ``routed`` is the same argument :func:`decidable_options` takes, and moves
    the same options: anything the app can now route on becomes a question, so
    it stops being a disclosure. Passing it to one and not the other would say
    two contradictory things about the same option.
    """
    levels = _enforcement(providers)
    asked = {option.name for option in decidable_options(providers, routed)}
    found = []

    for raw in catalogue()["options"]:
        if raw["name"] in asked:
            continue

        entries = {
            name: enforcement[raw["name"]]
            for name, enforcement in levels.items()
            if enforcement.get(raw["name"], {}).get("level")
        }
        if not entries:
            continue

        found.append(
            Disclosure(
                option=_option(raw),
                kind=_kind_for(list(entries.values())),
                by_provider={name: entry["level"] for name, entry in entries.items()},
                routable_from=tuple(
                    name for name, entry in entries.items() if entry.get("routable")
                ),
            )
        )

    return found
