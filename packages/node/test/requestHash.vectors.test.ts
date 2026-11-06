import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { requestHash } from "../src/requestHash.ts";

// The cross-language contract. These are the same vectors the Python SDK and the
// Ruby cloud test against: if Node disagrees by a single byte, every ACT token we
// issue for a Node caller would silently fail to verify.
const here = dirname(fileURLToPath(import.meta.url));
const vectorsPath = join(here, "..", "..", "descriptors", "testdata", "request-hash-vectors.json");

interface Vector {
  description: string;
  payload: Record<string, unknown>;
  expected_hash: string;
  sdk_strips?: string[];
}

const { vectors } = JSON.parse(readFileSync(vectorsPath, "utf8")) as { vectors: Vector[] };

test("the shared vector file is present", () => {
  assert.ok(vectors.length > 0, "no shared request-hash vectors found");
});

for (const vector of vectors) {
  test(`request-hash vector: ${vector.description}`, () => {
    // The identity params (user_id / ubt) and the per-request headers kwarg are
    // stripped by the SDK before hashing; the shared file marks them.
    const payload: Record<string, unknown> = { ...vector.payload };
    for (const key of vector.sdk_strips ?? []) {
      delete payload[key];
    }
    assert.equal(requestHash(payload), vector.expected_hash);
  });
}
