"""The compact header accents the measurement the listing was ranked by.

The rule is stated with the tones themselves, in `report.py`::

    #   ACCENT  the measurement this listing was ranked by. Exactly one per
    #           report, and it moves when `-i` or `--sort` moves the ranking.

`render_compact` had drifted from the second half. Its comment records the
first half of the same defect being fixed -- "it used to sit on the byte total
unconditionally, so under `-i` the report accented the size while sorting on
the file count" -- and that fix keyed on `-i` alone, so `--sort` never moved
the accent. Measured on one tree, before:

    --sort files        accented `4.8 MiB` while ranking by the file count
    -i --sort size      accented `204` files, while the table beside it
                        accented the size column -- two accents in a report
                        whose rule is "exactly one"

The two-accent half is measured above but deliberately NOT asserted: the
table's rows carry the heat ramp rather than ACCENT (`report.py` says so where
the tones are defined -- "colour already means magnitude ... not a fourth
scheme"), so which cells come back accented depends on the fixture's
filesystem. On local `/tmp` the header's top-entry line accents four of them;
on the GPFS root `tmp_path` actually lands on, none. A pin on that is a pin on
the block size.

`_sort_key` is the module's one ranking resolver and its docstring already
records this class ("`_entry_names` recomputed a *different* one from
`by_inodes` alone, and the count-mode fallback that would have caught it was
dead"). The table, the hairline and the JSON all ask it; the header was the
last consumer of a listing that did not.
"""

from __future__ import annotations

import re
from typing import Set

import pytest

from rapidu import fmt, report, ui, walk
from rapidu.report import ACCENT_SGR, SettleCheck, _sort_key
from rapidu.walk import WalkResult

_ACCENTED = re.compile("\x1b\\[" + ACCENT_SGR + "m([^\x1b]*)")

#: Every ranking the CLI can ask for, plus the unset default.
SORTS = ["", "size", "files", "density"]


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> WalkResult:
    """A tree whose two rankings disagree: one big file, many tiny ones.

    `big/` holds the bytes and `many/` holds the inodes, so "largest" and
    "most files" name different directories and an accent on the wrong one is
    visible rather than coincidental.
    """
    root = tmp_path_factory.mktemp("accent")
    (root / "big").mkdir()
    (root / "many").mkdir()
    (root / "big" / "one.bin").write_bytes(b"\0" * (4 * 1024 * 1024))
    for i in range(200):
        (root / "many" / ("f%d" % i)).write_text("x\n")
    return walk.walk(str(root))


@pytest.fixture(scope="module")
def style() -> ui.Style:
    return ui.Style(color=True, unicode_ok=True, width=100)


def _accented(res: WalkResult, style: ui.Style, by_inodes: bool, sort: str) -> Set[str]:
    lines = report.render_compact(res, SettleCheck(), 3, by_inodes, style, sort=sort)
    return set(_ACCENTED.findall("\n".join(lines)))


def _figures(res: WalkResult) -> "dict[str, str]":
    """The header's two candidate figures, as the report spells them."""
    return {
        "size": fmt.human_bytes(res.size),
        "files": fmt.human_count(res.inodes),
    }


class TestTheAccentNamesTheRanking:
    @pytest.mark.parametrize("sort", SORTS)
    @pytest.mark.parametrize("by_inodes", [False, True])
    def test_the_accented_figure_is_the_ranked_one(
        self, tree: WalkResult, style: ui.Style, by_inodes: bool, sort: str
    ) -> None:
        """Against `_sort_key`, the resolver the table and the JSON already use."""
        figures = _figures(tree)
        ranked = "files" if _sort_key(sort, by_inodes, tree) == "files" else "size"
        other = "size" if ranked == "files" else "files"
        hits = _accented(tree, style, by_inodes, sort)
        assert figures[ranked] in hits, (by_inodes, sort, ranked, sorted(hits))
        assert figures[other] not in hits, (by_inodes, sort, other, sorted(hits))

    # The three combinations whose rendering the fix changed, spelled out so the
    # test cannot be satisfied by moving `_sort_key` instead of the header.
    @pytest.mark.parametrize(
        "by_inodes,sort,expected",
        [
            (False, "files", "files"),  # accented the byte total
            (True, "size", "size"),  # accented the file count
            (True, "density", "size"),  # accented the file count
        ],
    )
    def test_the_cases_that_were_wrong(
        self,
        tree: WalkResult,
        style: ui.Style,
        by_inodes: bool,
        sort: str,
        expected: str,
    ) -> None:
        figures = _figures(tree)
        hits = _accented(tree, style, by_inodes, sort)
        assert figures[expected] in hits, (by_inodes, sort, sorted(hits))


class TestControls:
    """These held before the fix and must hold after it.

    Verified by neutering the header's resolver call -- each of these covers a
    combination the old expression already got right, so a neuter that reddens
    one of them means the change went further than `--sort`.
    """

    def test_the_default_view_accents_the_byte_total(
        self, tree: WalkResult, style: ui.Style
    ) -> None:
        """`rdu .` ranks by size, so the size is the accent. Always did."""
        hits = _accented(tree, style, False, "")
        assert _figures(tree)["size"] in hits
        assert _figures(tree)["files"] not in hits

    def test_dash_i_accents_the_file_count(self, tree: WalkResult, style: ui.Style) -> None:
        """The case the earlier fix was written for, and its comment describes."""
        hits = _accented(tree, style, True, "")
        assert _figures(tree)["files"] in hits
        assert _figures(tree)["size"] not in hits

    def test_an_explicit_sort_size_agrees_with_the_default(
        self, tree: WalkResult, style: ui.Style
    ) -> None:
        """`--sort size` and the unset default are the same ranking."""
        assert _accented(tree, style, False, "size") == _accented(tree, style, False, "")

    @pytest.mark.parametrize("sort", SORTS)
    @pytest.mark.parametrize("by_inodes", [False, True])
    def test_the_header_accents_exactly_one_of_its_two_totals(
        self, tree: WalkResult, style: ui.Style, by_inodes: bool, sort: str
    ) -> None:
        """CONTROL, and it was one before I ran the neuter rather than after.

        The old expression already accented exactly one of the two totals --
        it picked the wrong one, which is what the tests above catch. This
        holds in both states and guards the other direction: a fix that
        accented both, or neither, would redden here.
        """
        figures = _figures(tree)
        hits = _accented(tree, style, by_inodes, sort)
        marked = [name for name, text in figures.items() if text in hits]
        assert len(marked) == 1, (by_inodes, sort, marked, sorted(hits))

    def test_the_two_figures_are_distinguishable(self, tree: WalkResult, style: ui.Style) -> None:
        """Vacuity guard: if the fixture's two totals rendered alike, every
        assertion above would pass against any implementation."""
        figures = _figures(tree)
        assert figures["size"] != figures["files"]
        assert figures["size"] not in figures["files"]
        assert figures["files"] not in figures["size"]
