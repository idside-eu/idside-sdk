"""Tests for the descriptor-driven ProviderAdapter engine."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from idside.adapters import ProviderAdapter
from idside.exceptions import ConsentTokenError
from idside.providers import load_descriptors

_OPENAI = load_descriptors()["openai"]
_ANTHROPIC = load_descriptors()["anthropic"]


def test_name_comes_from_descriptor() -> None:
    assert ProviderAdapter(_OPENAI).name == "openai"


def test_apply_injects_params_into_kwargs_for_openai() -> None:
    out = ProviderAdapter(_OPENAI).apply({"model": "gpt-4o"}, {"store": False}, "tok")
    assert out["store"] is False
    assert out["extra_headers"]["X-AI-Consent-Token"] == "tok"


def test_apply_injects_params_into_the_body_for_anthropic() -> None:
    """The forced param goes where Anthropic reads it; the token stays a header.

    `inference_geo` is a named parameter of `messages.create` and travels in the
    JSON body. Injecting it as a header — which this descriptor did until
    2026-07-26 — put it on the wire and applied nothing, which is worse than not
    sending it, because it looked enforced. The two destinations are
    independent: `param_target` moved, `token_header` did not.

    It is used here as a body parameter and nothing more: no option forces it
    since `data_residency` was downgraded to `account` on 2026-07-27, and
    Anthropic's allowlist is empty. What this pins is the injection mechanics of
    `param_target: "kwargs"`, which outlive any one parameter.
    """
    out = ProviderAdapter(_ANTHROPIC).apply(
        {"model": "claude-3"}, {"inference_geo": "us"}, "tok"
    )
    assert out["inference_geo"] == "us"
    assert "inference_geo" not in out["extra_headers"]
    assert out["extra_headers"]["X-AI-Consent-Token"] == "tok"


def test_apply_filters_protected_params() -> None:
    # The cloud must never be able to override core request fields.
    out = ProviderAdapter(_OPENAI).apply(
        {"model": "gpt-4o"}, {"model": "evil", "store": False}, "tok"
    )
    assert out["model"] == "gpt-4o"
    assert out["store"] is False


def test_apply_preserves_existing_extra_headers() -> None:
    out = ProviderAdapter(_OPENAI).apply({"extra_headers": {"X-Foo": "1"}}, {}, "tok")
    assert out["extra_headers"]["X-Foo"] == "1"
    assert out["extra_headers"]["X-AI-Consent-Token"] == "tok"


def test_apply_does_not_mutate_input() -> None:
    original = {"model": "gpt-4o"}
    ProviderAdapter(_OPENAI).apply(original, {"store": False}, "tok")
    assert original == {"model": "gpt-4o"}


def test_extract_metadata_returns_model_and_sha256() -> None:
    meta = ProviderAdapter(_OPENAI).extract_request_metadata(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
    assert meta["model"] == "gpt-4o"
    assert meta["request_hash"].startswith("sha256:")
    assert len(meta["request_hash"]) == len("sha256:") + 64


def test_apply_respects_custom_headers_kwarg() -> None:
    # mistralai carries per-request headers in http_headers, not extra_headers.
    from idside.providers import ProviderDescriptor

    descriptor = ProviderDescriptor.from_dict(
        {
            "name": "x",
            "client_module": "x",
            "call_sites": [{"id": "chat.complete", "path": ["chat", "complete"]}],
            "headers_kwarg": "http_headers",
        }
    )
    out = ProviderAdapter(descriptor).apply(
        {"model": "m", "http_headers": {"X-Foo": "1"}}, {"safe_prompt": True}, "tok"
    )
    assert out["safe_prompt"] is True
    assert out["http_headers"]["X-Foo"] == "1"
    assert out["http_headers"]["X-AI-Consent-Token"] == "tok"
    assert "extra_headers" not in out


def test_extract_metadata_ignores_identity_and_headers() -> None:
    adapter = ProviderAdapter(_OPENAI)
    base = adapter.extract_request_metadata(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )["request_hash"]
    with_extra = adapter.extract_request_metadata(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        user_id="usr_x",
        extra_headers={"X-Trace": "1"},
    )["request_hash"]
    assert base == with_extra


def test_extract_metadata_ignores_ubt() -> None:
    adapter = ProviderAdapter(_OPENAI)
    base = adapter.extract_request_metadata(model="gpt-4o", messages=[])
    with_ubt = adapter.extract_request_metadata(
        model="gpt-4o", messages=[], ubt="eyJ.fake.ubt"
    )
    assert base["request_hash"] == with_ubt["request_hash"]


# ── S0.2: the request hash must be deterministic and specified (JCS) ─────────


class _FakePydanticMessage:
    """Mimics a Pydantic v2 model: exposes ``model_dump()``."""

    def __init__(self, role: str, content: str) -> None:
        self._data = {"role": role, "content": content}

    def model_dump(self) -> dict[str, Any]:
        return dict(self._data)


def test_hash_is_independent_of_kwarg_order() -> None:
    adapter = ProviderAdapter(_OPENAI)
    a = adapter.extract_request_metadata(model="gpt-4o", temperature=0.2, n=2)
    b = adapter.extract_request_metadata(n=2, temperature=0.2, model="gpt-4o")
    assert a["request_hash"] == b["request_hash"]


def test_hash_of_pydantic_like_object_is_stable_and_equals_dict_form() -> None:
    adapter = ProviderAdapter(_OPENAI)
    as_object = adapter.extract_request_metadata(
        model="gpt-4o", messages=[_FakePydanticMessage("user", "hi")]
    )
    as_object_again = adapter.extract_request_metadata(
        model="gpt-4o", messages=[_FakePydanticMessage("user", "hi")]
    )
    as_dict = adapter.extract_request_metadata(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
    assert as_object["request_hash"] == as_object_again["request_hash"]
    assert as_object["request_hash"] == as_dict["request_hash"]


def test_hash_of_arbitrary_object_raises_consent_token_error() -> None:
    # Never fall back to repr(): a memory address in the hash would silently
    # destroy its reproducibility (and the ACT token's evidential value).
    class _Opaque:
        pass

    adapter = ProviderAdapter(_OPENAI)
    with pytest.raises(ConsentTokenError) as exc_info:
        adapter.extract_request_metadata(
            model="gpt-4o", messages=[{"role": "user", "content": _Opaque()}]
        )
    message = str(exc_info.value)
    assert "_Opaque" in message  # names the offending type
    assert "messages[0].content" in message  # and where it sits in the payload


def test_hash_of_a_class_without_a_json_schema_raises_consent_token_error() -> None:
    """A CLASS is checked for its schema before it is checked for anything else.

    `chat.completions.parse(response_format=Model)` takes the class itself, and
    the provider puts its JSON schema on the wire — so the schema is what the
    request hash must cover. A class that has none is not a structured-output
    model; falling through to `model_dump` there would find an unbound function,
    call it, and blow up somewhere with nothing to say about response_format.
    """

    class _NotAModel:
        pass

    adapter = ProviderAdapter(_OPENAI)
    with pytest.raises(ConsentTokenError) as exc_info:
        adapter.extract_request_metadata(model="gpt-4o", response_format=_NotAModel)

    message = str(exc_info.value)
    assert "response_format" in message  # where it sits in the payload
    assert "type" in message  # and that a class is what arrived


def test_hash_of_unsafe_integer_raises_consent_token_error() -> None:
    adapter = ProviderAdapter(_OPENAI)
    with pytest.raises(ConsentTokenError):
        adapter.extract_request_metadata(model="gpt-4o", seed=2**53 + 1)


def test_hash_of_non_string_dict_key_raises_consent_token_error() -> None:
    adapter = ProviderAdapter(_OPENAI)
    with pytest.raises(ConsentTokenError):
        adapter.extract_request_metadata(model="gpt-4o", logit_bias={123: -100})


def test_get_call_site_follows_descriptor_path() -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: None))
    )
    resolved = ProviderAdapter(_OPENAI).get_call_site(client)
    assert resolved.method == "create"
    assert resolved.target is client.chat.completions
    assert resolved.site.id == "chat.completions.create"


def test_a_descriptor_may_put_forced_params_in_the_headers_instead() -> None:
    """`param_target` is the engine's other declarative seam, and it is exclusive.

    A provider whose per-request controls travel as headers must not also get
    them as kwargs: a body schema that rejects unknown fields turns the second
    copy into a 400, and the call the user consented to never happens. The ACT
    token still goes where it always goes, next to them.
    """
    from idside.providers import ProviderDescriptor

    descriptor = ProviderDescriptor.from_dict(
        {
            "name": "x",
            "client_module": "x",
            "call_sites": [{"id": "chat.complete", "path": ["chat", "complete"]}],
            "param_target": "headers",
        }
    )

    out = ProviderAdapter(descriptor).apply(
        {"model": "m"}, {"x-retention": "none"}, "tok"
    )

    assert "x-retention" not in out, "a forced param was sent twice"
    headers = out[descriptor.headers_kwarg]
    assert headers["x-retention"] == "none"
    assert headers["X-AI-Consent-Token"] == "tok"
