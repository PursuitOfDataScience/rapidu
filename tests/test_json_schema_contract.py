"""`schema` counts key REMOVALS, and nothing enumerated the keys it promises.

`to_json` states the contract itself::

    A version on the document, so a consumer can branch on shape instead of
    probing for keys. Bumped when a key changes meaning or disappears, not when
    one is added.

So the counter's whole job is to warn a consumer that a key it reads is gone --
and a consumer told to "branch on shape instead of probing for keys" is exactly
the one a silent removal breaks. What tracked the shape was
`test_features.py::...`, which pins `schema == 5` and then checks **three** keys of
`walk` plus two absences; `test_audit_round_six.py` deliberately asserts only
`schema >= 3`, noting "the counter belongs to whichever test tracks the current
document shape". Nothing enumerated the document. A key could vanish from
`quota`, `settling`, `deleted_but_open` or `reconciliation` with `schema` still
reading 5 and the whole suite green.

`_SCHEMA_5_PATHS` is that enumeration, taken from a **fully populated synthetic**
document -- every section is a `to_json` parameter, so nothing here depends on the
machine. Three deliberate design choices:

* the check is a SUBSET, not equality, because the stated convention is that an
  addition does not move the counter. `test_control_an_added_key_is_allowed` proves
  the check really is one-directional;
* `by_uid`/`by_gid` are keyed by username, so their paths are normalised to `*`;
* an empty list contributes only its own path (`walk.reclaimable[]`), not element
  keys -- honest about what this document exercises rather than guessing.

`schema == 5` is asserted beside it, so a legitimate bump has to come here and
update the list, which is the deliberate act the counter exists for.
"""

from __future__ import annotations

import os

import pytest

from rapidu import quota as Q
from rapidu import reconcile as rc
from rapidu import report
from rapidu import walk as walkmod
from rapidu.deleted import DeletedScan

#: Keys normalised to ``*`` because they are keyed by a username.
_DYNAMIC = {"by_uid", "by_gid"}

#: Every structural path a schema-5 document carries, fully populated.
_SCHEMA_5_PATHS = (
    "deleted_but_open.available",
    "deleted_but_open.complete",
    "deleted_but_open.files[]",
    "deleted_but_open.inodes",
    "deleted_but_open.nfs_silly_renamed[]",
    "deleted_but_open.nfs_silly_renamed_bytes",
    "deleted_but_open.nfs_silly_renamed_inodes",
    "deleted_but_open.node_local_only",
    "deleted_but_open.pid_namespaced",
    "deleted_but_open.reason",
    "deleted_but_open.scanned_pids",
    "deleted_but_open.timed_out",
    "deleted_but_open.total_bytes",
    "deleted_but_open.unreadable_pids",
    "quota.available",
    "quota.figure_note",
    "quota.mapping_notes[]",
    "quota.mount",
    "quota.reason",
    "quota.rows[]",
    "quota.rows[].device",
    "quota.rows[].fileset",
    "quota.rows[].grace",
    "quota.rows[].hard",
    "quota.rows[].kind",
    "quota.rows[].label",
    "quota.rows[].limit",
    "quota.rows[].mount",
    "quota.rows[].mount_guessed",
    "quota.rows[].mounts[]",
    "quota.rows[].scope",
    "quota.rows[].soft",
    "quota.rows[].used",
    "quota.snapshot_age_seconds",
    "quota.snapshot_taken_at",
    "quota.source",
    "quota.time_note",
    "reconciliation[]",
    "reconciliation[].accounted",
    "reconciliation[].blockers[]",
    "reconciliation[].candidates[]",
    "reconciliation[].deleted_but_open",
    "reconciliation[].difference",
    "reconciliation[].fileset",
    "reconciliation[].kind",
    "reconciliation[].notes[]",
    "reconciliation[].quota",
    "reconciliation[].scope",
    "reconciliation[].share_of_quota",
    "reconciliation[].tolerance",
    "reconciliation[].verdict",
    "reconciliation[].walked",
    "schema",
    "settling.conclusive",
    "settling.drift_bytes",
    "settling.future_mtime_files",
    "settling.headline_provisional",
    "settling.moved",
    "settling.recent_allocated_bytes",
    "settling.recent_apparent_bytes",
    "settling.recent_files",
    "settling.recheck_gap_seconds",
    "settling.recheck_measured_nothing",
    "settling.recheck_ran",
    "settling.rechecked",
    "settling.sampled",
    "settling.settled",
    "settling.touched_files",
    "settling.unlanded_bytes",
    "settling.vanished_allocated_bytes",
    "settling.vanished_files",
    "settling.window_seconds",
    "tool",
    "walk.abandoned_threads",
    "walk.allocation.inline_files",
    "walk.allocation.material",
    "walk.allocation.padded_files",
    "walk.allocation.padding_bytes",
    "walk.allocation.ratio",
    "walk.allocation.under_allocated_files",
    "walk.allocation.unit_bytes",
    "walk.allocation.unit_padding_ceiling_bytes",
    "walk.apparent_bytes",
    "walk.by_age[]",
    "walk.by_age[].bucket",
    "walk.by_age[].bytes",
    "walk.by_age[].files",
    "walk.by_gid.*.bytes",
    "walk.by_gid.*.gid",
    "walk.by_gid.*.inodes",
    "walk.by_uid.*.bytes",
    "walk.by_uid.*.inodes",
    "walk.by_uid.*.uid",
    "walk.complete",
    "walk.dirs",
    "walk.elapsed_seconds",
    "walk.files",
    "walk.filesystems",
    "walk.hardlink_extra_refs",
    "walk.hardlinked_inodes",
    "walk.inodes",
    "walk.interrupted",
    "walk.one_file_system",
    "walk.reclaimable[]",
    "walk.root",
    "walk.size_bytes",
    "walk.skipped_other_filesystem",
    "walk.skipped_other_filesystem_paths[]",
    "walk.specials",
    "walk.symlinks",
    "walk.threads",
    "walk.top_by_density[]",
    "walk.top_by_inodes[]",
    "walk.top_by_inodes[].bytes",
    "walk.top_by_inodes[].inodes",
    "walk.top_by_inodes[].path",
    "walk.top_by_size[]",
    "walk.top_by_size[].bytes",
    "walk.top_by_size[].inodes",
    "walk.top_by_size[].path",
    "walk.unreadable_dir_paths[]",
    "walk.unreadable_dir_paths_dropped",
    "walk.unreadable_dirs",
    "walk.unstatable_entries",
    "walk.unstatable_paths[]",
    "walk.vanished_dirs",
    "walk.vanished_entries",
    "walk.watched_bytes_over_cap",
    "walk.watched_dirs_seen",
    "walk.watched_dirs_tracked",
    "walk.watched_dirs_untracked",
    "walk.watched_inodes_over_cap",
)


def _paths(obj, prefix=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if prefix.split(".")[-1] in _DYNAMIC:
                yield from _paths(value, prefix + ".*")
            else:
                yield from _paths(value, (prefix + "." + key) if prefix else key)
    elif isinstance(obj, list):
        yield prefix + "[]"
        if obj:
            yield from _paths(obj[0], prefix + "[]")
    else:
        yield prefix


def _full_document(tmp_path):
    """A schema-5 document with every optional section supplied.

    `quota`, `deleted_but_open` and `reconciliation` are conditional on their
    component being passed -- measured: with all three `None` the top level is
    exactly `schema`, `settling`, `tool`, `walk` -- so the contract has to be
    taken from the populated call or it would pin four keys and miss ninety.
    """
    root = str(tmp_path / "tree")
    os.makedirs(os.path.join(root, "a"))
    with open(os.path.join(root, "a", "f.bin"), "wb") as handle:
        handle.write(b"x" * 40000)
    with open(os.path.join(root, "b.bin"), "wb") as handle:
        handle.write(b"y" * 900)
    res = walkmod.walk(root, threads=2, depth=2)
    settle = walkmod.recheck_settling(res, 0.0)
    snap = Q.QuotaSnapshot("quota -s")
    snap.available = True
    snap.taken_at = snap.read_at
    snap.rows = [Q.QuotaRow("fs", "blocks", "user", 10**9, 10**9, 2 * 10**9, "7days", root)]
    scan = DeletedScan()
    recs = [rc.reconcile(res, settle, snap, scan, "blocks")]
    return report.to_json(res, settle, snap, scan, recs, 3, root)


class TestTheEnumeratorCanProveSomething:
    def test_the_contract_is_populated(self):
        """Vacuity guard: an empty tuple would make the contract check pass on any
        document at all."""
        assert len(_SCHEMA_5_PATHS) >= 120, len(_SCHEMA_5_PATHS)
        assert len(set(_SCHEMA_5_PATHS)) == len(_SCHEMA_5_PATHS), "duplicate path"

    def test_it_walks_into_nesting_and_lists(self, tmp_path):
        """The enumerator has to reach three levels and into list elements, or a
        removal deep in the document would be invisible to it."""
        found = set(_paths(_full_document(tmp_path)))
        assert "walk.allocation.unit_bytes" in found
        assert "quota.rows[].scope" in found
        assert "walk.by_uid.*.uid" in found
        assert "reconciliation[].verdict" in found


class TestTheSchemaFiveContractHolds:
    def test_no_promised_key_has_disappeared(self, tmp_path):
        """The finding this file exists for."""
        found = set(_paths(_full_document(tmp_path)))
        missing = sorted(p for p in _SCHEMA_5_PATHS if p not in found)
        assert missing == [], missing

    def test_the_version_is_the_one_this_list_describes(self, tmp_path):
        """So a deliberate bump has to come here and update the enumeration."""
        doc = _full_document(tmp_path)
        assert doc["schema"] == 5, doc["schema"]
        assert doc["tool"] == "rapidu"

    def test_the_optional_sections_stay_optional(self, tmp_path):
        """The conditionality the contract rests on, pinned in both directions: a
        section must not start appearing unbidden, and the four always-present keys
        must not become conditional."""
        root = str(tmp_path / "bare")
        os.makedirs(root)
        res = walkmod.walk(root, threads=1, depth=1)
        doc = report.to_json(res, walkmod.recheck_settling(res, 0.0), None, None, None, 3, root)
        assert sorted(doc) == ["schema", "settling", "tool", "walk"], sorted(doc)


class TestControls:
    def test_control_an_added_key_is_allowed(self):
        """CONTROL, and the proof the check is one-directional: the convention is
        that an addition does not move the counter.

        Deliberately does NOT read the document: this is about the COMPARISON, and
        an earlier version built it from `_full_document` -- so when a neuter
        removed a promised key from `to_json`, this reddened for the document's
        reason rather than its own and stopped being a control at all.
        """
        found = set(_SCHEMA_5_PATHS) | {"walk.a_brand_new_figure"}
        assert [p for p in _SCHEMA_5_PATHS if p not in found] == []

    def test_control_a_removed_key_is_caught(self):
        """CONTROL on the check's own sensitivity: it must report a removal, or
        `test_no_promised_key_has_disappeared` proves nothing.

        Also document-independent, for the same reason as the test above: asserting
        an EXACT missing list against the live document turns any other real
        removal into a failure here."""
        found = set(_SCHEMA_5_PATHS) - {"walk.size_bytes"}
        assert [p for p in _SCHEMA_5_PATHS if p not in found] == ["walk.size_bytes"]

    @pytest.mark.parametrize("section", ["walk", "settling", "quota", "deleted_but_open"])
    def test_control_each_section_is_a_mapping(self, section, tmp_path):
        """Cheap shape guard: a section turned into a list would make every path
        under it vanish at once, and the message would name ninety keys instead of
        the one thing that changed."""
        doc = _full_document(tmp_path)
        assert isinstance(doc[section], dict), (section, type(doc[section]))
