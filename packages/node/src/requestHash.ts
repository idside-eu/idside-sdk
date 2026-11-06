// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

import { createHash } from "node:crypto";
import { canonicalize } from "./jcs.ts";

/**
 * Compute the `request_hash` bound into an ACT token: the lowercase SHA-256 of
 * the RFC 8785 (JCS) canonical encoding of `payload`, prefixed `sha256:`.
 *
 * The caller is responsible for excluding the identity parameter (`user_id` /
 * `ubt`) and the per-request headers kwarg before passing the payload here —
 * those never take part in the hash.
 */
export function requestHash(payload: unknown): string {
  const canonical = canonicalize(payload);
  return "sha256:" + createHash("sha256").update(canonical).digest("hex");
}
