import { test } from "node:test";
import assert from "node:assert/strict";
import { canonicalizeToString, canonicalize, CanonicalizationError } from "../src/jcs.ts";

const canon = (value: unknown): string => canonicalizeToString(value);

test("sorts object keys so argument order cannot change the output", () => {
  assert.equal(canon({ b: 1, a: 2 }), '{"a":2,"b":1}');
  // Non-ASCII: "z" (U+007A) sorts before "é" (U+00E9) by code unit.
  assert.equal(canon({ "é": 1, z: 2 }), '{"z":2,"é":1}');
});

test("follows ECMAScript Number::toString, not a fixed notation", () => {
  const cases: [number, string][] = [
    [0, "0"],
    [-0, "0"],
    [1, "1"],
    [4.5, "4.5"],
    [0.002, "0.002"],
    [1e30, "1e+30"],
    [1e21, "1e+21"],
    [1e-27, "1e-27"],
    [0.000001, "0.000001"],
    [1e-7, "1e-7"],
    [100, "100"],
    [-5.5, "-5.5"],
    [-1e-7, "-1e-7"],
    [9007199254740992, "9007199254740992"],
  ];
  for (const [value, expected] of cases) {
    assert.equal(canon(value), expected, `String form of ${expected}`);
  }
});

test("emits raw UTF-8 for non-ASCII, never u-escaped", () => {
  assert.equal(canon("Café 日本語 😀"), '"Café 日本語 😀"');
});

test("applies JSON string escaping and leaves the solidus bare", () => {
  assert.equal(canon("a\nb"), '"a\\nb"');
  assert.equal(canon("a\tb"), '"a\\tb"');
  assert.equal(canon('q"q'), '"q\\"q"');
  assert.equal(canon("back\\slash"), '"back\\\\slash"');
  assert.equal(canon("bell"), '"\\u0007bell"'); // control char -> 
  assert.equal(canon("a/b"), '"a/b"'); // RFC 8785 does not escape '/'
});

test("preserves null, booleans, empty string / object / array", () => {
  assert.equal(canon({ a: null, b: true, c: false, d: "", e: {}, f: [] }), '{"a":null,"b":true,"c":false,"d":"","e":{},"f":[]}');
});

test("accepts an integer exactly at the ±2^53 boundary", () => {
  assert.equal(canon(2 ** 53), "9007199254740992");
  assert.equal(canon(-(2 ** 53)), "-9007199254740992");
});

test("accepts exponential-form large floats but refuses non-exponential huge integers", () => {
  // 1e30 prints in exponential form → identical across SDKs → accepted.
  assert.equal(canon(1e30), "1e+30");
  // A whole number > 2^53 printed as plain digits cannot be exact in a JS double,
  // and the Python SDK rejects it; fail closed rather than hash a rounded value.
  assert.throws(() => canonicalize(1e16), CanonicalizationError);
  assert.throws(() => canonicalize(2 ** 53 + 2), CanonicalizationError);
});

test("refuses values it cannot canonicalize rather than guessing", () => {
  assert.throws(() => canonicalize(Number.NaN), CanonicalizationError);
  assert.throws(() => canonicalize(Number.POSITIVE_INFINITY), CanonicalizationError);
  assert.throws(() => canonicalize(10n as unknown), CanonicalizationError); // bigint
  assert.throws(() => canonicalize(undefined), CanonicalizationError);
  assert.throws(() => canonicalize(new Date()), CanonicalizationError); // non-plain object
  assert.throws(() => canonicalize(() => 0), CanonicalizationError);
  // Lone surrogates cannot encode to UTF-8; Python rejects them, so we must too.
  assert.throws(() => canonicalize("lone\ud800surrogate"), CanonicalizationError);
  assert.throws(() => canonicalize({ "\udc00": 1 }), CanonicalizationError);
});
