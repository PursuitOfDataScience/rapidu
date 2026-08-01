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


def render_quota(snap: QuotaSnapshot, paths: Optional[List[str]] = None) -> List[str]:
    out = _h("QUOTA")
    if not snap.available:
        out.append("  n/a - {}".format(snap.reason or "no quota backend available"))
        return out

    age = snap.age_seconds
    if age is None:
        out.append(
            "  source {}   figures published without a timestamp; age UNKNOWN".format(snap.source)
        )
    else:
        out.append(
            "  source {}   figures are a snapshot {} old".format(snap.source, human_duration(age))
        )
        if age > rc.DEFAULT_MAX_SNAPSHOT_AGE_S:
            out.append(
                "  ! this number predates anything you did in the last {}.".format(
                    human_duration(age)
                )
            )

    rows = snap.rows
    if paths:
        keep = []
        for p in paths:
            for r in snap.rows_for_path(p):
                if r not in keep:
                    keep.append(r)
        if keep:
            rows = keep

    out.append("")
    # The filesystem column is not decoration. A fileset name is unique only
    # within one filesystem: on this site "rcc-staff" names four different
    # quotas and "rcc" two, and without the mount the rows are indistinguishable
    # from each other.
    out.append(
        "  {:<18}{:<8}{:<7}{:>12}{:>12}{:>8}  {}".format(
            "fileset", "type", "scope", "used", "soft limit", "use%", "filesystem"
        )
    )
    for r in rows:
        used = human_count(r.used) if r.kind == "files" else human_bytes(r.used)
        soft = (
            "n/a"
            if r.soft is None
            else (human_count(r.soft) if r.kind == "files" else human_bytes(r.soft))
        )
        # An unmapped mount prints as "?" rather than blank: the row is real and
        # its numbers are real, we just could not tie it to a path on this host.
        where = r.mount or "?"
        grace = ""
        if r.grace and r.grace.lower() not in ("none", "-", ""):
            # A running grace timer means the soft limit is already exceeded and
            # writes stop when it expires. That is the most urgent thing on the
            # line and it must not be hidden in a column nobody reads.
            grace = "  ! IN GRACE, {} left".format(r.grace)
        out.append(
            "  {:<18}{:<8}{:<7}{:>12}{:>12}{:>8}  {}{}".format(
                r.fileset[:17],
                r.kind,
                r.scope or "-",
                used,
                soft,
                pct(r.usage_fraction, 1.0) if r.usage_fraction is not None else "n/a",
                where,
                grace,
            )
        )

    unmapped = [r for r in rows if not r.mount]
    if unmapped:
        for note in snap.mapping_notes():
            out.append("  ? {}".format(note))
    return out


def render_compact(res: WalkResult, settle: SettleCheck, top: int, by_inodes: bool) -> List[str]:
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
    out = [
        "{}   {}   {} inodes   {:.2f}s".format(
            res.root, human_bytes(res.size), human_count(res.inodes), res.elapsed
        )
    ]

    warn = []  # type: List[str]
    if not res.complete:
        detail = []
        if res.unreadable_dirs:
            detail.append("{} dirs unreadable".format(len(res.unreadable_dirs)))
        if res.unstatable:
            detail.append("{} entries unstatable".format(res.unstatable))
        if res.partial:
            detail.append("interrupted")
        warn.append("! this is a FLOOR, not a total: {}".format(", ".join(detail)))
    if settle.moved:
        warn.append(
            "! still settling: re-stat {:.0f}s later found {} {} allocated".format(
                settle.gap,
                human_bytes(abs(settle.drift)),
                "more" if settle.drift > 0 else "less",
            )
        )
    out.extend(warn)

    ranked = res.top_dirs(top, "files" if by_inodes else "size")
    if ranked:
        out.append("")
        for a in ranked:
            out.append(
                "  {:>10}  {:>9} inodes  {}".format(
                    human_bytes(a.size), human_count(a.inodes), os.path.relpath(a.path, res.root)
                )
            )
    return out


def render_walk(
    res: WalkResult,
    settle: SettleCheck,
    top: int = 10,
    show_uids: bool = True,
    scan: Optional[DeletedScan] = None,
) -> List[str]:
    out = _h("WALK  {}".format(res.root))
    rate = res.inodes / res.elapsed if res.elapsed > 0 else 0.0
    out.append("  {:<22}{}".format("allocated size", human_bytes(res.size)))
    out.append(
        "  {:<22}{}  ({} files, {} dirs)".format(
            "inodes", human_count(res.inodes), human_count(res.files), human_count(res.dirs)
        )
    )
    out.append(
        "  {:<22}{:.2f}s at {} threads ({:,.0f} inodes/s)".format(
            "walked in", res.elapsed, res.threads, rate
        )
    )

    if res.apparent != res.size:
        out.append(
            "  {:<22}{}  (st_size; differs from allocated by {})".format(
                "apparent size", human_bytes(res.apparent), human_bytes(res.apparent - res.size)
            )
        )
    if res.hardlinked_inodes:
        out.append(
            "  {:<22}{} inodes, {} extra references deduped".format(
                "hard links",
                human_count(res.hardlinked_inodes),
                human_count(res.hardlink_extra_refs),
            )
        )

    if not res.complete:
        out.append("")
        out.append("  ! this total is a FLOOR, not a total:")
        if res.unreadable_dirs:
            out.append("      {} directories were unreadable".format(len(res.unreadable_dirs)))
            for path, why in res.unreadable_dirs[:3]:
                out.append("        {} ({})".format(path, why))
            if len(res.unreadable_dirs) > 3:
                out.append("        ... and {} more".format(len(res.unreadable_dirs) - 3))
        if res.unstatable:
            out.append("      {} entries could not be stat'ed".format(res.unstatable))
        if res.partial:
            out.append("      the walk was interrupted")

    if show_uids and len(res.by_uid) > 1:
        out.append("")
        out.append("  owners (a group quota charges all of these):")
        ranked = sorted(res.by_uid.items(), key=lambda kv: kv[1][0], reverse=True)
        for uid, (size, inodes) in ranked[:6]:
            out.append(
                "      {:<16}{:>12}  {:>12} inodes".format(
                    _uname(uid), human_bytes(size), human_count(inodes)
                )
            )

    out.extend(render_settle(res, settle))
    # Belongs with the other one-line facts about this tree, not after the
    # rankings. The full section only appears when something was found.
    if scan is not None:
        out.extend(render_deleted_oneline(scan))
    out.extend(render_top(res, top))
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
            "      ! re-stat {:.0f}s later found {} {} allocated: this tree is "
            "still moving.".format(settle.gap, human_bytes(abs(settle.drift)), direction)
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
    if top <= 0:
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


def render_reconcile(recs: List[rc.Reconciliation]) -> List[str]:
    out = _h("RECONCILIATION")
    for r in recs:
        label = "bytes" if r.kind == "blocks" else "inodes"
        out.append("  {}: {}".format(label, rc.verdict_line(r)))

        if r.verdict == rc.NOT_COMPARED:
            for n in r.notes:
                out.append("      {}".format(n))
            continue

        show = _counter if r.kind == "files" else human_bytes

        out.append("      {:<26}{:>14}".format("walked", show(r.walk_value)))
        if r.deleted_value:
            out.append("      {:<26}{:>14}".format("+ unlinked-but-open", show(r.deleted_value)))
        out.append("      {:<26}{:>14}".format("= accounted for", show(r.accounted)))
        out.append("      {:<26}{:>14}".format("quota says", show(r.quota_value)))
        if r.gap:
            out.append("      {:<26}{:>14}".format("difference", show(r.gap)))

        for n in r.notes:
            out.append("      note: {}".format(n))
        if r.blockers:
            # The same facts are caveats on a comparison that closed and
            # disqualifiers for one that did not.
            out.append(
                "      caveats:"
                if r.verdict == rc.CLOSES
                else "      cannot call this a finding because:"
            )
            for b in r.blockers:
                out.append("        - {}".format(b))
        if r.candidates:
            out.append("      candidate explanations (none of these is asserted):")
            for c in r.candidates:
                out.append("        - {}".format(c))
        out.append("")
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
