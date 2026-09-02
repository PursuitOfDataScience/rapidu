"""The quota layer, and the terminal, on a *second* cluster.

Every test here is a finding from running the released package on midway2 --
CentOS 7.9, Python 3.14, fileset-level GPFS quotas, a broken ``quota`` wrapper,
and three clusters' scratch filesystems under one ``/scratch``. Nothing in it is
midway2-specific: each is a shape that any site can have, and on which the tool
was either blind (no quota at all, on a cluster where the quota is readable) or
confidently wrong (a walk reconciled against another cluster's quota, a
fabricated table row).

The mount table below is midway2's, trimmed: seven GPFS devices, and the
``/scratch/<cluster>`` layout that makes ``/scratch`` a directory rather than a
mount point.
"""

import argparse
import io
import os
import sys

import pytest

from rapidu import cli, report, ui
from rapidu import quota as Q
from rapidu import reconcile as rc
from rapidu import walk as walkmod

MIDWAY2_MOUNTS = """\
proc /proc proc rw,relatime 0 0
midway2_perf2 /home gpfs rw,relatime 0 0
midway2_perf2 /software gpfs rw,relatime 0 0
midway2_cap /project2 gpfs rw,relatime 0 0
midway2_perf /scratch/midway2 gpfs rw,relatime 0 0
midway3_perf /scratch/midway3 gpfs rw,relatime 0 0
beagle3_perf /scratch/beagle3 gpfs rw,relatime 0 0
dali_cap /dali gpfs rw,relatime 0 0
cds1 /cds1 ceph rw,relatime 0 0
"""

# What `mmlsquota -Y -u <user> <device>` actually prints on midway2. The header
# spells the fileset column in lower case, which is why the parser is keyed
# case-insensitively.
MM_HEADER = (
    "mmlsquota:user:HEADER:version:reserved:reserved:filesystemName:quotaType:id:"
    "name:blockUsage:blockQuota:blockLimit:blockInDoubt:blockGrace:filesUsage:"
    "filesQuota:filesLimit:filesInDoubt:filesGrace:remarks:filesetname:"
)
MM_HOME = (
    "mmlsquota:user:0:1:::midway2_perf2:USR:940740146:youzhi:1916224:31457280:"
    "36700160:0:none:23230:300000:1000000:0:none::home:"
)

# What bare `mmlsquota -Y` prints where there is no default quota-enabled
# filesystem -- three lines of diagnostics, on stderr, and **exit status 0**.
MM_NO_DEFAULT_ERR = (
    "No quota enabled file system found.\n"
    "mmlsquota: tslsquota  -Y  failed. Error code 22.\n"
    "mmlsquota: Command failed. Examine previous error messages to determine cause.\n"
)

# The site `quota` wrapper: it exists, it runs, and the command inside it does not.
BROKEN_WRAPPER_ERR = "/software/bin/quota: line 3: /srv/adm/gpfsquota: No such file or directory\n"


def _walk_of(size=1000, inodes=10, root="/mnt/quota"):
    """A minimal walk result to reconcile against.

    Built here rather than imported from ``tests.test_reconcile``. That import
    resolved only under ``python -m pytest``, which puts the working directory on
    ``sys.path``; under a bare ``pytest`` -- how most CI invokes it -- these two
    tests failed with ``ModuleNotFoundError``. A suite whose verdict depends on
    how it was invoked has the same defect as one that depends on the host it ran
    on, which is RD-10, so it is fixed the same way: state what you need.
    """
    res = walkmod.WalkResult(root)
    res.size = size
    res.files = inodes - 1
    res.dirs = 1
    res.by_uid = {os.getuid(): (size, inodes)}
    res.by_dev = {42: (size, inodes)}
    return res


def _settled():
    """A settle check that ran, with a gap long enough for a null result to mean something."""
    check = walkmod.SettleCheck()
    check.ran = True
    check.gap = 60.0
    return check


def _plain():
    return ui.resolve_style("never")


def _no_deleted():
    from rapidu.deleted import DeletedScan

    return DeletedScan()


@pytest.fixture
def midway2_mounts(monkeypatch, tmp_path):
    """Point every mount-table read in the module at midway2's /proc/mounts."""
    path = tmp_path / "mounts"
    path.write_text(MIDWAY2_MOUNTS)
    real = Q._mount_entries
    monkeypatch.setattr(Q, "_mount_entries", lambda p="/proc/mounts": real(str(path)))
    return path


# --------------------------------------------------------------------------
# RD-1 -- `mmlsquota -Y` with no device answers for nothing at a per-fileset site
# --------------------------------------------------------------------------


def test_mmlsquota_falls_back_to_naming_each_gpfs_device(midway2_mounts, monkeypatch):
    """The bare call fails; the same query with a device attached succeeds.

    This is the whole of RD-1: rapidu had the device list in hand (it reads the
    mount table on the line above) and asked for "the default quota-enabled
    filesystem", which this cluster does not have. The user's home quota -- 1.83
    GiB of 30 GiB, 23,230 of 300,000 files -- was readable the entire time.
    """
    asked = []

    def fake_run(cmd, timeout):
        asked.append(cmd)
        if cmd == ["mmlsquota", "-Y"]:
            return 0, "", MM_NO_DEFAULT_ERR
        if cmd[-1] == "midway2_perf2":
            return 0, MM_HEADER + "\n" + MM_HOME + "\n", ""
        return 0, "", MM_NO_DEFAULT_ERR

    monkeypatch.setattr(Q, "_run", fake_run)
    snap = Q.read_mmlsquota("/home/youzhi")

    assert snap.available, "the quota this cluster publishes must be read"
    assert asked[0] == ["mmlsquota", "-Y"], "the one-call form is still tried first"
    assert any("-u" in cmd for cmd in asked), "and a device is named when it fails"
    blocks = [r for r in snap.rows if r.kind == "blocks"]
    assert blocks[0].used == 1916224 * 1024
    assert blocks[0].soft == 31457280 * 1024
    files = [r for r in snap.rows if r.kind == "files"]
    assert files[0].used == 23230 and files[0].soft == 300000
    assert snap.rows_for_path("/home/youzhi/data"), "and it must map the path walked"


# Round 8 measured the fix against midway2's own authoritative tool
# (`/project2/rcc/rupat/bin/quota.py`) and the figures agreed exactly. They are
# the fixture here, so a future change to the merge has to keep agreeing with a
# real cluster's real quota rather than with a plausible-looking number.
#
# blockUsage and blockQuota are KiB, which is why each is a byte figure over 1024.
ROUND8_DEVICES = {
    "midway2_perf2": (1962213376 // 1024, 32212254720 // 1024, 23232, 300000, "home"),
    "midway2_perf": (0, 107374182400 // 1024, 0, 10000000, "scratch"),
    "midway3_perf": (23682351104 // 1024, 107374182400 // 1024, 3666, 10000000, "scratch"),
    "beagle3_perf": (0, 429496729600 // 1024, 1, 5120000, "scratch"),
    "midway2_cap": (2420096, 0, 10, 0, "project2-rcc"),
    "dali_cap": (100, 0, 1, 0, "dali-rcc"),
}


def _mm_record(device):
    used, soft, files, file_soft, fileset = ROUND8_DEVICES[device]
    return (
        "mmlsquota:user:0:1:::{d}:USR:940740146:youzhi:{u}:{s}:{s}:0:none:"
        "{f}:{fs}:{fs}:0:none::{name}:"
    ).format(d=device, u=used, s=soft, f=files, fs=file_soft, name=fileset)


def test_every_gpfs_device_is_merged_into_one_snapshot(midway2_mounts, monkeypatch):
    """Seven devices, one reading -- and every row named by its own fileset.

    Round 8 validated the mount half of this: taken from ``/proc/mounts`` by
    device name, nothing is guessed, so RD-3's mis-attribution cannot happen
    here at all. A walk of ``/scratch/midway3`` reaches midway3's quota and only
    midway3's, on a login node that mounts all three clusters.

    RD-18 is the other half. Every row used to be *named* after its device,
    because `filesetName` was read only on ``scope == "fileset"`` rows and the
    per-device fan-out asks for ``-u <user>``, which returns ``USR`` rows and
    nothing else. So the name was applied only on rows this code never fetches.
    Now the fileset names the row and the device is a field of its own.
    """
    asked = []

    def fake_run(cmd, timeout):
        asked.append(cmd)
        if cmd == ["mmlsquota", "-Y"]:
            return 0, "", MM_NO_DEFAULT_ERR
        return 0, MM_HEADER + "\n" + _mm_record(cmd[-1]) + "\n", ""

    monkeypatch.setattr(Q, "_run", fake_run)
    snap = Q.read_mmlsquota("/scratch/midway3/youzhi")

    assert snap.available
    assert sorted(c[-1] for c in asked[1:]) == sorted(ROUND8_DEVICES)
    assert not any(r.guessed for r in snap.rows), "a device name is not a guess"

    home = next(r for r in snap.rows if r.fileset == "home" and r.kind == "blocks")
    assert home.device == "midway2_perf2", "the device survives as its own field"
    assert home.used == 1962213376, "cross-checked against the site's own tool"
    assert home.soft == 32212254720, "exactly 30 GiB"
    # `/home`, not `["/home", "/software"]`. Two filesets share this device and
    # the first entry of its mount list was right for only one of them -- which
    # is how 12.8 GiB that lives in /software came to be shown against /home.
    assert home.mounts == ["/home"], "the fileset's mount, not the device's list"
    assert home.label == "midway2_perf2:home", (
        "qualified even though `home` is unique in THIS row set -- see the "
        "host-independence test below"
    )
    files = next(r for r in snap.rows if r.fileset == "home" and r.kind == "files")
    assert (files.used, files.soft) == (23232, 300000)

    # A fileset name is unique inside a filesystem and not across one: three
    # clusters each call theirs `scratch`, so the bare name identifies nothing
    # and the label carries the device.
    scratch = [r for r in snap.rows if r.fileset == "scratch"]
    assert len(scratch) == 6, "three devices, blocks and files each"
    assert all(r.ambiguous_fileset for r in scratch)
    assert {r.label for r in scratch} == {
        "midway2_perf:scratch",
        "midway3_perf:scratch",
        "beagle3_perf:scratch",
    }

    # The RD-3 hazard, still closed: three clusters under one /scratch.
    matched = snap.rows_for_path("/scratch/midway3/me")
    assert [r.device for r in matched] == ["midway3_perf", "midway3_perf"]
    assert {r.label for r in matched} == {"midway3_perf:scratch"}
    assert {r.device for r in snap.rows_for_path("/scratch/midway2/me")} == {"midway2_perf"}
    assert snap.rows_for_path("/scratch") == [], "/scratch is a directory, not a filesystem"


def test_a_non_gpfs_mount_is_not_asked(midway2_mounts, monkeypatch):
    """`cds1` is ceph. `mmlsquota` has nothing to say about it."""
    asked = []

    def fake_run(cmd, timeout):
        asked.append(cmd)
        return 0, "", MM_NO_DEFAULT_ERR

    monkeypatch.setattr(Q, "_run", fake_run)
    Q.read_mmlsquota("/cds1")
    assert not any("cds1" in cmd for cmd in asked)


def test_the_default_filesystem_answering_stops_the_fan_out(midway2_mounts, monkeypatch):
    """A site that has a default is one call, exactly as before."""
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        return 0, MM_HEADER + "\n" + MM_HOME + "\n", ""

    monkeypatch.setattr(Q, "_run", fake_run)
    snap = Q.read_mmlsquota("/home/youzhi")
    assert snap.available
    assert calls == [["mmlsquota", "-Y"]], "seven more calls would re-read one answer"


def test_the_device_fan_out_is_capped_and_shares_one_deadline(midway2_mounts, monkeypatch):
    """A cluster with many filesystems must not turn one read into many minutes."""
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        return 0, "", MM_NO_DEFAULT_ERR

    monkeypatch.setattr(Q, "_run", fake_run)
    snap = Q.read_mmlsquota("/home/youzhi")
    assert not snap.available
    assert len(calls) <= Q._MAX_MMLSQUOTA_DEVICES + 1
    # One device per GPFS *device*, not per mount: midway2_perf2 is mounted twice.
    assert len({tuple(c) for c in calls}) == len(calls), "no device asked twice"


BIG_SITE_MOUNTS = "proc /proc proc rw 0 0\n" + "".join(
    "fs{:02d} /gpfs/fs{:02d} gpfs rw 0 0\n".format(n, n) for n in range(20)
)


def test_the_filesystem_holding_the_walked_path_is_asked_before_the_cap_binds(
    monkeypatch, tmp_path
):
    """A cap may bound cost. It may not decide correctness by alphabet.

    midway2 has seven GPFS devices and the cap is eight, so this shape is not on
    that cluster -- but a site with twenty is ordinary, and there alphabetical
    order would decide by luck whether the walked path's own filesystem was
    queried at all. Reporting "no quota" about a filesystem nobody asked is the
    same class of wrong answer as RD-1 itself.
    """
    path = tmp_path / "mounts"
    path.write_text(BIG_SITE_MOUNTS)
    real = Q._mount_entries
    monkeypatch.setattr(Q, "_mount_entries", lambda p="/proc/mounts": real(str(path)))

    asked = []

    def fake_run(cmd, timeout):
        asked.append(cmd)
        if cmd == ["mmlsquota", "-Y"]:
            return 0, "", MM_NO_DEFAULT_ERR
        if cmd[-1] == "fs17":
            return 0, MM_HEADER + "\n" + MM_HOME.replace("midway2_perf2", "fs17") + "\n", ""
        return 0, "", MM_NO_DEFAULT_ERR

    monkeypatch.setattr(Q, "_run", fake_run)
    snap = Q.read_mmlsquota("/gpfs/fs17/me/data")

    assert asked[1][-1] == "fs17", "the device that holds the path leads"
    assert len(asked) <= Q._MAX_MMLSQUOTA_DEVICES + 1, "and the cap still bounds the cost"
    assert snap.available, "so the quota that governs this walk is read"
    assert snap.rows_for_path("/gpfs/fs17/me/data")


def test_a_cap_that_binds_is_said_out_loud(monkeypatch, tmp_path):
    """A bound that silently drops filesystems reads as 'this site has no quota'."""
    path = tmp_path / "mounts"
    path.write_text(BIG_SITE_MOUNTS)
    real = Q._mount_entries
    monkeypatch.setattr(Q, "_mount_entries", lambda p="/proc/mounts": real(str(path)))
    monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (0, "", MM_NO_DEFAULT_ERR))

    snap = Q.read_mmlsquota("/gpfs/fs03")
    assert not snap.available
    assert "8 of 20 GPFS filesystems were asked" in snap.reason
    assert "fs03" in snap.reason, "and which one led"


def test_a_cap_that_does_not_bind_says_nothing(midway2_mounts, monkeypatch):
    """Seven devices under a cap of eight is not a caveat."""
    monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (0, "", MM_NO_DEFAULT_ERR))
    assert "were asked" not in Q.read_mmlsquota("/home").reason


def test_a_missing_mmlsquota_is_not_asked_once_per_device(midway2_mounts, monkeypatch):
    """127 on the first call settles it for all of them."""
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        return 127, "", "command not found"

    monkeypatch.setattr(Q, "_run", fake_run)
    monkeypatch.setattr(Q.shutil, "which", lambda cmd: None)
    snap = Q.read_mmlsquota("/home")
    assert not snap.available
    assert len(calls) == 1
    assert "not on PATH" in snap.reason


def test_only_gpfs_devices_are_asked(midway2_mounts, monkeypatch):
    """`mmlsquota` is a GPFS command; a ceph mount is not a GPFS filesystem."""
    assert "cds1" not in Q._devices_of_type(("gpfs",), str(midway2_mounts))
    assert "midway2_perf2" in Q._devices_of_type(("gpfs",), str(midway2_mounts))


# --------------------------------------------------------------------------
# RD-2 -- exit 127 from a wrapper is not a missing command
# --------------------------------------------------------------------------


def test_a_broken_wrapper_is_reported_as_what_it_is(monkeypatch):
    monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (127, "", BROKEN_WRAPPER_ERR))
    monkeypatch.setattr(Q.shutil, "which", lambda cmd: "/software/bin/quota")
    snap = Q.read_quota_command()
    assert "not on PATH" not in snap.reason
    assert "/srv/adm/gpfsquota" in snap.reason


def test_an_absent_command_is_still_reported_as_absent(monkeypatch):
    monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (127, "", "command not found"))
    monkeypatch.setattr(Q.shutil, "which", lambda cmd: None)
    assert "not on PATH" in Q.read_quota_command().reason


# --------------------------------------------------------------------------
# RD-3 -- a header with no mount clause, and a guess that must not be confident
# --------------------------------------------------------------------------

NO_MOUNT_CLAUSE = """
Quota information updated at :  2026-08-22 14:00:00
fileset          type                   used      quota      limit    grace
---------------- ---------------- ---------- ---------- ---------- --------
>>> Capacity Filesystem: project2 (GPFS)
---------------- ---------------- ---------- ---------- ---------- --------
rcc              blocks (group)      198.56T    500.49T    501.49T     none
rcc              files  (group)     51981387  384875000  385875000     none
"""


def test_a_header_with_no_mount_clause_still_maps_its_rows(midway2_mounts, monkeypatch):
    """`(GPFS)` names the filesystem; /proc/mounts turns that into a mount point."""
    monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (0, NO_MOUNT_CLAUSE, ""))
    snap = Q.read_quota_command()
    assert snap.available
    assert snap.rows[0].mount == "/project2"
    assert snap.rows_for_path("/project2/rcc/someone")
    assert snap.rows[0].guessed, "resolved from a name is an inference, and says so"


def test_a_published_mount_clause_is_not_an_inference(monkeypatch):
    published = NO_MOUNT_CLAUSE.replace(
        "project2 (GPFS)", "project2 (Midway2 GPFS mounted at /project2)"
    )
    monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (0, published, ""))
    snap = Q.read_quota_command()
    assert snap.rows[0].mount == "/project2"
    assert not snap.rows[0].guessed


def test_a_fileset_name_never_resolves_to_a_directory_that_is_not_a_mount(midway2_mounts):
    """The heart of RD-3: `/scratch` exists, holds three clusters, and is not a mount.

    Returning it made ``rows_for_path("/scratch/midway3/$USER")`` match midway2's
    scratch quota -- one row, no collision, nothing to disambiguate, and a
    confidently wrong answer.
    """
    points = Q._mount_points(str(midway2_mounts))
    assert Q._guess_mount("scratch", points) is None
    assert Q._guess_mount("scratch/midway2", points) == "/scratch/midway2"


def test_home_resolves_to_the_mount_not_to_one_users_directory(midway2_mounts):
    points = Q._mount_points(str(midway2_mounts))
    assert Q._guess_mount("Midway2-home", points) == "/home"


def test_no_mount_table_at_all_falls_back_rather_than_dropping_every_row(tmp_path):
    """With no /proc there is no evidence either way, and rows still map."""
    assert Q._guess_mount("home", []) is not None


def test_an_inferred_mount_cannot_produce_an_unexplained_gap():
    """RD-3, third part: a guessed mapping is a blocker, not a finding.

    A gap computed across the wrong mount is a fabricated finding, and the
    arithmetic looks perfectly sound while it is wrong.
    """

    def snap_with(guessed):
        row = Q.QuotaRow("scratch", "blocks", "user", 50_000_000_000, None, None, "", "/scratch/x")
        row.guessed = guessed
        snap = Q.QuotaSnapshot("quota -s")
        snap.rows = [row]
        snap.available = True
        snap.taken_at = snap.read_at
        return snap

    walk = _walk_of(1000, root="/scratch/x")
    published = rc.reconcile(walk, _settled(), snap_with(False), _no_deleted(), "blocks")
    inferred = rc.reconcile(walk, _settled(), snap_with(True), _no_deleted(), "blocks")
    assert published.verdict == rc.GAP
    assert inferred.verdict == rc.INCONCLUSIVE
    assert any("inferred" in b for b in inferred.blockers)
    assert inferred.candidates, "the hypotheses are still worth listing"


# --------------------------------------------------------------------------
# RD-4 -- a backend failure is one line, said once
# --------------------------------------------------------------------------


def test_a_multi_line_backend_failure_is_collapsed_to_one_line(midway2_mounts, monkeypatch):
    monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (0, "", MM_NO_DEFAULT_ERR))
    snap = Q.read_mmlsquota("/home")
    assert not snap.available
    assert "\n" not in snap.reason
    assert snap.reason.count("No quota enabled") == 1, "eight devices failing alike is one fact"


def test_the_quota_panel_keeps_its_right_border_on_a_long_reason():
    snap = Q.QuotaSnapshot("mmlsquota")
    snap.reason = (
        "quota -s: `quota` is on PATH but exited 127: /software/bin/quota: line 3: "
        "/srv/adm/gpfsquota: No such file or directory; mmlsquota: No quota enabled "
        "file system found. mmlsquota: Command failed.; lfs quota: `lfs` is not on PATH"
    )
    style = ui.resolve_style("never")
    style.width = 76
    lines = report.render_quota(snap, style=style)
    framed = ui.box(lines, style, width=80)
    assert len({ui.visible_width(line) for line in framed}) == 1
    assert all(line.startswith("│") or line[0] in "╭╰" for line in framed)


def test_the_reason_is_not_reprinted_once_per_reconciliation():
    snap = Q.QuotaSnapshot("mmlsquota")
    snap.available = False
    snap.reason = "mmlsquota: No quota enabled file system found. Error code 22."
    recs = [
        rc.reconcile(_walk_of(10), _settled(), snap, _no_deleted(), kind)
        for kind in ("blocks", "files")
    ]
    text = "\n".join(report.render_reconcile(recs, ui.resolve_style("never")))
    assert "Error code 22" not in text, "the QUOTA panel above has already said it"
    assert text.count("no quota backend available") == 2, "once per comparison, one line each"


# --------------------------------------------------------------------------
# RD-5 -- `mmlsquota` exits 0 while failing, so rc is not the success signal
# --------------------------------------------------------------------------


def test_diagnostics_on_stdout_are_not_parsed_as_records(midway2_mounts, monkeypatch):
    """The load-bearing half of today's guard is `not out.strip()`, which is luck.

    A build that writes these lines to stdout instead of stderr would have them
    fed to the record parser. Success is asserted positively instead: the `-Y`
    format always prints a `:HEADER:` line, and these diagnostics veto the output
    wherever they appear.
    """
    monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (0, MM_NO_DEFAULT_ERR, ""))
    snap = Q.read_mmlsquota("/home")
    assert not snap.available
    assert snap.rows == []
    assert "No quota enabled file system found" in snap.reason


def test_records_without_a_header_line_are_not_trusted(midway2_mounts, monkeypatch):
    monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (0, MM_HOME + "\n", ""))
    assert not Q.read_mmlsquota("/home").available


def test_a_nonzero_exit_with_good_records_is_still_read(midway2_mounts, monkeypatch):
    """The converse: content decides, so a grumpy exit status does not lose a quota."""
    monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (1, MM_HEADER + "\n" + MM_HOME + "\n", ""))
    assert Q.read_mmlsquota("/home").available


# --------------------------------------------------------------------------
# RD-6 -- a filename is not trusted input
# --------------------------------------------------------------------------


def test_a_newline_in_a_name_cannot_forge_a_table_row():
    forged = "fake\n│   999.9 GiB  ███  99.9%     1  TOTALLY-REAL-ENTRY"
    shown = ui.printable(forged)
    assert "\n" not in shown
    assert "\\x0a" in shown
    assert "TOTALLY-REAL-ENTRY" in shown, "and the real name is not truncated at the newline"


def test_an_escape_sequence_in_a_name_does_not_reach_the_terminal():
    assert "\x1b" not in ui.printable("clear\x1b[2Jgone.txt")
    # Even a well-formed SGR sequence, which is exactly the interesting case: it
    # is indistinguishable from ours once the line is composed, so it has to be
    # escaped where the name is still known to be a name.
    assert "\x1b" not in ui.printable("ansi\x1b[31mRED\x1b[0m.txt")


def test_our_own_colour_survives_the_line_level_backstop():
    """The backstop escapes what it did not put there, and only that."""
    style = ui.resolve_style("always")
    painted = style.paint("size", "bold")
    line = ui.sanitize_line(painted + "  name\x01here")
    assert painted in line, "our own SGR run is how anything is painted at all"
    assert "\x01" not in line
    assert "\\x01" in line


def test_a_control_character_is_measured_at_the_width_it_renders():
    """`\\x01` occupies no column; counting it as one put the row off by one."""
    assert ui.visible_width("ctrl\x01char") == len("ctrlchar")
    assert ui.visible_width(ui.printable("ctrl\x01char")) == len("ctrl\\x01char")


def test_json_still_carries_the_real_name(tmp_path):
    """A machine consumer needs the bytes on disk, and JSON escapes them itself."""
    import json

    from rapidu import walk as walkmod

    (tmp_path / "ctrl\x01char.txt").write_bytes(b"x")
    doc = json.loads(
        json.dumps(report.to_json(walkmod.walk(str(tmp_path)), None, None, None, None))
    )
    names = [e["path"] for e in doc["walk"]["top_by_size"]]
    assert any(n.endswith("ctrl\x01char.txt") for n in names), names


# --------------------------------------------------------------------------
# RD-7 -- a thread count of zero is not an instruction
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "-5", "abc"])
def test_a_nonsense_thread_count_is_rejected_not_repaired(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        cli._positive_int(bad)


def test_the_documented_upper_clamp_still_clamps():
    assert cli._positive_int("999") == 999
    from rapidu import walk as walkmod

    assert max(1, min(cli._positive_int("999"), walkmod.MAX_THREADS)) == walkmod.MAX_THREADS


# --------------------------------------------------------------------------
# RD-8 -- `-x` is a cap the user asked for, and it was applied in silence
# --------------------------------------------------------------------------


class _FakeStat(object):
    """A stat result with ``st_dev`` overridden and everything else real."""

    def __init__(self, wrapped, dev):
        self._wrapped = wrapped
        self.st_dev = dev

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


class _FakeEntry(object):
    """A ``DirEntry`` proxy that reports a different filesystem.

    Only ``stat`` is overridden; ``name``, ``path``, ``is_dir`` and ``inode``
    fall through, because the walk uses all of them and they must stay real.
    """

    def __init__(self, entry, dev):
        self._entry = entry
        self._dev = dev

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def stat(self, follow_symlinks=True):
        return _FakeStat(self._entry.stat(follow_symlinks=follow_symlinks), self._dev)


def _crossing_tree(tmp_path, monkeypatch):
    """A directory whose three children report a different ``st_dev``.

    Round 9 could finally test `-x` because midway2's `/scratch` is a plain
    directory holding three cluster filesystems. Faking `st_dev` at `scandir`
    reproduces that shape without root and without a second filesystem, which is
    what round 3 lacked when it had to record `-x` as untestable. Only `scandir`
    is patched: replacing `os.stat` wholesale breaks pathlib, and therefore
    pytest.
    """
    root = tmp_path / "scratch"
    (root / "local").mkdir(parents=True)
    (root / "local" / "f.txt").write_bytes(b"x")
    for name in ("midway2", "midway3", "beagle3"):
        (root / name).mkdir()
        (root / name / "data.bin").write_bytes(b"y" * 100)

    elsewhere = {str(root / n) for n in ("midway2", "midway3", "beagle3")}
    other_dev = os.lstat(str(root)).st_dev + 7
    real_scandir = os.scandir

    class _Scan(object):
        """`walk` uses `with scandir(d) as it`, so this keeps the context manager."""

        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._it.__exit__(*exc)

        def __iter__(self):
            for entry in self._it:
                yield _FakeEntry(entry, other_dev) if entry.path in elsewhere else entry

    monkeypatch.setattr(walkmod.os, "scandir", _Scan)
    return str(root)


@pytest.mark.parametrize("count_only", [False, True])
def test_a_cross_device_skip_is_counted_and_named(tmp_path, monkeypatch, count_only):
    """Both walk paths, because `-c` takes a different branch and skipped silently too."""
    root = _crossing_tree(tmp_path, monkeypatch)
    res = walkmod.walk(root, threads=2, depth=1, one_file_system=True, count_only=count_only)

    assert res.crossed == 3
    assert sorted(os.path.basename(p) for p in res.crossed_paths) == [
        "beagle3",
        "midway2",
        "midway3",
    ]
    # A requested scope is not a failure. Round 9 verified `complete=true` for a
    # `-x` walk and called it right; the disclosure must not change that.
    assert res.complete, "-x is a cap the user asked for, not something that went wrong"


def test_a_skipped_subtree_is_stated_in_the_report(tmp_path, monkeypatch):
    """`rdu -x /scratch` used to read as "/scratch is empty"."""
    root = _crossing_tree(tmp_path, monkeypatch)
    res = walkmod.walk(root, threads=2, depth=1, one_file_system=True)
    style = ui.resolve_style("never")
    text = "\n".join(report._hard_warnings(res, walkmod.SettleCheck(), style))

    assert "3 entries on other filesystems skipped (-x)" in text
    assert "midway2" in text, "the paths are what make the number actionable"
    for line in report._hard_warnings(res, walkmod.SettleCheck(), style):
        assert ui.visible_width(line) <= style.width, line


def test_no_skip_says_nothing(tmp_path, monkeypatch):
    """An ordinary walk gains no caveat."""
    root = _crossing_tree(tmp_path, monkeypatch)
    res = walkmod.walk(root, threads=2, depth=1, one_file_system=False)
    assert res.crossed == 0
    text = "\n".join(report._hard_warnings(res, walkmod.SettleCheck(), ui.resolve_style("never")))
    assert "skipped (-x)" not in text


def test_the_json_records_what_was_skipped(tmp_path, monkeypatch):
    """`filesystems` counts what was visited, which is a different question."""
    root = _crossing_tree(tmp_path, monkeypatch)
    res = walkmod.walk(root, threads=2, depth=1, one_file_system=True)
    doc = report.to_json(res, None, None, None, None)["walk"]
    assert doc["skipped_other_filesystem"] == 3
    assert len(doc["skipped_other_filesystem_paths"]) == 3
    assert doc["filesystems"] == 1, "unchanged: it never answered this question"


# --------------------------------------------------------------------------
# RD-9 -- the headline called inodes "files"
# --------------------------------------------------------------------------


def test_the_headline_count_is_called_what_it_is(tmp_path):
    """`files=7 dirs=1 inodes=8` printed as "8 files" -- in the one tool that exists
    to separate byte pressure from inode pressure."""
    (tmp_path / "sub").mkdir()
    for n in range(7):
        (tmp_path / "sub" / "f{}".format(n)).write_bytes(b"x")
    res = walkmod.walk(str(tmp_path), threads=2, depth=1)
    assert (res.files, res.dirs, res.inodes) == (7, 2, 9)

    header = report.render_compact(res, walkmod.SettleCheck(), 4, False, ui.resolve_style("never"))
    line = header[1]
    assert "9 inodes" in line
    assert "9 files" not in line


def test_the_headline_noun_survives_hard_links(tmp_path):
    """Round 11's known-answer fixture, which is RD-9's sharpest form.

    One 1 MiB file with four extra hard links, plus an independent 512 KiB file:
    ground truth is 6 names and 2 file inodes. The gap between the two is bigger
    than the directory count, so the box read **"3 files" for six files**. The
    figure is the right one to show -- an inode quota charges inodes -- and Round
    11 said as much; only the label was wrong.

    The accounting itself is certified correct by that round and pinned by
    `test_walk.py`; this test exists to keep the *label* honest about a number
    whose distance from `files` is this visible.
    """
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (1 << 20))
    for n in range(4):
        os.link(str(big), str(tmp_path / "link{}.bin".format(n)))
    (tmp_path / "solo.bin").write_bytes(b"y" * (1 << 19))

    res = walkmod.walk(str(tmp_path), threads=2, depth=1)
    assert (res.files, res.inodes, res.hardlink_extra_refs) == (6, 3, 4)

    line = report.render_compact(res, walkmod.SettleCheck(), 4, False, ui.resolve_style("never"))[1]
    assert "3 inodes" in line
    assert "3 files" not in line, "six names reported as three files"


def test_the_count_only_headline_does_not_claim_inodes(tmp_path):
    """`-c` cannot see hard links, so its total is names -- and it says so.

    `reconcile` already refuses to compare a `-c` count against a files quota for
    this reason; the label has to be consistent with that refusal.
    """
    (tmp_path / "a").write_bytes(b"x")
    os.link(str(tmp_path / "a"), str(tmp_path / "b"))
    res = walkmod.walk(str(tmp_path), threads=2, depth=1, count_only=True)
    line = report.render_compact(res, walkmod.SettleCheck(), 4, True, ui.resolve_style("never"))[1]
    assert "entries" in line
    assert "inode" not in line, "two names for one inode are two here and one to a quota"


def test_the_rate_carries_the_same_noun_as_the_count(tmp_path):
    """ "424 inodes ... 35,151 files/s" put both labels on one number."""
    (tmp_path / "f").write_bytes(b"x")
    res = walkmod.walk(str(tmp_path), threads=2, depth=1)
    text = "\n".join(
        report.render_walk(res, walkmod.SettleCheck(), 4, style=ui.resolve_style("never"))
    )
    assert "inodes/s" in text
    assert "files/s" not in text


def test_the_json_field_names_are_unchanged(tmp_path):
    """The labels moved; the document did not. `--json` was already correct."""
    (tmp_path / "f").write_bytes(b"x")
    res = walkmod.walk(str(tmp_path), threads=2, depth=1)
    doc = report.to_json(res, None, None, None, None)["walk"]
    assert (doc["files"], doc["dirs"], doc["inodes"]) == (1, 1, 2)


# --------------------------------------------------------------------------
# RD-10 -- the suite's own verdict must not depend on the host it runs on
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "midway2-login1.rcc.local",
        "midway3-0455.rcc.local",
        "otherclust-1.example.org",
        "node-01",
        "localhost",
    ],
)
def test_mount_disambiguation_obeys_the_same_rule_on_every_host(monkeypatch, host):
    """The generalisation of RD-10, so the class of bug cannot come back.

    A test that asserts one host's *outcome* is a test that fails when the host
    changes -- which is how the suite came to be red on midway2 and green on
    midway3 with identical code. What is actually true everywhere is the rule:
    a contested guessed mount is kept by the row the hostname names, and by no
    row at all when it names none. That holds on every host below, and the
    parametrisation is the guard: adding a host here must not require touching
    an assertion.
    """
    monkeypatch.setattr(Q.socket, "gethostname", lambda: host)
    tokens = Q._host_tokens()
    rows = [
        Q.QuotaRow("midway2-home", "blocks", "user", 1, None, None, "", "/home/x", True),
        Q.QuotaRow("otherclust-home", "blocks", "user", 2, None, None, "", "/home/x", True),
    ]
    Q._disambiguate_mounts(rows)
    kept = [r for r in rows if r.mount == "/home/x"]
    matching = [r for r in rows if Q._name_matches_host(r.fileset, tokens)]

    if len(matching) == 1:
        assert kept == matching, "the host names one of them, so that one keeps it"
    else:
        assert kept == [], "nothing to tell them apart, so neither may claim it"
        assert all(r.mount_note for r in rows), "and each says why it is unmapped"


def test_the_hostname_is_the_only_thing_the_suite_reads_from_the_machine(monkeypatch):
    """Pinning the hostname alone must make the quota layer's decisions fixed.

    RD-10's cost was not the one red test -- it was that "gates green" became a
    host-dependent claim, so nobody verifying rapidu on a new cluster could tell
    a package bug from their own environment. This asserts the surface is that
    narrow: with the hostname pinned, disambiguation is decided by the fileset
    names and nothing else.
    """
    monkeypatch.setattr(Q.socket, "gethostname", lambda: "midway2-login1.rcc.local")
    outcomes = set()
    for _ in range(3):
        rows = [
            Q.QuotaRow("midway2-home", "blocks", "user", 1, None, None, "", "/home/x", True),
            Q.QuotaRow("otherclust-home", "blocks", "user", 2, None, None, "", "/home/x", True),
        ]
        Q._disambiguate_mounts(rows)
        outcomes.add(tuple(r.mount for r in rows))
    assert len(outcomes) == 1
    assert outcomes == {("/home/x", None)}, "on a midway2 host, midway2-home wins -- correctly"


# --------------------------------------------------------------------------
# Self-audit: the Lustre backend, which no cluster in this campaign has run
# --------------------------------------------------------------------------
#
# Both clusters tested here are GPFS, so `read_lfs_quota` has never seen real
# output from either. Driving it with the shapes `lfs quota` actually prints
# found two defects of exactly the kind the filed findings are about.

LFS_BRACKETED = """Disk quotas for usr me (uid 1000):
     Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace
      /lustre01 [1048576] 2097152 4194304       - [1000] 5000 10000       -
Some errors happened when getting quota info. Some devices may be not working \
or deactivated. The data in "[]" is inaccurate.
"""

LFS_CLEAN = """Disk quotas for usr me (uid 1000):
     Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace
      /lustre01 1048576 2097152 4194304       - 1000 5000 10000       -
"""


def test_an_unverified_lustre_figure_is_read_not_dropped():
    """A bracketed figure made the row vanish, so a readable quota read as none.

    When `lfs quota` cannot reach an OST it brackets the figure it could not
    verify. `int("[1048576]")` raises, the row was discarded, and the user was
    told "could not parse `lfs quota` output" -- the RD-1 failure mode on a
    different backend: a quota that is right there, reported as absent.
    """
    rows = Q._parse_lfs_rows(LFS_BRACKETED, "user", "/lustre01/me")
    assert [r.used for r in rows] == [1048576 * 1024, 1000]


def test_lustre_saying_its_own_figures_are_inaccurate_is_not_discarded():
    """`lfs` disowns the numbers it just printed, and that has to survive.

    This is RD-2's root cause on the Lustre path: the backend explains itself and
    the explanation is thrown away. Reporting a disowned figure with no caveat is
    worse than reporting nothing, because the whole tool is built on the idea
    that a quota reading carries doubt.
    """
    # No subprocess is reached below -- these are pure functions. The guard used to
    # be `Q._run = None`, a bare module assignment with no `monkeypatch`, which
    # leaked into every test that ran after it: in reverse collection order it
    # broke three unrelated tests with `TypeError: 'NoneType' object is not
    # callable`. A test that changes global state without undoing it is the same
    # class of defect as RD-10 -- a result that depends on something other than
    # the code.
    snap = Q.QuotaSnapshot("lfs quota")
    assert snap.figure_note == "", "clean by default"

    assert Q._lfs_unverified(LFS_BRACKETED)
    assert not Q._lfs_unverified(LFS_CLEAN), "a healthy filesystem gains no caveat"


@pytest.mark.parametrize("text,expect_note", [(LFS_BRACKETED, True), (LFS_CLEAN, False)])
def test_the_doubt_reaches_the_report_and_the_verdict(monkeypatch, text, expect_note):
    """A figure the backend disowned may not produce a confident gap."""
    monkeypatch.setattr(
        Q,
        "_run",
        lambda cmd, t: (1, "", "") if cmd[:2] == ["lfs", "project"] else (0, text, ""),
    )
    snap = Q.read_lfs_quota("/lustre01/me")
    assert snap.available, "the figures are read either way"
    assert bool(snap.figure_note) is expect_note

    style = ui.resolve_style("never")
    panel = "\n".join(report.render_quota(snap, style=style))
    assert ("could not reach every device" in panel) is expect_note

    res = _walk_of(500_000_000, inodes=10, root="/lustre01")
    rec = rc.reconcile(res, _settled(), snap, _no_deleted(), "blocks")
    if expect_note:
        assert rec.verdict == rc.INCONCLUSIVE
        assert any("inaccurate" in b for b in rec.blockers)
    else:
        assert rec.verdict != rc.INCONCLUSIVE or not any("inaccurate" in b for b in rec.blockers)


def test_the_figure_doubt_is_separate_from_the_age_doubt():
    """Two different doubts with two different remedies: waiting fixes one only."""
    snap = Q.QuotaSnapshot("lfs quota")
    snap.time_note = "an age caveat"
    snap.figure_note = "a figure caveat"
    doc = report.to_json(None, None, snap, None, None)["quota"]
    assert doc["time_note"] == "an age caveat"
    assert doc["figure_note"] == "a figure caveat"


# --------------------------------------------------------------------------
# Self-audit: the stock `quota` layout, also unexercised by either cluster
# --------------------------------------------------------------------------


def test_no_quota_set_is_an_answer_not_a_parse_failure(monkeypatch):
    """`quota` prints `... : none` for an account with no quota anywhere.

    That was reported as "could not parse `quota -s` output" -- blaming a command
    that worked for a failure that did not happen, which is RD-2's mistake in a
    different place. For a tool whose question is "why is my quota full", "you
    have no quota" is a useful reply, and a wrong diagnosis sends the reader
    looking for a bug in the tool.
    """
    monkeypatch.setattr(
        Q, "_run", lambda cmd, t: (0, "Disk quotas for user me (uid 1000): none\n", "")
    )
    snap = Q.read_quota_command()
    assert not snap.available
    assert "no quota set" in snap.reason
    assert "could not parse" not in snap.reason


def test_genuine_gibberish_is_still_a_parse_failure(monkeypatch):
    """The negative case: the new message must not swallow a real parse problem."""
    monkeypatch.setattr(Q, "_run", lambda cmd, t: (0, "not a quota table at all\n", ""))
    assert "could not parse" in Q.read_quota_command().reason


@pytest.mark.parametrize(
    "header,figure,expected",
    [
        # `quota` (no -s) prints raw 1 KiB blocks; `quota -s` prints suffixes. The
        # header states which, and reading a `blocks` figure as bytes under-reports
        # by 1024x -- a 30 GiB home quota printed as 30 MiB.
        ("blocks", "1048576", 1048576 * 1024),
        ("space", "1024M", 1024 * (1 << 20)),
    ],
)
def test_the_stock_header_decides_the_unit(header, figure, expected):
    text = (
        "Disk quotas for user me (uid 1000):\n"
        "     Filesystem  {}   quota   limit   grace   files   quota   limit   grace\n"
        "      /dev/sda1 {} 2097152 4194304            1000    5000   10000\n"
    ).format(header, figure)
    blocks = [r for r in Q._parse_stock_quota(text) if r.kind == "blocks"]
    assert blocks and blocks[0].used == expected


# --------------------------------------------------------------------------
# Self-audit: a host with no readable /proc/mounts
# --------------------------------------------------------------------------


def test_no_mount_table_still_maps_rows_and_costs_no_extra_calls(monkeypatch):
    """A container or chroot has no mount table; the backends still have to work.

    Absent evidence is not evidence against a candidate, so `_guess_mount` falls
    back to its weaker test rather than dropping every row -- and the GPFS fan-out
    has no device list to enumerate, so it must not spend calls pretending it does.
    """
    monkeypatch.setattr(Q, "_mount_entries", lambda path="/proc/mounts": [])
    assert Q.read_mount_table() == {}
    assert Q._devices_of_type(("gpfs",)) == []

    site = (
        "Quota information updated at :  2026-08-23 08:00:00\n"
        "fileset          type                   used      quota      limit    grace\n"
        "Midway3-home     blocks (user)       767.34M     30.00G     35.00G     none\n"
    )
    monkeypatch.setattr(Q, "_run", lambda cmd, t: (0, site, ""))
    snap = Q.read_quota_command()
    assert snap.rows, "a row the backend read and parsed must not be dropped"
    assert all(r.guessed for r in snap.rows), "and it is honest that the mount is inferred"

    calls = []

    def counting(cmd, t):
        calls.append(cmd)
        return 0, "", "No quota enabled file system found.\n"

    monkeypatch.setattr(Q, "_run", counting)
    Q.read_mmlsquota("/home/me")
    assert calls == [["mmlsquota", "-Y"]], "no devices to name, so no calls to spend"


# --------------------------------------------------------------------------
# Self-audit: the settle window described an operation it had not observed
# --------------------------------------------------------------------------


def _tree_with(tmp_path, written=0, touched=0, future=0):
    """A tree whose files sit in chosen positions relative to the settle window.

    ``touched`` files get an ancient mtime and, unavoidably, a fresh ctime --
    which is exactly what `chmod -R`, a `chgrp`, or the utime pass at the end of
    `tar -x` produces on a tree nobody has written to.
    """
    import time

    for n in range(written):
        (tmp_path / "w{}.bin".format(n)).write_bytes(b"x" * 4096)
    for n in range(touched):
        p = tmp_path / "t{}.bin".format(n)
        p.write_bytes(b"x" * 4096)
        old = time.time() - 400 * 86400
        os.utime(str(p), (old, old))
    for n in range(future):
        p = tmp_path / "f{}.bin".format(n)
        p.write_bytes(b"x" * 4096)
        ahead = time.time() + 90
        os.utime(str(p), (ahead, ahead))
    return walkmod.walk(str(tmp_path), threads=2, depth=1)


def test_an_inode_change_is_not_reported_as_a_write(tmp_path):
    """`chmod -R` made the report say every file "was written" and was provisional.

    `st_ctime` moves for a permission change, an ownership change, a rename, a
    hard link -- none of which touch a block. Folding those into `recent_files`
    stated something false about the tree, in the section whose whole job is to
    say how much the headline number can be trusted.
    """
    res = _tree_with(tmp_path, touched=4)
    assert res.recent_files == 0, "nothing was written"
    assert res.touched_files == 4

    text = "\n".join(report.render_settle(res, walkmod.recheck_settling(res, 0.0), _plain()))
    assert "changed without being written" in text
    assert "4 files written" not in text


def test_an_inode_change_still_blocks_a_finding(tmp_path):
    """Firing was right; the wording was not.

    A delayed allocation completing bumps ctime alone and *does* move
    `st_blocks`, and a stat cannot tell that from a chmod. So the blocker stays
    and names both causes rather than asserting the write.
    """
    res = _tree_with(tmp_path, touched=4)
    snap = Q.QuotaSnapshot("t")
    snap.available = True
    snap.taken_at = snap.read_at
    snap.rows = [Q.QuotaRow("fs", "blocks", "user", 10**9, None, None, "", str(tmp_path))]
    rec = rc.reconcile(res, walkmod.recheck_settling(res, 0.0), snap, _no_deleted(), "blocks")
    assert any("changed without being written" in b for b in rec.blockers)
    assert not any("files were written" in b for b in rec.blockers)


def test_a_written_file_is_still_called_written(tmp_path):
    """The negative case: the real signal must not have been weakened."""
    res = _tree_with(tmp_path, written=3)
    assert res.recent_files == 3 and res.touched_files == 0
    text = "\n".join(report.render_settle(res, walkmod.recheck_settling(res, 0.0), _plain()))
    assert "3 files written" in text


def test_subject_verb_agreement_on_one_file(tmp_path):
    """`1 file were written` -- the agreement rule this package states once."""
    res = _tree_with(tmp_path, written=1)
    snap = Q.QuotaSnapshot("t")
    snap.available = True
    snap.taken_at = snap.read_at
    snap.rows = [Q.QuotaRow("fs", "blocks", "user", 10**9, None, None, "", str(tmp_path))]
    rec = rc.reconcile(res, walkmod.recheck_settling(res, 0.0), snap, _no_deleted(), "blocks")
    joined = " ".join(rec.blockers)
    assert "1 file was written" in joined
    assert "1 file were" not in joined


def test_a_future_mtime_is_named_as_a_clock_difference(tmp_path):
    """A timestamp ahead of this node's clock is inside any window forever.

    A client whose clock trails the fileserver's stamps every write in the
    future, so the tree reads as permanently just-written and the verdict is
    permanently provisional -- for a reason that is not true. It is an
    observation about the clock, and it says so.
    """
    res = _tree_with(tmp_path, future=3)
    assert res.future_files == 3
    assert res.recent_files == 3, "they are inside the window, which is the problem"

    # Collapsed, for the reason `_flat` exists: the note wraps, and "ahead of
    # this node's clock" is only a substring of the unwrapped sentence.
    text = " ".join(
        " ".join(report.render_settle(res, walkmod.recheck_settling(res, 0.0), _plain())).split()
    )
    assert "ahead of this node's clock" in text

    doc = report.to_json(res, walkmod.recheck_settling(res, 0.0), None, None, None)["settling"]
    assert doc["future_mtime_files"] == 3
    assert doc["touched_files"] == 0


def test_an_ordinary_tree_says_nothing_about_settling(tmp_path):
    """No caveat where there is nothing to caveat."""
    import time

    p = tmp_path / "quiet.bin"
    p.write_bytes(b"x" * 4096)
    old = time.time() - 400 * 86400
    os.utime(str(p), (old, old))
    # A zero window puts the cutoff at the walk's own `now`, which is after the
    # `utime` above -- so neither timestamp is inside it. (`utime` bumps ctime,
    # which is precisely the finding above, so an "old ctime" cannot be staged.)
    res = walkmod.walk(str(tmp_path), threads=2, depth=1, settle_window=0.0)
    assert res.recent_files == 0 and res.touched_files == 0
    assert report.render_settle(res, walkmod.recheck_settling(res, 0.0), _plain()) == []


# --------------------------------------------------------------------------
# RD-11 -- a reclaim command that named a directory it had not measured
# --------------------------------------------------------------------------


def _reclaim_tree(tmp_path, *relpaths):
    for rel in relpaths:
        d = tmp_path
        for part in rel.split("/"):
            d = d / part
        d.mkdir(parents=True)
        (d / "blob.bin").write_bytes(b"x" * 4096)
    return walkmod.walk(str(tmp_path), threads=2, depth=1)


def test_the_reclaim_command_names_the_directory_it_measured(tmp_path):
    """RD-11: `cache/torch` printed `rm -rf ~/.cache/torch` wherever it matched.

    Relocating caches out of `$HOME` is standard on a cluster with a small home
    quota, so a `cache/torch` outside `~` is the normal case. There the command
    deleted an unrelated cache *and* left the bytes it had just reported in
    place -- the one outcome a disk tool must never produce.
    """
    res = _reclaim_tree(tmp_path, "data/cache/torch")
    text = "\n".join(report.render_reclaimable(res, _plain()))
    assert "~/.cache/torch" not in text
    assert str(tmp_path / "data" / "cache" / "torch") in text


def test_a_reclaim_command_is_quoted_so_a_truncated_copy_cannot_run(tmp_path):
    """Half of an `rm -rf` is still a valid `rm -rf` -- unless it is quoted.

    Interpolating a real path makes the line long enough for the frame to wrap
    it, and the first line then read `rm -rf /project/rcc/user/.cache/tmp/...`:
    a directory seven levels above the intended one, complete and runnable.
    Quoting makes a partial copy carry an unterminated quote, so the shell waits
    for input instead. `shlex.quote` alone is not enough -- it leaves an ordinary
    path bare, which is exactly the case that wraps.
    """
    import subprocess

    cmd = report._shell_command("rm -rf {path}", "/project/rcc/me/.cache/tmp/deep/cache/torch")
    assert cmd.startswith("rm -rf '") and cmd.endswith("'")
    for cut in (30, 45, len(cmd) - 5):
        proc = subprocess.run(
            ["bash", "-n", "-c", cmd[:cut]], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        assert proc.returncode != 0, "a truncated {!r} must not be runnable".format(cmd[:cut])
    # And the whole command is valid.
    assert (
        subprocess.run(
            ["bash", "-n", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).returncode
        == 0
    )


@pytest.mark.parametrize(
    "path", ["/data/my cache/x", "/data/it's here/x", "/data/glob*[a]/x", "/data/$HOME/x"]
)
def test_a_path_is_not_a_shell_word(path):
    """Spaces, quotes, globs and `$` in a directory name are ordinary on a shared
    filesystem, and an unquoted path is a different command from the one meant."""
    import subprocess

    cmd = report._shell_command("rm -rf {path}", path)
    proc = subprocess.run(
        ["bash", "-c", "printf '%s' " + cmd.split(" ", 2)[2]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout.decode() == path, "the shell must see exactly the path measured"


def test_an_unprintable_path_gets_no_command_at_all():
    """An escaped path is not the path, and a raw one puts control bytes on the
    terminal. There is no correct one-liner, so none is offered."""
    assert report._shell_command("rm -rf {path}", "/data/ctrl\x01dir") == ""


def test_a_tool_that_locates_its_own_store_is_left_alone(tmp_path, monkeypatch):
    """`pip cache purge` needs no path, and must not acquire one.

    ``which`` is pinned because the claim is about the *template*, not about this
    host: `reclaim_command` prints a tool only where the tool runs, so on a login
    node whose system python ships no `pip` binary the correct output is the
    `rm -rf` fallback and this test failed for being right. Measured on a RHEL8
    cluster whose `/usr/bin/python3` has no `pip` module at all -- and the suite
    was green on two other clusters that happen to have one, which is the whole
    problem with asserting a machine.
    """
    monkeypatch.setattr(report.shutil, "which", lambda tool: "/usr/bin/" + tool)
    res = _reclaim_tree(tmp_path, "data/cache/pip")
    text = "\n".join(report.render_reclaimable(res, _plain()))
    assert "pip cache purge" in text
    assert "rm -rf" not in text


def test_several_matches_each_get_their_own_command(tmp_path):
    """One line cannot be correct for two directories."""
    res = _reclaim_tree(tmp_path, "a/cache/torch", "b/cache/torch")
    text = "\n".join(report.render_reclaimable(res, _plain()))
    assert text.count("rm -rf") == 2
    assert str(tmp_path / "a" / "cache" / "torch") in text
    assert str(tmp_path / "b" / "cache" / "torch") in text


# --------------------------------------------------------------------------
# Self-audit: one conclusion served a range it could not be true across
# --------------------------------------------------------------------------


def _tree_at_ratio(ratio, apparent=8 << 30, inodes=5100):
    res = walkmod.WalkResult("/data")
    res.apparent = apparent
    res.size = int(apparent * ratio)
    res.files = inodes - 100
    res.dirs = 100
    return res


@pytest.mark.parametrize("ratio", [0.86, 0.60, 0.30])
def test_a_real_byte_cost_is_not_called_nearly_free(ratio):
    """Transparent compression lands here, and the bytes are not free.

    The allocation section fires from ~0.87x downwards and printed "Bytes are
    nearly free here" across the whole range -- the same sentence at 0.60x, where
    the tree is charged most of its data size, as at 0.0001x, where it is charged
    nothing. A conclusion that reads identically at both ends distinguishes
    neither, and is false at one of them.
    """
    text = " ".join(report.render_allocation(_tree_at_ratio(ratio), _plain()))
    assert "nearly free" not in text
    assert "{:.0f}% of the data size".format(100.0 * ratio) in text
    assert "saving, not an error" in text, "it is still not an error"


@pytest.mark.parametrize("ratio", [0.25, 0.02, 0.0001])
def test_a_genuinely_free_ratio_still_says_so(ratio):
    """A sparse file sits at ~0.00x and inline files at ~0.02x; there it is true."""
    text = " ".join(report.render_allocation(_tree_at_ratio(ratio), _plain()))
    assert "nearly free" in text
    assert "inodes are the cost to watch" in text


def test_the_section_never_asserts_which_quota_binds():
    """It has no quota rows, so it cannot know -- RECONCILE does and says so.

    "N inodes are the cost that will run out first" named the binding limit
    without consulting either limit.
    """
    for ratio in (0.86, 0.60, 0.25, 0.02):
        text = " ".join(report.render_allocation(_tree_at_ratio(ratio), _plain()))
        assert "run out first" not in text


def test_padding_the_other_way_is_untouched(tmp_path):
    """The over-allocated branch is the motivating case and must not have moved."""
    res = walkmod.WalkResult("/data")
    res.apparent = 1 << 30
    res.size = 8 << 30
    res.files, res.dirs = 5000, 100
    # `" ".join(lines)` is not enough: the continuation of a wrapped warning
    # carries its own two-space hanging indent, so joining with a space put three
    # spaces mid-sentence and the substring stopped matching.
    text = " ".join(" ".join(report.render_allocation(res, _plain())).split())
    assert "8.0x" in text
    assert "Your quota is charged the first number." in text


# GPFS's `-Y` header is internally inconsistent on midway2: field 7 is
# `filesystemName` (camelCase) and the fileset column is `filesetname` (all
# lowercase). The fileset name is only *read* on the `scope == "fileset"` branch,
# so a USR-scoped fixture passes whichever spelling the code looks up -- which is
# why round 8's prototype could not have caught this, and why this fixture is
# FILESET-scoped.
MM_FILESET = (
    "mmlsquota:fileset:0:1:::midway2_cap:FILESET:0:project2-rcc:2420096:31457280:"
    "36700160:0:none:1000:300000:1000000:0:none::project2-rcc:"
)


def test_a_fileset_row_is_named_by_its_fileset_under_a_lowercase_header(
    midway2_mounts, monkeypatch
):
    """The branch the case-insensitive lookup exists for, with midway2's spelling.

    Reading `filesetName` from a header that says `filesetname` returns `""`, so
    every fileset-scoped row would be named after its *filesystem* instead -- two
    labs sharing one mount merged under one label, which is exactly what
    `filesetName` is there to prevent.
    """
    monkeypatch.setattr(Q, "_run", lambda cmd, t: (0, MM_HEADER + "\n" + MM_FILESET + "\n", ""))
    snap = Q.read_mmlsquota("/project2/rcc")
    assert snap.available
    assert {r.scope for r in snap.rows} == {"fileset"}
    assert {r.fileset for r in snap.rows} == {"project2-rcc"}, "named by fileset, not by filesystem"
    assert "midway2_cap" not in {r.fileset for r in snap.rows}


def test_the_lowercase_header_is_what_makes_that_test_meaningful():
    """Guard the guard: the fixture must actually use the lowercase spelling.

    If MM_HEADER ever gained camelCase, the test above would pass against an
    exact-case lookup and stop testing anything.
    """
    assert ":filesetname:" in MM_HEADER
    assert ":filesetName:" not in MM_HEADER


def test_the_command_cap_names_the_group_and_hides_nothing(tmp_path):
    """The cap was silent, and its message promised a listing that did not exist.

    Requested in the RD-11 feedback. With six matches the section printed three
    commands and "... and 3 more, listed below" -- and below was the `largest:`
    line, two examples, both already among the three commands above it. So three
    directories the reader has to act on appeared nowhere at all: a silent cap on
    the one surface where every entry matters.
    """
    for n in range(6):
        d = tmp_path / "r{}".format(n) / "cache" / "torch"
        d.mkdir(parents=True)
        # Far enough apart to survive block rounding: at 4 KiB steps every
        # directory allocated the same and the tiebreak decided the order.
        #
        # `fsync` per file, not a bare `os.sync()`: round 11's GPFS note applies
        # to this fixture, and read immediately after writing every directory
        # measures 512 B because `st_blocks` is still 0 -- the ordering assertion
        # below would then be testing the tiebreak instead. `os.sync()` alone did
        # not settle it here; the explicit flush does.
        with open(str(d / "blob.bin"), "wb") as handle:
            handle.write(b"x" * ((1 << 18) * (6 - n)))
            handle.flush()
            os.fsync(handle.fileno())
    res = walkmod.walk(str(tmp_path), threads=2, depth=1)
    text = "\n".join(report.render_reclaimable(res, _plain()))

    assert text.count("rm -rf") == report._RECLAIM_COMMAND_CAP
    assert "listed below" not in text, "it does not list them below"
    assert "3 more cache/torch" in text, "the group is named"
    assert "--json" in text, "and the remainder is reachable"

    # The three shown are the largest three, not merge order.
    shown = [line for line in text.split("\n") if "rm -rf" in line]
    assert "r0" in shown[0] and "r1" in shown[1] and "r2" in shown[2]

    # And the document carries every one of them.
    doc = report.to_json(res, None, None, None, None)["walk"]["reclaimable"]
    group = next(g for g in doc if g["pattern"] == "cache/torch")
    assert len(group["paths"]) == 6
    assert group["command"] == "rm -rf {path}"
    assert group["bytes"] == sum(h[0] for h in report.reclaimable_groups(res)[0][2])


def test_a_group_inside_the_cap_says_nothing_about_a_remainder(tmp_path):
    """Two matches is two commands and no caveat."""
    for n in range(2):
        d = tmp_path / "r{}".format(n) / "cache" / "torch"
        d.mkdir(parents=True)
        (d / "blob.bin").write_bytes(b"x" * 4096)
    res = walkmod.walk(str(tmp_path), threads=2, depth=1)
    text = "\n".join(report.render_reclaimable(res, _plain()))
    assert text.count("rm -rf") == 2
    assert "more cache/torch" not in text


def test_the_json_and_the_report_read_one_grouping(tmp_path):
    """`reclaimable_groups` exists so the two views cannot drift apart."""
    (tmp_path / "a" / "cache" / "pip").mkdir(parents=True)
    (tmp_path / "a" / "cache" / "pip" / "x.bin").write_bytes(b"x" * 4096)
    (tmp_path / "b" / "__pycache__").mkdir(parents=True)
    (tmp_path / "b" / "__pycache__" / "y.pyc").write_bytes(b"y" * 4096)
    res = walkmod.walk(str(tmp_path), threads=2, depth=1)

    groups = {p for p, _c, _h in report.reclaimable_groups(res)}
    doc = {g["pattern"] for g in report.to_json(res, None, None, None, None)["walk"]["reclaimable"]}
    assert groups == doc == {"cache/pip", "__pycache__"}


class _named(object):
    """A passwd/group entry stand-in with just the field the resolver reads."""

    def __init__(self, name):
        self.pw_name = name
        self.gr_name = name


# --------------------------------------------------------------------------
# Self-audit: a JSON join key that moved with the node it ran on
# --------------------------------------------------------------------------


def _uid_table(res, table):
    res.by_uid = table
    return report.to_json(res, None, None, None, None)["walk"]["by_uid"]


def test_the_owner_id_survives_a_node_that_cannot_resolve_it(tmp_path, monkeypatch):
    """`by_uid` was keyed by resolved name, and the name is not stable per node.

    `pwd.getpwuid` finds nothing on a compute node at some sites -- measured on
    midway2, and the reason slurmwatch's headline finding exists -- so the same
    tree serialised as `{"youzhi": ...}` from a login node and
    `{"940740146": ...}` from a batch job. A consumer joining two runs sees two
    owners where there is one, and joining runs is what `--json` is for.
    """
    (tmp_path / "f.bin").write_bytes(b"x" * 4096)
    res = walkmod.walk(str(tmp_path), threads=2, depth=1)

    resolved = _uid_table(res, {4242: (100, 2)})
    monkeypatch.setattr(report.pwd, "getpwuid", lambda uid: _named("alice"))
    resolved = _uid_table(res, {4242: (100, 2)})
    assert list(resolved) == ["alice"], "the human-readable key is unchanged"
    assert resolved["alice"]["uid"] == 4242, "and the stable identifier is present"

    def missing(uid):
        raise KeyError(uid)

    monkeypatch.setattr(report.pwd, "getpwuid", missing)
    unresolved = _uid_table(res, {4242: (100, 2)})
    assert list(unresolved) == ["4242"]
    assert unresolved["4242"]["uid"] == 4242, "the key moved with the node; the uid did not"
    # The join a consumer actually needs: same owner across both runs.
    assert {v["uid"] for v in resolved.values()} == {v["uid"] for v in unresolved.values()}


def test_two_ids_resolving_to_one_name_lose_nothing(tmp_path, monkeypatch):
    """The dict comprehension let one entry overwrite the other, silently."""
    (tmp_path / "f.bin").write_bytes(b"x" * 4096)
    res = walkmod.walk(str(tmp_path), threads=2, depth=1)
    monkeypatch.setattr(report.pwd, "getpwuid", lambda uid: _named("shared"))

    table = _uid_table(res, {1000: (10, 1), 2000: (20, 2)})
    assert len(table) == 2, "neither entry may vanish"
    assert {v["uid"] for v in table.values()} == {1000, 2000}
    assert sum(v["bytes"] for v in table.values()) == 30
    assert all("shared" in key for key in table)


def test_groups_get_the_same_treatment(tmp_path, monkeypatch):
    """A group quota is charged by gid, so the same argument applies to it."""
    (tmp_path / "f.bin").write_bytes(b"x" * 4096)
    res = walkmod.walk(str(tmp_path), threads=2, depth=1)
    res.by_gid = {7777: (50, 3)}

    def missing(gid):
        raise KeyError(gid)

    monkeypatch.setattr(report.grp, "getgrgid", missing)
    doc = report.to_json(res, None, None, None, None)["walk"]["by_gid"]
    assert doc["7777"]["gid"] == 7777


# --------------------------------------------------------------------------
# Self-audit: the exit code disagreed with stderr on a partial run
# --------------------------------------------------------------------------


def test_a_refused_path_is_not_a_successful_run(tmp_path, capsys):
    """`rdu <good> /nope` walked the good one, said so on stderr, and exited 0.

    This package is explicitly meant to be run from cron -- its own comment
    justifying INCONCLUSIVE -> EXIT_ATTENTION says "a caller checking an exit code
    wants to be told". A job asking about two trees, one of which has been deleted
    or unmounted, was told everything was fine having measured one of them. The
    all-rejected case already returned EXIT_ERROR for exactly this reason.
    """
    (tmp_path / "a.txt").write_bytes(b"x")
    rc_code = cli.main(
        [str(tmp_path), str(tmp_path / "gone"), "--no-progress", "--no-box", "--color", "never"]
    )
    out = capsys.readouterr()
    assert rc_code == cli.EXIT_ERROR
    assert "no such path" in out.err
    assert str(tmp_path) in out.out, "the measurable path is still reported"


def test_all_good_paths_still_exit_zero(tmp_path):
    """The negative case: nothing refused, nothing to report."""
    (tmp_path / "a.txt").write_bytes(b"x")
    assert cli.main([str(tmp_path), "--no-progress", "--no-box", "--color", "never"]) == cli.EXIT_OK


def test_a_refused_path_counts_even_when_others_need_attention(tmp_path, capsys):
    """EXIT_ERROR outranks EXIT_ATTENTION: the request was not fulfilled."""
    (tmp_path / "a.txt").write_bytes(b"x")
    assert cli.main([str(tmp_path), "/nonexistent-xyz", "-D", "--no-box"]) == cli.EXIT_ERROR
    capsys.readouterr()


def test_resolve_paths_reports_the_count_not_just_the_message(tmp_path):
    """The count is the return value because the exit code has to see it."""
    (tmp_path / "d").mkdir()
    resolved, refused = cli._resolve_paths([str(tmp_path / "d"), str(tmp_path / "nope")])
    assert resolved == [str(tmp_path / "d")]
    assert refused == 1


def test_the_skip_count_equals_the_paths_it_names(tmp_path, monkeypatch):
    """Requested in the RD-8 feedback: tie the two halves of the message together.

    The headline count and the path list were two independent statements, so a
    correct 4 shown as "3 paths + and 1 more" could not be checked by the reader
    and was read as a double-count. Now: listed + remainder == count, asserted.
    """
    root = _crossing_tree(tmp_path, monkeypatch)
    res = walkmod.walk(root, threads=2, depth=1, one_file_system=True)
    text = "\n".join(report._hard_warnings(res, walkmod.SettleCheck(), _plain()))

    # Paths are truncated keeping the tail, so match on the basename rather than
    # the root prefix -- a long path is the normal case here, not the exception.
    expected = {os.path.basename(p) for p in res.crossed_paths}
    named = [
        line.strip()
        for line in text.split("\n")
        if any(line.strip().endswith("/" + name) for name in expected)
    ]
    remainder = 0
    for line in text.split("\n"):
        if "... and" in line and "more" in line:
            remainder = int(line.split("and")[1].split("more")[0].strip())
    assert len(named) + remainder == res.crossed, text
    assert "{} entries".format(res.crossed) in text


def test_no_directory_is_counted_at_two_skip_sites(tmp_path, monkeypatch):
    """The awkward candidate raised in the feedback, ruled out by construction.

    Three skip sites exist (the `-c` fast path, and the directory and
    non-directory branches of the full path). If a child were counted at two of
    them the total would scale with sites rather than entries, and would inflate
    silently on exactly the trees where `-x` gets read. The count must equal the
    number of *distinct* paths.
    """
    root = _crossing_tree(tmp_path, monkeypatch)
    for count_only in (False, True):
        res = walkmod.walk(root, threads=2, depth=1, one_file_system=True, count_only=count_only)
        assert res.crossed == len(set(res.crossed_paths)) == 3, "count_only=%s" % count_only


def test_one_hidden_path_is_never_hidden(tmp_path, monkeypatch):
    """`... and 1 more` costs the same line as the path and hides the answer."""
    root = _crossing_tree(tmp_path, monkeypatch)
    # A fourth crossing child, so the default cap of three would hide exactly one.
    extra = os.path.join(root, "fourth")
    os.mkdir(extra)
    real = walkmod.os.scandir

    res = walkmod.walk(root, threads=2, depth=1, one_file_system=True)
    assert res.crossed == 3, "the fixture crosses on three; the fourth is same-device"
    del real, extra

    # Drive the renderer directly for the four-path case, which is the shape that
    # produced the confusion on a /scratch holding four mounts.
    res.crossed = 4
    res.crossed_paths = ["/scratch/a", "/scratch/b", "/scratch/c", "/scratch/d"]
    text = "\n".join(report._hard_warnings(res, walkmod.SettleCheck(), _plain()))
    assert "and 1 more" not in text
    for path in res.crossed_paths:
        assert path in text

    # Two or more hidden still earn the summary.
    res.crossed = 6
    res.crossed_paths = ["/scratch/%d" % n for n in range(6)]
    text = "\n".join(report._hard_warnings(res, walkmod.SettleCheck(), _plain()))
    assert "and 3 more" in text


def test_the_restat_sample_knows_the_population_it_was_drawn_from(tmp_path):
    """A regression I introduced when `recent_files` and `touched_files` split.

    `recent_sample` is filled for the *union* of written-recently and
    touched-recently, but its denominator stayed the written half, so
    `SettleCheck.sampled` compared the sample size against the wrong total and a
    truncated re-stat reported no truncation. The disclosure line
    "(re-stat covered N of M recent files)" then never printed -- a cap hiding
    itself, which is the defect this report has filed twice.
    """
    res = _tree_with(tmp_path, written=2, touched=3)
    check = walkmod.recheck_settling(res, 0.0)
    assert check.sampled_of == res.recent_files + res.touched_files == 5
    assert check.checked == 5, "every sampled file was re-stat'ed"
    assert not check.sampled, "nothing was truncated here"


def test_a_truncated_sample_says_so():
    """The shape that was invisible: mostly-touched population, capped sample."""
    check = walkmod.SettleCheck()
    check.sampled_of, check.checked, check.gone = 1000 + 9000, walkmod._RECENT_SAMPLE_CAP, 0
    assert check.sampled, "10,000 candidates and 4,096 re-stat'ed is a truncation"

    style = ui.resolve_style("never")
    res = walkmod.WalkResult("/data")
    res.recent_files, res.touched_files = 1000, 9000
    res.size = res.apparent = 1 << 30
    check.ran, check.gap, check.drift = True, 75.0, 80 << 20
    text = "\n".join(report.render_settle(res, check, style))
    assert "re-stat covered" in text, "the magnitude came from a sample and must say so"
    assert "4,096 of 10,000" in text


# --------------------------------------------------------------------------
# Self-audit: a diagnostic that printed raw seconds where it humanises elsewhere
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (-1, "1s in the future"),
        (-59, "59s in the future"),
        (-3600, "1h 0m in the future"),
        (-46800, "13h 0m in the future"),
        (-90000, "1d 1h in the future"),
    ],
)
def test_a_future_timestamp_is_humanised_like_any_other(seconds, expected):
    """`46800s in the future` is the UTC-offset case, and it needs to read as 13h.

    `quota._timezone_suspicion` exists to say "this age sits on the UTC offset, so
    the backend may be publishing UTC" -- a diagnosis the reader has to make from
    the magnitude. Every other branch of `human_duration` humanises; this one
    printed raw seconds, so the one figure that makes the diagnosis obvious was
    the one figure left unreadable.
    """
    from rapidu.fmt import human_duration

    assert human_duration(seconds) == expected


def test_the_positive_branches_are_unchanged():
    from rapidu.fmt import human_duration

    assert human_duration(0) == "0s"
    assert human_duration(42) == "42s"
    assert human_duration(3600) == "1h 0m"
    assert human_duration(None) == "unknown"


def test_the_json_shape_contract_is_documented(tmp_path):
    """`--json` emits a document for one PATH and a list for several.

    A script written against the single-path form returns null the day a second
    path is added, silently -- the same "shape depends on invocation" defect as
    the owner key, but here the fix would break every existing consumer, so the
    contract is documented instead. This test exists so the documentation cannot
    drift from the behaviour.
    """
    import json

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "f").write_bytes(b"x")
    (tmp_path / "b" / "f").write_bytes(b"y")

    help_text = cli.build_parser().format_help()
    assert "one document per PATH" in help_text
    assert "list of them when several are given" in " ".join(help_text.split())

    import contextlib
    import io as _io

    def run(args):
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(args + ["--json", "--no-quota", "--no-deleted", "--no-progress"])
        return json.loads(buf.getvalue())

    assert isinstance(run([str(tmp_path / "a")]), dict)
    assert isinstance(run([str(tmp_path / "a"), str(tmp_path / "b")]), list)


# --------------------------------------------------------------------------
# RD-12 -- a command is not printed unless it was checked against this host
# --------------------------------------------------------------------------


def _one_cache(tmp_path, relpath):
    d = tmp_path
    for part in relpath.split("/"):
        d = d / part
    d.mkdir(parents=True)
    (d / "blob").write_bytes(b"x" * 4096)
    return walkmod.walk(str(tmp_path), threads=2, depth=1)


def test_a_tool_on_path_is_printed_as_it_is(tmp_path, monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda tool: "/usr/bin/" + tool)
    res = _one_cache(tmp_path, ".cache/pip")
    assert "pip cache purge" in "\n".join(report.render_reclaimable(res, _plain()))


def test_a_tool_behind_a_modulefile_gets_the_module_load(tmp_path, monkeypatch):
    """`uv` is absent from PATH and present as a modulefile on both clusters here."""
    monkeypatch.setattr(report.shutil, "which", lambda tool: None)
    monkeypatch.setattr(report, "_modulefile_for", lambda tool: tool)
    res = _one_cache(tmp_path, ".cache/uv")
    text = "\n".join(report.render_reclaimable(res, _plain()))
    assert "module load uv && uv cache clean" in text


def test_a_missing_tool_falls_back_to_deleting_the_measured_directory(tmp_path, monkeypatch):
    """The `huggingface-cli` case: not installed on midway2 in any configuration.

    A `command not found` is a dead end -- the reader then has to work out that the
    cache is just a directory, which is what they came to the tool to be told. The
    fallback is RD-11's quoted form, so it names the directory measured and a
    truncated copy cannot run.
    """
    monkeypatch.setattr(report.shutil, "which", lambda tool: None)
    monkeypatch.setattr(report, "_modulefile_for", lambda tool: "")
    res = _one_cache(tmp_path, ".cache/huggingface")
    text = "\n".join(report.render_reclaimable(res, _plain()))
    assert "huggingface-cli" not in text
    assert "rm -rf '" + str(tmp_path / ".cache" / "huggingface") + "'" in text


def test_a_missing_tool_is_never_replaced_by_deleting_a_repository(tmp_path, monkeypatch):
    """`git gc` repacks `.git/objects`; deleting it destroys the repository.

    This is why the substitution is a per-entry decision and not a rule.
    """
    monkeypatch.setattr(report.shutil, "which", lambda tool: None)
    monkeypatch.setattr(report, "_modulefile_for", lambda tool: "")
    res = _one_cache(tmp_path, "repo/.git/objects")
    text = "\n".join(report.render_reclaimable(res, _plain()))
    assert "rm -rf" not in text
    assert "git gc --aggressive --prune=now" in text
    assert "git is not on PATH here" in text, "a dead command still names the tool"


def test_advice_entries_are_not_treated_as_tools(tmp_path, monkeypatch):
    """ "safe to delete" has no first word to look up on PATH."""
    monkeypatch.setattr(report.shutil, "which", lambda tool: None)
    monkeypatch.setattr(report, "_modulefile_for", lambda tool: "")
    res = _one_cache(tmp_path, "pkg/__pycache__")
    text = "\n".join(report.render_reclaimable(res, _plain()))
    assert "safe to delete" in text
    assert "is not on PATH" not in text


@pytest.mark.parametrize(
    "command,delete_ok,expected,templated",
    [
        (None, False, None, False),
        ("safe to delete", True, "safe to delete", False),
        ("rm -rf {path}", True, "rm -rf {path}", True),
    ],
)
def test_the_resolver_passes_through_what_needs_no_check(command, delete_ok, expected, templated):
    assert report.reclaim_command(command, delete_ok) == (expected, templated)


def test_the_modulefile_probe_is_evidence_not_a_guess(monkeypatch, tmp_path):
    """A MODULEPATH entry named exactly the tool, or nothing."""
    (tmp_path / "uv").mkdir()
    monkeypatch.setenv("MODULEPATH", str(tmp_path))
    assert report._modulefile_for("uv") == "uv"
    assert report._modulefile_for("huggingface-cli") == ""
    monkeypatch.delenv("MODULEPATH")
    assert report._modulefile_for("uv") == "", "no modules is not a module"


def test_the_document_carries_the_host_resolved_command(tmp_path, monkeypatch):
    """The report and the JSON must not disagree about what is runnable."""
    monkeypatch.setattr(report.shutil, "which", lambda tool: None)
    monkeypatch.setattr(report, "_modulefile_for", lambda tool: "")
    res = _one_cache(tmp_path, ".cache/huggingface")
    doc = report.to_json(res, None, None, None, None)["walk"]["reclaimable"]
    group = next(g for g in doc if g["pattern"] == "cache/huggingface")
    assert group["command"] == "rm -rf {path}"


# --------------------------------------------------------------------------
# RD-12's rule, applied where else the tool prints a command
# --------------------------------------------------------------------------


def _tied_rows():
    return [
        Q.QuotaRow("labA", "blocks", "group", 10, None, None, "", "/project"),
        Q.QuotaRow("labB", "blocks", "group", 20, None, None, "", "/project"),
    ]


@pytest.mark.parametrize(
    "present,expected",
    [
        ((), ""),
        (("mmlsattr",), "`mmlsattr -L`"),
        (("lfs",), "`lfs project -d`"),
        (("mmlsattr", "lfs"), "`mmlsattr -L` or `lfs project -d`"),
    ],
)
def test_the_fileset_hint_names_only_tools_that_exist(monkeypatch, present, expected):
    """The tie-break note suggested two commands without checking either.

    Its whole job is telling the reader how to resolve an ambiguity the tool could
    not, so pointing at two `command not found`s is the one thing it must not do --
    and on any site that is neither GPFS nor Lustre, both were exactly that.
    """
    monkeypatch.setattr(
        rc.shutil, "which", lambda tool: "/usr/bin/" + tool if tool in present else None
    )
    _row, notes = rc._pick_row(_tied_rows(), "blocks", "/project/x")
    tail = notes[0].split("right one")[1]
    if expected:
        assert expected in tail
    else:
        assert tail == "", "no tool, no suggestion -- the ambiguity simply stands"


def test_the_documented_gpfs_flag_is_used(monkeypatch):
    """`mmlsattr --get-fileset` is not among mmlsattr's options; `-L` is the one
    that prints the fileset name. A reader who pasted the old string got a usage
    error rather than an answer."""
    monkeypatch.setattr(rc.shutil, "which", lambda tool: "/usr/bin/" + tool)
    _row, notes = rc._pick_row(_tied_rows(), "blocks", "/project/x")
    assert "--get-fileset" not in notes[0]
    assert "mmlsattr -L" in notes[0]


def test_the_tie_note_still_says_what_it_chose_and_why(monkeypatch):
    """Gating the hint must not cost the note its substance."""
    monkeypatch.setattr(rc.shutil, "which", lambda tool: None)
    _row, notes = rc._pick_row(_tied_rows(), "blocks", "/project/x")
    assert "govern this path equally" in notes[0]
    assert "most narrowly scoped" in notes[0]
    assert "labA" in notes[0] and "labB" in notes[0]


# --------------------------------------------------------------------------
# Self-audit: hard-link dedup is a cross-thread property, not a per-directory one
# --------------------------------------------------------------------------


def test_hard_link_dedup_holds_across_threads_and_directories(tmp_path):
    """`seen_links` is shared by every worker under one lock.

    The existing hard-link tests use a single directory, where one worker sees
    every name and the lock is never contended. Spreading the links *across*
    directories is what lets two workers meet the same inode at the same moment --
    and getting it wrong would misreport the inode count, which is the figure a
    files-quota is charged against.
    """
    dirs, inodes, links = 8, 16, 5
    for d in range(dirs):
        (tmp_path / "d{:02d}".format(d)).mkdir()
    targets = []
    for n in range(inodes):
        p = tmp_path / "d{:02d}".format(n % dirs) / "orig{:03d}".format(n)
        p.write_bytes(b"x" * 4096)
        targets.append(p)
    for n, target in enumerate(targets):
        for k in range(1, links):
            # Offset by a coprime stride so a name lands in a different directory
            # from its inode's other names.
            other = tmp_path / "d{:02d}".format((n + k * 3) % dirs) / "ln{:03d}_{}".format(n, k)
            os.link(str(target), str(other))

    results = set()
    for threads in (1, 2, 8):
        res = walkmod.walk(str(tmp_path), threads=threads, depth=1)
        results.add((res.files, res.inodes, res.hardlink_extra_refs, res.hardlinked_inodes))

    assert len(results) == 1, "the count must not depend on how many workers ran"
    files, total, extra, linked = results.pop()
    assert files == inodes * links, "every name is a directory entry"
    assert extra == inodes * (links - 1), "and all but one name per inode is suppressed"
    assert linked == inodes
    assert total == files + (dirs + 1) - extra, "inodes = names + dirs - suppressed"


# --------------------------------------------------------------------------
# Self-audit: a fabricated GAP when the tree belongs to somebody else
# --------------------------------------------------------------------------


def _owned(by_uid, size, inodes, root="/project/lab"):
    res = walkmod.WalkResult(root)
    res.size = size
    res.files = max(0, inodes - 100)
    res.dirs = 100 if inodes else 0
    res.by_uid = by_uid
    res.by_dev = {42: (size, inodes)}
    return res


def _user_snap(used, kind="blocks", mount="/project/lab"):
    snap = Q.QuotaSnapshot("t")
    snap.available = True
    snap.taken_at = snap.read_at
    snap.rows = [Q.QuotaRow("me", kind, "user", used, None, None, "", mount)]
    return snap


def test_a_tree_owned_by_someone_else_is_not_a_gap():
    """Auditing a colleague's directory reported the whole quota as unexplained.

    A user-scoped row compared against a tree containing none of that user's
    bytes gave "UNEXPLAINED GAP -- 0 B accounted for vs quota 800 GiB", with no
    note and no blocker, and offered candidate causes about unlinked files and
    filesystem snapshots -- for a tree whose entire explanation is that somebody
    else owns it. Walking a shared tree, or someone else's directory, is routine
    on a cluster.
    """
    other = os.getuid() + 1000
    res = _owned({other: (800 << 30, 5100)}, 800 << 30, 5100)
    rec = rc.reconcile(res, _settled(), _user_snap(800 << 30), _no_deleted(), "blocks")

    assert rec.verdict == rc.NOT_COMPARED
    assert not rec.candidates, "nothing to explain, so nothing to hypothesise about"
    assert any("no comparison to make" in n for n in rec.notes)


def test_the_narrowing_note_fires_on_one_other_owner_not_just_two_owners():
    """The old guard was `len(by_uid) > 1`, false in the case that matters most."""
    other = os.getuid() + 1000
    res = _owned({other: (800 << 30, 5100)}, 800 << 30, 5100)
    rec = rc.reconcile(res, _settled(), _user_snap(800 << 30), _no_deleted(), "blocks")
    assert any("you own of the" in n for n in rec.notes)


def test_mixed_ownership_still_produces_a_finding():
    """Owning some of it makes the difference real, and it must survive."""
    other = os.getuid() + 1000
    res = _owned({os.getuid(): (100 << 30, 500), other: (700 << 30, 4600)}, 800 << 30, 5100)
    rec = rc.reconcile(res, _settled(), _user_snap(800 << 30), _no_deleted(), "blocks")
    assert rec.verdict == rc.GAP
    assert rec.walk_value == 100 << 30


def test_a_genuinely_empty_tree_is_still_compared():
    """ "My quota says 800 GiB and this mount holds none of my files" is a finding.

    The guard must key on *somebody else owning it*, not on my share being zero,
    or it would suppress a real discrepancy.
    """
    res = _owned({}, 0, 0)
    rec = rc.reconcile(res, _settled(), _user_snap(800 << 30), _no_deleted(), "blocks")
    assert rec.verdict == rc.GAP


def test_a_tree_all_mine_still_closes():
    res = _owned({os.getuid(): (800 << 30, 5100)}, 800 << 30, 5100)
    rec = rc.reconcile(res, _settled(), _user_snap(800 << 30), _no_deleted(), "blocks")
    assert rec.verdict == rc.CLOSES


def test_a_group_scoped_row_counts_everybody(tmp_path):
    """The narrowing applies to user rows only; a group quota charges the lot."""
    other = os.getuid() + 1000
    res = _owned({other: (800 << 30, 5100)}, 800 << 30, 5100)
    snap = _user_snap(800 << 30)
    snap.rows[0].scope = "group"
    rec = rc.reconcile(res, _settled(), snap, _no_deleted(), "blocks")
    assert rec.verdict == rc.CLOSES, "every byte counts toward a group row"
    assert rec.walk_value == 800 << 30


def test_every_reason_for_not_comparing_is_printed():
    """The renderer showed `notes[0]` only, dropping the reason it computed."""
    other = os.getuid() + 1000
    res = _owned({other: (800 << 30, 5100)}, 800 << 30, 5100)
    rec = rc.reconcile(res, _settled(), _user_snap(800 << 30), _no_deleted(), "blocks")
    assert len(rec.notes) > 1, "this case has two reasons"
    text = "\n".join(report.render_reconcile([rec], _plain()))
    for note in rec.notes:
        assert note.split(" -- ")[0][:40] in text


# --------------------------------------------------------------------------
# Self-audit: a group quota is charged by gid, and gid was never consulted
# --------------------------------------------------------------------------


def _group_snap(used, name, kind="blocks", mount="/project/lab"):
    snap = Q.QuotaSnapshot("t")
    snap.available = True
    snap.taken_at = snap.read_at
    snap.rows = [Q.QuotaRow(name, kind, "group", used, None, None, "", mount)]
    return snap


def _mixed_group_tree(mine_bytes, other_bytes):
    import grp

    gid = os.getgid()
    total = mine_bytes + other_bytes
    res = walkmod.WalkResult("/project/lab")
    res.size, res.files, res.dirs = total, 5000, 100
    res.by_uid = {os.getuid(): (total, 5100)}
    res.by_gid = {gid: (mine_bytes, 2550), gid + 500: (other_bytes, 2550)}
    res.by_dev = {42: (total, 5100)}
    return res, grp.getgrgid(gid).gr_name


def test_a_group_row_is_compared_against_the_bytes_charged_to_that_group():
    """`by_gid` existed for this and reconcile never read it.

    A file written into a shared project directory whose setgid bit is missing
    lands in the writer's personal group, so those bytes are charged somewhere
    nobody is looking -- while the comparison counted them toward the project
    group. Measured: a row charged 400 GiB compared against an 800 GiB tree, and
    the -400 GiB difference reported as a gap blamed on a stale quota figure.
    """
    res, group = _mixed_group_tree(400 << 30, 400 << 30)
    rec = rc.reconcile(res, _settled(), _group_snap(400 << 30, group), _no_deleted(), "blocks")

    assert rec.walk_value == 400 << 30, "only the bytes charged to the group"
    assert rec.verdict == rc.CLOSES
    assert any("charged to the '{}' group".format(group) in n for n in rec.notes)
    assert any("setgid" in n for n in rec.notes), "and why the rest is not charged"


def test_nothing_is_said_when_nothing_was_excluded():
    """A tree entirely charged to the group gains no caveat."""
    res, group = _mixed_group_tree(800 << 30, 0)
    res.by_gid = {os.getgid(): (800 << 30, 5100)}
    rec = rc.reconcile(res, _settled(), _group_snap(800 << 30, group), _no_deleted(), "blocks")
    assert rec.walk_value == 800 << 30
    assert not any("charged to the" in n for n in rec.notes)


def test_an_unresolvable_group_name_keeps_the_old_comparison_and_says_so():
    """`mmlsquota` names a GRP row after its filesystem, not after the group.

    Guessing a gid would be worse than not narrowing, so the whole-tree
    comparison stands -- but the reader is told it includes other groups' bytes.
    """
    res, _group = _mixed_group_tree(400 << 30, 400 << 30)
    snap = _group_snap(800 << 30, "midway2_cap")
    rec = rc.reconcile(res, _settled(), snap, _no_deleted(), "blocks")
    assert rec.walk_value == 800 << 30
    assert any("could not be resolved" in n for n in rec.notes)


def test_the_inode_side_narrows_too():
    res, group = _mixed_group_tree(400 << 30, 400 << 30)
    snap = _group_snap(2550, group, kind="files")
    rec = rc.reconcile(res, _settled(), snap, _no_deleted(), "files")
    assert rec.walk_value == 2550
    assert rec.verdict == rc.CLOSES


def test_both_halves_of_the_sum_use_one_population():
    """The /proc scan is narrowed by gid as well, or the sum mixes populations.

    The block this fix sits in opens with that rule: "Both halves of `accounted`
    have to be narrowed to the quota's population, not just the walk."
    """
    from rapidu.deleted import DeletedFile, DeletedScan

    res, group = _mixed_group_tree(400 << 30, 400 << 30)
    gid = os.getgid()
    scan = DeletedScan()
    scan.files = [
        DeletedFile(42, 1, 8 << 30, "/project/lab/mine", os.getuid(), gid),
        DeletedFile(42, 2, 99 << 30, "/project/lab/theirs", os.getuid() + 1, gid + 500),
    ]
    rec = rc.reconcile(res, _settled(), _group_snap(400 << 30, group), scan, "blocks")
    assert rec.deleted_value == 8 << 30, "the other group's unlinked inode is excluded"


def test_a_user_row_is_unaffected():
    """The uid narrowing must not have moved."""
    res, _group = _mixed_group_tree(400 << 30, 400 << 30)
    snap = Q.QuotaSnapshot("t")
    snap.available = True
    snap.taken_at = snap.read_at
    snap.rows = [Q.QuotaRow("me", "blocks", "user", 800 << 30, None, None, "", "/project/lab")]
    rec = rc.reconcile(res, _settled(), snap, _no_deleted(), "blocks")
    assert rec.walk_value == 800 << 30, "by_uid says I own all of it"


def test_a_tree_charged_to_no_group_of_this_row_is_not_a_gap():
    """The gid counterpart of the ownership guard.

    A tree none of which is charged to the row's group tells you nothing about
    that group's quota, so calling the whole figure a gap would be the same
    fabrication the uid case produced.
    """
    import grp

    gid = os.getgid()
    group = grp.getgrgid(gid).gr_name
    res = walkmod.WalkResult("/project/lab")
    res.size, res.files, res.dirs = 800 << 30, 5000, 100
    res.by_uid = {os.getuid(): (800 << 30, 5100)}
    res.by_gid = {gid + 500: (800 << 30, 5100)}
    res.by_dev = {42: (800 << 30, 5100)}

    rec = rc.reconcile(res, _settled(), _group_snap(800 << 30, group), _no_deleted(), "blocks")
    assert rec.verdict == rc.NOT_COMPARED
    assert not rec.candidates
    assert any("charged to the '{}' group".format(group) in n for n in rec.notes)
    assert any("no comparison to make" in n for n in rec.notes)


def test_the_guard_keys_on_inodes_too_not_only_bytes():
    """A tree of empty files has no bytes and plenty of inodes.

    Keying the guard on `res.size` alone would compare a zero-byte tree of other
    people's files as though it were an empty directory.
    """
    res = walkmod.WalkResult("/project/lab")
    res.size, res.files, res.dirs = 0, 5000, 100
    other = os.getuid() + 1000
    res.by_uid = {other: (0, 5100)}
    res.by_dev = {42: (0, 5100)}
    rec = rc.reconcile(res, _settled(), _user_snap(5100, kind="files"), _no_deleted(), "files")
    assert rec.verdict == rc.NOT_COMPARED


# --------------------------------------------------------------------------
# Round 42 confirmed the arithmetic; this pins the closure identity it states
# --------------------------------------------------------------------------


def test_the_listed_entries_plus_the_root_inode_equal_the_total(tmp_path):
    """The table and the headline must not disagree about the same tree.

    Round 42 states the identity: `sum(-n 0 entries) + the root's own blocks ==
    size_bytes`, the residual being the walk root's own inode, which belongs in
    the total and not in a table of its children. This repo has already shipped a
    fix titled "totals that reported something other than they measured", so the
    identity is worth asserting rather than assumed.
    """
    for name, count in (("a", 3), ("b", 2), ("b/c", 4), ("d", 2)):
        d = tmp_path
        for part in name.split("/"):
            d = d / part
            if not d.exists():
                d.mkdir()
        for k in range(count):
            with open(str(d / "f{}".format(k)), "wb") as handle:
                handle.write(b"x" * (1000 * (k + 1)))
                handle.flush()
                os.fsync(handle.fileno())

    res = walkmod.walk(str(tmp_path), threads=4, depth=1)
    listed = sum(e.size for e in res.top_dirs(report._limit(0), "size"))
    assert listed + os.lstat(str(tmp_path)).st_blocks * 512 == res.size

    # And the allocation ratio is derived from the two figures, not estimated.
    assert round(res.apparent * (res.alloc_ratio or 1.0)) == res.size
    # No hard links in this tree, so every name is its own inode.
    assert res.files + res.dirs == res.inodes


@pytest.mark.parametrize("depth", [1, 3, 9])
def test_the_total_does_not_move_with_a_flag_that_only_changes_the_view(tmp_path, depth):
    """`-d` chooses how much of the tree is *listed*, never what is counted."""
    (tmp_path / "x" / "y").mkdir(parents=True)
    for path in (tmp_path / "x" / "f", tmp_path / "x" / "y" / "g"):
        with open(str(path), "wb") as handle:
            handle.write(b"x" * 5000)
            handle.flush()
            os.fsync(handle.fileno())

    baseline = walkmod.walk(str(tmp_path), threads=1, depth=1)
    other = walkmod.walk(str(tmp_path), threads=16, depth=depth)
    assert (other.size, other.apparent, other.files, other.dirs, other.inodes) == (
        baseline.size,
        baseline.apparent,
        baseline.files,
        baseline.dirs,
        baseline.inodes,
    )


# --------------------------------------------------------------------------
# The `expanduser` trap named in README.md: guard the result, not the exception
# --------------------------------------------------------------------------


def _no_home(monkeypatch):
    """The midway2 compute-node shape: no $HOME, no $USER, no passwd entry.

    `sbatch --export=NONE` leaves HOME, USER and LOGNAME unset, and midway2's
    compute nodes have no passwd entry for the user, so all the usual fallbacks
    fail at once. rapidu survives it -- this is about what it *says*.
    """
    for var in ("HOME", "USER", "LOGNAME"):
        monkeypatch.delenv(var, raising=False)

    def missing(uid):
        raise KeyError(uid)

    monkeypatch.setattr(cli.os.path, "expanduser", lambda p: p)
    return missing


def test_an_unexpandable_tilde_is_not_reported_as_a_missing_path(monkeypatch, capsys):
    """`expanduser` returns the string unchanged rather than raising.

    Refusing the path is right and rapidu already did. Calling it "no such path"
    was not: the path is not missing, the tilde is unexpanded, and the reader goes
    looking for a directory that was never named -- the same wrong-cause mistake
    as RD-2's "not on PATH".
    """
    _no_home(monkeypatch)
    rc_code = cli.main(["~/scratch", "--no-box", "--color", "never"])
    err = capsys.readouterr().err
    assert rc_code == cli.EXIT_ERROR
    assert "no such path" not in err
    assert "cannot expand `~`" in err
    assert "$HOME is unset" in err


def test_an_unknown_user_gets_its_own_reason(monkeypatch, capsys):
    """ "no such user" and "no $HOME" send the reader to different places."""
    _no_home(monkeypatch)
    cli.main(["~nosuchuser/data", "--no-box", "--color", "never"])
    err = capsys.readouterr().err
    assert "no such user `nosuchuser`" in err


def test_the_quota_reader_refuses_an_unexpanded_tilde_too(monkeypatch, capsys):
    """`cmd_quota` mapped rows to the path, so it would have asked about `./~`."""
    _no_home(monkeypatch)
    assert cli.main(["-Q", "~/scratch", "--no-box", "--color", "never"]) == cli.EXIT_ERROR
    assert "cannot expand `~`" in capsys.readouterr().err


def test_a_resolvable_tilde_is_untouched(tmp_path, monkeypatch):
    """The guard must not reject an ordinary home-relative path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "sub").mkdir()
    resolved, refused = cli._resolve_paths(["~/sub"])
    assert refused == 0
    assert resolved == [str(tmp_path / "sub")]


def test_a_path_with_a_tilde_inside_it_is_not_a_home_reference(tmp_path):
    """Only a leading `~` is an expansion; `a/~b` is a directory named `~b`."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "~b").mkdir()
    resolved, refused = cli._resolve_paths([str(tmp_path / "a" / "~b")])
    assert refused == 0 and len(resolved) == 1


def test_guess_mount_survives_an_unexpandable_home(monkeypatch):
    """`_guess_mount` appends `expanduser("~")`, which can be the literal `~`.

    With the mount-point rule from RD-3 the literal is rejected as evidence, which
    is what keeps a `home` fileset from mapping to a relative directory named `~`.
    """
    monkeypatch.setattr(Q.os.path, "expanduser", lambda p: p)
    assert Q._guess_mount("home", ["/home"]) == "/home"
    assert Q._guess_mount("home", []) != "~"


# --------------------------------------------------------------------------
# The second encoding layer: a filename is data, and no glyph table reaches it
# --------------------------------------------------------------------------


class _Encoded(object):
    """A stream stand-in that reports a chosen encoding."""

    def __init__(self, encoding):
        self.encoding = encoding


@pytest.mark.parametrize(
    "encoding,expected",
    [
        ("utf-8", "\u30d5\u30a1.txt"),
        ("iso8859-1", "\\u30d5\\u30a1.txt"),
        ("ascii", "\\u30d5\\u30a1.txt"),
    ],
)
def test_encode_safe_escapes_only_what_the_stream_cannot_hold(encoding, expected):
    assert ui.encode_safe("\u30d5\u30a1.txt", _Encoded(encoding)) == expected


def test_a_stream_with_no_encoding_is_left_alone():
    """A StringIO under test accepts anything, matching `_supports_unicode`."""
    assert ui.encode_safe("\u30d5.txt", io.StringIO()) == "\u30d5.txt"


def test_an_unknown_encoding_name_is_passed_through():
    assert ui.encode_safe("x", _Encoded("not-a-real-codec")) == "x"


def test_latin1_representable_characters_survive():
    """Escaping everything would be as wrong as escaping nothing."""
    assert ui.encode_safe("\xfcber.txt", _Encoded("iso8859-1")) == "\xfcber.txt"
    assert ui.encode_safe("\xfcber.txt", _Encoded("ascii")) == "\\xfcber.txt"


def test_a_cjk_filename_does_not_take_the_whole_report_with_it(tmp_path, monkeypatch):
    """Measured before the fix: rc=1 and *zero bytes*, on one filename.

    `fs=utf-8, stdout=iso8859-1` is what `PYTHONIOENCODING` set for a downstream
    consumer produces, or a UTF-8 filesystem under a latin-1 locale. The glyph
    probe cannot help: the characters are in a filename, so `--ascii` converts all
    of rapidu's own decoration and the report still dies.
    """
    import subprocess
    import sys

    (tmp_path / "\u30d5\u30a1\u30a4\u30eb.txt").write_bytes(b"x")
    (tmp_path / "plain.txt").write_bytes(b"y")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "iso8859-1"
    env["COLUMNS"] = "88"
    proc = subprocess.run(
        [sys.executable, "-m", "rapidu", "-n", "3", str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout, "the tree was measured; it has to be reported"
    text = proc.stdout.decode("iso8859-1")
    assert "\\u30d5" in text, "the unencodable name is escaped, not dropped"
    assert "plain.txt" in text, "and the rest of the table survives"


def test_the_frame_still_closes_once_names_are_escaped(tmp_path):
    """An escape is wider than what it replaces, so it must precede the padding."""
    import subprocess
    import sys

    (tmp_path / "\u30d5\u30a1\u30a4\u30eb.txt").write_bytes(b"x")
    (tmp_path / "plain.txt").write_bytes(b"y")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "ascii"
    env["COLUMNS"] = "88"
    proc = subprocess.run(
        [sys.executable, "-m", "rapidu", "-n", "3", str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    lines = proc.stdout.decode("ascii").splitlines()
    assert lines
    assert len({ui.visible_width(line) for line in lines}) == 1, "one width, or the border moved"


# --------------------------------------------------------------------------
# Self-audit: a deleted working directory was an unhandled traceback
# --------------------------------------------------------------------------


def test_a_deleted_working_directory_is_a_message_not_a_traceback(monkeypatch, capsys):
    """`os.getcwd()` raises, and both entry points called it unguarded.

    A scratch directory reclaimed by a cleanup policy, or removed by another job,
    while a shell sits in it -- routine on a cluster. rapidu answered with
    `FileNotFoundError` before doing any work, which is the failure mode this
    campaign filed against two of the sibling packages (SP-16, SM-16) and that
    rapidu was otherwise praised for surviving.
    """

    def gone():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(cli.os, "getcwd", gone)
    assert cli.main(["--no-box", "--color", "never"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "current directory no longer exists" in err
    assert "name one explicitly" in err, "and what to do about it"


def test_the_quota_reader_says_the_same_thing(monkeypatch, capsys):
    def gone():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(cli.os, "getcwd", gone)
    assert cli.main(["-Q", "--no-box", "--color", "never"]) == cli.EXIT_ERROR
    assert "current directory no longer exists" in capsys.readouterr().err


def test_an_explicit_path_still_works_without_a_working_directory(tmp_path, monkeypatch):
    """Only the *default* is unavailable; a named path needs no cwd."""

    def gone():
        raise FileNotFoundError(2, "No such file or directory")

    (tmp_path / "f").write_bytes(b"x")
    monkeypatch.setattr(cli.os, "getcwd", gone)
    assert (
        cli.main([str(tmp_path), "--no-box", "--color", "never", "--no-quota", "--no-deleted"])
        == cli.EXIT_OK
    )


def test_the_real_syscall_path_end_to_end(tmp_path):
    """The monkeypatched tests above cover the branches; this covers the syscall."""
    import subprocess
    import sys

    doomed = tmp_path / "doomed"
    doomed.mkdir()
    # An absolute PYTHONPATH: the child `cd`s into a directory that is then
    # removed, so a relative entry (`PYTHONPATH=src`, which is how this suite runs
    # against the 3.6 floor) no longer resolves from there.
    #
    # **Every** entry, not just the one added here. Prepending an absolute path
    # and keeping the inherited value verbatim left the relative entry in place,
    # and from 3.11 the path computation moved into `getpath.py`, which resolves
    # `sys.path` entries during interpreter startup: a relative one with no cwd
    # aborts the interpreter before `main` runs, so the child died with
    # "Exception ignored in running getpath: error evaluating path" and never
    # reached the message under test. Measured on a RHEL 9 login node's 3.11.13
    # and 3.13.15 with `PYTHONPATH=src`; the same tree passes there once the
    # inherited entry is absolute, and 3.9 and 3.12 tolerated it either way,
    # which is why the suite was green on the two hosts that happened to run
    # those. The test asserts how this tool reports a missing cwd, so it must not
    # be decided by how the suite was invoked.
    import rapidu

    env = dict(os.environ)
    inherited = [p for p in (env.get("PYTHONPATH") or "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.dirname(os.path.dirname(os.path.abspath(rapidu.__file__)))]
        + [os.path.abspath(p) for p in inherited]
    )
    proc = subprocess.run(
        [
            "sh",
            "-c",
            "cd '{0}' && rmdir '{0}' && exec {1} -m rapidu --no-box --color never".format(
                doomed, sys.executable
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.returncode == 2, proc.stderr.decode()
    assert b"Traceback" not in proc.stderr
    assert b"current directory no longer exists" in proc.stderr


@pytest.mark.parametrize("columns", ["0", "1", "10", "40", "100000"])
def test_an_absurd_terminal_width_still_frames(tmp_path, columns):
    """The floor and the content-hug have to hold at both extremes."""
    import subprocess
    import sys

    (tmp_path / "f").write_bytes(b"x" * 4096)
    env = dict(os.environ)
    env["COLUMNS"] = columns
    proc = subprocess.run(
        [sys.executable, "-m", "rapidu", "-n", "2", str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    lines = [line for line in proc.stdout.decode().splitlines() if line]
    assert lines
    assert len({ui.visible_width(line) for line in lines}) == 1, columns


# --------------------------------------------------------------------------
# RD-13 -- one key name carrying two different quantities
# --------------------------------------------------------------------------


def test_every_json_breakdown_reconciles_with_its_total(tmp_path):
    """The invariant asked for in RD-13, which is what would have caught it.

    `sum(by_age[].inodes)` was `files`, while `sum(by_uid[].inodes)` was `inodes`
    -- one key name, two quantities, inside one object. The bytes reconciled in
    every breakdown; only the count column diverged, and only there.
    """
    for n in range(4):
        d = tmp_path / "d{}".format(n)
        d.mkdir()
        with open(str(d / "f"), "wb") as handle:
            handle.write(b"x" * 4096)
            handle.flush()
            os.fsync(handle.fileno())
    res = walkmod.walk(str(tmp_path), threads=2, depth=1)
    doc = report.to_json(res, None, None, None, None)["walk"]

    for key in ("by_uid", "by_gid"):
        assert sum(v["bytes"] for v in doc[key].values()) == doc["size_bytes"], key
        assert sum(v["inodes"] for v in doc[key].values()) == doc["inodes"], key

    # Not equality: `by_age` buckets *regular files*, so its bytes exclude the
    # blocks the directories themselves occupy. RD-13's suggested invariant put
    # equality here, which happens to hold only where directories are inline (GPFS
    # stores a small one in its inode, so it costs 0 blocks) and fails wherever
    # they are not -- 2,560 bytes across five directories on this filesystem.
    assert 0 < sum(b["bytes"] for b in doc["by_age"]) <= doc["size_bytes"]
    # `files`, and it is the *deduped* regular-file count: a hard-link duplicate is
    # counted in `files` and then skipped before the bucketing.
    assert "inodes" not in doc["by_age"][0], "the misleading name is gone"
    assert sum(b["files"] for b in doc["by_age"]) == doc["files"] - doc["hardlink_extra_refs"]


def test_the_schema_version_records_the_rename(tmp_path):
    """The rule on that key: bumped when one changes meaning or disappears.

    This pins the *rename*, not the number. It asserted ``schema == 2`` and so
    failed the next time an unrelated key was fixed under the same rule -- which
    made a correct bump look like a regression in RD-13. What RD-13 is about is
    that ``by_age`` carries ``files`` and not ``inodes``, and that the counter
    moved when it changed; the exact value belongs to whichever test is checking
    the current document shape (see ``test_features``).
    """
    (tmp_path / "f").write_bytes(b"x")
    res = walkmod.walk(str(tmp_path), threads=1, depth=1)
    doc = report.to_json(res, None, None, None, None)
    assert doc["schema"] >= 2
    buckets = doc["walk"]["by_age"]
    assert all("files" in b for b in buckets)
    assert not any("inodes" in b for b in buckets)


def test_by_age_excludes_hard_link_duplicates(tmp_path):
    """Pinning which population the count is, since that is the whole finding."""
    target = tmp_path / "orig"
    with open(str(target), "wb") as handle:
        handle.write(b"x" * 4096)
        handle.flush()
        os.fsync(handle.fileno())
    for n in range(3):
        os.link(str(target), str(tmp_path / "ln{}".format(n)))
    res = walkmod.walk(str(tmp_path), threads=1, depth=1)
    doc = report.to_json(res, None, None, None, None)["walk"]
    assert doc["files"] == 4 and doc["hardlink_extra_refs"] == 3
    assert sum(b["files"] for b in doc["by_age"]) == 1


def test_the_cold_data_share_uses_the_population_it_counted(tmp_path):
    """A file count over an inode denominator diluted the share by every directory.

    The label beside it always said "file"; only the divisor disagreed. On a
    directory-heavy tree "4 files (16.0%) has not been modified in over a year"
    was really 100%, and because the sentence is gated at 5%, a flat enough tree
    would have suppressed the finding altogether -- the same failure this section
    was already fixed for once.
    """
    import time

    for n in range(20):
        (tmp_path / "d{:02d}".format(n)).mkdir()
    old = time.time() - 400 * 86400
    for n in range(4):
        p = tmp_path / "d{:02d}".format(n) / "f"
        p.write_bytes(b"x" * 4096)
        os.utime(str(p), (old, old))

    res = walkmod.walk(str(tmp_path), threads=2, depth=1)
    assert res.dirs > res.files, "the fixture has to be directory-heavy to show it"
    # The *sentence*, not the whole block. `16.0%` was the wrong divisor's answer
    # (4 of 25 inodes) and this asserted its absence anywhere in the section -- but
    # the table above the sentence carries byte shares, and on a filesystem whose
    # directories cost 4 KiB the cold bucket's share is legitimately 16384/102400,
    # which renders as exactly that string. The claim is about the divisor in the
    # sentence, so the sentence is what is read.
    lines = report.render_age(res, _plain())
    sentence = " ".join(" ".join(ln.split()) for ln in lines if "not been modified" in ln)
    assert sentence, lines
    assert "(100.0%)" in sentence, sentence
    assert "(16.0%)" not in sentence, sentence


# --------------------------------------------------------------------------
# RD-14 -- per-scope `lfs quota` failures joined without their scope name
# --------------------------------------------------------------------------


def _lfs_runner(per_scope):
    """A `_run` stand-in returning a chosen (rc, err) per scope flag."""

    def run(cmd, timeout):
        if cmd[:2] == ["lfs", "project"]:
            return 1, "", ""
        for flag, result in per_scope.items():
            if flag in cmd:
                return result
        return 1, "", "unexpected"

    return run


def test_each_scope_failure_names_its_scope(monkeypatch):
    """The scope was used only as a *fallback* for an empty stderr.

    So in the ordinary case -- a tool that failed and said why -- it was discarded
    in favour of the raw message, which is exactly when knowing whether the user,
    group or project query failed would help. Two scopes failing differently read
    as "cannot find quota for user; Operation not permitted", with nothing to say
    the second was the group query.
    """
    monkeypatch.setattr(
        Q,
        "_run",
        _lfs_runner(
            {
                "-u": (1, "", "cannot find quota for user"),
                "-g": (1, "", "Operation not permitted"),
            }
        ),
    )
    reason = Q.read_lfs_quota("/lustre/me").reason
    assert reason == "user: cannot find quota for user; group: Operation not permitted"


def test_scopes_failing_alike_are_collapsed_with_all_of_them_named(monkeypatch):
    """Naming the scope must not turn one fact into one sentence per scope."""
    monkeypatch.setattr(
        Q, "_run", _lfs_runner({"-u": (1, "", "MDS unreachable"), "-g": (1, "", "MDS unreachable")})
    )
    assert Q.read_lfs_quota("/lustre/me").reason == "user, group: MDS unreachable"


def test_a_silent_failure_still_names_the_scope_and_the_code(monkeypatch):
    monkeypatch.setattr(Q, "_run", _lfs_runner({"-u": (1, "", ""), "-g": (1, "", "")}))
    assert Q.read_lfs_quota("/lustre/me").reason == "user, group: rc=1"


def test_an_absent_tool_is_reported_about_the_tool_not_a_scope(monkeypatch):
    """RD-2's fix, which this round confirms working, takes precedence.

    Exit 127 is about `lfs` itself, so it carries no scope prefix and stops the
    loop -- the remaining scopes cannot go any better. This is why the symptom RD-14
    measured (`lfs quota: command not found; command not found`) no longer appears:
    the message names the real cause and is produced once.
    """
    monkeypatch.setattr(Q, "_run", lambda cmd, t: (127, "", "command not found"))
    monkeypatch.setattr(Q.shutil, "which", lambda tool: None)
    assert Q.read_lfs_quota("/lustre/me").reason == "`lfs` is not on PATH"


def test_the_merged_three_backend_reason_is_one_line_with_three_labels(monkeypatch):
    """What the QUOTA box actually renders when nothing can answer.

    RD-14 shows this breaking across four lines on 0.3.0. RD-4's collapse fixed
    that; this pins it, since the finding is about what the user reads.
    """

    def run(cmd, timeout):
        if cmd[0] == "quota":
            return 127, "", "command not found"
        if cmd[0] == "mmlsquota":
            return (
                0,
                "",
                (
                    "No quota enabled file system found.\n"
                    "mmlsquota: tslsquota  -Y  failed. Error code 22.\n"
                    "mmlsquota: Command failed. Examine previous error messages.\n"
                ),
            )
        return 127, "", "command not found"

    monkeypatch.setattr(Q, "_run", run)
    monkeypatch.setattr(Q.shutil, "which", lambda tool: None)
    reason = Q.read_best("/home/me", timeout=5.0).reason
    assert "\n" not in reason
    assert len(reason.split("; ")) == 3, reason
    for label in ("quota -s:", "mmlsquota:", "lfs quota:"):
        assert label in reason


# --------------------------------------------------------------------------
# RD-14's shape, found in the per-device fan-out I wrote for RD-1
# --------------------------------------------------------------------------


def test_per_device_failures_name_their_device(midway2_mounts, monkeypatch):
    """The RD-14 defect, in the code that fixed RD-1.

    Three GPFS devices failing three different ways joined their messages and
    dropped every device name -- and "Non root user is not permitted" is only
    actionable if you know *which* filesystem needs an admin. That message is not
    hypothetical: it is what `mmlsfileset` returns on midway2.
    """
    messages = {
        "midway2_perf2": "No quota enabled file system found.",
        "midway2_cap": "Non root user is not permitted to run with the specified option(s)",
        "midway2_perf": "Command failed. Examine previous error messages.",
    }

    def run(cmd, timeout):
        if cmd == ["mmlsquota", "-Y"]:
            return 0, "", "No quota enabled file system found."
        return 0, "", messages.get(cmd[-1], "Command failed.")

    monkeypatch.setattr(Q, "_run", run)
    reason = Q.read_mmlsquota("/home/me").reason
    assert "midway2_cap: Non root user is not permitted" in reason
    assert "\n" not in reason


def test_devices_failing_alike_are_one_segment_naming_all_of_them(midway2_mounts, monkeypatch):
    """Seven devices failing identically is one fact, not seven sentences."""
    monkeypatch.setattr(Q, "_run", lambda cmd, t: (0, "", "No quota enabled file system found."))
    reason = Q.read_mmlsquota("/home/me").reason
    assert reason.count("No quota enabled file system found.") == 1
    assert reason.startswith("default, "), reason
    assert "midway2_perf2" in reason and "dali_cap" in reason


def test_an_absent_mmlsquota_names_the_command_not_a_device(midway2_mounts, monkeypatch):
    monkeypatch.setattr(Q, "_run", lambda cmd, t: (127, "", "command not found"))
    monkeypatch.setattr(Q.shutil, "which", lambda tool: None)
    assert Q.read_mmlsquota("/home/me").reason == "`mmlsquota` is not on PATH"


@pytest.mark.parametrize(
    "pairs,expected",
    [
        ([("a", "boom")], "a: boom"),
        ([("a", "boom"), ("b", "boom")], "a, b: boom"),
        ([("a", "one"), ("b", "two")], "a: one; b: two"),
        ([("", "no subject")], "no subject"),
        ([("a", "boom"), ("a", "boom")], "a: boom"),
        ([], ""),
    ],
)
def test_the_grouping_helper_itself(pairs, expected):
    """One helper for both fan-outs, so the two cannot drift apart in wording."""
    assert Q._grouped_failures(pairs) == expected


# --------------------------------------------------------------------------
# RD-8's shape again: three more caps that did not publish themselves
# --------------------------------------------------------------------------


def _many_owners(uids=9, gids=8):
    res = walkmod.WalkResult("/project/lab")
    res.size, res.files, res.dirs = 1 << 30, 100, 10
    res.by_uid = {1000 + n: ((20 - n) << 20, 10) for n in range(uids)}
    res.by_gid = {2000 + n: ((20 - n) << 20, 10) for n in range(gids)}
    res.by_dev = {42: (res.size, 110)}
    return res


def test_the_owners_table_says_how_many_it_left_out():
    """Six of twenty owners, and the rows do not sum to the total.

    Every other bound in this report publishes itself. This one did not, so a
    reader doing the arithmetic finds a gap with nothing to explain it.
    """
    text = "\n".join(report.render_walk(_many_owners(), walkmod.SettleCheck(), 3, style=_plain()))
    assert "... and 3 more owners" in text


def test_the_groups_table_says_how_many_it_left_out():
    """This one is captioned "a group quota charges these".

    So the group a quota row actually names can sit seventh by bytes and never
    appear -- which matters more now that reconcile narrows a group row to exactly
    that gid.
    """
    text = "\n".join(report.render_walk(_many_owners(), walkmod.SettleCheck(), 3, style=_plain()))
    assert "... and 2 more groups" in text


def test_a_table_inside_the_cap_gains_no_caveat():
    text = "\n".join(
        report.render_walk(_many_owners(uids=3, gids=2), walkmod.SettleCheck(), 3, style=_plain())
    )
    assert "more owners" not in text and "more groups" not in text


def test_extra_holders_of_a_deleted_inode_are_counted():
    """Killing the three named and not getting the space back is the failure.

    The inode is freed when the *last* holder closes it, so a truncated holder
    list is a truncated instruction.
    """
    from rapidu.deleted import DeletedFile, DeletedScan

    scan = DeletedScan()
    held = DeletedFile(1, 1, 512 << 20, "/project/lab/big.bin", os.getuid(), os.getgid())
    for pid in range(7):
        held.add_holder(9000 + pid, "python3")
    scan.files = [held]
    text = "\n".join(report.render_deleted(scan, 10, _plain()))
    assert "(+4 more holding it)" in text

    one = DeletedScan()
    solo = DeletedFile(1, 2, 1 << 20, "/project/lab/small.bin", os.getuid(), os.getgid())
    solo.add_holder(1234, "python3")
    one.files = [solo]
    assert "more holding it" not in "\n".join(report.render_deleted(one, 10, _plain()))


@pytest.mark.parametrize(
    "total,shown,expected",
    [(9, 6, "... and 3 more owners"), (6, 6, None), (2, 6, None)],
)
def test_the_disclosure_helper(total, shown, expected):
    lines = report._and_more(total, shown, "owners", _plain())
    if expected is None:
        assert lines == []
    else:
        assert expected in lines[0]


def test_the_two_vocabularies_are_bridged_where_they_meet():
    """`files` in a quota row and `inodes` in the walk are the same quantity.

    Renaming the walk's counts to `inodes` (RD-9, extended to the tables) traded
    the old label's one virtue: `files` matched the backend's own word, so the two
    numbers could be compared without a translation step. That step is now stated
    explicitly, once, and only where a files row is actually on screen.
    """
    snap = Q.QuotaSnapshot("quota -s")
    snap.available = True
    snap.taken_at = snap.read_at
    snap.rows = [
        Q.QuotaRow("home", "blocks", "user", 1 << 30, 30 << 30, None, "", "/home"),
        Q.QuotaRow("home", "files", "user", 26633, 300000, None, "", "/home"),
    ]
    text = "\n".join(report.render_quota(snap, style=_plain()))
    assert "counts inodes" in text
    assert "directories included" in text


def test_no_files_row_means_no_vocabulary_note():
    """A blocks-only reading has no ambiguity to explain."""
    snap = Q.QuotaSnapshot("quota -s")
    snap.available = True
    snap.taken_at = snap.read_at
    snap.rows = [Q.QuotaRow("home", "blocks", "user", 1 << 30, 30 << 30, None, "", "/home")]
    assert "counts inodes" not in "\n".join(report.render_quota(snap, style=_plain()))


# --------------------------------------------------------------------------
# Round 59's hazard shape: an environment variable changing what is reported
# --------------------------------------------------------------------------


def test_a_stale_user_variable_cannot_redirect_the_quota_query(monkeypatch):
    """`export USER=somebody` made rapidu report somebody else's quota as yours.

    Round 59 is about mock modes, which rapidu has none of -- but the hazard it
    names is "an environment variable arriving from a test harness, CI wrapper,
    module file or stale export rather than from a deliberate flag", and
    `mmlsquota -u` / `lfs quota -u` took their user from `$USER`. `reconcile` would
    then compare your walk against a stranger's quota figure: a fabricated
    comparison sourced from the environment, with nothing on screen to say so.
    """
    import pwd

    monkeypatch.setenv("USER", "somebodyelse")
    assert Q._current_user() == pwd.getpwuid(os.getuid()).pw_name

    asked = []

    def run(cmd, timeout):
        asked.append(cmd)
        return 0, "", "No quota enabled file system found."

    monkeypatch.setattr(Q, "_run", run)
    monkeypatch.setattr(Q, "_mount_entries", lambda p="/proc/mounts": [("fsA", "/a", "gpfs")])
    Q.read_mmlsquota("/a")
    named = [cmd for cmd in asked if "-u" in cmd]
    assert named, "the per-device fan-out asks for a user"
    assert "somebodyelse" not in named[0]


def test_the_user_variable_is_still_the_fallback_where_passwd_cannot_answer(monkeypatch):
    """midway2's compute nodes have no passwd entry, which is why it is in the chain.

    There is no authority for `$USER` to disagree with, so using it is right.
    """
    import pwd

    def missing(uid):
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", missing)
    monkeypatch.setenv("USER", "fallbackname")
    assert Q._current_user() == "fallbackname"


def test_with_neither_the_bare_uid_is_used(monkeypatch):
    """Every backend here accepts a numeric id."""
    import pwd

    def missing(uid):
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", missing)
    monkeypatch.delenv("USER", raising=False)
    assert Q._current_user() == str(os.getuid())


def test_rapidu_has_no_mock_mode_to_disclose():
    """The finding's own subject: there is no synthetic-data path to mark.

    Asserted rather than assumed, because "we have no demo mode" is the kind of
    claim that quietly stops being true.
    """
    import rapidu.cli as climod

    help_text = climod.build_parser().format_help().lower()
    for word in ("--demo", "--mock", "--fake", "--simulate"):
        assert word not in help_text
    src = os.path.dirname(os.path.abspath(Q.__file__))
    for name in sorted(os.listdir(src)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(src, name)) as handle:
            body = handle.read()
        for var in ("RAPIDU_MOCK", "RAPIDU_DEMO", "RAPIDU_FAKE"):
            assert var not in body, "{} references {}".format(name, var)


# --------------------------------------------------------------------------
# Round 45's axis applied to stdout: a full filesystem is the condition a
# quota tool is run in
# --------------------------------------------------------------------------


def _run_cli(args, stdout_target=None, env_extra=None):
    import subprocess
    import sys

    env = dict(os.environ)
    env.update(env_extra or {})
    with open(stdout_target or os.devnull, "wb") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "rapidu"] + args,
            stdout=sink,
            stderr=subprocess.PIPE,
            env=env,
        )
        _out, err = proc.communicate()
    return proc.returncode, err.decode("utf-8", "replace")


def test_a_full_filesystem_is_reported_by_this_tool_not_by_python():
    """`rdu . > report.txt` on a full filesystem exited 120 with a Python internal.

    The tester's standard for this axis, met by `slurmwatch --log` across five
    write failures, is "rc=1, no traceback, the errno named". Left to interpreter
    shutdown the failure came back as

        Exception ignored in: <_io.TextIOWrapper name='<stdout>' ...>
        OSError: [Errno 28] No space left on device

    with rc=120 -- not one of this tool's three codes. A full filesystem is
    precisely the condition a quota tool is run in.
    """
    code, err = _run_cli(["-n", "2", "."], stdout_target="/dev/full")
    assert code == cli.EXIT_ERROR
    assert "Exception ignored" not in err
    assert "Traceback" not in err
    assert "cannot write the report" in err
    assert "Errno 28" in err


def test_a_closed_pipe_is_still_not_an_error():
    """`rdu -a . | head` is how anyone reads a long report."""
    import subprocess
    import sys

    walker = subprocess.Popen(
        [sys.executable, "-m", "rapidu", "-a", "."],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    head = subprocess.Popen(["head", "-2"], stdin=walker.stdout, stdout=subprocess.DEVNULL)
    # The parent's copy of the read end has to close or `head` never sees EOF.
    walker.stdout.close()
    head.wait()
    # Read stderr directly rather than via `communicate()`: on 3.6 -- the floor
    # this package advertises -- calling it after closing `.stdout` raises
    # `ValueError: Invalid file object`, so this test failed there while the
    # behaviour it checks was correct.
    err = walker.stderr.read()
    walker.stderr.close()
    walker.wait()
    assert walker.returncode in (0, 141), err.decode("utf-8", "replace")
    assert b"Exception ignored" not in err


def test_an_ordinary_run_is_unaffected(tmp_path):
    (tmp_path / "f").write_bytes(b"x")
    code, err = _run_cli(["-n", "1", str(tmp_path), "--no-quota", "--no-deleted"])
    assert code == cli.EXIT_OK
    assert err == ""


class _FullDisk(object):
    """A stdout whose writes are accepted and whose flush fails, like ENOSPC.

    Driven at the stream rather than at fd 1: under pytest's capture, fd 1 already
    belongs to the harness, so a `dup2` dance cannot make the write fail and the
    test skipped itself into uselessness.
    """

    encoding = "utf-8"

    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, text):
        self.written.append(text)
        return len(text)

    def flush(self):
        raise OSError(28, "No space left on device")

    def close(self):
        self.closed = True

    def isatty(self):
        return False

    def fileno(self):
        raise io.UnsupportedOperation("not a real fd")


def test_a_failed_write_is_reported_and_the_buffer_discarded(monkeypatch, capsys):
    """A failed flush leaves the unwritten report buffered, where it can resurface.

    Pointing fd 1 elsewhere -- the first shape of this handler -- left the data in
    `sys.stdout`'s buffer, so an in-process caller got the stale report emitted
    into whatever stream came next. Observed: a report written to a full device
    reappeared on the terminal during a *later* call. Closing discards it, which is
    what the `BrokenPipeError` handler beside it already did, and closing is the
    property asserted here.
    """
    sink = _FullDisk()
    monkeypatch.setattr(sys, "stdout", sink)
    code = cli.main(["-n", "1", "src", "--no-quota", "--no-deleted", "--no-box"])
    assert code == cli.EXIT_ERROR
    assert sink.closed, "the abandoned report must be discarded, not left buffered"
    assert sink.written, "the report was written before the flush failed"


def test_an_undeliverable_diagnosis_still_exits_with_this_tools_code(monkeypatch):
    """`rdu . > out 2>&1` on a full filesystem: nowhere to report the failure.

    The diagnosis write then raises too. Unguarded it escaped the handler and the
    process died at interpreter shutdown with exit **120** and an "ignored
    exception" dump -- the outcome this branch exists to replace, arriving by a
    second route. The exit code has to carry the whole message.
    """
    sink = _FullDisk()
    complaints = _FullDisk()
    monkeypatch.setattr(sys, "stdout", sink)
    monkeypatch.setattr(sys, "stderr", complaints)
    code = cli.main(["-n", "1", "src", "--no-quota", "--no-deleted", "--no-box"])
    assert code == cli.EXIT_ERROR
    assert sink.closed and complaints.closed, "both buffers discarded, nothing to fail at exit"


def test_a_working_stderr_is_never_closed(monkeypatch):
    """Only a stderr that has already failed is discarded."""

    class _Fine(_FullDisk):
        def flush(self):
            return None

    sink = _FullDisk()
    complaints = _Fine()
    monkeypatch.setattr(sys, "stdout", sink)
    monkeypatch.setattr(sys, "stderr", complaints)
    assert cli.main(["-n", "1", "src", "--no-quota", "--no-deleted", "--no-box"]) == (
        cli.EXIT_ERROR
    )
    assert not complaints.closed
    assert any("cannot write the report" in chunk for chunk in complaints.written)
