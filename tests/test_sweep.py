"""Work-dir sweep policy on a fake tree with controlled mtimes."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brickprint import sweep

DAY = 86400.0
MB = 1024 ** 2
NOW = 2_000_000_000.0


def make(root: Path, name: str, age_s: float, size: int = 1000) -> Path:
    d = root / name
    (d / "sub").mkdir(parents=True)
    f = d / "sub" / "plate_1.gcode"
    f.write_bytes(b"\0" * size)
    t = NOW - age_s
    for p in (f, d / "sub", d):
        os.utime(p, (t, t))
    return d


class SweepPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.temp = base / "T"
        self.cache = base / "cache"
        self.temp.mkdir()
        self.cache.mkdir()
        env = {k: v for k, v in os.environ.items() if k != sweep.NO_SWEEP_ENV}
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.logs = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_sweep(self, cfg=None, **kw):
        cfg = {"cache_dir": str(self.cache), **(cfg or {})}
        return sweep.sweep_workdirs(cfg, temp_root=self.temp, cache_root=self.cache, now=NOW,
                                    log=self.logs.append, **kw)

    def names(self, root):
        return sorted(p.name for p in root.iterdir())

    def test_age_cutoff(self):
        make(self.temp, "brickprint_verify_old", 4 * DAY)
        make(self.temp, "brickprint_verify_new", 2 * DAY)
        make(self.cache, "platecheck_old", 5 * DAY)
        make(self.cache, "probe_old", 3.5 * DAY)
        make(self.cache, "final_old", 10 * DAY)
        make(self.cache, "final_new", 1 * DAY)
        res = self.run_sweep()
        self.assertEqual(self.names(self.temp), ["brickprint_verify_new"])
        self.assertEqual(self.names(self.cache), ["final_new"])
        self.assertEqual(res["removed"], 4)
        self.assertEqual(res["freed"], 4000)
        self.assertEqual(len(self.logs), 1)
        self.assertIn("removed 4", self.logs[0])

    def test_sweep_days_config(self):
        make(self.cache, "probe_a", 2 * DAY)
        self.run_sweep({"sweep_days": 1})
        self.assertEqual(self.names(self.cache), [])

    def test_size_cap_oldest_first(self):
        for i, age_h in enumerate((30, 10, 50, 20)):   # all younger than 3 days
            make(self.temp, f"brickprint_verify_{i}", age_h * 3600, size=1 * MB)
        # cap 2.5 MB: the 4 MB total must lose the two oldest (50 h, 30 h)
        res = self.run_sweep({"verify_keep_gb": 2.5 * MB / 1024 ** 3})
        self.assertEqual(self.names(self.temp), ["brickprint_verify_1", "brickprint_verify_3"])
        self.assertEqual(res["removed"], 2)

    def test_cap_keeps_newest_even_when_over(self):
        make(self.temp, "brickprint_verify_old", 30 * 3600, size=3 * MB)
        make(self.temp, "brickprint_verify_new", 2 * 3600, size=3 * MB)
        # each dir alone is over the 1 MB cap: the older goes, the newest stays
        self.run_sweep({"verify_keep_gb": 1 * MB / 1024 ** 3})
        self.assertEqual(self.names(self.temp), ["brickprint_verify_new"])

    def test_cap_ignores_cache_dirs(self):
        make(self.cache, "platecheck_big", 2 * DAY, size=5 * MB)
        make(self.temp, "brickprint_verify_a", 2 * DAY, size=1 * MB)
        self.run_sweep({"verify_keep_gb": 2 * MB / 1024 ** 3})
        self.assertEqual(self.names(self.cache), ["platecheck_big"])
        self.assertEqual(self.names(self.temp), ["brickprint_verify_a"])

    def test_one_hour_guard(self):
        # old dir, but a file deep inside was written 10 minutes ago
        d = make(self.temp, "brickprint_verify_busy", 10 * DAY, size=3 * MB)
        f = d / "sub" / "plate_1.gcode"
        os.utime(f, (NOW - 600, NOW - 600))
        make(self.cache, "probe_running", 30 * 60)
        res = self.run_sweep({"verify_keep_gb": 1 * MB / 1024 ** 3})
        self.assertEqual(self.names(self.temp), ["brickprint_verify_busy"])
        self.assertEqual(self.names(self.cache), ["probe_running"])
        self.assertEqual(res["removed"], 0)
        self.assertEqual(self.logs, [])

    def test_symlinks_files_and_other_names_left_alone(self):
        target = make(Path(self.tmp.name), "precious", 10 * DAY)
        os.symlink(target, self.temp / "brickprint_verify_link")
        os.symlink(target, self.cache / "probe_link")
        (self.temp / "brickprint_verify_file").write_text("x")
        os.utime(self.temp / "brickprint_verify_file", (NOW - 10 * DAY,) * 2)
        make(self.temp, "platecheck_in_temp", 10 * DAY)      # wrong root for this prefix
        make(self.cache, "brickprint_verify_in_cache", 10 * DAY)
        make(self.cache, "meshes", 10 * DAY)
        make(self.temp, "other_tool_tmp", 10 * DAY)
        make(self.temp / "nested", "brickprint_verify_deep", 10 * DAY)   # not directly under
        res = self.run_sweep()
        self.assertEqual(res["removed"], 0)
        self.assertTrue((target / "sub" / "plate_1.gcode").is_file())
        self.assertEqual(self.names(self.temp), ["brickprint_verify_file", "brickprint_verify_link",
                                                 "nested", "other_tool_tmp", "platecheck_in_temp"])
        self.assertEqual(self.names(self.cache), ["brickprint_verify_in_cache", "meshes", "probe_link"])

    def test_exclude_user_workdir(self):
        wd = make(self.temp, "brickprint_verify_mine", 10 * DAY)
        holder = make(self.temp, "brickprint_verify_holder", 10 * DAY)
        self.run_sweep(exclude=[str(wd), str(holder / "sub" / "x.3mf")])
        self.assertEqual(self.names(self.temp), ["brickprint_verify_holder", "brickprint_verify_mine"])

    def test_opt_out(self):
        make(self.temp, "brickprint_verify_old", 10 * DAY)
        os.environ[sweep.NO_SWEEP_ENV] = "1"
        res = self.run_sweep()
        self.assertEqual(res["removed"], 0)
        self.assertEqual(self.names(self.temp), ["brickprint_verify_old"])
        self.assertIsNone(sweep.maybe_sweep({"cache_dir": str(self.cache)}))

    def test_nothing_freed_logs_nothing(self):
        make(self.temp, "brickprint_verify_new", 1 * DAY)
        self.run_sweep()
        self.assertEqual(self.logs, [])

    def test_delete_failure_is_not_fatal(self):
        make(self.temp, "brickprint_verify_old", 10 * DAY)
        with mock.patch.object(sweep.shutil, "rmtree", lambda *a, **k: None):
            res = self.run_sweep()
        self.assertEqual(res, {"removed": 0, "freed": 0, "failed": 1})

    def test_maybe_sweep_swallows_errors(self):
        with mock.patch.object(sweep, "sweep_workdirs", side_effect=OSError("boom")):
            self.assertIsNone(sweep.maybe_sweep({"cache_dir": str(self.cache)}, log=self.logs.append))
        self.assertIn("skipped", self.logs[0])


if __name__ == "__main__":
    unittest.main()
