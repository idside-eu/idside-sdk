/**
 * The streaming helpers: `chat.completions.stream()` and `responses.stream()`.
 *
 * A streaming call is an inference call: same prompt, same retention, same proof
 * needed. Nothing tested it in either SDK, and the two SDKs reach the guarantee
 * by different routes — which is exactly the kind of asymmetry that survives
 * only until nobody is looking.
 *
 * Here the helper is NOT patched: it returns its stream object synchronously and
 * an enforced wrapper is async, so patching it would hand every caller a Promise
 * where they expect a stream (see UNPATCHABLE_ALIASES in src/consent.ts). It
 * does not need to be: the helper forwards ONE body object — identity included —
 * to `client.chat.completions.create`, which is patched. Python's helpers take a
 * fixed keyword signature instead, so there the identity kwarg is a TypeError
 * and `stream` has to be a call site of its own.
 *
 * These tests assert the outcome both SDKs owe the user, whichever route they
 * took: one authorize, one ACT token, on the wire.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import OpenAI from "openai";
import { wrap } from "../src/consent.ts";
import type { Enforced } from "../src/enforced.ts";
import { MissingIdentityError } from "../src/exceptions.ts";
import { loadDescriptors } from "../src/descriptors.ts";
import type { ConsentClient, ConsentDecision, AuthorizeRequest } from "../src/transport.ts";

const DECISION: ConsentDecision = {
  consentState: { "retention.conversation": false },
  paramsForced: { store: false },
  actToken: "act.jwt.token",
  source: "widget",
  legalBasis: "consent",
  binding: "user_id",
  constraints: {},
};

const MESSAGES = [{ role: "user" as const, content: "hello" }];

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

interface Wire {
  readonly calls: { method: string; path: string; token: string | null; body: string }[];
  readonly fetch: (url: unknown, init?: unknown) => Promise<Response>;
}

/** A `fetch` that records what would have gone out, then refuses to go out. */
function probeFetch(): Wire {
  const calls: Wire["calls"][number][] = [];
  return {
    calls,
    async fetch(url: unknown, init?: unknown): Promise<Response> {
      const options = (init ?? {}) as {
        method?: string;
        headers?: ConstructorParameters<typeof Headers>[0];
        body?: unknown;
      };
      calls.push({
        method: (options.method ?? "GET").toUpperCase(),
        path: new URL(String(url)).pathname,
        token: new Headers(options.headers).get("X-AI-Consent-Token"),
        body: typeof options.body === "string" ? options.body : "",
      });
      throw new Error("PROBE: an HTTP request left the process");
    },
  };
}

/** The error itself, or whatever a provider helper re-threw it inside. */
function causeChain(error: unknown): unknown[] {
  const chain: unknown[] = [];
  let current = error;
  while (current !== undefined && current !== null && chain.length < 8) {
    chain.push(current);
    current = (current as { cause?: unknown }).cause;
  }
  return chain;
}

function probe(): { client: Enforced<OpenAI>; consent: ReturnType<typeof fakeConsentClient>; wire: Wire } {
  const wire = probeFetch();
  const raw = new OpenAI({ apiKey: "sk-test", fetch: wire.fetch as typeof fetch, maxRetries: 0 }); // pragma: allowlist secret
  const consent = fakeConsentClient();
  return { client: wrap(raw, { consentClient: consent }), consent, wire };
}

// ── the descriptor declares it; this SDK deliberately does not patch it ───────

test("the openai descriptor declares `stream`, and this SDK skips it by design", () => {
  const openai = loadDescriptors().get("openai");
  assert.ok(openai, "the openai descriptor is missing");
  for (const id of ["chat.completions.create", "responses.create"]) {
    const site = openai.callSites.find((s) => s.id === id);
    assert.ok(site, `${id} is missing from the descriptor`);
    assert.ok(site.aliases.includes("stream"), `${id} does not declare the stream alias`);
  }

  // Skipped, not patched: the helper must still return its stream object
  // synchronously, which an async wrapper could not do.
  const raw = new OpenAI({ apiKey: "sk-test", maxRetries: 0 }); // pragma: allowlist secret
  const before = raw.chat.completions.stream;
  wrap(raw, { consentClient: fakeConsentClient() });
  assert.equal(raw.chat.completions.stream, before, "stream must not be patched in the Node SDK");
});

// ── the real client, recorded at the transport ───────────────────────────────

test("chat.completions.stream() is enforced and the token reaches the wire", async () => {
  const { client, consent, wire } = probe();

  await assert.rejects(async () => {
    const stream = client.chat.completions.stream({
      model: "gpt-5",
      messages: MESSAGES,
      user_id: "usr_marie_456",
    } as never);
    for await (const _chunk of stream) void _chunk;
  });

  assert.equal(consent.calls.length, 1, "exactly one authorize per streaming call");
  assert.equal(consent.calls[0].callSite, "chat.completions.create");
  assert.deepEqual(
    wire.calls.map((c) => [c.method, c.path, c.token]),
    [["POST", "/v1/chat/completions", "act.jwt.token"]],
  );
  // The forced parameter is on the streaming request too: a stream is not a way
  // to opt out of what the user refused.
  assert.match(wire.calls[0].body, /"store":false/);
  assert.doesNotMatch(wire.calls[0].body, /user_id/);
});

test("responses.stream() is enforced and the token reaches the wire", async () => {
  const { client, consent, wire } = probe();

  await assert.rejects(async () => {
    const stream = client.responses.stream({
      model: "gpt-5",
      input: "hello",
      user_id: "usr_marie_456",
    } as never);
    for await (const _event of stream) void _event;
  });

  assert.equal(consent.calls.length, 1);
  assert.equal(consent.calls[0].callSite, "responses.create");
  assert.deepEqual(
    wire.calls.map((c) => [c.method, c.path, c.token]),
    [["POST", "/v1/responses", "act.jwt.token"]],
  );
});

test("a stream without an identity never reaches the wire", async () => {
  const { client, consent, wire } = probe();

  await assert.rejects(
    async () => {
      const stream = client.chat.completions.stream({ model: "gpt-5", messages: MESSAGES } as never);
      for await (const _chunk of stream) void _chunk;
    },
    // The provider's stream runner re-throws through its own error type, so the
    // security stop travels as a `cause`: the assertion follows the chain rather
    // than the top-level class, which is what a caller has to do too.
    (error: unknown) => causeChain(error).some((e) => e instanceof MissingIdentityError),
  );

  assert.deepEqual(consent.calls, []);
  assert.deepEqual(wire.calls, []);
});

// ── the resuming form of the same helper ─────────────────────────────────────

test("resuming a response is not an AI call, and is not enforced as one", async () => {
  // `responses.stream({ response_id })` replays a response that already exists:
  // the JS helper calls `responses.retrieve()`, which is not a call site, so it
  // never reaches enforcement and its GET passes the net untouched. Python
  // reaches the same place from the other side — there the helper IS patched, so
  // the openai descriptor names `resuming_params` and that SDK steps aside. The
  // outcome both SDKs owe: no identity demanded, no token signed, no ledger
  // entry, for a call that creates nothing.
  const { client, consent, wire } = probe();

  await assert.rejects(async () => {
    const stream = client.responses.stream({ response_id: "resp_123" } as never);
    for await (const _event of stream) void _event;
  });

  assert.deepEqual(consent.calls, [], "a call that creates nothing was authorized");
  assert.deepEqual(
    wire.calls.map((c) => [c.method, c.path, c.token]),
    [["GET", "/v1/responses/resp_123", null]],
  );
});

// ── the response helpers on the promise an enforced call returns ─────────────
//
// A provider method returns a promise subclass whose `withResponse()` and
// `asResponse()` give back the raw HTTP response. The enforced wrapper has to
// authorize first, so it is async, and an async function returns a plain
// Promise. Anthropic's own `messages.stream()` calls `create(…).withResponse()`,
// and it failed on every call made through a wrapped client until these were
// forwarded.

function answering(body: unknown): { fetch: typeof fetch; sent: () => number } {
  let sent = 0;
  const fetch = (async () => {
    sent++;
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof globalThis.fetch;
  return { fetch, sent: () => sent };
}

const COMPLETION = {
  id: "chatcmpl_1",
  object: "chat.completion",
  created: 0,
  model: "gpt-5",
  choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
};

test("withResponse() and asResponse() still reach the provider's response, once each", async () => {
  const transport = answering(COMPLETION);
  const consent = fakeConsentClient();
  const client = wrap(new OpenAI({ apiKey: "sk-test", fetch: transport.fetch, maxRetries: 0 }), { // pragma: allowlist secret
    consentClient: consent,
  });
  const body = { model: "gpt-5", messages: MESSAGES, user_id: "usr_marie_456" };

  const { data, response } = await client.chat.completions.create(body).withResponse();
  assert.equal(data.id, "chatcmpl_1");
  assert.equal(response.status, 200);

  const raw = await client.chat.completions.create(body).asResponse();
  assert.equal(raw.status, 200);

  // The promise keeps its own surface too: then / catch / finally.
  let settled = false;
  await client.chat.completions.create(body).finally(() => {
    settled = true;
  });
  assert.ok(settled);
  // The raw body is what asResponse() is for. Adopting the provider's promise
  // eagerly parsed it, and this read threw "Body has already been read".
  assert.equal(JSON.parse(await raw.text()).id, "chatcmpl_1");

  assert.equal(consent.calls.length, 3, "one authorize per call, whichever way it is read");
  assert.equal(transport.sent(), 3, "one request per call, whichever way it is read");
});

test("an enforced call reads as a promise through then, catch and finally alike", async () => {
  const transport = answering(COMPLETION);
  const client = wrap(new OpenAI({ apiKey: "sk-test", fetch: transport.fetch, maxRetries: 0 }), { // pragma: allowlist secret
    consentClient: fakeConsentClient(),
  });
  const body = { model: "gpt-5", messages: MESSAGES, user_id: "usr_marie_456" };

  let finished = false;
  const data = await client.chat.completions
    .create(body)
    .catch(() => undefined)
    .finally(() => {
      finished = true;
    });
  assert.equal((data as { id: string }).id, "chatcmpl_1");
  assert.equal(finished, true);
  assert.equal(transport.sent(), 1);
});

test("messages.stream() on a wrapped Anthropic client authorizes once and streams", async () => {
  const Anthropic = (await import("@anthropic-ai/sdk")).default;
  const events = [
    { type: "message_start", message: { id: "msg_1", type: "message", role: "assistant", model: "claude-sonnet-4-5", content: [], stop_reason: null, usage: { input_tokens: 1, output_tokens: 0 } } },
    { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } },
    { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "ok" } },
    { type: "content_block_stop", index: 0 },
    { type: "message_delta", delta: { stop_reason: "end_turn" }, usage: { output_tokens: 1 } },
    { type: "message_stop" },
  ];
  let sent = 0;
  const fetch = (async () => {
    sent++;
    return new Response(events.map((e) => `event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`).join(""), {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as typeof globalThis.fetch;
  const consent = fakeConsentClient();
  const client = wrap(new Anthropic({ apiKey: "sk-ant-test", fetch, maxRetries: 0 }), { consentClient: consent }); // pragma: allowlist secret

  const message = await client.messages
    .stream({ model: "claude-sonnet-4-5", max_tokens: 16, messages: MESSAGES, user_id: "usr_marie_456" })
    .finalMessage();

  assert.deepEqual(message.content, [{ type: "text", text: "ok" }]);
  assert.equal(consent.calls.length, 1);
  assert.equal(sent, 1);
});

test("a refused call read through withResponse() rejects there, and nowhere else", async () => {
  // The refusal must reach the caller through the helper they used, and must
  // not ALSO surface as an unhandled rejection of the promise they never read.
  const unhandled: unknown[] = [];
  const onUnhandled = (reason: unknown) => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandled);
  try {
    const transport = answering(COMPLETION);
    const client = wrap(new OpenAI({ apiKey: "sk-test", fetch: transport.fetch, maxRetries: 0 }), { // pragma: allowlist secret
      consentClient: fakeConsentClient(),
    });
    // Without the identity TypeScript refuses the call, which is the point of
    // `Enforced<T>`; the cast is how a plain JavaScript caller reaches it.
    const create = client.chat.completions.create as unknown as (
      body: unknown,
    ) => Record<"withResponse" | "asResponse", () => Promise<unknown>>;
    for (const helper of ["withResponse", "asResponse"] as const) {
      await assert.rejects(create({ model: "gpt-5", messages: MESSAGES })[helper](), MissingIdentityError);
    }
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(unhandled, []);
    assert.equal(transport.sent(), 0);
  } finally {
    process.off("unhandledRejection", onUnhandled);
  }
});

test("a provider method that returns no response promise says so, instead of failing obscurely", async () => {
  // A hand-built stand-in returns a plain value: there is no raw response to hand back.
  const client = wrap(
    { chat: { completions: { create: async (_body: Record<string, unknown>) => ({ id: "chatcmpl_1" }) } } },
    { consentClient: fakeConsentClient(), provider: "openai" },
  );
  const call = client.chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: "usr_marie_456" });

  await assert.rejects((call as unknown as { withResponse(): Promise<unknown> }).withResponse(), /withResponse\(\) is not available/);
  assert.deepEqual(await call, { id: "chatcmpl_1" }, "the call itself is still readable");
});

test("the promise an enforced call returns is one Promise.prototype's own methods can be applied to", async () => {
  // The value is a Promise subclass, so anything that captured
  // `Promise.prototype.then` — instrumentation, a tracing library, a zone
  // implementation, plain `Reflect.apply` — will apply it to this object rather
  // than call the method it carries. That used to throw "Promise resolve or
  // reject function is not callable": the subclass named itself as the species
  // its own `then` should construct, and its constructor ignores the executor
  // it would then be handed. `util.inspect` printed `Promise { undefined }` for
  // the other half of the same cause — the inherited state had been resolved
  // with nothing so the lazy read could stay lazy.
  const { inspect } = await import("node:util");
  const transport = answering(COMPLETION);
  const consent = fakeConsentClient();
  const client = wrap(new OpenAI({ apiKey: "sk-test", fetch: transport.fetch, maxRetries: 0 }), { // pragma: allowlist secret
    consentClient: consent,
  });
  const body = { model: "gpt-5", messages: MESSAGES, user_id: "usr_marie_456" };

  // Raced against a deadline: the failure this guards against is a read that
  // never settles, and a test that hangs says less than one that fails.
  const NEVER = Symbol("never settled");
  const settled = async (read: unknown): Promise<unknown> =>
    Promise.race([read, new Promise((resolve) => setTimeout(() => resolve(NEVER), 2_000).unref())]);

  const native = client.chat.completions.create(body);
  assert.ok(native instanceof Promise);
  const through = await settled(Promise.prototype.then.call(native, (value: unknown) => value));
  assert.equal((through as { id: string }).id, "chatcmpl_1");
  const reflected = await settled(Reflect.apply(Promise.prototype.then, native, []));
  assert.equal((reflected as { id: string }).id, "chatcmpl_1", "with no handlers, the value passes straight through");

  // And the object says what it holds, rather than claiming to hold nothing.
  const call = client.chat.completions.create(body);
  assert.match(inspect(call), /<pending>/, "nothing is read until it is read");
  await call;
  assert.match(inspect(call), /chatcmpl_1/);

  assert.equal(consent.calls.length, 2);
  assert.equal(transport.sent(), 2);
});

test("a rejection read only through Promise.prototype.catch is handled there", async () => {
  const unhandled: unknown[] = [];
  const onUnhandled = (reason: unknown) => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandled);
  try {
    const transport = answering(COMPLETION);
    const client = wrap(new OpenAI({ apiKey: "sk-test", fetch: transport.fetch, maxRetries: 0 }), { // pragma: allowlist secret
      consentClient: fakeConsentClient(),
    });
    const create = client.chat.completions.create as unknown as (body: unknown) => Promise<unknown>;

    const caught = await Promise.prototype.catch.call(create({ model: "gpt-5", messages: MESSAGES }), (e: unknown) => e);
    assert.ok(caught instanceof MissingIdentityError);

    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(unhandled, [], "the refusal reached its handler and nowhere else");
    assert.equal(transport.sent(), 0);
  } finally {
    process.off("unhandledRejection", onUnhandled);
  }
});
