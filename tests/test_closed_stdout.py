"""fd 1 CLOSED is not fd 1 redirected, and `sys.stdout` is then `None`.

`rdu . >&-` -- and any daemon, `systemd` unit or cron job started with closed
descriptors -- crashed:

    Traceback (most recent call last):
      File ".../rapidu/__main__.py", line 8, in <module>
      File ".../rapidu/cli.py", line 969, in main
    AttributeError: 'NoneType' object has no attribute 'flush'

The scan itself had already finished.  CPython makes `print()` a silent no-op
when `sys.stdout is None`, so every line of the report went nowhere without
complaint, and the explicit flush -- added so that a report lost to **ENOSPC**
is reported rather than dumped as an "ignored exception" at interpreter
shutdown -- was the single line that assumed a stream was there.

`AttributeError` is not an `OSError`, so none of the three handlers below it
caught this; it left `main` and became exit 1 with a traceback.  Nothing to
flush is not a failure: the caller asked for the report to go nowhere, and it
went nowhere.

The control is the one that matters here.  Deleting the flush would also make
the crash go away, and would silently undo the ENOSPC fix -- so the second
class asserts the flush is still *reached* and still diagnosed.
"""

from __future__ import annotations

import io
import os
import pathlib
import subprocess
import sys

import pytest

from rapidu.cli import EXIT_ERROR, main

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run_with_stdout_closed(*argv: str) -> subprocess.CompletedProcess[str]:
    """Run rapidu with fd 1 genuinely closed.

    Through `sh -c '... >&-'` rather than `subprocess(stdout=...)`, because
    every value that parameter accepts hands the child an *open* descriptor.
    Closing it is the whole condition under test.
    """
    return subprocess.run(
        [
            "/bin/sh",
            "-c",
            'exec "$1" -m rapidu "$2" "$3" "$4" >&-',
            "sh",
            sys.executable,
            *argv,
        ],
        capture_output=True,
        text=True,
        timeout=280,
        cwd=str(ROOT),
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
            "COLUMNS": "200",
        },
    )


class TestAClosedStdoutIsNotACrash:
    def test_a_scan_with_fd_1_closed_exits_cleanly(self):
        done = _run_with_stdout_closed(str(ROOT / "src"), "-d", "1")
        assert "Traceback" not in done.stderr, done.stderr[-600:]
        assert "AttributeError" not in done.stderr, done.stderr[-600:]
        assert done.returncode == 0, done.stderr[-600:]

    def test_the_quota_view_too(self):
        # A second entry point into the same exit path, and the one a cron job
        # actually runs.
        done = _run_with_stdout_closed("--quota-only", "-d", "1", str(ROOT))
        assert "Traceback" not in done.stderr, done.stderr[-600:]
        assert done.returncode in (0, 1), done.stderr[-600:]

    def test_no_output_is_attempted_rather_than_buffered_and_lost(self):
        # `print()` is a no-op with no stdout, so nothing is written anywhere --
        # in particular the report must not fall out on stderr.
        done = _run_with_stdout_closed(str(ROOT / "src"), "-d", "1")
        assert "rapidu" not in done.stderr.replace("rapidu:", ""), done.stderr[-400:]


class TestTheFlushIsStillReachedWhenThereIsAStream:
    """The control: the ENOSPC diagnosis must survive the guard.

    A guard written as "drop the flush" passes the class above and quietly
    restores the bug the flush was added for -- exit **120** and an "ignored
    exception" dump instead of this tool's own error code.
    """

    class _FullDevice(io.StringIO):
        def flush(self) -> None:
            raise OSError(28, "No space left on device")

    def test_a_failing_flush_is_still_diagnosed(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdout", self._FullDevice())
        code = main([str(ROOT / "src"), "-d", "1"])
        monkeypatch.undo()
        assert code == EXIT_ERROR
        assert "cannot write the report" in capsys.readouterr().err

    def test_the_guard_is_a_none_check_and_not_a_deletion(self):
        """Read from the source, so deleting the call fails here too.

        A runtime test cannot tell "flushed" from "nothing needed flushing" on a
        stream that never fails, which is why this one reads the text.
        """
        source = (ROOT / "src" / "rapidu" / "cli.py").read_text()
        assert "if sys.stdout is not None:\n            sys.stdout.flush()" in source, (
            "the guarded flush in `main` is gone; a closed fd 1 and a full "
            "filesystem are both handled by that one line"
        )


@pytest.mark.parametrize("stream", ["stdout"])
def test_the_interpreter_reports_none_for_a_closed_descriptor(stream):
    """The premise, asserted rather than assumed.

    If a future CPython hands back a dummy stream instead of `None`, the guard
    is dead weight and this test says so.
    """
    done = subprocess.run(
        [
            "/bin/sh",
            "-c",
            f'exec "$1" -c "import sys; sys.stderr.write(repr(sys.{stream}))" >&-',
            "sh",
            sys.executable,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.stdout == ""
    assert done.stderr == "None", done.stderr
