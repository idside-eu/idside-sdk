# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""ID Side — AI Consent Infrastructure SDK.

Intended usage::

    from openai import OpenAI
    from idside import consent

    client = consent.wrap(OpenAI())
    client.chat.completions.create(..., user_id="usr_marie_456")

Anyone holding a proof can check it offline, with no call to ID Side::

    from idside import request_hash, verify_act

    proof = verify_act(token, jwks=jwks, request_hash=request_hash(payload))

Invariants (see docs/versioning.md §3): opaque tunnel (no preference setter),
fail-closed, one ACT token = one request. No private key in this package: it
verifies proofs, it never issues them.

Licensed under the ID Side Source-Available License, version 1.0: see LICENSE
and NOTICE in this distribution.

Patents: https://sdk.idside.eu/patents
"""

from __future__ import annotations

from idside import consent, verify
from idside.__about__ import __version__
from idside._request_hash import request_hash
from idside._transport import (
    DEFAULT_BASE_URL,
    AsyncConsentClient,
    AsyncHttpConsentClient,
    ConsentClient,
    ConsentDecision,
    HttpConsentClient,
)
from idside.adapters import ProviderAdapter
from idside.exceptions import (
    ActAlgorithmError,
    ActBindingMismatchError,
    ActExpiredError,
    ActIssuerError,
    ActKeyNotFoundError,
    ActMalformedError,
    ActNotYetValidError,
    ActPrivateKeyInKeySetError,
    ActSignatureError,
    ActTypeMismatchError,
    ActVerificationError,
    ActVerificationUnavailableError,
    AuthenticationError,
    BindingError,
    BindingRequiredError,
    ConflictingIdentityError,
    ConsentDeniedError,
    ConsentRefreshRequiredError,
    ConsentTokenError,
    ConsentUnreachableError,
    ContractError,
    CredentialRoutingError,
    IDSideError,
    InvalidBindingError,
    MissingCredentialError,
    MissingIdentityError,
    QuotaExceededError,
    RateLimitedError,
    ToolUseRefusedError,
    UnenforceableOptionError,
    UnenforcedCallError,
    UnsupportedProviderError,
)
from idside.options import (
    ConsentOption,
    Disclosure,
    catalogue,
    decidable_options,
    disclosures,
)
from idside.verify import (
    ACT_ALGORITHM,
    ACT_TYPE,
    DEFAULT_LEEWAY_SECONDS,
    PRODUCTION_ISSUER,
    TEST_ISSUER,
    VerifiedAct,
    verify_act,
)

__all__ = [
    # Constants a caller needs to state what it is verifying against, exported
    # from the package root so both SDKs present the same surface.
    "ACT_ALGORITHM",
    "ACT_TYPE",
    # `DEFAULT_BASE_URL`, `ConsentClient`, `AsyncConsentClient`,
    # `ConsentDecision`, `HttpConsentClient` and `AsyncHttpConsentClient` are the
    # consent-client surface, for the supported `consent_client=` injection: the
    # protocols to implement, the decision to return, and the default transport
    # with its `timeout=`. Annotating or building any of them used to mean
    # importing `idside._transport`, a private module under no compatibility
    # promise, for something the documentation invites callers to do.
    "DEFAULT_BASE_URL",
    "DEFAULT_LEEWAY_SECONDS",
    "PRODUCTION_ISSUER",
    "TEST_ISSUER",
    "ActAlgorithmError",
    "ActBindingMismatchError",
    "ActExpiredError",
    "ActIssuerError",
    "ActKeyNotFoundError",
    "ActMalformedError",
    "ActNotYetValidError",
    "ActPrivateKeyInKeySetError",
    "ActSignatureError",
    "ActTypeMismatchError",
    "ActVerificationError",
    "ActVerificationUnavailableError",
    "AsyncConsentClient",
    "AsyncHttpConsentClient",
    "AuthenticationError",
    "BindingError",
    "BindingRequiredError",
    "ConflictingIdentityError",
    "ConsentClient",
    "ConsentDecision",
    "ConsentDeniedError",
    "ConsentOption",
    "ConsentRefreshRequiredError",
    "ConsentTokenError",
    "ConsentUnreachableError",
    "ContractError",
    "CredentialRoutingError",
    "Disclosure",
    "HttpConsentClient",
    "IDSideError",
    "InvalidBindingError",
    "MissingCredentialError",
    "MissingIdentityError",
    "ProviderAdapter",
    "QuotaExceededError",
    "RateLimitedError",
    "ToolUseRefusedError",
    "UnenforceableOptionError",
    "UnenforcedCallError",
    "UnsupportedProviderError",
    "VerifiedAct",
    "__version__",
    "catalogue",
    "consent",
    "decidable_options",
    "disclosures",
    # The request-hash formula, so a verifier can compute the `request_hash=`
    # it pins `verify_act()` to. Node exports the same function as `requestHash`.
    "request_hash",
    "verify",
    "verify_act",
]
