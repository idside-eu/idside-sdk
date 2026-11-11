# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""The options a consent-enforced call may carry, and the ones it may not.

A provider library accepts two kinds of keyword on a call. Request parameters
— ``model``, ``messages``, ``tools``, ``store`` — are the request: the SDK hashes
them, the cloud forces some of them, and the proof describes them. Request
*options* are instructions to the library about how to send it, and several act
after everything the SDK does. ``extra_body`` is merged into the body once it is
built, so ``extra_body={"store": True}`` re-enabled the retention a user had
refused while the signed proof said ``store: false``. ``extra_query``,
``server_url`` and ``http_options.base_url`` move the request, the ACT token and
the API key to another address. A header can pick another credential.

So the options are a closed list, per library. An option is let through only
when it is known to change nothing a proof covers — a timeout, a retry policy,
a header map holding no reserved header — and everything else is refused with
:class:`UnenforceableOptionError` before the cloud is asked. Closed, rather
than a list of known offenders, because the offenders were found by
experiment, and the next version of a library can add one nobody has tried.

Telling an option from a parameter needs the library's own word for what the
body is: the body schema it validates the call against (a ``TypedDict`` for
the Stainless clients, a Pydantic request model for Speakeasy). It is read at
run time from the installed library, so an option added by a version newer
than this SDK is still refused, and a *parameter* added by one still goes out.
``tests/test_request_options.py`` computes the same split from the pinned
libraries and fails when one gains an option nobody has classified here.

A schema can also move — ``openai 7`` relocating ``CompletionCreateParamsBase``
would do it — and that is the same event by another name: a library version
newer than this SDK. So every classified library also carries ``body_fields``,
the schema as the pinned version declared it, and an unreadable schema falls
back on that rather than on anything open. Whatever cannot be read, the list of
what an enforced call may carry only ever narrows.

Nothing here reads a value to decide anything but its presence, and no message
repeats one: a header value is as likely as not a key.
"""

from __future__ import annotations

import functools
import importlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from idside.exceptions import (
    CredentialRoutingError,
    UnenforceableOptionError,
    UnsupportedProviderError,
)
from idside.providers import ProviderDescriptor

#: The class names the provider libraries give their "not given" sentinels:
#: ``NOT_GIVEN`` and ``omit`` in openai and anthropic, ``UNSET`` in mistralai.
#: Named rather than inferred from falsiness, because a falsy value of some
#: other class — ``numpy.float64(0.0)`` for a temperature — is a real argument.
_SENTINEL_TYPES = frozenset({"NotGiven", "Omit", "Unset"})


def is_sentinel(value: Any) -> bool:
    """Whether ``value`` is a library's way of saying "this was not passed"."""
    return type(value).__name__ in _SENTINEL_TYPES and not value


def is_not_given(value: Any) -> bool:
    """``None`` or a sentinel: an argument that asks for nothing."""
    return value is None or is_sentinel(value)


class _Library(NamedTuple):
    """What one client library's calls accept besides their body.

    ``permitted`` is the whole list of options that pass. ``refused`` names the
    ones known to rewrite or redirect the request, and is what the trip-wire
    test checks the library against. ``transport`` are the permitted options
    whose value is an object rather than JSON (``httpx.Timeout``,
    ``RetryConfig``): they decide how the request travels, not what it says, and
    stay out of the hash when they cannot be written into it. ``body_schemas``
    maps a call site to where the library declares that endpoint's body, as
    ``module:attribute`` candidates tried in order.

    ``body_fields`` is the same map, resolved: the field names the pinned
    library declared when this release was cut. It exists for the day a library
    moves its schema — which is to say, for a version newer than this SDK, the
    one case invariant #8 is about. Reading the live schema is what lets a
    parameter invented after this release still go out; falling back on a list
    of known offenders when it cannot be read let every *option* invented after
    this release go out too, on every call, silently. So the fallback is this
    snapshot, which is closed: a new option is in neither it nor ``permitted``.
    ``tests/test_request_options.py`` compares it with the live schema, so it
    fails the day the two drift rather than the day a customer finds out.

    ``refused_fields`` are names the body schema declares and an enforced call
    may not carry all the same. Two reasons put a field there, and the trip-wire
    looks for the first one by itself. A library can declare a field in the
    schema it type-checks and send it somewhere else: ``anthropic>=2`` takes
    ``workspace_id`` beside the body and writes it into a header, where the
    request hash does not reach and where it picks the workspace — the unit
    credential routing chooses. Or a field can undo, from inside the body, a
    refusal the cloud cannot force back: a ``prompt_cache_options.ttl`` of
    ``12h`` is a cache lifetime the forced ``prompt_cache_retention`` does not
    bound.
    """

    permitted: frozenset[str]
    refused: frozenset[str]
    transport: frozenset[str]
    body_schemas: Mapping[str, tuple[str, ...]]
    body_fields: Mapping[str, frozenset[str]]
    refused_fields: frozenset[str] = frozenset()


def _fields(names: str) -> frozenset[str]:
    """One endpoint's frozen body field names, written as one string.

    Generated from the installed library, kept in the source rather than in a
    data file so that a change to what an enforced call may carry shows up in a
    diff someone reviews.
    """
    return frozenset(names.split())


def _mistral_schema(model: str) -> tuple[str, ...]:
    # mistralai 2 moved its models under `client`; 1.x kept them at the root.
    return (f"mistralai.client.models:{model}", f"mistralai.models:{model}")


#: Keyed by the descriptor's ``client_module``: a library, not a provider.
#: DeepSeek is reached through the ``openai`` client and inherits its options.
LIBRARIES: Mapping[str, _Library] = {
    "openai": _Library(
        permitted=frozenset(
            {
                "timeout",
                "extra_headers",
                # Helper arguments of `parse` and `stream`: they shape what the
                # library builds or reads, and none of them is a way around it.
                "text_format",
                "verbosity",
                "response_id",
                "starting_after",
                # `responses.parse()`: the type the output is parsed INTO, read
                # in `_output_text_parser` and never sent. Hashed as its JSON
                # schema, as `text_format` is.
                "parse_as",
            }
        ),
        refused=frozenset({"extra_body", "extra_query"}),
        transport=frozenset({"timeout"}),
        body_schemas={
            "chat.completions.create": (
                "openai.types.chat.completion_create_params:CompletionCreateParamsBase",
            ),
            "responses.create": (
                "openai.types.responses.response_create_params"
                ":ResponseCreateParamsBase",
            ),
        },
        body_fields={
            "chat.completions.create": _fields(
                "audio frequency_penalty function_call functions logit_bias "
                "logprobs max_completion_tokens max_tokens messages "
                "metadata modalities model moderation n parallel_tool_calls "
                "prediction presence_penalty prompt_cache_key "
                "prompt_cache_options prompt_cache_retention reasoning_effort "
                "response_format safety_identifier seed service_tier stop "
                "store stream_options temperature tool_choice tools "
                "top_logprobs top_p user verbosity web_search_options"
            ),
            "responses.create": _fields(
                "access_programs background context_management conversation "
                "image include input instructions max_output_tokens "
                "max_tool_calls metadata model moderation parallel_tool_calls "
                "previous_response_id prompt prompt_cache_key "
                "prompt_cache_options prompt_cache_retention reasoning "
                "safety_identifier service_tier store stream_options "
                "temperature text tool_choice tools top_logprobs top_p "
                "truncation user"
            ),
        },
        refused_fields=frozenset({"prompt_cache_options"}),
    ),
    "anthropic": _Library(
        permitted=frozenset({"timeout", "extra_headers", "output_format"}),
        refused=frozenset({"extra_body", "extra_query"}),
        transport=frozenset({"timeout"}),
        body_schemas={
            "messages.create": (
                "anthropic.types.message_create_params:MessageCreateParamsBase",
            ),
        },
        body_fields={
            "messages.create": _fields(
                "cache_control container diagnostics inference_geo max_tokens "
                "messages metadata model output_config service_tier speed "
                "stop_sequences system thinking tool_choice tools "
                "user_profile_id workspace_id"
            ),
        },
        refused_fields=frozenset({"workspace_id", "user_profile_id"}),
    ),
    "mistralai": _Library(
        permitted=frozenset({"http_headers", "retries", "timeout_ms"}),
        refused=frozenset({"server_url"}),
        transport=frozenset({"retries", "timeout_ms"}),
        body_schemas={
            "chat.complete": _mistral_schema("ChatCompletionRequest"),
            "chat.stream": _mistral_schema("ChatCompletionStreamRequest"),
            "fim.complete": _mistral_schema("FIMCompletionRequest"),
            "fim.stream": _mistral_schema("FIMCompletionStreamRequest"),
            "agents.complete": _mistral_schema("AgentsCompletionRequest"),
            "agents.stream": _mistral_schema("AgentsCompletionStreamRequest"),
            "beta.conversations.start": _mistral_schema("ConversationRequest"),
        },
        body_fields={
            "chat.complete": _fields(
                "frequency_penalty guardrails logprobs max_tokens messages "
                "metadata min_tokens model n parallel_tool_calls prediction "
                "presence_penalty prompt_cache_key prompt_logprobs "
                "prompt_mode random_seed reasoning_effort repetition_penalty "
                "response_format safe_prompt service_tier stop stream "
                "temperature tool_choice tools top_k top_logprobs top_p "
                "top_prompt_logprobs"
            ),
            "chat.stream": _fields(
                "frequency_penalty guardrails logprobs max_tokens messages "
                "metadata min_tokens model n parallel_tool_calls prediction "
                "presence_penalty prompt_cache_key prompt_logprobs "
                "prompt_mode random_seed reasoning_effort repetition_penalty "
                "response_format safe_prompt service_tier stop stream "
                "temperature tool_choice tools top_k top_logprobs top_p "
                "top_prompt_logprobs"
            ),
            "fim.complete": _fields(
                "max_tokens metadata min_tokens model prompt "
                "prompt_cache_key random_seed stop stream suffix "
                "temperature top_p"
            ),
            "fim.stream": _fields(
                "max_tokens metadata min_tokens model prompt "
                "prompt_cache_key random_seed stop stream suffix "
                "temperature top_p"
            ),
            "agents.complete": _fields(
                "agent_id frequency_penalty guardrails logprobs max_tokens "
                "messages metadata min_tokens n parallel_tool_calls "
                "prediction presence_penalty prompt_cache_key prompt_logprobs "
                "prompt_mode random_seed reasoning_effort repetition_penalty "
                "response_format service_tier stop stream tool_choice tools "
                "top_k top_logprobs top_prompt_logprobs"
            ),
            "agents.stream": _fields(
                "agent_id frequency_penalty guardrails logprobs max_tokens "
                "messages metadata min_tokens n parallel_tool_calls "
                "prediction presence_penalty prompt_cache_key prompt_logprobs "
                "prompt_mode random_seed reasoning_effort repetition_penalty "
                "response_format service_tier stop stream tool_choice tools "
                "top_k top_logprobs top_prompt_logprobs"
            ),
            "beta.conversations.start": _fields(
                "agent_id agent_version completion_args description "
                "guardrails handoff_execution inputs instructions metadata "
                "model name store stream tools"
            ),
        },
    ),
}

#: ``google-genai`` takes exactly ``model``, ``contents`` and ``config``, so its
#: options live inside ``config``: the fields no request converter reads.
#: ``should_return_http_response`` changes what the library hands back, not
#: what it sends. ``automatic_function_calling`` configures a loop that only
#: runs when a tool is a local callable — and that is refused on its own,
#: whatever this says, because one token would then cover several requests.
#: ``automatic_continuation`` (google-genai 2.30 and later) is that loop again
#: without any tool, and ON by default: a response that stops with
#: ``finish_reason=CONTINUATION`` is followed by the same request carrying its
#: ``continuation_token``, under the same config and so the same ACT token.
#: Asking for it is refused, and the Gemini adapter turns it off on every
#: enforced call; resuming is a call of its own, with ``continuation_token``,
#: authorized and proven like any other. ``http_options`` is inspected field by
#: field below.
GENAI_MODULE = "google.genai"
GENAI_CONFIG_OPTIONS = frozenset(
    {
        "http_options",
        "should_return_http_response",
        "automatic_function_calling",
        "automatic_continuation",
    }
)
#: Config fields the converters DO send, and that an enforced call may not carry
#: anyway. ``generation_output_config.output_uri_prefix`` has the provider write
#: the generated media to a ``gs://`` location of the caller's choosing and hand
#: back a link instead of the bytes: a copy of the output, kept somewhere the
#: proof never names, for as long as the bucket keeps it.
GENAI_CONFIG_REFUSED = frozenset({"generation_output_config"})
#: The only arguments ``models.generate_content(_stream)`` accepts. Anything
#: else is refused before the cloud is asked, rather than failing as a
#: ``TypeError`` inside the provider library after a proof was signed for it.
GENAI_ARGUMENTS = frozenset({"model", "contents", "config"})
#: ``config.http_options`` holds twelve fields and three of them are harmless.
#: The other nine choose the address, the path, the body or the HTTP client
#: itself.
GENAI_HTTP_PERMITTED = frozenset({"headers", "timeout", "retry_options"})
GENAI_HTTP_REFUSED = frozenset(
    {
        "base_url",
        "base_url_resource_scope",
        "api_version",
        "client_args",
        "async_client_args",
        "extra_body",
        "httpx_client",
        "httpx_async_client",
        "aiohttp_client",
    }
)

#: Refused for a library this SDK has no classification for at all. Not a closed
#: list — with neither a schema nor a snapshot, a new option cannot be told from
#: a new body field — but the options every client library in this space uses to
#: rewrite or redirect a request are not left open on that account.
#: ``http_options`` (top level, or inside ``config``) is inspected field by field
#: as for Gemini.
#:
#: A CLASSIFIED library never reaches this list. That it used to, whenever the
#: live body schema could not be read, was a hole with exactly one trigger: a
#: library version newer than this SDK, moving its schema. From that version on,
#: every option nobody had classified passed on every call — the case invariant
#: #8 is written for — and the trip-wire could not see it, since it only ever
#: runs against the pinned version. The fallback is now ``body_fields``.
KNOWN_OFFENDERS = frozenset({"extra_body", "extra_query", "server_url", "base_url"})

#: Why a known option is refused, in words a developer can act on.
_WHY = {
    "extra_body": "rewrites the request body after it has been authorized",
    "extra_query": "adds to the URL of a request that has already been authorized",
    "server_url": "sends the request to a server this client was not wrapped for",
    "base_url": "sends the request to a server this client was not wrapped for",
    "base_url_resource_scope": "changes the URL the request is sent to",
    "api_version": "rewrites the request path, and with it the model it reaches",
    "client_args": "builds another HTTP client for this request",
    "async_client_args": "builds another HTTP client for this request",
    "httpx_client": "sends this request on another HTTP client",
    "httpx_async_client": "sends this request on another HTTP client",
    "aiohttp_client": "sends this request on another HTTP client",
    "workspace_id": (
        "is sent as the `anthropic-workspace-id` header, outside the hashed body, "
        "and moves the call to another workspace — the unit credential routing "
        "chooses, with its own retention and residency"
    ),
    "user_profile_id": (
        "is sent as the `anthropic-user-profile-id` header, outside the hashed "
        "body, and attributes the call to a user profile whose programs can "
        "require retention and human review"
    ),
    "prompt_cache_options": (
        "sets a prompt-cache lifetime (`ttl`) that the forced "
        "`prompt_cache_retention` does not bound and the SDK cannot force"
    ),
    "generation_output_config": (
        "has the provider write the generated media to a storage bucket the "
        "proof never names"
    ),
    "automatic_continuation": (
        "makes the library send the request again, on its own, under the one "
        "ACT token authorized for this call, and one token proves one request"
    ),
}
_WHY_UNKNOWN = "is not an option this SDK knows to leave the request as authorized"


def transport_options(descriptor: ProviderDescriptor) -> frozenset[str]:
    """Options whose non-JSON value is left out of the request hash."""
    library = LIBRARIES.get(descriptor.client_module)
    return library.transport if library is not None else frozenset()


def refuse_unenforceable(
    descriptor: ProviderDescriptor, call_site: str, kwargs: Mapping[str, Any]
) -> None:
    """Raise if this call carries an option enforcement cannot stand behind.

    ``kwargs`` are the call's arguments with the identity already consumed.
    Raises :class:`UnenforceableOptionError`; returns nothing otherwise.
    """
    _refuse_reserved_headers(
        descriptor, descriptor.headers_kwarg, kwargs.get(descriptor.headers_kwarg)
    )
    if descriptor.client_module == GENAI_MODULE:
        for name, value in kwargs.items():
            if name not in GENAI_ARGUMENTS and not is_not_given(value):
                _refuse_option(name, name)
        _refuse_genai_config(descriptor, kwargs.get("config"))
        return
    library = LIBRARIES.get(descriptor.client_module)
    if library is None:
        _refuse_known_offenders(descriptor, kwargs)
        return
    body = _body_fields(library.body_schemas.get(call_site, ()))
    if body is None:
        body = _frozen_body(descriptor.client_module, call_site)
    for name, value in kwargs.items():
        if name in library.permitted or name == "stream" or is_not_given(value):
            continue
        if name in library.refused_fields or name not in body:
            _refuse_option(name, name)


@functools.cache
def _frozen_body(module: str, call_site: str) -> frozenset[str]:
    """What this SDK was built to accept on an endpoint whose schema will not read.

    The endpoint's own snapshot when there is one. Otherwise everything the
    library declares anywhere — the narrowest thing that can honestly be said
    about an endpoint nobody classified, and still closed: an option a newer
    version adds is in none of its endpoints' bodies, so it is refused, while
    the ordinary parameters of a call go out as they always did. Passing a
    parameter of a *different* endpoint is then the library's own ``TypeError``,
    which is the developer's mistake and not a proof describing another request.
    """
    library = LIBRARIES[module]
    frozen = library.body_fields.get(call_site)
    if frozen is not None:
        return frozen
    return frozenset().union(*library.body_fields.values())


def _refuse_known_offenders(
    descriptor: ProviderDescriptor, kwargs: Mapping[str, Any]
) -> None:
    """The fallback for a library this SDK classifies nothing about.

    Refuses the names known to rewrite or redirect a request, and reads any
    ``http_options`` — given directly or inside ``config`` — as google-genai's.
    """
    for name, value in kwargs.items():
        if is_not_given(value):
            continue
        if name in KNOWN_OFFENDERS:
            _refuse_option(name, name)
        elif name == "http_options":
            _refuse_http_options(descriptor, "http_options", value)
        elif name == "config":
            for raw_name, nested in (mapping_view(value) or {}).items():
                if _snake(str(raw_name)) == "http_options" and not is_not_given(nested):
                    _refuse_http_options(descriptor, "config.http_options", nested)


@functools.cache
def _body_fields(candidates: tuple[str, ...]) -> frozenset[str] | None:
    """The body fields a library declares for one endpoint, or None.

    Cached per endpoint: the schema is a class definition, and it does not
    change while the process runs.
    """
    for candidate in candidates:
        module_name, _, attribute = candidate.partition(":")
        try:
            schema = getattr(importlib.import_module(module_name), attribute)
        except (ImportError, AttributeError):
            continue
        fields = getattr(schema, "model_fields", None)
        if isinstance(fields, Mapping):
            return frozenset(fields)
        required = getattr(schema, "__required_keys__", None)
        optional = getattr(schema, "__optional_keys__", None)
        if isinstance(required, frozenset) and isinstance(optional, frozenset):
            return frozenset(required | optional)
    return None


def _refuse_option(where: str, name: str) -> None:
    raise UnenforceableOptionError(
        f"`{where}` cannot be used on a consent-enforced call: it "
        f"{_WHY.get(name, _WHY_UNKNOWN)}, so the request leaving the process "
        "would not be the one the proof describes. The AI call was not made and "
        "ID Side was not asked. Pass request parameters as named arguments of the "
        "call, and set where the client sends when you build it."
    )


class _Derivation(NamedTuple):
    """What ``with_options()``/``copy()`` may be asked on one library's clients.

    A derived client is built in one line from the wrapped one, and its
    construction options redo, for every call it will ever make, what the
    per-call list refuses: ``http_client=`` brings a transport of its own,
    ``default_query=`` rewrites every URL, ``api_key=`` or ``project=`` moves the
    calls onto another credential, ``middleware=`` rewrites requests. So these
    are a closed list too, classified parameter by parameter against the real
    signatures (``tests/test_request_options.py`` pins them):

    - ``harmless`` pass: they touch neither the transport, the credential, the
      URL nor the headers;
    - ``checked`` pass when the result holds: ``base_url`` must leave the origin
      unchanged, a header map may hold no reserved header, and a ``middleware``
      list must be the one the client already runs;
    - ``credentials`` are refused with ``CredentialRoutingError``: which
      credential carries a call is routing's to decide;
    - everything else, known or not, is refused with
      ``UnsupportedProviderError``: a client that sends differently is a new
      client, to be built and wrapped on its own.
    """

    harmless: frozenset[str]
    checked: frozenset[str]
    credentials: frozenset[str]
    unsupported: frozenset[str]


_DERIVED_HEADERS = frozenset({"default_headers", "set_default_headers"})

#: The one derivation option judged against the client it is derived FROM, and
#: the only one a library passes to ``copy()`` on the developer's behalf.
_MIDDLEWARE = "middleware"

DERIVATIONS: Mapping[str, _Derivation] = {
    "openai": _Derivation(
        harmless=frozenset({"timeout", "max_retries", "webhook_secret"}),
        checked=frozenset({"base_url"}) | _DERIVED_HEADERS,
        credentials=frozenset(
            {
                "api_key",
                "admin_api_key",
                "workload_identity",
                "organization",
                "project",
                "azure_ad_token",
                "azure_ad_token_provider",
            }
        ),
        unsupported=frozenset(
            {
                "http_client",
                "default_query",
                "set_default_query",
                "websocket_base_url",
                "api_version",
                # `openai>=3`: a regional endpoint, or a third-party provider
                # that sets the authentication and the base URL. Either is
                # another destination: a client of its own, wrapped on its own.
                "data_residency",
                "provider",
                "_enforce_credentials",
                "_extra_kwargs",
            }
        ),
    ),
    "anthropic": _Derivation(
        # `open_telemetry` (anthropic>=2): the tracer, what a span records, and
        # whether requests carry the trace-context headers — the propagator's,
        # which the parent client already writes by default. What is traced,
        # like what is logged; nothing about where or on which key.
        harmless=frozenset({"timeout", "max_retries", "webhook_key", "open_telemetry"}),
        checked=frozenset({"base_url", _MIDDLEWARE}) | _DERIVED_HEADERS,
        credentials=frozenset(
            {
                "api_key",
                "auth_token",
                "credentials",
                "config",
                "profile",
                "aws_access_key",
                "aws_secret_key",
                "aws_session_token",
                "aws_profile",
                "access_token",
                "project_id",
                "azure_ad_token_provider",
                "workspace_id",
                "skip_auth",
            }
        ),
        unsupported=frozenset(
            {
                "http_client",
                "default_query",
                "set_default_query",
                "region",
                "aws_region",
                "_extra_kwargs",
            }
        ),
    ),
}


#: For a library ``DERIVATIONS`` does not classify: the same closed list, reduced
#: to what every client library names alike. Timeouts and retries pass,
#: ``base_url`` and the header maps are checked, every credential any known
#: library takes is routing's, and everything else is refused.
GENERIC_DERIVATION = _Derivation(
    harmless=frozenset({"timeout", "max_retries"}),
    checked=frozenset({"base_url"}) | _DERIVED_HEADERS,
    credentials=frozenset().union(*(d.credentials for d in DERIVATIONS.values())),
    unsupported=frozenset(),
)


def _arguments(
    method: str, args: tuple[Any, ...], kwargs: Mapping[str, Any]
) -> list[tuple[str, str, Any]]:
    """Each argument of a derivation as (option, what to call it, value).

    The middle element is how the developer wrote the call, and it is carried
    rather than rebuilt because those two can differ. ``with_middleware(m)``
    passes its middleware positionally, and an error naming a keyword the
    developer did not write — worse, naming ``copy()``, the method the library
    reaches it through — sends them looking for a line that does not exist.
    """
    given = [(name, f"{method}({name}=…)", value) for name, value in kwargs.items()]
    if args:
        # `with_middleware(*middleware)` is the only positional derivation, and
        # the arguments of any other one cannot be told apart.
        if method == "with_middleware":
            given.append((_MIDDLEWARE, f"{method}(…)", args))
        else:
            given.append(("*args", f"{method}(*args=…)", args))
    return given


def refuse_underivable(
    descriptor: ProviderDescriptor,
    method: str,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    client: Any = None,
) -> None:
    """Raise if a derivation asks for something a derived client may not do.

    Checked on the arguments, before the library builds anything. What can only
    be judged on the result — the origin ``base_url`` resolves to, the headers
    left once merged — is checked by ``consent.wrap()`` on the derived client.

    ``client`` is the client being derived FROM, which one option has to be read
    against: middleware is judged on what it would *change*, and only the parent
    says what it is changing from.
    """
    derivation = DERIVATIONS.get(descriptor.client_module, GENERIC_DERIVATION)
    for name, shown, value in _arguments(method, args, kwargs):
        if is_not_given(value) or name in derivation.harmless:
            continue
        if name in _DERIVED_HEADERS:
            _refuse_derived_headers(descriptor, shown, value)
        elif name == _MIDDLEWARE and name in derivation.checked:
            _refuse_new_middleware(shown, client, value)
        elif name in derivation.checked:
            continue
        elif name in derivation.credentials:
            raise CredentialRoutingError(
                f"`{shown}` would put the derived client on another "
                "credential. Which credential carries a call is decided by "
                "credential routing from the user's answer, never by a derived "
                "client: provision each credential and pass them to "
                "consent.wrap(client, credentials={…}) instead. The client was not "
                "derived."
            )
        else:
            raise UnsupportedProviderError(
                f"`{shown}` would change how or where the derived client "
                "sends — its transport, its URL or what runs before the request — "
                "and a derived client inherits the enforcement of the one it came "
                "from, not a different way out. Build a new client with that option "
                "and wrap it with consent.wrap(), so its provider is detected and "
                "its destination frozen afresh. The client was not derived."
            )


def _refuse_new_middleware(shown: str, client: Any, middleware: Any) -> None:
    """Let a middleware argument through only when it changes nothing.

    Middleware is application code the library runs between a request and the
    wire, after this SDK has authorized the call and below everything it checks,
    so a derived client that gains some sends differently from the one that was
    wrapped and needs a wrap of its own.

    Asking for the middleware a client already runs changes nothing, and it is
    what the library's own ``with_middleware()`` does when it is handed none:
    ``copy(middleware=[*self._middleware])``. Refusing that refused a derivation
    that adds nothing, over an argument the developer never wrote.

    Compared by identity and never by ``==``: an object is free to call itself
    equal to whatever it likes, and this decides whose code runs under an
    authorized call. ``None`` for ``client`` — nobody said what it is derived
    from — can only mean refuse.
    """
    current = getattr(client, "_middleware", None)
    if (
        isinstance(middleware, (list, tuple))
        and isinstance(current, (list, tuple))
        and len(middleware) == len(current)
        and all(new is old for new, old in zip(middleware, current, strict=True))
    ):
        return
    raise UnsupportedProviderError(
        f"`{shown}` would put middleware of its own between the derived client's "
        "requests and the wire: code running after ID Side has authorized a call "
        "and below everything the SDK checks. A derived client inherits the "
        "enforcement of the one it came from, not a different way out. Build a new "
        "client with that middleware and wrap it with consent.wrap(), so its "
        "provider is detected and its destination frozen afresh. The client was "
        "not derived."
    )


def _refuse_derived_headers(
    descriptor: ProviderDescriptor, shown: str, headers: Any
) -> None:
    names = header_names(headers)
    reserved = (
        [header for header in names if descriptor.is_reserved_header(header)]
        if names is not None
        else ["an unreadable header map"]
    )
    if reserved:
        raise CredentialRoutingError(
            f"`{shown}` sets {reserved[0]!s}, which a derived client may "
            "not: those headers choose the credential or carry the consent token, "
            "and credential routing is what sets them. Build the client with them "
            "instead, or route with consent.wrap(client, credentials={…}). The "
            "client was not derived."
        )


def header_names(headers: Any) -> list[str] | None:
    """Every header name in a header map, or None if it cannot be read.

    Every shape a library accepts: a mapping (``httpx.Headers`` included), a
    sequence of pairs, names as ``bytes``.

    A pair is a tuple or a list, never any sequence of length two: ``"Au"`` has
    one, and ``["Au", "th"]`` read as the headers ``A`` and ``t`` — nothing
    reserved, so nothing refused, from a map nobody can read.
    """
    if isinstance(headers, Mapping):
        raw: list[Any] = list(headers.keys())
    elif isinstance(headers, Sequence) and not isinstance(headers, (str, bytes)):
        if not all(
            isinstance(pair, (tuple, list)) and len(pair) == 2 for pair in headers
        ):
            return None
        raw = [pair[0] for pair in headers]
    else:
        return None
    names: list[str] = []
    for name in raw:
        if isinstance(name, bytes):
            name = name.decode("latin-1")
        if not isinstance(name, str):
            return None
        names.append(name)
    return names


def _refuse_reserved_headers(
    descriptor: ProviderDescriptor, where: str, headers: Any
) -> None:
    """Refuse a header that picks the credential, or forges the ACT token.

    A value that removes such a header (``omit``) is refused too: taking
    ``X-Api-Key`` off a call changes the key it goes out on as surely as
    replacing it.
    """
    if is_not_given(headers):
        return
    names = header_names(headers)
    if names is None:
        raise UnenforceableOptionError(
            f"`{where}` is not a header map the SDK can read, so it cannot check "
            "that no header in it chooses the credential or carries a consent "
            "token. The AI call was not made. Pass a dict of header names to "
            "values."
        )
    reserved = [name for name in names if descriptor.is_reserved_header(name)]
    if not reserved:
        return
    token_header = descriptor.token_header.lower()
    token = [name for name in reserved if name.lower() == token_header]
    what = (
        "carries the ACT token, which only the SDK may set"
        if token
        else "chooses the credential, account or project the call goes out on"
    )
    raise UnenforceableOptionError(
        f"`{where}` sets `{(token or reserved)[0]}`, a header that {what}. "
        "Changing it on one call would send the request on terms the proof does "
        "not describe, and credential routing could no longer hold. The AI call "
        "was not made and ID Side was not asked. Set credentials when you build "
        "the client, and route between several with consent.wrap(credentials=…)."
    )


# ── google-genai ────────────────────────────────────────────────────────────


def _snake(name: str) -> str:
    """``httpOptions`` → ``http_options``: google-genai accepts both spellings."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def mapping_view(value: Any) -> dict[str, Any] | None:
    """The dict google-genai will be handed for ``value``, or None.

    The same reading ``GeminiAdapter.apply`` makes: a dict as it is, a Pydantic
    model as its ``model_dump(exclude_none=True)``. Inspecting anything else
    would inspect something other than what is sent.
    """
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump(exclude_none=True)
        if isinstance(dumped, dict):
            return dumped
    return None


def _refuse_genai_config(descriptor: ProviderDescriptor, config: Any) -> None:
    view = mapping_view(config)
    if view is None:
        return  # no config, or one `apply()` refuses on its own
    for raw_name, value in view.items():
        name = _snake(raw_name) if isinstance(raw_name, str) else raw_name
        if name == "tools":
            _refuse_local_tools(value)
        elif name == "http_options" and not is_not_given(value):
            _refuse_http_options(descriptor, "config.http_options", value)
        elif name == "automatic_continuation" and value:
            _refuse_option("config.automatic_continuation", name)
        elif name in GENAI_CONFIG_REFUSED and not is_not_given(value):
            _refuse_option(f"config.{name}", name)


def _refuse_http_options(
    descriptor: ProviderDescriptor, prefix: str, value: Any
) -> None:
    http_options = mapping_view(value)
    if http_options is None:
        raise UnenforceableOptionError(
            f"`{prefix}` is neither a dict nor an HttpOptions, so the SDK cannot "
            "check where it sends the request. The AI call was not made."
        )
    for raw_name, option in http_options.items():
        name = _snake(raw_name) if isinstance(raw_name, str) else str(raw_name)
        where = f"{prefix}.{name}"
        if name == "headers":
            _refuse_reserved_headers(descriptor, where, option)
        elif name not in GENAI_HTTP_PERMITTED and not is_not_given(option):
            _refuse_option(where, name)


def refuse_declared_rewrites(client: Any, descriptor: ProviderDescriptor) -> None:
    """Refuse a client BUILT to rewrite what the SDK authorizes.

    Not code — data given to the client at construction, which the library then
    applies to every request below the guard. ``genai.Client(http_options=
    {"extra_body": …})`` merges that body into each call, the ACT token going
    out with it; ``http_options={"headers": {"X-Goog-Api-Key": …}}`` sits beside
    the key the library sets itself, as a second one. Both are read off the
    options the client holds, so they are refused at ``wrap()`` rather than
    discovered on the wire.

    ``openai``, ``anthropic`` and ``mistralai`` have no client-level body to
    declare: ``extra_body`` exists there only per request, where it is refused
    per call.
    """
    if descriptor.client_module != GENAI_MODULE:
        return
    state = getattr(client, "__dict__", None)
    api_client = state.get("_api_client") if isinstance(state, dict) else None
    options = getattr(api_client, "_http_options", None)
    if getattr(options, "extra_body", None):
        raise UnsupportedProviderError(
            "This google-genai client was built with `http_options.extra_body`, "
            "which the library merges into every request body after the SDK has "
            "authorized it — the proof would describe requests that never left "
            "the process. The client was not wrapped: build it without "
            "`extra_body`, and pass parameters as named arguments of each call."
        )
    headers = getattr(options, "headers", None)
    own_key = getattr(api_client, "api_key", None)
    for name, value in (headers if isinstance(headers, Mapping) else {}).items():
        # The one reserved header google-genai writes itself, from `api_key`.
        if name == "x-goog-api-key" and value == own_key:
            continue
        if isinstance(name, str) and descriptor.is_reserved_header(name):
            raise CredentialRoutingError(
                f"This google-genai client was built with `{name}` in "
                "`http_options.headers`, a header that chooses the credential or "
                "carries the consent token: every call would go out with it, "
                "beside or instead of what the SDK and credential routing set. The "
                "client was not wrapped: pass the key as `api_key`, and route "
                "between credentials with consent.wrap(client, credentials={…})."
            )


def _refuse_local_tools(tools: Any) -> None:
    """Refuse a tool google-genai would run on the model's behalf.

    A Python callable, or an MCP client session, turns on automatic function
    calling: the library sends the request, runs the function the model asked
    for, and sends again — up to ten times — all under the one ACT token
    authorized for this call. One token proves one request (invariant #4), so
    this is refused whatever the user answered about tools.
    """
    if not isinstance(tools, (list, tuple)):
        return
    for index, tool in enumerate(tools):
        kind = (
            "an MCP client session"
            if _is_mcp_session(tool)
            else "a Python callable"
            if callable(tool)
            else None
        )
        if kind is None:
            continue
        raise UnenforceableOptionError(
            f"`config.tools[{index}]` is {kind}, which turns on google-genai's "
            "automatic function calling: the library would send several requests "
            "under the single ACT token authorized for this one, and one token "
            "proves one request. The AI call was not made. Declare the function "
            "instead (types.FunctionDeclaration) and run the tool loop yourself: "
            "each turn is then authorized and proven."
        )


def _is_mcp_session(tool: Any) -> bool:
    """``mcp.ClientSession``, recognised without importing ``mcp``."""
    return any(
        cls.__name__ == "ClientSession" and cls.__module__.split(".")[0] == "mcp"
        for cls in type(tool).__mro__
    )
