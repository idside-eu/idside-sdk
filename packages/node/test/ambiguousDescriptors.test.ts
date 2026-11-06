/**
 * Two descriptors claiming the same client library, and neither claiming a host.
 *
 * OpenAI-compatible providers all arrive through the `openai` package, so the
 * class name settles nothing and the base URL is the only thing that says who
 * receives the prompt. `client_base_url_hosts: []` means "the default for this
 * module" — and exactly one descriptor per module may say that. If two did, a
 * client pointed somewhere neither recognises would match both, and picking
 * either is picking which company gets the request.
 *
 * The bundled five cannot be made ambiguous — `descriptorsSync.test.ts` holds
 * them byte-identical to the shared source of truth — so the ambiguity is built
 * here, in a process of its own, by loading the adapter registry against a
 * descriptor set that has been made ambiguous for the duration of the load.
 *
 * This file deliberately does NOT import the registry at the top: it has to be
 * loaded after the mutation, because it snapshots the descriptors once.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import deepseek from "../src/providers/deepseek.json" with { type: "json" };
import { UnsupportedProviderError } from "../src/exceptions.ts";

const DEEPSEEK = deepseek as unknown as Record<string, unknown>;

/** The registry, loaded once against a descriptor set with two module defaults. */
const ambiguous = await (async () => {
  const hosts = DEEPSEEK.client_base_url_hosts;
  DEEPSEEK.client_base_url_hosts = [];
  try {
    return await import("../src/adapters/index.ts");
  } finally {
    DEEPSEEK.client_base_url_hosts = hosts;
  }
})();

function openAIClient(baseURL: string) {
  class OpenAI {
    baseURL = baseURL;
    chat = { completions: { create: async () => ({}) } };
    responses = { create: async () => ({}) };
  }
  return new OpenAI();
}

test("a host neither descriptor claims is refused, not guessed at", () => {
  // The refusal is the feature. Answering "openai" here would send a prompt to
  // a server governed by DeepSeek's terms under OpenAI's registry rules, or the
  // reverse, and the proof would record the wrong company either way.
  assert.throws(
    () => ambiguous.getAdapterFor(openAIClient("https://llm.internal.example")),
    UnsupportedProviderError,
  );
});

test("an explicit provider still resolves the ambiguity the caller can see", () => {
  // `{ provider }` is the documented way out, and the refusal message says so.
  assert.equal(ambiguous.getAdapterFor(openAIClient("https://llm.internal.example"), "openai").name, "openai");
  assert.equal(ambiguous.getAdapterFor(openAIClient("https://llm.internal.example"), "deepseek").name, "deepseek");
});

test("the mutation is confined to the registry built above", () => {
  // The JSON module object is shared process-wide; if the restore had not
  // happened, every later `loadDescriptors()` would see an ambiguous set.
  assert.deepEqual(DEEPSEEK.client_base_url_hosts, ["api.deepseek.com"]);
});
