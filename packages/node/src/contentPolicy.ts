// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * The organization's prompt-cleaning policy, as signed by the cloud, applied here.
 *
 * `GET /v1/content-policy` answers `{ "enabled": false }` when the application
 * cleans nothing, or the policy as a compact JWS (ES256, `typ:
 * content-policy+jwt`), a challenge and how long to keep it. The SDK applies a
 * policy only after checking, before any AI call relies on it: the signature
 * under the key the JWS names in `/.well-known/jwks.json` (the key set ACT
 * tokens are verified with), that this key is a policy key (the cloud names
 * each signing key after its purpose — `content-policy-g1`, `act-g1` — so a key
 * that signs proofs never signs what a call is cleaned by), `typ`, `alg`,
 * `iss`, `exp`, `iat`, that `digest`
 * is the SDK's own RFC 8785 digest of `policy`, and that `app_id` is the
 * policy's. Any failure is a `ContentPolicyError` and the AI call is not made.
 * A cloud that cannot be reached is a `ConsentUnreachableError` — unless the
 * policy held is still within its validity. That validity is `exp - iat`
 * seconds from the policy's reception, counted on the monotonic clock, and
 * never past `exp` by the wall clock: a machine running behind keeps it no
 * longer than the cloud meant. Python's twin is `idside._content_policy`; both
 * behave alike, line for line.
 */

import {
  ACT_ALGORITHM,
  PRODUCTION_ISSUER,
  TEST_ISSUER,
  checkNoCriticalExtensions,
  checkSignature,
  decodeSegment,
  jsonObject,
  readKeyId,
  selectKey,
} from "./act.ts";
import type { ContentFields } from "./descriptors.ts";
import { ActVerificationError, ConsentUnreachableError, ContentPolicyError } from "./exceptions.ts";
import { challengeResponse, ContentCleaner, ENGINE_VERSION, buildDigest, contentPolicyDigest, rulesDigest } from "./cleaning/index.ts";

export const CONTENT_POLICY_PATH = "/v1/content-policy";
export const CONTENT_EVENTS_PATH = "/v1/content-events";
export const JWKS_PATH = "/.well-known/jwks.json";
export const POLICY_TYPE = "content-policy+jwt";
/**
 * How the cloud names a key of the policy purpose (`content-policy-g1`, with
 * `-staging` or `-dev` outside production). One key set holds every purpose;
 * this prefix is what tells them apart.
 */
export const POLICY_KEY_PREFIX = "content-policy-";
/** Seconds a clock may be ahead of the cloud's when reading `iat`. */
export const CLOCK_SKEW_SECONDS = 300;
/** How long a disabled answer stands when it cannot be refreshed: a signed policy's window. */
export const DISABLED_VALIDITY_SECONDS = 900;
/**
 * While a valid policy is held, the cloud is asked for a fresh one at most once
 * per this many seconds — whether the last attempt succeeded or failed, and
 * whatever an authorize answer says.
 */
export const MIN_REFRESH_INTERVAL_SECONDS = 10;
export const DEFAULT_REFRESH_AFTER_SECONDS = 300;
/**
 * A 429 on the policy route while no valid policy is held is retried once,
 * after the `Retry-After` the cloud sent — delta-seconds only — capped here.
 */
export const RETRY_AFTER_CAP_SECONDS = 2;
/** The report of a blocked call is best effort: it waits this long at most. */
export const EVENTS_TIMEOUT_MS = 2_000;
/** What `POST /v1/content-events` keeps as a provider's name (`openai`). */
export const EVENT_PROVIDER = /^[a-z0-9][a-z0-9_-]{0,63}$/;
/**
 * What it keeps as a model: 1 to 200 printable characters, no blank at either
 * end. ASCII only — a subset of what the service reads as printable, so that no
 * Unicode table, in either SDK or in the service, can disagree (and a UTF-16
 * length is a character count).
 */
export const EVENT_MODEL = /^[\x21-\x7e](?:[\x20-\x7e]{0,198}[\x21-\x7e])?$/;

/**
 * Which provider and model a blocked call was for, as the service keeps them.
 *
 * Each is left out — never sent empty or null — unless it is a string of the
 * shape the service accepts: a member it cannot read makes it refuse the whole
 * report, and the blocked call would go uncounted. The model is the one the
 * caller wrote, never converted: `4` is not `"4"`. As in Python.
 */
export function eventLabels(provider: unknown, model: unknown): { provider?: string; model?: string } {
  const labels: { provider?: string; model?: string } = {};
  if (typeof provider === "string" && EVENT_PROVIDER.test(provider)) labels.provider = provider;
  if (typeof model === "string" && EVENT_MODEL.test(model)) labels.model = model;
  return labels;
}
/** The statuses of an authorize answer that ask for a fresh policy. */
const REFRESH_STATUSES = new Set(["outdated", "not_applied", "anomaly"]);

function refuse(what: string, cause?: unknown): ContentPolicyError {
  return new ContentPolicyError(
    `The prompt-cleaning policy from the ID Side cloud ${what}; the AI call was not made.`,
    cause === undefined ? undefined : { cause },
  );
}

/**
 * A policy that checks out but not by this machine's clock: say what to check.
 *
 * Only ever thrown for a policy just received, once its signature, issuer,
 * digest and application have checked out — so the date is what is wrong: this
 * machine's clock, or, for an answer already expired, a cache that served an
 * old one. Names no value of the policy; Python's `_out_of_step()` says the
 * same words.
 */
function outOfStep(where: string, cause: string, check: string): ContentPolicyError {
  return new ContentPolicyError(
    `The prompt-cleaning policy just received from the ID Side cloud is signed and intact, but this machine's clock ` +
      `puts it ${where}: ${cause}. Check that this machine's clock is set (NTP)${check}; the AI call was not made.`,
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** One verified answer of `/v1/content-policy`, ready to apply. */
export class PolicyState {
  /** The policy document, or `undefined` when the application cleans nothing. */
  readonly policy: Record<string, unknown> | undefined;
  /** The `content_policy` field of an authorize request, minus the counts. */
  readonly declaration: Record<string, unknown> | undefined;
  readonly digest: string | undefined;
  /** Monotonic seconds (`clock.monotonic`): refresh once past, keep using until `validUntil`. */
  readonly refreshAt: number;
  readonly validUntil: number;
  /**
   * Wall-clock seconds: the signed `exp`, which also bounds the use of a policy
   * held (an answer that cleans nothing has none).
   */
  readonly expiresAt: number;
  readonly #cleaners: Map<string, ContentCleaner>;

  constructor(
    policy: Record<string, unknown> | undefined,
    declaration: Record<string, unknown> | undefined,
    digest: string | undefined,
    refreshAt: number,
    validUntil: number,
    expiresAt = Number.POSITIVE_INFINITY,
    cleaners: Map<string, ContentCleaner> = new Map(),
  ) {
    this.policy = policy;
    this.declaration = declaration;
    this.digest = digest;
    this.refreshAt = refreshAt;
    this.validUntil = validUntil;
    this.expiresAt = expiresAt;
    this.#cleaners = cleaners;
  }

  get enabled(): boolean {
    return this.policy !== undefined;
  }

  /**
   * The same policy, checked and timed afresh: the cleaners compiled for it
   * carry over, since its digest names the same document.
   */
  renewed(declaration: Record<string, unknown>, refreshAt: number, validUntil: number, expiresAt: number): PolicyState {
    return new PolicyState(this.policy, declaration, this.digest, refreshAt, validUntil, expiresAt, this.#cleaners);
  }

  /** The policy compiled with this provider's content fields, once. */
  cleanerFor(provider: string, fields: ContentFields): ContentCleaner {
    let cleaner = this.#cleaners.get(provider);
    if (cleaner === undefined) {
      cleaner = compiler.cleaner(this.policy, fields);
      this.#cleaners.set(provider, cleaner);
    }
    return cleaner;
  }

  /** The same state, due for a refresh at `mono` (monotonic seconds). */
  dueAt(mono: number): PolicyState {
    return new PolicyState(
      this.policy,
      this.declaration,
      this.digest,
      mono,
      this.validUntil,
      this.expiresAt,
      this.#cleaners,
    );
  }
}

/** A `/v1/content-policy` answer read, its signature not yet checked. */
export interface PendingPolicy {
  readonly enabled: boolean;
  readonly refreshAfter: number;
  readonly keyId: string;
  readonly signingInput: string;
  readonly signature: Buffer;
  readonly claims: Record<string, unknown>;
  readonly challenge: { token: string; inputs: string[] };
}

/** Read one answer of the endpoint; refuse anything malformed. */
export function readAnswer(data: unknown): PendingPolicy {
  if (!isRecord(data)) throw refuse("is not a JSON object");
  const enabled = data.enabled;
  if (typeof enabled !== "boolean") throw refuse("does not say whether cleaning is enabled");
  const refreshAfter = data.refresh_after === undefined ? DEFAULT_REFRESH_AFTER_SECONDS : data.refresh_after;
  if (typeof refreshAfter !== "number" || !(refreshAfter >= 0 && refreshAfter <= 86_400)) {
    throw refuse("has a `refresh_after` that is not a number of seconds");
  }
  const none = { keyId: "", signingInput: "", signature: Buffer.alloc(0), claims: {}, challenge: { token: "", inputs: [] } };
  if (!enabled) return { enabled: false, refreshAfter, ...none };
  const token = data.policy;
  const challenge = data.challenge;
  if (typeof token !== "string" || token.length > 1_000_000) throw refuse("carries no signed policy");
  if (
    !isRecord(challenge) ||
    typeof challenge.token !== "string" ||
    challenge.token === "" ||
    !Array.isArray(challenge.inputs) ||
    !challenge.inputs.every((text) => typeof text === "string") ||
    challenge.inputs.length > 64
  ) {
    throw refuse("carries no usable challenge");
  }
  const segments = token.split(".");
  if (segments.length !== 3 || !segments.every((segment) => segment.length > 0)) throw refuse("is not a compact JWS");
  const [headerSegment, claimsSegment, signatureSegment] = segments;
  let header: Record<string, unknown>;
  let claims: Record<string, unknown>;
  let signature: Buffer;
  let keyId: string;
  try {
    header = jsonObject(decodeSegment(headerSegment, "header"), "header");
    claims = jsonObject(decodeSegment(claimsSegment, "claims"), "claims");
    signature = decodeSegment(signatureSegment, "signature");
    checkNoCriticalExtensions(header);
    keyId = readKeyId(header);
  } catch (cause) {
    throw refuse("is not a well-formed JWS", cause);
  }
  if (header.alg !== ACT_ALGORITHM) throw refuse(`is signed with ${JSON.stringify(header.alg)}, not ES256`);
  if (header.typ !== POLICY_TYPE) throw refuse(`is of type ${JSON.stringify(header.typ)}, not "${POLICY_TYPE}"`);
  if (!keyId.startsWith(POLICY_KEY_PREFIX)) throw refuse("is not signed with a prompt-cleaning policy key");
  return {
    enabled: true,
    refreshAfter,
    keyId,
    signingInput: `${headerSegment}.${claimsSegment}`,
    signature,
    claims,
    challenge: { token: challenge.token, inputs: challenge.inputs as string[] },
  };
}

function digestOrUndefined(policy: unknown): string | undefined {
  try {
    return contentPolicyDigest(policy);
  } catch {
    return undefined;
  }
}

/**
 * Check a signed answer against the key set and the clock; build its state.
 *
 * `now` is the wall clock, which the signed dates are read against; how long
 * the state is then held is counted from here on `clock.monotonic`. `held` is
 * the state this answer refreshes, if any: when it names the same digest, the
 * policy is the same and its compiled cleaners carry over — checked and timed
 * afresh, compiled once per digest.
 */
export function verifyPending(pending: PendingPolicy, jwks: unknown, now: number, held?: PolicyState): PolicyState {
  const received = clock.monotonic();
  if (!pending.enabled) {
    return new PolicyState(
      undefined,
      undefined,
      undefined,
      received + pending.refreshAfter,
      received + Math.max(pending.refreshAfter, DISABLED_VALIDITY_SECONDS),
    );
  }
  try {
    checkSignature(pending.signingInput, pending.signature, selectKey(jwks, pending.keyId), pending.keyId);
  } catch (cause) {
    const signature = cause instanceof ActVerificationError && cause.name === "ActSignatureError";
    throw refuse(
      signature ? "does not carry a valid ID Side signature" : `names a key the key set cannot verify with (${(cause as Error).message})`,
      cause,
    );
  }
  const claims = pending.claims;
  if (claims.iss !== PRODUCTION_ISSUER && claims.iss !== TEST_ISSUER) throw refuse("was not issued by ID Side");
  const issued = claims.iat;
  const expires = claims.exp;
  if (!Number.isInteger(issued) || !Number.isInteger(expires)) throw refuse("has no integer `iat` and `exp`");
  if ((expires as number) <= (issued as number)) throw refuse("expires before it was issued");
  const policy = claims.policy;
  // A string, first: a policy the canonicaliser cannot write has no digest,
  // and "none" must not equal a claim that is not there.
  if (typeof claims.digest !== "string" || digestOrUndefined(policy) !== claims.digest) {
    throw refuse("does not match its own digest");
  }
  if (!isRecord(policy) || typeof claims.app_id !== "string" || policy.app_id !== claims.app_id) {
    throw refuse("is not the policy of the application it was issued for");
  }
  // Last: everything else has checked out, so a date out of step with this
  // machine is its clock (the bounds themselves do not move).
  if ((expires as number) <= now) {
    throw outOfStep(
      "past its expiry",
      "either the clock looks ahead, or a cache between this machine and the cloud served an old answer",
      " and that no proxy caches /v1/content-policy",
    );
  }
  if ((issued as number) > now + CLOCK_SKEW_SECONDS) throw outOfStep("before its date of issue", "the clock looks behind", "");
  const same = held !== undefined && held.digest === claims.digest;
  // Compiled once per digest: ContentPolicyError if it cannot apply.
  if (!same) compiler.cleaner(policy);
  const declaration = {
    digest: claims.digest,
    engine: ENGINE_VERSION,
    rules: rulesDigest(),
    build: buildDigest(),
    challenge: {
      token: pending.challenge.token,
      response: challengeResponse(pending.challenge.token, pending.challenge.inputs),
    },
  };
  const lifetime = (expires as number) - (issued as number);
  const refreshAt = received + Math.min(pending.refreshAfter, lifetime);
  if (same) return held.renewed(declaration, refreshAt, received + lifetime, expires as number);
  return new PolicyState(
    policy,
    declaration,
    claims.digest,
    refreshAt,
    received + lifetime,
    expires as number,
  );
}

/**
 * What leads the message of a refusal the policy route qualifies (401, 402,
 * 403, 409, 422, 429): its class is the one it has on `/v1/authorize`, and its
 * words say which route answered. Python's `POLICY_ROUTE_LEAD` is the same.
 */
export const POLICY_ROUTE_LEAD = "While fetching the prompt-cleaning policy: ";

/** Why a call stops when no policy could be fetched; Python's `unfetched()` twin. */
export function unfetched(status?: number): string {
  const said = status === undefined ? "" : ` (HTTP ${status})`;
  return (
    `The ID Side cloud's prompt-cleaning policy could not be fetched${said}, and none is held that is still valid; ` +
    "the AI call was not made."
  );
}

export function unreachable(originHint: string, cause?: unknown): ConsentUnreachableError {
  const message = unfetched();
  return new ConsentUnreachableError(originHint ? `${message} ${originHint}` : message, cause === undefined ? undefined : { cause });
}

/**
 * The wait a `Retry-After` asks for, capped; `undefined` when it says none.
 *
 * Only delta-seconds (ASCII digits) are read: a date, a fraction or a sign asks
 * for nothing this SDK will wait for, and the call then fails closed at once.
 * `Headers` hands the value without the spaces and tabs around it, which
 * Python's `retry_after()` strips itself; both read the same.
 */
export function retryAfter(value: string | null): number | undefined {
  const text = value ?? "";
  if (!/^[0-9]+$/.test(text)) return undefined;
  return Math.min(Number(text), RETRY_AFTER_CAP_SECONDS);
}

/**
 * What a consent client holds between calls: the state, the key set, timing.
 * When to fetch, and what to do when fetching fails, is decided here once.
 */
export class PolicyCache {
  state: PolicyState | undefined;
  jwks: unknown;
  /** Monotonic seconds: when an authorize answer last asked for a fresh policy. */
  #forcedAt = Number.NEGATIVE_INFINITY;
  /** Monotonic seconds: when the cloud was last asked for the policy, whatever it answered. */
  attemptedAt = Number.NEGATIVE_INFINITY;

  knows(keyId: string): boolean {
    const keys = isRecord(this.jwks) ? this.jwks.keys : undefined;
    return Array.isArray(keys) && keys.some((key) => isRecord(key) && key.kid === keyId);
  }

  /**
   * The state to apply without asking the cloud, or `undefined` to ask it now.
   *
   * The state held until its refresh is due; then, while it is still valid, the
   * cloud is asked at most once per `MIN_REFRESH_INTERVAL_SECONDS`, so an
   * incident does not make every AI call pay a fetch that will fail. Once
   * nothing valid is held, never: an attempt may unblock the call. `now` is
   * the wall clock, `mono` the monotonic one.
   */
  held(now: number, mono: number): PolicyState | undefined {
    const state = this.usable(now, mono);
    if (state === undefined) return undefined;
    if (mono < state.refreshAt) return state;
    if (mono - this.attemptedAt < MIN_REFRESH_INTERVAL_SECONDS) return state;
    return undefined;
  }

  /** The held state if it may still be applied after a failed refresh. */
  usable(now: number, mono: number): PolicyState | undefined {
    const state = this.state;
    if (state === undefined || mono >= state.validUntil || now >= state.expiresAt) return undefined;
    return state;
  }

  /**
   * Seconds to wait before the one retry a 429 earns, or `undefined`: no retry.
   * Only while no valid policy is held: with one held, the call applies it at
   * once and waits for nothing.
   */
  retryDelay(status: number, header: string | null, now: number, mono: number): number | undefined {
    if (status !== 429 || this.usable(now, mono) !== undefined) return undefined;
    return retryAfter(header);
  }

  /** React to an authorize answer's `content_policy`: refresh when asked. */
  noteAnswer(answer: Readonly<Record<string, unknown>> | undefined, mono: number): void {
    const status = answer?.status;
    if (
      typeof status === "string" &&
      REFRESH_STATUSES.has(status) &&
      this.state !== undefined &&
      mono - this.#forcedAt >= MIN_REFRESH_INTERVAL_SECONDS
    ) {
      this.#forcedAt = mono;
      this.state = this.state.dueAt(mono);
    }
  }
}

/**
 * How a policy is compiled into a cleaner: one seam, as `clock` is, so that a
 * test can count compilations. Python's twin is the module's `ContentCleaner`.
 */
export const compiler = {
  cleaner: (policy: unknown, fields?: ContentFields): ContentCleaner => new ContentCleaner(policy, fields),
};

/**
 * The clocks in seconds, and the wait before the one retry after a 429, as
 * seams for tests. `monotonic` is a clock no one can set: how long a held
 * answer has been held, measured here.
 */
export const clock = {
  now: (): number => Date.now() / 1000,
  monotonic: (): number => performance.now() / 1000,
  sleep: (seconds: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, seconds * 1000)),
};
