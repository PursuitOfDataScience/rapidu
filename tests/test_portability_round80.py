"""RD-17 and RD-18, the two findings still open after the 0.4.0 round.

Both are the same shape as most of this directory: a figure the tool already had
in hand, given up somewhere between reading it and reporting it.

* **RD-17** -- resident memory grows with the tree, and the module docstring
  named three growing structures while a fourth, ``watched``, was the largest of
  them on a real tree: 28,180 paths and 8.2 MB walking a conda installation,
  because ``WATCHED_DIR_NAMES`` holds ``__pycache__`` and a Python tree has one
  per package.  ``unreadable_dirs`` had no cap either, while both its siblings
  did.
* **RD-18** -- ``mmlsquota`` names a fileset in every row it returns, and the
  name was read only on ``scope == "fileset"`` rows.  The per-device fan-out asks
  ``-u <user>``, which returns ``USR`` rows and nothing else, so the label was
  applied only on rows the code never fetches: seven filesets on one device
  rendered as seven identical ``midway3_cap`` rows all pointing at ``/project``,
  and ``-Q ~`` and ``-Q /software`` returned byte-identical output.
"""

import os
import pathlib

import pytest

from rapidu import quota as Q
from rapidu import report, ui
from rapidu.walk import SettleCheck, WalkResult, walk


@pytest.fixture(autouse=True)
def _no_memoized_filesets():
    """`read_path_fileset` memoizes per path for the life of the process.

    Correct for a rapidu run and wrong for a test session, where two tests can
    ask about the same path with different fake `mmlsattr` output.
    """
    Q.reset_path_fileset_cache()
    yield
    Q.reset_path_fileset_cache()


# --------------------------------------------------------------------------
# RD-17 -- the two structures that had no bound
# --------------------------------------------------------------------------


def _tree(root, dirs, per_dir_caches=1):
    """``dirs`` directories, each with ``per_dir_caches`` cache-shaped children."""
    for i in range(dirs):
        d = os.path.join(str(root), "pkg%03d" % i)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "mod.py"), "w") as fh:
            fh.write("x")
        for c in range(per_dir_caches):
            cache = os.path.join(d, "__pycache__" if c == 0 else "cache%d" % c)
            os.makedirs(cache, exist_ok=True)
            with open(os.path.join(cache, "mod.pyc"), "w") as fh:
                fh.write("y")


def test_the_watched_map_is_bounded(tmp_path, monkeypatch):
    # 60 cache directories against a cap of 8: the point is that the bound binds,
    # not that this particular tree is large.
    monkeypatch.setattr(Q, "_run", lambda *a, **k: (127, "", "not here"))
    monkeypatch.setattr("rapidu.walk._WATCHED_CAP", 8)
    monkeypatch.setattr("rapidu.walk._WATCHED_CAP_MIN_PER_WORKER", 4)
    _tree(tmp_path, 60)
    res = walk(str(tmp_path), depth=1, threads=2)
    assert res.watched_seen >= 60, "every cache directory was seen"
    assert len(res.watched) <= 8, "and the paths kept are bounded"
    assert res.watched_dropped == res.watched_seen - len(res.watched)


def test_the_bytes_of_what_the_cap_dropped_are_kept(tmp_path, monkeypatch):
    """A bound that loses data silently is worse than no bound.

    A reclaim figure is acted on, so one that quietly excludes 24,000 cache
    directories is a wrong number rather than a partial one.  The paths are what
    cost memory, so the paths are what is given up.
    """
    monkeypatch.setattr(Q, "_run", lambda *a, **k: (127, "", "not here"))
    _tree(tmp_path, 40)

    monkeypatch.setattr("rapidu.walk._WATCHED_CAP", 10_000)
    monkeypatch.setattr("rapidu.walk._WATCHED_CAP_MIN_PER_WORKER", 10_000)
    whole = walk(str(tmp_path), depth=1, threads=1)

    monkeypatch.setattr("rapidu.walk._WATCHED_CAP", 4)
    monkeypatch.setattr("rapidu.walk._WATCHED_CAP_MIN_PER_WORKER", 4)
    capped = walk(str(tmp_path), depth=1, threads=1)

    assert whole.watched_overflow == (0, 0), "nothing dropped when nothing binds"
    assert capped.watched_overflow[1] > 0, "the cap bound"
    tracked_inodes = sum(f for _b, f in capped.watched.values())
    whole_inodes = sum(f for _b, f in whole.watched.values())
    # Every inode charged to a watched directory is still charged to one, whether
    # or not its path survived.
    assert tracked_inodes + capped.watched_overflow[1] == whole_inodes
    tracked_bytes = sum(b for b, _f in capped.watched.values())
    whole_bytes = sum(b for b, _f in whole.watched.values())
    assert tracked_bytes + capped.watched_overflow[0] == whole_bytes
    # And the walk's own totals are untouched by any of it.
    assert (capped.size, capped.inodes) == (whole.size, whole.inodes)


def test_the_cap_does_not_scale_with_the_thread_count(tmp_path, monkeypatch):
    """A bound that a tuning flag multiplies is not a bound.

    A worker's tallies live in a thread-local dict until it exits, so a
    *per-thread* cap of 4096 is 32,768 paths at the default ``-t 8``.  Measured,
    that was the difference between the cap saving 3.7 MB and saving 14 MB on the
    same tree.
    """
    monkeypatch.setattr(Q, "_run", lambda *a, **k: (127, "", "not here"))
    monkeypatch.setattr("rapidu.walk._WATCHED_CAP", 16)
    monkeypatch.setattr("rapidu.walk._WATCHED_CAP_MIN_PER_WORKER", 1)
    _tree(tmp_path, 80)
    one = walk(str(tmp_path), depth=1, threads=1)
    eight = walk(str(tmp_path), depth=1, threads=8)
    # Bounded by the same figure whichever thread count is asked for, allowing
    # for the merge-side cap admitting up to the whole budget again.
    assert len(one.watched) <= 16
    assert len(eight.watched) <= 16


def test_the_report_says_what_the_cap_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(Q, "_run", lambda *a, **k: (127, "", "not here"))
    monkeypatch.setattr("rapidu.walk._WATCHED_CAP", 4)
    monkeypatch.setattr("rapidu.walk._WATCHED_CAP_MIN_PER_WORKER", 4)
    _tree(tmp_path, 40)
    res = walk(str(tmp_path), depth=1, threads=1)
    style = ui.Style(color=False, unicode_ok=True, width=100, depth=8)
    text = "\n".join(report.render_reclaimable(res, style))
    assert "tracking cap" in text, text
    # Silent truncation reads as "covered everything", which is the one thing a
    # bound must not do.
    assert "included in the total" in text


class TestTheOverflowDisclosureCannotBeSkippedOrRenderZero:
    """It sat AFTER ``if not grouped: return []``, one line above itself.

    ``reclaimable_groups`` is built from ``res.watched``, and the cap's whole job
    is to stop filling ``res.watched`` -- so a tree with more cache directories
    than the cap tracks can leave it empty while the overflow holds real bytes,
    and the early return then dropped those bytes out of the report entirely.
    That is exactly the silent truncation the disclosure exists to prevent.
    """

    def _style(self):
        return ui.Style(color=False, unicode_ok=True, width=100, depth=8)

    def _result(self, watched=None, overflow=(0, 0), dropped=0):
        res = WalkResult("/x")
        res.size = 1 << 30
        # `inodes` is derived from these two, so they are what a fixture sets.
        res.files = 9_000
        res.dirs = 1_000
        for path, pair in (watched or {}).items():
            res.watched[path] = pair
        res.watched_overflow = overflow
        res.watched_dropped = dropped
        return res

    def test_an_all_overflow_result_still_reports_the_bytes(self):
        res = self._result(overflow=(700 << 20, 900), dropped=40)
        text = "\n".join(report.render_reclaimable(res, self._style()))
        assert text, "the whole section was returned empty"
        assert "tracking cap" in text
        assert "40 further cache-shaped directories" in text
        assert "700.0 MiB reclaimable in total" in text, text

    def test_nothing_at_all_is_still_nothing(self):
        # The control: an ordinary tree with no caches and no overflow prints no
        # RECLAIMABLE section, which is what the early return is for.
        assert report.render_reclaimable(self._result(), self._style()) == []

    def test_overflow_bytes_with_no_dropped_directory_do_not_print_a_zero(self):
        """The two figures come from different places and need not agree.

        A worker gives up a path once its own thread-local cap is full and adds
        that path's bytes to ``watched_overflow``, but ``watched_dropped`` is
        computed against the MERGED ``watched`` -- so a path another worker
        happened to track leaves overflow bytes with no dropped directory beside
        them, and the message read "... and 0 further cache-shaped directories".
        """
        res = self._result(
            watched={"/x/pkg/__pycache__": (4096, 2)}, overflow=(64 << 20, 12), dropped=0
        )
        text = "\n".join(report.render_reclaimable(res, self._style()))
        assert "tracking cap" in text
        assert "0 further" not in text, text
        assert "further cache-shaped bytes" in text
        # And the bytes still land in the total rather than vanishing.
        assert "64.0 MiB" in text


def test_unreadable_directory_paths_are_sampled_and_the_count_is_not(
    tmp_path,
    monkeypatch,
):
    """The count is the finding; the paths are the sample.

    Every other bound in this walk publishes itself and caps its path list
    (`_UNSTAT_SAMPLE_CAP`, `_CROSSED_SAMPLE_CAP`); this one had no cap at all
    while its own count was read from `len()`.
    """
    monkeypatch.setattr(Q, "_run", lambda *a, **k: (127, "", "not here"))
    monkeypatch.setattr("rapidu.walk._UNREADABLE_SAMPLE_CAP", 3)
    made = []
    for i in range(9):
        d = tmp_path / ("locked%d" % i)
        d.mkdir()
        (d / "inner").mkdir()
        os.chmod(str(d), 0o000)
        made.append(d)
    try:
        res = walk(str(tmp_path), depth=1, threads=1)
        assert res.unreadable_dir_count == 9, "the count is exact"
        assert len(res.unreadable_dirs) <= 3, "the paths are sampled"
        assert res.unreadable_dirs_dropped == 9 - len(res.unreadable_dirs)
        assert not res.complete, "and the walk still knows its total is a floor"
        style = ui.Style(color=False, unicode_ok=True, width=100, depth=8)
        text = "\n".join(report.render_compact(res, SettleCheck(), 10, False, style))
        assert "9" in text and "FLOOR" in text, text
    finally:
        for d in made:
            os.chmod(str(d), 0o700)


def test_a_hand_built_result_cannot_contradict_its_own_count():
    """The count is derived, not stored beside the list it counts.

    A pair of fields that must agree is a pair that eventually does not -- and
    the first casualties were the suite's own fixtures, which append a path and
    nothing else.
    """
    res = WalkResult("/x")
    res.unreadable_dirs.append(("/x/a", "Permission denied"))
    assert res.unreadable_dir_count == 1
    assert not res.complete
    res.watched["/x/cache"] = (512, 1)
    assert res.watched_seen == 1
    assert res.watched_dropped == 0


def test_the_growth_is_disclosed_on_a_large_tree():
    """RD-17's third suggestion, and the one that reaches the 100M-inode case.

    The frontier and the hard-link set cannot be bounded without changing what
    the walk measures, so the honest move is the one this codebase already makes
    for every bound it cannot remove: publish it.
    """
    from rapidu import walk as walkmod
    from rapidu.walk import Entry

    res = WalkResult("/scratch/big")
    res.elapsed = 600.0
    entry = Entry("/scratch/big/d0", True)
    entry.size, entry.files, entry.dirs = 1 << 40, walkmod._MEMORY_NOTE_ENTRIES, 0
    res.dir_agg[entry.path] = entry
    res.finished_tops.add("d0")
    res.size = res.apparent = entry.size
    res.files, res.dirs = walkmod._MEMORY_NOTE_ENTRIES, 1
    style = ui.Style(color=False, unicode_ok=True, width=100, depth=8)
    text = "\n".join(report.render_compact(res, SettleCheck(), 10, False, style))
    assert "of memory for" in text, text

    small = WalkResult("/home/me")
    small.elapsed = 1.0
    small.files, small.dirs = 100, 1
    small.size = small.apparent = 1 << 20
    quiet = "\n".join(report.render_compact(small, SettleCheck(), 10, False, style))
    assert "of memory for" not in quiet, "an ordinary walk says nothing about this"


# --------------------------------------------------------------------------
# RD-18 -- the fileset name, read on rows the fan-out never produces
# --------------------------------------------------------------------------

#: Verbatim header from the cluster, which spells the column in lower case --
#: which is why the parser is keyed case-insensitively.
MM_HEADER = (
    "mmlsquota:user:HEADER:version:reserved:reserved:filesystemName:quotaType:id:"
    "name:blockUsage:blockQuota:blockLimit:blockInDoubt:blockGrace:filesUsage:"
    "filesQuota:filesLimit:filesInDoubt:filesGrace:remarks:filesetname:"
)


def _row(device, fileset, blocks, quota=0, files=100):
    return (
        "mmlsquota:user:0:1:::{d}:USR:940740146:youzhi:{b}:{q}:{q}:0:none:{f}:0:0:0:none::{fs}:"
    ).format(d=device, fs=fileset, b=blocks, q=quota, f=files)


#: The two filesets of `midway2_perf2`, from `mmlsquota -Y -u youzhi
#: midway2_perf2` field 23.  The second is the row that read `12.8 GiB / none`
#: against `/home` while living in `/software`.
TWO_FILESETS = [("home", 1862016, 31457280), ("software", 13445536, 0)]


def _parse(rows, table, points):
    out = MM_HEADER + "\n" + "\n".join(rows) + "\n"
    parsed = Q._parse_mmlsquota(out, table, points)
    Q._mark_ambiguous_filesets(parsed)
    return parsed


def test_a_user_scoped_row_is_still_named_by_its_fileset():
    """Scope and identity are orthogonal.

    ``quotaType`` says whose usage is reported; ``filesetName`` says which slice
    of the filesystem it is reported against.  A ``USR`` row scoped to a fileset
    is still a *fileset's* row -- and gating the name on ``scope == "fileset"``
    put it behind a condition the fan-out can never satisfy.
    """
    rows = _parse(
        [_row("midway2_perf2", fs, b, q) for fs, b, q in TWO_FILESETS],
        {"midway2_perf2": ["/home", "/software"]},
        ["/home", "/software"],
    )
    blocks = [r for r in rows if r.kind == "blocks"]
    assert [r.scope for r in blocks] == ["user", "user"], "the fan-out asks -u"
    assert [r.fileset for r in blocks] == ["home", "software"]
    assert {r.device for r in blocks} == {"midway2_perf2"}


def test_two_filesets_on_one_device_get_their_own_mounts():
    rows = [
        r
        for r in _parse(
            [_row("midway2_perf2", fs, b, q) for fs, b, q in TWO_FILESETS],
            {"midway2_perf2": ["/home", "/software"]},
            ["/home", "/software"],
        )
        if r.kind == "blocks"
    ]
    by_name = {r.fileset: r for r in rows}
    assert by_name["home"].mount == "/home"
    assert by_name["software"].mount == "/software"
    # The 12.8 GiB is not in /home.
    assert by_name["software"].used == 13445536 * 1024


def test_selecting_by_path_now_distinguishes_them():
    """``--help`` promises *"A PATH, if given, selects which rows to show."*

    Rows were keyed by device, so ``rapidu -Q ~`` and ``rapidu -Q /software``
    returned byte-identical output for two different filesets on one device.
    """
    rows = _parse(
        [_row("midway2_perf2", fs, b, q) for fs, b, q in TWO_FILESETS],
        {"midway2_perf2": ["/home", "/software"]},
        ["/home", "/software"],
    )
    snap = Q.QuotaSnapshot(source="mmlsquota")
    snap.rows = rows
    snap.available = True
    assert {r.fileset for r in snap.rows_for_path("/home/youzhi")} == {"home"}
    assert {r.fileset for r in snap.rows_for_path("/software/x")} == {"software"}


def test_a_fileset_under_a_mount_keeps_the_right_branch():
    """Seven ``project-*`` filesets on one device, none of them a mount point.

    ``/project/rcc`` is not in ``/proc/mounts`` -- filesets are not mounts -- so
    the row cannot be placed at its own directory from mount evidence.  What it
    can be placed at is the branch of the filesystem it is in, and the first
    entry of an unordered device list was ``/home``.
    """
    device = "midway3_cap"
    mounts = ["/home", "/project", "/software", "/gpfs/midway3/cap"]
    rows = [
        r
        for r in _parse(
            [
                _row(device, "software", 301056752),
                _row(device, "home", 747504),
                _row(device, "project-rcc", 9832056720),
                _row(device, "project-aaz", 6821808),
            ],
            {device: mounts},
            mounts,
        )
        if r.kind == "blocks"
    ]
    got = {r.fileset: r.mount for r in rows}
    assert got == {
        "software": "/software",
        "home": "/home",
        "project-rcc": "/project",
        "project-aaz": "/project",
    }
    assert not any(r.guessed for r in rows), "every one of these is a mount line"


def test_the_mount_never_depends_on_which_directories_exist_here(monkeypatch):
    """The rule `_guess_mount` settled once: an existing directory is not evidence.

    A first version of this returned the fileset's spelled path when
    ``os.path.isdir`` found it, and the same recorded ``mmlsquota`` output then
    produced ``/project2/rcc`` on a host with such a directory and ``/project2``
    on one without.  The suite caught it -- the same class of defect as RD-10,
    where a test hardcoded the development cluster's identity.
    """
    calls = []
    real = os.path.isdir
    monkeypatch.setattr(os.path, "isdir", lambda p: calls.append(p) or real(p))
    _parse(
        [_row("midway2_cap", "project2-rcc", 2420096)],
        {"midway2_cap": ["/project2"]},
        ["/project2"],
    )
    assert not any(c.startswith("/project2/") for c in calls), calls


def test_a_shared_fileset_name_is_qualified_by_its_device():
    """A fileset name is unique inside a filesystem and not across one.

    On a login node mounting three clusters, ``scratch`` is the fileset name on
    all three -- so naming rows by fileset alone would replace one wrong label
    (the device, everywhere) with a different one (the same word, three times).
    """
    rows = [
        r
        for r in _parse(
            [
                _row("midway2_perf", "scratch", 0),
                _row("midway3_perf", "scratch", 23131203),
                _row("beagle3_perf", "scratch", 0),
                _row("midway2_perf2", "home", 1862016),
            ],
            {
                "midway2_perf": ["/scratch/midway2"],
                "midway3_perf": ["/scratch/midway3"],
                "beagle3_perf": ["/scratch/beagle3"],
                "midway2_perf2": ["/home"],
            },
            ["/scratch/midway2", "/scratch/midway3", "/scratch/beagle3", "/home"],
        )
        if r.kind == "blocks"
    ]
    labels = {r.label for r in rows}
    assert labels == {
        "midway2_perf:scratch",
        "midway3_perf:scratch",
        "beagle3_perf:scratch",
        # Qualified too, though `home` is unique in this set: qualifying only on
        # a visible collision made the label a function of the host's mount
        # table. See `TestALabelDoesNotChangeWithTheHost`.
        "midway2_perf2:home",
    }, labels
    # `fileset` stays the bare name: it is matched against things that are names
    # -- a group, a probe's answer, a row selected by path.
    assert {r.fileset for r in rows} == {"scratch", "home"}


def test_the_panel_shows_the_label():
    rows = _parse(
        [_row("midway2_perf2", fs, b, q) for fs, b, q in TWO_FILESETS],
        {"midway2_perf2": ["/home", "/software"]},
        ["/home", "/software"],
    )
    snap = Q.QuotaSnapshot(source="mmlsquota")
    snap.rows = rows
    snap.available = True
    style = ui.Style(color=False, unicode_ok=True, width=110, depth=8)
    text = "\n".join(report.render_quota(snap, None, style))
    assert "home" in text and "software" in text
    # RD-18 was that the device was the label on every row -- four rows all
    # reading `midway2_perf2`, with the fileset that distinguishes them nowhere
    # on screen. It appears now as the qualifier of a label, which is the
    # opposite: what must hold is that the two filesets are still told apart.
    assert "midway2_perf2:home" in text, text
    assert "midway2_perf2:software" in text, text


# --------------------------------------------------------------------------
# RD-18, second half: the tool can run the command it told the reader to run
# --------------------------------------------------------------------------


class TestTheFilesetIsAskedForRatherThanInferred:
    """*"confirm with `mmlsattr -L`"* was sound advice and the wrong division of
    labour: one unprivileged call per walked path answers exactly the question,
    and its answer matches ``mmlsquota``'s own ``filesetName`` field.

    ``mmlsfileset``, which would enumerate them, is root-only on the site this
    was measured on -- so nothing here builds on enumeration.
    """

    OUT = (
        "file name:            /project/rcc/youzhi\n"
        "metadata replication: 1 max 2\n"
        "storage pool name:    system\n"
        "fileset name:         project-rcc\n"
        "snapshot name:\n"
    )

    def test_it_reads_the_fileset_line(self, monkeypatch):
        monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (0, self.OUT, ""))
        assert Q.read_path_fileset("/project/rcc/youzhi") == "project-rcc"

    def test_the_label_may_be_spelled_any_way(self, monkeypatch):
        for line in ("Fileset Name:  home", "fileset name:\thome", "  FILESET NAME: home"):
            monkeypatch.setattr(Q, "_run", lambda cmd, timeout, ln=line: (0, ln + "\n", ""))
            assert Q.read_path_fileset("/home/me") == "home"

    def test_a_missing_command_is_not_an_answer(self, monkeypatch):
        monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (127, "", "command not found"))
        assert Q.read_path_fileset("/x") is None

    def test_a_zero_exit_with_no_fileset_line_is_not_an_answer(self, monkeypatch):
        # `rc` is not the signal on GPFS -- RD-5, again -- so the answer is
        # asserted positively: the line is there, or there is no answer.
        monkeypatch.setattr(
            Q, "_run", lambda cmd, timeout: (0, "file name: /x\nsnapshot name:\n", "")
        )
        assert Q.read_path_fileset("/x") is None

    def _snapshot(self):
        rows = _parse(
            [_row("midway2_perf2", fs, b, q) for fs, b, q in TWO_FILESETS],
            {"midway2_perf2": ["/home", "/software"]},
            ["/home", "/software"],
        )
        # Both rows deliberately mapped to the same mount, which is the state
        # `_pick_row` exists for: two rows tied on the longest matching prefix.
        for r in rows:
            r.mount = "/home"
            r.mounts = ["/home"]
        snap = Q.QuotaSnapshot(source="mmlsquota")
        snap.rows = rows
        snap.available = True
        return snap

    def test_a_measured_fileset_removes_the_hedge(self, monkeypatch):
        from rapidu import reconcile as rcmod

        monkeypatch.setattr(rcmod.quotamod, "read_path_fileset", lambda p, *a, **k: "software")
        snap = self._snapshot()
        row, notes = rcmod._pick_row(snap.rows_for_path("/home/me"), "blocks", "/home/me")
        assert row is not None and row.fileset == "software"
        joined = " ".join(notes)
        assert "mmlsattr -L" in joined, notes
        # The sentence this replaces: "reconciled against 'X' because it is the
        # most narrowly scoped, not because it is known to be the right one".
        assert "most narrowly scoped" not in joined

    def test_without_it_the_hedge_stands(self, monkeypatch):
        from rapidu import reconcile as rcmod

        monkeypatch.setattr(rcmod.quotamod, "read_path_fileset", lambda p, *a, **k: None)
        snap = self._snapshot()
        row, notes = rcmod._pick_row(snap.rows_for_path("/home/me"), "blocks", "/home/me")
        assert row is not None
        # A confirmation that does not arrive must not remove information.
        assert notes, "the ambiguity is still disclosed"

    def test_it_is_not_asked_when_there_is_nothing_to_settle(self, monkeypatch):
        from rapidu import reconcile as rcmod

        asked = []
        monkeypatch.setattr(
            rcmod.quotamod,
            "read_path_fileset",
            lambda p, *a, **k: asked.append(p) or None,
        )
        rows = _parse(
            [_row("midway2_perf2", "home", 1862016, 31457280)],
            {"midway2_perf2": ["/home"]},
            ["/home"],
        )
        snap = Q.QuotaSnapshot(source="mmlsquota")
        snap.rows = rows
        snap.available = True
        rcmod._pick_row(snap.rows_for_path("/home/me"), "blocks", "/home/me")
        assert asked == [], "one matching row needs no tie broken"


@pytest.mark.parametrize(
    "fileset,mounts,expected",
    [
        # An exact mount for the fileset's own name.
        ("software", ["/home", "/software"], ["/software"]),
        # The site convention that turns `-` into `/`.
        ("project-rcc", ["/project"], ["/project"]),
        # A mount whose basename is the fileset name, wherever it sits.
        ("scratch", ["/gpfs/scratch"], ["/gpfs/scratch"]),
        # Nothing to go on: the caller keeps the device's whole list.
        ("labfs", ["/home", "/software"], []),
    ],
)
def test_the_fileset_to_mount_rules(fileset, mounts, expected):
    assert Q._mounts_for_fileset(fileset, mounts) == expected


class TestAFilesetIsOnlyEverPinnedToItsOwnDevicesMounts:
    """A rule here tested the spelled path against the HOST's mount points.

    Nothing intersected that with the device's own mounts, so a fileset could be
    pinned to a mount belonging to a different filesystem. Measured on a midway3
    login node, where ``midway3_cap`` is mounted at ``/home /project /programs
    /software /gpfs/midway3/cap``::

        _mounts_for_fileset("beagle3",  _mounts_for("midway3_cap"), _mount_points())
            -> ['/beagle3']
        _mounts_for_fileset("project2", ...)
            -> ['/project2']

    ``_parse_mmlsquota`` wrote that onto the row with ``guessed=False``, so
    ``rows_for_path`` matched it and a walk was reconciled against another
    filesystem's quota with no caveat -- the RD-3 mis-attribution this function's
    own docstring cites.
    """

    CAP_MOUNTS = ["/home", "/project", "/programs", "/software", "/gpfs/midway3/cap"]

    @pytest.mark.parametrize("foreign", ["beagle3", "project2", "scratch", "cds"])
    def test_a_foreign_filesystems_mount_is_never_returned(self, foreign):
        got = Q._mounts_for_fileset(foreign, self.CAP_MOUNTS)
        assert all(m in self.CAP_MOUNTS for m in got), got

    def test_the_real_case_returns_nothing_rather_than_the_wrong_thing(self):
        # `[]` means "undecidable", and the caller then keeps the device's whole
        # list -- an unmatched fileset is not evidence for any particular mount.
        assert Q._mounts_for_fileset("beagle3", self.CAP_MOUNTS) == []
        assert Q._mounts_for_fileset("project2", self.CAP_MOUNTS) == []

    def test_the_containing_mount_rule_still_works(self):
        # The rule that was kept: it was already restricted to `device_mounts`.
        assert Q._mounts_for_fileset("project-rcc", self.CAP_MOUNTS) == ["/project"]
        assert Q._mounts_for_fileset("home", self.CAP_MOUNTS) == ["/home"]
        assert Q._mounts_for_fileset("cap", self.CAP_MOUNTS) == ["/gpfs/midway3/cap"]

    def test_a_parsed_row_is_not_given_another_filesystems_mount(self):
        rows = _parse(
            [_row("midway3_cap", "beagle3", 1862016, 31457280)],
            {"midway3_cap": self.CAP_MOUNTS},
            self.CAP_MOUNTS + ["/beagle3", "/project2"],
        )
        for r in rows:
            assert r.mount != "/beagle3"
            assert "/beagle3" not in (r.mounts or [])


class TestTheMeasuredFilesetDoesNotMutateASharedRow:
    """`best.guessed = False` wrote a per-path conclusion onto a shared object.

    The same `QuotaRow` objects are handed to the blocks and the files
    `reconcile()` call and to every path in the `rdu -a p1 p2` loop, so one
    confirmed path permanently suppressed the inferred-mount blocker, the
    ", mount inferred from its name" text and `to_json`'s `mount_guessed` for
    every later consumer -- including paths `mmlsattr` was never asked about.

    It is also wrong on the merits: `guessed` records whether the BACKEND
    published the mount, and `mmlsattr -L` confirms which fileset a PATH is in.
    """

    def _rows(self):
        rows = _parse(
            [_row("midway2_perf2", fs, b, q) for fs, b, q in TWO_FILESETS],
            {"midway2_perf2": ["/home", "/software"]},
            ["/home", "/software"],
        )
        for r in rows:
            r.mount = "/home"
            r.mounts = ["/home"]
            r.guessed = True
        return rows

    def test_guessed_survives_a_measured_fileset(self, monkeypatch):
        from rapidu import reconcile as rcmod

        monkeypatch.setattr(rcmod.quotamod, "read_path_fileset", lambda p, *a, **k: "software")
        rows = self._rows()
        best, notes = rcmod._pick_row(rows, "blocks", "/home/me")
        assert best is not None and best.fileset == "software"
        assert best.guessed is True, "an inferred mount is still an inferred mount"
        # The measured fact travels in the note, scoped to the path it was
        # measured on.
        assert "mmlsattr -L" in " ".join(notes)

    def test_a_second_path_still_sees_the_caveat(self, monkeypatch):
        from rapidu import reconcile as rcmod

        monkeypatch.setattr(
            rcmod.quotamod,
            "read_path_fileset",
            lambda p, *a, **k: "software" if "me" in p else None,
        )
        rows = self._rows()
        rcmod._pick_row(rows, "blocks", "/home/me")
        # `rdu -a /home/me /home/other`: the second path was never confirmed.
        _best, notes = rcmod._pick_row(rows, "blocks", "/home/other")
        assert all(r.guessed for r in rows)
        assert notes, "the ambiguity is still disclosed for the unconfirmed path"


class TestTheFilesetProbeIsMemoizedAndBounded:
    """`_pick_row` is called once per KIND inside cli's per-path loop.

    So an un-memoized probe ran the identical `mmlsattr -L <path>` subprocess
    twice for every path, each at the 45 s default -- up to 90 s added per path on
    a hung GPFS, where before this probe existed there were none.
    """

    def test_the_subprocess_runs_once_per_path(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            Q,
            "_run",
            lambda cmd, timeout: (
                calls.append((cmd, timeout)) or (0, "fileset name: software\n", "")
            ),
        )
        assert Q.read_path_fileset("/home/me") == "software"
        assert Q.read_path_fileset("/home/me") == "software"
        assert Q.read_path_fileset("/home/me") == "software"
        assert len(calls) == 1, calls

    def test_a_negative_answer_is_memoized_too(self, monkeypatch):
        # The common reason for one is that `mmlsattr` is not installed, and
        # re-establishing that per kind buys nothing.
        calls = []
        monkeypatch.setattr(
            Q,
            "_run",
            lambda cmd, timeout: calls.append(cmd) or (127, "", "command not found"),
        )
        assert Q.read_path_fileset("/x") is None
        assert Q.read_path_fileset("/x") is None
        assert len(calls) == 1

    def test_each_path_is_asked_about_separately(self, monkeypatch):
        monkeypatch.setattr(
            Q,
            "_run",
            lambda cmd, timeout: (0, "fileset name: " + cmd[-1].strip("/") + "\n", ""),
        )
        assert Q.read_path_fileset("/a") == "a"
        assert Q.read_path_fileset("/b") == "b"

    def test_the_callers_budget_bounds_it(self, monkeypatch):
        """Every other subprocess in `quota` is bounded by `--quota-timeout`.

        This one used the 45 s module default because `_pick_row` passed nothing.
        """
        seen = []
        monkeypatch.setattr(Q, "_run", lambda cmd, timeout: seen.append(timeout) or (0, "", ""))
        Q.read_path_fileset("/home/me", 3.0)
        assert seen == [3.0]

    def test_reconcile_plumbs_it_down(self, monkeypatch):
        from rapidu import reconcile as rcmod

        seen = []
        monkeypatch.setattr(
            rcmod.quotamod,
            "read_path_fileset",
            lambda p, timeout=None, *a, **k: seen.append(timeout) or None,
        )
        rows = self_rows = _parse(
            [_row("midway2_perf2", fs, b, q) for fs, b, q in TWO_FILESETS],
            {"midway2_perf2": ["/home", "/software"]},
            ["/home", "/software"],
        )
        for r in self_rows:
            r.mount = "/home"
            r.mounts = ["/home"]
        rcmod._pick_row(rows, "blocks", "/home/me", 7.5)
        assert seen == [7.5]


class TestALabelDoesNotChangeWithTheHost:
    """RD-18 review, 2026-08-27: qualify-only-when-ambiguous was host-dependent.

    The device set is enumerated from ``/proc/mounts``, so "is this fileset name
    ambiguous?" is a question about the machine the command ran on. A node
    mounting one filesystem printed ``home``; a login node that also mounts
    ``midway3_cap`` printed ``midway2_perf2:home`` for the same quota on the same
    cluster, and a diff of the two runs showed churn that meant nothing. Same
    class as RD-10 -- output that depends on what the host can see.

    "Key it on the device set the backend enumerated" is no escape on this
    backend: that set *is* the mount table.
    """

    @staticmethod
    def _row(device, fileset):
        return Q.QuotaRow(
            fileset=fileset,
            kind="blocks",
            scope="user",
            used=1,
            soft=2,
            hard=3,
            device=device,
        )

    def test_one_device_and_three_label_the_same_row_alike(self):
        alone = [self._row("midway2_perf2", "home")]
        crowded = [
            self._row("midway2_perf2", "home"),
            self._row("midway3_cap", "home"),
            self._row("beagle3_perf", "scratch"),
        ]
        Q._mark_ambiguous_filesets(alone)
        Q._mark_ambiguous_filesets(crowded)
        assert alone[0].label == crowded[0].label == "midway2_perf2:home"

    def test_the_ambiguity_flag_no_longer_reaches_the_label(self):
        """The mechanism, not the string: nothing may re-wire it back.

        Asserted by flipping the flag rather than by reading the source, so a
        future `label` that consults it any other way still fails here.
        """
        row = self._row("midway3_cap", "scratch")
        row.ambiguous_fileset = False
        bare = row.label
        row.ambiguous_fileset = True
        assert row.label == bare == "midway3_cap:scratch"

    def test_a_row_with_no_device_still_has_a_label(self):
        # The control. Not every backend reports a device -- `quota(1)` does not
        # -- and a label is what the table prints, so it cannot be empty or a
        # stray colon.
        row = Q.QuotaRow(fileset="home", kind="blocks", scope="user", used=1, soft=2, hard=3)
        assert row.label == "home"

    def test_a_device_that_is_its_own_fileset_name_is_not_doubled(self):
        # GPFS filesystems with a single root fileset report both as the same
        # word; `midway3_cap:midway3_cap` is noise, not information.
        row = self._row("midway3_cap", "midway3_cap")
        assert row.label == "midway3_cap"


class TestTheTildeRefusalIsSpelledOnce:
    """Polish pass, 2026-08-27: one sentence, two copies, two protection levels.

    `--quota-only` and the walk path both refuse an unexpandable `~`, and the two
    wrote the same sentence -- one through `ui.encode_safe`, one raw.
    `ui.printable` escapes control bytes but deliberately does NOT escape ordinary
    non-ASCII, so the two differed on exactly the input this package sanitises
    filenames for.

    Nothing crashed, because Python gives `stderr` a `backslashreplace` handler by
    default. That is the interpreter saving one of the two rather than the code
    agreeing with itself.
    """

    ARG = "~中文nosuchuser/x"

    def _run(self, *extra):
        import subprocess
        import sys

        root = pathlib.Path(__file__).resolve().parent.parent
        return subprocess.run(
            [sys.executable, "-m", "rapidu", *extra, self.ARG],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(root),
            env={
                **os.environ,
                "PYTHONPATH": str(root / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "ascii",
                "NO_COLOR": "1",
            },
        )

    def test_both_paths_say_exactly_the_same_thing(self):
        walk = self._run()
        quota = self._run("--quota-only")
        assert "cannot expand" in walk.stderr, walk.stderr
        assert walk.stderr.strip() == quota.stderr.strip(), (
            f"walk said {walk.stderr!r}\nquota said {quota.stderr!r}"
        )

    def test_the_name_is_escaped_rather_than_lost(self):
        # `backslashreplace` would also produce an escape, so this is not by
        # itself proof the code did it -- the equality above is. What this pins is
        # that the sentence stays readable and the bytes do not reach the terminal.
        out = self._run("--quota-only")
        assert "\\u4e2d" in out.stderr, out.stderr
        assert "中" not in out.stderr, "the raw code point reached stderr"

    def test_the_sentence_exists_in_exactly_one_place(self):
        # A mechanism check, not a string check: two copies is what let them drift.
        source = (
            pathlib.Path(__file__).resolve().parent.parent / "src" / "rapidu" / "cli.py"
        ).read_text()
        assert source.count("cannot expand `~`") == 1, "the refusal is spelled more than once again"


class TestANonPositiveSnapshotAgeIsRefused:
    """Polish pass, 2026-08-28: `--max-snapshot-age 0` silently suppressed every finding.

    `reconcile` gates on ``age > max_snapshot_age``, so:

        age=  0.0  cap=  -1.0  -> stale
        age=  5.0  cap=   0.0  -> stale
        age=120.0  cap=   0.0  -> stale

    A cap of 0 makes every snapshot that is not exactly 0.00s old too stale to
    support a finding, and a negative cap makes even a snapshot taken this instant
    too stale. Every quota comparison is blockered out and the run still exits 0.

    It is refused rather than documented because this tool's own convention points
    the other way: `--max-dirs-per-sec 0` disables the rate limit and `--top 0`
    means every entry, so 0 here reads as "do not check the age" while doing the
    exact opposite. `--settle-wait` got the same treatment for consistency with the
    six sibling flags that already refuse a negative.
    """

    @staticmethod
    def _run(*args):
        import subprocess
        import sys

        root = pathlib.Path(__file__).resolve().parent.parent
        return subprocess.run(
            [sys.executable, "-m", "rapidu", *args, str(root / "src"), "-d", "1"],
            capture_output=True,
            text=True,
            timeout=240,
            cwd=str(root),
            env={
                **os.environ,
                "PYTHONPATH": str(root / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "NO_COLOR": "1",
                "COLUMNS": "200",
            },
        )

    @pytest.mark.parametrize("value", ["0", "-1", "-0.5"])
    def test_a_non_positive_cap_is_refused(self, value):
        done = self._run("--max-snapshot-age", value)
        assert done.returncode == 2, done.stdout + done.stderr
        assert "suppresses all of them" in done.stderr, done.stderr
        # And it names the flag that really does skip the comparison.
        assert "--no-quota" in done.stderr

    def test_a_positive_cap_still_runs(self):
        # The control: the guard is a floor, not a new requirement.
        assert self._run("--max-snapshot-age", "300").returncode == 0

    @pytest.mark.parametrize("value,rc", [("-1", 2), ("0", 0), ("0.5", 0)])
    def test_settle_wait_refuses_only_a_negative(self, value, rc):
        # 0 is the documented default and means "skip the wait", so it stays legal.
        assert self._run("--settle-wait", value).returncode == rc

    def test_the_arithmetic_the_guard_is_about(self):
        """The reason, pinned separately from the guard.

        If `reconcile` ever changes to `>=` or clamps the cap itself, this fails
        and the CLI message stops being the right explanation.
        """
        import inspect

        from rapidu import reconcile as rcmod

        # The name on the left changed once, when the walk's own duration was
        # folded in (`age_at_walk_end = age + res.elapsed`). What the CLI guard
        # rests on is the *shape*: a strict `>` against the cap as given, with no
        # clamping, so a cap of 0 flags every snapshot -- which the sum only makes
        # more true. Pinned as the expression rather than the value so a change to
        # `>=`, or a clamp, still fails here.
        assert "age_at_walk_end > max_snapshot_age" in inspect.getsource(rcmod), (
            "the staleness comparison moved; re-check what a 0 cap now means"
        )

    def test_quota_only_with_no_quota_is_refused(self):
        """The contradiction that was silent, unlike its neighbour.

        `--quota-only --deleted-only` has been refused since round six
        ("ask for different reports"), but `--quota-only --no-quota` produced
        output byte-identical to `--quota-only` alone: `--no-quota` was discarded
        without a word. That is the wrong one to drop -- it is passed precisely
        when the backend is slow, hanging or absent, so dropping it queries the
        thing the user asked to avoid.
        """
        import subprocess
        import sys

        root = pathlib.Path(__file__).resolve().parent.parent
        done = subprocess.run(
            [sys.executable, "-m", "rapidu", "--quota-only", "--no-quota", str(root / "src")],
            capture_output=True,
            text=True,
            timeout=240,
            cwd=str(root),
            env={
                **os.environ,
                "PYTHONPATH": str(root / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "NO_COLOR": "1",
                "COLUMNS": "200",
            },
        )
        assert done.returncode == 2, done.stdout + done.stderr
        assert "empty report" in done.stderr, done.stderr

    @pytest.mark.parametrize("flag", ["--quota-only", "--no-quota"])
    def test_either_flag_alone_still_works(self, flag):
        # The control: this refuses a combination, not either flag.
        assert self._run(flag).returncode == 0
