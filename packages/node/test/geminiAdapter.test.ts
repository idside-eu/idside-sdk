/**
 * The Gemini adapter, where the caller's `config` already exists.
 *
 * `models.generateContent` takes ONE object and no free-form kwargs, so the
 * adapter has nowhere to put the ACT token except nested inside the caller's own
 * `config.httpOptions.headers`. Every other provider gets a second argument the
 * SDK owns outright; here the SDK writes into a structure the application built
 * and still holds a reference to.
 *
 * That makes two questions load-bearing, and neither was asked before:
 *
 * - **What happens to a config that is already there?** A `config` carrying
 *   `temperature` or `systemInstruction` is the normal case, not an exotic one.
 *   Losing it would silently change the developer's request; mutating it in
 *   place would rewrite an object they still hold.
 * - **What happens to a config the SDK cannot read?** `asDict` accepts a plain
 *   object or anything exposing `toJSON()`, and refuses everything else rather
 *   than guessing. A guess here injects the token into a copy nobody sends, and
 *   the call goes out unproven.
 *
 * Mirrors the Gemini half of packages/python/tests/test_adapters.py.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { GeminiAdapter } from "../src/adapters/index.ts";
import { loadDescriptors } from "../src/descriptors.ts";
import { ConsentTokenError } from "../src/exceptions.ts";

const GEMINI = loadDescriptors().get("gemini")!;
const TOKEN = "act.jwt.token"; // pragma: allowlist secret

/** The nested header map the adapter is supposed to have built. */
function headersOf(args: unknown[]): Record<string, string> {
  const body = args[0] as {
    config?: { httpOptions?: { headers?: Record<string, string> } };
  };
  return body.config?.httpOptions?.headers ?? {};
}

function build(
  body: unknown,
  forced: Record<string, unknown> = {},
  credentialHeaders?: Record<string, string>,
): unknown[] {
  return new GeminiAdapter(GEMINI).buildEnforcedArgs(
    [body],
    forced,
    TOKEN,
    credentialHeaders,
  );
}

// ── a config the caller already built ────────────────────────────────────────

test("an existing config keeps everything the caller put in it", () => {
  // The forced params are merged INTO the config, next to what was there. A
  // config that came back holding only `store` would have dropped the
  // developer's own request without saying so.
  const args = build(
    {
      model: "gemini-2.0-flash",
      contents: "hi",
      config: { temperature: 0.5, systemInstruction: "be brief" },
    },
    { store: false },
  );

  const config = (args[0] as { config: Record<string, unknown> }).config;
  assert.equal(config.temperature, 0.5);
  assert.equal(config.systemInstruction, "be brief");
  assert.equal(config.store, false, "the cloud's forced param lands here too");
  assert.equal(headersOf(args)["X-AI-Consent-Token"], TOKEN);
});

test("the caller's own config object is never written through", () => {
  // A wrapped client must not rewrite an object the application still holds: a
  // config reused across two calls would accumulate the first call's token.
  const config: Record<string, unknown> = { temperature: 0.5 };
  const body = { model: "gemini-2.0-flash", contents: "hi", config };

  const args = build(body, { store: false });

  assert.deepEqual(config, { temperature: 0.5 }, "the caller's config was mutated");
  assert.equal((body as { config: unknown }).config, config, "the body was rewritten");
  assert.notEqual((args[0] as { config: unknown }).config, config);
});

test("existing httpOptions headers survive, and the ACT token joins them", () => {
  const args = build({
    model: "gemini-2.0-flash",
    contents: "hi",
    config: { httpOptions: { timeout: 30_000, headers: { "X-Trace-Id": "trace_42" } } },
  });

  const httpOptions = (args[0] as { config: { httpOptions: Record<string, unknown> } })
    .config.httpOptions;
  assert.equal(httpOptions.timeout, 30_000, "unrelated httpOptions must survive");
  assert.equal(headersOf(args)["X-Trace-Id"], "trace_42", "the caller's header must survive");
  assert.equal(headersOf(args)["X-AI-Consent-Token"], TOKEN);
});

test("a caller header under the token's name is replaced, not kept beside it", () => {
  // Two values for one header name is how an unproven token rides along beside
  // the real one. `mergeHeaders` drops the caller's spelling of the name.
  const args = build({
    model: "gemini-2.0-flash",
    contents: "hi",
    config: { httpOptions: { headers: { "x-ai-consent-token": "act.forged.token" } } },
  });

  const headers = headersOf(args);
  assert.equal(headers["X-AI-Consent-Token"], TOKEN);
  assert.equal(headers["x-ai-consent-token"], undefined, "a second spelling survived");
});

// ── a config the SDK cannot read ─────────────────────────────────────────────

test("a config exposing toJSON() is read through it", () => {
  // Provider SDKs hand back typed objects, and a typed config is a legitimate
  // thing to pass. `toJSON()` is the documented way to ask one what it holds.
  class TypedConfig {
    toJSON(): Record<string, unknown> {
      return { temperature: 0.2 };
    }
  }
  const args = build({ model: "gemini-2.0-flash", config: new TypedConfig() }, { store: false });

  const config = (args[0] as { config: Record<string, unknown> }).config;
  assert.equal(config.temperature, 0.2);
  assert.equal(config.store, false);
  assert.equal(headersOf(args)["X-AI-Consent-Token"], TOKEN);
});

test("nested httpOptions exposing toJSON() is read through it too", () => {
  class TypedHttpOptions {
    toJSON(): Record<string, unknown> {
      return { timeout: 1_000 };
    }
  }
  const args = build({
    model: "gemini-2.0-flash",
    config: { httpOptions: new TypedHttpOptions() },
  });

  const httpOptions = (args[0] as { config: { httpOptions: Record<string, unknown> } })
    .config.httpOptions;
  assert.equal(httpOptions.timeout, 1_000);
  assert.equal(headersOf(args)["X-AI-Consent-Token"], TOKEN);
});

test("a config that is neither a plain object nor readable stops the call", () => {
  // The alternative is injecting the token into a copy nobody sends, so the
  // provider call leaves with no proof on it and nothing says so.
  class Opaque {
    readonly temperature = 0.5;
  }
  assert.throws(
    () => build({ model: "gemini-2.0-flash", config: new Opaque() }),
    (error: unknown) =>
      error instanceof ConsentTokenError && /config is neither a plain object/.test(String(error)),
  );
});

test("a toJSON() that does not yield an object stops the call too", () => {
  // Accepting it would mean assigning the forced params onto a string.
  class Lying {
    toJSON(): string {
      return "config";
    }
  }
  assert.throws(
    () => build({ model: "gemini-2.0-flash", config: new Lying() }),
    ConsentTokenError,
  );
});

test("an unreadable httpOptions is named as httpOptions, not as config", () => {
  // The developer has to know which of the two objects to fix.
  class Opaque {}
  assert.throws(
    () => build({ model: "gemini-2.0-flash", config: { httpOptions: new Opaque() } }),
    (error: unknown) =>
      error instanceof ConsentTokenError && /config\.httpOptions is neither/.test(String(error)),
  );
});

// ── the body itself ──────────────────────────────────────────────────────────

test("a body that is not a plain object still leaves with a token on it", () => {
  // Nothing may reach the provider unproven, including a call shaped in a way
  // the adapter did not expect. The token is what makes the request legible to
  // the net; a body without one is refused at the transport.
  const args = build(undefined);

  assert.equal(headersOf(args)["X-AI-Consent-Token"], TOKEN);
});

test("protected params never reach the config, even forced", () => {
  // `model` is the request; the cloud decides retention, not what was asked.
  const args = build(
    { model: "gemini-2.0-flash", contents: "hi" },
    { model: "gemini-HACKED", store: false },
  );

  const body = args[0] as { model: string; config: Record<string, unknown> };
  assert.equal(body.model, "gemini-2.0-flash");
  assert.equal(body.config.model, undefined, "a protected param was injected into config");
  assert.equal(body.config.store, false);
});

test("a routed credential header is carried alongside the token", () => {
  // Nothing routes by header at Gemini today, and an adapter that dropped the
  // argument would only be found out the day one does.
  const args = build({ model: "gemini-2.0-flash" }, {}, { "X-Goog-Project": "proj_medhelp" });

  assert.equal(headersOf(args)["X-Goog-Project"], "proj_medhelp");
  assert.equal(headersOf(args)["X-AI-Consent-Token"], TOKEN);
});

test("trailing arguments are passed through untouched", () => {
  const tail = { signal: "abort-signal-stand-in" };
  const args = new GeminiAdapter(GEMINI).buildEnforcedArgs(
    [{ model: "gemini-2.0-flash" }, tail],
    {},
    TOKEN,
  );

  assert.equal(args.length, 2);
  assert.equal(args[1], tail);
});
