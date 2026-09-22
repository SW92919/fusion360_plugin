# APS cloud-render spike.
#
# GOAL: answer, empirically and on the client's real account, the one question
# that decides whether "Path A" (cloud rendering via the Fusion API) is even
# possible:
#
#   Does Rendering.startLocalRender("") — i.e. an EMPTY filename — offload the
#   render to Autodesk's cloud (consume a Flex token, run remotely), or does it
#   just compute locally and save the PNG to the cloud gallery?
#
# And, if it does produce a cloud image: where does it land (RenderFuture.filename)
# and is that location reachable so we could download it?
#
# HOW TO RUN
#   1. Sign in to Fusion with the account that has Flex tokens.
#   2. Open a design that is SAVED IN FUSION TEAM (cloud), not a local .f3d
#      import — cloud rendering needs the model in the cloud.
#   3. Note your Flex/cloud-credit balance BEFORE running (Autodesk Account →
#      Cloud credits). The API can't read the balance; you check it manually.
#   4. Utilities → ADD-INS → Scripts and Add-Ins → green + → pick this folder →
#      Run. Let it finish, then re-check your balance to see if a token was used.
#
# It writes everything to cloud_render_spike_log.txt next to this file and shows
# a summary dialog. Nothing here touches the batch add-in.

import time
import traceback
from pathlib import Path

import adsk.core
import adsk.fusion


def _write_log(lines):
    text = "\n".join(str(x) for x in lines)
    try:
        out = Path(__file__).resolve().parent / "cloud_render_spike_log.txt"
        out.write_text(text, encoding="utf-8")
    except Exception:
        pass
    return text


def _try(fn, default="<error>"):
    try:
        return fn()
    except Exception as ex:
        return "{} ({})".format(default, ex)


def run(context):
    _ = context
    app = adsk.core.Application.get()
    ui = app.userInterface
    L = []

    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            ui.messageBox("Open a Fusion DESIGN first (ideally a Fusion Team / cloud-saved doc).")
            return

        doc = app.activeDocument
        L.append("=== APS Cloud Render Spike ===")
        L.append("Document: {}".format(_try(lambda: doc.name)))

        # Is the doc a cloud (Fusion Team) document? Cloud render needs this.
        is_cloud = False
        try:
            df = doc.dataFile  # raises / None for unsaved or purely-local docs
            if df is not None:
                is_cloud = True
                L.append("Cloud doc: YES  (dataFile='{}', id='{}')".format(
                    _try(lambda: df.name), _try(lambda: df.id)))
        except Exception as ex:
            L.append("Cloud doc: NO / unknown ({}) — startLocalRender('') may fail "
                     "or fall back to local-only if the model isn't in Fusion Team.".format(ex))
        if not is_cloud:
            L.append("WARNING: run this on a model saved to Fusion Team for a valid cloud test.")

        rendering = None
        try:
            rendering = design.renderManager.rendering
        except Exception as ex:
            L.append("renderManager.rendering unavailable: {}".format(ex))
            ui.messageBox(_write_log(L)); return

        # Modest settings — keep the token cost / time small for the test.
        try:
            ar = adsk.fusion.RenderAspectRatios
            rendering.aspectRatio = ar.CustomRenderAspectRatio
            rendering.resolutionWidth = 1280
            rendering.resolutionHeight = 720
            rendering.renderQuality = 50
            L.append("Settings: 1280x720 q50")
        except Exception as ex:
            L.append("Settings warn: {}".format(ex))

        # --- Trigger the CLOUD-save render: empty filename ---
        L.append("")
        L.append("Calling startLocalRender('') — empty filename = 'save to cloud' per docs ...")
        t0 = time.monotonic()
        fut = None
        for label, call in (
            ("startLocalRender('')", lambda: rendering.startLocalRender("")),
            ("startLocalRender()",   lambda: rendering.startLocalRender()),
        ):
            try:
                fut = call()
                L.append("  {} -> RenderFuture={}".format(label, "OK" if fut else "None"))
                if fut:
                    break
            except Exception as ex:
                L.append("  {} FAILED: {}".format(label, ex))
        if fut is None:
            L.append("No RenderFuture obtained — the API would not start this render.")
            ui.messageBox(_write_log(L)); return

        # Immediately report where it says the output will go — the key data point.
        L.append("")
        L.append("IMMEDIATE RenderFuture:")
        for attr in ("filename", "imageWidth", "imageHeight", "renderState", "progress"):
            L.append("  future.{} = {}".format(attr, _try(lambda a=attr: getattr(fut, a))))

        # --- Poll (bounded) and log every state/progress change ---
        L.append("")
        L.append("Polling (max 5 min)...")
        deadline = time.monotonic() + 300.0
        last = None
        while time.monotonic() < deadline:
            st = _try(lambda: fut.renderState, default=None)
            pr = _try(lambda: fut.progress, default=None)
            key = (str(st), None if pr is None else round(float(pr), 3) if isinstance(pr, (int, float)) else str(pr))
            if key != last:
                L.append("  t+{:>5.1f}s  state={}  progress={}".format(time.monotonic() - t0, st, pr))
                last = key
            # terminal? progress complete, or state name looks final.
            done = False
            try:
                if isinstance(pr, (int, float)) and pr >= 1.0:
                    done = True
            except Exception:
                pass
            sname = str(st).lower()
            if any(w in sname for w in ("finish", "complete", "fail", "cancel")):
                done = True
            if done:
                L.append("  -> terminal state reached")
                break
            adsk.doEvents()
            time.sleep(1.0)
        else:
            L.append("  -> timed out after 5 min (cloud jobs can take longer; check the Render Gallery)")

        elapsed = time.monotonic() - t0
        L.append("")
        L.append("FINAL (after {:.1f}s):".format(elapsed))
        for attr in ("filename", "renderState", "progress"):
            L.append("  future.{} = {}".format(attr, _try(lambda a=attr: getattr(fut, a))))

        # If filename points at a real local file, this was a LOCAL-compute render.
        fn = _try(lambda: fut.filename, default="")
        if fn and not str(fn).startswith("<error>"):
            p = Path(str(fn))
            exists = _try(lambda: p.is_file(), default=False)
            L.append("")
            L.append("filename is a LOCAL file that exists on disk? {}".format(exists))
            L.append("  -> path: {}".format(p))
            if exists:
                L.append("  INTERPRETATION: output is a local file => compute was LOCAL; "
                         "no automated cloud download needed, but also no cloud offload.")
            else:
                L.append("  INTERPRETATION: filename is not a readable local file => output likely "
                         "went to the cloud Rendering Gallery; the Fusion API has no documented "
                         "download for it (would need APS Data Management or manual gallery download).")

        L.append("")
        L.append("NOW: re-check your Flex/cloud-credit balance in Autodesk Account.")
        L.append("  - Balance DROPPED  => startLocalRender('') really offloaded to the cloud (Path A alive).")
        L.append("  - Balance UNCHANGED => it computed locally and only saved to the gallery (Path A not real; "
                 "cloud-compute is UI-only → Design Automation is the only programmatic cloud path).")

        ui.messageBox(_write_log(L)[:9000], "APS Cloud Render Spike")

    except Exception:
        try:
            ui.messageBox("Spike failed:\n{}".format(traceback.format_exc()))
        except Exception:
            pass


def stop(context):
    _ = context
