#!/usr/bin/env bash
#
# Nothing in this repository may claim it is open source, and no draft of the
# licence may be published by accident.
#
# Both mistakes are one-way. A licence reaches a recipient the moment they can
# read it, and cannot be taken back from them afterwards — so a file that says
# "Apache-2.0" for one day hands out that licence's irrevocable patent grant
# for every version published while it said so. The same is true of a working
# draft from counsel: publish one and it becomes the operative terms for anyone
# who relied on it, and the privilege attached to it is gone.
#
# Neither mistake shows up in a test suite. The code runs identically under any
# licence, which is exactly why this is a check and not a unit test.
#
# What it enforces:
#
#   1. No tracked file names the licence this repository used to carry.
#   2. Nothing asserts that this repository is open source, open-core or
#      OSI-approved. Saying it is NOT is the point of several documents, so the
#      rule is about the assertion rather than the words: an assertion must be
#      negated inside its own clause, and a paragraph that uses the terms must
#      carry a real denial somewhere.
#   3. The package manifests carry the metadata a registry displays as fact.
#   4. LICENSE and NOTICE are byte-identical in the root and in both packages.
#      `check_publishable.sh` runs this script first and relies on it for that.
#   5. LICENSE is a text somebody may rely on and not a draft of one: it
#      carries a dated version line, its bytes are the installed rendering of
#      counsel's text — pinned by a SHA-256 fingerprint — and no tracked file
#      carries any of the markings counsel puts on a working copy.
#   6. NOTICE identifies the patents this repository asserts. A patent notice
#      reads as a complete list whether or not it is one, so a number missing
#      from it is worse than a notice nobody wrote.
#   7. The virtual patent marking — "Patents: https://sdk.idside.eu/patents" —
#      is where 35 U.S.C. §287(a) and clause 5 of LICENSE expect it: at the top
#      of every tracked source file of both SDKs, in the three READMEs, in
#      NOTICE, and in the metadata each registry displays.
#
# Run: ./scripts/check_licence_claims.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$ROOT" <<'PY'
# Annotations stay unevaluated, so `str | None` does not stop an interpreter
# older than 3.10 — macOS still ships 3.9 as `python3` — before a single check
# has run. The `tomllib` fallback below exists for the same interpreters.
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
failures: list[str] = []


def pass_(message: str) -> None:
    print(f"  ✅ {message}")


def fail(message: str) -> None:
    failures.append(message)
    print(f"  ❌ {message}")


_unreadable: set[str] = set()
# Kept apart from `_unreadable`: a file that is not UTF-8 still has bytes, and
# the checks that only need those — the copies, the fingerprint — still run on
# it. Folded into one set, a LICENSE re-encoded by an editor lost exactly the
# check that exists to catch that.
_undecodable: set[str] = set()


def read_bytes(relative: str) -> bytes | None:
    """Read a file the checks depend on, or name its absence ONCE and carry on.

    A missing NOTICE is precisely what this script exists to catch — it is the
    file that identifies the patents and says that, beyond the limited
    evaluation patent licence of clause 2 of LICENSE, rights under them are
    conveyed only by an executed commercial licence. It must never go missing
    from an artifact. Letting the read raise turned that into a Python traceback
    that named no file and, worse, stopped every check below it from running.
    LICENSE and NOTICE are each read by more than one check, so the absence is
    remembered: reported three times, one missing file read as three failures.
    """
    if relative in _unreadable:
        return None
    try:
        return (root / relative).read_bytes()
    except OSError:
        _unreadable.add(relative)
        fail(
            f"{relative} cannot be read, so it is missing from what ships.\n"
            "     Every check that reads it was skipped."
        )
        return None


def read_text(relative: str) -> str | None:
    """`read_bytes`, decoded — and a file that is not UTF-8 is named, not raised."""
    if relative in _undecodable:
        return None
    data = read_bytes(relative)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        _undecodable.add(relative)
        fail(
            f"{relative} is not UTF-8 ({error.reason} at byte {error.start}).\n"
            "     Every check that reads it as text was skipped."
        )
        return None


def python_manifest(toml_text: str) -> tuple[object, list[str]] | None:
    """`project.license` and the classifiers, as DATA.

    Quoting is not the check's business, and neither is a comment: a substring
    test accepted `license = "MIT"` sitting under a commented-out line carrying
    the right value, and the check printed that the manifest says what PyPI will
    display. None when the file does not parse, which is reported here.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        # Best effort. Comments go first — the one that EXPLAINS the classifier
        # rule is not a breach of it, and a commented-out `license` line is not
        # a licence — and both quote styles count. Broader than the parsed form
        # on purpose: any `License ::` string anywhere is read as a classifier.
        # The licence is read from the `[project]` table only: the first
        # `license =` anywhere in the file can sit in a `[tool.*]` table above
        # it while `[project]` says "MIT".
        text = re.sub(r"#[^\n]*", "", toml_text)
        table = re.search(r"^\[project\][^\n]*\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
        licence = (
            re.search(
                r"^[ \t]*license[ \t]*=[ \t]*(\"\"\"|'''|\"|')(.*?)\1",
                table.group(1),
                re.M | re.S,
            )
            if table is not None
            else None
        )
        return (
            licence.group(2) if licence else None,
            re.findall(r"['\"](License :: [^'\"]*)['\"]", text),
        )
    try:
        project = tomllib.loads(toml_text).get("project", {})
    except tomllib.TOMLDecodeError as error:
        fail(f"packages/python/pyproject.toml does not parse: {error}")
        return None
    if not isinstance(project, dict):
        return (None, [])
    declared = project.get("classifiers", [])
    return (
        project.get("license"),
        [c for c in declared if isinstance(c, str)] if isinstance(declared, list) else [],
    )


def project_urls(toml_text: str) -> dict[str, str]:
    """`[project.urls]` as DATA, for the same reason as `python_manifest()`."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        # Best effort: the table from its header to the next one, comments
        # stripped, either quote style.
        table = re.search(
            r"^\[project\.urls\][^\n]*\n(.*?)(?=^\[|\Z)",
            re.sub(r"#[^\n]*", "", toml_text),
            re.M | re.S,
        )
        if table is None:
            return {}
        return dict(
            re.findall(
                r"^\s*['\"]?([\w.-]+)['\"]?\s*=\s*['\"]([^'\"]*)['\"]",
                table.group(1),
                re.M,
            )
        )
    try:
        project = tomllib.loads(toml_text).get("project", {})
    except tomllib.TOMLDecodeError:
        return {}  # already reported by python_manifest(), which parses it first
    # Legal TOML need not hold a table here, and a traceback would stop the
    # report: the shapes are checked, as `python_manifest()` checks them.
    urls = project.get("urls", {}) if isinstance(project, dict) else {}
    if not isinstance(urls, dict):
        return {}
    return {str(key): str(value) for key, value in urls.items()}


# This file quotes every term it forbids, so it cannot be one of the files it
# reads. Nothing else is exempt.
SELF = "scripts/check_licence_claims.sh"

# A lockfile records the terms third parties chose for their own code. It makes
# no claim about this repository, and editing one to satisfy a check here would
# make it disagree with the registry it was resolved from.
THIRD_PARTY = re.compile(r"(^|/)(package-lock\.json|uv\.lock|poetry\.lock)$")

tracked = subprocess.run(
    ["git", "-C", str(root), "ls-files", "-z"],
    capture_output=True,
    check=True,
    text=True,
).stdout.split("\0")

documents: dict[str, str] = {}
for name in tracked:
    if not name or name == SELF:
        continue
    path = root / name
    if not path.is_file():  # a submodule, or a symlink pointing out of the tree
        continue
    try:
        documents[name] = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue  # binary, or unreadable: it makes no claims

# ── 1. The previous licence is named nowhere ───────────────────────────────
# A hard ban rather than the assertion rule below, because there is no sentence
# this repository needs that name for. The history of the change belongs in the
# changelog of the release that carries it, and no release carries one: nothing
# was ever published under those terms.
FORMER = re.compile(r"Apache", re.IGNORECASE)
named = sorted(
    name
    for name, text in documents.items()
    if FORMER.search(text) and not THIRD_PARTY.search(name)
)
if named:
    fail(
        "the licence this repository used to carry is named in: "
        + ", ".join(named)
        + "\n     Remove the name. If a dependency genuinely needs crediting, "
        "that belongs\n     in a third-party notice file, and this check needs "
        "changing deliberately."
    )
else:
    pass_("no tracked file names the previous licence")

# ── 2. Nothing asserts this repository is open source ──────────────────────
# Two layers, because one was not enough and the way it failed is instructive.
#
# The first version looked for the terms anywhere in a paragraph and accepted
# the paragraph if a denial appeared anywhere else in it. That is defeated by a
# denial that denies something else: "This SDK is open source, and there is no
# charge for using it" passed, because `no charge` counted as the denial. The
# sentence a check like this exists to stop went straight through it.
#
# So the denial now has to be in the right PLACE, not merely in the vicinity.
# And the place is the CLAUSE, not the sentence: a sentence-wide window let the
# same defeat back in reversed — "There is no charge for using it, and this SDK
# is open source" — and let a bullet borrow the negation of the bullet above it.
#
# `OSI` stays case-sensitive and word-bounded throughout: lowercased and
# unbounded it is a substring of "repository" and "exposing", and a check that
# cries wolf on every test file is a check people learn to skip.

# Layer one: the shape of an assertion. Where the text says something IS open
# source, the negation has to sit BEFORE it — between the start of its clause
# and the claim — because that is the only position from which a negation can
# reach it. Anything after it, or across a comma, is talking about something
# else.
ASSERTION = re.compile(
    r"\b(?:is|are|was|were|being|becomes?)\s+(?:an?\s+)?"
    r"(?:open[ _-]?source|open[ _-]?core)\b"
    r"|\bopen[ _-]?source\s+"
    r"(?:project|repository|repo|software|sdk|library|licen[cs]e|release)\b"
    r"|(?-i:\bOSI\b)[- ]approved\b",
    re.IGNORECASE,
)
# `nor` because a comma ends a clause: "is not open-source software, nor
# OSI-approved" carries its negation into the second clause with that word.
NEGATOR = re.compile(
    r"\bnot\b|\bno\b|\bnone\b|\bnothing\b|\bneither\b|\bnor\b|\bnever\b|n't",
    re.IGNORECASE,
)

# Layer two: the terms at all, denied somewhere in the paragraph. Broader and
# blunter, and it stays because layer one only knows the assertions somebody
# thought to enumerate. `\bno\b` is deliberately absent from this one — it is
# what let "no charge" through — so a paragraph here must carry a real denial.
CLAIM = re.compile(
    r"\bopen[ _-]?source\b|\bopen[ _-]?core\b|(?-i:\bOSI\b)", re.IGNORECASE
)
PARAGRAPH_DENIAL = re.compile(
    r"\bnot\b|\bnone\b|\bnothing\b|\bneither\b|\bnever\b|n't", re.IGNORECASE
)

# A heading and a table-of-contents entry ask the question the paragraph below
# them answers. "Is this open source?" is the entry a developer looks for, and
# moving the denial into the title would hide the answer from the person
# searching for the question.
INTERROGATIVE = re.compile(r"^\s*(#{1,6} .*\?|[-*] \[[^\]]*\]\(#[^)]*\))\s*$")


# Where a clause ends. Punctuation, and a line break only where the next line
# starts a new block: a list item or a table row. A wrapped line of prose does
# NOT end one — "is NOT published under an Open Source Initiative (OSI) /
# approved open-source license" puts the negation and the thing it negates on
# different lines, and stopping at every newline reported LICENSE itself. A
# colon ends a clause only as prose punctuation: `License :: OSI Approved` is a
# classifier path, and the negation before it still reaches it.
CLAUSE_END = re.compile(
    r"[.!?;,]|(?<!:):(?=\s)|\n(?=[ \t]*(?:[-*+][ \t]|\d+[.)][ \t]|\|))"
)
# In Markdown a heading is a block of its own even with no blank line after it:
# "## Not included" must not lend its negation to the line below. A heading
# that asks the question stays with its answer, though: alone it is exempt as
# INTERROGATIVE, and "## Is this open source?" over a bare "Yes." would then
# be read as nothing at all.
MARKDOWN_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t][^\n]*\n", re.M)
# A JSDoc continuation line opens with ` * `, which CLAUSE_END would read as a
# list item, so a denial wrapped inside a doc comment would stop at the wrap.
# In JavaScript and TypeScript the leader is dropped before reading: what it
# leaves is the comment's prose, wrapped as prose is everywhere else.
COMMENT_LEADER = re.compile(r"^([ \t]*)\*(?=[ \t]|$)", re.M)
SCRIPT_SOURCE = re.compile(r"\.(?:[cm]?[jt]s|[jt]sx)$")


def clause_opening(paragraph: str, position: int) -> int:
    opening = 0
    for end in CLAUSE_END.finditer(paragraph, 0, position):
        opening = end.end()
    return opening


def undenied_assertions(paragraph: str) -> list[str]:
    """Every assertion with no negation between it and the start of its clause.

    Reported as the text leading up to the claim, so the words that made it a
    claim are in the report however long the clause is.
    """
    found = []
    for match in ASSERTION.finditer(paragraph):
        opening = clause_opening(paragraph, match.start())
        if not NEGATOR.search(paragraph[opening : match.start()]):
            clause = " ".join(paragraph[opening : match.end()].split())
            found.append(clause if len(clause) <= 88 else "…" + clause[-87:])
    return found


asserted: list[str] = []
for name, text in documents.items():
    if THIRD_PARTY.search(name):
        continue
    if name.endswith(".md"):
        # A blank line after each heading that is not a question, so it is a
        # paragraph of its own.
        text = MARKDOWN_HEADING.sub(
            lambda heading: heading.group(0)
            + ("" if INTERROGATIVE.match(heading.group(0)) else "\n"),
            text,
        )
    elif SCRIPT_SOURCE.search(name):
        text = COMMENT_LEADER.sub(r"\1", text)
    # Every finding, not the first per file: a document can be wrong in more
    # than one place, and a report that stops at one sends somebody round the
    # loop once per sentence.
    for paragraph in re.split(r"\n\s*\n", text):
        claims = undenied_assertions(paragraph)
        if claims:
            asserted.extend(f"{name}: {claim}" for claim in claims)
            continue
        lines = [line for line in paragraph.splitlines() if line.strip()]
        if lines and all(INTERROGATIVE.match(line) for line in lines):
            continue
        if CLAIM.search(paragraph) and not PARAGRAPH_DENIAL.search(paragraph):
            asserted.append(f"{name}: {lines[0].strip()[:88]}")
if asserted:
    fail(
        "these read as a claim that this repository is open source:\n     "
        + "\n     ".join(sorted(asserted))
        + "\n     Say what it is instead — source-available — or deny the claim "
        "where it\n     stands, the way LICENSE and docs/ARCHITECTURE.md do."
    )
else:
    pass_("nothing claims this repository is open source, open-core or OSI-approved")

# ── 3. What the registries display as fact ─────────────────────────────────
# Three independent questions, deliberately not an `elif` chain. Chained, the
# one that matters most — the OSI classifier, which asserts an approval on the
# page people install from — was skipped whenever a smaller problem failed above
# it. A manifest can be wrong in more than one way at once, and the report has
# to say so in one run.
pyproject = read_text("packages/python/pyproject.toml")
# Read as DATA, not as text. The previous version was a regex anchored on
# double quotes at the start of a line, so `'License :: OSI Approved :: …'` —
# legal TOML, and what a formatter may produce — went straight past it, and the
# check printed that the manifest says what PyPI will display while the
# manifest asserted an approval these terms do not have.
parsed = python_manifest(pyproject) if pyproject is not None else None
problems: list[str] = []

if parsed is not None:
    declared_licence, declared = parsed
    if declared_licence != "LicenseRef-IDSide-Source-Available":
        problems.append(
            'it must carry license = "LicenseRef-IDSide-Source-Available" (it '
            f"says {declared_licence!r}).\n     There is no SPDX identifier for "
            "these terms, and `LicenseRef-` is how SPDX\n     spells a licence "
            "that is not on its list."
        )

    for classifier in declared:
        if classifier.startswith("License :: OSI Approved"):
            problems.append(
                f"it carries the classifier {classifier!r}. Every value under "
                "that prefix\n     asserts an approval these terms do not have."
            )
    if "License :: Other/Proprietary License" not in declared:
        problems.append(
            'it must carry the classifier "License :: Other/Proprietary License".'
        )

if problems:
    fail(
        "packages/python/pyproject.toml is not what PyPI should display:\n     - "
        + "\n     - ".join(problems)
    )
elif parsed is not None:
    pass_("the Python manifest says what PyPI will display")

package_json = read_text("packages/node/package.json")
try:
    manifest = json.loads(package_json) if package_json is not None else None
except json.JSONDecodeError as error:
    fail(f"packages/node/package.json does not parse: {error}")
    manifest = None
if manifest is not None and not isinstance(manifest, dict):
    fail("packages/node/package.json is not a JSON object.")
    manifest = None

# Independent, for the reason the Python checks above are: an `elif` here hid a
# missing NOTICE under `files` whenever the licence field was wrong as well.
node_problems: list[str] = []
if manifest is not None:
    if manifest.get("license") != "SEE LICENSE IN LICENSE":
        node_problems.append(
            'it must carry "license": "SEE LICENSE IN LICENSE" (it says '
            f"{manifest.get('license')!r}).\n     npm has no identifier for "
            "these terms either, and that string is how npm\n     spells one it "
            "cannot name."
        )
    files = manifest.get("files", [])
    # Only the strings: an object in the list is unhashable, and `set()` on it
    # raised, stopping every check below this one.
    listed = (
        {entry for entry in files if isinstance(entry, str)}
        if isinstance(files, list)
        else set()
    )
    if not {"LICENSE", "NOTICE"} <= listed:
        node_problems.append(
            "it must list LICENSE and NOTICE under `files`, or the tarball "
            "arrives with no\n     terms at all."
        )

if node_problems:
    fail(
        "packages/node/package.json is not what npm should display:\n     - "
        + "\n     - ".join(node_problems)
    )
elif manifest is not None:
    pass_("the Node manifest says what npm will display, and ships both files")

# ── 4. The copies have not drifted ─────────────────────────────────────────
# Neither npm nor hatchling reaches outside a package directory, so each
# publishable package keeps its own copy. Three copies is three chances to ship
# one licence and publish another.
for stem in ("LICENSE", "NOTICE"):
    canonical = read_bytes(stem)
    copies = {
        f"packages/{pkg}/{stem}": read_bytes(f"packages/{pkg}/{stem}")
        for pkg in ("python", "node")
    }
    if canonical is None:
        continue  # already named, and there is nothing to diff the copies against
    # A missing copy is already named; the copy still present is diffed anyway,
    # or a deleted NOTICE in one package would hide a drifted one in the other.
    drifted = [
        name for name, copy in copies.items() if copy is not None and copy != canonical
    ]
    if drifted:
        fail(
            f"{', '.join(drifted)} differs from /{stem}.\n"
            f"     Copy it again: cp {stem} packages/python/{stem} && "
            f"cp {stem} packages/node/{stem}"
        )
    elif all(copy is not None for copy in copies.values()):
        pass_(f"{stem} is byte-identical in the root and in both packages")

# ── 5. LICENSE is a licence, not a draft of one ────────────────────────────
licence = read_text("LICENSE")

# A version, and nothing else. The notice this file carried before version 1.0
# granted nothing, and a pattern that still accepted its heading would let that
# notice — or a stale copy of it — pass for the terms in force.
STATUS = re.compile(r"^Version \d+\.\d+ — \d{1,2} \w+ \d{4}$", re.M)
if licence is None:
    pass  # already named once; a missing file has no version line to judge
elif not STATUS.search(licence):
    fail(
        "LICENSE carries no dated version line.\n"
        "     A recipient keeps the terms they received forever, so they have to "
        "be able\n     to say which ones those were, and to tell a stale copy "
        "from the current one.\n     Expected a line of the form:\n"
        "       Version 1.0 — 21 September 2026"
    )
else:
    pass_("LICENSE says which version it is, and of what date")

# The fingerprint of the INSTALLED rendering of counsel's text, byte for byte.
# Not a check that the text is right — a check that nobody has made it say
# something else. A typo fixed here in good faith is a licence counsel never
# wrote, published under counsel's version number; so is a line ending or a
# trailing space an editor normalised on the way past. A change to the licence
# goes to counsel first, and arrives here as a new version line and a new
# fingerprint in the same commit.
LICENSE_SHA256 = "cad6a59f937837b1ceb9a2dac78900d677b9c1b8154bb7832dfcf649aeb819b1"  # pragma: allowlist secret
licence_bytes = read_bytes("LICENSE")
if licence_bytes is not None:
    actual = hashlib.sha256(licence_bytes).hexdigest()
    if actual != LICENSE_SHA256:
        fail(
            "LICENSE is not the installed rendering of counsel's text.\n"
            f"     expected sha256 {LICENSE_SHA256}\n"
            f"     found    sha256 {actual}\n"
            "     The fingerprint pins the rendering installed from counsel, byte "
            "for byte.\n     A change to the licence goes through counsel, then "
            "arrives here with a new\n     version line and a new fingerprint — "
            "never as a typo fix, a reflow, or\n     a normalised line ending."
        )
    else:
        pass_("LICENSE is, byte for byte, the installed rendering of counsel's text")

# The markings counsel puts on a working copy. Publishing a file carrying any
# of them is how a privileged draft becomes the operative terms.
DRAFT_MARKINGS = (
    "Attorney Privileged",
    "PRIVILEGED WORKING DOCUMENT",
    "NOT FOR EXTERNAL USE",
    "Confidential –",
    "Confidential -",
)
# Collected rather than reported inside the loop: a `for/else` here printed its
# green line under its own red one, because the `break` left the inner loop and
# the outer one still finished normally. The exit code was right and the output
# said the opposite of what it had just found — which is worse than a check that
# simply fails, because someone reads the last line and believes it.
marked = sorted(
    f"{name} carries {marking!r}"
    for name, text in documents.items()
    for marking in DRAFT_MARKINGS
    if marking in text
)
if marked:
    fail(
        "these carry a marking from a working copy:\n     "
        + "\n     ".join(marked)
        + "\n     A document that says it is not for external use cannot be the "
        "terms\n     this repository publishes."
    )
else:
    pass_("no tracked file carries a draft or privilege marking")

# ── 6. NOTICE identifies the patents this repository asserts ───────────────
# Three, and the third is an application rather than a grant. Counsel's list is
# the authority on what the family contains, and this repository had been
# carrying a shorter one — not a decision, an omission. `CN115151937A` is a
# published application number, so naming it discloses nothing; what must stay
# accurate is its status, because asserting a granted Chinese patent where only
# an application exists is a false claim of right.
notice = read_text("NOTICE")
PATENTS = ("12,413,397", "4,100,905", "CN115151937A")
missing = [n for n in PATENTS if n not in notice] if notice is not None else []
if notice is None:
    pass  # already named once; there is no list to find the numbers missing from
elif missing:
    fail(
        "NOTICE does not identify " + ", ".join(missing) + ".\n"
        "     Reproducing a patent notice with one of them left out is worse "
        "than not\n     reproducing one at all: it reads as the complete list."
    )
elif not re.search(
    r"CN115151937A[^.]*\b(pending|application)\b|\b(pending|application)\b[^.]*"
    r"CN115151937A",
    notice,
    re.IGNORECASE,
):
    fail(
        "NOTICE names CN115151937A without saying it is a pending application.\n"
        "     An application is not a grant. Listing it beside two granted "
        "patents with\n     no qualifier claims a right that does not exist yet, "
        "and a notice that\n     overclaims is worth less than one that says "
        "less."
    )
else:
    pass_("NOTICE identifies the patents, and calls the application an application")

# ── 7. The virtual patent marking is where it has to be ────────────────────
# 35 U.S.C. §287(a) lets a patentee mark a product with an internet address at
# which the patents are listed instead of with the numbers themselves, and ties
# damages for infringement before actual notice to that marking. Clause 5 of
# LICENSE provides for the notice in the source code, object code and
# documentation, and clauses 4(a) and 4(g) forbid removing or obscuring it. So
# the marking is checked wherever a reader of this SDK can meet it: at the top
# of each source file (and, through the build, of each shipped file —
# `check_publishable.sh` reads those inside the artefacts), on the pages a
# registry renders, and in the metadata it displays as fact.
#
# The source files come from `git ls-files`, never from a list kept by hand: a
# list goes stale the first time a module is added, and the new module ships
# unmarked while this check keeps saying yes. An empty enumeration is a failure,
# not a pass — a moved directory would otherwise mark nothing, silently.
MARKING = "Patents: https://sdk.idside.eu/patents"
PATENTS_URL = "https://sdk.idside.eu/patents"
unmarked: list[str] = []

SOURCES = {
    "packages/python/src": re.compile(r"^packages/python/src/.+\.py$"),
    "packages/node/src": re.compile(r"^packages/node/src/.+\.ts$"),
}
for tree, pattern in SOURCES.items():
    sources = sorted(name for name in tracked if pattern.match(name))
    if not sources:
        unmarked.append(
            f"{tree}: no tracked source file found, so the enumeration itself "
            "is broken"
        )
    for name in sources:
        try:
            head = (root / name).read_text(encoding="utf-8").splitlines()[:3]
        except (OSError, UnicodeDecodeError):
            unmarked.append(f"{name}: cannot be read")
            continue
        if not any(MARKING in line for line in head):
            unmarked.append(f"{name}: not in its first three lines")

for name in (
    "README.md",
    "packages/python/README.md",
    "packages/node/README.md",
    "NOTICE",
):
    if name in _unreadable or name in _undecodable:
        continue  # already named once, by the check that read it first
    text = documents.get(name)
    if text is None:
        unmarked.append(f"{name}: not tracked, or not readable")
    elif MARKING not in text:
        unmarked.append(f"{name}: absent")

# Not when the manifest did not parse: that is already named, and reading no
# `[project.urls]` out of it would name it a second time as a missing link.
if pyproject is not None and parsed is not None:
    urls = project_urls(pyproject)
    if urls.get("Patents") != PATENTS_URL:
        unmarked.append(
            "packages/python/pyproject.toml: [project.urls] needs "
            f'Patents = "{PATENTS_URL}" (it has {urls.get("Patents")!r})'
        )

if manifest is not None and "sdk.idside.eu/patents" not in str(
    manifest.get("description", "")
):
    unmarked.append(
        "packages/node/package.json: `description` does not mention "
        "sdk.idside.eu/patents"
    )

if unmarked:
    fail(
        f'the patent marking "{MARKING}" is missing from:\n     - '
        + "\n     - ".join(unmarked)
        + "\n     A source file carries it as a two-line comment at the very "
        "top; the other\n     files carry the line itself. An unmarked copy is "
        "one that can be passed on\n     with the patents left out."
    )
else:
    pass_("the patent marking is in every source file, README, NOTICE and manifest")

if failures:
    sys.exit(f"\n{len(failures)} check(s) failed.")
print("\nThe licence this repository publishes is the one it says it publishes.")
PY
