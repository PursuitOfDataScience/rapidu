"""Does ``--json`` say what the report says, on the same walk?

``report.to_json`` and ``render_compact``/``render_walk``/``render_settle`` are
two renderings of one :class:`walk.WalkResult`, and this package's own history is
that two renderings of one measurement drift apart -- ``settled`` reporting
``true`` from a re-stat that read nothing, ``top_by_size`` publishing subtrees the
table refused to show, ``bytes: 0`` under ``-c`` beside the terminal's ``n/a``.
Every case below runs both surfaces over the same object and compares them figure
for figure.

Two disagreements were found and are pinned here.

**1. The three-way inode split that ``-c`` never measured.**
``_inode_breakdown`` prints ``4 non-dirs + 1 dir`` under ``-c`` on purpose --
``st_mode`` is never read, so which non-directories are symlinks or sockets is
not known -- while the document published ``symlinks: 0, specials: 0`` for the
same walk of the same tree the full walk describes as ``symlinks: 1,
specials: 2``. Same fabrication that took ``hardlinked_inodes`` and
``hardlink_extra_refs`` through :func:`report._unmeasured`; the comment there
enumerating "every sibling stat-derived count" missed these two.

**2. ``headline_provisional: false`` over measured drift.**
:func:`report._headline_is_provisional` weighed only the *estimate* -- unlanded
bytes against the total -- so a tree growing fast enough that its blocks land
immediately had nothing unlanded and the document called its headline final,
beside ``moved: true`` and ``drift_bytes`` of 1.5 MiB, while both terminal views
printed the word "provisional". ``settled: false`` and
``headline_provisional: false`` in one object is the same class of contradiction
as ``settled: true`` beside ``vanished_files: 7``, which this module has already
fixed twice.

The control is the case neither fix may touch: a clean, settled, fully readable
walk, whose two surfaces are pinned line for line and key for key.
"""

import io
import os
import re
import socket

from rapidu import report, ui
from rapidu import walk as walkmod
from rapidu.walk import SettleCheck, recheck_settling, walk

PLAIN = ui.resolve_style("never")

# Old enough that nothing in the tree is "recent", so SETTLING has no subject and
# the control is genuinely settled rather than merely quiet.
_SETTLED_WINDOW = 0.0


def _flat(lines):
    return " ".join(" ".join(lines).split())


def _both(res, settle, top=10):
    """The two surfaces of one walk: the document, and the two text renderings."""
    doc = report.to_json(res, settle, None, None, None, top)
    compact = _flat(report.render_compact(res, settle, top, False, PLAIN))
    full = _flat(report.render_walk(res, settle, top, style=PLAIN))
    return doc, compact, full


def _mixed_tree(root):
    """A file, a symlink, a fifo and a socket: one of each thing ``walk`` counts.

    The socket is bound and closed rather than left open -- the inode survives
    the close, and an open ``AF_UNIX`` descriptor would put this tree in
    ``rapidu.deleted``'s subject matter, which is a different test.
    """
    os.makedirs(root)
    with io.open(os.path.join(root, "real"), "wb") as handle:
        handle.write(b"x" * 4096)
    os.symlink("real", os.path.join(root, "link"))
    os.mkfifo(os.path.join(root, "pipe"))
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(os.path.join(root, "sock"))
    finally:
        sock.close()
    return root


# --------------------------------------------------------------------------
# 1. `-c` and the inode split


def test_a_full_walk_publishes_the_split_the_terminal_prints(tmp_path):
    """The premise: on this tree the two surfaces agree, and both are non-zero."""
    res = walk(_mixed_tree(str(tmp_path / "mixed")), threads=1, depth=1)
    doc, _compact, full = _both(res, recheck_settling(res))

    assert "1 symlink" in full and "2 specials" in full, full
    assert doc["walk"]["symlinks"] == 1
    assert doc["walk"]["specials"] == 2


def test_count_only_does_not_publish_a_split_it_never_measured(tmp_path):
    """The defect: `symlinks: 0, specials: 0` about a tree holding 1 and 2.

    The failing assertion before the fix was on the document alone -- the
    terminal has been right all along, and is asserted here as the reference the
    document is supposed to match.
    """
    root = _mixed_tree(str(tmp_path / "mixed"))
    res = walk(root, threads=1, depth=1, count_only=True)
    doc, _compact, full = _both(res, recheck_settling(res))

    # What `-c` genuinely counted, from `d_type`: four non-directories and a
    # directory. The terminal refuses to say more than that.
    assert "4 non-dirs + 1 dir" in full, full
    assert "symlink" not in full and "special" not in full, full
    assert doc["walk"]["files"] == 4
    assert doc["walk"]["dirs"] == 1
    assert doc["walk"]["inodes"] == 5

    # ...so the document must not claim the tree has none of either.
    assert doc["walk"]["symlinks"] is None, doc["walk"]
    assert doc["walk"]["specials"] is None, doc["walk"]


def test_the_unmeasured_split_is_not_an_empty_tree(tmp_path):
    """`None` and `0` have to stay distinguishable, which is the whole point.

    A `-c` walk of a tree with no symlinks and no sockets is *also* unmeasured,
    so both cases read `null`; a *full* walk of that tree reports a real 0. That
    contrast is what a consumer needs and what a bare 0 destroyed.
    """
    root = str(tmp_path / "plain")
    os.makedirs(root)
    with io.open(os.path.join(root, "f"), "wb") as handle:
        handle.write(b"y" * 128)

    full_walk = walk(root, threads=1, depth=1)
    doc = report.to_json(full_walk, recheck_settling(full_walk), None, None, None)
    assert doc["walk"]["symlinks"] == 0
    assert doc["walk"]["specials"] == 0

    counted = walk(root, threads=1, depth=1, count_only=True)
    doc = report.to_json(counted, recheck_settling(counted), None, None, None)
    assert doc["walk"]["symlinks"] is None
    assert doc["walk"]["specials"] is None


def test_the_terms_the_terminal_does_print_stay_measured(tmp_path):
    """`files`/`dirs`/`inodes` come off `getdents`, so `-c` measures them.

    Nulling those would be the opposite error: the terminal prints them under
    `-c` -- "5 entries (4 non-dirs + 1 dir)" -- so the document must too.
    """
    res = walk(_mixed_tree(str(tmp_path / "mixed")), threads=1, depth=1, count_only=True)
    doc = report.to_json(res, recheck_settling(res), None, None, None)
    for key in ("files", "dirs", "inodes"):
        assert doc["walk"][key] is not None, key


# --------------------------------------------------------------------------
# 2. measured drift and the provisional headline


def _grown(root, nfiles=8, payload=4096, growth=200000):
    """Walk a tree of fresh files, then grow them, then re-stat. The real thing.

    The gap is assigned rather than slept, as ``test_partial_vanishing`` does and
    for the same reason: ``MIN_CONCLUSIVE_GAP_S`` is 5s, no test may sleep for
    it, and the gap is an input to the judgement rather than an observation about
    the tree. The drift itself is a real measurement -- the files are really
    bigger on the second reading.
    """
    os.makedirs(root)
    for i in range(nfiles):
        with io.open(os.path.join(root, "f%03d" % i), "wb") as handle:
            handle.write(b"q" * payload)
    res = walk(root, threads=2, depth=1)
    assert res.recent_files == nfiles, res.recent_files
    for name in sorted(os.listdir(root)):
        with io.open(os.path.join(root, name), "ab") as handle:
            handle.write(b"g" * growth)
    settle = recheck_settling(res)
    settle.gap = 6.0
    assert settle.moved and settle.drift > 0, settle.drift
    return res, settle


def test_measured_drift_makes_the_document_say_provisional_too(tmp_path):
    """The defect: `headline_provisional: false` under "still settling".

    Nothing is unlanded here -- the blocks landed as fast as they were written --
    so the estimate the old rule consulted was silent, and the one thing the
    check positively observed was ignored.
    """
    res, settle = _grown(str(tmp_path / "growing"))
    doc, compact, full = _both(res, settle)
    settling = doc["settling"]

    assert report._unlanded_bytes(res) == 0, "the estimate must not be what fires"
    assert settling["moved"] is True
    assert settling["drift_bytes"] > 0
    assert settling["settled"] is False

    # Both terminal views say it in the word the document has a key for.
    assert "still settling" in compact, compact
    assert "provisional" in compact or "provisional" in full, (compact, full)

    assert settling["headline_provisional"] is True, settling


def test_the_drift_figure_is_the_same_number_in_both_surfaces(tmp_path):
    """A key that agrees in truth value and disagrees in magnitude is no better."""
    res, settle = _grown(str(tmp_path / "growing"))
    doc, compact, _full = _both(res, settle)

    assert doc["settling"]["drift_bytes"] == settle.drift
    assert doc["settling"]["recheck_gap_seconds"] == settle.gap
    # The compact warning prints the same figure, unsigned, with its direction in
    # a word -- so the document's signed value has to reproduce it exactly.
    assert (
        "a re-stat 6s later found {} more allocated".format(report.human_bytes(abs(settle.drift)))
        in compact
    ), compact


def test_a_null_restat_that_could_be_believed_still_stands(tmp_path):
    """The other half of the rule, which this fix must not disturb.

    `rdu --settle-wait 120` on a tree that has not moved gets an answer, and the
    document must keep agreeing with the terminal's "the figure looks settled".
    """
    root = str(tmp_path / "quiet")
    os.makedirs(root)
    for i in range(4):
        with io.open(os.path.join(root, "f%d" % i), "wb") as handle:
            handle.write(b"z" * 8192)
    res = walk(root, threads=1, depth=1)
    settle = recheck_settling(res)
    settle.gap = 120.0
    assert settle.conclusive and not settle.moved

    doc, _compact, full = _both(res, settle)
    assert "looks settled" in full, full
    assert doc["settling"]["settled"] is True
    assert doc["settling"]["headline_provisional"] is False


def test_the_drift_rule_makes_no_claim_the_stat_free_walk_cannot_support():
    """`-c` read no blocks, so it has no headline to call provisional."""
    res = walkmod.WalkResult("/tmp/counted")
    res.count_only = True
    res.files, res.dirs = 3, 1
    settle = SettleCheck()
    settle.ran = True
    settle.drift = 1 << 20
    assert settle.moved
    assert report._headline_is_provisional(res, settle) is False


def test_the_two_surfaces_agree_about_the_word_on_a_grown_tree(tmp_path):
    """The parity invariant `test_audit_round_six` states, on a real fixture.

    That test's ``_checked`` fixture always carries unlanded bytes, so the
    estimate fired for an unrelated reason and the measured-drift case passed
    without ever exercising the rule. This builds the drift instead.
    """
    res, settle = _grown(str(tmp_path / "growing"))
    doc, compact, full = _both(res, settle)
    claims_provisional = "provisional" in compact or "provisional" in full
    assert doc["settling"]["headline_provisional"] == claims_provisional


# --------------------------------------------------------------------------
# CONTROL: the clean, settled, fully readable walk neither fix may move


def _stats(path):
    """Every inode in the tree at ``path``, root included, via ``os.lstat``."""
    yield os.lstat(path)
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            yield os.lstat(os.path.join(dirpath, name))


def _du(path):
    """``(allocated, apparent)`` bytes for one tree, from ``os.lstat`` alone.

    An INDEPENDENT oracle, which is what lets the control below keep pinning a
    measurement now that three of its literals have come out. It is `du`'s
    arithmetic -- sum ``st_blocks * 512``, sum ``st_size`` -- reached without
    touching :mod:`rapidu.walk`, so agreement is still evidence and not the
    report agreeing with itself.
    """
    allocated = apparent = 0
    for st in _stats(path):
        allocated += st.st_blocks * 512
        apparent += st.st_size
    return allocated, apparent


#: The cells the FILESYSTEM decides, tokenised out of the pinned lines below.
#:
#: These literals were recorded on a ``/tmp`` where a directory costs no block
#: and the allocation unit is 8 KiB. On the CI runner each of the three
#: directories costs 4096 and the unit is 4 KiB, so the same ten files rendered
#: ``284.0 KiB ... 85.9%`` against the recorded ``272.0 KiB ... 88.2%`` -- three
#: red tests that said nothing at all about the report, because what they had
#: pinned was the mount.
#:
#: Tokenised rather than dropped, and this is still a line-for-line control: the
#: column order, the header, the rule width, the entry names, their ORDER, the
#: inode counts, the blank lines and the ``9 more`` footer all stay byte-exact,
#: and every number that moved is compared against :func:`_du` in the document
#: test -- a stronger check than a literal transcribed from one host.
_BYTES = re.compile(r"\d+(?:\.\d+)? (?:B|KiB|MiB|GiB|TiB)")
_SHARE = re.compile("[\u2588-\u258f\u2591\u2592]+ +" + r"(?:\d+\.\d%|<0\.1%|>99\.9%)")
_RATIO = re.compile(r"(?:\d+(?:\.\d+)?|<0\.1|>99\.9)x")


def _pinned(lines, root):
    """``lines`` with the path and every filesystem-chosen cell tokenised."""
    out = []
    for line in lines:
        line = line.replace(root, "ROOT")
        line = _SHARE.sub("SHARE", line)
        line = _RATIO.sub("RATIO", line)
        out.append(_BYTES.sub("SIZE", line))
    return out


def _control_tree(root):
    """Ten files in two directories, all outside the settle window."""
    os.makedirs(root)
    for name, count, payload in (("a", 6, 40000), ("b", 4, 8000)):
        sub = os.path.join(root, name)
        os.makedirs(sub)
        for i in range(count):
            with io.open(os.path.join(sub, "f%d" % i), "wb") as handle:
                handle.write(b"x" * payload)
    return root


def _control(root):
    res = walk(_control_tree(root), threads=2, depth=2, settle_window=_SETTLED_WINDOW)
    # The premise of the control: nothing recent, nothing refused, nothing cut
    # short. If any of these is false the fixture is not the case being pinned.
    assert res.complete and not res.partial
    assert not res.recent_files and not res.touched_files
    assert not res.unreadable_dir_count and not res.unstatable
    res.elapsed = 0.5  # pinned: the rendered rate and the JSON second both use it
    return res, recheck_settling(res)


def test_the_control_walk_renders_exactly_as_it_did(tmp_path):
    """Byte-identical text on a clean settled walk, in both text surfaces.

    Pinned against literals rather than against a recomputation, because a
    recomputation of the report by the report is not a control. Two things get
    substituted rather than pinned: the path, since ``tmp_path`` moves, and the
    cells the filesystem decides -- see :data:`_BYTES` for what those cost when
    they are written into the literals, and :func:`_du` for the oracle that
    checks them instead.
    """
    root = str(tmp_path / "control")
    res, settle = _control(root)

    compact = report.render_compact(res, settle, 3, False, PLAIN)
    assert _pinned(compact, root) == [
        "ROOT",
        "SIZE  ·  13 inodes  ·  0.50s",
        "",
        "  " + "─" * 55,
        "        size  share                          inodes  entry",
        "   SIZE  SHARE          7  a/",
        "    SIZE  SHARE          1  a/f0",
        "    SIZE  SHARE          1  a/f1",
        "  9 more — use -n 0 for all",
    ], compact

    full = report.render_walk(res, settle, 3, style=PLAIN)
    # The headline is checked separately: `render_walk` truncates the path to fit
    # the frame, and a `tmp_path` is long enough to be truncated, so it is the one
    # line whose text a literal cannot own.
    assert full[0] == ""
    allocated, _apparent_unused = _du(root)
    assert full[1].startswith("WALK  "), full[1]
    assert full[1].endswith("   " + report.human_bytes(allocated)), full[1]
    assert _pinned(full[2:5], root) == [
        "  13 inodes (10 files + 3 dirs)  0.50s at 2 threads (26 inodes/s)",
        "  apparent SIZE (RATIO allocated)  SIZE allocation unit",
        "  " + "─" * 55,
    ], full[2:5]

    # Neither surface has a settling section to print, and neither invents one.
    joined = _flat(compact) + " " + _flat(full)
    assert "settling" not in joined and "provisional" not in joined, joined


def test_the_control_document_is_unchanged(tmp_path):
    """The same walk's document, key for key, against the same literals."""
    root = str(tmp_path / "control")
    res, settle = _control(root)
    doc = report.to_json(res, settle, None, None, None, 3)

    assert doc["tool"] == "rapidu" and doc["schema"] == 5
    walk_doc = doc["walk"]
    # Both totals are the filesystem's accounting rather than the tool's, so
    # both are compared against `_du`'s independent `os.lstat` reading.
    # `apparent_bytes` moves too, and less obviously than the allocation: a
    # directory's own `st_size` is ~45 bytes here and 4096 on ext4.
    allocated, apparent = _du(root)
    assert walk_doc["size_bytes"] == allocated
    assert walk_doc["apparent_bytes"] == apparent
    # The bytes this tree was BUILT to hold -- 6 x 40000 + 4 x 8000 -- which is
    # the one size figure no mount gets a vote on, and so the literal that can
    # stay a literal.
    dir_bytes = sum(os.lstat(os.path.join(root, name)).st_size for name in (".", "a", "b"))
    assert apparent - dir_bytes == 272000
    assert walk_doc["files"] == 10
    assert walk_doc["dirs"] == 3
    assert walk_doc["inodes"] == 13
    # A real 0, not an absence: this walk stat'ed every entry and found none.
    assert walk_doc["symlinks"] == 0
    assert walk_doc["specials"] == 0
    assert walk_doc["hardlinked_inodes"] == 0
    assert walk_doc["hardlink_extra_refs"] == 0
    assert walk_doc["complete"] is True
    assert walk_doc["interrupted"] is False
    assert walk_doc["unreadable_dirs"] == 0
    assert walk_doc["elapsed_seconds"] == 0.5
    # The unit belongs to the mount -- 8 KiB where this was recorded, 4 KiB on
    # the runner -- so what is pinned is the claim `alloc_unit` makes about it:
    # a real power of two, measured from the padded files and never guessed.
    unit = walk_doc["allocation"]["unit_bytes"]
    assert unit and unit >= 512 and unit & (unit - 1) == 0, unit
    assert walk_doc["allocation"]["material"] is False
    assert [row["bytes"] for row in walk_doc["top_by_size"]] == [
        _du(os.path.join(root, "a"))[0],
        _du(os.path.join(root, "a", "f0"))[0],
        _du(os.path.join(root, "a", "f1"))[0],
    ]
    # The ranking itself, which no filesystem decides: the subtree first, then
    # two of its files.
    assert [row["path"].replace(root, "ROOT") for row in walk_doc["top_by_size"]] == [
        os.path.join("ROOT", "a"),
        os.path.join("ROOT", "a", "f0"),
        os.path.join("ROOT", "a", "f1"),
    ]

    settling = doc["settling"]
    assert settling["recent_files"] == 0
    assert settling["touched_files"] == 0
    assert settling["drift_bytes"] == 0
    assert settling["moved"] is False
    assert settling["vanished_files"] == 0
    assert settling["vanished_allocated_bytes"] == 0
    assert settling["recheck_measured_nothing"] is False
    assert settling["unlanded_bytes"] == 0
    # Nothing was recent, so there is nothing to be unsettled about: the
    # strongest claim in the section, and the one both fixes have to leave alone.
    assert settling["settled"] is True
    assert settling["headline_provisional"] is False


def test_the_control_headline_is_the_same_number_in_both_surfaces(tmp_path):
    """The one comparison this whole module exists to make, on the clean case."""
    root = str(tmp_path / "control")
    res, settle = _control(root)
    doc = report.to_json(res, settle, None, None, None, 3)

    # The oracle names the figure and then all three surfaces have to say it.
    # A literal here only ever named it for one mount.
    headline = report.human_bytes(_du(root)[0])
    assert report.human_bytes(doc["walk"]["size_bytes"]) == headline
    assert headline in _flat(report.render_compact(res, settle, 3, False, PLAIN))
    assert headline in _flat(report.render_walk(res, settle, 3, style=PLAIN))
