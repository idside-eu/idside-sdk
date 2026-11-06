"""Tests for the exotic-provider override mechanism (the 'code per provider')."""

from __future__ import annotations

from collections.abc import Iterator

import openai
import pytest

from idside import adapters
from idside.adapters import ProviderAdapter, get_adapter_for, register_adapter


@pytest.fixture(autouse=True)
def _restore_overrides() -> Iterator[None]:
    saved = dict(adapters._OVERRIDES)
    yield
    adapters._OVERRIDES.clear()
    adapters._OVERRIDES.update(saved)


def test_default_provider_uses_the_generic_engine() -> None:
    adapter = get_adapter_for(openai.OpenAI(api_key="sk-test"))
    assert type(adapter) is ProviderAdapter


def test_register_adapter_overrides_generic_engine() -> None:
    class CustomOpenAI(ProviderAdapter):
        pass

    register_adapter("openai", CustomOpenAI)
    adapter = get_adapter_for(openai.OpenAI(api_key="sk-test"))
    assert isinstance(adapter, CustomOpenAI)
    assert adapter.name == "openai"  # still descriptor-driven
