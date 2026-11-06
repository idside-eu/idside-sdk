"""The published privacy matrix must match the descriptors it describes.

`docs/providers-privacy-matrix.md` is the document a customer's DPO reads, and
the one they will quote back to a regulator. Hand-maintaining it would let it
drift from what the code actually enforces, and the gap would stay invisible
until exactly the wrong moment. So it is generated, and this test fails the
build when the checked-in file no longer matches its source.

If this fails, do not edit the Markdown: run
`python packages/descriptors/build_matrix.py`.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_BUILDER = _ROOT / "packages" / "descriptors" / "build_matrix.py"
_DOC = _ROOT / "docs" / "providers-privacy-matrix.md"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_matrix", _BUILDER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_matrix"] = module
    spec.loader.exec_module(module)
    return module


def test_the_published_matrix_is_in_sync_with_the_descriptors() -> None:
    builder = _load_builder()

    assert _DOC.read_text(encoding="utf-8") == builder.render(*builder.load()), (
        "docs/providers-privacy-matrix.md is out of date. Do not edit it by hand — "
        "run `python packages/descriptors/build_matrix.py`."
    )


def test_a_provider_with_no_lever_is_not_said_to_have_a_contract() -> None:
    """No descriptor lacks a lever today, so a synthetic one does.

    DeepSeek's levels without its tool refusal are `unsupported` and `absent`:
    neither terms nor an account setting. A sentence calling every choice
    contractual or account-level there would be the one false line in its
    section.
    """
    builder = _load_builder()
    catalogue, descriptors = builder.load()
    descriptors = copy.deepcopy(descriptors)
    deepseek = next(d for d in descriptors if d["name"] == "deepseek")
    deepseek["enforcement"]["agentic_tools"] = {
        "level": "unsupported",
        "note": "Synthetic: no lever.",
    }

    page = builder.render(catalogue, descriptors)
    section = page.split("### Deepseek\n", 1)[1].split("</details>", 1)[0]

    assert f"**Technically enforced (0/{len(catalogue['options'])})**" in section
    assert "contractual or account-level" not in section
    assert "- Nothing." in section


def test_every_entry_without_a_lever_is_listed_under_its_provider() -> None:
    """A level the builder does not iterate is a reason the DPO never reads.

    The provider sections list every non-enforced entry with its note, level by
    level. A level missing from that walk drops its entries silently: the matrix
    cell still shows the badge, and the sentence saying why is gone.
    """
    builder = _load_builder()
    catalogue, descriptors = builder.load()
    page = builder.render(catalogue, descriptors)

    assert set(builder.LEVEL_ORDER) == set(builder.LEVEL_BADGE)
    assert set(builder.LEVEL_ORDER) == set(catalogue["enforcement_levels"])
    for descriptor in descriptors:
        section = page.split(f"### {descriptor['name'].capitalize()}\n", 1)[1]
        section = section.split("</details>", 1)[0]
        for option, entry in descriptor["enforcement"].items():
            if entry["level"] != "enforced":
                assert f"- `{option}` — " in section, (
                    f"{descriptor['name']}/{option} ({entry['level']}) is not "
                    "explained in its provider's section"
                )


def test_the_matrix_never_claims_enforcement_without_a_lever() -> None:
    """The one sentence in that document that must never be wrong.

    A DPO reading `enforced` will tell their regulator the control is technical.
    If it turns out to be a contract, that is our fault and their problem.
    """
    builder = _load_builder()
    _, descriptors = builder.load()

    for descriptor in descriptors:
        for option, entry in descriptor["enforcement"].items():
            if entry["level"] != "enforced":
                continue
            assert entry.get("params") or entry.get("mechanism"), (
                f"{descriptor['name']}/{option} is published as enforced "
                "with nothing behind it"
            )
            for param in entry.get("params", []):
                assert param["call_sites"], (
                    f"{descriptor['name']}/{option} enforces {param['name']} on "
                    "no endpoint at all, which enforces it nowhere"
                )
                # A parameter name is not a lever. What makes it one is the
                # answer that triggers it and the value it carries — the half
                # that used to live only in the cloud's rule table, and drifted
                # from this file until three options were asked and forced
                # nothing. The schema pins both (`when` is a boolean, `value` is
                # anything but null); this repeats it because the schema is
                # applied by a different test, over a different file set, and
                # the claim these two assertions defend — `enforced` means
                # something forces something — is the one a DPO repeats to a
                # regulator.
                assert isinstance(param["when"], bool), (
                    f"{descriptor['name']}/{option}: {param['name']} does not say "
                    "which answer acts on it"
                )
                assert param["value"] is not None, (
                    f"{descriptor['name']}/{option}: {param['name']} has no value "
                    "to force, so declaring it enforced promises nothing"
                )


def test_a_contractual_cell_says_which_way_the_terms_decide() -> None:
    """`📄 contractual` alone reads as a promise in the user's favour.

    It would be one only beside terms that rule the processing out for every
    call, and no provider's do today: beside the review of flagged content
    every provider's terms allow, or a no-training clause the customer's
    organization can opt out of, it says the opposite of those terms. The cell
    carries the direction, so a DPO never has to open the note to learn which
    of the two they are looking at.
    """
    builder = _load_builder()
    catalogue, descriptors = builder.load()
    rows = _matrix_rows(builder.render(catalogue, descriptors))

    for column, descriptor in enumerate(descriptors):
        for option, entry in descriptor["enforcement"].items():
            if entry["level"] != "contractual":
                continue
            cell = rows[option][column]
            expected = (
                builder.RULED_OUT if entry["protective"] else builder.NOT_RULED_OUT
            )
            assert expected in cell, f"{descriptor['name']}/{option}: {cell}"


def test_terms_that_rule_the_processing_out_are_said_so_in_the_cell() -> None:
    """No descriptor says `protective: true` today, so a synthetic one does.

    The schema admits it, so the day a provider earns it the cell must say so —
    and a branch no data exercises is one nobody would see break.
    """
    builder = _load_builder()
    catalogue, descriptors = builder.load()
    descriptors = copy.deepcopy(descriptors)
    column = next(i for i, d in enumerate(descriptors) if d["name"] == "anthropic")
    entry = descriptors[column]["enforcement"]["training.foundation"]
    assert entry["level"] == "contractual"
    entry["protective"] = True

    cell = _matrix_rows(builder.render(catalogue, descriptors))["training.foundation"][
        column
    ]

    assert builder.RULED_OUT in cell
    assert builder.NOT_RULED_OUT not in cell


def _matrix_rows(page: str) -> dict[str, list[str]]:
    """The matrix table, option by option, one cell per provider column."""
    matrix = page.split("## The matrix\n", 1)[1].split("\n## ", 1)[0]
    return {
        line.split("`")[1]: line.split(" | ")[1:]
        for line in matrix.splitlines()
        if line.startswith("| **`")
    }


def test_the_document_carries_the_caveats_that_make_it_honest() -> None:
    """Three truths a reader must not be able to miss."""
    text = _DOC.read_text(encoding="utf-8")

    assert "No absolute zero" in text
    assert "Tools reintroduce retention" in text
    assert "Contracts are the real guardrail" in text
