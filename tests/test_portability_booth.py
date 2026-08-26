"""The allocation panel, and the mount layer, on a *third* and *fourth* cluster.

Every test here is a finding from running the working tree on the two Booth
clusters -- mercury (RHEL 9.7, ``/usr/bin/python3`` 3.9) and pythia (RHEL 8.10,
``/usr/bin/python3`` 3.12) -- which between them differ from the GPFS site the
package grew up on in three ways that changed what it printed:

* **home is NFSv3 over OneFS, not GPFS.** ``st_blocks`` there carries the
  filesystem's data-protection overhead, so the gap between allocated and
  apparent is charged per byte stored rather than per file. Measured on
  mercury with incompressible data: every file from 1 B to 128 KiB reports
  exactly 24 KiB allocated, a 1 MiB file reports 1.273x its length and a 4 MiB
  file 1.256x.
* **there is no quota backend at all.** ``quota`` is installed and exits 1 with
  no output, ``mmlsquota`` and ``lfs`` are absent, and the export's limit
  arrives through ``statvfs`` -- the case the ``MountReport`` fallback and its
  "that is statvfs, not a quota backend" note already exist for.
* **``/home`` is an autofs map, one NFS mount per user.** The enclosing mount of
  ``/home/<me>/x`` is ``/home/<me>``, and the mount table also lists every other
  user's home that has been automounted since boot.

Nothing here is Booth-specific: each is a shape any site can have. The first is
the one that made the tool wrong rather than quiet, so most of the file is about
it.
"""

import os

from rapidu import cli, report, ui
from rapidu import deleted as D
from rapidu import quota as Q
from rapidu import walk as walkmod

PLAIN = ui.resolve_style("never")


def _flat(lines):
    return " ".join(" ".join(lines).split())


# --------------------------------------------------------------------------
# Padding a partly filled unit cannot produce, reported as padding
# --------------------------------------------------------------------------
#
# The sentence under the allocation headline read, on mercury's `~/.local`:
#
#   29,132 files average 190.6 KiB against a 8.0 KiB allocation unit, so they
#   occupy 1.0 GiB of padding. Packing them (tar, squashfs, a single archive)
#   returns it.
#
# Three of those figures cannot hold at once. A file allocated in whole 8 KiB
# units pays at most 8191 bytes for its last one, so 29,132 of them are bounded
# by 227.6 MiB -- and the measured gap was 1021.3 MiB, 4.5x that. The mechanism
# named in the "so" was refuted by the two numbers on either side of it, and the
# remedy that followed returns none of a per-byte overhead: the tarball is
# protected too.
#
# The figures below are the real ones, from `--json` on mercury.


def _mercury_local():
    """`~/.local` on mercury, as `--json` measured it."""
    r = walkmod.WalkResult("/home/youzhi/.local")
    r.files, r.dirs = 29235, 2496
    r.size = 6644662272
    r.apparent = 5676020224
    r.padded_files = 29132
    r.padded_alloc = 6642409472
    r.padded_apparent = r.padded_alloc - 1070842485
    r.alloc_bits = 8192
    return r


def _midway3_gpfs():
    """A 1.2M-inode GPFS tree on midway3, as `--json` measured it.

    The site the packing advice was written for, at a size where the ceiling is
    not a rounding artefact: 711,302 padded files against a 16 KiB subblock, and
    4.7 GiB of padding under a 10.9 GiB ceiling.
    """
    r = walkmod.WalkResult("/project/rcc/youzhi/.cache")
    r.files, r.dirs = 1044039, 170034
    r.size = 409717403136
    r.apparent = 414338010321
    r.padded_files = 711302
    r.padded_apparent = 414338010321
    r.padded_alloc = 414338010321 + 5097100219
    r.alloc_bits = 16384
    return r


def _gpfs_small_files():
    """The canonical case the panel exists for: 500,000 2 KiB files, 16 KiB subblocks.

    `_midway3_gpfs` is real but its ratio is 0.99x, so `allocation_is_material`
    keeps the panel shut -- correctly, there is nothing to say about a tree whose
    allocation matches its data. This is the shape that does print, and it must
    keep printing the packing advice: every one of those files pays for a whole
    subblock, and packing them genuinely returns 6.7 GiB.
    """
    r = walkmod.WalkResult("/scratch/midway3/youzhi/small")
    r.files, r.dirs = 500000, 1
    r.padded_files = 500000
    r.padded_apparent = 500000 * 2048
    r.padded_alloc = 500000 * 16384
    r.apparent = r.padded_apparent
    r.size = r.padded_alloc
    r.alloc_bits = 16384
    return r


def test_the_padding_ceiling_is_what_whole_units_can_produce():
    """`padded_files * (unit - 1)`, and nothing about the site."""
    res = _mercury_local()
    assert res.alloc_unit == 8192
    assert res.unit_padding_ceiling == 29132 * 8191
    assert res.padding == 1070842485
    assert res.padding > res.unit_padding_ceiling, "the measurement that refutes the mechanism"


def test_an_unmeasured_unit_has_no_ceiling_rather_than_a_guessed_one():
    """Same condition under which the report already drops the unit clause.

    A tree of 64-byte files allocates one 512-byte sector each, which is below
    `MIN_ALLOC_UNIT` and so contributes no evidence of the unit -- and that tree
    is the most packable there is. It must keep the packing advice, not lose it
    to a ceiling invented from nothing.
    """
    r = walkmod.WalkResult("/tmp/tiny")
    r.padded_files, r.padded_apparent, r.padded_alloc = 50000, 3200000, 25600000
    assert r.alloc_unit is None
    assert r.unit_padding_ceiling is None
    r.files, r.dirs = 50000, 1
    r.size, r.apparent = 25600000, 3200000
    text = _flat(report.render_allocation(r, PLAIN))
    assert "Packing them" in text, text
    assert "erasure coding" not in text, text


def test_no_padded_file_means_no_ceiling():
    """Nothing was padded, so there is no bound to state about padding."""
    r = walkmod.WalkResult("/tmp/none")
    r.alloc_bits = 16384
    assert r.padded_files == 0
    assert r.unit_padding_ceiling is None


def test_a_gap_bigger_than_units_can_explain_does_not_offer_packing():
    """The remedy has to match the cause, and on this filesystem it did not."""
    text = _flat(report.render_allocation(_mercury_local(), PLAIN))
    assert "Packing them" not in text, text
    assert "packing will not return it" in text, text
    # Both halves named, so the reader can see which is which rather than being
    # told a total and a mechanism that cannot produce it.
    assert "account for at most 227.6 MiB" in text, text
    assert "of the 1021.2 MiB gap" in text, text
    assert "the remaining 793.7 MiB" in text, text


def test_the_gpfs_tree_the_advice_was_written_for_keeps_it():
    """The fix must not cost the site the panel was built on its own answer."""
    assert _midway3_gpfs().padding < _midway3_gpfs().unit_padding_ceiling
    res = _gpfs_small_files()
    assert res.padding < res.unit_padding_ceiling
    text = _flat(report.render_allocation(res, PLAIN))
    assert "Packing them (tar, squashfs, a single archive) returns it." in text, text
    assert "erasure coding" not in text, text


def test_the_two_views_agree_on_whether_units_can_explain_the_gap():
    """`--json` carries the ceiling the human report decided on, not a re-derivation."""
    for res in (_mercury_local(), _gpfs_small_files()):
        alloc = report.to_json(res, walkmod.SettleCheck(), None, None, None)["walk"]["allocation"]
        assert alloc["unit_padding_ceiling_bytes"] == res.unit_padding_ceiling
        offers_packing = "Packing them" in _flat(report.render_allocation(res, PLAIN))
        explained = alloc["padding_bytes"] <= alloc["unit_padding_ceiling_bytes"]
        assert offers_packing is explained, alloc


def test_a_count_only_walk_publishes_no_ceiling_either():
    """`-c` never stats, so there is no allocation to bound."""
    r = walkmod.WalkResult("/tmp/c")
    r.count_only = True
    alloc = report.to_json(r, walkmod.SettleCheck(), None, None, None)["walk"]["allocation"]
    assert alloc["unit_padding_ceiling_bytes"] is None
    assert alloc["padding_bytes"] is None


# --------------------------------------------------------------------------
# One row per inode column, and a noun that agrees with it
# --------------------------------------------------------------------------
#
# A home shared with a single root-owned file printed `1 inodes` in the owners
# table, and a one-inode cache printed `1 inodes` in RECLAIMABLE directly above a
# total line that got it right -- `plural` was stated once and then bypassed at
# three call sites, which is what `noun` exists to prevent.


def test_a_single_inode_row_agrees_in_the_owners_table():
    r = walkmod.WalkResult("/home/youzhi")
    r.files, r.dirs = 3, 1
    r.size = r.apparent = 4096
    r.by_uid = {82889: [4096, 3], 0: [32768, 1]}
    r.by_gid = {100000: [4096, 3], 0: [32768, 1]}
    text = _flat(report.render_walk(r, walkmod.SettleCheck(), style=PLAIN))
    assert "1 inodes" not in text, text
    assert "1 inode" in text, text
    assert "3 inodes" in text, text


def _reclaim_result(inodes):
    """A walk whose one reclaimable match holds `inodes` of them.

    Built rather than walked: a real `.nv/ComputeCache` holding one file is two
    inodes -- the directory counts, which is the whole point of the column -- and
    an empty one has no bytes to rank. The row is what is under test.
    """
    r = walkmod.WalkResult("/home/youzhi")
    r.size = r.apparent = 40960
    r.files, r.dirs = inodes, 1
    entry = walkmod.Entry("/home/youzhi/.nv/ComputeCache", True)
    entry.add(40960, inodes, 0)
    r.dir_agg = {".nv/ComputeCache": entry}
    return r


def test_a_single_inode_row_agrees_in_the_reclaim_table():
    text = _flat(report.render_reclaimable(_reclaim_result(1), PLAIN))
    assert "1 inodes" not in text, text
    assert "1 inode " in text, text
    assert "12 inodes" in _flat(report.render_reclaimable(_reclaim_result(12), PLAIN))


def test_the_reclaim_rows_keep_the_path_column_aligned():
    """The noun sits in a fixed field, so one and many rows still line up.

    Alignment was the reason the hard-coded `inodes` was there in the first
    place, so agreeing must not cost it: `inode` is one character shorter and
    the path column would otherwise shift by one between rows.
    """
    one, many = (
        [ln for ln in report.render_reclaimable(_reclaim_result(n), PLAIN) if "ComputeCache" in ln][
            0
        ]
        for n in (1, 12)
    )
    assert one.index("ComputeCache") == many.index("ComputeCache"), (one, many)


# --------------------------------------------------------------------------
# `/home` is an automount map, and the mount that matters is one level down
# --------------------------------------------------------------------------

BOOTH_MOUNTS = """\
proc /proc proc rw,relatime 0 0
auto.home /home autofs rw,relatime,fd=6,pgrp=1520,timeout=300,indirect 0 0
sisyphus-n.chicagobooth.edu:/ifs/data/booth/system/home/staff/youzhi /home/youzhi nfs rw,vers=3 0 0
sisyphus-n.chicagobooth.edu:/ifs/data/booth/system/home/phd/other /home/other nfs rw,vers=3 0 0
sisyphus-n.chicagobooth.edu:/ifs/data/booth/system/data/slurm/apps /apps nfs rw,vers=3 0 0
"""


def test_the_enclosing_nfs_mount_is_the_users_own_home(tmp_path):
    """`/home` is autofs; the filesystem holding the bytes is `/home/<user>`."""
    table = tmp_path / "mounts"
    table.write_text(BOOTH_MOUNTS)
    assert Q._enclosing_mount("/home/youzhi/.cache", ("nfs", "nfs4"), str(table)) == "/home/youzhi"
    # And the neighbour's automounted home is not it, however alphabetical.
    assert Q._enclosing_mount("/home/other/x", ("nfs", "nfs4"), str(table)) == "/home/other"


def test_an_autofs_map_is_not_offered_as_the_filesystem(tmp_path):
    """Asking for the nfs mount must not return the autofs indirect map above it."""
    table = tmp_path / "mounts"
    table.write_text(BOOTH_MOUNTS)
    entries = {point: fstype for _dev, point, fstype in Q._mount_entries(str(table))}
    assert entries["/home"] == "autofs"
    assert entries["/home/youzhi"] == "nfs"
    assert Q._enclosing_mount("/home/youzhi", ("autofs",), str(table)) == "/home"


def test_a_home_with_no_quota_backend_still_reports_the_export(tmp_path):
    """`quota` exits 1 with no output here; the mount fallback is the whole answer.

    The note that follows it -- statvfs cannot tell a per-user export limit from
    the whole filesystem -- is the reason this is reported at all rather than
    dropped, and it is the only line on these two clusters that answers "how full
    am I".
    """
    lines = _flat(report._mount_fallback([str(tmp_path)], PLAIN))
    assert "the mount at" in lines
    assert "that is statvfs, not a quota backend" in lines


def test_quota_that_exits_nonzero_with_no_output_is_not_a_parse_failure(monkeypatch):
    """A backend that said nothing has not produced unparseable output.

    Reporting "could not parse `quota -s` output" for an empty stdout blames the
    parser for a backend that declined to answer, which is the wrong-cause
    mistake this layer keeps making. Both Booth clusters are in exactly this
    state: quota-tools 4.09 installed, exit 1, stdout empty.
    """
    monkeypatch.setattr(
        Q.shutil, "which", lambda tool: "/usr/bin/quota" if tool == "quota" else None
    )
    monkeypatch.setattr(Q, "_run", lambda *a, **k: (1, "", ""))
    snap = Q.read_quota_command(timeout=1.0)
    assert not snap.rows
    assert "no output" in snap.reason, snap.reason
    assert "parse" not in snap.reason, snap.reason


# --------------------------------------------------------------------------
# A backend that told us where the cause was, and we printed the signpost
# --------------------------------------------------------------------------

# Verbatim stderr from `mmlsquota -Y -u <user>` on a pythia login node. All three
# lines are on stderr together, which is why reading one stream is enough.
PYTHIA_MMLSQUOTA_STDERR = """\
Failed to connect to file system daemon: No such process
mmlsquota: GPFS is down on this node.
mmlsquota: Command failed. Examine previous error messages to determine cause.
"""

# The midway2 shape, for contrast: here the *first* marker line is already the
# cause, and this fix must not move it.
MIDWAY2_MMLSQUOTA_STDERR = """\
No quota enabled file system found.
mmlsquota: tslsquota  -Y  failed. Error code 22.
mmlsquota: Command failed. Examine previous error messages to determine cause.
"""


def test_the_reported_cause_is_not_the_line_pointing_at_the_cause():
    """`Examine previous error messages` is a signpost; print what it points at."""
    trouble = Q._mmlsquota_trouble(PYTHIA_MMLSQUOTA_STDERR)
    assert "GPFS is down on this node" in trouble, trouble
    assert "Examine previous error messages" not in trouble, trouble


def test_a_cause_already_first_stays_first():
    """midway2 read correctly and must keep reading correctly."""
    assert Q._mmlsquota_trouble(MIDWAY2_MMLSQUOTA_STDERR) == "No quota enabled file system found."


def test_the_signpost_alone_still_vetoes_the_output():
    """It is the only diagnostic there is, so it is both the veto and the message.

    Returning "" would let `:HEADER:`-less output through as a parse, which is
    what the marker list exists to prevent.
    """
    only = "mmlsquota: Command failed. Examine previous error messages to determine cause."
    assert Q._mmlsquota_trouble(only + "\n") == only


def test_a_record_line_is_never_mistaken_for_a_diagnostic():
    """A fileset or remarks field may contain the words; a record is not prose."""
    assert Q._mmlsquota_trouble("mmlsquota:user:HEADER:1:2:3:4:5:command failed:x\n") == ""


def test_a_down_gpfs_client_is_reported_as_down_not_as_no_quota(monkeypatch):
    """The whole point: two failures with different remedies, told apart.

    "No quota enabled file system found" means ask for a quota; "GPFS is down on
    this node" means this node cannot answer and another one can. They arrived as
    the same sentence.
    """
    monkeypatch.setattr(Q.shutil, "which", lambda tool: "/usr/lpp/mmfs/bin/" + tool)
    monkeypatch.setattr(Q, "_run", lambda *a, **k: (50, "", PYTHIA_MMLSQUOTA_STDERR))
    snap = Q.read_mmlsquota("/project_gpfs", timeout=1.0)
    assert not snap.available
    assert "GPFS is down on this node" in snap.reason, snap.reason
    assert "Examine previous" not in snap.reason, snap.reason


def test_the_two_clusters_disagree_about_an_empty_quota_and_both_are_handled():
    """One home, two login nodes, two spellings of "you have no quota".

    mercury's quota-tools 4.09 exits 1 and prints nothing; pythia's prints
    `Disk quotas for user youzhi (uid 82889): none` and exits 0. Same NFS home,
    same tool version. Neither is a parse failure and neither is a quota.
    """
    assert Q._QUOTA_NONE_RE.search("Disk quotas for user youzhi (uid 82889): none")
    rows = Q._parse_stock_quota("Disk quotas for user youzhi (uid 82889): none\n")
    assert rows == []


# --------------------------------------------------------------------------
# A feature that was a guaranteed no-op on two of the three clusters
# --------------------------------------------------------------------------
#
# `UNLINKED BUT STILL OPEN` is the section that finds space `du` cannot. On NFS
# there is never anything for it to find: the client renames the entry to
# `.nfsXXXX` and removes it when the last descriptor closes, so `st_nlink` is 1
# and the readlink target has no " (deleted)" suffix -- the two gates that make
# the local finding trustworthy both reject it, correctly. The panel therefore
# read "none found" on every NFS site whatever was held, which is true and tells
# the reader nothing.
#
# Measured on mercury: after `unlink` the directory held
# `.nfs00000002945e149d00002b83`, `/proc/self/fd/N` pointed at that real path,
# and the entry vanished on close.


def test_the_silly_rename_name_is_recognised():
    """The kernel's own format: `.nfs` + file id + counter, all hex."""
    assert D._SILLY_RENAME_RE.match(".nfs00000002945e149d00002b83")
    assert D._SILLY_RENAME_RE.match(".nfs0000000A")


def test_an_ordinary_dotfile_is_not_a_silly_rename():
    """`.nfs` prefixed names that are not all-hex belong to their owners."""
    for name in (".nfsrc", ".nfs", ".nfs-backup", ".nfs00000002945e149d00002b83.old", "nfs0000"):
        assert not D._SILLY_RENAME_RE.match(name), name


def test_a_deleted_but_open_nfs_file_is_found_and_reported(tmp_path, monkeypatch):
    """End to end through the real `/proc`, with the silly rename made by hand.

    The name is what NFS would have produced and the descriptor is genuinely
    open, which is the whole of the condition -- so this runs on any filesystem
    and asserts the reporting rather than the client's rename.
    """
    held = tmp_path / ".nfs00000002945e149d00002b83"
    handle = open(str(held), "wb")
    try:
        handle.write(b"z" * (2 << 20))
        handle.flush()
        os.fsync(handle.fileno())
        scan = D.scan(prefix=str(tmp_path), timeout=20.0)
        assert not scan.files, "nothing here is unlinked, and it must not be claimed as such"
        assert len(scan.silly_renamed) == 1, scan.silly_renamed
        found = scan.silly_renamed[0]
        assert found.path == str(held)
        assert os.getpid() in found.pids
        assert scan.silly_renamed_size == found.size

        text = _flat(report.render_deleted(scan, 10, PLAIN))
        assert "held by deleted-but-open files on an NFS mount" in text, text
        assert "released when the last descriptor closes" in text, text
        # And the panel no longer contradicts itself two lines apart.
        assert "on NFS a deleted-but-open file keeps an entry" in text, text
    finally:
        handle.close()


def test_a_hard_link_to_a_silly_rename_name_is_not_a_deletion(tmp_path):
    """A second link means nobody deleted it; the name is a hint, not proof.

    The same reasoning as the `nlink == 0` gate on the unlinked side: this
    section does not make findings out of names.
    """
    real = tmp_path / "keep.bin"
    real.write_bytes(b"z" * 4096)
    decoy = tmp_path / ".nfs00000002945e149d00002b83"
    os.link(str(real), str(decoy))
    handle = open(str(decoy), "rb")
    try:
        scan = D.scan(prefix=str(tmp_path), timeout=20.0)
        assert scan.silly_renamed == [], scan.silly_renamed
    finally:
        handle.close()


def test_the_prefix_filter_applies_to_the_nfs_half_too(tmp_path):
    """`-D <path>` must scope both findings the same way."""
    inside = tmp_path / "inside"
    inside.mkdir()
    handle = open(str(inside / ".nfs00000002945e149d00002b83"), "wb")
    try:
        handle.write(b"z" * 4096)
        handle.flush()
        assert D.scan(prefix=str(inside), timeout=20.0).silly_renamed
        assert D.scan(prefix=str(tmp_path / "elsewhere"), timeout=20.0).silly_renamed == []
        # And `under()` narrows what a broad scan already collected.
        broad = D.scan(prefix=str(tmp_path), timeout=20.0)
        assert broad.silly_renamed
        assert broad.under(str(tmp_path / "elsewhere")).silly_renamed == []
        assert broad.under(str(inside)).silly_renamed
    finally:
        handle.close()


def test_the_two_views_agree_about_the_nfs_half(tmp_path):
    """`--json` publishes it in its own fields, not folded into `total_bytes`."""
    handle = open(str(tmp_path / ".nfs00000002945e149d00002b83"), "wb")
    try:
        handle.write(b"z" * (1 << 20))
        handle.flush()
        os.fsync(handle.fileno())
        scan = D.scan(prefix=str(tmp_path), timeout=20.0)
        doc = report.to_json(None, None, None, scan, None)["deleted_but_open"]
        assert doc["nfs_silly_renamed_inodes"] == 1
        assert doc["nfs_silly_renamed_bytes"] == scan.silly_renamed_size
        # `total_bytes` is documented as space no walk can see. These bytes are
        # visible, so they must not be added to it.
        assert doc["total_bytes"] == 0
        assert doc["inodes"] == 0
        assert doc["nfs_silly_renamed"][0]["path"] == str(tmp_path / ".nfs00000002945e149d00002b83")
    finally:
        handle.close()


def test_one_held_inode_gets_a_noun_that_agrees():
    """`held by open file descriptors in 1 inodes`, on the section's headline."""
    scan = D.DeletedScan()
    scan.files = [D.DeletedFile(1, 2, 4096, "/tmp/gone")]
    text = _flat(report.render_deleted(scan, 10, PLAIN))
    assert "in 1 inode " in text, text
    assert "1 inodes" not in text, text


def test_the_nfs_half_is_kept_out_of_reconciliation(tmp_path):
    """The walk already charged a `.nfsXXXX` entry; adding it again invents a gap.

    `reconcile` explains a quota as walk + unlinked-but-open, through
    `owned_by`/`owned_by_gid`. Those read `files`, and these bytes are an
    ordinary directory entry the walk has already counted -- so a scan that
    folded the two together would over-report by exactly this amount, in the
    section whose job is to close a gap rather than open one.
    """
    handle = open(str(tmp_path / ".nfs00000002945e149d00002b83"), "wb")
    try:
        handle.write(b"z" * (1 << 20))
        handle.flush()
        os.fsync(handle.fileno())
        scan = D.scan(prefix=str(tmp_path), timeout=20.0)
        assert scan.silly_renamed, "the fixture must be found at all"
        assert scan.owned_by(os.getuid()) == []
        assert scan.owned_by_gid(os.getgid()) == []
        assert scan.total_size == 0
        # And the walk does see it, which is why the above must hold.
        res = walkmod.walk(str(tmp_path), threads=2)
        assert res.files == 1, res.files
    finally:
        handle.close()


# --------------------------------------------------------------------------
# One report, one width
# --------------------------------------------------------------------------
#
# Two `rdu` runs a second apart in the same 125-column terminal drew an
# 84-column frame for a clean tree and a 125-column one for a tree with an
# allocation warning -- with the table stranded in a 41-column gutter beside the
# prose that had set the width. `ui.box` hugs the widest line it is given, and
# prose was wrapping to the terminal while the table laid itself out to its own
# natural width, so the most elastic element in the report decided the size of
# everything else.


def _wide(columns):
    style = ui.resolve_style("never")
    style.width = columns
    return style


def _padded_tree():
    r = walkmod.WalkResult("/home/u")
    r.files, r.dirs = 13985, 13599
    r.size, r.apparent = 720 << 20, 609 << 20
    r.padded_files = 13985
    r.padded_apparent = 13985 * 43110
    r.padded_alloc = r.padded_apparent + (136 << 20)
    r.alloc_bits = 16384
    return r


def test_prose_wraps_to_the_layout_not_to_the_terminal():
    """A 240-column terminal must not produce a 240-column sentence."""
    for columns in (100, 125, 160):
        lines = report.render_allocation(_padded_tree(), _wide(columns), indent="    ")
        assert lines, columns
        assert max(ui.visible_width(ln) for ln in lines) <= report._LAYOUT_COLUMNS, (
            columns,
            lines,
        )


def test_the_warning_headline_wraps_like_every_other_warning():
    """It was appended unwrapped -- 91 columns that set the frame on their own."""
    lines = report.render_allocation(_padded_tree(), _wide(125), indent="    ")
    assert lines[0].startswith("!"), lines[0]
    assert ui.visible_width(lines[0]) <= report._LAYOUT_COLUMNS, lines[0]
    # And the continuation is aligned under the `! `, not at column zero, so it
    # does not read as a separate statement.
    assert lines[1].startswith("  ") and not lines[1].startswith("   "), lines[1]


def test_a_narrow_terminal_still_bounds_the_prose():
    """The layout is a cap, not a floor: 60 columns means 60 columns."""
    lines = report.render_allocation(_padded_tree(), _wide(60), indent="  ")
    assert lines
    # `_wrapped` floors its wrap at 40 columns, so a 60-column terminal is the
    # binding constraint and nothing may exceed the layout either way.
    assert max(ui.visible_width(ln) for ln in lines) <= report._LAYOUT_COLUMNS


def test_the_frame_does_not_widen_just_because_a_warning_fired():
    """The two shapes the user saw side by side, at one terminal width.

    The table's own width is what the frame should follow; an added sentence must
    not move it. Exact equality is not the claim -- a table is as wide as its
    longest path -- so this asserts the frame stays within a few columns of the
    layout rather than jumping to the terminal edge.
    """
    style = _wide(125)
    res = _padded_tree()
    res.dir_agg = {}
    for name, size in (("ArgonneAI", 292 << 20), ("daily-learning", 109 << 20)):
        entry = walkmod.Entry("/home/u/" + name, True)
        entry.add(size, 100, 10)
        res.dir_agg[name] = entry

    quiet = walkmod.WalkResult("/home/u")
    quiet.files, quiet.dirs = 13985, 13599
    quiet.size = quiet.apparent = 720 << 20
    quiet.dir_agg = res.dir_agg

    def framed(r):
        body = report.render_walk(r, walkmod.SettleCheck(), style=style)
        return max(ui.visible_width(ln) for ln in ui.box(body, style))

    warned, calm = framed(res), framed(quiet)
    assert abs(warned - calm) <= 4, (warned, calm)
    assert warned <= report._LAYOUT_COLUMNS + ui.BOX_CHROME + 4, warned


def test_the_rule_and_the_layout_are_one_number():
    """Three widths that have to agree, so they come from one constant."""
    assert report._LAYOUT_COLUMNS == report._RULE_COLUMNS + 2
    rule = report._section_rule(_wide(125))
    assert ui.visible_width(rule) == report._RULE_COLUMNS
    # And it still shrinks with a narrow terminal rather than overflowing it.
    assert ui.visible_width(report._section_rule(_wide(60))) == 59


# --------------------------------------------------------------------------
# Performance: the two things that were costing wall time
# --------------------------------------------------------------------------
#
# Both measured on a 1.19M-inode GPFS tree, and both are about *scale* rather
# than about any one report being wrong -- which is why they need tests that
# would notice the cost coming back rather than tests of the output.


def test_nested_reclaim_matches_are_dropped_without_comparing_every_pair():
    """The dedup is per-path, so its cost is depth and not the match count.

    `any(other != path and path.startswith(other + os.sep) for other in paths)`
    is O(n^2), and n is the number of matched cache directories -- 6,001 on a
    tree of Python packages, so 36 million string comparisons and 7.9 seconds,
    a fifth of the whole run. This asserts the shape that made it O(n * depth):
    a flat set of siblings must not become quadratic.

    Timing is not asserted -- a shared node makes that flaky. What is asserted is
    that the answer is right at a size where the old form would be visibly slow,
    which is the part that has to keep holding.
    """
    res = walkmod.WalkResult("/scratch/pkgs")
    # 4,000 siblings, none nested in another: the worst case for the old form and
    # the case a package tree actually produces.
    for i in range(4000):
        res.watched["/scratch/pkgs/p%04d/__pycache__" % i] = (4096, 2)
    groups = {p: hits for p, _cmd, hits in report.reclaimable_groups(res)}
    assert len(groups["__pycache__"]) == 4000


def test_a_reclaim_match_inside_another_is_still_dropped():
    """The bytes of a nested match are already inside its parent's total."""
    res = walkmod.WalkResult("/scratch/n")
    res.watched = {
        "/scratch/n/env/lib/__pycache__": (8192, 2),
        # Nested two levels down inside the one above: still nested.
        "/scratch/n/env/lib/__pycache__/deep/__pycache__": (4096, 2),
        "/scratch/n/other/__pycache__": (4096, 2),
    }
    kept = {p for _pat, _cmd, hits in report.reclaimable_groups(res) for _b, _i, p in hits}
    assert kept == {"/scratch/n/env/lib/__pycache__", "/scratch/n/other/__pycache__"}


def test_a_sibling_whose_name_merely_shares_a_prefix_is_not_nested():
    """`/a/bc` is not inside `/a/b`, and only a separator can tell them apart."""
    res = walkmod.WalkResult("/scratch/p")
    res.watched = {
        "/scratch/p/b/__pycache__": (4096, 2),
        "/scratch/p/bc/__pycache__": (4096, 2),
    }
    kept = {p for _pat, _cmd, hits in report.reclaimable_groups(res) for _b, _i, p in hits}
    assert len(kept) == 2, kept


def test_walking_up_to_the_filesystem_root_terminates():
    """`dirname` is its own fixed point at "/" -- the loop ends there, not never.

    A one-component path is the shortest walk that actually reaches the root, so
    it is the case that hangs if the loop trusts a sentinel instead of the fixed
    point. (`_reclaimable_match` needs a separator before the pattern, so a
    `__pycache__` sitting directly at `/` is not a match at all and cannot be
    used to test this.)
    """
    res = walkmod.WalkResult("/a")
    res.watched = {"/a/__pycache__": (4096, 2)}
    kept = {p for _pat, _cmd, hits in report.reclaimable_groups(res) for _b, _i, p in hits}
    assert kept == {"/a/__pycache__"}


def test_walking_up_a_relative_path_terminates_too():
    """`dirname("a")` is "", which is also its own fixed point.

    Reached by anything not rooted at `/` -- `os.path.relpath` output, or a walk
    of a bare directory name.
    """
    res = walkmod.WalkResult("a")
    res.watched = {"a/__pycache__": (4096, 2)}
    kept = {p for _pat, _cmd, hits in report.reclaimable_groups(res) for _b, _i, p in hits}
    assert kept == {"a/__pycache__"}


def test_the_default_thread_count_is_the_measured_optimum_not_the_cap():
    """They are different numbers on purpose: see `walk.MAX_THREADS`.

    16 is the best measured value on the slower of the two filesystems and 78% of
    the way to the best on the faster one. The cap is higher because GPFS keeps
    improving to a plateau at 24. A default equal to the cap would make `-t` a
    knob that can only go down.
    """
    assert walkmod.DEFAULT_THREADS == 16
    assert walkmod.MAX_THREADS == 24
    assert walkmod.DEFAULT_THREADS < walkmod.MAX_THREADS
    # And the serial choice sits below both, for filesystems where threads are a
    # cost rather than a saving. See `walk.choose_threads`.
    assert walkmod.LOCAL_THREADS < walkmod.DEFAULT_THREADS


def test_the_clamp_message_names_the_filesystem_it_measured(capsys):
    """It used to claim "32 threads was 31% worse than 16" without qualification.

    On GPFS 32 threads measured 10% *faster* than 16, and on a shared NFS export
    the run-to-run spread (10%) is wider than any difference thread count makes,
    so there is no single number to state. A figure that depends on the storage
    has to name the storage.
    """
    cli._warn_threads(walkmod.MAX_THREADS + 100)
    err = " ".join(capsys.readouterr().err.split())
    assert "clamped to {}".format(walkmod.MAX_THREADS) in err, err
    assert "31%" not in err, err
    assert "GPFS" in err, err


def test_the_nested_drop_agrees_with_the_form_it_replaced():
    """Differential, against the O(n^2) predicate, over random path sets.

    The ancestor walk is meant to be the *same* question asked cheaply, so the
    thing worth testing is that it answers identically -- not that it answers
    plausibly. It did not, at first: a set containing a bare `"/"` made every
    absolute path nested here and none of them nested there, because the old
    predicate compared against `other + os.sep` and for a root that is `"//"`.
    211 of 4,000 random sets hit it.

    Seeded, so a failure is reproducible rather than a story about one run.
    """
    import random

    def previous(paths):
        return {p for p in paths if not any(o != p and p.startswith(o + os.sep) for o in paths)}

    def current(paths):
        keep = set()
        for path in paths:
            res = walkmod.WalkResult("/")
            res.watched = {path: (4096, 2)}
            keep.add(path)
            del res
        # Drive the real function rather than a copy of it: one `WalkResult`
        # holding the whole set, so any future change to the dedup is covered.
        res = walkmod.WalkResult("/")
        res.watched = dict.fromkeys(paths, (4096, 2))
        return {p for _pat, _cmd, hits in report.reclaimable_groups(res) for _b, _i, p in hits}

    rng = random.Random(20260825)
    names = ["a", "b", "ab", "a b", "x.y", "c", "bc"]
    for _ in range(300):
        paths = set()
        for _ in range(rng.randint(1, 10)):
            depth = rng.randint(1, 4)
            parts = [rng.choice(names) for _ in range(depth)] + ["__pycache__"]
            root = rng.choice(["/", "", "/tmp/"])
            candidate = (root + "/".join(parts)).replace("//", "/")
            paths.add(candidate)
        # Only paths the matcher actually recognises can reach the dedup, so the
        # comparison is made over exactly those.
        matched = {p for p in paths if report._reclaimable_match(p)}
        assert current(paths) == previous(matched), sorted(paths)


def test_a_bare_root_in_the_set_is_not_everyones_ancestor():
    """The exact shape the differential run found, pinned on its own.

    Reachable only if `_reclaimable_match` ever returns `"/"`; the guard is here
    so that becoming reachable is not also becoming wrong.
    """
    assert report._reclaimable_match("/") is None, "the precondition the guard backs up"
    paths = {"/", "/tmp/__pycache__"}
    kept = set()
    for path in paths:
        current, nested = path, False
        while True:
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
            if current in paths and current not in ("/", ""):
                nested = True
                break
        if not nested:
            kept.add(path)
    assert kept == paths, "a root member must not swallow every absolute path"
