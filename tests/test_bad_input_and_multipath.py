"""`-Q` obeys the same path contract as walk, and `--json` stays parseable.

Five High findings from the audit, each reproduced before being fixed:

* `rdu -Q /definitely/not/here` printed a quota table and exited **0**, while
  the same path through walk or `-D` exited 2 — `cmd_quota` never checked
  `exists`. A script branching on the exit code read a typo as success.
* `rdu -D --json a b` printed one document per loop iteration, i.e. NDJSON:
  `json.load` gave `JSONDecodeError: Extra data`. `--help` promises "one
  document per PATH, or a list of them when several are given", which was
  true of walk only.
* `rdu -Q --json a b` probed and recorded `paths[0]` only, so
  `rdu -Q ~ /scratch/lustre` reconciled Lustre against `$HOME`'s backend and
  the document named neither path.
* `--settle-window nan` (and `inf`, and the four other float flags) passed
  validation and exited 0, because every guard is a comparison and `nan < 0`
  is False. `nan` then reached the token bucket, `communicate(timeout=nan)`
  and `age > max_snapshot_age` — always False, silently disabling the
  staleness gate the `<= 0` guard exists to protect.
* `rdu -Q good '~nosuchuser_xyz/foo'` returned exit 2 with **zero** bytes on
  stdout, discarding the path that was fine, while walk and `-D` report the
  valid ones and still exit 2.
"""

from __future__ import annotations

import json

import pytest

from rapidu import cli


@pytest.fixture
def two_trees(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "f").write_text("hi\n")
    (tmp_path / "b" / "g").write_text("ho\n")
    return str(tmp_path / "a"), str(tmp_path / "b")


class TestQuotaObeysThePathContract:
    def test_a_nonexistent_path_is_an_error(self, tmp_path, capsys):
        missing = str(tmp_path / "definitely-not-here")
        assert cli.main(["--quota-only", missing, "--no-box"]) == cli.EXIT_ERROR
        assert "no such path" in capsys.readouterr().err

    def test_walk_and_quota_now_agree_about_a_missing_path(self, tmp_path):
        # The control that made this a defect rather than a preference: the two
        # commands disagreed about the same input.
        missing = str(tmp_path / "definitely-not-here")
        assert cli.main([missing, "--no-quota", "--no-box"]) == cli.EXIT_ERROR
        assert cli.main(["--quota-only", missing, "--no-box"]) == cli.EXIT_ERROR

    def test_a_real_path_still_reports(self, two_trees, capsys):
        good, _ = two_trees
        code = cli.main(["--quota-only", good, "--no-box"])
        assert code in (cli.EXIT_OK, cli.EXIT_ATTENTION)
        assert capsys.readouterr().out.strip()

    def test_one_bad_path_does_not_discard_the_good_one(self, two_trees, capsys):
        good, _ = two_trees
        code = cli.main(["--quota-only", good, "~nosuchuser_xyz/foo", "--no-box"])
        captured = capsys.readouterr()
        # Still an error -- the caller asked about something unreadable -- but
        # the readable path is reported, which is the partial-failure contract
        # `_resolve_paths` documents and walk already honoured.
        assert code == cli.EXIT_ERROR
        assert captured.out.strip(), "the good path was discarded"


class TestJsonStaysParseable:
    def test_deleted_with_two_paths_is_one_json_list(self, two_trees, capsys):
        good, other = two_trees
        cli.main(["--deleted-only", "--json", good, other])
        payload = json.loads(capsys.readouterr().out)  # would raise on NDJSON
        assert isinstance(payload, list)
        assert len(payload) == 2

    def test_deleted_with_one_path_is_one_object(self, two_trees, capsys):
        # The control on the batching: one path must stay an object, which is
        # what `--help` promises and what walk already did.
        good, _ = two_trees
        cli.main(["--deleted-only", "--json", good])
        assert isinstance(json.loads(capsys.readouterr().out), dict)

    def test_quota_with_two_paths_names_both(self, two_trees, capsys):
        good, other = two_trees
        cli.main(["--quota-only", "--json", good, other])
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list)
        assert [doc["quota"]["path"] for doc in payload] == [good, other]

    def test_quota_with_one_path_is_one_object_that_names_it(self, two_trees, capsys):
        good, _ = two_trees
        cli.main(["--quota-only", "--json", good])
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, dict)
        assert payload["quota"]["path"] == good


class TestNonFiniteNumbersAreRefused:
    FLAGS = [
        "--settle-window",
        "--max-dirs-per-sec",
        "--quota-timeout",
        "--settle-wait",
        "--max-snapshot-age",
    ]

    @pytest.mark.parametrize("flag", FLAGS)
    @pytest.mark.parametrize("value", ["nan", "inf", "-inf", "Infinity"])
    def test_a_non_finite_value_exits_two(self, two_trees, flag, value):
        good, _ = two_trees
        with pytest.raises(SystemExit) as exit_info:
            cli.main([good, flag, value, "--no-box", "--no-quota"])
        assert exit_info.value.code == 2

    @pytest.mark.parametrize("flag", FLAGS)
    def test_a_finite_value_is_still_accepted(self, two_trees, flag):
        # The control: the range guards below the finiteness check must keep
        # working, so a sensible number still runs.
        good, _ = two_trees
        assert cli.main([good, flag, "30", "--no-box", "--no-quota"]) in (
            cli.EXIT_OK,
            cli.EXIT_ATTENTION,
        )

    def test_the_range_guards_still_refuse_their_own_cases(self, two_trees):
        # Controls that pass either way: negative and zero were already
        # refused, and this fix must not have replaced those checks.
        good, _ = two_trees
        for flag, bad in (
            ("--settle-window", "-5"),
            ("--quota-timeout", "0"),
            ("--max-snapshot-age", "0"),
        ):
            with pytest.raises(SystemExit) as exit_info:
                cli.main([good, flag, bad, "--no-box", "--no-quota"])
            assert exit_info.value.code == 2, (flag, bad)
