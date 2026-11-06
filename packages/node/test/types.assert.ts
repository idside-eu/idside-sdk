/**
 * Type-level assertions for the enforced client surface.
 *
 * Nothing in this file runs, and nothing in it is meant to: `tsc --noEmit` IS
 * the assertion, and CI runs it (`npm run typecheck`). It is also the only
 * shape of test that can fail here — no runtime test can observe that a
 * developer had to write a cast to get their own call to compile.
 *
 * What each group pins:
 *   - the identity is accepted on an object literal, with no cast at all;
 *   - forgetting it is a COMPILE error, not a runtime one;
 *   - naming the user twice is a compile error too;
 *   - the provider's own overloads survive — `stream: true` still returns a
 *     stream, and the non-streaming call still returns a completion;
 *   - the surfaces the SDK does not enforce keep their own types untouched.
 *
 * `@ts-expect-error` fails the build when the line below it COMPILES, so each
 * one is the negative half of the pair above it.
 */

import OpenAI from "openai";
import Anthropic from "@anthropic-ai/sdk";
import { Mistral } from "@mistralai/mistralai";
import { consent } from "../src/index.ts";
import type { Enforced } from "../src/index.ts";

// Nothing constructs a real client at runtime: these are declarations.
declare const openai: Enforced<OpenAI>;
declare const anthropic: Enforced<Anthropic>;
declare const mistral: Enforced<Mistral>;

/** The wrapped client is what `wrap()` gives back — the annotation must hold. */
export function inference(): void {
  const client: Enforced<OpenAI> = consent.wrap(new OpenAI({ apiKey: "sk-test-not-a-real-key" }));
  void client;
}

export async function openaiChatCompletions(): Promise<void> {
  // The identity, on a plain object literal, with no cast.
  const completion = await openai.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: "Hello!" }],
    user_id: "usr_marie_456",
  });
  const id: string = completion.id; // still a ChatCompletion, not a union
  void id;

  // The widget-issued binding is the other half of the identity.
  const streamed = await openai.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: "Hello!" }],
    stream: true,
    ubt: "ubt_marie_456",
  });
  for await (const chunk of streamed) void chunk; // still a Stream: the overload survived

  // @ts-expect-error the identity is required, and forgetting it must not compile
  await openai.chat.completions.create({ model: "gpt-4o-mini", messages: [] });

  // @ts-expect-error naming the user twice is a contradiction, not a choice
  await openai.chat.completions.create({ model: "gpt-4o-mini", messages: [], user_id: "u", ubt: "b" });
}

export async function openaiResponses(): Promise<void> {
  const response = await openai.responses.create({
    model: "gpt-4o-mini",
    input: "Hello!",
    user_id: "usr_marie_456",
  });
  const text: string = response.output_text;
  void text;

  // @ts-expect-error the identity is required here too
  await openai.responses.create({ model: "gpt-4o-mini", input: "Hello!" });
}

export function openaiAliases(): void {
  // `parse` and `stream` reach the same endpoint, so they demand the same identity.
  void openai.chat.completions.stream({ model: "gpt-4o-mini", messages: [], user_id: "usr_marie_456" });
  void openai.chat.completions.parse({ model: "gpt-4o-mini", messages: [], ubt: "ubt_marie_456" });
  void openai.responses.stream({ model: "gpt-4o-mini", input: "Hello!", user_id: "usr_marie_456" });

  // @ts-expect-error an alias without the identity fails at the same place as its call site
  void openai.chat.completions.stream({ model: "gpt-4o-mini", messages: [] });
}

export async function refusedOptions(): Promise<void> {
  const body = { model: "gpt-4o-mini", messages: [], user_id: "usr_marie_456" } as const;

  // What an enforced call keeps: timeouts, retries, cancellation, idempotency, headers.
  await openai.chat.completions.create(
    { ...body, messages: [] },
    { timeout: 30_000, maxRetries: 1, signal: new AbortController().signal, idempotencyKey: "idem_42", headers: { "X-Request-Id": "req_42" } },
  );
  await mistral.chat.complete(
    { model: "mistral-small-latest", messages: [], user_id: "usr_marie_456" },
    { timeoutMs: 30_000, retryCodes: ["503"], headers: { "X-Request-Id": "req_42" } },
  );

  // @ts-expect-error options.body would replace the request that was authorized
  await openai.chat.completions.create({ ...body, messages: [] }, { body: { store: true } });
  // @ts-expect-error fetchOptions reaches the request after enforcement
  await openai.responses.create({ model: "gpt-4o-mini", input: "hi", user_id: "usr_marie_456" }, { fetchOptions: {} });
  // @ts-expect-error per-request middleware rewrites the request on its way out
  await anthropic.messages.create({ model: "claude-sonnet-4-5", max_tokens: 64, messages: [], user_id: "usr_marie_456" }, { middleware: [] });
  // @ts-expect-error serverURL sends the request to another server
  await mistral.chat.complete({ model: "mistral-small-latest", messages: [], user_id: "usr_marie_456" }, { serverURL: "https://evil.example" });
}

export function untouchedSurfaces(): void {
  // Not a declared call site: its own type, unchanged, no identity to pass.
  void openai.models.list();
  void openai.files.list();
}

export async function anthropicMessages(): Promise<void> {
  const message = await anthropic.messages.create({
    model: "claude-sonnet-4-5",
    max_tokens: 64,
    messages: [{ role: "user", content: "Hello!" }],
    user_id: "usr_marie_456",
  });
  const id: string = message.id;
  void id;

  void anthropic.messages.stream({
    model: "claude-sonnet-4-5",
    max_tokens: 64,
    messages: [{ role: "user", content: "Hello!" }],
    ubt: "ubt_marie_456",
  });

  // @ts-expect-error the identity is required
  await anthropic.messages.create({ model: "claude-sonnet-4-5", max_tokens: 64, messages: [] });
}

export async function mistralCallSites(): Promise<void> {
  void (await mistral.chat.complete({ model: "mistral-small-latest", messages: [], user_id: "usr_marie_456" }));
  void (await mistral.chat.stream({ model: "mistral-small-latest", messages: [], ubt: "ubt_marie_456" }));
  void (await mistral.fim.complete({ model: "codestral-2405", prompt: "def f(", user_id: "usr_marie_456" }));
  void (await mistral.agents.complete({ agentId: "ag_medhelp", messages: [], user_id: "usr_marie_456" }));
  void (await mistral.beta.conversations.start({ inputs: "Hello!", user_id: "usr_marie_456" }));

  // @ts-expect-error every declared call site demands the identity, on every provider
  await mistral.chat.complete({ model: "mistral-small-latest", messages: [] });
}
