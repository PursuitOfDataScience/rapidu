"""Test-wide environment pinning, and the two filesystem capabilities the suite needs.

**Why this file exists.** Every module here builds its style with
``ui.resolve_style(...)``, often at import time (``PLAIN = ui.resolve_style(...)``),
and that reads the width from the environment: ``shutil.get_terminal_size``
consults ``COLUMNS`` first, then the tty, then a default. So roughly sixty call
sites across nine modules inherited the terminal of whoever ran the suite, and
every assertion about rendered text silently depended on it. Ten tests across
three modules failed under ``COLUMNS=40`` -- not because anything was broken, but
because they were written at a different width. A developer's wide terminal, a CI
runner with no tty, and a split tmux pane are three different suites.

``ui.terminal_width`` floors at 60 and caps at 160 on purpose ("narrow enough to
paste, wide enough for the table"), so ``COLUMNS=40`` renders at 60 and the
column layout genuinely changes. That is the tool behaving as designed; it is
only the tests that must not be at the mercy of it.

100 is pinned because it is what both this host and a CI runner resolve to
already -- ``get_terminal_size`` falls back to ``terminal_width``'s own default
when there is no ``COLUMNS`` and no tty -- so nothing about the existing
assertions moves. A test that cares about a specific width still sets
``style.width`` on its own object, which overrides this.

**The same argument, one layer down: ``TMPDIR``.** Nine tests assert POSIX
behaviour that not every filesystem provides, and they ran wherever ``tmp_path``
happened to land. Pointed at an NFS home -- which is what ``/tmp`` is on a
diskless compute node, a normal HPC configuration -- eleven tests failed and none
of them was about this package. The two capabilities are probed once here and
named, so a filesystem that lacks one produces a skip that says which and why,
rather than a page of red that reads like a broken tool.

Probed, not assumed from the filesystem type: ``statvfs`` gives a name, and the
name is not the behaviour. Both probes default to *capable* if they cannot
complete, so an unexpected environment still runs the tests and fails loudly
rather than skipping in silence.
"""

import os
import shutil
import tempfile

os.environ["COLUMNS"] = "100"


def _probe_unlink_hides_entry():
    """Does unlinking an open file remove its directory entry here?

    On every local filesystem, yes: the name goes at once and the blocks live on
    behind the descriptor. That invisible space is the whole subject of
    ``rapidu.deleted``.

    On NFS it does not. The client performs a *silly rename* -- the entry becomes
    ``.nfsXXXXXXXXXXXXXXXX`` and survives until the last descriptor closes -- so
    there is nothing unlinked to find, ``/proc/<pid>/fd/N`` resolves to a real
    path with no ``(deleted)`` suffix, and the space is plainly visible to ``du``.
    Measured on an NFSv3 home: after ``unlink`` the directory held
    ``.nfs00000002945e149d00002b83`` and the scan correctly returned nothing.

    So the scan is right on both, and six tests that assert it *finds* something
    can only pass where the filesystem hides the entry.
    """
    root = None
    try:
        root = tempfile.mkdtemp(prefix="rdu-cap-unlink-")
        path = os.path.join(root, "probe.bin")
        handle = open(path, "wb")
        try:
            handle.write(b"x" * 4096)
            handle.flush()
            os.unlink(path)
            return not os.listdir(root)
        finally:
            handle.close()
    except OSError:
        return True
    finally:
        if root:
            shutil.rmtree(root, ignore_errors=True)


def _probe_chmod_can_deny():
    """Can ``chmod 0`` stop the *owner* reading their own directory here?

    Three tests build a deliberately unreadable subtree, because "the total is a
    floor, not a total" is a claim this tool has to get right. They need the mode
    to be enforced against the owner.

    An ACL-backed filesystem need not do that. On an NFS export of OneFS the mode
    is synthesised from an ACL that keeps the owner's access: ``chmod(d, 0o000)``
    reads back as ``0o700`` and ``scandir`` succeeds. Nothing is broken -- the
    file server is doing what it documents -- but the fixture cannot be built, so
    the tests have no subject.
    """
    root = None
    try:
        root = tempfile.mkdtemp(prefix="rdu-cap-chmod-")
        locked = os.path.join(root, "locked")
        os.mkdir(locked)
        with open(os.path.join(locked, "f"), "wb") as handle:
            handle.write(b"x")
        os.chmod(locked, 0o000)
        try:
            os.listdir(locked)
            return False
        except OSError:
            return True
        finally:
            os.chmod(locked, 0o755)
    except OSError:
        return True
    finally:
        if root:
            shutil.rmtree(root, ignore_errors=True)


# Root bypasses mode bits entirely, so a `chmod 0` fixture has no subject there
# either -- the affected tests already skip on uid 0 individually, and folding it
# in here keeps one answer to "can this fixture be built".
UNLINK_HIDES_ENTRY = _probe_unlink_hides_entry()
CHMOD_CAN_DENY = _probe_chmod_can_deny() and os.getuid() != 0

NEEDS_REAL_UNLINK = (
    "this filesystem silly-renames instead of unlinking (NFS), so there is no "
    "unlinked-but-open file for the scan to find"
)
NEEDS_ENFORCED_MODE = (
    "chmod cannot deny the owner on this filesystem (ACL-backed export), so an "
    "unreadable directory cannot be created"
)
