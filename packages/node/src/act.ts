// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Local, offline verification of ACT tokens.
 *
 * An ACT token is the cryptographic proof ID Side issues for each AI call. This
 * module lets anyone holding a token — an auditor, a customer's compliance team,
 * a provider, a data protection authority — check it **without calling ID Side**:
 *
 * ```ts
 * import { verifyAct } from "@idside/sdk";
 * const proof = await verifyAct(token, { jwks, requestHash });
 * ```
 *
 * Verification only. This package never signs and holds no private key (see
 * docs/concepts.md §6, "The SDK cannot forge one"): the ECDSA signing keys live
 * exclusively in the ID Side cloud. Everything here works from **public** key
 * material, and `node:crypto` covers ES256 out of the box — the SDK keeps its
 * zero runtime dependencies.
 *
 * Every check fails closed. `alg` is pinned to `ES256` and `typ` to `act+jwt` —
 * a token declaring `none` or an HMAC algorithm is a forgery attempt, not
 * something to negotiate — and an unknown `kid` is never worked around by trying
 * another key.
 *
 * Behaviour is kept in step with the Python SDK's `idside.verify_act()`, down to
 * the error taxonomy and the order of the checks.
 *
 * Spec: `docs/spec/act-token.md`.
 */

import { createPublicKey, timingSafeEqual, verify as cryptoVerify } from "node:crypto";
import type { JsonWebKey, KeyObject } from "node:crypto";
import {
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
  ActVerificationUnavailableError,
} from "./exceptions.ts";
// The same header the authorize hop sends: a key-set host watching its traffic
// sees one SDK, not two anonymous callers (mirrors the Python SDK, whose
// `verify` module imports it from its transport for the same reason).
// Verification is the same fail-closed wait on the same kind of outage, so a
// caller does not wait a different length of time depending on which of the two
// it is doing — hence the deadline comes from there too, not from a second number.
import { DEFAULT_TIMEOUT_MS, USER_AGENT } from "./transport.ts";

/** The only algorithm an ACT token may declare. Pinned, never negotiated. */
export const ACT_ALGORITHM = "ES256";

/** The only `typ` an ACT token may declare (a UBT is `ubt+jwt`). */
export const ACT_TYPE = "act+jwt";

/** Issuer of production proofs. */
export const PRODUCTION_ISSUER = "idside.eu";

/** Issuer of development proofs — accepted only on explicit opt-in. */
export const TEST_ISSUER = "test.idside.eu";

/** Clock-skew tolerance applied to `exp`, `iat` and `nbf`. */
export const DEFAULT_LEEWAY_SECONDS = 60;

/** Maximum lifetime declared by an ACT, independent of clock-skew leeway. */
const MAX_ACT_TTL_SECONDS = 3_600;

/** ES256 in JOSE form: r‖s, 32 bytes each (IEEE P1363), never DER. */
const SIGNATURE_BYTES = 64;

/**
 * The spec bounds a token at 3,072 characters (the `deviations` claim is what can
 * take it past 2 KB); this cap only keeps absurd input from being parsed.
 */
const MAX_TOKEN_CHARS = 8_192;
const BASE64URL = /^[A-Za-z0-9_-]*$/;
const REQUEST_HASH = /^sha256:[0-9a-f]{64}$/;

/**
 * JWK members that only ever appear in a *private* key: the EC/RSA private
 * scalar and the symmetric key. `d` also covers RSA private keys, which always
 * carry it alongside their CRT members.
 */
const PRIVATE_JWK_MEMBERS = ["d", "k"] as const;

/** Claims every ACT token must carry, as non-empty strings (see spec §3). */
const STRING_CLAIMS = [
  "iss",
  "sub",
  "app_id",
  "jti",
  "legal_basis",
  "source",
  "provider",
  "model",
  "request_hash",
  "nonce",
  "binding",
] as const;

/**
 * A JWK from the key set. Every member is typed `unknown` on purpose: a key set
 * is untrusted input, so its shape is established at runtime, not by the
 * compiler.
 */
export interface ActJwk {
  readonly kty?: unknown;
  readonly kid?: unknown;
  readonly crv?: unknown;
  readonly x?: unknown;
  readonly y?: unknown;
  readonly alg?: unknown;
  readonly use?: unknown;
  readonly key_ops?: unknown;
  readonly [member: string]: unknown;
}

/** A JWK Set as published at `/.well-known/jwks.json`. */
export interface ActJwks {
  readonly keys: readonly ActJwk[];
}

/** The raw claim set of a token, under the JWT names of the spec (§3). */
export interface ActClaims {
  readonly [claim: string]: unknown;
}

/** Whether the API key that minted a verified proof was a test or live key. */
export type ActMode = "test" | "live";

/**
 * A verified ACT token — what the proof actually attests.
 *
 * Returned only when the signature, the token type, the algorithm, the issuer,
 * the time window and every binding the caller pinned have all checked out. Its
 * existence is the success signal; a failure is always a thrown error, never a
 * falsy return.
 *
 * `binding` records how the end user was tied to the request — `"ubt"` (the ID
 * Side widget bound the identity, the strong link) or `"user_id"` (the developer
 * asserted it). It is the evidentiary weight of the proof.
 */
export interface VerifiedAct {
  readonly issuer: string;
  /** The app-scoped pseudonym the cloud derived from the end user (`sub`). */
  readonly subject: string;
  readonly appId: string;
  /** `jti` — also the public `token_id` of the remote verify endpoint. */
  readonly tokenId: string;
  readonly issuedAt: number;
  readonly expiresAt: number;
  readonly consentState: Readonly<Record<string, unknown>>;
  readonly legalBasis: string;
  readonly source: string;
  readonly provider: string;
  readonly model: string;
  readonly requestHash: string;
  readonly nonce: string;
  readonly binding: string;
  /** Undefined only for a legacy proof accepted through the explicit option. */
  readonly mode: ActMode | undefined;
  /** The `kid` of the key that verified the signature. */
  readonly keyId: string;
  /** Every claim as signed, including any the SDK does not know about. */
  readonly claims: ActClaims;
}

export interface VerifyActOptions {
  /**
   * A JWK Set (`{ keys: [...] }`) or a single JWK. Exactly one of `jwks` /
   * `jwksUrl` is required — there is no ambient key source and no default
   * endpoint to fall back on.
   */
  readonly jwks?: ActJwks | ActJwk;
  /**
   * An `https://` URL serving the JWKS (ID Side publishes it at
   * `/.well-known/jwks.json`). Fetched on every call: for repeated
   * verification, fetch once yourself and pass `jwks` instead.
   */
  readonly jwksUrl?: string;
  /** Accepted issuer, or several. Defaults to `idside.eu` only. */
  readonly issuer?: string | readonly string[];
  /**
   * Also accept `test.idside.eu`, the issuer of a deployment of the cloud that
   * is not the production service. Off by default. A test key's proof from the
   * production service is signed `idside.eu` either way: read `mode` for that.
   */
  readonly allowTestIssuer?: boolean;
  /** Accept proofs minted before the signed `mode` claim existed. */
  readonly allowLegacyWithoutMode?: boolean;
  /** Clock-skew tolerance in seconds for `exp`/`iat`/`nbf`. */
  readonly leeway?: number;
  /**
   * Verification time, as an epoch in seconds. Defaults to now. Set it to audit
   * a past call — "was this proof valid when the call was made?".
   */
  readonly now?: number;
  /**
   * Pin the request the token must be bound to (`"sha256:<hex>"`, RFC 8785/JCS
   * canonical — see `requestHash()`). **One token = one request**: this is the
   * check that makes a stolen but perfectly signed token useless on another
   * payload.
   */
  readonly requestHash?: string;
  /** Pin the token's anti-replay `nonce`. */
  readonly nonce?: string;
  /**
   * Pin the `sub` claim. Note that `sub` is the app-scoped *pseudonym* the cloud
   * derived from the end user — the raw `user_id` you passed to `consent.wrap()`
   * deliberately never enters the token, so it cannot be pinned here.
   */
  readonly subject?: string;
  /** Pin the application the call was authorized for. */
  readonly appId?: string;
  /** Timeout for the JWKS fetch, in milliseconds. Defaults to the shared 5 s. */
  readonly timeoutMs?: number;
  /** Injectable for tests; defaults to the global `fetch`. */
  readonly fetch?: typeof fetch;
}

/**
 * Verify an ACT token locally and return what it attests.
 *
 * Checks, in order: compact JWS structure and strict base64url segments, `alg`
 * = ES256, `typ` = `act+jwt`, no unknown `crit`, key selection by `kid`, the
 * signature, the mandatory claims, the issuer, the time window, then every
 * binding the caller pinned. Any failure throws an `ActVerificationError`
 * subclass — a token is proof only when this function returns.
 *
 * A signature alone does not make a token relevant: pass `requestHash` to check
 * that the proof belongs to the request you are auditing. Replay across
 * *different* calls is not detectable offline (a verifier has no history); that
 * is the cloud ledger's job. What a local verifier proves is that this signed
 * token belongs to this exact request.
 *
 * Throws a `TypeError` for caller mistakes — no key source, two key sources, a
 * non-https `jwksUrl`, a negative `leeway`, an empty issuer set.
 */
export async function verifyAct(token: string, options: VerifyActOptions): Promise<VerifiedAct> {
  if ((options.jwks === undefined) === (options.jwksUrl === undefined)) {
    throw new TypeError(
      "verifyAct() needs exactly one key source: pass jwks (a JWK Set or a single JWK) " +
        "or jwksUrl (an https:// URL), not both and not neither.",
    );
  }
  const leeway = options.leeway ?? DEFAULT_LEEWAY_SECONDS;
  if (!Number.isFinite(leeway) || leeway < 0) {
    throw new TypeError("verifyAct() leeway must be zero or positive seconds.");
  }
  const now = options.now ?? Date.now() / 1000;
  if (!Number.isFinite(now)) {
    throw new TypeError("verifyAct() now must be a finite epoch in seconds.");
  }
  const expectedIssuers = resolveIssuers(options);

  const [headerSegment, payloadSegment, signatureSegment] = splitToken(token);
  const header = jsonObject(decodeSegment(headerSegment, "JOSE header"), "JOSE header");
  checkAlgorithm(header);
  checkType(header);
  checkNoCriticalExtensions(header);
  const keyId = readKeyId(header);

  // Decoded (not parsed) now: this validates the payload character set before it
  // is fed to the signature check. Its content stays untrusted until then.
  const payloadBytes = decodeSegment(payloadSegment, "payload");
  const signature = decodeSegment(signatureSegment, "signature");

  const keySet = options.jwksUrl !== undefined ? await fetchJwks(options.jwksUrl, options) : options.jwks;
  const jwk = selectKey(keySet, keyId);
  checkSignature(`${headerSegment}.${payloadSegment}`, signature, jwk, keyId);

  // Claims are read only now: nothing above this line trusted the payload.
  const claims = jsonObject(payloadBytes, "payload");
  const strings = {} as Record<(typeof STRING_CLAIMS)[number], string>;
  for (const name of STRING_CLAIMS) strings[name] = requireString(claims, name);
  const issuedAt = requireTimestamp(claims, "iat");
  const expiresAt = requireTimestamp(claims, "exp");
  const consentState = requireObject(claims, "consent_state");
  if (!REQUEST_HASH.test(strings.request_hash)) {
    throw new ActMalformedError(
      "Claim 'request_hash' is not a canonical 'sha256:<64 hex chars>' digest; " +
        "the token does not follow the ACT specification.",
    );
  }
  if (expiresAt <= issuedAt) {
    throw new ActMalformedError(
      "Claim 'exp' is not after 'iat': the token declares an empty or inverted validity window.",
    );
  }
  if (expiresAt - issuedAt > MAX_ACT_TTL_SECONDS) {
    throw new ActMalformedError(
      "Claims 'iat' and 'exp' declare a validity window longer than 3,600 seconds; " +
        "an ACT cannot live longer than one hour.",
    );
  }
  const rawMode = claims.mode;
  let mode: ActMode | undefined;
  if (rawMode === undefined && !("mode" in claims)) {
    if (options.allowLegacyWithoutMode !== true) {
      throw new ActMalformedError(
        "Missing required claim 'mode'. For a proof minted before this claim existed, " +
          "set allowLegacyWithoutMode: true explicitly.",
      );
    }
  } else if (rawMode === "test" || rawMode === "live") {
    mode = rawMode;
  } else {
    throw new ActMalformedError("Claim 'mode' must be the string 'test' or 'live'.");
  }

  if (!expectedIssuers.has(strings.iss)) {
    throw new ActIssuerError(
      `Issuer ${JSON.stringify(strings.iss)} is not accepted (expected ` +
        `${[...expectedIssuers].sort().join(" or ")}). A token from another environment is not a proof for this one.`,
    );
  }
  checkTimeWindow(claims, issuedAt, expiresAt, leeway, now);

  for (const [name, expected] of [
    ["request_hash", options.requestHash],
    ["nonce", options.nonce],
    ["sub", options.subject],
    ["app_id", options.appId],
  ] as const) {
    if (expected !== undefined && !constantTimeEqual(strings[name], expected)) {
      throw new ActBindingMismatchError(
        `Claim '${name}' does not match the value pinned by the caller: ` +
          "this proof was issued for a different request, user or app.",
      );
    }
  }

  return Object.freeze({
    issuer: strings.iss,
    subject: strings.sub,
    appId: strings.app_id,
    tokenId: strings.jti,
    issuedAt,
    expiresAt,
    consentState: Object.freeze({ ...consentState }),
    legalBasis: strings.legal_basis,
    source: strings.source,
    provider: strings.provider,
    model: strings.model,
    requestHash: strings.request_hash,
    nonce: strings.nonce,
    binding: strings.binding,
    mode,
    keyId,
    claims: Object.freeze({ ...claims }),
  });
}

// --- JWS structure ------------------------------------------------------

/** Split a compact JWS into its three segments. */
function splitToken(token: unknown): [string, string, string] {
  if (typeof token !== "string") {
    throw new ActMalformedError(`An ACT token is a string; got ${token === null ? "null" : typeof token}.`);
  }
  if (token.length > MAX_TOKEN_CHARS) {
    throw new ActMalformedError(
      `The token is implausibly large (${token.length} characters, limit ${MAX_TOKEN_CHARS}); ` +
        "an ACT token is at most 3,072 characters. Refusing to parse it.",
    );
  }
  const segments = token.split(".");
  if (segments.length !== 3 || !segments[0] || !segments[1]) {
    throw new ActMalformedError(
      `Not a compact JWS: expected 3 non-empty segments, got ${segments.length}. ` +
        "Pass the raw X-AI-Consent-Token value, untruncated.",
    );
  }
  return [segments[0], segments[1], segments[2]];
}

/**
 * Decode one JWS segment. Strict: unpadded base64url only, and canonical — a
 * segment a compliant encoder would not have produced is a rejection, never
 * something to clean up. Canonicality also means one token has exactly one
 * string form, so nothing keyed on that string can be fooled by a re-encoding.
 */
function decodeSegment(segment: string, what: string): Buffer {
  if (!BASE64URL.test(segment)) {
    throw new ActMalformedError(`The ${what} is not unpadded base64url (JOSE forbids '=', '+' and '/').`);
  }
  const decoded = Buffer.from(segment, "base64url");
  if (decoded.toString("base64url") !== segment) {
    throw new ActMalformedError(`The ${what} is not canonical base64url: it carries unused trailing bits.`);
  }
  return decoded;
}

function jsonObject(raw: Buffer, what: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw.toString("utf8"));
  } catch (cause) {
    throw new ActMalformedError(`The ${what} is not valid JSON.`, { cause });
  }
  if (!isRecord(parsed)) {
    throw new ActMalformedError(`The ${what} is not a JSON object.`);
  }
  return parsed;
}

// --- header -------------------------------------------------------------

function checkAlgorithm(header: Record<string, unknown>): void {
  if (header.alg !== ACT_ALGORITHM) {
    throw new ActAlgorithmError(
      `The JOSE header declares alg=${JSON.stringify(header.alg)}; ACT verification pins ` +
        `${ACT_ALGORITHM}. A token proposing another algorithm is a forgery attempt, not a negotiation.`,
    );
  }
}

function checkType(header: Record<string, unknown>): void {
  if (header.typ !== ACT_TYPE) {
    throw new ActTypeMismatchError(
      `The JOSE header declares typ=${JSON.stringify(header.typ)}; an ACT token is ` +
        `'${ACT_TYPE}'. A token minted for another purpose is not a proof of consent.`,
    );
  }
}

/** Reject `crit`: this verifier understands no JOSE extension. */
function checkNoCriticalExtensions(header: Record<string, unknown>): void {
  if ("crit" in header) {
    throw new ActMalformedError(
      "The JOSE header declares 'crit' extensions this verifier does not understand; " +
        "RFC 7515 requires rejecting the token.",
    );
  }
}

/** Read `kid`. Absent means no key selection is possible — fail closed. */
function readKeyId(header: Record<string, unknown>): string {
  const keyId = header.kid;
  if (typeof keyId !== "string" || !keyId) {
    throw new ActMalformedError(
      "The JOSE header carries no 'kid', so no verification key can be selected. " +
        "ID Side always names the signing key.",
    );
  }
  return keyId;
}

// --- key material -------------------------------------------------------

/** Fetch a JWKS over HTTPS. Anything unusable fails closed. */
async function fetchJwks(url: string, options: VerifyActOptions): Promise<unknown> {
  if (!url.toLowerCase().startsWith("https://")) {
    throw new TypeError(
      "verifyAct() only fetches a JWKS over https:// — public keys retrieved over an " +
        `unauthenticated channel prove nothing; got ${JSON.stringify(url)}.`,
    );
  }

  // Detach from `this` so a strict fetch implementation is never called with the
  // wrong receiver ("Illegal invocation").
  const doFetch = options.fetch ?? globalThis.fetch;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  let response: Response;
  try {
    response = await doFetch(url, {
      headers: { accept: "application/json", "user-agent": USER_AGENT },
      // A redirected key set is a key set from somewhere else.
      redirect: "error",
      signal: controller.signal,
    });
  } catch (cause) {
    clearTimeout(timer);
    throw new ActVerificationUnavailableError(`The JWKS at ${url} could not be fetched; the token was NOT accepted.`, {
      cause,
    });
  }

  // The timer deliberately stays armed until the BODY is read. Clearing it when
  // the fetch resolves — i.e. when headers arrive — left the body being read
  // with no timeout and no abort signal, so a host answering 200 and then
  // stalling the body hung verifyAct() for ever, ignoring timeoutMs entirely.
  // Python reads the whole body inside its httpx timeout, so this was also a
  // cross-SDK divergence, on a path whose entire purpose is to fail closed.
  try {
    if (!response.ok) {
      throw new ActVerificationUnavailableError(
        `The JWKS at ${url} returned HTTP ${response.status}; the token was NOT accepted.`,
      );
    }
    return await response.json();
  } catch (cause) {
    if (cause instanceof ActVerificationUnavailableError) throw cause;
    throw new ActVerificationUnavailableError(`The JWKS at ${url} is not valid JSON; the token was NOT accepted.`, {
      cause,
    });
  } finally {
    clearTimeout(timer);
  }
}

/** Pick the JWK named by `kid`. No fallback: unknown key ⇒ rejection. */
function selectKey(keySet: unknown, keyId: string): Record<string, unknown> {
  if (!isRecord(keySet)) {
    throw new ActVerificationUnavailableError(
      "The key set is neither a JWKS object ({ keys: [...] }) nor a single JWK; the token was NOT accepted.",
    );
  }
  let keys: unknown[];
  if ("keys" in keySet) {
    if (!Array.isArray(keySet.keys)) {
      throw new ActVerificationUnavailableError("The JWKS 'keys' member is not an array; the token was NOT accepted.");
    }
    keys = keySet.keys;
  } else {
    keys = [keySet]; // a bare JWK is accepted as a one-key set
  }

  const matches = keys.filter((key) => isRecord(key) && key.kid === keyId);
  if (matches.length === 0) {
    throw new ActKeyNotFoundError(
      `No key with kid=${JSON.stringify(keyId)} in the supplied key set. The signing key may have ` +
        "been rotated: refresh the JWKS from /.well-known/jwks.json. No other key was tried.",
    );
  }
  if (matches.length > 1) {
    throw new ActVerificationUnavailableError(
      `The key set is ambiguous: ${matches.length} keys share kid=${JSON.stringify(keyId)}. ` +
        "Verification needs one unmistakable key; the token was NOT accepted.",
    );
  }
  return checkKeyUsable(matches[0] as Record<string, unknown>, keyId);
}

/** Reject a matching JWK that cannot verify an ES256 signature. */
function checkKeyUsable(jwk: Record<string, unknown>, keyId: string): Record<string, unknown> {
  // Checked first, and reported on its own class: a key set carrying private
  // material is an incident in itself, whatever else is wrong with the key. It
  // must not be caught by a handler that refreshes the key set and retries.
  for (const member of PRIVATE_JWK_MEMBERS) {
    if (member in jwk) {
      throw new ActPrivateKeyInKeySetError(
        `The key with kid=${JSON.stringify(keyId)} carries private key material ('${member}'), which never ` +
          "belongs in a key set: either a signing key was published by mistake, or the set was poisoned. " +
          "Rotate the signing key now and treat the set as compromised — do not retry. The token was NOT accepted.",
      );
    }
  }

  let reason: string | undefined;
  if (jwk.kty !== "EC") {
    reason = `key type ${JSON.stringify(jwk.kty)} is not 'EC'`;
  } else if (jwk.crv !== "P-256") {
    reason = `curve ${JSON.stringify(jwk.crv)} is not 'P-256'`;
  } else if (jwk.alg !== undefined && jwk.alg !== ACT_ALGORITHM) {
    reason = `the key declares alg ${JSON.stringify(jwk.alg)}`;
  } else if (jwk.use !== undefined && jwk.use !== "sig") {
    reason = `the key is marked for ${JSON.stringify(jwk.use)}, not signatures`;
  } else if (jwk.key_ops !== undefined && !(Array.isArray(jwk.key_ops) && jwk.key_ops.includes("verify"))) {
    reason = "the key does not allow the 'verify' operation";
  } else if (typeof jwk.x !== "string" || typeof jwk.y !== "string") {
    reason = "its 'x'/'y' coordinates are missing";
  }
  if (reason !== undefined) {
    throw new ActKeyNotFoundError(
      `The key with kid=${JSON.stringify(keyId)} cannot verify an ACT signature: ${reason}.`,
    );
  }
  return jwk;
}

// --- signature ----------------------------------------------------------

function checkSignature(signingInput: string, signature: Buffer, jwk: Record<string, unknown>, keyId: string): void {
  if (signature.length !== SIGNATURE_BYTES) {
    throw new ActSignatureError(
      `An ES256 signature is ${SIGNATURE_BYTES} raw bytes (r‖s); this one is ${signature.length}. ` +
        "A JWS carries the P1363 form, never DER.",
    );
  }
  let key: KeyObject;
  try {
    key = createPublicKey({ key: jwk as JsonWebKey, format: "jwk" });
  } catch (cause) {
    throw new ActKeyNotFoundError(
      `The key with kid=${JSON.stringify(keyId)} could not be read as an EC P-256 public key.`,
      { cause },
    );
  }
  let valid = false;
  try {
    valid = cryptoVerify("sha256", Buffer.from(signingInput, "ascii"), { key, dsaEncoding: "ieee-p1363" }, signature);
  } catch (cause) {
    throw new ActSignatureError("The signature could not be checked; the token was NOT accepted.", { cause });
  }
  if (!valid) {
    throw new ActSignatureError(
      "The ES256 signature does not match the token's header and payload: the token was " +
        "altered after signing, or was never signed by the key it names.",
    );
  }
}

// --- claims -------------------------------------------------------------

function requireString(claims: Record<string, unknown>, name: string): string {
  const value = requirePresent(claims, name);
  if (typeof value !== "string" || !value) {
    throw new ActMalformedError(`Claim '${name}' must be a non-empty string; got ${JSON.stringify(value)}.`);
  }
  return value;
}

function requireTimestamp(claims: Record<string, unknown>, name: string): number {
  const value = requirePresent(claims, name);
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new ActMalformedError(`Claim '${name}' must be an integer epoch timestamp; got ${JSON.stringify(value)}.`);
  }
  return value;
}

function requireObject(claims: Record<string, unknown>, name: string): Record<string, unknown> {
  const value = requirePresent(claims, name);
  if (!isRecord(value)) {
    throw new ActMalformedError(`Claim '${name}' must be a JSON object.`);
  }
  return value;
}

function requirePresent(claims: Record<string, unknown>, name: string): unknown {
  if (!(name in claims)) {
    throw new ActMalformedError(
      `The token carries no '${name}' claim, so it is not a complete ACT token ` +
        "(see docs/spec/act-token.md §3).",
    );
  }
  return claims[name];
}

/** Resolve the accepted issuers; the test issuer needs an explicit opt-in. */
function resolveIssuers(options: VerifyActOptions): Set<string> {
  const expected = new Set<string>(
    options.issuer === undefined
      ? [PRODUCTION_ISSUER]
      : typeof options.issuer === "string"
        ? [options.issuer]
        : options.issuer,
  );
  if (options.allowTestIssuer) expected.add(TEST_ISSUER);
  if (expected.size === 0 || [...expected].some((issuer) => !issuer)) {
    throw new TypeError(
      "verifyAct() needs at least one non-empty expected issuer; an empty issuer would " +
        "accept a token from anywhere.",
    );
  }
  return expected;
}

/** Enforce `exp`, `iat` and — when present — `nbf`. */
function checkTimeWindow(
  claims: Record<string, unknown>,
  issuedAt: number,
  expiresAt: number,
  leeway: number,
  now: number,
): void {
  if (now > expiresAt + leeway) {
    throw new ActExpiredError(
      `The token expired at epoch ${expiresAt} (${Math.trunc(now - expiresAt)} s ago): it proves ` +
        "nothing about a call made now. ACT tokens live one hour.",
    );
  }
  if (now < issuedAt - leeway) {
    throw new ActNotYetValidError(
      `Claim 'iat' is ${Math.trunc(issuedAt - now)} s in the future. Check this machine's clock ` +
        "before suspecting the token.",
    );
  }
  if ("nbf" in claims) {
    const notBefore = requireTimestamp(claims, "nbf");
    if (now < notBefore - leeway) {
      throw new ActNotYetValidError(
        `Claim 'nbf' is ${Math.trunc(notBefore - now)} s in the future: the token is not valid yet.`,
      );
    }
  }
}

// --- helpers ------------------------------------------------------------

/** Compare two claim values without leaking a match position by timing. */
function constantTimeEqual(claimed: string, expected: string): boolean {
  const a = Buffer.from(claimed, "utf8");
  const b = Buffer.from(expected, "utf8");
  // timingSafeEqual needs equal lengths; a length difference is not a secret.
  return a.length === b.length && timingSafeEqual(a, b);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
