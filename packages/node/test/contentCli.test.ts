import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { EventEmitter } from "node:events";
import { closeSync, existsSync, mkdtempSync, openSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { contentPolicyDigest, referencePolicy } from "../src/cleaning/index.ts";
import { isEntryPoint, main, run as runAsProgram } from "../src/cleaning/cli.ts";

// `idside-verify-clean`: the replay check as a command, as Python's.
const here = dirname(fileURLToPath(import.meta.url));
const CLI = join(here, "..", "src", "cleaning", "cli.ts");
const POLICY = referencePolicy();

function run(body: unknown, policy: unknown = POLICY, ...extra: string[]): [number, string, string] {
  const dir = mkdtempSync(join(tmpdir(), "idside-cli-"));
  try {
    writeFileSync(join(dir, "request.json"), JSON.stringify(body));
    writeFileSync(join(dir, "policy.json"), JSON.stringify(policy));
    let out = "";
    let err = "";
    const code = main([join(dir, "request.json"), "--policy", join(dir, "policy.json"), "--provider", "openai", ...extra], {
      out: (text) => (out += text),
      err: (text) => (err += text),
    });
    return [code, out, err];
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("a clean request exits zero", () => {
  const body = { model: "gpt-4o", messages: [{ role: "user", content: "Hi [EMAIL_1]" }] };
  const [code, out] = run(body, POLICY, "--digest", contentPolicyDigest(POLICY));
  assert.equal(code, 0);
  assert.match(out, /clean: nothing left/);
  assert.ok(out.includes(contentPolicyDigest(POLICY)));
});

test("a request with a value left exits one and never prints it", () => {
  const [code, out, err] = run({ messages: [{ role: "user", content: "marie@example.com" }] });
  assert.equal(code, 1);
  assert.match(out, /would still mask email: 1/);
  assert.ok(!(out + err).includes("marie"));
});

test("a request the policy blocks exits one", () => {
  const [code, out] = run({ messages: [{ content: "4111111111111111" }] }, { schema: 1, detectors: { credit_card: "block" } });
  assert.equal(code, 1);
  assert.match(out, /would block it \(credit_card: 1\)/);
});

test("a wrong digest exits two", () => {
  const [code, , err] = run({}, POLICY, "--digest", "sha256:" + "0".repeat(64));
  assert.equal(code, 2);
  assert.match(err, /not the policy the proof names/);
});

test("a policy that cannot be applied exits two", () => {
  const [code, , err] = run({}, { schema: 2 });
  assert.equal(code, 2);
  assert.match(err, /cannot be applied/);
});

test("unreadable input, bad arguments and an unknown provider exit two; --help exits zero", () => {
  let err = "";
  let out = "";
  const streams = { out: (text: string) => (out += text), err: (text: string) => (err += text) };
  assert.equal(main(["/nonexistent.json", "--policy", "/nonexistent.json", "--provider", "openai"], streams), 2);
  assert.match(err, /cannot read the input/);
  assert.equal(main(["--unknown"], streams), 2);
  assert.equal(main(["a.json", "--policy", "p.json", "--provider", "nope"], streams), 2);
  assert.match(err, /PROVIDER is one of: anthropic, deepseek, gemini, mistral, openai/);
  assert.equal(main(["--help"], streams), 0);
  assert.match(out, /^usage: idside-verify-clean/);
});

test("the entry point is recognised through a symbolic link, as npm installs it", () => {
  const dir = mkdtempSync(join(tmpdir(), "idside-cli-"));
  try {
    symlinkSync(CLI, join(dir, "idside-verify-clean"));
    assert.equal(isEntryPoint(pathToFileURL(CLI).href, join(dir, "idside-verify-clean")), true);
    assert.equal(isEntryPoint(pathToFileURL(CLI).href, join(dir, "missing")), false);
    assert.equal(isEntryPoint(pathToFileURL(CLI).href, undefined), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("runs as a program", () => {
  const result = spawnSync(process.execPath, [CLI, "--help"], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /^usage: idside-verify-clean/);
});

test("writes to the process's own streams by default", (t) => {
  const written: string[] = [];
  t.mock.method(process.stdout, "write", (text: string) => (written.push(text), true));
  t.mock.method(process.stderr, "write", (text: string) => (written.push(text), true));
  assert.equal(main(["--help"]), 0);
  assert.equal(main(["--unknown"]), 2);
  assert.match(written.join(""), /usage: idside-verify-clean[\s\S]*Unknown option/);
});

// ── what is not an answer is never "NOT CLEAN" ─────────────────────────────
// Exit 1 says the body would still be cleaned. An error the command did not
// foresee must not say that: it exits 2, as unreadable input does, and its
// message says nothing was checked.

function files(request: string, policy: string): [string, string, () => void] {
  const dir = mkdtempSync(join(tmpdir(), "idside-cli-"));
  writeFileSync(join(dir, "request.json"), request);
  writeFileSync(join(dir, "policy.json"), policy);
  return [join(dir, "request.json"), join(dir, "policy.json"), () => rmSync(dir, { recursive: true, force: true })];
}

test("an unexpected error exits two, not one", () => {
  const [request, policy, done] = files("{}", JSON.stringify(POLICY));
  try {
    let err = "";
    const code = main([request, "--policy", policy, "--provider", "openai"], {
      out: () => {
        throw new Error("broken pipe"); // an output that fails, as a closed pipe does
      },
      err: (text) => (err += text),
    });
    assert.equal(code, 2);
    assert.match(err, /^idside-verify-clean: unexpected error/);
    assert.match(err, /nothing was checked/);
  } finally {
    done();
  }
});

const bodyWith = (content: string): string =>
  JSON.stringify({ model: "gpt-4o", messages: [{ role: "user", content }] });

for (const content of ["nothing to mask", "mail marie@example.com"]) {
  // A real pipe whose reader is gone, and a full device: the answer never
  // arrives, so the command must not say 0 or 1. Left to Node, the failed
  // write is an unhandled `error` event and the process exits 1.
  test(`an answer written to a closed pipe exits two: ${content}`, () => {
    const [request, policy, done] = files(bodyWith(content), JSON.stringify(POLICY));
    try {
      const script = '"$0" "$@" 2>/dev/null | true; exit "${PIPESTATUS[0]}"';
      const args = [CLI, request, "--policy", policy, "--provider", "openai"];
      const result = spawnSync("bash", ["-c", script, process.execPath, ...args]);
      assert.equal(result.status, 2);
    } finally {
      done();
    }
  });
  test(`an answer written to a full device exits two: ${content}`, { skip: !existsSync("/dev/full") }, () => {
    const [request, policy, done] = files(bodyWith(content), JSON.stringify(POLICY));
    const full = openSync("/dev/full", "w");
    try {
      const result = spawnSync(process.execPath, [CLI, request, "--policy", policy, "--provider", "openai"], {
        stdio: ["ignore", full, full],
      });
      assert.equal(result.status, 2);
    } finally {
      closeSync(full);
      done();
    }
  });
}

test("run sets the exit code and turns a failed write into exit two", () => {
  const exit = process.exit;
  const saved = process.exitCode;
  const outputs = [new EventEmitter(), new EventEmitter()];
  const exited: (number | string | null | undefined)[] = [];
  try {
    runAsProgram(["--provider", "nowhere"], outputs); // a usage error: written to stderr, exit 2
    assert.equal(process.exitCode, 2);
    process.exit = ((code?: number | string | null) => {
      exited.push(code);
    }) as typeof process.exit;
    for (const output of outputs) output.emit("error", new Error("EPIPE"));
    assert.deepEqual(exited, [2, 2]);
  } finally {
    process.exit = exit;
    process.exitCode = saved;
  }
});

test("a policy with a lone surrogate exits two", () => {
  const [request, policy, done] = files("{}", '{"schema": 1, "detectors": {"email": "mask"}, "note": "\\ud800"}');
  try {
    let err = "";
    const code = main([request, "--policy", policy, "--provider", "openai"], {
      out: () => undefined,
      err: (text) => (err += text),
    });
    assert.equal(code, 2);
    assert.match(err, /the policy is not JSON/);
  } finally {
    done();
  }
});

test("an unexpected error exits two from the program too", () => {
  const [request, policy, done] = files("{}", '{"schema": 1, "note": "\\ud800"}');
  try {
    const result = spawnSync(process.execPath, [CLI, request, "--policy", policy, "--provider", "openai"], {
      encoding: "utf8",
    });
    assert.equal(result.status, 2, result.stderr);
    assert.match(result.stderr, /^idside-verify-clean: /);
  } finally {
    done();
  }
});

// The engine reads 128 levels: past them nothing is checked, whatever depth a
// JSON reader stops at (Python's stops early). Both commands answer 2.
for (const [depth, expected] of [
  [129, 0],
  [130, 2],
  [200_000, 2],
] as const) {
  test(`a request nested ${depth} deep exits ${expected}`, () => {
    const dir = mkdtempSync(join(tmpdir(), "idside-cli-"));
    try {
      writeFileSync(join(dir, "request.json"), "[".repeat(depth) + "]".repeat(depth));
      writeFileSync(join(dir, "policy.json"), JSON.stringify(POLICY));
      let err = "";
      const code = main([join(dir, "request.json"), "--policy", join(dir, "policy.json"), "--provider", "openai"], {
        out: () => {},
        err: (text) => (err += text),
      });
      assert.equal(code, expected);
      if (expected) assert.match(err, /nested deeper than 128 levels/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
}
