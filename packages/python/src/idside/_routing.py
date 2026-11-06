# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Credential routing: which of the developer's credentials carries this call.

No provider lets a developer switch training off per request. The setting lives
on the credential — per project at OpenAI and Gemini, per workspace at
Anthropic, per organisation at Mistral — so one key means one posture for every
user of the app at once. A developer who provisions one credential per posture
lets the SDK send each call on the one matching that user's answer, and the
account control becomes a personal one. It is the only route by which a
training opt-out can be honoured per user today.

What this module does and does not decide:

- it does **not** decide which posture a consent state calls for. That is
  policy, it lives in the cloud, and the SDK is told the answer
  (``credential_posture`` on the authorize response). There is no
  consent→credential table here, exactly as there is no consent→params one;
- it does **not** let the developer choose either. They label what they hold;
  the label is matched against the cloud's answer. A callback would hand them
  the user's preferences, which is invariant #1;
- it fails closed. A posture nobody provisioned stops the call, because the
  alternative is sending it on a credential the user's answer excludes, with
  nothing anywhere to say so.

This module is internal (``_routing``). The public surface is the
``credentials=`` argument of :func:`idside.consent.wrap`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from idside.exceptions import CredentialRoutingError, MissingCredentialError
from idside.providers import CredentialRouting, ProviderDescriptor

#: Fallback shape for a routed header value when the descriptor pins no pattern:
#: printable ASCII, no space, no control character. Its job is narrow but not
#: optional — the value is written into an outgoing HTTP header, and a CR or LF
#: in it is header injection on the developer's own provider request.
_SAFE_HEADER_VALUE = re.compile(r"[!-~]{1,200}")


@dataclass(frozen=True)
class Route:
    """Where one call must go once the cloud has named a posture.

    Exactly one of the two mechanisms applies, and which one is a property of
    the provider, not of the call: ``headers`` for a provider that validates a
    per-request credential header (OpenAI's ``OpenAI-Project``), ``call`` for
    everyone else, where the key is fixed at client construction and switching
    posture means switching client. A route with neither is the ordinary case:
    the cloud named no posture, and nothing about the call changes.
    """

    headers: dict[str, str] = field(default_factory=dict)
    call: Callable[..., Any] | None = None


#: The unrouted call: no header to add, and the client the developer wrapped.
NO_ROUTE = Route()


def _resolve(client: Any, path: Sequence[str]) -> Any:
    """Walk an attribute path on a client, or return None if it does not exist."""
    current = client
    for attr in path:
        if not hasattr(current, attr):
            return None
        current = getattr(current, attr)
    return current


class CredentialRouter:
    """The developer's labelled credentials, resolved once at ``wrap()`` time.

    Everything that can be checked early is checked here rather than on the AI
    request path: an unroutable provider, a bare identifier where only a client
    will do, an identifier of the wrong shape, an alternative client missing the
    endpoint being wrapped. A developer meets those at startup, not in
    production, and never as a call that quietly went out on the wrong account.
    """

    def __init__(
        self,
        descriptor: ProviderDescriptor,
        credentials: Mapping[str, Any],
        call_sites: Sequence[tuple[str, Sequence[str]]],
        *,
        is_enforced: Callable[[Any], bool] = lambda method: False,
    ) -> None:
        """Validate and resolve ``credentials`` against a provider and a client.

        ``call_sites`` are the endpoints ``wrap()`` is about to patch, as
        ``(id, path)`` pairs — one per *method*, so an aliased endpoint appears
        once per alias with its own path. An alternative client must expose
        every one of them, or a call routed to it would find nothing to call,
        or worse, the wrong thing: two methods may share a wire id and still
        behave differently (``messages.parse`` is not ``messages.create``).

        ``is_enforced`` recognises a method this SDK has already patched. It is
        injected so the enforcement marker stays private to
        :mod:`idside.consent`; routing to an already-wrapped client would
        authorize twice and stack two ACT tokens onto one request.
        """
        if not isinstance(credentials, Mapping):
            raise CredentialRoutingError(
                "consent.wrap(credentials=…) takes a mapping of posture label → "
                f"credential, not {type(credentials).__name__}. The label is what "
                "the ID Side cloud names when it routes a call; a bare sequence "
                "of credentials would carry no posture at all."
            )
        routing = descriptor.credential_routing
        if routing is None:
            raise CredentialRoutingError(
                f"consent.wrap() was given credentials for '{descriptor.name}', "
                "which declares no credential routing: it exposes no account-level "
                "privacy posture a second credential could carry, so routing "
                "between two of them would change nothing for the user. Remove the "
                "credentials argument."
            )

        self._descriptor = descriptor
        self._routing = routing
        self._headers: dict[str, dict[str, str]] = {}
        self._calls: dict[tuple[str, tuple[str, ...]], Callable[..., Any]] = {}

        for posture, credential in credentials.items():
            if not isinstance(posture, str) or not posture.strip():
                raise CredentialRoutingError(
                    f"Every credential passed to consent.wrap() must be labelled "
                    f"with the posture it carries, and {posture!r} is not a usable "
                    "label. The label is what the ID Side cloud names when it "
                    "routes a call, so it must match the one configured for this "
                    "app in the dashboard."
                )
            if isinstance(credential, str):
                self._headers[posture] = {
                    self._header_name(posture): self._checked_value(credential, posture)
                }
            else:
                self._add_client(posture, credential, call_sites, is_enforced)

        #: Sorted so two processes holding the same configuration send the cloud
        #: the same payload.
        self.postures: tuple[str, ...] = tuple(sorted(credentials))

    # ── configuration ──────────────────────────────────────────────────────

    def _header_name(self, posture: str) -> str:
        """The routing header, or explain why a string cannot work here."""
        if self._routing.header:
            return self._routing.header
        raise CredentialRoutingError(
            f"The credential labelled '{posture}' is a bare identifier, but "
            f"'{self._descriptor.name}' reads no per-request header naming a "
            f"{self._routing.unit}: its key is chosen when the client is built. "
            f"Pass a second client constructed with that {self._routing.unit}'s "
            "key instead — a string here would be accepted and never sent, which "
            "is the one outcome routing exists to prevent."
        )

    def _checked_value(self, value: str, posture: str) -> str:
        """Validate a routed header value; never echo it back on failure."""
        pattern = self._routing.header_value_pattern
        ok = (
            re.fullmatch(pattern, value)
            if pattern
            else _SAFE_HEADER_VALUE.fullmatch(value)
        )
        if ok:
            return value
        expected = f" (expected {pattern})" if pattern else ""
        raise CredentialRoutingError(
            f"The credential labelled '{posture}' is not a "
            f"{self._descriptor.name} {self._routing.unit} identifier{expected}. "
            "The value is not repeated here on purpose: if what you pasted is an "
            "API key, it is a secret, and it was about to travel in a header that "
            "is not the one that carries it."
        )

    def _add_client(
        self,
        posture: str,
        credential: Any,
        call_sites: Sequence[tuple[str, Sequence[str]]],
        is_enforced: Callable[[Any], bool],
    ) -> None:
        """Resolve every wrapped endpoint on an alternative provider client."""
        self._check_same_provider(posture, credential)
        for _site_id, path in call_sites:
            method = _resolve(credential, path)
            if not callable(method):
                raise CredentialRoutingError(
                    f"The client passed as credential '{posture}' does not expose "
                    f"'{'.'.join(path)}', which consent.wrap() is enforcing on the "
                    "client you wrapped. A call routed to it would have nothing to "
                    "call. Pass a client of the same kind, built with that "
                    f"{self._routing.unit}'s key."
                )
            if is_enforced(method):
                raise CredentialRoutingError(
                    f"The client passed as credential '{posture}' is already "
                    "wrapped by consent.wrap(). Routing a call to it would "
                    "authorize the same request twice and stack two ACT tokens on "
                    "it. Pass the unwrapped client."
                )
            self._calls[(posture, tuple(path))] = method

    def _check_same_provider(self, posture: str, credential: Any) -> None:
        """Refuse a routed client that belongs to a different provider.

        Best effort, and it only ever refuses on a positive identification. It
        matters because the shapes coincide: an ``OpenAI`` client pointed at
        another vendor's base URL is still an ``OpenAI`` client, so every call
        site resolves and the only thing that differs is which company receives
        the prompt.
        """
        from idside.adapters import get_adapter_for
        from idside.exceptions import UnsupportedProviderError

        try:
            detected = get_adapter_for(credential).name
        except UnsupportedProviderError:
            return  # unidentifiable is not evidence of anything
        if detected != self._descriptor.name:
            raise CredentialRoutingError(
                f"The client passed as credential '{posture}' is a "
                f"'{detected}' client, but consent.wrap() is enforcing "
                f"'{self._descriptor.name}'. Routing between two providers would "
                "send the request to a company the user's answer never mentioned."
            )

    # ── the request path ───────────────────────────────────────────────────

    def route(self, posture: str, call_path: Sequence[str]) -> Route:
        """The route for ``posture``, or refuse the call.

        Called once per AI request, after the cloud has named the posture this
        user's answer requires. ``call_path`` is the attribute path of the method
        being called, not the endpoint's wire id: an aliased method must land on
        the same method of the alternative client.
        """
        headers = self._headers.get(posture)
        if headers is not None:
            return Route(headers=headers)
        call = self._calls.get((posture, tuple(call_path)))
        if call is not None:
            return Route(call=call)
        raise MissingCredentialError(
            missing_message(self._descriptor, posture, self.postures),
            posture=posture,
            provider=self._descriptor.name,
            provisioned=self.postures,
        )


def missing_message(
    descriptor: ProviderDescriptor, posture: str, provisioned: tuple[str, ...]
) -> str:
    """Say what is missing, in the provider's own vocabulary.

    Names the labels that do exist and never a credential: the labels are the
    developer's own words for their configuration, the credentials are secrets.
    """
    routing: CredentialRouting | None = descriptor.credential_routing
    unit = routing.unit if routing else "credential"
    have = ", ".join(f"'{name}'" for name in provisioned) if provisioned else "none"
    return (
        f"This user's consent requires the '{posture}' credential for "
        f"'{descriptor.name}', and consent.wrap() was given none carrying it "
        f"(provisioned: {have}). The AI call was not made: sending it on another "
        f"{unit} would place this user under a privacy regime they declined, and "
        f"nothing would record that it happened. Provision a {unit} with that "
        f"posture and pass it to consent.wrap() as credentials={{'{posture}': …}}."
    )
