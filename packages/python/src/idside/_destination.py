# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Where a wrapped client sends its requests, reduced to what can be compared.

The net used to bind an authorized request to its method and path, and never to
its host. A per-call ``server_url``, a derived client pointed elsewhere, a
``client.base_url = …`` after ``wrap()``: each sent the prompt, the ACT token
and the provider key to an address nobody had wrapped, carrying a token that
matched. So ``wrap()`` freezes the client's origin — scheme, host and effective
port, and nothing else of the URL — and the net refuses a body-bearing request
bound anywhere else.

One module for both halves on purpose: the origin frozen from a client's base
URL and the origin read off an outgoing request must be normalised the same
way, or the comparison refuses the client's own traffic.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import httpx

__all__ = ["Origin", "client_origin", "origin_of", "unguarded_route"]

_DEFAULT_PORTS = {"http": 80, "https": 443}


class Origin(NamedTuple):
    """Scheme, host and effective port: what a URL says about who receives it."""

    scheme: str
    host: str
    port: int | None

    def __str__(self) -> str:
        """The origin as a URL writes it, for a message a developer reads.

        No port when there is none or it is the scheme's default: ``host:None``
        names no address, and ``:443`` only adds noise to one. An IPv6 host is
        bracketed, or its colons would read as the port.
        """
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = (
            ""
            if self.port is None or self.port == _DEFAULT_PORTS.get(self.scheme)
            else f":{self.port}"
        )
        return f"{self.scheme}://{host}{port}"


def origin_of(url: Any) -> Origin | None:
    """The origin of a URL object or string, or None if it names no host.

    ``httpx.URL`` and ``httpx2.URL`` are read through their attributes, which
    already hold the parsed host — never through ``str()``, so a request's query
    string is not so much as formatted. A string goes through ``httpx.URL``: the
    same parser as the requests it is compared with, since two parsers disagree
    on an internationalised host — ``urlsplit`` keeps ``xn--bcher-kva.example``
    as written where ``httpx`` decodes it to ``bücher.example``, and every call
    of a client configured in punycode was refused.
    """
    if url is None:
        return None
    scheme = getattr(url, "scheme", None)
    host = getattr(url, "host", None)
    port: Any
    if isinstance(scheme, str) and isinstance(host, str):
        port = getattr(url, "port", None)
    else:
        try:
            parsed = httpx.URL(str(url))
        except httpx.InvalidURL:  # a port that is not a number is no origin at all
            return None
        scheme, host, port = parsed.scheme, parsed.host, parsed.port
    if not scheme or not host:
        return None
    scheme = scheme.lower()
    return Origin(
        scheme,
        host.lower(),
        port if isinstance(port, int) else _DEFAULT_PORTS.get(scheme),
    )


def client_origin(client: Any) -> Origin | None:
    """The origin a provider client sends to, or None if it does not say.

    Read from the attribute each library itself builds URLs from — never from
    the transport, whose own ``base_url`` the libraries do not use and which a
    derived client shares with its parent. ``google-genai`` keeps it in
    ``_api_client._http_options``, ``mistralai`` in ``sdk_configuration``, the
    Stainless clients (``openai``, ``anthropic``) in ``base_url``.
    """
    state = getattr(client, "__dict__", None)
    if not isinstance(state, dict):
        return None
    api_client = state.get("_api_client")
    if api_client is not None:
        options = getattr(api_client, "_http_options", None)
        return origin_of(getattr(options, "base_url", None))
    configuration = state.get("sdk_configuration")
    if configuration is not None:
        try:
            server_url, _variables = configuration.get_server_details()
        except Exception:
            return None
        return origin_of(server_url)
    try:
        return origin_of(getattr(client, "base_url", None))
    except Exception:
        # A `base_url` property is code; one that raises names no origin.
        return None


#: Modes in which ``google-genai`` sends through something that is not an HTTP
#: client the net can recognise, with what to do instead.
_GENAI_UNGUARDED = (
    (
        "_use_google_auth_sync",
        "sends on google-auth's `requests` session (Vertex AI with mTLS)",
        "configure mTLS on an httpx.Client and pass it as "
        "http_options=HttpOptions(httpx_client=…)",
    ),
    (
        "_use_aiohttp",
        "sends its async requests on aiohttp",
        "pass http_options=HttpOptions(httpx_async_client=httpx.AsyncClient())",
    ),
    (
        # Not implied by `_use_aiohttp`: an `async_client_args` transport turns
        # that one off, and leaves this one on.
        "_use_google_auth_async",
        "sends its async requests on google-auth's aiohttp session "
        "(Vertex AI with mTLS)",
        "configure mTLS on an httpx.AsyncClient and pass it as "
        "http_options=HttpOptions(httpx_async_client=…)",
    ),
)


def unguarded_route(client: Any) -> str | None:
    """Describe a route out of ``client`` the net cannot guard, if it has one.

    The net recognises HTTP clients by shape, and patches the ones a client
    holds. A client that sends through something else — a ``requests`` session,
    an ``aiohttp`` session created on first use — has a route no patch reaches,
    and wrapping it would claim a coverage that is not there.
    """
    state = getattr(client, "__dict__", None)
    api_client = state.get("_api_client") if isinstance(state, dict) else None
    if api_client is None:
        return None
    for probe, route, remedy in _GENAI_UNGUARDED:
        check = getattr(api_client, probe, None)
        if not callable(check):
            continue
        try:
            unguarded = bool(check())
        except Exception:
            unguarded = True  # a mode that cannot be asked cannot be ruled out
        if unguarded:
            return f"it {route}. To wrap it, {remedy}"
    return None
