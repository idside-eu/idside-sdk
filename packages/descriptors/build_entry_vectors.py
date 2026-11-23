#!/usr/bin/env python3
"""Generate `testdata/content-entry-vectors.json`: which custom entries a policy may hold.

An organization adds its own terms, domains and patterns to its prompt-cleaning
policy. The ID Side cloud validates an entry before signing it, and an SDK that
meets one it cannot compile refuses every call of that application (fail-closed).
So the cloud must never sign an entry an SDK refuses. Each vector here says, for
one entry alone, what compiling a policy that holds it decides: `accepted`, or a
`ContentPolicyError`. Both SDK suites hold every verdict; the cloud reads the
same file, and may be stricter than `accepted: true`, never looser than
`accepted: false`.

The verdicts are computed HERE, from the rules as written below — never by
either SDK — so the file is a statement about what an entry must be, which both
compilers are then held to:

- every entry: a label of 1 to 24 characters in [A-Z0-9_], and a value of 1 to
  `custom_value_chars` code points (an astral character is one);
- `term`: some character left once the value is normalised and split on ASCII
  whitespace (space, tab, CR, LF, FF, VT) — the normalisation drops what the
  catalogue lists as ignorable, so a value made only of invisible characters
  and ASCII whitespace has none;
- `domain`: once normalised and ASCII-lowered, stripped of ASCII whitespace at
  its ends, of one leading `@` then one leading `*.`, and of dots at its ends:
  2 to 9 labels of 1 to 63 characters in [a-z0-9-];
- `pattern`: at least one token once invisible characters are dropped (`#`,
  `@`, `*`, or any other character, literal, as its normalised characters),
  and no `*` followed by a token that can be a letter or a digit (`#`, `@`,
  `*`, an ASCII letter or digit): a run of letters and digits already takes
  them all, so such a pattern could never match.

The label is "CASE" unless a vector names one (`null` stands for a label that is
not a string). Run: `python packages/descriptors/build_entry_vectors.py`.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT = HERE / "testdata" / "content-entry-vectors.json"
RULES = json.loads((HERE / "content-rules.json").read_text(encoding="utf-8"))
NORMALIZATION = RULES["normalization"]
MAX_VALUE = RULES["limits"]["custom_value_chars"]

ASCII_SPACE = " \t\r\n\f\v"
DOMAIN = re.compile(r"[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){1,8}")
LABEL = re.compile(r"[A-Z0-9_]{1,24}")


def _within(ranges: list[list[int]], code: int) -> bool:
    return any(low <= code <= high for low, high in ranges)


def fold(text: str) -> str:
    """The catalogue's normalisation, code point by code point, ASCII-lowered."""
    equivalents = dict(NORMALIZATION["equivalents"])
    out = []
    for char in text:
        if _within(NORMALIZATION["ignorable"], ord(char)):
            continue
        for part in unicodedata.normalize("NFKD", char):
            code = ord(part)
            if _within(NORMALIZATION["combining"], code) or _within(
                NORMALIZATION["ignorable"], code
            ):
                continue
            if code in equivalents:
                out.append(equivalents[code])
            elif code > 0xFFFF:
                out.append("\ufffd")
            else:
                out.append(part)
    return "".join(out).translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def accepted(kind: str, value: str, label: Any) -> bool:
    if not isinstance(label, str) or not LABEL.fullmatch(label):
        return False
    if not 1 <= len(value) <= MAX_VALUE:
        return False
    if kind == "term":
        return any(re.split(r"[ \t\r\n\f\v]+", fold(value)))
    if kind == "domain":
        name = fold(value).strip(ASCII_SPACE)
        name = name.removeprefix("@").removeprefix("*.").strip(".")
        return DOMAIN.fullmatch(name) is not None
    # A wildcard is one of the three ASCII characters as written; a character
    # that only folds to one (a full-width or small form) is a literal.
    tokens: list[tuple[bool, str]] = []
    for char in value:
        if char in "#@*":
            tokens.append((True, char))
        else:
            tokens.extend((False, folded) for folded in fold(char))
    for (wild, token), (next_wild, following) in zip(tokens, tokens[1:]):
        if (
            wild
            and token == "*"
            and (next_wild or (following.isascii() and following.isalnum()))
        ):
            return False
    return bool(tokens)


VECTORS: list[dict[str, Any]] = []


def add(kind: str, value: str, note: str, label: Any = "CASE") -> None:
    vector: dict[str, Any] = {"kind": kind, "value": value}
    if label != "CASE":
        vector["label"] = label
    vector["accepted"] = accepted(kind, value, label)
    vector["note"] = note
    assert vector not in VECTORS, vector
    VECTORS.append(vector)


# ── blanks and invisible characters, alone, at either end and in the middle ──
BLANKS = {
    "space": " ",
    "tab": "\t",
    "line feed": "\n",
    "carriage return": "\r",
    "form feed": "\f",
    "vertical tab": "\v",
    "next line U+0085": "\u0085",
    "no-break space U+00A0": "\u00a0",
    "soft hyphen U+00AD": "\u00ad",
    "combining grapheme joiner U+034F": "\u034f",
    "arabic letter mark U+061C": "\u061c",
    "ogham space U+1680": "\u1680",
    "mongolian vowel separator U+180E": "\u180e",
    "en quad U+2000": "\u2000",
    "thin space U+2009": "\u2009",
    "zero-width space U+200B": "\u200b",
    "zero-width joiner U+200D": "\u200d",
    "line separator U+2028": "\u2028",
    "paragraph separator U+2029": "\u2029",
    "left-to-right embedding U+202A": "\u202a",
    "narrow no-break space U+202F": "\u202f",
    "word joiner U+2060": "\u2060",
    "ideographic space U+3000": "\u3000",
    "variation selector U+FE0F": "\ufe0f",
    "byte order mark U+FEFF": "\ufeff",
}
BASES = {
    "term": ("Projet Atlas", lambda value, blank: value[:3] + blank + value[3:]),
    "domain": ("acme.example", lambda value, blank: value[:2] + blank + value[2:]),
    "pattern": ("PRJ-####", lambda value, blank: value[:2] + blank + value[2:]),
}
for kind, (base, inside) in BASES.items():
    for name, blank in BLANKS.items():
        add(kind, blank, f"{name} alone")
        add(kind, blank * 3, f"{name} three times")
        add(kind, blank + base, f"{name} before the value")
        add(kind, base + blank, f"{name} after the value")
        add(kind, blank + base + blank, f"{name} around the value")
        add(kind, inside(base, blank), f"{name} inside the value")

# ── lengths, in code points ─────────────────────────────────────────────────
EMOJI = "\U0001f600"
for kind, unit in (("term", "a"), ("pattern", "a")):
    for size in (1, MAX_VALUE - 1, MAX_VALUE, MAX_VALUE + 1):
        add(kind, unit * size, f"{size} ASCII letters")
    for size in (1, MAX_VALUE, MAX_VALUE + 1):
        add(
            kind,
            EMOJI * size,
            f"{size} characters outside the BMP (two UTF-16 units each)",
        )
    add(
        kind,
        "e\u0301" * (MAX_VALUE // 2),
        "decomposed accents: two code points each, at the limit",
    )
    add(
        kind,
        "e\u0301" * (MAX_VALUE // 2) + "e",
        "decomposed accents: one code point past the limit",
    )
    add(
        kind,
        "\u00e9" * MAX_VALUE,
        "composed accents: one code point each, at the limit",
    )
add("term", "", "empty")
add("domain", "", "empty")
add("pattern", "", "empty")
label_63 = "a" * 63
long_domain = ".".join(["b" * 48] * 4) + ".abcd"  # 200 code points, 5 labels
assert len(long_domain) == MAX_VALUE
add("domain", long_domain, "200 code points, five labels")
add("domain", long_domain + "d", "201 code points")
add("domain", EMOJI + "acme.example", "an emoji before the domain")

# ── terms ───────────────────────────────────────────────────────────────────
for value, note in (
    ("Atlas", "one word"),
    ("Projet Atlas", "two words"),
    ("Projet   Atlas", "words separated by several spaces"),
    ("Projet\tAtlas", "words separated by a tab"),
    ("Op\u00e9ration Nordlys", "composed accent"),
    ("Ope\u0301ration Nordlys", "decomposed accent"),
    ("PROJET ATLAS", "capitals"),
    ("\ufb01nance", "a ligature"),
    ("\uff30\uff52\uff4f\uff4a\uff45\uff54", "full-width letters"),
    ("\u041f\u0440\u043e\u0435\u043a\u0442", "Cyrillic"),
    ("\u8ba1\u5212", "CJK"),
    (EMOJI, "an emoji"),
    ("C++", "punctuation at the end"),
    ("-", "a dash alone"),
    ("'", "an apostrophe alone"),
    ("\u2019", "a typographic apostrophe alone"),
    ("Projet\x00Atlas", "a NUL character inside"),
    ("\x00", "a NUL character alone"),
    ("\x07", "a bell character alone"),
    ("\x1f", "a unit separator alone"),
    ("\x7f", "DEL alone"),
    ("[EMAIL_1]", "a placeholder"),
):
    add("term", value, note)

# ── domains ─────────────────────────────────────────────────────────────────
for value, note in (
    ("acme.example", "two labels"),
    ("mail.acme.example", "three labels"),
    ("ACME.Example", "capitals"),
    ("acme.example.", "a final dot"),
    (".acme.example", "a leading dot"),
    ("..acme.example..", "dots at both ends"),
    ("acme..example", "an empty label"),
    ("@acme.example", "a leading at sign"),
    ("@@acme.example", "two leading at signs"),
    ("*.acme.example", "a wildcard"),
    ("@*.acme.example", "an at sign, then a wildcard"),
    ("*.@acme.example", "a wildcard, then an at sign"),
    ("**.acme.example", "two stars"),
    ("*acme.example", "a star without its dot"),
    ("user@acme.example", "an address"),
    ("https://acme.example", "a scheme"),
    ("acme.example/path", "a path"),
    ("acme.example:443", "a port"),
    ("acme.example?q=1", "a query"),
    ("10.0.0.1", "an IPv4 address: four numeric labels"),
    ("[2001:db8::1]", "an IPv6 address"),
    ("acme.123", "a numeric TLD"),
    ("localhost", "one label"),
    ("-acme.example", "a dash at the start of a label"),
    ("acme-.example", "a dash at the end of a label"),
    ("ac-me.example", "a dash inside a label"),
    ("acme_corp.example", "an underscore"),
    ("acme example", "a space inside"),
    (label_63 + ".example", "a label of 63 characters"),
    (label_63 + "a.example", "a label of 64 characters"),
    (".".join(["a"] * 9), "nine labels"),
    (".".join(["a"] * 10), "ten labels"),
    ("xn--e1afmkfd.xn--p1ai", "punycode"),
    ("\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444", "Cyrillic (IDN)"),
    ("caf\u00e9.example", "a composed accent, folded away"),
    ("cafe\u0301.example", "a decomposed accent, folded away"),
    ("\uff41\uff43\uff4d\uff45.example", "full-width letters"),
    ("acme\u3002example", "an ideographic full stop"),
    ("acme\uff0eexample", "a full-width full stop"),
    ("ac\u00adme.example", "a soft hyphen inside"),
    ("acme.exa\x00mple", "a NUL character inside"),
    ("acme.example\x00", "a NUL character at the end"),
    ("acme.ex" + EMOJI, "an emoji inside"),
    ("\ufb01.example", "a ligature"),
):
    add("domain", value, note)

# ── patterns ────────────────────────────────────────────────────────────────
for value, note in (
    ("#", "one digit"),
    ("@", "one letter"),
    ("*", "one run"),
    ("**", "two stars in a row"),
    ("***", "three stars"),
    ("a**b", "two stars inside"),
    ("*-*", "two runs"),
    ("*#*", "runs around a digit"),
    ("*@", "a run before a letter wildcard"),
    ("@*", "a letter wildcard before a run"),
    ("CLI-*A", "a run before a letter"),
    ("CLI-*a", "a run before a lower-case letter"),
    ("CLI-*7", "a run before a digit"),
    ("CLI-*#", "a run before a digit wildcard"),
    ("CLI-*-A", "a run, a dash, a letter"),
    ("CLI-* A", "a run, a space, a letter"),
    ("CLI-*\u00e9", "a run before an accented letter: its letter once normalised"),
    ("CLI-*\u00b2", "a run before a superscript two: a digit once normalised"),
    ("CLI-*\u200bA", "a run, an invisible character, a letter"),
    ("*\u200b*", "two runs around an invisible character"),
    ("*.*", "two runs around a dot"),
    ("*" + "\U0001f600", "a run before an emoji"),
    ("#@*", "only wildcards"),
    ("PRJ-####", "a reference"),
    ("@@-##", "letters and digits"),
    ("CASE-@@#*", "a mixed reference"),
    ("(##)", "brackets"),
    ("\\#", "a backslash before a digit wildcard: no escapes"),
    ("\\*", "a backslash before a run"),
    ("\\", "a backslash alone"),
    (".", "a dot alone"),
    ("[A-Z]", "a regular expression is literal"),
    ("a+b?", "regular expression characters are literal"),
    ("ABC", "letters only"),
    ("\u00e9", "an accented letter"),
    ("e\u0301", "a decomposed accent"),
    ("\ufb01", "a ligature"),
    ("\u216b", "a Roman numeral"),
    ("\u00b2", "a superscript digit"),
    ("\u0416", "a Cyrillic letter"),
    (EMOJI, "an emoji"),
    ("\x00", "a NUL character"),
    ("\x1b", "an escape character"),
    ("\uff0aA", "a full-width star is a literal, not a run"),
    ("X-*\uff03", "a run before a full-width number sign: a literal"),
    ("X-*\uff0a", "a run before a full-width star: a literal"),
    ("X-*\uff20", "a run before a full-width at sign: a literal"),
    ("X-*\ufe5f", "a run before a small number sign: a literal"),
    ("A\ufe61B", "a small star between letters: a literal"),
):
    add("pattern", value, note)

# ── labels ──────────────────────────────────────────────────────────────────
for label, note in (
    ("A", "one letter"),
    ("PROJECT_1", "letters, digit and underscore"),
    ("_", "an underscore alone"),
    ("9", "a digit alone"),
    ("A" * 24, "24 characters"),
    ("A" * 25, "25 characters"),
    ("", "empty"),
    ("project", "lower case"),
    ("PROJ-1", "a dash"),
    ("PROJ 1", "a space"),
    ("PROJ\u00c9", "an accented letter"),
    ("PROJECT\n", "a final line feed"),
    (" PROJECT", "a leading space"),
    ("\uff30\uff32\uff2f\uff2a", "full-width letters"),
    (None, "not a string"),
):
    for kind, value in (
        ("term", "Atlas"),
        ("domain", "acme.example"),
        ("pattern", "PRJ-#"),
    ):
        add(kind, value, f"label: {note}", label=label)


DOCUMENT = {
    "format": 1,
    "note": (
        "Generated by build_entry_vectors.py. One custom entry per vector, compiled "
        "alone in a policy with action mask; `accepted` is whether compiling succeeds "
        "(false: ContentPolicyError). Both SDKs hold every verdict; the ID Side cloud "
        "may refuse more than `accepted: true`, never less than `accepted: false`. "
        "The label is CASE unless the vector names one; null is a label that is not a string."
    ),
    "vectors": VECTORS,
}


def render() -> str:
    return json.dumps(DOCUMENT, indent=1, ensure_ascii=True) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    kept = sum(vector["accepted"] for vector in VECTORS)
    print(
        f"wrote {len(VECTORS)} vectors ({kept} accepted) to {OUT.relative_to(HERE.parent.parent)}"
    )
