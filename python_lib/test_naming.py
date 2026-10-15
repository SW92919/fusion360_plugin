"""Output names and colour-set folder scanning (the add-in's support_paths.py)."""

import sys
import unittest
from pathlib import Path

_ADDIN = Path(__file__).resolve().parent.parent / "fusion_addin" / "LifeproofBatchRender"
if str(_ADDIN) not in sys.path:
    sys.path.insert(0, str(_ADDIN))

from support_paths import build_output_basename, find_slot_images, versioned_path  # noqa: E402


class TestNaming(unittest.TestCase):
    def test_basename(self):
        b = build_output_basename(
            "Treads Plus Bullnose - Appearance",
            "antler-trail-oak-1",
            "Nose Front",
        )
        self.assertEqual(b, "Treads Plus Bullnose - Appearance - antler-trail-oak-1 - Nose Front")

    def test_versioned(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.png"
            p.write_bytes(b"x")
            v = versioned_path(p)
            self.assertEqual(v.name, "a (v2).png")


class TestFolderScan(unittest.TestCase):
    def test_slots(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "x_1.png").write_bytes(b"1")
            (root / "x_2.png").write_bytes(b"2")
            s1, s2 = find_slot_images(root)
            self.assertIsNotNone(s1)
            self.assertIsNotNone(s2)

    def test_flipped_sidecar_ignored(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "x_1.png").write_bytes(b"1")
            (root / "x_1_flipped.png").write_bytes(b"f")
            (root / "x_2.png").write_bytes(b"2")
            s1, s2 = find_slot_images(root)
            self.assertIsNotNone(s1)
            self.assertEqual(s1.name, "x_1.png")
            self.assertIsNotNone(s2)

    def test_unflipped_still_usable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "wood_unflipped_1.png").write_bytes(b"1")
            (root / "wood_unflipped_2.png").write_bytes(b"2")
            s1, s2 = find_slot_images(root)
            self.assertIsNotNone(s1)
            self.assertEqual(s1.name, "wood_unflipped_1.png")

    def test_flipped_with_space_ignored(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "x_1.png").write_bytes(b"1")
            (root / "x 1 flipped.png").write_bytes(b"f")
            (root / "x_2.png").write_bytes(b"2")
            s1, s2 = find_slot_images(root)
            self.assertIsNotNone(s1)
            self.assertEqual(s1.name, "x_1.png")

    def test_color_set_stem_with_flipped_suffix(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "Color Set 02-1.jpg").write_bytes(b"1")
            (root / "Color Set 02-1_flipped.jpg").write_bytes(b"f")
            (root / "Color Set 02-2.jpg").write_bytes(b"2")
            s1, s2 = find_slot_images(root)
            self.assertIsNotNone(s1)
            self.assertEqual(s1.name, "Color Set 02-1.jpg")

    def test_fused_flipped_token_ignored(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "ColorSet01-1.jpg").write_bytes(b"1")
            (root / "ColorSet01-1flipped.jpg").write_bytes(b"f")
            (root / "ColorSet01-2.jpg").write_bytes(b"2")
            s1, s2 = find_slot_images(root)
            self.assertIsNotNone(s1)
            self.assertEqual(s1.name, "ColorSet01-1.jpg")

    def test_hyphen_slot_suffixes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "Color Set 01-1.jpg").write_bytes(b"1")
            (root / "Color Set 01-2.jpg").write_bytes(b"2")
            s1, s2 = find_slot_images(root)
            self.assertIsNotNone(s1)
            self.assertEqual(s1.name, "Color Set 01-1.jpg")
            self.assertIsNotNone(s2)
            self.assertEqual(s2.name, "Color Set 01-2.jpg")

    def test_batch_output_png_not_used_as_slot(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "3-in-1 - Decal - Color Set 01-1 - Full Front.png").write_bytes(b"render")
            (root / "Color Set 01-1.jpg").write_bytes(b"1")
            (root / "Color Set 01-2.jpg").write_bytes(b"2")
            s1, s2 = find_slot_images(root)
            self.assertIsNotNone(s1)
            self.assertEqual(s1.name, "Color Set 01-1.jpg")


if __name__ == "__main__":
    unittest.main()
