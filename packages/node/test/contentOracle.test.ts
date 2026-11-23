// Never less than before: the non-regression oracle both suites hold.
//
// A frozen reference engine and its catalogue (`testdata/reference-engine/`,
// never edited) clean a generated corpus — every positive value of the shared
// vectors between every prefix and suffix of `testdata/content-oracle.json`,
// and every string input of the shared cases — and so does the engine under
// test. Every character the reference masked must still be masked: the engine
// may mask more, never less, except where a named exclusion of that file
// stands. The Python suite (`test_content_oracle.py`) builds the same corpus,
// checked by its digest.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as engineModule from "../src/cleaning/engine.ts";
import * as rulesModule from "../src/cleaning/rules.ts";
import * as shadowModule from "../src/cleaning/shadow.ts";
import { masked, type EngineModules } from "./cleaningSpans.ts";

const here = dirname(fileURLToPath(import.meta.url));
const testdata = join(here, "..", "..", "descriptors", "testdata");
const reference = join(testdata, "reference-engine");
const ORACLE = JSON.parse(readFileSync(join(testdata, "content-oracle.json"), "utf8")) as {
  prefixes: string[];
  suffixes: string[];
  exclusions: { name: string; detector: string; regex: string; ignore_case: boolean }[];
  corpus: { texts: number; digest: number };
};
const VECTORS = JSON.parse(readFileSync(join(testdata, "content-cleaning-vectors.json"), "utf8")) as {
  positives: unknown[];
  policies: Record<string, unknown>;
  cases: unknown[];
};

/**
 * The reference engine's files, as they were frozen: never edited. The SHA-256
 * of each file name, a zero byte, its length, a zero byte and its bytes, by name.
 */
const MODULES = ["rules", "shadow", "validators", "policy", "engine"];
const FROZEN_FILES = ["content-rules.json", ...MODULES.map((name) => `python/${name}.py`), ...MODULES.map((name) => `node/${name}.ts`)];
const FROZEN = "43899082580b47afe19b7ec4f21157738c2607973ee3c2f8dc0f070616cf4728"; // pragma: allowlist secret

function decode(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(decode);
  if (typeof value === "object" && value !== null) {
    const record = value as Record<string, unknown>;
    if (Object.keys(record).length === 1 && "$concat" in record) {
      return (record.$concat as (string | { repeat: string; times: number })[])
        .map((part) => (typeof part === "string" ? part : part.repeat.repeat(part.times)))
        .join("");
    }
    return Object.fromEntries(Object.entries(record).map(([key, item]) => [key, decode(item)]));
  }
  return value;
}

interface Positive {
  detector: string;
  lead: string;
  value: string;
  trail: string;
}

/** A positive value between `prefix` and `suffix` (the file's `placement`). */
export function compose({ lead, value, trail }: Positive, prefix: string, suffix: string): string {
  const form = lead !== "" && (!" \t\n".includes(lead[lead.length - 1]) || /[=:][ \t\n]*$/.test(lead) || /(?:bearer|basic) $/i.test(lead));
  const left = form ? prefix + lead : lead + prefix;
  const right = trail.startsWith("@") || trail.startsWith(":") ? trail + suffix : suffix + trail;
  return left + value + right;
}

function corpus(): [string, string][] {
  const seen = new Set<string>();
  const items: [string, string][] = [];
  for (const positive of decode(VECTORS.positives) as Positive[]) {
    for (const prefix of ORACLE.prefixes) {
      for (const suffix of ORACLE.suffixes) {
        const text = compose(positive, prefix, suffix);
        const key = positive.detector + "\u0000" + text;
        if (!seen.has(key)) {
          seen.add(key);
          items.push([positive.detector, text]);
        }
      }
    }
  }
  return items;
}

const CORPUS = corpus();
const DETECTORS = [...new Set(CORPUS.map(([detector]) => detector))].sort();

const ENGINE: EngineModules = { ...engineModule, ...rulesModule, ...shadowModule } as unknown as EngineModules;
const referenceEngine = (await import(join(reference, "node", "engine.ts"))) as Record<string, unknown>;
const REFERENCE: EngineModules = {
  ...referenceEngine,
  ...((await import(join(reference, "node", "rules.ts"))) as object),
  ...((await import(join(reference, "node", "shadow.ts"))) as object),
} as unknown as EngineModules;
type Cleaner = { clean(payload: unknown): { payload: unknown; blocked: Record<string, number> } };
const ReferenceCleaner = referenceEngine.ContentCleaner as new (policy: unknown) => Cleaner;
const Cleaner = engineModule.ContentCleaner as new (policy: unknown) => Cleaner;

function lost(length: number, before: Uint8Array | undefined, after: Uint8Array | undefined): number[] | undefined {
  if (after === undefined) return undefined; // the engine under test blocks: it sends less
  if (before === undefined) return Array.from({ length }, (_, index) => index);
  const indices: number[] = [];
  before.forEach((flag, index) => {
    if (flag && !after[index]) indices.push(index);
  });
  return indices.length > 0 ? indices : undefined;
}

/** The named exclusion that covers every lost index, of `detector` (of every detector: `undefined`). */
function excused(text: string, indices: number[], detector: string | undefined): string | undefined {
  for (const exclusion of ORACLE.exclusions) {
    if (detector !== undefined && exclusion.detector !== detector) continue;
    const regex = new RegExp(exclusion.regex, exclusion.ignore_case ? "gi" : "g");
    const spans = [...text.matchAll(regex)].map((match) => [match.index, match.index + match[0].length]);
    if (indices.every((index) => spans.some(([start, end]) => start <= index && index < end))) return exclusion.name;
  }
  return undefined;
}

test("the reference engine is the frozen one", () => {
  const digest = createHash("sha256");
  for (const name of [...FROZEN_FILES].sort()) {
    const data = readFileSync(join(reference, name));
    digest.update(Buffer.concat([Buffer.from(`${name}\u0000${data.length}\u0000`, "utf8"), data]));
  }
  assert.equal(digest.digest("hex"), FROZEN);
});

test("the corpus is the one both suites build", () => {
  const digest = createHash("sha256");
  for (const [detector, text] of CORPUS) digest.update(`${detector}\u0000${text}\u0000`, "utf8");
  // The first 48 bits of the digest, as a number.
  const head = Number.parseInt(digest.digest("hex").slice(0, 12), 16);
  assert.deepEqual([CORPUS.length, head], [ORACLE.corpus.texts, ORACLE.corpus.digest]);
  assert.ok(CORPUS.length >= 100_000);
});

for (const detector of DETECTORS) {
  test(`the engine masks whatever the reference masked: ${detector}`, () => {
    const policy = { schema: 1, detectors: { [detector]: "mask" } };
    const before = new ReferenceCleaner(policy);
    const after = new Cleaner(policy);
    const failures: string[] = [];
    for (const [item, text] of CORPUS) {
      if (item !== detector) continue;
      const [was] = masked(REFERENCE, before, text);
      const [now, cleaned] = masked(ENGINE, after, text);
      const indices = lost(text.length, was, now);
      if (indices === undefined || excused(text, indices, detector) !== undefined) continue;
      failures.push(`${JSON.stringify(text)} -> ${JSON.stringify(cleaned)}`);
    }
    assert.deepEqual(failures.slice(0, 12), [], `${failures.length} regressions`);
  });
}

test("the engine masks whatever the reference masked in the cases", () => {
  const policies = decode(VECTORS.policies) as Record<string, unknown>;
  const failures: string[] = [];
  for (const raw of decode(VECTORS.cases) as { name: string; policy: unknown; provider: unknown; input: unknown }[]) {
    if (raw.provider || typeof raw.input !== "string") continue;
    const policy = typeof raw.policy === "string" ? policies[raw.policy] : raw.policy;
    let before: Cleaner;
    try {
      before = new ReferenceCleaner(policy);
    } catch {
      continue; // a policy the reference did not know
    }
    let after: Cleaner;
    try {
      after = new Cleaner(policy);
    } catch {
      continue; // refused: the call is not made at all, which sends less
    }
    const [was] = masked(REFERENCE, before, raw.input);
    const [now, cleaned] = masked(ENGINE, after, raw.input);
    const indices = lost(raw.input.length, was, now);
    if (indices !== undefined && excused(raw.input, indices, undefined) === undefined) {
      failures.push(`${raw.name}: ${JSON.stringify(raw.input)} -> ${JSON.stringify(cleaned)}`);
    }
  }
  assert.deepEqual(failures.slice(0, 12), [], `${failures.length} regressions`);
});

test("the replay is the engine", () => {
  for (const detector of DETECTORS) {
    const policy = { schema: 1, detectors: { [detector]: "mask" } };
    const pairs: [EngineModules, Cleaner][] = [
      [REFERENCE, new ReferenceCleaner(policy)],
      [ENGINE, new Cleaner(policy)],
    ];
    const texts = CORPUS.filter(([item]) => item === detector)
      .map(([, text]) => text)
      .filter((_, index) => index % 97 === 0);
    for (const [modules, cleaner] of pairs) {
      for (const text of texts) {
        const [hidden, cleaned] = masked(modules, cleaner, text);
        const result = cleaner.clean(text);
        if (hidden === undefined) assert.ok(Object.keys(result.blocked).length > 0);
        else assert.equal(cleaned, result.payload, `${detector}: ${JSON.stringify(text)}`);
      }
    }
  }
});
