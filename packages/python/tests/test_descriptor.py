"""Tests for provider descriptors and their JSON auto-loading."""

from __future__ import annotations

from idside.providers import ProviderDescriptor, load_descriptors


def _site(path: list[str], forceable: list[str] | None = None) -> dict[str, object]:
    """A call site in its JSON form; the id is the dotted Python path."""
    return {
        "id": ".".join(path),
        "path": path,
        "forceable_params": forceable or [],
    }


def _ids(descriptor: ProviderDescriptor) -> set[str]:
    return {site.id for site in descriptor.call_sites}


def test_from_dict_applies_defaults() -> None:
    d = ProviderDescriptor.from_dict(
        {"name": "x", "client_module": "x", "call_sites": [_site(["a", "b"])]}
    )
    assert d.name == "x"
    assert d.client_module == "x"
    assert d.call_sites[0].id == "a.b"
    assert d.call_sites[0].path == ("a", "b")
    assert d.call_sites[0].forceable_params == frozenset()
    assert d.call_sites[0].resuming_params == ()
    assert d.identity_param == "user_id"
    assert d.ubt_param == "ubt"
    assert d.token_header == "X-AI-Consent-Token"
    assert d.param_target == "kwargs"
    assert d.headers_kwarg == "extra_headers"
    assert d.protected_params == frozenset()


def test_from_dict_reads_all_fields() -> None:
    d = ProviderDescriptor.from_dict(
        {
            "name": "y",
            "client_module": "ymod",
            "call_sites": [_site(["messages", "create"], ["inference_geo"])],
            "identity_param": "uid",
            "token_header": "X-H",
            "param_target": "headers",
            "protected_params": ["model", "messages"],
        }
    )
    assert d.client_module == "ymod"
    assert d.identity_param == "uid"
    assert d.token_header == "X-H"
    assert d.param_target == "headers"
    assert d.protected_params == frozenset({"model", "messages"})
    assert d.call_sites[0].forceable_params == frozenset({"inference_geo"})


def test_a_call_site_reads_the_arguments_that_mean_it_resumes() -> None:
    """An overloaded method's second form is descriptor data, not SDK knowledge.

    Absent means "this endpoint has one form", which is true of every site but
    OpenAI's `responses.create` — so the default has to be empty, not clever.
    """
    site = _site(["responses", "create"])
    site["resuming_params"] = ["response_id", "starting_after"]
    d = ProviderDescriptor.from_dict(
        {"name": "z", "client_module": "z", "call_sites": [site]}
    )
    assert d.call_sites[0].resuming_params == ("response_id", "starting_after")


def test_load_descriptors_discovers_bundled_providers() -> None:
    descriptors = load_descriptors()
    assert "openai" in descriptors
    assert "anthropic" in descriptors
    assert "gemini" in descriptors
    assert "mistral" in descriptors
    assert "deepseek" in descriptors


def test_bundled_openai_descriptor_shape() -> None:
    openai = load_descriptors()["openai"]
    assert openai.client_module == "openai"
    assert _ids(openai) == {"chat.completions.create", "responses.create"}
    assert openai.param_target == "kwargs"
    assert "model" in openai.protected_params


def test_bundled_anthropic_injects_via_kwargs() -> None:
    """`inference_geo` is a BODY parameter, so kwargs is the only target.

    It was `headers` until 2026-07-26, on the reasoning that Anthropic
    tolerates unknown headers and refuses unknown body fields. True, and
    beside the point: the one thing worth forcing here is not an unknown
    field, it is a named parameter of `messages.create`, and sent as a header
    it left the process and changed nothing. The ACT token still travels in a
    header — that is `token_header`, a separate mechanism.
    """
    anthropic = load_descriptors()["anthropic"]
    assert anthropic.client_module == "anthropic"
    assert _ids(anthropic) == {"messages.create"}
    assert anthropic.param_target == "kwargs"


def test_bundled_gemini_descriptor_shape() -> None:
    gemini = load_descriptors()["gemini"]
    assert gemini.client_module == "google.genai"
    assert _ids(gemini) == {
        "models.generate_content",
        "models.generate_content_stream",
    }
    assert "contents" in gemini.protected_params
    assert "config" in gemini.protected_params


def test_bundled_mistral_descriptor_shape() -> None:
    mistral = load_descriptors()["mistral"]
    assert mistral.client_module == "mistralai"
    assert {"chat.complete", "chat.stream", "fim.complete", "agents.complete"} <= _ids(
        mistral
    )
    assert mistral.headers_kwarg == "http_headers"
    assert "messages" in mistral.protected_params
