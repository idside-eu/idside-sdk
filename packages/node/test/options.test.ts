/**
 * What an app may ask its users, given the providers it wraps.
 *
 * The rule these tests protect: a question reaches an end user only if at least
 * one of the app's providers can enforce it PER REQUEST. An account-level
 * control lives on the developer's API key and applies to all of their users at
 * once, so asking one person about it would manufacture the appearance of a
 * choice nobody has.
 *
 * The last test is the one that matters most: Python and Node must produce the
 * SAME dialog for the same providers. A consent question that appears in one
 * SDK and not the other is a user asked something their neighbour was not.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { catalogue, decidableOptions, disclosures } from "../src/index.ts";
import { rawDescriptors } from "../src/descriptors.ts";

const ALL = ["openai", "anthropic", "gemini", "mistral", "deepseek"];

test("asks only what a provider enforces per request", () => {
  assert.deepEqual(
    decidableOptions(["openai"]).map((o) => o.name),
    ["agentic_tools", "retention.conversation", "retention.cache"],
  );
});

test("never asks about a control that lives on the API key", () => {
  const names = decidableOptions(["mistral"]).map((o) => o.name);
  assert.ok(!names.includes("training.foundation"));
});

test("returns the union for an app wrapping several providers, asked once each", () => {
  const both = decidableOptions(["openai", "anthropic"]).map((o) => o.name);
  const expected = new Set([
    ...decidableOptions(["openai"]).map((o) => o.name),
    ...decidableOptions(["anthropic"]).map((o) => o.name),
  ]);

  assert.deepEqual(new Set(both), expected);
  assert.equal(both.length, new Set(both).size);
});

test("order does not depend on which client was wrapped first", () => {
  assert.deepEqual(
    decidableOptions(["openai", "anthropic"]).map((o) => o.name),
    decidableOptions(["anthropic", "openai"]).map((o) => o.name),
  );
});

test("says which providers will honour each answer", () => {
  const options = new Map(decidableOptions(["openai", "mistral"]).map((o) => [o.name, o]));

  // Two providers, one question, and only one of them enforces the cache.
  assert.deepEqual(options.get("retention.cache")!.enforcedBy, ["openai"]);
  assert.deepEqual(options.get("retention.conversation")!.enforcedBy, ["openai", "mistral"]);
});

// `agentic_tools` was published as enforced once before, on all four providers,
// while the mechanism existed nowhere in either SDK — a claim with nothing
// behind it in the one document a customer repeats to their DPO. It was demoted
// to `unsupported`, which left Gemini and Mistral with nothing to ask at all,
// and the mechanism has now been written. These numbers are the difference
// between the two, and they are pinned because the first version of them was a
// lie. Mistral went from 1 to 2 on 2026-07-26: `beta.conversations.start` was
// added as a call site, and it carries a real `store`, so
// `retention.conversation` became a question its users can actually be asked.
//
// Anthropic went from 2 to 1 on 2026-07-27, and for the same reason in reverse.
// `data_residency` was declared enforced on the strength of `inference_geo`
// being a real parameter — which it is. What does not exist is a VALUE to force:
// its settings are `global` and `us`, and neither answers "inside my region" for
// a European user, whose region we never learn. The popup asked the question and
// nothing acted on the answer.
// Mirrors test_options.py.
for (const [provider, expected] of [
  ["openai", 3],
  ["anthropic", 1],
  ["gemini", 1],
  ["mistral", 2],
  ["deepseek", 1],
] as const) {
  test(`${provider} can be asked ${expected} question(s)`, () => {
    assert.equal(decidableOptions([provider]).length, expected);
  });
}

test("tool refusal is the one question every provider can answer", () => {
  // It works because the SDK sits on the outgoing request and can see what the
  // developer is about to send — not because any provider offers it.
  for (const provider of ALL) {
    assert.ok(
      decidableOptions([provider]).some((o) => o.name === "agentic_tools"),
      provider,
    );
  }
});

test("a provider whose answers are all elsewhere is not hidden", () => {
  assert.deepEqual(
    decidableOptions(["gemini"]).map((o) => o.name),
    ["agentic_tools"], // and nothing else: the rest is account-level
  );
  assert.ok(disclosures(["gemini"]).length > 0, "then the rest must appear as disclosures");
});

test("a name that is not a known provider never becomes a path", () => {
  for (const evil of ["../../../etc/passwd", "/etc/passwd", "../consent-options"]) {
    assert.deepEqual(decidableOptions([evil]), []);
  }
});

test("an unknown provider contributes nothing rather than throwing", () => {
  assert.deepEqual(decidableOptions(["not-a-provider"]), []);
  assert.deepEqual(
    decidableOptions(["openai", "not-a-provider"]).map((o) => o.name),
    decidableOptions(["openai"]).map((o) => o.name),
  );
});

test("every default is a refusal", () => {
  for (const option of decidableOptions(ALL)) assert.equal(option.default, false);
});

test("marks what a second credential would give users back", () => {
  const found = disclosures(["mistral"]).find((d) => d.option.name === "training.foundation");

  assert.equal(found?.kind, "beyond_user_control");
  assert.ok(found?.routableFrom.includes("mistral"));
});

// `contractual` says the terms decide, not which way they decide. Every provider
// that writes about human review lets its staff review flagged content, and both
// OpenAI's and Anthropic's no-training clauses give way to an opt-in the
// customer's organization can enable. Calling any of them a guarantee tells a
// user the opposite of the terms, in the helper a developer builds their own
// dialog from.
test("terms that allow the processing are no guarantee", () => {
  const kind = (provider: string, name: string) =>
    disclosures([provider]).find((d) => d.option.name === name)?.kind;

  assert.equal(kind("anthropic", "training.foundation"), "beyond_user_control");
  assert.equal(kind("anthropic", "human_review"), "beyond_user_control");
  assert.equal(kind("anthropic", "retention.conversation"), "beyond_user_control");
  assert.equal(kind("openai", "human_review"), "beyond_user_control");
  assert.equal(kind("openai", "training.foundation"), "beyond_user_control");
});

// No descriptor says `protective: true` today, so a synthetic one does. The
// branch is the one a dialog shows as good news; leaving it untested because no
// provider earns it right now is how it would break unseen the day one does.
// The bundled JSON is the very object the helpers read, so it is set and put
// back here, whatever the assertions do.
test("terms that rule the processing out are a guarantee", () => {
  const entries = ["anthropic", "openai"].map(
    (provider) => (rawDescriptors().get(provider)?.enforcement as Record<string, { protective?: boolean }>)["training.foundation"]!,
  );
  const saved = entries.map((entry) => entry.protective);
  const kind = (providers: string[]) =>
    disclosures(providers).find((d) => d.option.name === "training.foundation");
  try {
    entries[0]!.protective = true;
    entries[1]!.protective = false;

    assert.equal(kind(["anthropic"])?.kind, "guaranteed");
    // One provider whose terms allow it is enough for no guarantee to hold.
    assert.equal(kind(["anthropic", "openai"])?.kind, "beyond_user_control");
    assert.deepEqual(kind(["anthropic", "openai"])?.byProvider, { anthropic: "contractual", openai: "contractual" });

    entries[1]!.protective = true;
    assert.equal(kind(["anthropic", "openai"])?.kind, "guaranteed");
  } finally {
    entries.forEach((entry, i) => (entry.protective = saved[i]));
  }
});

// `absent` says what the provider does; the disclosure says who decided. Nobody
// configured memory on an OpenAI API app — the API has none — so it is stated as
// a control that does not exist, never as a decision the app made for its users.
// Beside a provider that has no switch for it either, the statement is the same.
test("a processing the API does not perform is not offered", () => {
  const memory = disclosures(["openai"]).find((d) => d.option.name === "memory");

  assert.equal(memory?.kind, "not_offered");
  assert.deepEqual(memory?.byProvider, { openai: "absent" });

  const both = disclosures(["openai", "anthropic"]).find((d) => d.option.name === "memory");

  assert.equal(both?.kind, "not_offered");
  assert.deepEqual(both?.byProvider, { openai: "absent", anthropic: "unsupported" });
});

test("nothing vanishes between what is asked and what is stated", () => {
  const asked = new Set(decidableOptions(ALL).map((o) => o.name));
  const stated = new Set(disclosures(ALL).map((d) => d.option.name));
  const everything = new Set(catalogue().options.map((o) => o.name));

  for (const name of everything) {
    assert.ok(asked.has(name) || stated.has(name), `${name} is neither asked nor stated`);
  }
  for (const name of asked) assert.ok(!stated.has(name), `${name} is both asked and stated`);
});

// ── What an app may ask once it holds more than one credential ────────────
//
// A question reaches a user only if the app can honour two different answers.
// For an account-level setting that means credential routing: one credential per
// posture, and the SDK sends each call on the one the answer requires. So the
// same option is a disclosure for an app with one key and a real question for
// the app next to it — the descriptor cannot decide alone, and neither can the
// SDK. It takes the app's own configuration.
//
// Mirrors TestRoutedOptions in packages/python/tests/test_options.py.

const PROVISIONED = { mistral: ["default", "no_training"] };

test("a second credential turns a disclosure into a question", () => {
  const before = decidableOptions(["mistral"]).map((o) => o.name);
  const after = decidableOptions(["mistral"], PROVISIONED).map((o) => o.name);

  assert.ok(!before.includes("training.foundation"));
  assert.ok(after.includes("training.foundation"));
});

test("one credential is not a choice", () => {
  // One posture for everyone is what the app already had.
  const options = decidableOptions(["mistral"], { mistral: ["default"] }).map((o) => o.name);

  assert.ok(!options.includes("training.foundation"));
});

test("two labels for the same posture are still one posture", () => {
  const options = decidableOptions(["mistral"], { mistral: ["a", "a"] }).map((o) => o.name);

  assert.ok(!options.includes("training.foundation"));
});

test("a routed option says it is routed rather than forced", () => {
  // The answer is as real either way; a UI may want to say which.
  const option = decidableOptions(["mistral"], PROVISIONED).find((o) => o.name === "training.foundation")!;

  assert.deepEqual(option.routedBy, ["mistral"]);
  assert.deepEqual(option.enforcedBy, []);
});

test("routing cannot invent a question the provider never offers", () => {
  // `memory` is not routable anywhere: no credential carries it.
  const options = decidableOptions(["mistral"], PROVISIONED).map((o) => o.name);

  assert.ok(!options.includes("memory"));
});

// Two projects, two organisations: human review is still not a choice. Every
// Gemini project lets Google's staff assess flagged content, and no Mistral
// console setting reaches human review; asking a user to refuse it, because the
// app holds two keys, is the false choice this module exists to prevent.
test("routing never asks what no posture honours", () => {
  const routed = { gemini: ["default", "billed"], mistral: ["default", "zdr"] };
  const asked = decidableOptions(["gemini", "mistral"], routed).map((o) => o.name);
  const stated = disclosures(["gemini", "mistral"], routed).find((d) => d.option.name === "human_review");

  assert.ok(!asked.includes("human_review"));
  assert.equal(stated?.kind, "beyond_user_control");
  assert.deepEqual(stated?.routableFrom, []);
});

test("what became a question stops being a disclosure", () => {
  const stated = disclosures(["mistral"], PROVISIONED).map((d) => d.option.name);

  assert.ok(!stated.includes("training.foundation"));
});

test("routing one provider says nothing about another", () => {
  const options = decidableOptions(["mistral", "gemini"], PROVISIONED);
  const option = options.find((o) => o.name === "training.foundation")!;

  assert.deepEqual(option.routedBy, ["mistral"]);
});

test("an app that wraps no provider has nothing to ask and nothing to state", () => {
  // Not a degenerate case to shrug at: a dialog built from an empty provider
  // list must be empty, not a page of questions whose answers could not reach
  // anything. Every option is dropped because no provider's enforcement
  // mentions it at all.
  assert.deepEqual(decidableOptions([]), []);
  assert.deepEqual(disclosures([]), []);
});
