"""probe: concurrent confirm rounds, balanced chunking, dirty-only cache save,
and check_final hygiene (all slicer calls stubbed, no Bambu needed)."""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from brickprint import probe, threemf, verify

V = np.array([[0, 0, 0], [5, 0, 0], [0, 5, 0], [0, 0, 5]], dtype=float)
T = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]])
CLEAN = {"float": False, "cant": False, "empty": False, "sliced": True, "msg": ""}
UNSLICED = {"float": False, "cant": False, "empty": False, "sliced": False, "msg": ""}


class _Comp:
    legacy = True

    def fingerprint(self):
        return ""


def scripted(kind: str, rnd: int, it: int) -> dict:
    fl = lambda r: {**CLEAN, "float": True, "msg": f"floating regions run {r}"}
    if kind == "clean":
        return CLEAN
    if kind == "f1":
        return fl(1) if rnd == 1 else CLEAN
    if kind == "f2":
        return fl(2) if rnd == 2 else CLEAN
    if kind == "f3":
        return fl(3) if rnd == 3 else CLEAN
    if kind == "f2f3":
        if rnd == 2:
            return fl(2)
        if rnd == 3:
            return {**CLEAN, "cant": True, "msg": "floating cantilever run 3"}
        return CLEAN
    if kind == "e1":
        return {**CLEAN, "empty": True, "sliced": False, "msg": "empty layer"} if rnd == 1 else CLEAN
    if kind == "rq":
        return UNSLICED if it == 0 else CLEAN
    if kind == "rqf3":
        if rnd == 3:
            return UNSLICED if it == 0 else fl(3)
        return CLEAN
    if kind == "u1":
        return UNSLICED if rnd == 1 else CLEAN
    if kind == "u2":
        return UNSLICED if rnd == 2 else CLEAN
    if kind == "u2f3":
        if rnd == 2:
            return UNSLICED
        return fl(3) if rnd == 3 else CLEAN
    raise AssertionError(kind)


KINDS = ["clean", "f1", "f2", "f3", "f2f3", "e1", "rq", "rqf3", "u1", "u2", "u2f3"]
_TAG = re.compile(r"r(\d+)_p(\d+)_b\d+$")


class Recorder:
    """Stub for probe._run_batch: scripted verdicts, records concurrency."""

    def __init__(self, delay=0.0, fail_in_run=None, slow_run=None, slow_delay=0.0):
        self.lock = threading.Lock()
        self.active = self.max_active = 0
        self.calls = []
        self.delay = delay
        self.fail_in_run = fail_in_run
        self.slow_run = slow_run
        self.slow_delay = slow_delay
        self.wrote_after_exit = False

    def __call__(self, cfg, bambu, jobs, parts, workdir):
        rnd, it = (int(x) for x in _TAG.search(Path(workdir).name).groups())
        Path(workdir).mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        t0 = time.time()
        try:
            d = self.slow_delay if self.slow_run == rnd else self.delay
            if d:
                time.sleep(d)
            if self.fail_in_run == rnd:
                raise RuntimeError("slicer died")
            # Simulate writing into workdir at the end of the subprocess; if
            # root was deleted prematurely while this worker was still active,
            # this detects the wait=False shutdown race.
            if not Path(workdir).exists():
                self.wrote_after_exit = True
            return {job: dict(scripted(job[0].split("_")[0], rnd, it)) for job in jobs}
        finally:
            with self.lock:
                self.active -= 1
                self.calls.append((rnd, t0, time.time()))


def serial_reference(cfg, parts, runs, workers, root):
    stats = {"bambu_runs": 0, "passes": 0}
    todo = sorted((pf, up) for pf in parts for up in probe.UPS)
    merged, unresolved, queue = {}, [], todo
    for rnd in range(1, runs + 1):
        if not queue:
            break
        got, left = probe._slice_all(cfg, "/no/bambu", queue, parts, set(), root, f"r{rnd}",
                                     workers, lambda *a: None, stats, time.time())
        unresolved += left
        for job, v in got.items():
            merged[job] = probe._merge(merged.get(job), v)
        queue = sorted(j for j in got if not probe.bad(merged[j]))
    return merged, sorted(unresolved)


def make_parts(n_per_kind=1):
    parts = {}
    for kind in KINDS:
        for i in range(n_per_kind):
            parts[f"{kind}_{i}.dat"] = {"V": V, "T": T, "mesh_key": f"k{kind}{i}",
                                        "cands": {up: (np.eye(3), {}) for up in probe.UPS}}
    return parts


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache"
        (self.cache / "meshes").mkdir(parents=True)
        self.cfg = {"cache_dir": str(self.cache), "bed": 180.0, "depth": 180.0, "margin": 6.0}
        env = {k: v for k, v in os.environ.items() if k != "BRICKPRINT_PROBE_KEEP"}
        self.patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(threemf, "composer", lambda cfg: _Comp()),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run_probe(self, parts, rec, workers):
        vc = probe.VerdictCache(self.cache / "meshes")
        with mock.patch.object(probe, "_run_batch", rec):
            verdicts, stats = probe.probe(self.cfg, parts, vc, "/no/bambu", workers=workers,
                                          log=lambda *a: None)
        return vc, verdicts, stats


class ConfirmRunsMatchSerial(Base):
    def check(self, batch, workers, n_per_kind=1):
        parts = make_parts(n_per_kind)
        with mock.patch.object(probe, "BATCH", batch), \
             mock.patch.object(probe, "_run_batch", Recorder()):
            ref_dir = Path(self.tmp.name) / "ref"
            want, want_unresolved = serial_reference(self.cfg, parts, 3, workers, ref_dir)
        with mock.patch.object(probe, "BATCH", batch):
            vc, verdicts, stats = self.run_probe(parts, Recorder(), workers)
        fp = ""
        for pf, p in parts.items():
            for up in probe.UPS:
                got = vc.get(pf, probe.pose_key(p["mesh_key"], up, fp))
                self.assertEqual(got, want.get((pf, up)), (pf, up))
        self.assertEqual(stats["unresolved"], len(want_unresolved))
        k = lambda pf: vc.get(pf, probe.pose_key(parts[pf]["mesh_key"], "+X", fp))
        self.assertEqual(k("clean_0.dat")["runs"], 3)
        self.assertFalse(probe.bad(k("clean_0.dat")))
        self.assertEqual((k("f2_0.dat")["runs"], k("f2_0.dat")["msg"]), (2, "floating regions run 2"))
        self.assertEqual((k("f3_0.dat")["runs"], k("f3_0.dat")["float"]), (3, True))
        f23 = k("f2f3_0.dat")
        self.assertEqual((f23["runs"], f23["cant"], f23["msg"]), (2, False, "floating regions run 2"))
        self.assertEqual(k("f1_0.dat")["runs"], 1)
        self.assertTrue(k("e1_0.dat")["empty"])
        self.assertEqual(k("rq_0.dat")["runs"], 3)
        self.assertTrue(k("rqf3_0.dat")["float"])
        self.assertIsNone(k("u1_0.dat"))
        self.assertEqual(k("u2_0.dat")["runs"], 1)
        self.assertEqual((k("u2f3_0.dat")["runs"], k("u2f3_0.dat")["float"]), (1, False))
        self.assertNotIn("u1_0.dat", verdicts)
        self.assertIn("u2_0.dat", verdicts)

    def test_one_project_per_run(self):
        self.check(batch=36, workers=3)

    def test_many_small_projects(self):
        self.check(batch=4, workers=2, n_per_kind=2)

    def test_single_worker(self):
        self.check(batch=5, workers=1)


class ConcurrencyCapAndShutdown(Base):
    def test_shared_pool_cap_and_overlap(self):
        parts = {pf: p for pf, p in make_parts(1).items() if pf.startswith(("clean", "f2", "f3"))}
        rec = Recorder(delay=0.05)
        with mock.patch.object(probe, "BATCH", 3):
            self.run_probe(parts, rec, workers=3)
        self.assertEqual(rec.max_active, 3)
        r2_end = max(e for r, s, e in rec.calls if r == 2)
        r3_start = min(s for r, s, e in rec.calls if r == 3)
        self.assertLess(r3_start, r2_end, "run 3 should start before run 2 finishes")
        r1_end = max(e for r, s, e in rec.calls if r == 1)
        self.assertGreaterEqual(min(s for r, s, e in rec.calls if r > 1), r1_end - 1e-3)

    def test_exception_waits_for_inflight_workers_before_rmtree(self):
        # Run 2 fails fast while run 3 is still executing; pool.shutdown(wait=True)
        # must wait for run 3 to finish before finally deletes the probe workdir.
        parts = {pf: p for pf, p in make_parts(1).items() if pf.startswith("clean")}
        rec = Recorder(delay=0.01, fail_in_run=2, slow_run=3, slow_delay=0.08)
        with mock.patch.object(probe, "BATCH", 6), self.assertRaises(RuntimeError):
            self.run_probe(parts, rec, workers=2)
        self.assertFalse(rec.wrote_after_exit, "workdir deleted while worker was still active")
        self.assertEqual(rec.active, 0)
        self.assertEqual([p.name for p in self.cache.iterdir() if p.name.startswith("probe_")], [])
        self.assertEqual(list((self.cache / "meshes").iterdir()), [])


class BalancedChunkingAndDirtySave(Base):
    def test_even_chunks_balances_stragglers(self):
        chunks = probe._even_chunks(list(range(40)), 36)
        self.assertEqual([len(c) for c in chunks], [20, 20])
        chunks2 = probe._even_chunks(list(range(73)), 36)
        self.assertEqual([len(c) for c in chunks2], [25, 24, 24])
        self.assertEqual(probe._even_chunks([], 36), [])

    def test_batches_balances_and_places_suspects_last(self):
        jobs = [(f"p{i}.dat", "+Z") for i in range(40)]
        suspects = {jobs[0], jobs[1]}
        bs = probe._batches(jobs, suspects)
        self.assertEqual(len(bs), 2)
        self.assertEqual([len(b) for b in bs], [20, 20])
        self.assertIn(bs[0][-1], suspects)
        self.assertIn(bs[1][-1], suspects)

    def test_dirty_only_verdict_cache_save(self):
        parts = {
            "clean_0.dat": {"V": V, "T": T, "mesh_key": "kc0",
                            "cands": {up: (np.eye(3), {}) for up in probe.UPS}},
            "clean_1.dat": {"V": V, "T": T, "mesh_key": "kc1",
                            "cands": {up: (np.eye(3), {}) for up in probe.UPS}},
        }
        vc = probe.VerdictCache(self.cache / "meshes")
        # Pre-populate clean_0.dat in cache
        for up in probe.UPS:
            vc.put("clean_0.dat", probe.pose_key("kc0", up, ""), {**CLEAN, "runs": 3})
        vc.save("clean_0.dat")
        saved = []
        orig_save = vc.save
        vc.save = lambda pf: (saved.append(pf), orig_save(pf))[1]
        with mock.patch.object(probe, "_run_batch", Recorder()):
            probe.probe(self.cfg, parts, vc, "/no/bambu", workers=2, log=lambda *a: None)
        self.assertEqual(saved, ["clean_1.dat"])

    def test_confirm_order(self):
        q = [("a", up) for up in probe.UPS] + [("b", up) for up in probe.UPS]
        self.assertEqual(probe._confirm_order(q, 2), sorted(q))
        o3 = probe._confirm_order(q, 3)
        self.assertEqual(sorted(o3), sorted(q))
        self.assertNotEqual(o3, sorted(q))
        self.assertEqual(o3, probe._confirm_order(list(reversed(q)), 3))


class CheckFinalHygiene(Base):
    def test_stem_collision_warning_join_and_gcode_cleanup(self):
        # Two parts sharing the same stem ("3001") must get distinct mesh keys,
        # multi-line warnings must be joined with " | ", and gcode deleted inside one().
        parts = {
            "a": {"V": V, "T": T, "file": "dir1/3001.dat"},
            "b": {"V": V, "T": T, "file": "dir2/3001.dat"},
        }
        seen_meshes = []
        gcode_paths = []

        def fake_write(cfg, info, plates, meshes, src, verdict=False):
            seen_meshes.append(set(meshes))
            Path(src).write_bytes(b"zip")

        def fake_slice(bambu, src, outdir, ids, log=None, timeout=None):
            Path(outdir).mkdir(parents=True, exist_ok=True)
            gp = Path(outdir) / "plate_1.gcode"
            gp.write_text("; gcode")
            gcode_paths.append(gp)
            txt = (
                "plate 1: found slicing warnings: It seems object a has floating regions\n"
                "plate 1: found slicing warnings: It seems object a has floating cantilever\n"
            )
            return {1: gp, 2: Path(outdir) / "plate_2.gcode"}, txt, {}

        vc = probe.VerdictCache(self.cache / "meshes")
        with mock.patch.object(threemf, "write_project", fake_write), \
             mock.patch.object(verify, "slice_plates", fake_slice):
            out = probe.check_final(self.cfg, "/no/bambu", parts, vc, runs=1, workers=1,
                                    log=lambda *a: None)
        self.assertEqual(len(seen_meshes[0]), 2, "parts with same stem must not collide")
        self.assertIn(" | ", out["a"]["msg"])
        self.assertTrue(out["a"]["float"] and out["a"]["cant"])
        self.assertFalse(out["b"]["float"])
        self.assertTrue(all(not gp.exists() for gp in gcode_paths))


if __name__ == "__main__":
    unittest.main()
