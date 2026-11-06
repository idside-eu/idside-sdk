/**
 * Observability: enough to debug a call that did not go out, and no more.
 *
 * A fail-closed SDK with no journal is undebuggable at a customer site. When a
 * call does not leave, the developer cannot learn which call site was
 * recognised, which option a user's answer forced, how long the authorization
 * took, or whether the cloud was reached at all.
 *
 * What it must never do matters more, so that is most of this file:
 *
 * - **Nothing secret ever reaches the journal.** Not an API key, not the end
 *   user's identifier in the clear, not a UBT, not a prompt, not an ACT token,
 *   not a credential. A credential is a secret (invariant #7) and so is the
 *   person.
 * - **Silent by default.** `util.debuglog` is Node's own answer to this: the
 *   SDK writes nothing unless `NODE_DEBUG=idside` asks it to. No dependency, no
 *   invented channel, nothing on stdout ever.
 * - **Observation is not influence** (invariant #8). Rendering happens on
 *   scalars and copies, never on the live decision, and there is no hook for an
 *   application to answer through.
 *
 * The net (`guard.ts`) journals nothing, and that is tested here too.
 *
 * Mirrors packages/python/tests/test_logging.py. The two SDKs emit the same
 * events with the same fields; `scripts/check_sdk_parity.sh` runs both and
 * diffs the lines.
 */

import { spawnSync } from "node:child_process";
import { debuglog } from "node:util";

// Node reads NODE_DEBUG once, when the process boots — assigning process.env
// later changes nothing, and `debuglog("idside").enabled` stays false. So this
// file re-runs itself once in a child that has the variable, rather than
// weakening the tests to work around the channel they exist to exercise.
// Every assertion below then runs against the real journal, on the real path.
//
// NODE_TEST_* must not be inherited by that child. This process was started BY
// the test runner, so its environment says "you are a test child reporting to a
// parent over IPC" — and a nested `node --test` that believes that reports its
// results upward instead of through its exit code, and exits 0 whatever
// happened. Every assertion in this file then passed unconditionally, including
// the one that holds invariant #3's "the net journals nothing". The child has
// to be told it is a runner in its own right.
if (!debuglog("idside").enabled) {
  const childEnv: NodeJS.ProcessEnv = { ...process.env, NODE_DEBUG: "idside" };
  for (const name of Object.keys(childEnv)) {
    if (name.startsWith("NODE_TEST_")) delete childEnv[name];
  }
  const child = spawnSync(process.execPath, ["--test", import.meta.filename], {
    env: childEnv,
    encoding: "utf8",
  });
  if (child.status !== 0) {
    process.stderr.write(child.stdout ?? "");
    process.stderr.write(child.stderr ?? "");
  }
  process.exit(child.status ?? 1);
}

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { wrap } from "../src/consent.ts";
import { GenericAdapter } from "../src/adapters/index.ts";
import { loadDescriptors } from "../src/descriptors.ts";
import { installTransportGuard } from "../src/guard.ts";
import {
  IDSideError,
  MissingCredentialError,
  ToolUseRefusedError,
  UnenforcedCallError,
} from "../src/exceptions.ts";
import { pseudonym, SALT } from "../src/log.ts";
import type { AuthorizeRequest, ConsentClient, ConsentDecision } from "../src/transport.ts";

const OPENAI = loadDescriptors().get("openai")!;

/** Every secret these tests hand the SDK. None may appear in the journal. */
const API_KEY = "sk_idside_supersecret_key"; // pragma: allowlist secret
const USER_ID = "usr_marie_456";
const UBT = "eyJhbGciOiJFUzI1NiJ9.dXNyX21hcmllXzQ1Ng.sig"; // pragma: allowlist secret
const PROMPT = "my haemoglobin was 11.2 and I am worried";
const ACT_TOKEN = "eyJ.act_token_that_proves_it.sig"; // pragma: allowlist secret
const CREDENTIAL = "proj_medhelp_zero_retention"; // pragma: allowlist secret

const MESSAGES = [{ role: "user", content: PROMPT }];

const DECISION: ConsentDecision = {
  consentState: { "retention.conversation": false },
  paramsForced: { store: false },
  actToken: ACT_TOKEN,
  source: "widget",
  legalBasis: "consent",
  binding: "user_id",
  constraints: {},
};

function fakeConsentClient(
  d: ConsentDecision = DECISION,
  fail?: Error,
): ConsentClient & { calls: AuthorizeRequest[] } {
  const calls: AuthorizeRequest[] = [];
  return {
    calls,
    async authorize(request: AuthorizeRequest): Promise<ConsentDecision> {
      calls.push(request);
      if (fail) throw fail;
      return d;
    },
  };
}

function fakeOpenAI() {
  const calls: Record<string, unknown>[] = [];
  const create = async (body: Record<string, unknown>) => {
    calls.push(body);
    return { id: "chatcmpl_fake" };
  };
  return { client: { chat: { completions: { create } } }, calls };
}

/** Run `fn` with stderr captured, and hand back everything the journal wrote. */
async function journal(fn: () => Promise<unknown> | unknown): Promise<string> {
  const original = process.stderr.write.bind(process.stderr);
  let captured = "";
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (process.stderr as any).write = (chunk: unknown): boolean => {
    captured += String(chunk);
    return true;
  };
  try {
    await fn();
  } finally {
    (process.stderr as any).write = original;
  }
  return captured;
}

/** Same, tolerating the rejection the scenario is about. */
async function journalOfFailure(fn: () => Promise<unknown>): Promise<string> {
  return journal(async () => {
    await assert.rejects(fn);
  });
}

// ── silent by default ────────────────────────────────────────────────────────

test("nothing is ever written to stdout", async () => {
  const original = process.stdout.write.bind(process.stdout);
  let captured = "";
  (process.stdout as any).write = (chunk: unknown): boolean => {
    captured += String(chunk);
    return true;
  };
  try {
    const { client, calls } = fakeOpenAI();
    const wrapped = wrap(client, { adapter: new GenericAdapter(OPENAI), consentClient: fakeConsentClient() });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
    assert.equal(calls.length, 1);
  } finally {
    (process.stdout as any).write = original;
  }
  assert.equal(captured, "", "a library that prints uninvited is a library that gets removed");
});

test("the journal is Node's own debuglog, so it is off unless asked", async () => {
  // This suite sets NODE_DEBUG=idside at the top; without it `enabled` is false
  // and `event()` returns before rendering anything. That is the whole
  // mechanism, and it is Node's, not one this SDK invented.
  const { debuglog } = await import("node:util");
  assert.equal(debuglog("idside").enabled, true);
  assert.equal(debuglog("idside-not-a-real-section").enabled, false);
});

// ── nothing secret, ever ─────────────────────────────────────────────────────

for (const identity of ["user_id", "ubt"] as const) {
  test(`no secret reaches the journal on a successful call (${identity})`, async () => {
    const { client } = fakeOpenAI();
    const written = await journal(async () => {
      const wrapped = wrap(client, {
        adapter: new GenericAdapter(OPENAI),
        consentClient: fakeConsentClient(),
        apiKey: API_KEY,
        credentials: { default: CREDENTIAL },
      });
      await (wrapped as any).chat.completions.create({
        model: "gpt-5",
        messages: MESSAGES,
        [identity]: identity === "user_id" ? USER_ID : UBT,
      });
    });

    assert.ok(written.length > 0, "the sweep is worthless if nothing was journalled");
    for (const secret of [API_KEY, USER_ID, UBT, PROMPT, ACT_TOKEN, CREDENTIAL, "haemoglobin"]) {
      assert.ok(!written.includes(secret), `${secret} reached the journal`);
    }
  });
}

test("no secret reaches the journal when the cloud refuses", async () => {
  const { client } = fakeOpenAI();
  const written = await journalOfFailure(async () => {
    const wrapped = wrap(client, {
      adapter: new GenericAdapter(OPENAI),
      consentClient: fakeConsentClient(DECISION, new Error("connection refused")),
      apiKey: API_KEY,
    });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
  });

  assert.ok(written.includes("authorize_failed"));
  for (const secret of [API_KEY, USER_ID, PROMPT, "haemoglobin"]) {
    assert.ok(!written.includes(secret), `${secret} reached the journal`);
  }
});

test("the ACT token is never journalled even though it is useful", async () => {
  // A proof is bearer material. The developer's real question — "which proof was
  // minted for this call?" — is answered by the request hash, which is what the
  // ledger indexes and what the token is bound to.
  const { client } = fakeOpenAI();
  const written = await journal(async () => {
    const wrapped = wrap(client, { adapter: new GenericAdapter(OPENAI), consentClient: fakeConsentClient() });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
  });

  assert.ok(!written.includes(ACT_TOKEN));
  assert.ok(!written.includes("act_token"));
  assert.ok(written.includes("request_hash="));
});

test("the consent state is not journalled but what it forced is", async () => {
  const { client } = fakeOpenAI();
  const written = await journal(async () => {
    const wrapped = wrap(client, { adapter: new GenericAdapter(OPENAI), consentClient: fakeConsentClient() });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
  });

  assert.ok(!written.includes("retention.conversation"), "the answer a person gave is theirs");
  assert.ok(written.includes("forced=store"), "the parameter it forced is the app's own request");
});

// ── the pseudonym: correlation without identification ────────────────────────

test("calls by one user correlate without naming them", async () => {
  const { client } = fakeOpenAI();
  const written = await journal(async () => {
    const wrapped = wrap(client, { adapter: new GenericAdapter(OPENAI), consentClient: fakeConsentClient() });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: "usr_other_789" });
  });

  const subjects = [...written.matchAll(/subject=(\S+)/g)].map((m) => m[1]);
  assert.ok(subjects.length >= 3);
  assert.equal(subjects[0], subjects[1], "one user, one pseudonym, within a process");
  assert.notEqual(subjects[0], subjects[subjects.length - 1], "two users must not collapse into one");
});

test("a UBT call is correlated by the UBT, not by an absent user_id", async () => {
  // The pseudonym is the only thing in the journal that stands for a person. On
  // a UBT call there is no `user_id` at all, so a pseudonym derived from one
  // would be the digest of `undefined` — identical for every user of the app,
  // which is a journal that says one person made every call.
  const { client } = fakeOpenAI();
  const other = "eyJhbGciOiJFUzI1NiJ9.dXNyX290aGVyXzc4OQ.sig"; // pragma: allowlist secret
  const written = await journal(async () => {
    const wrapped = wrap(client, { adapter: new GenericAdapter(OPENAI), consentClient: fakeConsentClient() });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, ubt: UBT });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, ubt: UBT });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, ubt: other });
  });

  const subjects = [...written.matchAll(/subject=(\S+)/g)].map((m) => m[1]);
  assert.ok(subjects.length >= 3);
  assert.equal(subjects[0], subjects[1], "one UBT, one pseudonym, within a process");
  assert.notEqual(subjects[0], subjects[subjects.length - 1], "two UBTs must not collapse into one");
  assert.ok(!written.includes(UBT) && !written.includes(other), "a UBT is a secret");
  assert.ok(written.includes("binding=ubt"), "which kind of identity named the user");
});

test("the pseudonym is salted, so a guessable id stays private", () => {
  // A user_id is low-entropy on purpose — `usr_marie_456` is a dictionary away.
  // An unsalted hash of one is not a pseudonym but an encoding: an attacker
  // holding the log hashes their guesses and reads the names back.
  const naive = createHash("sha256").update(USER_ID).digest("hex");
  const p = pseudonym(USER_ID);
  assert.ok(!naive.includes(p));
  assert.ok(!p.includes(USER_ID));
  assert.equal(p, pseudonym(USER_ID));
  assert.notEqual(p, pseudonym("usr_other_789"));
});

test("the pseudonym tells identities apart as the cloud does, whatever they are", () => {
  // An identity reaches the cloud as written, so a wrong one is refused there —
  // and journalled first. `String()` gave every object one pseudonym and an
  // array the one of its joined items, so the journal told two refused calls
  // apart no better than the old transport did.
  assert.notEqual(pseudonym({ id: USER_ID }), pseudonym({ id: "usr_other_789" }));
  assert.notEqual(pseudonym([USER_ID]), pseudonym(USER_ID));
  // The cloud reads 42 and "42" as one user, and so does the journal.
  assert.equal(pseudonym(42), pseudonym("42"));
  assert.equal(pseudonym(42n), pseudonym(42));
  // `String()` throws on an object with no prototype; the journal must not.
  assert.doesNotThrow(() => pseudonym(Object.create(null)));
});

test("the salt itself is never journalled", async () => {
  const { client } = fakeOpenAI();
  const written = await journal(async () => {
    const wrapped = wrap(client, { adapter: new GenericAdapter(OPENAI), consentClient: fakeConsentClient() });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
  });
  assert.ok(!written.includes(SALT.toString("hex")));
});

// ── what it is actually for ──────────────────────────────────────────────────

test("a developer can see which call site and what was forced", async () => {
  const { client } = fakeOpenAI();
  const written = await journal(async () => {
    const wrapped = wrap(client, { adapter: new GenericAdapter(OPENAI), consentClient: fakeConsentClient() });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
  });

  assert.ok(written.includes("call_site=chat.completions.create"), "which entry point");
  assert.ok(written.includes("forced=store"), "which option was forced");
  assert.ok(/duration_ms=\d+/.test(written), "how long authorize took");
  assert.ok(written.includes("request_hash="), "which proof, via the ledger");
});

test("an unreachable cloud says so with the time it waited", async () => {
  const { client, calls } = fakeOpenAI();
  const written = await journalOfFailure(async () => {
    const wrapped = wrap(client, {
      adapter: new GenericAdapter(OPENAI),
      consentClient: fakeConsentClient(DECISION, new TypeError("fetch failed")),
    });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
  });

  assert.ok(written.includes("authorize_failed"));
  assert.ok(written.includes("error=TypeError"));
  assert.ok(/duration_ms=\d+/.test(written));
  assert.equal(calls.length, 0);
});

test("a local refusal is journalled where the developer will look", async () => {
  // A tools refusal stops the call AFTER a healthy authorize. Without a line for
  // it the journal shows a successful authorize and then nothing at all, which
  // is the most confusing shape a journal can have.
  const { client, calls } = fakeOpenAI();
  const refusing = fakeConsentClient({ ...DECISION, paramsForced: {}, constraints: { tools: "refuse" } });
  const written = await journal(async () => {
    const wrapped = wrap(client, { adapter: new GenericAdapter(OPENAI), consentClient: refusing });
    await assert.rejects(
      (wrapped as any).chat.completions.create({
        model: "gpt-5",
        messages: MESSAGES,
        tools: [{ type: "function", function: { name: "f" } }],
        user_id: USER_ID,
      }),
      ToolUseRefusedError,
    );
  });

  assert.ok(written.includes("refused"));
  assert.ok(written.includes("ToolUseRefusedError"));
  assert.equal(calls.length, 0);
});

test("no constraint is said by the absence of the field, not by an empty list", async () => {
  // Python drops the field the same way, and `scripts/check_sdk_parity.sh`
  // diffs the two journals line for line — this spelling is what it compares.
  const plain = await journal(async () => {
    const wrapped = wrap(fakeOpenAI().client, {
      adapter: new GenericAdapter(OPENAI),
      consentClient: fakeConsentClient(),
    });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
  });

  assert.ok(plain.includes("authorize "), "the sweep is worthless if nothing was journalled");
  assert.ok(!plain.includes("constraints="), "an empty constraint set was rendered");

  const constrained = await journal(async () => {
    const wrapped = wrap(fakeOpenAI().client, {
      adapter: new GenericAdapter(OPENAI),
      consentClient: fakeConsentClient({ ...DECISION, constraints: { tools: "strip" } }),
    });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
  });

  assert.ok(constrained.includes("constraints=tools"), "a constraint that exists must be named");
});

test("a missing credential says which posture, not which credential", async () => {
  const { client, calls } = fakeOpenAI();
  const needsPosture = fakeConsentClient({ ...DECISION, paramsForced: {}, credentialPosture: "zero_retention" });
  const written = await journal(async () => {
    const wrapped = wrap(client, {
      adapter: new GenericAdapter(OPENAI),
      consentClient: needsPosture,
      credentials: { default: CREDENTIAL },
    });
    await assert.rejects(
      (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID }),
      MissingCredentialError,
    );
  });

  assert.ok(written.includes("zero_retention"), "the label is app configuration");
  assert.ok(!written.includes(CREDENTIAL), "the credential is a secret");
  assert.equal(calls.length, 0);
});

test("wrap says what it recognised", async () => {
  const { client } = fakeOpenAI();
  const written = await journal(() => {
    wrap(client, { adapter: new GenericAdapter(OPENAI), consentClient: fakeConsentClient() });
  });

  assert.ok(written.includes("provider=openai"));
  assert.ok(written.includes("call_sites=1"));
});

test("an alias that defers upstream says so instead of going quiet", async () => {
  const calls: Record<string, unknown>[] = [];
  const completions = {
    async create(body: Record<string, unknown>, _options?: unknown) {
      calls.push(body);
      return { id: "chatcmpl_fake" };
    },
    async parse(body: Record<string, unknown>, options?: unknown) {
      return this.create(body, options);
    },
  };
  const cc = fakeConsentClient();
  const written = await journal(async () => {
    const wrapped = wrap({ chat: { completions } }, { adapter: new GenericAdapter(OPENAI), consentClient: cc });
    await (wrapped as any).chat.completions.parse({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
  });

  assert.equal(cc.calls.length, 1);
  assert.equal(calls.length, 1);
  assert.ok(written.includes("passthrough"));
});

// ── the net stays blind ──────────────────────────────────────────────────────

test("the net journals nothing at all", async () => {
  // Documented in providers.md as "it hashes nothing, logs nothing". The guard
  // sits on the request path of a product whose premise is that we do not see
  // the request; observability is not a reason to give it a first thing to
  // record.
  const client: Record<string, unknown> = { fetch: async () => new Response("{}") };
  installTransportGuard(client, OPENAI.tokenHeader);

  const written = await journal(() => {
    // Synchronously: the guard refuses before the fetch is ever started, which
    // is the whole point of it being a detector rather than a wrapper.
    assert.throws(
      () =>
        (client.fetch as (u: string, i: RequestInit) => Promise<Response>)("https://api.example.test/v1/x", {
          method: "POST",
          body: "{}",
        }),
      UnenforcedCallError,
    );
  });

  assert.equal(written, "");
});

// ── observation is not influence (invariant #8) ──────────────────────────────

test("journalling adds no option to wrap", async () => {
  // Invariant #8: no knob arrives alongside the journal. Node cannot make an
  // unknown option a TypeError the way Python does, so the assertion is the
  // one that matters — an option nobody implemented changes nothing.
  const { client, calls } = fakeOpenAI();
  const wrapped = wrap(client, {
    adapter: new GenericAdapter(OPENAI),
    consentClient: fakeConsentClient(),
    ...({ allowUnenforced: true, onEvent: () => "tampered", logger: {} } as Record<string, unknown>),
  });
  await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });

  assert.equal(calls[0].store, false, "a bypass option must not have been honoured");
  assert.equal((calls[0] as any)[OPENAI.tokenHeader], undefined);
});

test("the journal renders copies, so nothing it is given can travel back", async () => {
  // The rendered line is built from scalars; the decision object itself never
  // leaves the enforcement path. Freezing what the cloud returned proves the
  // journal does not write through it on its way past.
  const { client, calls } = fakeOpenAI();
  const frozen: ConsentDecision = Object.freeze({
    ...DECISION,
    paramsForced: Object.freeze({ store: false }) as Record<string, unknown>,
  });
  await journal(async () => {
    const wrapped = wrap(client, { adapter: new GenericAdapter(OPENAI), consentClient: fakeConsentClient(frozen) });
    await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
  });

  assert.equal(calls[0].store, false);
  assert.equal(frozen.paramsForced.store, false);
});

// The key the transport reads for itself is held to the same rule. Same
// scenario in the Python suite: IDSIDE_API_KEY set, no key option, a call
// through `wrap()`, whatever the cloud answers. The global fetch is replaced,
// so nothing leaves the process.
const AUTHORIZED = {
  decision: "allow",
  consent_state: { "retention.conversation": false },
  params_forced: { store: false },
  mode: "test",
  act_token: ACT_TOKEN,
  token_id: "act_01k4f9h2m3",
  expires_at: "2026-09-12T12:00:00Z",
  source: "widget",
  legal_basis: "consent",
  binding: "user_id",
};
for (const [label, answer] of [
  ["200", async () => new Response(JSON.stringify(AUTHORIZED), { status: 200 })],
  ["401", async () => new Response("{}", { status: 401 })],
  ["503", async () => new Response("", { status: 503 })],
  [
    "unreachable",
    async (): Promise<Response> => {
      throw new TypeError("fetch failed");
    },
  ],
] as const) {
  test(`a key read from the environment reaches no journal line (${label})`, async () => {
    const realFetch = globalThis.fetch;
    const prev = { key: process.env.IDSIDE_API_KEY, base: process.env.IDSIDE_BASE_URL };
    process.env.IDSIDE_API_KEY = API_KEY;
    process.env.IDSIDE_BASE_URL = "https://cloud.test";
    const sent: (string | null)[] = [];
    globalThis.fetch = (async (_url: unknown, init?: RequestInit) => {
      sent.push(new Headers(init?.headers).get("authorization"));
      return answer();
    }) as typeof fetch;
    try {
      const { client } = fakeOpenAI();
      const written = await journal(async () => {
        const wrapped = wrap(client, { adapter: new GenericAdapter(OPENAI) });
        try {
          await (wrapped as any).chat.completions.create({ model: "gpt-5", messages: MESSAGES, user_id: USER_ID });
        } catch (error) {
          assert.ok(error instanceof IDSideError);
          assert.ok(!error.message.includes(API_KEY), error.message);
        }
      });

      assert.deepEqual(sent, [`Bearer ${API_KEY}`], "the key was not in play");
      assert.ok(written.includes("authorize"), "the sweep is worthless if nothing was journalled");
      assert.ok(!written.includes(API_KEY), "the key reached the journal");
    } finally {
      globalThis.fetch = realFetch;
      for (const [name, value] of [["IDSIDE_API_KEY", prev.key], ["IDSIDE_BASE_URL", prev.base]] as const) {
        if (value === undefined) delete process.env[name];
        else process.env[name] = value;
      }
    }
  });
}
