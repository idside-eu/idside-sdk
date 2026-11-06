/**
 * Credential routing — how an account setting becomes a per-person one.
 *
 * No provider lets a developer switch training off per request. The setting
 * lives on the CREDENTIAL: per project at OpenAI, per workspace at Anthropic,
 * per project at Gemini, per organisation at Mistral. A developer holding one
 * key therefore imposes one posture on all of their users at once, however
 * carefully each of them answered.
 *
 * Unless they hold several. If they provision one credential per posture, the
 * SDK can send each call on the one matching that user's answer, and the
 * account-level control becomes a personal one. It is the only route by which a
 * training opt-out can be honoured per user today.
 *
 * Two rules shape the design: the developer labels their credentials and stops
 * there (which posture an answer requires is policy, and policy is the cloud's
 * — invariant #1), and a posture nobody provisioned is a refusal rather than a
 * fallback (falling back would send the call on a credential the user's answer
 * excludes, and nobody would ever see it happen).
 *
 * Mirrors packages/python/tests/test_credential_routing.py.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { wrap } from "../src/consent.ts";
import { GenericAdapter, GeminiAdapter } from "../src/adapters/index.ts";
import { loadDescriptors } from "../src/descriptors.ts";
import { CredentialRoutingError, MissingCredentialError, IDSideError } from "../src/exceptions.ts";
import type { AuthorizeRequest, ConsentClient, ConsentDecision } from "../src/transport.ts";

const OPENAI = loadDescriptors().get("openai")!;
const GEMINI = loadDescriptors().get("gemini")!;

/** Fictitious project ids, in the shape OpenAI issues them. */
const DEFAULT_PROJECT = "proj_medhelp_default";
const ZDR_PROJECT = "proj_medhelp_zdr";

interface Recorded {
  body: Record<string, unknown>;
  options: Record<string, unknown> | undefined;
}

function fakeOpenAI(name = "base") {
  const calls: Recorded[] = [];
  const create = async (body: Record<string, unknown>, options?: Record<string, unknown>) => {
    calls.push({ body, options });
    return { id: `resp_${name}` };
  };
  return { client: { chat: { completions: { create } } }, calls };
}

function fakeAnthropic(name = "base") {
  const calls: Recorded[] = [];
  const create = async (body: Record<string, unknown>, options?: Record<string, unknown>) => {
    calls.push({ body, options });
    return { id: `msg_${name}` };
  };
  return { client: { messages: { create } }, calls };
}

function fakeGemini(name = "base") {
  const calls: Recorded[] = [];
  const generateContent = async (body: Record<string, unknown>) => {
    calls.push({ body, options: undefined });
    return { text: name };
  };
  return { client: { models: { generateContent } }, calls };
}

/** The cloud, answering with a posture the SDK must honour. */
function fakeConsentClient(posture?: string): ConsentClient & { calls: AuthorizeRequest[] } {
  const calls: AuthorizeRequest[] = [];
  return {
    calls,
    async authorize(request: AuthorizeRequest): Promise<ConsentDecision> {
      calls.push(request);
      return {
        consentState: { "training.foundation": false },
        paramsForced: { store: false },
        actToken: "tok_1",
        source: "widget",
        legalBasis: "consent",
        binding: "user_id",
        constraints: {},
        credentialPosture: posture,
      };
    },
  };
}

function headersOf(call: Recorded): Record<string, unknown> {
  return (call.options?.headers ?? {}) as Record<string, unknown>;
}

// ── Routing by header: OpenAI validates OpenAI-Project per request ─────────

test("a call with no posture goes out exactly as before", async () => {
  // Most calls are not routed. Nothing about them changes.
  const { client, calls } = fakeOpenAI();
  const wrapped = wrap(client, {
    consentClient: fakeConsentClient(),
    provider: "openai",
    credentials: { default: DEFAULT_PROJECT, zero_retention: ZDR_PROJECT },
  });

  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" });

  assert.ok(!("OpenAI-Project" in headersOf(calls[0]!)));
});

test("the cloud's posture selects the project", async () => {
  const { client, calls } = fakeOpenAI();
  const wrapped = wrap(client, {
    consentClient: fakeConsentClient("zero_retention"),
    provider: "openai",
    credentials: { default: DEFAULT_PROJECT, zero_retention: ZDR_PROJECT },
  });

  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" });

  assert.equal(headersOf(calls[0]!)["OpenAI-Project"], ZDR_PROJECT);
});

test("the project travels with the ACT token, not instead of it", async () => {
  // Routing is added enforcement, never a substitute for the proof.
  const { client, calls } = fakeOpenAI();
  const wrapped = wrap(client, {
    consentClient: fakeConsentClient("zero_retention"),
    provider: "openai",
    credentials: { zero_retention: ZDR_PROJECT },
  });

  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" });

  assert.equal(headersOf(calls[0]!)["X-AI-Consent-Token"], "tok_1");
  assert.equal(headersOf(calls[0]!)["OpenAI-Project"], ZDR_PROJECT);
  assert.equal(calls[0]!.body.store, false);
});

test("the developer's own headers survive routing", async () => {
  const { client, calls } = fakeOpenAI();
  const wrapped = wrap(client, {
    consentClient: fakeConsentClient("zero_retention"),
    provider: "openai",
    credentials: { zero_retention: ZDR_PROJECT },
  });

  await wrapped.chat.completions.create(
    { model: "gpt-4o", messages: [], user_id: "usr_marie_456" },
    { headers: { "X-Request-Id": "req_42" } },
  );

  assert.equal(headersOf(calls[0]!)["X-Request-Id"], "req_42");
});

// ── Fail-closed: a posture nobody provisioned is a refusal ────────────────

test("a posture the developer never provisioned stops the call", async () => {
  // The whole point. Silence here would send the call on the wrong project.
  const { client, calls } = fakeOpenAI();
  const wrapped = wrap(client, {
    consentClient: fakeConsentClient("zero_retention"),
    provider: "openai",
    credentials: { default: DEFAULT_PROJECT },
  });

  await assert.rejects(
    wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" }),
    MissingCredentialError,
  );
  assert.deepEqual(calls, []);
});

test("a posture with no credentials at all stops the call", async () => {
  const { client, calls } = fakeOpenAI();
  const wrapped = wrap(client, { consentClient: fakeConsentClient("zero_retention"), provider: "openai" });

  await assert.rejects(
    wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" }),
    MissingCredentialError,
  );
  assert.deepEqual(calls, []);
});

test("the refusal names the posture, the provider and what exists", async () => {
  const { client } = fakeOpenAI();
  const wrapped = wrap(client, {
    consentClient: fakeConsentClient("zero_retention"),
    provider: "openai",
    credentials: { default: DEFAULT_PROJECT },
  });

  let error!: MissingCredentialError;
  try {
    await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" });
  } catch (caught) {
    error = caught as MissingCredentialError;
  }

  assert.equal(error.posture, "zero_retention");
  assert.equal(error.provider, "openai");
  assert.deepEqual(error.provisioned, ["default"]);
  assert.match(error.message, /zero_retention/);
  assert.match(error.message, /project/); // the unit, so "provision one" means something
  // A credential is a secret. It does not belong in an exception.
  assert.ok(!error.message.includes(DEFAULT_PROJECT));
});

test("MissingCredentialError is a routing error is an IDSideError", () => {
  // Catching the base class is enough to fail closed.
  const error = new MissingCredentialError("x", { posture: "p", provider: "openai" });

  assert.ok(error instanceof CredentialRoutingError);
  assert.ok(error instanceof IDSideError);
});

// ── What the cloud is told ────────────────────────────────────────────────

test("the provisioned postures are advertised to the cloud, sorted", async () => {
  // The cloud cannot tell a developer what is missing without knowing this, and
  // two processes with the same configuration must send the same payload.
  const { client } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, {
    consentClient: cc,
    provider: "openai",
    credentials: { zero_retention: ZDR_PROJECT, default: DEFAULT_PROJECT },
  });

  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" });

  assert.deepEqual(cc.calls[0]!.credentialPostures, ["default", "zero_retention"]);
});

test("an app without routing still says it could route", async () => {
  // Empty is a different answer from absent: it says this SDK understands. An
  // older one sends no such field and would ignore a posture it was given —
  // exactly when the cloud must not count on routing.
  const { client } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" });

  assert.deepEqual(cc.calls[0]!.credentialPostures, []);
});

test("the credential itself never reaches the cloud", async () => {
  const { client } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, {
    consentClient: cc,
    provider: "openai",
    credentials: { zero_retention: ZDR_PROJECT },
  });

  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" });

  assert.ok(!JSON.stringify(cc.calls[0]).includes(ZDR_PROJECT));
});

test("the credential never reaches the request hash", () => {
  // The proof binds the request the developer wrote, not our plumbing.
  const adapter = new GenericAdapter(OPENAI);

  assert.equal(
    adapter.extractRequestMetadata({ model: "gpt-4o", messages: [] }).requestHash,
    adapter.extractRequestMetadata({
      model: "gpt-4o",
      messages: [],
      extra_headers: { "OpenAI-Project": ZDR_PROJECT },
    }).requestHash,
  );
});

// ── Routing by client: everywhere the header does not exist ───────────────

test("a provider without a routing header refuses a bare identifier", () => {
  // Anthropic reads no such header: a string here would be silently ignored.
  const { client } = fakeAnthropic();

  assert.throws(
    () =>
      wrap(client, {
        consentClient: fakeConsentClient(),
        provider: "anthropic",
        credentials: { no_training: "ws_medhelp_private" },
      }),
    (error: Error) =>
      error instanceof CredentialRoutingError &&
      /workspace/.test(error.message) &&
      /client/.test(error.message),
  );
});

test("a second client carries the second posture", async () => {
  const base = fakeAnthropic("base");
  const priv = fakeAnthropic("private");
  const wrapped = wrap(base.client, {
    consentClient: fakeConsentClient("no_training"),
    provider: "anthropic",
    credentials: { default: base.client, no_training: priv.client },
  });

  await wrapped.messages.create({ model: "claude-sonnet-4", messages: [], user_id: "usr_marie" });

  assert.equal(priv.calls.length, 1);
  assert.deepEqual(base.calls, []); // the base client never saw it
});

test("a routed call still carries the token and the forced params", async () => {
  const base = fakeAnthropic("base");
  const priv = fakeAnthropic("private");
  const wrapped = wrap(base.client, {
    consentClient: fakeConsentClient("no_training"),
    provider: "anthropic",
    credentials: { no_training: priv.client },
  });

  await wrapped.messages.create({ model: "claude-sonnet-4", messages: [], user_id: "usr_marie" });

  assert.equal(headersOf(priv.calls[0]!)["X-AI-Consent-Token"], "tok_1");
  // Anthropic injects forced params as body fields: that is the only place the
  // API reads them. The token is the header, and stays one.
  assert.equal(priv.calls[0]!.body.store, false);
});

test("the routed client returns its own answer", async () => {
  const base = fakeAnthropic("base");
  const priv = fakeAnthropic("private");
  const wrapped = wrap(base.client, {
    consentClient: fakeConsentClient("no_training"),
    provider: "anthropic",
    credentials: { no_training: priv.client },
  });

  const result = await wrapped.messages.create({ model: "c", messages: [], user_id: "usr_marie" });

  assert.deepEqual(result, { id: "msg_private" });
});

test("a routed call runs on the credential's own client, not the wrapped one", async () => {
  // Provider clients keep private state on the object that owns the method
  // (`this._client`), so calling it detached would break at runtime rather than
  // route. The receiver travels with the route.
  const base = fakeAnthropic("base");
  const seen: unknown[] = [];
  const priv = {
    id: "private-workspace",
    messages: {
      create(this: unknown) {
        seen.push(this);
        return Promise.resolve({ id: "msg_private" });
      },
    },
  };
  const wrapped = wrap(base.client, {
    consentClient: fakeConsentClient("no_training"),
    provider: "anthropic",
    credentials: { no_training: priv },
  });

  await wrapped.messages.create({ model: "c", messages: [], user_id: "usr_marie" });

  assert.equal(seen[0], priv.messages);
});

test("the base client may be its own posture without looping", async () => {
  // `{ default: client }` is the honest way to say what the base key is. The
  // routed method is captured before the client is patched, so a call routed
  // back to it runs the provider once, not the enforcement twice.
  const base = fakeAnthropic("base");
  const priv = fakeAnthropic("private");
  const wrapped = wrap(base.client, {
    consentClient: fakeConsentClient("default"),
    provider: "anthropic",
    credentials: { default: base.client, no_training: priv.client },
  });

  await wrapped.messages.create({ model: "c", messages: [], user_id: "usr_marie" });

  assert.equal(base.calls.length, 1);
});

test("gemini routes by client too", async () => {
  // @google/genai takes the key at construction; there is no header to set.
  const base = fakeGemini("base");
  const other = fakeGemini("routed");
  const wrapped = wrap(base.client, {
    consentClient: fakeConsentClient("no_training"),
    provider: "gemini",
    credentials: { no_training: other.client },
  });

  await wrapped.models.generateContent({ model: "gemini-2.0-flash", contents: "hi", user_id: "usr_marie" });

  const config = other.calls[0]!.body.config as Record<string, Record<string, Record<string, string>>>;
  assert.equal(config.httpOptions.headers["X-AI-Consent-Token"], "tok_1");
  assert.deepEqual(base.calls, []);
});

// ── Configuration errors, raised where they can still be fixed ────────────

test("a provider that cannot route at all refuses credentials", () => {
  // DeepSeek declares no routing: there is no posture to switch between.
  const { client } = fakeOpenAI();

  assert.throws(
    () =>
      wrap(client, {
        consentClient: fakeConsentClient(),
        provider: "deepseek",
        credentials: { default: DEFAULT_PROJECT },
      }),
    /deepseek/,
  );
});

test("an API key pasted where a project id belongs is refused", () => {
  // The one mistake that would put a secret in an outgoing header.
  const { client } = fakeOpenAI();

  assert.throws(
    () =>
      wrap(client, {
        consentClient: fakeConsentClient(),
        provider: "openai",
        credentials: { zero_retention: "sk-test-not-a-project-id" },
      }),
    (error: Error) =>
      error instanceof CredentialRoutingError && !error.message.includes("sk-test-not-a-project-id"),
  );
});

test("a credential carrying a newline is refused", () => {
  // A routed value is written into an HTTP header. CRLF is header injection.
  const { client } = fakeOpenAI();

  assert.throws(
    () =>
      wrap(client, {
        consentClient: fakeConsentClient(),
        provider: "openai",
        credentials: { zero_retention: "proj_ok\r\nX-Injected: 1" },
      }),
    CredentialRoutingError,
  );
});

test("an alternative client missing the call site is refused at wrap time", () => {
  // Better a startup error than a routed call that finds nothing to call.
  const base = fakeAnthropic();

  assert.throws(
    () =>
      wrap(base.client, {
        consentClient: fakeConsentClient(),
        provider: "anthropic",
        credentials: { no_training: {} },
      }),
    /no_training/,
  );
});

test("a client of another provider is refused as a credential", () => {
  // The shapes coincide often enough for this to be a real mistake, and it
  // would send the prompt to a company the user's answer never mentioned.
  const base = fakeAnthropic();

  assert.throws(
    () =>
      wrap(base.client, {
        consentClient: fakeConsentClient(),
        provider: "anthropic",
        credentials: { no_training: fakeGemini().client },
      }),
    CredentialRoutingError,
  );
});

test("an already wrapped client is refused as a credential", () => {
  // Routing to it would authorize twice and stack two tokens on one call.
  const base = fakeAnthropic("base");
  const other = fakeAnthropic("other");
  wrap(other.client, { consentClient: fakeConsentClient(), provider: "anthropic" });

  assert.throws(
    () =>
      wrap(base.client, {
        consentClient: fakeConsentClient(),
        provider: "anthropic",
        credentials: { no_training: other.client },
      }),
    /already/,
  );
});

test("an empty posture label is refused", () => {
  const { client } = fakeOpenAI();

  assert.throws(
    () =>
      wrap(client, {
        consentClient: fakeConsentClient(),
        provider: "openai",
        credentials: { "": DEFAULT_PROJECT },
      }),
    CredentialRoutingError,
  );
});

test("routing does not mutate the caller's arguments", async () => {
  const { client } = fakeOpenAI();
  const wrapped = wrap(client, {
    consentClient: fakeConsentClient("zero_retention"),
    provider: "openai",
    credentials: { zero_retention: ZDR_PROJECT },
  });
  const options: Record<string, unknown> = {};

  await wrapped.chat.completions.create(
    { model: "gpt-4o", messages: [], user_id: "usr_marie_456" },
    options,
  );

  assert.deepEqual(options, {});
});

// ── The mechanism inside the adapters ─────────────────────────────────────

test("the generic adapter carries a routed header", async () => {
  const args = new GenericAdapter(OPENAI).buildEnforcedArgs([{}], {}, "tok_1", {
    "OpenAI-Project": "proj_x",
  });

  assert.equal((args[1] as { headers: Record<string, string> }).headers["OpenAI-Project"], "proj_x");
});

test("the gemini adapter carries a routed header", async () => {
  // Nothing routes by header at Gemini today; an adapter that dropped the
  // argument would still be a hole the day one does.
  const args = new GeminiAdapter(GEMINI).buildEnforcedArgs([{}], {}, "tok_1", {
    "OpenAI-Project": "proj_x",
  });

  const body = args[0] as { config: { httpOptions: { headers: Record<string, string> } } };
  assert.equal(body.config.httpOptions.headers["OpenAI-Project"], "proj_x");
});

test("an adapter that swallows the routed header fails closed", async () => {
  // The last line of defence: never send a call routing was meant to move.
  class Forgetful extends GenericAdapter {
    override buildEnforcedArgs(args: unknown[], paramsForced: Record<string, unknown>, token: string): unknown[] {
      return super.buildEnforcedArgs(args, paramsForced, token);
    }
  }
  const { client, calls } = fakeOpenAI();
  const wrapped = wrap(client, {
    consentClient: fakeConsentClient("zero_retention"),
    adapter: new Forgetful(OPENAI),
    credentials: { zero_retention: ZDR_PROJECT },
  });

  await assert.rejects(
    wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" }),
    CredentialRoutingError,
  );
  assert.deepEqual(calls, []);
});

test("a bare array of credentials is refused", () => {
  // An array carries no posture, and a posture is the whole point.
  const { client } = fakeOpenAI();

  assert.throws(
    () =>
      wrap(client, {
        consentClient: fakeConsentClient(),
        provider: "openai",
        credentials: [DEFAULT_PROJECT, ZDR_PROJECT] as unknown as Record<string, unknown>,
      }),
    CredentialRoutingError,
  );
});

// ── The refusal, where nothing was labelled at all ───────────────────────────

test("a posture is refused even by a client that provisioned nothing", async () => {
  // No `credentials` means no router, so the refusal has to be produced without
  // one. Sending the call anyway would put this user under a privacy regime
  // they declined, and nothing anywhere would record that it happened.
  const { client, calls } = fakeOpenAI();
  const cc = fakeConsentClient("zero_retention");
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  await assert.rejects(
    wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" }),
    (error: unknown) =>
      error instanceof MissingCredentialError &&
      error.posture === "zero_retention" &&
      error.provider === "openai" &&
      /provisioned: none/.test(String(error)),
  );
  assert.equal(calls.length, 0, "the AI call must not have been made");
});

test("the refusal speaks the provider's own vocabulary", async () => {
  // "another project" at OpenAI, "another organisation" at Mistral. A developer
  // reading the message has to know which thing to go and provision.
  const { client } = fakeOpenAI();
  const wrapped = wrap(client, {
    consentClient: fakeConsentClient("zero_retention"),
    provider: "openai",
    credentials: { default: DEFAULT_PROJECT },
  });

  await assert.rejects(
    wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" }),
    (error: unknown) =>
      error instanceof MissingCredentialError &&
      /another project would place this user/.test(String(error)) &&
      /provisioned: 'default'/.test(String(error)),
  );
});

test("a provider that declares no routing still refuses, calling it a credential", async () => {
  // DeepSeek declares no credential routing at all, so `wrap({ credentials })`
  // is refused outright — which means the only way a posture can arrive is with
  // no router to answer it. The word has to degrade to something true rather
  // than to `undefined`, because the sentence is read by a human.
  const { client, calls } = fakeOpenAI();
  const wrapped = wrap(client, {
    consentClient: fakeConsentClient("zero_retention"),
    provider: "deepseek",
  });

  await assert.rejects(
    wrapped.chat.completions.create({ model: "deepseek-chat", messages: [], user_id: "usr_marie_456" }),
    (error: unknown) =>
      error instanceof MissingCredentialError &&
      /another credential would place this user/.test(String(error)) &&
      !/undefined/.test(String(error)),
  );
  assert.equal(calls.length, 0);
});

// ── An alternative client that is not the client it claims to be ─────────────

test("an alternative client whose endpoint is not a function is refused at wrap()", async () => {
  // `create` present but not callable resolves structurally and would only be
  // found out on the first routed call, in production, on the user whose answer
  // required the other credential.
  const { client } = fakeOpenAI();

  assert.throws(
    () =>
      wrap(client, {
        consentClient: fakeConsentClient(),
        provider: "openai",
        credentials: { default: client, zero_retention: { chat: { completions: { create: "soon" } } } },
      }),
    (error: unknown) =>
      error instanceof CredentialRoutingError && /does not expose 'chat.completions.create'/.test(String(error)),
  );
});

test("an alternative client whose path runs into a primitive is refused at wrap()", () => {
  const { client } = fakeOpenAI();

  for (const broken of [
    { chat: "completions" },
    { chat: null },
    { chat: { completions: null } },
    { chat: { completions: 42 } },
    {},
  ]) {
    assert.throws(
      () =>
        wrap(client, {
          consentClient: fakeConsentClient(),
          provider: "openai",
          credentials: { default: client, zero_retention: broken },
        }),
      CredentialRoutingError,
      JSON.stringify(broken),
    );
  }
});

// ── A descriptor that routes by header but pins no shape for the value ───────
//
// OpenAI pins `^proj_…$`, and that pattern is what stops a developer pasting an
// API key into the project header. A provider that routes by header without
// pinning a shape still gets the fallback guard, and the fallback is not a
// formality: the value is written into an outgoing HTTP header, so a CR or LF
// in it is header injection on the developer's own provider request.

const UNPATTERNED = {
  ...OPENAI,
  credentialRouting: { header: "X-Account-Id", unit: "account" },
};

function withUnpatternedRouting(credential: string) {
  const { client } = fakeOpenAI();
  return () =>
    wrap(client, {
      adapter: new GenericAdapter(UNPATTERNED),
      consentClient: fakeConsentClient(),
      credentials: { zero_retention: credential },
    });
}

test("without a pinned pattern, a printable identifier is accepted", () => {
  assert.doesNotThrow(withUnpatternedRouting("account_medhelp_zdr"));
});

test("without a pinned pattern, a CR or LF is still refused", () => {
  // Header injection on the developer's own provider request.
  for (const value of ["acct\r\nX-Other: 1", "acct\nX-Other: 1", "acct with space", ""]) {
    assert.throws(
      withUnpatternedRouting(value),
      CredentialRoutingError,
      JSON.stringify(value),
    );
  }
});

test("a refused value is never echoed back, pattern or no pattern", () => {
  // If what was pasted is an API key, it is a secret, and repeating it in an
  // error message puts it in a log (invariant #7).
  const secret = "sk-a-key-that-should-never-be-repeated"; // pragma: allowlist secret

  assert.throws(withUnpatternedRouting(`${secret}\n`), (error: unknown) => {
    assert.ok(error instanceof CredentialRoutingError);
    assert.ok(!String(error).includes(secret), "the refused credential was echoed back");
    return true;
  });

  const { client } = fakeOpenAI();
  assert.throws(
    () =>
      wrap(client, {
        consentClient: fakeConsentClient(),
        provider: "openai",
        credentials: { zero_retention: secret },
      }),
    (error: unknown) => {
      assert.ok(error instanceof CredentialRoutingError);
      assert.ok(!String(error).includes(secret));
      assert.match(String(error), /expected \^\(\?:\^proj_/, "the expected shape is the one thing it may say");
      return true;
    },
  );
});

test("without a pinned pattern the refusal names no expected shape", () => {
  // There is nothing true to say, and inventing one would send a developer
  // looking for a rule that does not exist.
  assert.throws(withUnpatternedRouting("acct with space"), (error: unknown) => {
    assert.ok(error instanceof CredentialRoutingError);
    assert.ok(!/expected/.test(String(error)));
    return true;
  });
});

// ── credentials that are not a posture → credential map at all ───────────────

test("a single credential passed as a bare string is refused, and named", () => {
  // `Object.keys("proj_x")` is not empty, so this reaches the router and has to
  // be refused there. The message names what was actually passed.
  const { client } = fakeOpenAI();

  assert.throws(
    () =>
      wrap(client, {
        consentClient: fakeConsentClient(),
        provider: "openai",
        credentials: DEFAULT_PROJECT as unknown as Record<string, unknown>,
      }),
    (error: unknown) =>
      error instanceof CredentialRoutingError &&
      /not string\b/.test(String(error)) &&
      !/an array/.test(String(error)),
  );
});

test("a bare list of credentials is refused, and told why", () => {
  // An array carries no posture, which is the one thing the label exists for.
  // The message has to say "an array" and not "object", because `typeof []` is
  // exactly the answer that would leave the developer re-reading their code.
  const { client } = fakeOpenAI();

  assert.throws(
    () =>
      wrap(client, {
        consentClient: fakeConsentClient(),
        provider: "openai",
        credentials: [DEFAULT_PROJECT, ZDR_PROJECT] as unknown as Record<string, unknown>,
      }),
    (error: unknown) =>
      error instanceof CredentialRoutingError &&
      /not an array\b/.test(String(error)) &&
      /a bare list of credentials would carry no posture/.test(String(error)),
  );
});

test("a hostile alternative client is not mistaken for an unidentifiable one", () => {
  // "Unidentifiable proves nothing" is deliberately narrow: it swallows exactly
  // UnsupportedProviderError. Anything else coming out of detection is a client
  // actively resisting inspection, and swallowing that would accept it.
  const { client } = fakeOpenAI();
  const hostile = new Proxy(
    {},
    {
      get(): never {
        throw new RangeError("this client refuses to be inspected");
      },
    },
  );

  assert.throws(
    () =>
      wrap(client, {
        consentClient: fakeConsentClient(),
        provider: "openai",
        credentials: { zero_retention: hostile },
      }),
    RangeError,
  );
});

// ── An alias is its call site on the wire, but not at the routing table ──────

test("a routed alias lands on the alternative client's alias, not on its create", () => {
  // `chat.completions.parse` travels to the cloud as `chat.completions.create`
  // — same endpoint, same name in the ledger. The routing table is keyed by the
  // METHOD PATH anyway: keyed by call-site id, the two would collide, and a
  // routed `parse()` would be answered by the other client's `create()` and
  // hand the caller an unparsed response.
  const landed: string[] = [];
  const resource = (name: string) => ({
    create: async (_body: Record<string, unknown>, _options?: unknown) => (
      landed.push(`${name}.create`), { id: "resp" }
    ),
    parse: async (_body: Record<string, unknown>, _options?: unknown) => (
      landed.push(`${name}.parse`), { id: "resp", parsed: true }
    ),
  });
  const base = { chat: { completions: resource("base.chat") }, responses: resource("base.responses") };
  const other = { chat: { completions: resource("routed.chat") }, responses: resource("routed.responses") };

  const wrapped = wrap(base, {
    consentClient: fakeConsentClient("zero_retention"),
    provider: "openai",
    credentials: { zero_retention: other },
  });

  return wrapped.chat.completions.parse({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" }).then(() => {
    assert.deepEqual(landed, ["routed.chat.parse"], "the routed call landed on the wrong method");
  });
});

test("an adapter that drops the routed credential stops the call", () => {
  // The adapter is what places headers on a call, and a custom one is free to
  // override `buildEnforcedArgs`. Verifying rather than trusting turns the one
  // failure that would otherwise be invisible — a call leaving on the very
  // credential routing was meant to move it off — into an error.
  class ForgetfulAdapter extends GenericAdapter {
    override buildEnforcedArgs(args: unknown[], forced: Record<string, unknown>, token: string): unknown[] {
      return super.buildEnforcedArgs(args, forced, token);
    }
  }
  const { client, calls } = fakeOpenAI();
  const wrapped = wrap(client, {
    adapter: new ForgetfulAdapter(OPENAI),
    consentClient: fakeConsentClient("zero_retention"),
    credentials: { zero_retention: ZDR_PROJECT },
  });

  return assert
    .rejects(
      (wrapped as never as { chat: { completions: { create(b: unknown): Promise<unknown> } } }).chat.completions.create(
        { model: "gpt-4o", messages: [], user_id: "usr_marie_456" },
      ),
      (error: unknown) =>
        error instanceof CredentialRoutingError &&
        /did not carry the routed credential onto this call/.test(String(error)),
    )
    .then(() => {
      assert.equal(calls.length, 0, "the call went out on the wrong credential");
    });
});
