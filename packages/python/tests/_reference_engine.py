"""Load the frozen reference engine kept in the shared test data.

`packages/descriptors/testdata/reference-engine/` holds an earlier engine and
its catalogue, never edited (their SHA-256 are pinned in
`test_content_oracle.py`). Its modules import one another as
`idside._cleaning.…`; they are loaded here, from their own bytes, under another
package name, so that they never mix with the engine under test.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

ROOT = (
    Path(__file__).resolve().parents[2]
    / "descriptors"
    / "testdata"
    / "reference-engine"
)
_PACKAGE = "idside_reference_engine"
#: In import order: each module only imports those before it.
MODULES = ("rules", "shadow", "validators", "policy", "engine")


def load() -> types.ModuleType:
    """The reference engine module (loaded once)."""
    if f"{_PACKAGE}.engine" in sys.modules:
        return sys.modules[f"{_PACKAGE}.engine"]
    package = types.ModuleType(_PACKAGE)
    package.__path__ = []
    sys.modules[_PACKAGE] = package
    for name in MODULES:
        path = ROOT / "python" / f"{name}.py"
        source = path.read_text(encoding="utf-8").replace(
            "from idside._cleaning.", f"from {_PACKAGE}."
        )
        module = types.ModuleType(f"{_PACKAGE}.{name}")
        module.__file__ = str(path)
        sys.modules[module.__name__] = module
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)  # noqa: S102 - the frozen files of this repository
        setattr(package, name, module)
    return sys.modules[f"{_PACKAGE}.engine"]


def cleaner(policy: Any) -> Any:
    """A reference `ContentCleaner` for ``policy``."""
    return load().ContentCleaner(policy)
