"""Generates tools/francmap.ico (teal rounded square with an "F") without extra dependencies."""
import struct
import zlib
from pathlib import Path


def png(size):
    rows = []
    r = size * 0.22
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            # rounded-square mask
            dx = max(r - x, 0, x - (size - 1 - r))
            dy = max(r - y, 0, y - (size - 1 - r))
            inside = dx * dx + dy * dy <= r * r
            t = (x + y) / (2 * size)
            c = (int(20 + (3 - 20) * t), int(184 + (105 - 184) * t), int(166 + (161 - 166) * t))
            # "F" glyph
            u, v = x / size, y / size
            f = (0.34 <= u <= 0.46 and 0.24 <= v <= 0.78) or (0.34 <= u <= 0.70 and 0.24 <= v <= 0.35) \
                or (0.34 <= u <= 0.63 and 0.46 <= v <= 0.56)
            if not inside:
                row += bytes((0, 0, 0, 0))
            elif f:
                row += bytes((255, 255, 255, 255))
            else:
                row += bytes((*c, 255))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) \
        + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


sizes = [16, 32, 48, 256]
images = [png(s) for s in sizes]
header = struct.pack("<HHH", 0, 1, len(sizes))
offset = 6 + 16 * len(sizes)
entries = b""
for s, img in zip(sizes, images):
    entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
    offset += len(img)
out = Path(__file__).with_name("francmap.ico")
out.write_bytes(header + entries + b"".join(images))
print("wrote", out)
