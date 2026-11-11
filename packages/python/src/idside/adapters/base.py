# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Descriptor-driven provider adapter — the injection engine.

The adapter handles only the *mechanics* (which call to intercept, where to inject
the cloud-decided params, which header carries the ACT token). The *policy* (which
params to force) is decided by the cloud and passed in as ``params_forced``.

Subclass and override for providers whose API does not fit the declarative model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, NamedTuple

from idside._request_hash import request_hash as _request_hash
from idside.adapters._call_options import (
    is_not_given,
    is_sentinel,
    mapping_view,
    transport_options,
)
from idside.exceptions import (
    ConsentTokenError,
    ToolUseRefusedError,
    UnsupportedProviderError,
)
from idside.providers import CallSite, ProviderDescriptor, ToolField

_JSON_SCALARS = (bool, str, int, float)


#: A plain-dict view of a value, or None if it is not one. Tolerates Pydantic
#: models because google-genai's ``config`` may be a ``GenerateContentConfig``
#: rather than a dict, and the tools we are looking for sit inside it either way.
#: The same reading the per-call option checks make, so the two cannot drift.
_as_mapping = mapping_view


def _spellings(key: str) -> tuple[str, ...]:
    """A key as written, and its camelCase twin.

    google-genai validates ``config`` with Pydantic aliases, so
    ``{"cachedContent": …}`` is the same argument as ``{"cached_content": …}``.
    A path read in one spelling only is a tool the other one walks past.
    """
    camel = re.sub(r"_([a-z])", lambda match: match.group(1).upper(), key)
    return (key,) if camel == key else (key, camel)


def _read_paths(value: Any, path: tuple[str, ...]) -> list[Any]:
    """Every value found at ``path``, in either spelling at each step."""
    if not path:
        return [value]
    mapping = _as_mapping(value)
    if mapping is None:
        return []
    head, *rest = path
    return [
        found
        for key in _spellings(head)
        if key in mapping
        for found in _read_paths(mapping[key], tuple(rest))
    ]


def _drop_path(kwargs: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """Return a copy of ``kwargs`` without the argument at ``path``.

    Removes the key rather than emptying it: several providers reject an empty
    ``tools`` list, and "no tools" is what the user asked for, not "zero tools".
    Both spellings go, so a camelCase twin cannot carry the tools on. Never
    mutates the caller's objects — a wrapped client must not rewrite the dict
    the application still holds a reference to.
    """
    head, *rest = path
    mapping = _as_mapping(kwargs)
    keys = [key for key in _spellings(head) if mapping is not None and key in mapping]
    if mapping is None or not keys:
        return kwargs

    new = dict(mapping)
    for key in keys:
        if not rest:
            del new[key]
        else:
            nested = _as_mapping(new[key])
            new[key] = new[key] if nested is None else _drop_path(nested, tuple(rest))
    return new


def _carries_tools(kwargs: dict[str, Any], field: ToolField) -> bool:
    """Whether this call hands the model a tool through ``field``.

    Presence, per kind: ``definitions`` carry tools unless they are an empty
    list or an empty mapping — ``tools=[]`` is no tools, in both SDKs — and any
    other value counts, ``False``, ``0`` and ``""`` included: a value nobody
    expected there is no proof that no tool is. An ``enabler`` carries them
    as soon as it is given at all, an empty object included, since
    ``web_search_options={}`` is exactly how hosted search is switched on. An
    ``input_items`` array carries them when one of its items is of a listed
    type; a string input has no items. A library's "not given" sentinel is
    absence under every kind. A kind this SDK does not know yet is read as an
    enabler: a tool field nobody taught it to read is not one it may assume
    empty.
    """
    for value in _read_paths(kwargs, field.path):
        if is_not_given(value):
            continue
        if field.kind == "definitions":
            if not (isinstance(value, (list, tuple, Mapping)) and len(value) == 0):
                return True
        elif field.kind == "input_items":
            if isinstance(value, (list, tuple)) and any(
                _item_type(item) in field.item_types for item in value
            ):
                return True
        else:
            return True
    return False


def _item_type(item: Any) -> Any:
    """The ``type`` of an input item, whether a dict or a typed model.

    An item that names no type (absent or null) but an ``id`` is a reference to
    a stored item: OpenAI's ``ItemReference`` makes ``type`` optional, so
    ``{"id": "fc_…"}`` alone is an ``item_reference``. Read by its declared type
    only, it walked past a refusal naming ``item_reference``.
    """
    if isinstance(item, dict):
        kind, reference = item.get("type"), item.get("id")
    else:
        kind, reference = getattr(item, "type", None), getattr(item, "id", None)
    if kind is None and reference is not None:
        return "item_reference"
    return kind


class ResolvedCallSite(NamedTuple):
    """A descriptor call site, located on a real client instance.

    ``target``/``method`` are what gets patched; ``site`` is what it *is* — the
    endpoint's stable id and the parameters that endpoint accepts.
    """

    target: Any
    method: str
    site: CallSite


def _merge_headers(current: Any, injected: dict[str, Any]) -> dict[str, Any]:
    """Replace injected HTTP fields case-insensitively in a fresh header map."""
    headers = dict(current or {})
    replaced = {name.lower() for name in injected}
    return {
        **{
            name: value
            for name, value in headers.items()
            if not isinstance(name, str) or name.lower() not in replaced
        },
        **injected,
    }


def _normalize(value: Any, path: str) -> Any:
    """Return a JSON-compatible copy of ``value``, or raise ``ConsentTokenError``.

    Strict by design: dict/list/str/int/float/bool/None pass through, objects
    exposing ``model_dump()`` (Pydantic v2) are converted recursively, anything
    else is an error. A silent fallback (``str()``/``repr()``) would embed
    interpreter-dependent text — at worst a memory address — into the request
    hash and destroy its reproducibility.
    """
    if value is None or isinstance(value, _JSON_SCALARS):
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConsentTokenError(
                    f"Cannot compute a deterministic request_hash: object key "
                    f"{key!r} at '{path}' is not a string. JSON object keys "
                    "must be strings."
                )
            normalized[key] = _normalize(item, f"{path}.{key}")
        return normalized
    if isinstance(value, type):
        # A CLASS, not an instance. `chat.completions.parse(response_format=…)`
        # takes the model class itself, and the provider puts its JSON schema on
        # the wire — so the schema is what the request hash must cover. Checked
        # before `model_dump`, which on a class is an unbound function that is
        # callable and blows up when called.
        json_schema = getattr(value, "model_json_schema", None)
        if callable(json_schema):
            return _normalize(json_schema(), path)
    else:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return _normalize(model_dump(), path)
    raise ConsentTokenError(
        f"Cannot compute a deterministic request_hash: value of type "
        f"'{type(value).__name__}' at '{path}' is not JSON-serializable. "
        "Pass JSON-compatible values (dict/list/str/int/float/bool/None) or "
        "objects exposing model_dump() (Pydantic v2)."
    )


class ProviderAdapter:
    """Generic engine that intercepts a provider call and enforces consent."""

    def __init__(self, descriptor: ProviderDescriptor) -> None:
        self.descriptor = descriptor

    @property
    def name(self) -> str:
        return self.descriptor.name

    def get_call_sites(self, client: Any) -> list[ResolvedCallSite]:
        """Resolve every declared-and-present endpoint to intercept.

        Returns ALL call sites declared by the descriptor that are actually
        present on the client (e.g. both OpenAI entrypoints,
        ``chat.completions.create`` and ``responses.create``). Every returned
        site must be consent-enforced: a declared-but-unwrapped site would let
        AI calls through without verification. Declared sites absent from the
        client are skipped.

        Each result carries the descriptor's :class:`CallSite`, so the caller
        knows *which endpoint* it patched — the SDK tells the cloud, and the
        cloud decides parameters per endpoint rather than per provider.

        A site's ``aliases`` are resolved too, each as its own result carrying
        the *same* :class:`CallSite`: ``chat.completions.parse`` reaches the same
        HTTP endpoint as ``create``, so it is enforced under the same wire id and
        the cloud is told nothing new. Absent aliases are skipped like absent
        sites.
        """
        sites: list[ResolvedCallSite] = []
        for site in self.descriptor.call_sites:
            target = client
            for attr in site.path[:-1]:
                if not hasattr(target, attr):
                    break
                target = getattr(target, attr)
            else:
                for method in (site.path[-1], *site.aliases):
                    if hasattr(target, method):
                        sites.append(ResolvedCallSite(target, method, site))
        if not sites:
            raise UnsupportedProviderError(
                f"None of the call sites for provider '{self.descriptor.name}' "
                f"exist on the given client."
            )
        return sites

    def get_call_site(self, client: Any) -> ResolvedCallSite:
        """Resolve the first declared-and-present endpoint.

        Kept for backward compatibility; enforcement code must use
        :meth:`get_call_sites` so that no declared entrypoint is left unwrapped.
        """
        return self.get_call_sites(client)[0]

    def extract_request_metadata(self, **kwargs: Any) -> dict[str, Any]:
        """Extract the model and the deterministic request hash for one call.

        The hash is ``sha256:<hex>`` over the RFC 8785 (JCS) canonical form of
        the caller's pre-enforcement payload — the call kwargs minus the two
        identity parameters (``user_id``/``ubt``) and the per-request headers
        kwarg. Cloud-forced parameters, local constraint transformations,
        routed credentials and the ACT header are applied only after this hash
        is computed. Shared test vectors in
        ``packages/descriptors/testdata/`` pin this computation across SDK
        languages. Values that cannot be normalized to JSON raise
        :class:`ConsentTokenError` (never a silent ``repr()`` fallback), with two
        exceptions that put nothing on the wire: a library's "not given"
        sentinel (``omit``, ``NOT_GIVEN``, ``UNSET``), and a transport option
        whose value is an object — ``timeout=httpx.Timeout(…)``,
        ``retries=RetryConfig(…)`` — which says how the request travels, not
        what it asks. Both used to raise, so leaving them out moves no hash.
        """
        ignored = {
            self.descriptor.identity_param,
            self.descriptor.ubt_param,
            self.descriptor.headers_kwarg,
        }
        transport = transport_options(self.descriptor)
        payload = {}
        for key, value in kwargs.items():
            # A sentinel is a library's "not passed": nothing goes on the wire
            # for it, so nothing goes in the hash either.
            if key in ignored or is_sentinel(value):
                continue
            try:
                payload[key] = _normalize(value, key)
            except ConsentTokenError:
                # `timeout=httpx.Timeout(10)` or `retries=RetryConfig(...)` says
                # how the request travels, not what it asks. Left out rather than
                # stringified, and only when it cannot be written as JSON: a
                # plain `timeout=5.0` was always hashed, and a hash that already
                # proved a call must not move. No call carrying one of these
                # objects ever had a hash, so leaving them out moves none.
                if key not in transport:
                    raise
        try:
            # The single source of the formula, shared with the public
            # `idside.request_hash()` a verifier recomputes it with.
            request_hash = _request_hash(payload)
        except ValueError as exc:
            raise ConsentTokenError(
                f"Cannot compute a deterministic request_hash: {exc}"
            ) from exc
        return {"model": kwargs.get("model", ""), "request_hash": request_hash}

    def apply_constraints(
        self,
        kwargs: dict[str, Any],
        constraints: dict[str, Any],
        call_site: str | None = None,
    ) -> dict[str, Any]:
        """Honour the cloud's non-parameter constraints on this call.

        A constraint is not a forced parameter and must not be one: forcing a
        value writes the developer's request, which the cloud is never allowed
        to do (see ``protected_params``). ``tools`` is the first and only
        constraint — the mechanism the descriptors call
        ``refuse_or_strip_tools``.

        Note what is NOT sent to the cloud to make this work: nothing. The cloud
        returns the constraint from the user's consent state alone and never
        learns whether this particular call carried tools. That question is
        answered here, on the caller's own machine, because answering it
        anywhere else would mean telling us something about the request — and
        the whole architecture rests on our not knowing.

        What counts as a tool is wider than one key. ``tools_param`` is where a
        provider takes tool definitions, and ``call_site`` names the endpoint
        whose ``tool_fields`` list every other argument that can hand the model
        one: ``functions``, ``web_search_options``, a stored conversation, a
        Mistral agent, a Gemini cache. Without ``call_site``, the tool fields of
        every call site the descriptor declares are read: a caller that does not
        say which endpoint it is gets the wide reading, never the narrow one.

        ``strip`` removes definitions, which the SDK can see and take out. Every
        other kind is refused under ``strip`` too: an agent or a stored
        conversation is sent whole or not at all, and removing it changes what
        the call means.
        """
        action = constraints.get("tools")
        if not action:
            return kwargs

        if action not in ("refuse", "strip"):
            # An unknown instruction is not "no instruction". Guessing would be
            # the one failure mode this whole path exists to prevent.
            raise ConsentTokenError(
                f"ID Side cloud returned an unknown tools constraint {action!r}; "
                "the AI call was not made. Upgrade the idside package."
            )

        path = self.descriptor.tools_param
        if path is None:
            raise ConsentTokenError(
                "Agentic tools are not accepted for this user, but the "
                f"'{self.descriptor.name}' descriptor does not say where tools "
                "travel on a call, so the SDK cannot check. The AI call was not "
                "made — a refusal it cannot apply is not a refusal it may ignore."
            )

        site = self.descriptor.call_site(call_site) if call_site else None
        sites = (site,) if site is not None else self.descriptor.call_sites
        fields = tuple(
            dict.fromkeys(
                (
                    ToolField(path=path, kind="definitions"),
                    *(field for each in sites for field in each.tool_fields),
                )
            )
        )
        carried = [field for field in fields if _carries_tools(kwargs, field)]
        if not carried:
            return kwargs  # no tools on this call: nothing to refuse

        def _named(found: list[ToolField]) -> str:
            return ", ".join(f"`{'.'.join(field.path)}`" for field in found)

        # One message for whoever refused — the person, nobody asking them, or
        # an organization's policy, which no answer lifts — because the SDK
        # does not know which, and must not. Node's is the same, word for word.
        refused = (
            "Agentic tools are not accepted for this user (refused by the "
            "person, never asked, or refused by a policy)"
        )
        # "Without them": the fields named, which are not always `tools` — told
        # "without the tools" about `conversation`, a developer drops `tools`,
        # keeps the reference and is refused again.
        remedy = (
            "The AI call was not made. Retry without them; never retry the same "
            "call, it will be refused again."
        )
        if action == "refuse":
            raise ToolUseRefusedError(
                f"{refused}, and this call passes {_named(carried)}. {remedy}"
            )
        whole = [field for field in carried if field.kind != "definitions"]
        if whole:
            raise ToolUseRefusedError(
                f"{refused}, and this call passes {_named(whole)}, which can bring "
                "tools the SDK cannot see or remove without changing what the "
                f"call asks. {remedy}"
            )
        for field in carried:
            kwargs = _drop_path(kwargs, field.path)
        return kwargs

    def apply(
        self,
        kwargs: dict[str, Any],
        params_forced: dict[str, Any],
        token: str,
        credential_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return new call kwargs with cloud-decided params + ACT token injected.

        ``params_forced`` keys listed in the descriptor's ``protected_params`` are
        dropped (the cloud can never override core request fields). Injection goes
        to kwargs or to per-request headers per the descriptor's ``param_target``;
        the headers travel in the kwarg named by ``headers_kwarg``. Does not
        mutate the input.

        ``credential_headers`` selects which of the developer's credentials
        carries this call (OpenAI's ``OpenAI-Project``), and is applied last: it
        is decided from the developer's own configuration, so nothing the cloud
        forces may overwrite it. An adapter overriding this method **must**
        forward it — ``consent.wrap()`` verifies the header landed and refuses
        the call otherwise, rather than let a routed request leave on the
        credential it was meant to move off.
        """
        new = dict(kwargs)
        safe = {
            k: v
            for k, v in params_forced.items()
            if k not in self.descriptor.protected_params
        }
        injected_headers: dict[str, Any] = {}
        if self.descriptor.param_target == "headers":
            injected_headers.update(safe)
        else:
            new.update(safe)
        injected_headers[self.descriptor.token_header] = token
        if credential_headers:
            injected_headers.update(credential_headers)
        new[self.descriptor.headers_kwarg] = _merge_headers(
            new.get(self.descriptor.headers_kwarg), injected_headers
        )
        return new
