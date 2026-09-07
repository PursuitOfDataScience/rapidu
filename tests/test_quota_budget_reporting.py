"""`_run` reported a spent budget and a sub-second one as "timed out after 0s".

`quota._run`'s error string becomes the quota panel's `reason` -- `read_quota_command`
branches on `rc == 124` and assigns `_one_line(err)` -- so it is the sentence a
reader is given for a missing quota reading. Two things were wrong with it.

**A sub-second budget printed as zero.** `"{:.0f}".format(0.4)` is `"0"`, so a
400 ms allowance was reported as `timed out after 0s`, which reads as rapidu
having used a zero timeout -- its own bug -- rather than as the time it actually
allowed. A sibling package records the identical `%.0f` defect and the identical
remedy: "a sub-second budget printed as `within 0s`, which reads as this tool
having used a zero timeout -- its own bug -- rather than as the 50 ms the caller
asked for." Measured here before the change:

    _run(["sleep", "5"], 0.05) -> "timed out after 0s"
    _run(["sleep", "5"], 0.4)  -> "timed out after 0s"

**A spent deadline still spawned the command.** `_budget` returns exactly `0.0`
once the deadline has passed, and **none of the four `_run` call sites in this
module checks that** (`quota -s`, `mmlsattr -L`, `lfs project -d`, `lfs quota`).
So the process was started only to be killed on the next line, and the reader was
told the command timed out when it was never given a turn. The two want opposite
remedies: a spent budget means narrow the scope or raise the timeout, a timeout
means the quota server is slow.

Both are fixed in `_run` alone -- no call site changed, and `rc` stays 124 so
`read_quota_command`'s branch still fires. Integer budgets render exactly as before,
which two existing tests depend on (`test_audit_round_six.py` asserts
`"timed out after 3s"`); that is pinned in `TestControls`.
"""

import subprocess
import time

import pytest

from rapidu import quota


class TestASpentBudgetIsNotAReportedTimeout:
    @pytest.mark.parametrize("budget", [0.0, -0.0, -1.0, -45.0])
    def test_no_process_is_started(self, budget, monkeypatch):
        started = []

        def spy(*args, **kwargs):
            started.append(args[0] if args else kwargs.get("args"))
            raise AssertionError("should not have spawned")

        monkeypatch.setattr(subprocess, "Popen", spy)
        rc, out, err = quota._run(["sleep", "5"], budget)
        assert started == [], started
        assert rc == 124
        assert out == ""

    def test_it_says_the_budget_ran_out_rather_than_naming_a_timeout(self):
        _rc, _out, err = quota._run(["sleep", "5"], 0.0)
        assert "no time left in the budget" in err, err
        assert "timed out" not in err, err
        # And it names the command, so a reader knows which query was skipped.
        assert "sleep" in err, err


class TestASubSecondBudgetReportsItsRealValue:
    @pytest.mark.parametrize(("budget", "shown"), [(0.05, "0.05s"), (0.2, "0.2s"), (0.4, "0.4s")])
    def test_the_budget_is_not_rounded_to_zero(self, budget, shown):
        _rc, _out, err = quota._run(["sleep", "5"], budget)
        assert "timed out after " + shown in err, err

    @pytest.mark.parametrize("budget", [0.05, 0.4])
    def test_the_naive_format_really_did_print_zero(self, budget):
        """Vacuity guard: the band has to be one `{:.0f}` gets wrong, or the
        cases above would pass against any implementation."""
        assert "{:.0f}".format(budget) == "0"

    def test_a_sub_second_report_is_not_mistakable_for_a_spent_budget(self):
        _rc, _out, err = quota._run(["sleep", "5"], 0.4)
        assert "timed out" in err, err
        assert "no time left" not in err, err


class TestControls:
    """Behaviour that must not change. Each passes in BOTH states."""

    def test_a_zero_budget_still_returns_immediately(self):
        """Passes in BOTH states -- verified by neutering, so it is a control.

        A zero budget was always fast: `communicate(timeout=0)` raises at once.
        What changed is that no process is started (pinned above) and what the
        reader is told. Kept because "fast" is the property a caller relies on
        when the deadline is spent, however it is achieved.
        """
        started = time.time()
        quota._run(["sleep", "5"], 0.0)
        assert time.time() - started < 1.0

    def test_a_zero_budget_still_returns_124(self):
        """Also both states -- `read_quota_command` assigns the reason only under
        `rc == 124`, so keeping the code is what lets the new sentence reach the
        panel at all. Pre-fix it was 124 via `TimeoutExpired`; now it is 124 by
        an explicit early return."""
        rc, _out, _err = quota._run(["sleep", "5"], 0.0)
        assert rc == 124

    @pytest.mark.parametrize(("budget", "shown"), [(1.0, "1s"), (3.0, "3s")])
    def test_a_whole_second_budget_reads_exactly_as_before(self, budget, shown):
        # `test_audit_round_six.py` asserts "timed out after 3s" and
        # `test_quota.py` builds "timed out after 45s"; `{:g}` must not move them.
        _rc, _out, err = quota._run(["sleep", "5"], budget)
        assert err == "timed out after " + shown, err

    def test_the_wording_for_whole_seconds_matches_the_old_format(self):
        for budget in (1.0, 3.0, 10.0, 45.0):
            assert "{:g}".format(budget) == "{:.0f}".format(budget)

    def test_a_command_that_finishes_still_returns_its_output(self):
        rc, out, err = quota._run(["echo", "hello"], 10.0)
        assert rc == 0 and out.strip() == "hello" and err == ""

    def test_a_missing_command_is_still_127(self):
        rc, _out, err = quota._run(["rapidu-no-such-binary"], 10.0)
        assert rc == 127 and "command not found" in err

    def test_a_real_timeout_is_still_124(self):
        rc, _out, err = quota._run(["sleep", "5"], 0.3)
        assert rc == 124 and "timed out" in err

    def test_budget_itself_is_unchanged(self):
        # No deadline: the caller's own timeout, untouched.
        assert quota._budget(45.0, None) == 45.0
        # A spent deadline floors at zero rather than going negative...
        assert quota._budget(45.0, time.time() - 10) == 0.0
        # ...and a live one is the smaller of the two.
        remaining = quota._budget(45.0, time.time() + 5)
        assert 0 < remaining <= 5.0

    def test_the_panel_still_reports_a_timeout_as_its_reason(self, monkeypatch):
        # End to end through the branch that consumes this string.
        monkeypatch.setattr(quota, "_run", lambda cmd, timeout: (124, "", "timed out after 45s"))
        snap = quota.read_quota_command()
        assert "timed out after 45s" in (snap.reason or ""), snap.reason
