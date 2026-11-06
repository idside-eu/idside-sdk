/**
 * Which provider is this client actually talking to? (P1.4)
 *
 * Detection used to be the client's class name, and that was right for as long
 * as one client library meant one provider. It stopped being right the moment an
 * OpenAI-compatible provider arrived: DeepSeek is reached through the `openai`
 * package pointed at a different base URL, so an `OpenAI` instance is an
 * `OpenAI` instance whoever receives the request.
 *
 * The consequence is not cosmetic. The detected name is what the SDK sends to
 * the cloud, and the cloud picks registry rules by it — so answering "openai"
 * for a DeepSeek call would apply OpenAI's parameters to a server that never
 * agreed to them.
 *
 * Mirrors packages/python/tests/test_provider_detection.py.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { GenericAdapter, getAdapterFor, registerAdapter } from "../src/adapters/index.ts";
import { UnsupportedProviderError } from "../src/exceptions.ts";

/** Stands in for the real `openai` client: same class name, chosen base URL. */
function openAIClient(baseURL?: string) {
  class OpenAI {
    baseURL = baseURL ?? "https://api.openai.com/v1";
    chat = { completions: { create: async () => ({}) } };
    responses = { create: async () => ({}) };
  }
  return new OpenAI();
}

test("a plain OpenAI client is detected as openai", () => {
  assert.equal(getAdapterFor(openAIClient()).name, "openai");
});

test("an OpenAI-compatible provider is not mistaken for OpenAI", () => {
  // The distinction the whole card exists for: same class, same module, same
  // call sites — different server, different terms, different registry rules.
  assert.equal(getAdapterFor(openAIClient("https://api.deepseek.com")).name, "deepseek");
});

test("the host decides even when the URL is written differently", () => {
  for (const url of [
    "https://api.deepseek.com",
    "https://api.deepseek.com/",
    "https://api.deepseek.com/v1",
    "https://API.DeepSeek.com/v1/",
  ]) {
    assert.equal(getAdapterFor(openAIClient(url)).name, "deepseek", url);
  }
});

test("an unknown base URL falls back to the module's default", () => {
  // A proxy, a gateway, or a compatible provider we have never heard of.
  // Falling back to `openai` is right: the mechanics are the OpenAI client's,
  // which is all the descriptor governs.
  assert.equal(getAdapterFor(openAIClient("https://llm.internal.example")).name, "openai");
});

test("detection survives a client with no base URL at all", () => {
  // Most clients do not expose one; reading it must never be load-bearing.
  class Anthropic {
    messages = { create: async () => ({}) };
  }
  assert.equal(getAdapterFor(new Anthropic()).name, "anthropic");
});

test("an explicit provider still wins over anything inferred", () => {
  assert.equal(getAdapterFor(openAIClient("https://api.deepseek.com"), "openai").name, "openai");
});

test("an unknown client says so instead of guessing", () => {
  assert.throws(() => getAdapterFor({}), UnsupportedProviderError);
});

// ── the edges of the same question ───────────────────────────────────────────
//
// Detection only ever NARROWS a choice, so every step of it is written to
// tolerate a client that answers oddly. What "tolerate" means is the point:
// each of these decides between a right descriptor, a wrong one, and a refusal,
// and the wrong one sends a prompt to a company the user's answer never named.

test("a base URL that is not a URL is simply not a match", () => {
  // A developer writing `baseURL: "api.deepseek.com"` (no scheme) has an
  // unparsable value, not a DeepSeek client. Falling back to the module default
  // is right; throwing out of `wrap()` over a cosmetic mistake is not.
  class OpenAI {
    baseURL = "api.deepseek.com";
    chat = { completions: { create: async () => ({}) } };
  }

  assert.equal(getAdapterFor(new OpenAI()).name, "openai");
});

test("an empty base URL is read as no base URL", () => {
  class OpenAI {
    baseURL = "";
    chat = { completions: { create: async () => ({}) } };
  }

  assert.equal(getAdapterFor(new OpenAI()).name, "openai");
});

test("a non-string base URL is ignored rather than coerced", () => {
  // `String({})` is "[object Object]", which parses as nothing and would only
  // ever produce a confusing near-match.
  class OpenAI {
    baseURL = { host: "api.deepseek.com" };
    chat = { completions: { create: async () => ({}) } };
  }

  assert.equal(getAdapterFor(new OpenAI()).name, "openai");
});

test("the python spelling base_url is read too", () => {
  // Some clients — and every hand-rolled test double — spell it the other way.
  class OpenAI {
    base_url = "https://api.deepseek.com";
    chat = { completions: { create: async () => ({}) } };
  }

  assert.equal(getAdapterFor(new OpenAI()).name, "deepseek");
});

test("a client that is not an object is not a provider", () => {
  for (const value of [null, undefined, 42, "openai", Symbol("openai")]) {
    assert.throws(() => getAdapterFor(value), UnsupportedProviderError, String(value));
  }
});

test("a call site whose path runs into a primitive does not resolve", () => {
  // `chat.completions` being a string means the walk has nowhere left to go.
  // Answering "the path resolves" would patch nothing and report success.
  assert.throws(() => getAdapterFor({ chat: { completions: "not a resource" } }), UnsupportedProviderError);
  assert.throws(() => getAdapterFor({ chat: { completions: null } }), UnsupportedProviderError);
  assert.throws(() => getAdapterFor({ chat: { completions: undefined } }), UnsupportedProviderError);
  assert.throws(() => getAdapterFor({ chat: null }), UnsupportedProviderError);
});

test("a resource that is a callable object still resolves", () => {
  // Provider clients do this: `client.responses` is callable in some versions
  // and carries its methods as properties. Walking only through plain objects
  // would silently leave that endpoint unpatched.
  const completions = Object.assign(function completions() {}, {
    create: async () => ({}),
  });

  assert.equal(getAdapterFor({ chat: { completions } }).name, "openai");
});

test("a structurally matched client is detected without any class name", () => {
  // An object literal with the right shape and no constructor hint: the
  // structural fallback is what makes a test double work at all.
  const client = { chat: { completions: { create: async () => ({}) } } };

  assert.equal(getAdapterFor(client).name, "openai");
});

test("the refusal names the class it could not place", () => {
  // Without it the developer is told only that something is unsupported, and
  // the one fact that would let them pass `{ provider }` is missing.
  class SomeVendorClient {}

  assert.throws(
    () => getAdapterFor(new SomeVendorClient()),
    (error: unknown) =>
      error instanceof UnsupportedProviderError &&
      /constructor 'SomeVendorClient'/.test(String(error)),
  );
});

test("a client with no constructor at all is refused without naming one", () => {
  const bare = Object.create(null) as object;

  assert.throws(
    () => getAdapterFor(bare),
    (error: unknown) =>
      error instanceof UnsupportedProviderError && !/constructor/.test(String(error)),
  );
});

test("an unknown provider hint is refused rather than guessed at", () => {
  assert.throws(
    () => getAdapterFor(openAIClient(), "0penai"),
    (error: unknown) =>
      error instanceof UnsupportedProviderError && /Unknown provider '0penai'/.test(String(error)),
  );
});

test("every bundled provider can be selected by name", () => {
  for (const name of ["openai", "anthropic", "gemini", "mistral", "deepseek"]) {
    assert.equal(getAdapterFor({}, name).name, name);
  }
});

test("the gemini override is what gets built, not the generic engine", () => {
  // Adding a provider is adding an adapter; the registry is what makes that
  // true, and a lookup that quietly fell back to GenericAdapter would send
  // Gemini's token into a second argument the client does not have.
  assert.equal(getAdapterFor({}, "gemini").constructor.name, "GeminiAdapter");
  assert.equal(getAdapterFor({}, "openai").constructor.name, "GenericAdapter");
});

test("a registered adapter replaces the engine for that provider only", () => {
  // "Adding a provider = adding an adapter, without touching the core" is only
  // true if the registry is actually consulted at build time. It is restored
  // afterwards because OVERRIDES is process-wide by design.
  class HouseAdapter extends GenericAdapter {}

  registerAdapter("deepseek", HouseAdapter);
  try {
    assert.equal(getAdapterFor({}, "deepseek").constructor.name, "HouseAdapter");
    assert.equal(getAdapterFor({}, "openai").constructor.name, "GenericAdapter");
    assert.equal(getAdapterFor({}, "deepseek").descriptor.name, "deepseek", "it still gets its own descriptor");
  } finally {
    registerAdapter("deepseek", GenericAdapter);
  }

  assert.equal(getAdapterFor({}, "deepseek").constructor.name, "GenericAdapter");
});

test("a client that is itself callable is narrowed like any other", () => {
  // Reading a base URL is best-effort by design: it only ever NARROWS a choice.
  // A callable client exposes no `baseURL` the walk can read, so the answer has
  // to be the module's default and not a crash on the way to it.
  const client = Object.assign(function client() {}, {
    chat: { completions: { create: async () => ({}) } },
  });

  assert.equal(getAdapterFor(client).name, "openai");
});
