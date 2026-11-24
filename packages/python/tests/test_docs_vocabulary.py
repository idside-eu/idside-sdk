"""The documentation calls ID Side's customer the organization, never the company.

The customer is whoever sets a policy for its employees, and the cloud names it
"organization" on every screen, in every e-mail and in every `detail` its API
returns. A guide that says "your company's policy" above an error message that
says "your organization's policy" describes one customer under two names, and
leaves the reader to work out that they are the same.

"company" keeps one other meaning in these documents: the provider a prompt is
sent to. Those sentences are listed below phrase by phrase, so an exemption
covers its own words and never the rest of the file.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]

# Where a developer reads about the customer: the guides, every README, and the
# changelog, which says what the first release carries in the same words.
_DOCUMENTS = sorted(
    [
        *(_ROOT / "docs").rglob("*.md"),
        _ROOT / "README.md",
        _ROOT / "AGENTS.md",
        _ROOT / "CHANGELOG.md",
        _ROOT / "examples" / "README.md",
        *_ROOT.glob("packages/*/README.md"),
    ]
)

_COMPANY = re.compile(r"\bcompan(?:y|ies)\b", re.IGNORECASE)

#: Where "company" names a provider — the business a prompt is sent to — and
#: not the customer. Whole phrases, matched across line breaks.
_PROVIDER_SENSE = ("send the prompt to a company the user's answer never mentioned",)


def _prose(text: str) -> str:
    """One line, so a phrase wrapped in the Markdown still reads as one."""
    return " ".join(text.split())


def _customer_mentions(text: str) -> list[str]:
    prose = _prose(text)
    for phrase in _PROVIDER_SENSE:
        prose = prose.replace(phrase, " ")
    return [
        prose[max(0, found.start() - 40) : found.end() + 40]
        for found in _COMPANY.finditer(prose)
    ]


def test_the_documents_call_the_customer_the_organization() -> None:
    # A glob that silently matched nothing would pass every document it missed.
    read = {path.relative_to(_ROOT).as_posix() for path in _DOCUMENTS}
    assert {"docs/concepts.md", "docs/errors.md", "README.md"} <= read

    said = {
        path.relative_to(_ROOT).as_posix(): mentions
        for path in _DOCUMENTS
        if (mentions := _customer_mentions(path.read_text(encoding="utf-8")))
    }

    assert said == {}, (
        "The customer is the organization, as on the cloud's screens and in "
        "its API. Where 'company' names a provider, add the phrase to "
        "_PROVIDER_SENSE."
    )


def test_the_exemption_covers_its_phrase_and_nothing_else() -> None:
    exempt = "would send the prompt to a company the user's answer never mentioned"

    assert _customer_mentions(exempt) == []
    assert _customer_mentions(exempt.replace(" a company", " a\ncompany")) == []
    assert len(_customer_mentions(f"{exempt}, and the company's policy")) == 1
    assert len(_customer_mentions("Held by your company's policy")) == 1
    assert len(_customer_mentions("For the company-imposed mode")) == 1
    assert len(_customer_mentions("Companies set a policy")) == 1
    assert _customer_mentions("the token that accompanies it") == []


def test_every_exempted_phrase_is_still_in_the_documents() -> None:
    # An exemption whose sentence was rewritten would wait, unnoticed, for the
    # next sentence that happens to match it.
    everything = " ".join(_prose(path.read_text("utf-8")) for path in _DOCUMENTS)

    for phrase in _PROVIDER_SENSE:
        assert phrase in everything, phrase


# The words of 2026-11-19 and 2026-11-23, as the dashboard and the public site
# say them: the signed record of a call is a certificate, not a "proof", and
# a strainer blocks a provider or lets it through, one switch per provider,
# instead of the single Enforce / Observe choice. A guide in the old words
# describes a setting nobody can find on the dashboard any more.
#
# The changelog keeps the words its released entries were written in, and the
# privacy matrix is generated from the descriptors, so neither is read here.
# Code is not prose: a variable named `proof` in an example, and an anchor kept
# so that an old link still lands, are not the notion.
_PROSE_DOCUMENTS = [
    path
    for path in _DOCUMENTS
    if path.name not in {"CHANGELOG.md", "providers-privacy-matrix.md"}
]

_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_ANCHOR = re.compile(r'<a id="[^"]*"></a>')
_LINK_TARGET = re.compile(r"\]\([^)]*\)")

_OLD_WORDS = re.compile(r"(?<![-\w])(?:proofs?|Proofs?|PROOF|Enforce|Observe)(?![-\w])")


def _old_words(text: str) -> list[str]:
    for code in (_FENCE, _INLINE_CODE, _ANCHOR, _LINK_TARGET):
        text = code.sub(" ", text)
    prose = _prose(text)
    return [
        prose[max(0, found.start() - 40) : found.end() + 40]
        for found in _OLD_WORDS.finditer(prose)
    ]


def test_the_documents_say_certificate_and_name_no_retired_setting() -> None:
    read = {path.relative_to(_ROOT).as_posix() for path in _PROSE_DOCUMENTS}
    assert {"docs/concepts.md", "docs/spec/act-token.md", "README.md"} <= read

    said = {
        path.relative_to(_ROOT).as_posix(): mentions
        for path in _PROSE_DOCUMENTS
        if (mentions := _old_words(path.read_text(encoding="utf-8")))
    }

    assert said == {}, (
        "The signed record of a call is a certificate, and a strainer blocks a "
        "provider or lets it through: 'proof', 'Enforce' and 'Observe' are the "
        "words the dashboard retired."
    )


def test_the_old_words_are_refused_in_prose_and_left_in_code() -> None:
    assert len(_old_words("and every proof records what applied")) == 1
    assert len(_old_words("Proofs carry the issuer")) == 1
    assert len(_old_words("set to Enforce, the call is held")) == 1
    assert len(_old_words("Observe lets the call through")) == 1
    assert _old_words("a tamper-proof ledger, future-proof fields") == []
    assert _old_words("read `proof.mode` first") == []
    assert _old_words("```python\nproof = verify_act(token)\n```\n") == []
    assert _old_words('<a id="7-verify-a-proof-yourself"></a>') == []
    assert _old_words("[step 7](#7-verify-a-proof-yourself)") == []
    assert _old_words("Enforcement splits along two axes; we enforce it") == []
    assert _old_words("what the stand-ins observed") == []
