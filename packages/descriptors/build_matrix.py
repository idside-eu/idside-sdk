#!/usr/bin/env python3
"""Generate `docs/providers-privacy-matrix.md` from the descriptors.

Hand-written documentation about what we enforce would drift from what we
actually enforce, and the gap would be invisible until a customer repeated a
claim of ours to their regulator. So the document is generated, and
`packages/python/tests/test_privacy_matrix_doc.py` fails the build when the
checked-in file no longer matches its source.

Run: `python packages/descriptors/build_matrix.py`
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent.parent / "docs" / "providers-privacy-matrix.md"

LEVEL_BADGE = {
    "enforced": "✅ **enforced**",
    "contractual": "📄 contractual",
    "account": "⚙️ account-level",
    "absent": "➖ absent",
    "unsupported": "❌ unsupported",
}
LEVEL_ORDER = ["enforced", "contractual", "account", "absent", "unsupported"]
#: Which way a `contractual` entry's terms decide (`protective`). The badge alone
#: read as a promise in the user's favour, beside terms that allow the review of
#: flagged content as much as beside terms that rule training out.
RULED_OUT = "*ruled out by the terms*"
NOT_RULED_OUT = "*terms do not rule it out*"


def escape_cell(text: str) -> str:
    """Escape what would otherwise end a Markdown table cell — or the whole row.

    A `|` ends a cell and backticks do not protect it in GFM, so a model glob
    (`gpt-5|o1*`) or a `conditions` sentence carrying one would silently grow
    the row a column. A newline is the worse version of the same bug: it ends
    the ROW, and the rest of the prose becomes a paragraph under the table.
    Every string entering a cell goes through here — escaping only the globs
    left the neighbouring prose one `|` away from the same bug, and the levels
    and endpoint tables one `|` away from it too.
    """
    return text.replace("|", "\\|").replace("\n", "<br>")


def as_list_item(text: str, indent: str) -> str:
    """Keep a multi-paragraph note inside the list item that introduces it.

    A `note` is prose, and several of them run to more than one paragraph.
    Written verbatim into a bullet, the second paragraph starts at column 0,
    which ENDS the list: the sibling bullet after it opens a new top-level one
    and the reader loses which parameter — or which provider — the text was
    about. Continuation lines are indented to the item's content column
    instead, which is what keeps them inside it.

    Blank lines stay genuinely blank. Indenting them would leave trailing
    whitespace that the pre-commit hook strips, so the generated file and its
    source would disagree on every run.
    """
    return "\n".join(
        line if i == 0 or not line else indent + line
        for i, line in enumerate(text.split("\n"))
    )


def model_scope(param: dict) -> str | None:
    """Where this parameter is NOT forced, in one sentence, or `None`.

    A model the parameter is skipped on is a call where the option is NOT
    enforced, whatever the badge says. Leaving that to the prose `conditions`
    let the document show a ✅ for a request nothing acts on.

    One function for both renderings on purpose: they said different things
    when they were two, and the denylist branch said something false — that the
    parameter was forced nowhere at all.
    """
    models = param.get("models") or {}
    if models.get("only"):
        return (
            f"forced ONLY on models matching `{models['only']}` — anywhere "
            "else, including any model released after this list was written, "
            "the call goes out unforced and this option is not enforced there"
        )
    if models.get("except"):
        return (
            f"forced everywhere EXCEPT models matching `{models['except']}` — "
            "those calls go out unforced and this option is not enforced for "
            "them"
        )
    return None


def load() -> tuple[dict, list[dict]]:
    catalogue = json.loads((HERE / "consent-options.json").read_text(encoding="utf-8"))
    descriptors = sorted(
        (
            json.loads(p.read_text(encoding="utf-8"))
            for p in (HERE / "descriptors").glob("*.json")
        ),
        key=lambda d: d["name"],
    )
    return catalogue, descriptors


def render(catalogue: dict, descriptors: list[dict]) -> str:
    options = {o["name"]: o for o in catalogue["options"]}
    groups = {g["name"]: g for g in catalogue["groups"]}
    names = [d["name"] for d in descriptors]

    out: list[str] = []
    w = out.append

    w("# What each AI provider actually lets you control")
    w("")
    w("> **Generated from `packages/descriptors/` — do not edit by hand.**")
    w(
        "> Run `python packages/descriptors/build_matrix.py` after changing a descriptor."
    )
    w("> A test fails the build if this file and the descriptors disagree.")
    w("")
    w('This is the heart of the product: the honest answer to *"if a user refuses')
    w('this, what actually happens?"* — provider by provider, option by option.')
    w("")

    w("## How to read the levels")
    w("")
    w("| | Meaning |")
    w("| --- | --- |")
    for level in LEVEL_ORDER:
        w(
            f"| {LEVEL_BADGE[level]} | "
            f"{escape_cell(catalogue['enforcement_levels'][level])} |"
        )
    w("")

    w("## Three things that are true of every provider")
    w("")
    for key, text in catalogue["hard_truths"].items():
        w(f"**{key.replace('_', ' ').capitalize()}.** {text}")
        w("")

    w("## The matrix")
    w("")
    w("| Option | " + " | ".join(n.capitalize() for n in names) + " |")
    w("| --- | " + " | ".join("---" for _ in names) + " |")
    for option_name, option in options.items():
        cells = []
        for d in descriptors:
            entry = d["enforcement"][option_name]
            cell = LEVEL_BADGE[entry["level"]]
            if entry.get("params"):
                cell += "<br>" + ", ".join(f"`{p['name']}`" for p in entry["params"])
            elif entry.get("mechanism"):
                cell += "<br>*SDK refuses the tool*"
            elif entry["level"] == "contractual":
                cell += "<br>" + (RULED_OUT if entry["protective"] else NOT_RULED_OUT)
            for param in entry.get("params", []):
                if param.get("conditions"):
                    cell += (
                        f"<br><sub>`{param['name']}`: "
                        f"{escape_cell(param['conditions'])}</sub>"
                    )
                scope = model_scope(param)
                if scope:
                    cell += f"<br><sub>`{param['name']}`: {escape_cell(scope)}</sub>"
            cells.append(cell)
        w(f"| **`{option_name}`** | " + " | ".join(cells) + " |")
    w("")

    w("## What the user is asked")
    w("")
    w("Options are grouped by **purpose, not by provider**: a person decides what may")
    w("be done with their data, not which vendor may do it. When an app uses several")
    w("providers, the widget shows the union of the relevant options — once each.")
    w("")
    for group_name, group in groups.items():
        in_group = [o for o in options.values() if o["group"] == group_name]
        if not in_group:
            continue
        w(f"### {group['label_en']} — *{group['label_fr']}*")
        w("")
        for option in in_group:
            w(f"#### `{option['name']}`")
            w("")
            # `<br>` rather than two trailing spaces: the trailing-whitespace
            # pre-commit hook strips those, so the generated file and its source
            # disagreed on every run and the sync test failed for a reason that
            # had nothing to do with the descriptors.
            w(f"> **{option['label_en']}**<br>")
            w(f"> *{option['label_fr']}*")
            w("")
            w(option["description_en"])
            w("")
            default = (
                "refused" if option["default"] is False else str(option["default"])
            )
            w(
                f"- **Default**: {default} — an option nobody has answered is never assumed"
            )
            w(f"- **Legal basis**: {option['legal_basis']} · {option['gdpr']}")
            if option.get("rationale"):
                w(f"- **Why it is its own option**: {option['rationale']}")
            w("")

    w("## Provider by provider")
    w("")
    for d in descriptors:
        enforced = [
            (name, entry)
            for name, entry in d["enforcement"].items()
            if entry["level"] == "enforced"
        ]
        w(f"### {d['name'].capitalize()}")
        w("")
        w(f"- **Client**: `{d['client_module']}`")
        w(f"- **Consent parameters injected into**: {d.get('param_target', 'n/a')}")
        w(
            "- **Never overridden** (protected): "
            + ", ".join(f"`{p}`" for p in d.get("protected_params", []))
        )
        w(f"- **Verified**: {d.get('enforcement_verified_at', 'n/a')}")
        w("")
        w("**Endpoints wrapped, and what can be forced on each**")
        w("")
        w("| Endpoint | Can force |")
        w("| --- | --- |")
        for site in d["call_sites"]:
            forceable = site.get("forceable_params") or []
            can = ", ".join(f"`{p}`" for p in forceable) if forceable else "— nothing"
            w(f"| `{escape_cell(site['id'])}` | {escape_cell(can)} |")
        w("")
        w(
            "Two endpoints of one provider do not accept the same parameters, so the "
            "allowlist is per endpoint. The SDK tells the cloud which one it is about "
            "to call, and the cloud decides accordingly — forcing a parameter where it "
            "does not exist would break the call for exactly the users who opted out, "
            "and for nobody else."
        )
        w("")
        w(f"**Technically enforced ({len(enforced)}/{len(options)})**")
        w("")
        if enforced:
            for name, entry in enforced:
                if entry.get("params"):
                    lever = ", ".join(f"`{p['name']}`" for p in entry["params"])
                else:
                    lever = "the SDK refuses the tool"
                w(f"- **`{name}`** via {lever}")
                for param in entry.get("params", []):
                    where = ", ".join(f"`{s}`" for s in param["call_sites"])
                    w(f"  - `{param['name']}` on {where}")
                    if param.get("conditions"):
                        w(f"    - Only when: {param['conditions']}")
                    scope = model_scope(param)
                    if scope:
                        w(f"    - {scope[:1].upper()}{scope[1:]}")
                    if param.get("note"):
                        w(f"    - {as_list_item(param['note'], '      ')}")
                w(f"  - {as_list_item(entry['note'], '    ')}")
        else:
            w(
                "- Nothing. No option here has a lever the SDK applies per "
                "request; each one is listed below with its level."
            )
        w("")
        w("<details><summary>Everything else, and why</summary>")
        w("")
        # Every level but `enforced`, from the one list: a level left out of
        # this walk would keep its badge in the matrix and lose its reason here.
        for level in (lvl for lvl in LEVEL_ORDER if lvl != "enforced"):
            rows = [(n, e) for n, e in d["enforcement"].items() if e["level"] == level]
            if not rows:
                continue
            w(f"**{LEVEL_BADGE[level]}**")
            w("")
            for name, entry in rows:
                w(f"- `{name}` — {as_list_item(entry['note'], '  ')}")
            w("")
        w("</details>")
        w("")

    w("---")
    w("")
    w(
        f"Catalogue version **{catalogue['catalogue_version']}** · "
        f"{len(options)} options · {len(descriptors)} providers"
    )
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(*load()), encoding="utf-8")
    print(f"wrote {OUT}")
