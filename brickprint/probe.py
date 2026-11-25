"""Ask Bambu Studio itself whether each part pose floats.

The numpy emulator in orient.py approximates Bambu's floating-region check,
but it disagrees with the real slicer on dozens of parts per set.  This module
uses the Bambu Studio CLI as the oracle (ported from
tools/countach_bambu_probe.py):

* every unique part is posed in each of its 6 up directions (the exact
  rotations orient.candidates() produced), one pose per plate, centred on the
  bed, with the build's own project settings (supports off);
* up to 36 poses go into one multi-plate probe 3MF, sliced headless with
  ``BambuStudio --slice 0 --arrange 0 --orient 0``; several projects run in
  parallel;
* per plate, the log's "plate N: found slicing warnings: ..." lines give the
  verdict: "floating regions" -> float, "floating cantilever" -> cant,
  "empty layer" -> empty.  An empty layer aborts the rest of that project, so
  poses on plates that produced no G-code are re-queued until every pose has a
  verdict.  Poses the emulator predicts to have an empty layer are put last in
  their project so they abort as little as possible;
* Bambu's floating check is NOT deterministic: re-slicing the very same
  probe file flips ~5 % of marginal poses between clean and floating.  So a
  pose only counts as clean when it is clean in CONFIRM_RUNS (3) independent
  runs; clean poses are re-sliced (in different project groupings), and any
  floating / cantilever / empty verdict is final.  Because the rule is AND,
  confirm runs 2..N all start from run 1's clean set together, sharing the
  one pool of Bambu workers; their results are folded in run order, so the
  cached verdicts match the serial run-after-run loop (a pose flagged in
  run 2 may also be sliced in run 3; that slice is discarded).

Verdicts are cached per part next to the mesh cache
(``<cache>/meshes/<lib>/<part>.verdicts.json``), keyed by repaired-mesh hash +
REPAIR_VERSION + ORIENT_VERSION + PROBE_VERSION + pose label, so re-runs are
instant and any mesh or policy change re-probes only what changed.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import orient, repair, slice_cache, threemf

PROBE_VERSION = 2           # bump when the probe project / parsing changes
CONFIRM_RUNS = 3            # a pose counts as clean only if clean in every run
BATCH = 36                  # Bambu's plate grid limit per project
MAX_PASSES = 40
TIMEOUT_S = 900
DEFAULT_BAMBU = "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"
UPS = list(orient.AXIS_NAMES)

_WARN = re.compile(r"plate (\d+): found (?:NON_CRITICAL )?slicing warnings: (.*)")
_GCODE = re.compile(r"plate_(\d+)\.gcode$")


def find_bambu(cfg: dict) -> str:
    """The resolved Bambu Studio executable (config "bambu", else the
    /Applications default), or "" when it is not installed."""
    for cand in (cfg.get("bambu") or "", DEFAULT_BAMBU):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return ""


def default_workers(cfg: dict) -> int:
    """Concurrent Bambu Studio runs: config ``probe_workers``, else
    min(4, cpus // 3) (at least 1).  One multi-plate Bambu slice already uses
    ~6 cores, so more workers would oversubscribe the machine."""
    return max(1, int(cfg.get("probe_workers") or min(4, (os.cpu_count() or 4) // 3)))


def _remove_workdir(root: Path) -> None:
    """Delete a probe / plate-check / final-check work dir unless
    BRICKPRINT_PROBE_KEEP is set; never raises."""
    if not os.environ.get("BRICKPRINT_PROBE_KEEP"):
        shutil.rmtree(root, ignore_errors=True)


def pose_key(mesh_key: str, up: str, fingerprint: str = "") -> str:
    """Verdict cache key.  ``fingerprint`` (settings.Composer.fingerprint())
    covers the verdict-relevant slicer settings (layer height, line widths,
    compensation ...); it is empty for the historic default profile so those
    cached verdicts stay valid."""
    key = (f"{mesh_key}:r{repair.REPAIR_VERSION}:o{orient.ORIENT_VERSION}"
           f":p{PROBE_VERSION}:{up}")
    return key + (f":s{fingerprint}" if fingerprint else "")


def bad(v: dict) -> bool:
    return bool(v.get("float") or v.get("cant") or v.get("empty") or not v.get("sliced", True))


class VerdictCache:
    """Per-part JSON files {pose_key: verdict} beside the mesh cache."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._mem: dict[str, dict] = {}

    def _path(self, part_file: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", part_file)
        return self.root / (safe + ".verdicts.json")

    def load(self, part_file: str) -> dict:
        if part_file not in self._mem:
            data = {}
            p = self._path(part_file)
            if p.is_file():
                try:
                    data = json.loads(p.read_text())
                except ValueError:
                    data = {}
            self._mem[part_file] = data
        return self._mem[part_file]

    def get(self, part_file: str, key: str) -> dict | None:
        return self.load(part_file).get(key)

    def put(self, part_file: str, key: str, verdict: dict) -> None:
        self.load(part_file)[key] = verdict

    def save(self, part_file: str) -> None:
        p = self._path(part_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(self._mem.get(part_file, {}), indent=0, sort_keys=True))
            os.replace(tmp, p)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def _posed(V, T, R) -> np.ndarray:
    W = np.asarray(V, dtype=np.float64) @ np.asarray(R, dtype=float).T
    return (W - W.min(axis=0)).astype(np.float32)


def _write_probe(cfg: dict, jobs: list, parts: dict, path: Path) -> None:
    """One pose per plate, centred, written with the build's own writer and
    project settings so the probe slices exactly what the build will -- except
    that supports are forced off (verdict copy): with enable_support=1 Bambu
    no longer reports floating regions at all (CLI-verified)."""
    meshes, plates = {}, []
    bed, depth, margin = float(cfg["bed"]), float(cfg["depth"]), float(cfg["margin"])
    for i, (part_file, up) in enumerate(jobs, 1):
        W = parts[part_file]["posed"][up]
        key = f"{part_file}@{up}"
        meshes[key] = {"V": W, "T": parts[part_file]["T"]}
        w, d = float(W[:, 0].max()), float(W[:, 1].max())
        plates.append({
            "color_id": "probe", "rgb": "FFFFFF", "color_name": "probe",
            "kind": "", "index": i, "count": len(jobs), "support": "",
            "items": [(key, (bed - w) / 2 - margin, (depth - d) / 2 - margin, w, d)],
        })
    threemf.write_project(cfg, {"set_num": "probe", "name": "brickprint probe"},
                          plates, meshes, path, verdict=True)


def _run_batch(cfg: dict, bambu: str, jobs: list, parts: dict, workdir: Path) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / "probe.3mf"
    _write_probe(cfg, jobs, parts, src)
    cmd = [bambu, "--slice", "0", "--arrange", "0", "--orient", "0", "--debug", "2",
           "--outputdir", str(workdir), str(src)]
    log_path = workdir / "log.txt"
    try:
        with open(log_path, "wb") as lf:
            subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pass
    log = log_path.read_text(errors="replace") if log_path.is_file() else ""
    warns: dict[int, list[str]] = {}
    for m in _WARN.finditer(log):
        warns.setdefault(int(m.group(1)), []).append(m.group(2).strip())
    sliced = {int(m.group(1)) for f in os.listdir(workdir) if (m := _GCODE.search(f))}
    out = {}
    for pi, job in enumerate(jobs, 1):
        t = " | ".join(warns.get(pi, []))
        out[job] = {
            "float": "floating regions" in t,
            "cant": "cantilever" in t,
            "empty": "empty layer" in t,
            "sliced": pi in sliced,
            "msg": t[:200],
        }
    # keep only the log (G-code is large and unused)
    for f in os.listdir(workdir):
        if f.endswith(".gcode"):
            try:
                os.remove(workdir / f)
            except OSError:
                pass
    return out


def _even_chunks(items: list, max_size: int) -> list[list]:
    """Split ``items`` into the minimum number of chunks of size <= ``max_size``
    with balanced sizes (differing by at most 1) to avoid straggler batches."""
    n = len(items)
    if not n:
        return []
    n_batches = math.ceil(n / max(1, max_size))
    base, rem = divmod(n, n_batches)
    out, i = [], 0
    for b in range(n_batches):
        size = base + (1 if b < rem else 0)
        out.append(items[i:i + size])
        i += size
    return out


def _batches(jobs: list, suspects: set) -> list[list]:
    """Balanced chunks of <= BATCH poses; each predicted-empty pose goes last
    in a chunk so an empty-layer abort costs as little as possible."""
    normal = [j for j in jobs if j not in suspects]
    sus = [j for j in jobs if j in suspects]
    out = []
    if normal:
        per = max(1, BATCH - 1) if sus else BATCH
        for chunk in _even_chunks(normal, per):
            if sus:
                chunk = list(chunk) + [sus.pop(0)]
            out.append(chunk)
    if sus:
        out.extend(_even_chunks(sus, BATCH))
    return out


def _merge(a: dict | None, b: dict) -> dict:
    """Combine repeated runs conservatively: any floating / cantilever /
    empty verdict wins; sliced only if every run sliced."""
    if a is None:
        return {**b, "runs": 1}
    return {
        "float": a["float"] or b["float"],
        "cant": a["cant"] or b["cant"],
        "empty": a["empty"] or b["empty"],
        "sliced": a["sliced"] and b["sliced"],
        "msg": a["msg"] or b["msg"],
        "runs": a.get("runs", 1) + 1,
    }


_STATS_LOCK = threading.Lock()


def _slice_all(cfg, bambu, todo, parts, suspects, root, tag, workers, log, stats, t0, ex=None):
    """One Bambu verdict per pose in ``todo``; re-queues poses whose plate was
    aborted by another plate's empty layer.  Returns (verdicts, unresolved).

    ``ex``: a shared executor (the probe's one pool of ``workers`` Bambu
    runs); several confirm runs submit their batches to it concurrently.
    Without it a private pool of ``workers`` is used."""
    got = {}
    for it in range(MAX_PASSES):
        if not todo:
            break
        batches = _batches(todo, suspects)
        fn = lambda ib: _run_batch(cfg, bambu, ib[1], parts, root / f"{tag}_p{it}_b{ib[0]}")
        if ex is not None:
            results = list(ex.map(fn, enumerate(batches)))
        else:
            with ThreadPoolExecutor(max_workers=workers) as own:
                results = list(own.map(fn, enumerate(batches)))
        with _STATS_LOCK:
            stats["bambu_runs"] += len(batches)
            stats["passes"] += 1
        requeue, done = [], 0
        for res in results:
            for job, v in res.items():
                if v["sliced"] or v["empty"]:
                    got[job] = v
                    done += 1
                else:
                    requeue.append(job)
        log(f"  {tag} pass {it + 1}: {len(batches)} project(s), {done} verdicts, "
            f"{len(requeue)} re-queued (aborted by an empty layer) "
            f"[{time.time() - t0:.0f}s]")
        if done == 0:
            # Bambu produced nothing at all: do not loop forever
            log("  probe made no progress; check the Bambu Studio log in " + str(root))
            break
        todo = sorted(requeue)
    return got, todo


def _confirm_order(queue: list, rnd: int) -> list:
    """Pose order (hence project grouping) of confirm run ``rnd``: run 2 keeps
    the sorted order, later runs a fixed shuffle, so concurrent confirm runs
    do not slice the very same projects."""
    q = sorted(queue)
    if rnd > 2:
        random.Random(f"brickprint-probe-run-{rnd}").shuffle(q)
    return q


def _combine_confirm(merged: dict, queue: list, outs: list) -> tuple[list, int]:
    """Fold confirm runs 2..N (``outs``: [(verdicts, unresolved)] in run order,
    all sliced from run 1's clean set ``queue``) into ``merged`` exactly as
    the serial loop did: runs are merged in order and a pose stops at its
    first bad verdict, or at the first run without a verdict (that pose is
    unresolved; its run-1 verdict stays).  Later runs of such a pose are
    discarded.  Returns (unresolved poses, discarded pose slices)."""
    unresolved, discarded = [], 0
    for job in queue:
        for i, (got, _) in enumerate(outs):
            if job not in got:
                unresolved.append(job)
                discarded += sum(1 for g, _ in outs[i + 1:] if job in g)
                break
            merged[job] = _merge(merged.get(job), got[job])
            if bad(merged[job]):
                discarded += sum(1 for g, _ in outs[i + 1:] if job in g)
                break
    return unresolved, discarded


def probe(cfg: dict, parts: dict, cache: VerdictCache, bambu: str,
          workers: int | None = None, log=print) -> tuple[dict, dict]:
    """Bambu verdicts for every pose of every part.

    ``parts``: {part_file: {"V", "T", "mesh_key", "cands": {up: (R, metrics)}}}.
    Returns ({part_file: {up: verdict}} for parts with all 6 poses known,
    stats).  Parts whose poses could not all be resolved are left out (the
    caller falls back to the emulator for them).
    """
    t0 = time.time()
    fp = threemf.composer(cfg).fingerprint()
    keys = {(pf, up): pose_key(p["mesh_key"], up, fp) for pf, p in parts.items() for up in UPS}
    todo = [job for job, k in keys.items() if cache.get(job[0], k) is None]
    stats = {"parts": len(parts), "poses": len(keys), "cached": len(keys) - len(todo),
             "probed": 0, "passes": 0, "bambu_runs": 0, "unresolved": 0, "seconds": 0.0}
    if todo:
        bed, depth = float(cfg["bed"]), float(cfg["depth"])
        dirty: set[str] = set()
        for pf in {j[0] for j in todo}:
            p = parts[pf]
            p["posed"] = {up: _posed(p["V"], p["T"], p["cands"][up][0]) for up in UPS}
        # a pose wider than the bed cannot be probed (nor printed that way)
        rest = []
        for pf, up in todo:
            W = parts[pf]["posed"][up]
            if W[:, 0].max() > bed - 0.1 or W[:, 1].max() > depth - 0.1:
                cache.put(pf, keys[(pf, up)], {"float": False, "cant": False, "empty": False,
                                                "sliced": False, "oversize": True,
                                                "msg": "larger than the bed"})
                dirty.add(pf)
            else:
                rest.append((pf, up))
        todo = sorted(rest)
        suspects = {(pf, up) for pf, up in todo
                    if float(parts[pf]["cands"][up][1].get("empty_layers", 0)) > 0}
        workers = workers or default_workers(cfg)
        log(f"probe: {len(todo)} poses of {len({j[0] for j in todo})} parts "
            f"({stats['cached']} cached), {workers} Bambu Studio workers ...")
        root = Path(tempfile.mkdtemp(prefix="probe_", dir=Path(cfg["cache_dir"])))
        keep = False    # an exception (interrupt, slicer crash) removes the dir
        try:
            runs = int(cfg.get("probe_runs") or CONFIRM_RUNS)
            # one pool of ``workers`` Bambu runs for run 1 and every confirm
            # run: confirm runs share it so concurrency never exceeds
            # ``workers``; their batches fill otherwise idle workers
            pool = ThreadPoolExecutor(max_workers=workers)
            try:
                got, left = _slice_all(cfg, bambu, todo, parts, suspects, root, "r1",
                                       workers, log, stats, t0, ex=pool)
                unresolved: list = list(left)
                merged: dict = {job: _merge(None, v) for job, v in got.items()}
                # only poses clean in run 1 need confirmation; a single
                # floating / empty verdict is final.  The rule is AND ("clean
                # in every run"), so runs 2..N all start from run 1's clean
                # set together instead of serially
                queue = sorted(j for j in got if not bad(merged[j]))
                if runs > 1 and queue:
                    log(f"  run 1 done: {len(queue)} clean pose(s) re-sliced in runs 2-{runs} "
                        f"together on the {workers} workers (Bambu's floating check is not "
                        f"deterministic)")
                    with ThreadPoolExecutor(max_workers=runs - 1) as drivers:
                        futs = [drivers.submit(_slice_all, cfg, bambu, _confirm_order(queue, rnd),
                                               parts, suspects, root, f"r{rnd}", workers, log,
                                               stats, t0, pool)
                                for rnd in range(2, runs + 1)]
                        try:
                            outs = [f.result() for f in futs]
                        except BaseException:
                            # cancel queued batches and wait for in-flight Bambu
                            # subprocesses before finally removes ``root``
                            pool.shutdown(wait=True, cancel_futures=True)
                            raise
                    more, discarded = _combine_confirm(merged, queue, outs)
                    unresolved += more
                    clean = sum(1 for j in queue if not bad(merged[j]) and j not in more)
                    log(f"  runs 2-{runs} done: {clean} of {len(queue)} pose(s) clean in all "
                        f"{runs} runs" + (f"; {discarded} slice(s) of poses already flagged in "
                                          f"an earlier run discarded" if discarded else "")
                        + f" [{time.time() - t0:.0f}s]")
            finally:
                pool.shutdown(wait=True)
            for job, v in merged.items():
                cache.put(job[0], keys[job], v)
                dirty.add(job[0])
            stats["probed"] = len(merged)
            todo = unresolved
            stats["unresolved"] = len(todo)
            # unresolved poses: keep the Bambu logs for diagnosis (the
            # "probe made no progress" message points here)
            keep = bool(todo)
        finally:
            if not keep:
                _remove_workdir(root)
        for pf in {j[0] for j in keys}:
            parts[pf].pop("posed", None)
        for pf in sorted(dirty):
            if cache.load(pf):
                cache.save(pf)
    verdicts = {}
    for pf in parts:
        vs = {up: cache.get(pf, keys[(pf, up)]) for up in UPS}
        if all(v is not None for v in vs.values()):
            verdicts[pf] = vs
    stats["seconds"] = round(time.time() - t0, 1)
    stats["parts_verified"] = len(verdicts)
    return verdicts, stats


# --------------------------------------------------------------------------
# whole-project check
# --------------------------------------------------------------------------

PLATE_CHECK_RUNS = 2        # independent full slices; a plate flagged by any run counts


def _project_hash(path: Path) -> str:
    """Content hash of a generated 3MF, ignoring the random p:UUIDs."""
    import hashlib
    import zipfile

    h = hashlib.sha1(f"p{PROBE_VERSION}".encode())
    uu = re.compile(rb'p:UUID="[^"]*"')
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            h.update(name.encode())
            h.update(uu.sub(b"", z.read(name)))
    return h.hexdigest()


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=0))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _remove_gcodes(directory: Path) -> None:
    """Remove sliced ``*.gcode`` files under ``directory`` (including subdirs)."""
    try:
        for g in directory.rglob("*.gcode"):
            try:
                g.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _plate_check_slices(cfg, bambu, project, root, runs, workers, log, plates,
                        pkeys: dict[int, str] | None = None,
                        gcode_cache: Path | None = None,
                        bambu_ver: str = "unknown") -> tuple[dict, dict[int, str], dict[int, str]]:
    """plate_check's slicing part: (flagged_dict, {orig_pid: msg}, {orig_pid: err}).

    When ``plates`` is a proper subset of the project's plates and the 3MF
    contains ``3D/3dmodel.model``, writes one filtered mini-3MF and slices it
    with ``--slice 0`` (1 Bambu project load per run instead of one per plate).
    Populates ``gcode_cache`` for cleanly sliced plates when ``pkeys`` is set.
    """
    from .verify import slice_plates
    import xml.etree.ElementTree as ET
    import zipfile

    with zipfile.ZipFile(project) as z:
        names = set(z.namelist())
        ms = ET.fromstring(z.read("Metadata/model_settings.config"))
    all_ids = [int(p.find("metadata[@key='plater_id']").get("value")) for p in ms.findall("plate")]
    target_ids = list(dict.fromkeys(plates)) if plates else all_ids

    src_path = Path(project)
    new_to_orig: dict[int, int] = {pid: pid for pid in all_ids}
    use_mini = (
        bool(plates)
        and set(target_ids) < set(all_ids)
        and "3D/3dmodel.model" in names
    )
    if use_mini:
        src_path = root / "uncached.3mf"
        new_to_orig = slice_cache.filter_plates_3mf(project, src_path, target_ids)
        slice_ids = list(new_to_orig)
    else:
        slice_ids = target_ids

    def one(i):
        d = root / f"run{i}"
        d.mkdir()
        if plates and not use_mini and set(target_ids) < set(all_ids):
            # Fallback for minimal stub 3MFs without 3D/3dmodel.model
            texts, failed, gfiles = [], {}, {}
            for pid in target_ids:
                g, t, f = slice_plates(bambu, src_path, d / f"plate{pid}", [pid],
                                       log=lambda *_: None, timeout=3 * TIMEOUT_S)
                texts.append(t)
                failed.update(f)
                gfiles.update(g)
            return gfiles, "\n".join(texts), failed
        gfiles, text, failed = slice_plates(bambu, src_path, d, slice_ids,
                                            log=lambda *_: None, timeout=3 * TIMEOUT_S)
        return gfiles, text, failed

    # a full-project slice uses ~6 cores: never more than default_workers()
    workers = workers or max(1, min(runs, default_workers(cfg)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        outs = list(ex.map(one, range(runs)))

    flagged: dict = {}
    errors: dict[int, str] = {}
    per_plate_warns: dict[int, list[str]] = {}
    best_gcode: dict[int, Path] = {}

    for gfiles, text, failed in outs:
        for new_pid, why in failed.items():
            orig_pid = new_to_orig.get(new_pid, new_pid)
            errors.setdefault(orig_pid, why[:300])
        for m in _WARN.finditer(text):
            msg = m.group(2).strip()
            new_pid = int(m.group(1))
            orig_pid = new_to_orig.get(new_pid, new_pid)
            if msg and msg not in per_plate_warns.get(orig_pid, ()):
                per_plate_warns.setdefault(orig_pid, []).append(msg)
            if "floating" in msg or "empty layer" in msg:
                if msg[:200] not in flagged.get(orig_pid, ""):
                    flagged[orig_pid] = (flagged[orig_pid] + " | " if orig_pid in flagged else "") + msg[:200]
        if pkeys and gcode_cache is not None:
            for new_pid, gpath in (gfiles or {}).items():
                orig_pid = new_to_orig.get(new_pid, new_pid)
                if new_pid in failed or orig_pid not in pkeys:
                    continue
                gp = Path(gpath)
                if gp.is_file() and gp.stat().st_size > 0:
                    best_gcode[orig_pid] = gp

    # Populate shared G-code cache for plates that succeeded in every run
    if pkeys and gcode_cache is not None:
        for orig_pid, gp in best_gcode.items():
            if orig_pid in errors:
                continue
            slice_cache.put(
                gcode_cache, pkeys[orig_pid], gp,
                bambu_version=bambu_ver,
                warnings=per_plate_warns.get(orig_pid, []),
                source="brickprint.plate_check",
            )

    # Drop large G-code promptly (workdir is also deleted in finally)
    _remove_gcodes(root)

    for pid, why in errors.items():
        log(f"WARNING: plate check: plate {pid} does not slice: {why[:160]}")
    out = dict(flagged)
    if errors:
        out["errors"] = errors
    return out, flagged, errors


def plate_check(cfg: dict, bambu: str, project: Path, runs: int | None = None,
                workers: int | None = None, log=print, plates: list[int] | None = None) -> dict:
    """Slice every plate of ``project`` (or only ``plates``) with Bambu Studio
    and return {plate_id: warning text} for plates with floating regions /
    floating cantilever / empty layer warnings (union over ``runs``
    independent runs, because Bambu's floating check is not deterministic).
    Several warnings of one plate are joined with " | " (Bambu names one
    object per warning).

    Parts that are clean alone can still be flagged on a full plate, so the
    build runs this after packing.  A plate that Bambu cannot slice at all
    (path conflict, G-code outside the bed) no longer hides the plates after
    it: those are sliced in a filtered batch.  Its error is returned under the
    key ``"errors"`` ({plate_id: text}) when present.  Results are cached per
    plate content hash (so multi-round plate checks re-slice only changed
    plates), and cleanly sliced G-code is stored in the shared G-code cache."""
    import zipfile

    t0 = time.time()
    runs = runs or int(cfg.get("plate_check_runs") or PLATE_CHECK_RUNS)
    cache_file = Path(cfg["cache_dir"]) / "plate_checks.json"
    try:
        known = json.loads(cache_file.read_text()) if cache_file.is_file() else {}
    except (OSError, ValueError):
        known = {}

    # Prefer per-plate content-hash caching when the 3MF has full 3D structure
    bambu_ver = slice_cache.bambu_version_for(bambu)
    pkeys: dict[int, str] | None = None
    try:
        with zipfile.ZipFile(project) as z:
            has_model = "3D/3dmodel.model" in z.namelist()
        if has_model:
            pkeys = slice_cache.plate_keys(project, bambu_ver)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        pkeys = None

    if pkeys:
        target_ids = list(dict.fromkeys(plates)) if plates else sorted(pkeys)
        cached_flagged: dict[int, str] = {}
        cached_errors: dict[int, str] = {}
        uncached: list[int] = []
        for pid in target_ids:
            pk = pkeys.get(pid)
            if pk is None:
                uncached.append(pid)
                continue
            entry_key = f"plate:{pk}:p{PROBE_VERSION}:x{runs}"
            if entry_key in known:
                rec = known[entry_key]
                if isinstance(rec, dict):
                    if rec.get("msg"):
                        cached_flagged[pid] = rec["msg"]
                    if rec.get("error"):
                        cached_errors[pid] = rec["error"]
            else:
                uncached.append(pid)
        if not uncached:
            out: dict = dict(cached_flagged)
            if cached_errors:
                out["errors"] = cached_errors
            log(f"plate check: cached ({len(cached_flagged)} flagged plate(s))")
            return out

        gcode_cache = slice_cache.default_dir(cfg)
        root = Path(tempfile.mkdtemp(prefix="platecheck_", dir=Path(cfg["cache_dir"])))
        try:
            _, new_flagged, new_errors = _plate_check_slices(
                cfg, bambu, project, root, runs, workers, log,
                plates=uncached if len(uncached) < len(pkeys) else None,
                pkeys=pkeys, gcode_cache=gcode_cache, bambu_ver=bambu_ver,
            )
        finally:
            _remove_workdir(root)

        # Cached only when every run completed (an exception never gets here)
        for pid in uncached:
            pk = pkeys.get(pid)
            if pk is None:
                continue
            entry_key = f"plate:{pk}:p{PROBE_VERSION}:x{runs}"
            rec = {}
            if pid in new_flagged:
                rec["msg"] = new_flagged[pid]
            if pid in new_errors:
                rec["error"] = new_errors[pid]
            known[entry_key] = rec
        _write_json_atomic(cache_file, known)

        merged_flagged = {**cached_flagged, **new_flagged}
        merged_errors = {**cached_errors, **new_errors}
        out = dict(merged_flagged)
        if merged_errors:
            out["errors"] = merged_errors
        cached_n = len(target_ids) - len(uncached)
        log(f"plate check: {len(uncached)} plate(s) x {runs} slice(s)"
            + (f" ({cached_n} cached)" if cached_n else "")
            + f", {len(merged_flagged)} plate(s) flagged [{time.time() - t0:.0f}s]")
        return out

    # Fallback path for minimal stub 3MFs without 3D/3dmodel.model
    key = f"{_project_hash(project)}:x{runs}"
    if plates:
        key += ":p" + ",".join(str(p) for p in sorted(plates))
    if key in known:
        log(f"plate check: cached ({len([k for k in known[key] if k != 'errors'])} flagged plate(s))")
        out = {int(k): v for k, v in known[key].items() if k != "errors"}
        if known[key].get("errors"):
            out["errors"] = {int(k): v for k, v in known[key]["errors"].items()}
        return out
    root = Path(tempfile.mkdtemp(prefix="platecheck_", dir=Path(cfg["cache_dir"])))
    try:
        flagged, only_flagged, _ = _plate_check_slices(
            cfg, bambu, project, root, runs, workers, log, plates,
        )
    finally:
        _remove_workdir(root)
    # cached only when every run completed (an exception never gets here)
    known[key] = {str(k): v for k, v in flagged.items()}
    _write_json_atomic(cache_file, known)
    log(f"plate check: {runs} full slice(s), {len(only_flagged)} plate(s) flagged "
        f"[{time.time() - t0:.0f}s]")
    return flagged


# --------------------------------------------------------------------------
# final geometry (after the ceiling recipe) of single parts
# --------------------------------------------------------------------------

FINAL_KEYS = ("V", "T", "rib", "bands", "part_meta", "object_meta")


def final_key(m: dict, fingerprint: str) -> str:
    """Cache key of a part's final, as-written geometry and per-part settings."""
    import hashlib
    h = hashlib.sha1(f"final:p{PROBE_VERSION}:s{fingerprint}".encode())
    for k in FINAL_KEYS:
        v = m.get(k)
        h.update(k.encode())
        if v is None:
            continue
        if k in ("V", "T"):
            h.update(np.ascontiguousarray(np.round(np.asarray(v, dtype=float), 4)).tobytes())
        elif k == "rib":
            h.update(np.round(np.asarray(v[0], dtype=float), 4).tobytes())
            h.update(np.asarray(v[1], dtype=np.int64).tobytes())
        else:
            h.update(json.dumps(v, sort_keys=True, default=str).encode())
    return "final:" + h.hexdigest()[:16]


def check_final(cfg: dict, bambu: str, parts: dict, cache: VerdictCache, runs: int = 2,
                workers: int | None = None, log=print) -> dict:
    """Bambu's floating verdict for parts as the build writes them (final
    mesh with raised ceilings, ribs, height-range bands, per-part settings;
    supports off, no enforcers), one part per plate, merged over ``runs``
    runs (any floating run counts).  ``parts``: {name: mesh dict}; the name
    is only a label (the cache file is per ``m["file"]`` or name).
    Returns {name: verdict}."""
    fp = threemf.composer(cfg).fingerprint()
    keys = {n: final_key(m, fp) for n, m in parts.items()}
    owner = {n: m.get("file", n) for n, m in parts.items()}
    out, todo = {}, []
    for n in parts:
        v = cache.get(owner[n], keys[n])
        if v is not None and v.get("runs", 0) >= runs:
            out[n] = v
        else:
            todo.append(n)
    if not todo:
        return out
    t0 = time.time()
    root = Path(tempfile.mkdtemp(prefix="final_", dir=Path(cfg["cache_dir"])))
    try:
        bed, depth, margin = float(cfg["bed"]), float(cfg["depth"]), float(cfg["margin"])
        chunks = _even_chunks(todo, BATCH)

        def write(ci, chunk):
            meshes, plates = {}, []
            for i, n in enumerate(chunk, 1):
                m = parts[n]
                key = f"p{i}_{Path(owner[n]).stem}.dat"
                meshes[key] = {k: m[k] for k in FINAL_KEYS if m.get(k) is not None}
                V = np.asarray(m["V"], dtype=float)
                w, d = float(np.ptp(V[:, 0])), float(np.ptp(V[:, 1]))
                plates.append({"color_id": "probe", "rgb": "FFFFFF", "color_name": "probe", "kind": "",
                               "index": i, "count": len(chunk), "support": "",
                               "items": [(key, (bed - w) / 2 - margin, (depth - d) / 2 - margin, w, d)]})
            src = root / f"c{ci}.3mf"
            threemf.write_project(cfg, {"set_num": "probe", "name": "brickprint final check"},
                                  plates, meshes, src, verdict=True)
            return src

        srcs = [write(ci, ch) for ci, ch in enumerate(chunks)]
        jobs = [(ci, r) for r in range(runs) for ci in range(len(chunks))]

        def one(job):
            ci, r = job
            d = root / f"c{ci}_r{r}"
            d.mkdir()
            from .verify import slice_plates
            _, text, failed = slice_plates(bambu, srcs[ci], d, list(range(1, len(chunks[ci]) + 1)),
                                           log=lambda *_: None, timeout=TIMEOUT_S)
            _remove_gcodes(d)
            res = {}
            warns: dict[int, list[str]] = {}
            for m in _WARN.finditer(text):
                warns.setdefault(int(m.group(1)), []).append(m.group(2).strip())
            for pi, n in enumerate(chunks[ci], 1):
                t = " | ".join(warns.get(pi, []))
                res[n] = {"float": "floating regions" in t, "cant": "cantilever" in t,
                          "empty": "empty layer" in t, "sliced": pi not in failed, "msg": t[:200]}
            return res

        workers = workers or default_workers(cfg)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(one, jobs))
    finally:
        _remove_workdir(root)
    # verdicts are cached only after every run completed
    merged: dict = {}
    for res in results:
        for n, v in res.items():
            merged[n] = _merge(merged.get(n), v)
    for n, v in merged.items():
        cache.put(owner[n], keys[n], v)
        out[n] = v
    for f in {owner[n] for n in merged}:
        cache.save(f)
    log(f"final-geometry check: {len(todo)} part(s) x {runs} run(s), "
        f"{sum(1 for n in todo if bad(merged[n]))} floating [{time.time() - t0:.0f}s]")
    return out
