<div align="center">

# ID Side SDK

**AI consent infrastructure — cryptographic proof, on every AI call, that you honored your users' choices.**

[![License: Source Available](https://img.shields.io/badge/License-Source_Available-blue.svg)](./LICENSE)
[![Status](https://img.shields.io/badge/status-pre--release-orange.svg)](#status)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab.svg)](./packages/python)
[![Node](https://img.shields.io/badge/node-%E2%89%A5%2022.12-5fa04e.svg)](./packages/node)

[Documentation](./docs/README.md) · [Quickstart](./docs/quickstart-python.md) · [Concepts](./docs/concepts.md) · [ACT token spec](./docs/spec/act-token.md)

**Source-available, not open source** — [ID Side Source-Available License, version 1.0](./LICENSE), also published at <https://sdk.idside.eu/legal/sdk-licence>

</div>

> **Pre-release.** Both SDKs are implemented and tested, but neither is published
> to a package registry yet and the ID Side cloud is not publicly available.
> See [Status](#status).

## The problem

Your app sends user data to an AI API. Months later, a data protection authority
asks you to prove that *this specific call*, on *this date*, for *this user*,
honored what that user had chosen at that moment.

A row in your database is an assertion. A screenshot is not evidence. Being
unable to prove compliance is, in practice, being non-compliant.

## The answer

ID Side applies **the Stripe model to AI consent**: an *opaque tunnel*.
Preferences travel from the user straight to ID Side — your backend is never on
that path, so it cannot leak them, and cannot fake them either. You supply an
identity; you get back an enforced call and a signed proof.

```python
from openai import OpenAI
from idside import consent

client = consent.wrap(OpenAI())          # wrap the provider client, once

client.chat.completions.create(
    model="gpt-4o-mini",                 # call it exactly as before…
    messages=[{"role": "user", "content": "Summarize my last consultation."}],
    user_id="usr_marie_456",             # …plus the end user's identity
)
```

<details>
<summary>The same in TypeScript</summary>

```ts
import OpenAI from "openai";
import { consent } from "@idside/sdk";

const client = consent.wrap(new OpenAI());

await client.chat.completions.create({
  model: "gpt-4o-mini",
  messages: [{ role: "user", content: "Summarize my last consultation." }],
  user_id: "usr_marie_456",
});
```

</details>

On every call, the SDK:

1. **asks** the ID Side cloud to authorize the request — sending an identifier, a
   provider, a model and a *hash*, never the prompt;
2. **applies** the non-retention parameters the cloud decided (`store=false`,
   no-training headers…);
3. **attaches** the cloud-signed ACT token (ECDSA P-256 JWT, 1 h TTL, bound to
   this exact request) as the `X-AI-Consent-Token` header;
4. leaves the call recorded in an **immutable hash-chained ledger** (audit proof,
   DSR, GDPR Art. 30).

If consent cannot be verified, **the call does not happen** — [fail-closed](./docs/concepts.md#9-fail-closed).

Apps running the ID Side widget pass `ubt="eyJ..."` (a **User Binding Token**
signed by ID Side) instead of `user_id`: the cloud then verifies the identity
rather than taking the developer's word for it, and the proof records the
stronger `binding="ubt"`.

And because a proof only the issuer can check is not a proof, both SDKs verify
ACT tokens **offline** — `verify_act()` / `verifyAct()`: signature against the
public JWKS, `ES256` pinned, expiry, and the request binding, with no call to
ID Side. This repository verifies; it never signs.

## Documentation

| Page | |
| --- | --- |
| [Examples](./examples/) | Three programs that run today, offline, and print what actually left the process |
| [Quickstart — Python](./docs/quickstart-python.md) | Install, wrap, first call, and a runnable offline dry run |
| [Quickstart — Node.js](./docs/quickstart-node.md) | The same for `@idside/sdk`, with the local install paths |
| [Concepts](./docs/concepts.md) | Opaque tunnel, ACT token, ledger, GPC, fail-closed — and the trust model, stated honestly |
| [Providers](./docs/providers.md) | Supported clients, intercepted call sites, what gets forced and why |
| [Provider privacy matrix](./docs/providers-privacy-matrix.md) | Option by option, what each provider actually lets you control — and what it does not |
| [Preference Center widget](./docs/widget.md) | The `<script>` tag that collects the answers the SDK enforces |
| [Errors](./docs/errors.md) | Every error both SDKs raise, and what to do about it |
| [Versioning](./docs/versioning.md) | What `0.x` promises, what the protocol promises instead, and how deprecations work |
| [Changelog](./CHANGELOG.md) | What each release changed — one version number covers both packages |
| [Releasing](./RELEASING.md) | How a release is cut, in which order, what a botched version costs, and what the first publish freezes forever |
| [FAQ](./docs/faq.md) | Outages, latency, what ID Side sees, GDPR, quota, verifying a token, bypassing the SDK |
| [Architecture](./docs/ARCHITECTURE.md) | The two circuits, the source-available/proprietary boundary, the descriptor system |
| [ACT token spec](./docs/spec/act-token.md) | The public token format — claims, canonicalization, verification |

## Supported providers

| Provider | Python client | Node client | Notes |
| --- | --- | --- | --- |
| OpenAI | `openai` | `openai` | Chat Completions & Responses APIs; covers OpenAI-compatible providers (Groq, Together, xAI…) |
| Anthropic | `anthropic` | `@anthropic-ai/sdk` | consent params injected as request headers |
| Google Gemini | `google-genai` | `@google/genai` | token carried via `config.http_options.headers` |
| Mistral | `mistralai` | `@mistralai/mistralai` | chat, FIM and agents entry points, streaming included |

Adding a standard provider is a JSON descriptor, not code — see
[`packages/descriptors/`](./packages/descriptors/) and
[docs/providers.md](./docs/providers.md#7-adding-a-provider).

## What is here, and what is not

This repository holds the **SDKs** and the **provider adapters** —
every line that touches your requests is auditable. The cloud infrastructure
(consent database, ACT signing keys, immutable ledger, dashboard) is a
proprietary service: it exists to be a trusted third party, which only works if
you cannot hold its keys or rewrite its ledger. The
[Preference Center widget](./docs/widget.md) sits on that side too, and for the
same reason: it is served by ID Side and framed cross-origin so that neither
your page nor your backend is ever on the path an answer travels. Boundary in
[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md#4-source-available--proprietary-boundary).

## Repository layout

```
packages/python/      Python SDK — reference implementation
packages/node/        Node.js SDK — @idside/sdk, at parity with Python
packages/descriptors/ shared provider descriptors + cross-SDK test vectors
packages/widget/      hosted Preference Center — pointer to docs/widget.md
docs/                 documentation set, architecture, ACT token specification
examples/             runnable programs, run and diffed against their own README in CI
```

## Status

Pre-release and under active development. What is true today:

- **Python SDK** — implemented and tested (`consent.wrap()`, four providers,
  typed errors, request-hash vectors, offline `verify_act()`). Blocking and
  async clients alike — `AsyncOpenAI` and `AsyncAnthropic` are enforced with the
  same guarantees, so FastAPI and other ASGI applications are supported.
  **Not published on PyPI yet** — install from a clone.
- **Node.js SDK** — at parity with Python (including `verifyAct()`), zero
  runtime dependencies, native TypeScript. **Not published on npm yet** —
  install from a clone.

  The **artefacts** are release-ready: `scripts/check_publishable.sh` builds
  each package, installs it outside the repository and exercises it, and the
  [publish workflow](./.github/workflows/publish.yml) hands those exact files to
  the registries. What is not ready is everything on the registry side — neither
  name is held, no publishing identity exists, and that is a decision rather
  than a task. [`RELEASING.md`](./RELEASING.md) is the list.
- **Preference Center widget** — implemented and served by the ID Side cloud:
  three presentations (banner, modal, inline panel), French and English,
  automatic GPC detection, and a host-page API that reports *that* a decision
  was recorded and never *what* was decided. It is a `<script>` tag, not an npm
  package — see the [integration guide](./docs/widget.md). It needs the cloud,
  so it requires a provisioned application and service URL.
- **ID Side cloud** — access must be provisioned. Set `IDSIDE_BASE_URL` to the
  supplied service origin rather than assuming the default endpoint is
  available. Both quickstarts also include a runnable offline dry run.

## Contributing

Outside contributions are not accepted yet —
[`CONTRIBUTING.md`](./CONTRIBUTING.md#license-of-contributions) says why; to
propose a provider, write to contact@idside.eu. New providers go through the
descriptor / `ProviderAdapter` system without touching the core, and every
change runs through the automated security checks (CI + `pre-commit`).

## Security

Found a vulnerability? **Do not** open a public issue — see [`SECURITY.md`](./SECURITY.md).

## Licensing

**Source-available, not open source.** The SDK is licensed under the
[ID Side Source-Available License, version 1.0](./LICENSE), also published at
<https://sdk.idside.eu/legal/sdk-licence>.

Clause 2 of the licence grants a royalty-free licence to reproduce, execute and
modify the SDK solely for non-production internal evaluation, research, and
non-commercial testing. Production deployment and any other commercial purpose
require a paid commercial license (clause 3), and redistributing the SDK or
hosting it as a managed service requires ID Side's prior written consent.
Beyond the limited evaluation patent licence of clause 2 of `LICENSE`, rights
under the patents are conveyed only by an executed commercial licence: see
[`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE).

Patents: https://sdk.idside.eu/patents

Commercial licensing: [`COMMERCIAL_LICENSE.md`](./COMMERCIAL_LICENSE.md).
