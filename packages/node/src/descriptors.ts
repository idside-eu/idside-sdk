// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Provider descriptors, loaded from the bundled JSON files.
 *
 * A descriptor declares the *mechanics* of a provider (which call to intercept,
 * where to inject the cloud-decided params, which header carries the ACT token).
 * The *policy* (which params to force for a given consent state) is decided by
 * the cloud and returned as `params_forced` — it does not live here.
 *
 * The JSON files in `providers/` are byte-identical copies of
 * `packages/descriptors/descriptors/` (the single source of truth shared with
 * the Python SDK and the cloud); a sync test keeps them identical.
 */

import openai from "./providers/openai.json" with { type: "json" };
import anthropic from "./providers/anthropic.json" with { type: "json" };
import gemini from "./providers/gemini.json" with { type: "json" };
import mistral from "./providers/mistral.json" with { type: "json" };
import deepseek from "./providers/deepseek.json" with { type: "json" };

export const DEFAULT_CONSENT_HEADER = "X-AI-Consent-Token";

/**
 * One argument through which a call can hand the model a tool.
 *
 * A `tools` constraint is only as wide as what the SDK reads. Reading the
 * provider's `tools` key alone let `functions`, `web_search_options`, a Mistral
 * agent, a Gemini cache or a stored OpenAI conversation carry tools straight
 * past a refusal — so each call site names every such argument.
 *
 * `kind` decides how a refusal is applied:
 * - `definitions` — a list of tool definitions. An empty list carries none;
 *   under `strip` the key is removed.
 * - `enabler` — any present, non-null value can bring tools with it: an object
 *   switching a hosted tool on, or a reference to a server-side object the SDK
 *   cannot see into. Removing it would change what the call means, so it is
 *   refused under `strip` as under `refuse`.
 * - `input_items` — an array whose items of the listed `itemTypes` carry tool
 *   definitions. Refused under both, for the same reason.
 *
 * `path` is already in this client's spelling: the descriptor's `js_path` when
 * it declares one (`agentId`, `cachedContent`), its Python `path` otherwise.
 */
export interface ToolField {
  readonly path: readonly string[];
  readonly kind: "definitions" | "enabler" | "input_items";
  readonly itemTypes: readonly string[];
}

/**
 * One endpoint to intercept, and what may be forced *on that endpoint*.
 *
 * A provider's endpoints do not accept the same parameters: OpenAI's
 * `background` exists on `/v1/responses` and nowhere else, so a provider-wide
 * allowlist could only ever be the intersection (losing the lever) or the union
 * (breaking `chat.completions` for exactly the users who opted out).
 *
 * `id` is the stable, language-neutral name sent to the cloud as `call_site`.
 * It is written in the Python client's spelling because the descriptor is
 * shared — `path` is what differs between languages, never the id.
 */
export interface CallSite {
  readonly id: string;
  readonly path: readonly string[];
  /**
   * Extra method names on the SAME target object that hit the SAME endpoint —
   * `chat.completions.parse` next to `chat.completions.create`. They are patched
   * with this call site's `id`, so the cloud contract does not grow: one
   * endpoint keeps one name on the wire whichever method the developer called.
   *
   * Both client languages' spellings may be listed; whichever exists is patched,
   * and an absent one is skipped exactly like an absent call site.
   */
  readonly aliases: readonly string[];
  readonly forceableParams: readonly string[];
  /** Every argument of THIS endpoint that can hand the model a tool, besides the provider-wide `toolsParam`. */
  readonly toolFields: readonly ToolField[];
}

// A call site may also declare `resuming_params` — the arguments that mean a
// call replays a result instead of creating one. This SDK does not read them,
// and does not need to: the overloaded helper they describe is `stream()`, which
// Node never patches, and its resuming form calls `responses.retrieve()`, which
// is not a call site at all. Its GET passes the net like any other retrieval.
// `test/streamingHelpers.test.ts` pins that outcome rather than assuming it.

/**
 * How this provider lets one app hold several privacy postures at once.
 *
 * The posture (does this traffic train the model, is it retained, in which
 * region) is not a request parameter anywhere — it is a property of the
 * credential. `unit` names what the credential selects: a *project* at OpenAI
 * and Gemini, a *workspace* at Anthropic, an *organisation* at Mistral.
 *
 * `header` is the per-request header that selects it, when the provider
 * validates one — only OpenAI does (`OpenAI-Project`). Without it, switching
 * posture means switching client, because a key is chosen at construction.
 *
 * `headerValuePattern` is checked before any value reaches an outgoing request:
 * it keeps CR/LF out of a header, and refuses a value of the wrong kind — an
 * OpenAI project id is `proj_…` where an API key is `sk-…`, and the developer
 * who pastes the second must be stopped before the secret leaves the process.
 */
export interface CredentialRouting {
  readonly header?: string;
  readonly headerValuePattern?: string;
  readonly unit: string;
}

export interface ProviderDescriptor {
  readonly name: string;
  readonly clientModule: string;
  /**
   * Hosts identifying THIS provider when several descriptors share a
   * `clientModule`. OpenAI-compatible providers (DeepSeek, and Groq/xAI/Together
   * when they get descriptors) are all reached through the `openai` client, so
   * the class name proves nothing — only the base URL says who is on the other
   * end. Empty means "the default for this module".
   */
  readonly clientBaseUrlHosts: readonly string[];
  readonly callSites: readonly CallSite[];
  /** Kwarg naming the developer-asserted end-user identity (consumed, never forwarded, hash-excluded). */
  readonly identityParam: string;
  /** Kwarg carrying the widget-issued User Binding Token (exclusive with identityParam). */
  readonly ubtParam: string;
  /** HTTP header that carries the signed ACT token on the provider call. */
  readonly tokenHeader: string;
  /** Where cloud-forced params are injected: as call kwargs, or as HTTP headers. */
  readonly paramTarget: "kwargs" | "headers";
  /** Kwarg through which the provider's client accepts per-request HTTP headers. */
  readonly headersKwarg: string;
  /** Core request fields the cloud may never override. */
  readonly protectedParams: readonly string[];
  /**
   * Path to the argument carrying agentic tool definitions on a call
   * (`["tools"]`, or `["config", "tools"]` for google-genai, which takes no
   * free-form kwargs). Read — never written — to honour a `tools` constraint
   * from the cloud. `undefined` means the SDK cannot see this provider's tools,
   * and so cannot honour it.
   */
  readonly toolsParam?: readonly string[];
  /**
   * Headers a caller may never set on an enforced call, as declared: the ones
   * that choose which credential, account or project the call goes out on.
   * Compare through `isReservedHeader`, which is case-insensitive and always
   * reserves `tokenHeader` as well.
   */
  readonly reservedHeaders: readonly string[];
  /**
   * How several credentials with different privacy postures can be held for this
   * provider, or `undefined` when it offers no such thing — in which case
   * `consent.wrap({ credentials })` is refused rather than quietly ignored.
   */
  readonly credentialRouting?: CredentialRouting;
  readonly descriptorVersion: string;
}

interface RawToolField {
  path: string[];
  js_path?: string[];
  kind: string;
  item_types?: string[];
  note?: string;
}

interface RawCallSite {
  id: string;
  path: string[];
  aliases?: string[];
  forceable_params?: string[];
  tool_fields?: RawToolField[];
  note?: string;
}

interface RawDescriptor {
  descriptor_version?: string;
  name: string;
  client_module: string;
  client_base_url_hosts?: string[];
  call_sites: RawCallSite[];
  identity_param?: string;
  ubt_param?: string;
  token_header?: string;
  param_target?: string;
  headers_kwarg?: string;
  protected_params?: string[];
  tools_param?: string[];
  reserved_headers?: string[];
  credential_routing?: {
    header?: string;
    header_value_pattern?: string;
    unit?: string;
    note?: string;
  };
  notes?: string;
}

/**
 * Paths for endpoints whose JS client method names differ from the Python ones
 * the shared descriptor records. The JSON stays the Python source of truth
 * (byte-identical across SDKs); only the in-memory path is adapted, keyed by the
 * call site's `id` so that the endpoint keeps ONE name on the wire in both SDKs.
 * So far only google-genai differs (`generateContent` vs `generate_content`).
 *
 * An id here that no descriptor declares would silently leave that endpoint
 * unenforced, so `descriptorsSync.test.ts` fails the build on an unknown key.
 */
export const JS_CALL_SITE_PATHS: Record<string, Record<string, string[]>> = {
  gemini: {
    "models.generate_content": ["models", "generateContent"],
    "models.generate_content_stream": ["models", "generateContentStream"],
  },
};

function toolFieldFromRaw(raw: RawToolField): ToolField {
  // A kind this SDK does not know yet is read as an enabler, as Python reads it:
  // a tool field nobody taught it to read is not one it may assume empty, so
  // any value given there refuses a call under a tools refusal. Not a throw:
  // descriptors load on import, and one unknown word would disable every provider.
  const kind = raw.kind === "definitions" || raw.kind === "input_items" ? raw.kind : "enabler";
  return { path: raw.js_path ?? raw.path, kind, itemTypes: raw.item_types ?? [] };
}

/**
 * Whether a caller-supplied header `name` must be refused on an enforced call.
 *
 * Case-insensitive, because HTTP header names are: `authorization` and
 * `Authorization` are one header to the provider, and a list compared by exact
 * spelling would let the first through.
 */
export function isReservedHeader(descriptor: ProviderDescriptor, name: string): boolean {
  const wanted = name.toLowerCase();
  if (wanted === descriptor.tokenHeader.toLowerCase()) return true;
  return descriptor.reservedHeaders.some((reserved) => reserved.toLowerCase() === wanted);
}

function fromRaw(raw: RawDescriptor): ProviderDescriptor {
  const overrides = JS_CALL_SITE_PATHS[raw.name] ?? {};
  return {
    name: raw.name,
    clientModule: raw.client_module,
    clientBaseUrlHosts: raw.client_base_url_hosts ?? [],
    callSites: raw.call_sites.map((site) => ({
      id: site.id,
      path: overrides[site.id] ?? site.path,
      aliases: site.aliases ?? [],
      forceableParams: site.forceable_params ?? [],
      toolFields: (site.tool_fields ?? []).map(toolFieldFromRaw),
    })),
    identityParam: raw.identity_param ?? "user_id",
    ubtParam: raw.ubt_param ?? "ubt",
    tokenHeader: raw.token_header ?? DEFAULT_CONSENT_HEADER,
    paramTarget: raw.param_target === "headers" ? "headers" : "kwargs",
    headersKwarg: raw.headers_kwarg ?? "extra_headers",
    protectedParams: raw.protected_params ?? [],
    toolsParam: raw.tools_param,
    reservedHeaders: raw.reserved_headers ?? [],
    credentialRouting: raw.credential_routing
      ? {
          header: raw.credential_routing.header,
          headerValuePattern: raw.credential_routing.header_value_pattern,
          unit: raw.credential_routing.unit || "credential",
        }
      : undefined,
    descriptorVersion: raw.descriptor_version ?? "2",
  };
}

const RAW: RawDescriptor[] = [
  openai as RawDescriptor,
  anthropic as RawDescriptor,
  gemini as RawDescriptor,
  mistral as RawDescriptor,
  deepseek as RawDescriptor,
];

/**
 * The bundled descriptors exactly as they are on disk, by provider name.
 *
 * Exposed so nothing else has to build a path from a provider name. That is not
 * a convenience: interpolating a caller-supplied name into a `require` once made
 * `decidableOptions(["../../../etc/passwd"])` load an arbitrary file whose
 * contents then flowed into the returned options. A statically imported map has
 * no path to traverse, and it cannot fall out of step with the loader either.
 */
export function rawDescriptors(): Map<string, Record<string, unknown>> {
  const map = new Map<string, Record<string, unknown>>();
  for (const raw of RAW) map.set(raw.name, raw as unknown as Record<string, unknown>);
  return map;
}

/** Parse every bundled descriptor into a name → descriptor map. */
export function loadDescriptors(): Map<string, ProviderDescriptor> {
  const map = new Map<string, ProviderDescriptor>();
  for (const raw of RAW) {
    const descriptor = fromRaw(raw);
    map.set(descriptor.name, descriptor);
  }
  return map;
}
