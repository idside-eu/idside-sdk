# Repository instructions

- Keep all tracked code, comments, documentation and commit messages in English.
- Do not commit, push or publish unless explicitly requested. Commit messages
  must not mention assistant tooling or add automated co-author attribution.
- Python commands run in `packages/python`: `uv sync --all-extras`,
  `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run bandit -c pyproject.toml -r src`.
- Node commands run in `packages/node`: `npm ci`, `npm run typecheck`,
  `npm test`, `npm run build`. Tests require Node 24; package smoke tests also
  run on the declared consumer floor, Node 22.12.
- Cross-package checks run from the repository root:
  `scripts/check_version_parity.sh`, `scripts/check_sdk_parity.sh`,
  `scripts/check_publishable.sh`, `scripts/check_docs_examples.sh`.
- Preserve fail-closed behavior: caller-supplied proof headers never authorize
  a provider call, malformed authorization responses never reach the provider,
  and incomplete installation of transport guards must fail explicitly.
  Bind internal provenance to the wrapped client, token and sending endpoint;
  preserve legitimate retries and roll back only patches owned by a failed wrap.
  Validate all ten required authorization fields. Keep `source` and
  `legal_basis` opaque printable metadata; see `docs/versioning.md`.
- Keep Python/Node behavior and bundled descriptors aligned. Test real provider
  client objects with mocked networking; do not send live provider requests as
  an incidental part of a local test.
- Preserve historical request hashes. Update public contracts when changing
  verification options, response validation or compatibility behavior.
- Publication is manual from the matching `v<VERSION>` tag and waits for the
  complete reusable CI workflow on the same commit. See `RELEASING.md` for
  registry setup and repository protections.

- Integration docs use `https://sdk.idside.eu`, the SDK default, and the
  registry installs, bare first (`pip install idside`, `npm install @idside/sdk`:
  the SDK wraps the provider client a reader already has), then the
  starting-from-nothing forms (`pip install 'idside[openai]'`,
  `npm install @idside/sdk openai`).
  `IDSIDE_BASE_URL` is for a local or staging cloud: the origin alone, without
  `/v1` or `/widget`. Naming the end user is optional: a call with no
  identity sends no identity field, and the API key is the proof's subject
  (`binding: api_key`; the answer's binding must be the identity sent —
  `user_id`, `ubt`, or `api_key` for none — and nothing else). An identity that
  is present but names nobody stays a `MissingIdentityError`, and so does a call
  naming nobody made from inside an enforced call in progress (a provider
  helper re-entering without the permit); two stay a `ConflictingIdentityError`.
  An argument spelled like `user_id`/`ubt` but not exactly (`userId`, `USER_ID`,
  `Ubt`: equal once case and separators are stripped) is an
  `UnenforceableOptionError` before the identity is read, in both SDKs. Strict
  visitor apps need a UBT bound to the same server session; never fall back to
  `user_id` or to naming nobody.
