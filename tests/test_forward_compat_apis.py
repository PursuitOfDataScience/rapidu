"""Deprecated stdlib APIs, which are the ones CI cannot catch.

`test_py36_compat.py` guards the FLOOR: no module newer than 3.6, no `__future__
annotations`, no PEP 604 unions, ast-parsed at `feature_version=(3, 6)`. It has to be
static because there is no 3.6 runner to be had.

The other end needs no such help, and that asymmetry is why this file is narrow.
Measured: a module PEP 594 *removed* (`cgi`, `pipes`, `telnetlib`, `imghdr`, ...) is an
`ImportError` on 3.13, and CI runs 3.13 — so the ceiling is already enforced by
execution and wants no test.

What execution cannot enforce is the middle: an API that is **deprecated but still
works**. `datetime.utcnow()`, `locale.getdefaultlocale()`, `typing.ByteString`,
`imp`, `distutils`, `pkg_resources` and `importlib.find_loader` pass every job in the
matrix today and fail on some later interpreter. Nothing here held the source away
from them.

**This package is the one most exposed to that list.** It is written to a 3.6 floor
with no dependencies, and `distutils`, `imp` and `pkg_resources` were the *ordinary*
way to do these things in the 3.6 era — so the idiom a 3.6-compatible module reaches
for is exactly the idiom that has since been removed. slurmate, slurmpast and
slurmwatch carry the same scan.

Not asserted here, unlike in slurmwatch: `pyproject.toml` declares only the generic
`Programming Language :: Python :: 3`, with no minor-version classifiers, so there is
no advertised list to compare against the matrix. Adding minors would mean choosing
whether to advertise 3.6-3.8, which `requires-python` allows and nothing executes;
that is a metadata decision, not a polish one.
"""

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "rapidu"

#: Deprecated or removed, but importable on every version CI runs. Same list the
#: three sibling packages scan for, so a change in one transfers.
BANNED = re.compile(
    r"\b(distutils|import imp\b|utcnow|getdefaultlocale|find_loader"
    r"|pkg_resources|typing\.ByteString)\b"
)


def _sources() -> list[pathlib.Path]:
    return sorted(SRC.glob("*.py"))


class TestNoDeprecatedStdlibApis:
    def test_the_source_is_clean(self) -> None:
        offenders = [
            f"{path.name}:{n}"
            for path in _sources()
            for n, line in enumerate(path.read_text().splitlines(), 1)
            if BANNED.search(line) and not line.lstrip().startswith("#")
        ]
        assert offenders == [], offenders

    def test_there_are_sources_to_scan(self) -> None:
        # A silent glob miss would make the scan vacuously clean.
        assert len(_sources()) >= 8

    def test_the_scanner_would_notice_a_real_offender(self) -> None:
        """A guard that cannot fail is not a guard."""
        for planted in (
            "from distutils.util import strtobool",
            "import imp",
            "datetime.datetime.utcnow()",
            "locale.getdefaultlocale()",
            "importlib.find_loader('x')",
            "import pkg_resources",
            "typing.ByteString",
        ):
            assert BANNED.search(planted), planted


class TestTheCeilingNeedsNoTestBecauseCiRunsIt:
    """Recorded so a later round does not add a redundant removed-module scan."""

    def test_ci_runs_a_version_that_removed_them(self) -> None:
        ci = (SRC.parent.parent / ".github" / "workflows" / "ci.yml").read_text()
        found = re.search(r"python-version:\s*\[([^\]]+)\]", ci)
        assert found, ci[:200]
        versions = [v.strip().strip('"').strip("'") for v in found.group(1).split(",")]
        assert "3.13" in versions, versions

    def test_no_pep594_module_is_imported_either(self) -> None:
        # Belt and braces: true today, and an ImportError on 3.13 if it stopped
        # being true, so this asserts the fact without pretending to be the guard.
        removed = re.compile(
            r"^\s*(?:import|from)\s+(aifc|audioop|cgi|cgitb|chunk|crypt|imghdr|mailcap"
            r"|msilib|nis|nntplib|ossaudiodev|pipes|sndhdr|spwd|sunau|telnetlib|uu|xdrlib)\b"
        )
        offenders = [
            f"{path.name}:{n}"
            for path in _sources()
            for n, line in enumerate(path.read_text().splitlines(), 1)
            if removed.search(line)
        ]
        assert offenders == [], offenders


class TestControls:
    """None of these reads the source tree, so each holds whatever it contains."""

    def test_the_scanner_ignores_a_comment(self) -> None:
        assert BANNED.search("distutils") and BANNED.search("# distutils")

    def test_a_word_merely_containing_a_banned_name_is_not_matched(self) -> None:
        # Word boundaries, so a local named `find_loaders` or `utcnowish` is safe.
        assert not BANNED.search("find_loaders(x)")
        assert not BANNED.search("my_utcnowish_helper")

    def test_the_source_tree_is_where_this_expects(self) -> None:
        assert (SRC / "cli.py").is_file() and (SRC / "fmt.py").is_file()

    def test_the_floor_guard_still_exists_and_is_the_other_half(self) -> None:
        # This file's docstring claims the floor is covered elsewhere; assert that
        # so the claim cannot go stale.
        floor = SRC.parent.parent / "tests" / "test_py36_compat.py"
        text = floor.read_text()
        assert "def test_no_stdlib_module_newer_than_the_floor" in text
        assert "feature_version" in text
