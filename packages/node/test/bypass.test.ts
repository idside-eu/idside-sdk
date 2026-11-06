import { test } from "node:test";
import assert from "node:assert/strict";
import { wrap } from "../src/consent.ts";
import type { ConsentClient, ConsentDecision, AuthorizeRequest } from "../src/transport.ts";

// Regression tests for the consent-bypass class: a provider convenience method
// (parse/stream/runTools) that internally calls `this.create(...)` must go
// through the enforced version. A read-only Proxy could not catch these — the
// internal call hits the real object — so enforcement patches the method in place.
const DECISION: ConsentDecision = {
  consentState: {},
  paramsForced: {},
  actToken: "act.jwt.token",
  source: "widget",
  legalBasis: "consent",
  binding: "ubt",
  constraints: {},
};

function fakeConsentClient(): ConsentClient & { calls: AuthorizeRequest[] } {
  const calls: AuthorizeRequest[] = [];
  return {
    calls,
    async authorize(request: AuthorizeRequest): Promise<ConsentDecision> {
      calls.push(request);
      return DECISION;
    },
  };
}

// Mirrors the real anthropic Messages resource: parse() and stream() route
// through this.create(...) on the same instance (messages.js / MessageStream.js).
function fakeAnthropic() {
  const calls: { body: Record<string, unknown>; options?: { headers?: Record<string, string> } }[] = [];
  const messages = {
    create(body: Record<string, unknown>, options?: { headers?: Record<string, string> }) {
      calls.push({ body, options });
      return Promise.resolve({ id: "msg_1" });
    },
    parse(body: Record<string, unknown>, options?: { headers?: Record<string, string> }) {
      return (this as typeof messages).create(body, options);
    },
    stream(body: Record<string, unknown>, options?: { headers?: Record<string, string> }) {
      return (this as typeof messages).create({ ...body, stream: true }, options);
    },
  };
  return { client: { messages }, calls };
}

test("parse() (which calls this.create) is enforced — no consent bypass", async () => {
  const { client, calls } = fakeAnthropic();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "anthropic" });

  await wrapped.messages.parse({ model: "claude-3", messages: [], user_id: "usr_marie_456" });

  assert.equal(cc.calls.length, 1, "parse() must trigger an authorize");
  assert.equal(calls[0]!.body.user_id, undefined, "identity must be stripped");
  assert.equal(calls[0]!.options?.headers?.["X-AI-Consent-Token"], "act.jwt.token");
});

test("stream() (which calls this.create) is enforced — no consent bypass", async () => {
  const { client, calls } = fakeAnthropic();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "anthropic" });

  await wrapped.messages.stream({ model: "claude-3", messages: [], user_id: "u" });

  assert.equal(cc.calls.length, 1, "stream() must trigger an authorize");
  assert.equal(calls[0]!.body.stream, true);
  assert.equal(calls[0]!.options?.headers?.["X-AI-Consent-Token"], "act.jwt.token");
});

test("wrap() enforces in place and returns the same client instance (Python parity)", () => {
  const { client } = fakeAnthropic();
  const cc = fakeConsentClient();
  const once = wrap(client, { consentClient: cc, provider: "anthropic" });
  assert.equal(once, client, "wrap() must mutate and return the same object");
  const twice = wrap(once, { consentClient: cc, provider: "anthropic" });
  assert.equal(twice, client, "re-wrapping is idempotent and still the same object");
});

test("a numeric or boolean identity is rejected like the Python SDK's truthiness", async () => {
  const { client, calls } = fakeAnthropic();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "anthropic" });

  // 0 / false are falsy → treated as absent (no identity) → MissingIdentityError.
  // TypeScript refuses them outright now, which is why each call needs the
  // directive; what this test still pins is the runtime half, for the callers
  // who reach the SDK from plain JavaScript and get no compiler at all.
  // @ts-expect-error a numeric identity is not an identity
  await assert.rejects(wrapped.messages.create({ model: "claude-3", messages: [], user_id: 0 }));
  // @ts-expect-error a boolean identity is not an identity
  await assert.rejects(wrapped.messages.create({ model: "claude-3", messages: [], ubt: false }));
  assert.equal(cc.calls.length, 0);
  assert.equal(calls.length, 0);
});
