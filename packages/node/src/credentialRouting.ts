// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Credential routing: which of the developer's credentials carries this call.
 *
 * No provider lets a developer switch training off per request. The setting
 * lives on the credential — per project at OpenAI and Gemini, per workspace at
 * Anthropic, per organisation at Mistral — so one key means one posture for
 * every user of the app at once. A developer who provisions one credential per
 * posture lets the SDK send each call on the one matching that user's answer,
 * and the account control becomes a personal one. It is the only route by which
 * a training opt-out can be honoured per user today.
 *
 * What this module does and does not decide:
 *
 * - it does NOT decide which posture a consent state calls for. That is policy,
 *   it lives in the cloud, and the SDK is told the answer (`credentialPosture`
 *   on the authorize response). There is no consent→credential table here, any
 *   more than there is a consent→params one;
 * - it does NOT let the developer choose either. They label what they hold; the
 *   label is matched against the cloud's answer. A callback would hand them the
 *   user's preferences, which is invariant #1;
 * - it fails closed. A posture nobody provisioned stops the call, because the
 *   alternative is sending it on a credential the user's answer excludes, with
 *   nothing anywhere to say so.
 *
 * Mirrors packages/python/src/idside/_routing.py.
 */

import { CredentialRoutingError, MissingCredentialError, UnsupportedProviderError } from "./exceptions.ts";
import { getAdapterFor } from "./adapters/index.ts";
import type { ProviderDescriptor } from "./descriptors.ts";

/**
 * Fallback shape for a routed header value when the descriptor pins no pattern:
 * printable ASCII, no space, no control character. Narrow, but not optional —
 * the value is written into an outgoing HTTP header, and a CR or LF in it is
 * header injection on the developer's own provider request.
 */
const SAFE_HEADER_VALUE = /^[!-~]{1,200}$/;

type CallMethod = (this: unknown, ...args: unknown[]) => unknown;

/**
 * Key for the (posture, endpoint) pair. NUL-separated because a posture is a
 * label a developer chooses: any printable separator could turn up inside one,
 * and two pairs colliding would route a call to the wrong endpoint.
 *
 * Keyed by the METHOD PATH, not by the call site id, because an alias shares its
 * call site's id by design — `chat.completions.parse` is `chat.completions.create`
 * on the wire. Keying by id would make the two collide, and a routed `parse()`
 * would land on the alternative client's `create()` and hand back an unparsed
 * response. Mirrors the Python SDK's `route(posture, call_path)`.
 */
function routeKey(posture: string, callPath: readonly string[]): string {
  return [posture, ...callPath].join("\u0000");
}

/** One endpoint the wrapped client exposes, as `wrap()` resolved it. */
export interface RoutableCallSite {
  readonly id: string;
  readonly path: readonly string[];
}

/**
 * Where one call must go once the cloud has named a posture.
 *
 * Exactly one mechanism applies, and which one is a property of the provider,
 * not of the call: `headers` for a provider that validates a per-request
 * credential header (OpenAI's `OpenAI-Project`), `call` for everyone else, where
 * the key is fixed at client construction and switching posture means switching
 * client. A route with neither is the ordinary case: the cloud named no posture,
 * and nothing about the call changes.
 *
 * `call` carries its receiver. Unlike Python, where an attribute lookup yields a
 * bound method, a JS method detached from its object loses `this` — and provider
 * clients keep their transport on it (`this._client`), so a detached call would
 * throw rather than route.
 *
 * It also carries the client it belongs to, as the developer passed it: that
 * client's transport sends the call, so its net is the one the call must satisfy.
 */
export interface Route {
  readonly headers: Record<string, string>;
  readonly call?: RoutedCall;
}

export interface RoutedCall {
  readonly fn: CallMethod;
  readonly receiver: object;
  readonly client: object;
}

/** The unrouted call: no header to add, and the client the developer wrapped. */
export const NO_ROUTE: Route = { headers: {} };

/** Resolve an attribute path to a method and the object that owns it. */
function resolveMethod(client: unknown, path: readonly string[]): { fn: CallMethod; receiver: object } | undefined {
  let receiver: unknown = client;
  for (let i = 0; i < path.length - 1; i++) {
    if (receiver === null || (typeof receiver !== "object" && typeof receiver !== "function")) return undefined;
    if (!(path[i]! in (receiver as object))) return undefined;
    receiver = (receiver as Record<string, unknown>)[path[i]!];
  }
  const name = path[path.length - 1]!;
  if (receiver === null || (typeof receiver !== "object" && typeof receiver !== "function")) return undefined;
  const fn = (receiver as Record<string, unknown>)[name];
  return typeof fn === "function" ? { fn: fn as CallMethod, receiver: receiver as object } : undefined;
}

/**
 * Say what is missing, in the provider's own vocabulary.
 *
 * Names the labels that do exist and never a credential: the labels are the
 * developer's own words for their configuration, the credentials are secrets.
 */
export function missingMessage(
  descriptor: ProviderDescriptor,
  posture: string,
  provisioned: readonly string[],
): string {
  const unit = descriptor.credentialRouting?.unit ?? "credential";
  const have = provisioned.length ? provisioned.map((name) => `'${name}'`).join(", ") : "none";
  return (
    `This user's consent requires the '${posture}' credential for '${descriptor.name}', and ` +
    `consent.wrap() was given none carrying it (provisioned: ${have}). The AI call was not made: ` +
    `sending it on another ${unit} would place this user under a privacy regime they declined, and ` +
    `nothing would record that it happened. Provision a ${unit} with that posture and pass it to ` +
    `consent.wrap() as credentials: { ${posture}: … }.`
  );
}

/**
 * The developer's labelled credentials, resolved once at `wrap()` time.
 *
 * Everything that can be checked early is checked here rather than on the AI
 * request path: an unroutable provider, a bare identifier where only a client
 * will do, an identifier of the wrong shape, an alternative client missing the
 * endpoint being wrapped. A developer meets those at startup, not in production,
 * and never as a call that quietly went out on the wrong account.
 */
export class CredentialRouter {
  /** Sorted, so two processes with the same configuration send the same payload. */
  readonly postures: readonly string[];
  readonly #descriptor: ProviderDescriptor;
  readonly #unit: string;
  readonly #header: string | undefined;
  readonly #pattern: RegExp | undefined;
  readonly #headers = new Map<string, Record<string, string>>();
  readonly #calls = new Map<string, RoutedCall>();

  /**
   * @param callSites the endpoints `wrap()` is about to patch. An alternative
   *   client must expose every one of them, or a call routed to it would find
   *   nothing to call.
   * @param isEnforced recognises a method this SDK has already patched. Injected
   *   so the enforcement marker stays private to `consent.ts`; routing to an
   *   already-wrapped client would authorize twice and stack two ACT tokens on
   *   one request.
   */
  constructor(
    descriptor: ProviderDescriptor,
    credentials: Record<string, unknown>,
    callSites: readonly RoutableCallSite[],
    isEnforced: (method: unknown) => boolean = () => false,
  ) {
    if (credentials === null || typeof credentials !== "object" || Array.isArray(credentials)) {
      throw new CredentialRoutingError(
        "consent.wrap({ credentials }) takes an object mapping posture label → credential, not " +
          `${Array.isArray(credentials) ? "an array" : typeof credentials}. The label is what the ID Side ` +
          "cloud names when it routes a call; a bare list of credentials would carry no posture at all.",
      );
    }
    const routing = descriptor.credentialRouting;
    if (routing === undefined) {
      throw new CredentialRoutingError(
        `consent.wrap() was given credentials for '${descriptor.name}', which declares no credential ` +
          "routing: it exposes no account-level privacy posture a second credential could carry, so " +
          "routing between two of them would change nothing for the user. Remove the credentials option.",
      );
    }

    this.#descriptor = descriptor;
    this.#unit = routing.unit;
    this.#header = routing.header;
    // Anchored so `test()` means the whole value, matching Python's fullmatch:
    // both SDKs must accept and refuse exactly the same credentials.
    this.#pattern = routing.headerValuePattern
      ? new RegExp("^(?:" + routing.headerValuePattern + ")$")
      : undefined;

    for (const [posture, credential] of Object.entries(credentials)) {
      if (posture.trim() === "") {
        throw new CredentialRoutingError(
          "Every credential passed to consent.wrap() must be labelled with the posture it carries, and " +
            "an empty label is not a usable one. The label is what the ID Side cloud names when it routes " +
            "a call, so it must match the one configured for this app in the dashboard.",
        );
      }
      if (typeof credential === "string") {
        this.#headers.set(posture, { [this.#headerName(posture)]: this.#checkedValue(credential, posture) });
      } else {
        this.#addClient(posture, credential, callSites, isEnforced);
      }
    }

    this.postures = Object.keys(credentials).sort();
  }

  // ── configuration ────────────────────────────────────────────────────────

  /** The routing header, or explain why a string cannot work here. */
  #headerName(posture: string): string {
    if (this.#header) return this.#header;
    throw new CredentialRoutingError(
      `The credential labelled '${posture}' is a bare identifier, but '${this.#descriptor.name}' reads no ` +
        `per-request header naming a ${this.#unit}: its key is chosen when the client is built. Pass a ` +
        `second client constructed with that ${this.#unit}'s key instead — a string here would be accepted ` +
        "and never sent, which is the one outcome routing exists to prevent.",
    );
  }

  /** Validate a routed header value; never echo it back on failure. */
  #checkedValue(value: string, posture: string): string {
    const ok = this.#pattern ? this.#pattern.test(value) : SAFE_HEADER_VALUE.test(value);
    if (ok) return value;
    const expected = this.#pattern ? ` (expected ${this.#pattern.source})` : "";
    throw new CredentialRoutingError(
      `The credential labelled '${posture}' is not a ${this.#descriptor.name} ${this.#unit} ` +
        `identifier${expected}. The value is not repeated here on purpose: if what you pasted is an API ` +
        "key, it is a secret, and it was about to travel in a header that is not the one that carries it.",
    );
  }

  /** Resolve every wrapped endpoint on an alternative provider client. */
  #addClient(
    posture: string,
    credential: unknown,
    callSites: readonly RoutableCallSite[],
    isEnforced: (method: unknown) => boolean,
  ): void {
    this.#checkSameProvider(posture, credential);
    for (const site of callSites) {
      const resolved = resolveMethod(credential, site.path);
      if (resolved === undefined) {
        throw new CredentialRoutingError(
          `The client passed as credential '${posture}' does not expose '${site.path.join(".")}', which ` +
            "consent.wrap() is enforcing on the client you wrapped. A call routed to it would have nothing " +
            `to call. Pass a client of the same kind, built with that ${this.#unit}'s key.`,
        );
      }
      if (isEnforced(resolved.fn)) {
        throw new CredentialRoutingError(
          `The client passed as credential '${posture}' is already wrapped by consent.wrap(). Routing a ` +
            "call to it would authorize the same request twice and stack two ACT tokens on it. Pass the " +
            "unwrapped client.",
        );
      }
      this.#calls.set(routeKey(posture, site.path), { ...resolved, client: credential as object });
    }
  }

  /**
   * Refuse a routed client that belongs to a different provider.
   *
   * Best effort, and it only ever refuses on a positive identification. It
   * matters because the shapes coincide: an `OpenAI` client pointed at another
   * vendor's base URL is still an `OpenAI` client, so every call site resolves
   * and the only thing that differs is which company receives the prompt.
   */
  #checkSameProvider(posture: string, credential: unknown): void {
    let detected: string;
    try {
      detected = getAdapterFor(credential).name;
    } catch (error) {
      if (error instanceof UnsupportedProviderError) return; // unidentifiable proves nothing
      throw error;
    }
    if (detected !== this.#descriptor.name) {
      throw new CredentialRoutingError(
        `The client passed as credential '${posture}' is a '${detected}' client, but consent.wrap() is ` +
          `enforcing '${this.#descriptor.name}'. Routing between two providers would send the request to ` +
          "a company the user's answer never mentioned.",
      );
    }
  }

  // ── the request path ─────────────────────────────────────────────────────

  /**
   * The route for `posture`, or refuse the call. Called once per AI request,
   * after the cloud has named the posture this user's answer requires.
   */
  route(posture: string, callPath: readonly string[]): Route {
    const headers = this.#headers.get(posture);
    if (headers !== undefined) return { headers };
    const call = this.#calls.get(routeKey(posture, callPath));
    if (call !== undefined) return { headers: {}, call };
    throw new MissingCredentialError(missingMessage(this.#descriptor, posture, this.postures), {
      posture,
      provider: this.#descriptor.name,
      provisioned: this.postures,
    });
  }
}
