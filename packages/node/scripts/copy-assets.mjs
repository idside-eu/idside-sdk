/**
 * Copy the bundled JSON data files from `src/` into `dist/`.
 *
 * `tsc` only emits the JSON files it can see through a static `import ... with
 * { type: "json" }` — that covers `providers/*.json` but not
 * `consent-options.json`, which `options.ts` resolves at runtime through
 * `createRequire`. A missing data file does not break a single test in this
 * repo: it breaks the consumer's first call, because in-repo the file is always
 * next to the source. So we mirror *every* JSON under `src/`, not a hand-written
 * list — adding a provider descriptor must never silently ship a broken package.
 */

import { cp, mkdir, readdir } from "node:fs/promises";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const srcDir = join(packageRoot, "src");
const distDir = join(packageRoot, "dist");

/** Every `.json` under `dir`, as paths relative to `srcDir`. */
async function jsonFiles(dir) {
  const found = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const absolute = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...(await jsonFiles(absolute)));
    else if (entry.name.endsWith(".json")) found.push(relative(srcDir, absolute));
  }
  return found;
}

const assets = await jsonFiles(srcDir);
if (assets.length === 0) throw new Error(`no JSON assets found under ${srcDir}`);

for (const asset of assets) {
  const destination = join(distDir, asset);
  await mkdir(dirname(destination), { recursive: true });
  await cp(join(srcDir, asset), destination);
}

console.log(`copied ${assets.length} JSON asset(s) into dist/: ${assets.sort().join(", ")}`);
