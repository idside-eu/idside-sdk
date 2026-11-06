"""Where a wrapped client sends, and what the SDK does when it cannot tell.

The origin frozen at ``wrap()`` and the origin read off each outgoing request
are compared for equality, so the two readings must agree on every spelling a
library hands them: an ``httpx.URL`` whose default port reads ``None``, a
string with the port written out, a host in capitals. A reading that disagreed
with itself would refuse the client's own traffic; one that gave up too easily
would compare nothing at all. Both halves are pinned here, together with the
two places that decide what an unreadable destination means — the provider's
own client is refused, an object of another shape keeps the answer it always
had.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from idside import _destination, consent
from idside._destination import Origin, client_origin, origin_of, unguarded_route
from idside._transport import ConsentDecision
from idside.adapters import ProviderAdapter
from idside.exceptions import UnsupportedProviderError
from idside.providers import load_descriptors

openai = pytest.importorskip("openai")

_OPENAI = load_descriptors()["openai"]


class _Cloud:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        return ConsentDecision(
            consent_state={},
            params_forced={},
            act_token="act.test",
            source="widget",
            legal_basis="consent",
            binding="user_id",
        )


# ── reading an origin ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            httpx.URL("https://api.openai.com/v1"),
            Origin("https", "api.openai.com", 443),
        ),
        ("https://API.OpenAI.com:443/v1/", Origin("https", "api.openai.com", 443)),
        ("http://127.0.0.1:8080/v1", Origin("http", "127.0.0.1", 8080)),
        (httpx.URL("http://[::1]:9000/"), Origin("http", "::1", 9000)),
        ("wss://relay.example/socket", Origin("wss", "relay.example", None)),
        (
            SimpleNamespace(scheme="HTTPS", host="API.OpenAI.com", port=None),
            Origin("https", "api.openai.com", 443),
        ),
    ],
    ids=[
        "httpx.URL",
        "string-with-default-port",
        "string-with-port",
        "ipv6",
        "odd",
        "url-object-that-keeps-case",
    ],
)
def test_one_origin_whatever_the_spelling(url: Any, expected: Origin) -> None:
    assert origin_of(url) == expected


@pytest.mark.parametrize(
    "url",
    [None, "", "/v1/chat/completions", "https://api.openai.com:notaport/v1"],
    ids=["none", "empty", "path-only", "bad-port"],
)
def test_a_url_that_names_no_host_has_no_origin(url: Any) -> None:
    assert origin_of(url) is None


def test_the_libraries_each_say_where_they_send() -> None:
    genai = pytest.importorskip("google.genai")
    mistral_client = pytest.importorskip("mistralai.client")
    anthropic = pytest.importorskip("anthropic")

    assert client_origin(openai.OpenAI(api_key="sk-test")) == (
        "https",
        "api.openai.com",
        443,
    )
    assert client_origin(anthropic.Anthropic(api_key="sk-test")) == (
        "https",
        "api.anthropic.com",
        443,
    )
    assert client_origin(genai.Client(api_key="test")) == (
        "https",
        "generativelanguage.googleapis.com",
        443,
    )
    assert client_origin(mistral_client.Mistral(api_key="test")) == (
        "https",
        "api.mistral.ai",
        443,
    )


class _RaisingConfiguration:
    def get_server_details(self) -> Any:
        raise ValueError('Invalid server "nowhere"')


class _RaisingBaseURL:
    @property
    def base_url(self) -> str:
        raise RuntimeError("not configured")


@pytest.mark.parametrize(
    "client",
    [
        object(),
        SimpleNamespace(),
        SimpleNamespace(sdk_configuration=_RaisingConfiguration()),
        _RaisingBaseURL(),
    ],
    ids=["no-__dict__", "no-base_url", "server-details-raise", "base_url-raises"],
)
def test_a_client_that_will_not_say_where_it_sends_has_no_origin(client: Any) -> None:
    assert client_origin(client) is None


# ── routes the net cannot see ────────────────────────────────────────────────


def test_a_mode_probe_that_is_not_a_method_is_not_a_mode() -> None:
    client = SimpleNamespace(
        _api_client=SimpleNamespace(_use_google_auth_sync=True, _use_aiohttp=None)
    )

    assert unguarded_route(client) is None


def test_a_mode_probe_that_raises_cannot_rule_the_mode_out() -> None:
    def _raises() -> bool:
        raise RuntimeError("credentials not loaded")

    client = SimpleNamespace(_api_client=SimpleNamespace(_use_google_auth_sync=_raises))

    route = unguarded_route(client)

    assert route is not None
    assert "httpx_client" in route


def test_a_client_without_an_api_client_has_no_such_route() -> None:
    assert unguarded_route(object()) is None


# ── what wrap() makes of it ─────────────────────────────────────────────────


def test_the_provider_s_own_client_must_say_where_it_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an origin the net could only check the token — which matches."""
    monkeypatch.setattr(_destination, "client_origin", lambda _client: None)
    raw = openai.OpenAI(api_key="sk-test")

    with pytest.raises(UnsupportedProviderError):
        consent.wrap(raw, consent_client=_Cloud())

    assert not hasattr(raw.chat.completions.create, "__idside_consent_enforced__")


class _Completions:
    def create(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs


class _FakeClient:
    """Neither the provider's library nor identifiable: an explicit adapter's job."""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_Completions())

    def with_options(self, **_options: Any) -> _FakeClient:
        return _FakeClient()


def test_a_client_of_another_shape_derives_without_an_identity_to_check() -> None:
    cloud = _Cloud()
    wrapped = consent.wrap(
        _FakeClient(), adapter=ProviderAdapter(_OPENAI), consent_client=cloud
    )

    derived = wrapped.with_options(timeout=5)
    derived.chat.completions.create(model="gpt-4o-mini", user_id="usr_marie_456")

    assert len(cloud.calls) == 1


@pytest.mark.parametrize(
    ("origin", "written"),
    [
        (Origin("https", "api.openai.com", 443), "https://api.openai.com"),
        (Origin("http", "127.0.0.1", 8080), "http://127.0.0.1:8080"),
        (Origin("wss", "relay.example", None), "wss://relay.example"),
        (Origin("https", "api.openai.com", None), "https://api.openai.com"),
        (Origin("http", "::1", 9000), "http://[::1]:9000"),
    ],
    ids=["default-port", "explicit-port", "no-port", "no-port-on-https", "ipv6"],
)
def test_an_origin_is_written_the_way_a_url_would_write_it(
    origin: Origin, written: str
) -> None:
    """It goes into error messages, where ``host:None`` names no address at all."""
    assert str(origin) == written


class _OddSchemeClient(_FakeClient):
    """A client on a scheme with no default port: its origin has none."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url

    def with_options(self, **options: Any) -> _OddSchemeClient:
        return _OddSchemeClient(options.get("base_url", self.base_url))


def test_a_derivation_elsewhere_names_the_origin_without_an_empty_port() -> None:
    wrapped = consent.wrap(
        _OddSchemeClient("wss://relay.example/v1"),
        adapter=ProviderAdapter(_OPENAI),
        consent_client=_Cloud(),
    )

    with pytest.raises(UnsupportedProviderError) as caught:
        wrapped.with_options(base_url="wss://collector.attacker.example/v1")

    assert "(wss://relay.example)" in str(caught.value)
    assert "None" not in str(caught.value)


def test_a_derived_client_nobody_can_identify_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from idside import consent as consent_module

    wrapped = consent.wrap(openai.OpenAI(api_key="sk-test"), consent_client=_Cloud())
    monkeypatch.setattr(consent_module, "_detected_provider", lambda _client: None)

    with pytest.raises(UnsupportedProviderError) as caught:
        wrapped.with_options(timeout=5)

    assert "not identifiable" in str(caught.value)


# ── internationalised hosts ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spelling",
    [
        "https://xn--bcher-kva.example/v1",
        "https://bücher.example/v1",
        httpx.URL("https://xn--bcher-kva.example/v1"),
        httpx.URL("https://bücher.example/v1"),
    ],
    ids=["punycode-string", "unicode-string", "punycode-url", "unicode-url"],
)
def test_an_internationalised_host_is_one_origin_in_every_spelling(
    spelling: Any,
) -> None:
    """One parser for both halves: the frozen origin and the request's."""
    httpx2 = pytest.importorskip("httpx2")

    assert origin_of(spelling) == origin_of(httpx.URL("https://bücher.example/"))
    assert origin_of(spelling) == origin_of(
        httpx2.URL("https://xn--bcher-kva.example/")
    )


@pytest.mark.parametrize(
    "server_url",
    ["https://xn--bcher-kva.example", "https://bücher.example"],
    ids=["punycode", "unicode"],
)
def test_a_client_on_an_internationalised_host_can_still_call(server_url: str) -> None:
    """Mistral names its origin as a string: the case one parser per side broke."""
    mistral_client = pytest.importorskip("mistralai.client")
    sent: list[str] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.host)
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "model": "m",
                "created": 0,
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
            },
        )

    client = consent.wrap(
        mistral_client.Mistral(
            api_key="mk-test",
            server_url=server_url,
            client=httpx.Client(transport=httpx.MockTransport(_answer)),
        ),
        consent_client=_Cloud(),
    )

    client.chat.complete(
        model="mistral-small-latest", messages=[], user_id="usr_marie_456"
    )

    assert sent == ["bücher.example"]
