# Releasing

How `idside` and `@idside/sdk` get published, in what order, and what happens
when a release goes wrong. What a version number *promises* is
[`docs/versioning.md`](./docs/versioning.md); this file is the mechanics.

> **The first pre-release is `0.1.0.dev0` / `0.1.0-dev.0`.** §0 is the one-time
> registry setup a first release relies on, and §6 is the checklist that goes
> before it; both are kept as the record.

Read §7 before the **first** publish. Everything in it is a decision that cannot
be revisited afterwards, and most of it costs nothing to get right today.

---

## 0. One-time registry setup

The npm scope `@idside` belongs to the `idside` organisation on npm. On PyPI, the
project `idside` is created by its first upload, through the trusted publisher
described in §0.2.

The two registries work differently here, and the difference is the whole reason
this section exists.

### 0.1 npm — the scope, then a first publish by hand

A **scope belongs to an organisation**, so creating the organisation is what
holds `@idside`.

1. Sign in at [npmjs.com](https://www.npmjs.com/) and **enable 2FA** on the
   account (`Account → Two-factor authentication`). Trusted publishing later
   refuses to be configured without it.
2. `Add organization` → name it **`idside`**. A free organisation may publish
   unlimited **public** packages, which is all this needs. The scope `@idside` is
   now yours, and nobody else can publish `@idside/anything`.
3. Publish `@idside/sdk` once, **by hand**, from a clone:

   ```bash
   cd packages/node
   npm publish --access public --tag next --dry-run   # read the file list first
   npm publish --access public --tag next             # asks for your 2FA code
   ```

   `--access public` is already the package's `publishConfig`, and is repeated
   here because the first publish of a scoped package fails without it.

   **`--tag next` is not optional for a pre-release.** `npm publish` moves the
   `latest` tag by default, whatever the version says, so publishing
   `0.1.0-dev.0` without it makes `npm install @idside/sdk` serve a development
   build to everyone. The package's `publishConfig.tag` is set to `next` for
   exactly this reason; the flag is belt and braces.

   > Why by hand? Because npm's trusted publisher is configured **on a package
   > that already exists** — `npm trust` says so outright ("the package you're
   > configuring must already exist on the npm registry"), and the settings page
   > only exists once there is a package to settle. There is no OIDC path for a
   > first publish. This one command is the only time a human has to hold the
   > credential.

4. Now wire up trusted publishing, so no npm token is ever created:

   ```bash
   npm install -g npm@latest        # needs npm >= 11.15.0 for `npm trust`
   npm trust github @idside/sdk     # asks for your 2FA code
   npm trust list @idside/sdk
   ```

   or the same thing through `npmjs.com → the package → Settings → Trusted
   Publisher`: GitHub owner `idside-eu`, repository `idside-sdk`, workflow file
   `publish.yml`, environment `npm`, allowed action `npm publish`.

### 0.2 PyPI — a pending publisher, then the first publish from CI

PyPI has no organisation to create and **no way to reserve a name without
uploading**. Its "pending" trusted publisher lets a workflow publish a project
that does not exist yet, but PyPI is explicit that it *does not* hold the name:
"a 'pending' publisher does **not** create a project or reserve a project's name
**until** it is actually used to publish". So the name is held by the first
upload, and only by it.

1. Create the account at [pypi.org](https://pypi.org/) and **enable 2FA** (it is
   mandatory for uploads).
2. `Account settings → Publishing → Add a new pending publisher`:
   - PyPI project name: **`idside`**
   - Owner: `idside-eu`
   - Repository name: `idside-sdk`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
3. Rehearse on **TestPyPI** first — same form, same account flow, at
   [test.pypi.org](https://test.pypi.org/). A rehearsal there burns a TestPyPI
   version number, which costs nothing; a rehearsal on PyPI burns a real one
   forever.
4. Run the [publish workflow](.github/workflows/publish.yml). It creates the
   project, claims the name, and converts the pending publisher into a normal
   one — "no further configuration is required".

A `0.1.0.dev0` upload is the right thing to hold the name with. While it is the
only release, a plain `pip install idside` installs it: pip falls back to
pre-releases when no final release exists. From the first final release on, pip
serves that one and a pre-release needs `--pre`. npm has no such rule — a
publish moves `latest` whatever the version says — which is why §0.1 insists on
`--tag next`.

---

## 1. What is already verified, and by what

Nothing below needs to be re-done by hand at release time. It runs on every pull
request, and the release workflow runs it again on the exact commit being
published.

| Check | What it proves | Where |
| --- | --- | --- |
| `pytest` (Python 3.10–3.13), `node --test` | the SDKs behave | CI `python`, `node` |
| `ruff`, `mypy --strict`, `bandit` | lint, types, security lint | CI `python` |
| `tsc --noEmit` | the Node SDK and its type-level assertions compile | CI `node` |
| `scripts/check_licence_claims.sh` | **the licence says what the repository means** — nothing claims to be open source, no manifest asserts an approval these terms do not have, `LICENSE` is byte for byte the fingerprinted rendering of counsel's text with a dated version line, `LICENSE` and `NOTICE` have not drifted between their three copies, no tracked file carries a marking from counsel's working copy, and the patent marking `Patents: https://sdk.idside.eu/patents` heads every source file and sits in the READMEs, `NOTICE` and both manifests | CI `licence`, and first inside `check_publishable.sh` |
| `scripts/check_publishable.sh` | both packages **pack, install outside the repository, and work there** — including the bundled JSON, the license copies, the patent marking at the top of every shipped `.js` and `.py` file and in the wheel's metadata, the `[verify]` extra, ESM *and* CJS consumers, and the sdist installing on its own | CI `publishable` |
| `scripts/check_sdk_parity.sh` | the two SDKs answer alike — consent dialog, credential routing, disclosure kinds, journal lines | run locally / before release |
| `scripts/check_docs_examples.sh` | every quickstart and every program in `examples/` still prints what its document claims | CI `docs-examples` |
| descriptor sync tests | the bundled descriptors are byte-identical to `packages/descriptors/` | both suites |
| shared vectors | both SDKs hash a request and verify a token identically | both suites |

The [publish workflow](.github/workflows/publish.yml) builds the sdist, the
wheel and the npm tarball **once**, in the job that runs those checks, and hands
those exact files to the publishing steps — so what is uploaded is what was
tested rather than a rebuild of it. What no check in this repository can prove is
what the registry then serves; §4 is how that gets looked at.

## 2. Cutting a release

One version number, both packages, one tag. In order:

1. **Decide the version** against [`docs/versioning.md`](./docs/versioning.md).
   A break means a minor bump while the major is `0`.
2. **Bump it in exactly two files** — they are the only two, and
   `check_publishable.sh` fails if they disagree:
   - `packages/python/src/idside/__about__.py` → `__version__` (PEP 440:
     `0.1.0`, `0.1.0.dev0`, `0.1.0rc1`)
   - `packages/node/package.json` → `version` (SemVer: `0.1.0`,
     `0.1.0-dev.0`, `0.1.0-rc.1`)
3. **Move the `Unreleased` section** of [`CHANGELOG.md`](./CHANGELOG.md) under
   the new version and date, and open a fresh `Unreleased`. An entry that breaks
   something says what to write instead, or it is not finished.
4. **If this is the first stable release**, remove `"tag": "next"` from
   `packages/node/package.json`'s `publishConfig` in the same commit — otherwise
   `1.0.0` publishes to the `next` tag and `latest` stays on a dev build.
5. **Run the two scripts CI does not run on every PR:**
   ```bash
   ./scripts/check_sdk_parity.sh
   ./scripts/check_publishable.sh
   ```
6. **Commit, then tag.** One annotated tag for both packages, because there is
   one version:
   ```bash
   git commit -m "chore(release): 0.1.0"
   git tag -a v0.1.0 -m "0.1.0"
   git push origin main --follow-tags
   ```
7. **Publish.** Dispatch the workflow from the tag itself. GitHub's web UI only
   offers branches in its **Run workflow** selector, while this workflow
   deliberately refuses a branch even when it points at the right commit. Use
   the CLI (or the equivalent REST API) to name the tag explicitly:

   ```bash
   gh workflow run publish.yml --ref v0.1.0.dev0 \
     -f version=0.1.0.dev0 -f packages=both -f npm_tag=next -f confirm=publish
   ```

   The tag and `version` input use the PEP 440 spelling, for example
   `v0.1.0.dev0` and `0.1.0.dev0`. The workflow verifies the tag and manifest
   versions, then invokes the complete CI matrix from that same commit before
   building release artifacts. Protect release tags against replacement/deletion
   and restrict the `npm`/`pypi` environments to release tags in repository
   settings. The workflow files do not configure those repository protections;
   a read-only preflight does refuse to build when the selected environment is
   absent or has no required reviewer.

   [GitHub's local reusable workflow contract](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)
   ensures that the called CI workflow comes from the caller's commit.

   | Input | What to type |
   | --- | --- |
   | `version` | the PEP 440 spelling, e.g. `0.1.0.dev0` — checked against both manifests |
   | `packages` | `both`, or one registry to finish a half-done release (or to skip npm on the very first publish, which is done by hand — §0.1) |
   | `npm_tag` | `next` for any pre-release, `latest` for a final one; it must match `publishConfig.tag` |
   | `confirm` | the word `publish` |

   Nothing is built until all four check out and the selected registry
   environments have a required reviewer. That reviewer is the last chance to
   stop an upload. Order and failure handling: §3.
8. **Verify from the registries** — §4.
9. **Write the GitHub release** from the CHANGELOG section, pointing at the tag.

## 3. In which order the two packages go out

**npm first, then PyPI.** Both burn their version number the instant the
registry accepts a file, and neither can be replaced, so the order is chosen for
where the attention is needed:

- The npm publish is the one that cannot be fully automated for a first release
  (§0.1) and the one where a missing dist-tag is instantly visible to every
  installer. It goes first, while a human is watching.
- The PyPI publish is the workflow's job, and once a final release exists a
  pre-release there is invisible to a plain `pip install`.

If the second one fails, **do not touch the first.** The failed package's
version was never consumed — fix the cause and publish it again at the *same*
version. Bumping the succeeded package to keep the pair aligned would burn a
second version number to solve a bookkeeping problem.

One PyPI subtlety: `twine`/the publish action uploads the sdist and the wheel as
two files. If one is accepted and the other rejected, only the accepted
*filename* is burned. Re-upload just the missing file; do not rebuild under a
new version.

## 4. After publishing — check the registry, not the repository

The packages the world gets are the ones on the registries. Install them the way
a stranger would, outside this repository:

```bash
# Node — from the registry, into an empty directory
mkdir /tmp/verify-node && cd /tmp/verify-node && npm init -y >/dev/null
npm install @idside/sdk@next          # or @latest for a stable release
node -e 'import("@idside/sdk").then(m => { if (m.loadDescriptors().size < 4) process.exit(1); console.log("ok", m.loadDescriptors().size, "descriptors"); })'
```

```bash
# Python — from the registry, into a fresh virtualenv
python3 -m venv /tmp/verify-py && /tmp/verify-py/bin/pip install --pre 'idside[verify]'
/tmp/verify-py/bin/python -c "import idside; from idside.providers import load_descriptors; assert len(load_descriptors()) >= 4; print('ok', idside.__version__)"
```

Then check what a browser sees: the PyPI project page renders
`packages/python/README.md`, the npm page renders `packages/node/README.md`, and
both are the first thing a stranger reads. A broken relative link in either
README is a link into nothing once it is off GitHub.

## 5. When a release goes wrong

**The rule both registries share: a published version number is gone forever.**
Not "hard to reuse" — gone. npm: "Once `package@version` has been used, you can
never use it again. You must publish a new version even if you unpublished the
old one." PyPI: "PyPI does not allow for a filename to be reused, even once a
project has been deleted and recreated", and "deleted files cannot be
re-uploaded."

So there is no such thing as replacing a bad release. There is only marking it
and publishing the next one.

| What happened | What to do | What **not** to do |
| --- | --- | --- |
| Broken build, wrong artefact, missing file | publish the fix as the next patch, then `npm deprecate '@idside/sdk@0.1.0' "broken, use 0.1.1"` and yank `0.1.0` on PyPI | delete or unpublish "to redo it" — the version number dies with it |
| Published to the wrong dist-tag (npm) | `npm dist-tag add @idside/sdk@<good> latest` — dist-tags are mutable, this one *is* recoverable | republish |
| Wrong version number, nothing else wrong | leave it, and say so in the CHANGELOG | try to reclaim the number |
| A secret got into the artefact | **rotate the secret first**, then treat the release as broken above, then open a [security advisory](./SECURITY.md). Unpublishing does not un-leak: the file was mirrored the moment it was public | assume deletion is containment |

Reference points:

- **PyPI yank** ([PEP 592](https://peps.python.org/pep-0592/)) is the right tool:
  "a yanked release is a release that is always ignored by an installer, unless
  it is the only release that matches a version specifier". Someone who pinned
  the exact version still gets it; nobody resolves to it by accident; the
  history stays visible.
- **`npm deprecate`** leaves the version installable and prints a warning on
  install. It is the npm equivalent and, like yanking, it is reversible.
- **`npm unpublish`** is only allowed within 72 hours of publishing, and after
  that only if nothing depends on the package, it had under 300 weekly
  downloads, and it has a single maintainer. Even then the version number is not
  recovered, and unpublishing *every* version locks the name for 24 hours.
  Treat it as unavailable.

Withdrawing a **build** is this section. Retiring an **API** is
[`docs/versioning.md` §4](./docs/versioning.md#4-deprecation); they are different
things and `npm deprecate` is not how a function is deprecated.

## 6. Before the first release

None of it is code, and none of it can be done from inside this repository.

| # | What | Why |
| --- | --- | --- |
| 1 | One manual `npm publish` of `@idside/sdk`, then its trusted publisher (§0.1) | npm has no OIDC path for a first publish |
| 2 | A **pending** trusted publisher for `idside` on PyPI and on TestPyPI (§0.2) | without it the workflow has no way to authenticate |
| 3 | Two GitHub **environments** on this repository, named `pypi` and `npm`, each with at least one required reviewer | GitHub otherwise creates a named-but-absent environment with no protection; the workflow checks the selected environments through the read-only API and stops before CI/build if either the environment or its reviewer rule is absent |
| 4 | A TestPyPI rehearsal (§0.2 step 3) | the only place a first publish can be got wrong for free |
| 5 | A read of §7 | every line of it is irreversible after the first upload |

Explicitly **not** needed, and not to be created: a PyPI API token, an npm
automation token, or any repository secret. Both registries publish from GitHub
Actions over OIDC, and a token that does not exist cannot leak.

GitHub does not offer required reviewers on private repositories on every plan.
If this repository's plan does not expose that setting, the preflight will keep
publication disabled. Make the repository public as intended before the first
release, or use a plan that supports required reviewers on private repositories;
do not weaken or remove the check to make a private publish pass.

## 7. What the first publish freezes

Everything here is cheap to change today and expensive or impossible to change
once a package is installable. Read it once, on purpose.

**Names, permanently:**

- The PyPI project name `idside` and the import name `idside`. The import name is
  the harder of the two: it appears in every user's source file.
- The npm scope `@idside` and the package name `@idside/sdk`. A second package
  under the scope is easy; renaming this one is not.
- The public submodules a caller may import: `idside.consent`, `idside.verify`,
  `idside.exceptions`, `idside.options`, `idside.adapters`, `idside.providers`.
  Anything meant to stay private needs its leading underscore **before** the
  first release, not after.
- The extra names `openai`, `anthropic`, `gemini`, `mistral`, `verify`, `all`.
  `idside[verify]` in someone's lockfile outlives any regret about the word.
- Every exported error class name — they are the documented thing to catch.

**Configuration, because it lives in other people's deployments:**

- `IDSIDE_API_KEY` and `IDSIDE_BASE_URL`. Renaming an environment variable means
  a silent failure in a production deployment that was working.
- The `X-AI-Consent-Token` header name, which is also on the wire and in the
  cloud.
- The journal switches — the `idside` Python logger name and `NODE_DEBUG=idside`.

**The protocol, which no package version can fix** (see
[`docs/versioning.md` §1](./docs/versioning.md#1-two-things-are-versioned-and-they-are-not-the-same-thing)):

- The request-hash definition, including *which* fields are excluded. An auditor
  recomputes it years later; if it moves, old proofs stop checking out.
- The ten mandatory fields of the authorize response. Removing one stops every
  older SDK from making AI calls at all.
- Call-site ids (`chat.completions.create`, …) and consent-option names
  (`training.foundation`, …). Both end up inside **signed** tokens and in the
  ledger, where nothing can be migrated.
- `typ: act+jwt`, `alg: ES256`, the `idside.eu` issuer.

**The addresses, per published version:**

- `github.com/idside-eu/idside-sdk` is in `package.json`'s `repository`,
  `homepage` and `bugs`, in `pyproject.toml`'s `[project.urls]`, and in the
  trusted-publisher configuration on both registries. Renaming the repository or
  the GitHub organisation stops publishing until both registries are
  reconfigured, and leaves every already-published version pointing at a 404.
- Each package's own README **is** its registry landing page — PyPI renders
  `packages/python/README.md`, npm renders `packages/node/README.md` — and
  neither can be edited after the fact for a version already published. PyPI
  does not rewrite relative links, so both files carry absolute
  `github.com/idside-eu/idside-sdk/blob/main/…` URLs and must keep doing so.

**The floors:**

- `requires-python = ">=3.10"` and `engines.node = ">=22.12.0"`. Raising either
  is a breaking change for the people it drops; lowering one never is. Publish
  the floor you can live with, not the newest one that happens to work.

**The licence:**

- The ID Side Source-Available License, version 1.0 — see
  [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE). Beyond the limited evaluation
  patent licence of clause 2 of `LICENSE`, rights under the patents are conveyed
  only by an executed commercial licence. A licence can be changed for *future*
  versions; it cannot be withdrawn from the ones already published. Anyone who
  installs `0.1.0` keeps its terms forever.

  That sentence is why the text is pinned. `scripts/check_licence_claims.sh`
  fingerprints `LICENSE` byte for byte, so the version a release carries is the
  rendering installed from counsel and nothing else. **Check the licence before
  the first upload, never after.** A change to it — even a typo — goes through
  counsel and comes back as a new version line and a new fingerprint, committed
  together, before the release that is to carry it.

  `scripts/check_publishable.sh` runs that check first, so this step cannot be
  the one somebody skips. It reads what the repository *claims*, not whether
  the claim is the one you want: deciding whether these are the terms to publish
  under is the one part of this runbook no script does for you.

**The patent marking:**

- Every shipped source file, the three READMEs, `NOTICE`, `[project.urls]` and
  the npm description carry `Patents: https://sdk.idside.eu/patents` — the
  virtual marking 35 U.S.C. §287(a) allows, and the notice clause 5 of
  `LICENSE` provides for. A version keeps the marking it shipped with, so the
  address has to keep answering for as long as any version is installed.

- **Never publish a version before https://sdk.idside.eu/patents answers.** A
  marking that points at nothing marks nothing. The publish workflow fetches the
  page before anything is built and refuses to go on unless it names
  `12,413,397`, `4,100,905` and `CN115151937A`.

- **Keep the page current; the marking stays the same.** That is the point of a
  virtual marking: when a patent issues and covers the SDK — the Chinese
  application, a continuation, a new filing — add it to the page; when one stops
  covering the SDK, because it lapsed, expired, was revoked or no longer reads on
  the code, take it off. Listing a patent that does not cover the product
  risks false marking. Record the numbers themselves in `NOTICE` in the same change,
  and in the patent list `check_licence_claims.sh` requires of it.
