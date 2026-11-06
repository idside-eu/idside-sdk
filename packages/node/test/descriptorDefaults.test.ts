/**
 * What a descriptor is allowed to leave out, and what it then means.
 *
 * The shared JSON Schema marks most of a descriptor optional, so the loader
 * carries a default for each — and every one of those defaults is a decision
 * about an unwritten field. Today all five bundled descriptors happen to spell
 * out enough that not one default ever fires, which is exactly why they need
 * pinning: the day a sixth provider omits `token_header`, the value that gets
 * used is decided here and nowhere else, and it travels into signed tokens.
 *
 * `descriptorsSync.test.ts` proves the bundled JSON matches the source of truth
 * byte for byte. This file proves what the loader makes of a descriptor that
 * exercises the schema's optionality.
 *
 * The mechanism: `loadDescriptors()` reads the imported JSON modules each time
 * it is called, and a JSON module object is the same shared instance everywhere.
 * So a field can be removed, the loader asked what it now sees, and the field
 * put straight back. Every case restores in a `finally`, and this file is its
 * own process under `node --test`, so nothing it touches can reach another
 * suite.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { GenericAdapter } from "../src/adapters/index.ts";
import { DEFAULT_CONSENT_HEADER, isReservedHeader, loadDescriptors } from "../src/descriptors.ts";
import { ToolUseRefusedError } from "../src/exceptions.ts";
import openai from "../src/providers/openai.json" with { type: "json" };
import deepseek from "../src/providers/deepseek.json" with { type: "json" };

type Raw = Record<string, unknown>;

/** Run `fn` with `field` absent from `raw`, then put it back exactly as it was. */
function without<T>(raw: Raw, field: string, fn: () => T): T {
  const had = field in raw;
  const previous = raw[field];
  delete raw[field];
  try {
    return fn();
  } finally {
    if (had) raw[field] = previous;
    else delete raw[field];
  }
}

/** Run `fn` with `field` set to `value`, then put it back exactly as it was. */
function withField<T>(raw: Raw, field: string, value: unknown, fn: () => T): T {
  const had = field in raw;
  const previous = raw[field];
  raw[field] = value;
  try {
    return fn();
  } finally {
    if (had) raw[field] = previous;
    else delete raw[field];
  }
}

const DEEPSEEK = deepseek as unknown as Raw;
const OPENAI = openai as unknown as Raw;

function loaded(name: string) {
  return loadDescriptors().get(name)!;
}

// ── the identity parameters ──────────────────────────────────────────────────

test("a descriptor that names no identity param gets user_id", () => {
  // The name is what the SDK strips off the call before hashing it, so a wrong
  // default would forward the end user's identifier to the provider.
  const identityParam = without(DEEPSEEK, "identity_param", () => loaded("deepseek").identityParam);

  assert.equal(identityParam, "user_id");
  assert.equal(loaded("deepseek").identityParam, "user_id", "the field must be back");
});

test("a descriptor that names no ubt param gets ubt", () => {
  const ubtParam = without(DEEPSEEK, "ubt_param", () => loaded("deepseek").ubtParam);

  assert.equal(ubtParam, "ubt");
});

// ── how the proof travels ────────────────────────────────────────────────────

test("a descriptor that names no token header gets the standard one", () => {
  // Silently choosing a different header would send the call with no proof on
  // it as far as the provider — and as far as the net — are concerned.
  const tokenHeader = without(DEEPSEEK, "token_header", () => loaded("deepseek").tokenHeader);

  assert.equal(tokenHeader, DEFAULT_CONSENT_HEADER);
  assert.equal(tokenHeader, "X-AI-Consent-Token");
});

test("a descriptor that names no headers kwarg gets extra_headers", () => {
  const headersKwarg = without(DEEPSEEK, "headers_kwarg", () => loaded("deepseek").headersKwarg);

  assert.equal(headersKwarg, "extra_headers");
});

// ── where forced params land ─────────────────────────────────────────────────

test("param_target is 'headers' only when it says exactly that", () => {
  const asHeaders = withField(DEEPSEEK, "param_target", "headers", () =>
    loaded("deepseek").paramTarget,
  );

  assert.equal(asHeaders, "headers");
});

test("any other param_target, including none at all, means kwargs", () => {
  // Not a tolerant read: a typo must not silently move every forced parameter
  // from the request body into a header the provider ignores.
  const typo = withField(DEEPSEEK, "param_target", "header", () => loaded("deepseek").paramTarget);
  const absent = without(DEEPSEEK, "param_target", () => loaded("deepseek").paramTarget);

  assert.equal(typo, "kwargs");
  assert.equal(absent, "kwargs");
});

// ── the lists ────────────────────────────────────────────────────────────────

test("a descriptor with no protected params protects nothing", () => {
  // An empty set, never undefined: `new Set(undefined)` is an empty set too,
  // but `protectedParams.includes` on undefined is a TypeError three frames in.
  const protectedParams = without(DEEPSEEK, "protected_params", () =>
    loaded("deepseek").protectedParams,
  );

  assert.deepEqual(protectedParams, []);
});

test("a call site with no forceable params declares an empty list", () => {
  const site = (DEEPSEEK.call_sites as Raw[])[0]!;
  const forceable = without(site, "forceable_params", () => loaded("deepseek").callSites[0]!.forceableParams);

  assert.deepEqual(forceable, []);
});

test("a call site with no aliases declares an empty list", () => {
  const site = (DEEPSEEK.call_sites as Raw[])[0]!;
  const aliases = without(site, "aliases", () => loaded("deepseek").callSites[0]!.aliases);

  assert.deepEqual(aliases, []);
});

test("a descriptor naming no base-url hosts claims none in particular", () => {
  // That is what makes it the module's default when several descriptors share a
  // client library, so the empty list is load-bearing, not a placeholder.
  const hosts = without(DEEPSEEK, "client_base_url_hosts", () => loaded("deepseek").clientBaseUrlHosts);

  assert.deepEqual(hosts, []);
});

// ── credential routing ───────────────────────────────────────────────────────

test("a routing block with no unit calls it a credential", () => {
  // The unit is the provider's own vocabulary — project, workspace,
  // organisation — and it is quoted at a developer whose call just failed. A
  // generic word is a worse message; an `undefined` in the sentence is a bug
  // report.
  const routing = OPENAI.credential_routing as Raw;
  const unit = without(routing, "unit", () => loaded("openai").credentialRouting!.unit);
  const blank = withField(routing, "unit", "", () => loaded("openai").credentialRouting!.unit);

  assert.equal(unit, "credential");
  assert.equal(blank, "credential", "an empty unit is not a usable one either");
  assert.equal(loaded("openai").credentialRouting!.unit, "project", "the field must be back");
});

test("a routing block with no header pins no pattern either", () => {
  const routing = OPENAI.credential_routing as Raw;
  const bare = without(routing, "header", () =>
    without(routing, "header_value_pattern", () => loaded("openai").credentialRouting!),
  );

  assert.equal(bare.header, undefined);
  assert.equal(bare.headerValuePattern, undefined);
  assert.equal(bare.unit, "project", "the rest of the block still loads");
});

test("a descriptor with no routing block has none, not an empty one", () => {
  // `undefined` is what makes `consent.wrap({ credentials })` refuse for this
  // provider instead of quietly accepting labels it can never act on.
  assert.equal(loaded("deepseek").credentialRouting, undefined);
});

// ── the version ──────────────────────────────────────────────────────────────

test("a descriptor that states no version is a version 2 descriptor", () => {
  const version = without(DEEPSEEK, "descriptor_version", () => loaded("deepseek").descriptorVersion);

  assert.equal(version, "2");
});

// ── reserved headers and tool fields ─────────────────────────────────────────

test("a descriptor that reserves no header still reserves the token header, in any case", () => {
  // The token header is the proof itself: a caller-supplied one is never a
  // tracing header, whatever the descriptor forgot to list.
  const deepseekLoaded = without(DEEPSEEK, "reserved_headers", () => loaded("deepseek"));

  assert.deepEqual(deepseekLoaded.reservedHeaders, []);
  assert.equal(isReservedHeader(deepseekLoaded, "x-ai-consent-token"), true);
  assert.equal(isReservedHeader(deepseekLoaded, "Authorization"), false);
  assert.equal(isReservedHeader(loaded("deepseek"), "AUTHORIZATION"), true, "the field must be back");
  assert.equal(isReservedHeader(loaded("deepseek"), "X-Request-Id"), false);
});

test("a tool field reads its JavaScript path when it has one, and no item types when it names none", () => {
  const gemini = loaded("gemini").callSites.find((site) => site.id === "models.generate_content")!;
  const responses = loaded("openai").callSites.find((site) => site.id === "responses.create")!;

  assert.deepEqual(gemini.toolFields, [{ path: ["config", "cachedContent"], kind: "enabler", itemTypes: [] }]);
  assert.ok(responses.toolFields.some((field) => field.kind === "input_items" && field.itemTypes.includes("additional_tools")));
});

test("a call site with no tool fields has none, and an unknown kind is read as an enabler", () => {
  // An unknown kind is not the most permissive one, and it is not a reason to
  // stop loading either: descriptors load when the SDK is imported, so a throw
  // here made every provider unusable. It is read the way Python reads it, as
  // an enabler — any value given there refuses the call under a tools refusal,
  // an empty list included, which a `definitions` field would have let through.
  const sites = OPENAI.call_sites as Array<Record<string, unknown>>;
  const chat = sites[0]!;
  const noFields = without(chat, "tool_fields", () => loaded("openai").callSites[0]!.toolFields);
  assert.deepEqual(noFields, []);

  const fields = chat.tool_fields as Array<Record<string, unknown>>;
  const path = fields[0]!.path as string[];
  withField(fields[0]!, "kind", "server_object", () => {
    const descriptor = loaded("openai");
    const field = descriptor.callSites[0]!.toolFields[0]!;
    assert.deepEqual(field, { path, kind: "enabler", itemTypes: [] });
    assert.deepEqual([...loadDescriptors().keys()].sort(), ["anthropic", "deepseek", "gemini", "mistral", "openai"]);

    const adapter = new GenericAdapter(descriptor);
    for (const action of ["refuse", "strip"]) {
      assert.throws(
        () => adapter.applyConstraints({ model: "gpt-4o", [path[0]!]: [] }, { tools: action }, "chat.completions.create"),
        (error: unknown) => error instanceof ToolUseRefusedError && error.message.includes(`\`${path.join(".")}\``),
        action,
      );
    }
    assert.deepEqual(adapter.applyConstraints({ model: "gpt-4o" }, { tools: "refuse" }, "chat.completions.create"), {
      model: "gpt-4o",
    });
  });
  assert.equal(loaded("openai").callSites[0]!.toolFields[0]!.kind, "definitions", "the field must be back");
});

// ── nothing above may have left a mark ───────────────────────────────────────

test("every bundled descriptor still loads exactly as it did", () => {
  const all = loadDescriptors();

  assert.deepEqual([...all.keys()].sort(), ["anthropic", "deepseek", "gemini", "mistral", "openai"]);
  assert.equal(all.get("openai")!.identityParam, "user_id");
  assert.equal(all.get("openai")!.paramTarget, "kwargs");
  assert.equal(all.get("openai")!.credentialRouting!.header, "OpenAI-Project");
  assert.equal(all.get("openai")!.credentialRouting!.unit, "project");
  assert.equal(all.get("deepseek")!.descriptorVersion, "2");
  assert.deepEqual(all.get("deepseek")!.clientBaseUrlHosts, ["api.deepseek.com"]);
});
