"""Where the freed-bytes bound actually falls.

:func:`report._freed_since_walk_is_material` demotes the total when the bytes
unlinked since the walk are *at least as large as what is left over* -- the
module's existing bound for "wrong by a factor" rather than "imprecise". Its
comment carried a ratio table measured on eight 64 KiB files and asserted the
result was "identical at 10, 40 and 70 of eighty". It is not, and the reason is
worth pinning: the directory's own blocks are part of the remainder and grow with
the entry count, so the *exactly half* case is decided by that overhead. Measured
here, an 8-entry directory adds 0 bytes to ``res.size`` and an 80-entry one adds
4096, which puts 40-of-80 at 0.9984x -- just inside the quiet side.

That is correct behaviour and a wrong comment. So the boundary is pinned on the
comparison itself, with the sizes supplied directly: a test that counted files on
whatever filesystem ``tmp_path`` landed on would be asserting the overhead of
that filesystem's directories, which is the thing that made the old claim wrong.
"""

import io
import os

from rapidu import report
from rapidu.walk import SettleCheck, WalkResult, recheck_settling, walk


def _at(size, gone_bytes, count_only=False):
    """The predicate, with the two quantities it compares supplied outright."""
    res = WalkResult("/tmp/bound")
    res.size = size
    res.count_only = count_only
    chk = SettleCheck()
    chk.gone_bytes = gone_bytes
    return report._freed_since_walk_is_material(res, chk)


def test_the_bound_is_exactly_gone_versus_remainder():
    """At the bound it fires; one byte below it does not.

    Filesystem-independent by construction, which is the point: this is the
    statement the comment's ratio table was trying to make.
    """
    # gone == remainder: the total is exactly twice the truth
    assert _at(200, 100) is True
    # one byte of remainder more, and the total is 1.99x -- the quiet side
    assert _at(201, 100) is False
    # one byte the other way
    assert _at(199, 100) is True


def test_the_directory_overhead_that_decides_the_half_case():
    """The measured numbers, as the comment now states them.

    4096 bytes is what an 80-entry directory added here. Supplied rather than
    measured off a live tree, so the arithmetic is the assertion.
    """
    files = 80 * 65536
    gone = 40 * 65536
    assert _at(files, gone) is True, "files alone: exactly half is on the bound"
    assert _at(files + 4096, gone) is False, "with the directory, just inside it"
    assert _at(files + 4096, 41 * 65536) is True, "one more file clears it"


def test_control_a_count_only_walk_cannot_know_any_bytes_were_freed():
    """CONTROL, passing with the comment corrected or not.

    ``-c`` reads no blocks, so no freed bytes are knowable and the bound must
    stay silent however lopsided the counts look.
    """
    assert _at(200, 100, count_only=True) is False


def test_control_b_no_deletion_is_never_material():
    """CONTROL, in both states. Zero freed bytes cannot move the total."""
    assert _at(200, 0) is False
    assert _at(0, 0) is False


def test_control_c_the_far_cases_are_unaffected_on_a_real_tree(tmp_path):
    """CONTROL, in both states, and the reason the bound is worth having.

    One file in eight and seven in eight sit far enough from the bound that no
    directory's overhead can move them, so they are safe to assert off a real
    walk -- and they are the two cases the ratio table was really about.
    """

    def built(name, nfiles, ngone):
        root = os.path.join(str(tmp_path), name)
        os.makedirs(root)
        for i in range(nfiles):
            with io.open(os.path.join(root, "f%03d" % i), "wb") as handle:
                handle.write(b"q" * 65536)
        res = walk(root, threads=2, depth=1)
        for entry in sorted(os.listdir(res.root))[:ngone]:
            os.unlink(os.path.join(res.root, entry))
        chk = recheck_settling(res, 0.0)
        chk.gap = 60.0
        return res, chk

    res, chk = built("one_in_eight", 8, 1)
    assert report._freed_since_walk_is_material(res, chk) is False

    res, chk = built("seven_in_eight", 8, 7)
    assert report._freed_since_walk_is_material(res, chk) is True

    res, chk = built("seventy_in_eighty", 80, 70)
    assert report._freed_since_walk_is_material(res, chk) is True
