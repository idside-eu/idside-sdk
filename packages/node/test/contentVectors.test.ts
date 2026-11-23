import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { ContentPolicyError } from "../src/exceptions.ts";
import { challengeResponse, clean, ContentCleaner, contentPolicyDigest, verifyClean } from "../src/cleaning/index.ts";

// The shared prompt-cleaning vectors, replayed by the Node engine. The Python
// suite replays the same file; its expectations are hand-written templates in
// packages/descriptors/build_content_vectors.py, never recordings of an engine.
const here = dirname(fileURLToPath(import.meta.url));
const vectorsPath = join(here, "..", "..", "descriptors", "testdata", "content-cleaning-vectors.json");

type Part = string | { repeat: string; times: number };

/** Join every `{"$concat": [...]}` back into the string it stands for. */
function decode(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(decode);
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value);
    if (entries.length === 1 && entries[0][0] === "$concat") {
      return (entries[0][1] as Part[]).map((part) => (typeof part === "string" ? part : part.repeat.repeat(part.times))).join("");
    }
    return Object.fromEntries(entries.map(([key, item]) => [key, decode(item)]));
  }
  return value;
}

interface Case {
  name: string;
  policy: string | Record<string, unknown>;
  provider: string | null;
  input: unknown;
  output?: unknown;
  counts: Record<string, number>;
  blocked: Record<string, number>;
  attachments: number;
  error?: string;
}

const raw = JSON.parse(readFileSync(vectorsPath, "utf8")) as Record<string, unknown>;
const policies = decode(raw.policies) as Record<string, Record<string, unknown>>;
const cases = decode(raw.cases) as Case[];
const challenges = decode(raw.challenges) as { token: string; inputs: string[]; outputs: string[]; response: string }[];
const digests = decode(raw.policy_digests) as { policy: unknown; digest: string }[];

test("there are hundreds of cases", () => {
  assert.ok(cases.length >= 300);
});

for (const vector of cases) {
  test(`content vector: ${vector.name}`, () => {
    const policy = typeof vector.policy === "string" ? policies[vector.policy] : vector.policy;
    if (vector.error === "policy") {
      assert.throws(() => clean(vector.input, policy, vector.provider), ContentPolicyError);
      return;
    }
    const result = clean(vector.input, policy, vector.provider);
    assert.deepEqual({ ...result.blocked }, vector.blocked);
    assert.equal(result.attachments, vector.attachments);
    if (Object.keys(vector.blocked).length > 0) {
      assert.equal(verifyClean(vector.input, policy, { provider: vector.provider }), false);
      return;
    }
    assert.deepEqual(result.payload, vector.output);
    assert.deepEqual({ ...result.counts }, vector.counts);
    // The replay property: what left is a fixed point of the same policy.
    const again = clean(result.payload, policy, vector.provider);
    assert.deepEqual(again.payload, result.payload);
    assert.deepEqual({ ...again.counts }, {});
    assert.equal(verifyClean(result.payload, policy, { provider: vector.provider }), true);
    assert.equal(
      verifyClean(vector.input, policy, { provider: vector.provider }),
      Object.keys(vector.counts).length === 0,
    );
  });
}

for (const challenge of challenges) {
  test(`challenge response: ${challenge.token}`, () => {
    assert.equal(challengeResponse(challenge.token, challenge.inputs), challenge.response);
    const cleaner = ContentCleaner.reference();
    assert.deepEqual(
      challenge.inputs.map((text) => cleaner.clean(text).payload),
      challenge.outputs,
    );
  });
}

for (const entry of digests) {
  test(`policy digest: ${entry.digest}`, () => {
    assert.equal(contentPolicyDigest(entry.policy), entry.digest);
  });
}

test("the rules file the vectors were written for is the bundled one", () => {
  const rules = readFileSync(join(here, "..", "src", "content-rules.json"));
  const canonical = readFileSync(join(here, "..", "..", "descriptors", "content-rules.json"));
  assert.ok(rules.equals(canonical), "src/content-rules.json drifted from packages/descriptors/content-rules.json");
  assert.match(createHash("sha256").update(rules).digest("hex"), /^[0-9a-f]{64}$/);
});
