"""Quota parsing, mount mapping, and -- above all -- snapshot age.

The fixture is real output from RCC Midway3's ``quota`` wrapper
(``/software/bin/quota`` -> ``python3 -m systool.quota9``), trimmed. It is the
form that carries the finding this tool exists to report: a "Quota information
updated at" line whose timestamp can be many minutes old.
"""

import time

from rapidu import quota as quotamod
from rapidu.quota import (
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
labgroup         blocks (group)       71.94T    202.34T    203.34T     none
labgroup         files  (group)     43583258  230900000  231900000     none
otherlab         files  (group)     16118011   17360000   18360000     none
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
    grouprows = [r for r in snap.rows if r.fileset == "labgroup"]
    assert grouprows and all(r.mount == "/project" for r in grouprows)
    assert all(r.scope == "group" for r in grouprows)


def test_rows_for_path_picks_longest_prefix(monkeypatch):
    snap = _parse(monkeypatch, SITE_OUTPUT)
    rows = snap.rows_for_path("/project/labgroup/someone/data")
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
    monkeypatch.setattr(quotamod.socket, "gethostname", lambda: "midway3-0455.example.local")
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
    grouprows = [r for r in snap.rows if r.fileset == "labgroup"]
    assert all(r.mount == "/project" and not r.guessed for r in grouprows)


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


def test_render_disambiguates_same_named_filesets(monkeypatch):
    """A fileset name is unique only within one filesystem.

    On a real site one fileset name can appear on several filesystems. Without
    the mount printed, those rows are indistinguishable and the reader cannot
    tell which number belongs to which filesystem.
    """
    from rapidu.report import render_quota

    text = "\n".join(render_quota(_parse(monkeypatch, SITE_OUTPUT)))
    rcc_lines = [ln for ln in text.splitlines() if ln.strip().startswith("labgroup ")]
    assert len(rcc_lines) == 2
    assert any("/project" in ln for ln in rcc_lines)
    # Every data row must name its filesystem, or say it does not know.
    for ln in text.splitlines():
        if " blocks " in ln or " files " in ln:
            assert ln.rstrip().split()[-1].startswith(("/", "?")), ln


def test_render_flags_a_running_grace_timer(monkeypatch):
    """An expired soft limit stops writes; it cannot be a quiet column."""
    from rapidu.report import render_quota

    over = SITE_OUTPUT.replace(
        "Midway3-home     blocks (user)       679.66M     30.00G     35.00G     none",
        "Midway3-home     blocks (user)        31.00G     30.00G     35.00G    6days",
    )
    text = "\n".join(render_quota(_parse(monkeypatch, over)))
    assert "IN GRACE" in text and "6days" in text


def test_units_are_base_1024():
    """The wrapper's M/G/T are IEC.

    Checked against ground truth: the row reading 127.94M for
    /scratch/midway3/$USER corresponds to a tree du measures at 134,631,936 B.
    Base-1024 predicts 134,154,813 (0.36% off, a stale snapshot); base-1000
    predicts 127,940,000 (5.2% off). Also decisive in the raw output: the block
    column prints "1024.00G" alongside "1.10T", so its G->T rollover is at 1024.
    """
    assert parse_size("127.94M") == int(127.94 * (1 << 20))
    assert parse_size("1024.00G") == 1 << 40
    assert parse_size("1.10T") == int(1.10 * (1 << 40))


# The `blocks` header means 1 KiB units; `space` means human-readable sizes.
# Same numbers, same layout, only the header word differs.
STOCK_BLOCKS_OUTPUT = """Disk quotas for user someone (uid 1000):
     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace
      /dev/sda1  1048576 2097152 4194304            1000    5000   10000
"""


def test_stock_quota_blocks_column_is_kibibytes():
    """`quota` prints 1 KiB blocks; `quota -s` prints sizes with a suffix.

    Reading a `blocks` figure as bytes under-reported by 1024x -- a 4 GiB hard
    limit printed as 4 MiB, which would make every reconciliation against it a
    fabricated gap the size of the quota.
    """
    rows = _parse_stock_quota(STOCK_BLOCKS_OUTPUT)
    blocks = [r for r in rows if r.kind == "blocks"]
    assert len(blocks) == 1
    assert blocks[0].used == 1024 * (1 << 20)  # 1048576 KiB == 1 GiB
    assert blocks[0].soft == 2 * (1 << 30)
    assert blocks[0].hard == 4 * (1 << 30)
    # The file counts are plain integers under either header.
    assert [r.used for r in rows if r.kind == "files"] == [1000]


def test_stock_space_and_blocks_headers_agree_on_the_same_quota():
    """Both spellings of the same quota must produce the same bytes."""
    spaced = [r for r in _parse_stock_quota(STOCK_OUTPUT) if r.kind == "blocks"][0]
    blocked = [r for r in _parse_stock_quota(STOCK_BLOCKS_OUTPUT) if r.kind == "blocks"][0]
    assert spaced.used == blocked.used == 1 << 30
    assert (spaced.soft, spaced.hard) == (blocked.soft, blocked.hard)
