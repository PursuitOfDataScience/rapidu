"""What ``SettleCheck.sampled_of`` is coupled to, and why ``conclusive`` is done.

``sampled_of`` is the population the re-stat drew from. ``recheck_settling`` sets
it from the walk (``res.recent_files + res.touched_files``) and then increments
``checked`` or ``gone`` once per entry in ``res.recent_sample``, and the sample is
appended in the same branch that increments those two counters. So two things are
true of every check the walk can actually produce:

* ``checked + gone == len(res.recent_sample) <= sampled_of``, and
* ``sampled_of == 0`` exactly when ``recent_sample`` is empty.

Neither was pinned anywhere, and nine fixtures had drifted outside both: they set
``checked`` to 5 or 60 while leaving ``sampled_of`` at its ``0`` default, or paired
``sampled_of=0`` with a ``WalkResult`` reporting 23 and 6,000 recent files. That
made a proposed ``sampled_of == 0`` branch in :attr:`~rapidu.walk.SettleCheck.conclusive`
fail nine tests and look like a semantic obstacle -- "the suite means something
else by this property" -- when the failures were fixture shorthand for a state
the walk cannot reach. Given the ``sampled_of`` the real path would have set,
all nine pass with that branch installed.

The branch was withdrawn on its own merits, not on theirs, and the second half of
this file is why: over every reachable state it changes exactly one published
value, ``settling.conclusive`` for the empty population, and ``false`` is the
better value there because that branch does not sleep -- ``conclusive: true``
beside ``recheck_gap_seconds: 0.0`` would claim a believable null from a zero-gap
re-stat. So these are regressions, not repairs: they pin today's behaviour so the
next reader finds the measurement instead of re-running it.
"""

import io
import os
import time

from rapidu import reconcile as rc
from rapidu import report
from rapidu.deleted import DeletedScan
from rapidu.quota import QuotaRow, QuotaSnapshot
from rapidu.walk import MIN_CONCLUSIVE_GAP_S, SettleCheck, WalkResult, recheck_settling, walk


def _tree(root, nfiles=8, payload=65536, subdirs=0):
    os.makedirs(root)
    for i in range(nfiles):
        with io.open(os.path.join(root, "f%05d" % i), "wb") as handle:
            handle.write(b"q" * payload)
    for j in range(subdirs):
        sub = os.path.join(root, "s%02d" % j)
        os.makedirs(sub)
        for i in range(nfiles):
            with io.open(os.path.join(sub, "g%05d" % i), "wb") as handle:
                handle.write(b"q" * payload)
    return root


def _shapes(tmp_path):
    """Every shape of walk that reaches ``recheck_settling`` with a real result.

    Named so a failure says which one, and deliberately spanning both halves of
    the population union, the empty tree, the switched-off window, the stat-free
    walk, and a sample large enough to hit the 4,096 cap.
    """
    out = []

    root = _tree(str(tmp_path / "fresh"))
    out.append(("fresh", walk(root, threads=2)))

    root = _tree(str(tmp_path / "nested"), nfiles=6, subdirs=3)
    out.append(("nested", walk(root, threads=2)))

    root = _tree(str(tmp_path / "window_off"))
    out.append(("window_off", walk(root, threads=2, settle_window=0.0)))

    root = _tree(str(tmp_path / "empty"), nfiles=0)
    out.append(("empty", walk(root, threads=2)))

    # The `touched` half of the union: age the mtime out of the window, then bump
    # ctime with a chmod. `recent_files` stays 0 and `touched_files` carries it.
    root = _tree(str(tmp_path / "touched"), nfiles=4)
    for name in os.listdir(root):
        path = os.path.join(root, name)
        os.utime(path, (1546300800, 1546300800))
        os.chmod(path, 0o640)
    out.append(("touched", walk(root, threads=2)))

    root = _tree(str(tmp_path / "capped"), nfiles=5000, payload=64)
    out.append(("capped", walk(root, threads=2)))

    root = _tree(str(tmp_path / "counted"), nfiles=5)
    out.append(("counted", walk(root, threads=2, count_only=True)))

    return out


# ---------------------------------------------------------------------------
# 1. the coupling the nine fixtures had drifted out of


def test_the_restat_never_checks_more_than_the_population_it_sampled(tmp_path):
    """``checked + gone <= sampled_of``, over every shape of walk.

    The bound the nine fixtures broke, and the one that makes ``sampled_of`` able
    to tell an empty population from a deleted one at all: if ``checked`` may
    exceed it, ``sampled_of == 0`` stops meaning anything.
    """
    for name, res in _shapes(tmp_path):
        for wait in (0.0, 4 * MIN_CONCLUSIVE_GAP_S):
            chk = recheck_settling(res, wait)
            assert chk.checked + chk.gone <= chk.sampled_of, (name, wait, vars(chk))


def test_the_population_is_the_walks_own_count_and_not_a_second_opinion(tmp_path):
    """``sampled_of`` is exactly ``recent_files + touched_files``, both halves.

    The other way the fixtures were unreachable: ``sampled_of=0`` beside a
    ``WalkResult`` reporting 23 and 6,000 recent files. Through the real path the
    two cannot disagree, so a check and the result it came from always describe
    one population -- including for ``-c``, where the check did not run and the
    population is unknown rather than zero.
    """
    for name, res in _shapes(tmp_path):
        chk = recheck_settling(res, 0.0)
        if res.count_only:
            assert chk.ran is False, name
            assert chk.sampled_of == 0, name
            continue
        assert chk.sampled_of == res.recent_files + res.touched_files, name
        assert chk.checked + chk.gone == len(res.recent_sample), name


def test_an_empty_population_is_the_only_way_to_reach_a_zero_population(tmp_path):
    """``sampled_of == 0`` iff the sample is empty -- the biconditional, both ways.

    This is what a ``sampled_of == 0`` branch in ``conclusive`` would key on, so
    it is what decides whether such a branch can see anything but the idle tree.
    """
    for name, res in _shapes(tmp_path):
        for wait in (0.0, 4 * MIN_CONCLUSIVE_GAP_S):
            chk = recheck_settling(res, wait)
            assert (chk.sampled_of == 0) == (res.recent_sample == []), (name, wait)
            if chk.sampled_of == 0 and chk.ran:
                # ...and then it is the idle tree, whose verdict comes from the
                # walk rather than from the check.
                assert (res.recent_files, res.touched_files) == (0, 0), name
                assert (chk.checked, chk.gone, chk.gap) == (0, 0, 0.0), name


def test_a_hand_built_check_that_breaks_the_bound_is_out_of_reach(tmp_path):
    """The nine fixtures' shape, stated once so the next reader sees it.

    ``checked=5, sampled_of=0`` is what made the branch look load-bearing. No
    walk produces it; the assertion above is the reason. Kept as an explicit
    statement rather than a comment because the failing-nine story is otherwise
    only recoverable by re-running the experiment.
    """
    hand_built = SettleCheck()
    hand_built.ran = True
    hand_built.checked = 5
    assert hand_built.checked + hand_built.gone > hand_built.sampled_of

    root = _tree(str(tmp_path / "real"))
    res = walk(root, threads=2)
    real = recheck_settling(res, 0.0)
    assert real.checked == 8
    assert real.sampled_of == 8, "the real path sets the population it checked against"


# ---------------------------------------------------------------------------
# 2. why the branch was withdrawn: what `conclusive` says for every reachable state


def _reachable(tmp_path):
    """One ``(label, res, chk)`` per reachable state of ``SettleCheck``.

    Built through ``recheck_settling`` wherever the state has a walk that
    produces it. ``gap`` is assigned rather than slept in the three states that
    need a believable one: ``MIN_CONCLUSIVE_GAP_S`` is 5s, no test may sleep for
    it, and the gap is an input to the judgement rather than an observation --
    ``test_walk_throttles._believable`` says so in those words.
    """
    out = []

    root = _tree(str(tmp_path / "cnt"), nfiles=5)
    counted = walk(root, threads=2, count_only=True)
    out.append(("no check asked for", counted, SettleCheck()))
    out.append(("count_only", counted, recheck_settling(counted, 4 * MIN_CONCLUSIVE_GAP_S)))

    root = _tree(str(tmp_path / "idle"), nfiles=8)
    idle = walk(root, threads=2, settle_window=0.0)
    out.append(("empty population", idle, recheck_settling(idle, 0.0)))
    out.append(("empty population, waited", idle, recheck_settling(idle, 4 * MIN_CONCLUSIVE_GAP_S)))

    root = _tree(str(tmp_path / "full"))
    full = walk(root, threads=2)
    out.append(("full re-stat, gap 0", full, recheck_settling(full, 0.0)))
    believable = recheck_settling(full, 0.0)
    believable.gap = 60.0
    out.append(("full re-stat, gap 60", full, believable))
    moved = recheck_settling(full, 0.0)
    moved.gap = 60.0
    moved.drift = 1 << 20
    out.append(("measured drift", full, moved))

    root = _tree(str(tmp_path / "vanished"))
    vanished = walk(root, threads=2)
    for name in os.listdir(vanished.root):
        os.unlink(os.path.join(vanished.root, name))
    blind = recheck_settling(vanished, 0.0)
    blind.gap = 60.0
    out.append(("whole sample deleted", vanished, blind))

    root = _tree(str(tmp_path / "partial"))
    partial = walk(root, threads=2)
    for name in sorted(os.listdir(partial.root))[:5]:
        os.unlink(os.path.join(partial.root, name))
    survivors = recheck_settling(partial, 0.0)
    survivors.gap = 60.0
    out.append(("partial vanishing", partial, survivors))

    root = _tree(str(tmp_path / "cap"), nfiles=5000, payload=64)
    capped = walk(root, threads=2)
    truncated = recheck_settling(capped, 0.0)
    truncated.gap = 60.0
    out.append(("truncated sample", capped, truncated))

    return out


def test_conclusive_over_every_reachable_state(tmp_path):
    """The truth table, pinned whole. A withdrawal needs a statement of what it kept.

    Read down the ``conclusive`` column: ``False`` wherever the check could not
    have seen the effect (no check, no mtime read, nothing sampled, a gap too
    short, a sample deleted from under it) and ``True`` only where it either had
    long enough and saw nothing or positively watched the tree move.
    """
    expected = {
        # label                       ran    sampled_of==0  conclusive
        "no check asked for": (False, True, False),
        "count_only": (False, True, False),
        "empty population": (True, True, False),
        "empty population, waited": (True, True, False),
        "full re-stat, gap 0": (True, False, False),
        "full re-stat, gap 60": (True, False, True),
        "measured drift": (True, False, True),
        "whole sample deleted": (True, False, False),
        "partial vanishing": (True, False, True),
        "truncated sample": (True, False, True),
    }
    seen = {}
    for label, _res, chk in _reachable(tmp_path):
        seen[label] = (chk.ran, chk.sampled_of == 0, chk.conclusive)
    assert seen == expected


def test_only_the_idle_tree_would_have_moved_and_only_in_the_document(tmp_path):
    """The scope of the withdrawn branch: one field, one state.

    ``sampled_of == 0 and ran`` is reachable in exactly one state, so a branch
    keyed on it could not have touched any other -- which is the whole reason it
    was not worth having. Asserted as the reachable state space rather than by
    installing the branch, because pinning a fix that was declined would pin the
    wrong thing.
    """
    keyed = [label for label, _res, chk in _reachable(tmp_path) if chk.ran and chk.sampled_of == 0]
    assert keyed == ["empty population", "empty population, waited"]


def test_the_idle_trees_verdict_does_not_come_from_the_check(tmp_path):
    """``settled: true`` is the walk's own counters, so ``conclusive`` carries nothing.

    The reason ``false`` costs a consumer nothing here: the verdict it wants is
    already published and already right, and the three fields beside it tell this
    state apart from the "ran but too brief" one that shares its ``conclusive``.
    """
    root = _tree(str(tmp_path / "quiet"))
    res = walk(root, threads=2, settle_window=0.0)
    chk = recheck_settling(res, 4 * MIN_CONCLUSIVE_GAP_S)
    doc = report.to_json(res, chk, None, None, None)["settling"]

    assert doc["settled"] is True, "nothing was written recently, so nothing is unsettled"
    assert doc["conclusive"] is False
    # ...and `conclusive: true` here would have sat beside this.
    assert doc["recheck_gap_seconds"] == 0.0, "the branch does not sleep, so there is no gap"
    # The disambiguation a consumer uses instead, all three from the walk.
    assert doc["recheck_ran"] is True
    assert doc["rechecked"] == 0
    assert (doc["recent_files"], doc["touched_files"]) == (0, 0)


def test_reconcile_cannot_read_conclusive_for_an_empty_population(tmp_path):
    """CONTROL -- the gate, not the value. ``reconcile``'s read is behind a population.

    ``reconcile`` consults ``settle.conclusive`` inside ``elif res.recent_files or
    res.touched_files``, so the one state a ``sampled_of`` branch could change is
    the one state that branch of ``reconcile`` does not run in. Both arms are
    exercised here so this is the gate being closed rather than the code being
    absent.
    """

    def _snap(res, used):
        snap = QuotaSnapshot("test")
        snap.available = True
        snap.read_at = snap.taken_at = time.time()
        snap.rows = [QuotaRow("fs", "blocks", "user", used, used * 10, used * 11, "", res.root)]
        return snap

    def _blockers(res, chk):
        over = (res.size or 1000) * 3 + 4096
        return rc.reconcile(res, chk, _snap(res, over), DeletedScan(), "blocks").blockers

    # Non-empty population: the read is live and flips the suffix both ways.
    root = _tree(str(tmp_path / "busy"))
    busy = walk(root, threads=2)
    brief = recheck_settling(busy, 0.0)
    assert brief.conclusive is False
    said = [b for b in _blockers(busy, brief) if "may not be final" in b]
    assert said and "the re-stat was immediate" in said[0], said

    believable = recheck_settling(busy, 0.0)
    believable.gap = 60.0
    assert believable.conclusive is True
    said = [b for b in _blockers(busy, believable) if "may not be final" in b]
    assert said and "the re-stat was immediate" not in said[0], said

    # Empty population: the branch is not entered at all, whatever `conclusive` says.
    root = _tree(str(tmp_path / "still"))
    still = walk(root, threads=2, settle_window=0.0)
    assert (still.recent_files, still.touched_files) == (0, 0)
    idle = recheck_settling(still, 4 * MIN_CONCLUSIVE_GAP_S)
    assert idle.sampled_of == 0
    assert [b for b in _blockers(still, idle) if "may not be final" in b] == []


def test_a_deleted_sample_is_still_the_blind_instrument(tmp_path):
    """CONTROL -- the state the ``sampled_of`` question must not disturb.

    ``sampled_of`` is what tells "nothing was written" apart from "the sample was
    unlinked", so any reading of it has to leave the second one saying it
    measured nothing. A 60s gap does not buy the verdict back.
    """
    root = _tree(str(tmp_path / "rotated"))
    res = walk(root, threads=2)
    for name in os.listdir(res.root):
        os.unlink(os.path.join(res.root, name))
    chk = recheck_settling(res, 0.0)
    chk.gap = 60.0

    assert (chk.sampled_of, chk.checked, chk.gone) == (8, 0, 8)
    assert chk.recheck_measured_nothing is True
    assert chk.conclusive is False
    doc = report.to_json(res, chk, None, None, None)["settling"]
    assert doc["recheck_measured_nothing"] is True
    assert doc["conclusive"] is False
    assert doc["settled"] is None


def test_a_full_walk_that_re_stats_is_untouched(tmp_path):
    """CONTROL -- the ordinary case, which no reading of ``sampled_of`` may reach.

    Eight files, all eight re-stat'ed, a believable gap and no drift: the
    population is non-zero, so this is the far side of the branch that was
    declined.
    """
    root = _tree(str(tmp_path / "ordinary"))
    res = walk(root, threads=2)
    chk = recheck_settling(res, 0.0)
    chk.gap = 60.0

    assert (chk.sampled_of, chk.checked, chk.gone) == (8, 8, 0)
    assert chk.conclusive is True and chk.moved is False
    assert chk.sampled is False
    doc = report.to_json(res, chk, None, None, None)["settling"]
    assert doc["settled"] is True
    assert doc["conclusive"] is True
    assert doc["headline_provisional"] is False
    assert doc["drift_bytes"] == 0


def test_the_bound_holds_for_a_hand_built_result_too(tmp_path):
    """CONTROL -- a ``WalkResult`` assembled by hand still gets a coupled check.

    ``recheck_settling`` reads the population off the result, so a fixture that
    sets ``recent_files`` gets a ``sampled_of`` to match without having to know
    to set it. The nine did not go through this function, which is how they
    drifted.
    """
    res = WalkResult(str(tmp_path))
    res.files, res.dirs = 3, 1
    res.recent_files = 2
    res.touched_files = 1
    res.settle_window = 120.0
    res.recent_sample = []

    chk = recheck_settling(res, 0.0)
    assert chk.sampled_of == 3, "taken from the result, not defaulted to 0"
    assert chk.checked + chk.gone <= chk.sampled_of
