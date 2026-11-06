/**
 * The injection engine, at the edges of what a call may contain.
 *
 * `GenericAdapter` does two jobs and both are decided by the shape of values it
 * did not choose: it turns the caller's arguments into a reproducible
 * `request_hash`, and it puts the ACT token where that provider reads headers.
 * The happy paths are covered from every direction elsewhere. What is covered
 * here is what happens when a call carries something the engine cannot treat as
 * JSON, or when a descriptor asks for the injection the bundled five never ask
 * for.
 *
 * The rule behind most of it: **never guess.** `String(value)` on an unknown
 * object would embed engine-dependent text into the request hash, and the hash
 * is frozen across SDKs and bound into signed tokens — a Python SDK and a Node
 * SDK that disagree about one call produce a proof that verifies against
 * nothing. Refusing is the only safe answer, so refusing is what is asserted.
 *
 * Mirrors packages/python/tests/test_adapters.py.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { GeminiAdapter, GenericAdapter } from "../src/adapters/index.ts";
import { headersToObject } from "../src/adapters/base.ts";
import { loadDescriptors, type ProviderDescriptor } from "../src/descriptors.ts";
import { ConsentTokenError, ToolUseRefusedError } from "../src/exceptions.ts";

const OPENAI = loadDescriptors().get("openai")!;
const GEMINI = loadDescriptors().get("gemini")!;
const TOKEN = "act.jwt.token"; // pragma: allowlist secret

const openai = new GenericAdapter(OPENAI);

function hashOf(params: Record<string, unknown>): string {
  return openai.extractRequestMetadata(params).requestHash;
}

/** Headers as the generic engine places them: on the second argument. */
function optionHeaders(args: unknown[]): Record<string, unknown> {
  return ((args[1] as { headers?: Record<string, unknown> } | undefined)?.headers ?? {});
}

// ── values the request hash cannot be computed over ──────────────────────────

test("a value with no JSON form stops the call instead of being stringified", () => {
  // `String(new Date())` is locale- and engine-dependent; embedding it would
  // make two SDKs hash the same call differently, and the token minted for one
  // would verify against nothing on the other.
  class Opaque {}

  assert.throws(
    () => hashOf({ model: "gpt-5", metadata: new Opaque() }),
    (error: unknown) =>
      error instanceof ConsentTokenError &&
      /type 'Opaque' at 'metadata'/.test(String(error)),
    "the error must name the type and the path a developer has to go and fix",
  );
});

test("an object exposing toJSON() is read through it, however deep", () => {
  class Attachment {
    toJSON(): Record<string, unknown> {
      return { kind: "note", length: 3 };
    }
  }

  const withObject = hashOf({ model: "gpt-5", extra: { at: new Attachment() } });
  const plain = hashOf({ model: "gpt-5", extra: { at: { kind: "note", length: 3 } } });

  assert.equal(withObject, plain, "toJSON() must yield the same hash as the plain form");
});

test("a bigint stops the call: JSON has no such number", () => {
  // `JSON.stringify` throws on one, so silently accepting it here would only
  // move the failure to a place with no context.
  assert.throws(
    () => hashOf({ model: "gpt-5", seed: 10n }),
    (error: unknown) =>
      error instanceof ConsentTokenError && /type 'bigint' at 'seed'/.test(String(error)),
  );
});

test("a function or a symbol stops the call for the same reason", () => {
  assert.throws(() => hashOf({ model: "gpt-5", cb: () => 1 }), ConsentTokenError);
  assert.throws(() => hashOf({ model: "gpt-5", tag: Symbol("x") }), ConsentTokenError);
});

test("NaN and Infinity stop the call, named as canonicalization failures", () => {
  // These survive `normalize` — they are numbers — and are refused one layer
  // down, by RFC 8785 itself. The message has to come back as a consent error
  // and not as an internal one, or the developer sees a stack from `jcs.ts`.
  for (const value of [Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(
      () => hashOf({ model: "gpt-5", temperature: value }),
      (error: unknown) =>
        error instanceof ConsentTokenError &&
        /Cannot compute a deterministic request_hash/.test(String(error)),
    );
  }
});

test("a lone surrogate stops the call: it has no UTF-8 form", () => {
  assert.throws(() => hashOf({ model: "gpt-5", messages: "\ud800" }), ConsentTokenError);
});

// ── values that are dropped rather than refused ──────────────────────────────

test("null is a value, and survives into the hash", () => {
  assert.notEqual(hashOf({ model: "gpt-5", stop: null }), hashOf({ model: "gpt-5" }));
});

test("an undefined array element becomes null, exactly as JSON.stringify does", () => {
  const sparse = hashOf({ model: "gpt-5", stop: [undefined, "x"] });
  const explicit = hashOf({ model: "gpt-5", stop: [null, "x"] });

  assert.equal(sparse, explicit);
});

test("an undefined-valued key is dropped, exactly as JSON.stringify does", () => {
  const withKey = hashOf({ model: "gpt-5", extra: { a: 1, b: undefined } });
  const without = hashOf({ model: "gpt-5", extra: { a: 1 } });

  assert.equal(withKey, without);
});

test("an object with a null prototype hashes like a plain one", () => {
  // `Object.create(null)` is what a parsed-with-care JSON object looks like. It
  // has no `constructor`, so a check written as "is it an Object instance"
  // would refuse a value that is more plainly JSON than most.
  const bare = Object.create(null) as Record<string, unknown>;
  bare.a = 1;

  assert.equal(hashOf({ model: "gpt-5", extra: bare }), hashOf({ model: "gpt-5", extra: { a: 1 } }));
});

test("the identity and the per-request headers are excluded from the hash", () => {
  // They are stripped before the cloud ever sees the payload, so including them
  // would make one user's hash unreproducible by anyone auditing the ledger.
  const bare = hashOf({ model: "gpt-5", messages: [] });
  const dressed = hashOf({
    model: "gpt-5",
    messages: [],
    user_id: "usr_marie_456",
    ubt: "ubt.stand-in.value", // pragma: allowlist secret
    extra_headers: { "X-Trace-Id": "trace_42" },
  });

  assert.equal(dressed, bare);
});

test("a call with no model reports an empty model, not 'undefined'", () => {
  // The model goes to the cloud as a string. `String(undefined)` would send the
  // literal text "undefined" and the ledger would record it.
  assert.equal(openai.extractRequestMetadata({ messages: [] }).model, "");
  assert.equal(openai.extractRequestMetadata({ model: null, messages: [] }).model, "");
  assert.equal(openai.extractRequestMetadata({ model: "gpt-5" }).model, "gpt-5");
});

// ── a descriptor whose forced params travel as headers ───────────────────────
//
// No bundled descriptor asks for this today, which is precisely why it needs a
// test: the branch decides where every cloud-forced parameter lands, and the
// day a provider reads its retention lever from a header, nothing else would
// have said whether this works.

const HEADER_TARGET: ProviderDescriptor = { ...OPENAI, paramTarget: "headers" };

test("with param_target 'headers' the forced params leave as headers, not in the body", () => {
  const args = new GenericAdapter(HEADER_TARGET).buildEnforcedArgs(
    [{ model: "gpt-5", messages: [] }],
    { store: false },
    TOKEN,
  );

  const body = args[0] as Record<string, unknown>;
  assert.equal(body.store, undefined, "a forced param leaked into the request body");
  assert.equal(optionHeaders(args).store, false);
  assert.equal(optionHeaders(args)["X-AI-Consent-Token"], TOKEN);
});

test("even as headers, a protected param is still dropped", () => {
  const args = new GenericAdapter(HEADER_TARGET).buildEnforcedArgs(
    [{ model: "gpt-5" }],
    { model: "gpt-HACKED", store: false },
    TOKEN,
  );

  assert.equal((args[0] as Record<string, unknown>).model, "gpt-5");
  assert.equal(optionHeaders(args).model, undefined, "the cloud rewrote the request through a header");
});

test("a body that is not a plain object still leaves with a token on it", () => {
  const args = openai.buildEnforcedArgs(["not a body"], {}, TOKEN);

  assert.deepEqual(args[0], {});
  assert.equal(optionHeaders(args)["X-AI-Consent-Token"], TOKEN);
});

// ── the caller's own header container, in each shape it arrives in ───────────

test("caller headers survive as a plain object, a Headers, or tuples", () => {
  // openai and anthropic both accept all three, and losing the caller's headers
  // silently would break tracing, idempotency keys and beta opt-ins.
  const shapes: unknown[] = [
    { "X-Trace-Id": "trace_42" },
    new Headers({ "X-Trace-Id": "trace_42" }),
    [["X-Trace-Id", "trace_42"]],
  ];

  for (const headers of shapes) {
    const args = openai.buildEnforcedArgs([{ model: "gpt-5" }, { headers }], {}, TOKEN);
    const applied = headersToObject(optionHeaders(args));

    assert.equal(
      applied["X-Trace-Id"] ?? applied["x-trace-id"],
      "trace_42",
      `the caller's header was lost for ${headers?.constructor?.name ?? typeof headers}`,
    );
    assert.equal(optionHeaders(args)["X-AI-Consent-Token"], TOKEN);
  }
});

test("headersToObject refuses to invent headers out of a non-container", () => {
  assert.deepEqual(headersToObject(undefined), {});
  assert.deepEqual(headersToObject("X-Trace-Id: trace_42"), {});
  assert.deepEqual(headersToObject(42), {});
});

test("an options argument that is not an object is replaced, not spread", () => {
  const args = openai.buildEnforcedArgs([{ model: "gpt-5" }, "timeout"], {}, TOKEN);

  assert.equal(optionHeaders(args)["X-AI-Consent-Token"], TOKEN);
});

// ── constraints, at the edges ────────────────────────────────────────────────

test("a decision object with no constraints at all means no constraint", () => {
  // TypeScript makes the field mandatory, but a plain-JavaScript consumer can
  // inject their own ConsentClient — the documented way to test without the
  // network — and an older stand-in that predates constraints must degrade to
  // "no constraint", not to a TypeError three frames inside the SDK.
  const params = { model: "gpt-5", tools: [{ type: "function" }] };

  assert.equal(openai.applyConstraints(params, undefined as never), params);
});

test("a nested tools path is read through an object exposing toJSON()", () => {
  // Gemini's tools live at `config.tools`, and a typed config is a legitimate
  // thing to pass. A refusal the SDK cannot see is a refusal it would ignore.
  class TypedConfig {
    toJSON(): Record<string, unknown> {
      return { tools: [{ functionDeclarations: [] }] };
    }
  }

  assert.throws(
    () =>
      new GenericAdapter(GEMINI).applyConstraints(
        { model: "gemini-2.0-flash", config: new TypedConfig() },
        { tools: "refuse" },
      ),
    ToolUseRefusedError,
  );
});

test("stripping a tools path that is not there changes nothing", () => {
  const params = { model: "gpt-5", messages: [] };

  assert.equal(openai.applyConstraints(params, { tools: "strip" }), params);
});

test("stripping removes the key rather than emptying it", () => {
  // Several providers reject an empty `tools` list, and "no tools" is what the
  // user asked for, not "zero tools".
  const params = { model: "gpt-5", tools: [{ type: "function" }] };
  const out = openai.applyConstraints(params, { tools: "strip" });

  assert.ok(!("tools" in out));
  assert.deepEqual(params.tools, [{ type: "function" }], "the caller's object was mutated");
});

test("stripping a nested tools path keeps the rest of the branch intact", () => {
  const params = {
    model: "gemini-2.0-flash",
    config: { temperature: 0.5, tools: [{ functionDeclarations: [] }] },
  };
  const out = new GenericAdapter(GEMINI).applyConstraints(params, { tools: "strip" }) as {
    config: Record<string, unknown>;
  };

  assert.equal(out.config.temperature, 0.5);
  assert.ok(!("tools" in out.config));
  assert.ok("tools" in params.config, "the caller's config was mutated");
});

test("an object whose type has no name is still refused, and says so", () => {
  // `Object.create(Object.create(null))` has no `constructor` to name. The
  // message has to degrade to something true rather than to `undefined`, which
  // is what a developer would read as a bug in the SDK rather than in their call.
  const nameless = Object.create(Object.create(null)) as Record<string, unknown>;
  nameless.a = 1;

  assert.throws(
    () => hashOf({ model: "gpt-5", extra: nameless }),
    (error: unknown) =>
      error instanceof ConsentTokenError &&
      /type 'object' at 'extra'/.test(String(error)) &&
      !/undefined/.test(String(error)),
  );
});

test("a config the SDK cannot read cannot smuggle tools past a refusal", () => {
  // `toJSON()` returning something that is not an object means the nested tools
  // path does not resolve, so `applyConstraints` sees no tools to refuse. That
  // must not end with the call going out: the Gemini adapter refuses to build
  // arguments around a config it cannot read, so the refusal it could not apply
  // stops the call anyway.
  class Unreadable {
    tools = [{ functionDeclarations: [] }];
    toJSON(): string {
      return "config";
    }
  }
  const adapter = new GenericAdapter(GEMINI);
  const params = { model: "gemini-2.0-flash", config: new Unreadable() };

  assert.equal(
    adapter.applyConstraints(params, { tools: "refuse" }),
    params,
    "an unreadable config hides its tools from the constraint check",
  );
  assert.throws(
    () => new GeminiAdapter(GEMINI).buildEnforcedArgs([params], {}, TOKEN),
    ConsentTokenError,
    "and the adapter is what stops the call from going out regardless",
  );
});
