"""brickprint.net.download: urllib -> curl -> wget fallback, .tmp hygiene (all mocked)."""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from brickprint import net

URL = "https://example.invalid/data.csv.gz"


class FakeResponse:
    """urlopen() result: yields `chunks`, then raises `error` (if any)."""

    def __init__(self, chunks=(b"payload",), error=None):
        self._chunks = list(chunks)
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        if self._chunks:
            return self._chunks.pop(0)
        if self._error is not None:
            raise self._error
        return b""


def urlopen_ok(data=(b"hello ", b"world")):
    return mock.patch("urllib.request.urlopen", return_value=FakeResponse(data))


def urlopen_fail_midway(error=ConnectionResetError("reset")):
    return mock.patch("urllib.request.urlopen",
                      return_value=FakeResponse((b"partial-bytes",), error))


def tmp_of(dest):
    return dest.with_name(dest.name + ".tmp")


def only_tool(*tools):
    """shutil.which stand-in: the listed tools exist, nothing else does."""
    return lambda name: f"/usr/bin/{name}" if name in tools else None


def fake_run(write=None, fail=False, seen=None):
    """subprocess.run stand-in: the tool's -o / -O target is the .tmp file."""
    def run(cmd, **kw):
        target = cmd[cmd.index("-o") + 1] if "-o" in cmd else cmd[cmd.index("-O") + 1]
        if seen is not None:
            seen.append((cmd[0], target))
        if write is not None:
            with open(target, "wb") as f:
                f.write(write)
        if fail:
            raise subprocess.CalledProcessError(22, cmd)
        return subprocess.CompletedProcess(cmd, 0)
    return run


def test_success_via_urllib(tmp_path):
    dest = tmp_path / "sub" / "data.csv.gz"            # parent is created
    with urlopen_ok() as uo, mock.patch("subprocess.run") as run:
        net.download(URL, dest, timeout=7)
    assert dest.read_bytes() == b"hello world"
    assert not tmp_of(dest).exists()
    run.assert_not_called()
    req = uo.call_args[0][0]
    assert req.full_url == URL
    assert req.get_header("User-agent") == "brickprint/1.0"
    assert uo.call_args[1]["timeout"] == 7


def test_success_replaces_existing_dest(tmp_path):
    dest = tmp_path / "d.bin"
    dest.write_bytes(b"old")
    with urlopen_ok((b"new",)):
        net.download(URL, dest)
    assert dest.read_bytes() == b"new"


def test_urllib_fails_midway_partial_removed_before_curl(tmp_path):
    dest = tmp_path / "d.bin"
    tmp = tmp_of(dest)
    seen_tmp_before_curl = []

    def run(cmd, **kw):
        seen_tmp_before_curl.append(tmp.exists())      # must have been cleaned already
        return fake_run(write=b"from-curl")(cmd, **kw)

    with urlopen_fail_midway(), \
            mock.patch("shutil.which", only_tool("curl")), \
            mock.patch("subprocess.run", side_effect=run) as sp:
        net.download(URL, dest)
    assert seen_tmp_before_curl == [False]
    assert sp.call_args[0][0][0] == "curl"
    assert dest.read_bytes() == b"from-curl"
    assert not tmp.exists()


def test_curl_missing_falls_through_to_wget(tmp_path):
    dest = tmp_path / "d.bin"
    seen = []
    with urlopen_fail_midway(), \
            mock.patch("shutil.which", only_tool("wget")), \
            mock.patch("subprocess.run", side_effect=fake_run(write=b"wget-data", seen=seen)):
        net.download(URL, dest)
    assert [tool for tool, _ in seen] == ["wget"]
    assert seen[0][1] == str(tmp_of(dest))
    assert dest.read_bytes() == b"wget-data"
    assert not tmp_of(dest).exists()


def test_curl_partial_then_failure_is_cleaned_before_wget(tmp_path):
    dest = tmp_path / "d.bin"
    tmp = tmp_of(dest)
    calls = []

    def run(cmd, **kw):
        calls.append((cmd[0], tmp.exists()))
        if cmd[0] == "curl":
            return fake_run(write=b"half", fail=True)(cmd, **kw)
        return fake_run(write=b"wget-ok")(cmd, **kw)

    with urlopen_fail_midway(), \
            mock.patch("shutil.which", only_tool("curl", "wget")), \
            mock.patch("subprocess.run", side_effect=run):
        net.download(URL, dest)
    assert calls == [("curl", False), ("wget", False)]   # fallback order, clean slate each time
    assert dest.read_bytes() == b"wget-ok"
    assert not tmp.exists()


def test_all_methods_fail_raises_and_leaves_no_tmp_and_keeps_existing_dest(tmp_path):
    dest = tmp_path / "d.bin"
    dest.write_bytes(b"precious")
    with urlopen_fail_midway(OSError("boom")), \
            mock.patch("shutil.which", only_tool("curl", "wget")), \
            mock.patch("subprocess.run", side_effect=fake_run(write=b"junk", fail=True)):
        with pytest.raises(RuntimeError) as err:
            net.download(URL, dest)
    assert str(err.value).startswith(f"download failed for {URL}: ")
    assert "22" in str(err.value)                        # last error = wget's CalledProcessError
    assert dest.read_bytes() == b"precious"
    assert not tmp_of(dest).exists()


def test_no_curl_or_wget_available_reports_urllib_error(tmp_path):
    dest = tmp_path / "d.bin"
    with urlopen_fail_midway(OSError("dns failure")), \
            mock.patch("shutil.which", return_value=None), \
            mock.patch("subprocess.run") as run:
        with pytest.raises(RuntimeError, match="download failed for .*dns failure"):
            net.download(URL, dest)
    run.assert_not_called()
    assert not dest.exists()
    assert not tmp_of(dest).exists()


def test_urlopen_itself_failing_falls_back(tmp_path):
    dest = tmp_path / "d.bin"
    with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")), \
            mock.patch("shutil.which", only_tool("curl")), \
            mock.patch("subprocess.run", side_effect=fake_run(write=b"curl-data")):
        net.download(URL, dest)
    assert dest.read_bytes() == b"curl-data"


def test_curl_timeout_counts_as_failure(tmp_path):
    dest = tmp_path / "d.bin"

    def run(cmd, **kw):
        fake_run(write=b"partial")(cmd, **kw)
        raise subprocess.TimeoutExpired(cmd, 1)

    with mock.patch("urllib.request.urlopen", side_effect=OSError("x")), \
            mock.patch("shutil.which", only_tool("curl")), \
            mock.patch("subprocess.run", side_effect=run):
        with pytest.raises(RuntimeError):
            net.download(URL, dest)
    assert not tmp_of(dest).exists()


def test_keyboard_interrupt_during_write_removes_tmp_and_propagates(tmp_path):
    dest = tmp_path / "d.bin"
    dest.write_bytes(b"keep")
    with urlopen_fail_midway(KeyboardInterrupt()), \
            mock.patch("subprocess.run") as run:
        with pytest.raises(KeyboardInterrupt):
            net.download(URL, dest)
    run.assert_not_called()
    assert not tmp_of(dest).exists()
    assert dest.read_bytes() == b"keep"


def test_keyboard_interrupt_during_curl_removes_tmp(tmp_path):
    dest = tmp_path / "d.bin"

    def run(cmd, **kw):
        fake_run(write=b"half")(cmd, **kw)
        raise KeyboardInterrupt()

    with mock.patch("urllib.request.urlopen", side_effect=OSError("x")), \
            mock.patch("shutil.which", only_tool("curl")), \
            mock.patch("subprocess.run", side_effect=run):
        with pytest.raises(KeyboardInterrupt):
            net.download(URL, dest)
    assert not tmp_of(dest).exists()
    assert not dest.exists()


def test_stale_tmp_from_a_crashed_run_is_not_reused(tmp_path):
    dest = tmp_path / "d.bin"
    tmp_of(dest).write_bytes(b"stale-garbage-that-is-longer-than-the-new-payload")
    with urlopen_ok((b"fresh",)):
        net.download(URL, dest)
    assert dest.read_bytes() == b"fresh"
    assert not tmp_of(dest).exists()
