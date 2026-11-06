/**
 * How one journal line is spelled — which is a cross-SDK contract, not a detail.
 *
 * `scripts/check_sdk_parity.sh` runs one enforced call through each SDK and
 * DIFFS the emitted lines. So the rendering rules here are load-bearing: an
 * array joined with `,` instead of `+`, an empty list rendered as `[]` instead
 * of `-`, or a `null` printed rather than dropped, each break the parity gate
 * for a reason no unit test in either SDK would otherwise state.
 *
 * `logging.test.ts` covers what the journal may and may not CONTAIN. This file
 * covers what it LOOKS like, plus the two decisions that are about cost rather
 * than content: nothing is formatted when nobody is listening, and a request
 * hash is truncated to its correlating half.
 *
 * Like `logging.test.ts`, the rendering half runs in a child: Node reads
 * `NODE_DEBUG` once at boot, so `debuglog("idside").enabled` cannot be turned on
 * from inside a process that started without it. Unlike `logging.test.ts`, the
 * child here is a plain script rather than a nested test runner — one hop, so
 * what it executes is visible to `--experimental-test-coverage`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { debuglog } from "node:util";
import { fileURLToPath } from "node:url";
import { event, shortHash, start } from "../src/log.ts";

const CHILD = fileURLToPath(new URL("./journalChild.ts", import.meta.url));

/** Run the child with the journal switched on, and hand back what it wrote. */
function childJournal(env: Record<string, string> = { NODE_DEBUG: "idside" }): string[] {
  const child = spawnSync(process.execPath, [CHILD], {
    env: { ...process.env, ...env },
    encoding: "utf8",
  });
  assert.equal(child.status, 0, `the journal child failed:\n${child.stderr}`);
  assert.equal(child.stdout, "", "the journal must never reach stdout");
  return child.stderr.split("\n").filter((line) => line.length > 0);
}

/** The message half of one `IDSIDE <pid>: <message>` line. */
function messages(lines: string[]): string[] {
  return lines.map((line) => {
    const match = /^IDSIDE \d+: (.*)$/.exec(line);
    assert.ok(match, `not a debuglog line: ${JSON.stringify(line)}`);
    return match[1]!;
  });
}

// ── the line shape both SDKs have to agree on ────────────────────────────────

test("one event renders as name then key=value, in the caller's order", () => {
  const [shapes] = messages(childJournal());

  assert.equal(
    shapes,
    "shapes yes=true no=false count=3 forced=background+store empty=- spaced=two_words",
  );
});

test("a list is sorted, joined with +, and an empty one is a dash", () => {
  // `forced=background+store` survives a grep and never spans a line; the
  // parameters a consent state forced are read far more often than parsed.
  // Sorted HERE and not only by the caller: two processes that forced the same
  // set must produce the same line, or the parity diff reports a difference
  // that is only an iteration order. `-` rather than nothing, so "the field
  // exists and is empty" is still said.
  const [shapes] = messages(childJournal());

  assert.match(shapes!, /(^| )forced=background\+store( |$)/);
  assert.match(shapes!, /(^| )empty=-( |$)/);
});

test("a value never spans two fields: spaces become underscores", () => {
  // A space is the field separator. A value containing one would make
  // `spaced=two words` parse as a field `spaced` and a field-less `words`.
  const [shapes] = messages(childJournal());

  assert.match(shapes!, /(^| )spaced=two_words( |$)/);
  assert.ok(!shapes!.includes("two words"));
});

test("null and undefined are dropped, never rendered", () => {
  // "the cloud named no posture" is said by the absence of `posture=`, not by
  // `posture=null`. Python drops them the same way and the diff compares both.
  const [shapes, , allDropped] = messages(childJournal());

  assert.ok(!shapes!.includes("absent="), "an undefined field was rendered");
  assert.ok(!shapes!.includes("none="), "a null field was rendered");
  assert.ok(!shapes!.includes("null"));
  assert.equal(allDropped, "all_dropped", "an event whose every field dropped is just its name");
});

test("an event with no fields is its name and nothing else", () => {
  const [, bare] = messages(childJournal());

  assert.equal(bare, "bare", "a trailing space would show up in every diff");
});

// ── silence, and what it costs ───────────────────────────────────────────────

test("without NODE_DEBUG the child writes nothing at all", () => {
  // The same code path, the same events, no channel. This is the default a
  // developer who never asked for a journal gets.
  const child = spawnSync(process.execPath, [CHILD], {
    env: { ...process.env, NODE_DEBUG: "" },
    encoding: "utf8",
  });

  assert.equal(child.status, 0);
  assert.equal(child.stdout, "");
  assert.equal(child.stderr, "", "a library that writes uninvited is a library that gets removed");
});

test("a section that is not ours does not switch ours on", () => {
  const child = spawnSync(process.execPath, [CHILD], {
    env: { ...process.env, NODE_DEBUG: "http" },
    encoding: "utf8",
  });

  assert.equal(child.status, 0);
  assert.equal(child.stderr, "");
});

test("nothing is formatted when nobody is listening", () => {
  // `event()` returns before touching its fields, so a caller may hand it a
  // value whose rendering would throw and pay nothing for it when the journal
  // is off. This process has no NODE_DEBUG, which is the whole assertion.
  assert.equal(debuglog("idside").enabled, false, "this suite must run with the journal off");

  const hostile = {
    get exploded(): string {
      throw new Error("a silent journal must not read its own fields");
    },
  };

  assert.doesNotThrow(() => event("never_rendered", hostile as never));
});

// ── the two helpers the enforcement path calls ───────────────────────────────

test("a request hash is truncated to its correlating half", () => {
  // The ledger indexes the request hash, so it is the honest answer to "which
  // proof was minted for this call?" — the ACT token is bearer material and is
  // never emitted. Truncated anyway: a full hash of a short prompt is a
  // dictionary attack away from the prompt.
  const digest = "a".repeat(64);

  assert.equal(shortHash(`sha256:${digest}`), "a".repeat(12));
});

test("a hash with no algorithm prefix is truncated from the start", () => {
  // The cloud sends `sha256:<hex>`, but the journal must not produce an empty
  // field the day something hands it a bare digest: `lastIndexOf(":")` finding
  // nothing means "the whole string is the digest", not "there is none".
  const digest = "0123456789abcdef";

  assert.equal(shortHash(digest), "0123456789ab");
  assert.equal(shortHash(""), "");
});

test("only the last colon separates the algorithm from the digest", () => {
  assert.equal(shortHash("sha256:sha256:abcdef123456789"), "abcdef123456"); // pragma: allowlist secret
});

test("authorize is timed monotonically, so a stepped clock cannot go negative", () => {
  // A journal that reports a negative duration because NTP moved the clock is a
  // journal that gets distrusted for the one number it exists to report.
  const elapsed = start();
  let spin = 0;
  for (let i = 0; i < 200_000; i++) spin += i;
  assert.ok(spin > 0);

  const ms = elapsed();
  assert.ok(Number.isInteger(ms), "a duration is whole milliseconds in both SDKs");
  assert.ok(ms >= 0);
});
