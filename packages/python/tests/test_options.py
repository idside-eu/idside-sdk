"""What an app may ask its users, given the providers it wraps.

The rule these tests protect: a question reaches an end user only if at least
one of the app's providers can enforce it PER REQUEST. An account-level control
lives on the developer's API key and applies to all of their users at once, so
asking one person about it would manufacture the appearance of a choice nobody
has — two users answering differently would be treated identically.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from idside import decidable_options, disclosures
from idside.options import _raw_enforcement, catalogue


class TestDecidableOptions:
    def test_asks_only_what_a_provider_enforces_per_request(self) -> None:
        names = [option.name for option in decidable_options(["openai"])]

        assert names == ["agentic_tools", "retention.conversation", "retention.cache"]

    def test_never_asks_about_a_control_that_lives_on_the_api_key(self) -> None:
        names = [option.name for option in decidable_options(["mistral"])]

        assert "training.foundation" not in names

    def test_returns_the_union_for_an_app_wrapping_several_providers(self) -> None:
        openai = {o.name for o in decidable_options(["openai"])}
        anthropic = {o.name for o in decidable_options(["anthropic"])}
        both = [o.name for o in decidable_options(["openai", "anthropic"])]

        assert set(both) == openai | anthropic
        assert len(both) == len(set(both)), (
            "a shared question must be asked once, not twice"
        )

    def test_order_does_not_depend_on_which_client_was_wrapped_first(self) -> None:
        one = [o.name for o in decidable_options(["openai", "anthropic"])]
        other = [o.name for o in decidable_options(["anthropic", "openai"])]

        assert one == other

    def test_says_which_providers_will_honour_each_answer(self) -> None:
        options = {o.name: o for o in decidable_options(["openai", "mistral"])}

        # Two providers, one question, and only one of them enforces the cache.
        assert options["retention.cache"].enforced_by == ("openai",)
        assert options["retention.conversation"].enforced_by == ("openai", "mistral")

    # `agentic_tools` was published as enforced once before, on all four
    # providers, while the mechanism existed nowhere in either SDK — a claim
    # with nothing behind it in the one document a customer repeats to their
    # DPO. It was demoted to `unsupported`, which left Gemini and Mistral with
    # nothing to ask at all, and the mechanism has now been written. These
    # numbers are the difference between the two, and they are pinned because
    # the first version of them was a lie. Mistral went from 1 to 2 on
    # 2026-07-26: `beta.conversations.start` was added as a call site, and it
    # carries a real `store`, so `retention.conversation` became a question its
    # users can actually be asked.
    #
    # Anthropic went from 2 to 1 on 2026-07-27, and for the same reason in
    # reverse. `data_residency` was declared enforced on the strength of
    # `inference_geo` being a real parameter — which it is. What does not exist
    # is a VALUE to force: its settings are `global` and `us`, and neither
    # answers "inside my region" for a European user, whose region we never
    # learn. The popup asked the question and nothing acted on the answer.
    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("openai", 3),
            ("anthropic", 1),
            ("gemini", 1),
            ("mistral", 2),
            ("deepseek", 1),
        ],
    )
    def test_what_each_provider_can_actually_be_asked(
        self, provider: str, expected: int
    ) -> None:
        assert len(decidable_options([provider])) == expected

    def test_the_one_question_every_provider_can_answer(self) -> None:
        """Tool refusal is the only lever that does not depend on the vendor.

        It works because the SDK sits on the outgoing request and can see what
        the developer is about to send — not because any provider offers it.
        """
        for provider in ("openai", "anthropic", "gemini", "mistral", "deepseek"):
            names = [o.name for o in decidable_options([provider])]
            assert "agentic_tools" in names, provider

    def test_a_provider_whose_answers_are_all_elsewhere_is_not_hidden(self) -> None:
        """An option nobody can decide is a finding, not a bug to paper over."""
        names = [o.name for o in decidable_options(["gemini"])]

        assert names == ["agentic_tools"]  # and nothing else: the rest is account-level
        assert disclosures(["gemini"]), "then the rest must appear as disclosures"

    def test_an_unknown_provider_contributes_nothing_rather_than_raising(self) -> None:
        """A newer SDK may meet a provider this build has never heard of."""
        assert decidable_options(["not-a-provider"]) == []
        assert decidable_options(["openai", "not-a-provider"]) == decidable_options(
            ["openai"]
        )

    def test_labels_come_in_both_languages(self) -> None:
        option = decidable_options(["openai"])[0]

        assert option.label("fr") == option.label_fr
        assert option.label("en") == option.label_en
        assert option.description("fr-FR") == option.description_fr

    def test_every_default_is_a_refusal(self) -> None:
        for option in decidable_options(
            ["openai", "anthropic", "gemini", "mistral", "deepseek"]
        ):
            assert option.default is False


class TestDisclosures:
    def test_states_what_the_user_cannot_decide(self) -> None:
        found = {d.option.name: d for d in disclosures(["mistral"])}

        assert found["training.foundation"].kind == "beyond_user_control"

    def test_terms_that_allow_the_processing_are_no_guarantee(self) -> None:
        """`contractual` says the terms decide, not which way they decide.

        Every provider that writes about human review lets its staff review
        flagged content, and both OpenAI's and Anthropic's no-training clauses
        give way to an opt-in the customer's organization can enable. Calling
        any of them a guarantee tells a user the opposite of the terms, in the
        helper a developer builds their own dialog from.
        """
        anthropic = {d.option.name: d for d in disclosures(["anthropic"])}
        openai = {d.option.name: d for d in disclosures(["openai"])}

        assert anthropic["training.foundation"].kind == "beyond_user_control"
        assert anthropic["human_review"].kind == "beyond_user_control"
        assert anthropic["retention.conversation"].kind == "beyond_user_control"
        assert openai["human_review"].kind == "beyond_user_control"
        assert openai["training.foundation"].kind == "beyond_user_control"

    def test_terms_that_rule_the_processing_out_are_a_guarantee(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No descriptor says `protective: true` today, so a synthetic one does.

        The branch is the one a dialog shows as good news; leaving it untested
        because no provider earns it right now is how it would break unseen the
        day one does.
        """
        for provider in ("anthropic", "openai"):
            entry = _raw_enforcement(provider)["training.foundation"]
            assert entry["level"] == "contractual"
            monkeypatch.setitem(entry, "protective", provider == "anthropic")

        alone = {d.option.name: d for d in disclosures(["anthropic"])}
        both = {d.option.name: d for d in disclosures(["anthropic", "openai"])}

        assert alone["training.foundation"].kind == "guaranteed"
        # One provider whose terms allow it is enough for no guarantee to hold.
        assert both["training.foundation"].kind == "beyond_user_control"
        assert both["training.foundation"].by_provider == {
            "anthropic": "contractual",
            "openai": "contractual",
        }

        monkeypatch.setitem(
            _raw_enforcement("openai")["training.foundation"], "protective", True
        )
        both = {d.option.name: d for d in disclosures(["anthropic", "openai"])}

        assert both["training.foundation"].kind == "guaranteed"

    def test_a_processing_the_api_does_not_perform_is_not_offered(self) -> None:
        """`absent` says what the provider does; the disclosure says who decided.

        Nobody configured memory on an OpenAI API app — the API has none — so it
        is stated as a control that does not exist, never as a decision the app
        made for its users. Beside a provider that has no switch for it either,
        the statement is the same: there is still nothing for anyone to decide.
        """
        found = {d.option.name: d for d in disclosures(["openai"])}

        assert found["memory"].kind == "not_offered"
        assert found["memory"].by_provider == {"openai": "absent"}

        both = {d.option.name: d for d in disclosures(["openai", "anthropic"])}

        assert both["memory"].kind == "not_offered"
        assert both["memory"].by_provider == {
            "openai": "absent",
            "anthropic": "unsupported",
        }

    # The escape hatch, and the only way training opt-out can ever be per-user.
    def test_marks_what_the_developer_could_still_give_their_users(self) -> None:
        found = {d.option.name: d for d in disclosures(["mistral"])}

        assert found["training.foundation"].routable is True
        assert "mistral" in found["training.foundation"].routable_from
        assert found["memory"].routable is False

    def test_an_unknown_provider_discloses_nothing_rather_than_everything(
        self,
    ) -> None:
        """The disclosure twin of the unknown-provider rule, and the riskier half.

        `decidable_options` erring towards silence asks one question too few.
        A disclosure list built from a provider nobody recognised would instead
        make *statements* about how a provider treats data, with nothing behind
        them: every catalogue entry, asserted at no level at all.
        """
        assert disclosures(["not-a-provider"]) == []
        assert disclosures(["openai", "not-a-provider"]) == disclosures(["openai"])

    def test_never_discloses_something_it_also_asked(self) -> None:
        providers = ["openai", "anthropic", "gemini", "mistral"]
        asked = {o.name for o in decidable_options(providers)}
        stated = {d.option.name for d in disclosures(providers)}

        assert not asked & stated

    def test_nothing_vanishes_between_the_two_lists(self) -> None:
        providers = ["openai", "anthropic", "gemini", "mistral"]
        asked = {o.name for o in decidable_options(providers)}
        stated = {d.option.name for d in disclosures(providers)}
        everything = {o["name"] for o in catalogue()["options"]}

        assert asked | stated == everything


def test_the_disclosure_kinds_match_the_node_sdk_verbatim() -> None:
    """These strings cross an SDK boundary, so they are a contract.

    `kind` is a VALUE, not a field name: anything that logs, persists or
    branches on it diverges the moment the two SDKs disagree. They already did
    once — Node said `beyondUserControl` where Python said `beyond_user_control`
    — and nothing caught it because no test compared the two.

    This asserts the vocabulary; `scripts/check_sdk_parity.sh` runs both SDKs and
    compares their actual answers.
    """
    source = (
        Path(__file__).resolve().parents[2] / "node" / "src" / "options.ts"
    ).read_text(encoding="utf-8")

    for kind in ("guaranteed", "beyond_user_control", "not_offered"):
        assert f'"{kind}"' in source, f"Node does not know the kind {kind!r}"

    # And the reverse: Node must not invent one Python has never heard of.
    assert "beyondUserControl" not in source


class TestRoutedOptions:
    """What an app may ask once it holds more than one credential.

    A question reaches a user only if the app can honour two different answers.
    For an account-level setting that means credential routing: one credential
    per posture, and the SDK sends each call on the one the answer requires. So
    the same option is a disclosure for an app with one key and a real question
    for the app next to it — the descriptor cannot decide alone, and neither can
    the SDK. It takes the app's own configuration.
    """

    _PROVISIONED: ClassVar[dict[str, list[str]]] = {
        "mistral": ["default", "no_training"]
    }

    def test_a_second_credential_turns_a_disclosure_into_a_question(self) -> None:
        before = {o.name for o in decidable_options(["mistral"])}
        after = {o.name for o in decidable_options(["mistral"], self._PROVISIONED)}

        assert "training.foundation" not in before
        assert "training.foundation" in after

    def test_one_credential_is_not_a_choice(self) -> None:
        """One posture for everyone is what the app already had."""
        options = decidable_options(["mistral"], {"mistral": ["default"]})

        assert "training.foundation" not in {o.name for o in options}

    def test_two_labels_for_the_same_posture_are_still_one_posture(self) -> None:
        options = decidable_options(["mistral"], {"mistral": ["a", "a"]})

        assert "training.foundation" not in {o.name for o in options}

    def test_a_routed_option_says_it_is_routed_rather_than_forced(self) -> None:
        """The answer is as real either way; a UI may want to say which."""
        option = next(
            o
            for o in decidable_options(["mistral"], self._PROVISIONED)
            if o.name == "training.foundation"
        )

        assert option.routed_by == ("mistral",)
        assert option.enforced_by == ()

    def test_routing_cannot_invent_a_question_the_provider_never_offers(self) -> None:
        """`memory` is not routable anywhere: no credential carries it."""
        options = decidable_options(["mistral"], self._PROVISIONED)

        assert "memory" not in {o.name for o in options}

    def test_what_became_a_question_stops_being_a_disclosure(self) -> None:
        stated = {d.option.name for d in disclosures(["mistral"], self._PROVISIONED)}

        assert "training.foundation" not in stated

    def test_routing_one_provider_says_nothing_about_another(self) -> None:
        options = decidable_options(["mistral", "gemini"], self._PROVISIONED)
        routed = {o.name: o.routed_by for o in options}

        assert routed["training.foundation"] == ("mistral",)

    def test_routing_never_asks_what_no_posture_honours(self) -> None:
        """Two projects, two organisations: human review is still not a choice.

        Every Gemini project lets Google's staff assess flagged content, and no
        Mistral console setting reaches human review; asking a user to refuse
        it, because the app holds two keys, is the false choice this module
        exists to prevent.
        """
        routed = {"gemini": ["default", "billed"], "mistral": ["default", "zdr"]}
        asked = {o.name for o in decidable_options(["gemini", "mistral"], routed)}
        stated = {d.option.name: d for d in disclosures(["gemini", "mistral"], routed)}

        assert "human_review" not in asked
        assert stated["human_review"].kind == "beyond_user_control"
        assert stated["human_review"].routable_from == ()


def test_the_enforcement_reader_refuses_a_name_that_is_not_a_provider() -> None:
    """The path guard holds on its own, not because a caller filters first.

    `decidable_options()` is public API taking caller-supplied strings, and the
    provider name used to be interpolated straight into a filesystem path:
    `decidable_options(["../../../etc/passwd"])` read and parsed an arbitrary
    file, whose contents then flowed into the options handed to a consent UI.
    The callers above happen to check the name against the loaded descriptors
    first — which is exactly the kind of thing a refactor moves.
    """
    assert _raw_enforcement("../../../etc/passwd") == {}
    # The one that proves the guard rather than the filesystem: this path really
    # does resolve to a readable descriptor, so only the name check stops it.
    assert _raw_enforcement("../providers/openai") == {}
    assert _raw_enforcement("openai"), "a real provider must still be read"
