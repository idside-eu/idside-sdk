"""The shared custom-entry vectors: which entries a policy may hold.

`packages/descriptors/testdata/content-entry-vectors.json` says, for one custom
entry alone, whether compiling a policy that holds it succeeds or raises
`ContentPolicyError`. The Node suite holds the same verdicts, and the ID Side
cloud reads the same file: it must never sign an entry an SDK refuses, or every
call of that application would fail closed. The verdicts come from
`build_entry_vectors.py`, which states the rules on its own, never from an SDK.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from idside._cleaning import ContentCleaner
from idside.exceptions import ContentPolicyError

_DESCRIPTORS = Path(__file__).resolve().parents[2] / "descriptors"
_PATH = _DESCRIPTORS / "testdata" / "content-entry-vectors.json"
_VECTORS: dict[str, Any] = json.loads(_PATH.read_text(encoding="utf-8"))


def _compiles(vector: dict[str, Any]) -> bool:
    entry = {
        "kind": vector["kind"],
        "value": vector["value"],
        "action": "mask",
        "label": vector.get("label", "CASE"),
    }
    try:
        ContentCleaner({"schema": 1, "custom": [entry]})
    except ContentPolicyError:
        return False
    return True


@pytest.mark.parametrize(
    "vector",
    _VECTORS["vectors"],
    ids=[f"{v['kind']}: {v['note']}" for v in _VECTORS["vectors"]],
)
def test_entry_vector(vector: dict[str, Any]) -> None:
    assert _compiles(vector) is vector["accepted"]


def test_entry_vectors_cover_both_verdicts_and_every_kind() -> None:
    assert _VECTORS["format"] == 1
    seen = {(v["kind"], v["accepted"]) for v in _VECTORS["vectors"]}
    assert seen == {
        (k, a) for k in ("term", "domain", "pattern") for a in (True, False)
    }


def test_entry_vectors_file_matches_its_generator() -> None:
    builder = _DESCRIPTORS / "build_entry_vectors.py"
    spec = importlib.util.spec_from_file_location("build_entry_vectors", builder)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_entry_vectors"] = module
    spec.loader.exec_module(module)
    assert module.render() == _PATH.read_text(encoding="utf-8"), (
        "content-entry-vectors.json is stale; "
        "run `python packages/descriptors/build_entry_vectors.py`."
    )
