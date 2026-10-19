# Read raster dimensions and make 90-degree rotated copies WITHOUT requiring Pillow.
# Pure stdlib (no adsk imports) so it can be unit-tested outside Fusion.
#
# Why this exists: the designer authored most decals around PORTRAIT images
# (141x1000, 137x786, ...) while the client's colour-set rasters are LANDSCAPE
# (2500x486, ...). Swapping a landscape raster into a decal laid out for a portrait
# one puts the long side ACROSS the part. Presenting the new raster in the same
# orientation as the authored one fixes that without touching the decal's placement.
#
# Fusion's bundled Python ships without Pillow, so rotation falls back to what the
# operating system already has: GDI+ on Windows (via ctypes) and ``sips`` on macOS.

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple

PORTRAIT = "portrait"
LANDSCAPE = "landscape"
SQUARE = "square"

# Treat nearly-square rasters as square: rotating them changes nothing useful.
_SQUARE_TOLERANCE = 0.02

_CACHE_DIR_NAME = "BatchRenderImageCache"

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
# JPEG start-of-frame markers that carry the image size.
_JPEG_SOF = frozenset(
    (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
)


def _size_from_stream(fh) -> Optional[Tuple[int, int]]:
    head = fh.read(32)
    if head[:8] == _PNG_SIG and len(head) >= 24:
        w, h = struct.unpack(">II", head[16:24])
        return (int(w), int(h)) if w and h else None
    if head[:2] != b"\xff\xd8":
        return None
    fh.seek(2)
    while True:
        marker = fh.read(2)
        if len(marker) < 2:
            return None
        if marker[0] != 0xFF:
            # Lost sync — resynchronise on the next 0xFF.
            fh.seek(-1, 1)
            continue
        code = marker[1]
        if code == 0xFF:
            fh.seek(-1, 1)
            continue
        if code in (0xD8, 0x01) or 0xD0 <= code <= 0xD7:
            continue
        if code == 0xD9:
            return None
        raw_len = fh.read(2)
        if len(raw_len) < 2:
            return None
        (seg_len,) = struct.unpack(">H", raw_len)
        if code in _JPEG_SOF:
            body = fh.read(5)
            if len(body) < 5:
                return None
            h, w = struct.unpack(">HH", body[1:5])
            return (int(w), int(h)) if w and h else None
        fh.seek(seg_len - 2, 1)


def image_size(path: Path) -> Optional[Tuple[int, int]]:
    """``(width, height)`` in pixels for a PNG or JPEG, read from the header only."""
    try:
        with open(str(path), "rb") as fh:
            return _size_from_stream(fh)
    except (OSError, struct.error, ValueError):
        return None


def image_size_from_bytes(data: bytes) -> Optional[Tuple[int, int]]:
    """Same as :func:`image_size` for an image already in memory."""
    import io as _io

    try:
        return _size_from_stream(_io.BytesIO(data))
    except (OSError, struct.error, ValueError):
        return None


def embedded_image_sizes(f3d_path: Path) -> list:
    """``[(suffix, (w, h)), ...]`` for the decal images stored inside a .f3d.

    A last-resort way to learn what orientation the designer's images had when a
    decal will not give its image up. .f3d entries are zstd-compressed (ZIP method
    93), which the standard library only reads from Python 3.14 on — the version
    Fusion bundles. On an older interpreter this simply returns an empty list.
    """
    import zipfile

    out = []
    try:
        with zipfile.ZipFile(str(f3d_path)) as zf:
            for info in zf.infolist():
                name = info.filename
                if info.is_dir() or "/Images.BlobParts/" not in name:
                    continue
                if not name.startswith("FusionAssetName"):
                    continue
                try:
                    size = image_size_from_bytes(zf.read(info))
                except Exception:
                    continue
                if size:
                    out.append((Path(name).suffix.lower(), size))
    except Exception:
        return []
    return out


def orientation_of(size: Optional[Tuple[int, int]]) -> Optional[str]:
    """``portrait`` / ``landscape`` / ``square`` for a pixel size, None if unknown."""
    if not size:
        return None
    w, h = size
    if w <= 0 or h <= 0:
        return None
    if abs(w - h) <= _SQUARE_TOLERANCE * max(w, h):
        return SQUARE
    return LANDSCAPE if w > h else PORTRAIT


def needs_rotation(authored: Optional[str], incoming: Optional[str]) -> bool:
    """True when one raster is portrait and the other landscape."""
    return {authored, incoming} == {PORTRAIT, LANDSCAPE}


def cache_root() -> Path:
    return Path(tempfile.gettempdir()) / _CACHE_DIR_NAME


def clear_cache() -> int:
    """Delete every rotated copy made by this module. Returns files removed."""
    root = cache_root()
    if not root.is_dir():
        return 0
    n = sum(1 for p in root.rglob("*") if p.is_file())
    shutil.rmtree(str(root), ignore_errors=True)
    return n


def aspect_of(size: Optional[Tuple[int, int]]) -> Optional[float]:
    """Width / height, or None when unknown."""
    if not size or size[0] <= 0 or size[1] <= 0:
        return None
    return float(size[0]) / float(size[1])


def planned_size(
    size: Tuple[int, int],
    rotate: bool,
    target_aspect: Optional[float],
    max_side: int = 4096,
) -> Tuple[int, int]:
    """Output ``(width, height)`` for a raster after optional rotation and conforming.

    Conforming keeps the LONG side's pixel count (so no detail is thrown away along
    the grain) and resamples the short side until width/height equals
    ``target_aspect``. Differences under 1% are ignored — not worth a resample.
    """
    w, h = int(size[0]), int(size[1])
    if rotate:
        w, h = h, w
    if target_aspect and target_aspect > 0:
        current = float(w) / float(h)
        if abs(current - target_aspect) / target_aspect > 0.01:
            long_side = max(w, h)
            if target_aspect < 1.0:
                h = long_side
                w = max(1, int(round(long_side * target_aspect)))
            else:
                w = long_side
                h = max(1, int(round(long_side / target_aspect)))
    biggest = max(w, h)
    if max_side and biggest > max_side:
        scale = float(max_side) / float(biggest)
        w = max(1, int(round(w * scale)))
        h = max(1, int(round(h * scale)))
    return w, h


def _cache_path(src: Path, tag: str) -> Path:
    """Stable per-source location; keeps the ORIGINAL stem as the filename.

    Fusion renames a decal after the image it is given, so keeping the stem means
    the decal is still called e.g. ``Color Set 04-2`` (and keeps its trailing slot
    digit) rather than a hash.
    """
    try:
        st = src.stat()
        stamp = "{}|{}|{}".format(st.st_size, int(st.st_mtime), tag)
    except OSError:
        stamp = "?|{}".format(tag)
    key = hashlib.sha1(
        "{}|{}".format(str(src.resolve()).lower(), stamp).encode("utf-8")
    ).hexdigest()[:12]
    return cache_root() / key / (src.stem + ".png")


def _make_with_pillow(
    src: Path, dst: Path, rotate: bool, clockwise: bool, out: Tuple[int, int]
) -> Tuple[bool, str]:
    try:
        from PIL import Image  # type: ignore
    except Exception as ex:  # ImportError or a broken install
        return False, "Pillow unavailable ({})".format(type(ex).__name__)
    try:
        with Image.open(str(src)) as im:
            work = im
            if rotate:
                work = work.transpose(Image.ROTATE_270 if clockwise else Image.ROTATE_90)
            if work.size != out:
                work = work.resize(out, Image.LANCZOS)
            work.save(str(dst), format="PNG")
        return True, "pillow"
    except Exception as ex:
        return False, "Pillow failed: {}".format(ex)


def _make_with_gdiplus(
    src: Path, dst: Path, rotate: bool, clockwise: bool, out: Tuple[int, int]
) -> Tuple[bool, str]:
    """Windows: rotate / resample through GDI+ (always present; reads PNG and JPEG)."""
    if not sys.platform.startswith("win"):
        return False, "GDI+ is Windows-only"
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as ex:
        return False, "ctypes unavailable: {}".format(ex)

    class _StartupInput(ctypes.Structure):
        _fields_ = [
            ("GdiplusVersion", ctypes.c_uint32),
            ("DebugEventCallback", ctypes.c_void_p),
            ("SuppressBackgroundThread", wintypes.BOOL),
            ("SuppressExternalCodecs", wintypes.BOOL),
        ]

    class _Guid(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    try:
        gdi = ctypes.WinDLL("gdiplus")
    except Exception as ex:
        return False, "gdiplus.dll not loadable: {}".format(ex)

    vp = ctypes.c_void_p
    ci = ctypes.c_int
    pvp = ctypes.POINTER(vp)
    protos = {
        "GdiplusStartup": (
            [ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(_StartupInput), vp],
            ci,
        ),
        "GdiplusShutdown": ([ctypes.c_size_t], None),
        "GdipLoadImageFromFile": ([ctypes.c_wchar_p, pvp], ci),
        "GdipImageRotateFlip": ([vp, ci], ci),
        "GdipGetImageWidth": ([vp, ctypes.POINTER(ctypes.c_uint)], ci),
        "GdipGetImageHeight": ([vp, ctypes.POINTER(ctypes.c_uint)], ci),
        "GdipCreateBitmapFromScan0": ([ci, ci, ci, ci, vp, pvp], ci),
        "GdipGetImageGraphicsContext": ([vp, pvp], ci),
        "GdipSetInterpolationMode": ([vp, ci], ci),
        "GdipSetPixelOffsetMode": ([vp, ci], ci),
        "GdipCreateImageAttributes": ([pvp], ci),
        "GdipSetImageAttributesWrapMode": ([vp, ci, ctypes.c_uint32, wintypes.BOOL], ci),
        "GdipDrawImageRectRectI": (
            [vp, vp, ci, ci, ci, ci, ci, ci, ci, ci, ci, vp, vp, vp],
            ci,
        ),
        "GdipDeleteGraphics": ([vp], ci),
        "GdipDisposeImageAttributes": ([vp], ci),
        "GdipSaveImageToFile": ([vp, ctypes.c_wchar_p, ctypes.POINTER(_Guid), vp], ci),
        "GdipDisposeImage": ([vp], ci),
    }
    try:
        for name, (argtypes, restype) in protos.items():
            fn = getattr(gdi, name)
            fn.argtypes = argtypes
            fn.restype = restype
    except AttributeError as ex:
        return False, "GDI+ entry point missing: {}".format(ex)

    token = ctypes.c_size_t(0)
    startup = _StartupInput(1, None, False, False)
    status = gdi.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None)
    if status != 0:
        return False, "GdiplusStartup status {}".format(status)

    image = vp()
    canvas = vp()
    graphics = vp()
    attrs = vp()
    try:
        status = gdi.GdipLoadImageFromFile(str(src), ctypes.byref(image))
        if status != 0 or not image:
            return False, "GDI+ could not load image (status {})".format(status)
        if rotate:
            # RotateFlipType: 1 = Rotate90FlipNone (clockwise), 3 = Rotate270FlipNone.
            status = gdi.GdipImageRotateFlip(image, 1 if clockwise else 3)
            if status != 0:
                return False, "GDI+ rotate status {}".format(status)

        cur_w = ctypes.c_uint(0)
        cur_h = ctypes.c_uint(0)
        gdi.GdipGetImageWidth(image, ctypes.byref(cur_w))
        gdi.GdipGetImageHeight(image, ctypes.byref(cur_h))
        to_save = image
        if (int(cur_w.value), int(cur_h.value)) != (int(out[0]), int(out[1])):
            # 0x0026200A = PixelFormat32bppARGB.
            status = gdi.GdipCreateBitmapFromScan0(
                int(out[0]), int(out[1]), 0, 0x0026200A, None, ctypes.byref(canvas)
            )
            if status != 0 or not canvas:
                return False, "GDI+ could not create {}x{} bitmap (status {})".format(
                    out[0], out[1], status
                )
            status = gdi.GdipGetImageGraphicsContext(canvas, ctypes.byref(graphics))
            if status != 0 or not graphics:
                return False, "GDI+ graphics context status {}".format(status)
            gdi.GdipSetInterpolationMode(graphics, 7)  # HighQualityBicubic
            gdi.GdipSetPixelOffsetMode(graphics, 4)  # Half — no half-pixel shift
            # Mirror at the borders so bicubic sampling does not fade the edge
            # pixels toward transparent (visible as a pale rim on the part).
            if gdi.GdipCreateImageAttributes(ctypes.byref(attrs)) == 0 and attrs:
                gdi.GdipSetImageAttributesWrapMode(attrs, 3, 0, False)  # TileFlipXY
            status = gdi.GdipDrawImageRectRectI(
                graphics,
                image,
                0,
                0,
                int(out[0]),
                int(out[1]),
                0,
                0,
                int(cur_w.value),
                int(cur_h.value),
                2,  # UnitPixel
                attrs if attrs else None,
                None,
                None,
            )
            if status != 0:
                return False, "GDI+ resample status {}".format(status)
            to_save = canvas

        # Built-in PNG encoder {557CF406-1A04-11D3-9A73-0000F81EF32E}.
        png = _Guid(
            0x557CF406,
            0x1A04,
            0x11D3,
            (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E),
        )
        status = gdi.GdipSaveImageToFile(to_save, str(dst), ctypes.byref(png), None)
        if status != 0:
            return False, "GDI+ save status {}".format(status)
        return True, "gdiplus"
    finally:
        if graphics:
            gdi.GdipDeleteGraphics(graphics)
        if attrs:
            gdi.GdipDisposeImageAttributes(attrs)
        if canvas:
            gdi.GdipDisposeImage(canvas)
        if image:
            gdi.GdipDisposeImage(image)
        gdi.GdiplusShutdown(token)


def _make_with_sips(
    src: Path, dst: Path, rotate: bool, clockwise: bool, out: Tuple[int, int]
) -> Tuple[bool, str]:
    """macOS: rotate / resample with the built-in ``sips`` tool."""
    if sys.platform != "darwin":
        return False, "sips is macOS-only"

    def _run(args):
        proc = subprocess.run(
            ["/usr/bin/sips"] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "sips exit {}: {}".format(
                    proc.returncode, proc.stderr.decode("utf-8", "replace")[:200]
                )
            )

    try:
        first = ["-s", "format", "png"]
        if rotate:
            first = ["-r", "90" if clockwise else "270"] + first
        _run(first + [str(src), "--out", str(dst)])
        if image_size(dst) != (int(out[0]), int(out[1])):
            # -z takes HEIGHT then WIDTH and resamples without keeping the aspect.
            _run(["-z", str(int(out[1])), str(int(out[0])), str(dst)])
    except Exception as ex:
        return False, "sips failed: {}".format(ex)
    return True, "sips"


def prepared_copy(
    src: Path,
    rotate: bool = False,
    clockwise: bool = True,
    target_aspect: Optional[float] = None,
    max_side: int = 4096,
) -> Tuple[Optional[Path], str]:
    """Cached PNG copy of ``src``, optionally rotated 90 degrees and/or conformed.

    ``target_aspect`` (width / height, measured AFTER any rotation) resamples the
    copy so it has exactly the proportions of the image the designer authored the
    decal with — the decal then occupies exactly the area it did in the model.

    Returns ``(path, method)``; ``(src, "original")`` when nothing needs doing and
    ``(None, reason)`` on failure. The output size is verified by re-reading the
    file, so a tool that silently wrote something else is reported as a failure
    rather than handed to Fusion.
    """
    src = Path(src)
    before = image_size(src)
    if before is None:
        return None, "cannot read image size of {}".format(src.name)

    out = planned_size(before, rotate, target_aspect, max_side)
    if not rotate and out == before:
        return src, "original"

    tag = "r{}c{}_{}x{}".format(int(bool(rotate)), int(bool(clockwise)), out[0], out[1])
    dst = _cache_path(src, tag)
    if dst.is_file() and image_size(dst) == out:
        return dst, "cache"

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()
    except OSError as ex:
        return None, "cannot prepare cache folder: {}".format(ex)

    reasons = []
    for make in (_make_with_pillow, _make_with_gdiplus, _make_with_sips):
        ok, note = make(src, dst, rotate, clockwise, out)
        if ok:
            after = image_size(dst)
            if after == out:
                return dst, note
            reasons.append(
                "{} wrote {} but expected {}x{}".format(note, after, out[0], out[1])
            )
            try:
                dst.unlink()
            except OSError:
                pass
        else:
            reasons.append(note)
    return None, "; ".join(reasons)


def rotated_copy(src: Path, clockwise: bool = True) -> Tuple[Optional[Path], str]:
    """Cached PNG copy of ``src`` rotated by 90 degrees (no resampling)."""
    return prepared_copy(
        src, rotate=True, clockwise=clockwise, target_aspect=None, max_side=0
    )
