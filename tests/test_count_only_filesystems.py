"""`-c` reported `filesystems: 1` from a walk that never read `st_dev`.

`report.to_json` published `"filesystems": len(res.by_dev)` unguarded. Under `-c` /
`count_only` the walk never stats an entry, so `by_dev` holds at most the root and the
document asserted **1 filesystem** — a measurement that was never taken. rapidu already
fixed this exact fabrication class for `symlinks`, `specials` and `hardlinked_inodes`
via `report._unmeasured`, whose docstring states the rule it enforces:

    ``None`` is not zero. A caller that has no measurement passes ``None`` and gets
    ``n/a``, never ``0.0 B``. [...] Zero and unmeasured are not the same claim, and
    only one of them is true here: a consumer cannot tell an empty tree from a walk
    that took no sizes.

Measured on a two-device tree: the full walk says 2, `-c` said 1. Now `-c` says `null`.

The distinction from `-x` matters and is pinned below: `one_file_system` DID stat every
entry it visited, so its `filesystems: 1` is a real measurement of a deliberately
bounded walk. Only `-c` lacks the reading. `test_crossed_filesystem_share.py::
test_x_still_reports_what_it_skipped` asserts that `1`, and it must keep passing.
"""

import os
import pathlib
from typing import Any

from rapidu import report
from rapidu import walk as walkmod
from rapidu.walk import SettleCheck


def _tree(root: pathlib.Path, count: int = 4) -> None:
    for i in range(count):
        (root / f"f{i}.bin").write_bytes(b"x" * 100)


def _walk_doc(root: pathlib.Path, *, count_only: bool) -> dict[str, Any]:
    res = walkmod.walk(str(root), count_only=count_only)
    return report.to_json(res, SettleCheck(), None, None, None)["walk"]


class TestCountOnlyDoesNotAssertAFilesystemCount:
    def test_it_is_null_not_one(self, tmp_path: pathlib.Path) -> None:
        _tree(tmp_path)
        value = _walk_doc(tmp_path, count_only=True)["filesystems"]
        assert value is None, value

    def test_a_full_walk_still_reports_the_honest_count(self, tmp_path: pathlib.Path) -> None:
        _tree(tmp_path)
        value = _walk_doc(tmp_path, count_only=False)["filesystems"]
        assert isinstance(value, int) and value >= 1, value

    def test_the_two_modes_do_not_publish_the_same_claim(self, tmp_path: pathlib.Path) -> None:
        # The whole point: before this they were both `1`.
        _tree(tmp_path)
        assert (
            _walk_doc(tmp_path, count_only=True)["filesystems"]
            != _walk_doc(tmp_path, count_only=False)["filesystems"]
        )

    def test_the_key_is_still_present_under_c(self, tmp_path: pathlib.Path) -> None:
        # Withheld as `null`, not dropped: a consumer reading it blind must not
        # get a KeyError where it used to get a number.
        _tree(tmp_path)
        assert "filesystems" in _walk_doc(tmp_path, count_only=True)


class TestControls:
    """None of these reads the guarded expression, so each holds with the fix in or out."""

    def test_the_siblings_were_already_null_under_c(self, tmp_path: pathlib.Path) -> None:
        # The precedent this follows, asserted independently of it.
        _tree(tmp_path)
        doc = _walk_doc(tmp_path, count_only=True)
        for key in ("symlinks", "specials", "hardlinked_inodes"):
            assert doc[key] is None, (key, doc[key])

    def test_a_bounded_walk_keeps_its_real_count(self, tmp_path: pathlib.Path) -> None:
        """`-x` stats what it visits, so its count is measured, not fabricated."""
        _tree(tmp_path)
        res = walkmod.walk(str(tmp_path), one_file_system=True)
        doc = report.to_json(res, SettleCheck(), None, None, None)["walk"]
        assert doc["filesystems"] == 1, doc["filesystems"]

    def test_the_helper_passes_a_measured_value_through(self, tmp_path: pathlib.Path) -> None:
        # `_unmeasured`'s own contract, independent of where it is applied.
        _tree(tmp_path)
        res = walkmod.walk(str(tmp_path))
        assert report._unmeasured(res, 7) == 7

    def test_by_dev_is_still_populated_on_the_full_path(self, tmp_path: pathlib.Path) -> None:
        _tree(tmp_path)
        res = walkmod.walk(str(tmp_path))
        assert len(res.by_dev) >= 1
        assert sum(b for b, _ in res.by_dev.values()) == res.size

    def test_the_tree_really_exists(self, tmp_path: pathlib.Path) -> None:
        _tree(tmp_path)
        assert len(os.listdir(tmp_path)) == 4
