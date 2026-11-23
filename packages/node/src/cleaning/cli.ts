#!/usr/bin/env node
// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * `idside-verify-clean`: replay prompt cleaning on a request that went out.
 *
 *     npx idside-verify-clean request.json --policy policy.json --provider openai
 *
 * `request.json` is the body the SDK sent to the provider; `policy.json` is the
 * cleaning policy the proof names (`--digest` checks it is that one). Exit 0:
 * cleaning that body again under that policy changes nothing. Exit 1: it would
 * still mask or block something, and the counts say what, never which value.
 * Exit 2: unreadable input, a policy that cannot be applied, a digest that is
 * not the one given, or an error that stopped the check before it answered:
 * nothing was checked. Python's twin is `idside-verify-clean` from the wheel.
 */

import { readFileSync, realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { loadDescriptors } from "../descriptors.ts";
import { clean, contentPolicyDigest, ENGINE_VERSION, rulesDigest } from "./index.ts";
import { LIMITS } from "./rules.ts";

/** Whether a JSON value nests past `limit` levels; Python's `_deeper_than()`. */
function deeperThan(value: unknown, limit: number): boolean {
  const pending: [unknown, number][] = [[value, 0]];
  while (pending.length > 0) {
    const [node, depth] = pending.pop() as [unknown, number];
    if (depth > limit) return true;
    if (Array.isArray(node)) for (const item of node) pending.push([item, depth + 1]);
    else if (typeof node === "object" && node !== null) for (const item of Object.values(node)) pending.push([item, depth + 1]);
  }
  return false;
}

interface Streams {
  out: (text: string) => void;
  err: (text: string) => void;
}

const DEFAULT_STREAMS: Streams = {
  out: (text) => process.stdout.write(text),
  err: (text) => process.stderr.write(text),
};

function summary(counts: Readonly<Record<string, number>>): string {
  return Object.keys(counts)
    .sort()
    .map((key) => `${key}: ${counts[key]}`)
    .join(", ");
}

const USAGE =
  "usage: idside-verify-clean REQUEST.json --policy POLICY.json --provider PROVIDER [--digest DIGEST]\n";

export function main(argv: readonly string[], streams: Streams = DEFAULT_STREAMS): number {
  try {
    return check(argv, streams);
  } catch (error) {
    // Exit 1 is an answer: the body would still be cleaned. An error no branch
    // below foresaw (an output that fails) must never read as that answer.
    streams.err(`idside-verify-clean: unexpected error, nothing was checked (${String(error)})\n`);
    return 2;
  }
}

function check(argv: readonly string[], streams: Streams): number {
  let values: { policy?: string; provider?: string; digest?: string; help?: boolean };
  let positionals: string[];
  try {
    ({ values, positionals } = parseArgs({
      args: [...argv],
      options: {
        policy: { type: "string" },
        provider: { type: "string" },
        digest: { type: "string" },
        help: { type: "boolean", short: "h" },
      },
      allowPositionals: true,
    }));
  } catch (error) {
    streams.err(`idside-verify-clean: ${(error as Error).message}\n${USAGE}`);
    return 2;
  }
  if (values.help) {
    streams.out(
      USAGE +
        "Check that a request body is a fixed point of an ID Side prompt-cleaning policy:\n" +
        "cleaning it again would mask nothing and block nothing.\n",
    );
    return 0;
  }
  const providers = [...loadDescriptors().keys()].sort();
  if (positionals.length !== 1 || !values.policy || !values.provider || !providers.includes(values.provider)) {
    streams.err(`${USAGE}PROVIDER is one of: ${providers.join(", ")}\n`);
    return 2;
  }

  let payload: unknown;
  let policy: unknown;
  try {
    payload = JSON.parse(readFileSync(positionals[0], "utf8"));
    policy = JSON.parse(readFileSync(values.policy, "utf8"));
  } catch (error) {
    streams.err(`idside-verify-clean: cannot read the input: ${(error as Error).message}\n`);
    return 2;
  }
  if (deeperThan(payload, LIMITS.max_depth)) {
    // Nothing is cleaned past this depth, so nothing can be checked: the same
    // answer whatever depth a JSON reader stops at (Python's stops early).
    streams.err(`idside-verify-clean: cannot read the input: nested deeper than ${LIMITS.max_depth} levels\n`);
    return 2;
  }

  let digest: string;
  try {
    digest = contentPolicyDigest(policy);
  } catch (error) {
    // A string with a lone surrogate: JSON.parse reads it, RFC 8785 cannot write it.
    streams.err(`idside-verify-clean: the policy is not JSON: ${(error as Error).message}\n`);
    return 2;
  }
  if (values.digest !== undefined && values.digest !== digest) {
    streams.err(
      `idside-verify-clean: the policy's digest is ${digest}, not ${values.digest}: this is not the policy the proof names\n`,
    );
    return 2;
  }
  let result;
  try {
    result = clean(payload, policy, values.provider);
  } catch (error) {
    // A policy this engine cannot apply (ContentPolicyError), or a body that is
    // not JSON: either way there is nothing to replay.
    streams.err(`idside-verify-clean: ${(error as Error).message}\n`);
    return 2;
  }
  streams.out(`policy  ${digest}\nengine  ${ENGINE_VERSION}\nrules   ${rulesDigest()}\n`);
  if (Object.keys(result.blocked).length > 0) {
    streams.out(`NOT CLEAN: this policy would block it (${summary(result.blocked)})\n`);
    return 1;
  }
  if (Object.keys(result.counts).length > 0) {
    streams.out(`NOT CLEAN: this policy would still mask ${summary(result.counts)}\n`);
    return 1;
  }
  streams.out("clean: nothing left to mask or block under this policy\n");
  return 0;
}

/** Whether this module is the program being run (directly, or through npm's bin link). */
export function isEntryPoint(moduleUrl: string, script: string | undefined): boolean {
  if (script === undefined) return false;
  try {
    return realpathSync(script) === realpathSync(fileURLToPath(moduleUrl));
  } catch {
    return false;
  }
}

/**
 * Run as the program. A write to a closed pipe or a full disk fails after
 * `main` has answered, as an `error` event on the stream: left unhandled, Node
 * would exit 1, which reads as "not clean" whatever was checked. Exit 2 says
 * the answer was not delivered.
 */
export function run(
  argv: readonly string[],
  outputs: readonly { on(event: "error", listener: () => void): unknown }[] = [process.stdout, process.stderr],
): void {
  for (const stream of outputs) stream.on("error", () => process.exit(2));
  process.exitCode = main(argv);
}

if (isEntryPoint(import.meta.url, process.argv[1])) run(process.argv.slice(2));
