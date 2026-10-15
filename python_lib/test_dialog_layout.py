"""Layout of the Batch Render dialog (plugin/ui.py) against a stand-in CommandInputs.

The settings sit in one three-column table (label | input | "..." button) so each
path and its button share a row, the labels stay visible and every row lines up.
Fusion's API cannot run here; these check the inputs the dialog asks Fusion for.
"""

import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

_ADDIN = Path(__file__).resolve().parent.parent / "fusion_addin" / "LifeproofBatchRender"
if str(_ADDIN) not in sys.path:
    sys.path.insert(0, str(_ADDIN))

if "adsk" not in sys.modules:
    _adsk, _core = types.ModuleType("adsk"), types.ModuleType("adsk.core")
    _core.__getattr__ = lambda attr: type(attr, (), {})
    _adsk.core = _core
    sys.modules.update({"adsk": _adsk, "adsk.core": _core})

from plugin import ui  # noqa: E402

_FAKE_ADSK = types.SimpleNamespace(
    core=types.SimpleNamespace(
        TablePresentationStyles=types.SimpleNamespace(transparentBackgroundTablePresentationStyle=2),
        DropDownStyles=types.SimpleNamespace(TextListDropDownStyle=1),
    )
)


class _Input:
    def __init__(self, kind, iid, name, owner, **extra):
        self.kind, self.id, self.name, self._owner = kind, iid, name, owner
        self.table = None
        self.text = None
        self.tooltip = ""
        self.value = ""
        self.listItems = types.SimpleNamespace(add=lambda *a: None)
        self.__dict__.update(extra)


class _Table(_Input):
    def __init__(self, iid, name, owner, ratio):
        super().__init__("table", iid, name, owner, columnRatio=ratio)
        self.cells = {}

    def addCommandInput(self, inp, row, column, row_span=0, column_span=0):
        self.cells[(row, column)] = inp
        inp.table = self
        return True


class _Inputs:
    def __init__(self, tables=True):
        self.items = []
        self.tables = tables

    def _add(self, inp):
        self.items.append(inp)
        return inp

    def addTableCommandInput(self, iid, name, _cols, ratio):
        if not self.tables:
            raise RuntimeError("no tables in this build")
        return self._add(_Table(iid, name, self, ratio))

    def addStringValueInput(self, iid, name, initial=""):
        return self._add(_Input("string", iid, name, self, value=initial))

    def addBoolValueInput(self, iid, name, is_check_box, resource_folder="", initial=False):
        return self._add(_Input("button", iid, name, self, resourceFolder=resource_folder))

    def addDropDownCommandInput(self, iid, name, _style):
        return self._add(_Input("dropdown", iid, name, self))

    def addProgressBarCommandInput(self, iid, name, _fmt):
        return self._add(_Input("progress", iid, name, self))

    def addTextBoxCommandInput(self, iid, name, text, rows, read_only):
        return self._add(_Input("textbox", iid, name, self, shown=text, readOnly=read_only, rows=rows))

    def itemById(self, iid):
        return next((i for i in self.items if i.id == iid), None)

    def rows(self):
        """What the dialog shows, top to bottom: inputs not placed inside a table."""
        return [i for i in self.items if i.table is None]


class DialogLayoutTests(unittest.TestCase):
    def setUp(self):
        self._adsk = ui.adsk
        ui.adsk = _FAKE_ADSK
        self._tmp = tempfile.TemporaryDirectory()
        self._gettempdir = ui.tempfile.gettempdir
        ui.tempfile.gettempdir = lambda: self._tmp.name

    def tearDown(self):
        ui.adsk = self._adsk
        ui.tempfile.gettempdir = self._gettempdir
        self._tmp.cleanup()

    def _table(self):
        ins = _Inputs()
        ui.build_command_inputs(ins)
        return ins, ins.itemById(ui.INPUT_SETTINGS_TABLE)

    def test_each_row_is_label_input_and_for_paths_a_button(self):
        ins, table = self._table()
        self.assertEqual(table.columnRatio, ui.SETTINGS_COLUMN_RATIO)
        expected = [
            ("Render Instructions Location", ui.INPUT_INSTRUCTIONS, ui.INPUT_BROWSE_INSTRUCTIONS),
            ("Texture root folder", ui.INPUT_TEXTURE_ROOT, ui.INPUT_BROWSE_TEXTURE_ROOT),
            ("Model files (empty = all)", ui.INPUT_MODEL_PATHS, ui.INPUT_BROWSE_MODEL_PATHS),
            ("Image width (px)", ui.INPUT_IMG_W, None),
            ("Image height (px)", ui.INPUT_IMG_H, None),
            ("Render Quality", ui.INPUT_QUALITY, None),
            ("Image format", ui.INPUT_FORMAT, None),
            ("Texture pipeline", ui.INPUT_PIPELINE, None),
        ]
        for row, (label, input_id, button_id) in enumerate(expected):
            cell = table.cells[(row, 0)]
            self.assertEqual((cell.kind, cell.shown, cell.readOnly, cell.rows), ("textbox", label, True, 1))
            self.assertEqual(table.cells[(row, 1)].id, input_id)
            if button_id is None:
                self.assertNotIn((row, 2), table.cells)
            else:
                button = table.cells[(row, 2)]
                self.assertEqual(button.id, button_id)
                self.assertTrue(button.tooltip)
            # The rest of the add-in still finds every input by id.
            self.assertIs(ins.itemById(input_id), table.cells[(row, 1)])
        self.assertTrue(table.cells[(2, 0)].tooltip.startswith("Model files (.f3d). Leave EMPTY"))

    def test_only_the_table_progress_and_status_are_rows_of_their_own(self):
        ins, _table = self._table()
        self.assertEqual([r.id for r in ins.rows()], [ui.INPUT_SETTINGS_TABLE, ui.INPUT_PROGRESS, ui.INPUT_STATUS])

    def test_browse_buttons_carry_a_drawn_button_picture(self):
        _ins, table = self._table()
        folder = Path(table.cells[(0, 2)].resourceFolder)
        for size in (16, 32):
            data = (folder / "{0}x{0}.png".format(size)).read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", data[16:24]), (size, size))
            self.assertTrue((folder / "{0}x{0}-dark.png".format(size)).is_file())
        self.assertIsNone(table.cells[(0, 2)].text, "a picture button gets no text")

    def test_build_without_tables_falls_back_to_a_button_row_under_each_field(self):
        ins = _Inputs(tables=False)
        ui.build_command_inputs(ins)
        self.assertEqual(
            [(r.kind, r.id) for r in ins.rows()[:8]],
            [
                ("string", ui.INPUT_INSTRUCTIONS), ("button", ui.INPUT_BROWSE_INSTRUCTIONS),
                ("string", ui.INPUT_TEXTURE_ROOT), ("button", ui.INPUT_BROWSE_TEXTURE_ROOT),
                ("string", ui.INPUT_MODEL_PATHS), ("button", ui.INPUT_BROWSE_MODEL_PATHS),
                ("string", ui.INPUT_IMG_W), ("string", ui.INPUT_IMG_H),
            ],
        )
        button = ins.itemById(ui.INPUT_BROWSE_INSTRUCTIONS)
        self.assertEqual((button.text, button.resourceFolder), ("   ... browse", ""))
        self.assertFalse(any(r.kind == "textbox" and r.id.startswith("lbr_label") for r in ins.rows()))

    def test_picked_model_paths_written_with_bars_parse_back(self):
        import batch_config

        a = r"C:\Models\3-in-1.f3d"
        b = r"C:\Models\Bullnose.f3d"
        self.assertEqual(
            [p.name for p in batch_config.parse_model_paths_blob(" | ".join([a, b]))],
            ["3-in-1.f3d", "Bullnose.f3d"],
        )


if __name__ == "__main__":
    unittest.main()
