"""One `scandir` per directory, at most one `stat` per entry — the claim, checked.

`walk.py` states the tool's core performance contract where it justifies its thread
defaults, and backs it with a measurement:

    The walk is within 6% of the syscall floor -- a threaded walker that does
    nothing but `scandir` + `fstatat` and count runs the same tree in 35.2s
    against rapidu's 37.4s at 16 threads, with one `scandir` per directory and
    exactly one `fstatat` per entry and no redundancy. There is no bookkeeping
    left to remove [...]

Nothing checked it. A refactor that stat'ed an entry twice would roughly double the
dominant cost of a tool whose entire purpose is finishing before a storage emergency
gets worse, and every existing test would still pass: the *numbers* would all be
right. `test_walk_throttles.py` asserts "one token per directory opened, not per
entry", which is the throttle's accounting, not the walk's syscall economy.

`DirEntry.stat` is a C method and cannot be patched, so this counts rapidu's own
calls by wrapping `os.scandir` and handing back proxies. That measures explicit
`.stat()` calls rather than kernel `fstatat`s — which is the half a refactor can
break, and the half "no redundancy" is about.
"""

import os
import pathlib
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from rapidu import walk as walkmod

_REAL_SCANDIR = os.scandir


class _Counting:
    """Records `.stat()` per entry and `scandir` per directory, thread-safely."""

    def __init__(self) -> None:
        self.stats: dict[str, int] = {}
        self.scandirs: dict[str, int] = {}
        self._lock = threading.Lock()

    def bump(self, table: dict[str, int], key: str) -> None:
        with self._lock:
            table[key] = table.get(key, 0) + 1


class _Proxy:
    """A `DirEntry` that counts the stats rapidu asks of it."""

    __slots__ = ("_entry", "_tally")

    def __init__(self, entry: Any, tally: _Counting) -> None:
        self._entry = entry
        self._tally = tally

    @property
    def name(self) -> str:
        return str(self._entry.name)

    @property
    def path(self) -> str:
        return str(self._entry.path)

    def is_dir(self, **kw: Any) -> bool:
        return bool(self._entry.is_dir(**kw))

    def is_file(self, **kw: Any) -> bool:
        return bool(self._entry.is_file(**kw))

    def is_symlink(self) -> bool:
        return bool(self._entry.is_symlink())

    def inode(self) -> int:
        return int(self._entry.inode())

    def stat(self, **kw: Any) -> Any:
        self._tally.bump(self._tally.stats, self._entry.path)
        return self._entry.stat(**kw)


def _install(tally: _Counting, extra_stats: int = 0) -> Any:
    @contextmanager
    def _scandir(path: Any) -> Iterator[list[_Proxy]]:
        tally.bump(tally.scandirs, str(path))
        with _REAL_SCANDIR(path) as it:
            proxies = [_Proxy(e, tally) for e in it]
        for _ in range(extra_stats):
            for proxy in proxies:
                proxy.stat(follow_symlinks=False)
        yield proxies

    return _scandir


def _tree(root: pathlib.Path) -> None:
    (root / "sub").mkdir()
    (root / "empty").mkdir()
    for index in range(5):
        (root / f"f{index}.bin").write_bytes(b"x" * 50)
    for index in range(3):
        (root / "sub" / f"g{index}.bin").write_bytes(b"x" * 50)
    (root / "link").symlink_to(root / "f0.bin")


def _walk_counting(root: pathlib.Path, *, extra_stats: int = 0, threads: int = 2):
    tally = _Counting()
    os.scandir = _install(tally, extra_stats)  # type: ignore[assignment]
    try:
        res = walkmod.walk(str(root), threads=threads)
    finally:
        os.scandir = _REAL_SCANDIR  # type: ignore[assignment]
    return res, tally


class TestTheWalkDoesNotStatAnythingTwice:
    def test_no_entry_is_stated_more_than_once(self, tmp_path: pathlib.Path) -> None:
        _tree(tmp_path)
        _res, tally = _walk_counting(tmp_path)
        repeats = {p: n for p, n in tally.stats.items() if n > 1}
        assert repeats == {}, repeats

    def test_no_directory_is_scanned_more_than_once(self, tmp_path: pathlib.Path) -> None:
        _tree(tmp_path)
        _res, tally = _walk_counting(tmp_path)
        repeats = {p: n for p, n in tally.scandirs.items() if n > 1}
        assert repeats == {}, repeats

    def test_something_was_actually_counted(self, tmp_path: pathlib.Path) -> None:
        # A silent proxy failure would make both checks above vacuous.
        _tree(tmp_path)
        _res, tally = _walk_counting(tmp_path)
        assert len(tally.stats) >= 8, tally.stats
        assert len(tally.scandirs) >= 3, tally.scandirs

    def test_a_single_thread_walk_is_just_as_economical(self, tmp_path: pathlib.Path) -> None:
        # Concurrency is the lever on wall time, not on the syscall count.
        _tree(tmp_path)
        _res, tally = _walk_counting(tmp_path, threads=1)
        assert max(tally.stats.values()) == 1, tally.stats


class TestControls:
    """The harness itself, and the walk's answers — independent of the economy claim."""

    def test_the_counter_would_notice_a_redundant_stat(self, tmp_path: pathlib.Path) -> None:
        """A guard that cannot fail is not a guard: plant the redundancy."""
        _tree(tmp_path)
        _res, tally = _walk_counting(tmp_path, extra_stats=1)
        repeats = {p: n for p, n in tally.stats.items() if n > 1}
        assert repeats, "the proxy failed to see a deliberate double stat"

    def test_the_proxy_does_not_change_what_the_walk_reports(self, tmp_path: pathlib.Path) -> None:
        _tree(tmp_path)
        proxied, _tally = _walk_counting(tmp_path)
        plain = walkmod.walk(str(tmp_path), threads=2)
        assert (proxied.files, proxied.dirs, proxied.size) == (
            plain.files,
            plain.dirs,
            plain.size,
        )

    def test_the_tree_has_the_shape_these_tests_assume(self, tmp_path: pathlib.Path) -> None:
        _tree(tmp_path)
        assert (tmp_path / "sub" / "g0.bin").is_file()
        assert (tmp_path / "empty").is_dir()
        assert (tmp_path / "link").is_symlink()
