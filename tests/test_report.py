"""Report rendering, and the settling block the full report is for.

``render_settle`` says the thing ``du`` structurally cannot: that the number you
are looking at is still moving. It is reachable only from the full report, and
it was unreachable dead code once -- these tests keep it wired in.
"""

import os
import re

from rapidu import report, ui
from rapidu.deleted import DeletedScan
from rapidu.walk import Entry, SettleCheck, WalkResult

PLAIN = ui.resolve_style("never")


def make_walk(root="/tmp/tree", recent=0, recent_apparent=0, size=1 << 30, inodes=5000):
    r = WalkResult(root)
    r.size = size
    r.apparent = size
    r.files = inodes - 1
    r.dirs = 1
    r.elapsed = 1.0
    r.threads = 8
    r.recent_files = recent
    r.recent_apparent = recent_apparent
    r.by_uid = {os.getuid(): (size, inodes)}
    r.by_dev = {42: (size, inodes)}
    return r


def make_settle(drift=0, gap=0.0, ran=True, checked=0, sampled_of=0):
    c = SettleCheck()
    c.ran = ran
    c.gap = gap
    c.drift = drift
    c.checked = checked
    c.sampled_of = sampled_of
    return c


def test_nothing_recent_says_nothing():
    assert report.render_settle(make_walk(recent=0), make_settle()) == []


def test_a_few_recent_files_get_one_line_not_a_section():
    """23 fresh files in a 5,000-inode tree cannot move the headline number.

    Spending five lines saying so trains the reader to skip the section on the
    run where it matters.
    """
    out = report.render_settle(make_walk(recent=23), make_settle(), PLAIN)
    assert len(out) == 1
    assert "provisional" in out[0]
    assert "SETTLING" not in out[0]


def test_measured_drift_gets_the_full_section():
    out = "\n".join(
        report.render_settle(make_walk(recent=6000), make_settle(drift=80 << 20, gap=75.0), PLAIN)
    )
    assert "SETTLING" in out
    assert "6,000 files were written" in out
    assert "re-stat 75s later found 80.0 MiB MORE allocated" in out
    assert "still moving" in out


def test_drift_downward_is_reported_as_less():
    """GPFS moves in both directions; a checker that only looks for growth lies."""
    out = "\n".join(
        report.render_settle(
            make_walk(recent=6000), make_settle(drift=-(80 << 20), gap=75.0), PLAIN
        )
    )
    assert "80.0 MiB LESS allocated" in out


def test_an_immediate_recheck_reports_provisional_not_settled():
    """A null result from a blind instrument is not evidence."""
    out = "\n".join(report.render_settle(make_walk(recent=6000), make_settle(gap=0.0), PLAIN))
    assert "PROVISIONAL" in out
    assert "settled" not in out.replace("SETTLING", "")


def test_a_long_enough_recheck_may_say_settled():
    out = "\n".join(
        report.render_settle(make_walk(recent=6000), make_settle(gap=60.0, checked=6000), PLAIN)
    )
    assert "looks settled" in out


def test_a_truncated_sample_says_how_much_it_covered():
    out = "\n".join(
        report.render_settle(
            make_walk(recent=6000),
            make_settle(drift=1 << 20, gap=60.0, checked=4096, sampled_of=6000),
            PLAIN,
        )
    )
    assert "covered 4,096 of 6,000" in out


def test_the_full_report_states_the_drift_exactly_once():
    """`render_walk` used to warn about drift and then render SETTLING too."""
    res = make_walk(recent=6000)
    settle = make_settle(drift=80 << 20, gap=75.0)
    out = "\n".join(report.render_walk(res, settle, style=PLAIN))
    assert "SETTLING" in out
    assert out.count("80.0 MiB") == 1


def test_the_compact_view_keeps_the_one_line_warning():
    """`rdu .` was asked a size question, and drift changes what the size means."""
    res = make_walk(recent=6000)
    out = "\n".join(
        report.render_compact(res, make_settle(drift=80 << 20, gap=75.0), 10, False, PLAIN)
    )
    assert "still settling" in out
    assert "SETTLING" not in out


# --- the ranked table ------------------------------------------------------


def listing(shares, root="/tmp/tree", total=1 << 40):
    """A walk whose children hold the given shares of the tree."""
    r = make_walk(root=root, size=total, inodes=100 * len(shares))
    for i, share in enumerate(shares):
        e = Entry(os.path.join(root, "child{}".format(i)), True)
        e.add(int(total * share), 99, 1)
        r.dir_agg[e.path] = e
    r.dir_agg[root] = Entry(root, True)
    r.files = sum(e.files for e in r.dir_agg.values())
    r.dirs = sum(e.dirs for e in r.dir_agg.values())
    return r


def bar_cells(line, style=PLAIN):
    return sum(line.count(ch) for ch in (style.bar_chars[0],) + ui._BAR_PARTIALS[1:])


def test_the_bar_measures_share_of_the_tree_not_the_row_above_it():
    """A 32% directory drawn as a full bar, beside the text "32%", is a lie.

    Scaling to the largest listed row also made the top bar full on *every*
    listing ever printed, so it carried no information at all.
    """
    rows = report.render_entries(listing([0.32, 0.28, 0.16]), 10, False, PLAIN)
    assert "31.9%" in rows[0] or "32.0%" in rows[0]
    assert bar_cells(rows[0]) <= report._BAR_W // 2, rows[0]
    # And the ordering the bar shows still matches the ordering the table has.
    assert bar_cells(rows[0]) > bar_cells(rows[1]) > bar_cells(rows[2])


def test_one_directory_holding_the_whole_tree_does_fill_the_bar():
    rows = report.render_entries(listing([0.99]), 10, False, PLAIN)
    assert bar_cells(rows[0]) == report._BAR_W


def test_every_bar_is_drawn_as_a_full_width_box():
    """The bar column is a box of fixed width, filled to the share.

    It used to trail off into blank space, which reserved eighteen columns on
    every row and drew nothing in most of them: a 4.0% bar and a 14.1% bar had
    no common edge to be measured against, and the table read as though it had a
    hole in it. Fill plus track must come to exactly the column width on every
    row, including the hatched remainder.
    """
    fill, empty = PLAIN.bar_chars
    rows = report.render_entries(listing([0.32, 0.28, 0.04]), 2, False, PLAIN)
    assert len(rows) == 3, "two ranked rows and the remainder"
    for row in rows:
        cells = bar_cells(row) + row.count(empty) + row.count(ui._BAR_HATCH)
        assert cells == report._BAR_W, (cells, row)
    # The track is drawn, and it is a different glyph from the fill.
    assert empty in rows[0] and fill != empty


def test_a_full_bar_has_no_track_left_to_draw():
    rows = report.render_entries(listing([0.999]), 10, False, PLAIN)
    assert bar_cells(rows[0]) == report._BAR_W
    assert PLAIN.bar_chars[1] not in rows[0]


def test_the_last_column_is_headed_path_because_files_are_listed_too():
    head = report._entries_header(PLAIN)
    assert head.endswith("path")
    assert "name" not in head and "directory" not in head


def test_an_interrupted_walk_says_the_bar_is_relative_instead():
    """With no total there is no share, so the bar cannot claim to be one."""
    res = listing([0.32, 0.28])
    res.partial = True
    res.finished_tops = {os.path.basename(e.path) for e in res.dir_agg.values()}
    assert "of largest" in report._entries_header(PLAIN, bar_label="of largest")
    rows = report.render_entries(res, 10, False, PLAIN)
    assert rows and "%" not in rows[0], rows[0]


def test_every_row_of_a_skewed_listing_gets_its_own_colour():
    """End to end: the colour a reader actually sees, not just the ramp."""
    style = ui.resolve_style("always")
    style.depth = 256
    shares = [0.319, 0.286, 0.166, 0.086, 0.051, 0.032, 0.028, 0.014, 0.006, 0.005]
    rows = report.render_entries(listing(shares), 10, False, style)
    tones = [re.findall(r"\033\[38;5;(\d+)m", row)[0] for row in rows]
    assert len(set(tones)) == len(shares), tones


def test_a_clean_deleted_scan_is_one_fact_on_the_walk_line():
    scan = DeletedScan()
    scan.scanned_pids = 27
    scan.unreadable_pids = 1416
    out = "\n".join(report.render_walk(make_walk(), make_settle(), scan=scan, style=PLAIN))
    assert "no unlinked-but-open space visible (27 of 1443 pids inspectable" in out


def test_an_empty_deleted_scan_does_not_read_as_an_all_clear():
    """27 of 1443 pids is 1.9% coverage, and none of them are on a compute node.

    The motivating case for the whole section -- a job holding a deleted
    checkpoint -- is invisible here by construction, so a bare "none found"
    answers a question the scan did not ask.
    """
    scan = DeletedScan()
    scan.scanned_pids = 27
    scan.unreadable_pids = 1416
    out = "\n".join(report.render_deleted(scan, style=PLAIN))
    assert "not an all-clear" in out
    assert "compute node" in out
    assert "27 of 1443" in out
