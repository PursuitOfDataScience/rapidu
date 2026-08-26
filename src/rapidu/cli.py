"""Command line interface.

    rdu .                 quota + walk + reconcile + where the inodes are
    rdu ~/scratch         the same, for another tree
    rdu --quota-only      just the quota table, with its snapshot age
    rdu --deleted-only    just the unlinked-but-open scan

**The only positional argument is a path.** An earlier version took bare-word
subcommands (``rdu quota``, ``rdu walk``, ``rdu deleted``), which is the wrong shape
for a tool whose primary argument is a directory: ``quota``, ``walk`` and
``deleted`` are all perfectly ordinary directory names, so ``rdu deleted`` was
ambiguous between "scan for deleted files" and "measure ./deleted" -- and it
silently resolved to the former. Modes are flags now, and a path is always a
path.

Plain text by default because the output goes in a support ticket; ``--json``
for tooling and for ``--support-bundle`` style composition.
"""

import argparse
import contextlib
import json
import os
import sys
import threading
from typing import Dict, List, Optional, Tuple  # noqa: F401  (`# type:` use)

from . import deleted as deletedmod
from . import quota as quotamod
from . import reconcile as rc
from . import report, ui
from . import walk as walkmod

EXIT_OK = 0
# Something a human should look at: an incomplete walk, an unsettled tree, an
# unexplained gap. Lets a script branch without parsing prose.
EXIT_ATTENTION = 1
EXIT_ERROR = 2

# Words the removed subcommand interface used. Kept only to recognise them when
# they fail to resolve as a path, so the error can point at the flag.
_LEGACY_COMMANDS = {
    "quota": "--quota-only",
    "walk": "--no-quota",
    "deleted": "--deleted-only",
}

_COLOR_MODES = ("auto", "always", "never")


class _Parser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that colours its own ``--help``.

    The colour goes on after argparse has finished laying the text out, not
    before -- see :func:`rapidu.ui.colorize_help` for why. ``style`` is set by
    :func:`main` before parsing, because ``-h`` is handled *during* parsing and
    so there is no parsed ``--color`` to consult by the time help is printed.
    """

    style = None  # type: Optional[ui.Style]

    def format_help(self) -> str:
        return ui.colorize_help(super().format_help(), self.style or ui.resolve_style("auto"))


def _peek_color(argv: List[str]) -> str:
    """``--color`` as written on the command line, before argparse sees it."""
    for i, arg in enumerate(argv):
        if arg.startswith("--color="):
            mode = arg.split("=", 1)[1]
        elif arg == "--color" and i + 1 < len(argv):
            mode = argv[i + 1]
        else:
            continue
        if mode in _COLOR_MODES:
            return mode
    return "auto"


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="rapidu",
        usage="rdu [PATH ...] [options]",
        description="Where your bytes and inodes are, what du cannot see, and "
        "how old the quota number you are comparing against is.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
        "  rdu                    how big is this tree, and what is big inside it\n"
        "  rdu ~/scratch -n 20    another tree, listing 20 directories\n"
        "  rdu -i                 rank by inode count instead of bytes\n"
        "  rdu -a                 the full report: quota, /proc scan, reconciliation\n"
        "  rdu -Q                 just the quota table and the age of its figures\n"
        "  rdu -D                 unlinked-but-open space held on this node\n"
        "  rdu -a --settle-wait 60     measure how far a fresh tree is still drifting\n"
        "\n"
        "rapidu agrees with `du -s --block-size=1` byte-for-byte on the\n"
        "same tree. It is faster, not more accurate.",
    )
    p.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="directories to walk (default: the current directory)",
    )
    # The shape is stated because it *varies with argc*: one path yields the
    # document, several yield a list of them. A script written against one path
    # (`rdu --json /project/me | jq .walk.size_bytes`) returns null the day
    # someone adds a second, silently. Changing it to always emit a list would fix
    # the contract and break every existing single-path consumer for a stylistic
    # gain, so it is documented rather than changed -- but it is documented, which
    # it was not.
    p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="machine-readable output: one document per PATH, or a list of them "
        "when several are given",
    )
    p.add_argument(
        "-t",
        "--threads",
        type=_positive_int,
        default=None,
        metavar="N",
        help="walk concurrency, clamped to {}. Chosen from the filesystem when "
        "not given, because the right number is a property of the storage and "
        "nothing else: {} where stats are local and already cheap, {} otherwise. "
        "Threads are there to hide latency, so on page-cached local storage they "
        "cost 3.4x the wall time and 5.2x the CPU, while on GPFS they save a fifth "
        "of the wall time for 30%% more CPU. Give a number to decide it "
        "yourself.".format(walkmod.MAX_THREADS, walkmod.LOCAL_THREADS, walkmod.DEFAULT_THREADS),
    )
    p.add_argument(
        "-d",
        "--depth",
        type=int,
        default=1,
        metavar="N",
        help="how deep to break the tree down. 1 lists the immediate children, "
        "like `du -d1` (default: %(default)s)",
    )
    p.add_argument(
        "-n",
        "--top",
        type=int,
        default=10,
        metavar="N",
        help="how many directories to list per ranking; 0 lists every entry (default: %(default)s)",
    )
    p.add_argument(
        "--max-dirs-per-sec",
        type=float,
        default=0.0,
        metavar="N",
        help="token-bucket rate limit on directory opens; 0 disables (default: %(default)s)",
    )
    p.add_argument(
        "--settle-window",
        type=float,
        default=walkmod.DEFAULT_SETTLE_WINDOW_S,
        metavar="SECONDS",
        help="treat files modified within this window as possibly unsettled (default: %(default)s)",
    )
    p.add_argument(
        "-x",
        "--one-file-system",
        action="store_true",
        help="do not cross filesystem boundaries; use this when "
        "reconciling against a per-filesystem quota",
    )
    p.add_argument(
        "-a",
        "--full",
        action="store_true",
        help="the whole diagnostic report: quota with its snapshot age, the "
        "unlinked-but-open scan, and the reconciliation between them. Off by "
        "default because `rdu .` is asked how big a tree is, not for an audit.",
    )
    p.add_argument(
        "-c",
        "--count",
        action="store_true",
        help="count files only, skipping every stat. Measured {:.0f}x faster on "
        "1.7M GPFS files (58.7s against 7.1s), because stat is ~90%% of a normal "
        "walk. No sizes, and hard links count once per name.".format(walkmod.COUNT_SPEEDUP),
    )
    p.add_argument(
        "-i",
        "--inodes",
        action="store_true",
        # `inodes`, not "file count": this ranks the column headed `inodes`, which
        # counts directories too. The package's own rule since RD-9 is `inodes`
        # everywhere it counts inodes and `files` only where it means regular
        # files; the flag that exists *for* the inode question was the last place
        # still saying the other word.
        help="rank by inode count instead of bytes -- what an inode quota limits. "
        "Add -c to answer it without stat -- ~{:.0f}x faster on GPFS, less on a "
        "page-cached local filesystem -- at the cost of counting a hard-linked "
        "file once per name rather than once per inode.".format(walkmod.COUNT_SPEEDUP),
    )
    p.add_argument(
        "-Q",
        "--quota-only",
        action="store_true",
        help="print only the quota table and the age of its figures; walk nothing. "
        "A PATH, if given, selects which rows to show.",
    )
    p.add_argument(
        "-D",
        "--deleted-only",
        action="store_true",
        help="print only unlinked-but-open space held on this node. A PATH, if "
        "given, restricts the scan to that subtree.",
    )
    p.add_argument(
        "--sort",
        choices=("size", "files", "density"),
        default=None,
        metavar="KEY",
        help="rank by size (default), files (the inode count, as the column is "
        "headed), or density -- files per GiB. "
        "Density is the 'what should I pack' signal: a subtree with a million "
        "small files costs inodes and allocation padding out of proportion to its "
        "bytes. It adds a files/GiB column, and it skips subtrees too small to be "
        "worth packing -- files per GiB is won on the denominator, so a directory "
        "holding three files would otherwise top the table. Needs sizes, so it "
        "cannot be combined with -c.",
    )
    p.add_argument(
        "--no-box",
        action="store_true",
        help="do not draw the frame around the report. The frame is one block to "
        "select and paste into a ticket; turn it off when piping into grep, awk "
        "or a diff, where the borders are noise.",
    )
    p.add_argument("--no-quota", action="store_true", help="skip the quota backend")
    p.add_argument(
        "--no-deleted", action="store_true", help="skip the /proc scan for unlinked-but-open files"
    )
    p.add_argument(
        "--settle-wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="wait this long, then re-stat recently written files to "
        "measure how far the tree is still drifting. Blocks move "
        "over tens of seconds on GPFS, so an immediate re-stat "
        "(the default) can only warn, not measure. Try 60.",
    )
    p.add_argument(
        "--no-settle-check",
        action="store_true",
        help="skip the re-stat pass that detects an unsettled tree",
    )
    p.add_argument(
        "--quota-timeout",
        type=float,
        default=quotamod.DEFAULT_TIMEOUT_S,
        metavar="SECONDS",
        help="total budget for reading quotas, across every backend tried "
        "(default: %(default)s). Backends run in sequence, so this bounds the "
        "whole quota step -- not each subprocess within it.",
    )
    p.add_argument(
        "--max-snapshot-age",
        type=float,
        default=rc.DEFAULT_MAX_SNAPSHOT_AGE_S,
        metavar="SECONDS",
        help="a quota snapshot older than this cannot support a finding (default: %(default)s)",
    )
    p.add_argument(
        "--no-progress",
        action="store_true",
        help="do not draw the progress spinner while walking (it is on stderr, "
        "and already suppressed when stderr is not a terminal)",
    )
    p.add_argument(
        "--color",
        choices=_COLOR_MODES,
        default="auto",
        metavar="WHEN",
        help="auto | always | never. auto colourises only when stdout is a "
        "terminal, and never when NO_COLOR is set (default: %(default)s)",
    )
    p.add_argument(
        "--ascii", action="store_true", help="draw bars with ASCII instead of block glyphs"
    )
    p.add_argument("-V", "--version", action="store_true", help="print version and exit")
    return p


def _default_path() -> Optional[str]:
    """The current directory, or ``None`` if it no longer exists.

    ``os.getcwd()`` **raises** when the directory the process sits in has been
    removed, and both entry points called it unguarded as the default path. On a
    cluster that is routine rather than exotic -- a scratch directory reclaimed by
    a cleanup policy, or removed by another job, while a shell sits in it -- and
    rapidu answered with an unhandled ``FileNotFoundError`` traceback before doing
    any work. That is the failure mode this campaign filed against two of the
    sibling packages; rapidu had a third variant of it.

    Returning ``None`` lets the caller say what happened and what to do about it,
    which is more useful than a traceback and is the whole difference between the
    two.
    """
    try:
        return os.getcwd()
    except OSError:
        return None


def _expand(path: str) -> Tuple[str, str]:
    """``path`` with ``~`` resolved, or the reason it could not be.

    ``os.path.expanduser`` **does not raise** when it cannot resolve a tilde: it
    returns the string unchanged. With no ``$HOME`` and no passwd entry -- the
    ordinary state of a compute node under ``sbatch --export=NONE`` at some sites
    -- ``~/scratch`` comes back as the literal ``~/scratch``, which then reads as
    a relative directory named ``~`` in the job's working directory.

    Refusing it is right, and rapidu already did. Calling it "no such path" was
    not: the path is not missing, the tilde is unexpanded, and the reader goes
    looking for a directory that was never named. Same mistake as reporting a
    broken `quota` wrapper as "not on PATH" -- a real failure with the wrong cause
    attached. So the *result* is guarded rather than only the exception.

    ``~someone`` for a user who does not exist lands here too, and gets its own
    reason, because "no such user" and "no ``$HOME``" send the reader to different
    places.
    """
    expanded = os.path.expanduser(path)
    if not expanded.startswith("~"):
        return expanded, ""
    who = expanded.split(os.sep)[0][1:]
    if who:
        return expanded, "no such user `{}`".format(who)
    return expanded, "$HOME is unset and this user has no passwd entry"


def _resolve_paths(raw: List[str]) -> Tuple[List[str], int]:
    """The measurable paths among ``raw``, and how many were refused.

    The count is returned rather than left on stderr because the exit code has to
    know. `rdu /project/a /project/b` where `b` has been deleted or unmounted
    walked `a`, said "no such path" about `b` on stderr, and exited **0** -- so a
    cron job asking about two trees was told everything was fine having measured
    one. The all-rejected case already returns `EXIT_ERROR` for precisely this
    reason ("a script saw 'success, nothing held'"), and there is no reason for
    the partial case to disagree with it.
    """
    out = []  # type: List[str]
    refused = 0
    requested = list(raw)
    if not requested:
        here = _default_path()
        if here is None:
            sys.stderr.write(
                "rapidu: the current directory no longer exists, so there is no "
                "default path -- name one explicitly\n"
            )
            return [], 1
        requested = [here]
    for p in requested:
        expanded, why = _expand(p)
        if why:
            sys.stderr.write(
                ui.encode_safe(
                    "rapidu: {}: cannot expand `~` -- {}\n".format(ui.printable(p), why),
                    sys.stderr,
                )
            )
            refused += 1
            continue
        ap = os.path.abspath(expanded)
        if not os.path.exists(ap):
            # The argument is echoed back, so it is escaped: a path is user data
            # on stderr exactly as it is in the report.
            sys.stderr.write(
                ui.encode_safe("rapidu: {}: no such path\n".format(ui.printable(p)), sys.stderr)
            )
            # Only when it is not a real directory: if ./quota exists it is a
            # path, unambiguously, and gets measured like any other.
            refused += 1
            if p in _LEGACY_COMMANDS:
                sys.stderr.write(
                    "rapidu: `{0}` is no longer a subcommand -- the only "
                    "positional argument is a path. Did you mean `rdu {1}`?\n".format(
                        ui.printable(p), _LEGACY_COMMANDS[p]
                    )
                )
            continue
        if not os.path.isdir(ap):
            sys.stderr.write(
                ui.encode_safe("rapidu: {}: not a directory\n".format(ui.printable(p)), sys.stderr)
            )
            refused += 1
            continue
        # A symlink to a directory has to be resolved *here*, because the two
        # halves of this tool disagreed about what the argument was: this
        # function admitted it (`isdir` follows symlinks) and `walk` then
        # rejected it (`os.lstat` does not), so `rdu ~/scratch` failed outright
        # with "is not a directory" -- the README's own second example, on the
        # standard layout where $SCRATCH is symlinked into $HOME. `du` offers two
        # readings, `linktest` (the link) and `linktest/` (the target), but
        # `os.path.abspath` has already stripped the trailing slash by now, so
        # that distinction is not recoverable and never was. Measuring the target
        # is the only reading that answers the question anyone types this to ask.
        if os.path.islink(ap):
            ap = os.path.realpath(ap)
        out.append(ap)
    return out, refused


def _positive_int(raw: str) -> int:
    """A thread count, rejected here rather than silently repaired later.

    The upper bound is *clamped* and says so in ``--help``, which sets the
    expectation that this option is validated -- and then ``-t 0`` and ``-t -5``
    were quietly raised to 1. Zero threads is not a request anyone can mean and a
    negative one is a typo (``-t -5`` is what you get from ``-t`` followed by a
    stray flag), so argparse rejects them with the value named. Rejecting is the
    honest direction: a clamp is a decision the tool can defend, a negative count
    is not a decision at all.
    """
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("{!r} is not a whole number".format(raw)) from None
    if value < 1:
        raise argparse.ArgumentTypeError(
            "{} threads is not a walk; give 1 or more (the cap is {})".format(
                value, walkmod.MAX_THREADS
            )
        )
    return value


def _warn_threads(requested: Optional[int]) -> None:
    # `None` is "you choose", which is not a request and has nothing to warn
    # about. Without this the comparison below raises a TypeError on 3.x, which
    # is the whole of what makes the adaptive default reachable from the CLI.
    if requested is None:
        return
    if requested > walkmod.MAX_THREADS:
        sys.stderr.write(
            "rapidu: --threads {} clamped to {}: past the cap the walk is slower "
            "(measured on GPFS: 48 threads was 6% worse than 24) and the metadata "
            "load stops being polite.\n".format(requested, walkmod.MAX_THREADS)
        )
    elif requested < 1:
        # Unreachable from the command line -- `_positive_int` rejects it there --
        # and kept for a caller that builds a Namespace itself, where the walk
        # still clamps to 1 and should say so.
        sys.stderr.write("rapidu: --threads {} raised to 1\n".format(requested))


# A quota this full is the finding, whether or not a walk was asked for.
QUOTA_ATTENTION_FRACTION = 0.90


def _quota_needs_attention(
    snap: "quotamod.QuotaSnapshot", paths: Optional[List[str]] = None
) -> bool:
    """Is any relevant quota row at/over the line, or running a grace timer?

    ``rdu -Q`` used to exit 0 with a fileset at 99.9% of blocks and 92.8% of
    files, and would have exited 0 with a grace timer already counting down --
    ``EXIT_ATTENTION`` fired only when the backend was *unavailable*. So the one
    invocation designed to be cheap enough to run from cron reported "fine" in
    the two states that mean "writes are about to stop".
    """
    rows = []  # type: List[quotamod.QuotaRow]
    for p in paths or []:
        rows.extend(snap.rows_for_path(p))
    if not rows:
        rows = list(snap.rows)
    for r in rows:
        if r.grace:
            return True
        frac = r.usage_fraction
        if frac is not None and frac >= QUOTA_ATTENTION_FRACTION:
            return True
    return False


def _framed(lines: List[str], style: ui.Style, boxed: bool) -> str:
    """One report, optionally inside a single frame.

    The frame derives its own width from the terminal, not from ``style.width`` --
    which ``_box_style`` has already reduced by the chrome for the renderers' sake.

    Every line passes :func:`ui.sanitize_line` on the way out. The renderers
    already escape the names they lay out -- that is where the width arithmetic
    happens, so it has to be there -- and this is the floor under them: one place
    that guarantees no control character reaches the terminal, whichever renderer
    produced the line and whether or not it is framed. It runs *before* the box,
    because a border can only be padded to a width that is already true.
    """
    # Two different failures with the same consequence, and both have to happen
    # *before* the box: `sanitize_line` handles characters the terminal would act
    # on, `encode_safe` characters the stream cannot represent. An escape is wider
    # than what it replaces, so padding computed before either one is padding
    # computed against the wrong width -- which is the RD-6 mistake, and it puts
    # the right-hand border in the wrong column.
    lines = [ui.encode_safe(ui.sanitize_line(line)) for line in lines]
    if boxed:
        lines = ui.box(lines, style)
    return "\n".join(lines)


def _box_style(color: str, ascii_only: bool, boxed: bool) -> "ui.Style":
    """A style whose width already accounts for the frame it will sit in.

    The renderers lay their columns out against ``style.width``; handing them the
    full terminal and then adding two borders would push the widest row past the
    edge and wrap it, which is worse than no frame at all. Reserve the chrome
    first, so every column decision downstream is made against the space that
    actually exists.
    """
    style = ui.resolve_style(color, ascii_only)
    if boxed:
        style.width = max(40, style.width - ui.BOX_CHROME)
    return style


def cmd_quota(args: argparse.Namespace) -> int:
    paths = []  # type: List[str]
    for raw in args.paths:
        expanded, why = _expand(raw)
        if why:
            # The quota reader maps rows to a path, so an unexpanded `~` here
            # would silently ask about a relative directory named `~`.
            sys.stderr.write("rapidu: {}: cannot expand `~` -- {}\n".format(ui.printable(raw), why))
            return EXIT_ERROR
        paths.append(os.path.abspath(expanded))
    if not paths:
        here = _default_path()
        if here is None:
            sys.stderr.write(
                "rapidu: the current directory no longer exists, so there is no "
                "path to look a quota up for -- name one explicitly\n"
            )
            return EXIT_ERROR
        paths = [here]
    snap = quotamod.read_best(paths[0], args.quota_timeout)
    if args.as_json:
        # `paths[0]` explicitly: there is no walk in `-Q` mode, so the document
        # has no other way to know which filesystem it is describing.
        print(json.dumps(report.to_json(None, None, snap, None, None, path=paths[0]), indent=2))
    else:
        boxed = not args.no_box
        style = _box_style(args.color, args.ascii, boxed)
        print(_framed(report.render_quota(snap, paths or None, style), style, boxed))
    if not snap.available:
        return EXIT_ATTENTION
    return EXIT_ATTENTION if _quota_needs_attention(snap, paths) else EXIT_OK


def cmd_deleted(args: argparse.Namespace) -> int:
    # `None` means "the whole node", which is what -D with no path asks for. The
    # annotation is a comment because the ternary this replaced inferred
    # `List[str]` from its first branch and then rejected the `None` in its second
    # -- an error under mypy 1.x and not under 2.x, so the type of this line
    # depended on which supported checker happened to be installed.
    targets = [None]  # type: List[Optional[str]]
    refused = 0
    if args.paths:
        resolved, refused = _resolve_paths(args.paths)
        targets = list(resolved)
        if not targets:
            # Every path was rejected, and `_resolve_paths` has already said why on
            # stderr. Falling through printed no report at all and still exited 0,
            # so a script saw "success, nothing held" -- `cmd_walk` returns
            # EXIT_ERROR for exactly this and there is no reason for the two to
            # disagree.
            return EXIT_ERROR
    rcode = EXIT_OK
    for target in targets:
        scan = deletedmod.scan(target)
        if args.as_json:
            print(json.dumps(report.to_json(None, None, None, scan, None, args.top), indent=2))
        else:
            boxed = not args.no_box
            style = _box_style(args.color, args.ascii, boxed)
            print(_framed(report.render_deleted(scan, args.top, style), style, boxed))
        if scan.files:
            rcode = EXIT_ATTENTION
    if refused:
        # Some of what the caller named could not be scanned. Reporting on the
        # rest is right; reporting success for it is not.
        return EXIT_ERROR
    return rcode


def _walk_with_progress(
    path: str, args: argparse.Namespace, style: ui.Style
) -> "walkmod.WalkResult":
    """Run the walk, painting a spinner on stderr while it is in flight.

    The results are printed only when the walk finishes, and that is deliberate.
    A ranking cannot be streamed: you do not know which directory is largest
    until you have seen them all, so emitting rows as they arrive would show an
    order that keeps changing and is wrong until the last moment. Progress is
    the honest thing to stream; conclusions are not.
    """
    # Resolved once, here: the `Progress` object is sized by it and the walk must
    # be given the same number, so choosing twice could disagree.
    nthreads = walkmod.choose_threads(path, args.threads)
    spinner = ui.Spinner(style)
    if args.no_progress or not spinner.enabled:
        return walkmod.walk(
            path,
            threads=nthreads,
            depth=args.depth,
            max_dirs_per_sec=args.max_dirs_per_sec,
            settle_window=args.settle_window,
            one_file_system=args.one_file_system,
            count_only=args.count,
        )

    progress = walkmod.Progress(nthreads)
    stop = threading.Event()

    def paint() -> None:
        # Nothing is drawn for a fast walk: below the delay the spinner would be
        # a flicker between the prompt and the output.
        if stop.wait(ui.PROGRESS_DELAY_S):
            return
        while not stop.is_set():
            spinner.paint(
                ui.progress_text(
                    ui.truncate(progress.current or path, max(20, style.width // 3)),
                    progress.inodes,
                    progress.dirs,
                    progress.rate,
                    progress.elapsed,
                )
            )
            stop.wait(ui.PROGRESS_INTERVAL_S)

    painter = threading.Thread(target=paint, name="rapidu-progress", daemon=True)
    painter.start()
    try:
        return walkmod.walk(
            path,
            threads=nthreads,
            depth=args.depth,
            max_dirs_per_sec=args.max_dirs_per_sec,
            settle_window=args.settle_window,
            one_file_system=args.one_file_system,
            progress=progress,
            count_only=args.count,
        )
    finally:
        stop.set()
        painter.join(timeout=1.0)
        spinner.clear()


def cmd_walk(args: argparse.Namespace) -> int:
    paths, refused = _resolve_paths(args.paths)
    if not paths:
        return EXIT_ERROR
    _warn_threads(args.threads)

    # --json is for tooling, which wants the complete document rather than
    # whichever subset the terminal view happens to show.
    full = args.full or args.as_json
    if args.count:
        # Nothing downstream of a stat-free walk has bytes to work with.
        args.no_settle_check = True
    # --json is a document, not a display: it is never framed.
    boxed = not args.no_box and not args.as_json
    style = _box_style(args.color, args.ascii, boxed)

    # Both of these are work the default view does not use: the quota backend
    # shells out to a site wrapper that can take seconds, and the /proc sweep
    # walks every pid on the node.
    #
    # The quota read is per path, and memoised per path, because `read_best`
    # explicitly chooses *the backend that can map the path it was given*. Reading
    # once for paths[0] and reusing it meant `rdu -a ~ /scratch/lustre` picked a
    # backend for $HOME and then reconciled the Lustre path against it -- which is
    # precisely the multi-filesystem failure the first-success-wins fix set out to
    # remove, reintroduced one layer up. Memoising keeps the common case (every
    # path on one filesystem, or a site-wide wrapper) at exactly one subprocess.
    snaps = {}  # type: Dict[str, Optional[quotamod.QuotaSnapshot]]

    def quota_for(path: str) -> "Optional[quotamod.QuotaSnapshot]":
        if args.no_quota or not full:
            return None
        if path not in snaps:
            fresh = quotamod.read_best(path, args.quota_timeout)
            # A snapshot that maps this path is specific to it; one that maps
            # nothing is a site-wide answer and can be shared with every path.
            snaps[path] = fresh
            if not fresh.rows_for_path(path):
                for other in paths:
                    snaps.setdefault(other, fresh)
        return snaps[path]

    scan = None
    if full and not args.no_deleted:
        scan = deletedmod.scan()

    docs = []
    rcode = EXIT_OK
    # Counted rather than assigned straight into `rcode`, for the reason given at
    # the `OSError` handler below.
    unmeasured = 0
    for path in paths:
        snap = quota_for(path)
        try:
            res = _walk_with_progress(path, args, style)
        except OSError as exc:
            # An OSError's string carries the path that failed.
            sys.stderr.write("rapidu: {}\n".format(ui.printable(str(exc))))
            # Not `rcode = EXIT_ERROR`. A later path reaching any of the
            # EXIT_ATTENTION lines below overwrote it, so the exit code depended
            # on the order of the arguments: `rdu -a vanished full` returned 1 and
            # `rdu -a full vanished` returned 2 for the same two paths in the same
            # two states. EXIT_ERROR outranks EXIT_ATTENTION -- the suite asserts
            # that for `refused` already -- so it is counted like `refused` and
            # applied after the loop, where nothing can lower it.
            unmeasured += 1
            continue

        settle = (
            walkmod.SettleCheck()
            if args.no_settle_check
            else walkmod.recheck_settling(res, args.settle_wait)
        )

        if scan is not None:
            path_scan = scan.under(path)
        else:
            path_scan = deletedmod.DeletedScan()
            path_scan.available = False
            path_scan.reason = "skipped (--no-deleted)"

        recs = []  # type: List[rc.Reconciliation]
        if snap is not None:
            for kind in ("blocks", "files"):
                recs.append(rc.reconcile(res, settle, snap, path_scan, kind, args.max_snapshot_age))

        if args.as_json:
            docs.append(report.to_json(res, settle, snap, path_scan, recs, args.top))
        elif not full:
            print(
                _framed(
                    report.render_compact(
                        res, settle, args.top, args.inodes or args.count, style, sort=args.sort
                    ),
                    style,
                    boxed,
                )
            )
        else:
            lines = []  # type: List[str]
            if snap is not None:
                lines.extend(report.render_quota(snap, [path], style))
            lines.extend(
                report.render_walk(
                    res,
                    settle,
                    args.top,
                    scan=path_scan,
                    style=style,
                    by_inodes=args.inodes,
                    sort=args.sort,
                )
            )
            if path_scan.available and path_scan.files:
                lines.extend(report.render_deleted(path_scan, args.top, style))
            if recs:
                lines.extend(report.render_reconcile(recs, style))
            # One frame around the whole report -- quota, walk, /proc scan and
            # reconciliation together -- rather than one per section. The sections
            # are one answer to one question and they are read as a unit.
            print(_framed(lines, style, boxed))

        if not res.complete or settle.moved:
            rcode = EXIT_ATTENTION
        # INCONCLUSIVE was excluded, and on any GPFS-native or Lustre site it is
        # the *only* verdict the vendor backends could produce, so the exit code
        # was constant there and a cron job could not use it. It means "there is a
        # difference I cannot rule out", which is precisely what a caller checking
        # an exit code wants to be told.
        if any(r.verdict in (rc.GAP, rc.INCONCLUSIVE) for r in recs):
            rcode = EXIT_ATTENTION
        if snap is not None and _quota_needs_attention(snap, [path]):
            rcode = EXIT_ATTENTION
        # `cmd_deleted` returns EXIT_ATTENTION for exactly this and the walk did
        # not, so the fuller invocation was the quieter one: 103.8 MiB held by an
        # unlinked-but-open descriptor printed the same panel under `-D` and `-a`
        # and exited 1 and 0 respectively. Same class as `_quota_needs_attention`
        # above -- the command documented as the full audit, and the one a cron job
        # would run, reported success in a state that wants a human. Reconciliation
        # calling it CLOSES does not make the bytes go away; it only explains them.
        if path_scan.files:
            rcode = EXIT_ATTENTION

    if args.as_json and docs:
        print(json.dumps(docs[0] if len(docs) == 1 else docs, indent=2))
    if refused or unmeasured:
        # See `_resolve_paths`: a named path that could not be measured is not a
        # successful run, however well the others went. A path that resolved and
        # then failed mid-walk -- purged, unmounted, or cleaned up under us -- is
        # the same thing one step later.
        return EXIT_ERROR
    return rcode


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    parser.style = ui.resolve_style(_peek_color(argv), "--ascii" in argv)  # type: ignore[attr-defined]
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__

        print("rapidu {}".format(__version__))
        return EXIT_OK

    if args.quota_only and args.deleted_only:
        parser.error("--quota-only and --deleted-only ask for different reports")

    # Four values were accepted and silently did something other than what they
    # look like. Each one disables a feature rather than adjusting it, and each
    # did so without a word: `-d 0` printed "0 entries" and exited 0, as though
    # the tree were empty; `--settle-window -5` pushed the cutoff into the future
    # and turned off the unsettled-tree check that is one of the tool's four
    # reasons to exist; `--max-dirs-per-sec -3` turned off the rate limiter on a
    # shared filesystem. Rejecting them costs one line each and cannot be wrong:
    # none of the four has a defensible meaning.
    # `-i` predates `--sort` and means `--sort files`. Keeping both is right --
    # `-i` is the flag someone reaches for when the quota says "files" -- so
    # resolve them here rather than making every renderer take two arguments.
    #
    # All three arms of this were wrong in one direction or another, and every one
    # of them showed the reader a table whose ordering did not match its own
    # emphasis:
    #
    # * `-c` was not consulted, so plain `rdu -c` resolved to `sort="size"` -- and
    #   a count-only walk has no sizes, so every key was 0, the sort was stable on
    #   an all-zero key, and the ordering was dict insertion order (thread merge
    #   order). Six runs on one tree gave four different orderings, the bars went
    #   2.4%, 55.8%, 5.5%, 10.3%, 25.5% down the table, and at `-n 3` the
    #   second-largest directory in the tree sat behind "2 more".
    # * `--sort size` did not clear `-i`, so `rdu -i --sort size` ordered rows by
    #   bytes while the bar, the share and the accented header column all measured
    #   inodes.
    # * `-c` cannot answer `--sort size` or `--sort density` at all -- both need
    #   bytes -- and silently pretending otherwise is what produced the first case.
    #   It is refused out loud instead.
    if args.count and args.sort in ("size", "density"):
        parser.error(
            "--sort {0} needs byte sizes and -c does not measure them (it skips "
            "stat entirely). Use `-c --sort files`, or drop -c to rank by "
            "{0}.".format(args.sort)
        )
    if args.sort is None:
        args.sort = "files" if (args.inodes or args.count) else "size"
    elif args.sort == "files":
        args.inodes = True
    elif args.sort == "size":
        args.inodes = False

    if args.depth < 1:
        parser.error(
            "--depth must be at least 1 (it is how deep entries are reported, not a filter)"
        )
    if args.top < 0:
        parser.error("-n must be 0 or more; 0 means every entry")
    if args.settle_window < 0:
        parser.error("--settle-window cannot be negative: it is a window into the past")
    if args.max_dirs_per_sec < 0:
        parser.error("--max-dirs-per-sec cannot be negative; 0 disables the rate limit")
    if args.quota_timeout <= 0:
        parser.error("--quota-timeout must be positive; use --no-quota to skip the backend")

    try:
        if args.quota_only:
            code = cmd_quota(args)
        elif args.deleted_only:
            code = cmd_deleted(args)
        else:
            code = cmd_walk(args)
        # Flushed *inside* the guard. Left to interpreter shutdown, a failed write
        # is reported by Python rather than by this tool: `rdu . > /report.txt` on
        # a full filesystem printed
        #
        #     Exception ignored in: <_io.TextIOWrapper name='<stdout>' ...>
        #     OSError: [Errno 28] No space left on device
        #
        # and exited **120**, which is not one of this tool's three codes. A full
        # filesystem is the condition a quota tool is run in, so that is the one
        # write failure it must report properly.
        sys.stdout.flush()
        return code
    except KeyboardInterrupt:
        sys.stderr.write("\nrapidu: interrupted\n")
        return EXIT_ERROR
    except BrokenPipeError:
        # Downstream closed the pipe (`| head`); nothing left to say.
        with contextlib.suppress(Exception):
            sys.stdout.close()
        return EXIT_OK
    except OSError as exc:
        # Caught after BrokenPipeError, which is a subclass of this, and closed
        # the same way rather than redirected. Two reasons:
        #
        # * The interpreter's own shutdown flush must not re-raise and re-print
        #   this as an "ignored exception" -- closing settles that, as the pipe
        #   case already demonstrates.
        # * A failed flush leaves the unwritten report *in the buffer*. Pointing
        #   fd 1 elsewhere leaves it there, and an in-process caller then gets the
        #   stale report emitted into whatever stream comes next -- observed:
        #   a report written to a full device reappeared on the terminal during a
        #   later call. Closing discards it.
        with contextlib.suppress(Exception):
            sys.stdout.close()
        # And the diagnosis itself is best-effort: `rdu . > out 2>&1` on a full
        # filesystem puts stderr on the same full device, so writing there raises
        # too. Unguarded that escaped the handler and the process died at
        # interpreter shutdown with exit 120 and an "ignored exception" dump --
        # the very outcome this branch exists to replace. There is nowhere left to
        # say it, so the exit code carries the whole message.
        try:
            sys.stderr.write("rapidu: cannot write the report: {}\n".format(exc))
            sys.stderr.flush()
        except Exception:
            # Nowhere left to say it, so discard the buffered message too. Left in
            # place, the interpreter's shutdown flush fails on it and *replaces*
            # this exit code with 120 plus an "ignored exception" dump -- which is
            # exactly the outcome this branch exists to prevent, arriving by a
            # second route. stderr is only closed when writing to it has already
            # failed; a working stderr is never touched.
            with contextlib.suppress(Exception):
                sys.stderr.close()
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
