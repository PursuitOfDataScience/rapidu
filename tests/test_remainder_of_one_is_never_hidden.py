"""One hidden entry is never hidden; two or more earn the summary.

`_hard_warnings` states the rule where it fixed it, and states why:

    `... and 1 more` costs the same line as the path itself and leaves the
    count unverifiable: on a /scratch with four mounts it showed three paths
    and hid the fourth, and the correct 4 was read as a double-counted 3.
    So one hidden entry is never hidden; two or more earn the summary.

`_CROSSED_SHOW`'s own comment points there for it. `render_walk` had it in
neither of its two capped listings:

* the unreadable-directory list showed `[:3]` and printed `... and 1 more` for
  a fourth;
* the owners and groups tables showed `[:6]` and printed `... and 1 more
  owners` for a seventh -- which also disagreed with its own count, the exact
  defect `noun` was split out of `plural` to prevent, and which the row
  builder two lines above already uses (`noun(inodes, "inode")`, added because
  "a home shared with one root-owned file printed `1 inodes` here").

`_show_bound` is now the one home for the rule. It takes `available` as well
as the total because some of these lists are bounded while their count is not
-- showing an item that was never recorded is not possible -- which is the
guard `_hard_warnings` writes inline as `res.crossed == len(res.crossed_paths)`.
"""

from __future__ import annotations

import os
import re

import pytest

from rapidu import report, ui
from rapidu.report import _OWNER_SHOW, _and_more, _show_bound
from rapidu.walk import SettleCheck, WalkResult

PLAIN = ui.resolve_style("never")
_ROW = re.compile(r"^ {6}\S.* [\d,]+ inodes?$")


def _walk(size: int = 1 << 30, inodes: int = 5000) -> WalkResult:
    r = WalkResult("/tmp/tree")
    r.size = size
    r.apparent = size
    r.files = inodes - 1
    r.dirs = 1
    r.elapsed = 1.0
    r.threads = 8
    r.by_uid = {os.getuid(): (size, inodes)}
    r.by_dev = {42: (size, inodes)}
    return r


def _settle() -> SettleCheck:
    c = SettleCheck()
    c.ran = True
    return c


def _owner_block(count: int, monkeypatch) -> list[str]:
    """`render_walk`'s owner rows and its tail, for a tree with `count` owners."""
    r = _walk()
    r.by_uid = {1000 + i: ((count - i) << 20, 10 * (count - i)) for i in range(count)}
    r.by_gid = {}
    names = {1000 + i: "owner%02d" % i for i in range(count)}
    monkeypatch.setattr(report, "_uname", lambda uid: names.get(uid, str(uid)))
    monkeypatch.setattr(report, "_gname", lambda gid: str(gid))
    lines = report.render_walk(r, _settle(), style=PLAIN)
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "owners:")
    block = []
    for ln in lines[start + 1 :]:
        if _ROW.match(ln) or "more" in ln:
            block.append(ln)
        elif ln.strip() == "":
            break
    return block


class TestTheRuleHasOneHome:
    """Guards on `_show_bound` itself, not publication tests.

    They stay green under a neuter that reverts the five CALL SITES and
    leaves the helper defined -- which is the neuter this fix takes, because
    deleting the helper is an import error rather than teeth. What they
    guard is that the helper encodes the rule at all.
    """

    @pytest.mark.parametrize("cap", [3, _OWNER_SHOW])
    def test_a_remainder_of_one_is_listed_instead(self, cap: int) -> None:
        assert _show_bound(cap + 1, cap + 1, cap) == cap + 1

    @pytest.mark.parametrize("cap", [3, _OWNER_SHOW])
    def test_a_remainder_of_two_earns_the_summary(self, cap: int) -> None:
        assert _show_bound(cap + 2, cap + 2, cap) == cap

    @pytest.mark.parametrize("cap", [3, _OWNER_SHOW])
    def test_nothing_hidden_needs_no_extra_row(self, cap: int) -> None:
        assert _show_bound(cap, cap, cap) == cap

    def test_a_bounded_list_cannot_show_what_was_never_recorded(self) -> None:
        """The three-argument form. `unreadable_dirs` is bounded, its count is not."""
        assert _show_bound(4, 3, 3) == 3
        assert _show_bound(4, 4, 3) == 4

    def test_the_tail_agrees_with_its_own_count(self) -> None:
        """`1 more owner`, not `1 more owners`."""
        assert _and_more(7, 6, "owner", PLAIN) == ["      ... and 1 more owner"]
        assert _and_more(8, 6, "owner", PLAIN) == ["      ... and 2 more owners"]


class TestTheOwnersTableKeepsIt:
    def test_a_seventh_owner_is_shown_not_summarised(self, monkeypatch) -> None:
        block = _owner_block(_OWNER_SHOW + 1, monkeypatch)
        rows = [ln for ln in block if _ROW.match(ln)]
        assert len(rows) == _OWNER_SHOW + 1, block
        assert not [ln for ln in block if "more" in ln], block


class TestTheUnreadableListKeepsItToo:
    """The second site, which had no test of its own before this round."""

    def _block(self, recorded: int, dropped: int = 0) -> list[str]:
        r = _walk()
        r.unreadable_dirs = [("/x/d%d" % i, "Permission denied") for i in range(recorded)]
        r.unreadable_dirs_dropped = dropped
        lines = report.render_walk(r, _settle(), style=PLAIN)
        return [ln for ln in lines if "Permission denied" in ln or "more" in ln]

    def test_a_fourth_unreadable_directory_is_named(self) -> None:
        block = self._block(4)
        assert len([ln for ln in block if "Permission denied" in ln]) == 4, block
        assert not [ln for ln in block if "more" in ln], block

    def test_a_fifth_earns_the_summary(self) -> None:
        block = self._block(5)
        assert len([ln for ln in block if "Permission denied" in ln]) == 3, block
        assert [ln for ln in block if "... and 2 more" in ln], block

    def test_a_dropped_path_cannot_be_named(self) -> None:
        """The count is 4 but only 3 paths were recorded, so the fourth is not
        available to show -- `available`, the third argument, is exactly this.
        """
        block = self._block(3, dropped=1)
        assert len([ln for ln in block if "Permission denied" in ln]) == 3, block
        assert [ln for ln in block if "... and 1 more" in ln], block


class TestControls:
    """Each passes with all five edits reverted as well as with them.

    They cover the cases the two listings already got right, so a neuter that
    reddens one of them means the change went further than the remainder of
    one -- verified by running it.
    """

    def test_the_listed_rows_and_the_tail_add_up(self, monkeypatch) -> None:
        """CONTROL, established by running the neuter rather than by naming it.

        `_hard_warnings`' other rule -- "Listed + hidden == the headline count"
        -- held before this round too: 6 rows plus `1 more` is still 7. What was
        wrong was WHICH side of the sum the seventh owner sat on, which the
        tests above catch.
        """
        for count in range(_OWNER_SHOW, _OWNER_SHOW + 4):
            block = _owner_block(count, monkeypatch)
            rows = [ln for ln in block if _ROW.match(ln)]
            tail = [ln for ln in block if "more" in ln]
            hidden = 0
            if tail:
                match = re.search(r"(\d+) more", tail[0])
                assert match is not None, tail
                hidden = int(match.group(1))
            assert len(rows) + hidden == count, (count, block)

    def test_two_hidden_owners_still_earn_a_summary(self, monkeypatch) -> None:
        block = _owner_block(_OWNER_SHOW + 2, monkeypatch)
        rows = [ln for ln in block if _ROW.match(ln)]
        assert len(rows) == _OWNER_SHOW, block
        assert [ln for ln in block if "2 more" in ln], block

    def test_a_table_under_the_cap_has_no_tail(self, monkeypatch) -> None:
        block = _owner_block(_OWNER_SHOW - 1, monkeypatch)
        assert not [ln for ln in block if "more" in ln], block
        assert len([ln for ln in block if _ROW.match(ln)]) == _OWNER_SHOW - 1

    def test_the_rules_origin_still_lists_a_fourth_crossed_path(self) -> None:
        """`_hard_warnings` kept the rule all along and is left alone."""
        r = _walk()
        r.crossed = 4
        r.crossed_paths = ["/mnt/a", "/mnt/b", "/mnt/c", "/mnt/d"]
        out = "\n".join(report._hard_warnings(r, _settle(), PLAIN, settling=False))
        for path in r.crossed_paths:
            assert path in out, out
        assert "more" not in out, out

    def test_five_crossed_paths_still_summarise_the_remainder(self) -> None:
        r = _walk()
        r.crossed = 5
        r.crossed_paths = ["/mnt/%d" % i for i in range(5)]
        out = "\n".join(report._hard_warnings(r, _settle(), PLAIN, settling=False))
        assert "... and 2 more" in out, out
