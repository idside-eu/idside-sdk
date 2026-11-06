// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * ID Side SDK — AI consent enforcement for provider clients.
 *
 * ```ts
 * import { consent } from "@idside/sdk";
 * import OpenAI from "openai";
 *
 * const client = consent.wrap(new OpenAI(), { apiKey: process.env.IDSIDE_API_KEY });
 * await client.chat.completions.create({ model: "gpt-5", messages, user_id: "usr_marie_456" });
 * ```
 *
 * Licensed under the ID Side Source-Available License, version 1.0: see
 * LICENSE and NOTICE in this package.
 *
 * Patents: https://sdk.idside.eu/patents
 */

export { consent, wrap } from "./consent.ts";
export type { WrapOptions } from "./consent.ts";

// The wrapped client's type, and the identity its call sites demand.
export type { Enforced, Identity } from "./enforced.ts";

export { getAdapterFor, registerAdapter, GenericAdapter, GeminiAdapter } from "./adapters/index.ts";
export type { ProviderAdapter, RequestMetadata } from "./adapters/index.ts";

export { loadDescriptors, DEFAULT_CONSENT_HEADER } from "./descriptors.ts";
export type { ProviderDescriptor, CredentialRouting } from "./descriptors.ts";

export { HttpConsentClient, DEFAULT_BASE_URL } from "./transport.ts";
export type { ConsentClient, ConsentDecision, AuthorizeRequest, HttpConsentClientOptions } from "./transport.ts";

export { canonicalize, canonicalizeToString, CanonicalizationError } from "./jcs.ts";
export { requestHash } from "./requestHash.ts";

export {
  verifyAct,
  ACT_ALGORITHM,
  ACT_TYPE,
  PRODUCTION_ISSUER,
  TEST_ISSUER,
  DEFAULT_LEEWAY_SECONDS,
} from "./act.ts";
export type { VerifyActOptions, VerifiedAct, ActClaims, ActJwk, ActJwks, ActMode } from "./act.ts";

export {
  IDSideError,
  ConsentUnreachableError,
  ConsentDeniedError,
  ConsentRefreshRequiredError,
  AuthenticationError,
  BindingError,
  InvalidBindingError,
  BindingRequiredError,
  QuotaExceededError,
  RateLimitedError,
  ContractError,
  ConsentTokenError,
  MissingIdentityError,
  ConflictingIdentityError,
  UnsupportedProviderError,
  ToolUseRefusedError,
  UnenforceableOptionError,
  UnenforcedCallError,
  CredentialRoutingError,
  MissingCredentialError,
  ActVerificationError,
  ActMalformedError,
  ActAlgorithmError,
  ActTypeMismatchError,
  ActKeyNotFoundError,
  ActPrivateKeyInKeySetError,
  ActSignatureError,
  ActExpiredError,
  ActNotYetValidError,
  ActIssuerError,
  ActBindingMismatchError,
  ActVerificationUnavailableError,
} from "./exceptions.ts";

// What an app may actually ask its users, given the providers it wraps — and
// what it may only state. See src/options.ts for why the distinction matters.
export { catalogue, decidableOptions, disclosures } from "./options.ts";
export type { ConsentOption, Disclosure, EnforcementLevel } from "./options.ts";
