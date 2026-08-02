"""Report rendering, and the settling block the full report is for.

``render_settle`` says the thing ``du`` structurally cannot: that the number you
are looking at is still moving. It is reachable only from the full report, and
it was unreachable dead code once -- these tests keep it wired in.
"""

import os

from slurmdisk import report, ui
from slurmdisk.deleted import DeletedScan
from slurmdisk.walk import SettleCheck, WalkResult

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
    """`sd .` was asked a size question, and drift changes what the size means."""
    res = make_walk(recent=6000)
    out = "\n".join(
        report.render_compact(res, make_settle(drift=80 << 20, gap=75.0), 10, False, PLAIN)
    )
    assert "still settling" in out
    assert "SETTLING" not in out


def test_a_clean_deleted_scan_is_one_fact_on_the_walk_line():
    scan = DeletedScan()
    scan.scanned_pids = 27
    scan.unreadable_pids = 1416
    out = "\n".join(report.render_walk(make_walk(), make_settle(), scan=scan, style=PLAIN))
    assert "no unlinked-but-open space (27 of 1443 pids inspectable)" in out
