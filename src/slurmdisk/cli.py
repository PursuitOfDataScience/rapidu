"""Command line interface.

    sd .                 quota + walk + reconcile + where the inodes are
    sd ~/scratch         the same, for another tree
    sd --quota-only      just the quota table, with its snapshot age
    sd --deleted-only    just the unlinked-but-open scan

**The only positional argument is a path.** An earlier version took bare-word
subcommands (``sd quota``, ``sd walk``, ``sd deleted``), which is the wrong shape
for a tool whose primary argument is a directory: ``quota``, ``walk`` and
``deleted`` are all perfectly ordinary directory names, so ``sd deleted`` was
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
from typing import List, Optional

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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="slurmdisk",
        usage="sd [PATH ...] [options]",
        description="Where your bytes and inodes are, what du cannot see, and "
        "how old the quota number you are comparing against is.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
        "  sd .                  how big is this tree, and what is big inside it\n"
        "  sd ~/scratch -n 20    the same, listing 20 directories\n"
        "  sd . -i               rank by inode count instead of bytes\n"
        "  sd . -a               the full report: quota, /proc scan, reconciliation\n"
        "  sd -Q                 just the quota table and the age of its figures\n"
        "  sd -D                 unlinked-but-open space held on this node\n"
        "  sd . -a --settle-wait 60   measure how far a fresh tree is still drifting\n"
        "\n"
        "slurmdisk agrees with `du -s --block-size=1` byte-for-byte on the\n"
        "same tree. It is faster, not more accurate.",
    )
    p.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="directories to walk (default: the current directory)",
    )
    p.add_argument("--json", action="store_true", dest="as_json", help="machine-readable output")
    p.add_argument(
        "-t",
        "--threads",
        type=int,
        default=walkmod.DEFAULT_THREADS,
        help="walk concurrency, clamped to {} (default: %(default)s). "
        "Past the cap the walk measurably slows down and the "
        "metadata load stops being polite.".format(walkmod.MAX_THREADS),
    )
    p.add_argument(
        "-d",
        "--depth",
        type=int,
        default=1,
        help="how deep to break the tree down. 1 lists the immediate children, "
        "like `du -d1` (default: %(default)s)",
    )
    p.add_argument(
        "-n",
        "--top",
        type=int,
        default=10,
        help="how many directories to list per ranking (default: %(default)s)",
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
        "default because `sd .` is asked how big a tree is, not for an audit.",
    )
    p.add_argument(
        "-i",
        "--inodes",
        action="store_true",
        help="rank directories by inode count instead of bytes",
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
        help="(default: %(default)s)",
    )
    p.add_argument(
        "--max-snapshot-age",
        type=float,
        default=rc.DEFAULT_MAX_SNAPSHOT_AGE_S,
        metavar="SECONDS",
        help="a quota snapshot older than this cannot support a finding (default: %(default)s)",
    )
    p.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colourise output. auto = only when stdout is a terminal, and never "
        "when NO_COLOR is set (default: %(default)s)",
    )
    p.add_argument(
        "--ascii", action="store_true", help="draw bars with ASCII instead of block glyphs"
    )
    p.add_argument("-V", "--version", action="store_true", help="print version and exit")
    return p


def _resolve_paths(raw: List[str]) -> List[str]:
    out = []  # type: List[str]
    for p in raw or [os.getcwd()]:
        ap = os.path.abspath(os.path.expanduser(p))
        if not os.path.exists(ap):
            sys.stderr.write("slurmdisk: {}: no such path\n".format(p))
            # Only when it is not a real directory: if ./quota exists it is a
            # path, unambiguously, and gets measured like any other.
            if p in _LEGACY_COMMANDS:
                sys.stderr.write(
                    "slurmdisk: `{0}` is no longer a subcommand -- the only "
                    "positional argument is a path. Did you mean `sd {1}`?\n".format(
                        p, _LEGACY_COMMANDS[p]
                    )
                )
            continue
        if not os.path.isdir(ap):
            sys.stderr.write("slurmdisk: {}: not a directory\n".format(p))
            continue
        out.append(ap)
    return out


def _warn_threads(requested: int) -> None:
    if requested > walkmod.MAX_THREADS:
        sys.stderr.write(
            "slurmdisk: --threads {} clamped to {}: past the cap the walk is "
            "slower (measured: 32 threads was 31% worse than 16) and the "
            "metadata load stops being polite.\n".format(requested, walkmod.MAX_THREADS)
        )
    elif requested < 1:
        sys.stderr.write("slurmdisk: --threads {} raised to 1\n".format(requested))


def cmd_quota(args: argparse.Namespace) -> int:
    paths = [os.path.abspath(os.path.expanduser(p)) for p in args.paths]
    snap = quotamod.read_best(paths[0] if paths else os.getcwd(), args.quota_timeout)
    if args.as_json:
        print(json.dumps(report.to_json(None, None, snap, None, None), indent=2))
    else:
        print(
            "\n".join(
                report.render_quota(snap, paths or None, ui.resolve_style(args.color, args.ascii))
            )
        )
    return EXIT_OK if snap.available else EXIT_ATTENTION


def cmd_deleted(args: argparse.Namespace) -> int:
    targets = _resolve_paths(args.paths) if args.paths else [None]
    rcode = EXIT_OK
    for target in targets:
        scan = deletedmod.scan(target)
        if args.as_json:
            print(json.dumps(report.to_json(None, None, None, scan, None, args.top), indent=2))
        else:
            print("\n".join(report.render_deleted(scan, args.top)))
        if scan.files:
            rcode = EXIT_ATTENTION
    return rcode


def cmd_walk(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args.paths)
    if not paths:
        return EXIT_ERROR
    _warn_threads(args.threads)

    # --json is for tooling, which wants the complete document rather than
    # whichever subset the terminal view happens to show.
    full = args.full or args.as_json
    style = ui.resolve_style(args.color, args.ascii)

    snap = None
    # Both of these are work the default view does not use: the quota backend
    # shells out to a site wrapper that can take seconds, and the /proc sweep
    # walks every pid on the node.
    if full and not args.no_quota:
        snap = quotamod.read_best(paths[0], args.quota_timeout)

    scan = None
    if full and not args.no_deleted:
        scan = deletedmod.scan()

    docs = []
    rcode = EXIT_OK
    for path in paths:
        try:
            res = walkmod.walk(
                path,
                threads=args.threads,
                depth=args.depth,
                max_dirs_per_sec=args.max_dirs_per_sec,
                settle_window=args.settle_window,
                one_file_system=args.one_file_system,
            )
        except OSError as exc:
            sys.stderr.write("slurmdisk: {}\n".format(exc))
            rcode = EXIT_ERROR
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
            print("\n".join(report.render_compact(res, settle, args.top, args.inodes, style)))
        else:
            lines = []  # type: List[str]
            if snap is not None:
                lines.extend(report.render_quota(snap, [path], style))
            lines.extend(
                report.render_walk(
                    res, settle, args.top, scan=path_scan, style=style, by_inodes=args.inodes
                )
            )
            if path_scan.available and path_scan.files:
                lines.extend(report.render_deleted(path_scan, args.top))
            if recs:
                lines.extend(report.render_reconcile(recs, style))
            print("\n".join(lines))

        if not res.complete or settle.moved:
            rcode = EXIT_ATTENTION
        if any(r.verdict == rc.GAP for r in recs):
            rcode = EXIT_ATTENTION

    if args.as_json and docs:
        print(json.dumps(docs[0] if len(docs) == 1 else docs, indent=2))
    return rcode


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.version:
        from . import __version__

        print("slurmdisk {}".format(__version__))
        return EXIT_OK

    if args.quota_only and args.deleted_only:
        parser.error("--quota-only and --deleted-only ask for different reports")

    try:
        if args.quota_only:
            return cmd_quota(args)
        if args.deleted_only:
            return cmd_deleted(args)
        return cmd_walk(args)
    except KeyboardInterrupt:
        sys.stderr.write("\nslurmdisk: interrupted\n")
        return EXIT_ERROR
    except BrokenPipeError:
        # Downstream closed the pipe (`| head`); nothing left to say.
        with contextlib.suppress(Exception):
            sys.stdout.close()
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
