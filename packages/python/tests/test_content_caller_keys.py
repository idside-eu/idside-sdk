"""Where the caller names the keys, a key is a name: what it holds is read.

A provider's structural keys (``name``, ``id``, ``type``…) are never read: in
the protocol they hold identifiers and enum-like settings. But some zones of a
call are maps whose keys the caller chose — a prompt's variables, metadata,
labels, the properties of a JSON Schema, a tool call's arguments, example
inputs. There, a key spelled ``name``, ``id`` or ``type`` is the caller's word,
and what it holds is text like any other: an address under it is masked.

One case per zone and per provider, each through the public ``clean()`` with
the descriptor bundled for that provider. Node's twin is
``test/contentCallerKeys.test.ts``; the two lists are the same.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from idside._cleaning import clean

ADDRESS = "marie@example.com"
POLICY = {
    "schema": 1,
    "detectors": {"email": "mask"},
    "attachments": "allow",
    "custom": [],
}


def _schema(key: str) -> dict[str, Any]:
    """A JSON Schema with one property, named ``key``, described with an address."""
    return {
        "type": "object",
        "properties": {key: {"type": "string", "description": f"Ask {ADDRESS}"}},
        "required": [key],
    }


#: (provider, zone, call body). Each body holds the address once, under a key
#: the caller named `name`, `id` or `type`.
CASES: list[tuple[str, str, dict[str, Any]]] = [
    # ── OpenAI ─────────────────────────────────────────────────────────────
    (
        "openai",
        "responses prompt.variables",
        {"model": "gpt-4o", "prompt": {"id": "pmpt_1", "variables": {"name": ADDRESS}}},
    ),
    ("openai", "chat metadata", {"model": "gpt-4o", "metadata": {"id": ADDRESS}}),
    (
        "openai",
        "chat tools[].function.parameters",
        {
            "model": "gpt-4o",
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": _schema("type")},
                }
            ],
        },
    ),
    (
        "openai",
        "chat response_format.json_schema.schema",
        {
            "model": "gpt-4o",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "contact", "schema": _schema("name")},
            },
        },
    ),
    (
        "openai",
        "chat response_format.json_schema.schema $defs",
        {
            "model": "gpt-4o",
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "contact",
                    "schema": {
                        "$ref": "#/$defs/id",
                        "$defs": {"id": {"type": "string", "description": ADDRESS}},
                    },
                },
            },
        },
    ),
    (
        "openai",
        "responses tools[].parameters",
        {
            "model": "gpt-4o",
            "tools": [
                {"type": "function", "name": "lookup", "parameters": _schema("id")}
            ],
        },
    ),
    (
        "openai",
        "responses text.format.schema",
        {
            "model": "gpt-4o",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "c",
                    "schema": _schema("name"),
                }
            },
        },
    ),
    (
        "openai",
        "chat tool message",
        {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": json.dumps({"name": ADDRESS}),
                }
            ],
        },
    ),
    (
        "openai",
        "chat assistant tool_calls[].function.arguments",
        {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": json.dumps({"id": ADDRESS}),
                            },
                        }
                    ],
                }
            ],
        },
    ),
    (
        "openai",
        "responses function_call_output.output",
        {
            "model": "gpt-4o",
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": json.dumps({"type": ADDRESS}),
                }
            ],
        },
    ),
    # ── Anthropic ──────────────────────────────────────────────────────────
    (
        "anthropic",
        "tools[].input_schema",
        {
            "model": "claude-sonnet-4-5",
            "tools": [{"name": "lookup", "input_schema": _schema("name")}],
        },
    ),
    (
        "anthropic",
        "tools[].input_examples",
        {
            "model": "claude-sonnet-4-5",
            "tools": [
                {
                    "name": "lookup",
                    "input_schema": {"type": "object"},
                    "input_examples": [{"id": ADDRESS}],
                }
            ],
        },
    ),
    (
        "anthropic",
        "output_config.format.schema",
        {
            "model": "claude-sonnet-4-5",
            "output_config": {
                "format": {"type": "json_schema", "schema": _schema("type")}
            },
        },
    ),
    (
        "anthropic",
        "tool_use.input",
        {
            "model": "claude-sonnet-4-5",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "lookup",
                            "input": {"name": ADDRESS},
                        }
                    ],
                }
            ],
        },
    ),
    (
        "anthropic",
        "tool_result.content",
        {
            "model": "claude-sonnet-4-5",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": json.dumps({"id": ADDRESS}),
                        }
                    ],
                }
            ],
        },
    ),
    # ── Gemini ─────────────────────────────────────────────────────────────
    (
        "gemini",
        "config.labels",
        {"model": "gemini-2.5-flash", "config": {"labels": {"name": ADDRESS}}},
    ),
    (
        "gemini",
        "config.tools[].function_declarations[].parameters",
        {
            "model": "gemini-2.5-flash",
            "config": {
                "tools": [
                    {
                        "function_declarations": [
                            {"name": "lookup", "parameters": _schema("id")}
                        ]
                    }
                ]
            },
        },
    ),
    (
        "gemini",
        "config.response_schema",
        {
            "model": "gemini-2.5-flash",
            "config": {"response_schema": _schema("type")},
        },
    ),
    (
        "gemini",
        "config.responseJsonSchema $defs",
        {
            "model": "gemini-2.5-flash",
            "config": {
                "responseJsonSchema": {
                    "$ref": "#/$defs/name",
                    "$defs": {"name": {"type": "string", "description": ADDRESS}},
                }
            },
        },
    ),
    (
        "gemini",
        "functionCall.args",
        {
            "model": "gemini-2.5-flash",
            "contents": [
                {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"name": "lookup", "args": {"id": ADDRESS}}}
                    ],
                }
            ],
        },
    ),
    (
        "gemini",
        "functionResponse.response",
        {
            "model": "gemini-2.5-flash",
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "lookup",
                                "response": {"name": ADDRESS},
                            }
                        }
                    ],
                }
            ],
        },
    ),
    # ── Mistral ────────────────────────────────────────────────────────────
    (
        "mistral",
        "tools[].function.parameters",
        {
            "model": "mistral-large-latest",
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": _schema("name")},
                }
            ],
        },
    ),
    (
        "mistral",
        "response_format.json_schema.schema",
        {
            "model": "mistral-large-latest",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "contact", "schema": _schema("id")},
            },
        },
    ),
    (
        "mistral",
        "metadata",
        {"model": "mistral-large-latest", "metadata": {"type": ADDRESS}},
    ),
    (
        "mistral",
        "tool_calls[].function.arguments as an object",
        {
            "model": "mistral-large-latest",
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "lookup",
                                "arguments": {"name": ADDRESS},
                            },
                        }
                    ],
                }
            ],
        },
    ),
    # ── DeepSeek ───────────────────────────────────────────────────────────
    (
        "deepseek",
        "tools[].function.parameters",
        {
            "model": "deepseek-chat",
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": _schema("type")},
                }
            ],
        },
    ),
    (
        "deepseek",
        "assistant tool_calls[].function.arguments",
        {
            "model": "deepseek-chat",
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": json.dumps({"id": ADDRESS}),
                            },
                        }
                    ],
                }
            ],
        },
    ),
]


@pytest.mark.parametrize(
    ("provider", "body"),
    [
        pytest.param(provider, body, id=f"{provider}: {zone}")
        for provider, zone, body in CASES
    ],
)
def test_a_value_under_a_key_the_caller_named_is_read(
    provider: str, body: dict[str, Any]
) -> None:
    result = clean(body, POLICY, provider=provider)
    assert result.counts == {"email": 1}
    written = json.dumps(result.payload)
    assert ADDRESS not in written
    assert "[EMAIL_1]" in written


@pytest.mark.parametrize("provider", ["openai", "mistral", "deepseek"])
def test_the_protocol_keys_around_them_stay_structural(provider: str) -> None:
    # A tool's name and type, and a schema format's name, are the protocol's:
    # never read, so never changed — even holding what a rule would mask.
    body = {
        "model": ADDRESS,
        "tools": [
            {
                "type": "function",
                "function": {"name": ADDRESS, "parameters": _schema("name")},
            }
        ],
    }
    result = clean(body, POLICY, provider=provider)
    assert result.counts == {"email": 1}
    assert result.payload["model"] == ADDRESS
    assert result.payload["tools"][0]["function"]["name"] == ADDRESS
    assert result.payload["tools"][0]["type"] == "function"


#: Every map of a JSON Schema whose keys the caller names, on every provider,
#: where that provider's tools carry a schema.
SCHEMA_MAPS = ["$defs", "definitions", "patternProperties", "properties"]


def _tool_with(provider: str, schema: dict[str, Any]) -> dict[str, Any]:
    if provider == "anthropic":
        return {"tools": [{"name": "lookup", "input_schema": schema}]}
    if provider == "gemini":
        declaration = {"name": "lookup", "parameters": schema}
        return {"config": {"tools": [{"function_declarations": [declaration]}]}}
    return {
        "tools": [
            {"type": "function", "function": {"name": "lookup", "parameters": schema}}
        ]
    }


@pytest.mark.parametrize(
    ("provider", "key"),
    [
        (provider, key)
        for provider in ["openai", "anthropic", "gemini", "mistral", "deepseek"]
        for key in SCHEMA_MAPS + (["defs"] if provider == "gemini" else [])
    ],
)
def test_every_schema_map_the_caller_names_is_read(provider: str, key: str) -> None:
    schema = {
        "type": "object",
        key: {"name": {"type": "string", "description": ADDRESS}},
    }
    result = clean(_tool_with(provider, schema), POLICY, provider=provider)
    assert result.counts == {"email": 1}
    assert ADDRESS not in json.dumps(result.payload)


def test_an_image_passed_as_a_prompt_variable_is_still_an_attachment() -> None:
    # A key the caller names (`caller_keyed_keys`) is a name, not a free-form
    # zone: what it holds is read by the usual rules, attachments included.
    image = {"type": "input_image", "image_url": "https://img.example.com/a.png"}
    body = {
        "model": "gpt-4o",
        "prompt": {"id": "pmpt_1", "variables": {"name": ADDRESS, "type": image}},
    }
    result = clean(body, POLICY, provider="openai")
    assert result.counts == {"email": 1}
    assert result.attachments == 1


def test_a_schema_type_under_properties_stays_structural() -> None:
    word = {"kind": "term", "value": "string", "action": "mask", "label": "WORD"}
    policy = {**POLICY, "custom": [word]}
    tool = {"name": "f", "parameters": _schema("name")}
    body = {"model": "gpt-4o", "tools": [{"type": "function", "function": tool}]}
    result = clean(body, policy, provider="openai")
    prop = result.payload["tools"][0]["function"]["parameters"]["properties"]["name"]
    assert prop["type"] == "string"
