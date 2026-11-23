// The shared custom-entry vectors: which entries a policy may hold. The Python
// suite holds the same verdicts, and the ID Side cloud reads the same file: it
// must never sign an entry an SDK refuses, or every call of that application
// would fail closed. The verdicts come from build_entry_vectors.py, never from
// an SDK.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { ContentPolicyError } from "../src/exceptions.ts";
import { ContentCleaner } from "../src/cleaning/index.ts";

const here = dirname(fileURLToPath(import.meta.url));
const path = join(here, "..", "..", "descriptors", "testdata", "content-entry-vectors.json");

interface EntryVector {
  kind: string;
  value: string;
  label?: string | null;
  accepted: boolean;
  note: string;
}

const document = JSON.parse(readFileSync(path, "utf8")) as { format: number; vectors: EntryVector[] };

function compiles(vector: EntryVector): boolean {
  const label = "label" in vector ? vector.label : "CASE";
  const entry = { kind: vector.kind, value: vector.value, action: "mask", label };
  try {
    new ContentCleaner({ schema: 1, custom: [entry] });
  } catch (error) {
    if (error instanceof ContentPolicyError) return false;
    throw error;
  }
  return true;
}

for (const vector of document.vectors) {
  test(`entry vector: ${vector.kind}: ${vector.note}`, () => {
    assert.equal(compiles(vector), vector.accepted);
  });
}

test("entry vectors cover both verdicts and every kind", () => {
  assert.equal(document.format, 1);
  const seen = new Set(document.vectors.map((vector) => `${vector.kind}:${vector.accepted}`));
  assert.deepEqual([...seen].sort(), ["domain:false", "domain:true", "pattern:false", "pattern:true", "term:false", "term:true"]);
});
