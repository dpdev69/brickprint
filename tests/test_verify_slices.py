"""verify: the supports-off slice is skipped when the copy is the same slicer
input, and otherwise runs concurrently with the file's own slice.  Bambu and
the G-code analysis are stubbed."""

import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from brickprint import verify

MS_PLAIN = '<?xml version="1.0"?><config><object id="1"><part id="1" subtype="normal_part"/></object></config>'
MS_ENF = ('<?xml version="1.0"?><config><object id="1"><part id="1" subtype="normal_part"/>\n'
          '  <part id="2" subtype="support_enforcer"><metadata key="name" value="e"/></part></object></config>')
MODEL = ('<model><resources><object id="3"><components><component p:path="/3D/Objects/a.model" '
         'objectid="1"/>\n  <component p:path="/3D/Objects/a.model" objectid="2"/></components>'
         '</object></resources></model>')
FLOAT = "plate 1: found slicing warnings: It seems object Red 1 of 1 has floating regions."


def write_3mf(path, settings, ms=MS_PLAIN, indent=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", MODEL)
        z.writestr("Metadata/model_settings.config", ms)
        z.writestr("Metadata/project_settings.config", json.dumps(settings, indent=indent))
        z.writestr("Metadata/plate_1.png", b"png")


def fake_project(path):
    return {"plates": [{"id": 1, "name": "Red 1 of 1", "objects": []}], "settings": {},
            "thumbnails": True, "models_sha": "x"}


class Slicer:
    """slice_plates stub: per-tag log text; records order and overlap."""

    def __init__(self, gcode, texts, barrier=None):
        self.gcode, self.texts, self.barrier = gcode, texts, barrier
        self.calls, self.lock, self.active, self.max_active = [], threading.Lock(), 0, 0

    def __call__(self, bambu, src, outdir, ids, log=print, timeout=3600):
        tag = Path(outdir).name
        with self.lock:
            self.calls.append((tag, Path(src).name))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            Path(outdir).mkdir(parents=True, exist_ok=True)
            return {1: self.gcode}, self.texts.get(tag, ""), {}
        finally:
            with self.lock:
                self.active -= 1


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.gcode = self.d / "plate_1.gcode"
        self.gcode.write_text("; stub")
        self.project = self.d / "set_hoth.3mf"

    def tearDown(self):
        self.tmp.cleanup()

    def run_verify(self, slicer, **kw):
        an = {"parts": [], "support": {"paths": 0, "outside_enforcers": 0, "inside_bodies": 0,
                                       "enforcers": 0, "per_object_supports": 0}, "bridges": {}}
        with mock.patch.object(verify, "read_project", fake_project), \
             mock.patch.object(verify, "slice_plates", slicer), \
             mock.patch.object(verify.gcode, "config_block", lambda g: {}), \
             mock.patch.object(verify, "analyse_plate", lambda proj, plate, g: an):
            return verify.run(self.project, "/no/bambu", workdir=self.d / "work",
                              log=lambda *a: None, **kw)


class SupportsOffAlreadyOff(Base):
    """Default-profile builds: global supports off, no enforcers."""

    def setUp(self):
        super().setUp()
        write_3mf(self.project, {"enable_support": "0", "layer_height": "0.08"})

    def test_one_slice_serves_both_checks(self):
        sl = Slicer(self.gcode, {"new": FLOAT})
        res = self.run_verify(sl)
        self.assertEqual(sl.calls, [("new", "set_hoth.3mf")])
        self.assertEqual(res["float_check"]["slice"], "new")
        self.assertFalse(res["ok"])
        self.assertTrue(any("floating with supports off" in e for e in res["errors"]), res["errors"])
        self.assertFalse((self.d / "work" / "supports_off.3mf").exists())

    def test_clean_slice_passes(self):
        res = self.run_verify(Slicer(self.gcode, {}))
        self.assertTrue(res["ok"], res["errors"])

    def test_no_float_check_no_copy(self):
        sl = Slicer(self.gcode, {"new": FLOAT})
        res = self.run_verify(sl, float_check=False)
        self.assertEqual(len(sl.calls), 1)
        self.assertTrue(res["ok"])
        self.assertNotIn("float_check", res)


class SupportsOn(Base):
    """Profile builds: global supports on + enforcers: the copy differs."""

    def setUp(self):
        super().setUp()
        write_3mf(self.project, {"enable_support": "1"}, ms=MS_ENF)

    def test_copy_sliced_concurrently_and_only_its_warnings_count(self):
        # the file's own slice (supports on) reports nothing about floating;
        # a float in "new" alone must not count (Bambu hides it with supports)
        sl = Slicer(self.gcode, {"new": FLOAT, "supports_off": ""}, barrier=threading.Barrier(2))
        res = self.run_verify(sl)
        self.assertEqual(sorted(c[0] for c in sl.calls), ["new", "supports_off"])
        self.assertEqual(sl.max_active, 2)
        self.assertEqual(res["float_check"]["slice"], "supports_off")
        self.assertTrue(res["ok"], res["errors"])
        self.assertTrue((self.d / "work" / "supports_off.3mf").is_file())

    def test_float_in_copy_is_an_error(self):
        res = self.run_verify(Slicer(self.gcode, {"supports_off": FLOAT}))
        self.assertFalse(res["ok"])

    def test_serial_when_not_concurrent(self):
        sl = Slicer(self.gcode, {"supports_off": FLOAT})
        res = self.run_verify(sl, concurrent=False)
        self.assertEqual([c[0] for c in sl.calls], ["new", "supports_off"])
        self.assertEqual(sl.max_active, 1)
        self.assertFalse(res["ok"])

    def test_copy_slice_error_propagates(self):
        def slicer(bambu, src, outdir, ids, log=print, timeout=3600):
            if Path(outdir).name == "supports_off":
                raise RuntimeError("slicer died")
            return {1: self.gcode}, "", {}
        with self.assertRaises(RuntimeError):
            self.run_verify(slicer)


class SameSliceInput(Base):
    def test_cases(self):
        a, b = self.d / "a.3mf", self.d / "b.3mf"
        write_3mf(a, {"enable_support": "0", "x": ["1", "2"]})
        write_3mf(b, {"x": ["1", "2"], "enable_support": "0"}, indent=1)   # re-serialised
        self.assertTrue(verify.same_slice_input(a, b))
        write_3mf(b, {"enable_support": "1", "x": ["1", "2"]})
        self.assertFalse(verify.same_slice_input(a, b))
        write_3mf(b, {"enable_support": "0", "x": ["1", "2"]}, ms=MS_ENF)
        self.assertFalse(verify.same_slice_input(a, b))
        b.write_bytes(b"not a zip")
        self.assertFalse(verify.same_slice_input(a, b))
        self.assertFalse(verify.same_slice_input(a, self.d / "missing.3mf"))

    def test_supports_off_copy_of_off_file_is_same_input(self):
        a, c = self.d / "a.3mf", self.d / "c.3mf"
        write_3mf(a, {"enable_support": "0"})
        verify.supports_off_copy(a, c)
        self.assertTrue(verify.same_slice_input(a, c))
        write_3mf(a, {"enable_support": "0"}, ms=MS_ENF)   # enforcers are removed
        verify.supports_off_copy(a, c)
        self.assertFalse(verify.same_slice_input(a, c))


if __name__ == "__main__":
    unittest.main()
