"""Quota readers, and the age of the number they return.

The central fact this module exists to carry: **a quota reading is a snapshot,
and a snapshot has an age.** Measured on Midway3, ``quota`` is a site Python
wrapper printing a cached figure that was 28 minutes old and did not refresh
while being polled -- a 512 MiB file was written, fsync'ed and unlinked without
the number moving at all. Two local ticket classes are exactly this:

    "quota is delayed / cached / not refreshing"   217 tickets, median 38.0 h
    "I deleted files but my quota did not change"   48 tickets, median 74.8 h

So every reading is returned with the timestamp the backend itself published,
and the age is printed beside the number. Where the backend publishes no
timestamp we report ``unknown`` -- never ``now``.

Backends are tried in order and all are optional. Off-site there may be no quota
command at all; that is reported as an absent field with a reason, not as zero
(Constraint 10), and nothing downstream is allowed to break because of it
(Constraint 15).
"""

import os
import re
import shutil
import socket
import subprocess
import time
from typing import Dict, List, Optional, Tuple  # noqa: F401  (`# type:` use)

# Site wrappers can be slow (they may query the filesystem's quota manager).
DEFAULT_TIMEOUT_S = 45.0

_SIZE_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*([KMGTPE]?)i?B?$", re.IGNORECASE)
_SCALE = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40, "P": 1 << 50, "E": 1 << 60}

# "Quota information updated at :  2026-08-01 17:32:24"
_UPDATED_RE = re.compile(
    r"updated\s+at\s*:?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2})",
    re.IGNORECASE,
)
# ">>> Capacity Filesystem: project (Midway3 GPFS mounted at /project)". The
# mount point is *published*, which makes the mapping evidence rather than a guess.
_MOUNT_RE = re.compile(r"mounted\s+at\s+(/\S+?)\)?\s*$", re.IGNORECASE)
# ">>> Capacity Filesystem: project2 (GPFS)" -- the same site wrapper, at a site
# that prints no mount clause. Requiring "mounted at" left `current_mount` None
# for every row of every section and handed the whole table to `_guess_mount`,
# which is the weakest evidence this module has. The filesystem *name* is still
# published, and /proc/mounts can turn a name into a mount point without
# guessing from a fileset label.
_FS_NAME_RE = re.compile(r"filesystem\s*:\s*([^\s(]+)", re.IGNORECASE)


def parse_size(token: str) -> Optional[int]:
    """Parse ``127.94M`` / ``1024.00G`` / ``0.00K`` into bytes."""
    m = _SIZE_RE.match(token.strip())
    if not m:
        return None
    return int(float(m.group(1)) * _SCALE[m.group(2).upper()])


class QuotaRow:
    """One quota line: a used/soft/hard triple for blocks or files."""

    def __init__(
        self,
        fileset: str,
        kind: str,
        scope: str,
        used: int,
        soft: Optional[int],
        hard: Optional[int],
        grace: str = "",
        mount: Optional[str] = None,
        guessed: bool = False,
    ) -> None:
        self.fileset = fileset
        self.kind = kind  # "blocks" | "files"
        self.scope = scope  # "user" | "group" | "fileset" | ""
        self.used = used
        self.soft = soft
        self.hard = hard
        self.grace = grace
        self.mount = mount
        # Every mount this row governs. One filesystem is routinely mounted at
        # several places -- on this cluster the single GPFS filesystem
        # `midway3_cap` is mounted at /home, /project, /software and
        # /gpfs/midway3/cap -- and a row that remembers only the first of them
        # fails to map a walk of any of the others.
        self.mounts = [mount] if mount else []  # type: List[str]
        # True when the mount was inferred from the fileset name rather than
        # published by the backend. Inferred mounts are dropped on ambiguity.
        self.guessed = guessed
        self.mount_note = ""

    @property
    def limit(self) -> Optional[int]:
        """The figure usage is measured against: the soft limit, else the hard one.

        A property because the fraction and the *displayed* limit have to be the
        same number and were not. Most backends spell "no limit" as ``0``, so a
        row with ``soft=0, hard=250000000`` had its percentage computed against
        the hard limit -- correctly -- while the table printed the soft one, and
        the reader saw ``44,812,476 / 0`` beside ``17.9%``. A used/limit pair that
        is not the denominator of the percentage next to it is worse than no pair
        at all.

        Zero and ``None`` both come back as ``None`` here, because neither is a
        limit; the renderer keeps them apart, since "the backend reported no
        limit" and "the limit could not be read" are different claims.

        **The smallest limit that is set, not the soft one.** Soft is normally the
        lower of the two, so on any correctly configured quota this is the soft
        limit and nothing changes. Where a site has raised the soft limit and left
        the hard one behind, soft is unreachable and hard binds first -- and
        preferring soft there reported a fileset at 100% of its enforced limit as
        "40.0%", with ``_quota_needs_attention`` returning False. That is a third
        state in which the cron-friendly invocation says "fine" while writes are
        about to stop, which is the exact failure that docstring was written about.
        """
        limits = [x for x in (self.soft, self.hard) if x]
        return min(limits) if limits else None

    @property
    def usage_fraction(self) -> Optional[float]:
        limit = self.limit
        if not limit:
            return None
        return self.used / float(limit)

    def __repr__(self) -> str:
        return "QuotaRow({} {} {} used={})".format(self.fileset, self.kind, self.scope, self.used)


class QuotaSnapshot:
    """A backend's whole reading, with the age of the figures it contains."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.rows = []  # type: List[QuotaRow]
        self.taken_at = None  # type: Optional[float]
        self.read_at = time.time()
        self.available = False
        self.reason = ""  # why it is unavailable, when it is
        # Doubt about the *age*, not the figures: see `_timezone_suspicion`.
        self.time_note = ""
        # Doubt about the *figures*, not their age. Kept apart from `time_note`
        # because they are different doubts with different remedies: a stale
        # figure needs waiting, a disowned one needs the filesystem fixed.
        #
        # `lfs quota` is the backend that raised this: when it cannot reach an
        # OST it brackets the figure it could not verify and prints "The data in
        # "[]" is inaccurate". A number the backend itself disowns cannot support
        # a finding, and reporting it silently is exactly what this module exists
        # not to do.
        self.figure_note = ""
        self.raw = ""

    @property
    def age_seconds(self) -> Optional[float]:
        """How stale the *figures* are, not how long ago we ran the command."""
        if self.taken_at is None:
            return None
        return self.read_at - self.taken_at

    def rows_for_path(self, path: str) -> List[QuotaRow]:
        """Rows whose mount point is the longest prefix of ``path``.

        Mount points come from the backend's own output where it publishes them
        (``mounted at /project``), so this mapping is not a hard-coded site fact.
        A path we cannot map returns an empty list and the caller says so.
        """
        target = os.path.abspath(path)
        best = -1
        chosen = []  # type: List[QuotaRow]
        for row in self.rows:
            for raw in row.mounts or ([row.mount] if row.mount else []):
                mount = raw.rstrip("/") or "/"
                if target == mount or target.startswith(mount.rstrip("/") + "/"):
                    if len(mount) > best:
                        best = len(mount)
                        chosen = [row]
                    elif len(mount) == best and row not in chosen:
                        chosen.append(row)
        return chosen

    def mapping_notes(self) -> List[str]:
        """Distinct reasons some rows could not be tied to a path."""
        notes = []  # type: List[str]
        for row in self.rows:
            if row.mount_note and row.mount_note not in notes:
                notes.append(row.mount_note)
        return notes


def _c_env() -> Dict[str, str]:
    """The caller's environment with the locale forced to C.

    Every number this module parses is read with a ``.`` decimal separator
    (``_SIZE_RE``) and every date with an ISO-ordered pattern. Under a
    comma-decimal locale -- ``de_DE``, ``fr_FR``, ``pt_BR``, and the default on a
    good many European institutional images -- a backend that prints ``127,94M``
    parses as nothing, and the tool reports "could not parse `quota -s` output"
    on a machine where ``quota`` works perfectly. The failure is total, silent,
    and entirely in our own output.
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def _run(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=_c_env(),
        )
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out or "", err or ""
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return 124, "", "timed out after {:.0f}s".format(timeout)
    except OSError as exc:
        return 1, "", str(exc)


def _current_user() -> str:
    """The name to ask a quota backend about: **who this process actually is.**

    The passwd entry for our own uid first, then ``$USER``, then the bare uid.

    The order used to be the other way round, on the reasoning that ``$USER`` is
    the name the site's own tooling uses. It buys nothing real -- a login shell
    sets ``$USER`` *from* the passwd entry, so where the two agree the order does
    not matter -- and where they disagree, ``$USER`` is the wrong one. A stale
    ``export USER=somebody`` from a test harness, a CI wrapper or a module file
    makes ``mmlsquota -u`` and ``lfs quota -u`` return **somebody else's quota**,
    which the report then presents as yours and `reconcile` compares your walk
    against. That is a fabricated comparison sourced from an environment variable,
    with nothing on screen to say so.

    ``$USER`` stays as the second choice because it is the only answer left when
    the name service cannot resolve our own uid -- measured on midway2, where
    ``getent passwd $UID`` finds nothing on a compute node -- and there is no
    authority for it to disagree with. The bare uid is last and every backend here
    accepts it.
    """
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        pass
    return os.environ.get("USER") or str(os.getuid())


def _one_line(text: str) -> str:
    """A backend's message, collapsed to a single line.

    ``reason`` is rendered inside a framed panel and referred to again by the
    reconcile section, and GPFS writes *three* lines of diagnostics for one
    failure. A field documented as a reason that is sometimes a paragraph makes
    its layout every consumer's problem.

    On 0.3.0 those newlines took the frame's right-hand border with them.
    :func:`ui.box` has since gained a split on embedded newlines, so the frame
    closes either way -- **that split is load-bearing and is not made redundant by
    this function.** What collapsing fixes is the rest of it: continuation lines
    that lost the indent marking them as continuations, and the same paragraph
    printed once in ``QUOTA`` and again for each kind in ``RECONCILE``.

    Done here, at the only place that knows the text is a message rather than data,
    so no consumer has to discover that a "reason" can be a paragraph.
    """
    return " ".join((text or "").split())


def _grouped_failures(failures: "List[Tuple[str, str]]") -> str:
    """``(subject, message)`` pairs as one line, grouped by message.

    Two places in this module ask the same question several times over and report
    how each attempt went: ``lfs quota`` once per scope, ``mmlsquota`` once per
    GPFS device. Both joined the messages and dropped the subject, so three
    devices failing three different ways read

        No quota enabled file system found.; Non root user is not permitted to
        run with the specified option(s); Command failed.

    -- unusable, because *which filesystem needs an admin* is the whole content of
    the second one. Naming the subject on every line instead repeats one fact per
    subject when they all fail alike, which is the other failure mode. Grouping
    does both: one segment per distinct message, with every subject it applies to.

        default, fsA: No quota enabled file system found.; fsB: Non root user ...

    Order is tracked explicitly rather than taken from dict insertion: CPython 3.6
    happens to preserve it, but the guarantee starts at 3.7 and this package
    supports 3.6.
    """
    order = []  # type: List[str]
    subjects = {}  # type: Dict[str, List[str]]
    for subject, message in failures:
        if message not in subjects:
            subjects[message] = []
            order.append(message)
        if subject and subject not in subjects[message]:
            subjects[message].append(subject)
    parts = []  # type: List[str]
    for message in order:
        named = subjects[message]
        parts.append("{}: {}".format(", ".join(named), message) if named else message)
    return _one_line("; ".join(parts))


def _explain_127(command: str, err: str) -> str:
    """Why a command exited 127: absent, or present and broken inside.

    Exit 127 has two causes and they need opposite answers. :func:`_run`
    synthesises it for ``FileNotFoundError``, which really is "no such command".
    But a shell *wrapper* whose inner command is missing also exits 127, and then
    the command is on PATH and running exactly as installed. Measured on midway2:
    ``/software/bin/quota`` is a two-line ``/bin/sh`` wrapper around
    ``/srv/adm/gpfsquota``, a path that does not exist on that cluster -- so
    ``quota`` exists, runs, exits 127, and rapidu reported "`quota` is not on
    PATH", flatly contradicting ``type -a quota`` while discarding the one line
    that said what was actually wrong::

        /software/bin/quota: line 3: /srv/adm/gpfsquota: No such file or directory

    ``shutil.which`` settles which of the two it is, and ``err`` is carried
    either way: a backend's own account of its own failure is the most useful
    thing this module can pass on, and it is the only signal a user gets when the
    working ``quota`` at their site is a shell alias a subprocess cannot see.
    """
    if shutil.which(command) is None:
        return "`{}` is not on PATH".format(command)
    return "`{}` is on PATH but exited 127: {}".format(
        command, _one_line(err) or "command not found"
    )


# Every spelling of a quota scope we have seen a backend publish, mapped to the
# four this codebase reasons about. `mmlsquota -Y` publishes `USR`/`GRP`/
# `FILESET`; `lfs quota` is asked one scope at a time and we name it ourselves;
# the RCC wrapper prints `(user)`/`(group)`. Lowercasing `USR` yields `usr`,
# which matched nothing, so a personal GPFS quota was compared against every
# file in a shared tree and the candidate list blamed a group that did not
# exist. The mapping is the fix; the point of doing it in one table is that a new
# backend cannot reintroduce the bug by inventing a fifth spelling silently --
# an unrecognised scope stays verbatim and `_pick_row` treats it as non-user,
# which is the conservative direction.
_SCOPE_ALIASES = {
    "usr": "user",
    "user": "user",
    "u": "user",
    "grp": "group",
    "group": "group",
    "g": "group",
    "fileset": "fileset",
    "filset": "fileset",
    "prj": "project",
    "proj": "project",
    "project": "project",
    "p": "project",
}


def _norm_scope(raw: str) -> str:
    """A backend's spelling of a quota scope, in this codebase's vocabulary."""
    return _SCOPE_ALIASES.get((raw or "").strip().lower(), (raw or "").strip().lower())


# Every spelling of "no timer is running", lower-cased. Verified against the
# vocabularies the real tools use: quota-tools writes `none` for no timer and
# `%ddays` / `%uhours` / `%uminutes` / `%useconds` (including `0seconds`) for a
# running one, `lfs` writes `-`, and the RCC wrapper writes `none`.
#
# One tuple because `report.render_quota` had its own copy of this test with a
# shorter list -- no `0`, no `n/a` -- so the renderer was a weaker second guard
# for a rule this function already applies. Every backend path here does call
# `_clean_grace`, so nothing was slipping through; two homes for one rule is
# simply how they drift apart, which this codebase says elsewhere and is worth
# saying once here.
_NOT_IN_GRACE = ("", "-", "none", "0", "n/a")


def in_grace(raw: str) -> bool:
    """Is this grace field reporting a running timer?"""
    return bool(_clean_grace(raw))


class MountReport:
    """What ``statvfs`` says about the filesystem a path sits on.

    Deliberately *not* a :class:`QuotaRow`. A quota row is a limit somebody set
    for you; this is whatever the mount chooses to report, and the two coincide
    only sometimes. It exists because on a cluster where no quota backend works
    the mount is often still telling you the answer, and printing nothing there
    is worse than printing a number with its provenance attached.
    """

    def __init__(self, path, mount, total, used, avail, inodes_total, inodes_free):
        self.path = path
        self.mount = mount
        self.total = total
        self.used = used
        self.avail = avail
        self.inodes_total = inodes_total
        self.inodes_free = inodes_free

    @property
    def fraction(self):
        # type: () -> Optional[float]
        if not self.total:
            return None
        return self.used / float(self.total)


def mount_report(path: str) -> "Optional[MountReport]":
    """``statvfs`` for ``path``, or ``None`` if it cannot be read.

    **Why this is not offered as a quota.** On a filesystem with no per-user
    limit, ``statvfs`` reports the whole filesystem's capacity. On an export that
    enforces one, the server reports the *quota* through the same fields -- Isilon
    and NetApp both do, and measured on a Booth login node a 14 GiB "filesystem"
    was in fact a 14 GiB home quota at 48%. Nothing in ``statvfs`` distinguishes
    the two cases, so the figure is reported with that ambiguity stated rather
    than relabelled as something it might not be.

    ``f_files`` of 0 means the filesystem does not report inode counts; that comes
    back as ``None`` rather than as a limit of zero.
    """
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    frsize = st.f_frsize or st.f_bsize or 0
    if not frsize or not st.f_blocks:
        return None
    total = st.f_blocks * frsize
    # `f_bavail` is the headroom *this* user has, which is the number that binds
    # under a quota; `f_bfree` can be larger where blocks are reserved for root.
    avail = st.f_bavail * frsize
    used = total - st.f_bfree * frsize
    inodes_total = st.f_files or None
    inodes_free = st.f_ffree if inodes_total else None
    mount = ""
    try:
        mount = _mount_for(path)
    except Exception:
        mount = ""
    return MountReport(path, mount, total, max(0, used), avail, inodes_total, inodes_free)


def _mount_for(path: str) -> str:
    """The longest mount point that is a prefix of ``path``, or ``""``."""
    target = os.path.abspath(path)
    best = ""
    for point in _mount_points():
        p = point.rstrip("/") or "/"
        if (target == p or target.startswith(p.rstrip("/") + "/")) and len(p) > len(best):
            best = p
    return best


def _clean_grace(raw: str) -> str:
    """A grace field, with every backend's spelling of "not in grace" removed.

    ``lfs`` prints ``-``, GPFS prints ``none`` and the RCC wrapper prints
    ``none`` too. Passing any of those through would make
    :func:`report.render_quota` paint "! IN GRACE, - left", which is worse than
    saying nothing: it is the only warning in the tool that means *writes are
    about to stop*, so a false positive spends the one alarm that matters.
    """
    g = (raw or "").strip()
    if g.lower() in _NOT_IN_GRACE:
        return ""
    return g


def _budget(timeout: float, deadline: Optional[float]) -> float:
    """How long one subprocess may run: its own timeout, capped by the deadline.

    ``read_best`` may run six subprocesses (``quota``, ``mmlsquota``, ``lfs
    project``, and ``lfs quota`` once per scope). Giving each the full timeout
    made the worst case their *sum* -- 225s of silence at the 45s default, 270s
    when a project id is found -- with no spinner, before the walk has started.
    A hanging ``lfs quota`` is not exotic; it is what a Lustre client does when an
    MDS is degraded, which is the same afternoon someone reaches for this tool.
    """
    if deadline is None:
        return timeout
    return max(0.0, min(timeout, deadline - time.time()))


def _guess_mount(fileset: str, points: Optional[List[str]] = None) -> Optional[str]:
    """Best-effort mount for a fileset named without an explicit mount line.

    Only returns a path the kernel calls a **mount point**, because a directory
    that merely exists is not evidence of anything. That distinction is the whole
    safety of this function, and without it the degradation it used to promise --
    "a wrong guess degrades to 'unmapped' rather than to a confident
    mis-attribution" -- did not hold. Measured on midway2: the ``scratch``
    fileset lives on ``midway2_perf`` at ``/scratch/midway2``, while ``/scratch``
    is merely the parent directory holding three clusters' scratch filesystems::

        midway2_perf   /scratch/midway2
        midway3_perf   /scratch/midway3
        beagle3_perf   /scratch/beagle3

    ``os.path.isdir("/scratch")`` is true, so ``/scratch`` was returned,
    :meth:`QuotaSnapshot.rows_for_path` matched it as a prefix of
    ``/scratch/midway3/$USER``, and a **midway3** walk was reconciled against
    **midway2's** scratch quota. Nothing collided, so ``_disambiguate_mounts``
    saw no ambiguity to resolve: it was one row, confidently wrong. Requiring a
    row in ``/proc/mounts`` rejects ``/scratch`` and keeps ``/scratch/midway2``.

    The same rule fixes a smaller version of it: ``home`` used to resolve to
    ``os.path.expanduser("~")``, which is a directory *inside* the mount rather
    than the mount, so any row built from it mapped one user's home and no other.

    Guesses are still run through :func:`_disambiguate_mounts`, and every row
    built from one carries ``guessed=True`` so :mod:`rapidu.reconcile` can refuse
    to turn an inferred mapping into a finding.
    """
    known = _mount_points() if points is None else points
    name = fileset.strip().lower()
    candidates = []  # type: List[str]
    if name.endswith("-home") or name == "home":
        candidates.append(os.path.expanduser("~"))
        candidates.append("/home")
    if "/" in name:
        candidates.append("/" + name)
    candidates.append("/" + name.replace("-", "/"))
    for c in candidates:
        if not c:
            continue
        full = os.path.abspath(c)
        if full in known:
            return full
        # No mount table at all -- no /proc, or one we could not read -- is not
        # evidence against a candidate, and refusing every one of them there
        # would drop rows the backend read and parsed perfectly well. Only in
        # that case does the weaker test apply.
        if not known and os.path.isdir(full):
            return full
    return None


# How close an age must sit to the UTC offset before we call it suspicious. A
# real snapshot age landing within three minutes of the offset, to the minute,
# is a coincidence worth one line of doubt.
_TZ_SUSPICION_S = 180.0


def _utc_offset(when: float) -> int:
    """Seconds local time is *ahead* of UTC at ``when``. Negative west of UTC."""
    return -(time.altzone if time.localtime(when).tm_isdst else time.timezone)


def _timezone_suspicion(age: Optional[float], read_at: float) -> str:
    """Does this age look like a timezone mis-parse rather than a real age?

    :func:`time.mktime` reads a naive timestamp as **local** time. The RCC
    wrapper publishes local time, so that is correct here -- Constraint 7
    exactly: never generalise site output to vendor behaviour. A backend
    publishing UTC breaks it, and displaces the age by precisely the UTC offset:

    * **East of UTC** the figure reads as hours *stale*. Nothing errors, every
      reconciliation goes ``INCONCLUSIVE`` with a plausible-looking reason, and
      the feature quietly stops working. This is the dangerous direction.
    * **West of UTC** the age goes negative and prints as "in the future", which
      :func:`fmt.human_duration` already catches and shows.

    A bare timestamp carries no zone, so neither reading can be *proved* and
    silently "correcting" one would be a guess dressed as a measurement. What
    can be said is that an age sitting on the UTC offset to the minute is more
    likely a zone mismatch than a coincidence -- so we say that, and leave the
    number alone.
    """
    if age is None:
        return ""
    offset = _utc_offset(read_at)
    if not offset or abs(age - offset) > _TZ_SUSPICION_S:
        return ""
    return (
        "this age is within a few minutes of the {:+.1f}h UTC offset, so the "
        "backend may be publishing UTC where a local timestamp is assumed; treat "
        "the age, not the reading, as unproven".format(offset / 3600.0)
    )


def read_mount_table(path: str = "/proc/mounts") -> Dict[str, List[str]]:
    """``device name -> every path it is mounted at``, from ``/proc/mounts``.

    GPFS filesystem names routinely differ from their mount points -- ``gpfs0``
    at ``/scratch``, ``fs1`` at ``/work``, ``cephfs`` at ``/cds`` -- so deriving
    the mount as ``"/" + filesystemName`` produces ``None`` at most sites and
    silently drops a quota row the backend read and parsed perfectly well. The
    kernel already publishes the answer; there is no need to guess it and no
    need for a site config file to state it.

    Mounts are returned shortest-first so the most likely user-facing path leads.
    """
    table = {}  # type: Dict[str, List[str]]
    for device, point, _fstype in _mount_entries(path):
        table.setdefault(device, []).append(point)
    for mounts in table.values():
        mounts.sort(key=lambda m: (len(m), m))
    return table


def _mount_entries(path: str = "/proc/mounts") -> List[Tuple[str, str, str]]:
    """``(device, mount point, filesystem type)`` for every mount, in kernel order.

    One parse behind the three questions this module asks of the mount table --
    where is this device mounted, is this path a mount point at all, and which
    devices are GPFS. The third needs the fstype column, which the old
    device-to-mounts reader dropped on the floor.
    """
    entries = []  # type: List[Tuple[str, str, str]]
    try:
        with open(path) as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 2:
                    continue
                # Mount points are octal-escaped for spaces and tabs.
                point = fields[1].replace("\\040", " ").replace("\\011", "\t")
                # A two-column line has no type to report. Real /proc/mounts and
                # /etc/mtab always have six, but the device-to-mounts reading was
                # never fussy about it and does not need to become so: an unknown
                # type simply matches no `_devices_of_type` filter.
                entries.append((fields[0], point, fields[2] if len(fields) > 2 else ""))
    except (OSError, UnicodeDecodeError):
        return []
    return entries


def _mount_points(path: str = "/proc/mounts") -> List[str]:
    """Every path the kernel calls a mount point.

    The distinction between this and "a directory that exists" is the whole
    safety of :func:`_guess_mount`; see the note there.
    """
    return [point for _device, point, _fstype in _mount_entries(path)]


def _devices_of_type(fstypes: Tuple[str, ...], path: str = "/proc/mounts") -> List[str]:
    """Device names of every mounted filesystem of one of ``fstypes``.

    Sorted and de-duplicated, because one device is routinely mounted several
    times and asking its quota manager the same question four times is four round
    trips for one answer.
    """
    wanted = tuple(t.lower() for t in fstypes)
    found = []  # type: List[str]
    for device, _point, fstype in _mount_entries(path):
        if fstype.lower() in wanted and device not in found:
            found.append(device)
    return sorted(found)


def _mounts_for(
    name: str,
    table: Optional[Dict[str, List[str]]] = None,
    points: Optional[List[str]] = None,
) -> List[str]:
    """Mount points for a filesystem name, by device match then by name match."""
    table = read_mount_table() if table is None else table
    if name in table:
        return list(table[name])
    # Some backends name the filesystem without the device prefix it is mounted
    # under; fall back to the old inference, which is right when it is right --
    # but only to a path the kernel calls a mount point, for the reason
    # `_guess_mount` sets out at length.
    known = _mount_points() if points is None else points
    guess = "/" + name.lstrip("/")
    if guess in known:
        return [guess]
    if not known and os.path.isdir(guess):
        return [guess]
    return []


def _header_mounts(
    header: str, table: Dict[str, List[str]], points: List[str]
) -> Tuple[List[str], bool]:
    """The mounts a ``>>>`` section header names, and whether they were inferred.

    Two forms, and the difference matters more than the parse does:

    ``(Midway3 GPFS mounted at /project)`` publishes the mount point, so the
    mapping is a fact and the row is not marked as guessed. Note the name in
    these headers is *not* unique -- on midway3 two sections are both called
    ``project``, one mounted at ``/beagle3`` -- so where a mount is published it
    wins outright.

    ``(GPFS)`` publishes only the filesystem name. A name has to be looked up,
    and looking it up against ``/proc/mounts`` is still far better evidence than
    guessing from a fileset label: it is the kernel's answer about a device that
    exists. But it is an inference, so it comes back flagged, and every row under
    such a header carries ``guessed`` -- which is what stops an inferred mapping
    from being reported as a confident gap.
    """
    published = _MOUNT_RE.search(header)
    if published:
        return [published.group(1)], False
    named = _FS_NAME_RE.search(header)
    if not named:
        return [], False
    resolved = _mounts_for(named.group(1).strip().rstrip(":"), table, points)
    return resolved, bool(resolved)


def _enclosing_mount(target: str, fstypes: Tuple[str, ...], path: str = "/proc/mounts") -> str:
    """The longest mount point of one of ``fstypes`` that contains ``target``.

    Used only as a fallback: when a backend's own output does not name the
    filesystem's mount point, the kernel still does. Returns ``""`` when nothing
    matches, so the caller degrades to "unmapped" rather than to a guess.
    """
    target = os.path.abspath(target)
    best = ""
    try:
        with open(path) as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 3 or fields[2] not in fstypes:
                    continue
                point = fields[1].replace("\\040", " ").replace("\\011", "\t")
                stem = point.rstrip("/") or "/"
                covers = target == stem or target.startswith(stem.rstrip("/") + "/")
                if covers and len(stem) > len(best):
                    best = stem
    except (OSError, UnicodeDecodeError):
        return ""
    return best


def _host_tokens() -> List[str]:
    host = socket.gethostname().lower()
    return [t for t in re.split(r"[^a-z0-9]+", host) if len(t) >= 4]


def _name_matches_host(fileset: str, host_tokens: List[str]) -> bool:
    """Does this fileset name mention the cluster we are actually running on?

    ``Midway2-home`` and ``Midway3-home`` both plausibly resolve to ``$HOME``;
    only one of them is the home we are standing in. The hostname is the only
    evidence available without a site config file, and it is used solely to
    *narrow* an ambiguity -- never to create a mapping on its own.
    """
    tokens = [t for t in re.split(r"[^a-z0-9]+", fileset.lower()) if len(t) >= 4]
    return any(t in host_tokens for t in tokens)


def _disambiguate_mounts(rows: List[QuotaRow]) -> None:
    """Drop inferred mounts that more than one fileset claims.

    A mount claimed by two filesets would otherwise silently attribute a walk to
    whichever row happened to be parsed first -- the wrong cluster's home
    directory, for instance. Where the hostname resolves the tie, keep the
    winner; where it does not, unmap all claimants and say why.
    """
    claims = {}  # type: Dict[str, set]
    for r in rows:
        if r.mount and r.guessed:
            claims.setdefault(r.mount, set()).add(r.fileset)

    host_tokens = _host_tokens()
    for mount, filesets in claims.items():
        if len(filesets) < 2:
            continue
        winners = [f for f in sorted(filesets) if _name_matches_host(f, host_tokens)]
        keep = winners[0] if len(winners) == 1 else None
        for r in rows:
            if r.guessed and r.mount == mount and r.fileset != keep:
                r.mount = None
                r.mounts = []
                r.mount_note = (
                    "{} filesets ({}) both resolve to {}; this one could not be "
                    "told apart from the others".format(
                        len(filesets), ", ".join(sorted(filesets)), mount
                    )
                )


def read_quota_command(
    timeout: float = DEFAULT_TIMEOUT_S, deadline: Optional[float] = None
) -> QuotaSnapshot:
    """Parse ``quota -s``, including site wrappers that print a table.

    Handles the tabular form used by RCC's ``systool.quota9`` wrapper:

        fileset          type                   used      quota      limit    grace
        Midway3-home     blocks (user)       679.66M     30.00G     35.00G     none
        rcc              files  (group)     43583258  230900000  231900000     none

    with ``>>> ... (Midway3 GPFS mounted at /project)`` section headers that
    supply the mount point for the rows beneath them.
    """
    snap = QuotaSnapshot("quota -s")
    rc, out, err = _run(["quota", "-s"], _budget(timeout, deadline))
    snap.raw = out
    if rc == 127:
        snap.reason = _explain_127("quota", err)
        return snap
    if rc == 124:
        snap.reason = _one_line(err)
        return snap
    if not out.strip():
        snap.reason = _one_line(err) or "`quota -s` produced no output"
        return snap

    m = _UPDATED_RE.search(out)
    if m:
        stamp = m.group(1).replace("T", " ")
        try:
            snap.taken_at = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            snap.taken_at = None
        else:
            snap.time_note = _timezone_suspicion(snap.age_seconds, snap.read_at)

    table = read_mount_table()
    points = _mount_points()
    current_mounts = []  # type: List[str]
    current_inferred = False
    for line in out.splitlines():
        s = line.strip()
        if not s or s.startswith("---") or s.lower().startswith("fileset"):
            continue
        if s.startswith(">>>"):
            current_mounts, current_inferred = _header_mounts(s, table, points)
            continue
        row = _parse_quota_row(s, current_mounts, current_inferred, points)
        if row is not None:
            snap.rows.append(row)

    if not snap.rows:
        # Fall back to the stock multi-line `quota` format rather than claiming
        # the user has no quota at all.
        snap.rows = _parse_stock_quota(out)
    _disambiguate_mounts(snap.rows)
    if snap.rows:
        snap.available = True
    else:
        if _QUOTA_NONE_RE.search(out):
            snap.reason = "`quota` reports no quota set for this account on any filesystem"
        else:
            snap.reason = _one_line(
                "could not parse `quota -s` output" + (": " + err.strip() if err.strip() else "")
            )
    return snap


def _parse_quota_row(
    line: str,
    mounts: Optional[List[str]] = None,
    inferred: bool = False,
    points: Optional[List[str]] = None,
) -> Optional[QuotaRow]:
    """One table row, tied to the mounts its section header named.

    ``mounts`` is what the header published or resolved; ``inferred`` says the
    resolution came from a filesystem *name* rather than from a printed mount
    point. Either way the row records how it was mapped, because the difference
    between a published mapping and an inferred one is the difference between a
    finding and a guess.
    """
    parts = line.split()
    if len(parts) < 5:
        return None
    fileset = parts[0]
    kind = parts[1].lower()
    if kind not in ("blocks", "files"):
        return None
    idx = 2
    scope = ""
    if parts[2].startswith("(") and parts[2].endswith(")"):
        scope = parts[2].strip("()").lower()
        idx = 3
    nums = parts[idx:]
    if len(nums) < 3:
        return None

    def conv(tok: str) -> Optional[int]:
        # `*` marks a figure that is over its limit, which is the one row in the
        # table that matters. Without this strip `parse_size` (anchored on `$`)
        # returned None for `35.00G*`, `used` came back None, and the row was
        # dropped -- so a fileset in grace disappeared from the report at exactly
        # the moment it became the finding, and a user over both limits got
        # "could not parse `quota -s` output" with no rows at all. Both sibling
        # parsers in this module already strip it; this one did not.
        tok = tok.rstrip("*")
        if kind == "files":
            try:
                return int(tok)
            except ValueError:
                return parse_size(tok)
        return parse_size(tok)

    used = conv(nums[0])
    if used is None:
        return None
    soft = conv(nums[1])
    hard = conv(nums[2])
    # Normalised at the parser, not at each consumer. The site wrapper prints the
    # literal word "none" when no timer is running, and a truthiness test on that
    # string reads as "in grace" -- which is how a 2.9%-full home came to set
    # EXIT_ATTENTION. One backend spelling "not in grace" as a non-empty string is
    # enough to poison every consumer, so none of them should have to know.
    grace = _clean_grace(nums[3] if len(nums) > 3 else "")
    resolved = list(mounts or [])
    guessed = inferred or not resolved
    if not resolved:
        # No header, or a header that named nothing this kernel knows: the fileset
        # label is all that is left, and it is the weakest evidence here.
        fallback = _guess_mount(fileset, points)
        if fallback:
            resolved = [fallback]
    row = QuotaRow(
        fileset, kind, scope, used, soft, hard, grace, resolved[0] if resolved else None, guessed
    )
    row.mounts = list(resolved)
    return row


def _is_figure(tok: str) -> bool:
    """Does this token read as one of the six numeric columns?

    A grace timer never does: every spelling ``quota`` uses -- ``6days``,
    ``13:20``, ``2weeks``, ``none`` -- fails :func:`parse_size`, while every
    numeric column passes it under either header (``1048576`` under ``blocks``,
    ``1000M`` under ``space``, a bare count for files), with an optional ``*``
    marking an exceeded limit. That asymmetry is what lets a row with one grace
    timer be read instead of discarded.
    """
    return parse_size(tok.rstrip("*")) is not None


# `Disk quotas for user someone (uid 1000):` -- the line that says whose figures
# the table below belongs to.
_QUOTA_SCOPE_RE = re.compile(r"disk\s+quotas\s+for\s+(user|group|project)\b", re.IGNORECASE)

# `Disk quotas for user me (uid 1000): none` -- what `quota` prints for an account
# that has no quota anywhere. It is an *answer*, and reporting it as "could not
# parse `quota -s` output" blames a command that worked for a failure that did not
# happen -- the same wrong-cause mistake as reporting a broken wrapper as "not on
# PATH". For a tool whose question is "why is my quota full", "you have no quota"
# is a useful reply.
_QUOTA_NONE_RE = re.compile(
    r"disk\s+quotas\s+for\s+\w+\s+[^\n:]*:\s*none\s*$", re.IGNORECASE | re.MULTILINE
)


def _parse_stock_quota(out: str) -> List[QuotaRow]:
    """Stock ``quota`` layout: a ``Filesystem`` header then one row per fs.

    The block column is headed ``blocks`` by plain ``quota`` and ``space`` by
    ``quota -s``. **That header also states the unit**: under ``blocks`` the
    figures are raw 1 KiB blocks, under ``space`` they are human-readable with a
    suffix. Reading a ``blocks`` figure as bytes under-reports by 1024x -- a
    30 GiB home quota would print as 30 MiB -- so the header decides the scale.

    Three things this got wrong, all of which discarded the row that mattered:

    **Every table, not just the first.** It ``break``\\ ed out after one
    ``Filesystem`` header, so the ``Disk quotas for group ...`` section was
    silently dropped -- and a group quota is routinely the binding limit on a
    shared project directory, which is where an HPC user actually runs out.

    **The scope is read, not assumed.** Every row was hard-coded ``user``, so even
    if the group table had been parsed its rows would have claimed to be personal
    ones, and ``reconcile._pick_row`` prefers user-scoped rows -- it would have
    compared a group figure against one user's walk.

    **A row with one grace timer is read.** Graces print only for an exceeded
    limit, so 6 figures means nothing is over, 8 means both are, and **7 means
    exactly one is** -- the commonest over-quota shape there is. Counting fields
    could not tell which of the two was present, so the row was skipped and the
    loop stopped, which meant ``quota -s`` parsed to zero rows precisely when the
    user was over. The position of the non-numeric token settles it: index 3 is a
    block grace, index 6 a file grace. See :func:`_is_figure`.
    """
    rows = []  # type: List[QuotaRow]
    lines = out.splitlines()
    table = read_mount_table()
    points = _mount_points()
    scope = "user"
    for i, line in enumerate(lines):
        found = _QUOTA_SCOPE_RE.search(line)
        if found:
            scope = found.group(1).lower()
            continue
        if "Filesystem" not in line:
            continue
        if "blocks" not in line and "space" not in line:
            continue
        kib_units = "space" not in line

        def block_bytes(tok, kib=kib_units):
            v = parse_size(tok.rstrip("*"))
            if v is None:
                return None
            return v * 1024 if kib else v

        pending = None  # type: Optional[str]
        for nxt in lines[i + 1 :]:
            parts = nxt.split()
            if not parts:
                break
            # `quota` wraps a long device name onto its own line and indents the
            # figures onto the next. Requiring name-and-numbers on one line lost
            # every row at sites whose device names are long, which is most NFS.
            if len(parts) == 1 and not _is_figure(parts[0]):
                pending = parts[0]
                continue
            if pending is not None:
                fs, nums = pending, parts
                pending = None
            else:
                fs, nums = parts[0], parts[1:]
            # Six figures plus a grace timer for each limit that is over. The end
            # of the table is anything else -- including the next section's
            # `Disk quotas for group ...`, whose tokens are not figures. The
            # previous test, "the first column starts with /", rejected the whole
            # table at any site whose device is `server:/export` or `//host/share`,
            # i.e. every NFS and CIFS mount, before a single number was read.
            graces = [n for n, tok in enumerate(nums) if not _is_figure(tok)]
            if len(nums) == 6 and not graces:
                bidx, fidx, bgrace, fgrace = 0, 3, "", ""
            elif len(nums) == 7 and graces == [3]:
                bidx, fidx, bgrace, fgrace = 0, 4, nums[3], ""
            elif len(nums) == 7 and graces == [6]:
                bidx, fidx, bgrace, fgrace = 0, 3, "", nums[6]
            elif len(nums) >= 8 and graces[:2] == [3, 7]:
                bidx, fidx, bgrace, fgrace = 0, 4, nums[3], nums[7]
            else:
                break
            blocks = block_bytes(nums[bidx])
            bsoft = block_bytes(nums[bidx + 1])
            bhard = block_bytes(nums[bidx + 2])
            if blocks is None:
                break
            files = None  # type: Optional[int]
            fsoft = None  # type: Optional[int]
            fhard = None  # type: Optional[int]
            try:
                files = int(nums[fidx].rstrip("*"))
                fsoft = int(nums[fidx + 1])
                fhard = int(nums[fidx + 2])
            except ValueError:
                files = fsoft = fhard = None
            # The first column of stock `quota` is a *device*, never a directory,
            # so testing it with `isdir` mapped nothing and every correctly
            # parsed row was dropped for want of a mount. /proc/mounts is keyed
            # by exactly that string and already knows the answer.
            mounts = list(table.get(fs) or [])
            guessed = False
            if not mounts and os.path.isdir(fs):
                mounts = [os.path.abspath(fs)]
            if not mounts:
                inferred = _guess_mount(fs, points)
                if inferred:
                    mounts = [inferred]
                    guessed = True
            mount = mounts[0] if mounts else None
            for kind, used, soft, hard, grace in (
                ("blocks", blocks, bsoft, bhard, _clean_grace(bgrace)),
                ("files", files, fsoft, fhard, _clean_grace(fgrace)),
            ):
                if used is None:
                    continue
                row = QuotaRow(
                    fs, kind, scope, used, soft or None, hard or None, grace, mount, guessed
                )
                row.mounts = list(mounts)
                rows.append(row)
    return rows


# GPFS is a multi-filesystem product and `mmlsquota` is a *per-filesystem*
# query, so a site with no filesystem-wide default needs one call per device.
# Seven GPFS devices in /proc/mounts is an ordinary cluster login node (measured
# on midway2) and each call is a full mm-command round trip, so the fan-out is
# capped and every call draws on the one deadline `_budget` is already holding.
_MAX_MMLSQUOTA_DEVICES = 8

# Diagnostics that mean "this output is not a record set", whatever the exit
# status says. This matters because `mmlsquota` **exits 0 while failing**:
#
#     $ mmlsquota -Y; echo "rc=$?"
#     No quota enabled file system found.
#     mmlsquota: tslsquota  -Y  failed. Error code 22.
#     mmlsquota: Command failed. Examine previous error messages to determine cause.
#     rc=0
#
# so `rc` cannot be the success signal. Today those lines go to stderr and the
# emptiness of stdout is what saves the parser -- which is incidental, not
# designed: a build that wrote them to stdout would have them parsed as records.
# Success is therefore defined positively, by the `:HEADER:` line the -Y format
# always emits, and these markers veto the output wherever they appear on a line
# that is not itself a record (see `_MMLSQUOTA_RECORD_FIELDS`).
_MMLSQUOTA_FAILURES = (
    "no quota enabled file system found",
    "command failed",
    "failed. error code",
    "not permitted",
    "no such file or directory",
)


# A `-Y` record carries around twenty colon-separated fields; a diagnostic carries
# one or two ("mmlsquota: Command failed. ..."). Requiring a line to be short in
# that sense before it can veto the output means a fileset or remarks field whose
# text happens to contain one of the markers cannot discard a whole good reading.
_MMLSQUOTA_RECORD_FIELDS = 5


def _device_order(devices: List[str], table: Dict[str, List[str]], path: str) -> List[str]:
    """GPFS devices, the one governing ``path`` first, then the rest by name.

    The fan-out is capped, and a cap only stays harmless while the device that
    can answer the caller's question is inside it. Eight is comfortably above
    midway2's seven, but a site with twenty GPFS filesystems is not exotic, and
    alphabetical order there would decide by luck whether the walked path's own
    quota got asked for at all -- reporting "no quota" about a filesystem nobody
    queried. Ordering by which device's mounts actually contain ``path`` makes the
    cap a bound on *cost* rather than on correctness.

    This orders the query; it does not scope it. Every device inside the cap is
    still asked, and rows are still mapped to paths afterwards by the mount table.
    """
    target = os.path.abspath(path or "/")

    def rank(device: str) -> Tuple[int, str]:
        covers = -1
        for mount in table.get(device) or []:
            stem = mount.rstrip("/") or "/"
            if target == stem or target.startswith(stem.rstrip("/") + "/"):
                covers = max(covers, len(stem))
        # Longest containing mount first, so the most specific filesystem leads.
        return (-covers, device)

    return sorted(devices, key=rank)


def _mmlsquota_trouble(text: str) -> str:
    """The diagnostic line in ``text`` that says this is not data, if there is one."""
    for line in (text or "").splitlines():
        if len(line.split(":")) >= _MMLSQUOTA_RECORD_FIELDS:
            continue
        low = line.lower()
        for marker in _MMLSQUOTA_FAILURES:
            if marker in low:
                return line.strip()
    return ""


def _parse_mmlsquota(out: str, table: Dict[str, List[str]], points: List[str]) -> List[QuotaRow]:
    """Every ``-Y`` record in ``out``, as rows mapped through the mount table."""
    rows = []  # type: List[QuotaRow]
    header = None  # type: Optional[List[str]]
    for line in out.splitlines():
        fields = line.split(":")
        if len(fields) < 3:
            continue
        if fields[2] == "HEADER":
            header = fields
            continue
        if header is None:
            continue
        # Keyed case-insensitively: the column GPFS calls `filesetName` in its
        # own documentation prints as `filesetname` on some builds, and an
        # exact-case lookup silently returns "" there -- which would name every
        # fileset-scoped row after its filesystem, merging two labs' quotas under
        # one label. The field names are a vendor's spelling of a vendor's
        # column, not data; normalise them and stop depending on the spelling.
        rec = dict(zip([f.strip().lower() for f in header], fields))
        try:
            block_used = int(rec.get("blockusage", "0")) * 1024
            block_soft = int(rec.get("blockquota", "0")) * 1024
            block_hard = int(rec.get("blocklimit", "0")) * 1024
            file_used = int(rec.get("filesusage", "0"))
            file_soft = int(rec.get("filesquota", "0"))
            file_hard = int(rec.get("fileslimit", "0"))
        except (TypeError, ValueError):
            continue
        name = rec.get("filesystemname", "?")
        scope = _norm_scope(rec.get("quotatype", ""))
        # A fileset-scoped row is named by its fileset, not by the filesystem;
        # the fileset name is what distinguishes two labs sharing one mount.
        if scope == "fileset":
            name = rec.get("filesetname", "") or name
        mounts = _mounts_for(rec.get("filesystemname", "?"), table, points)
        mount = mounts[0] if mounts else None
        block_grace = _clean_grace(rec.get("blockgrace", ""))
        file_grace = _clean_grace(rec.get("filesgrace", ""))
        for kind, used, soft, hard, grace in (
            ("blocks", block_used, block_soft, block_hard, block_grace),
            ("files", file_used, file_soft, file_hard, file_grace),
        ):
            row = QuotaRow(name, kind, scope, used, soft or None, hard or None, grace, mount)
            row.mounts = list(mounts)
            rows.append(row)
    return rows


def read_mmlsquota(
    path: str, timeout: float = DEFAULT_TIMEOUT_S, deadline: Optional[float] = None
) -> QuotaSnapshot:
    """GPFS ``mmlsquota -Y``: the default filesystem, then every device by name.

    Not present on every GPFS site (it is frequently root-only or simply not
    installed on login nodes), which is why it is one backend among several.

    **Bare ``mmlsquota -Y`` asks for "the default quota-enabled filesystem", and
    a great many sites have none.** Wherever quotas are set per *fileset* rather
    than filesystem-wide -- a normal GPFS configuration -- the bare call returns
    no records at all, and rapidu reported that as "no quota" on a cluster where
    the user's home quota was fully readable. It just never named a device.
    Naming one works, needs no privileges, and answers exactly what this module
    wants::

        $ mmlsquota -Y -u youzhi midway2_perf2
        mmlsquota:user:HEADER:...:filesystemName:...:blockUsage:...:filesetname:
        mmlsquota:user:0:1:::midway2_perf2:USR:...:1916224:31457280:36700160:...:home:

    So the bare call is still tried first -- it is one round trip and it is right
    at any site that has a default -- and only if it yields nothing do we
    enumerate the GPFS devices from ``/proc/mounts`` and ask each in turn. The
    fan-out is capped (:data:`_MAX_MMLSQUOTA_DEVICES`) and shares one deadline,
    and it stops early when the command turns out to be missing, since the next
    seven calls cannot go any better than the first.

    Fileset enumeration would be the tidier way to do this and it is not
    available: ``mmlsfileset`` is root-only at every site checked ("Non root user
    is not permitted to run with the specified option(s)"). ``-u <user>`` is the
    query an unprivileged user can actually make, which is also why the per-device
    fan-out asks only for the user scope -- a second pass for groups would double
    a cost that is already multiplied by the device count, and a user-scoped row
    is what :func:`reconcile._pick_row` prefers anyway.

    ``path`` does not scope the query -- the result is still every row every
    device published, mapped to paths afterwards through the mount table. It only
    *orders* the fan-out, so that the filesystem holding ``path`` is asked before
    the cap can be reached (:func:`_device_order`). An earlier version took the
    parameter, ignored it entirely, and read as though it scoped the query; this
    is the one thing it is legitimately good for.
    """
    snap = QuotaSnapshot("mmlsquota")
    table = read_mount_table()
    points = _mount_points()
    user = _current_user()
    attempts = [["mmlsquota", "-Y"]]
    devices = _device_order(_devices_of_type(("gpfs",)), table, path)
    for device in devices[:_MAX_MMLSQUOTA_DEVICES]:
        attempts.append(["mmlsquota", "-Y", "-u", user, device])
    # (device, message), so a mixed set of per-device failures can say which
    # filesystem produced which. See `_grouped_failures`.
    failures = []  # type: List[Tuple[str, str]]
    tool_failure = ""
    if len(devices) > _MAX_MMLSQUOTA_DEVICES:
        # Said out loud. A bound that silently drops filesystems reads as "this
        # site has no quota" when what happened is that nobody asked.
        # No subject: this is about the fan-out, not about any one device.
        failures.append(
            (
                "",
                "{} of {} GPFS filesystems were asked ({} first, as it holds this "
                "path); raise the cap to ask the rest".format(
                    _MAX_MMLSQUOTA_DEVICES, len(devices), devices[0]
                ),
            )
        )
    for index, cmd in enumerate(attempts):
        if _budget(timeout, deadline) <= 0:
            failures.append(("", "the quota budget ran out before every filesystem was asked"))
            break
        rc, out, err = _run(cmd, _budget(timeout, deadline))
        if not snap.raw:
            snap.raw = out
        if rc == 127:
            # About the command, not any one device, and the rest cannot go better.
            tool_failure = _explain_127("mmlsquota", err)
            break
        trouble = _mmlsquota_trouble(out) or _mmlsquota_trouble(err)
        if trouble or ":HEADER:" not in out:
            # `default` for the bare call, which names no device at all.
            failures.append(
                (
                    cmd[-1] if index else "default",
                    _one_line(trouble or err or "no records (rc={})".format(rc)),
                )
            )
            continue
        rows = _parse_mmlsquota(out, table, points)
        snap.rows.extend(rows)
        if index == 0 and rows:
            # The site has a default filesystem and it answered. Asking every
            # device as well would re-read the same figures seven more times.
            break
    snap.available = bool(snap.rows)
    # `mmlsquota` is a live query, not a cached report, so the figure is as
    # current as the filesystem's own accounting. Leaving `taken_at` unset made
    # `age_seconds` None, which permanently tripped reconcile's "published no
    # timestamp" blocker: on a GPFS-native site every verdict was downgraded to
    # INCONCLUSIVE and `GAP` -- with it `EXIT_ATTENTION` -- was unreachable.
    if snap.available:
        snap.taken_at = snap.read_at
        snap.time_note = (
            "read live from mmlsquota; GPFS quota accounting itself can lag "
            "writes by up to a minute, so treat a small difference as timing"
        )
    if not snap.available:
        # Eight devices failing the same way is one fact, not eight -- and three
        # failing differently is three facts that each need their device named.
        snap.reason = (
            tool_failure or _grouped_failures(failures) or "mmlsquota returned no parseable rows"
        )
    return snap


def _lfs_project_id(path: str, timeout: float, deadline: Optional[float] = None) -> Optional[str]:
    """The Lustre project id ``path`` is charged to, if it carries one.

    ``lfs project -d <dir>`` prints ``<projid> <flags> <path>``. Project id 0 is
    "no project", which is not a quota worth asking about.
    """
    rc, out, _ = _run(["lfs", "project", "-d", path], _budget(timeout, deadline))
    if rc != 0:
        return None
    parts = out.split()
    if parts and parts[0].isdigit() and parts[0] != "0":
        return parts[0]
    return None


# What `lfs quota` prints when it could not reach every OST or MDT. The figures
# it could not verify come back wrapped in brackets, and this line explains them.
_LFS_INACCURATE = 'the data in "[]" is inaccurate'


def _lfs_unverified(out: str) -> bool:
    """Did ``lfs`` disown any of the figures it just printed?

    Two signals, either of which is enough: the warning line it prints when a
    device is down or deactivated, and a bracketed figure. Both are ordinary on a
    Lustre site with a degraded OST -- which, as :func:`_budget` notes, is the
    same afternoon someone reaches for this tool.
    """
    if _LFS_INACCURATE in out.lower():
        return True
    return any("[" in line and "]" in line for line in out.splitlines()[2:])


def _lfs_figure(token: str) -> str:
    """One numeric column, with the marks ``lfs`` decorates it with removed.

    ``*`` marks a figure over its limit. ``[N]`` marks one ``lfs`` could not
    verify -- and stripping the brackets rather than failing on them is the whole
    point: ``int("[1048576]")`` raises, which dropped the row, which reported a
    *readable* quota as "could not parse `lfs quota` output". An uncertain figure
    carried with a caveat beats no figure at all; see :func:`_lfs_unverified`.
    """
    return token.strip().strip("[]").rstrip("*")


def _parse_lfs_rows(out: str, scope: str, path: str) -> List[QuotaRow]:
    """The one data line under Lustre's ``Filesystem ... kbytes`` header.

    Two things here were wrong and both produced a confident wrong answer.

    **The mount point.** The queried path was stored as the row's mount, so
    ``reconcile``'s test for "does this walk cover the whole quota'd tree"
    (``root == row.mount``) was true by construction. The ``SUBTREE`` verdict --
    the one that exists precisely to say *you walked a subdirectory of a much
    larger quota'd filesystem, the difference is expected* -- became unreachable
    on Lustre, and every ``rdu -a <subdir>`` reported the rest of the filesystem
    as a difference needing explanation. ``lfs`` publishes the filesystem's own
    mount point in the first column; that is what a mount point is.

    **Wrapped rows.** ``lfs`` wraps when the filesystem path is long, putting the
    name on its own line and the eight numbers on the next. Requiring nine
    fields on one line meant those sites parsed to zero rows -- no quota at all,
    reported as "could not parse", on precisely the paths whose names are long
    enough to wrap.
    """
    rows = []  # type: List[QuotaRow]
    lines = [ln for ln in out.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if "Filesystem" not in line or "kbytes" not in line:
            continue
        rest = lines[i + 1 :]
        if not rest:
            break
        parts = rest[0].split()
        # The unwrapped form is name + 8 numbers. The wrapped form is the name
        # alone, then the 8 numbers; `lfs` indents the continuation line.
        if len(parts) == 1 and len(rest) > 1:
            fsname = parts[0]
            parts = [fsname] + rest[1].split()
        if len(parts) < 9:
            break
        fsname = parts[0]
        # `lfs` names the filesystem by its mount point. Fall back to the kernel
        # when it does not, and to nothing at all when neither can say -- an
        # unmapped row is honest, a row mounted at the walk root is not.
        mount = fsname if fsname.startswith("/") else _enclosing_mount(path, ("lustre",))
        try:
            rows.append(
                QuotaRow(
                    os.path.basename(mount.rstrip("/")) or fsname,
                    "blocks",
                    scope,
                    int(_lfs_figure(parts[1])) * 1024,
                    int(_lfs_figure(parts[2])) * 1024 or None,
                    int(_lfs_figure(parts[3])) * 1024 or None,
                    _clean_grace(parts[4]),
                    mount or None,
                )
            )
            rows.append(
                QuotaRow(
                    os.path.basename(mount.rstrip("/")) or fsname,
                    "files",
                    scope,
                    int(_lfs_figure(parts[5])),
                    int(_lfs_figure(parts[6])) or None,
                    int(_lfs_figure(parts[7])) or None,
                    _clean_grace(parts[8]),
                    mount or None,
                )
            )
        except ValueError:
            pass
        break
    return rows


def read_lfs_quota(
    path: str, timeout: float = DEFAULT_TIMEOUT_S, deadline: Optional[float] = None
) -> QuotaSnapshot:
    """Lustre quotas for ``path``: user, group, and -- crucially -- project.

    Reading only ``-u`` was wrong in the case that matters. **Lustre project
    quotas are the standard mechanism for per-directory and per-lab allocations
    at Lustre sites**, and they are what a shared research directory is charged
    against. A user over their project quota who ran this got their personal
    user quota reported instead: a real number, correctly parsed, that is not the
    one stopping them from writing.

    All three scopes are collected because they are three different limits and
    any of them can be the binding one. ``reconcile._pick_row`` prefers the
    user-scoped row where several apply, which stays the right default.
    """
    snap = QuotaSnapshot("lfs quota")
    user = _current_user()
    scopes = [("user", ["-u", user])]
    try:
        import grp

        scopes.append(("group", ["-g", grp.getgrgid(os.getgid()).gr_name]))
    except (ImportError, KeyError, OSError):
        pass
    projid = _lfs_project_id(path, timeout, deadline)
    if projid:
        scopes.append(("project", ["-p", projid]))

    # (scope, detail) rather than a pre-joined string, so identical failures can be
    # collapsed *with their scopes named* rather than either repeated or anonymous.
    failures = []  # type: List[Tuple[str, str]]
    tool_failure = ""
    for scope, flags in scopes:
        rc, out, err = _run(["lfs", "quota"] + flags + [path], _budget(timeout, deadline))
        if not snap.raw:
            snap.raw = out
        if rc == 127:
            # The command is absent, or a wrapper around something absent. Either
            # way the other scopes will fail identically, so this is about the tool
            # rather than about any scope: no prefix, and stop asking.
            tool_failure = _explain_127("lfs", err)
            break
        if rc != 0 or not out.strip():
            # The scope is named on *every* failure, not only when stderr is empty.
            # The `or` this replaces used it as a fallback, so in the ordinary case
            # -- a tool that failed and said why -- the scope was discarded in
            # favour of the raw stderr, which is exactly when knowing which of
            # user/group/project was being asked would help. Two scopes failing
            # differently rendered as "cannot find quota for user; Operation not
            # permitted" with nothing to say the second was the group query.
            failures.append((scope, _one_line(err) or "rc={}".format(rc)))
            continue
        snap.rows.extend(_parse_lfs_rows(out, scope, path))
        if _lfs_unverified(out) and not snap.figure_note:
            snap.figure_note = (
                "lfs quota could not reach every device and marked some figures "
                "inaccurate, so these numbers are a reading of an incomplete "
                "filesystem -- check `lfs check servers`"
            )

    snap.available = bool(snap.rows)
    # A live query, like mmlsquota: see the note there for why leaving this unset
    # made `GAP` unreachable on every Lustre site.
    if snap.available:
        snap.taken_at = snap.read_at
        snap.time_note = (
            "read live from lfs quota; Lustre accounting is updated "
            "asynchronously by the OSTs, so treat a small difference as timing"
        )
    if not snap.available:
        # Grouped by message, so three scopes failing the same way read once with
        # all three named -- "user, group: MDS unreachable" -- instead of the same
        # sentence three times, or once with no idea which scope it came from.
        # Ordered explicitly rather than by dict insertion: 3.6 happens to preserve
        # it, but the guarantee only arrives in 3.7 and this package supports 3.6.
        snap.reason = (
            tool_failure or _grouped_failures(failures) or "could not parse `lfs quota` output"
        )
    return snap


def read_best(path: str, timeout: float = DEFAULT_TIMEOUT_S) -> QuotaSnapshot:
    """The backend that can answer for ``path``, else any that answered at all.

    **Not first-success-wins.** A very common HPC layout is an NFS or local
    ``$HOME`` beside a Lustre or GPFS scratch. There ``quota -s`` reports the
    home and knows nothing of the parallel filesystem -- but it produced *rows*,
    so a first-success rule returns it and never runs the backend that could
    have answered. A walk of the scratch path then reports "no quota row maps to
    this path" while ``lfs quota``, one call below in this same function, was
    holding the answer. The failure is silent and it removes reconciliation
    entirely on exactly the filesystem the user is over quota on.

    So a backend wins by mapping the path the caller actually asked about. A
    backend that merely produced rows is kept only as the fallback, which is
    still the right answer for ``-Q`` with no path.

    ``timeout`` is the budget for **all** of this, not for each subprocess within
    it. Backends run in sequence and one of them may run several commands, so a
    per-call timeout made the worst case their sum: 225s of silence at the 45s
    default, before the walk has started and with no spinner running.
    """
    attempts = []  # type: List[QuotaSnapshot]
    fallback = None  # type: Optional[QuotaSnapshot]
    deadline = time.time() + timeout
    for reader in (
        lambda: read_quota_command(timeout, deadline),
        lambda: read_mmlsquota(path, timeout, deadline),
        lambda: read_lfs_quota(path, timeout, deadline),
    ):
        if _budget(timeout, deadline) <= 0 and attempts:
            break
        snap = reader()
        attempts.append(snap)
        if not snap.available:
            continue
        if snap.rows_for_path(path):
            return snap
        if fallback is None:
            fallback = snap
    if fallback is not None:
        return fallback
    merged = attempts[0]
    merged.reason = _one_line(
        "; ".join("{}: {}".format(a.source, a.reason or "unavailable") for a in attempts)
    )
    if _budget(timeout, deadline) <= 0:
        merged.reason += "; the {:.0f}s quota budget was exhausted".format(timeout)
    return merged
