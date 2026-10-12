#!/usr/bin/env python3
"""Recover the CRC-16 polynomial of the stove frames from captured data.

Input: a text file with one /read response per line (the comma-separated "data"
string), all from the same request (e.g. byte=81), so they have the same length.
You need a few frames whose payload differs (temperatures change every few seconds,
so polling for a minute is enough).

How it works: for equal-length messages a CRC is affine, so
    crc(a) XOR crc(b) == crc0(a XOR b)
where crc0 has init=0 and xorout=0. That removes the unknown init/xorout values and
leaves only the polynomial, which is brute-forced (all 65536, both bit orders, both
byte orders of the stored value) with numpy. Then the usual init/xorout/start-offset
combinations are tried for the polynomial found.

    python3 crc_polysearch.py frames_81.txt
"""
import random
import sys

import numpy as np


def load(path):
    frames = []
    for line in open(path):
        b = [int(x) for x in line.strip().split(",") if x != ""]
        if len(b) > 4:
            n = b[2] + 5  # header: cmd, 0, len; frame = 3 + len + 2 (second CRC)
            frames.append(b[:n])
    return frames


def crc0_all_polys(msg, reflected):
    polys = np.arange(65536, dtype=np.uint32)
    crc = np.zeros(65536, dtype=np.uint32)
    for byte in msg:
        for k in range(8):
            if reflected:
                fb = (crc & 1) ^ ((byte >> k) & 1)
                crc = (crc >> 1) ^ np.where(fb == 1, polys, 0).astype(np.uint32)
            else:
                fb = ((crc >> 15) & 1) ^ ((byte >> (7 - k)) & 1)
                crc = ((crc << 1) & 0xFFFF) ^ np.where(fb == 1, polys, 0).astype(np.uint32)
    return crc


def crc16_reflected(data, poly, init):
    c = init
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ poly if c & 1 else c >> 1
    return c


def main():
    frames = load(sys.argv[1])
    frames = [f for f in frames if len(f) == len(frames[0])]
    distinct = list({tuple(f) for f in frames})
    if len(distinct) < 3:
        sys.exit("need at least 3 different frames of the same length")
    n = len(distinct[0])
    E = n - 4  # first CRC sits 4 bytes from the end
    print(f"{len(distinct)} distinct frames of {n} bytes; CRC assumed at bytes {E}-{E + 1}")
    random.seed(1)
    found = []
    for reflected in (False, True):
        for order in ("LE", "BE"):
            ok = np.ones(65536, dtype=bool)
            for _ in range(6):
                a, b = random.sample(distinct, 2)
                diff = [x ^ y for x, y in zip(a[:E], b[:E])]
                va = a[E] | a[E + 1] << 8 if order == "LE" else a[E] << 8 | a[E + 1]
                vb = b[E] | b[E + 1] << 8 if order == "LE" else b[E] << 8 | b[E + 1]
                ok &= crc0_all_polys(diff, reflected) == (va ^ vb)
                if not ok.any():
                    break
            hits = [int(p) for p in np.nonzero(ok)[0]]
            print(f"{'reflected' if reflected else 'normal':9} {order}: {[hex(p) for p in hits[:5]]}")
            found += [(p, reflected, order) for p in hits]

    for poly, reflected, order in found:
        if not reflected or order != "LE":
            continue  # only the reflected/LE case is checked further here
        for init in (0xFFFF, 0x0000):
            for start in range(4):
                good = sum(crc16_reflected(f[start:E], poly, init) == f[E] | f[E + 1] << 8 for f in distinct)
                if good == len(distinct):
                    print(f"=> poly {poly:#06x} reflected, init {init:#06x}, bytes {start}..{E - 1}, "
                          f"stored little-endian: {good}/{len(distinct)} frames match")


if __name__ == "__main__":
    main()
