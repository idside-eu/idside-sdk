"""Tests for provider auto-detection."""

from __future__ import annotations

import anthropic
import openai
import pytest

from idside.adapters import ProviderAdapter, get_adapter_for
from idside.exceptions import UnsupportedProviderError


def test_detects_openai_client() -> None:
    adapter = get_adapter_for(openai.OpenAI(api_key="sk-test"))
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.name == "openai"


def test_detects_anthropic_client() -> None:
    adapter = get_adapter_for(anthropic.Anthropic(api_key="sk-test"))
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.name == "anthropic"


def test_unknown_client_raises() -> None:
    with pytest.raises(UnsupportedProviderError):
        get_adapter_for(object())
