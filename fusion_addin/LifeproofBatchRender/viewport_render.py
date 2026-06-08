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


def is_deliverable_view_name(name: str) -> bool:
    """True if a saved named view should be exported.

    Exports every designer-authored named view except obvious working/draft
    angles (iso, top, bottom, macro). Close-up / detail / zoom views are
    kept — multi-part models (e.g. End Cap + track bar) need them alongside
    full-length hero shots.
    """
    n = (name or "").lower()
    if any(tok in n for tok in EXCLUDE_VIEW_NAME_TOKENS):
        return False
    return True


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


def pitch_camera_toward_horizontal(
    app: adsk.core.Application, degrees: float
) -> bool:
    """Tilt the camera up/down around its horizontal right-axis through the
    target, WITHOUT changing the azimuth (the 3/4 spin around vertical).

    ``degrees`` > 0 lowers the eye toward the horizon → a low product-
    photography angle that shows the part's side profile / bullnose curve
    (the client's hero look). < 0 raises it back toward straight-down.
    Uses Rodrigues rotation around the (viewDir × up) axis so it works for
    any model up-axis. If the result tilts the wrong way, negate ``degrees``.
    """
    try:
        vp = app.activeViewport
        cam = vp.camera
        if cam is None:
            return False
        eye, target, up = cam.eye, cam.target, cam.upVector
        if not (eye and target and up):
            return False
        # View offset (target -> eye) and the camera's horizontal right axis.
        dx, dy, dz = eye.x - target.x, eye.y - target.y, eye.z - target.z
        kx = dy * up.z - dz * up.y
        ky = dz * up.x - dx * up.z
        kz = dx * up.y - dy * up.x
        kmag = math.sqrt(kx * kx + ky * ky + kz * kz)
        if kmag < 1e-9:
            return False
        kx, ky, kz = kx / kmag, ky / kmag, kz / kmag

        ang = math.radians(degrees)
        c, s = math.cos(ang), math.sin(ang)

        def _rodrigues(vx, vy, vz):
            dot = vx * kx + vy * ky + vz * kz
            cxx = ky * vz - kz * vy
            cxy = kz * vx - kx * vz
            cxz = kx * vy - ky * vx
            return (
                vx * c + cxx * s + kx * dot * (1.0 - c),
                vy * c + cxy * s + ky * dot * (1.0 - c),
                vz * c + cxz * s + kz * dot * (1.0 - c),
            )

        nx, ny, nz = _rodrigues(dx, dy, dz)
        ux, uy, uz = _rodrigues(up.x, up.y, up.z)
        cam.eye = adsk.core.Point3D.create(
            target.x + nx, target.y + ny, target.z + nz
        )
        cam.upVector = adsk.core.Vector3D.create(ux, uy, uz)
        cam.isSmoothTransition = False
        vp.camera = cam
        vp.refresh()
        pump_ui()
        return True
    except Exception:
        return False


# When True, prefer the .f3d's own SAVED NAMED VIEWS over any computed angle.
# For the deliverable templates the designer baked the exact product cameras
# into the file (named "Nose Front" / "Nose Rear", etc.) — they ARE the
# cameras the client's reference renders were made from, so reproducing them
# by hand from azimuth/elevation guesses is error-prone and never matches.
# This is fully automatic: the plugin reads the views stored in the file; the
# client does nothing manual. When a model has NO saved named views, the
# batch falls back to the computed iso hero angles below.
PREFER_SAVED_NAMED_VIEWS: bool = True

# Fallback-only now: computed product-hero angles are used when a model has no
# usable saved named views (see PREFER_SAVED_NAMED_VIEWS). Left True so that
# view-less models still get a nice low 3/4 hero shot instead of a flat
# default viewport grab.
FORCE_ISOMETRIC_VIEW: bool = True

# When honoring saved named views we export every view in the .f3d except
# obvious working/draft angles listed here. Close-up / detail / zoom views
# are product deliverables for assemblies like End Cap + track bar.
# Cap count via the dialog's "Max named views" or by reordering views in Fusion.
EXCLUDE_VIEW_NAME_TOKENS = ("macro", "iso", "top", "bottom")

# When True, every image is a near-instant VIEWPORT screenshot — the
# ray-traced ``Rendering.startLocalRender`` path is never used, regardless
# of the dialog's render-backend selection. This is the anti-freeze switch:
# ray tracing 9 images is what locked Fusion up. The viewport already shows
# the applied decal texture, so captures are clean product stills with only
# slightly flatter lighting (no ray-traced GI / soft shadows). Set False to
# allow the dialog-selected ray-traced backend again.
FORCE_VIEWPORT_CAPTURE: bool = True

# Distinct product-hero angles rendered per color set when
# FORCE_ISOMETRIC_VIEW is on. Each entry is
# ``(label, azimuth_degrees, elevation_degrees)`` for set_product_hero_camera:
#   * azimuth  = rotation around the part's UP axis. 0 = straight at the
#     front (length runs flat across the frame); +/- swings to a 3/4 from
#     the right / left. Sign flips which end of the nose faces the camera —
#     if the bullnose faces away, negate the azimuths (or add/subtract 180).
#   * elevation = height above the ground plane. ~18-22° = the client's low
#     hero look showing the rounded nose profile; 90 = straight top-down.
# The label becomes the ``{named view}`` token in the output filename.
# Client deliverable = TWO views: the bullnose from the FRONT (convex nose
# toward camera) and from the REAR (looking across the tread toward the
# nose, showing the underside curl). They sit on opposite sides of the
# part's depth axis → azimuths ~180° apart, same low elevation. The labels
# become the filename suffix, matching the client's "... - Nose Front.png"
# / "... - Nose Rear.png" pattern.
#   * If Front/Rear look swapped, swap the two azimuths (or +/- 180).
#   * If a view shows the heel end instead of the nose, nudge that azimuth.
# FINAL 2-VIEW DELIVERABLE (exactly two output files per model):
#   Nose Front = (35, 22) — locked, matches the client sample: camera ABOVE
#     (+elevation) looking down at the top surface + convex nose.
#   Nose Rear  = (35, -18) — SAME azimuth as the front (so the nose end stays
#     on the LEFT, matching the client pair), but elevation goes NEGATIVE so
#     the camera drops BELOW the part and looks UP into the open underside
#     hollow / return. A 180° azimuth swing was wrong: it flipped the nose to
#     the right AND, at +elevation, still showed the top instead of the
#     hollow. The underside only reveals at negative elevation.
#   * Rear not deep enough into the hollow? Make the -18 more negative.
#   * Rear shows too much underside / too little top? Raise toward 0.
ISO_VIEW_ORIENTATIONS = (
    ("Nose Front", 35.0, 22.0),
    ("Nose Rear", 35.0, -18.0),
)

# Zoom factor when framing on the PART's own bounding box (see
# frame_part_centered). It scales the bounding-SPHERE fit, and a long thin
# moulding only occupies a thin diagonal slice of that sphere, so values
# well below 1.0 are correct and safe (the thin cross-section never clips):
#   1.0  = whole bounding sphere fits (part looks tiny — the old problem)
#   0.55 = part fills ~80% of the frame, matching the client's sample
#   lower = bigger (0.40 ran off the edges); higher = smaller (1.05 was tiny).
ISO_VIEW_MARGIN_SCALE: float = 0.55

# Zoom factor for the SAVED-NAMED-VIEW path (apply_named_view_framing). Same
# meaning as ISO_VIEW_MARGIN_SCALE: it keeps the designer's exact view angle
# and only sizes the part in frame. <1 = bigger (fills more). The client's
# Nose Front/Rear fill ~85% of the frame, so 0.62 enlarges the part to match.
# Raise toward 1.0 for more margin; lower for a tighter crop.
NAMED_VIEW_MARGIN_SCALE: float = 0.62

# When True, a saved named view is reproduced EXACTLY as the designer framed
# it — full camera (eye + target + zoom), no re-centering. The client's
# reference renders were made from these very views, so re-centering on the
# part's bbox would drag the designer's deliberate composition back to dead-
# center (e.g. it pulled the "Full Back" view down-left instead of leaving the
# nose up-and-right like the sample). NAMED_VIEW_MARGIN_SCALE is then ignored
# for these views. Set False to fall back to angle-preserving re-centering
# (used only if a saved view frames the part badly / clips it).
PRESERVE_NAMED_VIEW_COMPOSITION: bool = True

# Degrees to LOWER the camera from Fusion's steep ~35° isometric toward a
# low product-photography angle (the client's hero shots sit ~18-20° above
# horizontal, showing the rounded bullnose profile instead of a flat top-
# down view). Applied after the orientation + yaw, before the fit.
#   ~16 brings 35° iso down to ~19°.  0 = keep the standard steep iso.
#   If a render tilts the WRONG way (camera goes more overhead), negate this.
ISO_VIEW_PITCH_DOWN_DEGREES: float = 16.0


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


def _visible_part_bounds(design):
    """World AABB of the visible PRODUCT geometry as ``(xmin, ymin, zmin,
    xmax, ymax, zmax)`` or ``None``. Unions visible root bodies + visible,
    non-light sub-occurrences (excludes hidden alternate treads/risers and
    light/helper proxies) — the exact set that gets rendered.
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
            except Exception:
                continue
            # Union the occurrence's BODY bounding boxes (proxy bodies are in
            # world/assembly space). Occurrence.boundingBox is unreliable on
            # this build (returns nothing), which made the whole bounds empty
            # for occurrence-based assemblies like the Bullnose → the camera
            # fell back to ViewFit-with-light and rendered off-centre.
            try:
                obodies = occ.bRepBodies
            except Exception:
                obodies = None
            if obodies is not None:
                for bi in range(obodies.count):
                    try:
                        ob = obodies.item(bi)
                        if not _frame_entity_visible(ob):
                            continue
                        _union(ob.boundingBox)
                    except Exception:
                        continue
            # Fall back to occ.boundingBox if bodies gave nothing.
            try:
                if not have:
                    _union(occ.boundingBox)
            except Exception:
                pass

    except Exception:
        pass

    if not have:
        return None
    return (xmin, ymin, zmin, xmax, ymax, zmax)


def set_product_hero_camera(
    app: adsk.core.Application,
    design,
    elevation_deg: float = 20.0,
    azimuth_deg: float = 30.0,
) -> bool:
    """Aim a low product-photography camera built from the part's own axes.

    Identifies the part's thickness (smallest bbox extent → "up"), length
    (largest) and width (middle → "front") directions, then places the eye
    at ``elevation_deg`` above the ground plane and ``azimuth_deg`` rotated
    from straight-on-the-front toward the length. This reproduces the
    client's hero composition (part lying roughly flat/horizontal, low angle
    showing the rounded nose profile) regardless of how the model's axes are
    oriented. ``frame_part_centered`` should be called afterwards to set the
    distance / zoom. Returns False if no visible geometry is found.
    """
    bounds = _visible_part_bounds(design)
    if bounds is None:
        return False
    xmn, ymn, zmn, xmx, ymx, zmx = bounds
    center = adsk.core.Point3D.create(
        (xmn + xmx) / 2.0, (ymn + ymx) / 2.0, (zmn + zmx) / 2.0
    )
    exts = [
        (xmx - xmn, (1.0, 0.0, 0.0)),
        (ymx - ymn, (0.0, 1.0, 0.0)),
        (zmx - zmn, (0.0, 0.0, 1.0)),
    ]
    exts.sort(key=lambda t: t[0])
    up_v = exts[0][1]      # thinnest extent → up (part lies flat on this)
    front_v = exts[1][1]   # middle extent → width, faces the camera
    long_v = exts[2][1]    # largest extent → length, runs across the frame

    e = math.radians(elevation_deg)
    a = math.radians(azimuth_deg)
    ce, se, ca, sa = math.cos(e), math.sin(e), math.cos(a), math.sin(a)

    # Horizontal look-from direction: front blended toward length by azimuth.
    hx = ca * front_v[0] + sa * long_v[0]
    hy = ca * front_v[1] + sa * long_v[1]
    hz = ca * front_v[2] + sa * long_v[2]
    # Eye direction (target → eye): horizontal lifted toward up by elevation.
    ex = ce * hx + se * up_v[0]
    ey = ce * hy + se * up_v[1]
    ez = ce * hz + se * up_v[2]
    em = math.sqrt(ex * ex + ey * ey + ez * ez)
    if em < 1e-9:
        return False
    ex, ey, ez = ex / em, ey / em, ez / em

    diag = math.sqrt(
        (xmx - xmn) ** 2 + (ymx - ymn) ** 2 + (zmx - zmn) ** 2
    ) or 10.0

    # Up vector = thickness axis projected perpendicular to the view dir.
    dot = up_v[0] * ex + up_v[1] * ey + up_v[2] * ez
    ux, uy, uz = up_v[0] - dot * ex, up_v[1] - dot * ey, up_v[2] - dot * ez
    um = math.sqrt(ux * ux + uy * uy + uz * uz)
    if um < 1e-9:
        ux, uy, uz = 0.0, 0.0, 1.0
    else:
        ux, uy, uz = ux / um, uy / um, uz / um

    try:
        vp = app.activeViewport
        cam = vp.camera
        if cam is None:
            return False
        cam.target = center
        cam.eye = adsk.core.Point3D.create(
            center.x + ex * diag * 2.0,
            center.y + ey * diag * 2.0,
            center.z + ez * diag * 2.0,
        )
        cam.upVector = adsk.core.Vector3D.create(ux, uy, uz)
        cam.isSmoothTransition = False
        vp.camera = cam
        vp.refresh()
        pump_ui()
        return True
    except Exception:
        return False


def _visible_root_part_bbox(design):
    """World-space bbox center + radius of the visible PRODUCT geometry,
    derived from ``_visible_part_bounds``. Returns ``(Point3D, radius)`` or
    ``None`` (caller falls back to ViewFit).
    """
    bounds = _visible_part_bounds(design)
    if bounds is None:
        return None
    xmn, ymn, zmn, xmx, ymx, zmx = bounds
    cx = (xmn + xmx) / 2.0
    cy = (ymn + ymx) / 2.0
    cz = (zmn + zmx) / 2.0
    radius = 0.5 * math.sqrt(
        (xmx - xmn) ** 2 + (ymx - ymn) ** 2 + (zmx - zmn) ** 2
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
    azimuth_degrees: float = 30.0,
    elevation_degrees: float = 20.0,
    pad: float = ISO_VIEW_MARGIN_SCALE,
) -> None:
    """Aim a low product-hero camera (built from the part's own axes) and
    frame it tightly centred on the part.

    Replaces the old fixed-iso-corner + yaw + pitch approach, which was
    anchored to the model's arbitrary axis orientation and rendered the part
    corner-to-corner. ``set_product_hero_camera`` instead derives the part's
    length / width / thickness and places a controllable low-angle 3/4 shot
    matching the client's reference. Falls back to a standard iso + ViewFit
    only if the part's bounds can't be computed.
    """
    if set_product_hero_camera(app, design, elevation_degrees, azimuth_degrees):
        frame_part_centered(app, design, pad)
        return
    # Fallback: no usable bounds — old steep iso so we still produce an image.
    set_isometric_camera(app, "IsoTopRightViewOrientation", 0.0)
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
    design=None,
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
        # Trust the authored named-view camera. The caller already applied the
        # designer's exact camera via activate_named_view.
        if PRESERVE_NAMED_VIEW_COMPOSITION:
            # Reproduce the saved view EXACTLY — eye, target AND zoom — so the
            # designer's deliberate composition (e.g. the rear's nose framed
            # up-and-right) is preserved. Re-centering here is what pulled it
            # back to dead-center. Just refresh; do not touch the camera.
            try:
                app.activeViewport.refresh()
            except Exception:
                pass
            pump_ui()
            return
        # Legacy fallback: keep the authored view direction/up but recenter +
        # size onto the visible part. frame_part_centered excludes the
        # light/helpers (ViewFit would include them and shove the part
        # off-centre); fall back to a light ViewFit if bounds are missing.
        if design is not None and frame_part_centered(
            app, design, NAMED_VIEW_MARGIN_SCALE
        ):
            return
        fit_view_to_model(app)
        tighten_view_after_fit(app, NAMED_VIEW_MARGIN_SCALE)
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
