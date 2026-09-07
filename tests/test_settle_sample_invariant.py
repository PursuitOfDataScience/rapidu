"""The invariant `walk.py` argues for in a comment, measured on a real walk.

`recheck_settling`'s comment justifies leaving a branch OUT of `SettleCheck`, and the
justification is a structural claim:

    The sample is appended in the same branch that increments
    `recent_files`/`touched_files` and the loop below visits each entry once, so
    `checked + gone == len(recent_sample) <= sampled_of` always, and
    `sampled_of == 0` holds exactly when `recent_sample` is empty.

Nothing checked it. Every existing test ASSIGNS the triple by hand
(`check.sampled_of, check.checked, check.gone = ...`) or builds it through a
`make_settle(...)` factory, which is why the comment also notes that nine fixtures
"pin a state the walk cannot reach". A hand-built `SettleCheck` cannot confirm a claim
about what the walk produces.

So these cases run an actual `walk()` over an actual tree and read the triple off it.
If the invariant ever breaks, the argument for omitting the `sampled_of == 0` branch
goes with it, silently.

Measured while writing this, and worth recording because the two properties are easy
to confuse: `conclusive` does not read `sampled_of` at all — it is `ran`, `moved`,
`gap` and `recheck_measured_nothing`. The property that reads the invariant is
`sampled` (`sampled_of > checked + gone`).
"""

import os
import pathlib

import pytest

from rapidu import walk as walkmod

CAP = walkmod._RECENT_SAMPLE_CAP


def _tree(root: pathlib.Path, count: int) -> None:
    for i in range(count):
        (root / f"f{i}.bin").write_bytes(b"x")


def _walk_and_recheck(root: pathlib.Path, remove: int = 0):
    """A real walk, then a real re-stat — never a hand-built `SettleCheck`."""
    res = walkmod.walk(str(root))
    for i in range(remove):
        os.unlink(root / f"f{i}.bin")
    return res, walkmod.recheck_settling(res, wait=0.0)


class TestTheSampleAccountingHolds:
    @pytest.mark.parametrize(
        "count,remove",
        [(0, 0), (1, 0), (6, 0), (6, 2), (6, 6)],
        ids=["empty", "one", "six", "two-vanished", "all-vanished"],
    )
    def test_every_sampled_entry_is_checked_or_gone(
        self, tmp_path: pathlib.Path, count: int, remove: int
    ) -> None:
        _tree(tmp_path, count)
        res, chk = _walk_and_recheck(tmp_path, remove=remove)
        assert chk.checked + chk.gone == len(res.recent_sample), (
            chk.checked,
            chk.gone,
            len(res.recent_sample),
        )
        assert len(res.recent_sample) <= chk.sampled_of, (
            len(res.recent_sample),
            chk.sampled_of,
        )

    @pytest.mark.parametrize("count", [0, 1, 6], ids=["empty", "one", "six"])
    def test_an_empty_sample_means_nothing_was_recent(
        self, tmp_path: pathlib.Path, count: int
    ) -> None:
        _tree(tmp_path, count)
        res, chk = _walk_and_recheck(tmp_path)
        assert (chk.sampled_of == 0) == (not res.recent_sample), (
            chk.sampled_of,
            len(res.recent_sample),
        )

    def test_a_vanished_sample_is_counted_gone_not_dropped(self, tmp_path: pathlib.Path) -> None:
        # The case the `conclusive` docstring was written for: the whole sample
        # deleted between walk and re-check must not read as "nothing changed".
        _tree(tmp_path, 6)
        res, chk = _walk_and_recheck(tmp_path, remove=6)
        assert (chk.checked, chk.gone) == (0, 6), (chk.checked, chk.gone)
        assert chk.recheck_measured_nothing is True


class TestTheCapIsWhatMakesSampledMeaningful:
    """`sampled` can only be True because the sample is capped — pin the boundary."""

    def test_exactly_at_the_cap_nothing_was_left_out(self, tmp_path: pathlib.Path) -> None:
        _tree(tmp_path, CAP)
        res, chk = _walk_and_recheck(tmp_path)
        assert len(res.recent_sample) == CAP
        assert chk.sampled_of == CAP
        assert chk.sampled is False

    def test_one_file_past_the_cap_reports_a_partial_sample(self, tmp_path: pathlib.Path) -> None:
        _tree(tmp_path, CAP + 1)
        res, chk = _walk_and_recheck(tmp_path)
        assert len(res.recent_sample) == CAP
        assert chk.sampled_of == CAP + 1
        assert chk.sampled is True
        # ...and the invariant still holds at the boundary.
        assert chk.checked + chk.gone == CAP < chk.sampled_of


class TestControls:
    """None of these runs a walk, so each holds whatever the walk produces."""

    def test_sampled_follows_its_formula_on_a_built_check(self) -> None:
        # The property in isolation: this is what the existing tests do, and it
        # cannot confirm anything about the walk — which is why this file exists.
        chk = walkmod.SettleCheck()
        chk.sampled_of, chk.checked, chk.gone = 10, 4, 6
        assert chk.sampled is False
        chk.sampled_of = 11
        assert chk.sampled is True

    def test_conclusive_does_not_read_the_sample_size_at_all(self) -> None:
        # Measured, and the reason the two properties must not be conflated.
        chk = walkmod.SettleCheck()
        # `moved` is a property over `drift` (`drift != 0`), not a field — so the
        # only way to make it true is through the number it reads.
        chk.ran, chk.drift = True, 1
        assert chk.moved is True
        for sampled_of in (0, 1, 10**6):
            chk.sampled_of = sampled_of
            assert chk.conclusive is True

    def test_the_cap_is_the_constant_this_file_assumes(self) -> None:
        assert isinstance(CAP, int) and CAP > 0
