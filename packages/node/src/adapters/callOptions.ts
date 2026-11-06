// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * What an enforced call may carry besides its request: a closed list, per library.
 *
 * Enforcement works on the request body. It hashes it, forces parameters into
 * it, and attaches the ACT token to it. Every provider library also accepts a
 * second set of per-request options, and none of them is the body. Several undo
 * the enforcement without touching what was hashed:
 *
 * - `options.body` replaces the body outright;
 * - `fetchOptions`, `middleware` and `httpOptions.extraBody` rewrite the request
 *   on its way out;
 * - `path`, `defaultBaseURL`, `serverURL` and `httpOptions.baseUrl` send it to
 *   another server;
 * - an `Authorization` or `OpenAI-Project` header moves it onto another key.
 *
 * Each of these was proved, against the real libraries, to produce a signed
 * proof of a request that never went out.
 *
 * So this module lists every option each library declares, and says what it
 * does. The list is closed: an option it does not name is refused, and so is an
 * option a newer library version adds, until someone reads what it does. That is
 * the difference from a list of known dangers, which is only ever as long as
 * the last audit. `test/requestOptions.assert.ts` compares each list with the
 * library's own type, so a new option fails `npm run typecheck` until it is
 * classified here.
 *
 * The body is not closed. Its fields are what the model is asked, they are
 * hashed and proved as written, and whatever is added to them is a request
 * parameter the proof covers.
 */

import { isReservedHeader, type ProviderDescriptor } from "../descriptors.ts";
import { asMapping, isPlainObject } from "./base.ts";
import {
  CredentialRoutingError,
  UnenforceableOptionError,
  UnsupportedProviderError,
} from "../exceptions.ts";

/**
 * What one per-request option does to an enforced call.
 *
 * - `harmless`: how long, how often or whether the call is attempted. Never
 *   what is sent, where, or on which credential.
 * - `headers`: per-request headers. Allowed, except the names a descriptor
 *   reserves (`isReservedHeader`).
 * - `refused`: changes what is sent, where it goes, how it is built, or how the
 *   response is read.
 */
export type OptionEffect = "harmless" | "headers" | "refused";

/** `openai` (OpenAI, DeepSeek, Azure): the `RequestOptions` of every call site. */
export const OPENAI_REQUEST_OPTIONS = {
  headers: "headers",
  idempotencyKey: "harmless",
  maxRetries: "harmless",
  signal: "harmless",
  timeout: "harmless",
  body: "refused",
  defaultBaseURL: "refused",
  fetchOptions: "refused",
  method: "refused",
  path: "refused",
  query: "refused",
  stream: "refused",
  __binaryResponse: "refused",
  __metadata: "refused",
  __security: "refused",
  __streamClass: "refused",
  __synthesizeEventData: "refused",
} as const satisfies Readonly<Record<string, OptionEffect>>;

/** `@anthropic-ai/sdk`: the same Stainless shape, plus request middleware. */
export const ANTHROPIC_REQUEST_OPTIONS = {
  headers: "headers",
  idempotencyKey: "harmless",
  maxRetries: "harmless",
  signal: "harmless",
  timeout: "harmless",
  body: "refused",
  defaultBaseURL: "refused",
  // A fallback chain resends the request with another model when one refuses.
  fallbackState: "refused",
  fetchOptions: "refused",
  method: "refused",
  middleware: "refused",
  path: "refused",
  query: "refused",
  stream: "refused",
  __binaryResponse: "refused",
  __streamClass: "refused",
} as const satisfies Readonly<Record<string, OptionEffect>>;

/**
 * `@mistralai/mistralai`: Speakeasy flattens `RequestInit` into the options and
 * spreads all of them into the `Request` it builds. So every fetch setting is
 * an option here, and none of them belongs on an enforced call: `dispatcher`
 * picks the connection and `redirect` picks what happens after it. Keys the
 * DOM's `RequestInit` has and Node's does not (`cache`, `priority`) are not
 * listed, so they are refused as unknown.
 */
export const MISTRAL_REQUEST_OPTIONS = {
  headers: "headers",
  retries: "harmless",
  retryCodes: "harmless",
  signal: "harmless",
  timeoutMs: "harmless",
  credentials: "refused",
  dispatcher: "refused",
  duplex: "refused",
  fetchOptions: "refused",
  integrity: "refused",
  keepalive: "refused",
  mode: "refused",
  redirect: "refused",
  referrer: "refused",
  referrerPolicy: "refused",
  serverURL: "refused",
  window: "refused",
} as const satisfies Readonly<Record<string, OptionEffect>>;

/** A library this module does not know: the generic adapter's own convention, and nothing else. */
const GENERIC_REQUEST_OPTIONS = {
  headers: "headers",
} as const satisfies Readonly<Record<string, OptionEffect>>;

/**
 * `@google/genai` takes one object, so its signature IS its top-level keys.
 * The library drops any other key without sending it, so nothing legitimate is
 * lost by refusing one.
 */
export const GEMINI_PARAMETERS = {
  config: "config",
  contents: "request",
  model: "request",
} as const satisfies Readonly<Record<string, "config" | "request">>;

/** `config.httpOptions`: the transport options of a Gemini call, closed like any other. */
export const GEMINI_HTTP_OPTIONS = {
  headers: "headers",
  retryOptions: "harmless",
  timeout: "harmless",
  apiVersion: "refused",
  baseUrl: "refused",
  baseUrlResourceScope: "refused",
  extraBody: "refused",
} as const satisfies Readonly<Record<string, OptionEffect>>;

/**
 * Every field of `GenerateContentConfig`, and what the library does with it.
 *
 * - `request`: converted into the request body. Hashed, sent and proved.
 * - `local`: read in-process and never sent. `automaticFunctionCalling` only
 *   steers a loop that the executable-tool refusal below never lets run. It
 *   stays in the hash, where it has always been.
 * - `unhashed`: read in-process, and not data. An `AbortSignal` cannot be
 *   canonicalized, and it used to make the hash throw, so leaving it out moves
 *   no existing hash.
 * - `transport`: `httpOptions`, checked against `GEMINI_HTTP_OPTIONS`.
 *
 * At run time a field this list does not name is treated as `request`, because
 * the config is the body, and a newer library's generation parameter is a
 * request parameter. Transport options have their own closed map. The type
 * assertion still makes every new field a decision.
 */
export const GEMINI_CONFIG_FIELDS = {
  abortSignal: "unhashed",
  automaticFunctionCalling: "local",
  httpOptions: "transport",
  audioTimestamp: "request",
  audioTranscriptionConfig: "request",
  cachedContent: "request",
  candidateCount: "request",
  enableEnhancedCivicAnswers: "request",
  frequencyPenalty: "request",
  imageConfig: "request",
  labels: "request",
  logprobs: "request",
  maxOutputTokens: "request",
  mediaResolution: "request",
  modelArmorConfig: "request",
  modelSelectionConfig: "request",
  presencePenalty: "request",
  responseJsonSchema: "request",
  responseLogprobs: "request",
  responseMimeType: "request",
  responseModalities: "request",
  responseSchema: "request",
  routingConfig: "request",
  safetySettings: "request",
  seed: "request",
  serviceTier: "request",
  speechConfig: "request",
  stopSequences: "request",
  systemInstruction: "request",
  temperature: "request",
  thinkingConfig: "request",
  toolConfig: "request",
  tools: "request",
  topK: "request",
  topP: "request",
} as const satisfies Readonly<Record<string, "request" | "local" | "unhashed" | "transport">>;

/**
 * What one option passed to `withOptions()` / `copy()` does to the derived client.
 *
 * A derived client is built in one line from the wrapped one, and it is
 * enforced with the parent's adapter, credential labels and server. So a
 * derivation is closed like a call:
 *
 * - `harmless`: how long, how often, what is logged.
 * - `origin`: `baseURL`. Allowed, and the derived client must then point at
 *   the parent's server (checked on the constructed client).
 * - `headers`: `defaultHeaders`. Allowed, except the reserved names.
 * - `credential`: which key, account or posture the calls go out on. Refused
 *   (`CredentialRoutingError`): the labels the cloud routes between are the
 *   parent's, and credentials are declared to `wrap()`, not swapped under them.
 * - `refused`: how requests are built or where they go (`fetch`,
 *   `fetchOptions`, `middleware`, `defaultQuery`, `provider`). Refused
 *   (`UnsupportedProviderError`): that is a new client, to be wrapped as one.
 */
export type DerivationEffect = "harmless" | "origin" | "headers" | "credential" | "refused";

/**
 * `openai`: the options `withOptions()` accepts, `AzureOpenAI`'s included.
 *
 * `AzureOpenAI` inherits `withOptions()` and rebuilds itself from the same
 * spread, so its own options reach its constructor through it. They are
 * classified here for every client of the module: on a plain `OpenAI` client
 * they mean nothing, and are refused all the same.
 */
export const OPENAI_DERIVATION_OPTIONS = {
  dangerouslyAllowBrowser: "harmless",
  logLevel: "harmless",
  logger: "harmless",
  maxRetries: "harmless",
  timeout: "harmless",
  webhookSecret: "harmless",
  baseURL: "origin",
  defaultHeaders: "headers",
  adminAPIKey: "credential",
  apiKey: "credential",
  azureADTokenProvider: "credential",
  organization: "credential",
  project: "credential",
  workloadIdentity: "credential",
  // Azure: `endpoint` names the host; the API version and the deployment
  // choose what the request addresses.
  apiVersion: "refused",
  defaultQuery: "refused",
  deployment: "refused",
  endpoint: "refused",
  fetch: "refused",
  fetchOptions: "refused",
  provider: "refused",
} as const satisfies Readonly<Record<string, DerivationEffect>>;

/** `@anthropic-ai/sdk`: the `ClientOptions` `withOptions()` accepts. */
export const ANTHROPIC_DERIVATION_OPTIONS = {
  dangerouslyAllowBrowser: "harmless",
  logLevel: "harmless",
  logger: "harmless",
  maxRetries: "harmless",
  timeout: "harmless",
  webhookKey: "harmless",
  baseURL: "origin",
  defaultHeaders: "headers",
  apiKey: "credential",
  authToken: "credential",
  config: "credential",
  credentials: "credential",
  profile: "credential",
  defaultQuery: "refused",
  fetch: "refused",
  fetchOptions: "refused",
  middleware: "refused",
} as const satisfies Readonly<Record<string, DerivationEffect>>;

const DERIVATION_OPTIONS: ReadonlyMap<string, Readonly<Record<string, DerivationEffect>>> = new Map<
  string,
  Readonly<Record<string, DerivationEffect>>
>([
  ["openai", OPENAI_DERIVATION_OPTIONS],
  ["anthropic", ANTHROPIC_DERIVATION_OPTIONS],
]);

/**
 * A library whose derivation this module does not know: every name either known
 * one classifies, with Anthropic's word where both do (today they never
 * disagree). Anything neither names is refused, as everywhere else.
 */
const GENERIC_DERIVATION_OPTIONS: Readonly<Record<string, DerivationEffect>> = {
  ...OPENAI_DERIVATION_OPTIONS,
  ...ANTHROPIC_DERIVATION_OPTIONS,
};

/**
 * Refuse a derivation that would change what the wrapped client's enforcement
 * describes. Throws before the derived client is constructed.
 *
 * The options are read the way the libraries read them: they spread the
 * argument, so its own enumerable keys are the options, whatever object holds
 * them. For the same reason a `defaultHeaders` given here replaces the parent's
 * whole container, so it is refused when that would drop a reserved header the
 * parent was built with: removing `OpenAI-Project` changes the credential as
 * surely as setting it.
 */
export function assertDerivableOptions(descriptor: ProviderDescriptor, derivation: unknown, parent: object): void {
  if (derivation === undefined || derivation === null) return;
  const options = Object(derivation) as Record<string, unknown>;
  const effects = DERIVATION_OPTIONS.get(descriptor.clientModule) ?? GENERIC_DERIVATION_OPTIONS;
  for (const key of Object.keys(options)) {
    const effect = Object.hasOwn(effects, key) ? effects[key] : undefined;
    if (effect === "harmless" || effect === "origin") continue;
    if (effect === "credential") throw derivedCredential(descriptor, key);
    if (effect !== "headers") throw derivedTransport(descriptor, key);
    const names = readHeaderNames(options[key]);
    if (names === undefined) throw unreadableDerivedHeaders(descriptor);
    const reserved = names.find((name) => isReservedHeader(descriptor, name.trim()));
    if (reserved !== undefined) throw reservedDerivedHeader(descriptor, reserved);
    const inherited = readHeaderNames((parent as { _options?: { defaultHeaders?: unknown } })._options?.defaultHeaders);
    if (inherited === undefined) throw unreadableDerivedHeaders(descriptor);
    const dropped = inherited.find((name) => isReservedHeader(descriptor, name.trim()));
    if (dropped !== undefined) throw droppedDerivedHeader(descriptor, dropped);
  }
}

/**
 * Keys of a client-level `fetchOptions` that rewrite every request.
 *
 * openai and anthropic spread the client's `fetchOptions` over each request
 * after building it, so a `body` there replaces the body that was hashed and
 * forced, a `method` changes the call, and a `headers` replaces every header
 * the request was built with — the ACT token and the key included, whatever
 * names it holds. The other `RequestInit` keys (`dispatcher`, `keepalive`…)
 * steer the connection, which proxies need, and touch no content.
 */
const REWRITING_FETCH_OPTIONS: readonly string[] = ["body", "headers", "method"];

/**
 * Refuse a client whose own construction options rewrite what enforcement
 * produces. Throws before anything is patched.
 *
 * These are declarative options: data the SDK can read, unlike a custom
 * `fetch`, which is code and stays outside what it can judge. A request built
 * under them would not be the request authorized and proved.
 */
export function assertEnforceableClient(descriptor: ProviderDescriptor, client: object): void {
  const fetchOptions = (client as { fetchOptions?: unknown }).fetchOptions;
  if (fetchOptions !== undefined && fetchOptions !== null) {
    const options = Object(fetchOptions) as Record<string, unknown>;
    // Named first: a header choosing the credential is a routing mistake, and
    // says so, before it is refused below as a rewrite like any other header.
    if (Object.hasOwn(options, "headers")) assertClientHeaders(descriptor, options.headers, "fetchOptions.headers");
    for (const key of Object.keys(options)) {
      if (REWRITING_FETCH_OPTIONS.includes(key)) throw rewritingClient(descriptor, `fetchOptions.${key}`);
    }
  }

  // @google/genai merges a client-level extraBody into every request body. An
  // empty one adds nothing, and the library skips it.
  const httpOptions = (client as { apiClient?: { clientOptions?: { httpOptions?: { extraBody?: unknown; headers?: unknown } } } })
    .apiClient?.clientOptions?.httpOptions;
  const extraBody = httpOptions?.extraBody;
  if (extraBody !== undefined && extraBody !== null && !(isPlainObject(extraBody) && Object.keys(extraBody).length === 0)) {
    throw rewritingClient(descriptor, "httpOptions.extraBody");
  }
  // Its client-level headers are merged with each call's by exact spelling and
  // then appended: an `x-ai-consent-token` there doubles the token the SDK sets,
  // and an `x-goog-api-key` displaces the key. Refused as Python refuses them.
  assertClientHeaders(descriptor, httpOptions?.headers, "httpOptions.headers");
}

/** Refuse construction-time headers that choose the credential, or that cannot be read. */
function assertClientHeaders(descriptor: ProviderDescriptor, headers: unknown, where: string): void {
  const names = readHeaderNames(headers);
  if (names === undefined) throw unreadableClientHeaders(descriptor, where);
  const reserved = names.find((name) => isReservedHeader(descriptor, name.trim()));
  if (reserved !== undefined) throw reservedClientHeader(descriptor, reserved, where);
}

/** The request options each known client library accepts, by descriptor `client_module`. */
const REQUEST_OPTIONS: ReadonlyMap<string, Readonly<Record<string, OptionEffect>>> = new Map<
  string,
  Readonly<Record<string, OptionEffect>>
>([
  ["openai", OPENAI_REQUEST_OPTIONS],
  ["anthropic", ANTHROPIC_REQUEST_OPTIONS],
  ["mistralai", MISTRAL_REQUEST_OPTIONS],
]);

/**
 * Refuse a call whose options could undo its enforcement. Throws before anything happens.
 *
 * `params` is the request with the identity already consumed. `rest` holds the
 * arguments after it, as the caller passed them.
 */
export function assertEnforceableOptions(
  descriptor: ProviderDescriptor,
  params: Record<string, unknown>,
  rest: readonly unknown[],
): void {
  if (descriptor.clientModule === "google.genai") {
    assertGeminiOptions(descriptor, params);
    return;
  }
  assertRequestOptions(descriptor, rest[0], REQUEST_OPTIONS.get(descriptor.clientModule) ?? GENERIC_REQUEST_OPTIONS);
}

/**
 * The `(body, options)` libraries.
 *
 * Only a plain object is read, because only a plain object gets through: the
 * adapter replaces anything else with a fresh object that carries the token and
 * nothing more.
 */
function assertRequestOptions(
  descriptor: ProviderDescriptor,
  options: unknown,
  effects: Readonly<Record<string, OptionEffect>>,
): void {
  if (!isPlainObject(options)) return;
  for (const key of Object.keys(options)) {
    assertOption(descriptor, effects, key, options[key], key);
  }
}

function assertGeminiOptions(descriptor: ProviderDescriptor, params: Record<string, unknown>): void {
  for (const key of Object.keys(params)) {
    if (!Object.hasOwn(GEMINI_PARAMETERS, key)) throw refusedOption(key);
  }

  // The same view the Gemini adapter forwards: a typed config and typed
  // httpOptions reach the library as their `toJSON()`.
  const config = asMapping(params.config);
  if (config === undefined) return;

  const httpOptions = asMapping(config.httpOptions);
  if (httpOptions !== undefined) {
    for (const key of Object.keys(httpOptions)) {
      assertOption(descriptor, GEMINI_HTTP_OPTIONS, key, httpOptions[key], `config.httpOptions.${key}`);
    }
  }

  // Refused whatever the user answered about tools. This is about the token:
  // one executable tool makes the library call the model again, by itself, up
  // to ten times under this one authorization.
  if (Array.isArray(config.tools)) {
    config.tools.forEach((tool: unknown, index: number) => {
      if (isExecutableTool(tool)) throw executableTool(index);
    });
  }
}

function assertOption(
  descriptor: ProviderDescriptor,
  effects: Readonly<Record<string, OptionEffect>>,
  key: string,
  value: unknown,
  where: string,
): void {
  const effect = Object.hasOwn(effects, key) ? effects[key] : undefined;
  if (effect === "harmless") return;
  if (effect !== "headers") throw refusedOption(where);
  for (const name of headerNames(value, where)) {
    if (isReservedHeader(descriptor, name.trim())) throw reservedHeader(name, where);
  }
}

/**
 * The header names a caller passed, in any shape a library accepts.
 *
 * Only names are read. A name is enough to refuse, and a `null` value counts:
 * removing `Authorization` changes the key as surely as replacing it. A
 * container this cannot read is refused, because "could not tell" is not "not
 * reserved".
 */
function headerNames(headers: unknown, where: string): string[] {
  const names = readHeaderNames(headers);
  if (names === undefined) throw unreadableHeaders(where);
  return names;
}

/**
 * The names in a headers container: a plain object, a `Headers` instance or a
 * list of `[name, value]` pairs. `undefined` when it is none of those.
 */
function readHeaderNames(headers: unknown): string[] | undefined {
  if (headers === undefined || headers === null) return [];
  if (headers instanceof Headers) return [...headers.keys()];
  if (Array.isArray(headers)) {
    const names: string[] = [];
    for (const entry of headers as unknown[]) {
      if (!Array.isArray(entry) || typeof entry[0] !== "string") return undefined;
      names.push(entry[0]);
    }
    return names;
  }
  if (isPlainObject(headers)) return Object.keys(headers);
  return undefined;
}

/** Mirrors @google/genai's own test for a tool it will run: a `callTool` function. */
function isExecutableTool(tool: unknown): boolean {
  return (
    tool !== null &&
    (typeof tool === "object" || typeof tool === "function") &&
    "callTool" in tool &&
    typeof (tool as { callTool?: unknown }).callTool === "function"
  );
}

// ── what the developer reads ─────────────────────────────────────────────────
//
// Each message names the option or the header and never its value, because a
// header value is often a key.

const NOT_MADE = "The AI call was not made, and ID Side was not asked to authorize it.";

function refusedOption(where: string): UnenforceableOptionError {
  return new UnenforceableOptionError(
    `\`${where}\` cannot be passed on a consent-enforced call. ${NOT_MADE} The SDK proves the request it ` +
      "authorized, and this option can change what is actually sent, where it goes or how it is built after " +
      "that. Put request parameters in the request itself and configure the client when you construct it. An " +
      "enforced call accepts timeouts, retries, cancellation, idempotency keys and headers that do not choose " +
      "a credential.",
  );
}

function reservedHeader(name: string, where: string): UnenforceableOptionError {
  return new UnenforceableOptionError(
    `The \`${name}\` header cannot be set on a consent-enforced call (in \`${where}\`). ${NOT_MADE} It chooses ` +
      "the key, account or project the call goes out on, or it carries the ACT token itself. A per-call value " +
      "would send the request on something the proof does not record. Configure credentials on the client, or " +
      "let consent.wrap({ credentials }) route between them.",
  );
}

function unreadableHeaders(where: string): UnenforceableOptionError {
  return new UnenforceableOptionError(
    `\`${where}\` is in a form the SDK cannot read (a plain object, a Headers instance or a list of ` +
      `[name, value] pairs), so it cannot tell whether it sets a reserved header. ${NOT_MADE}`,
  );
}

function rewritingClient(descriptor: ProviderDescriptor, where: string): UnsupportedProviderError {
  return new UnsupportedProviderError(
    `The '${descriptor.name}' client was constructed with \`${where}\`, which the library applies to every request ` +
      "after it is built: the request that goes out would not be the one authorized and proved. The client was not " +
      "wrapped. Construct it without that option. Transport settings such as a proxy dispatcher remain accepted.",
  );
}

function reservedClientHeader(descriptor: ProviderDescriptor, name: string, where: string): CredentialRoutingError {
  return new CredentialRoutingError(
    `The '${descriptor.name}' client was constructed with a \`${name}\` header in \`${where}\`, so it ` +
      "was not wrapped. That header chooses the key, account or project calls go out on, and there it would override " +
      "what the client and the SDK set on every request. Configure the credential through the client's own options, " +
      "or declare every credential you hold to consent.wrap(client, { credentials }).",
  );
}

function unreadableClientHeaders(descriptor: ProviderDescriptor, where: string): CredentialRoutingError {
  return new CredentialRoutingError(
    `The '${descriptor.name}' client was constructed with \`${where}\` in a form the SDK cannot read, ` +
      "so it cannot tell whether they choose a credential. The client was not wrapped.",
  );
}

function derivedCredential(descriptor: ProviderDescriptor, key: string): CredentialRoutingError {
  return new CredentialRoutingError(
    `\`${key}\` cannot be changed when deriving a client from a wrapped '${descriptor.name}' client, so the ` +
      "client was not derived. It chooses the key, account or posture calls go out on, and a derived client keeps " +
      "its parent's credential labels: the cloud would route a posture onto a credential nobody labelled. Declare " +
      "every credential you hold to consent.wrap(client, { credentials }) and let the SDK route between them.",
  );
}

function derivedTransport(descriptor: ProviderDescriptor, key: string): UnsupportedProviderError {
  return new UnsupportedProviderError(
    `\`${key}\` cannot be passed when deriving a client from a wrapped '${descriptor.name}' client, so the ` +
      "client was not derived. It changes how requests are built or where they go, and the wrapped client's " +
      "enforcement was set up without it. Construct a new client with that option and pass it to consent.wrap(): " +
      "its provider is detected again and its server bound anew.",
  );
}

function reservedDerivedHeader(descriptor: ProviderDescriptor, name: string): CredentialRoutingError {
  return new CredentialRoutingError(
    `A client derived from a wrapped '${descriptor.name}' client cannot set the \`${name}\` default header, so ` +
      "the client was not derived. It chooses the key, account or project calls go out on, or carries the ACT " +
      "token, while the derived client keeps its parent's credential labels. Declare every credential you hold " +
      "to consent.wrap(client, { credentials }) and let the SDK route between them.",
  );
}

function droppedDerivedHeader(descriptor: ProviderDescriptor, name: string): CredentialRoutingError {
  return new CredentialRoutingError(
    `This derivation of a wrapped '${descriptor.name}' client replaces its default headers, and would drop the ` +
      `\`${name}\` header the client was built with, so the client was not derived. That header chooses the key, ` +
      "account or project calls go out on. Leave `defaultHeaders` out of the derivation, or declare every credential " +
      "you hold to consent.wrap(client, { credentials }) and let the SDK route between them.",
  );
}

function unreadableDerivedHeaders(descriptor: ProviderDescriptor): CredentialRoutingError {
  return new CredentialRoutingError(
    `The default headers given to this derivation of a wrapped '${descriptor.name}' client are in a form the SDK ` +
      "cannot read, so it cannot tell whether they choose a credential. The client was not derived.",
  );
}

function executableTool(index: number): UnenforceableOptionError {
  return new UnenforceableOptionError(
    `\`config.tools[${index}]\` is an executable tool: it has \`callTool\`, as \`mcpToTool()\` does. With it, ` +
      "@google/genai calls the model again by itself, up to ten requests under one authorization, and an ACT " +
      `token covers exactly one request. ${NOT_MADE} Declare the function instead, and run the loop yourself: ` +
      "each turn is then authorized and proved.",
  );
}
