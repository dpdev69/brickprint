"""probe / plate_check / check_final remove their work dirs on every exit path,
and cache results only on success.  The slicer is stubbed (no Bambu needed)."""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np

from brickprint import probe, threemf, verify

MODEL_SETTINGS = (b'<?xml version="1.0"?><config>'
                  b'<plate><metadata key="plater_id" value="1"/></plate>'
                  b'<plate><metadata key="plater_id" value="2"/></plate></config>')

V = np.array([[0, 0, 0], [5, 0, 0], [0, 5, 0], [0, 0, 5]], dtype=float)
T = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]])


class _Comp:
    legacy = True

    def fingerprint(self):
        return ""


def _boom(*a, **k):
    raise RuntimeError("slicer died")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache"
        self.cache.mkdir()
        self.cfg = {"cache_dir": str(self.cache), "bed": 180.0, "depth": 180.0, "margin": 6.0}
        env = {k: v for k, v in os.environ.items() if k != "BRICKPRINT_PROBE_KEEP"}
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def leftovers(self, prefix):
        return [p.name for p in self.cache.iterdir() if p.name.startswith(prefix)]


class PlateCheckCleanup(Base):
    def setUp(self):
        super().setUp()
        self.project = Path(self.tmp.name) / "p.3mf"
        with zipfile.ZipFile(self.project, "w") as z:
            z.writestr("Metadata/model_settings.config", MODEL_SETTINGS)

    def test_exception_removes_dir_and_caches_nothing(self):
        with mock.patch.object(verify, "slice_plates", _boom):
            with self.assertRaises(RuntimeError):
                probe.plate_check(self.cfg, "/no/bambu", self.project, runs=2, log=lambda *a: None)
        self.assertEqual(self.leftovers("platecheck_"), [])
        self.assertFalse((self.cache / "plate_checks.json").exists())

    def test_keep_env_keeps_dir_on_exception(self):
        os.environ["BRICKPRINT_PROBE_KEEP"] = "1"
        with mock.patch.object(verify, "slice_plates", _boom):
            with self.assertRaises(RuntimeError):
                probe.plate_check(self.cfg, "/no/bambu", self.project, runs=1, log=lambda *a: None)
        self.assertEqual(len(self.leftovers("platecheck_")), 1)

    def test_success_removes_dir_and_caches(self):
        def ok(bambu, src, outdir, ids, log=None, timeout=None):
            Path(outdir).mkdir(parents=True, exist_ok=True)
            (Path(outdir) / "plate_1.gcode").write_text("x")
            return {}, "plate 2: found slicing warnings: It seems object a has floating regions", {}
        with mock.patch.object(verify, "slice_plates", ok):
            out = probe.plate_check(self.cfg, "/no/bambu", self.project, runs=2, log=lambda *a: None)
        self.assertIn(2, out)
        self.assertEqual(self.leftovers("platecheck_"), [])
        self.assertTrue((self.cache / "plate_checks.json").is_file())


class ProbeCleanup(Base):
    def test_exception_removes_dir(self):
        parts = {"a.dat": {"V": V, "T": T, "mesh_key": "k",
                           "cands": {up: (np.eye(3), {}) for up in probe.UPS}}}
        vc = probe.VerdictCache(self.cache / "meshes")
        with mock.patch.object(threemf, "composer", lambda cfg: _Comp()), \
             mock.patch.object(probe, "_slice_all", _boom):
            with self.assertRaises(RuntimeError):
                probe.probe(self.cfg, parts, vc, "/no/bambu", workers=1, log=lambda *a: None)
        self.assertEqual(self.leftovers("probe_"), [])


class CheckFinalCleanup(Base):
    def test_exception_removes_dir(self):
        parts = {"a": {"V": V, "T": T, "file": "a.dat"}}
        vc = probe.VerdictCache(self.cache / "meshes")
        (self.cache / "meshes").mkdir()
        with mock.patch.object(threemf, "composer", lambda cfg: _Comp()), \
             mock.patch.object(threemf, "write_project",
                               lambda cfg, info, plates, meshes, src, verdict=False: Path(src).write_bytes(b"")), \
             mock.patch.object(verify, "slice_plates", _boom):
            with self.assertRaises(RuntimeError):
                probe.check_final(self.cfg, "/no/bambu", parts, vc, runs=2, workers=1,
                                  log=lambda *a: None)
        self.assertEqual(self.leftovers("final_"), [])
        self.assertEqual(list((self.cache / "meshes").iterdir()), [])


class WorkerDefaults(unittest.TestCase):
    def test_default_workers(self):
        with mock.patch.object(os, "cpu_count", lambda: 10):
            self.assertEqual(probe.default_workers({}), 3)
            self.assertEqual(probe.default_workers({"probe_workers": 6}), 6)
            self.assertEqual(probe.default_workers({"probe_workers": "2"}), 2)
        with mock.patch.object(os, "cpu_count", lambda: 24):
            self.assertEqual(probe.default_workers({}), 4)
        with mock.patch.object(os, "cpu_count", lambda: 2):
            self.assertEqual(probe.default_workers({}), 1)
        with mock.patch.object(os, "cpu_count", lambda: None):
            self.assertEqual(probe.default_workers({}), 1)


if __name__ == "__main__":
    unittest.main()
