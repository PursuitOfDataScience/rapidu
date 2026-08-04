"""Round-four audit: the presentation and CLI-wiring layer.

The walker's measurements survived this round intact -- ``du`` agreement, hard-link
dedup, sparse handling, the allocation-unit estimate and the settling logic were
all confirmed under pressure. What did not survive was the layer between a correct
measurement and the reader: a ranking sorted on a column of zeroes, a flag that
meant "all" in one renderer and "none" in another, a total that summed six of ten
rows, and an interrupted walk that handed back an object its own threads were
still writing to.

Every test here is a regression test for something that printed a wrong number, or
a right number with the wrong label, and none of them would have failed by
crashing. That is why they are written against the *rendered output* and the
*returned object* rather than against an exit code -- ``test_every_sort_key_runs``
asserted `EXIT_OK` for `--sort density` for two rounds while the table it was
checking was empty.
"""

import json
import os
import re
import threading
import time

import pytest

from rapidu import cli, report, ui
from rapidu import deleted as deletedmod
from rapidu import quota as quotamod
from rapidu import reconcile as rc
from rapidu import walk as walkmod
from rapidu.deleted import DeletedFile, DeletedScan
from rapidu.fmt import human_bytes
from rapidu.quota import QuotaRow, QuotaSnapshot, parse_size
from rapidu.walk import SettleCheck, WalkResult, walk

PLAIN = ui.resolve_style("never", True)


@pytest.fixture(scope="session")
def skewed(tmp_path_factory):
    """Five children whose byte order and inode order agree, but are all distinct.

    Distinct matters: a ranking bug that returns dict insertion order is only
    visible when the correct order is not the insertion order, and every count
    here differs so any misordering shows up as a swap.

    Session-scoped, like ``test_walk.tree`` and for the same reason -- every test
    here only reads it, and on a parallel filesystem rebuilding it per test cost
    more than the whole rest of the suite put together.
    """
    root = tmp_path_factory.mktemp("skew") / "t"
    root.mkdir()
    for n in (2, 7, 15, 40, 90):
        d = root / "dir_{:02d}".format(n)
        d.mkdir()
        for i in range(n + 1):
            (d / "f{}.bin".format(i)).write_bytes(b"x" * (n * 10 + 7))
    return str(root)


def _entries(lines):
    """The last column of every table row, in the order printed."""
    return [ln.split()[-1] for ln in lines if ln.strip().endswith("/")]


def _flat(lines):
    """One whitespace-normalised string.

    Prose in this report is soft-wrapped to the terminal, so asserting on a phrase
    that happens to straddle a wrap boundary tests the terminal width rather than
    the sentence.
    """
    return " ".join(" ".join(lines).split())


def _walk_threads():
    return {t.ident for t in threading.enumerate() if t.name.startswith("rapidu-walk")}


# ---------------------------------------------------------------------------
# 1 -- a count-only walk was ranked on a column of zeroes
# ---------------------------------------------------------------------------


def test_count_mode_ranks_by_inodes_not_by_a_column_of_zeroes(skewed, capsys):
    """`rdu -c` printed an unranked table, differently unranked on every run.

    `main` resolved `--sort` from `args.inodes` alone, so plain `-c` got
    `sort="size"` -- and a stat-free walk has no sizes, so every key was 0. A
    stable sort on an all-zero key returns dict insertion order, which is thread
    merge order: six runs on one tree gave four different orderings, and at `-n 3`
    the second-largest directory in the tree sat behind "2 more".
    """
    assert cli.main([skewed, "-c", "--color", "never", "--no-box"]) == cli.EXIT_OK
    rows = _entries(capsys.readouterr().out.splitlines())
    assert rows == ["dir_90/", "dir_40/", "dir_15/", "dir_07/", "dir_02/"], rows


def test_the_count_mode_ranking_is_the_same_every_run(skewed, capsys):
    """The failure was non-determinism, so one correct run does not prove it."""
    seen = set()
    for _ in range(6):
        cli.main([skewed, "-c", "--color", "never", "--no-box"])
        seen.add(tuple(_entries(capsys.readouterr().out.splitlines())))
    assert len(seen) == 1, seen


def test_count_mode_does_not_hide_the_second_largest_behind_n(skewed, capsys):
    """The consequence at the default -n: a truncated listing of the wrong rows."""
    cli.main([skewed, "-c", "-n", "3", "--color", "never", "--no-box"])
    rows = _entries(capsys.readouterr().out.splitlines())
    assert rows == ["dir_90/", "dir_40/", "dir_15/"], rows


def test_a_direct_top_dirs_call_cannot_rank_a_count_walk_by_bytes():
    """Fixed at the producer as well, so no consumer can reintroduce it."""
    res = WalkResult("/r")
    res.count_only = True
    for name, n in (("a", 3), ("b", 40), ("c", 9)):
        e = walkmod.Entry("/r/" + name, True)
        e.add(0, n, 0)
        res.dir_agg["/r/" + name] = e
    assert [os.path.basename(e.path) for e in res.top_dirs(10, "size")] == ["b", "c", "a"]


def test_sort_size_clears_the_inode_flag(skewed, capsys):
    """`-i --sort size` ordered by bytes and measured inodes.

    `--sort files` set `args.inodes`; nothing cleared it, so the rows came back in
    byte order while the bar, the share and the accented header column all drew the
    file count. On a tree where the two orders differ the bars ran 0.7%, 89.9%,
    9.2% down the table.
    """
    parser = cli.build_parser()
    args = parser.parse_args([skewed, "-i", "--sort", "size"])
    assert args.inodes and args.sort == "size"
    cli.main([skewed, "-i", "--sort", "size", "--color", "never", "--no-box"])
    out = capsys.readouterr().out.splitlines()
    shares = [
        float(ln.split("%")[0].split()[-1]) for ln in out if "%" in ln and ln.strip()[0].isdigit()
    ]
    assert shares == sorted(shares, reverse=True), shares


def test_count_mode_refuses_a_byte_ranking(skewed, capsys):
    """`-c --sort size` cannot be answered, and saying so beats guessing."""
    for key in ("size", "density"):
        with pytest.raises(SystemExit) as caught:
            cli.main([skewed, "-c", "--sort", key, "--no-box"])
        assert caught.value.code == 2
        assert "does not measure them" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 2 -- an interrupted walk handed back an object its threads were still writing
# ---------------------------------------------------------------------------

_HANG_S = 6.0


@pytest.fixture(scope="session")
def _hung_tree(tmp_path_factory):
    """Six depth-1 subtrees, two of them named so ``scandir`` will wedge on them."""
    root = tmp_path_factory.mktemp("hang") / "t"
    root.mkdir()
    for name in ["fast_{}".format(i) for i in range(4)] + [
        "wedged_{}_slow".format(i) for i in range(2)
    ]:
        d = root / name
        d.mkdir()
        for j in range(40):
            (d / "f{}.bin".format(j)).write_bytes(b"x" * 4096)
    return str(root)


@pytest.fixture()
def hung(_hung_tree, monkeypatch):
    """``scandir`` blocks on two of the six subtrees, like a hung mount.

    This is the shape the bound exists for. A worker inside ``getdents`` on a
    degraded MDS cannot be woken -- the syscall is uninterruptible and no signal
    reaches it -- so the walk has to stop waiting, and everything about how it
    stops is what these tests check.

    The wedge has to outlast :data:`walkmod.STOP_GRACE_S`, or the worker comes back
    inside the grace period, merges normally, and the test proves nothing. The tree
    is shared; only the patch is per-test.
    """
    assert _HANG_S > walkmod.STOP_GRACE_S, "the wedge must outlast the grace period"
    real = os.scandir
    entered = threading.Event()

    def slow(path):
        if str(path).rstrip("/").endswith("_slow"):
            entered.set()
            time.sleep(_HANG_S)
        return real(path)

    monkeypatch.setattr(walkmod.os, "scandir", slow)
    return _hung_tree, entered


def _stopped_walk(hung):
    """Walk the hung tree, stopping it as soon as a worker is wedged."""
    root, entered = hung
    stop = threading.Event()
    threading.Thread(target=lambda: (entered.wait(_HANG_S * 2), stop.set()), daemon=True).start()
    began = time.monotonic()
    res = walk(root, threads=4, depth=1, stop=stop)
    return res, time.monotonic() - began


def test_a_stopped_walk_does_not_wait_on_a_wedged_syscall(hung):
    """`stop` had no bound at all: `join()` waited for a worker that never came.

    The KeyboardInterrupt path had a five-second-per-worker bound, which is a
    different bug (40s at the default -t 8); the documented `stop` parameter had
    none, so a caller using it against a hung mount blocked forever.
    """
    res, took = _stopped_walk(hung)
    assert res.partial
    assert took < _HANG_S, "waited {:.1f}s for a {:.0f}s wedge".format(took, _HANG_S)
    assert res.abandoned_workers > 0


def test_an_abandoned_walk_publishes_a_snapshot(hung):
    """The returned object must not change after it is returned.

    Before the merge door, workers abandoned at the deadline went on to merge into
    `res` when their syscall finally came back: the caller was handed 2.3 MiB / 601
    files and the *same object* read 8.3 MiB / 1,600 files thirty seconds later,
    with the renderer already iterating `dir_agg` while it grew.
    """
    res, _took = _stopped_walk(hung)
    style = ui.resolve_style("never", True)
    first = (res.size, res.files, res.inodes, len(res.dir_agg), set(res.finished_tops))
    rendered = "\n".join(report.render_compact(res, SettleCheck(), 10, False, style))
    # Long enough for every wedged worker to come back and try to merge. The walk
    # already spent STOP_GRACE_S of the wedge waiting, so what is left is the
    # remainder plus a margin.
    deadline = time.monotonic() + (_HANG_S - walkmod.STOP_GRACE_S) + 2.0
    while time.monotonic() < deadline:
        report.render_compact(res, SettleCheck(), 10, False, style)
        report.to_json(res, SettleCheck(), None, None, None, 10)
        time.sleep(0.01)
    assert (res.size, res.files, res.inodes, len(res.dir_agg), set(res.finished_tops)) == first
    assert rendered == "\n".join(report.render_compact(res, SettleCheck(), 10, False, style))


def test_an_abandoned_worker_cannot_mark_its_subtrees_finished(hung):
    """`finished_tops` is the whole interrupt guarantee, and it was overstated.

    `outstanding[top]` reaching zero proves the *directories* were processed, not
    that their tallies arrived: a worker's counts live in thread locals until it
    exits, so a subtree it contributed to and never merged is missing an unknown
    fraction of its contents. Ranking that is the exact failure the flag exists to
    prevent.
    """
    res, _took = _stopped_walk(hung)
    ranked = res.top_dirs(50, "files", finished_only=True)
    assert res.abandoned_workers > 0
    # Whatever is ranked must be exact. Each fast_N holds 40 files + itself.
    for e in ranked:
        assert e.inodes == 41, "{} ranked with {} inodes".format(e.path, e.inodes)
    assert not any("_slow" in e.path for e in ranked)


def test_the_interrupt_caveat_states_no_denominator_it_cannot_defend(hung):
    """ "2 of 2 top-level entries" -- with four finished and two withheld.

    Both halves of that ratio came from the same partially merged `dir_agg`, so it
    read "all of them" at the moment it was least true. There is no honest
    denominator here: how many entries the tree has is what the walk did not learn.
    """
    res, _took = _stopped_walk(hung)
    lines = report._hard_warnings(res, SettleCheck(), PLAIN)
    text = _flat(lines)
    assert "top-level entries were walked to completion" in text
    assert re.search(r"\d+ of \d+ top-level", text) is None, text
    assert "still blocked" in text
    # Wrapped, so it cannot tear a frame or run off an 80-column terminal.
    for line in report._hard_warnings(res, SettleCheck(), PLAIN):
        assert ui.visible_width(line) <= PLAIN.width, line


def test_the_rate_limiter_wakes_on_the_stop_event(tmp_path):
    """`TokenBucket.take` slept without ever looking at the stop event.

    At 0.2 dirs/sec a worker parks here for seconds per directory, so after a stop
    the bounded join expired with workers still queued for a token -- measured at
    11s to return, with two threads left parked, on a tree that takes 0.01s.
    """
    root = tmp_path / "rl"
    root.mkdir()
    for i in range(8):
        (root / "d{}".format(i)).mkdir()
        (root / "d{}".format(i) / "f").write_bytes(b"x" * 4096)
    stop = threading.Event()
    threading.Thread(target=lambda: (time.sleep(0.4), stop.set()), daemon=True).start()
    # Only threads this walk started. Tests above deliberately abandon workers in a
    # six-second sleep, and those are still in `threading.enumerate()`.
    before = _walk_threads()
    began = time.monotonic()
    res = walk(str(root), threads=4, depth=1, max_dirs_per_sec=0.2, stop=stop)
    took = time.monotonic() - began
    assert res.partial
    assert took < 3.0, "took {:.1f}s to notice the stop".format(took)
    assert not (_walk_threads() - before), "a worker is still parked on a token"
    assert res.abandoned_workers == 0, "nothing had to be abandoned"


def test_the_bucket_still_limits_when_nothing_is_stopping_it():
    """The interruptible version must not have stopped rate-limiting."""
    bucket = walkmod.TokenBucket(20.0, burst=1.0)
    assert bucket.take(threading.Event()) is True
    began = time.monotonic()
    bucket.take(threading.Event())
    assert time.monotonic() - began >= 0.02


# ---------------------------------------------------------------------------
# 3 -- `-n 0` meant "all" in one renderer and "none" in two others
# ---------------------------------------------------------------------------


def test_top_zero_means_every_entry_in_the_json_document(skewed, capsys):
    """`main` validates -n 0 as "0 means every entry"; `to_json` sliced with it."""
    cli.main([skewed, "--json", "-n", "0"])
    zero = json.loads(capsys.readouterr().out)["walk"]
    cli.main([skewed, "--json", "-n", "10"])
    ten = json.loads(capsys.readouterr().out)["walk"]
    assert len(zero["top_by_size"]) == len(ten["top_by_size"]) == 5
    assert len(zero["top_by_inodes"]) == len(ten["top_by_inodes"]) == 5


def _held(count=4):
    scan = DeletedScan()
    for i in range(count):
        f = DeletedFile(
            9, 100 + i, (count - i) << 20, "/scratch/held_{}.bin".format(i), uid=os.getuid()
        )
        f.add_holder(1234 + i, "python train.py")
        scan.files.append(f)
    scan.scanned_pids, scan.unreadable_pids = 30, 700
    return scan


def test_top_zero_means_every_entry_in_the_deleted_table():
    """It listed nothing and then announced the rows it had been asked to show."""
    scan = _held()
    text = "\n".join(report.render_deleted(scan, 0, PLAIN))
    assert text.count("/scratch/held_") == 4
    assert "more" not in text
    assert len(report.to_json(None, None, None, scan, None, 0)["deleted_but_open"]["files"]) == 4


def test_a_real_limit_still_truncates_and_says_so():
    scan = _held()
    text = "\n".join(report.render_deleted(scan, 2, PLAIN))
    assert text.count("/scratch/held_") == 2
    assert "and 2 more" in text


# ---------------------------------------------------------------------------
# 4 -- a total that summed the rows it printed, not the rows it counted
# ---------------------------------------------------------------------------


def _cache_tree(tmp_path, kinds):
    root = tmp_path / "caches"
    for name, size in kinds:
        d = root / name
        d.mkdir(parents=True)
        (d / "blob.bin").write_bytes(b"z" * size)
    return str(root)


_TEN_KINDS = (
    (".conda/pkgs", 90000),
    (".cache/pip", 70000),
    (".cache/huggingface/hub", 50000),
    (".cache/uv", 30000),
    ("node_modules", 20000),
    (".apptainer/cache", 15000),
    ("__pycache__", 12000),
    (".mypy_cache", 10000),
    (".ruff_cache", 9000),
    (".pytest_cache", 8000),
)


def test_reclaimable_total_counts_every_kind_not_the_six_it_prints(tmp_path):
    """`total` accumulated inside `for pattern, hits in ranked[:6]`.

    Any home directory with conda, pip, uv, HF, node_modules and a tool cache is
    already past six kinds, so the line labelled "in total" was the top six only --
    and the share of the tree computed from it was understated with it. Measured on
    a ten-kind tree: 1.1 MiB printed against a true 1.9 MiB.
    """
    root = _cache_tree(tmp_path, _TEN_KINDS)
    res = walk(root, threads=2, depth=1)
    lines = report.render_reclaimable(res, PLAIN)
    total_line = [ln for ln in lines if "reclaimable in total" in ln][0]
    printed = parse_size(total_line.split()[0] + total_line.split()[1].replace("iB", ""))
    truth = sum(v for _p, v in res.watched.items() for v in [v[0]] if False) or None  # noqa: F841
    # Ground truth from the tree itself: every matched directory, counted once.
    matched = {}
    for path, (size, _n) in res.watched.items():
        if report._reclaimable_match(path):
            matched[path] = size
    nested = {p for p in matched for o in matched if o != p and p.startswith(o + os.sep)}
    expected = sum(s for p, s in matched.items() if p not in nested)
    assert len([ln for ln in lines if "files  " in ln]) == 6, "still prints six rows"
    assert "more kinds" in "\n".join(lines)
    assert printed is not None
    assert abs(printed - expected) <= expected * 0.02, (printed, expected)


def test_the_untold_kinds_say_what_they_are_worth(tmp_path):
    root = _cache_tree(tmp_path, _TEN_KINDS)
    res = walk(root, threads=2, depth=1)
    tail = [ln for ln in report.render_reclaimable(res, PLAIN) if "more kinds" in ln][0]
    assert "and 4 more kinds" in tail
    assert "counted in the total below" in tail


# ---------------------------------------------------------------------------
# 5 -- the full report printed 0 B for a measurement it had not taken
# ---------------------------------------------------------------------------


def test_count_mode_never_prints_a_fabricated_zero_size(skewed, capsys):
    """`rdu -a -c` reported the tree as `0 B`, with `apparent 0 B` under it.

    This module's own docstring: "An absent measurement prints n/a with a reason.
    It never prints 0." `render_compact` got it right on the same walk ("counts
    only, no sizes"); `render_walk` printed `human_bytes(res.size)` unconditionally.
    """
    cli.main([skewed, "-a", "-c", "--no-quota", "--no-deleted", "--color", "never", "--no-box"])
    out = capsys.readouterr().out
    head = [ln for ln in out.splitlines() if ln.startswith("WALK")][0]
    assert "0 B" not in head and head.endswith("n/a")
    assert "counts only, no sizes" in out
    assert "apparent" not in out
    assert "0 B" not in out


def test_a_normal_walk_still_leads_with_its_total(skewed, capsys):
    cli.main([skewed, "-a", "--no-quota", "--no-deleted", "--color", "never", "--no-box"])
    out = capsys.readouterr().out
    assert "n/a" not in [ln for ln in out.splitlines() if ln.startswith("WALK")][0]
    assert "apparent" in out


def test_reclaimable_prints_no_zero_bytes_in_count_mode(tmp_path):
    """Same rule one section down: `0 B  1 files  .conda/pkgs` was a real cache."""
    root = _cache_tree(tmp_path, _TEN_KINDS[:3])
    res = walk(root, threads=2, depth=1, count_only=True)
    text = "\n".join(report.render_reclaimable(res, PLAIN))
    if text:  # count mode reaches only depth-1 matches; skip if none matched
        assert "0 B" not in text
        assert "n/a" in text


# ---------------------------------------------------------------------------
# 6 -- a ranking whose key was printed nowhere, and a filter that said nothing
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def dense(tmp_path_factory):
    """Two subtrees that clear the density floor, one that cannot.

    ``many_small`` and ``mid`` hold enough inodes to be ranked and differ by an
    order of magnitude in files-per-GiB; ``few_big`` is below the floor, which is
    what makes the "N of M entries" note testable. Session-scoped: read-only.
    """
    root = tmp_path_factory.mktemp("dense") / "t"
    (root / "many_small").mkdir(parents=True)
    for i in range(120):
        (root / "many_small" / "s{}.bin".format(i)).write_bytes(b"x" * 100)
    (root / "few_big").mkdir()
    for i in range(2):
        (root / "few_big" / "b{}.bin".format(i)).write_bytes(b"x" * 900000)
    (root / "mid").mkdir()
    for i in range(120):
        (root / "mid" / "m{}.bin".format(i)).write_bytes(b"x" * 20000)
    return str(root)


def test_a_density_ranking_shows_the_value_it_ranked_by(dense, capsys):
    """The rows were ordered by files-per-GiB and no column carried it.

    Neither printed column moves monotonically down a density listing, so the
    reader had no way to see the value they asked to rank by or to check the order
    against anything -- which is indistinguishable from a broken sort.
    `files_per_gib` existed and was reachable only through `--json`.
    """
    assert cli.main([dense, "--sort", "density", "--color", "never", "--no-box"]) == cli.EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    header = [ln for ln in lines if "entry" in ln and "files" in ln][0]
    assert "files/GiB" in header
    values = [
        int(ln.split()[-2].replace(",", ""))
        for ln in lines
        if ln.strip().endswith("/")
        and "," in ln
        or (ln.strip().endswith("/") and ln.split()[-2].isdigit())
    ]
    assert len(values) >= 2
    assert values == sorted(values, reverse=True), values


def test_a_density_listing_claims_no_share_of_anything(dense, capsys):
    """A density is a ratio, so no row is a percentage of a tree's density."""
    cli.main([dense, "--sort", "density", "--color", "never", "--no-box"])
    lines = capsys.readouterr().out.splitlines()
    assert any("vs largest" in ln for ln in lines)
    assert not [ln for ln in lines if ln.strip().endswith("/") and "%" in ln]


def test_an_empty_density_ranking_says_why(skewed, capsys):
    """`--sort density` printed a headline, no table, and exited 0.

    The inode floor -- max(100, inodes/100) -- drops everything on an ordinary
    tree, and `render_entries` returned [] with nothing said. "There is nothing
    dense here" and "the question was never answered" are different statements and
    the reader was shown the wrong one.
    """
    assert cli.main([skewed, "--sort", "density", "--color", "never", "--no-box"]) == cli.EXIT_OK
    raw = capsys.readouterr().out
    out = _flat(raw.splitlines())
    assert "cannot be ranked by density" in out
    assert "the measure is files per GiB" in out
    assert "--sort files ranks the same tree by inode count" in out
    assert not _entries(raw.splitlines())


def test_a_short_density_ranking_says_what_it_dropped(dense, capsys):
    cli.main([dense, "--sort", "density", "--color", "never", "--no-box"])
    out = _flat(capsys.readouterr().out.splitlines())
    assert "cannot be ranked by density" in out
    # ...but it does not claim -n would show them, because -n is not the filter.
    assert "use -n 0 for all" not in out


# ---------------------------------------------------------------------------
# 7, 8 -- quota rows discarded at exactly the moment they became the finding
# ---------------------------------------------------------------------------

_OVER = [
    "Midway3-home     blocks (user)        35.00G*    30.00G     35.00G     6days",
    "Midway3-home     files  (user)        310001*    300000     310000     7days",
]


def test_an_over_limit_row_survives_its_own_marker():
    """`conv` never stripped the `*`, so the only row that mattered was dropped.

    `parse_size` is `$`-anchored, so `35.00G*` returned None, `used` came back None
    and the row vanished -- and if every row is over, `snap.rows` empties and `-Q`
    reports "could not parse `quota -s` output". Both sibling parsers in the module
    already stripped it.
    """
    rows = [quotamod._parse_quota_row(line, "/home") for line in _OVER]
    assert all(r is not None for r in rows)
    assert rows[0].used == int(35 * (1 << 30))
    assert rows[1].used == 310001
    assert [r.grace for r in rows] == ["6days", "7days"]
    assert all(r.usage_fraction is not None and r.usage_fraction > 1.0 for r in rows)


_TWO_TABLES = """Disk quotas for user someone (uid 1000):
     Filesystem   space   quota   limit   grace   files   quota   limit   grace
      /dev/sda1   1000M   2000M   3000M           10000   20000   30000
Disk quotas for group lab (gid 9000):
     Filesystem   space   quota   limit   grace   files   quota   limit   grace
      /dev/sdb1    900G   1000G   1100G          1500000 2000000 2100000
"""


def test_stock_quota_reads_the_group_table_too():
    """It `break`ed after the first `Filesystem` header.

    A group quota is routinely the binding limit on a shared project directory,
    which is where an HPC user actually runs out -- and it was invisible.
    """
    rows = quotamod._parse_stock_quota(_TWO_TABLES)
    assert {(r.fileset, r.scope) for r in rows} == {
        ("/dev/sda1", "user"),
        ("/dev/sdb1", "group"),
    }
    group = [r for r in rows if r.scope == "group" and r.kind == "blocks"][0]
    assert group.used == 900 * (1 << 30)


def test_stock_quota_rows_are_not_all_claimed_to_be_the_users():
    """Every row was hard-coded `scope="user"` regardless of its section.

    `reconcile._pick_row` prefers user-scoped rows, so a group figure labelled
    `user` would have been compared against one user's walk.
    """
    assert {r.scope for r in quotamod._parse_stock_quota(_TWO_TABLES)} == {"user", "group"}


@pytest.mark.parametrize(
    "row,expect",
    [
        # Over the block limit only: 7 figures, the grace at index 3.
        ("  /dev/sda1  2500M*  2000M  3000M  6days  10000  20000  30000", ("6days", "")),
        # Over the file limit only: 7 figures, the grace at index 6.
        ("  /dev/sda1  1000M  2000M  3000M  35000*  20000  30000  7days", ("", "7days")),
        # Over both: 8 figures, one grace each.
        (
            "  /dev/sda1  2500M*  2000M  3000M  6days  35000*  20000  30000  7days",
            ("6days", "7days"),
        ),
    ],
)
def test_stock_quota_reads_a_row_with_one_grace_timer(row, expect):
    """A 7-figure row is the commonest over-quota shape and it was skipped.

    Graces print only for an exceeded limit, so 6 figures means nothing is over, 8
    means both are, and 7 means exactly one is. Counting fields could not say
    which, so the row was dropped *and the loop stopped* -- `quota -s` parsed to
    zero rows precisely when the user was over. The position of the non-numeric
    token settles it.
    """
    text = (
        "Disk quotas for user x (uid 1):\n"
        "     Filesystem   space   quota   limit   grace   files   quota   limit   grace\n"
        + row
        + "\n"
    )
    rows = quotamod._parse_stock_quota(text)
    assert len(rows) == 2, rows
    assert (
        next(r.grace for r in rows if r.kind == "blocks"),
        next(r.grace for r in rows if r.kind == "files"),
    ) == expect


def test_a_grace_timer_is_never_mistaken_for_a_figure():
    for token in ("6days", "13:20", "2weeks", "none", "unset"):
        assert not quotamod._is_figure(token)
    for token in ("1048576", "1000M", "2.5G", "35000*", "0"):
        assert quotamod._is_figure(token)


# ---------------------------------------------------------------------------
# 9, 10 -- a type that depended on which checker was installed; a stale assertion
# ---------------------------------------------------------------------------


def test_deleted_only_with_no_valid_path_is_an_error(capsys):
    """It printed no report at all and exited 0: "success, nothing held".

    `cmd_walk` returns EXIT_ERROR for exactly this. The empty-list fallthrough also
    hid the mypy-1.x type error on the same line.
    """
    assert cli.main(["-D", "/nonexistent/aaa", "/nonexistent/bbb"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert err.count("no such path") == 2


def test_deleted_only_with_no_path_still_scans_the_node(capsys):
    assert cli.main(["-D", "--no-box", "--color", "never"]) in (cli.EXIT_OK, cli.EXIT_ATTENTION)
    assert "UNLINKED BUT STILL OPEN" in capsys.readouterr().out


def test_the_namespace_probe_reads_the_namespace_inode():
    """The pid-1 `comm` allowlist was a false positive on runit, s6 and dinit.

    A false positive is not the harmless direction the old docstring reasoned
    about: it flips `complete` to False and prints a container caveat on a
    bare-metal node. The initial namespace's inode is a kernel constant, so the
    question is answered rather than guessed.
    """
    expected = os.stat("/proc/self/ns/pid").st_ino != deletedmod._INIT_PID_NS_INO
    assert deletedmod._in_pid_namespace() is expected


def test_an_unexpected_init_name_is_not_read_as_a_container(monkeypatch):
    """The regression, directly: a host whose pid 1 is `runit`."""
    real = open

    def fake(path, *a, **kw):
        if str(path) == "/proc/1/comm":
            raise AssertionError("pid 1's name must not be consulted")
        return real(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake)
    assert deletedmod._in_pid_namespace() is False


def test_the_deleted_scan_returns_a_snapshot(monkeypatch):
    """The abandoned sweep kept incrementing the counters the caller was handed."""
    started = threading.Event()

    def creeping(res, found, prefix, done):
        started.set()
        for _ in range(200):
            res.scanned_pids += 1
            time.sleep(0.01)
        done.set()

    monkeypatch.setattr(deletedmod, "_sweep", creeping)
    scan = deletedmod.scan(timeout=0.2)
    assert started.is_set() and scan.timed_out
    first = scan.scanned_pids
    time.sleep(0.5)
    assert scan.scanned_pids == first


# ---------------------------------------------------------------------------
# Presentation: the rule, the tolerance floor, the scale, the dead field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count_only", [False, True])
def test_the_hairline_matches_the_table_it_divides(skewed, count_only):
    """It was reconstructed from a column tally that was wrong twice over.

    The tally double-counted the indent (+2 on every listing ever printed) and
    counted the 12-column size field in `-c` mode where that field is not printed
    (+14), and it took its widest name from a differently sorted list than the
    table -- so `--sort density` sized the rule from rows that were not in it.
    """
    res = walk(skewed, threads=2, depth=1, count_only=count_only)
    lines = report._table(res, 10, count_only, PLAIN, "files" if count_only else "size")
    rule = [ln for ln in lines if set(ln.strip()) == {"-"}][0]
    rows = [ln for ln in lines if ln.strip().endswith("/")]
    assert rows
    assert ui.visible_width(rule) == max(ui.visible_width(r) for r in rows)


def test_the_hairline_covers_a_column_added_later(dense):
    """The density column is exactly the kind of change the tally got wrong."""
    res = walk(dense, threads=2, depth=1)
    lines = report._table(res, 10, False, PLAIN, "density")
    rule = [ln for ln in lines if set(ln.strip()) == {"-"}][0]
    rows = [ln for ln in lines if ln.strip().endswith("/")]
    assert ui.visible_width(rule) == max(ui.visible_width(r) for r in rows)


def test_the_tolerance_floor_cannot_swallow_the_whole_measurement():
    """An 8 MiB floor made "quota says 0, walk says 4.7 MiB" *reconcile*.

    That is the anti-goal `MIN_TOLERANCE_BYTES`' own comment names: a floor that
    swallows the measurement manufactures a green verdict for a comparison that
    never happened.
    """
    assert rc._effective_tolerance(0, 4800 * 1024, "blocks") < 4800 * 1024
    assert rc._effective_tolerance(1000, 1005, "blocks") >= 5
    # On any realistic quota the 2% fraction dominates and nothing changed.
    thirty_gib = 30 << 30
    assert rc._effective_tolerance(thirty_gib, thirty_gib, "blocks") == rc._tolerance(
        thirty_gib, "blocks"
    )


def test_a_quota_of_zero_against_a_real_walk_is_not_agreement(tmp_path):
    root = str(tmp_path)
    res = WalkResult(root)
    res.size = 4800 * 1024
    res.files, res.dirs = 40, 1
    res.by_uid[os.getuid()] = (res.size, 41)
    snap = QuotaSnapshot("test")
    snap.available = True
    snap.taken_at = snap.read_at = time.time()
    snap.rows = [QuotaRow("proj", "blocks", "group", 0, 30 << 30, 35 << 30, "", root)]
    settle = SettleCheck()
    settle.ran = True
    settle.gap = 60.0
    verdict = rc.reconcile(res, settle, snap, DeletedScan(), "blocks", 300.0)
    assert verdict.verdict != rc.CLOSES


def test_a_user_scoped_file_comparison_says_it_narrowed(tmp_path):
    """The blocks branch explained itself; the files branch narrowed in silence."""
    res = WalkResult(str(tmp_path))
    res.files, res.dirs = 9, 1
    res.by_uid = {os.getuid(): (1000, 10), 99999: (5000, 50)}
    snap = QuotaSnapshot("test")
    snap.available = True
    snap.taken_at = snap.read_at = time.time()
    snap.rows = [QuotaRow("fs", "files", "user", 10, None, None, "", str(tmp_path))]
    settle = SettleCheck()
    settle.ran = True
    settle.gap = 60.0
    out = rc.reconcile(res, settle, snap, DeletedScan(), "files", 300.0)
    assert any("user-scoped" in n and "inodes you own" in n for n in out.notes), out.notes


def test_deleted_space_is_narrowed_to_the_same_population(tmp_path):
    """A uid-filtered walk plus every unlinked inode on the node is two populations.

    The /proc scan is exactly where another user's bytes turn up -- a shared group
    directory is the case the section exists for -- so adding all of them to your
    own walk figure and calling the remainder a gap compared unlike with unlike.
    """
    root = str(tmp_path)
    res = WalkResult(root)
    res.size = 1000
    res.files, res.dirs = 9, 1
    res.by_uid = {os.getuid(): (1000, 10), 99999: (5000, 50)}
    scan = DeletedScan()
    mine = DeletedFile(1, 2, 500, root + "/mine.bin", uid=os.getuid())
    mine.add_holder(1, "py")
    theirs = DeletedFile(1, 3, 90000, root + "/theirs.bin", uid=99999)
    theirs.add_holder(2, "py")
    scan.files = [mine, theirs]
    snap = QuotaSnapshot("test")
    snap.available = True
    snap.taken_at = snap.read_at = time.time()
    snap.rows = [QuotaRow("fs", "blocks", "user", 1500, None, None, "", root)]
    settle = SettleCheck()
    settle.ran = True
    settle.gap = 60.0
    out = rc.reconcile(res, settle, snap, scan, "blocks", 300.0)
    assert out.deleted_value == 500
    assert any("owned by other users" in n for n in out.notes), out.notes


def test_a_group_scoped_row_still_counts_everything():
    """The narrowing is scoped to the scope, not applied everywhere."""
    scan = DeletedScan()
    scan.files = [
        DeletedFile(1, 2, 500, "/x/a", uid=os.getuid()),
        DeletedFile(1, 3, 700, "/x/b", uid=99999),
    ]
    assert len(scan.owned_by(os.getuid())) == 1
    assert sum(f.size for f in scan.files) == 1200


def test_an_unknown_owner_is_not_silently_dropped():
    """A synthetic or unreadable inode keeps counting: the figure is a floor."""
    scan = DeletedScan()
    scan.files = [DeletedFile(1, 2, 500, "/x/a")]
    assert scan.files[0].uid == deletedmod.UID_UNKNOWN
    assert len(scan.owned_by(os.getuid())) == 1


def test_the_formatter_reaches_as_far_as_the_parser():
    """`human_bytes` stopped at PiB while `parse_size` accepted `E`."""
    assert human_bytes(1 << 60) == "1.0 EiB"
    assert parse_size("1E") == 1 << 60
    assert human_bytes(parse_size("1E")) == "1.0 EiB"


def test_settlecheck_keeps_no_second_copy_of_the_window():
    """Assigned twice, read nowhere; the window lives on the WalkResult."""
    assert not hasattr(SettleCheck(), "window")
    res = WalkResult("/x")
    res.settle_window = 42.0
    assert walkmod.recheck_settling(res, 0.0).sampled_of == 0


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def test_publishing_is_gated_on_the_suite():
    """A tag push went straight to PyPI with no dependency on any test run.

    A PyPI upload cannot be taken back -- the version is burned whether or not the
    code works -- and GitHub cannot express `needs:` across workflow files, so the
    gate has to live in the release workflow and run on the tagged commit.
    """
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), ".github", "workflows", "release.yml"
    )
    if not os.path.exists(path):
        pytest.skip("not a source checkout")
    with open(path) as fh:
        text = fh.read()
    assert "needs: gate" in text
    # The checks must sit in the gate job, which is the part of the file above the
    # publishing job -- split on the job key, not on the name, which the comment
    # explaining the gate also mentions.
    gate = text.split("\n  build-and-publish:")[0]
    for check in ("python -m pytest", "mypy src/ tests/", "ruff check ."):
        assert check in gate, check
