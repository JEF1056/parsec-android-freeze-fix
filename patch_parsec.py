#!/usr/bin/env python3
"""Patch Parsec Android's libmain.so to fix the AMediaCodec deadlock.

Bug in libmatoya (MTY) inside libmain.so:
1. AMediaCodec_dequeueInputBuffer(codec, -1) uses an infinite timeout (-1).
2. AMediaCodec_dequeueOutputBuffer(codec, &info, 0) is only called once per
   input frame and returns immediately on INFO_OUTPUT_FORMAT_CHANGED (-2) or
   INFO_OUTPUT_BUFFERS_CHANGED (-3) without draining pending output frames.
   When output buffers back up in Codec2's AidlBufferPool (e.g. 6/6 used at 4K),
   the decoder stops accepting input and dequeueInputBuffer(-1) deadlocks forever.

Fix:
1. Replace dequeueInputBuffer(codec, -1) with a helper that uses a 5ms timeout
   and, on INFO_TRY_AGAIN_LATER (-1), drains/delivers pending output frames via
   decode_frame(ctx, NULL, 0, out_cb, out_status) before retrying (up to 20x).
2. Loop dequeueOutputBuffer when it returns -2 (INFO_OUTPUT_FORMAT_CHANGED) or
   -3 (INFO_OUTPUT_BUFFERS_CHANGED).
"""

import struct
import sys

# Offsets in Parsec 3.150.097.05 (315009705) arm64-v8a libmain.so
ADDR_DECODE_FRAME = 0x16B830
ADDR_DEQUEUE_IN_CALL = 0x16B888
ADDR_DEQUEUE_IN_PLT = 0x22F570
# Loop back to ldr x0, [x19] (0x16B8E8), NOT 0x16B8EC, because x0 holds -2/-3 after dequeueOutputBuffer
ADDR_DEQUEUE_OUT_LOOP = 0x16B8E8
ADDR_DEQUEUE_OUT_CHK = 0x16B904
ADDR_DEQUEUE_OUT_OK = 0x16B908
ADDR_DEQUEUE_OUT_SKIP = 0x16B9A4

ALIGN_16K = 0x4000
CAVE_VADDR = 0x254000


def align_up(val: int, align: int) -> int:
    return (val + align - 1) & ~(align - 1)


def enc_b(pc: int, target: int) -> int:
    delta = target - pc
    assert -0x08000000 <= delta < 0x08000000 and (delta & 3) == 0, f"b out of range: {delta:#x}"
    return 0x14000000 | ((delta >> 2) & 0x03FFFFFF)


def enc_bl(pc: int, target: int) -> int:
    delta = target - pc
    assert -0x08000000 <= delta < 0x08000000 and (delta & 3) == 0, f"bl out of range: {delta:#x}"
    return 0x94000000 | ((delta >> 2) & 0x03FFFFFF)


def enc_b_cond(pc: int, target: int, cond: int) -> int:
    delta = target - pc
    assert -0x00100000 <= delta < 0x00100000 and (delta & 3) == 0, f"b.cond out of range: {delta:#x}"
    return 0x54000000 | (((delta >> 2) & 0x7FFFF) << 5) | cond


def enc_tbz(pc: int, target: int, reg: int, bit: int) -> int:
    delta = target - pc
    assert -0x8000 <= delta < 0x8000 and (delta & 3) == 0, f"tbz out of range: {delta:#x}"
    b5 = (bit >> 5) & 1
    b40 = bit & 0x1F
    return (b5 << 31) | 0x36000000 | (b40 << 19) | (((delta >> 2) & 0x3FFF) << 5) | reg


def enc_tbnz(pc: int, target: int, reg: int, bit: int) -> int:
    delta = target - pc
    assert -0x8000 <= delta < 0x8000 and (delta & 3) == 0, f"tbnz out of range: {delta:#x}"
    b5 = (bit >> 5) & 1
    b40 = bit & 0x1F
    return (b5 << 31) | 0x37000000 | (b40 << 19) | (((delta >> 2) & 0x3FFF) << 5) | reg


def build_cave(cave_vaddr: int):
    """Assemble cave_dequeue_input and cave_check_output at cave_vaddr."""
    insns = []

    def pc() -> int:
        return cave_vaddr + len(insns) * 4

    # --- cave_dequeue_input ---
    insns.append(0xA9BF7BF9)  # stp x25, x30, [sp, #-16]!
    insns.append(0x52800519)  # mov w25, #0x28 (40 retries * 5ms = 200ms)

    loop_pc = pc()
    insns.append(0xF9400260)  # ldr x0, [x19] (ctx->codec)
    insns.append(0xD2827101)  # mov x1, #0x1388 (5000 us)
    insns.append(enc_bl(pc(), ADDR_DEQUEUE_IN_PLT))
    tbz_done_idx = len(insns)
    insns.append(0)
    insns.append(0xB100041F)  # cmn x0, #1
    bne_done_idx = len(insns)
    insns.append(0)
    insns.append(0x71000739)  # subs w25, w25, #1
    beq_timeout_idx = len(insns)
    insns.append(0)

    # Drain one output frame via decode_frame(ctx, NULL, 0, out_cb, out_status)
    insns.append(0xAA1303E0)  # mov x0, x19
    insns.append(0xAA1F03E1)  # mov x1, xzr
    insns.append(0x2A1F03E2)  # mov w2, wzr
    insns.append(0xAA1403E3)  # mov x3, x20
    insns.append(0xAA1603E4)  # mov x4, x22
    insns.append(enc_bl(pc(), ADDR_DECODE_FRAME))
    insns.append(enc_b(pc(), loop_pc))

    timeout_pc = pc()
    insns.append(0x92800000)  # mov x0, #-1

    done_pc = pc()
    insns[tbz_done_idx] = enc_tbz(cave_vaddr + tbz_done_idx * 4, done_pc, 0, 63)
    insns[bne_done_idx] = enc_b_cond(cave_vaddr + bne_done_idx * 4, done_pc, 1)
    insns[beq_timeout_idx] = enc_b_cond(cave_vaddr + beq_timeout_idx * 4, timeout_pc, 0)

    insns.append(0xA8C17BF9)  # ldp x25, x30, [sp], #16
    insns.append(0xD65F03C0)  # ret

    # --- cave_check_output ---
    check_out_pc = pc()
    insns.append(enc_tbnz(pc(), pc() + 8, 0, 63))   # if x0 < 0, skip next
    insns.append(enc_b(pc(), ADDR_DEQUEUE_OUT_OK))  # x0 >= 0 -> 0x16b908
    insns.append(0xB100081F)  # cmn x0, #2 (INFO_OUTPUT_FORMAT_CHANGED)
    beq_loop_idx = len(insns)
    insns.append(0)
    insns.append(0xB1000C1F)  # cmn x0, #3 (INFO_OUTPUT_BUFFERS_CHANGED)
    bne_skip_idx = len(insns)
    insns.append(0)
    loop_jmp_pc = pc()
    insns.append(enc_b(pc(), ADDR_DEQUEUE_OUT_LOOP))
    skip_jmp_pc = pc()
    insns.append(enc_b(pc(), ADDR_DEQUEUE_OUT_SKIP))
    insns[beq_loop_idx] = enc_b_cond(cave_vaddr + beq_loop_idx * 4, loop_jmp_pc, 0)
    insns[bne_skip_idx] = enc_b_cond(cave_vaddr + bne_skip_idx * 4, skip_jmp_pc, 1)

    return b"".join(struct.pack("<I", i) for i in insns), cave_vaddr, check_out_pc


def patch_elf(in_path: str, out_path: str) -> None:
    data = bytearray(open(in_path, "rb").read())

    assert data[:4] == b"\x7fELF" and data[4] == 2 and struct.unpack("<H", data[18:20])[0] == 183
    assert data[ADDR_DEQUEUE_IN_CALL:ADDR_DEQUEUE_IN_CALL + 8] == struct.pack("<II", 0x92800001, 0x94030F39), \
        "Unexpected instructions at ADDR_DEQUEUE_IN_CALL (unsupported Parsec version?)"
    assert data[ADDR_DEQUEUE_OUT_CHK:ADDR_DEQUEUE_OUT_CHK + 4] == struct.pack("<I", 0xB7F80500), \
        "Unexpected instruction at ADDR_DEQUEUE_OUT_CHK (unsupported Parsec version?)"

    cave_offset = align_up(len(data), ALIGN_16K)
    data.extend(b"\x00" * (cave_offset - len(data)))

    cave_bytes, cave_in_vaddr, cave_out_vaddr = build_cave(CAVE_VADDR)
    data.extend(cave_bytes)

    # Convert PT_NOTE (type=4) program header into PT_LOAD (type=1, PF_R|PF_X=5)
    e_phoff = struct.unpack("<Q", data[32:40])[0]
    e_phentsize = struct.unpack("<H", data[54:56])[0]
    e_phnum = struct.unpack("<H", data[56:58])[0]
    replaced = False
    for idx in range(e_phnum):
        off = e_phoff + idx * e_phentsize
        p_type = struct.unpack("<I", data[off:off + 4])[0]
        if p_type == 4:
            data[off:off + e_phentsize] = struct.pack(
                "<IIQQQQQQ",
                1, 5, cave_offset, CAVE_VADDR, CAVE_VADDR,
                len(cave_bytes), len(cave_bytes), ALIGN_16K,
            )
            replaced = True
            break
    assert replaced, "Could not find PT_NOTE program header to repurpose"

    data[ADDR_DEQUEUE_IN_CALL:ADDR_DEQUEUE_IN_CALL + 4] = struct.pack(
        "<I", enc_bl(ADDR_DEQUEUE_IN_CALL, cave_in_vaddr)
    )
    data[ADDR_DEQUEUE_IN_CALL + 4:ADDR_DEQUEUE_IN_CALL + 8] = struct.pack("<I", 0xD503201F)
    data[ADDR_DEQUEUE_OUT_CHK:ADDR_DEQUEUE_OUT_CHK + 4] = struct.pack(
        "<I", enc_b(ADDR_DEQUEUE_OUT_CHK, cave_out_vaddr)
    )

    open(out_path, "wb").write(data)
    print(f"Patched {out_path}: cave_in=0x{cave_in_vaddr:x}, cave_out=0x{cave_out_vaddr:x}, size={len(cave_bytes)}B")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input_libmain.so> <output_libmain.so>")
        sys.exit(1)
    patch_elf(sys.argv[1], sys.argv[2])
