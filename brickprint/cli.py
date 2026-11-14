"""brickprint command line: set number -> colour-grouped multi-plate 3MF."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import ceilings, config, features, ldraw, pack, probe, quality, rebrickable, settings, sweep, threemf

# plate check: re-check rounds after per-object supports were added
PLATE_CHECK_ROUNDS = 4

UNVERIFIED_BANNER = """\
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!! WARNING: FLOATING DETECTION IS UNVERIFIED ({why}).
!! Orientation and support plates come from the numpy slicer emulator
!! only, which misses dozens of floating / cantilever poses per set.
!! Supports-off plates may print parts in mid-air.  Install Bambu Studio
!! (or set "bambu" in ~/.config/brickprint/config.json) and rebuild
!! without --no-probe before printing.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"""


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return value[:80] or "set"


def _load_orient(path: str | None) -> dict:
    data = dict(ldraw.ORIENT)
    if path:
        extra = json.loads(Path(path).read_text())
        if not isinstance(extra, dict):
            raise SystemExit(f"--orient: {path} must be a JSON object of part -> rotation")
        # case-insensitive labels; an unknown label is an error, not identity
        # a trailing "!" forces the pose even when it floats (support plate)
        data.update({str(k).lower(): ldraw.rot_label(v, str(k))
                     + ("!" if str(v).strip().endswith("!") else "")
                     for k, v in extra.items()})
    return data


def _map_lots(lots, library: ldraw.LDrawLibrary, relationships, overrides):
    mapped = []
    missing = []
    for lot in lots:
        res = library.resolve(lot["part_num"], relationships, overrides)
        if not res["path"]:
            missing.append({**lot, "status": res["status"], "note": res["note"]})
            continue
        mapped.append({
            **lot,
            "file": res["file"],
            "ldraw_part": res["part"],
            "status": res["status"],
            "note": res["note"],
        })
    return mapped, missing


def _merge_lots(mapped):
    merged = defaultdict(lambda: None)
    for lot in mapped:
        key = (lot["file"], lot["color_id"])
        if merged[key] is None:
            merged[key] = {
                "part_file": lot["file"],
                "color_id": lot["color_id"],
                "quantity": 0,
                "part_name": lot["part_name"],
                "color_name": lot["color_name"],
                "rgb": lot["rgb"],
                "is_trans": lot["is_trans"],
            }
        merged[key]["quantity"] += lot["quantity"]
    return list(merged.values())


def _bottom_contact(V: np.ndarray, T: np.ndarray) -> float:
    """Approximate bed-contact area from triangles within 0.05 mm of z=0."""
    tri = V[T].astype(np.float64)
    zmin = float(V[:, 2].min())
    mask = np.all(tri[:, :, 2] <= zmin + 0.05, axis=1)
    if not mask.any():
        return 0.0
    t = tri[mask]
    cross = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    return float(0.5 * np.linalg.norm(cross, axis=1).sum())


def _orientation_warnings(meshes: dict) -> list[dict]:
    warnings = []
    for fn, mesh in sorted(meshes.items()):
        area = _bottom_contact(mesh["V"], mesh["T"])
        height = float(mesh["size"][2])
        if area < 20.0 and height > 12.0 and mesh.get("rot", "id") == "id":
            warnings.append({
                "part_file": fn,
                "bottom_contact_mm2": round(area, 1),
                "height_mm": round(height, 2),
            })
    return warnings


def _write_plates_csv(path: Path, plates, meshes, color_ids):
    fields = [
        "plate", "name", "colour", "color_id", "extruder", "filament_hex",
        "parts", "size_x_mm", "size_y_mm", "max_z_mm", "supports",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for pid, plate in enumerate(plates, 1):
            max_z = max((float(meshes[it[0]]["size"][2]) for it in plate["items"]), default=0.0)
            cs = pack.plate_colors(plate)
            if plate.get("groups"):
                # mixed plate: one ';'-separated entry per colour, in the same order
                colour = "; ".join(f"{c['color_name']} ({len(c['items'])})" for c in cs)
            else:
                colour = plate["color_name"]
            w.writerow({
                "plate": pid,
                "name": pack.plate_name(plate),
                "colour": colour,
                "color_id": ";".join(c["color_id"] for c in cs),
                "extruder": ";".join(str(color_ids.index(c["color_id"]) + 1) for c in cs),
                "filament_hex": ";".join("#" + c["rgb"].lstrip("#") for c in cs),
                "parts": len(plate["items"]),
                "size_x_mm": round(plate["bbox"][0], 2),
                "size_y_mm": round(plate["bbox"][1], 2),
                "max_z_mm": round(max_z, 2),
                "supports": plate.get("support", ""),
            })


def _normalise_with_bambu(cfg, raw_path: Path, out_path: Path) -> bool:
    bambu = cfg.get("bambu", "")
    if not bambu or not Path(bambu).is_file():
        print("Bambu Studio not found; leaving the raw 3MF (valid, no thumbnails).")
        return False
    # The CLI export works while the Bambu Studio GUI is open (verified with
    # 02.09.01.70).  The output directory must exist and be absolute.
    outdir = (Path(cfg["cache_dir"]) / "bambu" / uuid.uuid4().hex).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        bambu, str(Path(raw_path).resolve()),
        "--outputdir", str(outdir),
        "--export-3mf", out_path.name,
    ]
    print("Bambu: " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        print("Bambu export timed out; leaving the raw 3MF.")
        return False
    produced = outdir / out_path.name
    if proc.returncode != 0 or not produced.is_file():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        print("Bambu export failed; leaving the raw 3MF.")
        for line in tail:
            print("  " + line)
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(produced, out_path)
    return True


def cmd_doctor(args):
    cfg = config.load_config()
    return config.doctor(cfg)


def cmd_bootstrap(args):
    cfg = config.load_config()
    config.ensure_dirs(cfg)
    print("fetching Rebrickable CSV dumps ...")
    rebrickable.ensure_data(config.rebrickable_dir(cfg), refresh=args.refresh)
    ldraw_dir = Path(cfg["ldraw_dir"]) if cfg["ldraw_dir"] else None
    if ldraw_dir and (ldraw_dir / "LDConfig.ldr").is_file():
        print(f"LDraw library: {ldraw_dir}")
        return 0
    target = config.ldraw_cache_dir(cfg)
    target.mkdir(parents=True, exist_ok=True)
    print("LDraw library not configured; downloading complete.zip (~140 MB) ...")
    from .net import download
    url = "https://library.ldraw.org/library/updates/complete.zip"
    zip_path = target / "complete.zip"
    if not zip_path.is_file():
        print("downloading LDraw complete.zip ...")
        download(url, zip_path, timeout=600)
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(target)
    root = target / "ldraw"
    if not (root / "LDConfig.ldr").is_file():
        # Some archives extract directly into the target.
        root = target
    cfg["ldraw_dir"] = str(root)
    config_file = Path(os.environ.get(config.CONFIG_ENV, config.DEFAULT_CONFIG_PATH)).expanduser()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if config_file.is_file():
        existing = json.loads(config_file.read_text())
    existing["ldraw_dir"] = str(root)
    config_file.write_text(json.dumps(existing, indent=1))
    print(f"LDraw library ready: {root}")
    print(f"Config updated: {config_file}")
    return 0


def cmd_inventory(args):
    cfg = config.load_config()
    config.ensure_dirs(cfg)
    rb = config.rebrickable_dir(cfg)
    rebrickable.ensure_data(rb, refresh=args.refresh)
    info = rebrickable.lookup_set(
        rb, args.set, include_spares=args.include_spares,
        include_minifigs=args.include_minifigs,
    )
    out = Path(args.out) if args.out else Path(f"{info['set_num']}_{slugify(info['name'])}_bom.csv")
    rebrickable.write_bom_csv(out, info["lots"])
    total = sum(l["quantity"] for l in info["lots"])
    colors = sorted({l["color_id"] for l in info["lots"]})
    print(f"{info['set_num']}  {info['name']}  ({info['year']})")
    print(f"lots {len(info['lots'])}  parts {total}  colours {len(colors)}  minifigs {len(info['minifigs'])}")
    print(f"BOM: {out}")
    if info["minifigs"] and not args.include_minifigs:
        print("note: minifigs are listed but not expanded; add --include-minifigs")
    if cfg["ldraw_dir"]:
        library = ldraw.LDrawLibrary(cfg["ldraw_dir"])
        mapped, missing = _map_lots(info["lots"], library, info["relationships"], {})
        print(f"LDraw mapped lots {len(mapped)}  missing {len(missing)}")
        for m in missing[:20]:
            print(f"  MISSING {m['part_num']} x{m['quantity']} ({m['part_name']})")
    return 0


def _apply_options(cfg, args) -> None:
    """Profile / tuning options -> cfg (read by settings.Composer)."""
    for key in ("profile", "fit", "supports", "bridging", "ceilings"):
        val = getattr(args, key, None)
        if val not in (None, ""):
            cfg[key] = val
    cfg["filaments"] = list(getattr(args, "filament", None) or [])
    cfg["set"] = list(getattr(args, "set_kv", None) or [])
    cfg["allow_shrink"] = bool(getattr(args, "allow_shrink", False))
    cfg["_composer"] = None


def _prepare_meshes(cfg, unique_files, overrides, use_probe: bool, cache=None):
    """Repaired meshes -> Bambu floating probe -> print orientation.

    Returns (meshes, probe_report, bambu)."""
    cache = cache or ldraw.MeshCache(cfg)
    meshes = {}
    print(f"meshes: {len(unique_files)} unique LDraw files ...")
    # 1. repaired mesh (raw LDView cache untouched) + the 6 candidate poses;
    #    cache misses (LDView export, repair, emulator) run in parallel
    #    across parts (config mesh_workers), cached parts load serially
    items = [(pf, ldraw.start_rotation(pf, overrides)[0]) for pf in unique_files]
    prepared = {}
    for part_file, (V, T, cands) in cache.prepare(
            items, ldraw.default_mesh_workers(cfg)).items():
        prepared[part_file] = {
            "V": V, "T": T, "mesh_key": ldraw.mesh_key(V, T), "cands": cands,
        }

    # 2. Bambu Studio decides which poses float (cached per mesh + pose +
    #    verdict-relevant settings); always sliced with supports OFF
    verdicts = {}
    probe_report = {"used": False}
    bambu = probe.find_bambu(cfg)
    if not use_probe:
        print(UNVERIFIED_BANNER.format(why="--no-probe"))
        probe_report["skipped"] = "--no-probe"
    elif not bambu:
        print(UNVERIFIED_BANNER.format(why="Bambu Studio not found"))
        probe_report["skipped"] = "Bambu Studio not found"
    else:
        verdicts, pstats = probe.probe(
            cfg, prepared, probe.VerdictCache(cache.root), bambu)
        probe_report = {"used": True, **pstats}
        print(f"probe: {pstats['parts_verified']}/{pstats['parts']} parts verified by "
              f"Bambu Studio ({pstats['cached']} poses cached, {pstats['probed']} probed, "
              f"{pstats['bambu_runs']} Bambu runs) in {pstats['seconds']}s")
        unverified = sorted(set(prepared) - set(verdicts))
        if unverified:
            print(f"WARNING: {len(unverified)} part(s) could not be probed; emulator "
                  f"only (floating unverified): {', '.join(unverified[:30])}")
            probe_report["unverified_parts"] = unverified

    # 3. print orientation: ORIENT/--orient entries are hard overrides unless
    #    they float; Bambu verdicts outrank the emulator
    for part_file in unique_files:
        p = prepared[part_file]
        V, T = p["V"], p["T"]
        ori = cache.orientation(part_file, V, T, overrides,
                                verdicts=verdicts.get(part_file), cands=p["cands"])
        V = ldraw.apply_rotation(V, ori["R"])
        meshes[part_file] = {
            "V": V, "T": T, "rot": ori["rot"],
            "size": (V.max(axis=0) - V.min(axis=0)),
            "needs_support": ori["needs_support"],
        }
        if ori["changed"]:
            print(f"  orient {part_file}: {ori['old_rot']} -> {ori['rot']} ({ori['reason']})")
    return meshes, probe_report, bambu


def _object_pad(comp, m: dict) -> float:
    """Keep-out (mm per side) around a part that is its own Bambu object and
    carries a support enforcer: its support may not run into a neighbouring
    object (Bambu rejects the plate with "gcode path conflicts")."""
    if comp.ceilings_mode == "off" or m.get("enforcer") is None:
        return 0.0
    EV = np.asarray(m["enforcer"][0], dtype=float)
    V = np.asarray(m["V"], dtype=float)
    over = max(float((V[:, :2].min(axis=0) - EV[:, :2].min(axis=0)).max()),
               float((EV[:, :2].max(axis=0) - V[:, :2].max(axis=0)).max()), 0.0)
    return round(over + 3.0, 2)


def _apply_features(cfg, meshes) -> dict:
    """Enforcer volumes / per-part bridge angles from the profile (none for
    the default profile)."""
    comp = threemf.composer(cfg)
    prof = comp.profile
    out = {"supports": comp.supports_mode, "bridging": comp.bridging_mode,
           "ceilings": comp.ceilings_mode, "enforcers": {}, "bridge_angles": {},
           "ceiling_parts": {}}
    sp = prof.get("supports") or {}
    br = prof.get("bridging") or {}
    for pf, m in sorted(meshes.items()):
        if comp.supports_mode == "enforcers":
            boxes = features.cantilever_boxes(
                m["V"], m["T"], protrusion=float(sp.get("protrusion_mm", 0.8)),
                margin=float(sp.get("margin_mm", 0.2)), below=float(sp.get("below_mm", 0.3)),
                above=float(sp.get("above_mm", 0.6)))
            if boxes:
                m["enforcer"] = features.box_mesh(boxes)
                out["enforcers"][pf] = boxes
        if comp.bridging_mode == "short-span":
            ang = features.bridge_angle(m["V"], m["T"], int(br.get("x_long_angle", 90)))
            if ang:
                m["part_meta"] = {"bridge_angle": ang}
                out["bridge_angles"][pf] = ang
        if comp.ceilings_mode == "recipe8":
            # after the bridge angle: ribs run perpendicular to the strands
            ang = float((m.get("part_meta") or {}).get("bridge_angle")
                        or comp.process_overrides().get("bridge_angle", 0) or 0)
            p = ceilings.plan(m["V"], m["T"], ang)
            m["V"] = np.asarray(p["V"], dtype=float)
            if p["bands"]:
                m["bands"] = p["bands"]
            if p["ribs"]:
                m["rib"] = (np.asarray(p["ribs"][0], dtype=float), np.asarray(p["ribs"][1], dtype=int))
            out["ceiling_parts"][pf] = {
                "raise": p["raise"].get("status"), "reason": p["raise"].get("reason"),
                "bands": [[a, b, o] for a, b, o in p["bands"]],
                "ribs": len(p["report"]["ribs"]),
                "ceilings": [{k: c.get(k) for k in ("z", "decision", "band_decision", "rib_decision",
                                                    "spans_before", "spans_after")}
                             for c in p["report"]["ceilings"]],
            }
    if out["enforcers"]:
        print("support enforcers (cantilevered features outside the part body): "
              + ", ".join(f"{k} x{len(v)}" for k, v in out["enforcers"].items()))
    # per-object settings (need one object per part, i.e. the ceilings recipe)
    out["object_meta"] = {}
    if comp.ceilings_mode != "off":
        brim = prof.get("brim") or {}
        pfit = prof.get("part_settings") or {}
        for pf, m in sorted(meshes.items()):
            meta = {}
            if brim.get("mode") == "small-contact":
                V = np.asarray(m["V"], dtype=float)
                T = np.asarray(m["T"], dtype=int)
                tri = V[T]
                flat = np.all(np.abs(tri[:, :, 2] - V[:, 2].min()) < 1e-3, axis=1)
                e1, e2 = tri[flat, 1] - tri[flat, 0], tri[flat, 2] - tri[flat, 0]
                area = float(np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]).sum() / 2)
                if area < float(brim.get("contact_mm2", 20)):
                    meta.update({"brim_type": "outer_only",
                                 "brim_width": f"{float(brim.get('width_mm', 3)):g}",
                                 "brim_object_gap": f"{float(brim.get('gap_mm', 0.2)):g}"})
            for k, v in (pfit.get(Path(pf).stem) or {}).items():
                meta[k] = str(v)
            if meta:
                m["object_meta"] = meta
                out["object_meta"][pf] = meta
        if out["object_meta"]:
            print("per-object settings: " + "; ".join(
                f"{k} {', '.join(f'{a}={b}' for a, b in v.items())}" for k, v in out["object_meta"].items()))
    if out["ceiling_parts"]:
        cp = out["ceiling_parts"]
        print("socket ceilings (recipe 8): raised "
              + (", ".join(k for k, v in cp.items() if v["raise"] == "raise") or "none")
              + "; ribs " + (", ".join(k for k, v in cp.items() if v["ribs"]) or "none")
              + "; height-range bands on " + str(sum(1 for v in cp.values() if v["bands"])) + " part type(s)")
        for k, v in cp.items():
            if v["raise"] == "refused":
                print(f"  ceiling not raised on {k}: {v['reason']}")
    if out["bridge_angles"]:
        print("per-part bridge angle (X-long cavity ceilings): "
              + ", ".join(f"{k}={v}" for k, v in out["bridge_angles"].items()))
    return out


def _check_ribs(cfg, bambu, meshes, feats, vcache) -> None:
    """The probe judged the bare mesh; a ceiling rib is new geometry that can
    float on its own (70490: a rib 0.24 mm under an 11.2 mm dome ceiling,
    flagged "floating cantilever" in 5 of 5 runs, 0 of 5 without the rib).
    Slice every rib part as written; drop the ribs of a part that floats with
    them but is clean without them in every run."""
    ribbed = {pf: {**m, "file": pf} for pf, m in meshes.items() if m.get("rib") is not None}
    if not ribbed:
        return
    res = probe.check_final(cfg, bambu, ribbed, vcache, runs=probe.CONFIRM_RUNS)
    suspects = [pf for pf, v in res.items() if probe.bad(v)]
    if not suspects:
        return
    bare = {pf: {k: v for k, v in ribbed[pf].items() if k != "rib"} for pf in suspects}
    res2 = probe.check_final(cfg, bambu, bare, vcache, runs=probe.CONFIRM_RUNS)
    for pf in suspects:
        cp = feats["ceiling_parts"].setdefault(pf, {})
        if not probe.bad(res2[pf]):
            meshes[pf].pop("rib", None)
            cp["ribs_dropped"] = f"floats with ribs ({res[pf]['msg'][:80]}), clean without"
            cp["ribs"] = 0
            print(f"  ceiling ribs dropped on {pf}: Bambu flags the part with them "
                  f"({res[pf]['msg'][:60]}...) and not without")
        else:
            cp["ribs_check"] = "floats with and without ribs (plate check decides)"


def _prime_tower_on(comp) -> bool:
    """Does the profile print a prime tower on multi-filament plates?"""
    S = comp.compose([{"color_id": "0", "color_name": "a", "hex": "FFFFFF"},
                      {"color_id": "1", "color_name": "b", "hex": "000000"}])
    return str(S.get("enable_prime_tower", "0")) == "1"


def _screen_final(cfg, bambu, meshes, feats, vcache) -> None:
    """Every part type as the build writes it (chosen pose, raised ceilings,
    ribs, bands, per-part settings; supports off, no enforcers), sliced alone
    in CONFIRM_RUNS runs (cached).  A type Bambu flags in any run goes to the
    support plates.  The probe's 3 clean runs were not enough for a few
    marginal poses: in the Countach, 15068 up=-X, 63868 up=+X and 3172 up=-Y
    were flagged in about 2 of 3 later runs, alone as well as packed."""
    todo = {pf: {**m, "file": pf} for pf, m in meshes.items() if not m["needs_support"]}
    if not todo:
        return
    res = probe.check_final(cfg, bambu, todo, vcache, runs=probe.CONFIRM_RUNS)
    flagged = sorted(pf for pf, v in res.items() if probe.bad(v))
    feats["final_check"] = {pf: res[pf]["msg"][:120] for pf in flagged}
    for pf in flagged:
        meshes[pf]["needs_support"] = True
    if flagged:
        print(f"final-geometry check: {len(flagged)} part type(s) float as built "
              f"-> support plates: {', '.join(flagged)}")


def _check_profile(cfg, colors: dict) -> None:
    """Compose once with the real colours so profile errors stop the build
    before the slow steps; report --filament specs that match no colour."""
    comp = threemf.composer(cfg)
    slots = [{"color_id": cid, "color_name": c["name"], "hex": c["rgb"]} for cid, c in colors.items()]
    comp.compose(slots)
    unused = comp.unused_specs()
    if unused:
        raise SystemExit(f"--filament: {', '.join(unused)} match no colour of this build "
                         f"(colours: {', '.join(c['name'] for c in colors.values())})")


def _resolve_parts(library, parts: list[str], relationships, part_map) -> list[str]:
    files = []
    for p in parts:
        res = library.resolve(p, relationships, part_map)
        if not res["path"]:
            raise SystemExit(f"test part {p}: no LDraw file ({res['note']})")
        files.append(res["file"])
    return files


def _test_objects(meshes, files, gap, width, metadata_for=None, label=""):
    """Separate objects for test pieces, packed into one bin."""
    b = pack.Bin(width, width)
    objs = []
    for pf in sorted(files, key=lambda f: -float(meshes[f]["size"][0] * meshes[f]["size"][1])):
        w, d = float(meshes[pf]["size"][0]), float(meshes[pf]["size"][1])
        pos = b.insert(w + gap, d + gap, pf)
        if pos is None:
            return None
        meta = metadata_for(pf) if metadata_for else {}
        objs.append({"name": f"{label}{Path(pf).stem}", "items": [(pf, pos[0], pos[1], w, d)],
                     "metadata": meta})
    return objs


def _auto_test_parts(merged, feats, skip=()) -> list[str]:
    """Default --fit-test pieces; ``skip``: part types that need supports (a
    fit-test piece is its own object without supports)."""
    files = [m["part_file"] for m in merged if m["part_file"] not in skip]
    pick = []
    for want in ("3022.dat", "3003.dat", "3020.dat", "3005.dat", "3024.dat"):
        if want in files and len(pick) < 2:
            pick.append(want)
    pick += [f for f in feats.get("enforcers", {}) if f not in pick and f in files][:1]
    pick += [f for f in files if f not in pick and f in ("6541.dat", "3700.dat")][:1]
    return pick or files[:3]


def cmd_build(args):
    t0 = time.time()
    cfg = config.load_config()
    for key, attr in (("bed", "bed"), ("depth", "depth"), ("margin", "margin"), ("gap", "gap")):
        if getattr(args, key, None) is not None:
            cfg[key] = float(getattr(args, key))
    config.ensure_dirs(cfg)
    sweep.maybe_sweep(cfg)
    _apply_options(cfg, args)
    comp = threemf.composer(cfg)

    overrides = _load_orient(args.orient)
    part_map = ldraw.load_part_map(getattr(args, "part_map", None))

    if args.bom:
        lots = ldraw.read_bom(Path(args.bom))
        set_info = {"set_num": args.set or "custom", "name": args.name or Path(args.bom).stem}
        relationships = {}
        rb_dir = config.rebrickable_dir(cfg)
        if (rb_dir / "part_relationships.csv.gz").is_file():
            relationships = rebrickable.load_relationships(rb_dir)
        if (rb_dir / "colors.csv.gz").is_file():
            known = rebrickable.load_colors(rb_dir)
            for lot in lots:
                color = known.get(lot["color_id"])
                if color and (not lot["color_name"] or lot["color_name"].startswith("Color ")):
                    lot["color_name"] = color["name"]
                    lot["rgb"] = color["rgb"]
                    lot["is_trans"] = color["is_trans"]
    else:
        rb = config.rebrickable_dir(cfg)
        rebrickable.ensure_data(rb, refresh=args.refresh)
        info = rebrickable.lookup_set(
            rb, args.set, include_spares=args.include_spares,
            include_minifigs=args.include_minifigs,
        )
        lots = info["lots"]
        relationships = info["relationships"]
        set_info = {"set_num": info["set_num"], "name": info["name"], "year": info["year"]}
        if args.name:
            set_info["name"] = args.name

    if not cfg["ldraw_dir"] or not Path(cfg["ldraw_dir"]).is_dir():
        raise SystemExit("LDraw library not found; run brickprint doctor or brickprint bootstrap")
    library = ldraw.LDrawLibrary(cfg["ldraw_dir"])
    mapped, missing = _map_lots(lots, library, relationships, part_map)
    if args.strict and missing:
        for m in missing:
            print(f"MISSING {m['part_num']} x{m['quantity']} {m['note']}")
        raise SystemExit(f"{len(missing)} lot(s) have no LDraw geometry (--strict)")
    merged = _merge_lots(mapped)

    colors = {}
    for lot in merged:
        colors.setdefault(lot["color_id"], {
            "name": lot["color_name"], "rgb": lot["rgb"], "is_trans": lot["is_trans"],
        })
    if not comp.legacy:
        _check_profile(cfg, colors)
        print(f"profile: {comp.name}; fit contour {comp.fit[0]:+.3f} / hole {comp.fit[1]:+.3f} mm; "
              f"supports {comp.supports_mode}; bridging {comp.bridging_mode}")

    test_files = []
    if getattr(args, "fit_test", None):
        if args.fit_test != "auto":
            test_files = _resolve_parts(library, [p.strip() for p in args.fit_test.split(",") if p.strip()],
                                        relationships, part_map)

    cache = ldraw.MeshCache(cfg)
    unique_files = sorted({m["part_file"] for m in merged} | set(test_files))
    meshes, probe_report, bambu = _prepare_meshes(cfg, unique_files, overrides, args.probe, cache)
    feats = _apply_features(cfg, meshes) if not comp.legacy else {}
    if feats and probe_report.get("used"):
        vcache = probe.VerdictCache(cache.root)
        _check_ribs(cfg, bambu, meshes, feats, vcache)
        _screen_final(cfg, bambu, meshes, feats, vcache)
    needs_support = sorted(f for f, m in meshes.items() if m["needs_support"])
    if needs_support:
        print(f"needs supports ({len(needs_support)}): {', '.join(needs_support)}")

    orientation_warnings = _orientation_warnings(meshes)
    if orientation_warnings:
        print("orientation advisories (small bottom contact, tall part):")
        for warning in orientation_warnings[:20]:
            print(
                f"  {warning['part_file']}: {warning['bottom_contact_mm2']} mm² "
                f"contact, {warning['height_mm']} mm tall"
            )
        print("  add a manual Rx/Ry override with --orient if the part should lie flat")

    tower = pack.TOWER_KEEPOUT if _prime_tower_on(comp) else None

    def layout():
        instances = [
            {
                "part_file": m["part_file"],
                "color_id": m["color_id"],
                "size": meshes[m["part_file"]]["size"],
                "count": m["quantity"],
                "needs_support": meshes[m["part_file"]]["needs_support"],
                "pad": _object_pad(comp, meshes[m["part_file"]]),
            }
            for m in merged
        ]
        # a --fit-test plate is appended after packing: keep a grid slot free for it
        plates, oversized, _ = pack.pack(
            instances, colors,
            bed=cfg["bed"], depth=cfg["depth"],
            margin=cfg["margin"], gap=cfg["gap"],
            max_plates=pack.MAX_PLATES - (1 if getattr(args, "fit_test", None) else 0),
            mix=getattr(args, "mix_colours", None) or "auto",
            tower=tower,
        )
        if oversized:
            print(f"WARNING: {len(oversized)} lot(s) do not fit on the {cfg['bed']:.0f} mm bed")
            for o in oversized[:20]:
                print(f"  OVERSIZED {o['part_file']} {o['size'][0]:.1f} x {o['size'][1]:.1f} mm")
        if not plates:
            raise SystemExit("nothing fits on the bed")
        if getattr(args, "fit_test", None):
            first = plates[0]
            objs = _test_objects(meshes, test_files, max(cfg["gap"], 6.0),
                                 min(cfg["bed"], cfg["depth"]) - 2 * cfg["margin"], label="fit test ",
                                 metadata_for=lambda pf: (dict(threemf.AUTO_SUPPORT_META)
                                                          if meshes[pf]["needs_support"] else {}))
            if objs is None:
                raise SystemExit("--fit-test pieces do not fit on one plate")
            for o in objs:   # a piece that needs support: its own tree supports, sockets blocked
                o["auto_support"] = bool(o["metadata"])
            if len(plates) >= 36:
                raise SystemExit("--fit-test: the project already has 36 plates")
            plates.append({
                "color_id": first["color_id"], "color_name": "Fit test", "rgb": first["rgb"],
                "is_trans": first["is_trans"], "index": 1, "count": 1, "kind": "",
                "support": "", "items": [it for o in objs for it in o["items"]], "objects": objs,
                "bbox": (0, 0),
            })
        return plates, oversized

    if getattr(args, "fit_test", None) and not test_files:
        test_files = _auto_test_parts(merged, feats,
                                      skip={f for f, m in meshes.items() if m["needs_support"]})
    plates, oversized = layout()
    if getattr(args, "fit_test", None):
        print(f"fit-test plate: {', '.join(test_files)} (separate objects, "
              f"fit {comp.fit[0]:+.3f}/{comp.fit[1]:+.3f}, slot 1)")

    out_path = Path(args.out) if args.out else Path(
        f"{set_info['set_num']}_{slugify(set_info['name'])}_parts_by_color.3mf"
    )
    raw_path = out_path.with_name(out_path.stem + "_raw.3mf")
    threemf.write_project(cfg, set_info, plates, meshes, raw_path)

    # 4. whole-project check: parts that are clean alone can still be flagged
    #    once packed (and Bambu's check is not deterministic).  A floating
    #    warning on a supports-off plate is a bug.  A one-object plate (default
    #    profile) gets the support-plate overrides as a whole.  On plates with
    #    one object per part (or per colour) Bambu names every floating object
    #    (one warning line each); their part types are moved to the support
    #    plates (tree supports, sockets blocked: one object per plate, so no
    #    path conflicts) and the project is re-packed and re-checked.
    #    With global supports on (enforcer profile) Bambu hides floating
    #    warnings, so a supports-off verdict copy is sliced instead.
    plate_check = {"run": False}
    if probe_report.get("used"):
        check_path = raw_path
        if not comp.legacy:
            check_path = raw_path.with_name(out_path.stem + "_verdict.3mf")
            threemf.write_project(cfg, set_info, plates, meshes, check_path, verdict=True)
        per_part = comp.ceilings_mode != "off"
        plate_check = {"run": True, "flagged": {}, "rounds": [], "moved_to_support_plates": []}
        if check_path != raw_path:
            plate_check["supports_off_copy"] = True
        fixed = []
        for rnd in range(PLATE_CHECK_ROUNDS):
            flagged = probe.plate_check(cfg, bambu, check_path)
            errs = flagged.pop("errors", {})
            for pid, why in sorted(errs.items()):
                print(f"ERROR: plate check: plate {pid} ({pack.plate_name(plates[pid - 1])}) "
                      f"does not slice: {why[:200]}")
            plate_check["rounds"].append({"flagged": {str(k): v for k, v in flagged.items()},
                                          "errors": {str(k): v for k, v in errs.items()}})
            moved, fixed = [], []
            for pid, msg in sorted(flagged.items()):
                plate = plates[pid - 1]
                plate_check["flagged"][str(pid)] = msg
                if "empty layer" in msg:
                    print(f"ERROR: plate {pid} ({pack.plate_name(plate)}) has an empty layer: {msg}")
                    continue
                if plate.get("support"):
                    continue
                if plate.get("objects"):
                    print(f"WARNING: fit-test plate {pid} floats on the full plate ({msg[:60]}...)")
                    continue
                pobjs = threemf._plate_objects(plate, pack.plate_name(plate), per_part=per_part)
                stems = {Path(it[0]).stem: it[0] for it in plate["items"]}
                pfs = [stems.get(n.rsplit(" #", 1)[0])
                       for n in re.findall(r"It seems object (.+?) has floating", msg)]
                if len(pobjs) == 1 or not pfs or not all(pfs):
                    # one object (default profile), or Bambu named a colour
                    # object: the plate as before
                    plate["support"] = "slice-check"
                    fixed.append(pid)
                    print(f"plate check: plate {pid} ({pack.plate_name(plate)}) floats on the full "
                          f"plate ({msg[:60]}...); enabling supports on that plate")
                    continue
                for pf in dict.fromkeys(pfs):
                    one = next((m for m in msg.split(" | ") if f"object {Path(pf).stem} #" in m), msg)
                    if not meshes[pf]["needs_support"]:
                        meshes[pf]["needs_support"] = True
                        moved.append(pf)
                        print(f"plate check: plate {pid} ({pack.plate_name(plate)}): {pf} floats "
                              f"({one[:60]}...); moving that part type to the support plates")
            if not moved or rnd == PLATE_CHECK_ROUNDS - 1:
                if moved:
                    print(f"WARNING: plate check: part types still newly flagged after "
                          f"{PLATE_CHECK_ROUNDS} rounds ({', '.join(moved)}); run verify")
                break
            plate_check["moved_to_support_plates"] += sorted(set(moved))
            plates, oversized = layout()
            threemf.write_project(cfg, set_info, plates, meshes, check_path,
                                  verdict=check_path != raw_path)
        plate_check["supports_enabled"] = fixed
        needs_support = sorted(f for f, m in meshes.items() if m["needs_support"])
        threemf.write_project(cfg, set_info, plates, meshes, raw_path)
        if check_path != raw_path:
            try:
                check_path.unlink()
            except OSError:
                pass

    final_from_bambu = False
    if args.bambu:
        final_from_bambu = _normalise_with_bambu(cfg, raw_path, out_path)
    if not final_from_bambu:
        shutil.copy2(raw_path, out_path)

    stats = threemf.validate_project(out_path, cfg["bed"], cfg["depth"])
    color_ids = [s["color_id"] for s in threemf._slots(plates)]   # extruder order
    plates_csv = out_path.with_name(out_path.stem + "_plates.csv")
    _write_plates_csv(plates_csv, plates, meshes, color_ids)
    if missing:
        missing_csv = out_path.with_name(out_path.stem + "_missing.csv")
        rebrickable.write_bom_csv(missing_csv, missing)
    report = {
        "set": set_info,
        "bom_lots": len(lots),
        "mapped_lots": len(mapped),
        "merged_lots": len(merged),
        "parts": sum(m["quantity"] for m in merged),
        "missing_lots": len(missing),
        "missing_parts": sum(m["quantity"] for m in missing),
        "oversized_lots": len(oversized),
        "colors": len(color_ids),
        "plates": stats["plate_count"],
        "components": stats["components"],
        "triangles": stats["triangles"],
        "bad_plates": stats["bad_plates"],
        "orientation_warnings": orientation_warnings,
        "needs_support": needs_support,
        "support_plates": sum(1 for p in plates if p.get("support")),
        "mix_colours": getattr(args, "mix_colours", None) or "auto",
        "mixed_plates": sum(1 for p in plates if p.get("groups")),
        "probe": probe_report,
        "plate_check": plate_check,
        "floating_verified": bool(probe_report.get("used"))
                             and not probe_report.get("unverified_parts"),
        "bambu_normalised": final_from_bambu,
        "output": str(out_path),
        "plates_csv": str(plates_csv),
        "missing_csv": str(out_path.with_name(out_path.stem + "_missing.csv")) if missing else None,
        "seconds": round(time.time() - t0, 1),
    }
    if not comp.legacy:
        rows = quality.report(cache, unique_files)
        qcsv = out_path.with_name(out_path.stem + "_quality.csv")
        quality.write_csv(qcsv, rows)
        flagged_q = [r for r in rows if r["flags"]]
        report.update({
            "settings": comp.summary(),
            "features": feats,
            "fit_test_parts": test_files,
            "quality_csv": str(qcsv),
            "quality_flags": {r["part_file"]: r["flags"] for r in flagged_q},
        })
        if flagged_q:
            print("mesh quality flags: " + "; ".join(f"{r['part_file']}: {', '.join(r['flags'])}"
                                                     for r in flagged_q[:20]))
        if final_from_bambu:
            from . import verify
            report["normalised_models_sha"] = verify.models_sha(out_path)
    report_path = out_path.with_name(out_path.stem + "_report.json")
    report_path.write_text(json.dumps(report, indent=1))

    print()
    print(f"{set_info['set_num']}  {set_info['name']}")
    print(f"  parts     {report['parts']}  ({report['mapped_lots']} LDraw lots)")
    print(f"  colours   {report['colors']}")
    print(f"  plates    {report['plates']}  ({report['support_plates']} with supports"
          + (f", {report['mixed_plates']} mixed-colour" if report["mixed_plates"] else "") + ")")
    if probe_report.get("used"):
        print(f"  probe     {probe_report['parts_verified']}/{probe_report['parts']} parts "
              f"Bambu-verified, {probe_report['seconds']}s")
    else:
        print(f"  probe     SKIPPED ({probe_report.get('skipped')}): floating detection unverified")
    print(f"  missing  {report['missing_lots']} lots / {report['missing_parts']} parts")
    print(f"  oversized {report['oversized_lots']} lots")
    print(f"  output    {out_path}")
    print(f"  plates    {plates_csv}")
    print(f"  report    {report_path}")
    if stats["bad_plates"]:
        print(f"WARNING: {stats['bad_plates']} plate(s) failed the bbox check")
    rc = 0 if stats["bad_plates"] == 0 else 1
    if getattr(args, "verify", False):
        rc = max(rc, _run_verify(cfg, out_path, None, getattr(args, "reference", None),
                                 0.01, None))
    print(f"Done in {round(time.time() - t0, 1)}s")
    return rc


def _expected(cfg) -> dict:
    """Process-level values the build intended (profile overrides, fit, --set)."""
    comp = threemf.composer(cfg)
    if comp.legacy:
        return {}
    out = {}
    for k, v in comp.process_overrides().items():
        out[k] = settings.fmt(v)
    return out


def _run_verify(cfg, path: Path, plates, reference, tol, workdir: Path | None,
                expected: dict | None = None, report: Path | None = None) -> int:
    from . import verify
    bambu = probe.find_bambu(cfg)
    if not bambu:
        print("verify: Bambu Studio not found; cannot slice")
        return 1
    if expected is None:
        expected = _expected(cfg) if cfg.get("_composer") is not None else {}
    # the supports-off slice runs beside the file's own slice unless the
    # machine is configured for one Bambu run at a time (probe_workers 1)
    res = verify.run(Path(path), bambu, plates=plates, reference=reference, tol=tol,
                     workdir=workdir, expected=expected,
                     concurrent=probe.default_workers(cfg) > 1)
    verify.print_summary(res)
    if report is not None:
        out = Path(report)
    elif workdir is not None:
        out = Path(workdir) / (Path(path).stem + "_verify.json")
    else:
        out = Path(path).with_name(Path(path).stem + "_verify.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1, default=str))
    print(f"verify report: {out}")
    return 0 if res["ok"] else 1


def cmd_verify(args):
    cfg = config.load_config()
    plates = [int(x) for x in args.plates.split(",")] if args.plates else None
    wd = Path(args.workdir) if args.workdir else None
    sweep.maybe_sweep(cfg, exclude=[str(wd) if wd else "", str(Path(args.file).resolve())])
    expected = {}
    rep = Path(args.file).with_name(Path(args.file).stem + "_report.json")
    if args.profile or args.fit or args.set_kv:
        _apply_options(cfg, args)
        expected = _expected(cfg)
    elif rep.is_file() and json.loads(rep.read_text()).get("settings"):
        s = json.loads(rep.read_text())["settings"]
        cfg.update({"profile": s["profile"], "fit": f"{s['fit']['contour']},{s['fit']['hole']}",
                    "set": [f"{k}={v}" for k, v in s.get("set", {}).items()],
                    "supports": s.get("supports"), "bridging": s.get("bridging"),
                    "filaments": [], "_composer": None})
        expected = _expected(cfg)
        print(f"verify: intended settings from {rep.name} (profile {s['profile']})")
    for kv in args.expect or []:
        if "=" not in kv:
            raise SystemExit(f"--expect {kv!r}: expected KEY=VALUE")
        k, v = kv.split("=", 1)
        expected[k.strip()] = v.strip()
    return _run_verify(cfg, Path(args.file), plates, args.reference, args.tol, wd, expected,
                       Path(args.report) if args.report else None)


def cmd_calibrate(args):
    """One project, one object per (fit value, test part); per-object X-Y
    compensation (Bambu honours it per object, not per part)."""
    cfg = config.load_config()
    config.ensure_dirs(cfg)
    _apply_options(cfg, args)
    comp = threemf.composer(cfg)
    try:
        values = [float(v) for v in args.values.split(",")]
    except ValueError:
        raise SystemExit(f"--values {args.values!r}: expected comma-separated mm values")
    if not values:
        raise SystemExit("--values: no values given")
    for v in values:
        settings.check_fit_value("--values", v, "contour")
    values = sorted(set(values))      # deterministic layout: ascending value
    if not cfg["ldraw_dir"] or not Path(cfg["ldraw_dir"]).is_dir():
        raise SystemExit("LDraw library not found; run brickprint doctor or brickprint bootstrap")
    library = ldraw.LDrawLibrary(cfg["ldraw_dir"])
    part_map = ldraw.load_part_map(getattr(args, "part_map", None))
    rb_dir = config.rebrickable_dir(cfg)
    rel = rebrickable.load_relationships(rb_dir) if (rb_dir / "part_relationships.csv.gz").is_file() else {}
    files = _resolve_parts(library, [p.strip() for p in args.parts.split(",") if p.strip()], rel, part_map)
    colour = {"name": "Fit calibration", "rgb": "CCCCCC", "is_trans": False}
    if cfg["filaments"]:
        spec = cfg["filaments"][0].split("=", 1)[-1]
        fp = settings.load_profile("filament", spec)
        cfg["filaments"] = [spec]
        colour["rgb"] = (fp.get("colour") or "#CCCCCC").lstrip("#")
    cfg["_composer"] = None
    comp = threemf.composer(cfg)
    _check_profile(cfg, {"cal": colour})
    meshes, probe_report, bambu = _prepare_meshes(cfg, sorted(set(files)), _load_orient(None),
                                                  args.probe)
    feats = _apply_features(cfg, meshes)
    width = min(cfg["bed"], cfg["depth"]) - 2 * cfg["margin"]
    gap = 6.0
    # rows of test pieces, one row per value
    objs, y = [], 0.0
    plates_objs = [[]]
    row_pos = []          # (value, plate number, row number on that plate)
    for v in values:
        h = 0.0
        x = 0.0
        hole = -v if args.hole_ratio is None else -v * args.hole_ratio
        row = []
        for pf in files:
            w, d = float(meshes[pf]["size"][0]), float(meshes[pf]["size"][1])
            if x + w > width:
                raise SystemExit("calibration row does not fit the bed; pass fewer --parts")
            row.append({"name": f"fit {v:+.3f} {Path(pf).stem}", "items": [(pf, x, None, w, d)],
                        "metadata": {"xy_contour_compensation": settings.fmt(v),
                                     "xy_hole_compensation": settings.fmt(hole)}})
            x += w + gap
            h = max(h, d)
        if y + h > width:
            plates_objs.append([])
            y = 0.0
        for o in row:
            pf, ox, _, w, d = o["items"][0]
            o["items"] = [(pf, ox, y, w, d)]
            plates_objs[-1].append(o)
        row_pos.append((v, len(plates_objs), sum(1 for r in row_pos if r[1] == len(plates_objs)) + 1))
        y += h + gap
    plates = []
    for i, objs in enumerate(plates_objs, 1):
        plates.append({"color_id": "cal", "color_name": "Fit calibration", "rgb": colour["rgb"],
                       "is_trans": False, "index": i, "count": len(plates_objs), "kind": "",
                       "support": "", "items": [it for o in objs for it in o["items"]],
                       "objects": objs, "bbox": (0, 0)})
    out = Path(args.out)
    raw = out.with_name(out.stem + "_raw.3mf")
    threemf.write_project(cfg, {"set_num": "fit-calibration", "name": "brickprint fit calibration"},
                          plates, meshes, raw)
    ok = False
    if args.bambu:
        ok = _normalise_with_bambu(cfg, raw, out)
    if not ok:
        shutil.copy2(raw, out)
    print(f"calibration: {len(values)} values x {len(files)} parts on {len(plates)} plate(s) -> {out}")
    names = ", ".join(Path(pf).stem for pf in files)
    for v, pn, rn in row_pos:
        hv = -v if args.hole_ratio is None else -v * args.hole_ratio
        print(f"  plate {pn} row {rn}: contour {v:+.3f} / hole {hv:+.3f}")
    print("row mapping: the printed pieces carry no marking. Rows run from the FRONT of the "
          "plate (y min, toward you at the printer) to the BACK in ASCENDING contour value, "
          "so the front row is the smallest value; within a row the parts run left to right "
          f"as: {names}. If there are several plates, rows continue on the next plate. "
          "In Bambu Studio's object list each object is named 'fit <contour> <part>'.")
    rc = 0
    if args.verify:
        rc = _run_verify(cfg, out, None, None, 0.01, None)
    return rc


def cmd_quality(args):
    cfg = config.load_config()
    if not cfg["ldraw_dir"] or not Path(cfg["ldraw_dir"]).is_dir():
        raise SystemExit("LDraw library not found; run brickprint doctor or brickprint bootstrap")
    library = ldraw.LDrawLibrary(cfg["ldraw_dir"])
    part_map = ldraw.load_part_map(getattr(args, "part_map", None))
    if args.bom:
        lots = ldraw.read_bom(Path(args.bom))
        mapped, _ = _map_lots(lots, library, {}, part_map)
        files = sorted({m["file"] for m in mapped})
    else:
        files = _resolve_parts(library, args.parts, {}, part_map)
    rows = quality.report(ldraw.MeshCache(cfg), files)
    quality.print_table(rows)
    if args.csv:
        quality.write_csv(Path(args.csv), rows)
        print(f"wrote {args.csv}")
    return 0


def cmd_profiles(args):
    cfg = config.load_config()
    if args.action == "list":
        for kind, items in settings.list_profiles().items():
            print(f"{kind}:")
            for name, path in items:
                d = json.loads(path.read_text())
                print(f"  {name:28s} {d.get('description') or d.get('display') or ''}")
        return 0
    if args.action == "show":
        kind = "filament" if any(args.name == n for n, _ in settings.list_profiles()["filament"]) else "process"
        prof = settings.load_profile(kind, args.name)
        if not args.resolved:
            print(json.dumps({k: v for k, v in prof.items() if k != "_path"}, indent=2, ensure_ascii=False))
            return 0
        cfg["profile"] = args.name if kind == "process" else None
        cfg["filaments"] = [args.name] if kind == "filament" else list(args.filament or [])
        cfg["fit"] = args.fit
        cfg["set"] = []
        comp = settings.Composer(cfg)
        S = comp.compose([{"color_id": "1", "color_name": "slot1", "hex": "CCCCCC"}])
        diff = S["different_settings_to_system"]
        print(f"process base: {S['print_settings_id']}; filament base: {S['filament_settings_id'][0]}")
        print(f"different_settings_to_system[0] (process): {diff[0] or '(none)'}")
        print(f"different_settings_to_system[1] (filament): {diff[1] or '(none)'}")
        keys = set(k for d in diff for k in d.split(";") if k)
        for k in sorted(keys):
            print(f"  {k} = {S[k]}")
        return 0
    if args.action == "check":
        return _profiles_check()
    if args.action == "export":
        prof = settings.load_profile("filament", args.name)
        cfg["filaments"] = [args.name]
        cfg["set"] = []
        comp = settings.Composer(cfg)
        S = comp.compose([{"color_id": "1", "color_name": "x", "hex": "CCCCCC"}])
        out = {"type": "filament", "name": f"{prof.get('display', prof['name'])} @ A1M",
               "inherits": prof["base_filament"], "from": "User", "instantiation": "true",
               "filament_id": S["filament_ids"][0],
               "compatible_printers": [comp.profile.get("printer", "Bambu Lab A1 mini 0.4 nozzle")]}
        for k in prof.get("overrides", {}):
            out[k] = S[k] if isinstance(S[k], list) else [S[k]]
        if not args.to:
            raise SystemExit("profiles export: pass --to DIR")
        for key in ("base_filament", "name"):
            if key not in prof:
                raise SystemExit(f"filament profile {args.name}: missing {key!r}")
        dest = Path(args.to)
        if dest.resolve().as_posix().find("BambuStudio/user") >= 0:
            raise SystemExit("profiles export never writes into Bambu Studio's user folders; "
                             "export elsewhere and import the file in Bambu Studio")
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / f"{out['name']}.json"
        f.write_text(json.dumps(out, indent=4) + "\n")
        print(f"wrote {f} (import it in Bambu Studio: Filament > Import)")
        return 0
    if args.action == "regen-template":
        from . import bambu_profiles as bp
        app = bp.app_from_exe(cfg.get("bambu"))
        text = bp.template_text(bp.build_template(app))
        target = Path(cfg["settings"]) if not args.out else Path(args.out)
        old = target.read_text() if target.is_file() else ""
        if old == text:
            print(f"{target}: up to date with Bambu Studio {bp.app_version(app)}")
            return 0
        target.write_text(text)
        print(f"rewrote {target} from Bambu Studio {bp.app_version(app)}")
        return 0
    return 1


def _profiles_check() -> int:
    """Read-only: installed Bambu user filament presets vs brickprint profiles."""
    root = Path.home() / "Library" / "Application Support" / "BambuStudio" / "user"
    profs = {}
    for name, path in settings.list_profiles()["filament"]:
        d = json.loads(path.read_text())
        profs[d.get("display", name).lower()] = d
    found = 0
    for f in sorted(root.glob("*/filament/*.json")) if root.is_dir() else []:
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        nm = d.get("name", f.stem)
        match = next((p for disp, p in profs.items() if nm.lower().startswith(disp)), None)
        if not match:
            continue
        found += 1
        issues = []
        for k, v in match.get("overrides", {}).items():
            have = d.get(k)
            if have is None:
                continue
            hv = have[0] if isinstance(have, list) else have
            if settings.norm(hv) != settings.norm(settings.fmt(v)):
                issues.append(f"{k}: installed {hv} vs profile {settings.fmt(v)}")
        for i, s in enumerate(d.get("filament_shrink") or []):
            if float(str(s).rstrip("%")) > 100:
                issues.append(f"filament_shrink {s} > 100% (scales parts DOWN)")
        print(f"{f.relative_to(root)}  ({match['name']}): " + ("OK" if not issues else "STALE"))
        for i in issues:
            print(f"   {i}")
    if not found:
        print(f"no installed user presets match a brickprint filament profile under {root}")
    print("(read-only check; brickprint never writes Bambu Studio's preset folders. "
          "Projects embed their settings; use `brickprint profiles export` to create an importable preset.)")
    return 0


def cmd_validate(args):
    cfg = config.load_config()
    stats = threemf.validate_project(Path(args.file), float(args.bed or cfg["bed"]), float(args.depth or cfg["depth"]))
    print(f"plates {stats['plate_count']}  components {stats['components']}  triangles {stats['triangles']}")
    for p in stats["plates"]:
        print(
            f"{p['id']:2d} {p['name']:28s} parts={p['components']:3d} "
            f"{p['size'][0]:6.1f} x {p['size'][1]:6.1f} x {p['size'][2]:5.1f} "
            f"{'OK' if p['ok'] else 'BAD'}"
        )
    if args.slice:
        print("use `brickprint verify FILE` to slice and check the project.")
    return 0 if stats["bad_plates"] == 0 else 1


def _tuning_args(p, fit: bool = True) -> None:
    g = p.add_argument_group("print tuning (see `brickprint profiles list`)")
    g.add_argument("--profile", help="process profile (default a1mini-pla-stock = Bambu stock "
                                     "PLA 0.20 mm, unchanged output)")
    g.add_argument("--filament", action="append", metavar="COLOUR=PROFILE",
                   help="filament profile per colour (name, color_id or hex), or PROFILE for "
                        "every slot; repeatable")
    if fit:
        g.add_argument("--fit", metavar="C[,H]",
                       help="X-Y contour compensation +C mm and hole compensation H "
                            "(default -C); 0 = none; default from the profile")
    g.add_argument("--supports", choices=["enforcers", "plates"],
                   help="enforcers: support only in generated enforcer volumes under "
                        "cantilevered features (never inside sockets); plates: '<Colour> "
                        "supports' plates with tree supports; default from the profile")
    g.add_argument("--ceilings", choices=["recipe8", "off"],
                   help="recipe8: plate sockets raised to 2.2 mm, thick anchored ceiling "
                        "bridges (two 0.24 mm layers, 2 wall loops on the bridge layer) and "
                        "span-cutting ribs; one object per part; needs thick_bridges=1; "
                        "default from the profile")
    g.add_argument("--bridging", choices=["short-span", "stock"],
                   help="short-span: per-part bridge angle across the short side of cavity "
                        "ceilings; default from the profile")
    g.add_argument("--set", dest="set_kv", action="append", metavar="KEY=VALUE",
                   help="expert: any Bambu process key (shaped to Bambu's type and listed in "
                        "different_settings_to_system); repeatable")
    g.add_argument("--allow-shrink", action="store_true",
                   help="accept filament_shrink above 100%% (it scales parts DOWN)")
    g.add_argument("--part-map", help="JSON rebrickable part -> LDraw part (adds to "
                                      "~/.config/brickprint/part_overrides.json)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="brickprint", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check paths and dependencies")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("bootstrap", help="download Rebrickable + LDraw data")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("inventory", help="look up a set and write its BOM")
    p.add_argument("set")
    p.add_argument("--out")
    p.add_argument("--include-spares", action="store_true")
    p.add_argument("--include-minifigs", action="store_true")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_inventory)

    p = sub.add_parser("build", help="build the colour-grouped multi-plate 3MF")
    p.add_argument("set", nargs="?")
    p.add_argument("--bom", help="use a local BOM CSV instead of Rebrickable")
    p.add_argument("--out")
    p.add_argument("--name")
    p.add_argument("--orient", help="JSON file of part -> Rx/Ry/Rx2/Ry2")
    p.add_argument("--bed", type=float)
    p.add_argument("--depth", type=float)
    p.add_argument("--margin", type=float)
    p.add_argument("--gap", type=float)
    p.add_argument("--include-spares", action="store_true")
    p.add_argument("--include-minifigs", action="store_true")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--strict", action="store_true", help="fail on missing LDraw parts")
    p.add_argument("--bambu", action="store_true",
                   help="normalise with the Bambu Studio CLI (thumbnails); works with the GUI open")
    p.add_argument("--no-bambu", dest="bambu", action="store_false")
    p.add_argument("--no-probe", dest="probe", action="store_false",
                   help="skip the Bambu Studio floating probe (emulator only: "
                        "floating detection unverified)")
    _tuning_args(p)
    p.add_argument("--fit-test", nargs="?", const="auto", metavar="PARTS",
                   help="append a 'Fit test' plate of separate test pieces (comma-separated "
                        "part numbers, e.g. --fit-test=3022,3005; bare --fit-test picks from the "
                        "BOM; put it after the set number) printed with the build's fit")
    p.add_argument("--mix-colours", choices=list(pack.MIX_MODES), default="auto",
                   help="let tiny colour groups (last plate <= "
                        f"{pack.MIX_FILL_MAX * 100:.0f}%% full) share plates, <= {pack.MIX_MAX_COLOURS} colours each: "
                        "auto (default) = only when needed to stay within 36 plates (incl. a "
                        "--fit-test plate) instead of folding supports; always; off")
    p.add_argument("--verify", action="store_true",
                   help="slice the result with the Bambu Studio CLI and run `verify`")
    p.add_argument("--reference", help="with --verify: compare against this 3MF")
    p.set_defaults(func=cmd_build, bambu=False, probe=True)

    p = sub.add_parser("calibrate", help="fit calibration project (several X-Y compensation values in one print)")
    p.add_argument("what", choices=["fit"])
    p.add_argument("--values", default="0.02,0.03,0.04,0.05",
                   help="contour compensation values in mm, one row each (default 0.02,0.03,0.04,0.05)")
    p.add_argument("--hole-ratio", type=float, default=None,
                   help="hole compensation = -value x ratio (default 1: hole = -value)")
    p.add_argument("--parts", default="3022,3005,3024,4733",
                   help="test parts, comma-separated (default 3022,3005,3024,4733)")
    p.add_argument("--out", default="fit_calibration.3mf")
    p.add_argument("--bambu", action="store_true", help="normalise with Bambu Studio (thumbnails)")
    p.add_argument("--no-probe", dest="probe", action="store_false")
    p.add_argument("--verify", action="store_true", help="slice and measure every row")
    _tuning_args(p, fit=False)
    p.set_defaults(func=cmd_calibrate, probe=True)

    p = sub.add_parser("verify", help="slice a 3MF with the Bambu Studio CLI and check effective "
                                      "settings, stud/socket toolpaths, supports, floating")
    p.add_argument("file")
    p.add_argument("--plates", help="comma-separated plate numbers (default: all)")
    p.add_argument("--reference", help="compare settings and fit numbers with this 3MF")
    p.add_argument("--tol", type=float, default=0.01, help="fit tolerance in mm (default 0.01)")
    p.add_argument("--workdir", help="keep the sliced G-code here; the <file>_verify.json report "
                                     "is written here too (default: next to FILE)")
    p.add_argument("--report", metavar="PATH",
                   help="write the JSON report to PATH (overrides --workdir / next-to-FILE)")
    p.add_argument("--expect", action="append", metavar="KEY=VALUE",
                   help="intended value that must be effective in the G-code; repeatable")
    p.add_argument("--profile", help="intended settings = this profile (+ --fit/--set); "
                                     "default: from <file>_report.json if present")
    p.add_argument("--fit", help="intended fit (with --profile)")
    p.add_argument("--set", dest="set_kv", action="append", help="intended extra keys (with --profile)")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("quality", help="per-part mesh quality report")
    p.add_argument("parts", nargs="*", help="part numbers")
    p.add_argument("--bom", help="all parts of a BOM CSV")
    p.add_argument("--csv", help="also write a CSV")
    p.add_argument("--part-map", help="JSON rebrickable part -> LDraw part")
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser("profiles", help="list / show / check / export print profiles")
    p.add_argument("action", choices=["list", "show", "check", "export", "regen-template"])
    p.add_argument("name", nargs="?")
    p.add_argument("--resolved", action="store_true", help="show: final values + diff lists")
    p.add_argument("--filament", action="append", help="show --resolved: filament profile")
    p.add_argument("--fit", help="show --resolved: fit")
    p.add_argument("--to", help="export: target directory (never Bambu's user folders)")
    p.add_argument("--out", help="regen-template: write here instead of the skill asset")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("validate", help="check a generated 3MF")
    p.add_argument("file")
    p.add_argument("--bed", type=float)
    p.add_argument("--depth", type=float)
    p.add_argument("--slice", action="store_true")
    p.set_defaults(func=cmd_validate)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command == "build" and not args.set and not args.bom:
        ap.error("build needs a set number or --bom")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
