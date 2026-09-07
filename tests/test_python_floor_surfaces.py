"""The declared floor, the badge, and the guard's own constant say one thing.

`requires-python = ">=3.6"` is load-bearing here rather than decorative:
`test_py36_compat.py` opens by explaining that the tool's whole deployability
argument is running on the bare `/usr/bin/python3` of a login node during a storage
emergency, which on RHEL8 is 3.6.8. That claim is backed by real static checks — ast
parse at `feature_version=(3, 6)`, no `__future__ annotations`, no PEP 604 unions, no
third-party or too-new stdlib imports — because **CI's lowest job is 3.9**, so nothing
executes on the floor itself.

Three places name that floor. Two of them were unguarded:

* `pyproject.toml`'s `requires-python` — already pinned, together with ruff's
  `target-version = "py37"`, by
  `test_audit_round_three.py::test_the_ruff_target_version_does_not_outrank_requires_python`.
  (py37 is one minor above the floor on purpose: ruff has no py36 target.)
* the README's `python-3.6+` badge — pinned nowhere. A shields.io URL is the copy
  nobody greps, so it is the one that keeps saying 3.6 after a floor change.
* `test_py36_compat.MIN_FEATURE_VERSION` — pinned nowhere, and it is what the compat
  guard actually checks against. If the floor rose and this constant did not, the
  guard would keep certifying the wrong version while every other surface moved.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"
COMPAT = ROOT / "tests" / "test_py36_compat.py"


def badge_floor() -> str:
    """The `python-3.6+` badge's version. `%2B` is the URL-encoded `+`."""
    found = re.search(r"badge/python-(\d+\.\d+)%2B", README.read_text())
    assert found, "the python badge should still be in the README"
    return found.group(1)


def declared_floor() -> str:
    found = re.search(r'requires-python\s*=\s*">=(\d+\.\d+)"', PYPROJECT.read_text())
    assert found, "requires-python should still be declared"
    return found.group(1)


def guard_floor() -> str:
    """What `test_py36_compat` actually parses against."""
    found = re.search(r"MIN_FEATURE_VERSION\s*=\s*\((\d+),\s*(\d+)\)", COMPAT.read_text())
    assert found, "MIN_FEATURE_VERSION should still be declared"
    return f"{found.group(1)}.{found.group(2)}"


class TestTheFloorIsOneNumberEverywhere:
    def test_the_badge_matches_the_declared_floor(self) -> None:
        assert badge_floor() == declared_floor(), (
            f"README badge says {badge_floor()}+, pyproject requires >={declared_floor()} — "
            "the badge is the surface nobody greps"
        )

    def test_the_compat_guard_checks_the_declared_floor(self) -> None:
        assert guard_floor() == declared_floor(), (
            f"MIN_FEATURE_VERSION is {guard_floor()} but the package claims "
            f">={declared_floor()}; the guard would certify the wrong version"
        )

    def test_ci_cannot_reach_the_floor_so_the_static_guard_is_the_backing(self) -> None:
        """Not a defect — recorded so nobody 'fixes' it by lowering the matrix.

        There is no 3.6 runner to be had, which is exactly why the compat checks are
        static. This asserts the guard exists rather than that CI covers the floor.
        """
        text = COMPAT.read_text()
        assert "feature_version" in text, "the static floor check is what backs the claim"
        assert "def test_parses_under_python36" in text


class TestControls:
    """None of these compares two surfaces, so each holds whatever the floor says."""

    def test_every_surface_was_actually_found(self) -> None:
        for value in (badge_floor(), declared_floor(), guard_floor()):
            assert re.fullmatch(r"\d+\.\d+", value), value

    def test_the_other_readme_badges_are_untouched(self) -> None:
        text = README.read_text()
        assert "dependencies-none" in text and "license-MIT" in text

    def test_the_declared_floor_is_still_pinned_by_its_own_test(self) -> None:
        # The surface this file deliberately does NOT re-pin; asserted so the
        # docstring's claim about where it lives cannot go stale.
        text = (ROOT / "tests" / "test_audit_round_three.py").read_text()
        assert "test_the_ruff_target_version_does_not_outrank_requires_python" in text
        assert 'requires-python = ">=3.6"' in text
