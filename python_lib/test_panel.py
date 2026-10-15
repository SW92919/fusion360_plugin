"""The Batch Render panel (plugin/panel.py, panel_settings.py, html/batch_render.html).

Fusion's palette cannot run here; these drive the panel's request handling, the
settings check and the run with small stand-ins, and check that the page and the
add-in agree on the messages they exchange.
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import test_authored_decal_swap

_STAND_IN_ADSK = test_authored_decal_swap.tp  # importing it installs the stand-in adsk package

_ROOT = Path(__file__).resolve().parent.parent
_ADDIN = _ROOT / "fusion_addin" / "LifeproofBatchRender"
if str(_ADDIN) not in sys.path:
    sys.path.insert(0, str(_ADDIN))

from plugin import controller, panel, panel_settings  # noqa: E402
from plugin import ui as uip  # noqa: E402

_CLIENT_JSON = _ROOT / "Resources" / "Models" / "Rendering Instructions.json"


class FakePalette:
    def __init__(self):
        self.isValid = True
        self.isVisible = True
        self.sent = []

    def sendInfoToHTML(self, action, data):
        self.sent.append((action, json.loads(data)))
        return "OK"


class FakeApp:
    def __init__(self, accept=True):
        self.accept = accept
        self.fired = []

    def fireCustomEvent(self, event_id, info=""):
        self.fired.append(event_id)
        return self.accept


class FakeUI:
    def __init__(self, palette=None):
        self.palettes = type("P", (), {"itemById": lambda _s, _id: palette})()


class _TempFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.instructions = t / "Rendering Instructions.json"
        self.instructions.write_text("[]", encoding="utf-8")
        self.textures = t / "Texture"
        self.textures.mkdir()
        self.model_a = t / "Bullnose.f3d"
        self.model_b = t / "3-in-1.f3d"
        self.model_a.write_bytes(b"x")
        self.model_b.write_bytes(b"x")
        panel._state.update(pending=None, running=False, values={})

    def tearDown(self):
        panel._state.update(pending=None, running=False, values={})
        self.tmp.cleanup()

    def settings(self, **over):
        s = {
            "instructions": str(self.instructions),
            "textureRoot": str(self.textures),
            "models": "{} | {}".format(self.model_a, self.model_b),
            "width": "1920",
            "height": "1080",
            "quality": "Draft (25)",
            "format": "JPG",
            "pipeline": "Force Decal",
        }
        s.update(over)
        return s


class SettingsTests(_TempFiles):
    def test_valid_settings_become_a_request(self):
        request, problem = panel_settings.validate_settings(self.settings())
        self.assertEqual(problem, "")
        self.assertEqual(request.models, [self.model_a, self.model_b])
        self.assertEqual((request.width, request.height), (1920, 1080))
        self.assertEqual((request.render_quality, request.ext, request.pipeline), (25, ".jpg", "Force Decal"))

    def test_quoted_paths_and_bad_numbers_fall_back_sensibly(self):
        request, _ = panel_settings.validate_settings(
            self.settings(instructions='"{}"'.format(self.instructions), width="abc", quality="?",
                          format="PNG", pipeline="nonsense")
        )
        self.assertEqual(request.instructions_path, self.instructions)
        self.assertEqual(request.width, int(uip.DEFAULT_IMG_W))
        self.assertEqual((request.render_quality, request.ext), (uip.DEFAULT_RENDER_QUALITY, ".png"))
        self.assertEqual(request.pipeline, uip.PIPELINE_CHOICES[0])

    def test_each_missing_setting_says_what_to_fix(self):
        cases = (
            (dict(instructions=""), "Enter the Render Instructions Location"),
            (dict(instructions="nowhere.json"), "That is a RELATIVE path"),
            (dict(textureRoot=""), "Enter the texture root folder path."),
            (dict(textureRoot=str(self.model_a)), "Texture root is not a valid folder"),
            (dict(models=str(Path(self.tmp.name) / "missing.f3d")), "Model file not found"),
        )
        for over, expected in cases:
            request, problem = panel_settings.validate_settings(self.settings(**over))
            self.assertIsNone(request, over)
            self.assertIn(expected, problem, over)

    @unittest.skipUnless(_CLIENT_JSON.is_file(), "client's Render Instructions not in this checkout")
    def test_empty_model_field_renders_every_model_next_to_the_instructions(self):
        request, problem = panel_settings.validate_settings(
            self.settings(instructions=str(_CLIENT_JSON), models="")
        )
        self.assertEqual(problem, "")
        self.assertEqual(len(request.models), 8)

    def test_defaults_match_the_shared_choices(self):
        d = panel_settings.panel_defaults()
        self.assertEqual(d["values"]["quality"], "Final (75)")
        self.assertEqual((d["values"]["width"], d["values"]["height"]), ("2000", "1130"))
        self.assertEqual(d["pipelines"][0], "Auto (from render instructions)")


class RequestTests(_TempFiles):
    def test_ready_returns_choices_and_remembered_values(self):
        panel._state["values"] = {"textureRoot": r"C:\T"}
        reply = panel.handle_action(FakeApp(), FakeUI(), "ready", {})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["values"]["textureRoot"], r"C:\T")
        self.assertEqual(reply["values"]["width"], "2000")
        self.assertFalse(reply["running"])

    def test_render_with_good_settings_queues_the_run(self):
        reply, _ran = self._act(FakeApp(), "render", self.settings())
        self.assertEqual(reply, {"ok": True})
        self.assertTrue(panel._state["running"])
        self.assertEqual(panel._state["pending"].models, [self.model_a, self.model_b])

    def test_render_with_bad_settings_is_refused_and_nothing_starts(self):
        app = FakeApp()
        reply = panel.handle_action(app, FakeUI(), "render", self.settings(textureRoot=""))
        self.assertFalse(reply["ok"])
        self.assertIn("texture root", reply["error"])
        self.assertEqual(app.fired, [])
        self.assertFalse(panel._state["running"])

    def test_second_render_while_running_is_refused(self):
        panel._state["running"] = True
        reply = panel.handle_action(FakeApp(), FakeUI(), "render", self.settings())
        self.assertEqual(reply, {"ok": False, "error": "A batch is already running."})

    def _act(self, app, action, data=None):
        """handle_action with the start thread run inline and the batch recorded."""
        ran = []
        originals = (controller.execute_batch, panel._spawn, panel.time.sleep)
        controller.execute_batch = lambda reporter, *args: ran.append(args)
        panel._spawn = lambda target: target()
        panel.time.sleep = lambda _s: None
        try:
            reply = panel.handle_action(app, FakeUI(), action, data if data is not None else {})
        finally:
            controller.execute_batch, panel._spawn, panel.time.sleep = originals
        return reply, ran

    def test_render_replies_at_once_and_fires_the_start_event_from_a_thread(self):
        app = FakeApp()
        reply, ran = self._act(app, "render", self.settings())
        self.assertEqual(reply, {"ok": True})
        self.assertEqual(app.fired, [panel.RUN_EVENT_ID])
        self.assertEqual(ran, [], "the batch runs later, from the custom event")
        self.assertTrue(panel._state["running"])

    def test_refused_start_event_is_logged_and_the_batch_waits_for_start_now(self):
        logged = []
        original_log = panel._log
        panel._log = logged.append
        try:
            reply, ran = self._act(FakeApp(accept=False), "render", self.settings())
        finally:
            panel._log = original_log
        self.assertEqual(reply, {"ok": True})
        self.assertEqual(ran, [])
        self.assertTrue(any("returned False" in m for m in logged), logged)
        self.assertIsNotNone(panel._state["pending"])

    def test_start_now_runs_a_batch_whose_event_never_arrived(self):
        # 2026-10-15: fireCustomEvent returned False from the main thread.
        self._act(FakeApp(accept=False), "render", self.settings())
        reply, ran = self._act(FakeApp(), "start_now")
        self.assertEqual(reply, {"ok": True})
        self.assertEqual(len(ran), 1)
        self.assertFalse(panel._state["running"])
        self.assertIsNone(panel._state["pending"])

    def test_start_now_with_nothing_waiting_does_nothing(self):
        reply, ran = self._act(FakeApp(), "start_now")
        self.assertEqual((reply, ran), ({"ok": True}, []))

    def test_late_start_event_during_a_running_batch_changes_nothing(self):
        # The custom event can arrive through the running batch's doEvents.
        panel._state.update(active=True, running=True, pending=None)
        palette = FakePalette()
        panel.run_pending(FakeUI(palette))
        self.assertEqual(palette.sent, [], "no 'idle' that would unlock Render mid-batch")
        self.assertTrue(panel._state["running"])
        panel._state.update(active=False, running=False)

    def test_the_registered_event_object_is_kept_alive(self):
        class FakeEvent:
            def __init__(self):
                self.handlers = []

            def add(self, handler):
                self.handlers.append(handler)

        class EventApp(FakeApp):
            def __init__(self):
                super().__init__()
                self.event = FakeEvent()
                self.unregistered = []

            def registerCustomEvent(self, event_id):
                return self.event

            def unregisterCustomEvent(self, event_id):
                self.unregistered.append(event_id)
                return True

        app = EventApp()
        original_handler = panel._OnRun
        panel._OnRun = lambda fui: ("handler", fui)
        try:
            panel.start(app, FakeUI(), lambda _m: None)
            self.assertIs(panel._events[panel.RUN_EVENT_ID], app.event)
            self.assertEqual(len(app.event.handlers), 1)
            panel.stop(app, FakeUI())
        finally:
            panel._OnRun = original_handler
        self.assertEqual(panel._events, {})
        self.assertEqual(app.unregistered, [panel.RUN_EVENT_ID, panel.RUN_EVENT_ID])

    def test_page_reply_to_a_status_message_is_ignored_quietly(self):
        # Fusion's Qt browser hands the page's answer back as a 'response' event.
        logged = []
        original_log, original_args = panel._log, panel.adsk.core.HTMLEventArgs
        panel._log = logged.append
        panel.adsk.core.HTMLEventArgs = type("A", (), {"cast": staticmethod(lambda a: a)})
        try:
            args = type("Args", (), {"action": "response", "data": "OK", "returnData": ""})()
            panel._OnIncoming(FakeApp(), FakeUI()).notify(args)
        finally:
            panel._log, panel.adsk.core.HTMLEventArgs = original_log, original_args
        self.assertEqual(logged, [])
        self.assertEqual(args.returnData, "")

    def test_browse_for_models_joins_the_picked_files(self):
        original = panel._dialog_model_paths
        panel._dialog_model_paths = lambda _fui: [self.model_a, self.model_b]
        try:
            reply = panel.handle_action(FakeApp(), FakeUI(), "browse", {"target": "models"})
        finally:
            panel._dialog_model_paths = original
        self.assertEqual(reply, {"ok": True, "value": "{} | {}".format(self.model_a, self.model_b)})

    def test_cancelled_browse_changes_nothing(self):
        original = panel._dialog_texture_root
        panel._dialog_texture_root = lambda _fui: None
        try:
            reply = panel.handle_action(FakeApp(), FakeUI(), "browse", {"target": "textureRoot"})
        finally:
            panel._dialog_texture_root = original
        self.assertFalse(reply["ok"])


class RunTests(_TempFiles):
    def _run(self, batch):
        palette = FakePalette()
        panel.handle_action(FakeApp(), FakeUI(palette), "render", self.settings())
        original = controller.execute_batch
        controller.execute_batch = batch
        try:
            panel.run_pending(FakeUI(palette))
        finally:
            controller.execute_batch = original
        return palette.sent

    def test_progress_reaches_the_page_and_the_panel_is_released(self):
        def batch(reporter, *args):
            reporter.set_status("Rendering 1 of 2: Bullnose.png")
            reporter.set_progress(1, 2)
            reporter.finish("Renders OK: 2  Failed: 0")

        sent = self._run(batch)
        actions = [a for a, _ in sent]
        self.assertEqual(actions[0], "started")
        self.assertIn(("progress", {"value": 1, "maximum": 2}), sent)
        self.assertIn(("finished", {"text": "Renders OK: 2  Failed: 0", "ok": True}), sent)
        self.assertEqual(actions[-1], "idle")
        self.assertFalse(panel._state["running"])

    def test_a_crash_is_shown_on_the_panel_not_lost(self):
        def batch(reporter, *args):
            raise RuntimeError("boom")

        sent = self._run(batch)
        failed = [d for a, d in sent if a == "finished"]
        self.assertEqual(len(failed), 1)
        self.assertFalse(failed[0]["ok"])
        self.assertIn("boom", failed[0]["text"])
        self.assertFalse(panel._state["running"])

    def test_bad_texture_folder_is_reported_on_the_panel(self):
        # The real controller, up to its first check (an empty texture root).
        reporter = panel.PanelReporter(FakePalette())
        controller.execute_batch(
            reporter, self.textures, [self.model_a], 1920, 1080, ".png",
            uip.PIPELINE_CHOICES[0], self.instructions, 75,
        )
        sent = reporter._palette.sent
        self.assertEqual(sent[-1][0], "finished")
        self.assertFalse(sent[-1][1]["ok"])
        self.assertIn("No color subfolders found", sent[-1][1]["text"])

    def test_result_brings_a_closed_panel_back(self):
        for end in ("finish", "fail"):
            palette = FakePalette()
            palette.isVisible = False  # closed while the batch ran
            getattr(panel.PanelReporter(palette), end)("Renders OK: 2")
            self.assertTrue(palette.isVisible, end)

    def test_reporter_stays_quiet_once_the_panel_is_gone(self):
        palette = FakePalette()
        palette.isValid = False
        panel.PanelReporter(palette).set_status("x")
        self.assertEqual(palette.sent, [])


class PageContractTests(unittest.TestCase):
    """The page and the add-in must agree on ids and message names."""

    @classmethod
    def setUpClass(cls):
        cls.html = panel.HTML_FILE.read_text(encoding="utf-8")
        cls.py = Path(panel.__file__).read_text(encoding="utf-8")

    def test_every_setting_has_an_input_on_the_page(self):
        names = panel_settings.panel_defaults()["values"].keys()
        for name in names:
            self.assertRegex(self.html, r'id="{}"'.format(name))
        fields = re.search(r"var FIELDS = \[([^\]]*)\]", self.html).group(1)
        self.assertEqual(sorted(re.findall(r'"(\w+)"', fields)), sorted(names))

    def test_requests_from_the_page_are_handled(self):
        sent = set(re.findall(r'send\("(\w+)"', self.html))
        self.assertEqual(sent, {"ready", "browse", "render", "start_now"})
        for action in sent:
            self.assertIn('action == "{}"'.format(action), self.py)

    def test_messages_to_the_page_are_handled(self):
        sent = set(re.findall(r'self\._send\("(\w+)"', self.py))
        self.assertEqual(sent, {"status", "progress", "started", "finished", "idle"})
        for action in sent:
            self.assertIn('action === "{}"'.format(action), self.html)

    def test_page_answers_fusion_with_json(self):
        self.assertIn("return JSON.stringify({ ok: true });", self.html)
        self.assertNotIn('return "OK"', self.html)

    def test_page_is_given_to_fusion_as_a_file_url(self):
        # A plain Windows path arrived as file:///C:/%5CUsers%5C... (ERR_INVALID_URL).
        url = panel.html_url()
        self.assertTrue(url.startswith("file:///"), url)
        self.assertNotIn("\\", url)
        self.assertTrue(url.endswith("/plugin/html/batch_render.html"), url)
        self.assertEqual(url[len("file:///"):].replace("/", "\\").lower(), str(panel.HTML_FILE).lower())

    def test_browse_buttons_name_their_field(self):
        targets = re.findall(r'data-target="(\w+)"', self.html)
        self.assertEqual(targets, ["instructions", "textureRoot", "models"])
        for target in targets:
            self.assertIn('target == "{}"'.format(target), self.py)


if __name__ == "__main__":
    unittest.main()
