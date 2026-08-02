"""Allocated versus apparent -- the question the tool exists to answer.

Both numbers were always in ``WalkResult`` and the report printed them forty
characters apart without ever dividing one by the other, so a reader who could
do that arithmetic themselves did not need the tool. These tests pin the join.

The allocation unit is *measured*, not assumed. ``statvfs`` cannot supply it:
on the GPFS this was written against it reports the 4 MiB block size while files
actually allocate in 16 KiB subblocks -- a 256x error in the direction that
makes small files look free.
"""

from rapidu import report, ui
from rapidu.walk import WalkResult

PLAIN = ui.resolve_style("never")

KIB = 1 << 10
MIB = 1 << 20


def padded(files=3000, apparent_each=8 * KIB, unit=16 * KIB):
    """A tree of small files, each paying for a whole allocation unit."""
    r = WalkResult("/tmp/small")
    r.files, r.dirs = files, 1
    r.apparent = files * apparent_each
    r.size = files * unit
    r.padded_files = files
    r.padded_apparent = files * apparent_each
    r.padded_alloc = files * unit
    r.alloc_bits = unit
    return r


def under(files=30000, apparent_each=3000, alloc_each=512):
    """Data small enough to live in the inode: bytes near-free, inodes are not."""
    r = WalkResult("/tmp/inline")
    r.files, r.dirs = files, 1
    r.apparent = files * apparent_each
    r.size = files * alloc_each
    r.under_files = files
    r.under_apparent = files * apparent_each
    r.under_alloc = files * alloc_each
    return r


def test_the_allocation_unit_is_measured_from_the_tree():
    r = padded()
    assert r.alloc_unit == 16 * KIB
    assert r.padding == 3000 * 8 * KIB
    assert abs(r.alloc_ratio - 2.0) < 1e-9


def test_the_unit_is_the_lowest_set_bit_not_the_smallest_allocation():
    """Mixed allocations still identify the unit they are all multiples of."""
    r = padded()
    r.alloc_bits = (16 * KIB) | (32 * KIB) | (64 * KIB)
    assert r.alloc_unit == 16 * KIB


def test_no_padded_files_means_no_claim_about_the_unit():
    """A tree carrying no evidence of the unit reports none, rather than a guess."""
    r = WalkResult("/tmp/x")
    assert r.alloc_unit is None
    assert r.alloc_ratio is None


def test_padding_is_named_and_quantified():
    out = "\n".join(report.render_allocation(padded(), PLAIN))
    assert "2.0x" in out
    assert "quota is charged the first number" in out
    assert "16.0 KiB allocation unit" in out
    assert "8.0 KiB" in out  # the average file, against that unit
    assert "23.4 MiB" in out  # the padding itself
    assert "Packing" in out


def test_stored_below_apparent_is_not_reported_as_an_error():
    """Data-in-inode is a *feature*. It must point at inodes, not invent a bug."""
    out = "\n".join(report.render_allocation(under(), PLAIN))
    assert out, "the deflated direction must still be explained"
    assert "inode" in out
    assert "nearly free" in out
    for alarming in ("quota is charged", "Packing", "padding"):
        assert alarming not in out


def test_a_ratio_near_one_says_nothing():
    r = WalkResult("/tmp/ordinary")
    r.apparent = 10_000 * MIB
    r.size = 10_400 * MIB  # 1.04x: rounding and metadata, not a finding
    assert not report.allocation_is_material(r)
    assert report.render_allocation(r, PLAIN) == []


def test_a_lopsided_ratio_on_a_tiny_tree_says_nothing_either():
    """8x of nothing is still nothing; the floor keeps trivia out of the report."""
    r = padded(files=20)
    assert r.alloc_ratio == 2.0
    assert not report.allocation_is_material(r)


def test_count_mode_makes_no_allocation_claim():
    """-c never called stat, so it has no bytes to draw a conclusion from."""
    r = padded()
    r.count_only = True
    assert not report.allocation_is_material(r)


def test_the_walk_line_states_a_ratio_not_a_second_bare_number():
    from rapidu.walk import SettleCheck

    r = padded()
    r.elapsed, r.threads = 1.0, 8
    out = "\n".join(report.render_walk(r, SettleCheck(), style=PLAIN))
    assert "(2.0x allocated)" in out
    assert "16.0 KiB allocation unit" in out


def test_the_unit_is_measured_from_a_real_filesystem(tmp_path):
    """End to end: write files smaller than the unit and read the unit back.

    Deliberately makes no claim about *which* unit -- that is the filesystem's
    business and differs between /home and /scratch on the same cluster. What is
    asserted is that whatever unit the files landed on is the one reported, and
    that it explains the gap between apparent and allocated.
    """
    import os

    from rapidu.walk import walk

    root = str(tmp_path)
    payload = b"x" * 1000
    for i in range(64):
        with open(os.path.join(root, "f%02d" % i), "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

    r = walk(root, threads=2)
    if not r.padded_files:
        # Every file landed inside its inode, or exactly on a unit boundary.
        # Both are legitimate filesystem behaviour and neither is evidence of
        # the unit, so there is nothing here to check.
        return
    unit = r.alloc_unit
    assert unit and unit & (unit - 1) == 0, "an allocation unit is a power of two"
    assert r.padded_alloc % unit == 0, "every allocation is a whole number of units"
    assert r.padding == r.padded_alloc - r.padded_apparent
    # The measured unit must be consistent with what the filesystem did.
    st = os.lstat(os.path.join(root, "f00"))
    assert st.st_blocks * 512 % unit == 0


def test_inline_files_do_not_become_the_allocation_unit(tmp_path):
    """A one-sector inline file is not evidence of the block size.

    GPFS gives a 100-byte file a single 512-byte sector. 512 > 100, so it looked
    like a padded file, joined the unit estimate, and dragged the reported unit
    from the true 16 KiB down to 512 B -- setting the one number that makes the
    diagnosis actionable from the files it does not describe.
    """
    import os

    from rapidu.walk import MIN_ALLOC_UNIT, walk

    root = str(tmp_path)
    for i in range(40):  # tiny files: inline wherever the filesystem supports it
        with open(os.path.join(root, "t%02d" % i), "wb") as fh:
            fh.write(b"x" * 100)
            fh.flush()
            os.fsync(fh.fileno())
    for i in range(40):  # files that genuinely occupy a block and then some
        with open(os.path.join(root, "b%02d" % i), "wb") as fh:
            fh.write(b"x" * (MIN_ALLOC_UNIT + 1))
            fh.flush()
            os.fsync(fh.fileno())

    r = walk(root, threads=2)
    assert r.alloc_unit is None or r.alloc_unit >= MIN_ALLOC_UNIT
    assert r.inline_files + r.padded_files + r.under_files <= r.files
