/**
 * Type-level tripwire: every per-request option each library declares is classified.
 *
 * `src/adapters/callOptions.ts` closes the list of options an enforced call may
 * carry. A closed list goes stale one way only. A library release adds an
 * option, the list does not name it, and at run time it is refused. That is
 * safe, but nobody has decided whether it should be. This file turns that
 * silence into a build failure: each assertion states that a classification
 * map names exactly the keys the library's own type declares, no more and no
 * fewer. `npm run typecheck` includes this file, so an added or removed option
 * fails CI until someone has read what it does and classified it.
 *
 * The same holds for the construction options `withOptions()` accepts, which
 * are closed the same way for a derived client.
 *
 * Nothing here runs. The keys come from the call signatures themselves,
 * because the libraries do not export their `RequestOptions` types from a
 * public path.
 */

import type OpenAI from "openai";
import type { AzureClientOptions } from "openai/azure";
import type Anthropic from "@anthropic-ai/sdk";
import type { Mistral } from "@mistralai/mistralai";
import type { GenerateContentConfig, GenerateContentParameters, HttpOptions } from "@google/genai";
import type {
  ANTHROPIC_DERIVATION_OPTIONS,
  ANTHROPIC_REQUEST_OPTIONS,
  GEMINI_CONFIG_FIELDS,
  GEMINI_HTTP_OPTIONS,
  GEMINI_PARAMETERS,
  MISTRAL_REQUEST_OPTIONS,
  OPENAI_DERIVATION_OPTIONS,
  OPENAI_REQUEST_OPTIONS,
  OPENAI_RESPONSES_PARSE_OPTIONS,
} from "../src/adapters/callOptions.ts";

/**
 * True only when `A` and `B` are the same type. A plain mutual `extends` would
 * accept a union that is merely assignable. This compares how the compiler
 * resolves the two types, which only coincide for identical ones.
 */
type Equal<A, B> = (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
type Expect<T extends true> = T;

/** The key union of a method's second parameter, the options object. */
type OptionKeys<F> = F extends (...args: infer A) => unknown ? keyof NonNullable<A[1]> : never;

/** The key union of a method's first parameter: the options `withOptions()` builds a client from. */
type DerivationKeys<F> = F extends (...args: infer A) => unknown ? keyof NonNullable<A[0]> : never;

type Classified<M> = keyof M;

export type OpenAIChatCompletions = Expect<
  Equal<Classified<typeof OPENAI_REQUEST_OPTIONS>, OptionKeys<OpenAI["chat"]["completions"]["create"]>>
>;
export type OpenAIResponses = Expect<
  Equal<Classified<typeof OPENAI_REQUEST_OPTIONS>, OptionKeys<OpenAI["responses"]["create"]>>
>;
// The aliases forward their options to `create`, so they must not widen them.
export type OpenAIParse = Expect<
  Equal<Classified<typeof OPENAI_REQUEST_OPTIONS>, OptionKeys<OpenAI["chat"]["completions"]["parse"]>>
>;
// `responses.parse()` adds one option of its own, a parser that is never sent.
export type OpenAIResponsesParse = Expect<
  Equal<
    Classified<typeof OPENAI_REQUEST_OPTIONS> | Classified<typeof OPENAI_RESPONSES_PARSE_OPTIONS>,
    OptionKeys<OpenAI["responses"]["parse"]>
  >
>;

export type AnthropicMessages = Expect<
  Equal<Classified<typeof ANTHROPIC_REQUEST_OPTIONS>, OptionKeys<Anthropic["messages"]["create"]>>
>;
export type AnthropicStream = Expect<
  Equal<Classified<typeof ANTHROPIC_REQUEST_OPTIONS>, OptionKeys<Anthropic["messages"]["stream"]>>
>;

export type MistralChat = Expect<Equal<Classified<typeof MISTRAL_REQUEST_OPTIONS>, OptionKeys<Mistral["chat"]["complete"]>>>;
export type MistralChatStream = Expect<
  Equal<Classified<typeof MISTRAL_REQUEST_OPTIONS>, OptionKeys<Mistral["chat"]["stream"]>>
>;
export type MistralFim = Expect<Equal<Classified<typeof MISTRAL_REQUEST_OPTIONS>, OptionKeys<Mistral["fim"]["complete"]>>>;
export type MistralFimStream = Expect<
  Equal<Classified<typeof MISTRAL_REQUEST_OPTIONS>, OptionKeys<Mistral["fim"]["stream"]>>
>;
export type MistralAgents = Expect<
  Equal<Classified<typeof MISTRAL_REQUEST_OPTIONS>, OptionKeys<Mistral["agents"]["complete"]>>
>;
export type MistralAgentsStream = Expect<
  Equal<Classified<typeof MISTRAL_REQUEST_OPTIONS>, OptionKeys<Mistral["agents"]["stream"]>>
>;
export type MistralConversations = Expect<
  Equal<Classified<typeof MISTRAL_REQUEST_OPTIONS>, OptionKeys<Mistral["beta"]["conversations"]["start"]>>
>;

export type GeminiParameters = Expect<Equal<Classified<typeof GEMINI_PARAMETERS>, keyof GenerateContentParameters>>;
export type GeminiHttpOptions = Expect<Equal<Classified<typeof GEMINI_HTTP_OPTIONS>, keyof HttpOptions>>;
export type GeminiConfig = Expect<Equal<Classified<typeof GEMINI_CONFIG_FIELDS>, keyof GenerateContentConfig>>;

// A derived client is closed like a call: every construction option
// `withOptions()` takes is classified, and a new one fails the build too.
// The openai map classifies AzureOpenAI's constructor options too, since its
// inherited withOptions() passes them through: the two unions must both hold.
type AzureOnly = "apiVersion" | "azureADTokenProvider" | "deployment" | "endpoint";
export type OpenAIDerivation = Expect<
  Equal<Exclude<Classified<typeof OPENAI_DERIVATION_OPTIONS>, AzureOnly>, DerivationKeys<OpenAI["withOptions"]>>
>;
export type AzureDerivation = Expect<Equal<Classified<typeof OPENAI_DERIVATION_OPTIONS>, keyof AzureClientOptions>>;
export type AnthropicDerivation = Expect<
  Equal<Classified<typeof ANTHROPIC_DERIVATION_OPTIONS>, DerivationKeys<Anthropic["withOptions"]>>
>;

// ── the tripwire trips ───────────────────────────────────────────────────────
//
// `@ts-expect-error` fails the build when the line below it compiles. So these
// prove that `Equal` notices an option added by a library, and one that
// disappeared, instead of trusting that a hand-written comparison is strict.

// @ts-expect-error a library option the map does not classify must fail the build
export type AddedOption = Expect<Equal<Classified<typeof OPENAI_REQUEST_OPTIONS>, OptionKeys<OpenAI["chat"]["completions"]["create"]> | "someNewOption">>;

// @ts-expect-error a classified option the library no longer has must fail the build too
export type RemovedOption = Expect<Equal<Classified<typeof MISTRAL_REQUEST_OPTIONS> | "someOldOption", OptionKeys<Mistral["chat"]["complete"]>>>;
