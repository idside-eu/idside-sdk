import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, sign as cryptoSign, type KeyObject } from "node:crypto";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import OpenAI from "openai";
import Anthropic from "@anthropic-ai/sdk";
import { GoogleGenAI } from "@google/genai";
import { Mistral } from "@mistralai/mistralai";
import { HTTPClient } from "@mistralai/mistralai/lib/http.js";
import { wrap } from "../src/consent.ts";
import { GenericAdapter } from "../src/adapters/index.ts";
import { loadDescriptors } from "../src/descriptors.ts";
import { clock, compiler, eventLabels, PolicyCache, PolicyState, readAnswer } from "../src/contentPolicy.ts";
import { HttpConsentClient, contentPolicyAnswer, type ConsentDecision } from "../src/transport.ts";
import {
  AuthenticationError,
  ConsentDeniedError,
  ConsentUnreachableError,
  ContentBlockedError,
  ContentPolicyError,
  ContractError,
  IDSideError,
  QuotaExceededError,
  RateLimitedError,
  UnenforceableOptionError,
} from "../src/exceptions.ts";
import { ENGINE_VERSION, buildDigest, challengeResponse, contentPolicyDigest, rulesDigest } from "../src/cleaning/index.ts";
import { requestHash } from "../src/requestHash.ts";

// Prompt cleaning on enforced calls: the signed policy, the hash, the
// declaration — the same cases as packages/python/tests/test_content_policy.py.
// A stand-in cloud signs policies with a key made for this run (never written
// anywhere) and records what it receives; the provider is a recording fake.

const NOW = 1_800_000_000;
const realNow = clock.now;
const realMonotonic = clock.monotonic;
const realSleep = clock.sleep;
let now = NOW;
/** The monotonic clock on its own, for a test that moves the wall clock alone. */
let mono: number | undefined;
beforeEach(() => {
  now = NOW;
  mono = undefined;
  clock.now = () => now;
  // Both clocks read the same value unless a test sets `mono` apart: moving
  // `now` moves both, as time passing does.
  clock.monotonic = () => mono ?? now;
});
afterEach(() => {
  clock.now = realNow;
  clock.monotonic = realMonotonic;
  clock.sleep = realSleep;
});

const b64url = (data: Buffer | string) => Buffer.from(data).toString("base64url");

class SigningKey {
  readonly kid: string;
  readonly privateKey: KeyObject;
  readonly publicJwk: Record<string, unknown>;
  constructor(kid = "content-policy-g1") {
    const { privateKey, publicKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
    this.kid = kid;
    this.privateKey = privateKey;
    this.publicJwk = { ...publicKey.export({ format: "jwk" }), kid, use: "sig", alg: "ES256" };
  }
  sign(input: string): Buffer {
    return cryptoSign("sha256", Buffer.from(input), { key: this.privateKey, dsaEncoding: "ieee-p1363" });
  }
}

function jws(key: SigningKey, claims: unknown, header?: unknown, tamper = false): string {
  const head = header ?? { alg: "ES256", typ: "content-policy+jwt", kid: key.kid };
  const input = b64url(JSON.stringify(head)) + "." + b64url(JSON.stringify(claims));
  const signature = key.sign(input);
  if (tamper) signature[signature.length - 1] ^= 1;
  return input + "." + b64url(signature);
}

function policyDocument(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema: 1,
    app_id: "app_medhelp",
    revision: 7,
    detectors: { email: "mask", credit_card: "block" },
    attachments: "allow",
    custom: [{ kind: "term", value: "Projet Atlas", action: "mask", label: "PROJECT" }],
    ...overrides,
  };
}

function claimsFor(policy: Record<string, unknown>, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    iss: "idside.eu",
    app_id: policy.app_id,
    iat: NOW - 10,
    exp: NOW + 890,
    digest: contentPolicyDigest(policy),
    policy,
    ...overrides,
  };
}

const CHALLENGE = { token: "chl_test_0001", inputs: ["Mail marie@example.com", "nothing"] };

interface Recorded {
  path: string;
  method: string;
  headers: Headers;
  body: Record<string, unknown> | undefined;
}

class Cloud {
  key = new SigningKey();
  policy: Record<string, unknown> | null = policyDocument();
  answer: unknown = undefined;
  policyStatus = 200;
  jwksStatus = 200;
  jwksBody: string | undefined;
  authorizeExtra: Record<string, unknown> = {};
  failing = new Set<string>();
  /** The headers of the next policy answers to come back 429, one per answer. */
  limited: Record<string, string>[] = [];
  requests: Recorded[] = [];

  policyAnswer(): unknown {
    if (this.answer !== undefined) return this.answer;
    if (this.policy === null) return { enabled: false, refresh_after: 300 };
    return { enabled: true, policy: jws(this.key, claimsFor(this.policy)), challenge: CHALLENGE, refresh_after: 300 };
  }

  readonly fetch = (async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : undefined;
    this.requests.push({ path: url.pathname, method: init?.method ?? "GET", headers: new Headers(init?.headers), body });
    if (this.failing.has(url.pathname)) throw new TypeError("fetch failed");
    const json = (value: unknown, status = 200) =>
      new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });
    if (url.pathname === "/v1/content-policy") {
      const limited = this.limited.shift();
      if (limited !== undefined) {
        return new Response(JSON.stringify({ title: "Slow" }), { status: 429, headers: limited });
      }
      return this.policyStatus === 200 ? json(this.policyAnswer()) : json({ title: "nope" }, this.policyStatus);
    }
    if (url.pathname === "/.well-known/jwks.json") {
      if (this.jwksBody !== undefined) return new Response(this.jwksBody, { status: 200 });
      return this.jwksStatus === 200 ? json({ keys: [this.key.publicJwk] }) : new Response("", { status: this.jwksStatus });
    }
    if (url.pathname === "/v1/content-events") return new Response(null, { status: 204 });
    return json({
      decision: "allow",
      consent_state: {},
      params_forced: {},
      mode: "test",
      act_token: "act.jwt.token",
      token_id: "act_01k4f9h2m3",
      expires_at: "2026-09-12T12:00:00Z",
      source: "default_policy",
      legal_basis: "legitimate_interest",
      binding: body && "user_id" in body ? "user_id" : "api_key",
      ...this.authorizeExtra,
    });
  }) as typeof fetch;

  paths(): string[] {
    return this.requests.map((request) => request.path);
  }

  bodies(path: string): Record<string, unknown>[] {
    return this.requests.filter((request) => request.path === path).map((request) => request.body!);
  }
}

function wrapped(cloud: Cloud) {
  const calls: Record<string, unknown>[] = [];
  const create = async (body: Record<string, unknown>) => {
    calls.push(body);
    return { id: "chatcmpl_fake" };
  };
  const consentClient = new HttpConsentClient("https://cloud.example.test", {
    apiKey: "sk_test_cleaning", // pragma: allowlist secret
    fetch: cloud.fetch,
  });
  const client = wrap({ chat: { completions: { create } } }, { provider: "openai", consentClient }) as any;
  return { client, calls };
}

const MESSAGES = [
  { role: "system", content: "About Projet Atlas." },
  { role: "user", content: "Write to marie@example.com" },
];
const CLEANED = [
  { role: "system", content: "About [PROJECT_1]." },
  { role: "user", content: "Write to [EMAIL_1]" },
];
const CALL = { model: "gpt-4o", messages: MESSAGES, user_id: "usr_marie_456" };

// ── cleaning off: exactly the call an SDK without cleaning makes ────────────

test("cleaning off sends no field and moves no hash", async () => {
  const cloud = new Cloud();
  cloud.policy = null;
  const { client, calls } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  const [authorize] = cloud.bodies("/v1/authorize");
  assert.ok(!("content_policy" in authorize));
  assert.equal(authorize.request_hash, requestHash({ model: "gpt-4o", messages: MESSAGES }));
  assert.equal(calls[0].messages, MESSAGES);
  assert.deepEqual(cloud.paths(), ["/v1/content-policy", "/v1/authorize"]);
});

test("a consent client that cannot fetch a policy cleans nothing", async () => {
  const seen: Record<string, unknown>[] = [];
  const calls: Record<string, unknown>[] = [];
  const client = wrap(
    { chat: { completions: { create: async (body: Record<string, unknown>) => (calls.push(body), {}) } } },
    {
      provider: "openai",
      consentClient: {
        async authorize(request): Promise<ConsentDecision> {
          seen.push(request as unknown as Record<string, unknown>);
          return {
            consentState: {},
            paramsForced: {},
            actToken: "act.jwt.token",
            source: "widget",
            legalBasis: "consent",
            binding: "user_id",
            constraints: {},
          };
        },
      },
    },
  ) as any;
  await client.chat.completions.create(CALL);
  assert.ok(!("contentPolicy" in seen[0]));
  assert.equal(calls[0].messages, MESSAGES);
});

// ── cleaning on ─────────────────────────────────────────────────────────────

test("cleaning masks before the hash and declares what it applied", async () => {
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  assert.deepEqual(calls[0].messages, CLEANED);
  const [authorize] = cloud.bodies("/v1/authorize");
  assert.equal(authorize.request_hash, requestHash({ model: "gpt-4o", messages: CLEANED }));
  assert.deepEqual(authorize.content_policy, {
    digest: contentPolicyDigest(policyDocument()),
    engine: ENGINE_VERSION,
    rules: rulesDigest(),
    build: buildDigest(),
    challenge: { token: CHALLENGE.token, response: challengeResponse(CHALLENGE.token, CHALLENGE.inputs) },
    counts: { email: 1, "custom:PROJECT": 1 },
  });
  const jwks = cloud.requests.find((request) => request.path === "/.well-known/jwks.json")!;
  assert.equal(jwks.headers.get("authorization"), null, "a key set is fetched without the API key");
  assert.equal(cloud.requests[0].headers.get("authorization"), "Bearer sk_test_cleaning");
});

test("a call with nothing to mask still declares the policy", async () => {
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  const messages = [{ role: "user", content: "hello" }];
  await client.chat.completions.create({ model: "gpt-4o", messages, user_id: "usr_marie_456" });
  const [authorize] = cloud.bodies("/v1/authorize");
  assert.deepEqual((authorize.content_policy as Record<string, unknown>).counts, {});
  assert.equal(authorize.request_hash, requestHash({ model: "gpt-4o", messages }));
  assert.equal(calls[0].messages, messages);
});

test("only changed arguments are replaced, and nothing is mutated", async () => {
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  const tools = [{ type: "function", function: { name: "f", parameters: { type: "object" } } }];
  await client.chat.completions.create({ ...CALL, tools, temperature: 0.2 });
  assert.equal(calls[0].tools, tools);
  assert.deepEqual(calls[0].messages, CLEANED);
  assert.equal(MESSAGES[1].content, "Write to marie@example.com");
});

test("a block rule stops the call before anything is sent", async () => {
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  await assert.rejects(
    client.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: "card 4111 1111 1111 1111" }], user_id: "u" }),
    (error: unknown) =>
      error instanceof ContentBlockedError &&
      error instanceof IDSideError &&
      error.name === "ContentBlockedError" &&
      JSON.stringify(error.blocked) === '{"credit_card":1}' &&
      !error.message.includes("4111") &&
      error.message.includes("credit_card (1)"),
  );
  assert.deepEqual(calls, []);
  assert.ok(!cloud.paths().includes("/v1/authorize"));
  assert.deepEqual(cloud.bodies("/v1/content-events"), [
    {
      digest: contentPolicyDigest(policyDocument()),
      engine: ENGINE_VERSION,
      blocked: { credit_card: 1 },
      provider: "openai",
      model: "gpt-4o",
    },
  ]);
});

const BLOCKED = [{ role: "user", content: "card 4111 1111 1111 1111" }];

test("a blocked call reports its model only as the service keeps it", async () => {
  const cases: [unknown, boolean][] = [
    ["gpt-4o-mini-2024-07-18", true],
    ["ft:gpt-4o:org:atlas:abc123", true],
    ["a", true],
    ["x".repeat(200), true],
    ["my model v2", true], // a space inside is printable
    ["x".repeat(201), false], // longer than the service keeps
    ["", false],
    [" gpt-4o", false],
    ["gpt-4o ", false],
    ["gpt\n4o", false],
    ["gpt\t4o", false],
    ["gpt-4o\x7f", false],
    ["modèle", false], // ASCII only: no Unicode table to disagree on
    [4, false], // not written as it would be read: left out, not "4"
    [null, false],
    [["gpt-4o"], false],
  ];
  for (const [model, sent] of cases) {
    const cloud = new Cloud();
    const { client } = wrapped(cloud);
    await assert.rejects(client.chat.completions.create({ model, messages: BLOCKED }), ContentBlockedError);
    // Left out, never null: the service refuses a member it cannot read, and
    // the whole report with it.
    assert.deepEqual(
      cloud.bodies("/v1/content-events"),
      [
        {
          digest: contentPolicyDigest(policyDocument()),
          engine: ENGINE_VERSION,
          blocked: { credit_card: 1 },
          provider: "openai",
          ...(sent ? { model } : {}),
        },
      ],
      `model ${JSON.stringify(model)}`,
    );
  }
});

test("a blocked call without a model reports the provider alone", async () => {
  const cloud = new Cloud();
  const { client } = wrapped(cloud);
  await assert.rejects(client.chat.completions.create({ messages: BLOCKED }), ContentBlockedError);
  const [event] = cloud.bodies("/v1/content-events");
  assert.ok(!("model" in event));
  assert.equal(event.provider, "openai");
});

test("a report names the provider only as the service keeps it", () => {
  const cases: [unknown, boolean][] = [
    ["openai", true],
    ["my_provider-2", true],
    ["0" + "x".repeat(63), true],
    ["0" + "x".repeat(64), false],
    ["Open AI", false],
    ["OpenAI", false],
    ["-openai", false],
    ["openai\n", false],
    ["", false],
    [undefined, false],
  ];
  for (const [provider, sent] of cases) {
    assert.deepEqual(eventLabels(provider, "m"), { ...(sent ? { provider } : {}), model: "m" }, String(provider));
  }
});

test("the transport writes only the members it is given", async () => {
  const bodies: unknown[] = [];
  const fetch = (async (_url: string | URL | Request, init?: RequestInit) => {
    bodies.push(JSON.parse(String(init?.body)));
    return new Response(null, { status: 204 });
  }) as typeof globalThis.fetch;
  const client = new HttpConsentClient("https://cloud.example.test", { fetch });
  const base = { digest: "sha256:x", engine: "1", blocked: { email: 1 } };
  await client.contentEvents(base);
  await client.contentEvents({ ...base, provider: "openai" });
  await client.contentEvents({ ...base, model: "gpt-4o" });
  await client.contentEvents({ ...base, provider: "openai", model: "gpt-4o" });
  assert.deepEqual(bodies, [
    base,
    { ...base, provider: "openai" },
    { ...base, model: "gpt-4o" },
    { ...base, provider: "openai", model: "gpt-4o" },
  ]);
});

test("a report that cannot be delivered does not change the error", async () => {
  const cloud = new Cloud();
  cloud.failing.add("/v1/content-events");
  const { client } = wrapped(cloud);
  await assert.rejects(
    client.chat.completions.create({ model: "gpt-4o", messages: [{ content: "4111111111111111" }], user_id: "u" }),
    ContentBlockedError,
  );
});

test("a consent client whose report throws still blocks, and one without a report blocks too", async () => {
  for (const contentEvents of [
    async () => {
      throw new Error("down");
    },
    undefined,
  ]) {
    const inner = new HttpConsentClient("https://cloud.example.test", { fetch: new Cloud().fetch });
    const client = wrap(
      { chat: { completions: { create: async () => ({}) } } },
      {
        provider: "openai",
        consentClient: {
          authorize: (request) => inner.authorize(request),
          contentPolicy: () => inner.contentPolicy(),
          ...(contentEvents ? { contentEvents } : {}),
        },
      },
    ) as any;
    await assert.rejects(
      client.chat.completions.create({ model: "gpt-4o", messages: [{ content: "4111111111111111" }] }),
      ContentBlockedError,
    );
  }
});

test("attachments can be blocked", async () => {
  const cloud = new Cloud();
  cloud.policy = policyDocument({ attachments: "block" });
  const { client, calls } = wrapped(cloud);
  await assert.rejects(
    client.chat.completions.create({
      model: "gpt-4o",
      messages: [{ role: "user", content: [{ type: "image_url", image_url: { url: "https://img.example.com/a.png" } }] }],
    }),
    (error: unknown) => error instanceof ContentBlockedError && error.blocked.attachments === 1,
  );
  assert.deepEqual(calls, []);
});

test("the journal names counts, never values", () => {
  // Without NODE_TEST_CONTEXT: a child that inherits it reports to this runner
  // instead of running, and exits 0 whatever happened.
  const { NODE_TEST_CONTEXT: _, ...env } = process.env;
  const child = spawnSync(process.execPath, [fileURLToPath(new URL("./contentJournalChild.ts", import.meta.url))], {
    encoding: "utf8",
    env: { ...env, NODE_DEBUG: "idside" },
  });
  assert.equal(child.status, 0, child.stderr);
  const text = child.stderr;
  const digest = contentPolicyDigest(policyDocument()).slice("sha256:".length, "sha256:".length + 12);
  assert.ok(text.includes(`cleaned call_site=chat.completions.create policy=${digest} masked=custom:PROJECT:1+email:1`), text);
  assert.ok(text.includes(`content_blocked call_site=chat.completions.create policy=${digest} blocked=credit_card:1`), text);
  assert.ok(text.includes("content_policy_failed call_site=chat.completions.create error=ConsentUnreachableError"), text);
  for (const value of ["marie", "Atlas", "4111"]) assert.ok(!text.includes(value), value);
});

test("a call naming nobody is cleaned too", async () => {
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  await client.chat.completions.create({ model: "gpt-4o", messages: MESSAGES });
  assert.deepEqual(calls[0].messages, CLEANED);
});

// ── no way around it ────────────────────────────────────────────────────────

for (const escape of [{ content_policy: false }, { skip_cleaning: true }, { cleaning: false }, { idside_clean: false }]) {
  test(`no body field turns cleaning off: ${JSON.stringify(escape)}`, async () => {
    // The body is open in Node — what is in it is hashed and proved as
    // written — and the SDK never reads a switch out of it: the field goes to
    // the provider like any other, and the call is cleaned all the same.
    const cloud = new Cloud();
    const wire = capture(CHAT);
    const consentClient = new HttpConsentClient("https://cloud.example.test", { fetch: cloud.fetch });
    const client = wrap(new OpenAI({ apiKey: "sk-test", fetch: wire.fetch, maxRetries: 0 }), { consentClient }) as any; // pragma: allowlist secret
    await client.chat.completions.create({ ...CALL, ...escape });
    assert.deepEqual(wire.bodies[0].messages, CLEANED);
    assert.deepEqual((cloud.bodies("/v1/authorize")[0].content_policy as Record<string, unknown>).counts, {
      email: 1,
      "custom:PROJECT": 1,
    });
  });
}

for (const options of [{ content_policy: false }, { cleaning: false }, { body: { messages: [] } }]) {
  test(`no per-call option turns cleaning off: ${JSON.stringify(options)}`, async () => {
    const cloud = new Cloud();
    const wire = capture(CHAT);
    const consentClient = new HttpConsentClient("https://cloud.example.test", { fetch: cloud.fetch });
    const client = wrap(new OpenAI({ apiKey: "sk-test", fetch: wire.fetch, maxRetries: 0 }), { consentClient }) as any; // pragma: allowlist secret
    await assert.rejects(client.chat.completions.create(CALL, options), UnenforceableOptionError);
    assert.deepEqual(wire.bodies, []);
  });
}

for (const hatch of ["contentPolicy", "cleaning", "clean", "skipCleaning"]) {
  test(`wrap() has no cleaning option: ${hatch} changes nothing`, async () => {
    const cloud = new Cloud();
    const calls: Record<string, unknown>[] = [];
    const consentClient = new HttpConsentClient("https://cloud.example.test", { fetch: cloud.fetch });
    const client = wrap(
      { chat: { completions: { create: async (body: Record<string, unknown>) => (calls.push(body), {}) } } },
      { provider: "openai", consentClient, [hatch]: false } as any,
    ) as any;
    await client.chat.completions.create(CALL);
    assert.deepEqual(calls[0].messages, CLEANED);
  });
}

// ── a policy that does not check out is never applied ───────────────────────

const answerWith = (token: string) => ({ enabled: true, policy: token, challenge: CHALLENGE, refresh_after: 300 });

async function refused(cloud: Cloud, match: RegExp): Promise<void> {
  const { client, calls } = wrapped(cloud);
  await assert.rejects(client.chat.completions.create(CALL), (error: unknown) => {
    assert.ok(error instanceof ContentPolicyError, String(error));
    assert.match(error.message, match);
    return true;
  });
  assert.deepEqual(calls, []);
  assert.ok(!cloud.paths().includes("/v1/authorize"));
}

test("a tampered signature is refused", async () => {
  const cloud = new Cloud();
  cloud.answer = answerWith(jws(cloud.key, claimsFor(policyDocument()), undefined, true));
  await refused(cloud, /signature/);
});

test("a policy signed by another key is refused", async () => {
  const cloud = new Cloud();
  cloud.answer = answerWith(jws(new SigningKey(cloud.key.kid), claimsFor(policyDocument())));
  await refused(cloud, /signature/);
});

for (const [header, match] of [
  [{ alg: "HS256", typ: "content-policy+jwt", kid: "content-policy-g1" }, /not ES256/],
  [{ alg: "ES256", typ: "act+jwt", kid: "content-policy-g1" }, /of type/],
  [{ alg: "ES256", typ: "content-policy+jwt" }, /well-formed/],
  [{ alg: "ES256", typ: "content-policy+jwt", kid: "content-policy-g1", crit: ["x"] }, /well-formed/],
  [{ alg: "ES256", typ: "content-policy+jwt", kid: "content-policy-g9" }, /names a key/],
  [{ alg: "ES256", typ: "content-policy+jwt", kid: "unknown-kid" }, /not signed with a prompt-cleaning policy key/],
] as const) {
  test(`a header that is not a policy header is refused: ${JSON.stringify(header)}`, async () => {
    const cloud = new Cloud();
    cloud.answer = answerWith(jws(cloud.key, claimsFor(policyDocument()), header));
    await refused(cloud, match);
  });
}

// The cloud names each signing key after its purpose (`act-g1`,
// `content-policy-g1`, with `-staging` or `-dev` outside production) and
// publishes them in one key set. Each signature below is valid and its key is
// in that set: only its purpose is wrong. An ACT key must not sign what a call
// is cleaned by.
for (const kid of ["act-g1", "ubt-g1", "act-g1-staging", "content-policy", "Content-policy-g1"]) {
  test(`a key of another purpose never signs a policy: ${kid}`, async () => {
    const cloud = new Cloud();
    cloud.key = new SigningKey(kid);
    await refused(cloud, /not signed with a prompt-cleaning policy key/);
    assert.ok(!cloud.paths().includes("/.well-known/jwks.json"), "refused before any lookup");
  });
}

for (const kid of ["content-policy-g2", "content-policy-g1-staging"]) {
  test(`any generation of the policy key verifies: ${kid}`, async () => {
    const cloud = new Cloud();
    cloud.key = new SigningKey(kid);
    const { client, calls } = wrapped(cloud);
    await client.chat.completions.create(CALL);
    assert.deepEqual(calls[0].messages, CLEANED);
  });
}

test("a high-s signature verifies, as it does in Python", async () => {
  // ECDSA accepts both s and n - s; the cloud's signer does not normalise s.
  const n = BigInt("0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551");
  const cloud = new Cloud();
  const [head, body, signature] = jws(cloud.key, claimsFor(policyDocument())).split(".");
  const raw = Buffer.from(signature, "base64url");
  const s = BigInt("0x" + raw.subarray(32).toString("hex"));
  const high = Buffer.concat([raw.subarray(0, 32), Buffer.from((n - s).toString(16).padStart(64, "0"), "hex")]);
  cloud.answer = answerWith(`${head}.${body}.${b64url(high)}`);
  const { client, calls } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  assert.deepEqual(calls[0].messages, CLEANED);
});

test("a key-set key off the curve is refused", async () => {
  const cloud = new Cloud();
  const y = BigInt("0x" + Buffer.from(String(cloud.key.publicJwk.y), "base64url").toString("hex"));
  const p = BigInt("0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF");
  cloud.key.publicJwk.y = b64url(Buffer.from(((y + 1n) % p).toString(16).padStart(64, "0"), "hex"));
  await refused(cloud, /names a key the key set cannot verify with/);
});

for (const [claims, match] of [
  [{ iss: "evil.example" }, /issued by ID Side/],
  [{ exp: NOW }, /past its expiry/],
  [{ iat: NOW + 301, exp: NOW + 900 }, /before its date of issue/],
  [{ iat: NOW, exp: NOW }, /expires before it was issued/],
  [{ iat: NOW + 10, exp: NOW + 5 }, /expires before it was issued/],
  [{ iat: "1", exp: NOW + 900 }, /integer/],
  [{ exp: NOW + 890.5 }, /integer/],
  [{ iat: true, exp: NOW + 900 }, /integer/],
  [{ digest: "sha256:" + "0".repeat(64) }, /digest/],
  [{ app_id: "app_other" }, /application/],
  [{ app_id: null }, /application/],
] as const) {
  test(`claims that do not check out are refused: ${JSON.stringify(claims)}`, async () => {
    const cloud = new Cloud();
    cloud.answer = answerWith(jws(cloud.key, claimsFor(policyDocument(), claims)));
    await refused(cloud, match);
  });
}

const CLOCK_AHEAD =
  "The prompt-cleaning policy just received from the ID Side cloud is signed and intact, but this machine's clock " +
  "puts it past its expiry: either the clock looks ahead, or a cache between this machine and the cloud served an " +
  "old answer. Check that this machine's clock is set (NTP) and that no proxy caches /v1/content-policy; the AI call " +
  "was not made.";
const CLOCK_BEHIND =
  "The prompt-cleaning policy just received from the ID Side cloud is signed and intact, but this machine's clock " +
  "puts it before its date of issue: the clock looks behind. Check that this machine's clock is set (NTP); the AI call " +
  "was not made.";

// The policy is valid 900 s from `iat`, and `iat` may be up to 300 s ahead of
// this machine: past either bound, the clock is what is wrong — and the
// developer must read "check your clock", not an outage of ID Side.
for (const [offset, expected] of [
  [900, CLOCK_AHEAD],
  [3600, CLOCK_AHEAD],
  [-301, CLOCK_BEHIND],
  [-86_400, CLOCK_BEHIND],
] as const) {
  test(`a policy out of step with this clock says so: ${offset} s`, async () => {
    const cloud = new Cloud();
    now = NOW - 10 + offset; // the policy was issued at NOW - 10
    const { client, calls } = wrapped(cloud);
    await assert.rejects(client.chat.completions.create(CALL), (error: unknown) => {
      assert.ok(error instanceof ContentPolicyError);
      assert.equal(error.message, expected);
      for (const value of ["app_medhelp", "Projet Atlas", "sha256", CHALLENGE.token, "18"]) {
        assert.ok(!error.message.includes(value), value);
      }
      return true;
    });
    assert.deepEqual(calls, []);
  });
}

for (const offset of [889, -300]) {
  test(`the clock bounds are unchanged: ${offset} s`, async () => {
    const cloud = new Cloud();
    now = NOW - 10 + offset;
    const { client, calls } = wrapped(cloud);
    await client.chat.completions.create(CALL);
    assert.deepEqual(calls[0].messages, CLEANED);
  });
}

test("the clock is named only for a policy that checks out otherwise", async () => {
  const expired = claimsFor(policyDocument(), { exp: NOW });
  const tampered = new Cloud();
  tampered.answer = answerWith(jws(tampered.key, expired, undefined, true));
  await refused(tampered, /signature/);
  const other = new Cloud();
  other.answer = answerWith(jws(other.key, { ...expired, app_id: "x" }));
  await refused(other, /application/);
  const digest = new Cloud();
  digest.answer = answerWith(jws(digest.key, { ...expired, digest: "d" }));
  await refused(digest, /digest/);
});

test("a policy this engine cannot apply is refused", async () => {
  const cloud = new Cloud();
  cloud.answer = answerWith(jws(cloud.key, claimsFor(policyDocument({ schema: 2 }))));
  await refused(cloud, /schema/);
});

test("a policy that is not an object is refused", async () => {
  const cloud = new Cloud();
  const claims = claimsFor(policyDocument());
  claims.policy = ["not", "an", "object"];
  claims.digest = contentPolicyDigest(claims.policy);
  cloud.answer = answerWith(jws(cloud.key, claims));
  await refused(cloud, /application/);
});

test("a policy holding a value the canonicaliser cannot write is refused", async () => {
  const cloud = new Cloud();
  const claims = claimsFor(policyDocument());
  // A lone surrogate: JSON can carry it, the canonical form cannot.
  claims.policy = { ...policyDocument(), note: "\ud800" };
  cloud.answer = answerWith(jws(cloud.key, claims));
  await refused(cloud, /digest/);
});

for (const [answer, match] of [
  [[], /not a JSON object/],
  [{}, /whether cleaning is enabled/],
  [{ enabled: "yes" }, /whether cleaning is enabled/],
  [{ enabled: false, refresh_after: -1 }, /refresh_after/],
  [{ enabled: false, refresh_after: true }, /refresh_after/],
  [{ enabled: false, refresh_after: 86_401 }, /refresh_after/],
  [{ enabled: true, challenge: CHALLENGE }, /no signed policy/],
  [{ enabled: true, policy: "a.b.c" }, /no usable challenge/],
  [{ enabled: true, policy: "a.b.c", challenge: { token: "", inputs: [] } }, /no usable challenge/],
  [{ enabled: true, policy: "a.b.c", challenge: { token: "t", inputs: [1] } }, /no usable challenge/],
  [{ enabled: true, policy: "a.b.c", challenge: { token: "t", inputs: Array(65).fill("x") } }, /no usable challenge/],
  [{ enabled: true, policy: "a.b", challenge: CHALLENGE }, /compact JWS/],
  [{ enabled: true, policy: "a..c", challenge: CHALLENGE }, /compact JWS/],
  [{ enabled: true, policy: "!.b.c", challenge: CHALLENGE }, /well-formed/],
] as const) {
  test(`an answer that is not one is refused: ${JSON.stringify(answer).slice(0, 60)}`, async () => {
    const cloud = new Cloud();
    cloud.answer = answer;
    await refused(cloud, match);
  });
}

test("an answer that is not JSON is refused", async () => {
  const cloud = new Cloud();
  const inner = cloud.fetch;
  const fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    if (String(input).endsWith("/v1/content-policy")) return new Response("<html>", { status: 200 });
    return inner(input, init);
  }) as typeof globalThis.fetch;
  const client = wrap(
    { chat: { completions: { create: async () => ({}) } } },
    { provider: "openai", consentClient: new HttpConsentClient("https://cloud.example.test", { fetch }) },
  ) as any;
  await assert.rejects(client.chat.completions.create(CALL), /not a JSON object/);
});

// ── a cloud that does not answer ────────────────────────────────────────────

for (const [status, ErrorType] of [
  [404, ConsentUnreachableError],
  [503, ConsentUnreachableError],
  [401, AuthenticationError],
  [402, QuotaExceededError],
  [403, ConsentDeniedError],
  [422, ContractError],
  [429, RateLimitedError],
] as const) {
  test(`a policy endpoint error fails closed: ${status}`, async () => {
    const cloud = new Cloud();
    cloud.policyStatus = status;
    const { client, calls } = wrapped(cloud);
    await assert.rejects(client.chat.completions.create(CALL), (error: unknown) => {
      assert.ok(error instanceof ErrorType);
      // Whatever its class, the message names the route that answered.
      assert.match((error as Error).message, /prompt-cleaning policy/);
      assert.doesNotMatch((error as Error).message, /verify consent/);
      return true;
    });
    assert.deepEqual(calls, []);
  });
}

// Not "Could not verify consent": /v1/authorize may be answering well, and the
// developer must look at the policy route, not at authorization. A qualified
// refusal (401, 402, 403, 422, 429) keeps its own class.
for (const status of [404, 409, 418, 500, 503]) {
  test(`a policy endpoint error names the policy and the status: ${status}`, async () => {
    const cloud = new Cloud();
    cloud.policyStatus = status;
    const { client, calls } = wrapped(cloud);
    await assert.rejects(client.chat.completions.create(CALL), (error: unknown) => {
      assert.ok(error instanceof ConsentUnreachableError);
      assert.equal(
        error.message,
        `The ID Side cloud's prompt-cleaning policy could not be fetched (HTTP ${status}), and none is held that is ` +
          "still valid; the AI call was not made. Cloud says: nope.",
      );
      return true;
    });
    assert.deepEqual(calls, []);
  });
}

test("a policy endpoint error on the default cloud names the address", async () => {
  const previous = process.env.IDSIDE_BASE_URL;
  delete process.env.IDSIDE_BASE_URL;
  try {
    const cloud = new Cloud();
    cloud.policyStatus = 503;
    const client = new HttpConsentClient(undefined, { fetch: cloud.fetch });
    await assert.rejects(client.contentPolicy(), (error: unknown) => {
      assert.ok(error instanceof ConsentUnreachableError);
      assert.match(error.message, /\(HTTP 503\)/);
      assert.match(error.message, /IDSIDE_BASE_URL/);
      return true;
    });
  } finally {
    if (previous !== undefined) process.env.IDSIDE_BASE_URL = previous;
  }
});

for (const broken of ["status", "json"] as const) {
  test(`a key set that cannot be fetched fails closed: ${broken}`, async () => {
    const cloud = new Cloud();
    if (broken === "status") cloud.jwksStatus = 404;
    else cloud.jwksBody = "not json";
    const { client, calls } = wrapped(cloud);
    await assert.rejects(client.chat.completions.create(CALL), ConsentUnreachableError);
    assert.deepEqual(calls, []);
  });
}

for (const route of ["/v1/content-policy", "/.well-known/jwks.json"]) {
  test(`a network failure fails closed: ${route}`, async () => {
    const cloud = new Cloud();
    cloud.failing.add(route);
    const { client } = wrapped(cloud);
    await assert.rejects(client.chat.completions.create(CALL), ConsentUnreachableError);
  });
}

test("an unexpected error from the policy fetch is not swallowed by a held policy", async () => {
  const cloud = new Cloud();
  const { client } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  now += 600; // refresh due, policy still valid
  cloud.answer = { enabled: "maybe" };
  await assert.rejects(client.chat.completions.create(CALL), ContentPolicyError);
});

// ── caching and refreshing ──────────────────────────────────────────────────

test("the policy is fetched once, then again after refresh_after", async () => {
  const cloud = new Cloud();
  const { client } = wrapped(cloud);
  for (let i = 0; i < 3; i++) await client.chat.completions.create(CALL);
  const count = (path: string) => cloud.paths().filter((p) => p === path).length;
  assert.equal(count("/v1/content-policy"), 1);
  assert.equal(count("/.well-known/jwks.json"), 1);
  now += 300;
  await client.chat.completions.create(CALL);
  assert.equal(count("/v1/content-policy"), 2);
  assert.equal(count("/.well-known/jwks.json"), 1);
});

test("a failed refresh keeps a policy still valid, then fails", async () => {
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  cloud.policyStatus = 503;
  now += 600;
  await client.chat.completions.create(CALL);
  assert.deepEqual(calls.at(-1)!.messages, CLEANED);
  now = NOW + 890;
  await assert.rejects(client.chat.completions.create(CALL), ConsentUnreachableError);
});

test("a disabled answer stands for its window when it cannot be refreshed", async () => {
  const cloud = new Cloud();
  cloud.policy = null;
  const { client } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  cloud.policyStatus = 503;
  now += 600;
  await client.chat.completions.create(CALL);
  now = NOW + 900;
  await assert.rejects(client.chat.completions.create(CALL), ConsentUnreachableError);
});

for (const status of ["outdated", "not_applied", "anomaly"]) {
  test(`an answer asking for a fresh policy is heard: ${status}`, async () => {
    const cloud = new Cloud();
    cloud.authorizeExtra = { content_policy: { status, digest: "sha256:new" } };
    const { client } = wrapped(cloud);
    const count = () => cloud.paths().filter((p) => p === "/v1/content-policy").length;
    await client.chat.completions.create(CALL);
    // Fetched again on the next call — once ten seconds have passed since the
    // last fetch: while a policy is held, the cloud is asked at most that often.
    now += 9;
    await client.chat.completions.create(CALL);
    assert.equal(count(), 1);
    now += 1;
    await client.chat.completions.create(CALL);
    assert.equal(count(), 2);
    await client.chat.completions.create(CALL);
    assert.equal(count(), 2, "at most once every ten seconds");
    now += 10;
    await client.chat.completions.create(CALL);
    await client.chat.completions.create(CALL);
    assert.equal(count(), 3);
  });
}

test("not_applied while cleaning is off here fetches the policy at once", async () => {
  // The cloud says cleaning is on while the answer held says off (switched on
  // since): the next call fetches it, rather than waiting refresh_after.
  const cloud = new Cloud();
  cloud.policy = null;
  cloud.authorizeExtra = { content_policy: { status: "not_applied", digest: "d" } };
  const { client, calls } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  assert.equal(calls[0].messages, MESSAGES);
  cloud.policy = policyDocument();
  now += 10; // at once when the last fetch is ten seconds old, as it almost always is
  await client.chat.completions.create(CALL);
  assert.equal(cloud.paths().filter((p) => p === "/v1/content-policy").length, 2);
  assert.deepEqual(calls[1].messages, CLEANED);
  assert.ok("content_policy" in cloud.bodies("/v1/authorize")[1]);
});

// During an incident, every AI call would otherwise pay the fetch's timeout and
// double the cloud's load: while the policy held is still valid, it is applied
// between two attempts, at most one every ten seconds.
for (const how of ["503", "404", "network", "refused"] as const) {
  test(`a failed refresh is retried at most every ten seconds while a policy is held: ${how}`, async () => {
    const cloud = new Cloud();
    const { client, calls } = wrapped(cloud);
    const fetches = () => cloud.paths().filter((p) => p === "/v1/content-policy").length;
    const attempt = async () => {
      if (how === "refused") await assert.rejects(client.chat.completions.create(CALL), ContentPolicyError);
      else await client.chat.completions.create(CALL);
    };
    await client.chat.completions.create(CALL);
    if (how === "503" || how === "404") cloud.policyStatus = Number(how);
    else if (how === "refused") cloud.answer = { enabled: "maybe" };
    else cloud.failing.add("/v1/content-policy");
    now += 300; // refresh due; the policy held is valid to NOW + 890
    await attempt();
    assert.equal(fetches(), 2);
    for (let i = 0; i < 3; i++) await client.chat.completions.create(CALL); // the policy held is applied
    now += 9.5;
    await client.chat.completions.create(CALL);
    assert.equal(fetches(), 2);
    assert.ok(calls.every((call) => JSON.stringify(call.messages) === JSON.stringify(CLEANED)));
    now += 0.5;
    await attempt();
    assert.equal(fetches(), 3);
  });
}

test("once no valid policy is held, every call tries again", async () => {
  // Never delay an attempt that could unblock the call: past the policy's
  // expiry nothing may be applied, so each call asks, and fails closed.
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  const fetches = () => cloud.paths().filter((p) => p === "/v1/content-policy").length;
  await client.chat.completions.create(CALL);
  cloud.policyStatus = 503;
  now = NOW + 885;
  await client.chat.completions.create(CALL); // an attempt, failed; the policy held is still valid
  now = NOW + 890; // expired, five seconds after that attempt
  for (let attempt = 0; attempt < 3; attempt++) {
    await assert.rejects(client.chat.completions.create(CALL), ConsentUnreachableError);
    assert.equal(fetches(), 3 + attempt);
  }
  cloud.policyStatus = 200;
  // The cloud is back, signing at its own time: the very next call recovers.
  cloud.answer = answerWith(jws(cloud.key, claimsFor(policyDocument(), { iat: NOW + 880, exp: NOW + 1780 })));
  await client.chat.completions.create(CALL);
  assert.equal(calls.length, 3);
});

test("a clock running behind keeps a policy no longer than its lifetime", async () => {
  // Four minutes behind, within the tolerance: by this machine's wall clock the
  // policy would stand four minutes past its real expiry. Its lifetime counts
  // from its reception, on the monotonic clock: `exp - iat` seconds.
  const cloud = new Cloud();
  now = NOW - 240;
  mono = 5_000;
  const { client, calls } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  cloud.policyStatus = 503;
  now += 899;
  mono += 899;
  await client.chat.completions.create(CALL);
  assert.deepEqual(calls.at(-1)!.messages, CLEANED);
  now += 1;
  mono += 1;
  await assert.rejects(client.chat.completions.create(CALL), ConsentUnreachableError);
});

/** A policy signed `age` seconds before it arrives, as a short cache serves it. */
function agedAnswer(cloud: Cloud, age: number): unknown {
  const claims = claimsFor(policyDocument(), { iat: NOW - age, exp: NOW - age + 900 });
  return { enabled: true, policy: jws(cloud.key, claims), challenge: CHALLENGE, refresh_after: 300 };
}

test("an aged policy on a clock behind ends at the earlier bound", async () => {
  // Signed 30 s before it arrives, on a machine four minutes behind. By this
  // machine's wall clock it would stand four minutes past its real `exp`; from
  // its reception, `exp - iat` stands 30 s past it. Held: the earlier of the
  // two — never applied once the real `exp` is 30 s gone.
  const cloud = new Cloud();
  cloud.answer = agedAnswer(cloud, 30);
  now = NOW - 240;
  mono = 5_000;
  const { client, calls } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  cloud.policyStatus = 503;
  now += 899; // real time: `exp` + 29 s
  mono += 899;
  await client.chat.completions.create(CALL);
  assert.deepEqual(calls.at(-1)!.messages, CLEANED);
  now += 1; // real time: `exp` + 30 s
  mono += 1;
  await assert.rejects(client.chat.completions.create(CALL), ConsentUnreachableError);
});

test("an aged policy on a clock ahead ends at its signed expiry", async () => {
  // Signed 30 s before it arrives, on a machine two minutes ahead: from its
  // reception it would stand 30 s past `exp`, but this machine's wall clock
  // reaches `exp` two minutes early, and that ends it — the earlier bound.
  const cloud = new Cloud();
  cloud.answer = agedAnswer(cloud, 30);
  now = NOW + 120;
  mono = 5_000;
  const { client, calls } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  cloud.policyStatus = 503;
  now += 749; // this machine: `exp` - 1 s
  mono += 749;
  await client.chat.completions.create(CALL);
  assert.deepEqual(calls.at(-1)!.messages, CLEANED);
  now += 1; // this machine: `exp`
  mono += 1;
  await assert.rejects(client.chat.completions.create(CALL), ConsentUnreachableError);
});

test("the wall-clock expiry still bounds a held policy", async () => {
  // A clock set forward past `exp` while the process runs: the policy is no
  // longer applied, whatever the monotonic clock says.
  const cloud = new Cloud();
  mono = 5_000;
  const { client } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  cloud.policyStatus = 503;
  now = NOW + 890;
  await assert.rejects(client.chat.completions.create(CALL), ConsentUnreachableError);
});

test("a policy asked to refresh keeps its wall-clock expiry", async () => {
  // An `outdated` answer makes the policy held due at once; it must still not
  // be applied past its signed `exp`.
  const cloud = new Cloud();
  cloud.authorizeExtra = { content_policy: { status: "outdated", digest: "sha256:new" } };
  mono = 5_000;
  const { client } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  cloud.policyStatus = 503;
  now = NOW + 890;
  await assert.rejects(client.chat.completions.create(CALL), ConsentUnreachableError);
});

test("refresh and its spacing count on the monotonic clock", async () => {
  // The wall clock stands still: time passing is read on the monotonic one.
  const cloud = new Cloud();
  mono = 5_000;
  const { client } = wrapped(cloud);
  const fetches = () => cloud.paths().filter((p) => p === "/v1/content-policy").length;
  await client.chat.completions.create(CALL);
  mono += 10; // past the spacing, not yet due: nothing is asked
  await client.chat.completions.create(CALL);
  assert.equal(fetches(), 1);
  mono += 290;
  await client.chat.completions.create(CALL);
  assert.equal(fetches(), 2);
  cloud.policyStatus = 503;
  mono += 300;
  await client.chat.completions.create(CALL);
  assert.equal(fetches(), 3);
  mono += 9.5;
  await client.chat.completions.create(CALL);
  assert.equal(fetches(), 3);
  mono += 0.5;
  await client.chat.completions.create(CALL);
  assert.equal(fetches(), 4);
});

test("an applied answer changes nothing", async () => {
  const cloud = new Cloud();
  cloud.authorizeExtra = { content_policy: { status: "applied", digest: "sha256:x" } };
  const { client } = wrapped(cloud);
  for (let i = 0; i < 3; i++) await client.chat.completions.create(CALL);
  assert.equal(cloud.paths().filter((p) => p === "/v1/content-policy").length, 1);
});

for (const [value, expected] of [
  [undefined, undefined],
  [null, undefined],
  ["applied", undefined],
  [[], undefined],
  [{ digest: "x" }, undefined],
  [{ status: 1 }, undefined],
  [{ status: "applied", digest: "d", extra: 1 }, { status: "applied", digest: "d" }],
] as const) {
  test(`the answer field is read leniently: ${JSON.stringify(value)}`, () => {
    assert.deepEqual(contentPolicyAnswer(value), expected);
  });
}

test("the decision carries the answer", async () => {
  const cloud = new Cloud();
  cloud.authorizeExtra = { content_policy: { status: "applied", digest: "d" } };
  const client = new HttpConsentClient("https://cloud.example.test", { fetch: cloud.fetch });
  const decision = await client.authorize({ provider: "openai", model: "m", requestHash: "sha256:" + "0".repeat(64) });
  assert.deepEqual(decision.contentPolicy, { status: "applied", digest: "d" });
});

test("the cache knows a key only from a key set it holds", () => {
  const cache = new PolicyCache();
  assert.equal(cache.knows("k"), false);
  cache.jwks = { keys: "nope" };
  assert.equal(cache.knows("k"), false);
  cache.jwks = { keys: [null, { kid: "k" }] };
  assert.equal(cache.knows("k"), true);
  cache.noteAnswer({ status: "outdated" }, NOW); // nothing held: nothing to refresh
  assert.equal(cache.state, undefined);
});

test("a disabled answer reads with the default refresh", () => {
  assert.equal(readAnswer({ enabled: false }).refreshAfter, 300);
  const state = new PolicyState(undefined, undefined, undefined, 1, 2);
  assert.equal(state.enabled, false);
});

test("the wall clock is the default clock", () => {
  const before = Date.now() / 1000;
  const reading = realNow();
  assert.ok(reading >= before && reading <= Date.now() / 1000);
});

test("the default monotonic clock and pause are the real ones", async () => {
  const first = realMonotonic();
  assert.ok(Math.abs(first - performance.now() / 1000) < 5);
  await realSleep(0);
  assert.ok(realMonotonic() >= first);
});

// ── one fetch in flight per client ──────────────────────────────────────────
//
// A fresh process under load: every call in flight needs the policy at once.
// The stand-in cloud holds its policy answer until every caller is inside
// `contentPolicy()`, so a client that fetched once per caller would be seen
// fetching once per caller — without the hold, the first answer could land
// before the others ask, and the test would pass whatever the client does.

const N_CALLS = 200;
const policyGets = (cloud: Cloud) => cloud.paths().filter((p) => p === "/v1/content-policy").length;
const jwksGets = (cloud: Cloud) => cloud.paths().filter((p) => p === "/.well-known/jwks.json").length;

async function together(cloud: Cloud, callers = N_CALLS) {
  let entered = 0;
  let release!: () => void;
  const allIn = new Promise<void>((resolve) => (release = resolve));
  const fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    if (String(input).endsWith("/v1/content-policy")) {
      let timer: NodeJS.Timeout | undefined;
      const late = new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`only ${entered} callers entered`)), 10_000);
      });
      try {
        await Promise.race([allIn, late]);
      } finally {
        clearTimeout(timer);
      }
      for (let i = 0; i < 10; i++) await new Promise((resolve) => setImmediate(resolve));
    }
    return cloud.fetch(input, init);
  }) as typeof globalThis.fetch;
  const consentClient = new HttpConsentClient("https://cloud.example.test", {
    apiKey: "sk_test_cleaning", // pragma: allowlist secret
    fetch,
  });
  const fetchPolicy = consentClient.contentPolicy.bind(consentClient);
  consentClient.contentPolicy = async () => {
    if (++entered === callers) release();
    return fetchPolicy();
  };
  const calls: Record<string, unknown>[] = [];
  const create = async (body: Record<string, unknown>) => (calls.push(body), { id: "chatcmpl_fake" });
  const client = wrap({ chat: { completions: { create } } }, { provider: "openai", consentClient }) as any;
  const settled = await Promise.allSettled(Array.from({ length: callers }, () => client.chat.completions.create(CALL)));
  const errors = settled.flatMap((result) => (result.status === "rejected" ? [result.reason as unknown] : []));
  return { client, calls, errors };
}

for (const cleaning of [true, false]) {
  test(`simultaneous calls on a fresh client share one fetch with Promise.all: cleaning ${cleaning}`, async () => {
    const cloud = new Cloud();
    if (!cleaning) cloud.policy = null;
    const { calls, errors } = await together(cloud);
    assert.deepEqual(errors, []);
    assert.equal(policyGets(cloud), 1, `${policyGets(cloud)} policy GETs, ${jwksGets(cloud)} JWKS GETs`);
    assert.equal(jwksGets(cloud), cleaning ? 1 : 0, `${jwksGets(cloud)} JWKS GETs`);
    assert.equal(calls.length, N_CALLS);
    const expected = JSON.stringify(cleaning ? CLEANED : MESSAGES);
    assert.ok(calls.every((call) => JSON.stringify(call.messages) === expected));
  });
}

test("a failed shared fetch reaches every waiter, then is tried again", async () => {
  const unhandled: unknown[] = [];
  const onUnhandled = (reason: unknown) => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandled);
  try {
    const cloud = new Cloud();
    cloud.policyStatus = 503;
    const { client, calls, errors } = await together(cloud);
    assert.equal(errors.length, N_CALLS);
    assert.ok(errors.every((error) => error instanceof ConsentUnreachableError));
    assert.equal(policyGets(cloud), 1);
    // A failure is never kept: the next call asks again, and recovers.
    cloud.policyStatus = 200;
    await client.chat.completions.create(CALL);
    assert.equal(policyGets(cloud), 2);
    assert.deepEqual(calls[0].messages, CLEANED);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(unhandled, []);
  } finally {
    process.off("unhandledRejection", onUnhandled);
  }
});

// ── a 429 on the policy route ───────────────────────────────────────────────

/** What the SDK waited, in seconds, instead of waiting it. */
function recordSleeps(): number[] {
  const slept: number[] = [];
  clock.sleep = async (seconds: number) => {
    slept.push(seconds);
  };
  return slept;
}

for (const [header, waited] of [
  ["1", 1],
  ["0", 0],
  ["2", 2],
  ["60", 2],
  [" 1\t", 1],
  ["9".repeat(400), 2],
] as const) {
  test(`a 429 with no policy held waits its Retry-After, then asks once more: ${header.slice(0, 8)}`, async () => {
    const slept = recordSleeps();
    const cloud = new Cloud();
    cloud.limited.push({ "Retry-After": header });
    const { client, calls } = wrapped(cloud);
    await client.chat.completions.create(CALL);
    assert.deepEqual(slept, [waited]); // delta-seconds, at most two
    assert.equal(policyGets(cloud), 2);
    assert.deepEqual(calls[0].messages, CLEANED);
  });
}

for (const header of [undefined, "", "1.5", "-1", "+1", "1_0", "1 2", "0x10", "1e1", "Wed, 21 Oct 2026 07:28 GMT"]) {
  test(`a 429 without a usable Retry-After fails closed at once: ${JSON.stringify(header)}`, async () => {
    const slept = recordSleeps();
    const cloud = new Cloud();
    cloud.limited.push(header === undefined ? {} : { "Retry-After": header });
    const { client, calls } = wrapped(cloud);
    await assert.rejects(client.chat.completions.create(CALL), RateLimitedError);
    assert.deepEqual(slept, []);
    assert.equal(policyGets(cloud), 1);
    assert.deepEqual(calls, []);
  });
}

test("a 429 is retried once only", async () => {
  const slept = recordSleeps();
  const cloud = new Cloud();
  cloud.limited.push({ "Retry-After": "1" }, { "Retry-After": "1" });
  const { client, calls } = wrapped(cloud);
  await assert.rejects(client.chat.completions.create(CALL), RateLimitedError);
  assert.deepEqual(slept, [1]);
  assert.equal(policyGets(cloud), 2);
  assert.deepEqual(calls, []);
});

test("a 429 while a valid policy is held waits for nothing", async () => {
  const slept = recordSleeps();
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  cloud.limited.push({ "Retry-After": "1" });
  now += 300; // refresh due; the policy held is still valid
  await client.chat.completions.create(CALL);
  assert.deepEqual(slept, []);
  assert.equal(policyGets(cloud), 2);
  assert.deepEqual(calls.at(-1)!.messages, CLEANED);
});

test("a 429 is retried once per fetch, not once per caller", async () => {
  const slept = recordSleeps();
  const cloud = new Cloud();
  cloud.limited.push({ "Retry-After": "1" });
  const { errors } = await together(cloud);
  assert.deepEqual(errors, []);
  assert.deepEqual(slept, [1]);
  assert.equal(policyGets(cloud), 2);
});

// ── what cleaning writes back into the call ─────────────────────────────────

test("a branch cleaning did not touch leaves as the caller's own object", async () => {
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  const earlier = { role: "assistant", content: "Noted." };
  // What a helper hangs on an argument out of JSON's sight (a parser, a brand).
  const parser = () => "parsed";
  const format = { type: "json_schema", json_schema: { name: "contact", description: "As for marie@example.com" } };
  Object.defineProperty(format, "$parseRaw", { value: parser, enumerable: false });
  await client.chat.completions.create({
    model: "gpt-4o",
    messages: [earlier, { role: "user", content: "Write to marie@example.com" }],
    response_format: format,
    user_id: "usr_marie_456",
  });
  const sent = calls[0] as { messages: unknown[]; response_format: Record<string, any> };
  assert.equal(sent.messages[0], earlier, "an untouched message is the object that was passed");
  assert.deepEqual(sent.messages[1], { role: "user", content: "Write to [EMAIL_1]" });
  assert.equal(sent.response_format.json_schema.description, "As for [EMAIL_1]");
  assert.equal(sent.response_format.$parseRaw, parser, "what JSON does not write stays on the argument");
  assert.equal(format.json_schema.description, "As for marie@example.com"); // never mutated
  const [authorize] = cloud.bodies("/v1/authorize");
  assert.equal(authorize.request_hash, requestHash(JSON.parse(JSON.stringify({ ...sent, user_id: undefined }))));
});

test("frozen arguments, as a state library hands them over, are cleaned without being touched", async () => {
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  const freeze = <T>(value: T): T => {
    if (typeof value === "object" && value !== null) Object.values(value).forEach(freeze);
    return Object.freeze(value);
  };
  const part = { type: "text", text: "Write to marie@example.com" };
  const messages = freeze([
    { role: "system", content: "Be brief." },
    { role: "user", content: [part] },
  ]);
  await client.chat.completions.create({ model: "gpt-4o", messages, user_id: "usr_marie_456" });
  const sent = calls[0].messages as unknown[];
  assert.equal(sent[0], messages[0]);
  assert.deepEqual(sent[1], { role: "user", content: [{ type: "text", text: "Write to [EMAIL_1]" }] });
  assert.equal(part.text, "Write to marie@example.com");
});

test("a gemini call that carries an abort signal is cleaned, and keeps its signal", async () => {
  const cloud = new Cloud();
  const calls: Record<string, any>[] = [];
  const consentClient = new HttpConsentClient("https://cloud.example.test", { fetch: cloud.fetch });
  const client = wrap(
    { models: { generateContent: async (body: Record<string, unknown>) => (calls.push(body), { text: "ok" }) } },
    { provider: "gemini", consentClient },
  ) as any;
  const abortSignal = new AbortController().signal;
  await client.models.generateContent({
    model: "gemini-2.5-flash",
    contents: "hello",
    config: { systemInstruction: "Escalate to marie@example.com", abortSignal },
  });
  await client.models.generateContent({ model: "gemini-2.5-flash", contents: "hello", config: { abortSignal } });
  assert.equal(calls[0].config.systemInstruction, "Escalate to [EMAIL_1]");
  assert.equal(calls[0].config.abortSignal, abortSignal, "the signal that cancels the call is still on it");
  assert.equal(calls[1].config.abortSignal, abortSignal);
  const [first] = cloud.bodies("/v1/authorize");
  assert.equal(
    first.request_hash,
    requestHash({ model: "gemini-2.5-flash", contents: "hello", config: { systemInstruction: "Escalate to [EMAIL_1]" } }),
  );
});

test("a key named __proto__ is hashed and cleaned like any other", async () => {
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  // JSON.parse makes it a property of its own; a plain assignment would lose it.
  const metadata = JSON.parse('{"__proto__": "about marie@example.com", "ref": "r1"}') as Record<string, unknown>;
  const messages = [{ role: "user", content: "hello" }];
  await client.chat.completions.create({ model: "gpt-4o", messages, metadata, user_id: "usr_marie_456" });
  const wire = JSON.stringify(calls[0]);
  assert.ok(!wire.includes("marie@example.com"), wire);
  assert.ok(wire.includes('"__proto__":"about [EMAIL_1]"'), wire);
  const [authorize] = cloud.bodies("/v1/authorize");
  assert.deepEqual((authorize.content_policy as Record<string, unknown>).counts, { email: 1 });
  assert.equal(authorize.request_hash, requestHash(JSON.parse(JSON.stringify({ ...calls[0], user_id: undefined }))));
  // And when nothing is masked, the hash still covers it.
  const plain = JSON.parse('{"__proto__": "nothing here"}') as Record<string, unknown>;
  await client.chat.completions.create({ model: "gpt-4o", messages, metadata: plain, user_id: "usr_marie_456" });
  assert.equal(cloud.bodies("/v1/authorize")[1].request_hash, requestHash({ model: "gpt-4o", messages, metadata: plain }));
});

test("an object that writes itself differently each time leaves as it was cleaned", async () => {
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  let written = 0;
  const shifting = { toJSON: () => ({ role: "user", content: `reading ${++written}` }) };
  await client.chat.completions.create({
    model: "gpt-4o",
    messages: [{ role: "user", content: "Write to marie@example.com" }, shifting],
    user_id: "usr_marie_456",
  });
  const sent = calls[0].messages as Record<string, unknown>[];
  // The reading that was cleaned is the one that leaves, as plain JSON: the
  // object itself would have written a third one on its way out.
  assert.deepEqual(sent, [
    { role: "user", content: "Write to [EMAIL_1]" },
    { role: "user", content: "reading 2" },
  ]);
  const [authorize] = cloud.bodies("/v1/authorize");
  assert.equal(authorize.request_hash, requestHash({ model: "gpt-4o", messages: sent }));
});

test("an adapter of your own, with the three documented methods, is cleaned too", async () => {
  const cloud = new Cloud();
  const generic = new GenericAdapter(loadDescriptors().get("openai")!);
  const calls: Record<string, unknown>[] = [];
  const client = wrap(
    { chat: { completions: { create: async (body: Record<string, unknown>) => (calls.push(body), {}) } } },
    {
      adapter: {
        descriptor: generic.descriptor,
        name: generic.name,
        extractRequestMetadata: (params) => generic.extractRequestMetadata(params),
        applyConstraints: (params, constraints, callSite) => generic.applyConstraints(params, constraints, callSite),
        buildEnforcedArgs: (args, forced, token, headers) => generic.buildEnforcedArgs(args, forced, token, headers),
      },
      consentClient: new HttpConsentClient("https://cloud.example.test", { fetch: cloud.fetch }),
    },
  ) as any;
  await client.chat.completions.create(CALL);
  assert.deepEqual(calls[0].messages, CLEANED);
});

// ── through the REAL provider libraries: what reaches the wire ──────────────

const POLICY = policyDocument({ detectors: { email: "mask", phone: "mask", credit_card: "block" }, custom: [] });

function capture(answer: unknown) {
  const bodies: Record<string, unknown>[] = [];
  const fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const text = input instanceof Request ? await input.clone().text() : String(init?.body ?? "");
    bodies.push(JSON.parse(text) as Record<string, unknown>);
    return new Response(JSON.stringify(answer), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof globalThis.fetch;
  return { fetch, bodies };
}

function cloudClient(): { cloud: Cloud; consentClient: HttpConsentClient } {
  const cloud = new Cloud();
  cloud.policy = POLICY;
  return { cloud, consentClient: new HttpConsentClient("https://cloud.example.test", { fetch: cloud.fetch }) };
}

const CHAT = {
  id: "c",
  object: "chat.completion",
  created: 0,
  model: "m",
  choices: [{ index: 0, finish_reason: "stop", message: { role: "assistant", content: "ok" } }],
  usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
};

for (const provider of ["openai", "deepseek"] as const) {
  test(`real ${provider} client: the cleaned body is what leaves`, async () => {
    const { cloud, consentClient } = cloudClient();
    const wire = capture(CHAT);
    const raw = new OpenAI({
      apiKey: "sk-test", // pragma: allowlist secret
      fetch: wire.fetch,
      maxRetries: 0,
      ...(provider === "deepseek" ? { baseURL: "https://api.deepseek.com" } : {}),
    });
    const client = wrap(raw, { consentClient });
    await client.chat.completions.create({
      model: "gpt-4o",
      messages: [{ role: "user", content: "Call 06 12 34 56 78 or marie@example.com" }],
      user_id: "usr_marie_456",
    });
    assert.equal((wire.bodies[0].messages as { content: string }[])[0].content, "Call [PHONE_1] or [EMAIL_1]");
    const [authorize] = cloud.bodies("/v1/authorize");
    assert.equal(authorize.provider, provider);
    assert.deepEqual((authorize.content_policy as Record<string, unknown>).counts, { email: 1, phone: 1 });
    await assert.rejects(
      client.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: "4111 1111 1111 1111" }] }),
      ContentBlockedError,
    );
    assert.equal(wire.bodies.length, 1);
  });
}

test("real anthropic client: system and messages are cleaned", async () => {
  const { consentClient } = cloudClient();
  const wire = capture({
    id: "msg_1",
    type: "message",
    role: "assistant",
    model: "claude-x",
    content: [{ type: "text", text: "ok" }],
    stop_reason: "end_turn",
    usage: { input_tokens: 1, output_tokens: 1 },
  });
  const client = wrap(new Anthropic({ apiKey: "sk-ant-test", fetch: wire.fetch, maxRetries: 0 }), { consentClient }); // pragma: allowlist secret
  await client.messages.create({
    model: "claude-x",
    max_tokens: 10,
    system: "Operator: ops@example.com",
    messages: [{ role: "user", content: [{ type: "text", text: "call 06 12 34 56 78" }] }],
  });
  assert.equal(wire.bodies[0].system, "Operator: [EMAIL_1]");
  assert.equal(((wire.bodies[0].messages as any)[0].content[0] as { text: string }).text, "call [PHONE_1]");
});

test("real gemini client: contents and config are cleaned", async () => {
  const { consentClient } = cloudClient();
  const wire = capture({ candidates: [{ content: { role: "model", parts: [{ text: "ok" }] }, finishReason: "STOP" }] });
  const original = globalThis.fetch;
  globalThis.fetch = wire.fetch;
  try {
    const client = wrap(
      new GoogleGenAI({ apiKey: "gemini-test-key", httpOptions: { baseUrl: "https://gemini.invalid" } }), // pragma: allowlist secret
      { consentClient },
    );
    await client.models.generateContent({
      model: "gemini-2.5-flash",
      contents: "mail marie@example.com",
      config: { systemInstruction: "ops@example.com decides", temperature: 0.1 },
    });
  } finally {
    globalThis.fetch = original;
  }
  const text = JSON.stringify(wire.bodies[0]);
  assert.ok(!text.includes("marie@example.com") && !text.includes("ops@example.com"), text);
  assert.ok(text.includes("[EMAIL_1]") && text.includes("[EMAIL_2]"), text);
});

test("real mistral client: messages are cleaned", async () => {
  const { consentClient } = cloudClient();
  const wire = capture(CHAT);
  const client = wrap(new Mistral({ apiKey: "sk-mistral-test", httpClient: new HTTPClient({ fetcher: wire.fetch }) }), { // pragma: allowlist secret
    consentClient,
  });
  await client.chat.complete({ model: "mistral-small", messages: [{ role: "user", content: "mail marie@example.com" }] });
  assert.equal((wire.bodies[0].messages as { content: string }[])[0].content, "mail [EMAIL_1]");
});

// ── timeouts, hints and messages ────────────────────────────────────────────

/** A fetch that answers only when its request is aborted, by rejecting. */
const hanging = (async (_url: string | URL | Request, init?: RequestInit) =>
  new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  })) as typeof globalThis.fetch;

test("a policy fetch that does not answer in time is an unreachable cloud", async () => {
  const client = new HttpConsentClient("https://cloud.example.test", { fetch: hanging, timeoutMs: 5 });
  await assert.rejects(client.contentPolicy(), ConsentUnreachableError);
});

test("a report that does not answer in time is dropped, and needs no key", async () => {
  const seen: Headers[] = [];
  const fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    seen.push(new Headers(init?.headers));
    return hanging(url, init);
  }) as typeof globalThis.fetch;
  const previous = process.env.IDSIDE_API_KEY;
  delete process.env.IDSIDE_API_KEY;
  try {
    const client = new HttpConsentClient("https://cloud.example.test", { fetch, timeoutMs: 5 });
    await client.contentEvents({ digest: "sha256:x", engine: "1", blocked: { email: 1 } });
  } finally {
    if (previous !== undefined) process.env.IDSIDE_API_KEY = previous;
  }
  assert.equal(seen[0].get("authorization"), null);
});

/** An answer whose headers arrive and whose body then fails as `how` says. */
function brokenBody(how: "error" | "stall"): typeof globalThis.fetch {
  return (async (_url: string | URL | Request, init?: RequestInit) => {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('{"enabled": tr'));
        if (how === "error") controller.error(new TypeError("terminated"));
        else init?.signal?.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")));
      },
    });
    return new Response(body, { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof globalThis.fetch;
}

for (const how of ["error", "stall"] as const) {
  // A deadline of its own: a regression here hangs instead of failing.
  test(`a policy answer whose body does not arrive is an unreachable cloud: ${how}`, { timeout: 5000 }, async () => {
    const client = new HttpConsentClient("https://cloud.example.test", { fetch: brokenBody(how), timeoutMs: 20 });
    await assert.rejects(client.contentPolicy(), (error: unknown) => {
      assert.ok(error instanceof ConsentUnreachableError, String(error));
      assert.match(error.message, /could not be fetched/);
      return true;
    });
  });
}

test("a refresh whose body does not arrive keeps a policy still valid", async () => {
  const cloud = new Cloud();
  let broken = false;
  const fetch = ((url: string | URL | Request, init?: RequestInit) =>
    broken && new URL(String(url)).pathname === "/v1/content-policy"
      ? brokenBody("error")(url, init)
      : cloud.fetch(url, init)) as typeof globalThis.fetch;
  const calls: Record<string, unknown>[] = [];
  const client = wrap(
    { chat: { completions: { create: async (body: Record<string, unknown>) => (calls.push(body), {}) } } },
    { provider: "openai", consentClient: new HttpConsentClient("https://cloud.example.test", { fetch }) },
  ) as any;
  await client.chat.completions.create(CALL);
  broken = true;
  now += 600;
  await client.chat.completions.create(CALL);
  assert.deepEqual(calls.at(-1)!.messages, CLEANED);
});

test("a policy that ran out while its refresh was failing is not applied", async () => {
  const cloud = new Cloud();
  let late = false;
  // The refresh takes its time, and fails: by then the policy held has expired.
  const fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    if (late && new URL(String(url)).pathname === "/v1/content-policy") {
      now = NOW + 890;
      throw new TypeError("fetch failed");
    }
    return cloud.fetch(url, init);
  }) as typeof globalThis.fetch;
  const calls: Record<string, unknown>[] = [];
  const client = wrap(
    { chat: { completions: { create: async (body: Record<string, unknown>) => (calls.push(body), {}) } } },
    { provider: "openai", consentClient: new HttpConsentClient("https://cloud.example.test", { fetch }) },
  ) as any;
  await client.chat.completions.create(CALL);
  now += 600;
  late = true;
  await assert.rejects(client.chat.completions.create(CALL), ConsentUnreachableError);
  assert.equal(calls.length, 1);
});

test("a signed policy that names no digest is refused, whatever it holds", async () => {
  const cloud = new Cloud();
  const claims = claimsFor(policyDocument());
  claims.policy = { ...policyDocument(), note: "\ud800" };
  delete claims.digest;
  cloud.answer = answerWith(jws(cloud.key, claims));
  await refused(cloud, /digest/);
});

test("a refresh that brings the same policy compiles nothing again", async (t) => {
  const compile = t.mock.method(compiler, "cleaner");
  const cloud = new Cloud();
  cloud.policy = policyDocument({ custom: [] });
  const { client, calls } = wrapped(cloud);
  await client.chat.completions.create(CALL);
  const once = compile.mock.callCount();
  assert.ok(once > 0);
  // Due for refresh: fetched and verified again, the same digest.
  now += 400;
  await client.chat.completions.create(CALL);
  assert.equal(cloud.paths().filter((path) => path === "/v1/content-policy").length, 2);
  assert.equal(compile.mock.callCount(), once);
  // A new digest is a new policy: compiled, and applied.
  cloud.policy = policyDocument();
  now += 400;
  await client.chat.completions.create(CALL);
  assert.equal(cloud.paths().filter((path) => path === "/v1/content-policy").length, 3);
  assert.equal(compile.mock.callCount(), 2 * once);
  assert.deepEqual(
    calls.map((sent) => sent.messages),
    [[MESSAGES[0], CLEANED[1]], [MESSAGES[0], CLEANED[1]], CLEANED],
  );
});

test("a policy answer with no body (204) is refused as not a JSON object, as in Python", async () => {
  const cloud = new Cloud();
  const inner = cloud.fetch;
  const fetch = ((url: string | URL | Request, init?: RequestInit) =>
    new URL(String(url)).pathname === "/v1/content-policy"
      ? Promise.resolve(new Response(null, { status: 204 }))
      : inner(url, init)) as typeof globalThis.fetch;
  (cloud as { fetch: typeof globalThis.fetch }).fetch = fetch;
  await refused(cloud, /is not a JSON object/);
});

test("an argument with no prototype keeps none once a string in it is masked", async () => {
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  const metadata = Object.assign(Object.create(null) as Record<string, unknown>, { note: "about marie@example.com" });
  await client.chat.completions.create({ ...CALL, metadata });
  const sent = calls[0].metadata as Record<string, unknown>;
  assert.equal(sent.note, "about [EMAIL_1]");
  assert.equal(Object.getPrototypeOf(sent), null);
  assert.equal(metadata.note, "about marie@example.com"); // never mutated
});

test("a top-level argument named __proto__, as JSON.parse makes one, is hashed and cleaned", async () => {
  const cloud = new Cloud();
  const { client, calls } = wrapped(cloud);
  // A body forwarded as parsed: the key is a property of its own.
  const params = JSON.parse(
    '{"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}], "__proto__": {"note": "about marie@example.com"}}',
  ) as Record<string, unknown>;
  await client.chat.completions.create(params);
  const wire = JSON.stringify(calls[0]);
  assert.ok(!wire.includes("marie@example.com"), wire);
  assert.ok(wire.includes('"__proto__":{"note":"about [EMAIL_1]"}'), wire);
  const [authorize] = cloud.bodies("/v1/authorize");
  assert.equal(authorize.request_hash, requestHash(JSON.parse(wire)));
});

test("an unreachable default cloud says which address was used", async () => {
  const previous = process.env.IDSIDE_BASE_URL;
  delete process.env.IDSIDE_BASE_URL;
  try {
    const failing = (async () => {
      throw new TypeError("fetch failed");
    }) as typeof globalThis.fetch;
    const client = new HttpConsentClient(undefined, { fetch: failing });
    await assert.rejects(client.contentPolicy(), (error: unknown) => {
      assert.ok(error instanceof ConsentUnreachableError);
      assert.match(error.message, /IDSIDE_BASE_URL/);
      return true;
    });
  } finally {
    if (previous !== undefined) process.env.IDSIDE_BASE_URL = previous;
  }
});

test("a blocked error lists its rules sorted, with their counts", () => {
  const error = new ContentBlockedError({ email: 2, attachments: 1, "custom:CODENAME": 3 });
  assert.deepEqual(Object.keys(error.blocked), ["attachments", "custom:CODENAME", "email"]);
  assert.match(error.message, /found attachments \(1\), custom:CODENAME \(3\), email \(2\)\./);
  assert.deepEqual(Object.keys(new ContentBlockedError({ a: 1, b: 1 }).blocked), ["a", "b"]);
});
