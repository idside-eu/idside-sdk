// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * The digests a cleaned call declares: policy, rules, build, challenge.
 *
 * - `digest`: SHA-256 of the RFC 8785 canonical form of the policy document.
 * - `rules`: SHA-256 of the bundled `content-rules.json`, as its bytes.
 * - `build`: SHA-256 over what decides cleaning in this package as installed:
 *   the cleaning code (`dist/cleaning/*.js` in the published package) and the
 *   provider descriptors (`dist/providers/*.json`). For each file, sorted by
 *   path: the path relative to the package root (UTF-8), a NUL, the file's
 *   length in bytes written in decimal, a NUL, then its bytes.
 * - `challenge.response`: SHA-256 of the challenge token, a newline, then the
 *   challenge inputs, each cleaned on its own under the reference policy,
 *   joined by newlines.
 *
 * Python's twin is `_cleaning/digests.py`. A build that cannot be read is
 * declared as the digest of no file at all: the cloud then flags the engine.
 */

import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { basename, dirname, extname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { canonicalize } from "../jcs.ts";
import { RULES_BYTES } from "./rules.ts";

function sha256(data: Uint8Array | string): string {
  return "sha256:" + createHash("sha256").update(data).digest("hex");
}

/** The digest of a policy document, as the cloud signs it and a proof carries it. */
export function contentPolicyDigest(policy: unknown): string {
  return sha256(canonicalize(policy));
}

export function rulesDigest(): string {
  return sha256(RULES_BYTES);
}

/** Files are framed by path and length, then sorted by path, as Python does it. */
export function buildDigestOf(files: readonly [string, Uint8Array][]): string {
  const hash = createHash("sha256");
  const sorted = [...files].sort(([a], [b]) => {
    const left = Buffer.from(a, "utf8");
    const right = Buffer.from(b, "utf8");
    return Buffer.compare(left, right);
  });
  for (const [path, data] of sorted) {
    hash.update(Buffer.from(`${path}\u0000${data.length}\u0000`, "utf8"));
    hash.update(data);
  }
  return "sha256:" + hash.digest("hex");
}

/** What the build digest covers: this module's folder, and the descriptors beside it. */
export function buildFiles(moduleUrl: string = import.meta.url): [string, Uint8Array][] {
  const cleaningDir = dirname(fileURLToPath(moduleUrl));
  const rootDir = dirname(cleaningDir); // `dist` (published) or `src` (this repository)
  const root = basename(rootDir);
  const extension = extname(fileURLToPath(moduleUrl)); // `.js` or `.ts`
  const files: [string, Uint8Array][] = [];
  for (const name of readdirSync(cleaningDir)) {
    if (extname(name) === extension) files.push([`${root}/cleaning/${name}`, readFileSync(join(cleaningDir, name))]);
  }
  for (const name of readdirSync(join(rootDir, "providers"))) {
    if (extname(name) === ".json") files.push([`${root}/providers/${name}`, readFileSync(join(rootDir, "providers", name))]);
  }
  return files;
}

/** The build digest of the module at `moduleUrl`; of no file at all when unreadable. */
export function buildDigestAt(moduleUrl: string): string {
  let files: [string, Uint8Array][];
  try {
    files = buildFiles(moduleUrl);
  } catch {
    files = [];
  }
  return buildDigestOf(files);
}

let cachedBuild: string | undefined;

export function buildDigest(): string {
  cachedBuild ??= buildDigestAt(import.meta.url);
  return cachedBuild;
}

export function challengeDigest(token: string, cleaned: readonly string[]): string {
  return sha256(Buffer.from(token + "\n" + cleaned.join("\n"), "utf8"));
}
