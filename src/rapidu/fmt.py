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
        if v < 1024.0 or unit == _UNITS[-1]:
            s = "{:.{p}f} {}".format(v, unit, p=0 if unit == "B" else precision)
            return "-" + s if neg else s
        v /= 1024.0
    return "n/a"  # unreachable


def human_count(n: Optional[int]) -> str:
    """Thousands-separated integer, or ``n/a``."""
    return "n/a" if n is None else "{:,}".format(n)


def plural(n: Optional[int], noun: str, suffix: str = "s") -> str:
    """``1 file``, ``12 files``, ``n/a files`` -- the count and a noun that agrees.

    Every count this tool prints alongside a noun goes through here, for the same
    reason every byte figure goes through :func:`human_bytes`: the agreement rule
    is stated once. It was not, and so an empty directory reported ``1 files`` on
    the facts line and an interrupted walk reported ``PARTIAL -- 1 files scanned``,
    while ``render_settle`` two sections down got the identical case right.

    ``None`` is unknown, not one: it renders ``n/a`` with the plural noun, because
    ``n/a file`` reads as a singular measurement that was taken.
    """
    return "{} {}".format(human_count(n), noun if n == 1 else noun + suffix)


def human_duration(seconds: Optional[float]) -> str:
    """Coarse human duration used for snapshot ages. ``None`` -> ``unknown``."""
    if seconds is None:
        return "unknown"
    s = int(round(seconds))
    if s < 0:
        # A timestamp in the future: clock skew between the quota host and here.
        return "{}s in the future".format(-s)
    if s < 60:
        return "{}s".format(s)
    if s < 3600:
        return "{}m {}s".format(s // 60, s % 60)
    if s < 86400:
        return "{}h {}m".format(s // 3600, (s % 3600) // 60)
    return "{}d {}h".format(s // 86400, (s % 86400) // 3600)


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
