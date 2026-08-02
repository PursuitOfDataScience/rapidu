"""Rendering. Plain text by default (it goes in a ticket), ``--json`` for tools.

Two rules govern everything printed here:

* An absent measurement prints ``n/a`` **with a reason**. It never prints ``0``.
* Any figure derived from more than one source prints the age and completeness
  of both, so a reader can see whether the comparison was safe.
"""

import os
import pwd
from typing import Any, Dict, List, Optional

from . import reconcile as rc
from . import ui
from .deleted import DeletedScan
from .fmt import files_per_gib, human_bytes, human_count, human_duration, pct
from .quota import QuotaSnapshot
from .walk import SettleCheck, WalkResult

_RULE = "-" * 78


def _uname(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _counter(n: Optional[int]) -> str:
    """``human_count`` under the same signature as ``human_bytes``."""
    return human_count(n)


def render_quota(
    snap: QuotaSnapshot, paths: Optional[List[str]] = None, style: Optional[ui.Style] = None
) -> List[str]:
    style = style or ui.resolve_style("never")
    if not snap.available:
        return [
            ui.heading("QUOTA", style),
            "  n/a - {}".format(snap.reason or "no quota backend available"),
        ]

    age = snap.age_seconds
    if age is None:
        stamp = style.paint("age UNKNOWN (backend published no timestamp)", "yellow")
    else:
        text = "snapshot {} old".format(human_duration(age))
        stale = age > rc.DEFAULT_MAX_SNAPSHOT_AGE_S
        stamp = style.paint(
            text + (" -- predates anything you just did" if stale else ""),
            "yellow" if stale else "dim",
        )
    out = ["{}  {}".format(ui.heading("QUOTA", style), stamp)]
    if snap.time_note:
        out.append(style.paint("  ? " + snap.time_note, "yellow"))

    rows = snap.rows
    if paths:
        keep = []
        for p in paths:
            for r in snap.rows_for_path(p):
                if r not in keep:
                    keep.append(r)
        if keep:
            rows = keep

    for r in rows:
        used = human_count(r.used) if r.kind == "files" else human_bytes(r.used)
        soft = (
            "n/a"
            if r.soft is None
            else (human_count(r.soft) if r.kind == "files" else human_bytes(r.soft))
        )
        frac = r.usage_fraction
        # Colour is the only thing that makes a near-full quota jump out of a
        # table of thirty rows.
        tone = "green"
        if frac is not None and frac >= 0.90:
            tone = "red"
        elif frac is not None and frac >= 0.75:
            tone = "yellow"
        bar = ui.bar(frac if frac is not None else 0.0, 10, style, accent=tone, min_tick=False)
        grace = ""
        if r.grace and r.grace.lower() not in ("none", "-", ""):
            # An expired soft limit stops writes; it cannot sit in a quiet column.
            grace = style.paint("  ! IN GRACE, {} left".format(r.grace), "red")
        out.append(
            "  {}  {}  {}  {}  {}  {}{}".format(
                style.paint("{:<16}".format(r.fileset[:16]), "bold"),
                style.paint("{:<6}".format(r.kind), "dim"),
                "{:>11} / {:<11}".format(used, soft),
                bar,
                style.paint("{:>6}".format(pct(frac, 1.0) if frac is not None else "n/a"), tone),
                style.paint(r.mount or "?", "dim"),
                grace,
            )
        )

    if any(not r.mount for r in rows):
        for note in snap.mapping_notes():
            out.append(style.paint("  ? " + note, "dim"))
    return out


def render_entries(
    res: WalkResult, top: int, by_inodes: bool, style: ui.Style, indent: str = "  "
) -> List[str]:
    """The ranked table: size, proportional bar, share of tree, inodes, path.

    Sizes are cumulative subtree totals, so any row agrees with ``du -s`` on that
    path. Plain files appear alongside directories -- three 63 MiB ``.db`` files
    in a home directory are a quarter of it, and a directory-only listing cannot
    show them. That is also why the last column is headed ``path`` and not
    ``directory``: not everything in it is one.

    The path comes last and is never truncated: it is the only variable-width
    column, and putting it at the end keeps every numeric column aligned no
    matter how deep the paths go. It is also the order ``du`` prints.
    """
    # After an interrupt, only subtrees that were walked to completion can be
    # ranked. A directory caught mid-walk carries an arbitrary fraction of its
    # contents, and placing that in an ordered table is not an approximation --
    # it is the wrong answer, presented with the authority of the right one.
    # -n 0 means "all of them", not "none".
    limit = top if top > 0 else 10**9
    ranked = res.top_dirs(limit, "files" if by_inodes else "size", finished_only=res.partial)
    if not ranked:
        return []

    rest_size = max(0, res.size - sum(e.size for e in ranked))
    rest_inodes = max(0, res.inodes - sum(e.inodes for e in ranked))
    # A remainder row is only well defined when the listed entries are siblings
    # that partition the tree. At depth > 1 rows nest and it would double-count;
    # after an interrupt the unscanned part is unknown, so there is no remainder
    # to state.
    #
    # It also requires something to actually be hidden. With everything shown the
    # leftover is exactly the root directory's own inode -- which belongs to no
    # child -- and reporting that as "(0 more)" is noise.
    hidden = _other_count(res, ranked)
    show_rest = hidden > 0 and rest_size > 0 and not res.partial and _entries_partition_tree(res)

    # The bar and the share must measure whatever the rows were ranked by, or a
    # -i listing shows an inode ordering with byte-length bars and reads as
    # though it were mis-sorted.
    def metric(e):
        return e.inodes if (by_inodes or res.count_only) else e.size

    # A share needs a denominator. After an interrupt there is no tree total, so
    # the column is blanked rather than filled with a fraction of an accident.
    if res.partial:
        total = 0
    elif by_inodes or res.count_only:
        total = res.inodes or 1
    else:
        total = res.size or 1

    # The bar is scaled to the *total*, so a full track is the whole tree and the
    # bar says exactly what the share column beside it says. It used to be scaled
    # to the largest listed row, which made the top row's bar full on every
    # listing ever printed -- 31.9% of a tree drawn as a completely full bar,
    # directly left of the text "31.9%". A mark that is identical on every run
    # carries no information, and here it actively contradicted the number.
    #
    # Only when there is no total -- an interrupted walk -- does it fall back to
    # ranking against the largest row, and there the share column is already
    # blank, so nothing disagrees with anything.
    peak = max(metric(e) for e in ranked) or 1
    scale = float(total) if total else float(peak)

    # Colour is assigned across the listing rather than row by row, so that two
    # rows share a tone only when they are genuinely the same size.
    tones = style.heat_scale([metric(e) for e in ranked])

    ranked_by_files = bool(by_inodes or res.count_only)
    rows = [
        _entry_line(
            os.path.relpath(e.path, res.root) + ("/" if e.is_dir else ""),
            e.size,
            e.inodes,
            metric(e),
            metric(e) / scale,
            total,
            tone,
            style,
            indent,
            size_hidden=res.count_only,
            ranked_by_files=ranked_by_files,
        )
        for e, tone in zip(ranked, tones)
    ]
    if show_rest:
        # Say how to see them. A truncated listing that does not tell you it is
        # truncated, or how to expand it, is just missing data.
        label = "({} more {} use -n 0 for all)".format(human_count(hidden), ui.dash(style))
        rest_value = rest_inodes if ranked_by_files else rest_size
        rows.append(
            _entry_line(
                label,
                rest_size,
                rest_inodes,
                rest_value,
                rest_value / scale,
                total,
                "dim",
                style,
                indent,
                aggregate=True,
                size_hidden=res.count_only,
                ranked_by_files=ranked_by_files,
            )
        )
    return rows


_BAR_W = 18


def _entry_line(
    name: str,
    size: int,
    inodes: int,
    value: int,
    fraction: float,
    total: int,
    tone: str,
    style: ui.Style,
    indent: str,
    aggregate: bool = False,
    size_hidden: bool = False,
    ranked_by_files: bool = False,
) -> str:
    """One row. ``fraction`` drives the bar; ``tone`` is assigned by the listing.

    **The column the rows were ranked by carries the tone.** Bar, share and path
    carry it too, so whichever of those the eye lands on gives the same answer.
    The other numeric column is a real measurement that simply is not the sort
    key, and it is painted :attr:`ui.Style.muted` -- quieter than the ramp, but
    legible. It used to be ``dim``, which is the grey this report uses for
    *context* (a snapshot age, a caveat), and a column of file counts in it read
    as furniture rather than as data. Worse, under ``-i`` the count column was
    the one the listing was sorted by and it was still the greyest thing on the
    line.

    Files are *not* dimmed. A 63 MiB file that is 9% of a home directory is more
    worth looking at than a 3% directory, and greying it out said the opposite.
    Directories are marked the way ``ls -p`` marks them, with a trailing slash --
    shape, not colour, so colour is left free to mean size.

    The remainder row is a summary of many entries rather than one of them, so
    it takes a hatched bar and the muted tone throughout: it still shows its true
    length -- a quarter of the tree is worth seeing -- without impersonating a
    single directory that size.
    """
    lead = style.muted if aggregate else tone
    bar = ui.bar(fraction, _BAR_W, style, accent=lead, hatched=aggregate)
    # In count mode there are no sizes at all, so the column is omitted rather
    # than left as ten blank characters the eye has to step over.
    size_tone = style.muted if (ranked_by_files and not aggregate) else lead
    files_tone = lead if (ranked_by_files or aggregate) else style.muted
    size_cell = "" if size_hidden else style.paint(human_bytes(size).rjust(10), size_tone) + "  "
    return "{}{}{}  {}  {}  {}".format(
        indent,
        size_cell,
        bar,
        style.paint("{:>6}".format(pct(value, total) if total else ""), lead),
        style.paint("{:>9}".format(human_count(inodes)), files_tone),
        style.paint(name, *(["dim"] if aggregate else [tone])),
    )


# indent + size(10) + 2 + bar + 2 + share(6) + 2 + files(9) + 2
_FIXED_COLS = 2 + 10 + 2 + 2 + 6 + 2 + 9 + 2


def _entries_rule(style: ui.Style, names: List[str], indent: str = "  ") -> str:
    """A hairline between the header and the table, sized to the table.

    One dim rule is enough structure to separate the two blocks; a box would be
    heavier than a du replacement warrants and breaks when pasted into a ticket.
    Sized to the widest row rather than to the terminal, because a rule running
    forty characters past the last column looks like a mistake.
    """
    glyph = "\u2500" if style.unicode else "-"
    widest = max([len(n) for n in names] or [8])
    span = min(style.width - len(indent) - 1, _FIXED_COLS + _BAR_W + widest)
    # The same near-background grey the bar tracks use, so the rule and the
    # eighteen boxes below it read as one frame rather than two greys.
    return style.paint(indent + glyph * max(20, span), style.track)


def _entries_header(
    style: ui.Style,
    indent: str = "  ",
    size_label: str = "size",
    bar_label: str = "of tree",
    ranked_by_files: bool = False,
) -> str:
    """Column labels, with the one the table is sorted by marked.

    The sort key is otherwise invisible: a ``-i`` listing and a default listing
    have identical headers and differ only in an ordering the reader has to
    infer by checking two rows against each other. Marking the active column
    costs nothing and answers it at a glance.

    The count column is headed ``files``, not ``inodes``. "inode" is the correct
    term -- it is the on-disk structure a file or directory occupies, and it is
    the resource that runs out -- but the quota the reader is up against calls
    them files (``files (user) 21,553 / 300,000``), so using the quota's own word
    lets the two numbers be compared without a translation step. Directories are
    included in the count, exactly as the quota includes them.

    The last column is ``path``, not ``name`` and not ``directory``. ``name`` said
    nothing -- every column is the name of something -- and ``directory`` would be
    a lie, because plain files are ranked here too.
    """
    size_tone = "dim" if ranked_by_files else "bold"
    files_tone = "bold" if ranked_by_files else "dim"
    head = "" if not size_label else style.paint("{:>10}".format(size_label), size_tone) + "  "
    return "{}{}{}  {}  {}  {}".format(
        indent,
        head,
        style.paint("{:<{}}".format(bar_label, _BAR_W), "dim"),
        style.paint("{:>6}".format("share"), "dim"),
        style.paint("{:>9}".format("files"), files_tone),
        style.paint("path", "dim"),
    )


def _other_count(res: WalkResult, shown: List[Any]) -> int:
    return max(0, len([e for e in res.dir_agg.values() if e.path != res.root]) - len(shown))


def _entries_partition_tree(res: WalkResult) -> bool:
    """True when every reported entry is a direct child of the root."""
    parents = {os.path.dirname(e.path) for e in res.dir_agg.values() if e.path != res.root}
    return parents == {res.root}


def _entry_names(res: WalkResult, top: int, by_inodes: bool) -> List[str]:
    limit = top if top > 0 else 10**9
    key = "files" if (by_inodes or res.count_only) else "size"
    return [
        os.path.relpath(e.path, res.root) + ("/" if e.is_dir else "")
        for e in res.top_dirs(limit, key, finished_only=res.partial)
    ]


def _entry_total(res: WalkResult) -> int:
    return len([e for e in res.dir_agg.values() if e.path != res.root])


def _facts(style: ui.Style, pairs: List[Any]) -> str:
    """``21,829 files  ·  94 entries  ·  0.17s`` -- three numbers, three roles.

    This line used to be painted one flat grey end to end. Three measurements
    and two separators at one weight is a wall of text with no entry point, and
    the three things a reader wants from it were the same colour as the
    punctuation between them.

    Each fact now carries the encoding its *kind* earns, so the line can be read
    without parsing it:

    * **files** -- the other quota axis, and the one that runs out first on an
      ML tree. Cyan, the same accent the report uses elsewhere for counts.
    * **entries** -- how many rows the table is drawn from. Structure, not a
      measurement of the tree: muted.
    * **elapsed** -- how long this took. Context: dim.

    The separators drop to the track grey, quieter than any of them, because
    punctuation should be the last thing the eye lands on.
    """
    joiner = style.paint("  {}  ".format(ui.sep(style)), style.track)
    out = []  # type: List[str]
    for value, noun, tones in pairs:
        # `tones` is passed whole rather than assembled here: pairing every fact
        # with "bold" would emit dim+bold together for the elapsed time, and a
        # terminal that implements faint as "not bold" resolves that to whichever
        # code it saw last -- so the quietest fact came out as the loudest.
        cell = style.paint(value, *tones)
        out.append(cell + " " + style.paint(noun, "dim") if noun else cell)
    return joiner.join(out)


def _header(style: ui.Style, headline: str, path: str, subtitle: str) -> List[str]:
    """Lead with the answer, then the path, then the metadata.

    ``du -sh`` prints the size first and everyone reads it that way, so the size
    goes first and carries the visual weight.

    The path is split. Everything up to the last component is where the tree
    happens to live and is dimmed; the last component is *what was measured* and
    is bold. On ``/scratch/midway3/$USER/experiments/run-14`` that is the
    difference between a wall of equally-weighted text and a line whose subject
    you can find without reading it. Counts and timing are weighted by
    :func:`_facts`.
    """
    parent, _, leaf = path.rpartition(os.sep)
    subject = (
        style.paint(parent + os.sep, "dim") + style.paint(leaf, "bold")
        if parent and leaf
        else style.paint(path, "bold")
    )
    return [
        "{}   {}".format(style.paint(headline.rjust(10), "bold_cyan"), subject),
        "{}   {}".format(" " * 10, subtitle),
        "",
    ]


def render_compact(
    res: WalkResult, settle: SettleCheck, top: int, by_inodes: bool, style: ui.Style
) -> List[str]:
    """The default view: how big is this tree, and what is big inside it.

    That is the question ``sd .`` is asked, and answering it should look like
    ``du -sh`` with a breakdown -- not like a diagnostic report. Everything this
    tool knows that ``du`` does not (quota and its age, the reconciliation, the
    /proc scan) is real work the user did not ask for here, costs latency, and
    lives behind ``--full``.

    Only warnings that change what the number *means* survive into this view: an
    incomplete walk, or drift that was actually measured. A caveat that fires on
    every run is not a warning, it is furniture.
    """
    if res.count_only:
        out = _header(
            style,
            "{} files".format(human_count(res.inodes)),
            res.root,
            _facts(
                style,
                [
                    ("counts only, no sizes", "", ("yellow",)),
                    (human_count(_entry_total(res)), "entries", (style.muted, "bold")),
                    ("{:.2f}s".format(res.elapsed), "", ("dim",)),
                ],
            ),
        )
    elif res.partial:
        out = _header(
            style,
            human_bytes(res.size),
            res.root,
            style.paint(
                "PARTIAL \u2014 {} files scanned before the interrupt".format(
                    human_count(res.inodes)
                ),
                "yellow",
            ),
        )
    else:
        out = _header(
            style,
            human_bytes(res.size),
            res.root,
            _facts(
                style,
                [
                    (human_count(res.inodes), "files", ("cyan", "bold")),
                    (human_count(_entry_total(res)), "entries", (style.muted, "bold")),
                    ("{:.2f}s".format(res.elapsed), "", ("dim",)),
                ],
            ),
        )
    out.extend(_hard_warnings(res, settle, style))
    out.extend(render_allocation(res, style))
    hint = _count_hint(res, by_inodes, style)
    body = render_entries(res, top, by_inodes, style)
    if body:
        out.append(_entries_rule(style, _entry_names(res, top, by_inodes)))
        out.append(
            _entries_header(
                style,
                size_label="" if res.count_only else "size",
                # An interrupted walk has no total to be a share of, so the bar
                # falls back to ranking against the largest row and says so.
                bar_label="of largest" if res.partial else "of tree",
                ranked_by_files=bool(by_inodes or res.count_only),
            )
        )
        out.extend(body)
    out.extend(hint)
    return out


# Below this a walk is over before the hint could have saved anything.
_HINT_AFTER_S = 3.0


def _count_hint(res: WalkResult, by_inodes: bool, style: ui.Style) -> List[str]:
    """Tell a slow ``-i`` walk that the fast path it wanted already exists.

    ``-i`` asks the inode question and ``-c`` is the flag that answers it
    without ``stat``, but they are independent and nothing connects them, so a
    user asking "where are my inodes" reaches for ``-i`` and pays for the full
    stat walk. Keeping them separate is *correct* -- ``-c`` counts names, not
    inodes, so making ``-i`` imply it would silently turn a hard-linked file
    into several and regress exactly the kind of accuracy this codebase guards.
    Saying so costs one dim line and gives up nothing.
    """
    if not by_inodes or res.count_only or res.partial or res.elapsed < _HINT_AFTER_S:
        return []
    return [
        style.paint(
            "  hint: -i -c answers this ~{:.0f}x faster ({:.0f}s here) by skipping stat; "
            "a hard-linked file then counts once per name, not once per inode.".format(
                walk_speedup(), res.elapsed / walk_speedup()
            ),
            "dim",
        )
    ]


def walk_speedup() -> float:
    from .walk import COUNT_SPEEDUP

    return COUNT_SPEEDUP


def _hard_warnings(
    res: WalkResult, settle: SettleCheck, style: ui.Style, settling: bool = True
) -> List[str]:
    """Only the things that change what the headline number means.

    ``settling`` is off for the full report, which follows this with the whole
    SETTLING block and would otherwise state the drift twice.
    """
    out = []  # type: List[str]
    if res.partial:
        out.append(
            ui.alarm(
                "INTERRUPTED after {:.0f}s -- this is not a measurement of the tree.".format(
                    res.elapsed
                ),
                style,
            )
        )
        out.append(
            style.paint(
                "  {} of {} top-level entries were walked to completion and are"
                " listed below; the rest is unknown, so there is no total and no"
                " share of anything.".format(
                    len(res.top_dirs(10**6, finished_only=True)),
                    len([e for e in res.dir_agg.values() if e.path != res.root]),
                ),
                "dim",
            )
        )
    elif not res.complete:
        detail = []
        if res.unreadable_dirs:
            detail.append("{} dirs unreadable".format(len(res.unreadable_dirs)))
        if res.unstatable:
            detail.append("{} entries unstatable".format(res.unstatable))
        out.append(ui.alarm("this is a FLOOR, not a total: " + ", ".join(detail), style))
    if settling and settle.moved:
        # With --settle-wait 0 the re-stat is immediate, so "0s later" would be a
        # fabricated precision: the real observation window is however long the
        # walk itself took to reach the end.
        when = "{:.0f}s later".format(settle.gap) if settle.gap >= 1 else "after the walk"
        out.append(
            ui.warn(
                "still settling: a re-stat {} found {} {} allocated".format(
                    when,
                    human_bytes(abs(settle.drift)),
                    "more" if settle.drift > 0 else "less",
                ),
                style,
            )
        )
    return out


def render_walk(
    res: WalkResult,
    settle: SettleCheck,
    top: int = 10,
    show_uids: bool = True,
    scan: Optional[DeletedScan] = None,
    style: Optional[ui.Style] = None,
    by_inodes: bool = False,
) -> List[str]:
    """The walk block of the full report: headline, then facts worth a line each.

    The headline carries only the path and the total. The breakdown moved down to
    the facts line because a long scratch path plus the counts ran past 100
    columns and got clipped -- and the clipped end was the number, not the path.
    """
    style = style or ui.resolve_style("never")
    out = [
        "",
        "{}  {}   {}".format(
            ui.heading("WALK", style),
            style.paint(ui.truncate(res.root, max(24, style.width - 28)), "bold"),
            style.paint(human_bytes(res.size), "bold_cyan"),
        ),
    ]

    facts = [
        "{} files ({} regular + {} dirs)".format(
            human_count(res.inodes),
            human_count(res.files - res.hardlink_extra_refs),
            human_count(res.dirs),
        ),
        "{:.2f}s at {} threads ({:,.0f} files/s)".format(
            res.elapsed, res.threads, res.inodes / res.elapsed if res.elapsed > 0 else 0.0
        ),
        # Stated as a ratio, not as a bare second number. "apparent 23.6 MiB"
        # beside "187.6 MiB" left the reader to divide.
        "apparent {}{}".format(
            human_bytes(res.apparent),
            " ({:.1f}x allocated)".format(res.alloc_ratio) if res.alloc_ratio else "",
        ),
    ]
    if res.alloc_unit:
        facts.append("{} allocation unit".format(human_bytes(res.alloc_unit)))
    if res.hardlinked_inodes:
        facts.append(
            "{} hard-linked files, {} extra names deduped".format(
                human_count(res.hardlinked_inodes), human_count(res.hardlink_extra_refs)
            )
        )
    if scan is not None and scan.available and not scan.files:
        # "none visible", not "none": this scan sees neither other users'
        # processes nor any compute node. See `render_deleted`.
        facts.append(
            "no unlinked-but-open space visible ({} of {} pids inspectable, this node only)".format(
                scan.scanned_pids, scan.scanned_pids + scan.unreadable_pids
            )
        )
    out.append(style.paint("  " + "  ".join(facts), "dim"))

    out.extend(_hard_warnings(res, settle, style, settling=False))
    out.extend(render_allocation(res, style, indent="    "))
    out.extend(render_settle(res, settle, style))

    if not res.complete and res.unreadable_dirs:
        for path, why in res.unreadable_dirs[:3]:
            out.append(style.paint("      {} ({})".format(path, why), "dim"))
        if len(res.unreadable_dirs) > 3:
            out.append(
                style.paint("      ... and {} more".format(len(res.unreadable_dirs) - 3), "dim")
            )

    if show_uids and len(res.by_uid) > 1:
        out.append("")
        out.append(style.paint("  owners (a group quota charges all of these):", "dim"))
        for uid, (size, inodes) in sorted(
            res.by_uid.items(), key=lambda kv: kv[1][0], reverse=True
        )[:6]:
            out.append(
                "      {:<16}{:>12}  {:>12} files".format(
                    _uname(uid), human_bytes(size), human_count(inodes)
                )
            )

    body = render_entries(res, top, by_inodes, style)
    if body:
        out.append(_entries_rule(style, _entry_names(res, top, by_inodes)))
        out.append(
            _entries_header(
                style,
                size_label="" if res.count_only else "size",
                # An interrupted walk has no total to be a share of, so the bar
                # falls back to ranking against the largest row and says so.
                bar_label="of largest" if res.partial else "of tree",
                ranked_by_files=bool(by_inodes or res.count_only),
            )
        )
        out.extend(body)
    return out


def render_settle(
    res: WalkResult, settle: SettleCheck, style: Optional[ui.Style] = None
) -> List[str]:
    """The "this tree has not settled" warning -- a truth ``du`` cannot tell."""
    style = style or ui.resolve_style("never")
    if not res.recent_files:
        return []

    # A handful of freshly written files in a large tree cannot move the
    # headline number, and spending five lines saying so trains the reader to
    # skip the section on the run where it matters. Compact unless it is either
    # measurably moving or big enough to move things.
    if not settle.moved and not _settling_is_material(res):
        return [
            style.paint(
                "  {:<22}{} file{} written in the last {} -- figure is provisional"
                " (--settle-wait 60 to measure)".format(
                    "settling",
                    human_count(res.recent_files),
                    "" if res.recent_files == 1 else "s",
                    human_duration(res.settle_window),
                ),
                "dim",
            )
        ]

    out = ["", "  " + ui.heading("SETTLING", style)]
    out.append(
        style.paint(
            "      {} file{} written in the last {}".format(
                human_count(res.recent_files),
                " was" if res.recent_files == 1 else "s were",
                human_duration(res.settle_window),
            ),
            "dim",
        )
    )

    if settle.moved:
        direction = "MORE" if settle.drift > 0 else "LESS"
        out.append(
            "    "
            + ui.warn(
                "a re-stat {} found {} {} allocated: this tree is still moving.".format(
                    "{:.0f}s later".format(settle.gap) if settle.gap >= 1 else "after the walk",
                    human_bytes(abs(settle.drift)),
                    direction,
                ),
                style,
            )
        )
        out.append(
            style.paint(
                "        Any size you read right now -- from this tool or from du -- is\n"
                "        provisional. Measured on GPFS, a freshly written tree has settled\n"
                "        both upward (5.58x) and downward (3.3x) over ~60s.",
                "dim",
            )
        )
        if settle.sampled:
            out.append(
                style.paint(
                    "        (re-stat covered {} of {} recent files)".format(
                        human_count(settle.checked), human_count(settle.sampled_of)
                    ),
                    "dim",
                )
            )
    elif settle.conclusive:
        out.append(
            style.paint(
                "      re-stat {:.0f}s later found no change in {} of them; "
                "the figure looks settled".format(settle.gap, human_count(settle.checked)),
                "dim",
            )
        )
    else:
        # A re-stat taken immediately cannot see an effect that takes tens of
        # seconds. Saying "looks settled" here would be a null result from a
        # blind instrument -- but four lines of caveat for a handful of files in
        # a large tree is noise, so the long form is reserved for the case where
        # the unsettled files could actually move the total.
        out.append(
            style.paint(
                "      figure is PROVISIONAL -- use --settle-wait 60 to measure the drift", "dim"
            )
        )
    if settle.gone:
        out.append(
            style.paint(
                "      {} of them disappeared between the walk and the re-stat".format(settle.gone),
                "dim",
            )
        )
    return out


# A ratio closer to 1 than this is rounding, not a finding, and a difference
# smaller than this cannot be worth a line however lopsided the ratio looks.
# The floor is `reconcile.MIN_TOLERANCE_BYTES`, which is already this codebase's
# answer to "below what size is a byte difference not worth reporting".
_ALLOC_RATIO = 1.15
_ALLOC_FLOOR = rc.MIN_TOLERANCE_BYTES


def allocation_is_material(res: WalkResult) -> bool:
    """Does the gap between allocated and apparent change the answer?

    Both directions count. On the tree that motivated this the ratio is 8x the
    wrong way; one directory later the same filesystem stores 8.7 MiB in 1.6 MiB
    because the files fit inside their inodes. Neither is an error and both are
    the reason the headline number is not the number the reader expected.
    """
    if res.count_only or not res.apparent or not res.size:
        return False
    ratio = res.alloc_ratio or 1.0
    if abs(res.size - res.apparent) < _ALLOC_FLOOR:
        return False
    return ratio >= _ALLOC_RATIO or ratio <= 1.0 / _ALLOC_RATIO


def _wrapped(text: str, style: ui.Style, indent: str, tone: str = "dim") -> List[str]:
    """Soft-wrap a prose line to the terminal, painting each line separately.

    Colour has to be applied per output line rather than to the whole
    paragraph: an SGR run that spans a newline is reset by some terminals and
    inherited by others, and a paste into a ticket keeps whichever happened.
    Wrapping is measured on the *unpainted* text, because escape codes occupy no
    columns but `textwrap` would count them.
    """
    import textwrap

    width = max(40, style.width - len(indent) - 1)
    return [style.paint(indent + line, tone) for line in textwrap.wrap(text, width)]


def render_allocation(res: WalkResult, style: ui.Style, indent: str = "  ") -> List[str]:
    """Say which of the two numbers the quota is charged against, and why.

    This is the question the tool exists for. Both operands have always been in
    :class:`WalkResult` and the report printed them forty characters apart on
    adjacent lines -- ``187.6 MiB`` on one and ``apparent 23.6 MiB`` on the next
    -- without ever computing the ratio, flagging the divergence, or saying
    which one the quota counts. A reader who could do that arithmetic themselves
    did not need the tool.

    The direction matters and is not symmetric:

    * **Allocated above apparent** is padding. Every file smaller than the
      allocation unit pays for the whole unit, so a tree of small files can cost
      several times what it holds. That is recoverable: pack the files.
    * **Allocated below apparent** is *not* an error and must not be reported as
      one. The data is sparse, compressed, or small enough to live inside the
      inode. Bytes are nearly free there and the real cost is inodes, which
      ``RECONCILE`` already reports well -- so this branch points at that
      instead of inventing a byte problem.
    """
    if not allocation_is_material(res):
        return []
    ratio = res.alloc_ratio or 1.0
    unit = res.alloc_unit
    out = []  # type: List[str]

    if ratio >= 1.0:
        head = "{} allocated for {} of data — {:.1f}x".format(
            human_bytes(res.size), human_bytes(res.apparent), ratio
        )
        out.append(ui.warn(head + ". Your quota is charged the first number.", style))
        if res.padded_files and unit:
            mean = res.padded_apparent // res.padded_files
            out.extend(
                _wrapped(
                    "{} files average {} against a {} allocation unit, so they"
                    " occupy {} of padding. Packing them (tar, squashfs, a single"
                    " archive) returns it.".format(
                        human_count(res.padded_files),
                        human_bytes(mean),
                        human_bytes(unit),
                        human_bytes(res.padding),
                    ),
                    style,
                    indent + "  ",
                )
            )
    else:
        out.extend(
            _wrapped(
                "{} of data stored in {} — {:.2f}x. These files are sparse,"
                " compressed, or small enough to live in their inodes.".format(
                    human_bytes(res.apparent), human_bytes(res.size), ratio
                ),
                style,
                indent,
            )
        )
        out.extend(
            _wrapped(
                "Bytes are nearly free here; {} inodes are the cost that will"
                " run out first.".format(human_count(res.inodes)),
                style,
                indent + "  ",
            )
        )
    return out


def _settling_is_material(res: WalkResult) -> bool:
    """Could the unsettled files plausibly move the headline number?

    23 recently written files in a 21,530-inode tree cannot, and saying so at
    length trains the reader to skip the section for the run where it can.
    """
    if not res.recent_files:
        return False
    return res.recent_files >= max(50, res.inodes // 100) or res.recent_apparent >= (256 << 20)


def render_deleted(scan: DeletedScan, top: int = 10, style: Optional[ui.Style] = None) -> List[str]:
    """Space with no directory entry. Every other section honours --color; so
    does this one -- and here the headline figure is genuinely an alarm, because
    it is quota being charged for something no walker can show you."""
    style = style or ui.resolve_style("never")
    out = ["", ui.heading("UNLINKED BUT STILL OPEN", style), style.paint(_RULE, "dim")]
    if not scan.available:
        out.append("  n/a - {}".format(scan.reason))
        return out
    if not scan.files:
        # Not an all-clear, and it must not read as one. On a login node this
        # scan typically reaches ~2% of the processes on the box, and the case
        # that motivates the whole section -- a job holding a deleted checkpoint,
        # the most expensive class in the ticket record -- runs on a compute node
        # this scan cannot see by construction. "none found" is true; "nothing
        # there" is not what was measured.
        out.append(
            style.paint(
                "  none found in the {} of {} processes this scan can inspect".format(
                    scan.scanned_pids, scan.scanned_pids + scan.unreadable_pids
                ),
                "dim",
            )
        )
        out.append(
            style.paint(
                "  this is not an all-clear: a job holding a deleted file on a "
                "compute node cannot be seen from here.",
                "dim",
            )
        )
    else:
        out.append(
            "  {} {}".format(
                style.paint(human_bytes(scan.total_size), "bold_red"),
                style.paint(
                    "held by open file descriptors in {} inodes".format(len(scan.files)), "bold"
                ),
            )
        )
        out.append(style.paint("  (invisible to du, to ls, and to this tool's own walk)", "dim"))
        out.append("")
        for f in scan.files[:top]:
            holders = ", ".join(
                "{} {}".format(p, c.split()[0] if c else "?") for p, c in f.holders[:3]
            )
            out.append(
                "      {}  {}".format(
                    style.paint("{:>10}".format(human_bytes(f.size)), "bold_yellow"),
                    style.paint("pid {}".format(holders), "cyan"),
                )
            )
            out.append("                  {}".format(f.path))
        if len(scan.files) > top:
            out.append(style.paint("      ... and {} more".format(len(scan.files) - top), "dim"))
    scope = ["", "  scope: this node only, {} processes inspected".format(scan.scanned_pids)]
    if scan.unreadable_pids:
        scope.append(
            "         {} processes belong to other users and cannot be inspected".format(
                scan.unreadable_pids
            )
        )
        scope.append("         without root, so this figure is a floor.")
    scope.append("         A job holding a deleted file on a compute node is not visible here.")
    out.extend(style.paint(ln, "dim") if ln else ln for ln in scope)
    return out


def render_reconcile(recs: List[rc.Reconciliation], style: Optional[ui.Style] = None) -> List[str]:
    """One line per comparison when it closes; the full account when it does not.

    A reconciliation that agrees needs to say so and get out of the way. One that
    is blocked or unexplained is the whole reason the section exists, and gets
    the numbers, the blockers and the candidate causes.
    """
    style = style or ui.resolve_style("never")
    out = ["", ui.heading("RECONCILE", style)]
    for r in recs:
        label = "bytes" if r.kind == "blocks" else "files"
        show = _counter if r.kind == "files" else human_bytes

        if r.verdict == rc.NOT_COMPARED:
            out.append(
                "  {}  {}".format(
                    label, style.paint(r.notes[0] if r.notes else "not compared", "dim")
                )
            )
            continue

        if r.verdict == rc.CLOSES:
            out.append(
                "  {}  {}  {}".format(
                    label,
                    style.paint("reconciles", "green"),
                    style.paint(
                        "{} vs quota {}, difference {} (within {})".format(
                            show(r.accounted), show(r.quota_value), show(r.gap), show(r.tolerance)
                        ),
                        "dim",
                    ),
                )
            )
            for b in r.blockers:
                out.append(style.paint("      caveat: " + b, "dim"))
            continue

        if r.verdict == rc.SUBTREE:
            out.append("  {}  {}".format(label, style.paint(rc.verdict_line(r), "dim")))
            # Only ever populated when the subtree exceeds the whole quota
            # figure, which is a real puzzle rather than an expected difference.
            for c in r.candidates:
                out.append(style.paint("      possible cause (not asserted): " + c, "dim"))
            continue

        tone = "yellow" if r.verdict == rc.INCONCLUSIVE else "red"
        headline = "INCONCLUSIVE" if r.verdict == rc.INCONCLUSIVE else "UNEXPLAINED GAP"
        out.append(
            "  {}  {}  {}".format(
                label,
                style.paint(headline, tone),
                style.paint(
                    "{} accounted for vs quota {}, difference {}".format(
                        show(r.accounted), show(r.quota_value), show(r.gap)
                    ),
                    "dim",
                ),
            )
        )
        if r.deleted_value:
            out.append(
                style.paint(
                    "      ({} of that is unlinked-but-open)".format(show(r.deleted_value)), "dim"
                )
            )
        for b in r.blockers:
            out.append(style.paint("      cannot call this a finding: " + b, "dim"))
        for c in r.candidates:
            out.append(style.paint("      possible cause (not asserted): " + c, "dim"))
        for n in r.notes:
            out.append(style.paint("      " + n, "dim"))
    return out


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


def to_json(
    res: Optional[WalkResult],
    settle: Optional[SettleCheck],
    snap: Optional[QuotaSnapshot],
    scan: Optional[DeletedScan],
    recs: Optional[List[rc.Reconciliation]],
    top: int = 10,
) -> Dict[str, Any]:
    doc = {"tool": "rapidu"}  # type: Dict[str, Any]

    if snap is not None:
        doc["quota"] = {
            "source": snap.source,
            "available": snap.available,
            "reason": snap.reason or None,
            "snapshot_age_seconds": snap.age_seconds,
            "snapshot_taken_at": snap.taken_at,
            "rows": [
                {
                    "fileset": r.fileset,
                    "kind": r.kind,
                    "scope": r.scope,
                    "used": r.used,
                    "soft": r.soft,
                    "hard": r.hard,
                    "grace": r.grace or None,
                    "mount": r.mount,
                }
                for r in snap.rows
            ],
        }

    if res is not None:
        doc["walk"] = {
            "root": res.root,
            "size_bytes": res.size,
            "apparent_bytes": res.apparent,
            "allocation": {
                "ratio": res.alloc_ratio,
                "unit_bytes": res.alloc_unit,
                "padding_bytes": res.padding,
                "padded_files": res.padded_files,
                "under_allocated_files": res.under_files,
                "inline_files": res.inline_files,
                "material": allocation_is_material(res),
            },
            "files": res.files,
            "dirs": res.dirs,
            "inodes": res.inodes,
            "symlinks": res.symlinks,
            "hardlinked_inodes": res.hardlinked_inodes,
            "hardlink_extra_refs": res.hardlink_extra_refs,
            "elapsed_seconds": round(res.elapsed, 3),
            "threads": res.threads,
            "complete": res.complete,
            "unreadable_dirs": len(res.unreadable_dirs),
            "unstatable_entries": res.unstatable,
            "interrupted": res.partial,
            "by_uid": {_uname(u): {"bytes": b, "inodes": i} for u, (b, i) in res.by_uid.items()},
            "filesystems": len(res.by_dev),
            "top_by_size": [
                {"path": a.path, "bytes": a.size, "inodes": a.inodes}
                for a in res.top_dirs(top, "size")
            ],
            "top_by_inodes": [
                {"path": a.path, "bytes": a.size, "inodes": a.inodes}
                for a in res.top_dirs(top, "files")
            ],
            "top_by_density": [
                {
                    "path": a.path,
                    "bytes": a.size,
                    "inodes": a.inodes,
                    "files_per_gib": files_per_gib(a.size, a.inodes),
                }
                for a in res.top_dirs(top, "density")
            ],
        }

    if res is not None and settle is not None:
        doc["settling"] = {
            "window_seconds": res.settle_window,
            "recent_files": res.recent_files,
            "rechecked": settle.checked,
            "recheck_gap_seconds": settle.gap,
            "drift_bytes": settle.drift,  # signed: GPFS moves both ways
            "moved": settle.moved,
            # null when the check could not have seen drift, rather than a
            # reassuring false.
            "settled": (
                True if not res.recent_files else (not settle.moved if settle.conclusive else None)
            ),
            "conclusive": settle.conclusive,
            "sampled": settle.sampled,
        }

    if scan is not None:
        doc["deleted_but_open"] = {
            "available": scan.available,
            "reason": scan.reason or None,
            "total_bytes": scan.total_size,
            "inodes": len(scan.files),
            "scanned_pids": scan.scanned_pids,
            "unreadable_pids": scan.unreadable_pids,
            "complete": scan.complete,
            "node_local_only": True,
            "files": [
                {
                    "path": f.path,
                    "bytes": f.size,
                    "pids": f.pids,
                    "holders": [c for _, c in f.holders],
                }
                for f in scan.files[:top]
            ],
        }

    if recs:
        doc["reconciliation"] = [
            {
                "kind": r.kind,
                "verdict": r.verdict,
                "fileset": r.row.fileset if r.row else None,
                "scope": r.row.scope if r.row else None,
                "walked": r.walk_value,
                "deleted_but_open": r.deleted_value,
                "accounted": r.accounted,
                "quota": r.quota_value,
                "difference": r.gap,
                "tolerance": r.tolerance,
                "share_of_quota": r.share,
                "blockers": r.blockers,
                "candidates": r.candidates,
                "notes": r.notes,
            }
            for r in recs
        ]

    return doc
