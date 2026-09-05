"""An unreadable directory does not make its subtree a finished measurement.

``finished_tops`` is the whole interrupt guarantee. ``report.render_entries``
ranks with ``finished_only=res.partial`` precisely so that a half-counted
directory never appears in a table, and
``test_audit_round_four.test_an_abandoned_worker_cannot_mark_its_subtrees_finished``
states the promise in one line: *whatever is ranked must be exact*.

Every way a directory scan can stop early sets ``d_truncated`` -- the rate
limiter, the stop check inside the entry loop, the non-``OSError`` guard -- and
that flag is what keeps the scanned directory's top-level ancestor out of
``finished_tops``. A scan that *raised* did not set it, so a subtree holding one
unreadable directory reached ``outstanding[top] == 0`` and was published as
complete while missing everything under that directory.

``res.complete`` was already False, but that is a statement about the *total*.
The per-subtree claim is the one ``finished_tops`` makes, and the table it feeds
is where a reader decides which directory to go and delete.
"""

import errno
import os
import threading

import pytest

from rapidu import report, ui
from rapidu import walk as walkmod
from rapidu.walk import SettleCheck, walk

PLAIN = ui.resolve_style("never", True)

# 40 files directly in the depth-1 child, 20 more inside a subdirectory of it.
# `blocked` therefore holds 62 inodes: itself, 40 files, `locked`, 20 files.
_TOP_FILES = 40
_HIDDEN_FILES = 20
_TRUE_INODES = 1 + _TOP_FILES + 1 + _HIDDEN_FILES


def _flat(lines):
    """One whitespace-normalised string; the report soft-wraps to the terminal."""
    return " ".join(" ".join(lines).split())


@pytest.fixture()
def tree(tmp_path):
    """``blocked/`` with 40 files of its own and ``blocked/locked/`` holding 20."""
    root = tmp_path / "t"
    blocked = root / "blocked"
    locked = blocked / "locked"
    locked.mkdir(parents=True)
    for i in range(_TOP_FILES):
        (blocked / "f{}.bin".format(i)).write_bytes(b"x" * 4096)
    for i in range(_HIDDEN_FILES):
        (locked / "h{}.bin".format(i)).write_bytes(b"x" * 4096)
    return str(root), str(locked)


def _interrupted_walk(monkeypatch, root, victim, deny):
    """Walk ``root``, interrupting it the moment ``victim`` is scanned.

    ``deny`` decides the one difference between the two tests: the scan of
    ``victim`` either raises ``EACCES`` -- what the kernel does for a directory
    the walk may not read -- or succeeds normally. Either way ``stop`` is set at
    that instant, which is what makes ``res.partial`` true and the
    ``finished_only`` filter live.

    Deterministic by construction rather than by timing: one thread, and the
    interrupt is triggered from inside the syscall it has to follow. ``victim``
    is the last directory in the tree, so at the moment it is scanned everything
    else has already been counted and its own ancestor's ``outstanding`` counter
    is one away from zero. Setting ``stop`` from a timer thread instead made this
    reproduce on two runs in three.
    """
    real = os.scandir
    stop = threading.Event()

    def scandir(path):
        if str(path).rstrip(os.sep) == victim:
            stop.set()
            if deny:
                raise PermissionError(errno.EACCES, "Permission denied", victim)
        return real(path)

    monkeypatch.setattr(walkmod.os, "scandir", scandir)
    res = walk(root, threads=1, depth=1, stop=stop)
    assert res.partial, "the fixture must interrupt the walk, or nothing is filtered"
    return res


def test_an_unreadable_directory_cannot_mark_its_subtree_finished(tree, monkeypatch):
    """The defect: `blocked` ranked at 42 inodes against a true 62.

    An `OSError` out of `scandir` is the extreme case of the truncated scan
    `d_truncated` exists for -- zero entries read -- so the subtree is short by
    everything under that directory. It was the one early exit that left the flag
    alone.
    """
    root, locked = tree
    res = _interrupted_walk(monkeypatch, root, locked, deny=True)

    # The fixture has to have actually denied the directory, or this proves
    # nothing about unreadability.
    assert [p for p, _why in res.unreadable_dirs] == [locked], res.unreadable_dirs
    assert not res.complete

    ranked = res.top_dirs(50, "files", finished_only=True)
    assert ranked == [], "ranked a subtree with an unreadable directory in it: {}".format(
        [(os.path.basename(e.path), e.inodes) for e in ranked]
    )
    assert "blocked" not in res.finished_tops, res.finished_tops

    # The reader-facing consequence, and the reason an empty table is the right
    # answer: the caveat says how many entries survived the filter.
    text = _flat(report._hard_warnings(res, SettleCheck(), PLAIN))
    assert "0 top-level entries were walked to completion" in text, text


def test_an_interrupted_walk_still_ranks_a_subtree_it_could_read(tree, monkeypatch):
    """CONTROL -- passes before and after the fix, and is not a mirror of it.

    Same tree, same single thread, same interrupt fired from inside the scan of
    the same directory. The only difference is that the directory is readable, so
    nothing about the subtree is unknown and it must still be ranked -- exactly,
    at all 62 inodes.

    This is what stops the fix from being "publish nothing once a walk is
    interrupted", which would satisfy the test above and destroy the only part of
    an interrupted result that is worth printing.
    """
    root, locked = tree
    res = _interrupted_walk(monkeypatch, root, locked, deny=False)

    assert res.unreadable_dirs == []
    assert res.finished_tops == {"blocked"}
    ranked = res.top_dirs(50, "files", finished_only=True)
    assert [os.path.basename(e.path) for e in ranked] == ["blocked"]
    assert ranked[0].inodes == _TRUE_INODES, "{} inodes, expected {}".format(
        ranked[0].inodes, _TRUE_INODES
    )
