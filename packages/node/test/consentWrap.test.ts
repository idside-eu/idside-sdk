import { test } from "node:test";
import assert from "node:assert/strict";
import { wrap } from "../src/consent.ts";
import {
  HttpConsentClient,
  isBlank,
  type ConsentClient,
  type ConsentDecision,
  type AuthorizeRequest,
} from "../src/transport.ts";
import {
  AuthenticationError,
  BindingRequiredError,
  ConflictingIdentityError,
  ConsentDeniedError,
  ConsentRefreshRequiredError,
  ConsentUnreachableError,
  ContractError,
  IDSideError,
  InvalidBindingError,
  MissingIdentityError,
  UnsupportedProviderError,
} from "../src/exceptions.ts";
import { POLICY_HOLD } from "./cloudProblems.ts";

// A forced set that also tries to overwrite `model` — a protected param the cloud
// must never be able to override.
const DECISION: ConsentDecision = {
  consentState: { training: false },
  paramsForced: { store: false, model: "HACKED" },
  actToken: "act.jwt.token",
  source: "widget",
  legalBasis: "consent",
  binding: "ubt",
  constraints: {},
};

function fakeConsentClient(
  decision: ConsentDecision = DECISION,
): ConsentClient & { calls: AuthorizeRequest[] } {
  const calls: AuthorizeRequest[] = [];
  return {
    calls,
    async authorize(request: AuthorizeRequest): Promise<ConsentDecision> {
      calls.push(request);
      return decision;
    },
  };
}

// Records both positional arguments, like the real openai/anthropic/mistral
// `create(body, options?)` signature.
function fakeOpenAI() {
  const calls: {
    body: Record<string, unknown>;
    options: { headers?: Record<string, string> } | undefined;
  }[] = [];
  const create = async (
    body: Record<string, unknown>,
    options?: { headers?: Record<string, string> },
  ) => {
    calls.push({ body, options });
    return { id: "resp_1" };
  };
  return {
    client: { chat: { completions: { create } }, responses: { create } },
    calls,
  };
}

test("forces params in the body, carries the ACT token in options.headers, strips identity", async () => {
  const { client, calls } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  await wrapped.chat.completions.create({
    model: "gpt-4o",
    messages: [{ role: "user", content: "hi" }],
    user_id: "usr_marie_456",
  });

  const call = calls[0]!;
  assert.equal(
    call.body.user_id,
    undefined,
    "identity must be stripped from the body",
  );
  assert.equal(
    call.body.store,
    false,
    "kwargs-target forced param goes in the body",
  );
  assert.equal(
    call.body.model,
    "gpt-4o",
    "protected param must not be overridden",
  );
  assert.equal(
    call.body.extra_headers,
    undefined,
    "the token must not leak into the body",
  );
  assert.equal(call.options?.headers?.["X-AI-Consent-Token"], "act.jwt.token");

  assert.equal(cc.calls[0]!.provider, "openai");
  assert.equal(cc.calls[0]!.userId, "usr_marie_456");
  assert.match(cc.calls[0]!.requestHash, /^sha256:[0-9a-f]{64}$/);
});

test("merges the token into a caller-supplied options.headers without dropping it", async () => {
  const { client, calls } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  await wrapped.chat.completions.create(
    { model: "gpt-4o", messages: [], user_id: "u" },
    { headers: { "X-Trace-Id": "abc" } },
  );

  const headers = calls[0]!.options?.headers ?? {};
  assert.equal(headers["X-Trace-Id"], "abc", "caller header must survive");
  assert.equal(headers["X-AI-Consent-Token"], "act.jwt.token");
});

test("preserves caller headers passed as a Headers instance (not just a plain object)", async () => {
  const { client, calls } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  await wrapped.chat.completions.create(
    { model: "gpt-4o", messages: [], user_id: "u" },
    {
      headers: new Headers({ "X-Trace-Id": "abc" }),
    } as unknown as { headers?: Record<string, string> },
  );

  const headers = calls[0]!.options?.headers ?? {};
  assert.equal(
    headers["x-trace-id"],
    "abc",
    "caller Headers must not be clobbered",
  ); // Headers lowercases keys
  assert.equal(headers["X-AI-Consent-Token"], "act.jwt.token");
});

test("requires exactly one identity, making no cloud query or AI call otherwise", async () => {
  const missing = fakeOpenAI();
  const ccMissing = fakeConsentClient();
  const wMissing = wrap(missing.client, {
    consentClient: ccMissing,
    provider: "openai",
  });
  await assert.rejects(
    // @ts-expect-error omitting the identity is a compile error; this pins the runtime one
    wMissing.chat.completions.create({ model: "gpt-4o", messages: [] }),
    MissingIdentityError,
  );

  const conflict = fakeOpenAI();
  const ccConflict = fakeConsentClient();
  const wConflict = wrap(conflict.client, {
    consentClient: ccConflict,
    provider: "openai",
  });
  await assert.rejects(
    // @ts-expect-error naming the user twice is a compile error; this pins the runtime one
    wConflict.chat.completions.create({
      model: "gpt-4o",
      messages: [],
      user_id: "u",
      ubt: "eyJ",
    }),
    ConflictingIdentityError,
  );

  assert.equal(
    ccMissing.calls.length + ccConflict.calls.length,
    0,
    "no authorize call",
  );
  assert.equal(
    missing.calls.length + conflict.calls.length,
    0,
    "no provider call",
  );
});

test("is fail-closed: an unreachable cloud stops the provider call", async () => {
  const { client, calls } = fakeOpenAI();
  const cc: ConsentClient = {
    async authorize() {
      throw new ConsentUnreachableError("cloud down");
    },
  };
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });
  await assert.rejects(
    wrapped.chat.completions.create({
      model: "gpt-4o",
      messages: [],
      user_id: "u",
    }),
    ConsentUnreachableError,
  );
  assert.equal(calls.length, 0, "provider must not be called");
});

test("a 200 deny response never reaches the provider", async () => {
  const { client, calls } = fakeOpenAI();
  const cloud = new HttpConsentClient("https://cloud.test", {
    fetch: async () =>
      new Response(
        JSON.stringify({
          decision: "deny",
          consent_state: {},
          params_forced: {},
          mode: "live",
          act_token: "act.jwt.token",
          token_id: "act_01k4f9h2m3",
          expires_at: "2026-09-12T12:00:00Z",
          source: "widget",
          legal_basis: "consent",
          binding: "user_id",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
  });
  const wrapped = wrap(client, { consentClient: cloud, provider: "openai" });

  await assert.rejects(
    wrapped.chat.completions.create({
      model: "gpt-4o",
      messages: [],
      user_id: "u",
    }),
    ConsentUnreachableError,
  );

  assert.equal(calls.length, 0, "provider must not be called");
});

// Every refusal the cloud can qualify, and the ones it cannot, end the call
// before the provider: a class that tells the developer what to do is worth
// nothing if the call it describes went out anyway. An organization policy's hold is
// one of them, with the reason that says it was the policy and not the account.
const REFUSALS: [string, number, unknown, new (...a: never[]) => Error, string?][] = [
  ["401-key", 401, {}, AuthenticationError],
  ["401-ubt-invalid", 401, { type: "https://idside.eu/errors/ubt_invalid" }, InvalidBindingError],
  ["401-ubt-required", 401, { type: "https://idside.eu/errors/ubt_required" }, BindingRequiredError],
  ["401-unknown-type", 401, { type: "https://idside.eu/errors/ubt_expired" }, AuthenticationError],
  ["403-policy-hold", 403, POLICY_HOLD, ConsentDeniedError, "policy_hold"],
  [
    "409-refresh-required",
    409,
    { type: "https://idside.eu/errors/consent_refresh_required" },
    ConsentRefreshRequiredError,
  ],
  ["409-other", 409, { type: "https://idside.eu/errors/consent_not_collected" }, ConsentUnreachableError],
];

test("a qualified refusal never reaches the provider", async () => {
  for (const [label, status, problem, ErrorType, reason] of REFUSALS) {
    const { client, calls } = fakeOpenAI();
    const cloud = new HttpConsentClient("https://cloud.test", {
      fetch: async () =>
        new Response(JSON.stringify(problem), {
          status,
          headers: { "content-type": "application/problem+json" },
        }),
    });
    const wrapped = wrap(client, { consentClient: cloud, provider: "openai" });

    await assert.rejects(
      wrapped.chat.completions.create({
        model: "gpt-4o",
        messages: [],
        user_id: "usr_marie_456",
      }),
      (error: unknown) => {
        assert.equal((error as Error).constructor, ErrorType, label);
        assert.equal((error as { reason?: string }).reason, reason, label);
        return true;
      },
    );
    assert.equal(calls.length, 0, `${label}: provider must not be called`);
  }
});

test("is idempotent: wrapping twice enforces exactly once", async () => {
  const { client, calls } = fakeOpenAI();
  const cc = fakeConsentClient();
  const once = wrap(client, { consentClient: cc, provider: "openai" });
  const twice = wrap(once, { consentClient: cc, provider: "openai" });
  await twice.chat.completions.create({
    model: "gpt-4o",
    messages: [],
    user_id: "u",
  });
  assert.equal(calls.length, 1);
  assert.equal(cc.calls.length, 1);
});

test("rejects an unknown provider client", () => {
  const cc = fakeConsentClient();
  assert.throws(
    () => wrap({ nothing: {} }, { consentClient: cc }),
    UnsupportedProviderError,
  );
});

// `inference_geo` is a named parameter of `messages.create` and travels in the
// JSON BODY. This descriptor injected into headers until 2026-07-26, which put
// it on the wire and applied nothing — worse than not sending it, because it
// looked enforced. The token is a separate mechanism (`token_header`) and still
// rides in a header. It is used here as a body parameter and nothing more: no
// option forces it since `data_residency` was downgraded to `account` on
// 2026-07-27, and Anthropic's allowlist is empty — what this pins is the
// injection mechanics, which outlive any one parameter. Mirrors
// test_engine.py::test_apply_injects_params_into_the_body_for_anthropic.
test("anthropic injects forced params into the body, the token into a header", async () => {
  const calls: {
    body: Record<string, unknown>;
    options?: { headers?: Record<string, string> };
  }[] = [];
  const client = {
    messages: {
      create: async (
        body: Record<string, unknown>,
        options?: { headers?: Record<string, string> },
      ) => {
        calls.push({ body, options });
        return { id: "msg_1" };
      },
    },
  };
  const cc = fakeConsentClient({
    ...DECISION,
    paramsForced: { inference_geo: "us" },
  });
  const wrapped = wrap(client, { consentClient: cc, provider: "anthropic" });

  await wrapped.messages.create({
    model: "claude-3",
    messages: [],
    user_id: "u",
  });

  assert.equal(
    calls[0]!.body["inference_geo"],
    "us",
    "the body is where Anthropic reads it",
  );
  const headers = calls[0]!.options?.headers ?? {};
  assert.equal(headers["inference_geo"], undefined, "no longer a header");
  assert.equal(headers["X-AI-Consent-Token"], "act.jwt.token");
});

test("gemini carries the token in config.httpOptions.headers (single-arg call)", async () => {
  let sent: Record<string, unknown> = {};
  const client = {
    models: {
      generateContent: async (body: Record<string, unknown>) => {
        sent = body;
        return { text: "hi" };
      },
    },
  };
  const cc = fakeConsentClient({ ...DECISION, paramsForced: {} });
  const wrapped = wrap(client, { consentClient: cc, provider: "gemini" });

  await wrapped.models.generateContent({
    model: "gemini-2.0-flash",
    contents: [],
    user_id: "u",
  });

  const config = sent.config as {
    httpOptions: { headers: Record<string, string> };
  };
  assert.equal(
    config.httpOptions.headers["X-AI-Consent-Token"],
    "act.jwt.token",
  );
});

// ── P1.12: the cloud is told WHICH endpoint is about to be called ───────────

test("each enforced endpoint names itself on authorize", async () => {
  // Without this the cloud can only answer per provider, and a parameter that
  // exists on one endpoint has to be dropped everywhere (losing the lever) or
  // forced everywhere (breaking the call for exactly the users who opted out).
  const { client } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  await wrapped.chat.completions.create({
    model: "gpt-4o",
    messages: [],
    user_id: "usr_a",
  });
  await wrapped.responses.create({
    model: "gpt-4o",
    input: "hi",
    user_id: "usr_b",
  });

  assert.deepEqual(
    cc.calls.map((c) => c.callSite),
    ["chat.completions.create", "responses.create"],
  );
});

test("the call site follows the endpoint, not the order of the call", async () => {
  const { client } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  await wrapped.responses.create({
    model: "gpt-4o",
    input: "hi",
    user_id: "usr_b",
  });
  await wrapped.chat.completions.create({
    model: "gpt-4o",
    messages: [],
    user_id: "usr_a",
  });

  assert.deepEqual(
    cc.calls.map((c) => c.callSite),
    ["responses.create", "chat.completions.create"],
  );
});

// ── the UBT: the other way a call names its end user ─────────────────────────

test("a call named by a UBT is authorized on the UBT, and the UBT never travels on", async () => {
  // `user_id` is a developer's assertion; a UBT is the widget's, signed and
  // verified by the cloud, and it is what makes the proof worth more. Both are
  // consumed by the SDK — forwarding either to the provider would hand them an
  // identifier the user never agreed to share with them.
  const { client, calls } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });
  const UBT = "eyJhbGciOiJFUzI1NiJ9.dXNyX21hcmllXzQ1Ng.sig"; // pragma: allowlist secret

  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], ubt: UBT } as never);

  assert.equal(cc.calls[0]!.ubt, UBT);
  assert.equal(cc.calls[0]!.userId, undefined, "a UBT call must not also assert a user_id");
  assert.equal(calls[0]!.body.ubt, undefined, "the UBT reached the provider");
  assert.equal(calls[0]!.body.user_id, undefined);
});

test("an identity that is present but empty is no identity at all", async () => {
  // "", 0 and false are absent in both SDKs. A falsy `user_id` reaching the
  // cloud as the literal "" would mint a proof naming nobody.
  const { client, calls } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  for (const empty of ["", 0, false, null, undefined]) {
    await assert.rejects(
      wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: empty } as never),
      MissingIdentityError,
      String(empty),
    );
  }
  assert.equal(cc.calls.length, 0);
  assert.equal(calls.length, 0);
});

test("a body that is not an object is refused for want of an identity", async () => {
  // Nothing can be read out of it, so nothing names the user, so the call does
  // not happen. What must not happen is the SDK inventing a body of its own and
  // sending it.
  const { client, calls } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  for (const body of ["hi", 42, null, undefined, ["hi"]]) {
    await assert.rejects(
      wrapped.chat.completions.create(body as never),
      MissingIdentityError,
      String(body),
    );
  }

  // An array carrying a `user_id` property is the case that tells "read as a
  // request body" from "spread whatever this is": no provider takes an array as
  // a request, so reading an identity out of one would authorize a call whose
  // body the SDK never understood.
  const arrayBody = ["hi"] as unknown as Record<string, unknown>;
  arrayBody.user_id = "usr_marie_456";
  await assert.rejects(wrapped.chat.completions.create(arrayBody as never), MissingIdentityError);

  assert.equal(cc.calls.length, 0);
  assert.equal(calls.length, 0);
});

// ── a decision from a stand-in that predates a field ─────────────────────────

test("a decision with no constraints field is a decision with no constraints", async () => {
  // TypeScript makes the field mandatory, but the documented way to test
  // without the network is to inject a ConsentClient, and the type system never
  // sees a plain-JavaScript one. An older stand-in must degrade to "no
  // constraint", not to a TypeError three frames inside the SDK.
  const { client, calls } = fakeOpenAI();
  const cc: ConsentClient = {
    async authorize(): Promise<ConsentDecision> {
      return {
        consentState: {},
        paramsForced: { store: false },
        actToken: "act.jwt.token",
        source: "widget",
        legalBasis: "consent",
        binding: "user_id",
      } as unknown as ConsentDecision;
    },
  };
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  await wrapped.chat.completions.create({
    model: "gpt-4o",
    messages: [],
    tools: [{ type: "function" }],
    user_id: "usr_marie_456",
  } as never);

  assert.equal(calls.length, 1);
  assert.equal(calls[0]!.body.store, false);
  assert.deepEqual(calls[0]!.body.tools, [{ type: "function" }], "no constraint means nothing is stripped");
});

// ── clients this SDK cannot enforce ──────────────────────────────────────────

test("a client exposing none of a provider's call sites is refused", async () => {
  // Returning it unwrapped would hand back something that looks enforced and
  // is not. There is no partial answer here: nothing was patched.
  assert.throws(
    () => wrap({}, { consentClient: fakeConsentClient(), provider: "openai" }),
    (error: unknown) =>
      error instanceof UnsupportedProviderError &&
      /None of the call sites for provider 'openai'/.test(String(error)),
  );
});

test("a call site that is present but not callable is skipped, not patched", async () => {
  // `create` resolving to a string is a client shape the SDK cannot enforce at
  // that endpoint. The other endpoints are still enforced, and the unusable one
  // is left exactly as it was rather than replaced by a wrapper that would
  // throw somewhere confusing.
  const { calls } = fakeOpenAI();
  const create = async (body: Record<string, unknown>, options?: Record<string, unknown>) => {
    calls.push({ body, options: options as { headers?: Record<string, string> } | undefined });
    return { id: "resp_1" };
  };
  const client = {
    chat: { completions: { create: "not callable yet" } },
    responses: { create },
  };
  const cc = fakeConsentClient();

  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  assert.equal(client.chat.completions.create, "not callable yet", "an unusable call site was replaced");
  await (wrapped as unknown as typeof client & { responses: { create: typeof create } }).responses.create({
    model: "gpt-4o",
    input: "hi",
    user_id: "usr_marie_456",
  } as never);
  assert.equal(cc.calls.length, 1, "the usable endpoint is still enforced");
});

// ── wrapping a client that is already half wrapped ───────────────────────────

test("a second wrap patches only what the first one did not", async () => {
  // A client that grew an alias between the two calls — or an interrupted first
  // attempt that is being retried. Re-patching an already-enforced method would
  // wrap a wrapper: one call would authorize twice and stack two ACT tokens on
  // one request.
  const calls: string[] = [];
  const completions: Record<string, unknown> = {
    create: async () => (calls.push("create"), { id: "resp_1" }),
  };
  const client: Record<string, unknown> = {
    chat: { completions },
    withOptions: () => ({ chat: { completions } }),
  };
  const cc = fakeConsentClient();

  wrap(client, { consentClient: cc, provider: "openai" });
  const enforcedCreate = completions.create;
  const patchedWithOptions = client.withOptions;

  completions.parse = async (body: Record<string, unknown>, options?: unknown) =>
    (completions.create as (b: unknown, o?: unknown) => Promise<unknown>)(body, options);
  const twice = wrap(client, { consentClient: cc, provider: "openai" });

  assert.equal(completions.create, enforcedCreate, "an already-enforced method was re-patched");
  assert.equal(client.withOptions, patchedWithOptions, "an already-patched factory was re-patched");
  assert.notEqual(completions.parse, undefined);

  await (twice as never as { chat: { completions: { create(b: unknown): Promise<unknown> } } }).chat.completions.create(
    { model: "gpt-4o", messages: [], user_id: "usr_marie_456" },
  );
  assert.equal(cc.calls.length, 1, "one call, one authorize");
  assert.deepEqual(calls, ["create"]);
});

// ── methods called without their object ──────────────────────────────────────

test("an enforced method still works when it is called detached", async () => {
  // `const create = client.chat.completions.create` is ordinary JavaScript, and
  // the provider's own helpers do it. Provider clients keep their transport on
  // `this` (`this._client`), so a wrapper that dropped the receiver would throw
  // instead of calling — and a developer who hits that reaches for an unwrapped
  // client.
  const completions = {
    transport: "the object the method needs",
    seen: [] as Record<string, unknown>[],
    async create(this: { transport?: string; seen: Record<string, unknown>[] }, body: Record<string, unknown>, options?: unknown) {
      if (this?.transport === undefined) throw new TypeError("create() lost its resource object");
      this.seen.push({ body, options });
      return { id: "resp_1" };
    },
  };
  const cc = fakeConsentClient();
  const wrapped = wrap({ chat: { completions } }, { consentClient: cc, provider: "openai" });

  const create = (wrapped as never as { chat: { completions: { create(b: unknown): Promise<unknown> } } }).chat
    .completions.create;
  await create({ model: "gpt-4o", messages: [], user_id: "usr_marie_456" });

  assert.equal(cc.calls.length, 1);
  assert.equal(completions.seen.length, 1, "the detached call never reached the provider");
  assert.equal(
    (completions.seen[0]!.options as { headers: Record<string, string> }).headers["X-AI-Consent-Token"],
    "act.jwt.token",
  );
});

test("a detached withOptions() still hands back an enforced client", async () => {
  const { client, calls } = fakeOpenAI();
  const derived = fakeOpenAI();
  (client as Record<string, unknown>).withOptions = function (this: unknown) {
    // Provider factories read their own options off `this`; one called without
    // a receiver would throw rather than derive.
    if (this === undefined || this === null) throw new TypeError("withOptions() lost its client");
    return derived.client;
  };
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  const withOptions = (wrapped as unknown as { withOptions(): unknown }).withOptions;
  const copy = withOptions() as typeof derived.client;
  await copy.chat.completions.create({
    model: "gpt-4o",
    messages: [],
    user_id: "usr_marie_456",
  } as never);

  assert.equal(cc.calls.length, 1, "the derived client reached the provider unenforced");
  assert.equal(derived.calls.length, 1);
  assert.equal(calls.length, 0);
});

test("a deriving method that hands back something that is not a client is passed through", async () => {
  // `copy()` returning undefined, or a promise, or a number: there is nothing
  // to wrap, and inventing a wrapper around it would break the caller for no
  // security gain — nothing was derived, so nothing can be called unenforced.
  for (const value of [undefined, null, 42, "client"]) {
    const { client } = fakeOpenAI();
    (client as Record<string, unknown>).copy = () => value;
    const wrapped = wrap(client, { consentClient: fakeConsentClient(), provider: "openai" });

    assert.equal((wrapped as unknown as { copy(): unknown }).copy(), value, String(value));
  }
});

// ── a resource that is a function ────────────────────────────────────────────

test("a call site hanging off a callable resource is patched like any other", async () => {
  // Some provider clients make a resource callable and hang its methods off it.
  // Walking only through plain objects would leave that endpoint unenforced and
  // say nothing about it.
  const seen: Record<string, unknown>[] = [];
  const completions = Object.assign(function completions() {}, {
    create: async (body: Record<string, unknown>) => (seen.push(body), { id: "resp_1" }),
  });
  const cc = fakeConsentClient();
  const wrapped = wrap({ chat: { completions } }, { consentClient: cc, provider: "openai" });

  await (wrapped as never as { chat: { completions: { create(b: unknown): Promise<unknown> } } }).chat.completions.create(
    { model: "gpt-4o", messages: [], user_id: "usr_marie_456" },
  );

  assert.equal(cc.calls.length, 1);
  assert.equal(seen[0]!.user_id, undefined, "the identity reached the provider");
});

// ── An identity made of blanks names nobody ─────────────────────────────────
//
// `user_id: "   "` used to leave: only falsy values counted as absent. The cloud
// refused it with a 422, and the SDK's sentence sent the developer to upgrade
// the package. Blank means what the cloud means by it — the 25 code points
// ActiveSupport's `blank?` matches, nothing more; `trim()` disagrees with it on
// U+FEFF and U+0085 — so the SDK stops exactly the values the cloud would
// refuse. Nothing else is trimmed: a `user_id` with spaces around it is sent as
// written, because its pseudonym is derived from those exact characters. The
// Python suite pins the same list.

const BLANK_CODES = [
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x20, 0x85, 0xa0, 0x1680,
  0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a,
  0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
];
const BLANKS = BLANK_CODES.map((code) => String.fromCharCode(code));

test("the blank set is the cloud's and no wider", () => {
  const blank: number[] = [];
  for (let code = 0; code < 0x10000; code += 1) {
    if (isBlank(String.fromCharCode(code))) blank.push(code);
  }
  assert.deepEqual(blank, BLANK_CODES);
  assert.equal(isBlank(""), false, "empty is already absent, and is not a blank");
});

function untouchedCloud(): ConsentClient & { calls: AuthorizeRequest[] } {
  const calls: AuthorizeRequest[] = [];
  return {
    calls,
    async authorize(request: AuthorizeRequest): Promise<ConsentDecision> {
      calls.push(request);
      throw new Error("a blank identity reached the cloud");
    },
  };
}

for (const param of ["user_id", "ubt"] as const) {
  for (const blank of ["   ", "\t\n", "\u00a0", "\u3000\u2028", BLANKS.join("")]) {
    test(`an identity made of blanks is absent (${param} ${JSON.stringify(blank)})`, async () => {
      const { client, calls } = fakeOpenAI();
      const cloud = untouchedCloud();
      const wrapped = wrap(client, { consentClient: cloud, provider: "openai" });

      await assert.rejects(
        wrapped.chat.completions.create({ model: "gpt-4o", messages: [], [param]: blank } as never),
        MissingIdentityError,
      );
      assert.equal(cloud.calls.length, 0);
      assert.equal(calls.length, 0);
    });
  }
}

test("a blank user_id beside a ubt is no conflict", async () => {
  const { client } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "   ", ubt: "eyJ.u.b" } as never);

  assert.equal(cc.calls.at(-1)?.ubt, "eyJ.u.b");
  assert.equal(cc.calls.at(-1)?.userId, undefined);
});

for (const value of [" usr_marie_456 ", "\t usr_marie_456", "\ufeff", "\u200b", "\u001c"]) {
  test(`nothing else is trimmed (${JSON.stringify(value)})`, async () => {
    const { client } = fakeOpenAI();
    const cc = fakeConsentClient();
    const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

    await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: value });

    assert.equal(cc.calls.at(-1)?.userId, value);
  });
}

// ── An identity leaves exactly as the caller wrote it ───────────────────────
//
// It used to go through `String()` on its way to the cloud: `user_id: true` was
// authorized as the user "true", an array as its joined items, and EVERY object
// as one person, "[object Object]" — a JavaScript caller who passed `req.user`
// for `req.user.id` merged all their visitors into one, under one `sub`, and
// nothing said so. The Python SDK sends the value as written, and the cloud is
// the one judge of its type: a non-blank string or a finite number names
// somebody, anything else is a 422. The cloud below applies that rule to what
// actually crossed the wire.

const ALLOW = {
  decision: "allow",
  consent_state: {},
  params_forced: {},
  mode: "test",
  act_token: "act.jwt.token",
  token_id: "act_01k4f9h2m3",
  expires_at: "2026-09-12T12:00:00Z",
  source: "widget",
  legal_basis: "consent",
  binding: "user_id",
};

function judgingCloud(): { cloud: HttpConsentClient; bodies: string[] } {
  const bodies: string[] = [];
  const cloud = new HttpConsentClient("https://cloud.test", {
    fetch: async (_url, init) => {
      const body = String(init?.body);
      bodies.push(body);
      const { user_id: identity } = JSON.parse(body) as { user_id?: unknown };
      const named =
        (typeof identity === "string" && identity.trim() !== "") ||
        (typeof identity === "number" && Number.isFinite(identity));
      return named
        ? new Response(JSON.stringify(ALLOW), { status: 200, headers: { "content-type": "application/json" } })
        : new Response(
            JSON.stringify({
              type: "https://idside.eu/errors/invalid_request",
              title: "Invalid request",
              detail: "user_id must be a non-blank string or a number.",
            }),
            { status: 422, headers: { "content-type": "application/problem+json" } },
          );
    },
  });
  return { cloud, bodies };
}

const sentIdentities = (bodies: string[]): unknown[] =>
  bodies.map((body) => (JSON.parse(body) as { user_id?: unknown }).user_id);

for (const [label, identity] of [
  ["true", true],
  ["an object", { id: "usr_marie_456" }],
  ["an array", ["usr_marie_456"]],
  ["a Map", new Map([["id", "usr_marie_456"]])],
] as const) {
  test(`a user_id that is ${label} reaches the cloud as written, and the cloud refuses it`, async () => {
    const { client, calls } = fakeOpenAI();
    const { cloud, bodies } = judgingCloud();
    const wrapped = wrap(client, { consentClient: cloud, provider: "openai" });

    await assert.rejects(
      wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: identity } as never),
      ContractError,
    );
    assert.deepEqual(sentIdentities(bodies), [JSON.parse(JSON.stringify(identity))]);
    assert.equal(calls.length, 0, "provider must not be called");
  });
}

test("two different objects are never authorized as one person", async () => {
  const { client, calls } = fakeOpenAI();
  const { cloud, bodies } = judgingCloud();
  const wrapped = wrap(client, { consentClient: cloud, provider: "openai" });
  const people = [{ id: "usr_marie_456" }, { id: "usr_paul_789" }];

  for (const identity of people) {
    await assert.rejects(
      wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: identity } as never),
      ContractError,
    );
  }
  assert.deepEqual(sentIdentities(bodies), people);
  assert.equal(calls.length, 0, "provider must not be called");
});

test("a numeric user_id reaches the cloud as a JSON number", async () => {
  const { client, calls } = fakeOpenAI();
  const { cloud, bodies } = judgingCloud();
  const wrapped = wrap(client, { consentClient: cloud, provider: "openai" });

  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: 4242101 } as never);

  assert.match(bodies[0]!, /"user_id":4242101[,}]/);
  assert.equal(calls.length, 1);
});

test("a BigInt user_id is written as the integer it is", async () => {
  // A database driver's 64-bit key comes back as a BigInt, and Python sends the
  // same key as an int: a JSON number, every digit kept. `JSON.stringify` alone
  // throws on one, and `Number()` would round this one.
  const { client, calls } = fakeOpenAI();
  const { cloud, bodies } = judgingCloud();
  const wrapped = wrap(client, { consentClient: cloud, provider: "openai" });

  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: 9007199254740993n } as never);

  assert.match(bodies[0]!, /"user_id":9007199254740993[,}]/);
  assert.equal(calls.length, 1);
});

test("a ubt is forwarded untouched too", async () => {
  const { client } = fakeOpenAI();
  const cc = fakeConsentClient();
  const wrapped = wrap(client, { consentClient: cc, provider: "openai" });

  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [], ubt: 4242101 } as never);

  assert.equal(cc.calls.at(-1)?.ubt, 4242101);
});

for (const param of ["user_id", "ubt"] as const) {
  for (const [label, empty] of [
    ["array", []],
    ["object", {}],
    ["object with no prototype", Object.create(null) as object],
  ] as const) {
    test(`an empty ${label} names nobody, as in Python (${param})`, async () => {
      // Python reads an empty list or dict as it reads "": absent, before any
      // request. Sent as written, it would come back as a 422 instead.
      const { client, calls } = fakeOpenAI();
      const cloud = untouchedCloud();
      const wrapped = wrap(client, { consentClient: cloud, provider: "openai" });

      await assert.rejects(
        wrapped.chat.completions.create({ model: "gpt-4o", messages: [], [param]: empty } as never),
        MissingIdentityError,
      );
      assert.equal(cloud.calls.length, 0);
      assert.equal(calls.length, 0);
    });
  }
}

test("an identity JSON cannot write fails as in Python, not as an outage", async () => {
  // Python's JSON encoder refuses such a value with its own error before any
  // request; this one used to become "the cloud is unreachable".
  const { client, calls } = fakeOpenAI();
  const { cloud, bodies } = judgingCloud();
  const wrapped = wrap(client, { consentClient: cloud, provider: "openai" });
  const circular: Record<string, unknown> = { id: "usr_marie_456" };
  circular.self = circular;

  await assert.rejects(
    wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: circular } as never),
    (error: unknown) => error instanceof TypeError,
  );
  assert.equal(bodies.length, 0, "nothing was sent");
  assert.equal(calls.length, 0);
});

// JSON does not fail on everything it cannot write: it drops a function or a
// symbol and writes a non-finite number as null. As an identity, the first left
// an authorize body naming nobody at all, and the second sent `user_id: null`,
// where Python's encoder refuses both before any request. They fail the same
// way here now, with JSON's own kind of error, and nothing is sent.
for (const [label, identity] of [
  ["a function", () => "usr_marie_456"],
  ["a symbol", Symbol("usr_marie_456")],
  ["Infinity", Number.POSITIVE_INFINITY],
  ["-Infinity", Number.NEGATIVE_INFINITY],
  ["an object holding Infinity", { id: "usr_marie_456", score: Number.POSITIVE_INFINITY }],
  ["an object holding a function", { id: "usr_marie_456", toString: () => "usr_marie_456" }],
] as const) {
  test(`a user_id that is ${label} fails as in Python, and nothing is sent`, async () => {
    const { client, calls } = fakeOpenAI();
    const { cloud, bodies } = judgingCloud();
    const wrapped = wrap(client, { consentClient: cloud, provider: "openai" });

    await assert.rejects(
      wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: identity } as never),
      (error: unknown) => error instanceof TypeError && !(error instanceof IDSideError),
    );
    assert.equal(bodies.length, 0, "nothing was sent");
    assert.equal(calls.length, 0, "provider must not be called");
  });
}
