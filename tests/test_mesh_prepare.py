"""MeshCache.prepare(): the process-pool path matches the serial loop.

Synthetic box meshes stand in for LDView (FakeCache._export), so no LDView
or Bambu Studio is needed.  FakeCache lives at module level so spawned pool
workers can unpickle it."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from brickprint import ldraw

BOX_T = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])


def box(dx, dy, dz):
    V = np.array([[0, 0, 0], [dx, 0, 0], [dx, dy, 0], [0, dy, 0],
                  [0, 0, dz], [dx, 0, dz], [dx, dy, dz], [0, dy, dz]], dtype=np.float64)
    return V, BOX_T.copy()


class FakeCache(ldraw.MeshCache):
    """Part "b<x>_<y>_<z>.dat" is an x*y*z mm box; "fail_*" raise like
    _ldview does, "exit_*" like a missing LDView."""

    def _export(self, part_file):
        name = part_file.removesuffix(".dat")
        if name.startswith("fail"):
            raise RuntimeError(f"LDView failed for {part_file}: synthetic")
        if name.startswith("exit"):
            raise SystemExit("LDView not found. synthetic")
        dx, dy, dz = (float(v) for v in name[1:].split("_"))
        self._last_export = {"variant": "synthetic box"}
        if "_7" in name:          # an L shape: two boxes, so poses differ
            V2, T2 = box(dx / 2, dy, dz * 2)
            V, T = box(dx, dy, dz)
            return np.vstack([V, V2 + [0, 0, 0]]), np.vstack([T, T2 + 8])
        return box(dx, dy, dz)


PARTS = [f"b{x}_{y}_{z}.dat" for x, y, z in
         [(4, 4, 2), (8, 2, 3), (2, 2, 9), (6, 3, 7), (3, 5, 1), (5, 5, 5), (9, 2, 2),
          (2, 8, 7)]]


def items(parts):
    return [(p, "Rx" if i % 3 == 1 else "id") for i, p in enumerate(parts)]


def cache_files(root: Path) -> dict:
    out = {}
    for p in sorted(root.iterdir()):
        if p.suffix == ".npz":
            d = np.load(p)
            out[p.name] = {k: d[k] for k in d.files}
        else:
            out[p.name] = json.loads(p.read_text())
    return out


class PrepareParallel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cache(self, name):
        return FakeCache({"cache_dir": str(self.base / name), "ldraw_dir": "/nonexistent",
                          "ldview": ""})

    def run_prepare(self, cache, parts, workers):
        lines = []
        out = cache.prepare(items(parts), workers, log=lines.append)
        return out, lines

    def assert_same(self, a, b):
        self.assertEqual(list(a), list(b))
        for k in a:
            Va, Ta, ca = a[k]
            Vb, Tb, cb = b[k]
            self.assertEqual(Va.dtype, Vb.dtype)
            self.assertEqual(Ta.dtype, Tb.dtype)
            np.testing.assert_array_equal(Va, Vb)
            np.testing.assert_array_equal(Ta, Tb)
            self.assertEqual(list(ca), list(cb))
            for up in ca:
                np.testing.assert_array_equal(ca[up][0], cb[up][0])
                self.assertEqual(ca[up][1], cb[up][1])

    def test_parallel_matches_serial(self):
        parts = PARTS[::-1] + PARTS[:0]     # not sorted: order must be input order
        cs, cp = self.cache("serial"), self.cache("parallel")
        serial, slog = self.run_prepare(cs, parts, 1)
        par, plog = self.run_prepare(cp, parts, 3)
        self.assertEqual(list(par), parts)
        self.assert_same(serial, par)
        self.assertEqual(slog, plog)
        fs, fp = cache_files(cs.root), cache_files(cp.root)
        self.assertEqual(sorted(fs), sorted(fp))
        self.assertEqual(len(fs), 4 * len(parts))       # raw, q.json, repaired, cands
        for name in fs:
            if isinstance(fs[name], dict) and name.endswith(".npz"):
                for k in fs[name]:
                    np.testing.assert_array_equal(fs[name][k], fp[name][k])
            else:
                self.assertEqual(fs[name], fp[name], name)
        self.assertFalse([p for p in cp.root.iterdir() if p.name.endswith(".tmp")])
        # warm: everything from the cache, same result, no pool
        with mock.patch.object(ldraw, "_run_pool", side_effect=AssertionError("pool")):
            warm, _ = self.run_prepare(cp, parts, 3)
        self.assert_same(serial, warm)

    def test_partial_cache_and_progress_lines(self):
        parts = [f"b{2 + i % 7}_{3 + i // 7}_1.dat" for i in range(55)]
        cs, cp = self.cache("serial"), self.cache("parallel")
        cp.prepare(items(parts[5:20]), 1, log=lambda s: None)     # some cached already
        serial, slog = self.run_prepare(cs, parts, 1)
        par, plog = self.run_prepare(cp, parts, 4)
        self.assert_same(serial, par)
        self.assertEqual(slog, plog)
        self.assertEqual(len(plog), 2)                    # [50/n] and [n/n]
        self.assertIn(f"[  50/{len(parts)}]", plog[0])

    def test_worker_error_is_the_original_exception(self):
        parts = PARTS[:3] + ["fail_a.dat"] + PARTS[3:5] + ["fail_b.dat"]
        with self.assertRaises(RuntimeError) as cm:
            self.run_prepare(self.cache("p"), parts, 3)
        self.assertEqual(str(cm.exception), "LDView failed for fail_a.dat: synthetic")
        self.assertIs(type(cm.exception), RuntimeError)
        self.assertIsNone(cm.exception.__cause__)
        with self.assertRaises(RuntimeError) as cs:
            self.run_prepare(self.cache("s"), parts, 1)
        self.assertEqual(str(cs.exception), str(cm.exception))

    def test_worker_systemexit_is_reraised(self):
        parts = PARTS[:2] + ["exit_x.dat"]
        with self.assertRaises(SystemExit) as cm:
            self.run_prepare(self.cache("p"), parts, 2)
        self.assertEqual(str(cm.exception.code), "LDView not found. synthetic")

    def test_serial_path_without_pool(self):
        c = self.cache("c")
        with mock.patch.object(ldraw, "_run_pool", side_effect=AssertionError("pool")):
            self.run_prepare(c, PARTS, 1)                       # workers = 1
            self.run_prepare(c, PARTS, 8)                       # all cached
            self.run_prepare(c, PARTS + ["b1_2_3.dat"], 8)      # one miss
        with mock.patch.object(ldraw, "_run_pool", wraps=ldraw._run_pool) as pool:
            self.run_prepare(c, PARTS + ["b1_2_4.dat", "b1_2_5.dat"], 8)
        self.assertEqual(pool.call_count, 1)
        self.assertEqual(pool.call_args.args[2], 2)             # min(workers, misses)

    def test_default_workers(self):
        with mock.patch("os.cpu_count", return_value=10):
            self.assertEqual(ldraw.default_mesh_workers({}), 8)
            self.assertEqual(ldraw.default_mesh_workers({"mesh_workers": 3}), 3)
            self.assertEqual(ldraw.default_mesh_workers({"mesh_workers": 0}), 1)
        with mock.patch("os.cpu_count", return_value=2):
            self.assertEqual(ldraw.default_mesh_workers({}), 1)

    def test_atomic_write_leaves_no_partial_file(self):
        p = self.base / "x.json"
        p.write_text("old")

        def boom(f):
            f.write(b"half")
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            ldraw._atomic_write(p, boom)
        self.assertEqual(p.read_text(), "old")
        self.assertEqual(sorted(x.name for x in self.base.iterdir()), ["x.json"])

    def test_pickled_cache_drops_unpicklable_cfg(self):
        import pickle
        c = self.cache("c")
        c.cfg["_composer"] = io.StringIO()        # not picklable
        c2 = pickle.loads(pickle.dumps(c))
        self.assertEqual(c2.root, c.root)
        self.assertNotIn("_composer", c2.cfg)


if __name__ == "__main__":
    unittest.main()
