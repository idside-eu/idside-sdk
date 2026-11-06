"""Tests for multi-call-site resolution in the adapter engine."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from idside.adapters import ProviderAdapter
from idside.exceptions import UnsupportedProviderError
from idside.providers import ProviderDescriptor


def _desc(
    paths: list[list[str]], forceable: dict[str, list[str]] | None = None
) -> ProviderDescriptor:
    """Build a descriptor from bare paths; the id is the dotted path."""
    forceable = forceable or {}
    return ProviderDescriptor.from_dict(
        {
            "name": "x",
            "client_module": "x",
            "call_sites": [
                {
                    "id": ".".join(path),
                    "path": path,
                    "forceable_params": forceable.get(".".join(path), []),
                }
                for path in paths
            ],
        }
    )


def _noop(**kwargs: Any) -> None:
    return None


def test_picks_first_available_call_site() -> None:
    # Descriptor lists two entry points; the client only exposes the second.
    adapter = ProviderAdapter(
        _desc([["chat", "completions", "create"], ["responses", "create"]])
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=_noop))
    resolved = adapter.get_call_site(client)
    assert resolved.method == "create"
    assert resolved.target is client.responses
    assert resolved.site.id == "responses.create"


def test_prefers_earlier_call_site_when_both_present() -> None:
    adapter = ProviderAdapter(
        _desc([["chat", "completions", "create"], ["responses", "create"]])
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_noop)),
        responses=SimpleNamespace(create=_noop),
    )
    resolved = adapter.get_call_site(client)
    assert resolved.target is client.chat.completions  # first listed wins


def test_raises_when_no_call_site_matches() -> None:
    adapter = ProviderAdapter(_desc([["chat", "completions", "create"]]))
    with pytest.raises(UnsupportedProviderError):
        adapter.get_call_site(SimpleNamespace())


# ── S0.1: get_call_sites resolves EVERY declared-and-present site ────────────


def test_get_call_sites_returns_all_present_sites() -> None:
    adapter = ProviderAdapter(
        _desc([["chat", "completions", "create"], ["responses", "create"]])
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_noop)),
        responses=SimpleNamespace(create=_noop),
    )
    sites = adapter.get_call_sites(client)
    assert [(s.target, s.method) for s in sites] == [
        (client.chat.completions, "create"),
        (client.responses, "create"),
    ]


def test_get_call_sites_skips_declared_but_absent_sites() -> None:
    adapter = ProviderAdapter(
        _desc([["chat", "completions", "create"], ["responses", "create"]])
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=_noop))
    sites = adapter.get_call_sites(client)
    assert [(s.target, s.method) for s in sites] == [(client.responses, "create")]


def test_get_call_sites_raises_when_none_match() -> None:
    adapter = ProviderAdapter(_desc([["chat", "completions", "create"]]))
    with pytest.raises(UnsupportedProviderError):
        adapter.get_call_sites(SimpleNamespace())


# ── P1.12: a resolved site knows WHICH endpoint it is ────────────────────────


def test_each_resolved_site_carries_its_own_identity_and_allowlist() -> None:
    """Two endpoints of one provider are not interchangeable.

    The whole point of the per-call-site model: `background` is a retention
    lever on /v1/responses and a TypeError on chat.completions. If both sites
    resolved to the same identity, the cloud could not tell them apart and would
    be back to forcing the intersection (losing the lever) or the union
    (breaking the call for exactly the users who opted out).
    """
    adapter = ProviderAdapter(
        _desc(
            [["chat", "completions", "create"], ["responses", "create"]],
            forceable={
                "chat.completions.create": ["store"],
                "responses.create": ["store", "background"],
            },
        )
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_noop)),
        responses=SimpleNamespace(create=_noop),
    )
    by_id = {s.site.id: s.site for s in adapter.get_call_sites(client)}

    assert by_id["chat.completions.create"].forceable_params == frozenset({"store"})
    assert by_id["responses.create"].forceable_params == frozenset(
        {"store", "background"}
    )


def test_descriptor_looks_a_call_site_up_by_its_wire_id() -> None:
    descriptor = _desc([["responses", "create"]], {"responses.create": ["background"]})

    assert descriptor.call_site("responses.create") is not None
    assert descriptor.call_site("responses.retrieve") is None
