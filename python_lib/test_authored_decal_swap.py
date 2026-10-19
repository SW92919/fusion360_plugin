"""Simulation tests for the authored-decal image swap in texture_pipeline.

Fusion's API cannot run outside Fusion, so these drive ``apply_decal_color_set``
against small stand-in objects that mimic the behaviours measured on the client's
models:

* Fusion RENAMES a decal after the image it is given.
* A healthy decal accepts a new image.
* Bullnose's "dusk-cherry - 1 - Horizontal" reports ``faces == [None]`` and refuses
  the write with "invalid argument face" (its stored face reference points at an
  occurrence that no longer exists).
* ``Decal`` has no ``parentComponent``; ``Decal.transform`` is read-only.

They check control flow, bookkeeping and image preparation — not Fusion itself.
"""

import struct
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ADDIN = _ROOT / "fusion_addin" / "BatchRender"
for _p in (str(_ADDIN), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# --- stand-in adsk package (only needed so texture_pipeline can be imported) ---
class _Anything:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        return _Anything()

    def __call__(self, *a, **k):
        return _Anything()


def _fake_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: type(attr, (_Anything,), {})
    return mod


_REAL_ADSK = "adsk" in sys.modules and not isinstance(
    getattr(sys.modules["adsk"], "__getattr__", None), types.FunctionType
)
if not _REAL_ADSK:
    _adsk, _core, _fusion = _fake_module("adsk"), _fake_module("adsk.core"), _fake_module("adsk.fusion")

    class _Cast:
        @staticmethod
        def cast(obj):
            return obj

    class _EntityTypes:
        BRepFaceEntityType = 1

    _fusion.Decal = _Cast
    _fusion.BRepFace = _Cast
    _fusion.BRepEntityTypes = _EntityTypes
    _adsk.core, _adsk.fusion = _core, _fusion
    sys.modules.update({"adsk": _adsk, "adsk.core": _core, "adsk.fusion": _fusion})

import image_orient  # noqa: E402
import render_instructions as ri  # noqa: E402
import texture_pipeline as tp  # noqa: E402


# --- tiny PNG writer ---------------------------------------------------------
def _png(path, width, height):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + bytes((180, 140, 90)) * width for _ in range(height))
    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    return Path(path)


# --- stand-in Fusion objects -------------------------------------------------
class Vec:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = float(x), float(y), float(z)


class Point(Vec):
    def distanceTo(self, other):
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2 + (self.z - other.z) ** 2) ** 0.5


class Matrix:
    def __init__(self, origin, x, y, z):
        self.origin, self.x, self.y, self.z = origin, x, y, z

    def getAsCoordinateSystem(self):
        return self.origin, self.x, self.y, self.z

    def copy(self):
        return Matrix(self.origin, self.x, self.y, self.z)

    def setWithCoordinateSystem(self, origin, x, y, z):
        self.origin, self.x, self.y, self.z = origin, x, y, z
        return True


class FakeTimeline:
    """design.timeline: the marker sits at the end unless a decal rolled it back."""

    def __init__(self):
        self.count = 10
        self._marker = 10
        self.rolled_to = None
        self.moves_to_end = 0
        self.stuck = False

    @property
    def markerPosition(self):
        return self._marker

    @markerPosition.setter
    def markerPosition(self, value):
        if getattr(self, "stuck", False):
            return
        self._marker = int(value)
        self.rolled_to = None  # the marker left the decal it was rolled to

    def moveToEnd(self):
        self.markerPosition = self.count
        self.moves_to_end += 1
        return True


_TIMELINE = FakeTimeline()

if not _REAL_ADSK:
    _core_mod = sys.modules["adsk.core"]
    _core_mod.Vector3D = types.SimpleNamespace(create=lambda x=0.0, y=0.0, z=0.0: Vec(x, y, z))
    _core_mod.Point3D = types.SimpleNamespace(create=lambda x=0.0, y=0.0, z=0.0: Point(x, y, z))
    _core_mod.Matrix3D = types.SimpleNamespace(
        create=lambda: Matrix(Point(0, 0, 0), Vec(1, 0, 0), Vec(0, 1, 0), Vec(0, 0, 1))
    )


class _Evaluator:
    """A horizontal plane at the face's height; parameters are (x, z)."""

    def __init__(self, normal, face=None):
        self._n = normal
        self._face = face

    def getNormalAtPoint(self, _point):
        return True, self._n

    def getParameterAtPoint(self, point):
        self._face._check_alive()
        return True, types.SimpleNamespace(x=point.x, y=point.z)

    def getPointAtParameter(self, param):
        self._face._check_alive()
        return True, Point(param.x, self._face._y, param.y)


class Face:
    def __init__(self, body, z_height, normal=(0, 1, 0), area=1800.0, box=None):
        self.body, self.area = body, area
        self._y = z_height
        self.evaluator = _Evaluator(Vec(*normal), self)
        self._box = box  # ((x0, y0, z0), (x1, y1, z1)) or None = unbounded
        self.stale_when_rolled = False  # a finished-model face that does not exist at the decal's step

    def _check_alive(self):
        if self.stale_when_rolled and _TIMELINE.rolled_to is not None:
            raise RuntimeError("this face does not exist at this point in the timeline")

    @property
    def boundingBox(self):
        if self._box is None:
            raise RuntimeError("no bounding box in this fake")
        lo, hi = self._box
        return types.SimpleNamespace(minPoint=Point(*lo), maxPoint=Point(*hi))

    def isPointOnFace(self, point, tol=0.0):
        self._check_alive()
        if abs(point.y - self._y) > tol:
            return False
        if self._box is None:
            return True
        lo, hi = self._box
        return lo[0] - tol <= point.x <= hi[0] + tol and lo[2] - tol <= point.z <= hi[2] + tol


class _Coll:
    def __init__(self, items):
        self._items = items

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]


class Body:
    def __init__(self, name, top_y):
        self.name = name
        self.isVisible = True
        self._faces = [Face(self, top_y)]

    @property
    def faces(self):
        return _Coll(self._faces)

    @property
    def boundingBox(self):
        boxes = [f._box for f in self._faces if f._box is not None]
        if not boxes:
            raise RuntimeError("no bounding box in this fake")
        lo = tuple(min(b[0][i] for b in boxes) for i in range(3))
        hi = tuple(max(b[1][i] for b in boxes) for i in range(3))
        return types.SimpleNamespace(minPoint=Point(*lo), maxPoint=Point(*hi))


class _Timeline:
    def __init__(self, decal):
        self.entity = decal
        self.index = 3
        self.healthState = 0
        self.isSuppressed = False
        self.isRolledBack = False

    def rollTo(self, before):
        _TIMELINE._marker = self.index if before else self.index + 1
        _TIMELINE.rolled_to = self.entity
        if getattr(self, "raise_after_roll", False):
            raise RuntimeError("compute failed after roll")
        return True


class Decal:
    def __init__(self, name, image, transform, face, broken=False):
        self.name = name
        self._image = str(image)
        self._transform = transform
        self._face = face
        self.broken = broken
        self.isChainFaces = True
        self.opacity = 1.0
        self._bulb = True
        self.refuse_show = False   # setting the bulb back ON fails (rollback trouble)
        self.refuse_delete = False
        self.refuse_write = False  # write refused although the face reads fine
        self.refuse_redefine = False  # True: redefine returns False; "raise": it raises
        self.faces_override = None  # a list of real faces, when n copies of one won't do
        self.rolled_faces = None    # what decal.faces reads while the marker is rolled to it
        self.store_as = None        # fn(matrix) -> matrix Fusion keeps (first redefine only)
        self.refuse_centre = None   # fn(origin) -> True: refused as "not on primary face"
        self.redefines = []
        self.n_faces = 1
        self.isDerived = False
        self.deleted = False
        self.writes = []
        self.timelineObject = _Timeline(self)

    @property
    def isLightBulbOn(self):
        return self._bulb

    @isLightBulbOn.setter
    def isLightBulbOn(self, value):
        if value and self.refuse_show:
            raise RuntimeError("cannot show")
        self._bulb = bool(value)

    @property
    def isVisible(self):
        return bool(self.isLightBulbOn)

    @property
    def faces(self):
        # Like the real API: a dead reference reads as ONE None however many faces
        # the designer picked (3-in-1's decal covers ~8 and reported nFaces=1).
        if self.rolled_faces is not None and _TIMELINE.rolled_to is self:
            return list(self.rolled_faces)
        if self.faces_override is not None:
            return list(self.faces_override)
        return [None] if self.broken else [self._face] * self.n_faces

    @property
    def transform(self):  # read-only, like the real build
        return self._transform

    @property
    def imageFilename(self):
        return self._image

    @imageFilename.setter
    def imageFilename(self, value):
        if self.broken or self.refuse_write:
            raise RuntimeError("3 : invalid argument face")
        self._image = str(value)
        self.writes.append(str(value))
        self.name = Path(str(value)).stem  # Fusion renames the decal

    def saveImage(self, filename):
        return False

    def redefine(self, matrix, faces, chain):
        self.redefines.append((matrix, list(faces), chain))
        if self.refuse_redefine == "raise":
            raise RuntimeError("redefine failed")
        if self.refuse_redefine:
            return False
        if _TIMELINE.rolled_to is not self:
            raise RuntimeError("the timeline marker is not just before this decal")
        _o, mx, my, mz = matrix.getAsCoordinateSystem()

        def _dot(a, b):
            return a.x * b.x + a.y * b.y + a.z * b.z

        def _len(a):
            return _dot(a, a) ** 0.5

        for a, b in ((mx, my), (my, mz), (mx, mz)):
            if abs(_dot(a, b)) > 1e-6 * _len(a) * _len(b):
                # The message Fusion gave on 3-in-1 (2026-10-15).
                raise RuntimeError("3 : Y and Z axes defined by the matrix, must be perpendicular to one another")
        # Fusion is strict about the centre (2026-10-15, four of nine decals).
        try:
            on_primary = not faces or faces[0] is None or faces[0].isPointOnFace(_o, 1e-6)
        except RuntimeError:
            on_primary = False  # a face that does not exist at this step: Fusion says the same
        if not on_primary or (self.refuse_centre is not None and self.refuse_centre(_o)):
            raise RuntimeError("3 : Input point is not located on primary face (first face in array)")
        if getattr(self, "redefine_distort", False) and len(self.redefines) == 1:
            o, mx, my, mz = matrix.getAsCoordinateSystem()
            matrix = Matrix(o, Vec(mx.x / 2, mx.y / 2, mx.z / 2), my, mz)  # Fusion keeps half the size
        if self.store_as is not None and len(self.redefines) == 1:
            matrix = self.store_as(matrix)
        self._transform = matrix
        if len(faces) > 1:
            self.faces_override = list(faces)  # Fusion lists the faces primary-first afterwards
        return True

    def deleteMe(self):
        if self.refuse_delete:
            raise RuntimeError("delete refused")
        self.deleted = True
        self._owner._items.remove(self)
        return True


class DecalInput:
    def __init__(self, image, faces, point):
        self.image, self.faces, self.point = image, faces, point
        self.isChainFaces = False
        self.opacity = 1.0
        self.transform = None


class Decals(_Coll):
    def __init__(self, items, reject_add=False, drift=0.0):
        super().__init__(items)
        self.reject_add, self.drift = reject_add, drift
        self.refuse_delete = False   # stand-ins made here cannot be deleted
        for d in items:
            d._owner = self

    def createInput(self, image, faces, point):
        return DecalInput(image, faces, point)

    def add(self, inp):
        if self.reject_add:
            raise RuntimeError("decals.add refused")
        m = inp.transform
        if self.drift:
            m = Matrix(Point(m.origin.x + self.drift, m.origin.y, m.origin.z), m.x, m.y, m.z)
        new = Decal(Path(inp.image).stem, inp.image, m, inp.faces[0])
        new.isChainFaces = inp.isChainFaces
        new.refuse_delete = self.refuse_delete
        new._owner = self
        self._items.append(new)
        return new


class Component:
    def __init__(self, cid, name, bodies, decals):
        self.id, self.name, self.partNumber = cid, name, name
        self._bodies = bodies
        self.decals = decals
        self.isDecalFolderLightBulbOn = True
        for b in bodies:
            b.parentComponent = self

    @property
    def bRepBodies(self):
        return _Coll(self._bodies)

    def findBRepUsingPoint(self, point, _etype, tol=-1, _visible_only=True):
        hits = [f for b in self._bodies for f in b._faces if f.isPointOnFace(point, max(tol, 0))]
        return _Coll(hits)


class Occurrence:
    def __init__(self, name, component):
        self.name, self.component = name, component


class Design:
    def __init__(self, occurrences, root_name="Bullnose - Decal"):
        # Real roots are named; in most one-tone models the root's name IS the
        # product name ("End Cap", "L-Cap" ...), which must never select the trim.
        self.rootComponent = types.SimpleNamespace(
            id="root", name=root_name, partNumber=root_name, decals=Decals([]),
            allOccurrences=_Coll(occurrences), isDecalFolderLightBulbOn=True,
        )
        _TIMELINE.__init__()
        self.timeline = _TIMELINE
        self.snapshots = types.SimpleNamespace(hasPendingSnapshot=False)


def _transform(y=1.0, along=127.0, across=22.86):
    """Decal frame whose |X| is ``along`` and |Y| is ``across`` (cm)."""
    return Matrix(Point(0, y, 0), Vec(along, 0, 0), Vec(0, 0, across), Vec(0, 1, 0))


# Footprints read from the client's Bullnose in Fusion (summary log, 2026-10-14).
PLANK_FOOTPRINT = dict(along=13.041, across=121.518)   # |X| x |Y|: portrait
NOSE_FOOTPRINT = dict(along=123.744, across=22.592)    # landscape


class _Fixture(unittest.TestCase):
    """Bullnose-shaped model: Supplemental Plank (portrait image) + Stair Nose (landscape)."""

    NOSE_BROKEN = False

    def setUp(self):
        self._json_size_flag = tp.DECAL_APPLY_JSON_SIZE
        tp.DECAL_APPLY_JSON_SIZE = False  # this suite: designer sizes
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.authored_plank = _png(t / "authored-plank.png", 14, 79)   # 137x786-ish, portrait
        self.authored_nose = _png(t / "authored-nose.png", 80, 14)     # 797x144-ish, landscape
        self.cs_a1 = _png(t / "Color Set 71-1.png", 100, 20)
        self.cs_a2 = _png(t / "Color Set 71-2.png", 90, 21)
        self.cs_b1 = _png(t / "Color Set 72-1.png", 120, 20)
        self.cs_b2 = _png(t / "Color Set 72-2.png", 110, 30)

        plank_skin = Body("Plank Skin (1)", top_y=1.0)
        self.plank_decal = Decal("dusk-cherry-2", self.authored_plank,
                                 _transform(**PLANK_FOOTPRINT), plank_skin._faces[0])
        self.plank = Component("c-plank", "Supplemental Plank", [plank_skin, Body("Body2", 0.4)],
                               Decals([self.plank_decal]))

        nose_skin = Body("Nose Skin (7)", top_y=1.0)
        self.nose_skin = nose_skin
        self.nose_decal = Decal("dusk-cherry - 1 - Horizontal", self.authored_nose,
                                _transform(**NOSE_FOOTPRINT),
                                nose_skin._faces[0], broken=self.NOSE_BROKEN)
        self.nose = Component("c-nose", "Stair Nose", [nose_skin, Body("Body2", 0.4)],
                              Decals([self.nose_decal]))

        # Plank occurrence listed FIRST, so a positional guess would hand it slot 1.
        self.design = Design([Occurrence("Supplemental Plank:1", self.plank),
                              Occurrence("Stair Nose:1", self.nose)])

        spec = ri.parse_instructions(
            '[{"fileName":"Bullnose","decals":['
            '{"componentPartNumber":"Stair Nose","textureSize":{"width":9,"length":50,"unit":"in"}},'
            '{"componentPartNumber":"Supplemental Plank","textureSize":{"width":7,"length":50,"unit":"in"}}],'
            '"renderConfigurations":[{"namedView":"Full Front","hideBodies":[]}]}]'
        )
        tp.set_decal_targets(ri.decal_size_map(spec.models[0]))
        tp.pop_decal_swap_failures()
        image_orient.clear_cache()

    def tearDown(self):
        tp.DECAL_APPLY_JSON_SIZE = self._json_size_flag
        tp.set_decal_targets(None)
        tp.pop_decal_swap_failures()
        image_orient.clear_cache()
        self.tmp.cleanup()

    def _apply(self, s1, s2):
        return tp.apply_decal_color_set(self.design, s1, s2)

    def _backend_available(self):
        dst, _ = image_orient.prepared_copy(self.cs_a1, rotate=True)
        return dst is not None


class HealthyModelTests(_Fixture):
    def test_both_decals_take_the_image_for_their_own_component(self):
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(total, 2, "\n".join(lines))
        # Slot comes from the Render Instructions (Stair Nose = decals[0] -> _1),
        # NOT from position (plank is listed first) and not from the decal's name
        # ("dusk-cherry - 1 - Horizontal" has no trailing digit).
        self.assertEqual(self.nose_decal.name, "Color Set 71-1")
        self.assertEqual(self.plank_decal.name, "Color Set 71-2")
        self.assertEqual(tp.pop_decal_swap_failures(), [])
        self.assertTrue(any("render-instructions slot 1" in l for l in lines), lines)

    def test_landscape_raster_is_turned_for_the_portrait_authored_decal(self):
        if not self._backend_available():
            self.skipTest("no image backend on this platform")
        self._apply(self.cs_a1, self.cs_a2)
        plank_img = image_orient.image_size(Path(self.plank_decal.imageFilename))
        nose_img = image_orient.image_size(Path(self.nose_decal.imageFilename))
        self.assertEqual(image_orient.orientation_of(plank_img), image_orient.PORTRAIT)
        self.assertEqual(image_orient.orientation_of(nose_img), image_orient.LANDSCAPE)

    def test_prepared_raster_has_the_authored_proportions(self):
        if not self._backend_available():
            self.skipTest("no image backend on this platform")
        self._apply(self.cs_a1, self.cs_a2)
        w, h = image_orient.image_size(Path(self.plank_decal.imageFilename))
        self.assertAlmostEqual(w / h, 14 / 79, delta=0.02)
        w, h = image_orient.image_size(Path(self.nose_decal.imageFilename))
        self.assertAlmostEqual(w / h, 80 / 14, delta=0.4)

    def test_nothing_is_written_into_the_colour_set_folder(self):
        before = sorted(p.name for p in Path(self.tmp.name).iterdir())
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), before)

    def test_second_colour_set_keeps_slots_and_authored_proportions(self):
        if not self._backend_available():
            self.skipTest("no image backend on this platform")
        self._apply(self.cs_a1, self.cs_a2)
        total, lines = self._apply(self.cs_b1, self.cs_b2)
        self.assertEqual(total, 2, "\n".join(lines))
        # After the first swap the decals are NAMED after colour set A; the slot
        # must still be remembered, and the proportions must still be the ones the
        # DESIGNER used — not those of the colour-set image now sitting on the decal.
        self.assertEqual(self.nose_decal.name, "Color Set 72-1")
        self.assertEqual(self.plank_decal.name, "Color Set 72-2")
        w, h = image_orient.image_size(Path(self.plank_decal.imageFilename))
        self.assertAlmostEqual(w / h, 14 / 79, delta=0.02)

    def test_no_stand_in_is_created_for_healthy_decals(self):
        self._apply(self.cs_a1, self.cs_a2)
        self._apply(self.cs_b1, self.cs_b2)
        self.assertEqual(self.plank.decals.count, 1)
        self.assertEqual(self.nose.decals.count, 1)

    def test_one_image_model_gives_slot1_only(self):
        total, lines = self._apply(self.cs_a1, None)
        self.assertEqual(total, 1, "\n".join(lines))
        self.assertEqual(self.nose_decal.name, "Color Set 71-1")
        self.assertEqual(self.plank_decal.name, "dusk-cherry-2")  # untouched


class BrokenFaceReferenceTests(_Fixture):
    NOSE_BROKEN = True

    def test_stand_in_takes_over_with_the_designers_exact_placement(self):
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        self.assertEqual(tp.pop_decal_swap_failures(), [], text)
        self.assertEqual(self.nose.decals.count, 2, text)
        original, stand_in = self.nose.decals.item(0), self.nose.decals.item(1)
        self.assertIs(original, self.nose_decal)
        self.assertFalse(original.isLightBulbOn, "original must be hidden, not stacked")
        self.assertTrue(stand_in.isLightBulbOn)
        self.assertEqual(stand_in.name, "Color Set 71-1")
        o1, x1, y1, _ = original.transform.getAsCoordinateSystem()
        o2, x2, y2, _ = stand_in.transform.getAsCoordinateSystem()
        self.assertAlmostEqual(o1.distanceTo(o2), 0.0)
        self.assertAlmostEqual(x1.x, x2.x)
        self.assertAlmostEqual(y1.z, y2.z)
        self.assertTrue(stand_in.isChainFaces)
        self.assertIs(stand_in.faces[0].body, self.nose_skin, "must sit on the Skin body")
        self.assertIn("REPLACED", text)

    def test_healthy_decal_is_untouched_by_the_fallback(self):
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(self.plank.decals.count, 1)
        self.assertEqual(len(self.plank_decal.writes), 1)
        self.assertTrue(self.plank_decal.isLightBulbOn)

    def test_next_colour_set_reuses_the_stand_in_instead_of_stacking(self):
        self._apply(self.cs_a1, self.cs_a2)
        tp.pop_decal_swap_failures()
        total, lines = self._apply(self.cs_b1, self.cs_b2)
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        self.assertEqual(self.nose.decals.count, 2, "no third decal: " + text)
        self.assertEqual(self.nose.decals.item(1).name, "Color Set 72-1")
        self.assertFalse(self.nose_decal.isLightBulbOn)
        self.assertEqual(tp.pop_decal_swap_failures(), [], text)
        self.assertNotIn("direct write refused", text, "retired original must be skipped")

    def test_no_face_under_the_decal_means_report_not_guess(self):
        self.nose_skin._faces[0]._y = 50.0  # nothing lies under the decal's centre
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 1, text)
        self.assertEqual(self.nose.decals.count, 1, text)
        self.assertTrue(self.nose_decal.isLightBulbOn, "original must stay visible")
        fails = tp.pop_decal_swap_failures()
        self.assertEqual(len(fails), 1, text)
        self.assertIn("FAILED", text)
        self.assertIn("re-select its face", text)

    def test_stand_in_that_lands_elsewhere_is_rolled_back(self):
        self.nose.decals.drift = 0.5  # stand-in comes back 5 mm off
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 1, text)
        self.assertEqual(self.nose.decals.count, 1, "rejected stand-in must be deleted: " + text)
        self.assertTrue(self.nose_decal.isLightBulbOn, "original restored: " + text)
        self.assertEqual(len(tp.pop_decal_swap_failures()), 1)
        self.assertIn("rolled back", text)

    def test_refused_creation_restores_the_original(self):
        self.nose.decals.reject_add = True
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 1, text)
        self.assertEqual(self.nose.decals.count, 1)
        self.assertTrue(self.nose_decal.isLightBulbOn, text)
        self.assertEqual(len(tp.pop_decal_swap_failures()), 1)

    def test_fallback_can_be_switched_off(self):
        old = tp.DECAL_REPLACE_UNWRITABLE_AUTHORED
        tp.DECAL_REPLACE_UNWRITABLE_AUTHORED = False
        try:
            total, lines = self._apply(self.cs_a1, self.cs_a2)
        finally:
            tp.DECAL_REPLACE_UNWRITABLE_AUTHORED = old
        self.assertEqual(total, 1, "\n".join(lines))
        self.assertEqual(self.nose.decals.count, 1)
        self.assertEqual(len(tp.pop_decal_swap_failures()), 1)

    def test_failure_is_never_counted_as_success(self):
        self.nose_skin._faces[0]._y = 50.0
        total, _ = self._apply(self.cs_a1, self.cs_a2)
        self.assertLess(total, 2)


class UnknownAuthoredImageTests(_Fixture):
    def test_unknown_authored_image_still_follows_the_footprint(self):
        if not self._backend_available():
            self.skipTest("no image backend on this platform")
        self.plank_decal._image = str(Path(self.tmp.name) / "gone.jpg")  # file no longer exists
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        size = image_orient.image_size(Path(self.plank_decal.imageFilename))
        self.assertEqual(image_orient.orientation_of(size), image_orient.PORTRAIT, text)
        self.assertIn("footprint 13.0x121.5 cm (portrait)", text)

    def test_square_footprint_and_unknown_image_writes_the_raster_untouched(self):
        self.plank_decal._image = str(Path(self.tmp.name) / "gone.jpg")
        self.plank_decal._transform = _transform(along=50.0, across=50.0)
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        self.assertEqual(Path(self.plank_decal.imageFilename), self.cs_a2.resolve())
        self.assertIn("NOT ADJUSTED", text)

    def test_orientation_matching_can_be_switched_off(self):
        old = tp.DECAL_MATCH_AUTHORED_IMAGE_ORIENTATION
        tp.DECAL_MATCH_AUTHORED_IMAGE_ORIENTATION = False
        try:
            self._apply(self.cs_a1, self.cs_a2)
        finally:
            tp.DECAL_MATCH_AUTHORED_IMAGE_ORIENTATION = old
        self.assertEqual(Path(self.plank_decal.imageFilename), self.cs_a2.resolve())


class EmbeddedImagePickTests(unittest.TestCase):
    """Choosing a decal's image among the .f3d's embedded ones (sizes from the real models)."""

    @staticmethod
    def _decal(mag_x, mag_y):
        return types.SimpleNamespace(transform=_transform(along=mag_x, across=mag_y))

    def test_single_candidate_is_exact(self):
        size, orient, src = tp._pick_embedded_image(self._decal(1, 1), [(797, 144)])
        self.assertEqual((size, orient), ((797, 144), image_orient.LANDSCAPE))
        self.assertEqual(src, ".f3d embedded image")

    def test_square_nose_pair_is_told_apart_by_the_decals_footprint(self):
        pair = [(311, 676), (364, 676)]
        for w, h in pair:
            size, _o, src = tp._pick_embedded_image(self._decal(w / 100.0, h / 100.0), pair)
            self.assertEqual(size, (w, h))
            self.assertIn("proportions", src)

    def test_footprint_in_the_other_axis_order_still_identifies_the_image(self):
        pair = [(311, 676), (364, 676)]
        size, _o, _src = tp._pick_embedded_image(self._decal(6.76, 3.64), pair)
        self.assertEqual(size, (364, 676))

    def test_stretched_footprint_falls_back_to_orientation_only(self):
        # Designer stretched the decal: matches neither image -> no size is claimed.
        size, orient, src = tp._pick_embedded_image(
            self._decal(1.0, 1.0), [(311, 676), (364, 676)]
        )
        self.assertIsNone(size)
        self.assertEqual(orient, image_orient.PORTRAIT)
        self.assertIn("orientation only", src)

    def test_near_identical_candidates_are_interchangeable(self):
        size, orient, src = tp._pick_embedded_image(self._decal(1, 1), [(67, 496), (70, 497)])
        self.assertIn(size, [(67, 496), (70, 497)])
        self.assertEqual(orient, image_orient.PORTRAIT)
        self.assertIn("near-identical", src)

    def test_end_cap_trio_uses_the_footprint_when_it_matches(self):
        trio = [(145, 717), (70, 497), (67, 496)]
        size, _o, _src = tp._pick_embedded_image(self._decal(1.45, 7.17), trio)
        self.assertEqual(size, (145, 717))

    def test_mixed_orientations_without_a_match_are_unknown(self):
        size, orient, src = tp._pick_embedded_image(self._decal(1, 1), [(137, 786), (797, 144)])
        self.assertEqual((size, orient, src), (None, None, "unknown"))

    def test_unreadable_transform_never_crashes(self):
        size, orient, _src = tp._pick_embedded_image(object(), [(311, 676), (364, 676)])
        self.assertIsNone(size)
        self.assertEqual(orient, image_orient.PORTRAIT)


class EmbeddedImageFromModelFileTests(_Fixture):
    """The whole fallback chain when the decal's own image file is gone."""

    def _model(self, entries):
        import zipfile

        path = Path(self.tmp.name) / "Model.f3d"
        with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
            for name, (w, h) in entries:
                src = _png(Path(self.tmp.name) / "e.png", w, h)
                zf.writestr("FusionAssetName[Active]/Images.BlobParts/" + name, src.read_bytes())
            zf.writestr("FusionAssetName[Active]/Previews/big.png", _png(Path(self.tmp.name) / "p.png", 64, 64).read_bytes())
        return path

    def test_suffix_and_footprint_pick_the_designers_image(self):
        model = self._model([("Image.aaa.png", (31, 68)), ("Image.bbb.png", (36, 68)),
                             ("Image.ccc.jpg", (80, 14))])
        tp.set_decal_targets(tp._DECAL_TARGETS_BY_PART, model_path=model)
        self.plank_decal._image = "fec1d894-17f3-48c0-b3f6-f504eebf2563.png"  # bare name, as Fusion reports
        self.plank_decal._transform = _transform(along=0.36, across=0.68)
        size, orient, src = tp._authored_image_info(self.plank_decal, ("c-plank", 0))
        self.assertEqual(size, (36, 68), src)
        self.assertEqual(orient, image_orient.PORTRAIT)

    def test_bullnose_layout_resolves_each_decal_by_file_type(self):
        model = self._model([("Image.1de2.jpg", (137, 786)), ("Image.b73c.png", (797, 144))])
        tp.set_decal_targets(tp._DECAL_TARGETS_BY_PART, model_path=model)
        self.plank_decal._image = "fec1d894-17f3-48c0-b3f6-f504eebf2563.jpg"
        self.nose_decal._image = "f769c407-120e-4918-9b74-bbd6125105af.png"
        self.assertEqual(tp._authored_image_info(self.plank_decal, ("c-plank", 0))[0], (137, 786))
        self.assertEqual(tp._authored_image_info(self.nose_decal, ("c-nose", 0))[0], (797, 144))


class FootprintRuleTests(_Fixture):
    def test_bullnose_log_numbers_are_reproduced_exactly(self):
        """Same inputs as the approved 2026-10-14 Bullnose run -> same prepared rasters."""
        if not self._backend_available():
            self.skipTest("no image backend on this platform")
        t = Path(self.tmp.name)
        self.plank_decal._image = str(_png(t / "fec1d894.jpg.png", 137, 786))
        self.nose_decal._image = str(_png(t / "f769c407.png", 797, 144))
        cs1 = _png(t / "Color Set 04-1.png", 2500, 486)
        cs2 = _png(t / "Color Set 04-2.png", 1836, 424)
        total, lines = self._apply(cs1, cs2)
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        self.assertEqual(image_orient.image_size(Path(self.plank_decal.imageFilename)), (320, 1836), text)
        self.assertEqual(image_orient.image_size(Path(self.nose_decal.imageFilename)), (2500, 452), text)
        self.assertIn("rotated 90 + fitted to authored proportions (x0.75 across) -> 320x1836", text)
        self.assertIn("not rotated + fitted to authored proportions (x0.93 across) -> 2500x452", text)
        self.assertNotIn("other way", text)

    def test_footprint_wins_when_the_authored_image_runs_the_other_way(self):
        # Portrait authored image, but the decal covers a LANDSCAPE footprint.
        self.plank_decal._transform = _transform(along=121.5, across=13.0)
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        # Landscape raster on a landscape footprint: no turn, and no fitting to the
        # (wrong-way) authored proportions, so nothing to prepare at all.
        self.assertEqual(Path(self.plank_decal.imageFilename), self.cs_a2.resolve(), text)
        self.assertIn("ran the other way to the footprint", text)

    def test_footprint_rule_can_be_switched_off(self):
        if not self._backend_available():
            self.skipTest("no image backend on this platform")
        self.plank_decal._transform = _transform(along=121.5, across=13.0)
        old = tp.DECAL_ORIENT_BY_FOOTPRINT
        tp.DECAL_ORIENT_BY_FOOTPRINT = False
        try:
            self._apply(self.cs_a1, self.cs_a2)
        finally:
            tp.DECAL_ORIENT_BY_FOOTPRINT = old
        size = image_orient.image_size(Path(self.plank_decal.imageFilename))
        self.assertEqual(image_orient.orientation_of(size), image_orient.PORTRAIT)

    def test_probe_line_reports_bulb_and_centre(self):
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        probe = [l for l in lines if "Decal probe [Stair Nose#0]" in l]
        self.assertEqual(len(probe), 1, lines)
        self.assertIn("bulb=True", probe[0])
        self.assertIn("centre=(0.00,1.00,0.00)", probe[0])
        self.assertIn("size=123.744x22.592", probe[0])


class _OneToneFixture(unittest.TestCase):
    """3-in-1 style: three authored decals on the trim, plus a decal on another part.

    The real one-tone models carry 'Wentworth Verdon Hickory-2', '...-1' and
    'Grand Palais Oak-1'; which part each sits on is only visible inside Fusion.
    """

    def setUp(self):
        self._json_size_flag = tp.DECAL_APPLY_JSON_SIZE
        tp.DECAL_APPLY_JSON_SIZE = False  # this suite: designer sizes
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cs1 = _png(t / "Color Set 71-1.png", 100, 20)
        self.cs2 = _png(t / "Color Set 71-2.png", 90, 21)
        trim_skin = Body("Trim Skin", top_y=1.0)
        self.trim_decals = [
            Decal("Wentworth Verdon Hickory-2", _png(t / "a.png", 7, 50), _transform(along=7.6, across=127.0),
                  trim_skin._faces[0]),
            Decal("Wentworth Verdon Hickory-1", _png(t / "b.png", 7, 50), _transform(along=7.6, across=127.0),
                  trim_skin._faces[0]),
            Decal("Grand Palais Oak-1", _png(t / "c.png", 14, 100), _transform(along=7.6, across=127.0),
                  trim_skin._faces[0]),
        ]
        self.trim = Component("c-trim", "3-In-1", [trim_skin], Decals(list(self.trim_decals)))
        track_body = Body("Track", top_y=0.2)
        self.track_decal = Decal("Wentworth Verdon Hickory-2", _png(t / "d.png", 7, 50),
                                 _transform(y=0.2, along=7.6, across=127.0), track_body._faces[0])
        self.track = Component("c-track", "Track", [track_body], Decals([self.track_decal]))
        self.track_occ = Occurrence("Track:1", self.track)
        self.trim_occ = Occurrence("3-In-1:1", self.trim)
        self.design = Design([self.track_occ, self.trim_occ], root_name="3-in-1")
        spec = ri.parse_instructions(
            '[{"fileName":"3-in-1","decals":['
            '{"componentPartNumber":"3-In-1","textureSize":{"width":3,"length":50,"unit":"in"}}],'
            '"renderConfigurations":[{"namedView":"Close Up","hideBodies":[]}]}]'
        )
        tp.set_decal_targets(ri.decal_size_map(spec.models[0]))
        tp.pop_decal_swap_failures()
        image_orient.clear_cache()

    def tearDown(self):
        tp.DECAL_APPLY_JSON_SIZE = self._json_size_flag
        tp.set_decal_targets(None)
        tp.pop_decal_swap_failures()
        image_orient.clear_cache()
        self.tmp.cleanup()

    def _apply(self, s1=None, s2=None):
        return tp.apply_decal_color_set(self.design, s1 or self.cs1, s2 or self.cs2)


class OneToneLayoutTests(_OneToneFixture):
    def test_every_trim_decal_takes_the_one_tone_image(self):
        total, lines = self._apply()
        text = "\n".join(lines)
        self.assertEqual(total, 3, text)
        # "...-2" in a NAME must not pull the _2 raster onto a one-tone trim.
        self.assertEqual([d.name for d in self.trim_decals], ["Color Set 71-1"] * 3, text)
        self.assertEqual(tp.pop_decal_swap_failures(), [])

    def test_decal_on_an_unlisted_part_keeps_the_designers_image(self):
        before = self.track_decal.imageFilename
        _total, lines = self._apply()
        text = "\n".join(lines)
        self.assertEqual(self.track_decal.imageFilename, before, text)
        self.assertEqual(self.track_decal.writes, [], text)
        self.assertIn('(in "Track"): kept as authored', text)

    def test_skip_is_reported_once_per_model(self):
        self._apply()
        _total, lines = self._apply(_png(Path(self.tmp.name) / "Color Set 72-1.png", 120, 20),
                                    _png(Path(self.tmp.name) / "Color Set 72-2.png", 110, 30))
        self.assertFalse(any("kept as authored" in l for l in lines), lines)

    def test_without_render_instructions_the_name_fallback_still_applies(self):
        tp.set_decal_targets(None)
        self._apply()
        self.assertEqual(self.track_decal.name, "Color Set 71-2")  # legacy behaviour kept

    def test_switched_off_decal_is_left_alone(self):
        self.trim_decals[0].isLightBulbOn = False
        total, lines = self._apply()
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        self.assertEqual(self.trim_decals[0].writes, [], text)
        self.assertEqual(self.trim_decals[0].name, "Wentworth Verdon Hickory-2")
        self.assertIn("switched off in the model", text)
        self.assertEqual(tp.pop_decal_swap_failures(), [])

    def test_root_folder_decal_is_matched_through_its_face(self):
        # Designer kept the decal in the ROOT Decals folder, on a face of the trim.
        trim_occ = self.design.rootComponent.allOccurrences.item(1)
        face = Face(Body("Trim Skin", top_y=1.0), 1.0)
        face.body.parentComponent = self.trim
        face.assemblyContext = trim_occ
        root_decal = Decal("Grand Palais Oak-1", _png(Path(self.tmp.name) / "r.png", 14, 100),
                           _transform(along=7.6, across=127.0), face)
        self.design.rootComponent.decals = Decals([root_decal])
        _total, lines = self._apply()
        text = "\n".join(lines)
        self.assertEqual(root_decal.name, "Color Set 71-1", text)
        self.assertIn("via its face", text)

    def test_root_folder_decal_on_an_unlisted_part_is_kept(self):
        face = Face(Body("Track", top_y=0.2), 0.2)
        face.body.parentComponent = self.track
        face.assemblyContext = self.design.rootComponent.allOccurrences.item(0)
        root_decal = Decal("Wentworth Verdon Hickory-1", _png(Path(self.tmp.name) / "s.png", 7, 50),
                           _transform(y=0.2, along=7.6, across=127.0), face)
        self.design.rootComponent.decals = Decals([root_decal])
        self._apply()
        self.assertEqual(root_decal.writes, [])

    def test_switched_off_broken_decal_gets_no_visible_stand_in(self):
        hidden = self.trim_decals[2]
        hidden.isLightBulbOn = False
        hidden.broken = True
        total, lines = self._apply()
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        self.assertEqual(self.trim.decals.count, 3, "no stand-in for a hidden decal: " + text)
        self.assertEqual(tp.pop_decal_swap_failures(), [], text)


class RootFolderTests(_OneToneFixture):
    """Decals kept in the ROOT Decals folder (root named like the trim, as in 5 models)."""

    def _root_decal(self, name, face, broken=False):
        d = Decal(name, _png(Path(self.tmp.name) / (name + ".png"), 7, 50),
                  _transform(along=7.6, across=127.0), face, broken=broken)
        self.design.rootComponent.decals = Decals([d])
        return d

    def _face_on(self, comp, occ):
        face = Face(Body("x", top_y=1.0), 1.0)
        face.body.parentComponent = comp
        face.assemblyContext = occ
        return face

    def test_root_name_alone_never_selects_the_trim(self):
        d = self._root_decal("Wentworth Verdon Hickory-1", self._face_on(self.track, self.track_occ))
        _total, lines = self._apply()
        self.assertEqual(d.writes, [], "\n".join(lines))
        self.assertIn('(root Decals folder): kept as authored — "Track:1" is not listed', "\n".join(lines))

    def test_root_decal_on_a_root_body_is_kept(self):
        d = self._root_decal("Grand Palais Oak-1", self._face_on(self.design.rootComponent, None))
        _total, lines = self._apply()
        self.assertEqual(d.writes, [])
        self.assertIn("a body of the root component is not listed", "\n".join(lines))

    def test_root_decal_with_an_unreadable_face_is_a_failure_not_a_skip(self):
        d = self._root_decal("Grand Palais Oak-1", None, broken=True)
        _total, lines = self._apply()
        text = "\n".join(lines)
        self.assertEqual(d.writes, [], text)
        self.assertIn("cannot tell which part it textures", text)
        self.assertIn("Fix it in the model", text)
        self.assertEqual(len(tp.pop_decal_swap_failures()), 1, text)

    def test_root_decal_that_refuses_its_image_gets_no_stand_in(self):
        d = self._root_decal("Grand Palais Oak-1", self._face_on(self.trim, self.trim_occ))
        d.refuse_write = True  # routed to the trim by its face, but the write is refused
        _total, lines = self._apply()
        text = "\n".join(lines)
        self.assertEqual(self.design.rootComponent.decals.count, 1, "no stand-in: " + text)
        self.assertEqual(self.trim.decals.count, 3, "no stand-in in the trim either: " + text)
        self.assertTrue(d.isLightBulbOn, text)
        self.assertIn("no stand-in — it lives in the root Decals folder", text)
        self.assertEqual(len(tp.pop_decal_swap_failures()), 1, text)


class ThreeInOneLayoutTests(unittest.TestCase):
    """The real 3-in-1 layout (2026-10-14 log): the trim's only decal sits in the ROOT
    Decals folder (root "Untitled"), wrapping off, and its face reference is dead —
    it really covers ~8 faces (the top and both rounded edges) but reads as [None].
    The code cannot rebuild that, so it must report it plainly, never half-fix it.
    """

    def setUp(self):
        self._json_size_flag = tp.DECAL_APPLY_JSON_SIZE
        tp.DECAL_APPLY_JSON_SIZE = False  # this suite: designer sizes
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cs1 = _png(t / "Color Set 04-1.png", 100, 20)
        self.cs2 = _png(t / "Color Set 04-2.png", 90, 21)
        self.trim = Component("c-trim", "3-In-1", [Body("3-In-1", top_y=0.76)], Decals([]))
        self.design = Design([Occurrence("Track:1", Component("c-track", "Track", [Body("Track", 0.2)], Decals([]))),
                              Occurrence("3-In-1:1", self.trim)], root_name="Untitled")
        self.decal = Decal("Grand Palais Oak-1", _png(t / "gpo.png", 14, 100),
                           _transform(y=0.76, along=5.848, across=131.198), None, broken=True)
        self.decal.isChainFaces = False
        self.design.rootComponent.decals = Decals([self.decal])
        spec = ri.parse_instructions(
            '[{"fileName":"3-in-1","decals":['
            '{"componentPartNumber":"3-In-1","textureSize":{"width":3,"length":50,"unit":"in"}}],'
            '"renderConfigurations":[{"namedView":"Close Up","hideBodies":[]}]}]'
        )
        tp.set_decal_targets(ri.decal_size_map(spec.models[0]))
        tp.pop_decal_swap_failures()
        image_orient.clear_cache()

    def tearDown(self):
        tp.DECAL_APPLY_JSON_SIZE = self._json_size_flag
        tp.set_decal_targets(None)
        tp.pop_decal_swap_failures()
        image_orient.clear_cache()
        self.tmp.cleanup()

    def test_reported_as_a_model_fix_every_colour_set(self):
        for n in (1, 2):
            total, lines = tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
            text = "\n".join(lines)
            self.assertEqual(total, 0, text)
            self.assertIn("Fix it in the model", text)
            self.assertEqual(len(tp.pop_decal_swap_failures()), 1, "colour set {}: {}".format(n, text))
        self.assertTrue(self.decal.isLightBulbOn, "the designer's decal stays as it is")
        self.assertEqual(self.trim.decals.count, 0, "nothing half-made in the trim")
        self.assertEqual(self.design.rootComponent.decals.count, 1)


class DesignerStateTests(_OneToneFixture):
    def test_suppressed_decal_is_left_alone(self):
        self.trim_decals[1].timelineObject.isSuppressed = True
        self.trim_decals[1].broken = True  # would otherwise trigger a visible stand-in
        total, lines = self._apply()
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        self.assertEqual(self.trim.decals.count, 3, text)
        self.assertIn("suppressed in the timeline — left as is", text)
        self.assertEqual(tp.pop_decal_swap_failures(), [])

    def test_rolled_back_decal_is_left_alone(self):
        self.trim_decals[2].timelineObject.isRolledBack = True
        total, lines = self._apply()
        self.assertEqual(total, 2, "\n".join(lines))
        self.assertEqual(self.trim_decals[2].writes, [])

    def test_decals_folder_switched_off_is_left_alone(self):
        self.trim.isDecalFolderLightBulbOn = False
        total, lines = self._apply()
        text = "\n".join(lines)
        self.assertEqual(total, 0, text)
        self.assertIn("its Decals folder is switched off", text)

    def test_unreadable_states_count_as_active(self):
        class Boom:
            def __getattr__(self, name):
                raise RuntimeError("API error")
        self.trim_decals[0].timelineObject = Boom()
        total, _lines = self._apply()
        self.assertEqual(total, 3)


class StandInEdgeTests(_Fixture):
    NOSE_BROKEN = True

    def test_rollback_that_cannot_reshow_is_retried_not_called_switched_off(self):
        self.nose.decals.drift = 0.5          # stand-in rejected ...
        self.nose_decal.refuse_show = True    # ... and the original cannot be re-shown
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(len(tp.pop_decal_swap_failures()), 1)
        _total, lines = self._apply(self.cs_b1, self.cs_b2)
        text = "\n".join(lines)
        self.assertNotIn("switched off in the model", text)
        self.assertEqual(len(tp.pop_decal_swap_failures()), 1, text)

    def test_unremovable_stand_in_takes_over_and_is_flagged_every_time(self):
        self.nose.decals.drift = 0.5          # stand-in rejected ...
        self.nose.decals.refuse_delete = True  # ... and cannot be deleted
        _t, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(self.nose.decals.count, 2, text)
        self.assertFalse(self.nose_decal.isLightBulbOn, "original stays hidden")
        self.assertIn("unverified stand-in now carries this part", text)
        self.assertEqual(len(tp.pop_decal_swap_failures()), 1)
        total, lines = self._apply(self.cs_b1, self.cs_b2)
        text = "\n".join(lines)
        self.assertEqual(self.nose.decals.count, 2, "no third decal: " + text)
        self.assertEqual(self.nose.decals.item(1).name, "Color Set 72-1", text)
        self.assertIn("failed verification in an earlier colour set", text)
        self.assertEqual(len(tp.pop_decal_swap_failures()), 1, text)
        self.assertEqual(total, 2, text)

    def test_dead_decal_without_wrapping_gets_no_partial_stand_in(self):
        # A dead reference reads as [None] whatever the designer picked, so the
        # faces it covered are unknown; a stand-in could cover only one of them.
        self.nose_decal.isChainFaces = False
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 1, text)
        self.assertEqual(self.nose.decals.count, 1, text)
        self.assertTrue(self.nose_decal.isLightBulbOn, text)
        self.assertIn("faces it was applied to cannot be read back", text)
        self.assertEqual(len(tp.pop_decal_swap_failures()), 1)

    def test_single_readable_face_without_wrapping_is_reproduced(self):
        # Write refused although its one face reads fine: that face is known.
        self.nose_decal.broken = False
        self.nose_decal.refuse_write = True
        self.nose_decal.isChainFaces = False
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        self.assertFalse(self.nose.decals.item(1).isChainFaces, text)

    def test_several_readable_faces_without_wrapping_get_no_partial_stand_in(self):
        self.nose_decal.broken = False
        self.nose_decal.refuse_write = True
        self.nose_decal.isChainFaces = False
        self.nose_decal.n_faces = 7
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 1, text)
        self.assertEqual(self.nose.decals.count, 1, text)
        self.assertIn("covers 7 chosen faces", text)


class PlacementCheckTests(_Fixture):
    """3-in-1's re-made decal read back with its centre 6 cm off its own surface."""

    def test_healthy_decals_pass_the_check(self):
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertNotIn("not on its own surface", text)
        self.assertEqual(tp.pop_decal_swap_failures(), [])
        probe = [l for l in lines if "Decal probe [Supplemental Plank#0]" in l]
        self.assertIn("centreOnFace=True", probe[0])

    def test_decal_centred_off_its_surface_is_flagged_but_still_written(self):
        self.plank_decal._transform = _transform(y=6.0, **PLANK_FOOTPRINT)  # 5 cm off the face
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        self.assertIn("its centre is not on its own surface", text)
        self.assertIn("Z Angle 0", text)
        fails = tp.pop_decal_swap_failures()
        self.assertEqual(len(fails), 1, text)
        self.assertIn("placed off its surface", fails[0])
        probe = [l for l in lines if "Decal probe [Supplemental Plank#0]" in l]
        self.assertIn("centreOnFace=False", probe[0])

    def test_dead_reference_that_iterates_as_an_empty_wrapper_is_not_misplaced(self):
        # Real Fusion on Bullnose's nose: faces[0] is None, but iterating the list
        # yields a wrapper whose calls raise. That must read as "cannot tell".
        class _NullFace:
            def __getattr__(self, name):
                raise RuntimeError("null face")

        class _OddList(list):
            def __iter__(self):
                return iter([_NullFace()])

        class _NoseFacesFake(Decal):
            @property
            def faces(self):
                return _OddList([None])

        self.nose_decal.__class__ = _NoseFacesFake
        self.nose_decal.broken = True
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertNotIn("not on its own surface", text)
        self.assertEqual([f for f in tp.pop_decal_swap_failures() if "off its surface" in f], [], text)
        probe = [l for l in lines if "Decal probe [Stair Nose#0]" in l]
        self.assertIn("centreOnFace=None", probe[0])

    def test_dead_reference_is_not_reported_as_misplaced(self):
        self.nose_decal.broken = True
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertNotIn("not on its own surface", text)
        probe = [l for l in lines if "Decal probe [Stair Nose#0]" in l]
        self.assertIn("centreOnFace=None", probe[0])


class NearSquareFootprintTests(_Fixture):
    def test_barely_rectangular_footprint_does_not_overrule_a_portrait_image(self):
        if not self._backend_available():
            self.skipTest("no image backend on this platform")
        self.plank_decal._transform = _transform(along=100.0, across=97.0)
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        size = image_orient.image_size(Path(self.plank_decal.imageFilename))
        self.assertEqual(image_orient.orientation_of(size), image_orient.PORTRAIT, text)
        self.assertNotIn("other way", text)


def _mags(decal):
    _o, x, y, _z = decal.transform.getAsCoordinateSystem()
    return (round(tp._vector_length(x), 3), round(tp._vector_length(y), 3))


class _JsonSizeBullnoseFixture(_Fixture):
    """Deliverable 2 on a Bullnose-shaped model (real footprints, real JSON sizes)."""

    def setUp(self):
        super().setUp()
        tp.DECAL_APPLY_JSON_SIZE = True
        # Plank decal: X across (13.04), Y along world z (121.5); its skin face spans 121.92 cm.
        self.plank.bRepBodies.item(0)._faces[0]._box = ((-6.6, 1.0, -60.96), (6.6, 1.0, 60.96))
        # Nose decal: X along world x (123.7), Y across (22.6).
        self.nose_skin._faces[0]._box = ((-60.96, 1.0, -11.5), (60.96, 1.0, 11.5))
        tp.pop_decal_size_warnings()

    def tearDown(self):
        tp.pop_decal_size_warnings()
        super().tearDown()


class JsonSizeBullnoseTests(_JsonSizeBullnoseFixture):
    def test_both_decals_get_the_render_instructions_size(self):
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        self.assertEqual(_mags(self.plank_decal), (17.78, 127.0), text)   # 7 x 50 in
        self.assertEqual(_mags(self.nose_decal), (127.0, 22.86), text)    # 50 x 9 in
        self.assertIn("size set from the Render Instructions: 17.8 x 127.0 cm", text)
        self.assertIn("designer's 13.0 x 121.5 cm", text)
        self.assertEqual(tp.pop_decal_size_warnings(), [], text)
        self.assertEqual(_TIMELINE.markerPosition, _TIMELINE.count)

    def test_centre_and_directions_are_the_designers(self):
        self._apply(self.cs_a1, self.cs_a2)
        o, x, y, _z = self.plank_decal.transform.getAsCoordinateSystem()
        self.assertEqual((o.x, o.y, o.z), (0.0, 1.0, 0.0))
        self.assertEqual((x.y, x.z, y.x, y.y), (0.0, 0.0, 0.0, 0.0))
        self.assertGreater(x.x, 0)
        self.assertGreater(y.z, 0)

    def test_images_are_still_swapped_and_fitted_over_the_new_footprint(self):
        if not self._backend_available():
            self.skipTest("no image backend on this platform")
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(self.plank_decal.name, "Color Set 71-2")
        self.assertIn("footprint 17.8x127.0 cm (portrait)", text)
        self.assertIn("footprint 127.0x22.9 cm (landscape)", text)

    def test_sizes_are_applied_once_per_model(self):
        self._apply(self.cs_a1, self.cs_a2)
        self._apply(self.cs_b1, self.cs_b2)
        self.assertEqual(len(self.plank_decal.redefines), 1)
        self.assertEqual(len(self.nose_decal.redefines), 1)

    def test_refused_redefine_keeps_the_designer_size_and_says_so(self):
        self.plank_decal.refuse_redefine = True
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        self.assertEqual(_mags(self.plank_decal), (13.041, 121.518))
        warnings = tp.pop_decal_size_warnings()
        self.assertEqual(len(warnings), 1, text)
        self.assertIn("Fusion refused the new size", warnings[0])
        self.assertEqual(_TIMELINE.markerPosition, _TIMELINE.count)

    def test_failing_redefine_still_returns_the_timeline_marker(self):
        self.plank_decal.refuse_redefine = "raise"
        total, _lines = self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(total, 2)
        self.assertIn("redefine failed", tp.pop_decal_size_warnings()[0])
        self.assertEqual(_TIMELINE.markerPosition, _TIMELINE.count)
        self.assertEqual(_TIMELINE.rolled_to, None)

    def test_switch_off_keeps_every_designer_size(self):
        tp.DECAL_APPLY_JSON_SIZE = False
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(self.plank_decal.redefines, [])
        self.assertEqual(_mags(self.plank_decal), (13.041, 121.518))

    def test_switched_off_decal_is_not_resized(self):
        self.plank_decal.isLightBulbOn = False
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(self.plank_decal.redefines, [])

    def test_broken_nose_gets_a_stand_in_at_the_render_instructions_size(self):
        self.nose_decal.broken = True
        total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(total, 2, text)
        self.assertEqual(self.nose_decal.redefines, [], "a dead decal cannot be redefined")
        self.assertIn("goes to its stand-in", text)
        stand_in = self.nose.decals.item(1)
        self.assertEqual(_mags(stand_in), (127.0, 22.86), text)
        self.assertIn("sized from the Render Instructions: 127.0 x 22.9 cm", text)
        self.assertFalse(self.nose_decal.isLightBulbOn)
        self.assertEqual(tp.pop_decal_swap_failures(), [], text)


class JsonSizeTimelineAndFrameTests(_JsonSizeBullnoseFixture):
    """Fixes from the 2026-10-15 Fusion run on 3-in-1."""

    def test_marker_goes_back_where_the_model_had_it_not_to_the_end(self):
        # One-tone models are saved with the marker part-way; an old stair scene
        # after it must stay off (moveToEnd brought it into the renders).
        _TIMELINE.markerPosition = 7
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(_TIMELINE.markerPosition, 7)
        self.assertEqual(_mags(self.plank_decal), (17.78, 127.0))

    def test_marker_goes_back_even_when_redefine_fails(self):
        _TIMELINE.markerPosition = 7
        self.plank_decal.refuse_redefine = "raise"
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(_TIMELINE.markerPosition, 7)

    def test_frame_that_is_not_square_is_squared_and_accepted(self):
        # Fusion read back a slightly skewed frame for the re-made 3-in-1 decal;
        # handing it to redefine as-is was refused.
        self.plank_decal._transform = Matrix(
            Point(0, 1, 0), Vec(13.041, 0, 0), Vec(0, 0.4, 121.518), Vec(0, 1, 0)
        )
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(tp.pop_decal_size_warnings(), [], text)
        _o, x, y, z = self.plank_decal.transform.getAsCoordinateSystem()
        self.assertAlmostEqual(y.x * z.x + y.y * z.y + y.z * z.z, 0.0, places=6)
        self.assertEqual(_mags(self.plank_decal), (17.78, 127.0))
        self.assertGreater(y.z, 0, "the designer's direction is kept")

    def test_mirrored_decal_stays_mirrored(self):
        self.plank_decal._transform = Matrix(
            Point(0, 1, 0), Vec(13.041, 0, 0), Vec(0, 0, -121.518), Vec(0, 1, 0)
        )
        self._apply(self.cs_a1, self.cs_a2)
        _o, x, y, _z = self.plank_decal.transform.getAsCoordinateSystem()
        self.assertGreater(x.x, 0)
        self.assertLess(y.z, 0, "a flipped decal must stay flipped")


class JsonSizeHardeningTests(_JsonSizeBullnoseFixture):
    """Second review round: every way a resize can go wrong leaves the model sound."""

    def test_marker_put_back_even_when_the_roll_itself_raises(self):
        _TIMELINE.markerPosition = 7
        self.plank_decal.timelineObject.raise_after_roll = True
        total, _lines = self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(total, 2)
        self.assertEqual(_TIMELINE.markerPosition, 7)
        self.assertEqual(_mags(self.plank_decal), (13.041, 121.518))
        self.assertTrue(any("compute failed after roll" in w for w in tp.pop_decal_size_warnings()))

    def test_marker_that_cannot_be_put_back_stops_sizing_and_is_a_failure(self):
        _TIMELINE.markerPosition = 7
        _TIMELINE.stuck = True
        self._apply(self.cs_a1, self.cs_a2)
        fails = tp.pop_decal_swap_failures()
        self.assertTrue(any("timeline marker could not be put back" in f for f in fails), fails)
        resized = len(self.plank_decal.redefines) + len(self.nose_decal.redefines)
        self.assertEqual(resized, 1, "sizing stops after the first stuck marker")

    def test_model_with_uncaptured_positions_is_not_resized(self):
        self.design.snapshots.hasPendingSnapshot = True
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(self.plank_decal.redefines, [])
        self.assertTrue(any("moved without a captured position" in w for w in tp.pop_decal_size_warnings()))

    def test_resize_fusion_got_wrong_is_undone(self):
        self.plank_decal.redefine_distort = True
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(_mags(self.plank_decal), (13.041, 121.518), text)
        self.assertEqual(len(self.plank_decal.redefines), 2, "resize + undo")
        self.assertTrue(any("the designer's size was kept" in w for w in tp.pop_decal_size_warnings()), text)
        self.assertEqual(_TIMELINE.markerPosition, _TIMELINE.count)

    def test_unhealthy_decal_after_resize_is_reported(self):
        self.plank_decal.timelineObject.healthState = 1
        self._apply(self.cs_a1, self.cs_a2)
        self.assertTrue(any("not healthy" in w for w in tp.pop_decal_size_warnings()))

    def test_stand_in_size_that_never_got_used_is_reported(self):
        self.nose_decal.broken = True
        old = tp.DECAL_REPLACE_UNWRITABLE_AUTHORED
        tp.DECAL_REPLACE_UNWRITABLE_AUTHORED = False
        try:
            _total, lines = self._apply(self.cs_a1, self.cs_a2)
        finally:
            tp.DECAL_REPLACE_UNWRITABLE_AUTHORED = old
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(any("no stand-in was made" in w for w in warnings), "\n".join(lines))

    def test_decals_are_fetched_again_after_every_roll(self):
        real = tp._collect_decals_with_owner
        calls = []

        def counting(design):
            calls.append(1)
            return real(design)

        tp._collect_decals_with_owner = counting
        try:
            tp.apply_json_decal_sizes(self.design)
        finally:
            tp._collect_decals_with_owner = real
        self.assertEqual(_mags(self.plank_decal), (17.78, 127.0))
        self.assertEqual(_mags(self.nose_decal), (127.0, 22.86))
        self.assertGreaterEqual(len(calls), 3, "initial collection + one after each roll")

    def test_controller_step_runs_once_and_the_colour_set_does_not_repeat_it(self):
        lines = tp.apply_json_decal_sizes(self.design)
        self.assertTrue(any("size set from the Render Instructions" in l for l in lines))
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(len(self.plank_decal.redefines), 1)

    def test_unmeasurable_part_and_shorter_decal_is_reported(self):
        self.plank.bRepBodies.item(0)._faces[0]._box = None
        self.plank_decal._transform = _transform(along=13.041, across=200.0)  # longer than JSON 127
        self._apply(self.cs_a1, self.cs_a2)
        self.assertTrue(any("could not be measured and the decal gets shorter" in w
                            for w in tp.pop_decal_size_warnings()))


class JsonSizeSolidBodyTests(_JsonSizeBullnoseFixture):
    """A wrapping decal on a full solid must not run onto the cut ends (Square Nose plank)."""

    def setUp(self):
        super().setUp()
        self._audit = tp.audit_decal_body_is_show_only
        tp.audit_decal_body_is_show_only = lambda body: (False, "full solid with cut-end faces")

    def tearDown(self):
        tp.audit_decal_body_is_show_only = self._audit
        super().tearDown()

    def test_length_is_held_to_the_part_on_a_solid_body(self):
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(_mags(self.plank_decal), (17.78, 121.92), text)  # part, not 127
        self.assertIn("length held to the part's 121.9 cm (Render Instructions: 127.0 cm)", text)

    def test_wider_decal_on_a_solid_body_is_reported(self):
        self._apply(self.cs_a1, self.cs_a2)
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(any("wider than the designer's decal (13.0 cm)" in w for w in warnings), warnings)

    def test_show_only_skin_keeps_the_full_json_length(self):
        tp.audit_decal_body_is_show_only = self._audit
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(_mags(self.plank_decal), (17.78, 127.0))


class JsonSizeSmallGuardTests(_JsonSizeBullnoseFixture):
    def test_derived_decal_is_not_resized(self):
        self.plank_decal.isDerived = True
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(self.plank_decal.redefines, [])
        self.assertTrue(any("derived from another design" in w for w in tp.pop_decal_size_warnings()))

    def test_designer_opacity_survives_the_resize(self):
        self.plank_decal.opacity = 0.8
        original = Decal.redefine

        def resetting(decal, matrix, faces, chain):
            ok = original(decal, matrix, faces, chain)
            decal.opacity = 1.0  # a Fusion build that resets it
            return ok

        Decal.redefine = resetting
        try:
            self._apply(self.cs_a1, self.cs_a2)
        finally:
            Decal.redefine = original
        self.assertEqual(self.plank_decal.opacity, 0.8)


def _rotated_about_y(matrix, degrees):
    """``matrix`` with X and Z turned about world Y (the fakes' face normal)."""
    import math

    o, x, y, z = matrix.getAsCoordinateSystem()
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))

    def turn(v):
        return Vec(c * v.x + s * v.z, v.y, -s * v.x + c * v.z)

    return Matrix(o, turn(x), turn(y), z)


class JsonSizeFusionRun3Tests(_JsonSizeBullnoseFixture):
    """The third Fusion run (all 8 models, 2026-10-15): 7 of 9 resizes refused or undone."""

    def test_centre_a_hair_off_its_face_is_put_exactly_on_it(self):
        # Bullnose and Square Nose planks: "Input point is not located on primary face".
        self.plank_decal._transform = Matrix(
            Point(0, 1.005, 0), Vec(13.041, 0, 0), Vec(0, 0, 121.518), Vec(0, 1, 0)
        )
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(_mags(self.plank_decal), (17.78, 127.0), text)
        o, _x, _y, _z = self.plank_decal.transform.getAsCoordinateSystem()
        self.assertEqual(o.y, 1.0)
        self.assertIn("centre put exactly on its face (it read 0.005 cm off)", text)
        self.assertEqual(tp.pop_decal_size_warnings(), [], text)

    def test_centre_far_off_its_face_is_not_moved_onto_it(self):
        self.plank_decal._transform = Matrix(
            Point(0, 1.5, 0), Vec(13.041, 0, 0), Vec(0, 0, 121.518), Vec(0, 1, 0)
        )
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertIn("its centre is 0.50 cm from its nearest face", text)
        self.assertEqual(_mags(self.plank_decal), (13.041, 121.518), "Fusion refuses; designer kept")
        self.assertTrue(any("primary face" in w for w in tp.pop_decal_size_warnings()))

    def test_face_that_sits_apart_while_rolled_back_gets_one_retry(self):
        rolled = Face(self.plank.bRepBodies.item(0), 1.02, box=((-6.6, 1.02, -60.96), (6.6, 1.02, 60.96)))
        self.plank_decal.rolled_faces = [rolled]
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(len(self.plank_decal.redefines), 2, "refused, then retried on the face")
        self.assertEqual(self.plank_decal.redefines[1][0].origin.y, 1.02)
        self.assertEqual(_mags(self.plank_decal), (17.78, 127.0), text)

    def test_face_far_apart_while_rolled_back_is_reported_with_the_distance(self):
        rolled = Face(self.plank.bRepBodies.item(0), 3.0, box=((-6.6, 3.0, -60.96), (6.6, 3.0, 60.96)))
        self.plank_decal.rolled_faces = [rolled]
        self._apply(self.cs_a1, self.cs_a2)
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(
            any("at the decal's step in the timeline its centre is 2.00 cm from that face" in w
                for w in warnings),
            warnings,
        )
        self.assertEqual(_TIMELINE.markerPosition, _TIMELINE.count)

    def test_frame_stored_slightly_off_square_is_kept(self):
        # L-Cap, Reducer, Quarter Round: exact size and centre, axes "turned".
        def tilt_out_of_surface(m):
            o, x, y, z = m.getAsCoordinateSystem()
            return Matrix(o, x, Vec(y.x, y.y + 0.05 * y.z, y.z), z)  # ~3 deg off the surface

        self.plank_decal.store_as = tilt_out_of_surface
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(len(self.plank_decal.redefines), 1, "no undo")
        self.assertEqual(tp.pop_decal_size_warnings(), [], text)

    def test_reversed_axis_is_kept_and_logged(self):
        def reverse_y(m):
            o, x, y, z = m.getAsCoordinateSystem()
            return Matrix(o, x, Vec(-y.x, -y.y, -y.z), z)

        self.plank_decal.store_as = reverse_y
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(len(self.plank_decal.redefines), 1, "no undo")
        self.assertIn("Fusion stored it with its Y axis reversed", text)
        self.assertEqual(tp.pop_decal_size_warnings(), [], text)

    def test_small_turn_on_the_surface_is_kept_and_logged(self):
        self.plank_decal.store_as = lambda m: _rotated_about_y(m, 2.0)
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(len(self.plank_decal.redefines), 1, "no undo")
        self.assertIn("its length turned 2.0 deg on the surface", text)

    def test_centre_a_millimetre_off_after_the_resize_is_logged_not_warned(self):
        # Bullnose plank, 4th Fusion run: Fusion put the centre 0.136 cm across.
        def shift_across(m):
            o, x, y, z = m.getAsCoordinateSystem()
            return Matrix(Point(o.x + 0.136, o.y, o.z), x, y, z)

        self.plank_decal.store_as = shift_across
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(len(self.plank_decal.redefines), 1, "no undo")
        self.assertIn("its centre 0.14 cm from where it was asked", text)
        self.assertEqual(tp.pop_decal_size_warnings(), [], text)

    def test_sideways_centre_shift_under_a_centimetre_is_kept(self):
        # Square Nose plank, 4-model run: exact size, centre 0.59 cm across -> undone,
        # and the undo landed 0.59 cm across as well.
        def shift_across(m):
            o, x, y, z = m.getAsCoordinateSystem()
            return Matrix(Point(o.x + 0.59, o.y, o.z), x, y, z)  # plank length runs along z

        self.plank_decal.store_as = shift_across
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(len(self.plank_decal.redefines), 1, "no undo")
        self.assertIn("its centre 0.59 cm from where it was asked (0.00 along the part, 0.59 across)", text)
        self.assertEqual(tp.pop_decal_size_warnings(), [], text)

    def test_sideways_centre_shift_over_a_centimetre_is_undone(self):
        def shift_across(m):
            o, x, y, z = m.getAsCoordinateSystem()
            return Matrix(Point(o.x + 1.5, o.y, o.z), x, y, z)

        self.plank_decal.store_as = shift_across
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(len(self.plank_decal.redefines), 2, "resize + undo")
        self.assertEqual(_mags(self.plank_decal), (13.041, 121.518))

    def test_centre_shift_along_the_part_is_undone(self):
        def shift_along(m):
            o, x, y, z = m.getAsCoordinateSystem()
            return Matrix(Point(o.x, o.y, o.z + 0.5), x, y, z)

        self.plank_decal.store_as = shift_along
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(len(self.plank_decal.redefines), 2, "resize + undo")
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(any("0.50 along the part, 0.00 across" in w for w in warnings), warnings)

    def test_turn_just_under_the_limit_is_kept(self):
        self.plank_decal.store_as = lambda m: _rotated_about_y(m, 4.0)
        self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(len(self.plank_decal.redefines), 1, "4 deg is within the 5 deg limit")

    def test_large_tilt_off_the_surface_alone_is_kept_and_logged(self):
        def tilt_length(m):
            import math

            o, x, y, z = m.getAsCoordinateSystem()
            t = math.radians(8.0)
            n = (y.x ** 2 + y.y ** 2 + y.z ** 2) ** 0.5  # length axis is Y on the plank
            return Matrix(o, x, Vec(0, n * math.sin(t), n * math.cos(t)), z)

        self.plank_decal.store_as = tilt_length
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(len(self.plank_decal.redefines), 1, text)
        self.assertIn("its length tilted 8.0 deg off the surface", text)

    def test_skewed_designer_frame_keeps_the_length_direction(self):
        # Squaring around X would turn a Y-long decal's grain by the skew.
        self.plank_decal._transform = Matrix(
            Point(0, 1, 0), Vec(13.041, 0, 0.68), Vec(0, 0, 121.518), Vec(0, 1, 0)
        )
        self._apply(self.cs_a1, self.cs_a2)
        _o, x, y, _z = self.plank_decal.transform.getAsCoordinateSystem()
        self.assertEqual((y.x, y.y), (0.0, 0.0))
        self.assertAlmostEqual(y.z, 127.0, places=9)
        self.assertAlmostEqual(x.x * y.x + x.y * y.y + x.z * y.z, 0.0, places=9)

    def test_large_turn_on_the_surface_is_undone(self):
        self.plank_decal.store_as = lambda m: _rotated_about_y(m, 10.0)
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        text = "\n".join(lines)
        self.assertEqual(len(self.plank_decal.redefines), 2, "resize + undo")
        self.assertEqual(_mags(self.plank_decal), (13.041, 121.518), text)
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(any("length turned 10.0 deg on the surface" in w and "designer's size was kept" in w
                            for w in warnings), warnings)

    def test_undo_squares_a_designer_frame_redefine_will_not_take(self):
        # Quarter Round: "putting the designer's size back failed (3 : X and Z axes
        # defined by the matrix, must be perpendicular to one another)".
        self.plank_decal._transform = Matrix(
            Point(0, 1, 0), Vec(13.041, 0.3, 0), Vec(0, 0, 121.518), Vec(0, 1, 0)
        )
        self.plank_decal.redefine_distort = True
        _total, lines = self._apply(self.cs_a1, self.cs_a2)
        self.assertEqual(len(self.plank_decal.redefines), 3, "\n".join(lines))
        self.assertAlmostEqual(_vector_len(self.plank_decal.transform.x), (13.041 ** 2 + 0.3 ** 2) ** 0.5, places=6)
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(any("the designer's size was kept" in w for w in warnings), warnings)
        self.assertFalse(any("putting the designer's size back failed" in w for w in warnings), warnings)


def _vector_len(v):
    return (v.x * v.x + v.y * v.y + v.z * v.z) ** 0.5


class ModelPathsBlobTests(unittest.TestCase):
    """The panel's model-files box: only one of two picked models was rendered."""

    A = r"C:\Users\L.C\Fusion360Model\Resources\Models\3-in-1.f3d"
    B = r"C:\Users\L.C\Fusion360Model\Resources\Models\Bullnose.f3d"

    def _names(self, text):
        import batch_config
        return [p.name for p in batch_config.parse_model_paths_blob(text)]

    def test_every_way_the_text_box_can_hand_two_paths_back(self):
        both = ["3-in-1.f3d", "Bullnose.f3d"]
        for text in (self.A + " |\n" + self.B, self.A + "\n" + self.B, self.A + self.B,
                     self.A + "<br>" + self.B, "<p>" + self.A + "</p><p>" + self.B + "</p>",
                     self.A + " | " + self.B, self.A + ";" + self.B):
            self.assertEqual(self._names(text), both, text)

    def test_single_path_and_empty(self):
        self.assertEqual(self._names(self.A), ["3-in-1.f3d"])
        self.assertEqual(self._names("  "), [])

    def test_f3d_inside_a_folder_name_is_not_a_split_point(self):
        self.assertEqual(self._names(r"C:\x.f3d files\a.f3d"), ["a.f3d"])


class JsonSizeTrimTests(unittest.TestCase):
    """One-tone trims: 3-in-1 (centre at the trim's start), L-Cap, Reducer shapes."""

    def setUp(self):
        self._flag = tp.DECAL_APPLY_JSON_SIZE
        tp.DECAL_APPLY_JSON_SIZE = True
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cs1 = _png(t / "Color Set 04-1.png", 100, 20)
        self.cs2 = _png(t / "Color Set 04-2.png", 90, 21)
        tp.pop_decal_size_warnings()
        tp.pop_decal_swap_failures()
        image_orient.clear_cache()

    def tearDown(self):
        tp.DECAL_APPLY_JSON_SIZE = self._flag
        tp.set_decal_targets(None)
        tp.pop_decal_size_warnings()
        tp.pop_decal_swap_failures()
        image_orient.clear_cache()
        self.tmp.cleanup()

    def _trim(self, part, width_in, origin_x, along, across, part_x=(0.0, 121.92), faces=None,
              length_in=50):
        body = Body(part, top_y=0.76)
        body._faces[0]._box = ((part_x[0], 0.76, -2.9), (part_x[1], 0.76, 2.9))
        if faces:
            body._faces = faces
            for f in faces:
                f.body = body
        decal = Decal("Grand Palais Oak-1", _png(Path(self.tmp.name) / "g.png", 100, 14),
                      Matrix(Point(origin_x, 0.76, 0), Vec(along, 0, 0), Vec(0, 0, across), Vec(0, 1, 0)),
                      body._faces[0])
        decal.isChainFaces = False
        comp = Component("c-" + part, part, [body], Decals([decal]))
        self.design = Design([Occurrence(part + ":1", comp)], root_name="Untitled")
        spec = ri.parse_instructions(
            '[{"fileName":"m","decals":[{"componentPartNumber":"%s","textureSize":'
            '{"width":%s,"length":%s,"unit":"in"}}],"renderConfigurations":'
            '[{"namedView":"Close Up","hideBodies":[]}]}]' % (part, width_in, length_in)
        )
        tp.set_decal_targets(ri.decal_size_map(spec.models[0]))
        return decal

    def test_3in1_decal_reopened_at_the_trim_start_is_centred_and_sized(self):
        decal = self._trim("3-In-1", 3, origin_x=0.03, along=254.0, across=7.62)
        total, lines = tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        text = "\n".join(lines)
        self.assertEqual(total, 1, text)
        o, _x, _y, _z = decal.transform.getAsCoordinateSystem()
        self.assertAlmostEqual(o.x, 60.96, places=2)
        self.assertEqual(_mags(decal), (127.0, 7.62))
        self.assertIn("centred on the part (moved +60.9 cm along its length)", text)
        self.assertEqual(tp.pop_decal_size_warnings(), [], text)

    def test_lcap_json_width_narrower_than_the_designer_is_reported(self):
        decal = self._trim("L-Cap", 3, origin_x=60.97, along=131.379, across=8.986)
        _total, lines = tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        self.assertEqual(_mags(decal), (127.0, 7.62))
        warnings = tp.pop_decal_size_warnings()
        self.assertEqual(len(warnings), 1, "\n".join(lines))
        self.assertIn("narrower than the designer's decal (9.0 cm)", warnings[0])

    def test_part_longer_than_the_json_length_is_reported(self):
        decal = self._trim("Reducer", 3, origin_x=89.7, along=179.419, across=5.537,
                           part_x=(0.0, 179.4))
        tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        self.assertEqual(_mags(decal), (127.0, 7.62))
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(any("about 52.4 cm at its ends stays bare" in w for w in warnings), warnings)

    def test_centre_moved_onto_another_listed_face_makes_it_the_primary_face(self):
        # T-Molding (42 faces split along its length), 2026-10-15: the moved centre
        # sat on a neighbour of the first face and Fusion refused it.
        first = Face(None, 0.76, box=((0.0, 0.76, -2.9), (30.0, 0.76, 2.9)))
        second = Face(None, 0.76, box=((30.0, 0.76, -2.9), (121.92, 0.76, 2.9)))
        decal = self._trim("3-In-1", 3, origin_x=0.03, along=254.0, across=7.62,
                           faces=[first, second])
        decal.faces_override = [first, second]
        _total, lines = tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        text = "\n".join(lines)
        o, _x, _y, _z = decal.transform.getAsCoordinateSystem()
        self.assertAlmostEqual(o.x, 60.96, places=2)
        self.assertEqual(_mags(decal), (127.0, 7.62))
        self.assertIs(decal.redefines[0][1][0], second, "the face under the centre goes first")
        self.assertEqual(len(decal.redefines[0][1]), 2, "every listed face is kept")
        self.assertEqual(tp.pop_decal_size_warnings(), [], text)

    def test_refused_moved_centre_is_sized_at_the_designers_centre(self):
        # T-Molding, 4-model run: Fusion refused the centre moved -3.2 cm to the
        # middle of the part, so the Render Instructions size was never applied.
        decal = self._trim("T-Molding", 2.5, origin_x=64.19, along=128.884, across=5.961)
        decal.refuse_centre = lambda o: abs(o.x - 64.19) > 0.01
        _total, lines = tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        text = "\n".join(lines)
        o, _x, _y, _z = decal.transform.getAsCoordinateSystem()
        self.assertAlmostEqual(o.x, 64.19, places=6)
        self.assertEqual(_mags(decal), (127.0, 6.35), text)
        self.assertIn("so it is sized at the designer's centre instead", text)
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(any("could not be centred on it, so about 0.7 cm at that end stays bare" in w
                            for w in warnings), warnings)
        self.assertFalse(any("size not applied" in w for w in warnings), warnings)
        self.assertEqual(_TIMELINE.markerPosition, _TIMELINE.count)

    def test_faces_split_after_the_decals_step_are_used_as_they_are_at_that_step(self):
        # T-Molding, 2-model run: even the designer's own centre was refused. Its
        # decal sits at step 5 and later steps re-split the faces, so the finished
        # model's face objects (here two) do not exist where redefine runs (one).
        first = Face(None, 0.76, box=((0.0, 0.76, -2.9), (30.0, 0.76, 2.9)))
        second = Face(None, 0.76, box=((30.0, 0.76, -2.9), (121.92, 0.76, 2.9)))
        first.stale_when_rolled = second.stale_when_rolled = True
        whole = Face(None, 0.76, box=((0.0, 0.76, -2.9), (121.92, 0.76, 2.9)))
        decal = self._trim("T-Molding", 2.5, origin_x=64.19, along=128.884, across=5.961,
                           faces=[first, second])
        decal.faces_override = [first, second]
        decal.rolled_faces = [whole]
        _total, lines = tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        text = "\n".join(lines)
        o, _x, _y, _z = decal.transform.getAsCoordinateSystem()
        self.assertAlmostEqual(o.x, 60.96, places=2)
        self.assertEqual(_mags(decal), (127.0, 6.35), text)
        self.assertEqual([f for f in decal.redefines[0][1]], [whole])
        self.assertEqual(tp.pop_decal_size_warnings(), [], text)

    def test_refusal_reports_the_faces_at_the_decals_step(self):
        decal = self._trim("T-Molding", 2.5, origin_x=64.19, along=128.884, across=5.961)
        decal.refuse_centre = lambda o: True
        tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        warnings = tp.pop_decal_size_warnings()
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("and at the designer's centre:", warnings[0])
        self.assertIn("1 face(s) at the decal's step, 1 in the finished model: "
                      "1 of 1 face(s) within 0.1 cm of the centre, 1 measurable, nearest 0.000 cm away",
                      warnings[0])
        self.assertEqual(_mags(decal), (128.884, 5.961))

    def test_shorter_json_length_does_not_skip_the_fallback_check(self):
        # Review 2026-10-15: with the part longer than the JSON length the check
        # read 0 cm, so 3-in-1 at 40 in would have been sized at its trim-end
        # centre and left ~71 cm bare.
        decal = self._trim("3-In-1", 3, origin_x=0.03, along=254.0, across=7.62, length_in=40)
        decal.refuse_centre = lambda o: abs(o.x - 0.03) > 0.01
        tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        self.assertEqual(_mags(decal), (254.0, 7.62))
        self.assertEqual(len(decal.redefines), 1)
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(any("size not applied" in w and "about 60.9 cm more of the part would stay bare" in w
                            for w in warnings), warnings)

    def test_slightly_off_centre_fallback_reports_uneven_bare_ends(self):
        decal = self._trim("Reducer", 3, origin_x=60.2, along=179.419, across=5.537, length_in=40)
        decal.refuse_centre = lambda o: abs(o.x - 60.2) > 0.01
        _total, lines = tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        text = "\n".join(lines)
        self.assertEqual(_mags(decal), (101.6, 7.62), text)
        self.assertIn("so it is sized at the designer's centre instead", text)
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(any("the bare ends are uneven: about 10.9 cm at one end and 9.4 cm at the other" in w
                            for w in warnings), warnings)

    def test_refused_moved_centre_keeps_the_designer_size_when_much_would_stay_bare(self):
        # 3-in-1's centre sits at the trim's end: 127 cm there would leave 58.4 cm bare.
        decal = self._trim("3-In-1", 3, origin_x=0.03, along=254.0, across=7.62)
        decal.refuse_centre = lambda o: abs(o.x - 0.03) > 0.01
        _total, lines = tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        text = "\n".join(lines)
        self.assertEqual(_mags(decal), (254.0, 7.62), text)
        self.assertEqual(len(decal.redefines), 1, "no second try at the trim's end")
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(any("size not applied" in w and "about 58.4 cm more of the part would stay bare" in w
                            for w in warnings), warnings)

    def test_undo_after_a_primary_switch_leads_with_the_designers_face(self):
        # Review 2026-10-15: Fusion lists the faces primary-first after the resize,
        # so an undo that went by position led with the wrong face and was refused.
        first = Face(None, 0.76, box=((0.0, 0.76, -2.9), (30.0, 0.76, 2.9)))
        second = Face(None, 0.76, box=((30.0, 0.76, -2.9), (121.92, 0.76, 2.9)))
        decal = self._trim("3-In-1", 3, origin_x=0.03, along=254.0, across=7.62,
                           faces=[first, second])
        decal.faces_override = [first, second]
        decal.store_as = lambda m: _rotated_about_y(m, 10.0)
        _total, lines = tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        text = "\n".join(lines)
        self.assertEqual(len(decal.redefines), 2, text)
        self.assertIs(decal.redefines[1][1][0], first)
        self.assertEqual(_mags(decal), (254.0, 7.62), text)
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(any("the designer's size was kept" in w for w in warnings), warnings)

    def test_centre_that_cannot_move_onto_any_listed_face_is_reported(self):
        first = Face(None, 0.76, box=((0.0, 0.76, -2.9), (30.0, 0.76, 2.9)))
        last = Face(None, 0.76, box=((90.0, 0.76, -2.9), (121.92, 0.76, 2.9)))
        decal = self._trim("3-In-1", 3, origin_x=0.03, along=254.0, across=7.62,
                           faces=[first, last])
        decal.faces_override = [first, last]
        _total, lines = tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        o, _x, _y, _z = decal.transform.getAsCoordinateSystem()
        self.assertAlmostEqual(o.x, 0.03)
        warnings = tp.pop_decal_size_warnings()
        self.assertTrue(any("could not be centred" in w for w in warnings), "\n".join(lines))

    def test_centre_slides_along_the_surface_not_along_a_tilted_axis(self):
        # 3-in-1, 2026-10-15: moving the centre along the decal's own (tilted)
        # length axis lifted it off the face, and Fusion refused it.
        decal = self._trim("3-In-1", 3, origin_x=0.03, along=254.0, across=7.62)
        decal._transform = Matrix(Point(0.03, 0.76, 0), Vec(254.0, 0.6, 0), Vec(0, 0, 7.62), Vec(0, 1, 0))
        _total, lines = tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        text = "\n".join(lines)
        o, _x, _y, _z = decal.transform.getAsCoordinateSystem()
        self.assertAlmostEqual(o.x, 60.96, places=2)
        self.assertAlmostEqual(o.y, 0.76, places=9)
        self.assertEqual(tp.pop_decal_size_warnings(), [], text)

    def test_nearly_square_decal_is_left_alone(self):
        decal = self._trim("3-In-1", 3, origin_x=60.0, along=10.0, across=9.5)
        tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        self.assertEqual(decal.redefines, [])
        self.assertTrue(any("nearly equal" in w for w in tp.pop_decal_size_warnings()))

    def test_decal_on_an_unlisted_part_is_not_resized(self):
        decal = self._trim("Track", 3, origin_x=60.0, along=127.0, across=7.0)
        tp.set_decal_targets(ri.decal_size_map(ri.parse_instructions(
            '[{"fileName":"m","decals":[{"componentPartNumber":"3-In-1","textureSize":'
            '{"width":3,"length":50,"unit":"in"}}],"renderConfigurations":'
            '[{"namedView":"Close Up","hideBodies":[]}]}]').models[0]))
        tp.apply_decal_color_set(self.design, self.cs1, self.cs2)
        self.assertEqual(decal.redefines, [])


class PerModelResetTests(_Fixture):
    def test_state_does_not_leak_into_the_next_model(self):
        self._apply(self.cs_a1, self.cs_a2)
        self.assertTrue(tp._AUTHORED_SLOT_BY_KEY)
        tp.set_decal_targets(None)
        self.assertFalse(tp._AUTHORED_SLOT_BY_KEY)
        self.assertFalse(tp._AUTHORED_IMAGE_BY_KEY)
        self.assertFalse(tp._RETIRED_AUTHORED_KEYS)


if __name__ == "__main__":
    unittest.main()
