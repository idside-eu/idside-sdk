// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * The typing half of enforcement: what a wrapped client's call sites demand.
 *
 * `consent.wrap()` requires an identity — `user_id` or `ubt` — on every AI call
 * it intercepts, and no provider's own types know that field exists. Under
 * strict TypeScript that made the identity an excess property on an object
 * literal, so the documented workaround was a cast at every call site:
 *
 * ```ts
 * await client.chat.completions.create({ … } as unknown as Parameters<…>[0]);
 * ```
 *
 * That cast is worse than friction. `as unknown as` silences the check on the
 * WHOLE body, so a misspelled `mdoel` or a malformed `messages` stops being a
 * compile error too — the developer pays a cast and loses the type checking it
 * was supposed to protect. What follows removes the cast and gives that
 * checking back, on the one condition that matters: forgetting the identity has
 * to fail at compile time, never at run time.
 *
 * Three approaches were tried; two are recorded here because they look right.
 *
 * 1. **Augmenting the provider's own parameter types** (`declare module`) is
 *    global — it would teach `user_id` to EVERY OpenAI client in the program,
 *    wrapped or not — and it can only add the field as optional. An optional
 *    identity is exactly the failure this must prevent.
 * 2. **The obvious mapped type**, `(body: B, …r: R) => Ret` inferred once, keeps
 *    only the LAST overload of an overloaded method. On
 *    `chat.completions.create` that collapses three signatures into the base
 *    one, whose return type is `ChatCompletion | Stream<…>`: every caller then
 *    has to narrow a union the provider had already narrowed for them.
 * 3. **What this file does**: rewrite each overload separately, and replace the
 *    method rather than intersecting with it.
 *
 * The replacement is the part with a visible cost. Intersecting (`T & {…}`)
 * would preserve the provider's own class identity, but an intersection can
 * only ADD signatures — the original identity-less ones would remain callable,
 * and forgetting `user_id` would still compile. Replacing means `Enforced<T>`
 * is a structural type where `T` was a class, so it no longer carries `T`'s
 * private fields and is no longer assignable to `T`. Annotate the wrapped
 * client `Enforced<OpenAI>` rather than `OpenAI`; that is the honest name for
 * it, since it is a client that demands more than an OpenAI client does.
 */

import type {
  ANTHROPIC_REQUEST_OPTIONS,
  MISTRAL_REQUEST_OPTIONS,
  OPENAI_REQUEST_OPTIONS,
} from "./adapters/callOptions.ts";

/**
 * Exactly one of the two identities, never both and never neither.
 *
 * `ubt?: never` is what makes naming the user twice a compile error instead of
 * a `ConflictingIdentityError` at run time. The names mirror every descriptor's
 * `identity_param` / `ubt_param`, and `enforcedTyping.test.ts` fails the build
 * if a descriptor ever chooses different ones — a type cannot read a JSON file,
 * so the only thing standing between the two is that test.
 */
export type Identity = { user_id: string; ubt?: never } | { ubt: string; user_id?: never };

/** Marks a leaf of {@link CALL_SITE_TREE}: a method that carries the identity. */
const CALL = 1;
type Call = typeof CALL;

/**
 * The enforced call sites, as a shape TypeScript can walk.
 *
 * It is a hand-written mirror of `packages/descriptors/` — types cannot read
 * the descriptor JSON, whose string values are just `string` to the compiler.
 * That makes it a copy, and a copy drifts, so it is a runtime value rather than
 * a bare `type`: `enforcedTyping.test.ts` walks it against the loaded
 * descriptors and fails when one gains a call site the other has not.
 *
 * Every provider's paths live in ONE tree because the transform only rewrites
 * keys a given client actually has: `chat.completions` is OpenAI's and
 * `chat.complete` is Mistral's, and neither client sees the other's.
 *
 * `stream` is here even though this SDK deliberately does not patch it (see
 * `UNPATCHABLE_ALIASES` in consent.ts): the helper forwards its one body object
 * to the `create` that IS patched, so a `stream()` call without an identity
 * fails at run time just the same. The type says what the call requires, not
 * which function this SDK replaced.
 */
export const CALL_SITE_TREE = {
  // openai, deepseek — `chat.completions.*`; mistral — `chat.complete`/`chat.stream`
  chat: { completions: { create: CALL, parse: CALL, stream: CALL }, complete: CALL, stream: CALL },
  responses: { create: CALL, parse: CALL, stream: CALL },
  messages: { create: CALL, parse: CALL, stream: CALL },
  // gemini's JS spelling: the descriptor records the Python one (see JS_CALL_SITE_PATHS)
  models: { generateContent: CALL, generateContentStream: CALL },
  fim: { complete: CALL, stream: CALL },
  agents: { complete: CALL, stream: CALL },
  beta: { conversations: { start: CALL } },
} as const;

type CallSiteTree = typeof CALL_SITE_TREE;

/**
 * The per-request options an enforced call accepts, in every library at once.
 *
 * Derived from the classification the SDK applies at run time, so the two
 * cannot disagree: an option `callOptions.ts` refuses is not a key the compiler
 * offers. A key another library accepts (`retries` is Mistral's) is harmless
 * here, because it is only kept where the method's own type already has it.
 */
type Accepted<M> = { [K in keyof M]: M[K] extends "refused" ? never : K }[keyof M];
type AcceptedOption =
  | Accepted<typeof OPENAI_REQUEST_OPTIONS>
  | Accepted<typeof ANTHROPIC_REQUEST_OPTIONS>
  | Accepted<typeof MISTRAL_REQUEST_OPTIONS>;

/** The options parameter, narrowed to what an enforced call accepts. A method without one keeps none. */
type AcceptedOptions<Rest extends readonly unknown[]> = Rest extends readonly []
  ? Rest
  : Rest extends readonly [options?: infer O, ...tail: infer Tail]
    ? [options?: Pick<NonNullable<O>, Extract<keyof NonNullable<O>, AcceptedOption>>, ...tail: Tail]
    : Rest;

/**
 * Add the identity to a signature's first parameter, and narrow the options after it.
 *
 * Only the body carries the identity, because only the body is what the SDK
 * reads it out of. The options object is narrowed to what an enforced call
 * accepts, so `{ body }`, `fetchOptions` or `serverURL` written on the call is a
 * compile error rather than an `UnenforceableOptionError`. It is only a first
 * line: TypeScript checks excess keys on an object literal, not on an options
 * object built elsewhere, and a reserved header name is a string like any
 * other. The run-time check is the one that holds.
 */
type WithIdentityParam<A extends readonly unknown[]> = A extends readonly [infer Body, ...infer Rest]
  ? [body: Body & Identity, ...rest: AcceptedOptions<Rest>]
  : A;

/**
 * Rewrite every overload of a method so its body also carries the identity.
 *
 * TypeScript can only infer from an overloaded type through a pattern with the
 * same number of call signatures, and it has no way to iterate them — hence the
 * ladder from four down to one. Four covers every method in the tree today
 * (three is the most any provider uses: non-streaming, streaming, and the base
 * signature they share) and leaves room for one more without a second look.
 *
 * A generic signature — `parse`, `stream` — loses its type parameters here,
 * which sounds worse than it is: `Parameters<typeof parse>[0]`, what the cast
 * this replaces was written against, erased them too. The parsed payload comes
 * back as `unknown` instead of `null`, and the body is type-checked where it
 * was not. There is no way around it: the type system cannot map over a generic
 * signature's own parameters.
 */
type WithIdentity<F> = F extends {
  (...a: infer A1): infer R1;
  (...a: infer A2): infer R2;
  (...a: infer A3): infer R3;
  (...a: infer A4): infer R4;
}
  ? {
      (...a: WithIdentityParam<A1>): R1;
      (...a: WithIdentityParam<A2>): R2;
      (...a: WithIdentityParam<A3>): R3;
      (...a: WithIdentityParam<A4>): R4;
    }
  : F extends { (...a: infer A1): infer R1; (...a: infer A2): infer R2; (...a: infer A3): infer R3 }
    ? {
        (...a: WithIdentityParam<A1>): R1;
        (...a: WithIdentityParam<A2>): R2;
        (...a: WithIdentityParam<A3>): R3;
      }
    : F extends { (...a: infer A1): infer R1; (...a: infer A2): infer R2 }
      ? { (...a: WithIdentityParam<A1>): R1; (...a: WithIdentityParam<A2>): R2 }
      : F extends (...a: infer A1) => infer R1
        ? (...a: WithIdentityParam<A1>) => R1
        : F;

/** The keys of `T` this branch of the tree has something to say about. */
type Enforcing<T, Node> = Extract<keyof T, keyof Node>;

/**
 * Walk one client (or resource) against one branch of the tree.
 *
 * A node with nothing to say gives `T` back untouched — same type, same
 * identity, no structural copy. That is what keeps `client.models.list()` and
 * every other unenforced surface exactly as the provider typed it, and it is
 * why one tree can serve five providers.
 */
type Apply<T, Node> = [Enforcing<T, Node>] extends [never]
  ? T
  : Omit<T, Enforcing<T, Node>> & {
      [K in Enforcing<T, Node>]: Node[K & keyof Node] extends Call
        ? WithIdentity<T[K]>
        : Apply<T[K], Node[K & keyof Node]>;
    };

/**
 * A provider client as `consent.wrap()` hands it back: same object, stricter type.
 *
 * Every declared call site now demands `user_id` or `ubt` on its body and keeps
 * its own overloads and return types; everything else is untouched. Use it to
 * annotate a wrapped client, or to type a function that receives one:
 *
 * ```ts
 * const client: Enforced<OpenAI> = consent.wrap(new OpenAI());
 * async function summarise(client: Enforced<OpenAI>, userId: string) { … }
 * ```
 */
export type Enforced<T> = Apply<T, CallSiteTree>;
