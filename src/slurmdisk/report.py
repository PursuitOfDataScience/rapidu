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


def _h(title: str) -> List[str]:
    return ["", title, _RULE]


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
    """The ranked table: size, proportional bar, share of tree, inodes, name.

    Sizes are cumulative subtree totals, so any row agrees with ``du -s`` on that
    path. Plain files appear alongside directories -- three 63 MiB ``.db`` files
    in a home directory are a quarter of it, and a directory-only listing cannot
    show them.

    The name comes last and is never truncated: it is the only variable-width
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

    # Scaled to the largest listed entry, not to the total: the bar exists to
    # discriminate between the rows on screen. The remainder row is deliberately
    # left barless -- it is an aggregate of things not shown, and giving it the
    # longest bar would make "everything else" look like the top offender.
    peak = max(metric(e) for e in ranked) or 1
    # A share needs a denominator. After an interrupt there is no tree total, so
    # the column is blanked rather than filled with a fraction of an accident.
    if res.partial:
        total = 0
    elif by_inodes or res.count_only:
        total = res.inodes or 1
    else:
        total = res.size or 1

    rows = [
        _entry_line(
            os.path.relpath(e.path, res.root) + ("/" if e.is_dir else ""),
            e.size,
            e.inodes,
            metric(e),
            peak,
            total,
            style,
            indent,
            is_dir=e.is_dir,
            size_hidden=res.count_only,
        )
        for e in ranked
    ]
    if show_rest:
        # Say how to see them. A truncated listing that does not tell you it is
        # truncated, or how to expand it, is just missing data.
        label = "({} more {} use -n 0 for all)".format(human_count(hidden), ui.dash(style))
        rows.append(
            _entry_line(
                label,
                rest_size,
                rest_inodes,
                rest_inodes if by_inodes else rest_size,
                None,
                total,
                style,
                indent,
                is_dir=False,
                aggregate=True,
                size_hidden=res.count_only,
            )
        )
    return rows


_BAR_W = 18


def _entry_line(
    name: str,
    size: int,
    inodes: int,
    value: int,
    peak: Optional[int],
    total: int,
    style: ui.Style,
    indent: str,
    is_dir: bool,
    aggregate: bool = False,
    size_hidden: bool = False,
) -> str:
    """``value`` is the ranked metric; it drives the bar, the share and the tone.

    The tone is a function of share-of-tree, so the colour of a row says how much
    it matters. Size and bar carry the same tone: whichever column the eye lands
    on gives the same answer.

    Files are *not* dimmed. A 63 MiB file that is 9% of a home directory is more
    worth looking at than a 3% directory, and greying it out said the opposite.
    Directories are marked by a trailing slash and a blue name, the way ``ls``
    does it -- shape, not emphasis.
    """
    # Colour is indexed by the same quantity the bar length encodes -- this row
    # against the largest row -- so the two always agree and the full ramp is
    # used on every listing. Share-of-total keeps its own column.
    rel = (value / float(peak)) if peak else 0.0
    tone = "dim" if aggregate else style.heat(rel)
    bar = " " * _BAR_W if peak is None else ui.bar(value / float(peak), _BAR_W, style, accent=tone)
    # In count mode there are no sizes at all, so the column is omitted rather
    # than left as ten blank characters the eye has to step over.
    size_cell = "" if size_hidden else style.paint(human_bytes(size).rjust(10), tone) + "  "
    name_style = []  # type: List[str]
    if aggregate:
        name_style = ["dim"]
    elif is_dir:
        name_style = ["bold_blue"]
    return "{}{}{}  {}  {}  {}".format(
        indent,
        size_cell,
        bar,
        style.paint(
            "{:>6}".format(pct(value, total) if total else ""), "dim" if aggregate else tone
        ),
        style.paint("{:>9}".format(human_count(inodes)), "dim"),
        style.paint(name, *name_style),
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
    return style.paint(indent + glyph * max(20, span), "dim")


def _entries_header(style: ui.Style, indent: str = "  ", size_label: str = "size") -> str:
    """Column labels.

    The count column is headed ``files``, not ``inodes``. "inode" is the correct
    term -- it is the on-disk structure a file or directory occupies, and it is
    the resource that runs out -- but the quota the reader is up against calls
    them files (``files (user) 21,553 / 300,000``), so using the quota's own word
    lets the two numbers be compared without a translation step. Directories are
    included in the count, exactly as the quota includes them.
    """
    head = "" if not size_label else style.paint("{:>10}".format(size_label), "dim") + "  "
    return "{}{}{}  {}  {}  {}".format(
        indent,
        head,
        " " * _BAR_W,
        style.paint("{:>6}".format("share"), "dim"),
        style.paint("{:>9}".format("files"), "dim"),
        style.paint("name", "dim"),
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


def _header(style: ui.Style, headline: str, path: str, subtitle: str) -> List[str]:
    """Lead with the answer, then the path, then the metadata.

    ``du -sh`` prints the size first and everyone reads it that way, so the size
    goes first and carries the visual weight. The path is the subject and gets
    bold. Counts, entry total and timing are context and are dimmed -- putting
    all four values at the same weight, as this used to, left nothing for the eye
    to land on.
    """
    return [
        "{}   {}".format(
            style.paint(headline.rjust(10), "bold_cyan"),
            style.paint(path, "bold"),
        ),
        style.paint("{}   {}".format(" " * 10, subtitle), "dim"),
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
            "counts only, no sizes  {sep}  {} entries  {sep}  {:.2f}s".format(
                human_count(_entry_total(res)), res.elapsed, sep=ui.sep(style)
            ),
        )
    elif res.partial:
        out = _header(
            style,
            human_bytes(res.size),
            res.root,
            "PARTIAL \u2014 {} files scanned before the interrupt".format(human_count(res.inodes)),
        )
    else:
        out = _header(
            style,
            human_bytes(res.size),
            res.root,
            "{} files  {sep}  {} entries  {sep}  {:.2f}s".format(
                human_count(res.inodes),
                human_count(_entry_total(res)),
                res.elapsed,
                sep=ui.sep(style),
            ),
        )
    out.extend(_hard_warnings(res, settle, style))
    body = render_entries(res, top, by_inodes, style)
    if body:
        out.append(_entries_rule(style, _entry_names(res, top, by_inodes)))
        out.append(_entries_header(style, size_label="" if res.count_only else "size"))
        out.extend(body)
    return out


def _hard_warnings(res: WalkResult, settle: SettleCheck, style: ui.Style) -> List[str]:
    """Only the things that change what the headline number means."""
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
    if settle.moved:
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
    """The walk block of the full report: headline, then facts worth a line each."""
    style = style or ui.resolve_style("never")
    out = [
        "",
        "{}  {}   {}   {}".format(
            ui.heading("WALK", style),
            style.paint(res.root, "bold"),
            style.paint(human_bytes(res.size), "bold_cyan"),
            style.paint(
                "{} files  ({} regular + {} directories)".format(
                    human_count(res.inodes),
                    human_count(res.files - res.hardlink_extra_refs),
                    human_count(res.dirs),
                ),
                "dim",
            ),
        ),
    ]

    facts = [
        "{:.2f}s at {} threads ({:,.0f} files/s)".format(
            res.elapsed, res.threads, res.inodes / res.elapsed if res.elapsed > 0 else 0.0
        ),
        "apparent {}".format(human_bytes(res.apparent)),
    ]
    if res.hardlinked_inodes:
        facts.append(
            "{} hard-linked files, {} extra names deduped".format(
                human_count(res.hardlinked_inodes), human_count(res.hardlink_extra_refs)
            )
        )
    if scan is not None and scan.available and not scan.files:
        facts.append(
            "no unlinked-but-open space ({} of {} pids inspectable)".format(
                scan.scanned_pids, scan.scanned_pids + scan.unreadable_pids
            )
        )
    out.append(style.paint("  " + "  ".join(facts), "dim"))

    out.extend(_hard_warnings(res, settle, style))
    if res.recent_files and not settle.moved:
        out.append(
            style.paint(
                "  {} file{} written in the last {} -- figure is provisional"
                " (--settle-wait 60 to measure)".format(
                    human_count(res.recent_files),
                    "" if res.recent_files == 1 else "s",
                    human_duration(res.settle_window),
                ),
                "dim",
            )
        )

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
        out.append(_entries_header(style, size_label="" if res.count_only else "size"))
        out.extend(body)
    return out


def render_footer() -> List[str]:
    """Kept for --about; deliberately not printed on every run."""
    return [
        "",
        _RULE,
        "slurmdisk agrees with `du -s --block-size=1` byte-for-byte on the same tree.",
        "It is not more accurate than du; it is faster, and it reports three things du",
        "cannot see: unsettled trees, unlinked-but-open space, and the age of the quota",
        "number it is compared against.",
    ]


def render_settle(res: WalkResult, settle: SettleCheck) -> List[str]:
    """The "this tree has not settled" warning -- a truth ``du`` cannot tell."""
    if not res.recent_files:
        return []

    # A handful of freshly written files in a large tree cannot move the
    # headline number, and spending five lines saying so trains the reader to
    # skip the section on the run where it matters. Compact unless it is either
    # measurably moving or big enough to move things.
    if not settle.moved and not _settling_is_material(res):
        return [
            "  {:<22}{} file{} written in the last {} -- figure is provisional"
            " (--settle-wait 60 to measure)".format(
                "settling",
                human_count(res.recent_files),
                "" if res.recent_files == 1 else "s",
                human_duration(res.settle_window),
            )
        ]

    out = ["", "  SETTLING"]
    out.append(
        "      {} file{} written in the last {}".format(
            human_count(res.recent_files),
            " was" if res.recent_files == 1 else "s were",
            human_duration(res.settle_window),
        )
    )

    if settle.moved:
        direction = "MORE" if settle.drift > 0 else "LESS"
        out.append(
            "      ! a re-stat {} found {} {} allocated: this tree is still moving.".format(
                "{:.0f}s later".format(settle.gap) if settle.gap >= 1 else "after the walk",
                human_bytes(abs(settle.drift)),
                direction,
            )
        )
        out.append("        Any size you read right now -- from this tool or from du --")
        out.append("        is provisional. Measured on GPFS, a freshly written tree has")
        out.append("        settled both upward (5.58x) and downward (3.3x) over ~60s.")
        if settle.sampled:
            out.append(
                "        (re-stat covered {} of {} recent files)".format(
                    human_count(settle.checked), human_count(settle.sampled_of)
                )
            )
    elif settle.conclusive:
        out.append(
            "      re-stat {:.0f}s later found no change in {} of them; "
            "the figure looks settled".format(settle.gap, human_count(settle.checked))
        )
    else:
        # A re-stat taken immediately cannot see an effect that takes tens of
        # seconds. Saying "looks settled" here would be a null result from a
        # blind instrument -- but four lines of caveat for a handful of files in
        # a large tree is noise, so the long form is reserved for the case where
        # the unsettled files could actually move the total.
        out.append("      figure is PROVISIONAL -- use --settle-wait 60 to measure the drift")
    if settle.gone:
        out.append(
            "      {} of them disappeared between the walk and the re-stat".format(settle.gone)
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


def render_top(res: WalkResult, top: int) -> List[str]:
    if top < 0:
        return []
    out = []  # type: List[str]
    by_size = res.top_dirs(top, "size")
    if by_size:
        out.extend(["", "  LARGEST SUBTREES"])
        for a in by_size:
            out.append(
                "      {:>10}  {:>10} inodes  {}".format(
                    human_bytes(a.size), human_count(a.inodes), os.path.relpath(a.path, res.root)
                )
            )

    # Ranked by inodes, with density as a *column* rather than its own ranking.
    #
    # There used to be a third "DENSEST" section sorted by files/GiB. It was
    # worse than useless: a ratio is won by the smallest denominator, so it
    # nominated a 260 KiB .git directory as the "best candidate to pack" ahead of
    # one holding ten times the inodes. Packing a directory reclaims the inodes
    # it holds, so the absolute count is the ranking and density only says how
    # cheap the tar will be.
    by_files = res.top_dirs(top, "files")
    if by_files and [a.path for a in by_files] != [a.path for a in by_size]:
        out.extend(["", "  MOST INODES  (density is what a tar would cost you)"])
        for a in by_files:
            d = files_per_gib(a.size, a.inodes)
            out.append(
                "      {:>10} inodes  {:>10}  {:>12}  {}".format(
                    human_count(a.inodes),
                    human_bytes(a.size),
                    "n/a" if d is None else "{:,.0f}/GiB".format(d),
                    os.path.relpath(a.path, res.root),
                )
            )
    return out


def render_deleted_oneline(scan: DeletedScan) -> List[str]:
    """One line for the combined report when the scan found nothing.

    The full section exists to describe space that was found. Printing seven
    lines of scope caveats to announce a null result buries the rest of the
    report, and "incomplete" is unconditionally true on a shared login node
    where every other user's processes are unreadable.
    """
    if not scan.available or scan.files:
        return []
    return [
        "  {:<22}none on this node ({} of {} processes inspectable)".format(
            "unlinked-but-open", scan.scanned_pids, scan.scanned_pids + scan.unreadable_pids
        )
    ]


def render_deleted(scan: DeletedScan, top: int = 10) -> List[str]:
    out = _h("UNLINKED BUT STILL OPEN")
    if not scan.available:
        out.append("  n/a - {}".format(scan.reason))
        return out
    if not scan.files:
        out.append(
            "  none found in {} inspectable processes on this node".format(scan.scanned_pids)
        )
    else:
        out.append(
            "  {} held by open file descriptors in {} inodes".format(
                human_bytes(scan.total_size), len(scan.files)
            )
        )
        out.append("  (invisible to du, to ls, and to this tool's own walk)")
        out.append("")
        for f in scan.files[:top]:
            holders = ", ".join(
                "{} {}".format(p, c.split()[0] if c else "?") for p, c in f.holders[:3]
            )
            out.append("      {:>10}  pid {}".format(human_bytes(f.size), holders))
            out.append("                  {}".format(f.path))
        if len(scan.files) > top:
            out.append("      ... and {} more".format(len(scan.files) - top))
    out.append("")
    out.append("  scope: this node only, {} processes inspected".format(scan.scanned_pids))
    if scan.unreadable_pids:
        out.append(
            "         {} processes belong to other users and cannot be inspected".format(
                scan.unreadable_pids
            )
        )
        out.append("         without root, so this figure is a floor.")
    out.append("         A job holding a deleted file on a compute node is not visible here.")
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
    doc = {"tool": "slurmdisk"}  # type: Dict[str, Any]

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
