"""Shared content-addressed per-plate G-code cache & 3MF plate filter.

Stdlib-only (no numpy import) so both ``brickprint`` (``plate_check``,
``verify``) and ``bambu-oracle`` can share cached per-plate G-code and slicing
warnings across builds without redundant Bambu Studio CLI runs.

On-disk layout (``SCHEMA_VERSION = 1``)::

    <root>/v1/<key[:2]>/<key>/
        plate.gcode   # written atomically first (only when rc == 0 & size > 0)
        meta.json     # written atomically last; mtime updated on hit (LRU)

Per-plate keys are SHA-256 hashes of every slice-affecting input for that
plate: ``bambu_version``, canonical ``cli_flags`` (default ``arrange=0,
orient=0``), canonical ``Metadata/project_settings.config`` (with per-plate
``wipe_tower_x``/``wipe_tower_y`` narrowed to the plate's own slot), plate
metadata (minus plate number/name/thumbnail paths), per-plate
``filament_sequence.json``, ``Metadata/custom_gcode_per_layer.xml``, and each
object's bed-local transform, object/part metadata, ``layer_config_ranges.xml``
bands, and referenced 3D mesh bytes (with volatile ``p:UUID`` stripped).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import plistlib
import re
import shutil
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from functools import lru_cache
from pathlib import Path

SCHEMA_VERSION = 1
ENV_CACHE_DIR = "BAMBU_GCODE_CACHE_DIR"
ENV_MAX_GB = "BAMBU_GCODE_CACHE_MAX_GB"
ENV_MAX_DAYS = "BAMBU_GCODE_CACHE_MAX_DAYS"
DEFAULT_MAX_GB = 4.0
DEFAULT_MAX_DAYS = 14.0
ACTIVE_GUARD_S = 60.0
DEFAULT_CLI_FLAGS = {"arrange": 0, "orient": 0}

_CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_PROD_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
_NS = {"c": _CORE_NS}
_P_PATH = f"{{{_PROD_NS}}}path"
_P_UUID = f"{{{_PROD_NS}}}UUID"

_UUID_RE = re.compile(rb'\s+p:UUID="[^"]*"')
_OBJ_ID_RE = re.compile(rb'\bid="([^"]+)"')

# Plate-level metadata keys that never change slicing output (renumbered or GUI-only).
_IGNORE_PLATE_META = frozenset({
    "plater_id", "plater_name", "gcode_file",
    "thumbnail_file", "thumbnail_no_light_file", "top_file", "pick_file",
})
# Part-level GUI provenance keys written by Bambu Studio re-export, not used by slicer.
_IGNORE_PART_META = frozenset({
    "source_file", "source_object_id", "source_volume_id",
})
# Model-instance keys that are IDs/labels rather than geometry/placement.
_IGNORE_INST_META = frozenset({"object_id", "identify_id"})


def default_dir(cfg: dict | None = None) -> Path:
    """Resolve the shared G-code cache directory."""
    env = os.environ.get(ENV_CACHE_DIR, "").strip()
    if env:
        return Path(env).expanduser()
    if cfg and cfg.get("cache_dir"):
        return Path(str(cfg["cache_dir"])).expanduser() / "gcode_cache"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "brickprint" / "gcode_cache"
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "brickprint" / "gcode_cache"


def _app_from_exe(exe: str | Path | None) -> Path | None:
    if not exe:
        return None
    p = Path(exe).resolve()
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    return None


@lru_cache(maxsize=32)
def _cached_bambu_version(exe_str: str) -> str:
    app = _app_from_exe(exe_str)
    if app is None:
        return "unknown"
    plist = app / "Contents" / "Info.plist"
    if not plist.is_file():
        return "unknown"
    try:
        data = plistlib.loads(plist.read_bytes())
        return str(data.get("CFBundleShortVersionString") or "unknown")
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
        return "unknown"


def bambu_version_for(bambu_exe: str | Path | None) -> str:
    """Return Bambu Studio version string for ``bambu_exe``, or ``'unknown'``."""
    if not bambu_exe:
        return "unknown"
    return _cached_bambu_version(str(bambu_exe))


def _bed_dims(ps: dict) -> tuple[float, float]:
    """(bed_x, bed_y) from ``project_settings.config`` ``printable_area``."""
    area = ps.get("printable_area")
    if isinstance(area, list) and area:
        xs, ys = [], []
        for pt in area:
            if isinstance(pt, str) and "x" in pt:
                try:
                    a, b = pt.split("x", 1)
                    xs.append(float(a))
                    ys.append(float(b))
                except ValueError:
                    pass
        if xs and ys:
            dx, dy = max(xs) - min(xs), max(ys) - min(ys)
            if dx > 0 and dy > 0:
                return dx, dy
    return 180.0, 180.0


def _plate_origin(pid: int, n_plates: int, bed: float, depth: float) -> tuple[float, float]:
    cols = math.ceil(math.sqrt(max(n_plates, 1)))
    return ((pid - 1) % cols) * bed * 1.2, -((pid - 1) // cols) * depth * 1.2


def _parse_transform(text: str | None) -> list[float]:
    if not text:
        return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    vals = [float(x) for x in text.split()]
    if len(vals) != 12:
        return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    return vals


def _fmt_transform(vals: list[float]) -> str:
    out = []
    for v in vals:
        r = round(v, 7)
        if r == 0.0:
            out.append("0")
        else:
            s = f"{r:.7f}".rstrip("0").rstrip(".")
            out.append("0" if s in ("-0", "") else s)
    return " ".join(out)


def _norm_transform(vals: list[float], ox: float = 0.0, oy: float = 0.0) -> list[float]:
    out = list(vals)
    out[9] -= ox
    out[10] -= oy
    return [0.0 if round(v, 4) == 0.0 else round(v, 4) for v in out]


def _hash_object_models(z: zipfile.ZipFile, model_path: str) -> dict[str, str]:
    """Hash each ``<object id="...">`` payload in a ``3D/Objects/*.model`` file.

    Strips volatile ``p:UUID`` attributes and normalises away the outer
    ``id`` attribute so identical geometry renumbered in a mini-3MF hashes
    identically.
    """
    clean_path = model_path.lstrip("/")
    try:
        raw = z.read(clean_path)
    except KeyError:
        return {}
    stripped = _UUID_RE.sub(b"", raw) if b"p:UUID=" in raw else raw
    out: dict[str, str] = {}
    pos = 0
    while True:
        start = stripped.find(b"<object", pos)
        if start < 0:
            break
        gt = stripped.find(b">", start)
        if gt < 0:
            break
        end = stripped.find(b"</object>", gt)
        if end < 0:
            break
        attrs = stripped[start + 7:gt]
        body = stripped[gt + 1:end]
        pos = end + 9
        im = _OBJ_ID_RE.search(attrs)
        if not im:
            continue
        oid = im.group(1).decode("utf-8", errors="replace")
        norm_attrs = _OBJ_ID_RE.sub(b"", attrs).strip()
        h = hashlib.sha256()
        h.update(norm_attrs)
        h.update(b"\0")
        h.update(body)
        out[oid] = h.hexdigest()
    return out


def _canonical_layer_ranges(z: zipfile.ZipFile) -> dict[str, list]:
    """Parse ``Metadata/layer_config_ranges.xml`` into ``{object_id: [...]}``."""
    try:
        raw = z.read("Metadata/layer_config_ranges.xml")
    except KeyError:
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    out: dict[str, list] = {}
    for obj in root.findall("object"):
        oid = obj.get("id")
        if not oid:
            continue
        ranges = []
        for rng in obj.findall("range"):
            opts = sorted(
                (opt.get("opt_key") or "", (opt.text or "").strip())
                for opt in rng.findall("option")
            )
            try:
                lo = round(float(rng.get("min_z") or 0), 4)
                hi = round(float(rng.get("max_z") or 0), 4)
            except ValueError:
                lo, hi = rng.get("min_z"), rng.get("max_z")
            ranges.append([lo, hi, opts])
        if ranges:
            out[oid] = sorted(ranges)
    return out


def _canonical_object_settings(ms_root: ET.Element) -> dict[str, dict]:
    """Canonical slicing-relevant metadata & parts per ``<object id="...">``."""
    out: dict[str, dict] = {}
    for obj in ms_root.findall("object"):
        oid = obj.get("id")
        if not oid:
            continue
        meta = sorted(
            (m.get("key"), m.get("value"))
            for m in obj.findall("metadata")
            if m.get("key")
        )
        parts = []
        for part in obj.findall("part"):
            pmeta = sorted(
                (m.get("key"), m.get("value"))
                for m in part.findall("metadata")
                if m.get("key") and m.get("key") not in _IGNORE_PART_META
            )
            parts.append({"subtype": part.get("subtype", "normal_part"), "meta": pmeta})
        out[oid] = {"meta": meta, "parts": parts}
    return out


def _canonical_cli_flags(cli_flags: dict | None) -> dict:
    flags = dict(DEFAULT_CLI_FLAGS)
    if cli_flags:
        flags.update(cli_flags)
    return {str(k): flags[k] for k in sorted(flags)}


def plate_keys(
    path_3mf: Path | str,
    bambu_version: str,
    cli_flags: dict | None = None,
) -> dict[int, str]:
    """Compute canonical content-addressed SHA-256 slice keys per plate ID.

    Returns ``{plater_id: sha256_hex}``.  Plates with identical bed-local
    geometry, settings, metadata, and slicer version produce the exact same
    key whether they come from a full project, a supports-off copy, or a
    filtered mini-3MF.
    """
    flags = _canonical_cli_flags(cli_flags)
    with zipfile.ZipFile(path_3mf) as z:
        names = set(z.namelist())
        ps = json.loads(z.read("Metadata/project_settings.config")) if "Metadata/project_settings.config" in names else {}
        bed, depth = _bed_dims(ps)

        ms_root = ET.fromstring(z.read("Metadata/model_settings.config"))
        obj_settings = _canonical_object_settings(ms_root)
        layer_ranges = _canonical_layer_ranges(z)

        fseq = {}
        if "Metadata/filament_sequence.json" in names:
            try:
                fseq = json.loads(z.read("Metadata/filament_sequence.json"))
            except ValueError:
                fseq = {}
        custom_gcode_sha = ""
        if "Metadata/custom_gcode_per_layer.xml" in names:
            custom_gcode_sha = hashlib.sha256(z.read("Metadata/custom_gcode_per_layer.xml")).hexdigest()

        model_root = ET.fromstring(z.read("3D/3dmodel.model"))
        build_items: dict[str, list[float]] = {}
        for it in model_root.findall("c:build/c:item", _NS):
            oid = it.get("objectid")
            if oid:
                build_items[oid] = _parse_transform(it.get("transform"))

        res_comps: dict[str, list[tuple[str, str, list[float]]]] = {}
        inline_hashes: dict[str, str] = {}
        for obj in model_root.findall("c:resources/c:object", _NS):
            oid = obj.get("id")
            if not oid:
                continue
            comps = []
            for comp in obj.findall(".//c:component", _NS):
                cpath = (comp.get(_P_PATH) or "").replace("//", "/")
                cid = comp.get("objectid") or ""
                comps.append((cpath, cid, _parse_transform(comp.get("transform"))))
            if comps:
                res_comps[oid] = comps
            else:
                mesh = obj.find("c:mesh", _NS)
                if mesh is not None:
                    raw_mesh = _UUID_RE.sub(b"", ET.tostring(mesh, encoding="utf-8"))
                    inline_hashes[oid] = hashlib.sha256(raw_mesh).hexdigest()

        model_hash_cache: dict[str, dict[str, str]] = {}

        def mesh_hash(cpath: str, cid: str) -> str:
            if not cpath:
                return inline_hashes.get(cid, "")
            if cpath not in model_hash_cache:
                model_hash_cache[cpath] = _hash_object_models(z, cpath)
            return model_hash_cache[cpath].get(cid, "")

        plate_els = ms_root.findall("plate")
        n_plates = len(plate_els)
        out: dict[int, str] = {}
        for pl in plate_els:
            pid_el = pl.find("metadata[@key='plater_id']")
            if pid_el is None or not pid_el.get("value"):
                continue
            pid = int(pid_el.get("value"))
            ox, oy = _plate_origin(pid, n_plates, bed, depth)

            pmeta = sorted(
                (m.get("key"), m.get("value"))
                for m in pl.findall("metadata")
                if m.get("key") and m.get("key") not in _IGNORE_PLATE_META
            )

            ps_plate = dict(ps)
            for wk in ("wipe_tower_x", "wipe_tower_y"):
                val = ps_plate.get(wk)
                if isinstance(val, list) and len(val) > 1:
                    ps_plate[wk] = [val[pid - 1]] if pid - 1 < len(val) else [val[0]]

            instances = []
            for mi in pl.findall("model_instance"):
                oid_el = mi.find("metadata[@key='object_id']")
                if oid_el is None or not oid_el.get("value"):
                    continue
                oid = oid_el.get("value")
                imeta = sorted(
                    (m.get("key"), m.get("value"))
                    for m in mi.findall("metadata")
                    if m.get("key") and m.get("key") not in _IGNORE_INST_META
                )
                item_tr = _norm_transform(build_items.get(oid, _parse_transform(None)), ox, oy)
                comp_sig = []
                if oid in res_comps:
                    for cpath, cid, ctr in res_comps[oid]:
                        comp_sig.append([mesh_hash(cpath, cid), _norm_transform(ctr)])
                elif oid in inline_hashes:
                    comp_sig.append([inline_hashes[oid], _norm_transform(_parse_transform(None))])
                instances.append({
                    "imeta": imeta,
                    "item_tr": item_tr,
                    "obj": obj_settings.get(oid, {"meta": [], "parts": []}),
                    "ranges": layer_ranges.get(oid, []),
                    "comps": comp_sig,
                })
            # Canonical sort so instance serialization order doesn't defeat hits
            instances.sort(key=lambda x: json.dumps(x, sort_keys=True, separators=(",", ":")))

            payload = {
                "schema": SCHEMA_VERSION,
                "bambu_version": str(bambu_version),
                "cli_flags": flags,
                "project_settings": ps_plate,
                "plate_meta": pmeta,
                "filament_sequence": fseq.get(f"plate_{pid}") if isinstance(fseq, dict) else None,
                "custom_gcode_sha": custom_gcode_sha,
                "instances": instances,
            }
            blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            out[pid] = hashlib.sha256(blob).hexdigest()
    return out


def entry_dir(cache_root: Path | str, key: str) -> Path:
    return Path(cache_root) / f"v{SCHEMA_VERSION}" / key[:2] / key


def _tmp_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def get(cache_root: Path | str, key: str, touch: bool = True) -> dict | None:
    """Fetch a valid cached plate slice, or None on miss / corruption."""
    ed = entry_dir(cache_root, key)
    meta_path = ed / "meta.json"
    gcode_path = ed / "plate.gcode"
    if not (meta_path.is_file() and gcode_path.is_file()):
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return None
        if meta.get("schema") != SCHEMA_VERSION or meta.get("ok") is not True:
            return None
        if meta.get("key") != key:
            return None
        want_size = int(meta.get("gcode_bytes") or 0)
        if want_size <= 0 or gcode_path.stat().st_size != want_size:
            return None
    except (OSError, ValueError, TypeError):
        return None
    if touch:
        try:
            os.utime(meta_path, None)
        except OSError:
            pass
    return {
        "key": key,
        "gcode": gcode_path,
        "meta": meta,
        "warnings": list(meta.get("warnings") or []),
    }


def put(
    cache_root: Path | str,
    key: str,
    gcode_path: Path | str,
    *,
    bambu_version: str,
    cli_flags: dict | None = None,
    warnings: list[str] | None = None,
    source: str = "brickprint",
) -> Path | None:
    """Atomically store a valid sliced ``plate.gcode`` and ``meta.json``.

    Never caches missing or empty G-code files.  Returns the cached G-code
    path on success, or None if ``gcode_path`` is unusable.
    """
    src = Path(gcode_path)
    try:
        size = src.stat().st_size
    except OSError:
        return None
    if size <= 0:
        return None

    ed = entry_dir(cache_root, key)
    ed.mkdir(parents=True, exist_ok=True)
    dst_gcode = ed / "plate.gcode"
    dst_meta = ed / "meta.json"
    tmp_gcode = _tmp_path(dst_gcode)
    tmp_meta = _tmp_path(dst_meta)
    try:
        shutil.copyfile(src, tmp_gcode)
        os.replace(tmp_gcode, dst_gcode)
        meta = {
            "schema": SCHEMA_VERSION,
            "key": key,
            "bambu_version": str(bambu_version),
            "cli_flags": _canonical_cli_flags(cli_flags),
            "ok": True,
            "warnings": list(warnings or []),
            "gcode_bytes": size,
            "created_at": time.time(),
            "source": source,
        }
        tmp_meta.write_text(json.dumps(meta, indent=0, sort_keys=True), encoding="utf-8")
        os.replace(tmp_meta, dst_meta)
        return dst_gcode
    except OSError:
        tmp_gcode.unlink(missing_ok=True)
        tmp_meta.unlink(missing_ok=True)
        return None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def evict(
    cache_root: Path | str,
    max_gb: float | None = None,
    max_days: float | None = None,
    now: float | None = None,
) -> dict:
    """Apply TTL + LRU size cap to ``cache_root/v<SCHEMA_VERSION>``.

    Skips entries touched within ``ACTIVE_GUARD_S`` (60 s) so concurrent
    slices are never evicted mid-write/read.
    """
    res = {"removed": 0, "freed": 0}
    root = Path(cache_root) / f"v{SCHEMA_VERSION}"
    if not root.is_dir():
        return res
    now = time.time() if now is None else now
    days = _env_float(ENV_MAX_DAYS, DEFAULT_MAX_DAYS) if max_days is None else float(max_days)
    cap_gb = _env_float(ENV_MAX_GB, DEFAULT_MAX_GB) if max_gb is None else float(max_gb)
    cap_bytes = int(cap_gb * (1024 ** 3)) if cap_gb > 0 else 0

    entries: list[tuple[Path, int, float]] = []
    try:
        shards = list(os.scandir(root))
    except OSError:
        return res
    for shard in shards:
        if not shard.is_dir(follow_symlinks=False):
            continue
        try:
            items = list(os.scandir(shard.path))
        except OSError:
            continue
        for item in items:
            if not item.is_dir(follow_symlinks=False):
                continue
            p = Path(item.path)
            size = 0
            newest = 0.0
            try:
                with os.scandir(p) as it:
                    for f in it:
                        try:
                            st = f.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        size += st.st_size
                        newest = max(newest, st.st_mtime)
            except OSError:
                continue
            if newest == 0.0:
                try:
                    newest = p.stat().st_mtime
                except OSError:
                    continue
            entries.append((p, size, newest))

    gone: set[Path] = set()

    def _rm(p: Path, size: int) -> bool:
        shutil.rmtree(p, ignore_errors=True)
        if not p.exists():
            res["removed"] += 1
            res["freed"] += size
            gone.add(p)
            # Best-effort remove empty shard dir
            try:
                p.parent.rmdir()
            except OSError:
                pass
            return True
        return False

    if days > 0:
        cutoff = now - days * 86400.0
        for p, size, newest in entries:
            if now - newest < ACTIVE_GUARD_S:
                continue
            if newest < cutoff:
                _rm(p, size)

    if cap_bytes > 0:
        left = sorted((e for e in entries if e[0] not in gone), key=lambda e: e[2])
        total = sum(e[1] for e in left)
        for p, size, newest in left:
            if total <= cap_bytes:
                break
            if now - newest < ACTIVE_GUARD_S:
                continue
            if _rm(p, size):
                total -= size
    return res


def filter_plates_3mf(
    src: Path | str,
    dst: Path | str,
    keep_pids: list[int],
) -> dict[int, int]:
    """Write a minimal valid 3MF containing only ``keep_pids`` renumbered 1..K.

    Adjusts each kept plate's grid origin in ``3D/3dmodel.model`` (and
    ``<assemble>`` items), prunes unreferenced ``3D/Objects/*.model`` files and
    ``Metadata/layer_config_ranges.xml`` entries, and slices per-plate
    ``wipe_tower_x``/``wipe_tower_y`` lists when present.

    Returns ``{new_pid: orig_pid}``.
    """
    keep_ordered = list(dict.fromkeys(int(p) for p in keep_pids))
    if not keep_ordered:
        raise ValueError("filter_plates_3mf requires at least one plate id")

    # Preserve Bambu / 3MF namespaces cleanly when serializing 3dmodel.model
    ET.register_namespace("", _CORE_NS)
    ET.register_namespace("p", _PROD_NS)
    ET.register_namespace("BambuStudio", "http://schemas.bambulab.com/package/2021")

    with zipfile.ZipFile(src) as zin:
        names = zin.namelist()
        ps = json.loads(zin.read("Metadata/project_settings.config")) if "Metadata/project_settings.config" in names else {}
        bed, depth = _bed_dims(ps)

        ms_root = ET.fromstring(zin.read("Metadata/model_settings.config"))
        all_plates = ms_root.findall("plate")
        old_n = len(all_plates)
        plate_by_id: dict[int, ET.Element] = {}
        for pl in all_plates:
            pid_el = pl.find("metadata[@key='plater_id']")
            if pid_el is not None and pid_el.get("value"):
                plate_by_id[int(pid_el.get("value"))] = pl

        missing_ids = [pid for pid in keep_ordered if pid not in plate_by_id]
        if missing_ids:
            raise ValueError(f"plate id(s) not in {src}: {missing_ids}")

        new_n = len(keep_ordered)
        new_to_orig: dict[int, int] = {}
        kept_oids: set[str] = set()
        oid_delta: dict[str, tuple[float, float]] = {}

        # Remove dropped plates and renumber kept plates 1..K
        for pl in list(all_plates):
            ms_root.remove(pl)

        assemble_el = ms_root.find("assemble")
        insert_idx = list(ms_root).index(assemble_el) if assemble_el is not None else len(ms_root)

        for new_pid, orig_pid in enumerate(keep_ordered, 1):
            new_to_orig[new_pid] = orig_pid
            pl = plate_by_id[orig_pid]
            old_ox, old_oy = _plate_origin(orig_pid, old_n, bed, depth)
            new_ox, new_oy = _plate_origin(new_pid, new_n, bed, depth)
            dx, dy = new_ox - old_ox, new_oy - old_oy

            for m in list(pl.findall("metadata")):
                k = m.get("key")
                if k == "plater_id":
                    m.set("value", str(new_pid))
                elif k in ("thumbnail_file", "thumbnail_no_light_file", "top_file", "pick_file"):
                    pl.remove(m)
            for mi in pl.findall("model_instance"):
                oid_el = mi.find("metadata[@key='object_id']")
                if oid_el is not None and oid_el.get("value"):
                    oid = oid_el.get("value")
                    kept_oids.add(oid)
                    oid_delta[oid] = (dx, dy)
            ms_root.insert(insert_idx, pl)
            insert_idx += 1

        for obj in list(ms_root.findall("object")):
            if obj.get("id") not in kept_oids:
                ms_root.remove(obj)

        if assemble_el is not None:
            for ai in list(assemble_el.findall("assemble_item")):
                oid = ai.get("object_id")
                if oid not in kept_oids:
                    assemble_el.remove(ai)
                elif ai.get("instance_id") is not None and ai.get("transform"):
                    dx, dy = oid_delta.get(oid, (0.0, 0.0))
                    if dx or dy:
                        tr = _parse_transform(ai.get("transform"))
                        tr[9] += dx
                        tr[10] += dy
                        ai.set("transform", _fmt_transform(tr))

        # Filter 3D/3dmodel.model and shift kept build items to their new plate origin
        model_root = ET.fromstring(zin.read("3D/3dmodel.model"))
        resources = model_root.find("c:resources", _NS)
        kept_model_files: set[str] = set()
        if resources is not None:
            for obj in list(resources.findall("c:object", _NS)):
                if obj.get("id") not in kept_oids:
                    resources.remove(obj)
                else:
                    for comp in obj.findall(".//c:component", _NS):
                        cpath = (comp.get(_P_PATH) or "").replace("//", "/").lstrip("/")
                        if cpath:
                            kept_model_files.add(cpath)
        build = model_root.find("c:build", _NS)
        if build is not None:
            for it in list(build.findall("c:item", _NS)):
                oid = it.get("objectid")
                if oid not in kept_oids:
                    build.remove(it)
                else:
                    dx, dy = oid_delta.get(oid, (0.0, 0.0))
                    if dx or dy:
                        tr = _parse_transform(it.get("transform"))
                        tr[9] += dx
                        tr[10] += dy
                        it.set("transform", _fmt_transform(tr))

        # Slice per-plate wipe_tower_x / wipe_tower_y if present
        ps_out = dict(ps)
        for wk in ("wipe_tower_x", "wipe_tower_y"):
            val = ps_out.get(wk)
            if isinstance(val, list) and len(val) > 1:
                ps_out[wk] = [val[orig_pid - 1] if orig_pid - 1 < len(val) else val[0]
                              for orig_pid in keep_ordered]

        # Filter Metadata/layer_config_ranges.xml if present
        lcr_bytes: bytes | None = None
        if "Metadata/layer_config_ranges.xml" in names:
            lcr_root = ET.fromstring(zin.read("Metadata/layer_config_ranges.xml"))
            for obj in list(lcr_root.findall("object")):
                if obj.get("id") not in kept_oids:
                    lcr_root.remove(obj)
            lcr_bytes = b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(lcr_root, encoding="utf-8")

        # Filter Metadata/filament_sequence.json if present
        fseq_bytes: bytes | None = None
        if "Metadata/filament_sequence.json" in names:
            try:
                fseq = json.loads(zin.read("Metadata/filament_sequence.json"))
                if isinstance(fseq, dict):
                    fseq_new = {}
                    for new_pid, orig_pid in new_to_orig.items():
                        if f"plate_{orig_pid}" in fseq:
                            fseq_new[f"plate_{new_pid}"] = fseq[f"plate_{orig_pid}"]
                    fseq_bytes = json.dumps(fseq_new).encode("utf-8")
            except ValueError:
                fseq_bytes = None

        ms_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(ms_root, encoding="utf-8")
        model_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(model_root, encoding="utf-8")
        ps_bytes = json.dumps(ps_out, indent=1).encode("utf-8")

        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zout:
            for info in zin.infolist():
                fn = info.filename
                if fn.startswith("3D/Objects/") and fn.endswith(".model"):
                    if fn not in kept_model_files:
                        continue
                if fn.startswith("Metadata/") and (
                    fn.endswith(".png")
                    or (re.match(r"^Metadata/plate_\d+\.json$", fn) is not None)
                ):
                    continue
                if fn == "3D/3dmodel.model":
                    zout.writestr(fn, model_bytes)
                elif fn == "Metadata/model_settings.config":
                    zout.writestr(fn, ms_bytes)
                elif fn == "Metadata/project_settings.config":
                    zout.writestr(fn, ps_bytes)
                elif fn == "Metadata/layer_config_ranges.xml" and lcr_bytes is not None:
                    zout.writestr(fn, lcr_bytes)
                elif fn == "Metadata/filament_sequence.json" and fseq_bytes is not None:
                    zout.writestr(fn, fseq_bytes)
                else:
                    zout.writestr(fn, zin.read(fn))

    return new_to_orig
