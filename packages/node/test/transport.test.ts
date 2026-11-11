import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import {
  DEFAULT_BASE_URL,
  DEFAULT_TIMEOUT_MS,
  HttpConsentClient,
  USER_AGENT,
} from "../src/transport.ts";
import * as exceptions from "../src/exceptions.ts";
import * as index from "../src/index.ts";
import {
  AuthenticationError,
  BindingError,
  BindingRequiredError,
  ConflictingIdentityError,
  ConsentDeniedError,
  ConsentRefreshRequiredError,
  ConsentUnreachableError,
  ContractError,
  IDSideError,
  InvalidBindingError,
  MissingIdentityError,
  QuotaExceededError,
  RateLimitedError,
} from "../src/exceptions.ts";
import { POLICY_HOLD } from "./cloudProblems.ts";

// Like the autouse fixture of Python's transport tests: every client here
// chooses its address explicitly, or relies on there being none configured.
// `node --test` runs each file in its own process, so this stays in this file.
delete process.env.IDSIDE_BASE_URL;
delete process.env.IDSIDE_API_KEY;

const DECISION_BODY = {
  decision: "allow",
  consent_state: { training: false },
  params_forced: { store: false },
  mode: "test",
  act_token: "act.jwt.token",
  token_id: "act_01k4f9h2m3",
  expires_at: "2026-09-12T12:00:00Z",
  source: "widget",
  legal_basis: "consent",
  binding: "ubt",
};

const REQUEST = {
  provider: "openai",
  model: "gpt-4o",
  requestHash: "sha256:abc",
  userId: "usr_1",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("returns a ConsentDecision on a well-formed 200", async () => {
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () => jsonResponse(DECISION_BODY),
  });
  const decision = await client.authorize(REQUEST);
  assert.equal(decision.actToken, "act.jwt.token");
  assert.deepEqual(decision.paramsForced, { store: false });
  assert.equal(decision.binding, "ubt");
});

// `legal_basis` is opaque: a value the cloud adds needs no SDK release.
// `public_task`, `legal_obligation` and `vital_interests` arrived that way. A
// closed list here would fail closed on every call an imposed policy signs on a
// basis added after this release — on a field the SDK only carries.
test("every legal basis reaches the decision as the cloud wrote it", async () => {
  for (const legalBasis of [
    "legitimate_interest",
    "contractual_obligation",
    "public_task",
    "legal_obligation",
    "vital_interests",
    "not_established",
    "a_basis_added_later",
  ]) {
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () =>
        jsonResponse({ ...DECISION_BODY, source: "default_policy", legal_basis: legalBasis }),
    });
    const decision = await client.authorize(REQUEST);
    assert.equal(decision.legalBasis, legalBasis);
    assert.equal(decision.source, "default_policy");
  }
});

test("sends user_id or ubt (not both) and the request hash on the wire", async () => {
  let sentBody: Record<string, unknown> = {};
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async (_url, init) => {
      sentBody = JSON.parse(String(init?.body));
      return jsonResponse(DECISION_BODY);
    },
  });
  await client.authorize({
    provider: "openai",
    model: "gpt-4o",
    requestHash: "sha256:xyz",
    ubt: "eyJ.u.b",
  });
  assert.equal(sentBody.ubt, "eyJ.u.b");
  assert.equal(sentBody.user_id, undefined);
  assert.equal(sentBody.request_hash, "sha256:xyz");
});

test("fails closed on a connection error without retrying (authorize is not idempotent)", async () => {
  let attempts = 0;
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () => {
      attempts += 1;
      throw new TypeError("fetch failed");
    },
  });
  await assert.rejects(client.authorize(REQUEST), ConsentUnreachableError);
  assert.equal(
    attempts,
    1,
    "authorize must not be resent — it may already have been recorded",
  );
});

test("fails closed on a timeout without retrying", async () => {
  let attempts = 0;
  const client = new HttpConsentClient("https://cloud.test", {
    timeoutMs: 10,
    fetch: async (_url, init) => {
      attempts += 1;
      // Hang until the client's own timeout aborts the request.
      return await new Promise<Response>((_resolve, reject) => {
        (init?.signal as AbortSignal).addEventListener("abort", () =>
          reject(new Error("aborted")),
        );
      });
    },
  });
  await assert.rejects(client.authorize(REQUEST), ConsentUnreachableError);
  assert.equal(attempts, 1, "a timeout must not be retried");
});

test("authorize() treats a blank identity as absent, with no request", async () => {
  for (const identity of [{ userId: " \t\u3000" }, { ubt: " \t\u3000" }]) {
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () => {
        throw new Error("no HTTP request must be made for a blank identity");
      },
    });
    await assert.rejects(
      client.authorize({ provider: "openai", model: "gpt-4o", requestHash: "sha256:abc", ...identity }),
      MissingIdentityError,
    );
  }
});

test("a 422 says the value may be the caller's own", async () => {
  // The cloud answers 422 for a value the caller passed, too. The sentence used
  // to name one cause — a contract mismatch, upgrade or report a bug — and then
  // quote a cloud detail saying the caller's own value was the problem.
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () =>
      jsonResponse(
        {
          type: "https://idside.eu/errors/invalid_request",
          title: "Invalid request",
          detail: "Provide the user this call is for: `user_id` (or `ubt`, if you use the widget).",
        },
        422,
      ),
  });
  await assert.rejects(client.authorize(REQUEST), (error: unknown) => {
    assert.ok(error instanceof ContractError);
    assert.ok(
      error.message.startsWith(
        "ID Side refused this call's payload (HTTP 422). If the cloud's detail names a value your code " +
          "passed, fix that value; otherwise the SDK and the cloud disagree on the contract — upgrade the " +
          "SDK or report it; the AI call was not made. Cloud says: Invalid request; Provide the user",
      ),
      error.message,
    );
    assert.ok(!error.message.includes("contract mismatch — upgrade"), error.message);
    return true;
  });
});

test("maps error statuses to typed exceptions", async () => {
  const cases: [number, unknown, new (...a: never[]) => Error][] = [
    [401, {}, AuthenticationError],
    [402, {}, QuotaExceededError],
    [422, {}, ContractError],
    [429, {}, RateLimitedError],
    [500, {}, ConsentUnreachableError],
    [418, {}, ConsentUnreachableError],
  ];
  for (const [status, body, ErrorType] of cases) {
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () => jsonResponse(body, status),
    });
    await assert.rejects(
      client.authorize(REQUEST),
      ErrorType,
      `HTTP ${status}`,
    );
  }
});

test("carries the deny reason from a 403 problem+json", async () => {
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () =>
      jsonResponse({ type: "fraud_suspended", title: "App suspended" }, 403),
  });
  await assert.rejects(client.authorize(REQUEST), (error: unknown) => {
    assert.ok(error instanceof ConsentDeniedError);
    assert.equal(error.reason, "fraud_suspended");
    return true;
  });
});

// A denial reason the SDK documents but nothing emits is worse than a missing
// one: a caller writes a branch for it, the branch is never taken, and the dead
// code reads as tested behaviour. The denial still lands — only the explanation
// is withheld, which is what "we could not qualify this" means.
test("leaves provider_blocked unqualified: the cloud does not send it", async () => {
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () =>
      jsonResponse(
        { type: "https://idside.eu/errors/provider_blocked", title: "Blocked" },
        403,
      ),
  });
  await assert.rejects(client.authorize(REQUEST), (error: unknown) => {
    assert.ok(error instanceof ConsentDeniedError);
    assert.equal(error.reason, undefined);
    return true;
  });
});

test("treats a 200 with an unusable body as fail-closed", async () => {
  const malformed = new HttpConsentClient("https://cloud.test", {
    fetch: async () => new Response("not json", { status: 200 }),
  });
  await assert.rejects(malformed.authorize(REQUEST), ConsentUnreachableError);

  const incomplete = new HttpConsentClient("https://cloud.test", {
    fetch: async () => jsonResponse({ act_token: "x" }), // missing consent_state, source…
  });
  await assert.rejects(incomplete.authorize(REQUEST), ConsentUnreachableError);
});

test("rejects invalid decision fields in a 200 response", async () => {
  const cases: [string, unknown][] = [
    ["consent_state", []],
    ["params_forced", []],
    ["decision", "deny"],
    ["decision", true],
    ["decision", {}],
    ["mode", "bogus"],
    ["mode", null],
    ["mode", []],
    ["mode", {}],
    ["act_token", ""],
    ["act_token", "not-a-compact-jws"],
    ["act_token", "abc.$.def"],
    ["token_id", "token_1"],
    ["token_id", {}],
    ["expires_at", "tomorrow"],
    ["expires_at", "2026-02-30T12:00:00Z"],
    ["expires_at", false],
    ["source", 7],
    ["source", ""],
    ["source", "   "],
    ["source", "widget\u0085suffix"],
    ["legal_basis", false],
    ["legal_basis", ""],
    ["legal_basis", "   "],
    ["binding", {}],
    ["binding", "cookie"],
    ["constraints", []],
    ["constraints", { tools: "allow" }],
    ["constraints", { tools: ["refuse"] }],
    ["constraints", { tools: { action: "refuse" } }],
    ["constraints", { tools: false }],
    ["constraints", { tools: null }],
    ["constraints", { future: "refuse" }],
  ];

  for (const [field, value] of cases) {
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () => jsonResponse({ ...DECISION_BODY, [field]: value }),
    });
    await assert.rejects(
      client.authorize(REQUEST),
      ConsentUnreachableError,
      `${field}=${JSON.stringify(value)}`,
    );
  }
});

test("requires every field the OpenAPI authorize response marks required", async () => {
  for (const field of [
    "decision",
    "consent_state",
    "source",
    "legal_basis",
    "binding",
    "params_forced",
    "mode",
    "act_token",
    "token_id",
    "expires_at",
  ]) {
    const body = { ...DECISION_BODY } as Record<string, unknown>;
    delete body[field];
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () => jsonResponse(body),
    });
    await assert.rejects(
      client.authorize(REQUEST),
      ConsentUnreachableError,
      field,
    );
  }
});

// ── P1.12: the endpoint travels to the cloud ────────────────────────────────

test("sends the call site so the cloud can decide per endpoint", async () => {
  let sentBody: Record<string, unknown> = {};
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async (_url, init) => {
      sentBody = JSON.parse(String(init?.body));
      return jsonResponse(DECISION_BODY);
    },
  });
  await client.authorize({ ...REQUEST, callSite: "responses.create" });
  assert.equal(sentBody.call_site, "responses.create");
});

test("omits the call site rather than asserting an empty one", async () => {
  // An empty `call_site` on the wire would look like a real answer; its absence
  // tells the cloud to fall back to what is safe on every endpoint.
  let sentBody: Record<string, unknown> = {};
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async (_url, init) => {
      sentBody = JSON.parse(String(init?.body));
      return jsonResponse(DECISION_BODY);
    },
  });
  await client.authorize(REQUEST);
  assert.ok(!("call_site" in sentBody));
});

// ── Credential routing: what the SDK advertises, and what it accepts back ──

test("always sends credential_postures, empty included", async () => {
  // Empty is a different answer from absent: its presence says this SDK
  // understands routing. An older one omits the field and would ignore a
  // posture it was handed — the one case the cloud must not route for.
  let sentBody: Record<string, unknown> = {};
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async (_url, init) => {
      sentBody = JSON.parse(String(init?.body));
      return jsonResponse(DECISION_BODY);
    },
  });

  await client.authorize(REQUEST);

  assert.deepEqual(sentBody.credential_postures, []);
});

test("advertises the provisioned postures", async () => {
  let sentBody: Record<string, unknown> = {};
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async (_url, init) => {
      sentBody = JSON.parse(String(init?.body));
      return jsonResponse(DECISION_BODY);
    },
  });

  await client.authorize({
    ...REQUEST,
    credentialPostures: ["default", "zero_retention"],
  });

  assert.deepEqual(sentBody.credential_postures, ["default", "zero_retention"]);
});

test("reads the credential posture the cloud named", async () => {
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () =>
      jsonResponse({ ...DECISION_BODY, credential_posture: "zero_retention" }),
  });

  assert.equal(
    (await client.authorize(REQUEST)).credentialPosture,
    "zero_retention",
  );
});

test("an absent or null credential posture means the client as built is fine", async () => {
  const absent = new HttpConsentClient("https://cloud.test", {
    fetch: async () => jsonResponse(DECISION_BODY),
  });
  const explicit = new HttpConsentClient("https://cloud.test", {
    fetch: async () =>
      jsonResponse({ ...DECISION_BODY, credential_posture: null }),
  });

  assert.equal((await absent.authorize(REQUEST)).credentialPosture, undefined);
  assert.equal(
    (await explicit.authorize(REQUEST)).credentialPosture,
    undefined,
  );
});

test("a posture the SDK cannot use fails closed", async () => {
  // Ignoring it would send the call on the credential routing exists to avoid.
  for (const posture of [
    42,
    "",
    "   ",
    ["zero_retention"],
    "x".repeat(201),
    "zero\nretention",
    "zero\u0085retention",
  ]) {
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () =>
        jsonResponse({ ...DECISION_BODY, credential_posture: posture }),
    });

    await assert.rejects(
      client.authorize(REQUEST),
      ConsentUnreachableError,
      `accepted ${JSON.stringify(posture)}`,
    );
  }
});

// ── The reason on a denial, which never once arrived ───────────────────────
//
// The cloud emits RFC 9457 `type` as a URI — `https://idside.eu/errors/<name>` —
// and both SDKs compared it against the bare name. So `reason` was always
// undefined, including for `fraud_suspended`, while docs/errors.md documented a
// table of values and showed callers branching on them. Nobody noticed because
// the denial itself worked: only the explanation was missing.

async function denied(
  problem: Record<string, unknown>,
): Promise<ConsentDeniedError> {
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () => jsonResponse(problem, 403),
  });
  try {
    await client.authorize(REQUEST);
  } catch (error) {
    return error as ConsentDeniedError;
  }
  throw new Error("expected a denial");
}

test("the reason survives the URI form the cloud actually sends", async () => {
  assert.equal(
    (await denied({ type: "https://idside.eu/errors/fraud_suspended" })).reason,
    "fraud_suspended",
  );
});

test("an organization suspension is told apart from a fraud review", async () => {
  // Sending a developer to a fraud review when their organization's account is
  // stopped sends them to the wrong person.
  assert.equal(
    (await denied({ type: "https://idside.eu/errors/organization_suspended" }))
      .reason,
    "organization_suspended",
  );
});

test("a policy hold is told apart from a suspension", async () => {
  // Nothing is wrong with the account: its policy held this one call. A
  // developer sent to billing or to a fraud review is sent to the wrong
  // person; whoever sets the organization's policy is the one who can change it.
  const error = await denied(POLICY_HOLD);

  assert.equal(error.constructor, ConsentDeniedError);
  assert.equal(error.reason, "policy_hold");
  assert.ok(
    error.message.startsWith(
      "The requested AI usage was denied (HTTP 403); the AI call was not made.",
    ),
  );
  assert.match(error.message, /Policy hold/);
  // The cloud's sentence names what held the call; the docs promise it reaches
  // the developer in the message, whole.
  assert.ok(error.message.includes(POLICY_HOLD.detail), error.message);
});

test("the bare form still works", async () => {
  assert.equal(
    (await denied({ type: "fraud_suspended" })).reason,
    "fraud_suspended",
  );
});

test("a reason we do not know stays unqualified", async () => {
  assert.equal(
    (await denied({ type: "https://idside.eu/errors/something_new" })).reason,
    undefined,
  );
  assert.equal((await denied({})).reason, undefined);
});

test("only our own error namespace may name a reason", async () => {
  assert.equal(
    (await denied({ type: "https://evil.example/errors/fraud_suspended" }))
      .reason,
    undefined,
  );
  assert.equal(
    (await denied({ type: "https://idside.eu/other/fraud_suspended" })).reason,
    undefined,
  );
});

// The cloud has no other way to know which SDK, in which version, is calling.
// Python has sent one since the first release; Node sent none, so half of a
// customer's traffic was anonymous exactly when an incident needed it named.
test("identifies the SDK and its version to the cloud on every request", async () => {
  let sent: Headers | undefined;
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async (_url, init) => {
      sent = new Headers(init?.headers);
      return jsonResponse(DECISION_BODY);
    },
  });
  await client.authorize(REQUEST);
  assert.equal(sent?.get("user-agent"), USER_AGENT);
  assert.match(USER_AGENT, /^idside-node\/\d+\.\d+\.\d+/);
});

// The version in the header must be the version the consumer installed, or it
// names the wrong release in the one log line an incident is diagnosed from.
test("the advertised version is the published one", async () => {
  const { version } = createRequire(import.meta.url)("../package.json") as {
    version: string;
  };
  assert.equal(USER_AGENT, `idside-node/${version}`);
});

// On a fail-closed path a timeout is not a tuning knob: it is the moment the
// product decides the cloud is dead and stops the caller's AI calls. This SDK
// waited 10 s where Python waited 5, so one network incident cut a Python
// application and spared its Node equivalent. `check_sdk_parity.sh` compares
// the number observed here against the one Python arms.
test("arms one deadline on the authorize hop, and it is the shared one", async () => {
  const armed: number[] = [];
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = ((fn: () => void, ms: number) =>
    realSetTimeout(
      fn,
      (armed.push(ms), 3_600_000),
    )) as unknown as typeof setTimeout;
  try {
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () => jsonResponse(DECISION_BODY),
    });
    await client.authorize(REQUEST);
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
  assert.deepEqual(armed, [DEFAULT_TIMEOUT_MS]);
  assert.equal(
    DEFAULT_TIMEOUT_MS,
    5_000,
    "the number Python publishes as DEFAULT_TIMEOUT_SECONDS",
  );
});

// ── the identity, checked before anything leaves the process ────────────────
//
// `consent.wrap()` checks this too, but `HttpConsentClient` is public API and a
// developer may hold one directly. Checking here is what keeps a request that
// names nobody — or names two people — from reaching the cloud at all, where it
// would either be refused with a less useful error or, worse, mint a proof.

test("authorize() refuses to name the user twice", async () => {
  let sent = 0;
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () => (sent++, jsonResponse(DECISION_BODY)),
  });

  await assert.rejects(
    client.authorize({ ...REQUEST, ubt: "eyJ.ubt.sig" }),
    ConflictingIdentityError,
  );
  assert.equal(sent, 0, "nothing may leave the process");
});

test("authorize() refuses to name no user at all", async () => {
  let sent = 0;
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () => (sent++, jsonResponse(DECISION_BODY)),
  });

  for (const identity of [{}, { userId: "" }, { ubt: "" }, { userId: "", ubt: "" }]) {
    await assert.rejects(
      client.authorize({ ...REQUEST, userId: undefined, ...identity }),
      MissingIdentityError,
      JSON.stringify(identity),
    );
  }
  assert.equal(sent, 0);
});

test("a UBT is forwarded as-is and no user_id is invented beside it", async () => {
  // The UBT is opaque to the SDK: the cloud alone verifies its signature and
  // binding. Sending a `user_id` alongside would be the SDK asserting an
  // identity of its own next to one that was actually proved.
  let sentBody: Record<string, unknown> = {};
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async (_url, init) => {
      sentBody = JSON.parse(String(init?.body));
      return jsonResponse(DECISION_BODY);
    },
  });

  await client.authorize({ ...REQUEST, userId: undefined, ubt: "eyJ.ubt.sig" });

  assert.equal(sentBody.ubt, "eyJ.ubt.sig");
  assert.ok(!("user_id" in sentBody));
});

// ── a 200 whose body is JSON but not a decision ─────────────────────────────

test("a 200 carrying JSON that is not an object fails closed", async () => {
  // `[]`, `"ok"`, `7`, `null`: all parse, none is a decision. Reading fields off
  // them would produce `undefined` for every mandatory field and a confusing
  // "missing field" message instead of the truth.
  for (const body of [[], ["allow"], "allow", 7, true, null]) {
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () => jsonResponse(body),
    });
    await assert.rejects(
      client.authorize(REQUEST),
      (error: unknown) =>
        error instanceof ConsentUnreachableError && /not a JSON object/.test(String(error)),
      JSON.stringify(body),
    );
  }
});

// ── the expiry, which is a date and has to be one ───────────────────────────

test("a real leap day is accepted and an invented one is not", async () => {
  // The year in an ACT expiry is not decoration: a date this SDK misreads is a
  // token it would treat as valid for a day that does not exist.
  const accepted = [
    "2024-02-29T12:00:00Z", // divisible by 4
    "2000-02-29T12:00:00Z", // divisible by 400: a leap year
    "2026-12-31T23:59:60Z", // a leap second is a real instant
    "2026-09-12T12:00:00+02:00",
    "2026-09-12t12:00:00z",
    "2026-09-12T12:00:00.123456Z",
  ];
  for (const expires of accepted) {
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () => jsonResponse({ ...DECISION_BODY, expires_at: expires }),
    });
    assert.ok(await client.authorize(REQUEST), expires);
  }

  const refused = [
    "2023-02-29T12:00:00Z", // not a leap year
    "1900-02-29T12:00:00Z", // divisible by 100 but not 400: NOT a leap year
    "2026-04-31T12:00:00Z", // April has 30 days
    "2026-00-10T12:00:00Z",
    "2026-13-10T12:00:00Z",
    "2026-09-00T12:00:00Z",
    "2026-09-12T24:00:00Z",
    "2026-09-12T12:60:00Z",
    "2026-09-12T12:00:61Z",
    "2026-09-12T12:00:00+24:00",
    "2026-09-12T12:00:00+02:60",
  ];
  for (const expires of refused) {
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () => jsonResponse({ ...DECISION_BODY, expires_at: expires }),
    });
    await assert.rejects(client.authorize(REQUEST), ConsentUnreachableError, expires);
  }
});

// ── the error body, which is written by whatever answered ───────────────────

test("an error body that is not JSON still produces the typed exception", async () => {
  // A proxy returning an HTML error page must not turn a 401 into a parser
  // crash: the status is what decides the class, and the body only ever adds
  // detail.
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () => new Response("<html>Gateway error</html>", { status: 401 }),
  });

  await assert.rejects(client.authorize(REQUEST), (error: unknown) => {
    assert.ok(error instanceof AuthenticationError);
    assert.ok(!String(error).includes("Cloud says"), "there was nothing to quote");
    return true;
  });
});

test("an error body that is JSON but not an object adds no detail", async () => {
  for (const body of [[], "denied", 7]) {
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () => jsonResponse(body, 429),
    });
    await assert.rejects(client.authorize(REQUEST), (error: unknown) => {
      assert.ok(error instanceof RateLimitedError, JSON.stringify(body));
      assert.ok(!String(error).includes("Cloud says"));
      return true;
    });
  }
});

test("a cloud detail that ends a sentence gets no second full stop", async () => {
  // The cloud writes its `detail` as a sentence, full stop included. Appending
  // one regardless printed `…(Signature has expired)..` at the end of the
  // message a developer reads first when a call does not go out.
  for (const ending of [".", "!", "?"]) {
    const detail = `50000/50000 authorize calls used this month${ending}`;
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () => jsonResponse({ title: "Quota", detail }, 402),
    });
    await assert.rejects(client.authorize(REQUEST), (error: unknown) => {
      assert.ok(error instanceof QuotaExceededError);
      assert.ok(
        error.message.endsWith(`Cloud says: Quota; ${detail}`),
        error.message,
      );
      return true;
    });
  }
});

test("a cloud detail without a full stop is given one", async () => {
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () =>
      jsonResponse({ title: "Too many requests", detail: "Slow down" }, 429),
  });
  await assert.rejects(client.authorize(REQUEST), (error: unknown) => {
    assert.ok(error instanceof RateLimitedError);
    assert.ok(
      error.message.endsWith("Cloud says: Too many requests; Slow down."),
      error.message,
    );
    return true;
  });
});

test("a deeper path under our own namespace is not an error name", async () => {
  // `https://idside.eu/errors/v2/fraud_suspended` is not the `fraud_suspended`
  // this SDK knows; qualifying it would be reading a name out of a URI shape we
  // never agreed on.
  assert.equal(
    (await denied({ type: "https://idside.eu/errors/v2/fraud_suspended" })).reason,
    undefined,
  );
  assert.equal((await denied({ type: 7 })).reason, undefined);
});

// ── A 401 is not always the key: the visitor's binding, told apart ─────────
//
// The cloud answers 401 for three different reasons — the secret key is refused
// (`invalid_api_key`), the visitor's UBT no longer verifies (`ubt_invalid`), or
// a strict application was called with `user_id` (`ubt_required`) — and all
// three used to read "Check the apiKey". The second is what every visitor coming
// back after 24 hours produces, and it sent developers to regenerate a key that
// was fine. Both binding cases stay `AuthenticationError`s, so a handler written
// against the old hierarchy still catches them.

const UBT_INVALID = "https://idside.eu/errors/ubt_invalid";
const UBT_REQUIRED = "https://idside.eu/errors/ubt_required";

async function unauthorized(problem: unknown): Promise<AuthenticationError> {
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () => jsonResponse(problem, 401),
  });
  try {
    await client.authorize(REQUEST);
  } catch (error) {
    assert.ok(error instanceof AuthenticationError, String(error));
    return error;
  }
  throw new Error("expected a 401");
}

test("an expired binding is not reported as a rejected key", async () => {
  const error = await unauthorized({
    type: UBT_INVALID,
    title: "Ubt invalid",
    status: 401,
    detail: "UBT is invalid or expired (Signature has expired).",
  });

  assert.equal(error.constructor, InvalidBindingError);
  assert.equal(error.name, "InvalidBindingError");
  assert.ok(error instanceof BindingError);
  assert.ok(!error.message.includes("apiKey"), error.message);
  assert.ok(error.message.includes("widget"), error.message);
  assert.ok(error.message.includes("the AI call was not made."), error.message);
  assert.ok(
    error.message.endsWith(
      "Cloud says: Ubt invalid; UBT is invalid or expired (Signature has expired).",
    ),
    error.message,
  );
});

test("an invalid binding does not claim two causes are the only ones", async () => {
  // The cloud answers `ubt_invalid` for any UBT that does not verify: an edited
  // payload, an ACT presented as a UBT and a truncated token all get it, and
  // none of them is expired or foreign. A message naming those two as the
  // causes is false for them, then quotes a cloud detail saying otherwise.
  const error = await unauthorized({
    type: UBT_INVALID,
    detail: "UBT is invalid or expired (Signature verification failed).",
  });

  assert.ok(error.message.includes("did not verify"), error.message);
  for (const cause of ["expired", "another application", "altered", "not a UBT"]) {
    assert.ok(error.message.includes(cause), cause);
  }
  assert.ok(
    !error.message.includes("has expired, or was issued for another application."),
    error.message,
  );
});

test("a strict app called without a UBT says what it needs", async () => {
  const error = await unauthorized({
    type: UBT_REQUIRED,
    title: "Ubt required",
    status: 401,
    detail: "This app requires a User Binding Token.",
  });

  assert.equal(error.constructor, BindingRequiredError);
  assert.equal(error.name, "BindingRequiredError");
  assert.ok(error instanceof BindingError);
  assert.ok(!error.message.includes("apiKey"), error.message);
  assert.ok(error.message.includes("widget"), error.message);
  assert.ok(error.message.includes("the AI call was not made."), error.message);
});

test("the bare binding names are read too", async () => {
  // An older cloud, or a test double, may send the short name.
  assert.equal((await unauthorized({ type: "ubt_invalid" })).constructor, InvalidBindingError);
  assert.equal((await unauthorized({ type: "ubt_required" })).constructor, BindingRequiredError);
});

test("any other 401 stays a rejected key", async () => {
  // Closed on purpose: a value this SDK does not know never moves the class.
  const others: unknown[] = [
    {},
    { type: "https://idside.eu/errors/invalid_api_key" },
    { type: "https://idside.eu/errors/ubt_expired" },
    { type: "https://evil.example/errors/ubt_invalid" },
    { type: "https://idside.eu/errors/v2/ubt_invalid" },
    { type: "https://idside.eu/other/ubt_required" },
    { type: "UBT_INVALID" },
    { type: " ubt_invalid" },
    { type: 7 },
    { type: ["ubt_invalid"] },
    ["ubt_invalid"],
  ];
  for (const problem of others) {
    const error = await unauthorized(problem);
    assert.equal(error.constructor, AuthenticationError, JSON.stringify(problem));
    assert.ok(error.message.includes("apiKey"), JSON.stringify(problem));
  }
});

test("a binding type qualifies a 401 and nothing else", async () => {
  const statuses: [number, new (...a: never[]) => Error][] = [
    [403, ConsentDeniedError],
    [409, ConsentUnreachableError],
    [422, ContractError],
    [500, ConsentUnreachableError],
  ];
  for (const type of [UBT_INVALID, UBT_REQUIRED]) {
    for (const [status, ErrorType] of statuses) {
      const client = new HttpConsentClient("https://cloud.test", {
        fetch: async () => jsonResponse({ type }, status),
      });
      await assert.rejects(client.authorize(REQUEST), (error: unknown) => {
        assert.ok(error instanceof ErrorType, `${status} ${type}`);
        assert.ok(!(error instanceof BindingError), `${status} ${type}`);
        return true;
      });
    }
  }
});

// ── A 409 that asks for the widget is not an outage ────────────────────────
//
// `consent_refresh_required`: the visitor's UBT still verifies, but the choices
// it is bound to no longer cover what this application now asks — the policy or
// the required questions changed since they answered. It used to fall into the
// default branch and come out as ConsentUnreachableError, whose remedy is
// "retry later". Retrying changes nothing; showing the widget does.

const REFRESH = "https://idside.eu/errors/consent_refresh_required";

async function conflict(problem: unknown): Promise<IDSideError> {
  const client = new HttpConsentClient("https://cloud.test", {
    fetch: async () => jsonResponse(problem, 409),
  });
  try {
    await client.authorize(REQUEST);
  } catch (error) {
    assert.ok(error instanceof IDSideError, String(error));
    return error;
  }
  throw new Error("expected a 409");
}

test("a refresh required asks for the widget, not a retry", async () => {
  const error = await conflict({
    type: REFRESH,
    title: "Consent refresh required",
    status: 409,
    detail: "The consent bound to this UBT no longer covers this app's current AI use.",
  });

  assert.equal(error.constructor, ConsentRefreshRequiredError);
  assert.equal(error.name, "ConsentRefreshRequiredError");
  assert.ok(!(error instanceof ConsentUnreachableError));
  assert.ok(error.message.includes("widget"), error.message);
  assert.ok(error.message.includes("the AI call was not made."), error.message);
  assert.ok(
    error.message.endsWith("no longer covers this app's current AI use."),
    error.message,
  );
});

test("the bare refresh name is read too", async () => {
  assert.equal(
    (await conflict({ type: "consent_refresh_required" })).constructor,
    ConsentRefreshRequiredError,
  );
});

test("any other 409 keeps failing closed as before", async () => {
  const others: unknown[] = [
    {},
    { type: "https://idside.eu/errors/consent_not_collected" },
    { type: "https://idside.eu/errors/account_link_not_collected" },
    { type: "https://evil.example/errors/consent_refresh_required" },
    { type: "https://idside.eu/errors/v2/consent_refresh_required" },
    { type: "CONSENT_REFRESH_REQUIRED" },
    { type: 409 },
    ["consent_refresh_required"],
  ];
  for (const problem of others) {
    const error = await conflict(problem);
    assert.equal(error.constructor, ConsentUnreachableError, JSON.stringify(problem));
    assert.ok(
      error.message.includes("Could not verify consent (HTTP 409)"),
      JSON.stringify(problem),
    );
  }
});

test("a refresh type qualifies a 409 and nothing else", async () => {
  const statuses: [number, new (...a: never[]) => Error][] = [
    [401, AuthenticationError],
    [403, ConsentDeniedError],
    [422, ContractError],
    [500, ConsentUnreachableError],
  ];
  for (const [status, ErrorType] of statuses) {
    const client = new HttpConsentClient("https://cloud.test", {
      fetch: async () => jsonResponse({ type: REFRESH }, status),
    });
    await assert.rejects(client.authorize(REQUEST), (error: unknown) => {
      assert.equal((error as Error).constructor, ErrorType, String(status));
      return true;
    });
  }
});

test("the index exports every error class the SDK defines", () => {
  // A class a caller cannot import is a class they cannot catch by name, which
  // is the only supported way to tell two refusals apart.
  for (const [name, value] of Object.entries(exceptions)) {
    if (typeof value !== "function" || !(value.prototype instanceof Error)) continue;
    assert.equal((index as Record<string, unknown>)[name], value, name);
  }
  assert.ok(Object.prototype.hasOwnProperty.call(index, "IDSideError"));
  assert.ok(new InvalidBindingError("x") instanceof IDSideError);
});

// ── what the client is built with ────────────────────────────────────────────

test("the API key travels as a bearer token, and only when there is one", async () => {
  // A client built without one must not send an empty `Authorization`: the cloud
  // would answer 401 and the developer would be told their key was rejected
  // rather than that they never passed one.
  const sent: (string | undefined)[] = [];
  const capture = async (_url: unknown, init?: RequestInit): Promise<Response> => {
    sent.push((init?.headers as Record<string, string>).Authorization);
    return jsonResponse(DECISION_BODY);
  };

  await new HttpConsentClient("https://cloud.test", {
    fetch: capture,
    apiKey: "sk_idside_test_key", // pragma: allowlist secret
  }).authorize(REQUEST);
  await new HttpConsentClient("https://cloud.test", { fetch: capture }).authorize(REQUEST);
  await new HttpConsentClient("https://cloud.test", { fetch: capture, apiKey: "" }).authorize(REQUEST);

  assert.deepEqual(sent, ["Bearer sk_idside_test_key", undefined, undefined]);
});

test("with no fetch injected, the global one is used", async () => {
  // The documented way to test without the network is to inject a `fetch`; the
  // default has to be the platform's, captured at construction so a strict
  // implementation is never called with the wrong receiver.
  const original = globalThis.fetch;
  const seen: string[] = [];
  globalThis.fetch = (async (url: unknown): Promise<Response> => {
    seen.push(String(url));
    return jsonResponse(DECISION_BODY);
  }) as typeof fetch;
  try {
    const decision = await new HttpConsentClient("https://cloud.test").authorize(REQUEST);
    assert.equal(decision.actToken, "act.jwt.token");
  } finally {
    globalThis.fetch = original;
  }

  assert.deepEqual(seen, ["https://cloud.test/v1/authorize"]);
});

test("a trailing slash on the base URL does not double up in the path", async () => {
  const seen: string[] = [];
  const capture = async (url: unknown): Promise<Response> => {
    seen.push(String(url));
    return jsonResponse(DECISION_BODY);
  };

  for (const base of ["https://cloud.test", "https://cloud.test/", "https://cloud.test///"]) {
    await new HttpConsentClient(base, { fetch: capture }).authorize(REQUEST);
  }

  assert.deepEqual(seen, [
    "https://cloud.test/v1/authorize",
    "https://cloud.test/v1/authorize",
    "https://cloud.test/v1/authorize",
  ]);
});

// ── An unconfigured address says so, instead of looking like an outage ──────
//
// With neither `baseUrl` nor IDSIDE_BASE_URL the SDK talks to its default,
// https://sdk.idside.eu — a contract, and kept. But a developer who forgot the
// variable met `Could not verify consent (HTTP 404)` and went looking for a
// network fault or a cloud incident, when one line of configuration was
// missing. Where the address was the default, the transport failure and the
// unexpected status now say so; where it was given, nothing changes. Every
// request below is answered by an injected fetch: none reaches idside.eu.

const DEFAULT_ORIGIN_SENTENCE =
  "No ID Side address was configured, so the SDK used its default, " +
  "https://sdk.idside.eu: set IDSIDE_BASE_URL to the address shown in your ID " +
  "Side dashboard, or pass that address explicitly.";

async function failures(baseUrl: string | undefined): Promise<string[]> {
  const answers: (() => Promise<Response>)[] = [
    async () => {
      throw new TypeError("fetch failed");
    },
    async () => new Response("<html>Not Found</html>", { status: 404 }),
  ];
  const messages: string[] = [];
  for (const answer of answers) {
    const client = new HttpConsentClient(baseUrl, { fetch: answer });
    await assert.rejects(client.authorize(REQUEST), (error: unknown) => {
      assert.equal((error as Error).constructor, ConsentUnreachableError);
      messages.push((error as Error).message);
      return true;
    });
  }
  return messages;
}

test("a failure on the default address names the missing setting", async () => {
  for (const message of await failures(undefined)) {
    assert.ok(message.includes("the AI call was not made."), message);
    assert.ok(message.endsWith(DEFAULT_ORIGIN_SENTENCE), message);
  }
});

test("a failure on an address that was given is unchanged", async () => {
  for (const message of await failures("https://cloud.test")) {
    assert.ok(!message.includes(DEFAULT_ORIGIN_SENTENCE), message);
  }
});

test("the default address given explicitly counts as given", async () => {
  // The sentence is about a missing setting, not about the host.
  for (const message of await failures(DEFAULT_BASE_URL)) {
    assert.ok(!message.includes(DEFAULT_ORIGIN_SENTENCE), message);
  }
});

test("a qualified refusal on the default address is left alone", async () => {
  // Only the outage-shaped failures carry it: a typed refusal already says
  // what to do, and the class never changes either way.
  const cases: [number, unknown][] = [
    [401, {}],
    [401, { type: UBT_INVALID }],
    [402, {}],
    [403, {}],
    [409, { type: REFRESH }],
    [422, {}],
    [429, {}],
  ];
  for (const [status, problem] of cases) {
    const client = new HttpConsentClient(undefined, {
      fetch: async () => jsonResponse(problem, status),
    });
    await assert.rejects(client.authorize(REQUEST), (error: unknown) => {
      assert.ok(error instanceof IDSideError);
      assert.ok(!(error instanceof ConsentUnreachableError), String(status));
      assert.ok(!error.message.includes(DEFAULT_ORIGIN_SENTENCE), String(status));
      return true;
    });
  }
});

// ── the key is a secret: no message carries it ──────────────────────────────

const SECRET_KEY = "sk_idside_supersecret_key"; // pragma: allowlist secret

/** The message and stack of an error and of everything chained to it. */
function chainText(error: unknown): string {
  const parts: string[] = [];
  const seen = new Set<unknown>();
  for (let link = error; link instanceof Error && !seen.has(link); link = link.cause) {
    seen.add(link);
    parts.push(link.message, String(link.stack), String(link));
  }
  return parts.join("\n");
}

test("the key appears in no error message, wherever it came from", async () => {
  // Every way an authorize can fail: the network, each qualified refusal, an
  // unqualified status and an unreadable 200.
  const answers: (() => Promise<Response>)[] = [
    async () => {
      throw new TypeError("fetch failed");
    },
    ...(
      [
        [401, {}],
        [401, { type: UBT_INVALID }],
        [401, { type: UBT_REQUIRED }],
        [402, {}],
        [403, { type: "https://idside.eu/errors/fraud_suspended" }],
        [409, { type: REFRESH }],
        [409, {}],
        [422, {}],
        [429, {}],
        [500, {}],
        [404, {}],
      ] as const
    ).map(([status, body]) => async () => jsonResponse(body, status)),
    async () => new Response("not json", { status: 200 }),
  ];
  for (const source of ["argument", "environment"] as const) {
    if (source === "environment") process.env.IDSIDE_API_KEY = SECRET_KEY;
    try {
      for (const answer of answers) {
        const client = new HttpConsentClient("https://cloud.test", {
          ...(source === "argument" ? { apiKey: SECRET_KEY } : {}),
          fetch: answer,
        });
        await assert.rejects(client.authorize(REQUEST), (error: unknown) => {
          assert.ok(error instanceof IDSideError);
          assert.ok(!chainText(error).includes(SECRET_KEY), `${source}: ${error.message}`);
          return true;
        });
      }
    } finally {
      delete process.env.IDSIDE_API_KEY;
    }
  }
});

// ── a key a header cannot carry is refused before it is sent, unquoted ──────
//
// `fetch` drops whitespace around a header value, so a key read from a secret
// file with its line break still worked here — and failed in Python. A line
// break, a NUL or a character above U+00FF INSIDE the key made `fetch` throw an
// error quoting the whole header, key included (`Headers.append: "Bearer sk_…"
// is an invalid header value`), chained under an "unreachable" that sent the
// developer looking for an outage. Both SDKs now drop the whitespace around the
// key, and refuse anything else that is not printable ASCII where the transport
// is built, naming the setting without repeating the key. Same table in Python.

const KEY_HALVES = ["sk_live_unsendable", "keytail"] as const; // pragma: allowlist secret
const UNSENDABLE_SEPARATORS = ["\n", "\r\n", " ", "\t", "\u0000", "\u007f", "é", "€"];

for (const separator of UNSENDABLE_SEPARATORS) {
  for (const source of ["argument", "environment"] as const) {
    // An environment variable cannot hold a NUL: the process cuts it there.
    const skip = source === "environment" && separator === "\u0000";
    test(`a key with ${JSON.stringify(separator)} inside it is refused unquoted (${source})`, { skip }, () => {
      const key = KEY_HALVES.join(separator);
      if (source === "environment") process.env.IDSIDE_API_KEY = key;
      try {
        assert.throws(
          () => new HttpConsentClient("https://cloud.test", source === "argument" ? { apiKey: key } : {}),
          (error: unknown) => {
            assert.ok(error instanceof TypeError, String(error));
            assert.ok(error.message.includes("IDSIDE_API_KEY"), error.message);
            assert.ok(error.message.includes("apiKey"), error.message);
            const text = chainText(error);
            for (const half of KEY_HALVES) assert.ok(!text.includes(half), text);
            return true;
          },
        );
      } finally {
        delete process.env.IDSIDE_API_KEY;
      }
    });
  }
}

for (const [label, configured, expected] of [
  ["newline", "sk_test_trimmed\n", "Bearer sk_test_trimmed"],
  ["around", " \tsk_test_trimmed\r\n", "Bearer sk_test_trimmed"],
  ["only-whitespace", " \n", null],
] as const) {
  test(`whitespace around the key is dropped (${label})`, async () => {
    process.env.IDSIDE_API_KEY = configured;
    try {
      const sent: (string | null)[] = [];
      const client = new HttpConsentClient("https://cloud.test", {
        fetch: async (_url: string | URL | Request, init?: RequestInit) => {
          sent.push(new Headers(init?.headers).get("authorization"));
          return jsonResponse(DECISION_BODY);
        },
      });
      await client.authorize(REQUEST);
      // What `fetch` would have sent anyway, said by the SDK rather than left to it.
      assert.deepEqual(sent, [expected]);
    } finally {
      delete process.env.IDSIDE_API_KEY;
    }
  });
}
