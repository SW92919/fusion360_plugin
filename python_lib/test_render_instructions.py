"""Tests for the Render Instructions loader.

Imported from the add-in folder because render_instructions.py is pure stdlib
(no adsk), which is exactly why it lives outside texture_pipeline.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ADDIN = Path(__file__).resolve().parent.parent / "fusion_addin" / "BatchRender"
if str(_ADDIN) not in sys.path:
    sys.path.insert(0, str(_ADDIN))

import render_instructions as ri  # noqa: E402


def _entry(name="Bullnose", decals=None, configs=None):
    return {
        "fileName": name,
        "decals": decals if decals is not None else [],
        "renderConfigurations": configs
        if configs is not None
        else [{"namedView": "Full Front", "hideBodies": []}],
    }


class NameKeyTests(unittest.TestCase):
    def test_case_space_and_separator_insensitive(self):
        for variant in ("Square Nose", "square nose", "SQUARE  NOSE", "Square_Nose", "square-nose"):
            self.assertEqual(ri.normalize_name_key(variant), "square-nose", variant)

    def test_folds_nbsp_and_unicode_dashes(self):
        self.assertEqual(ri.normalize_name_key("Square Nose"), "square-nose")
        self.assertEqual(ri.normalize_name_key("3–in–1"), "3-in-1")

    def test_model_match_key_drops_extension(self):
        self.assertEqual(ri.model_match_key(Path("/x/Square Nose.f3d")), "square-nose")
        self.assertEqual(ri.model_match_key(Path("/x/3-in-1.f3d")), "3-in-1")

    def test_strip_legacy_mode_suffix(self):
        self.assertEqual(ri.strip_legacy_mode_suffix("End Cap - Decal"), "End Cap")
        self.assertEqual(ri.strip_legacy_mode_suffix("Treads Plus Bullnose - Appearance2"),
                         "Treads Plus Bullnose")
        self.assertEqual(ri.strip_legacy_mode_suffix("Bullnose"), "Bullnose")


class UnitTests(unittest.TestCase):
    def test_inches_convert_to_centimetres(self):
        got = ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "Stair Nose",
             "textureSize": {"width": 9, "length": 50, "unit": "in"}}])]))
        spec = got.models[0].decals[0]
        self.assertAlmostEqual(spec.width_cm, 9 * 2.54, places=6)
        self.assertAlmostEqual(spec.length_cm, 50 * 2.54, places=6)
        self.assertAlmostEqual(spec.width_in, 9.0, places=6)

    def test_unit_defaults_to_inches_when_absent(self):
        got = ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "Trim", "textureSize": {"width": 3, "length": 50}}])]))
        self.assertAlmostEqual(got.models[0].decals[0].length_cm, 50 * 2.54, places=6)

    def test_millimetre_length_is_not_rejected_by_the_sanity_ceiling(self):
        # 1270 mm IS the client's 50 in — the range check must run after conversion.
        got = ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "Trim",
             "textureSize": {"width": 76.2, "length": 1270, "unit": "mm"}}])]))
        self.assertTrue(got.ok, [p.message for p in got.problems])
        self.assertAlmostEqual(got.models[0].decals[0].length_in, 50.0, places=4)

    def test_absurd_dimension_is_rejected(self):
        got = ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "Trim",
             "textureSize": {"width": 3, "length": 99999, "unit": "in"}}])]))
        self.assertFalse(got.ok)

    def test_unknown_unit_is_an_error_not_a_silent_inch(self):
        got = ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "Trim",
             "textureSize": {"width": 3, "length": 50, "unit": "furlong"}}])]))
        self.assertFalse(got.ok)
        self.assertTrue(any("furlong" in p.message for p in got.problems))

    def test_length_shorter_than_width_warns(self):
        got = ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "Trim",
             "textureSize": {"width": 50, "length": 3, "unit": "in"}}])]))
        self.assertTrue(any(p.level == ri.PROBLEM_WARN and "long axis" in p.message
                            for p in got.problems))

    def test_bool_is_not_a_number(self):
        got = ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "Trim",
             "textureSize": {"width": True, "length": 50, "unit": "in"}}])]))
        self.assertFalse(got.ok)


class LabelTests(unittest.TestCase):
    def test_unhidden_render_has_no_label(self):
        got = ri.parse_instructions(json.dumps([_entry(configs=[
            {"namedView": "Full Front", "hideBodies": []}])]))
        self.assertEqual(got.models[0].render_configurations[0].label, "")

    def test_repeated_view_with_different_hides_gets_distinct_labels(self):
        got = ri.parse_instructions(json.dumps([_entry(configs=[
            {"namedView": "Full Front", "hideBodies": []},
            {"namedView": "Full Front", "hideBodies": ["Supplemental Plank"]}])]))
        labels = [c.label for c in got.models[0].render_configurations]
        self.assertEqual(labels, ["", "no Supplemental Plank"])
        self.assertEqual(len(set(labels)), 2)

    def test_true_duplicate_configs_do_not_collide(self):
        got = ri.parse_instructions(json.dumps([_entry(configs=[
            {"namedView": "Full Front", "hideBodies": ["Track"]},
            {"namedView": "Full Front", "hideBodies": ["Track"]}])]))
        labels = [c.label for c in got.models[0].render_configurations]
        self.assertEqual(len(set(labels)), 2, labels)
        self.assertTrue(any(p.level == ri.PROBLEM_WARN for p in got.problems))

    def test_config_index_is_the_real_json_position(self):
        got = ri.parse_instructions(json.dumps([_entry(configs=[
            {"namedView": "", "hideBodies": []},  # dropped
            {"namedView": "Full Back", "hideBodies": []}])]))
        cfgs = got.models[0].render_configurations
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0].index, 1)  # not 0 — the surviving config was 2nd


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.ins = ri.parse_instructions(json.dumps([
            _entry("Bullnose"), _entry("Square Nose"), _entry("3-in-1")]))

    def test_matches_ignoring_case_and_extension(self):
        for p in ("Square Nose.f3d", "square nose.f3d", "SQUARE_NOSE.f3d"):
            self.assertIsNotNone(ri.find_for_model(self.ins, Path("/m") / p), p)

    def test_unlisted_model_returns_none(self):
        self.assertIsNone(ri.find_for_model(self.ins, Path("/m/Stair Riser.f3d")))

    def test_legacy_suffixed_filename_still_matches(self):
        got = ri.find_for_model(self.ins, Path("/m/Bullnose - Decal.f3d"))
        self.assertIsNotNone(got)
        self.assertEqual(got.file_name, "Bullnose")

    def test_duplicate_filename_entries_are_reported(self):
        ins = ri.parse_instructions(json.dumps([_entry("Bullnose"), _entry("bullnose")]))
        self.assertEqual(len(ins.models), 1)
        self.assertFalse(ins.ok)


class OccurrenceSuffixTests(unittest.TestCase):
    """Fusion shows ``L-Cap (1):1``; the client's JSON says ``L-Cap``."""

    def test_strips_instance_and_copy_suffixes(self):
        for raw, want in (
            ("L-Cap (1):1", "L-Cap"),
            ("L-Cap:1", "L-Cap"),
            ("L-Cap (1)", "L-Cap"),
            ("Stair Nose:1", "Stair Nose"),
            ("Quarter Round (1):1", "Quarter Round"),
            ("3-In-1:1", "3-In-1"),
            ("Plank Skin (1)", "Plank Skin"),
            ("Bullnose", "Bullnose"),
        ):
            self.assertEqual(ri.strip_occurrence_suffixes(raw), want, raw)

    def test_part_key_matches_browser_name_to_json_name(self):
        self.assertEqual(ri.normalize_part_key("L-Cap (1):1"), ri.normalize_part_key("L-Cap"))
        self.assertEqual(ri.normalize_part_key("End Cap (1)"), ri.normalize_part_key("End-Cap"))
        self.assertEqual(ri.normalize_part_key("3-In-1:1"), ri.normalize_part_key("3-in-1"))

    def test_does_not_collapse_genuinely_different_parts(self):
        self.assertNotEqual(ri.normalize_part_key("Reducer"), ri.normalize_part_key("Reducer 2"))
        self.assertNotEqual(ri.normalize_part_key("Stair Nose"),
                            ri.normalize_part_key("Supplemental Plank"))

    def test_lookup_resolves_a_browser_style_name(self):
        ins = ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "L-Cap",
             "textureSize": {"width": 3, "length": 50, "unit": "in"}}])]))
        self.assertIsNotNone(ri.decal_size_for_part(ins.models[0], "L-Cap (1):1"))


class SlotOrderTests(unittest.TestCase):
    """Confirmed against Bullnose.f3d: Stair Nose -> _1, Supplemental Plank -> _2."""

    def _two_part(self):
        return ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "Stair Nose",
             "textureSize": {"width": 9, "length": 50, "unit": "in"}},
            {"componentPartNumber": "Supplemental Plank",
             "textureSize": {"width": 7, "length": 50, "unit": "in"}}])]))

    def test_first_entry_is_slot_1_second_is_slot_2(self):
        entry = self._two_part().models[0]
        self.assertEqual([d.slot for d in entry.decals], [1, 2])
        self.assertEqual(ri.decal_size_for_part(entry, "Stair Nose").slot, 1)
        self.assertEqual(ri.decal_size_for_part(entry, "Supplemental Plank").slot, 2)

    def test_single_part_model_is_slot_1(self):
        ins = ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "T-Molding",
             "textureSize": {"width": 2.5, "length": 50, "unit": "in"}}])]))
        self.assertEqual([d.slot for d in ins.models[0].decals], [1])

    def test_third_entry_reuses_slot_1_and_warns(self):
        ins = ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "A", "textureSize": {"width": 3, "length": 50}},
            {"componentPartNumber": "B", "textureSize": {"width": 3, "length": 50}},
            {"componentPartNumber": "C", "textureSize": {"width": 3, "length": 50}}])]))
        self.assertEqual([d.slot for d in ins.models[0].decals], [1, 2, 1])
        self.assertTrue(any(p.level == ri.PROBLEM_WARN and "_1 image" in p.message
                            for p in ins.problems))


class DecalLookupTests(unittest.TestCase):
    def test_lookup_is_case_and_spacing_insensitive(self):
        ins = ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "Supplemental Plank",
             "textureSize": {"width": 7, "length": 50, "unit": "in"}}])]))
        entry = ins.models[0]
        for probe in ("Supplemental Plank", "supplemental plank", "Supplemental_Plank"):
            self.assertIsNotNone(ri.decal_size_for_part(entry, probe), probe)
        self.assertIsNone(ri.decal_size_for_part(entry, "Track"))

    def test_size_map_is_keyed_the_same_way_as_lookup(self):
        ins = ri.parse_instructions(json.dumps([_entry(decals=[
            {"componentPartNumber": "Stair Nose",
             "textureSize": {"width": 9, "length": 50, "unit": "in"}}])]))
        entry = ins.models[0]
        self.assertIn(ri.normalize_name_key("stair nose"), ri.decal_size_map(entry))


class MalformedInputTests(unittest.TestCase):
    def test_broken_json_reports_line_and_column(self):
        got = ri.parse_instructions('[{"fileName": "Bullnose",}]')
        self.assertFalse(got.ok)
        self.assertTrue(any("line" in p.message for p in got.problems))

    def test_object_is_not_an_array(self):
        self.assertFalse(ri.parse_instructions('{"fileName": "Bullnose"}').ok)

    def test_wrapper_object_is_tolerated(self):
        got = ri.parse_instructions(json.dumps({"models": [_entry("Bullnose")]}))
        self.assertTrue(got.ok, [p.message for p in got.problems])

    def test_entry_without_configurations_is_an_error(self):
        self.assertFalse(ri.parse_instructions(json.dumps([_entry(configs=[])])).ok)

    def test_missing_file_is_reported_not_raised(self):
        got = ri.load_instructions(Path("/definitely/not/here.json"))
        self.assertFalse(got.ok)
        self.assertTrue(any("not found" in p.message for p in got.problems))

    def test_empty_path_is_reported_not_raised(self):
        self.assertFalse(ri.load_instructions(Path("")).ok)


class TextFileTests(unittest.TestCase):
    """The client sends the same JSON as a .txt file (Notepad) or as .json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, data, file_name="Rendering Instructions.txt"):
        path = self.dir / file_name
        path.write_bytes(data)
        return ri.load_instructions(path)

    def test_txt_and_json_load_the_same(self):
        data = json.dumps([_entry("Bullnose"), _entry("L-Cap")]).encode("utf-8")
        for file_name in ("Rendering Instructions.txt", "Rendering Instructions.json"):
            got = self._load(data, file_name)
            self.assertTrue(got.ok, [p.message for p in got.problems])
            self.assertEqual([m.file_name for m in got.models], ["Bullnose", "L-Cap"], file_name)

    def test_notepad_encodings_with_an_accented_letter(self):
        # ANSI (cp1252) with an even byte count used to be read as UTF-16 junk.
        name = "Caf" + chr(0xE9) + " Trim"
        text = json.dumps([_entry(name)], ensure_ascii=False)
        for pad in ("", " "):
            for encoding in ("cp1252", "utf-8", "utf-8-sig", "utf-16"):
                got = self._load((text + pad).encode(encoding))
                self.assertEqual([m.file_name for m in got.models], [name], (encoding, len(pad)))

    def test_client_file_saved_as_txt_loads_like_the_json(self):
        real = _ADDIN.parent.parent / "Resources" / "Models" / "Rendering Instructions.json"
        if not real.is_file():
            self.skipTest("client instructions file not present")
        got = self._load(real.read_bytes())
        self.assertTrue(got.ok, [p.message for p in got.problems])
        self.assertEqual(got.models, ri.load_instructions(real).models)


class RealClientFileTests(unittest.TestCase):
    """The actual file the client shipped, if it is present in the repo."""

    @classmethod
    def setUpClass(cls):
        cls.path = (Path(__file__).resolve().parent.parent
                    / "Resources" / "Models" / "Rendering Instructions.json")

    def setUp(self):
        if not self.path.is_file():
            self.skipTest("client instructions file not present")

    def test_loads_without_errors(self):
        got = ri.load_instructions(self.path)
        self.assertTrue(got.ok, [p.message for p in got.problems])
        self.assertEqual(len(got.models), 8)

    def test_every_model_lists_its_decals(self):
        got = ri.load_instructions(self.path)
        self.assertEqual([m.file_name for m in got.models if not m.decals], [])

    def test_bullnose_has_four_configs_with_unique_labels(self):
        got = ri.load_instructions(self.path)
        bull = ri.find_for_model(got, Path("Bullnose.f3d"))
        self.assertIsNotNone(bull)
        self.assertEqual(len(bull.render_configurations), 4)
        names = [(c.named_view, c.label) for c in bull.render_configurations]
        self.assertEqual(len(set(names)), 4, names)

    def test_every_f3d_on_disk_matches_an_entry(self):
        models_dir = self.path.parent
        got = ri.load_instructions(self.path)
        unmatched = [p.name for p in sorted(models_dir.glob("*.f3d"))
                     if ri.find_for_model(got, p) is None]
        self.assertEqual(unmatched, [], "these .f3d files have no JSON entry")

    def test_two_part_models_carry_both_slots(self):
        got = ri.load_instructions(self.path)
        for stem in ("Bullnose", "Square Nose"):
            entry = ri.find_for_model(got, Path(stem + ".f3d"))
            self.assertIsNotNone(entry, stem)
            self.assertEqual([d.slot for d in entry.decals], [1, 2], stem)
            self.assertEqual(entry.decals[0].part_number, "Stair Nose", stem)
            self.assertEqual(entry.decals[1].part_number, "Supplemental Plank", stem)

    def test_no_entry_still_says_Trim(self):
        """'Trim' matches no component in any model — it must not come back."""
        got = ri.load_instructions(self.path)
        offenders = [(m.file_name, d.part_number)
                     for m in got.models for d in m.decals
                     if d.part_key == ri.normalize_part_key("Trim")]
        self.assertEqual(offenders, [])

    def test_every_part_number_exists_in_its_model(self):
        """Guards the naming-mismatch class of bug that has bitten this project twice.

        Reads the .f3d binaries directly: entries are zstd (method 93) and names are
        length-prefixed UTF-16LE, so a plain zipfile read finds nothing.
        """
        try:
            import struct
            import zipfile

            import zstandard
        except ImportError:
            self.skipTest("zstandard not installed")

        def entry_bytes(f3d, info):
            with open(f3d, "rb") as fh:
                fh.seek(info.header_offset)
                head = fh.read(30)
                if len(head) < 30:
                    return b""
                sig, _v, _f, method, _t, _d, _c, csize, _u, nlen, elen = struct.unpack(
                    "<IHHHHHIIIHH", head)
                if sig != 0x04034B50:
                    return b""
                fh.seek(nlen + elen, 1)
                payload = fh.read(csize or info.compress_size)
            if method == 93:
                try:
                    return zstandard.ZstdDecompressor().decompressobj().decompress(payload)
                except Exception:
                    return b""
            if method == 8:
                import zlib
                try:
                    return zlib.decompressobj(-15).decompress(payload)
                except Exception:
                    return b""
            return payload

        def contains(f3d, needle):
            probe = needle.encode("utf-16-le")
            plain = needle.encode("utf-8")
            for info in zipfile.ZipFile(f3d).infolist():
                if info.is_dir():
                    continue
                blob = entry_bytes(f3d, info)
                if blob and (probe in blob or plain in blob):
                    return True
            return False

        got = ri.load_instructions(self.path)
        missing = []
        for model in got.models:
            f3d = self.path.parent / (model.file_name + ".f3d")
            if not f3d.is_file():
                continue
            for spec in model.decals:
                bare = ri.strip_occurrence_suffixes(spec.part_number)
                if not contains(f3d, bare):
                    missing.append("{}: componentPartNumber {!r}".format(
                        f3d.name, spec.part_number))
            for cfg in model.render_configurations:
                for name in cfg.hide_bodies:
                    if not contains(f3d, ri.strip_occurrence_suffixes(name)):
                        missing.append("{}: hideBodies {!r}".format(f3d.name, name))
        self.assertEqual(missing, [], "names in the JSON that no model contains")


class DecalTargetingIntegrationTests(unittest.TestCase):
    """Fusion browser names must resolve against the JSON's componentPartNumber.

    The browser decorates names ('L-Cap (1):1') while the client writes the bare
    part ('L-Cap'), and these must meet in the middle.
    """

    def setUp(self):
        self.path = (Path(__file__).resolve().parent.parent
                     / "Resources" / "Models" / "Rendering Instructions.json")
        if not self.path.is_file():
            self.skipTest("client instructions file not present")
        self.ins = ri.load_instructions(self.path)

    def _map_for(self, stem):
        entry = ri.find_for_model(self.ins, Path(stem + ".f3d"))
        self.assertIsNotNone(entry, stem)
        return entry, ri.decal_size_map(entry)

    def test_browser_names_from_the_real_models_resolve(self):
        # Left column is exactly what Fusion's browser shows (from the client's
        # screenshots and the .f3d component inventory).
        cases = [
            ("Bullnose", "Stair Nose:1", "Stair Nose"),
            ("Bullnose", "Supplemental Plank:1", "Supplemental Plank"),
            ("Square Nose", "Stair Nose:1", "Stair Nose"),
            ("3-in-1", "3-In-1:1", "3-In-1"),
            ("L-Cap", "L-Cap (1):1", "L-Cap"),
            ("End-Cap", "End Cap (1):1", "End Cap"),
            ("Reducer", "Reducer (1):1", "Reducer"),
            ("T-Molding", "T-Molding (1):1", "T-Molding"),
            ("Quarter-Round", "Quarter Round (1):1", "Quarter Round"),
        ]
        for stem, browser_name, expected_part in cases:
            entry, sizes = self._map_for(stem)
            key = ri.normalize_part_key(browser_name)
            self.assertIn(key, sizes, "{}: {!r} did not resolve".format(stem, browser_name))
            self.assertEqual(sizes[key].part_number, expected_part, browser_name)

    def test_non_target_components_do_not_resolve(self):
        # Hiding-only and scenery parts must NOT resolve; their decals stay as authored.
        _entry, sizes = self._map_for("3-in-1")
        for name in ("Track:1", "Foam Pad:1", "Main Light:1", "Riser 1.5", "Plank:1"):
            self.assertNotIn(ri.normalize_part_key(name), sizes, name)

    def test_every_target_has_a_positive_size(self):
        for model in self.ins.models:
            for spec in model.decals:
                self.assertGreater(spec.width_cm, 0.0, model.file_name)
                self.assertGreater(spec.length_cm, 0.0, model.file_name)
                self.assertGreaterEqual(spec.length_cm, spec.width_cm, model.file_name)

    def test_inches_round_trip_to_the_authored_numbers(self):
        entry, sizes = self._map_for("Bullnose")
        nose = sizes[ri.normalize_part_key("Stair Nose:1")]
        self.assertAlmostEqual(nose.width_in, 9.0, places=6)
        self.assertAlmostEqual(nose.length_in, 50.0, places=6)
        self.assertAlmostEqual(nose.width_cm, 22.86, places=2)
        self.assertAlmostEqual(nose.length_cm, 127.0, places=2)


class ModelDiscoveryTests(unittest.TestCase):
    """Model files are derivable: the JSON names them and they ship beside it."""

    def setUp(self):
        self.path = (Path(__file__).resolve().parent.parent
                     / "Resources" / "Models" / "Rendering Instructions.json")
        if not self.path.is_file():
            self.skipTest("client instructions file not present")
        self.ins = ri.load_instructions(self.path)

    def test_finds_every_model_next_to_the_instructions(self):
        found, lines = ri.discover_model_files(self.ins)
        self.assertEqual(len(found), len(self.ins.models), "\n".join(lines))
        self.assertTrue(all(p.suffix.lower() == ".f3d" for p in found))
        self.assertNotIn("NOT FOUND", "\n".join(lines))

    def test_order_follows_the_json(self):
        found, _ = ri.discover_model_files(self.ins)
        keys = [ri.model_match_key(p) for p in found]
        self.assertEqual(keys, [m.match_key for m in self.ins.models])

    def test_missing_file_is_reported_not_guessed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            found, lines = ri.discover_model_files(self.ins, search_dir=Path(tmp))
            self.assertEqual(found, [])
            self.assertIn("NOT FOUND", "\n".join(lines))

    def test_unknown_folder_degrades_gracefully(self):
        found, lines = ri.discover_model_files(self.ins, search_dir=Path("/no/such/dir"))
        self.assertEqual(found, [])
        self.assertTrue(lines)


class OutputNameIntegrationTests(unittest.TestCase):
    """The JSON label must survive into a distinct, filesystem-safe filename.

    Two renders of one named view differing only by hideBodies previously
    collided, and versioned_path quietly turned the second into '... (v2).png',
    which the client cannot tell apart from a re-run.
    """

    def setUp(self):
        import support_paths

        self.naming = support_paths

    def _names_for(self, configs):
        ins = ri.parse_instructions(json.dumps([_entry(configs=configs)]))
        return [
            self.naming.build_output_basename(
                "Bullnose", "Color Set 01-1", cfg.named_view, cfg.label
            )
            for cfg in ins.models[0].render_configurations
        ]

    def test_same_view_different_hides_yields_distinct_names(self):
        names = self._names_for([
            {"namedView": "Full Front", "hideBodies": []},
            {"namedView": "Full Front", "hideBodies": ["Supplemental Plank"]},
        ])
        self.assertEqual(names, [
            "Bullnose - Color Set 01-1 - Full Front",
            "Bullnose - Color Set 01-1 - Full Front - no Supplemental Plank",
        ])
        self.assertEqual(len(set(names)), 2)

    def test_unhidden_render_keeps_the_existing_three_segment_name(self):
        names = self._names_for([{"namedView": "Full Front", "hideBodies": []}])
        self.assertEqual(names[0], "Bullnose - Color Set 01-1 - Full Front")

    def test_real_client_file_produces_no_duplicate_names(self):
        path = (Path(__file__).resolve().parent.parent
                / "Resources" / "Models" / "Rendering Instructions.json")
        if not path.is_file():
            self.skipTest("client instructions file not present")
        got = ri.load_instructions(path)
        for model in got.models:
            names = [
                self.naming.build_output_basename(
                    model.file_name, "Color Set 01-1", c.named_view, c.label
                )
                for c in model.render_configurations
            ]
            self.assertEqual(len(set(names)), len(names),
                             "{} produces colliding filenames: {}".format(model.file_name, names))

    def test_label_with_path_hostile_characters_is_sanitized(self):
        names = self._names_for([
            {"namedView": "Full Front", "hideBodies": ["A/B:C"]},
        ])
        for bad in ("/", ":", "\\", "?", "*", "<", ">", "|", '"'):
            self.assertNotIn(bad, names[0], bad)


if __name__ == "__main__":
    unittest.main()
