// A value is taken whole or not at all: the property both suites hold.
//
// A detector that counts a value while part of it is left in the clear tells
// the organization a secret was masked when it left. So, for every value the
// shared vectors say a detector takes (`positives`), placed in generated
// contexts — prefixes and suffixes, a prefix on every line, escaped newlines,
// the value lengthened past the engine's bounds, a private key cut before its
// END line — under a policy enabling that detector alone: whenever the detector
// counts something, no run of 4 characters of the value is left in the output.
// The Python suite (`test_content_whole_values.py`) generates the same contexts.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { ContentCleaner } from "../src/cleaning/index.ts";

const here = dirname(fileURLToPath(import.meta.url));
const descriptors = join(here, "..", "..", "descriptors");
const vectors = JSON.parse(
  readFileSync(
    join(descriptors, "testdata", "content-cleaning-vectors.json"),
    "utf8",
  ),
) as {
  positives: Record<string, unknown>[];
};
const PLACEHOLDER = /\[[A-Z0-9_]{1,24}_[0-9]{1,9}\]/g;
const FLOOR = (
  JSON.parse(
    readFileSync(join(descriptors, "testdata", "content-oracle.json"), "utf8"),
  ) as {
    floor: { prefixes: string[]; suffixes: string[] };
  }
).floor;

const join8 = (value: unknown): string =>
  typeof value === "object" && value !== null && "$concat" in value
    ? (value as { $concat: string[] }).$concat.join("")
    : (value as string);

interface Positive {
  detector: string;
  lead: string;
  value: string;
  trail: string;
}
const POSITIVES: Positive[] = vectors.positives.map((positive) => ({
  detector: join8(positive.detector),
  lead: join8(positive.lead),
  value: join8(positive.value),
  trail: join8(positive.trail),
}));

// What stands before the text, before every line of a multi-line value, and
// after the text. Written the same way in the Python suite.
const PREFIXES = [
  "",
  ">",
  "> ",
  "-",
  "- ",
  "+",
  "+ ",
  "//",
  "// ",
  "#",
  "# ",
  "*",
  "* ",
  ";",
  " * ",
  ...[1, 2, 4, 8, 16, 17, 20, 32, 64].map((width) => " ".repeat(width)),
];
const SUFFIXES = [
  "",
  " ",
  ".",
  "...",
  ",",
  ")",
  '"',
  "'",
  ";",
  ":",
  "abc",
  "7",
  "_x",
  " €",
  "€",
  " EUR",
  " USD",
  " European",
  " USDA",
  ":8080",
  ":s3cretPass",
  "/path",
  "\n",
  "\\n",
];
// Characters appended to a value to take it past an internal bound.
const LENGTHS = [1, 64, 300, 5000];
/**
 * The token and secret detectors whose every pattern follows a value to its end (`extend`): a
 * value written in a URL keeps its escapes (%2F), which are part of it.
 */
const CATALOGUE = JSON.parse(
  readFileSync(join(descriptors, "content-rules.json"), "utf8"),
) as {
  detectors: { id: string; group: string; patterns: { extend?: string }[] }[];
};
const ESCAPED = new Set(
  CATALOGUE.detectors
    .filter((detector) =>
      ["ai_keys", "tech_keys", "secrets"].includes(detector.group),
    )
    .filter((detector) =>
      detector.patterns.every((pattern) => pattern.extend !== undefined),
    )
    .map((detector) => detector.id),
);

const isAlnum = (char: string): boolean => /^[A-Za-z0-9]$/.test(char);

/**
 * `value` and `count` more of its own letters and digits, in turn. Only a value
 * that ends with a letter or a digit is open-ended: one that ends with
 * punctuation (`-----`, `==`) has a closing mark, past which more text is not
 * more of the value.
 */
function lengthened(value: string, count: number): string {
  const alnum = [...value].filter(isAlnum);
  let tail = "";
  for (let index = 0; index < count; index++)
    tail += alnum[index % alnum.length];
  return value + tail;
}

/** [name, the value's own text, the form written], for one value. */
function* variants(
  value: string,
  escaped: boolean,
): Generator<[string, string, string]> {
  yield ["as is", value, value];
  if (isAlnum(value[value.length - 1])) {
    for (const count of LENGTHS) {
      const longer = lengthened(value, count);
      yield [`lengthened by ${count}`, longer, longer];
    }
    if (escaped) {
      const longer = value + "%2F" + lengthened(value, 8).slice(value.length);
      yield ["lengthened by an escape", longer, longer];
    }
  }
  if (!value.includes("\n")) return;
  const bodies: [string, string][] = [["whole", value]];
  const lines = value.split("\n");
  if (lines.length > 2)
    bodies.push(["cut before its last line", lines.slice(0, -1).join("\n")]);
  for (const [bodyName, body] of bodies) {
    for (const prefix of PREFIXES) {
      if (prefix) {
        const lined = body
          .split("\n")
          .map((line) => prefix + line)
          .join("\n");
        yield [
          `${bodyName}, lines after ${JSON.stringify(prefix)}`,
          body,
          lined,
        ];
      }
    }
    yield [`${bodyName}, escaped newlines`, body, body.replaceAll("\n", "\\n")];
    yield [
      `${bodyName}, newlines escaped twice`,
      body,
      body.replaceAll("\n", "\\\\n"),
    ];
    yield [`${bodyName}, CRLF escaped`, body, body.replaceAll("\n", "\\r\\n")];
    yield [`${bodyName}, trailing spaces`, body, body.replaceAll("\n", "  \n")];
    yield [
      `${bodyName}, HTML line breaks`,
      body,
      body.replaceAll("\n", "<br>"),
    ];
    yield [`${bodyName}, on one line`, body, body.replaceAll("\n", " ")];
    // Past every internal bound: a line of 70,000 characters, 5,000 lines.
    const cut = body.indexOf("\n");
    const head = body.slice(0, cut);
    const rest = body.slice(cut + 1);
    const wide = head + "\n" + lengthened("A", 70_000) + "\n" + rest;
    yield [`${bodyName}, a line past 65,536 characters`, wide, wide];
    const line = rest
      .split("\n")
      .reduce((best, item) => (item.length > best.length ? item : best)); // a line of the key itself
    const tall = head + "\n" + Array(5000).fill(line).join("\n") + "\n" + rest;
    yield [
      `${bodyName}, past 4,096 lines`,
      tall,
      tall
        .split("\n")
        .map((line) => "> " + line)
        .join("\n"),
    ];
    if (bodyName !== "whole") yield [bodyName, body, body];
  }
}

/** [name, value's own text, text to clean, the frame around the value]. */
function* contexts(
  lead: string,
  value: string,
  trail: string,
  escaped = false,
): Generator<[string, string, string, string]> {
  const frame = lead + "\u0000" + trail;
  for (const [name, own, written] of variants(value, escaped))
    yield [name, own, lead + written + trail, frame];
  for (const prefix of PREFIXES.slice(1)) {
    yield [
      `after ${JSON.stringify(prefix)}`,
      value,
      prefix + lead + value + trail,
      frame + "\u0000" + prefix,
    ];
  }
  // Right after the value: what is written next to it is what may cut it. A
  // trail that belongs to the value's form (the @host of a connection string)
  // stays next to it, and the suffix comes after.
  const joined = trail.startsWith("@") || trail.startsWith(":");
  for (const suffix of SUFFIXES.slice(1)) {
    for (const after of [trail, ""]) {
      const text = joined
        ? lead + value + trail + suffix
        : lead + value + suffix + after;
      const name =
        `before ${JSON.stringify(suffix)}` + (after ? "" : ", nothing after");
      yield [name, value, text, frame + "\u0000" + suffix];
    }
  }
}

/** Runs of 4 characters of the value left in the output, framing aside. */
function leaks(own: string, output: string, frame: string): string[] {
  const bare = output.replace(PLACEHOLDER, "\u0000");
  const grams = (text: string): Set<string> => {
    const set = new Set<string>();
    for (let start = 0; start + 4 <= text.length; start++)
      set.add(text.slice(start, start + 4));
    return set;
  };
  const left = grams(bare);
  const framed = grams(frame);
  const found: string[] = [];
  for (const line of own.split("\n")) {
    for (let start = 0; start + 4 <= line.length; start++) {
      const gram = line.slice(start, start + 4);
      if (left.has(gram) && !framed.has(gram) && !found.includes(gram))
        found.push(gram);
    }
  }
  return found;
}

for (const { detector, lead, value, trail } of POSITIVES) {
  test(`a counted value leaves nothing behind: ${detector}: ${JSON.stringify(value.slice(0, 24))}`, () => {
    const cleaner = new ContentCleaner({
      schema: 1,
      detectors: { [detector]: "mask" },
    });
    const failures: string[] = [];
    for (const [name, own, text, frame] of contexts(
      lead,
      value,
      trail,
      ESCAPED.has(detector),
    )) {
      const result = cleaner.clean(text);
      if (!result.counts[detector]) continue;
      const left = leaks(own, result.payload as string, frame);
      if (left.length > 0)
        failures.push(`${name}: ${JSON.stringify(left.slice(0, 3))}`);
    }
    assert.deepEqual(failures.slice(0, 8), []);
  });
}

/**
 * `value` between `prefix` and `suffix`, as `content-oracle.json` says. A lead
 * that ends with anything but a blank (`postgres://`, `Email:`), or with an
 * authorization scheme (`Bearer `), is part of the value's form: the prefix
 * goes before it. A trail that starts with `@` or `:` is too.
 */
function place(
  lead: string,
  value: string,
  trail: string,
  prefix: string,
  suffix: string,
): string {
  const form =
    lead !== "" &&
    (!" \t\n".includes(lead[lead.length - 1]) ||
      /[=:][ \t\n]*$/.test(lead) ||
      /(?:bearer|basic) $/i.test(lead));
  const left = form ? prefix + lead : lead + prefix;
  const right =
    trail.startsWith("@") || trail.startsWith(":")
      ? trail + suffix
      : suffix + trail;
  return left + value + right;
}

for (const { detector, lead, value, trail } of POSITIVES) {
  // The floor: a context the detector skips is a failure, not a pass.
  test(`a value in a neutral context is counted: ${detector}: ${JSON.stringify(value.slice(0, 24))}`, () => {
    const cleaner = new ContentCleaner({
      schema: 1,
      detectors: { [detector]: "mask" },
    });
    const skipped: string[] = [];
    for (const prefix of FLOOR.prefixes) {
      for (const suffix of FLOOR.suffixes) {
        const text = place(lead, value, trail, prefix, suffix);
        if (!cleaner.clean(text).counts[detector])
          skipped.push(
            `${JSON.stringify(prefix)} + value + ${JSON.stringify(suffix)}`,
          );
      }
    }
    assert.deepEqual(
      skipped.slice(0, 12),
      [],
      `${skipped.length} contexts skipped`,
    );
  });
}

test("the property sees every detector and a partial mask", () => {
  const seen = new Set(POSITIVES.map((positive) => positive.detector));
  const rules = JSON.parse(
    readFileSync(join(descriptors, "content-rules.json"), "utf8"),
  ) as { detectors: { id: string }[] };
  assert.deepEqual(
    [...seen].sort(),
    rules.detectors.map((detector) => detector.id).sort(),
  );
  assert.deepEqual(leaks("AbCdEfGh", "[X_1]EfGh", ""), ["EfGh"]);
  assert.deepEqual(leaks("AbCdEfGh", "[X_1]", ""), []);
  assert.deepEqual(leaks("AbCd", "key AbCd", "key AbCd"), []);
});
