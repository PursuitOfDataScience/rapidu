"""Round-five: five filed issues, all in the layer that *describes* a measurement.

The walk itself was not wrong in any of these. What was wrong was the sentence
printed next to it, the ordering chosen among equals, and one document key that
answered a question the CLI refuses to answer at all:

* the density listing's only "rows are missing" note blamed the inode floor for
  what ``-n`` had cut, and pointed away from the flag that would show them;
* ``rdu -a -c`` reported every cache as ``0 files`` and ``0.0% of the tree``,
  because the inode source it reads is filled only on the stat path;
* ``top_dirs`` had no tiebreaker, so a tree of equal-sized directories ordered
  itself by thread-merge order -- a report that will not ``diff`` against itself,
  and under ``-n`` a different *subset* on every run;
* ``--json -c`` published ``top_by_size`` and ``top_by_density``, both of which
  the CLI refuses for the same walk, with ``"bytes": 0`` on every row;
* ``1 files``, and "and are listed below" naming a table that is not there.

Written against rendered output and the returned object, for the reason
``test_audit_round_four`` states: none of these would ever fail by crashing.
"""

import json
import os

import pytest

from rapidu import cli, report, ui
from rapidu import walk as walkmod
from rapidu.fmt import plural
from rapidu.walk import walk

PLAIN = ui.resolve_style("never", True)


def _flat(text):
    """Prose wrapped to the terminal, joined back into one line."""
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# 1 -- the floor note blamed the inode floor for -n truncation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def all_clear_the_floor(tmp_path_factory):
    """Four depth-1 entries, every one of them above the density floor."""
    root = str(tmp_path_factory.mktemp("dense"))
    for name in ("alpha", "beta", "gamma", "delta"):
        d = os.path.join(root, name)
        os.makedirs(d)
        for i in range(150):
            with open(os.path.join(d, "f%d" % i), "wb") as fh:
                fh.write(b"x" * 400)
    return root


def _density_lines(root, limit):
    res = walk(root, threads=2, depth=1)
    assert len([e for e in res.dir_agg.values() if e.path != root]) == 4
    qualifying = len(res.top_dirs(10**9, "density"))
    assert qualifying == 4, "fixture must put every entry above the floor"
    return _flat("\n".join(report._table(res, limit, False, PLAIN, "density")))


def test_the_floor_is_not_blamed_for_what_n_cut(all_clear_the_floor):
    """ "3 of 4 entries hold fewer than 100 files" -- when zero of the four do.

    `shown` came from the `-n`-limited ranking, so `total - shown` was floor-drops
    plus truncation and the whole quantity was attributed to the floor. `-n 0`
    proved it: the note vanished, which is only possible if the arithmetic was
    measuring the slice.
    """
    for limit in (1, 2, 3):
        text = _density_lines(all_clear_the_floor, limit)
        assert "hold fewer than" not in text, (limit, text)


def test_truncation_gets_its_own_true_sentence(all_clear_the_floor):
    """`render_entries` suppresses the "N more" row for a density listing, so this
    note is the reader's only signal that rows are missing -- and `-n 0` really is
    the instruction that brings them back."""
    text = _density_lines(all_clear_the_floor, 1)
    assert "3 more clear the floor but were cut by -n" in text
    assert "use -n 0 for all" in text


def test_one_truncated_row_reads_as_one(all_clear_the_floor):
    text = _density_lines(all_clear_the_floor, 3)
    assert "1 more clears the floor but was cut by -n" in text


def test_no_note_at_all_when_nothing_is_missing(all_clear_the_floor):
    assert "cut by -n" not in _density_lines(all_clear_the_floor, 0)
    assert "hold fewer than" not in _density_lines(all_clear_the_floor, 0)


def test_both_reasons_are_stated_when_both_apply(tmp_path):
    """Three above the floor, two below, showing one: two true sentences."""
    root = str(tmp_path / "t")
    for name in ("big1", "big2", "big3"):
        d = os.path.join(root, name)
        os.makedirs(d)
        for i in range(150):
            with open(os.path.join(d, "f%d" % i), "wb") as fh:
                fh.write(b"x" * 400)
    for name in ("tiny1", "tiny2"):
        os.makedirs(os.path.join(root, name))
        with open(os.path.join(root, name, "f"), "wb") as fh:
            fh.write(b"x" * 400)
    res = walk(root, threads=2, depth=1)
    text = _flat("\n".join(report._table(res, 1, False, PLAIN, "density")))
    assert "2 of 5 entries hold fewer than" in text
    assert "2 more clear the floor but were cut by -n" in text


# ---------------------------------------------------------------------------
# 2 -- RECLAIMABLE reported 0 files for every cache in count mode
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def caches(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("caches"))
    for name in ("__pycache__", os.path.join(".cache", "pip")):
        os.makedirs(os.path.join(root, name))
        with open(os.path.join(root, name, "f"), "wb") as fh:
            fh.write(b"x" * 300000)
    with open(os.path.join(root, "keepme.bin"), "wb") as fh:
        fh.write(b"y" * 900000)
    return root


def test_count_mode_reclaimable_counts_the_inodes_it_walked(caches):
    """`0 files` for a directory the walk had just counted, and `0.0% of the tree`.

    The `counts = res.count_only` branch does the right thing with the wrong
    input: `watched` is filled only in the stat arm, so after `-c` every watched
    path carried (0, 0) -- and `render_reclaimable` lists `watched` first and
    dedupes by path, so the zero shadowed the real count in `dir_agg`.
    """
    res = walk(caches, threads=2, depth=2, count_only=True)
    text = "\n".join(report.render_reclaimable(res, PLAIN))
    assert "0 files" not in text
    assert "0.0% of the tree" not in text
    # Two caches, each one file plus its own directory.
    assert "4 files reclaimable in total" in text
    # The bytes are genuinely absent, and still say so.
    assert "n/a" in text and "0 B" not in text


def test_the_two_row_sources_count_the_same_thing(caches):
    """A `files` column that means one thing for a `watched` row and another for a
    `dir_agg` row is not a column, and in count mode it is the ranking key."""
    for count_only in (False, True):
        res = walk(caches, threads=2, depth=2, count_only=count_only)
        for path, (size, inodes) in res.watched.items():
            entry = res.dir_agg.get(path)
            if entry is None:
                continue  # deeper than the reported depth; nothing to compare
            assert (size, inodes) == (entry.size, entry.inodes), (path, count_only)


def test_the_walk_still_finds_caches_below_the_reported_depth_in_count_mode(tmp_path):
    """The reason `watched` exists at all: `dir_agg` only reaches `-d`."""
    root = str(tmp_path / "t")
    hf = os.path.join(root, "sub", ".cache", "huggingface")
    os.makedirs(hf)
    with open(os.path.join(hf, "blob"), "wb") as fh:
        fh.write(b"z" * 4096)
    res = walk(root, threads=2, depth=1, count_only=True)
    text = "\n".join(report.render_reclaimable(res, PLAIN))
    assert "huggingface" in text
    assert "2 files" in text  # the blob and the directory holding it


# ---------------------------------------------------------------------------
# 3 -- top_dirs had no tiebreaker
# ---------------------------------------------------------------------------


_TIED = ("c1", "c2", "c3", "c4", "c5", "c6")


def _tied_result(order):
    """Six entries tied on every metric, merged into `dir_agg` in ``order``.

    `dir_agg` insertion order *is* the order worker threads took `merge_lock`, so
    permuting it here is the same input the walker hands `top_dirs` on a real
    eight-thread run -- without depending on a filesystem to produce the tie.
    """
    res = walkmod.WalkResult("/r")
    for name in order:
        e = walkmod.Entry("/r/" + name, True)
        e.add(8192, 120, 1)
        res.dir_agg["/r/" + name] = e
    res.files, res.dirs = 720, 6
    return res


@pytest.mark.parametrize("key", ["size", "files", "density"])
def test_a_tied_ranking_does_not_depend_on_merge_order(key):
    """Stable sort plus no secondary key means ties keep `dir_agg` insertion
    order -- so two runs over an unchanged tree returned different rankings."""
    orders = {
        tuple(e.path for e in _tied_result(perm).top_dirs(10**9, key))
        for perm in (
            _TIED,
            _TIED[::-1],
            _TIED[3:] + _TIED[:3],
            ("c4", "c1", "c6", "c2", "c5", "c3"),
        )
    }
    assert orders == {tuple("/r/" + n for n in _TIED)}, orders


def test_under_n_the_tied_subset_is_stable_too():
    """Not just the order: *which* entries fall behind the remainder row changed
    per run, so two people running one command on one tree saw different
    directories named."""
    subsets = {
        tuple(os.path.basename(e.path) for e in _tied_result(perm).top_dirs(3, "size"))
        for perm in (_TIED, _TIED[::-1], ("c4", "c1", "c6", "c2", "c5", "c3"))
    }
    assert subsets == {("c1", "c2", "c3")}, subsets


def test_the_tiebreaker_never_outranks_the_measurement():
    """`z` before `a` when `z` is bigger: the path is the *last* key, not the first."""
    res = walkmod.WalkResult("/r")
    for name, size, files in (("a_small", 4096, 1), ("z_big", 400000, 300)):
        e = walkmod.Entry("/r/" + name, True)
        e.add(size, files, 1)
        res.dir_agg["/r/" + name] = e
    res.files, res.dirs = 301, 2
    assert [os.path.basename(e.path) for e in res.top_dirs(10**9, "size")] == ["z_big", "a_small"]
    assert [os.path.basename(e.path) for e in res.top_dirs(10**9, "files")] == ["z_big", "a_small"]


def test_the_other_metric_breaks_the_tie_before_the_path_does():
    """Ranking on `(primary, other_metric, path)`: a tied byte figure falls back to
    a real measurement, not straight to alphabetical order."""
    res = walkmod.WalkResult("/r")
    for name, files in (("a_few", 2), ("z_many", 400)):
        e = walkmod.Entry("/r/" + name, True)
        e.add(8192, files, 1)
        res.dir_agg["/r/" + name] = e
    res.files, res.dirs = 402, 2
    assert [os.path.basename(e.path) for e in res.top_dirs(10**9, "size")] == ["z_many", "a_few"]


def test_the_whole_report_reproduces_for_an_unchanged_tree(tmp_path, capsys):
    """`--no-box` is documented for "piping into grep, awk or a diff". A report
    that will not diff against itself cannot serve that."""
    root = str(tmp_path / "t")
    for name in _TIED:
        os.makedirs(os.path.join(root, name))
        with open(os.path.join(root, name, "f"), "wb") as fh:
            fh.write(b"x" * 10)
    # Let the filesystem finish allocating before comparing two measurements of
    # it -- the same trap the package's own settling logic exists for, and the
    # same wait the CI's du-agreement job makes.
    for _ in range(20):
        if walk(root, threads=2, depth=1).size == walk(root, threads=2, depth=1).size:
            break
    seen = set()
    for _ in range(4):
        cli.main([root, "--no-box", "--no-quota", "--no-deleted", "--color", "never"])
        out = capsys.readouterr().out
        # Drop the elapsed time, which legitimately differs run to run.
        seen.add("\n".join(ln for ln in out.splitlines() if "0.0" not in ln))
    assert len(seen) == 1, seen


# ---------------------------------------------------------------------------
# 4 -- --json -c published rankings the CLI refuses for the same walk
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def by_count(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("counts"))
    for n in (2, 40, 7, 90, 15):
        d = os.path.join(root, "dir_%d" % n)
        os.makedirs(d)
        for i in range(n):
            open(os.path.join(d, "f%d" % i), "wb").close()
    return root


def test_the_cli_still_refuses_a_byte_ranking_without_bytes(by_count, capsys):
    with pytest.raises(SystemExit):
        cli.main([by_count, "-c", "--sort", "size"])
    assert "does not measure them" in capsys.readouterr().err


def test_the_document_refuses_it_the_way_a_document_can(by_count, capsys):
    """`top_by_size` was byte-for-byte identical to `top_by_inodes` -- a files
    ranking under a size name -- and `top_by_density` had a null density on every
    row. `null` is what the `"schema": 1` contract already means by "no
    measurement" (`settling.settled` sets the precedent)."""
    cli.main([by_count, "-c", "--json", "-n", "3", "--no-quota", "--no-deleted"])
    doc = json.loads(capsys.readouterr().out)["walk"]
    assert doc["top_by_size"] is None
    assert doc["top_by_density"] is None
    # The one ranking a stat-free walk can actually make is still published.
    assert [os.path.basename(r["path"]) for r in doc["top_by_inodes"]] == [
        "dir_90",
        "dir_40",
        "dir_15",
    ]


def test_a_walk_with_bytes_still_publishes_all_three(by_count, capsys):
    cli.main([by_count, "--json", "-n", "3", "--no-quota", "--no-deleted"])
    doc = json.loads(capsys.readouterr().out)["walk"]
    assert isinstance(doc["top_by_size"], list) and doc["top_by_size"]
    assert isinstance(doc["top_by_density"], list)


# ---------------------------------------------------------------------------
# 5 -- "1 files", and "and are listed below" when nothing is
# ---------------------------------------------------------------------------


def test_plural_agrees_with_its_count():
    assert plural(1, "file") == "1 file"
    assert plural(0, "file") == "0 files"
    assert plural(2, "file") == "2 files"
    assert plural(1234, "file") == "1,234 files"
    # Unknown is not one.
    assert plural(None, "file") == "n/a files"


def test_the_facts_line_says_one_file(tmp_path, capsys):
    """An empty directory is the shortest reproduction: `4.0 KiB · 1 files · 0.00s`."""
    root = str(tmp_path / "e")
    os.makedirs(root)
    cli.main([root, "--no-box", "--no-quota", "--no-deleted", "--color", "never"])
    out = capsys.readouterr().out
    assert "1 file " in out and "1 files" not in out


def test_the_count_only_headline_says_one_file(tmp_path, capsys):
    root = str(tmp_path / "e")
    os.makedirs(root)
    cli.main([root, "-c", "--no-box", "--no-quota", "--no-deleted", "--color", "never"])
    assert "1 files" not in capsys.readouterr().out


def test_the_interrupted_headline_says_one_file(tmp_path, capsys, monkeypatch):
    """Where a real 1 is most likely: the walk stopped almost immediately."""
    root = str(tmp_path / "e")
    os.makedirs(root)
    real_walk = walkmod.walk

    def interrupted(*a, **kw):
        res = real_walk(*a, **kw)
        res.partial = True
        return res

    monkeypatch.setattr(walkmod, "walk", interrupted)
    cli.main([root, "--no-progress", "--no-box", "--color", "never"])
    out = capsys.readouterr().out
    assert "PARTIAL" in out
    assert "1 file scanned before the interrupt" in out


def test_nothing_finished_names_no_table(tmp_path, capsys, monkeypatch):
    """`render_entries` returns [] once `finished_only` has filtered everything, so
    "and are listed below" pointed at a table that is not there."""
    root = str(tmp_path / "t")
    os.makedirs(os.path.join(root, "sub"))
    with open(os.path.join(root, "sub", "f"), "wb") as fh:
        fh.write(b"x" * 4096)
    real_walk = walkmod.walk

    def interrupted(*a, **kw):
        res = real_walk(*a, **kw)
        res.partial = True
        res.finished_tops = set()
        return res

    monkeypatch.setattr(walkmod, "walk", interrupted)
    cli.main([root, "--no-progress", "--no-box", "--color", "never"])
    flat = _flat(capsys.readouterr().out)
    assert "0 top-level entries were walked to completion;" in flat
    assert "listed below" not in flat


def test_one_finished_entry_is_listed_and_reads_as_one(tmp_path, capsys, monkeypatch):
    root = str(tmp_path / "t")
    for name in ("kept", "lost"):
        os.makedirs(os.path.join(root, name))
        with open(os.path.join(root, name, "f"), "wb") as fh:
            fh.write(b"x" * 4096)
    real_walk = walkmod.walk

    def interrupted(*a, **kw):
        res = real_walk(*a, **kw)
        res.partial = True
        res.finished_tops = {"kept"}
        return res

    monkeypatch.setattr(walkmod, "walk", interrupted)
    cli.main([root, "--no-progress", "--no-box", "--color", "never"])
    flat = _flat(capsys.readouterr().out)
    assert "1 top-level entry was walked to completion and is listed below;" in flat
