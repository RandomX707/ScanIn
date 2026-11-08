from __future__ import annotations

import struct
import zlib
from zipfile import ZipFile

from tests.conftest import make_fixture


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _validate_png(png: bytes):
    assert png.startswith(PNG_SIGNATURE)
    offset = len(PNG_SIGNATURE)
    chunks = []
    idat = bytearray()
    while offset < len(png):
        assert offset + 12 <= len(png), "truncated PNG chunk"
        length = struct.unpack_from(">I", png, offset)[0]
        kind = png[offset + 4 : offset + 8]
        end = offset + 12 + length
        assert end <= len(png), "PNG chunk extends beyond end of file"
        payload = png[offset + 8 : offset + 8 + length]
        declared_crc = struct.unpack_from(">I", png, offset + 8 + length)[0]
        assert declared_crc == zlib.crc32(kind + payload) & 0xFFFFFFFF, f"bad CRC in {kind!r}"
        chunks.append(kind)
        if kind == b"IDAT":
            idat.extend(payload)
        offset = end
        if kind == b"IEND":
            break

    assert offset == len(png), "trailing bytes after IEND"
    assert chunks[0] == b"IHDR" and chunks[-1] == b"IEND"
    assert chunks.count(b"IHDR") == 1 and chunks.count(b"IEND") == 1
    assert chunks[1:-1] == [b"IDAT"] and len(idat) > 0
    width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", png[16:29])
    assert (bit_depth, color_type, compression, filtering, interlace) == (8, 2, 0, 0, 0)
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(idat) + decoder.flush()
    assert decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
    assert len(decoded) == height * (1 + width * 3), "decoded RGB scanlines have an invalid length"
    assert all(decoded[row * (1 + width * 3)] == 0 for row in range(height)), "unexpected PNG filter"
    pixels = [decoded[row * (1 + width * 3) + 1 : (row + 1) * (1 + width * 3)] for row in range(height)]
    return width, height, pixels


def _decode_fixture_png(path):
    with ZipFile(path) as package:
        return _validate_png(package.read("xl/media/image1.png"))


def test_generated_fixture_png_has_valid_chunks_and_decodable_rgb_pixels(tmp_path):
    path = make_fixture(tmp_path / "png.xlsx")
    width, height, rows = _decode_fixture_png(path)

    assert width >= 32 and height >= 32
    colors = {tuple(row[x : x + 3]) for row in rows for x in range(0, len(row), 3)}
    assert len(colors) >= 3, "synthetic image should be visibly nontrivial"
    assert {(30, 75, 122), (255, 255, 255), (35, 153, 91)} <= colors
