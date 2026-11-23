"""``idside-verify-clean``: the replay check as a command."""

from __future__ import annotations

import io
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from idside._cleaning import content_policy_digest, reference_policy
from idside._cleaning.cli import main

POLICY = reference_policy()


def _run(
    tmp_path: Path, body: Any, policy: Any = POLICY, *extra: str
) -> tuple[int, str, str]:
    request = tmp_path / "request.json"
    request.write_text(json.dumps(body), encoding="utf-8")
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    code = main(
        [str(request), "--policy", str(policy_file), "--provider", "openai", *extra],
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue()


def test_a_clean_request_exits_zero(tmp_path: Path) -> None:
    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "Hi [EMAIL_1]"}],
    }
    code, out, _ = _run(
        tmp_path, body, POLICY, "--digest", content_policy_digest(POLICY)
    )
    assert code == 0
    assert "clean: nothing left" in out
    assert content_policy_digest(POLICY) in out


def test_a_request_with_a_value_left_exits_one_and_never_prints_it(
    tmp_path: Path,
) -> None:
    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "marie@example.com"}],
    }
    code, out, err = _run(tmp_path, body)
    assert code == 1
    assert "would still mask email: 1" in out
    assert "marie" not in out + err


def test_a_request_the_policy_blocks_exits_one(tmp_path: Path) -> None:
    policy = {"schema": 1, "detectors": {"credit_card": "block"}}
    code, out, _ = _run(
        tmp_path, {"messages": [{"content": "4111111111111111"}]}, policy
    )
    assert code == 1
    assert "would block it (credit_card: 1)" in out


def test_a_wrong_digest_exits_two(tmp_path: Path) -> None:
    code, _, err = _run(tmp_path, {}, POLICY, "--digest", "sha256:" + "0" * 64)
    assert code == 2
    assert "not the policy the proof names" in err


def test_unreadable_input_exits_two(tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    missing = str(tmp_path / "missing.json")
    code = main(
        [missing, "--policy", missing, "--provider", "openai"], out=out, err=err
    )
    assert code == 2
    assert "cannot read the input" in err.getvalue()


def test_a_policy_that_cannot_be_applied_exits_two(tmp_path: Path) -> None:
    code, _, err = _run(tmp_path, {}, {"schema": 2})
    assert code == 2
    assert "cannot be applied" in err


def test_a_policy_outside_json_exits_two(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text('{"schema": 1, "revision": NaN}', encoding="utf-8")
    err = io.StringIO()
    code = main(
        [str(request), "--policy", str(policy), "--provider", "openai"],
        out=io.StringIO(),
        err=err,
    )
    assert code == 2
    assert "not JSON" in err.getvalue()


def test_runs_as_a_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(POLICY), encoding="utf-8")
    argv = [
        "idside-verify-clean",
        str(request),
        "--policy",
        str(policy),
        "--provider",
        "openai",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    sys.modules.pop("idside._cleaning.cli", None)
    with pytest.raises(SystemExit) as raised:
        runpy.run_module("idside._cleaning.cli", run_name="__main__")
    assert raised.value.code == 0


# ── what is not an answer is never "NOT CLEAN" ─────────────────────────────
# Exit 1 says the body would still be cleaned. An error the command did not
# foresee must not say that: it exits 2, as unreadable input does, and its
# message says nothing was checked.


class _BrokenStream(io.StringIO):
    """An output that fails, as a closed pipe does."""

    def write(self, text: str) -> int:
        raise OSError("broken pipe")


def test_an_unexpected_error_exits_two_not_one(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(POLICY), encoding="utf-8")
    err = io.StringIO()
    code = main(
        [str(request), "--policy", str(policy), "--provider", "openai"],
        out=_BrokenStream(),
        err=err,
    )
    assert code == 2
    assert err.getvalue().startswith("idside-verify-clean: unexpected error")
    assert "nothing was checked" in err.getvalue()


def _files(tmp_path: Path, content: str) -> list[str]:
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {"model": "gpt-4o", "messages": [{"role": "user", "content": content}]}
        ),
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(POLICY), encoding="utf-8")
    return [str(request), "--policy", str(policy), "--provider", "openai"]


@pytest.mark.parametrize("content", ["nothing to mask", "mail marie@example.com"])
def test_an_answer_written_to_a_closed_pipe_exits_two(
    tmp_path: Path, content: str
) -> None:
    """A real pipe whose reader is gone: the answer never arrives, so 2."""
    reader, writer = os.pipe()
    os.close(reader)
    try:
        done = subprocess.run(  # noqa: S603 - this interpreter, this module
            [sys.executable, "-m", "idside._cleaning.cli", *_files(tmp_path, content)],
            stdout=writer,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    finally:
        os.close(writer)
    assert done.returncode == 2


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="needs /dev/full")
@pytest.mark.parametrize("content", ["nothing to mask", "mail marie@example.com"])
def test_an_answer_written_to_a_full_device_exits_two(
    tmp_path: Path, content: str
) -> None:
    with open("/dev/full", "w") as full:
        done = subprocess.run(  # noqa: S603 - this interpreter, this module
            [sys.executable, "-m", "idside._cleaning.cli", *_files(tmp_path, content)],
            stdout=full,
            stderr=full,
            check=False,
        )
    assert done.returncode == 2


class _Unflushable(io.StringIO):
    """An output whose bytes cannot leave: what a full disk does to a flush."""

    def __init__(self, descriptor: int) -> None:
        super().__init__()
        self.descriptor = descriptor

    def write(self, text: str) -> int:
        raise OSError("no space left on device")

    def flush(self) -> None:
        raise OSError("no space left on device")

    def fileno(self) -> int:
        return self.descriptor


def test_what_cannot_be_written_is_dropped_not_retried_at_exit(
    tmp_path: Path,
) -> None:
    """Else the interpreter retries at exit and exits 120, not 2."""
    target = tmp_path / "out"
    descriptors = [os.open(target, os.O_WRONLY | os.O_CREAT) for _ in range(2)]
    try:
        code = main(
            _files(tmp_path, "mail marie@example.com"),
            out=_Unflushable(descriptors[0]),
            err=_Unflushable(descriptors[1]),
        )
        assert code == 2
        for descriptor in descriptors:  # now the null device
            os.write(descriptor, b"lost")
        assert target.read_bytes() == b""
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def test_a_request_too_deep_to_read_exits_two(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(POLICY), encoding="utf-8")
    err = io.StringIO()
    code = main(
        [str(request), "--policy", str(policy), "--provider", "openai"],
        out=io.StringIO(),
        err=err,
    )
    # Deeper than Python's JSON reader goes: unread, so nothing was checked.
    # Node reads it, and answers the same: deeper than the engine reads.
    assert code == 2
    assert "cannot read the input" in err.getvalue()


@pytest.mark.parametrize(("depth", "code"), [(129, 0), (130, 2)])
def test_a_request_deeper_than_the_engine_reads_exits_two(
    tmp_path: Path, depth: int, code: int
) -> None:
    # The engine reads 128 levels: past them nothing is checked, whatever depth
    # a JSON reader stops at. Node's answer is the same.
    request = tmp_path / "request.json"
    request.write_text("[" * depth + "]" * depth, encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(POLICY), encoding="utf-8")
    err = io.StringIO()
    answer = main(
        [str(request), "--policy", str(policy), "--provider", "openai"],
        out=io.StringIO(),
        err=err,
    )
    assert answer == code
    if code:
        assert "nested deeper than 128 levels" in err.getvalue()


def test_a_policy_with_a_lone_surrogate_exits_two(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text(
        '{"schema": 1, "detectors": {"email": "mask"}, "note": "\\ud800"}',
        encoding="utf-8",
    )
    err = io.StringIO()
    code = main(
        [str(request), "--policy", str(policy), "--provider", "openai"],
        out=io.StringIO(),
        err=err,
    )
    assert code == 2
    assert "the policy is not JSON" in err.getvalue()


def test_bad_arguments_exit_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--unknown"])
    assert raised.value.code == 2
    with pytest.raises(SystemExit) as raised:
        main(["a.json", "--policy", "p.json", "--provider", "nope"])
    assert raised.value.code == 2
    assert "usage: idside-verify-clean" in capsys.readouterr().err
