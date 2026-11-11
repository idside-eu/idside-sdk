// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Public entry point: `consent.wrap()`.
 *
 * ⚠️ *Opaque tunnel* invariant: this module exposes NO function to set or modify
 * preferences (no `setPreferences`, `overrideConsent`…). The SDK only reads the
 * state resolved by the cloud.
 *
 * Enforcement patches the declared call-site methods IN PLACE on the client's
 * resource objects (mirroring the Python SDK's `setattr`), rather than returning
 * a read-only Proxy. This is deliberate: a Proxy only intercepts access *through*
 * it, so a convenience method like `messages.stream()` / `chat.completions.parse()`
 * — which internally calls `this.create(...)` / `this._client.…​.create(...)` on
 * the real object — would reach the provider unenforced. Patching the method on
 * the resource instance means those internal calls hit the enforced version too.
 */

import { AsyncLocalStorage } from "node:async_hooks";

import { getAdapterFor } from "./adapters/index.ts";
import {
  assertDerivableOptions,
  assertEnforceableClient,
  assertEnforceableOptions,
  forwardableOptions,
} from "./adapters/callOptions.ts";
import type { ProviderAdapter } from "./adapters/base.ts";
import { headersToObject, isPlainObject } from "./adapters/base.ts";
import type { CallSite as DescriptorCallSite } from "./descriptors.ts";
import { originOf } from "./destination.ts";
import {
  HttpConsentClient,
  namesSomebody,
  type ConsentClient,
  type ConsentDecision,
} from "./transport.ts";
import {
  CredentialRouter,
  NO_ROUTE,
  missingMessage,
  type Route,
} from "./credentialRouting.ts";
import { installTransportGuard, type NetState } from "./guard.ts";
import type { Enforced } from "./enforced.ts";
import * as log from "./log.ts";
import {
  ConflictingIdentityError,
  CredentialRoutingError,
  MissingCredentialError,
  MissingIdentityError,
  UnsupportedProviderError,
} from "./exceptions.ts";

/** Private, unforgeable marker on patched methods so a second wrap() never stacks. */
const ENFORCED: unique symbol = Symbol("idside.consent.enforced");

interface WrapState {
  complete: boolean;
}

interface InternalCall {
  readonly callSite: string;
  readonly scope: object;
  readonly token: string;
  consumed: boolean;
  transportRoute?: string;
}

/** Context-local, one-shot provenance for a nested provider-method hop. */
const INTERNAL_CALL = new AsyncLocalStorage<InternalCall>();

export interface WrapOptions {
  /** Override the cloud transport (mainly for testing). */
  consentClient?: ConsentClient;
  /** Override provider auto-detection with a ready adapter. */
  adapter?: ProviderAdapter;
  /** Force the provider by name instead of auto-detecting from the client. */
  provider?: string;
  /** Override the ID Side API base URL (else IDSIDE_BASE_URL, else the default). */
  baseUrl?: string;
  /** ID Side API key for the cloud transport (else IDSIDE_API_KEY). */
  apiKey?: string;
  /**
   * Posture label → credential, for per-user credential routing.
   *
   * How an account-level privacy setting becomes a per-user one. No provider
   * exposes training opt-out as a request parameter: the posture lives on the
   * credential — a project at OpenAI and Gemini, a workspace at Anthropic, an
   * organisation at Mistral — so one key imposes one posture on every user of
   * the app. Provision one credential per posture, label each with the posture
   * it carries, and the SDK sends each call on the one this user's answer
   * requires:
   *
   * ```ts
   * const client = consent.wrap(new OpenAI(), {
   *   credentials: {
   *     default: "proj_medhelp_default",      // the OpenAI-Project header
   *     zero_retention: "proj_medhelp_zdr",
   *   },
   * });
   *
   * const claude = consent.wrap(new Anthropic(), {
   *   // no such header exists at Anthropic: the key is chosen at construction
   *   credentials: { no_training: new Anthropic({ apiKey: PRIVATE_WORKSPACE_KEY }) },
   * });
   * ```
   *
   * A value is either an identifier for the provider's per-request credential
   * header (only OpenAI validates one) or a second client of the same kind built
   * with that credential's key. **You label; you do not choose**: which posture a
   * given consent state requires is policy, the cloud answers it, and there is
   * no callback to override it — that would hand the developer the user's
   * preferences (invariant #1). If the cloud names a posture nothing carries,
   * the call throws `MissingCredentialError` and never goes out on another
   * credential. Misconfiguration throws `CredentialRoutingError` from `wrap()`
   * itself, not on the request path.
   *
   * A client passed here sends the calls routed to it, so it is guarded like
   * the wrapped client: its own transport under the net, bound to its own
   * server, refused by `wrap()` when that cannot be done.
   */
  credentials?: Record<string, unknown>;
}

type CallMethod = (this: unknown, ...args: unknown[]) => unknown;

/** A descriptor call site, located on a real client instance. */
interface ResolvedCallSite {
  readonly target: object;
  readonly method: string;
  /** The endpoint's stable wire id, sent to the cloud on every authorize. */
  readonly id: string;
  /** How to find the same endpoint on another client of the same provider. */
  readonly path: readonly string[];
}

/**
 * Wrap an AI provider client to enforce ID Side consent, in place.
 *
 * On every intercepted AI call, the client: (1) resolves the user's preferences
 * from the ID Side cloud and obtains a signed ACT token, (2) forces the
 * provider's non-retention parameters, (3) attaches the ACT token as the
 * `X-AI-Consent-Token` header, (4) calls the provider. If consent cannot be
 * verified, the call does not happen (fail-closed).
 *
 * The caller names the end user on every AI call with exactly ONE of `user_id`
 * (developer-asserted) or `ubt` (widget-issued binding); both are consumed by the
 * SDK and never forwarded to the provider. Passing both throws
 * `ConflictingIdentityError`; passing neither throws `MissingIdentityError` — in
 * both cases no cloud query and no AI call is made. Wrapping is idempotent and
 * returns the same client instance.
 *
 * The same holds for a per-request option that could change what is sent after
 * it was authorized (`options.body`, `fetchOptions`, a `serverURL`, an
 * `Authorization` header…): `UnenforceableOptionError`, before anything
 * happens. And the client is bound to the server it was wrapped with. See
 * `adapters/callOptions.ts` for the closed list.
 *
 * The return type says so: `Enforced<T>` is `T` with the identity required on
 * every declared call site, so TypeScript refuses the call the SDK would have
 * refused at run time. See `enforced.ts` for what that costs — chiefly that the
 * result is no longer assignable to the provider's own class type.
 */
export function wrap<T extends object>(
  client: T,
  options: WrapOptions = {},
): Enforced<T> {
  return wrapClient(client, options, undefined);
}

/**
 * What a derived client inherits from the client it was derived from.
 *
 * Inherited, never recomputed, and deliberately not a `WrapOptions` field: a
 * public option to pass in "the server this client is bound to" would be the
 * binding's own way around.
 */
interface Lineage {
  /** The server every request must go to, or undefined for a stand-in that names none. */
  readonly origin: OriginBinding | undefined;
  /** The provider detected on the first client wrapped, or undefined if detection could not tell. */
  readonly provider: string | undefined;
  /**
   * The server each client named in `credentials` must send to, bound the first
   * time that client is guarded. A derived client routes to the same clients,
   * and reading their base URL again at derivation would bind wherever one had
   * been pointed since.
   */
  readonly alternatives: Map<object, OriginBinding | undefined>;
}

/**
 * Refuse a client whose server cannot be read.
 *
 * A request is authorized for a server as much as for a route. A client whose
 * server cannot be read would leave the net unable to tell one server from
 * another. As below, a plain object literal is a stand-in with no server.
 */
function assertServerReadable(
  client: object,
  origin: OriginBinding | undefined,
  subject: string,
): void {
  if (origin === undefined && !isPlainObject(client)) {
    throw new UnsupportedProviderError(
      `The SDK cannot read which server ${subject} sends its requests to, so it could not ` +
        "refuse one sent elsewhere. The client was not wrapped; use a supported client version or report its shape.",
    );
  }
}

/** Refuse a client the net did not cover completely. */
function assertCovered(client: object, guarded: NetState, subject: string): void {
  const Subject = subject.charAt(0).toUpperCase() + subject.slice(1);
  if (guarded === "partial") {
    throw new UnsupportedProviderError(
      `${Subject} exposes an HTTP transport, but the transport guard could not cover every ` +
        "route. The client was not wrapped; use a supported client version or report its transport shape.",
    );
  }
  // No transport found is not "nothing to guard" for a real client. Every
  // provider library builds its client from a class, and one whose transport
  // the walk cannot see keeps it where no JavaScript can reach: Mistral's
  // default HTTP client sits in an ES private field. Wrapping it anyway would
  // enforce the declared call sites and leave every other route out open, under
  // a journal that said `net=false`. A plain object literal is the one shape
  // that holds nothing it does not show, so a test double or the parity script's
  // stand-in, with no transport, stays wrappable.
  if (guarded === false && !isPlainObject(client)) {
    throw new UnsupportedProviderError(
      `${Subject} has no HTTP transport the SDK can guard, so nothing would stop a request ` +
        "its declared call sites do not cover. The client was not wrapped. Give it a transport it can reach: " +
        "for Mistral, `new Mistral({ apiKey, httpClient: new HTTPClient() })` (HTTPClient is exported by " +
        "@mistralai/mistralai); for another library, pass its `fetch` option.",
    );
  }
}

/**
 * Put every client named in `credentials` under the net the wrapped client is under.
 *
 * A call routed to one of them is sent by that client, through its own
 * transport, to its own server. So each is bound like the wrapped client and
 * never to it: a residency posture is another host by design
 * (`https://eu.api.openai.com/v1`). Its transport is guarded, its origin read
 * from its own base URL, and it is refused by the same rules when either cannot
 * be done — the net on the wrapped client says nothing about a request another
 * client sends.
 *
 * Returns the scope a call's permit must carry to leave through each client.
 * The wrapped client named as its own posture keeps its own scope, so a nested
 * hop through one of its patched methods is still recognised as the call's.
 */
function guardRoutedClients(
  client: object,
  credentials: Record<string, unknown> | undefined,
  adapter: ProviderAdapter,
  lineage: Lineage,
  transportScope: object,
  undo: Array<() => void>,
): ReadonlyMap<object, object> {
  const scopes = new Map<object, object>([[client, transportScope]]);
  for (const [posture, credential] of Object.entries(credentials ?? {})) {
    // Anything but a string was resolved as a client by the router already.
    if (typeof credential === "string" || scopes.has(credential as object)) continue;
    const routed = credential as object;
    const scope = {};
    scopes.set(routed, scope);

    if (!lineage.alternatives.has(routed)) lineage.alternatives.set(routed, bindOrigin(routed));
    const origin = lineage.alternatives.get(routed);
    const subject = `the client passed as credential '${posture}'`;
    assertServerReadable(routed, origin, subject);
    const guarded = installTransportGuard(
      routed,
      adapter.descriptor.tokenHeader,
      adapter.name,
      transportProvenance(scope, origin),
      scope,
      undo,
    );
    assertCovered(routed, guarded, subject);
  }
  return scopes;
}

function wrapClient<T extends object>(
  client: T,
  given: WrapOptions,
  inherited: Lineage | undefined,
): Enforced<T> {
  // The mapping is read ONCE, here. A developer's object stays theirs, and
  // a posture added to it after wrap() must route nothing: the labels were
  // declared to the cloud, every client behind one was guarded and bound to its
  // own server, and a later entry has been through none of that. Without the
  // copy the re-read happened at every derivation — `withOptions({ timeout })`
  // was enough to enrol a credential nobody had declared. Python freezes it the
  // same way, in `_wrap`, and its README promises it. Shallow on purpose: the
  // values are the developer's own clients, and they are meant to be the same
  // objects.
  // Only a mapping is copied: an array or a bare string is passed on as it was
  // given, so the router still refuses it by name rather than being handed a
  // spread of it that no longer looks like the mistake it is.
  const options: WrapOptions = isRecord(given.credentials)
    ? { ...given, credentials: { ...given.credentials } }
    : given;
  const adapter = options.adapter ?? getAdapterFor(client, options.provider);

  const sites = resolveCallSites(client, adapter.descriptor.callSites);
  if (sites.length === 0) {
    throw new UnsupportedProviderError(
      `None of the call sites for provider '${adapter.name}' exist on the given client.`,
    );
  }

  // Idempotence includes the transport guard. Re-installing it would retain an
  // unusable provenance closure on every wrap() call. A completion state, set
  // only after call sites and derived-client methods are patched, distinguishes
  // a fully wrapped client from an interrupted construction.
  if (
    sites.every(({ target, method }) =>
      isCompletelyWrapped(
        (target as Record<PropertyKey, unknown>)[method],
      ),
    )
  ) {
    return client as unknown as Enforced<T>;
  }

  // Resolved before anything is patched: an alternative client may be the
  // wrapped one itself (`{ default: client }` is the honest way to say what the
  // base key is), and capturing its method now is what stops a call routed back
  // to it from re-entering enforcement.
  const router =
    options.credentials && Object.keys(options.credentials).length > 0
      ? new CredentialRouter(
          adapter.descriptor,
          options.credentials,
          sites,
          isEnforced,
        )
      : undefined;

  const consentClient =
    options.consentClient ??
    // The transport resolves the address and the key itself — the option, then
    // IDSIDE_BASE_URL / IDSIDE_API_KEY — as Python's does, so it knows when the
    // address is its default and can say so when it does not answer.
    new HttpConsentClient(options.baseUrl, { apiKey: options.apiKey });

  const lineage: Lineage = inherited ?? {
    origin: bindOrigin(client),
    provider: detectedProvider(client),
    alternatives: new Map(),
  };
  const subject = `the '${adapter.name}' client`;
  assertServerReadable(client, lineage.origin, subject);

  // A construction option that rewrites every request (a client-level
  // `fetchOptions.body`, Gemini's `httpOptions.extraBody`) would make every
  // proof describe a request that did not go out. Refused before anything is
  // patched, like everything below.
  assertEnforceableClient(adapter.descriptor, client);
  // A client named in `credentials` sends the calls routed to it, so a rewrite
  // declared on it rewrites those calls exactly as one declared on `client`.
  for (const alternative of Object.values(options.credentials ?? {})) {
    if (typeof alternative !== "string") assertEnforceableClient(adapter.descriptor, Object(alternative));
  }

  // Require a complete net. Run this before patching call sites, so that a
  // construction failure leaves them untouched.
  const transportScope = {};
  const wrapState: WrapState = { complete: false };
  const undo: Array<() => void> = [];
  const guarded = installTransportGuard(
    client,
    adapter.descriptor.tokenHeader,
    adapter.name,
    transportProvenance(transportScope, lineage.origin),
    transportScope,
    undo,
  );
  assertCovered(client, guarded, subject);

  let routedScopes: ReadonlyMap<object, object>;
  try {
    routedScopes = guardRoutedClients(client, options.credentials, adapter, lineage, transportScope, undo);
  } catch (error) {
    for (const step of undo.reverse()) step();
    throw error;
  }

  for (const { target, method, id, path } of sites) {
    const record = target as Record<PropertyKey, unknown>;
    const original = record[method];
    if (typeof original !== "function") continue;
    if (isEnforced(original)) continue; // already enforced
    record[method] = makeEnforced(
      original as CallMethod,
      target,
      adapter,
      consentClient,
      id,
      path,
      transportScope,
      wrapState,
      routedScopes,
      router,
    );
  }

  // The net under the patched call sites: whatever the descriptor does not
  // declare — a new resource, a helper posting straight to the transport — is
  // refused at the transport rather than escaping silently.
  // And a client derived from this one comes back enforced, not naked.
  patchDerivedClients(client, options, adapter, consentClient, lineage);
  // A successful retry also completes wrappers left by an earlier interrupted
  // attempt: their transport predicate remains installed, and this attempt has
  // now covered every remaining call site and derived-client method.
  for (const { target, method } of sites) {
    const state = enforcedState(
      (target as Record<PropertyKey, unknown>)[method],
    );
    if (state !== undefined) state.complete = true;
  }

  // Half the support questions a wrapped client ever produces are "is it even
  // being enforced?". This answers all of them at once: which descriptor
  // matched, how many entry points were patched, and what the net came to:
  // `true` means every supported route was guarded; `false` means a plain
  // stand-in exposed none. A partial result, and a real client with no route
  // found, were already refused above. Posture labels are
  // app configuration, so they can be named; the credentials behind them never
  // are.
  log.event("wrap", {
    provider: adapter.name,
    call_sites: sites.length,
    // Constant here, and deliberately present anyway: Python emits `blocking`
    // or `async` because it wraps two shapes of client, and a field that exists
    // in one SDK's journal and not the other's is a field somebody debugs
    // without. This SDK has one shape, and saying so is the honest answer.
    mode: "async",
    net: guarded,
    postures: router ? [...router.postures].sort() : undefined,
  });

  // The same object, deliberately: enforcement patched its methods in place, so
  // every reference the caller already held is enforced too. `Enforced<T>` is a
  // purely type-level statement about what those patched methods now require,
  // and there is no value for the compiler to check it against.
  return client as unknown as Enforced<T>;
}

/**
 * Make `withOptions()` / `copy()` hand back a wrapped client.
 *
 * Provider clients let a developer derive a fresh client that re-uses the same
 * options — a different timeout for one call, a different retry policy. The
 * derived client is a NEW object: none of its resource methods were patched, so
 * every call through it used to reach the provider unenforced. Patching the
 * factory means the derived client is wrapped with the SAME adapter, consent
 * client and credentials before the developer ever sees it — and, because that
 * goes through the same wrapping, its own `withOptions()` is patched too,
 * recursively.
 *
 * The SAME adapter is exactly why a derivation is checked first. The adapter
 * decides which provider the cloud is told about and which credentials the
 * labels mean, and a derivation can change what it no longer describes: a
 * DeepSeek client given OpenAI's base URL used to go on announcing
 * `provider=deepseek` to the cloud.
 */
function patchDerivedClients(
  client: object,
  options: WrapOptions,
  adapter: ProviderAdapter,
  consentClient: ConsentClient,
  lineage: Lineage,
): void {
  const record = client as Record<PropertyKey, unknown>;
  for (const name of DERIVING_METHODS) {
    const original = record[name];
    if (typeof original !== "function") continue;
    if (isEnforced(original)) continue;

    const deriving = function (this: unknown, ...args: unknown[]): unknown {
      assertDerivableOptions(adapter.descriptor, args[0], (this ?? client) as object);
      const derived = (original as CallMethod).apply(this ?? client, args);
      if (derived === null || typeof derived !== "object") return derived;
      assertDerivable(derived, adapter, lineage);
      return wrapClient(
        derived as object,
        { ...options, adapter, consentClient },
        lineage,
      );
    };
    Object.defineProperty(deriving, ENFORCED, {
      value: true,
      enumerable: false,
    });
    record[name] = deriving;
  }
}

/** Factory methods returning a fresh client built from this one's options. */
const DERIVING_METHODS = ["withOptions", "copy"] as const;

/**
 * Refuse a derived client the parent's enforcement no longer describes.
 *
 * Its options were already checked against a closed list, before it was
 * constructed (`assertDerivableOptions`). What only the constructed client can
 * say is checked here, before it exists for the caller, so nothing it would
 * have sent is ever sent: the same provider (detected again, compared with what
 * was detected on the first client wrapped) and the same server.
 */
function assertDerivable(
  derived: object,
  adapter: ProviderAdapter,
  lineage: Lineage,
): void {
  const provider = detectedProvider(derived);
  if (provider !== lineage.provider) {
    throw new UnsupportedProviderError(
      `This client was derived from a wrapped '${lineage.provider ?? "unidentified"}' client, and it is a ` +
        `'${provider ?? "unidentified"}' client. It was not returned: its requests would be authorized and proved ` +
        "under the first provider's rules while going to another. Wrap a separately constructed client for it.",
    );
  }

  if (lineage.origin !== undefined) {
    const expected = lineage.origin.value ?? lineage.origin.read();
    if (originOf(serverOf(derived)) !== expected) {
      throw new UnsupportedProviderError(
        `This client was derived from a wrapped '${adapter.name}' client, and it sends its requests to another ` +
          "server. It was not returned: deriving changes how a call is attempted, not where it goes. Wrap a " +
          "separately constructed client for that server.",
      );
    }
  }

}

/**
 * The server a wrapped client's requests must go to.
 *
 * `value` is bound when the client is wrapped. The one exception is a client
 * whose base URL is still being resolved: Anthropic's, while its credential
 * chain reads a profile that may name the host. Binding the default host then
 * would refuse every legitimate call, and binding from a request URL would bind
 * whatever that request says. So `value` stays empty and is read from the
 * client itself, once, when the first authorized request goes out.
 */
interface OriginBinding {
  value: string | undefined;
  readonly read: () => string | undefined;
}

function bindOrigin(client: object): OriginBinding | undefined {
  // Weakly, for the same reason: the binding travels with the transport's check.
  const clientRef = new WeakRef(client);
  // What the client names now. Only a host its credential chain resolves may
  // replace it later: a `baseURL` assigned between wrap() and the first request
  // is not a resolution, and reading it then would bind wherever it points.
  const atWrap = originOf(serverOf(client));
  // `Object(undefined)` is an empty object, so a collected client simply names
  // no server — and the check that still holds this binding is pruned anyway.
  const read = () => originOf(resolvedServerOf(Object(clientRef.deref()) as object)) ?? atWrap;
  if (baseUrlStillResolving(client)) return { value: undefined, read };
  return atWrap === undefined ? undefined : { value: atWrap, read };
}

/**
 * The host Anthropic's credential chain resolved for a client built without an
 * explicit base URL, or undefined while none is.
 */
function resolvedServerOf(client: object): unknown {
  const record = client as Record<string, unknown>;
  // Anthropic copies a profile's host onto `baseURL` lazily, when a request is
  // built. The value it copies is already on its auth state.
  const auth = record._authState as { baseURL?: unknown } | null | undefined;
  return record._baseURLIsExplicit === false && typeof auth?.baseURL === "string" ? auth.baseURL : undefined;
}

/** Where each supported library keeps the base URL its requests are built from. */
function serverOf(client: object): unknown {
  const record = client as Record<string, unknown>;
  const resolved = resolvedServerOf(client);
  if (resolved !== undefined) return resolved;
  if (record.baseURL !== undefined) return record.baseURL; // openai, anthropic
  if (record._baseURL !== undefined) return record._baseURL; // mistral: a URL
  const apiClient = record.apiClient as { getBaseUrl?: unknown } | undefined;
  if (typeof apiClient?.getBaseUrl === "function") {
    try {
      return (apiClient as { getBaseUrl(): unknown }).getBaseUrl(); // @google/genai
    } catch {
      return undefined;
    }
  }
  return undefined;
}

function baseUrlStillResolving(client: object): boolean {
  const record = client as Record<string, unknown>;
  const auth = record._authState as { resolution?: unknown } | null | undefined;
  return (
    record._baseURLIsExplicit === false &&
    auth !== null &&
    typeof auth === "object" &&
    auth.resolution != null
  );
}

/** The provider detection names for `client`, or undefined when it cannot tell. */
function detectedProvider(client: object): string | undefined {
  try {
    return getAdapterFor(client).name;
  } catch (error) {
    if (error instanceof UnsupportedProviderError) return undefined;
    throw error;
  }
}

/** True if this SDK has already patched `method`. */
function isEnforced(method: unknown): boolean {
  return Boolean(
    (method as Record<PropertyKey, unknown> | undefined)?.[ENFORCED],
  );
}

function enforcedState(method: unknown): WrapState | undefined {
  const state = (method as Record<PropertyKey, unknown> | undefined)?.[ENFORCED];
  return state !== null && typeof state === "object" && "complete" in state
    ? (state as unknown as WrapState)
    : undefined;
}

function isCompletelyWrapped(method: unknown): boolean {
  return enforcedState(method)?.complete === true;
}

/**
 * Find the credential this call must go out on, or refuse to make it.
 *
 * `posture` is undefined for almost every call: the cloud names one only when
 * the user's answer requires a credential other than the one the client was
 * built with.
 */
function resolveRoute(
  router: CredentialRouter | undefined,
  posture: string | undefined,
  adapter: ProviderAdapter,
  callPath: readonly string[],
): Route {
  if (posture === undefined) return NO_ROUTE;
  if (router === undefined) {
    throw new MissingCredentialError(
      missingMessage(adapter.descriptor, posture, []),
      {
        posture,
        provider: adapter.name,
      },
    );
  }
  return router.route(posture, callPath);
}

/**
 * Refuse to call the provider if the routed credential did not land.
 *
 * The adapter is what places headers on a call, and a custom one is free to
 * override that. Verifying rather than trusting costs a lookup and turns the one
 * failure that would be invisible — a call leaving on the credential routing was
 * meant to move it off — into an error.
 */
function assertRouted(
  adapter: ProviderAdapter,
  args: unknown[],
  headers: Record<string, string>,
): void {
  const applied = headersToObject(
    (args[1] as { headers?: unknown } | undefined)?.headers,
  );
  for (const [name, value] of Object.entries(headers)) {
    if (applied[name] !== value) {
      throw new CredentialRoutingError(
        `The '${adapter.name}' adapter did not carry the routed credential onto this call, so the AI ` +
          "call was not made. An adapter overriding buildEnforcedArgs() must forward its " +
          "credentialHeaders argument.",
      );
    }
  }
}

/**
 * Aliases whose patching would change the shape of the return value, and which
 * Node therefore does not patch.
 *
 * Every `stream()` helper — `messages.stream()`, `chat.completions.stream()`,
 * `responses.stream()` — returns its stream object **synchronously**; the
 * documented idiom is `client.messages.stream(...).on("text", …)`, not `await`.
 * The enforced wrapper is `async`, so patching `stream` would turn that into a
 * Promise and break every caller. It costs nothing here: a JS helper forwards
 * ONE body object — identity included — to `create()`, which IS patched, so the
 * call is enforced anyway. `test/streamingHelpers.test.ts` proves it rather than
 * assuming it.
 *
 * This is the one deliberate Python/Node asymmetry in the alias mechanism. The
 * Python helpers take a fixed keyword signature, so `user_id=` is a TypeError on
 * them and the identity cannot reach `create` that way: that SDK has to patch
 * `stream` itself. Same outcome, different route — and the descriptor schema
 * anticipates exactly this, requiring an SDK to skip an alias whose interception
 * would change what the method returns.
 */
const UNPATCHABLE_ALIASES: ReadonlySet<string> = new Set(["stream"]);

/**
 * Resolve every declared-and-present endpoint to patch, keeping its wire id.
 *
 * An alias resolves on the same target as its call site and carries the SAME
 * `id`: `chat.completions.parse` is `chat.completions.create` as far as the
 * cloud is concerned, because it is the same HTTP endpoint. Absent aliases are
 * skipped silently, exactly like absent call sites.
 */
function resolveCallSites(
  client: object,
  callSites: readonly DescriptorCallSite[],
): ResolvedCallSite[] {
  const sites: ResolvedCallSite[] = [];
  for (const { id, path, aliases } of callSites) {
    let target: unknown = client;
    let reachable = true;
    for (let i = 0; i < path.length - 1; i++) {
      if (!hasProp(target, path[i])) {
        reachable = false;
        break;
      }
      target = (target as Record<string, unknown>)[path[i]];
    }
    if (!reachable) continue;

    const method = path[path.length - 1];
    if (hasProp(target, method)) {
      sites.push({ target: target as object, method, id, path });
    }
    for (const alias of aliases) {
      if (UNPATCHABLE_ALIASES.has(alias)) continue;
      if (!hasProp(target, alias)) continue;
      sites.push({
        target: target as object,
        method: alias,
        id,
        path: [...path.slice(0, -1), alias],
      });
    }
  }
  return sites;
}

function hasProp(target: unknown, attr: string): boolean {
  return (
    target !== null &&
    (typeof target === "object" || typeof target === "function") &&
    attr in (target as object)
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/**
 * Whether this is the one nested hop authorized by the outer wrapper.
 *
 * A public header is insufficient. The header must match a context-local,
 * one-shot permit bound to the call site and exact ACT token produced for the
 * current authorization. Header lookup stays case-insensitive because provider
 * helpers may fold options into a `Headers` object.
 */
function isInternalCall(
  options: unknown,
  tokenHeader: string,
  callSite: string,
  scope: object,
): boolean {
  let headers: Record<string, unknown>;
  try {
    headers = headersToObject(
      (options as { headers?: unknown } | undefined)?.headers,
    );
  } catch {
    // Headers this cannot even read carry no permit. The call goes on to
    // enforcement, and the options check there refuses it by name.
    return false;
  }
  const wanted = tokenHeader.toLowerCase();
  const matching = Object.entries(headers).find(
    ([name]) => name.toLowerCase() === wanted,
  );
  const permit = INTERNAL_CALL.getStore();
  if (
    matching === undefined ||
    permit === undefined ||
    permit.consumed ||
    permit.scope !== scope ||
    permit.callSite !== callSite ||
    permit.token !== matching[1]
  ) {
    return false;
  }
  permit.consumed = true;
  return true;
}

/**
 * Whether the provider transport belongs to the currently authorized call.
 *
 * Belongs means: this client's permit, this exact token, the server the client
 * was wrapped with, and the route the first request of this call took. A
 * provider retry resends the same route to the same server and passes; a base
 * URL changed after wrap() does not.
 */
function transportProvenance(
  scope: object,
  origin: OriginBinding | undefined,
): (token: unknown, method: string, path: string, requestOrigin: string) => boolean {
  // Weakly: this check outlives nothing. The transport holds it, and a strong
  // reference here would keep a collected derived client's scope alive.
  const scopeRef = new WeakRef(scope);
  return (
    token: unknown,
    method: string,
    path: string,
    requestOrigin: string,
  ): boolean => {
    const permit = INTERNAL_CALL.getStore();
    if (
      typeof token !== "string" ||
      permit === undefined ||
      permit.scope !== scopeRef.deref() ||
      permit.token !== token
    ) {
      return false;
    }
    if (origin !== undefined) {
      // Bound here only when wrap() could not bind it (see OriginBinding).
      origin.value ??= origin.read();
      if (requestOrigin !== origin.value) return false;
    }
    const route = `${method.toUpperCase()} ${path}`;
    permit.transportRoute ??= route;
    return permit.transportRoute === route;
  };
}

/**
 * The identity as it will be sent, `undefined` when it names nobody — as the
 * Python SDK decides it: "", 0, false, null, undefined, [], {} and a string made
 * only of blanks are all absent. Nothing else is trimmed or converted.
 */
function identityOf(value: unknown): unknown {
  return namesSomebody(value) ? value : undefined;
}

/**
 * Build the consent-enforcing wrapper around one provider call method.
 *
 * `callSite` is the endpoint's stable id, closed over per patched method. It
 * travels to the cloud on every authorize so the decision is made for the
 * endpoint actually being called: `background` is a retention lever on
 * `/v1/responses` and an unknown parameter on `chat.completions`.
 *
 * `router` holds the developer's labelled credentials, when they provisioned
 * several. It decides WHICH credential carries the call; the cloud decides which
 * posture that has to be.
 */
function makeEnforced(
  original: CallMethod,
  fallbackReceiver: object,
  adapter: ProviderAdapter,
  consentClient: ConsentClient,
  callSite: string,
  callPath: readonly string[],
  transportScope: object,
  wrapState: WrapState,
  routedScopes: ReadonlyMap<object, object>,
  router?: CredentialRouter,
): CallMethod {
  const { identityParam, ubtParam, tokenHeader } = adapter.descriptor;

  /**
   * Re-entrancy: an alias routes through its own call site — the real
   * `chat.completions.parse` calls the very `create` this SDK just patched. A
   * second enforcement would fail, and rightly: the identity was consumed by the
   * first hop, so this one arrives without one. The outer wrapper issues a
   * context-local one-shot permit bound to the call site and exact ACT token;
   * the nested hop must present both the permit and matching header.
   *
   * Deliberately NOT async: this hop must return the original's value
   * *untouched*, not a Promise wrapping it. `parse()` calls `._thenUnwrap()` on
   * what `create()` gave it, and an `async` pass-through would hand it a plain
   * Promise that has no such method.
   */
  const enforced = function (this: unknown, ...args: unknown[]): unknown {
    if (isInternalCall(args[1], tokenHeader, callSite, transportScope)) {
      // Said out loud, because the alternative is a journal in which one
      // provider call has no authorize next to it and looks like the leak this
      // SDK exists to prevent.
      log.event("passthrough", { call_site: callSite });
      return original.apply(this ?? fallbackReceiver, args);
    }
    return withResponseHelpers(enforce.apply(this, args));
  };

  const enforce = async function (
    this: unknown,
    ...args: unknown[]
  ): Promise<ProviderResult> {
    const body = args[0];
    const rest = args.slice(1);
    const params: Record<string, unknown> = { ...(isRecord(body) ? body : {}) };
    const userId = identityOf(params[identityParam]);
    const ubt = identityOf(params[ubtParam]);
    delete params[identityParam];
    delete params[ubtParam];

    if (userId !== undefined && ubt !== undefined) {
      throw new ConflictingIdentityError(
        "Pass either user_id or ubt on a consent-enforced AI call, not both: the UBT already names the user.",
      );
    }
    if (userId === undefined && ubt === undefined) {
      throw new MissingIdentityError(
        "user_id or ubt is required for consent-enforced AI calls.",
      );
    }

    // Before the hash and before the cloud: an option that can change what is
    // sent after it was authorized makes the authorization worthless, so the
    // call is refused while nothing has happened yet.
    assertEnforceableOptions(adapter.descriptor, params, rest);

    const meta = adapter.extractRequestMetadata(params);
    // Every journalled field below is either a fact about the developer's own
    // request or already a digest: a salted pseudonym instead of the identity,
    // a truncated request hash instead of the ACT token, the names of the
    // forced parameters instead of the consent state that chose them.
    const subject = log.pseudonym(ubt ?? userId);
    const elapsed = log.start();
    let decision: ConsentDecision;
    try {
      decision = await consentClient.authorize({
        provider: adapter.name,
        model: meta.model,
        requestHash: meta.requestHash,
        callSite,
        // Untouched, as Python sends them: the cloud is the one judge of what an
        // identity may be. `String()` here once authorized `true` as the user
        // "true" and every object as one person, "[object Object]".
        userId,
        ubt,
        credentialPostures: router?.postures ?? [],
      });
    } catch (error) {
      // The class of what went wrong and how long it took, never the message:
      // an exception message is written for a human and can quote whatever it
      // was handed. The message reaches the developer anyway, on the error they
      // are about to catch.
      log.event("authorize_failed", {
        call_site: callSite,
        model: meta.model,
        subject,
        duration_ms: elapsed(),
        error: error instanceof Error ? error.constructor.name : "unknown",
      });
      throw error;
    }
    // Named once: the event drops the field when there is none, and reading
    // the same keys twice to decide that is how the two spellings drift.
    const constraintNames = Object.keys(decision.constraints ?? {}).sort();
    log.event("authorize", {
      call_site: callSite,
      model: meta.model,
      subject,
      binding: ubt !== undefined ? "ubt" : "user_id",
      request_hash: log.shortHash(meta.requestHash),
      duration_ms: elapsed(),
      forced: Object.keys(decision.paramsForced).sort(),
      // Omitted when empty, not rendered as an empty list: "the cloud imposed
      // no constraint" is said by the absence of the field. Python says it the
      // same way, and the parity script caught the day this did not.
      constraints: constraintNames.length ? constraintNames : undefined,
      posture: decision.credentialPosture,
    });

    let allowed: Record<string, unknown>;
    let route: Route;
    try {
      // Constraints first: a refused call must not be dressed with a token and
      // forced parameters on its way to being rejected.
      allowed = adapter.applyConstraints(
        params,
        decision.constraints,
        callSite,
      );
      route = resolveRoute(
        router,
        decision.credentialPosture,
        adapter,
        callPath,
      );
    } catch (error) {
      // A local refusal after a healthy authorize. Without this line the journal
      // shows an authorize that succeeded and then nothing at all, which is the
      // most confusing shape a journal can have.
      log.event("refused", {
        call_site: callSite,
        error: error instanceof Error ? error.constructor.name : "unknown",
        posture: decision.credentialPosture,
      });
      throw error;
    }
    const enforcedArgs = adapter.buildEnforcedArgs(
      [allowed, ...forwardableOptions(adapter.descriptor, rest)],
      decision.paramsForced,
      decision.actToken,
      route.headers,
    );
    if (Object.keys(route.headers).length > 0)
      assertRouted(adapter, enforcedArgs, route.headers);
    log.event("enforced", {
      call_site: callSite,
      forced: Object.keys(decision.paramsForced).sort(),
      routed: Object.keys(route.headers).length > 0 || route.call !== undefined,
    });
    // `route.call` is the same endpoint on the credential's own client, for the
    // providers where a key is fixed at construction and no header switches it.
    // Either way the receiver is the object that owns the method, so provider
    // private (#) fields keep working.
    const permit: InternalCall = {
      callSite,
      // The scope of the client that sends the call: a routed call is checked
      // by the routed client's net, against the routed client's server. Every
      // routed client was given one at wrap(); none can be missing here.
      scope: route.call ? routedScopes.get(route.call.client)! : transportScope,
      token: decision.actToken,
      consumed: false,
    };
    // Boxed, so the provider's own promise comes back intact rather than
    // adopted: its response helpers are what `withResponseHelpers` forwards.
    return INTERNAL_CALL.run(permit, () => ({
      result: route.call
        ? route.call.fn.apply(route.call.receiver, enforcedArgs)
        : original.apply(this ?? fallbackReceiver, enforcedArgs),
    }));
  };

  Object.defineProperty(enforced, ENFORCED, {
    value: wrapState,
    enumerable: false,
  });
  return enforced;
}

/** What the provider method returned, kept out of promise adoption. */
interface ProviderResult {
  readonly result: unknown;
}

/**
 * The promise an enforced call returns, with the provider's response helpers on it.
 *
 * Provider methods return a promise subclass (`APIPromise`) whose
 * `withResponse()` and `asResponse()` hand back the raw HTTP response. The
 * enforced wrapper has to authorize first, so it is async, and an async
 * function returns a plain `Promise`: those two methods were gone. That broke
 * any caller using them, and it broke Anthropic's own `messages.stream()`,
 * which calls `messages.create(…).withResponse()` and failed on every call of a
 * wrapped client. Forwarding them keeps the call the provider documents
 * working, and the request still happens only once, whichever way it is read.
 */
function withResponseHelpers(
  pending: Promise<ProviderResult>,
): Promise<unknown> {
  return new EnforcedCall(pending);
}

/**
 * A promise that adopts the provider's own promise only when it is read.
 *
 * Adopting an `APIPromise` calls its `then()`, and that parses the response:
 * the body is read. Done eagerly, `asResponse()` handed back a `Response` whose
 * body was already consumed, so the one thing that helper is for — reading the
 * raw body — threw. So the provider's promise is adopted on the first `then()`,
 * as `APIPromise` itself does, and a caller using `asResponse()` alone gets the
 * body untouched. The request itself is not deferred: it left when the
 * provider method was called.
 */
class EnforcedCall extends Promise<unknown> {
  /**
   * What `Promise.prototype.then` builds for its result: a plain promise.
   *
   * Left to itself it would construct another `EnforcedCall`, whose constructor
   * takes a pending call and not an executor — so the resolve and reject
   * functions it would be handed were dropped, and the caller got
   * "Promise resolve or reject function is not callable". Nothing here needs
   * the subclass back: the value derived from this one is an ordinary promise.
   */
  static override get [Symbol.species](): PromiseConstructor {
    return Promise;
  }

  readonly #pending: Promise<ProviderResult>;
  #settled: Promise<unknown> | undefined;
  readonly #inherit: (value: unknown) => void;

  constructor(pending: Promise<ProviderResult>) {
    let inherit!: (value: unknown) => void;
    // The inherited promise state stays PENDING until something reads this one.
    // It used to be resolved with `undefined` at construction, which is what
    // `Promise.prototype.then.call(call, …)` and `util.inspect` read: they go to
    // the internal state and never to the methods below. Resolving it with the
    // real value at construction is the one thing that cannot be done — that is
    // the eager adoption this class exists to avoid — so it is resolved at the
    // first read, from `#settle()`, and what a native `then` attaches before
    // then is a reaction on a pending promise, which fires with the value.
    super((resolve) => {
      inherit = resolve;
    });
    this.#inherit = inherit;
    this.#pending = pending;
    // `Promise.prototype.then` reads `this.constructor` on its way to the
    // species, and that read is the only hook a native call gives us: it is
    // where a read made entirely through `Promise.prototype` becomes a read
    // this object knows about. It answers what it always answered.
    Object.defineProperty(this, "constructor", {
      configurable: true,
      enumerable: false,
      get: (): typeof EnforcedCall => {
        this.#settle();
        return EnforcedCall;
      },
    });
  }

  #settle(): Promise<unknown> {
    if (this.#settled === undefined) {
      // Assigned first: resolving the inherited state reads `this.constructor`
      // again, and this is what stops that from being infinite.
      this.#settled = this.#pending.then(({ result }) => result);
      // The inherited state is about to follow a promise that may reject, and
      // nobody attaches a handler to it — the methods below all hand back a
      // promise derived from `#settled`. Without this, a refusal a caller
      // catches would ALSO be reported as an unhandled rejection of the object
      // they caught it on. The rejection still reaches every caller, through
      // whichever promise they are holding.
      Promise.prototype.then.call(this, undefined, () => {});
      this.#inherit(this.#settled);
    }
    return this.#settled;
  }

  override then<TResult1 = unknown, TResult2 = never>(
    onfulfilled?: ((value: unknown) => TResult1 | PromiseLike<TResult1>) | null,
    onrejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null,
  ): Promise<TResult1 | TResult2> {
    return this.#settle().then(onfulfilled, onrejected);
  }

  override catch<TResult = never>(
    onrejected?: ((reason: unknown) => TResult | PromiseLike<TResult>) | null,
  ): Promise<unknown> {
    return this.#settle().catch(onrejected);
  }

  override finally(onfinally?: (() => void) | null): Promise<unknown> {
    return this.#settle().finally(onfinally);
  }

  withResponse(): Promise<unknown> {
    return this.#helper("withResponse");
  }

  asResponse(): Promise<unknown> {
    return this.#helper("asResponse");
  }

  #helper(name: "withResponse" | "asResponse"): Promise<unknown> {
    return this.#pending.then(({ result }) => {
      const method = (result as Record<string, unknown> | null | undefined)?.[
        name
      ];
      if (typeof method !== "function") {
        throw new TypeError(
          `${name}() is not available: the provider method did not return a response promise.`,
        );
      }
      return (method as () => unknown).call(result);
    });
  }
}

/** The public surface mirrors the Python `consent` module: `consent.wrap(client)`. */
export const consent = { wrap };
