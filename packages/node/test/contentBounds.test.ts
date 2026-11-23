// A value that runs on is taken whole, to the engine's limit, in linear time.
//
// `testdata/content-open-values.json` writes, for each rule whose value can run
// on, a text that the value fills: 1 MiB, 5 MiB and exactly the engine's limit
// (8,388,608 characters). Each must come out as what stood before the value, one
// placeholder and what stood after it, counted once, within seconds; one
// character more is blocked `size_limit`, never skipped. Here a pattern whose
// matcher keeps a frame per repetition once overflowed the stack past 5.6
// million characters. The Python suite (`test_content_bounds.py`) reads the
// same file.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { ContentCleaner } from "../src/cleaning/index.ts";

const here = dirname(fileURLToPath(import.meta.url));
const descriptors = join(here, "..", "..", "descriptors");

interface Form {
  rule: string;
  custom?: Record<string, unknown>;
  before: string;
  head: string | string[];
  fill: string;
  after: string;
}
const OPEN = JSON.parse(readFileSync(join(descriptors, "testdata", "content-open-values.json"), "utf8")) as {
  sizes: number[];
  past_limit: number;
  forms: Form[];
};
const PLACEHOLDER = /^\[[A-Z0-9_]{1,24}_1\]$/;

const cleanerOf = (form: Form): ContentCleaner =>
  new ContentCleaner(
    form.custom === undefined ? { schema: 1, detectors: { [form.rule]: "mask" } } : { schema: 1, custom: [form.custom] },
  );

/** The start of the value, written in parts where a scanner would flag it. */
const headOf = (form: Form): string => (typeof form.head === "string" ? form.head : form.head.join(""));

function textOf(form: Form, size: number): string {
  const head = headOf(form);
  const fixed = form.before.length + head.length + form.after.length;
  return form.before + head + form.fill.repeat(size - fixed) + form.after;
}

for (const form of OPEN.forms) {
  const name = `${form.rule}: ${JSON.stringify(form.before + headOf(form))}`;
  test(`a value that fills the text is taken whole: ${name}`, () => {
    const cleaner = cleanerOf(form);
    for (const size of OPEN.sizes) {
      const text = textOf(form, size);
      assert.equal(text.length, size);
      const began = performance.now();
      const result = cleaner.clean(text);
      const elapsed = (performance.now() - began) / 1000;
      assert.deepEqual(result.counts, { [form.rule]: 1 }, String(size));
      const output = result.payload as string;
      assert.ok(output.startsWith(form.before) && output.endsWith(form.after));
      const middle = output.slice(form.before.length, output.length - form.after.length);
      assert.match(middle, PLACEHOLDER, String(size));
      assert.ok(elapsed < 10, `${size}: ${elapsed.toFixed(2)} s`);
    }
  });
  test(`one character past the limit is blocked: ${name}`, () => {
    const result = cleanerOf(form).clean(textOf(form, OPEN.past_limit));
    assert.deepEqual([result.blocked, result.counts], [{ size_limit: 1 }, {}]);
  });
}

test("every rule that follows a value has a form", () => {
  const rules = JSON.parse(readFileSync(join(descriptors, "content-rules.json"), "utf8")) as {
    detectors: { id: string; patterns: { extend?: string }[] }[];
  };
  const forms = new Set(OPEN.forms.map((form) => form.rule));
  for (const detector of rules.detectors) {
    if (detector.patterns.some((pattern) => pattern.extend !== undefined)) assert.ok(forms.has(detector.id), detector.id);
  }
});

// ── slow shapes found before the release, read in linear time since ────────

/**
 * A BEGIN line (written in parts, for the secret scanners) followed by four
 * header lines whose values hold `<br>` again and again, and no key: the
 * header value and the separators after it can both take `>` and `<br>`, so
 * each line can be split in as many places as it holds `<br>`.
 */
const BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----\n";

function bestOf(runs: number, cleaner: ContentCleaner, text: string): number {
  let best = Infinity;
  for (let run = 0; run < runs; run++) {
    const began = performance.now();
    cleaner.clean(text);
    best = Math.min(best, (performance.now() - began) / 1000);
  }
  return best;
}

test(
  "header lines full of line breaks are read in linear time",
  () => {
    // 48 rather than Python's 32: once V8 has compiled the pattern to native
    // code (earlier tests warm it), 32 takes about 0.2 s, 48 about a second.
    const text = BEGIN + Array.from({ length: 4 }, () => "Comment: " + "<br>".repeat(48) + "\n").join("") + "!!";
    const cleaner = new ContentCleaner({ schema: 1, detectors: { private_key: "mask" } }); // pragma: allowlist secret
    const elapsed = bestOf(1, cleaner, text);
    assert.ok(elapsed < 0.25, `${elapsed.toFixed(2)} s`);
  },
);

test(
  "many findings are resolved in linear time",
  () => {
    const textOf = (size: number): string =>
      " 1.1.1.1".repeat(Math.floor(size / 16)) + " 10.10.10.10".repeat(Math.floor(size / 24));
    const cleaner = new ContentCleaner({ schema: 1, detectors: { ip_address: "mask" } });
    const small = bestOf(3, cleaner, textOf(512 * 1024));
    const large = bestOf(1, cleaner, textOf(4 * 1024 * 1024));
    // Eight times the text: about eight times the time when linear, sixty-four
    // when quadratic.
    assert.ok(large / small < 16, `${small.toFixed(2)} s, then ${large.toFixed(2)} s`);
  },
);
