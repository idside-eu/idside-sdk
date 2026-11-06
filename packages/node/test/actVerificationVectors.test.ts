/**
 * Replays the shared ACT verification vectors against the Node verifier.
 *
 * The vectors live in `packages/descriptors/testdata/act-verification-vectors.json`
 * and are shared by every SDK. They exist because "the token verifies" must mean
 * the same thing everywhere: an auditor who checks a proof with this SDK and a
 * customer who checks the same proof with the Python one have to reach the same
 * verdict, or the proof is not a proof — it is an opinion that depends on the
 * library you happened to install.
 *
 * Each case pins the *reason* too, not merely accept/reject. Both SDKs use the
 * same error class names on purpose, so a divergence in taxonomy fails here
 * rather than surprising somebody during an incident review.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { DEFAULT_LEEWAY_SECONDS, PRODUCTION_ISSUER, verifyAct } from "../src/index.ts";

interface Vector {
  readonly name: string;
  readonly token: string;
  readonly options: Record<string, unknown>;
  /** Overrides the file-level key set, for cases whose subject IS the key set. */
  readonly jwks?: { readonly keys: readonly Record<string, unknown>[] };
  readonly claims?: Record<string, unknown>;
  readonly expect: string;
  readonly why: string;
}

interface VectorFile {
  readonly now: number;
  readonly jwks: { readonly keys: readonly Record<string, unknown>[] };
  readonly options_defaults: Record<string, unknown>;
  readonly cases: readonly Vector[];
}

/** Every field a case may carry. An unknown one means this harness is stale. */
const CASE_FIELDS = new Set(["name", "token", "options", "expect", "why", "jwks", "claims"]);

const VECTORS_PATH = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "descriptors",
  "testdata",
  "act-verification-vectors.json",
);

const raw = readFileSync(VECTORS_PATH, "utf8");
const vectors = JSON.parse(raw) as VectorFile;

// The vectors are language-neutral (snake_case); this is the only place that
// knows how they map onto this SDK's option names.
function optionsFor(vector: Vector): Record<string, unknown> {
  for (const field of Object.keys(vector)) {
    // Same reasoning as the option guard below, one level up: a case field this
    // harness does not know about would be ignored in silence, and the case
    // would still run — pinning something other than what it was written to pin.
    assert.ok(CASE_FIELDS.has(field), `vector '${vector.name}' carries field '${field}', which this harness ignores`);
  }
  // Pinned claims are read back only from a token that verifies: on a case
  // expected to fail they would be ignored in silence, like an unknown field.
  assert.ok(
    !("claims" in vector) || vector.expect === "valid",
    `vector '${vector.name}' pins claims but is expected to fail, so this harness never reads them`,
  );
  // A case may bring its own key set — that is how the poisoned-JWKS case says
  // "the token is fine, the keys are not".
  // These fixed tokens were minted before `mode` entered the ACT claim set.
  // Their legacy status is explicit in the shared file; the verifier default
  // remains strict for newly minted proofs.
  const options: Record<string, unknown> = {
    jwks: vector.jwks ?? vectors.jwks,
    now: vectors.now,
    allowLegacyWithoutMode: vectors.options_defaults.allow_legacy_without_mode,
  };
  const map: Record<string, string> = {
    allow_test_issuer: "allowTestIssuer",
    request_hash: "requestHash",
    nonce: "nonce",
    app_id: "appId",
    subject: "subject",
  };

  for (const name of Object.keys(vector.options)) {
    // A key this SDK does not know about must not be dropped on the floor: the
    // case would then pass while testing something other than what it pins.
    const to = map[name];
    assert.ok(to, `vector '${vector.name}' uses option '${name}', which this SDK does not map`);
    options[to] = vector.options[name];
  }
  return options;
}

for (const vector of vectors.cases) {
  test(`vector: ${vector.name}`, async () => {
    if (vector.expect === "valid") {
      const verified = await verifyAct(vector.token, optionsFor(vector) as never);
      // The key set actually used, not the file-level one: a valid case is
      // allowed to bring its own, and then that is the kid to expect.
      assert.equal(verified.keyId, (vector.jwks ?? vectors.jwks).keys[0]!.kid);
      // A case may pin claims too: what the verifier hands back must be what
      // was signed, nested values included.
      for (const [name, expected] of Object.entries(vector.claims ?? {})) {
        assert.deepEqual(verified.claims[name], expected, vector.why);
      }
      return;
    }

    await assert.rejects(
      () => verifyAct(vector.token, optionsFor(vector) as never),
      (error: Error) => {
        assert.equal(error.constructor.name, vector.expect, vector.why);
        return true;
      },
    );
  });
}

test("the shared defaults are this SDK's defaults", () => {
  // Every case is written against these. If the shared file moved its default
  // leeway or issuer and this SDK did not, the cases would still pass here while
  // meaning something different in each SDK — the divergence the vectors exist
  // to catch, hiding in the one field no case states explicitly.
  assert.equal(vectors.options_defaults.issuer, PRODUCTION_ISSUER);
  assert.equal(vectors.options_defaults.leeway, DEFAULT_LEEWAY_SECONDS);
  assert.equal(vectors.options_defaults.allow_test_issuer, false);
  assert.equal(vectors.options_defaults.allow_legacy_without_mode, true);
});

test("the vector file is not silently empty", () => {
  // A file that failed to load would make every case above vacuously pass.
  assert.ok(vectors.cases.length >= 20);
  assert.ok(new Set(vectors.cases.map((c) => c.expect)).size > 1);
});

// Members that make a JWK a *private* key: `d` is the EC private scalar, the
// rest are the RSA and symmetric equivalents.
const PRIVATE_JWK_MEMBERS = new Set(["d", "k", "p", "q", "dp", "dq", "qi"]);
const PUBLIC_JWK_MEMBERS = new Set(["kty", "crv", "x", "y", "kid", "use", "alg", "key_ops"]);

test("the verification key set is public only", () => {
  // This repository never contains a signing key, test vectors included. The key
  // that produced these tokens was generated to write the file and then
  // destroyed; anything private in the file-level key set would mean these
  // tokens could be minted at will by anyone who cloned the repo.
  assert.ok(!raw.includes("PRIVATE KEY"));

  for (const jwk of vectors.jwks.keys) {
    for (const member of Object.keys(jwk)) {
      assert.ok(PUBLIC_JWK_MEMBERS.has(member), `unexpected JWK member ${member}`);
      assert.ok(!PRIVATE_JWK_MEMBERS.has(member), `private JWK member ${member} in the key set`);
    }
  }
});

test("private material appears only in cases that must be rejected", () => {
  // One vector deliberately carries a poisoned key set. Refusing a JWKS that
  // contains a private key is itself a rule worth pinning, so a vector has to be
  // able to present one. What must never happen is private material in a case
  // the SDKs are expected to ACCEPT — that would be a usable signing key wearing
  // a test's clothes. Hence: not "no `d` anywhere", but "`d` only where the
  // expected outcome is rejection".
  for (const vector of vectors.cases) {
    // A case may carry a JWK SET or a single bare JWK — both verifiers accept
    // the second shape as a one-key set. Looking only inside `keys` let a bare
    // JWK carrying `d` through, and `.gitleaks.toml` exempts this whole file on
    // the strength of this check, so it was the only control left.
    const supplied = vector.jwks as { keys?: Record<string, unknown>[] } | undefined;
    const candidates = supplied
      ? "keys" in supplied
        ? (supplied.keys ?? [])
        : [supplied as unknown as Record<string, unknown>]
      : [];
    for (const jwk of candidates) {
      if (Object.keys(jwk).some((member) => PRIVATE_JWK_MEMBERS.has(member))) {
        assert.notEqual(
          vector.expect,
          "valid",
          `vector '${vector.name}' carries private key material but is expected to verify`,
        );
      }
    }
  }
});
