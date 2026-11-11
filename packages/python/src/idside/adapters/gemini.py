# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""Built-in adapter for Google's ``google-genai`` client.

``models.generate_content`` (and its ``_stream`` variant) accepts exactly
``(model, contents, config)`` — no free-form kwargs — so the generic
kwargs/headers engine cannot inject anything at the top level. Per-request
headers ARE supported, but only nested as ``config.http_options.headers``
(the google-genai transport merges them over the client-level headers).

This adapter therefore overrides :meth:`apply` to (1) merge the cloud-forced
params into ``config`` — that is where Gemini's generation parameters live —
and (2) carry the ACT token in ``config.http_options.headers``. The caller's
``config`` may be a plain dict or a ``GenerateContentConfig`` (Pydantic v2)
object; both are normalized to the dict form google-genai equally accepts,
without mutating the caller's objects.
"""

from __future__ import annotations

import functools
import importlib
from typing import Any

from idside.adapters.base import ProviderAdapter, _merge_headers
from idside.exceptions import ConsentTokenError


@functools.cache
def _resumes_on_its_own() -> bool:
    """Whether the installed google-genai follows a ``CONTINUATION`` by itself.

    From google-genai 2.30, ``generate_content`` and its stream answer a
    response that stopped with ``finish_reason=CONTINUATION`` by sending the
    same request again with its ``continuation_token`` — on by default, under
    the same config, so under the same ACT token. Read off the library's own
    config model rather than its version number: an older one has no such
    field, and would refuse a config naming it.
    """
    try:
        types = importlib.import_module("google.genai.types")
    except ImportError:
        return False
    fields = getattr(types.GenerateContentConfig, "model_fields", {})
    return "automatic_continuation" in fields


def _as_dict(value: Any, what: str) -> dict[str, Any]:
    """Return ``value`` as a fresh plain dict (None → {}, Pydantic → dump)."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        # exclude_none: in google-genai config models, None means "unset".
        dumped = model_dump(exclude_none=True)
        if isinstance(dumped, dict):
            return dumped
    raise ConsentTokenError(
        f"Cannot enforce consent on this Gemini call: {what} of type "
        f"'{type(value).__name__}' is neither a dict nor a Pydantic model."
    )


class GeminiAdapter(ProviderAdapter):
    """Descriptor-driven engine specialized for ``google-genai`` mechanics."""

    def apply(
        self,
        kwargs: dict[str, Any],
        params_forced: dict[str, Any],
        token: str,
        credential_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Merge forced params into ``config``; token via ``http_options.headers``.

        ``protected_params`` applies to the forced keys before they touch the
        config, exactly as in the generic engine. Does not mutate the input.

        ``credential_headers`` is carried in the same nested header map, for
        completeness: google-genai reads no header that selects a project, so
        Gemini routing means a second client rather than a header, and this stays
        empty in practice.
        """
        new = dict(kwargs)
        safe = {
            k: v
            for k, v in params_forced.items()
            if k not in self.descriptor.protected_params
        }
        config = _as_dict(new.get("config"), "config")
        config.update(safe)
        if _resumes_on_its_own():
            # One token proves one request (invariant #4). An explicit `True`
            # was refused before the cloud was asked; this turns off the
            # default. The answer then comes back with its continuation token,
            # and resuming is a call of its own. Local to the library, never
            # sent, and set after the hash, so no request hash moves.
            config.pop("automaticContinuation", None)
            config["automatic_continuation"] = False
        # google-genai also takes `httpOptions`, and refuses a config holding
        # both spellings: the token must join the caller's own options, not sit
        # beside them under the other name.
        http_options = {
            **_as_dict(config.pop("httpOptions", None), "config.httpOptions"),
            **_as_dict(config.get("http_options"), "config.http_options"),
        }
        injected_headers = {self.descriptor.token_header: token}
        if credential_headers:
            injected_headers.update(credential_headers)
        http_options["headers"] = _merge_headers(
            http_options.get("headers"), injected_headers
        )
        config["http_options"] = http_options
        new["config"] = config
        return new
