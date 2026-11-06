/**
 * The child half of `journalRendering.test.ts`. Not a test file: no `*.test.ts`
 * suffix, so the runner never picks it up on its own.
 *
 * Node reads `NODE_DEBUG` once, when the process boots, so the only way to
 * exercise the journal's rendering is from a process that had the variable from
 * the start. This is that process. It emits one event per line shape the
 * `scripts/check_sdk_parity.sh` diff depends on and lets the parent read them
 * back off stderr, which is where `util.debuglog` writes.
 */

import { event } from "../src/log.ts";

// Every shape `Fields` allows, in one event, in the caller's order. Field order
// is part of the contract: two runs of the same path have to produce comparable
// lines, and that is what makes the two SDKs diffable.
event("shapes", {
  yes: true,
  no: false,
  count: 3,
  forced: ["store", "background"],
  empty: [],
  spaced: "two words",
  absent: undefined,
  none: null,
});

// A name with nothing to say is a name, not a name with a trailing space.
event("bare", {});

// Every field dropped is the same case: the event still says it happened.
event("all_dropped", { a: undefined, b: null });
