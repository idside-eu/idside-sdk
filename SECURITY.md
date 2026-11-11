# Security Policy

Security is at the heart of ID Side: the product *is* a proof of compliance.
A flaw in this SDK can compromise our users' chain of proof. We take every
report very seriously.

## Reporting a vulnerability

**Never open a public issue for a vulnerability.**

Please use one of these private channels:

1. **GitHub Private Vulnerability Reporting** — the repo's *Security* tab →
   *Report a vulnerability* (preferred).
2. **Email**: `contact@idside.eu`.

Please include: a description, the impact, reproduction steps, the affected
version/commit, and any PoC. **Never test** against production systems or real
user data.

## Response commitment

| Step | Target |
| ---- | ------ |
| Acknowledgement | 48 hours |
| Initial severity assessment | 5 business days |
| Fix or mitigation plan | severity-dependent (critical: best-effort immediate) |

We practice **coordinated disclosure**: we publish once a fix is available, and
credit reporters who wish to be.

## Scope

This repository: Python/Node SDK, provider adapters and the local token
**validation**. The widget and the cloud are reported through the same
channels.

The cloud infrastructure (Consent DB, ACT signing, ledger, dashboard) is managed
separately; report any issue affecting it through the same private channels.

## Golden rules for contributors

- **No secrets** in code, tests or commits (keys, tokens, `.env`). CI and
  `pre-commit` (gitleaks + detect-secrets) block leaks — do not bypass these
  guardrails.
- **No private ECDSA keys** may ever appear in this repository: a key
  committed to a repository is a key in everybody's clone of it, and git keeps
  it after the deletion commit.
- Treat all network / user input as untrusted.
- Honor the [invariants](./docs/versioning.md#3-what-no-version-number-can-change): opaque tunnel (no preference setter),
  fail-closed, no bypass option, one token = one request.
