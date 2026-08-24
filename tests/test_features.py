"""The post-processing features: cold data, reclaimable caches, gids, sort, JSON.

All five share one property, and it is why they were chosen together: they read
data the walk *already collects and used to discard*. No new syscall, no new
subprocess, no new dependency. `st_mtime` was read for the settling check and
thrown away; `st_gid` was never read at all despite the report claiming to answer
a group-quota question.
"""

import json
import os

import pytest

from rapidu import cli, report, ui
from rapidu.fmt import human_bytes
from rapidu.walk import AGE_BUCKET_LABELS, WATCHED_DIR_NAMES, SettleCheck, walk


@pytest.fixture
def tree(tmp_path):
    """A tree with an old file, a new file, and two recognisable caches."""
    root = str(tmp_path / "t")
    os.makedirs(root)
    fresh = os.path.join(root, "fresh")
    os.makedirs(fresh)
    for j in range(4):
        with open(os.path.join(fresh, "f%d" % j), "wb") as fh:
            fh.write(b"x" * 8192)

    stale = os.path.join(root, "stale")
    os.makedirs(stale)
    old = os.path.join(stale, "ancient.bin")
    with open(old, "wb") as fh:
        fh.write(b"y" * 65536)
    two_years = 2 * 365 * 86400
    now = os.stat(old).st_mtime
    os.utime(old, (now - two_years, now - two_years))

    # Three levels down, which is the whole point: a default -d 1 walk puts
    # nothing this deep into dir_agg.
    hf = os.path.join(root, "sub", ".cache", "huggingface")
    os.makedirs(hf)
    with open(os.path.join(hf, "blob"), "wb") as fh:
        fh.write(b"z" * 32768)
    pycache = os.path.join(root, "pkg", "__pycache__")
    os.makedirs(pycache)
    with open(os.path.join(pycache, "m.pyc"), "wb") as fh:
        fh.write(b"w" * 4096)
    return root


# ---------------------------------------------------------------------------
# F31 -- by_gid
# ---------------------------------------------------------------------------


def test_the_walk_records_gids(tree):
    """A group quota is charged by gid, and the walk never read st_gid.

    `render_walk` captioned its *uid* table "a group quota charges all of these",
    which answers a question nobody asked: the two diverge exactly when it
    matters, because a file written into a shared directory whose setgid bit is
    missing lands in the writer's personal group.
    """
    res = walk(tree, threads=2, depth=1)
    assert res.by_gid, "no gid was recorded"
    assert sum(f for _b, f in res.by_gid.values()) == sum(f for _b, f in res.by_uid.values())


def test_the_uid_caption_no_longer_claims_to_answer_the_group_question(tree):
    res = walk(tree, threads=2, depth=1)
    text = "\n".join(report.render_walk(res, SettleCheck(), 10, style=ui.resolve_style("never")))
    assert "owners (a group quota charges all of these)" not in text


# ---------------------------------------------------------------------------
# F13 -- cold data by age
# ---------------------------------------------------------------------------


def test_age_buckets_separate_old_files_from_new(tree):
    res = walk(tree, threads=2, depth=1)
    by_label = dict(zip(AGE_BUCKET_LABELS, res.by_age))
    assert by_label["> 1y"][1] == 1, "the two-year-old file is not in the oldest bucket"
    assert by_label["< 7d"][1] >= 4, "the fresh files are not in the youngest bucket"


def test_every_regular_file_lands_in_exactly_one_bucket(tree):
    """The buckets partition the files; a file in two of them double-counts bytes."""
    res = walk(tree, threads=2, depth=1)
    bucketed = sum(inodes for _b, inodes in res.by_age)
    # Directories are deliberately not bucketed: their mtime tracks their
    # *contents* changing, so counting them would report the same event twice.
    regular = res.files - res.hardlink_extra_refs - res.symlinks
    assert bucketed == regular, "{} bucketed vs {} regular files".format(bucketed, regular)


def test_the_age_report_renders_the_buckets(tree):
    res = walk(tree, threads=2, depth=1)
    text = "\n".join(report.render_age(res, ui.resolve_style("never")))
    assert "BY AGE" in text
    for label in AGE_BUCKET_LABELS:
        assert label in text


def test_the_cold_finding_fires_on_inodes_when_bytes_are_still_settling(tree):
    """On GPFS a just-written tree reports st_blocks=0 until allocation catches up.

    Gating the finding on bytes alone made it vanish on exactly the freshly
    written trees this tool is run against -- and inodes are a limit in their own
    right, so there was a finding to report either way.
    """
    res = walk(tree, threads=2, depth=1)
    # Simulate the unsettled case explicitly rather than racing the filesystem.
    res.size = 0
    res.by_age = [(0, 0)] * (len(AGE_BUCKET_LABELS) - 1) + [(0, res.inodes)]
    text = "\n".join(report.render_age(res, ui.resolve_style("never")))
    # The claim is that the finding *fires*; matching the verb too pinned
    # "has not been modified" for a count of several, which is the
    # disagreement that wording was later fixed for.
    assert "not been modified in over a year" in text
    # The count half of the sentence, which is what fires when bytes are zero. It
    # reads "N files" rather than "N inodes" since RD-13: `by_age` buckets regular
    # files only, and the share is now taken over that same population.
    assert "files (" in text


def test_the_cold_finding_prefers_bytes_when_they_are_the_bigger_share(tree):
    res = walk(tree, threads=2, depth=1)
    res.size = 1000
    res.by_age = [(0, 0)] * (len(AGE_BUCKET_LABELS) - 1) + [(900, 1)]
    text = "\n".join(report.render_age(res, ui.resolve_style("never")))
    # The claim is that the finding *fires*; matching the verb too pinned
    # "has not been modified" for a count of several, which is the
    # disagreement that wording was later fixed for.
    assert "not been modified in over a year" in text


def test_a_tree_with_nothing_cold_says_nothing(tree):
    """A finding that fires on every run trains the reader to skip the section."""
    res = walk(tree, threads=2, depth=1)
    res.size = 1000
    res.by_age = [(1000, res.inodes)] + [(0, 0)] * (len(AGE_BUCKET_LABELS) - 1)
    text = "\n".join(report.render_age(res, ui.resolve_style("never")))
    assert "has not been modified" not in text


def test_a_count_only_walk_has_no_age_report(tree):
    """`-c` never calls stat, so it has no mtime and must not invent one."""
    res = walk(tree, threads=2, depth=1, count_only=True)
    assert report.render_age(res, ui.resolve_style("never")) == []


# ---------------------------------------------------------------------------
# F15 -- reclaimable caches
# ---------------------------------------------------------------------------


def test_caches_are_found_below_the_reported_depth(tree):
    """This is the failure mode the feature would have shipped with.

    Every cache worth naming sits three or four levels down, and `dir_agg` holds
    only depth-1 entries by default -- so a detector reading it finds nothing on a
    default run and looks implemented while doing nothing.
    """
    res = walk(tree, threads=2, depth=1)
    text = "\n".join(report.render_reclaimable(res, ui.resolve_style("never")))
    assert "RECLAIMABLE" in text
    assert "huggingface" in text
    assert "__pycache__" in text


def test_the_reclaim_command_is_printed_and_nothing_is_deleted(tree, monkeypatch):
    """A command is offered for the cache, whichever of them this host can run.

    Pinning one string made this assert a fact about the machine. `huggingface-cli`
    was renamed to `hf`, and where neither is installed `reclaim_command` falls
    through to the quoted `rm -rf` form -- which is the RD-12 behaviour and is
    correct, but is a third answer. Both worlds are forced here so the test does
    not depend on what happens to be on PATH.
    """
    res = walk(tree, threads=2, depth=1)

    monkeypatch.setattr(
        report.shutil, "which", lambda tool: "/usr/bin/hf" if tool == "hf" else None
    )
    text = "\n".join(report.render_reclaimable(res, ui.resolve_style("never")))
    assert "hf cache prune" in text, text

    monkeypatch.setattr(report.shutil, "which", lambda _tool: None)
    monkeypatch.setattr(report, "_modulefile_for", lambda _tool: "")
    text = "\n".join(report.render_reclaimable(res, ui.resolve_style("never")))
    assert "rm -rf " in text, text
    # The tool's authority comes from being a measurement instrument.
    assert os.path.isdir(os.path.join(tree, "sub", ".cache", "huggingface"))
    assert os.path.isdir(os.path.join(tree, "pkg", "__pycache__"))


def test_watched_subtrees_carry_real_totals(tree):
    res = walk(tree, threads=2, depth=1)
    hits = {p: v for p, v in res.watched.items() if p.endswith("huggingface")}
    assert hits, "the huggingface directory was not accumulated"
    size, inodes = list(hits.values())[0]
    # The blob plus the directory holding it. A watched subtree is counted the way
    # `Entry` counts one -- directories included, the watched directory itself
    # included -- because RECLAIMABLE draws rows from `watched` and from `dir_agg`
    # into one `files` column, and in count mode that column is the ranking key.
    assert inodes == 2
    assert size >= 0  # blocks may still be settling on GPFS


def test_watched_and_dir_agg_agree_on_a_path_they_both_reach(tmp_path):
    """One `files` column, one meaning, whichever source a RECLAIMABLE row came from.

    `watched` was incremented only in the stat arm's *file* branch, which sits below
    the directory `continue` -- so it held regular files while `dir_agg` held files
    plus directories. Cosmetic while the column was decoration; it became the
    ranking key in count mode.
    """
    root = str(tmp_path / "t")
    deep = os.path.join(root, "pkg", "__pycache__", "nested")
    os.makedirs(deep)
    for i in range(3):
        with open(os.path.join(deep, "m%d.pyc" % i), "wb") as fh:
            fh.write(b"w" * 4096)
    with open(os.path.join(root, "pkg", "__pycache__", "top.pyc"), "wb") as fh:
        fh.write(b"w" * 4096)
    for count_only in (False, True):
        res = walk(root, threads=2, depth=3, count_only=count_only)
        key = os.path.join(root, "pkg", "__pycache__")
        assert key in res.watched, res.watched
        entry = res.dir_agg[key]
        size, inodes = res.watched[key]
        # 4 files + __pycache__ + nested
        assert inodes == 6, (count_only, inodes)
        assert (size, inodes) == (entry.size, entry.inodes), count_only


def test_a_count_only_walk_still_counts_its_caches(tmp_path):
    """`rdu -a -c` reported `0 files` for every cache and `0.0% of the tree`.

    The watch slots were allocated on both paths but incremented only on the stat
    path, so after `-c` every watched directory carried (0, 0) -- and because
    `render_reclaimable` lists `watched` first and dedupes by path, that zero
    shadowed the real count `dir_agg` was holding. A fabricated zero, which this
    module's docstring rules out.
    """
    root = str(tmp_path / "t")
    for name in ("__pycache__", os.path.join(".cache", "pip")):
        os.makedirs(os.path.join(root, name))
        with open(os.path.join(root, name, "f"), "wb") as fh:
            fh.write(b"x" * 4096)
    res = walk(root, threads=2, depth=1, count_only=True)
    assert all(n for _b, n in res.watched.values()), res.watched
    text = "\n".join(report.render_reclaimable(res, ui.resolve_style("never")))
    assert "0 files" not in text
    assert "(0.0% of the tree)" not in text
    assert "4 inodes reclaimable in total" in text


def test_a_nested_match_is_not_counted_twice(tmp_path):
    """`.cache/huggingface/hub` inside `.cache/huggingface` is the same bytes."""
    root = str(tmp_path / "t")
    hub = os.path.join(root, ".cache", "huggingface", "hub")
    os.makedirs(hub)
    with open(os.path.join(hub, "blob"), "wb") as fh:
        fh.write(b"z" * 4096)
    res = walk(root, threads=1, depth=1)
    lines = report.render_reclaimable(res, ui.resolve_style("never"))
    rows = [ln for ln in lines if "huggingface" in ln and "inodes" in ln]
    assert len(rows) == 1, rows


def test_watched_names_and_patterns_agree():
    """A pattern whose last component is not watched can never fire."""
    for pattern, _command, _delete_ok in report._RECLAIMABLE:
        leaf = pattern.replace("/", os.sep).split(os.sep)[-1]
        assert leaf in WATCHED_DIR_NAMES or leaf.lstrip(".") in WATCHED_DIR_NAMES, (
            "{!r} is unreachable: {!r} is not in WATCHED_DIR_NAMES".format(pattern, leaf)
        )


# ---------------------------------------------------------------------------
# F45 -- --sort
# ---------------------------------------------------------------------------


def test_sort_density_is_reachable_from_the_terminal(tree, capsys):
    """`top_dirs(key="density")` was implemented, documented, and json-only."""
    assert cli.main([tree, "--sort", "density", "--color", "never", "--no-box"]) == cli.EXIT_OK
    assert capsys.readouterr().out.strip()


@pytest.mark.parametrize("key", ["size", "files", "density"])
def test_every_sort_key_runs(tree, key, capsys):
    assert cli.main([tree, "--sort", key, "--color", "never", "--no-box"]) == cli.EXIT_OK
    capsys.readouterr()


def test_dash_i_still_means_sort_files(tree):
    """`-i` predates `--sort` and is the flag someone reaches for. Keep it."""
    parser = cli.build_parser()
    args = parser.parse_args([tree, "-i"])
    assert args.sort is None and args.inodes
    # main() resolves the two; check the resolution rule directly.
    assert cli.main([tree, "-i", "--color", "never", "--no-box"]) == cli.EXIT_OK


# ---------------------------------------------------------------------------
# F44 -- the JSON holes
# ---------------------------------------------------------------------------


def test_json_carries_what_a_human_reader_is_shown(tree, capsys):
    """A machine consumer could not see the caveats a human gets."""
    cli.main([tree, "--json", "--no-quota", "--no-deleted", "--no-settle-check"])
    doc = json.loads(capsys.readouterr().out)
    # This is the one place that pins the current number, so a bump is never
    # silent. 4: under `-c` every stat-derived figure is `null` rather than 0 (or
    # `true`, for `settled`) -- Constraint 10, which the terminal already obeyed.
    # 3 was `walk.recent_bytes` becoming `settling.recent_allocated_bytes`: it
    # held *allocated* blocks under a bare `bytes`. 2 was `by_age[].inodes`
    # becoming `by_age[].files`: always a file count, so summing it against
    # `inodes` came up short by every directory.
    assert doc["schema"] == 4
    walk_doc = doc["walk"]
    for key in ("by_gid", "by_age", "unreadable_dir_paths"):
        assert key in walk_doc, key
    assert "recent_bytes" not in walk_doc
    assert "recent_allocated_bytes" in doc["settling"]
    assert [b["bucket"] for b in walk_doc["by_age"]] == list(AGE_BUCKET_LABELS)


def test_json_quota_rows_carry_their_mount_detail():
    from rapidu.quota import QuotaRow, QuotaSnapshot

    snap = QuotaSnapshot("quota -s")
    row = QuotaRow("f", "blocks", "user", 1, 2, 2, "", "/home", True)
    row.mounts = ["/home", "/gpfs/home"]
    snap.rows = [row]
    snap.available = True
    snap.time_note = "the age looks like a timezone mis-parse"
    doc = report.to_json(None, None, snap, None, None)
    assert doc["quota"]["time_note"]
    assert doc["quota"]["rows"][0]["mounts"] == ["/home", "/gpfs/home"]
    assert doc["quota"]["rows"][0]["mount_guessed"] is True


# ---------------------------------------------------------------------------
# The header facts, and saying when the table is truncated
# ---------------------------------------------------------------------------


def test_the_header_states_only_measurements_of_the_tree(tree):
    """There used to be a fourth fact and it was the most confusing number here.

    It counted the rows the table *could* draw at the current --depth, while -n
    decided how many were listed -- but sitting between a byte total and a file
    total it read as a third measurement of the tree, and reconciling it against
    the file count is impossible because they count different things. No wording
    rescued it: "95 entries" was opaque and "10 of 95 entries" still leaned on a
    word that means nothing to a reader. It is gone, and the information it carried
    now lives next to the table it describes.
    """
    res = walk(tree, threads=2, depth=1)
    header = report.render_compact(res, SettleCheck(), 4, False, ui.resolve_style("never"))[1]
    # The removed fact was worded "95 entries"; the count that remains is nouned
    # `inodes` since RD-9 (`entries` is reserved for the -c name count).
    assert "entries" not in header
    assert "inodes" in header
    assert "of" not in header.split("inodes")[1]


def test_a_truncated_table_says_so_at_depth_one(tree):
    """Depth 1 gets the remainder row, which carries bytes as well as the count."""
    res = walk(tree, threads=2, depth=1)
    if report._entry_total(res) <= 1:
        pytest.skip("fixture has nothing to hide")
    body = "\n".join(report.render_entries(res, 1, False, ui.resolve_style("never")))
    assert "more" in body and "use -n 0 for all" in body


def test_a_truncated_table_says_so_below_depth_one_too(tree):
    """This is where it used to stop without a word.

    The remainder row carries bytes, so it is only well defined when the listed
    rows partition the tree -- at depth > 1 they nest and a total would
    double-count. So the row is suppressed there, and the table simply ended: at
    `-d 2 -n 10` it showed ten of fifty-nine and looked complete. The count alone
    is true at any depth.
    """
    res = walk(tree, threads=2, depth=3)
    total = report._entry_total(res)
    if total <= 2:
        pytest.skip("fixture is not deep enough")
    body = "\n".join(report.render_entries(res, 2, False, ui.resolve_style("never")))
    assert "more" in body, body
    assert "use -n 0 for all" in body


def test_nothing_is_said_when_nothing_is_hidden(tree):
    """A note that fires on every run is furniture."""
    res = walk(tree, threads=2, depth=1)
    body = "\n".join(report.render_entries(res, 0, False, ui.resolve_style("never")))
    assert "more" not in body


# ---------------------------------------------------------------------------
# One emphasis rule, applied everywhere
# ---------------------------------------------------------------------------

_SGR = ui._ANSI_RE


def _runs(line):
    """[(text, sgr-params)] so a test can assert on weight, not on appearance.

    "" (nothing emitted yet) and "0" (just reset) both mean *default weight*, so
    they are normalised together -- otherwise a row that is uniformly unstyled
    looks like it mixes two weights. At eight colours `style.muted` is deliberately
    no styling at all, because there is no second grey to spare, so that is the
    common case rather than an edge one.
    """
    out, cur, pos = [], "0", 0
    for m in _SGR.finditer(line):
        seg = line[pos : m.start()]
        if seg.strip():
            out.append((seg.strip(), cur))
        cur = m.group(0)[2:-1] or "0"
        pos = m.end()
    if line[pos:].strip():
        out.append((line[pos:].strip(), cur))
    return out


def _tone_of(line, text):
    for seg, code in _runs(line):
        if text in seg:
            return code
    raise AssertionError("{!r} not found in {!r}".format(text, _SGR.sub("", line)))


def _colour_style():
    style = ui.resolve_style("always")
    style.depth = 8  # base-16 codes are stable to assert on
    return style


def test_the_accent_marks_what_the_listing_was_ranked_by(tree):
    """Emphasis has to move when the ranking moves, or it points at the wrong number.

    The byte total carried the accent unconditionally, so `-i` accented the size
    while ordering the table by the file count.
    """
    style = _colour_style()
    res = walk(tree, threads=2, depth=1)
    by_size = report.render_compact(res, SettleCheck(), 3, False, style)[1]
    by_files = report.render_compact(res, SettleCheck(), 3, True, style)[1]

    size = human_bytes(res.size)
    assert _tone_of(by_size, size) == report.ACCENT_SGR
    assert _tone_of(by_files, size) != report.ACCENT_SGR


def test_a_value_is_never_painted_at_the_label_weight(tree):
    """The elapsed time was, which is why it read as a different class of thing.

    `2m 0s` and `21,827` are both measurements; one of them was the same grey as
    the word "files" beside it.
    """
    style = _colour_style()
    res = walk(tree, threads=2, depth=1)
    facts = report.render_compact(res, SettleCheck(), 3, False, style)[1]
    elapsed = "{:.2f}s".format(res.elapsed)
    assert _tone_of(facts, elapsed) != report.LABEL_SGR
    # ...while the noun beside a number still is a label.
    assert _tone_of(facts, "inodes") == report.LABEL_SGR


def test_the_column_header_emphasises_only_the_sorted_column(tree):
    """Three local rules produced bold, dim and dim-again for no legible reason."""
    style = _colour_style()
    head_size = report._entries_header(style, ranked_by_files=False)
    head_files = report._entries_header(style, ranked_by_files=True)
    assert _tone_of(head_size, "size") != _tone_of(head_size, "inodes")
    assert _tone_of(head_files, "inodes") != _tone_of(head_files, "size")
    # `entry` names no measurement, so it is a label under either ranking.
    assert _tone_of(head_size, "entry") == _tone_of(head_files, "entry") == report.LABEL_SGR
    # `share` labels the bar, which draws whichever metric was ranked, so it
    # inherits that column's weight rather than having one of its own.
    assert _tone_of(head_size, "share") == _tone_of(head_size, "size")
    assert _tone_of(head_files, "share") == _tone_of(head_files, "inodes")


def test_the_remainder_row_is_one_weight_throughout(tree):
    """It named real content at the caveat weight, so the row that tells you the
    table is truncated was the faintest thing on screen. The hatched bar is the
    only thing that should mark it as a summary."""
    style = _colour_style()
    res = walk(tree, threads=2, depth=1)
    rows = report.render_entries(res, 1, False, style)
    if len(rows) < 2:
        pytest.skip("fixture has nothing to summarise")
    codes = {code for seg, code in _runs(rows[-1]) if seg.strip("░▒ ")}
    assert len(codes) == 1, "remainder row mixes weights: {}".format(codes)
