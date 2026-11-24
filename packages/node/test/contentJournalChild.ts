/**
 * The child half of the journal test in `contentPolicy.test.ts`. Not a test
 * file: no `*.test.ts` suffix, so the runner never picks it up on its own.
 *
 * Node reads `NODE_DEBUG` once, at boot, so the journal can only be read from a
 * process that had the variable from the start. This one makes a cleaned call,
 * a blocked call and a call whose policy cannot be fetched, against a cloud
 * that answers from memory, and exits 0 only if each did what it should.
 */

import { generateKeyPairSync, sign } from "node:crypto";
import { wrap } from "../src/consent.ts";
import { clock } from "../src/contentPolicy.ts";
import { contentPolicyDigest } from "../src/cleaning/index.ts";
import { ConsentUnreachableError, ContentBlockedError } from "../src/exceptions.ts";
import { HttpConsentClient } from "../src/transport.ts";

const NOW = 1_800_000_000;
clock.now = () => NOW;
const { privateKey, publicKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
const b64url = (data: string | Buffer) => Buffer.from(data).toString("base64url");
const policy = {
  schema: 1,
  app_id: "app_medhelp",
  revision: 7,
  detectors: { email: "mask", credit_card: "block" },
  attachments: "allow",
  custom: [{ kind: "term", value: "Projet Atlas", action: "mask", label: "PROJECT" }],
};
const input =
  b64url(JSON.stringify({ alg: "ES256", typ: "content-policy+jwt", kid: "content-policy-g1" })) +
  "." +
  b64url(
    JSON.stringify({
      iss: "idside.eu",
      app_id: "app_medhelp",
      iat: NOW - 10,
      exp: NOW + 890,
      digest: contentPolicyDigest(policy),
      policy,
    }),
  );
const token = input + "." + b64url(sign("sha256", Buffer.from(input), { key: privateKey, dsaEncoding: "ieee-p1363" }));

function cloud(policyStatus: number): typeof fetch {
  return (async (url: string | URL | Request, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
    if (path === "/v1/content-policy") {
      if (policyStatus !== 200) return json({}, policyStatus);
      return json({ enabled: true, policy: token, challenge: { token: "t", inputs: ["x"] }, refresh_after: 300 });
    }
    if (path === "/.well-known/jwks.json") return json({ keys: [{ ...publicKey.export({ format: "jwk" }), kid: "content-policy-g1" }] });
    if (path === "/v1/content-events") return new Response(null, { status: 204 });
    void init;
    return json({
      decision: "allow",
      consent_state: {},
      params_forced: {},
      mode: "test",
      act_token: "act.jwt.token",
      token_id: "act_01k4f9h2m3",
      expires_at: "2026-09-12T12:00:00Z",
      source: "default_policy",
      legal_basis: "legitimate_interest",
      binding: "user_id",
    });
  }) as typeof fetch;
}

function client(policyStatus: number) {
  return wrap(
    { chat: { completions: { create: async () => ({}) } } },
    { provider: "openai", consentClient: new HttpConsentClient("https://cloud.test", { fetch: cloud(policyStatus) }) },
  ) as any;
}

const messages = [
  { role: "system", content: "About Projet Atlas." },
  { role: "user", content: "Write to marie@example.com" },
];
await client(200).chat.completions.create({ model: "gpt-4o", messages, user_id: "usr_marie_456" });
await client(200)
  .chat.completions.create({ model: "gpt-4o", messages: [{ content: "4111111111111111" }], user_id: "u" })
  .then(
    () => process.exit(1),
    (error: unknown) => {
      if (!(error instanceof ContentBlockedError)) process.exit(1);
    },
  );
await client(503)
  .chat.completions.create({ model: "gpt-4o", messages, user_id: "u" })
  .then(
    () => process.exit(1),
    (error: unknown) => {
      if (!(error instanceof ConsentUnreachableError)) process.exit(1);
    },
  );
