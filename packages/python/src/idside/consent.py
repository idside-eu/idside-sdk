# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Public entry point: ``consent.wrap()``.

⚠️ *Opaque tunnel* invariant: this module exposes **no** function to set or
modify preferences (no ``set_preferences``, ``override_consent``…). The SDK only
**reads** the state resolved by the cloud.
"""

from __future__ import annotations

import functools
import inspect
import weakref
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, NamedTuple, cast

from idside import _destination, _guard, _log
from idside._destination import Origin
from idside._routing import NO_ROUTE, CredentialRouter, Route, missing_message
from idside._transport import (
    AsyncConsentClient,
    AsyncHttpConsentClient,
    ConsentClient,
    ConsentDecision,
    HttpConsentClient,
    _named,
)
from idside.adapters import get_adapter_for
from idside.adapters._call_options import (
    refuse_declared_rewrites,
    refuse_underivable,
    refuse_unenforceable,
)
from idside.adapters.base import ProviderAdapter
from idside.exceptions import (
    ConflictingIdentityError,
    CredentialRoutingError,
    MissingCredentialError,
    MissingIdentityError,
    UnsupportedProviderError,
)
from idside.providers import ProviderDescriptor

__all__ = ["wrap"]

#: Either transport to the cloud. Which one a wrapped client got is decided once,
#: at ``wrap()`` time, from the shape of the client itself.
AnyConsentClient = ConsentClient | AsyncConsentClient

#: Marker set on patched methods so a second ``wrap()`` never stacks enforcement.
_ENFORCED_MARKER = "__idside_consent_enforced__"


class _WrapState:
    """Completion marker shared by the call sites patched in one wrap attempt."""

    __slots__ = ("complete",)

    def __init__(self) -> None:
        self.complete = False


#: Methods that hand back a *fresh* client built from this one. Left alone, they
#: are a hole the size of the whole SDK: ``client.with_options(timeout=30)``
#: returns an unpatched client that still talks to the provider. They are
#: patched so the derived client comes back enforced too.
_DERIVING_METHODS = ("with_options", "copy", "with_middleware")

#: Aliases whose call builds a context manager and defers the HTTP request to
#: the moment it is entered. Every Python client's ``stream()`` helper is one:
#: ``chat.completions.stream(...)`` returns a ``ChatCompletionStreamManager``
#: and posts nothing until ``with`` runs. Authorizing at the call would mint an
#: ACT token and a ledger entry for a request that may never be made — a proof
#: of something that did not happen, which is worse than no proof at all. So the
#: cloud is asked in ``__enter__``, next to the request it authorizes.
#:
#: Applied to ALIASES only, never to a call site's own path: Mistral's
#: ``chat.stream`` is a call site named ``stream`` whose request goes out at the
#: call, and deferring that one would be the mirror-image mistake. This mirrors
#: the Node SDK's ``UNPATCHABLE_ALIASES``, which names the same helper for the
#: same reason — its return type is what makes the two SDKs treat it specially.
_DEFERRED_ALIASES = frozenset({"stream"})


class _InternalCall:
    """Private proof tying one authorized call to its wrapper and route."""

    __slots__ = ("call_site", "consumed", "scope", "token", "transport_route")

    def __init__(self, call_site: str, token: str, scope: object) -> None:
        self.call_site = call_site
        self.token = token
        self.scope = scope
        self.consumed = False
        self.transport_route: tuple[str, str] | None = None

    def consume(self, scope: object, call_site: str, token: Any) -> bool:
        """Consume this permit only for the matching call site and ACT token."""
        if (
            self.consumed
            or scope is not self.scope
            or call_site != self.call_site
            or token != self.token
        ):
            return False
        self.consumed = True
        return True

    def allows_transport(
        self, scope: object, token: Any, method: str, path: str
    ) -> bool:
        """Bind transport use to this wrapper, token, and first request route."""
        if scope is not self.scope or not isinstance(token, str) or token != self.token:
            return False
        route = (method.upper(), path)
        if self.transport_route is None:
            self.transport_route = route
        return self.transport_route == route


# Context-local so concurrent async calls cannot consume one another's permit.
# It exists only while the already-authorized provider method is running; unlike
# the public ACT header, callers cannot manufacture this provenance.
_INTERNAL_CALL: ContextVar[_InternalCall | None] = ContextVar(
    "idside_internal_call", default=None
)


class _TransportScope:
    """The identity a wrapped client's permits and transport predicate share.

    A class rather than a bare ``object()`` only because the predicate refers to
    it weakly: see :func:`_transport_provenance`.
    """

    __slots__ = ("__weakref__",)


def _transport_provenance(
    scope: _TransportScope, origin: Origin | None
) -> _guard.Provenance:
    """Build the guard predicate for one wrapped client.

    ``origin`` is the one frozen at ``wrap()``, and it lives here — in the
    predicate belonging to this client — rather than on the transport: a derived
    client shares its parent's transport, and ``transport.base_url`` is not what
    the provider libraries build URLs from anyway. The origin is compared before
    the permit is consulted, so a request bound elsewhere cannot fix the route
    the permit then holds its retries to. ``None`` is a client that names no
    origin (a test double, an exotic adapter): nothing to compare against.

    The scope is held WEAKLY. A derived client shares its parent's transport and
    adds a predicate to it; held strongly, every ``client.with_options(...)``
    made per request would stay in that list for the life of the process,
    growing the memory and the work of every check. The scope lives exactly as
    long as the client's enforced methods, so once the client is collected the
    predicate reports itself dead and the guard drops it.
    """
    scope_ref = weakref.ref(scope)

    def _has_provenance(
        token: str, method: str, path: str, request_origin: Origin | None
    ) -> bool:
        if origin is not None and request_origin != origin:
            return False
        permit = _INTERNAL_CALL.get()
        return permit is not None and permit.allows_transport(
            scope_ref(), token, method, path
        )

    setattr(_has_provenance, _guard.ALIVE, lambda: scope_ref() is not None)
    return _has_provenance


class _Lineage(NamedTuple):
    """What a derived client must still share with the client it came from.

    ``with_options()`` and ``copy()`` build a fresh client from keyword
    arguments, and any of them can change what was wrapped:
    ``base_url=`` moves every call elsewhere, ``default_headers=`` can add a
    second ``openai-project`` that case-sensitive merging leaves beside the
    routed one. So the parent's facts travel to the derivation and the derived
    client is held to them — ``provider`` as detected (``None`` when the parent
    itself could not be identified), ``origin`` as frozen, and the reserved
    headers the parent was built with.

    ``route_scopes`` are the nets the parent put on the clients named in
    ``credentials``, by posture label. The derived client routes to the same
    clients, so it sends through the same nets, held to the origins frozen when
    the parent was wrapped — never to whatever those clients say at derivation.

    Carried by a private argument, never an option of ``wrap()``: a public way
    to hand ``wrap()`` a different origin would be a bypass (invariant #8).
    """

    provider: str | None
    origin: Origin | None
    reserved_headers: tuple[tuple[str, Any], ...]
    route_scopes: Mapping[str, _TransportScope]


class _RoutedClient(NamedTuple):
    """A client named in ``credentials``, and what ``wrap()`` froze of it.

    ``labels`` are every posture it carries: one client may be named twice.
    """

    labels: tuple[str, ...]
    client: Any
    origin: Origin | None


def _lazily_permitted(permit: _InternalCall, result: Any) -> Any:
    """Keep ``permit`` in force for I/O the provider has not done yet.

    A declared call site can be a *generator function*: ``google.genai``'s
    ``models.generate_content_stream`` is one. Calling it runs none of its body
    and sends nothing — the request happens on the first ``next()``, long after
    the ``with`` block that authorized the call has reset the context variable.
    Without this, the net would refuse the very request this SDK just
    authorized, and every Gemini streaming call would raise
    ``UnenforcedCallError``.

    The permit is re-established around each step of the iteration and around
    nothing else: the consumer never gets control while one is in scope, so the
    window stays exactly as wide as the provider's own work.
    """
    if inspect.isasyncgen(result):
        return _permitted_async_iteration(permit, result)
    if inspect.isgenerator(result):
        return _permitted_iteration(permit, result)
    return result


def _permitted_iteration(permit: _InternalCall, source: Any) -> Any:
    """Drive ``source`` one step at a time with ``permit`` installed."""
    try:
        while True:
            context_token = _INTERNAL_CALL.set(permit)
            try:
                item = next(source)
            except StopIteration:
                return
            finally:
                _INTERNAL_CALL.reset(context_token)
            yield item
    finally:
        # The consumer may abandon the stream; the provider still owns an open
        # HTTP response and has to be told, exactly as `yield from` would.
        source.close()


async def _permitted_async_iteration(permit: _InternalCall, source: Any) -> Any:
    """The async half of :func:`_permitted_iteration`, step for step."""
    try:
        while True:
            context_token = _INTERNAL_CALL.set(permit)
            try:
                item = await source.__anext__()
            except StopAsyncIteration:
                return
            finally:
                _INTERNAL_CALL.reset(context_token)
            yield item
    finally:
        await source.aclose()


def _is_async_call_site(func: Any) -> bool:
    """Return True if calling ``func`` yields a coroutine (an async def site).

    ``inspect.iscoroutinefunction`` already sees through bound methods; we also
    peel ``functools.partial`` and ``functools.wraps`` layers so an async call
    method hidden behind them (e.g. a decorated ``AsyncOpenAI`` entrypoint) is
    still detected.
    """
    while isinstance(func, functools.partial):
        func = func.func
    if inspect.iscoroutinefunction(func):
        return True
    try:
        unwrapped = inspect.unwrap(func)
    except ValueError:  # __wrapped__ cycle
        return False
    return unwrapped is not func and inspect.iscoroutinefunction(unwrapped)


def _is_enforced(method: Any) -> bool:
    """True if this SDK has already patched ``method``."""
    return bool(getattr(method, _ENFORCED_MARKER, False))


def _is_completely_wrapped(method: Any) -> bool:
    """True only after the wrap attempt that patched ``method`` completed."""
    state = getattr(method, _ENFORCED_MARKER, None)
    return isinstance(state, _WrapState) and state.complete


async def _awaited(value: Any) -> Any:
    """Await ``value`` if it is awaitable, otherwise take it as it is.

    An async client is given the async transport to the cloud, so this is not
    where an event loop gets blocked. It exists because ``consent_client=`` is a
    documented override: a test double, or an application's own implementation
    of the protocol, is a plain callable more often than not, and refusing one
    on the async path would buy no safety at all.
    """
    return await value if inspect.isawaitable(value) else value


def wrap(
    client: Any,
    *,
    consent_client: AnyConsentClient | None = None,
    adapter: ProviderAdapter | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    credentials: Mapping[str, Any] | None = None,
) -> Any:
    """Wrap an AI provider client to enforce ID Side consent.

    On every intercepted AI call, the wrapped client: (1) resolves the user's
    preferences from the ID Side cloud and obtains a signed ACT token, (2) forces
    the provider's non-retention parameters (Type 1), (3) attaches the ACT token
    (Type 2) as the ``X-AI-Consent-Token`` header, (4) calls the provider. If
    consent cannot be verified, the call does not happen (fail-closed).

    EVERY call site declared by the provider descriptor and present on the
    client is enforced (e.g. both OpenAI entrypoints, ``chat.completions.create``
    and ``responses.create``); declared sites absent from the client are skipped.
    Wrapping is idempotent: calling ``wrap()`` again on the same client neither
    stacks enforcement nor breaks the call.

    One form of one call is deliberately let through: a method that RESUMES a
    result instead of creating one — ``responses.stream(response_id=…)``, which
    replays a stored response over a ``GET`` — creates nothing to consent to, so
    it needs no identity and gets no token and no ledger entry. It is not a way
    out: the call carries no ACT token, so the transport guard refuses it if it
    turns out to carry a body.

    The caller names the end user on every AI call with exactly ONE of two
    kwargs; both are consumed by the SDK and never forwarded to the provider:

    - ``ubt="eyJ..."`` — use this when your frontend runs the ID Side
      Preference Center widget. The widget hands your frontend a **User
      Binding Token** (a JWT signed by ID Side binding user, app, session and
      consent state); forward it to your backend and pass it here. The SDK
      treats it as opaque; the cloud verifies it, defeating identity spoofing
      (the proof records ``binding="ubt"`` — the strong link).
    - ``user_id="usr_..."`` — use this for backend-only integrations without
      the widget. You assert the identity yourself; the proof records the
      weaker ``binding="user_id"``.

    Passing both raises ``ConflictingIdentityError``; passing neither raises
    ``MissingIdentityError`` — in both cases no cloud query and no AI call is
    made.

    A call is also refused, before the cloud is asked, when it carries an option
    that would make the request sent differ from the request proven: a body or
    query rewrite (``extra_body``, ``extra_query``), a per-call destination
    (``server_url``, ``http_options.base_url``), a header choosing the credential
    or carrying the ACT token, or a Gemini tool the library would call on its
    own. That is ``UnenforceableOptionError``; timeouts, retry policies and
    ordinary headers pass. The client is held to the origin it had when it was
    wrapped, and an authorized request never follows a redirect. Deriving a
    client (``with_options()``, ``copy()``, ``with_middleware()``) is a closed
    list as well: a new credential raises ``CredentialRoutingError``, a new
    transport, query, middleware or unclassified option
    ``UnsupportedProviderError``, and the derived client must keep the provider,
    the origin and the credential headers of the one it came from.

    ``credentials`` is how an account-level privacy setting becomes a per-user
    one. No provider exposes training opt-out as a request parameter: the
    posture lives on the credential — a project at OpenAI and Gemini, a
    workspace at Anthropic, an organisation at Mistral — so one key imposes one
    posture on every user of the app. Provision one credential per posture,
    label each with the posture it carries, and the SDK sends each call on the
    one this user's answer requires::

        client = consent.wrap(
            OpenAI(),
            credentials={
                "default": "proj_medhelp_default",     # OpenAI-Project header
                "zero_retention": "proj_medhelp_zdr",
            },
        )

        client = consent.wrap(
            Anthropic(),                               # no such header exists
            credentials={"no_training": Anthropic(api_key=PRIVATE_WORKSPACE_KEY)},
        )

    A value is either an identifier for the provider's per-request credential
    header (only OpenAI validates one, ``OpenAI-Project``) or a second client of
    the same kind built with that credential's key. **You label; you do not
    choose**: which posture a given consent state requires is policy, the cloud
    answers it, and there is no callback to override it — that would hand the
    developer the user's preferences (invariant #1). If the cloud names a
    posture nothing carries, the call raises ``MissingCredentialError`` and
    never goes out on another credential.

    Args:
        client: a supported provider client instance (e.g. ``OpenAI()``).
        consent_client: override the cloud transport (mainly for testing). It
            has to be a shape this client can wait for: a blocking provider
            client is enforced on the calling thread and can await nothing, so
            an async transport there is a ``TypeError`` at wrap time rather than
            an ``AttributeError`` on a coroutine at the first AI call. The
            reverse is fine — the async path takes a plain callable as it is.
        adapter: override provider auto-detection.
        base_url: override the ID Side API base URL (else the ``IDSIDE_BASE_URL``
            environment variable, else the default).
        api_key: ID Side API key for the cloud transport (else the
            ``IDSIDE_API_KEY`` environment variable).
        credentials: posture label → credential, for per-user credential
            routing. Misconfiguration raises ``CredentialRoutingError`` here,
            at wrap time, rather than on the request path.

    Returns:
        The same client, with its AI call method wrapped for consent enforcement.
    """
    return _wrap(
        client,
        consent_client=consent_client,
        adapter=adapter,
        base_url=base_url,
        api_key=api_key,
        credentials=credentials,
        lineage=None,
    )


def _wrap(
    client: Any,
    *,
    consent_client: AnyConsentClient | None,
    adapter: ProviderAdapter | None,
    base_url: str | None,
    api_key: str | None,
    credentials: Mapping[str, Any] | None,
    lineage: _Lineage | None,
) -> Any:
    """``wrap()``, plus the lineage of a derived client. See :func:`wrap`."""
    resolved_adapter = adapter if adapter is not None else get_adapter_for(client)
    call_sites = resolved_adapter.get_call_sites(client)

    # What shape of client is this? Asked once, from the methods the descriptor
    # declares, before anything is patched — an `AsyncOpenAI` answers yes because
    # `chat.completions.create` is a coroutine function. It decides two things
    # that must agree: which transport to the cloud is built, and which wrapper
    # each call site gets.
    methods = [getattr(target, name) for target, name, _site in call_sites]
    # A repeated wrap must be inert all the way down to the transport guard. Its
    # private provenance list otherwise retains one unusable closure per call.
    # The completion bit is set only after call sites and derived-client methods
    # have all been patched, so a failed construction is never mistaken for one.
    # `methods` is never empty — `get_call_sites()` raises first — and the
    # emptiness is named anyway: `all(())` is True, and the one thing this
    # branch must never do is hand back an unwrapped client.
    if methods and all(_is_completely_wrapped(method) for method in methods):
        return client
    is_async = any(_is_async_call_site(method) for method in methods)
    descriptor = resolved_adapter.descriptor

    # Where this client sends, checked before anything is patched. A derived
    # client is held to its parent's facts and inherits the frozen origin; any
    # other client has its own read off the attribute its library builds URLs
    # from. A client that is the provider's own library object must name one:
    # without it the net could only check the token, and a token that matches
    # is exactly what a request sent elsewhere carries.
    if lineage is not None:
        _refuse_diverging_derivation(client, resolved_adapter, lineage)
    refuse_declared_rewrites(client, descriptor)
    # A client named in `credentials` sends the calls routed to it, so a rewrite
    # declared on it rewrites those calls exactly as one declared on `client`.
    # Anything but a mapping is the router's to refuse, with its own message.
    alternatives = credentials.values() if isinstance(credentials, Mapping) else ()
    for alternative in alternatives:
        if not isinstance(alternative, str):
            refuse_declared_rewrites(alternative, descriptor)
    unguarded = _destination.unguarded_route(client)
    if unguarded is not None:
        raise UnsupportedProviderError(
            f"This '{resolved_adapter.name}' client cannot be wrapped: {unguarded}. "
            "The transport guard only sees requests sent on an HTTP client the "
            "client holds, so a route outside one would reach the provider with "
            "nothing to stop it. The client was not wrapped."
        )
    # Inherited by a derived client, never re-established on one — the same rule
    # as the origin just below, and for the same reason. A derivation sends on
    # its parent's transport and cannot bring another (`http_client=` is refused
    # at the derivation), so whether that transport will take
    # `follow_redirects` cannot have changed since the parent was wrapped.
    # Walking the object graph again would put a walk per `with_options()` on the
    # request path of an application that derives a client per call, to
    # re-establish something already known.
    unredirectable = _guard.unredirectable_route(client) if lineage is None else None
    if unredirectable is not None:
        raise UnsupportedProviderError(
            f"This '{resolved_adapter.name}' client cannot be wrapped: "
            f"{unredirectable}. A 307 or 308 is followed below the guard, where "
            "nothing sees the second hop, and it carries the prompt, the ACT token "
            "and the provider key to whatever host the redirect names. Sending "
            "without the keyword is that risk; sending with it would fail the call "
            "after ID Side had authorized it and recorded the proof. The client "
            "was not wrapped: give it an HTTP client of a supported kind "
            "(http_client=, client=, or http_options.httpx_client=), or report "
            "its transport shape."
        )
    is_library_client = _is_library_client(client, descriptor)
    origin = (
        lineage.origin if lineage is not None else _destination.client_origin(client)
    )
    if origin is None and is_library_client:
        raise UnsupportedProviderError(
            f"The '{resolved_adapter.name}' client does not say where it sends "
            "requests, so the transport guard could not hold its calls to that "
            "destination. The client was not wrapped; use a supported client "
            "version or report its shape."
        )

    # Resolved before anything is patched: an alternative client may be the
    # wrapped one itself (`{"default": client}` is the honest way to say what
    # the base key is), and capturing its method now is what stops a call routed
    # back to it from re-entering enforcement.
    router = (
        CredentialRouter(
            resolved_adapter.descriptor,
            credentials,
            [(site.id, (*site.path[:-1], method)) for _t, method, site in call_sites],
            is_enforced=_is_enforced,
        )
        if credentials
        else None
    )
    if credentials and any(isinstance(c, str) for c in credentials.values()):
        _refuse_routing_header_in_defaults(client, descriptor)
    # A derived client routes to its parent's clients, through the nets its
    # parent already put on them; only a first wrap has clients to check.
    routed = (
        _routed_clients(client, credentials, resolved_adapter)
        if credentials and lineage is None
        else []
    )

    resolved_client: AnyConsentClient
    if consent_client is not None:
        _assert_consent_client_fits(consent_client, is_async)
        resolved_client = consent_client
    elif is_async:
        # An ASGI application serves every other request in flight on the same
        # event loop. A blocking hop to the cloud once per AI call would stall
        # all of them, which is worse than the gap async support closes.
        resolved_client = AsyncHttpConsentClient(base_url, api_key=api_key)
    else:
        # base_url/api_key may be None here; HttpConsentClient resolves them
        # from IDSIDE_BASE_URL / IDSIDE_API_KEY, else its defaults.
        resolved_client = HttpConsentClient(base_url, api_key=api_key)

    # Require a complete net. Run this before patching call sites so a
    # construction failure leaves them untouched.
    transport_scope = _TransportScope()
    wrap_state = _WrapState()
    undo: list[Callable[[], None]] = []
    guarded = _guard.install(
        client, descriptor, _transport_provenance(transport_scope, origin), undo=undo
    )
    if guarded == "partial":
        raise UnsupportedProviderError(
            f"The '{resolved_adapter.name}' client exposes an HTTP transport, but "
            "the transport guard could not cover every route. The client was not "
            "wrapped; use a supported client version or report its transport shape."
        )
    # `False` means the search finished and found no transport. On the
    # provider's own client that does not mean "nothing to guard": every
    # supported library sends on an HTTP client it holds, so finding none means
    # a version that builds it lazily or keeps it out of reach — every route
    # unguarded. An object of another shape wrapped with an explicit adapter (a
    # test double, an exotic provider) keeps the old answer: it holds no
    # transport to leak through.
    if guarded is False and is_library_client:
        raise UnsupportedProviderError(
            f"No HTTP transport was found on this '{resolved_adapter.name}' "
            "client, so the transport guard could not be installed and nothing "
            "would stop a request the enforced methods do not see. The client was "
            "not wrapped; pass the client an explicit HTTP client (http_client=, "
            "client=, or http_options.httpx_client=), or report its shape."
        )
    route_scopes = (
        lineage.route_scopes
        if lineage is not None
        else _guard_routed_clients(routed, descriptor, undo)
    )

    for original, (target, method_name, site) in zip(methods, call_sites, strict=True):
        if getattr(original, _ENFORCED_MARKER, False):
            continue  # already enforced: wrap() is idempotent
        enforcer = _Enforcer(
            resolved_adapter,
            site.id,
            (*site.path[:-1], method_name),
            router,
            site.resuming_params,
            transport_scope,
            route_scopes,
        )
        wrapped = _wrapper_for(
            original,
            enforcer,
            resolved_client,
            is_async,
            defers_request=(
                method_name in site.aliases and method_name in _DEFERRED_ALIASES
            ),
        )
        setattr(wrapped, _ENFORCED_MARKER, wrap_state)
        setattr(target, method_name, wrapped)

    # A derived client is a new object: its call sites are unpatched, and it
    # talks to the same provider. Re-wrap it on the way out — with the
    # credentials as they are now: a client put into the caller's mapping after
    # this line was never checked, and no net was put on it.
    _keep_derived_enforced(
        client,
        resolved_adapter,
        resolved_client,
        dict(credentials) if credentials else credentials,
        _Lineage(
            provider=(
                lineage.provider if lineage is not None else _detected_provider(client)
            ),
            origin=origin,
            reserved_headers=_reserved_default_headers(client, descriptor),
            route_scopes=route_scopes,
        ),
    )
    # Existing methods can belong to an earlier interrupted attempt. A successful
    # retry completes those states too: their scope predicate is still installed,
    # while this attempt covered every remaining site and derived-client method.
    for target, method_name, _site in call_sites:
        state = getattr(getattr(target, method_name), _ENFORCED_MARKER, None)
        if isinstance(state, _WrapState):
            state.complete = True

    # Half the support questions a wrapped client ever produces are "is it even
    # being enforced?". This answers all of them at once: which descriptor
    # matched, how many entry points were patched, which shape of client, and
    # what the net came to: `true` means every supported route was guarded;
    # `false` means the client exposed none. A partial result was already
    # refused above. Posture labels are app configuration, so they
    # can be named; the credentials behind them never appear.
    _log.event(
        "wrap",
        provider=resolved_adapter.name,
        call_sites=len(call_sites),
        mode="async" if is_async else "blocking",
        net=guarded,
        postures=sorted(router.postures) if router is not None else None,
    )
    return client


def _assert_consent_client_fits(
    consent_client: AnyConsentClient, is_async: bool
) -> None:
    """Refuse a cloud transport this client's shape has no way to wait for.

    A blocking wrapper cannot await anything: handed an async ``authorize()`` it
    passes the coroutine on as if it were a decision, and the developer gets an
    ``AttributeError`` on a coroutine object, plus a "coroutine was never
    awaited" warning raised somewhere else entirely. Fail-closed — no AI call
    goes out — and undiagnosable, which is worth fixing on its own.

    Checked at ``wrap()``, because that is the line that got it wrong and
    because both facts are already in hand here. Only this direction is refused:
    a *blocking* transport on an async client is a documented, working
    combination (see ``_awaited``) — a test double or an application's own
    implementation of the protocol is a plain callable more often than not.
    """
    if is_async or not _is_async_call_site(getattr(consent_client, "authorize", None)):
        return
    raise TypeError(
        "consent.wrap() was given an async consent_client for a blocking "
        "provider client. A blocking client is enforced on the calling thread "
        "and has nothing to await the cloud with, so this would fail on the "
        "first AI call, in a place that does not name the mistake. Pass a "
        "blocking consent client (HttpConsentClient, or a synchronous double), "
        "or wrap the provider's async client (AsyncOpenAI, AsyncAnthropic) — "
        "wrap() then builds the async transport to the cloud itself."
    )


def _is_library_client(client: Any, descriptor: ProviderDescriptor) -> bool:
    """Whether ``client`` is an object of the provider's own client library.

    The same test auto-detection makes, by module: ``openai._client.OpenAI``
    belongs to ``openai``, ``google.genai.client.Client`` to ``google.genai``.
    """
    module = type(client).__module__
    library = descriptor.client_module
    return module == library or module.startswith(library + ".")


def _detected_provider(client: Any) -> str | None:
    """The provider auto-detection names for ``client``, or None if it names none."""
    try:
        return get_adapter_for(client).name
    except UnsupportedProviderError:
        return None


def _reserved_default_headers(
    client: Any, descriptor: ProviderDescriptor
) -> tuple[tuple[str, Any], ...]:
    """The reserved headers among those the client was built with.

    Stainless clients keep the ``default_headers=`` they were given in
    ``_custom_headers``, and merge it into every request with a case-sensitive
    dict — so ``openai-project`` there sits beside a routed ``OpenAI-Project``
    as a second header on the wire rather than giving way to it.
    """
    state = getattr(client, "__dict__", None)
    custom = state.get("_custom_headers") if isinstance(state, dict) else None
    if not isinstance(custom, Mapping):
        return ()
    return tuple(
        (name, value)
        for name, value in custom.items()
        if isinstance(name, str) and descriptor.is_reserved_header(name)
    )


def _refuse_routing_header_in_defaults(
    client: Any, descriptor: ProviderDescriptor
) -> None:
    """Refuse a client built with the header credential routing has to set.

    Routing by header adds ``OpenAI-Project`` to each call; the Stainless
    clients merge that with ``default_headers=`` case-sensitively, so an
    ``openai-project`` given at construction left two projects on one request.
    Refused in every spelling, because routing is then the only thing that may
    name the project: the unrouted default belongs in ``credentials=`` too.
    """
    routing = descriptor.credential_routing
    header = routing.header.lower() if routing and routing.header else None
    for name, _value in _reserved_default_headers(client, descriptor):
        if name.lower() == header:
            raise CredentialRoutingError(
                f"This client was built with `{name}` in its default headers, and "
                "consent.wrap(credentials=…) sets that header on every routed "
                "call: the request would carry two, and the provider would choose. "
                "Build the client without it and name that credential in "
                "credentials= instead (the label 'default' is the usual one)."
            )


def _routed_clients(
    client: Any, credentials: Mapping[str, Any], adapter: ProviderAdapter
) -> list[_RoutedClient]:
    """The clients named in ``credentials``, each checked as ``client`` was.

    A call routed to one of them goes out on it, so it is held to what the
    wrapped client is held to — found here, before anything is patched: a route
    outside any HTTP client the net can see, and, for the provider's own client,
    no origin to freeze. Its origin is its own, not the wrapped client's: a
    residency posture may live on another host (``eu.api.openai.com``).

    ``client`` itself is left out, since its net is the wrapped client's. So is
    a bare identifier, which only adds a header to a call ``client`` sends.
    """
    named: dict[int, tuple[list[str], Any]] = {}
    for label, credential in credentials.items():
        if isinstance(credential, str) or credential is client:
            continue
        named.setdefault(id(credential), ([], credential))[0].append(label)
    routed: list[_RoutedClient] = []
    for labels, credential in named.values():
        unguarded = _destination.unguarded_route(credential)
        if unguarded is not None:
            raise UnsupportedProviderError(
                f"The client passed as credential '{labels[0]}' cannot carry "
                f"routed calls: {unguarded}. The transport guard only sees requests "
                "sent on an HTTP client the client holds, so a call routed to it "
                "would reach the provider with nothing to stop it. The client was "
                "not wrapped."
            )
        unredirectable = _guard.unredirectable_route(credential)
        if unredirectable is not None:
            raise UnsupportedProviderError(
                f"The client passed as credential '{labels[0]}' cannot carry "
                f"routed calls: {unredirectable}. A call routed to it is an "
                "authorized one leaving on its transport, so it is held to what the "
                "wrapped client is held to: a redirect below the guard would carry "
                "the prompt, the ACT token and that credential's key to a host "
                f"nobody wrapped. The '{adapter.name}' client was not wrapped; give "
                "that client an HTTP client of a supported kind, or report its "
                "transport shape."
            )
        origin = _destination.client_origin(credential)
        if origin is None and _is_library_client(credential, adapter.descriptor):
            raise UnsupportedProviderError(
                f"The client passed as credential '{labels[0]}' does not say where "
                "it sends requests, so the transport guard could not hold the calls "
                f"routed to it to that destination. The '{adapter.name}' client was "
                "not wrapped; use a supported client version or report its shape."
            )
        routed.append(_RoutedClient(tuple(labels), credential, origin))
    return routed


def _guard_routed_clients(
    routed: list[_RoutedClient],
    descriptor: ProviderDescriptor,
    undo: list[Callable[[], None]],
) -> dict[str, _TransportScope]:
    """Put the net on every routed client, or undo this wrap's nets and refuse.

    Each client gets a scope of its own, and a routed call's permit is minted
    for that scope: on a transport two clients share (``copy()`` shares it), the
    wrapped client's calls are then held to its origin and the routed client's
    to its own, never to either. The criterion is the wrapped client's — a route
    the walk could not finish, a transport that refused the patch, or no
    transport at all on the provider's own client — and so is the outcome:
    nothing this wrap patched stays patched.
    """
    scopes: dict[str, _TransportScope] = {}
    for labels, routed_client, origin in routed:
        scope = _TransportScope()
        net = _guard.install(
            routed_client,
            descriptor,
            _transport_provenance(scope, origin),
            undo=undo,
        )
        if net == "partial" or (
            net is False and _is_library_client(routed_client, descriptor)
        ):
            for step in reversed(undo):
                step()
            raise UnsupportedProviderError(
                f"The transport guard could not cover every route of the client "
                f"passed as credential '{labels[0]}', so a call routed to it could "
                "reach the provider with nothing to stop it. The client was not "
                "wrapped; pass that client an explicit HTTP client (http_client=, "
                "client=, or http_options.httpx_client=), or report its shape."
            )
        scopes.update(dict.fromkeys(labels, scope))
    return scopes


def _refuse_diverging_derivation(
    derived: Any, adapter: ProviderAdapter, lineage: _Lineage
) -> None:
    """Hold a derived client to what its parent was wrapped as, or refuse it.

    Raised at the derivation, before the derived client exists for the caller
    and before anything is sent — the line that changed the destination is the
    line that fails. ``UnsupportedProviderError`` rather than a routing error:
    a client bound for another origin, another provider or another credential
    is not the client ``wrap()`` agreed to enforce, which is what that error
    means everywhere else, while ``CredentialRoutingError`` describes a
    ``credentials=`` mapping and would send its reader to the wrong place.
    """
    name = adapter.name
    if lineage.provider is not None:
        detected = _detected_provider(derived)
        if detected != lineage.provider:
            became = f"identified as '{detected}'" if detected else "not identifiable"
            raise UnsupportedProviderError(
                f"A client derived from a consent-enforced '{lineage.provider}' "
                f"client is {became}. The provider tells ID Side whose rules apply "
                "to the call; derive only clients of the same provider, and wrap a "
                "client of another one on its own."
            )
    if lineage.origin is not None and (
        _destination.client_origin(derived) != lineage.origin
    ):
        raise UnsupportedProviderError(
            f"A client derived from a consent-enforced '{name}' client must send "
            f"to the same origin ({lineage.origin}), and this one does "
            "not: its calls would carry the prompt, the ACT token and the key "
            "somewhere nobody wrapped. Build and wrap a separate client for "
            "another destination."
        )
    if _reserved_default_headers(derived, adapter.descriptor) != (
        lineage.reserved_headers
    ):
        raise CredentialRoutingError(
            f"A client derived from a consent-enforced '{name}' client must keep "
            "the credential headers it was built with — none added, none replaced, "
            "none dropped: they choose the credential or carry the consent token, "
            "and credential routing is what sets them. Build the client with the "
            "headers it needs, or route with consent.wrap(client, credentials={…})."
        )


def _keep_derived_enforced(
    client: Any,
    adapter: ProviderAdapter,
    consent_client: AnyConsentClient,
    credentials: Mapping[str, Any] | None,
    lineage: _Lineage,
) -> None:
    """Make ``with_options()``/``copy()`` hand back an enforced client.

    The derived client is wrapped with the *same* adapter and consent client,
    and its own deriving methods are patched by the same code path, so a chain
    of them stays enforced all the way down. ``credentials`` is re-resolved
    against the new client rather than carried over: an alternative client is a
    separate object the derivation never touched, and re-checking it costs a few
    attribute lookups at derive time instead of a silently stale route.
    """
    for name in _DERIVING_METHODS:
        derive = getattr(client, name, None)
        if not callable(derive) or getattr(derive, _ENFORCED_MARKER, False):
            continue
        setattr(
            client,
            name,
            _make_deriving(
                client, derive, name, adapter, consent_client, credentials, lineage
            ),
        )


def _make_deriving(
    client: Any,
    original: Callable[..., Any],
    method: str,
    adapter: ProviderAdapter,
    consent_client: AnyConsentClient,
    credentials: Mapping[str, Any] | None,
    lineage: _Lineage,
) -> Callable[..., Any]:
    """Wrap a client-deriving method so its result comes back enforced.

    The arguments are judged before the library builds the derived client, and
    the derived client against its parent once it exists; either refusal
    happens before anything is sent. The parent goes to the argument check
    because one option — middleware — is a *change* to what the parent runs, and
    can only be judged against it.
    """

    @functools.wraps(original)
    def _deriving(*args: Any, **kwargs: Any) -> Any:
        refuse_underivable(adapter.descriptor, method, args, kwargs, client)
        return _wrap(
            original(*args, **kwargs),
            consent_client=consent_client,
            adapter=adapter,
            base_url=None,
            api_key=None,
            credentials=credentials,
            lineage=lineage,
        )

    setattr(_deriving, _ENFORCED_MARKER, True)
    return _deriving


def _resolve_route(
    router: CredentialRouter | None,
    posture: str | None,
    adapter: ProviderAdapter,
    route_key: tuple[str, ...],
) -> Route:
    """Find the credential this call must go out on, or refuse to make it.

    ``posture`` is ``None`` for almost every call: the cloud names one only when
    the user's answer requires a credential other than the one the client was
    built with.
    """
    if posture is None:
        return NO_ROUTE
    if router is None:
        raise MissingCredentialError(
            missing_message(adapter.descriptor, posture, ()),
            posture=posture,
            provider=adapter.name,
        )
    return router.route(posture, route_key)


def _takes_call_site(apply_constraints: Callable[..., Any]) -> bool:
    """Whether an adapter's ``apply_constraints`` accepts the ``call_site`` keyword.

    ``call_site`` was added after custom adapters were documented as
    ``apply_constraints(kwargs, constraints)``. Passing it to one of those raised
    ``TypeError`` after the cloud had authorized the call — a proof in the ledger
    for a call that was never made. Such an adapter is called as it was written,
    and reads what it always read.
    """
    try:
        parameters = inspect.signature(apply_constraints).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "call_site" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _assert_routed(
    adapter: ProviderAdapter, kwargs: dict[str, Any], headers: dict[str, str]
) -> None:
    """Refuse to call the provider if the routed credential did not land.

    The adapter is what places headers on a call, and a custom one is free to
    override that. Verifying rather than trusting costs a dict lookup and turns
    the one failure that would be invisible — a call leaving on the credential
    routing was meant to move it off — into an error.
    """
    applied = kwargs.get(adapter.descriptor.headers_kwarg)
    if isinstance(applied, Mapping) and all(
        applied.get(name) == value for name, value in headers.items()
    ):
        return
    raise CredentialRoutingError(
        f"The '{adapter.name}' adapter did not carry the routed credential onto "
        "this call, so the AI call was not made. An adapter overriding apply() "
        "must forward its credential_headers argument."
    )


class _Enforcer:
    """Everything a patched method does around the one question put to the cloud.

    The blocking wrapper, the async one and the deferred one differ by where the
    ``await`` goes and by nothing else. What surrounds it — consuming the
    identity, hashing the request, honouring the constraints, routing the
    credential, injecting the forced parameters and the token — lives here, once.
    That is not tidiness: a parameter one path forced and another forgot would be
    a user's refusal silently not applied on whichever client shape they happened
    to be served by, and neither wrapper could show it on its own.

    ``call_site`` is the endpoint's stable id, closed over per patched method. It
    travels to the cloud on every authorize so the decision is made for the
    endpoint actually being called: ``background`` is a retention lever on
    ``/v1/responses`` and a TypeError on ``chat.completions``.

    ``route_key`` is the attribute path of *this* method, aliases included. It is
    what credential routing looks a call up by, because two methods sharing a
    wire id are still two different methods: a call made on ``messages.parse``
    must land on the alternative client's ``parse``, not on its ``create``.

    ``router`` holds the developer's labelled credentials, when they provisioned
    several. It decides *which* credential carries the call; the cloud decides
    which posture that has to be.

    ``resuming_params`` are the keyword arguments that mean this call resumes a
    result instead of creating one — the descriptor's answer for an overloaded
    method, and empty for every site that has only one form.

    ``route_scopes`` name, by posture label, the net of each client a call can
    be routed to. A routed call is sent by that client, on its transport, so its
    permit is minted for that client's scope and not for ``transport_scope``.
    """

    __slots__ = (
        "adapter",
        "call_site",
        "passes_call_site",
        "resuming_params",
        "route_key",
        "route_scopes",
        "router",
        "transport_scope",
    )

    def __init__(
        self,
        adapter: ProviderAdapter,
        call_site: str,
        route_key: tuple[str, ...],
        router: CredentialRouter | None,
        resuming_params: tuple[str, ...] = (),
        transport_scope: object | None = None,
        route_scopes: Mapping[str, object] | None = None,
    ) -> None:
        self.adapter = adapter
        self.call_site = call_site
        # Read once: the adapter is fixed for the life of this wrapper, and
        # `inspect.signature` on every AI call is work repeated for nothing.
        self.passes_call_site = _takes_call_site(adapter.apply_constraints)
        self.route_key = route_key
        self.router = router
        self.resuming_params = resuming_params
        self.transport_scope = (
            transport_scope if transport_scope is not None else object()
        )
        self.route_scopes = route_scopes or {}

    def resumes(self, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        """The kwargs to pass straight through, or ``None`` for a real call.

        A client method can be overloaded: ``responses.stream(response_id=…)``
        replays a response that already exists over ``GET /v1/responses/{id}``,
        where ``responses.stream(model=…, input=…)`` creates one over POST. The
        resuming form carries no prompt and no model. Enforcing it demanded an
        identity for a call that creates nothing, authorized with an empty model
        — and signed a token asserting an AI call that never happened.

        So it is let through, and letting it through is safe rather than
        trusted: nothing here decides that no request will be made, only that
        this SDK will not certify one. A call passed through carries no ACT
        token, so if it turns out to be body-bearing after all, the net refuses
        it before it leaves the process. The identity arguments are consumed as
        they are everywhere else — the provider has never heard of them.
        """
        marker = next(
            (name for name in self.resuming_params if kwargs.get(name) is not None),
            None,
        )
        if marker is None:
            return None
        descriptor = self.adapter.descriptor
        # Said out loud: a provider call with no authorize beside it is exactly
        # what a leak looks like in a journal, and this one is not a leak.
        _log.event("resume", call_site=self.call_site, marker=marker)
        return {
            key: value
            for key, value in kwargs.items()
            if key not in (descriptor.identity_param, descriptor.ubt_param)
        }

    def is_internal_call(self, kwargs: dict[str, Any]) -> bool:
        """True only for a nested hop of the currently authorized call.

        An alias may defer to a method this SDK also patched
        (``messages.parse`` calls ``messages.create``). The public header cannot
        prove that happened. The outer wrapper creates a one-shot context-local
        permit after authorization and keeps it active across awaited and
        deferred provider operations. Matching the call site and token binds the
        nested hop to that exact call.
        """
        carried = kwargs.get(self.adapter.descriptor.headers_kwarg)
        token = (
            carried.get(self.adapter.descriptor.token_header)
            if isinstance(carried, Mapping)
            else None
        )
        permit = _INTERNAL_CALL.get()
        internal = permit is not None and permit.consume(
            self.transport_scope, self.call_site, token
        )
        if internal:
            # Said out loud, because the alternative is a journal in which one
            # provider call has no authorize next to it and looks like the leak
            # this SDK exists to prevent.
            _log.event("passthrough", call_site=self.call_site)
        return internal

    @contextmanager
    def internal_call(self, decision: ConsentDecision) -> Any:
        """Scope a one-shot nested-hop permit to this provider invocation.

        Yields the permit so a lazily produced result can keep it in force for
        the I/O it has not done yet — see :func:`_lazily_permitted`.

        The permit belongs to the client that sends the call: the one routing
        named when a posture is carried by another client, the wrapped one
        otherwise — a header route included, since that call leaves on it.
        """
        posture = decision.credential_posture
        scope = (
            self.transport_scope
            if posture is None
            else self.route_scopes.get(posture, self.transport_scope)
        )
        permit = _InternalCall(self.call_site, decision.act_token, scope)
        context_token = _INTERNAL_CALL.set(permit)
        try:
            yield permit
        finally:
            _INTERNAL_CALL.reset(context_token)

    def begin(self, kwargs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Consume the identity and hash the request; ask nothing of anybody.

        Pure on purpose. It runs before the cloud is reached on every path, so a
        call that names no user — or two — fails where the mistake was made,
        including on a streaming helper whose real request only happens later.
        So does a call carrying an option that would undo enforcement once the
        cloud has answered (``extra_body``, a per-call destination, a header
        choosing the credential): asking first would put a proof in the ledger
        for a request that was never going to be the one sent. Checked before
        the hash, too, since some of those options cannot be hashed at all.

        Returns the arguments for ``authorize()`` and the call kwargs that
        survive, the identity removed.
        """
        descriptor = self.adapter.descriptor
        remaining = dict(kwargs)
        user_id = remaining.pop(descriptor.identity_param, None)
        ubt = remaining.pop(descriptor.ubt_param, None)
        # Blank names nobody, as empty already did; nothing else is trimmed.
        user_id = user_id if _named(user_id) else None
        ubt = ubt if _named(ubt) else None
        if user_id and ubt:
            raise ConflictingIdentityError(
                "Pass either user_id or ubt on a consent-enforced AI call, "
                "not both: the UBT already names the user."
            )
        if not user_id and not ubt:
            raise MissingIdentityError(
                "user_id or ubt is required for consent-enforced AI calls."
            )
        refuse_unenforceable(descriptor, self.call_site, remaining)
        meta = self.adapter.extract_request_metadata(**remaining)
        authorize_kwargs = {
            "user_id": user_id,
            "ubt": ubt,
            "provider": self.adapter.name,
            "model": meta["model"],
            "request_hash": meta["request_hash"],
            "call_site": self.call_site,
            "credential_postures": (
                self.router.postures if self.router is not None else ()
            ),
        }
        return authorize_kwargs, remaining

    def journal_authorized(
        self, authorize_kwargs: dict[str, Any], decision: ConsentDecision, ms: int
    ) -> None:
        """Say what the cloud answered, in terms that name nobody.

        Every field here is either a fact about the developer's own request or
        already a digest. The subject is a salted pseudonym, the request hash is
        truncated, and three things the decision carries are deliberately
        absent: the ACT token (bearer material), the raw identity, and
        ``consent_state`` — the person's stated preference, which answers no
        debugging question that ``forced`` does not answer better.
        """
        _log.event(
            "authorize",
            call_site=self.call_site,
            model=authorize_kwargs["model"],
            subject=_log.pseudonym(
                authorize_kwargs["ubt"] or authorize_kwargs["user_id"]
            ),
            binding="ubt" if authorize_kwargs["ubt"] else "user_id",
            request_hash=_log.short_hash(authorize_kwargs["request_hash"]),
            duration_ms=ms,
            forced=sorted(decision.params_forced),
            constraints=sorted(decision.constraints) or None,
            posture=decision.credential_posture,
        )

    def journal_authorize_failed(
        self, authorize_kwargs: dict[str, Any], exc: BaseException, ms: int
    ) -> None:
        """The class of what went wrong and how long it took, never its message.

        An exception message is written for a human and can quote whatever it
        was handed — a URL with a query string, a header, the body it failed to
        parse. The type is the part that is safe to emit and the part that
        answers "is this a config problem or an outage?"; the message reaches
        the developer anyway, on the exception they are about to catch.
        """
        _log.event(
            "authorize_failed",
            call_site=self.call_site,
            model=authorize_kwargs["model"],
            subject=_log.pseudonym(
                authorize_kwargs["ubt"] or authorize_kwargs["user_id"]
            ),
            duration_ms=ms,
            error=type(exc).__name__,
        )

    def finish(
        self, kwargs: dict[str, Any], decision: ConsentDecision
    ) -> tuple[Callable[..., Any] | None, dict[str, Any]]:
        """Apply the cloud's answer, and say which callable must make the call.

        Returns the alternative client's method when credential routing named
        one — the same endpoint on the credential's own client, for the providers
        where a key is fixed at construction and there is no header to switch it
        — or ``None``, meaning the method that was patched.
        """
        adapter = self.adapter
        try:
            # Constraints first: a refused call must not be dressed with a token
            # and forced parameters on its way to being rejected.
            allowed_kwargs = (
                adapter.apply_constraints(
                    kwargs, decision.constraints, call_site=self.call_site
                )
                if self.passes_call_site
                else adapter.apply_constraints(kwargs, decision.constraints)
            )
            route = _resolve_route(
                self.router, decision.credential_posture, adapter, self.route_key
            )
        except Exception as exc:
            # A local refusal after a healthy authorize. Without this line the
            # journal shows an authorize that succeeded and then nothing at all,
            # which is the most confusing shape a journal can have. Every class,
            # not only `IDSideError`: the Node wrapper catches everything here,
            # and a journal that goes quiet on a TypeError from a provider's own
            # argument handling is the same confusing shape by another route.
            # `UnenforcedCallError` derives from BaseException and is raised by
            # the net, not from here, so it is deliberately out of reach.
            _log.event(
                "refused",
                call_site=self.call_site,
                error=type(exc).__name__,
                posture=decision.credential_posture,
            )
            raise
        enforced_kwargs = adapter.apply(
            allowed_kwargs,
            decision.params_forced,
            decision.act_token,
            credential_headers=route.headers,
        )
        if route.headers:
            _assert_routed(adapter, enforced_kwargs, route.headers)
        _log.event(
            "enforced",
            call_site=self.call_site,
            forced=sorted(decision.params_forced),
            routed=bool(route.headers or route.call),
        )
        return route.call, enforced_kwargs


def _wrapper_for(
    original: Callable[..., Any],
    enforcer: _Enforcer,
    consent_client: AnyConsentClient,
    is_async: bool,
    *,
    defers_request: bool,
) -> Callable[..., Any]:
    """Pick the enforcement wrapper this method's own shape can carry.

    Four shapes, and the choice is forced by what the method returns and when it
    sends, never by preference:

    - a coroutine function (``AsyncOpenAI.chat.completions.create``) takes the
      async wrapper;
    - anything else on an async client is a helper that hands back a context
      manager and defers the real request to ``__aenter__`` — every provider's
      ``stream()``. Wrapping it in a coroutine would break ``async with``, so
      the enforcement is deferred with it;
    - a helper on a BLOCKING client that defers its request the same way
      (``defers_request``, i.e. a ``stream()`` alias) is deferred the same way
      too. It reads like the eager case and is not one: the request happens in
      ``__enter__``, so that is where the proof belongs. The two client shapes
      would otherwise put a different number of entries in one customer's
      ledger for the same abandoned stream;
    - a plain method on a blocking client takes the blocking wrapper.
    """
    if _is_async_call_site(original):
        return _make_async_enforced(original, enforcer, consent_client)
    if is_async:
        return _make_deferred_enforced(
            original, enforcer, consent_client, _EnforcedAsyncContext
        )
    if defers_request:
        return _make_deferred_enforced(
            original, enforcer, consent_client, _EnforcedSyncContext
        )
    # A blocking client is only ever paired with a blocking transport to the
    # cloud: `wrap()` decides both from the same answer, and an async
    # `consent_client=` override is refused at wrap time.
    return _make_enforced(original, enforcer, cast(ConsentClient, consent_client))


def _make_enforced(
    original: Callable[..., Any],
    enforcer: _Enforcer,
    consent_client: ConsentClient,
) -> Callable[..., Any]:
    """The blocking wrapper: authorize, then call, on this thread."""

    @functools.wraps(original)
    def _enforced(**kwargs: Any) -> Any:
        if enforcer.is_internal_call(kwargs):
            return original(**kwargs)
        resumed = enforcer.resumes(kwargs)
        if resumed is not None:
            return original(**resumed)
        authorize_kwargs, call_kwargs = enforcer.begin(kwargs)
        try:
            with _log.timer() as elapsed:
                decision = consent_client.authorize(**authorize_kwargs)
        except BaseException as exc:
            enforcer.journal_authorize_failed(authorize_kwargs, exc, elapsed.ms)
            raise
        enforcer.journal_authorized(authorize_kwargs, decision, elapsed.ms)
        call, enforced_kwargs = enforcer.finish(call_kwargs, decision)
        with enforcer.internal_call(decision) as permit:
            return _lazily_permitted(permit, (call or original)(**enforced_kwargs))

    setattr(_enforced, _ENFORCED_MARKER, True)
    return _enforced


def _make_async_enforced(
    original: Callable[..., Any],
    enforcer: _Enforcer,
    consent_client: AnyConsentClient,
) -> Callable[..., Any]:
    """The async wrapper: the same steps, yielding the loop at the cloud hop.

    ``await`` on the provider call too, and not only on the cloud: the caller
    awaits this coroutine and must receive what the provider returned, not a
    coroutine of it.
    """

    @functools.wraps(original)
    async def _enforced(**kwargs: Any) -> Any:
        if enforcer.is_internal_call(kwargs):
            return await original(**kwargs)
        resumed = enforcer.resumes(kwargs)
        if resumed is not None:
            return await original(**resumed)
        authorize_kwargs, call_kwargs = enforcer.begin(kwargs)
        try:
            with _log.timer() as elapsed:
                decision = await _awaited(consent_client.authorize(**authorize_kwargs))
        except BaseException as exc:
            enforcer.journal_authorize_failed(authorize_kwargs, exc, elapsed.ms)
            raise
        enforcer.journal_authorized(authorize_kwargs, decision, elapsed.ms)
        call, enforced_kwargs = enforcer.finish(call_kwargs, decision)
        with enforcer.internal_call(decision) as permit:
            return _lazily_permitted(
                permit, await (call or original)(**enforced_kwargs)
            )

    setattr(_enforced, _ENFORCED_MARKER, True)
    return _enforced


def _make_deferred_enforced(
    original: Callable[..., Any],
    enforcer: _Enforcer,
    consent_client: AnyConsentClient,
    context: type[_DeferredEnforcement],
) -> Callable[..., Any]:
    """The wrapper for a helper that defers its request to a context manager.

    ``AsyncOpenAI.chat.completions.stream`` is not a coroutine function, and
    ``OpenAI.chat.completions.stream`` is not an ordinary method: both return a
    context manager, and the request happens when that manager is entered.
    Patching the async one with a coroutine would hand every caller a
    Promise-shaped object where ``async with`` expects a manager — the same
    reason the Node SDK does not patch ``stream`` at all.

    So the enforcement splits along the same seam the helper already has: the
    identity check and the request hash happen here, at the call, where a
    mistake belongs; the cloud is asked when the manager is entered, next to the
    request it authorizes. ``context`` is which of the two managers this client
    shape can carry, and it is the only thing that differs between them.
    """

    @functools.wraps(original)
    def _deferred(**kwargs: Any) -> Any:
        if enforcer.is_internal_call(kwargs):
            return original(**kwargs)
        resumed = enforcer.resumes(kwargs)
        if resumed is not None:
            # The provider's own manager, untouched: this form makes no AI call,
            # so there is nothing to defer and nothing to certify.
            return original(**resumed)
        authorize_kwargs, call_kwargs = enforcer.begin(kwargs)
        return context(
            original, enforcer, consent_client, authorize_kwargs, call_kwargs
        )

    setattr(_deferred, _ENFORCED_MARKER, True)
    return _deferred


class _DeferredEnforcement:
    """What a deferred helper carries from the call to the moment it is entered.

    Stands in for what ``stream()`` returns and delegates to it, once the cloud
    has answered. Entering is the only way to get anything out of it, which is
    also the only way the provider's own manager works: **a stream nobody
    entered made no request, so it needed no proof.**

    It is deliberately not a proxy: nothing else on it is forwarded to the
    provider's manager, so there is no route to the provider that does not go
    through the authorize below. The net on the transport is the other half of
    that guarantee — a request built any other way carries no ACT token and is
    refused before it leaves the process.
    """

    __slots__ = (
        "_authorize_kwargs",
        "_call_kwargs",
        "_consent_client",
        "_enforcer",
        "_manager",
        "_original",
    )

    def __init__(
        self,
        original: Callable[..., Any],
        enforcer: _Enforcer,
        consent_client: AnyConsentClient,
        authorize_kwargs: dict[str, Any],
        call_kwargs: dict[str, Any],
    ) -> None:
        self._original = original
        self._enforcer = enforcer
        self._consent_client = consent_client
        self._authorize_kwargs = authorize_kwargs
        self._call_kwargs = call_kwargs
        self._manager: Any = None

    def _prepare_open(
        self, decision: ConsentDecision
    ) -> tuple[Callable[..., Any], dict[str, Any]]:
        """Apply the cloud's answer and return the provider manager factory."""
        call, enforced_kwargs = self._enforcer.finish(self._call_kwargs, decision)
        return call or self._original, enforced_kwargs


class _EnforcedSyncContext(_DeferredEnforcement):
    """The blocking half: ``with client.chat.completions.stream(...) as s``."""

    __slots__ = ()

    def __enter__(self) -> Any:
        enforcer = self._enforcer
        client = cast(ConsentClient, self._consent_client)
        try:
            with _log.timer() as elapsed:
                decision = client.authorize(**self._authorize_kwargs)
        except BaseException as exc:
            enforcer.journal_authorize_failed(self._authorize_kwargs, exc, elapsed.ms)
            raise
        enforcer.journal_authorized(self._authorize_kwargs, decision, elapsed.ms)
        call, enforced_kwargs = self._prepare_open(decision)
        with enforcer.internal_call(decision) as permit:
            self._manager = call(**enforced_kwargs)
            return _lazily_permitted(permit, self._manager.__enter__())

    def __exit__(self, *exc_info: Any) -> Any:
        # Unreachable with a None manager: Python does not call __exit__ when
        # __enter__ raised, and nothing else can set it.
        return self._manager.__exit__(*exc_info)


class _EnforcedAsyncContext(_DeferredEnforcement):
    """The async half: ``async with client.chat.completions.stream(...) as s``."""

    __slots__ = ()

    async def __aenter__(self) -> Any:
        enforcer = self._enforcer
        try:
            with _log.timer() as elapsed:
                decision = await _awaited(
                    self._consent_client.authorize(**self._authorize_kwargs)
                )
        except BaseException as exc:
            enforcer.journal_authorize_failed(self._authorize_kwargs, exc, elapsed.ms)
            raise
        enforcer.journal_authorized(self._authorize_kwargs, decision, elapsed.ms)
        call, enforced_kwargs = self._prepare_open(decision)
        with enforcer.internal_call(decision) as permit:
            self._manager = call(**enforced_kwargs)
            return _lazily_permitted(permit, await self._manager.__aenter__())

    async def __aexit__(self, *exc_info: Any) -> Any:
        # Unreachable with a None manager: Python does not call __aexit__ when
        # __aenter__ raised, and nothing else can set it.
        return await self._manager.__aexit__(*exc_info)
