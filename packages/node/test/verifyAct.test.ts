/**
 * Local (offline) verification of ACT tokens.
 *
 * The key pairs below are generated in-process for the duration of the test run:
 * throwaway test-only material, never committed, never a real signing key. The
 * SDK itself only ever verifies — see the "no private keys here" invariant.
 *
 * Behaviour is mirrored on the Python SDK's `tests/test_verify_act.py`: same
 * checks, same order, same error taxonomy.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac, createPublicKey, generateKeyPairSync, sign as cryptoSign } from "node:crypto";
import { verifyAct } from "../src/act.ts";
import { USER_AGENT } from "../src/transport.ts";
import {
  ActAlgorithmError,
  ActBindingMismatchError,
  ActExpiredError,
  ActIssuerError,
  ActKeyNotFoundError,
  ActMalformedError,
  ActNotYetValidError,
  ActPrivateKeyInKeySetError,
  ActSignatureError,
  ActTypeMismatchError,
  ActVerificationError,
  ActVerificationUnavailableError,
  ConsentTokenError,
  IDSideError,
} from "../src/exceptions.ts";

const KID = "act-2026q3";
/** Fixed reference clock so the fixtures never drift. */
const NOW = 1_784_936_000;
const REQUEST_HASH = `sha256:${"a".repeat(64)}`;

const { publicKey, privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
const { publicKey: otherPublicKey, privateKey: otherPrivateKey } = generateKeyPairSync("ec", {
  namedCurve: "P-256",
});

const JWK = { ...publicKey.export({ format: "jwk" }), kid: KID, alg: "ES256", use: "sig" };
const OTHER_JWK = { ...otherPublicKey.export({ format: "jwk" }), kid: "act-2026q4", alg: "ES256" };
const JWKS = { keys: [JWK] };

function b64u(input: string | Buffer): string {
  return Buffer.from(input).toString("base64url");
}

function claims(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    iss: "idside.eu",
    sub: "usr_pseudo_marie_456",
    app_id: "app_medhelp",
    iat: NOW - 60,
    exp: NOW + 3540,
    jti: "act_01j9xk2m7q",
    consent_state: { training: false },
    legal_basis: "consent",
    source: "widget",
    provider: "openai",
    model: "gpt-4o-mini",
    request_hash: REQUEST_HASH,
    nonce: "nonce_01j9xk2m7q",
    binding: "ubt",
    mode: "live",
    ...overrides,
  };
}

/** Build a signed token. A header override set to `undefined` removes the field. */
function makeToken(
  payload: unknown = claims(),
  headerOverrides: Record<string, unknown> = {},
  key = privateKey,
): string {
  const header: Record<string, unknown> = { alg: "ES256", typ: "act+jwt", kid: KID, ...headerOverrides };
  for (const [name, value] of Object.entries(headerOverrides)) {
    if (value === undefined) delete header[name];
  }
  const signingInput = `${b64u(JSON.stringify(header))}.${b64u(JSON.stringify(payload))}`;
  const signature = cryptoSign("sha256", Buffer.from(signingInput), { key, dsaEncoding: "ieee-p1363" });
  return `${signingInput}.${signature.toString("base64url")}`;
}

/** Correctly sign an arbitrary payload segment — even one that is not JSON. */
function makeTokenFromSegment(payloadSegment: string): string {
  const signingInput = `${b64u(JSON.stringify({ alg: "ES256", typ: "act+jwt", kid: KID }))}.${payloadSegment}`;
  const signature = cryptoSign("sha256", Buffer.from(signingInput), {
    key: privateKey,
    dsaEncoding: "ieee-p1363",
  });
  return `${signingInput}.${signature.toString("base64url")}`;
}

/** Header and payload with an arbitrary (possibly invalid) signature segment. */
function unsignedToken(payload: unknown, signature: string, headerOverrides: Record<string, unknown> = {}): string {
  const header = { alg: "ES256", typ: "act+jwt", kid: KID, ...headerOverrides };
  return `${b64u(JSON.stringify(header))}.${b64u(JSON.stringify(payload))}.${signature}`;
}

/** A token whose payload segment ends on a partial 4-character group (slack bits, padding). */
function tokenWithPayloadSlack(): string {
  let filler = "";
  let token = makeToken(claims({ filler }));
  while (token.split(".")[1].length % 4 === 0) {
    filler += "x";
    token = makeToken(claims({ filler }));
  }
  return token;
}

const OPTIONS = { jwks: JWKS, now: NOW };

// --- happy path ---------------------------------------------------------

test("verifies a well-formed ACT token and returns what it attests", async () => {
  const proof = await verifyAct(makeToken(), OPTIONS);
  assert.equal(proof.issuer, "idside.eu");
  assert.equal(proof.subject, "usr_pseudo_marie_456");
  assert.equal(proof.appId, "app_medhelp");
  assert.equal(proof.tokenId, "act_01j9xk2m7q");
  assert.equal(proof.issuedAt, NOW - 60);
  assert.equal(proof.expiresAt, NOW + 3540);
  assert.deepEqual(proof.consentState, { training: false });
  assert.equal(proof.legalBasis, "consent");
  assert.equal(proof.source, "widget");
  assert.equal(proof.provider, "openai");
  assert.equal(proof.model, "gpt-4o-mini");
  assert.equal(proof.requestHash, REQUEST_HASH);
  assert.equal(proof.nonce, "nonce_01j9xk2m7q");
  assert.equal(proof.binding, "ubt");
  assert.equal(proof.mode, "live");
  assert.equal(proof.keyId, KID);
});

test("returns test mode as a typed proof field", async () => {
  assert.equal((await verifyAct(makeToken(claims({ mode: "test" })), OPTIONS)).mode, "test");
});

test("rejects an invalid ACT mode", async () => {
  for (const mode of ["production", "", 7, false, null, {}]) {
    await assert.rejects(verifyAct(makeToken(claims({ mode })), OPTIONS), ActMalformedError);
  }
});

test("requires an explicit opt-in for a legacy proof without mode", async () => {
  const token = makeToken(claims({ mode: undefined }));

  await assert.rejects(verifyAct(token, OPTIONS), ActMalformedError);
  assert.equal((await verifyAct(token, { ...OPTIONS, allowLegacyWithoutMode: true })).mode, undefined);
});

test("rejects an ACT validity window longer than one hour", async () => {
  const token = makeToken(claims({ iat: NOW - 60, exp: NOW + 3541 }));
  await assert.rejects(verifyAct(token, OPTIONS), ActMalformedError);
});

test("returns the claims as signed, including ones the SDK does not know about", async () => {
  const proof = await verifyAct(makeToken(claims({ ledger_seq: 4212 })), OPTIONS);
  assert.equal(proof.claims.ledger_seq, 4212);
  assert.equal(proof.claims.request_hash, REQUEST_HASH);
  assert.throws(() => {
    (proof.claims as Record<string, unknown>).source = "forged";
  }, TypeError);
});

// An Observe proof: the call went out, and the token says what did not apply.
// `deviations` is a claim added under `/v1`, so a verifier released before it
// must accept it and keep it: refusing it would make every flagged call
// unverifiable, and dropping it would hide the one thing an auditor reads such
// a proof for.
test("a proof flagged by its organization verifies with its deviations", async () => {
  const deviations = [
    { kind: "provider_not_allowed" },
    { kind: "option_not_enforced", option: "training.foundation" },
    { kind: "basis_not_established" },
  ];
  const token = makeToken(
    claims({
      source: "default_policy",
      legal_basis: "not_established",
      binding: "user_id",
      deviations,
    }),
  );

  const proof = await verifyAct(token, OPTIONS);

  assert.deepEqual(proof.claims.deviations, deviations);
  assert.equal(proof.legalBasis, "not_established");
});

// The verifier reads `legal_basis` as an opaque string, as the spec says. A
// proof signed on a basis added after this release must still verify: an
// enumeration here would make every such proof unverifiable, with no SDK release
// able to arrive first.
test("a legal basis added later verifies as signed", async () => {
  for (const legalBasis of ["public_task", "legal_obligation", "vital_interests", "a_basis_added_later"]) {
    const token = makeToken(claims({ source: "default_policy", legal_basis: legalBasis, binding: "user_id" }));

    assert.equal((await verifyAct(token, OPTIONS)).legalBasis, legalBasis);
  }
});

test("accepts a single JWK as the key set", async () => {
  const proof = await verifyAct(makeToken(), { jwks: JWK, now: NOW });
  assert.equal(proof.keyId, KID);
});

test("accepts an optional nbf that is already in the past", async () => {
  const proof = await verifyAct(makeToken(claims({ nbf: NOW - 30 })), OPTIONS);
  assert.equal(proof.claims.nbf, NOW - 30);
});

// --- validity window ----------------------------------------------------

test("rejects an expired token", async () => {
  await assert.rejects(
    verifyAct(makeToken(claims({ iat: NOW - 7200, exp: NOW - 3600 })), OPTIONS),
    ActExpiredError,
  );
});

test("tolerates a small clock skew on exp (60 s leeway by default)", async () => {
  const almost = makeToken(claims({ exp: NOW - 30 }));
  assert.equal((await verifyAct(almost, OPTIONS)).expiresAt, NOW - 30);
  await assert.rejects(verifyAct(almost, { ...OPTIONS, leeway: 0 }), ActExpiredError);
});

test("rejects a token that is not valid yet (nbf in the future)", async () => {
  await assert.rejects(verifyAct(makeToken(claims({ nbf: NOW + 300 })), OPTIONS), ActNotYetValidError);
});

test("rejects a token issued in the future (iat beyond the leeway)", async () => {
  await assert.rejects(verifyAct(makeToken(claims({ iat: NOW + 300 })), OPTIONS), ActNotYetValidError);
});

test("rejects an inverted or empty validity window", async () => {
  await assert.rejects(verifyAct(makeToken(claims({ iat: NOW, exp: NOW })), OPTIONS), ActMalformedError);
  await assert.rejects(verifyAct(makeToken(claims({ iat: NOW, exp: NOW - 1 })), OPTIONS), ActMalformedError);
});

test("rejects timestamps that are not integer epochs instead of coercing them", async () => {
  for (const exp of [`${NOW + 3600}`, NOW + 3600.5, true, null]) {
    await assert.rejects(verifyAct(makeToken(claims({ exp })), OPTIONS), ActMalformedError, `exp ${String(exp)}`);
  }
  await assert.rejects(verifyAct(makeToken(claims({ nbf: "soon" })), OPTIONS), ActMalformedError);
});

// --- header: algorithm and type ----------------------------------------

test("rejects alg: none before looking at the signature (alg confusion)", async () => {
  await assert.rejects(verifyAct(unsignedToken(claims(), "", { alg: "none" }), OPTIONS), ActAlgorithmError);
  // ... and the same forgery carrying a plausible-looking signature segment.
  await assert.rejects(
    verifyAct(unsignedToken(claims(), b64u("x".repeat(64)), { alg: "none" }), OPTIONS),
    ActAlgorithmError,
  );
});

test("rejects HS256 signed with the public key as the HMAC secret (alg confusion)", async () => {
  const header = b64u(JSON.stringify({ alg: "HS256", typ: "act+jwt", kid: KID }));
  const payload = b64u(JSON.stringify(claims()));
  const secret = publicKey.export({ type: "spki", format: "pem" }) as string;
  const mac = createHmac("sha256", secret).update(`${header}.${payload}`).digest("base64url");
  await assert.rejects(verifyAct(`${header}.${payload}.${mac}`, OPTIONS), ActAlgorithmError);

  // The JWK serialization is just as plausible a guess for the shared secret.
  const jwkMac = createHmac("sha256", JSON.stringify(JWK)).update(`${header}.${payload}`).digest("base64url");
  await assert.rejects(verifyAct(`${header}.${payload}.${jwkMac}`, OPTIONS), ActAlgorithmError);
});

test("rejects any algorithm other than ES256, including stronger ECDSA curves", async () => {
  for (const alg of ["ES384", "ES512", "RS256", "PS256", "EdDSA", "es256", undefined]) {
    await assert.rejects(
      verifyAct(makeToken(claims(), { alg }), OPTIONS),
      ActAlgorithmError,
      `alg ${String(alg)} must be rejected`,
    );
  }
});

test("rejects a token with an unsupported crit header", async () => {
  await assert.rejects(verifyAct(makeToken(claims(), { crit: ["exp"] }), OPTIONS), ActMalformedError);
});

test("rejects a UBT presented as an ACT (typ: ubt+jwt)", async () => {
  await assert.rejects(verifyAct(makeToken(claims(), { typ: "ubt+jwt" }), OPTIONS), ActTypeMismatchError);
});

test("rejects a plain JWT and a token with no typ at all", async () => {
  await assert.rejects(verifyAct(makeToken(claims(), { typ: "JWT" }), OPTIONS), ActTypeMismatchError);
  await assert.rejects(verifyAct(makeToken(claims(), { typ: undefined }), OPTIONS), ActTypeMismatchError);
});

// --- key selection ------------------------------------------------------

test("rejects an unknown kid without trying the other keys (fail-closed)", async () => {
  // The key set holds the very key that signed the token, under a different kid.
  const renamed = { keys: [{ ...JWK, kid: "act-2026q1" }] };
  await assert.rejects(verifyAct(makeToken(), { ...OPTIONS, jwks: renamed }), ActKeyNotFoundError);
  await assert.rejects(verifyAct(makeToken(), { ...OPTIONS, jwks: { keys: [] } }), ActKeyNotFoundError);
});

test("rejects a token whose header carries no kid", async () => {
  await assert.rejects(verifyAct(makeToken(claims(), { kid: undefined }), OPTIONS), ActMalformedError);
  await assert.rejects(verifyAct(makeToken(claims(), { kid: "" }), OPTIONS), ActMalformedError);
});

test("selects the right key when the key set holds several", async () => {
  const proof = await verifyAct(makeToken(), { ...OPTIONS, jwks: { keys: [OTHER_JWK, JWK] } });
  assert.equal(proof.tokenId, "act_01j9xk2m7q");
});

test("refuses an ambiguous key set (two keys sharing a kid)", async () => {
  const ambiguous = { keys: [JWK, { ...OTHER_JWK, kid: KID }] };
  await assert.rejects(verifyAct(makeToken(), { ...OPTIONS, jwks: ambiguous }), ActVerificationUnavailableError);
});

test("reports private key material in the key set as an incident of its own", async () => {
  const leaks: Array<Record<string, unknown>> = [
    { d: "not-a-real-private-scalar" }, // EC (and RSA) private scalar
    { k: "not-a-real-symmetric-key" }, // symmetric key
    { kty: "RSA", d: "not-a-real-private-exponent" }, // the leak outranks the wrong key type
  ];
  for (const leak of leaks) {
    const failure = await verifyAct(makeToken(), {
      ...OPTIONS,
      jwks: { keys: [{ ...JWK, ...leak }] },
    }).catch((error: unknown) => error);

    assert.ok(failure instanceof ActPrivateKeyInKeySetError, JSON.stringify(leak));
    assert.ok(failure instanceof ActVerificationError);
    // The property a later refactor could silently undo: a caller whose
    // ActKeyNotFoundError handler refreshes the key set and retries must NOT
    // catch this, or it loops against a poisoned set during the incident.
    assert.ok(
      !(failure instanceof ActKeyNotFoundError),
      "a leaked key must not be catchable as a key-lookup failure",
    );
    assert.match(failure.message, /rotate the signing key now/i);
    assert.match(failure.message, /NOT accepted/);
  }
});

test("reports a symmetric key set entry as a leak, not as the wrong key type", async () => {
  // No x/y at all: the 'not an EC key' branch would otherwise claim this one.
  const symmetric = { keys: [{ kty: "oct", kid: KID, k: "not-a-real-symmetric-key" }] };
  await assert.rejects(
    verifyAct(makeToken(), { ...OPTIONS, jwks: symmetric }),
    ActPrivateKeyInKeySetError,
  );
});

test("rejects a JWK that cannot verify an ES256 signature", async () => {
  const unusable: Array<Record<string, unknown>> = [
    { kty: "RSA" },
    { crv: "P-384" },
    { alg: "ES384" },
    { use: "enc" },
    { key_ops: ["encrypt"] },
    { x: undefined },
    { y: 42 },
    { x: b64u(Buffer.alloc(31)) }, // a P-256 coordinate is exactly 32 bytes
    { x: "not-base64url!" },
    { x: b64u(Buffer.alloc(32)), y: b64u(Buffer.alloc(32)) }, // not a point on the curve
  ];
  for (const bad of unusable) {
    await assert.rejects(
      verifyAct(makeToken(), { ...OPTIONS, jwks: { keys: [{ ...JWK, ...bad }] } }),
      ActKeyNotFoundError,
      `JWK ${JSON.stringify(bad)} must be rejected`,
    );
  }
});

test("rejects a key set that is not a JWKS at all", async () => {
  for (const jwks of [{ keys: {} }, "not-a-key-set", 42]) {
    await assert.rejects(
      verifyAct(makeToken(), { ...OPTIONS, jwks: jwks as never }),
      ActVerificationUnavailableError,
    );
  }
});

// --- signature ----------------------------------------------------------

test("rejects a tampered payload", async () => {
  const [header, , signature] = makeToken().split(".");
  const forged = b64u(JSON.stringify(claims({ consent_state: { training: true } })));
  await assert.rejects(verifyAct(`${header}.${forged}.${signature}`, OPTIONS), ActSignatureError);
});

test("rejects a signature produced by another key", async () => {
  await assert.rejects(verifyAct(makeToken(claims(), {}, otherPrivateKey), OPTIONS), ActSignatureError);
});

test("rejects a signature that is not 64 raw bytes (DER-encoded, truncated or absent)", async () => {
  const der = cryptoSign("sha256", Buffer.from("whatever"), privateKey).toString("base64url");
  await assert.rejects(verifyAct(unsignedToken(claims(), der), OPTIONS), ActSignatureError);
  await assert.rejects(verifyAct(unsignedToken(claims(), b64u("short")), OPTIONS), ActSignatureError);
  await assert.rejects(verifyAct(unsignedToken(claims(), ""), OPTIONS), ActSignatureError);
});

// --- issuer -------------------------------------------------------------

test("rejects an unknown issuer", async () => {
  await assert.rejects(verifyAct(makeToken(claims({ iss: "idside.eu.attacker.test" })), OPTIONS), ActIssuerError);
  await assert.rejects(verifyAct(makeToken(claims({ iss: "https://idside.eu" })), OPTIONS), ActIssuerError);
});

test("rejects the test issuer by default", async () => {
  await assert.rejects(verifyAct(makeToken(claims({ iss: "test.idside.eu" })), OPTIONS), ActIssuerError);
});

test("accepts the test issuer only on an explicit opt-in", async () => {
  const devToken = makeToken(claims({ iss: "test.idside.eu" }));
  const proof = await verifyAct(devToken, { ...OPTIONS, allowTestIssuer: true });
  assert.equal(proof.issuer, "test.idside.eu");
  // The opt-in adds the test issuer, it does not replace the production one.
  assert.equal((await verifyAct(makeToken(), { ...OPTIONS, allowTestIssuer: true })).issuer, "idside.eu");
});

test("honours a custom issuer allow-list", async () => {
  const proof = await verifyAct(makeToken(claims({ iss: "idside.example" })), {
    ...OPTIONS,
    issuer: "idside.example",
  });
  assert.equal(proof.issuer, "idside.example");
  await assert.rejects(verifyAct(makeToken(), { ...OPTIONS, issuer: "idside.example" }), ActIssuerError);
  await assert.rejects(verifyAct(makeToken(), { ...OPTIONS, issuer: [] }), TypeError);
  await assert.rejects(verifyAct(makeToken(), { ...OPTIONS, issuer: [""] }), TypeError);
});

// --- encoding strictness ------------------------------------------------

test("rejects a token that is not a three-part compact JWS", async () => {
  const [header, payload, signature] = makeToken().split(".");
  for (const bad of [
    "",
    "not-a-token",
    `${header}.${payload}`,
    `${header}.${payload}.${signature}.extra`,
    `.${payload}.${signature}`,
    `${header}..${signature}`,
  ]) {
    await assert.rejects(verifyAct(bad, OPTIONS), ActMalformedError, `"${bad.slice(0, 24)}…" must be rejected`);
  }
  await assert.rejects(verifyAct(undefined as never, OPTIONS), ActMalformedError);
  await assert.rejects(verifyAct(`${header}.${payload}.${"A".repeat(9000)}`, OPTIONS), ActMalformedError);
});

// Word for word what Python says: the cap that refused the token, and the bound
// the spec sets, which is not the cap.
test("a token past the cap names the cap and the spec's bound", async () => {
  const oversized = `${"A".repeat(8_193)}.eyJ9.sig`;

  await assert.rejects(verifyAct(oversized, OPTIONS), (error: unknown) => {
    assert.ok(error instanceof ActMalformedError);
    assert.equal(
      error.message,
      `The token is implausibly large (${oversized.length} characters, limit 8192); ` +
        "an ACT token is at most 3,072 characters. Refusing to parse it.",
    );
    return true;
  });
});

test("rejects standard base64 padding and alphabet instead of coercing", async () => {
  const [header, payload, signature] = tokenWithPayloadSlack().split(".");
  const padded = Buffer.from(payload, "base64url").toString("base64"); // padded, '+' and '/' allowed
  assert.ok(padded.endsWith("="), "the fixture must exercise real base64 padding");
  await assert.rejects(verifyAct(`${header}.${padded}.${signature}`, OPTIONS), ActMalformedError);
  await assert.rejects(verifyAct(`${header}.${payload}==.${signature}`, OPTIONS), ActMalformedError);
  await assert.rejects(verifyAct(`${header}.${payload.slice(0, -1)}+.${signature}`, OPTIONS), ActMalformedError);
  await assert.rejects(verifyAct(`${header}.${payload.slice(0, -1)}/.${signature}`, OPTIONS), ActMalformedError);
  await assert.rejects(verifyAct(`${header}.${payload.slice(0, -1)} .${signature}`, OPTIONS), ActMalformedError);
});

test("rejects a non-canonical base64url segment (unused trailing bits set)", async () => {
  // Derived rather than written out: the literal is a high-entropy string that
  // trips secret scanners, and one fewer permanent scanner exception is worth
  // three lines. Same reasoning as the Python suite.
  const letters = (from: number, count: number) =>
    Array.from({ length: count }, (_, i) => String.fromCharCode(from + i)).join("");
  const alphabet = `${letters(65, 26)}${letters(97, 26)}${letters(48, 10)}-_`;
  // A final group shorter than 4 characters leaves slack bits to flip.
  const [header, payload, signature] = tokenWithPayloadSlack().split(".");
  const flipped = alphabet[alphabet.indexOf(payload[payload.length - 1]) ^ 1];
  const mutated = payload.slice(0, -1) + flipped;
  assert.deepEqual(
    Buffer.from(mutated, "base64url"),
    Buffer.from(payload, "base64url"),
    "the mutated segment must decode to the same bytes",
  );
  await assert.rejects(verifyAct(`${header}.${mutated}.${signature}`, OPTIONS), ActMalformedError);
});

test("rejects a payload that is not a JSON object, however well it is signed", async () => {
  for (const payload of [["not", "an", "object"], "usr_marie_456", 42, null]) {
    await assert.rejects(verifyAct(makeToken(payload), OPTIONS), ActMalformedError, JSON.stringify(payload));
  }
  await assert.rejects(verifyAct(makeTokenFromSegment(b64u("{not json")), OPTIONS), ActMalformedError);
  await assert.rejects(verifyAct(makeTokenFromSegment(b64u("\xff\xfe")), OPTIONS), ActMalformedError);
});

// --- required claims ----------------------------------------------------

test("rejects a token missing any claim the proof rests on", async () => {
  const required = [
    "iss",
    "sub",
    "app_id",
    "iat",
    "exp",
    "jti",
    "consent_state",
    "legal_basis",
    "source",
    "provider",
    "model",
    "request_hash",
    "nonce",
    "binding",
  ];
  for (const name of required) {
    const partial = claims();
    delete partial[name];
    await assert.rejects(verifyAct(makeToken(partial), OPTIONS), ActMalformedError, `missing ${name}`);
  }
});

test("rejects claims of the wrong type or shape", async () => {
  for (const overrides of [
    { sub: "" },
    { provider: 42 },
    { binding: null },
    { consent_state: "training=false" },
    { consent_state: [] },
    { request_hash: "a".repeat(64) },
    { request_hash: `sha256:${"A".repeat(64)}` },
    { request_hash: `sha512:${"a".repeat(64)}` },
  ]) {
    await assert.rejects(
      verifyAct(makeToken(claims(overrides)), OPTIONS),
      ActMalformedError,
      JSON.stringify(overrides),
    );
  }
});

// --- request binding ----------------------------------------------------

test("checks the request_hash binding when the caller pins one", async () => {
  const token = makeToken();
  assert.equal((await verifyAct(token, { ...OPTIONS, requestHash: REQUEST_HASH })).requestHash, REQUEST_HASH);
  await assert.rejects(
    verifyAct(token, { ...OPTIONS, requestHash: `sha256:${"b".repeat(64)}` }),
    ActBindingMismatchError,
  );
});

test("checks the nonce, the subject and the app when the caller pins them", async () => {
  const token = makeToken();
  await verifyAct(token, {
    ...OPTIONS,
    nonce: "nonce_01j9xk2m7q",
    subject: "usr_pseudo_marie_456",
    appId: "app_medhelp",
  });
  await assert.rejects(verifyAct(token, { ...OPTIONS, nonce: "nonce_other" }), ActBindingMismatchError);
  await assert.rejects(verifyAct(token, { ...OPTIONS, subject: "usr_pseudo_paul_789" }), ActBindingMismatchError);
  await assert.rejects(verifyAct(token, { ...OPTIONS, appId: "app_other" }), ActBindingMismatchError);
});

test("checks the bindings only after the signature, never before", async () => {
  // A pinned mismatch must not become an oracle for unsigned tokens.
  await assert.rejects(
    verifyAct(makeToken(claims(), {}, otherPrivateKey), { ...OPTIONS, nonce: "nonce_other" }),
    ActSignatureError,
  );
});

// --- key source ---------------------------------------------------------

test("fetches the key set when given a URL", async () => {
  let requested = "";
  let init: RequestInit | undefined;
  const proof = await verifyAct(makeToken(), {
    now: NOW,
    jwksUrl: "https://api.idside.example/.well-known/jwks.json",
    fetch: async (url, options) => {
      requested = String(url);
      init = options;
      return new Response(JSON.stringify(JWKS), { headers: { "content-type": "application/json" } });
    },
  });
  assert.equal(requested, "https://api.idside.example/.well-known/jwks.json");
  assert.equal(init?.redirect, "error", "a redirected key set is a key set from somewhere else");
  assert.equal(
    new Headers(init?.headers).get("user-agent"),
    USER_AGENT,
    "the key-set host must be able to name the SDK version fetching it, as the cloud can",
  );
  assert.equal(proof.provider, "openai");
});

test("needs exactly one key source", async () => {
  await assert.rejects(verifyAct(makeToken(), { now: NOW }), TypeError);
  await assert.rejects(
    verifyAct(makeToken(), { ...OPTIONS, jwksUrl: "https://api.idside.example/jwks.json" }),
    TypeError,
  );
});

test("refuses a key-set URL that is not https", async () => {
  for (const jwksUrl of ["http://api.idside.example/jwks.json", "file:///etc/jwks.json", "not a url"]) {
    await assert.rejects(
      verifyAct(makeToken(), { now: NOW, jwksUrl, fetch: async () => new Response("{}") }),
      TypeError,
      `${jwksUrl} must be refused`,
    );
  }
});

test("refuses a negative leeway", async () => {
  await assert.rejects(verifyAct(makeToken(), { ...OPTIONS, leeway: -1 }), TypeError);
});

test("fails closed when the key set cannot be fetched or parsed", async () => {
  const jwksUrl = "https://api.idside.example/.well-known/jwks.json";
  const failures: Array<() => Promise<Response>> = [
    async () => {
      throw new TypeError("fetch failed");
    },
    async () => new Response("nope", { status: 503 }),
    async () => new Response("<html>not json</html>", { status: 200 }),
  ];
  for (const failing of failures) {
    await assert.rejects(
      verifyAct(makeToken(), { now: NOW, jwksUrl, fetch: failing }),
      ActVerificationUnavailableError,
    );
  }
});

// --- error hierarchy ----------------------------------------------------

test("every verification failure stays inside the SDK error hierarchy", async () => {
  const failure = await verifyAct(makeToken(claims({ iss: "elsewhere.test" })), OPTIONS).catch((e: unknown) => e);
  assert.ok(failure instanceof ActIssuerError);
  assert.ok(failure instanceof ActVerificationError);
  assert.ok(failure instanceof ConsentTokenError);
  assert.ok(failure instanceof IDSideError);
  assert.equal((failure as Error).name, "ActIssuerError");
});

test("never signs: verification works from public key material alone", async () => {
  // The key set carries only x/y — no private scalar is needed or accepted.
  const jwk = createPublicKey({ key: JWK as never, format: "jwk" }).export({ format: "jwk" });
  assert.equal(jwk.d, undefined);
  const proof = await verifyAct(makeToken(), { ...OPTIONS, jwks: { keys: [{ ...jwk, kid: KID }] } });
  assert.equal(proof.subject, "usr_pseudo_marie_456");
});

// ── the clock the verification is judged against ─────────────────────────────

test("a clock that is not a finite number is refused before any key is read", async () => {
  // `now` exists so a verifier can pin the instant — replaying an export, or
  // testing. A NaN would make every `exp`/`nbf` comparison false, which is a
  // verifier that accepts an expired token and says nothing. It is a TypeError,
  // not an ActVerificationError: the caller's own argument is wrong.
  for (const now of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
    await assert.rejects(
      verifyAct(makeToken(), { jwks: JWKS, now }),
      (error: unknown) =>
        error instanceof TypeError && /now must be a finite epoch in seconds/.test(String(error)),
      String(now),
    );
  }
});

test("with no clock given, the wall clock decides", async () => {
  // The default is the point of the option existing: a caller who passes no
  // `now` must get a verification against the present, not against zero.
  const live = claims({ iat: Math.floor(Date.now() / 1000) - 60, exp: Math.floor(Date.now() / 1000) + 3540 });
  const proof = await verifyAct(makeToken(live), { jwks: JWKS });
  assert.equal(proof.claims.jti, "act_01j9xk2m7q");

  const stale = claims({ iat: 1_000_000, exp: 1_000_060 });
  await assert.rejects(verifyAct(makeToken(stale), { jwks: JWKS }), ActExpiredError);
});

test("with no fetch injected, a key set is fetched with the global one", async () => {
  // Captured at call time from `globalThis`, detached from `this` so a strict
  // implementation is never called with the wrong receiver.
  const original = globalThis.fetch;
  const seen: string[] = [];
  globalThis.fetch = (async (url: unknown): Promise<Response> => {
    seen.push(String(url));
    return new Response(JSON.stringify(JWKS), { headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    const proof = await verifyAct(makeToken(), {
      now: NOW,
      jwksUrl: "https://api.idside.example/.well-known/jwks.json",
    });
    assert.equal(proof.claims.jti, "act_01j9xk2m7q");
  } finally {
    globalThis.fetch = original;
  }

  assert.deepEqual(seen, ["https://api.idside.example/.well-known/jwks.json"]);
});

// An explicit suite timeout, because the regression this pins does not fail —
// it HANGS. A test that never returns reports nothing at all.
test("the fetch deadline stays armed until the key set's body is read", { timeout: 5_000 }, async () => {
  // A host that answers 200 and then stalls the body used to hang verifyAct()
  // for ever, ignoring timeoutMs entirely — because the timer was cleared when
  // the headers arrived. Python reads the whole body inside its httpx timeout,
  // so this was also a cross-SDK divergence, on a path whose whole purpose is
  // to fail closed.
  const stalling = async (_url: unknown, init?: RequestInit): Promise<Response> => {
    const signal = init!.signal!;
    const body = new ReadableStream({
      start(controller) {
        signal.addEventListener("abort", () => controller.error(new Error("the deadline fired")));
      },
    });
    return new Response(body, { headers: { "content-type": "application/json" } });
  };

  await assert.rejects(
    verifyAct(makeToken(), {
      now: NOW,
      jwksUrl: "https://api.idside.example/.well-known/jwks.json",
      timeoutMs: 25,
      fetch: stalling as unknown as typeof fetch,
    }),
    (error: unknown) =>
      error instanceof ActVerificationUnavailableError && /NOT accepted/.test(String(error)),
  );
});
