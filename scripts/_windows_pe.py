"""Portable PE architecture and dependency inspection for Windows build tools."""

from __future__ import annotations

import struct
from pathlib import Path


def _read_c_string(data: bytes, offset: int) -> str | None:
    if offset < 0 or offset >= len(data):
        return None
    terminator = data.find(b"\0", offset)
    if terminator < 0:
        return None
    try:
        return data[offset:terminator].decode("ascii")
    except UnicodeDecodeError:
        return None


def pe_machine(path: Path) -> int:
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if len(data) < 0x86 or data[:2] != b"MZ":
        return 0
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 6 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return 0
    return struct.unpack_from("<H", data, pe_offset + 4)[0]


def pe_imports(path: Path) -> set[str]:
    """Return imported DLL names without depending on a Visual Studio installation."""
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        return set()
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return set()
    coff_offset = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff_offset + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff_offset + 16)[0]
    optional_offset = coff_offset + 20
    if optional_offset + optional_size > len(data):
        return set()
    magic = struct.unpack_from("<H", data, optional_offset)[0]
    if magic == 0x20B:
        image_base = struct.unpack_from("<Q", data, optional_offset + 24)[0]
        directory_count_offset = optional_offset + 108
        directory_offset = optional_offset + 112
    elif magic == 0x10B:
        image_base = struct.unpack_from("<I", data, optional_offset + 28)[0]
        directory_count_offset = optional_offset + 92
        directory_offset = optional_offset + 96
    else:
        return set()
    if directory_count_offset + 4 > len(data):
        return set()
    directory_count = struct.unpack_from("<I", data, directory_count_offset)[0]
    section_offset = optional_offset + optional_size
    sections: list[tuple[int, int, int, int]] = []
    for index in range(section_count):
        offset = section_offset + index * 40
        if offset + 40 > len(data):
            return set()
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, offset + 8
        )
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset, raw_size))

    def rva_to_offset(rva: int) -> int | None:
        for virtual_address, span, raw_offset, raw_size in sections:
            if virtual_address <= rva < virtual_address + span:
                relative = rva - virtual_address
                if relative >= raw_size:
                    return None
                result = raw_offset + relative
                return result if result < len(data) else None
        return rva if rva < len(data) else None

    def data_directory(index: int) -> tuple[int, int]:
        if directory_count <= index or directory_offset + (index + 1) * 8 > len(data):
            return (0, 0)
        return struct.unpack_from("<II", data, directory_offset + index * 8)

    imports: set[str] = set()
    import_rva, import_size = data_directory(1)
    import_offset = rva_to_offset(import_rva) if import_rva else None
    if import_offset is not None:
        end = min(len(data), import_offset + import_size) if import_size else len(data)
        cursor = import_offset
        while cursor + 20 <= end:
            descriptor = struct.unpack_from("<IIIII", data, cursor)
            if descriptor == (0, 0, 0, 0, 0):
                break
            name_offset = rva_to_offset(descriptor[3])
            name = _read_c_string(data, name_offset) if name_offset is not None else None
            if name:
                imports.add(name)
            cursor += 20

    delay_rva, delay_size = data_directory(13)
    delay_offset = rva_to_offset(delay_rva) if delay_rva else None
    if delay_offset is not None:
        end = min(len(data), delay_offset + delay_size) if delay_size else len(data)
        cursor = delay_offset
        while cursor + 32 <= end:
            descriptor = struct.unpack_from("<IIIIIIII", data, cursor)
            if descriptor == (0, 0, 0, 0, 0, 0, 0, 0):
                break
            attributes, name_address = descriptor[:2]
            name_rva = name_address if attributes & 1 else name_address - image_base
            name_offset = rva_to_offset(name_rva) if name_rva >= 0 else None
            name = _read_c_string(data, name_offset) if name_offset is not None else None
            if name:
                imports.add(name)
            cursor += 32
    return imports
