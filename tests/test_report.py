"""Report rendering, and the settling block the full report is for.

``render_settle`` says the thing ``du`` structurally cannot: that the number you
are looking at is still moving. It is reachable only from the full report, and
it was unreachable dead code once -- these tests keep it wired in.
"""

import os
import re

from rapidu import report, ui
from rapidu.deleted import DeletedScan
from rapidu.quota import QuotaRow, QuotaSnapshot
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
    # One *statement*, which now wraps to the width rather than running past it.
    assert "provisional" in " ".join(out)
    assert "SETTLING" not in " ".join(out)
    assert len(out) <= 2


def test_measured_drift_gets_the_full_section():
    out = "\n".join(
        report.render_settle(make_walk(recent=6000), make_settle(drift=80 << 20, gap=75.0), PLAIN)
    )
    assert "SETTLING" in out
    # A noun phrase, not a verb: the same line has to be able to say "and N
    # inodes changed without being written", which no single verb covers.
    assert "6,000 files written" in out
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


def test_the_last_column_is_headed_entry_because_it_holds_both():
    """`directory` would be a lie and `path` was wrong in the other direction.

    Plain files are ranked in that column alongside directories, so `directory`
    excludes half of it. But what is printed is a *name* relative to the walk
    root, not a path, so `path` promised something it did not give -- and
    `msg3_plain.db` sitting under a column headed `path` invites exactly that
    misreading. A directory entry is precisely the category that contains both,
    and it is already the word the facts line uses for their count.
    """
    head = report._entries_header(PLAIN)
    assert head.endswith("entry")
    assert "directory" not in head
    assert not head.endswith("path")


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


# --- the listing's arithmetic has to close ---------------------------------

_BAR_GLYPHS = "".join(
    set(PLAIN.bar_chars + PLAIN.partials + (ui._BAR_HATCH, ui._BAR_HATCH_ASCII)) - {""}
)
_BYTE_UNITS = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40}


def row_figures(row):
    """The bytes and inodes one *rendered* row shows, or None if it shows none.

    Parsed back out of the text rather than read off the entries, because the
    claim under test is about what the reader is told. Every figure in the
    fixtures below is a whole number of GiB, so the round trip through
    ``human_bytes`` is lossless and the sums can be compared exactly.
    """
    left, _, right = re.sub("[" + re.escape(_BAR_GLYPHS) + "]+", "\x00", row).partition("\x00")
    cells = [c for c in re.split(r"\s{2,}", right.strip()) if c]
    counts = [c for c in cells if re.match(r"^[\d,]+$", c)]
    if not counts:
        # A bare "N more" line: a count, no columns, nothing summed.
        return None
    inodes = int(counts[0].replace(",", ""))
    figure = left.strip()
    if not figure:
        return None, inodes  # `-c` prints no byte column at all
    number, unit = figure.split()
    return int(round(float(number) * _BYTE_UNITS[unit])), inodes


def sparse_listing(root="/tmp/tree"):
    """A tree whose smaller children hold inodes and no *allocated* bytes.

    Not a contrived shape. On XFS a small directory is stored inside its own
    inode, so ``st_blocks`` is 0 -- a subtree of directories costs inodes and no
    space at all, and ``/tmp`` on this host is exactly such a filesystem: every
    fixture directory this suite builds reports zero allocated bytes.

    ``d0``, ``d1`` and ``d2`` are deliberately identical on both ranking keys, so
    the listings below also cover a tie: which of them lands above the summary row
    may vary, but what the summary has to account for must not.
    """
    r = make_walk(root=root)
    kids = [("big", 4 << 30, 3), ("mid", 2 << 30, 3), ("d0", 0, 4), ("d1", 0, 4), ("d2", 0, 4)]
    for name, nbytes, inodes in kids:
        e = Entry(os.path.join(root, name), True)
        e.add(nbytes, inodes - 1, 1)
        r.dir_agg[e.path] = e
    r.size = sum(e.size for e in r.dir_agg.values())
    r.apparent = r.size
    r.files = sum(e.files for e in r.dir_agg.values())
    # The root's own inode is not a child of anything and so has no row. It is
    # the one thing a complete listing legitimately leaves out.
    r.dirs = sum(e.dirs for e in r.dir_agg.values()) + 1
    r.by_uid = {os.getuid(): (r.size, r.inodes)}
    r.by_dev = {42: (r.size, r.inodes)}
    return r


def test_the_shown_rows_and_the_summary_complete_the_total():
    """Rows + "(N more -- use -n 0 for all)" == the tree, in both columns.

    The summary row was gated on ``rest_size > 0`` -- bytes only -- and that is
    the wrong column twice over. A root whose hidden children are directories has
    ``rest_size == 0`` on any filesystem that stores a small directory in its
    inode, so ``-n 1`` printed one row of 3 inodes, then a bare "4 more", and the
    other 12 inodes were stated nowhere in the report. Under ``-c`` every size is
    zero, so the one mode whose only measurement is inodes never printed a
    summary at all.
    """
    res = sparse_listing()
    for top in (0, 1, 2, 3, 99):
        for by_inodes in (False, True):
            rows = report.render_entries(res, top, by_inodes, PLAIN)
            figures = [row_figures(row) for row in rows]
            assert None not in figures, (top, by_inodes, rows)
            shown_bytes = sum(f[0] for f in figures)
            shown_inodes = sum(f[1] for f in figures)
            if "more" in rows[-1]:
                assert shown_bytes == res.size, (top, by_inodes, rows)
                assert shown_inodes == res.inodes, (top, by_inodes, rows)
            else:
                # Nothing hidden, so the only thing off the table is the root's
                # own inode -- and every byte is still accounted for.
                assert shown_bytes == res.size, (top, by_inodes, rows)
                assert shown_inodes == res.inodes - 1, (top, by_inodes, rows)

    counted = sparse_listing()
    counted.count_only = True
    counted.size = 0
    for entry in counted.dir_agg.values():
        entry.size = 0
    rows = report.render_entries(counted, 2, True, PLAIN)
    figures = [row_figures(row) for row in rows]
    assert None not in figures, rows
    assert [f[0] for f in figures] == [None] * len(rows), rows
    assert "more" in rows[-1], rows
    assert sum(f[1] for f in figures) == counted.inodes, rows


def test_the_summary_row_is_still_withheld_where_it_would_double_count():
    """The control on the test above: the fix widened *which figures* earn the
    summary row, not *when* a row may carry figures at all.

    Nested rows overlap, so bytes or inodes attached to them would double-count,
    and an interrupted walk does not know what it failed to scan. Both still get
    the bare count, or nothing -- a total is withheld because it would be wrong,
    not because one of its columns happens to be zero.
    """
    nested = sparse_listing()
    inner = Entry(os.path.join("/tmp/tree", "big", "inner"), True)
    inner.add(1 << 30, 2, 1)
    nested.dir_agg[inner.path] = inner
    assert not report._entries_partition_tree(nested)
    tail = report.render_entries(nested, 2, False, PLAIN)[-1]
    assert "more" in tail and "use -n 0 for all" in tail
    assert row_figures(tail) is None, tail

    stopped = sparse_listing()
    stopped.partial = True
    stopped.finished_tops = {os.path.basename(e.path) for e in stopped.dir_agg.values()}
    assert "more" not in "\n".join(report.render_entries(stopped, 1, False, PLAIN))


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


# ---------------------------------------------------------------------------
# The quota table's label column, measured and filled in COLUMNS
# ---------------------------------------------------------------------------

# Nine CJK characters: `len` says 9, a terminal spends 18 cells drawing them.
# Both numbers are under and over the column's 16-cell floor respectively, so a
# character-based width both under-sizes the column and over-fills it.
_WIDE = "\u4e2d\u6587\u9879\u76ee\u6587\u4ef6\u96c6\u5408\u533a"


def quota_snapshot(*filesets):
    """One `blocks` row per fileset, all on one device, all half full."""
    snap = QuotaSnapshot("mmlsquota")
    snap.rows = [
        QuotaRow(name, "blocks", "user", 100, 200, 300, "", "/mnt/" + str(i), False, "dev")
        for i, name in enumerate(filesets)
    ]
    snap.available = True
    snap.taken_at = snap.read_at
    return snap


def quota_label_cells(snap):
    """The rendered label field of every row, scope column onward stripped."""
    cells = []
    for line in report.render_quota(snap, style=PLAIN):
        if " blocks " in line:
            cells.append(line.split("user")[0])
    return cells


def test_the_quota_label_column_is_sized_and_filled_in_columns():
    """A fileset named in Chinese took the label column apart two ways at once.

    `render_quota` sized the field with `len` and filled it with `{:<{w}}`, both
    character counts, in a module whose `ui` measures and cuts in columns. For a
    nine-glyph name that is 9 against the 18 cells it actually occupies, so the
    column was sized at the 16-cell floor -- narrower than the label needed --
    and `ui.truncate`, which *does* count columns, then cut a name that fits.
    The row lost data and still pushed the scope, kind, used/limit, bar,
    percentage and mount columns six cells right of every other row.

    Fileset names come from whatever `mmlsquota` printed, so this is a name the
    tool is handed, not one it composes.
    """
    cells = quota_label_cells(quota_snapshot("home", _WIDE))
    assert len(cells) == 2, cells
    # Nothing was cut: the column is as wide as its widest member needs.
    assert _WIDE in cells[1], cells[1]
    assert "..." not in cells[1], cells[1]
    # And the next column starts at one cell on both rows -- which is the only
    # sense in which a table is aligned.
    assert ui.visible_width(cells[0]) == ui.visible_width(cells[1]), cells
    # Not by accident of equal character counts: these two differ there.
    assert len(cells[0]) != len(cells[1]), cells


def test_an_ascii_quota_label_is_padded_exactly_as_it_always_was():
    """The control: counting columns must not re-pad a table of ASCII names.

    `ui.visible_width` and `len` agree on every character a POSIX fileset name
    normally contains, so every existing panel has to come out unchanged --
    including the 16-cell floor for short names and the measured width for a
    name that passes it. A fix that widened or shifted these would satisfy the
    test above and quietly move every column in every report anyone has.
    """
    # Short names: the floor, filled to 16 by the old character-based expression.
    cells = quota_label_cells(quota_snapshot("home", "scratch"))
    assert cells == [
        "  " + "{:<{w}}".format("dev:home", w=16) + "  ",
        "  " + "{:<{w}}".format("dev:scratch", w=16) + "  ",
    ], cells
    # A name past the floor: measured, still character-for-character the same.
    longer = "project-with-a-long-name"
    cells = quota_label_cells(quota_snapshot("home", longer))
    width = max(len("dev:home"), len("dev:" + longer))
    assert cells == [
        "  " + "{:<{w}}".format("dev:home", w=width) + "  ",
        "  " + "{:<{w}}".format("dev:" + longer, w=width) + "  ",
    ], cells


# ---------------------------------------------------------------------------
# The owners / groups block's name column, measured and filled in COLUMNS
# ---------------------------------------------------------------------------

# The byte figure of an owners row, wherever it has ended up. The inode count
# beside it cannot match: `human_count` prints no unit.
_ROW_BYTES = re.compile(r"[\d.]+ (?:[KMGTP]i)?B")


def owner_rows(uids, gids, monkeypatch):
    """The rendered `by-uid` and `by-gid` rows of a walk, names substituted.

    `uids` and `gids` map a name to its `(size, inodes)` cell; the uid and gid
    numbers are an implementation detail of the lookup being stubbed. What `pwd`
    and `grp` return is the site's business and not this tool's -- see the two
    real names quoted in the test below.
    """
    r = make_walk()
    r.by_uid = {}
    r.by_gid = {}
    unames = {}
    gnames = {}
    for i, (name, cell) in enumerate(uids.items()):
        r.by_uid[1000 + i] = cell
        unames[1000 + i] = name
    for i, (name, cell) in enumerate(gids.items()):
        r.by_gid[2000 + i] = cell
        gnames[2000 + i] = name
    monkeypatch.setattr(report, "_uname", lambda uid: unames.get(uid, str(uid)))
    monkeypatch.setattr(report, "_gname", lambda gid: gnames.get(gid, str(gid)))
    lines = report.render_walk(r, make_settle(), style=PLAIN)
    # Six spaces of indent and a trailing inode count is the row shape; the facts
    # line above also says "inodes" and starts at two.
    return [ln for ln in lines if re.match(r"^ {6}\S.* [\d,]+ inodes?$", ln)]


def test_the_owner_name_column_is_sized_and_filled_in_columns(monkeypatch):
    """A name longer than sixteen characters moved its own row's columns.

    The field was a hard-coded `{:<16}` with no measurement behind it at all, for
    a string `pwd` and `grp` hand over from whatever the site's directory holds.
    Both long names below are real accounts and groups on the cluster this was
    written for: `gnome-initial-setup` is nineteen characters and
    `caprioli-cattaneo-software` is twenty-six, so each shifted its own row's
    bytes, inode count and noun three and ten cells right of the column every
    other row keeps them in -- in plain ASCII, before any question of glyph
    width. `_WIDE` is the same field failing at the other end: nine characters
    and eighteen cells, so `{:<16}` filled it to sixteen characters and
    twenty-two columns.

    The width is shared by both blocks, which print under two captions with no
    break between them and are read against each other.
    """
    rows = owner_rows(
        {"youzhi": (3 << 30, 500), "gnome-initial-setup": (2 << 30, 400), _WIDE: (1 << 30, 300)},
        {"rcc": (3 << 30, 500), "caprioli-cattaneo-software": (2 << 30, 400)},
        monkeypatch,
    )
    assert len(rows) == 5, rows
    # Nothing was cut -- the column is as wide as its widest member needs.
    for name in ("gnome-initial-setup", "caprioli-cattaneo-software", _WIDE):
        assert any(name in row for row in rows), (name, rows)
    # And the byte column ends on the same cell on every row, which is the only
    # sense in which a table is aligned.
    ends = {ui.visible_width(row[: _ROW_BYTES.search(row).end()]) for row in rows}
    assert len(ends) == 1, rows
    # Not by accident of equal character counts: the wide-glyph row differs there.
    assert len({len(row[: _ROW_BYTES.search(row).end()]) for row in rows}) > 1, rows


def test_a_short_ascii_owner_name_is_padded_exactly_as_it_always_was(monkeypatch):
    """The control: measuring the column must not re-pad the panel everyone has.

    `len` and `ui.visible_width` agree on every character a POSIX account or group
    name normally contains, so a block of short ASCII names has to come out
    character-for-character what the hard-coded field produced -- sixteen is now
    the floor rather than the whole rule, and it still has to be the answer here.
    A fix that widened these rows would satisfy the test above and quietly move
    every column of every owners block already in a support ticket.
    """
    rows = owner_rows(
        {"youzhi": (3 << 30, 500), "root": (2 << 30, 400)},
        {"rcc": (3 << 30, 500), "kicp": (2 << 30, 400)},
        monkeypatch,
    )
    # Written out as the old expression, not as literals: the field width, the
    # two figure columns and the single space before the noun are all pinned.
    assert rows == [
        "      {:<16}{:>12}  {:>12} inodes".format(name, size, count)
        for name, size, count in [
            ("youzhi", "3.0 GiB", "500"),
            ("root", "2.0 GiB", "400"),
            ("rcc", "3.0 GiB", "500"),
            ("kicp", "2.0 GiB", "400"),
        ]
    ], rows


class TestCountOnlyDoesNotPublishAZeroItNeverMeasured:
    """`hardlinked_inodes` and `hardlink_extra_refs` were emitted raw under `-c`.

    Both need `st_nlink`/`st_ino`, which `-c` skips by design, so the dedup pass
    never runs and both stay at their initial 0. Published unwrapped, the document
    said "0 hard-linked files, 0 extra names" about a walk that never looked --
    on the same tree where the full walk reports 1 and 2.

    `_unmeasured`'s own docstring is the standard being applied here: *"Zero and
    unmeasured are not the same claim... the sites disagreeing is how the document
    came to null `top_by_size` correctly while leaving `bytes: 0` on every row of
    `top_by_inodes`."* Every sibling stat-derived count in this document
    (`inline_files`, `recent_files`, `touched_files`, `padded_files`) already goes
    through it. These two were the remaining pair.

    The user-facing help is not at fault and is not changed: `-c` says "hard links
    count once per name" and `-i` spells out the same trade in full. The terminal
    is not at fault either -- it gates the note on `if res.hardlinked_inodes:`, so
    it prints nothing rather than a false zero. Only the JSON asserted it.
    """

    @staticmethod
    def _tree(tmp_path, hardlink: bool):
        root = tmp_path / "t"
        root.mkdir()
        for i in range(4):
            (root / f"f{i}.bin").write_bytes(b"x" * 512)
        if hardlink:
            os.link(str(root / "f0.bin"), str(root / "another-name.bin"))
        return str(root)

    @staticmethod
    def _walk_doc(root, count_only):
        from rapidu import report
        from rapidu.walk import walk

        res = walk(root, threads=1, depth=1, count_only=count_only)
        # `res` is to_json's FIRST parameter; the 4th is a DeletedScan.
        return report.to_json(res, None, None, None, None, 10)["walk"]

    def test_the_hardlink_figures_are_null_under_count_only(self, tmp_path):
        doc = self._walk_doc(self._tree(tmp_path, hardlink=True), count_only=True)
        assert doc["hardlinked_inodes"] is None
        assert doc["hardlink_extra_refs"] is None

    # The one key in this document that is a reading of the *clock* rather than a
    # figure read off the tree, and the reason the scan below cannot simply take
    # every numeric key. `-c` measures elapsed time exactly as honestly as a full
    # walk does; what differs is how long the two take. On this five-entry fixture
    # the full walk stats every entry and lands near 0.00013 s while the stat-free
    # walk lands near 0.00010 s -- both `0.0` once `round(..., 3)` has had them,
    # until the full walk's tail crosses 0.0005 s and rounds to 0.001 instead.
    # Then the fast path's `0.0` reads as a fabricated zero and this test fails on
    # a timing rather than on a claim: 3 of 300 in-process repetitions on an idle
    # login node, 4 of 25 full-suite runs on a loaded one, and 8 of 8 once the
    # fixture is widened to 128 files, which is the same crossing made reliable.
    #
    # The same call is already made one file over and for the same reason:
    # `test_walk_throttles._snapshot` compares every attribute of `WalkResult` and
    # skips `elapsed` because it "is the thing being varied". The alternative -- a
    # fixture big enough for both walks to take a measurable time -- is the one
    # this suite has already refused in so many words: "an upper bound on elapsed
    # time would be a coin flip on a shared login node."
    #
    # Named, not inferred from a type or a `_seconds` suffix, and pinned by
    # `test_the_timing_exclusion_covers_only_the_timing` below, so this cannot
    # become a list that quietly grows over a real fabricated zero. What makes the
    # exclusion honest rather than a cover-up is that `-c` genuinely takes this
    # measurement -- `test_the_excluded_timing_is_a_reading_and_not_an_absence`.
    _CLOCK_NOT_TREE = ("elapsed_seconds",)

    @classmethod
    def _false_zeroes(cls, full, lean):
        """Keys the full walk gives a nonzero number and `-c` gives exactly 0.

        Factored out of the test below so the control can exercise this scan
        rather than a second copy of it.
        """
        return [
            k
            for k, v in full.items()
            if k not in cls._CLOCK_NOT_TREE
            and isinstance(v, (int, float))
            and not isinstance(v, bool)
            and v
            and lean.get(k) == 0
            and not isinstance(lean.get(k), bool)
        ]

    def test_no_field_reports_zero_where_the_full_walk_reports_a_number(self, tmp_path):
        """The general invariant, so a future missed site is caught too.

        Rather than naming the two fields, this compares the whole walk document
        against the full walk's: any key the full walk gives a nonzero number and
        `-c` gives exactly 0 is a figure claimed rather than measured. `None` is
        the correct answer and passes; a different nonzero number passes too
        (`inodes` legitimately differs, 5 against 4, because a hard link is
        counted once per name without stat -- which the help states).

        `_CLOCK_NOT_TREE` is out of scope, for the reason given there.
        """
        root = self._tree(tmp_path, hardlink=True)
        full = self._walk_doc(root, count_only=False)
        lean = self._walk_doc(root, count_only=True)
        false_zeroes = self._false_zeroes(full, lean)
        assert false_zeroes == [], (
            "these fields publish a measured-looking 0 under -c: %s" % false_zeroes
        )

    def test_the_timing_exclusion_covers_only_the_timing(self, tmp_path):
        """CONTROL -- what stops `_CLOCK_NOT_TREE` growing to hide a defect.

        Every other numeric key at this level is a count of bytes or of entries,
        i.e. a figure read off the tree, and belongs in the scan. Checked against
        both real documents rather than as a bare literal, so an exclusion that no
        longer names a live key -- silently skipping nothing, or skipping a key
        that has since become an integer count -- is caught here.
        """
        root = self._tree(tmp_path, hardlink=True)
        full = self._walk_doc(root, count_only=False)
        lean = self._walk_doc(root, count_only=True)
        assert self._CLOCK_NOT_TREE == ("elapsed_seconds",)
        for key in self._CLOCK_NOT_TREE:
            assert key in full and key in lean, key
            # A duration, in both documents. Every figure the scan does cover is
            # an `int`, so a count arriving in this tuple would fail here.
            assert isinstance(full[key], float), key
            assert isinstance(lean[key], float), key

    def test_the_excluded_timing_is_a_reading_and_not_an_absence(self, tmp_path):
        """CONTROL -- passes before and after; the exclusion is not a cover-up.

        What separates `elapsed_seconds` from every key the scan covers is whether
        `-c` measures the quantity at all. `size_bytes` is structurally absent
        there: no tree makes the stat-free walk read `st_blocks`, which is why it
        has to be `null`. The clock is not absent -- `perf_counter` ran across the
        stat-free walk and returned a positive interval, and the published `0.0` is
        that interval rounded to milliseconds rather than the lack of one.

        Asserted on the raw `elapsed`, so this is a floor at zero and not a bound
        on how fast the machine is.
        """
        from rapidu import report
        from rapidu.walk import walk

        root = self._tree(tmp_path, hardlink=True)
        res = walk(root, threads=1, depth=1, count_only=True)
        doc = report.to_json(res, None, None, None, None, 10)["walk"]
        assert res.elapsed > 0.0, "the clock ran across the stat-free walk"
        assert doc["elapsed_seconds"] == round(res.elapsed, 3), "published as read"
        assert res.size == 0 and doc["size_bytes"] is None, (
            "st_blocks was never read, so the 0 behind it is not a reading"
        )

    def test_the_scan_still_catches_a_fabricated_zero(self, tmp_path):
        """CONTROL -- passes before and after, and the one that matters most.

        Excluding a key from a scan sits one keystroke away from excluding the
        defect, so the scan is run over the real pair of documents with real
        fabrications put back into the `-c` one: the two hard-link fields
        unwrapped, as they were before `_unmeasured` reached them, plus a byte
        figure. The full walk reports 1, 1 and 16384 on this tree, so all three
        have to come back -- skipping `elapsed_seconds` must not cost the scan its
        teeth.
        """
        root = self._tree(tmp_path, hardlink=True)
        full = self._walk_doc(root, count_only=False)
        lean = self._walk_doc(root, count_only=True)
        # The baseline is stated rather than inherited: the timing pinned to the
        # full walk's reading, and the three subject keys pinned to the `null` the
        # schema asks for. So this measures the scan's sensitivity and nothing
        # else -- not the machine's clock, and not what `to_json` currently does
        # with those keys, which is the invariant test's job one method up. It
        # holds with `_CLOCK_NOT_TREE` in force and with it emptied, which is what
        # makes it a control.
        clean = dict(
            lean,
            elapsed_seconds=full["elapsed_seconds"],
            hardlinked_inodes=None,
            hardlink_extra_refs=None,
            size_bytes=None,
        )
        assert self._false_zeroes(full, clean) == []
        regressed = dict(clean, hardlinked_inodes=0, hardlink_extra_refs=0, size_bytes=0)
        assert sorted(self._false_zeroes(full, regressed)) == [
            "hardlink_extra_refs",
            "hardlinked_inodes",
            "size_bytes",
        ]

    def test_the_full_walk_still_reports_the_real_figures(self, tmp_path):
        """CONTROL -- passes before and after; the fix must not null everything."""
        doc = self._walk_doc(self._tree(tmp_path, hardlink=True), count_only=False)
        assert doc["hardlinked_inodes"] == 1
        assert doc["hardlink_extra_refs"] == 1

    def test_a_genuinely_measured_zero_stays_zero(self, tmp_path):
        """CONTROL -- and the one that matters most.

        A full walk over a tree with no hard links measured zero of them, and that
        IS a measurement. Turning it into `null` would trade this defect for its
        mirror: a consumer unable to tell "no hard links" from "did not look".
        """
        doc = self._walk_doc(self._tree(tmp_path, hardlink=False), count_only=False)
        assert doc["hardlinked_inodes"] == 0
        assert doc["hardlink_extra_refs"] == 0
