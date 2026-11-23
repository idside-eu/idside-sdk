import { test } from "node:test";
import assert from "node:assert/strict";
import { clean } from "../src/cleaning/index.ts";

// Where the caller names the keys, a key is a name: what it holds is read.
//
// A provider's structural keys (`name`, `id`, `type`…) are never read: in the
// protocol they hold identifiers and enum-like settings. But some zones of a
// call are maps whose keys the caller chose — a prompt's variables, metadata,
// labels, the properties of a JSON Schema, a tool call's arguments, example
// inputs. There, a key spelled `name`, `id` or `type` is the caller's word, and
// what it holds is text like any other: an address under it is masked.
//
// One case per zone and per provider, each through `clean()` with the
// descriptor bundled for that provider. Python's twin is
// `tests/test_content_caller_keys.py`; the two lists are the same.

const ADDRESS = "marie@example.com";
const POLICY = { schema: 1, detectors: { email: "mask" }, attachments: "allow", custom: [] };

/** A JSON Schema with one property, named `key`, described with an address. */
function schema(key: string): Record<string, unknown> {
  return {
    type: "object",
    properties: { [key]: { type: "string", description: `Ask ${ADDRESS}` } },
    required: [key],
  };
}

// [provider, zone, call body]. Each body holds the address once, under a key
// the caller named `name`, `id` or `type`.
const CASES: [string, string, Record<string, unknown>][] = [
  // ── OpenAI ─────────────────────────────────────────────────────────────
  ["openai", "responses prompt.variables", { model: "gpt-4o", prompt: { id: "pmpt_1", variables: { name: ADDRESS } } }],
  ["openai", "chat metadata", { model: "gpt-4o", metadata: { id: ADDRESS } }],
  [
    "openai",
    "chat tools[].function.parameters",
    { model: "gpt-4o", tools: [{ type: "function", function: { name: "lookup", parameters: schema("type") } }] },
  ],
  [
    "openai",
    "chat response_format.json_schema.schema",
    { model: "gpt-4o", response_format: { type: "json_schema", json_schema: { name: "contact", schema: schema("name") } } },
  ],
  [
    "openai",
    "chat response_format.json_schema.schema $defs",
    {
      model: "gpt-4o",
      response_format: {
        type: "json_schema",
        json_schema: {
          name: "contact",
          schema: { $ref: "#/$defs/id", $defs: { id: { type: "string", description: ADDRESS } } },
        },
      },
    },
  ],
  [
    "openai",
    "responses tools[].parameters",
    { model: "gpt-4o", tools: [{ type: "function", name: "lookup", parameters: schema("id") }] },
  ],
  [
    "openai",
    "responses text.format.schema",
    { model: "gpt-4o", text: { format: { type: "json_schema", name: "c", schema: schema("name") } } },
  ],
  [
    "openai",
    "chat tool message",
    { model: "gpt-4o", messages: [{ role: "tool", tool_call_id: "call_1", content: JSON.stringify({ name: ADDRESS }) }] },
  ],
  [
    "openai",
    "chat assistant tool_calls[].function.arguments",
    {
      model: "gpt-4o",
      messages: [
        {
          role: "assistant",
          tool_calls: [
            { id: "call_1", type: "function", function: { name: "lookup", arguments: JSON.stringify({ id: ADDRESS }) } },
          ],
        },
      ],
    },
  ],
  [
    "openai",
    "responses function_call_output.output",
    {
      model: "gpt-4o",
      input: [{ type: "function_call_output", call_id: "call_1", output: JSON.stringify({ type: ADDRESS }) }],
    },
  ],
  // ── Anthropic ──────────────────────────────────────────────────────────
  ["anthropic", "tools[].input_schema", { model: "claude-sonnet-4-5", tools: [{ name: "lookup", input_schema: schema("name") }] }],
  [
    "anthropic",
    "tools[].input_examples",
    {
      model: "claude-sonnet-4-5",
      tools: [{ name: "lookup", input_schema: { type: "object" }, input_examples: [{ id: ADDRESS }] }],
    },
  ],
  [
    "anthropic",
    "output_config.format.schema",
    { model: "claude-sonnet-4-5", output_config: { format: { type: "json_schema", schema: schema("type") } } },
  ],
  [
    "anthropic",
    "tool_use.input",
    {
      model: "claude-sonnet-4-5",
      messages: [{ role: "assistant", content: [{ type: "tool_use", id: "toolu_1", name: "lookup", input: { name: ADDRESS } }] }],
    },
  ],
  [
    "anthropic",
    "tool_result.content",
    {
      model: "claude-sonnet-4-5",
      messages: [
        {
          role: "user",
          content: [{ type: "tool_result", tool_use_id: "toolu_1", content: JSON.stringify({ id: ADDRESS }) }],
        },
      ],
    },
  ],
  // ── Gemini ─────────────────────────────────────────────────────────────
  ["gemini", "config.labels", { model: "gemini-2.5-flash", config: { labels: { name: ADDRESS } } }],
  [
    "gemini",
    "config.tools[].function_declarations[].parameters",
    {
      model: "gemini-2.5-flash",
      config: { tools: [{ function_declarations: [{ name: "lookup", parameters: schema("id") }] }] },
    },
  ],
  ["gemini", "config.response_schema", { model: "gemini-2.5-flash", config: { response_schema: schema("type") } }],
  [
    "gemini",
    "config.responseJsonSchema $defs",
    {
      model: "gemini-2.5-flash",
      config: { responseJsonSchema: { $ref: "#/$defs/name", $defs: { name: { type: "string", description: ADDRESS } } } },
    },
  ],
  [
    "gemini",
    "functionCall.args",
    {
      model: "gemini-2.5-flash",
      contents: [{ role: "model", parts: [{ functionCall: { name: "lookup", args: { id: ADDRESS } } }] }],
    },
  ],
  [
    "gemini",
    "functionResponse.response",
    {
      model: "gemini-2.5-flash",
      contents: [{ role: "user", parts: [{ functionResponse: { name: "lookup", response: { name: ADDRESS } } }] }],
    },
  ],
  // ── Mistral ────────────────────────────────────────────────────────────
  [
    "mistral",
    "tools[].function.parameters",
    {
      model: "mistral-large-latest",
      tools: [{ type: "function", function: { name: "lookup", parameters: schema("name") } }],
    },
  ],
  [
    "mistral",
    "response_format.json_schema.schema",
    {
      model: "mistral-large-latest",
      response_format: { type: "json_schema", json_schema: { name: "contact", schema: schema("id") } },
    },
  ],
  ["mistral", "metadata", { model: "mistral-large-latest", metadata: { type: ADDRESS } }],
  [
    "mistral",
    "tool_calls[].function.arguments as an object",
    {
      model: "mistral-large-latest",
      messages: [{ role: "assistant", tool_calls: [{ id: "call_1", function: { name: "lookup", arguments: { name: ADDRESS } } }] }],
    },
  ],
  // ── DeepSeek ───────────────────────────────────────────────────────────
  [
    "deepseek",
    "tools[].function.parameters",
    { model: "deepseek-chat", tools: [{ type: "function", function: { name: "lookup", parameters: schema("type") } }] },
  ],
  [
    "deepseek",
    "assistant tool_calls[].function.arguments",
    {
      model: "deepseek-chat",
      messages: [
        {
          role: "assistant",
          tool_calls: [
            { id: "call_1", type: "function", function: { name: "lookup", arguments: JSON.stringify({ id: ADDRESS }) } },
          ],
        },
      ],
    },
  ],
];

for (const [provider, zone, body] of CASES) {
  test(`a value under a key the caller named is read — ${provider}: ${zone}`, () => {
    const result = clean(body, POLICY, provider);
    assert.deepEqual({ ...result.counts }, { email: 1 });
    const written = JSON.stringify(result.payload);
    assert.ok(!written.includes(ADDRESS), written);
    assert.ok(written.includes("[EMAIL_1]"), written);
  });
}

for (const provider of ["openai", "mistral", "deepseek"]) {
  test(`the protocol keys around them stay structural — ${provider}`, () => {
    // A tool's name and type are the protocol's: never read, so never changed
    // — even holding what a rule would mask.
    const body = {
      model: ADDRESS,
      tools: [{ type: "function", function: { name: ADDRESS, parameters: schema("name") } }],
    };
    const result = clean(body, POLICY, provider);
    assert.deepEqual({ ...result.counts }, { email: 1 });
    const payload = result.payload as typeof body;
    assert.equal(payload.model, ADDRESS);
    assert.equal(payload.tools[0].function.name, ADDRESS);
    assert.equal(payload.tools[0].type, "function");
  });
}

// Every map of a JSON Schema whose keys the caller names, on every provider,
// where that provider's tools carry a schema.
const SCHEMA_MAPS = ["$defs", "definitions", "patternProperties", "properties"];

function toolWith(provider: string, schema: Record<string, unknown>): Record<string, unknown> {
  if (provider === "anthropic") return { tools: [{ name: "lookup", input_schema: schema }] };
  if (provider === "gemini") {
    return { config: { tools: [{ function_declarations: [{ name: "lookup", parameters: schema }] }] } };
  }
  return { tools: [{ type: "function", function: { name: "lookup", parameters: schema } }] };
}

for (const provider of ["openai", "anthropic", "gemini", "mistral", "deepseek"]) {
  for (const key of [...SCHEMA_MAPS, ...(provider === "gemini" ? ["defs"] : [])]) {
    test(`every schema map the caller names is read — ${provider}: ${key}`, () => {
      const schema = { type: "object", [key]: { name: { type: "string", description: ADDRESS } } };
      const result = clean(toolWith(provider, schema), POLICY, provider);
      assert.deepEqual({ ...result.counts }, { email: 1 });
      assert.ok(!JSON.stringify(result.payload).includes(ADDRESS));
    });
  }
}

// A key the caller names (`caller_keyed_keys`) is a name, not a free-form zone:
// what it holds is read by the usual rules, attachments included.
test("an image passed as a prompt variable is still an attachment", () => {
  const image = { type: "input_image", image_url: "https://img.example.com/a.png" };
  const body = { model: "gpt-4o", prompt: { id: "pmpt_1", variables: { name: ADDRESS, type: image } } };
  const result = clean(body, POLICY, "openai");
  assert.deepEqual({ ...result.counts }, { email: 1 });
  assert.equal(result.attachments, 1);
});

test("a schema type under properties stays structural", () => {
  const policy = { ...POLICY, custom: [{ kind: "term", value: "string", action: "mask", label: "WORD" }] };
  const body = { model: "gpt-4o", tools: [{ type: "function", function: { name: "f", parameters: schema("name") } }] };
  const result = clean(body, policy, "openai") as { payload: { tools: { function: { parameters: { properties: { name: { type: string } } } } }[] } };
  assert.equal(result.payload.tools[0].function.parameters.properties.name.type, "string");
});
