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
import re
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "rapidu"
MIN_FEATURE_VERSION = (3, 6)


def _sources():
    return sorted(SRC.glob("*.py"))


def test_there_are_sources_to_check():
    """A silent glob miss would make every test below vacuously pass."""
    assert len(_sources()) >= 8


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_parses_under_python36(path):
    """Every shipped module must parse with 3.6 syntax rules.

    ``feature_version`` is a **3.8+** parameter, so on 3.6 itself this check used
    to raise ``TypeError`` for every module -- the guard for the floor interpreter
    could not run on the floor interpreter, which is the same shape as RD-10 (a
    test asserting something about an environment it cannot execute in).

    Where it is unavailable the interpreter's own parser is the better check
    anyway: if the file parses here, it parses on this Python, and on 3.6 that is
    exactly the question. So the proxy is used on newer interpreters and the real
    thing on old ones.
    """
    source = path.read_text()
    kwargs = {}
    if sys.version_info >= (3, 8):
        kwargs["feature_version"] = MIN_FEATURE_VERSION
    try:
        ast.parse(source, filename=str(path), **kwargs)
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
    import rapidu

    assert isinstance(rapidu.__version__, str)
    assert rapidu.__version__


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
        ["/usr/bin/python3", "-c", "import rapidu; print(rapidu.__version__)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    assert proc.returncode == 0, "import failed under /usr/bin/python3:\n{}".format(
        proc.stdout.decode("utf-8", "replace")
    )


# --------------------------------------------------------------------------
# The other half of the deployability claim: stdlib only
# --------------------------------------------------------------------------
#: Modules that are part of the package, so importing them is not a dependency.
#:
#: Read from the tree rather than listed, because a hardcoded list is how this
#: kind of guard comes to pass vacuously after a module is added.
def _local_module_names():
    return {p.stem for p in SRC.rglob("*.py")} | {"rapidu"}


#: Stdlib modules that did NOT exist at this package's floor, and when they landed.
#:
#: `sys.stdlib_module_names` is the RUNNING interpreter's, so it answers "is this
#: stdlib in 3.11" and cannot answer "was it stdlib in 3.6". Importing `tomllib`
#: would therefore pass the third-party guard below while raising
#: `ModuleNotFoundError` on the login-node interpreter the whole deployability
#: claim is about. Deliberately small: the stdlib additions between 3.6 and the
#: newest version this is run on. A module missing from the table is not a false
#: pass -- `test_imports_under_the_system_interpreter` still runs the real 3.6-era
#: interpreter where one exists -- it is a slower way to find out.
TOO_NEW_FOR_THE_FLOOR = {
    "dataclasses": (3, 7),
    "contextvars": (3, 7),
    "importlib.resources": (3, 7),
    "zoneinfo": (3, 9),
    "graphlib": (3, 9),
    "tomllib": (3, 11),
}


#: ``sys.stdlib_module_names`` arrived in **3.10**. Without it every import looks
#: third-party, so the audit cannot run and must SKIP rather than invent findings.
#: ``ci.yml`` guards the same API deliberately -- ``getattr(sys,
#: "stdlib_module_names", ())`` then ``assert stdlib, "need Python 3.10+"`` -- and
#: this file called it bare, which failed the ``Test (py3.9)`` job while every
#: other job passed. The package supports 3.6 and the matrix tests 3.9, so the
#: interpreter running the suite is NOT guaranteed to have it.
_NEEDS_STDLIB_NAMES = pytest.mark.skipif(
    not hasattr(sys, "stdlib_module_names"),
    reason="sys.stdlib_module_names is 3.10+; the import audit cannot run without it",
)


def _third_party_imports(path):
    """Top-level modules ``path`` imports that are neither stdlib nor local."""
    local = _local_module_names()
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    # Same assert ci.yml makes: an empty set here would make every stdlib
    # import look like a dependency, so callers must carry
    # `_NEEDS_STDLIB_NAMES` rather than reach a wrong answer.
    assert stdlib, "sys.stdlib_module_names is 3.10+; caller must skip"
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        roots = []
        if isinstance(node, ast.Import):
            roots = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots = [node.module.split(".")[0]]
        found |= {r for r in roots if r not in stdlib and r not in local}
    return found


@_NEEDS_STDLIB_NAMES
@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_third_party_import(path):
    """`pip install rapidu` must pull in nothing, and neither must running it.

    The syntax floor above is only half the deployability argument. The other
    half is that the tool runs on a login node's bare `/usr/bin/python3` during a
    storage emergency, before any conda env is activated -- and one `import rich`
    would end that, on an interpreter where the user cannot install anything and
    would not want to.

    Asserted here rather than left to the `zero-install` CI job, because a CI-only
    check cannot fail during the local gate run that introduces it: the four gates
    would all pass and the breakage would surface on push.
    """
    found = sorted(_third_party_imports(path))
    assert not found, (
        f"{path.name} imports {found}; rapidu declares no dependencies and its "
        f"deployability rests on that"
    )


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_stdlib_module_newer_than_the_floor(path):
    """A module that is stdlib *now* may not have been at 3.6.

    See `TOO_NEW_FOR_THE_FLOOR`: the third-party guard above cannot catch these,
    because the running interpreter reports them as stdlib and they are.
    """
    imported = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
    offenders = {
        name: version
        for name, version in TOO_NEW_FOR_THE_FLOOR.items()
        if name in imported or any(i.split(".")[0] == name for i in imported)
    }
    assert not offenders, (
        f"{path.name} imports {offenders}, which post-date the "
        f"{MIN_FEATURE_VERSION} floor this package claims"
    )


def test_the_declared_dependency_list_is_actually_empty():
    """The claim from the other direction, so the two cannot disagree.

    A dependency added to `pyproject.toml` but not yet imported would pass every
    test above while already breaking `pip install` on an air-gapped node.
    """
    root = SRC.parent.parent
    text = (root / "pyproject.toml").read_text()
    block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
    declared = (
        []
        if block is None
        else [d for d in (x.strip().strip('",') for x in block.group(1).split("\n")) if d]
    )
    assert not declared, f"pyproject declares dependencies: {declared}"


@_NEEDS_STDLIB_NAMES
def test_the_guard_would_notice_a_real_import():
    """The control: a guard that cannot fail is not a guard.

    `test_there_are_sources_to_check` covers the glob going empty; this covers
    the detector itself returning nothing for input it should flag.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        probe = pathlib.Path(tmp) / "probe.py"
        probe.write_text("import rich\nfrom textual.app import App\n")
        assert _third_party_imports(probe) == {"rich", "textual"}


def test_the_audit_refuses_to_run_without_the_stdlib_list(tmp_path, monkeypatch):
    """Without ``sys.stdlib_module_names`` the audit must stop, not answer wrongly.

    The set is what tells a stdlib import from a dependency. Empty, every ``import
    os`` reads as a third-party package and ``test_no_third_party_import`` fails
    every source file for the wrong reason -- which is worse than not running,
    because the message names dependencies that do not exist.

    This is the API the ``Test (py3.9)`` job tripped over: 3.10+ only, called bare
    here while ``ci.yml`` guarded the same call with ``getattr`` and an assert.
    """
    monkeypatch.delattr(sys, "stdlib_module_names", raising=False)
    probe = tmp_path / "probe.py"
    probe.write_text("import os\nimport sys\n")
    with pytest.raises(AssertionError) as caught:
        _third_party_imports(probe)
    assert "3.10" in str(caught.value), caught.value


def test_control_the_audit_still_works_where_the_list_exists(tmp_path):
    """CONTROL, passing with the guard present or absent.

    On any interpreter that has the set, a stdlib import is still not a finding
    and a real dependency still is. A guard that skipped unconditionally would
    pass the test above and fail this one.
    """
    if not hasattr(sys, "stdlib_module_names"):
        pytest.skip("this interpreter predates the API; nothing to control against")
    probe = tmp_path / "probe.py"
    probe.write_text("import os\nimport sys\nimport rich\nfrom textual import App\n")
    assert _third_party_imports(probe) == {"rich", "textual"}
