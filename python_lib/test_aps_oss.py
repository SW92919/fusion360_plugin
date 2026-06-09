import unittest
from pathlib import Path

from aps_oss import (
    build_job_manifest,
    build_job_prefix,
    encode_object_key,
    manifest_json,
    sanitize_bucket_key,
)


class TestApsOss(unittest.TestCase):
    def test_sanitize_bucket_key(self):
        self.assertEqual(sanitize_bucket_key("My Bucket!"), "my-bucket")
        self.assertTrue(sanitize_bucket_key("", fallback_seed="abc123").startswith("lbr-"))

    def test_encode_object_key(self):
        self.assertEqual(encode_object_key("jobs/a b/c.png"), "jobs%2Fa%20b%2Fc.png")
        self.assertIn("%2F", encode_object_key("jobs/20260609/uid/model/job.json"))

    def test_build_job_prefix(self):
        prefix = build_job_prefix("Model A", "Color Set 01", "Full Front")
        self.assertTrue(prefix.startswith("jobs/"))
        self.assertIn("Model_A", prefix)
        self.assertIn("Full_Front", prefix)

    def test_build_job_manifest(self):
        slot = Path("Color Set 01-1.jpg")
        prefix = "jobs/test/job"
        manifest = build_job_manifest(
            model_stem="End Cap - Decal",
            model_path=Path("End Cap - Decal.f3d"),
            color_folder=Path("Color Set 01"),
            color_name="Color Set 01",
            view_name="Full Front",
            width=1920,
            height=1080,
            render_mode="fusion_raytrace",
            slot_paths=[slot, None],
            job_prefix=prefix,
        )
        self.assertEqual(manifest["view"], "Full Front")
        self.assertEqual(len(manifest["textures"]), 1)
        self.assertTrue(manifest_json(manifest).startswith(b"{"))


if __name__ == "__main__":
    unittest.main()
