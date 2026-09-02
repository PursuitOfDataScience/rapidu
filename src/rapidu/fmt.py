"""Formatting helpers.

Deliberately tiny and dependency-free. Every number this tool prints goes
through here so that units are stated exactly once, in one place.
"""

from typing import Optional

# Up to EiB, because `quota.parse_size` accepts an `E` suffix and the two have to
# agree: stopping at PiB meant a figure this module could read back it could not
# print, rendering 1 EiB as "1024.0 PiB". Nothing here will meet an exabyte soon,
# but a formatter and its parser disagreeing about the top of the scale is the
# kind of thing that is only ever noticed by the person it confuses.
_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


def human_bytes(n: Optional[int], precision: int = 1) -> str:
    """Render a byte count in IEC units, or ``n/a`` if it is unknown.

    ``None`` is *not* zero (Constraint 10). A caller that has no measurement
    passes ``None`` and gets ``n/a``, never ``0.0 B``.
    """
    if n is None:
        return "n/a"
    neg = n < 0
    v = float(abs(n))
    for unit in _UNITS:
        places = 0 if unit == "B" else precision
        # Compare the ROUNDED value, not the raw one. `v < 1024.0` let a figure
        # just under a boundary through and then rounded it *up* across it, so
        # 1 GiB - 1 printed as "1024.0 MiB" -- 1024 of a unit that has a larger
        # sibling, which no formatter should ever emit. `du -h` and
        # `numfmt --to=iec` both give "1.0G" for that byte count. The window is
        # narrow at MiB (~52 KiB wide) and wide where the numbers here actually
        # live: ~52 MiB below every TiB boundary, on a tool whose headline figure
        # is a filesystem total. Rounded at the same precision the format string
        # will use, so the test and the output cannot disagree.
        #
        # The docstring above `_UNITS` already describes this defect at the TOP of
        # the scale ("rendering 1 EiB as 1024.0 PiB") and fixed it by extending the
        # unit list; the interior boundaries have the same bug for a different
        # reason. A sibling package's `format_bytes` has compared the rounded value
        # since its own sighting of this (A5).
        if unit == _UNITS[-1] or round(v, places) < 1024.0:
            s = "{:.{p}f} {}".format(v, unit, p=places)
            return "-" + s if neg else s
        v /= 1024.0
    return "n/a"  # unreachable


def human_count(n: Optional[int]) -> str:
    """Thousands-separated integer, or ``n/a``."""
    return "n/a" if n is None else "{:,}".format(n)


def noun(n: Optional[int], word: str, suffix: str = "s", irregular: Optional[str] = None) -> str:
    """The form of ``word`` that agrees with ``n`` -- just the noun, no count.

    Split out of :func:`plural` for the callers that cannot take the count and the
    noun as one string: a right-aligned table column has to keep its digits in the
    column and its label outside it, so ``BY AGE`` formatted the number itself and
    appended a hard-coded ``files``, which is how it came to print ``1 files``.
    Alignment was a real constraint; stating the agreement rule twice was not.

    ``irregular`` is for nouns that do not pluralise by suffix (``entry`` ->
    ``entries``), which concatenation cannot reach.
    """
    if n == 1:
        return word
    return irregular if irregular is not None else word + suffix


def plural(n: Optional[int], word: str, suffix: str = "s", irregular: Optional[str] = None) -> str:
    """``1 file``, ``12 files``, ``n/a files`` -- the count and a noun that agrees.

    Every count this tool prints alongside a noun goes through here, for the same
    reason every byte figure goes through :func:`human_bytes`: the agreement rule
    is stated once. It was not, and so an empty directory reported ``1 files`` on
    the facts line and an interrupted walk reported ``PARTIAL -- 1 files scanned``,
    while ``render_settle`` two sections down got the identical case right.

    ``None`` is unknown, not one: it renders ``n/a`` with the plural noun, because
    ``n/a file`` reads as a singular measurement that was taken.
    """
    return "{} {}".format(human_count(n), noun(n, word, suffix, irregular))


def human_duration(seconds: Optional[float]) -> str:
    """Coarse human duration used for snapshot ages. ``None`` -> ``unknown``."""
    if seconds is None:
        return "unknown"
    s = int(round(seconds))
    if s < 0:
        # A timestamp in the future: clock skew between the quota host and here,
        # or a backend publishing UTC where a local time is assumed. Humanised
        # like any other magnitude -- it used to print raw seconds, so the
        # timezone case that `quota._timezone_suspicion` exists to name arrived as
        # "46800s in the future" when "13h in the future" makes the offset, and
        # therefore the diagnosis, legible at a glance.
        return "{} in the future".format(human_duration(-s))
    if s < 60:
        return "{}s".format(s)
    if s < 3600:
        return "{}m {}s".format(s // 60, s % 60)
    if s < 86400:
        return "{}h {}m".format(s // 3600, (s % 3600) // 60)
    return "{}d {}h".format(s // 86400, (s % 86400) // 3600)


def ratio_x(r: Optional[float]) -> str:
    """The allocated-over-apparent ratio, as a figure that is never a false zero.

    One helper because the report has two places to say it and they disagreed:
    the ``WALK`` facts line formatted it ``{:.1f}`` and the ``ALLOCATION`` panel
    ``{:.2f}``, so one tree printed ``(0.0x allocated)`` five lines above
    ``-- 0.01x`` about the identical number.

    Both were also capable of rounding a real measurement to zero, and worst
    exactly where the panel earns its place: a wholly sparse tree sits near
    ``0.00x``, so the sparser the files the closer the reported ratio came to
    saying nothing. Below a hundredth this prints ``<0.01x`` -- an inequality is
    still a measurement, where ``0.00x`` reads as one that failed. A ratio of
    exactly zero is not that case: nothing was allocated, which is a fact, and it
    prints ``0x``.
    """
    if r is None:
        return "n/a"
    if r <= 0:
        return "0x"
    if r >= 1.0:
        return "{:.1f}x".format(r)
    if r >= 0.01:
        return "{:.2f}x".format(r)
    return "<0.01x"


def files_per_gib(size_bytes: int, files: int) -> Optional[float]:
    """Inode density: the ranking signal for "what should I pack?".

    Returns ``None`` for a subtree with no bytes, rather than dividing by zero
    and reporting an infinity as though it were a measurement.
    """
    gib = size_bytes / float(1 << 30)
    if gib <= 0:
        return None
    return files / gib


def pct(part: Optional[float], whole: Optional[float]) -> str:
    """Percentage, or ``n/a`` when the denominator is missing or zero."""
    if part is None or not whole:
        return "n/a"
    return "{:.1f}%".format(100.0 * part / whole)
