// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Descriptor-driven provider adapter — the injection engine.
 *
 * The adapter handles only the *mechanics* (which call to intercept, where to
 * inject the cloud-decided params, which header carries the ACT token). The
 * *policy* (which params to force) is decided by the cloud and passed in as
 * `paramsForced`. Subclass and override for providers whose API does not fit the
 * declarative model (see the Gemini adapter).
 */

import { CanonicalizationError } from "../jcs.ts";
import { requestHash } from "../requestHash.ts";
import { ConsentTokenError, ToolUseRefusedError } from "../exceptions.ts";
import type { ProviderDescriptor, ToolField } from "../descriptors.ts";

export interface RequestMetadata {
  readonly model: string;
  readonly requestHash: string;
}

export interface ProviderAdapter {
  readonly descriptor: ProviderDescriptor;
  readonly name: string;
  extractRequestMetadata(params: Record<string, unknown>): RequestMetadata;
  /**
   * Honour the cloud's non-parameter constraints on this call, returning the
   * request body to send — or throwing if the call must not happen at all.
   *
   * `callSite` is the id of the endpoint being called, which decides where
   * tools can travel on it. `consent.wrap()` always passes it. Without it, an
   * adapter must read every call site's tool fields: reading fewer is how a
   * refusal gets past.
   */
  applyConstraints(
    params: Record<string, unknown>,
    constraints: Record<string, unknown>,
    callSite?: string,
  ): Record<string, unknown>;
  /**
   * Build the argument list for the real provider call. `args` are the caller's
   * original arguments, with the identity already stripped from the first one
   * (the request body). Returns the arguments to spread into the method.
   *
   * `credentialHeaders` selects which of the developer's credentials carries
   * this call (OpenAI's `OpenAI-Project`). An adapter MUST forward it: routing
   * is what makes an account-level opt-out per-user, and dropping it would send
   * the call on the very credential the user's answer excluded. `consent.wrap()`
   * verifies the header landed and refuses the call otherwise, so an adapter
   * that ignores it fails loudly rather than quietly.
   */
  buildEnforcedArgs(
    args: unknown[],
    paramsForced: Record<string, unknown>,
    token: string,
    credentialHeaders?: Record<string, string>,
  ): unknown[];
}

/** A plain-object view of `value`, or undefined if it is not one. */
function asMapping(value: unknown): Record<string, unknown> | undefined {
  if (isPlainObject(value)) return value;
  if (value !== null && typeof value === "object") {
    const toJSON = (value as { toJSON?: unknown }).toJSON;
    if (typeof toJSON === "function") {
      const dumped = (value as { toJSON(): unknown }).toJSON();
      if (isPlainObject(dumped)) return dumped;
    }
  }
  return undefined;
}

/** Read a nested call argument, or undefined if any step is missing. */
function readPath(params: Record<string, unknown>, path: readonly string[]): unknown {
  let current: unknown = params;
  for (const attr of path) {
    const mapping = asMapping(current);
    if (mapping === undefined || !(attr in mapping)) return undefined;
    current = mapping[attr];
  }
  return current;
}

/**
 * A copy of `params` without the argument at `path`.
 *
 * Removes the key rather than emptying it: several providers reject an empty
 * `tools` list, and "no tools" is what the user asked for, not "zero tools".
 * Never mutates the caller's objects — a wrapped client must not rewrite the
 * object the application still holds a reference to.
 */
function dropPath(params: Record<string, unknown>, path: readonly string[]): Record<string, unknown> {
  const [head, ...rest] = path;
  if (head === undefined || !(head in params)) return params;

  const next = { ...params };
  if (rest.length === 0) {
    delete next[head];
    return next;
  }
  const nested = asMapping(next[head]);
  if (nested !== undefined) next[head] = dropPath(nested, rest);
  return next;
}

/**
 * Whether one argument hands the model a tool, by the kind of argument it is.
 *
 * - `definitions`: a list. An empty list or an empty object carries nothing;
 *   any other value does, a falsy one included (`false`, `0`, `""`). The same
 *   rule in both SDKs, and the safe way round: only what is clearly nothing is
 *   nothing.
 * - `enabler`: anything that is there. `web_search_options: {}` is enough to
 *   switch search on, and `undefined` is how this language says "not given".
 * - `input_items`: an array with at least one item of a tool-bearing type. A
 *   string input has no items at all. An item that names no type but an `id`
 *   is a reference to a stored item: OpenAI's `ItemReference` makes `type`
 *   optional, so `{ id: "fc_…" }` alone is an `item_reference`, and reading the
 *   declared type only let it past a refusal naming one.
 */
function carriesTools(value: unknown, field: ToolField): boolean {
  if (value === undefined || value === null) return false;
  switch (field.kind) {
    case "definitions":
      if (Array.isArray(value)) return value.length > 0;
      return !(isPlainObject(value) && Object.keys(value).length === 0);
    case "enabler":
      return true;
    case "input_items":
      return (
        Array.isArray(value) &&
        value.some((item: unknown) => {
          const mapping = asMapping(item);
          const type = mapping?.type ?? (mapping?.id == null ? undefined : "item_reference");
          return typeof type === "string" && field.itemTypes.includes(type);
        })
      );
  }
}

/** True for a plain object (not null, array, or class instance). */
export function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/** Coerce any HeadersLike (plain object, Headers instance, or [k, v] tuples) to a plain object. */
function headersToObject(value: unknown): Record<string, unknown> {
  if (value instanceof Headers) return Object.fromEntries(value.entries());
  if (Array.isArray(value)) return Object.fromEntries(value as Iterable<readonly [PropertyKey, unknown]>);
  return isPlainObject(value) ? { ...value } : {};
}

/**
 * Merge `headers` into a caller-supplied per-request options object (the 2nd
 * argument of openai/anthropic/mistral calls), under `options.headers`. Returns a
 * fresh object; never mutates the caller's. The caller's headers may be a plain
 * object, a Headers instance, or an array of tuples — all are preserved.
 */
function mergeHeaders(current: unknown, injected: Record<string, unknown>): Record<string, unknown> {
  const merged = headersToObject(current);
  const replaced = new Set(Object.keys(injected).map((name) => name.toLowerCase()));
  for (const name of Object.keys(merged)) {
    if (replaced.has(name.toLowerCase())) delete merged[name];
  }
  return { ...merged, ...injected };
}

function withOptionHeaders(options: unknown, headers: Record<string, unknown>): Record<string, unknown> {
  const base = isPlainObject(options) ? { ...options } : {};
  base.headers = mergeHeaders(base.headers, headers);
  return base;
}

const JSON_SCALAR = new Set(["boolean", "number", "string"]);

/**
 * Return a JSON-compatible copy of `value`, or throw `ConsentTokenError`.
 *
 * Strict by design: primitives pass, arrays and plain objects recurse, objects
 * exposing `toJSON()` are converted, anything else is an error. A silent
 * fallback (`String(value)`) would embed engine-dependent text into the request
 * hash and destroy its reproducibility across SDKs.
 */
function normalize(value: unknown, path: string): unknown {
  if (value === null) return null;

  const type = typeof value;
  if (JSON_SCALAR.has(type)) return value;

  if (Array.isArray(value)) {
    // JSON.stringify turns array holes / undefined into null.
    return value.map((item, i) => (item === undefined ? null : normalize(item, `${path}[${i}]`)));
  }

  if (type === "object") {
    const toJSON = (value as { toJSON?: unknown }).toJSON;
    if (typeof toJSON === "function") {
      return normalize((value as { toJSON(): unknown }).toJSON(), path);
    }
    const proto = Object.getPrototypeOf(value);
    if (proto === Object.prototype || proto === null) {
      const out: Record<string, unknown> = {};
      for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
        if (item === undefined) continue; // JSON drops undefined-valued keys
        out[key] = normalize(item, `${path}.${key}`);
      }
      return out;
    }
    const name = (value as { constructor?: { name?: string } }).constructor?.name ?? "object";
    throw new ConsentTokenError(
      `Cannot compute a deterministic request_hash: value of type '${name}' at '${path}' is not ` +
        "JSON-serializable. Pass JSON-compatible values or objects exposing toJSON().",
    );
  }

  // bigint, undefined, function, symbol
  throw new ConsentTokenError(
    `Cannot compute a deterministic request_hash: value of type '${type}' at '${path}' is not ` +
      "JSON-serializable. Pass JSON-compatible values or objects exposing toJSON().",
  );
}

/** Drop the descriptor's protected keys from the cloud-forced params. */
function filterProtected(
  paramsForced: Record<string, unknown>,
  protectedParams: ReadonlySet<string>,
): Record<string, unknown> {
  const safe: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(paramsForced)) {
    if (!protectedParams.has(key)) safe[key] = value;
  }
  return safe;
}

/** Generic engine that enforces consent on a provider call, driven by a descriptor. */
export class GenericAdapter implements ProviderAdapter {
  readonly descriptor: ProviderDescriptor;
  // Derived once from the immutable descriptor, not per call.
  protected readonly protectedParams: ReadonlySet<string>;
  readonly #ignored: ReadonlySet<string>;

  constructor(descriptor: ProviderDescriptor) {
    this.descriptor = descriptor;
    this.protectedParams = new Set(descriptor.protectedParams);
    this.#ignored = new Set([descriptor.identityParam, descriptor.ubtParam, descriptor.headersKwarg]);
  }

  get name(): string {
    return this.descriptor.name;
  }

  /**
   * The model and the deterministic request hash for one call. The hash is
   * `sha256:<hex>` over the RFC 8785 (JCS) canonical form of the caller's
   * pre-enforcement params, minus both identity parameters and per-request
   * headers. Forced params, constraint transformations, routed credentials and
   * the ACT header are applied after this hash. Shared vectors pin it across
   * SDK languages.
   */
  extractRequestMetadata(params: Record<string, unknown>): RequestMetadata {
    const payload: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(params)) {
      if (this.#ignored.has(key) || value === undefined) continue;
      payload[key] = normalize(value, key);
    }

    let hash: string;
    try {
      // Single source of the hash formula (shared with the request-hash vectors).
      hash = requestHash(payload);
    } catch (error) {
      if (error instanceof CanonicalizationError) {
        throw new ConsentTokenError(`Cannot compute a deterministic request_hash: ${error.message}`);
      }
      throw error;
    }

    const model = params.model == null ? "" : String(params.model);
    return { model, requestHash: hash };
  }

  /**
   * Honour the cloud's non-parameter constraints on this call.
   *
   * A constraint is not a forced parameter and must not be one: forcing a value
   * writes the developer's request, which the cloud is never allowed to do (see
   * `protectedParams`). `tools` is the first and only constraint — the mechanism
   * the descriptors call `refuse_or_strip_tools`.
   *
   * Note what is NOT sent to the cloud to make this work: nothing. The cloud
   * returns the constraint from the user's consent state alone and never learns
   * whether this particular call carried tools. That question is answered here,
   * on the caller's own machine, because answering it anywhere else would mean
   * telling us something about the request — and the whole architecture rests on
   * our not knowing.
   */
  applyConstraints(
    params: Record<string, unknown>,
    constraints: Record<string, unknown>,
    callSite?: string,
  ): Record<string, unknown> {
    // `constraints ?? {}`: TypeScript makes this field mandatory, but a plain
    // JavaScript consumer can inject their own ConsentClient — the documented
    // way to test without the network — and the type system never sees it. An
    // older stand-in that predates constraints must degrade to "no constraint",
    // not to a TypeError three frames inside the SDK.
    const action = (constraints ?? {}).tools;
    if (!action) return params;

    if (action !== "refuse" && action !== "strip") {
      // An unknown instruction is not "no instruction". Guessing would be the
      // one failure mode this whole path exists to prevent.
      throw new ConsentTokenError(
        `ID Side cloud returned an unknown tools constraint '${String(action)}'; the AI call was not made. ` +
          "Upgrade the @idside/sdk package.",
      );
    }

    const path = this.descriptor.toolsParam;
    if (path === undefined) {
      throw new ConsentTokenError(
        `Agentic tools are not accepted for this user, but the '${this.descriptor.name}' descriptor does not say where ` +
          "tools travel on a call, so the SDK cannot check. The AI call was not made — a refusal it cannot " +
          "apply is not a refusal it may ignore.",
      );
    }

    // `tools` is one way in among several: `functions`, hosted search, an MCP
    // server, an agent, a cache, a stored conversation. The provider-wide key
    // first, then every field this endpoint declares, in the descriptor's order,
    // so the message names the same field every time.
    const fields: ToolField[] = [{ path, kind: "definitions", itemTypes: [] }, ...this.#toolFieldsOf(callSite)];
    const carried = fields.filter((field) => carriesTools(readPath(params, field.path), field));
    if (carried.length === 0) return params; // no tools on this call: nothing to refuse

    // One message for whoever refused — the person, nobody asking them, or an
    // organization's policy, which no answer lifts — because the SDK does not
    // know which, and must not. Python's is the same, word for word.
    const named = (found: readonly ToolField[]): string =>
      found.map((field) => `\`${field.path.join(".")}\``).join(", ");
    const refused =
      "Agentic tools are not accepted for this user (refused by the person, never asked, or refused by a policy)";
    // "Without them": the fields named, which are not always `tools` — told
    // "without the tools" about `conversation`, a developer drops `tools`,
    // keeps the reference and is refused again.
    const remedy = "The AI call was not made. Retry without them; never retry the same call, it will be refused again.";
    if (action === "refuse") {
      throw new ToolUseRefusedError(`${refused}, and this call passes ${named(carried)}. ${remedy}`);
    }

    // Stripping removes definitions. It cannot remove a reference to an agent,
    // a cache or a conversation: the call would still run, asking something
    // other than what was written, and nothing would say so.
    const kept = carried.filter((field) => field.kind !== "definitions");
    if (kept.length > 0) {
      throw new ToolUseRefusedError(
        `${refused}, and this call passes ${named(kept)}, which can bring tools the SDK cannot see or remove ` +
          `without changing what the call asks. ${remedy}`,
      );
    }
    let allowed = params;
    for (const field of carried) allowed = dropPath(allowed, field.path);
    return allowed;
  }

  /**
   * The tool fields of one endpoint. For an unknown or unnamed one, every
   * endpoint's fields, because a refusal that reads too few fields lets tools
   * through.
   */
  #toolFieldsOf(callSite: string | undefined): readonly ToolField[] {
    const site = this.descriptor.callSites.find((candidate) => candidate.id === callSite);
    if (site !== undefined) return site.toolFields;
    const seen = new Set<string>();
    return this.descriptor.callSites
      .flatMap((candidate) => candidate.toolFields)
      .filter((field) => {
        const key = `${field.kind}\u0000${field.path.join("\u0000")}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
  }

  /**
   * openai / anthropic / mistral all take `(body, options?)`, with per-request
   * HTTP headers under `options.headers`. So the ACT token (and, when
   * `paramTarget` is "headers", the forced params) go into the 2nd argument;
   * "kwargs" forced params go into the body. `protectedParams` are dropped from
   * the forced set. Does not mutate the caller's arguments.
   *
   * `credentialHeaders` is applied last: it is decided from the developer's own
   * configuration, so nothing the cloud forces may overwrite it.
   */
  buildEnforcedArgs(
    args: unknown[],
    paramsForced: Record<string, unknown>,
    token: string,
    credentialHeaders?: Record<string, string>,
  ): unknown[] {
    const [body, options, ...tail] = args;
    const safe = filterProtected(paramsForced, this.protectedParams);

    const newBody: Record<string, unknown> = { ...(isPlainObject(body) ? body : {}) };
    const injectedHeaders: Record<string, unknown> = {};
    if (this.descriptor.paramTarget === "headers") {
      Object.assign(injectedHeaders, safe);
    } else {
      Object.assign(newBody, safe);
    }
    injectedHeaders[this.descriptor.tokenHeader] = token;
    if (credentialHeaders) Object.assign(injectedHeaders, credentialHeaders);

    return [newBody, withOptionHeaders(options, injectedHeaders), ...tail];
  }
}

export { asMapping, dropPath, filterProtected, headersToObject, mergeHeaders };
