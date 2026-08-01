"""Reconcile the live walk against the quota snapshot -- and know when not to.

The invariant this tool was originally specified around was::

    walk_total + deleted_but_open  ~=  quota_used      # when this fails, SAY SO

That is right in spirit and unsafe as written, because the third term is a
snapshot of unknown age. Measured locally, ``quota`` was 28 minutes stale and
did not move even while a 512 MiB file plainly existed. A tool that reconciles
against that number without checking its age will report a phantom gap and
accuse an innocent file descriptor.

So the rule here is Constraint 20: *a number with a timestamp on it is a number
with an age, and a discrepancy is not a finding until you can rule out that one
of the inputs is simply old.* Every input that could invalidate the comparison
downgrades the verdict to INCONCLUSIVE and names itself. A gap is reported as a
gap with candidate explanations listed -- never as an accusation.
"""

import os
from typing import List, Optional

from . import walk as walkmod
from .deleted import DeletedScan
from .fmt import human_bytes, human_count
from .quota import QuotaRow, QuotaSnapshot

# Comparison verdicts.
NOT_COMPARED = "not-compared"
SUBTREE = "subtree"
INCONCLUSIVE = "inconclusive"
CLOSES = "closes"
GAP = "gap"

# A quota snapshot older than this cannot support a finding about a live tree.
DEFAULT_MAX_SNAPSHOT_AGE_S = 300.0
# Slack on the comparison: quota accounting and a block walk legitimately differ
# a little (metadata blocks, replication, rounding). The absolute floors exist
# only to absorb that noise on a small tree -- they must stay well under any
# realistic quota, or they would swallow the measurement and manufacture a
# "reconciles" verdict for a comparison that never happened.
DEFAULT_TOLERANCE_FRACTION = 0.02
MIN_TOLERANCE_BYTES = 8 << 20
MIN_TOLERANCE_FILES = 100


class Reconciliation:
    """One comparison of one walked tree against one quota row."""

    def __init__(self, kind: str) -> None:
        self.kind = kind  # "blocks" | "files"
        self.verdict = NOT_COMPARED
        self.row = None  # type: Optional[QuotaRow]
        self.walk_value = None  # type: Optional[int]
        self.deleted_value = 0
        self.quota_value = None  # type: Optional[int]
        self.gap = None  # type: Optional[int]
        self.tolerance = 0
        self.blockers = []  # type: List[str]
        self.candidates = []  # type: List[str]
        self.notes = []  # type: List[str]

    @property
    def accounted(self) -> Optional[int]:
        """walk + deleted-but-open: everything we can actually see."""
        if self.walk_value is None:
            return None
        return self.walk_value + self.deleted_value

    @property
    def share(self) -> Optional[float]:
        """Walked subtree as a fraction of the quota figure."""
        acc = self.accounted
        if acc is None or not self.quota_value:
            return None
        return acc / float(self.quota_value)


def _tolerance(quota_value: int, kind: str) -> int:
    frac = int(abs(quota_value) * DEFAULT_TOLERANCE_FRACTION)
    if kind == "files":
        return max(frac, MIN_TOLERANCE_FILES)
    return max(frac, MIN_TOLERANCE_BYTES)


def _pick_row(rows: List[QuotaRow], kind: str) -> Optional[QuotaRow]:
    """Prefer a user-scoped row; a group row measures more than one person."""
    matching = [r for r in rows if r.kind == kind]
    if not matching:
        return None
    for r in matching:
        if r.scope == "user":
            return r
    return matching[0]


def reconcile(
    res: "walkmod.WalkResult",
    settle: "walkmod.SettleCheck",
    snap: QuotaSnapshot,
    deleted: DeletedScan,
    kind: str = "blocks",
    max_snapshot_age: float = DEFAULT_MAX_SNAPSHOT_AGE_S,
) -> Reconciliation:
    """Compare one walked tree against the quota row that governs it."""
    rec = Reconciliation(kind)

    if not snap.available:
        rec.notes.append(
            "no quota backend available ({}), so there is nothing to reconcile against".format(
                snap.reason or "unknown reason"
            )
        )
        return rec

    rows = snap.rows_for_path(res.root)
    row = _pick_row(rows, kind)
    if row is None:
        rec.notes.append(
            "no {} quota row maps to {} -- the backend published no mount point "
            "matching this path, so the tree is reported on its own".format(kind, res.root)
        )
        for note in snap.mapping_notes():
            rec.notes.append(note)
        return rec

    rec.row = row
    rec.quota_value = row.used

    # ---- what the walk saw, restricted to the same population as the quota ----
    my_uid = os.getuid()
    if kind == "blocks":
        if row.scope == "user":
            rec.walk_value = res.by_uid.get(my_uid, (0, 0))[0]
            if len(res.by_uid) > 1:
                rec.notes.append(
                    "the quota row is user-scoped, so only the {} you own of the "
                    "{} walked is compared".format(
                        human_bytes(rec.walk_value), human_bytes(res.size)
                    )
                )
        else:
            rec.walk_value = res.size
        rec.deleted_value = deleted.total_size
    else:
        if row.scope == "user":
            rec.walk_value = res.by_uid.get(my_uid, (0, 0))[1]
        else:
            rec.walk_value = res.inodes
        rec.deleted_value = len(deleted.files)

    rec.tolerance = _tolerance(row.used, kind)
    rec.gap = row.used - (rec.accounted or 0)

    # ---- does the walk even cover the same tree the quota counts? ----
    mount = (row.mount or "").rstrip("/")
    root = os.path.abspath(res.root).rstrip("/")
    covers_whole_tree = bool(mount) and root == mount
    if not covers_whole_tree:
        rec.verdict = SUBTREE
        rec.notes.append(
            "the {} quota covers {} ({}-scoped); this walk covers only {}, so "
            "the difference is expected, not a discrepancy".format(
                row.fileset, mount or "an unknown mount", row.scope or "un", root
            )
        )
        return rec

    # ---- anything that makes the comparison unsafe, before any verdict ----
    age = snap.age_seconds
    if age is None:
        rec.blockers.append(
            "the quota backend published no timestamp, so the age of its figure is unknown"
        )
    elif age > max_snapshot_age:
        rec.blockers.append(
            "the quota figure is a snapshot taken {:.0f}s ago and may predate "
            "recent writes or deletions".format(age)
        )

    if kind == "blocks":
        if settle.moved:
            rec.blockers.append(
                "the tree has not settled: a re-stat {:.0f}s later found {} "
                "{} allocated than the walk read".format(
                    settle.gap,
                    human_bytes(abs(settle.drift)),
                    "more" if settle.drift > 0 else "less",
                )
            )
        elif res.recent_files:
            rec.blockers.append(
                "{} files were modified within the last {:.0f}s and their blocks "
                "may not be final{}".format(
                    res.recent_files,
                    res.settle_window,
                    ""
                    if settle.conclusive
                    else " (the re-stat was immediate, so it could not have seen "
                    "drift; use --settle-wait)",
                )
            )

    if res.unreadable_dirs:
        rec.blockers.append(
            "{} directories could not be read, so the walk total is a floor, not a total".format(
                len(res.unreadable_dirs)
            )
        )
    if res.unstatable:
        rec.blockers.append("{} entries could not be stat'ed".format(res.unstatable))
    if res.partial:
        rec.blockers.append("the walk was interrupted before it finished")

    if len(res.by_dev) > 1:
        rec.blockers.append(
            "the walk crossed {} filesystems but the quota governs one; re-run "
            "with --one-file-system to compare like with like".format(len(res.by_dev))
        )

    # ---- verdict ----
    if rec.gap is not None and abs(rec.gap) <= rec.tolerance:
        rec.verdict = CLOSES
        return rec

    if rec.blockers:
        rec.verdict = INCONCLUSIVE
        return rec

    rec.verdict = GAP
    rec.candidates = _candidates(rec, res, deleted, kind)
    return rec


def _candidates(
    rec: Reconciliation, res: "walkmod.WalkResult", deleted: DeletedScan, kind: str
) -> List[str]:
    """Things that could explain a gap. Listed, never asserted."""
    out = []  # type: List[str]
    if rec.gap is None:
        return out
    if rec.gap > 0:
        # Quota says more than we can see.
        out.append(
            "unlinked-but-open files held by processes on other nodes -- this "
            "scan only sees this node"
        )
        if not deleted.complete:
            out.append(
                "unlinked-but-open files held by {} processes belonging to other "
                "users, which an unprivileged scan cannot inspect".format(deleted.unreadable_pids)
            )
        if rec.row is not None and rec.row.scope != "user":
            out.append(
                "files under this tree owned by other members of the '{}' group".format(
                    rec.row.fileset
                )
            )
        out.append("filesystem snapshots, if this fileset is snapshotted")
        if kind == "blocks":
            out.append(
                "quota accounting that differs from a block walk (replication "
                "factor, metadata blocks, or a different block size)"
            )
    else:
        # We can see more than the quota admits.
        out.append("a quota figure computed before the most recent writes landed")
        if kind == "blocks":
            out.append(
                "sparse or not-yet-allocated blocks counted differently by the "
                "quota manager than by st_blocks"
            )
    return out


def verdict_line(rec: Reconciliation) -> str:
    """One-line human summary of a reconciliation."""
    if rec.verdict == NOT_COMPARED:
        return "not compared"
    if rec.verdict == SUBTREE:
        share = rec.share
        if share is None:
            return "subtree of a larger quota'd tree"
        return "this subtree is {:.1f}% of the {} quota figure".format(
            100.0 * share, rec.row.fileset if rec.row else "?"
        )
    if rec.verdict == CLOSES:
        tol = human_count(rec.tolerance) if rec.kind == "files" else human_bytes(rec.tolerance)
        return "reconciles (difference is within {})".format(tol)
    if rec.verdict == INCONCLUSIVE:
        return "INCONCLUSIVE -- {}".format(rec.blockers[0] if rec.blockers else "unknown")
    return "UNEXPLAINED GAP"
