"""Rendering. Plain text by default (it goes in a ticket), ``--json`` for tools.

Two rules govern everything printed here:

* An absent measurement prints ``n/a`` **with a reason**. It never prints ``0``.
* Any figure derived from more than one source prints the age and completeness
  of both, so a reader can see whether the comparison was safe.
"""

import grp
import os
import pwd
import shutil
from typing import Any, Dict, List, Optional, Set, Tuple  # noqa: F401  (`# type:` use)

from . import quota as quotamod
from . import reconcile as rc
from . import ui
from . import walk as walkmod
from .deleted import DeletedScan
from .fmt import (
    files_per_gib,
    human_bytes,
    human_count,
    human_duration,
    noun,
    pct,
    plural,
    ratio_x,
)
from .quota import QuotaSnapshot
from .walk import SettleCheck, WalkResult

# The width the report lays its *elastic* content out to, which is not the width
# of the terminal.
#
# Prose can wrap anywhere. A table row, a path, a bar cannot -- they have a
# natural width and breaking them damages them. So the report's width has to be
# decided by the inelastic content and the elastic content has to conform, or one
# wrapped sentence sets the width of everything else.
#
# It did. `ui.box` hugs the widest line it is given, prose wrapped to the
# terminal and the table laid itself out to its own natural width, so the same
# command in the same 125-column terminal drew an 84-column frame for a clean
# tree and a 125-column one the moment an allocation warning fired -- with the
# table stranded in a 41-column gutter beside it. Measured on this host at
# COLUMNS=125: table 80, prose 121. Two invocations a second apart looked like
# two different tools.
#
# 78 is not a new number. It is what this rule was already capped at, and the
# ranked table's rule is that plus its two-column indent; naming it once is what
# stops the three from drifting apart again. The frame stays free to grow past it
# for a long path, which is inelastic content and the answer being asked for.
_RULE_COLUMNS = 78
_LAYOUT_COLUMNS = _RULE_COLUMNS + 2


def _layout_width(style: "ui.Style") -> int:
    """Columns available to elastic content: the layout, bounded by the terminal.

    Bounded rather than fixed -- on a 60-column terminal the layout *is* 60 and
    prose still has to fit inside it. The cap only bites upwards.
    """
    return min(style.width, _LAYOUT_COLUMNS)


def _warn_wrapped(note: str, style: "ui.Style") -> "List[str]":
    """A ``!`` warning wrapped to the layout, with its continuations aligned.

    Hanging indent: the first line carries the ``! `` that makes a warning
    findable and the rest align under it. Letting :func:`ui.box` wrap it instead
    put the continuation at column zero, level with the report's own margin,
    where it read as a second unrelated statement.

    Written out twice already and needed a third time for the allocation
    headline, which was appended unwrapped -- 91 columns of prose that set the
    width of the whole frame on its own. See :data:`_LAYOUT_COLUMNS`.
    """
    import textwrap

    chunks = textwrap.wrap(note, max(40, _layout_width(style) - 4), break_on_hyphens=False)
    if not chunks:
        return []
    out = [ui.warn(chunks[0], style)]
    out.extend(style.paint("  " + extra, "yellow") for extra in chunks[1:])
    return out


def _section_rule(style: "ui.Style") -> str:
    """A section divider in the same glyph and grey as the table's own rule.

    Was a hard-coded 78 ASCII dashes, which ignored both the terminal width and
    the ``--ascii`` glyph decision -- so a unicode report carried one unicode rule
    and one ASCII one.
    """
    glyph = "\u2500" if style.unicode else "-"
    return style.paint(glyph * max(20, min(_RULE_COLUMNS, style.width - 1)), style.track)


def _uname(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _gname(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


# The report's three weights, named. Every emphasis decision below picks one of
# these and nothing else, because the failure mode is not choosing the wrong
# weight -- it is choosing a fourth one locally and ending up with several rules
# that each look like emphasis and collectively mean nothing.
#
#   ACCENT  the measurement this listing was ranked by. Exactly one per report,
#           and it moves when `-i` or `--sort` moves the ranking.
#   VALUE   any other real measurement -- and the *label* of the ranked column,
#           because emphasis is inherited from what a label names rather than
#           being a weight of its own.
#   LABEL   names of everything else, separators, caveats, hints. Never a number.
#
# In the table below, colour already means magnitude (the heat ramp), so the rows
# use `style.muted` for content that is not the ranked column and `style.track`
# for the bar's background. Those are the same three roles in the ramp's own
# vocabulary, not a fourth scheme.
ACCENT = "bold_cyan"
VALUE = "bold"
LABEL = "dim"

# The resolved escape parameters, for tests that assert on the rule rather
# than on how it happens to look.
ACCENT_SGR = "1;36"
VALUE_SGR = "1"
LABEL_SGR = "2"


def _unmeasured(res: WalkResult, value: "Any") -> "Any":
    """``value``, or ``None`` when ``-c`` skipped the stat that would have made it.

    Constraint 10, which :func:`fmt.human_bytes` states and enforces: *``None`` is
    not zero. A caller that has no measurement passes ``None`` and gets ``n/a``,
    never ``0.0 B``.* The terminal obeys it -- under ``-c`` the headline is ``n/a``
    and the byte column, ``BY AGE`` and ``SETTLING`` are absent entirely -- and
    the document did not, publishing ``size_bytes: 0``, ``by_age`` full of zeroes,
    ``reclaimable[].bytes: 0`` beside the terminal's ``n/a`` for the same figure,
    and ``settled: true`` about files written seconds earlier whose mtime was
    never read.

    Zero and unmeasured are not the same claim, and only one of them is true here:
    a consumer cannot tell an empty tree from a walk that took no sizes. One
    helper rather than a conditional at each site, because the sites disagreeing
    is how the document came to null `top_by_size` correctly while leaving
    `bytes: 0` on every row of `top_by_inodes`.
    """
    return None if res.count_only else value


def _owner_json(
    table: "Dict[int, Tuple[int, int]]",
    resolve: "Any",
    id_field: str,
    sized: bool = True,
) -> "Dict[str, Dict[str, Optional[int]]]":
    """``name -> {id_field, bytes, inodes}``: keyed by name, carrying the number.

    The key stays the name -- that is schema 1, and a person reads it -- but the
    numeric id is now beside it, because **the key is not stable across nodes.**
    ``pwd.getpwuid`` finds nothing on a compute node at some sites (measured on
    midway2, and the reason slurmwatch's headline finding exists), so the same
    tree serialises as ``{"youzhi": ...}`` from a login node and
    ``{"940740146": ...}`` from a batch job. A consumer joining or diffing two
    runs then sees two owners where there is one -- and joining runs is what a
    machine-readable document is for. The uid is the identifier that does not move.

    Collisions are no longer silent either. The dict comprehension this replaces
    let two ids resolving to one name overwrite each other, dropping one entry's
    bytes from the document altogether; every member of a colliding name is now
    suffixed with its id instead.
    """
    names = {}  # type: Dict[int, str]
    seen = {}  # type: Dict[str, int]
    for ident in table:
        name = resolve(ident)
        names[ident] = name
        seen[name] = seen.get(name, 0) + 1
    out = {}  # type: Dict[str, Dict[str, Optional[int]]]
    for ident, (nbytes, inodes) in table.items():
        name = names[ident]
        key = name if seen[name] == 1 else "{} ({})".format(name, ident)
        # Under `-c` the only byte figure in this table is the root directory's
        # own blocks, read once at walk start -- so it is not zero, it is a real
        # number describing one inode of the tree, which is worse. The terminal
        # drops the column; so does this.
        out[key] = {
            id_field: ident,
            "bytes": nbytes if sized else None,
            "inodes": inodes,
        }
    return out


def _counter(n: Optional[int]) -> str:
    """``human_count`` under the same signature as ``human_bytes``."""
    return human_count(n)


# `-n 0` means every entry. Large enough to be every entry of anything, small
# enough to slice a list with.
_ALL = 10**9


def _limit(top: int) -> int:
    """``-n`` as a slice bound. ``0`` means *all*, never *none*.

    ``main`` validates ``-n 0`` with the words "0 means every entry" and
    :func:`render_entries` honoured that -- but ``to_json`` and
    :func:`render_deleted` passed the raw value straight into a slice, where 0
    means the opposite. So ``rdu --json -n 0`` published an empty ``top_by_size``
    and ``rdu -D -n 0`` listed no files at all and then printed "... and 3 more",
    announcing the rows it had just been asked to show. One reading of the flag,
    in one place.
    """
    return top if top > 0 else _ALL


def _count_noun(res: WalkResult, n: Optional[int]) -> str:
    """What ``res.inodes`` is called, in the mode that produced it.

    RD-9: the most-read line in the report gave **inodes** the label **files**,
    for a tool whose entire purpose is separating byte pressure from inode
    pressure -- and which has a dedicated ``-i`` mode and correct separate JSON
    fields. ``inodes`` is what the figure is: ``files + dirs``, hard-link
    duplicates removed, which is exactly what a files-quota charges.

    Except under ``-c``. That mode skips ``stat``, so it cannot see hard links:
    its total counts one per *name*, and two names for one inode are two here and
    one to the quota. :func:`reconcile.reconcile` already refuses to compare a
    ``-c`` count against a files quota for precisely this reason, so claiming
    "inodes" here would claim a precision the mode gave up. It is ``entries``,
    which is what was counted.
    """
    if res.count_only:
        return "entry" if n == 1 else "entries"
    return "inode" if n == 1 else "inodes"


def _count_phrase(res: WalkResult) -> str:
    """``res.inodes`` with a noun that agrees, and that is true. See :func:`_count_noun`."""
    return "{} {}".format(human_count(res.inodes), _count_noun(res, res.inodes))


def _inode_breakdown(res: WalkResult) -> str:
    """What the headline's inode count is made of, in parts that sum to it.

    It read ``N regular + M dirs``, and neither half was quite true. ``N`` was
    ``files - hardlink_extra_refs``, and ``walk`` counts a symlink in ``files``
    like any other non-directory entry -- so a tree with one symlink and one
    hard-linked pair printed ``6 regular`` for five regular files, the two errors
    happening to cancel. Since ``--json`` publishes ``symlinks`` separately, a
    reader adding them counted the symlink twice.

    Symlinks get their own term when there are any, so the parts still add up to
    the total and nothing has to be inferred. Each is pluralised, which the old
    line also did not do: one directory printed ``1 dirs``.

    ``specials`` -- sockets, fifos, devices -- got a term for the same reason.
    ``walk`` counts them in ``files`` like any other non-directory entry, so the
    term named ``files`` quietly absorbed them: a tree of one file, one symlink,
    one fifo and one socket printed ``3 files`` where ``find -type f`` found one,
    and a real 27,415-inode home printed one more than ``find`` could account
    for, the culprit being a single ssh ControlMaster socket. Every term is now
    checkable against the ``find -type`` that measures it.

    Under ``-c`` there is no ``stat`` and so no ``st_mode``: the walk separates
    directories from the rest off ``d_type`` and knows nothing else about them.
    That mode gets one honest term, ``non-dirs``, rather than a three-way split
    it never measured.
    """
    nondir = res.files - res.hardlink_extra_refs
    if res.count_only:
        # `-c` never stats, so the walk knows only directory from non-directory,
        # off `d_type`. Which non-directories are files, symlinks or sockets it
        # did not measure -- so a term reading "files" would name a split that
        # never happened, and did: a tree of one file, one symlink, one fifo and
        # one socket reported "4 files".
        return " + ".join([plural(nondir, "non-dir"), plural(res.dirs, "dir")])
    # `symlinks` and `specials` count names and `nondir` inodes, so a hard-linked
    # one could in principle push a term past the whole; clamp so the parts never
    # exceed the sum they are explaining.
    syms = min(res.symlinks, max(nondir, 0))
    spec = min(res.specials, max(nondir - syms, 0))
    parts = [plural(nondir - syms - spec, "file")]
    if syms:
        parts.append(plural(syms, "symlink"))
    if spec:
        parts.append(plural(spec, "special"))
    parts.append(plural(res.dirs, "dir"))
    return " + ".join(parts)


def _sort_key(sort: str, by_inodes: bool, res: WalkResult) -> str:
    """The one ranking key, resolved once for every consumer of a listing.

    The table, the hairline sized to it and the JSON document have to agree about
    what the rows were ranked by. They did not: ``render_entries`` took the CLI's
    key, ``_entry_names`` recomputed a *different* one from ``by_inodes`` alone,
    and the count-mode fallback that would have caught it was dead because the CLI
    always passes a non-empty ``sort``.
    """
    key = sort or ("files" if (by_inodes or res.count_only) else "size")
    # A stat-free walk has no bytes to rank or divide by. `top_dirs` enforces this
    # too, for a direct caller; doing it here as well keeps the header, the tones
    # and the note below in step with the rows.
    if res.count_only and key in ("size", "density"):
        key = "files"
    return key


def _mount_fallback(paths: List[str], style: ui.Style) -> List[str]:
    """What the mount says, for the case where no quota backend could say anything.

    On a cluster with no `quota`, no `mmlsquota` and no `lfs` -- a Booth login node
    is one, with an NFS home on Isilon -- every backend fails and the report used
    to stop at "n/a", telling the reader nothing at all. The mount was answering
    the whole time: `statvfs` on that home reports 14.0 GiB total and 6.7 GiB used,
    and the 14 GiB *is* the enforced quota, because Isilon presents per-user quotas
    through those fields.

    **It is not labelled a quota, because sometimes it is not one.** Measured on
    one midway3 login node: `statvfs` on `/project` reports 58.6 TiB of 202 TiB and
    231,900,000 inodes, which is the GPFS fileset quota almost exactly -- while
    `statvfs` on `/home` reports 6.4 PiB, the raw filesystem, against a real home
    quota of 30 GiB. Same syscall, same host, quota in one case and capacity in the
    other, with nothing in the result to tell them apart. So the figure is given
    with its provenance and the ambiguity stated, rather than promoted into the
    quota table where it would read as a limit somebody set.

    Only when the backend failed: with rows on screen this would duplicate better
    evidence, and a second set of numbers that sometimes disagrees is worse than
    none.
    """
    if not paths:
        return []
    seen = set()  # type: Set[Tuple[int, int]]
    out = []  # type: List[str]
    for path in paths:
        report = quotamod.mount_report(path)
        if report is None:
            continue
        try:
            st = os.stat(path)
            key = (st.st_dev, 0)
        except OSError:
            key = (hash(report.mount or path), 0)
        if key in seen:
            continue
        seen.add(key)
        where = ui.printable(report.mount or path)
        # Where blocks are held back for root, `used + avail` falls short of
        # `total` and three numbers on one line stop adding up, which reads as an
        # arithmetic error in the tool. Name the remainder instead of leaving it
        # to be noticed.
        tail = "{} free".format(human_bytes(report.avail))
        if report.reserved:
            tail = "{} free to you and {} reserved for root".format(
                human_bytes(report.avail), human_bytes(report.reserved)
            )
        out.extend(
            _wrapped(
                "the mount at {} reports {} of {} used ({}), {}".format(
                    where,
                    human_bytes(report.used),
                    human_bytes(report.total),
                    pct(report.fraction, 1.0),
                    tail,
                ),
                style,
                "  ",
                tone="yellow",
            )
        )
        if report.inodes_total:
            # The byte line above carries a percentage and this one did not, so a
            # mount 93.3% out of inodes sat under a headline reading 88.5% and the
            # reader had to divide nine-digit numbers to find which resource was
            # nearer the wall. An inode quota is the one that bites without
            # warning; it gets the same treatment as the bytes.
            inode_tail = ""
            if report.inodes_reserved:
                inode_tail = ", {} reserved for root".format(human_count(report.inodes_reserved))
            out.extend(
                _wrapped(
                    "and {} of {} inodes ({}){}".format(
                        human_count(report.inodes_used),
                        human_count(report.inodes_total),
                        pct(report.inodes_fraction, 1.0),
                        inode_tail,
                    ),
                    style,
                    "    ",
                )
            )
    if out:
        out.extend(
            _wrapped(
                "that is statvfs, not a quota backend: where an export enforces a "
                "per-user limit the server reports it through these same fields, "
                "and where it does not this is the whole filesystem. Nothing here "
                "distinguishes the two, so the figure is reported and the doubt "
                "named rather than either being hidden.",
                style,
                "    ",
            )
        )
    return out


def render_quota(
    snap: QuotaSnapshot, paths: Optional[List[str]] = None, style: Optional[ui.Style] = None
) -> List[str]:
    style = style or ui.resolve_style("never")
    if not snap.available:
        # Wrapped, not printed as one long line. A backend failure is the one
        # message a user on a site with no quota command ever sees, and GPFS
        # writes a paragraph for it: three lines of `mmlsquota` diagnostics ran
        # past the right-hand border and took it with them. `quota.reason` is
        # collapsed to one line at the source, and this is where it is given a
        # width.
        reason = snap.reason or "no quota backend available"
        out = [ui.heading("QUOTA", style)]
        out.extend(_wrapped("n/a - " + reason, style, "  ", tone=""))
        out.extend(_mount_fallback(paths or [], style))
        return out

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
        # Wrapped here, with the indent, for the same reason `reason` is: a line
        # the frame has to wrap for us loses its indent at the break, so the
        # continuation reads as a new fact rather than as the rest of a sentence.
        out.extend(_wrapped("? " + snap.time_note, style, "  ", tone="yellow"))
    if snap.figure_note:
        # Louder than the age note, and deliberately: a stale figure is still a
        # figure, while this one the backend has disowned.
        out.extend(_wrapped("! " + snap.figure_note, style, "  ", tone="red"))

    rows = snap.rows
    unmapped = []  # type: List[str]
    if paths:
        keep = []
        for p in paths:
            found = snap.rows_for_path(p)
            if not found:
                unmapped.append(p)
            for r in found:
                if r not in keep:
                    keep.append(r)
        if keep:
            rows = keep
    # Falling through to *all* rows when a path mapped nothing printed thirty
    # rows for eight filesystems, none of which governed the path asked about,
    # with nothing to say so -- and it looked authoritative. The reconciler says
    # the honest thing in exactly this situation; `-Q` said nothing.
    for p in unmapped:
        out.append(
            style.paint(
                "  ? no quota row maps to {}{}".format(
                    ui.printable(p),
                    " -- every row is shown below instead" if not rows or rows is snap.rows else "",
                ),
                "yellow",
            )
        )

    # Two filesets on one mount render identically without this: the mount column
    # cannot disambiguate a collision that is *on* one mount, and the scope column
    # is what separates a user row from a group row for the same fileset -- which
    # is every Lustre reading, where user, group and project all come back at once.
    # A fileset name and a mount point are both strings a subprocess printed, and
    # they are measured for this column before they are painted.
    # The backend's own word for the inode column is "files", and it includes
    # directories. This tool now says "inodes" for that quantity everywhere it
    # counts it itself (see `_entries_header`), so the one place the two
    # vocabularies meet says so -- once, and only when a files row is actually on
    # screen. Without it the reader has "files 26,633 / 300,000" here and
    # "14 inodes" in the walk with nothing connecting them.
    if any(r.kind == "files" for r in rows):
        out.extend(
            _wrapped(
                "the backend's `files` column counts inodes -- directories "
                "included, which is what this tool reports as `inodes`",
                style,
                "  ",
            )
        )
    # `label`, not `fileset`: a row whose fileset name is shared with another
    # filesystem's is qualified by its device, and one whose name is its own is
    # not. See `QuotaRow.label`.
    # Measured in COLUMNS, with `ui.visible_width`, because that is the unit the
    # field is filled in (`ui.pad`, below) and the unit `ui.truncate` cuts to. It
    # was `len`: a fileset named in Chinese is two columns per character, so the
    # column was sized at half the space its own contents needed and then padded
    # by character count on top of that -- three columns of drift for a
    # three-glyph label, thirteen for one long enough to be truncated first, on
    # that row alone, carrying the scope, kind, used/limit, bar, percentage and
    # mount columns with it. See `ui.pad`.
    labels = {id(r): ui.printable(r.label) for r in rows}
    width = max([16] + [ui.visible_width(labels[id(r)]) for r in rows]) if rows else 16
    width = min(width, 40)

    def figure(value, kind):
        """One side of the ``used / limit`` pair, in the row's own units."""
        if value is None:
            return "n/a"
        return human_count(value) if kind == "files" else human_bytes(value)

    def limit_of(row):
        """What to print as the limit: always the one the percentage used.

        `QuotaRow.limit` is that number. Printing `row.soft` instead meant a row
        whose soft limit was the conventional `0` showed "44,812,476 / 0" beside a
        percentage measured against the hard limit.

        With no limit at all, "none" rather than "0": zero is what the backend
        wrote, not what it meant, and a limit of zero is the most alarming thing
        this column could say. "n/a" stays for the different case where the
        figures could not be read -- Constraint 10, one more time.
        """
        if row.limit is not None:
            return figure(row.limit, row.kind)
        return "n/a" if row.soft is None and row.hard is None else "none"

    # The used and limit columns are measured from the rows, the way the fileset
    # label above already is. Eleven was enough for every byte figure -- the
    # widest `human_bytes` can produce is "1023.9 PiB" -- and for a files quota up
    # to 999,999,999. This cluster's own row is "44,812,476 / 230,900,000", eleven
    # characters exactly, one order of magnitude from the edge: a billion-inode
    # quota overflowed both fields and shifted the bar, the percentage, the mount
    # and the OVER marker four columns right on that row alone.
    figures = [(figure(r.used, r.kind), limit_of(r)) for r in rows]
    used_w = max([11] + [len(u) for u, _s in figures]) if figures else 11
    soft_w = max([11] + [len(x) for _u, x in figures]) if figures else 11

    for r in rows:
        used = figure(r.used, r.kind)
        soft = limit_of(r)
        frac = r.usage_fraction
        # Colour is the only thing that makes a near-full quota jump out of a
        # table of thirty rows.
        tone = "green"
        if frac is not None and frac >= 0.90:
            tone = "red"
        elif frac is not None and frac >= 0.75:
            tone = "yellow"
        bar = ui.bar(frac if frac is not None else 0.0, 10, style, accent=tone, min_tick=False)
        # A bar clamps at full, so 100% and 450% drew identically. Over the limit
        # is the one state the bar cannot express, so it gets a mark of its own.
        over = style.paint(" OVER", "red") if frac is not None and frac > 1.0 else ""
        grace = ""
        # One predicate, in `quota`, beside the vocabulary it is derived from.
        # This line used to carry its own list, missing `0` and `n/a`.
        if quotamod.in_grace(r.grace):
            # An expired soft limit stops writes; it cannot sit in a quiet column.
            grace = style.paint("  ! IN GRACE, {} left".format(r.grace), "red")
        out.append(
            # An explicit space between the bar and the percentage. There was
            # none: the gap came from `{:>6}` padding a five-character figure like
            # " 22.2%", and it disappeared the moment the number itself filled the
            # field -- so a row at 105.6% rendered as "##########105.6%", with the
            # bar and the figure fused. That is the one state the row exists to
            # shout about, and the only one where it was unreadable. Four digits
            # (1024.5%) overflowed the field as well and collided the same way.
            #
            # The field keeps its width so the column still aligns on its right
            # edge; the separator no longer depends on the value being short.
            "  {}  {}  {}  {}  {} {}  {}{}".format(
                style.paint(
                    ui.pad(ui.truncate(labels[id(r)], width), width),
                    "bold",
                ),
                style.paint("{:<7}".format((r.scope or "?")[:7]), "dim"),
                style.paint("{:<6}".format(r.kind), "dim"),
                "{:>{uw}} / {:<{sw}}".format(used, soft, uw=used_w, sw=soft_w),
                bar,
                # Seven, not six: "1022.2%" needs it, and a row that far over is
                # exactly the one a reader is scanning for. Capping the figure
                # instead would be the lie the `OVER` marker exists to avoid --
                # the bar clamps, the number must not -- so the column widens by
                # one and every row after it stays aligned.
                style.paint("{:>7}".format(pct(frac, 1.0) if frac is not None else "n/a"), tone),
                style.paint(ui.printable(r.mount) if r.mount else "?", "dim"),
                over + grace,
            )
        )

    if any(not r.mount for r in rows):
        for note in snap.mapping_notes():
            out.append(style.paint("  ? " + note, "dim"))
    return out


# Directories whose contents are a cache, an artifact tree, or a working set
# nobody meant to keep, mapped to the command that reclaims them.
#
# This is the highest-value pattern table in the package and the reason is in the
# ticket record: hidden dotfiles are the single largest staff-named cause (82
# tickets), with container caches (18) and conda/pip caches (12) as separate
# entries on the same list. A walker sees all of them by construction -- it is
# already standing in the directory -- so recognising them costs a dict lookup and
# closes the three biggest classes at once.
#
# `None` means "review this yourself": `wandb/` and `mlruns/` are often the
# experiment record somebody needs, and the tool has no business implying
# otherwise.
_RECLAIMABLE = (
    ("conda/pkgs", "conda clean -a", True),
    ("mamba/pkgs", "conda clean -a", True),
    ("cache/pip", "pip cache purge", True),
    # Two vintages of one tool, newest first. `huggingface-cli` was renamed to
    # `hf`, and the old name is still installed as a stub that prints
    # "`huggingface-cli` is deprecated and no longer works. Use `hf` instead."
    # -- so it passes `shutil.which` and is a dead end anyway, which is exactly
    # the failure RD-12 filed against unchecked commands. `shutil.which` answers
    # "is it on PATH", and for a tool mid-rename that is not the same question as
    # "does it work".
    #
    # Both names are kept because both clusters are real: a host with an older
    # `huggingface_hub` has only `huggingface-cli`, and there it works. The
    # successor is tried first, so a host with both gets the one that runs.
    ("cache/huggingface/hub", ("hf cache prune", "huggingface-cli delete-cache"), True),
    ("cache/huggingface", ("hf cache prune", "huggingface-cli delete-cache"), True),
    # `{path}` is interpolated with the directory that actually matched. Every
    # other entry here either invokes a tool that locates its own store (`pip
    # cache purge`) or is location-neutral advice ("safe to delete"); this was the
    # one entry that hardcoded a path, and the pattern matches anywhere. On a
    # cluster with a small home quota, relocating caches out of `$HOME` is the
    # normal thing to do, so a `cache/torch` outside `~` is the common case rather
    # than a corner one -- and there the printed `rm -rf ~/.cache/torch` deleted an
    # unrelated cache while leaving the bytes it had just reported in place. For an
    # `rm -rf`, being wrong is unrecoverable, so this is the one command in the
    # table that must name its own subject.
    ("cache/torch", "rm -rf {path}", True),
    ("cache/uv", "uv cache clean", True),
    ("apptainer/cache", "apptainer cache clean", True),
    ("singularity/cache", "singularity cache clean", True),
    ("nv/ComputeCache", "safe to delete", True),
    ("local/share/Trash", "safe to delete", True),
    ("node_modules", "safe to delete and reinstall", True),
    ("__pycache__", "safe to delete", True),
    (".mypy_cache", "safe to delete", True),
    (".ruff_cache", "safe to delete", True),
    (".pytest_cache", "safe to delete", True),
    # `delete_ok=False`, and it is the entry that makes this a per-entry decision
    # rather than a rule: `git gc` *repacks* this directory, and deleting it
    # destroys the repository. If `git` is missing the honest answer is to say so,
    # not to offer the destructive substitute.
    (".git/objects", "git gc --aggressive --prune=now", False),
    ("wandb", None, False),
    ("mlruns", None, False),
    ("lightning_logs", None, False),
    ("jupyter/runtime", None, False),
)

# Entries whose "command" is advice rather than something to run. Listed rather
# than sniffed, so a new tool whose name happens to read like prose cannot be
# mistaken for one of these.
_RECLAIM_ADVICE = frozenset(("safe to delete", "safe to delete and reinstall"))


# How many concrete commands a path-templated entry prints before it defers to the
# list of examples below it. Three keeps the section readable while still giving a
# copy-pasteable line for the directories that hold most of the bytes.
_RECLAIM_COMMAND_CAP = 3

# Why no command could be printed. The *cause* is a property of the path and the
# stream; the *remedy* depends on what the command was for, so the caller supplies
# it. Reusing one fixed note everywhere told a reader looking for cold files to
# "delete it by inode ... from the list below" -- advice about deletion, and about
# a list, neither of which exists in that section.
_UNQUOTABLE_CAUSE = "this path contains unprintable characters"
_RECLAIM_UNQUOTABLE_REMEDY = (
    "identify it from the list below and delete it by inode or with a glob, not by pasting a name"
)

# The same refusal for a different cause: the path is perfectly ordinary, it is
# *this terminal* that cannot render it, so re-running under a UTF-8 locale
# produces a command that works.
_UNENCODABLE_CAUSE = (
    "this terminal's encoding cannot represent this path, so any command printed "
    "here would name a different directory"
)
_RECLAIM_UNENCODABLE_REMEDY = "re-run under a UTF-8 locale, or delete it by inode"


def _no_command_note(
    path: str,
    unquotable: str = _RECLAIM_UNQUOTABLE_REMEDY,
    unencodable: str = _RECLAIM_UNENCODABLE_REMEDY,
) -> str:
    """Why no command was printed for ``path`` -- the two causes want different
    next steps, and a reader who cannot tell them apart cannot act on either.

    Control characters mean the name itself is unpasteable, whatever the terminal.
    An encoding that cannot carry the path means the *terminal* is the problem and
    the name is fine: re-running under a UTF-8 locale produces a working command.

    The remedies default to the reclaim wording, which is where this started; a
    caller printing some other kind of command passes its own, because "delete it
    by inode" is the wrong next step for a listing.
    """
    quoted = "'" + path.replace("'", "'\"'\"'") + "'"
    if ui.printable(quoted) != quoted:
        return "# {} -- {}".format(_UNQUOTABLE_CAUSE, unquotable)
    return "# {} -- {}".format(_UNENCODABLE_CAUSE, unencodable)


def _shell_command(template: str, path: str) -> str:
    """A suggested command with ``path`` substituted, or ``""`` if that is unsafe.

    Two hazards, and the second is the one that made this necessary.

    **A path is not a shell word.** Spaces, quotes and glob characters in a
    directory name are ordinary on a shared filesystem, and an unquoted path is a
    different command from the one intended. ``shlex.quote`` is the fix, and it is
    required rather than cosmetic for anything with ``rm`` in it.

    **A long command wraps, and half of an `rm -rf` is still a valid `rm -rf`.**
    Interpolating a real path made the line long enough for the frame to wrap it,
    and the first line then read ``rm -rf /project/rcc/user/.cache/tmp/...`` --
    a directory seven levels above the intended one, complete and runnable.
    Quoting removes that: a partial copy carries an unterminated quote, so the
    shell waits for input instead of deleting the wrong tree. A command that fails
    closed when truncated is the only kind worth printing.

    Where the path holds characters :func:`ui.printable` would have to escape,
    there is no correct one-liner -- an escaped path is not the path, and a raw
    one puts control characters on the terminal (see :func:`ui.printable`). That
    returns ``""`` and the caller says so instead of guessing.
    """
    # Always quoted, never only-when-necessary. `shlex.quote` leaves an ordinary
    # path bare, which is correct as escaping and useless as a guard: the bare
    # path is exactly the case that wraps and leaves a runnable prefix behind.
    # The inner escaping is `shlex.quote`'s own: end the quote, emit a literal
    # quote, reopen.
    quoted = "'" + path.replace("'", "'\"'\"'") + "'"
    if ui.printable(quoted) != quoted:
        return ""
    # The same test against the *output* encoding, which is the other way this
    # command can reach the reader saying something else. `printable` passes an
    # ordinary accented path through untouched; `ui.encode_safe` at the write then
    # turns `café` into `caf\xe9`, and inside single quotes that is seven literal
    # characters naming a directory that does not exist. Measured under
    # `PYTHONIOENCODING=ascii`: `rm -rf '.../enc/caf\xe9/cache/torch'`.
    #
    # The reasoning is the docstring's own, one mechanism further along: an
    # escaped path is not the path. It was applied to control characters and not
    # to the encoding, and the difference landed on the only output this tool
    # invites anyone to execute.
    if ui.encode_safe(quoted) != quoted:
        return ""
    return template.format(path=quoted)


def _first_runnable(command: "Any") -> Optional[str]:
    """The first of one-or-more candidate commands whose tool is on ``PATH``.

    An entry may name several vintages of the same tool, newest first, because a
    rename is not a one-off: `huggingface-cli` became `hf`, and a fleet has hosts
    on both sides of that. A single fixed string cannot be right on both.

    With none of them present the *first* is returned, so the caller still has a
    command to try `module load` against and a name to report as missing --
    which is the newest name, the one worth telling the reader about.
    """
    if command is None or isinstance(command, str):
        return command
    for candidate in command:
        if shutil.which(candidate.split()[0]):
            return candidate
    return command[0] if command else None


def _reclaimable_match(path: str) -> Optional[Tuple[str, Optional[str], bool]]:
    """The reclaim rule for a path, if one applies. Longest pattern wins."""
    normalised = path.replace(os.sep, "/").lstrip("/")
    best = None  # type: Optional[Tuple[str, Any, bool]]
    for pattern, command, delete_ok in _RECLAIMABLE:
        stem = pattern.lstrip(".")
        hit = normalised.endswith("/" + pattern) or normalised.endswith("/." + stem)
        if hit and (best is None or len(pattern) > len(best[0])):
            best = (pattern, command, delete_ok)
    if best is None:
        return None
    return (best[0], _first_runnable(best[1]), best[2])


def _modulefile_for(tool: str) -> str:
    """``tool`` if a modulefile of that name is on ``MODULEPATH``, else ``""``.

    Evidence rather than a guess: both Lmod and environment-modules lay
    ``MODULEPATH`` out as one entry per package, so an entry named ``uv`` means
    ``module load uv`` resolves. Measured on both clusters in this campaign: `uv`,
    `apptainer` and `singularity` are absent from ``PATH`` and present as
    modulefiles, which is exactly the case where the bare command fails and
    ``module load uv && uv cache clean`` works.

    Anywhere without modules the environment variable is unset and this returns
    ``""``, so the caller falls through to its next option.
    """
    for root in os.environ.get("MODULEPATH", "").split(os.pathsep):
        if root and os.path.exists(os.path.join(root, tool)):
            return tool
    return ""


def reclaim_command(command: Optional[str], delete_ok: bool) -> Tuple[Optional[str], bool]:
    """The command to print for this host, and whether it needs a path per match.

    **Nothing is printed as a command unless it was checked against this host.**
    Most entries delegate to a tool, and nothing checked the tool was runnable:
    on midway2 two of the three commonest suggestions failed as printed
    (`huggingface-cli` is not installed in any configuration there, `uv` needs a
    module first) and the third depended on which shell the report was run from.
    A `command not found` is a dead end -- the reader then has to work out for
    themselves that the cache is just a directory, which is what they came here to
    be told.

    The order is: the tool if it is on ``PATH``; else ``module load <tool> &&
    ...`` if a modulefile of that name exists; else the quoted ``rm -rf`` form
    where the entry says the directory is a regenerable cache; else the command
    with the reason it will not run, because a dead command that says it is dead
    still tells the reader what the tool would have been.
    """
    # Resolve alternatives here too, not only in `_reclaimable_match`: this is the
    # entry point anything iterating `_RECLAIMABLE` reaches for, and it was handed
    # a tuple straight from the table. `_first_runnable` passes a plain string
    # through, so doing it at both doors is idempotent.
    command = _first_runnable(command)
    if command is None or command in _RECLAIM_ADVICE:
        return command, False
    if "{path}" in command:
        # Already host-independent: `rm` is in coreutils, and the path is the
        # subject rather than a store the tool has to locate.
        return command, True
    tool = command.split()[0]
    if shutil.which(tool):
        return command, False
    if _modulefile_for(tool):
        return "module load {} && {}".format(tool, command), False
    if delete_ok:
        return "rm -rf {path}", True
    return "{} ({} is not on PATH here)".format(command, tool), False


def reclaimable_groups(
    res: WalkResult,
) -> List[Tuple[str, Optional[str], List[Tuple[int, int, str]]]]:
    """``(pattern, command, [(bytes, inodes, path), ...])`` per kind, heaviest first.

    Extracted from :func:`render_reclaimable` so the report and ``--json`` are
    reading one grouping. The section names directories the user is expected to
    act on, and the human view caps how many commands it prints; without this the
    capped remainder existed nowhere at all -- the report said "listed below" and
    nothing below listed them.
    """
    groups = {}  # type: Dict[str, List[Tuple[int, int, str]]]
    commands = {}  # type: Dict[str, Optional[str]]
    deletable = {}  # type: Dict[str, bool]
    matched = []  # type: List[Tuple[str, int, int, str]]

    # `res.watched` carries these at any depth; `dir_agg` only reaches the reported
    # depth and would miss every one of them on a default run.
    candidates = [(path, size, n) for path, (size, n) in res.watched.items()]
    candidates += [
        (e.path, e.size, e.inodes) for e in res.dir_agg.values() if e.is_dir and e.path != res.root
    ]
    seen = set()  # type: Set[str]
    for path, size, inodes in candidates:
        if path in seen:
            continue
        match = _reclaimable_match(path)
        if match is None:
            continue
        seen.add(path)
        matched.append((path, size, inodes, match[0]))
        commands[match[0]] = match[1]
        deletable[match[0]] = match[2]

    # A nested match sits inside its parent's total already, so reporting both
    # counts the same bytes twice.
    #
    # Asked of each path's own ancestors rather than of every other match. The
    # `any(... for other in paths)` this replaces was O(n^2) in the number of
    # matches, and n is not small: a tree of Python packages matched 6,001
    # `__pycache__` directories, so the check ran 36 million `startswith` calls
    # and took **7.9 seconds** -- a fifth of the entire runtime of `rdu -a` on
    # that tree, spent in a summary of sixty rows. The ancestor walk does the
    # same work in 0.07s, and on that tree both keep the identical 5,959 paths.
    #
    # It is the same question, not an approximation of it: `other + os.sep` being
    # a prefix of `path` is exactly "`other` is a proper ancestor directory of
    # `path`", and walking `dirname` upwards enumerates those and nothing else.
    # Ancestors are bounded by path depth where the old form was bounded by the
    # number of matches, which is why one scales and the other does not.
    paths = {m[0] for m in matched}
    for path, size, inodes, pattern in matched:
        nested = False
        current = path
        while True:
            parent = os.path.dirname(current)
            # `dirname` is its own fixed point at "/" and at "", which is what
            # ends this loop -- there is no sentinel to compare against.
            if parent == current:
                break
            current = parent
            # `"/"` and `""` are excluded because the form this replaces could
            # never match them: it asked `path.startswith(other + os.sep)`, and
            # for a root `other` that is `"//"`, which no path begins with. So a
            # `"/"` in the set was every absolute path's ancestor here and none
            # of their ancestor there -- a differential run over 4,000 random
            # path sets found the two disagreeing on 211 of them, always this.
            # `_reclaimable_match` cannot return a bare root today (it needs a
            # separator before the pattern, so `"/"` normalises to `""` and
            # matches nothing), which is why this was invisible; a guard that
            # costs one comparison is cheaper than depending on that.
            if current in paths and current not in ("/", ""):
                nested = True
                break
        if nested:
            continue
        groups.setdefault(pattern, []).append((size, inodes, path))

    # A stat-free walk has no bytes for any of these, so the ranking moves to the
    # one measurement it does have. Ranking by an all-zero key returns thread merge
    # order.
    weight = (
        (lambda hits: sum(h[1] for h in hits))
        if res.count_only
        else (lambda hits: sum(h[0] for h in hits))
    )
    ordered = sorted(groups.items(), key=lambda kv: weight(kv[1]), reverse=True)
    # The command is resolved against *this host* here, so the report and the
    # document cannot disagree about what is runnable. See `reclaim_command`.
    return [
        (
            pattern,
            reclaim_command(commands.get(pattern), deletable.get(pattern, False))[0],
            sorted(hits, reverse=True),
        )
        for pattern, hits in ordered
    ]


def render_reclaimable(res: WalkResult, style: ui.Style) -> List[str]:
    """Directories in this tree that are caches, grouped by what reclaims them.

    Suggests; never deletes, and never offers to. The tool's authority comes from
    being a measurement instrument, and one that also removes things is one nobody
    runs on a full filesystem at 2 a.m. Printing the command is strictly more
    useful than running it, because the reader can read it first.

    **Grouped by pattern, not listed per directory.** A home directory with
    twenty-five git repositories produced twenty-five ``.git/objects`` rows each
    repeating the same ``git gc``, which buried the one 2 GiB model cache that was
    the actual answer. One line per *kind* of reclaimable thing, with its total and
    its largest few examples, is the same information in the order it is useful.
    """
    grouped = reclaimable_groups(res)
    over_bytes, over_inodes = res.watched_overflow
    # `watched_dropped` is the number whose PATH the cap gave up; `watched_seen`
    # is derived from it, so subtracting is the same figure the long way round.
    over_dirs = res.watched_dropped
    # The overflow can be the ONLY thing there is to say. `grouped` is built from
    # `res.watched`, and the cap's whole job is to stop filling `res.watched` --
    # so a tree with more cache directories than the cap tracks can leave it empty
    # while the overflow holds real bytes, and returning early then dropped those
    # bytes out of the report entirely. That is the failure this disclosure exists
    # to prevent, reintroduced one line above the disclosure itself.
    if not grouped and not (over_dirs or over_inodes or over_bytes):
        return []
    counts = res.count_only
    commands = {pattern: command for pattern, command, _hits in grouped}
    ranked = [(pattern, hits) for pattern, _command, hits in grouped]
    out = ["", ui.heading("RECLAIMABLE", style)]
    out.extend(
        _wrapped(
            "caches and build artifacts, listed not asserted -- read each command "
            "before you run it",
            style,
            "  ",
        )
    )
    # Every kind, not the six that are printed. `total` used to accumulate inside
    # the display loop, so on any tree with more than six kinds of cache -- a home
    # directory with conda, pip, uv, HF, node_modules and a couple of tool caches
    # is already past it -- the figure labelled "in total" was the top six only,
    # and the share of the tree computed from it was understated with it.
    total = sum(sum(h[0] for h in hits) for _pattern, hits in ranked)
    total_inodes = sum(sum(h[1] for h in hits) for _pattern, hits in ranked)
    shown = ranked[:6]
    for pattern, hits in shown:
        hits.sort(
            key=(lambda h: (h[1], h[0])) if counts else (lambda h: (h[0], h[1])), reverse=True
        )
        size = sum(h[0] for h in hits)
        inodes = sum(h[1] for h in hits)
        # One hit names the actual directory; several name the pattern and count.
        # Printing the *pattern* for a single hit dropped the one thing the reader
        # needs -- which directory it is -- in favour of restating the rule.
        if len(hits) == 1:
            label = ui.printable(os.path.relpath(hits[0][2], res.root))
        else:
            label = "{}x {}".format(len(hits), pattern)
        out.append(
            # `noun`, not a hard-coded `inodes`: the total line below already
            # agrees via `plural` and these rows did not, so a single-inode
            # match printed `1 inodes` directly above `... reclaimable in
            # total`. The label sits in a fixed-width field so the path column
            # does not shift by the one character between the two forms.
            "  {:>10}  {:>10} {:<8}{}".format(
                style.paint("n/a" if counts else human_bytes(size), "bold"),
                human_count(inodes),
                noun(inodes, "inode"),
                label,
            )
        )
        command = commands.get(pattern)
        if command and "{path}" in command:
            # One line per directory, absolute. A single line cannot be correct
            # for several directories, and a root-relative path would be wrong
            # for any reader whose shell is not sitting in the walk root -- both
            # of which matter more than usual for a command that removes things.
            for _size, _inodes, hit in hits[:_RECLAIM_COMMAND_CAP]:
                rendered = _shell_command(command, hit)
                out.append(
                    style.paint(
                        "              " + (rendered or _no_command_note(hit)),
                        "cyan" if rendered else "yellow",
                    )
                )
            if len(hits) > _RECLAIM_COMMAND_CAP:
                # Names the group, and does not claim the rest are listed. It said
                # "listed below", and below was the `largest:` line -- two examples,
                # both already among the commands printed above it. So the
                # remaining directories appeared nowhere, which is a silent cap on
                # the one surface where the reader has to act on every entry.
                # `--json` now carries all of them.
                out.extend(
                    _wrapped(
                        "... and {} more {} {} not shown here -- `--json` lists"
                        " every one under `reclaimable`".format(
                            len(hits) - _RECLAIM_COMMAND_CAP,
                            pattern,
                            "directory is"
                            if len(hits) - _RECLAIM_COMMAND_CAP == 1
                            else "directories are",
                        ),
                        style,
                        "              ",
                    )
                )
        else:
            out.append(
                style.paint("              {}".format(command or "review before deleting"), "cyan")
            )
        if len(hits) > 1:
            examples = ", ".join(
                "{} ({})".format(
                    ui.printable(os.path.relpath(h[2], res.root)),
                    plural(h[1], "file") if counts else human_bytes(h[0]),
                )
                for h in hits[:2]
            )
            out.extend(_wrapped("largest: " + examples, style, "              "))
    if len(ranked) > len(shown):
        rest = ranked[len(shown) :]
        out.append(
            style.paint(
                "  ... and {} more kinds ({}), counted in the total below".format(
                    len(rest),
                    plural(sum(sum(h[1] for h in v) for _p, v in rest), "file")
                    if counts
                    else human_bytes(sum(sum(h[0] for h in v) for _p, v in rest)),
                ),
                "dim",
            )
        )
    if over_dirs or over_inodes or over_bytes:
        # The walk stopped tracking cache directories individually past its cap,
        # and this section is the only place that shows.  Said, not swallowed: a
        # bound the reader is not told about reads as a total, and this is a
        # figure they act on.  The bytes ARE in the total below -- what was
        # dropped is the paths, so these rows cannot be attributed to a kind.
        total += over_bytes
        total_inodes += over_inodes
        # The directory count is only mentioned when there IS one. The two figures
        # come from different places and do not have to agree: a worker gives up a
        # path once its own thread-local cap is full and adds that path's bytes to
        # `watched_overflow`, but `watched_dropped` is computed against the MERGED
        # `watched`, so a path another worker happened to track leaves overflow
        # bytes behind with no dropped directory to go with them. Formatted
        # unconditionally, that printed "... and 0 further cache-shaped
        # directories", which reads as a bug in the tool rather than as a bound.
        if over_dirs:
            what = "{} further cache-shaped {}".format(
                over_dirs, "directory" if over_dirs == 1 else "directories"
            )
        else:
            what = "further cache-shaped bytes"
        out.extend(
            _wrapped(
                "... and {} beyond the walk's tracking cap ({} / {}), included in "
                "the total below but not attributed to a kind -- a deeper `-d` or "
                "a narrower path lists them".format(
                    what,
                    plural(over_inodes, "inode"),
                    "n/a" if counts else human_bytes(over_bytes),
                ),
                style,
                "  ",
            )
        )
    if counts:
        share = " ({} of the tree)".format(pct(total_inodes, res.inodes)) if res.inodes else ""
        figure = plural(total_inodes, "inode")
    else:
        share = " ({} of the tree)".format(pct(total, res.size)) if res.size else ""
        figure = human_bytes(total)
    out.append(style.paint("  {} reclaimable in total{}".format(figure, share), "dim"))
    return out


def render_age(res: WalkResult, style: ui.Style) -> List[str]:
    """Bytes and inodes by how long ago they were last modified.

    For a quota that is full, "what is big" is not the actionable question -- the
    biggest thing is usually the thing being worked on. "What is big *and* has not
    been touched in a year" is, and it is the question this histogram answers.

    Regular files only. Directories are a handful of bytes each and their mtime
    tracks their *contents* changing, so bucketing them would count the same event
    twice and add nothing to either column.
    """
    if res.count_only or not any(size or files for size, files in res.by_age):
        return []
    peak = max(size for size, _files in res.by_age) or 1
    out = [
        "",
        ui.heading("BY AGE", style),
        # Not "regular files": a symlink reaches the bucketing like any other
        # non-directory entry (`walk` counts it in `files` *and* in `symlinks`),
        # so this said "regular" about a population that included them. The
        # exclusion worth stating is directories, which really are left out.
        style.paint("  last modified, files and symlinks -- not directories", "dim"),
    ]
    for label, (size, files) in zip(walkmod.AGE_BUCKET_LABELS, res.by_age):
        share = size / float(res.size) if res.size else 0.0
        out.append(
            "  {:<8}{:>10}  {}  {:>6}  {:>10} {}".format(
                label,
                human_bytes(size),
                ui.bar(size / float(peak), 12, style, accent=style.heat(size / float(peak))),
                pct(share, 1.0),
                human_count(files),
                # The count keeps the column and the noun sits outside it, so this
                # cannot go through `plural`. It went through nothing at all and
                # printed "1 files" -- the very case `fmt.plural` was written for.
                noun(files, "file"),
            )
        )
    # Whichever of the two is material, because either can be the binding limit
    # and they do not move together. On a tree that has not settled the byte column
    # is legitimately all zeros -- GPFS has not allocated the blocks yet, which is
    # the phenomenon `render_settle` reports -- and the inode count is still the
    # whole story. Gating this sentence on bytes alone meant the cold-data finding
    # vanished on exactly the freshly-written trees people run this against.
    cold_bytes, cold_files = res.by_age[-1]
    # The denominator is the population `by_age` actually counts: non-directory
    # entries, symlinks included, hard-link duplicates already suppressed (a
    # duplicate is counted in `files` and then `continue`s before the bucketing).
    # It used to be `res.inodes`,
    # which adds every directory, so this share was diluted by the whole directory
    # count -- 280 of 3,798 on a config tree, and worse the flatter the tree. The
    # label beside it always said "file"; only the divisor disagreed.
    bucketed = res.files - res.hardlink_extra_refs
    byte_share = cold_bytes / float(res.size) if res.size else 0.0
    file_share = cold_files / float(bucketed) if bucketed else 0.0
    if byte_share >= 0.05 or file_share >= 0.05:
        # The verb has to follow whichever measure was chosen. A byte figure is a
        # mass noun and takes "has"; a count of two or more files takes "have",
        # and this printed "4 files (66.7%) has not been modified". That exact
        # string is quoted in the round-45 report -- where it was filed for having
        # the wrong *denominator*, and the disagreement beside it went unnoticed
        # by everyone, including the pass that fixed the denominator.
        if byte_share >= file_share:
            measure = "{} ({})".format(human_bytes(cold_bytes), pct(byte_share, 1.0))
            verb = "has"
        else:
            measure = "{} ({})".format(plural(cold_files, "file"), pct(file_share, 1.0))
            verb = "has" if cold_files == 1 else "have"
        out.extend(
            _wrapped(
                "{} {} not been modified in over a year. On a full quota that is "
                "the first place to look, and it is invisible to a size-only "
                "listing.".format(measure, verb),
                style,
                "  ",
            )
        )
        # Where to look. This was the one finding in the report that named a
        # quantity and nothing else: `RECLAIMABLE` prints paths and commands,
        # `UNLINKED` prints paths and pids, the entries table prints paths, and
        # both floor causes now print paths -- while the section that says "that
        # is the first place to look" gave the reader no way to look. `--sort` has
        # no mtime key, so nothing else in the tool could find them either.
        #
        # Built through `_shell_command`, so it inherits that machinery whole: the
        # path is always single-quoted, a truncated copy fails closed on the
        # unterminated quote, and a path this terminal cannot render is refused
        # with the reason rather than guessed at.
        #
        # `! -type d` rather than `-type f`, because the population above is every
        # non-directory entry and symlinks are in it. The day boundary is taken
        # from the bucket itself so the two cannot drift apart; `find` counts whole
        # 24-hour periods, so the edge of the range can differ by a day, and the
        # sentence claims a listing rather than a matching count.
        # `-xdev` exactly when the walk was bounded by `-x`. Without it the
        # command crosses a mount boundary the walk stopped at, so it lists files
        # this section never counted -- a command that answers a different question
        # from the finding it is printed under.
        bounded = " -xdev" if res.one_file_system else ""
        command = _shell_command(
            "find {path}%s ! -type d -mtime +%d" % (bounded, walkmod.AGE_BUCKET_DAYS[-1]),
            res.root,
        )
        out.append(
            style.paint(
                "      "
                + (
                    command
                    or _no_command_note(
                        res.root,
                        "reach it by inode or with a glob rather than by pasting a name",
                        "re-run under a UTF-8 locale to get a command you can paste",
                    )
                ),
                "dim",
            )
        )
    return out


# The inode column's floor. Nine holds "9,999,999"; a tenth digit is where a
# fixed width stops being enough.
_INODE_COL = 9


def _inode_width(counts: "Any") -> int:
    """How wide the inode column has to be for these rows.

    Measured off the values it will hold, which is the rule
    :func:`_entries_rule` already states for the hairline: *measured off the rows
    it is drawn over, not reconstructed from a column tally.* The column itself was
    a fixed nine, which holds up to 9,999,999 -- and a directory with ten million
    inodes is ordinary on the filesystems this tool exists for. `/project/rcc` on
    this cluster holds 44 million. At the tenth digit the field overflowed, so the
    `entry` names below it started at different columns and the `inodes` header no
    longer sat over its own column.
    """
    widest = 0
    for count in counts:
        widest = max(widest, len(human_count(count)))
    return max(_INODE_COL, widest)


def _entry_rows(
    res: WalkResult,
    top: int,
    by_inodes: bool,
    style: ui.Style,
    indent: str = "  ",
    sort: str = "",
) -> Tuple[List[str], int]:
    """The ranked table: size, proportional bar, share of the total, inodes, path.

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
    limit = _limit(top)
    key = _sort_key(sort, by_inodes, res)
    ranked = res.top_dirs(limit, key, finished_only=res.partial)
    if not ranked:
        return [], _INODE_COL
    by_density = key == "density"

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
    # A remainder row carries the leftover bytes and inodes, which is a sum. A
    # density is a ratio, so there is nothing to sum and no bar to draw for "the
    # rest" -- and the rows missing from a density listing are mostly missing
    # because of the inode floor, not because of -n, so the row's "use -n 0 for
    # all" would be a false instruction.
    #
    # The row does have to carry a figure, but the gate was `rest_size > 0` --
    # bytes only -- and that suppressed it in two cases where the *inodes* were
    # the thing being hidden, leaving the listed rows and the summary unable to
    # complete the total:
    #
    # * ``-c`` never stats, so every size is zero. The one mode whose only
    #   measurement is inodes was also the one mode that never printed the
    #   remainder: ``rdu -c -n 2`` on a 21-inode tree listed 11 inodes and then
    #   said "3 more" with no figure at all.
    # * A hidden sibling can hold many inodes and zero *allocated* bytes. On XFS a
    #   small directory lives inside its own inode and `st_blocks` is 0 -- so a
    #   root whose hidden children are directories has `rest_size == 0` on a
    #   perfectly ordinary filesystem. `rdu -n 1` on a 19-inode tree of that shape
    #   printed one row of 2 inodes, "4 more", and nothing about the other 16.
    #
    # Either figure being non-zero earns the row, and `_entry_line` already knows
    # how to draw it in both modes -- `size_hidden` omits the byte column under
    # ``-c``, and a true `0 B` beside a real inode count is what the other case is.
    show_rest = (
        hidden > 0
        and (rest_size > 0 or rest_inodes > 0)
        and not res.partial
        and not by_density
        and _entries_partition_tree(res)
    )
    # The remainder row carries bytes, so it is only well defined when the listed
    # rows are siblings that partition the tree -- at depth > 1 they nest and it
    # would double-count. But "there are rows you are not seeing" is true at any
    # depth, and the table used to just stop without saying so: at `-d 2 -n 10` it
    # showed ten of fifty-nine and looked complete. A count with no byte figure
    # attached is honest at every depth.
    # "N more -- use -n 0 for all" is a false instruction on a density listing:
    # what is missing is mostly below the inode floor, and -n will not bring it
    # back. `_density_floor_note` says the true thing instead, and `_table` adds it
    # *after* the rows -- prose in here would end up sizing the hairline, which is
    # measured off the widest row it is drawn over.
    say_hidden = hidden > 0 and not show_rest and not res.partial and not by_density

    # The bar and the share must measure whatever the rows were ranked by, or a
    # -i listing shows an inode ordering with byte-length bars and reads as
    # though it were mis-sorted.
    def metric(e):
        if by_density:
            return files_per_gib(e.size, e.inodes) or 0.0
        return e.inodes if key == "files" else e.size

    # A share needs a denominator. After an interrupt there is no tree total, so
    # the column is blanked rather than filled with a fraction of an accident.
    #
    # Density has no total either, and for a different reason: files-per-GiB is a
    # ratio, so densities do not add up to a tree's density and no row is a
    # "share" of anything. The bar falls back to ranking against the densest row
    # and the percentage column is left empty, exactly as after an interrupt.
    if res.partial or by_density:
        total = 0
    elif key == "files":
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

    ranked_by_files = key == "files"
    # One width for the whole listing, the remainder row included, so every row
    # and the header agree about where the `entry` column starts.
    width_counts = [e.inodes for e in ranked]
    if show_rest:
        width_counts.append(rest_inodes)
    inode_width = _inode_width(width_counts)
    rows = [
        _entry_line(
            # Escaped here rather than at the frame, because this is where the
            # row's width is decided and where the name is still known to be
            # nothing but a filename. See `ui.printable`.
            ui.printable(os.path.relpath(e.path, res.root)) + ("/" if e.is_dir else ""),
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
            density=files_per_gib(e.size, e.inodes) if by_density else None,
            ranked_by_density=by_density,
            inode_width=inode_width,
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
                density=files_per_gib(rest_size, rest_inodes) if by_density else None,
                ranked_by_density=by_density,
                inode_width=inode_width,
            )
        )
    elif say_hidden:
        # No bar and no byte figure -- just the count and how to see them. At depth
        # greater than one the rows nest, so any total attached here would
        # double-count; the count itself is still true and is the whole point.
        rows.append(
            style.paint(
                "{}{} more {} use -n 0 for all".format(indent, human_count(hidden), ui.dash(style)),
                "dim",
            )
        )
    return rows, inode_width


def render_entries(
    res: WalkResult,
    top: int,
    by_inodes: bool,
    style: ui.Style,
    indent: str = "  ",
    sort: str = "",
) -> List[str]:
    """The ranked table's rows. See :func:`_entry_rows` for the reasoning.

    A thin wrapper, because the rows and the width of their inode column are one
    decision -- the header has to agree with them -- and `_table` needs both.
    Callers that only want the rows keep this signature and this return type.
    """
    return _entry_rows(res, top, by_inodes, style, indent, sort)[0]


def _density_floor_note(
    res: WalkResult, style: ui.Style, indent: str = "  ", shown: int = 0, qualifying: int = 0
) -> List[str]:
    """Why a density ranking is short, or empty.

    ``top_dirs`` drops any subtree holding fewer than
    :attr:`WalkResult.density_floor` inodes, because files-per-GiB is won by the
    smallest denominator and a 4 KiB directory with three files in it is not the
    answer to "what should I pack". That filter is right and it is also invisible:
    on an ordinary tree it removes *everything*, and ``rdu --sort density`` printed
    a headline, no table, and exited 0 -- which reads as "there is nothing dense
    here" when what happened is that the question was never answered.

    **Two reasons, two sentences.** ``qualifying`` is how many entries clear the
    floor and ``shown`` is how many ``-n`` then left, and conflating them made
    this note state, as a fact, something that was false: with all four entries of
    a tree above the floor, ``-n 1`` printed "3 of 4 entries hold fewer than 100
    files". The arithmetic was measuring the slice. It matters more here than it
    would elsewhere, because :func:`render_entries` deliberately suppresses both
    the remainder row and the "N more" line for a density listing -- so this is
    the *only* signal that rows are missing, and it was pointing away from the
    flag that would show them.

    Two lines at most, and only when something was actually dropped.
    """
    total = _entry_total(res)
    below_floor = max(0, total - qualifying)
    truncated = max(0, qualifying - shown)
    if not below_floor and not truncated:
        return []
    out = []  # type: List[str]
    if below_floor:
        tail = (
            ""
            if qualifying
            else " Nothing here clears it, so there is no density ranking to show;"
            " --sort files ranks the same tree by inode count."
        )
        out.extend(
            _wrapped(
                # `below_floor` is >= 1 by the guard above, so the verb has to
                # agree with it: "1 of 4 entries hold fewer than 100 inodes".
                "{} of {} {} fewer than {} inodes and cannot be ranked by "
                "density -- the measure is files per GiB, so a nearly empty directory "
                "wins it on the denominator alone.{}".format(
                    human_count(below_floor),
                    human_count(total),
                    "entry holds"
                    if total == 1
                    else ("entries holds" if below_floor == 1 else "entries hold"),
                    human_count(res.density_floor),
                    tail,
                ),
                style,
                indent,
            )
        )
    if truncated:
        # The one case where "use -n 0 for all" is a true instruction: these rows
        # cleared the floor and were cut by the limit, so raising it brings them
        # back.
        out.extend(
            _wrapped(
                "{} more {} the floor but {} cut by -n {} use -n 0 for all.".format(
                    human_count(truncated),
                    "clears" if truncated == 1 else "clear",
                    "was" if truncated == 1 else "were",
                    ui.dash(style),
                ),
                style,
                indent,
            )
        )
    return out


_BAR_W = 18


def _entry_line(
    name: str,
    size: int,
    inodes: int,
    value: float,
    fraction: float,
    total: int,
    tone: str,
    style: ui.Style,
    indent: str,
    aggregate: bool = False,
    size_hidden: bool = False,
    ranked_by_files: bool = False,
    density: Optional[float] = None,
    ranked_by_density: bool = False,
    inode_width: int = _INODE_COL,
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

    ``density`` adds the files-per-GiB column, and only ``--sort density`` passes
    it. Before that the rows were *ordered* by a number that appeared nowhere in
    the output -- neither column moved monotonically down the table and the reader
    had no way to see the value they had asked to rank by, or to check the ranking
    against anything. A ranking whose key is invisible is indistinguishable from a
    broken sort.
    """
    lead = style.muted if aggregate else tone
    bar = ui.bar(fraction, _BAR_W, style, accent=lead, hatched=aggregate)
    # In count mode there are no sizes at all, so the column is omitted rather
    # than left as ten blank characters the eye has to step over.
    quiet = ranked_by_files or ranked_by_density
    size_tone = style.muted if (quiet and not aggregate) else lead
    files_tone = lead if ((ranked_by_files and not ranked_by_density) or aggregate) else style.muted
    size_cell = "" if size_hidden else style.paint(human_bytes(size).rjust(10), size_tone) + "  "
    if density is None:
        dens_cell = ""
    else:
        dens_tone = style.muted if aggregate else lead
        dens_cell = "  " + style.paint("{:>10,.0f}".format(density), dens_tone)
    return "{}{}{}  {}  {}{}  {}".format(
        indent,
        size_cell,
        bar,
        style.paint("{:>6}".format(pct(value, total) if total else ""), lead),
        style.paint("{:>{w}}".format(human_count(inodes), w=inode_width), files_tone),
        dens_cell,
        # `muted`, not `dim`. The remainder row names real content -- "91 more" is
        # a measurement of the tree, not a caveat about it -- and painting it the
        # context grey made the one row that tells you the table is truncated the
        # faintest thing on screen. The hatched bar already says it is a summary;
        # it does not need to whisper as well.
        style.paint(name, *([style.muted] if aggregate else [tone])),
    )


def _entries_rule(style: ui.Style, rows: List[str], indent: str = "  ") -> str:
    """A hairline between the header and the table, sized to the table.

    One dim rule separates the header from the table. It is drawn in the same
    near-background grey as the bar tracks and the outer frame, so the three read
    as one piece of structure rather than three competing ones.

    Sized to the widest row rather than to the terminal, because a rule running
    forty characters past the last column looks like a mistake. That also means it
    is *narrower* than the frame around it, which is deliberate: the frame bounds
    the report, the rule divides one section of it.

    **Measured off the rows it is drawn over, not reconstructed from a column
    tally.** The tally was wrong twice over: it double-counted the indent, so the
    rule overhung by two columns on every listing ever printed, and it counted the
    12-column size field in ``-c`` mode where that field is not printed at all, for
    a 14-column overhang. It also took its widest name from a *differently sorted*
    list than the table, so ``--sort density`` sized the rule from rows that were
    not in it. Passing the rendered rows in makes all three impossible, and keeps
    the rule right when a column is added -- as the density column just was.
    ``ui.visible_width`` is what does the work: escapes are free and a wide glyph
    costs two columns.
    """
    glyph = "\u2500" if style.unicode else "-"
    widest = max([ui.visible_width(r) for r in rows] or [len(indent) + 8])
    span = min(style.width - 1, widest) - len(indent)
    # The same near-background grey the bar tracks use, so the rule and the
    # eighteen boxes below it read as one frame rather than two greys.
    return style.paint(indent + glyph * max(20, span), style.track)


def _entries_header(
    style: ui.Style,
    indent: str = "  ",
    size_label: str = "size",
    bar_label: str = "share",
    ranked_by_files: bool = False,
    density: bool = False,
    inode_width: int = _INODE_COL,
) -> str:
    """Column labels, with the one the table is sorted by marked.

    The sort key is otherwise invisible: a ``-i`` listing and a default listing
    have identical headers and differ only in an ordering the reader has to
    infer by checking two rows against each other. Marking the active column
    costs nothing and answers it at a glance.

    The count column is headed ``inodes``. It used to be ``files``, on the
    argument that the quota the reader is up against calls them files
    (``files (user) 21,553 / 300,000``) and includes directories exactly as this
    column does, so borrowing the quota's word saved a translation step.

    That argument assumed ``files`` was unambiguous here, and it is not: this
    package's own ``--json`` uses ``files`` for *non-directory entries only*,
    excluding directories, beside a separate ``inodes``. So one word named two
    quantities depending on where you read it -- which is what RD-9 filed against the
    headline, and the headline is not a special case. ``inodes`` everywhere this
    tool counts inodes; ``files`` only where it means non-directory entries
    (``BY AGE``, the headline breakdown) or where it is quoting a backend's own
    label (the ``QUOTA`` rows). The translation step the old wording avoided is
    now carried by :func:`render_quota`, which states the equivalence once, where
    the two vocabularies actually meet -- ``RECONCILE`` bridges them structurally
    (its ``files`` line compares the quota's files figure against the walk's inode
    count) but never said they were the same quantity, which is not good enough for
    a reader holding "files 26,633 / 300,000" and "14 inodes".

    The last column is ``entry``. ``directory`` would be a lie, because plain files
    are ranked here too -- three 63 MiB ``.db`` files in a home directory are a
    quarter of it. ``path`` was the previous answer and it was also wrong, in the
    other direction: what is printed is a *name* relative to the walk root, not a
    path, and calling it one while listing `msg3_plain.db` beside `ArgonneAI/`
    invites the reader to expect something they are not being given. ``name`` says
    nothing, since every column is the name of something.

    ``entry`` is what these actually are -- directory entries, which is precisely
    the category that contains both -- and it is already the word the facts line
    uses for their count ("94 entries"), so the two agree.

    ``share`` labels the bar, and the percentage beside it goes unlabelled, because
    they are one column in two forms -- the picture and the number. The bar used to
    be headed ``of tree``, which put ``size  of tree`` next to each other and read
    as the phrase "size of tree": a description of the whole row, which is not what
    either word was doing there. Two headers for one measurement was the actual
    mistake; naming it once, over the wider of its two forms, fixes it.
    """
    # One rule for the whole row: every label is a label, so every label carries
    # the same weight -- except the column the table is sorted by, which carries
    # the accent. That was three rules before ("size" bold, "files" bold-or-dim,
    # "share" and "entry" always dim), and three rules that each look like
    # emphasis add up to none: a reader cannot tell what the bold is *for*.
    #
    # `share` takes the sorted column's weight rather than a weight of its own,
    # because the bar underneath it draws whichever metric was ranked. Emphasising
    # the number but not the picture of the same measurement is the same mistake
    # one level down.
    ranked, plain = "bold", "dim"
    # `density` is a third ranked column, so it takes the accent off both of the
    # others: under `--sort density` neither the bytes nor the file count is what
    # the table was ordered by, and marking one of them would point at the wrong
    # number as confidently as the right one.
    size_tone = plain if (ranked_by_files or density) else ranked
    files_tone = ranked if (ranked_by_files and not density) else plain
    head = "" if not size_label else style.paint("{:>10}".format(size_label), size_tone) + "  "
    dens = "" if not density else "  " + style.paint("{:>10}".format("files/GiB"), ranked)
    # The bar draws whichever metric was ranked, so its label inherits that
    # column's weight rather than having one of its own.
    bar_tone = ranked if density else (files_tone if ranked_by_files else size_tone)
    return "{}{}{}  {}  {}{}  {}".format(
        indent,
        head,
        style.paint("{:<{}}".format(bar_label, _BAR_W), bar_tone),
        " " * 6,
        style.paint("{:>{w}}".format("inodes", w=inode_width), files_tone),
        dens,
        style.paint("entry", plain),
    )


def _other_count(res: WalkResult, shown: List[Any]) -> int:
    return max(0, len([e for e in res.dir_agg.values() if e.path != res.root]) - len(shown))


def _entries_partition_tree(res: WalkResult) -> bool:
    """True when every reported entry is a direct child of the root."""
    parents = {os.path.dirname(e.path) for e in res.dir_agg.values() if e.path != res.root}
    return parents == {res.root}


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
    for value, label, tones in pairs:
        # `tones` is passed whole rather than assembled here: pairing every fact
        # with "bold" would emit dim+bold together for the elapsed time, and a
        # terminal that implements faint as "not bold" resolves that to whichever
        # code it saw last -- so the quietest fact came out as the loudest.
        cell = style.paint(value, *tones)
        out.append(cell + " " + style.paint(label, "dim") if label else cell)
    return joiner.join(out)


def _header(
    style: ui.Style, headline: str, path: str, subtitle: str, headline_tone: str = ACCENT
) -> List[str]:
    """Name the subject, then measure it.

    **The path leads.** It is what the report is *about*, and every other line
    below it is a number describing it. Putting the size first followed ``du -sh``,
    which prints one number and nothing else -- but this report prints four
    measurements, so leading with one of them left the size stranded on its own
    line while its three siblings sat on the next. The path is the title; the
    size is the first fact.

    **One weight for the whole path.** It used to be split -- the leading
    directories dimmed, the last component bold -- so that the subject of a long
    path like ``/scratch/midway3/$USER/experiments/run-14`` could be found without
    reading it. That earned its keep when the path sat mid-line beside the size,
    competing for attention with a number.

    It does not any more, because the path now has a line to itself: the whole line
    *is* the subject, so there is nothing to pick it out from. What the split does
    instead is read as arbitrary -- on ``/home/youzhi`` it bolds one of two
    components and dims the other, which looks like emphasis with a reason nobody
    can guess. Emphasis that a reader cannot explain is noise, however consistent
    the rule behind it.

    The size keeps the accent it had, so it is still the first thing the eye
    lands on among the numbers -- it just no longer outranks the name of the
    thing it measures.
    """
    # The subject is a path from the filesystem, and it is measured for wrapping
    # two lines down, so it is escaped before either happens.
    subject = style.paint(ui.printable(path), "bold")
    facts = style.paint(headline, headline_tone)
    if subtitle:
        # The same joiner `_facts` uses, so the size sits in that line as one of
        # its members rather than as a prefix stuck on the front of it.
        facts += style.paint("  {}  ".format(ui.sep(style)), style.track) + subtitle
    return [subject, facts, ""]


def render_compact(
    res: WalkResult,
    settle: SettleCheck,
    top: int,
    by_inodes: bool,
    style: ui.Style,
    sort: str = "",
) -> List[str]:
    """The default view: how big is this tree, and what is big inside it.

    That is the question ``rdu .`` is asked, and answering it should look like
    ``du -sh`` with a breakdown -- not like a diagnostic report. Everything this
    tool knows that ``du`` does not (quota and its age, the reconciliation, the
    /proc scan) is real work the user did not ask for here, costs latency, and
    lives behind ``--full``.

    Only warnings that change what the number *means* survive into this view: an
    incomplete walk, or drift that was actually measured. A caveat that fires on
    every run is not a warning, it is furniture.
    """
    # The accent marks what the listing was ranked by, here as in the table below.
    # It used to sit on the byte total unconditionally, so under `-i` the report
    # accented the size while sorting on the file count -- pointing at one number
    # and ordering by another.
    ranked_by_files = bool(by_inodes or res.count_only)
    size_tone = VALUE if ranked_by_files else ACCENT
    files_tone = ACCENT if ranked_by_files else VALUE

    if res.count_only:
        out = _header(
            style,
            _count_phrase(res),
            res.root,
            _facts(
                style,
                [
                    ("counts only, no sizes", "", ("yellow",)),
                    ("{:.2f}s".format(res.elapsed), "", (VALUE,)),
                ],
            ),
            headline_tone=files_tone,
        )
    elif res.partial:
        out = _header(
            style,
            human_bytes(res.size),
            res.root,
            style.paint(
                "PARTIAL {} {} scanned before the interrupt".format(
                    ui.dash(style), _count_phrase(res)
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
                    (
                        human_count(res.inodes),
                        _count_noun(res, res.inodes),
                        (files_tone,),
                    ),
                    ("{:.2f}s".format(res.elapsed), "", (VALUE,)),
                ],
            ),
            headline_tone=size_tone,
        )
    out.extend(_hard_warnings(res, settle, style))
    out.extend(_provisional_note(res, settle, style))
    out.extend(render_allocation(res, style))
    hint = _count_hint(res, by_inodes, style)
    out.extend(_table(res, top, by_inodes, style, sort))
    out.extend(hint)
    return out


def _table(
    res: WalkResult,
    top: int,
    by_inodes: bool,
    style: ui.Style,
    sort: str,
    indent: str = "  ",
) -> List[str]:
    """Rule, header and rows -- or the reason there are none.

    Assembled in one place because the three have to agree about the sort key, the
    column set and the width, and when they were assembled twice (once for the
    compact view, once for the full report) they drifted: the header was told
    ``by_inodes`` while the rows were ranked by ``sort``, and the rule was sized
    from a third list again. It is also the only place that can notice a table came
    back empty and say why, which ``--sort density`` needed and did not have.
    """
    key = _sort_key(sort, by_inodes, res)
    body, inode_width = _entry_rows(res, top, by_inodes, style, indent, sort)
    note = []  # type: List[str]
    if key == "density":
        # The inode floor can shorten a density listing or empty it, and either way
        # the reader has to be told. Kept out of `render_entries` and appended after
        # the rows, so a paragraph of prose never ends up sizing the hairline --
        # which is measured off the widest row it is drawn over.
        # Both counts come from `top_dirs`, one limited and one not, so the note
        # can separate "below the floor" from "cut by -n" instead of charging the
        # floor for both.
        qualifying = len(res.top_dirs(_ALL, key, finished_only=res.partial))
        shown = len(res.top_dirs(_limit(top), key, finished_only=res.partial))
        note = _density_floor_note(res, style, indent, shown, qualifying)
    if not body:
        # No rows at all: either the floor took everything, which is worth
        # explaining, or the tree has no reportable children and there is nothing
        # to say.
        return ([""] + note) if note else []
    return (
        [
            _entries_rule(style, body, indent),
            _entries_header(
                style,
                indent=indent,
                size_label="" if res.count_only else "size",
                # An interrupted walk has no total to be a share of, and a density
                # is not a share of anything, so the bar falls back to ranking
                # against the largest row and says so.
                bar_label="vs largest" if (res.partial or key == "density") else "share",
                ranked_by_files=key == "files",
                density=key == "density",
                inode_width=inode_width,
            ),
        ]
        + body
        + note
    )


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
    # No predicted runtime. The old form divided this walk's elapsed time by
    # COUNT_SPEEDUP and printed the quotient as "(Ns here)", which turned a
    # constant measured on cold GPFS into a forecast for whatever filesystem is
    # actually in front of the user. Re-measured across three: 8.5x on a large
    # GPFS tree, 2.1x on a page-cached local one, 1.6x on a small warm GPFS tree
    # -- so the prediction was out by -74% and -80% on two of the three. The
    # ratio is a property of the filesystem's stat latency, not of this tool, and
    # Constraint 18 applies to a number synthesised from a measurement as much as
    # to one invented outright. Name where it was measured; promise nothing here.
    return [
        style.paint(
            "  hint: -i -c answers this without stat -- measured ~{:.0f}x faster on GPFS, "
            "less on a page-cached local filesystem; a hard-linked file then counts "
            "once per name, not once per inode.".format(walk_speedup()),
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
                # `{:.2f}`, as every other rendering of `res.elapsed` uses. With
                # `{:.0f}` a walk cut short after 0.4s reported "INTERRUPTED after
                # 0s", which reads as "instantly" directly above "20,051 inodes
                # scanned before the interrupt" -- and a Ctrl-C landing inside the
                # first second is the common case, not the rare one. The settle
                # lines already guard sub-second gaps (`if gap >= 1`); this one did
                # not.
                "INTERRUPTED after {:.2f}s -- this is not a measurement of the tree.".format(
                    res.elapsed
                ),
                style,
            )
        )
        # No denominator. It used to read "N of M top-level entries", with M taken
        # from `dir_agg` -- the entries that had been *merged* -- so on an
        # interrupted walk with a blocked worker it printed "2 of 2", meaning "all
        # of them", while four subtrees had finished and two were being withheld.
        # A ratio whose denominator is the same partial state as its numerator
        # cannot say what it looks like it says, and there is no honest M here:
        # how many entries the tree has is exactly what the walk did not find out.
        # "and are listed below" named a table that is not there when the count is
        # zero: `render_entries` returns [] once `finished_only` has filtered
        # everything, which is exactly the case this sentence is most likely to
        # describe (every worker blocked on a slow mount, so nothing finished).
        finished = len(res.top_dirs(_ALL, finished_only=True))
        out.extend(
            _wrapped(
                "{} top-level {} walked to completion{}; the rest is unknown, so"
                " there is no total and no share of anything.".format(
                    human_count(finished),
                    "entry was" if finished == 1 else "entries were",
                    (" and is listed below" if finished == 1 else " and are listed below")
                    if finished
                    else "",
                ),
                style,
                "  ",
            )
        )
        if res.abandoned_workers:
            # The measurements those threads held were dropped rather than merged
            # into a result the caller had already been handed. That makes every
            # figure below lower than what the walk had actually counted, which is
            # not a detail -- it is the difference between "small tree" and
            # "abandoned walk".
            out.extend(
                _wrapped(
                    "{} walk thread{} still blocked -- almost certainly a stat or"
                    " getdents on an unresponsive mount -- so the counts they had"
                    " already made were discarded. The figures below are lower than"
                    " what the walk had reached.".format(
                        res.abandoned_workers,
                        " was" if res.abandoned_workers == 1 else "s were",
                    ),
                    style,
                    "  ",
                )
            )
    elif not res.complete:
        detail = []
        if res.unreadable_dir_count:
            # Split by cause. "unreadable" about a directory that had been deleted
            # sends the reader after access they already have; on a shared
            # filesystem the usual cause is another job writing to the tree.
            #
            # The COUNT, not the length of the path sample: the sample is capped
            # and this figure is the finding.
            refused = res.unreadable_dir_count - res.vanished_dirs
            if refused > 0:
                detail.append("{} unreadable".format(plural(refused, "dir")))
            if res.vanished_dirs:
                detail.append("{} vanished mid-walk".format(plural(res.vanished_dirs, "dir")))
        if res.unstatable:
            # Split by cause, as the directory half above is: an entry deleted
            # while the walk was in it is the tree moving, not access withheld.
            unreachable = res.unstatable - res.vanished_entries
            if unreachable > 0:
                detail.append(
                    "{} unstatable".format(plural(unreachable, "entry", irregular="entries"))
                )
            if res.vanished_entries:
                detail.append(
                    "{} vanished mid-walk".format(
                        plural(res.vanished_entries, "entry", irregular="entries")
                    )
                )
        out.append(ui.alarm("this is a FLOOR, not a total: " + ", ".join(detail), style))
    if res.crossed:
        # `-x` is a cap the user asked for, so this is yellow rather than red --
        # nothing failed. But it is still a cap, and it was applied in silence:
        # `rdu -x /scratch` where /scratch holds three clusters' filesystems
        # reported `0 B - 1 files` and nothing else, which reads as "/scratch is
        # empty". Every other bound this walk applies is published; so is this one.
        # The paths are named because they are what makes the number actionable --
        # they are the mounts to walk separately.

        note = (
            "{} {} on other filesystems skipped (-x): this total covers one "
            "filesystem, not everything under the path".format(
                human_count(res.crossed), "entry" if res.crossed == 1 else "entries"
            )
        )
        out.extend(_warn_wrapped(note, style))
        # `... and 1 more` costs the same line as the path itself and leaves the
        # count unverifiable: on a /scratch with four mounts it showed three paths
        # and hid the fourth, and the correct 4 was read as a double-counted 3.
        # So one hidden entry is never hidden; two or more earn the summary.
        listed = res.crossed_paths[:_CROSSED_SHOW]
        if len(res.crossed_paths) == _CROSSED_SHOW + 1 and res.crossed == len(res.crossed_paths):
            listed = res.crossed_paths[: _CROSSED_SHOW + 1]
        for path in listed:
            # Truncated keeping the tail, the way every other path in this report
            # is: `.../scratch/beagle3` is the part that identifies the mount, and
            # a full scratch path is routinely wider than the frame.
            out.append(
                style.paint(
                    "      " + ui.truncate(ui.printable(path), max(20, style.width - 8)), "dim"
                )
            )
        hidden = res.crossed - len(listed)
        if hidden > 0:
            # Listed + hidden == the headline count, always. The two halves of this
            # message are now arithmetic rather than two independent statements.
            out.append(style.paint("      ... and {} more".format(hidden), "dim"))
    if res.inodes >= walkmod._MEMORY_NOTE_ENTRIES:
        # rapidu holds state proportional to the tree it is measuring, and the
        # trees worth pointing it at are the large ones. The two structures that
        # can be bounded now are (`watched`, `unreadable_dirs`); the breadth-first
        # frontier and the hard-link set cannot be, without changing what is
        # measured. So this is said rather than left for the OOM killer to say --
        # the same rule as `-x`, the unreadable count and the interrupt: every
        # bound this walk works under is published.
        out.extend(
            _warn_wrapped(
                "{} inodes walked, and rapidu holds roughly {} of memory for "
                "each: this walk needed on the order of {}. A tree ten times "
                "the size needs ten times that -- walk it in parts if that "
                "matters here.".format(
                    human_count(res.inodes),
                    human_bytes(walkmod._BYTES_PER_ENTRY),
                    human_bytes(res.inodes * walkmod._BYTES_PER_ENTRY),
                ),
                style,
            )
        )
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
    sort: str = "",
) -> List[str]:
    """The walk block of the full report: headline, then facts worth a line each.

    The headline carries only the path and the total. The breakdown moved down to
    the facts line because a long scratch path plus the counts ran past 100
    columns and got clipped -- and the clipped end was the number, not the path.
    """
    style = style or ui.resolve_style("never")
    # Same rule as the compact view and the table: the accent marks what the
    # listing was ranked by, so under `-i` it belongs on the count, not the bytes.
    #
    # In count mode there is no byte figure at all, and this printed `0 B` -- and
    # `apparent 0 B` under it -- which is the one thing this module's own docstring
    # forbids: an absent measurement prints `n/a` with a reason, never `0`. A
    # reader given "0 B" for a tree with 165 files has been told something false,
    # while `render_compact` on the same walk said "counts only, no sizes".
    headline = "n/a" if res.count_only else human_bytes(res.size)
    out = [
        "",
        "{}  {}   {}".format(
            ui.heading("WALK", style),
            style.paint(ui.truncate(ui.printable(res.root), max(24, style.width - 28)), "bold"),
            style.paint(headline, VALUE if (by_inodes or res.count_only) else ACCENT),
        ),
    ]

    facts = [
        # `inodes`, not `files`. This figure counts directories too, and an inode
        # is what an inode quota charges -- separating that from byte pressure is
        # the whole point of the tool, so the most-read line is the last place to
        # blur it. The breakdown that follows is what makes the word concrete.
        "{} ({})".format(_count_phrase(res), _inode_breakdown(res)),
        # The rate divides the same figure, so it carries the same noun. Printing
        # "424 inodes ... 35,151 files/s" put both labels on one number.
        "{:.2f}s at {} threads ({:,.0f} {}/s)".format(
            res.elapsed,
            res.threads,
            res.inodes / res.elapsed if res.elapsed > 0 else 0.0,
            _count_noun(res, 2),
        ),
    ]
    if res.count_only:
        # The reason the headline is n/a, on the line where the reader looks for
        # the number that is missing.
        facts.insert(0, "counts only, no sizes: -c skips stat entirely")
    else:
        # Stated as a ratio, not as a bare second number. "apparent 23.6 MiB"
        # beside "187.6 MiB" left the reader to divide.
        facts.append(
            "apparent {}{}".format(
                human_bytes(res.apparent),
                # `is not None`, not truthiness: a ratio of exactly 0.0 is a
                # measurement -- nothing was allocated for data that is there --
                # and it was the one this line silently dropped.
                " ({} allocated)".format(ratio_x(res.alloc_ratio))
                if res.alloc_ratio is not None
                else "",
            )
        )
    if res.alloc_unit:
        facts.append("{} allocation unit".format(human_bytes(res.alloc_unit)))
    if res.hardlinked_inodes:
        facts.append(
            "{}, {} deduped".format(
                plural(res.hardlinked_inodes, "hard-linked file"),
                plural(res.hardlink_extra_refs, "extra name"),
            )
        )
    if scan is not None and scan.available and scan.silly_renamed:
        # Named on the facts line for the same reason the walk's own figures are:
        # this is the line a reader of `rdu -a` scans, and on an NFS site it was
        # the line that said the space was not there.
        facts.append(
            "{} held by deleted-but-open files on NFS (see below)".format(
                human_bytes(scan.silly_renamed_size)
            )
        )
    elif scan is not None and scan.available and not scan.files:
        # "none visible", not "none": this scan sees neither other users'
        # processes nor any compute node. See `render_deleted`.
        facts.append(
            "no unlinked-but-open space visible ({} of {} pids inspectable, {})".format(
                scan.scanned_pids,
                scan.scanned_pids + scan.unreadable_pids,
                "this PID namespace only" if scan.namespaced else "this node only",
            )
        )
    # Packed onto as many lines as it takes, not forced onto one. Joined raw this
    # reached 230 columns with every fact present -- it overflowed an 80-column
    # terminal long before there was a frame to break, and inside one it is the
    # line that breaks it. Packed by fact, so no fact is ever split in half.
    out.extend(_packed(facts, style, "  "))

    out.extend(_hard_warnings(res, settle, style, settling=False))
    out.extend(render_allocation(res, style, indent="    "))
    out.extend(render_settle(res, settle, style))

    if not res.complete and res.unreadable_dirs:
        for path, why in res.unreadable_dirs[:3]:
            out.append(style.paint("      {} ({})".format(ui.printable(path), why), "dim"))
        if res.unreadable_dir_count > 3:
            out.append(
                style.paint("      ... and {} more".format(res.unreadable_dir_count - 3), "dim")
            )

    def owner_rows(counts, name_of):
        """The rows one block shows, biggest first, as ``(name, size, inodes)``."""
        ranked = sorted(counts.items(), key=lambda kv: kv[1][0], reverse=True)
        return [(name_of(k), size, inodes) for k, (size, inodes) in ranked[:_OWNER_SHOW]]

    uid_rows = owner_rows(res.by_uid, _uname) if show_uids and len(res.by_uid) > 1 else []
    gid_rows = owner_rows(res.by_gid, _gname) if show_uids and len(res.by_gid) > 1 else []
    # The name column is measured from the rows, in COLUMNS. It was a hard-coded
    # `{:<16}` filled by character count, for a string this tool does not choose:
    # NSS hands back whatever the site's directory holds, and two names in this
    # cluster's own passwd and group overflow sixteen today, in plain ASCII. The
    # account `gnome-initial-setup` (19) pushes its row's bytes, inodes and noun
    # three columns right of the column every other row keeps them in, and the
    # group `caprioli-cattaneo-software` (26) pushes them ten. A name in Chinese
    # is two columns per character on top of that, which `len` cannot see at all.
    # Same defect and same fix as the fileset column in `render_quota` -- measure
    # with `ui.visible_width`, fill with `ui.pad`, bound with `ui.truncate`.
    #
    # One width across both blocks rather than one each: they print as a single
    # block under two captions, and the comparison those captions exist for --
    # the same bytes attributed by uid and by gid -- is only legible if both name
    # columns end in the same place. Floor and cap are the fileset column's, and
    # 6 + 40 + 12 + 2 + 12 + " inodes" is still inside eighty columns.
    name_w = max([16] + [ui.visible_width(n) for n, _s, _i in uid_rows + gid_rows])
    name_w = min(name_w, 40)

    def owner_row(row):
        """One row of either block, in the shared name column."""
        name, size, inodes = row
        return "      {}{:>12}  {:>12} {}".format(
            ui.pad(ui.truncate(name, name_w), name_w),
            human_bytes(size),
            human_count(inodes),
            # See the reclaim rows: the count is right-aligned in its column and
            # the noun agrees outside it, which is what `noun` is for. A home
            # shared with one root-owned file printed `1 inodes` here.
            noun(inodes, "inode"),
        )

    if uid_rows:
        out.append("")
        out.append(style.paint("  owners:", "dim"))
        out.extend(owner_row(row) for row in uid_rows)
        out.extend(_and_more(len(res.by_uid), _OWNER_SHOW, "owners", style))
    # The uid table used to be captioned "a group quota charges all of these",
    # which is the wrong table: a group quota is charged by **gid**. The two
    # diverge exactly when it matters -- a file written into a shared project
    # directory whose setgid bit is missing is charged to the writer's personal
    # group, where nobody is looking for it.
    if gid_rows:
        out.append(style.paint("  groups (a group quota charges these):", "dim"))
        out.extend(owner_row(row) for row in gid_rows)
        out.extend(_and_more(len(res.by_gid), _OWNER_SHOW, "groups", style))

    out.extend(_table(res, top, by_inodes, style, sort))
    # Only in the full report. `rdu .` is asked how big a tree is, not for an
    # audit -- the same reason the quota and /proc sections sit behind -a.
    out.extend(render_age(res, style))
    out.extend(render_reclaimable(res, style))
    return out


# How many skipped paths the report names before summarising the rest. Three keeps
# the section short; see `_hard_warnings` for why a remainder of one is never
# summarised.
_CROSSED_SHOW = 3

# How many owners or groups the walk section lists before saying how many it did
# not. Six fits the section; the count is what keeps the cap from being silent.
_OWNER_SHOW = 6

# How many holding processes one unlinked-but-open inode names.
_HOLDER_SHOW = 3


def _and_more(total: int, shown: int, noun: str, style: ui.Style) -> List[str]:
    """A line naming what a cap left out, or nothing when it left out nothing.

    Every bound in this report is supposed to publish itself -- unreadable
    directories, `-x` skips, reclaim commands, the re-stat sample all do. These
    two tables did not, and the `groups` one is captioned "a group quota charges
    these": the group a quota row actually names can sit seventh by bytes and never
    appear, while the rows shown do not sum to the total and nothing says why.
    """
    hidden = total - shown
    if hidden <= 0:
        return []
    return [style.paint("      ... and {} more {}".format(hidden, noun), "dim")]


def _settle_subject(res: WalkResult) -> str:
    """What the settle window actually saw, as a noun phrase.

    Three different observations used to print as one sentence -- "N files were
    written" -- and only the first of them was a write. See
    :attr:`walk.WalkResult.touched_files`.
    """
    parts = []  # type: List[str]
    if res.recent_files:
        parts.append("{} written".format(plural(res.recent_files, "file")))
    if res.touched_files:
        parts.append("{} changed without being written".format(plural(res.touched_files, "inode")))
    return " and ".join(parts) or "nothing changed"


def _measured_nothing_clause(settle: SettleCheck) -> str:
    """The verdict for a re-stat that had files to measure and measured none.

    One sentence, two callers: the compact settling line embeds it after its
    subject, and the ``SETTLING`` panel prints it on its own. It is a helper
    rather than a string spelled twice because the two spellings had already
    come apart -- the compact line said this, and the panel, for the same
    ``SettleCheck``, said *"figure is PROVISIONAL -- use --settle-wait 60 to
    measure the drift"*. That advice is wrong here twice over, and the two
    faults are separate: the sample was deleted, so a longer wait deletes more
    of it rather than measuring it; and ``gap`` is on the object, so the sixty
    seconds offered may be *less* than the wait the reader already sat through
    (measured: ``rdu -a --settle-wait 60`` printed it after 60s).

    So what is said instead is what was observed -- the instrument took no
    reading -- and no remedy is offered, because none of the knobs this tool has
    is one. The count is the subject; the byte figure that says how wrong the
    headline is stays where it already was, in ``render_compact``'s
    :func:`_provisional_note` and in ``--json``'s ``vanished_allocated_bytes``.
    Deliberately not repeated here: this clause is printed by both surfaces, and
    only one of them is the one with room for the number.
    """
    return (
        "a re-stat {:.0f}s later found {} already deleted and none left to"
        " measure, so the figure is provisional".format(settle.gap, plural(settle.gone, "file"))
    )


def render_settle(
    res: WalkResult, settle: SettleCheck, style: Optional[ui.Style] = None
) -> List[str]:
    """The "this tree has not settled" warning -- a truth ``du`` cannot tell."""
    style = style or ui.resolve_style("never")
    if not res.recent_files and not res.touched_files:
        return []

    # A handful of freshly written files in a large tree cannot move the
    # headline number, and spending five lines saying so trains the reader to
    # skip the section on the run where it matters. Compact unless it is either
    # measurably moving or big enough to move things.
    if not settle.moved and not _settling_is_material(res):
        # The clock clause is not subject to the materiality threshold. A handful
        # of future-dated files cannot move the headline number, but they *can*
        # make it read provisional on every run forever, and the compact line is
        # then the only place the reader could learn why.
        clock = (
            " ({} with an mtime ahead of this node's clock)".format(human_count(res.future_files))
            if res.future_files
            else ""
        )
        # Files deleted out from under the re-stat, which only the long form said.
        # This compact form is the default for every tree whose recent files
        # cannot move the headline -- the common case -- so on that run the
        # terminal was the one view that did not mention that the population the
        # drift was measured over had shrunk underneath it. `to_json` has
        # published `vanished_files` from the start and its comment there claims
        # "the terminal reports it", which was true of one branch of two.
        vanished = (
            " ({} disappeared between the walk and the re-stat)".format(human_count(settle.gone))
            if settle.gone
            else ""
        )
        # A re-stat that ran long enough to see an effect and saw none has
        # *answered* this, and the long form below has always said so ("found no
        # change in N of them; the figure looks settled"). This line ignored
        # `conclusive` and printed "provisional (--settle-wait 60 to measure)"
        # regardless -- so `rdu --settle-wait 120` on a tree that had not moved in
        # two minutes was told its figure could not be trusted and advised to
        # wait sixty seconds, which is both a contradiction of the measurement it
        # had just taken and less than the wait it had just performed. `to_json`
        # got this right from the start (`settled` consults `conclusive`), so the
        # document and the terminal disagreed about the same check.
        if settle.conclusive:
            # "The figure looks settled" is a claim about the figure, not about
            # the sample, and the previous round left it standing over a partial
            # vanishing on the grounds that the survivors genuinely had not
            # moved. They had not -- but with seven of eight files unlinked the
            # sentence read "found no change in 1 file (7 disappeared between the
            # walk and the re-stat); the figure looks settled" above a 512.0 KiB
            # headline for a tree holding 64.0 KiB. Disclosing the deletion does
            # not repair a verdict that contradicts it, so above the factor bar
            # the verdict is withdrawn and the error stated instead; below it the
            # sentence is unchanged, which is the whole point of having a bar.
            if _freed_since_walk_is_material(res, settle):
                return _wrapped(
                    "{:<22}{}{} in the last {} -- a re-stat {:.0f}s later found no"
                    " change in the {} still there, but {} holding {} vanished in"
                    " between, so the total above stays provisional: it counts"
                    " blocks the filesystem already freed".format(
                        "settling",
                        _settle_subject(res),
                        clock,
                        human_duration(res.settle_window),
                        settle.gap,
                        plural(settle.checked, "file"),
                        human_count(settle.gone),
                        human_bytes(settle.gone_bytes),
                    ),
                    style,
                    "  ",
                )
            return _wrapped(
                "{:<22}{}{} in the last {} -- a re-stat {:.0f}s later found no"
                " change in {}{}; the figure looks settled".format(
                    "settling",
                    _settle_subject(res),
                    clock,
                    human_duration(res.settle_window),
                    settle.gap,
                    plural(settle.checked, "file"),
                    vanished,
                ),
                style,
                "  ",
            )
        # How provisional. The sentence said the figure could not be trusted
        # without saying by how much, so a reader had no way to tell a tree that
        # might move by a kilobyte from one that will move by a factor -- and it
        # is the same measurement the default view now gates on.
        # "not yet allocated" asserted a cause. The measurement is that the bytes
        # are *unallocated*; whether they are still coming or were never going to
        # is what the sparse/compressed panel above answers, and this line used to
        # contradict it in the same report.
        # Gated on the figure it prints, not only on the verdict: since
        # `_headline_is_provisional` gained a second, unrelated reason to say yes
        # (blocks freed by deletions, which are the opposite of unallocated), the
        # verdict alone put ", holding 0 B unallocated" into the
        # whole-sample-deleted sentence -- a clause asserting the reading it is
        # made of was zero.
        held = (
            ", holding {} unallocated".format(human_bytes(_unlanded_bytes(res)))
            if _unlanded_bytes(res) and _headline_is_provisional(res, settle)
            else ""
        )
        # A re-stat that ran long enough but re-stat'ed nothing -- see
        # `SettleCheck.recheck_measured_nothing`. Waiting longer is not the
        # remedy here and must not
        # be offered as one: the sample was deleted, so a longer wait only deletes
        # more of it, and the wait already performed may well have been longer
        # than the sixty seconds the other branch suggests.
        if settle.recheck_measured_nothing:
            return _wrapped(
                "{:<22}{}{} in the last {}{} -- {}".format(
                    "settling",
                    _settle_subject(res),
                    clock,
                    human_duration(res.settle_window),
                    held,
                    _measured_nothing_clause(settle),
                ),
                style,
                "  ",
            )
        # Otherwise only reachable when the check did not run, or ran for less
        # than `MIN_CONCLUSIVE_GAP_S`, so sixty seconds is always longer than
        # whatever was already tried.
        return _wrapped(
            "{:<22}{}{} in the last {}{}{} -- figure is provisional"
            " (--settle-wait 60 to measure)".format(
                "settling",
                _settle_subject(res),
                clock,
                human_duration(res.settle_window),
                held,
                vanished,
            ),
            style,
            "  ",
        )

    out = ["", "  " + ui.heading("SETTLING", style)]
    out.extend(
        _wrapped(
            "{} in the last {}".format(_settle_subject(res), human_duration(res.settle_window)),
            style,
            "      ",
        )
    )
    if res.touched_files:
        # Two causes, one observation. A stat cannot tell them apart, so naming
        # both is the honest form -- asserting the write was the defect.
        out.extend(
            _wrapped(
                "an inode change without a write is either a metadata operation "
                "(permissions, ownership, a rename) or a delayed allocation "
                "completing; only the second moves the byte figure",
                style,
                "      ",
            )
        )
    if res.future_files:
        out.extend(
            _wrapped(
                # One future-dated file is the common case -- a single restored
                # timestamp -- and this read "1 of these carry an mtime ... cannot
                # be judged for them".
                "{} of these {} an mtime ahead of this node's clock, so "
                "'recently written' cannot be judged for {} -- most likely a "
                "clock difference between this node and the fileserver, or "
                "restored timestamps".format(
                    human_count(res.future_files),
                    "carries" if res.future_files == 1 else "carry",
                    "it" if res.future_files == 1 else "them",
                ),
                style,
                "      ",
                tone="yellow",
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
        # Wrapped, not hand-broken. This was three lines joined with "\n" inside a
        # single list element, which measured as one 200-column line and tore the
        # frame around it in half.
        out.extend(
            _wrapped(
                "Any size you read right now -- from this tool or from du -- is "
                "provisional. Measured on GPFS, a freshly written tree has settled "
                "both upward (5.58x) and downward (3.3x) over ~60s.",
                style,
                "        ",
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
    elif settle.conclusive and _freed_since_walk_is_material(res, settle):
        # Same withdrawal as the compact form above, in the view that has room to
        # name the number. The trailing "N of them disappeared" line below still
        # prints, and carries the count this one leaves out.
        out.extend(
            _wrapped(
                "re-stat {:.0f}s later found no change in the {} still there, but"
                " at least {} of the total above belongs to files that no longer"
                " exist -- the figure reads provisional, and high, not settled".format(
                    settle.gap,
                    plural(settle.checked, "file"),
                    human_bytes(settle.gone_bytes),
                ),
                style,
                "      ",
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
    elif settle.recheck_measured_nothing:
        # The same withdrawal the compact form makes, in the same words -- see
        # `_measured_nothing_clause`. This branch is why the clause is a helper:
        # the panel used to fall through to the `else` below and print "use
        # --settle-wait 60 to measure the drift" over a re-stat that had *nothing
        # to measure*, so `rdu -a --settle-wait 60` on a tree whose whole recent
        # sample was unlinked advised a sixty-second wait it had just performed,
        # to measure a sample that no longer exists. Two surfaces of one check
        # disagreeing about that check is the defect; sharing the sentence is what
        # stops them drifting apart again.
        out.extend(_wrapped(_measured_nothing_clause(settle), style, "      "))
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
    # Not after the clause above, which already names the count as the subject of
    # its own sentence ("found 60 files already deleted"). Every other branch
    # leaves the deletion unmentioned, so there this line is the only disclosure.
    if settle.gone and not settle.recheck_measured_nothing:
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

# Below this, "bytes are nearly free" is a fair reading of the measurement: a
# sparse file lands at ~0.00x and files living inside their inodes at ~0.02x, and
# at a quarter or less the inode count is plainly the binding cost. Above it the
# saving is real but the bytes are not free -- transparent compression routinely
# lands at 0.4x-0.7x, where being charged most of the data size is the fact that
# matters. One sentence cannot serve both ends of that range.
_ALLOC_NEARLY_FREE = 0.25
_ALLOC_FLOOR = rc.MIN_TOLERANCE_BYTES


def allocation_is_material(res: WalkResult) -> bool:
    """Does the gap between allocated and apparent change the answer?

    Both directions count. On the tree that motivated this the ratio is 8x the
    wrong way; one directory later the same filesystem stores 8.7 MiB in 1.6 MiB
    because the files fit inside their inodes. Neither is an error and both are
    the reason the headline number is not the number the reader expected.
    """
    # `alloc_ratio is None` is the only genuine absence here: no apparent bytes
    # to divide by, so there is no ratio to report. `count_only` is the other --
    # no stat, so neither figure exists.
    #
    # `not res.size` used to be a third, and it threw away the strongest case
    # this panel has. Allocated == 0 with apparent > 0 is not a missing
    # measurement, it is the maximum of the phenomenon: data that is charged
    # nothing. It is also reachable on any filesystem whose directories occupy no
    # blocks -- tmpfs is one, so a 10 MiB sparse file in /dev/shm reported "0 B,
    # 2 inodes" and not one word about the 10 MiB, while the same file under
    # /project (whose directories do allocate) got the full panel. The comment on
    # `_ALLOC_NEARLY_FREE` below already says a sparse file "lands at ~0.00x";
    # this guard excluded the exact point it lands on.
    if res.count_only or res.alloc_ratio is None:
        return False
    ratio = res.alloc_ratio
    if abs(res.size - res.apparent) < _ALLOC_FLOOR:
        return False
    return ratio >= _ALLOC_RATIO or ratio <= 1.0 / _ALLOC_RATIO


def _packed(items: List[str], style: "ui.Style", indent: str, sep: str = "  ") -> List[str]:
    """Pack whole items onto as few lines as fit, never splitting one.

    Word-wrapping this line was the obvious thing and it was wrong: each fact is
    a unit -- ``apparent 23.4 MiB (2.0x allocated)`` states a number *and* what it
    means, and a break between them leaves a bare figure on one line and a
    parenthetical on the next, which is the exact confusion the fact was reworded
    to remove. Packing at item boundaries keeps every fact whole and still fits
    the width.
    """
    width = max(40, _layout_width(style) - len(indent) - 1)
    lines = []  # type: List[str]
    current = ""
    for item in items:
        candidate = item if not current else current + sep + item
        if current and ui.visible_width(candidate) > width:
            lines.append(current)
            current = item
        else:
            current = candidate
    if current:
        lines.append(current)
    return [style.paint(indent + line, "dim") for line in lines]


# The margin every continuation in the reconciliation section already uses: the
# candidates, the notes, the blockers and the unlinked-but-open figure all hang
# at six columns. A verdict's figures dropping to their own line join them there
# rather than inventing a seventh alignment.
_VERDICT_MARGIN = "      "


def _verdict_headline(
    label: str, headline: str, tone: str, figures: str, style: "ui.Style"
) -> List[str]:
    """A verdict's ``label  headline  figures`` line, figures dropped when short.

    This is the first line of the RECONCILE section and the one a reader's eye
    lands on, and it was built as a fixed three-part line with no width test at
    all. Rendered it is 84 to 93 columns against a layout of 80 -- and, like the
    blocker line fixed just before it, insensitive to the terminal, so a wider
    one did not help and a narrower one only moved the damage. For ``blocks`` it
    overflows on *every* realistic pair of figures: ``4.0 KiB`` against
    ``8.0 KiB``, the smallest comparison the tool can report, already renders at
    85. Only a degenerate all-zero triple fitted.

    Soft-wrapping it with :func:`_wrapped` would be wrong, which is why it was
    left alone twice. ``textwrap`` breaks on whitespace and every figure here
    *contains* whitespace, so the break lands between ``47.7`` and ``MiB`` and
    turns one number into two. The terminal's own hard wrap already does exactly
    that -- an 87-column line at 80 leaves ``difference -82`` at the end of one
    row and ``3.4 GiB`` alone at column zero of the next, level with the section
    margin, where it reads as a separate statement.

    So the figures move instead of being wrapped. They are a column, not prose:
    kept whole, and dropped to the section's own six-column margin when the
    label and the headline do not leave room for them -- the same "never split
    one fact" rule :func:`_packed` is built on, applied to a line with exactly
    two parts to choose between. The verdict word itself is never displaced,
    because it is the one thing the line exists to say.

    When all three parts fit, the single line is emitted unchanged, byte for
    byte, including its painting. A short verdict -- ``files`` with four-digit
    counts, which is the common case on an inode quota -- must not pay for the
    byte-scale one.
    """
    head = "  {}  {}".format(label, style.paint(headline, tone))
    # One column short of the layout, matching `_wrapped` and `_packed`: the last
    # cell is left empty so a terminal at exactly this width does not wrap the
    # line it was just told fits.
    budget = _layout_width(style) - 1
    plain = "  {}  {}  {}".format(label, headline, figures)
    if ui.visible_width(plain) <= budget:
        return ["{}  {}".format(head, style.paint(figures, "dim"))]
    return [head, style.paint(_VERDICT_MARGIN + figures, "dim")]


def _wrapped(text: str, style: ui.Style, indent: str, tone: str = "dim") -> List[str]:
    """Soft-wrap a prose line to the terminal, painting each line separately.

    Colour has to be applied per output line rather than to the whole
    paragraph: an SGR run that spans a newline is reset by some terminals and
    inherited by others, and a paste into a ticket keeps whichever happened.
    Wrapping is measured on the *unpainted* text, because escape codes occupy no
    columns but `textwrap` would count them.
    """
    import textwrap

    width = max(40, _layout_width(style) - len(indent) - 1)
    wrapped = textwrap.wrap(text, width, break_on_hyphens=False)
    return [style.paint(indent + line, tone) for line in wrapped]


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
    # `allocation_is_material` has already refused `None`; keeping `or 1.0` here
    # meant a real 0.0 was rendered as 1.0, which selects the branch below that
    # says "allocated for ... 1.0x" -- allocation matches the data exactly. That
    # is the precise opposite of what a zero measures.
    ratio = res.alloc_ratio if res.alloc_ratio is not None else 1.0
    unit = res.alloc_unit
    out = []  # type: List[str]

    if ratio >= 1.0:
        head = "{} allocated for {} of data {} {}".format(
            human_bytes(res.size), human_bytes(res.apparent), ui.dash(style), ratio_x(ratio)
        )
        out.extend(_warn_wrapped(head + ". Your quota is charged the first number.", style))
        if res.padded_files and res.padding > 0:
            mean = res.padded_apparent // res.padded_files
            # The unit clause is dropped when the unit could not be measured, and
            # the sentence still lands: the padding total is the actionable
            # number and the remedy does not depend on knowing the unit.
            #
            # Requiring the unit meant the advice was withheld exactly where it
            # was most wanted. Every allocation in a tree of 64-byte files is one
            # 512-byte sector -- too small to estimate a unit from, by design --
            # so `unit` was `None`, and 21.4 MiB of padding across 50,000 files
            # went unmentioned on the most packable tree there is.
            against = " against a {} allocation unit".format(human_bytes(unit)) if unit else ""
            # A gap larger than partly filled units can produce is not padding,
            # whatever it is, and the remedy printed for it must not be
            # "packing returns it". See `WalkResult.unit_padding_ceiling`: on an
            # NFS-exported OneFS home the gap was 4.5x the ceiling, and the
            # overhead there is charged per byte stored, so a tarball of the same
            # data carries it too. Both operands are the tool's own measurements,
            # so the split needs no site knowledge and no threshold -- above the
            # ceiling the arithmetic refutes the mechanism, below it nothing
            # changes and the GPFS advice this panel was written for still prints.
            ceiling = res.unit_padding_ceiling
            if ceiling is not None and res.padding > ceiling:
                out.extend(
                    _wrapped(
                        "{} {} {}. Partly filled {} units account for at most {}"
                        " of the {} gap; the remaining {} scales with the data"
                        " rather than with the file count -- replication, erasure"
                        " coding or per-block checksums -- and packing will not"
                        " return it.".format(
                            plural(res.padded_files, "file"),
                            "averages" if res.padded_files == 1 else "average",
                            human_bytes(mean),
                            human_bytes(unit),
                            human_bytes(ceiling),
                            human_bytes(res.padding),
                            human_bytes(res.padding - ceiling),
                        ),
                        style,
                        indent + "  ",
                    )
                )
            else:
                out.extend(
                    _wrapped(
                        "{} {} {}{}, so {} {} of"
                        " padding. Packing them (tar, squashfs, a single archive)"
                        " returns it.".format(
                            plural(res.padded_files, "file"),
                            "averages" if res.padded_files == 1 else "average",
                            human_bytes(mean),
                            against,
                            # The subject and its verb agree together or not at
                            # all. `averages` and `it` were conditional here and
                            # `occupy` was not, so one padded file read "1 file
                            # averages 22.1 MiB ..., so it occupy 29.0 MiB of
                            # padding".
                            "it occupies" if res.padded_files == 1 else "they occupy",
                            human_bytes(res.padding),
                        ),
                        style,
                        indent + "  ",
                    )
                )
    else:
        out.extend(
            _wrapped(
                "{} of data stored in {} {} {}. These files are sparse,"
                " compressed, or small enough to live in their inodes.".format(
                    human_bytes(res.apparent),
                    human_bytes(res.size),
                    ui.dash(style),
                    ratio_x(ratio),
                ),
                style,
                indent,
            )
        )
        if ratio <= _ALLOC_NEARLY_FREE:
            out.extend(
                _wrapped(
                    "Bytes are nearly free at this ratio; {} inodes are the cost to watch.".format(
                        human_count(res.inodes)
                    ),
                    style,
                    indent + "  ",
                )
            )
        else:
            # The strong sentence used to print here too, unconditionally, from
            # 0.87x downwards. At 0.60x -- ordinary lz4 on text -- it read "bytes
            # are nearly free here" about a tree being charged 4.8 GiB, and it was
            # the identical sentence printed at 0.0001x, so it distinguished the
            # two cases not at all and was wrong in one of them. It also named the
            # limit that "will run out first" without consulting either limit;
            # RECONCILE holds the quota rows and answers that question properly.
            out.extend(
                _wrapped(
                    "That is a saving, not an error -- but the quota is still"
                    " charged {}, {:.0f}% of the data size, so bytes remain a"
                    " real cost here.".format(human_bytes(res.size), 100.0 * ratio),
                    style,
                    indent + "  ",
                )
            )
    return out


def _unlanded_bytes(res: WalkResult) -> int:
    """Apparent bytes in recently written files that hold no blocks yet.

    Both halves are measured: ``recent_apparent`` is what those files say they
    contain and ``recent_size`` is what has actually been allocated for them. The
    difference is delayed allocation, which is the one thing that can make this
    tool's headline disagree with the tree by a factor rather than a fraction.
    """
    return max(0, res.recent_apparent - res.recent_size)


def _freed_since_walk_is_material(res: WalkResult, settle: SettleCheck) -> bool:
    """Has enough of the counted total been unlinked to move it by a factor?

    Same shape and same bound as :func:`_headline_is_provisional`, read in the
    other direction: *the headline is wrong by at least a factor* is a different
    statement from *the headline is imprecise*, and only the first is worth
    demoting the figure over. The error here is not even an estimate --
    ``SettleCheck.gone_bytes`` is what the walk itself read for the paths the
    re-stat found unlinked -- so the test is against what survived it. Half the
    total gone means the reader is looking at a number twice the size of the
    tree.

    **Why a factor and not "any deletion at all".** Measured with eight 64 KiB
    files and the re-stat given a believable 60s gap, the total overstates the
    tree by 1.14x with one file unlinked, 2.00x with four and 8.00x with seven.

    **Not identical at eighty, and this comment used to claim it was.** The
    directory's own blocks are part of what survives, and they scale with the
    entry count: measured here, an 8-entry directory adds 0 bytes to
    ``res.size`` and an 80-entry one adds 4096. That decides the *exactly half*
    case and nothing else, because at half the two sides of the comparison below
    are equal to within precisely that overhead::

        4  of 8    gone   262144   remainder   262144   1.0000x   fires
        40 of 80   gone  2621440   remainder  2625536   0.9984x   does NOT fire
        41 of 80   gone  2686976   remainder  2560000   1.0496x   fires
        7  of 8    gone   458752   remainder    65536   7.0000x   fires
        70 of 80   gone  4587520   remainder   659456   6.9565x   fires

    The behaviour is right -- the tree really does still hold those 4096 bytes,
    so eighty files with forty gone is a total overstated 1.998x rather than
    2.000x, and it belongs on the quiet side of a bound that says *at least a
    factor*. What was wrong was the claim of scale-invariance: one file in eight
    is on the far side of the bound and half the files is ON it, so the ratio
    table is exact only for the shape it was measured on. Anything within a
    directory's worth of blocks of the bound is decided by that overhead, which
    is why :func:`test_the_bound_is_exactly_gone_versus_remainder` pins the
    comparison itself rather than a file count on some particular filesystem.

    The 1.14x case is a 64 KiB error,
    and on the tree this tool is pointed at -- eight files written into a
    multi-terabyte scratch directory, one of them rotated away -- it is 64 KiB
    against terabytes; qualifying that figure would put a caveat on every run of
    every tree that rotates anything, which is how a caveat stops being read,
    and ``SettleCheck.gone`` is disclosed there either way. 2.00x and 8.00x are
    the same magnitude as the drift this whole check exists to catch (5.58x up,
    3.3x down, measured -- see :class:`walk.SettleCheck`) and get the same
    treatment.

    So the bound is not invented for the occasion. It is the one the module
    already applies to unlanded bytes -- *an error at least as large as what is
    left over* -- and the ratio table above is what says one deletion in eight
    falls on the other side of it.
    """
    if res.count_only:
        return False  # no blocks were read, so none can be known to be freed
    if not settle.gone_bytes:
        return False
    return settle.gone_bytes >= res.size - settle.gone_bytes


def _headline_is_provisional(res: WalkResult, settle: Optional[SettleCheck] = None) -> bool:
    """Is there more unlanded data than the whole measured total?

    A ratio, not a floor. If the bytes waiting to be allocated equal or exceed
    everything the walk did measure, the headline is going to change by at least
    a factor -- it is not a caveat about precision.

    This is what lets the default view carry the warning without it becoming
    furniture: on a settled tree ``recent_apparent`` and ``recent_size`` agree, so
    the difference is zero and this is silent however many files were touched. It
    fires only on the case it is about.

    **A believable null re-stat outranks it.** This figure is an estimate built
    from two numbers taken at the same instant; ``--settle-wait`` takes a second
    reading later and compares them, which answers the question directly. When
    that check ran long enough to see an effect and saw none, the bytes are not
    going to land and the headline is the answer -- so ``rdu --settle-wait 120``
    on a tree that had not moved in two minutes must not still call its own
    figure provisional. ``SettleCheck.conclusive`` is the codebase's own test for
    "can a null result from this check be believed"; this defers to it rather
    than inventing a second rule.

    **But that null answers a question about the surviving files only.** This is
    the consumer that turns *the sampled files stopped changing* into *the total
    is the answer*, and the re-stat can come back conclusive having lost most of
    the population it set out to measure: seven of eight files unlinked during a
    7s ``--settle-wait`` left one survivor that had not moved, and on that
    strength this returned ``False`` for a 512.0 KiB headline over a 64.0 KiB
    tree -- 8.00x, larger than the drift the check exists to catch, and the
    ``settling`` line said "the figure looks settled" above a table still
    listing the seven deleted files. A null result cannot outrank a *measured*
    error, so the deletion is tested first; see
    :func:`_freed_since_walk_is_material` for why it is a factor and not any
    deletion at all.
    """
    if res.count_only:
        return False  # no sizes were taken, so there is no headline to qualify
    if settle is not None and _freed_since_walk_is_material(res, settle):
        return True
    if settle is not None and settle.conclusive and not settle.moved:
        return False
    unlanded = _unlanded_bytes(res)
    if not unlanded:
        return False
    return unlanded >= res.size if res.size else True


def _provisional_note(res: WalkResult, settle: SettleCheck, style: ui.Style) -> List[str]:
    """The provisional-figure warning, for the view that was not printing it.

    ``render_walk`` states this as one of its facts, so it reached ``-a`` only.
    The default view -- the one the tool documents as the question ``rdu .`` is
    asked -- said nothing at all: four files written a moment earlier on a
    filesystem that had not allocated their blocks yet gave

        1.0 KiB  .  6 inodes  .  0.00s

    for a directory holding 789.2 KiB, and no caveat from either renderer.
    ``render_compact`` admits "an incomplete walk, or drift that was actually
    measured", and that line was drawn in the right place for the wrong reason:
    *how much data has not been allocated yet* is measured too. What is provisional
    is only whether it will land, which is what the sentence says.

    Silent when the re-stat already found drift, because :func:`_hard_warnings`
    reports that -- with a figure, which is strictly better than this estimate.

    **The second branch is about growth, not about landing.** The rule above is a
    ratio between bytes that have not been allocated yet and the total; a tree
    whose blocks land as fast as they are written trips neither it nor
    ``settle.moved``, because the default re-stat gap is 0s and a re-stat taken at
    the same instant as the walk cannot see growth at all. So a tree gaining
    2 MiB/s rendered ``56.0 MiB`` with no qualifier -- byte-identical to a static
    tree of the same shape, which is the one thing a reader would use to tell them
    apart. The check ran, did the work, and could not conclude; that is what gets
    said. ``_settling_is_material`` gates it, so the run where a handful of fresh
    files cannot move the headline stays silent and the line does not become
    furniture.
    """
    if settle.moved:
        return []

    if _freed_since_walk_is_material(res, settle):
        # The default view is the one with no ``SETTLING`` block to fall back on,
        # so before this it was the *quietest* rendering of the worst case:
        # `rdu` on a tree that lost seven of its eight recent files during the
        # re-stat printed "512.0 KiB . 9 inodes . 0.00s", a table of eight
        # entries of which one still existed, and not one word of qualification.
        note = (
            "{} in the last {}, of which {} holding {} vanished between the walk"
            " and the re-stat -- the total above still counts those blocks, so it"
            " reads high by at least that much and stays provisional".format(
                _settle_subject(res),
                human_duration(res.settle_window),
                human_count(settle.gone),
                human_bytes(settle.gone_bytes),
            )
        )
    elif _headline_is_provisional(res, settle):
        note = (
            "{} in the last {}, holding {} unallocated -- more than this whole"
            " total, so the figure above is provisional. Blocks still to land and"
            " a sparse file look identical from one reading; --settle-wait 60"
            " tells them apart".format(
                _settle_subject(res),
                human_duration(res.settle_window),
                human_bytes(_unlanded_bytes(res)),
            )
        )
    elif settle.ran and not settle.conclusive and _settling_is_material(res):
        note = (
            "{} in the last {}, and the two readings were {} apart -- too close"
            " together to tell whether this tree is still growing, so the total"
            " above may be a moving figure (--settle-wait 60 to measure)".format(
                _settle_subject(res),
                human_duration(res.settle_window),
                human_duration(settle.gap),
            )
        )
    else:
        return []
    return _warn_wrapped(note, style)


def _settling_is_material(res: WalkResult) -> bool:
    """Could the unsettled files plausibly move the headline number?

    23 recently written files in a 21,530-inode tree cannot, and saying so at
    length trains the reader to skip the section for the run where it can.
    """
    moving = res.recent_files + res.touched_files
    if not moving:
        return False
    return moving >= max(50, res.inodes // 100) or res.recent_apparent >= (256 << 20)


def _render_silly_renamed(scan: DeletedScan, top: int, style: ui.Style) -> List[str]:
    """The NFS form of this section's subject, when the scan found any.

    Reported here rather than in its own section because it is the same event --
    deleted, still open, still charged -- and a reader looking for held space
    should find both answers in one place. Reported *separately* within it
    because the two differ in the one respect this section's headline claims:
    these are visible to ``du``, under a name that explains nothing. Without it
    the panel said "none found" on every NFS site no matter how much was held,
    which is true and useless.

    The remedy is different too, and worth stating: the entry disappears on its
    own when the last descriptor closes, so there is nothing to delete and
    deleting it by hand does not free the blocks any sooner.
    """
    if not scan.silly_renamed:
        return []
    out = [
        "",
        "  {} {}".format(
            style.paint(human_bytes(scan.silly_renamed_size), "bold_red"),
            style.paint(
                "held by deleted-but-open files on an NFS mount ({})".format(
                    plural(len(scan.silly_renamed), "inode")
                ),
                "bold",
            ),
        ),
    ]
    out.extend(
        _wrapped(
            "NFS renames a file to .nfsXXXX instead of unlinking it, so this space "
            "is charged to your quota and du can see it -- under a name that says "
            "nothing. It is released when the last descriptor closes; deleting the "
            ".nfsXXXX entry does not free it any sooner.",
            style,
            "  ",
        )
    )
    out.append("")
    limit = _limit(top)
    for f in scan.silly_renamed[:limit]:
        holders = ", ".join(
            "{} {}".format(p, ui.printable(c.split()[0]) if c else "?")
            for p, c in f.holders[:_HOLDER_SHOW]
        )
        if len(f.holders) > _HOLDER_SHOW:
            holders += " (+{} more holding it)".format(len(f.holders) - _HOLDER_SHOW)
        out.append(
            "      {}  {}".format(
                style.paint("{:>10}".format(human_bytes(f.size)), "bold_yellow"),
                style.paint("pid {}".format(holders), "cyan"),
            )
        )
        out.append("                  {}".format(ui.printable(f.path)))
    if len(scan.silly_renamed) > limit:
        out.append(
            style.paint("      ... and {} more".format(len(scan.silly_renamed) - limit), "dim")
        )
    return out


def render_deleted(scan: DeletedScan, top: int = 10, style: Optional[ui.Style] = None) -> List[str]:
    """Space with no directory entry. Every other section honours --color; so
    does this one -- and here the headline figure is genuinely an alarm, because
    it is quota being charged for something no walker can show you."""
    style = style or ui.resolve_style("never")
    out = ["", ui.heading("UNLINKED BUT STILL OPEN", style), _section_rule(style)]
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
                "  none found in the {} of {} processes this scan can inspect{}".format(
                    scan.scanned_pids,
                    scan.scanned_pids + scan.unreadable_pids,
                    " -- and that total is this PID namespace only, not the node"
                    if scan.namespaced
                    else "",
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
        if scan.silly_renamed:
            # Otherwise "none found" sits directly above a figure, and the reader
            # has to work out that the two sentences are about different things.
            out.append(
                style.paint(
                    "  -- but see below: on NFS a deleted-but-open file keeps an "
                    "entry, so it is never 'unlinked'.",
                    "dim",
                )
            )
    else:
        out.append(
            "  {} {}".format(
                style.paint(human_bytes(scan.total_size), "bold_red"),
                style.paint(
                    "held by open file descriptors in {}".format(plural(len(scan.files), "inode")),
                    "bold",
                ),
            )
        )
        out.append(style.paint("  (invisible to du, to ls, and to this tool's own walk)", "dim"))
        out.append("")
        limit = _limit(top)
        for f in scan.files[:limit]:
            # A holder's command line is another process's string, read out of
            # /proc, and it lands on the terminal exactly as a filename does.
            holders = ", ".join(
                "{} {}".format(p, ui.printable(c.split()[0]) if c else "?")
                for p, c in f.holders[:_HOLDER_SHOW]
            )
            if len(f.holders) > _HOLDER_SHOW:
                # Killing the three named and not getting the space back is the
                # failure this prevents: the inode is freed when the *last* holder
                # closes it.
                holders += " (+{} more holding it)".format(len(f.holders) - _HOLDER_SHOW)
            out.append(
                "      {}  {}".format(
                    style.paint("{:>10}".format(human_bytes(f.size)), "bold_yellow"),
                    style.paint("pid {}".format(holders), "cyan"),
                )
            )
            out.append("                  {}".format(ui.printable(f.path)))
        if len(scan.files) > limit:
            out.append(style.paint("      ... and {} more".format(len(scan.files) - limit), "dim"))
    out.extend(_render_silly_renamed(scan, top, style))
    scope = [
        "",
        "  scope: {}, {} processes inspected".format(
            "this PID namespace only" if scan.namespaced else "this node only",
            scan.scanned_pids,
        ),
    ]
    if scan.unreadable_pids:
        scope.append(
            "         {} processes belong to other users and cannot be inspected".format(
                scan.unreadable_pids
            )
        )
        scope.append("         without root, so this figure is a floor.")
    # The denominator is whatever /proc showed, and /proc shows only the current
    # PID namespace. Under Apptainer, Docker or a Slurm cgroup with proc
    # remounted, "1 of 1 processes" is a 100%-coverage sentence produced from a
    # namespace holding one process, on a node running fourteen hundred. Honest
    # about EACCES, blind to this -- so the count needs the qualifier, not just
    # the `complete` flag.
    if scan.namespaced:
        scope.append("         /proc shows only this PID namespace (container or cgroup), so")
        scope.append("         that count is not the node's process count and the coverage")
        scope.append("         above cannot be read as node-wide.")
    if scan.timed_out:
        scope.append("         The sweep was abandoned early: " + scan.reason)
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
            # One line, and no repetition of the backend's own error text. With a
            # multi-line `mmlsquota` failure this section printed the same four
            # lines twice -- once for bytes, once for files -- after the QUOTA
            # panel above had already printed them, so the error was three times
            # the length of the report it was attached to. The reason belongs to
            # the panel that reads the backend; this section says what it means
            # for the comparison.
            out.extend(
                _wrapped(
                    "{}  {}".format(label, r.notes[0] if r.notes else "not compared"),
                    style,
                    "  ",
                )
            )
            # Every note, not just the first. This branch was written when
            # `NOT_COMPARED` carried exactly one, and a second reason for not
            # comparing -- "none of what was walked is owned by you, so there is
            # no comparison to make" -- was computed, stored and then dropped on
            # the floor. The whole value of this verdict is the reason for it.
            for extra in r.notes[1:]:
                out.extend(_wrapped(extra, style, "      "))
            continue

        if r.verdict == rc.CLOSES:
            out.extend(
                _verdict_headline(
                    label,
                    "reconciles",
                    "green",
                    "{} vs quota {}, difference {} (within {})".format(
                        show(r.accounted), show(r.quota_value), show(r.gap), show(r.tolerance)
                    ),
                    style,
                )
            )
            for b in r.blockers:
                out.extend(_wrapped("caveat: " + b, style, "      "))
            continue

        if r.verdict == rc.SUBTREE:
            out.append("  {}  {}".format(label, style.paint(rc.verdict_line(r), "dim")))
            # `reconcile` builds a note here that names the fileset, the mount and
            # the scope the figure came from -- "the rcc quota covers /project
            # (group-scoped); this walk covers only /project/dachxiu/x, so the
            # difference is expected" -- and this branch used to `continue` past
            # it. SUBTREE is the most common verdict on a real cluster, so the one
            # line that says *which* quota you are being measured against was the
            # one line never printed, on nearly every run. It is also what makes a
            # tie broken between two filesets on one mount visible at all.
            for n in r.notes:
                out.extend(_wrapped(n, style, "      "))
            # Only ever populated when the subtree exceeds the whole quota
            # figure, which is a real puzzle rather than an expected difference.
            for c in r.candidates:
                out.extend(_wrapped("possible cause (not asserted): " + c, style, "      "))
            continue

        tone = "yellow" if r.verdict == rc.INCONCLUSIVE else "red"
        headline = "INCONCLUSIVE" if r.verdict == rc.INCONCLUSIVE else "UNEXPLAINED GAP"
        out.extend(
            _verdict_headline(
                label,
                headline,
                tone,
                "{} accounted for vs quota {}, difference {}".format(
                    show(r.accounted), show(r.quota_value), show(r.gap)
                ),
                style,
            )
        )
        if r.deleted_value:
            out.append(
                style.paint(
                    "      ({} of that is unlinked-but-open)".format(show(r.deleted_value)), "dim"
                )
            )
        for b in r.blockers:
            # Wrapped, and at the same indent as the candidates and notes below.
            # This was the one `out.append` left in the branch, so the single
            # longest sentence the package writes -- the snapshot-age blocker,
            # which now names the walk's own duration as well as the read
            # staleness, 187 columns rendered -- was the one line that set the
            # width of the whole report on its own. The prefix stays inside the
            # wrapped text rather than becoming the indent, so continuations
            # align under it in the same column as every other prose line here
            # instead of hanging off the end of "cannot call this a finding: ".
            out.extend(_wrapped("cannot call this a finding: " + b, style, "      "))
        for c in r.candidates:
            out.extend(_wrapped("possible cause (not asserted): " + c, style, "      "))
        for n in r.notes:
            out.extend(_wrapped(n, style, "      "))
    return out


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


def _mount_json(
    snap: QuotaSnapshot, res: Optional[WalkResult], path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """``statvfs`` for the path this document is about, when the backend failed.

    See :func:`_mount_fallback` for why this is not merged into `rows`.

    The path is taken from the caller before the walk, because `rdu -Q` has no
    walk: it passes `res=None`, and keying off `res.root` alone meant the terminal
    printed the mount figures there while the document omitted them -- the same
    text-versus-document split this session has closed several times, reintroduced
    by me one round earlier.
    """
    root = path or (res.root if res is not None else None)
    if snap.available or not root:
        return None
    report = quotamod.mount_report(root)
    if report is None:
        return None
    return {
        "path": root,
        "mount": report.mount or None,
        "total_bytes": report.total,
        "used_bytes": report.used,
        "available_bytes": report.avail,
        # A measured zero on almost every filesystem, so it is 0 and not `null`:
        # nothing here went unmeasured.
        "reserved_bytes": report.reserved,
        "fraction": report.fraction,
        "inodes_total": report.inodes_total,
        "inodes_used": report.inodes_used,
        "inodes_available": report.inodes_avail,
        "inodes_reserved": report.inodes_reserved,
        "inodes_fraction": report.inodes_fraction,
        # Stated in the document too, so a consumer cannot read `used/total` as a
        # quota when it may be the whole filesystem.
        "is_a_quota": None,
    }


def to_json(
    res: Optional[WalkResult],
    settle: Optional[SettleCheck],
    snap: Optional[QuotaSnapshot],
    scan: Optional[DeletedScan],
    recs: Optional[List[rc.Reconciliation]],
    top: int = 10,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    # A version on the document, so a consumer can branch on shape instead of
    # probing for keys. Bumped when a key changes meaning or disappears, not when
    # one is added.
    # Schema 2: `by_age[].inodes` became `by_age[].files`. That element was always
    # a *file* count -- non-directory entries only, by design -- so a consumer
    # summing it against the document's `inodes` was short by exactly `dirs`. Renaming is a
    # key changing meaning, which is what the rule above says to bump for; the
    # alternative, emitting both names for one quantity, would have kept the
    # misleading one alive for whoever read it next.
    #
    # Schema 3: `walk.recent_bytes` moved into `settling` as
    # `recent_allocated_bytes`. It held `recent_size` -- *allocated* blocks -- in a
    # document that is careful everywhere else to say `size_bytes` against
    # `apparent_bytes`, so a bare `bytes` on a recent-file figure read as the data
    # size, and a consumer comparing it against `apparent_bytes` was wrong by the
    # allocation ratio. Same class as schema 2, same remedy. It also now sits with
    # the other `recent_*` fields rather than one level up from them.
    #
    # Schema 4: under `-c` every figure derived from `stat` is now `null` rather
    # than `0` (or, for `settled`, `true`). Constraint 10 -- *`None` is not zero* --
    # which the terminal has always obeyed here, printing `n/a` for the headline
    # and omitting the byte column, `BY AGE` and `SETTLING` outright. A consumer
    # could not tell an empty tree from a walk that took no sizes, and a `-c`
    # consumer summing `size_bytes` now meets a `null` where it used to read 0, so
    # the counter moves.
    #
    # Schema 5: `rows[].soft` and `rows[].hard` carry what the backend printed,
    # including a literal `0`. Three of the four quota parsers folded 0 to `None`,
    # so a Lustre row whose limits are `0 0` -- how `lfs` spells "no limit", seen on
    # an ALCF login node -- published `null` and rendered as `n/a`, which claims the
    # figure could not be read when it had been read perfectly. A consumer that
    # tested `soft is None` for "no limit" now sees 0 there, so the counter moves by
    # the same rule that took it to 4: the key means what it always meant, but its
    # value domain changed and a reader has to adapt. Use `rows[].limit` for "the
    # figure usage is measured against" -- `null` for both 0 and unreadable, which
    # is the question most consumers are actually asking.
    #
    # Schema 5 also redefines `mount.fraction` as `used / (used + available)`,
    # which is `df`'s `Use%`, where it was `used / total_bytes`. Blocks reserved
    # for root are in neither term: a default-formatted ext4 with `f_bavail` at
    # zero published 0.95 for a filesystem no non-root writer can add a byte to.
    # `total_bytes` is unchanged and still `df`'s `Size`, so a consumer wanting
    # the old ratio can still divide.
    doc = {"tool": "rapidu", "schema": 5}  # type: Dict[str, Any]
    # `-n 0` means every entry here too. It used to reach the slices raw, so the
    # flag documented as "0 means every entry" published empty rankings and an
    # empty file list -- the JSON consumer got *less* than at the default.
    limit = _limit(top)

    if snap is not None:
        doc["quota"] = {
            "source": snap.source,
            "available": snap.available,
            "reason": snap.reason or None,
            "snapshot_age_seconds": snap.age_seconds,
            "snapshot_taken_at": snap.taken_at,
            # The two caveats a human reader is shown and a machine consumer was
            # not: the timezone-suspicion warning and the reasons some rows could
            # not be tied to a path.
            "time_note": snap.time_note or None,
            "figure_note": snap.figure_note or None,
            "mapping_notes": snap.mapping_notes(),
            # What the mount reports, when no backend could report anything. Same
            # rule as the terminal: present only on the failure path, and named
            # `mount` rather than `quota` because `statvfs` cannot tell an enforced
            # per-user limit from the filesystem's own capacity. A consumer that
            # wants to alarm on headroom can use it; one that wants a quota should
            # look at `rows`, which is empty here for a reason.
            "mount": _mount_json(snap, res, path),
            "rows": [
                {
                    "fileset": r.fileset,
                    # The filesystem the fileset is on, which used to BE the
                    # `fileset` value on every `mmlsquota` row -- so seven
                    # filesets on one device rendered as seven identical labels.
                    "device": r.device,
                    "label": r.label,
                    "kind": r.kind,
                    "scope": r.scope,
                    "used": r.used,
                    "soft": r.soft,
                    # The figure the tool measures usage against: the soft limit
                    # if set, else the hard one, and `null` when neither is. Both
                    # raw values stay above it, because a consumer may want to see
                    # what the backend actually wrote -- but deriving the limit
                    # from them means reimplementing the soft-or-hard rule, and
                    # most backends spell "no limit" as `0`, so the obvious
                    # reading of `soft` alone divides by zero or reports a full
                    # quota as empty.
                    "limit": r.limit,
                    "hard": r.hard,
                    "grace": r.grace or None,
                    "mount": r.mount,
                    "mounts": list(r.mounts),
                    "mount_guessed": r.guessed,
                }
                for r in snap.rows
            ],
        }

    if res is not None:
        doc["walk"] = {
            "root": res.root,
            "size_bytes": _unmeasured(res, res.size),
            "apparent_bytes": _unmeasured(res, res.apparent),
            "allocation": {
                "ratio": res.alloc_ratio,
                "unit_bytes": res.alloc_unit,
                "padding_bytes": _unmeasured(res, res.padding),
                # Published rather than left to the consumer to multiply out,
                # for the reason the grouping helpers exist: the human report
                # decides whether to offer the packing advice by comparing these
                # two, and a `--json` reader deciding it differently is the two
                # views drifting apart.
                "unit_padding_ceiling_bytes": res.unit_padding_ceiling,
                "padded_files": _unmeasured(res, res.padded_files),
                "under_allocated_files": _unmeasured(res, res.under_files),
                "inline_files": _unmeasured(res, res.inline_files),
                # `false` reads as "the allocation gap is not material"; under
                # `-c` the honest answer is that it was never measured.
                "material": _unmeasured(res, allocation_is_material(res)),
            },
            "files": res.files,
            "dirs": res.dirs,
            "inodes": res.inodes,
            "symlinks": res.symlinks,
            "specials": res.specials,
            # Both need `st_nlink`/`st_ino`, so under `-c` they are structurally
            # zero rather than measured: dedup never runs, `seen_links` stays
            # empty. Emitted raw, the document asserted "0 hard-linked files, 0
            # extra names" about a walk that never looked -- on a tree where the
            # full walk reports 1 and 2. That is the exact claim `_unmeasured`
            # exists to stop, and every sibling stat-derived count here
            # (`inline_files`, `recent_files`, `touched_files`, `padded_files`)
            # already goes through it; these two were missed.
            "hardlinked_inodes": _unmeasured(res, res.hardlinked_inodes),
            "hardlink_extra_refs": _unmeasured(res, res.hardlink_extra_refs),
            "elapsed_seconds": round(res.elapsed, 3),
            "threads": res.threads,
            "complete": res.complete,
            "unreadable_dirs": res.unreadable_dir_count,
            # What the two sampling caps in `walk` dropped, so a consumer can see
            # that a figure is bounded rather than inferring it from a list
            # length that happens to be round.
            "unreadable_dir_paths_dropped": res.unreadable_dirs_dropped,
            "watched_dirs_seen": res.watched_seen,
            "watched_dirs_untracked": res.watched_dropped,
            "watched_dirs_tracked": len(res.watched),
            "watched_bytes_over_cap": res.watched_overflow[0],
            "watched_inodes_over_cap": res.watched_overflow[1],
            # How many of `unreadable_dirs` were gone rather than refused. A
            # consumer alerting on permission problems was counting concurrent
            # deletions among them, and the two want different responses.
            "vanished_dirs": res.vanished_dirs,
            "unstatable_entries": res.unstatable,
            # How many of those were gone rather than refused, and where they
            # were. The count alone was unactionable -- the same reason
            # `unreadable_dir_paths` exists.
            "vanished_entries": res.vanished_entries,
            "unstatable_paths": list(res.unstatable_paths[:64]),
            # `filesystems` counts what was visited, which is not the same
            # question: with -x it is always 1, and said nothing about what was
            # refused. These two say what the walk left out and where it is.
            # The flag itself, not only its effect: a bounded walk with nothing to
            # skip reports 0 crossings, so a consumer cannot infer it from those.
            "one_file_system": res.one_file_system,
            "skipped_other_filesystem": res.crossed,
            "skipped_other_filesystem_paths": res.crossed_paths[:64],
            "interrupted": res.partial,
            # *Why* the walk is incomplete, which `interrupted` cannot say: a
            # Ctrl-C and a thread wedged on an unresponsive mount both set it.
            # These threads never returned, so the tallies they had already made
            # were discarded rather than merged -- meaning every figure in this
            # document is lower than what the walk had actually reached. The
            # terminal has always shouted about it ("the figures below are lower
            # than what the walk had reached"); a consumer reading `interrupted:
            # true` had no way to tell an undercount from a clean early exit, and
            # a hung mount is the failure that travels between clusters.
            "abandoned_threads": res.abandoned_workers,
            "by_uid": _owner_json(res.by_uid, _uname, "uid", sized=not res.count_only),
            "by_gid": _owner_json(res.by_gid, _gname, "gid", sized=not res.count_only),
            # Bucketing is by mtime, which `-c` never reads, so every bucket came
            # back `{"bytes": 0, "files": 0}` -- indistinguishable from a tree with
            # nothing in it. `null`, as `top_by_size` already is for the same
            # reason. The terminal omits the section outright.
            "by_age": None
            if res.count_only
            else [
                # `files`, not `inodes`: non-directory entries only -- symlinks
                # among them, since `walk` counts one in `files` like any other --
                # which is what the terminal column says. Note this population
                # already includes whatever `symlinks` reports, so the two must not
                # be added. See `walk.WalkResult.by_age`.
                {"bucket": label, "bytes": b, "files": f}
                for label, (b, f) in zip(walkmod.AGE_BUCKET_LABELS, res.by_age)
            ],
            # Paths, not just the count. A consumer that knows three directories
            # were unreadable cannot act on it; one that knows *which* can.
            "unreadable_dir_paths": [p for p, _why in res.unreadable_dirs[:64]],
            # The human section caps how many commands it prints; this is where the
            # remainder lives, so the cap is bounded rather than silent. Paths are
            # the real bytes, unescaped, as everywhere else in this document.
            "reclaimable": [
                {
                    "pattern": pattern,
                    "command": command,
                    # The terminal prints `n/a` here under `-c`; this printed 0.
                    "bytes": _unmeasured(res, sum(h[0] for h in hits)),
                    "inodes": sum(h[1] for h in hits),
                    "paths": [h[2] for h in hits[:64]],
                }
                for pattern, command, hits in reclaimable_groups(res)
            ],
            "filesystems": len(res.by_dev),
            # `finished_only=res.partial`, exactly as `render_entries` does. One
            # result object must not have two honesty policies: these three keys
            # exist to be ranked on, and on an interrupted walk they published
            # subtrees the text renderer refuses to show -- /usr/lib64 at 17% of
            # its real size, /usr/mpi at 0 bytes -- with "interrupted": true
            # sitting three keys above, where a ranking consumer never looks.
            # A stat-free walk has no bytes, so neither of these rankings exists.
            # `top_dirs` coerces the key to "files" -- right for the terminal,
            # where the column header says so -- but through here it published a
            # files ranking under the name `top_by_size` (byte-for-byte identical
            # to `top_by_inodes`, every row `"bytes": 0`) and a `top_by_density`
            # whose density was null on every row. `main` refuses `-c --sort size`
            # out loud; the document says the same thing the way a document can,
            # with the null the `"schema": 1` contract already defines as "no
            # measurement".
            "top_by_size": (
                None
                if res.count_only
                else [
                    {"path": a.path, "bytes": a.size, "inodes": a.inodes}
                    for a in res.top_dirs(limit, "size", finished_only=res.partial)
                ]
            ),
            "top_by_inodes": [
                # `top_by_size` is already `null` under `-c`; these rows carried
                # `"bytes": 0` regardless, which is the same fabrication one key
                # further in.
                {
                    "path": a.path,
                    "bytes": _unmeasured(res, a.size),
                    "inodes": a.inodes,
                }
                for a in res.top_dirs(limit, "files", finished_only=res.partial)
            ],
            "top_by_density": (
                None
                if res.count_only
                else [
                    {
                        "path": a.path,
                        "bytes": a.size,
                        "inodes": a.inodes,
                        "files_per_gib": files_per_gib(a.size, a.inodes),
                    }
                    for a in res.top_dirs(limit, "density", finished_only=res.partial)
                ]
            ),
        }

    if res is not None and settle is not None:
        # Every count in this section is derived from an mtime, and `-c` reads
        # none. They came back 0 -- "nothing was written recently" -- for a tree
        # whose files were written seconds earlier.
        doc["settling"] = {
            "window_seconds": res.settle_window,
            "recent_files": _unmeasured(res, res.recent_files),
            "touched_files": _unmeasured(res, res.touched_files),
            "future_mtime_files": _unmeasured(res, res.future_files),
            "rechecked": settle.checked,
            "recheck_gap_seconds": settle.gap,
            "drift_bytes": settle.drift,  # signed: GPFS moves both ways
            "moved": settle.moved,
            # null when the check could not have seen drift, rather than a
            # reassuring false.
            # `count_only` first: with no mtime read, `recent_files` is 0 because
            # nothing was measured, not because nothing is recent -- and the old
            # expression turned that into an affirmative `settled: true`. That is
            # the strongest claim in this section, made by an instrument that was
            # switched off.
            # `_freed_since_walk_is_material` is consulted here for the reason the
            # terminal consults it: `settled` is read as "the total stands", and
            # `not settle.moved` answered only "the allocation is not drifting".
            # With seven of eight recent files unlinked, this published
            # `settled: true` beside `vanished_files: 7` for a total 8.00x the
            # size of the tree -- the document's strongest claim, contradicted by
            # the field next to it. No schema bump: `settled` still means what it
            # meant and its value domain is unchanged, this is the same class of
            # correction as the whole-sample-deleted case that took it to `null`.
            "settled": (
                None
                if res.count_only
                else (
                    True
                    if not (res.recent_files or res.touched_files)
                    else (
                        (not settle.moved and not _freed_since_walk_is_material(res, settle))
                        if settle.conclusive
                        else None
                    )
                )
            ),
            "conclusive": settle.conclusive,
            "sampled": settle.sampled,
            # `conclusive: false` conflates two different situations -- no
            # re-stat was asked for, and one ran but was too brief to see the
            # effect. Only the second says anything about the filesystem.
            "recheck_ran": settle.ran,
            # A caveat on `drift_bytes` of exactly the kind `sampled` is: the
            # drift was measured over a population that changed underneath it.
            # The terminal reports it ("N of them disappeared between the walk
            # and the re-stat") and the document did not.
            "vanished_files": settle.gone,
            # The same caveat in the units the headline is in, so a consumer can
            # weigh it instead of counting files: this is what the walk read for
            # those paths, i.e. the amount by which `walk.size_bytes` above is
            # already known to be high. A count cannot carry that -- one file of
            # eight is 64 KiB or a terabyte -- and it is what decides `settled`.
            "vanished_allocated_bytes": settle.gone_bytes,
            # The limit case of the line above, and the reason `settled` is null
            # rather than true when it fires: every sampled file was deleted, so
            # `drift_bytes: 0` is an absent reading and not a settled tree.
            # Derived, like `moved` / `sampled` / `conclusive`, and published for
            # the same reason -- a consumer should not have to know that
            # `rechecked == 0 and vanished_files > 0` means the check was blind.
            "recheck_measured_nothing": settle.recheck_measured_nothing,
            # Both halves, unambiguously named, so the subtraction below is
            # checkable and a consumer can do its own arithmetic on either.
            "recent_allocated_bytes": _unmeasured(res, res.recent_size),
            "recent_apparent_bytes": _unmeasured(res, res.recent_apparent),
            # The derived figure and the verdict built from it. `settled: null`
            # above says the headline is unknown; these say *how* unknown, which
            # is the number both terminal views now print and the one this
            # document withheld -- `recent_apparent` was absent, so the difference
            # could not be recomputed from anything published. A machine consumer
            # has to be able to reach the reader's conclusion, not a weaker one.
            "unlanded_bytes": _unmeasured(res, _unlanded_bytes(res)),
            # Passed the check, so this agrees with `settled` above rather than
            # contradicting it: a conclusive null re-stat means the figure stands.
            "headline_provisional": _unmeasured(res, _headline_is_provisional(res, settle)),
        }

    if scan is not None:
        doc["deleted_but_open"] = {
            "available": scan.available,
            "reason": scan.reason or None,
            "total_bytes": scan.total_size,
            "inodes": len(scan.files),
            # The NFS form of the same event, kept in its own pair of fields for
            # the reason `deleted._SILLY_RENAME_RE` gives: `total_bytes` is
            # documented as space no walk can see, and these bytes are visible.
            # A consumer adding them together would be summing two different
            # claims; one that wants "space held by a deleted file" can add them
            # itself, knowing which is which.
            "nfs_silly_renamed_bytes": scan.silly_renamed_size,
            "nfs_silly_renamed_inodes": len(scan.silly_renamed),
            "scanned_pids": scan.scanned_pids,
            "unreadable_pids": scan.unreadable_pids,
            "complete": scan.complete,
            "node_local_only": True,
            # A consumer computing coverage from scanned_pids needs to know the
            # denominator is namespace-scoped, and that the sweep may have been
            # cut short -- neither is inferable from the counts alone.
            "pid_namespaced": scan.namespaced,
            "timed_out": scan.timed_out,
            "files": [
                {
                    "path": f.path,
                    "bytes": f.size,
                    "pids": f.pids,
                    "holders": [c for _, c in f.holders],
                }
                for f in scan.files[:limit]
            ],
            "nfs_silly_renamed": [
                {
                    "path": f.path,
                    "bytes": f.size,
                    "pids": f.pids,
                    "holders": [c for _, c in f.holders],
                }
                for f in scan.silly_renamed[:limit]
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
