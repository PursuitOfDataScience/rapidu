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
import shutil
from typing import List, Optional, Tuple

from . import quota as quotamod
from . import walk as walkmod
from .deleted import DeletedScan
from .fmt import human_bytes, human_count, plural
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
        # True when the walk this was computed from did not finish reading its own
        # tree, so ``walk_value`` -- and everything derived from it, ``accounted``
        # and ``share`` -- is a floor. Every verdict below turns that into a
        # blocker; :data:`SUBTREE` returns before the blocker list is built and
        # prints a percentage anyway, so it reads the flag itself.
        self.walk_is_floor = False

    @property
    def within_tolerance(self) -> bool:
        """Do the two figures agree, ignoring whether the comparison was sound?

        A property because two places need the same test and they disagreed about
        what to do with it: the verdict treated it as sufficient for ``CLOSES``
        and :func:`verdict_line` had no way to say "they agree, and that is not
        evidence".
        """
        return self.gap is not None and abs(self.gap) <= self.tolerance

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


def _floor_phrase(res: "walkmod.WalkResult") -> str:
    """Why the walked total is a floor, in the fewest words that name the causes.

    :func:`reconcile`'s blocker list says all of this at length, split by cause
    and by remedy. This is the short form, for the one branch that returns before
    that list is built.
    """
    parts = []  # type: List[str]
    if res.unreadable_dir_count:
        parts.append(
            "{} could not be read".format(
                plural(res.unreadable_dir_count, "directory", irregular="directories")
            )
        )
    if res.unstatable:
        parts.append(
            "{} could not be stat'ed".format(plural(res.unstatable, "entry", irregular="entries"))
        )
    if res.partial:
        parts.append("it was interrupted before it finished")
    return "; ".join(parts)


def _changed_phrase(res: "walkmod.WalkResult") -> str:
    """What changed inside the settle window, without claiming which kind."""
    parts = []  # type: List[str]
    if res.recent_files:
        parts.append(
            "{} {} written".format(
                plural(res.recent_files, "file"), "was" if res.recent_files == 1 else "were"
            )
        )
    if res.touched_files:
        parts.append("{} changed without being written".format(plural(res.touched_files, "inode")))
    return " and ".join(parts)


# How to confirm which fileset a path belongs to, per filesystem. `mmlsattr -L`
# prints the fileset name on GPFS; `lfs project -d` prints the project id on
# Lustre. The flag matters: GPFS documents `-L` for this, and the note used to
# suggest `mmlsattr --get-fileset`, which is not among mmlsattr's options -- so a
# reader who pasted it got a usage error rather than an answer. Unverified here
# (no mm* command is installed on either cluster in this campaign), so it is
# stated from the GPFS CLI reference rather than measured.
_FILESET_PROBES = (("mmlsattr", "mmlsattr -L"), ("lfs", "lfs project -d"))


def _fileset_probe_hint() -> str:
    """How to confirm the fileset, naming only tools that exist on this host.

    RD-12's rule, applied outside the reclaim table: *nothing is printed as a
    command unless it was checked against this host.* This note exists to tell the
    reader how to resolve an ambiguity the tool could not, so suggesting two
    commands that both answer `command not found` is the one thing it must not do
    -- and on any site that is neither GPFS nor Lustre, both were exactly that.

    Returns ``""`` where neither is available, and the caller then says the
    ambiguity stands rather than pointing at nothing.
    """
    usable = [form for tool, form in _FILESET_PROBES if shutil.which(tool)]
    if not usable:
        return ""
    return " -- confirm with {}".format(" or ".join("`{}`".format(u) for u in usable))


def _row_gid(row: QuotaRow) -> Optional[int]:
    """The gid a group-scoped row is charged to, if it can be resolved.

    A group quota is charged **by gid**, and reconcile compared group rows against
    the *whole* walked tree. The two differ exactly where it matters, which
    `walk.WalkResult.by_gid` was added to capture: a file written into a shared
    project directory whose setgid bit is missing lands in the writer's personal
    group, so those bytes are charged somewhere nobody is looking -- while the
    comparison counted them toward the project group. Measured on a synthetic
    half-and-half tree: a row charged 400 GiB was compared against 800 GiB and the
    -400 GiB difference was reported as a gap, blamed on a stale quota figure and
    on sparse blocks.

    The row's fileset column carries the group name for the site-wrapper backends
    (`rcc-staff`, `labgroup`). Where it does not resolve to a real group -- an
    `mmlsquota` row is named after its filesystem, and a name service can simply
    be down -- this returns ``None`` and the caller keeps the whole-tree
    comparison it has always made, saying that it could not narrow. Guessing a gid
    would be worse than not narrowing.
    """
    if row.scope != "group":
        return None
    try:
        import grp

        return grp.getgrnam(row.fileset).gr_gid
    except (ImportError, KeyError, TypeError):
        return None


def _others_own_some(res: "walkmod.WalkResult", my_uid: int) -> bool:
    """Does this walk contain anything owned by somebody other than ``my_uid``?

    Not ``len(res.by_uid) > 1``, which was the old test and which is false in the
    one case that matters most: a tree where a single *other* person owns
    everything. Then the user-scoped comparison silently measured zero of theirs
    and reported the whole quota as a gap.
    """
    return any(uid != my_uid for uid in res.by_uid)


def _inferred_mount_note(row: QuotaRow) -> str:
    """Why an inferred mapping cannot support a finding.

    ``QuotaRow.guessed`` says the backend never published a mount point for this
    row and rapidu worked one out -- from the filesystem name in a section header,
    or in the worst case from the fileset label. That is a mapping, not a
    measurement, and an unexplained gap computed across a wrong mapping is a
    fabricated finding of exactly the kind this module exists to prevent: on a
    cluster where three filesystems live under one ``/scratch``, the wrong guess
    reconciles one cluster's tree against another cluster's quota and the
    arithmetic looks perfectly sound.

    So it is stated as a blocker, which downgrades ``GAP`` to ``INCONCLUSIVE``
    while leaving the candidate causes visible. A comparison that *closes*
    survives -- the numbers agreeing is itself evidence the mapping was right --
    and carries this as a caveat instead.
    """
    return (
        "the mount point for the {} quota row was inferred from its name rather "
        "than published by the backend, so this comparison may be against a "
        "different filesystem's quota".format(row.fileset)
    )


# Scope preference when several rows govern one path. A user row measures exactly
# the person asking; a project row measures the allocation a shared directory is
# charged against; a group or fileset row measures everybody. Narrowest first.
_SCOPE_RANK = {"user": 0, "project": 1, "fileset": 2, "group": 3}


def _pick_row(
    rows: List[QuotaRow],
    kind: str,
    path: str = "",
    probe_timeout: float = quotamod.DEFAULT_TIMEOUT_S,
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

    # Asked first, inferred second.
    #
    # The note this used to print told the reader to run `mmlsattr -L` to settle
    # it. That was the right advice and the wrong division of labour: it is one
    # unprivileged call per walked path, it answers exactly the question, and it
    # matches `mmlsquota`'s own `filesetName` field exactly. Where it answers,
    # the tie is not broken -- there is no tie.
    # `probe_timeout` rather than the 45 s default: every other subprocess in
    # `quota` is bounded by the caller's budget (`--quota-timeout`), and this one
    # is on the same GPFS that a hung `mmlsquota` was already given a deadline
    # for. `read_path_fileset` memoizes per path, so this runs at most once for a
    # path however many kinds ask.
    measured = quotamod.read_path_fileset(path, probe_timeout) if path else None
    hint = ""
    if not measured:
        for r in matching:
            hint = _fileset_hint(path, r.mount or "") or hint
            if hint:
                break
    key = measured or hint
    named = [r for r in matching if key and r.fileset.lower() == key.lower()]
    pool = named or matching
    best = min(pool, key=lambda r: (_SCOPE_RANK.get(r.scope, 4), pool.index(r)))

    notes = []  # type: List[str]
    others = [r for r in matching if r is not best]
    if named:
        if measured:
            # Stated in the note, NOT written back onto the row.
            #
            # This used to do `best.guessed = False`, and two things were wrong
            # with that. It mutates a `QuotaRow` that is shared: the same objects
            # are handed to both the blocks and the files `reconcile()` call, and
            # to every path in the `rdu -a p1 p2` loop, so one confirmed path
            # permanently suppressed the inferred-mount blocker, the
            # ", mount inferred from its name" text and `to_json`'s
            # `mount_guessed` for every later consumer of that row -- including
            # paths `mmlsattr` was never asked about.
            #
            # And it is wrong on the merits even for this path. `guessed`
            # documents one thing: whether the BACKEND published this row's
            # mount or rapidu inferred it from the fileset's name. `mmlsattr -L`
            # confirms which fileset a PATH is in. That settles the tie, which is
            # what it is used for here, and says nothing about where the
            # filesystem is mounted -- so it cannot retire the caveat about the
            # mount. The measured fact travels in the note instead, where it is
            # scoped to the path it was measured on.
            if len(named) > 1 or len(matching) > 1:
                notes.append(
                    "{} of {} {} rows kept: `mmlsattr -L` reports {} is in fileset '{}'".format(
                        len(named), len(matching), kind, path, best.fileset
                    )
                )
        elif len(named) > 1:
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
            "to be the right one{}".format(
                len(matching),
                kind,
                # `label`, not `fileset`. This sentence exists to show that
                # several rows govern the path, and a fileset name is unique
                # inside a filesystem but not across one -- so two `scratch`
                # rows on different devices rendered as "2 rows govern this path
                # equally (scratch)", a set of two collapsing to one word in the
                # one message whose whole job is to distinguish them. RD-18
                # added `label` for exactly this and the notes did not use it.
                ", ".join(sorted({r.label for r in others} | {best.label})),
                best.label,
                _fileset_probe_hint(),
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
    probe_timeout: float = quotamod.DEFAULT_TIMEOUT_S,
) -> Reconciliation:
    """Compare one walked tree against the quota row that governs it.

    ``probe_timeout`` bounds the one subprocess this module can start -- the
    `mmlsattr -L` fileset probe in :func:`_pick_row` -- and is the caller's
    ``--quota-timeout``, so it is the same budget the backend query ran under.
    """
    rec = Reconciliation(kind)
    # Recorded before any branch can return, because one of them returns before
    # the blocker list that normally carries this.
    rec.walk_is_floor = not res.complete

    if not snap.available:
        # The backend's own explanation is not repeated here. It is on the
        # snapshot the caller passed in, the QUOTA panel prints it once, and the
        # JSON document carries it under `quota.reason` -- while this note is
        # emitted once per kind, so interpolating a three-line GPFS failure made
        # it the longest thing in the report and said nothing new the second time.
        rec.notes.append(
            "no quota backend available, so there is nothing to reconcile "
            "against -- see QUOTA for why"
        )
        return rec

    rows = snap.rows_for_path(res.root)
    row, pick_notes = _pick_row(rows, kind, res.root, probe_timeout)
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
    row_gid = _row_gid(row)
    if user_scoped:
        mine = deleted.owned_by(my_uid)
    elif row_gid is not None:
        # Both halves narrowed to the same population, which is the rule this
        # block opens with: narrowing the walk by gid while adding every unlinked
        # inode regardless would put two different populations on the two sides of
        # one sum.
        mine = deleted.owned_by_gid(row_gid)
    else:
        mine = deleted.files
    if kind == "blocks":
        if row_gid is not None:
            rec.walk_value = res.by_gid.get(row_gid, (0, 0))[0]
            if rec.walk_value != res.size:
                rec.notes.append(
                    "the quota row is charged to the '{}' group, so only the {} of "
                    "the {} walked that is charged to it is compared -- the rest "
                    "belongs to other groups (a missing setgid bit is the usual "
                    "reason)".format(
                        row.fileset, human_bytes(rec.walk_value), human_bytes(res.size)
                    )
                )
        elif user_scoped:
            rec.walk_value = res.by_uid.get(my_uid, (0, 0))[0]
            if _others_own_some(res, my_uid):
                rec.notes.append(
                    "the quota row is user-scoped, so only the {} you own of the "
                    "{} walked is compared".format(
                        human_bytes(rec.walk_value), human_bytes(res.size)
                    )
                )
        else:
            rec.walk_value = res.size
            if row.scope == "group":
                rec.notes.append(
                    "the whole tree is compared against a group row whose gid could "
                    "not be resolved from '{}', so bytes charged to another group "
                    "are included".format(row.fileset)
                )
        rec.deleted_value = sum(f.size for f in mine)
    else:
        if row_gid is not None:
            rec.walk_value = res.by_gid.get(row_gid, (0, 0))[1]
            if rec.walk_value != res.inodes:
                rec.notes.append(
                    # `plural` and agreeing verbs: this read "only the 1 inodes
                    # of the 13 walked that are charged to it are compared".
                    "the quota row is charged to the '{}' group, so only the {} "
                    "of the {} walked that {} charged to it {} compared".format(
                        row.fileset,
                        plural(rec.walk_value, "inode"),
                        human_count(res.inodes),
                        "is" if rec.walk_value == 1 else "are",
                        "is" if rec.walk_value == 1 else "are",
                    )
                )
        elif user_scoped:
            rec.walk_value = res.by_uid.get(my_uid, (0, 0))[1]
            # The same sentence the blocks branch has always printed. Without it
            # the files comparison silently dropped every inode owned by someone
            # else and then reported the shortfall as a difference, with nothing on
            # screen to say the two sides counted different things.
            if _others_own_some(res, my_uid):
                rec.notes.append(
                    "the quota row is user-scoped, so only the {} you own "
                    "of the {} walked {} compared".format(
                        plural(rec.walk_value, "inode"),
                        human_count(res.inodes),
                        "is" if rec.walk_value == 1 else "are",
                    )
                )
        else:
            rec.walk_value = res.inodes
        rec.deleted_value = len(mine)
    if user_scoped and len(mine) != len(deleted.files):
        others = len(deleted.files) - len(mine)
        rec.notes.append(
            # Both verbs and the noun agree with their counts. The guard above is
            # `!=`, so `others` is routinely 1 -- one other user's descriptor on a
            # shared node -- and this read "1 of the 1 unlinked-but-open inodes
            # found are owned by other users and are excluded".
            "{} of the {} found {} owned by other users and {} excluded from this "
            "user-scoped comparison".format(
                others,
                plural(len(deleted.files), "unlinked-but-open inode"),
                "is" if others == 1 else "are",
                "is" if others == 1 else "are",
            )
        )

    narrowed = user_scoped or row_gid is not None
    if narrowed and not rec.accounted and (res.size or res.inodes):
        # The walk found content and none of it is in the population this row
        # counts. That is not a small difference; it is not a difference at all.
        # Comparing a user quota against zero bytes of that user produced
        # "UNEXPLAINED GAP -- 0 B accounted for vs quota 800 GiB", with candidate
        # causes about unlinked files and snapshots, for a tree whose whole
        # explanation is that a colleague owns it -- and auditing someone else's
        # directory, or a shared tree populated by others, is routine on a cluster.
        # The same applies to a group row once `_row_gid` narrows it: a tree none
        # of which is charged to that group says nothing about that group's quota.
        #
        # Two things this must *not* swallow. A genuinely empty tree still
        # compares, because "my quota says 800 GiB and this mount holds none of my
        # files" is a real finding. And the test is bytes *or* inodes, because a
        # tree of empty files owned by others has no bytes and plenty of inodes.
        rec.notes.append(
            "none of the {} walked is {}, so the walk measured nothing this quota "
            "row counts -- there is no comparison to make".format(
                human_bytes(res.size) if kind == "blocks" else human_count(res.inodes),
                "owned by you (this row is user-scoped)"
                if user_scoped
                else "charged to the '{}' group".format(row.fileset),
            )
        )
        return rec

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
        # The inferred-mount caveat rides in this note rather than adding a
        # second one. It has to be said -- a subtree of the *wrong* mount is not
        # a subtree of anything -- but this section already prints one note per
        # kind, and a three-line sentence repeated for bytes and for files is how
        # a caveat stops being read.
        how = "{}-scoped{}".format(
            row.scope or "un", ", mount inferred from its name" if row.guessed else ""
        )
        rec.notes.append(
            "the {} quota covers {} ({}); this walk covers only {}, so "
            "the difference is expected, not a discrepancy".format(
                row.fileset, mount or "an unknown mount", how, root
            )
        )
        # ...and the walk may also be short of the *subtree*, which the note above
        # does not cover and this branch returns before the blocker list can.
        #
        # So a walk that was interrupted, or that could not read 11,267 of its own
        # directories, reached `verdict_line` as "this subtree is 25.0% of the fs
        # quota figure" -- a percentage whose numerator is a floor, printed as a
        # measurement, with `blockers` empty. And SUBTREE is by far the most
        # common verdict on a real cluster (`_candidates` says why: reaching GAP
        # needs the walk root to *be* the mount root), so this was the usual way
        # the number was read.
        #
        # Not a blocker and not a downgrade: SUBTREE is already "the difference is
        # expected", not a finding, and there is nothing to withhold. It is the
        # figure itself that has to say what it is -- the same rule the walk panel
        # follows with "a floor, not a total".
        if rec.walk_is_floor:
            # No figure interpolated beside the verbs. The walked total is already
            # on the line above and in the walk panel, and pairing it with "is a
            # floor" here made this the one message in `src/` that
            # `test_no_message_pairs_a_count_with_a_fixed_verb` catches -- "the
            # 11,267 it measured is a floor" for the files kind.
            rec.notes.append(
                "the walk did not finish reading even that subtree ({}), so its "
                "total counts as a floor and the percentage above as a lower "
                "bound, not a measurement".format(_floor_phrase(res))
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
    else:
        # The cap is about the distance between the two figures being compared,
        # and `age` is only half of it. `QuotaSnapshot.age_seconds` deliberately
        # measures how stale the figures were *when they were read*, and
        # `cli.cmd_walk` reads them before it starts the walk -- so by the time
        # the walk has produced the other half of this comparison, the two
        # measurements are `age + res.elapsed` apart.
        #
        # Testing `age` alone made the gate blind to the walk it was gating. A
        # backend that computes on demand has `taken_at == read_at`, so `age` is
        # 0.0; add a half-hour walk and the blocker list came back empty and the
        # verdict was a confident `gap` between two numbers measured thirty
        # minutes apart, with nothing on the report saying so.
        #
        # The sum, and not the larger of the two, because they compose: the
        # figure was already `age` seconds behind reality when it was read, and
        # then reality moved for another `res.elapsed` while the walk ran.
        # Anything written or deleted anywhere in that combined window lands in
        # one measurement and not the other, which is exactly what this blocker
        # is about. It is the separation at the *end* of the walk -- the widest
        # of it, since the first entry the walk stat'ed was only `age` seconds
        # off -- and the widest is the right one for a gate whose job is to
        # refuse, not to reassure. It is still a lower bound on the true gap:
        # the settle re-stat and the /proc sweep happen later again.
        age_at_walk_end = age + res.elapsed
        if age_at_walk_end > max_snapshot_age:
            rec.blockers.append(
                # Not "{}s ago": the reader is being told how far apart the two
                # sides of the comparison are, and "ago" understated that by the
                # whole duration of the walk. The split is spelled out whenever
                # the walk contributed a measurable part of it, because "2200s"
                # against a backend that reported a fresh figure is otherwise
                # unattributable.
                "the quota figure is a snapshot taken {:.0f}s before this walk "
                "finished{} and may predate recent writes or deletions{}".format(
                    age_at_walk_end,
                    ""
                    if res.elapsed < 1
                    else " ({:.0f}s stale when read, then a {:.0f}s walk)".format(age, res.elapsed),
                    " -- though " + snap.time_note if snap.time_note else "",
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
        elif res.recent_files or res.touched_files:
            rec.blockers.append(
                "{} within the last {:.0f}s, so {} blocks may not be final{}".format(
                    # "written" is only true of the mtime half. A `chmod -R` or a
                    # `chgrp` bumps ctime on every file in a tree, and asserting a
                    # write about those made this blocker state something false --
                    # while still being right to fire, since a delayed allocation
                    # completing looks identical from a stat.
                    _changed_phrase(res),
                    res.settle_window,
                    "its" if (res.recent_files + res.touched_files) == 1 else "their",
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

    if res.unreadable_dir_count:
        # Two causes, two remedies. "could not be read" points at permissions; a
        # directory deleted between being listed and being opened points at
        # something writing to the tree, and the answer there is to re-run when it
        # is idle, not to chase access. Both still make the total a floor.
        #
        # The exact count, not the capped path sample beside it.
        refused = res.unreadable_dir_count - res.vanished_dirs
        if refused > 0:
            rec.blockers.append(
                "{} could not be read, so the walk total is a floor, not a total".format(
                    plural(refused, "directory", irregular="directories")
                )
            )
        if res.vanished_dirs:
            rec.blockers.append(
                "{} vanished between being listed and being walked -- the tree was "
                "changing underneath, so the total is a floor and a moving "
                "one".format(plural(res.vanished_dirs, "directory", irregular="directories"))
            )
    if res.unstatable:
        # Same split as the directories above, for the same reason: "could not be
        # stat'ed" reads as a permission problem, and an entry unlinked while the
        # walk was reading its directory is not one.
        unreachable = res.unstatable - res.vanished_entries
        if unreachable > 0:
            rec.blockers.append(
                "{} could not be stat'ed".format(plural(unreachable, "entry", irregular="entries"))
            )
        if res.vanished_entries:
            rec.blockers.append(
                "{} vanished before {} could be stat'ed -- the tree was changing underneath".format(
                    plural(res.vanished_entries, "entry", irregular="entries"),
                    # The pronoun has to agree as well as the noun.
                    # `render_allocation` already does this -- "it" for one file,
                    # "they" otherwise -- and writing a new message without reusing
                    # the idiom produced "1 entry vanished before they could be
                    # stat'ed". Found by auditing this session's own additions
                    # against the rule the session had been enforcing.
                    "it" if res.vanished_entries == 1 else "they",
                )
            )
    if res.partial:
        rec.blockers.append("the walk was interrupted before it finished")

    if len(res.by_dev) > 1:
        rec.blockers.append(
            "the walk crossed {} filesystems but the quota governs one; re-run "
            "with --one-file-system to compare like with like".format(len(res.by_dev))
        )

    if row.guessed:
        rec.blockers.append(_inferred_mount_note(row))

    if snap.figure_note:
        # The backend disowned its own numbers. Comparing a walk against a figure
        # whose publisher says it is inaccurate cannot produce a finding -- it can
        # only produce a difference of unknown origin, which is what INCONCLUSIVE
        # is for. Distinct from the staleness blocker: waiting does not fix this.
        rec.blockers.append(snap.figure_note)

    # ---- verdict ----
    #
    # `CLOSES` used to be decided here, before the blockers above were consulted,
    # so every one of them was collected and then thrown away whenever the two
    # figures happened to land within tolerance. The headline then read
    # "reconciles (difference is within 2.0 GiB)" -- an all-clear, and the
    # strongest thing this tool says -- directly above a blocker reading "11,267
    # directories could not be read, so the walk total is a floor, not a total".
    #
    # An agreement reached by an unsound comparison is not evidence. If the walk
    # undercounts, matching the quota means the true total *exceeds* it; if the
    # walk crossed three filesystems while the quota governs one, the match is
    # arithmetic coincidence. This is the same discipline `SettleCheck.conclusive`
    # applies to the settling check -- before believing a null result, ask whether
    # the instrument could see the effect at all -- and `CLOSES` is a null result.
    #
    # It is also what this module's own docstring already promised: *every input
    # that could invalidate the comparison downgrades the verdict to INCONCLUSIVE
    # and names itself*. The blockers were named; the downgrade was skipped.
    if rec.within_tolerance and not rec.blockers:
        rec.verdict = CLOSES
        return rec

    if rec.blockers:
        rec.verdict = INCONCLUSIVE
        if rec.within_tolerance:
            # Worth saying out loud, because it is genuinely reassuring and the
            # reader can see the two figures anyway -- but as an observation, not
            # a verdict. `_candidates` is not called: it exists to explain a gap
            # and there is no gap to explain.
            rec.notes.append(
                "the two figures do agree, but the comparison that produced the "
                "agreement is not sound, so it is not evidence that the quota is "
                "explained"
            )
        else:
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
        #
        # What the /proc scan could not see, one cause at a time.
        #
        # This used to be a single sentence about other users, gated on
        # `DeletedScan.complete`, and `complete` is False for three unrelated
        # reasons: EACCES on another user's process, a PID namespace showing only
        # its own processes, and a sweep abandoned on a hung mount. So the two
        # that are not EACCES printed "held by 0 processes belonging to other
        # users" -- a possible cause naming a population the same scan had just
        # measured as empty -- while the cause that actually limited coverage was
        # not listed at all. Inside Apptainer, which is how most work on this
        # cluster runs, that was the only wording a reader ever saw.
        #
        # And `complete` used to be True for a scan that never ran: with
        # `--no-deleted`, or on a platform without /proc, `available` is False and
        # the counters are all zero, so all three tests passed. The line below
        # then credited a scan that did not happen with covering "this node",
        # which is both false and backwards -- an unlinked file on *this* node is
        # the one thing a scan that did not run cannot rule out. `complete` now
        # tests `available` first; this list still reads the four fields directly,
        # because it has to say *which* limit applied, not merely that one did.
        if not deleted.available:
            out.append(
                # No "would have" beside the interpolation: the value is a reason
                # string, not a count, but `have` is one of the words
                # `test_no_message_pairs_a_count_with_a_fixed_verb` sweeps for, and
                # rewording is cheaper than another allow-list entry to read past.
                "unlinked-but-open files on any node, this one included -- the "
                "/proc scan of this node did not run{}".format(
                    " ({})".format(deleted.reason) if deleted.reason else ""
                )
            )
        else:
            out.append(
                "unlinked-but-open files held by processes on other nodes -- this "
                "scan only sees this node"
            )
            if deleted.unreadable_pids:
                out.append(
                    # Through `plural`, because the guard is now a truth test
                    # rather than `!= 0` on a complete flag: one refused process
                    # on a shared node is the common case, and it read "held by 1
                    # processes".
                    "unlinked-but-open files held by {} belonging to other "
                    "users, which an unprivileged scan cannot inspect".format(
                        plural(deleted.unreadable_pids, "process", irregular="processes")
                    )
                )
            if deleted.namespaced:
                out.append(
                    "unlinked-but-open files held by processes outside this PID "
                    "namespace, which /proc did not list"
                )
            if deleted.timed_out:
                out.append(
                    "unlinked-but-open files the /proc sweep had not reached when "
                    "it was abandoned on an unresponsive mount"
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
        return "this subtree is {}{:.1f}% of the {} quota figure".format(
            # A floor over a fixed denominator is a lower bound, and the two words
            # that say so belong in the headline rather than only in the note
            # underneath it. Without them the line is a measurement, and the
            # reader has no way to tell it from one.
            "at least " if rec.walk_is_floor else "",
            100.0 * share,
            rec.row.fileset if rec.row else "?",
        )
    if rec.verdict == CLOSES:
        tol = human_count(rec.tolerance) if rec.kind == "files" else human_bytes(rec.tolerance)
        return "reconciles (difference is within {})".format(tol)
    if rec.verdict == INCONCLUSIVE:
        why = rec.blockers[0] if rec.blockers else "unknown"
        if rec.within_tolerance:
            # Naming the agreement without the qualifier would be read as the
            # all-clear this verdict exists to withhold.
            return "INCONCLUSIVE -- the figures agree, but not soundly: {}".format(why)
        return "INCONCLUSIVE -- {}".format(why)
    return "UNEXPLAINED GAP"
