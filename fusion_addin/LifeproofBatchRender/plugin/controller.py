from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

import adsk.core
import adsk.fusion

import batch_config
import support_paths
import texture_pipeline
import visibility_apply
import viewport_render

from plugin import image_mapper
from plugin import logger as logutil
from plugin import model_handler
from plugin import renderer_aps
from plugin import task_manager
from plugin import ui as uip


def _log_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append_log(log_path: Path, row: List[str]) -> None:
    new_file = not log_path.exists()
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "model", "color_set", "view", "output", "success", "backend"])
        w.writerow(row)


def _backend_label(
    render_backend: str,
    used_aps: bool,
    used_fallback: bool,
    *,
    local_capture: str = "viewport",
) -> str:
    """local_capture: which local exporter wrote pixels — ``fusion`` or ``viewport``."""
    if "APS" in (render_backend or "").upper():
        if used_aps:
            return "aps"
        if used_fallback:
            return "aps->local_fusion" if local_capture == "fusion" else "aps->local_viewport"
        return "aps_failed"
    return "local_fusion" if local_capture == "fusion" else "local_viewport"


def _save_local_image(
    design: adsk.fusion.Design,
    app: adsk.core.Application,
    out_path: Path,
    render_width: int,
    render_height: int,
    render_backend: str,
) -> Tuple[bool, str]:
    """Returns ``(ok, local_capture)`` where ``local_capture`` is ``fusion`` or ``viewport``.

    Fusion ``Rendering.startLocalRender`` is the default local path; viewport capture is only
    used when that backend is explicitly selected, or as fallback after Fusion fails.

    ``FORCE_VIEWPORT_CAPTURE`` (viewport_render) hard-overrides this: when True
    we ALWAYS use the near-instant viewport screenshot and never trigger a
    ray-traced local render, regardless of the dialog's backend selection.
    This is what keeps Fusion from freezing on 9-image batches.
    """
    path = str(out_path.resolve())
    force_viewport = bool(
        getattr(viewport_render, "FORCE_VIEWPORT_CAPTURE", False)
    )
    viewport_only = force_viewport or (
        (render_backend or "").strip() == uip.RENDER_BACKEND_LOCAL_VIEWPORT
    )
    if not viewport_only:
        if viewport_render.save_fusion_local_render(design, app, path, render_width, render_height):
            return True, "fusion"
        if viewport_render.save_viewport_image(app, path, render_width, render_height):
            return True, "viewport"
        return False, "viewport"
    if viewport_render.save_viewport_image(app, path, render_width, render_height):
        return True, "viewport"
    return False, "viewport"


def execute_batch(
    fui: adsk.core.UserInterface,
    ins: adsk.core.CommandInputs,
    texture_root: Path,
    models: List[Path],
    render_width: int,
    render_height: int,
    output_ext: str,
    pipeline_selection: str,
    render_backend: str,
    concurrency: int,
    aps_fallback: bool,
    max_named_views: int,
    decal_scale_plane_xy: float,
    addin_dir: Path,
) -> None:
    log_path = logutil.default_log_path(texture_root)
    logutil.append_log(log_path, "execute_batch start backend={}".format(render_backend))

    texture_pipeline.set_runtime_decal_scale_plane_xy(decal_scale_plane_xy)
    try:
        _execute_batch_inner(
            fui,
            ins,
            texture_root,
            models,
            render_width,
            render_height,
            output_ext,
            pipeline_selection,
            render_backend,
            concurrency,
            aps_fallback,
            max_named_views,
            decal_scale_plane_xy,
            addin_dir,
            log_path,
        )
    finally:
        texture_pipeline.set_runtime_decal_scale_plane_xy(None)


def _execute_batch_inner(
    fui: adsk.core.UserInterface,
    ins: adsk.core.CommandInputs,
    texture_root: Path,
    models: List[Path],
    render_width: int,
    render_height: int,
    output_ext: str,
    pipeline_selection: str,
    render_backend: str,
    concurrency: int,
    aps_fallback: bool,
    max_named_views: int,
    decal_scale_plane_xy: float,
    addin_dir: Path,
    log_path: Path,
) -> None:

    try:
        color_sets = support_paths.scan_texture_root(texture_root)
    except Exception as ex:
        fui.messageBox("Invalid texture folder:\n{}".format(ex))
        logutil.append_log(log_path, "scan_texture_root failed", ex)
        return
    if not color_sets:
        fui.messageBox("No color subfolders found under:\n{}".format(texture_root))
        return
    if not models:
        fui.messageBox("No model files selected.")
        return

    texture_pipeline.clear_decal_shift_temp_files()

    aps_cfg = renderer_aps.load_aps_config(addin_dir)
    token = ""
    aps_executor: Optional[task_manager.BoundedExecutor] = None
    aps_bucket_key = ""
    if "APS" in (render_backend or "").upper():
        tr = renderer_aps.fetch_two_legged_token(aps_cfg)
        if not tr.ok:
            msg = "APS authentication failed:\n{}\n\n".format(tr.error)
            if aps_fallback:
                fui.messageBox(msg + "Falling back to local rendering.")
                render_backend = uip.RENDER_BACKEND_LOCAL_FUSION
            else:
                fui.messageBox(msg + "Enable fallback or fix aps_config.json.")
                return
        else:
            token = tr.access_token
            logutil.append_log(log_path, "APS token acquired (expires_in={})".format(tr.expires_in))
            ready, ready_msg = renderer_aps.verify_aps_setup(aps_cfg, token)
            logutil.append_log(log_path, ready_msg)
            aps_bucket_key = renderer_aps.resolve_bucket_key(aps_cfg)
            if not ready:
                if aps_fallback:
                    fui.messageBox(
                        "APS setup failed:\n{}\n\nFalling back to local rendering.".format(ready_msg)
                    )
                    render_backend = uip.RENDER_BACKEND_LOCAL_FUSION
                    token = ""
                else:
                    fui.messageBox("APS setup failed:\n{}\nEnable fallback or fix aps_config.json.".format(ready_msg))
                    return
            else:
                aps_executor = task_manager.BoundedExecutor(concurrency)

    app = adsk.core.Application.get()
    csv_log = texture_root / "_LifeproofBatchRender_log.csv"
    summary_lines: List[str] = [
        "Texture root: {}".format(texture_root),
        "Color sets: {}".format(len(color_sets)),
        "Models: {}".format(len(models)),
        "Output: {} × {} {}".format(render_width, render_height, output_ext.upper()),
        "Pipeline mode: {}".format(pipeline_selection),
        "Render backend: {}".format(render_backend),
        *(
            [
                "APS bucket: {}".format(aps_bucket_key or "(none)"),
                "APS render_mode: {}".format(aps_cfg.render_mode),
            ]
            if token
            else []
        ),
        (
            "Max named views per color set: 0 (all views in each design)"
            if max_named_views <= 0
            else "Max named views per color set: {} (first {} views in design order only)".format(
                max_named_views, max_named_views
            )
        ),
        "Decal Scale Plane XY: {:.2f}".format(decal_scale_plane_xy),
        "",
        "Delivery swatches (texture copies per color folder):",
    ]
    for cs in color_sets:
        for purge_line in support_paths.purge_ignored_texture_sidecars(cs.folder):
            summary_lines.append("  {}".format(purge_line))
        _ns, sw_lines = support_paths.write_color_set_delivery_swatches(cs)
        summary_lines.extend(["  {}".format(x) for x in sw_lines])
    summary_lines.append("")

    renders_ok = 0
    renders_fail = 0
    aps_render_ok = 0
    aps_fallback_count = 0

    total_steps: Optional[int] = None
    done_steps = 0

    def bump_progress(msg: str) -> None:
        uip.set_status(ins, msg)
        try:
            pct = 0 if not total_steps else int(min(100, (100 * done_steps) // max(1, total_steps)))
            uip.set_progress(ins, pct, 100)
        except Exception:
            pass
        viewport_render.pump_ui()

    for mp in models:
        mode = batch_config.resolve_pipeline_mode(
            pipeline_selection,
            mp,
            support_paths.infer_texture_mode,
        )
        model_stem = mp.stem
        summary_lines.append("=== {} → {} ===".format(mp.name, mode))

        doc, err = model_handler.open_model(app, mp)
        if not doc:
            summary_lines.append(err or "Open failed.")
            continue

        design = model_handler.active_design(app)
        if not design:
            summary_lines.append("No active design.")
            continue

        viewport_render.prepare_clean_render_view(app)

        root = design.rootComponent
        summary_lines.append(
            "  Named-view visibility rules: {}".format(
                "ON (hide:/show: in descriptions)" if texture_pipeline.APPLY_NAMED_VIEW_VISIBILITY else "OFF (use file visibility as opened)"
            )
        )
        view_tasks, vmsg = model_handler.named_view_tasks(design)
        if vmsg:
            summary_lines.append(vmsg)
        n_named_views_in_design = len(view_tasks)
        if max_named_views > 0 and len(view_tasks) > max_named_views:
            view_tasks = view_tasks[:max_named_views]
            summary_lines.append(
                "  Named views in design: {}; exporting first {} only (UI max)".format(
                    n_named_views_in_design, max_named_views
                )
            )

        # View source: PREFER the .f3d's own saved named views (the designer's
        # exact "Nose Front"/"Nose Rear" product cameras the client signed off
        # on). Only fall back to computed low-angle 3/4 hero shots when the
        # model has NO saved named views. Honoring the saved views is fully
        # automatic — the plugin reads them from the file, nothing manual.
        prefer_named = bool(getattr(viewport_render, "PREFER_SAVED_NAMED_VIEWS", True))
        have_named_views = n_named_views_in_design > 0
        use_saved_named_views = prefer_named and have_named_views
        force_iso = (not use_saved_named_views) and bool(
            getattr(viewport_render, "FORCE_ISOMETRIC_VIEW", False)
        )
        iso_label_to_cam: dict = {}
        if use_saved_named_views:
            # Drop working/draft views (iso, top, bottom, macro). Keep close-up
            # and full-length hero views — multi-part models need both.
            _is_deliverable = getattr(viewport_render, "is_deliverable_view_name", None)
            if callable(_is_deliverable):
                kept = [(nv, name) for (nv, name) in view_tasks if _is_deliverable(name)]
                dropped = [name for (nv, name) in view_tasks if not _is_deliverable(name)]
                if kept:
                    view_tasks = kept
                    if dropped:
                        summary_lines.append(
                            "  Saved views filtered — dropped working views: {}".format(
                                ", ".join(dropped)
                            )
                        )
            summary_lines.append(
                "  Camera: honoring {} saved named view(s) from the file "
                "(designer's product cameras): {}".format(
                    len(view_tasks),
                    ", ".join(str(vn_name) for _nv, vn_name in view_tasks),
                )
            )
        if force_iso:
            iso_orients = list(getattr(viewport_render, "ISO_VIEW_ORIENTATIONS", ()))
            if not iso_orients:
                iso_orients = [("Front 3-4", 30.0, 20.0)]
            # Each entry is (label, azimuth_degrees, elevation_degrees).
            iso_label_to_cam = {
                row[0]: (
                    float(row[1]) if len(row) > 1 else 30.0,
                    float(row[2]) if len(row) > 2 else 20.0,
                )
                for row in iso_orients
            }
            view_tasks = [(None, row[0]) for row in iso_orients]
            summary_lines.append(
                "  Camera: forced low product-hero angles — {} per color set "
                "({} saved named view(s) ignored): {}".format(
                    len(view_tasks),
                    n_named_views_in_design,
                    ", ".join(row[0] for row in iso_orients),
                )
            )

        an, dn = texture_pipeline.list_appearance_and_decal_names(design)
        summary_lines.append(
            "Template targets - Appearances ({}): {} | Decals ({}): {}".format(
                len(an),
                ", ".join(an) if an else "(none)",
                len(dn),
                ", ".join(dn) if dn else "(none)",
            )
        )

        if total_steps is None:
            total_steps = max(1, len(models) * len(color_sets) * max(1, len(view_tasks)))

        occ_snap, body_snap, mesh_snap, decal_snap = visibility_apply.capture_visibility(root)
        batch_hide_tokens = visibility_apply.collect_batch_hide_persistent_tokens(root)

        carrier: Optional[adsk.fusion.Appearance] = None
        body_appearance_snap: list = []
        batch_decals: List[Any] = []
        use_decal_coverage = (
            texture_pipeline.FORCE_BODY_COVERAGE
            and texture_pipeline.BODY_COVERAGE_VIA_DECALS
        )

        if use_decal_coverage:
            first_image = None
            for cs in color_sets:
                if cs.slot1:
                    first_image = cs.slot1
                    break
            if first_image is None:
                summary_lines.append(
                    "  Decal projection skipped: no color-set image available"
                )
            else:
                _neutralize = getattr(
                    texture_pipeline, "neutralize_dark_body_appearances", None
                )
                body_appearance_snap = texture_pipeline.capture_body_appearances(
                    design
                )
                if callable(_neutralize):
                    _, neut_lines = _neutralize(design)
                    for line in neut_lines:
                        summary_lines.append("  " + line)
                batch_decals, decal_lines = texture_pipeline.create_batch_decals_for_all_bodies(
                    design, first_image, appearance_snap=body_appearance_snap, model_path=mp
                )
                for line in decal_lines:
                    summary_lines.append("  " + line)
        elif texture_pipeline.FORCE_BODY_COVERAGE:
            summary_lines.append("  Pre-batch appearance audit:")
            for line in texture_pipeline.audit_design_appearances(design):
                summary_lines.append("    " + line)

            test_image = None
            for cs in color_sets:
                if cs.slot1:
                    test_image = cs.slot1
                    break
            carrier, carrier_lines = texture_pipeline.get_or_create_carrier(
                design, test_image=test_image
            )
            for line in carrier_lines:
                summary_lines.append("  Carrier setup: {}".format(line))
            if carrier is not None:
                body_appearance_snap = texture_pipeline.capture_body_appearances(design)
                n_ok, n_fail, body_lines = texture_pipeline.apply_carrier_to_all_bodies(
                    design, carrier
                )
                summary_lines.append(
                    "  Carrier '{}' force-assigned: bodies {} OK / {} failed".format(
                        carrier.name, n_ok, n_fail
                    )
                )
                for line in body_lines:
                    summary_lines.append("    {}".format(line))

                summary_lines.append("  Post-carrier appearance audit:")
                for line in texture_pipeline.audit_design_appearances(design):
                    summary_lines.append("    " + line)
            else:
                # No carrier means this Fusion build exposes no usable
                # appearance texture slots (every addByCopy probe failed), so
                # the per-name appearance match can't texture solid materials
                # like the end-cap "Paint - Metallic (Black)". Decals are the
                # only mechanism that works here — auto-fall back to projecting
                # batch tile decals onto every face so coverage is complete.
                summary_lines.append(
                    "  Carrier setup: NONE found - auto-falling back to "
                    "batch-decal coverage (build has no texture slots)"
                )
                fb_image = test_image
                if fb_image is None:
                    summary_lines.append(
                        "  Decal fallback skipped: no color-set image available"
                    )
                else:
                    # Neutralize dark/reflective body appearances BEFORE decals
                    # project, so any decal gap shows matte grey instead of a
                    # black-metallic or shiny-steel stripe. Snapshot is
                    # restored at end-of-batch via body_appearance_snap.
                    # getattr-guarded so a texture_pipeline version that lacks
                    # this helper degrades gracefully instead of hard-crashing.
                    _neutralize = getattr(
                        texture_pipeline, "neutralize_dark_body_appearances", None
                    )
                    body_appearance_snap = texture_pipeline.capture_body_appearances(
                        design
                    )
                    if callable(_neutralize):
                        _, neut_lines = _neutralize(design)
                        for line in neut_lines:
                            summary_lines.append("  " + line)
                    batch_decals, decal_lines = (
                        texture_pipeline.create_batch_decals_for_all_bodies(
                            design, fb_image, appearance_snap=body_appearance_snap, model_path=mp
                        )
                    )
                    for line in decal_lines:
                        summary_lines.append("  " + line)

        zero_texture_color_sets: List[str] = []

        for cs in color_sets:
            if not cs.slot1:
                summary_lines.append("Skip color '{}': no _1 / first image.".format(cs.folder.name))
                continue

            bump_progress("Textures: {} / {}".format(model_stem, cs.folder.name))

            s1, s2 = image_mapper.effective_texture_slots(design, mode, cs.slot1, cs.slot2)
            n_tex = 0
            tex_lines: List[str] = []
            if batch_decals:
                decal_image = s1 or cs.slot1
                n_tex, dec_lines, batch_decals = texture_pipeline.update_batch_decal_images(
                    batch_decals, decal_image
                )
                tex_lines.extend(dec_lines)
                if mode == "appearance":
                    n_app, app_lines = texture_pipeline.apply_hybrid_appearance_for_batch(
                        design, s1, s2
                    )
                    n_tex += n_app
                    tex_lines.extend(app_lines)
                # Optionally also swap imageFilename on user-authored decals
                # in the .f3d. Disabled by default — every extra decal write
                # triggers a Fusion re-projection that grows the freeze
                # window, and with the batch tile coverage we usually don't
                # need them. Toggle UPDATE_USER_AUTHORED_DECALS in
                # texture_pipeline.py to re-enable.
                if texture_pipeline.UPDATE_USER_AUTHORED_DECALS:
                    try:
                        batch_ids = set(id(d) for d in batch_decals)
                    except Exception:
                        batch_ids = set()
                    n_user, user_lines = texture_pipeline.update_user_authored_decals(
                        design, decal_image, batch_ids
                    )
                    n_tex += n_user
                    tex_lines.extend(user_lines)
            else:
                n_tex, tex_lines = texture_pipeline.apply_color_set_for_open_design(
                    mode, s1, s2, carrier=carrier
                )
            if n_tex == 0:
                zero_texture_color_sets.append(cs.folder.name)
            else:
                for line in tex_lines:
                    summary_lines.append("  {} | {}".format(cs.folder.name, line))

            viewport_render.pump_ui()
            try:
                app.activeViewport.refresh()
            except Exception:
                pass

            for nv, view_name in view_tasks:
                visibility_apply.restore_visibility(
                    occ_snap, body_snap, mesh_snap, decal_snap
                )
                if texture_pipeline.APPLY_NAMED_VIEW_VISIBILITY:
                    visibility_apply.apply_visibility_for_named_view(root, view_name)
                visibility_apply.apply_batch_render_geometry_hides(root, batch_hide_tokens)
                if force_iso:
                    _az, _el = iso_label_to_cam.get(view_name, (30.0, 20.0))
                    viewport_render.apply_isometric_view_framing(
                        app, design, _az, _el
                    )
                elif nv is not None:
                    viewport_render.activate_named_view(app, nv)
                    viewport_render.apply_named_view_framing(
                        app, view_name, preserve_orientation=True, design=design
                    )
                else:
                    try:
                        app.activeViewport.refresh()
                    except Exception:
                        pass
                    viewport_render.apply_named_view_framing(app, view_name or "Viewport")
                viewport_render.pump_ui()
                visibility_apply.apply_batch_render_geometry_hides(root, batch_hide_tokens)
                viewport_render.pump_ui()
                # Reframe once helpers/lights are off — otherwise ViewFit bounds still
                # include proxy solids hidden after the first fit (wrong crop vs manual hide).
                if force_iso:
                    _az, _el = iso_label_to_cam.get(view_name, (30.0, 20.0))
                    viewport_render.apply_isometric_view_framing(
                        app, design, _az, _el
                    )
                elif nv is not None:
                    # Re-apply the designer's exact saved camera right before
                    # capture so the visibility toggling above can't nudge the
                    # composition (PRESERVE_NAMED_VIEW_COMPOSITION trusts it 1:1).
                    viewport_render.activate_named_view(app, nv)
                    viewport_render.apply_named_view_framing(
                        app, view_name, preserve_orientation=True, design=design
                    )
                else:
                    viewport_render.apply_named_view_framing(app, view_name or "Viewport")
                viewport_render.pump_ui()

                # Last-chance hide after ``apply_named_view_framing`` (camera updates can
                # resurrect Render-proxy meshes until bulbs are toggled again).
                visibility_apply.apply_batch_render_geometry_hides(root, batch_hide_tokens)
                viewport_render.pump_ui()

                base = support_paths.build_output_basename(
                    model_stem,
                    cs.output_image_stem,
                    view_name,
                )
                out_path = support_paths.versioned_path(cs.folder / (base + output_ext))

                used_aps = False
                used_fallback = False
                ok = False
                local_capture = "viewport"

                if "APS" in (render_backend or "").upper() and token:
                    outcome = renderer_aps.submit_render_job(
                        aps_cfg,
                        token,
                        design=design,
                        app=app,
                        model_path=mp,
                        color_folder=cs.folder,
                        color_name=cs.folder.name,
                        view_name=view_name,
                        width=render_width,
                        height=render_height,
                        model_stem=model_stem,
                        slot_paths=[s1 or cs.slot1, s2],
                    )
                    if outcome.ok and outcome.output_bytes:
                        try:
                            out_path.write_bytes(outcome.output_bytes)
                            ok = True
                            used_aps = True
                            aps_render_ok += 1
                            if outcome.job_prefix:
                                summary_lines.append(
                                    "  APS job: oss://{}/{}".format(
                                        outcome.bucket_key or aps_bucket_key,
                                        outcome.job_prefix,
                                    )
                                )
                        except Exception as ex:
                            ok = False
                            summary_lines.append("APS write failed: {}".format(ex))
                    elif aps_fallback:
                        aps_fail_msg = (outcome.message or "APS render failed (no message).").strip()
                        summary_lines.append(
                            "  APS fallback ({} | {}): {}".format(
                                cs.folder.name, view_name, aps_fail_msg
                            )
                        )
                        logutil.append_log(
                            log_path,
                            "APS fallback model={} color={} view={} reason={}".format(
                                model_stem, cs.folder.name, view_name, aps_fail_msg
                            ),
                        )
                        ok, local_capture = _save_local_image(
                            design,
                            app,
                            out_path,
                            render_width,
                            render_height,
                            uip.RENDER_BACKEND_LOCAL_FUSION,
                        )
                        used_fallback = True
                        aps_fallback_count += 1
                    else:
                        ok = False
                        aps_fail_msg = (outcome.message or "APS render failed.").strip()
                        summary_lines.append(
                            "  APS failed ({} | {}): {}".format(
                                cs.folder.name, view_name, aps_fail_msg
                            )
                        )
                        logutil.append_log(
                            log_path,
                            "APS failed model={} color={} view={} reason={}".format(
                                model_stem, cs.folder.name, view_name, aps_fail_msg
                            ),
                        )
                else:
                    ok, local_capture = _save_local_image(
                        design,
                        app,
                        out_path,
                        render_width,
                        render_height,
                        render_backend,
                    )

                ts = _log_timestamp()
                backend = _backend_label(
                    render_backend,
                    used_aps,
                    used_fallback,
                    local_capture=local_capture,
                )
                try:
                    _append_log(
                        csv_log,
                        [ts, model_stem, cs.folder.name, view_name, str(out_path), "1" if ok else "0", backend],
                    )
                except Exception:
                    pass
                if ok:
                    renders_ok += 1
                else:
                    renders_fail += 1

                done_steps += 1
                bump_progress("Saved: {} ({})".format(out_path.name, backend))

                visibility_apply.restore_visibility(
                    occ_snap, body_snap, mesh_snap, decal_snap
                )
                viewport_render.pump_ui()

        visibility_apply.restore_visibility(occ_snap, body_snap, mesh_snap, decal_snap)

        if batch_decals:
            if texture_pipeline.SKIP_BATCH_DECAL_CLEANUP:
                summary_lines.append(
                    "  Decal teardown: skipped (SKIP_BATCH_DECAL_CLEANUP=True — inspect in Fusion)"
                )
            else:
                n_removed = texture_pipeline.cleanup_batch_decals(batch_decals)
                summary_lines.append(
                    "  Decal teardown: {} batch decal(s) removed".format(n_removed)
                )

        if body_appearance_snap and texture_pipeline.RESTORE_BODY_APPEARANCES_ON_FINISH:
            n_restored = texture_pipeline.restore_body_appearances(body_appearance_snap)
            summary_lines.append(
                "  Carrier teardown: original appearance restored on {} body/bodies".format(
                    n_restored
                )
            )
        elif body_appearance_snap:
            summary_lines.append(
                "  Carrier teardown: skipped (RESTORE_BODY_APPEARANCES_ON_FINISH=False)"
            )

        if zero_texture_color_sets:
            summary_lines.append(
                "Textures 0 for {} color set(s): {}".format(
                    len(zero_texture_color_sets),
                    ", ".join(zero_texture_color_sets),
                )
            )
            summary_lines.append(
                "  Hint: rename appearances/decals to match SLOT*_NAMES in texture_pipeline.py,"
            )
            summary_lines.append(
                "  or rely on the positional fallback (decal mode only)."
            )

    summary_lines.append("")
    _post_purge: List[str] = []
    for cs in color_sets:
        _post_purge.extend(support_paths.purge_ignored_texture_sidecars(cs.folder))
    if _post_purge:
        summary_lines.append("Removed *flipped* / mirror sidecar rasters after batch:")
        summary_lines.extend(["  {}".format(x) for x in _post_purge])

    if "APS" in (render_backend or "").upper() or aps_render_ok or aps_fallback_count:
        summary_lines.append("")
        summary_lines.append(
            "APS renders OK: {}  APS fallbacks: {}".format(
                aps_render_ok, aps_fallback_count
            )
        )
        if aps_fallback_count and not aps_render_ok:
            summary_lines.append(
                "  Hint: check APS fallback lines above for OSS/upload/render errors."
            )

    summary_lines.append("")
    summary_lines.append("Renders OK: {}  Failed: {}".format(renders_ok, renders_fail))
    summary_lines.append("CSV log: {}".format(csv_log))
    summary_lines.append("Plugin log: {}".format(log_path))

    summary_path = texture_root / "_LifeproofBatchRender_summary.txt"
    full_text = "\n".join(summary_lines)
    try:
        summary_path.write_text(full_text, encoding="utf-8")
    except Exception as ex:
        logutil.append_log(log_path, "summary write failed: {}".format(ex))

    short_lines: List[str] = [
        "Texture root: {}".format(texture_root),
        "Color sets: {} | Models: {}".format(len(color_sets), len(models)),
        "Output: {} x {} {}".format(render_width, render_height, output_ext.upper()),
        "Pipeline: {} | Backend: {}".format(pipeline_selection, render_backend),
        "",
        "Renders OK: {}  Failed: {}".format(renders_ok, renders_fail),
        "",
        "Full summary: {}".format(summary_path),
        "CSV log:      {}".format(csv_log),
        "Plugin log:   {}".format(log_path),
    ]
    short_text = "\n".join(short_lines)

    if aps_executor is not None:
        aps_executor.shutdown(wait=True)

    try:
        uip.set_progress(ins, 100, 100)
    except Exception:
        pass
    fui.messageBox(short_text, "Lifeproof Batch Render")
    logutil.append_log(log_path, "execute_batch done ok={} fail={}".format(renders_ok, renders_fail))
    texture_pipeline.clear_decal_shift_temp_files()
