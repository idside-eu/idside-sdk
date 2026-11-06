"""Which provider is this client actually talking to? (P1.4)

Detection used to be a one-liner on the client's module, and that was right for
as long as one client library meant one provider. It stopped being right the
moment an OpenAI-compatible provider arrived: DeepSeek is reached through the
`openai` package pointed at a different base URL, so an `OpenAI` instance is an
`OpenAI` instance whoever receives the request.

The consequence is not cosmetic. The detected name is what the SDK sends to the
cloud, and the cloud picks registry rules by it — so answering "openai" for a
DeepSeek call would apply OpenAI's parameters to a server that never agreed to
them, which is the Gemini `store` bug with a different provider on the receiving
end.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from idside.adapters import get_adapter_for
from idside.exceptions import UnsupportedProviderError

_ALL = ["openai", "anthropic", "gemini", "mistral", "deepseek"]


def _client(provider: str) -> Any:
    """A REAL client instance for each provider, built the way a caller would."""
    if provider == "openai":
        return pytest.importorskip("openai").OpenAI(api_key="sk-test")
    if provider == "deepseek":
        # The documented way to call DeepSeek: OpenAI's client, their base URL.
        return pytest.importorskip("openai").OpenAI(
            api_key="sk-test", base_url="https://api.deepseek.com"
        )
    if provider == "anthropic":
        return pytest.importorskip("anthropic").Anthropic(api_key="sk-test")
    if provider == "gemini":
        return pytest.importorskip("google.genai").Client(api_key="test")
    return pytest.importorskip("mistralai.client").Mistral(api_key="test")


@pytest.mark.parametrize("provider", _ALL)
def test_every_supported_client_is_recognised_without_configuration(
    provider: str,
) -> None:
    """The promise the SDK makes: wrap the client, name nothing."""
    assert get_adapter_for(_client(provider)).name == provider


def test_an_openai_compatible_provider_is_not_mistaken_for_openai() -> None:
    """The distinction the whole card exists for.

    Same class, same module, same call sites — different server, different
    terms, different registry rules.
    """
    assert get_adapter_for(_client("deepseek")).name == "deepseek"
    assert get_adapter_for(_client("openai")).name == "openai"


def test_the_host_decides_even_when_the_url_is_written_differently() -> None:
    openai = pytest.importorskip("openai")

    for url in (
        "https://api.deepseek.com",
        "https://api.deepseek.com/",
        "https://api.deepseek.com/v1",
        "https://API.DeepSeek.com/v1/",
    ):
        client = openai.OpenAI(api_key="sk-test", base_url=url)
        assert get_adapter_for(client).name == "deepseek", url


def test_an_unknown_base_url_falls_back_to_the_module_s_default() -> None:
    """A proxy, a gateway, or a compatible provider we have never heard of.

    Falling back to `openai` is right: the mechanics are the OpenAI client's,
    which is all the descriptor governs. What the cloud then enforces is a
    policy question, and the ledger records which provider was claimed.
    """
    openai = pytest.importorskip("openai")
    client = openai.OpenAI(api_key="sk-test", base_url="https://llm.internal.example")

    assert get_adapter_for(client).name == "openai"


def test_an_unknown_client_says_so_instead_of_guessing() -> None:
    with pytest.raises(UnsupportedProviderError) as caught:
        get_adapter_for(SimpleNamespace())

    assert "No ID Side adapter" in str(caught.value)


def test_detection_survives_a_client_with_no_base_url_at_all() -> None:
    """Most clients do not have one; reading it must never be load-bearing."""
    anthropic = pytest.importorskip("anthropic")
    client = anthropic.Anthropic(api_key="sk-test")

    assert get_adapter_for(client).name == "anthropic"


class _OpenAICompatible:
    """Something from the ``openai`` module that is not ``openai.OpenAI``.

    Wrappers, proxies and subclasses exist: an observability shim, a corporate
    gateway's thin client, a release older than the attribute being read. The
    module still names two descriptors, so the base URL is still consulted —
    and consulting it must never be what decides whether a client can be
    wrapped at all.
    """

    __module__ = "openai"

    def __init__(self, base_url: Any = None) -> None:
        if base_url is not None:
            self.base_url = base_url


def test_a_client_that_does_not_say_where_it_points_falls_back_to_the_default() -> None:
    """No base URL is not evidence of anything; it is the absence of evidence.

    Refusing here would make the SDK unwrappable for a client shape it fully
    supports, and guessing `deepseek` would send the cloud a provider name the
    request never justified.
    """
    assert get_adapter_for(_OpenAICompatible()).name == "openai"


def test_a_base_url_that_is_not_a_url_narrows_nothing_and_raises_nothing() -> None:
    """Detection only ever NARROWS a choice, so a bad value must not be fatal.

    `urlsplit` raises on some malformed inputs rather than returning an empty
    host, and that exception would come out of `consent.wrap()` from a line
    that has nothing to do with what the developer got wrong.
    """
    assert get_adapter_for(_OpenAICompatible("http://[oops")).name == "openai"
    assert get_adapter_for(_OpenAICompatible(object())).name == "openai"


def test_the_same_stand_in_is_still_told_apart_by_a_readable_host() -> None:
    """The pair to the two above: a host that reads is still what decides."""
    assert get_adapter_for(_OpenAICompatible("https://api.deepseek.com/v1")).name == (
        "deepseek"
    )
