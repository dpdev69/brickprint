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
  floating / cantilever / empty verdict is final.

Verdicts are cached per part next to the mesh cache
(``<cache>/meshes/<lib>/<part>.verdicts.json``), keyed by repaired-mesh hash +
REPAIR_VERSION + ORIENT_VERSION + PROBE_VERSION + pose label, so re-runs are
instant and any mesh or policy change re-probes only what changed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import orient, repair, threemf

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
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._mem.get(part_file, {}), indent=0, sort_keys=True))
        os.replace(tmp, p)


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


def _batches(jobs: list, suspects: set) -> list[list]:
    """Chunks of <= BATCH poses; each predicted-empty pose goes last in a
    chunk so an empty-layer abort costs as little as possible."""
    normal = [j for j in jobs if j not in suspects]
    sus = [j for j in jobs if j in suspects]
    out = []
    per = BATCH - 1 if sus else BATCH
    for b in range(0, len(normal), per):
        chunk = normal[b:b + per]
        if sus:
            chunk.append(sus.pop(0))
        out.append(chunk)
    for b in range(0, len(sus), BATCH):
        out.append(sus[b:b + BATCH])
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


def _slice_all(cfg, bambu, todo, parts, suspects, root, tag, workers, log, stats, t0):
    """One Bambu verdict per pose in ``todo``; re-queues poses whose plate was
    aborted by another plate's empty layer.  Returns (verdicts, unresolved)."""
    got = {}
    for it in range(MAX_PASSES):
        if not todo:
            break
        batches = _batches(todo, suspects)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(
                lambda ib: _run_batch(cfg, bambu, ib[1], parts, root / f"{tag}_p{it}_b{ib[0]}"),
                enumerate(batches)))
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
            merged: dict = {}
            unresolved: list = []
            queue = todo
            for rnd in range(1, runs + 1):
                if not queue:
                    break
                got, left = _slice_all(cfg, bambu, queue, parts, suspects, root, f"r{rnd}",
                                       workers, log, stats, t0)
                unresolved += left
                for job, v in got.items():
                    merged[job] = _merge(merged.get(job), v)
                # only poses that are still clean need another confirmation run;
                # a single floating / empty verdict is final
                queue = sorted(j for j in got if not bad(merged[j]))
                if rnd < runs and queue:
                    log(f"  run {rnd} done: {len(queue)} clean pose(s) re-sliced to confirm "
                        f"(Bambu's floating check is not deterministic)")
            for job, v in merged.items():
                cache.put(job[0], keys[job], v)
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
        for pf in {j[0] for j in keys}:
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


def _plate_check_slices(cfg, bambu, project, root, runs, workers, log, plates) -> dict:
    """plate_check's slicing part: {plate_id: warnings} (+ "errors")."""
    from .verify import slice_plates
    import zipfile
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(project) as z:
        ms = ET.fromstring(z.read("Metadata/model_settings.config"))
    all_ids = [int(p.find("metadata[@key='plater_id']").get("value")) for p in ms.findall("plate")]

    def one(i):
        d = root / f"run{i}"
        d.mkdir()
        if plates:   # a subset: one Bambu run per plate
            texts, failed = [], {}
            for pid in plates:
                _, t, f = slice_plates(bambu, project, d / f"plate{pid}", [pid], log=lambda *_: None,
                                       timeout=3 * TIMEOUT_S)
                texts.append(t)
                failed.update(f)
            return "\n".join(texts), failed
        _, text, failed = slice_plates(bambu, project, d, all_ids, log=lambda *_: None,
                                       timeout=3 * TIMEOUT_S)
        return text, failed

    # a full-project slice uses ~6 cores: never more than default_workers()
    workers = workers or max(1, min(runs, default_workers(cfg)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        outs = list(ex.map(one, range(runs)))
    flagged: dict = {}
    errors: dict[int, str] = {}
    for text, failed in outs:
        for pid, why in failed.items():
            errors.setdefault(pid, why[:300])
        for m in _WARN.finditer(text):
            msg = m.group(2).strip()
            if "floating" in msg or "empty layer" in msg:
                pid = int(m.group(1))
                if msg[:200] not in flagged.get(pid, ""):
                    flagged[pid] = (flagged[pid] + " | " if pid in flagged else "") + msg[:200]
    for pid, why in errors.items():
        log(f"WARNING: plate check: plate {pid} does not slice: {why[:160]}")
    if errors:
        flagged["errors"] = errors
    return flagged


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
    it: those are sliced one by one.  Its error is returned under the key
    ``"errors"`` ({plate_id: text}) when present.  Results are cached by
    project content."""
    t0 = time.time()
    runs = runs or int(cfg.get("plate_check_runs") or PLATE_CHECK_RUNS)
    cache_file = Path(cfg["cache_dir"]) / "plate_checks.json"
    key = f"{_project_hash(project)}:x{runs}"
    if plates:
        key += ":p" + ",".join(str(p) for p in sorted(plates))
    try:
        known = json.loads(cache_file.read_text()) if cache_file.is_file() else {}
    except ValueError:
        known = {}
    if key in known:
        log(f"plate check: cached ({len([k for k in known[key] if k != 'errors'])} flagged plate(s))")
        out = {int(k): v for k, v in known[key].items() if k != "errors"}
        if known[key].get("errors"):
            out["errors"] = {int(k): v for k, v in known[key]["errors"].items()}
        return out
    root = Path(tempfile.mkdtemp(prefix="platecheck_", dir=Path(cfg["cache_dir"])))
    try:
        flagged = _plate_check_slices(cfg, bambu, project, root, runs, workers, log, plates)
    finally:
        _remove_workdir(root)
    # cached only when every run completed (an exception never gets here)
    known[key] = {str(k): v for k, v in flagged.items()}
    cache_file.write_text(json.dumps(known, indent=0))
    log(f"plate check: {runs} full slice(s), {len(flagged)} plate(s) flagged "
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
        chunks = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]

        def write(ci, chunk):
            meshes, plates = {}, []
            for i, n in enumerate(chunk, 1):
                m = parts[n]
                key = f"{Path(owner[n]).stem}.dat"
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
            res = {}
            warns: dict[int, str] = {}
            for m in _WARN.finditer(text):
                warns[int(m.group(1))] = warns.get(int(m.group(1)), "") + m.group(2).strip()
            for pi, n in enumerate(chunks[ci], 1):
                t = warns.get(pi, "")
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
