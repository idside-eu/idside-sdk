"""Refusing agentic tools — the one lever that does not depend on a provider.

Every provider's zero-retention arrangement excludes agentic features while the
API itself does not block them. A call that hands the model tools therefore
falls outside the retention regime the user was promised, and the SDK is the
only component that can see it happening: it is the only one that sees the
outgoing request.

That is also the constraint on the design. The cloud must never be told whether
a given call carried tools — it would be learning something about the request,
and the product's whole claim is that it cannot. So the cloud answers from the
consent state alone (*"this user refuses agentic tools"*) and the SDK decides
locally whether that applies to the call in front of it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from idside import consent
from idside._transport import ConsentDecision
from idside.adapters import GeminiAdapter, ProviderAdapter
from idside.exceptions import ConsentTokenError, ToolUseRefusedError
from idside.providers import ProviderDescriptor, load_descriptors

_OPENAI = load_descriptors()["openai"]
_GEMINI = load_descriptors()["gemini"]

_TOOLS = [{"type": "function", "function": {"name": "lookup_patient_record"}}]


def _decision(constraints: dict[str, Any]) -> ConsentDecision:
    return ConsentDecision(
        consent_state={"agentic_tools": False},
        params_forced={},
        act_token="tok_1",
        source="widget",
        legal_basis="consent",
        binding="user_id",
        constraints=constraints,
    )


class _Recording:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "chatcmpl_fake"}


class _FakeConsent:
    def __init__(self, decision: ConsentDecision) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        return self.decision


# ── The adapter-level mechanism ─────────────────────────────────────────────


def test_no_constraint_leaves_the_call_exactly_as_it_was() -> None:
    adapter = ProviderAdapter(_OPENAI)
    kwargs = {"model": "gpt-4o", "tools": _TOOLS}

    assert adapter.apply_constraints(kwargs, {}) == kwargs


def test_a_refusal_does_nothing_to_a_call_that_carries_no_tools() -> None:
    """The user refused agentic tools, not AI. Most calls are unaffected."""
    adapter = ProviderAdapter(_OPENAI)
    kwargs = {"model": "gpt-4o", "messages": []}

    assert adapter.apply_constraints(kwargs, {"tools": "refuse"}) == kwargs


def test_a_refusal_stops_a_call_that_carries_tools() -> None:
    adapter = ProviderAdapter(_OPENAI)

    with pytest.raises(ToolUseRefusedError) as caught:
        adapter.apply_constraints(
            {"model": "gpt-4o", "tools": _TOOLS}, {"tools": "refuse"}
        )

    assert "tools" in str(caught.value)


# One message, true whoever refused. It used to end on "run the Preference
# Center so the user can answer", which sends an organization's developer to a
# widget that does not exist for its employees: there, a policy refused, and no
# answer lifts it. The SDK does not know which happened (invariant #1), so the
# message says what is true in every case — word for word what Node says, which
# the parity script compares.


def test_the_refusal_names_every_field_it_read_and_points_at_no_widget() -> None:
    adapter = ProviderAdapter(_OPENAI)

    with pytest.raises(ToolUseRefusedError) as caught:
        adapter.apply_constraints(
            {
                "model": "gpt-4o",
                "tools": _TOOLS,
                "functions": [{"name": "lookup_patient_record"}],
            },
            {"tools": "refuse"},
            "chat.completions.create",
        )

    assert str(caught.value) == (
        "Agentic tools are not accepted for this user (refused by the person, "
        "never asked, or refused by a policy), and this call passes `tools`, "
        "`functions`. The AI call was not made. Retry without them; never "
        "retry the same call, it will be refused again."
    )


def test_a_reference_strip_cannot_take_out_is_refused_in_the_same_words() -> None:
    adapter = ProviderAdapter(_OPENAI)

    with pytest.raises(ToolUseRefusedError) as caught:
        adapter.apply_constraints(
            {
                "model": "gpt-4o",
                "tools": _TOOLS,
                "conversation": "conv_123",
                "previous_response_id": "resp_123",
            },
            {"tools": "strip"},
            "responses.create",
        )

    assert str(caught.value) == (
        "Agentic tools are not accepted for this user (refused by the person, "
        "never asked, or refused by a policy), and this call passes "
        "`conversation`, `previous_response_id`, which can bring tools the SDK "
        "cannot see or remove without changing what the call asks. The AI call "
        "was not made. Retry without them; never retry the same call, it "
        "will be refused again."
    )


def test_strip_removes_the_key_rather_than_emptying_it() -> None:
    """Several providers reject an empty tools list.

    And "no tools" is what the user asked for, not "zero tools".
    """
    adapter = ProviderAdapter(_OPENAI)

    out = adapter.apply_constraints(
        {"model": "gpt-4o", "tools": _TOOLS}, {"tools": "strip"}
    )

    assert "tools" not in out
    assert out["model"] == "gpt-4o"


def test_strip_does_not_mutate_the_caller_s_own_dict() -> None:
    adapter = ProviderAdapter(_OPENAI)
    kwargs = {"model": "gpt-4o", "tools": _TOOLS}

    adapter.apply_constraints(kwargs, {"tools": "strip"})

    assert kwargs["tools"] == _TOOLS


def test_an_unknown_constraint_is_not_treated_as_no_constraint() -> None:
    """Guessing is the one failure mode this whole path exists to prevent."""
    adapter = ProviderAdapter(_OPENAI)

    with pytest.raises(ConsentTokenError):
        adapter.apply_constraints(
            {"model": "gpt-4o", "tools": _TOOLS}, {"tools": "quarantine"}
        )


def test_a_provider_whose_tools_the_sdk_cannot_see_fails_closed() -> None:
    """A refusal we cannot apply is not a refusal we may ignore."""
    descriptor = ProviderDescriptor.from_dict(
        {
            "name": "x",
            "client_module": "x",
            "call_sites": [{"id": "chat.create", "path": ["chat", "create"]}],
        }
    )

    with pytest.raises(ConsentTokenError) as caught:
        ProviderAdapter(descriptor).apply_constraints(
            {"tools": _TOOLS}, {"tools": "refuse"}
        )

    assert "does not say where tools travel" in str(caught.value)


# ── Nested tools: google-genai puts them inside `config` ────────────────────


def test_gemini_tools_are_found_inside_config() -> None:
    adapter = GeminiAdapter(_GEMINI)

    with pytest.raises(ToolUseRefusedError):
        adapter.apply_constraints(
            {
                "model": "gemini-2.0-flash",
                "contents": "hi",
                "config": {"tools": _TOOLS},
            },
            {"tools": "refuse"},
        )


def test_gemini_config_without_tools_passes_through() -> None:
    adapter = GeminiAdapter(_GEMINI)
    kwargs = {
        "model": "gemini-2.0-flash",
        "contents": "hi",
        "config": {"temperature": 0.5},
    }

    assert adapter.apply_constraints(kwargs, {"tools": "refuse"}) == kwargs


def test_gemini_strip_keeps_the_rest_of_the_config() -> None:
    adapter = GeminiAdapter(_GEMINI)

    out = adapter.apply_constraints(
        {"model": "gemini-2.0-flash", "config": {"temperature": 0.5, "tools": _TOOLS}},
        {"tools": "strip"},
    )

    assert out["config"] == {"temperature": 0.5}


def test_gemini_finds_tools_in_a_real_config_object() -> None:
    """A caller may pass a GenerateContentConfig, not a dict."""
    types = pytest.importorskip("google.genai.types")
    adapter = GeminiAdapter(_GEMINI)
    config = types.GenerateContentConfig(
        temperature=0.5,
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )

    with pytest.raises(ToolUseRefusedError):
        adapter.apply_constraints(
            {"model": "gemini-2.0-flash", "config": config}, {"tools": "refuse"}
        )


class _ImpostorConfig:
    """Something that looks like a Pydantic model and does not behave like one.

    `model_dump` is duck-typed here on purpose — google-genai's config really
    may be a Pydantic model — so what makes it trustworthy is not the attribute
    but what the attribute returns. A test double, a mock, or a model from a
    library with a different `model_dump` contract satisfies the check and
    hands back something that is not a mapping.
    """

    def model_dump(self, **_kwargs: Any) -> Any:
        return ["temperature=1"]


@pytest.mark.parametrize(
    ("config", "named"),
    [("temperature=1", "str"), (_ImpostorConfig(), "_ImpostorConfig")],
    ids=["a-plain-string", "a-model_dump-that-returns-a-list"],
)
def test_a_config_the_sdk_cannot_read_stops_the_call_rather_than_passing_it(
    config: Any, named: str
) -> None:
    """A config that answers nothing is not a config that carries no tools.

    `_read_path` is deliberately forgiving — a step it cannot walk reads as
    "nothing there" — and on its own that would let a call whose tools the SDK
    could not see go out under a refusal. It does not, because the same
    unreadable config is what `apply()` refuses a moment later: the ACT token
    has nowhere to go, so the call is not made. Both halves are asserted here,
    because the first one alone would be a fail-open.
    """
    adapter = GeminiAdapter(_GEMINI)
    kwargs = {"model": "gemini-2.0-flash", "contents": "hi", "config": config}

    # Nothing found, nothing refused: the tools check steps aside.
    assert adapter.apply_constraints(kwargs, {"tools": "refuse"}) == kwargs

    # And the call still does not go out.
    with pytest.raises(ConsentTokenError) as caught:
        adapter.apply(kwargs, {}, "tok_1")

    assert named in str(caught.value), "the message must name what was passed"


def test_dropping_a_path_that_is_not_there_changes_nothing() -> None:
    """The helper's other half: asked to remove what is absent, it removes nothing.

    It returns the caller's own object rather than a copy of it, which is the
    point — `apply_constraints` hands its result straight on, and a call with
    nothing to strip must reach the provider as the application wrote it.
    """
    from idside.adapters.base import _drop_path

    kwargs = {"model": "gemini-2.0-flash", "contents": "hi"}

    assert _drop_path(kwargs, ("tools",)) is kwargs
    assert _drop_path(kwargs, ("config", "tools")) is kwargs


# ── Every argument that can hand the model a tool ───────────────────────────


@pytest.mark.parametrize("call_site", [None, "responses.create"])
def test_without_a_call_site_every_call_site_s_tool_fields_are_read(
    call_site: str | None,
) -> None:
    """A caller that names no endpoint gets the wide reading, never the narrow one."""
    adapter = ProviderAdapter(_OPENAI)
    kwargs = {"model": "gpt-4o", "input": "hi", "previous_response_id": "resp_1"}

    with pytest.raises(ToolUseRefusedError):
        adapter.apply_constraints(kwargs, {"tools": "refuse"}, call_site=call_site)
    with pytest.raises(ToolUseRefusedError):
        adapter.apply_constraints(
            {"model": "gpt-4o", "web_search_options": {}},
            {"tools": "refuse"},
            call_site=call_site if call_site is None else "chat.completions.create",
        )


@pytest.mark.parametrize(
    ("value", "carries"),
    [
        (None, False),
        ("omit", False),
        ([], False),
        ((), False),
        ({}, False),
        (False, True),
        (0, True),
        ("", True),
        ("a-tool", True),
        (_TOOLS, True),
    ],
    ids=[
        "None",
        "omit",
        "empty-list",
        "empty-tuple",
        "empty-mapping",
        "False",
        "0",
        "empty-string",
        "string",
        "tools",
    ],
)
def test_definitions_are_absent_only_when_empty_or_not_given(
    value: Any, carries: bool
) -> None:
    """The rule both SDKs apply: an unexpected value is no proof of absence."""
    if value == "omit":
        value = pytest.importorskip("openai").omit
    adapter = ProviderAdapter(_OPENAI)
    kwargs = {"model": "gpt-4o", "tools": value}

    if carries:
        with pytest.raises(ToolUseRefusedError):
            adapter.apply_constraints(kwargs, {"tools": "refuse"})
    else:
        assert adapter.apply_constraints(kwargs, {"tools": "refuse"}) == kwargs


def test_an_input_item_is_recognised_whether_a_dict_or_a_typed_object() -> None:
    """``input=response.output`` hands items back as models, not dicts."""
    adapter = ProviderAdapter(_OPENAI)
    item = SimpleNamespace(type="mcp_approval_response", approve=True)

    with pytest.raises(ToolUseRefusedError) as caught:
        adapter.apply_constraints(
            {"model": "gpt-4o", "input": [item]},
            {"tools": "refuse"},
            call_site="responses.create",
        )

    assert "`input`" in str(caught.value)


def test_a_tool_field_of_a_kind_the_sdk_does_not_know_is_read_as_present() -> None:
    """A kind nobody taught the SDK is not one it may assume empty."""
    descriptor = ProviderDescriptor.from_dict(
        {
            "name": "x",
            "client_module": "x",
            "tools_param": ["tools"],
            "call_sites": [
                {
                    "id": "chat.create",
                    "path": ["chat", "create"],
                    "tool_fields": [{"path": ["assistant"], "kind": "server_object"}],
                }
            ],
        }
    )

    with pytest.raises(ToolUseRefusedError):
        ProviderAdapter(descriptor).apply_constraints(
            {"assistant": "asst_1"}, {"tools": "strip"}, call_site="chat.create"
        )


def test_strip_removes_definitions_in_both_spellings() -> None:
    """``tools`` is spelled alike; a camelCase definitions path must go whole."""
    descriptor = ProviderDescriptor.from_dict(
        {
            "name": "x",
            "client_module": "x",
            "tools_param": ["config", "function_declarations"],
            "call_sites": [{"id": "chat.create", "path": ["chat", "create"]}],
        }
    )
    config = {"function_declarations": _TOOLS, "functionDeclarations": _TOOLS, "t": 1}

    out = ProviderAdapter(descriptor).apply_constraints(
        {"config": config}, {"tools": "strip"}, call_site="chat.create"
    )

    assert out["config"] == {"t": 1}
    assert config["functionDeclarations"] == _TOOLS, "the caller's dict is untouched"


# ── End to end through consent.wrap() ───────────────────────────────────────


def test_a_refused_call_never_reaches_the_provider() -> None:
    completions = _Recording()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent(_decision({"tools": "refuse"})),
    )

    with pytest.raises(ToolUseRefusedError):
        wrapped.chat.completions.create(
            model="gpt-4o", messages=[], tools=_TOOLS, user_id="usr_marie_456"
        )

    assert completions.calls == []


def test_a_stripped_call_reaches_the_provider_without_its_tools() -> None:
    completions = _Recording()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    wrapped = consent.wrap(
        client,
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_FakeConsent(_decision({"tools": "strip"})),
    )

    wrapped.chat.completions.create(
        model="gpt-4o", messages=[], tools=_TOOLS, user_id="usr_marie_456"
    )

    call = completions.calls[-1]
    assert "tools" not in call
    assert call["extra_headers"]["X-AI-Consent-Token"] == "tok_1"


def test_a_custom_adapter_written_without_call_site_still_works() -> None:
    """`apply_constraints(kwargs, constraints)` is the documented override.

    Passing it `call_site=` raised TypeError after the cloud had authorized
    the call: a proof in the ledger, and no call.
    """

    class _TwoArgumentAdapter(ProviderAdapter):
        def apply_constraints(  # type: ignore[override]
            self, kwargs: dict[str, Any], constraints: dict[str, Any]
        ) -> dict[str, Any]:
            return super().apply_constraints(kwargs, constraints)

    completions = _Recording()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    wrapped = consent.wrap(
        client,
        adapter=_TwoArgumentAdapter(_OPENAI),
        consent_client=_FakeConsent(_decision({"tools": "strip"})),
    )

    wrapped.chat.completions.create(
        model="gpt-4o", messages=[], tools=_TOOLS, user_id="usr_marie_456"
    )

    assert "tools" not in completions.calls[-1]
    assert consent._takes_call_site(ProviderAdapter(_OPENAI).apply_constraints)
    assert consent._takes_call_site(lambda *args, **kwargs: None)
    assert not consent._takes_call_site(print)  # no signature to read


def test_the_refusal_still_names_the_user_to_the_cloud_first() -> None:
    """The proof is minted and journalled before the SDK refuses locally.

    That is on purpose: the ledger entry records the constraint that was in
    force for this user, which is exactly what an auditor needs. It records no
    claim about whether the call carried tools, because we were never told.
    """
    client = SimpleNamespace(chat=SimpleNamespace(completions=_Recording()))
    consent_client = _FakeConsent(_decision({"tools": "refuse"}))
    wrapped = consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=consent_client
    )

    with pytest.raises(ToolUseRefusedError):
        wrapped.chat.completions.create(
            model="gpt-4o", messages=[], tools=_TOOLS, user_id="usr_marie_456"
        )

    assert len(consent_client.calls) == 1
    assert "tools" not in consent_client.calls[0]
    assert "has_tools" not in consent_client.calls[0]


def test_the_request_hash_still_covers_the_tools_the_developer_passed() -> None:
    """The proof binds the request as it was written, not as it was allowed.

    Otherwise a refused call and an accepted one could share a hash, and the
    binding would no longer say which request was authorised.
    """
    adapter = ProviderAdapter(_OPENAI)

    without = adapter.extract_request_metadata(model="gpt-4o", messages=[])
    with_tools = adapter.extract_request_metadata(
        model="gpt-4o", messages=[], tools=_TOOLS
    )

    assert without["request_hash"] != with_tools["request_hash"]
