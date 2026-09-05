"""The walk's two throttles and its filesystem boundary.

Three promises the tool makes about how it behaves on someone else's storage,
none of which had been driven end to end:

* ``--max-dirs-per-sec`` must bound the pace and must not touch the *answer*.
* ``-x`` must stop at a mount boundary, name what it refused, and leave it out
  of the totals -- while a symlink pointing at another filesystem is not a
  crossing, because nothing here follows one.
* ``--settle-window`` / :func:`recheck_settling` must say what it actually
  observed when a recent file grew, shrank, or was deleted underneath it.

**Timing assertions here are one-sided by construction.** A token bucket hands
out ``burst`` tokens free and then one per ``1/rate`` seconds, so directory *k*
cannot be opened before ``(k - burst) / rate`` seconds have passed; scheduling on
a shared login node can only make it later. So the limiter is asserted as a
*lower* bound on elapsed time, or -- better -- as a count of how many times a
worker had to park, which no amount of load can perturb.
"""

import os
import subprocess
import threading

import pytest

from rapidu import report, ui
from rapidu import walk as walkmod
from rapidu.walk import SettleCheck, TokenBucket, recheck_settling, walk

PLAIN = ui.resolve_style("never")


# --------------------------------------------------------------------------
# --max-dirs-per-sec
# --------------------------------------------------------------------------


class _CountingStop(threading.Event):
    """A stop event that counts the parks the rate limiter performs on it.

    ``TokenBucket.take`` is the only thing in the walk that calls ``wait`` on the
    stop event (everything else asks ``is_set``), so this is an exact,
    load-independent count of "a worker had to queue for a token" -- which is the
    thing a rate limit is, stated without reference to the clock.
    """

    def __init__(self):
        threading.Event.__init__(self)
        self.parks = 0

    def wait(self, timeout=None):
        self.parks += 1
        return threading.Event.wait(self, timeout)


def _flat_tree(root, ndirs, nfiles=2, payload=4096):
    os.makedirs(root)
    for i in range(ndirs):
        d = os.path.join(root, "d%03d" % i)
        os.makedirs(d)
        for j in range(nfiles):
            with open(os.path.join(d, "f%d" % j), "wb") as handle:
                handle.write(b"x" * payload)
    return root


def _snapshot(res):
    """Every figure the walk publishes, in a comparable form.

    Deliberately built from ``vars`` rather than a hand-written list of fields:
    a figure added later is compared without anyone remembering to add it here,
    which is the whole value of "the limiter does not change the answer".
    ``elapsed`` is excluded because it is the thing being varied.
    """
    out = {}
    for name, value in sorted(vars(res).items()):
        if name == "elapsed":
            continue
        if name == "dir_agg":
            out[name] = sorted((p, e.size, e.files, e.dirs, e.is_dir) for p, e in value.items())
        elif isinstance(value, dict):
            out[name] = sorted(
                (k, tuple(v) if isinstance(v, list) else v) for k, v in value.items()
            )
        elif isinstance(value, (list, set)):
            out[name] = sorted(str(v) for v in value)
        else:
            out[name] = value
    out["_top"] = [(e.path, e.size, e.files, e.dirs) for e in res.top_dirs(50)]
    out["_derived"] = (
        res.inodes,
        res.alloc_unit,
        res.padding,
        res.density_floor,
        res.complete,
        res.watched_seen,
    )
    return out


def test_the_rate_limiter_changes_the_pace_and_not_the_answer(tmp_path):
    """Same tree, four throttles, one set of figures.

    A rate limiter that quietly dropped a directory would still look like a rate
    limiter -- slower, and short by an amount nobody would notice on a real tree.
    Compared over every attribute of :class:`WalkResult`, the per-directory
    aggregates, the ``top_dirs`` ordering and the derived allocation figures.
    """
    root = _flat_tree(str(tmp_path / "t"), ndirs=12)
    unlimited = _snapshot(walk(root, threads=1, depth=2, max_dirs_per_sec=0.0))
    for rate, threads in ((20.0, 1), (40.0, 4), (1e9, 4)):
        limited = _snapshot(walk(root, threads=threads, depth=2, max_dirs_per_sec=rate))
        limited["threads"] = unlimited["threads"]  # the one thing varied on purpose
        differing = [k for k in unlimited if unlimited[k] != limited.get(k)]
        assert not differing, (rate, threads, differing)


def test_a_token_is_charged_per_directory_not_per_entry(tmp_path):
    """The limiter is a bound on *metadata server round trips*, i.e. opens.

    ``walk.py`` checks the stop event only every 1024 entries because
    ``Event.is_set`` is a Python-level call and one directory can hold a million
    names -- so the natural worry is that the token check inherited that
    granularity too, which would make a small limit meaningless inside a wide
    directory. It did not: ``take`` is called once before each ``scandir``, so a
    directory holding 1,500 entries costs exactly one token, the same as an
    empty one.
    """
    root = str(tmp_path / "t")
    wide = os.path.join(root, "wide")
    os.makedirs(wide)
    for i in range(1500):  # > 1024, so the stop check fires inside this one
        open(os.path.join(wide, "f%04d" % i), "wb").close()
    os.makedirs(os.path.join(root, "a", "b"))

    calls = [0]
    real_take = TokenBucket.take

    def counting(self, stop=None):
        calls[0] += 1
        return real_take(self, stop)

    try:
        TokenBucket.take = counting
        res = walk(root, threads=1, depth=1, max_dirs_per_sec=1e6)
    finally:
        TokenBucket.take = real_take

    assert res.files == 1500
    assert res.dirs == 4, "root, wide, a, a/b"
    assert calls[0] == res.dirs, "one token per directory opened, not per entry"


def test_a_limit_larger_than_the_tree_never_parks_a_worker(tmp_path):
    """The no-op case, stated as a count rather than as a wall-clock reading.

    ``capacity = max(rate, 1)``, so a limit at or above the tree's directory
    count is spent entirely out of the initial burst and no directory is ever
    delayed. Asserted through the stop event, which the limiter is the only
    caller of -- an upper bound on elapsed time would be a coin flip on a shared
    login node.
    """
    root = _flat_tree(str(tmp_path / "t"), ndirs=20, nfiles=1)
    stop = _CountingStop()
    res = walk(root, threads=1, depth=1, max_dirs_per_sec=1e6, stop=stop)
    assert res.dirs == 21
    assert stop.parks == 0, "a burst wider than the tree cannot throttle it"
    assert res.complete and not res.partial


def test_a_limit_smaller_than_the_tree_parks_workers_and_bounds_the_pace(tmp_path):
    """The control for the test above, plus the one-sided timing bound.

    21 directories against a burst of 20 leaves exactly one directory to wait
    for a fresh token, so at 20/s the walk cannot finish sooner than 1/20 s.
    Measured at 1.050s for 41 directories at 20/s (predicted 1.050s) and 14.500s
    for 31 at 2/s (predicted 14.500s), identically at 1, 4 and 8 threads -- the
    bucket is shared and locked, so the bound is on the walk and not on a worker.
    """
    root = _flat_tree(str(tmp_path / "t"), ndirs=20, nfiles=1)
    stop = _CountingStop()
    started = walkmod.time.monotonic()
    res = walk(root, threads=1, depth=1, max_dirs_per_sec=20.0, stop=stop)
    elapsed = walkmod.time.monotonic() - started
    assert res.dirs == 21
    assert stop.parks > 0, "the 21st directory has no free token left"
    assert elapsed >= (21 - 20) / 20.0, elapsed
    assert res.complete and not res.partial


def test_a_fractional_limit_is_honoured_rather_than_rounded_to_zero(tmp_path):
    """``--max-dirs-per-sec 0.5`` is half a directory per second, not "disabled".

    0 is the sentinel for off, so every positive value below 1 has to mean
    something: ``capacity`` floors at one token, then one arrives every ``1/rate``
    seconds. Measured on a 4-directory tree: 6.001s at 0.5 (predicted 6.000s) and
    12.001s at 0.25 (predicted 12.000s), and 0.500/s steady-state against a
    requested 0.5. Only the lower bound is asserted.
    """
    root = str(tmp_path / "t")
    os.makedirs(root)
    for i in range(2):
        os.makedirs(os.path.join(root, "d%d" % i))

    started = walkmod.time.monotonic()
    res = walk(root, threads=1, depth=1, max_dirs_per_sec=0.5)
    elapsed = walkmod.time.monotonic() - started
    assert res.dirs == 3
    # capacity is 1, so two of the three directories wait 2s each.
    assert elapsed >= (3 - 1) / 0.5 - 0.05, elapsed
    assert TokenBucket(0.5).capacity == 1.0


# --------------------------------------------------------------------------
# -x / --one-file-system
# --------------------------------------------------------------------------


def _mount_points():
    points = set()
    try:
        with open("/proc/mounts") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) > 2:
                    points.add(fields[1].replace("\\040", " "))
    except OSError:
        return set()
    return points


def _real_boundary():
    """A readable directory that directly contains a real mount point.

    Only the ``-x`` walk is ever run against it, so this stays cheap however
    large the filesystem on the other side is: an unbounded walk of the parent of
    ``/gpfs/midway3`` descends into every home directory on the cluster.

    ``/proc`` and ``/sys`` are ranked last rather than excluded. They do offer
    boundaries -- ``/proc/sys/fs`` holds ``binfmt_misc`` -- but the claim under
    test is about storage, and a pseudo-filesystem is the weaker witness for it;
    a host that has nothing else should still run the test rather than skip.
    """
    parents = {}
    for point in _mount_points():
        parent = os.path.dirname(point.rstrip("/"))
        if parent in ("", "/") or not os.path.isdir(parent):
            continue
        if not os.access(parent, os.R_OK | os.X_OK):
            continue
        try:
            if os.lstat(parent).st_dev == os.lstat(point).st_dev:
                continue
        except OSError:
            continue
        parents[parent] = parents.get(parent, 0) + 1
    if not parents:
        return None

    def rank(parent):
        try:
            own = len(os.listdir(parent))
        except OSError:
            own = 0
        # Most same-filesystem entries first, so the comparison against `du` is
        # over a real tree rather than over a stub directory that holds nothing
        # but mount points and compares 1 against 1.
        return (parent.startswith(("/proc", "/sys")), -own, -parents[parent], parent)

    return sorted(parents, key=rank)[0]


def _du_inodes(path, extra):
    try:
        out = subprocess.check_output(
            ["du", "--inodes", "-s"] + extra + [path],
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        return int(out.split()[0])
    except (IndexError, ValueError):
        return None


def test_one_file_system_counts_what_du_x_counts_at_a_real_mount_boundary():
    """``-x`` against the kernel's own mount table, checked by ``du --inodes -sx``.

    The synthetic fixtures fake ``st_dev`` at ``scandir``, which proves the
    branch and not the boundary. Measured here on ``/dev`` (devtmpfs, holding
    ``/dev/shm``, ``/dev/pts``, ``/dev/mqueue`` and ``/dev/hugepages``):
    ``du --inodes -sx`` 571 against the walk's 571, five devices without ``-x``
    and one with it. Every refused path was a line in ``/proc/mounts`` and was
    genuinely on another device.
    """
    parent = _real_boundary()
    if parent is None:
        pytest.skip("no readable directory on this host directly contains a mount point")
    expected = _du_inodes(parent, ["-x"])
    if expected is None:
        pytest.skip("du --inodes is unavailable here")
    if expected > 500000:
        pytest.skip("{} is too large to measure twice inside a unit test".format(parent))

    res = walk(parent, threads=4, depth=1, one_file_system=True)
    if not res.complete:
        pytest.skip(
            "{} is not fully readable, so du and the walk see different trees".format(parent)
        )
    if expected != _du_inodes(parent, ["-x"]):
        pytest.skip("{} changed while it was being measured twice".format(parent))

    assert res.crossed > 0, parent
    assert res.inodes == expected, (parent, res.inodes, expected)
    assert len(res.by_dev) == 1, (parent, sorted(res.by_dev))
    assert res.complete, "-x is a cap the user asked for, not something that failed"

    points = _mount_points()
    root_dev = os.lstat(parent).st_dev
    for path in res.crossed_paths:
        assert path in points, (path, "named as crossed but not a mount point")
        assert os.lstat(path).st_dev != root_dev, (path, "named as crossed but same device")


def test_a_symlink_to_another_filesystem_is_not_a_crossing(tmp_path):
    """``-x`` reads ``st_dev`` from an ``lstat``, so a link cannot cross anything.

    The one case where ``-x`` could plausibly have been over-eager: a tree full
    of links into other filesystems is normal on a cluster -- ``~/.cache`` and
    ``~/scratch`` are usually links onto a project filesystem -- and counting
    them as refused subtrees would print a "3 entries skipped" caveat about a
    tree that was measured completely. They are symlinks on *this* filesystem,
    which is also what ``du -x`` says.
    """
    root = str(tmp_path / "t")
    os.makedirs(os.path.join(root, "local"))
    with open(os.path.join(root, "local", "f"), "wb") as handle:
        handle.write(b"a" * 8192)
    targets = [p for p in ("/dev/shm", "/proc", "/sys") if os.path.isdir(p)]
    if not targets:
        pytest.skip("no directory on another filesystem to point at")
    elsewhere = 0
    root_dev = os.lstat(root).st_dev
    for i, target in enumerate(targets):
        os.symlink(target, os.path.join(root, "link%d" % i))
        if os.stat(target).st_dev != root_dev:
            elsewhere += 1
    if not elsewhere:
        pytest.skip("every candidate target turned out to be on this filesystem")

    bounded = walk(root, threads=2, depth=1, one_file_system=True)
    plain = walk(root, threads=2, depth=1, one_file_system=False)

    assert bounded.crossed == 0, bounded.crossed_paths
    assert bounded.crossed_paths == []
    assert bounded.symlinks == len(targets)
    assert (bounded.inodes, bounded.files, bounded.dirs, bounded.size) == (
        plain.inodes,
        plain.files,
        plain.dirs,
        plain.size,
    )
    text = "\n".join(report._hard_warnings(bounded, SettleCheck(), PLAIN))
    assert "skipped (-x)" not in text, text


# --------------------------------------------------------------------------
# --settle-window and recheck_settling
# --------------------------------------------------------------------------


def _recent_tree(root, nfiles=8, payload=65536):
    os.makedirs(root)
    for i in range(nfiles):
        with open(os.path.join(root, "f%02d" % i), "wb") as handle:
            handle.write(b"q" * payload)
    return root


def _believable(chk):
    """Give the check a gap long enough that a null result is worth arguing about.

    ``MIN_CONCLUSIVE_GAP_S`` is 5s and the tests must not sleep for it; the gap
    is an input to the judgement, not an observation about the tree, so setting
    it is not faking a measurement.
    """
    chk.gap = 60.0
    return chk


def test_drift_is_signed_and_survives_a_file_disappearing(tmp_path):
    """Grew, shrank, vanished -- the three things a re-stat can find.

    Vanished is the interesting one: the file's walk-time blocks are left out of
    *both* sides of the subtraction, so a deletion cannot masquerade as the tree
    shrinking. Measured: +1,048,576 after appending 64 KiB to each of eight
    files, -1,015,808 after truncating each to 4 KiB, and exactly 0 with three
    of the eight deleted and the rest untouched.
    """
    grew = walk(_recent_tree(str(tmp_path / "grew")), threads=2, depth=1)
    for name in os.listdir(grew.root):
        with open(os.path.join(grew.root, name), "ab") as handle:
            handle.write(b"g" * 65536)
    up = recheck_settling(grew, 0.0)
    assert up.drift > 0 and up.moved and up.gone == 0

    shrank = walk(_recent_tree(str(tmp_path / "shrank"), payload=131072), threads=2, depth=1)
    for name in os.listdir(shrank.root):
        with open(os.path.join(shrank.root, name), "r+b") as handle:
            handle.truncate(4096)
    down = recheck_settling(shrank, 0.0)
    assert down.drift < 0 and down.moved and down.gone == 0

    part = walk(_recent_tree(str(tmp_path / "part")), threads=2, depth=1)
    for name in sorted(os.listdir(part.root))[:3]:
        os.unlink(os.path.join(part.root, name))
    mixed = recheck_settling(part, 0.0)
    assert (mixed.gone, mixed.checked) == (3, 5)
    assert mixed.drift == 0, "a deletion is not the tree shrinking"


def test_a_recheck_whose_whole_sample_was_deleted_is_not_believed(tmp_path):
    """``drift == 0`` because nothing was left to stat is not "settled".

    The tree this tool is pointed at deletes its own recent files: checkpoint
    rotation unlinks the old ``.pt`` while the next one is written, and a
    ``--settle-wait`` long enough to be believed is long enough for the whole
    sample to go. Before this was fixed, ``checked=0 gone=8 drift=0`` with a 60s
    gap reported *"a re-stat 60s later found no change in 0 files; the figure
    looks settled"* and ``"settled": true`` -- the section's strongest claim,
    from a reading that never happened.
    """
    res = walk(_recent_tree(str(tmp_path / "gone")), threads=2, depth=1)
    assert res.recent_files == 8 and len(res.recent_sample) == 8
    for name in list(os.listdir(res.root)):
        os.unlink(os.path.join(res.root, name))

    chk = _believable(recheck_settling(res, 0.0))
    assert (chk.checked, chk.gone, chk.drift) == (0, 8, 0)
    assert chk.recheck_measured_nothing is True
    assert chk.moved is False
    assert chk.conclusive is False, "a re-stat that re-stat'ed nothing cannot answer this"

    text = "\n".join(report.render_settle(res, chk, PLAIN))
    assert "looks settled" not in text, text
    assert "none left to measure" in text, text
    # Waiting longer is not the remedy when the sample is being deleted, and the
    # wait already performed may have been longer than the advice.
    assert "--settle-wait 60" not in text, text
    doc = report.to_json(res, chk, None, None, None)["settling"]
    assert doc["settled"] is None
    assert doc["recheck_measured_nothing"] is True
    assert doc["vanished_files"] == 8 and doc["rechecked"] == 0


def test_a_recheck_that_did_measure_something_is_still_believed(tmp_path):
    """CONTROL. The null result the check exists to deliver must survive.

    Same tree, same 60s gap, nothing deleted: eight files re-stat'ed, no drift,
    and that is a real answer -- ``rdu --settle-wait 120`` on a tree that has not
    moved in two minutes must not be told its own figure is provisional.
    """
    res = walk(_recent_tree(str(tmp_path / "quiet")), threads=2, depth=1)
    chk = _believable(recheck_settling(res, 0.0))
    assert (chk.checked, chk.gone, chk.drift) == (8, 0, 0)
    assert chk.recheck_measured_nothing is False
    assert chk.conclusive is True

    text = "\n".join(report.render_settle(res, chk, PLAIN))
    assert "found no change in 8 files" in text, text
    assert "the figure looks settled" in text, text
    assert report.to_json(res, chk, None, None, None)["settling"]["settled"] is True


def test_nothing_recent_at_all_is_still_a_believable_null():
    """CONTROL. An empty population is not a blind instrument.

    ``recheck_settling`` returns early with ``ran=True`` and ``checked=0`` when
    nothing was written recently, which is the *other* way ``checked == 0``
    arrives -- and there is nothing there to be unsettled, so the null result is
    fine. ``recheck_measured_nothing`` keys off ``gone``, not off ``checked``,
    precisely so this case is untouched.
    """
    chk = _believable(SettleCheck())
    chk.ran = True
    assert (chk.checked, chk.gone) == (0, 0)
    assert chk.recheck_measured_nothing is False
    assert chk.conclusive is True


def test_the_compact_settling_line_discloses_files_that_vanished(tmp_path):
    """``vanished_files`` was in the document and in one of the terminal's two forms.

    The long ``SETTLING`` panel has always ended with "N of them disappeared
    between the walk and the re-stat". The compact one -- which is what prints
    whenever the recent files cannot move the headline, i.e. the common case --
    said nothing, so the run where the population shrank underneath the drift
    figure looked identical to the run where it did not.
    """
    res = walk(_recent_tree(str(tmp_path / "part")), threads=2, depth=1)
    for name in sorted(os.listdir(res.root))[:3]:
        os.unlink(os.path.join(res.root, name))
    chk = recheck_settling(res, 0.0)
    assert (chk.gone, chk.checked) == (3, 5)

    brief = report.render_settle(res, chk, PLAIN)
    assert brief and len(brief) <= 4, brief  # the compact form, not the panel
    text = " ".join(" ".join(brief).split())
    assert "3 disappeared between the walk and the re-stat" in text, text

    believed = " ".join(" ".join(report.render_settle(res, _believable(chk), PLAIN)).split())
    assert "found no change in 5 files" in believed, believed
    assert "3 disappeared between the walk and the re-stat" in believed, believed
    for line in report.render_settle(res, chk, PLAIN):
        assert ui.visible_width(line) <= PLAIN.width, line


def test_the_compact_settling_line_is_unchanged_when_nothing_vanished(tmp_path):
    """CONTROL. The clause is a disclosure, not new furniture on every run."""
    res = walk(_recent_tree(str(tmp_path / "whole")), threads=2, depth=1)
    chk = recheck_settling(res, 0.0)
    assert chk.gone == 0

    text = " ".join(" ".join(report.render_settle(res, chk, PLAIN)).split())
    assert "disappeared" not in text, text
    assert "figure is provisional (--settle-wait 60 to measure)" in text, text

    believed = " ".join(" ".join(report.render_settle(res, _believable(chk), PLAIN)).split())
    assert "disappeared" not in believed, believed
    assert "found no change in 8 files; the figure looks settled" in believed, believed


def test_a_settle_window_of_zero_disables_the_section(tmp_path):
    """0 means off, and says so rather than reporting a settled tree by accident.

    Measured on a tree of six files written moments earlier: window 120 gives
    ``recent_files=5``, ``touched_files=1`` (one file's ctime was bumped by
    ``utime`` without its data changing) and 393,216 recent bytes; window 0 gives
    zeroes, an empty re-stat sample and no ``settling`` lines at all, with
    ``window_seconds: 0.0`` published so the reader can see the check was
    switched off rather than passed.
    """
    root = _recent_tree(str(tmp_path / "w"), nfiles=6)
    live = walk(root, threads=2, depth=1, settle_window=120.0)
    assert live.recent_files == 6 and len(live.recent_sample) == 6
    assert report.render_settle(live, recheck_settling(live, 0.0), PLAIN)

    off = walk(root, threads=2, depth=1, settle_window=0.0)
    assert (off.recent_files, off.touched_files, off.future_files) == (0, 0, 0)
    assert off.recent_sample == []
    assert (off.recent_size, off.recent_apparent) == (0, 0)
    assert report.render_settle(off, recheck_settling(off, 0.0), PLAIN) == []
    # The figures the window does *not* touch are untouched.
    assert (off.size, off.inodes, off.files) == (live.size, live.inodes, live.files)
    doc = report.to_json(off, recheck_settling(off, 0.0), None, None, None)["settling"]
    assert doc["window_seconds"] == 0.0
    assert doc["settled"] is True, "nothing was written recently, so nothing is unsettled"


def test_the_settle_window_shrinks_the_population_it_reports_on(tmp_path):
    """The window is a cutoff, not a switch: an old file falls out of it.

    Deliberately not a timing test -- ``os.utime`` puts the file where the window
    needs it. Note the split the walk keeps: rewinding *both* stamps with
    ``utime`` cannot rewind ``st_ctime``, which the inode records as "now", so a
    backdated file lands in ``touched_files`` rather than ``recent_files``. That
    is the ``chmod -R`` / ``tar -x`` case the two counters exist to tell apart.
    """
    root = _recent_tree(str(tmp_path / "mixed"), nfiles=4)
    old = os.path.join(root, "f00")
    ancient = walkmod.time.time() - 86400
    os.utime(old, (ancient, ancient))

    res = walk(root, threads=2, depth=1, settle_window=120.0)
    assert res.recent_files == 3, "the backdated file is no longer recently written"
    assert res.touched_files == 1, "but its inode changed, and that is a separate count"
    assert len(res.recent_sample) == 4, "the re-stat sample is the union of both"
    assert res.future_files == 0
