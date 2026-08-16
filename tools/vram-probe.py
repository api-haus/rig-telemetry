#!/usr/bin/env python3
"""Allocate VRAM until the driver refuses. Used by `vram-guard verify`.

Exit 0 when refused, 2 when the ceiling was reached without a refusal, 3 when
CUDA itself is unavailable. Run it inside the cgroup under test.
"""

import ctypes
import os
import sys

CHUNK = 128 << 20
cuda = ctypes.CDLL("libcuda.so.1")


def charged() -> int:
    """Read dmem.current for the cgroup this process is in."""
    with open(f"/proc/{os.getpid()}/cgroup") as f:
        path = f.read().strip().split(":")[-1]
    with open(f"/sys/fs/cgroup{path}/dmem.current") as f:
        return int(f.read().strip().rsplit(" ", 1)[1])


def main() -> int:
    ceiling = int(sys.argv[1]) if len(sys.argv) > 1 else 6 << 30
    if cuda.cuInit(0) != 0:
        print("no CUDA")
        return 3
    dev = ctypes.c_int()
    ctx = ctypes.c_void_p()
    if cuda.cuDeviceGet(ctypes.byref(dev), 0) != 0:
        print("no device")
        return 3
    if cuda.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev) != 0:
        print(f"refused at context create, charged {charged() >> 20} MiB")
        return 0

    held = 0
    while held < ceiling:
        p = ctypes.c_void_p()
        if cuda.cuMemAlloc_v2(ctypes.byref(p), ctypes.c_size_t(CHUNK)) != 0:
            print(f"refused after {held >> 20} MiB, charged {charged() >> 20} MiB")
            return 0
        held += CHUNK

    print(f"NOT refused, held {held >> 20} MiB, charged {charged() >> 20} MiB")
    return 2


if __name__ == "__main__":
    sys.exit(main())
