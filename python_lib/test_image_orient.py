"""Tests for image_orient: header-only size reading and Pillow-free 90-degree rotation.

Imported from the add-in folder because image_orient.py is pure stdlib (no adsk).
"""

import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

_ADDIN = Path(__file__).resolve().parent.parent / "fusion_addin" / "BatchRender"
if str(_ADDIN) not in sys.path:
    sys.path.insert(0, str(_ADDIN))

import image_orient as io  # noqa: E402


def _png_bytes(width, height, rgb=(200, 150, 90)):
    """Minimal valid 8-bit RGB PNG whose top-left pixel is pure red (orientation marker)."""

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter type: None
        for x in range(width):
            rows.extend((255, 0, 0) if (x == 0 and y == 0) else rgb)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows)))
        + chunk(b"IEND", b"")
    )


def _png_pixel(path, x, y):
    """Decode one RGB pixel from a non-interlaced 8-bit RGB/RGBA PNG (test helper)."""
    data = Path(path).read_bytes()
    pos = 8
    width = height = None
    channels = 3
    idat = b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert depth == 8, depth
            channels = {2: 3, 6: 4}[ctype]
        elif tag == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * channels
    prev = bytearray(stride)
    out = None
    offset = 0
    for row in range(height):
        ftype = raw[offset]
        line = bytearray(raw[offset + 1:offset + 1 + stride])
        offset += 1 + stride
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 0xFF
            elif ftype == 2:
                line[i] = (line[i] + b) & 0xFF
            elif ftype == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        if row == y:
            out = tuple(line[x * channels:x * channels + 3])
        prev = line
    return out


class SizeTests(unittest.TestCase):
    def test_png_size_from_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.png"
            p.write_bytes(_png_bytes(40, 9))
            self.assertEqual(io.image_size(p), (40, 9))

    def test_jpeg_size_from_sof_marker(self):
        # SOI, APP0 (skipped), SOF0 with height=486 width=2500, EOI.
        jpeg = (
            b"\xff\xd8"
            + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
            + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 486, 2500)
            + b"\x03" + b"\x00" * 9
            + b"\xff\xd9"
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.jpg"
            p.write_bytes(jpeg)
            self.assertEqual(io.image_size(p), (2500, 486))

    def test_unreadable_or_foreign_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.txt"
            p.write_bytes(b"not an image")
            self.assertIsNone(io.image_size(p))
            self.assertIsNone(io.image_size(Path(tmp) / "missing.png"))


class OrientationTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(io.orientation_of((2500, 486)), io.LANDSCAPE)
        self.assertEqual(io.orientation_of((141, 1000)), io.PORTRAIT)
        self.assertEqual(io.orientation_of((500, 500)), io.SQUARE)
        self.assertEqual(io.orientation_of((500, 505)), io.SQUARE)  # within tolerance
        self.assertIsNone(io.orientation_of(None))
        self.assertIsNone(io.orientation_of((0, 10)))

    def test_rotation_only_when_portrait_meets_landscape(self):
        self.assertTrue(io.needs_rotation(io.PORTRAIT, io.LANDSCAPE))
        self.assertTrue(io.needs_rotation(io.LANDSCAPE, io.PORTRAIT))
        self.assertFalse(io.needs_rotation(io.LANDSCAPE, io.LANDSCAPE))
        self.assertFalse(io.needs_rotation(io.PORTRAIT, io.PORTRAIT))
        # Unknown or square on either side: leave the image alone.
        self.assertFalse(io.needs_rotation(None, io.LANDSCAPE))
        self.assertFalse(io.needs_rotation(io.SQUARE, io.LANDSCAPE))

    def test_the_real_authored_and_colour_set_sizes(self):
        """The mismatch that produced the crosswise band on the Bullnose plank."""
        authored_plank = io.orientation_of((137, 786))
        authored_nose = io.orientation_of((797, 144))  # "dusk-cherry - 1 - Horizontal"
        colour_set = io.orientation_of((1836, 424))
        self.assertTrue(io.needs_rotation(authored_plank, colour_set))
        self.assertFalse(io.needs_rotation(authored_nose, colour_set))


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "Color Set 99-2.png"
        self.src.write_bytes(_png_bytes(12, 5))
        self.made = []

    def tearDown(self):
        for p in self.made:
            try:
                p.unlink()
                p.parent.rmdir()
            except OSError:
                pass
        self.tmp.cleanup()

    def _rotate(self, clockwise=True):
        dst, how = io.rotated_copy(self.src, clockwise=clockwise)
        if dst is None:
            self.skipTest("no rotation backend on this platform: {}".format(how))
        self.made.append(dst)
        return dst, how

    def test_dimensions_swap(self):
        dst, _ = self._rotate()
        self.assertEqual(io.image_size(dst), (5, 12))

    def test_keeps_the_original_stem(self):
        # Fusion renames a decal after its image, so the stem (and its trailing
        # slot digit) must survive.
        dst, _ = self._rotate()
        self.assertEqual(dst.stem, "Color Set 99-2")
        self.assertEqual(dst.suffix, ".png")

    def test_never_writes_next_to_the_source(self):
        # The colour-set folder is the client's delivery folder.
        dst, _ = self._rotate()
        self.assertNotEqual(dst.parent, self.src.parent)
        self.assertEqual(sorted(p.name for p in self.src.parent.iterdir()), ["Color Set 99-2.png"])

    def test_second_call_is_served_from_cache(self):
        self._rotate()
        _dst, how = self._rotate()
        self.assertEqual(how, "cache")

    def test_clockwise_moves_top_left_to_top_right(self):
        dst, _ = self._rotate(clockwise=True)
        w, _h = io.image_size(dst)
        self.assertEqual(_png_pixel(dst, w - 1, 0), (255, 0, 0))

    def test_counter_clockwise_moves_top_left_to_bottom_left(self):
        dst, _ = self._rotate(clockwise=False)
        _w, h = io.image_size(dst)
        self.assertEqual(_png_pixel(dst, 0, h - 1), (255, 0, 0))

    def test_unreadable_source_fails_cleanly(self):
        bad = Path(self.tmp.name) / "bad.png"
        bad.write_bytes(b"nope")
        dst, why = io.rotated_copy(bad)
        self.assertIsNone(dst)
        self.assertIn("cannot read image size", why)


class PlannedSizeTests(unittest.TestCase):
    """Pure arithmetic: what the prepared copy's pixel size will be."""

    def test_rotation_only_swaps_the_sides(self):
        self.assertEqual(io.planned_size((1836, 424), True, None), (424, 1836))

    def test_conform_keeps_the_long_side_and_matches_the_authored_aspect(self):
        # Bullnose plank: authored 137x786, colour set 1836x424 rotated.
        w, h = io.planned_size((1836, 424), True, io.aspect_of((137, 786)))
        self.assertEqual(h, 1836)
        self.assertAlmostEqual(w / h, 137 / 786, delta=0.002)

    def test_landscape_authored_keeps_width(self):
        # Bullnose nose "... - Horizontal": authored 797x144, no rotation.
        w, h = io.planned_size((2500, 486), False, io.aspect_of((797, 144)))
        self.assertEqual(w, 2500)
        self.assertAlmostEqual(w / h, 797 / 144, delta=0.02)

    def test_squarer_authored_image_widens_the_copy(self):
        # Square Nose: authored 311x676. Rotation alone would give 486x2500 and
        # cover only ~42% of the authored width — the bare-margin case.
        w, h = io.planned_size((2500, 486), True, io.aspect_of((311, 676)))
        self.assertEqual(h, 2500)
        self.assertGreater(w, 486 * 2)
        self.assertAlmostEqual(w / h, 311 / 676, delta=0.002)

    def test_near_identical_aspect_is_left_alone(self):
        self.assertEqual(io.planned_size((1000, 200), False, 5.02), (1000, 200))

    def test_long_side_is_capped(self):
        w, h = io.planned_size((5237, 3491), True, io.aspect_of((141, 1000)), max_side=4096)
        self.assertEqual(max(w, h), 4096)
        self.assertAlmostEqual(w / h, 141 / 1000, delta=0.002)

    def test_no_cap_when_disabled(self):
        self.assertEqual(io.planned_size((5237, 3491), True, None, max_side=0), (3491, 5237))

    def test_aspect_of(self):
        self.assertAlmostEqual(io.aspect_of((200, 100)), 2.0)
        self.assertIsNone(io.aspect_of(None))
        self.assertIsNone(io.aspect_of((0, 5)))


class PreparedCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "Color Set 98-1.png"
        self.src.write_bytes(_png_bytes(40, 10))
        self.made = []

    def tearDown(self):
        for p in self.made:
            try:
                p.unlink()
                p.parent.rmdir()
            except OSError:
                pass
        self.tmp.cleanup()

    def _prepare(self, **kw):
        dst, how = io.prepared_copy(self.src, **kw)
        if dst is None:
            self.skipTest("no image backend on this platform: {}".format(how))
        if dst != self.src:
            self.made.append(dst)
        return dst, how

    def test_nothing_to_do_returns_the_original_file(self):
        dst, how = self._prepare(rotate=False, target_aspect=4.0)
        self.assertEqual(dst, self.src)
        self.assertEqual(how, "original")

    def test_rotate_and_conform_to_a_portrait_aspect(self):
        dst, _ = self._prepare(rotate=True, target_aspect=0.5)
        self.assertEqual(io.image_size(dst), (20, 40))

    def test_conform_without_rotation(self):
        dst, _ = self._prepare(rotate=False, target_aspect=8.0)
        self.assertEqual(io.image_size(dst), (40, 5))

    def test_conformed_copy_is_opaque_at_its_edges(self):
        # Bicubic resampling must not fade the border toward transparent/black —
        # that would show as a pale or dark rim along the edge of the part.
        dst, how = self._prepare(rotate=False, target_aspect=8.0)
        if how not in ("gdiplus", "pillow", "cache"):
            self.skipTest("edge check only meaningful for resampling backends")
        w, h = io.image_size(dst)
        for x, y in ((w - 1, h - 1), (w - 1, 0), (w // 2, h - 1)):
            r, g, b = _png_pixel(dst, x, y)
            self.assertGreater(r, 150, (x, y, (r, g, b)))
            self.assertGreater(g, 100, (x, y, (r, g, b)))

    def test_different_requests_do_not_share_a_cache_entry(self):
        a, _ = self._prepare(rotate=True, target_aspect=None)
        b, _ = self._prepare(rotate=True, target_aspect=0.5)
        self.assertNotEqual(a, b)
        self.assertEqual(a.name, b.name)  # both keep the original stem


if __name__ == "__main__":
    unittest.main()
