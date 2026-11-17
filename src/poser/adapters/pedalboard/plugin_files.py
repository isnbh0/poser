"""Where a plugin's loadable binary and its Windows version live.

This module imports only the standard library, so the parent and the Pedalboard worker both use
it. Reading a version reads bytes and runs none of the plugin's code. The layouts follow
Steinberg's VST3 bundle format and Microsoft's PE format, VS_VERSIONINFO, and VS_FIXEDFILEINFO
definitions.
"""

import struct
from pathlib import Path
from typing import BinaryIO

WINDOWS_BINARY_DIR = ("Contents", "x86_64-win")
PE_HEADER_OFFSET = 0x3C
PE_SIGNATURE = b"PE\0\0"
COFF_HEADER_SIZE = 20
SECTION_HEADER_SIZE = 40
RESOURCE_DIRECTORY_INDEX = 2
# The offsets of NumberOfRvaAndSizes and of the data directories in each optional header form.
OPTIONAL_HEADER_LAYOUTS = {0x10B: (92, 96), 0x20B: (108, 112)}  # PE32, PE32+
RESOURCE_TABLE_SIZE = 16
RESOURCE_ENTRY_SIZE = 8
SUBDIRECTORY = 0x80000000
RT_VERSION = 16
VERSION_KEY = "VS_VERSION_INFO\0".encode("utf-16-le")
FIXED_INFO_OFFSET = 40  # The header and key, padded to 32 bits.
FIXED_INFO_SIZE = 52
FIXED_INFO_SIGNATURE = 0xFEEF04BD
MAX_VERSION_BYTES = 64 * 1024


class _NoVersionError(Exception):
    """The file holds no version this module can read."""


def windows_binary(path: Path) -> Path:
    """Return the Windows x64 binary a VST3 path names: `<bundle>/Contents/x86_64-win/<bundle
    name>` for a bundle directory, and the path itself otherwise."""
    if path.is_dir():
        return path.joinpath(*WINDOWS_BINARY_DIR, path.name)
    return path


def pe_version(binary: Path) -> str | None:
    """Return a PE file's version, from the fixed file version of its version resource, or None.

    Reads bytes only, so no code in the file runs. The fixed file version wins, and the fixed
    product version stands in when the file version is 0.0.0.0. The format is `a.b.c.d`, so a
    build-only update changes it. A missing, unreadable, truncated, or malformed file gives None,
    so a version never stops a sound from resolving.
    """
    try:
        with binary.open("rb") as file:
            return _read_version(file)
    except (OSError, struct.error, _NoVersionError):
        return None


def _read_version(file: BinaryIO) -> str | None:
    dos_header = _read(file, 0, PE_HEADER_OFFSET + 4)
    if dos_header[:2] != b"MZ":
        raise _NoVersionError
    (pe_offset,) = struct.unpack_from("<I", dos_header, PE_HEADER_OFFSET)
    headers = _read(file, pe_offset, len(PE_SIGNATURE) + COFF_HEADER_SIZE)
    if headers[:4] != PE_SIGNATURE:
        raise _NoVersionError
    (sections,) = struct.unpack_from("<H", headers, 4 + 2)
    (optional_size,) = struct.unpack_from("<H", headers, 4 + 16)
    optional_offset = pe_offset + len(headers)
    optional = _read(file, optional_offset, optional_size)
    (magic,) = struct.unpack_from("<H", optional, 0)
    if magic not in OPTIONAL_HEADER_LAYOUTS:
        raise _NoVersionError
    count_offset, directories_offset = OPTIONAL_HEADER_LAYOUTS[magic]
    (directories,) = struct.unpack_from("<I", optional, count_offset)
    if directories <= RESOURCE_DIRECTORY_INDEX:
        raise _NoVersionError
    resource_rva, _ = struct.unpack_from(
        "<II", optional, directories_offset + 8 * RESOURCE_DIRECTORY_INDEX
    )
    if resource_rva == 0:
        raise _NoVersionError
    table = _read(file, optional_offset + optional_size, sections * SECTION_HEADER_SIZE)
    section_spans = [
        struct.unpack_from("<IIII", table, index * SECTION_HEADER_SIZE + 8)
        for index in range(sections)
    ]

    def file_offset(rva: int) -> int:
        for virtual_size, virtual_address, raw_size, raw_offset in section_spans:
            size = min(virtual_size, raw_size) if virtual_size else raw_size
            if virtual_address <= rva < virtual_address + size:
                return raw_offset + rva - virtual_address
        raise _NoVersionError

    resources = file_offset(resource_rva)
    types = _subdirectory(_entries(file, resources, 0), RT_VERSION)
    names = _subdirectory(_entries(file, resources, types), None)
    languages = _entries(file, resources, names)
    if not languages or languages[0][1] & SUBDIRECTORY:
        raise _NoVersionError
    data_rva, data_size = struct.unpack_from("<II", _read(file, resources + languages[0][1], 8), 0)
    info = _read(file, file_offset(data_rva), min(data_size, MAX_VERSION_BYTES))
    (value_size,) = struct.unpack_from("<H", info, 2)
    key = info[6 : 6 + len(VERSION_KEY)]
    if key != VERSION_KEY or value_size < FIXED_INFO_SIZE:
        raise _NoVersionError
    signature, _, file_ms, file_ls, product_ms, product_ls = struct.unpack_from(
        "<6I", info, FIXED_INFO_OFFSET
    )
    if signature != FIXED_INFO_SIGNATURE:
        raise _NoVersionError
    for most, least in ((file_ms, file_ls), (product_ms, product_ls)):
        if most or least:
            return f"{most >> 16}.{most & 0xFFFF}.{least >> 16}.{least & 0xFFFF}"
    return None


def _read(file: BinaryIO, offset: int, size: int) -> bytes:
    """Return exactly `size` bytes at `offset`, raising _NoVersionError when the file is
    shorter."""
    file.seek(offset)
    data = file.read(size)
    if len(data) != size:
        raise _NoVersionError
    return data


def _entries(file: BinaryIO, resources: int, table: int) -> list[tuple[int, int]]:
    """Return the (name or ID, offset) entries of the resource directory table at `table`, an
    offset from the start of the resource section."""
    header = _read(file, resources + table, RESOURCE_TABLE_SIZE)
    named, numbered = struct.unpack_from("<HH", header, 12)
    count = named + numbered
    data = _read(file, resources + table + RESOURCE_TABLE_SIZE, count * RESOURCE_ENTRY_SIZE)
    return [struct.unpack_from("<II", data, index * RESOURCE_ENTRY_SIZE) for index in range(count)]


def _subdirectory(entries: list[tuple[int, int]], wanted: int | None) -> int:
    """Return the subdirectory offset of the entry with ID `wanted`, or of the first entry when
    `wanted` is None. A name entry's first field has its high bit set, so it never matches an
    ID."""
    for name, offset in entries:
        if (wanted is None or name == wanted) and offset & SUBDIRECTORY:
            return offset & ~SUBDIRECTORY
        if wanted is None:
            break
    raise _NoVersionError
