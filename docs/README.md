# ID Side SDK — documentation

AI consent infrastructure: a signed proof, attached to every AI API call, of the
choices it was authorized against and the settings forced on it. This is the documentation set for the
source-available SDKs.

## Start here

| Page | What it covers |
| --- | --- |
| [Examples](../examples/) | Three programs to clone and run today — a wrapped call in Python, the same on an async client, and the typed TypeScript one |
| [Quickstart — Python](./quickstart-python.md) | Install, `consent.wrap()`, your first enforced call, and a runnable offline dry run |
| [Quickstart — Node.js](./quickstart-node.md) | The same for `@idside/sdk`, including how to run it from a checkout |
| [Concepts](./concepts.md) | The mental model: consent as infrastructure, the opaque tunnel, **the two modes** (asked and imposed), the ACT token, the ledger, GPC, fail-closed |
| [Preference Center widget](./widget.md) | The other half of the integration: the `<script>` tag that collects the answers the SDK enforces |

## Reference

| Page | What it covers |
| --- | --- |
| HTTP API reference — `GET /openapi.yaml` | The endpoints this SDK calls, field by field: request bodies, every response, and the full catalogue of problem types. Served by the cloud unauthenticated, at the service origin configured in `IDSIDE_BASE_URL`, so you can read it, diff it and generate a client from it without an account. **This is the contract**: where a sentence in these pages and that file disagree, that file is right — it is checked against the running routes on every build, and prose is not. |
| [Providers](./providers.md) | Supported clients, intercepted call sites, where parameters and the token are injected, and how to add a provider |
| [Provider privacy matrix](./providers-privacy-matrix.md) | Option by option, what each provider actually lets you control — enforced, contractual, account-level, absent from the API, or not through the endpoints the SDK wraps |
| [Errors](./errors.md) | Every error type both SDKs raise — enforcement and token verification — when each fires, and what to do about it |
| [FAQ](./faq.md) | Outages, latency, what ID Side sees, GDPR posture, quota, verifying a token, bypassing the SDK |
| [Versioning](./versioning.md) | What a version number promises, what the *protocol* promises instead — you can pin the SDK and not the cloud — and how a deprecation is announced |
| [Changelog](../CHANGELOG.md) · [Releasing](../RELEASING.md) | What changed in each release, and the mechanics of cutting one |

## Specifications and design

| Document | What it covers |
| --- | --- |
| [Architecture](./ARCHITECTURE.md) | The two circuits, the source-available/proprietary boundary, the descriptor plugin system |
| [ACT token specification](./spec/act-token.md) | The public token format: claims, canonicalization, key rotation, verification |

## Package references

| Package | |
| --- | --- |
| [`packages/python/`](../packages/python/README.md) | Python SDK — reference implementation |
| [`packages/node/`](../packages/node/README.md) | Node.js SDK (`@idside/sdk`), at parity with Python |
| [`packages/descriptors/`](../packages/descriptors/README.md) | Shared provider descriptors, JSON Schema, and the cross-SDK vectors that pin both halves of the contract: request hashing and verification verdicts |
| [`packages/widget/`](../packages/widget/README.md) | Preference Center widget — a hosted service, not a package; pointer to the [guide](./widget.md) |

## Suggested reading path

If you are evaluating ID Side, read [Concepts](./concepts.md) first — especially
[the opaque tunnel](./concepts.md#3-the-opaque-tunnel), which explains why the
SDK refuses to let you set a user's preferences, and
[the trust model](./concepts.md#what-the-tunnel-does-not-claim), which is honest
about what the design does and does not guarantee.

If you are integrating, go straight to a quickstart, then read
[errors](./errors.md) before you ship — the fail-closed behavior is a design
decision your calling code has to be ready for. If your users are people you
intend to *ask*, the [widget guide](./widget.md) is the other half of the
integration: without it nobody ever writes a preference, so every option
resolves to a refusal and every proof you collect records that none was
established.

## Status

Pre-release, under active development.

- The Python and Node.js SDKs are implemented and tested — enforcement
  (`consent.wrap()`) and offline proof verification (`verify_act()` /
  `verifyAct()`). Both are published as pre-releases: `idside` `0.1.0.dev0` on
  PyPI and `@idside/sdk` `0.1.0-dev.0` on npm, under the `next` dist-tag.
- The Preference Center widget is implemented and served by the ID Side cloud —
  three presentations, French and English, GPC detection, and a host-page API
  that never exposes what a user chose. It is a `<script>` tag rather than a
  package in this repository; the [guide](./widget.md) is its interface.
- The ID Side cloud is self-service at <https://sdk.idside.eu>, with a free
  plan, and both SDKs point there by default. A real end-to-end call or a live
  widget needs an account. Without one, both
  quickstarts include a runnable offline dry run, and [`examples/`](../examples/)
  holds the same mechanism as programs you can run, including the async surface.

Anything not implemented is marked as such on the page where it would live. If
you find documentation that promises something the code does not do, that is a
bug — please [open an issue](https://github.com/idside-eu/idside-sdk/issues).

## Licence

**Source-available, not open source.** The SDK is licensed under the
[ID Side Source-Available License, version 1.0](../LICENSE), also published at
<https://sdk.idside.eu/legal/sdk-licence>. Clause 2 of the licence grants a
royalty-free licence to reproduce, execute and modify the SDK solely for
non-production internal evaluation, research, and non-commercial testing;
production deployment and any other commercial purpose require a paid
commercial license ([`COMMERCIAL_LICENSE.md`](../COMMERCIAL_LICENSE.md)). Why it
is arranged this way: [Is this open source?](./faq.md#is-this-open-source)

## Contributing and security

[`CONTRIBUTING.md`](../CONTRIBUTING.md) for why outside contributions are not
accepted yet, and how to propose a provider (usually a JSON file) meanwhile.
[`SECURITY.md`](../SECURITY.md) for vulnerability reports — never in a public
issue.
