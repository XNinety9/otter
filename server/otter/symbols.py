"""Turns crash addresses into function names and source lines, from the firmware's ELF file
(#24), in pure Python (pyelftools): no ESP-IDF toolchain on the server."""

import bisect
from functools import lru_cache

from elftools.elf.constants import SH_FLAGS
from elftools.elf.elffile import ELFFile

from .storage import open_elf


class Symbolizer:
    def __init__(self, elf_sha256: str) -> None:
        with open_elf(elf_sha256) as f:
            elf = ELFFile(f)
            self.arch = elf["e_machine"]
            self.code = [
                (s["sh_addr"], s["sh_addr"] + s["sh_size"])
                for s in elf.iter_sections()
                if s["sh_flags"] & SH_FLAGS.SHF_EXECINSTR and s["sh_size"]
            ]
            funcs = sorted(
                (sym["st_value"], sym["st_value"] + max(sym["st_size"], 1), sym.name)
                for sym in elf.get_section_by_name(".symtab").iter_symbols()
                if sym["st_info"]["type"] == "STT_FUNC" and sym["st_value"]
            )
            self._starts = [f[0] for f in funcs]
            self._funcs = funcs
            self._lines = self._line_table(elf) if elf.has_dwarf_info() else ([], [])

    @staticmethod
    def _line_table(elf: ELFFile) -> tuple[list[int], list[tuple[str, int]]]:
        rows = []
        dwarf = elf.get_dwarf_info()
        for cu in dwarf.iter_CUs():
            program = dwarf.line_program_for_CU(cu)
            if program is None:
                continue
            files = program["file_entry"]
            version = program["version"]
            for entry in program.get_entries():
                state = entry.state
                if state is None or state.end_sequence:
                    continue
                index = state.file if version >= 5 else state.file - 1
                if 0 <= index < len(files):
                    rows.append((state.address, files[index].name.decode(errors="replace"), state.line))
        rows.sort()
        return [r[0] for r in rows], [(r[1], r[2]) for r in rows]

    def is_code(self, address: int) -> bool:
        return any(start <= address < end for start, end in self.code)

    def function(self, address: int) -> tuple[str, int] | None:
        i = bisect.bisect_right(self._starts, address) - 1
        if i >= 0 and address < self._funcs[i][1]:
            return self._funcs[i][2], address - self._funcs[i][0]
        return None

    def line(self, address: int) -> tuple[str, int] | None:
        addresses, rows = self._lines
        i = bisect.bisect_right(addresses, address) - 1
        return rows[i] if i >= 0 else None

    def frame(self, address: int, kind: str) -> dict:
        """kind: pc (where it crashed), return (a return address: the call is just before it),
        or stack (a code address found on the stack: probably a caller, not certainly)."""
        lookup = address - 1 if kind in ("return", "stack") else address
        func = self.function(lookup)
        line = self.line(lookup)
        return {
            "address": f"0x{address:08x}",
            "kind": kind,
            "function": func[0] if func else None,
            "offset": func[1] if func else None,
            "file": line[0] if line else None,
            "line": line[1] if line else None,
        }


@lru_cache(maxsize=4)
def symbolizer(elf_sha256: str) -> Symbolizer:
    return Symbolizer(elf_sha256)
