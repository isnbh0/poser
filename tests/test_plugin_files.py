"""Where a plugin's Windows binary lives, and its version, read from the binary's bytes."""

import os
import struct
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from helpers import write_pe

from poser.adapters.pedalboard.plugin_files import pe_version, windows_binary


@pytest.mark.parametrize("pe32", [False, True], ids=["pe32+", "pe32"])
def test_pe_version_reads_the_fixed_file_version(tmp_path: Path, pe32: bool) -> None:
    binary = write_pe(tmp_path / "Plugin.vst3", file_version=(1, 3, 4, 0), pe32=pe32)
    assert pe_version(binary) == "1.3.4.0"


def test_pe_version_keeps_every_field_of_the_version(tmp_path: Path) -> None:
    binary = write_pe(tmp_path / "Plugin.vst3", file_version=(65535, 2, 300, 4096))
    assert pe_version(binary) == "65535.2.300.4096"


def test_pe_version_falls_back_to_the_product_version(tmp_path: Path) -> None:
    binary = write_pe(
        tmp_path / "Plugin.vst3", file_version=(0, 0, 0, 0), product_version=(2, 1, 0, 7)
    )
    assert pe_version(binary) == "2.1.0.7"


def test_pe_version_is_none_when_both_versions_are_zero(tmp_path: Path) -> None:
    binary = write_pe(
        tmp_path / "Plugin.vst3", file_version=(0, 0, 0, 0), product_version=(0, 0, 0, 0)
    )
    assert pe_version(binary) is None


def test_pe_version_is_none_for_a_file_that_is_no_pe_image(tmp_path: Path) -> None:
    assert pe_version(tmp_path / "Missing.vst3") is None
    (tmp_path / "Empty.vst3").write_bytes(b"")
    assert pe_version(tmp_path / "Empty.vst3") is None
    (tmp_path / "Text.vst3").write_bytes(b"not a portable executable" * 40)
    assert pe_version(tmp_path / "Text.vst3") is None
    (tmp_path / "Folder.vst3").mkdir()
    assert pe_version(tmp_path / "Folder.vst3") is None
    assert pe_version(tmp_path / "Nul\0.vst3") is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no FIFOs in the file system")
def test_pe_version_never_opens_a_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "Plugin.vst3"
    os.mkfifo(fifo)
    versions: list[str | None] = []
    reader = threading.Thread(target=lambda: versions.append(pe_version(fifo)), daemon=True)
    reader.start()
    reader.join(timeout=5)
    blocked = reader.is_alive()
    if blocked:  # Opening the FIFO waits for a writer, so release the reader.
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        reader.join()
    assert not blocked
    assert versions == [None]


MALFORMED: dict[str, Callable[[Path], Path]] = {
    "no-resource-table": lambda path: write_pe(path, resource_directory=False),
    "no-version-resource": lambda path: write_pe(path, resource_type=3),  # RT_ICON
    "wrong-signature": lambda path: write_pe(path, signature=0xFEEF04BC),
}


@pytest.mark.parametrize("write_malformed", MALFORMED.values(), ids=MALFORMED.keys())
def test_pe_version_is_none_without_a_readable_version_resource(
    tmp_path: Path, write_malformed: Callable[[Path], Path]
) -> None:
    assert pe_version(write_pe(tmp_path / "Plugin.vst3")) == "1.3.4.0"
    assert pe_version(write_malformed(tmp_path / "Plugin.vst3")) is None


RESOURCES = 0x200  # Where write_pe puts the resource section.
# Hostile values at fields of write_pe's image, each as (offset, format, written value, value).
HOSTILE: dict[str, tuple[int, str, int, int]] = {
    "pe-header-past-the-end": (0x3C, "<I", 0x40, 0xFFFFFFFF),
    "most-sections": (0x46, "<H", 1, 0xFFFF),
    "longest-optional-header": (0x54, "<H", 240, 0xFFFF),
    "most-type-entries": (RESOURCES + 0x0E, "<H", 1, 0xFFFF),
    "type-named-like-rt-version": (RESOURCES + 0x10, "<I", 16, 0x80000010),
    "name-entry-loops-to-the-root": (RESOURCES + 0x2C, "<I", 0x80000030, 0x80000000),
    "language-entry-is-a-directory": (RESOURCES + 0x44, "<I", 0x48, 0x80000048),
    "largest-version-data": (RESOURCES + 0x4C, "<I", 92, 0xFFFFFFFF),
}


@pytest.mark.parametrize(("offset", "layout", "written", "hostile"), HOSTILE.values(), ids=HOSTILE)
def test_pe_version_is_none_for_a_hostile_header_value(
    tmp_path: Path, offset: int, layout: str, written: int, hostile: int
) -> None:
    binary = write_pe(tmp_path / "Plugin.vst3")
    data = bytearray(binary.read_bytes())
    assert struct.unpack_from(layout, data, offset) == (written,)
    struct.pack_into(layout, data, offset, hostile)
    binary.write_bytes(data)
    assert pe_version(binary) is None


def test_pe_version_is_none_for_a_truncated_image(tmp_path: Path) -> None:
    data = write_pe(tmp_path / "Plugin.vst3").read_bytes()
    cut = tmp_path / "Cut.vst3"
    for size in range(0, len(data), 16):
        cut.write_bytes(data[:size])
        assert pe_version(cut) is None, size


def test_windows_binary_names_the_binary_inside_a_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "Plugin.vst3"
    bundle.mkdir()
    assert windows_binary(bundle) == bundle / "Contents" / "x86_64-win" / "Plugin.vst3"


def test_windows_binary_keeps_a_file_or_a_missing_path(tmp_path: Path) -> None:
    binary = write_pe(tmp_path / "Plugin.vst3")
    assert windows_binary(binary) == binary
    assert windows_binary(tmp_path / "Missing.vst3") == tmp_path / "Missing.vst3"
