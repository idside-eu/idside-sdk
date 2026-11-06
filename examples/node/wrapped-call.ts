/**
 * A consent-enforced OpenAI call, from `wrap()` to the wire.
 *
 * Runs anywhere, with no ID Side account and no OpenAI bill. Two things are
 * replaced by stand-ins, and only two: the ID Side cloud, which would sign the
 * proof, and OpenAI's endpoint, which would answer it. Everything between them
 * is the real SDK doing the real work — finding the call site the descriptor
 * declares, hashing the request, consuming the identity, forcing the retention
 * parameter the decision names, attaching the proof, and refusing anything that
 * did not go through enforcement.
 *
 * It is a `.ts` file on purpose: the identity is part of the type, so the call
 * below compiles under `strict` with no cast, and forgetting `user_id` would
 * not compile at all. `npm run typecheck` is what checks that; running the file
 * only strips the types.
 *
 * Run it:  node wrapped-call.ts
 * Live:    see ../README.md#going-live — it is a two-line diff.
 */

import OpenAI from "openai";
import {
  consent,
  type AuthorizeRequest,
  type ConsentClient,
  type ConsentDecision,
  type Enforced,
} from "@idside/sdk";

// Fictitious throughout, as everything in this repository's examples is.
const USER = "usr_marie_456";
const MODEL = "gpt-4o-mini";
const PROMPT = "Summarize my last consultation.";

/**
 * Stands in for idside.eu.
 *
 * The SDK reads a decision; it never constructs one, so a stand-in only has to
 * return the same shape. The real one arrives as JSON from `POST /v1/authorize`
 * and its `actToken` is signed by a key that exists only in the ID Side cloud —
 * which is the reason this file cannot make one.
 */
const fakeCloud: ConsentClient = {
  async authorize({
    provider,
    model,
    requestHash,
    callSite,
    userId,
    ubt,
    credentialPostures,
  }: AuthorizeRequest): Promise<ConsentDecision> {
    console.log("2. What ID Side was asked");
    console.log(`   provider=${provider} model=${model} callSite=${callSite}`);
    console.log(`   requestHash=${requestHash}`);
    console.log(`   userId=${userId} ubt=${ubt}`);
    console.log(`   credentialPostures=${JSON.stringify(credentialPostures ?? [])}`);
    console.log("   ...and nothing else. The prompt is not in this request.");
    console.log();
    console.log("3. What ID Side answered");
    console.log("   consentState={ training: false, 'retention.conversation': false }");
    console.log("   paramsForced={ store: false }   <- the SDK applies it, not you");
    console.log();
    return {
      // What this person answered. The SDK reads it; it can never write it.
      consentState: { training: false, "retention.conversation": false },
      // What that answer forces on THIS endpoint. The cloud derives it from the
      // descriptor, so `chat.completions.create` and `responses.create` do not
      // get the same list.
      paramsForced: { store: false },
      // What to refuse rather than set — `{ tools: "refuse" }` would make a call
      // carrying tools throw ToolUseRefusedError.
      constraints: {},
      actToken: "act.eyJhbGciOiJFUzI1NiJ9.fake.signature",
      source: "widget",
      legalBasis: "consent",
      binding: "user_id",
      // Which of the developer's credentials must carry the call. Undefined
      // here: this app holds one key, so one posture for everyone.
      credentialPosture: undefined,
    };
  },
};

/** Stands in for api.openai.com, and prints what actually left. */
const openaiStub: typeof fetch = async (_input, init) => {
  const headers = new Headers(init?.headers);
  console.log("4. What actually left this process");
  console.log(`   POST https://api.openai.com/v1/chat/completions`);
  console.log(`   X-AI-Consent-Token: ${headers.get("X-AI-Consent-Token")}`);
  console.log(`   body: ${String(init?.body)}`);
  console.log();
  return new Response(
    JSON.stringify({
      id: "chatcmpl_1",
      object: "chat.completion",
      created: 0,
      model: MODEL,
      choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
};

// `Enforced<OpenAI>`, not `OpenAI`: a wrapped client demands the identity that
// an unwrapped one does not, and the type says so. Annotating it `OpenAI` would
// be the compiler agreeing to a call that throws.
const client: Enforced<OpenAI> = consent.wrap(
  new OpenAI({
    apiKey: "sk-test-not-a-real-key",
    fetch: openaiStub,
    maxRetries: 0,
  }),
  { consentClient: fakeCloud },
);

console.log("1. The call the developer writes");
console.log("   await client.chat.completions.create({");
console.log(`     model: ${JSON.stringify(MODEL)},`);
console.log(`     messages: [{ role: "user", content: ${JSON.stringify(PROMPT)} }],`);
console.log(`     user_id: ${JSON.stringify(USER)},`);
console.log("   });");
console.log();

const response = await client.chat.completions.create({
  model: MODEL,
  messages: [{ role: "user", content: PROMPT }],
  user_id: USER, // required by the type: omitting it does not compile
});

console.log("5. What the provider replied");
console.log(`   ${JSON.stringify(response.choices[0]?.message.content)}`);
console.log();
console.log("Three things to read twice:");
console.log("   - the cloud was given a hash, never the prompt;");
console.log("   - store=false — ID Side's decision — reached OpenAI's request body;");
console.log("   - user_id did NOT. The SDK consumed it; the provider never sees it.");
