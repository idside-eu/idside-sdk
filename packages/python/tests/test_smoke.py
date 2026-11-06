"""Smoke tests — verify the package imports and exposes its API surface."""

from __future__ import annotations

import idside
from idside import consent
from idside.exceptions import ConsentUnreachableError, IDSideError


def test_version_is_exposed() -> None:
    assert isinstance(idside.__version__, str)
    assert idside.__version__  # non-empty


def test_public_surface() -> None:
    assert callable(consent.wrap)
    assert "consent" in idside.__all__
    assert issubclass(ConsentUnreachableError, IDSideError)


def test_full_exception_hierarchy_is_exported() -> None:
    for name in (
        "AuthenticationError",
        "BindingError",
        "BindingRequiredError",
        "ConflictingIdentityError",
        "ConsentDeniedError",
        "ConsentRefreshRequiredError",
        "ConsentTokenError",
        "ConsentUnreachableError",
        "ContractError",
        "InvalidBindingError",
        "MissingIdentityError",
        "QuotaExceededError",
        "RateLimitedError",
        "UnsupportedProviderError",
    ):
        assert name in idside.__all__
        assert issubclass(getattr(idside, name), IDSideError)


def test_a_binding_error_is_still_an_authentication_error() -> None:
    """Added under the class that used to cover every 401, never beside it.

    A handler written before the binding errors existed catches
    `AuthenticationError`; it must go on catching an expired UBT.
    """
    for name in ("InvalidBindingError", "BindingRequiredError"):
        error_class = getattr(idside, name)
        assert issubclass(error_class, idside.BindingError)
        assert issubclass(error_class, idside.AuthenticationError)


def test_a_refresh_required_is_not_an_outage() -> None:
    """Its remedy is the widget, so no `except ConsentUnreachableError` retry
    loop may swallow it."""
    assert not issubclass(
        idside.ConsentRefreshRequiredError, idside.ConsentUnreachableError
    )
    assert issubclass(idside.ConsentRefreshRequiredError, IDSideError)


def test_the_consent_client_surface_is_importable_from_the_root() -> None:
    """The type a developer annotates must not live behind an underscore.

    Injecting `consent_client=` is the documented way to point the SDK at a
    staging cloud, to test without one, or to tune the deadline. Node exports
    the whole set from its index; Python exported none of it, so the same
    supported extension point meant importing `idside._transport` — a private
    module, under no compatibility promise, in the SDK whose fail-closed
    behaviour a caller is standing on.
    """
    import idside

    for name in (
        "AsyncConsentClient",
        "AsyncHttpConsentClient",
        "ConsentClient",
        "ConsentDecision",
        "DEFAULT_BASE_URL",
        "HttpConsentClient",
    ):
        assert name in idside.__all__, name
        assert getattr(idside, name) is not None, name


def test_a_consent_decision_can_be_built_without_a_private_import() -> None:
    """The dataclass is not just annotated, it is constructed: a hand-written
    `authorize()` has to return one."""
    from idside import ConsentDecision

    decision = ConsentDecision(
        consent_state={"retention.conversation": False},
        params_forced={"store": False},
        act_token="act.jwt.token",
        source="widget",
        legal_basis="consent",
        binding="user_id",
    )
    assert decision.credential_posture is None
    assert decision.constraints == {}
