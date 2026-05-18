# Named views + viewport image export for local batch rendering.

from __future__ import annotations

import math
import re
import time
from typing import List

import adsk.core
import adsk.fusion

# Fusion ``Rendering.startLocalRender`` accepts widths/heights in [108, 4000].
_LOCAL_RENDER_MIN_PX = 108
_LOCAL_RENDER_MAX_PX = 4000

# When True (default): keep Fusion’s soft cast shadow where supported — closer to “product /
# Render‑style” stills than a floating model. Set False if you prefer a floating object with
# no ground contact shadow (older batch default behavior).
PRESERVE_GROUND_SHADOW_FOR_BATCH: bool = True


def pump_ui() -> None:
    try:
        import adsk  # type: ignore

        adsk.doEvents()
    except Exception:
        pass


def list_named_views(design: adsk.fusion.Design) -> List[adsk.fusion.NamedView]:
    out: List[adsk.fusion.NamedView] = []
    try:
        coll = design.namedViews
        for i in range(coll.count):
            nv = coll.item(i)
            out.append(nv)
    except Exception:
        pass
    return out


def fit_view_to_model(app: adsk.core.Application) -> bool:
    """Re-frame the active viewport so the whole visible design fits.

    Tries the public ``Viewport.fit`` API first, then falls back to the
    ``ViewFit`` text command which is available on builds where ``fit``
    is missing. Keeps the named view's camera angle but adjusts the
    distance / target so nothing is cropped.
    """
    try:
        vp = app.activeViewport
        if hasattr(vp, "fit"):
            try:
                vp.fit()
                vp.refresh()
                pump_ui()
                return True
            except Exception:
                pass
        try:
            app.executeTextCommand("Commands.Start ViewFit")
            vp.refresh()
            pump_ui()
            return True
        except Exception:
            return False
    except Exception:
        return False


# After fit, multiply viewExtents / camera distance. Values > 1 zoom out (more even margin
# on all four sides); < 1 zooms in. Named views containing ``full`` enforce at least
# ``_BALANCED_MARGIN_MIN_SCALE`` so hero shots keep reference-style breathing room.
_BALANCED_MARGIN_MIN_SCALE: float = 1.34


def tighten_view_after_fit(app: adsk.core.Application, extent_scale: float = 1.0) -> bool:
    """Adjust zoom after ``fit`` while keeping camera orientation and target.

    * ``extent_scale`` < 1: zoom in (model fills more of the frame).
    * ``extent_scale`` == 1: leave ``fit`` framing unchanged.
    * ``extent_scale`` > 1: zoom out (clear space at the edges — reference-style framing).
    """
    try:
        vp = app.activeViewport
        cam = vp.camera
        if cam is None:
            return False

        # Ceiling raised to 6.0 so a very long thin part can be zoomed far
        # enough out to sit small and centered with generous margin.
        s = max(0.20, min(6.0, float(extent_scale)))

        # Primary path: scale orthographic / parallel view extents.
        try:
            ext = float(cam.viewExtents)
            if ext > 0:
                cam.viewExtents = ext * s
                vp.camera = cam
                vp.refresh()
                pump_ui()
                return True
        except Exception:
            pass

        # Fallback for perspective cameras when viewExtents is unavailable.
        try:
            eye = cam.eye
            target = cam.target
            if eye and target:
                dx = eye.x - target.x
                dy = eye.y - target.y
                dz = eye.z - target.z
                cam.eye = adsk.core.Point3D.create(
                    target.x + dx * s,
                    target.y + dy * s,
                    target.z + dz * s,
                )
                vp.camera = cam
                vp.refresh()
                pump_ui()
                return True
        except Exception:
            pass
    except Exception:
        return False
    return False


def yaw_orbit_camera_about_world_y(app: adsk.core.Application, degrees: float) -> bool:
    """Rotate eye around target in the XZ plane (yaw). Helps thin edge-on shots."""
    try:
        vp = app.activeViewport
        cam = vp.camera
        if cam is None:
            return False
        eye = cam.eye
        target = cam.target
        if not eye or not target:
            return False
        vx = eye.x - target.x
        vy = eye.y - target.y
        vz = eye.z - target.z
        rad = math.radians(degrees)
        c = math.cos(rad)
        s = math.sin(rad)
        rx = vx * c + vz * s
        ry = vy
        rz = -vx * s + vz * c
        cam.eye = adsk.core.Point3D.create(target.x + rx, target.y + ry, target.z + rz)
        vp.camera = cam
        vp.refresh()
        pump_ui()
        return True
    except Exception:
        return False


# When True, the batch ignores the .f3d's saved named views entirely and
# shoots every output from fixed, Fusion-standard 3/4 isometric angles.
# This reliably reproduces the compact, centered "product still" look for
# long thin parts (stair treads / noses) — the saved named views in these
# templates are side/elevation angles that render a long part as a thin
# diagonal streak. Set False to go back to honoring saved named views.
FORCE_ISOMETRIC_VIEW: bool = True

# When True, every image is a near-instant VIEWPORT screenshot — the
# ray-traced ``Rendering.startLocalRender`` path is never used, regardless
# of the dialog's render-backend selection. This is the anti-freeze switch:
# ray tracing 9 images is what locked Fusion up. The viewport already shows
# the applied decal texture, so captures are clean product stills with only
# slightly flatter lighting (no ray-traced GI / soft shadows). Set False to
# allow the dialog-selected ray-traced backend again.
FORCE_VIEWPORT_CAPTURE: bool = True

# Distinct standard angles rendered per color set when FORCE_ISOMETRIC_VIEW
# is on. Each entry is ``(label, ViewOrientations-enum-name, yaw_degrees)``;
# the label becomes the ``{named view}`` token in the output filename so
# every angle is a separate file. ``yaw_degrees`` orbits the camera about
# the world vertical AFTER the orientation is set, giving a distinct 3/4
# angle without dropping to the edge-on bottom isometric. Add/remove entries
# to control how many images per color set you get.
ISO_VIEW_ORIENTATIONS = (
    ("Iso Front-Right", "IsoTopRightViewOrientation", 0.0),
    ("Iso Front-Left", "IsoTopLeftViewOrientation", 0.0),
    ("Iso Front 3-4", "IsoTopRightViewOrientation", 28.0),
)

# Zoom factor when framing on the PART's own bounding box (see
# frame_part_centered). It scales the bounding-SPHERE fit, and a long thin
# moulding only occupies a thin diagonal slice of that sphere, so values
# well below 1.0 are correct and safe (the thin cross-section never clips):
#   1.0  = whole bounding sphere fits (part looks tiny — the old problem)
#   1.05 = part sits a bit smaller, comfortably centered with even margin
#   lower = bigger (0.52 ran off the frame); higher = smaller.
ISO_VIEW_MARGIN_SCALE: float = 1.05


def set_isometric_camera(
    app: adsk.core.Application,
    orientation_name: str = "IsoTopRightViewOrientation",
    yaw_degrees: float = 0.0,
) -> bool:
    """Aim the active viewport at a Fusion standard isometric orientation.

    Prefers the official ``Camera.viewOrientation`` enum (Fusion computes the
    correct corner regardless of the model's up-axis). Falls back to a manual
    Z-up (1, -1, 1) eye vector on builds where the enum assignment is rejected
    (the fallback only approximates the top-right corner). When ``yaw_degrees``
    is non-zero the camera is orbited about the world vertical afterwards, so
    callers can get a distinct 3/4 angle without using the edge-on bottom iso.
    """
    try:
        vp = app.activeViewport
        cam = vp.camera
        if cam is None:
            return False
    except Exception:
        return False

    # Primary: official orientation enum — build-stable, up-axis aware.
    ok = False
    try:
        orient = getattr(
            adsk.core.ViewOrientations,
            orientation_name,
            adsk.core.ViewOrientations.IsoTopRightViewOrientation,
        )
        cam.viewOrientation = orient
        cam.isSmoothTransition = False
        vp.camera = cam
        vp.refresh()
        pump_ui()
        ok = True
    except Exception:
        ok = False

    if ok:
        if yaw_degrees:
            # Orbit BEFORE the caller's fit so the rotated pose is what gets
            # framed (fitting first then orbiting slides a long part off-frame).
            yaw_orbit_camera_about_world_y(app, float(yaw_degrees))
        return True

    # Fallback: place the eye on the (1, -1, 1) corner, Z up.
    try:
        target = cam.target or adsk.core.Point3D.create(0.0, 0.0, 0.0)
        eye = cam.eye
        if eye and target:
            dx = eye.x - target.x
            dy = eye.y - target.y
            dz = eye.z - target.z
            dist = math.sqrt(dx * dx + dy * dy + dz * dz) or 10.0
        else:
            dist = 10.0
        n = math.sqrt(3.0)
        cam.eye = adsk.core.Point3D.create(
            target.x + dist / n,
            target.y - dist / n,
            target.z + dist / n,
        )
        cam.target = target
        cam.upVector = adsk.core.Vector3D.create(0.0, 0.0, 1.0)
        cam.isSmoothTransition = False
        vp.camera = cam
        vp.refresh()
        pump_ui()
        if yaw_degrees:
            yaw_orbit_camera_about_world_y(app, float(yaw_degrees))
        return True
    except Exception:
        return False


# Occurrence name fragments that must NOT drive framing: studio/scene
# lights and Render-workspace helper proxies. Mirrors the batch-hide rules
# so the camera frames the product, not an off-to-the-side light.
_FRAME_SKIP_OCC_SUBSTRINGS = (
    "light",
    "uv plane",
    "uv_plane",
    "uv reference",
    "uv map",
    "backdrop",
    "render proxy",
    "light rig",
    "light proxy",
)


def _frame_entity_visible(entity) -> bool:
    """Best-effort 'shown in the document' (isVisible, then isLightBulbOn)."""
    for attr in ("isVisible", "isLightBulbOn"):
        try:
            v = getattr(entity, attr)
        except Exception:
            continue
        if v is None:
            continue
        return bool(v)
    return True


def _visible_root_part_bbox(design):
    """World-space bbox center + radius of the visible PRODUCT geometry.

    Unions the visible root-component bodies AND every visible, non-light
    sub-occurrence (the Bullnose's plank / nose / foam all live in
    occurrences, so a root-only bbox was empty → the camera fell back to
    ViewFit, which included the hidden treads + light and shoved the part
    off-centre). Hidden occurrences (alternate treads/risers the designer
    turned off) and light / helper proxies are excluded so the frame is
    centred on exactly what gets rendered. Returns ``(Point3D, radius)`` or
    ``None`` (caller falls back to ViewFit).
    """
    have = False
    xmin = ymin = zmin = 1e30
    xmax = ymax = zmax = -1e30

    def _union(bb):
        nonlocal have, xmin, ymin, zmin, xmax, ymax, zmax
        if bb is None:
            return
        mn, mx = bb.minPoint, bb.maxPoint
        xmin = min(xmin, mn.x); ymin = min(ymin, mn.y); zmin = min(zmin, mn.z)
        xmax = max(xmax, mx.x); ymax = max(ymax, mx.y); zmax = max(zmax, mx.z)
        have = True

    # Root-component bodies (single-body mouldings live here).
    try:
        bodies = design.rootComponent.bRepBodies
        for i in range(bodies.count):
            try:
                b = bodies.item(i)
                if not _frame_entity_visible(b):
                    continue
                _union(b.boundingBox)
            except Exception:
                continue
    except Exception:
        pass

    # Visible product sub-occurrences (assemblies like the Bullnose).
    try:
        occs = design.rootComponent.allOccurrences
        for i in range(occs.count):
            try:
                occ = occs.item(i)
                nm = (occ.name or "").lower()
                if any(s in nm for s in _FRAME_SKIP_OCC_SUBSTRINGS):
                    continue
                if not _frame_entity_visible(occ):
                    continue
                _union(occ.boundingBox)
            except Exception:
                continue
    except Exception:
        pass

    if not have:
        return None
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    cz = (zmin + zmax) / 2.0
    radius = 0.5 * math.sqrt(
        (xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2
    )
    if radius <= 0.0:
        return None
    return adsk.core.Point3D.create(cx, cy, cz), radius


def frame_part_centered(
    app: adsk.core.Application, design, pad: float = ISO_VIEW_MARGIN_SCALE
) -> bool:
    """Aim the camera dead-centre on the part's bbox at a controlled zoom.

    Keeps the current view direction (set by the isometric orientation), so
    only the target / distance / extents change. ``pad`` scales the
    bounding-sphere fit: <1 zooms IN (bigger part — correct for thin
    mouldings), 1 fits the whole sphere, >1 adds margin. Independent of any
    off-to-the-side light geometry that ``ViewFit`` would otherwise include.
    """
    info = _visible_root_part_bbox(design)
    if info is None:
        return False
    center, radius = info
    # Guard only against zero/insane zoom; allow <1 so thin parts get big.
    radius *= max(0.05, float(pad))
    try:
        vp = app.activeViewport
        cam = vp.camera
        if cam is None:
            return False

        eye = cam.eye
        tgt = cam.target
        dx, dy, dz = eye.x - tgt.x, eye.y - tgt.y, eye.z - tgt.z
        dlen = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dlen <= 1e-9:
            dx, dy, dz, dlen = 1.0, -1.0, 1.0, math.sqrt(3.0)
        ux, uy, uz = dx / dlen, dy / dlen, dz / dlen

        # Perspective: distance so a sphere of `radius` fills the FOV.
        try:
            fov = float(cam.perspectiveAngle)
        except Exception:
            fov = 0.0
        if fov <= 0.0:
            fov = 0.61  # ~35° fallback
        distance = radius / math.tan(fov / 2.0)

        cam.target = center
        cam.eye = adsk.core.Point3D.create(
            center.x + ux * distance,
            center.y + uy * distance,
            center.z + uz * distance,
        )
        # Orthographic / parallel cameras ignore distance — set the extent.
        try:
            cam.viewExtents = 2.0 * radius
        except Exception:
            pass
        cam.isSmoothTransition = False
        vp.camera = cam
        vp.refresh()
        pump_ui()
        return True
    except Exception:
        return False


def apply_isometric_view_framing(
    app: adsk.core.Application,
    design,
    orientation_name: str = "IsoTopRightViewOrientation",
    yaw_degrees: float = 0.0,
    pad: float = ISO_VIEW_MARGIN_SCALE,
) -> None:
    """Force a 3/4 isometric (optionally yawed), centred tightly on the part.

    Frames on the part's own bounding box so the moulding sits dead-centre;
    falls back to ViewFit + zoom-out only if the bbox can't be computed.
    """
    set_isometric_camera(app, orientation_name, yaw_degrees)
    if not frame_part_centered(app, design, pad):
        fit_view_to_model(app)
        tighten_view_after_fit(app, 2.4)


_ISO_TOKENS = (
    "iso",
    "isometric",
    "corner",
    "oblique",
    "perspective",
    "hero",
    "showcase",
)
_AXIS_TOKENS = frozenset(("close", "front", "back"))


def apply_named_view_framing(
    app: adsk.core.Application,
    view_name: str,
    *,
    preserve_orientation: bool = False,
) -> None:
    """Frame the model for export.

    ``preserve_orientation=True`` means the caller activated a **human-authored
    named view** from the ``.f3d``. The designer already chose a good camera
    angle (the look the client signed off on), so we must NOT yaw-orbit or
    re-aim it — doing that rotated good 3/4 angles into a near-edge-on line.
    We only re-fit (which keeps the authored eye direction / up vector and just
    adjusts distance + target so nothing is clipped and the part is centered)
    and add a small even margin.

    ``preserve_orientation=False`` is the fallback "Viewport" capture: there is
    no authored camera, so name-token heuristics pick a yaw + margin. Order
    matters there — the yaw must happen **before** the fit, otherwise a long
    thin part gets swung diagonally off-frame after the fit already framed the
    old angle.
    """
    if preserve_orientation:
        # Trust the authored named-view camera. Just recenter + light margin.
        fit_view_to_model(app)
        tighten_view_after_fit(app, 1.08)
        return

    vn = (view_name or "").lower()
    # Whole tokens only — avoids matching ``feedback``, ``backdrop``, etc.
    words = set(re.findall(r"[a-z0-9]+", vn))

    def _margin_scale(base: float) -> float:
        """``Full *`` views: never tighter than a balanced reference margin on all sides."""
        b = float(base)
        if "full" in words:
            return max(b, _BALANCED_MARGIN_MIN_SCALE)
        return b

    # 1. Decide the orientation tweak + zoom margin from the view name.
    if "close" in words:
        # Close / detail shots: modest zoom-in (still leaves some air).
        scale = _margin_scale(0.82)
    elif "back" in words and "front" not in words:
        # Rear angles on long thin parts read as a “line”; yaw slightly off-axis.
        yaw_orbit_camera_about_world_y(app, 18.0)
        scale = _margin_scale(1.36)
    elif "front" in words and "back" not in words:
        # Front-style hero shots — even corner space like isometric references.
        scale = _margin_scale(1.36)
    elif {"top", "bottom"}.intersection(words):
        # True top/bottom captures — keep yaw (edge-on L reads as a line if we orbit).
        scale = _margin_scale(1.28)
    elif {"trimetric"}.intersection(words):
        scale = _margin_scale(1.28)
    elif any(t in words for t in _ISO_TOKENS):
        yaw_orbit_camera_about_world_y(app, 14.0)
        scale = _margin_scale(1.30)
    else:
        if _AXIS_TOKENS.isdisjoint(words):
            # Default / single “Viewport” captures: gentle ¾ orbit matches many
            # product stills (L-profiles read as slabs when shot edge-on).
            yaw_orbit_camera_about_world_y(app, 11.0)
        scale = _margin_scale(1.32)

    # 2. Fit AFTER the yaw so the (now final) camera direction frames the whole
    #    model centered. 3. Pull back for even margin without losing centering.
    fit_view_to_model(app)
    tighten_view_after_fit(app, scale)


def prepare_clean_render_view(app: adsk.core.Application) -> None:
    """Best-effort cleanup of the viewport before saving an image.

    Disables cluttered CAD chrome (origin, grid lines, harsh reflections).
    Optionally keeps soft ground/contact shadows — many reference renders use that look.
    Every step is wrapped in try/except because the available text
    commands and viewport flags differ between Fusion versions; a
    failure on any one step is fine, the rest still apply.
    """
    try:
        vp = app.activeViewport
    except Exception:
        return

    shadow_attrs = (
        ("groundShadow", False),
        ("groundShadowEnabled", False),
    )
    if not PRESERVE_GROUND_SHADOW_FOR_BATCH:
        for attr, value in shadow_attrs:
            try:
                if hasattr(vp, attr):
                    setattr(vp, attr, value)
            except Exception:
                pass
    non_shadow = (
        ("groundReflection", False),
        ("groundReflectionEnabled", False),
        ("displayGrid", False),
        ("isGridVisible", False),
    )
    for attr, value in non_shadow:
        try:
            if hasattr(vp, attr):
                setattr(vp, attr, value)
        except Exception:
            pass

    cmds = []
    if not PRESERVE_GROUND_SHADOW_FOR_BATCH:
        cmds.extend(
            (
                "Commands.SetBool Visual.GroundShadow false",
            )
        )
    cmds.extend(
        (
            "Commands.SetBool Visual.GroundReflection false",
            "Commands.SetBool Visual.OriginGeometry false",
            "Commands.SetBool Visual.OriginPlanes false",
            "Commands.SetBool Visual.Axes false",
            "Commands.SetBool Visual.LayoutGridLines false",
        )
    )
    if not PRESERVE_GROUND_SHADOW_FOR_BATCH:
        cmds.append("NuCommands.SetEnvironmentDisplay 0")
    for cmd in cmds:
        try:
            app.executeTextCommand(cmd)
        except Exception:
            pass

    try:
        vp.refresh()
        pump_ui()
    except Exception:
        pass


def activate_named_view(app: adsk.core.Application, named_view: adsk.fusion.NamedView) -> bool:
    try:
        vp = app.activeViewport
        cam = named_view.camera
        if cam:
            vp.camera = cam
        vp.refresh()
        pump_ui()
        return True
    except Exception:
        return False


def save_viewport_image(app: adsk.core.Application, filepath: str, width: int, height: int) -> bool:
    """Save active viewport; extension chooses format (.png / .jpg)."""
    vp = app.activeViewport
    path = filepath
    try:
        if hasattr(vp, "saveAsImageFile"):
            return bool(vp.saveAsImageFile(path, int(width), int(height)))
        if hasattr(vp, "saveAsImage"):
            return bool(vp.saveAsImage(path, int(width), int(height)))
    except Exception:
        return False
    return False


def _clamp_local_render_extent(value: int) -> int:
    return max(_LOCAL_RENDER_MIN_PX, min(_LOCAL_RENDER_MAX_PX, int(value)))


def save_fusion_local_render(
    design: adsk.fusion.Design,
    app: adsk.core.Application,
    filepath: str,
    width: int,
    height: int,
    *,
    render_quality: int = 60,
    timeout_sec: float = 1800.0,
    poll_sec: float = 0.25,
) -> bool:
    """Ray-traced local export via ``Rendering.startLocalRender`` (Render workspace API).

    Uses the active viewport camera. Requires ``design.renderManager``; returns False if the
    API is unavailable or the render fails/timeouts (caller may fall back to ``save_viewport_image``).

    ``render_quality`` is the Fusion Render-quality slider (25 draft … 100
    final). Ray-trace time scales steeply with it; 60 is the speed/quality
    sweet spot for these matte product stills (≈half the time of 90 with no
    visible difference on a textured part against a plain backdrop). Raise
    toward 90 only if you need print-grade output and can wait.
    """
    path = filepath
    low = path.lower()
    if not any(low.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".tiff", ".tif")):
        return False

    w = _clamp_local_render_extent(width)
    h = _clamp_local_render_extent(height)
    q = max(25, min(100, int(render_quality)))

    try:
        rm = design.renderManager
        rendering = rm.rendering
    except Exception:
        return False

    cam = None
    try:
        cam = app.activeViewport.camera
    except Exception:
        cam = None

    try:
        ar = adsk.fusion.RenderAspectRatios
        rendering.aspectRatio = ar.CustomRenderAspectRatio
        rendering.resolutionWidth = w
        rendering.resolutionHeight = h
        rendering.renderQuality = q
        fut = rendering.startLocalRender(path, cam)
    except Exception:
        return False

    deadline = time.monotonic() + max(5.0, float(timeout_sec))
    finished = adsk.fusion.LocalRenderStates.FinishedLocalRenderState
    failed = adsk.fusion.LocalRenderStates.FailedLocalRenderState

    try:
        while time.monotonic() < deadline:
            pump_ui()
            try:
                state = fut.renderState
                if state == finished:
                    return True
                if state == failed:
                    return False
            except Exception:
                return False
            time.sleep(max(0.05, float(poll_sec)))
    except Exception:
        return False
    return False
