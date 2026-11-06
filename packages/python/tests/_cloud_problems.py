"""Problem documents the cloud answers with, shared by the tests that read them.

Not a test module: pytest collects ``test_*.py`` only.
"""

from __future__ import annotations

from typing import Any

#: What the cloud sends when an organization's policy cannot apply to a call
#: and the application is set to hold such calls. This is the Enforce half: a
#: 403, no token, and the provider is never called. The Observe half — the call
#: goes out, and its token names the same deviations in a ``deviations`` claim
#: — is covered in test_verify_act.py. Deviation kinds and option names, never a
#: person. ``detail`` is the cloud's own sentence for these two deviations on
#: an OpenAI call, word for word.
POLICY_HOLD: dict[str, Any] = {
    "type": "https://idside.eu/errors/policy_hold",
    "title": "Policy hold",
    "status": 403,
    "detail": (
        "Held by your organization's policy: openai is not among the providers "
        "your organization allows (provider_not_allowed); openai cannot apply "
        "your organization's refusal of training.foundation (option_not_enforced). "
        "This team is set to Enforce, so nothing was signed and the AI call must "
        "not go out; your organization sees this call in its ID Side dashboard."
    ),
    "deviations": [
        {"kind": "provider_not_allowed"},
        {"kind": "option_not_enforced", "option": "training.foundation"},
    ],
}
