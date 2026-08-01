"""Guard the oldest interpreter this package claims to support.

``requires-python = ">=3.6"`` is not a courtesy: the tool's whole deployability
argument is that it runs on the bare ``/usr/bin/python3`` of a login node during
a storage emergency, before any conda env is activated. On RHEL8 that is 3.6.8.

That claim is easy to break by accident, and it has already been broken once --
setuptools-scm's stock ``version_file`` template opens with
``from __future__ import annotations``, which is a SyntaxError on 3.6 and took
down every import of the package. These tests fail when it happens again.
"""

import ast
import os
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "slurmdisk"
MIN_FEATURE_VERSION = (3, 6)


def _sources():
    return sorted(SRC.glob("*.py"))


def test_there_are_sources_to_check():
    """A silent glob miss would make every test below vacuously pass."""
    assert len(_sources()) >= 8


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_parses_under_python36(path):
    """Every shipped module must parse with 3.6 syntax rules."""
    source = path.read_text()
    try:
        ast.parse(source, filename=str(path), feature_version=MIN_FEATURE_VERSION)
    except SyntaxError as exc:
        pytest.fail(
            "{} is not valid Python {}.{} syntax: {} (line {})".format(
                path.name, MIN_FEATURE_VERSION[0], MIN_FEATURE_VERSION[1], exc.msg, exc.lineno
            )
        )


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_future_annotations_import(path):
    """`from __future__ import annotations` does not exist before 3.7.

    ``ast.parse(feature_version=...)`` does not reject it, so it needs its own
    check -- and it is the exact regression that broke this package once.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            names = [a.name for a in node.names]
            assert "annotations" not in names, (
                "{} imports `annotations` from __future__, which is a SyntaxError "
                "on Python 3.6".format(path.name)
            )


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_pep604_unions_in_annotations(path):
    """`int | str` in an annotation is a TypeError at runtime before 3.10.

    Annotations are evaluated eagerly without the ``annotations`` future import,
    so a PEP 604 union in a module- or class-level annotation raises on import.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        annotation = getattr(node, "annotation", None) or getattr(node, "returns", None)
        if annotation is None:
            continue
        for sub in ast.walk(annotation):
            if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
                pytest.fail(
                    "{} uses a PEP 604 union in an annotation, which raises on "
                    "Python < 3.10".format(path.name)
                )


def test_version_is_a_usable_string():
    """Whatever the build backend wrote, the package must expose a version."""
    import slurmdisk

    assert isinstance(slurmdisk.__version__, str)
    assert slurmdisk.__version__


@pytest.mark.skipif(
    not os.path.exists("/usr/bin/python3"), reason="no system python3 to test against"
)
def test_imports_under_the_system_interpreter():
    """End-to-end: the package actually imports under /usr/bin/python3.

    This is the real claim -- the AST checks above are a fast proxy for it. It is
    skipped where there is no system interpreter, and it passes trivially where
    the system interpreter is modern.
    """
    import subprocess

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC.parent)
    proc = subprocess.run(
        ["/usr/bin/python3", "-c", "import slurmdisk; print(slurmdisk.__version__)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    assert proc.returncode == 0, "import failed under /usr/bin/python3:\n{}".format(
        proc.stdout.decode("utf-8", "replace")
    )
