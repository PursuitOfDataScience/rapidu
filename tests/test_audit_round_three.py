"""Regression tests for the defects found in audit round three.

Every test here corresponds to a numbered finding, and every one of them would
have failed before the fix. They exist as a group because §D of that audit made a
point worth keeping in front of us: the suite was green, 214 tests passed,
``ruff`` and ``mypy`` were clean, and six wrong-number defects survived all of it.

The pattern behind that, repeated four times over, was a test asserting the field
the *previous* round had fixed and not the field beside it -- the ``mmlsquota``
test checked the mount and never the scope; the ``lfs`` test checked the scope and
never the mount, and passed a path that happened to look like a mount, which hid
the bug it was closest to. So these tests deliberately assert the neighbour.
"""

import json
import os
import subprocess
import sys
import threading

import pytest
from conftest import NEEDS_REAL_UNLINK, UNLINK_HIDES_ENTRY

from rapidu import cli, report, ui
from rapidu import deleted as deletedmod
from rapidu import quota as quotamod
from rapidu import reconcile as rc
from rapidu.quota import QuotaRow, QuotaSnapshot
from rapidu.walk import walk


class _Stream:
    """A stdout stand-in whose ``encoding`` we control.

    ``io.StringIO`` has no assignable ``encoding``, and the whole point here is
    what happens when the stream reports one the glyphs do not fit in.
    """

    def __init__(self, encoding):
        self.encoding = encoding
        self._buf = []

    def write(self, text):
        self._buf.append(text)

    def isatty(self):
        return False

    def getvalue(self):
        return "".join(self._buf)


# ---------------------------------------------------------------------------
# #18 -- mmlsquota publishes USR/GRP/FILESET, and nothing matched "user"
# ---------------------------------------------------------------------------

_MM_HEADER = (
    "mmlsquota::HEADER:version:reserved:reserved:filesystemName:quotaType:"
    "filesetName:blockUsage:blockQuota:blockLimit:blockGrace:filesUsage:"
    "filesQuota:filesLimit:filesGrace:"
)


def _mm_row(quota_type, fileset="root", block_grace="none", files_grace="none"):
    return (
        "mmlsquota::0:1:::gpfs0:{}:{}:104857600:209715200:209715200:{}:5000:10000:10000:{}:".format(
            quota_type, fileset, block_grace, files_grace
        )
    )


def _mmlsquota(monkeypatch, lines):
    text = "\n".join([_MM_HEADER] + lines)
    monkeypatch.setattr(quotamod, "_run", lambda cmd, timeout: (0, text, ""))
    return quotamod.read_mmlsquota("/scratch")


def test_mmlsquota_scopes_are_normalised_to_this_codebases_vocabulary(monkeypatch):
    """``USR`` lowercases to ``usr``, which every consumer failed to match.

    The consequence was not cosmetic: ``reconcile`` compared a *personal* quota
    against every file in a shared tree, suppressed the note that says only your
    own bytes were counted, and offered a candidate cause naming a group that did
    not exist -- the filesystem name, presented as a group.
    """
    snap = _mmlsquota(monkeypatch, [_mm_row("USR"), _mm_row("GRP"), _mm_row("FILESET", "dachxiu")])
    assert {r.scope for r in snap.rows} == {"user", "group", "fileset"}


def test_mmlsquota_user_rows_reach_the_user_preference_in_pick_row(monkeypatch):
    """The scope value must actually satisfy the consumer that reads it."""
    snap = _mmlsquota(monkeypatch, [_mm_row("GRP"), _mm_row("USR")])
    row, _notes = rc._pick_row(snap.rows_for_path("/scratch") or snap.rows, "blocks", "/scratch")
    assert row is not None and row.scope == "user"


def test_mmlsquota_fileset_rows_are_named_by_their_fileset(monkeypatch):
    """A fileset-scoped row identifies a fileset, not the whole filesystem."""
    snap = _mmlsquota(monkeypatch, [_mm_row("FILESET", "dachxiu")])
    assert all(r.fileset == "dachxiu" for r in snap.rows)


# ---------------------------------------------------------------------------
# #26 -- the grace timer, the only "writes stop soon" warning in the tool
# ---------------------------------------------------------------------------


def test_mmlsquota_carries_the_grace_timer(monkeypatch):
    """``blockGrace`` was in the parsed record and thrown away.

    ``render_quota`` paints ``! IN GRACE, <n> left`` in red and it could never
    fire on a GPFS-native site.
    """
    snap = _mmlsquota(monkeypatch, [_mm_row("USR", block_grace="6days", files_grace="3days")])
    assert {r.kind: r.grace for r in snap.rows} == {"blocks": "6days", "files": "3days"}
    assert "IN GRACE" in "\n".join(report.render_quota(snap))


def test_a_grace_column_meaning_no_grace_is_not_a_warning(monkeypatch):
    """Every backend spells "no timer" differently; none may raise the alarm.

    ``none`` is truthy, so leaving it verbatim made a 2.9%-full home directory set
    ``EXIT_ATTENTION``. Normalising at the parser is what stops each consumer
    having to know every backend's vocabulary.
    """
    snap = _mmlsquota(monkeypatch, [_mm_row("USR", block_grace="none", files_grace="-")])
    assert all(r.grace == "" for r in snap.rows)
    assert not cli._quota_needs_attention(snap)


# ---------------------------------------------------------------------------
# #16 -- lfs rows carried the walked path as their mount point
# ---------------------------------------------------------------------------

_LFS = """Disk quotas for usr someone (uid 1000):
     Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace
/lus/scratch  1048576  2097152 2097152       -    1000   10000   10000   6days
"""

# `lfs` wraps when the filesystem name is long: name on its own line, figures on
# the next. Requiring nine fields on one line returned zero rows at those sites.
_LFS_WRAPPED = """Disk quotas for usr someone (uid 1000):
     Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace
/lustre/scratch/a/very/long/filesystem/name
              1048576  2097152 2097152       -    1000   10000   10000       -
"""


def test_lfs_rows_carry_the_filesystem_as_their_mount_not_the_queried_path():
    """The mount must be the filesystem's, or ``SUBTREE`` becomes unreachable.

    ``reconcile`` decides whether a walk covers the whole quota'd tree by
    comparing the walk root against ``row.mount``. When the mount *was* the
    walked path that test was true by construction, so the verdict that exists to
    say "you walked a subdirectory of a much larger quota'd filesystem, the
    difference is expected" could never fire on Lustre -- and every ``rdu -a
    <subdir>`` reported the rest of the filesystem as a difference to explain.

    Note the path passed here is deliberately *not* the filesystem: the old test
    for this function passed one that was, which is why asserting the mount would
    have looked correct even then.
    """
    rows = quotamod._parse_lfs_rows(_LFS, "user", "/lus/scratch/me/experiment/run-14")
    assert rows, "the fixture must parse"
    assert all(r.mount == "/lus/scratch" for r in rows)
    assert all(r.mount != "/lus/scratch/me/experiment/run-14" for r in rows)


def test_lfs_rows_keep_their_scope_and_their_mount_together():
    """Assert the neighbour: both fields, in one test, so neither can drift."""
    for scope in ("user", "group", "project"):
        rows = quotamod._parse_lfs_rows(_LFS, scope, "/lus/scratch/sub/dir")
        assert [r.scope for r in rows] == [scope, scope]
        assert [r.mount for r in rows] == ["/lus/scratch", "/lus/scratch"]


def test_lfs_grace_is_carried():
    rows = quotamod._parse_lfs_rows(_LFS, "user", "/lus/scratch/x")
    assert {r.kind: r.grace for r in rows} == {"blocks": "", "files": "6days"}


def test_lfs_wrapped_rows_still_parse():
    """A long filesystem name wraps the data row; it is not a reason to give up."""
    rows = quotamod._parse_lfs_rows(_LFS_WRAPPED, "user", "/lustre/scratch/a/very/long/x")
    assert len(rows) == 2
    assert rows[0].used == 1048576 * 1024
    assert all(r.mount == "/lustre/scratch/a/very/long/filesystem/name" for r in rows)


def test_a_lustre_subtree_walk_reports_subtree_not_a_difference(tmp_path, monkeypatch):
    """End to end: the verdict must be SUBTREE, with the gap left unclaimed."""
    root = str(tmp_path / "run-14")
    os.makedirs(root)
    with open(os.path.join(root, "f"), "wb") as fh:
        fh.write(b"x" * 4096)
    res = walk(root, threads=1, depth=1)

    snap = QuotaSnapshot("lfs quota")
    snap.rows = quotamod._parse_lfs_rows(_LFS, "user", root)
    # Point the row's mount at an ancestor of the walk, which is the real shape.
    for r in snap.rows:
        r.mount = str(tmp_path)
        r.mounts = [str(tmp_path)]
    snap.available = True
    snap.taken_at = snap.read_at

    from rapidu.walk import SettleCheck

    rec = rc.reconcile(res, SettleCheck(), snap, deletedmod.DeletedScan(), "blocks")
    assert rec.verdict == rc.SUBTREE
    assert rec.notes, "SUBTREE must explain which quota it was measured against"


def test_the_subtree_verdict_prints_its_own_note():
    """The note naming the fileset, mount and scope was built and never rendered.

    ``render_reconcile``'s SUBTREE branch printed the verdict line and
    ``continue``d past ``rec.notes``. SUBTREE is the most common verdict on a real
    cluster, so the one line saying *which* quota governs you was the line never
    shown, on nearly every run.
    """
    rec = rc.Reconciliation("blocks")
    rec.verdict = rc.SUBTREE
    rec.row = QuotaRow("rcc", "blocks", "group", 100, 200, 200, "", "/project")
    rec.walk_value = 10
    rec.quota_value = 100
    rec.notes.append("the rcc quota covers /project (group-scoped); this walk covers only /x")
    text = "\n".join(report.render_reconcile([rec], ui.resolve_style("never")))
    assert "the rcc quota covers /project" in text


# ---------------------------------------------------------------------------
# #17 -- several filesets on one published mount
# ---------------------------------------------------------------------------


def _project_rows():
    """The live shape on this cluster: three labs sharing /project."""
    return [
        QuotaRow("aaz", "blocks", "group", 3, 5, 5, "", "/project"),
        QuotaRow("dachxiu", "blocks", "group", 10, 10, 10, "", "/project"),
        QuotaRow("rcc", "blocks", "group", 63, 202, 203, "", "/project"),
    ]


@pytest.mark.parametrize("lab", ["aaz", "dachxiu", "rcc"])
def test_the_fileset_the_path_sits_in_wins_the_tie(lab):
    """Parse order must not decide which lab's quota you are measured against.

    ``rows_for_path`` returns every row tied for the longest matching mount and
    ``_pick_row`` took ``matching[0]``, so a user whose own fileset was at 99.9%
    was reconciled against a sibling lab's 31%-full one and told their tree was a
    rounding error. GPFS independent filesets are the standard way to give each
    lab its own quota inside one filesystem, and they all share one mount.
    """
    row, _notes = rc._pick_row(_project_rows(), "blocks", "/project/{}/someone/data".format(lab))
    assert row is not None and row.fileset == lab


def test_an_unresolvable_tie_says_so_rather_than_choosing_quietly():
    """A guess must be labelled. Silence is what made the wrong lab invisible."""
    row, notes = rc._pick_row(_project_rows(), "blocks", "/project/unknown-lab/x")
    assert row is not None
    assert notes and "not because it is known to be the right one" in notes[0]


def test_a_published_mount_collision_is_reported_like_a_guessed_one():
    """The asymmetry was the bug: guessed collisions warned, published ones did not."""
    rec = rc.Reconciliation("blocks")
    _row, notes = rc._pick_row(_project_rows(), "blocks", "/project")
    rec.notes.extend(notes)
    assert rec.notes


# ---------------------------------------------------------------------------
# #25 -- stock `quota` rows parsed and then dropped for want of a mount
# ---------------------------------------------------------------------------

_NFS_QUOTA = """Disk quotas for user someone (uid 1000):
     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace
 nfs-srv:/export 1048576 2097152 2097152            5000  100000  100000
"""


def test_a_device_name_that_is_not_a_path_still_parses():
    """``server:/export`` is the normal spelling of an NFS device.

    The loop broke on ``not parts[0].startswith("/")``, so the whole table was
    rejected before a single number was read -- at every NFS and CIFS site, which
    is the fallback path, the one that has to work when nothing else does.
    """
    rows = quotamod._parse_stock_quota(_NFS_QUOTA)
    assert len(rows) == 2
    blocks = [r for r in rows if r.kind == "blocks"][0]
    assert blocks.used == 1048576 * 1024  # the `blocks` header means KiB
    assert [r for r in rows if r.kind == "files"][0].used == 5000


def test_a_device_is_mapped_to_its_mount_through_proc_mounts():
    """``isdir`` on a device name maps nothing; /proc/mounts is keyed by it."""
    table = quotamod.read_mount_table()
    dev = next((d for d, m in sorted(table.items()) if d.startswith("/dev/") and m), None)
    if dev is None:
        pytest.skip("no block device in /proc/mounts to test against")
    text = (
        "Disk quotas for user someone (uid 1000):\n"
        "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
        "  {} 1048576 2097152 2097152            5000  100000  100000\n".format(dev)
    )
    rows = quotamod._parse_stock_quota(text)
    assert rows and rows[0].mount == table[dev][0]

    snap = QuotaSnapshot("quota -s")
    snap.rows = rows
    snap.available = True
    assert snap.rows_for_path(os.path.join(table[dev][0], "anything"))


# ---------------------------------------------------------------------------
# #30 -- the quota timeout bounded each subprocess, not the whole read
# ---------------------------------------------------------------------------


def test_the_quota_timeout_bounds_the_whole_read(monkeypatch):
    """Six serial backends x 45s was 225s of silence with no spinner.

    ``lfs quota`` hanging is what a Lustre client does when an MDS is degraded,
    which is the same afternoon someone reaches for this tool.
    """
    import time

    calls = []

    def hang(cmd, timeout):
        calls.append(cmd[0])
        time.sleep(max(0.0, timeout))
        return (124, "", "timed out")

    monkeypatch.setattr(quotamod, "_run", hang)
    t0 = time.time()
    quotamod.read_best("/some/path", 0.6)
    elapsed = time.time() - t0
    assert elapsed < 1.2, "took {:.2f}s for {} backends; the budget is not total".format(
        elapsed, len(calls)
    )


# ---------------------------------------------------------------------------
# #16/#18 corollary -- a live vendor query must publish a timestamp
# ---------------------------------------------------------------------------


def test_live_vendor_backends_publish_a_timestamp(monkeypatch):
    """Otherwise ``GAP`` -- and ``EXIT_ATTENTION`` -- are unreachable.

    ``age_seconds`` was permanently ``None`` for ``mmlsquota`` and ``lfs``, so
    reconcile always appended its "published no timestamp" blocker and every
    verdict on a GPFS-native or Lustre site was downgraded to INCONCLUSIVE. The
    exit code was therefore constant, and a cron job could not use it.
    """
    snap = _mmlsquota(monkeypatch, [_mm_row("USR")])
    assert snap.taken_at is not None
    assert snap.age_seconds is not None and snap.age_seconds < 60
    assert snap.time_note, "a live read should still say the accounting can lag"


# ---------------------------------------------------------------------------
# #20 -- --json published the half-counted directories text refuses to
# ---------------------------------------------------------------------------


def test_json_rankings_honour_the_interrupt_filter(tmp_path):
    """One result object must not have two honesty policies.

    ``to_json`` built its three rankings without ``finished_only``, so a consumer
    ranking on ``top_by_size`` -- the reason the key exists -- got truncated
    subtrees, with ``"interrupted": true`` sitting elsewhere in the document.
    """
    root = str(tmp_path / "t")
    for name in ("alpha", "beta"):
        d = os.path.join(root, name)
        os.makedirs(d)
        for j in range(4):
            with open(os.path.join(d, "f%d" % j), "wb") as fh:
                fh.write(b"x" * 4096)
    res = walk(root, threads=1, depth=1)

    # Mark one subtree unfinished, exactly as an interrupt would.
    res.partial = True
    res.finished_tops = {"beta"}

    from rapidu.walk import SettleCheck

    doc = report.to_json(res, SettleCheck(), None, None, None, 10)
    for key in ("top_by_size", "top_by_inodes", "top_by_density"):
        names = {os.path.basename(e["path"]) for e in doc["walk"][key]}
        assert "alpha" not in names, "{} published a truncated subtree".format(key)
    assert doc["walk"]["interrupted"] is True

    text = report.render_entries(res, 10, False, ui.resolve_style("never"))
    assert not any("alpha" in line for line in text)


def test_to_json_is_exercised_at_all(tmp_path):
    """``to_json`` had zero references in the whole suite before this.

    That is why #20 shipped: not "no test for the interrupted case" but no test
    for the function.
    """
    root = str(tmp_path / "t")
    os.makedirs(root)
    with open(os.path.join(root, "f"), "wb") as fh:
        fh.write(b"x" * 1024)
    from rapidu.walk import SettleCheck

    doc = report.to_json(walk(root, threads=1), SettleCheck(), None, None, None, 5)
    assert json.loads(json.dumps(doc))["walk"]["files"] == 1


# ---------------------------------------------------------------------------
# #21 -- walking / reported every top-level directory twice
# ---------------------------------------------------------------------------


def test_walking_the_real_root_does_not_double_count(tmp_path):
    """``root + os.sep + part`` gives "//etc" when root is "/".

    The child's own key is built as ``d + os.sep + name`` with ``d`` already
    ending in a separator, so the two disagreed by one slash and produced two
    ``Entry`` objects per directory. Both ``relpath`` to the same displayed name,
    so the listing showed every top-level entry twice, and
    ``os.path.dirname("//etc")`` is ``"//"``, which silently suppressed the
    remainder row on the one root where it matters most.
    """
    res = walk("/", threads=8, depth=1, one_file_system=True)
    keys = [e.path for e in res.dir_agg.values()]
    assert not any(k.startswith("//") for k in keys), "double-slash keys are back"
    shown = [os.path.relpath(k, "/") for k in keys]
    assert len(shown) == len(set(shown)), "duplicate entries at /"
    assert report._entries_partition_tree(res), "the remainder row is suppressed again"


def test_a_non_root_walk_is_unaffected(tmp_path):
    """The fix must not perturb the ordinary case."""
    root = str(tmp_path / "t")
    os.makedirs(os.path.join(root, "sub"))
    with open(os.path.join(root, "sub", "f"), "wb") as fh:
        fh.write(b"x" * 4096)
    res = walk(root, threads=1, depth=1)
    keys = [e.path for e in res.dir_agg.values()]
    assert os.path.join(root, "sub") in keys
    assert not any("//" in k for k in keys)


# ---------------------------------------------------------------------------
# #22 -- rendered output must be encodable in the stream's encoding
# ---------------------------------------------------------------------------


def _material_allocation_result(tmp_path):
    """A result whose allocation ratio is large enough to print the warning."""
    root = str(tmp_path / "t")
    os.makedirs(root)
    with open(os.path.join(root, "f"), "wb") as fh:
        fh.write(b"x" * 4096)
    res = walk(root, threads=1, depth=1)
    # Force the ratio rather than depend on the filesystem's allocation unit.
    res.apparent = 4 << 20
    res.size = 64 << 20
    res.padded_files = 1000
    res.padded_apparent = 4 << 20
    res.padded_alloc = 64 << 20
    return res


def test_ascii_mode_output_is_actually_ascii(tmp_path):
    """``--ascii`` is the documented escape hatch and it leaked U+2014.

    Three strings bypassed ``ui.dash(style)``. The reason this is not merely
    cosmetic is the next test.
    """
    res = _material_allocation_result(tmp_path)
    style = ui.resolve_style("never", ascii_only=True)
    from rapidu.walk import SettleCheck

    lines = report.render_walk(res, SettleCheck(), 10, style=style)
    lines += report.render_allocation(res, style)
    lines += report.render_compact(res, SettleCheck(), 10, False, style)
    body = "\n".join(lines)
    assert "allocated for" in body, "the em-dash line must be present or this proves nothing"
    body.encode("ascii")  # raises if any glyph leaked


def test_every_rendered_line_is_encodable_in_an_ascii_stream(tmp_path):
    """The real failure was a crash, not a blemish.

    On RHEL8's ``/usr/bin/python3`` -- 3.6.8, the interpreter this package names
    as the reason for its stdlib-only constraint -- ``LC_ALL=C`` gives
    ``sys.stdout.encoding == 'ANSI_X3.4-1968'``, and PEP 538's C-locale coercion
    is 3.7+. Printing U+2014 there is a ``UnicodeEncodeError`` traceback.
    ``LC_ALL=C`` is the default in a great many batch scripts and cron
    environments.
    """
    res = _material_allocation_result(tmp_path)
    stream = _Stream("ANSI_X3.4-1968")
    style = ui.resolve_style("never", stream=stream)
    assert not style.unicode, "an ASCII stream must not be given the unicode glyph set"

    from rapidu.walk import SettleCheck

    lines = report.render_walk(res, SettleCheck(), 10, style=style)
    lines += report.render_allocation(res, style)
    for line in lines:
        line.encode("ascii")


def test_the_environment_cannot_override_the_streams_encoding(monkeypatch):
    """``LC_ALL=C.UTF-8`` on 3.6 leaves stdout ASCII while the env says utf.

    Falling back to ``LC_ALL``/``LANG`` let the environment promise what the
    stream could not deliver, and the disagreement crashed on U+00B7 from
    ``ui.sep`` before the em dash was even reached.
    """
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert ui._supports_unicode(_Stream("ANSI_X3.4-1968")) is False
    # ...and a stream that really can encode them is still allowed to.
    assert ui._supports_unicode(_Stream("utf-8")) is True


def test_the_glyph_probe_covers_every_glyph_the_module_emits():
    """A probe testing a subset clears a stream that then crashes on the rest."""
    for glyph in (ui._BAR_FULL, ui._BAR_EMPTY, ui._BAR_HATCH, "—", "·", "─"):
        assert glyph in ui._GLYPHS, "{!r} is emitted but not probed".format(glyph)
    for frame in ui._SPIN:
        assert frame in ui._GLYPHS


# ---------------------------------------------------------------------------
# #23 -- a symlink to a directory was rejected in every spelling
# ---------------------------------------------------------------------------


def test_a_symlinked_root_is_walked(tmp_path):
    """``$SCRATCH`` symlinked into ``$HOME`` is a standard site layout.

    ``_resolve_paths`` accepted the path with ``os.path.isdir`` (which follows)
    and ``walk`` then rejected it with ``os.lstat`` (which does not), so the two
    halves of the tool disagreed about whether the argument was a directory.
    ``rdu ~/scratch`` -- the README's own second example -- failed with "is not a
    directory" and exit 2.
    """
    real = tmp_path / "real"
    os.makedirs(str(real))
    with open(str(real / "f"), "wb") as fh:
        fh.write(b"x" * 4096)
    link = str(tmp_path / "link")
    os.symlink(str(real), link)

    resolved, _refused = cli._resolve_paths([link])
    assert resolved == [str(real)]
    res = walk(resolved[0], threads=1, depth=1)
    assert res.files == 1


def test_a_symlinked_root_matches_du_following_the_link(tmp_path):
    real = tmp_path / "real"
    os.makedirs(str(real))
    for j in range(8):
        with open(str(real / ("f%d" % j)), "wb") as fh:
            fh.write(b"x" * 4096)
    link = str(tmp_path / "link")
    os.symlink(str(real), link)

    out = subprocess.check_output(
        ["du", "-s", "--block-size=1", link + "/"], universal_newlines=True
    )
    walked = walk(cli._resolve_paths([link])[0][0], threads=1).size
    assert walked == int(out.split()[0])


def test_a_plain_file_is_still_rejected(tmp_path):
    """Resolving symlinks must not start accepting non-directories."""
    f = tmp_path / "a-file"
    f.write_text("hello")
    assert cli._resolve_paths([str(f)]) == ([], 1), "a refused path is counted, not just logged"


# ---------------------------------------------------------------------------
# #24 -- -c silently ignored --one-file-system
# ---------------------------------------------------------------------------


def test_count_only_honours_one_file_system(tmp_path):
    """The two flags the tool steers you to combine were the pair that disagreed.

    ``--one-file-system`` is documented as "use this when reconciling against a
    per-filesystem quota" and ``-i -c`` is the hint the tool itself prints for
    the inode question. The count path never called ``stat``, so it never read
    ``st_dev`` and the flag was accepted and ignored.

    ``/dev`` is used because it reliably carries foreign mounts beneath it.
    """
    plain_c = walk("/dev", threads=4, depth=1, count_only=True)
    ofs_c = walk("/dev", threads=4, depth=1, count_only=True, one_file_system=True)
    ofs_full = walk("/dev", threads=4, depth=1, one_file_system=True)
    if plain_c.inodes == ofs_full.inodes:
        pytest.skip("/dev has no foreign mounts on this host")
    assert ofs_c.inodes < plain_c.inodes, "-c ignored --one-file-system"
    assert ofs_c.inodes == ofs_full.inodes, "-c -x disagrees with the full walk's -x"


def test_count_only_without_the_flag_still_crosses(tmp_path):
    """The fix must not turn the flag on by accident.

    `/dev` is live: ptys come and go, and two walks a moment apart counted 12,097
    and 12,012 -- an 85-entry difference that is the directory changing, not the
    walk disagreeing. The property under test is that neither walk stopped at a
    device boundary, so that is what is asserted: the counts agree to within the
    churn, and the crossing walk is strictly larger than the `-x` one. A `-c` walk
    that had silently gained `--one-file-system` would lose all of `/dev/pts` and
    `/dev/shm`, which no churn tolerance could hide.
    """
    plain = walk("/dev", threads=4, depth=1, count_only=True)
    full = walk("/dev", threads=4, depth=1)
    assert abs(plain.inodes - full.inodes) <= max(64, full.inodes // 100), (
        plain.inodes,
        full.inodes,
    )
    ofs = walk("/dev", threads=4, depth=1, one_file_system=True)
    if ofs.inodes == full.inodes:
        pytest.skip("/dev has no foreign mounts on this host")
    assert plain.inodes > ofs.inodes, "-c stopped at the device boundary"


# ---------------------------------------------------------------------------
# #32 -- -Q with a path it can map none of
# ---------------------------------------------------------------------------


def _two_row_snapshot():
    snap = QuotaSnapshot("quota -s")
    snap.rows = [
        QuotaRow("home", "blocks", "user", 1, 2, 2, "", "/home"),
        QuotaRow("scratch", "blocks", "user", 1, 2, 2, "", "/scratch"),
    ]
    snap.available = True
    snap.taken_at = snap.read_at
    return snap


def test_quota_only_says_so_when_a_path_maps_nothing():
    """Falling through to every row looked authoritative and said nothing.

    The reconciler says "no quota row maps to X" in exactly this situation.
    """
    text = "\n".join(report.render_quota(_two_row_snapshot(), ["/nowhere/at/all"]))
    assert "no quota row maps to /nowhere/at/all" in text


def test_quota_only_still_filters_when_a_path_does_map():
    text = "\n".join(report.render_quota(_two_row_snapshot(), ["/home/someone"]))
    assert "home" in text
    assert "scratch" not in text


# ---------------------------------------------------------------------------
# #31 -- fileset names truncated to 16 columns with no marker
# ---------------------------------------------------------------------------


def test_two_long_fileset_names_on_one_mount_stay_distinguishable():
    """The mount column cannot disambiguate a collision that is *on* one mount."""
    snap = QuotaSnapshot("quota -s")
    snap.rows = [
        QuotaRow("very-long-project-alpha", "blocks", "group", 1, 2, 2, "", "/project"),
        QuotaRow("very-long-project-beta", "blocks", "group", 9, 2, 2, "", "/project"),
    ]
    snap.available = True
    lines = [ln for ln in report.render_quota(snap) if " blocks " in ln]
    assert len(lines) == 2
    assert lines[0] != lines[1], "two filesets rendered identically"


def test_usage_over_the_limit_is_marked_not_just_clamped():
    """``ui.bar`` clamps at full, so 450% and 100% drew the same bar."""
    snap = QuotaSnapshot("quota -s")
    snap.rows = [QuotaRow("f", "blocks", "group", 9, 2, 2, "", "/project")]
    snap.available = True
    assert "OVER" in "\n".join(report.render_quota(snap))


def test_a_user_row_and_a_group_row_are_distinguishable():
    """On Lustre all three scopes come back for one filesystem at once."""
    snap = QuotaSnapshot("lfs quota")
    snap.rows = [
        QuotaRow("scratch", "blocks", "user", 1, 4, 4, "", "/scratch"),
        QuotaRow("scratch", "blocks", "group", 2, 4, 4, "", "/scratch"),
        QuotaRow("scratch", "blocks", "project", 3, 4, 4, "", "/scratch"),
    ]
    snap.available = True
    lines = [ln for ln in report.render_quota(snap) if " blocks " in ln]
    assert len(set(lines)) == 3


# ---------------------------------------------------------------------------
# #33 -- exit codes and option validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["-d", "0"],
        ["-d", "-1"],
        ["-n", "-1"],
        ["--settle-window", "-5"],
        ["--max-dirs-per-sec", "-3"],
        ["--quota-timeout", "0"],
    ],
)
def test_nonsensical_option_values_are_rejected(argv, capsys):
    """Each of these silently disabled a feature rather than adjusting it.

    ``-d 0`` printed "0 entries" and exited 0, as though the tree were empty.
    ``--settle-window -5`` pushed the cutoff into the future and turned off the
    unsettled-tree check, which is one of the tool's four reasons to exist.
    ``--max-dirs-per-sec -3`` turned off the rate limiter on a shared filesystem.
    """
    with pytest.raises(SystemExit) as exc:
        cli.main([os.path.dirname(__file__)] + argv)
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_a_near_full_quota_sets_the_attention_exit_code():
    """``rdu -Q`` exited 0 with a fileset at 99.9% and would have with a timer.

    ``EXIT_ATTENTION`` fired only when the backend was *unavailable*, so the one
    invocation cheap enough to run from cron reported "fine" in the two states
    that mean writes are about to stop.
    """
    snap = QuotaSnapshot("quota -s")
    snap.rows = [QuotaRow("dachxiu", "blocks", "group", 999, 1000, 1000, "", "/project")]
    snap.available = True
    assert cli._quota_needs_attention(snap, ["/project/dachxiu"])


def test_a_running_grace_timer_sets_the_attention_exit_code():
    snap = QuotaSnapshot("quota -s")
    snap.rows = [QuotaRow("f", "blocks", "user", 1, 1000, 1000, "6days", "/home")]
    snap.available = True
    assert cli._quota_needs_attention(snap, ["/home/someone"])


def test_a_healthy_quota_does_not():
    snap = QuotaSnapshot("quota -s")
    snap.rows = [QuotaRow("f", "blocks", "user", 29, 1000, 1000, "", "/home")]
    snap.available = True
    assert not cli._quota_needs_attention(snap, ["/home/someone"])


def test_inconclusive_reaches_the_attention_exit_code(tmp_path, monkeypatch, capsys):
    """The previous assertion here (INCONCLUSIVE != GAP) tested nothing.

    ``cmd_walk`` checked only for GAP, and per #16/#18 the vendor backends could
    produce nothing *but* INCONCLUSIVE, so on any GPFS-native or Lustre site the
    exit code was constant and a cron job could not use it. This drives the real
    exit path with a real reconciliation.
    """
    root = str(tmp_path / "t")
    os.makedirs(root)
    with open(os.path.join(root, "f"), "wb") as fh:
        fh.write(b"x" * 4096)

    snap = QuotaSnapshot("quota -s")
    # A quota figure far above the walk, on a mount equal to the walk root, so
    # the verdict is a real difference rather than SUBTREE...
    row = QuotaRow("fs", "blocks", "user", 8 << 30, 16 << 30, 16 << 30, "", root)
    files = QuotaRow("fs", "files", "user", 5_000_000, 9_000_000, 9_000_000, "", root)
    snap.rows = [row, files]
    snap.available = True
    snap.taken_at = None  # ...and no timestamp, which is what forces INCONCLUSIVE

    monkeypatch.setattr(quotamod, "read_best", lambda path, timeout: snap)
    monkeypatch.setattr(deletedmod, "scan", lambda *a, **k: deletedmod.DeletedScan())

    code = cli.main(["-a", root, "--color", "never", "--no-settle-check"])
    body = capsys.readouterr().out
    assert "INCONCLUSIVE" in body, body[-600:]
    assert code == cli.EXIT_ATTENTION, "INCONCLUSIVE must not exit 0"


# ---------------------------------------------------------------------------
# #29 -- multi-path -a reconciled every path against paths[0]'s backend
# ---------------------------------------------------------------------------


def test_each_path_gets_its_own_quota_backend(tmp_path, monkeypatch):
    """``read_best`` chooses *the backend that can map the path it was given*.

    Hoisting one call out of the per-path loop meant ``rdu -a ~ /scratch/lustre``
    picked a backend for ``$HOME`` and then reconciled the Lustre path against it
    -- exactly the multi-filesystem failure the first-success-wins fix set out to
    remove, reintroduced one layer up.
    """
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    for d in (a, b):
        os.makedirs(d)
        with open(os.path.join(d, "f"), "wb") as fh:
            fh.write(b"x" * 4096)

    asked = []

    def per_path(path, timeout):
        asked.append(path)
        snap = QuotaSnapshot("fake")
        # Each backend maps only the path it was asked about, which is the shape
        # that makes reusing one snapshot lose the other path's quota entirely.
        snap.rows = [QuotaRow("fs", "blocks", "user", 1 << 30, 2 << 30, 2 << 30, "", path)]
        snap.available = True
        snap.taken_at = snap.read_at
        return snap

    monkeypatch.setattr(quotamod, "read_best", per_path)
    monkeypatch.setattr(deletedmod, "scan", lambda *ar, **kw: deletedmod.DeletedScan())
    cli.main(["-a", a, b, "--color", "never", "--no-settle-check", "--json"])
    assert asked == [a, b], "each path must select its own backend, got {}".format(asked)


def test_one_backend_is_reused_when_it_is_site_wide(tmp_path, monkeypatch):
    """The fix must not cost a subprocess per path in the common case.

    A snapshot that maps nothing for the path it was read for is a site-wide
    answer, so it is shared rather than re-read.
    """
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    for d in (a, b):
        os.makedirs(d)
        with open(os.path.join(d, "f"), "wb") as fh:
            fh.write(b"x" * 4096)

    calls = []

    def site_wide(path, timeout):
        calls.append(path)
        snap = QuotaSnapshot("quota -s")
        snap.rows = [QuotaRow("fs", "blocks", "user", 1, 2, 2, "", "/nowhere-relevant")]
        snap.available = True
        return snap

    monkeypatch.setattr(quotamod, "read_best", site_wide)
    monkeypatch.setattr(deletedmod, "scan", lambda *ar, **kw: deletedmod.DeletedScan())
    cli.main(["-a", a, b, "--color", "never", "--no-settle-check", "--json"])
    assert len(calls) == 1, "a site-wide snapshot should be read once, not per path"


# ---------------------------------------------------------------------------
# #38 -- the namespace qualifier has to reach the reader, not just the flag
# ---------------------------------------------------------------------------


def test_a_namespaced_scan_says_so_in_the_rendered_report():
    """Setting ``complete = False`` is not enough; the *count* is what misleads.

    "none found in the 1 of 1 processes this scan can inspect" reads as 100%
    coverage. The finding is about that sentence, so the qualifier belongs beside
    it -- in the text, in the compact facts line, and in the JSON.
    """
    scan = deletedmod.DeletedScan()
    scan.available = True
    scan.scanned_pids = 1
    scan.namespaced = True
    text = "\n".join(report.render_deleted(scan, 10, ui.resolve_style("never")))
    assert "namespace" in text.lower(), text

    doc = report.to_json(None, None, None, scan, None, 10)
    assert doc["deleted_but_open"]["pid_namespaced"] is True
    assert doc["deleted_but_open"]["complete"] is False


def test_a_normal_scan_does_not_claim_a_namespace():
    scan = deletedmod.DeletedScan()
    scan.available = True
    scan.scanned_pids = 30
    scan.unreadable_pids = 1400
    text = "\n".join(report.render_deleted(scan, 10, ui.resolve_style("never")))
    assert "namespace" not in text.lower()
    assert "this node only" in text


def test_an_abandoned_sweep_says_so_in_the_rendered_report():
    scan = deletedmod.DeletedScan()
    scan.available = True
    scan.scanned_pids = 3
    scan.timed_out = True
    scan.reason = "the /proc sweep was abandoned after 10s"
    text = "\n".join(report.render_deleted(scan, 10, ui.resolve_style("never")))
    assert "abandoned" in text


# ---------------------------------------------------------------------------
# #37 / #38 -- the /proc sweep
# ---------------------------------------------------------------------------


def test_the_deleted_scan_is_bounded(monkeypatch):
    """``os.stat`` through /proc/<pid>/fd resolves the real inode.

    On a hung NFS/Lustre/autofs mount that blocks in uninterruptible sleep with
    no timeout and no signal that will reach it -- and ``rdu -a`` runs this scan
    unconditionally, on the emergency path.
    """
    started = threading.Event()

    def wedged(res, found, prefix, done):
        started.set()
        threading.Event().wait(30)  # a stat that never returns

    monkeypatch.setattr(deletedmod, "_sweep", wedged)
    scan = deletedmod.scan(timeout=0.3)
    assert started.is_set()
    assert scan.timed_out
    assert not scan.complete
    assert "abandoned" in scan.reason


def test_the_deleted_scan_reports_a_namespaced_proc(monkeypatch):
    """ "1 of 1 processes" reads as node-wide coverage inside a container.

    Under Apptainer, Docker, or a Slurm cgroup with ``proc`` remounted, /proc
    shows only the namespace's processes.
    """
    monkeypatch.setattr(deletedmod, "_in_pid_namespace", lambda: True)
    scan = deletedmod.scan(timeout=5.0)
    assert scan.namespaced
    assert not scan.complete


def test_the_namespace_probe_is_honest_on_this_host():
    """It must not report a namespace where there is none."""
    if os.path.exists("/proc/1/comm"):
        with open("/proc/1/comm") as fh:
            init = fh.read().strip()
        if init in ("systemd", "init"):
            assert deletedmod._in_pid_namespace() is False


@pytest.mark.skipif(not UNLINK_HIDES_ENTRY, reason=NEEDS_REAL_UNLINK)
def test_the_deleted_scan_still_finds_a_real_unlinked_file(tmp_path):
    """The bounded rewrite must not break the mechanism it bounds."""
    path = str(tmp_path / "held.bin")
    with open(path, "wb") as fh:
        fh.write(b"x" * (4 << 20))
        fh.flush()
        os.unlink(path)  # unlinked while the fd is still open: the whole point
        scan = deletedmod.scan(prefix=str(tmp_path), timeout=20.0)
    assert not scan.timed_out
    assert any(f.path == path for f in scan.files), "the held inode was not found"


# ---------------------------------------------------------------------------
# #42 -- dead code that reads as live
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module,name",
    [
        ("rapidu.walk", "_entry_keys"),
        ("rapidu.walk", "_bump"),
        ("rapidu.ui", "key_value"),
        ("rapidu.ui", "columns"),
        ("rapidu.ui", "rule"),
        ("rapidu.ui", "ok"),
    ],
)
def test_superseded_helpers_are_gone(module, name):
    """``_entry_keys``' docstring described the live design, so it read as live code.

    It was the natural place a future contributor would "fix" the ``rdu /``
    double-count without effect.
    """
    mod = __import__(module, fromlist=["_"])
    assert not hasattr(mod, name), "{}.{} is dead code and should not be back".format(module, name)


# ---------------------------------------------------------------------------
# #35 -- the 3.6 floor was guarded by an AST parse over source only
# ---------------------------------------------------------------------------

_SYSTEM_PYTHON = "/usr/bin/python3"


def _system_python_version():
    try:
        out = subprocess.check_output(
            [_SYSTEM_PYTHON, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            universal_newlines=True,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return tuple(int(p) for p in out.strip().split("."))


def test_every_module_imports_under_the_system_interpreter():
    """``import rapidu`` loads two modules; the floor needs all of them.

    ``test_imports_under_the_system_interpreter`` runs the real interpreter but
    ``rapidu/__init__`` only pulls in ``_version``, so nine modules were never
    executed by it. An AST parse cannot see a *runtime* API that does not exist on
    3.6 -- ``subprocess.run(capture_output=)`` (3.7), ``dict | dict`` (3.9),
    ``str.removeprefix`` (3.9), ``importlib.metadata`` (3.8) all parse fine -- and
    CI runs 3.9-3.13, so nothing automated would have caught one.
    """
    version = _system_python_version()
    if version is None:
        pytest.skip("no {} on this host".format(_SYSTEM_PYTHON))
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env = dict(os.environ, PYTHONPATH=src, PYTHONDONTWRITEBYTECODE="1")
    modules = [
        "rapidu.cli",
        "rapidu.deleted",
        "rapidu.fmt",
        "rapidu.quota",
        "rapidu.reconcile",
        "rapidu.report",
        "rapidu.ui",
        "rapidu.walk",
    ]
    code = "import " + ", ".join(modules)
    proc = subprocess.Popen(
        [_SYSTEM_PYTHON, "-c", code],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    _out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, "every module must import on {}: {}".format(version, err)


def test_a_full_run_works_under_the_system_interpreter_in_the_c_locale(tmp_path):
    """The end-to-end claim, in the locale that broke it.

    This is the test that would have caught #22: not "is the source 3.6 syntax"
    but "does a real run on the advertised floor, under the locale a batch script
    actually has, produce output rather than a traceback".
    """
    version = _system_python_version()
    if version is None:
        pytest.skip("no {} on this host".format(_SYSTEM_PYTHON))
    root = str(tmp_path / "t")
    os.makedirs(root)
    for j in range(40):
        with open(os.path.join(root, "f%02d" % j), "wb") as fh:
            fh.write(b"x" * 100)

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    for locale in ("C", "C.UTF-8", "POSIX"):
        env = dict(
            os.environ,
            PYTHONPATH=src,
            PYTHONDONTWRITEBYTECODE="1",
            LC_ALL=locale,
            LANG=locale,
        )
        proc = subprocess.Popen(
            [_SYSTEM_PYTHON, "-m", "rapidu", root, "--no-quota", "--no-deleted"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        out, err = proc.communicate(timeout=120)
        assert "UnicodeEncodeError" not in err, "LC_ALL={} crashed: {}".format(locale, err)
        assert proc.returncode == 0, "LC_ALL={} exited {}: {}".format(locale, proc.returncode, err)
        assert out.strip(), "LC_ALL={} produced no output".format(locale)


def test_the_ruff_target_version_does_not_outrank_requires_python():
    """``target-version = "py38"`` told the linter py38 constructs were fine.

    ``requires-python`` says 3.6. A linter configured above the floor cannot warn
    about the gap, which is half of why the floor was held by hand only.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "pyproject.toml")) as fh:
        text = fh.read()
    assert 'requires-python = ">=3.6"' in text
    # ruff has no py36 target, so py37 is as close to the floor as it goes.
    # Anything above that tells the linter constructs are fine which are not.
    assert 'target-version = "py37"' in text, (
        "ruff's target-version is above the supported floor; lower it to py37"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# The outer frame, end to end through the CLI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [[], ["-a", "--no-quota", "--no-deleted"], ["-Q"], ["-D"]])
def test_every_text_mode_is_framed(argv, tmp_path, capsys):
    """The frame goes around the whole report, in every mode that prints one."""
    root = str(tmp_path / "t")
    os.makedirs(root)
    with open(os.path.join(root, "f"), "wb") as fh:
        fh.write(b"x" * 4096)
    cli.main([root, "--color", "never", "--no-settle-check"] + argv)
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0].startswith("╭"), out[:2]
    assert out[-1].startswith("╰"), out[-2:]


def test_json_is_never_framed(tmp_path, capsys):
    """``--json`` is a document for a machine; a border would make it unparseable."""
    root = str(tmp_path / "t")
    os.makedirs(root)
    with open(os.path.join(root, "f"), "wb") as fh:
        fh.write(b"x" * 4096)
    cli.main([root, "--json", "--no-quota", "--no-deleted", "--no-settle-check"])
    out = capsys.readouterr().out
    assert "╭" not in out and "│" not in out
    json.loads(out)


def test_no_box_removes_the_frame(tmp_path, capsys):
    """The escape hatch for piping into grep, awk or a diff."""
    root = str(tmp_path / "t")
    os.makedirs(root)
    with open(os.path.join(root, "f"), "wb") as fh:
        fh.write(b"x" * 4096)
    cli.main([root, "--color", "never", "--no-settle-check", "--no-box"])
    out = capsys.readouterr().out
    assert "╭" not in out and "│" not in out


def test_the_frame_is_ascii_under_ascii(tmp_path, capsys):
    """Same lesson as #22: no glyph decision may bypass the style."""
    root = str(tmp_path / "t")
    os.makedirs(root)
    with open(os.path.join(root, "f"), "wb") as fh:
        fh.write(b"x" * 4096)
    cli.main([root, "--color", "never", "--ascii", "--no-settle-check"])
    out = capsys.readouterr().out
    out.encode("ascii")
    assert out.strip().splitlines()[0].startswith("+")


def test_the_framed_report_is_square(tmp_path, capsys):
    """The real report, with real column layout, must not have a ragged edge."""
    root = str(tmp_path / "t")
    for name in ("alpha", "beta", "gamma"):
        d = os.path.join(root, name)
        os.makedirs(d)
        for j in range(3):
            with open(os.path.join(d, "f%d" % j), "wb") as fh:
                fh.write(b"x" * 4096)
    cli.main([root, "--color", "never", "--no-settle-check"])
    lines = capsys.readouterr().out.strip().splitlines()
    widths = {ui.visible_width(line) for line in lines}
    assert len(widths) == 1, "ragged frame: widths {}".format(sorted(widths))


def test_a_fact_is_never_split_across_lines(tmp_path):
    """``apparent 23.4 MiB (2.0x allocated)`` states a number and what it means.

    Word-wrapping the facts line broke between them, leaving a bare figure on one
    line and a parenthetical on the next -- the exact confusion that fact was
    reworded to remove. Packing at fact boundaries keeps each one whole.
    """
    style = ui.resolve_style("never")
    style.width = 60
    facts = [
        "3,001 files (3,000 regular + 1 dirs)",
        "1.00s at 8 threads (3,001 files/s)",
        "apparent 23.4 MiB (2.0x allocated)",
        "16.0 KiB allocation unit",
    ]
    packed = "\n".join(report._packed(facts, style, "  "))
    for fact in facts:
        assert fact in packed, "{!r} was split".format(fact)
