/**
 * The interception net: nothing body-bearing leaves a wrapped client unenforced.
 *
 * `wrap()` patches methods on resource instances, which can never be exhaustive:
 * `chat.completions.parse()` goes straight to the transport, `withOptions()`
 * hands back a fresh unpatched client, and `embeddings` / `images` / `audio` /
 * `batches` were never declared at all. Each of those reached the provider with
 * no authorize, no ACT token and no ledger entry.
 *
 * These tests are deliberately probe-style: the provider client is a REAL one,
 * built with a `fetch` that records every call and then fails. So an assertion
 * on `probe.calls` is an assertion about what left the process — not about what
 * the SDK believes it did.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import OpenAI from "openai";
import { wrap } from "../src/consent.ts";
import { UNKNOWN_ORIGIN, originOf, requestOrigin } from "../src/destination.ts";
import { installTransportGuard } from "../src/guard.ts";
import type { Enforced } from "../src/enforced.ts";
import {
  MissingIdentityError,
  UnenforceableOptionError,
  UnenforcedCallError,
  UnsupportedProviderError,
} from "../src/exceptions.ts";
import type {
  ConsentClient,
  ConsentDecision,
  AuthorizeRequest,
} from "../src/transport.ts";

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

interface Probe {
  readonly calls: { method: string; path: string; token: string | null }[];
  readonly fetch: (url: unknown, init?: unknown) => Promise<Response>;
}

/** A `fetch` that records what would have gone out, then refuses to go out. */
function probeFetch(): Probe {
  const calls: { method: string; path: string; token: string | null }[] = [];
  return {
    calls,
    async fetch(url: unknown, init?: unknown): Promise<Response> {
      const options = (init ?? {}) as {
        method?: string;
        headers?: ConstructorParameters<typeof Headers>[0];
      };
      calls.push({
        method: (options.method ?? "GET").toUpperCase(),
        path: new URL(String(url)).pathname,
        token: new Headers(options.headers).get("X-AI-Consent-Token"),
      });
      throw new Error("PROBE: an HTTP request left the process");
    },
  };
}

function openaiClient(probe: Probe): OpenAI {
  // pragma: allowlist secret
  return new OpenAI({
    apiKey: "sk-test",
    fetch: probe.fetch as typeof fetch,
    maxRetries: 0,
  });
}

function provenanceCount(fn: unknown): number | undefined {
  if (typeof fn !== "function") return undefined;
  const guarded = Object.getOwnPropertySymbols(fn).find(
    (symbol) => symbol.description === "idside.guard.installed",
  );
  if (guarded === undefined) return undefined;
  const provenances = (fn as unknown as Record<symbol, unknown>)[guarded];
  return Array.isArray(provenances) ? provenances.length : undefined;
}

/** The error itself, or whatever it was wrapped in — providers re-throw. */
function causeChain(error: unknown): unknown[] {
  const chain: unknown[] = [];
  let current = error;
  while (current !== undefined && current !== null && chain.length < 8) {
    chain.push(current);
    current = (current as { cause?: unknown }).cause;
  }
  return chain;
}

function unenforced(error: unknown): UnenforcedCallError | undefined {
  return causeChain(error).find(
    (e): e is UnenforcedCallError => e instanceof UnenforcedCallError,
  );
}

// ── layer 1: call-site aliases ────────────────────────────────────────────────

test("chat.completions.parse() is enforced (alias of chat.completions.create)", async () => {
  const probe = probeFetch();
  const cc = fakeConsentClient();
  const client = wrap(openaiClient(probe), {
    consentClient: cc,
    provider: "openai",
  });

  await assert.rejects(
    client.chat.completions.parse({
      model: "gpt-4o",
      messages: [],
      user_id: "usr_marie_456",
    } as never),
  );

  assert.equal(
    cc.calls.length,
    1,
    "parse() must trigger exactly one authorize",
  );
  assert.equal(
    cc.calls[0]!.callSite,
    "chat.completions.create",
    "the alias keeps the wire id",
  );
  assert.equal(probe.calls.length, 1);
  assert.equal(
    probe.calls[0]!.token,
    "act.jwt.token",
    "the request that left carried the ACT token",
  );
});

test("re-wrapping does not retain another transport provenance", () => {
  const probe = probeFetch();
  const cc = fakeConsentClient();
  const raw = openaiClient(probe);
  const rawTransport = raw as unknown as { fetch: typeof fetch };
  const once = wrap(raw, { consentClient: cc, provider: "openai" });
  const guardedFetch = rawTransport.fetch;
  assert.equal(provenanceCount(guardedFetch), 1);

  const twice = wrap(once, { consentClient: cc, provider: "openai" });

  assert.equal(twice, once);
  assert.equal(rawTransport.fetch, guardedFetch);
  assert.equal(provenanceCount(guardedFetch), 1);
});

test("chat.completions.parse() without an identity sends nothing at all", async () => {
  const probe = probeFetch();
  const cc = fakeConsentClient();
  const client = wrap(openaiClient(probe), {
    consentClient: cc,
    provider: "openai",
  });

  await assert.rejects(
    client.chat.completions.parse({ model: "gpt-4o", messages: [] } as never),
    MissingIdentityError,
  );
  assert.equal(cc.calls.length, 0);
  assert.equal(probe.calls.length, 0, "no HTTP request may leave");
});

test("responses.parse() is enforced too (both openai call sites declare the alias)", async () => {
  const probe = probeFetch();
  const cc = fakeConsentClient();
  const client = wrap(openaiClient(probe), {
    consentClient: cc,
    provider: "openai",
  });

  await assert.rejects(
    client.responses.parse({ model: "gpt-4o", input: "hi" } as never),
    MissingIdentityError,
  );
  assert.equal(probe.calls.length, 0, "no HTTP request may leave");
});

test("a `stream` alias is skipped in Node: stream() must not become a Promise", () => {
  // Anthropic's descriptor lists `stream` as an alias of messages.create. Python
  // patches it (its wrapper is sync); Node must not, because the enforced wrapper
  // is `async` and `messages.stream()` returns a MessageStream synchronously.
  // Enforcement is unaffected: stream() reaches the endpoint through create().
  const sentinel = { on(): void {} };
  const messages = {
    create(body: Record<string, unknown>) {
      return Promise.resolve({ id: "msg_1", body });
    },
    parse(body: Record<string, unknown>) {
      return (this as typeof messages).create(body);
    },
    stream(_body: Record<string, unknown>) {
      return sentinel;
    },
  };
  const client = wrap(
    { messages },
    { consentClient: fakeConsentClient(), provider: "anthropic" },
  );

  const result = client.messages.stream({
    model: "claude-3",
    messages: [],
    user_id: "usr_marie_456",
  });
  assert.equal(
    result,
    sentinel,
    "stream() must stay synchronous and keep its return value",
  );
});

// ── layer 2: re-entrancy pass-through ────────────────────────────────────────

test("an alias that re-enters the patched create() authorizes once, not twice", async () => {
  // The real openai `chat.completions.parse` calls `this._client.chat.completions
  // .create(body, {...options, headers: {...options?.headers, ...}})`: the second
  // hop arrives on the already-patched create, carrying the ACT token header. A
  // second enforcement would fail — the identity was consumed by the first.
  const probe = probeFetch();
  const cc = fakeConsentClient();
  const client = wrap(openaiClient(probe), {
    consentClient: cc,
    provider: "openai",
  });

  await assert.rejects(
    client.chat.completions.parse({
      model: "gpt-4o",
      messages: [],
      user_id: "usr_marie_456",
    } as never),
  );

  assert.equal(cc.calls.length, 1, "one call = one authorize = one ACT token");
  assert.equal(probe.calls.length, 1);
});

test("a caller-supplied token header cannot skip authorization", async () => {
  const calls: {
    body: Record<string, unknown>;
    options?: { headers?: Record<string, string> };
  }[] = [];
  const messages = {
    create(
      body: Record<string, unknown>,
      options?: { headers?: Record<string, string> },
    ) {
      calls.push({ body, options });
      return Promise.resolve({ id: "msg_1" });
    },
  };
  const cc = fakeConsentClient();
  const client = wrap(
    { messages },
    { consentClient: cc, provider: "anthropic" },
  );
  const callWithoutIdentity = client.messages.create as unknown as (
    body: Record<string, unknown>,
    options?: { headers?: Record<string, string> },
  ) => Promise<unknown>;

  for (const token of ["", "act.caller.supplied"]) {
    await assert.rejects(
      callWithoutIdentity(
        { model: "claude-3", messages: [] },
        { headers: { "X-AI-Consent-Token": token } },
      ),
      MissingIdentityError,
    );
  }

  assert.equal(
    cc.calls.length,
    0,
    "identity failure happens before authorization",
  );
  assert.equal(calls.length, 0, "the provider method was not called");
});

// ── layer 3: the transport net ───────────────────────────────────────────────

test("an unenforced POST endpoint is refused before the request goes out", async () => {
  const probe = probeFetch();
  const cc = fakeConsentClient();
  const client = wrap(openaiClient(probe), {
    consentClient: cc,
    provider: "openai",
  });

  const error = await client.embeddings
    .create({ model: "text-embedding-3-small", input: "a private sentence" })
    .then(() => undefined)
    .catch((e: unknown) => e);

  const refusal = unenforced(error);
  assert.ok(
    refusal,
    `expected an UnenforcedCallError in the cause chain, got ${String(error)}`,
  );
  assert.equal(
    cc.calls.length,
    0,
    "an endpoint the SDK does not enforce never reaches the cloud either",
  );
  assert.equal(probe.calls.length, 0, "NO HTTP request may leave");
});

test("a caller-supplied token header cannot open the transport net", async () => {
  for (const token of ["caller.forged.token", DECISION.actToken]) {
    const probe = probeFetch();
    const cc = fakeConsentClient();
    const client = wrap(openaiClient(probe), {
      consentClient: cc,
      provider: "openai",
    });

    const error = await client.embeddings
      .create(
        { model: "text-embedding-3-small", input: "a private sentence" },
        { headers: { "X-AI-Consent-Token": token } },
      )
      .then(() => undefined)
      .catch((e: unknown) => e);

    assert.ok(unenforced(error), `caller token ${token} opened the net`);
    assert.equal(
      cc.calls.length,
      0,
      "an undeclared endpoint is never authorized",
    );
    assert.equal(probe.calls.length, 0, "NO HTTP request may leave");
  }
});

test("a caller token header, in any case, is refused rather than silently replaced", async () => {
  // It used to be overwritten by the authorized token, which hid a call that
  // tried to carry its own proof. Now it is named and refused before anything.
  const probe = probeFetch();
  const cc = fakeConsentClient();
  const client = wrap(openaiClient(probe), {
    consentClient: cc,
    provider: "openai",
  });

  await assert.rejects(
    client.chat.completions.create(
      { model: "gpt-4o", messages: [], user_id: "usr_marie_456" } as never,
      { headers: { "x-ai-consent-token": "caller.forged.token" } },
    ),
    UnenforceableOptionError,
  );

  assert.equal(cc.calls.length, 0);
  assert.equal(probe.calls.length, 0);
});

test("an active permit cannot authorize another client or another endpoint", async () => {
  let otherWire = 0;
  const other = wrap(
    new OpenAI({
      apiKey: "sk-test", // pragma: allowlist secret
      maxRetries: 0,
      fetch: (async () => {
        otherWire++;
        return new Response("{}", {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }) as typeof fetch,
    }),
    { consentClient: fakeConsentClient(), provider: "openai" },
  );

  const paths: string[] = [];
  let crossClientError: unknown;
  let crossEndpointError: unknown;
  let client!: Enforced<OpenAI>;
  const fetch = async (
    input: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> => {
    const path = new URL(String(input)).pathname;
    paths.push(path);
    const token = new Headers(init?.headers).get("X-AI-Consent-Token")!;
    if (path === "/v1/chat/completions") {
      crossClientError = await other.embeddings
        .create(
          { model: "text-embedding-3-small", input: "private" },
          { headers: { "X-AI-Consent-Token": token } },
        )
        .catch((error: unknown) => error);
      crossEndpointError = await client.embeddings
        .create(
          { model: "text-embedding-3-small", input: "private" },
          { headers: { "X-AI-Consent-Token": token } },
        )
        .catch((error: unknown) => error);
      return new Response(
        JSON.stringify({
          id: "chatcmpl_1",
          object: "chat.completion",
          created: 0,
          model: "gpt-4o",
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: "ok" },
              finish_reason: "stop",
            },
          ],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    return new Response(JSON.stringify({ data: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  const cc = fakeConsentClient();
  client = wrap(new OpenAI({ apiKey: "sk-test", maxRetries: 0, fetch }), {
    consentClient: cc,
    provider: "openai",
  });

  await client.chat.completions.create({
    model: "gpt-4o",
    messages: [{ role: "user", content: "hi" }],
    user_id: "usr_marie_456",
  });

  assert.ok(unenforced(crossClientError));
  assert.ok(unenforced(crossEndpointError));
  assert.equal(cc.calls.length, 1);
  assert.equal(otherWire, 0);
  assert.deepEqual(paths, ["/v1/chat/completions"]);
});

test("a provider retry keeps the same authorized transport route", async () => {
  const tokens: string[] = [];
  const fetch = async (
    _input: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> => {
    tokens.push(new Headers(init?.headers).get("X-AI-Consent-Token") ?? "");
    if (tokens.length === 1) {
      return new Response(JSON.stringify({ error: { message: "retry" } }), {
        status: 500,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(
      JSON.stringify({
        id: "chatcmpl_1",
        object: "chat.completion",
        created: 0,
        model: "gpt-4o",
        choices: [
          {
            index: 0,
            message: { role: "assistant", content: "ok" },
            finish_reason: "stop",
          },
        ],
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  const cc = fakeConsentClient();
  const client = wrap(
    new OpenAI({
      apiKey: "sk-test",
      maxRetries: 1,
      fetch: fetch as typeof globalThis.fetch,
    }),
    { consentClient: cc, provider: "openai" },
  );

  await client.chat.completions.create({
    model: "gpt-4o",
    messages: [],
    user_id: "usr_1",
  });

  assert.equal(cc.calls.length, 1);
  assert.deepEqual(tokens, [DECISION.actToken, DECISION.actToken]);
});

test("a bodyless request does not pin transport provenance before a POST", async () => {
  const calls: string[] = [];
  const routes: string[] = [];
  const client = {
    fetch: async (url: string, init?: RequestInit): Promise<Response> => {
      calls.push(`${init?.method ?? "GET"} ${new URL(url).pathname}`);
      return new Response("{}");
    },
  };
  let route: string | undefined;
  installTransportGuard(
    client,
    "X-AI-Consent-Token",
    "openai",
    (token, method, path) => {
      assert.equal(token, "act.jwt.token");
      const candidate = `${method} ${path}`;
      routes.push(candidate);
      route ??= candidate;
      return route === candidate;
    },
  );

  await client.fetch("https://provider.test/models", {
    method: "GET",
    headers: { "X-AI-Consent-Token": "act.jwt.token" },
  });
  await client.fetch("https://provider.test/v1/chat/completions", {
    method: "POST",
    headers: { "X-AI-Consent-Token": "act.jwt.token" },
  });

  assert.deepEqual(routes, ["POST /v1/chat/completions"]);
  assert.deepEqual(calls, ["GET /models", "POST /v1/chat/completions"]);
});

test("the net's message names the method and the path, and nothing else", async () => {
  const probe = probeFetch();
  const client = wrap(openaiClient(probe), {
    consentClient: fakeConsentClient(),
    provider: "openai",
  });

  const error = await client.embeddings
    .create({ model: "text-embedding-3-small", input: "a private sentence" })
    .catch((e: unknown) => e);
  const refusal = unenforced(error)!;

  assert.match(refusal.message, /POST/);
  assert.match(refusal.message, /\/v1\/embeddings/);
  assert.ok(
    !refusal.message.includes("a private sentence"),
    "the net never reads the body",
  );
  assert.ok(
    !refusal.message.includes("sk-test"),
    "the net never reads credentials",
  );
  assert.ok(
    !refusal.message.includes("?"),
    "the net never reads the query string",
  );
  assert.match(refusal.message, /unwrapped/i, "it says what to do instead");
});

test("a non-AI GET is neither authorized nor refused", async () => {
  const probe = probeFetch();
  const cc = fakeConsentClient();
  const client = wrap(openaiClient(probe), {
    consentClient: cc,
    provider: "openai",
  });

  const error = await client.models.list().catch((e: unknown) => e);

  assert.equal(
    unenforced(error),
    undefined,
    "GET carries no prompt: the net must let it through",
  );
  assert.equal(
    cc.calls.length,
    0,
    "models.list() has no model and no user: it must not hit /v1/authorize",
  );
  assert.equal(probe.calls.length, 1, "the request did go out");
  assert.equal(probe.calls[0]!.method, "GET");
});

test("a DELETE on the management surface is let through", async () => {
  const probe = probeFetch();
  const cc = fakeConsentClient();
  const client = wrap(openaiClient(probe), {
    consentClient: cc,
    provider: "openai",
  });

  const error = await client.models
    .delete("ft:gpt-4o:acme:1")
    .catch((e: unknown) => e);

  assert.equal(unenforced(error), undefined);
  assert.equal(probe.calls.length, 1);
  assert.equal(probe.calls[0]!.method, "DELETE");
});

test("a client with no discoverable fetch gets no net, and no error", async () => {
  const calls: unknown[] = [];
  const messages = {
    create(body: Record<string, unknown>) {
      calls.push(body);
      return Promise.resolve({ id: "msg_1" });
    },
  };
  const cc = fakeConsentClient();
  const client = wrap(
    { messages },
    { consentClient: cc, provider: "anthropic" },
  );

  await client.messages.create({
    model: "claude-3",
    messages: [],
    user_id: "usr_marie_456",
  });
  assert.equal(calls.length, 1);
});

// ── layer 4: derived clients ─────────────────────────────────────────────────

test("withOptions() hands back a client that is still enforced", async () => {
  const probe = probeFetch();
  const cc = fakeConsentClient();
  const client = wrap(openaiClient(probe), {
    consentClient: cc,
    provider: "openai",
  });

  const derived = client.withOptions({ maxRetries: 0 });

  await assert.rejects(
    derived.chat.completions.create({ model: "gpt-4o", messages: [] }),
    MissingIdentityError,
    "the derived client must demand an identity like its parent",
  );
  assert.equal(probe.calls.length, 0, "no HTTP request may leave");

  await assert.rejects(
    derived.chat.completions.create({
      model: "gpt-4o",
      messages: [],
      user_id: "usr_marie_456",
    } as never),
  );
  assert.equal(
    cc.calls.length,
    1,
    "and it must authorize through the same consent client",
  );
  assert.equal(probe.calls[0]!.token, "act.jwt.token");
});

test("a derived client keeps its aliases, its net, and its own withOptions()", async () => {
  const probe = probeFetch();
  const cc = fakeConsentClient();
  const client = wrap(openaiClient(probe), {
    consentClient: cc,
    provider: "openai",
  });

  const grandchild = client.withOptions({}).withOptions({});

  await assert.rejects(
    grandchild.chat.completions.parse({
      model: "gpt-4o",
      messages: [],
    } as never),
    MissingIdentityError,
  );
  const error = await grandchild.embeddings
    .create({ model: "text-embedding-3-small", input: "x" })
    .catch((e) => e);
  assert.ok(unenforced(error), "the net rides along to derived clients");
  assert.equal(probe.calls.length, 0, "no HTTP request may leave");
});

// ── the net on Mistral (a different transport shape) ─────────────────────────

test("the net reaches Mistral's transport when the app supplies an HTTPClient", async () => {
  const { Mistral, HTTPClient } = await import("@mistralai/mistralai");
  const calls: string[] = [];
  const fetcher = async (input: Request): Promise<Response> => {
    calls.push(input.method);
    throw new Error("PROBE: an HTTP request left the process");
  };
  // pragma: allowlist secret
  const raw = new Mistral({
    apiKey: "sk-mistral-test",
    httpClient: new HTTPClient({ fetcher }),
  });
  const client = wrap(raw, {
    consentClient: fakeConsentClient(),
    provider: "mistral",
  });

  // Mistral's SDK calls `fetcher(request)` with a single Request — the other
  // call shape the guard has to understand.
  const error = await client.embeddings
    .create({ model: "mistral-embed", inputs: ["a private sentence"] })
    .catch((e: unknown) => e);

  assert.ok(
    unenforced(error),
    `expected an UnenforcedCallError, got ${String(error)}`,
  );
  assert.equal(calls.length, 0, "NO HTTP request may leave");
});

// ── credential routing keys on the method path, not the wire id ──────────────

test("a routed alias lands on the SAME method of the alternative client", async () => {
  const seen: string[] = [];
  // `Record<string, unknown>` rather than `unknown`: a body type of `unknown`
  // and the identity intersect to the identity ALONE, so the fake would reject
  // the `model` a real provider accepts.
  const resource = () => ({
    create(_b: Record<string, unknown>, _o?: unknown) {
      seen.push("create");
      return Promise.resolve({ id: "msg_1" });
    },
    parse(_b: Record<string, unknown>, _o?: unknown) {
      seen.push("parse");
      return Promise.resolve({ id: "msg_1" });
    },
  });
  const alternative = { messages: resource() };
  const consentClient: ConsentClient = {
    async authorize(): Promise<ConsentDecision> {
      return { ...DECISION, credentialPosture: "no_training" };
    },
  };
  const client = wrap(
    { messages: resource() },
    {
      consentClient,
      provider: "anthropic",
      credentials: { no_training: alternative },
    },
  );

  await client.messages.parse({
    model: "claude-3",
    messages: [],
    user_id: "usr_marie_456",
  });

  // `parse` shares `messages.create`'s wire id; routing must still call parse().
  assert.deepEqual(
    seen,
    ["parse"],
    "a routed parse() must not be downgraded to create()",
  );
});

// ── what `net=` in the journal is allowed to mean ────────────────────────────
//
// One field answers "is this client covered?", and three facts have to stay
// apart inside it: the search finished and the transport is guarded; the search
// finished and there was nothing to guard; the search did not finish, or what it
// found would not take the patch — so a route out of this client is, or may be,
// unguarded. The last one used to be told as one of the first two.

test("a guarded transport reports a complete net", () => {
  const client = { fetch: async () => new Response("{}") };
  assert.equal(installTransportGuard(client, "X-AI-Consent-Token"), true);
});

test("a client with nothing to guard says so, and does not say `partial`", () => {
  assert.equal(
    installTransportGuard({ messages: {} }, "X-AI-Consent-Token"),
    false,
  );
});

test("a search cut short by its node budget never reports a complete net", async () => {
  // A client fat enough that the walk gives up before reaching the transport,
  // which here sits one hop in — Mistral's shape, behind 300 siblings. Saying
  // `false` there says "there is nothing to guard" about a client that has a
  // live route out; the last two lines are that route, used.
  const filler: Record<string, unknown> = {};
  for (let i = 0; i < 300; i++) filler[`resource_${i}`] = { i };
  let sent = 0;
  const transport = { fetch: async () => (sent++, new Response("{}")) };
  const client = { ...filler, _options: transport };

  assert.equal(installTransportGuard(client, "X-AI-Consent-Token"), "partial");

  await transport.fetch();
  assert.equal(sent, 1, "the test no longer demonstrates an unguarded route");
});

test("a transport that cannot be replaced makes wrap() refuse the client", async () => {
  const client = { fetch: async () => new Response("{}") };
  Object.freeze(client);

  assert.equal(installTransportGuard(client, "X-AI-Consent-Token"), "partial");

  const cc = fakeConsentClient();
  const messages = { create: async (body: Record<string, unknown>) => body };
  const frozenClient = Object.freeze({
    messages,
    fetch: async () => new Response("{}"),
  });
  const original = messages.create;

  assert.throws(
    () => wrap(frozenClient, { consentClient: cc, provider: "anthropic" }),
    UnsupportedProviderError,
  );
  assert.equal(
    messages.create,
    original,
    "call sites stay untouched when construction fails",
  );
  assert.equal(cc.calls.length, 0);
});

// ── every transport, not the first one found ─────────────────────────────────
//
// The walk used to return the moment it found a transport, and to call that a
// finished search. A client that owns two routes out then had one guarded, one
// wide open, and `net=true` said it was covered. The Python net was corrected
// for exactly this — `google-genai` holds a blocking and an async transport on
// one client — and the two SDKs have to answer alike or the parity check is
// comparing a fixed net against a broken one.

function twoRoutesOut(): {
  client: {
    fetch(...a: unknown[]): Promise<Response>;
    _options: { httpClient: { fetcher(...a: unknown[]): Promise<Response> } };
  };
  sent: () => [number, number];
} {
  let viaFetch = 0;
  let viaFetcher = 0;
  return {
    client: {
      fetch: async (..._a: unknown[]) => (viaFetch++, new Response("{}")),
      _options: {
        httpClient: {
          fetcher: async (..._a: unknown[]) => (
            viaFetcher++,
            new Response("{}")
          ),
        },
      },
    },
    sent: () => [viaFetch, viaFetcher],
  };
}

const POST: [string, { method: string }] = [
  "https://api.example.test/v1/chat",
  { method: "POST" },
];

/**
 * Whatever came out of a call that must not go through, thrown or rejected.
 *
 * The guard throws SYNCHRONOUSLY even over an async transport, which is
 * deliberate — a refusal that arrived as a rejection is a refusal a provider
 * library's own `.catch()` can swallow, and this one is not allowed to be
 * swallowed. Provider libraries call `fetch` from inside an async function, so
 * it reaches their caller as a rejection anyway; a direct call sees the throw.
 */
async function refused(call: () => unknown): Promise<unknown> {
  try {
    return await call();
  } catch (error: unknown) {
    return error;
  }
}

test("every transport a client owns is guarded, not the first one found", async () => {
  const { client, sent } = twoRoutesOut();

  assert.equal(installTransportGuard(client, "X-AI-Consent-Token"), true);

  assert.ok(
    unenforced(await refused(() => client.fetch(...POST))),
    "the transport on the client itself must refuse",
  );

  const nested = await refused(() =>
    client._options.httpClient.fetcher(...POST),
  );
  assert.ok(
    unenforced(nested),
    "the SECOND route out was left unguarded, and reported as covered",
  );

  assert.deepEqual(sent(), [0, 0], "no request may leave through either route");
});

test("a client with two routes out, one of them sealed, reports `partial`", () => {
  // Guarding what can be guarded and saying so. Reporting `true` here would
  // claim the sealed route is covered; refusing the client outright would trade
  // a partial net for none.
  const { client } = twoRoutesOut();
  const original = client.fetch;
  Object.freeze(client._options.httpClient);

  assert.equal(installTransportGuard(client, "X-AI-Consent-Token"), "partial");
  assert.equal(
    client.fetch,
    original,
    "the successful sibling patch must be rolled back",
  );
});

test("partial rollback keeps a previous guard on a shared transport", async () => {
  const shared = { fetch: async (..._args: unknown[]) => new Response("{}") };
  assert.equal(installTransportGuard(shared, "X-AI-Consent-Token"), true);
  const previousGuard = shared.fetch;
  const sealed = { fetcher: async () => new Response("{}") };
  Object.freeze(sealed);

  assert.equal(
    installTransportGuard(
      { shared, sealed },
      "X-AI-Consent-Token",
      "openai",
      () => true,
    ),
    "partial",
  );
  assert.equal(shared.fetch, previousGuard);
  assert.ok(
    unenforced(
      await refused(() =>
        shared.fetch("https://api.example.test/v1/chat", {
          method: "POST",
          headers: { "X-AI-Consent-Token": "forged" },
        }),
      ),
    ),
  );
});

test("a refusal on one transport rolls back the patches after it", async () => {
  const { client, sent } = twoRoutesOut();
  const original = client._options.httpClient.fetcher;
  Object.freeze(client);

  assert.equal(installTransportGuard(client, "X-AI-Consent-Token"), "partial");
  assert.equal(client._options.httpClient.fetcher, original);

  const nested = await refused(() =>
    client._options.httpClient.fetcher(...POST),
  );
  assert.ok(nested instanceof Response);
  assert.deepEqual(sent(), [0, 1]);
});

test("a transport whose owner swallows the write is reported, not believed", async () => {
  // Nothing throws here: the `set` trap accepts the patch and drops it. That is
  // the one failure a `try`/`catch` around the write cannot see, and the reason
  // the write is read back instead of trusted.
  const target = { fetch: async (..._a: unknown[]) => new Response("{}") };
  const owner = new Proxy(target, { set: () => true });

  assert.equal(
    installTransportGuard({ inner: owner }, "X-AI-Consent-Token"),
    "partial",
  );

  // And what makes `partial` the honest word rather than a precaution: this
  // route out is open. `true` would have described the net as covering the very
  // transport it failed to touch.
  assert.ok(
    (await refused(() => target.fetch(...POST))) instanceof Response,
    "the test no longer demonstrates an unpatched route",
  );
});

// ── what the net reads, and what it refuses to read ──────────────────────────
//
// Invariant #3 draws the line tightly: the method, the URL PATH, and one named
// header whose value is compared against a private permit. Never the body,
// never the query string, never any other header. Everything below asserts one
// side of that line — either that the net still refuses, or that it refused
// without learning anything it is not allowed to know.

test("the token is found in header tuples, the third shape fetch accepts", async () => {
  // `fetch(url, { headers: [["k", "v"]] })` is legal and some transports build
  // headers that way. A net that only understood objects and `Headers` would
  // have refused every authorized call made through one.
  const client: Record<string, unknown> = {
    fetch: async () => new Response("{}"),
  };
  installTransportGuard(
    client,
    "X-AI-Consent-Token",
    "openai",
    (token) => token === "act.jwt.token",
  );
  const call = client.fetch as (u: string, i: RequestInit) => Promise<Response>;

  const allowed = await refused(() =>
    call("https://api.example.test/v1/chat", {
      method: "POST",
      headers: [["x-ai-consent-token", "act.jwt.token"]],
    }),
  );
  assert.ok(allowed instanceof Response, "an authorized call in tuple form was refused");

  assert.ok(
    unenforced(
      await refused(() =>
        call("https://api.example.test/v1/chat", {
          method: "POST",
          headers: [["x-trace-id", "trace_42"], ["x-ai-consent-token", "act.forged.token"]],
        }),
      ),
    ),
    "a tuple carrying a token with no provenance must still be refused",
  );

  assert.ok(
    unenforced(
      await refused(() =>
        call("https://api.example.test/v1/chat", {
          method: "POST",
          headers: [["x-trace-id", "trace_42"], "not-a-tuple" as unknown as [string, string]],
        }),
      ),
    ),
    "a malformed tuple list is not a token",
  );
});

test("a headers object whose get() throws is read as a plain object instead", async () => {
  // Duck-typing on `.get` is what lets a `Headers` from another realm work. A
  // thing that merely HAS a `get` is not one, and the fallback is what stops it
  // being read as "no token at all" on an authorized call.
  const client: Record<string, unknown> = {
    fetch: async () => new Response("{}"),
  };
  installTransportGuard(
    client,
    "X-AI-Consent-Token",
    "openai",
    (token) => token === "act.jwt.token",
  );
  const call = client.fetch as (u: string, i: RequestInit) => Promise<Response>;

  const headers = {
    get(): never {
      throw new TypeError("not a Headers after all");
    },
    "X-AI-Consent-Token": "act.jwt.token",
  };

  const allowed = await refused(() =>
    call("https://api.example.test/v1/chat", {
      method: "POST",
      headers: headers as unknown as RequestInit["headers"],
    }),
  );
  assert.ok(allowed instanceof Response);
});

test("a request with no method at all is a GET, and passes", async () => {
  // `fetch(url)` defaults to GET. Treating an absent method as a body-bearing
  // call would refuse every retrieval a provider client makes.
  const client: Record<string, unknown> = {
    fetch: async () => new Response("{}"),
  };
  installTransportGuard(client, "X-AI-Consent-Token");
  const call = client.fetch as (u: string) => Promise<Response>;

  assert.ok((await refused(() => call("https://api.example.test/v1/models"))) instanceof Response);
});

test("an unparsable URL is refused with no detail echoed back", async () => {
  // The refusal message goes into somebody's logs. A URL that will not parse
  // yields "(unknown path)" rather than a best-effort echo of a string that may
  // hold credentials.
  const client: Record<string, unknown> = {
    fetch: async () => new Response("{}"),
  };
  installTransportGuard(client, "X-AI-Consent-Token");
  const call = client.fetch as (u: unknown, i: RequestInit) => Promise<Response>;

  const secretish = "not-a-url?api_key=sk-should-never-appear"; // pragma: allowlist secret
  const error = unenforced(await refused(() => call(secretish, { method: "POST" })));

  assert.ok(error);
  assert.match(String(error), /POST \(unknown path\)/);
  assert.ok(!String(error).includes("sk-should-never-appear"));
});

test("a URL that cannot even be stringified is refused just as blindly", async () => {
  // Reading the path must never be able to fail open. An object whose own
  // `toString` throws is the shortest way to prove the refusal does not depend
  // on the URL being readable at all.
  const client: Record<string, unknown> = {
    fetch: async () => new Response("{}"),
  };
  installTransportGuard(client, "X-AI-Consent-Token");
  const call = client.fetch as (u: unknown, i: RequestInit) => Promise<Response>;
  const unreadable = {
    toString(): never {
      throw new Error("this URL refuses to be read");
    },
  };

  const error = unenforced(await refused(() => call(unreadable, { method: "POST" })));

  assert.ok(error);
  assert.match(String(error), /POST \(unknown path\)/);
});

test("the query string never reaches the refusal message", async () => {
  // It is request content and it can carry identifiers. Dropped rather than
  // trimmed for display.
  const client: Record<string, unknown> = {
    fetch: async () => new Response("{}"),
  };
  installTransportGuard(client, "X-AI-Consent-Token");
  const call = client.fetch as (u: string, i: RequestInit) => Promise<Response>;

  const error = unenforced(
    await refused(() =>
      call("https://api.example.test/v1/chat?user=usr_marie_456", { method: "POST" }),
    ),
  );

  assert.ok(error);
  assert.match(String(error), /POST \/v1\/chat /);
  assert.ok(!String(error).includes("usr_marie_456"));
});

// ── the @google/genai request boundary ───────────────────────────────────────

class ApiClient {
  async request(_r: unknown): Promise<Response> {
    return new Response("{}");
  }
  async requestStream(_r: unknown): Promise<Response> {
    return new Response("{}");
  }
  async apiCall(_url: string, _init?: RequestInit): Promise<Response> {
    return new Response("{}");
  }
}

function googleGenAI(apiClient: unknown): object {
  class GoogleGenAI {
    apiClient = apiClient;
  }
  return new GoogleGenAI();
}

test("a direct gemini apiCall that carries a body and no token is refused", async () => {
  // apiCall is where @google/genai hands a real URL and RequestInit to the
  // global fetch. request() and requestStream() both end there, and so does a
  // caller who skips them: the net judges the request where it exists.
  const client = googleGenAI(new ApiClient());
  assert.equal(installTransportGuard(client, "X-AI-Consent-Token", "gemini"), true);
  const apiClient = (client as { apiClient: ApiClient }).apiClient;

  const error = unenforced(
    await refused(() =>
      apiClient.apiCall("https://generativelanguage.googleapis.com/v1beta/models/x:generateContent", {
        method: "POST",
        body: "{}",
      }),
    ),
  );
  assert.ok(error);
  assert.match(String(error), /POST \/v1beta\/models\/x:generateContent /);
});
test("a gemini GET is let through, like any other retrieval", async () => {
  const client = googleGenAI(new ApiClient());
  installTransportGuard(client, "X-AI-Consent-Token", "gemini");
  const apiClient = (client as { apiClient: ApiClient }).apiClient;

  const allowed = await refused(() =>
    apiClient.apiCall("https://generativelanguage.googleapis.com/v1beta/models", { method: "GET" }),
  );
  assert.ok(allowed instanceof Response, "a bodyless gemini call must not be refused");
});
test("a gemini URL that will not parse yields no detail either", async () => {
  const client = googleGenAI(new ApiClient());
  installTransportGuard(client, "X-AI-Consent-Token", "gemini");
  const apiClient = (client as { apiClient: ApiClient }).apiClient;

  const error = unenforced(await refused(() => apiClient.apiCall("http://", { method: "POST", body: "{}" })));
  assert.ok(error);
  assert.match(String(error), /POST \(unknown path\)/);
});
test("a gemini client with no apiClient is partial, never silently uncovered", async () => {
  // `complete: false` and not `found: []`. Reporting "nothing to guard" here
  // would hand back `net=false` for a client whose whole transport the walk
  // simply could not see, and wrap() would accept it.
  for (const apiClient of [undefined, null, "not an object", 42]) {
    assert.equal(
      installTransportGuard(googleGenAI(apiClient), "X-AI-Consent-Token", "gemini"),
      "partial",
      String(apiClient),
    );
  }
});

test("a gemini client without apiCall is partial: nothing would judge its requests", () => {
  class NoDispatch {
    async request(_r: unknown): Promise<Response> {
      return new Response("{}");
    }
    async requestStream(_r: unknown): Promise<Response> {
      return new Response("{}");
    }
  }
  assert.equal(installTransportGuard(googleGenAI(new NoDispatch()), "X-AI-Consent-Token", "gemini"), "partial");
});
test("gemini apiCall: an authorized request passes the full check and is told not to follow redirects", async () => {
  const inits: RequestInit[] = [];
  class Recording extends ApiClient {
    override async apiCall(_url: string, init?: RequestInit): Promise<Response> {
      inits.push(init!);
      return new Response("{}");
    }
  }
  const client = googleGenAI(new Recording());
  const checks: unknown[][] = [];
  installTransportGuard(client, "X-AI-Consent-Token", "gemini", (...args: unknown[]) => (checks.push(args), args[0] === "act.jwt.token"));
  const apiClient = (client as { apiClient: ApiClient }).apiClient;
  const url = "https://gemini.invalid/v1beta/models/x:generateContent?alt=sse";

  await apiClient.apiCall(url, { method: "POST", body: "{}", headers: new Headers({ "x-ai-consent-token": "act.jwt.token" }) });
  assert.ok(unenforced(await refused(() => apiClient.apiCall(url, { method: "POST", body: "{}", headers: { "X-AI-Consent-Token": "forged" } }))));
  await apiClient.apiCall("https://gemini.invalid/v1beta/models", { method: "GET", headers: new Headers() });

  assert.deepEqual(inits.map((init) => [init.method, init.redirect]), [["POST", "manual"], ["GET", undefined]]);
  assert.deepEqual(checks[0], ["act.jwt.token", "POST", "/v1beta/models/x:generateContent", "https://gemini.invalid"]);
});
test("an apiCall that is not a function is not a transport", async () => {
  class NotQuiteApiClient {
    apiCall = "soon";
  }

  assert.equal(
    installTransportGuard(googleGenAI(new NotQuiteApiClient()), "X-AI-Consent-Token", "gemini"),
    "partial",
  );
});
test("the gemini boundary is only looked for on a GoogleGenAI client", () => {
  // The search is keyed on the class name, so a client that is not one is not
  // partially covered — it simply has no such boundary.
  class NotGoogle {
    apiClient = new ApiClient();
  }

  assert.equal(installTransportGuard(new NotGoogle(), "X-AI-Consent-Token", "gemini"), false);
});

test("a client with no prototype at all is not mistaken for a named class", () => {
  const bare = Object.create(null) as object;

  assert.equal(installTransportGuard(bare, "X-AI-Consent-Token", "gemini"), false);
});

test("a client whose constructor is not a function is not a named class either", () => {
  const impostor = Object.create({ constructor: "GoogleGenAI" }) as object;

  assert.equal(installTransportGuard(impostor, "X-AI-Consent-Token", "gemini"), false);
});

// ── the NextGen surfaces, and the shapes that resist being guarded ───────────
//
// Each surface builds its own generated client on demand, so what the net is
// installed on is the factory rather than the transport. These probe the ends
// of that: a client that will not take the shadow, a surface that will not take
// the patch, a generated client with no transport at all. Every one of them is
// `partial` or a refusal, and never a claim of coverage.

/** The shape a generated Speakeasy client has: an HTTPClient with a `fetcher`. */
function speakeasySdk(): { _httpClient: { fetcher: (r: Request) => Promise<Response> } } {
  return { _httpClient: { fetcher: async (_r: Request) => new Response("{}") } };
}

/** A surface: `parentClient`, a cached SDK, and `getClient()` on its prototype. */
class NextGenSurface {
  readonly sdk = speakeasySdk();
  getClient(): object {
    return this.sdk;
  }
}

/** A GoogleGenAI stand-in whose NextGen surfaces sit behind accessors, as the real one's do. */
function nextGenClient(
  surfaces: Record<string, unknown>,
  own: Record<string, unknown> = {},
  descriptor: Partial<PropertyDescriptor> = {},
): object {
  class GoogleGenAI {
    apiClient = new ApiClient();
    constructor() {
      for (const [name, surface] of Object.entries(surfaces)) {
        Object.defineProperty(this, name, {
          configurable: true,
          enumerable: false,
          get: () => surface,
          ...descriptor,
        });
      }
      Object.assign(this, own);
    }
  }
  return new GoogleGenAI();
}

test("a second wrap adds its check to the SDK a surface builds, without installing twice", () => {
  const client = nextGenClient({ interactions: new NextGenSurface() });
  for (const _ of [1, 2]) {
    assert.equal(installTransportGuard(client, "X-AI-Consent-Token", "gemini", () => false, {}, []), true);
  }

  const sdk = (client as { interactions: NextGenSurface }).interactions.getClient() as ReturnType<typeof speakeasySdk>;
  assert.equal(provenanceCount(sdk._httpClient.fetcher), 2);
});

test("a surface that names no way to build a client is refused when it is read", () => {
  // Reading it is the last moment before a request goes out on whatever it
  // holds, and this SDK has no way to judge that request.
  const client = nextGenClient({ interactions: {} });
  assert.equal(installTransportGuard(client, "X-AI-Consent-Token", "gemini"), true);

  assert.throws(() => (client as { interactions: unknown }).interactions, UnenforcedCallError);
});

test("a generated client with no transport the net can find is refused, not waved through", () => {
  const client = nextGenClient({ interactions: { getClient: () => ({ nothing: "here" }) } });
  installTransportGuard(client, "X-AI-Consent-Token", "gemini");

  const surface = (client as { interactions: { getClient(): unknown } }).interactions;
  assert.throws(() => surface.getClient(), UnenforcedCallError);
});

test("a surface that will not take the patch is refused, in either way it can refuse it", () => {
  const frozen = Object.freeze(new NextGenSurface());
  // A Proxy that answers "done" to defineProperty and writes nothing: the patch
  // raises nothing and is simply not there, which is why it is read back.
  const swallowing = new Proxy(new NextGenSurface(), { defineProperty: () => true });

  for (const [label, surface] of Object.entries({ frozen, swallowing })) {
    const client = nextGenClient({ interactions: surface });
    installTransportGuard(client, "X-AI-Consent-Token", "gemini");
    assert.throws(() => (client as { interactions: unknown }).interactions, UnenforcedCallError, label);
  }
});

test("a client that will not take the shadow is partial, in either way it can refuse it", () => {
  const cases: Record<string, object> = {
    // Frozen: the marker itself cannot be written.
    frozen: Object.freeze(nextGenClient({ interactions: new NextGenSurface() })),
    // A getter that cannot be redefined.
    sealed: nextGenClient({ interactions: new NextGenSurface() }, {}, { configurable: false }),
    // A Proxy that accepts every definition and keeps none.
    swallowing: new Proxy(nextGenClient({ interactions: new NextGenSurface() }), { defineProperty: () => true }),
  };

  for (const [label, client] of Object.entries(cases)) {
    assert.equal(installTransportGuard(client, "X-AI-Consent-Token", "gemini"), "partial", label);
  }
});

test("a surface and an SDK built before wrap() are guarded where they already are", () => {
  // The library caches both on the instance, so the shadowed getter would never
  // be asked for them again.
  const built = new NextGenSurface();
  const sdk = speakeasySdk();
  const client = nextGenClient({}, { _interactions: built, _nextGenClient: sdk });

  assert.equal(installTransportGuard(client, "X-AI-Consent-Token", "gemini", () => false, {}, []), true);

  assert.equal(provenanceCount(sdk._httpClient.fetcher), 1);
  assert.equal(
    provenanceCount((built.getClient() as ReturnType<typeof speakeasySdk>)._httpClient.fetcher),
    1,
  );
});

test("an SDK built before wrap() that has no transport makes the client partial", () => {
  const client = nextGenClient({}, { _nextGenClient: { nothing: "here" } });

  assert.equal(installTransportGuard(client, "X-AI-Consent-Token", "gemini"), "partial");
});

/** The private marker `guardNextGen` leaves on a client, seen from outside. */
function nextGenInstalled(client: object): boolean {
  return Object.getOwnPropertySymbols(client).some((s) => s.description === "idside.guard.nextgen");
}

test("a surface exposed as a plain property, rather than behind a getter, is guarded too", () => {
  // `accessorOf` finds nothing to shadow when the surface is an own DATA
  // property, and `_interactions` does not exist either — so nothing would have
  // reached the client it builds. The library does not do this today; the point
  // is that the cover must not depend on it continuing not to.
  const surface = new NextGenSurface();
  const client = nextGenClient({}, { interactions: surface });

  assert.equal(installTransportGuard(client, "X-AI-Consent-Token", "gemini", () => false, {}, []), true);

  const sdk = surface.getClient() as ReturnType<typeof speakeasySdk>;
  assert.equal(provenanceCount(sdk._httpClient.fetcher), 1);
});

test("a client refused after its surfaces were shadowed is handed back exactly as it was", () => {
  // The install order puts the NextGen shadows first, so a transport that
  // refuses the patch afterwards has to undo them. A client `wrap()` refused
  // must not be left with accessors — or a factory method — this SDK put there.
  const getNextGenClient = (): object => speakeasySdk();
  const client = nextGenClient({ interactions: new NextGenSurface() }, { getNextGenClient });
  const before = Object.getOwnPropertyDescriptor(client, "interactions");
  // An apiClient that cannot be patched: `apiCall` has nowhere to be written.
  Object.defineProperty(client, "apiClient", { value: Object.freeze(new ApiClient()), configurable: true });

  assert.equal(installTransportGuard(client, "X-AI-Consent-Token", "gemini"), "partial");

  assert.deepEqual(Object.getOwnPropertyDescriptor(client, "interactions"), before);
  assert.equal((client as { getNextGenClient: unknown }).getNextGenClient, getNextGenClient);
  assert.equal(nextGenInstalled(client), false);
});

test("a second wrap refused after the first succeeded takes back only its own check", () => {
  const client = nextGenClient({ interactions: new NextGenSurface() });
  assert.equal(installTransportGuard(client, "X-AI-Consent-Token", "gemini", () => false, {}, []), true);

  // The same client, pointed at a transport that will not take the patch.
  Object.defineProperty(client, "apiClient", { value: Object.freeze(new ApiClient()), configurable: true });
  assert.equal(installTransportGuard(client, "X-AI-Consent-Token", "gemini", () => false, {}, []), "partial");

  // The first install is still there, and alone.
  assert.equal(nextGenInstalled(client), true);
  const sdk = (client as { interactions: NextGenSurface }).interactions.getClient() as ReturnType<typeof speakeasySdk>;
  assert.equal(provenanceCount(sdk._httpClient.fetcher), 1);
});

test("getNextGenClient() is shadowed like a surface, and guards what it returns", () => {
  const sdk = speakeasySdk();
  const client = nextGenClient({}, { getNextGenClient: () => sdk });

  installTransportGuard(client, "X-AI-Consent-Token", "gemini", () => false, {}, []);

  assert.equal((client as { getNextGenClient(): unknown }).getNextGenClient(), sdk);
  assert.equal(provenanceCount(sdk._httpClient.fetcher), 1);
});

// ── the walk, when the client resists being walked ───────────────────────────

test("a node that refuses to list its keys is stepped over, not thrown from", async () => {
  // `wrap()` must not die on a client holding something hostile in a field it
  // never asked about. The walk skips it and keeps looking for the transport
  // that matters — and still reports a complete search, because nothing can be
  // hidden behind an object that will not answer at all.
  const hostile = new Proxy(
    {},
    {
      ownKeys(): never {
        throw new RangeError("this object refuses to be enumerated");
      },
    },
  );
  const client: Record<string, unknown> = {
    hostile,
    fetch: async () => new Response("{}"),
  };

  assert.equal(installTransportGuard(client, "X-AI-Consent-Token"), true);

  const call = client.fetch as (u: string, i: RequestInit) => Promise<Response>;
  assert.ok(unenforced(await refused(() => call(...POST))));
});

test("a client that will not take the patch back is still reported as partial", () => {
  // Rollback is best effort by construction: a Proxy can make perfect
  // restoration impossible. What may never happen is the install then claiming
  // coverage — so the answer stays `partial` and `wrap()` refuses the client.
  const apiClient = new Proxy(new ApiClient(), {
    deleteProperty(): never {
      throw new TypeError("this transport refuses to be restored");
    },
  });
  class GoogleGenAI {
    apiClient = apiClient;
    // Swallows the write: the patch is accepted and dropped, which is what
    // makes the install fail and the rollback run.
    fetch = async (): Promise<Response> => new Response("{}");
  }
  const client = new Proxy(new GoogleGenAI(), { set: () => true });

  assert.equal(installTransportGuard(client, "X-AI-Consent-Token", "gemini"), "partial");
});

test("when a Request and an init both name a method, the init's is used", async () => {
  // `fetch(request, { method })` is legal and the init wins, so reading the
  // Request's method instead would classify the call by the wrong verb — and a
  // POST read as a GET is a POST the net lets through.
  const client: Record<string, unknown> = {
    fetch: async () => new Response("{}"),
  };
  installTransportGuard(client, "X-AI-Consent-Token");
  const call = client.fetch as (r: Request, i: RequestInit) => Promise<Response>;

  const asGet = new Request("https://api.example.test/v1/chat", { method: "GET" });
  assert.ok(
    unenforced(await refused(() => call(asGet, { method: "POST" }))),
    "a POST disguised behind a GET Request must still be refused",
  );

  const asPost = new Request("https://api.example.test/v1/chat", { method: "POST", body: "{}" });
  const allowed = await refused(() => call(asPost, { method: "GET" }));
  assert.ok(allowed instanceof Response, "the init's GET is what fetch will actually send");
});

test("a gemini apiCall with no headers carries no token", async () => {
  const client = googleGenAI(new ApiClient());
  installTransportGuard(client, "X-AI-Consent-Token", "gemini", () => true);
  const apiClient = (client as { apiClient: ApiClient }).apiClient;
  const url = "https://gemini.invalid/v1beta/models/x:generateContent";

  // Nothing to read: there is no header map, so there is no token, whatever the
  // provenance predicate would have said about one.
  assert.ok(unenforced(await refused(() => apiClient.apiCall(url, { method: "POST", body: "{}" }))));

  const allowed = await refused(() =>
    apiClient.apiCall(url, { method: "POST", body: "{}", headers: { "X-AI-Consent-Token": "act.jwt.token" } }),
  );
  assert.ok(allowed instanceof Response, "a provenanced token must pass");
});
// ── installing the net causes nothing to happen ──────────────────────────────
//
// Provider clients expose lazily-constructed resources behind accessors, and
// `wrap()` runs before the application has made a single call. Reading one to
// look for a transport would make wrapping a client cause side effects it has
// no business causing — a connection opened, a token minted, a resource built
// for an endpoint nobody is going to use.

test("wrapping never evaluates a getter on the client", () => {
  let reads = 0;
  const client = {
    get lazyResource(): object {
      reads++;
      return { fetch: async () => new Response("{}") };
    },
    fetch: async (): Promise<Response> => new Response("{}"),
  };

  assert.equal(installTransportGuard(client, "X-AI-Consent-Token"), true);
  assert.equal(reads, 0, "the walk evaluated an accessor");
});

test("a transport behind a getter is not patched, and not pretended about", () => {
  // The consequence has to be stated rather than glossed: an ES private field
  // or an accessor-backed transport is invisible here by construction. Two of
  // the two gemini routes is the condition for coverage; one of them is
  // `partial`, and `wrap()` refuses the client rather than claiming a net.
  let reads = 0;
  class LazyApiClient {
    get apiCall(): (url: string) => Promise<Response> {
      reads++;
      return async () => new Response("{}");
    }
  }

  assert.equal(
    installTransportGuard(googleGenAI(new LazyApiClient()), "X-AI-Consent-Token", "gemini"),
    "partial",
  );
  assert.equal(reads, 0, "the search evaluated an accessor looking for a transport");
});

// ── the server a request goes to, and whether it may be redirected ───────────

test("the provenance check is told the request's origin, and only its origin", async () => {
  const seen: unknown[][] = [];
  const client: Record<string, unknown> = { fetch: async () => new Response("{}") };
  installTransportGuard(client, "X-AI-Consent-Token", "openai", (...args: unknown[]) => {
    seen.push(args);
    return true;
  });
  const call = client.fetch as (u: string, i: RequestInit) => Promise<Response>;

  await call("HTTPS://API.Example.test:443/v1/chat?user=usr_marie_456", {
    method: "POST",
    headers: { "X-AI-Consent-Token": "act.jwt.token" },
  });
  assert.deepEqual(seen, [["act.jwt.token", "POST", "/v1/chat", "https://api.example.test"]]);
});

test("an authorized request in a Request-like shape with no init is still told not to follow redirects", async () => {
  // Not a native Request, so it cannot be copied: the mode travels in an init
  // of its own, which wins over the request it accompanies.
  const received: unknown[][] = [];
  const client: Record<string, unknown> = {
    fetch: async (...args: unknown[]) => (received.push(args), new Response("{}")),
  };
  installTransportGuard(client, "X-AI-Consent-Token", "openai", () => true);
  const requestLike = {
    url: "https://api.example.test/v1/chat",
    method: "POST",
    headers: { "X-AI-Consent-Token": "act.jwt.token" },
  };

  await (client.fetch as (r: unknown) => Promise<Response>)(requestLike);
  assert.deepEqual(received, [[requestLike, { redirect: "manual" }]]);
});

test("a gemini request is checked against the server of its real URL", async () => {
  // Not reconstructed from httpOptions.baseUrl and getBaseUrl(): judged on the
  // URL the library actually built, so whatever moved the server is seen.
  const origins: string[] = [];
  const client = googleGenAI(new ApiClient());
  installTransportGuard(client, "X-AI-Consent-Token", "gemini", (_t, _m, _p, origin) => (origins.push(origin), true));
  const apiClient = (client as { apiClient: ApiClient }).apiClient;
  const init = { method: "POST", body: "{}", headers: { "X-AI-Consent-Token": "act.jwt.token" } };

  await apiClient.apiCall("https://generativelanguage.googleapis.com/v1beta/models/x:generateContent", init);
  await apiClient.apiCall("https://attacker.example/v1beta/models/x:generateContent", init);
  assert.deepEqual(origins, ["https://generativelanguage.googleapis.com", "https://attacker.example"]);
});
// ── a derived client's check goes with the client ────────────────────────────

test("a derived client's provenance check goes when the client does, and a live one stays guarded", async () => {
  // Every derived client shares its parent's fetch and adds its own check to
  // it. Kept forever, `withOptions({ timeout })` per request grows that list,
  // and the time every request spends in the net, without bound.
  const { setFlagsFromString } = await import("node:v8");
  const { runInNewContext } = await import("node:vm");
  setFlagsFromString("--expose-gc");
  const gc = runInNewContext("gc") as () => void;
  const collect = async () => {
    for (let round = 0; round < 6; round++) {
      await new Promise((resolve) => setImmediate(resolve));
      gc();
    }
  };

  let sent = 0;
  const fetch = (async () => (
    sent++,
    new Response(
      JSON.stringify({ id: "chatcmpl_1", object: "chat.completion", created: 0, model: "gpt-4o", choices: [] }),
      { status: 200, headers: { "content-type": "application/json" } },
    )
  )) as typeof globalThis.fetch;
  const raw = new OpenAI({ apiKey: "sk-test", fetch, maxRetries: 0 }); // pragma: allowlist secret
  const client = wrap(raw, { consentClient: fakeConsentClient(), provider: "openai" });
  const transport = raw as unknown as { fetch: unknown };
  const deriveAndDrop = (count: number) => {
    for (let i = 0; i < count; i++) client.withOptions({ timeout: 5_000 });
  };

  // Pruned when a new check is installed.
  deriveAndDrop(200);
  await collect();
  const live = client.withOptions({ timeout: 5_000 });
  assert.ok(provenanceCount(transport.fetch)! <= 10, `after install: ${provenanceCount(transport.fetch)} checks`);

  // Pruned when a request is checked.
  deriveAndDrop(200);
  await collect();
  await client.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" });
  assert.ok(provenanceCount(transport.fetch)! <= 10, `after a request: ${provenanceCount(transport.fetch)} checks`);

  // The derived client still alive is still enforced, and still guarded.
  const create = live.chat.completions.create as unknown as (body: Record<string, unknown>) => Promise<unknown>;
  await create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" });
  assert.equal(sent, 2);
  assert.ok(unenforced(await live.embeddings.create({ model: "text-embedding-3-small", input: "x" }).catch((e: unknown) => e)));
  assert.equal(sent, 2);
});

// ── one normaliser, for both halves of the comparison ────────────────────────

test("the origin a client is bound to and the origin a request is judged by are read by the same function", () => {
  // Two copies of this used to exist, one per half: `consent.ts` froze a
  // client's origin with one, `guard.ts` read an outgoing request's with
  // another. They agreed on the day they were written, which is the only day
  // that matters less than every day after it — two normalisers that drift
  // either accept a host nobody wrapped or refuse the client's own traffic.
  const vectors: [unknown, string | undefined][] = [
    ["https://api.openai.com/v1", "https://api.openai.com"],
    ["HTTPS://API.OpenAI.com:443/v1", "https://api.openai.com"],
    ["http://api.openai.com:443/v1", "http://api.openai.com:443"],
    ["https://api.openai.com/v1?key=sk-secret", "https://api.openai.com"],
    [new URL("https://eu.api.openai.com/v1"), "https://eu.api.openai.com"],
    ["data:text/plain,hello", undefined], // an opaque origin names no server
    ["not-a-url", undefined],
    [42, undefined],
    [undefined, undefined],
  ];

  for (const [value, expected] of vectors) {
    assert.equal(originOf(value), expected, `originOf(${String(value)})`);
    // The net's half differs in one thing only, deliberately: it has a message
    // to write, so it says so in words rather than in `undefined`.
    assert.equal(requestOrigin(value), expected ?? UNKNOWN_ORIGIN, `requestOrigin(${String(value)})`);
  }
});

test("neither half of the comparison parses a URL of its own again", () => {
  const src = join(import.meta.dirname, "..", "src");
  for (const file of ["guard.ts", "consent.ts"]) {
    const source = readFileSync(join(src, file), "utf8");
    assert.ok(source.includes('from "./destination.ts"'), `${file} must read a destination through destination.ts`);
    assert.ok(
      !/new URL\(/.test(source),
      `${file} parses a URL of its own again: a second normaliser is how the two halves drift apart`,
    );
  }
});
