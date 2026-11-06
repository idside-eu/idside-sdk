"""The trip-wire: every option a provider library accepts has been looked at.

``extra_body`` was not found by reading a list. It was found by trying it, after
the SDK had shipped a promise it quietly broke: an option passed on one call
rewrote the body after enforcement, and the proof described a request that
never left the process. The options a call may carry are now a closed list in
``idside.adapters._call_options``, and a closed list is only as good as the day
it was last compared with the libraries.

So this file does the comparison, mechanically and offline, against the client
libraries this package is tested with. The option surface of an endpoint is
whatever its signature accepts that its body schema does not declare:

- Stainless (``openai``, ``anthropic``): the method's parameters minus the keys
  of the body ``TypedDict``, with ``parse`` and ``stream`` included — their helper
  arguments are options too, and are classified like any other;
- Speakeasy (``mistralai``): the parameters minus the request model's fields;
- ``google-genai``: the ``GenerateContentConfig`` fields that neither request
  converter reads, and every field of ``HttpOptions``.

Each surface is pinned exactly, and each name in it must be classified. A
library upgrade that adds an option fails here until someone decides what it
does — which is the whole point: the runtime refuses an unclassified option
anyway, and this is where the refusal is explained before a customer meets it.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Any

import pytest

from idside.adapters import _call_options
from idside.adapters._call_options import (
    GENAI_CONFIG_OPTIONS,
    GENAI_HTTP_PERMITTED,
    GENAI_HTTP_REFUSED,
    LIBRARIES,
    header_names,
    mapping_view,
    refuse_unenforceable,
)
from idside.exceptions import UnenforceableOptionError
from idside.providers import ProviderDescriptor, load_descriptors

openai = pytest.importorskip("openai")
anthropic = pytest.importorskip("anthropic")
genai_models = pytest.importorskip("google.genai.models")
genai_types = pytest.importorskip("google.genai.types")
mistral_client = pytest.importorskip("mistralai.client")

_DESCRIPTORS = load_descriptors()

_STAINLESS = frozenset({"extra_body", "extra_headers", "extra_query", "timeout"})
_SPEAKEASY = frozenset({"http_headers", "retries", "server_url", "timeout_ms"})

#: (provider, call site, method) → the options that endpoint accepts, as the
#: pinned libraries compute them. Change a line here only together with the
#: classification in `_call_options.py`.
_EXPECTED: dict[tuple[str, str, str], frozenset[str]] = {
    ("openai", "chat.completions.create", "create"): _STAINLESS,
    ("openai", "chat.completions.create", "parse"): _STAINLESS,
    ("openai", "chat.completions.create", "stream"): _STAINLESS,
    ("openai", "responses.create", "create"): _STAINLESS,
    ("openai", "responses.create", "parse"): _STAINLESS | {"text_format", "verbosity"},
    ("openai", "responses.create", "stream"): _STAINLESS
    | {"response_id", "starting_after", "text_format"},
    ("deepseek", "chat.completions.create", "create"): _STAINLESS,
    ("deepseek", "chat.completions.create", "parse"): _STAINLESS,
    ("deepseek", "chat.completions.create", "stream"): _STAINLESS,
    ("anthropic", "messages.create", "create"): _STAINLESS,
    ("anthropic", "messages.create", "parse"): _STAINLESS | {"output_format"},
    ("anthropic", "messages.create", "stream"): _STAINLESS | {"output_format"},
    ("mistral", "chat.complete", "complete"): _SPEAKEASY,
    ("mistral", "chat.stream", "stream"): _SPEAKEASY,
    ("mistral", "fim.complete", "complete"): _SPEAKEASY,
    ("mistral", "fim.stream", "stream"): _SPEAKEASY,
    ("mistral", "agents.complete", "complete"): _SPEAKEASY,
    ("mistral", "agents.stream", "stream"): _SPEAKEASY,
    ("mistral", "beta.conversations.start", "start"): _SPEAKEASY,
}


def _clients(provider: str) -> list[Any]:
    """Every client shape a provider is wrapped as: blocking and async."""
    if provider == "openai":
        return [openai.OpenAI(api_key="sk-test"), openai.AsyncOpenAI(api_key="sk-test")]
    if provider == "deepseek":
        return [
            openai.OpenAI(api_key="sk-test", base_url="https://api.deepseek.com"),
            openai.AsyncOpenAI(api_key="sk-test", base_url="https://api.deepseek.com"),
        ]
    if provider == "anthropic":
        return [
            anthropic.Anthropic(api_key="sk-test"),
            anthropic.AsyncAnthropic(api_key="sk-test"),
        ]
    return [mistral_client.Mistral(api_key="test")]


def _surface(method: Callable[..., Any], body: frozenset[str]) -> frozenset[str]:
    """What a method accepts that is not its body: its option surface."""
    parameters = inspect.signature(method).parameters
    return frozenset(parameters) - body - {"self", "stream"}


def _unclassified(surface: frozenset[str], module: str) -> frozenset[str]:
    library = LIBRARIES[module]
    return surface - library.permitted - library.refused


def _resolve(client: Any, site_path: tuple[str, ...], method: str) -> Any:
    target = client
    for attr in site_path[:-1]:
        target = getattr(target, attr)
    return getattr(target, method)


def _computed() -> dict[tuple[str, str, str], frozenset[str]]:
    computed: dict[tuple[str, str, str], frozenset[str]] = {}
    for provider in ("openai", "deepseek", "anthropic", "mistral"):
        descriptor = _DESCRIPTORS[provider]
        library = LIBRARIES[descriptor.client_module]
        for site in descriptor.call_sites:
            body = _call_options._body_fields(library.body_schemas[site.id])
            assert body is not None, f"{provider}:{site.id} has no readable body schema"
            for client in _clients(provider):
                for method in (site.path[-1], *site.aliases):
                    try:
                        found = _resolve(client, site.path, method)
                    except AttributeError:
                        continue  # an alias from another client language
                    key = (provider, site.id, method)
                    surface = _surface(found, body)
                    assert computed.setdefault(key, surface) == surface, (
                        f"{key}: the blocking and async clients disagree"
                    )
    return computed


def test_every_option_surface_is_exactly_the_pinned_one() -> None:
    assert _computed() == _EXPECTED


@pytest.mark.parametrize("key", sorted(_EXPECTED), ids=":".join)
def test_every_option_is_classified(key: tuple[str, str, str]) -> None:
    module = _DESCRIPTORS[key[0]].client_module
    unclassified = _unclassified(_computed()[key], module)

    assert not unclassified, (
        f"{':'.join(key)} accepts {sorted(unclassified)}, which _call_options.py "
        "does not classify. Decide whether each one can change what the request "
        "says or where it goes; the runtime refuses it until then."
    )


@pytest.mark.parametrize("module", sorted(LIBRARIES))
def test_the_frozen_body_is_the_one_the_installed_library_declares(module: str) -> None:
    """The snapshot is only worth having while it still matches the live schema.

    ``body_fields`` is what a refusal falls back on the day a library moves its
    schema out of reach, so a stale snapshot would let through the very options
    the move was supposed to leave refused. It is generated from the pinned
    library, and this is where it is compared with one again.
    """
    library = LIBRARIES[module]

    assert sorted(library.body_fields) == sorted(library.body_schemas)
    for site, candidates in library.body_schemas.items():
        assert _call_options._body_fields(candidates) == library.body_fields[site], (
            f"{module}:{site} declares a body the frozen snapshot no longer "
            "matches. Regenerate _call_options.LIBRARIES[...].body_fields from "
            "the installed library."
        )


def test_an_unreadable_schema_cannot_widen_what_a_call_may_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The day ``openai 7`` moves its schema, nothing unclassified starts passing.

    This is the shape of the defect the snapshot closes: a future library
    version is exactly what makes the live schema unreadable, and the trip-wire
    above only ever sees the pinned version. Falling back on the open list
    reopened the closed one on every call, in the one situation invariant #8
    names.
    """
    monkeypatch.setattr(_call_options, "_body_fields", lambda _candidates: None)

    with pytest.raises(UnenforceableOptionError) as caught:
        refuse_unenforceable(
            _DESCRIPTORS["openai"], "chat.completions.create", {"extra_transport": 1}
        )
    assert "extra_transport" in str(caught.value)

    # And the other half: what the SDK was built against still goes out, so a
    # library that merely moved its schema does not brick every enforced call.
    refuse_unenforceable(
        _DESCRIPTORS["openai"],
        "chat.completions.create",
        {"model": "gpt-4o-mini", "messages": [], "store": False, "timeout": 5},
    )


def test_an_endpoint_with_no_snapshot_falls_back_on_the_library_not_on_the_open_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call site nobody classified is still judged against a closed list.

    The narrowest honest answer for an endpoint this SDK has no schema for is
    everything the library's other endpoints accept: an option a future version
    adds is in none of them, so it is refused, while ordinary parameters pass.
    """
    descriptor = _descriptor("openai", "beta.chat.create")
    monkeypatch.setattr(_call_options, "_body_fields", lambda _candidates: None)

    refuse_unenforceable(descriptor, "beta.chat.create", {"temperature": 1})
    with pytest.raises(UnenforceableOptionError):
        refuse_unenforceable(descriptor, "beta.chat.create", {"extra_transport": 1})


def test_the_trip_wire_catches_an_option_a_library_adds() -> None:
    """A test never seen failing does not count — so this one is made to."""

    def create(
        *,
        model: str,
        messages: list[Any],
        extra_headers: Any = None,
        extra_query: Any = None,
        extra_body: Any = None,
        timeout: Any = None,
        extra_transport: Any = None,
    ) -> None: ...

    surface = _surface(create, frozenset({"model", "messages"}))

    assert _unclassified(surface, "openai") == {"extra_transport"}


@pytest.mark.parametrize("module", sorted(LIBRARIES))
def test_no_classified_option_is_left_over(module: str) -> None:
    """A classification naming an option no library has is a stale one."""
    library = LIBRARIES[module]
    seen = frozenset().union(
        *(
            surface
            for key, surface in _EXPECTED.items()
            if _DESCRIPTORS[key[0]].client_module == module
        )
    )

    assert library.permitted | library.refused == seen
    assert not library.permitted & library.refused
    assert library.transport <= library.permitted


# ── google-genai: the options live inside `config` ──────────────────────────


def _read_by_converters() -> frozenset[str]:
    read: set[str] = set()
    for converter in (
        genai_models._GenerateContentConfig_to_mldev,
        genai_models._GenerateContentConfig_to_vertex,
    ):
        read |= set(
            re.findall(r"getv\(from_object, \['(\w+)'\]", inspect.getsource(converter))
        )
    return frozenset(read)


def test_gemini_config_options_are_exactly_the_fields_no_converter_reads() -> None:
    fields = frozenset(genai_types.GenerateContentConfig.model_fields)

    assert fields - _read_by_converters() == GENAI_CONFIG_OPTIONS


def test_every_gemini_http_option_is_classified() -> None:
    fields = frozenset(genai_types.HttpOptions.model_fields)

    assert fields == GENAI_HTTP_PERMITTED | GENAI_HTTP_REFUSED
    assert not GENAI_HTTP_PERMITTED & GENAI_HTTP_REFUSED


# ── the classification is what the runtime does ─────────────────────────────


def _site_of(module: str) -> tuple[ProviderDescriptor, str]:
    descriptor = next(d for d in _DESCRIPTORS.values() if d.client_module == module)
    return descriptor, descriptor.call_sites[0].id


def _harmless_value(name: str) -> Any:
    return {} if "header" in name else 1


_PERMITTED = sorted(
    (module, name) for module, lib in LIBRARIES.items() for name in lib.permitted
)
_REFUSED = sorted(
    (module, name) for module, lib in LIBRARIES.items() for name in lib.refused
)


@pytest.mark.parametrize(("module", "name"), _PERMITTED)
def test_a_permitted_option_is_let_through(module: str, name: str) -> None:
    descriptor, site = _site_of(module)

    refuse_unenforceable(descriptor, site, {name: _harmless_value(name)})


@pytest.mark.parametrize(("module", "name"), _REFUSED)
def test_a_refused_option_is_refused(module: str, name: str) -> None:
    descriptor, site = _site_of(module)

    with pytest.raises(UnenforceableOptionError) as caught:
        refuse_unenforceable(descriptor, site, {name: {"anything": "secret-value"}})

    assert name in str(caught.value)
    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize("name", sorted(GENAI_HTTP_PERMITTED))
def test_a_permitted_gemini_http_option_is_let_through(name: str) -> None:
    refuse_unenforceable(
        _DESCRIPTORS["gemini"],
        "models.generate_content",
        {"config": {"http_options": {name: _harmless_value(name)}}},
    )


@pytest.mark.parametrize("name", sorted(GENAI_HTTP_REFUSED))
def test_a_refused_gemini_http_option_is_refused(name: str) -> None:
    with pytest.raises(UnenforceableOptionError) as caught:
        refuse_unenforceable(
            _DESCRIPTORS["gemini"],
            "models.generate_content",
            {"config": {"http_options": {name: "secret-value"}}},
        )

    assert f"config.http_options.{name}" in str(caught.value)
    assert "secret-value" not in str(caught.value)


def test_an_option_newer_than_this_sdk_is_refused_at_run_time() -> None:
    """The runtime is a closed list too: not a body field, not permitted, refused."""
    with pytest.raises(UnenforceableOptionError) as caught:
        refuse_unenforceable(
            _DESCRIPTORS["openai"], "chat.completions.create", {"extra_transport": 1}
        )

    assert "extra_transport" in str(caught.value)


def test_a_body_parameter_newer_than_this_sdk_still_passes() -> None:
    """New provider fields are the cloud's business, not an SDK release's."""
    refuse_unenforceable(
        _DESCRIPTORS["openai"],
        "chat.completions.create",
        {"model": "gpt-4o-mini", "messages": [], "store": False, "stream": True},
    )


@pytest.mark.parametrize("value", [None, openai.NOT_GIVEN, openai.omit])
def test_an_option_that_asks_for_nothing_is_not_refused(value: Any) -> None:
    refuse_unenforceable(
        _DESCRIPTORS["openai"], "chat.completions.create", {"extra_body": value}
    )


# ── what happens when the library cannot be read ────────────────────────────


def _descriptor(module: str, site: str = "chat.create") -> ProviderDescriptor:
    return ProviderDescriptor.from_dict(
        {
            "name": "custom",
            "client_module": module,
            "call_sites": [{"id": site, "path": site.split(".")}],
        }
    )


def test_a_library_this_sdk_does_not_know_keeps_what_it_cannot_classify() -> None:
    """No body schema, so no closed list: only the known offenders are refused."""
    refuse_unenforceable(_descriptor("exotic_client"), "chat.create", {"anything": 1})
    with pytest.raises(UnenforceableOptionError):
        refuse_unenforceable(
            _descriptor("exotic_client"), "chat.create", {"server_url": "x"}
        )


def test_without_a_schema_for_the_endpoint_the_library_snapshot_is_used() -> None:
    """An endpoint missing from the schema table is judged on the library's."""
    descriptor = _descriptor("openai", "beta.chat.create")

    refuse_unenforceable(descriptor, "beta.chat.create", {"temperature": 1})
    with pytest.raises(UnenforceableOptionError):
        refuse_unenforceable(descriptor, "beta.chat.create", {"extra_body": {}})


def test_a_schema_location_is_tried_candidate_by_candidate() -> None:
    fields = _call_options._body_fields(
        (
            "idside_no_such_module:Anything",
            "openai.types.chat.completion_create_params:NoSuchClass",
            "idside.providers:ProviderDescriptor",
            "openai.types.chat.completion_create_params:CompletionCreateParamsBase",
        )
    )

    assert fields is not None
    assert {"model", "messages", "store"} <= fields


def test_no_readable_schema_is_an_answer_of_its_own() -> None:
    assert _call_options._body_fields(("idside_no_such_module:Anything",)) is None


def test_mistral_schemas_resolve_on_the_installed_version() -> None:
    library = LIBRARIES["mistralai"]

    for candidates in library.body_schemas.values():
        assert _call_options._body_fields(candidates) is not None


@pytest.mark.parametrize(
    "headers",
    [
        [("Authorization",)],
        [("X-Trace", "1", "extra")],
        {1: "one"},
        "Authorization",
        ["Au", "th"],
        [b"Au", b"th"],
        [("X-Trace", "1"), "Au"],
    ],
    ids=[
        "short-pair",
        "long-pair",
        "non-string-name",
        "a-string",
        "two-letter-strings",
        "two-letter-bytes",
        "a-string-among-pairs",
    ],
)
def test_a_header_map_that_is_not_one_cannot_be_read(headers: Any) -> None:
    """A two-letter string has length two, and is still not a (name, value) pair.

    ``["Au", "th"]`` used to read as the headers ``A`` and ``t``: nothing in it
    was reserved, so nothing was refused — and "a map the SDK cannot read is
    refused" held only for maps that happened to have the wrong length.
    """
    assert header_names(headers) is None


@pytest.mark.parametrize(
    "headers",
    [[("X-Trace", "1")], [["X-Trace", "1"]], [(b"X-Trace", b"1")]],
    ids=["tuple-pairs", "list-pairs", "bytes-pairs"],
)
def test_a_sequence_of_pairs_is_still_read(headers: Any) -> None:
    assert header_names(headers) == ["X-Trace"]


def test_a_config_whose_model_dump_is_not_a_mapping_is_not_inspected() -> None:
    class _Impostor:
        def model_dump(self, **_kwargs: Any) -> list[str]:
            return ["http_options"]

    assert mapping_view(_Impostor()) is None
    refuse_unenforceable(
        _DESCRIPTORS["gemini"], "models.generate_content", {"config": _Impostor()}
    )


def test_unreadable_gemini_http_options_are_refused() -> None:
    with pytest.raises(UnenforceableOptionError):
        refuse_unenforceable(
            _DESCRIPTORS["gemini"],
            "models.generate_content",
            {"config": {"http_options": "base_url=https://collector.example"}},
        )


def test_gemini_tools_that_are_not_a_list_hold_no_local_function() -> None:
    refuse_unenforceable(
        _DESCRIPTORS["gemini"],
        "models.generate_content",
        {"config": {"tools": "not-a-list", "http_options": None}},
    )


# ── deriving a client: the same closed list, on with_options() and copy() ───

#: Every client class auto-detection files under a supported library, and the
#: parameters of its deriving methods as the pinned libraries declare them.
_OPENAI_COPY = frozenset(
    {
        "api_key",
        "admin_api_key",
        "workload_identity",
        "organization",
        "project",
        "webhook_secret",
        "websocket_base_url",
        "base_url",
        "timeout",
        "http_client",
        "max_retries",
        "default_headers",
        "set_default_headers",
        "default_query",
        "set_default_query",
        "_enforce_credentials",
        "_extra_kwargs",
    }
)
_ANTHROPIC_TAIL = frozenset(
    {
        "base_url",
        "timeout",
        "http_client",
        "max_retries",
        "default_headers",
        "set_default_headers",
        "default_query",
        "set_default_query",
        "middleware",
        "_extra_kwargs",
    }
)
_DERIVING: dict[tuple[str, str], frozenset[str]] = {
    ("openai", "OpenAI"): _OPENAI_COPY,
    ("openai", "AsyncOpenAI"): _OPENAI_COPY,
    ("openai", "AzureOpenAI"): _OPENAI_COPY
    | {"api_version", "azure_ad_token", "azure_ad_token_provider"},
    ("openai", "AsyncAzureOpenAI"): _OPENAI_COPY
    | {"api_version", "azure_ad_token", "azure_ad_token_provider"},
    ("anthropic", "Anthropic"): _ANTHROPIC_TAIL
    | {"api_key", "auth_token", "credentials", "config", "profile", "webhook_key"},
    ("anthropic", "AsyncAnthropic"): _ANTHROPIC_TAIL
    | {"api_key", "auth_token", "credentials", "config", "profile", "webhook_key"},
    ("anthropic", "AnthropicBedrock"): _ANTHROPIC_TAIL
    | {
        "aws_secret_key",
        "aws_access_key",
        "aws_region",
        "aws_session_token",
        "api_key",
    },
    ("anthropic", "AsyncAnthropicBedrock"): _ANTHROPIC_TAIL
    | {
        "aws_secret_key",
        "aws_access_key",
        "aws_region",
        "aws_session_token",
        "api_key",
    },
    ("anthropic", "AnthropicVertex"): _ANTHROPIC_TAIL
    | {"region", "project_id", "access_token", "credentials"},
    ("anthropic", "AsyncAnthropicVertex"): _ANTHROPIC_TAIL
    | {"region", "project_id", "access_token", "credentials"},
    ("anthropic", "AnthropicFoundry"): _ANTHROPIC_TAIL
    | {"api_key", "azure_ad_token_provider", "webhook_key"},
    ("anthropic", "AsyncAnthropicFoundry"): _ANTHROPIC_TAIL
    | {"api_key", "azure_ad_token_provider", "webhook_key"},
    ("anthropic", "AnthropicAWS"): _ANTHROPIC_TAIL
    | {
        "api_key",
        "aws_access_key",
        "aws_secret_key",
        "aws_region",
        "aws_profile",
        "aws_session_token",
        "workspace_id",
        "skip_auth",
        "auth_token",
        "webhook_key",
    },
}
_DERIVING[("anthropic", "AsyncAnthropicAWS")] = _DERIVING[("anthropic", "AnthropicAWS")]


def _deriving_parameters(module: str, name: str) -> dict[str, frozenset[str]]:
    cls = getattr(pytest.importorskip(module), name)
    return {
        method: frozenset(inspect.signature(getattr(cls, method)).parameters) - {"self"}
        for method in ("copy", "with_options", "with_middleware")
        if hasattr(cls, method)
    }


@pytest.mark.parametrize("key", sorted(_DERIVING), ids=":".join)
def test_every_deriving_signature_is_exactly_the_pinned_one(
    key: tuple[str, str],
) -> None:
    parameters = _deriving_parameters(*key)

    assert parameters["copy"] == _DERIVING[key]
    assert parameters["with_options"] == _DERIVING[key]
    assert parameters.get("with_middleware", frozenset({"middleware"})) == {
        "middleware"
    }


@pytest.mark.parametrize("key", sorted(_DERIVING), ids=":".join)
def test_every_deriving_parameter_is_classified(key: tuple[str, str]) -> None:
    derivation = _call_options.DERIVATIONS[key[0]]
    classified = (
        derivation.harmless
        | derivation.checked
        | derivation.credentials
        | derivation.unsupported
    )
    unclassified = frozenset().union(*_deriving_parameters(*key).values()) - classified

    assert not unclassified, (
        f"{key[1]} can be derived with {sorted(unclassified)}, which "
        "_call_options.DERIVATIONS does not classify: decide whether it touches "
        "the transport, the credential, the URL or the headers."
    )


@pytest.mark.parametrize("module", sorted(_call_options.DERIVATIONS))
def test_the_deriving_classification_is_neither_stale_nor_ambiguous(
    module: str,
) -> None:
    derivation = _call_options.DERIVATIONS[module]
    groups = [
        derivation.harmless,
        derivation.checked,
        derivation.credentials,
        derivation.unsupported,
    ]
    seen = frozenset().union(
        *(surface for key, surface in _DERIVING.items() if key[0] == module)
    )

    assert frozenset().union(*groups) == seen
    assert sum(len(group) for group in groups) == len(seen), "a name in two groups"


def test_a_library_without_a_deriving_classification_gets_the_generic_list() -> None:
    """Not open: timeouts and retries pass, the rest is refused as elsewhere."""
    from idside.exceptions import CredentialRoutingError, UnsupportedProviderError

    exotic = _descriptor("exotic_client")
    exotic = ProviderDescriptor.from_dict(
        {
            "name": "custom",
            "client_module": "exotic_client",
            "reserved_headers": ["Authorization"],
            "call_sites": [{"id": "chat.create", "path": ["chat", "create"]}],
        }
    )
    derive = _call_options.refuse_underivable

    derive(exotic, "with_options", (), {"timeout": 5, "max_retries": 1})
    derive(exotic, "with_options", (), {"base_url": "https://api.exotic.example"})
    derive(exotic, "copy", (), {"default_headers": {"X-Trace": "1"}})
    with pytest.raises(CredentialRoutingError):
        derive(exotic, "copy", (), {"default_headers": {"authorization": "k"}})
    with pytest.raises(CredentialRoutingError):
        derive(exotic, "with_options", (), {"api_key": "sk-test-other"})
    with pytest.raises(UnsupportedProviderError):
        derive(exotic, "with_options", (), {"anything": 1})


def test_positional_derivation_arguments_cannot_be_classified() -> None:
    from idside.exceptions import UnsupportedProviderError

    with pytest.raises(UnsupportedProviderError) as caught:
        _call_options.refuse_underivable(
            _DESCRIPTORS["openai"], "copy", ("sk-test-other",), {}
        )

    assert "*args" in str(caught.value)
    assert "sk-test-other" not in str(caught.value)
