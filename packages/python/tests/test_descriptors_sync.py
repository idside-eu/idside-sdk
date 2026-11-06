"""Tests keeping the bundled descriptors in sync with the shared canonical package.

The canonical descriptors live in ``packages/descriptors/`` — the single source of
truth shared by every SDK (Python today, Node later). The Python package bundles
byte-identical copies so the wheel stays self-contained; any divergence between a
bundled copy and its canonical counterpart must fail CI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from idside.providers import load_descriptors

_PYTHON_PKG = Path(__file__).resolve().parents[1]
_DESCRIPTORS_PKG = _PYTHON_PKG.parent / "descriptors"
_CANONICAL_DIR = _DESCRIPTORS_PKG / "descriptors"
_BUNDLED_DIR = _PYTHON_PKG / "src" / "idside" / "providers"
_SCHEMA_PATH = _DESCRIPTORS_PKG / "schema" / "provider-descriptor.schema.json"


def _load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _validator() -> Draft202012Validator:
    schema = _load_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


#: The descriptors that must exist whatever the directory listing says: a
#: glob-driven list would happily shrink to nothing.
_REQUIRED_DESCRIPTORS = [
    "openai.json",
    "anthropic.json",
    "gemini.json",
    "mistral.json",
    "deepseek.json",
]

#: Read from the canonical directory rather than written out, so a descriptor
#: added later is compared and validated without anyone remembering to list it
#: here. A hand-written list left `deepseek.json` byte-unchecked and
#: schema-unvalidated on this side while the Node suite (which globs) covered
#: it — the one file that could have drifted was the one nothing looked at.
#:
#: The required names are UNIONED in rather than merely asserted elsewhere: an
#: empty `parametrize` list produces zero test cases, which pytest reports as a
#: skip and CI reads as green. A canonical file that vanished would take its own
#: byte-identity and schema checks with it, silently.
_ALL_DESCRIPTORS = sorted(
    {path.name for path in _CANONICAL_DIR.glob("*.json")} | set(_REQUIRED_DESCRIPTORS)
)


def test_canonical_package_ships_all_descriptors() -> None:
    names = {path.name for path in _CANONICAL_DIR.glob("*.json")}
    assert set(_REQUIRED_DESCRIPTORS) <= names


def test_bundled_and_canonical_descriptor_sets_match() -> None:
    canonical = {path.name for path in _CANONICAL_DIR.glob("*.json")}
    bundled = {path.name for path in _BUNDLED_DIR.glob("*.json")}
    assert bundled == canonical


@pytest.mark.parametrize("name", _ALL_DESCRIPTORS)
def test_bundled_descriptor_is_byte_identical_to_canonical(name: str) -> None:
    canonical = (_CANONICAL_DIR / name).read_bytes()
    bundled = (_BUNDLED_DIR / name).read_bytes()
    assert bundled == canonical, (
        f"{name} diverged from packages/descriptors/descriptors/{name}; "
        "copy the canonical file over the bundled one."
    )


#: The consent catalogue is copied into both SDKs exactly like the descriptors
#: are, and until this test existed nothing checked the three agreed. Every copy
#: is hand-edited — the rationale of an option was last changed in three files in
#: one commit — and a catalogue that drifts asks one SDK's users a question the
#: other SDK's users are never shown, or shows them different words for it.
_CATALOGUE_COPIES = {
    "python": _PYTHON_PKG / "src" / "idside" / "consent-options.json",
    "node": _PYTHON_PKG.parent / "node" / "src" / "consent-options.json",
}


@pytest.mark.parametrize("sdk", sorted(_CATALOGUE_COPIES))
def test_bundled_catalogue_is_byte_identical_to_canonical(sdk: str) -> None:
    canonical = (_DESCRIPTORS_PKG / "consent-options.json").read_bytes()
    assert _CATALOGUE_COPIES[sdk].read_bytes() == canonical, (
        f"the {sdk} copy of consent-options.json diverged from "
        "packages/descriptors/consent-options.json; copy the canonical file over it."
    )


def test_schema_is_json_schema_2020_12_and_closed() -> None:
    schema = _load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert "descriptor_version" in schema["required"]


@pytest.mark.parametrize("name", _ALL_DESCRIPTORS)
def test_canonical_descriptor_validates_against_schema(name: str) -> None:
    descriptor = json.loads((_CANONICAL_DIR / name).read_text(encoding="utf-8"))
    _validator().validate(descriptor)


def test_descriptor_with_unknown_field_is_rejected() -> None:
    descriptor = json.loads((_CANONICAL_DIR / "openai.json").read_text("utf-8"))
    descriptor["set_preferences"] = True  # any undeclared field must be refused
    with pytest.raises(ValidationError):
        _validator().validate(descriptor)


def test_descriptor_missing_required_field_is_rejected() -> None:
    descriptor = json.loads((_CANONICAL_DIR / "openai.json").read_text("utf-8"))
    del descriptor["call_sites"]
    with pytest.raises(ValidationError):
        _validator().validate(descriptor)


def test_descriptor_with_wrong_param_target_is_rejected() -> None:
    descriptor = json.loads((_CANONICAL_DIR / "openai.json").read_text("utf-8"))
    descriptor["param_target"] = "body"
    with pytest.raises(ValidationError):
        _validator().validate(descriptor)


def test_absent_is_a_level_and_the_list_of_levels_stays_closed() -> None:
    """`absent`: the provider does not perform this processing on this API.

    It is not `unsupported`, where the processing happens and cannot be turned
    off. Read by the cloud, the difference decides whether a refusal deviates
    from what an organization asked: refusing what never happens changes nothing. A
    level nobody defined is still refused — an unknown level is an unknown
    promise.
    """
    descriptor = json.loads((_CANONICAL_DIR / "openai.json").read_text("utf-8"))
    descriptor["enforcement"]["memory"] = {
        "level": "absent",
        "note": "Memory in an API app is built by the developer, not the provider.",
    }
    _validator().validate(descriptor)

    descriptor["enforcement"]["memory"]["level"] = "not_applicable"
    with pytest.raises(ValidationError):
        _validator().validate(descriptor)


def test_absent_names_no_lever() -> None:
    """Nothing to refuse means nothing to force: a parameter there is a false claim."""
    descriptor = json.loads((_CANONICAL_DIR / "openai.json").read_text("utf-8"))
    lever = descriptor["enforcement"]["retention.conversation"]["params"]
    descriptor["enforcement"]["memory"] = {
        "level": "absent",
        "note": "Memory in an API app is built by the developer, not the provider.",
        "params": lever,
    }
    with pytest.raises(ValidationError):
        _validator().validate(descriptor)


def test_a_contractual_entry_must_say_which_way_its_terms_decide() -> None:
    """`protective` and `level: contractual` imply each other, both ways.

    Without the rule a contractual entry nobody classified would validate and
    read as either direction, and the direction is what decides whether a
    consent helper may call the option guaranteed.
    """
    descriptor = json.loads((_CANONICAL_DIR / "openai.json").read_text("utf-8"))
    entry = descriptor["enforcement"]["training.foundation"]
    assert entry["level"] == "contractual"

    for value in (True, False):
        entry["protective"] = value
        _validator().validate(descriptor)

    del entry["protective"]
    with pytest.raises(ValidationError):
        _validator().validate(descriptor)

    entry["protective"] = "no"
    with pytest.raises(ValidationError):
        _validator().validate(descriptor)

    entry["protective"] = False
    descriptor["enforcement"]["data_residency"]["protective"] = False
    assert descriptor["enforcement"]["data_residency"]["level"] == "account"
    with pytest.raises(ValidationError):
        _validator().validate(descriptor)


def test_only_an_account_entry_may_be_routable() -> None:
    """`routable` turns a disclosure into a question once an app holds two credentials.

    Only an account-level setting has a posture a second credential can carry.
    On an entry the terms decide, one with nothing to refuse or one nothing can
    honour, `routable` would have a consent dialog ask users to refuse what no
    credential stops — Gemini and Mistral `human_review` said exactly that
    until their level was corrected, and only a hand edit removed it.
    """
    descriptor = json.loads((_CANONICAL_DIR / "gemini.json").read_text("utf-8"))
    assert descriptor["enforcement"]["training.foundation"]["level"] == "account"
    assert descriptor["enforcement"]["training.foundation"]["routable"] is True
    _validator().validate(descriptor)

    for option in ("human_review", "memory", "location", "agentic_tools"):
        candidate = json.loads(json.dumps(descriptor))
        assert candidate["enforcement"][option]["level"] != "account", option
        candidate["enforcement"][option]["routable"] = True
        with pytest.raises(ValidationError):
            _validator().validate(candidate)


def test_bundled_descriptors_expose_descriptor_version() -> None:
    descriptors = load_descriptors()
    for provider in ("openai", "anthropic", "gemini", "mistral", "deepseek"):
        assert descriptors[provider].descriptor_version == "2"
