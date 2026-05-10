import unittest
from pathlib import Path

from naming import build_output_basename, versioned_path
from folder_scan import find_slot_images, scan_texture_root
from visibility_rules import parse_description, is_visible_for_view, visibility_for_description
from model_kind import infer_texture_mode
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


class TestVisibility(unittest.TestCase):
    def test_hide_list(self):
        d = parse_description("hide: Nose Front , Tread Rear ")
        assert d is not None
        self.assertFalse(is_visible_for_view(d, "Nose Front"))
        self.assertTrue(is_visible_for_view(d, "SQ - Tread Front"))

    def test_show_only(self):
        d = parse_description("show:Nose Front")
        assert d is not None
        self.assertTrue(is_visible_for_view(d, "Nose Front"))
        self.assertFalse(is_visible_for_view(d, "Tread Rear"))

    def test_substring_freeform(self):
        self.assertFalse(visibility_for_description("markers for Nose Front QA", "Nose Front"))
        self.assertTrue(visibility_for_description("markers for Nose Front QA", "Tread Rear"))


class TestModelKind(unittest.TestCase):
    def test_infer_from_filename(self):
        from pathlib import Path

        self.assertEqual(infer_texture_mode(Path("Treads Plus Square Nose - Decal.f3d")), "decal")
        self.assertEqual(infer_texture_mode(Path("Treads Plus Bullnose - Appearance.f3d")), "appearance")


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


if __name__ == "__main__":
    unittest.main()
