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
from typing import List, Optional, Tuple

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


# The floor may absorb noise; it may never absorb most of the comparison. A tenth
# is the line: five times the 2% fraction, so it never binds on a real quota, and
# small enough that a difference which is most of what was measured cannot hide
# under it.
_FLOOR_SHARE_OF_SCALE = 10


def _effective_tolerance(quota_value: int, accounted: int, kind: str) -> int:
    """:func:`_tolerance`, capped so it cannot swallow the measurement.

    The absolute floors exist to keep rounding noise on a small tree from reading
    as a discrepancy, and unbounded they did the opposite of what this module is
    for. With a quota row reporting 0 and a walk of 4.7 MiB, the 8 MiB floor made
    the verdict ``reconciles ... (within 8.0 MiB)`` -- a comparison that never
    happened, printed in green, which ``MIN_TOLERANCE_BYTES``' own comment names
    as the thing it must not do.

    So the floor is capped at a tenth of the larger operand. On any realistic
    quota the 2% fraction dominates and this changes nothing; on a small one the
    tolerance shrinks with what is being compared, which is the only way a
    difference of 100% of the measurement cannot be called agreement.
    """
    raw = _tolerance(quota_value, kind)
    scale = max(abs(quota_value), abs(accounted))
    if not scale:
        # Nothing on either side. There is no measurement to swallow, and a
        # zero-vs-zero comparison closes on the exact figures anyway.
        return raw
    return min(raw, max(scale // _FLOOR_SHARE_OF_SCALE, 1))


def _fileset_hint(path: str, mount: str) -> str:
    """The fileset ``path`` most likely belongs to: its first component below ``mount``.

    GPFS *independent filesets* are the standard way to give each lab its own
    quota inside one filesystem, and they all share one mount point. On this
    cluster ``/project`` carries three, and the convention -- near-universal
    because it is the only one that scales to a directory listing -- is that the
    fileset is the first path component beneath the mount: ``/project/dachxiu``
    is the ``dachxiu`` fileset.

    This is a *hint*, not an assertion. It is used only to break a tie between
    rows that all match the path equally well, and it loses to nothing: when it
    matches no row, the previous ordering stands.
    """
    target = os.path.abspath(path)
    stem = (mount or "").rstrip("/")
    if not stem or not target.startswith(stem + "/"):
        return ""
    return target[len(stem) + 1 :].split(os.sep)[0]


# Scope preference when several rows govern one path. A user row measures exactly
# the person asking; a project row measures the allocation a shared directory is
# charged against; a group or fileset row measures everybody. Narrowest first.
_SCOPE_RANK = {"user": 0, "project": 1, "fileset": 2, "group": 3}


def _pick_row(
    rows: List[QuotaRow], kind: str, path: str = ""
) -> Tuple[Optional[QuotaRow], List[str]]:
    """The row that governs ``path``, and any note about how it was chosen.

    ``rows_for_path`` returns *every* row tied for the longest matching mount.
    Taking ``matching[0]`` meant parse order decided, so a user whose own fileset
    was at 99.9% could be reconciled against a sibling lab's 31%-full one and
    told their tree was a rounding error. Ties are now broken by the fileset the
    path actually sits in, then by how narrowly the row is scoped, and a tie
    broken on anything less than the fileset name says so out loud.
    """
    matching = [r for r in rows if r.kind == kind]
    if not matching:
        return None, []
    if len(matching) == 1:
        return matching[0], []

    hint = ""
    for r in matching:
        hint = _fileset_hint(path, r.mount or "") or hint
        if hint:
            break
    named = [r for r in matching if hint and r.fileset.lower() == hint.lower()]
    pool = named or matching
    best = min(pool, key=lambda r: (_SCOPE_RANK.get(r.scope, 4), pool.index(r)))

    notes = []  # type: List[str]
    others = [r for r in matching if r is not best]
    if named:
        if len(named) > 1:
            notes.append(
                "{} {} rows govern {} equally; reconciled against the {}-scoped "
                "one because {} is the fileset this path sits in".format(
                    len(named), kind, path, best.scope or "un", best.fileset
                )
            )
    else:
        notes.append(
            "{} {} quota rows govern this path equally ({}); reconciled against "
            "'{}' because it is the most narrowly scoped, not because it is known "
            "to be the right one -- confirm with `mmlsattr --get-fileset` or "
            "`lfs project -d`".format(
                len(matching),
                kind,
                ", ".join(sorted({r.fileset for r in others} | {best.fileset})),
                best.fileset,
            )
        )
    return best, notes


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
    row, pick_notes = _pick_row(rows, kind, res.root)
    rec.notes.extend(pick_notes)
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

    # ---- can this walk answer this question at all? ----
    # A -c walk never calls stat, so it has no bytes and no per-file ownership.
    # Comparing its zeroes against a real quota figure produced an UNEXPLAINED
    # GAP the size of the entire quota -- a fabricated finding, which is the one
    # thing this module exists to prevent.
    if res.count_only:
        if kind == "blocks":
            rec.notes.append(
                "the walk was run with -c, which skips stat entirely, so it "
                "measured no bytes; there is nothing to compare against a block quota"
            )
            return rec
        if row.scope == "user":
            rec.notes.append(
                "the walk was run with -c, so it could not read who owns each "
                "file; a user-scoped file quota cannot be reconciled against it"
            )
            return rec

    # ---- what the walk saw, restricted to the same population as the quota ----
    # Both halves of `accounted` have to be narrowed to the quota's population,
    # not just the walk. Adding every unlinked-but-open inode on the node to a
    # uid-filtered walk figure compared two different populations and called the
    # remainder a gap -- and the /proc scan is precisely where another user's
    # bytes show up, because the motivating case is a shared group directory.
    my_uid = os.getuid()
    user_scoped = row.scope == "user"
    mine = deleted.owned_by(my_uid) if user_scoped else deleted.files
    if kind == "blocks":
        if user_scoped:
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
        rec.deleted_value = sum(f.size for f in mine)
    else:
        if user_scoped:
            rec.walk_value = res.by_uid.get(my_uid, (0, 0))[1]
            # The same sentence the blocks branch has always printed. Without it
            # the files comparison silently dropped every inode owned by someone
            # else and then reported the shortfall as a difference, with nothing on
            # screen to say the two sides counted different things.
            if len(res.by_uid) > 1:
                rec.notes.append(
                    "the quota row is user-scoped, so only the {} inodes you own "
                    "of the {} walked are compared".format(
                        human_count(rec.walk_value), human_count(res.inodes)
                    )
                )
        else:
            rec.walk_value = res.inodes
        rec.deleted_value = len(mine)
    if user_scoped and len(mine) != len(deleted.files):
        rec.notes.append(
            "{} of the {} unlinked-but-open inodes found are owned by other "
            "users and are excluded from this user-scoped comparison".format(
                len(deleted.files) - len(mine), len(deleted.files)
            )
        )

    rec.tolerance = _effective_tolerance(row.used, rec.accounted or 0, kind)
    rec.gap = row.used - (rec.accounted or 0)

    # ---- does the walk even cover the same tree the quota counts? ----
    root = os.path.abspath(res.root).rstrip("/")
    mounts = [m.rstrip("/") for m in (row.mounts or ([row.mount] if row.mount else []))]
    mount = next((m for m in mounts if root == m), "")
    covers_whole_tree = bool(mount)
    if not covers_whole_tree:
        mount = (row.mount or "").rstrip("/")
        rec.verdict = SUBTREE
        rec.notes.append(
            "the {} quota covers {} ({}-scoped); this walk covers only {}, so "
            "the difference is expected, not a discrepancy".format(
                row.fileset, mount or "an unknown mount", row.scope or "un", root
            )
        )
        # A subtree smaller than its quota needs no explanation: the rest of the
        # mount is the explanation, and the note above just said so. A subtree
        # *larger* than the whole quota figure is the interesting case -- it was
        # the one this audit actually hit, at 146.7% of the fileset figure -- and
        # there the candidates are worth having.
        if (rec.gap or 0) < 0:
            rec.candidates = _candidates(rec, res, deleted, kind)
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
            "recent writes or deletions{}".format(
                age, " -- though " + snap.time_note if snap.time_note else ""
            )
        )

    if kind == "blocks":
        if settle.moved:
            rec.blockers.append(
                "the tree has not settled: a re-stat {} found {} {} allocated "
                "than the walk read".format(
                    "{:.0f}s later".format(settle.gap) if settle.gap >= 1 else "after the walk",
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

    if res.count_only:
        # Only the fileset/group-scoped files comparison reaches here. -c counts
        # names, and a quota counts inodes; the two differ by however many hard
        # links the tree holds, which -c cannot know.
        rec.blockers.append(
            "the walk was run with -c, which counts one entry per name; a hard-linked "
            "file is one inode to the quota and several names to this count"
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
        rec.candidates = _candidates(rec, res, deleted, kind)
        return rec

    rec.verdict = GAP
    rec.candidates = _candidates(rec, res, deleted, kind)
    return rec


def _candidates(
    rec: Reconciliation, res: "walkmod.WalkResult", deleted: DeletedScan, kind: str
) -> List[str]:
    """Things that could explain a gap. Listed, never asserted.

    **Reached from every verdict that has a gap to explain, not only ``GAP``.**
    This list is the module's entire explanatory payload, and gating it behind
    the strictest verdict made it almost unreachable in practice. Getting to
    ``GAP`` needs the walk root to *be* the mount root -- so any walk of a
    subdirectory, which is nearly every walk anyone runs, returned ``SUBTREE``
    first -- and then needs zero blockers, while on this cluster the quota
    snapshot alone is routinely half an hour old against a 300 s threshold, so
    ``INCONCLUSIVE`` fires on essentially every run.

    The verdict machinery is right: a stale quota genuinely cannot support a
    *finding*. But snapshots, replication, other nodes' file descriptors and
    group-owned files are worth *mentioning* whether or not the arithmetic
    closes. They are hypotheses, and the module already labels them as such --
    "possible cause (not asserted)". Withholding a hypothesis because the
    evidence is not conclusive is what the blockers list is for; it is not a
    reason to withhold the hypothesis as well.
    """
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
