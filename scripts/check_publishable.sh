#!/usr/bin/env bash
# Build both SDKs the way a release does, then install and exercise them from
# OUTSIDE the repository.
#
# Every packaging bug this catches is invisible in-tree: the tests import from
# `src/`, where the data files are always next to the code and the license is
# always two directories up. A consumer gets a tarball. If a descriptor did not
# make it in, the failure is not a red test — it is the developer's first AI
# call, in their app. So the only check worth running is the real one: pack,
# install elsewhere, import by package name, call the code paths that read the
# bundled data. Run from the repository root.
set -euo pipefail

# The virtual patent marking (35 U.S.C. §287(a)). check_licence_claims.sh
# requires it at the top of every source file; this script checks that it is
# still there in what a consumer actually installs.
MARKING="Patents: https://sdk.idside.eu/patents"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass() { echo "  ✅ $1"; }
fail() { echo "  ❌ $1"; exit 1; }

# ── One release, two manifests ─────────────────────────────────────────────
# Before anything is built: the two packages must agree which version this is,
# and the npm dist-tag must match the kind of release it is. Both mistakes are
# uncorrectable after an upload — neither registry lets a version number be
# reused — so they are checked first, where a failure costs nothing.
echo "One version, spelled for both ecosystems:"
"$ROOT/scripts/check_version_parity.sh"

echo
# ── The licence says what the repository means ─────────────────────────────
# Second, and for the same reason as the version above: uncorrectable after the
# upload. A package reaches its first recipient under whatever LICENSE said at
# that moment, and no later change reaches them. This is the one path a human
# walks on the day of a release, so it is the one path that must not be able to
# skip the check — RELEASING.md says "check the licence before the first upload,
# never after", and until this line existed, nothing here did.
echo "The licence says what the repository means:"
"$ROOT/scripts/check_licence_claims.sh"

echo
# ── The license must travel with the package ───────────────────────────────
# The licence is source-available, so a package that arrives without it arrives
# with no terms at all — and a recipient who found none could reasonably read
# the silence as permission. The NOTICE beside it identifies the patents and
# says that, beyond the limited evaluation patent licence of clause 2 of
# LICENSE, rights under them are conveyed only by an executed commercial
# licence — the notice clauses 4(a) and 4(g) of LICENSE forbid anyone to drop.
#
# Neither npm nor hatchling will reach outside a package directory to find one,
# so each publishable package keeps a copy of the repo-root LICENSE/NOTICE, and
# those copies must not drift. The licence check above has already proved they
# have not — it would have stopped this script — so what is left to prove here
# is that each tarball actually carries its copy, below.

# ── Node ───────────────────────────────────────────────────────────────────
# Node refuses to strip types under node_modules, so publishing raw `.ts` would
# throw ERR_UNSUPPORTED_NODE_MODULES_TYPE_STRIPPING on the consumer's first
# import. What ships is compiled JS + declarations; this proves it resolves.
echo
echo "Node — npm pack, then import @idside/sdk from outside the repo:"
cd "$ROOT/packages/node"
# `prepack` runs the build, so this exercises the real release path. Find the
# tarball on disk rather than parsing stdout, which also carries build output.
npm pack --pack-destination "$WORK" --silent >/dev/null
TARBALL="$(find "$WORK" -maxdepth 1 -name '*.tgz' -print -quit)"
[ -n "$TARBALL" ] || fail "npm pack produced no tarball"
pass "packed $(basename "$TARBALL")"

# The JSON data files are resolved at runtime; a tarball without them installs
# cleanly and fails on first use.
# (List once into a variable: `… | grep -q` under `pipefail` reports failure even
# on a match, because grep exits early and the producer dies on SIGPIPE.)
TARBALL_FILES="$(tar -tzf "$TARBALL")"
# Derived from the descriptors on disk, never listed by hand: a hardcoded list
# would go stale the first time a provider is added, and the tarball would ship
# without its descriptor while this check kept saying yes.
NODE_ASSETS="dist/consent-options.json dist/content-rules.json dist/cleaning/cli.js dist/index.js dist/index.d.ts LICENSE NOTICE"
for descriptor in "$ROOT"/packages/descriptors/descriptors/*.json; do
  NODE_ASSETS="$NODE_ASSETS dist/providers/$(basename "$descriptor")"
done

for asset in $NODE_ASSETS; do
  if grep -qx "package/$asset" <<<"$TARBALL_FILES"; then
    pass "tarball contains $asset"
  else
    fail "tarball is MISSING $asset"
  fi
done

# The marking has to survive the compiler, not just sit in the sources: what a
# consumer installs is `dist/`, and a build that dropped leading comments would
# ship every module unmarked while the source tree looked right. Read from the
# tarball itself, in Python rather than through a `tar | head` pipeline, which
# under `pipefail` fails on SIGPIPE exactly as `… | grep -q` does.
if NODE_MARKING="$(python3 - "$TARBALL" "$MARKING" <<'PY'
import sys
import tarfile

tarball, marking = sys.argv[1], sys.argv[2]
with tarfile.open(tarball, "r:gz") as archive:
    shipped = sorted(
        member.name
        for member in archive.getmembers()
        if member.isfile()
        and member.name.startswith("package/dist/")
        and member.name.endswith(".js")
    )
    if not shipped:
        sys.exit("the tarball has no package/dist/*.js file at all")
    unmarked = [
        name
        for name in shipped
        if not any(
            marking in line
            for line in archive.extractfile(name).read().decode("utf-8").splitlines()[:3]
        )
    ]
if unmarked:
    sys.exit("not in the first three lines of " + ", ".join(unmarked))
print(len(shipped))
PY
)"; then
  pass "all $NODE_MARKING compiled .js files in the tarball open with the patent marking"
else
  fail "the patent marking did not survive the build"
fi

mkdir -p "$WORK/node-consumer"
cd "$WORK/node-consumer"
echo '{"name":"c","version":"1.0.0","type":"module","private":true}' > package.json
npm install --silent --no-audit --no-fund "$TARBALL"

cat > smoke.mjs <<'SMOKE'
import { wrap, loadDescriptors, catalogue, decidableOptions, disclosures,
         verifyAct, requestHash, HttpConsentClient, MissingIdentityError,
         ConflictingIdentityError, ActVerificationError, verifyClean,
         contentPolicyDigest } from "@idside/sdk";

const assert = (cond, msg) => { if (!cond) { console.error("FAIL:", msg); process.exit(1); } };

// Reads dist/providers/*.json (static JSON import).
assert([...loadDescriptors().keys()].length >= 4, "descriptors did not load");
// Reads dist/consent-options.json (createRequire, at runtime).
assert(Object.keys(catalogue()).length > 0, "consent catalogue did not load");
assert(decidableOptions(["openai", "anthropic"]).length > 0, "no decidable options");
assert(disclosures(["openai", "gemini"]).length > 0, "no disclosures");
assert((await requestHash({ model: "gpt-5", messages: [] })).startsWith("sha256:"), "bad request hash");
// Reads dist/content-rules.json at load: without it, cleaning has no catalogue.
const cleaningPolicy = { schema: 1, detectors: { email: "mask" } };
assert(!verifyClean({ messages: [{ content: "marie@example.com" }] }, cleaningPolicy, { provider: "openai" }),
       "the cleaning engine found nothing to mask");
assert(verifyClean({ messages: [{ content: "[EMAIL_1]" }] }, cleaningPolicy, { provider: "openai" }),
       "the cleaning engine changed a cleaned body");
assert(contentPolicyDigest(cleaningPolicy).startsWith("sha256:"), "bad policy digest");

// Verification is reachable and fails closed.
await verifyAct("not.a.token", { jwks: { keys: [] } }).then(
  () => { console.error("FAIL: verifyAct accepted garbage"); process.exit(1); },
  (e) => assert(e instanceof ActVerificationError, "wrong error for garbage token"));

// Naming nobody is allowed: the call is authorized with no identity field at
// all, and the answer names the API key as its subject. The cloud is a stub
// behind the package's own transport, so nothing leaves the machine.
const bodies = [];
const answer = { decision: "allow", consent_state: {}, params_forced: {}, mode: "test",
  act_token: "act.jwt.token", token_id: "act_01k4f9h2m3", expires_at: "2026-09-12T12:00:00Z",
  source: "default_policy", legal_basis: "legitimate_interest", binding: "api_key" };
const cloud = new HttpConsentClient("https://cloud.smoke.test", {
  apiKey: "sk-test-not-a-real-key",
  fetch: async (url, init) => {
    if (new URL(url).pathname === "/v1/content-policy") {
      return new Response(JSON.stringify({ enabled: false }), { headers: { "content-type": "application/json" } });
    }
    bodies.push(JSON.parse(init.body));
    return new Response(JSON.stringify(answer), { headers: { "content-type": "application/json" } });
  },
});
const provider = [];
const client = wrap({ chat: { completions: { create: async (body) => (provider.push(body), {}) } } },
                    { provider: "openai", consentClient: cloud });
await client.chat.completions.create({ model: "gpt-5", messages: [] });
assert(bodies.length === 1 && !("user_id" in bodies[0]) && !("ubt" in bodies[0]),
       "a call naming nobody must send no identity field");
assert(provider.length === 1, "a call naming nobody must reach the provider");

// An identity that names nobody, or two, is refused before any network call or
// provider call.
await client.chat.completions.create({ model: "gpt-5", messages: [], user_id: "" }).then(
  () => { console.error("FAIL: call with an empty identity was allowed"); process.exit(1); },
  (e) => assert(e instanceof MissingIdentityError, "wrong error with an empty identity"));
await client.chat.completions.create({ model: "gpt-5", messages: [], user_id: "usr_marie_456", ubt: "ubt_demo" }).then(
  () => { console.error("FAIL: call with two identities was allowed"); process.exit(1); },
  (e) => assert(e instanceof ConflictingIdentityError, "wrong error with two identities"));
assert(bodies.length === 1 && provider.length === 1, "a refused identity reached the cloud or the provider");

// A signed cleaning policy, verified and applied by the installed package: the
// path that reads dist/cleaning/*.js and dist/providers/*.json for the build
// digest. The key lives only in this process's memory.
import { generateKeyPairSync, sign } from "node:crypto";
const { privateKey, publicKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
const b64 = (data) => Buffer.from(data).toString("base64url");
const signedPolicy = { schema: 1, app_id: "app_medhelp", revision: 1, detectors: { email: "mask" }, attachments: "allow", custom: [] };
const signedAt = Math.floor(Date.now() / 1000);
const jwsInput = b64(JSON.stringify({ alg: "ES256", typ: "content-policy+jwt", kid: "content-policy-g1" })) + "." +
  b64(JSON.stringify({ iss: "idside.eu", app_id: "app_medhelp", iat: signedAt - 10, exp: signedAt + 890,
                       digest: contentPolicyDigest(signedPolicy), policy: signedPolicy }));
const jws = jwsInput + "." + b64(sign("sha256", Buffer.from(jwsInput), { key: privateKey, dsaEncoding: "ieee-p1363" }));
const cleanedBodies = [];
const cleaningCloud = new HttpConsentClient("https://cloud.smoke.test", {
  apiKey: "sk-test-not-a-real-key",
  fetch: async (url, init) => {
    const reply = (value) => new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } });
    const path = new URL(url).pathname;
    if (path === "/v1/content-policy") {
      return reply({ enabled: true, policy: jws, challenge: { token: "chl_smoke", inputs: ["marie@example.com"] }, refresh_after: 300 });
    }
    if (path === "/.well-known/jwks.json") {
      return reply({ keys: [{ ...publicKey.export({ format: "jwk" }), kid: "content-policy-g1", use: "sig", alg: "ES256" }] });
    }
    cleanedBodies.push(JSON.parse(init.body));
    return reply(answer);
  },
});
const cleanedProvider = [];
const cleaningClient = wrap({ chat: { completions: { create: async (body) => (cleanedProvider.push(body), {}) } } },
                            { provider: "openai", consentClient: cleaningCloud });
await cleaningClient.chat.completions.create({ model: "gpt-5", messages: [{ role: "user", content: "Write to marie@example.com" }] });
assert(cleanedProvider.length === 1 && cleanedProvider[0].messages[0].content === "Write to [EMAIL_1]",
       "the installed package did not clean the call under a signed policy");
const declared = cleanedBodies[0].content_policy;
assert(declared && declared.counts.email === 1 && /^sha256:[0-9a-f]{64}$/.test(declared.build),
       "the authorize request did not declare the cleaning applied, with the build digest");
SMOKE

node smoke.mjs && pass "ESM consumer: descriptors, catalogue, verifyAct, cleaning under a signed policy, optional and fail-closed identity"

# The replay command a consumer gets on PATH with the package.
echo '{"messages": [{"content": "[EMAIL_1]"}]}' > request.json
echo '{"schema": 1, "detectors": {"email": "mask"}}' > policy.json
if npx --no-install idside-verify-clean request.json --policy policy.json --provider openai >/dev/null; then
  pass "npx idside-verify-clean runs from the installed package"
else
  fail "idside-verify-clean did not run from the installed package"
fi

# The package is ESM-only (options.ts uses import.meta.url), so CJS consumers
# depend on Node's require(esm). Prove it rather than assume it.
echo 'const s = require("@idside/sdk"); if (typeof s.wrap !== "function" || s.loadDescriptors().size < 4) process.exit(1);' > smoke.cjs
node smoke.cjs 2>/dev/null && pass "CJS consumer: require(\"@idside/sdk\") resolves" \
  || fail "CJS require() of the ESM build failed"

# ── Python ─────────────────────────────────────────────────────────────────
echo
echo "Python — uv build, then install the wheel outside the repo:"
cd "$ROOT/packages/python"
rm -rf dist
uv build --quiet
WHEEL="$(ls dist/*.whl)"
SDIST="$(ls dist/*.tar.gz)"
pass "built $(basename "$WHEEL")"

WHEEL_FILES="$(python3 -m zipfile -l "$WHEEL")"
PY_ASSETS="idside/consent-options.json idside/content-rules.json idside/py.typed \
           dist-info/licenses/LICENSE dist-info/licenses/NOTICE"
for descriptor in "$ROOT"/packages/descriptors/descriptors/*.json; do
  PY_ASSETS="$PY_ASSETS idside/providers/$(basename "$descriptor")"
done

for asset in $PY_ASSETS; do
  if grep -q "$asset" <<<"$WHEEL_FILES"; then
    pass "wheel contains $asset"
  else
    fail "wheel is MISSING $asset"
  fi
done

# The same marking in the wheel: at the top of every module a consumer imports,
# and in the METADATA that PyPI renders as the project's links.
if PY_MARKING="$(python3 - "$WHEEL" "$MARKING" <<'PY'
import sys
import zipfile

wheel, marking = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    modules = sorted(
        name for name in names if name.startswith("idside/") and name.endswith(".py")
    )
    if not modules:
        sys.exit("the wheel has no idside/*.py module at all")
    unmarked = [
        name
        for name in modules
        if not any(
            marking in line
            for line in archive.read(name).decode("utf-8").splitlines()[:3]
        )
    ]
    metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
    if len(metadata) != 1:
        sys.exit(f"expected one METADATA file, found {metadata}")
    project_urls = archive.read(metadata[0]).decode("utf-8").splitlines()
problems = []
if unmarked:
    problems.append("not in the first three lines of " + ", ".join(unmarked))
if "Project-URL: Patents, https://sdk.idside.eu/patents" not in project_urls:
    problems.append("METADATA has no 'Project-URL: Patents, https://sdk.idside.eu/patents'")
if problems:
    sys.exit("; ".join(problems))
print(len(modules))
PY
)"; then
  pass "all $PY_MARKING modules in the wheel open with the patent marking, and METADATA links the page"
else
  fail "the patent marking is missing from the wheel"
fi

# A `license-files` entry outside the project directory produces an sdist whose
# members escape the archive root; pip and GNU tar both refuse to unpack it.
if grep -q '\.\.' <<<"$(tar -tzf "$SDIST")"; then
  fail "sdist contains paths escaping its root (check license-files in pyproject.toml)"
else
  pass "sdist has no path-escaping members"
fi

python3 -m venv "$WORK/py-consumer" >/dev/null
"$WORK/py-consumer/bin/pip" install --quiet "${WHEEL}[verify]"
cd "$WORK"
"$WORK/py-consumer/bin/python" - <<'SMOKE'
import idside
from idside import consent
from idside.providers import load_descriptors
from idside.exceptions import ActVerificationError, ActVerificationUnavailableError

assert callable(consent.wrap), "consent.wrap missing"
assert len(load_descriptors()) >= 4, "descriptors did not load"
assert idside.catalogue(), "consent catalogue did not load"
assert idside.decidable_options(["openai", "anthropic"]), "no decidable options"
assert idside.disclosures(["openai", "gemini"]), "no disclosures"
assert idside.request_hash({"model": "gpt-5", "messages": []}).startswith("sha256:"), "bad request hash"
# Reads idside/content-rules.json at import: without it, cleaning has no catalogue.
_policy = {"schema": 1, "detectors": {"email": "mask"}}
assert not idside.verify_clean({"messages": [{"content": "marie@example.com"}]}, _policy, provider="openai")
assert idside.verify_clean({"messages": [{"content": "[EMAIL_1]"}]}, _policy, provider="openai")
assert idside.content_policy_digest(_policy).startswith("sha256:")
# What help(idside) shows an installed consumer.
assert "Patents: https://sdk.idside.eu/patents" in (idside.__doc__ or ""), "no patent marking"

# The [verify] extra must pull in cryptography: without it verify_act raises
# ActVerificationUnavailableError, which would make the extra a no-op.
try:
    idside.verify_act("not.a.token", jwks={"keys": []})
except ActVerificationUnavailableError as exc:  # pragma: no cover
    raise SystemExit(f"[verify] extra did not install cryptography: {exc}")
except ActVerificationError:
    pass
else:  # pragma: no cover
    raise SystemExit("verify_act accepted a garbage token")

# Naming nobody is allowed: the call is authorized with no identity field at
# all. An identity that names nobody, or two, is refused before any I/O. The
# cloud is a stub behind the package's own transport: nothing leaves the machine.
import json
from types import SimpleNamespace

import httpx
from idside.adapters import ProviderAdapter
from idside.exceptions import ConflictingIdentityError, MissingIdentityError

bodies, provider = [], []
answer = {"decision": "allow", "consent_state": {}, "params_forced": {}, "mode": "test",
          "act_token": "act.jwt.token", "token_id": "act_01k4f9h2m3",
          "expires_at": "2026-09-12T12:00:00Z", "source": "default_policy",
          "legal_basis": "legitimate_interest", "binding": "api_key"}

def cloud(request):
    if request.url.path == "/v1/content-policy":
        return httpx.Response(200, json={"enabled": False})  # cleans nothing
    bodies.append(json.loads(request.content))
    return httpx.Response(200, json=answer)

class Completions:
    def create(self, **kwargs):
        provider.append(kwargs)
        return {}

client = consent.wrap(
    SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
    adapter=ProviderAdapter(load_descriptors()["openai"]),
    consent_client=idside.HttpConsentClient(
        "https://cloud.smoke.test", transport=httpx.MockTransport(cloud)
    ),
)
client.chat.completions.create(model="gpt-5", messages=[])
assert len(bodies) == 1 and "user_id" not in bodies[0] and "ubt" not in bodies[0], bodies
assert len(provider) == 1, "a call naming nobody must reach the provider"
for identity, error in (({"user_id": ""}, MissingIdentityError),
                        ({"user_id": "usr_marie_456", "ubt": "ubt_demo"}, ConflictingIdentityError)):
    try:
        client.chat.completions.create(model="gpt-5", messages=[], **identity)
    except error:
        pass
    else:  # pragma: no cover
        raise SystemExit(f"{identity} was not refused with {error.__name__}")
assert len(bodies) == 1 and len(provider) == 1, "a refused identity reached the cloud or the provider"
SMOKE
pass "wheel consumer: descriptors, catalogue, verify_act via [verify] extra, cleaning, optional and fail-closed identity"

echo '{"messages": [{"content": "[EMAIL_1]"}]}' > "$WORK/request.json"
echo '{"schema": 1, "detectors": {"email": "mask"}}' > "$WORK/policy.json"
if "$WORK/py-consumer/bin/idside-verify-clean" "$WORK/request.json" --policy "$WORK/policy.json" --provider openai >/dev/null; then
  pass "idside-verify-clean is installed with the wheel and runs"
else
  fail "idside-verify-clean did not run from the installed wheel"
fi

# A bare install must verify a signed cleaning policy too: `pip install idside`
# has no `cryptography` (the [verify] extra brings it), and a policy that could
# not be checked would stop every call. The policy is signed by the consumer
# environment above, which has the extra, with a key that lives only in that
# process's memory: the token and the public key set are all that is written.
"$WORK/py-consumer/bin/python" - "$WORK/signed-policy.json" <<'SIGN'
import base64, json, sys, time
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from idside import content_policy_digest

b64 = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=").decode()
key = ec.generate_private_key(ec.SECP256R1())
numbers = key.public_key().public_numbers()
policy = {"schema": 1, "app_id": "app_medhelp", "revision": 1, "detectors": {"email": "mask"},
          "attachments": "allow", "custom": []}
now = int(time.time())
claims = {"iss": "idside.eu", "app_id": "app_medhelp", "iat": now - 10, "exp": now + 890,
          "digest": content_policy_digest(policy), "policy": policy}
signing_input = (b64(json.dumps({"alg": "ES256", "typ": "content-policy+jwt", "kid": "content-policy-g1"}).encode())
                 + "." + b64(json.dumps(claims).encode()))
r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
jwk = {"kty": "EC", "crv": "P-256", "kid": "content-policy-g1", "use": "sig", "alg": "ES256",
       "x": b64(numbers.x.to_bytes(32, "big")), "y": b64(numbers.y.to_bytes(32, "big"))}
with open(sys.argv[1], "w") as out:
    json.dump({"token": signing_input + "." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big")),
               "jwks": {"keys": [jwk]}}, out)
SIGN
python3 -m venv "$WORK/py-bare" >/dev/null
"$WORK/py-bare/bin/pip" install --quiet "$ROOT/packages/python/$WHEEL"
"$WORK/py-bare/bin/python" - "$WORK/signed-policy.json" <<'SMOKE'
import importlib.util, json, sys
from types import SimpleNamespace

import httpx
import idside
from idside import _p256, consent
from idside.adapters import ProviderAdapter
from idside.exceptions import ContentPolicyError
from idside.providers import load_descriptors

assert importlib.util.find_spec("cryptography") is None, "this environment was meant to have no cryptography"
signed = json.load(open(sys.argv[1]))
verified = []
real_verify = _p256.verify
_p256.verify = lambda *args: verified.append(1) or real_verify(*args)

def run(token):
    bodies, provider = [], []
    def cloud(request):
        if request.url.path == "/v1/content-policy":
            return httpx.Response(200, json={"enabled": True, "policy": token, "refresh_after": 300,
                                             "challenge": {"token": "chl_smoke", "inputs": ["marie@example.com"]}})
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json=signed["jwks"])
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"decision": "allow", "consent_state": {}, "params_forced": {},
            "mode": "test", "act_token": "act.jwt.token", "token_id": "act_01k4f9h2m3",
            "expires_at": "2026-09-12T12:00:00Z", "source": "default_policy",
            "legal_basis": "legitimate_interest", "binding": "api_key"})
    class Completions:
        def create(self, **kwargs):
            provider.append(kwargs)
            return {}
    client = consent.wrap(SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
                          adapter=ProviderAdapter(load_descriptors()["openai"]),
                          consent_client=idside.HttpConsentClient("https://cloud.smoke.test",
                                                                  transport=httpx.MockTransport(cloud)))
    client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": "Write to marie@example.com"}])
    return bodies, provider

bodies, provider = run(signed["token"])
assert provider[0]["messages"][0]["content"] == "Write to [EMAIL_1]", provider
assert bodies[0]["content_policy"]["counts"] == {"email": 1}, bodies
assert verified, "the signature was not checked by the built-in verifier"
import base64
head, payload, signature = signed["token"].split(".")
raw = bytearray(base64.urlsafe_b64decode(signature + "=="))
raw[-1] ^= 1
forged = f"{head}.{payload}.{base64.urlsafe_b64encode(bytes(raw)).rstrip(b'=').decode()}"
try:
    run(forged)
except ContentPolicyError as error:
    assert "signature" in str(error), error
else:  # pragma: no cover
    raise SystemExit("a forged policy signature was accepted by a bare install")
SMOKE
pass "bare wheel (no [verify] extra, no cryptography): a signed policy is verified, applied, and a forgery refused"

# The sdist must build and install on its own — PyPI serves it to anyone using
# --no-binary, and a wheel is rebuilt from it.
python3 -m venv "$WORK/py-sdist" >/dev/null
"$WORK/py-sdist/bin/pip" install --quiet --no-binary :all: "$ROOT/packages/python/$SDIST"
"$WORK/py-sdist/bin/python" -c "import idside; from idside import consent; assert callable(consent.wrap)"
pass "sdist installs and imports"

echo
echo "Both packages are publishable."
