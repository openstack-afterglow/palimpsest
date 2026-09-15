#!/usr/bin/env python3
"""Seal a linked ELF to the exact sectionless runtime image Palimpsest ships."""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: seal_static_elf.py ELF")
    path = Path(sys.argv[1])
    payload = bytearray(path.read_bytes())
    header = struct.Struct("<16sHHIQQQIHHHHHH")
    if len(payload) < header.size:
        raise SystemExit("ELF is truncated")
    fields = list(header.unpack_from(payload))
    ident, elf_type, machine = fields[:3]
    phoff, phentsize, phnum = fields[5], fields[9], fields[10]
    if ident[:7] != b"\x7fELF\x02\x01\x01" or elf_type != 2 or machine != 62 or phentsize != 56 or not phnum:
        raise SystemExit("ELF identity is unsupported")
    extent = max(header.size, phoff + phentsize * phnum)
    executable_entry = False
    stack_policy_seen = False
    for index in range(phnum):
        offset = phoff + index * phentsize
        if offset + phentsize > len(payload):
            raise SystemExit("ELF program header is truncated")
        kind, flags, file_offset, virtual, _physical, file_size, memory_size, alignment = struct.unpack_from(
            "<IIQQQQQQ", payload, offset
        )
        if flags & ~7 or kind in {2, 3} or file_size > memory_size or file_offset + file_size > len(payload):
            raise SystemExit("ELF program segment policy is invalid")
        if alignment not in {0, 1} and (alignment & (alignment - 1) or file_offset % alignment != virtual % alignment):
            raise SystemExit("ELF program segment alignment is invalid")
        if kind == 1:
            if flags & 3 == 3:
                raise SystemExit("ELF load segment is writable and executable")
            if flags & 5 == 5 and virtual <= fields[4] < virtual + file_size:
                executable_entry = True
            extent = max(extent, file_offset + file_size)
        if kind == 0x6474E551:
            if stack_policy_seen or flags & 1 or file_size or memory_size:
                raise SystemExit("ELF stack policy is invalid")
            stack_policy_seen = True
    if not executable_entry or not stack_policy_seen:
        raise SystemExit("ELF entry or stack policy is invalid")
    if extent > len(payload):
        raise SystemExit("ELF load segment is truncated")
    fields[6] = 0
    fields[11] = 0
    fields[12] = 0
    fields[13] = 0
    header.pack_into(payload, 0, *fields)
    descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC)
    try:
        view = memoryview(payload)[:extent]
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SystemExit("ELF seal write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
