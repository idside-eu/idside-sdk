// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * The transport net: the last thing between an unenforced call and the wire.
 *
 * `consent.wrap()` patches the endpoints a descriptor declares. That list is a
 * snapshot of one provider's API on one day, so it is structurally incomplete: a
 * helper posts straight to the transport, a resource is added next release, a
 * derived client is built from another one. Every gap used to be silent — the
 * call reached the provider with no authorize, no ACT token, and no trace that
 * it happened.
 *
 * So the SDK also watches the client's own HTTP transport and refuses to carry a
 * body-bearing request unless its ACT token matches private authorization
 * provenance for this wrapped client and method/path. It is a **detector, not an
 * enforcer**: it never authorizes anything, it only stops what enforcement did
 * not touch.
 *
 * And it is deliberately narrow. It reads the HTTP method, the URL's path and
 * origin, and one named header, comparing them locally with private provenance:
 * the token must be the one this client was just authorized, for this route,
 * going to the server the client was wrapped with. Never the body, query string
 * or any other header. It hashes nothing, logs nothing and sends nothing
 * anywhere. The one thing it changes is on a request it let through as
 * authorized: that request is sent with `redirect: "manual"`, because a
 * followed redirect is a second request the net never sees.
 *
 * Mirrors packages/python/src/idside/_guard.py in intent; the mechanism differs
 * because the transport does (a `fetch` function here, an `httpx.Client` there).
 */

import { requestOrigin, requestPath } from "./destination.ts";
import { UnenforcedCallError } from "./exceptions.ts";

/** Private, unforgeable marker so a second wrap() never stacks two nets. */
const GUARDED: unique symbol = Symbol("idside.guard.installed");

/**
 * Methods let through unconditionally — this is where the line is drawn.
 *
 * They carry no prompt to infer on, and they are the management surface every
 * application legitimately needs from the same client: `models.list`,
 * `files.retrieve`, `files.delete`, `batches.retrieve`, polling a stored
 * response. `/v1/authorize` could not decide them anyway: it requires a `model`
 * these requests do not have. A body-bearing method is an inference call until
 * proven otherwise.
 */
const BODYLESS_METHODS: ReadonlySet<string> = new Set([
  "GET",
  "HEAD",
  "OPTIONS",
  "DELETE",
]);

/**
 * Property names a provider client keeps its fetch function under.
 *
 * `fetch` covers openai and anthropic (`this.fetch` on the client itself).
 * `fetcher` covers the Mistral TS SDK, which wraps its own `HTTPClient` whose
 * public `fetcher` field holds the function and is called as `fetcher(request)`
 * — the single-`Request` shape this guard already understands.
 */
const TRANSPORT_KEYS: ReadonlySet<string> = new Set(["fetch", "fetcher"]);

/**
 * The transport sits on the client itself, or two hops in via its options —
 * Mistral's is at `client._options.httpClient.fetcher`, which is exactly this
 * depth. Not raised further: every hop is a chance to patch something that only
 * looks like a transport.
 */
const MAX_DEPTH = 2;
/** A cap, so an unexpected client shape cannot turn `wrap()` into a graph walk. */
const MAX_NODES = 256;

type FetchLike = (this: unknown, ...args: unknown[]) => unknown;
type TokenHasProvenance = (
  token: unknown,
  method: string,
  path: string,
  origin: string,
) => boolean;

/**
 * One wrapped client's check on a transport, held no longer than that client.
 *
 * A derived client shares its parent's transport and adds a check to it.
 * `scope` is a weak reference to the client's private scope, which only its
 * patched methods keep alive, so when the client is collected its check is
 * pruned: at the next install on the transport, and at the next request it
 * sees. Without it, `withOptions({ timeout })` per request grew the list, and
 * the time every request spends here, without bound. No scope: kept for good.
 */
interface Provenance {
  readonly scope: WeakRef<object> | undefined;
  readonly check: TokenHasProvenance;
}

interface FoundFetch {
  readonly owner: Record<string, unknown>;
  /** The property the transport lives under: `fetch`, Mistral's `fetcher`, or @google/genai's `apiCall`. */
  readonly key: string;
  readonly fn: FetchLike;
  /**
   * One sentence added to a refusal on this transport, when the generic advice
   * would send a developer looking in the wrong place. Set for the Gemini
   * NextGen surfaces, whose routes no descriptor declares at all.
   */
  readonly hint?: string;
}

/** What one walk found, and whether it got to finish before its budget ran out. */
interface Scan {
  /**
   * EVERY transport the walk reached, never just the first.
   *
   * A client can own two routes out — its own `fetch` and an `HTTPClient` with
   * its own `fetcher`, the shape `google-genai` has in the Python SDK — and
   * stopping at the first left the other carrying whatever was sent through it,
   * under a `net=true` that said the client was covered.
   */
  readonly found: readonly FoundFetch[];
  readonly complete: boolean;
}

/**
 * What `installTransportGuard` answers, and what the journal's `net=` repeats.
 * `true`/`false` keep the meaning they always had; `"partial"` is the case that
 * used to be told as one of them and is neither.
 */
export type NetState = boolean | "partial";

/**
 * Install the net on `client`'s HTTP transport. Idempotent.
 *
 * Three answers, because three things are true and only two were ever said.
 * This value goes into the journal as `net=`, and that field is read as "is this
 * client covered?":
 *
 * - `true` — the transport was found and is guarded (or already was).
 * - `false` — there was nothing to guard, and the search finished saying so. A
 *   client that owns no discoverable `fetch` — every fake in the test suite —
 *   cannot leak a request through a transport it does not have.
 * - `"partial"` — a route out of this client is, or may be, unguarded: the walk
 *   ran out of node budget before finishing, or a transport would not take the
 *   patch. Changes from this attempt are rolled back; `wrap()` treats the
 *   result as unsupported and refuses the client.
 *
 * `undo`, when given, receives the inverse of an install that succeeded. A
 * `wrap()` guards more than one client — the wrapped one and every client named
 * in its `credentials` — and one it refuses after the first install must not
 * leave that client's transport refusing requests nobody will ever authorize.
 */
export function installTransportGuard(
  client: object,
  tokenHeader: string,
  provider?: string,
  tokenHasProvenance: TokenHasProvenance = () => false,
  scope?: object,
  undo?: Array<() => void>,
): NetState {
  const fetchScan = findFetches(client);
  const geminiScan =
    provider === "gemini"
      ? findGeminiRequests(client)
      : { found: [], complete: true };
  const scan: Scan = {
    found: [...fetchScan.found, ...geminiScan.found],
    complete: fetchScan.complete && geminiScan.complete,
  };
  // Discovery is a preflight. A bounded scan that did not finish must not
  // mutate whichever routes it happened to encounter before failing wrap().
  if (!scan.complete) return "partial";
  // An array, deliberately materialised and not a short-circuit inside
  // `every()`: every transport has to be OFFERED the patch, and giving up at
  // the first refusal would skip checking the ones after it. Each mutation has
  // an inverse so a partial result restores only this install attempt's work.
  const guarded: boolean[] = [];
  const rollbacks: Array<() => void> = [];
  const entry: Provenance = {
    scope: scope === undefined ? undefined : new WeakRef(scope),
    check: tokenHasProvenance,
  };
  // The Gemini NextGen surfaces own transports that do not exist yet: each
  // builds its own on demand. The net cannot be put on what has not been made,
  // so it is put on what makes it. Done FIRST, because one of those transports
  // can also be sitting where the walk below reaches it — a client whose
  // `getNextGenClient()` was called before `wrap()` — and whichever install
  // gets there first is the one whose refusal the developer reads.
  const nextGen =
    provider === "gemini" ? guardNextGen(client, tokenHeader, entry, rollbacks) : true;
  for (const found of scan.found) {
    guarded.push(guardUndoably(found, tokenHeader, entry, rollbacks));
  }
  if (!nextGen || !guarded.every(Boolean)) {
    for (const rollback of rollbacks.reverse()) rollback();
    return "partial";
  }
  // In install order: the caller undoes everything it was handed last first.
  undo?.push(...rollbacks);
  return guarded.length > 0;
}

/** `guardOne`, plus the inverse of whatever it changed. */
function guardUndoably(
  found: FoundFetch,
  tokenHeader: string,
  entry: Provenance,
  rollbacks: Array<() => void>,
): boolean {
  const current = currentTransport(found);
  const existing = provenancesOf(current.fn);
  const previous = Object.getOwnPropertyDescriptor(current.owner, current.key);
  // Already carrying this exact check: two installs reached the same transport
  // by two routes, and recording it twice would run it twice on every request
  // and leave the rollback undoing half of something it did not do.
  if (existing?.includes(entry) === true) return true;

  const guarded = guardOne(current, tokenHeader, entry);
  if (existing !== undefined) {
    rollbacks.push(() => {
      const index = existing.lastIndexOf(entry);
      if (index >= 0) existing.splice(index, 1);
    });
  } else {
    rollbacks.push(() => restoreProperty(current.owner, current.key, previous));
  }
  return guarded;
}

/**
 * The same transport, read as it is NOW rather than as the scan saw it.
 *
 * A scan is a preflight, and between it and the patch another install can have
 * put the net there already — the NextGen surfaces and the walk reach some of
 * the same objects. Wrapping the stale function would bury the first net under
 * a second one that calls it: two nets, one of them never consulted.
 */
function currentTransport(found: FoundFetch): FoundFetch {
  const current = found.owner[found.key];
  return typeof current === "function" && current !== found.fn
    ? { ...found, fn: current as FetchLike }
    : found;
}

function restoreProperty(
  owner: object,
  key: PropertyKey,
  previous: PropertyDescriptor | undefined,
): void {
  try {
    if (previous === undefined) delete (owner as Record<PropertyKey, unknown>)[key];
    else Object.defineProperty(owner, key, previous);
  } catch {
    // A hostile setter/proxy can make perfect rollback impossible. The install
    // still reports partial and never claims coverage.
  }
}

/**
 * Patch one transport, and say whether it ended up patched.
 *
 * A transport that will not take the patch — a frozen client, an accessor-backed
 * property, a Proxy that drops the write — is reported to `wrap()`. The caller
 * installs the net before patching call sites, so refusal leaves them untouched.
 */
function guardOne(
  found: FoundFetch,
  tokenHeader: string,
  entry: Provenance,
): boolean {
  const existing = provenancesOf(found.fn);
  if (existing !== undefined) {
    liveChecks(existing);
    if (!existing.includes(entry)) existing.push(entry);
    return true;
  }

  const original = found.fn;
  const provenances = [entry];
  const hint = found.hint;
  const guarded = function (this: unknown, ...args: unknown[]): unknown {
    const authorized = assertEnforced(args, tokenHeader, liveChecks(provenances), hint);
    return original.apply(this, authorized ? withoutRedirects(args) : args);
  };
  Object.defineProperty(guarded, GUARDED, {
    value: provenances,
    enumerable: false,
  });

  try {
    found.owner[found.key] = guarded;
  } catch {
    return false;
  }
  // Read back rather than trusted: a setter or a Proxy that swallows the write
  // throws nothing, and the difference between "patched" and "silently ignored"
  // is the whole difference between `net=true` and `net=partial`.
  return isGuarded(found.owner[found.key]);
}

/** True if this SDK has already guarded `fn`. */
function isGuarded(fn: unknown): boolean {
  return provenancesOf(fn) !== undefined;
}

function provenancesOf(fn: unknown): Provenance[] | undefined {
  const value = (fn as Record<PropertyKey, unknown> | undefined)?.[GUARDED];
  return Array.isArray(value) ? (value as Provenance[]) : undefined;
}

/** Drop, in place, the checks of clients that were collected, and return the rest. */
function liveChecks(provenances: Provenance[]): TokenHasProvenance[] {
  let kept = 0;
  for (const entry of provenances) {
    if (entry.scope === undefined || entry.scope.deref() !== undefined) provenances[kept++] = entry;
  }
  provenances.length = kept;
  return provenances.map((entry) => entry.check);
}

/**
 * Refuse a request that never went through enforcement.
 *
 * Handles both call shapes: `fetch(url, init)` (openai, anthropic) and
 * `fetch(Request)`. When both a `Request` and an `init` are given, the token is
 * accepted from either: the question being asked is "did enforcement touch this
 * call", and a token anywhere on it answers yes.
 *
 * Returns whether the request is one enforcement authorized, as opposed to a
 * bodyless one let through untouched.
 */
function assertEnforced(
  args: unknown[],
  tokenHeader: string,
  tokenProvenances: TokenHasProvenance[],
  hint?: string,
): boolean {
  const [first, second] = args;
  const init = isObject(second)
    ? (second as { method?: unknown; headers?: unknown })
    : undefined;

  let rawUrl: unknown = first;
  let method: unknown = init?.method;

  if (isRequestLike(first)) {
    rawUrl = first.url;
    if (typeof method !== "string" || method === "") method = first.method;
  }

  // fetch() defaults to GET when init names no method.
  const verb = (
    typeof method === "string" && method !== "" ? method : "GET"
  ).toUpperCase();
  const path = requestPath(rawUrl);
  if (BODYLESS_METHODS.has(verb)) return false;
  const origin = requestOrigin(rawUrl);
  const carried =
    hasProvenancedHeader(
      init?.headers,
      tokenHeader,
      tokenProvenances,
      verb,
      path,
      origin,
    ) ||
    (isRequestLike(first) &&
      hasProvenancedHeader(
        first.headers,
        tokenHeader,
        tokenProvenances,
        verb,
        path,
        origin,
      ));
  refuseUnlessEnforced(verb, path, carried, hint);
  return true;
}

/**
 * The same authorized request, told not to follow a redirect.
 *
 * `fetch` follows a 307 or 308 by itself: it sends the body, the ACT token and
 * the credential headers again, to wherever the `Location` points, and this
 * net only ever sees the first request. With `redirect: "manual"` the 3xx comes
 * back as the response, and the provider library reports it as the error
 * status it is. An `init` wins over its `Request`, so the mode goes on the init
 * when there is one, and onto a copy of the `Request` when that is all there is.
 */
function withoutRedirects(args: unknown[]): unknown[] {
  const [first, second, ...rest] = args;
  if (isObject(second)) return [first, { ...second, redirect: "manual" }, ...rest];
  if (first instanceof Request) {
    return [new Request(first, { redirect: "manual" }), ...args.slice(1)];
  }
  return [first, { redirect: "manual" }, ...rest];
}

function refuseUnlessEnforced(
  verb: string,
  path: string,
  carried: boolean,
  hint: string | undefined,
): void {
  if (carried) return;

  throw new UnenforcedCallError(
    `${verb} ${path} was about to leave a consent-enforced client without going through ID ` +
      "Side consent enforcement, so it was not sent. Every body-bearing request made by a client passed " +
      "to consent.wrap() must carry the ACT token authorized for it, to the server the client was wrapped " +
      "with: a base URL changed afterwards is refused the same way. Endpoints the SDK does not enforce — embeddings, " +
      "images, audio, moderations, batches, file uploads, beta previews — belong on a separate, " +
      "unwrapped client; there is no option to let a call out of a wrapped one." +
      (hint === undefined ? "" : ` ${hint}`),
  );
}

/** A `Request`-shaped first argument, without depending on the global class. */
function isRequestLike(
  value: unknown,
): value is { url: string; method: string; headers: unknown } {
  return (
    isObject(value) &&
    typeof (value as { url?: unknown }).url === "string" &&
    typeof (value as { method?: unknown }).method === "string"
  );
}

function isObject(value: unknown): value is object {
  return value !== null && typeof value === "object";
}

/** Whether `name` carries the token authorized in the current call context. */
function hasProvenancedHeader(
  headers: unknown,
  name: string,
  tokenProvenances: TokenHasProvenance[],
  method: string,
  path: string,
  origin: string,
): boolean {
  if (headers === null || headers === undefined) return false;

  // `Headers` (and any duck-typed equivalent from another realm) performs its
  // own case-insensitive lookup. The value is compared locally and is never
  // logged, hashed or copied elsewhere.
  const get = (headers as { get?: unknown }).get;
  if (typeof get === "function") {
    try {
      const token = (headers as { get(name: string): unknown }).get(name);
      return tokenProvenances.some((check) => check(token, method, path, origin));
    } catch {
      /* not a Headers after all; fall through */
    }
  }

  const wanted = name.toLowerCase();
  if (Array.isArray(headers)) {
    for (const entry of headers) {
      if (
        Array.isArray(entry) &&
        typeof entry[0] === "string" &&
        entry[0].toLowerCase() === wanted &&
        tokenProvenances.some((check) => check(entry[1], method, path, origin))
      ) {
        return true;
      }
    }
    return false;
  }
  if (typeof headers === "object") {
    for (const key of Object.keys(headers as object)) {
      if (
        key.toLowerCase() === wanted &&
        tokenProvenances.some((check) =>
          check((headers as Record<string, unknown>)[key], method, path, origin),
        )
      ) {
        return true;
      }
    }
  }
  return false;
}

/**
 * @google/genai calls the global fetch, from one method of its ApiClient:
 * `apiCall(url, init, …)`. `request()` and `requestStream()` both end there,
 * and it is the one place the real URL and RequestInit exist. So that is what
 * the net masks on the instance, with the same check as any `fetch`: the
 * token's provenance, the route and the origin of the URL actually built, then
 * `redirect: "manual"`. Not finding it is partial coverage, and wrap() refuses
 * the client.
 */
function findGeminiRequests(root: object): Scan {
  if (constructorName(root) !== "GoogleGenAI")
    return { found: [], complete: true };

  const descriptor = Object.getOwnPropertyDescriptor(root, "apiClient");
  if (
    descriptor === undefined ||
    !("value" in descriptor) ||
    !isObject(descriptor.value)
  ) {
    return { found: [], complete: false };
  }

  const owner = descriptor.value as Record<string, unknown>;
  const method = dataMethod(owner, "apiCall");
  return method === undefined
    ? { found: [], complete: false }
    : { found: [{ owner, key: "apiCall", fn: method }], complete: true };
}

/**
 * The Gemini "NextGen" surfaces, and why `apiCall` is not enough.
 *
 * `@google/genai` 2.22 carries a second API beside `models` / `chats` /
 * `files`: five prototype getters on `GoogleGenAI` — `interactions`,
 * `webhooks`, `agents`, `triggers`, `environments` — and `getNextGenClient()`.
 * 3.x adds two more of the same make, `credentials` and `voices`: each is a
 * `GeminiNextGen…` class whose `getClient(apiVersion)` calls
 * `buildGoogleGenAIClient(parentClient)`, a generated client of its own, exactly
 * as `webhooks` does.
 * They are accessors, so they appear in no `Object.keys(client)` and the walk
 * below never reaches them; and none of them sends through `ApiClient.apiCall`,
 * where the net sits. Each builds a generated Speakeasy client whose own
 * `HTTPClient` holds a plain `fetcher` calling the global `fetch`. So
 * `interactions.create({ model, input })` on a wrapped client left the process
 * with the prompt and the API key, no ACT token and no `/v1/authorize`.
 *
 * The net CAN be installed there, so it is — a client is not refused for
 * owning these. Three things make it awkward, and each is why the code below
 * is shaped as it is:
 *
 * - the transports do not exist at `wrap()`. Each surface builds its SDK on
 *   first use, and `getClient(api_version)` builds a NEW one on EVERY call. So
 *   what is patched is `getClient` itself, and the net goes on whatever it
 *   hands back, every time;
 * - four of the getters print an experimental-usage warning on first access.
 *   Reading them at `wrap()` to reach what they build would print warnings the
 *   developer never caused, so each is shadowed by an own accessor that defers
 *   to the real one only when the developer asks for it;
 * - a surface read BEFORE `wrap()` is cached on the instance, and the shadow
 *   would never be asked for it again. Those are guarded where they already are,
 *   read as own data properties so no accessor is evaluated.
 *
 * No descriptor declares a call site on any of these routes, so the net refuses
 * every body-bearing request made on them — which is the correct, closed answer
 * — and lets every retrieval through, exactly as it does everywhere else.
 *
 * ## Why this cover is NAMED, and not a general rule about accessors
 *
 * The root cause is structural: `findFetches` walks the client's OWN data
 * properties, so a resource behind a PROTOTYPE accessor is invisible to it. The
 * obvious generalisation — read every prototype accessor and look for a
 * transport — is the one thing that must not be done. Reading an accessor runs
 * somebody else's code: four of these seven print a warning the developer never
 * asked for, and nothing says a getter in another library is free of worse.
 * `wrap()` causing side effects on the client it was handed is not a trade this
 * SDK makes.
 *
 * A named cover is enough because the other three libraries were checked, on
 * the versions this package builds against:
 *
 * - `openai` and `@anthropic-ai/sdk` expose no prototype accessor at all —
 *   `chat`, `responses`, `beta`, `batches`, `messages`, `models`… are own
 *   properties, and every one of them sends on the parent's `fetch`;
 * - `@mistralai/mistralai` DOES expose prototype accessors (`beta`, `chat`,
 *   `agents`, `ocr`, `workflows`, `events`…), but each sub-client — and
 *   `beta.conversations` with them — holds the very `HTTPClient` instance the
 *   net already patches. Object identity was verified, not assumed;
 * - `@google/genai` is the only one where a prototype accessor builds a NEW
 *   transport.
 *
 * So the list below is a classification, and a name the library adds must not
 * be able to arrive quietly: `test/callTimeEscapes.test.ts` compares it against
 * what the INSTALLED `GoogleGenAI.prototype` actually exposes, and fails until
 * somebody has looked at the new one. The day another library grows a getter
 * that builds its own transport, it needs the same treatment and the same
 * check — not a generic walk over accessors.
 */
const NEXTGEN_SURFACES = [
  "interactions",
  "webhooks",
  "agents",
  "triggers",
  "environments",
  "credentials",
  "voices",
] as const;

/** Private marker holding, on one client, the installs its surfaces must carry. */
const NEXTGEN: unique symbol = Symbol("idside.guard.nextgen");
/** Private marker on a factory method this SDK has already shadowed. */
const SHADOWED: unique symbol = Symbol("idside.guard.shadowed");

/** What one wrapped client needs installed on every SDK its surfaces build. */
interface NextGenInstall {
  readonly tokenHeader: string;
  readonly entry: Provenance;
}

/** Added to the refusal, because the generic advice does not name this door. */
const NEXTGEN_HINT =
  "This one went out on a Gemini NextGen surface — interactions, webhooks, agents, triggers, " +
  "environments, credentials, voices, or the client getNextGenClient() returns. No provider descriptor declares a call site " +
  "there, so there is no endpoint for ID Side to decide and nothing on those routes can be authorized: " +
  "use a second, unwrapped GoogleGenAI client for them.";

const NEXTGEN_UNGUARDABLE =
  "A Gemini NextGen surface built an HTTP client this SDK could not put its net on, so the request was " +
  "not made: nothing would have stopped it leaving a consent-enforced client unauthorized. Use a " +
  "separate, unwrapped GoogleGenAI client for that surface, and report the @google/genai version.";

/**
 * Put every SDK the NextGen surfaces build, now or later, under this client's net.
 *
 * Returns false when a route is, or may be, left uncovered — `wrap()` then
 * refuses the client, as it does for any other partial coverage.
 */
function guardNextGen(
  client: object,
  tokenHeader: string,
  entry: Provenance,
  rollbacks: Array<() => void>,
): boolean {
  if (constructorName(client) !== "GoogleGenAI") return true;

  const install: NextGenInstall = { tokenHeader, entry };
  const known = (client as Record<PropertyKey, unknown>)[NEXTGEN];
  if (Array.isArray(known)) {
    // A second wrap() of the same client, or of a client named as its own
    // credential: the patches are already in place and only the check is new.
    const installs = liveInstalls(known as NextGenInstall[]);
    installs.push(install);
    rollbacks.push(() => {
      const index = installs.lastIndexOf(install);
      if (index >= 0) installs.splice(index, 1);
    });
    return true;
  }

  const installs: NextGenInstall[] = [install];
  try {
    Object.defineProperty(client, NEXTGEN, { value: installs, configurable: true, enumerable: false });
  } catch {
    return false;
  }
  rollbacks.push(() => restoreProperty(client, NEXTGEN, undefined));

  let covered = true;
  for (const name of NEXTGEN_SURFACES) {
    covered = shadowSurface(client, name, installs, rollbacks) && covered;
  }
  covered = shadowFactory(client, "getNextGenClient", installs, "accept", rollbacks) && covered;
  return covered && guardBuiltSurfaces(client, installs, rollbacks);
}

/**
 * Shadow one surface getter, so what it hands back is guarded before it is used.
 *
 * An own accessor over the prototype one: the real getter is called only when
 * the developer reads the surface, and it answers exactly what it always
 * answered. Nothing is shadowed on a library version that has no such surface.
 */
function shadowSurface(
  client: object,
  name: string,
  installs: NextGenInstall[],
  rollbacks: Array<() => void>,
): boolean {
  const original = accessorOf(client, name);
  if (original === undefined) return true;

  const previous = Object.getOwnPropertyDescriptor(client, name);
  const shadow = function (this: object): unknown {
    const surface = original.call(this);
    if (isObject(surface) && !shadowFactory(surface, "getClient", installs, "refuse")) {
      throw new UnenforcedCallError(NEXTGEN_UNGUARDABLE);
    }
    return surface;
  };
  try {
    Object.defineProperty(client, name, { configurable: true, enumerable: false, get: shadow });
  } catch {
    return false;
  }
  // Read back through the descriptor, never through the property: reading the
  // property would run whichever getter is there, which is the one thing this
  // install exists to avoid doing on the developer's behalf.
  if (Object.getOwnPropertyDescriptor(client, name)?.get !== shadow) return false;
  rollbacks.push(() => restoreProperty(client, name, previous));
  return true;
}

/**
 * Shadow a method that hands back an SDK, so the net is on it before it is used.
 *
 * `getNextGenClient()` on the client, and `getClient(apiVersion)` on each
 * surface: the same shape, and the second is why this exists at all — it builds
 * a new SDK on every call that names an API version.
 *
 * `absent` says what it means for the method not to be there, because it means
 * two different things. On the client it is a library version that predates the
 * NextGen API: nothing to shadow. On a surface it is the only way that surface
 * builds anything, so a surface without it is a route this SDK cannot judge.
 */
function shadowFactory(
  owner: object,
  name: string,
  installs: NextGenInstall[],
  absent: "accept" | "refuse",
  rollbacks?: Array<() => void>,
): boolean {
  const record = owner as Record<string, unknown>;
  const original = dataMethod(record, name);
  if (original === undefined) return absent === "accept";
  // Already shadowed: a surface read twice, or a client wrapped twice. The
  // shadow reads the same `installs` this call was given, so there is nothing
  // to add — and stacking a second one would call the first, which counts a
  // request's coverage twice and undoes half of it on rollback.
  if ((original as unknown as Record<PropertyKey, unknown>)[SHADOWED] === true) return true;

  const previous = Object.getOwnPropertyDescriptor(owner, name);
  const guarded = function (this: unknown, ...args: unknown[]): unknown {
    const sdk = original.apply(this, args);
    if (!isObject(sdk) || !guardSdk(sdk, installs)) {
      throw new UnenforcedCallError(NEXTGEN_UNGUARDABLE);
    }
    return sdk;
  };
  Object.defineProperty(guarded, SHADOWED, { value: true, enumerable: false });
  try {
    Object.defineProperty(owner, name, {
      value: guarded,
      configurable: true,
      writable: true,
      enumerable: false,
    });
  } catch {
    return false;
  }
  if (record[name] !== guarded) return false;
  rollbacks?.push(() => restoreProperty(record, name, previous));
  return true;
}

/**
 * Guard what the surfaces already built, before this client was wrapped.
 *
 * Own DATA properties only, so no accessor is evaluated and no experimental
 * warning is printed: the library caches each surface under `_interactions`,
 * `_agents`… and the SDK `getNextGenClient()` built under `_nextGenClient`.
 *
 * The surface's own name is read here as well, and not only its cache. A
 * version that stopped putting `interactions` behind a getter and simply built
 * it in the constructor would leave `accessorOf` nothing to shadow and
 * `_interactions` empty, and the client it builds would be reached by nothing —
 * quietly, because the walk stops at own properties and this one holds its
 * transport behind a method. The library does not do this today; the cover must
 * not depend on it continuing not to.
 */
function guardBuiltSurfaces(
  client: object,
  installs: NextGenInstall[],
  rollbacks: Array<() => void>,
): boolean {
  let covered = true;
  for (const name of NEXTGEN_SURFACES) {
    for (const built of [ownValue(client, `_${name}`), ownValue(client, name)]) {
      if (built !== undefined) covered = shadowFactory(built, "getClient", installs, "refuse") && covered;
    }
  }
  const sdk = ownValue(client, "_nextGenClient");
  if (sdk !== undefined) covered = guardSdk(sdk, installs, rollbacks) && covered;
  return covered;
}

/** An own data property's object value, without evaluating anything. */
function ownValue(owner: object, key: string): object | undefined {
  const descriptor = Object.getOwnPropertyDescriptor(owner, key);
  return descriptor !== undefined && "value" in descriptor && isObject(descriptor.value)
    ? descriptor.value
    : undefined;
}

/**
 * Put the net on one generated SDK: every transport it owns, for every install.
 *
 * No transport found is a refusal here and not a `false` like everywhere else:
 * this object was built by a library whose shape is known, precisely so that
 * its requests could be judged, and one that owns no reachable `fetch` is a
 * route nothing would stop.
 *
 * An empty `installs` is not that case: it means no wrapped client is asking
 * any more — every one of them was collected or rolled back — and an SDK built
 * by a client nobody wraps has nothing to answer to.
 */
function guardSdk(
  sdk: object,
  installs: NextGenInstall[],
  rollbacks: Array<() => void> = [],
): boolean {
  const scan = findFetches(sdk);
  if (!scan.complete || scan.found.length === 0) return false;
  const live = liveInstalls(installs);
  let covered = true;
  for (const found of scan.found) {
    const named: FoundFetch = { ...found, hint: NEXTGEN_HINT };
    for (const install of live) {
      covered = guardUndoably(named, install.tokenHeader, install.entry, rollbacks) && covered;
    }
  }
  return covered;
}

/** Drop, in place, the installs of clients that were collected, and return the rest. */
function liveInstalls(installs: NextGenInstall[]): NextGenInstall[] {
  let kept = 0;
  for (const install of installs) {
    const scope = install.entry.scope;
    if (scope === undefined || scope.deref() !== undefined) installs[kept++] = install;
  }
  installs.length = kept;
  return installs;
}

/** Find a getter on the object or its prototypes, without evaluating it. */
function accessorOf(owner: object, key: string): ((this: object) => unknown) | undefined {
  let current: object | null = owner;
  for (let depth = 0; current !== null && depth < 8; depth++) {
    const descriptor = Object.getOwnPropertyDescriptor(current, key);
    if (descriptor !== undefined) {
      return typeof descriptor.get === "function"
        ? (descriptor.get as (this: object) => unknown)
        : undefined;
    }
    current = Object.getPrototypeOf(current) as object | null;
  }
  return undefined;
}

function constructorName(value: object): string | undefined {
  const prototype = Object.getPrototypeOf(value) as object | null;
  if (prototype === null) return undefined;
  const descriptor = Object.getOwnPropertyDescriptor(prototype, "constructor");
  return descriptor &&
    "value" in descriptor &&
    typeof descriptor.value === "function"
    ? descriptor.value.name
    : undefined;
}

/** Find a method without evaluating an accessor. */
function dataMethod(
  owner: Record<string, unknown>,
  key: string,
): FetchLike | undefined {
  let current: object | null = owner;
  for (let depth = 0; current !== null && depth < 8; depth++) {
    const descriptor = Object.getOwnPropertyDescriptor(current, key);
    if (descriptor !== undefined) {
      return "value" in descriptor && typeof descriptor.value === "function"
        ? (descriptor.value as FetchLike)
        : undefined;
    }
    current = Object.getPrototypeOf(current) as object | null;
  }
  return undefined;
}

/**
 * Breadth-first search for EVERY `fetch` the client owns, over OWN DATA properties only.
 *
 * Own data properties only, and that is the important part: a getter is never
 * read. Provider clients expose lazily-constructed resources behind accessors,
 * and evaluating one to look for a transport would make `wrap()` cause side
 * effects it has no business causing. It also means an ES private field (`#x`)
 * is invisible here — by construction, not by oversight: nothing in JavaScript
 * can reach one. Depth 2 covers the shapes that exist: `client.fetch` (openai,
 * anthropic) and `client._options.httpClient.fetcher` (mistral).
 */
function findFetches(root: object): Scan {
  const seen = new WeakSet<object>([root]);
  const found: FoundFetch[] = [];
  let frontier: object[] = [root];
  let budget = MAX_NODES;

  for (let depth = 0; depth <= MAX_DEPTH && frontier.length > 0; depth++) {
    const next: object[] = [];
    for (const node of frontier) {
      // Out of budget is not "there is no transport": it is "we stopped
      // looking", and the caller is told which of the two happened. What was
      // already found is still returned so the caller knows discovery was
      // incomplete and can avoid mutating any of it.
      if (budget-- <= 0) return { found, complete: false };
      let keys: string[];
      try {
        keys = Object.getOwnPropertyNames(node);
      } catch {
        continue; // a revoked Proxy, or something equally hostile
      }
      for (const key of keys) {
        const descriptor = Object.getOwnPropertyDescriptor(node, key);
        if (descriptor === undefined || !("value" in descriptor)) continue; // accessor: never evaluated
        const value = descriptor.value as unknown;
        if (TRANSPORT_KEYS.has(key) && typeof value === "function") {
          // Recorded, and the walk goes on: the sibling that holds the second
          // route out is still ahead of us.
          found.push({
            owner: node as Record<string, unknown>,
            key,
            fn: value as FetchLike,
          });
          continue;
        }
        if (
          depth < MAX_DEPTH &&
          isObject(value) &&
          !Array.isArray(value) &&
          !seen.has(value)
        ) {
          seen.add(value);
          next.push(value);
        }
      }
    }
    frontier = next;
  }
  return { found, complete: true };
}
