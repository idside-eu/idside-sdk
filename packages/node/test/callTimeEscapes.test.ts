/**
 * What a caller could hand a wrapped client, next to the body, to undo enforcement.
 *
 * Every vector here was proved against the real provider libraries before this
 * file existed: a per-request `options.body` that replaced the hashed body, a
 * `fetchOptions` or `middleware` that rewrote the request on its way out, a
 * `path` or `serverURL` that sent it to another host, an `Authorization` header
 * that moved it onto another key, a Gemini executable tool that made three
 * requests under one token. The authorization was real, the proof was signed,
 * and it described a request that did not go out.
 *
 * So every test drives a REAL provider client, over a stub transport that
 * records what would have left the process. An assertion on `wire.sent` is an
 * assertion about the network, not about what the SDK believes it did; an
 * assertion on `cloud.calls` says whether ID Side was ever asked.
 *
 * Mirrors packages/python/tests/test_call_time_escapes.py.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import OpenAI from "openai";
import Anthropic from "@anthropic-ai/sdk";
import { GoogleGenAI, mcpToTool } from "@google/genai";
import { Mistral, HTTPClient } from "@mistralai/mistralai";
import { wrap } from "../src/consent.ts";
import {
  CredentialRoutingError,
  MissingCredentialError,
  ToolUseRefusedError,
  UnenforceableOptionError,
  UnenforcedCallError,
  UnsupportedProviderError,
} from "../src/index.ts";
import type { AuthorizeRequest, ConsentClient, ConsentDecision } from "../src/transport.ts";

const USER = "usr_marie_456";
const ATTACKER_KEY = "sk-ATTACKER-never-in-a-message"; // pragma: allowlist secret
const MESSAGES = [{ role: "user" as const, content: "a private sentence" }];

// ── the stand-ins: a cloud that records, a wire that records ──────────────────

interface Cloud extends ConsentClient {
  readonly calls: AuthorizeRequest[];
}

function cloud(constraints: Record<string, unknown> = {}, token = "act.jwt.token"): Cloud {
  const calls: AuthorizeRequest[] = [];
  return {
    calls,
    async authorize(request: AuthorizeRequest): Promise<ConsentDecision> {
      calls.push(request);
      return {
        consentState: {},
        paramsForced: {},
        actToken: token,
        source: "widget",
        legalBasis: "consent",
        binding: "user_id",
        constraints,
      };
    },
  };
}

interface Sent {
  readonly url: string;
  readonly method: string;
  readonly headers: Headers;
  readonly body: string;
  readonly redirect: string | undefined;
}

interface Wire {
  readonly sent: Sent[];
  readonly fetch: (input: unknown, init?: RequestInit) => Promise<Response>;
}

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

/** Records every request that reaches it, in either shape `fetch` accepts. */
function wire(respond: (sent: Sent) => Response): Wire {
  const sent: Sent[] = [];
  return {
    sent,
    async fetch(input: unknown, init?: RequestInit): Promise<Response> {
      let request: Sent;
      if (input instanceof Request) {
        request = {
          url: input.url,
          method: input.method,
          headers: new Headers(input.headers),
          body: await input.clone().text(),
          redirect: init?.redirect ?? input.redirect,
        };
      } else {
        request = {
          url: String(input),
          method: (init?.method ?? "GET").toUpperCase(),
          headers: new Headers(init?.headers),
          body: typeof init?.body === "string" ? init.body : "",
          redirect: init?.redirect,
        };
      }
      sent.push(request);
      return respond(request);
    },
  };
}

const CHAT_COMPLETION = {
  id: "chatcmpl_1",
  object: "chat.completion",
  created: 0,
  model: "gpt-4o",
  choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
};

const RESPONSE = {
  id: "resp_1",
  object: "response",
  created_at: 0,
  model: "gpt-4o",
  status: "completed",
  output: [],
};

const MESSAGE = {
  id: "msg_1",
  type: "message",
  role: "assistant",
  model: "claude-sonnet-4-5",
  content: [{ type: "text", text: "ok" }],
  stop_reason: "end_turn",
  usage: { input_tokens: 1, output_tokens: 1 },
};

const GEMINI_RESPONSE = {
  candidates: [{ content: { role: "model", parts: [{ text: "ok" }] }, finishReason: "STOP", index: 0 }],
};

function openaiWire(): Wire {
  return wire((s) => json(new URL(s.url).pathname.endsWith("/responses") ? RESPONSE : CHAT_COMPLETION));
}

function openai(w: Wire, options: Record<string, unknown> = {}): OpenAI {
  // pragma: allowlist secret
  return new OpenAI({ apiKey: "sk-test", fetch: w.fetch as typeof fetch, maxRetries: 0, ...options });
}

function deepseek(w: Wire): OpenAI {
  return openai(w, { baseURL: "https://api.deepseek.com" });
}

function anthropic(w: Wire, options: Record<string, unknown> = {}): Anthropic {
  // pragma: allowlist secret
  return new Anthropic({ apiKey: "sk-ant-test", fetch: w.fetch as typeof fetch, maxRetries: 0, ...options });
}

function mistral(w: Wire): Mistral {
  // pragma: allowlist secret
  return new Mistral({ apiKey: "sk-mistral-test", httpClient: new HTTPClient({ fetcher: w.fetch }) });
}

/** @google/genai has no constructor-level fetch: only the global one is replaced, and always restored. */
async function withGlobalFetch<T>(w: Wire, run: () => Promise<T>): Promise<T> {
  const original = globalThis.fetch;
  globalThis.fetch = w.fetch as typeof fetch;
  try {
    return await run();
  } finally {
    globalThis.fetch = original;
  }
}

function gemini(): GoogleGenAI {
  // pragma: allowlist secret
  return new GoogleGenAI({ apiKey: "gemini-test-key", httpOptions: { baseUrl: "https://gemini.invalid" } });
}

/** Whatever the call ended with: its value, or the error it threw or rejected with. */
async function outcome(call: () => unknown): Promise<unknown> {
  try {
    return await call();
  } catch (error: unknown) {
    return error;
  }
}

/** The error itself, or whatever a provider library wrapped it in. */
function inChain<T>(error: unknown, type: abstract new (...a: never[]) => T): T | undefined {
  let current = error;
  for (let depth = 0; current !== undefined && current !== null && depth < 8; depth++) {
    if (current instanceof type) return current;
    current = (current as { cause?: unknown }).cause;
  }
  return undefined;
}

type Call = (options: unknown) => Promise<unknown>;

interface Endpoint {
  readonly wire: Wire;
  readonly cloud: Cloud;
  readonly call: Call;
}

/** Every `(body, options)` endpoint whose options object this file probes. */
const STAINLESS_ENDPOINTS: Record<string, (constraints?: Record<string, unknown>) => Endpoint> = {
  "openai chat.completions.create": (constraints) => {
    const w = openaiWire();
    const cc = cloud(constraints);
    const client = wrap(openai(w), { consentClient: cc });
    const call = (options: unknown) =>
      client.chat.completions.create({ model: "gpt-4o", messages: MESSAGES, user_id: USER }, options as never);
    return { wire: w, cloud: cc, call };
  },
  "openai responses.create": (constraints) => {
    const w = openaiWire();
    const cc = cloud(constraints);
    const client = wrap(openai(w), { consentClient: cc });
    const call = (options: unknown) =>
      client.responses.create({ model: "gpt-4o", input: "a private sentence", user_id: USER }, options as never);
    return { wire: w, cloud: cc, call };
  },
  "deepseek chat.completions.create": (constraints) => {
    const w = openaiWire();
    const cc = cloud(constraints);
    const client = wrap(deepseek(w), { consentClient: cc });
    const call = (options: unknown) =>
      client.chat.completions.create({ model: "deepseek-chat", messages: MESSAGES, user_id: USER }, options as never);
    return { wire: w, cloud: cc, call };
  },
  "anthropic messages.create": (constraints) => {
    const w = wire(() => json(MESSAGE));
    const cc = cloud(constraints);
    const client = wrap(anthropic(w), { consentClient: cc });
    const call = (options: unknown) =>
      client.messages.create(
        { model: "claude-sonnet-4-5", max_tokens: 16, messages: MESSAGES, user_id: USER },
        options as never,
      );
    return { wire: w, cloud: cc, call };
  },
};

/** The call was refused locally: ID Side never asked, and nothing on the wire. */
async function assertRefusedBeforeAuthorize(endpoint: Endpoint, options: unknown, label: string): Promise<UnenforceableOptionError> {
  const error = await outcome(() => endpoint.call(options));
  assert.ok(
    error instanceof UnenforceableOptionError,
    `${label}: expected UnenforceableOptionError, got ${String(error)}`,
  );
  assert.equal(endpoint.cloud.calls.length, 0, `${label}: the cloud was asked to authorize a call that cannot be enforced`);
  assert.equal(endpoint.wire.sent.length, 0, `${label}: a request left the process`);
  return error;
}

// ── B. the options object is a closed list ───────────────────────────────────

test("options.body cannot replace the body that was hashed and authorized", async () => {
  // The first vector found: store:true back on, tools re-injected, the model
  // swapped — on a request whose proof says store:false.
  for (const [name, make] of Object.entries(STAINLESS_ENDPOINTS)) {
    const endpoint = make();
    await assertRefusedBeforeAuthorize(
      endpoint,
      { body: { model: "gpt-4o-unapproved", store: true, tools: [{ type: "function" }] } },
      name,
    );
  }
});

test("fetchOptions is refused, even empty: its whole purpose is to reach the request", async () => {
  for (const [name, make] of Object.entries(STAINLESS_ENDPOINTS)) {
    await assertRefusedBeforeAuthorize(make(), { fetchOptions: { body: "{\"store\":true}" } }, `${name} fetchOptions.body`);
    await assertRefusedBeforeAuthorize(make(), { fetchOptions: {} }, `${name} empty fetchOptions`);
  }
});

test("anthropic per-request middleware and fallback state are refused", async () => {
  const rewrite = async (request: unknown, next: (r: unknown) => Promise<Response>) => next(request);
  const make = STAINLESS_ENDPOINTS["anthropic messages.create"]!;
  await assertRefusedBeforeAuthorize(make(), { middleware: [rewrite] }, "middleware");
  await assertRefusedBeforeAuthorize(make(), { fallbackState: { index: 0 } }, "fallbackState");
});

test("options that choose where the request goes, or how it is built, are refused", async () => {
  const vectors: Record<string, unknown> = {
    path: "https://evil.example/v1/chat/completions",
    defaultBaseURL: "https://evil.example/v1",
    query: { exfiltrate: "yes" },
    method: "put",
    stream: true,
    __security: {},
    __binaryResponse: true,
    __streamClass: class {},
    __metadata: { hasStreamingBody: true },
    __synthesizeEventData: true,
  };
  for (const [name, make] of Object.entries(STAINLESS_ENDPOINTS)) {
    for (const [key, value] of Object.entries(vectors)) {
      const error = await assertRefusedBeforeAuthorize(make(), { [key]: value }, `${name} ${key}`);
      assert.match(error.message, new RegExp(`\`${key.replace(/[$_]/g, "\\$&")}\``), "the message names the option");
    }
  }
});

test("an option this SDK has never heard of is refused, not waved through", async () => {
  // A closed list, not a list of known dangers: an option a future library
  // version adds is refused until someone has read what it does.
  for (const [name, make] of Object.entries(STAINLESS_ENDPOINTS)) {
    await assertRefusedBeforeAuthorize(make(), { rerouteTo: "https://evil.example" }, name);
  }
});

// ── C. headers that choose the credential ────────────────────────────────────

test("a per-call header naming the credential is refused, in every spelling and shape", async () => {
  const reserved: Record<string, string[]> = {
    "openai chat.completions.create": ["Authorization", "OpenAI-Project", "openai-organization", "api-key", "X-AI-Consent-Token"],
    "openai responses.create": ["authorization", "OPENAI-PROJECT", "x-ai-consent-token"],
    "deepseek chat.completions.create": ["Authorization", "OpenAI-Organization"],
    "anthropic messages.create": ["x-api-key", "Authorization", "api-key", "X-AI-Consent-Token"],
  };
  for (const [name, headers] of Object.entries(reserved)) {
    const make = STAINLESS_ENDPOINTS[name]!;
    for (const header of headers) {
      const shapes: Record<string, unknown> = {
        object: { [header]: `Bearer ${ATTACKER_KEY}` },
        Headers: new Headers({ [header]: `Bearer ${ATTACKER_KEY}` }),
        tuples: [["X-Request-Id", "req_42"], [header, `Bearer ${ATTACKER_KEY}`]],
        // Removing Authorization changes the key as surely as replacing it.
        "null value": { [header]: null },
      };
      for (const [shape, value] of Object.entries(shapes)) {
        const error = await assertRefusedBeforeAuthorize(make(), { headers: value }, `${name} ${header} (${shape})`);
        assert.ok(error.message.toLowerCase().includes(header.toLowerCase()), "the message names the header");
        assert.ok(!error.message.includes(ATTACKER_KEY), "the message never repeats a header value");
      }
    }
  }
});

test("a headers container the SDK cannot read is refused rather than trusted", async () => {
  class HeaderBag {
    authorization = `Bearer ${ATTACKER_KEY}`;
  }
  const make = STAINLESS_ENDPOINTS["openai chat.completions.create"]!;
  await assertRefusedBeforeAuthorize(make(), { headers: new HeaderBag() }, "class instance");
  await assertRefusedBeforeAuthorize(make(), { headers: ["Authorization"] }, "tuple list with a bare name");
});

// ── the options an enforced call keeps ───────────────────────────────────────

test("timeouts, retries, cancellation, idempotency keys and tracing headers still pass", async () => {
  for (const [name, make] of Object.entries(STAINLESS_ENDPOINTS)) {
    const endpoint = make();
    await endpoint.call({
      timeout: 30_000,
      maxRetries: 0,
      signal: new AbortController().signal,
      idempotencyKey: "idem_42",
      headers: { "X-Request-Id": "req_42" },
    });
    assert.equal(endpoint.cloud.calls.length, 1, name);
    assert.equal(endpoint.wire.sent.length, 1, name);
    assert.equal(endpoint.wire.sent[0]!.headers.get("X-AI-Consent-Token"), "act.jwt.token", name);
    assert.equal(endpoint.wire.sent[0]!.headers.get("X-Request-Id"), "req_42", name);
  }
});

test("anthropic-beta, and headers in Headers or tuple form, still pass", async () => {
  const make = STAINLESS_ENDPOINTS["anthropic messages.create"]!;
  for (const headers of [
    { "anthropic-beta": "prompt-caching-2024-07-31" },
    new Headers({ "anthropic-beta": "prompt-caching-2024-07-31" }),
    [["anthropic-beta", "prompt-caching-2024-07-31"]],
    null,
  ]) {
    const endpoint = make();
    await endpoint.call({ headers });
    assert.equal(endpoint.wire.sent.length, 1);
    if (headers !== null) {
      assert.equal(endpoint.wire.sent[0]!.headers.get("anthropic-beta"), "prompt-caching-2024-07-31");
    }
  }
});

test("the streaming and parsing helpers still go through, with the options they inject", async () => {
  // Both inject `signal` and an `X-Stainless-Helper-Method` header into the
  // options they hand the patched create(): the closed list must allow them.
  const w = openaiWire();
  const cc = cloud();
  const client = wrap(openai(w), { consentClient: cc });
  await client.chat.completions.parse({ model: "gpt-4o", messages: MESSAGES, user_id: USER } as never, {
    headers: { "X-Request-Id": "req_42" },
  });
  assert.equal(cc.calls.length, 1);
  assert.equal(w.sent[0]!.headers.get("X-Stainless-Helper-Method"), "chat.completions.parse");

  const aw = wire(
    () =>
      new Response(
        [
          `event: message_start\ndata: ${JSON.stringify({ type: "message_start", message: { ...MESSAGE, content: [] } })}\n\n`,
          `event: message_stop\ndata: ${JSON.stringify({ type: "message_stop" })}\n\n`,
        ].join(""),
        { status: 200, headers: { "content-type": "text/event-stream" } },
      ),
  );
  const acc = cloud();
  const claude = wrap(anthropic(aw), { consentClient: acc });
  const stream = claude.messages.stream(
    { model: "claude-sonnet-4-5", max_tokens: 16, messages: MESSAGES, user_id: USER },
    { signal: new AbortController().signal, headers: { "X-Request-Id": "req_42" } },
  );
  await outcome(() => stream.done());
  assert.equal(acc.calls.length, 1, "messages.stream() must still authorize exactly once");
  assert.equal(aw.sent.length, 1);
  assert.equal(aw.sent[0]!.headers.get("X-Stainless-Helper-Method"), "stream");
});

test("a helper that forwards its options to create() cannot smuggle a refused one through", async () => {
  // `chat.completions.stream()` is not patched in Node: it spreads its options
  // into the patched create(), which is where the closed list applies.
  const w = openaiWire();
  const cc = cloud();
  const client = wrap(openai(w), { consentClient: cc });
  const error = await outcome(async () => {
    const stream = client.chat.completions.stream(
      { model: "gpt-4o", messages: MESSAGES, user_id: USER },
      { fetchOptions: { body: "{}" } } as never,
    );
    for await (const _chunk of stream) void _chunk;
  });
  assert.ok(inChain(error, UnenforceableOptionError), `got ${String(error)}`);
  assert.equal(cc.calls.length, 0);
  assert.equal(w.sent.length, 0);
});

// ── Mistral: RequestInit is flattened into its options ───────────────────────

function mistralEndpoint(constraints?: Record<string, unknown>): Endpoint & { client: ReturnType<typeof wrap<Mistral>> } {
  const w = wire(() =>
    json({
      id: "cmpl_1",
      object: "chat.completion",
      model: "mistral-small-latest",
      created: 0,
      usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
    }),
  );
  const cc = cloud(constraints);
  const client = wrap(mistral(w), { consentClient: cc });
  const call = (options: unknown) =>
    client.chat.complete({ model: "mistral-small-latest", messages: MESSAGES, user_id: USER }, options as never);
  return { wire: w, cloud: cc, call, client };
}

test("mistral: serverURL, fetchOptions, dispatcher and every RequestInit key are refused", async () => {
  const vectors: Record<string, unknown> = {
    serverURL: "https://evil.example",
    fetchOptions: {},
    dispatcher: {},
    keepalive: true,
    redirect: "follow",
    credentials: "include",
    mode: "no-cors",
    referrer: "https://evil.example",
    referrerPolicy: "unsafe-url",
    integrity: "sha256-x",
    window: null,
    duplex: "half",
    cache: "no-store",
    priority: "high",
  };
  for (const [key, value] of Object.entries(vectors)) {
    await assertRefusedBeforeAuthorize(mistralEndpoint(), { [key]: value }, `mistral ${key}`);
  }
  for (const header of ["Authorization", "authorization", "X-AI-Consent-Token"]) {
    await assertRefusedBeforeAuthorize(mistralEndpoint(), { headers: { [header]: `Bearer ${ATTACKER_KEY}` } }, `mistral ${header}`);
  }
});

test("mistral: retries, retry codes, timeouts, cancellation and tracing headers still pass", async () => {
  const endpoint = mistralEndpoint();
  await endpoint.call({
    retries: { strategy: "none" },
    retryCodes: ["503"],
    timeoutMs: 30_000,
    signal: new AbortController().signal,
    headers: { "X-Request-Id": "req_42" },
  });
  assert.equal(endpoint.cloud.calls.length, 1);
  assert.equal(endpoint.wire.sent.length, 1);
  assert.equal(endpoint.wire.sent[0]!.headers.get("X-AI-Consent-Token"), "act.jwt.token");
  assert.equal(endpoint.wire.sent[0]!.headers.get("X-Request-Id"), "req_42");
});

// ── Gemini: the options live inside the body, under config.httpOptions ─────

async function geminiCall(
  config: Record<string, unknown>,
  constraints?: Record<string, unknown>,
): Promise<{ error: unknown; cloud: Cloud; wire: Wire }> {
  const w = wire(() => json(GEMINI_RESPONSE));
  const cc = cloud(constraints);
  const error = await withGlobalFetch(w, async () => {
    const client = wrap(gemini(), { consentClient: cc });
    return outcome(() =>
      client.models.generateContent({ model: "gemini-2.0-flash", contents: "a private sentence", config, user_id: USER }),
    );
  });
  return { error, cloud: cc, wire: w };
}

test("gemini: httpOptions that rewrite the body or move the request are refused", async () => {
  const vectors: Record<string, unknown> = {
    extraBody: { generationConfig: { temperature: 2 } },
    baseUrl: "https://evil.example",
    apiVersion: "v1alpha",
    baseUrlResourceScope: "COLLECTION",
    somethingNew: true,
  };
  for (const [key, value] of Object.entries(vectors)) {
    const { error, cloud: cc, wire: w } = await geminiCall({ httpOptions: { [key]: value } });
    assert.ok(error instanceof UnenforceableOptionError, `${key}: got ${String(error)}`);
    assert.match(error.message, new RegExp(`httpOptions\\.${key}`));
    assert.equal(cc.calls.length, 0, key);
    assert.equal(w.sent.length, 0, key);
  }
  for (const header of ["x-goog-api-key", "Authorization", "X-Goog-User-Project", "x-ai-consent-token"]) {
    const { error, cloud: cc, wire: w } = await geminiCall({ httpOptions: { headers: { [header]: ATTACKER_KEY } } });
    assert.ok(error instanceof UnenforceableOptionError, `${header}: got ${String(error)}`);
    assert.ok(!error.message.includes(ATTACKER_KEY));
    assert.equal(cc.calls.length, 0, header);
    assert.equal(w.sent.length, 0, header);
  }
});

test("gemini: timeouts, retry options, tracing headers and an abort signal still pass", async () => {
  const { error, cloud: cc, wire: w } = await geminiCall({
    abortSignal: new AbortController().signal,
    httpOptions: { timeout: 30_000, retryOptions: { attempts: 1 }, headers: { "X-Trace-Id": "trace_42" } },
  });
  assert.equal(error instanceof Error ? error.message : undefined, undefined, String(error));
  assert.equal(cc.calls.length, 1);
  assert.equal(w.sent.length, 1);
  assert.equal(w.sent[0]!.headers.get("X-AI-Consent-Token"), "act.jwt.token");
  assert.equal(w.sent[0]!.headers.get("X-Trace-Id"), "trace_42");
});

test("gemini: an abort signal is not part of the request, so it is not part of its hash", async () => {
  // It used to make the hash throw ConsentTokenError, so no existing hash moves.
  const withSignal = await geminiCall({ temperature: 0.2, abortSignal: new AbortController().signal });
  const without = await geminiCall({ temperature: 0.2 });
  assert.equal(withSignal.cloud.calls[0]!.requestHash, without.cloud.calls[0]!.requestHash);
});

// ── E. Gemini automatic function calling ─────────────────────────────────────

test("gemini: an executable tool is refused before the library can loop on one token", async () => {
  // Proved before the fix: one authorize, three requests, the same ACT token on
  // each, three different bodies. A CallableTool with toJSON() even hashed.
  const callable = {
    async tool() {
      return { functionDeclarations: [{ name: "lookup_patient_record" }] };
    },
    async callTool() {
      return [{ functionResponse: { name: "lookup_patient_record", response: { ok: true } } }];
    },
    toJSON() {
      return { functionDeclarations: [{ name: "lookup_patient_record" }] };
    },
  };
  const mcp = mcpToTool({ listTools: async () => ({ tools: [] }), callTool: async () => ({ content: [] }) } as never);
  for (const [label, tool] of Object.entries({ callable, mcpToTool: mcp })) {
    // Refused whatever the user answered about tools: this is about the token, not the tools.
    const { error, cloud: cc, wire: w } = await geminiCall({ tools: [{ googleSearch: {} }, tool] });
    assert.ok(error instanceof UnenforceableOptionError, `${label}: got ${String(error)}`);
    assert.match(error.message, /config\.tools\[1\]/);
    assert.equal(cc.calls.length, 0, label);
    assert.equal(w.sent.length, 0, label);
  }
});

// ── D. every field that can hand the model a tool ────────────────────────────

const REFUSE = { tools: "refuse" };
const STRIP = { tools: "strip" };

async function refusedAfterAuthorize(endpoint: Endpoint, run: () => Promise<unknown>, field: string): Promise<void> {
  const error = await outcome(run);
  assert.ok(error instanceof ToolUseRefusedError, `${field}: expected ToolUseRefusedError, got ${String(error)}`);
  assert.match(error.message, new RegExp(`\`${field}\``), "the message names the field");
  assert.equal(endpoint.cloud.calls.length, 1, `${field}: the refusal is applied to the authorized decision`);
  assert.equal(endpoint.wire.sent.length, 0, `${field}: a request left the process`);
}

/** A wrapped OpenAI client whose cloud answers with `constraints`. */
function openaiUnder(constraints: Record<string, unknown>): Endpoint & { client: ReturnType<typeof wrap<OpenAI>> } {
  const w = openaiWire();
  const cc = cloud(constraints);
  return { wire: w, cloud: cc, call: async () => undefined, client: wrap(openai(w), { consentClient: cc }) };
}

test("openai chat: functions and web_search_options carry tools past a `tools` refusal no more", async () => {
  for (const decision of [REFUSE, STRIP]) {
    // Hosted search needs no `tools` key at all: an empty object switches it on.
    const e = openaiUnder(decision);
    await refusedAfterAuthorize(
      e,
      () => e.client.chat.completions.create({ model: "gpt-4o", messages: MESSAGES, web_search_options: {}, user_id: USER }),
      "web_search_options",
    );
  }

  const e = openaiUnder(REFUSE);
  await refusedAfterAuthorize(
    e,
    () =>
      e.client.chat.completions.create({
        model: "gpt-4o",
        messages: MESSAGES,
        functions: [{ name: "lookup_patient_record" }],
        user_id: USER,
      }),
    "functions",
  );
});

test("openai chat under strip: every tool definition goes, not only `tools`", async () => {
  const w = openaiWire();
  const cc = cloud(STRIP);
  const wrapped = wrap(openai(w), { consentClient: cc });
  await wrapped.chat.completions.create({
    model: "gpt-4o",
    messages: MESSAGES,
    tools: [{ type: "function", function: { name: "lookup_patient_record" } }],
    functions: [{ name: "lookup_patient_record" }],
    user_id: USER,
  });
  const body = JSON.parse(w.sent[0]!.body) as Record<string, unknown>;
  assert.ok(!("tools" in body));
  assert.ok(!("functions" in body));
});

test("openai responses: stored conversations, prompts and tool-bearing input items are refused", async () => {
  const vectors: Array<[string, Record<string, unknown>]> = [
    ["conversation", { conversation: "conv_medhelp_1" }],
    ["previous_response_id", { previous_response_id: "resp_medhelp_1" }],
    ["prompt", { prompt: { id: "pmpt_medhelp_1" } }],
    ["input", { input: [{ type: "message", role: "user", content: "hi" }, { type: "additional_tools", tools: [] }] }],
    // `type` is optional on an item reference: the id alone points at the stored item.
    ["input", { input: [{ id: "fc_medhelp_1" }] }],
    ["input", { input: [{ id: "fc_medhelp_1", type: null }] }],
  ];
  for (const decision of [REFUSE, STRIP]) {
    for (const [field, extra] of vectors) {
      const w = openaiWire();
      const cc = cloud(decision);
      const wrapped = wrap(openai(w), { consentClient: cc });
      await refusedAfterAuthorize(
        { wire: w, cloud: cc, call: async () => undefined },
        () => wrapped.responses.create({ model: "gpt-4o", input: "hi", ...extra, user_id: USER } as never),
        field,
      );
    }
  }
});

test("openai responses: input that is text, or items of other types, is not a tool", async () => {
  for (const input of [
    "a private sentence",
    [{ type: "message", role: "user", content: "hi" }],
    // A message may omit its type too; naming no stored item, it references none.
    [{ role: "user", content: "hi" }],
    ["not an item"],
  ]) {
    const w = openaiWire();
    const wrapped = wrap(openai(w), { consentClient: cloud(REFUSE) });
    await wrapped.responses.create({ model: "gpt-4o", input, user_id: USER } as never);
    assert.equal(w.sent.length, 1, JSON.stringify(input));
  }
});

test("an empty tool list is no tool at all", async () => {
  // Parity with Python, and what the user refused: tools, not an empty list.
  const w = openaiWire();
  const cc = cloud(REFUSE);
  const wrapped = wrap(openai(w), { consentClient: cc });
  await wrapped.chat.completions.create({ model: "gpt-4o", messages: MESSAGES, tools: [], functions: [], user_id: USER });
  assert.equal(w.sent.length, 1);
});

test("anthropic: MCP servers are tools, refused or stripped like them", async () => {
  const servers = [{ type: "url", url: "https://mcp.example", name: "records" }];
  const w = wire(() => json(MESSAGE));
  const cc = cloud(REFUSE);
  const wrapped = wrap(anthropic(w), { consentClient: cc });
  await refusedAfterAuthorize(
    { wire: w, cloud: cc, call: async () => undefined },
    () =>
      wrapped.messages.create({
        model: "claude-sonnet-4-5",
        max_tokens: 16,
        messages: MESSAGES,
        mcp_servers: servers,
        user_id: USER,
      } as never),
    "mcp_servers",
  );

  const sw = wire(() => json(MESSAGE));
  const stripped = wrap(anthropic(sw), { consentClient: cloud(STRIP) });
  await stripped.messages.create({
    model: "claude-sonnet-4-5",
    max_tokens: 16,
    messages: MESSAGES,
    mcp_servers: servers,
    user_id: USER,
  } as never);
  assert.ok(!("mcp_servers" in (JSON.parse(sw.sent[0]!.body) as object)));
});

test("mistral: an agent may carry tools the SDK cannot see, so it is refused under both", async () => {
  for (const decision of [REFUSE, STRIP]) {
    const agents = mistralEndpoint(decision);
    await refusedAfterAuthorize(
      agents,
      () => agents.client.agents.complete({ agentId: "ag_medhelp", messages: MESSAGES, user_id: USER }),
      "agentId",
    );
    const conversations = mistralEndpoint(decision);
    await refusedAfterAuthorize(
      conversations,
      () => conversations.client.beta.conversations.start({ agentId: "ag_medhelp", inputs: "hi", user_id: USER } as never),
      "agentId",
    );
  }
});

test("gemini: a cached content may carry tools the SDK cannot see, so it is refused", async () => {
  for (const decision of [REFUSE, STRIP]) {
    const { error, cloud: cc, wire: w } = await geminiCall({ cachedContent: "cachedContents/medhelp" }, decision);
    assert.ok(error instanceof ToolUseRefusedError, String(error));
    assert.match(error.message, /`config\.cachedContent`/);
    assert.equal(cc.calls.length, 1);
    assert.equal(w.sent.length, 0);
  }
});

// ── I. a client the net cannot cover is not wrapped ──────────────────────────

test("a Mistral client built without an HTTPClient is refused, and says how to build one", () => {
  // Its transport sits in an ES private field, out of any JavaScript's reach.
  // `wrap()` used to accept it with no net at all, and log `net=false`.
  const raw = new Mistral({ apiKey: "sk-mistral-test" }); // pragma: allowlist secret
  const chat = raw.chat;
  const before = chat.complete;
  const cc = cloud();

  assert.throws(
    () => wrap(raw, { consentClient: cc }),
    (error: unknown) =>
      error instanceof UnsupportedProviderError && /new HTTPClient\(\)/.test(error.message) && /httpClient/.test(error.message),
  );
  assert.equal(chat.complete, before, "a refused client keeps its methods untouched");
  assert.equal(cc.calls.length, 0);
});

test("any client built from a class, with no transport the net can find, is refused", () => {
  class HouseClient {
    chat = { completions: { create: async (_body: Record<string, unknown>) => ({ id: "chatcmpl_1" }) } };
  }
  assert.throws(() => wrap(new HouseClient(), { consentClient: cloud(), provider: "openai" }), UnsupportedProviderError);
});

test("a plain object stand-in has no transport to leak through, and is still wrapped", async () => {
  // Every provider library builds its client from a class. An object literal is
  // a test double, the shape the parity script and this suite rely on.
  const sent: unknown[] = [];
  const client = wrap(
    { chat: { completions: { create: async (body: Record<string, unknown>) => (sent.push(body), { id: "chatcmpl_1" }) } } },
    { consentClient: cloud(), provider: "openai" },
  );
  await client.chat.completions.create({ model: "gpt-4o", messages: MESSAGES, user_id: USER });
  assert.equal(sent.length, 1);

  const bare = Object.assign(Object.create(null) as object, {
    chat: { completions: { create: async (_body: Record<string, unknown>) => ({ id: "chatcmpl_1" }) } },
  });
  assert.doesNotThrow(() => wrap(bare, { consentClient: cloud(), provider: "openai" }));
});

// ── F. every request goes to the server the client was wrapped with ─────────

async function refusedAtTransport(run: () => Promise<unknown>, w: Wire, label: string): Promise<void> {
  const before = w.sent.length;
  const error = await outcome(run);
  assert.ok(inChain(error, UnenforcedCallError), `${label}: expected UnenforcedCallError, got ${String(error)}`);
  assert.equal(w.sent.length, before, `${label}: a request left for another server`);
}

test("openai and anthropic: a base URL changed after wrap() is refused at the transport", async () => {
  const w = openaiWire();
  const raw = openai(w);
  const client = wrap(raw, { consentClient: cloud() });
  raw.baseURL = "https://attacker.example/v1";
  await refusedAtTransport(
    () => client.chat.completions.create({ model: "gpt-4o", messages: MESSAGES, user_id: USER }),
    w,
    "openai",
  );

  const aw = wire(() => json(MESSAGE));
  const claude = anthropic(aw);
  const wrapped = wrap(claude, { consentClient: cloud() });
  claude.baseURL = "https://attacker.example";
  await refusedAtTransport(
    () => wrapped.messages.create({ model: "claude-sonnet-4-5", max_tokens: 16, messages: MESSAGES, user_id: USER }),
    aw,
    "anthropic",
  );
});

test("mistral and gemini: a server moved after wrap() is refused at the transport", async () => {
  // Speakeasy resources are SDK objects of their own, each holding a copy of the
  // server URL: the one a chat call is built from is the resource's.
  const endpoint = mistralEndpoint();
  (endpoint.client.chat as unknown as { _baseURL: URL })._baseURL = new URL("https://attacker.example/");
  await refusedAtTransport(() => endpoint.call(undefined), endpoint.wire, "mistral");

  const w = wire(() => json(GEMINI_RESPONSE));
  await withGlobalFetch(w, async () => {
    const raw = gemini();
    const client = wrap(raw, { consentClient: cloud() });
    (raw as unknown as { apiClient: { setBaseUrl(url: string): void } }).apiClient.setBaseUrl("https://attacker.example");
    await refusedAtTransport(
      () => client.models.generateContent({ model: "gemini-2.0-flash", contents: "hi", user_id: USER }),
      w,
      "gemini",
    );
  });
});

test("the origin is scheme, host and effective port: the same server written differently still passes", async () => {
  const w = openaiWire();
  const raw = openai(w, { baseURL: "HTTPS://API.OpenAI.com:443/v1" });
  const client = wrap(raw, { consentClient: cloud() });
  await client.chat.completions.create({ model: "gpt-4o", messages: MESSAGES, user_id: USER });
  assert.equal(w.sent.length, 1);

  raw.baseURL = "http://api.openai.com/v1";
  await refusedAtTransport(
    () => client.chat.completions.create({ model: "gpt-4o", messages: MESSAGES, user_id: USER }),
    w,
    "another scheme is another server",
  );
});

test("anthropic: a base URL its credential chain is still resolving is bound at the first send", async () => {
  // Without a key, `new Anthropic()` resolves a profile asynchronously, and the
  // profile may name the host (client.js, `_resolveDefaultCredentials`). Binding
  // at wrap() would bind the default host and refuse every call; binding from
  // the request URL would bind whatever the request says. So it is read from the
  // client, when the first call goes out. This reproduces that pending state.
  const w = wire(() => json(MESSAGE));
  const raw = anthropic(w);
  const internals = raw as unknown as {
    _baseURLIsExplicit: boolean;
    _authState: { resolution: Promise<void> | null; baseURL?: string };
  };
  let settle!: () => void;
  internals._baseURLIsExplicit = false;
  internals._authState.resolution = new Promise<void>((resolve) => {
    settle = () => {
      internals._authState.baseURL = "https://profile.example";
      internals._authState.resolution = null;
      resolve();
    };
  });
  const client = wrap(raw, { consentClient: cloud() });

  const pending = client.messages.create({ model: "claude-sonnet-4-5", max_tokens: 16, messages: MESSAGES, user_id: USER });
  settle();
  await pending;
  assert.equal(new URL(w.sent[0]!.url).origin, "https://profile.example");

  internals._baseURLIsExplicit = true;
  raw.baseURL = "https://attacker.example";
  await refusedAtTransport(
    () => client.messages.create({ model: "claude-sonnet-4-5", max_tokens: 16, messages: MESSAGES, user_id: USER }),
    w,
    "bound from the first send on",
  );
});

test("anthropic: a profile host already resolved but not yet copied onto baseURL is the one bound", async () => {
  // The library copies it when it builds the next request, so at wrap() time
  // `baseURL` still names the default host while the auth state names the real one.
  const w = wire(() => json(MESSAGE));
  const raw = anthropic(w);
  const internals = raw as unknown as { _baseURLIsExplicit: boolean; _authState: { baseURL?: string } };
  internals._baseURLIsExplicit = false;
  internals._authState.baseURL = "https://profile.example";
  const client = wrap(raw, { consentClient: cloud() });

  await client.messages.create({ model: "claude-sonnet-4-5", max_tokens: 16, messages: MESSAGES, user_id: USER });
  assert.equal(new URL(w.sent[0]!.url).origin, "https://profile.example");
});

test("a class-built client whose server cannot be read is refused: nothing could bind its requests", () => {
  class HouseClient {
    fetch = async () => new Response("{}");
    chat = { completions: { create: async (_body: Record<string, unknown>) => ({ id: "chatcmpl_1" }) } };
  }
  const raw = new HouseClient();
  const before = raw.fetch;
  assert.throws(() => wrap(raw, { consentClient: cloud(), provider: "openai" }), UnsupportedProviderError);
  assert.equal(raw.fetch, before, "the transport of a refused client is left as it was");
});

// ── construction options that rewrite every request ─────────────────────────
//
// Declarative client options, not code: detected at wrap(), where a custom
// `fetch` (code) could not be.

test("openai and anthropic: a client-level fetchOptions body or method is refused at wrap()", () => {
  const makers: Record<string, (options: Record<string, unknown>) => object> = {
    openai: (options) => openai(openaiWire(), options),
    anthropic: (options) => anthropic(wire(() => json(MESSAGE)), options),
  };
  for (const [name, make] of Object.entries(makers)) {
    for (const [key, value] of Object.entries({ body: "{\"store\":true}", method: "PUT" })) {
      const raw = make({ fetchOptions: { [key]: value } }) as { fetch: unknown };
      const transport = raw.fetch;
      assert.throws(
        () => wrap(raw, { consentClient: cloud() }),
        (error: unknown) =>
          error instanceof UnsupportedProviderError && error.message.includes(`\`fetchOptions.${key}\``),
        `${name} ${key}`,
      );
      assert.equal(raw.fetch, transport, `${name} ${key}: a refused client keeps its transport untouched`);
    }
  }
});

test("a client-level fetchOptions header naming the credential is refused at wrap()", () => {
  assert.throws(
    () => wrap(openai(openaiWire(), { fetchOptions: { headers: { Authorization: `Bearer ${ATTACKER_KEY}` } } }), { consentClient: cloud() }),
    (error: unknown) =>
      error instanceof CredentialRoutingError && /Authorization/.test(error.message) && !error.message.includes(ATTACKER_KEY),
  );
  assert.throws(
    () => wrap(anthropic(wire(() => json(MESSAGE)), { fetchOptions: { headers: new Headers({ "x-api-key": ATTACKER_KEY }) } }), { consentClient: cloud() }),
    CredentialRoutingError,
  );
  class HeaderBag {
    "x-api-key" = ATTACKER_KEY;
  }
  assert.throws(
    () => wrap(anthropic(wire(() => json(MESSAGE)), { fetchOptions: { headers: new HeaderBag() } }), { consentClient: cloud() }),
    CredentialRoutingError,
    "headers the SDK cannot read are not assumed harmless",
  );
});

test("a client-level fetchOptions.headers of any name is refused at wrap(): it replaces the token", () => {
  // openai and anthropic spread `fetchOptions` over the request they built, so
  // `headers` there is not merged: it replaces them all, ACT token included.
  // Wrapped, every call was authorized, proved and then stopped by the net.
  const makers: Record<string, (options: Record<string, unknown>) => object> = {
    openai: (options) => openai(openaiWire(), options),
    anthropic: (options) => anthropic(wire(() => json(MESSAGE)), options),
  };
  for (const [name, make] of Object.entries(makers)) {
    for (const headers of [{ "X-Request-Id": "req_medhelp_1" }, undefined]) {
      const raw = make({ fetchOptions: { headers } }) as { fetch: unknown };
      const transport = raw.fetch;
      assert.throws(
        () => wrap(raw, { consentClient: cloud() }),
        (error: unknown) =>
          error instanceof UnsupportedProviderError && error.message.includes("`fetchOptions.headers`"),
        `${name} ${JSON.stringify(headers)}`,
      );
      assert.equal(raw.fetch, transport, `${name}: a refused client keeps its transport untouched`);
    }
  }
});

test("client-level transport settings such as a dispatcher are still accepted", async () => {
  // They serve proxies and connection pools, and touch no request content.
  const w = openaiWire();
  const cc = cloud();
  const client = wrap(openai(w, { fetchOptions: { dispatcher: {}, keepalive: true } }), { consentClient: cc });
  await client.chat.completions.create({ model: "gpt-4o", messages: MESSAGES, user_id: USER });
  assert.equal(w.sent.length, 1);
  assert.equal(w.sent[0]!.headers.get("X-AI-Consent-Token"), "act.jwt.token");
});

test("gemini: a client-level httpOptions.extraBody is refused at wrap()", async () => {
  const w = wire(() => json(GEMINI_RESPONSE));
  await withGlobalFetch(w, async () => {
    const raw = new GoogleGenAI({
      apiKey: "gemini-test-key", // pragma: allowlist secret
      httpOptions: { baseUrl: "https://gemini.invalid", extraBody: { generationConfig: { temperature: 2 } } },
    });
    assert.throws(
      () => wrap(raw, { consentClient: cloud() }),
      (error: unknown) => error instanceof UnsupportedProviderError && error.message.includes("`httpOptions.extraBody`"),
    );

    for (const extraBody of [undefined, null, {}]) {
      const clean = new GoogleGenAI({
        apiKey: "gemini-test-key", // pragma: allowlist secret
        httpOptions: { baseUrl: "https://gemini.invalid", extraBody: extraBody as never },
      });
      const client = wrap(clean, { consentClient: cloud() });
      await client.models.generateContent({ model: "gemini-2.0-flash", contents: "hi", user_id: USER });
    }
  });
  assert.equal(w.sent.length, 3, "an empty or absent extraBody adds nothing, and is accepted");
});

test("gemini: a client-level header choosing the key or carrying the token is refused at wrap(), as in Python", async () => {
  // @google/genai merges these with each call's headers by exact spelling and
  // appends both: a lowercase token header doubles the one the SDK sets.
  const w = wire(() => json(GEMINI_RESPONSE));
  await withGlobalFetch(w, async () => {
    for (const name of ["x-goog-api-key", "x-ai-consent-token"]) {
      const raw = new GoogleGenAI({
        apiKey: "gemini-test-key", // pragma: allowlist secret
        httpOptions: { baseUrl: "https://gemini.invalid", headers: { [name]: "forged" } },
      });
      assert.throws(
        () => wrap(raw, { consentClient: cloud() }),
        (error: unknown) =>
          error instanceof CredentialRoutingError &&
          error.message.includes("`httpOptions.headers`") &&
          !error.message.includes("forged"),
        name,
      );
    }
    class HeaderBag {
      "x-goog-api-key" = "forged";
    }
    const unreadable = new GoogleGenAI({
      apiKey: "gemini-test-key", // pragma: allowlist secret
      httpOptions: { baseUrl: "https://gemini.invalid", headers: new HeaderBag() as never },
    });
    assert.throws(() => wrap(unreadable, { consentClient: cloud() }), CredentialRoutingError);

    const clean = new GoogleGenAI({
      apiKey: "gemini-test-key", // pragma: allowlist secret
      httpOptions: { baseUrl: "https://gemini.invalid", headers: { "X-Request-Id": "req_medhelp_1" } },
    });
    const client = wrap(clean, { consentClient: cloud() });
    await client.models.generateContent({ model: "gemini-2.0-flash", contents: "hi", user_id: USER });
  });
  assert.equal(w.sent.length, 1, "a header choosing nothing is accepted");
});

test("gemini: a credential client built with httpOptions.extraBody is refused at wrap() too", async () => {
  const w = wire(() => json(GEMINI_RESPONSE));
  await withGlobalFetch(w, async () => {
    const raw = new GoogleGenAI({ apiKey: "gemini-test-key", httpOptions: { baseUrl: "https://gemini.invalid" } }); // pragma: allowlist secret
    const alternative = new GoogleGenAI({
      apiKey: "gemini-test-routed", // pragma: allowlist secret
      httpOptions: { baseUrl: "https://gemini.invalid", extraBody: { generationConfig: { temperature: 2 } } },
    });
    assert.throws(
      () => wrap(raw, { consentClient: cloud(), credentials: { no_training: alternative } }),
      (error: unknown) => error instanceof UnsupportedProviderError && error.message.includes("`httpOptions.extraBody`"),
    );
  });
  assert.equal(w.sent.length, 0);
});

// ── H. a derived client is checked when it is derived ────────────────────────

test("a derived client pointed at another server is refused before it can send anything", () => {
  const w = openaiWire();
  const client = wrap(openai(w), { consentClient: cloud() });
  assert.throws(() => client.withOptions({ baseURL: "https://attacker.example/v1" }), UnsupportedProviderError);

  const aw = wire(() => json(MESSAGE));
  const claude = wrap(anthropic(aw), { consentClient: cloud() });
  assert.throws(() => claude.withOptions({ baseURL: "https://attacker.example" }), UnsupportedProviderError);
  assert.equal(w.sent.length + aw.sent.length, 0);
});

test("copy() is checked like withOptions()", () => {
  // No current library names it `copy`, older ones did, and the SDK patches both.
  class OpenAI {
    baseURL: string;
    fetch = async () => new Response("{}");
    chat = { completions: { create: async (_body: Record<string, unknown>) => ({}) } };
    constructor(baseURL = "https://api.openai.com/v1") {
      this.baseURL = baseURL;
    }
    copy(options: { baseURL?: string } = {}) {
      return new OpenAI(options.baseURL ?? this.baseURL);
    }
  }
  const client = wrap(new OpenAI(), { consentClient: cloud() });
  assert.doesNotThrow(() => client.copy());
  assert.throws(() => client.copy({ baseURL: "https://attacker.example/v1" }), UnsupportedProviderError);
});

test("a DeepSeek client cannot derive an OpenAI one: the provider is detected again and compared", () => {
  // Before, the derived client kept the parent's adapter and announced
  // provider=deepseek on requests sent to OpenAI.
  const client = wrap(deepseek(openaiWire()), { consentClient: cloud() });
  assert.throws(
    () => client.withOptions({ baseURL: "https://api.openai.com/v1" }),
    (error: unknown) =>
      error instanceof UnsupportedProviderError && /deepseek/.test(error.message) && /openai/.test(error.message),
  );
});

test("a derived client cannot bring its own credential headers", () => {
  const client = wrap(openai(openaiWire()), { consentClient: cloud() });
  for (const header of ["Authorization", "openai-project", "X-AI-Consent-Token"]) {
    assert.throws(
      () => client.withOptions({ defaultHeaders: { [header]: `Bearer ${ATTACKER_KEY}` } }),
      (error: unknown) =>
        error instanceof CredentialRoutingError &&
        error.message.toLowerCase().includes(header.toLowerCase()) &&
        !error.message.includes(ATTACKER_KEY),
      header,
    );
  }
  const claude = wrap(anthropic(wire(() => json(MESSAGE))), { consentClient: cloud() });
  assert.throws(() => claude.withOptions({ defaultHeaders: { "X-Api-Key": ATTACKER_KEY } }), CredentialRoutingError);
  assert.throws(
    () => claude.withOptions({ defaultHeaders: new (class Bag {})() as never }),
    CredentialRoutingError,
    "headers the SDK cannot read are not assumed harmless",
  );
});

test("a derived client on the same server is enforced, and bound to the parent's server", async () => {
  const w = openaiWire();
  const cc = cloud();
  const client = wrap(openai(w), { consentClient: cc });
  const derived = client.withOptions({ timeout: 5_000, defaultHeaders: { "X-Request-Source": "batch" } });

  // withOptions() is typed by the provider as returning its own client class.
  const create = derived.chat.completions.create as unknown as (body: Record<string, unknown>) => Promise<unknown>;
  await create({ model: "gpt-4o", messages: MESSAGES, user_id: USER });
  assert.equal(cc.calls.length, 1);
  assert.equal(w.sent[0]!.headers.get("X-AI-Consent-Token"), "act.jwt.token");

  (derived as unknown as { baseURL: string }).baseURL = "https://attacker.example/v1";
  await refusedAtTransport(
    () => create({ model: "gpt-4o", messages: MESSAGES, user_id: USER }),
    w,
    "the derived client inherits the binding",
  );
});

test("a derivation may still change timeouts, retries, logging and webhook secrets", async () => {
  const w = openaiWire();
  const cc = cloud();
  const client = wrap(openai(w), { consentClient: cc });
  const derived = client.withOptions({
    timeout: 5_000,
    maxRetries: 0,
    logger: console,
    logLevel: "warn",
    dangerouslyAllowBrowser: false,
    webhookSecret: "whsec_medhelp_test", // pragma: allowlist secret
    baseURL: "https://api.openai.com/v1",
  });
  const create = derived.chat.completions.create as unknown as (body: Record<string, unknown>) => Promise<unknown>;
  await create({ model: "gpt-4o", messages: MESSAGES, user_id: USER });
  assert.equal(w.sent.length, 1);

  const claude = wrap(anthropic(wire(() => json(MESSAGE))), { consentClient: cloud() });
  assert.doesNotThrow(() =>
    claude.withOptions({
      timeout: 5_000,
      maxRetries: 0,
      logger: console,
      logLevel: "warn",
      dangerouslyAllowBrowser: false,
      webhookKey: "whsec_medhelp_test", // pragma: allowlist secret
    }),
  );
});

test("a derivation cannot change the credential: credentials are declared to wrap(), and routed", () => {
  // A derived client keeps its parent's credential labels. Another key under
  // them would carry a posture the cloud never named for it.
  const vectors: Array<[string, () => { withOptions(options: never): unknown }, Record<string, unknown>]> = [
    ["openai", () => wrap(openai(openaiWire()), { consentClient: cloud() }), {
      apiKey: ATTACKER_KEY,
      adminAPIKey: ATTACKER_KEY,
      organization: "org_medhelp_other",
      project: "proj_medhelp_other",
      workloadIdentity: { provider: "gcp" },
    }],
    ["anthropic", () => wrap(anthropic(wire(() => json(MESSAGE))), { consentClient: cloud() }), {
      apiKey: ATTACKER_KEY,
      authToken: ATTACKER_KEY,
      credentials: { getCredentials: async () => ({ token: ATTACKER_KEY }) },
      config: { authentication: { type: "user_oauth" } },
      profile: "medhelp_other",
    }],
  ];
  for (const [name, make, options] of vectors) {
    for (const [key, value] of Object.entries(options)) {
      assert.throws(
        () => make().withOptions({ [key]: value } as never),
        (error: unknown) =>
          error instanceof CredentialRoutingError &&
          error.message.includes(`\`${key}\``) &&
          /consent\.wrap\(client, \{ credentials \}\)/.test(error.message) &&
          !error.message.includes(ATTACKER_KEY),
        `${name} ${key}`,
      );
    }
  }
});

test("a derivation cannot change how requests are built: that is a new client, wrapped anew", () => {
  const vectors: Array<[string, () => { withOptions(options: never): unknown }, Record<string, unknown>]> = [
    ["openai", () => wrap(openai(openaiWire()), { consentClient: cloud() }), {
      fetch: async () => new Response("{}"),
      fetchOptions: { body: "{\"store\":true}" },
      defaultQuery: { exfiltrate: "yes" },
      provider: { name: "azure" },
      somethingNew: true,
    }],
    ["anthropic", () => wrap(anthropic(wire(() => json(MESSAGE))), { consentClient: cloud() }), {
      fetch: async () => new Response("{}"),
      fetchOptions: {},
      middleware: [async (request: unknown, next: (r: unknown) => unknown) => next(request)],
      defaultQuery: {},
      somethingNew: true,
    }],
  ];
  for (const [name, make, options] of vectors) {
    for (const [key, value] of Object.entries(options)) {
      assert.throws(
        () => make().withOptions({ [key]: value } as never),
        (error: unknown) =>
          error instanceof UnsupportedProviderError &&
          error.message.includes(`\`${key}\``) &&
          /consent\.wrap\(\)/.test(error.message),
        `${name} ${key}`,
      );
    }
  }
});

test("a derivation cannot drop a reserved header the parent was built with", () => {
  // withOptions() spreads its argument over the parent's options, so a new
  // `defaultHeaders` replaces the whole inherited container: the
  // OpenAI-Project the client was constructed with would silently go.
  const w = openaiWire();
  const client = wrap(openai(w, { defaultHeaders: { "OpenAI-Project": "proj_medhelp_default" } }), { consentClient: cloud() });

  assert.throws(
    () => client.withOptions({ defaultHeaders: { "X-Request-Source": "batch" } }),
    (error: unknown) =>
      error instanceof CredentialRoutingError &&
      /OpenAI-Project/i.test(error.message) &&
      !error.message.includes("proj_medhelp_default"),
  );
  assert.doesNotThrow(() => client.withOptions({ timeout: 5_000 }), "a derivation that leaves the headers alone keeps them");

  const plain = wrap(openai(openaiWire(), { defaultHeaders: new Headers({ "X-Request-Source": "web" }) }), { consentClient: cloud() });
  assert.doesNotThrow(() => plain.withOptions({ defaultHeaders: { "X-Request-Source": "batch" } }));

  // Inherited headers the SDK cannot read might hold a reserved one: not assumed harmless either.
  class HeaderBag {
    "X-Request-Source" = "web";
  }
  const opaque = wrap(openai(openaiWire(), { defaultHeaders: new HeaderBag() }), { consentClient: cloud() });
  assert.throws(() => opaque.withOptions({ defaultHeaders: {} }), CredentialRoutingError);
});

test("a derivation on a library this SDK does not know is read with the names the known ones agree on", async () => {
  const { GenericAdapter } = await import("../src/adapters/index.ts");
  const { loadDescriptors } = await import("../src/descriptors.ts");
  const house = new GenericAdapter({ ...loadDescriptors().get("openai")!, clientModule: "house_llm", name: "house" });
  const make = () => ({ chat: { completions: { create: async (_body: Record<string, unknown>) => ({}) } } });
  const client = wrap(
    { ...make(), withOptions: (_options?: unknown) => make() },
    { consentClient: cloud(), adapter: house },
  );

  assert.doesNotThrow(() => client.withOptions({ timeout: 5_000, webhookKey: "whsec_x" })); // pragma: allowlist secret
  assert.throws(() => client.withOptions({ authToken: ATTACKER_KEY }), CredentialRoutingError);
  assert.throws(() => client.withOptions({ middleware: [] }), UnsupportedProviderError);
});

test("azure: a derivation cannot change the Azure credential, API version, deployment or endpoint", async () => {
  const { AzureOpenAI } = await import("openai");
  const make = () =>
    wrap(
      new AzureOpenAI({
        apiKey: "azure-test-key", // pragma: allowlist secret
        endpoint: "https://medhelp.openai.azure.com",
        apiVersion: "2024-10-21",
        fetch: openaiWire().fetch as typeof fetch,
        maxRetries: 0,
      }),
      { consentClient: cloud() },
    );

  // Refused before the derived client is constructed, which matters here:
  // openai's withOptions() does not carry `apiVersion` over, so an accepted
  // Azure derivation throws inside the library unless OPENAI_API_VERSION is set.
  assert.throws(
    () => make().withOptions({ azureADTokenProvider: async () => ATTACKER_KEY } as never),
    (error: unknown) => error instanceof CredentialRoutingError && error.message.includes("`azureADTokenProvider`"),
  );
  // `endpoint` names the host; the other two choose what the request addresses.
  for (const [key, value] of Object.entries({ apiVersion: "2025-01-01", deployment: "gpt-unapproved", endpoint: "https://attacker.example" })) {
    assert.throws(
      () => make().withOptions({ [key]: value } as never),
      (error: unknown) => error instanceof UnsupportedProviderError && error.message.includes(`\`${key}\``),
      key,
    );
  }
});

test("a refused derivation is refused before the client is even constructed", () => {
  // Constructing one is not free: Anthropic starts resolving credentials in
  // its constructor. And a derivation read as a class instance is read by the
  // keys the library would spread out of it.
  let constructed = 0;
  class OpenAI {
    baseURL = "https://api.openai.com/v1";
    fetch = async () => new Response("{}");
    chat = { completions: { create: async (_body: Record<string, unknown>) => ({}) } };
    constructor() {
      constructed++;
    }
    withOptions(_options?: unknown) {
      return new OpenAI();
    }
  }
  const client = wrap(new OpenAI(), { consentClient: cloud() });
  constructed = 0;

  assert.throws(() => client.withOptions({ fetchOptions: {} }), UnsupportedProviderError);
  class Derivation {
    apiKey = ATTACKER_KEY;
  }
  assert.throws(() => client.withOptions(new Derivation()), CredentialRoutingError);
  assert.throws(() => client.withOptions("abc"), UnsupportedProviderError, "a string spreads into indexed keys");
  assert.equal(constructed, 0);

  assert.doesNotThrow(() => client.withOptions());
  assert.doesNotThrow(() => client.withOptions(null));
  assert.equal(constructed, 2);
});

// ── G. an enforced request does not follow a redirect ────────────────────────

/** A server that answers every request with a redirect to another origin. */
function redirecting(): Wire {
  return wire(() => new Response(null, { status: 307, headers: { location: "https://attacker.example/v1/steal" } }));
}

test("openai and anthropic: an enforced request is sent with redirect: manual, and a 307 comes back as an error", async () => {
  // Followed, a 307 or 308 resends the body, the ACT token and the API key to
  // wherever the Location says, and the net only ever sees the first request.
  for (const status of [307, 308]) {
    const w = wire(() => new Response(null, { status, headers: { location: "https://attacker.example/v1/steal" } }));
    const client = wrap(openai(w), { consentClient: cloud() });
    const error = await outcome(() => client.chat.completions.create({ model: "gpt-4o", messages: MESSAGES, user_id: USER }));
    assert.ok(error instanceof OpenAI.APIError && error.status === status, `got ${String(error)}`);
    assert.deepEqual(w.sent.map((s) => s.redirect), ["manual"]);
  }

  const aw = redirecting();
  const claude = wrap(anthropic(aw), { consentClient: cloud() });
  const refused = await outcome(() =>
    claude.messages.create({ model: "claude-sonnet-4-5", max_tokens: 16, messages: MESSAGES, user_id: USER }),
  );
  assert.ok(refused instanceof Anthropic.APIError && refused.status === 307, `got ${String(refused)}`);
  assert.deepEqual(aw.sent.map((s) => s.redirect), ["manual"]);
});

test("mistral: the enforced Request itself carries redirect: manual", async () => {
  // Speakeasy calls `fetcher(request)` with nothing else, and a custom fetcher
  // is free to ignore a second argument: the mode has to be on the Request.
  const modes: string[] = [];
  const fetcher = async (input: Request, ...rest: unknown[]): Promise<Response> => {
    modes.push(`${input.redirect}+${rest.length}`);
    return new Response(null, { status: 307, headers: { location: "https://attacker.example/" } });
  };
  // pragma: allowlist secret
  const raw = new Mistral({ apiKey: "sk-mistral-test", httpClient: new HTTPClient({ fetcher: fetcher as never }) });
  const client = wrap(raw, { consentClient: cloud() });
  const error = await outcome(() =>
    client.chat.complete({ model: "mistral-small-latest", messages: MESSAGES, user_id: USER }),
  );
  assert.ok(error instanceof Error, `got ${String(error)}`);
  assert.deepEqual(modes, ["manual+0"]);
});

test("a request the net lets through untouched keeps its own redirect mode", async () => {
  // Only what enforcement authorized is changed. A GET carries no prompt, and
  // the net's line has always been to leave it alone.
  const w = wire(() => json({ object: "list", data: [] }));
  const client = wrap(openai(w), { consentClient: cloud() });
  await client.models.list();
  assert.deepEqual(w.sent.map((s) => [s.method, s.redirect]), [["GET", undefined]]);
});

test("gemini: a direct apiCall with a body and no token is refused on the real client", async () => {
  const w = wire(() => json(GEMINI_RESPONSE));
  const cc = cloud();
  await withGlobalFetch(w, async () => {
    const client = wrap(gemini(), { consentClient: cc });
    const apiClient = (client as unknown as { apiClient: { apiCall(url: string, init: RequestInit): Promise<unknown> } }).apiClient;
    const error = await outcome(() =>
      apiClient.apiCall("https://gemini.invalid/v1beta/models/gemini-2.0-flash:generateContent", {
        method: "POST",
        body: JSON.stringify({ contents: [{ parts: [{ text: "a private sentence" }] }] }),
      }),
    );
    assert.ok(inChain(error, UnenforcedCallError), `got ${String(error)}`);
  });
  assert.equal(cc.calls.length, 0);
  assert.equal(w.sent.length, 0);
});

test("gemini: an enforced request is sent with redirect: manual, and a 307 comes back as an error", async () => {
  // @google/genai calls the global fetch from ApiClient.apiCall, and no
  // request option reaches `redirect`: the net masks apiCall on the instance.
  const w = redirecting();
  const error = await withGlobalFetch(w, async () => {
    const client = wrap(gemini(), { consentClient: cloud() });
    return outcome(() => client.models.generateContent({ model: "gemini-2.0-flash", contents: "hi", user_id: USER }));
  });
  assert.ok(error instanceof Error && /307/.test(String((error as { status?: unknown }).status ?? error.message)), `got ${String(error)}`);
  assert.deepEqual(w.sent.map((s) => s.redirect), ["manual"]);
});
// ── the edges of the same rules ──────────────────────────────────────────────

test("gemini: a top-level key the signature does not have is refused, not dropped in silence", async () => {
  const w = wire(() => json(GEMINI_RESPONSE));
  const cc = cloud();
  await withGlobalFetch(w, async () => {
    const client = wrap(gemini(), { consentClient: cc });
    for (const key of ["httpOptions", "extra_headers"]) {
      const error = await outcome(() =>
        client.models.generateContent({ model: "gemini-2.0-flash", contents: "hi", [key]: {}, user_id: USER } as never),
      );
      assert.ok(error instanceof UnenforceableOptionError, `${key}: got ${String(error)}`);
      assert.match(error.message, new RegExp(`\`${key}\``));
    }
  });
  assert.equal(cc.calls.length, 0);
  assert.equal(w.sent.length, 0);
});

test("gemini: a typed config is read the way the adapter forwards it, through toJSON()", async () => {
  class TypedConfig {
    toJSON() {
      return { temperature: 0.2, httpOptions: { extraBody: { generationConfig: { temperature: 2 } } } };
    }
  }
  const { error, cloud: cc } = await geminiCall(new TypedConfig() as unknown as Record<string, unknown>);
  assert.ok(error instanceof UnenforceableOptionError, String(error));
  assert.match(error.message, /config\.httpOptions\.extraBody/);
  assert.equal(cc.calls.length, 0);

  // A typed httpOptions that dumps to something other than an object holds no
  // option at all: the adapter could not forward one either.
  class Opaque {
    toJSON() {
      return "opaque";
    }
  }
  const opaque = await geminiCall({ httpOptions: new Opaque() });
  assert.ok(!(opaque.error instanceof UnenforceableOptionError), String(opaque.error));
});

test("gemini: an executable tool written as a function is still an executable tool", async () => {
  const tool = Object.assign(() => undefined, { callTool: async () => [] });
  const { error, cloud: cc } = await geminiCall({ tools: [tool] });
  assert.ok(error instanceof UnenforceableOptionError, String(error));
  assert.equal(cc.calls.length, 0);
});

test("the constraint is applied for the endpoint actually called, aliases included", async () => {
  const { GenericAdapter } = await import("../src/adapters/index.ts");
  const { loadDescriptors } = await import("../src/descriptors.ts");
  const seen: (string | undefined)[] = [];
  class Recording extends GenericAdapter {
    override applyConstraints(params: Record<string, unknown>, constraints: Record<string, unknown>, callSite?: string) {
      seen.push(callSite);
      return super.applyConstraints(params, constraints, callSite);
    }
  }
  const w = openaiWire();
  const client = wrap(openai(w), { consentClient: cloud(), adapter: new Recording(loadDescriptors().get("openai")!) });
  await client.responses.create({ model: "gpt-4o", input: "hi", user_id: USER });
  await client.chat.completions.parse({ model: "gpt-4o", messages: MESSAGES, user_id: USER } as never);
  assert.deepEqual(seen, ["responses.create", "chat.completions.create"]);
});

test("a library this SDK does not know may pass headers, and nothing else", async () => {
  const { GenericAdapter } = await import("../src/adapters/index.ts");
  const { loadDescriptors } = await import("../src/descriptors.ts");
  const house = new GenericAdapter({ ...loadDescriptors().get("openai")!, clientModule: "house_llm", name: "house" });
  const sent: unknown[] = [];
  const cc = cloud();
  const client = wrap(
    { chat: { completions: { create: async (body: unknown, options: unknown) => (sent.push([body, options]), {}) } } },
    { consentClient: cc, adapter: house },
  );
  const create = client.chat.completions.create as unknown as (body: unknown, options: unknown) => Promise<unknown>;

  await create({ model: "house-1", messages: MESSAGES, user_id: USER }, { headers: { "X-Request-Id": "req_42" } });
  assert.equal(sent.length, 1);
  await assert.rejects(create({ model: "house-1", messages: MESSAGES, user_id: USER }, { timeout: 1 }), UnenforceableOptionError);
  assert.equal(sent.length, 1);
});

test("credential routing does not open a door: reserved headers are refused before any posture is chosen", async () => {
  // Routed options reach either the routed header or the alternative client as
  // the caller wrote them, so both routing forms are covered by one refusal.
  const w = openaiWire();
  const cc = cloud();
  const routed = wrap(openai(w), { consentClient: cc, credentials: { zero_retention: "proj_medhelp_zdr" } });
  await assert.rejects(
    routed.chat.completions.create(
      { model: "gpt-4o", messages: MESSAGES, user_id: USER },
      { headers: { "OpenAI-Project": "proj_medhelp_default" } },
    ),
    UnenforceableOptionError,
  );

  const aw = wire(() => json(MESSAGE));
  const alternative = anthropic(wire(() => json(MESSAGE)));
  const byClient = wrap(anthropic(aw), { consentClient: cc, credentials: { no_training: alternative } });
  await assert.rejects(
    byClient.messages.create(
      { model: "claude-sonnet-4-5", max_tokens: 16, messages: MESSAGES, user_id: USER },
      { headers: { "X-Api-Key": ATTACKER_KEY } },
    ),
    UnenforceableOptionError,
  );
  assert.equal(cc.calls.length, 0);
  assert.equal(w.sent.length + aw.sent.length, 0);
});

test("a client whose base URL does not name a server is refused at wrap()", () => {
  for (const baseURL of ["api.openai.com/v1", "file:///etc/openai", 42]) {
    class OpenAIShaped {
      baseURL = baseURL;
      fetch = async () => new Response("{}");
      chat = { completions: { create: async (_body: Record<string, unknown>) => ({}) } };
    }
    assert.throws(
      () => wrap(new OpenAIShaped(), { consentClient: cloud(), provider: "openai" }),
      UnsupportedProviderError,
      String(baseURL),
    );
  }
});

test("a derivation made while the base URL is still resolving is compared with what the parent would use", () => {
  const raw = anthropic(wire(() => json(MESSAGE)));
  const internals = raw as unknown as { _baseURLIsExplicit: boolean; _authState: { resolution: Promise<void> | null } };
  internals._baseURLIsExplicit = false;
  internals._authState.resolution = new Promise<void>(() => undefined);
  const client = wrap(raw, { consentClient: cloud() });

  assert.doesNotThrow(() => client.withOptions({ timeout: 5_000 }));
  assert.throws(() => client.withOptions({ baseURL: "https://attacker.example" }), UnsupportedProviderError);
});

test("a client that throws when asked what it is stops wrap(), rather than being taken for unidentified", () => {
  const target = { chat: { completions: { create: async (_body: Record<string, unknown>) => ({}) } } };
  const hostile = new Proxy(target, {
    get(object, key, receiver) {
      if (key === "constructor") throw new RangeError("this client refuses to say what it is");
      return Reflect.get(object, key, receiver);
    },
  });
  const create = target.chat.completions.create;
  assert.throws(() => wrap(hostile, { consentClient: cloud(), provider: "openai" }), RangeError);
  assert.equal(target.chat.completions.create, create, "nothing was patched");
});

// ── J. a routed call goes out under its own client's net ─────────────────────
//
// A client passed in `credentials` sends every call routed to it, so it gets
// what the wrapped client gets: its own transport guarded, its own server fixed
// at wrap(), redirects not followed. Its own server, not the wrapped client's:
// a residency posture is another host by design.

/** The cloud, naming the posture this user's answer requires. */
function routing(posture: string): Cloud {
  const inner = cloud();
  return {
    calls: inner.calls,
    async authorize(request: AuthorizeRequest): Promise<ConsentDecision> {
      return { ...(await inner.authorize(request)), credentialPosture: posture };
    },
  };
}

const REGIONAL = "https://eu.api.openai.com/v1";

test("a call routed to a regional client goes out to that client's server, under that client's net", async () => {
  const w = openaiWire();
  const ew = openaiWire();
  const regional = openai(ew, { baseURL: REGIONAL });
  const client = wrap(openai(w), { consentClient: routing("eu_residency"), credentials: { eu_residency: regional } });

  await client.chat.completions.create({ model: "gpt-4o", messages: MESSAGES, user_id: USER });
  assert.equal(w.sent.length, 0);
  assert.deepEqual(
    ew.sent.map((s) => [new URL(s.url).origin, s.headers.get("X-AI-Consent-Token"), s.redirect]),
    [["https://eu.api.openai.com", "act.jwt.token", "manual"]],
  );

  // The same net as the wrapped client's: what enforcement did not authorize
  // does not leave through the routed client either.
  await refusedAtTransport(
    () => regional.chat.completions.create({ model: "gpt-4o", messages: MESSAGES }),
    ew,
    "the routed client called directly",
  );
});

test("a routed client whose base URL changes after wrap() is refused, and nothing is sent", async () => {
  // Bound to its own server, not to the wrapped client's: moving it there is
  // refused as well. And a client derived later inherits the binding rather
  // than reading the moved URL again.
  const attempts: Array<[string, string]> = [
    ["https://attacker.example/v1", "moved to another host"],
    ["https://api.openai.com/v1", "moved to the wrapped client's own host"],
  ];
  for (const [baseURL, label] of attempts) {
    const ew = openaiWire();
    const regional = openai(ew, { baseURL: REGIONAL });
    const cc = routing("eu_residency");
    const client = wrap(openai(openaiWire()), { consentClient: cc, credentials: { eu_residency: regional } });

    regional.baseURL = baseURL;
    const call = { model: "gpt-4o", messages: MESSAGES, user_id: USER };
    await refusedAtTransport(() => client.chat.completions.create(call), ew, label);
    await refusedAtTransport(() => client.withOptions({ timeout: 5_000 }).chat.completions.create(call), ew, `${label}, derived`);
    assert.equal(cc.calls.length, 2, "authorized, then stopped at the transport");
  }
});

test("a redirect answered to a routed call is not followed", async () => {
  const ow = redirecting();
  const client = wrap(openai(openaiWire()), {
    consentClient: routing("eu_residency"),
    credentials: { eu_residency: openai(ow, { baseURL: REGIONAL }) },
  });
  const error = await outcome(() => client.chat.completions.create({ model: "gpt-4o", messages: MESSAGES, user_id: USER }));
  assert.ok(error instanceof OpenAI.APIError && error.status === 307, `got ${String(error)}`);
  assert.deepEqual(ow.sent.map((s) => s.redirect), ["manual"]);

  const aw = redirecting();
  const claude = wrap(anthropic(wire(() => json(MESSAGE))), {
    consentClient: routing("no_training"),
    // pragma: allowlist secret
    credentials: { no_training: anthropic(aw, { apiKey: "sk-ant-routed", baseURL: "https://eu.anthropic.invalid" }) },
  });
  const refused = await outcome(() =>
    claude.messages.create({ model: "claude-sonnet-4-5", max_tokens: 16, messages: MESSAGES, user_id: USER }),
  );
  assert.ok(refused instanceof Anthropic.APIError && refused.status === 307, `got ${String(refused)}`);
  assert.deepEqual(aw.sent.map((s) => [new URL(s.url).origin, s.redirect]), [["https://eu.anthropic.invalid", "manual"]]);

  const gw = redirecting();
  const geminiError = await withGlobalFetch(gw, async () => {
    const routed = new GoogleGenAI({
      apiKey: "gemini-test-routed", // pragma: allowlist secret
      httpOptions: { baseUrl: "https://gemini-eu.invalid" },
    });
    const client = wrap(gemini(), { consentClient: routing("no_training"), credentials: { no_training: routed } });
    return outcome(() => client.models.generateContent({ model: "gemini-2.0-flash", contents: "hi", user_id: USER }));
  });
  assert.ok(geminiError instanceof Error, `got ${String(geminiError)}`);
  assert.deepEqual(gw.sent.map((s) => [new URL(s.url).origin, s.redirect]), [["https://gemini-eu.invalid", "manual"]]);
});

test("the wrapped client named as its own posture keeps its own net, aliases included", async () => {
  // `{ default: client }` routes back to the wrapped client. `parse()` reaches
  // the patched `create()` from inside, and that nested hop must still be
  // recognised as this call's.
  const w = openaiWire();
  const raw = openai(w);
  const client = wrap(raw, { consentClient: routing("default"), credentials: { default: raw } });

  await client.chat.completions.parse({ model: "gpt-4o", messages: MESSAGES, user_id: USER });
  assert.deepEqual(w.sent.map((s) => [s.headers.get("X-AI-Consent-Token"), s.redirect]), [["act.jwt.token", "manual"]]);
});

test("a routed client with no transport the net can guard is refused at wrap(), and nothing is left patched", async () => {
  const w = wire(() => json({}));
  const raw = mistral(w);
  const cc = routing("no_training");
  assert.throws(
    // pragma: allowlist secret
    () => wrap(raw, { consentClient: cc, credentials: { no_training: new Mistral({ apiKey: "sk-mistral-routed" }) } }),
    (error: unknown) =>
      error instanceof UnsupportedProviderError &&
      error.message.includes("'no_training'") &&
      /new HTTPClient\(\)/.test(error.message),
  );

  // The wrapped client's own net was taken back with the rest: it sends as it
  // did before wrap() was tried.
  await outcome(() => raw.chat.complete({ model: "mistral-small-latest", messages: MESSAGES }));
  assert.equal(w.sent.length, 1);
  assert.equal(cc.calls.length, 0);
});

test("a routed client that does not say which server it sends to is refused at wrap()", () => {
  class Anthropic {
    fetch = async (): Promise<Response> => json(MESSAGE);
    messages = {
      create: async (_body: Record<string, unknown>) => MESSAGE,
      parse: async (_body: Record<string, unknown>) => MESSAGE,
    };
  }
  const raw = anthropic(wire(() => json(MESSAGE)));
  const transport = (raw as unknown as { fetch: unknown }).fetch;
  assert.throws(
    () => wrap(raw, { consentClient: routing("no_training"), credentials: { no_training: new Anthropic() } }),
    (error: unknown) => error instanceof UnsupportedProviderError && error.message.includes("'no_training'"),
  );
  assert.equal((raw as unknown as { fetch: unknown }).fetch, transport, "the wrapped client's transport was restored");
});

test("a routed client whose transport will not take the patch is refused at wrap()", () => {
  const routed = openai(openaiWire(), { baseURL: REGIONAL });
  Object.freeze(routed);
  const raw = openai(openaiWire());
  const transport = (raw as unknown as { fetch: unknown }).fetch;
  assert.throws(
    () => wrap(raw, { consentClient: routing("eu_residency"), credentials: { eu_residency: routed } }),
    (error: unknown) => error instanceof UnsupportedProviderError && error.message.includes("'eu_residency'"),
  );
  assert.equal((raw as unknown as { fetch: unknown }).fetch, transport, "the wrapped client's transport was restored");
});

test("the credentials mapping is read once at wrap(), derived clients included", async () => {
  // Node used to re-read the developer's own object every time a client was
  // derived from a wrapped one, so a posture added to it afterwards started
  // routing calls — on a client nobody had wrapped, bound to a server nobody
  // had read, and announced to the cloud as if it had been declared. Python
  // has always copied the mapping at wrap(), and its README says so.
  const late = openaiWire();
  const credentials: Record<string, unknown> = { default: "proj_medhelp_default" };
  const cc = routing("zero_retention");
  const client = wrap(openai(openaiWire()), { consentClient: cc, credentials });

  credentials.zero_retention = openai(late, { baseURL: "https://zdr.api.openai.com/v1" });
  const derived = client.withOptions({ timeout: 5_000 });

  // The cloud is told what the developer declared at wrap(), and nothing else.
  // withOptions() is typed by the provider as returning its own client class.
  const create = derived.chat.completions.create as unknown as (body: Record<string, unknown>) => Promise<unknown>;
  const error = await outcome(() => create({ model: "gpt-4o", messages: MESSAGES, user_id: USER }));
  assert.deepEqual(cc.calls.map((call) => call.credentialPostures), [["default"]]);
  assert.equal(late.sent.length, 0, "the late credential must carry nothing");
  assert.ok(error instanceof MissingCredentialError, `got ${String(error)}`);
});

// ── K. the Gemini NextGen surfaces leave through the same net ────────────────
//
// `@google/genai` 2.22 carries a second API next to `models` / `chats` /
// `files`: five prototype getters (`interactions`, `webhooks`, `agents`,
// `triggers`, `environments`) and `getNextGenClient()`. None of them is in
// `Object.keys(client)` — they are accessors — and none of them sends through
// `ApiClient.apiCall`, where the net had been put. Each builds its own
// Speakeasy SDK with its own `HTTPClient`, whose `fetcher` calls the global
// `fetch`. On a wrapped client `interactions.create({model, input})` therefore
// left the process with the prompt and the API key, no ACT token, no
// `/v1/authorize`, and nothing anywhere saying it happened.
//
// The net can be installed there, so it is — rather than the client being
// refused. No descriptor declares a call site on those routes, so every
// body-bearing request on them is refused, and every retrieval still works.

type NextGen = Record<string, Record<string, (...args: never[]) => Promise<unknown>>>;

const NEXTGEN_POSTS: Record<string, (ai: NextGen) => Promise<unknown>> = {
  interactions: (ai) => ai.interactions!.create!({ model: "gemini-2.0-flash", input: "a private sentence" } as never),
  "interactions, api_version": (ai) =>
    ai.interactions!.create!({ model: "gemini-2.0-flash", input: "a private sentence", api_version: "v1alpha" } as never),
  agents: (ai) => ai.agents!.create!({ displayName: "helper" } as never),
  webhooks: (ai) => ai.webhooks!.create!({ url: "https://hooks.invalid/inbox" } as never),
  triggers: (ai) => ai.triggers!.create!({ name: "nightly" } as never),
  environments: (ai) => ai.environments!.create!({ displayName: "sandbox" } as never),
};

test("gemini: every NextGen surface refuses a body-bearing request, and says where it belongs", async () => {
  for (const [surface, post] of Object.entries(NEXTGEN_POSTS)) {
    const w = wire(() => json({ id: "int_1" }));
    const cc = cloud();
    const error = await withGlobalFetch(w, async () => {
      const client = wrap(gemini(), { consentClient: cc });
      return outcome(() => post(client as unknown as NextGen));
    });
    const refusal = inChain(error, UnenforcedCallError);
    assert.ok(refusal, `${surface}: got ${String(error)}`);
    assert.match(refusal.message, /NextGen/, surface);
    assert.match(refusal.message, /unwrapped/, surface);
    assert.equal(w.sent.length, 0, surface);
    assert.equal(cc.calls.length, 0, surface);
  }
});

test("gemini: each client an api_version builds is guarded, not just the first", async () => {
  // `getClient(apiVersion)` caches nothing: it builds a NEW Speakeasy client on
  // every call that names a version. Guarding what the surface built the first
  // time would leave every call after it carrying the prompt out untouched.
  const w = wire(() => json({ id: "int_1" }));
  const cc = cloud();
  const errors = await withGlobalFetch(w, async () => {
    const ai = wrap(gemini(), { consentClient: cc }) as unknown as NextGen;
    const outcomes: unknown[] = [];
    for (const api_version of [undefined, "v1alpha", "v1beta"]) {
      outcomes.push(
        await outcome(() =>
          ai.interactions!.create!({ model: "gemini-2.0-flash", input: "a private sentence", api_version } as never),
        ),
      );
    }
    return outcomes;
  });

  errors.forEach((error, index) => assert.ok(inChain(error, UnenforcedCallError), `#${index}: ${String(error)}`));
  assert.equal(w.sent.length, 0);
  assert.equal(cc.calls.length, 0);
});

test("gemini: a NextGen retrieval still goes out, like any other bodyless call", async () => {
  const w = wire(() => json({ id: "int_1", object: "interaction" }));
  await withGlobalFetch(w, async () => {
    const client = wrap(gemini(), { consentClient: cloud() }) as unknown as NextGen;
    await client.interactions!.get!("int_1" as never);
  });
  assert.deepEqual(w.sent.map((s) => [s.method, new URL(s.url).pathname]), [["GET", "/v1beta/interactions/int_1"]]);
});

test("gemini: wrapping says nothing on the experimental surfaces it does not touch", async () => {
  // `get agents()`, `get triggers()` and `get environments()` each warn on first
  // access. Reading them at wrap() to guard what they build would print three
  // warnings the developer did not cause, so nothing is read until they read it.
  const warnings: unknown[] = [];
  const warn = console.warn;
  console.warn = (...args: unknown[]) => warnings.push(args[0]);
  try {
    const w = wire(() => json({ id: "int_1" }));
    await withGlobalFetch(w, async () => {
      const client = wrap(gemini(), { consentClient: cloud() });
      assert.deepEqual(warnings, [], "wrap() must not touch the experimental getters");
      await outcome(() => (client as unknown as NextGen).agents!.create!({ displayName: "helper" } as never));
    });
    assert.equal(warnings.length, 1, "and the one warning left is the library's own, on the developer's access");
  } finally {
    console.warn = warn;
  }
});

test("gemini: a NextGen surface taken before wrap() is under the net too", async () => {
  // The getters cache on the instance, so a developer who read `interactions`
  // before wrapping holds an object the shadowed getter would never be asked
  // for again. It is guarded where it already is.
  const w = wire(() => json({ id: "int_1" }));
  const cc = cloud();
  const error = await withGlobalFetch(w, async () => {
    const raw = gemini();
    const surface = (raw as unknown as NextGen).interactions!;
    wrap(raw, { consentClient: cc });
    return outcome(() => surface.create!({ model: "gemini-2.0-flash", input: "a private sentence" } as never));
  });
  assert.ok(inChain(error, UnenforcedCallError), `got ${String(error)}`);
  assert.equal(w.sent.length, 0);
  assert.equal(cc.calls.length, 0);
});

test("gemini: the Speakeasy client getNextGenClient() hands back is guarded as well", async () => {
  const w = wire(() => json({ id: "int_1" }));
  const cc = cloud();
  const error = await withGlobalFetch(w, async () => {
    const client = wrap(gemini(), { consentClient: cc });
    const sdk = (client as unknown as { getNextGenClient(): NextGen }).getNextGenClient();
    return outcome(() => sdk.interactions!.create!({ model: "gemini-2.0-flash", input: "a private sentence" } as never));
  });
  assert.ok(inChain(error, UnenforcedCallError), `got ${String(error)}`);
  assert.equal(w.sent.length, 0);
  assert.equal(cc.calls.length, 0);
});

test("gemini: a NextGen surface this SDK has not classified fails the build", () => {
  // A tripwire, in the spirit of the per-call options one, and for the same
  // reason: the cover is a NAMED list in `guard.ts` — see the comment there for
  // why it cannot be a general rule about prototype accessors — so a name the
  // library adds must not be able to arrive quietly. This reads what the
  // INSTALLED @google/genai actually exposes rather than restating today's five
  // names, and it reads it through descriptors, so no getter is evaluated and
  // no experimental warning is printed.
  //
  // Prototype METHODS are classified here rather than in the source, because
  // only one of them builds anything; a new one fails this test until somebody
  // has looked at what it returns.
  // A Map, not an object literal: `constructor` and `toString` are inherited
  // names, and an object would answer for them whether they were classified or
  // not — which is the one thing a tripwire must not do.
  const CLASSIFIED_METHODS = new Map<string, "shadowed" | "builds nothing">([
    ["constructor", "builds nothing"],
    ["getNextGenClient", "shadowed"],
  ]);
  const UNTIL_THEN = " Until somebody has looked at it, a request could leave a wrapped client unproven.";
  const NEW_SURFACE =
    " Read what it builds: if it makes its own HTTP client, add its name to NEXTGEN_SURFACES in src/guard.ts " +
    "so the net reaches it; if it shares the parent's transport, say so there." + UNTIL_THEN;
  const NEW_METHOD =
    " Read what it returns: a factory handing back a generated client has to be shadowed like " +
    "getNextGenClient(); anything else is classified as building nothing, here." + UNTIL_THEN;

  const client = wrap(gemini(), { consentClient: cloud() });

  for (const name of Object.getOwnPropertyNames(GoogleGenAI.prototype)) {
    const onPrototype = Object.getOwnPropertyDescriptor(GoogleGenAI.prototype, name)!;
    const onInstance = Object.getOwnPropertyDescriptor(client, name);

    if (typeof onPrototype.get === "function") {
      assert.ok(
        typeof onInstance?.get === "function" && onInstance.get !== onPrototype.get,
        `@google/genai exposes the prototype accessor '${name}', and wrap() does not shadow it.${NEW_SURFACE}`,
      );
      continue;
    }

    const classified = CLASSIFIED_METHODS.get(name);
    assert.ok(classified !== undefined, `@google/genai added the prototype method '${name}'.${NEW_METHOD}`);
    if (classified === "shadowed") {
      assert.equal(typeof onInstance?.value, "function", `'${name}' is classified as shadowed and wrap() does not shadow it.${UNTIL_THEN}`);
    } else {
      assert.equal(onInstance, undefined, `'${name}' builds nothing, so wrap() should leave it alone.`);
    }
  }
});

test("gemini: a NextGen client that cloned its HTTPClient would leave the net, and this says so", async () => {
  // The canary for the one hole this cover knowingly leaves open.
  //
  // The net is installed on `HTTPClient.fetcher`. `HTTPClient.clone()` builds
  // `new HTTPClient(this.options)`, and `options.fetcher` is undefined for the
  // client `buildGoogleGenAIClient` makes — it passes no `http_client` at all —
  // so a clone would fall back on DEFAULT_FETCHER, the global `fetch`, outside
  // the net and carrying the prompt.
  //
  // Nothing clones on this path in 2.22.0: the only use is
  // `this._httpClient.request(req)`. So the net writes `fetcher` and NOT
  // `options.fetcher`, deliberately — writing both would make the walk see a
  // second transport at `options.fetcher` and record the same check twice.
  // Rather than harden against something the library does not do, this fails
  // loudly on the day it starts.
  const probe = new GoogleGenAI({ apiKey: "gemini-test-key", httpOptions: { baseUrl: "https://gemini.invalid" } }); // pragma: allowlist secret
  // `getNextGenClient()` is typed private; the library still exposes it, and
  // this test is about what the library does rather than what its types say.
  const nextGen = (probe as unknown as { getNextGenClient(): { _httpClient: object } }).getNextGenClient();
  const httpClient = nextGen._httpClient;
  const prototype = Object.getPrototypeOf(httpClient) as { clone?: unknown };
  assert.equal(
    typeof prototype.clone,
    "function",
    "@google/genai's HTTPClient no longer has clone(). Re-read this test: it may now be watching nothing.",
  );

  const original = prototype.clone as (this: unknown) => unknown;
  let clones = 0;
  prototype.clone = function (this: unknown): unknown {
    clones++;
    return original.call(this);
  };
  const w = wire(() => json({ id: "int_1", object: "interaction" }));
  try {
    await withGlobalFetch(w, async () => {
      const ai = wrap(gemini(), { consentClient: cloud() }) as unknown as NextGen;
      await outcome(() => ai.interactions!.create!({ model: "gemini-2.0-flash", input: "a private sentence" } as never));
      await ai.interactions!.get!("int_1" as never); // the route that does reach the wire
    });
  } finally {
    prototype.clone = original;
  }

  assert.equal(
    clones,
    0,
    "@google/genai now clones its HTTPClient while sending. A clone starts from options.fetcher, which is unset, " +
      "so it would send on the global fetch with the net nowhere near it. The net must now also write " +
      "options.fetcher — and the walk in guard.ts must be kept from counting that as a second transport.",
  );
  assert.deepEqual(w.sent.map((s) => s.method), ["GET"], "the POST was refused, the retrieval went out");
});

test("gemini: the call sites a descriptor does declare still go out, NextGen or not", async () => {
  // The net added over those surfaces changes nothing for the API that has
  // call sites: one authorize, one request, the token on it.
  const w = wire(() => json(GEMINI_RESPONSE));
  const cc = cloud();
  await withGlobalFetch(w, async () => {
    const client = wrap(gemini(), { consentClient: cc });
    await client.models.generateContent({ model: "gemini-2.0-flash", contents: "a private sentence", user_id: USER });
  });
  assert.equal(cc.calls.length, 1);
  assert.deepEqual(w.sent.map((s) => [s.method, s.headers.get("X-AI-Consent-Token")]), [["POST", "act.jwt.token"]]);
});
