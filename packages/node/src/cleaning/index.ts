// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Prompt cleaning: the engine, its digests and the replay check.
 *
 * What the SDK applies to a call before it hashes and sends it, under the
 * policy the organization set on the dashboard. The public surface is
 * re-exported from the package root: `verifyClean()` and
 * `contentPolicyDigest()`. Python's twin is `idside._cleaning`.
 */

import { loadDescriptors, NO_CONTENT_FIELDS, type ContentFields } from "../descriptors.ts";
import { canonicalizeToString } from "../jcs.ts";
import { challengeDigest } from "./digests.ts";
import { ContentCleaner, type CleanResult } from "./engine.ts";

export { buildDigest, buildDigestOf, contentPolicyDigest, rulesDigest } from "./digests.ts";
export { ATTACHMENTS, ContentCleaner, referencePolicy, SIZE_LIMIT, type CleanResult } from "./engine.ts";
export { CATALOGUE_IDS, ENGINE_VERSION, catalogueProblem } from "./rules.ts";

/** The content block of a bundled provider descriptor (none: read everything). */
export function contentFields(provider: string | null | undefined): ContentFields {
  if (provider === null || provider === undefined) return NO_CONTENT_FIELDS;
  const descriptor = loadDescriptors().get(provider);
  if (descriptor === undefined) throw new TypeError(`unknown provider ${JSON.stringify(provider)}`);
  return descriptor.content;
}

const CACHE = new Map<string, ContentCleaner>();

/** Clean `payload` under `policy` as the SDK does before a call leaves. */
export function clean(payload: unknown, policy: unknown, provider: string | null | undefined): CleanResult {
  let canonical: string;
  try {
    canonical = canonicalizeToString(policy);
  } catch {
    // Not JSON: compiled uncached, to throw the policy error it deserves.
    return new ContentCleaner(policy, contentFields(provider)).clean(payload);
  }
  const key = `${provider ?? ""}\u0000${canonical}`;
  let cleaner = CACHE.get(key);
  if (cleaner === undefined) {
    cleaner = new ContentCleaner(JSON.parse(canonical), contentFields(provider));
    if (CACHE.size >= 32) CACHE.delete(CACHE.keys().next().value as string);
    CACHE.set(key, cleaner);
  }
  return cleaner.clean(payload);
}

/**
 * Whether `payload` is what cleaning under `policy` lets leave, unchanged.
 *
 * The replay check. A body the SDK sent is a fixed point of the policy it
 * applied: cleaning it again masks nothing and blocks nothing. Whoever holds
 * the request that went out (and the policy its proof names, by digest) can
 * run this years later, without ID Side. `true` does not mean the body held
 * nothing sensitive — only that this policy, with this engine, finds nothing
 * more to do. `provider` is the descriptor name (`"openai"`…) whose structural
 * fields are left alone; `null` reads every string.
 */
export function verifyClean(payload: unknown, policy: unknown, options: { provider: string | null }): boolean {
  const result = clean(payload, policy, options.provider);
  return Object.keys(result.blocked).length === 0 && Object.keys(result.counts).length === 0;
}

/** The answer to a cloud challenge: each input cleaned alone, reference policy. */
export function challengeResponse(token: string, inputs: readonly string[]): string {
  if (typeof token !== "string" || !inputs.every((text) => typeof text === "string")) {
    throw new TypeError("a challenge is a token and a list of strings");
  }
  const cleaner = ContentCleaner.reference();
  return challengeDigest(
    token,
    inputs.map((text) => cleaner.clean(text).payload as string),
  );
}
