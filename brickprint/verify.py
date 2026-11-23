"""Slice a project with the Bambu Studio CLI and check what will really print.

1. effective settings: every key listed in different_settings_to_system (plus
   the fit / support / bridge keys) must appear in the sliced G-code's config
   block with the project's value;
2. fit: per part instance, the outer-wall toolpaths at the stud layer
   (top - 0.8 mm) and the cavity layer (z = 1.0 mm) give stud diameter
   (min / max caliper / 0-90 deg mean), outer size, cavity / socket gap and tube
   OD; parts are matched between files by mesh signature;
3. supports: every Support path lies within ENFORCER_TOL mm of a
   support-enforcer volume (Bambu grows normal supports ~3.5 mm around the
   enforced overhang, measured on seagull v4) and nowhere inside a part body
   (first-layer footprint shrunk 0.1 mm: sockets, tubes, Technic holes);
4. floating: a supports-off copy is sliced; "floating regions / cantilever"
   on a plate without per-object supports is an error (Bambu hides these
   warnings when supports are enabled);
5. thumbnails: warns if the 3D models changed after Bambu normalisation.

``--reference`` compares settings (minus an allow-list) and fit numbers with
another project, e.g. the hand-built seagull v4.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import features, gcode
from .settings import norm

NS = {"c": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
P_NS = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"
SUPPORT_FEATS = ("Support", "Support interface", "Support transition")
STUD_RANGE = (4.3, 5.4)
CAVITY_Z = 1.0
STUD_DEPTH = 0.8
ENFORCER_TOL = 5.0
# TIE Fighter 75300 plates 4/7/8: tree branches reached 4.4-4.9 mm
# outside their enforcer boxes; 4.0 rejected harmless branch spread.
# The enclosure check below still catches support inside stud sockets.
# support of an object with its own enable_support=1 may spread this far
# beyond the object's bounding box (tree branches)
AUTO_SUPPORT_TOL = 10.0
# a support point is compared with part outlines printed at the same height
# (support layers share the object layer grid; 0.06 < one 0.08 mm layer, so
# a support standing on a part's top surface is not compared with that top)
ENCLOSED_LAYER_TOL = 0.06
# First-layer support is a base/brim on the bed; it can sit inside a part's
# footprint without blocking a socket above it.  Any intrusion that continues
# upward is still caught at the next layers.  TIE Fighter 75300 v3's 32828
# clips put 38-point support bases at z=0.20 only.
FIRST_LAYER_SKIP = 0.25

# keys that differ between brickprint and the GUI-built seagull v4 for
# bookkeeping reasons, plus v4's orange-slot leftovers from a PLA preset
# (decision: brickprint writes clean PETG Basic values for every slot).
ALLOW_BOOKKEEPING = {
    "different_settings_to_system", "inherits_group", "filament_settings_id",
    "filament_ids", "filament_colour", "print_settings_id", "printer_settings_id",
    "best_object_pos", "default_wipe_tower_pos", "wipe_tower_x", "wipe_tower_y",
    "extruder_ams_count", "extruder_nozzle_stats", "flush_volumes_matrix",
    "flush_volumes_vector", "filament_map", "filament_volume_map", "filament_nozzle_map",
    "filament_self_index", "default_filament_colour", "version", "filament_colour_type",
    "filament_multi_colour", "inherits", "upward_compatible_machine", "compatible_printers",
    # GUI-only bookkeeping keys that a GUI-saved project carries and the CLI
    # echoes; not used for the A1 mini toolpaths (printer host, multi-nozzle
    # mapping of H2-series printers, empty per-filament maps)
    "host_type", "printhost_authorization_type", "printhost_ssl_ignore_revoke",
    "extruder_nozzle_slot", "extruder_nozzle_stats_new", "multi_nozzle_diameter_mode",
    "printer_nozzle_diameter_mode", "filament_diameter_map",
    "filament_prime_tower_speed_factor_curve",
}
ALLOW_V4_ORANGE_PLA_LEFTOVERS = {
    "additional_cooling_fan_speed", "close_additional_fan_first_x_layers",
    "cool_plate_temp", "cool_plate_temp_initial_layer", "eng_plate_temp",
    "eng_plate_temp_initial_layer", "filament_adhesiveness_category",
    "filament_change_length", "filament_cost", "filament_density",
    "filament_dev_chamber_drying_bed_temperature", "filament_dev_drying_cooling_temperature",
    "filament_metal_stickiness", "filament_prime_volume", "filament_start_gcode",
    "filament_wipe", "filament_wipe_distance", "fins_extrude_safe_temp",
    "impact_strength_z", "supertack_plate_temp", "supertack_plate_temp_initial_layer",
    "temperature_vitrification", "close_optional_fan_first_x_layers", "optional_cooling_fan_speed",
    # length-only differences (v4 trimmed these AMS-drying lists to the slot count)
    "filament_dev_ams_drying_ams_limitations", "filament_dev_ams_drying_heat_distortion_temperature",
    "filament_dev_ams_drying_temperature", "filament_dev_ams_drying_time",
    "filament_dev_drying_softening_temperature", "filament_dev_chamber_drying_time",
}
ALWAYS_CHECK = ("xy_contour_compensation", "xy_hole_compensation", "layer_height", "wall_loops",
                "enable_support", "support_type", "bridge_angle", "thick_bridges",
                "filament_shrink", "nozzle_temperature")


# ---------------------------------------------------------------------------
# 3MF reading
# ---------------------------------------------------------------------------

def _tr(s: str) -> np.ndarray:
    v = [float(x) for x in s.split()]
    return np.array(v).reshape(4, 3)


def _apply(M: np.ndarray, V: np.ndarray) -> np.ndarray:
    return V @ M[:3, :3] + M[3]


def _compose(inner: np.ndarray, outer: np.ndarray) -> np.ndarray:
    R = inner[:3, :3] @ outer[:3, :3]
    t = inner[3] @ outer[:3, :3] + outer[3]
    return np.vstack([R, t])


def read_project(path: Path) -> dict:
    z = zipfile.ZipFile(path)
    names = z.namelist()
    meshes = {}
    for name in names:
        if not (name.startswith("3D/Objects/") and name.endswith(".model")):
            continue
        root = ET.fromstring(z.read(name))
        for obj in root.findall(".//c:object", NS):
            vs = obj.findall(".//c:vertex", NS)
            if not vs:
                continue
            V = np.array([[float(v.get("x")), float(v.get("y")), float(v.get("z"))] for v in vs])
            T = np.array([[int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))]
                          for t in obj.findall(".//c:triangle", NS)])
            meshes[("/" + name, obj.get("id"))] = (V, T)
    root = ET.fromstring(z.read("3D/3dmodel.model"))
    comps, items = {}, {}
    for obj in root.findall("c:resources/c:object", NS):
        cl = []
        for c in obj.findall(".//c:component", NS):
            cl.append(((c.get(P_NS + "path") or "").replace("//", "/"), c.get("objectid"),
                       _tr(c.get("transform") or "1 0 0 0 1 0 0 0 1 0 0 0")))
        comps[obj.get("id")] = cl
    for it in root.findall("c:build/c:item", NS):
        items[it.get("objectid")] = _tr(it.get("transform") or "1 0 0 0 1 0 0 0 1 0 0 0")
    ms = ET.fromstring(z.read("Metadata/model_settings.config"))
    objmeta, subtypes, pnames = {}, {}, {}
    for o in ms.findall("object"):
        objmeta[o.get("id")] = {m.get("key"): m.get("value") for m in o.findall("metadata") if m.get("key")}
        subtypes[o.get("id")] = [p.get("subtype", "normal_part") for p in o.findall("part")]
        pnames[o.get("id")] = [(p.find("metadata[@key='name']").get("value")
                                if p.find("metadata[@key='name']") is not None else "")
                               for p in o.findall("part")]
    plates = []
    for pl in ms.findall("plate"):
        pid = int(pl.find("metadata[@key='plater_id']").get("value"))
        pname = pl.find("metadata[@key='plater_name']").get("value")
        objs = []
        for mi in pl.findall("model_instance"):
            oid = mi.find("metadata[@key='object_id']").get("value")
            ident = int(mi.find("metadata[@key='identify_id']").get("value"))
            st = subtypes.get(oid) or []
            nm = pnames.get(oid) or []
            parts = []
            for k, (mpath, mid, M) in enumerate(comps.get(oid, [])):
                sub = st[k] if k < len(st) else "normal_part"
                W = _compose(M, items.get(oid, _tr("1 0 0 0 1 0 0 0 1 0 0 0")))
                parts.append({"mesh": (mpath, mid), "M": W, "subtype": sub,
                              "rib": (nm[k] if k < len(nm) else "").endswith(" ceiling rib")})
            objs.append({"oid": oid, "ident": ident, "meta": objmeta.get(oid, {}), "parts": parts})
        plates.append({"id": pid, "name": pname, "objects": objs})
    settings = json.loads(z.read("Metadata/project_settings.config"))
    models_sha = hashlib.sha1(b"".join(z.read(n) for n in sorted(names) if n.startswith("3D/"))).hexdigest()
    return {"path": str(path), "settings": settings, "meshes": meshes, "plates": plates,
            "thumbnails": any(n.startswith("Metadata/plate_") and n.endswith(".png") for n in names),
            "models_sha": models_sha}


def signature(V: np.ndarray, T: np.ndarray) -> str:
    ext = V.max(axis=0) - V.min(axis=0)
    return f"v{len(V)}t{len(T)}:" + "x".join(f"{e:.2f}" for e in ext)


def models_sha(path: Path) -> str:
    z = zipfile.ZipFile(path)
    return hashlib.sha1(b"".join(z.read(n) for n in sorted(z.namelist()) if n.startswith("3D/"))).hexdigest()


# ---------------------------------------------------------------------------
# slicing
# ---------------------------------------------------------------------------

def slice_project(bambu: str, src: Path, outdir: Path, plate: int = 0, timeout: int = 3600) -> tuple[int, str]:
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [bambu, "--slice", str(plate), "--arrange", "0", "--orient", "0", "--debug", "2",
           "--outputdir", str(outdir.resolve()), str(Path(src).resolve())]
    log = outdir / "log.txt"
    try:
        with open(log, "wb") as lf:
            rc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        rc = -1
    return rc, log.read_text(errors="replace")


def _slice_error(text: str, rc: int, log_path: Path) -> str:
    why = []
    for ln in text.splitlines():
        low = ln.lower()
        if ("[error]" in low and ("conflict" in low or "unprintable" in low or "empty layer" in low)) \
                or "error_string" in low:
            why.append(ln.split("] ", 2)[-1].strip())
    res = log_path.with_name("result.json")
    if res.is_file():
        try:
            es = json.loads(res.read_text()).get("error_string")
            if es:
                why.append(es)
        except ValueError:
            pass
    return f"Bambu exited {rc}" + (": " + "; ".join(dict.fromkeys(why))[:400] if why else "") \
        + f" (see {log_path})"


def slice_plates(bambu: str, src: Path, outdir: Path, ids: list[int], log=print,
                 timeout: int = 3600) -> tuple[dict, str, dict]:
    """Slice ``ids`` of a project.  The Bambu CLI stops at the first plate it
    cannot slice (path conflict, G-code outside the bed, empty layer), so
    every plate left without G-code is then sliced on its own.  Returns
    ({pid: gcode path}, combined log text, {pid: error} for plates that do not
    slice even alone)."""
    rc, text = slice_project(bambu, src, outdir, ids[0] if len(ids) == 1 else 0, timeout)
    g = {pid: outdir / f"plate_{pid}.gcode" for pid in ids}
    failed: dict[int, str] = {}
    if rc == 0:
        return g, text, failed
    # the plate that stopped the run may still have left (unusable) G-code
    for m in re.finditer(r"plate (\d+): found (?:slicing result conflict|gcode unprintable)", text):
        if int(m.group(1)) in g:
            failed[int(m.group(1))] = _slice_error(text, rc, outdir / "log.txt")
    missing = [pid for pid in ids if not g[pid].is_file() and pid not in failed]
    if len(ids) == 1 and not failed:
        failed[ids[0]] = _slice_error(text, rc, outdir / "log.txt")
    for pid in failed:
        g[pid] = outdir / f"plate_{pid}.unusable"   # never analysed
    if len(ids) == 1 or not missing:
        return g, text, failed
    log(f"verify: Bambu stopped (exit {rc}); slicing the {len(missing)} plate(s) without G-code "
        f"one by one ...")
    texts = [text]
    for pid in missing:
        d = outdir / f"plate{pid}_alone"
        rc1, t1 = slice_project(bambu, src, d, pid, timeout)
        texts.append(t1)
        if (d / f"plate_{pid}.gcode").is_file() and rc1 == 0:
            g[pid] = d / f"plate_{pid}.gcode"
        else:
            failed[pid] = _slice_error(t1, rc1, d / "log.txt")
    return g, "\n".join(texts), failed


def supports_off_copy(src: Path, dst: Path) -> None:
    """Global supports off, and no support-enforcer volumes: with support
    disabled Bambu reports an enforced overhang as floating although the real
    file supports exactly that overhang (60470b alone: 5/5 runs flagged with
    its enforcer, 0/5 without).  Per-object enable_support stays."""
    zin = zipfile.ZipFile(src)
    ms = zin.read("Metadata/model_settings.config").decode()
    enf = set(re.findall(r'<part id="(\d+)" subtype="support_enforcer"', ms))
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "Metadata/project_settings.config":
                ps = json.loads(data)
                ps["enable_support"] = "0"
                data = json.dumps(ps, indent=1).encode()
            elif enf and info.filename == "Metadata/model_settings.config":
                data = re.sub(r'\s*<part id="\d+" subtype="support_enforcer">.*?</part>', "",
                              ms, flags=re.S).encode()
            elif enf and info.filename == "3D/3dmodel.model":
                data = re.sub(r'\s*<component [^>]*objectid="(\d+)"[^>]*/>',
                              lambda m: "" if m.group(1) in enf else m.group(0),
                              data.decode()).encode()
            z.writestr(info, data)


def same_slice_input(a: Path, b: Path) -> bool:
    """True when two 3MFs give Bambu the very same input: the same entries
    with identical bytes, except ``Metadata/project_settings.config``, which
    is compared as parsed JSON (``supports_off_copy`` re-serialises it).  A
    supports-off copy of a file with global supports already off and no
    enforcer volumes is such a copy: slicing it again is just another run of
    the same project."""
    try:
        with zipfile.ZipFile(a) as za, zipfile.ZipFile(b) as zb:
            if sorted(za.namelist()) != sorted(zb.namelist()):
                return False
            for n in za.namelist():
                x, y = za.read(n), zb.read(n)
                if x == y:
                    continue
                if n == "Metadata/project_settings.config" and json.loads(x) == json.loads(y):
                    continue
                return False
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return False
    return True


# ---------------------------------------------------------------------------
# measurements
# ---------------------------------------------------------------------------

def _depths(boxes: list[tuple]) -> list[int]:
    """Nesting depth of each loop bbox (0 = outermost)."""
    out = []
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        d = 0
        for j, (a0, b0, a1, b1) in enumerate(boxes):
            if i != j and a0 <= x0 + 1e-6 and b0 <= y0 + 1e-6 and a1 >= x1 - 1e-6 and b1 >= y1 - 1e-6 \
                    and (a1 - a0) * (b1 - b0) > (x1 - x0) * (y1 - y0) + 1e-6:
                d += 1
        out.append(d)
    return out


def _nearest_layer(zs: list[float], target: float) -> float | None:
    if not zs:
        return None
    return min(zs, key=lambda z: abs(z - target))


def measure_part(loops: list[dict], height: float, has_studs: bool) -> dict:
    """loops: outer-wall closed loops of one part instance (G-code coords)."""
    zs = sorted({round(l["z"], 4) for l in loops})
    r: dict = {}
    if has_studs:
        zs_top = _nearest_layer(zs, height - STUD_DEPTH)
        studs = []
        for l in loops:
            if abs(l["z"] - zs_top) > 1e-3:
                continue
            P = l["pts"]
            cal = gcode.calipers(P) + l["lw"]
            if not (STUD_RANGE[0] <= cal.max() <= STUD_RANGE[1]):
                continue
            if gcode.circle_fit(P)[3] > 0.06:
                continue
            studs.append((cal.min(), cal.max(), (cal[0] + cal[180]) / 2))
        if studs:
            r["stud_n"] = len(studs)
            r["stud_d_min"] = min(s[0] for s in studs)
            r["stud_d_max"] = max(s[1] for s in studs)
            r["stud_d_axis"] = float(np.mean([s[2] for s in studs]))
    zc = _nearest_layer([z for z in zs if z >= 0.3], CAVITY_Z)
    if zc is not None:
        L = [l for l in loops if abs(l["z"] - zc) < 1e-3]
        boxes = [(*l["pts"].min(0), *l["pts"].max(0)) for l in L]
        dep = _depths(boxes)
        outer = [l for l, d in zip(L, dep) if d == 0]
        def size(l):
            # round loops: mean caliper (an axis-aligned bbox of a tessellated
            # circle depends on where the arc points fall)
            if gcode.circle_fit(l["pts"])[3] < 0.03:
                m = float(gcode.calipers(l["pts"]).mean())
                return m, m
            w, h = np.ptp(l["pts"], 0)
            return float(w), float(h)
        if outer:
            o = max(outer, key=lambda l: np.ptp(l["pts"], 0).prod())
            w, h = size(o)
            r["outer_x"], r["outer_y"] = w + o["lw"], h + o["lw"]
        cav = [l for l, d in zip(L, dep) if d == 1]
        if cav:
            c = max(cav, key=lambda l: np.ptp(l["pts"], 0).prod())
            w, h = size(c)
            # a stud socket is a rectangular (or round) hollow; the irregular
            # hollow of a wedge plate is reported separately (its bbox varies
            # ~0.02 mm even between mirrored instances in one slice)
            Q = c["pts"]
            area = 0.5 * abs(float(np.dot(Q[:, 0], np.roll(Q[:, 1], -1)) - np.dot(Q[:, 1], np.roll(Q[:, 0], -1))))
            rect = area >= 0.97 * float(np.ptp(Q, 0).prod()) or gcode.circle_fit(Q)[3] < 0.03
            key = "cavity" if rect else "cavity_irregular"
            r[key + "_x"], r[key + "_y"] = w - c["lw"], h - c["lw"]
        tubes = [l for l, d in zip(L, dep) if d == 2 and gcode.circle_fit(l["pts"])[3] < 0.06]
        if tubes:
            cal = gcode.calipers(tubes[0]["pts"]) + tubes[0]["lw"]
            r["tube_od_min"], r["tube_od_max"] = float(cal.min()), float(cal.max())
        r["cavity_z"] = zc
    return r


def _instances(proj: dict, plate: dict) -> list[dict]:
    """Part instances of a plate with world mesh, signature, body hull."""
    out = []
    for obj in plate["objects"]:
        for p in obj["parts"]:
            V, T = proj["meshes"][p["mesh"]]
            W = _apply(p["M"], V)
            out.append({"obj": obj, "subtype": p["subtype"], "W": W, "T": T, "rib": p.get("rib", False),
                        "sig": signature(V, T) if p["subtype"] == "normal_part" else None})
    return out


def _point_in_poly(pts: np.ndarray, poly: np.ndarray, chunk: int = 4000) -> np.ndarray:
    """Even-odd ray casting, vectorised (poly: closed loop, last point may
    repeat the first)."""
    x0, y0 = poly[:, 0][None, :], poly[:, 1][None, :]
    x1, y1 = np.roll(poly[:, 0], -1)[None, :], np.roll(poly[:, 1], -1)[None, :]
    dy = np.where(np.abs(y1 - y0) < 1e-12, 1e-12, y1 - y0)
    out = np.zeros(len(pts), bool)
    for s in range(0, len(pts), chunk):
        x, y = pts[s:s + chunk, 0][:, None], pts[s:s + chunk, 1][:, None]
        cross = ((y0 > y) != (y1 > y)) & (x < x0 + (y - y0) * (x1 - x0) / dy)
        out[s:s + chunk] = (cross.sum(1) % 2) == 1
    return out


def enclosed_support(P: list[dict], layer_tol: float = ENCLOSED_LAYER_TOL) -> list[dict]:
    """Support toolpath points that lie inside a part's outer contour on the
    same layer, i.e. enclosed by the part's own walls: a filled stud socket,
    tube, Technic hole or other cavity.  Taken from the G-code alone (no
    mesh, no object labels): per layer, the closed "Outer wall" loops that are
    not nested in another loop are part outlines.  Support next to or under a
    part (side-stud cantilevers, between the studs of an upside-down brick) is
    outside every outline and passes.

    Returns [{"z", "n", "bbox"}] per (layer, outline) with enclosed points."""
    sup = [p for p in P if p["feat"] in SUPPORT_FEATS]
    if not sup:
        return []
    loops_by_z = defaultdict(list)
    for p in P:
        if p["feat"] == "Outer wall" and gcode.is_closed(p["pts"]):
            loops_by_z[round(p["z"], 3)].append(p["pts"])
    zs = np.array(sorted(loops_by_z))
    if not len(zs):
        return []
    outlines: dict[float, list] = {}

    def outer_loops(z):
        if z not in outlines:
            L = loops_by_z[z]
            B = np.array([[*l.min(0), *l.max(0)] for l in L])
            area = (B[:, 2] - B[:, 0]) * (B[:, 3] - B[:, 1])
            cont = ((B[:, None, 0] <= B[None, :, 0] + 1e-6) & (B[:, None, 1] <= B[None, :, 1] + 1e-6)
                    & (B[:, None, 2] >= B[None, :, 2] - 1e-6) & (B[:, None, 3] >= B[None, :, 3] - 1e-6)
                    & (area[:, None] > area[None, :] + 1e-6))      # cont[i, j]: i contains j
            outlines[z] = [(L[j], B[j]) for j in range(len(L)) if not cont[:, j].any()]
        return outlines[z]

    hits: dict[tuple, dict] = {}
    by_z = defaultdict(list)
    for p in sup:
        by_z[round(p["z"], 3)].append(p["pts"])
    for zsup, pl in by_z.items():
        if zsup <= FIRST_LAYER_SKIP:
            continue
        pts = np.vstack(pl)
        near = zs[np.abs(zs - zsup) <= layer_tol + 1e-9]
        for zl in near:
            for loop, b in outer_loops(float(zl)):
                sel = ((pts[:, 0] > b[0]) & (pts[:, 0] < b[2]) & (pts[:, 1] > b[1]) & (pts[:, 1] < b[3]))
                if not sel.any():
                    continue
                n = int(_point_in_poly(pts[sel], loop).sum())
                if n:
                    key = (zsup, tuple(np.round(b, 1)))
                    h = hits.setdefault(key, {"z": zsup, "n": 0, "bbox": [round(float(v), 2) for v in b]})
                    h["n"] = max(h["n"], n)
    return sorted(hits.values(), key=lambda h: (h["bbox"], h["z"]))


def analyse_plate(proj: dict, plate: dict, gpath: Path) -> dict:
    P = gcode.paths(gpath)
    insts = _instances(proj, plate)
    normal = [i for i in insts if i["subtype"] == "normal_part" and not i["rib"]]
    enf = [i for i in insts if i["subtype"] == "support_enforcer"]
    # G-code offset: one per plate (outer-wall bbox vs world bbox of all its
    # parts).  Slicing uses --arrange 0, so every object keeps its place; the
    # G-code object labels cannot be used for this: Bambu omits them on a
    # single-object plate and on plates with more than 64 objects, and its
    # label ids differ from the 3MF identify_ids after a Bambu re-export
    # (CLI-verified on the Countach v8 build, plates 4 / 17: 91 / 77 objects).
    walls = [p for p in P if p["feat"] == "Outer wall"]
    objs = plate["objects"]
    off = np.zeros(2)
    if walls and normal:
        wpts = np.vstack([p["pts"] for p in walls])
        lw = walls[0]["lw"] or 0.42
        off = wpts.min(0) - lw / 2 - np.vstack([i["W"] for i in normal]).min(0)[:2]
    for i in insts:
        i["g_min"] = i["W"].min(0)[:2] + off
        i["g_max"] = i["W"].max(0)[:2] + off
    # assign closed outer-wall loops to part instances
    per = defaultdict(list)
    for p in walls:
        if not gcode.is_closed(p["pts"]):
            continue
        lo, hi = p["pts"].min(0), p["pts"].max(0)
        for k, i in enumerate(normal):
            if (lo >= i["g_min"] - 0.6).all() and (hi <= i["g_max"] + 0.6).all():
                per[k].append(p)
                break
    parts = []
    for k, i in enumerate(normal):
        Wl = i["W"] - i["W"].min(0)
        has_studs = bool(features.stud_rings(Wl, i["T"]))
        m = measure_part(per.get(k, []), float(np.ptp(i["W"][:, 2])), has_studs)
        parts.append({"sig": i["sig"], "object": i["obj"]["meta"].get("name", i["obj"]["oid"]),
                      "at": [round(float(x), 1) for x in i["g_min"]], **m})
    # supports
    sup_pts = [p["pts"] for p in P if p["feat"] in SUPPORT_FEATS]
    auto = [i for i in normal if i["obj"]["meta"].get("enable_support") == "1"]
    sup_objs_auto = [o for o in objs if o["meta"].get("enable_support") == "1"]
    support = {"paths": len(sup_pts), "outside_enforcers": 0, "auto_object_points": 0,
               "inside_bodies": 0, "enforcers": len(enf), "per_object_supports": len(sup_objs_auto)}
    if sup_pts:
        pts = np.vstack(sup_pts)
        # 1. location: without per-object supports, Bambu (normal(manual))
        #    may only support inside enforcer volumes.  Objects with their
        #    own enable_support=1 (supports plates, flagged objects) support
        #    wherever their overhangs need it; their support is exempt from
        #    the enforcer rule, never from the enclosure rule below.
        ok = np.zeros(len(pts), bool)
        for e in enf:
            ok |= ((pts >= e["g_min"] - ENFORCER_TOL) & (pts <= e["g_max"] + ENFORCER_TOL)).all(1)
        auto_ok = np.zeros(len(pts), bool)
        for i in auto:
            auto_ok |= ((pts >= i["g_min"] - AUTO_SUPPORT_TOL) & (pts <= i["g_max"] + AUTO_SUPPORT_TOL)).all(1)
        support["auto_object_points"] = int((auto_ok & ~ok).sum())
        support["outside_enforcers"] = int((~(ok | auto_ok)).sum())
        # 2. the thing that ruins a LEGO part: support enclosed by the part's
        #    own walls (stud socket, tube, hole), from the G-code alone
        enc = enclosed_support(P)
        for h in enc:
            c = np.array([(h["bbox"][0] + h["bbox"][2]) / 2, (h["bbox"][1] + h["bbox"][3]) / 2])
            owner = next((i for i in normal if (c >= i["g_min"] - 0.6).all() and (c <= i["g_max"] + 0.6).all()),
                         None)
            h["part"] = owner["obj"]["meta"].get("name", owner["obj"]["oid"]) if owner else "?"
            h["sig"] = owner["sig"] if owner else None
        support["inside_bodies"] = sum(h["n"] for h in enc)
        support["enclosed"] = enc[:200]
    # bridge directions per signature
    bridges = defaultdict(lambda: defaultdict(float))
    for p in P:
        if p["feat"] != "Bridge":
            continue
        lo = p["pts"].min(0)
        for k, i in enumerate(normal):
            if (lo >= i["g_min"] - 0.6).all() and (p["pts"].max(0) <= i["g_max"] + 0.6).all():
                d = np.diff(p["pts"], axis=0)
                L = np.hypot(d[:, 0], d[:, 1])
                ang = np.round(np.degrees(np.arctan2(d[:, 1], d[:, 0])) % 180).astype(int) % 180
                for a, l in zip(ang, L):
                    if l > 1:
                        bridges[i["sig"]][int(a)] += float(l)
                break
    return {"parts": parts, "support": support,
            "bridges": {s: {a: round(l, 1) for a, l in sorted(v.items()) if l > 5}
                        for s, v in bridges.items()}}


def aggregate(parts: list[dict]) -> dict:
    """signature -> metric -> mean over instances (+ instance spread)."""
    by = defaultdict(list)
    for p in parts:
        by[p["sig"]].append(p)
    out = {}
    for sig, ps in by.items():
        keys = sorted({k for p in ps for k in p if isinstance(p.get(k), float) and k != "cavity_z"})
        out[sig] = {"n": len(ps)}
        for k in keys:
            vals = [p[k] for p in ps if k in p]
            out[sig][k] = round(float(np.mean(vals)), 4)
            out[sig][k + "_spread"] = round(float(np.ptp(vals)), 4)
    return out


# ---------------------------------------------------------------------------
# settings checks
# ---------------------------------------------------------------------------

def _gvals(v: str) -> list:
    return [norm(x) for x in gcode.split_value(v)]


def _pvals(v) -> list:
    if isinstance(v, list):
        return [norm(x) for x in v]
    return [norm(v)]


def _same(a: list, b: list) -> bool:
    if a == b:
        return True
    if len(set(a)) == 1 and len(set(b)) == 1 and a[0] == b[0]:
        return True
    return False


def check_effective(settings: dict, cfgblock: dict) -> list[str]:
    keys = set(ALWAYS_CHECK)
    for entry in settings.get("different_settings_to_system", []):
        keys |= {k for k in str(entry).split(";") if k}
    errs = []
    for k in sorted(keys):
        if k not in settings:
            continue
        if k not in cfgblock:
            errs.append(f"{k}: not in the G-code config block")
            continue
        if not _same(_pvals(settings[k]), _gvals(cfgblock[k])):
            errs.append(f"{k}: project {settings[k]!r} but G-code {cfgblock[k][:80]!r}")
    return errs


def check_expected(expected: dict, cfgblock: dict) -> list[str]:
    """Intended values from outside the file (profile / build report): a
    GUI re-save that reset tuned keys makes the file self-consistent, so only
    an external expectation catches it."""
    errs = []
    for k, v in sorted(expected.items()):
        if k not in cfgblock:
            errs.append(f"{k}: expected {v!r}, not in the G-code config block")
        elif not _same(_pvals(v), _gvals(cfgblock[k])):
            errs.append(f"{k}: expected {v!r} but G-code has {cfgblock[k][:60]!r}")
    return errs


def compare_config(a: dict, b: dict, allow: set) -> tuple[list[str], list[str]]:
    errs, allowed = [], []
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k), b.get(k)
        if va is None or vb is None:
            (allowed if k in allow else errs).append(f"{k}: only in {'reference' if va is None else 'new'}")
            continue
        if not _same(_gvals(va), _gvals(vb)):
            (allowed if k in allow else errs).append(f"{k}: new {va[:60]!r} ref {vb[:60]!r}")
    return errs, allowed


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def _sibling_report(path: Path) -> dict | None:
    """The build's ``<stem>_report.json`` next to ``path``, or None when it is
    missing or unreadable."""
    rep = Path(path).with_name(Path(path).stem + "_report.json")
    if not rep.is_file():
        return None
    try:
        data = json.loads(rep.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def build_is_legacy(path: Path) -> bool | None:
    """Was ``path`` built with the untouched default profile ("legacy")?
    Read from the sibling build report: only profile builds get a
    ``settings`` block (with ``legacy_default`` False).  None when there is no
    readable report, so the caller cannot know and stays strict."""
    data = _sibling_report(path)
    if data is None:
        return None
    s = data.get("settings")
    if not s:
        return True
    return bool(s.get("legacy_default")) if isinstance(s, dict) else None


LEGACY_ENCLOSURE_NOTE = ("default-profile build: it writes no socket support blockers, so "
                         "supports can fill sockets; build with --profile (or any tuning "
                         "option) to get blockers")


def enclosure_issue(pid: int, sup: dict, legacy: bool | None) -> tuple[str, str] | None:
    """("error" | "warning", message) for support enclosed by a part's walls on
    plate ``pid``, or None.  Profile builds put support blockers over every
    enclosed cavity, so a hit there is an error; default-profile builds write
    no blockers (their output is kept unchanged), so there it is a warning."""
    if not sup.get("inside_bodies"):
        return None
    who = defaultdict(lambda: [0, 99.0, 0.0])
    for h in sup.get("enclosed", []):
        w = who[h["part"]]
        w[0] += h["n"]
        w[1], w[2] = min(w[1], h["z"]), max(w[2], h["z"])
    msg = (f"plate {pid}: {sup['inside_bodies']} support points enclosed by a part's "
           f"walls (socket / cavity): " + "; ".join(
               f"{k} {v[0]} pts z {v[1]:.2f}-{v[2]:.2f}"
               for k, v in sorted(who.items(), key=lambda kv: -kv[1][0])[:6]))
    if legacy:
        return "warning", f"{msg} ({LEGACY_ENCLOSURE_NOTE})"
    return "error", msg


def run(path: Path, bambu: str, plates: list[int] | None = None, reference: Path | None = None,
        tol: float = 0.01, workdir: Path | None = None, float_check: bool = True,
        expected: dict | None = None, log=print, legacy: bool | None = None,
        concurrent: bool = True) -> dict:
    """``legacy``: the build used the default profile (no socket blockers);
    None reads it from the sibling ``_report.json`` (no report: strict).

    Floating check: one supports-off Bambu run.  When the supports-off copy
    is the same slicer input as the file (global supports already off, no
    enforcers: default-profile builds), that run is the file's own slice and
    no second slice is made (the old code sliced the same project twice and
    read the floating warnings of one run only, so the verdict is drawn the
    same way).  Otherwise the copy is sliced while the file's plates are
    sliced and analysed (``concurrent``; False: one after the other)."""
    path = Path(path)
    if legacy is None:
        legacy = build_is_legacy(path)
    # G-code is large: default to a temp dir (kept; its path is in the report)
    work = Path(workdir or tempfile.mkdtemp(prefix="brickprint_verify_"))
    work.mkdir(parents=True, exist_ok=True)
    proj = read_project(path)
    ids = plates or [p["id"] for p in proj["plates"]]
    unknown = sorted(set(ids) - {p["id"] for p in proj["plates"]})
    if unknown:
        raise SystemExit(f"verify: no plate(s) {unknown} in {path.name}")
    errors, warnings = [], []
    result = {"file": str(path), "workdir": str(work), "plates": {}}

    def sliced(proj_path: Path, tag: str) -> tuple[dict, str, dict]:
        g, text, failed = slice_plates(bambu, proj_path, work / tag, ids, log=log)
        return g, text, failed

    off, off_same, fut_off, pool = None, False, None, None
    if float_check:
        off = work / "supports_off.3mf"
        supports_off_copy(path, off)
        off_same = same_slice_input(path, off)
        if off_same:
            off.unlink(missing_ok=True)
        elif concurrent:
            pool = ThreadPoolExecutor(max_workers=1)
            fut_off = pool.submit(sliced, off, "supports_off")
        result["float_check"] = {
            "slice": "new" if off_same else "supports_off",
            "note": ("supports-off copy identical to the file (global supports off, no "
                     "enforcers): floating warnings read from the file's own slice")
            if off_same else "supports-off copy sliced" + (" concurrently" if concurrent else "")}
    try:
        return _run_checks(path, bambu, proj, ids, reference, tol, work, float_check, expected,
                           log, legacy, result, errors, warnings, sliced, off, off_same, fut_off)
    finally:
        if pool is not None:
            pool.shutdown(wait=True)


def _run_checks(path, bambu, proj, ids, reference, tol, work, float_check, expected, log, legacy,
                result, errors, warnings, sliced, off, off_same, fut_off) -> dict:
    if float_check and off_same:
        log(f"verify: slicing {path.name} plates {ids} (its supports-off copy is the same "
            f"project: one slice serves both checks) ...")
    elif fut_off is not None:
        log(f"verify: slicing {path.name} plates {ids} and its supports-off copy together ...")
    else:
        log(f"verify: slicing {path.name} plates {ids} ...")
    gfiles, text, failed = sliced(path, "new")
    for pid, why in sorted(failed.items()):
        errors.append(f"plate {pid}: Bambu could not slice it: {why}")
    for pid in ids:
        g = gfiles[pid]
        if not g.is_file():
            if pid not in failed:
                errors.append(f"plate {pid}: no G-code")
            continue
        cfgb = gcode.config_block(g)
        eff = check_effective(proj["settings"], cfgb)
        errors += [f"plate {pid}: effective setting mismatch: {e}" for e in eff]
        if expected:
            errors += [f"plate {pid}: intended setting not effective: {e}"
                       for e in check_expected(expected, cfgb)]
        plate = next(p for p in proj["plates"] if p["id"] == pid)
        an = analyse_plate(proj, plate, g)
        sup = an["support"]
        if sup["outside_enforcers"]:
            errors.append(f"plate {pid}: {sup['outside_enforcers']} support points outside enforcer boxes")
        enc = enclosure_issue(pid, sup, legacy)
        if enc:
            (warnings if enc[0] == "warning" else errors).append(enc[1])
        result["plates"][pid] = {"name": plate["name"], "config": cfgb, **an,
                                 "fit": aggregate(an["parts"])}
    if float_check:
        if off_same:
            text_off = text
        elif fut_off is not None:
            _, text_off, _ = fut_off.result()
        else:
            _, text_off, _ = sliced(off, "supports_off")
        for m in re.finditer(r"plate (\d+): found (?:NON_CRITICAL )?slicing warnings: (.*)", text_off):
            pid, msg = int(m.group(1)), m.group(2)
            if pid not in ids or not ("floating" in msg or "empty layer" in msg):
                continue
            plate = next(p for p in proj["plates"] if p["id"] == pid)
            named = re.search(r"It seems object (.+?) has floating", msg)
            supp = [o for o in plate["objects"] if o["meta"].get("enable_support") == "1"]
            if named:   # Bambu names the object: only its own supports count
                supp = [o for o in supp if o["meta"].get("name") == named.group(1)]
            if supp:
                warnings.append(f"plate {pid}: {msg[:100]} (object has per-object supports)")
            else:
                errors.append(f"plate {pid}: floating with supports off and no per-object "
                              f"supports: {msg[:120]}")
    # thumbnails
    rep = _sibling_report(path)
    if proj["thumbnails"] and rep is not None:
        want = rep.get("normalised_models_sha")
        if want and want != proj["models_sha"]:
            warnings.append("3D models changed after Bambu normalisation: plate thumbnails are stale")
    elif not proj["thumbnails"]:
        warnings.append("no plate thumbnails (raw project; build with --bambu to add them)")

    if reference:
        ref = read_project(Path(reference))
        rids = [p["id"] for p in ref["plates"]]
        log(f"verify: slicing reference {Path(reference).name} ...")
        rout = work / "reference"
        rc2, _ = slice_project(bambu, Path(reference), rout, 0 if len(rids) > 1 else rids[0])
        ref_fit, ref_cfg = {}, None
        for pid in rids:
            g = rout / f"plate_{pid}.gcode"
            if not g.is_file():
                errors.append(f"reference plate {pid}: no G-code")
                continue
            plate = next(p for p in ref["plates"] if p["id"] == pid)
            an = analyse_plate(ref, plate, g)
            for sig, m in aggregate(an["parts"]).items():
                ref_fit.setdefault(sig, []).append(m)
            if ref_cfg is None:
                ref_cfg = gcode.config_block(g)
        new_fit = {}
        for pid, pr in result["plates"].items():
            for sig, m in pr["fit"].items():
                new_fit.setdefault(sig, []).append(m)
        cmp_rows = []
        for sig in sorted(set(new_fit) & set(ref_fit)):
            for k in sorted({k for m in new_fit[sig] + ref_fit[sig] for k in m
                             if not k.endswith("_spread") and k != "n"}):
                a = [m[k] for m in new_fit[sig] if k in m]
                b = [m[k] for m in ref_fit[sig] if k in m]
                if not a or not b:
                    continue
                d = abs(float(np.mean(a)) - float(np.mean(b)))
                cmp_rows.append({"sig": sig, "metric": k, "new": round(float(np.mean(a)), 4),
                                 "ref": round(float(np.mean(b)), 4), "diff": round(d, 4)})
                if d > tol:
                    msg = f"fit {sig} {k}: new {np.mean(a):.3f} ref {np.mean(b):.3f} (diff {d:.3f} > {tol})"
                    # stud / socket / tube toolpaths are the clutch-relevant
                    # numbers; outer sizes (incl. round bases such as the 59900
                    # cone, sensitive to arc fitting) are reported, not gated
                    (warnings if k.startswith(("outer_", "cavity_irregular")) else errors).append(msg)
        missing = sorted(set(ref_fit) - set(new_fit))
        if missing:
            warnings.append(f"reference parts not in the new project: {missing}")
        first = (result["plates"].get(ids[0]) or {}).get("config", {}) if ids else {}
        cerr, callowed = compare_config(first, ref_cfg or {},
                                        ALLOW_BOOKKEEPING | ALLOW_V4_ORANGE_PLA_LEFTOVERS)
        errors += [f"config differs from reference: {e}" for e in cerr]
        result["reference"] = {"file": str(reference), "fit_compare": cmp_rows,
                               "config_allowed_diffs": callowed,
                               "matched_signatures": len(set(new_fit) & set(ref_fit))}
    for pid in result["plates"]:
        result["plates"][pid].pop("config", None)
    result["errors"], result["warnings"] = errors, warnings
    result["ok"] = not errors
    return result


def print_summary(res: dict) -> None:
    for pid, pr in res["plates"].items():
        s = pr["support"]
        print(f"plate {pid} {pr['name']}: {len(pr['parts'])} parts; support paths {s['paths']} "
              f"(enforcers {s['enforcers']}, outside {s['outside_enforcers']}, "
              f"per-object supports {s.get('per_object_supports', 0)}, enclosed {s['inside_bodies']})")
        cal = sorted((q for q in pr["parts"] if re.match(r"fit [+-]\d", str(q.get("object", "")))),
                     key=lambda q: float(str(q["object"]).split()[1]))   # stable: layout order within a row
        if cal:   # calibration plate: one line per row/part (rows share a part signature)
            for q in cal:
                bits = []
                if "stud_d_axis" in q:
                    bits.append(f"stud D {q['stud_d_axis']:.3f}")
                if "cavity_x" in q:
                    bits.append(f"socket {q['cavity_x']:.3f}x{q['cavity_y']:.3f}")
                print(f"   {q['object']:22s} " + "; ".join(bits))
            continue
        for sig, m in sorted(pr["fit"].items()):
            bits = []
            if "stud_d_axis" in m:
                bits.append(f"stud {m['stud_d_min']:.3f}-{m['stud_d_max']:.3f} (axis {m['stud_d_axis']:.3f})")
            if "cavity_x" in m:
                bits.append(f"socket {m['cavity_x']:.3f}x{m['cavity_y']:.3f}")
            if "cavity_irregular_x" in m:
                bits.append(f"hollow {m['cavity_irregular_x']:.3f}x{m['cavity_irregular_y']:.3f}")
            if "outer_x" in m:
                bits.append(f"outer {m['outer_x']:.3f}x{m['outer_y']:.3f}")
            if "tube_od_min" in m:
                bits.append(f"tube {m['tube_od_min']:.3f}-{m['tube_od_max']:.3f}")
            print(f"   {sig:34s} n={m['n']} " + "; ".join(bits))
    if "reference" in res:
        r = res["reference"]
        gated = lambda m: not m.startswith(("outer_", "cavity_irregular"))
        worst = max((row["diff"] for row in r["fit_compare"] if gated(row["metric"])), default=0.0)
        worst_o = max((row["diff"] for row in r["fit_compare"] if not gated(row["metric"])), default=0.0)
        print(f"reference {Path(r['file']).name}: {r['matched_signatures']} part types matched, "
              f"{len(r['fit_compare'])} metrics, max stud/socket/tube diff {worst:.4f} mm "
              f"(outer sizes / irregular hollows {worst_o:.4f}); "
              f"{len(r['config_allowed_diffs'])} allow-listed config differences")
    for w in res["warnings"]:
        print("WARNING: " + w)
    for e in res["errors"]:
        print("ERROR: " + e)
    print("verify: " + ("PASS" if res["ok"] else f"FAIL ({len(res['errors'])} errors)"))
