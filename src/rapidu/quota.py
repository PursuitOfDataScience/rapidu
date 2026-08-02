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
# ">>> Capacity Filesystem: project (Midway3 GPFS mounted at /project)"
_MOUNT_RE = re.compile(r"mounted\s+at\s+(/\S+?)\)?\s*$", re.IGNORECASE)


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
    def usage_fraction(self) -> Optional[float]:
        limit = self.soft or self.hard
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


def _guess_mount(fileset: str) -> Optional[str]:
    """Best-effort mount for a fileset named without an explicit mount line.

    Only returns a path that actually exists, so a wrong guess degrades to
    "unmapped" rather than to a confident mis-attribution. Guesses are then run
    through :func:`_disambiguate_mounts`, which drops any that collide.
    """
    name = fileset.strip().lower()
    candidates = []  # type: List[str]
    if name.endswith("-home") or name == "home":
        candidates.append(os.path.expanduser("~"))
        candidates.append("/home")
    if "/" in name:
        candidates.append("/" + name)
    candidates.append("/" + name.replace("-", "/"))
    for c in candidates:
        if c and os.path.isdir(c):
            return os.path.abspath(c)
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
    try:
        with open(path) as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 2:
                    continue
                # Mount points are octal-escaped for spaces and tabs.
                point = fields[1].replace("\\040", " ").replace("\\011", "\t")
                table.setdefault(fields[0], []).append(point)
    except (OSError, UnicodeDecodeError):
        return {}
    for mounts in table.values():
        mounts.sort(key=lambda m: (len(m), m))
    return table


def _mounts_for(name: str, table: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """Mount points for a filesystem name, by device match then by name match."""
    table = read_mount_table() if table is None else table
    if name in table:
        return list(table[name])
    # Some backends name the filesystem without the device prefix it is mounted
    # under; fall back to the old inference, which is right when it is right.
    guess = "/" + name
    return [guess] if os.path.isdir(guess) else []


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


def read_quota_command(timeout: float = DEFAULT_TIMEOUT_S) -> QuotaSnapshot:
    """Parse ``quota -s``, including site wrappers that print a table.

    Handles the tabular form used by RCC's ``systool.quota9`` wrapper:

        fileset          type                   used      quota      limit    grace
        Midway3-home     blocks (user)       679.66M     30.00G     35.00G     none
        rcc              files  (group)     43583258  230900000  231900000     none

    with ``>>> ... (Midway3 GPFS mounted at /project)`` section headers that
    supply the mount point for the rows beneath them.
    """
    snap = QuotaSnapshot("quota -s")
    rc, out, err = _run(["quota", "-s"], timeout)
    snap.raw = out
    if rc == 127:
        snap.reason = "`quota` is not on PATH"
        return snap
    if rc == 124:
        snap.reason = err
        return snap
    if not out.strip():
        snap.reason = err.strip() or "`quota -s` produced no output"
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

    current_mount = None  # type: Optional[str]
    for line in out.splitlines():
        s = line.strip()
        if not s or s.startswith("---") or s.lower().startswith("fileset"):
            continue
        if s.startswith(">>>"):
            mm = _MOUNT_RE.search(s)
            current_mount = mm.group(1) if mm else None
            continue
        row = _parse_quota_row(s, current_mount)
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
        snap.reason = "could not parse `quota -s` output"
    return snap


def _parse_quota_row(line: str, mount: Optional[str]) -> Optional[QuotaRow]:
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
    grace = nums[3] if len(nums) > 3 else ""
    guessed = mount is None
    resolved = mount or _guess_mount(fileset)
    return QuotaRow(fileset, kind, scope, used, soft, hard, grace, resolved, guessed)


def _parse_stock_quota(out: str) -> List[QuotaRow]:
    """Stock ``quota`` layout: a ``Filesystem`` header then one row per fs.

    The block column is headed ``blocks`` by plain ``quota`` and ``space`` by
    ``quota -s``. **That header also states the unit**: under ``blocks`` the
    figures are raw 1 KiB blocks, under ``space`` they are human-readable with a
    suffix. Reading a ``blocks`` figure as bytes under-reports by 1024x -- a
    30 GiB home quota would print as 30 MiB -- so the header decides the scale.

    Grace columns are printed only when a limit is exceeded, so a data row
    carries 7 fields normally and 9 when both graces are present. An 8-field row
    is ambiguous -- which of the two graces is present cannot be determined --
    and is skipped rather than guessed at.
    """
    rows = []  # type: List[QuotaRow]
    lines = out.splitlines()
    for i, line in enumerate(lines):
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

        for nxt in lines[i + 1 :]:
            parts = nxt.split()
            if len(parts) < 7 or not parts[0].startswith("/"):
                break
            if len(parts) == 7:
                bidx, fidx = 1, 4
            elif len(parts) >= 9:
                bidx, fidx = 1, 5
            else:
                continue
            fs = parts[0]
            blocks = block_bytes(parts[bidx])
            bsoft = block_bytes(parts[bidx + 1])
            bhard = block_bytes(parts[bidx + 2])
            files = None  # type: Optional[int]
            fsoft = None  # type: Optional[int]
            fhard = None  # type: Optional[int]
            try:
                files = int(parts[fidx].rstrip("*"))
                fsoft = int(parts[fidx + 1])
                fhard = int(parts[fidx + 2])
            except ValueError:
                files = fsoft = fhard = None
            mount = fs if os.path.isdir(fs) else None
            if blocks is not None:
                rows.append(
                    QuotaRow(fs, "blocks", "user", blocks, bsoft or None, bhard or None, "", mount)
                )
            if files is not None:
                rows.append(
                    QuotaRow(fs, "files", "user", files, fsoft or None, fhard or None, "", mount)
                )
        break
    return rows


def read_mmlsquota(path: str, timeout: float = DEFAULT_TIMEOUT_S) -> QuotaSnapshot:
    """GPFS ``mmlsquota -Y``, reporting every filesystem it knows about.

    Not present on every GPFS site (it is frequently root-only or simply not
    installed on login nodes), which is why it is one backend among several.

    ``path`` selects nothing: ``mmlsquota -Y`` is filesystem-wide and the caller
    maps rows to paths afterwards through the mount table. The parameter is kept
    because every backend in this module takes one, but it is documented as
    unused rather than silently accepted -- an earlier version took it, ignored
    it, and read as though it scoped the query.
    """
    snap = QuotaSnapshot("mmlsquota")
    table = read_mount_table()
    rc, out, err = _run(["mmlsquota", "-Y"], timeout)
    snap.raw = out
    if rc != 0 or not out.strip():
        snap.reason = err.strip() or "mmlsquota unavailable (rc={})".format(rc)
        return snap
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
        rec = dict(zip(header, fields))
        try:
            block_used = int(rec.get("blockUsage", "0")) * 1024
            block_soft = int(rec.get("blockQuota", "0")) * 1024
            block_hard = int(rec.get("blockLimit", "0")) * 1024
            file_used = int(rec.get("filesUsage", "0"))
            file_soft = int(rec.get("filesQuota", "0"))
            file_hard = int(rec.get("filesLimit", "0"))
        except (TypeError, ValueError):
            continue
        name = rec.get("filesystemName", "?")
        scope = (rec.get("quotaType", "") or "").lower()
        mounts = _mounts_for(name, table)
        mount = mounts[0] if mounts else None
        for kind, used, soft, hard in (
            ("blocks", block_used, block_soft, block_hard),
            ("files", file_used, file_soft, file_hard),
        ):
            row = QuotaRow(name, kind, scope, used, soft or None, hard or None, "", mount)
            row.mounts = list(mounts)
            snap.rows.append(row)
    snap.available = bool(snap.rows)
    if not snap.available:
        snap.reason = "mmlsquota returned no parseable rows"
    return snap


def _lfs_project_id(path: str, timeout: float) -> Optional[str]:
    """The Lustre project id ``path`` is charged to, if it carries one.

    ``lfs project -d <dir>`` prints ``<projid> <flags> <path>``. Project id 0 is
    "no project", which is not a quota worth asking about.
    """
    rc, out, _ = _run(["lfs", "project", "-d", path], timeout)
    if rc != 0:
        return None
    parts = out.split()
    if parts and parts[0].isdigit() and parts[0] != "0":
        return parts[0]
    return None


def _parse_lfs_rows(out: str, scope: str, path: str) -> List[QuotaRow]:
    """The one data line under Lustre's ``Filesystem ... kbytes`` header."""
    rows = []  # type: List[QuotaRow]
    lines = [ln for ln in out.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if "Filesystem" not in line or "kbytes" not in line:
            continue
        if i + 1 >= len(lines):
            break
        parts = lines[i + 1].split()
        if len(parts) >= 9:
            try:
                rows.append(
                    QuotaRow(
                        parts[0],
                        "blocks",
                        scope,
                        int(parts[1].rstrip("*")) * 1024,
                        int(parts[2]) * 1024 or None,
                        int(parts[3]) * 1024 or None,
                        "",
                        path,
                    )
                )
                rows.append(
                    QuotaRow(
                        parts[0],
                        "files",
                        scope,
                        int(parts[5].rstrip("*")),
                        int(parts[6]) or None,
                        int(parts[7]) or None,
                        "",
                        path,
                    )
                )
            except ValueError:
                pass
        break
    return rows


def read_lfs_quota(path: str, timeout: float = DEFAULT_TIMEOUT_S) -> QuotaSnapshot:
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
    user = os.environ.get("USER") or str(os.getuid())
    scopes = [("user", ["-u", user])]
    try:
        import grp

        scopes.append(("group", ["-g", grp.getgrgid(os.getgid()).gr_name]))
    except (ImportError, KeyError, OSError):
        pass
    projid = _lfs_project_id(path, timeout)
    if projid:
        scopes.append(("project", ["-p", projid]))

    failures = []  # type: List[str]
    for scope, flags in scopes:
        rc, out, err = _run(["lfs", "quota"] + flags + [path], timeout)
        if not snap.raw:
            snap.raw = out
        if rc != 0 or not out.strip():
            failures.append(err.strip() or "{} (rc={})".format(scope, rc))
            continue
        snap.rows.extend(_parse_lfs_rows(out, scope, path))

    snap.available = bool(snap.rows)
    if not snap.available:
        snap.reason = "; ".join(failures) or "could not parse `lfs quota` output"
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
    """
    attempts = []  # type: List[QuotaSnapshot]
    fallback = None  # type: Optional[QuotaSnapshot]
    for reader in (
        lambda: read_quota_command(timeout),
        lambda: read_mmlsquota(path, timeout),
        lambda: read_lfs_quota(path, timeout),
    ):
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
    merged.reason = "; ".join(
        "{}: {}".format(a.source, a.reason or "unavailable") for a in attempts
    )
    return merged
