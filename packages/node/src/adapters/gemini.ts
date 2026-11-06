// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Built-in adapter for Google's `@google/genai` client.
 *
 * `models.generateContent` takes a single `{ model, contents, config }` object
 * and no free-form kwargs, so — unlike openai/anthropic/mistral — there is no
 * second options argument to carry per-request headers. Headers are supported
 * only nested as `config.httpOptions.headers`. This adapter merges the
 * cloud-forced params into `config` and carries the ACT token in
 * `config.httpOptions.headers`, without mutating the caller's objects.
 */

import { GenericAdapter, dropPath, filterProtected, isPlainObject, mergeHeaders, type RequestMetadata } from "./base.ts";
import { GEMINI_CONFIG_FIELDS } from "./callOptions.ts";
import { ConsentTokenError } from "../exceptions.ts";

function asDict(value: unknown, what: string): Record<string, unknown> {
  if (value === null || value === undefined) return {};
  if (isPlainObject(value)) return { ...value };
  const toJSON = (value as { toJSON?: unknown }).toJSON;
  if (typeof value === "object" && typeof toJSON === "function") {
    const dumped = (value as { toJSON(): unknown }).toJSON();
    if (isPlainObject(dumped)) return { ...dumped };
  }
  throw new ConsentTokenError(
    `Cannot enforce consent on this Gemini call: ${what} is neither a plain object nor exposes toJSON().`,
  );
}

export class GeminiAdapter extends GenericAdapter {
  /**
   * The hash, without the config fields that are not request data.
   *
   * `config.abortSignal` is how a Gemini call is cancelled. It never leaves the
   * process, and it cannot be canonicalized: it used to make every call that
   * passed one throw `ConsentTokenError`. So no existing hash moves.
   */
  override extractRequestMetadata(params: Record<string, unknown>): RequestMetadata {
    let hashed = params;
    for (const [field, role] of Object.entries(GEMINI_CONFIG_FIELDS)) {
      if (role === "unhashed") hashed = dropPath(hashed, ["config", field]);
    }
    return super.extractRequestMetadata(hashed);
  }

  /**
   * `credentialHeaders` travels in the same nested header map, for completeness:
   * @google/genai reads no header that selects a project, so Gemini routing
   * means a second client rather than a header, and this stays empty in practice.
   */
  override buildEnforcedArgs(
    args: unknown[],
    paramsForced: Record<string, unknown>,
    token: string,
    credentialHeaders?: Record<string, string>,
  ): unknown[] {
    const [body, ...tail] = args;
    const out: Record<string, unknown> = { ...(isPlainObject(body) ? body : {}) };
    const safe = filterProtected(paramsForced, this.protectedParams);

    const config = asDict(out.config, "config");
    Object.assign(config, safe);

    const httpOptions = asDict(config.httpOptions, "config.httpOptions");
    const injectedHeaders: Record<string, unknown> = { [this.descriptor.tokenHeader]: token };
    if (credentialHeaders) Object.assign(injectedHeaders, credentialHeaders);
    httpOptions.headers = mergeHeaders(httpOptions.headers, injectedHeaders);
    config.httpOptions = httpOptions;
    out.config = config;

    return [out, ...tail];
  }
}
