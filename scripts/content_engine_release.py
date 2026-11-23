#!/usr/bin/env python3
"""Produce, at release time, what the ID Side cloud needs to recognise this engine.

    uv run --project packages/python python scripts/content_engine_release.py \\
        --out /path/outside/the/repository [--pool-size 2000] [--seed 12345]

Writes two files into ``--out``, which must lie outside this repository (the
challenge pool is the cloud's private material and is never committed):

- ``content-engine-manifest.json``: the engine version, the ``rules`` digest and
  the ``build`` digests of the Python and Node packages exactly as they are
  published — computed from the built wheel and the packed npm tarball, then
  checked against what each installed package declares at run time.
- ``content-challenge-pool.json``: seeded inputs and the text each must become
  under the reference policy (every catalogue detector masking, nothing else).
  Every expected output is computed by both SDKs, and by every Python found
  between 3.10 and 3.14, and the script stops on the first difference: a pool
  entry two runtimes clean differently would fail an honest SDK's challenge.

Inputs use ASCII and a short list of Latin-1 letters only, so that no Unicode
version difference between runtimes can reach them. Every value is fictitious.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import string
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON_PKG = ROOT / "packages" / "python"
NODE_PKG = ROOT / "packages" / "node"
RULES_FILE = ROOT / "packages" / "descriptors" / "content-rules.json"


def fail(message: str) -> None:
    sys.exit(f"content_engine_release: {message}")


def build_digest_of(files: list[tuple[str, bytes]]) -> str:
    """The SDKs' framing: sorted by path; path, NUL, length, NUL, bytes."""
    digest = hashlib.sha256()
    for path, data in sorted(files):
        digest.update(path.encode() + b"\0" + str(len(data)).encode() + b"\0")
        digest.update(data)
    return "sha256:" + digest.hexdigest()


# ── what is published ──────────────────────────────────────────────────────


def python_build(work: Path) -> tuple[str, Path]:
    out = work / "wheel"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=PYTHON_PKG, check=True, capture_output=True,
    )  # fmt: skip
    wheel = next(out.glob("idside-*.whl"))
    extracted = work / "wheel-files"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(extracted)
    files = [
        (path.relative_to(extracted).as_posix(), path.read_bytes())
        for pattern in ("idside/_cleaning/*.py", "idside/providers/*.json")
        for path in extracted.glob(pattern)
    ]
    return build_digest_of(files), extracted


def node_build(work: Path) -> tuple[str, Path]:
    out = work / "npm"
    out.mkdir()
    subprocess.run(
        ["npm", "pack", "--pack-destination", str(out)],
        cwd=NODE_PKG, check=True, capture_output=True,
    )  # fmt: skip
    tarball = next(out.glob("idside-sdk-*.tgz"))
    with tarfile.open(tarball) as archive:
        archive.extractall(out, filter="data")
    package = out / "package"
    files = [
        (path.relative_to(package).as_posix(), path.read_bytes())
        for pattern in ("dist/cleaning/*.js", "dist/providers/*.json")
        for path in package.glob(pattern)
    ]
    return build_digest_of(files), package


def declared_python(extracted: Path, python: str | None = None) -> dict[str, str]:
    """What the published wheel declares at run time, imported from the wheel itself."""
    code = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "import idside._cleaning as c; "
        "print(json.dumps({'build': c.build_digest(), 'rules': c.rules_digest(), "
        "'engine': c.ENGINE_VERSION, 'file': c.__file__}))"
    )
    command = (
        ["uv", "run", "--project", str(PYTHON_PKG), "python", "-I", "-c", code]
        if python is None
        else ["uv", "run", "--no-project", "--python", python, "--with", "httpx",
              "python", "-I", "-c", code]
    )  # fmt: skip
    result = subprocess.run(
        [*command, str(extracted)], check=True, capture_output=True, text=True
    )
    found: dict[str, str] = json.loads(result.stdout)
    if not found.pop("file").startswith(str(extracted)):
        fail("the wheel's own idside was not the one imported")
    return found


def declared_node(package: Path) -> dict[str, str]:
    code = (
        "const c = await import(process.argv[1]);"
        "console.log(JSON.stringify({build: c.buildDigest(), rules: c.rulesDigest(), "
        "engine: c.ENGINE_VERSION}));"
    )
    module = (package / "dist" / "cleaning" / "index.js").as_uri()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", code, module],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    found: dict[str, str] = json.loads(result.stdout)
    return found


# ── the challenge pool ─────────────────────────────────────────────────────

LATIN = "éèêëàâçùûôîïÉÈÀÇ"
WORDS = (
    "the report is due before the meeting with the team about the budget "
    "le rapport doit partir avant la réunion avec l'équipe sur le budget "
    "please summarise this note and keep the tone neutral merci de résumer "
    "ce message en gardant un ton neutre pour le client à Paris et à Lyon"
).split()
FIRST = ["marie", "paul", "lea", "hugo", "chloe", "lucas", "emma", "louis", "zoe"]
LAST = ["dupont", "martin", "bernard", "petit", "durand", "leroy", "moreau"]
ALNUM = string.ascii_letters + string.digits
HEX = "0123456789abcdef"
#: The letters of a French plate: no I, O or U.
PLATE_LETTERS = "".join(c for c in string.ascii_uppercase if c not in "IOU")


def luhn_complete(rng: random.Random, prefix: str, length: int) -> str:
    body = prefix + "".join(
        rng.choice(string.digits) for _ in range(length - len(prefix) - 1)
    )
    for last in string.digits:
        number = body + last
        total = 0
        for index, char in enumerate(reversed(number)):
            value = int(char) * (2 if index % 2 else 1)
            total += value - 9 if value > 9 else value
        if total % 10 == 0:
            return number
    raise AssertionError(body)


def iban(rng: random.Random, country: str, length: int) -> str:
    bban = "".join(rng.choice(string.digits) for _ in range(length - 4))
    moved = bban + country + "00"
    check = 98 - int("".join(str(int(char, 36)) for char in moved)) % 97
    return f"{country}{check:02d}{bban}"


def nir(rng: random.Random) -> str:
    body = (
        rng.choice("12") + f"{rng.randint(40, 99):02d}" + f"{rng.randint(1, 12):02d}"
        + f"{rng.randint(1, 95):02d}" + f"{rng.randint(1, 999):03d}" + f"{rng.randint(1, 999):03d}"
    )  # fmt: skip
    return body + f"{97 - int(body) % 97:02d}"


def grouped(text: str, size: int = 4) -> str:
    return " ".join(text[i : i + size] for i in range(0, len(text), size))


def value(rng: random.Random) -> str:
    """One fictitious value of a kind the catalogue knows, or a near miss."""
    first, last = rng.choice(FIRST), rng.choice(LAST)
    makers = [
        lambda: f"{first}.{last}@example.{rng.choice(['com', 'org', 'net'])}",
        lambda: (
            f"0{rng.randint(1, 7)} "
            + " ".join(f"{rng.randint(0, 99):02d}" for _ in range(4))
        ),
        lambda: (
            f"+33 {rng.randint(6, 7)} "
            + " ".join(f"{rng.randint(0, 99):02d}" for _ in range(4))
        ),
        lambda: grouped(luhn_complete(rng, rng.choice(["4", "51", "55", "2221"]), 16)),
        lambda: luhn_complete(rng, rng.choice(["34", "37"]), 15),
        lambda: grouped(iban(rng, "FR", 27)),
        lambda: iban(rng, "DE", 22),
        lambda: "SIREN " + luhn_complete(rng, "", 9),
        lambda: "SIRET " + luhn_complete(rng, luhn_complete(rng, "", 9), 14),
        lambda: "NIR " + nir(rng),
        lambda: (
            f"{rng.randint(1, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
        ),
        lambda: (
            "fe80::"
            + ":".join("".join(rng.choice(HEX) for _ in range(4)) for _ in range(3))
        ),
        lambda: ":".join("".join(rng.choice(HEX) for _ in range(2)) for _ in range(6)),
        lambda: (
            "".join(rng.choice(PLATE_LETTERS) for _ in range(2))
            + f"-{rng.randint(1, 999):03d}-"
            + "".join(rng.choice(PLATE_LETTERS) for _ in range(2))
        ),
        lambda: "sk-" + "proj-" + "".join(rng.choice(ALNUM) for _ in range(48)),
        lambda: "sk-" + "ant-" + "".join(rng.choice(ALNUM) for _ in range(48)),
        lambda: (
            "AKIA"
            + "".join(
                rng.choice(string.ascii_uppercase + string.digits) for _ in range(16)
            )
        ),
        lambda: "ghp_" + "".join(rng.choice(ALNUM) for _ in range(36)),
        lambda: "sk_" + "test_" + "".join(rng.choice(ALNUM) for _ in range(32)),
        lambda: "Bearer " + "".join(rng.choice(ALNUM) for _ in range(40)),
        lambda: (
            f"postgres://{first}:"
            + "".join(rng.choice(ALNUM) for _ in range(12))
            + "@db.example.com/app"
        ),
        lambda: "password=" + "".join(rng.choice(ALNUM) for _ in range(14)),
        lambda: grouped(luhn_complete(rng, "4", 16)[:-1] + "x"),  # near miss
        lambda: f"{first}@localhost",  # near miss
        lambda: (
            f"version {rng.randint(1, 9)}.{rng.randint(0, 20)}.{rng.randint(0, 99)}"
        ),  # near miss
        lambda: f"order {rng.randint(10**9, 10**10 - 1)}",
    ]
    return rng.choice(makers)()


def sentence(rng: random.Random) -> str:
    words = [rng.choice(WORDS) for _ in range(rng.randint(3, 12))]
    if rng.random() < 0.15:
        words.insert(
            rng.randrange(len(words) + 1), rng.choice(LATIN) + rng.choice(WORDS)
        )
    if rng.random() < 0.8:
        words.insert(rng.randrange(len(words) + 1), value(rng))
    text = " ".join(words)
    # Upper-case the first letter only: `capitalize()` would lower the values.
    return text[0].upper() + text[1:] + rng.choice([".", "!", "?", ":", ";"])


def pool_inputs(seed: int, size: int) -> list[str]:
    rng = random.Random(seed)  # noqa: S311 - reproducible test material, not a secret
    inputs: list[str] = []
    seen: set[str] = set()
    while len(inputs) < size:
        text = " ".join(sentence(rng) for _ in range(rng.randint(1, 3)))
        if text not in seen:
            seen.add(text)
            inputs.append(text)
    return inputs


def node_outputs(package: Path, inputs: list[str], work: Path) -> list[str]:
    source = work / "pool-inputs.json"
    source.write_text(json.dumps(inputs), encoding="utf-8")
    code = (
        "const fs = await import('node:fs');"
        "const c = await import(process.argv[1]);"
        "const inputs = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));"
        "const cleaner = c.ContentCleaner.reference();"
        "console.log(JSON.stringify(inputs.map((t) => cleaner.clean(t).payload)));"
    )
    module = (package / "dist" / "cleaning" / "index.js").as_uri()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", code, module, str(source)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    outputs: list[str] = json.loads(result.stdout)
    return outputs


def python_outputs(
    extracted: Path, inputs: list[str], work: Path, python: str | None
) -> list[str]:
    source = work / "pool-inputs.json"
    source.write_text(json.dumps(inputs), encoding="utf-8")
    code = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "from idside._cleaning import ContentCleaner; "
        "c = ContentCleaner.reference(); "
        "print(json.dumps([c.clean(t).payload for t in json.load(open(sys.argv[2]))]))"
    )
    command = (
        ["uv", "run", "--project", str(PYTHON_PKG), "python", "-I", "-c", code]
        if python is None
        else ["uv", "run", "--no-project", "--python", python, "--with", "httpx",
              "python", "-I", "-c", code]
    )  # fmt: skip
    result = subprocess.run(
        [*command, str(extracted), str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    outputs: list[str] = json.loads(result.stdout)
    return outputs


def other_pythons() -> list[str]:
    found = []
    for version in ("3.11", "3.12", "3.13", "3.14"):
        probe = subprocess.run(
            ["uv", "python", "find", "--no-project", version],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            found.append(version)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--pool-size", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    out = args.out.resolve()
    if out == ROOT or ROOT in out.parents:
        fail(
            f"{out} is inside the repository: the challenge pool must never be committed"
        )
    seed = args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "big")

    rules = "sha256:" + hashlib.sha256(RULES_FILE.read_bytes()).hexdigest()
    engine = json.loads(RULES_FILE.read_text(encoding="utf-8"))["engine"]
    version = (PYTHON_PKG / "src" / "idside" / "__about__.py").read_text().split('"')[1]
    node_version = json.loads((NODE_PKG / "package.json").read_text())["version"]

    work = Path(tempfile.mkdtemp(prefix="idside-content-release-"))
    try:
        python_digest, wheel = python_build(work)
        node_digest, package = node_build(work)
        for name, declared in (
            ("python", declared_python(wheel)),
            ("node", declared_node(package)),
        ):
            expected = {"python": python_digest, "node": node_digest}[name]
            if declared != {"build": expected, "rules": rules, "engine": engine}:
                fail(
                    f"the {name} package declares {declared}, not build {expected}, rules {rules}"
                )

        inputs = pool_inputs(seed, args.pool_size)
        expected = python_outputs(wheel, inputs, work, None)
        runtimes = {
            "python (project)": expected,
            "node": node_outputs(package, inputs, work),
        }
        for python in other_pythons():
            runtimes[f"python {python}"] = python_outputs(wheel, inputs, work, python)
        for runtime, outputs in runtimes.items():
            for index, (want, got) in enumerate(zip(expected, outputs, strict=True)):
                if want != got:
                    fail(
                        f"{runtime} cleans pool entry {index} differently (seed {seed})"
                    )
        changed = sum(
            1 for before, after in zip(inputs, expected, strict=True) if before != after
        )

        out.mkdir(parents=True, exist_ok=True)
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = {
            "engine": engine,
            "rules": rules,
            "build": {"python": python_digest, "node": node_digest},
            "sdk_version": {"python": version, "node": node_version},
            "generated_at": generated,
        }
        pool = {
            "engine": engine,
            "rules": rules,
            "seed": seed,
            "generated_at": generated,
            "reference_policy": "every catalogue detector set to mask, no custom entry, attachments allow",
            "response": 'sha256(token + "\\n" + the expected outputs of the drawn entries, in order, joined by "\\n"), UTF-8',
            "checked_by": sorted(runtimes),
            "entries": [
                {"id": f"p{index:05d}", "input": text, "expected": cleaned}
                for index, (text, cleaned) in enumerate(
                    zip(inputs, expected, strict=True)
                )
            ],
        }
        (out / "content-engine-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", "utf-8"
        )
        (out / "content-challenge-pool.json").write_text(
            json.dumps(pool, indent=1, ensure_ascii=False) + "\n", "utf-8"
        )
        print(json.dumps(manifest, indent=2))
        print(f"pool: {len(inputs)} entries, {changed} changed by cleaning, seed {seed}, "
              f"checked by {', '.join(sorted(runtimes))}")  # fmt: skip
        print(f"written to {out}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
