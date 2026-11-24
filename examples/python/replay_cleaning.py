"""Replay prompt cleaning on a request that left: the check an auditor runs.

When an organization cleans prompts, the SDK masks what its policy names before
the call is hashed and sent, and the certificate names the policy by its digest. A
request the SDK cleaned is a fixed point of that policy: cleaning it again
changes nothing. So whoever holds the request that left and the policy can
check it, offline, years later, without ID Side — which is all this program
does, with the SDK's public API.

Nothing here is a stand-in: no cloud is asked and no provider is called. The
policy and the requests are fictitious.

Run it:  uv run replay_cleaning.py
"""

from __future__ import annotations

import json

from idside import content_policy_digest, verify_clean

# The organization's policy, as the dashboard set it and the certificate names it.
POLICY = {
    "schema": 1,
    "app_id": "app_medhelp",
    "revision": 7,
    "detectors": {"email": "mask", "phone": "mask", "credit_card": "block"},
    "attachments": "allow",
    "custom": [
        {"kind": "term", "value": "Projet Atlas", "action": "mask", "label": "PROJECT"}
    ],
}

# The request that left, as the provider received it.
SENT = {
    "model": "gpt-4o-mini",
    "messages": [
        {"role": "system", "content": "You help the [PROJECT_1] team."},
        {"role": "user", "content": "Reply to [EMAIL_1], or call [PHONE_1]."},
    ],
}

# The same request, had a value slipped through.
LEAKED = {
    "model": "gpt-4o-mini",
    "messages": [
        {"role": "system", "content": "You help the [PROJECT_1] team."},
        {"role": "user", "content": "Reply to marie.dupont@example.com."},
    ],
}


def main() -> None:
    digest = content_policy_digest(POLICY)
    print("1. The policy the certificate names")
    print(f"   digest: {digest}")
    print("   (compare it with the certificate's before trusting the replay)")
    print()
    print("2. The request that left")
    print(f"   {json.dumps(SENT['messages'][1]['content'])}")
    print(f"   verify_clean: {verify_clean(SENT, POLICY, provider='openai')}")
    print("   ...nothing left to mask or block: what left was cleaned.")
    print()
    print("3. A request with an address left in it")
    print(f"   {json.dumps(LEAKED['messages'][1]['content'])}")
    print(f"   verify_clean: {verify_clean(LEAKED, POLICY, provider='openai')}")
    print("   ...this policy would still mask something: it was not applied.")
    print()
    print("True says this policy finds nothing more to do, not that the request")
    print("held nothing sensitive: cleaning reduces what leaves, it does not find")
    print("everything.")


if __name__ == "__main__":
    main()
