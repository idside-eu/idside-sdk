#!/usr/bin/env bash
#
# Run every runnable example this repository advertises, and compare what it
# prints with what the document says it prints.
#
# Two families, one rule. The quickstarts' example is EXTRACTED from the
# Markdown and run, because a copy kept beside the document is a thing that
# drifts. The programs under examples/ are run where they live, and the command
# line comes from the `console` block that advertises it — so the block cannot
# describe a command nobody runs.
#
# This is the code with no test suite behind it and the first a developer meets,
# which is the worst possible combination. The quickstarts were already broken
# once each: adding `call_site` to the authorize contract made the Python one
# raise TypeError, and adding `constraints` left the Node one reading a property
# of `undefined`. Both suites were green throughout.
#
# Run: ./scripts/check_docs_examples.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass() { printf '  \033[32m✅\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m❌\033[0m %s\n' "$1"; exit 1; }

# Extract the fenced block containing NEEDLE, and the `console` block that
# follows it, from a quickstart.
extract() {
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import pathlib, re, sys

doc, lang, needle, out_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
text = pathlib.Path(doc).read_text(encoding="utf-8")

blocks = re.findall(rf"```{lang}\n(.*?)```", text, re.S)
code = [b for b in blocks if needle in b]
if len(code) != 1:
    sys.exit(f"{doc}: expected exactly one {lang} block containing {needle!r}, found {len(code)}")

console = re.findall(r"```console\n(.*?)```", text, re.S)
expected = [c for c in console if "[cloud] authorize" in c]
if len(expected) != 1:
    sys.exit(f"{doc}: expected exactly one console block showing the run, found {len(expected)}")

out = pathlib.Path(out_dir)
(out / "code").write_text(code[0], encoding="utf-8")
# Drop the `$ command` line: the shell prompt is not output.
(out / "expected").write_text(
    "".join(line for line in expected[0].splitlines(keepends=True) if not line.startswith("$ ")),
    encoding="utf-8",
)
PY
}

compare() {
  if diff -u "$1/expected" "$1/actual" >"$1/diff" 2>&1; then
    pass "$2"
  else
    cat "$1/diff"
    fail "$2 — the document and the program disagree"
  fi
}

echo
echo "Python quickstart:"
mkdir -p "$WORK/py"
extract "$ROOT/docs/quickstart-python.md" python "class FakeCloud" "$WORK/py"
# Run from packages/python so `openai` and `idside` resolve, but keep the file
# outside the tree so nothing about the repo layout leaks into the example.
# stdout is what the document shows; stderr is the runtime talking about itself
# — a virtualenv being created, a deprecation — and belongs in neither.
(cd "$ROOT/packages/python" && uv run python "$WORK/py/code") \
  >"$WORK/py/actual" 2>"$WORK/py/stderr" ||
  { cat "$WORK/py/stderr" "$WORK/py/actual"; fail "the Python example did not run"; }
pass "runs verbatim"
compare "$WORK/py" "prints exactly what the document says"

echo
echo "Node quickstart:"
mkdir -p "$WORK/node"
extract "$ROOT/docs/quickstart-node.md" js "fakeCloud" "$WORK/node"
# The example imports "@idside/sdk"; point it at the local sources, and run it
# from packages/node so `openai` resolves.
sed 's#"@idside/sdk"#"'"$ROOT"'/packages/node/src/index.ts"#' \
  "$WORK/node/code" >"$ROOT/packages/node/.docs-example.scratch.mjs"
trap 'rm -rf "$WORK" "$ROOT/packages/node/.docs-example.scratch.mjs"' EXIT
# Same split, and here it is load-bearing rather than tidy: on the
# type-stripping floor this job pins, Node prints an ExperimentalWarning to
# stderr, and merging the two streams compared that warning against the document
# — which will never mention it. The `example()` runner below already had this
# right; the quickstarts did not.
(cd "$ROOT/packages/node" && node --experimental-strip-types ./.docs-example.scratch.mjs) \
  >"$WORK/node/actual" 2>"$WORK/node/stderr" ||
  { cat "$WORK/node/stderr" "$WORK/node/actual"; fail "the Node example did not run"; }
pass "runs verbatim"
compare "$WORK/node" "prints exactly what the document says"

# ── examples/ ───────────────────────────────────────────────────────────────
#
# The programs there run where they live, so what CI runs is exactly what the
# README tells a developer to type. The command is this script's, never the
# document's — the document is only searched for the block that advertises it.

# Print the command line of every `console` block in examples/README.md, so a
# block describing an example nobody runs fails the build.
advertised() {
  python3 - "$ROOT/examples/README.md" <<'INNER'
import pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
for block in re.findall(r"```console\n(.*?)```", text, re.S):
    first = block.splitlines()[0]
    if first.startswith("$ "):
        print(first[2:])
INNER
}

# Extract the output a `console` block claims for one command.
expected_output() {
  python3 - "$ROOT/examples/README.md" "$1" "$2" <<'INNER'
import pathlib, re, sys

doc, wanted, out = sys.argv[1], sys.argv[2], sys.argv[3]
text = pathlib.Path(doc).read_text(encoding="utf-8")
blocks = [
    b
    for b in re.findall(r"```console\n(.*?)```", text, re.S)
    if b.splitlines()[0] == f"$ {wanted}"
]
if len(blocks) != 1:
    sys.exit(
        f"examples/README.md: expected exactly one console block for "
        f"{wanted!r}, found {len(blocks)}"
    )
pathlib.Path(out).write_text(
    "".join(blocks[0].splitlines(keepends=True)[1:]), encoding="utf-8"
)
INNER
}

ran=""
example() { # $1 = directory under the repo root, $2 = command line, verbatim
  local dir="$1" cmd="$2" out
  out="$WORK/$(printf '%s' "$dir/$cmd" | tr -c '[:alnum:]' '-')"
  mkdir -p "$out"
  expected_output "$cmd" "$out/expected"
  # stdout is what the document shows; stderr is a virtualenv being created or
  # a Node experimental warning, and belongs in neither the diff nor the noise.
  if ! (cd "$ROOT/$dir" && eval "$cmd") >"$out/actual" 2>"$out/stderr"; then
    cat "$out/stderr" "$out/actual"
    fail "$cmd did not run"
  fi
  pass "runs: $cmd"
  compare "$out" "prints exactly what examples/README.md says: $cmd"
  ran="$ran$cmd"$'\n'
}

echo
echo "examples/ — Python:"
example examples/python "uv run wrapped_call.py"
example examples/python "uv run async_wrapped_call.py"

echo
echo "examples/ — Node.js:"
# The example imports the BUILT package, so a stale dist/ would have it
# exercising yesterday's SDK. Rebuilt every run for that reason, not tidiness.
npm --prefix "$ROOT/packages/node" run build >/dev/null
[ -d "$ROOT/examples/node/node_modules" ] ||
  npm --prefix "$ROOT/examples/node" ci --prefer-offline >/dev/null
npm --prefix "$ROOT/examples/node" run typecheck >/dev/null ||
  fail "examples/node does not compile under strict TypeScript"
pass "compiles under strict TypeScript, with no cast"
example examples/node "node wrapped-call.ts"

echo
if diff -u <(advertised | sort) <(printf '%s' "$ran" | sed '/^$/d' | sort) >"$WORK/coverage"; then
  pass "every command examples/README.md advertises is one this script runs"
else
  cat "$WORK/coverage"
  fail "examples/README.md advertises a command this script does not run"
fi

echo
echo "Both quickstarts and every example still do what they claim."
