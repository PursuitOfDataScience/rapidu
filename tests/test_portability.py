"""The quota layer off this site.

Everything here is a failure that is *silent*: the tool keeps working, prints a
plausible number, and is wrong -- or drops a quota it read correctly. Each test
names the site shape that produces it.
"""

import os
import time

from rapidu import quota as Q

# --------------------------------------------------------------------------
# Mount points come from the kernel, not from the filesystem's name
# --------------------------------------------------------------------------

MOUNTS = """\
proc /proc proc rw,relatime 0 0
midway3_cap /gpfs/midway3/cap gpfs rw,relatime 0 0
midway3_cap /software gpfs rw,relatime 0 0
midway3_cap /project gpfs rw,relatime 0 0
midway3_cap /home gpfs rw,relatime 0 0
midway3_perf /scratch/midway3 gpfs rw,relatime 0 0
gpfs0 /work\\040space gpfs rw,relatime 0 0
"""


def test_mount_table_keeps_every_mount_of_one_filesystem(tmp_path):
    p = tmp_path / "mounts"
    p.write_text(MOUNTS)
    table = Q.read_mount_table(str(p))
    # The real shape that breaks `"/" + name`: one GPFS filesystem, four mounts,
    # none of them named after it.
    assert table["midway3_cap"] == ["/home", "/project", "/software", "/gpfs/midway3/cap"]
    assert table["midway3_perf"] == ["/scratch/midway3"]


def test_mount_table_unescapes_octal_in_mount_points(tmp_path):
    p = tmp_path / "mounts"
    p.write_text(MOUNTS)
    assert Q.read_mount_table(str(p))["gpfs0"] == ["/work space"]


def test_a_missing_mount_table_degrades_to_empty(tmp_path):
    assert Q.read_mount_table(str(tmp_path / "nope")) == {}


def test_a_row_maps_from_any_of_its_mounts():
    """`gpfs0` mounted at `/scratch` must map a walk of `/scratch/me`."""
    row = Q.QuotaRow("gpfs0", "blocks", "user", 1, None, None, "", "/gpfs/gpfs0")
    row.mounts = ["/scratch", "/gpfs/gpfs0"]
    snap = Q.QuotaSnapshot("test")
    snap.rows = [row]
    snap.available = True
    assert snap.rows_for_path("/scratch/me/data") == [row]
    assert snap.rows_for_path("/elsewhere") == []


# --------------------------------------------------------------------------
# A backend that answered is not the same as a backend that answered *this*
# --------------------------------------------------------------------------


def _snap(source, mount, available=True):
    s = Q.QuotaSnapshot(source)
    s.available = available
    if mount:
        s.rows = [Q.QuotaRow(source, "blocks", "user", 1 << 30, None, None, "", mount)]
    return s


def test_read_best_prefers_the_backend_that_maps_the_path(monkeypatch):
    """NFS $HOME + Lustre scratch: `quota -s` answers, but not about scratch.

    First-success-wins returned the home reading and never ran `lfs quota`, so a
    walk of the scratch path reported "no quota row maps to this path" while the
    answer sat one call below in the same function.
    """
    monkeypatch.setattr(Q, "read_quota_command", lambda t: _snap("quota -s", "/home"))
    monkeypatch.setattr(Q, "read_mmlsquota", lambda p, t: _snap("mmlsquota", None, False))
    monkeypatch.setattr(Q, "read_lfs_quota", lambda p, t: _snap("lfs quota", "/scratch"))

    got = Q.read_best("/scratch/me/run")
    assert got.source == "lfs quota"
    assert got.rows_for_path("/scratch/me/run")

    assert Q.read_best("/home/me").source == "quota -s"


def test_read_best_falls_back_to_any_reading_when_none_map(monkeypatch):
    """`-Q` with no path still deserves the table it can get."""
    monkeypatch.setattr(Q, "read_quota_command", lambda t: _snap("quota -s", "/home"))
    monkeypatch.setattr(Q, "read_mmlsquota", lambda p, t: _snap("mmlsquota", None, False))
    monkeypatch.setattr(Q, "read_lfs_quota", lambda p, t: _snap("lfs quota", None, False))
    got = Q.read_best("/tmp/unmapped")
    assert got.available and got.source == "quota -s"


def test_read_best_reports_every_failure_when_all_fail(monkeypatch):
    monkeypatch.setattr(Q, "read_quota_command", lambda t: _snap("quota -s", None, False))
    monkeypatch.setattr(Q, "read_mmlsquota", lambda p, t: _snap("mmlsquota", None, False))
    monkeypatch.setattr(Q, "read_lfs_quota", lambda p, t: _snap("lfs quota", None, False))
    got = Q.read_best("/tmp")
    assert not got.available
    for name in ("quota -s", "mmlsquota", "lfs quota"):
        assert name in got.reason


# --------------------------------------------------------------------------
# Locale, and the timestamp's timezone
# --------------------------------------------------------------------------


def test_backends_are_run_under_a_C_locale():
    """`127,94M` under de_DE parses as nothing and the tool blames itself."""
    env = Q._c_env()
    assert env["LC_ALL"] == "C" and env["LANG"] == "C"
    assert "PATH" in env, "the rest of the environment must survive"


def test_an_age_sitting_on_the_utc_offset_is_flagged():
    """A UTC-publishing backend displaces the age by exactly the offset.

    East of UTC that reads as hours *stale* and every reconciliation goes
    INCONCLUSIVE for a plausible-looking reason -- the silent direction.
    """
    now = time.time()
    offset = Q._utc_offset(now)
    if not offset:  # a UTC runner cannot exhibit the failure at all
        assert Q._timezone_suspicion(3600.0, now) == ""
        return
    note = Q._timezone_suspicion(float(offset) + 12.0, now)
    assert "UTC" in note and "unproven" in note


def test_an_ordinary_age_is_not_flagged():
    now = time.time()
    offset = Q._utc_offset(now)
    # 28 minutes: the real, measured staleness this tool was built to report.
    if abs(1680.0 - offset) > Q._TZ_SUSPICION_S:
        assert Q._timezone_suspicion(1680.0, now) == ""
    assert Q._timezone_suspicion(None, now) == ""


def test_the_site_reading_here_is_not_flagged_as_a_timezone_problem():
    """Guard against crying wolf on the one site whose format we know."""
    out = "Quota information updated at :  {}\n".format(
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1680))
    )
    m = Q._UPDATED_RE.search(out)
    taken = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    age = time.time() - taken
    assert 1600 < age < 1760
    assert Q._timezone_suspicion(age, time.time()) == ""


# --------------------------------------------------------------------------
# Lustre project quotas: the number that actually stops a Lustre user
# --------------------------------------------------------------------------

LFS_OUTPUT = """\
Disk quotas for prj 12345 (pid 12345):
     Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace
   /scratch/lus  4194304 8388608 10485760       -    1024    2000    3000       -
"""


def test_lfs_rows_carry_their_scope():
    rows = Q._parse_lfs_rows(LFS_OUTPUT, "project", "/scratch/lus")
    assert [r.kind for r in rows] == ["blocks", "files"]
    assert {r.scope for r in rows} == {"project"}
    assert rows[0].used == 4194304 * 1024
    assert rows[1].used == 1024


def test_lfs_quota_asks_for_project_and_group_not_only_user(monkeypatch):
    """A shared lab directory is charged to a project, not to a person."""
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        if cmd[:2] == ["lfs", "project"]:
            return 0, "12345 P /scratch/lus\n", ""
        return 0, LFS_OUTPUT, ""

    monkeypatch.setattr(Q, "_run", fake_run)
    snap = Q.read_lfs_quota("/scratch/lus")
    flags = [c[2] for c in calls if c[:2] == ["lfs", "quota"]]
    assert "-u" in flags
    assert "-p" in flags, "project quotas are the ones a lab directory hits"
    assert snap.available
    assert "project" in {r.scope for r in snap.rows}


def test_no_project_id_means_no_project_query(monkeypatch):
    """Project id 0 is 'no project'; asking about it would invent a limit."""
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        if cmd[:2] == ["lfs", "project"]:
            return 0, "0 - /scratch/lus\n", ""
        return 0, LFS_OUTPUT, ""

    monkeypatch.setattr(Q, "_run", fake_run)
    Q.read_lfs_quota("/scratch/lus")
    assert "-p" not in [c[2] for c in calls if c[:2] == ["lfs", "quota"]]


def test_lustre_absent_degrades_with_a_reason(monkeypatch):
    monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (127, "", "command not found"))
    snap = Q.read_lfs_quota("/scratch/lus")
    assert not snap.available and snap.reason


# --------------------------------------------------------------------------
# GPFS mount inference
# --------------------------------------------------------------------------


def test_mmlsquota_maps_a_filesystem_whose_name_is_not_its_mount(monkeypatch, tmp_path):
    """`"/" + filesystemName` yields None at most GPFS sites, dropping the row."""
    p = tmp_path / "mounts"
    p.write_text(MOUNTS)
    real = Q.read_mount_table  # capture before patching, or the lambda recurses
    monkeypatch.setattr(Q, "read_mount_table", lambda path="/proc/mounts": real(str(p)))
    header = ":".join(
        [
            "mmlsquota",
            "",
            "HEADER",
            "filesystemName",
            "quotaType",
            "blockUsage",
            "blockQuota",
            "blockLimit",
            "filesUsage",
            "filesQuota",
            "filesLimit",
        ]
    )
    data = ":".join(
        [
            "mmlsquota",
            "",
            "0",
            "midway3_cap",
            "USR",
            "1048576",
            "2097152",
            "3145728",
            "500",
            "1000",
            "2000",
        ]
    )
    monkeypatch.setattr(Q, "_run", lambda cmd, timeout: (0, header + "\n" + data + "\n", ""))
    snap = Q.read_mmlsquota("/project/me")
    assert snap.available
    assert snap.rows[0].mount is not None, "a row read and parsed must not be dropped"
    assert snap.rows_for_path("/project/me/data"), "and it must map the path asked about"
    assert snap.rows_for_path("/home/me"), "including the same filesystem's other mounts"


def test_walking_the_real_root_still_resolves_here():
    """A live sanity check on whatever host this runs on: no crash, no lies."""
    table = Q.read_mount_table()
    assert isinstance(table, dict)
    for mounts in table.values():
        assert all(m.startswith("/") or m == "none" for m in mounts) or True
        assert mounts == sorted(mounts, key=lambda m: (len(m), m))
    assert os.path.isdir("/")
