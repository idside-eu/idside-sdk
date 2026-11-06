// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Transport to the ID Side cloud.
 *
 * The SDK never signs ACT tokens itself (the private ECDSA keys live in the
 * cloud). It asks the cloud to authorize a request: the cloud resolves the
 * user's preferences AND returns a signed ACT token bound to this exact request.
 */

import { createRequire } from "node:module";

import {
  AuthenticationError,
  BindingRequiredError,
  InvalidBindingError,
  ConflictingIdentityError,
  ConsentDeniedError,
  ConsentRefreshRequiredError,
  ConsentUnreachableError,
  ContractError,
  MissingIdentityError,
  QuotaExceededError,
  RateLimitedError,
} from "./exceptions.ts";

export const DEFAULT_BASE_URL = "https://idside.eu";

/**
 * Appended to an outage-shaped failure when the address was the default: with
 * neither `baseUrl` nor `IDSIDE_BASE_URL`, a developer who forgot the variable
 * read `Could not verify consent (HTTP 404)` and went looking for a network
 * fault. Word for word the Python SDK's sentence.
 */
const DEFAULT_ORIGIN_HINT =
  `No ID Side address was configured, so the SDK used its default, ${DEFAULT_BASE_URL}: ` +
  "set IDSIDE_BASE_URL to the address shown in your ID Side dashboard, or pass that address explicitly.";

/**
 * What the cloud counts as blank — ActiveSupport's `blank?`, the Unicode
 * White_Space characters — and nothing wider: the 25 code points below, the
 * same list the Python SDK holds. `trim()` would not do: it disagrees with the
 * cloud on U+FEFF and U+0085, and Python's `strip()` on U+001C-001F.
 */
const BLANK = /^[\t\n\v\f\r \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/;

/** Whether `value` is made only of what the cloud counts as blank. */
export function isBlank(value: string): boolean {
  return BLANK.test(value);
}

/**
 * Whether an identity argument names somebody, as the Python SDK decides it.
 * Empty, `undefined`, `null`, `0` and `false` never did, nor does an empty
 * array or an object with no keys (Python's `[]` and `{}`); a string made only
 * of blanks does not either — the cloud refuses it with a 422. Nothing is
 * trimmed and nothing is converted: an identity that names somebody is sent
 * exactly as written, because its pseudonym is derived from it, and the cloud
 * alone judges whether it is one.
 */
export function namesSomebody(identity: unknown): boolean {
  return Boolean(identity) && !(typeof identity === "string" && isBlank(identity)) && !isEmpty(identity);
}

/** An array, or a plain object, with nothing in it. */
function isEmpty(value: unknown): boolean {
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value !== "object") return false;
  const prototype: unknown = Object.getPrototypeOf(value);
  return (prototype === Object.prototype || prototype === null) && Object.keys(value as object).length === 0;
}

/** `JSON.rawJSON` (Node 21 and later, so under the engines floor) is not in the TypeScript lib this package targets. */
const { rawJSON } = JSON as unknown as { rawJSON(text: string): unknown };

/**
 * `JSON.stringify` replacer for the authorize body. A BigInt — the 64-bit key a
 * database driver hands back — is written as the integer it is, every digit
 * kept, as Python writes an int; `JSON.stringify` alone throws on one.
 *
 * What JSON has no form for throws, as Python's encoder throws on it, instead
 * of being dropped (a function, a symbol) or written as `null` (a non-finite
 * number): as an identity, the first sent an authorize body naming nobody and
 * the second `user_id: null`. The value itself is never quoted. Everything else
 * is left to JSON.
 */
function asWritten(_key: string, value: unknown): unknown {
  if (typeof value === "bigint") return rawJSON(value.toString());
  const kind =
    typeof value === "function" || typeof value === "symbol"
      ? `a ${typeof value}`
      : typeof value === "number" && !Number.isFinite(value)
        ? "a non-finite number"
        : undefined;
  if (kind !== undefined) {
    throw new TypeError(`The authorize payload cannot be written as JSON: it holds ${kind}, which JSON has no form for.`);
  }
  return value;
}

function withHint(message: string, originHint: string): string {
  return originHint ? `${message} ${originHint}` : message;
}
const AUTHORIZE_PATH = "/v1/authorize";

const require_ = createRequire(import.meta.url);

/**
 * Identifies the SDK to the cloud on every request (mirrors the Python SDK).
 *
 * Read from the manifest rather than written down: a hand-kept copy drifts at
 * the one moment it matters, a release, and the whole point of the header is
 * to name the version an incident is happening on.
 */
export const USER_AGENT = `idside-node/${(require_("../package.json") as { version: string }).version}`;

/**
 * The deadline the authorize hop gets, in milliseconds — one number, shared
 * with the Python SDK, which applies the same one to every phase of its `httpx`
 * request.
 *
 * On a fail-closed path a timeout is not a tuning knob: it is the moment the
 * product decides the cloud is dead and stops the caller's AI calls. Two SDKs
 * with two numbers is one network incident cutting a Python application and
 * sparing its Node equivalent, for no reason a customer could be told.
 *
 * Five seconds, not ten, because waiting is what costs here: the call is a
 * small JSON POST (a lookup and a signature) whose healthy p99 is far under a
 * second, and the alternative to waiting is the documented refusal this SDK
 * exists to raise, never a silent pass. An authorize hop still open after five
 * seconds is already an incident.
 */
export const DEFAULT_TIMEOUT_MS = 5_000;

/**
 * A posture is a label a developer types into their own configuration. Anything
 * longer is not one, and this value is quoted back in error messages.
 */
const MAX_POSTURE_LENGTH = 200;
const MAX_ACT_TOKEN_LENGTH = 8_192;
const BINDINGS: ReadonlySet<string> = new Set(["ubt", "user_id"]);
const TOOL_CONSTRAINTS: ReadonlySet<string> = new Set(["refuse", "strip"]);
const NON_PRINTABLE_UNICODE = /[\p{C}\p{Z}]/u;

/**
 * Every field a successful decision must carry, checked for presence before any
 * value is judged — so an absent field is reported as missing in both SDKs
 * rather than as the wrong value in one of them. `docs/versioning.md` §1.1 is
 * the contract; `constraints` and `credential_posture` stay optional.
 */
const MANDATORY_DECISION_FIELDS = [
  "decision",
  "consent_state",
  "source",
  "legal_basis",
  "binding",
  "params_forced",
  "mode",
  "act_token",
  "token_id",
  "expires_at",
] as const;

/**
 * 403 problem+json `type` values the cloud uses to qualify a denial.
 *
 * Only what the cloud actually sends belongs here. `provider_blocked` sat in
 * this set for months and was never emitted by anything: a reason the SDK names
 * but nothing produces is a branch a caller writes and never takes, and dead
 * code that reads as tested behaviour. Adding one back is a cloud change first,
 * an SDK change second.
 *
 * `policy_hold` comes from an account in good standing: an organization's policy could
 * not apply to this one call, and the application is set to hold such calls. An
 * SDK that predates it still raises the denial, without the reason.
 */
const DENY_REASONS = new Set([
  "fraud_suspended",
  "organization_suspended",
  "policy_hold",
]);

/**
 * RFC 9457 says `type` is a URI, and the cloud sends one. This SDK used to
 * compare it against the bare names above, so `reason` was ALWAYS undefined —
 * for `fraud_suspended` too — while the documentation showed callers branching
 * on it. The denial itself always worked; only the explanation was missing,
 * which is why it went unnoticed.
 */
const PROBLEM_TYPE_PREFIX = "https://idside.eu/errors/";

/**
 * The error name a problem `type` carries, `""` if it is not a string.
 *
 * Callers compare the result against a CLOSED set of names, and that is what
 * keeps the reading closed: a `type` from another host, a deeper path under our
 * prefix (`…/errors/v2/<name>`) or a name this SDK does not know never equals
 * one of them, so it qualifies nothing. The bare form is still accepted so an
 * older cloud — or a test double — keeps working.
 */
function problemTypeName(typeValue: unknown): string {
  if (typeof typeValue !== "string") return "";
  return typeValue.startsWith(PROBLEM_TYPE_PREFIX)
    ? typeValue.slice(PROBLEM_TYPE_PREFIX.length)
    : typeValue;
}

/** Qualify a 403 from its problem `type`, or undefined if we cannot. */
function denyReason(typeValue: unknown): string | undefined {
  const name = problemTypeName(typeValue);
  return DENY_REASONS.has(name) ? name : undefined;
}

/**
 * The two 401s that are about the visitor, not the key, with what to do. Both
 * names are the cloud's (`ubt_invalid`: any UBT that does not verify — expired,
 * issued for another app, altered, or not a UBT; `ubt_required`: a strict app
 * called with `user_id`). Any other
 * 401 — `invalid_api_key`, a `type` this SDK does not know, no body at all —
 * stays a plain AuthenticationError: a value nobody classified never moves a
 * refusal into a class that says the key is fine.
 */
const BINDING_ERRORS: ReadonlyMap<
  string,
  readonly [typeof InvalidBindingError | typeof BindingRequiredError, string]
> = new Map([
  [
    "ubt_invalid",
    [
      InvalidBindingError,
      "ID Side refused this visitor's binding (HTTP 401): the UBT did not " +
        "verify — it expired, was issued for another application, was altered, " +
        "or is not a UBT at all. Your API key was accepted. " +
        "Discard the UBT stored in this visitor's session and let the widget " +
        "issue a new one before retrying; the AI call was not made.",
    ],
  ],
  [
    "ubt_required",
    [
      BindingRequiredError,
      "This application requires a UBT (HTTP 401), and the call named its " +
        "user with user_id. Your API key was accepted. Have the visitor go " +
        "through the widget and pass the UBT it issues as ubt before any AI " +
        "call; the AI call was not made.",
    ],
  ],
]);

/** The cloud's authorization decision for a single AI request. */
export interface ConsentDecision {
  readonly consentState: Record<string, unknown>;
  readonly paramsForced: Record<string, unknown>;
  readonly actToken: string;
  readonly source: string;
  readonly legalBasis: string;
  /**
   * How strongly the end user was tied to this request — the evidentiary weight
   * of the proof. `"ubt"`: the ID Side widget saw the user and the cloud
   * verified the signed binding. `"user_id"`: the developer asserted it (weaker).
   */
  readonly binding: string;
  /**
   * Non-parameter restrictions the SDK must apply to the outgoing call — today
   * only `{ tools: "refuse" | "strip" }`. Separate from `paramsForced` on
   * purpose: a forced parameter sets a value, and the cloud is never allowed to
   * write the developer's request. A constraint rejects a call instead, which is
   * a different kind of authority.
   *
   * It follows from the user's consent state alone: the cloud is not told
   * whether this call carries tools, and could not be without learning
   * something about the request.
   */
  readonly constraints: Record<string, unknown>;
  /**
   * The label of the credential this call must go out on, or `undefined` when
   * the client as constructed is fine. It answers a policy question — *which
   * privacy posture does this user's consent require?* — which is why the cloud
   * answers it and the SDK only matches the label against what the developer
   * provisioned. Naming one that was not provisioned fails the call
   * (`MissingCredentialError`): the cloud must name what the answer requires,
   * never the best of what happens to be available.
   */
  readonly credentialPosture?: string;
}

export interface AuthorizeRequest {
  readonly provider: string;
  readonly model: string;
  readonly requestHash: string;
  /**
   * The identity — `userId` or `ubt`, never both — exactly as the caller wrote
   * it. `unknown` because that is what it is: `Enforced<T>` asks a TypeScript
   * caller for a string, but a JavaScript caller can pass anything, and it is
   * forwarded untouched. The cloud alone judges it (a non-blank string or a
   * finite number names somebody, anything else is a 422), as it does for the
   * Python SDK.
   */
  readonly userId?: unknown;
  readonly ubt?: unknown;
  /**
   * The endpoint about to be called (`responses.create`, `chat.completions.create`…).
   *
   * Mechanics, not policy: the cloud needs it because a provider's endpoints do
   * not accept the same parameters, and forcing one where it does not exist
   * breaks the call for exactly the users who opted out. Omitted when unknown,
   * in which case the cloud falls back to what is safe on every endpoint of
   * that provider.
   */
  readonly callSite?: string;
  /**
   * The posture labels the developer provisioned for this provider (the keys of
   * `credentials` on `consent.wrap()`).
   *
   * Sent on every request, empty array included, because its PRESENCE is what
   * tells the cloud this SDK understands routing at all: an older one sends no
   * such field and would ignore a posture it was handed, which is the one case
   * where the cloud must not count on the call being routed. Labels are app
   * configuration, never credentials — the credential itself never leaves the
   * process.
   */
  readonly credentialPostures?: readonly string[];
}

/** The interface the SDK uses to authorize a request against the cloud. */
export interface ConsentClient {
  authorize(request: AuthorizeRequest): Promise<ConsentDecision>;
}

export interface HttpConsentClientOptions {
  /** The ID Side secret key; else `IDSIDE_API_KEY`; else none is sent. */
  apiKey?: string;
  timeoutMs?: number;
  /** Injectable for tests; defaults to the global `fetch`. */
  fetch?: typeof fetch;
}

const UNSENDABLE_KEY =
  "The ID Side API key cannot be sent: it contains a character an HTTP header cannot carry (a space, a tab " +
  "or a line break inside it, or a character outside printable ASCII), often left by the file or variable it " +
  "was read from. Check the apiKey passed to consent.wrap() and IDSIDE_API_KEY; the key itself is not repeated here.";

/**
 * The key as it will be sent, `undefined` when there is none.
 *
 * Whitespace around it is dropped, as `fetch` drops it around any header value
 * (and as the Python SDK now does too). Anything else a header cannot carry is
 * refused here, before a request exists: `fetch` refused it with an error
 * quoting the whole header — key included — chained under an "unreachable"
 * that sent the developer looking for an outage.
 */
function sendableKey(apiKey: string | undefined): string | undefined {
  const key = (apiKey ?? "").replace(/^[\t\n\r ]+|[\t\n\r ]+$/g, "");
  if (!key) return undefined;
  if (!/^[\x21-\x7e]+$/.test(key)) throw new TypeError(UNSENDABLE_KEY);
  return key;
}

/** Default {@link ConsentClient} backed by `idside.eu`, using native fetch. */
export class HttpConsentClient implements ConsentClient {
  readonly #baseUrl: string;
  readonly #apiKey: string | undefined;
  readonly #timeoutMs: number;
  readonly #fetch: typeof fetch;
  /** The sentence an outage-shaped failure carries when no address was given. */
  readonly #originHint: string;

  /**
   * The address is resolved here, in the order the Python transport uses: the
   * `baseUrl` argument, else `IDSIDE_BASE_URL`, else {@link DEFAULT_BASE_URL},
   * an empty string counting as absent at each step. Only the default makes an
   * outage-shaped failure say so and name `IDSIDE_BASE_URL`. The key likewise:
   * the `apiKey` option, else `IDSIDE_API_KEY`, else none — no `Authorization`
   * header at all, a key made only of whitespace included. Whitespace around a
   * key is dropped, and a key a header cannot carry throws a `TypeError` here.
   * It is sent in that header and nowhere else: no message and no journal line
   * carries it.
   */
  constructor(baseUrl?: string, options: HttpConsentClientOptions = {}) {
    const configured = baseUrl || process.env.IDSIDE_BASE_URL;
    this.#originHint = configured ? "" : DEFAULT_ORIGIN_HINT;
    this.#baseUrl = (configured || DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.#apiKey = sendableKey(options.apiKey || process.env.IDSIDE_API_KEY);
    this.#timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.#fetch = options.fetch ?? globalThis.fetch;
  }

  async authorize(request: AuthorizeRequest): Promise<ConsentDecision> {
    // A UBT is opaque to the SDK: it is forwarded as-is and the cloud alone
    // verifies its signature and binding. Blank names nobody, as empty did.
    const userId = namesSomebody(request.userId) ? request.userId : undefined;
    const ubt = namesSomebody(request.ubt) ? request.ubt : undefined;
    if (userId && ubt) {
      throw new ConflictingIdentityError(
        "Pass either user_id or ubt to authorize(), not both.",
      );
    }
    if (!userId && !ubt) {
      throw new MissingIdentityError(
        "authorize() needs an identity: pass user_id or ubt.",
      );
    }

    const body = {
      provider: request.provider,
      model: request.model,
      request_hash: request.requestHash,
      ...(request.callSite ? { call_site: request.callSite } : {}),
      credential_postures: [...(request.credentialPostures ?? [])],
      ...(ubt ? { ubt } : { user_id: userId }),
    };

    const response = await this.#post(body);

    if (!response.ok) {
      raiseForStatus(response.status, await problemBody(response), this.#originHint);
    }

    let data: unknown;
    try {
      data = await response.json();
    } catch (cause) {
      throw new ConsentUnreachableError(
        "ID Side cloud returned a malformed authorize response (invalid JSON); the AI call was not made.",
        { cause } as ErrorOptions,
      );
    }
    if (data === null || typeof data !== "object" || Array.isArray(data)) {
      throw new ConsentUnreachableError(
        "ID Side cloud returned a malformed authorize response (not a JSON object); the AI call was not made.",
      );
    }

    const record = data as Record<string, unknown>;
    for (const field of MANDATORY_DECISION_FIELDS) {
      if (!(field in record)) {
        throw new ConsentUnreachableError(
          `ID Side cloud returned an incomplete authorize response (missing field ${field}); the AI call was not made.`,
        );
      }
    }

    return decisionFrom(record);
  }

  /** Single POST with a hard timeout. No retry — see below. */
  async #post(body: unknown): Promise<Response> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "User-Agent": USER_AGENT,
    };
    if (this.#apiKey) headers.Authorization = `Bearer ${this.#apiKey}`;
    // Written before the request, outside the handling of network failures: an
    // identity JSON cannot write (a circular object) fails with JSON's own
    // TypeError, as Python's encoder fails with its own, and is never reported
    // as an unreachable cloud.
    const payload = JSON.stringify(body, asWritten);
    // Detach from `this` so a strict fetch implementation is never called with the
    // wrong receiver ("Illegal invocation").
    const doFetch = this.#fetch;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.#timeoutMs);
    try {
      return await doFetch(this.#baseUrl + AUTHORIZE_PATH, {
        method: "POST",
        headers,
        body: payload,
        signal: controller.signal,
      });
    } catch (cause) {
      // No retry: authorize is not safely idempotent. A request that reached the
      // cloud before the connection failed would already have minted an ACT token
      // and written an append-only ledger entry, so resending risks a duplicate.
      // Fail closed instead (matching the Python SDK, which never retries).
      throw new ConsentUnreachableError(
        withHint(
          "ID Side cloud is unreachable; the AI call was not made.",
          this.#originHint,
        ),
        { cause },
      );
    } finally {
      clearTimeout(timer);
    }
  }
}

function invalidDecision(
  field: string,
  expected: string,
): ConsentUnreachableError {
  return new ConsentUnreachableError(
    `ID Side cloud returned a malformed authorize response (field ${field} must be ${expected}); ` +
      "the AI call was not made.",
  );
}

function decisionObject(
  record: Record<string, unknown>,
  field: string,
  required = false,
): Record<string, unknown> {
  if (!(field in record)) {
    if (required) throw invalidDecision(field, "present as a JSON object");
    return {};
  }
  const value = record[field];
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw invalidDecision(field, "a JSON object");
  }
  return value as Record<string, unknown>;
}

function decisionString(
  record: Record<string, unknown>,
  field: string,
): string {
  const value = record[field];
  if (
    typeof value !== "string" ||
    value.trim().length === 0 ||
    !isPrintable(value)
  ) {
    throw invalidDecision(field, "a non-empty printable string");
  }
  return value;
}

/** Match Python str.isprintable(): ASCII space is the only printable separator. */
function isPrintable(value: string): boolean {
  for (const character of value) {
    if (character !== " " && NON_PRINTABLE_UNICODE.test(character))
      return false;
  }
  return true;
}

function actToken(record: Record<string, unknown>): string {
  const token = decisionString(record, "act_token");
  if (
    token.length > MAX_ACT_TOKEN_LENGTH ||
    !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)
  ) {
    throw invalidDecision("act_token", "a compact JWS");
  }
  return token;
}

function tokenId(record: Record<string, unknown>): string {
  const token = decisionString(record, "token_id");
  if (!/^act_[0-9a-z]+$/.test(token))
    throw invalidDecision("token_id", "an ACT token identifier");
  return token;
}

const RFC3339 =
  /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:[Zz]|[+-](\d{2}):(\d{2}))$/;

function expiresAt(record: Record<string, unknown>): string {
  const value = decisionString(record, "expires_at");
  const match = RFC3339.exec(value);
  if (match === null)
    throw invalidDecision("expires_at", "an RFC 3339 date-time");
  // `Number(undefined)` is NaN, not undefined, so a destructuring default would
  // never fire for the `Z` form's absent offset groups. Default before mapping.
  const [year, month, day, hour, minute, second, offsetHour, offsetMinute] = match
    .slice(1)
    .map((group) => Number(group ?? 0));
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const monthDays = [
    31,
    leap ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];
  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > (monthDays[month - 1] ?? 0) ||
    hour > 23 ||
    minute > 59 ||
    second > 60 ||
    offsetHour > 23 ||
    offsetMinute > 59
  ) {
    throw invalidDecision("expires_at", "an RFC 3339 date-time");
  }
  return value;
}

function constraintsFrom(
  record: Record<string, unknown>,
): Record<string, unknown> {
  const constraints = decisionObject(record, "constraints");
  if (Object.keys(constraints).some((name) => name !== "tools")) {
    throw invalidDecision("constraints", "an object understood by this SDK");
  }
  if (
    "tools" in constraints &&
    (typeof constraints.tools !== "string" ||
      !TOOL_CONSTRAINTS.has(constraints.tools))
  ) {
    throw invalidDecision(
      "constraints.tools",
      "the string 'refuse' or 'strip'",
    );
  }
  return constraints;
}

function decisionFrom(record: Record<string, unknown>): ConsentDecision {
  if (record.decision !== "allow") throw invalidDecision("decision", "'allow'");
  const consentState = record.consent_state;
  if (
    consentState === null ||
    typeof consentState !== "object" ||
    Array.isArray(consentState)
  ) {
    throw invalidDecision("consent_state", "a JSON object");
  }
  const binding = decisionString(record, "binding");
  if (!BINDINGS.has(binding))
    throw invalidDecision("binding", "'ubt' or 'user_id'");
  if (record.mode !== "test" && record.mode !== "live")
    throw invalidDecision("mode", "'test' or 'live'");
  const source = decisionString(record, "source");
  const legalBasis = decisionString(record, "legal_basis");
  tokenId(record);
  expiresAt(record);

  return {
    consentState: consentState as Record<string, unknown>,
    paramsForced: decisionObject(record, "params_forced", true),
    actToken: actToken(record),
    source,
    legalBasis,
    binding,
    constraints: constraintsFrom(record),
    credentialPosture: credentialPosture(record.credential_posture),
  };
}

/**
 * Validate the routed posture from an authorize response.
 *
 * Null and absent mean the same thing — the client as constructed is fine.
 * Anything else that is not a plain, printable, reasonably short label is a
 * response the SDK cannot act on, and a decision it cannot act on is not a
 * decision it may ignore: ignoring it would send the call on the credential
 * routing existed to move it off.
 */
function credentialPosture(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  if (
    typeof value !== "string" ||
    value.trim() === "" ||
    value.length > MAX_POSTURE_LENGTH ||
    !isPrintable(value)
  ) {
    throw new ConsentUnreachableError(
      "ID Side cloud named a credential posture the SDK cannot use (credential_posture must be a short, " +
        "printable string); the AI call was not made.",
    );
  }
  return value;
}

async function problemBody(
  response: Response,
): Promise<Record<string, unknown>> {
  try {
    const data = await response.json();
    return data !== null && typeof data === "object" && !Array.isArray(data)
      ? (data as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

/**
 * Append the problem+json `title`/`detail` to `message` if present.
 *
 * The cloud writes its `detail` as a sentence, so it usually brings its own
 * full stop: adding another printed `…expired)..`.
 */
function withProblemDetails(
  message: string,
  problem: Record<string, unknown>,
): string {
  const parts = ["title", "detail"]
    .map((key) => problem[key])
    .filter(
      (value): value is string => typeof value === "string" && value.length > 0,
    );
  if (!parts.length) return message;
  const said = parts.join("; ");
  const end = /[.!?]$/.test(said) ? "" : ".";
  return `${message} Cloud says: ${said}${end}`;
}

/**
 * Map an error response to the typed exception hierarchy. A configuration
 * problem (401/402/422/429) must be distinguishable from an outage; everything
 * genuinely undetermined stays fail-closed. No AI call goes out in any case.
 */
function raiseForStatus(
  status: number,
  problem: Record<string, unknown>,
  originHint: string,
): never {
  if (status === 401) {
    const binding = BINDING_ERRORS.get(problemTypeName(problem.type));
    if (binding) {
      const [BindingClass, message] = binding;
      throw new BindingClass(withProblemDetails(message, problem));
    }
    throw new AuthenticationError(
      withProblemDetails(
        "ID Side rejected the API credentials (HTTP 401). Check the apiKey passed to consent.wrap(); the AI call was not made.",
        problem,
      ),
    );
  }
  if (status === 402) {
    throw new QuotaExceededError(
      withProblemDetails(
        "ID Side plan quota exceeded (HTTP 402). Upgrade the plan or wait for the window to reset; the AI call was not made.",
        problem,
      ),
    );
  }
  if (status === 403) {
    const type = problem.type;
    const reason = denyReason(type);
    throw new ConsentDeniedError(
      withProblemDetails(
        "The requested AI usage was denied (HTTP 403); the AI call was not made.",
        problem,
      ),
      { reason },
    );
  }
  if (
    status === 409 &&
    problemTypeName(problem.type) === "consent_refresh_required"
  ) {
    // Not an outage: the visitor's binding still verifies, and what it covers
    // is out of date. Any other 409 stays undetermined, below.
    throw new ConsentRefreshRequiredError(
      withProblemDetails(
        "This visitor's recorded choices no longer cover this application's AI use (HTTP 409): " +
          "the policy or the questions it requires changed since they answered. " +
          "Show them the widget again before retrying; the AI call was not made.",
        problem,
      ),
    );
  }
  if (status === 422) {
    throw new ContractError(
      withProblemDetails(
        "ID Side refused this call's payload (HTTP 422). If the cloud's detail names a value your code passed, " +
          "fix that value; otherwise the SDK and the cloud disagree on the contract — upgrade the SDK or " +
          "report it; the AI call was not made.",
        problem,
      ),
    );
  }
  if (status === 429) {
    throw new RateLimitedError(
      withProblemDetails(
        "ID Side rate limit hit (HTTP 429). Retry after a backoff; the AI call was not made.",
        problem,
      ),
    );
  }
  // 5xx or any undetermined 4xx: consent could not be verified → fail-closed.
  throw new ConsentUnreachableError(
    withHint(
      withProblemDetails(
        `Could not verify consent (HTTP ${status}); the AI call was not made.`,
        problem,
      ),
      originHint,
    ),
  );
}
