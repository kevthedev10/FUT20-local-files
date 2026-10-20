"""Read-only locator for FIFA 20's loaded Origin auth-code implementation."""
from __future__ import annotations

import argparse
import ctypes
import importlib.util
import struct
from pathlib import Path

from capstone import Cs, CS_ARCH_X86, CS_MODE_64


def load_memory_api():
    path = Path(__file__).with_name("PATCH_RUNNING_FIFA20_TRUST.py")
    spec = importlib.util.spec_from_file_location("fifa20_memory", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def iter_chunks(api, process, base: int, size: int, chunk_size: int = 4 * 1024 * 1024):
    offset = 0
    while offset < size:
        block = api.read_region(process, base + offset, min(chunk_size, size - offset))
        if block:
            yield base + offset, block
        offset += chunk_size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--base", type=lambda value: int(value, 0), required=True)
    parser.add_argument("--size", type=lambda value: int(value, 0), required=True)
    parser.add_argument("--disassemble", nargs="*", type=lambda value: int(value, 0))
    args = parser.parse_args()
    api = load_memory_api()
    process = api.kernel32.OpenProcess(api.PROCESS_QUERY_INFORMATION | api.PROCESS_VM_READ, False, args.pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if args.disassemble:
            decoder = Cs(CS_ARCH_X86, CS_MODE_64)
            for instruction in args.disassemble:
                start = instruction - 96
                block = api.read_region(process, start, 256) or b""
                print("DISASM", hex(instruction))
                for item in decoder.disasm(block, start):
                    marker = ">" if item.address == instruction else " "
                    print(f"{marker}{item.address:#x}: {item.mnemonic} {item.op_str}")
            return 0
        needles = (b"FOS-SERVER\0", "FOS-SERVER\0".encode("utf-16le"), b"OriginRequestAuthCode\0", b"GetAuthCode\0")
        matches: dict[bytes, list[int]] = {needle: [] for needle in needles}
        for address, block in iter_chunks(api, process, args.base, args.size):
            for needle in needles:
                start = 0
                while True:
                    index = block.find(needle, start)
                    if index < 0:
                        break
                    matches[needle].append(address + index)
                    start = index + 1
        for needle, addresses in matches.items():
            print(needle.rstrip(b"\0").decode("ascii", "replace"), *(hex(value) for value in addresses))

        # Locate common x64 RIP-relative LEA/MOV references to each string.
        targets = {address for addresses in matches.values() for address in addresses}
        references: list[tuple[int, int, bytes]] = []
        for address, block in iter_chunks(api, process, args.base, args.size):
            for index in range(max(0, len(block) - 7)):
                # REX + 8D/8B with mod=00 and r/m=101, followed by disp32.
                if block[index] not in range(0x40, 0x50) or block[index + 1] not in (0x8D, 0x8B):
                    continue
                if block[index + 2] & 0xC7 != 0x05:
                    continue
                instruction = address + index
                target = instruction + 7 + struct.unpack_from("<i", block, index + 3)[0]
                if target in targets:
                    references.append((instruction, target, block[index:index + 7]))
        for instruction, target, raw in references:
            print("XREF", hex(instruction), "->", hex(target), raw.hex())
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        for instruction, target, _ in references:
            start = instruction - 80
            block = api.read_region(process, start, 200) or b""
            print("DISASM", hex(instruction), "string", hex(target))
            for item in decoder.disasm(block, start):
                marker = ">" if item.address == instruction else " "
                print(f"{marker}{item.address:#x}: {item.mnemonic} {item.op_str}")
    finally:
        api.kernel32.CloseHandle(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
