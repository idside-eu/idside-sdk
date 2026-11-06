<!-- Thanks for contributing to ID Side! -->

## Description

<!-- What does this PR do and why? Link the issue: Closes #… -->

## Type of change

- [ ] Bug fix
- [ ] Feature
- [ ] New provider adapter
- [ ] Documentation
- [ ] Refactor / internal

## Checklist

- [ ] Tests pass locally (`uv run pytest`).
- [ ] `pre-commit run --all-files` passes (ruff, gitleaks, detect-secrets…).
- [ ] I added/updated tests and documentation.
- [ ] My commits are atomic and clearly described.

## Security checklist (required)

- [ ] **No secrets** (key, token, `.env`, credential) are introduced.
- [ ] **No private ECDSA key** or ACT signing material.
- [ ] The [invariants](../docs/versioning.md#3-what-no-version-number-can-change) are respected: opaque tunnel (no
      preference setter), fail-closed, no bypass option, one token = one request.
- [ ] Network / user input is treated as untrusted.

## Notes for reviewers

<!-- Points of attention, design decisions, trade-offs. -->
