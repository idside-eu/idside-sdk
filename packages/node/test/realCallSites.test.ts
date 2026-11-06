import { test } from "node:test";
import assert from "node:assert/strict";
import OpenAI from "openai";
import Anthropic from "@anthropic-ai/sdk";
import { GoogleGenAI } from "@google/genai";
import { Mistral, HTTPClient } from "@mistralai/mistralai";
import { wrap } from "../src/consent.ts";
import { UnenforcedCallError } from "../src/exceptions.ts";
import type { ConsentClient, ConsentDecision } from "../src/transport.ts";

// End-to-end against the real provider SDKs, with their HTTP layer mocked. This
// is where the descriptor's JS injection convention is proven: the ACT token has
// to leave the machine on the actual request the provider client builds.
const DECISION: ConsentDecision = {
  consentState: { training: false },
  paramsForced: { store: false },
  actToken: "act.jwt.token",
  source: "widget",
  legalBasis: "consent",
  binding: "ubt",
  constraints: {},
};

const consentClient: ConsentClient = {
  async authorize(): Promise<ConsentDecision> {
    return DECISION;
  },
};

interface Captured {
  url: string;
  headers: Headers;
  body: Record<string, unknown> | undefined;
}

function captureFetch(responseBody: unknown): { fetch: typeof fetch; captured: Captured[] } {
  const captured: Captured[] = [];
  const impl = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : (input as Request).url;
    let body: Record<string, unknown> | undefined;
    try {
      body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : undefined;
    } catch {
      body = undefined;
    }
    captured.push({ url, headers: new Headers(init?.headers), body });
    return new Response(JSON.stringify(responseBody), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  return { fetch: impl as unknown as typeof fetch, captured };
}

test("openai: the ACT token header and forced params reach the wire; identity never does", async () => {
  const { fetch: mock, captured } = captureFetch({
    id: "chatcmpl_1",
    object: "chat.completion",
    created: 0,
    model: "gpt-4o",
    choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
  });
  const client = new OpenAI({ apiKey: "sk-test", fetch: mock, maxRetries: 0 }); // pragma: allowlist secret
  const wrapped = wrap(client, { consentClient, provider: "openai" });

  // No cast: `wrap()` returns the client with the identity typed onto every
  // declared call site, so this is the call a developer actually writes.
  await wrapped.chat.completions.create({
    model: "gpt-4o",
    messages: [{ role: "user", content: "hi" }],
    user_id: "usr_marie_456",
  });

  const request = captured.at(-1)!;
  assert.equal(request.headers.get("X-AI-Consent-Token"), "act.jwt.token");
  assert.equal(request.body?.store, false, "forced param must reach the provider");
  assert.equal(request.body?.user_id, undefined, "identity must never reach the provider");
});

test("anthropic: the ACT token header reaches the wire; identity never does", async () => {
  const { fetch: mock, captured } = captureFetch({
    id: "msg_1",
    type: "message",
    role: "assistant",
    model: "claude-3-5-sonnet",
    content: [{ type: "text", text: "ok" }],
    stop_reason: "end_turn",
    usage: { input_tokens: 1, output_tokens: 1 },
  });
  const client = new Anthropic({ apiKey: "sk-ant-test", fetch: mock, maxRetries: 0 }); // pragma: allowlist secret
  const wrapped = wrap(client, { consentClient, provider: "anthropic" });

  await wrapped.messages.create({
    model: "claude-3-5-sonnet-latest",
    max_tokens: 16,
    messages: [{ role: "user", content: "hi" }],
    user_id: "usr_marie_456",
  });

  const request = captured.at(-1)!;
  assert.equal(request.headers.get("X-AI-Consent-Token"), "act.jwt.token");
  assert.equal(request.body?.user_id, undefined, "identity must never reach the provider");
});

test("gemini: the real client carries the ACT token to its nested HTTP options", async () => {
  const { fetch: mock, captured } = captureFetch({
    candidates: [
      {
        content: { role: "model", parts: [{ text: "ok" }] },
        finishReason: "STOP",
        index: 0,
      },
    ],
  });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = mock;
  try {
    // @google/genai does not expose a constructor-level fetch override. Its real
    // request builder is exercised here while only the final global fetch is
    // replaced, so no provider request can leave the process.
    const client = new GoogleGenAI({
      apiKey: "gemini-test-key", // pragma: allowlist secret
      httpOptions: { baseUrl: "https://gemini.invalid" },
    });
    const calls: unknown[] = [];
    const wrapped = wrap(client, {
      provider: "gemini",
      consentClient: {
        async authorize(request): Promise<ConsentDecision> {
          calls.push(request);
          return { ...DECISION, paramsForced: {} };
        },
      },
    });

    await wrapped.models.generateContent({
      model: "gemini-2.0-flash",
      contents: "hi",
      user_id: "usr_marie_456",
    });

    assert.equal(calls.length, 1, "the real call triggers one authorization");
    assert.equal(captured.length, 1, "the real client builds one HTTP request");
    const request = captured[0]!;
    assert.equal(request.headers.get("X-AI-Consent-Token"), "act.jwt.token");
    assert.equal(request.body?.user_id, undefined, "identity must never reach the provider");

    await assert.rejects(
      wrapped.models.embedContent({ model: "text-embedding-004", contents: "private text" }),
      UnenforcedCallError,
      "an undeclared Gemini endpoint must be stopped at the real client's HTTP boundary",
    );
    assert.equal(captured.length, 1, "the undeclared request never reaches fetch");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("mistral: the ACT token header reaches the wire via options.headers", async () => {
  // The mistralai (Speakeasy) client calls its fetcher with a Request object.
  const captured: { headers: Headers; body: Record<string, unknown> | undefined }[] = [];
  const fetcher = async (input: string | URL | Request): Promise<Response> => {
    const req = input as Request;
    let body: Record<string, unknown> | undefined;
    try {
      const text = await req.clone().text();
      body = text ? (JSON.parse(text) as Record<string, unknown>) : undefined;
    } catch {
      body = undefined;
    }
    captured.push({ headers: new Headers(req.headers), body });
    return new Response(JSON.stringify({ id: "cmpl_1", object: "chat.completion", model: "mistral-small" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  const client = new Mistral({ apiKey: "sk-mistral-test", httpClient: new HTTPClient({ fetcher }) }); // pragma: allowlist secret
  const wrapped = wrap(client, { consentClient, provider: "mistral" });

  // Speakeasy validates the response shape; we only care that the request carried
  // the token, captured by the fetcher before any response parsing.
  await wrapped.chat
    .complete({
      model: "mistral-small-latest",
      messages: [{ role: "user", content: "hi" }],
      user_id: "usr_marie_456",
    })
    .catch(() => undefined);

  const request = captured.at(-1)!;
  assert.equal(request.headers.get("X-AI-Consent-Token"), "act.jwt.token", "token must reach the Mistral request");
  assert.equal(request.body?.user_id, undefined, "identity must never reach the provider");
});
