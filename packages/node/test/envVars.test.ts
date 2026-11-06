import { test } from "node:test";
import assert from "node:assert/strict";
import { wrap } from "../src/consent.ts";
import { ConsentUnreachableError } from "../src/exceptions.ts";
import { HttpConsentClient } from "../src/transport.ts";

// node --test runs each test file in its own process, so mutating globalThis.fetch
// and process.env here does not leak into the other suites.
for (const [label, configured, expectedUrl] of [
  ["configured origin", "https://cloud.env.test", "https://cloud.env.test/v1/authorize"],
  ["missing origin", undefined, "https://idside.eu/v1/authorize"],
  ["empty origin", "", "https://idside.eu/v1/authorize"],
] as const) {
  test(`wrap() uses the service URL for ${label} and the environment API key`, async () => {
    const realFetch = globalThis.fetch;
    const prevBase = process.env.IDSIDE_BASE_URL;
    const prevKey = process.env.IDSIDE_API_KEY;
    if (configured === undefined) delete process.env.IDSIDE_BASE_URL;
    else process.env.IDSIDE_BASE_URL = configured;
    process.env.IDSIDE_API_KEY = "sk_test_env_key"; // pragma: allowlist secret

    const seen: { url: string; auth: string | null }[] = [];
    globalThis.fetch = (async (
      input: string | URL | Request,
      init?: RequestInit,
    ) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : (input as Request).url;
      seen.push({ url, auth: new Headers(init?.headers).get("authorization") });
      return new Response(
        JSON.stringify({
          decision: "allow",
          consent_state: {},
          params_forced: {},
          mode: "test",
          act_token: "act.jwt.token",
          token_id: "act_01k4f9h2m3",
          expires_at: "2026-09-12T12:00:00Z",
          source: "s",
          legal_basis: "consent",
          binding: "user_id",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as unknown as typeof fetch;

    try {
      // `unknown` as a body type intersects with the identity to the identity
      // alone, which would reject `model`; a real provider's params are a record.
      const create = async (
        body: Record<string, unknown>,
        options?: unknown,
      ) => ({ body, options });
      const client = { chat: { completions: { create } } };
      // No consentClient / baseUrl / apiKey: the transport must come from the env.
      const wrapped = wrap(client, { provider: "openai" });
      await wrapped.chat.completions.create({
        model: "gpt-4o",
        messages: [],
        user_id: "u",
      });

      assert.equal(seen.length, 1);
      assert.equal(seen[0]!.url, expectedUrl);
      assert.equal(seen[0]!.auth, "Bearer sk_test_env_key");
    } finally {
      globalThis.fetch = realFetch;
      if (prevBase === undefined) delete process.env.IDSIDE_BASE_URL;
      else process.env.IDSIDE_BASE_URL = prevBase;
      if (prevKey === undefined) delete process.env.IDSIDE_API_KEY;
      else process.env.IDSIDE_API_KEY = prevKey;
    }
  });
}

// Where the address came from decides whether a failure names the missing
// setting: `wrap()` must not hand its transport the default as if somebody had
// configured it. The global fetch is replaced, so nothing reaches idside.eu.
for (const [label, option, configured, named] of [
  ["neither option nor environment", undefined, undefined, true],
  ["an empty environment variable", undefined, "", true],
  ["the environment", undefined, "https://cloud.env.test", false],
  ["the baseUrl option", "https://cloud.option.test", undefined, false],
] as const) {
  test(`a failure through wrap() with ${label} ${named ? "names" : "does not name"} the default`, async () => {
    const realFetch = globalThis.fetch;
    const prevBase = process.env.IDSIDE_BASE_URL;
    if (configured === undefined) delete process.env.IDSIDE_BASE_URL;
    else process.env.IDSIDE_BASE_URL = configured;
    globalThis.fetch = (async () =>
      new Response("<html>Not Found</html>", { status: 404 })) as unknown as typeof fetch;

    try {
      const calls: unknown[] = [];
      const create = async (body: Record<string, unknown>) => {
        calls.push(body);
        return {};
      };
      const wrapped = wrap(
        { chat: { completions: { create } } },
        { provider: "openai", ...(option ? { baseUrl: option } : {}) },
      );
      await assert.rejects(
        wrapped.chat.completions.create({ model: "gpt-4o", messages: [], user_id: "u" }),
        (error: unknown) => {
          assert.ok(error instanceof ConsentUnreachableError);
          assert.equal(
            error.message.includes("No ID Side address was configured"),
            named,
            error.message,
          );
          return true;
        },
      );
      assert.equal(calls.length, 0, "provider must not be called");
    } finally {
      globalThis.fetch = realFetch;
      if (prevBase === undefined) delete process.env.IDSIDE_BASE_URL;
      else process.env.IDSIDE_BASE_URL = prevBase;
    }
  });
}

// A transport built by hand resolves its address exactly as Python's does, in
// its constructor and in the same order: the argument, then IDSIDE_BASE_URL,
// then the default — an empty string counting as absent at each step — and only
// the default makes a failure name the missing setting. Node used to read the
// variable in `wrap()` alone, so `new HttpConsentClient()` with IDSIDE_BASE_URL
// set went to the default and told the developer to set a variable that was set.
for (const [label, argument, configured, expectedUrl, named] of [
  ["the environment and no argument", undefined, "https://cloud.env.test", "https://cloud.env.test/v1/authorize", false],
  ["neither argument nor environment", undefined, undefined, "https://idside.eu/v1/authorize", true],
  ["an empty environment variable", undefined, "", "https://idside.eu/v1/authorize", true],
  ["an argument and the environment", "https://cloud.arg.test", "https://cloud.env.test", "https://cloud.arg.test/v1/authorize", false],
  ["an empty argument and the environment", "", "https://cloud.env.test", "https://cloud.env.test/v1/authorize", false],
] as const) {
  test(`a hand-built HttpConsentClient with ${label} goes to ${expectedUrl}`, async () => {
    const prevBase = process.env.IDSIDE_BASE_URL;
    if (configured === undefined) delete process.env.IDSIDE_BASE_URL;
    else process.env.IDSIDE_BASE_URL = configured;
    try {
      const seen: string[] = [];
      const client = new HttpConsentClient(argument, {
        fetch: async (url: string | URL | Request) => {
          seen.push(String(url));
          return new Response("<html>Not Found</html>", { status: 404 });
        },
      });
      await assert.rejects(
        client.authorize({
          provider: "openai",
          model: "gpt-4o",
          requestHash: "sha256:abc",
          userId: "usr_marie_456",
        }),
        (error: unknown) => {
          assert.ok(error instanceof ConsentUnreachableError);
          assert.equal(
            error.message.includes("No ID Side address was configured"),
            named,
            error.message,
          );
          return true;
        },
      );
      assert.deepEqual(seen, [expectedUrl]);
    } finally {
      if (prevBase === undefined) delete process.env.IDSIDE_BASE_URL;
      else process.env.IDSIDE_BASE_URL = prevBase;
    }
  });
}

// The key, likewise: the `apiKey` option, then IDSIDE_API_KEY, an empty string
// counting as absent at each step, resolved in the constructor as Python's
// transport does. Node read the variable in `wrap()` alone, so a transport built
// by hand with IDSIDE_API_KEY set sent no Authorization header — and the cloud
// answered 401 to a key that was configured. The same table runs in Python.
const ARG_KEY = "sk_test_arg_key"; // pragma: allowlist secret
const ENV_KEY = "sk_test_env_key"; // pragma: allowlist secret
for (const [label, argument, configured, expected] of [
  ["an argument and the environment", ARG_KEY, ENV_KEY, `Bearer ${ARG_KEY}`],
  ["the environment and no argument", undefined, ENV_KEY, `Bearer ${ENV_KEY}`],
  ["neither argument nor environment", undefined, undefined, null],
  ["an empty environment variable", undefined, "", null],
  ["an empty argument and the environment", "", ENV_KEY, `Bearer ${ENV_KEY}`],
] as const) {
  test(`a hand-built HttpConsentClient with ${label} sends ${expected ?? "no Authorization"}`, async () => {
    const prevKey = process.env.IDSIDE_API_KEY;
    if (configured === undefined) delete process.env.IDSIDE_API_KEY;
    else process.env.IDSIDE_API_KEY = configured;
    try {
      const sent: (string | null)[] = [];
      const client = new HttpConsentClient("https://cloud.test", {
        ...(argument === undefined ? {} : { apiKey: argument }),
        fetch: async (_url: string | URL | Request, init?: RequestInit) => {
          sent.push(new Headers(init?.headers).get("authorization"));
          return new Response("{}", { status: 503 });
        },
      });
      await assert.rejects(
        client.authorize({
          provider: "openai",
          model: "gpt-4o",
          requestHash: "sha256:abc",
          userId: "usr_marie_456",
        }),
        ConsentUnreachableError,
      );
      assert.deepEqual(sent, [expected]);
    } finally {
      if (prevKey === undefined) delete process.env.IDSIDE_API_KEY;
      else process.env.IDSIDE_API_KEY = prevKey;
    }
  });
}
