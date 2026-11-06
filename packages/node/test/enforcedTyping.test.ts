import { test } from "node:test";
import assert from "node:assert/strict";
import { loadDescriptors } from "../src/descriptors.ts";
import { CALL_SITE_TREE } from "../src/enforced.ts";

/**
 * The runtime half of the typed identity.
 *
 * `Enforced<T>` demands `user_id` or `ubt` on exactly the endpoints named in
 * `CALL_SITE_TREE`, which is a hand-written copy of the descriptors — types
 * cannot read JSON, so nothing but this file stands between the two. A
 * descriptor that gains a call site while the tree does not would leave that
 * endpoint enforced at run time and unenforced at compile time: the developer
 * would get a `MissingIdentityError` from a call TypeScript told them was fine.
 *
 * The type assertions themselves live in `test/types.assert.ts`, where `tsc`
 * runs them.
 */

/** Every endpoint the descriptors declare, `create` and its aliases alike. */
function declaredEndpoints(): Set<string> {
  const paths = new Set<string>();
  for (const descriptor of loadDescriptors().values()) {
    for (const site of descriptor.callSites) {
      paths.add(site.path.join("."));
      for (const alias of site.aliases) {
        paths.add([...site.path.slice(0, -1), alias].join("."));
      }
    }
  }
  return paths;
}

/** Every method the tree marks as carrying the identity. */
function treeLeaves(node: object, prefix: readonly string[] = []): Set<string> {
  const leaves = new Set<string>();
  for (const [key, value] of Object.entries(node)) {
    const path = [...prefix, key];
    if (typeof value === "object" && value !== null) {
      for (const leaf of treeLeaves(value, path)) leaves.add(leaf);
    } else {
      leaves.add(path.join("."));
    }
  }
  return leaves;
}

test("the typed call sites are exactly the ones the descriptors declare", () => {
  const declared = [...declaredEndpoints()].sort();
  const typed = [...treeLeaves(CALL_SITE_TREE)].sort();
  assert.deepEqual(
    typed,
    declared,
    "CALL_SITE_TREE has drifted from the descriptors: an endpoint enforced at " +
      "run time but not at compile time (or the reverse) is a MissingIdentityError " +
      "no compiler warned about",
  );
});

test("every descriptor names the identity the way the Identity type spells it", () => {
  // `Identity` is `{ user_id } | { ubt }`, written out in TypeScript because a
  // type cannot read `identity_param` out of a JSON file. A descriptor that
  // renamed either one would type-check a field the SDK no longer reads.
  for (const [name, descriptor] of loadDescriptors()) {
    assert.equal(descriptor.identityParam, "user_id", `${name}: identity_param`);
    assert.equal(descriptor.ubtParam, "ubt", `${name}: ubt_param`);
  }
});
