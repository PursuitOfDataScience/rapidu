"""Round six: four defects in the sentence printed beside a correct measurement.

Found by rendering one fixture tree -- a sparse file, a hard-linked pair, a
symlink, an unreadable directory, one file older than a year -- and reading the
report as a reader would rather than as its author.

* ``allocation_is_material`` refused a tree whose *allocated* size is zero, on a
  guard that read like "we need both figures" but excluded the maximum of the
  very phenomenon the panel explains. A 10 MiB sparse file on tmpfs -- whose
  directories occupy no blocks, so nothing in the tree allocates -- reported
  ``0 B, 2 inodes`` and never mentioned the 10 MiB. The same file under a
  filesystem whose directories *do* allocate got the full panel;
* the allocated-over-apparent ratio was formatted in two places with two
  precisions, so one report said ``(0.0x allocated)`` five lines above
  ``-- 0.01x`` about the same number -- and both were able to round a real
  measurement to zero, worst where the panel matters most;
* ``1 dirs unreadable``, ``1 hard-linked files, 1 extra names deduped`` and
  ``1 files`` in ``BY AGE`` -- three sites that bypassed :func:`fmt.plural`,
  whose own docstring records this class being fixed elsewhere;
* ``N regular + M dirs`` called a symlink a regular file. ``walk`` counts one in
  ``files`` like any other non-directory entry, so a tree with one symlink and
  one hard-linked pair printed ``6 regular`` for five regular files -- the two
  errors cancelling -- while ``--json`` published ``symlinks`` separately, so a
  reader adding them counted the symlink twice.
"""

import contextlib
import errno
import json
import os
import re
import sys

from rapidu import report, ui
from rapidu.fmt import noun, plural, ratio_x
from rapidu.report import SettleCheck
from rapidu.walk import WalkResult, walk

PLAIN = ui.resolve_style("never", True)

KIB = 1 << 10
MIB = 1 << 20


def _flat(lines):
    return " ".join(" ".join(lines).split())


def _sparse(apparent=10 * MIB, size=0):
    """Data that is charged nothing: the extreme of the sparse case."""
    r = WalkResult("/dev/shm/sparse")
    r.files, r.dirs = 1, 1
    r.apparent = apparent
    r.size = size
    return r


# --- allocated == 0 is a measurement, not a missing one ---------------------


def test_a_tree_that_allocates_nothing_still_gets_its_allocation_panel():
    res = _sparse()
    assert res.alloc_ratio == 0.0
    assert report.allocation_is_material(res)
    text = _flat(report.render_allocation(res, PLAIN))
    assert "10.0 MiB" in text, text
    assert "stored in 0 B" in text, text


def test_zero_allocated_is_not_rendered_as_matching_the_data():
    """``or 1.0`` sent a real 0.0 into the branch that means "no divergence"."""
    text = _flat(report.render_allocation(_sparse(), PLAIN))
    assert "1.0x" not in text, text
    assert "allocated for" not in text, text


def test_no_apparent_bytes_is_still_refused():
    """The one genuine absence: nothing to divide by, so there is no ratio."""
    r = WalkResult("/tmp/empty")
    r.files, r.dirs = 0, 1
    assert r.alloc_ratio is None
    assert not report.allocation_is_material(r)
    assert report.render_allocation(r, PLAIN) == []


def test_count_only_still_makes_no_allocation_claim():
    r = _sparse()
    r.count_only = True
    assert not report.allocation_is_material(r)


def test_a_real_tmpfs_sparse_file_reports_its_apparent_size(tmp_path):
    """The reproduction, on a filesystem rather than a hand-built result.

    ``/dev/shm`` is tmpfs and its directories have ``st_blocks == 0``, so this is
    reachable without constructing anything: every entry in the tree allocates
    nothing while 10 MiB of apparent data sits there.
    """
    root = "/dev/shm"
    if not os.path.isdir(root) or not os.access(root, os.W_OK):
        return  # not a tmpfs host; the hand-built cases above still pin it
    d = os.path.join(root, "rapidu_round_six_%d" % os.getpid())
    os.mkdir(d)
    try:
        with open(os.path.join(d, "s.bin"), "wb") as fh:
            fh.truncate(10 * MIB)
        if os.stat(d).st_blocks:
            return  # this tmpfs charges for directories; nothing to pin here
        res = walk(d, threads=2)
        assert res.size == 0
        assert res.apparent >= 10 * MIB
        assert report.allocation_is_material(res)
        assert "10.0 MiB" in _flat(report.render_allocation(res, PLAIN))
    finally:
        for name in os.listdir(d):
            os.unlink(os.path.join(d, name))
        os.rmdir(d)


# --- one rendering of one ratio ---------------------------------------------


def test_the_ratio_is_formatted_the_same_way_in_both_places():
    """Facts line and panel, same number, one string."""
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 6, 7
    r.apparent = 10132881
    r.size = 120320
    token = ratio_x(r.alloc_ratio)
    facts = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    panel = _flat(report.render_allocation(r, PLAIN))
    assert token == "0.01x"
    assert token in facts, facts
    assert token in panel, panel
    assert "0.0x" not in facts, facts


def test_a_ratio_below_a_hundredth_is_not_reported_as_zero():
    assert ratio_x(1.6e-05) == "<0.01x"
    assert ratio_x(0.0001) == "<0.01x"


def test_an_exact_zero_ratio_says_zero_not_almost_zero():
    assert ratio_x(0.0) == "0x"


def test_an_unmeasurable_ratio_is_not_a_number():
    assert ratio_x(None) == "n/a"


def test_ratios_at_or_above_one_keep_their_old_rendering():
    assert ratio_x(1.0) == "1.0x"
    assert ratio_x(8.03) == "8.0x"


def test_the_facts_line_omits_the_ratio_only_when_there_is_none():
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 1, 1
    r.size = 4096
    assert r.alloc_ratio is None
    assert "allocated)" not in _flat(report.render_walk(r, SettleCheck(), style=PLAIN))


# --- counts agree with their nouns -----------------------------------------


def test_one_unreadable_directory_is_one_dir():
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 3, 2
    r.size = r.apparent = 4096
    r.unreadable_dirs = [("/tmp/t/noread", "Permission denied")]
    assert not r.complete
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "1 dir unreadable" in text, text
    assert "1 dirs" not in text, text


def test_one_unstatable_entry_is_one_entry():
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 3, 2
    r.size = r.apparent = 4096
    r.unstatable = 1
    assert not r.complete
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "1 entry unstatable" in text, text
    assert "1 entrys" not in text and "1 entries" not in text, text


def test_several_unstatable_entries_are_entries_not_entrys():
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 3, 2
    r.size = r.apparent = 4096
    r.unstatable = 4
    assert not r.complete
    assert "4 entries unstatable" in _flat(report.render_walk(r, SettleCheck(), style=PLAIN))


def test_one_hard_link_is_one_file_and_one_extra_name():
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 3, 2
    r.size = r.apparent = 4096
    r.hardlinked_inodes = 1
    r.hardlink_extra_refs = 1
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "1 hard-linked file, 1 extra name deduped" in text, text


def test_several_hard_links_still_pluralise():
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 9, 2
    r.size = r.apparent = 4096
    r.hardlinked_inodes = 2
    r.hardlink_extra_refs = 3
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "2 hard-linked files, 3 extra names deduped" in text, text


def test_a_single_file_in_an_age_bucket_is_one_file():
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 6, 1
    r.size = r.apparent = 116 * KIB
    r.by_age = [(115 * KIB, 5), (0, 0), (0, 0), (0, 0), (512, 1)]
    rows = report.render_age(r, PLAIN)
    tail = [ln for ln in rows if "> 1y" in ln]
    assert tail and tail[0].rstrip().endswith("1 file"), tail
    assert not any(ln.rstrip().endswith("1 files") for ln in rows), rows


def test_the_age_table_keeps_its_numeric_column_aligned():
    """The noun agrees *outside* the column, so the digits still line up."""
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 12346, 1
    r.size = r.apparent = 116 * KIB
    r.by_age = [(115 * KIB, 12345), (0, 0), (0, 0), (0, 0), (512, 1)]
    rows = [ln for ln in report.render_age(r, PLAIN) if "%" in ln]
    columns = [ln.index("file") for ln in rows if "file" in ln]
    # every row's noun starts one space after a 10-wide right-aligned count
    assert len(set(columns)) == 1, rows


def test_the_noun_helper_agrees_without_carrying_a_count():
    assert noun(1, "file") == "file"
    assert noun(0, "file") == "files"
    assert noun(None, "file") == "files"
    assert plural(1, "entry", irregular="entries") == "1 entry"
    assert plural(2, "entry", irregular="entries") == "2 entries"


# --- a symlink is not a regular file ---------------------------------------


def test_the_breakdown_names_symlinks_separately():
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 7, 7
    r.symlinks = 1
    r.hardlink_extra_refs = 1
    r.size = r.apparent = 116 * KIB
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "13 inodes (5 files + 1 symlink + 7 dirs)" in text, text
    assert "regular" not in text, text


def test_the_breakdown_parts_sum_to_the_inode_total():
    import re

    for files, dirs, syms, extra in ((7, 7, 1, 1), (10, 3, 0, 0), (5, 1, 5, 0), (1, 1, 1, 0)):
        r = WalkResult("/tmp/t")
        r.files, r.dirs, r.symlinks, r.hardlink_extra_refs = files, dirs, syms, extra
        r.size = r.apparent = 4096
        text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
        head = text.split(" inodes (")[1].split(")")[0]
        parts = [int(re.sub(r"[^0-9]", "", p).replace(",", "")) for p in head.split(" + ")]
        assert sum(parts) == r.inodes, (head, r.inodes)


def test_no_symlinks_means_no_symlink_term():
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 6, 7
    r.size = r.apparent = 116 * KIB
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "13 inodes (6 files + 7 dirs)" in text, text
    assert "symlink" not in text, text


def test_a_single_directory_is_one_dir_in_the_breakdown():
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 1, 1
    r.symlinks = 1
    r.size = r.apparent = 4096
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "(0 files + 1 symlink + 1 dir)" in text, text


def test_the_age_heading_does_not_claim_regular_files():
    r = WalkResult("/tmp/t")
    r.files, r.dirs = 6, 1
    r.size = r.apparent = 116 * KIB
    r.by_age = [(115 * KIB, 5), (0, 0), (0, 0), (0, 0), (512, 1)]
    text = _flat(report.render_age(r, PLAIN))
    assert "regular" not in text, text
    assert "not directories" in text, text


def test_a_walked_symlink_is_counted_once_in_files_and_once_in_symlinks(tmp_path):
    """The double-counting trap the breakdown used to invite, pinned on disk."""
    d = tmp_path / "t"
    (d / "sub").mkdir(parents=True)
    (d / "plain.txt").write_bytes(b"x" * 10)
    os.symlink("plain.txt", str(d / "link"))
    res = walk(str(d), threads=2)
    assert res.symlinks == 1
    assert res.files == 2  # the regular file and the symlink
    assert res.inodes == res.files + res.dirs
    doc = report.to_json(res, None, None, None, None)
    # a consumer adding `symlinks` to `files` would count the link twice
    assert doc["walk"]["files"] == 2
    assert doc["walk"]["symlinks"] == 1
    text = _flat(report.render_walk(res, SettleCheck(), style=PLAIN))
    assert "(1 file + 1 symlink + 2 dirs)" in text, text


def test_the_age_denominator_matches_the_population_it_buckets(tmp_path):
    """Bucket totals and the share's divisor count the same entries."""
    d = tmp_path / "t"
    d.mkdir()
    for i in range(3):
        (d / ("f%d.txt" % i)).write_bytes(b"x" * 100)
    os.symlink("f0.txt", str(d / "link"))
    old = d / "old.txt"
    old.write_bytes(b"y" * 100)
    stamp = 1546300800  # 2019-01-01
    os.utime(str(old), (stamp, stamp))
    res = walk(str(d), threads=2)
    bucketed = sum(f for _b, f in res.by_age)
    assert bucketed == res.files - res.hardlink_extra_refs
    text = _flat(report.render_age(res, PLAIN))
    if "has not been modified" in text:
        assert "(20.0%)" in text, text  # 1 of 5 bucketed entries


def test_the_document_still_round_trips(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    (d / "f").write_bytes(b"x" * 10)
    res = walk(str(d), threads=2)
    assert json.loads(json.dumps(report.to_json(res, None, None, None, None)))


# --- the default view was withholding a caveat -a printed --------------------
#
# `render_walk` states "figure is provisional" as one of its facts, so it reached
# `-a` only. `rdu .` -- the invocation the tool documents as its whole purpose --
# printed `1.0 KiB . 6 inodes` for a directory holding 789.2 KiB and said nothing,
# because `render_compact` admits only measured drift and the re-stat had not run.
# How much data is *not yet allocated* is measured too.


def _unsettled(size=1024, recent_apparent=800000, recent_size=0, recent=4):
    r = WalkResult("/tmp/fresh")
    r.files, r.dirs = recent, 2
    r.size = size
    r.apparent = size + recent_apparent
    r.recent_files = recent
    r.recent_apparent = recent_apparent
    r.recent_size = recent_size
    r.settle_window = 120.0
    return r


def test_the_default_view_says_the_headline_is_provisional():
    text = _flat(report.render_compact(_unsettled(), SettleCheck(), 10, False, PLAIN))
    assert "provisional" in text, text
    assert "781.2 KiB not yet allocated" in text, text


def test_the_warning_is_marked_so_it_can_be_found():
    lines = report.render_compact(_unsettled(), SettleCheck(), 10, False, PLAIN)
    assert any(ln.startswith("! ") for ln in lines), lines


def test_a_settled_tree_is_silent_even_with_recently_written_files():
    """The furniture test: recent files whose blocks have landed say nothing."""
    r = _unsettled(size=852992, recent_apparent=800000, recent_size=851968)
    assert report._unlanded_bytes(r) == 0
    assert not report._headline_is_provisional(r)
    assert "provisional" not in _flat(report.render_compact(r, SettleCheck(), 10, False, PLAIN))


def test_a_little_unlanded_data_in_a_large_tree_is_silent():
    """A ratio, not a floor: 10 KiB pending against 4 GiB cannot move anything."""
    r = _unsettled(size=4 << 30, recent_apparent=10240, recent_size=0, recent=1)
    assert not report._headline_is_provisional(r)
    assert "provisional" not in _flat(report.render_compact(r, SettleCheck(), 10, False, PLAIN))


def test_unlanded_data_equal_to_the_total_is_enough():
    r = _unsettled(size=800000, recent_apparent=800000, recent_size=0)
    assert report._headline_is_provisional(r)


def test_a_zero_total_with_pending_data_is_provisional():
    r = _unsettled(size=0, recent_apparent=800000, recent_size=0)
    assert report._headline_is_provisional(r)
    assert "provisional" in _flat(report.render_compact(r, SettleCheck(), 10, False, PLAIN))


def test_count_only_makes_no_claim_about_a_headline_it_did_not_measure():
    r = _unsettled()
    r.count_only = True
    assert not report._headline_is_provisional(r)


def test_allocated_above_apparent_does_not_go_negative():
    """Block padding can put `recent_size` above `recent_apparent`."""
    r = _unsettled(recent_apparent=800000, recent_size=851968)
    assert report._unlanded_bytes(r) == 0


def test_measured_drift_is_reported_once_not_twice():
    """`_hard_warnings` states drift with a figure; this estimate would repeat it."""
    r = _unsettled()
    chk = SettleCheck()
    chk.drift = 780000  # `moved` is derived from this
    chk.gap = 60.0
    chk.checked = 4
    chk.ran = True
    assert chk.moved
    text = _flat(report.render_compact(r, chk, 10, False, PLAIN))
    assert "still settling" in text, text
    assert text.count("provisional") == 0, text


def test_both_views_state_the_same_pending_figure():
    """One measurement, so the two renderers cannot drift apart on it."""
    r = _unsettled()
    compact = _flat(report.render_compact(r, SettleCheck(), 10, False, PLAIN))
    full = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    figure = "781.2 KiB"
    assert figure in compact, compact
    assert figure in full, full


def test_the_full_view_still_says_provisional_without_a_magnitude_when_small():
    """The `-a` one-liner survives for trees this gate does not fire on."""
    r = _unsettled(size=4 << 30, recent_apparent=10240, recent_size=0, recent=1)
    full = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "figure is provisional" in full, full
    assert "not yet allocated" not in full, full


def test_delayed_allocation_on_a_real_filesystem_is_caught_or_absent(tmp_path):
    """End to end: if the filesystem defers allocation, the default view says so.

    Not every filesystem defers -- on one that allocates on write there is nothing
    to warn about and nothing to assert, which is itself the correct behaviour.
    """
    d = tmp_path / "t"
    (d / "a").mkdir(parents=True)
    for i in range(4):
        (d / "a" / ("f%d.bin" % i)).write_bytes(b"\x5a" * 200000)
    res = walk(str(d), threads=4)
    text = _flat(report.render_compact(res, SettleCheck(), 10, False, PLAIN))
    if report._unlanded_bytes(res) >= res.size and res.size >= 0:
        assert "provisional" in text, text
    else:
        assert "provisional" not in text, text


# --- a wrapped line has to say which line it belongs to ----------------------
#
# Nothing in the report is truncated -- `render_entries` and `ui.box` both say so
# deliberately -- so a long name is wrapped instead, and the tail landed at column
# zero: level with the report's own margin, where it read as a new row. A ranked
# row ending `2  a b` above a bare `c/` said there was an entry named `c/`.


def test_a_wrapped_row_indents_its_continuation():
    row = "        512 B  " + "#" * 18 + "   16.7%          2  a b  c/"
    pieces = ui._wrap_ansi(row, 58, ui._CONT_INDENT)
    assert len(pieces) > 1
    for piece in pieces[1:]:
        assert piece.startswith(ui._CONT_INDENT), repr(piece)


def test_a_continuation_never_begins_with_the_padding_it_was_cut_from():
    row = "  " + " " * 30 + "value" + " " * 20 + "trailing_name_that_is_long_enough/"
    for width in range(20, 60, 7):
        for piece in ui._wrap_ansi(row, width, ui._CONT_INDENT)[1:]:
            body = piece[len(ui._CONT_INDENT) :]
            assert not body.startswith(" "), (width, repr(piece))


def test_indenting_does_not_reopen_the_mid_token_break_bug():
    """A path still comes apart at separators, not wherever the column ran out."""
    path = "/project/rcc/youzhi/models/checkpoints/step-4000/shard-00001-of-00008"
    pieces = ui._wrap_ansi(path, 34, ui._CONT_INDENT)
    assert len(pieces) > 1
    for piece in pieces[:-1]:
        assert piece.endswith("/"), repr(piece)


def test_colour_still_closes_on_every_indented_piece():
    style = ui.resolve_style("always")
    text = style.paint("/aaaaaaaaaa/bbbbbbbbbb/cccccccccc/dddddddddd", "bold_red")
    pieces = ui._wrap_ansi(text, 20, "  ")
    assert len(pieces) > 1
    for piece in pieces:
        assert piece.lstrip(" ").startswith("\033["), repr(piece)
        assert piece.endswith("\033[0m"), repr(piece)


def test_an_indent_wider_than_the_frame_is_dropped_not_honoured():
    """A frame that cannot fit a character is worse than an unindented wrap."""
    pieces = ui._wrap_ansi("z" * 30, 5, " " * 6)
    assert pieces and all(ui.visible_width(p) <= 5 for p in pieces), pieces


def test_the_unindented_wrap_is_byte_identical_to_before():
    """`subsequent_indent` defaults off, so every other caller is untouched."""
    row = "        512 B  " + "#" * 18 + "   16.7%          2  a b  c/"
    assert ui._wrap_ansi(row, 58) == [
        "        512 B  ##################   16.7%          2  a b",
        " c/",
    ]


def test_a_run_of_spaces_is_not_treated_as_layout():
    """A directory really named `a b  c` puts a two-space run inside the name.

    Preferring space *runs* over lone spaces was tried, to stop column padding
    losing to a space in a filename. It cannot work -- the wrapper is handed a flat
    string and does not know where the columns are -- and it broke the case it was
    added for, cutting inside `a b  c/` instead of before it.
    """
    row = "  x  " + "a b  c/"
    pieces = ui._wrap_ansi(row, 9)
    assert "".join(p.strip() for p in pieces).replace(" ", "") != ""
    # whatever the break, the visible characters survive minus discarded spaces
    assert "c/" in "".join(pieces)


def test_the_frame_still_closes_on_every_line_at_every_width():
    """The one property a frame has to have, now that continuations are indented."""
    style = ui.resolve_style("never")
    body = [
        "  short",
        "        512 B  " + "#" * 18 + "   16.7%          2  a b  c/",
        "  " + "z" * 200,
        "  /a/very/long/path/" + "segment/" * 20,
        "  日本語のディレクトリ" * 8,
    ]
    for width in (40, 60, 80, 100, 160):
        out = ui.box(body, style, width=width)
        assert len(out) >= 3
        widths = {ui.visible_width(ln) for ln in out}
        assert len(widths) == 1, (width, sorted(widths))
        for line in out[1:-1]:
            assert line.startswith("|") or line.startswith("│"), line
            assert line.endswith("|") or line.endswith("│"), line


def test_east_asian_names_do_not_tear_the_frame(tmp_path):
    """Every framed line is the same display width, CJK and emoji included."""
    import unicodedata

    def true_width(text):
        total = 0
        for ch in text:
            if unicodedata.combining(ch):
                continue
            total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        return total

    d = tmp_path / "t"
    for name in ("日本語のディレクトリ", "ελληνικά", "emoji_x_dir", "a b  c"):
        (d / name).mkdir(parents=True)
        (d / name / "f.bin").write_bytes(b"\x5a" * 4000)
    res = walk(str(d), threads=2)
    lines = report.render_compact(res, SettleCheck(), 10, False, PLAIN)
    framed = ui.box(lines, PLAIN, width=100)
    assert len({true_width(ln) for ln in framed}) == 1, sorted({true_width(ln) for ln in framed})
    for line in framed:
        assert ui.visible_width(line) == true_width(line), line


# --- the document withheld the number both text views print -------------------
#
# `settling.settled: null` says the headline is unknown. Nothing said *how*
# unknown: `walk.recent_bytes` held the allocated half and `recent_apparent` was
# published nowhere, so the difference -- the figure both terminal views now print
# -- could not be recomputed from anything in the document. A machine consumer has
# to be able to reach the reader's conclusion, not a weaker one.


def test_the_document_publishes_both_halves_of_the_settling_measurement():
    r = _unsettled()
    doc = report.to_json(r, SettleCheck(), None, None, None)
    s = doc["settling"]
    assert s["recent_apparent_bytes"] == 800000
    assert s["recent_allocated_bytes"] == 0


def test_a_consumer_can_recompute_the_derived_figure():
    r = _unsettled()
    s = report.to_json(r, SettleCheck(), None, None, None)["settling"]
    assert s["unlanded_bytes"] == max(0, s["recent_apparent_bytes"] - s["recent_allocated_bytes"])


def test_the_document_reaches_the_same_verdict_as_the_terminal():
    for r in (
        _unsettled(),
        _unsettled(size=852992, recent_apparent=800000, recent_size=851968),
        _unsettled(size=4 << 30, recent_apparent=10240, recent_size=0, recent=1),
    ):
        s = report.to_json(r, SettleCheck(), None, None, None)["settling"]
        text = _flat(report.render_compact(r, SettleCheck(), 10, False, PLAIN))
        assert s["headline_provisional"] == ("provisional" in text), (s, text[:80])


def test_the_published_figure_is_the_one_the_terminal_prints():
    r = _unsettled()
    s = report.to_json(r, SettleCheck(), None, None, None)["settling"]
    from rapidu.fmt import human_bytes

    assert human_bytes(s["unlanded_bytes"]) in _flat(
        report.render_compact(r, SettleCheck(), 10, False, PLAIN)
    )


def test_the_ambiguous_key_is_gone_and_the_schema_says_so():
    """`recent_bytes` held *allocated* blocks under a bare `bytes`.

    Every other byte figure in this document distinguishes `size_bytes` from
    `apparent_bytes`, so a consumer reading `recent_bytes` as the data size and
    comparing it against `apparent_bytes` was wrong by the allocation ratio. A key
    disappearing is exactly what the schema counter is for.
    """
    doc = report.to_json(_unsettled(), SettleCheck(), None, None, None)
    # The absence is the finding; the counter belongs to whichever test tracks the
    # current document shape. Pinning the number here made a later, correct bump
    # look like a regression in *this* finding -- the same trap RD-13's test fell
    # into.
    assert "recent_bytes" not in doc["walk"]
    assert doc["schema"] >= 3


def test_settling_is_present_whenever_walk_is(tmp_path):
    """The field moved into `settling`, which must not be the narrower section."""
    d = tmp_path / "t"
    d.mkdir()
    (d / "f").write_bytes(b"x" * 64)
    res = walk(str(d), threads=2)
    doc = report.to_json(res, SettleCheck(), None, None, None)
    assert "walk" in doc and "settling" in doc


def test_count_only_publishes_no_settling_bytes_it_did_not_measure():
    """`None`, not `False`: `-c` took no sizes, so there is no verdict to give.

    `False` reads as "the headline is sound", which is a claim about a measurement
    that was never made. See :func:`report._unmeasured`.
    """
    r = _unsettled()
    r.count_only = True
    s = report.to_json(r, SettleCheck(), None, None, None)["settling"]
    assert s["headline_provisional"] is None


def test_document_arithmetic_identities_hold_on_a_real_tree(tmp_path):
    """The invariants a consumer would reasonably assume, on a walked tree."""
    d = tmp_path / "t"
    (d / "a").mkdir(parents=True)
    (d / "deep" / "deeper").mkdir(parents=True)
    (d / "a" / "big.bin").write_bytes(b"\x5a" * 200000)
    os.link(str(d / "a" / "big.bin"), str(d / "hard.bin"))
    os.symlink("a/big.bin", str(d / "link"))
    old = d / "deep" / "deeper" / "old.txt"
    old.write_bytes(b"y" * 700)
    os.utime(str(old), (1546300800, 1546300800))
    res = walk(str(d), threads=2)
    w = report.to_json(res, SettleCheck(), None, None, None)["walk"]
    assert w["inodes"] == w["files"] + w["dirs"] - w["hardlink_extra_refs"]
    assert sum(b["files"] for b in w["by_age"]) == w["files"] - w["hardlink_extra_refs"]
    assert sum(b["bytes"] for b in w["by_age"]) <= w["size_bytes"]
    assert w["symlinks"] <= w["files"]
    assert len(w["unreadable_dir_paths"]) <= w["unreadable_dirs"]
    assert len(w["skipped_other_filesystem_paths"]) <= w["skipped_other_filesystem"]
    for key, field in (("top_by_size", "bytes"), ("top_by_inodes", "inodes")):
        values = [row[field] for row in w[key]]
        assert values == sorted(values, reverse=True), (key, values)


# --- a measurement that was taken must not be called provisional --------------
#
# `rdu --settle-wait 120` on a tree that had not moved in two minutes printed
# "figure is provisional (--settle-wait 60 to measure)": a contradiction of the
# check it had just run, and advice to wait less than it already had. The compact
# line ignored `SettleCheck.conclusive`; the SETTLING long form had consulted it
# all along, and so had `to_json` -- so the document and the terminal disagreed
# about the same check.


def _checked(gap, drift=0, files=5):
    chk = SettleCheck()
    chk.gap = gap
    chk.ran = gap > 0
    chk.checked = files
    chk.drift = drift
    return chk


def test_a_conclusive_null_restat_is_not_called_provisional():
    r = _unsettled(recent_apparent=1500000)
    chk = _checked(120.0)
    assert chk.conclusive and not chk.moved
    assert not report._headline_is_provisional(r, chk)
    text = _flat(report.render_settle(r, chk, PLAIN))
    assert "looks settled" in text, text
    assert "provisional" not in text, text


def test_a_conclusive_null_restat_advises_nothing():
    """Advice to wait 60s, printed after a 120s wait, is worse than silence."""
    text = _flat(report.render_settle(_unsettled(), _checked(120.0), PLAIN))
    assert "--settle-wait" not in text, text


def test_the_advice_only_appears_when_the_wait_was_shorter_than_it_suggests():
    """Reachable only below MIN_CONCLUSIVE_GAP_S, so 60 always exceeds it."""
    from rapidu.walk import MIN_CONCLUSIVE_GAP_S

    for gap in (0.0, 1.0, MIN_CONCLUSIVE_GAP_S - 0.1):
        text = _flat(report.render_settle(_unsettled(), _checked(gap), PLAIN))
        assert "--settle-wait 60" in text, (gap, text)
        assert gap < 60.0


def test_a_restat_too_short_to_believe_is_still_provisional():
    r = _unsettled()
    chk = _checked(3.0)
    assert not chk.conclusive
    assert report._headline_is_provisional(r, chk)
    assert "provisional" in _flat(report.render_compact(r, chk, 10, False, PLAIN))


def test_measured_drift_still_wins_over_the_estimate():
    r = _unsettled()
    chk = _checked(120.0, drift=780000)
    assert chk.moved
    text = _flat(report.render_compact(r, chk, 10, False, PLAIN))
    assert "still settling" in text, text
    assert "not yet allocated" not in text, text


def test_the_verdict_without_a_check_is_unchanged():
    """`settle` is optional, so every existing caller keeps its behaviour."""
    r = _unsettled()
    assert report._headline_is_provisional(r) is True
    assert report._headline_is_provisional(r, None) is True


def test_the_document_and_the_terminal_agree_about_the_check():
    """One check, one conclusion, in both renderings."""
    for gap, drift in ((0.0, 0), (3.0, 0), (120.0, 0), (120.0, 780000)):
        r = _unsettled()
        chk = _checked(gap, drift)
        s = report.to_json(r, chk, None, None, None)["settling"]
        settle_text = _flat(report.render_settle(r, chk, PLAIN))
        compact = _flat(report.render_compact(r, chk, 10, False, PLAIN))
        claims_provisional = "provisional" in settle_text or "provisional" in compact
        assert s["headline_provisional"] == claims_provisional, (gap, drift, s, compact[:70])
        if chk.conclusive and not chk.moved:
            assert s["settled"] is True
            assert s["headline_provisional"] is False


def test_settled_and_headline_provisional_never_contradict_each_other():
    for gap, drift in ((0.0, 0), (3.0, 0), (120.0, 0), (120.0, -5000)):
        s = report.to_json(_unsettled(), _checked(gap, drift), None, None, None)["settling"]
        if s["settled"] is True:
            assert s["headline_provisional"] is False, (gap, drift, s)


def test_the_rate_limiter_is_a_token_bucket_with_a_one_second_burst():
    """`capacity = max(rate, 1)`, which is what the measured wall time implies.

    121 directories at 20/s took 5.15s, not 6.05s -- consistent with one second
    of allowance up front and nothing looser than that.
    """
    from rapidu.walk import TokenBucket

    assert TokenBucket(20.0).capacity == 20.0
    assert TokenBucket(0.5).capacity == 1.0
    assert TokenBucket(20.0, burst=3).capacity == 3.0


# --- anything the terminal states, the document states -----------------------
#
# Found by enumerating each object's public attributes against the emitted keys,
# rather than by reading the backlog entry that was supposed to track this -- which
# is how these three survived it. All additions, so the schema does not move.


def _hung(workers=2):
    r = WalkResult("/scratch/hung")
    r.files, r.dirs = 40, 6
    r.size = r.apparent = 900000
    r.partial = True
    r.abandoned_workers = workers
    return r


def test_a_hung_mount_is_distinguishable_from_a_clean_interrupt():
    """`interrupted: true` is set by Ctrl-C *and* by threads wedged on a mount.

    Only the second means every figure emitted is a known undercount, because
    those threads' tallies were discarded rather than merged.
    """
    hung = report.to_json(_hung(2), SettleCheck(), None, None, None)["walk"]
    calm = report.to_json(_hung(0), SettleCheck(), None, None, None)["walk"]
    assert hung["interrupted"] is True and calm["interrupted"] is True
    assert hung["abandoned_threads"] == 2
    assert calm["abandoned_threads"] == 0


def test_the_terminal_and_the_document_agree_about_abandoned_threads():
    r = _hung(2)
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    doc = report.to_json(r, SettleCheck(), None, None, None)["walk"]
    assert "2 walk threads were still blocked" in text, text
    assert doc["abandoned_threads"] == 2


def test_vanished_files_are_published_as_a_caveat_on_the_drift():
    """The same kind of caveat as `sampled`, which was already published."""
    chk = _checked(60.0, files=3)
    chk.gone = 2
    r = _unsettled(recent_apparent=4096, recent_size=4096)
    s = report.to_json(r, chk, None, None, None)["settling"]
    assert s["vanished_files"] == 2
    assert s["rechecked"] == 3


def test_a_recheck_that_did_not_run_is_distinguishable_from_a_brief_one():
    """`conclusive: false` covers both, and only one says anything about the fs."""
    absent = report.to_json(_unsettled(), _checked(0.0), None, None, None)["settling"]
    brief = report.to_json(_unsettled(), _checked(3.0), None, None, None)["settling"]
    assert absent["conclusive"] is False and brief["conclusive"] is False
    assert absent["recheck_ran"] is False
    assert brief["recheck_ran"] is True


def test_the_filled_holes_are_all_present():
    """No literal version here: a snapshot cannot show that a bump did *not* happen,
    and asserting the number made an unrelated correct bump fail this test."""
    doc = report.to_json(_hung(1), SettleCheck(), None, None, None)
    for key in ("abandoned_threads",):
        assert key in doc["walk"]
    for key in ("recheck_ran", "vanished_files"):
        assert key in doc["settling"]


def test_no_walk_or_settle_measurement_is_left_unpublished():
    """The sweep that found the three, kept as a standing check.

    Attribute names are matched against the document by name *or* through this
    map, because the document renames for clarity (`size` -> `size_bytes`). A new
    measurement added to either object with no home here fails this test, which is
    the point: the backlog entry meant to track it had gone stale instead.
    """
    renamed = {
        "size": "size_bytes",
        "apparent": "apparent_bytes",
        "partial": "interrupted",
        "unstatable": "unstatable_entries",
        "crossed": "skipped_other_filesystem",
        "crossed_paths": "skipped_other_filesystem_paths",
        "future_files": "future_mtime_files",
        "recent_apparent": "recent_apparent_bytes",
        "recent_size": "recent_allocated_bytes",
        "elapsed": "elapsed_seconds",
        "settle_window": "window_seconds",
        "abandoned_workers": "abandoned_threads",
        "padding": "padding_bytes",
        "under_files": "under_allocated_files",
        "alloc_ratio": "ratio",
        "alloc_unit": "unit_bytes",
        "checked": "rechecked",
        "drift": "drift_bytes",
        "gap": "recheck_gap_seconds",
        "gone": "vanished_files",
        "ran": "recheck_ran",
    }
    # Internals and raw halves of a published derived figure: not measurements a
    # consumer needs, or already carried by the figure computed from them.
    internal = {
        "dir_agg",
        "watched",
        "finished_tops",
        "recent_sample",
        "by_dev",
        "root",
        "density_floor",
        "alloc_bits",
        "padded_alloc",
        "padded_apparent",
        "under_alloc",
        "under_apparent",
        "count_only",
        "sampled_of",
        "unreadable_dirs",
        "hardlinked_inodes",
    }
    res = _hung(1)
    chk = _checked(60.0)
    doc = json.dumps(report.to_json(res, chk, None, None, None))
    missing = []
    for obj in (res, chk):
        for name in dir(obj):
            if name.startswith("_") or name in internal:
                continue
            try:
                value = getattr(obj, name)
            except Exception:
                continue
            if callable(value):
                continue
            key = renamed.get(name, name)
            if ('"%s"' % key) not in doc:
                missing.append((type(obj).__name__, name, key))
    assert not missing, missing


# --- the only output a reader is invited to execute ---------------------------
#
# `RECLAIMABLE` prints shell commands. Everything else this tool emits is read;
# these get pasted. Verified by asking a real shell what words it would produce,
# rather than by inspecting the quoting -- "looks quoted" and "is quoted" are
# different claims and only one of them is testable.

HOSTILE_NAMES = [
    "quote'inside",
    "dollar$(whoami)",
    "back`id`tick",
    "semi; rm -rf HOME",
    "-rf",
    "star*glob",
    "brace{path}",
    'dq"inside',
    "hash#comment",
    "amp&background",
    "pipe|into",
    "nl$'x'",
]

UNPRINTABLE_NAMES = ["newline\nhere", "esc\x1b[31mred", "tab\there"]


def _torch_cache(root, name):
    """A `cache/torch` tree under a hostile directory name; its rule is `rm -rf`."""
    leaf = os.path.join(str(root), name, "cache", "torch")
    os.makedirs(leaf)
    with open(os.path.join(leaf, "blob.bin"), "wb") as handle:
        handle.write(b"\x5a" * 40000)
    return leaf


def _emitted_command(root):
    res = walk(str(root), threads=2)
    lines = report.render_reclaimable(res, PLAIN)
    commands = [ln.strip() for ln in lines if ln.strip().startswith("rm -rf")]
    return commands, lines


def test_a_hostile_directory_name_survives_a_real_shell(tmp_path):
    """One argument, equal to the path, for every name a filesystem allows."""
    import subprocess

    for index, name in enumerate(HOSTILE_NAMES):
        root = tmp_path / ("h%d" % index)
        leaf = _torch_cache(root, name)
        commands, lines = _emitted_command(root)
        assert commands, (name, lines)
        # Substitute a printer for `rm` so the shell does the parsing and nothing
        # is deleted; what it echoes is exactly what `rm` would have received.
        probe = commands[0].replace("rm -rf", "printf '%s\\n'", 1)
        out = subprocess.Popen(
            ["bash", "-c", probe], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).communicate()[0]
        words = [w for w in out.decode("utf-8", "replace").split("\n") if w != ""]
        assert words == [leaf], (name, words, leaf)


def test_command_substitution_in_a_name_does_not_run(tmp_path):
    """The failure that would matter: a name that executes when pasted."""
    import subprocess

    leaf = _torch_cache(tmp_path / "h", "x$(touch " + str(tmp_path / "PWNED") + ")y")
    commands, lines = _emitted_command(tmp_path / "h")
    assert commands, lines
    probe = commands[0].replace("rm -rf", "printf '%s\\n'", 1)
    subprocess.Popen(["bash", "-c", probe], stdout=subprocess.PIPE).communicate()
    assert not os.path.exists(str(tmp_path / "PWNED"))
    assert os.path.isdir(leaf)


def test_an_unprintable_name_is_refused_and_the_refusal_is_inert(tmp_path):
    """No correct one-liner exists, so none is printed -- and the note no-ops."""
    for index, name in enumerate(UNPRINTABLE_NAMES):
        root = tmp_path / ("u%d" % index)
        _torch_cache(root, name)
        commands, lines = _emitted_command(root)
        assert not commands, (name, commands)
        note = [ln.strip() for ln in lines if "unprintable" in ln]
        assert note, lines
        # A `#` comment is what makes pasting it harmless.
        assert note[0].startswith("#"), note


def test_the_emitted_path_is_always_absolute(tmp_path):
    """What protects against a name like `-rf`: quoting does not stop `rm` from
    reading a leading dash as options, but an absolute path has none to read.

    `cli._resolve_paths` absolutises, so this holds however the path was typed.
    """
    from rapidu import cli

    leaf = _torch_cache(tmp_path / "h", "-rf")
    resolved, refused = cli._resolve_paths([os.path.join(str(tmp_path), "h", "-rf")])
    assert refused == 0 and resolved and resolved[0].startswith("/")
    commands, _lines = _emitted_command(tmp_path / "h")
    assert commands and commands[0].startswith("rm -rf '/"), commands
    assert leaf.startswith("/")


def test_a_truncated_command_is_not_a_runnable_one(tmp_path):
    """Half of an `rm -rf` must not be a valid `rm -rf`."""
    import subprocess

    _torch_cache(tmp_path / "h", "ordinary")
    commands, _lines = _emitted_command(tmp_path / "h")
    assert commands
    command = commands[0]
    # Every prefix that cuts inside the quoted path leaves the quote open, so a
    # shell asked to parse it reports an error rather than deleting a parent.
    start = command.index("'")
    for cut in range(start + 2, len(command) - 1, 7):
        probe = command[:cut].replace("rm -rf", "printf '%s\\n'", 1)
        proc = subprocess.Popen(["bash", "-n", "-c", probe], stderr=subprocess.PIPE)
        proc.communicate()
        assert proc.returncode != 0, repr(command[:cut])


def test_no_user_data_reaches_a_command_name(tmp_path):
    """The non-`{path}` forms interpolate only the constant tool table."""
    for pattern, command, delete_ok in report._RECLAIMABLE:
        rendered, needs_path = report.reclaim_command(command, delete_ok)
        if rendered is None or rendered in report._RECLAIM_ADVICE:
            continue
        if not needs_path:
            assert "{" not in rendered or "{path}" not in rendered, (pattern, rendered)


# --- an agreement reached by an unsound comparison is not evidence -----------
#
# `CLOSES` was decided before the blockers were consulted, so every one of them
# was collected and then thrown away whenever the two figures happened to land
# within tolerance. The headline read "reconciles (difference is within 2.0 GiB)"
# -- the strongest thing this tool says -- directly above "11,267 directories
# could not be read, so the walk total is a floor, not a total".
#
# The module docstring had already promised the opposite: *every input that could
# invalidate the comparison downgrades the verdict to INCONCLUSIVE and names
# itself*. The blockers were named; the downgrade was skipped.

import grp  # noqa: E402

from rapidu import reconcile as rc  # noqa: E402
from rapidu.deleted import DeletedScan  # noqa: E402
from rapidu.quota import QuotaRow, QuotaSnapshot  # noqa: E402

QUOTA_BYTES = 100 << 30
GROUP_NAME = grp.getgrgid(os.getgid()).gr_name


def _row_snap(used, stamped=True, mount="/scratch"):
    snap = QuotaSnapshot("test")
    snap.available = True
    snap.rows = [QuotaRow(GROUP_NAME, "blocks", "group", used, used * 4, used * 5, mount=mount)]
    if stamped:
        snap.taken_at = snap.read_at - 60.0
    return snap


def _walk_charged(size, unreadable=0, unstatable=0, partial=False, devs=1, root="/scratch"):
    r = WalkResult(root)
    r.files, r.dirs = 100, 10
    r.size = r.apparent = size
    r.by_gid = {os.getgid(): (size, 110)}
    for i in range(unreadable):
        r.unreadable_dirs.append(("/scratch/x%d" % i, "Permission denied"))
    r.unstatable = unstatable
    r.partial = partial
    for dev in range(devs):
        r.by_dev[dev] = (size // max(devs, 1), 110)
    return r


def _settled_check():
    chk = SettleCheck()
    chk.ran = True
    chk.gap = 60.0
    return chk


BLOCKER_CASES = [
    ("unreadable directories", {"unreadable": 11267}),
    ("unstatable entries", {"unstatable": 40}),
    ("interrupted walk", {"partial": True}),
    ("crossed filesystems", {"devs": 3}),
]


def test_a_clean_comparison_still_closes():
    """The genuine case has to survive the fix, or the verdict is worthless."""
    rec = rc.reconcile(
        _walk_charged(QUOTA_BYTES),
        _settled_check(),
        _row_snap(QUOTA_BYTES),
        DeletedScan(),
        "blocks",
    )
    assert rec.verdict == rc.CLOSES
    assert "reconciles" in rc.verdict_line(rec)


def test_a_blocker_downgrades_an_agreement():
    for label, kw in BLOCKER_CASES:
        rec = rc.reconcile(
            _walk_charged(QUOTA_BYTES, **kw),
            _settled_check(),
            _row_snap(QUOTA_BYTES),
            DeletedScan(),
            "blocks",
        )
        assert rec.within_tolerance, label
        assert rec.verdict == rc.INCONCLUSIVE, (label, rec.verdict)
        assert rec.blockers, label


def test_the_headline_no_longer_reads_as_an_all_clear():
    for label, kw in BLOCKER_CASES:
        rec = rc.reconcile(
            _walk_charged(QUOTA_BYTES, **kw),
            _settled_check(),
            _row_snap(QUOTA_BYTES),
            DeletedScan(),
            "blocks",
        )
        line = rc.verdict_line(rec)
        assert "reconciles" not in line, (label, line)
        assert line.startswith("INCONCLUSIVE"), (label, line)
        assert "not soundly" in line, (label, line)


def test_the_agreement_is_still_reported_as_an_observation():
    """It is genuinely reassuring; it just is not a verdict."""
    rec = rc.reconcile(
        _walk_charged(QUOTA_BYTES, unreadable=11267),
        _settled_check(),
        _row_snap(QUOTA_BYTES),
        DeletedScan(),
        "blocks",
    )
    joined = " ".join(" ".join(n.split()) for n in rec.notes)
    assert "do agree" in joined, rec.notes
    assert "not evidence" in joined, rec.notes


def test_no_candidates_are_offered_for_a_gap_that_does_not_exist():
    """`_candidates` explains a gap; within tolerance there is none to explain."""
    rec = rc.reconcile(
        _walk_charged(QUOTA_BYTES, unreadable=5),
        _settled_check(),
        _row_snap(QUOTA_BYTES),
        DeletedScan(),
        "blocks",
    )
    assert rec.within_tolerance
    assert rec.candidates == [], rec.candidates


def test_a_real_gap_with_a_blocker_still_gets_its_candidates():
    rec = rc.reconcile(
        _walk_charged(QUOTA_BYTES // 4, unreadable=5),
        _settled_check(),
        _row_snap(QUOTA_BYTES),
        DeletedScan(),
        "blocks",
    )
    assert not rec.within_tolerance
    assert rec.verdict == rc.INCONCLUSIVE
    assert rec.candidates, "a gap under a blocker still needs explanations"


def test_an_unstamped_quota_figure_also_downgrades_an_agreement():
    """Constraint 20: a number with no timestamp has an unknown age."""
    rec = rc.reconcile(
        _walk_charged(QUOTA_BYTES),
        _settled_check(),
        _row_snap(QUOTA_BYTES, stamped=False),
        DeletedScan(),
        "blocks",
    )
    assert rec.verdict == rc.INCONCLUSIVE
    assert any("no timestamp" in b for b in rec.blockers)


def test_within_tolerance_has_one_definition():
    """Two callers needed the same test and disagreed about what it implied."""
    rec = rc.Reconciliation("blocks")
    assert rec.within_tolerance is False  # no gap measured at all
    rec.gap, rec.tolerance = 0, 100
    assert rec.within_tolerance is True
    rec.gap = -100
    assert rec.within_tolerance is True
    rec.gap = 101
    assert rec.within_tolerance is False


def test_the_panel_and_the_document_agree_about_the_downgrade():
    res = _walk_charged(QUOTA_BYTES, unreadable=11267)
    chk = _settled_check()
    snap = _row_snap(QUOTA_BYTES)
    rec = rc.reconcile(res, chk, snap, DeletedScan(), "blocks")
    panel = _flat(report.render_reconcile([rec], PLAIN))
    assert "INCONCLUSIVE" in panel and "reconciles" not in panel, panel
    doc = report.to_json(res, chk, snap, DeletedScan(), [rec])["reconciliation"][0]
    assert doc["verdict"] == rc.INCONCLUSIVE
    assert doc["difference"] == 0 and doc["blockers"]


# --- Constraint 10 in the document: None is not zero -------------------------
#
# `-c` skips every stat. The terminal says so plainly -- headline `n/a`, no byte
# column, `BY AGE` and `SETTLING` absent -- and the document published
# `size_bytes: 0`, `by_age` full of zeroes, `reclaimable[].bytes: 0` next to the
# terminal's `n/a` for the same figure, `top_by_inodes[].bytes: 0` beside a
# correctly-null `top_by_size`, and `settled: true` about files written seconds
# earlier whose mtime was never read. `by_uid` was worse than zero: the root
# directory's own blocks, a real number describing one inode of the tree.


def _counted(tmp_path, files=3, size=200000):
    d = tmp_path / "t" / "a"
    d.mkdir(parents=True)
    for i in range(files):
        (d / ("f%d.bin" % i)).write_bytes(b"\x5a" * size)
    (tmp_path / "t" / "__pycache__").mkdir()
    (tmp_path / "t" / "__pycache__" / "x.pyc").write_bytes(b"\x5a" * 5000)
    return str(tmp_path / "t")


def _doc(root, count_only):
    res = walk(root, threads=2, count_only=count_only)
    return report.to_json(res, SettleCheck(), None, None, None), res


def test_count_only_publishes_no_byte_figure_it_did_not_take(tmp_path):
    doc, _res = _doc(_counted(tmp_path), True)
    w = doc["walk"]
    assert w["size_bytes"] is None
    assert w["apparent_bytes"] is None
    assert w["by_age"] is None
    assert w["top_by_size"] is None


def test_count_only_does_not_claim_a_tree_is_settled(tmp_path):
    """The strongest claim in that section, from an instrument switched off."""
    doc, _res = _doc(_counted(tmp_path), True)
    s = doc["settling"]
    assert s["settled"] is None
    assert s["recent_files"] is None
    assert s["touched_files"] is None
    assert s["future_mtime_files"] is None


def test_count_only_nulls_the_allocation_verdict(tmp_path):
    doc, _res = _doc(_counted(tmp_path), True)
    alloc = doc["walk"]["allocation"]
    assert alloc["material"] is None
    assert alloc["ratio"] is None
    assert alloc["padding_bytes"] is None


def test_count_only_drops_the_owner_byte_column(tmp_path):
    """Not zero -- the root's own blocks, describing one inode of the tree."""
    doc, _res = _doc(_counted(tmp_path), True)
    for table in ("by_uid", "by_gid"):
        rows = doc["walk"][table]
        assert rows, table
        for entry in rows.values():
            assert entry["bytes"] is None, (table, entry)
            assert entry["inodes"] >= 1, (table, entry)


def test_count_only_agrees_with_the_terminal_about_reclaimable(tmp_path):
    """The terminal prints `n/a` for this figure; the document printed 0."""
    root = _counted(tmp_path)
    doc, res = _doc(root, True)
    groups = doc["walk"]["reclaimable"]
    assert groups, doc["walk"]
    for group in groups:
        assert group["bytes"] is None, group
        assert group["inodes"] >= 1, group
    assert "n/a" in _flat(report.render_reclaimable(res, PLAIN))


def test_count_only_nulls_the_byte_column_inside_the_inode_ranking(tmp_path):
    doc, _res = _doc(_counted(tmp_path), True)
    rows = doc["walk"]["top_by_inodes"]
    assert rows
    for row in rows:
        assert row["bytes"] is None, row
        assert row["inodes"] >= 1, row


def test_counts_that_were_measured_survive(tmp_path):
    """The fix must null what was skipped, not everything."""
    doc, _res = _doc(_counted(tmp_path), True)
    w = doc["walk"]
    assert w["inodes"] >= 5
    assert w["files"] >= 4 and w["dirs"] >= 3
    assert w["elapsed_seconds"] >= 0
    assert w["complete"] is True
    assert w["top_by_inodes"], "the inode ranking is the point of -c"


def test_a_normal_walk_publishes_every_figure_as_before(tmp_path):
    """No null leaks into the path that did take measurements."""
    doc, _res = _doc(_counted(tmp_path), False)
    w = doc["walk"]
    assert w["size_bytes"] > 0 and w["apparent_bytes"] > 0
    assert w["by_age"] is not None and len(w["by_age"]) == 5
    assert w["allocation"]["material"] in (True, False)
    assert all(v["bytes"] is not None for v in w["by_uid"].values())
    assert all(g["bytes"] is not None for g in w["reclaimable"])
    assert all(r["bytes"] is not None for r in w["top_by_inodes"])


def test_the_terminal_and_the_document_agree_about_what_c_measured(tmp_path):
    """`n/a` in one is `null` in the other -- the same claim, twice."""
    root = _counted(tmp_path)
    doc, res = _doc(root, True)
    text = _flat(report.render_walk(res, SettleCheck(), style=PLAIN))
    assert "n/a" in text, text
    assert "no sizes" in text, text
    assert doc["walk"]["size_bytes"] is None


# --- the README showed output the tool had stopped producing -------------------
#
# `README.md` is `readme =` in pyproject, so it is the PyPI landing page and the
# first thing anyone reads. Its "Reading the table" example still used the
# pre-RD-9 labels -- `5,435 files` in the headline and `files  entry` as the
# column header -- for a tool that prints `inodes` in both places. RD-9 is the
# tester's own finding about `files` naming two quantities; the code was fixed and
# the shipped documentation was not.
#
# Its percentages were wrong too: 661.5 GiB of 1.4 TiB is 46.1%, not the 31.9%
# printed beside it, so the illustration did not add up on its own terms.
#
# The example is now generated by this construction rather than hand-written, and
# this test is what keeps them in step.


def _readme_example_lines():
    """The exact figures in README.md's table, rendered by the real renderer."""
    from rapidu.walk import Entry

    gib = 1 << 30
    res = WalkResult("/project/lab/shared")
    res.elapsed = 4.12
    children = [("checkpoints", 661.5, 350), ("datasets", 343.8, 968)]
    children += [("run%02d" % i, 470.9 / 84.0, 49) for i in range(84)]
    total_bytes = total_inodes = 0
    for name, gibibytes, inodes in children:
        entry = Entry("/project/lab/shared/" + name, True)
        entry.size = int(gibibytes * gib)
        entry.files, entry.dirs = inodes, 0
        res.dir_agg[entry.path] = entry
        res.finished_tops.add(name)
        total_bytes += entry.size
        total_inodes += inodes
    res.size = res.apparent = total_bytes
    res.files, res.dirs = total_inodes - 435, 435
    style = ui.Style(color=False, unicode_ok=True, width=85, depth=8)
    body = report.render_compact(res, SettleCheck(), 2, False, style)
    return ui.box(body, style, width=85)


def _readme_text():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "README.md")
    if not os.path.exists(path):
        return None  # installed from a wheel, which does not carry the README
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


def test_the_readme_example_is_what_the_renderer_produces():
    """Documentation drift, caught mechanically instead of by eye."""
    text = _readme_text()
    if text is None:
        return
    for line in _readme_example_lines():
        assert line in text, "README example is stale:\n" + line


def test_the_readme_example_is_internally_consistent():
    """Its own shares add to 100%, which the hand-written version did not."""
    rows = [ln for ln in _readme_example_lines() if "%" in ln and "share" not in ln]
    assert rows
    shares = [float(ln.split("%")[0].split()[-1]) for ln in rows]
    assert abs(sum(shares) - 100.0) < 0.2, shares


def test_the_readme_does_not_use_files_for_the_inode_column():
    text = _readme_text()
    if text is None:
        return
    example = text.split("## Reading the table", 1)[1]
    block = example.split("```")[1]
    assert "inodes  entry" in block, block
    assert "files  entry" not in block, block
    # The headline is the line carrying the interpuncts between its three figures.
    headline = [ln for ln in block.splitlines() if ln.count("\u00b7") == 2]
    assert headline, block
    assert "inodes" in headline[0], headline[0]
    assert "files" not in headline[0], headline[0]


def test_the_inodes_flag_describes_the_column_it_ranks():
    """`-i` ranks the column headed `inodes`; its help said "file count"."""
    from rapidu import cli

    text = cli.build_parser().format_help()
    assert "rank by inode count" in text, text
    assert "rank by file count" not in text, text


# --- a directory that was deleted is not one you may not read -----------------
#
# On a shared filesystem the tree moves while you walk it -- usually because
# another job, often the reader's own, is writing to it. A directory deleted
# between being listed and being opened raised ENOENT and was filed under
# `unreadable_dirs`, so the report said "1 dir unreadable" and reconcile raised
# "1 directories could not be read". Both point at permissions, which sends the
# reader after access they already have.
#
# Classified from `errno`, never from the message: `strerror` is localised, so
# matching "No such file or directory" would stop working on a non-English host.


def test_a_vanished_directory_is_not_reported_as_unreadable():
    from rapidu.walk import SettleCheck as _S

    r = WalkResult("/scratch")
    r.files, r.dirs = 100, 10
    r.size = r.apparent = 1 << 20
    r.unreadable_dirs.append(("/scratch/gone", "No such file or directory"))
    r.vanished_dirs = 1
    text = _flat(report.render_walk(r, _S(), style=PLAIN))
    assert "1 dir vanished mid-walk" in text, text
    assert "unreadable" not in text, text


def test_a_refused_directory_still_says_unreadable():
    r = WalkResult("/scratch")
    r.files, r.dirs = 100, 10
    r.size = r.apparent = 1 << 20
    r.unreadable_dirs.append(("/scratch/secret", "Permission denied"))
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "1 dir unreadable" in text, text
    assert "vanished" not in text, text


def test_both_causes_are_counted_separately():
    r = WalkResult("/scratch")
    r.files, r.dirs = 100, 10
    r.size = r.apparent = 1 << 20
    for i in range(2):
        r.unreadable_dirs.append(("/scratch/p%d" % i, "Permission denied"))
    for i in range(5):
        r.unreadable_dirs.append(("/scratch/v%d" % i, "No such file or directory"))
    r.vanished_dirs = 5
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "2 dirs unreadable" in text, text
    assert "5 dirs vanished mid-walk" in text, text


def test_the_reconcile_blocker_names_the_right_remedy():
    rec_refused = rc.reconcile(
        _walk_charged(QUOTA_BYTES, unreadable=3),
        _settled_check(),
        _row_snap(QUOTA_BYTES),
        DeletedScan(),
        "blocks",
    )
    joined = " ".join(rec_refused.blockers)
    assert "could not be read" in joined, joined
    assert "vanished" not in joined, joined

    res = _walk_charged(QUOTA_BYTES, unreadable=4)
    res.vanished_dirs = 4
    rec_gone = rc.reconcile(res, _settled_check(), _row_snap(QUOTA_BYTES), DeletedScan(), "blocks")
    joined = " ".join(rec_gone.blockers)
    assert "vanished between being listed and being walked" in joined, joined
    assert "could not be read" not in joined, joined


def test_a_vanished_directory_still_makes_the_total_a_floor():
    """The cause changed; the consequence did not."""
    res = _walk_charged(QUOTA_BYTES, unreadable=1)
    res.vanished_dirs = 1
    assert not res.complete
    rec = rc.reconcile(res, _settled_check(), _row_snap(QUOTA_BYTES), DeletedScan(), "blocks")
    assert rec.verdict == rc.INCONCLUSIVE
    assert any("floor" in b for b in rec.blockers)


def test_the_document_separates_the_two_causes():
    res = _walk_charged(QUOTA_BYTES, unreadable=6)
    res.vanished_dirs = 4
    w = report.to_json(res, SettleCheck(), None, None, None)["walk"]
    assert w["unreadable_dirs"] == 6
    assert w["vanished_dirs"] == 4


def test_the_classification_is_by_errno_not_by_message():
    """A localised `strerror` must not change how a failure is classified."""
    from rapidu.walk import _VANISHED_ERRNOS

    assert errno.ENOENT in _VANISHED_ERRNOS
    assert errno.ESTALE in _VANISHED_ERRNOS  # NFS: removed or replaced on the server
    assert errno.ENOTDIR in _VANISHED_ERRNOS
    assert errno.EACCES not in _VANISHED_ERRNOS
    assert errno.EPERM not in _VANISHED_ERRNOS


def test_a_tree_mutating_under_the_walk_is_survived_and_disclosed(tmp_path):
    """The real thing: delete and recreate directories while the walk runs."""
    import shutil
    import threading

    root = tmp_path / "t"
    for d in range(40):
        sub = root / ("d%02d" % d)
        sub.mkdir(parents=True)
        for f in range(10):
            (sub / ("f%02d.bin" % f)).write_bytes(b"\x5a" * 4096)

    stop = threading.Event()

    def churn():
        while not stop.is_set():
            for d in range(0, 40, 2):
                target = str(root / ("d%02d" % d))
                try:
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    else:
                        os.makedirs(target)
                except OSError:
                    pass
                if stop.is_set():
                    break

    worker = threading.Thread(target=churn)
    worker.start()
    try:
        res = walk(str(root), threads=8)
    finally:
        stop.set()
        worker.join()

    # The walk must finish, and must not claim a total it could not take.
    assert res.files >= 0 and res.dirs >= 1
    assert res.vanished_dirs <= len(res.unreadable_dirs)
    if res.unreadable_dirs:
        assert not res.complete
    # Nothing was mislabelled: every recorded reason is consistent with its count.
    gone = sum(1 for _p, why in res.unreadable_dirs if "No such file" in why or "Stale" in why)
    assert res.vanished_dirs >= 0 and gone >= 0
    doc = report.to_json(res, SettleCheck(), None, None, None)["walk"]
    assert doc["vanished_dirs"] == res.vanished_dirs


# --- the same two questions for entries, which only directories could answer ---
#
# `unstatable` was a bare count: a file deleted while the walk was reading its
# directory and a file that may not be stat'ed were the same number, the report
# said "40 entries unstatable" and reconcile said "could not be stat'ed" -- both
# pointing at permissions -- and neither named a single one of them.
#
# `unreadable_dirs` has carried its paths from the start, for the reason stated
# beside `unreadable_dir_paths`: a consumer that knows three directories were
# unreadable cannot act on it; one that knows which can. That rule had never been
# applied to the sibling counter.


def _entries(unstatable=0, vanished=0, size=100 << 30):
    r = _walk_charged(size)
    r.unstatable = unstatable
    r.vanished_entries = vanished
    return r


def test_a_vanished_entry_is_not_reported_as_unstatable():
    text = _flat(report.render_walk(_entries(7, 7), _settled_check(), style=PLAIN))
    assert "7 entries vanished mid-walk" in text, text
    assert "unstatable" not in text, text


def test_a_refused_entry_still_says_unstatable():
    text = _flat(report.render_walk(_entries(4, 0), _settled_check(), style=PLAIN))
    assert "4 entries unstatable" in text, text
    assert "vanished" not in text, text


def test_both_entry_causes_are_counted_separately():
    text = _flat(report.render_walk(_entries(9, 6), _settled_check(), style=PLAIN))
    assert "3 entries unstatable" in text, text
    assert "6 entries vanished mid-walk" in text, text


def test_one_of_each_agrees_with_its_noun():
    text = _flat(report.render_walk(_entries(2, 1), _settled_check(), style=PLAIN))
    assert "1 entry unstatable" in text, text
    assert "1 entry vanished mid-walk" in text, text


def test_the_entry_blocker_names_the_right_remedy():
    refused = rc.reconcile(
        _entries(4, 0), _settled_check(), _row_snap(QUOTA_BYTES), DeletedScan(), "blocks"
    )
    joined = " ".join(refused.blockers)
    assert "could not be stat'ed" in joined, joined
    assert "vanished" not in joined, joined

    gone = rc.reconcile(
        _entries(4, 4), _settled_check(), _row_snap(QUOTA_BYTES), DeletedScan(), "blocks"
    )
    joined = " ".join(gone.blockers)
    assert "vanished before they could be stat'ed" in joined, joined
    assert "could not be stat'ed" not in joined, joined


def test_the_document_carries_the_paths_not_only_the_count():
    res = _entries(3, 2)
    res.unstatable_paths = ["/scratch/a", "/scratch/b", "/scratch/c"]
    w = report.to_json(res, SettleCheck(), None, None, None)["walk"]
    assert w["unstatable_entries"] == 3
    assert w["vanished_entries"] == 2
    assert w["unstatable_paths"] == ["/scratch/a", "/scratch/b", "/scratch/c"]


def test_the_path_sample_is_bounded():
    from rapidu.walk import _UNSTAT_SAMPLE_CAP

    res = _entries(5000, 5000)
    res.unstatable_paths = ["/scratch/f%d" % i for i in range(_UNSTAT_SAMPLE_CAP)]
    w = report.to_json(res, SettleCheck(), None, None, None)["walk"]
    assert len(w["unstatable_paths"]) <= 64
    assert w["unstatable_entries"] == 5000, "the count is not capped, only the sample"


def test_files_deleted_during_the_walk_are_classified_and_named(tmp_path):
    """The real thing: unlink files while the walk is stat'ing them."""
    import shutil
    import threading

    root = tmp_path / "t"
    for d in range(24):
        sub = root / ("d%02d" % d)
        sub.mkdir(parents=True)
        for f in range(50):
            (sub / ("f%02d.bin" % f)).write_bytes(b"\x5a" * 512)

    stop = threading.Event()

    def churn():
        for d in range(24):
            for f in range(50):
                if stop.is_set():
                    return
                with contextlib.suppress(OSError):
                    os.unlink(str(root / ("d%02d" % d) / ("f%02d.bin" % f)))

    worker = threading.Thread(target=churn)
    worker.start()
    try:
        res = walk(str(root), threads=8)
    finally:
        stop.set()
        worker.join()
    shutil.rmtree(str(root), ignore_errors=True)

    # Whatever the race produced, the accounting has to hold.
    assert res.vanished_entries <= res.unstatable
    assert len(res.unstatable_paths) <= 64
    if res.unstatable:
        assert not res.complete
    # Everything recorded was a stat failure on this tree, not a stray path.
    for path in res.unstatable_paths:
        assert path.startswith(str(root)), path
    doc = report.to_json(res, SettleCheck(), None, None, None)["walk"]
    assert doc["vanished_entries"] == res.vanished_entries
    assert doc["unstatable_entries"] == res.unstatable


def test_count_only_also_classifies_a_vanished_entry(tmp_path):
    """`-c` has its own stat-free failure site; it must classify the same way."""
    root = tmp_path / "t"
    root.mkdir()
    for f in range(20):
        (root / ("f%02d" % f)).write_bytes(b"x")
    res = walk(str(root), threads=2, count_only=True)
    # Nothing raced here, so both are zero -- what is pinned is that the fields
    # exist on the count-only path and stay consistent.
    assert res.vanished_entries == 0
    assert res.unstatable == 0
    assert res.unstatable_paths == []


# --- symlink loops, and the interrupt path ------------------------------------
#
# Two ways a walk goes wrong that had only been exercised synthetically. The loops
# are fine -- symlinks are entries, never descended, so a directory symlink
# pointing at its own ancestor cannot spin -- and that is worth a standing test
# because a hang is the worst failure this tool has.
#
# The interrupt was not fine: `res.elapsed` is rendered `{:.2f}s` everywhere
# except the INTERRUPTED line, which used `{:.0f}s` and so reported "INTERRUPTED
# after 0s" for a walk cut short after 0.4s -- directly above "20,051 inodes
# scanned before the interrupt". A Ctrl-C inside the first second is the common
# case. The settle lines already guard sub-second values with `if gap >= 1`; this
# one did not.


def test_a_symlink_loop_does_not_spin(tmp_path):
    root = tmp_path / "t"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "a" / "b" / "c" / "f.bin").write_bytes(b"\x5a" * 3000)
    os.symlink("../../..", str(root / "a" / "b" / "c" / "up"))
    os.symlink(str(root), str(root / "a" / "self"))
    os.symlink(".", str(root / "here"))
    res = walk(str(root), threads=4)
    # 4 directories, 1 file, 3 symlinks -- each symlink counted once, never entered.
    assert res.dirs == 4, res.dirs
    assert res.symlinks == 3, res.symlinks
    assert res.files == 4, res.files  # the regular file plus the three links


def test_a_symlink_loop_still_agrees_with_du(tmp_path):
    import subprocess

    root = tmp_path / "t"
    (root / "a").mkdir(parents=True)
    (root / "a" / "f.bin").write_bytes(b"\x5a" * 3000)
    os.symlink("..", str(root / "a" / "up"))
    res = walk(str(root), threads=4)
    out = subprocess.Popen(
        ["du", "-s", "--block-size=1", str(root)], stdout=subprocess.PIPE
    ).communicate()[0]
    expected = int(out.decode().split()[0])
    assert res.size == expected, (res.size, expected)


def test_an_interrupted_walk_reports_its_real_elapsed():
    """`{:.0f}` turned every sub-second interrupt into "after 0s"."""
    r = WalkResult("/scratch/big")
    r.files, r.dirs = 20000, 51
    r.size = r.apparent = 10 << 20
    r.elapsed = 0.32
    r.partial = True
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "INTERRUPTED after 0.32s" in text, text
    assert "after 0s" not in text, text


def test_a_sub_second_interrupt_is_not_reported_as_instant():
    for elapsed in (0.01, 0.07, 0.4, 0.99):
        r = WalkResult("/scratch/big")
        r.files, r.dirs = 100, 5
        r.size = r.apparent = 1 << 20
        r.elapsed = elapsed
        r.partial = True
        text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
        assert "after 0s " not in text, (elapsed, text)
        assert "%.2fs" % elapsed in text, (elapsed, text)


def test_the_interrupt_elapsed_matches_the_other_renderings():
    """One quantity, one precision -- the facts line already used `{:.2f}`."""
    r = WalkResult("/scratch/big")
    r.files, r.dirs = 100, 5
    r.size = r.apparent = 1 << 20
    r.elapsed = 1.234
    r.partial = True
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert text.count("1.23s") >= 1, text


def test_an_interrupted_walk_claims_no_total_or_share():
    r = WalkResult("/scratch/big")
    r.files, r.dirs = 20000, 51
    r.size = r.apparent = 10 << 20
    r.elapsed = 0.32
    r.partial = True
    text = _flat(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "no total and no share of anything" in text, text
    doc = report.to_json(r, SettleCheck(), None, None, None)["walk"]
    assert doc["interrupted"] is True
    assert doc["complete"] is False


def test_a_real_sigint_yields_a_partial_report_not_a_crash(tmp_path):
    """End to end, made deterministic by the rate limiter rather than by luck.

    A first version passed `--max-dirs-per-sec 200` over 120 directories and the
    walk finished in 0.08s, before the signal -- so the test took its own
    "finished first" escape and asserted nothing. The token bucket's burst is
    `max(rate, 1)`, so 121 opens fit inside it and nothing was throttled.

    With 200 directories at 20/s the burst covers 20 of them and the remaining 180
    are paced, putting a floor of about nine seconds on the walk however fast the
    host is. A signal at half a second is then reliably mid-walk, and the test
    still finishes in under a second because the process is killed, not waited on.
    """
    import signal
    import subprocess
    import time

    root = tmp_path / "t"
    for d in range(200):
        sub = root / ("d%03d" % d)
        sub.mkdir(parents=True)
        for f in range(5):
            (sub / ("f%02d" % f)).write_bytes(b"\x5a" * 1024)

    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path), COLUMNS="100")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rapidu",
            "--no-box",
            "--no-quota",
            "--no-deleted",
            "--max-dirs-per-sec",
            "20",
            str(root),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    time.sleep(0.5)
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=60)
    text = out.decode("utf-8", "replace")
    errtext = err.decode("utf-8", "replace")

    assert "PARTIAL" in text, (
        "the rate limiter should have kept the walk running past the signal:\n"
        + text[:600]
        + errtext[-400:]
    )
    assert proc.returncode == 1, (proc.returncode, errtext[-400:])
    assert "INTERRUPTED" in text, text
    assert "no total and no share of anything" in text, text
    # The elapsed must not read as instantaneous when inodes were scanned.
    assert "INTERRUPTED after 0s" not in text, text
    # rapidu's own code must not have raised. A KeyboardInterrupt landing during
    # interpreter startup -- inside `site`, importing an unrelated package -- is
    # not its traceback to own, and that is what an early signal produces.
    assert "rapidu/walk.py" not in errtext, errtext[-600:]
    assert "rapidu/report.py" not in errtext, errtext[-600:]


# --- verified against real mounts and real permissions, not fixtures ----------
#
# RD-8's `-x` disclosure was checked against a synthetic mount table, and the
# refused half of the vanished/unstatable split was only ever constructed. Both
# hold on the real thing; these keep it that way.


def _a_directory_containing_a_real_mount():
    """A walkable directory with at least one foreign filesystem mounted inside.

    Read from `/proc/mounts` rather than hard-coded: on Linux `/dev` holds
    `devtmpfs` with `tmpfs`, `devpts` and friends beneath it, but that is a fact
    about the host, not something to assume.
    """
    try:
        with open("/proc/mounts", "rb") as handle:
            entries = [
                line.decode("utf-8", "replace").split()[1]
                for line in handle
                if len(line.split()) > 2
            ]
    except OSError:
        return None, []
    parents = {}
    for point in entries:
        parent = os.path.dirname(point.rstrip("/"))
        if parent in ("", "/") or not os.path.isdir(parent):
            continue
        if not os.access(parent, os.R_OK | os.X_OK):
            continue
        parents.setdefault(parent, []).append(point)
    for parent, points in sorted(parents.items(), key=lambda kv: -len(kv[1])):
        if len(points) >= 1 and parent in entries:
            return parent, points
    return None, []


def test_one_file_system_skips_real_mount_points():
    parent, points = _a_directory_containing_a_real_mount()
    if parent is None:
        return  # no usable mount boundary on this host
    crossing = walk(parent, threads=4, one_file_system=False)
    bounded = walk(parent, threads=4, one_file_system=True)
    assert len(crossing.by_dev) > 1, (parent, len(crossing.by_dev))
    assert len(bounded.by_dev) == 1, (parent, len(bounded.by_dev))
    assert bounded.crossed > 0, parent
    assert bounded.inodes <= crossing.inodes


def test_the_skipped_mounts_are_named_not_just_counted():
    parent, _points = _a_directory_containing_a_real_mount()
    if parent is None:
        return
    res = walk(parent, threads=4, one_file_system=True)
    if not res.crossed:
        return
    text = _flat(report.render_walk(res, SettleCheck(), style=PLAIN))
    assert "on other filesystems skipped (-x)" in text, text
    # Every listed path must be a real mount point, and at least one must show.
    assert res.crossed_paths, res.crossed
    shown = [p for p in res.crossed_paths if os.path.basename(p) in text]
    assert shown, (text, res.crossed_paths[:4])


def test_a_listable_but_unsearchable_directory_is_refused_not_vanished(tmp_path):
    """mode 444: `readdir` succeeds, `stat` on the children does not.

    The real counterpart to the vanished case -- EACCES rather than ENOENT -- and
    the branch that had only ever been constructed.
    """
    root = tmp_path / "t"
    blocked = root / "nosearch"
    blocked.mkdir(parents=True)
    for i in range(5):
        (blocked / ("f%d.bin" % i)).write_bytes(b"\x5a" * 4096)
    os.chmod(str(blocked), 0o444)
    try:
        res = walk(str(root), threads=4)
    finally:
        os.chmod(str(blocked), 0o755)
    if not res.unstatable:
        return  # running as a user who bypasses the check, e.g. root
    assert res.vanished_entries == 0, res.vanished_entries
    assert res.unstatable == 5, res.unstatable
    assert len(res.unstatable_paths) == 5, res.unstatable_paths
    text = _flat(report.render_walk(res, SettleCheck(), style=PLAIN))
    assert "5 entries unstatable" in text, text
    assert "vanished" not in text, text


def test_both_floor_causes_exit_the_same_way(tmp_path):
    """A floor is a floor: an unreadable directory and an unstatable entry both
    make the total incomplete, so both must reach the same exit code."""
    from rapidu import cli

    root = tmp_path / "t"
    blocked = root / "blocked"
    blocked.mkdir(parents=True)
    (blocked / "f.bin").write_bytes(b"\x5a" * 4096)

    codes = {}
    for mode, label in ((0o444, "unstatable"), (0o000, "unreadable"), (0o755, "clean")):
        os.chmod(str(blocked), mode)
        try:
            codes[label] = cli.main([str(root), "--no-box", "--no-quota", "--no-deleted"])
        finally:
            os.chmod(str(blocked), 0o755)
    assert codes["clean"] == cli.EXIT_OK, codes
    # Skip the permission arms if this user bypasses them.
    if codes["unreadable"] != cli.EXIT_OK:
        assert codes["unreadable"] == cli.EXIT_ATTENTION, codes
        assert codes["unstatable"] == codes["unreadable"], codes


# --- a command the output encoding would rewrite ------------------------------
#
# `_shell_command` refuses when `ui.printable` would alter the path, on the stated
# grounds that an escaped path is not the path. The same is true of the *output*
# encoding and it was not checked: `printable` passes `café` through untouched,
# `ui.encode_safe` at the write turns it into `caf\xe9`, and inside single quotes
# that is seven literal characters naming a directory that does not exist.
#
# Measured under `PYTHONIOENCODING=ascii`:
#     rm -rf '.../enc/caf\xe9/cache/torch'
#
# A login node with UTF-8 and a batch node with POSIX is an ordinary cluster split,
# and this landed on the only output the tool invites anyone to execute.


class _Narrow:
    """A stream whose encoding cannot carry non-ASCII, as `PYTHONIOENCODING` gives."""

    encoding = "ascii"

    def write(self, _text):
        raise AssertionError("nothing should be written through this probe")


def test_an_ordinary_accented_path_is_a_command_on_a_utf8_stream():
    import sys as _sys

    class Wide(object):
        encoding = "utf-8"

    saved, _sys.stdout = _sys.stdout, Wide()
    try:
        command = report._shell_command("rm -rf {path}", "/data/café/cache/torch")
    finally:
        _sys.stdout = saved
    assert command == "rm -rf '/data/café/cache/torch'", command


def test_the_same_path_is_refused_on_a_stream_that_cannot_carry_it():
    import sys as _sys

    saved, _sys.stdout = _sys.stdout, _Narrow()
    try:
        command = report._shell_command("rm -rf {path}", "/data/café/cache/torch")
    finally:
        _sys.stdout = saved
    assert command == "", command


def test_the_two_refusals_name_different_remedies():
    """One is the name's fault and one is the terminal's; the fix differs."""
    import sys as _sys

    saved, _sys.stdout = _sys.stdout, _Narrow()
    try:
        encoding_note = report._no_command_note("/data/café/cache/torch")
    finally:
        _sys.stdout = saved
    control_note = report._no_command_note("/data/ctrl\x01dir/cache/torch")
    assert "UTF-8 locale" in encoding_note, encoding_note
    assert "unprintable characters" in control_note, control_note
    assert encoding_note != control_note


def test_both_refusals_are_inert_if_pasted():
    import sys as _sys

    saved, _sys.stdout = _sys.stdout, _Narrow()
    try:
        notes = [
            report._no_command_note("/data/café/x"),
            report._no_command_note("/data/ctrl\x01dir/x"),
        ]
    finally:
        _sys.stdout = saved
    for note in notes:
        assert note.startswith("#"), note


def test_an_ascii_stream_refuses_the_command_end_to_end(tmp_path):
    """The whole pipeline, with the encoding set the way a batch node sets it."""
    import subprocess

    root = tmp_path / "café"
    (root / "cache" / "torch").mkdir(parents=True)
    (root / "cache" / "torch" / "blob.bin").write_bytes(b"\x5a" * 40000)

    def run(**overrides):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path), COLUMNS="200")
        env.update(overrides)
        out = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "rapidu",
                "-a",
                "--no-box",
                "--no-quota",
                "--no-deleted",
                str(root),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        ).communicate()[0]
        return out.decode("utf-8", "replace")

    wide = run(PYTHONIOENCODING="utf-8")
    narrow = run(PYTHONIOENCODING="ascii", LC_ALL="C")

    assert "rm -rf " in wide, wide[-500:]
    assert "café" in wide, wide[-500:]
    # The corrupted form must never appear as a command.
    assert "caf\\xe9" not in wide, wide[-500:]
    assert "rm -rf " not in narrow, narrow[-500:]
    assert "UTF-8 locale" in narrow, narrow[-500:]


def test_a_narrow_stream_still_reports_the_tree(tmp_path):
    """Refusing the command must not cost the measurement."""
    import subprocess

    root = tmp_path / "日本語"
    root.mkdir()
    (root / "f.bin").write_bytes(b"\x5a" * 30000)
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(sys.path),
        PYTHONIOENCODING="ascii",
        LC_ALL="C",
        COLUMNS="100",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "rapidu", "--no-box", "--no-quota", "--no-deleted", str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    out, err = proc.communicate()
    text = out.decode("utf-8", "replace")
    assert proc.returncode == 0, (proc.returncode, err.decode("utf-8", "replace")[-400:])
    assert "inodes" in text, text
    assert "KiB" in text, text
    assert "Traceback" not in err.decode("utf-8", "replace")


# --- filenames are bytes, not text -------------------------------------------
#
# A tarball unpacked from another system routinely carries latin-1 names, so a
# directory whose name is not valid UTF-8 is ordinary on a shared filesystem.
# `os.listdir` hands those back surrogate-escaped (`b"caf\xe9"` -> `"caf\udce9"`),
# which is Python's lossless representation of the raw bytes.
#
# All three layers hold, and none had been tested: the walk measures them and
# agrees with `du`; the text report renders the escape visibly rather than raising;
# and `--json` stays pure ASCII, parses back, and round-trips to a string `os` can
# reopen the file with.


def _byte_named_tree(tmp_path):
    """Three directories whose names are not valid UTF-8, each holding a cache."""
    root = os.path.join(str(tmp_path).encode("utf-8"), b"t")
    for name in (b"caf\xe9", b"latin\xff1", b"mixed\xc3\xa9\xe9"):
        leaf = os.path.join(root, name, b"cache", b"torch")
        os.makedirs(leaf)
        with open(os.path.join(leaf, b"blob.bin"), "wb") as handle:
            handle.write(b"\x5a" * 30000)
    return root.decode("utf-8", "surrogateescape")


def test_a_name_that_is_not_valid_utf8_is_still_measured(tmp_path):
    import subprocess

    root = _byte_named_tree(tmp_path)
    res = walk(root, threads=4)
    assert res.dirs == 10, res.dirs  # root + 3 x (name, cache, torch)
    assert res.files == 3, res.files
    out = subprocess.Popen(
        ["du", "-s", "--block-size=1", root], stdout=subprocess.PIPE
    ).communicate()[0]
    assert res.size == int(out.decode("utf-8", "replace").split()[0])


def test_the_text_report_renders_the_escape_instead_of_raising(tmp_path):
    res = walk(_byte_named_tree(tmp_path), threads=4)
    text = _flat(report.render_compact(res, SettleCheck(), 10, False, PLAIN))
    # The surrogate is shown as an escape, and nothing raw reaches the line.
    assert "\\udce9" in text, text
    assert "\udce9" not in text, "a lone surrogate must not be emitted raw"


def test_the_document_is_ascii_parses_and_round_trips(tmp_path):
    root = _byte_named_tree(tmp_path)
    res = walk(root, threads=4)
    text = json.dumps(report.to_json(res, SettleCheck(), None, None, None), indent=2)
    assert all(ord(ch) < 128 for ch in text), "json.dumps must escape, not emit raw"
    doc = json.loads(text)
    paths = [row["path"] for row in doc["walk"]["top_by_inodes"]]
    assert paths, doc["walk"]
    # The round-tripped string is the one `os` needs to reach the file again.
    for path in paths:
        assert os.path.isdir(path) or os.path.isfile(path), path


def test_a_surrogate_path_is_refused_with_the_unprintable_reason(tmp_path):
    """`printable` is checked before `encode_safe`, and that ordering matters.

    No locale can encode a lone surrogate, so "re-run under a UTF-8 locale" would
    be useless advice. `ui.printable` flags surrogates, so the earlier check wins
    and the reader is told the name itself is unpasteable -- which is true.
    """
    path = "/data/caf\udce9/cache/torch"
    assert report._shell_command("rm -rf {path}", path) == ""
    note = report._no_command_note(path)
    assert "unprintable characters" in note, note
    assert "UTF-8 locale" not in note, note


def test_no_command_is_offered_for_any_byte_named_cache(tmp_path):
    res = walk(_byte_named_tree(tmp_path), threads=4)
    lines = report.render_reclaimable(res, PLAIN)
    assert lines, "the caches should still be found and listed"
    assert not [ln for ln in lines if ln.strip().startswith("rm -rf")]
    notes = [ln.strip() for ln in lines if ln.strip().startswith("#")]
    assert notes, lines
    for note in notes:
        assert "unprintable characters" in note, note


# --- the rule applied to this session's own additions, and beyond -------------
#
# Auditing the diff the way the diff had been auditing everything else. One of my
# own new messages read "1 entry vanished before *they* could be stat'ed" -- the
# noun agreed and the pronoun did not, when `render_allocation` already carries
# the idiom ("it" for one file, "they" otherwise).
#
# Sweeping every string that interpolates a value next to a fixed verb then found
# two older ones: "4 files (66.7%) *has* not been modified in over a year" -- a
# string quoted verbatim in the round-45 report, filed there for its denominator
# while the disagreement beside it went unremarked -- and "1 of the 1
# unlinked-but-open inodes found *are* owned by other users".


def test_the_vanished_entry_blocker_agrees_in_pronoun_too():
    for count, pronoun in ((1, " it could"), (2, " they could"), (9, " they could")):
        res = _walk_charged(QUOTA_BYTES)
        res.unstatable = count
        res.vanished_entries = count
        rec = rc.reconcile(res, _settled_check(), _row_snap(QUOTA_BYTES), DeletedScan(), "blocks")
        joined = " ".join(rec.blockers)
        assert pronoun in joined, (count, joined)


def test_the_cold_data_sentence_agrees_with_its_subject():
    kib = 1 << 10
    for cold, total, verb in ((1, 6, "has"), (4, 6, "have"), (12, 20, "have")):
        r = WalkResult("/t")
        r.files, r.dirs = total, 1
        r.size = r.apparent = 200 * kib
        r.by_age = [
            (190 * kib, total - cold),
            (0, 0),
            (0, 0),
            (0, 0),
            (4 * kib, cold),
        ]
        text = _flat(report.render_age(r, PLAIN))
        assert "%s not been modified" % verb in text, (cold, text[-120:])
        wrong = "has" if verb == "have" else "have"
        assert "%s not been modified" % wrong not in text, (cold, text[-120:])


def test_a_byte_measure_keeps_the_singular_verb():
    """The same sentence can carry a mass noun, where "has" is correct."""
    kib = 1 << 10
    r = WalkResult("/t")
    r.files, r.dirs = 20, 1
    r.size = r.apparent = 200 * kib
    r.by_age = [(10 * kib, 19), (0, 0), (0, 0), (0, 0), (180 * kib, 1)]
    text = _flat(report.render_age(r, PLAIN))
    assert "KiB (90.0%) has not been modified" in text, text


def test_the_other_users_note_agrees_with_its_count():
    from rapidu.deleted import DeletedFile

    def scan_with(total, mine_count):
        scan = DeletedScan()
        scan.available = True
        for i in range(total):
            scan.files.append(
                DeletedFile(
                    1,
                    100 + i,
                    4096,
                    "/scratch/gone%d" % i,
                    uid=os.getuid() if i < mine_count else os.getuid() + 1000,
                    gid=os.getgid(),
                )
            )
        return scan

    for total, mine_count, verb in ((1, 0, " is owned"), (3, 2, " is owned"), (5, 3, " are owned")):
        snap = _row_snap(QUOTA_BYTES)
        snap.rows[0].scope = "user"
        snap.rows[0].fileset = "youzhi"
        rec = rc.reconcile(
            _walk_charged(QUOTA_BYTES),
            _settled_check(),
            snap,
            scan_with(total, mine_count),
            "blocks",
        )
        note = " ".join(rec.notes)
        if "owned by other users" not in note:
            continue  # this row was not user-scoped on this host
        assert verb in note, (total, mine_count, note)
        assert "inodes found is" not in note or total > 1, note


def test_no_message_pairs_a_count_with_a_fixed_verb():
    """The sweep that found all four, kept as a standing check.

    Read through `ast` rather than by matching raw literals: a message assembled
    from adjacent string pieces is one string to a reader, and a first version that
    scanned literals kept breaking whenever `ruff` reflowed a line across the
    boundary. `ast.Constant` gives the joined value, which is what the reader sees.

    Anything interpolating a value beside a bare agreement-sensitive word is listed
    below with the reason it is safe. A new one fails this test, which is the
    point: three of the four had survived every prior pass because nobody had
    enumerated them.
    """
    import ast
    import glob
    import re

    safe = (
        # The interpolated value cannot be 1, or the word agrees with something
        # other than it. One entry per message, with the reason.
        "quota rows govern this path equally",  # `_pick_row` returns early at 1
        "rows govern {} equally",  # guarded by `len(named) > 1`
        "GPFS filesystems were asked",  # only fires above the cap of 8
        "threads is not a walk",  # "is" agrees with the rejected value
        "clamped to",  # advisory prose about the cap
        "is not on PATH",  # a command name
        "is on PATH but exited",  # a command name
        "is not a directory",  # a path
        "faster on 1.7M GPFS files",  # a ratio, in help text
        "quota budget was exhausted",  # a duration
        "sweep was abandoned after",  # a duration
        "UTC offset",  # a duration
        "snapshot taken",  # a duration
        "INTERRUPTED after",  # a duration
        "figure is provisional",  # a duration
        "the figure above is provisional",  # "is" agrees with "the figure";
        # the leading count goes through `_settle_subject`, which agrees itself
        "was inferred from its name",  # a fileset name
        "is the fileset this path sits in",  # a fileset name
        "most narrowly scoped",  # a fileset name
        "gid could not be resolved from",  # a fileset name
        "no {} quota row maps to",  # a kind and a path
        "quota covers {}",  # mount points
        "quota row is charged to the",  # "is" agrees with "the quota row"
        "quota row is user-scoped",  # likewise
        "walked that is charged to it is compared",  # byte figures: mass nouns
        "you own of the {} walked is compared",  # byte figures
        "of the {} quota figure",  # a percentage
        "difference is within",  # a byte figure
        "allocated than the walk read",  # a byte figure
        "allocated: this tree is still moving",  # a byte figure
        "of that is unlinked-but-open",  # a byte figure
        "These files are sparse",  # two byte figures
        "of the data size",  # a byte figure and a percentage
        "none of the {} walked is",  # a rendered phrase
        "wins it on the denominator",  # prose after an agreed clause
        "ratio, as a figure that is never",  # a docstring
        "inodes are the cost to watch",  # always plural where this fires
        "more holding it",  # a bare "+N more"
        "of them disappeared between the walk",  # "1 of them" is correct
        "walk thread{} still blocked",  # carries its own was/were
        "so {} occupy {} of padding",  # carries its own averages/average and it/they;
        # the unit clause is now optional, so the literal no longer names it
        # Verified to agree, each with a test above.
        "could not be read, so the walk total",
        "vanished between being listed",
        "vanished before {} could",
        "not been modified in over a year",
        "top-level {} walked to completion",
        "found no change in {} of them",
        "found {} owned by other users",
        "walked that {} charged to it {} compared",
        "you own of the {} walked {} compared",
        "fewer than {} inodes and cannot be ranked",
        "of these {} an mtime ahead",
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for path in sorted(glob.glob(os.path.join(root, "src", "rapidu", "*.py"))):
        with open(path, "rb") as handle:
            tree = ast.parse(handle.read().decode("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value
            if "{}" not in text and "{:" not in text:
                continue
            if not re.search(r"\b(they|them|is|are|was|were|has|have)\b", text):
                continue
            if any(fragment in text for fragment in safe):
                continue
            offenders.append((os.path.basename(path), " ".join(text.split())[:96]))
    assert not offenders, offenders


# --- `shutil.which` is not a runnability test --------------------------------
#
# Sweeping every flag and command the tool names in a message: 24 of the 27 long
# flags are its own and exist, and the other three belong to the tool they are
# passed to (`du --block-size`, `git gc --aggressive --prune=now`). The commands
# checked out too, except one.
#
# `huggingface-cli` was renamed to `hf`, and the old name is still installed as a
# stub that prints "`huggingface-cli` is deprecated and no longer works. Use `hf`
# instead." So it satisfies `shutil.which`, passes rapidu's own gate, and is a
# dead end -- which is the failure RD-12 filed against unchecked commands, arriving
# by a route that check cannot see: PATH-presence stopped implying runnability.
#
# Both names are kept, newest first, because both clusters are real: a host with an
# older `huggingface_hub` has only `huggingface-cli`, and there it works.


def test_alternatives_prefer_the_tool_that_is_present(monkeypatch):
    monkeypatch.setattr(
        report.shutil, "which", lambda tool: "/usr/bin/hf" if tool == "hf" else None
    )
    assert report._first_runnable(("hf cache prune", "huggingface-cli delete-cache")) == (
        "hf cache prune"
    )


def test_alternatives_fall_back_to_the_older_name(monkeypatch):
    """An older cluster has only `huggingface-cli`, and there it is the right answer."""
    monkeypatch.setattr(
        report.shutil,
        "which",
        lambda tool: "/usr/bin/huggingface-cli" if tool == "huggingface-cli" else None,
    )
    assert report._first_runnable(("hf cache prune", "huggingface-cli delete-cache")) == (
        "huggingface-cli delete-cache"
    )


def test_with_neither_present_the_newest_name_is_reported(monkeypatch):
    """So the reader is told about the tool worth installing, not the dead one."""
    monkeypatch.setattr(report.shutil, "which", lambda _tool: None)
    assert report._first_runnable(("hf cache prune", "huggingface-cli delete-cache")) == (
        "hf cache prune"
    )


def test_a_single_command_and_none_pass_straight_through():
    assert report._first_runnable("pip cache purge") == "pip cache purge"
    assert report._first_runnable(None) is None


def test_the_huggingface_rule_resolves_to_a_command_that_runs(monkeypatch):
    monkeypatch.setattr(
        report.shutil, "which", lambda tool: "/usr/bin/hf" if tool == "hf" else None
    )
    for path in ("/home/u/.cache/huggingface/hub", "/home/u/.cache/huggingface"):
        match = report._reclaimable_match(path)
        assert match is not None, path
        assert match[1] == "hf cache prune", match


def test_a_deprecated_stub_no_longer_wins_over_its_successor(monkeypatch):
    """Both on PATH -- the state of a host mid-migration -- must pick the live one."""
    monkeypatch.setattr(report.shutil, "which", lambda _tool: "/usr/bin/x")
    match = report._reclaimable_match("/home/u/.cache/huggingface")
    assert match[1] == "hf cache prune", match


def test_no_reclaim_rule_names_a_flag_of_this_tool_that_does_not_exist():
    """A message suggesting `--foo` where `--foo` was renamed is a dead end too.

    Docstrings are excluded: they are internal notes, and one of them refers to a
    `--support-bundle` mode that is a design sketch rather than a flag.
    """
    import ast
    import glob
    import re

    from rapidu import cli

    known = {opt for action in cli.build_parser()._actions for opt in action.option_strings}
    # Long flags belonging to the tool each command invokes, not to rapidu.
    foreign = {"--block-size", "--aggressive", "--prune", "--export"}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for path in sorted(glob.glob(os.path.join(root, "src", "rapidu", "*.py"))):
        with open(path, "rb") as handle:
            tree = ast.parse(handle.read().decode("utf-8"))
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                body = getattr(node, "body", None)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                ):
                    docs.add(id(body[0].value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docs:
                continue
            for flag in re.findall(r"(?<![\w-])--[A-Za-z][A-Za-z0-9-]*", node.value):
                if flag not in known and flag not in foreign:
                    offenders.append((os.path.basename(path), flag))
    assert not offenders, offenders


# --- a small allocation is not an inlined file --------------------------------
#
# `MIN_ALLOC_UNIT` exists because a 100-byte file given one 512-byte sector would
# drag the measured allocation unit from a true 16 KiB down to 512 B. That
# exclusion is right, and it was doing two jobs: it also *classified* the file as
# inlined, which is a different claim and a false one. `st_blocks == 1` means
# blocks were allocated; genuinely inlined data reports `st_blocks == 0`.
#
# Measured on 50,000 files of 64 bytes: `inline_files: 50000`,
# `padding_bytes: 0`, and no packing advice -- on the most packable tree there is.
# The padding is 21.4 MiB.


def test_tiny_files_are_padded_not_inlined(tmp_path):
    root = tmp_path / "t"
    root.mkdir()
    for i in range(400):
        (root / ("f%04d" % i)).write_bytes(b"\x5a" * 64)
    res = walk(str(root), threads=4)
    if res.size <= res.apparent:
        return  # a filesystem that inlines these; nothing to classify
    assert res.padded_files == 400, res.padded_files
    assert res.inline_files == 0, res.inline_files
    assert res.padding > 0, res.padding


def test_a_small_allocation_still_stays_out_of_the_unit_estimate(tmp_path):
    """The regression `MIN_ALLOC_UNIT` was introduced to fix, still fixed.

    A tree of small files beside larger ones must measure the *larger* unit; the
    sectors handed to the small ones must not set it.
    """
    root = tmp_path / "t"
    root.mkdir()
    for i in range(60):
        (root / ("big%02d" % i)).write_bytes(b"\x5a" * 9000)
    for i in range(60):
        (root / ("tiny%02d" % i)).write_bytes(b"\x5a" * 100)
    res = walk(str(root), threads=4)
    if res.alloc_unit is None:
        return  # no padded file large enough to estimate from on this filesystem
    from rapidu.walk import MIN_ALLOC_UNIT

    assert res.alloc_unit >= MIN_ALLOC_UNIT, res.alloc_unit
    assert res.padded_files >= 60, res.padded_files


def test_the_packing_advice_no_longer_needs_a_measured_unit():
    r = WalkResult("/t")
    r.files, r.dirs = 50000, 1
    r.apparent = 3400000
    r.size = 25600000
    r.padded_files = 50000
    r.padded_apparent = 3200000
    r.padded_alloc = 25600000
    r.alloc_bits = 0  # every allocation was too small to estimate a unit from
    assert r.alloc_unit is None
    text = _flat(report.render_allocation(r, PLAIN))
    assert "of padding" in text, text
    assert "Packing them" in text, text
    assert "allocation unit" not in text, text


def test_the_unit_clause_appears_when_the_unit_is_known():
    r = WalkResult("/t")
    r.files, r.dirs = 3000, 1
    r.apparent = 3000 * 8192
    r.size = 3000 * 16384
    r.padded_files = 3000
    r.padded_apparent = 3000 * 8192
    r.padded_alloc = 3000 * 16384
    r.alloc_bits = 16384
    assert r.alloc_unit == 16384
    text = _flat(report.render_allocation(r, PLAIN))
    assert "against a 16.0 KiB allocation unit" in text, text
    assert "of padding" in text, text


def test_inline_now_means_the_filesystem_allocated_nothing():
    """`blocks == 0` with data present -- the bytes are in the inode."""
    r = WalkResult("/t")
    r.files, r.dirs = 10, 1
    r.apparent = 10 * 900
    r.size = 512
    r.inline_files = 10
    doc = report.to_json(r, SettleCheck(), None, None, None)["walk"]
    assert doc["allocation"]["inline_files"] == 10
    assert doc["allocation"]["padded_files"] == 0


def test_a_symlink_is_not_counted_as_an_inlined_file(tmp_path):
    """A fast symlink is stored in its inode by construction, and says nothing
    about how this tree stores its data."""
    root = tmp_path / "t"
    root.mkdir()
    (root / "target").write_bytes(b"\x5a" * 4096)
    for i in range(5):
        os.symlink("target", str(root / ("link%d" % i)))
    res = walk(str(root), threads=2)
    assert res.symlinks == 5, res.symlinks
    assert res.inline_files == 0, res.inline_files


def test_the_padding_figure_is_the_gap_it_claims_to_be(tmp_path):
    root = tmp_path / "t"
    root.mkdir()
    for i in range(200):
        (root / ("f%03d" % i)).write_bytes(b"\x5a" * 64)
    res = walk(str(root), threads=4)
    if not res.padded_files:
        return
    assert res.padding == res.padded_alloc - res.padded_apparent
    assert res.padded_alloc <= res.size
    assert res.padded_apparent <= res.apparent


# --- the one finding that named a quantity and nothing else -------------------
#
# `RECLAIMABLE` prints paths and commands, `UNLINKED` prints paths and pids, the
# entries table prints paths, and both floor causes now print paths. The cold-data
# sentence said "on a full quota that is the first place to look" and gave the
# reader no way to look -- and `--sort` has no mtime key, so nothing else in the
# tool could find them either.


def _cold_tree(tmp_path, cold=4, fresh=1):
    root = tmp_path / "t"
    (root / "sub").mkdir(parents=True)
    stamp = 1546300800  # 2019-01-01
    for i in range(cold):
        target = root / "sub" / ("old%d.txt" % i)
        target.write_bytes(b"\x5a" * 3000)
        os.utime(str(target), (stamp, stamp))
    for i in range(fresh):
        (root / ("new%d.txt" % i)).write_bytes(b"\x5a" * 2000)
    return str(root)


def test_the_cold_finding_names_a_way_to_list_them(tmp_path):
    res = walk(_cold_tree(tmp_path), threads=2)
    text = _flat(report.render_age(res, PLAIN))
    assert "not been modified in over a year" in text, text
    assert "find '" in text, text
    assert "! -type d" in text, text


def test_the_listing_command_actually_finds_them(tmp_path):
    """The command is only worth printing if running it answers the question."""
    import subprocess

    root = _cold_tree(tmp_path, cold=4, fresh=1)
    res = walk(root, threads=2)
    lines = report.render_age(res, PLAIN)
    command = [ln.strip() for ln in lines if ln.strip().startswith("find '")]
    assert command, lines
    out = subprocess.Popen(
        ["bash", "-c", command[0]], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).communicate()[0]
    found = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
    cold_counted = res.by_age[-1][1]
    assert len(found) == cold_counted, (found, cold_counted)


def test_the_boundary_comes_from_the_bucket_itself():
    """So the command and the bucket it explains cannot drift apart."""
    from rapidu.walk import AGE_BUCKET_DAYS

    r = WalkResult("/t")
    r.files, r.dirs = 6, 1
    r.size = r.apparent = 200 << 10
    r.by_age = [(10 << 10, 5), (0, 0), (0, 0), (0, 0), (190 << 10, 1)]
    text = _flat(report.render_age(r, PLAIN))
    assert "-mtime +%d" % AGE_BUCKET_DAYS[-1] in text, text


def test_the_listing_command_is_quoted_like_every_other(tmp_path):
    """It goes through `_shell_command`, so a hostile root cannot break out."""
    root = tmp_path / "a b  c'd"
    (root / "sub").mkdir(parents=True)
    stamp = 1546300800
    for i in range(3):
        target = root / "sub" / ("old%d.txt" % i)
        target.write_bytes(b"\x5a" * 3000)
        os.utime(str(target), (stamp, stamp))
    res = walk(str(root), threads=2)
    lines = report.render_age(res, PLAIN)
    command = [ln.strip() for ln in lines if ln.strip().startswith("find '")]
    assert command, lines
    import subprocess

    probe = command[0].replace("find ", "printf '%s\\n' ", 1).split(" ! -type")[0]
    out = subprocess.Popen(["bash", "-c", probe], stdout=subprocess.PIPE).communicate()[0]
    words = [w for w in out.decode("utf-8", "replace").split("\n") if w]
    assert words == [str(root)], (words, str(root))


def test_an_unprintable_root_is_refused_with_a_listing_remedy(tmp_path):
    """The cause is shared with RECLAIMABLE; the remedy is not.

    "delete it by inode ... from the list below" is advice about deletion, and
    about a list, and this section has neither.
    """
    root = tmp_path / "ctl\x01dir"
    (root / "sub").mkdir(parents=True)
    stamp = 1546300800
    for i in range(3):
        target = root / "sub" / ("old%d.txt" % i)
        target.write_bytes(b"\x5a" * 3000)
        os.utime(str(target), (stamp, stamp))
    res = walk(str(root), threads=2)
    text = _flat(report.render_age(res, PLAIN))
    assert "unprintable characters" in text, text
    assert "reach it by inode" in text, text
    assert "delete it by inode" not in text, text
    assert "list below" not in text, text


def test_the_reclaim_remedy_is_unchanged(tmp_path):
    """The default wording is byte-identical, so that section did not move."""
    note = report._no_command_note("/data/ctrl\x01dir/cache/torch")
    assert note == (
        "# this path contains unprintable characters -- identify it from the list "
        "below and delete it by inode or with a glob, not by pasting a name"
    ), note


def test_the_two_causes_still_differ_in_both_contexts():
    import sys as _sys

    class Narrow(object):
        encoding = "ascii"

    saved, _sys.stdout = _sys.stdout, Narrow()
    try:
        reclaim = report._no_command_note("/data/café/x")
        listing = report._no_command_note(
            "/data/café/x", "reach it by inode", "re-run under a UTF-8 locale to paste"
        )
    finally:
        _sys.stdout = saved
    assert "delete it by inode" in reclaim, reclaim
    assert "delete it by inode" not in listing, listing
    assert "encoding cannot represent" in reclaim and "encoding cannot represent" in listing


# --- the quota row was unreadable exactly when it mattered --------------------
#
# The row format had `{}{}` between the bar and the percentage -- no separator.
# The gap came from `{:>6}` padding a five-character figure like " 22.2%", so it
# vanished the moment the number filled the field: a row at 105.6% rendered as
#
#     lab  group  files  4,750,000 / 4,500,000  ##########105.6%  /project OVER
#
# with the bar fused to the figure. That is the one state the row exists to shout
# about, and the only one where it could not be read. A four-digit percentage
# overflowed the field as well and shifted every column after it.


def _quota_rows(*specs):
    snap = QuotaSnapshot("test")
    snap.available = True
    snap.taken_at = snap.read_at - 60.0
    snap.rows = [
        QuotaRow(name, kind, "group", used, soft, hard, mount="/project")
        for name, kind, used, soft, hard in specs
    ]
    return snap


def _rendered_rows(snap):
    return [ln for ln in report.render_quota(snap, style=PLAIN) if "/project" in ln]


def test_the_bar_and_the_percentage_never_fuse():
    for used, label in (
        (450_000, "9%"),
        (1_110_000, "24%"),
        (4_750_000, "105%"),
        (46_000_000, "1022%"),
        (0, "0%"),
    ):
        snap = _quota_rows(("lab", "files", used, 4_500_000, 5_000_000))
        rows = _rendered_rows(snap)
        assert rows, label
        # whatever the magnitude, a space separates the bar from the figure
        for row in rows:
            assert "-%" not in row and "#%" not in row, (label, row)
            index = row.index("%")
            digits = row[:index].rstrip("0123456789.")
            assert digits.endswith(" "), (label, row)


def test_an_over_limit_row_is_readable():
    rows = _rendered_rows(_quota_rows(("lab", "files", 4_750_000, 4_500_000, 5_000_000)))
    assert rows
    # At least one space; the exact count depends on the field width, which is
    # not the property under test.
    assert re.search(r"#{10} +105\.6%", rows[0]), rows[0]
    assert "OVER" in rows[0], rows[0]


def test_a_four_digit_percentage_does_not_shift_the_columns():
    snap = _quota_rows(
        ("lab", "blocks", 450_000, 4_500_000, 5_000_000),
        ("lab", "files", 1_110_000, 4_500_000, 5_000_000),
        ("lab", "files", 4_750_000, 4_500_000, 5_000_000),
        ("other", "files", 46_000_000, 4_500_000, 5_000_000),
    )
    rows = _rendered_rows(snap)
    assert len(rows) == 4
    mounts = [row.index("/project") for row in rows]
    assert len(set(mounts)) == 1, (mounts, rows)


def test_a_row_with_no_limit_aligns_with_the_rest():
    snap = _quota_rows(
        ("lab", "files", 1_110_000, 4_500_000, 5_000_000),
        ("z", "files", 100, None, None),
    )
    rows = _rendered_rows(snap)
    assert len(rows) == 2
    assert len({row.index("/project") for row in rows}) == 1, rows
    assert "n/a" in rows[1], rows[1]


def test_the_percentage_is_never_capped():
    """The bar clamps; the number must not, which is why `OVER` exists."""
    rows = _rendered_rows(_quota_rows(("lab", "files", 46_000_000, 4_500_000, 5_000_000)))
    assert "1022.2%" in rows[0], rows[0]
    assert "100.0%" not in rows[0], rows[0]


def test_the_binding_limit_is_the_one_marked():
    """Bytes fine, inodes over: only the inode row carries the mark."""
    snap = _quota_rows(
        ("lab", "blocks", 20 << 40, 90 << 40, 100 << 40),
        ("lab", "files", 4_750_000, 4_500_000, 5_000_000),
    )
    rows = _rendered_rows(snap)
    blocks = [r for r in rows if " blocks " in r][0]
    files = [r for r in rows if " files " in r][0]
    assert "OVER" not in blocks, blocks
    assert "OVER" in files, files


# --- the inode column was a fixed nine ----------------------------------------
#
# Nine holds "9,999,999". A directory with ten million inodes is ordinary on the
# filesystems this tool exists for -- `/project/rcc` on this cluster holds 44
# million -- and at the tenth digit the field overflowed, so the `entry` names
# below it started at different columns and the `inodes` header no longer sat over
# its own column:
#
#         size  share                          inodes  entry
#     40.0 GiB  ███████████▊░░░░░░   65.6%  12,345,678  huge/
#     20.0 GiB  █████▉░░░░░░░░░░░░   32.8%    987,654  big/
#
# Measured off the values it will hold, which is the rule `_entries_rule` already
# states for the hairline.


def _wide_table(counts, width=110):
    from rapidu.walk import Entry

    gib = 1 << 30
    res = WalkResult("/project/lab")
    res.elapsed = 60.0
    total_bytes = total_inodes = 0
    for index, inodes in enumerate(counts):
        entry = Entry("/project/lab/d%d" % index, True)
        entry.size = (40 - index) * gib
        entry.files, entry.dirs = inodes, 0
        res.dir_agg[entry.path] = entry
        res.finished_tops.add("d%d" % index)
        total_bytes += entry.size
        total_inodes += inodes
    res.size = res.apparent = total_bytes
    res.files, res.dirs = total_inodes - 100, 100
    style = ui.Style(color=False, unicode_ok=True, width=width, depth=8)
    return report.render_compact(res, SettleCheck(), 10, False, style)


def test_the_entry_column_aligns_at_every_magnitude():
    for counts in (
        [9_999_999, 42, 7],
        [12_345_678, 987_654, 42],
        [123_456_789, 42, 7],
        [1_234_567_890, 42, 7],
    ):
        lines = _wide_table(counts)
        rows = [ln for ln in lines if "  d" in ln]
        assert len(rows) == len(counts), (counts, rows)
        starts = {ln.index("d%d" % i) for i, ln in enumerate(rows)}
        assert len(starts) == 1, (counts, sorted(starts), rows)


def test_the_header_tracks_the_column_it_labels():
    for counts in ([42, 7], [12_345_678, 42], [1_234_567_890, 42]):
        lines = _wide_table(counts)
        header = [ln for ln in lines if "entry" in ln][0]
        rows = [ln for ln in lines if "  d" in ln]
        assert header.index("entry") == rows[0].index("d0"), (counts, header, rows[0])


def test_a_small_tree_keeps_the_narrow_column():
    """The floor still applies, so ordinary listings are unchanged."""
    from rapidu.report import _INODE_COL, _inode_width

    assert _inode_width([1, 42, 999]) == _INODE_COL
    assert _inode_width([]) == _INODE_COL


def test_the_width_grows_only_as_far_as_the_widest_value():
    from rapidu.report import _inode_width

    assert _inode_width([9_999_999]) == 9
    assert _inode_width([10_000_000]) == 10
    assert _inode_width([123_456_789]) == 11
    assert _inode_width([1_234_567_890]) == 13


def test_the_remainder_row_is_measured_too():
    """It carries a count as well, and it is the widest row on a truncated table."""
    from rapidu.walk import Entry

    gib = 1 << 30
    res = WalkResult("/project/lab")
    res.elapsed = 1.0
    for index in range(30):
        entry = Entry("/project/lab/d%02d" % index, True)
        entry.size = gib
        entry.files = 1_000_000
        entry.dirs = 0
        res.dir_agg[entry.path] = entry
        res.finished_tops.add("d%02d" % index)
    res.size = res.apparent = 30 * gib
    res.files, res.dirs = 30_000_000, 30
    style = ui.Style(color=False, unicode_ok=True, width=110, depth=8)
    lines = report.render_compact(res, SettleCheck(), 3, False, style)
    rows = [ln for ln in lines if "  d" in ln or "more" in ln]
    assert len(rows) >= 4, rows
    # the remainder row's 27,000,000 is wider than any listed row's 1,000,000
    assert any("more" in ln for ln in rows), rows
    trailing = [ln for ln in rows if "  d" in ln]
    starts = {ln.index("d") for ln in trailing}
    assert len(starts) == 1, (sorted(starts), rows)


def test_render_entries_keeps_its_public_shape():
    """The split is internal: this still returns a list of rows."""
    from rapidu.walk import Entry

    res = WalkResult("/t")
    entry = Entry("/t/a", True)
    entry.size, entry.files, entry.dirs = 1 << 20, 5, 0
    res.dir_agg["/t/a"] = entry
    res.finished_tops.add("a")
    res.size = res.apparent = 1 << 20
    res.files, res.dirs = 5, 1
    rows = report.render_entries(res, 10, False, PLAIN)
    assert isinstance(rows, list)
    assert rows and all(isinstance(row, str) for row in rows)


# --- the used/limit columns were fixed at eleven ------------------------------
#
# Eleven holds every byte figure `human_bytes` can produce ("1023.9 PiB") and a
# files quota up to 999,999,999. This cluster's own row is
# "44,812,476 / 230,900,000" -- eleven characters exactly, one order of magnitude
# from the edge. A billion-inode quota overflowed both fields and shifted the bar,
# the percentage, the mount and the OVER marker four columns right on that row.
#
# Measured from the rows now, as the fileset label in the same function already is.


def _quota_snap(*specs):
    snap = QuotaSnapshot("test")
    snap.available = True
    snap.taken_at = snap.read_at - 60.0
    snap.rows = [
        QuotaRow(name, kind, "group", used, soft, hard, mount="/project")
        for name, kind, used, soft, hard in specs
    ]
    return snap


def _quota_lines(snap):
    return [ln for ln in report.render_quota(snap, style=PLAIN) if "/project" in ln]


def test_a_billion_inode_quota_does_not_shift_the_row():
    rows = _quota_lines(
        _quota_snap(
            ("lab", "files", 44_812_476, 230_900_000, 250_000_000),
            ("lab", "files", 1_204_812_476, 2_309_000_000, 2_500_000_000),
            ("lab", "files", 812, 900, 1000),
        )
    )
    assert len(rows) == 3
    assert len({row.index("/project") for row in rows}) == 1, rows


def test_bytes_and_inode_rows_share_one_layout():
    """They sit in the same columns, which is why the field was fixed to begin with."""
    tib = 1 << 40
    rows = _quota_lines(
        _quota_snap(
            ("rcc", "blocks", 64 * tib, 202 * tib, 222 * tib),
            ("rcc", "files", 44_812_476, 230_900_000, 250_000_000),
        )
    )
    assert len(rows) == 2
    assert len({row.index("/project") for row in rows}) == 1, rows


def test_an_ordinary_table_is_unchanged():
    """The floor is still eleven, so the common case looks exactly as it did."""
    tib = 1 << 40
    rows = _quota_lines(
        _quota_snap(
            ("rcc", "blocks", 64 * tib, 202 * tib, 222 * tib),
            ("rcc", "files", 44_812_476, 230_900_000, 250_000_000),
        )
    )
    assert "44,812,476 / 230,900,000" in rows[1], rows[1]
    assert "64.0 TiB / 202.0 TiB" in rows[0], rows[0]


def test_a_missing_limit_still_aligns():
    rows = _quota_lines(
        _quota_snap(
            ("lab", "files", 1_204_812_476, None, None),
            ("lab", "files", 812, 900, 1000),
        )
    )
    assert len(rows) == 2
    assert len({row.index("/project") for row in rows}) == 1, rows
    assert "n/a" in rows[0], rows[0]


def test_the_widest_row_sets_the_width_and_nothing_wider_appears():
    """A single huge row widens the column for all of them, not just itself."""
    rows = _quota_lines(
        _quota_snap(
            ("lab", "files", 812, 900, 1000),
            ("lab", "files", 9_876_543_210, 9_999_999_999, 10_000_000_000),
        )
    )
    assert len(rows) == 2
    narrow, wide = rows
    assert "9,876,543,210 / 9,999,999,999" in wide, wide
    assert len({row.index("/project") for row in rows}) == 1, rows
    # the small row is padded to the same layout rather than left short
    assert "            812 / 900" in narrow, repr(narrow)


# --- a zero limit means "no limit", and the row said otherwise ----------------
#
# Most quota backends spell "no limit" as `0`. `usage_fraction` already knew --
# `soft or hard` makes a zero fall through -- but the table printed `row.soft`
# directly, so the two disagreed:
#
#   soft=0, hard=250,000,000 -> "44,812,476 / 0"  beside  "17.9%"
#
# 17.9% is measured against the hard limit, correctly. A used/limit pair that is
# not the denominator of the percentage next to it is worse than no pair at all.
# And with no limit anywhere the row read "/ 0" -- a limit of zero, the most
# alarming thing that column can say, when the truth was the opposite.


def test_the_printed_limit_is_the_one_the_percentage_used():
    rows = _quota_lines(_quota_snap(("lab", "files", 44_812_476, 0, 250_000_000)))
    assert len(rows) == 1
    assert "44,812,476 / 250,000,000" in rows[0], rows[0]
    assert "17.9%" in rows[0], rows[0]
    assert "/ 0 " not in rows[0], rows[0]


def test_no_limit_reads_as_none_not_zero():
    rows = _quota_lines(_quota_snap(("lab", "files", 44_812_476, 0, 0)))
    assert "44,812,476 / none" in rows[0], rows[0]
    assert "n/a" in rows[0], rows[0]  # no percentage, because there is no limit


def test_an_unreadable_limit_is_still_n_a():
    """ "The backend reported no limit" and "the limit could not be read" are
    different claims, and only one of them is `none`."""
    rows = _quota_lines(_quota_snap(("lab", "files", 44_812_476, None, None)))
    assert "44,812,476 / n/a" in rows[0], rows[0]


def test_a_zero_byte_limit_reads_the_same_way():
    tib = 1 << 40
    rows = _quota_lines(_quota_snap(("lab", "blocks", 64 * tib, 0, 0)))
    assert "64.0 TiB / none" in rows[0], rows[0]
    assert "0 B" not in rows[0], rows[0]


def test_the_limit_property_is_the_single_source():
    assert QuotaRow("a", "files", "group", 5, 0, 100).limit == 100
    assert QuotaRow("a", "files", "group", 5, 90, 100).limit == 90
    assert QuotaRow("a", "files", "group", 5, 0, 0).limit is None
    assert QuotaRow("a", "files", "group", 5, None, None).limit is None
    assert QuotaRow("a", "files", "group", 5, None, 100).limit == 100


def test_the_fraction_and_the_limit_cannot_diverge():
    for soft, hard, expected in ((0, 100, 0.05), (90, 100, 5 / 90.0), (0, 0, None)):
        row = QuotaRow("a", "files", "group", 5, soft, hard)
        if expected is None:
            assert row.usage_fraction is None
            assert row.limit is None
        else:
            assert row.limit is not None
            assert abs(row.usage_fraction - 5 / float(row.limit)) < 1e-12


def test_the_exit_code_uses_the_same_limit():
    """A zero soft limit must not make a full quota look empty, or an empty one full."""
    from rapidu import cli

    snap = _quota_snap(("lab", "files", 249_000_000, 0, 250_000_000))
    assert cli._quota_needs_attention(snap, ["/project"]) is True
    snap = _quota_snap(("lab", "files", 1_000, 0, 250_000_000))
    assert cli._quota_needs_attention(snap, ["/project"]) is False
    snap = _quota_snap(("lab", "files", 10**12, 0, 0))
    assert cli._quota_needs_attention(snap, ["/project"]) is False, "no limit, no alarm"


def test_the_document_publishes_the_derived_limit():
    """So a consumer need not reimplement the soft-or-hard rule and divide by zero."""
    snap = _quota_snap(
        ("a", "files", 44_812_476, 0, 250_000_000),
        ("b", "files", 44_812_476, 0, 0),
        ("c", "files", 44_812_476, 230_900_000, 250_000_000),
    )
    rows = report.to_json(None, None, snap, None, None)["quota"]["rows"]
    assert [row["limit"] for row in rows] == [250_000_000, None, 230_900_000]
    # the raw values stay, because a consumer may want what the backend wrote
    assert [row["soft"] for row in rows] == [0, 0, 230_900_000]


# --- the binding limit, not the soft one --------------------------------------
#
# Soft is normally the lower of the two, so measuring against it is right and this
# changes nothing on a correctly configured quota. Where a site has raised the soft
# limit and left the hard one behind, soft is unreachable and hard binds first --
# and preferring soft reported a fileset sitting at 100% of its enforced limit as
#
#   files   100,000,000 / 250,000,000  ####------   40.0%  /project
#
# with `_quota_needs_attention` returning False. A third state in which the
# cron-friendly invocation says "fine" while writes are about to stop, which is the
# exact failure that docstring was written about.


def test_a_soft_limit_above_the_hard_one_does_not_hide_a_full_quota():
    from rapidu import cli

    snap = _quota_snap(("lab", "files", 100_000_000, 250_000_000, 100_000_000))
    row = snap.rows[0]
    assert row.limit == 100_000_000, row.limit
    assert row.usage_fraction == 1.0
    rows = _quota_lines(snap)
    assert "100,000,000 / 100,000,000" in rows[0], rows[0]
    assert "100.0%" in rows[0], rows[0]
    assert cli._quota_needs_attention(snap, ["/project"]) is True


def test_an_ordinary_quota_still_measures_against_soft():
    """Soft is the earlier alarm, and on any sane configuration it is the lower."""
    snap = _quota_snap(("lab", "files", 100_000_000, 230_900_000, 250_000_000))
    assert snap.rows[0].limit == 230_900_000
    assert "100,000,000 / 230,900,000" in _quota_lines(snap)[0]


def test_every_combination_of_set_and_unset_limits():
    cases = (
        (230_900_000, 250_000_000, 230_900_000),  # both set, sane
        (250_000_000, 100_000_000, 100_000_000),  # both set, mis-set
        (0, 250_000_000, 250_000_000),  # soft disabled
        (230_900_000, 0, 230_900_000),  # hard disabled
        (None, 250_000_000, 250_000_000),  # soft unreadable
        (230_900_000, None, 230_900_000),  # hard unreadable
        (0, 0, None),  # no limit at all
        (None, None, None),  # nothing readable
    )
    for soft, hard, expected in cases:
        row = QuotaRow("a", "files", "group", 5, soft, hard)
        assert row.limit == expected, (soft, hard, row.limit, expected)


def test_the_equal_case_is_unambiguous():
    row = QuotaRow("a", "files", "group", 5, 100, 100)
    assert row.limit == 100


def test_a_negative_reading_raises_no_false_alarm():
    """A broken counter must not trip the exit code in either direction."""
    from rapidu import cli

    snap = _quota_snap(("lab", "files", -5_000, 230_900_000, 250_000_000))
    assert cli._quota_needs_attention(snap, ["/project"]) is False
    rows = _quota_lines(snap)
    assert "-5,000" in rows[0], rows[0]


# --- one predicate for "is a grace timer running" -----------------------------
#
# The grace field is free-form backend text and it drives the only warning in the
# tool that means *writes are about to stop*, so a false positive spends the one
# alarm that matters. `_clean_grace` strips every spelling of "no timer";
# `render_quota` then re-tested the same thing with a shorter list -- no `0`, no
# `n/a` -- making the renderer a weaker second guard for a rule already applied.
#
# Vocabulary checked against the real tools rather than assumed: quota-tools
# writes `none` for no timer and `%ddays` / `%uhours` / `%uminutes` / `%useconds`
# (including `0seconds`) for a running one, `lfs` writes `-`.


def test_every_spelling_of_no_timer_is_silent():
    from rapidu.quota import in_grace

    for raw in ("", "  ", "-", "none", "NONE", "None", "0", "n/a", "N/A"):
        assert in_grace(raw) is False, raw


def test_a_running_timer_is_reported():
    from rapidu.quota import in_grace

    for raw in ("7days", "13hours", "6d23h", "0seconds", "6 days"):
        assert in_grace(raw) is True, raw


def test_the_renderer_agrees_with_the_cleaner():
    """Both spellings the renderer used to miss must now be silent there too."""
    for raw in ("0", "n/a"):
        snap = _quota_snap(("lab", "files", 240_000_000, 230_900_000, 250_000_000))
        snap.rows[0].grace = raw
        line = _quota_lines(snap)[0]
        assert "IN GRACE" not in line, (raw, line)


def test_a_real_timer_still_raises_the_alarm():
    snap = _quota_snap(("lab", "files", 240_000_000, 230_900_000, 250_000_000))
    snap.rows[0].grace = "7days"
    line = _quota_lines(snap)[0]
    assert "IN GRACE, 7days left" in line, line


def test_a_grace_timer_sets_the_exit_code_whatever_the_usage():
    """It is the state that means writes stop, so usage is beside the point."""
    from rapidu import cli

    snap = _quota_snap(("lab", "files", 1_000, 230_900_000, 250_000_000))
    assert cli._quota_needs_attention(snap, ["/project"]) is False
    snap.rows[0].grace = "7days"
    assert cli._quota_needs_attention(snap, ["/project"]) is True


def test_no_second_copy_of_the_grace_vocabulary():
    """The list lives in `quota` and nowhere else."""
    import glob

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for path in sorted(glob.glob(os.path.join(root, "src", "rapidu", "*.py"))):
        if os.path.basename(path) == "quota.py":
            continue
        with open(path, "rb") as handle:
            source = handle.read().decode("utf-8")
        for marker in ('"none", "-"', '"-", "none"', "'none', '-'"):
            if marker in source:
                offenders.append((os.path.basename(path), marker))
    assert not offenders, offenders


# --- when no quota backend works, the mount is often still answering ----------
#
# Measured on a Booth login node: NFS home on Isilon, `quota -s` silent, no
# `mmlsquota`, no `lfs`. Every backend failed, the report stopped at "n/a", and
# `statvfs` on that home reported 14.0 GiB total with 6.7 GiB used -- where the
# 14 GiB *is* the enforced quota, because Isilon presents per-user quotas through
# those fields.
#
# It is not labelled a quota, because sometimes it is not one. On one midway3
# login node `statvfs` on `/project` reports 58.6 TiB of 202 TiB and 231,900,000
# inodes -- the GPFS fileset quota almost exactly -- while `statvfs` on `/home`
# reports 6.4 PiB against a real 30 GiB home quota. Same syscall, same host, quota
# in one case and capacity in the other.


def test_the_mount_report_reads_statvfs():
    from rapidu.quota import mount_report

    report_ = mount_report("/tmp")
    assert report_ is not None
    assert report_.total > 0
    assert 0 <= report_.used <= report_.total
    assert report_.avail >= 0
    assert 0.0 <= report_.fraction <= 1.0


def test_a_path_that_cannot_be_statted_reports_nothing():
    from rapidu.quota import mount_report

    assert mount_report("/nonexistent-path-for-a-test") is None


def test_inode_counts_are_none_when_the_filesystem_does_not_report_them():
    """`f_files == 0` means unsupported, not a limit of zero -- Constraint 10."""
    from rapidu.quota import MountReport

    r = MountReport("/x", "/x", 1 << 30, 1 << 20, 1 << 29, None, None)
    assert r.inodes_total is None
    assert r.fraction is not None


def test_a_zero_total_yields_no_fraction():
    from rapidu.quota import MountReport

    assert MountReport("/x", "/x", 0, 0, 0, None, None).fraction is None


def _unavailable(reason="no quota backend available"):
    snap = QuotaSnapshot("test")
    snap.available = False
    snap.reason = reason
    return snap


def test_the_fallback_appears_only_when_the_backend_failed():
    text = _flat(report.render_quota(_unavailable(), ["/tmp"], style=PLAIN))
    assert "the mount at" in text, text

    ok = QuotaSnapshot("test")
    ok.available = True
    ok.taken_at = ok.read_at - 60.0
    ok.rows = [QuotaRow("lab", "files", "group", 5, 90, 100, mount="/tmp")]
    assert "the mount at" not in _flat(report.render_quota(ok, ["/tmp"], style=PLAIN))


def test_the_fallback_never_calls_itself_a_quota():
    text = _flat(report.render_quota(_unavailable(), ["/tmp"], style=PLAIN))
    assert "statvfs, not a quota backend" in text, text
    assert "Nothing here distinguishes the two" in text, text


def test_the_fallback_needs_a_path():
    """With nothing walked there is nothing to stat, and it stays silent."""
    assert "the mount at" not in _flat(report.render_quota(_unavailable(), None, style=PLAIN))
    assert "the mount at" not in _flat(report.render_quota(_unavailable(), [], style=PLAIN))


def test_one_line_per_filesystem_not_per_path():
    """Two paths on one mount are one report, not two identical ones."""
    text = _flat(report.render_quota(_unavailable(), ["/tmp", "/tmp"], style=PLAIN))
    assert text.count("the mount at") == 1, text


def test_the_original_reason_is_still_shown():
    """The fallback adds to the failure message; it must not replace it."""
    text = _flat(report.render_quota(_unavailable("`lfs` is not on PATH"), ["/tmp"], style=PLAIN))
    assert "`lfs` is not on PATH" in text, text
    assert "n/a -" in text, text


def test_the_document_publishes_the_same_fallback(tmp_path):
    (tmp_path / "f").write_bytes(b"\x5a" * 4096)
    res = walk(str(tmp_path), threads=2)
    doc = report.to_json(res, SettleCheck(), _unavailable(), None, None)
    mount = doc["quota"]["mount"]
    assert mount is not None
    assert mount["total_bytes"] > 0
    assert mount["used_bytes"] >= 0
    assert 0.0 <= mount["fraction"] <= 1.0
    # the document must not let a consumer read this as a limit somebody set
    assert mount["is_a_quota"] is None
    assert doc["quota"]["rows"] == []


def test_the_document_omits_it_when_a_backend_worked(tmp_path):
    (tmp_path / "f").write_bytes(b"\x5a" * 4096)
    res = walk(str(tmp_path), threads=2)
    ok = QuotaSnapshot("test")
    ok.available = True
    ok.taken_at = ok.read_at - 60.0
    ok.rows = [QuotaRow("lab", "files", "group", 5, 90, 100, mount="/tmp")]
    assert report.to_json(res, SettleCheck(), ok, None, None)["quota"]["mount"] is None


def test_available_headroom_not_free_blocks():
    """`f_bavail` is what binds under a quota; `f_bfree` can be larger."""
    import os as _os

    from rapidu.quota import mount_report

    st = _os.statvfs("/tmp")
    r = mount_report("/tmp")
    frsize = st.f_frsize or st.f_bsize
    assert r.avail == st.f_bavail * frsize
    assert r.used == max(0, st.f_blocks * frsize - st.f_bfree * frsize)


# --- the fallback reached the terminal but not the document -------------------
#
# `rdu -Q` has no walk: it calls `to_json` with `res=None`. `_mount_json` keyed off
# `res.root`, so on a host with no working backend the terminal printed the mount
# figures and the document omitted them -- the same text-versus-document split
# this session has closed several times, reintroduced by me one round earlier.


def test_quota_only_json_carries_the_mount_fallback():
    doc = report.to_json(None, None, _unavailable(), None, None, path="/tmp")
    mount = doc["quota"]["mount"]
    assert mount is not None
    assert mount["path"] == "/tmp"
    assert mount["total_bytes"] > 0


def test_the_terminal_and_the_document_agree_with_no_walk():
    text = _flat(report.render_quota(_unavailable(), ["/tmp"], style=PLAIN))
    doc = report.to_json(None, None, _unavailable(), None, None, path="/tmp")
    assert ("the mount at" in text) == (doc["quota"]["mount"] is not None)


def test_the_walk_root_is_still_used_when_no_path_is_passed(tmp_path):
    """The full report has a walk, and must keep working without the new argument."""
    (tmp_path / "f").write_bytes(b"\x5a" * 4096)
    res = walk(str(tmp_path), threads=2)
    doc = report.to_json(res, SettleCheck(), _unavailable(), None, None)
    assert doc["quota"]["mount"] is not None
    assert doc["quota"]["mount"]["path"] == str(tmp_path)


def test_an_explicit_path_wins_over_the_walk_root(tmp_path):
    (tmp_path / "f").write_bytes(b"\x5a" * 4096)
    res = walk(str(tmp_path), threads=2)
    doc = report.to_json(res, SettleCheck(), _unavailable(), None, None, path="/tmp")
    assert doc["quota"]["mount"]["path"] == "/tmp"


def test_neither_a_walk_nor_a_path_yields_nothing():
    doc = report.to_json(None, None, _unavailable(), None, None)
    assert doc["quota"]["mount"] is None


def test_a_working_backend_still_suppresses_it_in_both_modes():
    ok = QuotaSnapshot("test")
    ok.available = True
    ok.taken_at = ok.read_at - 60.0
    ok.rows = [QuotaRow("lab", "files", "group", 5, 90, 100, mount="/tmp")]
    assert report.to_json(None, None, ok, None, None, path="/tmp")["quota"]["mount"] is None
    assert "the mount at" not in _flat(report.render_quota(ok, ["/tmp"], style=PLAIN))
