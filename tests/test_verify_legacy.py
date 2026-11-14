"""verify: enclosed-support severity depends on whether the build was legacy.

No Bambu binary needed: the slicer and the G-code analysis are stubbed.
Run: PYTHONPATH=skills/brickprint <blender python> -m unittest discover -s skills/brickprint/tests
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brickprint import verify

ENCLOSED_SUPPORT = {
    "paths": 5, "outside_enforcers": 0, "auto_object_points": 0, "inside_bodies": 12,
    "enforcers": 0, "per_object_supports": 1,
    "enclosed": [{"n": 12, "z": 1.0, "part": "3001.dat", "sig": "3001", "bbox": [0, 0, 1, 1]}],
}


def _fake_project(path):
    return {"plates": [{"id": 1, "name": "Red 1 of 1", "objects": []}], "settings": {},
            "thumbnails": True, "models_sha": "x"}


class VerifyLegacySeverity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.project = self.d / "set_hoth.3mf"
        self.project.write_bytes(b"not read: read_project is stubbed")
        self.gcode = self.d / "plate_1.gcode"
        self.gcode.write_text("; stub")

    def tearDown(self):
        self.tmp.cleanup()

    def _report(self, data):
        (self.d / "set_hoth_report.json").write_text(json.dumps(data))

    def _run(self):
        an = {"parts": [], "support": json.loads(json.dumps(ENCLOSED_SUPPORT)), "bridges": {}}
        with mock.patch.object(verify, "read_project", _fake_project), \
             mock.patch.object(verify, "slice_plates", lambda *a, **k: ({1: self.gcode}, "", {})), \
             mock.patch.object(verify.gcode, "config_block", lambda g: {}), \
             mock.patch.object(verify, "analyse_plate", lambda proj, plate, g: an):
            return verify.run(self.project, "/no/bambu", workdir=self.d / "work",
                              float_check=False, log=lambda *a: None)

    def test_legacy_report_downgrades_enclosure_to_warning(self):
        self._report({"plates": 1, "support_plates": 1})   # legacy: no "settings" key
        res = self._run()
        self.assertTrue(res["ok"], res["errors"])
        enc = [w for w in res["warnings"] if "enclosed by a part's walls" in w]
        self.assertEqual(len(enc), 1, res["warnings"])
        self.assertIn("plate 1: 12 support points", enc[0])
        self.assertIn("default-profile build", enc[0])
        self.assertIn("blockers", enc[0])
        # enclosure data in the report is unchanged
        sup = res["plates"][1]["support"]
        self.assertEqual(sup["inside_bodies"], 12)
        self.assertEqual(sup["enclosed"], ENCLOSED_SUPPORT["enclosed"])

    def test_profile_report_keeps_error(self):
        self._report({"plates": 1, "settings": {"profile": "x", "legacy_default": False}})
        res = self._run()
        self.assertFalse(res["ok"])
        self.assertTrue(any("enclosed by a part's walls" in e for e in res["errors"]))

    def test_no_report_stays_strict(self):
        res = self._run()
        self.assertFalse(res["ok"])
        self.assertTrue(any("enclosed by a part's walls" in e for e in res["errors"]))

    def test_unreadable_report_stays_strict(self):
        (self.d / "set_hoth_report.json").write_text("{not json")
        res = self._run()
        self.assertFalse(res["ok"])


class BuildIsLegacy(unittest.TestCase):
    def test_detection(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "a.3mf"
            rep = Path(t) / "a_report.json"
            self.assertIsNone(verify.build_is_legacy(p))
            rep.write_text(json.dumps({"plates": 3}))
            self.assertTrue(verify.build_is_legacy(p))
            rep.write_text(json.dumps({"settings": {"legacy_default": False}}))
            self.assertFalse(verify.build_is_legacy(p))
            rep.write_text(json.dumps({"settings": {"legacy_default": True}}))
            self.assertTrue(verify.build_is_legacy(p))
            rep.write_text("[]")
            self.assertIsNone(verify.build_is_legacy(p))

    def test_enclosure_issue(self):
        sup = json.loads(json.dumps(ENCLOSED_SUPPORT))
        self.assertIsNone(verify.enclosure_issue(1, {"inside_bodies": 0}, legacy=True))
        sev, msg = verify.enclosure_issue(3, sup, legacy=None)
        self.assertEqual(sev, "error")
        self.assertIn("3001.dat 12 pts z 1.00-1.00", msg)
        self.assertEqual(verify.enclosure_issue(3, sup, legacy=False)[0], "error")
        self.assertEqual(verify.enclosure_issue(3, sup, legacy=True)[0], "warning")


if __name__ == "__main__":
    unittest.main()
