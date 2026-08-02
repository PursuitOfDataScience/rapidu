"""CLI dispatch, exit codes, and JSON shape."""

import json
import os

import pytest

from rapidu import cli
from rapidu import walk as walkmod
from rapidu.fmt import human_bytes, human_count, human_duration, pct


@pytest.fixture
def small_tree(tmp_path):
    root = str(tmp_path / "t")
    os.makedirs(root)
    for i in range(3):
        with open(os.path.join(root, "f%d" % i), "wb") as fh:
            fh.write(b"x" * 4096)
    return root


def test_a_directory_named_like_an_old_subcommand_is_measured(tmp_path, capsys):
    """The reason subcommands were removed: these are ordinary directory names.

    `rdu deleted` used to mean "scan for unlinked-but-open files" even when
    ./deleted was a real directory the user wanted measured. A path is now
    always a path.
    """
    for name in ("quota", "walk", "deleted"):
        d = tmp_path / name
        d.mkdir()
        (d / "f").write_bytes(b"x" * 4096)
        import os

        cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            cli.main([name])
        finally:
            os.chdir(cwd)
        out = capsys.readouterr().out
        assert name in out, name
        assert "files" in out, name
        assert "UNLINKED" not in out, "%s was taken as a subcommand" % name


def test_quota_only_flag(capsys):
    cli.main(["--quota-only"])
    out = capsys.readouterr().out
    assert "QUOTA" in out
    assert "WALK" not in out


def test_deleted_only_flag(capsys):
    cli.main(["--deleted-only"])
    out = capsys.readouterr().out
    assert "UNLINKED BUT STILL OPEN" in out
    assert "WALK" not in out


def test_conflicting_only_flags_rejected():
    with pytest.raises(SystemExit):
        cli.main(["--quota-only", "--deleted-only"])


def test_removed_subcommand_word_gets_a_pointer(tmp_path, capsys):
    """`rdu quota` with no ./quota directory should say what to type instead."""
    import os

    cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        assert cli.main(["quota"]) == cli.EXIT_ERROR
    finally:
        os.chdir(cwd)
    err = capsys.readouterr().err
    assert "no longer a subcommand" in err
    assert "--quota-only" in err


def test_default_answers_how_big_and_nothing_else(small_tree, capsys):
    """`rdu .` was asked a size question. It must not run an audit.

    The quota backend shells out to a site wrapper and the /proc sweep visits
    every pid on the node; neither is used to answer "how big is this tree".
    """
    cli.main([small_tree])
    out = capsys.readouterr().out
    assert small_tree in out
    # Byte totals are not asserted: on GPFS a just-written fixture reports
    # delayed-allocation blocks, which is the effect this package reports
    # elsewhere and has no business making a CLI test flaky.
    # "files", not "inodes": the quota the reader is up against uses that word.
    assert "files" in out
    for section in ("QUOTA", "WALK", "RECONCILE", "UNLINKED"):
        assert section not in out, section
    # Headline, blank, column header, then one line per child.
    assert len(out.strip().splitlines()) <= 8


def test_full_flag_restores_the_whole_report(small_tree, capsys):
    cli.main([small_tree, "-a", "--no-quota", "--no-deleted"])
    out = capsys.readouterr().out
    assert "WALK" in out


def test_inodes_flag_switches_the_ranking(small_tree, capsys):
    cli.main([small_tree, "-i"])
    assert small_tree in capsys.readouterr().out


def test_json_implies_the_full_document(small_tree, capsys):
    """Tooling wants everything, not whichever subset the terminal view shows."""
    cli.main([small_tree, "--json", "--no-quota", "--no-deleted"])
    doc = json.loads(capsys.readouterr().out)
    assert "walk" in doc and "settling" in doc


def test_no_density_ranking_section(small_tree, capsys):
    """A files/GiB ranking is won by the smallest denominator.

    It nominated a 260 KiB .git directory as the "best candidate to pack" ahead
    of one holding ten times the inodes. Density is a column now, not a ranking.
    """
    cli.main([small_tree, "-a", "--no-quota", "--no-deleted", "-n", "5"])
    out = capsys.readouterr().out
    assert "DENSEST" not in out


def test_clean_deleted_scan_is_one_line(small_tree, capsys):
    """In the full report, a null result must not cost seven lines of caveats."""
    cli.main([small_tree, "-a", "--no-quota"])
    out = capsys.readouterr().out
    assert "UNLINKED BUT STILL OPEN" not in out
    assert len([ln for ln in out.splitlines() if "unlinked-but-open" in ln]) == 1


def test_no_quota_skips_the_quota_section(small_tree, capsys):
    cli.main([small_tree, "-a", "--no-quota", "--no-deleted"])
    assert "QUOTA" not in capsys.readouterr().out


def test_json_is_valid_and_shaped(small_tree, capsys):
    cli.main([small_tree, "--no-quota", "--no-deleted", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["tool"] == "rapidu"
    assert doc["walk"]["root"] == small_tree
    assert doc["walk"]["files"] == 3
    assert doc["walk"]["complete"] is True
    assert "settling" in doc


def test_json_settled_is_null_when_inconclusive(small_tree, capsys):
    """A blind check must report null, not a reassuring false."""
    cli.main([small_tree, "--no-quota", "--no-deleted", "--json"])
    doc = json.loads(capsys.readouterr().out)
    s = doc["settling"]
    if s["recent_files"] and not s["conclusive"]:
        assert s["settled"] is None


def test_missing_path_errors(capsys):
    assert cli.main(["/definitely/not/here"]) == cli.EXIT_ERROR
    assert "no such path" in capsys.readouterr().err


def test_file_argument_rejected(tmp_path, capsys):
    f = tmp_path / "f"
    f.write_text("x")
    assert cli.main([str(f)]) == cli.EXIT_ERROR
    assert "not a directory" in capsys.readouterr().err


def test_thread_clamp_warns(small_tree, capsys):
    cli.main([small_tree, "--threads", "999"])
    assert "clamped" in capsys.readouterr().err


def test_deleted_only_json(capsys):
    rc = cli.main(["--deleted-only", "--json"])
    assert rc in (cli.EXIT_OK, cli.EXIT_ATTENTION)
    doc = json.loads(capsys.readouterr().out)
    assert doc["deleted_but_open"]["node_local_only"] is True


def test_version():
    assert cli.main(["--version"]) == cli.EXIT_OK


def test_help_mentions_it_is_not_more_accurate(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "not more accurate" in capsys.readouterr().out


# ---- formatting ----------------------------------------------------------


def test_absent_is_na_not_zero():
    """Constraint 10: [N/A] is not zero."""
    assert human_bytes(None) == "n/a"
    assert human_count(None) == "n/a"
    assert human_duration(None) == "unknown"
    assert human_bytes(0) == "0 B"


def test_human_bytes_units():
    assert human_bytes(1023) == "1023 B"
    assert human_bytes(1024) == "1.0 KiB"
    assert human_bytes(1 << 30) == "1.0 GiB"
    assert human_bytes(-(1 << 20)) == "-1.0 MiB"


def test_human_duration():
    assert human_duration(45) == "45s"
    assert human_duration(1680) == "28m 0s"
    assert "in the future" in human_duration(-10)


def test_pct_guards_zero_denominator():
    assert pct(1, 0) == "n/a"
    assert pct(None, 10) == "n/a"
    assert pct(0.5, 1.0) == "50.0%"


def test_interrupted_output_states_no_total_and_no_shares(small_tree, capsys, monkeypatch):
    """An interrupted walk must not present itself as a measurement."""
    real_walk = walkmod.walk

    def interrupted(*a, **kw):
        res = real_walk(*a, **kw)
        res.partial = True
        # Nothing finished, the harshest case.
        res.finished_tops = set()
        return res

    monkeypatch.setattr(walkmod, "walk", interrupted)
    cli.main([small_tree, "--no-progress"])
    out = capsys.readouterr().out
    assert "INTERRUPTED" in out
    assert "PARTIAL" in out
    assert "no total and no share" in out
    # No percentage may be printed, because there is no denominator.
    assert "%" not in out


def test_n_zero_shows_everything_and_no_phantom_remainder(small_tree, capsys):
    """-n 0 means all. With nothing hidden there is no "(N more)" row.

    The leftover with everything shown is exactly the root directory's own
    inode, which belongs to no child; reporting it as "(0 more)" is noise.
    """
    cli.main([small_tree, "-n", "0"])
    out = capsys.readouterr().out
    assert "more" not in out


def test_truncated_listing_says_how_to_expand(tmp_path, capsys):
    """A listing that hides rows without saying so is just missing data."""
    root = str(tmp_path / "many")
    import os

    for i in range(12):
        d = os.path.join(root, "d%02d" % i)
        os.makedirs(d)
        with open(os.path.join(d, "f"), "wb") as fh:
            fh.write(b"x" * (4096 * (i + 1)))
    cli.main([root, "-n", "3"])
    out = capsys.readouterr().out
    assert "more" in out and "-n 0" in out
