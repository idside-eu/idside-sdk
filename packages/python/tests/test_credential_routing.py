"""Credential routing — how an account setting becomes a per-person one.

No provider lets a developer switch training off per request. The setting lives
on the **credential**: per project at OpenAI, per workspace at Anthropic, per
project at Gemini, per organisation at Mistral. A developer holding one key
therefore imposes one posture on all of their users at once, however carefully
each of them answered.

Unless they hold several. If they provision one credential per posture, the SDK
can send each call on the one matching that user's answer, and the account-level
control becomes a personal one. It is the only route by which a training
opt-out can be honoured per user today.

Two rules shape the design:

- the developer labels their credentials, and stops there. Which posture
  satisfies which answer is policy, and policy is the cloud's — a callback
  letting the developer choose would put them back in charge of the user's
  preferences (invariant #1, the opaque tunnel);
- a posture the developer has not provisioned is a refusal, never a fallback.
  Falling back would send the call on a credential the user's answer excludes,
  and nobody would ever see it happen.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from idside import consent
from idside._transport import ConsentDecision
from idside.adapters import GeminiAdapter, ProviderAdapter
from idside.exceptions import (
    CredentialRoutingError,
    IDSideError,
    MissingCredentialError,
)
from idside.providers import load_descriptors

_OPENAI = load_descriptors()["openai"]
_ANTHROPIC = load_descriptors()["anthropic"]
_GEMINI = load_descriptors()["gemini"]
_DEEPSEEK = load_descriptors()["deepseek"]

#: Fictitious project ids, in the shape OpenAI issues them.
_DEFAULT_PROJECT = "proj_medhelp_default"
_ZDR_PROJECT = "proj_medhelp_zdr"


class _Recording:
    """One provider endpoint, remembering what it was called with."""

    def __init__(self, name: str = "base") -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": f"chatcmpl_{self.name}"}


def _fake_openai(name: str = "base") -> tuple[Any, _Recording]:
    completions = _Recording(name)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def _fake_anthropic(name: str = "base") -> tuple[Any, _Recording]:
    messages = _Recording(name)
    return SimpleNamespace(messages=messages), messages


class _FakeConsent:
    """The cloud, answering with a posture the SDK must honour."""

    def __init__(self, posture: str | None = None) -> None:
        self.posture = posture
        self.calls: list[dict[str, Any]] = []

    def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        return ConsentDecision(
            consent_state={"training.foundation": False},
            params_forced={"store": False},
            act_token="tok_1",
            source="widget",
            legal_basis="consent",
            binding="user_id",
            credential_posture=self.posture,
        )


def _call(wrapped: Any, **extra: Any) -> Any:
    return wrapped.chat.completions.create(
        model="gpt-4o", messages=[], user_id="usr_marie_456", **extra
    )


# ── Routing by header: OpenAI validates OpenAI-Project per request ──────────


def test_a_call_with_no_posture_goes_out_exactly_as_before() -> None:
    """Most calls are not routed. Nothing about them changes."""
    client, completions = _fake_openai()
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent(),
        credentials={"default": _DEFAULT_PROJECT, "zero_retention": _ZDR_PROJECT},
    )

    _call(wrapped)

    assert "OpenAI-Project" not in completions.calls[-1]["extra_headers"]


def test_the_cloud_s_posture_selects_the_project() -> None:
    client, completions = _fake_openai()
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent("zero_retention"),
        credentials={"default": _DEFAULT_PROJECT, "zero_retention": _ZDR_PROJECT},
    )

    _call(wrapped)

    assert completions.calls[-1]["extra_headers"]["OpenAI-Project"] == _ZDR_PROJECT


def test_the_project_travels_with_the_act_token_not_instead_of_it() -> None:
    """Routing is added enforcement, never a substitute for the proof."""
    client, completions = _fake_openai()
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent("zero_retention"),
        credentials={"zero_retention": _ZDR_PROJECT},
    )

    _call(wrapped)

    headers = completions.calls[-1]["extra_headers"]
    assert headers["X-AI-Consent-Token"] == "tok_1"
    assert headers["OpenAI-Project"] == _ZDR_PROJECT
    assert completions.calls[-1]["store"] is False


def test_the_developer_s_own_headers_survive_routing() -> None:
    client, completions = _fake_openai()
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent("zero_retention"),
        credentials={"zero_retention": _ZDR_PROJECT},
    )

    _call(wrapped, extra_headers={"X-Request-Id": "req_42"})

    assert completions.calls[-1]["extra_headers"]["X-Request-Id"] == "req_42"


# ── Fail-closed: a posture nobody provisioned is a refusal ──────────────────


def test_a_posture_the_developer_never_provisioned_stops_the_call() -> None:
    """The whole point. Silence here would send the call on the wrong project."""
    client, completions = _fake_openai()
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent("zero_retention"),
        credentials={"default": _DEFAULT_PROJECT},
    )

    with pytest.raises(MissingCredentialError):
        _call(wrapped)

    assert completions.calls == []


def test_a_posture_with_no_credentials_at_all_stops_the_call() -> None:
    """An app that never provisioned anything is not a special case."""
    client, completions = _fake_openai()
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent("zero_retention"),
    )

    with pytest.raises(MissingCredentialError):
        _call(wrapped)

    assert completions.calls == []


def test_the_refusal_names_the_posture_the_provider_and_what_exists() -> None:
    """An error a developer can act on without reading the SDK."""
    client, _ = _fake_openai()
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent("zero_retention"),
        credentials={"default": _DEFAULT_PROJECT},
    )

    with pytest.raises(MissingCredentialError) as caught:
        _call(wrapped)

    error = caught.value
    assert error.posture == "zero_retention"
    assert error.provider == "openai"
    assert error.provisioned == ("default",)
    message = str(error)
    assert "zero_retention" in message
    assert "project" in message  # the unit, so "provision one" means something


def test_the_refusal_never_repeats_a_credential() -> None:
    """A credential is a secret. It does not belong in an exception."""
    client, _ = _fake_openai()
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent("zero_retention"),
        credentials={"default": _DEFAULT_PROJECT},
    )

    with pytest.raises(MissingCredentialError) as caught:
        _call(wrapped)

    assert _DEFAULT_PROJECT not in str(caught.value)


def test_missing_credential_is_a_routing_error_is_an_idside_error() -> None:
    """Catching the base class is enough to fail closed."""
    assert issubclass(MissingCredentialError, CredentialRoutingError)
    assert issubclass(CredentialRoutingError, IDSideError)


# ── What the cloud is told ──────────────────────────────────────────────────


def test_the_provisioned_postures_are_advertised_to_the_cloud() -> None:
    """The cloud cannot tell a developer what is missing without knowing this.

    Sorted, so two processes with the same configuration send the same payload.
    """
    client, _ = _fake_openai()
    consent_client = _FakeConsent()
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=consent_client,
        credentials={"zero_retention": _ZDR_PROJECT, "default": _DEFAULT_PROJECT},
    )

    _call(wrapped)

    assert consent_client.calls[-1]["credential_postures"] == (
        "default",
        "zero_retention",
    )


def test_an_app_without_routing_still_says_it_could_route() -> None:
    """Empty is a different answer from absent: it says this SDK understands.

    An older SDK sends no such field at all, and would ignore a posture it was
    given — which is exactly when the cloud must not count on routing.
    """
    client, _ = _fake_openai()
    consent_client = _FakeConsent()
    wrapped = consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=consent_client
    )

    _call(wrapped)

    assert consent_client.calls[-1]["credential_postures"] == ()


def test_the_credential_itself_never_reaches_the_cloud() -> None:
    client, _ = _fake_openai()
    consent_client = _FakeConsent()
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=consent_client,
        credentials={"zero_retention": _ZDR_PROJECT},
    )

    _call(wrapped)

    assert _ZDR_PROJECT not in repr(consent_client.calls[-1])


def test_the_credential_never_reaches_the_request_hash() -> None:
    """The proof binds the request the developer wrote, not our plumbing."""
    adapter = ProviderAdapter(_OPENAI)
    plain = adapter.extract_request_metadata(model="gpt-4o", messages=[])
    routed = adapter.extract_request_metadata(
        model="gpt-4o", messages=[], extra_headers={"OpenAI-Project": _ZDR_PROJECT}
    )

    assert plain["request_hash"] == routed["request_hash"]


# ── Routing by client: everywhere the header does not exist ─────────────────


def test_a_provider_without_a_routing_header_refuses_a_bare_identifier() -> None:
    """Anthropic reads no such header: a string here would be silently ignored."""
    client, _ = _fake_anthropic()

    with pytest.raises(CredentialRoutingError) as caught:
        consent.wrap(
            client,
            adapter=ProviderAdapter(_ANTHROPIC),
            consent_client=_FakeConsent(),
            credentials={"no_training": "ws_medhelp_private"},
        )

    message = str(caught.value)
    assert "workspace" in message
    assert "client" in message


def test_a_second_client_carries_the_second_posture() -> None:
    base, base_messages = _fake_anthropic("base")
    private, private_messages = _fake_anthropic("private")
    wrapped = consent.wrap(
        base,
        adapter=ProviderAdapter(_ANTHROPIC),
        consent_client=_FakeConsent("no_training"),
        credentials={"default": base, "no_training": private},
    )

    wrapped.messages.create(model="claude-sonnet-4", messages=[], user_id="usr_marie")

    assert len(private_messages.calls) == 1
    assert base_messages.calls == []  # the base client never saw it


def test_a_routed_call_still_carries_the_token_and_the_forced_params() -> None:
    base, _ = _fake_anthropic("base")
    private, private_messages = _fake_anthropic("private")
    wrapped = consent.wrap(
        base,
        adapter=ProviderAdapter(_ANTHROPIC),
        consent_client=_FakeConsent("no_training"),
        credentials={"no_training": private},
    )

    wrapped.messages.create(model="claude-sonnet-4", messages=[], user_id="usr_marie")

    call = private_messages.calls[-1]
    assert call["extra_headers"]["X-AI-Consent-Token"] == "tok_1"
    # Anthropic injects forced params as kwargs: they belong in the body, which
    # is the only place the API reads them. The token is the header, and stays one.
    assert call["store"] is False


def test_the_routed_client_returns_its_own_answer() -> None:
    base, _ = _fake_anthropic("base")
    private, _ = _fake_anthropic("private")
    wrapped = consent.wrap(
        base,
        adapter=ProviderAdapter(_ANTHROPIC),
        consent_client=_FakeConsent("no_training"),
        credentials={"no_training": private},
    )

    result = wrapped.messages.create(model="c", messages=[], user_id="usr_marie")

    assert result == {"id": "chatcmpl_private"}


def test_the_base_client_may_be_its_own_posture_without_looping() -> None:
    """`{"default": client}` is the honest way to say what the base key is.

    The routed method is captured before the client is patched, so a call
    routed back to it runs the provider once, not the enforcement twice.
    """
    base, base_messages = _fake_anthropic("base")
    private, _ = _fake_anthropic("private")
    wrapped = consent.wrap(
        base,
        adapter=ProviderAdapter(_ANTHROPIC),
        consent_client=_FakeConsent("default"),
        credentials={"default": base, "no_training": private},
    )

    wrapped.messages.create(model="c", messages=[], user_id="usr_marie")

    assert len(base_messages.calls) == 1


def test_gemini_routes_by_client_too() -> None:
    """google-genai takes the key at construction; there is no header to set."""
    base = SimpleNamespace(models=SimpleNamespace(generate_content=_Recording().create))
    routed = _Recording("routed")
    other = SimpleNamespace(models=SimpleNamespace(generate_content=routed.create))
    wrapped = consent.wrap(
        base,
        adapter=GeminiAdapter(_GEMINI),
        consent_client=_FakeConsent("no_training"),
        credentials={"no_training": other},
    )

    wrapped.models.generate_content(
        model="gemini-2.0-flash", contents="hi", user_id="usr_marie"
    )

    assert (
        routed.calls[-1]["config"]["http_options"]["headers"]["X-AI-Consent-Token"]
        == "tok_1"
    )


# ── Configuration errors, raised where they can still be fixed ──────────────


def test_a_provider_that_cannot_route_at_all_refuses_credentials() -> None:
    """DeepSeek declares no routing: there is no posture to switch between."""
    client, _ = _fake_openai()

    with pytest.raises(CredentialRoutingError) as caught:
        consent.wrap(
            client,
            adapter=ProviderAdapter(_DEEPSEEK),
            consent_client=_FakeConsent(),
            credentials={"default": _DEFAULT_PROJECT},
        )

    assert "deepseek" in str(caught.value)


def test_an_api_key_pasted_where_a_project_id_belongs_is_refused() -> None:
    """The one mistake that would put a secret in an outgoing header."""
    client, _ = _fake_openai()

    with pytest.raises(CredentialRoutingError) as caught:
        consent.wrap(
            client,
            adapter=ProviderAdapter(_OPENAI),
            consent_client=_FakeConsent(),
            credentials={"zero_retention": "sk-test-not-a-project-id"},
        )

    assert "sk-test-not-a-project-id" not in str(caught.value)


def test_a_credential_carrying_a_newline_is_refused() -> None:
    """A routed value is written into an HTTP header. CRLF is header injection."""
    client, _ = _fake_openai()

    with pytest.raises(CredentialRoutingError):
        consent.wrap(
            client,
            adapter=ProviderAdapter(_OPENAI),
            consent_client=_FakeConsent(),
            credentials={"zero_retention": "proj_ok\r\nX-Injected: 1"},
        )


def test_an_alternative_client_missing_the_call_site_is_refused_at_wrap() -> None:
    """Better a startup error than a routed call that finds nothing to call."""
    base, _ = _fake_anthropic()

    with pytest.raises(CredentialRoutingError) as caught:
        consent.wrap(
            base,
            adapter=ProviderAdapter(_ANTHROPIC),
            consent_client=_FakeConsent(),
            credentials={"no_training": SimpleNamespace()},
        )

    assert "no_training" in str(caught.value)


def test_a_second_client_of_the_same_provider_is_accepted_as_a_credential() -> None:
    """The pair to the refusal below, and the half that would break routing.

    The provider check is best-effort and refuses only on a positive
    identification — so it has to positively identify the ORDINARY case too, and
    not refuse it. A false positive here would make credential routing
    unusable for exactly the clients it exists for: two real clients of one
    provider, built with two different keys.
    """
    openai = pytest.importorskip("openai")
    base = openai.OpenAI(api_key="sk-base")  # pragma: allowlist secret
    zero_retention = openai.OpenAI(api_key="sk-zdr")  # pragma: allowlist secret

    wrapped = consent.wrap(
        base,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent(),
        credentials={"zero_retention": zero_retention},
    )

    assert wrapped is base


def test_a_credential_belonging_to_another_provider_is_refused_at_wrap() -> None:
    """Routing must not be able to change which company receives the prompt.

    The shapes coincide — an `OpenAI` client pointed at another vendor's base
    URL is still an `OpenAI` client — so the check is best-effort and refuses
    only on a positive identification. When it does identify one, the call it
    would have permitted is a prompt sent to a company the user's answer never
    mentioned, on the strength of a posture label.
    """
    openai = pytest.importorskip("openai")
    base, _ = _fake_anthropic()
    another_vendor = openai.OpenAI(api_key="sk-test")  # pragma: allowlist secret

    with pytest.raises(CredentialRoutingError) as caught:
        consent.wrap(
            base,
            adapter=ProviderAdapter(_ANTHROPIC),
            consent_client=_FakeConsent(),
            credentials={"no_training": another_vendor},
        )

    message = str(caught.value)
    assert "openai" in message and "anthropic" in message


def test_an_already_wrapped_client_is_refused_as_a_credential() -> None:
    """Routing to it would authorize twice and stack two tokens on one call."""
    base, _ = _fake_anthropic("base")
    other, _ = _fake_anthropic("other")
    consent.wrap(
        other, adapter=ProviderAdapter(_ANTHROPIC), consent_client=_FakeConsent()
    )

    with pytest.raises(CredentialRoutingError) as caught:
        consent.wrap(
            base,
            adapter=ProviderAdapter(_ANTHROPIC),
            consent_client=_FakeConsent(),
            credentials={"no_training": other},
        )

    assert "already" in str(caught.value)


def test_an_empty_posture_label_is_refused() -> None:
    client, _ = _fake_openai()

    with pytest.raises(CredentialRoutingError):
        consent.wrap(
            client,
            adapter=ProviderAdapter(_OPENAI),
            consent_client=_FakeConsent(),
            credentials={"": _DEFAULT_PROJECT},
        )


def test_no_credentials_leaves_the_wrapped_client_untouched() -> None:
    """Nobody who ignores this feature pays for it."""
    client, completions = _fake_openai()
    wrapped = consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=_FakeConsent()
    )

    _call(wrapped)

    assert "OpenAI-Project" not in completions.calls[-1]["extra_headers"]


def test_routing_does_not_mutate_the_caller_s_arguments() -> None:
    client, _ = _fake_openai()
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent("zero_retention"),
        credentials={"zero_retention": _ZDR_PROJECT},
    )
    headers: dict[str, Any] = {}

    _call(wrapped, extra_headers=headers)

    assert headers == {}


# ── The mechanism inside the adapters ───────────────────────────────────────


@pytest.mark.parametrize(
    ("adapter", "read"),
    [
        (
            ProviderAdapter(_OPENAI),
            lambda out: out["extra_headers"],
        ),
        (
            GeminiAdapter(_GEMINI),
            lambda out: out["config"]["http_options"]["headers"],
        ),
    ],
    ids=["generic", "gemini"],
)
def test_every_built_in_adapter_carries_a_routed_header(
    adapter: ProviderAdapter, read: Any
) -> None:
    """An adapter that drops it would send the call on the wrong credential.

    Only the engines shipped here are proven; a custom adapter that overrides
    `apply()` must forward `credential_headers` too. It cannot fail silently —
    `consent.wrap()` checks the header actually landed before calling the
    provider — but it will fail.
    """
    out = adapter.apply(
        {}, {}, "tok_1", credential_headers={"OpenAI-Project": "proj_x"}
    )

    assert read(out)["OpenAI-Project"] == "proj_x"


def test_an_adapter_that_swallows_the_routed_header_fails_closed() -> None:
    """The last line of defence: never send a call routing was meant to move."""

    class _Forgetful(ProviderAdapter):
        def apply(
            self,
            kwargs: dict[str, Any],
            params_forced: dict[str, Any],
            token: str,
            credential_headers: dict[str, str] | None = None,
        ) -> dict[str, Any]:
            return super().apply(kwargs, params_forced, token)

    client, completions = _fake_openai()
    wrapped = consent.wrap(
        client,
        adapter=_Forgetful(_OPENAI),
        consent_client=_FakeConsent("zero_retention"),
        credentials={"zero_retention": _ZDR_PROJECT},
    )

    with pytest.raises(CredentialRoutingError):
        _call(wrapped)

    assert completions.calls == []


def test_a_bare_sequence_of_credentials_is_refused() -> None:
    """A list carries no posture, and a posture is the whole point."""
    client, _ = _fake_openai()

    with pytest.raises(CredentialRoutingError):
        consent.wrap(
            client,
            adapter=ProviderAdapter(_OPENAI),
            consent_client=_FakeConsent(),
            credentials=[_DEFAULT_PROJECT, _ZDR_PROJECT],  # type: ignore[arg-type]
        )
