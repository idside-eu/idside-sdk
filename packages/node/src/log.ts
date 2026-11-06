// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * The journal: enough to debug a call that did not go out, and nothing more.
 *
 * A fail-closed SDK with no journal is undebuggable at a customer site. When a
 * call does not leave the process, the developer needs to learn which call site
 * was recognised, which parameters a user's answer forced, how long the cloud
 * took, and whether it was reached at all. Every one of those is a fact about
 * *their own* request.
 *
 * Three rules shape everything here, and the first two are refusals.
 *
 * **Nothing secret is ever emitted.** Not the ID Side key, not the provider
 * key, not the end user's identifier in the clear, not a UBT, not a prompt, not
 * an ACT token, not a credential. Invariant #7 says a credential is a secret —
 * never logged, never in an error message — and the person on the other end
 * deserves the same treatment. What is emitted is either a fact about the
 * developer's own configuration (the provider, the call site, the model, the
 * posture *label*) or a value that is already a digest.
 *
 * **Silent by default**, through `util.debuglog`, which is Node's own answer to
 * this problem: nothing is written unless `NODE_DEBUG=idside` asks for it, and
 * it costs no dependency in a package that has none. Python reaches the same
 * place through `logging` and a `NullHandler`; each SDK uses the mechanism its
 * ecosystem already configures, rather than inventing a channel. What is
 * identical is the content — same events, same fields, same rendering — and
 * `scripts/check_sdk_parity.sh` runs both and diffs the lines. One event is
 * Python's alone: `resume`, for the overloaded helper that SDK patches and this
 * one does not (see `descriptors.ts` on `resuming_params`). A journal cannot
 * report a step this SDK never takes.
 *
 * **Observation is not influence** (invariant #8). Rendering happens on scalars
 * and freshly built arrays, never on the live `ConsentDecision` or the argument
 * object the SDK is about to send. There is no hook, no `onEvent` and no
 * logger to inject: a point where an application could watch *and* answer would
 * be the opaque tunnel through the back door.
 *
 * The net (`guard.ts`) uses none of this. It stays blind by design, and
 * observability is not a reason to give it a first thing to record.
 */

import { createHash, randomBytes } from "node:crypto";
import { debuglog, inspect } from "node:util";

const debug = debuglog("idside");

/**
 * Per-process salt for the subject pseudonym. Random, never persisted, never
 * emitted.
 *
 * A `user_id` is low-entropy on purpose — `usr_marie_456` is a dictionary away
 * — so an unsalted digest of one is not a pseudonym but an encoding: anyone
 * holding the log hashes their guesses and reads the names back. Salting per
 * process keeps the only property debugging actually needs, correlating one
 * user's calls *within one log*, and drops the one nobody asked for,
 * recognising that user across logs, machines or restarts.
 *
 * Exported for the test that asserts it never appears in the journal, and for
 * nothing else.
 */
export const SALT: Buffer = randomBytes(32);

/** Enough digest to tell users apart in a log, far too little to attack. */
const PSEUDONYM_CHARS = 12;

/**
 * The request hash is already a digest and is what the ledger indexes, so it is
 * the honest answer to "which proof was minted for this call?" — the ACT token
 * itself is bearer material and is never emitted. Truncated anyway: a full hash
 * of a short prompt is a dictionary attack away from the prompt.
 */
const HASH_CHARS = 12;

/** A stable, unforgeable stand-in for one end user, for this process only. */
export function pseudonym(value: unknown): string {
  return createHash("sha256").update(SALT).update(subjectText(value)).digest("hex").slice(0, PSEUDONYM_CHARS);
}

/**
 * The text a pseudonym is derived from. An identity reaches the cloud as the
 * caller wrote it, so a wrong one — an object, an array — is refused there, and
 * journalled first: `String()` gave every object one pseudonym, and threw on an
 * object with no prototype. An object is read as `inspect` shows it, without
 * running any of its own code; anything else keeps `String()`, under which 42
 * and "42" are one user, as the cloud reads them.
 */
function subjectText(value: unknown): string {
  return typeof value === "object" ? inspect(value, { customInspect: false }) : String(value);
}

/** The correlating half of a `sha256:<hex>` request hash. */
export function shortHash(requestHash: unknown): string {
  const text = String(requestHash);
  const colon = text.lastIndexOf(":");
  return (colon === -1 ? text : text.slice(colon + 1)).slice(0, HASH_CHARS);
}

/**
 * Milliseconds spent since `start()`, for the one hop worth measuring.
 *
 * Monotonic: a journal that reports a negative authorize because NTP stepped
 * the clock is a journal that gets distrusted for the one number it exists to
 * report.
 */
export function start(): () => number {
  const from = process.hrtime.bigint();
  return () => Number((process.hrtime.bigint() - from) / 1_000_000n);
}

/** One field of an event. `undefined` and `null` are dropped, never rendered. */
export type Fields = Record<string, string | number | boolean | readonly string[] | undefined | null>;

/**
 * Emit one event, as the same stable line Python emits.
 *
 * Field order is the caller's, and stable, so two runs of the same path produce
 * comparable lines — which is what lets the parity script diff this journal
 * against Python's. `null`/`undefined` are dropped rather than rendered: "the
 * cloud named no posture" is said by the absence of `posture=`, not by
 * `posture=null`.
 */
export function event(name: string, fields: Fields): void {
  if (!debug.enabled) return; // do not pay for formatting nobody will read
  const rendered = Object.entries(fields)
    .flatMap(([key, value]) => (value === undefined || value === null ? [] : [`${key}=${render(value)}`]))
    .join(" ");
  debug("%s", rendered ? `${name} ${rendered}` : name);
}

/**
 * One value, in a form that survives a grep and never spans a line.
 *
 * An array becomes `a+b` rather than `["a","b"]`: the parameters a consent
 * state forced are read far more often than they are parsed, and the sorted
 * join is identical in both SDKs, which is what the parity script compares.
 */
function render(value: string | number | boolean | readonly string[]): string {
  if (typeof value === "boolean") return value ? "true" : "false";
  if (Array.isArray(value)) {
    return value.map(String).sort().join("+") || "-";
  }
  return String(value).replace(/ /g, "_");
}
