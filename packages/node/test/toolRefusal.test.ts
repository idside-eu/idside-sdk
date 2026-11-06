/**
 * Refusing agentic tools — the one lever that does not depend on a provider.
 *
 * Every provider's zero-retention arrangement excludes agentic features while
 * the API itself does not block them. A call that hands the model tools
 * therefore falls outside the retention regime the user was promised, and the
 * SDK is the only component that can see it happening: it is the only one that
 * sees the outgoing request.
 *
 * That is also the constraint on the design. The cloud must never be told
 * whether a given call carried tools — it would be learning something about the
 * request, and the product's whole claim is that it cannot. So the cloud answers
 * from the consent state alone ("this user refuses agentic tools") and the SDK
 * decides locally whether that applies to the call in front of it.
 *
 * Mirrors packages/python/tests/test_tool_refusal.py.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { wrap } from "../src/consent.ts";
import { GenericAdapter, GeminiAdapter } from "../src/adapters/index.ts";
import { loadDescriptors } from "../src/descriptors.ts";
import { ConsentTokenError, ToolUseRefusedError } from "../src/exceptions.ts";
import type { AuthorizeRequest, ConsentClient, ConsentDecision } from "../src/transport.ts";

const OPENAI = loadDescriptors().get("openai")!;
const GEMINI = loadDescriptors().get("gemini")!;

const TOOLS = [{ type: "function", function: { name: "lookup_patient_record" } }];

function decision(constraints: Record<string, unknown>): ConsentDecision {
  return {
    consentState: { agentic_tools: false },
    paramsForced: {},
    actToken: "act.jwt.token",
    source: "widget",
    legalBasis: "consent",
    binding: "user_id",
    constraints,
  };
}

function fakeConsentClient(d: ConsentDecision): ConsentClient & { calls: AuthorizeRequest[] } {
  const calls: AuthorizeRequest[] = [];
  return {
    calls,
    async authorize(request: AuthorizeRequest): Promise<ConsentDecision> {
      calls.push(request);
      return d;
    },
  };
}

function fakeOpenAI() {
  const calls: { body: Record<string, unknown> }[] = [];
  const create = async (body: Record<string, unknown>) => {
    calls.push({ body });
    return { id: "resp_1" };
  };
  return { client: { chat: { completions: { create } }, responses: { create } }, calls };
}

// ── The adapter-level mechanism ────────────────────────────────────────────

test("no constraint leaves the call exactly as it was", () => {
  const params = { model: "gpt-4o", tools: TOOLS };
  assert.deepEqual(new GenericAdapter(OPENAI).applyConstraints(params, {}), params);
});

test("a refusal does nothing to a call that carries no tools", () => {
  // The user refused agentic tools, not AI. Most calls are unaffected.
  const params = { model: "gpt-4o", messages: [] };
  assert.deepEqual(new GenericAdapter(OPENAI).applyConstraints(params, { tools: "refuse" }), params);
});

test("a refusal stops a call that carries tools", () => {
  assert.throws(
    () => new GenericAdapter(OPENAI).applyConstraints({ model: "gpt-4o", tools: TOOLS }, { tools: "refuse" }),
    ToolUseRefusedError,
  );
});

test("strip removes the key rather than emptying it", () => {
  // Several providers reject an empty tools list, and "no tools" is what the
  // user asked for, not "zero tools".
  const out = new GenericAdapter(OPENAI).applyConstraints({ model: "gpt-4o", tools: TOOLS }, { tools: "strip" });

  assert.ok(!("tools" in out));
  assert.equal(out.model, "gpt-4o");
});

test("strip does not mutate the caller's own object", () => {
  const params = { model: "gpt-4o", tools: TOOLS };
  new GenericAdapter(OPENAI).applyConstraints(params, { tools: "strip" });
  assert.deepEqual(params.tools, TOOLS);
});

test("an unknown constraint is not treated as no constraint", () => {
  // Guessing is the one failure mode this whole path exists to prevent.
  assert.throws(
    () => new GenericAdapter(OPENAI).applyConstraints({ model: "gpt-4o", tools: TOOLS }, { tools: "quarantine" }),
    ConsentTokenError,
  );
});

test("a provider whose tools the SDK cannot see fails closed", () => {
  // A refusal we cannot apply is not a refusal we may ignore.
  const blind = { ...OPENAI, toolsParam: undefined };
  assert.throws(
    () => new GenericAdapter(blind).applyConstraints({ tools: TOOLS }, { tools: "refuse" }),
    /does not say where tools travel/,
  );
});

// ── Every field that can carry a tool, not only `tools` ────────────────────
//
// The descriptor names, per call site, each argument through which a call can
// hand the model a tool. What counts as carrying one depends on the kind of
// argument, and the same rules hold in the Python SDK.

test("an empty definitions list or object carries no tool; any other value does, even a falsy one", () => {
  // The same rule in both SDKs, and the safe way round: only a value that is
  // clearly nothing is nothing. `false`, `0` or `""` where tools belong is a
  // malformed call, and a refusal is the answer that cannot be wrong.
  const adapter = new GenericAdapter(OPENAI);
  for (const absent of [[], {}, null, undefined]) {
    const params = { model: "gpt-4o", tools: absent, functions: [] };
    assert.deepEqual(adapter.applyConstraints(params, { tools: "refuse" }, "chat.completions.create"), params);
  }
  for (const present of [false, 0, "", { type: "function" }, "lookup_patient_record"]) {
    assert.throws(
      () => adapter.applyConstraints({ tools: present }, { tools: "refuse" }, "chat.completions.create"),
      ToolUseRefusedError,
      JSON.stringify(present),
    );
  }
});
test("the fields read are the called endpoint's own", () => {
  // `previous_response_id` means stored context on /v1/responses and nothing on
  // chat completions, where the provider rejects it anyway.
  const adapter = new GenericAdapter(OPENAI);
  const params = { model: "gpt-4o", previous_response_id: "resp_medhelp_1" };

  assert.deepEqual(adapter.applyConstraints(params, { tools: "refuse" }, "chat.completions.create"), params);
  assert.throws(
    () => adapter.applyConstraints(params, { tools: "refuse" }, "responses.create"),
    /`previous_response_id`/,
  );
});

test("without a known endpoint, every endpoint's fields are read", () => {
  // Reading fewer fields is how a refusal gets past, so an adapter asked
  // without a call site reads all of them.
  const adapter = new GenericAdapter(OPENAI);
  const params = { model: "gpt-4o", previous_response_id: "resp_medhelp_1" };
  for (const callSite of [undefined, "responses.retrieve"]) {
    assert.throws(() => adapter.applyConstraints(params, { tools: "refuse" }, callSite), ToolUseRefusedError);
  }
  assert.throws(
    () => adapter.applyConstraints({ web_search_options: {} }, { tools: "refuse" }),
    /`web_search_options`/,
  );
});

test("an input item is a tool by its type, read through toJSON() when it is typed", () => {
  const adapter = new GenericAdapter(OPENAI);
  class TypedItem {
    toJSON() {
      return { type: "mcp_approval_response", approve: true };
    }
  }
  assert.throws(
    () => adapter.applyConstraints({ input: [new TypedItem()] }, { tools: "refuse" }, "responses.create"),
    /`input`/,
  );
  const plain = { input: [{ type: "message", role: "user", content: "hi" }, null, "text", { role: "user" }] };
  assert.deepEqual(adapter.applyConstraints(plain, { tools: "refuse" }, "responses.create"), plain);
});

test("strip removes every definitions field and refuses what cannot be removed", () => {
  const adapter = new GenericAdapter(OPENAI);
  const params = { model: "gpt-4o", tools: TOOLS, functions: [{ name: "lookup_patient_record" }] };

  const out = adapter.applyConstraints(params, { tools: "strip" }, "chat.completions.create");
  assert.deepEqual(out, { model: "gpt-4o" });
  assert.deepEqual(params.tools, TOOLS, "the caller's object is left alone");

  // An enabler under strip names itself, even when definitions came first.
  assert.throws(
    () =>
      adapter.applyConstraints(
        { ...params, web_search_options: {} },
        { tools: "strip" },
        "chat.completions.create",
      ),
    (error: Error) => error instanceof ToolUseRefusedError && /`web_search_options`/.test(error.message),
  );
});

// ── One message, true whoever refused ───────────────────────────────────────
//
// It used to end on "run the Preference Center so the user can answer", which
// sends an organization's developer to a widget that does not exist for its
// employees: there, a policy refused, and no answer lifts it. The SDK does not
// know which happened (invariant #1), so the message says what is true in every
// case. Python's says it word for word, and the parity script compares the two.
// "Retry without them" — the fields it names — and not "without the tools": a
// developer told that about `conversation` drops `tools`, keeps the reference,
// and is refused again.

test("the refusal names every field it read and points at no widget", () => {
  const adapter = new GenericAdapter(OPENAI);
  assert.throws(
    () =>
      adapter.applyConstraints(
        { model: "gpt-4o", tools: TOOLS, functions: [{ name: "lookup_patient_record" }] },
        { tools: "refuse" },
        "chat.completions.create",
      ),
    (error: Error) => {
      assert.ok(error instanceof ToolUseRefusedError);
      assert.equal(
        error.message,
        "Agentic tools are not accepted for this user (refused by the person, never asked, or refused by a " +
          "policy), and this call passes `tools`, `functions`. The AI call was not made. Retry without " +
          "them; never retry the same call, it will be refused again.",
      );
      return true;
    },
  );
});

test("a reference strip cannot take out is refused in the same words", () => {
  const adapter = new GenericAdapter(OPENAI);
  assert.throws(
    () =>
      adapter.applyConstraints(
        { model: "gpt-4o", tools: TOOLS, conversation: "conv_123", previous_response_id: "resp_123" },
        { tools: "strip" },
        "responses.create",
      ),
    (error: Error) => {
      assert.ok(error instanceof ToolUseRefusedError);
      assert.equal(
        error.message,
        "Agentic tools are not accepted for this user (refused by the person, never asked, or refused by a " +
          "policy), and this call passes `conversation`, `previous_response_id`, which can bring tools the SDK " +
          "cannot see or remove without changing what the call asks. The AI call was not made. Retry without " +
          "them; never retry the same call, it will be refused again.",
      );
      return true;
    },
  );
});

// ── Nested tools: google-genai puts them inside `config` ───────────────────

test("gemini tools are found inside config", () => {
  assert.throws(
    () =>
      new GeminiAdapter(GEMINI).applyConstraints(
        { model: "gemini-2.0-flash", contents: "hi", config: { tools: TOOLS } },
        { tools: "refuse" },
      ),
    ToolUseRefusedError,
  );
});

test("gemini config without tools passes through", () => {
  const params = { model: "gemini-2.0-flash", config: { temperature: 0.5 } };
  assert.deepEqual(new GeminiAdapter(GEMINI).applyConstraints(params, { tools: "refuse" }), params);
});

test("gemini strip keeps the rest of the config", () => {
  const out = new GeminiAdapter(GEMINI).applyConstraints(
    { model: "gemini-2.0-flash", config: { temperature: 0.5, tools: TOOLS } },
    { tools: "strip" },
  );

  assert.deepEqual(out.config, { temperature: 0.5 });
});

// ── End to end through consent.wrap() ─────────────────────────────────────

test("a refused call never reaches the provider", async () => {
  const { client, calls } = fakeOpenAI();
  const wrapped = wrap(client, { consentClient: fakeConsentClient(decision({ tools: "refuse" })), provider: "openai" });

  await assert.rejects(
    wrapped.chat.completions.create({ model: "gpt-4o", messages: [], tools: TOOLS, user_id: "usr_marie_456" }),
    ToolUseRefusedError,
  );
  assert.deepEqual(calls, []);
});

test("a stripped call reaches the provider without its tools", async () => {
  const { client, calls } = fakeOpenAI();
  const wrapped = wrap(client, { consentClient: fakeConsentClient(decision({ tools: "strip" })), provider: "openai" });

  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], tools: TOOLS, user_id: "usr_marie_456" });

  assert.ok(!("tools" in calls[0]!.body));
});

test("the refusal tells the cloud nothing about the call", async () => {
  // The proof is minted and journalled before the SDK refuses locally: the
  // ledger entry records the constraint that was in force for this user. It
  // records no claim about whether the call carried tools, because we were
  // never told.
  const { client } = fakeOpenAI();
  const cc = fakeConsentClient(decision({ tools: "refuse" }));
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  await assert.rejects(
    wrapped.chat.completions.create({ model: "gpt-4o", messages: [], tools: TOOLS, user_id: "usr_marie_456" }),
    ToolUseRefusedError,
  );

  assert.equal(cc.calls.length, 1);
  assert.deepEqual(Object.keys(cc.calls[0]!).sort(), [
    "callSite",
    // What the app provisioned, never what the call contains.
    "credentialPostures",
    "model",
    "provider",
    "requestHash",
    "ubt",
    "userId",
  ]);
});

test("the request hash still covers the tools the developer passed", () => {
  // The proof binds the request as it was written, not as it was allowed.
  // Otherwise a refused call and an accepted one could share a hash, and the
  // binding would no longer say which request was authorised.
  const adapter = new GenericAdapter(OPENAI);

  assert.notEqual(
    adapter.extractRequestMetadata({ model: "gpt-4o", messages: [] }).requestHash,
    adapter.extractRequestMetadata({ model: "gpt-4o", messages: [], tools: TOOLS }).requestHash,
  );
});
