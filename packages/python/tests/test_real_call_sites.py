"""Pins each descriptor's call sites against the REAL provider client library.

A descriptor that names a method the real client does not expose would leave
``consent.wrap()`` with nothing to enforce — these tests fail loudly instead.
They also pin the per-request header mechanics the adapters rely on:

- ``mistralai``: every declared call site accepts ``http_headers``;
- ``google-genai``: ``generate_content`` accepts per-request headers only via
  ``config.http_options.headers`` (no free-form kwargs), which is why the SDK
  ships a dedicated Gemini adapter.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from idside.providers import load_descriptors


def _resolve(client: Any, path: tuple[str, ...]) -> Any:
    target = client
    for attr in path:
        assert hasattr(target, attr), f"real client lacks {'.'.join(path)}"
        target = getattr(target, attr)
    return target


def _real_client(provider: str) -> Any:
    if provider == "openai":
        openai = pytest.importorskip("openai")
        return openai.OpenAI(api_key="sk-test")
    if provider == "deepseek":
        # OpenAI-compatible: the same client, pointed elsewhere.
        openai = pytest.importorskip("openai")
        return openai.OpenAI(api_key="sk-test", base_url="https://api.deepseek.com")
    if provider == "anthropic":
        anthropic = pytest.importorskip("anthropic")
        return anthropic.Anthropic(api_key="sk-test")
    if provider == "gemini":
        genai = pytest.importorskip("google.genai")
        return genai.Client(api_key="test")
    mistral = pytest.importorskip("mistralai.client")
    return mistral.Mistral(api_key="test")


@pytest.mark.parametrize(
    "provider", ["openai", "anthropic", "gemini", "mistral", "deepseek"]
)
def test_every_declared_call_site_exists_on_the_real_client(provider: str) -> None:
    descriptor = load_descriptors()[provider]
    client = _real_client(provider)
    for site in descriptor.call_sites:
        method = _resolve(client, site.path)
        assert callable(method)


@pytest.mark.parametrize(
    "provider", ["openai", "anthropic", "gemini", "mistral", "deepseek"]
)
def test_call_site_ids_are_unique_and_match_their_path(provider: str) -> None:
    """The id is what the cloud decides on; two endpoints must not share one.

    It is also written in the Python client's spelling, which is what makes it
    the *same* name in the Node SDK, where the path may differ
    (``generateContent`` vs ``generate_content``).
    """
    descriptor = load_descriptors()[provider]
    ids = [site.id for site in descriptor.call_sites]

    assert len(ids) == len(set(ids)), f"{provider} declares a duplicate call-site id"
    for site in descriptor.call_sites:
        assert site.id == ".".join(site.path), (
            f"{provider}: call-site id '{site.id}' does not match its Python path "
            f"'{'.'.join(site.path)}'. The id IS the dotted Python path — that is "
            "what makes one endpoint one name across SDKs."
        )


def test_mistral_call_sites_accept_per_request_http_headers() -> None:
    descriptor = load_descriptors()["mistral"]
    client = _real_client("mistral")
    for site in descriptor.call_sites:
        method = _resolve(client, site.path)
        params = inspect.signature(method).parameters
        assert "http_headers" in params, (
            f"mistralai {site.id} lost its http_headers kwarg; "
            "the mistral descriptor's headers_kwarg no longer holds"
        )


def test_gemini_call_sites_take_no_free_form_kwargs() -> None:
    # generate_content accepts exactly (model, contents, config): the ACT token
    # can only travel inside config.http_options.headers, and forced params
    # inside config — the premise of the dedicated Gemini adapter.
    descriptor = load_descriptors()["gemini"]
    client = _real_client("gemini")
    for site in descriptor.call_sites:
        method = _resolve(client, site.path)
        params = inspect.signature(method).parameters
        assert set(params) == {"model", "contents", "config"}
        assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def test_gemini_config_accepts_http_options_headers_as_dict() -> None:
    types = pytest.importorskip("google.genai.types")
    config = types.GenerateContentConfig.model_validate(
        {"http_options": {"headers": {"X-AI-Consent-Token": "tok"}}}
    )
    assert config.http_options is not None
    assert config.http_options.headers == {"X-AI-Consent-Token": "tok"}


def test_gemini_adapter_handles_a_real_generate_content_config() -> None:
    # A caller may pass config as a GenerateContentConfig object; the adapter
    # must normalize it, keep its fields, and never mutate the caller's object.
    types = pytest.importorskip("google.genai.types")
    from idside.adapters import GeminiAdapter
    from idside.providers import load_descriptors

    original = types.GenerateContentConfig(
        temperature=0.5, http_options=types.HttpOptions(headers={"X-Trace": "1"})
    )
    adapter = GeminiAdapter(load_descriptors()["gemini"])
    out = adapter.apply(
        {"model": "gemini-2.0-flash", "contents": "hi", "config": original},
        {"seed": 7},
        "tok_1",
    )
    applied = out["config"]
    assert applied["temperature"] == 0.5
    assert applied["seed"] == 7
    assert applied["http_options"]["headers"]["X-Trace"] == "1"
    assert applied["http_options"]["headers"]["X-AI-Consent-Token"] == "tok_1"
    # the resulting dict form must still be a valid config for google-genai
    types.GenerateContentConfig.model_validate(applied)
    # caller's object untouched
    assert original.http_options.headers == {"X-Trace": "1"}
    assert original.temperature == 0.5
