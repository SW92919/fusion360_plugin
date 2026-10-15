"""The Batch Render panel: a Fusion palette that stays open while the batch runs.

A command dialog cannot do this: it belongs to the document it was opened in, so
Fusion closes it as soon as the batch opens its first model, and current builds
have no progress-bar input for it. A palette is document-independent and shows an
HTML page, so the settings, Render button, status text and a green progress bar
all live in ``html/batch_render.html``.

Page â†’ Python: ``adsk.fusionSendData(action, json)`` arrives in ``_OnIncoming``.
Python â†’ page: ``palette.sendInfoToHTML(action, json)`` (see ``PanelReporter``).
The batch itself runs from a custom event, after the page's request has returned,
so the page is never left waiting on a long call.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import adsk.core

from plugin import controller
from plugin import panel_settings
from plugin import ui as uip

HTML_FILE = Path(__file__).resolve().parent / "html" / "batch_render.html"
RUN_EVENT_ID = "LifeproofBatchRender_run"
PANEL_TITLE = "Batch Render"
PANEL_WIDTH = 640
PANEL_HEIGHT = 560
PANEL_MIN_WIDTH = 480
PANEL_MIN_HEIGHT = 440

_handlers: List[Any] = []
# The registered CustomEvent itself must stay referenced (Autodesk's sample keeps
# it in a global): once Python drops it, Fusion forgets the registration and
# fireCustomEvent returns False ("Fusion did not accept the request", 2026-10-15).
_events: Dict[str, Any] = {}
# pending: checked request waiting to run; running: from Render until the batch
# ends (the Render button is locked); active: execute_batch is executing.
_state: Dict[str, Any] = {"pending": None, "running": False, "active": False, "values": {}}
_log: Callable[[str], None] = lambda _msg: None


class PanelReporter:
    """Shows the batch's status and progress on the panel's page."""

    def __init__(self, palette: Any) -> None:
        self._palette = palette

    def _send(self, action: str, payload: Dict[str, Any]) -> None:
        try:
            if self._palette is not None and self._palette.isValid:
                self._palette.sendInfoToHTML(action, json.dumps(payload))
        except Exception:
            pass

    def set_status(self, text: str) -> None:
        self._send("status", {"text": str(text)})

    def set_progress(self, value: int, maximum: int = 100) -> None:
        self._send("progress", {"value": max(0, int(value)), "maximum": max(1, int(maximum))})

    def started(self) -> None:
        self._send("started", {})

    def _bring_back(self) -> None:
        """Show the panel again if it was closed while the batch ran."""
        try:
            if self._palette is not None and self._palette.isValid and not self._palette.isVisible:
                self._palette.isVisible = True
        except Exception:
            pass

    def finish(self, text: str) -> None:
        """The batch ran to the end with every render made; ``text`` is the summary."""
        self._bring_back()
        self._send("finished", {"text": str(text), "ok": True})

    def fail(self, text: str) -> None:
        """The batch could not run, stopped, or some renders failed; ``text`` says why."""
        self._bring_back()
        self._send("finished", {"text": str(text), "ok": False})

    def idle(self) -> None:
        self._send("idle", {})


def html_url() -> str:
    """The panel page as a file URL, the way Autodesk's own add-ins pass it.

    A plain Windows path reaches the browser with its backslashes escaped
    ("file:///C:/%5CUsers%5C..." â€” "This site can't be reached", 2026-10-15).
    """
    return "file:///" + str(HTML_FILE).replace("\\", "/").lstrip("/")


def _palette(fui: adsk.core.UserInterface) -> Optional[adsk.core.Palette]:
    try:
        return fui.palettes.itemById(uip.PALETTE_ID)
    except Exception:
        return None


def _dialog_instructions_file(fui: adsk.core.UserInterface) -> Optional[Path]:
    dlg = fui.createFileDialog()
    dlg.title = "Select the Render Instructions file"
    dlg.filter = "Render instructions (*.json;*.txt)|*.json;*.txt|All files (*.*)|*.*"
    dlg.filterIndex = 0
    if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
        return None
    return Path(dlg.filename) if dlg.filename else None


def _dialog_texture_root(fui: adsk.core.UserInterface) -> Optional[Path]:
    fd = fui.createFolderDialog()
    fd.title = "Select texture root (subfolders = color sets)"
    if fd.showDialog() != adsk.core.DialogResults.DialogOK:
        return None
    return Path(fd.folder) if fd.folder else None


def _dialog_model_paths(fui: adsk.core.UserInterface) -> List[Path]:
    dlg = fui.createFileDialog()
    dlg.isMultiSelectEnabled = True
    dlg.title = "Select one or more Fusion designs (.f3d)"
    dlg.filter = "Fusion designs (*.f3d)|*.f3d|All files (*.*)|*.*"
    dlg.filterIndex = 0
    if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
        return []
    paths: List[Path] = []
    try:
        names = dlg.filenames
        # The API returns a plain list[str]; reading it as a collection (.count /
        # .item) failed and silently kept only one of several picked files.
        if hasattr(names, "count") and hasattr(names, "item") and not isinstance(names, list):
            names = [names.item(i) for i in range(names.count)]
        paths = [Path(n) for n in names if n]
    except Exception:
        paths = []
    if not paths and dlg.filename:
        paths.append(Path(dlg.filename))
    return paths


def handle_action(
    app: Any, fui: Any, action: str, data: Dict[str, Any]
) -> Dict[str, Any]:
    """Answer one request from the page. Returns the JSON-able reply."""
    if action == "ready":
        defaults = panel_settings.panel_defaults()
        values = dict(defaults["values"])
        values.update(_state["values"])
        return {"ok": True, "defaults": defaults, "values": values, "running": _state["running"]}

    if action == "browse":
        target = str(data.get("target") or "")
        if _state["running"]:
            return {"ok": False}
        if target == "instructions":
            picked = _dialog_instructions_file(fui)
            value = str(picked) if picked else ""
        elif target == "textureRoot":
            picked = _dialog_texture_root(fui)
            value = str(picked) if picked else ""
        elif target == "models":
            # One line in the panel: "|" keeps the paths apart.
            value = " | ".join(str(p) for p in _dialog_model_paths(fui))
        else:
            return {"ok": False, "error": "unknown browse target {!r}".format(target)}
        return {"ok": bool(value), "value": value}

    if action == "render":
        if _state["running"]:
            return {"ok": False, "error": "A batch is already running."}
        _state["values"] = {k: v for k, v in data.items() if isinstance(v, str)}
        request, problem = panel_settings.validate_settings(data)
        if request is None:
            return {"ok": False, "error": problem}
        _state["pending"] = request
        _state["running"] = True
        # Reply now; the batch starts from the custom event once Fusion is idle.
        # Fired from the main thread, fireCustomEvent returned False on the
        # client's build (2026-10-15); custom events are meant to be fired from
        # a worker thread, so a short-lived one does it.
        _spawn(lambda: _fire_run_event(app))
        return {"ok": True}

    if action == "start_now":
        # The page saw no "started" a few seconds after Render: the event never
        # arrived, so run the batch here. The page ignores this reply (it can
        # come minutes later) and follows the batch through its messages.
        if _state["pending"] is not None and not _state["active"]:
            _log("the queued start did not arrive; running the batch directly")
            run_pending(fui)
        return {"ok": True}

    return {"ok": False, "error": "unknown action {!r}".format(action)}


def _spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="LifeproofBatchRenderStart", daemon=True).start()


def _fire_run_event(app: Any) -> None:
    """Worker thread: ask Fusion to call _OnRun on the main thread."""
    time.sleep(0.05)  # let the page's request finish first
    try:
        if not app.fireCustomEvent(RUN_EVENT_ID, "run"):
            _log("fireCustomEvent returned False; the page will ask to start directly")
    except Exception as ex:
        _log("fireCustomEvent failed: {}".format(ex))


def run_pending(fui: Any) -> None:
    """Run the batch the page asked for (from the custom event, or "start_now")."""
    if _state["active"]:
        return  # a batch is executing; this call arrived through its doEvents
    request = _state["pending"]
    _state["pending"] = None
    reporter = PanelReporter(_palette(fui))
    if request is None:
        _state["running"] = False
        reporter.idle()
        return
    _state["active"] = True
    try:
        reporter.started()
        reporter.set_progress(0, 1)
        reporter.set_status("Startingâ€¦")
        controller.execute_batch(
            reporter,
            request.texture_root,
            request.models,
            request.width,
            request.height,
            request.ext,
            request.instructions_path,
            request.render_quality,
        )
    except Exception:
        tb = traceback.format_exc()
        _log("batch failed:\n" + tb)
        reporter.fail("The batch stopped with an error:\n\n" + tb)
    finally:
        _state["active"] = False
        _state["running"] = False
        reporter.idle()


class _OnIncoming(adsk.core.HTMLEventHandler):
    def __init__(self, app: Any, fui: Any) -> None:
        super().__init__()
        self._app, self._fui = app, fui

    def notify(self, args):
        html_args = adsk.core.HTMLEventArgs.cast(args)
        if html_args.action == "response":
            # The page's answer to a sendInfoToHTML (the Qt browser delivers it as
            # an event); nothing to do, and it is not a request to parse.
            return
        try:
            data = json.loads(html_args.data) if html_args.data else {}
            if not isinstance(data, dict):
                data = {}
            reply = handle_action(self._app, self._fui, html_args.action, data)
        except Exception:
            tb = traceback.format_exc()
            _log("panel request {!r} failed:\n{}".format(getattr(html_args, "action", "?"), tb))
            reply = {"ok": False, "error": "Internal error â€” see the add-in's startup log.\n\n" + tb}
        try:
            html_args.returnData = json.dumps(reply)
        except Exception:
            pass


class _OnRun(adsk.core.CustomEventHandler):
    def __init__(self, fui: Any) -> None:
        super().__init__()
        self._fui = fui

    def notify(self, args):
        run_pending(self._fui)


def start(app: adsk.core.Application, fui: adsk.core.UserInterface, log: Callable[[str], None]) -> None:
    """Register the run event (call once from the add-in's run())."""
    global _log
    _log = log
    _state.update(pending=None, running=False, active=False)
    try:
        app.unregisterCustomEvent(RUN_EVENT_ID)  # left over from a crashed session
    except Exception:
        pass
    event = app.registerCustomEvent(RUN_EVENT_ID)
    if event is None:
        raise RuntimeError("Could not register the custom event {}".format(RUN_EVENT_ID))
    _events[RUN_EVENT_ID] = event
    on_run = _OnRun(fui)
    event.add(on_run)
    _handlers.append(on_run)


def _layout(palette: adsk.core.Palette) -> None:
    """Floating, at its working size â€” in the order Autodesk's own add-ins use.

    Created hidden and shown afterwards, the page sat in a tiny scrolled box in the
    corner of an otherwise empty panel (2026-10-15).
    """
    for step in (
        lambda: palette.setMinimumSize(PANEL_MIN_WIDTH, PANEL_MIN_HEIGHT),
        lambda: setattr(
            palette, "dockingState", adsk.core.PaletteDockingStates.PaletteDockStateFloating
        ),
        lambda: palette.setSize(PANEL_WIDTH, PANEL_HEIGHT),
    ):
        try:
            step()
        except Exception:
            pass


def show(app: adsk.core.Application, fui: adsk.core.UserInterface) -> None:
    """Open the panel (create it the first time)."""
    palette = _palette(fui)
    if palette is None:
        if not HTML_FILE.is_file():
            raise RuntimeError("Panel page missing: {}".format(HTML_FILE))
        palette = fui.palettes.add(
            uip.PALETTE_ID,
            PANEL_TITLE,
            html_url(),
            True,  # visible from the start, like Autodesk's own palettes
            True,  # Fusion's Close button at the bottom (works even if the page fails)
            True,
            PANEL_WIDTH,
            PANEL_HEIGHT,
            True,
        )
        on_incoming = _OnIncoming(app, fui)
        palette.incomingFromHTML.add(on_incoming)
        _handlers.append(on_incoming)
        _layout(palette)
        return
    try:
        if palette.htmlFileURL != html_url():
            palette.htmlFileURL = html_url()  # a panel left from an older build
    except Exception:
        pass
    palette.isVisible = True
    # Reopened: make sure the Render button matches whether a batch is running.
    reporter = PanelReporter(palette)
    if _state["running"]:
        reporter.started()
    else:
        reporter.idle()


def stop(app: adsk.core.Application, fui: adsk.core.UserInterface) -> None:
    """Remove the panel and the run event (call from the add-in's stop())."""
    palette = _palette(fui)
    try:
        if palette is not None:
            palette.deleteMe()
    except Exception:
        pass
    try:
        app.unregisterCustomEvent(RUN_EVENT_ID)
    except Exception:
        pass
    _handlers.clear()
    _events.clear()
    _state.update(pending=None, running=False, active=False)
