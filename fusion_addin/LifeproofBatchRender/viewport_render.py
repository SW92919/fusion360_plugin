# Named views, geometry settling and Fusion local-render export for the batch.

from __future__ import annotations

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


# Seconds to let Fusion finish computing/tessellating a freshly opened model
# BEFORE the first render. ``importToNewDocument`` returns as soon as the
# document exists, but Fusion keeps refining the display mesh in the background.
# Ray-tracing against a still-coarse mesh bakes flat facets into the image —
# smooth arcs come out as a few straight segments ("hexagonal"), which is exactly
# the intermittent faceting the client reported. A short settle removes it.
GEOMETRY_SETTLE_AFTER_OPEN_SEC: float = 4

# Cheap top-up settle right before each capture (negligible next to a ray-trace).
GEOMETRY_SETTLE_BEFORE_CAPTURE_SEC: float = 0.5


def settle_geometry(
    app: adsk.core.Application,
    seconds: float,
    design: adsk.fusion.Design = None,
) -> float:
    """Let Fusion finish computing geometry / refining tessellation.

    Pumps the event loop and refreshes the viewport for up to ``seconds`` so the
    adaptive display mesh converges before we capture. When ``design`` is given,
    ``computeAll()`` is called first to force any pending geometry compute.
    Returns the seconds actually spent (0.0 if disabled).
    """
    budget = max(0.0, float(seconds))
    if budget <= 0.0:
        return 0.0

    if design is not None:
        compute_all = getattr(design, "computeAll", None)
        if callable(compute_all):
            try:
                compute_all()
            except Exception:
                pass

    start = time.monotonic()
    deadline = start + budget
    while time.monotonic() < deadline:
        try:
            app.activeViewport.refresh()
        except Exception:
            pass
        pump_ui()
        time.sleep(0.05)
    return time.monotonic() - start


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


def apply_named_view_framing(app: adsk.core.Application) -> None:
    """Show the activated named view exactly as the designer saved it.

    The batch renders only the Render Instructions' saved named views, so the
    camera is never re-aimed or re-fitted: the designer's composition (eye,
    target AND zoom, e.g. the rear's nose framed up-and-right) is what the
    client signed off on, and re-centering is what used to spoil it. This only
    refreshes the viewport.
    """
    try:
        app.activeViewport.refresh()
    except Exception:
        pass
    pump_ui()


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
        src = named_view.camera
        if src:
            # Assigning the NamedView's camera object wholesale does NOT stick on
            # this Fusion build (the viewport kept its default view for every
            # named view). Copy the eye/target/up onto the viewport's OWN camera
            # object and reassign — that applies reliably. isSmoothTransition off
            # so it snaps instantly (no mid-animation capture).
            cam = vp.camera
            try:
                cam.isSmoothTransition = False
            except Exception:
                pass
            for attr in ("eye", "target", "upVector"):
                try:
                    setattr(cam, attr, getattr(src, attr))
                except Exception:
                    pass
            try:
                cam.cameraType = src.cameraType
            except Exception:
                pass
            try:
                cam.viewExtents = src.viewExtents
            except Exception:
                pass
            vp.camera = cam
        vp.refresh()
        pump_ui()
        return True
    except Exception:
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
    camera=None,
) -> bool:
    """Ray-traced local export via ``Rendering.startLocalRender`` (Render workspace API).

    Uses the active viewport camera. Requires ``design.renderManager``; returns False if the
    API is unavailable or the render fails/timeouts.

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

    # Prefer an explicit camera (e.g. the named view's own camera) — on this
    # build assigning ``viewport.camera`` doesn't stick, so the viewport stays on
    # the default view for every named view. Passing the intended camera straight
    # to ``startLocalRender`` renders the correct view regardless of the viewport.
    cam = camera
    if cam is None:
        try:
            cam = app.activeViewport.camera
        except Exception:
            cam = None
    else:
        try:
            cam.isSmoothTransition = False
        except Exception:
            pass

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
