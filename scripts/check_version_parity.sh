#!/usr/bin/env bash
#
# The two packages carry ONE version, spelled twice.
#
# `idside` and `@idside/sdk` are released together, so a caller who reads a
# changelog entry and pins `0.2.0` in both languages must get the same code. The
# two ecosystems do not spell a pre-release the same way, though — PEP 440 says
# `0.1.0.dev0`, SemVer says `0.1.0-dev.0` — so nothing but a translation can
# compare them, and until this script existed nothing did. A release published
# from two manifests that had drifted cannot be corrected: neither registry lets
# a version number be reused.
#
# It also checks the npm dist-tag, which is the other mistake of this kind.
# `npm publish` moves `latest` regardless of what the version says, so a
# pre-release published without `--tag next` becomes what `npm install` serves
# everyone. `publishConfig.tag` is the durable half of that guard; this is the
# check that it matches the version actually in the manifest.
#
# Run:
#   ./scripts/check_version_parity.sh              # the two manifests agree
#   ./scripts/check_version_parity.sh 0.1.0.dev0   # …and they are this version
#
# The argument is the PEP 440 spelling (what `__about__.py` holds), because a
# release is decided once and typed once.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$ROOT" "${1-}" <<'PY'
import json
import pathlib
import re
import sys

root, expected = pathlib.Path(sys.argv[1]), sys.argv[2]

about = (root / "packages/python/src/idside/__about__.py").read_text(encoding="utf-8")
match = re.search(r'^__version__ = "([^"]+)"', about, re.M)
if match is None:
    sys.exit("❌ packages/python/src/idside/__about__.py has no __version__")
python_version = match.group(1)

manifest = json.loads((root / "packages/node/package.json").read_text(encoding="utf-8"))
node_version = manifest["version"]

# The grammar deliberately covers only what this project releases. A version
# outside it is a build failure rather than something to guess at: `0.1.0.post1`
# and `0.1.0.dev0+local` have no agreed SemVer spelling, and inventing one here
# is how the two packages come to disagree.
PEP440 = re.compile(r"^(?P<release>\d+\.\d+\.\d+)(?:(?P<kind>\.dev|a|b|rc)(?P<n>\d+))?$")
SPELLING = {".dev": "dev", "a": "alpha", "b": "beta", "rc": "rc"}

parsed = PEP440.match(python_version)
if parsed is None:
    sys.exit(
        f"❌ {python_version!r} is not a version this project knows how to "
        f"publish.\n   Supported: 0.1.0, 0.1.0.dev0, 0.1.0a1, 0.1.0b1, 0.1.0rc1."
    )

release, kind, number = parsed.group("release", "kind", "n")
prerelease = kind is not None
want_node = release if not prerelease else f"{release}-{SPELLING[kind]}.{number}"

if node_version != want_node:
    sys.exit(
        f"❌ the two packages disagree about which release this is:\n"
        f"     packages/python/src/idside/__about__.py  {python_version}\n"
        f"     packages/node/package.json               {node_version}\n"
        f"   For {python_version} on PyPI, npm must say {want_node}."
    )
print(f"  ✅ one version, spelled twice: {python_version} / {node_version}")

# ── the npm dist-tag has to match the kind of release ──────────────────────
tag = manifest.get("publishConfig", {}).get("tag")
if prerelease and tag != "next":
    sys.exit(
        f"❌ {node_version} is a pre-release, so packages/node/package.json must "
        f'set publishConfig.tag to "next" (it is {tag!r}).\n'
        f"   Without it `npm publish` moves the `latest` tag and every plain "
        f"`npm install @idside/sdk` serves a development build."
    )
if not prerelease and tag not in (None, "latest"):
    sys.exit(
        f"❌ {node_version} is a final release, so publishConfig.tag must be "
        f'absent or "latest" (it is {tag!r}).\n'
        f"   Leaving it at {tag!r} publishes the release where nobody installing "
        f"by default will find it, and `latest` stays on an older build."
    )
print(f"  ✅ npm dist-tag matches the release kind: {tag or 'latest (default)'}")

if expected and expected != python_version:
    sys.exit(
        f"❌ asked to check {expected!r}, but the manifests say {python_version!r}."
    )
if expected:
    print(f"  ✅ the manifests are the version that was asked for: {expected}")
PY
