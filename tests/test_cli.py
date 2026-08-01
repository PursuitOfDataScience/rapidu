"""CLI dispatch, exit codes, and JSON shape."""

import json
import os

import pytest

from slurmdisk import cli
from slurmdisk.fmt import human_bytes, human_count, human_duration, pct


@pytest.fixture
def small_tree(tmp_path):
    root = str(tmp_path / "t")
    os.makedirs(root)
    for i in range(3):
        with open(os.path.join(root, "f%d" % i), "wb") as fh:
            fh.write(b"x" * 4096)
    return root


def test_command_word_is_split_from_paths(small_tree, capsys):
    """`slurmdisk walk PATH` must not treat 'walk' as a path."""
    assert cli.main(["walk", small_tree, "--no-deleted"]) in (cli.EXIT_OK, cli.EXIT_ATTENTION)
    out = capsys.readouterr().out
    assert "WALK" in out
    assert "no such path" not in out


def test_bare_path_runs_the_full_report(small_tree, capsys):
    cli.main([small_tree, "--no-quota", "--no-deleted"])
    out = capsys.readouterr().out
    assert "WALK" in out
    assert "du" in out  # the footer's honesty note


def test_walk_subcommand_skips_quota(small_tree, capsys):
    cli.main(["walk", small_tree, "--no-deleted"])
    assert "QUOTA" not in capsys.readouterr().out


def test_json_is_valid_and_shaped(small_tree, capsys):
    cli.main([small_tree, "--no-quota", "--no-deleted", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["tool"] == "slurmdisk"
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
    cli.main(["walk", small_tree, "--threads", "999", "--no-deleted"])
    assert "clamped" in capsys.readouterr().err


def test_deleted_subcommand(capsys):
    rc = cli.main(["deleted", "--json"])
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
