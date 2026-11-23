// Key material after a private key's BEGIN line: the check `followed_by` names.
//
// The check replaced a look-ahead of the catalogue that a backtracking matcher
// read in time exponential in the number of header lines. It must answer
// exactly what that look-ahead answered: the shared vectors state the answers
// at every bound by hand (`testdata/content-material-vectors.json`; the Python
// suite also runs the look-ahead itself against the check on random text), and
// the engine is held to linear time on the shapes that were slow.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { ContentCleaner } from "../src/cleaning/index.ts";
import { resolveCandidates } from "../src/cleaning/engine.ts";
import { privateKeyMaterial } from "../src/cleaning/material.ts";
import { DETECTORS, FOLLOWED_BY, followedBy } from "../src/cleaning/rules.ts";
import { shadowOf } from "../src/cleaning/shadow.ts";
import type { Rule } from "../src/cleaning/policy.ts";

const here = dirname(fileURLToPath(import.meta.url));
type Part = string | { repeat: string; times: number };
type Concat = string | { $concat: Part[] };
const VECTORS = JSON.parse(
  readFileSync(join(here, "..", "..", "descriptors", "testdata", "content-material-vectors.json"), "utf8"),
) as { begin: Concat; cases: { name: string; after: Concat; material: boolean }[] };

function decode(value: Concat): string {
  if (typeof value === "string") return value;
  return value.$concat.map((part) => (typeof part === "string" ? part : part.repeat.repeat(part.times))).join("");
}

const BEGIN = decode(VECTORS.begin);

function range(start: number, stop: number, step: number): number[] {
  const out: number[] = [];
  for (let value = start; value < stop; value += step) out.push(value);
  return out;
}
const KEYS = { schema: 1, detectors: { private_key: "mask" } }; // pragma: allowlist secret
const BOTH = { schema: 1, detectors: { email: "mask", private_key: "mask" } }; // pragma: allowlist secret

// ── the vectors ─────────────────────────────────────────────────────────────

for (const { name, after, material } of VECTORS.cases) {
  test(`the check answers the vector: ${name}`, () => {
    assert.equal(privateKeyMaterial(BEGIN + decode(after), BEGIN.length), material);
  });

  test(`a key without its END line is masked only before key material: ${name}`, () => {
    const text = "Here it is: " + BEGIN + decode(after);
    const result = new ContentCleaner(KEYS).clean(text);
    if (material) {
      const counts = { private_key: 1 }; // pragma: allowlist secret
      assert.deepEqual([result.payload, result.counts], ["Here it is: [PRIVATE_KEY_1]", counts]);
    } else {
      assert.deepEqual([result.payload, result.counts], [text, {}]);
    }
  });
}

test("the pattern names the check and no longer looks ahead", () => {
  const named = DETECTORS.get("private_key")!.patterns.filter((pattern) => pattern.followedBy !== undefined);
  assert.equal(named.length, 1);
  assert.equal(named[0].followedBy, privateKeyMaterial);
  assert.equal(FOLLOWED_BY.get("private_key_material"), privateKeyMaterial);
  assert.ok(!named[0].regex.source.includes("(?="));
});

test("a check this engine does not know is refused", () => {
  assert.equal(followedBy("private_key_material"), privateKeyMaterial);
  assert.throws(() => followedBy("nothing"), /unknown followed_by check "nothing"/);
});

test("a failed check moves on to the next BEGIN line", () => {
  // The first BEGIN line is only quoted; the second has its key material.
  const text = BEGIN + " is how a key starts.\n" + BEGIN + "\n" + "A".repeat(64);
  const result = new ContentCleaner(KEYS).clean(text);
  assert.equal(result.payload, BEGIN + " is how a key starts.\n[PRIVATE_KEY_1]");
});

// ── the check against the look-ahead it replaced, on random text ───────────

const B64 = "[A-Za-z0-9+/]";
const B64_FIRST = "[A-Za-z0-9/]";
const SKIP = "(?:[ \\t\\r\\n>+#;*|\"',«»-]|\\\\[nrt]?|//|<[Bb][Rr] ?/?>|\\[[A-Z0-9_]{1,24}_[0-9]{1,9}\\])";
const NAMES = ["Proc-Type", "DEK-Info", "Version", "Comment", "Hash", "Charset", "MessageID"];
const NOT_A_NAME = "(?!" + NAMES.map((name) => name + ":").join("|") + ")";
const ARMOR =
  "(?:" +
  NAMES.join("|") +
  "):(?:(?:" +
  NOT_A_NAME +
  "[^\\r\\n\\\\]){0,255}" +
  NOT_A_NAME +
  "[^\\r\\n\\\\ \\t>+#;*|\"',«»<-])?";
const TWO_RUNS = range(16, 32, 1)
  .map((n) => "|" + B64_FIRST + B64 + `{${n - 1}}` + SKIP + "{1,1024}" + B64_FIRST + (n < 31 ? B64 + `{${31 - n}}` : ""))
  .join("");
/** The look-ahead the check replaced, as the specification. */
const LOOK_AHEAD = new RegExp(
  "(?=" + SKIP + "{0,1024}(?:" + ARMOR + SKIP + "{0,512}){0,4}(?:" + B64_FIRST + B64 + "{31}" + TWO_RUNS + "))",
  "y",
);

function lookAhead(text: string, at: number): boolean {
  LOOK_AHEAD.lastIndex = at;
  return LOOK_AHEAD.test(text);
}

for (const { name, after, material } of VECTORS.cases) {
  test(`the vector is what the look-ahead answered: ${name}`, () => {
    assert.equal(lookAhead(BEGIN + decode(after), BEGIN.length), material);
  });
}

/** A small seeded generator (mulberry32): reproducible test data. */
function generator(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

const SEPARATORS = [..." \t\r\n>+#;*|\"',«»-", "\\n", "\\", "//", "/", "<br>", "<br >", "<br/>", "<br />", "<BR>", "<"];
SEPARATORS.push("[EMAIL_1]", "[A_1]", "[", "]");
const HEADERS = [...NAMES.map((name) => name + ":"), "comment:", "Comment"];

function randomAfter(random: () => number): string {
  const choice = <T>(items: readonly T[]): T => items[Math.floor(random() * items.length)];
  const run = (size: number): string => Array.from({ length: size }, () => choice([..."ABCabc019+/"])).join("");
  const skip = (count: number): string => Array.from({ length: count }, () => choice(SEPARATORS)).join("");
  const parts = [skip(choice([0, 1, 2, 5]))];
  for (let line = choice([0, 1, 1, 2, 3, 4, 5]); line > 0; line--) {
    const value = Array.from({ length: choice([0, 1, 3, 6]) }, () =>
      choice(["x", " ", "<br>", "[A_1]", "//", "a@b.fr", run(4), "Hash:"]),
    ).join("");
    parts.push(choice(HEADERS) + value + skip(choice([0, 1, 2, 4])));
  }
  parts.push(run(choice([8, 15, 16, 20, 31, 32])));
  if (random() < 0.6) parts.push(skip(choice([0, 1, 2])) + run(choice([1, 8, 15, 16, 17])));
  return parts.join("");
}

test("the check answers what the look-ahead answered on random text", () => {
  const random = generator(20261123);
  let accepted = 0;
  for (let index = 0; index < 6000; index++) {
    const text = BEGIN + randomAfter(random);
    const expected = lookAhead(text, BEGIN.length);
    if (expected) accepted += 1;
    assert.equal(privateKeyMaterial(text, BEGIN.length), expected, JSON.stringify(text));
  }
  // Both answers are well represented.
  assert.ok(accepted > 500 && accepted < 5000, String(accepted));
});

// ── time ────────────────────────────────────────────────────────────────────

function blocks(block: string, size: number): string {
  return block.repeat(Math.floor(size / block.length) + 1);
}

/** Shapes that the look-ahead read in time exponential in the header lines, or quadratic in what follows a value. */
const SLOW_SHAPES: Record<string, string> = {
  "four lines of <br>": BEGIN + ("\nComment: " + "<br>".repeat(63)).repeat(4) + "\n!!\n",
  "four lines of placeholders": BEGIN + ("\nComment: " + "[A_1]".repeat(50)).repeat(4) + "\n!!\n",
  "four lines of addresses": BEGIN + ("\nComment: " + "a@b.fr ".repeat(40)).repeat(4) + "\n!!\n",
  "a value ending in a <br>, then blanks": BEGIN + "\nComment: " + "<br>".repeat(63) + "\n".repeat(449) + "!!\n",
  "64 header lines of 255": BEGIN + ("\nComment: " + ".".repeat(254)).repeat(64) + "\n!!\n",
  "values of every length":
    BEGIN +
    range(1, 256, 17)
      .map((size) => "\nComment: " + "a <br>[A_1]//".repeat(20).slice(0, size))
      .join("") +
    "\n!!\n",
  "separators of every sort":
    BEGIN + ("\nComment: x" + " \t>+#;*|\"',«»-\\n\\//<br><br ><br/><br />[A_1]".repeat(12)).repeat(4) + "\n!!\n",
  "four comment lines then blanks": BEGIN + ("\nComment: x" + " ".repeat(512)).repeat(4) + "\n!!\n",
};

for (const shape of Object.keys(SLOW_SHAPES).sort()) {
  test(`one block of a slow shape takes milliseconds: ${shape}`, () => {
    // One block took up to 6 s with the look-ahead (four lines of <br>); the
    // addresses, masked first, become placeholders the second pass reads.
    const cleaner = new ContentCleaner(BOTH);
    const began = performance.now();
    const result = cleaner.clean(SLOW_SHAPES[shape]);
    assert.ok(performance.now() - began < 500);
    assert.equal(result.counts.private_key, undefined);
  });

  test(`a megabyte of a slow shape grows linearly: ${shape}`, () => {
    const cleaner = new ContentCleaner(BOTH);
    const best = (size: number): number => {
      const text = blocks(SLOW_SHAPES[shape], size);
      let least = Infinity;
      for (let run = 0; run < 2; run++) {
        const began = performance.now();
        cleaner.clean(text);
        least = Math.min(least, performance.now() - began);
      }
      return least;
    };
    const small = best(262_144);
    const large = best(1_048_576);
    assert.ok(large <= 8 * Math.max(small, 2), `${small.toFixed(0)} ms, then ${large.toFixed(0)} ms`);
    assert.ok(large < 5_000);
  });
}

for (const lines of [1, 2, 3, 4, 5, 8, 16, 32, 64]) {
  test(`one block of ${lines} header lines of <br> takes milliseconds`, () => {
    // Only four header lines are read; the look-ahead's time grew with each.
    const block = BEGIN + ("\nComment: " + "<br>".repeat(63)).repeat(lines) + "\n!!\n";
    const began = performance.now();
    const result = new ContentCleaner(BOTH).clean(block);
    assert.ok(performance.now() - began < 500);
    assert.equal(result.counts.private_key, undefined);
  });
}

function alternating(size: number): string {
  const half = Math.floor(size / 2);
  return " 1.1.1.1".repeat(Math.floor(half / 8) + 1).slice(0, half) + " 10.10.10.10".repeat(Math.floor(half / 12) + 1).slice(0, half);
}

test("many findings of two lengths resolve in linear time", () => {
  // Splicing each kept finding into a sorted list took quadratic time: 11.5 s
  // for 8 MiB of alternating addresses.
  const cleaner = new ContentCleaner({ schema: 1, detectors: { ip_address: "mask" } });
  const best = (size: number): number => {
    const text = alternating(size);
    let least = Infinity;
    for (let run = 0; run < 2; run++) {
      const began = performance.now();
      cleaner.clean(text);
      least = Math.min(least, performance.now() - began);
    }
    return least;
  };
  const small = best(1_048_576);
  const large = best(4_194_304);
  assert.ok(large <= 8 * small, `${small.toFixed(0)} ms, then ${large.toFixed(0)} ms`);
});

test("resolving keeps each group of overlapping findings under its first", () => {
  const rule = (order: number, key: string) => ({ order, key }) as unknown as Rule;
  const first = rule(0, "a");
  const second = rule(1, "b");
  const resolved = resolveCandidates(
    [
      { start: 0, end: 2, rule: second }, // joins the group through (1, 4)
      { start: 1, end: 4, rule: first }, // the longest: wins the group
      { start: 6, end: 8, rule: second }, // alone
      { start: 4, end: 5, rule: first }, // touches (1, 4), does not overlap it
    ],
    shadowOf("abcdefghij"),
  );
  assert.deepEqual(
    resolved.map((c) => [c.start, c.end, c.rule.key]),
    [
      [0, 4, "a"],
      [4, 5, "a"],
      [6, 8, "b"],
    ],
  );
});
