import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { ContentPolicyError, IDSideError } from "../src/exceptions.ts";
import {
  buildDigest,
  buildDigestOf,
  CATALOGUE_IDS,
  catalogueProblem,
  challengeResponse,
  clean,
  ContentCleaner,
  contentFields,
  contentPolicyDigest,
  referencePolicy,
  rulesDigest,
  verifyClean,
} from "../src/cleaning/index.ts";
import { buildDigestAt, buildFiles, challengeDigest } from "../src/cleaning/digests.ts";
import { resolveCandidates } from "../src/cleaning/engine.ts";
import { codePoints, compilePolicy } from "../src/cleaning/policy.ts";
import { LIMITS, RULES } from "../src/cleaning/rules.ts";
import { fold, mapCodePoint, shadowOf } from "../src/cleaning/shadow.ts";
import * as v from "../src/cleaning/validators.ts";

// The engine beyond the shared vectors: validators arm by arm, the shadow's
// offset map, limits, digests, and linear time on adversarial input. The same
// cases as packages/python/tests/test_content_engine.py, so each validator is
// held in both languages. Identifiers are fictitious.

const here = dirname(fileURLToPath(import.meta.url));
const REFERENCE = referencePolicy();

// ── validators ──────────────────────────────────────────────────────────────

test("luhn reference numbers", () => {
  assert.ok(v.luhn("79927398713"));
  assert.ok(!v.luhn("79927398710"));
});

const CARDS: [string, boolean][] = [
  ["4111111111111111", true],
  ["4111111111111112", false],
  ["4222222222222", true],
  ["411111111111116", false],
  ["5555555555554444", true],
  ["2221000000000009", true],
  ["2720999999999996", true],
  ["2721000000000004", false],
  ["378282246310005", true],
  ["3782822463100050", false],
  ["6011111111111117", true],
  ["9111111111111113", false],
];
for (const [number, ok] of CARDS) {
  test(`card ${number}`, () => assert.equal(v.card(number) !== undefined, ok));
}

const IBANS: [string, number | undefined][] = [
  ["FR7630006000011234567890189", 27],
  ["fr7630006000011234567890189", 27],
  ["FR76 3000 6000 0112 3456 7890 189", 33],
  ["BE68 5390 0754 7034 ABCD", 19],
  ["BE68539007547034ABCD", undefined], // pragma: allowlist secret
  ["FR7630006000011234567890188", undefined],
  ["XX7630006000011234567890189", undefined],
  ["FR763000600001123456789", undefined],
  ["FR293000600001123456", undefined],
];
for (const [value, expected] of IBANS) {
  test(`iban ${value}`, () => assert.equal(v.iban(value), expected));
}

test("bic country", () => {
  assert.equal(v.bic("EXMPFRPPXXX"), 11);
  assert.equal(v.bic("EXMPXXPP"), undefined);
});

function nirOf(first13: string): string {
  const body = first13.replace("2A", "19").replace("2B", "18");
  return first13 + String(97 - (Number(body) % 97)).padStart(2, "0");
}

const NIR_BODIES = ["1850578006084", "269052A123456", "178122B045012"]; // pragma: allowlist secret
for (const first13 of NIR_BODIES) {
  test(`nir key ${first13}`, () => {
    const good = nirOf(first13);
    assert.equal(v.nir(good), 15);
    const wrong = String((Number(good.slice(-2)) % 97) + 1).padStart(2, "0");
    assert.equal(v.nir(good.slice(0, -2) + wrong), undefined);
  });
}

test("nir: corsica substitution is not symmetric, shapes are checked", () => {
  assert.equal(v.nir(nirOf(NIR_BODIES[1]).replace("2A", "2B")), undefined);
  assert.equal(v.nir("1 85 05 78 006 084"), undefined);
  assert.equal(v.nir("185052C00608400"), undefined);
  assert.equal(v.nir("18505780060840A"), undefined);
  assert.equal(v.nir("185057800606909"), 15);
  assert.equal(v.nir("18505780060699"), undefined);
});

test("siren and siret", () => {
  assert.equal(v.siren("123456782"), 9);
  assert.equal(v.siren("123 456 782"), 11);
  assert.equal(v.siren("123456783"), undefined);
  assert.equal(v.siren("12345678"), undefined);
  assert.equal(v.siret("12345678200010"), 14);
  assert.equal(v.siret("12345678200011"), undefined);
  assert.equal(v.siret("12345678300010"), undefined);
  assert.ok(v.luhn("12345678010005"));
  assert.equal(v.siret("12345678010005"), undefined);
});

test("siret: the La Poste rule", () => {
  assert.equal(v.siret("35600000009075"), 14);
  assert.ok(!v.luhn("35600000009075"));
  assert.equal(v.siret("35600000049838"), undefined);
  assert.equal(v.siret("35600000000048"), 14);
  assert.equal(v.siret("35600000000049"), undefined);
});

const VATS: [string, boolean][] = [
  ["FR11123456782", true],
  ["FR 11 123 456 782", true],
  ["FR12123456782", false],
  ["FR11123456783", false],
  ["FRA0123456782", true],
  ["FR0L123456782", true],
  ["FR1M123456782", true],
  ["FRA1123456782", false],
  ["FR53000004605", true],
  ["FR1112345678", false],
];
for (const [value, ok] of VATS) {
  test(`fr vat ${value}`, () => assert.equal(v.frVat(value) !== undefined, ok));
}

const IPS: [string, boolean][] = [
  ["192.168.1.10", true],
  // A prefix is not the validator's: the engine takes it with the address.
  ["10.0.0.0/24", false],
  ["10.0.0.0/33", false],
  ["10.0.0.0/", false],
  ["256.1.1.1", false],
  ["1.2.3", false],
  ["2001:db8::1", true],
  ["::1", false], // loopback, and a slice of code: no group of 3+ digits
  ["1::2", false],
  ["fe80::1", true],
  ["abc::1", true],
  ["::", false],
  ["abc::def", false],
  ["1:2:3:4:5:6:7:8", true],
  ["1:2:3:4:5:6:7", false],
  ["1:2:3:4:5:6:7::8", false],
  ["2001::db8::1", false],
  ["2001:db8:12345::1", false],
  ["::ffff:192.0.2.128", true],
  ["::ffff:192.0.2.256", false],
  ["1:2:3:4:5:6:1.2.3.4", true],
  ["1:2:3:4:5:6:7:1.2.3.4", false],
];
for (const [value, ok] of IPS) {
  test(`ip ${value}`, () => assert.equal(v.ip(value) !== undefined, ok));
}

test("phone and plate", () => {
  assert.equal(v.phone("06 12 34 56 78"), 14);
  assert.equal(v.phone("+12 345"), undefined);
  assert.equal(v.phone("+1 415 555 0123"), 15);
  assert.equal(v.phone("0033 6 12 34 56 78"), 18);
  assert.equal(v.phone("00336123"), undefined);
  assert.equal(v.phone("+1234567890123456"), undefined);
  assert.equal(v.phone("+12 500 000"), undefined); // eight digits: an amount
  assert.equal(v.phone("+12 500 0001"), 12); // nine digits
  assert.equal(v.phone("0012 500 0001"), 13);
  assert.equal(v.phone("0012 500 000"), undefined);
  assert.equal(v.phone("(+33) 6 12 34 56 78"), 19);
  assert.equal(v.plate("AB-123-CD"), 9);
  assert.equal(v.plate("AB-000-CD"), undefined);
  assert.equal(v.plate("AB-123-CD-69"), 12);
  assert.equal(v.plate("AB-000-CD-69"), undefined); // the department is not the plate
  assert.equal(v.plate("AB-001-CD"), 9);
  assert.equal(v.iban("FR76-3000-6000-0112-3456-7890-189"), 33);
  assert.equal(v.iban("FR 76 3000 6000 0112 3456 7890 189"), 34);
  assert.equal(v.iban("BE68-5390-0754-7034-ABCD"), 19); // cut at the country length
  assert.equal(v.iban("BE68-5390-0754-7034ABCD"), undefined); // glued: not cut
  assert.equal(v.nir("1-85-05-78-006-084-91"), 21);
  assert.equal(v.nir("1-85-05-78-006-084-92"), undefined);
});

// ── shadow ──────────────────────────────────────────────────────────────────

test("shadow of ASCII shares offsets", () => {
  const shadow = shadowOf("Hello WORLD");
  assert.equal(shadow.text, "Hello WORLD");
  assert.equal(shadow.lower, "hello world");
  assert.equal(shadow.start(3), 3);
  assert.equal(shadow.end(5), 5);
});

test("a ligature maps back to one character, an invisible one to nothing", () => {
  const shadow = shadowOf("a\ufb01b\u200bc");
  assert.equal(shadow.text, "afibc");
  assert.deepEqual([0, 1, 2, 3, 4].map((index) => shadow.start(index)), [0, 1, 1, 2, 4]);
  assert.deepEqual([1, 2, 3, 4, 5].map((end) => shadow.end(end)), [1, 2, 2, 3, 5]);
});

test("an astral character is one shadow character and two original units", () => {
  const shadow = shadowOf("\u00c9l\u00e8ve \u{1F600}x");
  assert.equal(shadow.text, "Eleve \ufffdx");
  assert.equal(shadow.start(6), 6);
  assert.equal(shadow.end(7), 8);
  assert.equal(shadow.start(7), 8);
});

test("the lowered shadow lowers ASCII only", () => {
  assert.equal(fold("\u00c9COLE \u03a9mega \u1e9e"), "ecole \u03a9mega \u1e9e");
});

test("code point mapping", () => {
  assert.equal(mapCodePoint(0x200b), "");
  assert.equal(mapCodePoint(0xe9), "e");
  assert.equal(mapCodePoint(0x2013), "-");
  assert.equal(mapCodePoint(0x2019), "'");
  assert.equal(mapCodePoint(0x1f600), "\ufffd");
  assert.equal(mapCodePoint(0xff4d), "m");
  assert.equal(mapCodePoint(0x1d400), "A");
  assert.equal(mapCodePoint(0x1d6), "u");
});

test("overlap is judged on original characters", () => {
  const shadow = shadowOf("\ufb01");
  const rule = (key: string, order: number) => ({
    key,
    label: "X",
    action: "mask",
    order,
    patterns: [],
    validator: undefined,
    lowered: false,
  });
  const kept = resolveCandidates(
    [
      { start: 1, end: 2, rule: rule("custom:B", 1) },
      { start: 0, end: 1, rule: rule("custom:A", 0) },
    ],
    shadow,
  );
  assert.deepEqual(
    kept.map((candidate) => candidate.rule.key),
    ["custom:A"],
  );
});

test("an overlap is absorbed under the best-ranked candidate", () => {
  // A short candidate bridging two kept ones joins them into one span, under
  // the one ranked first (the longest), whichever side it stands on.
  const shadow = shadowOf("x".repeat(30));
  const rule = (key: string, order: number) => ({
    key,
    label: "X",
    action: "mask" as const,
    order,
    patterns: [],
    validator: undefined,
    lowered: false,
  });
  const left = { start: 0, end: 8, rule: rule("custom:LEFT", 0) };
  const right = { start: 10, end: 30, rule: rule("custom:RIGHT", 1) };
  const bridge = { start: 7, end: 11, rule: rule("custom:BRIDGE", 2) };
  const kept = resolveCandidates([bridge, left, right], shadow);
  assert.deepEqual(
    kept.map((candidate) => [candidate.start, candidate.end, candidate.rule.key]),
    [[0, 30, "custom:RIGHT"]],
  );
  // Disjoint candidates stay apart, each its own.
  const apart = resolveCandidates([left, right], shadow);
  assert.deepEqual(
    apart.map((candidate) => [candidate.start, candidate.end]),
    [
      [0, 8],
      [10, 30],
    ],
  );
  assert.equal(apart[0], left);
});

test("code points are counted as Python counts them", () => {
  assert.equal(codePoints("a\u{1F600}b"), 3);
  assert.equal(codePoints("\ud800x"), 2); // a lone surrogate is one
  assert.equal(codePoints("x\udc00\ud800"), 3);
});

// ── policies and the public helpers ─────────────────────────────────────────

test("a policy error is an IDSideError and names no value", () => {
  const policy = { schema: 1, custom: [{ kind: "term", value: "Op\u00e9ration Nordlys", action: "mask", label: "bad label" }] };
  assert.throws(
    () => new ContentCleaner(policy),
    (error: unknown) =>
      error instanceof ContentPolicyError &&
      error instanceof IDSideError &&
      error.name === "ContentPolicyError" &&
      !error.message.includes("Nordlys") &&
      error.message.includes("custom entry 0"),
  );
});

for (const policy of [null, [], "x", { schema: true }, { schema: 2 }, { schema: 1, detectors: null }]) {
  test(`a policy that is not a schema 1 object is refused: ${JSON.stringify(policy)}`, () => {
    assert.throws(() => new ContentCleaner(policy), ContentPolicyError);
  });
}

test("domain and pattern normalisation", () => {
  const cleaner = new ContentCleaner({
    schema: 1,
    custom: [
      { kind: "domain", value: "*.Acme.Example.", action: "mask", label: "D" },
      { kind: "pattern", value: "N\u00b0#\u200b#", action: "mask", label: "P" },
      { kind: "pattern", value: "#-x", action: "block", label: "Q" },
      { kind: "pattern", value: "V2.#", action: "mask", label: "V" },
    ],
  });
  assert.equal(cleaner.clean("mail@ACME.example N\u00b012").payload, "[D_1] [P_1]");
  assert.deepEqual(cleaner.clean("ref 7-X").blocked, { "custom:Q": 1 });
  assert.equal(cleaner.clean("v2.3 and V2.33").payload, "[V_1] and V2.33");
  assert.throws(
    () => new ContentCleaner({ schema: 1, custom: [{ kind: "pattern", value: "\u200b", action: "mask", label: "P" }] }),
    ContentPolicyError,
  );
});

test("the body must be JSON", () => {
  assert.throws(() => clean({ a: 10n }, REFERENCE, null), TypeError);
  assert.throws(() => clean({ a: () => 1 }, REFERENCE, null), TypeError);
});

test("an undefined property is absent, as JSON writes it", () => {
  const result = clean({ a: undefined, b: "marie@example.com" }, REFERENCE, null);
  assert.deepEqual(result.payload, { a: undefined, b: "[EMAIL_1]" });
});

test("untouched branches are the same objects, and nothing is mutated", () => {
  const untouched = { deep: ["a", "b"] };
  const body = { model: "m", keep: untouched, messages: [{ content: "marie@example.com" }] };
  const result = clean(body, REFERENCE, "openai");
  assert.equal((result.payload as typeof body).keep, untouched);
  assert.equal(body.messages[0].content, "marie@example.com");
  assert.equal(clean(untouched, REFERENCE, "openai").payload, untouched);
});

test("an unknown provider is refused", () => {
  assert.throws(() => contentFields("nope"), /unknown provider/);
});

test("verifyClean", () => {
  assert.equal(verifyClean("hello", REFERENCE, { provider: null }), true);
  assert.equal(verifyClean("marie@example.com", REFERENCE, { provider: null }), false);
  assert.equal(verifyClean("[EMAIL_1]", REFERENCE, { provider: null }), true);
  assert.equal(verifyClean("4111111111111111", { schema: 1, detectors: { credit_card: "block" } }, { provider: null }), false);
});

test("a policy that is not JSON is compiled only to be refused", () => {
  assert.throws(() => clean("x", { schema: 1, detectors: { email: Number.NaN } }, null), ContentPolicyError);
});

test("the cleaner cache keeps at most 32 compiled policies", () => {
  for (let revision = 0; revision < 40; revision++) {
    assert.equal(clean("marie@example.com", { ...REFERENCE, revision }, null).payload, "[EMAIL_1]");
  }
});

test("the pass limit blocks instead of leaving a non fixed point", () => {
  const text = "SIREN " + "a".repeat(60) + "@example.com 123456782";
  assert.deepEqual({ ...clean(text, REFERENCE, null).counts }, { email: 1, fr_siren: 1 });
  const saved = LIMITS.max_passes;
  LIMITS.max_passes = 1;
  try {
    assert.deepEqual(new ContentCleaner(REFERENCE).clean(text).blocked, { size_limit: 1 });
  } finally {
    LIMITS.max_passes = saved;
  }
});

test("the reference policy masks every detector", () => {
  assert.deepEqual(REFERENCE.detectors, Object.fromEntries(CATALOGUE_IDS.map((id) => [id, "mask"])));
  assert.equal(CATALOGUE_IDS.length, 34);
});

test("the catalogue is for this engine", () => {
  assert.equal(catalogueProblem(), undefined);
  const saved = RULES.engine;
  RULES.engine = "2";
  try {
    assert.match(catalogueProblem() ?? "", /engine "2"/);
  } finally {
    RULES.engine = saved;
  }
});

// ── digests ─────────────────────────────────────────────────────────────────

test("the rules digest is the file's bytes", () => {
  const bytes = readFileSync(join(here, "..", "src", "content-rules.json"));
  assert.equal(rulesDigest(), "sha256:" + createHash("sha256").update(bytes).digest("hex"));
});

test("the build digest frames each file, sorted by path", () => {
  const sha = (data: string) => "sha256:" + createHash("sha256").update(data).digest("hex");
  assert.equal(buildDigestOf([]), sha(""));
  const one = buildDigestOf([["a", Buffer.from("bc")]]);
  assert.equal(one, sha("a\u00002\u0000bc"));
  assert.notEqual(one, buildDigestOf([["ab", Buffer.from("c")]]));
  assert.equal(
    buildDigestOf([
      ["b", Buffer.from("1")],
      ["a", Buffer.from("2")],
    ]),
    buildDigestOf([
      ["a", Buffer.from("2")],
      ["b", Buffer.from("1")],
    ]),
  );
});

test("the build digest covers the cleaning code and the descriptors", () => {
  const src = join(here, "..", "src");
  const expected: [string, Uint8Array][] = [
    ...readdirSync(join(src, "cleaning"))
      .filter((name) => name.endsWith(".ts"))
      .map((name): [string, Uint8Array] => [`src/cleaning/${name}`, readFileSync(join(src, "cleaning", name))]),
    ...readdirSync(join(src, "providers"))
      .filter((name) => name.endsWith(".json"))
      .map((name): [string, Uint8Array] => [`src/providers/${name}`, readFileSync(join(src, "providers", name))]),
  ];
  const names = new Set(buildFiles().map(([name]) => name));
  for (const name of ["src/cleaning/engine.ts", "src/cleaning/validators.ts", "src/providers/openai.json"]) {
    assert.ok(names.has(name), name);
  }
  assert.equal(buildDigest(), buildDigestOf(expected));
});

test("the build files of a compiled module are its .js siblings and the descriptors", () => {
  const root = mkdtempSync(join(tmpdir(), "idside-build-"));
  try {
    mkdirSync(join(root, "dist", "cleaning"), { recursive: true });
    mkdirSync(join(root, "dist", "providers"));
    for (const name of ["engine.js", "engine.d.ts", "engine.js.map", "digests.js"]) {
      writeFileSync(join(root, "dist", "cleaning", name), name);
    }
    writeFileSync(join(root, "dist", "providers", "openai.json"), "{}");
    writeFileSync(join(root, "dist", "providers", "README.md"), "#");
    const url = pathToFileURL(join(root, "dist", "cleaning", "digests.js")).href;
    assert.deepEqual(buildFiles(url).map(([name]) => name).sort(), [
      "dist/cleaning/digests.js",
      "dist/cleaning/engine.js",
      "dist/providers/openai.json",
    ]);
    assert.equal(buildDigestAt(url), buildDigestOf(buildFiles(url)));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("an unreadable build is declared as no file at all", () => {
  assert.equal(buildDigestAt("file:///nonexistent/dist/cleaning/digests.js"), buildDigestOf([]));
});

test("the policy digest is JCS", () => {
  const expected = createHash("sha256").update('{"a":[1,2],"b":"\u00e9","schema":1}').digest("hex");
  assert.equal(contentPolicyDigest({ schema: 1, b: "\u00e9", a: [1, 2] }), "sha256:" + expected);
  assert.throws(() => contentPolicyDigest({ x: Number.NaN }));
});

test("challenge response", () => {
  const expected = challengeDigest("tok", ["[EMAIL_1]", "plain"]);
  assert.equal(challengeResponse("tok", ["marie@example.com", "plain"]), expected);
  assert.equal(expected, "sha256:" + createHash("sha256").update("tok\n[EMAIL_1]\nplain").digest("hex"));
  assert.throws(() => challengeResponse("tok", ["x", 1 as unknown as string]), TypeError);
});

// ── linear time ─────────────────────────────────────────────────────────────

const PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----";
const ADVERSARIAL: Record<string, (n: number) => string> = {
  letters: (n) => "a".repeat(n),
  digits: (n) => "1".repeat(n),
  "digit groups": (n) => "1111 ".repeat(n / 5),
  "at signs": (n) => "a@".repeat(n / 2),
  "dotted labels": (n) => "a.".repeat(n / 2),
  "email chain": (n) => "a@a.".repeat(n / 4),
  colons: (n) => "0:".repeat(n / 2),
  "hex groups": (n) => "ab:".repeat(Math.floor(n / 3)),
  "pem headers": (n) => PEM_BEGIN.repeat(Math.floor(n / 27)),
  "pem body": (n) => PEM_BEGIN + "A".repeat(n),
  "key prefixes": (n) => "sk-".repeat(Math.floor(n / 3)),
  "jwt parts": (n) => "eyJ" + "a.".repeat(n / 2),
  assignments: (n) => "password=".repeat(Math.floor(n / 9)),
  urls: (n) => "https://".repeat(n / 8),
  connection: (n) => "postgres://a:".repeat(Math.floor(n / 13)),
  "plus groups": (n) => "+33 ".repeat(n / 4),
  "iban letters": (n) => "FR76 ABCD ".repeat(n / 10),
  "placeholder-ish": (n) => "[A_1".repeat(n / 4),
  keywords: (n) => "siren 123 ".repeat(n / 10),
  accents: (n) => "\u00e91".repeat(n / 2),
  invisible: (n) => "a\u200b".repeat(n / 2),
  // Shapes of the rules widened after the end-to-end review.
  "pem escaped lines": (n) => PEM_BEGIN + ("\\n" + "A".repeat(62)).repeat(Math.floor(n / 64)),
  "pem quoted lines": (n) => PEM_BEGIN + ("\n> " + "A".repeat(61)).repeat(Math.floor(n / 64)),
  "pem dashes": (n) => PEM_BEGIN + "----a".repeat(Math.floor(n / 5)),
  "pem short parts": (n) => PEM_BEGIN + (" " + "A".repeat(15)).repeat(Math.floor(n / 16)),
  "version words": (n) => "version ".repeat(Math.floor(n / 8)),
  "version chains": (n) => "version 1.2.3.".repeat(Math.floor(n / 14)),
  "open quotes": (n) => ('password: "' + "a".repeat(2000)).repeat(Math.floor(n / 2011)),
  "escaped quotes": (n) => 'password: "' + '\\"'.repeat(Math.floor(n / 2)),
  "bearer words": (n) => ("Bearer " + "a".repeat(5000)).repeat(Math.floor(n / 5007)),
  "double-spaced digits": (n) => "1111  ".repeat(Math.floor(n / 6)),
  "iban dashes": (n) => "FR 76-".repeat(Math.floor(n / 6)),
  "cyrillic at signs": (n) => "\u044f@".repeat(Math.floor(n / 2)),
  // Shapes of the second pass: a key cut before its END line, quoted headers,
  // passwords holding @, values past every bound.
  "pem cut then prose": (n) => PEM_BEGIN + "\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC" + " lorem".repeat(Math.floor(n / 6)),
  "pem quoted headers": (n) => (PEM_BEGIN + " lorem ipsum ").repeat(Math.floor(n / 40)),
  "at chains": (n) => "postgres://u:" + "p@".repeat(Math.floor(n / 2)),
  "long assignment": (n) => "password=" + "a".repeat(n),
  "plus amounts": (n) => "+150 000 000 ".repeat(Math.floor(n / 13)),
  "pem quoted lines with an end": (n) =>
    PEM_BEGIN + ("\n> " + "A".repeat(61)).repeat(Math.floor(n / 64)) + "\n> -----END " + "PRIVATE KEY-----",
  "pem one long line with an end": (n) => PEM_BEGIN + "\n" + "A".repeat(n) + "\n-----END " + "PRIVATE KEY-----",
  // Shapes of the third pass: values followed by the engine to their end,
  // escapes written in two characters, readings that back off.
  "slack prefixes": (n) => "xoxb-".repeat(Math.floor(n / 5)),
  escapes: (n) => "\\n".repeat(Math.floor(n / 2)),
  "escaped addresses": (n) => "\\na@b.co ".repeat(Math.floor(n / 9)),
  "later at signs": (n) => "postgres://u:p@" + "a@".repeat(Math.floor(n / 2)),
  "long parts between at signs": (n) => ("redis://:p@" + "a".repeat(5000)).repeat(Math.floor(n / 5011)),
  "domain labels run on": (n) => "a@b.co" + ".x".repeat(Math.floor(n / 2)),
  "ipv6 runs": (n) => "1:2:3:4:5:6:7:8:".repeat(Math.floor(n / 16)),
  "glued numbers": (n) => "+33 6 12 34 56 78a ".repeat(Math.floor(n / 19)),
  "pem cut keys": (n) => (PEM_BEGIN + "\n" + "A".repeat(40) + "\n").repeat(Math.floor(n / 69)),
  // Shapes of the fourth pass: header lines and blanks after a BEGIN line,
  // two short runs, escapes in tokens, glued passwords, // credentials.
  "pem header lines and blanks": (n) =>
    (PEM_BEGIN + ("\nComment: x" + " ".repeat(512)).repeat(4) + "\n").repeat(Math.floor(n / 2100)),
  "pem header words": (n) => (PEM_BEGIN + ("\nComment: " + "a b ".repeat(60)).repeat(4) + "\n").repeat(Math.floor(n / 1030)),
  "pem two runs and blanks": (n) =>
    (PEM_BEGIN + " " + "A".repeat(20) + " ".repeat(1000) + "B".repeat(11) + "\n").repeat(Math.floor(n / 1060)),
  "url escapes": (n) => "xoxd-" + "%2F".repeat(Math.floor(n / 3)),
  "glued passwords": (n) => ("m@example.com:" + "x".repeat(30) + " ").repeat(Math.floor(n / 45)),
  "schemeless credentials": (n) => "//a:b@ ".repeat(Math.floor(n / 7)),
};

const TIMING_POLICY = {
  schema: 1,
  detectors: Object.fromEntries(CATALOGUE_IDS.map((id) => [id, "mask"])),
  custom: [
    { kind: "term", value: "projet atlas", action: "mask", label: "T" },
    { kind: "domain", value: "acme.example", action: "mask", label: "D" },
    { kind: "pattern", value: "*-*-*-#*-@", action: "mask", label: "P" },
  ],
};

function bestOf(cleaner: ContentCleaner, text: string, runs = 3): number {
  let best = Number.POSITIVE_INFINITY;
  for (let run = 0; run < runs; run++) {
    const began = performance.now();
    cleaner.clean(text);
    best = Math.min(best, performance.now() - began);
  }
  return best;
}

for (const name of Object.keys(ADVERSARIAL).sort()) {
  test(`cleaning time grows linearly on adversarial input: ${name}`, () => {
    // Four times the input may cost at most eight times the time: linear growth
    // predicts four, a quadratic engine sixteen.
    const cleaner = new ContentCleaner(TIMING_POLICY);
    const small = ADVERSARIAL[name](16_000);
    const large = ADVERSARIAL[name](64_000);
    const floor = 2; // milliseconds: below this a timer measures the machine
    assert.ok(bestOf(cleaner, large) <= 8 * Math.max(bestOf(cleaner, small), floor));
  });
}

test("one megabyte of adversarial input stays under seconds", () => {
  const cleaner = new ContentCleaner(TIMING_POLICY);
  const text = Object.keys(ADVERSARIAL)
    .sort()
    .map((name) => ADVERSARIAL[name](40_000))
    .join("");
  assert.ok(text.length > 800_000);
  assert.ok(bestOf(cleaner, text, 1) < 20_000);
});

// Every rule alone, against a megabyte of each adversarial shape: a rule whose
// pattern backtracks would show here even if the full policy hid it behind
// another. A quadratic one would take hours on a megabyte, so a bound of
// seconds per run leaves a slow machine room and still fails it.
const MEGABYTE = 1_048_576;
const ONE_RULE: Record<string, Record<string, unknown>> = {
  ...Object.fromEntries(CATALOGUE_IDS.map((id) => [id, { schema: 1, detectors: { [id]: "mask" } }])),
  ...Object.fromEntries(TIMING_POLICY.custom.map((entry) => [`custom ${entry.kind}`, { schema: 1, custom: [entry] }])),
};
for (const rule of Object.keys(ONE_RULE).sort()) {
  test(`one megabyte of each adversarial input per rule: ${rule}`, () => {
    const cleaner = new ContentCleaner(ONE_RULE[rule]);
    for (const name of Object.keys(ADVERSARIAL).sort()) {
      const text = ADVERSARIAL[name](MEGABYTE);
      assert.ok(text.length > MEGABYTE * 0.9, name);
      const began = performance.now();
      cleaner.clean(text);
      const elapsed = performance.now() - began;
      assert.ok(elapsed < 5_000, `${rule} on ${name}: ${elapsed.toFixed(0)} ms`);
    }
  });
}

// ── convergence on random input ─────────────────────────────────────────────

const FUZZ_TOKENS = [
  "a", "Z", "1", "0", "@", ".", "-", ":", "/", "+", " ", "[", "]", "_", "=", "'",
  '"', "\n", "\u00e9", "\u200b", "sk-", "sk_live_", PEM_BEGIN, "-----END " + "PRIVATE KEY-----",
  "x@y.zz", "4111111111111111", "4111 ", "FR76", "3000 ", "06 12 34 56 78", "+33",
  "siren", "123 456 782", "https://", "postgres://u:p@h", "Bearer ", "password=", "eyJ",
  "[EMAIL_1]", "[A_", "10.0.0.1", "::", "ab:", "AB-123-CD", "AKIA", "mistral", "bic",
  "TESTDEFF", "acme.example", "projet atlas", "PRJ-1-2-3a", "fe80::1", "00:1a:2b:3c:4d:5e",
  "GB82WEST12345698765432",
  // Shapes of the rules widened after the end-to-end review.
  "version ", "v", "4.2.1.3", "\\n", "> ", "-----END " + "PRIVATE KEY-----",
  "Authorization: ", "Basic ", "(+33) ", "06/12/", "  ", "\u20ac", "\u043c@\u0440\u0444",
  "\u044f", "git@github.com:", "@2x.png", "xapp-1-", "glffct-", "PGPASS" + "WORD=",
  "Password=\x27", "os.environ", "mot de passe : ", "_KEY=", "R.C.S.", "FR 76-",
];

/** A small deterministic generator (mulberry32), so a failure can be replayed. */
function generator(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

for (const [label, policy] of [
  ["reference", REFERENCE],
  ["custom", TIMING_POLICY],
] as const) {
  test(`random text converges and stays put: ${label}`, () => {
    const random = generator(20261116);
    const cleaner = new ContentCleaner(policy);
    for (let round = 0; round < 3000; round++) {
      const size = 1 + Math.floor(random() * 40);
      let text = "";
      for (let index = 0; index < size; index++) text += FUZZ_TOKENS[Math.floor(random() * FUZZ_TOKENS.length)];
      const result = cleaner.clean(text);
      assert.deepEqual({ ...result.blocked }, {}, JSON.stringify(text));
      const again = cleaner.clean(result.payload);
      assert.deepEqual([again.payload, { ...again.counts }], [result.payload, {}], JSON.stringify(text));
    }
  });
}

// ── patterns that run only under Block (`block_only`) ─────────────────────

const QUOTED_HEADER = "A key file starts with -----BEGIN " + "PRIVATE KEY----- and a dashed line.";

test("a block_only pattern runs only when its detector blocks", () => {
  const patterns = (action: string): boolean[] => {
    const compiled = compilePolicy({ schema: 1, detectors: { private_key: action } });
    assert.equal(compiled.rules.length, 1);
    return compiled.rules[0]!.patterns.map((pattern) => pattern.blockOnly);
  };
  assert.deepEqual(patterns("block"), [false, false, true]);
  assert.deepEqual(patterns("mask"), [false, false]);
});

test("a quoted key header is left under mask and blocks under block", () => {
  const result = new ContentCleaner({ schema: 1, detectors: { private_key: "mask" } }).clean(QUOTED_HEADER); // pragma: allowlist secret
  assert.deepEqual([result.payload, { ...result.counts }, { ...result.blocked }], [QUOTED_HEADER, {}, {}]);
  const blocked = new ContentCleaner({ schema: 1, detectors: { private_key: "block" } }).clean(QUOTED_HEADER); // pragma: allowlist secret
  assert.deepEqual({ ...blocked.blocked }, { private_key: 1 });
});
