"""Quota parsing, mount mapping, and -- above all -- snapshot age.

The fixture is real output from RCC Midway3's ``quota`` wrapper
(``/software/bin/quota`` -> ``python3 -m systool.quota9``), trimmed. It is the
form that carries the finding this tool exists to report: a "Quota information
updated at" line whose timestamp can be many minutes old.
"""

import time

from slurmdisk import quota as quotamod
from slurmdisk.quota import (
    QuotaRow,
    _disambiguate_mounts,
    _parse_stock_quota,
    parse_size,
)

SITE_OUTPUT = """
Quota information updated at :  2026-08-01 17:32:24
---------------------------------------------------------------------------
fileset          type                   used      quota      limit    grace
---------------- ---------------- ---------- ---------- ---------- --------
Midway2-home     blocks (user)       894.53M     30.00G     35.00G     none
Midway2-home     files  (user)         23184     300000    1000000     none
Midway3-home     blocks (user)       679.66M     30.00G     35.00G     none
Midway3-home     files  (user)         21259     300000    1000000     none
scratch/midway3  blocks (user)       127.94M    100.00G      2.00T     none
scratch/midway3  files  (user)          3598   10000000   20000000     none
---------------- ---------------- ---------- ---------- ---------- --------
>>> Capacity Filesystem: project (Midway3 GPFS mounted at /project)
---------------- ---------------- ---------- ---------- ---------- --------
rcc              blocks (group)       71.94T    202.34T    203.34T     none
rcc              files  (group)     43583258  230900000  231900000     none
otherlab          files  (group)     16118011   17360000   18360000     none
---------------- ---------------- ---------- ---------- ---------- --------
"""

STOCK_OUTPUT = """Disk quotas for user someone (uid 1000):
     Filesystem   space   quota   limit   grace   files   quota   limit   grace
      /dev/sda1  1024M   2048M   4096M            1000    5000   10000
"""


def _parse(monkeypatch, text, rc=0):
    monkeypatch.setattr(quotamod, "_run", lambda cmd, timeout: (rc, text, ""))
    return quotamod.read_quota_command()


def test_parse_size():
    assert parse_size("894.53M") == int(894.53 * (1 << 20))
    assert parse_size("30.00G") == 30 * (1 << 30)
    assert parse_size("0.00K") == 0
    assert parse_size("2.00T") == 2 * (1 << 40)
    assert parse_size("nonsense") is None


def test_snapshot_timestamp_is_parsed(monkeypatch):
    snap = _parse(monkeypatch, SITE_OUTPUT)
    assert snap.available
    assert snap.taken_at is not None
    expected = time.mktime(time.strptime("2026-08-01 17:32:24", "%Y-%m-%d %H:%M:%S"))
    assert snap.taken_at == expected


def test_age_is_reported_not_assumed(monkeypatch):
    """A snapshot without a timestamp must report unknown age, never 'now'."""
    snap = _parse(monkeypatch, SITE_OUTPUT.replace("Quota information updated at", "x"))
    assert snap.age_seconds is None


def test_rows_parsed(monkeypatch):
    snap = _parse(monkeypatch, SITE_OUTPUT)
    home = [r for r in snap.rows if r.fileset == "Midway3-home"]
    assert len(home) == 2
    blocks = [r for r in home if r.kind == "blocks"][0]
    assert blocks.scope == "user"
    assert blocks.used == int(679.66 * (1 << 20))
    assert blocks.soft == 30 * (1 << 30)
    files = [r for r in home if r.kind == "files"][0]
    assert files.used == 21259
    assert files.soft == 300000


def test_group_rows_take_mount_from_section_header(monkeypatch):
    """`mounted at /project` is published by the backend, not hard-coded."""
    snap = _parse(monkeypatch, SITE_OUTPUT)
    rcc = [r for r in snap.rows if r.fileset == "rcc"]
    assert rcc and all(r.mount == "/project" for r in rcc)
    assert all(r.scope == "group" for r in rcc)


def test_rows_for_path_picks_longest_prefix(monkeypatch):
    snap = _parse(monkeypatch, SITE_OUTPUT)
    rows = snap.rows_for_path("/project/rcc/someone/data")
    assert rows and all(r.mount == "/project" for r in rows)
    assert snap.rows_for_path("/nowhere/at/all") == []


def test_ambiguous_guessed_mounts_are_dropped():
    """Midway2-home and Midway3-home must not both claim $HOME silently."""
    rows = [
        QuotaRow("Midway2-home", "blocks", "user", 1, None, None, "", "/home/x", True),
        QuotaRow("Otherclust-home", "blocks", "user", 2, None, None, "", "/home/x", True),
    ]
    _disambiguate_mounts(rows)
    # Neither name matches this host, so neither may keep the mount.
    assert all(r.mount is None for r in rows)
    assert all(r.mount_note for r in rows)


def test_hostname_breaks_the_tie(monkeypatch):
    monkeypatch.setattr(quotamod.socket, "gethostname", lambda: "midway3-0455.rcc.local")
    rows = [
        QuotaRow("Midway2-home", "blocks", "user", 1, None, None, "", "/home/x", True),
        QuotaRow("Midway3-home", "blocks", "user", 2, None, None, "", "/home/x", True),
    ]
    _disambiguate_mounts(rows)
    kept = [r for r in rows if r.mount == "/home/x"]
    assert len(kept) == 1 and kept[0].fileset == "Midway3-home"


def test_published_mounts_are_never_dropped(monkeypatch):
    """Only *guessed* mounts are subject to disambiguation."""
    snap = _parse(monkeypatch, SITE_OUTPUT)
    rcc = [r for r in snap.rows if r.fileset == "rcc"]
    assert all(r.mount == "/project" and not r.guessed for r in rcc)


def test_missing_command_is_reported_not_zero(monkeypatch):
    monkeypatch.setattr(quotamod, "_run", lambda cmd, timeout: (127, "", "command not found"))
    snap = quotamod.read_quota_command()
    assert not snap.available
    assert "not on PATH" in snap.reason
    assert snap.rows == []


def test_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(quotamod, "_run", lambda cmd, timeout: (124, "", "timed out after 45s"))
    snap = quotamod.read_quota_command()
    assert not snap.available
    assert "timed out" in snap.reason


def test_stock_quota_format():
    rows = _parse_stock_quota(STOCK_OUTPUT)
    kinds = {r.kind: r for r in rows}
    assert kinds["blocks"].used == 1024 * (1 << 20)
    assert kinds["files"].used == 1000


def test_usage_fraction():
    r = QuotaRow("f", "files", "group", 92, 100, 110)
    assert abs(r.usage_fraction - 0.92) < 1e-9
    assert QuotaRow("f", "files", "group", 5, None, None).usage_fraction is None
