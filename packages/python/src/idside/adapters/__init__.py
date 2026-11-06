# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Provider adapters: the descriptor-driven engine + auto-detection registry.

Standard providers are described entirely by a JSON descriptor (see
:mod:`idside.providers`). Exotic providers can register a ``ProviderAdapter``
subclass in ``_OVERRIDES`` to customize the mechanics.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from idside.adapters.base import ProviderAdapter
from idside.adapters.gemini import GeminiAdapter
from idside.exceptions import UnsupportedProviderError
from idside.providers import ProviderDescriptor, load_descriptors

__all__ = ["GeminiAdapter", "ProviderAdapter", "get_adapter_for", "register_adapter"]

#: Per-provider engine overrides for exotic APIs (default: the generic engine).
#: google-genai takes no free-form kwargs, so Gemini ships a built-in override.
_OVERRIDES: dict[str, type[ProviderAdapter]] = {"gemini": GeminiAdapter}

#: Maps a client's module (or module prefix, e.g. "google.genai") to the
#: descriptors that use it. A LIST, not one descriptor: OpenAI-compatible
#: providers (DeepSeek, and Groq/xAI/Together when they get descriptors) are all
#: reached through the `openai` client, so a module can legitimately name
#: several. Prefixes let namespace packages match without capturing the whole
#: namespace ("google.genai" matches google.genai.client, not google.*).
_BY_MODULE: dict[str, list[ProviderDescriptor]] = {}
for _descriptor in load_descriptors().values():
    _BY_MODULE.setdefault(_descriptor.client_module, []).append(_descriptor)


def _base_url_host(client: Any) -> str | None:
    """The host a client is pointed at, or None if it does not say.

    ``openai.OpenAI`` exposes ``base_url`` as an ``httpx.URL``; other clients
    expose a string, or nothing at all. Everything here is best-effort and
    failure-tolerant, because this only ever *narrows* a choice.
    """
    raw = getattr(client, "base_url", None)
    if raw is None:
        return None
    try:
        host = urlsplit(str(raw)).hostname
    except ValueError:  # a malformed URL is simply not a match
        return None
    return host.lower() if host else None


def _resolve(client: Any) -> ProviderDescriptor | None:
    """Pick the descriptor for ``client``: by module, then by base URL.

    The second step is not a nicety. An `OpenAI` instance pointed at
    api.deepseek.com is an `OpenAI` instance, so the class name says nothing
    about who receives the request — and the provider name is what tells the
    cloud which registry rules to apply. Answering "openai" for a DeepSeek call
    would apply OpenAI's parameters to a server that never agreed to them.
    """
    parts = type(client).__module__.split(".")
    candidates = next(
        (
            found
            for end in range(len(parts), 0, -1)
            if (found := _BY_MODULE.get(".".join(parts[:end]))) is not None
        ),
        None,
    )
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    host = _base_url_host(client)
    if host is not None:
        for descriptor in candidates:
            if host in descriptor.client_base_url_hosts:
                return descriptor

    # No host matched: fall back to the descriptor that claims no host in
    # particular — the module's default (openai itself). If several do, the
    # descriptors are ambiguous and guessing would be worse than refusing.
    defaults = [d for d in candidates if not d.client_base_url_hosts]
    return defaults[0] if len(defaults) == 1 else None


def get_adapter_for(client: Any) -> ProviderAdapter:
    """Return the adapter matching ``client``'s provider, or raise."""
    descriptor = _resolve(client)
    if descriptor is None:
        raise UnsupportedProviderError(
            f"No ID Side adapter for client of type "
            f"{type(client).__module__}.{type(client).__qualname__}."
        )
    adapter_cls = _OVERRIDES.get(descriptor.name, ProviderAdapter)
    return adapter_cls(descriptor)


def register_adapter(name: str, adapter_cls: type[ProviderAdapter]) -> None:
    """Register a ``ProviderAdapter`` subclass to override the generic engine.

    Use this for exotic providers whose API does not fit the declarative model
    (e.g. SDKs without per-request header support). ``name`` must match a
    descriptor's ``name``.
    """
    _OVERRIDES[name] = adapter_cls
