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

        s = max(0.20, min(2.35, float(extent_scale)))

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


def apply_named_view_framing(app: adsk.core.Application, view_name: str) -> None:
    """Fit visible geometry, then tune zoom / yaw by view name for consistent exports."""
    fit_view_to_model(app)
    vn = (view_name or "").lower()
    # Whole tokens only — avoids matching ``feedback``, ``backdrop``, etc.
    words = set(re.findall(r"[a-z0-9]+", vn))

    def _margin_scale(base: float) -> float:
        """``Full *`` views: never tighter than a balanced reference margin on all sides."""
        b = float(base)
        if "full" in words:
            return max(b, _BALANCED_MARGIN_MIN_SCALE)
        return b

    # Close / detail shots: modest zoom-in (still leaves some air vs old ultra-tight crops).
    if "close" in words:
        tighten_view_after_fit(app, _margin_scale(0.82))
        return

    # Rear angles on long thin parts read as a “line”; yaw slightly off-axis.
    if "back" in words and "front" not in words:
        yaw_orbit_camera_about_world_y(app, 18.0)
        tighten_view_after_fit(app, _margin_scale(1.36))
        return

    # Front-style hero shots — even corner space like manual / isometric references.
    if "front" in words and "back" not in words:
        tighten_view_after_fit(app, _margin_scale(1.36))
        return

    # True top/bottom captures — keep yaw (edge-on L / channel reads as a line if we orbit).
    if {"top", "bottom"}.intersection(words):
        tighten_view_after_fit(app, _margin_scale(1.28))
        return

    if {"trimetric"}.intersection(words):
        tighten_view_after_fit(app, _margin_scale(1.28))
        return

    _iso_tokens = (
        "iso",
        "isometric",
        "corner",
        "oblique",
        "perspective",
        "hero",
        "showcase",
    )
    axis_tokens = frozenset(("close", "front", "back"))
    if any(t in words for t in _iso_tokens):
        yaw_orbit_camera_about_world_y(app, 14.0)
        tighten_view_after_fit(app, _margin_scale(1.30))
        return

    if axis_tokens.isdisjoint(words):
        # Default / single “Viewport” captures: gentle ¾ orbit matches many product stills
        # (e.g. structural angles and L‑profiles that read as slabs when shot edge-on).
        yaw_orbit_camera_about_world_y(app, 11.0)

    tighten_view_after_fit(app, _margin_scale(1.32))


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
    render_quality: int = 90,
    timeout_sec: float = 1800.0,
    poll_sec: float = 0.25,
) -> bool:
    """Ray-traced local export via ``Rendering.startLocalRender`` (Render workspace API).

    Uses the active viewport camera. Requires ``design.renderManager``; returns False if the
    API is unavailable or the render fails/timeouts (caller may fall back to ``save_viewport_image``).
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
