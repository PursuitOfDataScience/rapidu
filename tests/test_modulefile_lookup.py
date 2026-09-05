"""`_modulefile_for` described its own mechanism backwards.

The reclaim suggestion is chosen in three tiers — the tool if it is on `PATH`,
else `module load <tool> && …` if a modulefile exists, else the quoted `rm -rf`.
The middle tier reads `MODULEPATH`, and its docstring used to say:

    both Lmod and environment-modules lay ``MODULEPATH`` out as one entry per
    package, so an entry named ``uv`` means ``module load uv`` resolves

A `MODULEPATH` entry is a search **root**; the per-package names live inside it.
The code has always joined the root to the tool name, so it is correct — and
under the layout that sentence described it finds nothing, which is provable in
one line and is pinned below. Measured on this cluster: `MODULEPATH` is the single
entry `/software/modulefiles`, holding 244 entries (239 bare-named directories, 8
files, no `.lua`), one per package.

Nothing tested this function at all, which is how the wording survived. The risk
the wording carries is not a wrong answer today — it is that a reader who trusts
it "corrects" the join and breaks the layout every cluster actually uses.
"""

import os

import pytest

from rapidu.report import _modulefile_for


@pytest.fixture
def modulepath(monkeypatch):
    """Set `MODULEPATH` to the given roots for one test.

    Synthetic rather than the real one: rapidu runs on clusters this campaign has
    never seen, and a test that reads `/software/modulefiles` would pass here and
    say nothing anywhere else.
    """

    def _set(*roots):
        monkeypatch.setenv("MODULEPATH", os.pathsep.join(str(r) for r in roots))

    return _set


class TestTheEntryIsASearchRootNotAPackage:
    def test_a_directory_named_for_the_tool_inside_a_root_is_found(self, tmp_path, modulepath):
        """The layout every cluster in this campaign uses: 239 of 244 entries."""
        (tmp_path / "uv").mkdir()
        modulepath(tmp_path)
        assert _modulefile_for("uv") == "uv"

    def test_a_file_named_for_the_tool_is_found_too(self, tmp_path, modulepath):
        """The other 8: a modulefile can be a plain file rather than a version
        directory, and `os.path.exists` covers both — which is why the check is
        `exists` and not `isdir`."""
        (tmp_path / "apptainer").write_text("#%Module\n")
        modulepath(tmp_path)
        assert _modulefile_for("apptainer") == "apptainer"

    def test_a_root_that_IS_the_package_finds_nothing(self, tmp_path, modulepath):
        """The line that proves the old wording was backwards.

        "one entry per package" means `MODULEPATH=/…/modulefiles/uv`. The lookup
        then needs `/…/modulefiles/uv/uv`, which no layout creates, so it returns
        "". Pinned as the current, deliberate behaviour — the caller falls through
        to the quoted `rm -rf`, which works regardless — so that a future reader
        finds the case decided rather than looking like an oversight.
        """
        pkg = tmp_path / "uv"
        pkg.mkdir()
        modulepath(pkg)
        assert _modulefile_for("uv") == ""

    def test_a_symlinked_root_is_followed(self, tmp_path, modulepath):
        """`/software/modulefiles` is a symlink to `repo/modulefiles` here, so the
        check has to follow one. `os.path.exists` does; `find` without `-L` does
        not, which is what first made this layout look empty when I measured it.
        """
        real = tmp_path / "repo"
        (real / "singularity").mkdir(parents=True)
        link = tmp_path / "modulefiles"
        link.symlink_to(real)
        modulepath(link)
        assert _modulefile_for("singularity") == "singularity"

    def test_every_root_is_searched_not_just_the_first(self, tmp_path, modulepath):
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        (second / "uv").mkdir(parents=True)
        modulepath(first, second)
        assert _modulefile_for("uv") == "uv"


class TestTheDocstringSaysWhatTheCodeDoes:
    def test_it_calls_the_entry_a_root(self):
        doc = _modulefile_for.__doc__ or ""
        assert "search ROOT" in doc or "search **root**" in doc, doc

    def test_the_backwards_sentence_is_gone_from_the_claim(self):
        """The exact phrase, and only where it would be a CLAIM: the docstring now
        quotes it to say it was wrong, so a bare substring check would fail on its
        own fix. Keyed on the assertion form instead."""
        doc = _modulefile_for.__doc__ or ""
        assert "lay ``MODULEPATH`` out as one entry per package, so" not in doc
        assert "used to say" in doc, doc


class TestControls:
    def test_control_an_unset_modulepath_is_not_a_match(self, monkeypatch):
        """Anywhere without modules. Holds before and after the fix."""
        monkeypatch.delenv("MODULEPATH", raising=False)
        assert _modulefile_for("uv") == ""

    def test_control_an_empty_modulepath_is_not_a_match(self, monkeypatch):
        monkeypatch.setenv("MODULEPATH", "")
        assert _modulefile_for("uv") == ""

    def test_control_a_tool_that_is_nowhere_is_not_a_match(self, tmp_path, modulepath):
        (tmp_path / "uv").mkdir()
        modulepath(tmp_path)
        assert _modulefile_for("nonesuch") == ""

    def test_control_an_empty_entry_in_the_list_is_skipped(self, tmp_path, monkeypatch):
        """A trailing separator gives an empty entry, and `os.path.join("", tool)`
        is a RELATIVE path — so without the `if root` guard this would answer from
        the current working directory. Holds in both states; pinned because the
        guard is easy to lose."""
        (tmp_path / "uv").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MODULEPATH", "")
        assert _modulefile_for("uv") == ""
        monkeypatch.setenv("MODULEPATH", os.pathsep + os.pathsep)
        assert _modulefile_for("uv") == ""
