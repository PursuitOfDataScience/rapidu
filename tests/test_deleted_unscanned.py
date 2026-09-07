"""The document named figures for a sweep that never ran.

`_unmeasured` states this package's Constraint 10 and the exact shape of the
defect it was written for:

    Zero and unmeasured are not the same claim, and only one of them is true
    here: a consumer cannot tell an empty tree from a walk that took no sizes.
    ... The terminal obeys it ... and the document did not, publishing
    ``size_bytes: 0`` ... beside the terminal's ``n/a`` for the same figure.

`deleted_but_open` was the same disagreement from the other end. With `/proc`
absent the terminal refuses to name anything --

    UNLINKED BUT STILL OPEN
    n/a - /proc is not available on this platform

-- while `to_json` published `total_bytes: 0`, `inodes: 0`, `scanned_pids: 0`
and three more, every one of them a *measurement of the node*: no deleted-but-
open files here, nothing holding space. Nothing was looked at.

It matters more here than for most keys. "Unlinked but still open" is the one
figure in this report a reader goes looking for **because no walk can see it**;
`total_bytes` is documented in the block itself as "space no walk can see". A
false zero there says there is nothing to reclaim, about a platform where the
question was never asked.

`_unscanned` is `_unmeasured`'s sibling, gated on `scan.available` instead of
`res.count_only`, and applied to the six figures that are claims about the node.
The fields that describe the *sweep* keep their values -- they are how a consumer
learns to expect the nulls -- and so does `files`, a container where an empty
iteration and no iteration reach the same place. Both boundaries are pinned in
`TestControls`.

No schema bump: no key changes meaning or disappears, and `report.py` already
calls this "the null the ``schema: 1`` contract already defines as 'no
measurement'". `test_json_schema_contract.py` enumerates key *paths* from a
fully-populated synthetic, so it is unaffected -- checked, not assumed.
"""

from unittest import mock

import pytest

from rapidu import deleted, report, ui
from rapidu.deleted import DeletedScan

PLAIN = ui.resolve_style("never")

#: Claims about the node: null when nothing was swept.
MEASUREMENTS = (
    "total_bytes",
    "inodes",
    "nfs_silly_renamed_bytes",
    "nfs_silly_renamed_inodes",
    "scanned_pids",
    "unreadable_pids",
)

#: Claims about the sweep, or constants: never nulled.
DESCRIPTIONS = ("available", "reason", "complete", "timed_out", "node_local_only")


def _block(scan):
    return report.to_json(None, None, None, scan, None)["deleted_but_open"]


def _unavailable():
    with mock.patch.object(deleted, "_PROC", "/nonexistent-proc"):
        return deleted.scan()


def _available_but_empty():
    """A sweep that really ran and really found nothing."""
    scan = DeletedScan()
    scan.available = True
    scan.scanned_pids = 106
    scan.unreadable_pids = 555
    return scan


class TestAnUnscannedProcNamesNoFigures:
    def test_the_sweep_reports_itself_unavailable(self):
        scan = _unavailable()
        assert scan.available is False
        assert scan.reason == "/proc is not available on this platform"

    @pytest.mark.parametrize("key", MEASUREMENTS)
    def test_every_claim_about_the_node_is_null(self, key):
        assert _block(_unavailable())[key] is None

    def test_the_two_surfaces_now_agree(self):
        """The tactic that found it: the rendered report against its --json."""
        scan = _unavailable()
        rendered = " ".join(report.render_deleted(scan, PLAIN))
        assert "n/a" in rendered
        assert scan.reason in rendered
        # The terminal names no figure, and now neither does the document.
        block = _block(scan)
        assert [block[k] for k in MEASUREMENTS] == [None] * len(MEASUREMENTS)

    def test_no_measurement_is_left_as_a_zero(self):
        """Stated as a property, so a seventh figure added later is covered."""
        block = _block(_unavailable())
        numeric = {
            k: v
            for k, v in block.items()
            if isinstance(v, int) and not isinstance(v, bool) and k not in DESCRIPTIONS
        }
        assert numeric == {}, numeric


class TestControls:
    """Behaviour that must not change. Each passes in BOTH states."""

    @pytest.mark.parametrize("key", MEASUREMENTS)
    def test_a_sweep_that_ran_still_publishes_its_figures(self, key):
        # Including the ones that are genuinely zero: a measured zero is a
        # fact, and telling it apart from "not measured" is the whole point.
        assert _block(_available_but_empty())[key] is not None

    def test_a_measured_zero_stays_zero(self):
        block = _block(_available_but_empty())
        assert block["total_bytes"] == 0
        assert block["inodes"] == 0
        assert block["nfs_silly_renamed_bytes"] == 0
        assert block["scanned_pids"] == 106
        assert block["unreadable_pids"] == 555

    @pytest.mark.parametrize("key", DESCRIPTIONS)
    def test_the_fields_describing_the_sweep_are_never_nulled(self, key):
        # `reason` is None on a healthy sweep by design, so it is checked by
        # presence rather than value; the rest must carry a real answer even
        # when nothing was scanned.
        block = _block(_unavailable())
        assert key in block
        if key != "reason":
            assert block[key] is not None

    def test_pid_namespaced_keeps_its_documented_default(self):
        # `_in_pid_namespace` returns False when neither signal is readable and
        # says why: "report the wider view rather than claiming a restriction we
        # could not observe". That is a decision, not a reading, so the fix
        # leaves it alone.
        assert _block(_unavailable())["pid_namespaced"] is False

    def test_files_stays_a_list(self):
        assert _block(_unavailable())["files"] == []
        assert _block(_available_but_empty())["files"] == []

    def test_the_key_set_is_unchanged_and_the_schema_did_not_move(self):
        doc = report.to_json(None, None, None, _available_but_empty(), None)
        assert doc["schema"] == 5
        assert set(doc["deleted_but_open"]) == set(_block(_unavailable())), (
            "nulling a value must not add or drop a key"
        )

    def test_the_rendered_surface_is_untouched(self):
        assert report.render_deleted(_unavailable(), PLAIN)[-1].strip() == (
            "n/a - /proc is not available on this platform"
        )

    def test_unmeasured_still_works(self):
        # The sibling helper this one is modelled on, left alone.
        from rapidu.walk import WalkResult

        res = WalkResult("/tmp/t")
        res.count_only = True
        assert report._unmeasured(res, 5) is None
        res.count_only = False
        assert report._unmeasured(res, 5) == 5
