// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Provider adapters: the descriptor-driven engine + auto-detection registry.
 *
 * Standard providers are described entirely by a JSON descriptor. Exotic
 * providers register a `ProviderAdapter` subclass to customize the mechanics
 * (Gemini ships a built-in override).
 */

import { GenericAdapter, type ProviderAdapter } from "./base.ts";
import { GeminiAdapter } from "./gemini.ts";
import { loadDescriptors, type CallSite, type ProviderDescriptor } from "../descriptors.ts";
import { UnsupportedProviderError } from "../exceptions.ts";

export { GenericAdapter } from "./base.ts";
export type { ProviderAdapter, RequestMetadata } from "./base.ts";
export { GeminiAdapter } from "./gemini.ts";

type AdapterConstructor = new (descriptor: ProviderDescriptor) => ProviderAdapter;

const DESCRIPTORS = loadDescriptors();

/** Per-provider engine overrides for exotic APIs (default: the generic engine). */
const OVERRIDES = new Map<string, AdapterConstructor>([["gemini", GeminiAdapter]]);

/**
 * Maps a JS provider client's constructor name to a descriptor. JS clients carry
 * no module path (unlike Python), so auto-detection starts from the class name
 * and falls back to structural matching against the descriptor's call sites.
 */
const CONSTRUCTOR_HINTS: Record<string, string> = {
  OpenAI: "openai",
  AzureOpenAI: "openai",
  Anthropic: "anthropic",
  AnthropicBedrock: "anthropic",
  AnthropicVertex: "anthropic",
  Mistral: "mistral",
  GoogleGenAI: "gemini",
};

function build(descriptor: ProviderDescriptor): ProviderAdapter {
  const ctor = OVERRIDES.get(descriptor.name) ?? GenericAdapter;
  return new ctor(descriptor);
}

/** True if at least one of the descriptor's call-site paths fully resolves on `client`. */
export function callSiteResolves(client: unknown, callSites: readonly CallSite[]): boolean {
  return callSites.some(({ path }) => {
    let target: unknown = client;
    for (const attr of path) {
      if (target === null || target === undefined) return false;
      if (typeof target !== "object" && typeof target !== "function") return false;
      if (!(attr in (target as object))) return false;
      target = (target as Record<string, unknown>)[attr];
    }
    return true;
  });
}

/**
 * The host a client is pointed at, or undefined if it does not say.
 *
 * `openai`'s client exposes `baseURL`; others may expose `base_url` or nothing.
 * Everything here is best-effort and failure-tolerant, because it only ever
 * *narrows* a choice.
 */
function baseUrlHost(client: unknown): string | undefined {
  if (client === null || typeof client !== "object") return undefined;
  const raw = (client as { baseURL?: unknown; base_url?: unknown }).baseURL ?? (client as { base_url?: unknown }).base_url;
  if (typeof raw !== "string" || raw.length === 0) return undefined;
  try {
    return new URL(raw).hostname.toLowerCase();
  } catch {
    return undefined; // a malformed URL is simply not a match
  }
}

/**
 * Narrow a set of same-module candidates by the base URL the client points at.
 *
 * Not a nicety. An `OpenAI` instance pointed at api.deepseek.com is an `OpenAI`
 * instance, so the class name says nothing about who receives the request — and
 * the provider name is what tells the cloud which registry rules to apply.
 * Answering "openai" for a DeepSeek call would apply OpenAI's parameters to a
 * server that never agreed to them.
 */
function narrowByBaseUrl(client: unknown, candidates: ProviderDescriptor[]): ProviderDescriptor | undefined {
  if (candidates.length <= 1) return candidates[0];

  const host = baseUrlHost(client);
  if (host !== undefined) {
    const matched = candidates.find((d) => d.clientBaseUrlHosts.includes(host));
    if (matched) return matched;
  }
  // No host matched: fall back to the descriptor claiming no host in particular
  // — the module's default. If several do, the descriptors are ambiguous and
  // guessing would be worse than refusing.
  const defaults = candidates.filter((d) => d.clientBaseUrlHosts.length === 0);
  return defaults.length === 1 ? defaults[0] : undefined;
}

/** Return the adapter matching `client`'s provider, or throw. */
export function getAdapterFor(client: unknown, providerHint?: string): ProviderAdapter {
  if (providerHint) {
    const descriptor = DESCRIPTORS.get(providerHint);
    if (!descriptor) {
      throw new UnsupportedProviderError(`Unknown provider '${providerHint}'.`);
    }
    return build(descriptor);
  }

  const constructorName = (client as { constructor?: { name?: string } })?.constructor?.name;
  const hintedName = constructorName ? CONSTRUCTOR_HINTS[constructorName] : undefined;
  if (hintedName) {
    // The hint names a client library, not a provider: several descriptors may
    // claim it. `openai` means OpenAI *or* any OpenAI-compatible service.
    const sameModule = [...DESCRIPTORS.values()].filter(
      (d) => d.clientModule === DESCRIPTORS.get(hintedName)?.clientModule,
    );
    const chosen = narrowByBaseUrl(client, sameModule);
    if (chosen) return build(chosen);
  }

  const structural = narrowByBaseUrl(
    client,
    [...DESCRIPTORS.values()].filter((d) => callSiteResolves(client, d.callSites)),
  );
  if (structural) return build(structural);

  throw new UnsupportedProviderError(
    `No ID Side adapter for the given client${constructorName ? ` (constructor '${constructorName}')` : ""}. ` +
      "Pass { provider } to consent.wrap(), or register a custom adapter with registerAdapter().",
  );
}

/** Register a `ProviderAdapter` subclass to override the generic engine for `name`. */
export function registerAdapter(name: string, adapterClass: AdapterConstructor): void {
  OVERRIDES.set(name, adapterClass);
}
