"""The consent option catalogue, and each provider's honest answer to it.

`packages/descriptors/consent-options.json` is the single vocabulary every SDK,
the widget, the provider registry and the ACT token spec draw from. Each
provider descriptor then answers it option by option, with an `enforcement`
level: `enforced` (a real request parameter), `contractual` (a written
commitment, no lever), `account` (settable but only outside the API), `absent`
(the API does not perform the processing at all), or `unsupported`.

The whole design rests on one rule, and these tests exist to keep it: **a
provider may never claim to enforce something it cannot.** Announcing technical
enforcement where only a contract exists is the most damaging thing this product
could do, because the customer repeats the claim to their regulator.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

import pytest

_SHARED = Path(__file__).resolve().parents[2] / "descriptors"
_CATALOGUE: dict[str, Any] = json.loads(
    (_SHARED / "consent-options.json").read_text(encoding="utf-8")
)
_OPTIONS: dict[str, Any] = {o["name"]: o for o in _CATALOGUE["options"]}
_LEVELS = set(_CATALOGUE["enforcement_levels"])

_DESCRIPTORS: list[dict[str, Any]] = sorted(
    (
        json.loads(path.read_text(encoding="utf-8"))
        for path in (_SHARED / "descriptors").glob("*.json")
    ),
    key=lambda d: d["name"],
)
_IDS = [d["name"] for d in _DESCRIPTORS]


class TestCatalogue:
    def test_every_option_has_both_languages_and_a_legal_basis(self) -> None:
        for name, option in _OPTIONS.items():
            for field in ("label_en", "label_fr", "description_en", "description_fr"):
                assert option.get(field), f"{name} is missing {field}"
            assert option.get("gdpr"), f"{name} does not say which article it engages"

    def test_every_default_is_a_refusal(self) -> None:
        """An option nobody has answered is refused, never assumed.

        The conservative default IS the product. A default of `true` anywhere
        would mean consent presumed on a question never asked.
        """
        for name, option in _OPTIONS.items():
            assert option["default"] is False, (
                f"{name} defaults to something other than refusal"
            )

    def test_every_option_belongs_to_a_declared_group(self) -> None:
        groups = {g["name"] for g in _CATALOGUE["groups"]}
        for name, option in _OPTIONS.items():
            group = option["group"]
            assert group in groups, f"{name} is in unknown group {group}"

    def test_the_legacy_boolean_maps_to_the_most_protective_reading(self) -> None:
        """`training: false` must widen to a refusal on every purpose it split into.

        Migrating it to anything less would silently grant something the user
        never agreed to.
        """
        mapping = _CATALOGUE["legacy_mapping"]["training"]["maps_to"]

        assert set(mapping) == {n for n in _OPTIONS if n.startswith("training.")}


class TestProviderEnforcement:
    @pytest.mark.parametrize("descriptor", _DESCRIPTORS, ids=_IDS)
    def test_every_provider_answers_every_option(
        self, descriptor: dict[str, Any]
    ) -> None:
        """Silence is not an answer: an unanswered option is an unknown promise."""
        enforcement = descriptor.get("enforcement")
        assert enforcement, f"{descriptor['name']} declares no enforcement map"

        missing = set(_OPTIONS) - set(enforcement)
        assert not missing, f"{descriptor['name']} says nothing about {sorted(missing)}"

    @pytest.mark.parametrize("descriptor", _DESCRIPTORS, ids=_IDS)
    def test_no_provider_answers_an_option_that_does_not_exist(
        self, descriptor: dict[str, Any]
    ) -> None:
        unknown = set(descriptor["enforcement"]) - set(_OPTIONS)
        assert not unknown, (
            f"{descriptor['name']} declares unknown options {sorted(unknown)}"
        )

    @pytest.mark.parametrize("descriptor", _DESCRIPTORS, ids=_IDS)
    def test_levels_are_from_the_catalogue_and_explained(
        self, descriptor: dict[str, Any]
    ) -> None:
        for option, entry in descriptor["enforcement"].items():
            where = f"{descriptor['name']}/{option}"
            assert entry["level"] in _LEVELS, f"{where}: {entry['level']}"
            assert entry.get("note"), f"{where} gives no reason"

    @pytest.mark.parametrize("descriptor", _DESCRIPTORS, ids=_IDS)
    def test_enforced_means_a_real_parameter_and_nothing_else_claims_one(
        self, descriptor: dict[str, Any]
    ) -> None:
        """`enforced` without a parameter is a claim with nothing behind it."""
        for option, entry in descriptor["enforcement"].items():
            if entry["level"] == "enforced":
                # Enforcement is either a request parameter or an act of the SDK
                # (refusing a tool). Claiming it with neither is a claim with
                # nothing behind it.
                assert entry.get("params") or entry.get("mechanism"), (
                    f"{descriptor['name']}/{option} claims enforcement but names "
                    "neither a parameter nor a mechanism"
                )
            else:
                assert "params" not in entry and "mechanism" not in entry, (
                    f"{descriptor['name']}/{option} is {entry['level']} yet claims "
                    "a parameter or a mechanism"
                )

    @pytest.mark.parametrize("descriptor", _DESCRIPTORS, ids=_IDS)
    def test_the_allowlist_is_exactly_the_enforced_parameters(
        self, descriptor: dict[str, Any]
    ) -> None:
        """Two ways of saying the same thing must never drift apart.

        `forceable_params` is what the cloud may inject; the enforcement map is
        what we tell customers we enforce. If one grows without the other, one of
        the two is a lie.
        """
        by_site: dict[str, set[str]] = {
            site["id"]: set(site["forceable_params"])
            for site in descriptor["call_sites"]
        }

        for option, entry in descriptor["enforcement"].items():
            for param in entry.get("params", []):
                for site_id in param["call_sites"]:
                    assert site_id in by_site, (
                        f"{descriptor['name']}/{option} enforces {param['name']} on "
                        f"'{site_id}', which is not one of this provider's call "
                        f"sites ({sorted(by_site)})"
                    )
                    assert param["name"] in by_site[site_id], (
                        f"{descriptor['name']}/{option} enforces {param['name']} on "
                        f"'{site_id}', but that endpoint does not list it as "
                        "forceable. One of the two is wrong, and the call breaks "
                        "for exactly the users who opted out."
                    )

        claimed = {
            param["name"]
            for entry in descriptor["enforcement"].values()
            for param in entry.get("params", [])
        }
        offered = set().union(*by_site.values()) if by_site else set()

        assert offered == claimed, (
            f"{descriptor['name']}: the call sites offer {sorted(offered)} but the "
            f"enforcement map claims {sorted(claimed)}. A parameter offered and "
            "never claimed is dead weight; a parameter claimed and never offered "
            "is a promise nothing keeps."
        )

    @pytest.mark.parametrize("descriptor", _DESCRIPTORS, ids=_IDS)
    def test_the_provider_wide_allowlist_is_the_union_of_its_call_sites(
        self, descriptor: dict[str, Any]
    ) -> None:
        """`forceable_params` at the top level is derived, not independent.

        It exists so a reader can answer "does this provider enforce anything at
        all?" without walking the sites — which is only useful if it cannot
        disagree with them.
        """
        union: set[str] = set()
        for site in descriptor["call_sites"]:
            union |= set(site["forceable_params"])

        assert set(descriptor["forceable_params"]) == union, (
            f"{descriptor['name']}: forceable_params={descriptor['forceable_params']} "
            f"but its call sites offer {sorted(union)}"
        )


def test_every_copy_of_the_level_vocabulary_names_the_same_levels() -> None:
    """The schema, the catalogue and both SDKs' public types list one set.

    The schema decides what a descriptor may say, the catalogue explains each
    level to whoever reads the matrix, and both SDKs type the levels their
    consent helpers hand back. A level added to one of the four and not the
    others is a descriptor that validates, renders and types three different
    ways.
    """
    from typing import get_args

    from idside.options import EnforcementLevel

    schema = json.loads(
        (_SHARED / "schema" / "provider-descriptor.schema.json").read_text("utf-8")
    )
    entry = schema["properties"]["enforcement"]["additionalProperties"]
    in_schema = set(entry["properties"]["level"]["enum"])
    node = (
        Path(__file__).resolve().parents[2] / "node" / "src" / "options.ts"
    ).read_text(encoding="utf-8")
    declared = node.split("export type EnforcementLevel =", 1)[1].split(";", 1)[0]
    in_node = {part.strip().strip('"') for part in declared.split("|")}

    assert in_schema == _LEVELS == set(get_args(EnforcementLevel)) == in_node
    assert "absent" in in_schema


#: The only entries whose own notes establish that the provider does not do the
#: processing on the API at all. Everything else without a lever stays
#: `unsupported`: "not exposed as a request parameter" says there is no switch,
#: not that there is nothing to switch off — and OpenAI infers no location but
#: processes the one an application declares to its web-search tools.
_ABSENT = {
    ("openai", "training.personalisation"),
    ("openai", "memory"),
    ("deepseek", "memory"),
    ("gemini", "memory"),
    ("mistral", "memory"),
}


def test_absent_is_said_only_where_the_note_establishes_it() -> None:
    absent = {
        (d["name"], option)
        for d in _DESCRIPTORS
        for option, entry in d["enforcement"].items()
        if entry["level"] == "absent"
    }

    assert absent == _ABSENT


#: The `contractual` entries whose terms rule the processing out for every call,
#: without condition: none today. Every one is governed by terms that allow it,
#: impose it — a mandatory 30-day retention, review of flagged content — or leave
#: it to the developer's organization: OpenAI keeps API data out of training
#: "unless you explicitly opt in", and Anthropic's "may not train models on
#: Customer Content" gives way to its Development Partner Program, which a
#: customer joins by enabling a data opt-in setting (Service Specific Terms,
#: section E). Saying `guaranteed` of those told a user the opposite of the terms.
_PROTECTIVE: set[tuple[str, str]] = set()


def test_every_contractual_entry_says_which_way_its_terms_decide() -> None:
    """`protective` is declared on every `contractual` entry, and only there.

    Required rather than defaulted: a new contractual entry nobody classified
    must fail here, not slip through as either reading.
    """
    for descriptor in _DESCRIPTORS:
        for option, entry in descriptor["enforcement"].items():
            where = f"{descriptor['name']}/{option}"
            if entry["level"] == "contractual":
                assert isinstance(entry.get("protective"), bool), where
            else:
                assert "protective" not in entry, where

    protective = {
        (d["name"], option)
        for d in _DESCRIPTORS
        for option, entry in d["enforcement"].items()
        if entry.get("protective") is True
    }

    assert protective == _PROTECTIVE


#: Where each provider's own documentation puts human review of API content.
#: OpenAI, Anthropic and Google write that their staff may review what abuse or
#: safety detection flags; Mistral calls its abuse monitoring automated
#: moderation, and documents human access only to what a customer reports. None
#: offers a request parameter, and no project, workspace or organisation setting
#: rules review out in writing — OpenAI's zero-retention projects come closest,
#: and still leave flagged images to manual review — so all four are terms that
#: decide without ruling it out. Google's unpaid tier adds routine review, which
#: a billed project removes, but the review of flagged content stays on every
#: tier. DeepSeek writes nothing about it either way.
_HUMAN_REVIEW = {
    "anthropic": "contractual",
    "deepseek": "unsupported",
    "gemini": "contractual",
    "mistral": "contractual",
    "openai": "contractual",
}


def test_human_review_is_contractual_wherever_terms_decide_it() -> None:
    """Terms that decide without ruling it out — and no posture to route to.

    `routable` lets a consent helper ask the question once an app holds two
    credentials. Gemini and Mistral said it of human review, so an app with two
    projects was told to ask its users something neither project could honour.
    """
    entries = {d["name"]: d["enforcement"]["human_review"] for d in _DESCRIPTORS}

    assert {name: entry["level"] for name, entry in entries.items()} == _HUMAN_REVIEW
    assert not any(entry.get("protective") for entry in entries.values())
    assert not any(entry.get("routable") for entry in entries.values())


def test_every_human_review_note_says_when_it_was_checked() -> None:
    """A level read off a provider's terms is only as good as the day it was read.

    These five notes are what a DPO quotes on human review, and the terms they
    paraphrase move: the date is what tells the reader how stale the reading is.
    """
    for descriptor in _DESCRIPTORS:
        note = descriptor["enforcement"]["human_review"]["note"]
        assert re.search(r"Checked on \d{4}-\d{2}-\d{2}", note), descriptor["name"]


def test_at_least_one_provider_enforces_something() -> None:
    """A guard against the catalogue quietly becoming decorative.

    If every option on every provider were `contractual` or `unsupported`, the
    product would still be worth something — the ACT token and the ledger prove
    the choice was collected — but we would need to say so very differently.
    """
    enforced = [
        (d["name"], option)
        for d in _DESCRIPTORS
        for option, entry in d["enforcement"].items()
        if entry["level"] == "enforced"
    ]

    assert enforced, (
        "no provider enforces anything technically any more — re-read the marketing"
    )


def test_a_declared_mechanism_must_actually_exist_in_the_sdk() -> None:
    """`enforced` by a mechanism means code, not a string in a JSON file.

    This guard was written because `agentic_tools` shipped as
    `level: enforced, mechanism: refuse_or_strip_tools` on all four providers
    while that mechanism existed nowhere in either SDK. The option was offered to
    end users as answerable and rendered in the regulator-facing matrix as "SDK
    refuses the tool" — a claim with nothing behind it, in the one document a
    customer repeats to their DPO.

    The sibling check `test_enforced_means_a_real_parameter_and_nothing_else_
    claims_one` did not catch it: it only asserted the string was present.
    """
    sources = [
        path.read_text(encoding="utf-8")
        for root in (
            Path(__file__).resolve().parents[1] / "src",
            Path(__file__).resolve().parents[2] / "node" / "src",
        )
        for path in root.rglob("*")
        if path.suffix in {".py", ".ts"}
    ]

    for descriptor in _DESCRIPTORS:
        for option, entry in descriptor["enforcement"].items():
            mechanism = entry.get("mechanism")
            if mechanism is None:
                continue
            assert any(mechanism in source for source in sources), (
                f"{descriptor['name']}/{option} claims mechanism "
                f"{mechanism!r}, which is "
                "implemented in neither SDK. Publishing it tells a DPO we enforce "
                "something we do not."
            )


def _matches(model: str, glob: str) -> bool:
    """The `models.only` / `models.except` syntax, as the schema defines it.

    "`*` wildcards, `|` alternatives" — restated here rather than imported,
    because no SDK is allowed to read these fields (invariant #1: the decision
    is the cloud's). The point of the restatement is that the *outcome* of a
    glob written in this repository can be pinned in this repository, model by
    model, without any SDK gaining the ability to evaluate one at runtime.
    """
    return any(fnmatch.fnmatchcase(model, alt) for alt in glob.split("|"))


#: Real model ids the cache-retention allowlist MUST cover. Every one of these
#: accepts `prompt_cache_retention`, so a user who refused the cache must have
#: `in_memory` forced on it.
_CACHE_FORCED_ON = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-4-turbo",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5-2026-01-15",
    "gpt-5.1",
    "gpt-5.1-mini",
    "gpt-5.4",
    "o1",
    "o1-mini",
    "o3",
    "o3-mini",
    "o4-mini",
]

#: Model ids the allowlist MUST NOT cover: OpenAI supports only `24h` from
#: GPT-5.5 onward, so forcing `in_memory` there is a 400 on every call — for
#: exactly the users who refused the cache and for nobody else. `gpt-5.10` and
#: `gpt-5.11-mini` are the trap a trailing `*` sets on a version number:
#: `gpt-5.1*` matches them, and they are "5.5 and later".
_CACHE_SKIPPED_ON = [
    "gpt-5.5",
    "gpt-5.5-mini",
    "gpt-5.6",
    "gpt-5.7",
    "gpt-5.10",
    "gpt-5.11-mini",
    "gpt-6",
    "gpt-6-mini",
]


def _cache_allowlist() -> str:
    openai = next(d for d in _DESCRIPTORS if d["name"] == "openai")
    param = openai["enforcement"]["retention.cache"]["params"][0]
    assert param["name"] == "prompt_cache_retention"
    return param["models"]["only"]  # type: ignore[no-any-return]


@pytest.mark.parametrize("model", _CACHE_FORCED_ON)
def test_the_cache_allowlist_still_covers_every_model_it_must(model: str) -> None:
    """The outcome of the glob, pinned model by model rather than reviewed.

    Written as bare `gpt-5` once, which matches the string `gpt-5` and nothing
    else: `gpt-5-mini`, `gpt-5-nano` and every dated snapshot silently fell out
    of enforcement. A user refused the cache, the call went out unforced, the
    prompt was cached for 24h, and the ledger recorded the refusal as honoured.
    Nothing in the suite failed, because nothing in the suite read the glob.
    """
    assert _matches(model, _cache_allowlist()), (
        f"{model} is no longer covered by the retention.cache allowlist "
        f"({_cache_allowlist()}): a refusal on that model now reaches nothing, "
        "while the ledger still records it as honoured"
    )


@pytest.mark.parametrize("model", _CACHE_SKIPPED_ON)
def test_the_cache_allowlist_still_excludes_every_model_it_must(model: str) -> None:
    """The other direction, which is the one that breaks calls rather than
    weakening them: `gpt-5*` would sweep in `gpt-5.5-mini`, and `in_memory` on
    a 5.5+ model is a 400 for exactly the users who exercised their choice."""
    assert not _matches(model, _cache_allowlist()), (
        f"{model} is now covered by the retention.cache allowlist "
        f"({_cache_allowlist()}), but OpenAI accepts only `24h` from GPT-5.5 "
        "onward: forcing `in_memory` there 400s the call"
    )
