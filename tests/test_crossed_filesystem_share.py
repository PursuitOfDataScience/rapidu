"""How much of the total is on the *other* filesystem, which nothing read.

`walk` sums `(bytes, inodes)` per `st_dev` into `WalkResult.by_dev` for every entry
it stats. Both readers of that table took `len()` of it:

    reconcile.py  `if len(res.by_dev) > 1:` -> "the walk crossed 3 filesystems but
                  the quota governs one; re-run with --one-file-system to compare
                  like with like"
    report.py     `"filesystems": len(res.by_dev)`

So the magnitudes were summed exactly, per device, and then reduced to a count. That
is the one blocker in `reconcile` whose remedy is *do the whole measurement again* --
minutes to hours on the trees this tool is pointed at -- and whether re-running
changes anything depends entirely on how much of the total is out there. The walk
already knew: `-x` decides every skip on `st.st_dev != root_dev` and `by_dev` is
keyed by exactly that, so what `--one-file-system` would have left out is the same
partition of the same table by the same key.

**The loss, measured before the fix.** Two hand-built 14.0 TiB walks over the same
two devices, one holding 58.0 GiB off-root and the other 14.0 TiB -- a 246x
difference in the only figure that decides it -- produced a byte-identical RECONCILE
section (sha256 `feb613215f7a289f` both times) and a byte-identical `--json` document
(`a18e47a1ddace1aa` both times), beside a stated difference of -9.0 TiB that the
section exists to explain. The same pair over a real `walk()` is below: same tree,
same total, same device count, only which subtree reports the foreign device.

**Why this was not caught by the standing inventory.** `by_dev` sits in the
`internal` allow-list of `test_no_walk_or_settle_measurement_is_left_unpublished`,
under "already carried by the figure computed from them". The figure computed from it
was `len()`, which carries the device count and neither magnitude -- unlike its
neighbours `watched_overflow` (both halves published) and `padded_alloc` (carried by
`padding_bytes`). The allow-list entry was the hiding place, not the audit.

**Why `root_dev` had to be stored.** An `st_dev` number is opaque and nothing in
`by_dev` says which key the walk was pointed at, so the split cannot be recovered
from the table alone. It is `None` on a hand-assembled result, and `-c` never reads
`st_dev` for an entry, so `_off_root` refuses in both cases: a zero there would be
the absence of a reading dressed as a reading of zero, which is the line
`_unmeasured` draws for every other figure that mode does not collect.

**One wording note, because it looks arbitrary.** The clause reads "of the walked
total" rather than "of what was counted":
`test_audit_round_six.py::test_no_message_pairs_a_count_with_a_fixed_verb` refuses any
message string that pairs `{}` with `was`. Here the verb agrees with "what" and not
with the figure, so the sweep is wrong -- and a reword costs one word where an
allow-list entry would cost the next reader a judgement call about a rule.
"""

import json
import os
import pathlib
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from rapidu import reconcile as rc
from rapidu import report, ui
from rapidu import walk as walkmod
from rapidu.deleted import DeletedScan
from rapidu.quota import QuotaRow, QuotaSnapshot
from rapidu.walk import SettleCheck, WalkResult

PLAIN = ui.resolve_style("never")

# The two subtrees, and the ~256x difference between them. Small enough that a real
# walk of the pair costs nothing, far enough apart that no rounding can hide it.
BIG_BYTES = 512 * 1024
SMALL_BYTES = 2 * 1024

# Captured at import, before any patching. A test that walks the same tree twice
# patches `os.scandir` twice, and reading the "real" one inside the second patch
# would find the first patch still installed -- the two fakes would compose and
# both subtrees would report the foreign device, which is the one arrangement
# that cannot distinguish anything.
_REAL_SCANDIR = os.scandir


# --- a real walk over a real tree with one subtree on another device ----------
#
# `st_dev` is faked at `scandir`, the way `test_portability_midway2` already does
# it for `-x`: that reproduces midway2's `/scratch` -- a plain directory holding
# three cluster filesystems -- without root and without a second filesystem. The
# difference here is that the fake must cover the whole foreign subtree, not just
# its top entry: `-x` prunes at the boundary and never looks inside, but a walk
# *without* `-x` descends, and those descendants have to be charged to the foreign
# device too or `by_dev` splits in the wrong place.


class _FakeStat(object):
    def __init__(self, real: os.stat_result, dev: int) -> None:
        self._real = real
        self.st_dev = dev

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _FakeEntry(object):
    def __init__(self, entry: Any, dev: int) -> None:
        self._entry = entry
        self._dev = dev

    def __getattr__(self, name: str) -> Any:
        return getattr(self._entry, name)

    def stat(self, follow_symlinks: bool = True) -> Any:
        return _FakeStat(self._entry.stat(follow_symlinks=follow_symlinks), self._dev)


def _build_tree(root: pathlib.Path) -> None:
    """``root/{big,small}``, each a directory holding one file."""
    (root / "big").mkdir(parents=True)
    (root / "big" / "data.bin").write_bytes(b"y" * BIG_BYTES)
    (root / "small").mkdir()
    (root / "small" / "data.bin").write_bytes(b"z" * SMALL_BYTES)


def _patch_dev(root: pathlib.Path, foreign: str, monkeypatch: Any) -> None:
    """Put everything under ``root/foreign`` on another device.

    ONE tree is built and walked twice, with only this choice changed, so the
    two walks share a root path, a file set and a block count. `res.root` is in
    the document -- two trees in two temp directories would differ there and the
    byte-identical comparison below would pass for a reason that has nothing to
    do with the split.
    """
    prefix = str(root / foreign)
    other_dev = os.lstat(str(root)).st_dev + 7

    class _Scan(object):
        """`walk` uses `with scandir(d) as it`, so the context manager is kept."""

        def __init__(self, path: str) -> None:
            self._it = _REAL_SCANDIR(path)

        def __enter__(self) -> "_Scan":
            return self

        def __exit__(self, *exc: Any) -> Any:
            return self._it.__exit__(*exc)

        def __iter__(self) -> Iterator[Any]:
            for entry in self._it:
                path = entry.path
                if path == prefix or path.startswith(prefix + os.sep):
                    yield _FakeEntry(entry, other_dev)
                else:
                    yield entry

    monkeypatch.setattr(walkmod.os, "scandir", _Scan)


@pytest.fixture
def crossing_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "scratch"
    _build_tree(root)
    return root


def _crossing_walk(
    root: pathlib.Path, foreign: str, monkeypatch: Any, count_only: bool = False
) -> WalkResult:
    _patch_dev(root, foreign, monkeypatch)
    return walkmod.walk(str(root), threads=2, depth=1, count_only=count_only)


# --- the comparison the blocker is raised inside -----------------------------


def _snap(root: str, used: int, kind: str = "blocks") -> QuotaSnapshot:
    snap = QuotaSnapshot("test")
    snap.available = True
    # Pinned rather than `time.time()`: the document publishes `snapshot_taken_at`,
    # and a wall clock in it would make any two documents differ for a reason that
    # has nothing to do with what is under test.
    snap.read_at = 1_788_600_000.0
    snap.taken_at = snap.read_at
    snap.rows = [QuotaRow("fs", kind, "user", used, used, used, "", root)]
    return snap


def _settled() -> SettleCheck:
    chk = SettleCheck()
    chk.ran = True
    chk.gap = 60.0  # long enough that a null result means something
    return chk


def _recs(res: WalkResult, snap: QuotaSnapshot, kind: str = "blocks") -> List[Any]:
    return [rc.reconcile(res, _settled(), snap, DeletedScan(), kind)]


def _crossing_blocker(res: WalkResult, snap: QuotaSnapshot, kind: str = "blocks") -> str:
    hits = [b for r in _recs(res, snap, kind) for b in r.blockers if "filesystems" in b]
    assert len(hits) == 1, hits
    return hits[0]


def _terminal(res: WalkResult, snap: QuotaSnapshot, kind: str = "blocks") -> str:
    return "\n".join(report.render_reconcile(_recs(res, snap, kind), PLAIN))


def _document(res: WalkResult, snap: Optional[QuotaSnapshot] = None) -> Dict[str, Any]:
    recs = _recs(res, snap) if snap is not None else None
    return report.to_json(res, _settled(), snap, None, recs)


def _hand_built(
    root: str, split: Dict[int, Tuple[int, int]], root_dev: Optional[int]
) -> WalkResult:
    """The same shape a walk produces, assembled -- for the cases a walk cannot reach."""
    res = WalkResult(root)
    res.files, res.dirs = 1_000_000, 100_000
    res.size = res.apparent = sum(b for b, _ in split.values())
    res.by_uid = {os.getuid(): (res.size, res.inodes)}
    res.by_dev = dict(split)
    res.root_dev = root_dev
    return res


# --- the loss, and its repair -------------------------------------------------


class TestTheOffRootShareReachesTheReader:
    """Two real walks of the same tree, differing only in which side is foreign."""

    def test_the_pair_differs_in_nothing_else_a_surface_can_see(
        self, crossing_root: pathlib.Path, monkeypatch: Any
    ) -> None:
        """The premise. Without this the comparison below proves nothing."""
        away = _crossing_walk(crossing_root, "big", monkeypatch)
        home = _crossing_walk(crossing_root, "small", monkeypatch)
        assert away.size == home.size
        assert away.apparent == home.apparent
        assert away.inodes == home.inodes
        assert len(away.by_dev) == len(home.by_dev) == 2
        assert away.complete and home.complete

    def test_the_off_root_share_is_what_x_would_have_left_out(
        self, crossing_root: pathlib.Path, monkeypatch: Any
    ) -> None:
        away = _crossing_walk(crossing_root, "big", monkeypatch)
        home = _crossing_walk(crossing_root, "small", monkeypatch)
        for res in (away, home):
            dev = res.root_dev
            assert dev is not None
            mine_bytes, mine_inodes = res.by_dev[dev]
            assert res.other_fs_size == res.size - mine_bytes
            assert res.other_fs_inodes == res.inodes - mine_inodes
        # The whole point: one figure, two very different values, same everything else.
        assert away.other_fs_size is not None and home.other_fs_size is not None
        assert away.other_fs_size >= BIG_BYTES
        assert home.other_fs_size < BIG_BYTES // 4
        assert away.other_fs_size > home.other_fs_size * 4

    def test_the_blocker_names_the_figure_that_decides_the_re_run(
        self, crossing_root: pathlib.Path, monkeypatch: Any
    ) -> None:
        away = _crossing_walk(crossing_root, "big", monkeypatch)
        home = _crossing_walk(crossing_root, "small", monkeypatch)
        snap_a = _snap(away.root, away.size * 4)
        snap_h = _snap(home.root, home.size * 4)
        blk_a = _crossing_blocker(away, snap_a)
        blk_h = _crossing_blocker(home, snap_h)
        assert blk_a != blk_h, blk_a
        assert report.human_bytes(away.other_fs_size) in blk_a, blk_a
        assert report.human_bytes(home.other_fs_size) in blk_h, blk_h
        # And the advice it qualifies is still there, unchanged.
        for blk in (blk_a, blk_h):
            assert "--one-file-system" in blk

    def test_the_rendered_section_is_no_longer_byte_identical(
        self, crossing_root: pathlib.Path, monkeypatch: Any
    ) -> None:
        away = _crossing_walk(crossing_root, "big", monkeypatch)
        home = _crossing_walk(crossing_root, "small", monkeypatch)
        text_a = _terminal(away, _snap(away.root, away.size * 4))
        text_h = _terminal(home, _snap(home.root, home.size * 4))
        assert text_a != text_h
        assert report.human_bytes(away.other_fs_size) in text_a
        assert report.human_bytes(home.other_fs_size) in text_h

    def test_the_document_publishes_both_halves(
        self, crossing_root: pathlib.Path, monkeypatch: Any
    ) -> None:
        away = _crossing_walk(crossing_root, "big", monkeypatch)
        home = _crossing_walk(crossing_root, "small", monkeypatch)
        doc_a = _document(away, _snap(away.root, away.size * 4))["walk"]
        doc_h = _document(home, _snap(home.root, home.size * 4))["walk"]
        assert doc_a["other_filesystem_bytes"] == away.other_fs_size
        assert doc_h["other_filesystem_bytes"] == home.other_fs_size
        assert doc_a["other_filesystem_bytes"] != doc_h["other_filesystem_bytes"]
        assert doc_a["other_filesystem_inodes"] == away.other_fs_inodes

    def test_the_whole_document_is_no_longer_byte_identical(
        self, crossing_root: pathlib.Path, monkeypatch: Any
    ) -> None:
        """The form the loss was measured in: `json.dumps` of the two, compared."""
        away = _crossing_walk(crossing_root, "big", monkeypatch)
        home = _crossing_walk(crossing_root, "small", monkeypatch)
        # `elapsed_seconds` is a wall clock, so it is stamped equal here for the
        # same reason `snapshot_taken_at` is pinned: it moves for its own reasons.
        away.elapsed = home.elapsed = 1.0
        a = json.dumps(_document(away, _snap(away.root, away.size * 4)), sort_keys=True)
        h = json.dumps(_document(home, _snap(home.root, home.size * 4)), sort_keys=True)
        assert a != h

    def test_a_files_comparison_is_told_in_inodes_not_bytes(self) -> None:
        """Both halves are needed: an inode count is not derivable from a byte count."""
        split = {0x801: (4 << 30, 40), 0x802: (1 << 30, 4_100)}
        res = _hand_built("/scratch", split, 0x801)
        blk = _crossing_blocker(res, _snap("/scratch", 100, kind="files"), kind="files")
        assert report.human_count(res.other_fs_inodes) in blk, blk
        assert report.human_bytes(res.other_fs_size) not in blk, blk


class TestTheSplitRefusesWhatItCannotMeasure:
    def test_a_stat_free_walk_publishes_null_not_zero(
        self, crossing_root: pathlib.Path, monkeypatch: Any
    ) -> None:
        """`-c` never reads `st_dev` for an entry, so it has no split to report."""
        res = _crossing_walk(crossing_root, "big", monkeypatch, count_only=True)
        assert res.count_only
        assert res.other_fs_size is None
        assert res.other_fs_inodes is None
        walk_doc = _document(res)["walk"]
        assert walk_doc["other_filesystem_bytes"] is None
        assert walk_doc["other_filesystem_inodes"] is None

    def test_a_result_with_no_root_device_keeps_the_bare_sentence(self) -> None:
        """A fixture that fills `by_dev` alone must not have its whole tree called
        foreign -- and the advice it cannot qualify still has to be given."""
        res = _hand_built("/scratch", {1: (500, 5), 2: (500, 5)}, None)
        assert res.other_fs_size is None
        blk = _crossing_blocker(res, _snap("/scratch", 4000))
        assert "--one-file-system" in blk
        assert "would leave out" not in blk, blk

    def test_a_root_device_missing_from_the_table_refuses_too(self) -> None:
        res = _hand_built("/scratch", {1: (500, 5), 2: (500, 5)}, 0xDEAD)
        assert res.other_fs_size is None
        assert res.other_fs_inodes is None

    def test_a_walk_publishes_the_device_it_was_pointed_at(self, tmp_path: pathlib.Path) -> None:
        """The wiring, end to end: nothing else can identify the root's share."""
        (tmp_path / "f.bin").write_bytes(b"x" * 4096)
        res = walkmod.walk(str(tmp_path), threads=2)
        assert res.root_dev == os.lstat(str(tmp_path)).st_dev
        # An ordinary single-filesystem tree measures zero off-root. Zero, not None:
        # the walk did read `st_dev` and there genuinely was nothing out there.
        assert res.other_fs_size == 0
        assert res.other_fs_inodes == 0


# --- controls: true before the fix and after it, and reading none of it -------


class TestControls:
    def test_the_device_count_still_reaches_both_surfaces(
        self, crossing_root: pathlib.Path, monkeypatch: Any
    ) -> None:
        res = _crossing_walk(crossing_root, "big", monkeypatch)
        assert _document(res)["walk"]["filesystems"] == 2
        blk = _crossing_blocker(res, _snap(res.root, res.size * 4))
        assert "crossed 2 filesystems" in blk, blk
        assert "the quota governs one" in blk, blk

    def test_a_single_filesystem_walk_raises_no_crossing_blocker(
        self, tmp_path: pathlib.Path
    ) -> None:
        (tmp_path / "f.bin").write_bytes(b"x" * 4096)
        res = walkmod.walk(str(tmp_path), threads=2)
        assert len(res.by_dev) == 1
        rec = _recs(res, _snap(str(tmp_path), res.size))[0]
        assert not [b for b in rec.blockers if "filesystems" in b], rec.blockers

    def test_the_genuine_case_still_closes(self) -> None:
        """One device, agreeing figures: the verdict this blocker exists to withhold.

        Built by hand and with `root_dev` left alone, so it goes through the branch
        the fix touched without depending on anything the fix added.
        """
        res = WalkResult("/scratch")
        res.files, res.dirs = 9, 1
        res.size = res.apparent = 4096
        res.by_uid = {os.getuid(): (4096, 10)}
        res.by_dev = {0x801: (4096, 10)}
        rec = _recs(res, _snap("/scratch", 4096))[0]
        assert rec.verdict == rc.CLOSES, (rec.verdict, rec.blockers)

    def test_x_still_reports_what_it_skipped(
        self, crossing_root: pathlib.Path, monkeypatch: Any
    ) -> None:
        """The bounded half of the same question, which was never the broken one."""
        _patch_dev(crossing_root, "big", monkeypatch)
        res = walkmod.walk(str(crossing_root), threads=2, depth=1, one_file_system=True)
        assert res.crossed == 1
        assert [os.path.basename(p) for p in res.crossed_paths] == ["big"]
        assert res.complete, "-x is a cap the user asked for, not something that failed"
        walk_doc = _document(res)["walk"]
        assert walk_doc["skipped_other_filesystem"] == 1
        assert walk_doc["filesystems"] == 1

    def test_the_walk_total_still_agrees_with_the_device_table(
        self, crossing_root: pathlib.Path, monkeypatch: Any
    ) -> None:
        """The invariant the split rests on, checked without using the split."""
        res = _crossing_walk(crossing_root, "big", monkeypatch)
        assert sum(b for b, _ in res.by_dev.values()) == res.size
        assert sum(i for _, i in res.by_dev.values()) == res.inodes

    def test_time_is_not_what_makes_two_documents_differ(
        self, crossing_root: pathlib.Path, monkeypatch: Any
    ) -> None:
        """The pinning above is load-bearing, so it is checked rather than assumed:
        one walk rendered twice is byte-identical, which is what makes a difference
        between two of them attributable."""
        res = _crossing_walk(crossing_root, "big", monkeypatch)
        res.elapsed = 1.0
        snap = _snap(res.root, res.size * 4)
        first = json.dumps(_document(res, snap), sort_keys=True)
        time.sleep(0.01)
        assert first == json.dumps(_document(res, snap), sort_keys=True)


@pytest.mark.parametrize("foreign", ["big", "small"])
def test_the_split_survives_a_deeper_foreign_subtree(
    crossing_root: pathlib.Path, monkeypatch: Any, foreign: str
) -> None:
    """Charged for the whole subtree, not just its top entry.

    A walk without `-x` descends past the boundary, so every descendant has to be
    charged to the device it reports. Reading only the boundary entry would put the
    subtree's bytes back on the root's device and report an off-root share of one
    directory inode's worth.
    """
    res = _crossing_walk(crossing_root, foreign, monkeypatch)
    payload = BIG_BYTES if foreign == "big" else SMALL_BYTES
    assert res.other_fs_size is not None
    assert res.other_fs_size >= payload, (res.other_fs_size, payload)
    # A directory plus its one file.
    assert res.other_fs_inodes == 2
