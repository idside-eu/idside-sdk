# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Provider descriptors and their auto-loading from bundled JSON files.

A descriptor declares the *mechanics* of a provider (which calls to intercept,
where to inject forced params, which header carries the ACT token). The *policy*
(which params to force for a given consent state) is decided by the cloud and
returned as ``params_forced`` — it does not live here.

Add a standard provider by dropping a JSON file in this package; no Python needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

DEFAULT_CONSENT_HEADER = "X-AI-Consent-Token"

__all__ = [
    "CallSite",
    "CredentialRouting",
    "ProviderDescriptor",
    "ToolField",
    "load_descriptors",
]


@dataclass(frozen=True)
class CredentialRouting:
    """How this provider lets one app hold several privacy postures at once.

    The privacy posture (does this traffic train the model, is it retained, in
    which region) is not a request parameter anywhere — it is a property of the
    credential. ``unit`` names what the credential selects: a *project* at
    OpenAI and Gemini, a *workspace* at Anthropic, an *organisation* at Mistral.

    ``header`` is the per-request header that selects it, when the provider
    validates one — only OpenAI does (``OpenAI-Project``). Without it, switching
    posture means switching client, because a key is chosen at construction.

    ``header_value_pattern`` is checked before any value reaches an outgoing
    request. It keeps CR/LF out of a header, and refuses a value of the wrong
    kind: an OpenAI project id is ``proj_…`` where an API key is ``sk-…``, and
    the developer who pastes the second must be stopped before the secret
    leaves the process.
    """

    header: str | None = None
    header_value_pattern: str | None = None
    unit: str = "credential"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CredentialRouting:
        return cls(
            header=data.get("header"),
            header_value_pattern=data.get("header_value_pattern"),
            unit=data.get("unit") or "credential",
        )


@dataclass(frozen=True)
class ToolField:
    """One argument through which a call can hand the model a tool.

    A ``tools`` constraint is only as wide as what the SDK reads. Reading the
    provider's ``tools`` key alone let ``functions``, ``web_search_options``, a
    Mistral agent, a Gemini cache or a stored OpenAI conversation carry tools
    straight past a refusal — so each call site names every such argument.

    ``kind`` decides how a refusal is applied:

    - ``definitions`` — a list of tool definitions. An empty list carries none;
      under ``strip`` the key is removed.
    - ``enabler`` — any present, non-null value can bring tools with it: an
      object switching a hosted tool on, or a reference to a server-side object
      the SDK cannot see into. Removing it would change what the call means, so
      it is refused under ``strip`` as under ``refuse``.
    - ``input_items`` — an array whose items of the listed ``item_types`` carry
      tool definitions. Refused under both, for the same reason.

    ``path`` is in the Python client's spelling; the descriptor's ``js_path``
    belongs to the other SDK and is not read here.
    """

    path: tuple[str, ...]
    kind: str
    item_types: frozenset[str] = frozenset()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolField:
        return cls(
            path=tuple(data["path"]),
            kind=data["kind"],
            item_types=frozenset(data.get("item_types", ())),
        )


@dataclass(frozen=True)
class CallSite:
    """One endpoint to intercept, and what may be forced *on that endpoint*.

    A provider's endpoints do not accept the same parameters. OpenAI's
    ``background`` exists on ``/v1/responses`` and nowhere else, so a
    provider-wide allowlist could only ever be the intersection (losing the
    lever) or the union (breaking ``chat.completions`` for exactly the users who
    opted out). Hence one allowlist per site.

    ``id`` is the stable, language-neutral name sent to the cloud as
    ``call_site``. It is written in the Python client's spelling because the
    descriptor is shared: the JS client may call the same endpoint something else
    (``generateContent`` vs ``generate_content``), and one endpoint must have one
    name on the wire.
    """

    id: str
    path: tuple[str, ...]
    #: Extra method names on the SAME target object that reach the SAME HTTP
    #: endpoint, and must therefore be enforced under this site's ``id`` —
    #: ``chat.completions.parse`` posts to ``/v1/chat/completions`` just as
    #: ``create`` does, and would otherwise reach the provider unenforced. Names
    #: from every client language may be listed; whichever exists on the client
    #: is patched, and an absent alias is skipped exactly like an absent call
    #: site. Aliasing adds no new wire id: the cloud contract is unchanged.
    aliases: tuple[str, ...] = ()
    #: Keyword arguments whose presence means this call RESUMES a result that
    #: already exists instead of creating one. Some client methods are
    #: overloaded: ``responses.stream(response_id=…)`` replays a stored response
    #: over ``GET /v1/responses/{id}``, where ``responses.stream(model=…,
    #: input=…)`` creates one over POST. The resuming form carries no prompt and
    #: no model, so it is let through unenforced — authorizing it would ask the
    #: cloud to decide about an empty model and sign a token asserting an AI call
    #: that never happened. It is not a bypass: a call let through carries no ACT
    #: token, so the net refuses it the moment it turns out to carry a body.
    resuming_params: tuple[str, ...] = ()
    forceable_params: frozenset[str] = frozenset()
    #: Every argument of THIS endpoint that can hand the model a tool, besides
    #: the provider-wide ``tools_param``. See :class:`ToolField`.
    tool_fields: tuple[ToolField, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CallSite:
        return cls(
            id=data["id"],
            path=tuple(data["path"]),
            aliases=tuple(data.get("aliases", ())),
            resuming_params=tuple(data.get("resuming_params", ())),
            forceable_params=frozenset(data.get("forceable_params", ())),
            tool_fields=tuple(
                ToolField.from_dict(f) for f in data.get("tool_fields", ())
            ),
        )


@dataclass(frozen=True)
class ProviderDescriptor:
    """Declarative description of a provider's injection mechanics.

    ``identity_param`` and ``ubt_param`` are the two exclusive kwargs a caller
    may use to name the end user (developer-asserted id vs widget-issued User
    Binding Token). Both are consumed by the SDK, never forwarded to the
    provider, and excluded from the request hash. ``headers_kwarg`` names the
    kwarg through which the provider's client accepts per-request HTTP headers
    (also hash-excluded, since the ACT token travels there).
    """

    name: str
    client_module: str
    call_sites: tuple[CallSite, ...]
    #: Hosts identifying THIS provider when several descriptors share a
    #: ``client_module``. OpenAI-compatible providers are all reached through
    #: the ``openai`` client, so the class name proves nothing — only the base
    #: URL says who is on the other end. Empty means "the default for this
    #: module".
    client_base_url_hosts: frozenset[str] = frozenset()
    identity_param: str = "user_id"
    ubt_param: str = "ubt"
    token_header: str = DEFAULT_CONSENT_HEADER
    param_target: str = "kwargs"  # "kwargs" | "headers"
    headers_kwarg: str = "extra_headers"
    protected_params: frozenset[str] = frozenset()
    #: Path to the call argument carrying agentic tool definitions, e.g.
    #: ``("tools",)`` or ``("config", "tools")`` for google-genai. Read — never
    #: written — to honour a ``tools`` constraint from the cloud. ``None`` means
    #: the SDK cannot see this provider's tools, and so cannot honour it.
    tools_param: tuple[str, ...] | None = None
    #: Headers a caller may never set on an enforced call, as declared: the ones
    #: that choose which credential, account or project the call goes out on.
    #: Compare through :meth:`is_reserved_header`, which is case-insensitive and
    #: always reserves ``token_header`` as well.
    reserved_headers: tuple[str, ...] = ()
    #: How several credentials with different privacy postures can be held for
    #: this provider, or ``None`` when it offers no such thing — in which case
    #: ``consent.wrap(credentials=…)`` is refused rather than quietly ignored.
    credential_routing: CredentialRouting | None = None
    descriptor_version: str = "2"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderDescriptor:
        return cls(
            name=data["name"],
            client_module=data["client_module"],
            call_sites=tuple(CallSite.from_dict(s) for s in data["call_sites"]),
            client_base_url_hosts=frozenset(data.get("client_base_url_hosts", ())),
            identity_param=data.get("identity_param", "user_id"),
            ubt_param=data.get("ubt_param", "ubt"),
            token_header=data.get("token_header", DEFAULT_CONSENT_HEADER),
            param_target=data.get("param_target", "kwargs"),
            headers_kwarg=data.get("headers_kwarg", "extra_headers"),
            protected_params=frozenset(data.get("protected_params", ())),
            tools_param=(
                tuple(data["tools_param"]) if data.get("tools_param") else None
            ),
            reserved_headers=tuple(data.get("reserved_headers", ())),
            credential_routing=(
                CredentialRouting.from_dict(data["credential_routing"])
                if data.get("credential_routing")
                else None
            ),
            descriptor_version=data.get("descriptor_version", "2"),
        )

    def is_reserved_header(self, name: str) -> bool:
        """Whether a caller-supplied header ``name`` must be refused.

        Case-insensitive, because HTTP header names are, and because the
        provider libraries merge header maps with case-sensitive dict keys:
        ``authorization`` next to ``Authorization`` is two headers on the wire,
        and a list compared by exact spelling would miss the second.
        """
        wanted = name.lower()
        return wanted == self.token_header.lower() or any(
            wanted == reserved.lower() for reserved in self.reserved_headers
        )

    def call_site(self, site_id: str) -> CallSite | None:
        """Look one call site up by its wire id."""
        return next((s for s in self.call_sites if s.id == site_id), None)


def load_descriptors() -> dict[str, ProviderDescriptor]:
    """Auto-discover and parse every ``*.json`` descriptor in this package."""
    descriptors: dict[str, ProviderDescriptor] = {}
    for entry in resources.files(__name__).iterdir():
        if entry.name.endswith(".json"):
            data = json.loads(entry.read_text(encoding="utf-8"))
            descriptor = ProviderDescriptor.from_dict(data)
            descriptors[descriptor.name] = descriptor
    return descriptors
