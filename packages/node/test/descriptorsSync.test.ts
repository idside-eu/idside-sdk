import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { JS_CALL_SITE_PATHS, loadDescriptors } from "../src/descriptors.ts";

// The provider descriptors bundled in this package are copies. The single source
// of truth is packages/descriptors/; these copies must stay byte-identical to it,
// exactly like the Python SDK's descriptors sync test.
const here = dirname(fileURLToPath(import.meta.url));
const bundledDir = join(here, "..", "src", "providers");
const sourceDir = join(here, "..", "..", "descriptors", "descriptors");

const sourceNames = readdirSync(sourceDir)
  .filter((f) => f.endsWith(".json"))
  .sort();

test("bundles every descriptor from the source of truth (no more, no less)", () => {
  const bundled = readdirSync(bundledDir)
    .filter((f) => f.endsWith(".json"))
    .sort();
  assert.deepEqual(bundled, sourceNames);
});

for (const name of sourceNames) {
  test(`bundled descriptor is byte-identical to packages/descriptors: ${name}`, () => {
    const source = readFileSync(join(sourceDir, name));
    const bundled = readFileSync(join(bundledDir, name));
    assert.ok(bundled.equals(source), `${name} has drifted from the source of truth`);
  });

  test(`bundled descriptor carries the required fields: ${name}`, () => {
    const descriptor = JSON.parse(readFileSync(join(bundledDir, name), "utf8"));
    for (const field of ["descriptor_version", "name", "client_module", "call_sites"]) {
      assert.ok(field in descriptor, `${name} is missing required field '${field}'`);
    }
    assert.ok(
      Array.isArray(descriptor.call_sites) && descriptor.call_sites.length >= 1,
      `${name} must declare at least one call site`,
    );
  });
}

// ── P1.12: call sites are objects, and each says what may be forced ON IT ────

for (const name of sourceNames) {
  test(`every call site declares an id, a path and its own allowlist: ${name}`, () => {
    const descriptor = JSON.parse(readFileSync(join(bundledDir, name), "utf8"));
    const ids = new Set<string>();

    for (const site of descriptor.call_sites) {
      assert.equal(typeof site.id, "string", `${name}: a call site has no id`);
      assert.ok(Array.isArray(site.path) && site.path.length >= 1, `${name}:${site.id} has no path`);
      assert.equal(
        site.id,
        site.path.join("."),
        `${name}:${site.id} — the id IS the dotted Python path; that is what makes one endpoint one name across SDKs`,
      );
      assert.ok(
        Array.isArray(site.forceable_params),
        `${name}:${site.id} does not say what may be forced on it. An empty list is a valid answer; an absent one silently means "whatever the provider allows somewhere", which is how a parameter reaches an endpoint that rejects it`,
      );
      assert.ok(!ids.has(site.id), `${name} declares the call-site id '${site.id}' twice`);
      ids.add(site.id);
    }
  });
}

test("the JS path overrides all point at call sites that exist", () => {
  // An override keyed on an id no descriptor declares would silently leave that
  // endpoint on its Python path — which does not exist on the JS client — and
  // therefore unenforced. That is a bypass, not a typo.
  const byName = new Map(
    sourceNames.map((n) => [
      JSON.parse(readFileSync(join(sourceDir, n), "utf8")).name as string,
      JSON.parse(readFileSync(join(sourceDir, n), "utf8")),
    ]),
  );

  for (const [provider, overrides] of Object.entries(JS_CALL_SITE_PATHS)) {
    const descriptor = byName.get(provider);
    assert.ok(descriptor, `JS_CALL_SITE_PATHS overrides unknown provider '${provider}'`);
    const declared = new Set(descriptor.call_sites.map((s: { id: string }) => s.id));
    for (const id of Object.keys(overrides)) {
      assert.ok(declared.has(id), `JS_CALL_SITE_PATHS['${provider}'] overrides unknown call site '${id}'`);
    }
  }
});

test("the loaded gemini descriptor keeps the Python id on the JS path", () => {
  const gemini = loadDescriptors().get("gemini")!;
  const site = gemini.callSites.find((s) => s.id === "models.generate_content")!;
  assert.ok(site, "gemini lost its generate_content call site");
  assert.deepEqual([...site.path], ["models", "generateContent"]);
});
