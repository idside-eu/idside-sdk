"""Observability: enough to debug a call that did not go out, and no more.

A fail-closed SDK with no journal is undebuggable at a customer site. When a
call does not leave, the developer has no way to learn which option was forced,
which call site was recognised, how long the authorization took, or whether the
cloud was even reached. That is what this adds.

What it must never do matters more than what it adds, so that is what most of
this file is about:

- **Nothing secret ever reaches a log.** Not an API key, not the end user's
  identifier in the clear, not a UBT, not a prompt, not an ACT token. A
  credential is a secret (invariant #7) and so is the person.
- **Silent by default.** The SDK emits records; it configures no handler and
  writes to no stream unless the application asks. A library that prints
  uninvited is a library that gets removed.
- **Observation is not influence** (invariant #8). A logging handler is
  arbitrary application code running in the middle of the enforcement path. It
  must not be able to reach the decision — that would be the opaque tunnel
  through the back door, wearing a logger's clothes.

The net (`_guard.py`) logs NOTHING, and that is tested here too: it is a blind
detector on the request path of a product whose premise is that we do not see
the request.
"""

from __future__ import annotations

import asyncio
import logging
import re
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from idside import _log, consent
from idside._transport import AsyncHttpConsentClient, ConsentDecision, HttpConsentClient
from idside.adapters import ProviderAdapter
from idside.exceptions import (
    ConsentUnreachableError,
    IDSideError,
    MissingCredentialError,
    ToolUseRefusedError,
    UnenforcedCallError,
)
from idside.providers import load_descriptors

_OPENAI = load_descriptors()["openai"]

#: Every secret this suite hands the SDK. None of them may appear in a record.
_API_KEY = "sk_idside_supersecret_key"  # pragma: allowlist secret
_PROVIDER_KEY = "sk-provider_supersecret_key"  # pragma: allowlist secret
_USER_ID = "usr_marie_456"
_UBT = "eyJhbGciOiJFUzI1NiJ9.dXNyX21hcmllXzQ1Ng.sig"
_PROMPT = "my haemoglobin was 11.2 and I am worried"
_ACT_TOKEN = "eyJ.act_token_that_proves_it.sig"
_CREDENTIAL = "proj_medhelp_zero_retention"

_DECISION = ConsentDecision(
    consent_state={"retention.conversation": False},
    params_forced={"store": False},
    act_token=_ACT_TOKEN,
    source="widget",
    legal_basis="consent",
    binding="user_id",
)

_MESSAGES = [{"role": "user", "content": _PROMPT}]


class _Recording:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "chatcmpl_fake"}


class _FakeConsent:
    def __init__(
        self, decision: ConsentDecision = _DECISION, exc: Exception | None = None
    ) -> None:
        self.decision = decision
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.decision


def _wrapped(
    consent_client: Any = None, **wrap_kwargs: Any
) -> tuple[Any, _Recording, Any]:
    completions = _Recording()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    cc = consent_client if consent_client is not None else _FakeConsent()
    wrapped = consent.wrap(
        client, adapter=ProviderAdapter(_OPENAI), consent_client=cc, **wrap_kwargs
    )
    return wrapped, completions, cc


def _everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every record, rendered AND with its structured payload, as one string.

    Deliberately not just the message: an application logging as JSON reads the
    structured fields, so a secret hiding there would leak just as thoroughly.
    """
    parts: list[str] = []
    for record in _idside_records(caplog):
        parts.append(record.getMessage())
        parts.append(repr(getattr(record, "idside", None)))
    return "\n".join(parts)


def _idside_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Only this SDK's records: caplog captures every logger in the process."""
    return [r for r in caplog.records if r.name.split(".")[0] == "idside"]


# ── silent by default ────────────────────────────────────────────────────────


def test_the_sdk_writes_nothing_unless_the_application_asks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No handler configured ⇒ no output, on any stream, at any level.

    Without a NullHandler on the package logger, Python's `lastResort` handler
    prints WARNING and above to stderr — so "we only log at DEBUG" is not on its
    own an answer, and this test would catch the day it stops being one.
    """
    wrapped, completions, _cc = _wrapped()
    wrapped.chat.completions.create(model="gpt-5", messages=_MESSAGES, user_id=_USER_ID)
    _log.logger().warning("a warning that must not reach a terminal")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert len(completions.calls) == 1  # the call still happened


def test_the_package_logger_carries_a_null_handler() -> None:
    """The mechanism behind the test above, pinned so it cannot be dropped."""
    assert any(
        isinstance(handler, logging.NullHandler)
        for handler in logging.getLogger("idside").handlers
    )


def test_the_sdk_never_configures_the_root_logger() -> None:
    """basicConfig() in a library steals every other library's configuration."""
    root = logging.getLogger()
    before = list(root.handlers)
    wrapped, _completions, _cc = _wrapped()
    wrapped.chat.completions.create(model="gpt-5", messages=_MESSAGES, user_id=_USER_ID)
    assert list(root.handlers) == before


# ── nothing secret, ever ─────────────────────────────────────────────────────


@pytest.mark.parametrize("identity", ["user_id", "ubt"])
def test_no_secret_reaches_a_record_on_a_successful_call(
    caplog: pytest.LogCaptureFixture, identity: str
) -> None:
    """The central test: hand the SDK every secret at once and sweep the output.

    Parametrized over both identity shapes because they leak differently — a
    user_id is a bare string that a naive f-string would carry straight through,
    a UBT is a signed JWT whose payload names the same person.
    """
    completions = _Recording()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped = consent.wrap(
            client,
            adapter=ProviderAdapter(_OPENAI),
            consent_client=_FakeConsent(),
            api_key=_API_KEY,
            credentials={"default": _CREDENTIAL},
        )
        wrapped.chat.completions.create(
            model="gpt-5",
            messages=_MESSAGES,
            **{identity: _USER_ID if identity == "user_id" else _UBT},
        )

    logged = _everything_logged(caplog)
    assert _idside_records(caplog), "the sweep is worthless if nothing was logged"
    for secret in (
        _API_KEY,
        _USER_ID,
        _UBT,
        _PROMPT,
        _ACT_TOKEN,
        _CREDENTIAL,
        "haemoglobin",
    ):
        assert secret not in logged, f"{secret!r} reached a log record"


def test_no_secret_reaches_a_record_when_the_cloud_refuses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure path prints more, and is where a naive exception repr leaks."""
    completions = _Recording()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    boom = httpx.ConnectError("connection refused")
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped = consent.wrap(
            client,
            adapter=ProviderAdapter(_OPENAI),
            consent_client=_FakeConsent(exc=boom),
            api_key=_API_KEY,
        )
        with pytest.raises(httpx.ConnectError):
            wrapped.chat.completions.create(
                model="gpt-5", messages=_MESSAGES, user_id=_USER_ID, ubt=None
            )

    logged = _everything_logged(caplog)
    assert _idside_records(caplog)
    for secret in (_API_KEY, _USER_ID, _PROMPT, "haemoglobin"):
        assert secret not in logged


def test_the_act_token_is_never_logged_even_though_it_is_useful(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A proof is bearer material. What is logged is the request_hash instead.

    The developer's real question — "which proof was minted for this call?" — is
    answered by the request_hash, which is what the ledger indexes and what the
    token is bound to. Logging the token itself would put a signed credential in
    a log aggregator to answer a question a hash already answers.
    """
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped, _completions, _cc = _wrapped()
        wrapped.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id=_USER_ID
        )

    logged = _everything_logged(caplog)
    assert _ACT_TOKEN not in logged
    assert "act_token" not in logged
    assert "request_hash=" in logged


def test_the_consent_state_is_not_logged_but_what_it_forced_is(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The answer a person gave is theirs; the parameter it forced is the app's.

    `params_forced` is what the developer needs to debug an outgoing request,
    and it is a fact about their own call. `consent_state` is the person's
    stated preference — a more intimate reading of the same decision that
    answers no debugging question, so it stays out.
    """
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped, _completions, _cc = _wrapped()
        wrapped.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id=_USER_ID
        )

    logged = _everything_logged(caplog)
    assert "retention.conversation" not in logged
    assert "forced=store" in logged


# ── the pseudonym: correlation without identification ────────────────────────


def test_calls_by_one_user_correlate_without_naming_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped, _completions, _cc = _wrapped()
        wrapped.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id=_USER_ID
        )
        wrapped.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id=_USER_ID
        )
        wrapped.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id="usr_other_789"
        )

    subjects = re.findall(r"subject=(\S+)", _everything_logged(caplog))
    assert len(subjects) >= 3
    assert subjects[0] == subjects[1], "one user, one pseudonym, within a process"
    assert subjects[0] != subjects[-1], "two users must not collapse into one"


def test_the_pseudonym_is_salted_so_a_guessable_id_stays_private() -> None:
    """A user_id is low-entropy on purpose — `usr_marie_456` is a dictionary away.

    An unsalted hash of it is therefore not a pseudonym, it is an encoding: an
    attacker holding the log hashes their guesses and reads the names back. The
    salt is random per process, so the digest is unforgeable from the outside,
    and the property that debugging actually needs — correlating one user's
    calls within one log — is exactly what survives.
    """
    import hashlib

    naive = hashlib.sha256(_USER_ID.encode()).hexdigest()
    pseudonym = _log.pseudonym(_USER_ID)
    assert pseudonym not in naive
    assert _USER_ID not in pseudonym
    assert pseudonym == _log.pseudonym(_USER_ID)
    assert pseudonym != _log.pseudonym("usr_other_789")


def test_the_salt_itself_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """It is what makes the pseudonym unforgeable; in a log it would undo it."""
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped, _completions, _cc = _wrapped()
        wrapped.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id=_USER_ID
        )
    assert _log._SALT.hex() not in _everything_logged(caplog)


# ── what it is actually for ──────────────────────────────────────────────────


def test_a_developer_can_see_which_call_site_and_what_was_forced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The four questions from the brief, answered in the log."""
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped, _completions, _cc = _wrapped()
        wrapped.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id=_USER_ID
        )

    logged = _everything_logged(caplog)
    assert "call_site=chat.completions.create" in logged  # which entry point
    assert "forced=store" in logged  # which option was forced
    assert re.search(r"duration_ms=\d+", logged)  # how long authorize took
    assert "request_hash=" in logged  # which proof, via the ledger


def test_an_unreachable_cloud_says_so_with_the_time_it_waited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped, completions, _cc = _wrapped(
            _FakeConsent(exc=RuntimeError("cloud down"))
        )
        with pytest.raises(RuntimeError):
            wrapped.chat.completions.create(
                model="gpt-5", messages=_MESSAGES, user_id=_USER_ID
            )

    logged = _everything_logged(caplog)
    assert "authorize_failed" in logged
    assert "error=RuntimeError" in logged
    assert re.search(r"duration_ms=\d+", logged)
    assert completions.calls == []


def test_a_local_refusal_is_logged_where_the_developer_will_look(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tools refusal stops the call after a successful authorize.

    Without a line for it, the log would show a healthy authorize and then
    nothing at all, which is the most confusing shape a journal can have.
    """
    refusing = _FakeConsent(
        ConsentDecision(
            consent_state={"agentic_tools": False},
            params_forced={},
            act_token=_ACT_TOKEN,
            source="widget",
            legal_basis="consent",
            binding="user_id",
            constraints={"tools": "refuse"},
        )
    )
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped, completions, _cc = _wrapped(refusing)
        with pytest.raises(ToolUseRefusedError):
            wrapped.chat.completions.create(
                model="gpt-5",
                messages=_MESSAGES,
                tools=[{"type": "function", "function": {"name": "f"}}],
                user_id=_USER_ID,
            )

    logged = _everything_logged(caplog)
    assert "refused" in logged
    assert "ToolUseRefusedError" in logged
    assert completions.calls == []


def test_a_missing_credential_says_which_posture_not_which_credential(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A posture label is app configuration; the credential behind it is a secret."""
    needs_posture = _FakeConsent(
        ConsentDecision(
            consent_state={},
            params_forced={},
            act_token=_ACT_TOKEN,
            source="widget",
            legal_basis="consent",
            binding="user_id",
            credential_posture="zero_retention",
        )
    )
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped, completions, _cc = _wrapped(
            needs_posture, credentials={"default": _CREDENTIAL}
        )
        with pytest.raises(MissingCredentialError):
            wrapped.chat.completions.create(
                model="gpt-5", messages=_MESSAGES, user_id=_USER_ID
            )

    logged = _everything_logged(caplog)
    assert "zero_retention" in logged  # the label: app configuration
    assert _CREDENTIAL not in logged  # the credential: a secret
    assert completions.calls == []


def test_wrap_says_what_it_recognised(caplog: pytest.LogCaptureFixture) -> None:
    """Half the support questions are "is my client even being enforced?"."""
    with caplog.at_level(logging.DEBUG, logger="idside"):
        _wrapped()

    logged = _everything_logged(caplog)
    assert "provider=openai" in logged
    assert "call_sites=1" in logged


def test_an_alias_that_defers_upstream_says_so_instead_of_going_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second authorize would be a bug; a silent second call looks like one too."""

    class _ReentrantRecording(_Recording):
        def parse(self, **kwargs: Any) -> dict[str, str]:
            return self.create(**kwargs)

    completions = _ReentrantRecording()
    cc = _FakeConsent()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped = consent.wrap(
            client, adapter=ProviderAdapter(_OPENAI), consent_client=cc
        )
        wrapped.chat.completions.parse(
            model="gpt-5",
            messages=_MESSAGES,
            user_id=_USER_ID,
        )

    assert len(cc.calls) == 1
    assert len(completions.calls) == 1
    assert "passthrough" in _everything_logged(caplog)


# ── the net stays blind ──────────────────────────────────────────────────────


def test_the_net_logs_nothing_at_all(caplog: pytest.LogCaptureFixture) -> None:
    """Documented in providers.md as "it hashes nothing, logs nothing".

    The guard sits on the request path of a product whose premise is that we do
    not see the request. It reads the HTTP method and whether one header is
    present; a log line naming the path it refused would be the first thing it
    ever recorded about a request, and observability is not a reason to start.
    """
    transport = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200))
    )
    client = SimpleNamespace(_client=transport)
    from idside import _guard

    _guard.install(client, _OPENAI)

    with (
        caplog.at_level(logging.DEBUG, logger="idside"),
        pytest.raises(UnenforcedCallError),
    ):
        transport.send(httpx.Request("POST", "https://api.example.test/v1/x"))

    assert _idside_records(caplog) == []


# ── observation is not influence (invariant #8) ──────────────────────────────


class _Meddling(logging.Handler):
    """A handler that tries to change everything it is shown.

    Not a straw man: a logging handler is arbitrary application code running
    inside the enforcement path, and formatters routinely walk and normalise the
    objects they are handed. If a record carried the live decision, this is all
    it would take to unforce a parameter a user's refusal had forced.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.seen = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.seen += 1
        payload = getattr(record, "idside", None)
        if isinstance(payload, dict):
            for key in list(payload):
                if isinstance(payload[key], dict):
                    payload[key].clear()
                    payload[key]["injected"] = True
                else:
                    payload[key] = "tampered"


def test_a_logging_handler_cannot_reach_the_decision() -> None:
    """Invariant #8, wearing a logger's clothes.

    Observing may not influence. Whatever a handler does to what it is given,
    the parameters the cloud forced must land on the provider call unchanged.
    """
    meddler = _Meddling()
    logger = logging.getLogger("idside")
    logger.addHandler(meddler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        wrapped, completions, _cc = _wrapped()
        wrapped.chat.completions.create(
            model="gpt-5", messages=_MESSAGES, user_id=_USER_ID
        )
    finally:
        logger.removeHandler(meddler)
        logger.setLevel(previous)

    assert meddler.seen > 0, "the handler must actually have run"
    kwargs = completions.calls[0]
    assert kwargs["store"] is False, "a forced parameter was altered by a log handler"
    assert kwargs[_OPENAI.headers_kwarg][_OPENAI.token_header] == _ACT_TOKEN
    assert kwargs["model"] == "gpt-5"


def test_logging_adds_no_option_to_wrap() -> None:
    """Invariant #8: no knob arrives alongside the journal."""
    client = SimpleNamespace(chat=SimpleNamespace(completions=_Recording()))
    for hatch in ("logger", "on_event", "debug", "log_level", "audit_hook"):
        with pytest.raises(TypeError):
            consent.wrap(
                client,
                adapter=ProviderAdapter(_OPENAI),
                consent_client=_FakeConsent(),
                **{hatch: True},
            )


# ── the async path journals the same things ──────────────────────────────────


class _RecordingAsync:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"id": "chatcmpl_fake"}


class _FakeAsyncConsent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def authorize(self, **kwargs: Any) -> ConsentDecision:
        self.calls.append(kwargs)
        return _DECISION


def test_the_async_path_logs_what_the_blocking_one_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A journal that thins out on the async path is a journal that misleads."""
    completions = _RecordingAsync()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped = consent.wrap(
            client,
            adapter=ProviderAdapter(_OPENAI),
            consent_client=_FakeAsyncConsent(),
        )
        asyncio.run(
            wrapped.chat.completions.create(
                model="gpt-5", messages=_MESSAGES, user_id=_USER_ID
            )
        )

    logged = _everything_logged(caplog)
    assert "call_site=chat.completions.create" in logged
    assert "forced=store" in logged
    assert re.search(r"duration_ms=\d+", logged)
    assert _USER_ID not in logged
    assert _PROMPT not in logged


def test_the_cloud_transport_logs_no_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one place an ID Side key exists in memory is the transport itself."""
    with caplog.at_level(logging.DEBUG, logger="idside"):
        client = HttpConsentClient(
            base_url="https://api.test",
            api_key=_API_KEY,
            transport=httpx.MockTransport(lambda _r: httpx.Response(503)),
        )
        with pytest.raises(ConsentUnreachableError):
            client.authorize(
                user_id=_USER_ID,
                provider="openai",
                model="gpt-5",
                request_hash="sha256:abc",
            )
        aclient = AsyncHttpConsentClient(
            base_url="https://api.test",
            api_key=_API_KEY,
            transport=httpx.MockTransport(lambda _r: httpx.Response(503)),
        )
        with pytest.raises(ConsentUnreachableError):
            asyncio.run(
                aclient.authorize(
                    user_id=_USER_ID,
                    provider="openai",
                    model="gpt-5",
                    request_hash="sha256:abc",
                )
            )

    logged = _everything_logged(caplog)
    assert _API_KEY not in logged
    assert _USER_ID not in logged


_AUTHORIZED = {
    "decision": "allow",
    "consent_state": {"retention.conversation": False},
    "params_forced": {"store": False},
    "mode": "test",
    "act_token": _ACT_TOKEN,
    "token_id": "act_01k4f9h2m3",
    "expires_at": "2026-09-12T12:00:00Z",
    "source": "widget",
    "legal_basis": "consent",
    "binding": "user_id",
}


def _connection_refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(lambda r: httpx.Response(200, json=_AUTHORIZED), id="200"),
        pytest.param(lambda r: httpx.Response(401, json={}), id="401"),
        pytest.param(lambda r: httpx.Response(503), id="503"),
        pytest.param(_connection_refused, id="unreachable"),
    ],
)
def test_a_key_read_from_the_environment_reaches_no_record(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    answer: Any,
) -> None:
    """The key the transport reads for itself is held to the same rule.

    Same scenario in the Node suite: IDSIDE_API_KEY set, no key argument, a
    call through `wrap()`, whatever the cloud answers.
    """
    monkeypatch.setenv("IDSIDE_API_KEY", _API_KEY)
    sent: list[str | None] = []

    def cloud(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("authorization"))
        return answer(request)  # type: ignore[no-any-return]

    completions = _Recording()
    with caplog.at_level(logging.DEBUG, logger="idside"):
        wrapped = consent.wrap(
            SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            adapter=ProviderAdapter(_OPENAI),
            consent_client=HttpConsentClient(
                "https://api.test", transport=httpx.MockTransport(cloud)
            ),
        )
        try:
            wrapped.chat.completions.create(
                model="gpt-5", messages=_MESSAGES, user_id=_USER_ID
            )
        except IDSideError as exc:
            assert _API_KEY not in str(exc)

    assert sent == [f"Bearer {_API_KEY}"], "the key was not in play"
    assert _idside_records(caplog)
    assert _API_KEY not in _everything_logged(caplog)
