# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""The interception net: a blind guard on the provider client's HTTP transport.

Patching the methods a descriptor declares can never be exhaustive. A provider
ships a new endpoint, a helper posts to the API without going through the method
the SDK wrapped, a resource nobody thought to declare (``embeddings``,
``images``, ``audio``, ``moderations``, ``batches``, ``files``, ``beta.*``) sits
on the same client. Each of those reaches the provider with no authorize, no ACT
token and no ledger entry — and, worse than failing, does it silently.

So the last line is drawn where every request must pass whatever route it took:
the client's own HTTP transport. A body-bearing request passes only when its ACT
token matches the private authorization context for this wrapped client and the
same method/path (including retries and streaming continuation). Any other such
request is refused before it leaves the process.

Every transport the client owns, and recognised by shape rather than by name.
Two mistakes are possible here and both have been made. The first is counting
one transport: an HTTP client library splits blocking and async into two
unrelated classes — the async one is no subclass of the blocking one, and its
``send`` is a coroutine — and a single client may own both at once, as one
``google-genai`` ``Client`` does (``_httpx_client`` and ``_async_httpx_client``,
with the async surface at ``client.aio``). Stopping at the first match left the
other route wide open.

The second is asking which distribution shipped the class. ``isinstance`` against
``httpx``'s two classes answered that question, not the one that was meant, and
the answer changed underneath it: ``openai>=3`` requires ``httpx2``, a separate
distribution whose clients are not ``httpx`` clients and never will be. Under it
nothing matched, and the net reported that there had been nothing to guard about
a client holding a live route to the provider. A transport is now anything
carrying the shape of one — ``send``, ``build_request``, ``base_url``, read off
the class — whoever packaged it.

**This guard is a detector, not an enforcer, and it is deliberately narrow.** It
reads the request's HTTP method, the URL's path and origin (scheme, host, port)
and one named header, comparing them locally with private authorization
provenance: the token must be the one this wrapped client was just given, for
this route, bound for the origin the client was wrapped with. Never the body,
the query string or any other header. It hashes nothing, logs nothing and
sends nothing anywhere — it is on the AI request path of an application whose
whole premise is that we do not see the request.

An authorized request is also sent with ``follow_redirects=False``. The provider
libraries build their HTTP clients to follow redirects, and ``httpx`` follows
them below ``send``, where no guard runs: a 307 or 308 re-sent the body, the ACT
token and the API key to wherever it pointed, and the guard only ever saw the
first hop. A redirect now comes back to the caller as the provider's answer.

Which makes what a transport's ``send`` accepts a fact about whether the client
can be guarded at all, and ``unredirectable_route`` is where that is asked —
before ``wrap()`` patches anything. Recognising transports by shape says nothing
about their signature, and a transport of the right shape whose ``send`` will
not take the keyword left the only two outcomes that are both unacceptable:
sending anyway with redirects followed, or a ``TypeError`` raised *after* the
cloud authorized the call and appended the ledger — a proof of a request that
never left the process, failing where nothing names the cause, while every
bodyless call kept working.
"""

from __future__ import annotations

import functools
import inspect
import threading
from collections import deque
from collections.abc import Callable
from typing import Any, Literal, NamedTuple, Protocol

from idside._destination import Origin, origin_of
from idside.exceptions import UnenforcedCallError
from idside.providers import ProviderDescriptor

#: What a wrapped client's predicate is asked: the ACT header's value, the
#: method, the URL path, and the origin the request is bound for.
Provenance = Callable[[str, str, str, Origin | None], bool]

__all__ = ["install", "unredirectable_route"]

#: The one keyword the net adds to a send it lets through.
_NO_REDIRECTS = "follow_redirects"

#: Attribute a predicate may carry: a callable answering whether the client it
#: belongs to still exists. A predicate without one is taken to live forever.
ALIVE = "__idside_alive__"


#: Serialises every WRITE to a transport's predicate list. The list is shared by
#: a client and every client derived from it, and those are used from several
#: threads: an unlocked prune computed on one thread and written back after
#: another thread's ``append`` dropped that predicate, leaving a live derived
#: client whose every authorized request was refused.
#:
#: One lock for the whole process, so anything that takes it on the request path
#: serialises every wrapped client in it against every other. Reading does not
#: take it — see :func:`_live`.
_PROVENANCE_LOCK = threading.Lock()


def _forever() -> bool:
    """The aliveness of a predicate that declares none: it outlives the list."""
    return True


def _is_alive(check: Provenance) -> bool:
    """Whether the client this predicate belongs to still exists."""
    return getattr(check, ALIVE, _forever)()


def _compact(provenances: list[Provenance]) -> None:
    """Drop, in place, the predicates of clients that no longer exist.

    The list lives on a transport that derived clients share with their parent,
    so one predicate is added per ``with_options()`` — and a derived client made
    per request would otherwise leave one behind per request, forever.

    Under the lock, because it writes and ``_guard_one`` appends.
    """
    with _PROVENANCE_LOCK:
        provenances[:] = [check for check in provenances if _is_alive(check)]


def _live(provenances: list[Provenance]) -> tuple[Provenance, ...]:
    """The predicates still standing — rewriting the list only if one has gone.

    This runs on every body-bearing request of every wrapped client, so what it
    does *not* do is the point. It used to take the process-wide lock and
    rebuild the whole list, on every request, whether or not anything had died —
    and nothing usually has, since a predicate dies only when a derived client
    is collected. A server calling the AI from several threads was serialising
    its own outgoing requests against each other to do no work.

    The snapshot is taken without the lock and cannot tear: ``tuple(list)`` is
    one C-level copy that runs no Python code, so it sees the list either before
    or after another thread's write, never half-way through one. Iterating the
    list itself would not be safe — a concurrent compaction shifts the indices
    under an iterator, which is how a live derived client loses its predicate
    mid-check and has its authorized request refused.

    A dead predicate left in the list for one more request is harmless: its
    scope is gone, so it answers ``False``, which is exactly what it answered
    while alive for any call that was not its own. Compaction is therefore a
    memory concern and nothing else, and it is done once a death is seen rather
    than on the chance of one.
    """
    snapshot = tuple(provenances)
    live = tuple(check for check in snapshot if _is_alive(check))
    if len(live) != len(snapshot):
        _compact(provenances)
    return live


#: Marker on a patched ``send`` so a second ``wrap()`` never stacks guards.
_GUARD_MARKER = "__idside_transport_guarded__"

#: Methods allowed through unenforced. This is where the line is drawn: they
#: carry no prompt to infer on, they are the provider's management surface
#: (``models.list``, ``files.delete``, polling a response id), and
#: ``/v1/authorize`` needs a ``model`` they do not have. A body-bearing method
#: is an inference call until proven otherwise.
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})

#: How far into a client's object graph to look for its transport, and how many
#: objects to touch at most. Both are bounds, not tuning: an unbounded walk over
#: an arbitrary third-party object graph is a hang waiting to happen, on the
#: request path.
#:
#: They are not the same KIND of bound, and only one of them is reported. The
#: depth is the declared scope of the search — "a transport a client owns is
#: within three hops of it" — uniform for every client, and true of every client
#: this SDK ships support for. The node budget is a give-up: whether it is hit,
#: and what is left unseen when it is, depends on how many attributes happened
#: to be materialised on this particular instance. The same client can then be
#: fully guarded in one process and half-guarded in the next, which is exactly
#: the kind of fact ``install()`` must not round off to ``True``.
_MAX_DEPTH = 3
_MAX_NODES = 256

#: The three marks of an HTTP client, looked for on the CLASS and never on the
#: instance.
#:
#: ``isinstance(node, httpx.Client | httpx.AsyncClient)`` did not ask "is this an
#: HTTP client". It asked which *distribution* shipped the class, and that
#: question had one answer for years and now has two: ``openai>=3`` requires
#: ``httpx2``, a separate distribution whose clients are not ``httpx`` clients
#: and never will be. Under it nothing matched, ``install()`` answered ``False``,
#: and the journal printed ``net=false`` — "there was nothing to guard" — about a
#: client holding a live route to the provider. Silent, and reported as
#: reassuring.
#:
#: So the question asked is the one that was meant: does this object have the
#: shape of an HTTP client. All three marks, because any one alone is common —
#: a mailer sends, an SDK shim has a ``base_url`` — and a false positive here is
#: not a harmless extra guard but a working object broken on the request path.
#:
#: Read off ``type(node)``, which is inert: ``getattr`` on a class hands back the
#: function or the ``property`` OBJECT without running either. That is the same
#: reason the walk reads ``__dict__`` and never an instance attribute.
_TRANSPORT_MARKS = ("send", "build_request", "base_url")


class _Transport(Protocol):
    """What the net recognises, stated as a shape rather than as a class.

    Never used to *construct* anything — these objects only ever arrive out of a
    third-party client's ``__dict__``. It is here so the annotations below say
    what they mean.
    """

    def send(self, request: Any, /, *args: Any, **kwargs: Any) -> Any: ...

    def build_request(self, *args: Any, **kwargs: Any) -> Any: ...

    @property
    def base_url(self) -> Any: ...


def _is_transport(node: Any) -> bool:
    """Whether ``node`` is an HTTP client, whoever packaged it."""
    cls = type(node)
    try:
        marks = [getattr(cls, name, None) for name in _TRANSPORT_MARKS]
    except Exception:
        # A metaclass ``__getattr__`` is arbitrary code and may raise. Something
        # that will not answer what it is does not get patched.
        return False
    send, build_request, base_url = marks
    return callable(send) and callable(build_request) and base_url is not None


def _sends_coroutines(node: Any) -> bool:
    """Whether this transport's ``send`` must be awaited.

    Asked of the CLASS, not of ``node.send``: by the time a second ``wrap()``
    comes past, the instance attribute is this module's own wrapper, and the
    shape to preserve is the one the transport was written in.
    """
    return inspect.iscoroutinefunction(getattr(type(node), "send", None))


#: What ``install()`` answers, and what the journal's ``net=`` repeats verbatim.
#: ``True`` and ``False`` keep the meaning they always had; ``"partial"`` is the
#: case that used to be reported as one of them and is neither.
NetState = bool | Literal["partial"]


class _Scan(NamedTuple):
    """Every transport one walk found, and whether that walk got to finish.

    The second field is the whole point: a list of transports says nothing about
    the ones the search never reached, and a caller that cannot tell the two
    apart can only guess in the reassuring direction.
    """

    transports: list[_Transport]
    complete: bool


def _find_transports(client: Any) -> _Scan:
    """Breadth-first search for EVERY HTTP transport a client sends on.

    Reads ``__dict__`` only, never ``getattr`` on a guessed attribute name: a
    property is arbitrary code, and evaluating one to go looking for a transport
    could open a connection, mint a token or raise from somewhere unrelated. The
    instance dictionary is inert.

    Covers the shapes that exist today — ``openai``/``anthropic`` (``_client``,
    on ``httpx`` up to openai 2 and on ``httpx2`` from openai 3), ``google-genai``
    (``_api_client._httpx_client`` *and* ``_async_httpx_client``), ``mistralai``
    (``sdk_configuration.client``) — without a descriptor field naming any of
    them, because the point of this net is to catch what nobody declared.

    Every match, not the first: a client that owns a blocking and an async
    transport at once has two routes out, and guarding one of them guards
    nothing. A matched transport is not walked into — its own internals hold
    pools and connections, never another client's route to the provider.

    Returns an empty list *and* ``complete=True`` when there is nothing to
    guard, which is the honest answer for a client that owns no HTTP transport:
    it cannot leak a request through one. Running out of budget is a different
    answer and is reported as one — ``complete=False`` — because what was not
    reached may well have been a second route out.
    """
    found: list[_Transport] = []
    queue: deque[tuple[Any, int]] = deque([(client, 0)])
    seen: set[int] = {id(client)}
    budget = _MAX_NODES
    while queue:
        node, depth = queue.popleft()
        if _is_transport(node):
            found.append(node)
            continue
        if depth >= _MAX_DEPTH:
            continue
        try:
            children = list(vars(node).values())
        except TypeError:
            continue  # no instance dictionary (slots, builtin): nothing to walk
        for child in children:
            # Checked BEFORE the child is spent, not after: giving up on the
            # object that exhausted the budget would report ``partial`` for a
            # graph the walk had in fact just finished crossing. Incomplete
            # means "there was one more and we did not look at it".
            if budget <= 0:
                return _Scan(found, complete=False)
            budget -= 1
            if id(child) not in seen:
                seen.add(id(child))
                queue.append((child, depth + 1))
    return _Scan(found, complete=True)


def _takes_no_redirects(transport: _Transport) -> bool:
    """Whether this transport's ``send`` will accept ``follow_redirects=False``.

    Asked of the signature, which is what the call will be judged against.
    ``**kwargs`` counts as acceptance, deliberately: it is how every recorder,
    retry shim and piece of application instrumentation wraps a ``send``, and
    reading it as a refusal would refuse the clients this SDK is most often
    handed. ``httpx`` and ``httpx2`` both name the keyword outright.

    A signature that cannot be read at all — a ``send`` implemented in C, a
    callable object that will not describe itself — is a refusal. Neither
    remaining outcome is acceptable, so there is nothing to fall back on.
    """
    try:
        parameters = inspect.signature(transport.send).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        (
            parameter.name == _NO_REDIRECTS
            and parameter.kind
            in (
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        )
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def unredirectable_route(client: Any) -> str | None:
    """Describe a transport that cannot be told to refuse redirects, if any.

    A preflight, in the shape of ``_destination.unguarded_route``: it changes
    nothing and is asked before ``wrap()`` patches a single method, so a client
    it names is refused whole rather than left half-enforced. It walks the
    object graph a second time, which costs a bounded walk once per wrap and
    nothing at all on the request path.

    ``None`` means every transport found will take the keyword — including the
    case where there is no transport to find, which the caller already judges on
    its own terms (a stand-in with no HTTP client of its own cannot leak through
    one).
    """
    for transport in _find_transports(client).transports:
        if not _takes_no_redirects(transport):
            return (
                f"its `{type(transport).__name__}.send` does not accept "
                f"`{_NO_REDIRECTS}`, which every authorized request is sent with"
            )
    return None


def install(
    client: Any,
    descriptor: ProviderDescriptor,
    token_has_provenance: Provenance | None = None,
    *,
    undo: list[Callable[[], None]] | None = None,
) -> NetState:
    """Guard every HTTP transport ``client`` owns, and say what that came to.

    Patches ``send`` on each transport *instance*, so nothing global and no other
    client is affected — including the SDK's own connection to the ID Side
    cloud, which is a different transport that is not reachable from here.
    Idempotent via a marker, and a derived client that shares its parent's
    transport (``OpenAI.copy()`` does) is already covered.

    Three answers, because three things are true and only one of them used to be
    said. This value goes straight into the journal as ``net=``, and that field
    is read as "is this client covered?":

    - ``True`` — the search finished, and every transport it found is guarded.
    - ``False`` — the search finished and found nothing to guard. A client that
      owns no HTTP transport cannot leak a request through one; that is the shape
      of every fake in the test suite, and it is not an error.
    - ``"partial"`` — at least one route out of this client is, or may be,
      unguarded: the walk ran out of budget before it finished, or a transport
      refused the patch. Changes from this attempt are rolled back;
      ``consent.wrap()`` treats the result as unsupported and refuses the client.

    ``undo``, when given, receives what reverses an install that succeeded. One
    wrap guards several clients — the wrapped one, and every client named in
    ``credentials`` — and an install that went through is not rolled back by a
    later one that did not: the caller has to be able to do it.
    """
    scan = _find_transports(client)
    # Discovery is a preflight. If the bounded walk did not finish, mutating the
    # routes it happened to see would leave a failed wrap in a half-guarded state.
    if not scan.complete:
        return "partial"
    # A list, deliberately, and not a generator inside ``all()``: every
    # transport has to be offered the patch, and short-circuiting on the first
    # refusal would skip checking the ones after it. Every mutation is recorded
    # so a partial result restores only what this install attempt changed.
    provenance = token_has_provenance or (lambda _token, _method, _path, _origin: False)
    guarded: list[bool] = []
    rollbacks: list[Callable[[], None]] = []
    for transport in scan.transports:
        existing = getattr(transport.send, _GUARD_MARKER, None)
        instance_state = getattr(transport, "__dict__", None)
        had_own_send = isinstance(instance_state, dict) and "send" in instance_state
        previous_send = (
            instance_state.get("send") if isinstance(instance_state, dict) else None
        )

        guarded.append(_guard_one(transport, descriptor.token_header, provenance))
        if isinstance(existing, list):

            def _remove_provenance(
                provenances: list[Provenance] = existing,
                predicate: Provenance = provenance,
            ) -> None:
                with _PROVENANCE_LOCK:
                    for index in range(len(provenances) - 1, -1, -1):
                        if provenances[index] is predicate:
                            provenances.pop(index)
                            return

            rollbacks.append(_remove_provenance)
        else:

            def _restore_send(
                target: _Transport = transport,
                had_own: bool = had_own_send,
                previous: Any = previous_send,
            ) -> None:
                try:
                    if had_own:
                        setattr(target, "send", previous)  # noqa: B010
                    elif "send" in getattr(target, "__dict__", {}):
                        delattr(target, "send")
                except Exception:
                    # A hostile setter can make perfect rollback impossible. The
                    # install still reports partial and never claims coverage.
                    return

            rollbacks.append(_restore_send)

    if not all(guarded):
        for rollback in reversed(rollbacks):
            rollback()
        return "partial"
    if undo is not None:
        undo.extend(rollbacks)
    return bool(guarded)


def _guard_one(
    transport: _Transport,
    token_header: str,
    token_has_provenance: Provenance,
) -> bool:
    """Wrap one transport's ``send``, in the shape that transport is written in.

    The blocking and the async halves get different wrappers because they must:
    an ``async def`` on a blocking client returns a coroutine nobody awaits, and
    a plain ``def`` on an async one hands back a response where a coroutine was
    expected. Only the wrapping differs — the question asked is the same one, and
    both ask it before anything is sent.

    Returns whether this transport ended up guarded. A transport that will not
    take the patch — sealed attributes, a read-only proxy — is reported to
    ``consent.wrap()``. The caller installs the net before patching call sites,
    so refusal leaves them untouched.
    """
    existing = getattr(transport.send, _GUARD_MARKER, None)
    if isinstance(existing, list):
        _compact(existing)
        with _PROVENANCE_LOCK:
            existing.append(token_has_provenance)
        return True

    provenances = [token_has_provenance]

    guarded: Any
    if _sends_coroutines(transport):
        async_original = transport.send

        @functools.wraps(async_original)
        async def _guarded_async(request: Any, *args: Any, **kwargs: Any) -> Any:
            if _refuse_unenforced(request, token_header, provenances):
                kwargs["follow_redirects"] = False
            return await async_original(request, *args, **kwargs)

        guarded = _guarded_async
    else:
        sync_original = transport.send

        @functools.wraps(sync_original)
        def _guarded_sync(request: Any, *args: Any, **kwargs: Any) -> Any:
            if _refuse_unenforced(request, token_header, provenances):
                kwargs["follow_redirects"] = False
            return sync_original(request, *args, **kwargs)

        guarded = _guarded_sync

    setattr(guarded, _GUARD_MARKER, provenances)
    try:
        # setattr, not `transport.send = …`: this deliberately shadows a method
        # on ONE instance, which is exactly what a type checker is right to flag
        # when it is not deliberate.
        setattr(transport, "send", guarded)  # noqa: B010
    except Exception:
        return False
    # Read back rather than trusted: a ``__setattr__`` that swallows the write
    # raises nothing, and the difference between "patched" and "silently
    # ignored" is the whole difference between ``net=true`` and ``net=partial``.
    return isinstance(getattr(transport.send, _GUARD_MARKER, None), list)


def _refuse_unenforced(
    request: Any,
    token_header: str,
    token_provenances: list[Provenance],
) -> bool:
    """The whole decision, shared by both wrappers so it cannot differ.

    The method, path and origin bind the private permit; the named header
    supplies the token it must match. If the blocking and async net ever
    answered differently, the async client would be the one that leaked.

    Returns whether the request went through as an AUTHORIZED one — the only
    kind that must not follow a redirect. A bodyless request passes without
    being read any further, exactly as before.
    """
    method = request.method.upper()
    if method in _BODYLESS_METHODS:
        return False
    live = _live(token_provenances)
    token = request.headers.get(token_header)
    path = request.url.path
    origin = origin_of(request.url)
    if not isinstance(token, str) or not any(
        check(token, method, path, origin) for check in live
    ):
        raise UnenforcedCallError(_refusal(request.method, path))
    return True


def _refusal(method: str, path: str) -> str:
    """Say what was stopped, using only what the guard is allowed to look at.

    The method and the URL *path* — never the query string, which is part of the
    request and can carry identifiers.
    """
    return (
        f"{method} {path} was about to be sent by a consent-enforced client "
        "without going through ID Side consent enforcement, or to an origin other "
        "than the one the client was wrapped with, so it was NOT sent. "
        "Only the endpoints the provider descriptor declares are enforced; this "
        "one is not, and letting it through would mean an AI call reaching the "
        "provider with no proof of consent and no ledger entry. Non-inference "
        "traffic (uploads, batches, management endpoints) belongs on a second, "
        "UNWRAPPED client — there is no bypass option on a wrapped one. If this "
        "endpoint does carry inference, it needs a call site in the descriptor: "
        "please report it at https://github.com/idside-eu/idside-sdk/issues."
    )
