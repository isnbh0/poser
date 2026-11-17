"""Every kind of directory link a studio can hold, so a test of link handling covers each.

On Windows a directory link is a symbolic link, which poser never makes, or a directory
junction, which the file-system facility makes for `latest` and a user can make with
`mklink /J`. Elsewhere only symbolic links exist, and the junction case is skipped.
"""

import sys
from pathlib import Path

import pytest

LINK_KINDS = [
    pytest.param("symlink"),
    pytest.param(
        "junction",
        marks=pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows'"),
    ),
]


def make_link(kind: str, link: Path, target: Path) -> None:
    """Make a directory link of `kind` at `link`, leading to the absolute `target`, which must
    exist for a junction."""
    if kind == "symlink":
        link.symlink_to(target, target_is_directory=True)
        return
    assert sys.platform == "win32"
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def make_dangling_link(kind: str, link: Path) -> None:
    """Make a directory link of `kind` at `link` whose target does not exist."""
    target = link.parent / f"{link.name}-target"
    target.mkdir()
    make_link(kind, link, target)
    target.rmdir()
