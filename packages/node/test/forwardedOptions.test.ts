/**
 * What an enforced call forwards to the provider once its options are checked.
 *
 * One option is not forwarded as given: openai's `__metadata.captureResponseParser`,
 * a hook the library calls with the request it is building. `responses.stream()`
 * puts its own there, so it cannot be refused; it is handed a read-only view of
 * the body instead of the request. These tests pin every path through that
 * substitution, and that the view refuses every kind of write.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { assertEnforceableOptions, forwardableOptions } from "../src/adapters/callOptions.ts";
import { loadDescriptors } from "../src/descriptors.ts";
import { UnenforceableOptionError } from "../src/exceptions.ts";

const OPENAI = loadDescriptors().get("openai")!;
const ANTHROPIC = loadDescriptors().get("anthropic")!;
const GEMINI = loadDescriptors().get("gemini")!;

/** The view a hook is handed, captured from a forwarded call. */
function viewOf(body: unknown): Record<string, unknown> {
  let seen: unknown;
  const [options] = forwardableOptions(OPENAI, [{ __metadata: { captureResponseParser: (p: unknown) => (seen = p) } }]);
  const hook = (options as { __metadata: { captureResponseParser(p: unknown): unknown } }).__metadata.captureResponseParser;
  hook({ body, headers: { authorization: "Bearer sk-test" }, fetchOptions: {} });
  return seen as Record<string, unknown>;
}

test("everything but the hook is forwarded as it was given", () => {
  const options = { timeout: 5, headers: { "X-Request-Id": "req_42" } };
  assert.deepEqual(forwardableOptions(OPENAI, [options, "tail"]), [options, "tail"]);
  assert.deepEqual(forwardableOptions(OPENAI, []), []);
  assert.deepEqual(forwardableOptions(OPENAI, [{ __metadata: { helperMethod: "stream" } }]), [
    { __metadata: { helperMethod: "stream" } },
  ]);
  assert.deepEqual(forwardableOptions(OPENAI, [{ __metadata: "stream" }]), [{ __metadata: "stream" }]);
  const hook = () => undefined;
  // Another library's options are never rewritten, whatever they hold.
  const foreign = { __metadata: { captureResponseParser: hook } };
  assert.equal(forwardableOptions(ANTHROPIC, [foreign])[0], foreign);
});

test("the hook is handed the body alone, and the caller's options are left untouched", () => {
  const hook = () => undefined;
  const given = { __metadata: { captureResponseParser: hook, helperMethod: "stream" } };
  const [forwarded] = forwardableOptions(OPENAI, [given]);
  assert.notEqual(forwarded, given);
  assert.equal(given.__metadata.captureResponseParser, hook, "the caller's object is not rewritten");
  assert.equal((forwarded as typeof given).__metadata.helperMethod, "stream");

  const view = viewOf({ model: "gpt-4o", text: { format: { type: "json_schema" } } });
  assert.deepEqual(Object.keys(view), ["body"]);
  assert.equal((view.body as { model: string }).model, "gpt-4o");
  assert.equal(((view.body as { text: { format: { type: string } } }).text.format.type), "json_schema");
  assert.equal(viewOf(undefined).body, undefined);
  assert.equal(viewOf("raw").body, "raw");
});

test("the view refuses every write, at any depth", () => {
  const body = { model: "gpt-4o", input: [{ type: "message" }] };
  const view = viewOf(body).body as Record<string, unknown>;
  const nested = (view.input as Record<string, unknown>[])[0]!;
  const writes: Array<() => unknown> = [
    () => (view.store = true),
    () => delete view.model,
    () => Object.defineProperty(view, "store", { value: true }),
    () => Object.setPrototypeOf(view, null),
    () => (nested.type = "additional_tools"),
    () => (view.input as unknown[]).push({ type: "additional_tools" }),
  ];
  for (const write of writes) assert.throws(write, TypeError);
  assert.deepEqual(body, { model: "gpt-4o", input: [{ type: "message" }] });
  assert.ok(Array.isArray(view.input), "an array still reads as one");
});

test("a frozen property reads as itself, as the Proxy rules require", () => {
  const format = Object.freeze({ type: "json_schema" });
  const body = {};
  Object.defineProperty(body, "format", { value: format, enumerable: true, writable: false, configurable: false });
  const view = viewOf(body).body as { format: unknown };
  assert.equal(view.format, format);
});

test("a refused body key or config field that asks for nothing is not refused", () => {
  assertEnforceableOptions(ANTHROPIC, { model: "claude-sonnet-4-5", workspace_id: undefined }, []);
  assertEnforceableOptions(GEMINI, { model: "gemini-2.0-flash", config: { generationOutputConfig: null } }, []);
  assertEnforceableOptions(GEMINI, { model: "gemini-2.0-flash", config: { automaticContinuation: false } }, []);
  assert.throws(
    () => assertEnforceableOptions(GEMINI, { model: "gemini-2.0-flash", config: { generationOutputConfig: {} } }, []),
    UnenforceableOptionError,
  );
});
