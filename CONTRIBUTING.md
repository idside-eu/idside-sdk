# Contributing to the ID Side SDK

Thanks for your interest! This SDK is **source-available, not open source**. It
is licensed under the ID Side Source-Available License, version 1.0 — see
[`LICENSE`](./LICENSE). Clause 2 grants a royalty-free licence to reproduce,
execute and modify it solely for non-production internal evaluation, research,
and non-commercial testing; production and any other commercial purpose require
a paid commercial license. This file adds nothing to that grant, and takes
nothing from it: what you may do with the code is what `LICENSE` says.

## Before you start

- Read [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) and
  [`docs/concepts.md`](./docs/concepts.md). The **invariants** — opaque tunnel,
  fail-closed, no bypass, one token per request, no committed secrets — are
  listed in [`docs/versioning.md` §3](./docs/versioning.md#3-what-no-version-number-can-change). They are not negotiable, they are
  not covered by any version number, and a PR that breaks one will be rejected.
- For non-trivial changes, open an **issue** first to discuss.

## Set up your environment

```bash
# Global tools
uv tool install pre-commit

# Security hooks (required)
pre-commit install

# Python package
cd packages/python
uv sync --all-extras
uv run pytest
```

## Standards

- **TDD**: a failing test first, then the production code that makes it pass.
- **Lint/format**: `uv run ruff check --fix` and `uv run ruff format`.
- **Security**: `pre-commit` must pass. **Never use `--no-verify`.**
  No secrets, keys or `.env` in commits.
- **Atomic commits**, clear and descriptive messages, in English.

## Versioning and compatibility

Read [`docs/versioning.md`](./docs/versioning.md) before changing anything a
caller can see. Three rules do most of the work:

- **The package and the protocol are versioned separately.** A caller pins the
  SDK; nobody can pin the cloud. So a change to what `/v1/authorize` sends or
  expects is not a change to this package — it has to keep working for every
  SDK ever published, or every wrapped client running that version fails closed
  and stops making AI calls.
- **Nothing signed can be renamed.** Call-site ids and consent-option names end
  up inside signed ACT tokens and in the ledger. They are added to, never
  changed. The request-hash function is frozen for the same reason: an auditor
  recomputes it years later.
- **A break lands on a minor bump while the major is `0`**, and goes in
  [`CHANGELOG.md`](./CHANGELOG.md) with what to write instead. A patch never
  breaks anything.

## Adding a provider

Most providers need no adapter code at all: a descriptor in
`packages/descriptors/` drives the generic engine (see `packages/descriptors/README.md`).
When one is needed, implement the `ProviderAdapter` interface (see
`docs/ARCHITECTURE.md` §5): `get_call_sites()`, `extract_request_metadata()`,
`apply_constraints()`, `apply()` — and declare the provider's `tool_fields` and
`reserved_headers`, or a tool refusal and a credential posture will each be
narrower than they claim.
A new provider = a new adapter plus its tests, **without touching the core**.

## PR process

**Read the next section first: outside pull requests are not being accepted
yet.** What follows is the process they will go through once the inbound terms
exist, and it is what the maintainers follow today.

1. Branch from `main`.
2. Add tests + docs. Make sure CI passes locally (`pre-commit run --all-files`, `pytest`).
3. Open the PR and complete the template checklist (including the security section).
4. A review from a `CODEOWNER` is required. Signed commits are appreciated.

## License of contributions

**Outside contributions are not being accepted yet, and this section is why.**

Under a permissive licence, the question is settled by the licence itself:
contributions come in and go out under the same terms, symmetrically, so nobody
has to negotiate anything. A source-available licence is not symmetric. ID Side licenses this software
commercially, so it needs inbound rights broad enough to relicense what it
receives — otherwise a single contributed patch would be a piece of the product
that cannot be sold, and no amount of goodwill afterwards fixes that.

Those inbound terms — a contributor agreement, or an assignment — do not exist
yet, and `LICENSE` is not one: it sets out what a recipient may do with the
code. Until inbound terms exist there is nothing for a contributor to agree to,
and accepting a patch on a handshake would leave both sides worse off than
waiting.

If you have something you want to contribute, write to contact@idside.eu and we
will come back to you when the terms are ready. Bug reports and security reports
need none of this — see [`SECURITY.md`](./SECURITY.md).

## Code of conduct

All participation is governed by [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md).
