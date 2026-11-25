"""Bambu Studio multi-plate 3MF writer and validator."""

from __future__ import annotations

import json
import math
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as _sax_escape

import numpy as np

from .pack import plate_colors, plate_name as _plate_name

NS = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" '
    'requiredextensions="p"'
)

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="config" ContentType="text/xml"/>
 <Default Extension="png" ContentType="image/png"/>
 <Default Extension="txt" ContentType="text/plain"/>
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header>
    <header_item key="X-BBL-Client-Type" value="slicer"/>
    <header_item key="X-BBL-Client-Version" value="02.09.01.70"/>
  </header>
</config>
"""


def xml_escape(text) -> str:
    """Escape for XML text and double-quoted attribute values (&, <, >, ")."""
    return _sax_escape(str(text), {'"': "&quot;"})


def _mesh_body(V: np.ndarray, T: np.ndarray) -> str:
    verts = "".join([
        f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in V.tolist()
    ])
    tris = "".join([f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in T.tolist()])
    return f'<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh>'


def _mesh_object(oid: int, V: np.ndarray, T: np.ndarray, body: str | None = None) -> str:
    if body is None:
        body = _mesh_body(V, T)
    return f'<object id="{oid}" p:UUID="{uuid.uuid4()}" type="model">{body}</object>'


def _support_metadata(plate: dict, cfg: dict | None = None) -> str:
    """Per-object overrides in model_settings.config for support plates.

    Project settings keep enable_support=0; only "<Colour> supports" plates
    (and normal plates that a support group was folded into) turn on tree
    supports, with bridge_no_support=1 so brick cavity ceilings stay bridges.
    """
    if not plate.get("support"):
        return ""
    return "".join(f'<metadata key="{xml_escape(k)}" value="{xml_escape(v)}"/>'
                   for k, v in support_metadata(cfg).items())


def _plate_model(entries: list[str]) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" {NS}>
 <metadata name="BambuStudio:3mfVersion">1</metadata>
 <resources>{"".join(entries)}</resources>
</model>
"""


def expand_per_filament(settings: dict, hexes: list[str]) -> dict:
    """Expand the one-slot settings template to len(hexes) filament slots.

    Every key listed in the template's reserved "_brickprint_per_filament_keys"
    (written by tools/build_settings_template.py) is repeated per slot;
    filament_colour gets the real colours.  Per-extruder/machine arrays are
    left alone.  Mismatched sizes make Bambu fail with "Flush volumes matrix
    do not match to the correct size!" or silently reuse slot 1.
    """
    settings = dict(settings)
    keys = settings.pop("_brickprint_per_filament_keys", None)
    if keys is None:
        raise SystemExit("settings template lacks _brickprint_per_filament_keys; "
                         "regenerate it with tools/build_settings_template.py")
    nfil = len(hexes)
    for k in keys:
        v = settings[k]
        if not (isinstance(v, list) and len(v) == 1):
            raise SystemExit(f"settings template: {k} is not a one-slot list")
        settings[k] = v * nfil
    settings["filament_colour"] = list(hexes)
    settings["filament_self_index"] = [str(i + 1) for i in range(nfil)]
    settings["flush_volumes_matrix"] = [
        str(0 if i == j else 280) for i in range(nfil) for j in range(nfil)
    ]
    settings["flush_volumes_vector"] = ["140"] * (2 * nfil)
    # [process, filament_1..N, printer]; all empty = stock system presets.
    settings["different_settings_to_system"] = [""] * (nfil + 2)
    settings["inherits_group"] = [""] * (nfil + 2)
    return settings


def _slots(plates: list[dict]) -> list[dict]:
    """Filament slots in plate order (extruder i+1 = slots[i]); a mixed
    plate contributes each of its colours in group order."""
    out, seen = [], set()
    for plate in plates:
        for c in plate_colors(plate):
            if c["color_id"] not in seen:
                seen.add(c["color_id"])
                out.append({"color_id": c["color_id"], "color_name": c.get("color_name", ""),
                            "hex": c["rgb"].lstrip("#")})
    return out


def composer(cfg: dict):
    """The build's settings.Composer (created once per cfg)."""
    from . import settings as _settings
    if cfg.get("_composer") is None:
        cfg["_composer"] = _settings.Composer(cfg)
    return cfg["_composer"]


AUTO_SUPPORT_META = {"enable_support": "1", "support_type": "tree(auto)", "bridge_no_support": "1"}


def support_metadata(cfg: dict | None = None) -> dict:
    """Per-object settings of an object with its own tree supports: the base
    keys plus the --support-set / profile ``supports.object_settings`` extras."""
    meta = dict(AUTO_SUPPORT_META)
    if cfg is not None:
        meta.update(composer(cfg).support_set)
    return meta


def _plate_objects(plate: dict, plate_name: str, per_part: bool = False) -> list[dict]:
    """A plate is one object (all parts as components) unless it lists
    ``objects`` (fit-test / calibration plates: one object per piece, each
    with its own per-object settings).

    A mixed plate (``groups``) never puts two colours in one object: Bambu's
    extruder is per object here, so it gets one object per colour group (or
    one per part with ``per_part``), each carrying its ``color_id``.

    ``auto_support`` marks objects with automatic supports (they get socket
    blockers in profile builds).
    """
    if plate.get("objects"):
        return plate["objects"]
    def part_objs(pairs):
        seen: dict[str, int] = {}
        objs = []
        for it, color_id in pairs:
            stem = Path(it[0]).stem
            seen[stem] = seen.get(stem, 0) + 1
            name = f"{stem} #{seen[stem]}"
            o = {"name": name, "items": [it], "metadata": {}}
            if color_id is not None:
                o["color_id"] = color_id
            objs.append(o)
        return objs

    if plate.get("groups"):
        if per_part:
            return part_objs([(it, g["color_id"]) for g in plate["groups"] for it in g["items"]])
        short = plate_name.split(" (", 1)[0]   # "Mixed [supports ]i of n"
        return [{"name": f"{short}: {g['color_name']}", "items": g["items"], "metadata": {},
                 "_legacy_support": True, "color_id": g["color_id"],
                 "auto_support": bool(plate.get("support"))} for g in plate["groups"]]
    if per_part:
        # height ranges are per object: one object per part instance
        return part_objs([(it, None) for it in plate["items"]])
    meta = {}
    if plate.get("support"):
        meta = dict(AUTO_SUPPORT_META)
    return [{"name": plate_name, "items": plate["items"], "metadata": meta, "_legacy_support": True,
             "auto_support": bool(plate.get("support"))}]


def _matrix(x: float, y: float) -> str:
    return f"1 0 0 {x:.4f} 0 1 0 {y:.4f} 0 0 1 0 0 0 0 1"


def _merge_bands(bands: list[tuple[float, float, dict]]) -> list[tuple[float, float, dict]]:
    """Merge height-range bands from multiple parts in a merged Bambu object
    into sorted, non-overlapping ``(z0, z1, opts)`` ranges."""
    if len(bands) <= 1:
        return list(bands)
    raw_pts = sorted(p for z0, z1, _ in bands for p in (float(z0), float(z1)))
    pts: list[float] = []
    for p in raw_pts:
        if not pts or p - pts[-1] > 1e-4:
            pts.append(p)
    merged: list[tuple[float, float, dict]] = []
    for za, zb in zip(pts, pts[1:]):
        if zb - za <= 1e-4:
            continue
        mid = 0.5 * (za + zb)
        active = [opts for z0, z1, opts in bands if float(z0) <= mid <= float(z1)]
        if not active:
            continue
        opts: dict[str, str] = {}
        for d in active:
            for k, v in d.items():
                sv = str(v)
                if k not in opts:
                    opts[k] = sv
                elif k == "layer_height":
                    # Finest layer height wins (preserves 0.08 mm slope-safe anchor bands)
                    if float(sv) < float(opts[k]):
                        opts[k] = sv
                elif k == "wall_loops":
                    # Fewest wall loops wins (preserves 2-wall bridge anchoring)
                    if int(sv) < int(opts[k]):
                        opts[k] = sv
        z0_r, z1_r = round(za, 4), round(zb, 4)
        if merged and abs(merged[-1][1] - z0_r) <= 1e-4 and merged[-1][2] == opts:
            merged[-1] = (merged[-1][0], z1_r, opts)
        else:
            merged.append((z0_r, z1_r, opts))
    return merged


def write_project(
    cfg: dict,
    set_info: dict,
    plates: list[dict],
    meshes: dict[str, dict],
    out_path: Path,
    verdict: bool = False,
    compresslevel: int | None = None,
) -> Path:
    """Write the multi-plate project.

    ``meshes[part]`` may carry ``part_meta`` (per-part settings such as
    bridge_angle) and ``enforcer`` ((V, T) support-enforcer volume in the
    part frame); objects that use either get Bambu ``<part>`` entries for every
    component, in component order.  ``verdict=True`` writes the supports-off
    copy used for Bambu's floating checks (Bambu stops reporting floating
    regions when supports are enabled).  ``compresslevel`` defaults to 1 for
    ephemeral probe/verdict projects and 6 for final user projects.
    """
    if len(plates) > 36:
        raise SystemExit(f"too many plates ({len(plates)}); Bambu's plate grid supports 36")
    if len(plates) == 0:
        raise SystemExit("nothing to pack")

    per_part = composer(cfg).ceilings_mode != "off"
    # support blockers over enclosed cavities (sockets, tubes) of every part
    # in an object with automatic supports; profile builds only (the stock
    # PLA output stays byte-identical).  Kept in verdict=True as well so
    # support plates match verify.supports_off_copy and keep sockets clear.
    blockers = not composer(cfg).legacy
    slots = _slots(plates)
    color_ids = [s["color_id"] for s in slots]
    color_slot = {cid: i + 1 for i, cid in enumerate(color_ids)}
    if len(color_ids) > int(cfg.get("max_filaments", 32)):
        raise SystemExit(
            f"set needs {len(color_ids)} filament colours, above the configured "
            f"max_filaments={cfg.get('max_filaments')}"
        )

    settings_path = Path(cfg["settings"])
    if not settings_path.is_file():
        raise SystemExit(f"settings template not found: {settings_path}")
    settings = composer(cfg).compose(slots, verdict=verdict)
    towers = [p.get("tower") for p in plates]
    if any(towers) and str(settings.get("enable_prime_tower", "0")) == "1":
        # per-plate prime tower position (lists are per plate): mixed plates
        # get the corner the packer kept free; other plates keep the default
        x0 = (settings.get("wipe_tower_x") or ["15"])[0]
        y0 = (settings.get("wipe_tower_y") or ["220"])[0]
        settings["wipe_tower_x"] = [f"{t[0]:g}" if t else x0 for t in towers]
        settings["wipe_tower_y"] = [f"{t[1]:g}" if t else y0 for t in towers]

    # Allocate globally unique object ids: meshes, enforcer meshes, then the
    # plate object(s).
    plate_parts: list[dict[str, int]] = []
    plate_enf: list[dict[str, int]] = []
    plate_rib: list[dict[str, int]] = []
    plate_blk: list[dict[str, int]] = []
    plate_oids: list[list[int]] = []
    plate_objs: list[list[dict]] = []
    next_id = 1
    for plate in plates:
        name = _plate_name(plate)
        objs = _plate_objects(plate, name, per_part=per_part and not plate.get("support"))
        plate_objs.append(objs)
        files = sorted({it[0] for o in objs for it in o["items"]})
        auto_files = {it[0] for o in objs if o.get("auto_support") for it in o["items"]} if blockers else set()
        for part_file in sorted(auto_files):
            if "blocker" not in meshes[part_file]:
                from . import features
                excl = meshes[part_file].get("enforcer_boxes")
                if excl is None and meshes[part_file].get("enforcer") is not None:
                    excl = features.mesh_boxes(*meshes[part_file]["enforcer"])
                bx = features.socket_blockers(
                    meshes[part_file]["V"], meshes[part_file]["T"], exclude=excl
                )
                meshes[part_file]["blocker"] = features.box_mesh(bx) if bx else None
        mids = {}
        for part_file in files:
            mids[part_file] = next_id
            next_id += 1
        plate_parts.append(mids)
        eids = {}
        for part_file in files:
            # the supports-off verdict copy carries no enforcers: with support
            # disabled Bambu reports an enforced overhang as "floating" (CLI-
            # verified: 60470b alone 5/5 runs flagged with its enforcer, 0/5
            # without), while the real file supports exactly that overhang
            if meshes[part_file].get("enforcer") is not None and not verdict:
                eids[part_file] = next_id
                next_id += 1
        plate_enf.append(eids)
        rids = {}
        for part_file in files:
            if meshes[part_file].get("rib") is not None:
                rids[part_file] = next_id
                next_id += 1
        plate_rib.append(rids)
        bids = {}
        for part_file in sorted(auto_files):
            if meshes[part_file].get("blocker") is not None:
                bids[part_file] = next_id
                next_id += 1
        plate_blk.append(bids)
        oids = []
        for _ in objs:
            oids.append(next_id)
            next_id += 1
        plate_oids.append(oids)

    bed = float(cfg["bed"])
    depth = float(cfg["depth"])
    cols = math.ceil(math.sqrt(len(plates)))
    stride_x = bed * 1.2
    stride_y = depth * 1.2
    margin = float(cfg["margin"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if compresslevel is None:
        compresslevel = 1 if (verdict or set_info.get("set_num") == "probe") else 6
    mesh_body_cache: dict[tuple[str, str], str] = {}

    def cached_mesh(kind: str, fn: str, oid: int, V, T) -> str:
        key = (kind, fn)
        body = mesh_body_cache.get(key)
        if body is None:
            body = _mesh_body(np.asarray(V), np.asarray(T))
            mesh_body_cache[key] = body
        return _mesh_object(oid, V, T, body=body)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("Metadata/slice_info.config", SLICE_INFO)
        z.writestr("Metadata/project_settings.config", json.dumps(settings, indent=1))

        for pi, plate in enumerate(plates):
            mids = plate_parts[pi]
            entries = [
                cached_mesh("part", fn, mids[fn], meshes[fn]["V"], meshes[fn]["T"])
                for fn in mids
            ]
            for fn, eid in plate_enf[pi].items():
                EV, ET = meshes[fn]["enforcer"]
                entries.append(cached_mesh("enf", fn, eid, EV, ET))
            for fn, rid in plate_rib[pi].items():
                RV, RT = meshes[fn]["rib"]
                entries.append(cached_mesh("rib", fn, rid, RV, RT))
            for fn, bid in plate_blk[pi].items():
                BV, BT = meshes[fn]["blocker"]
                entries.append(cached_mesh("blk", fn, bid, BV, BT))
            z.writestr(f"3D/Objects/plate_{pi + 1:02d}.model", _plate_model(entries))

        resources, build, objects, plate_xml = [], [], [], []
        extra_identify = 10000
        object_seq = 0
        ranges_xml: list[str] = []
        for pi, plate in enumerate(plates):
            bx = (pi % cols) * stride_x
            by = -(pi // cols) * stride_y
            extruder = color_slot[plate["color_id"]]
            plate_name = _plate_name(plate)
            instances = []
            for oi, obj in enumerate(plate_objs[pi]):
                oid = plate_oids[pi][oi]
                comps, parts = [], []
                path = f"/3D/Objects/plate_{pi + 1:02d}.model"
                blk = plate_blk[pi] if obj.get("auto_support") else {}
                need_parts = any(meshes[it[0]].get("part_meta") or it[0] in plate_enf[pi]
                                 or meshes[it[0]].get("rib") is not None or it[0] in blk
                                 for it in obj["items"])
                for part_file, x, y, w, d in obj["items"]:
                    tx, ty = margin + x, margin + y
                    comps.append(
                        f'<component p:path="{path}" '
                        f'objectid="{plate_parts[pi][part_file]}" p:UUID="{uuid.uuid4()}" '
                        f'transform="1 0 0 0 1 0 0 0 1 {tx:.4f} {ty:.4f} 0"/>'
                    )
                    if need_parts:
                        meta = "".join(f'<metadata key="{xml_escape(k)}" value="{xml_escape(v)}"/>'
                                       for k, v in sorted((meshes[part_file].get("part_meta") or {}).items()))
                        parts.append(
                            f'<part id="{plate_parts[pi][part_file]}" subtype="normal_part">'
                            f'<metadata key="name" value="{xml_escape(Path(part_file).stem)}"/>{meta}'
                            f'<metadata key="matrix" value="{_matrix(tx, ty)}"/></part>')
                    if part_file in plate_rib[pi]:
                        rid = plate_rib[pi][part_file]
                        comps.append(
                            f'<component p:path="{path}" objectid="{rid}" p:UUID="{uuid.uuid4()}" '
                            f'transform="1 0 0 0 1 0 0 0 1 {tx:.4f} {ty:.4f} 0"/>')
                        parts.append(
                            f'<part id="{rid}" subtype="normal_part">'
                            f'<metadata key="name" value="{xml_escape(Path(part_file).stem)} ceiling rib"/>'
                            f'<metadata key="matrix" value="{_matrix(tx, ty)}"/></part>')
                    if part_file in plate_enf[pi]:
                        eid = plate_enf[pi][part_file]
                        comps.append(
                            f'<component p:path="{path}" objectid="{eid}" p:UUID="{uuid.uuid4()}" '
                            f'transform="1 0 0 0 1 0 0 0 1 {tx:.4f} {ty:.4f} 0"/>')
                        parts.append(
                            f'<part id="{eid}" subtype="support_enforcer">'
                            f'<metadata key="name" value="{xml_escape(Path(part_file).stem)} support enforcer"/>'
                            f'<metadata key="matrix" value="{_matrix(tx, ty)}"/></part>')
                    if part_file in blk:
                        bid = blk[part_file]
                        comps.append(
                            f'<component p:path="{path}" objectid="{bid}" p:UUID="{uuid.uuid4()}" '
                            f'transform="1 0 0 0 1 0 0 0 1 {tx:.4f} {ty:.4f} 0"/>')
                        parts.append(
                            f'<part id="{bid}" subtype="support_blocker">'
                            f'<metadata key="name" value="{xml_escape(Path(part_file).stem)} socket blocker"/>'
                            f'<metadata key="matrix" value="{_matrix(tx, ty)}"/></part>')
                if need_parts and len(parts) != len(comps):
                    raise SystemExit("internal error: <part> entries do not match components")
                resources.append(
                    f'<object id="{oid}" p:UUID="{uuid.uuid4()}" type="model">'
                    f'<components>{"".join(comps)}</components></object>'
                )
                build.append(
                    f'<item objectid="{oid}" p:UUID="{uuid.uuid4()}" '
                    f'transform="1 0 0 0 1 0 0 0 1 {bx:.3f} {by:.3f} 0" printable="1"/>'
                )
                object_seq += 1
                obj_bands: list[tuple[float, float, dict]] = []
                if len(obj["items"]) == 1:
                    obj_bands = list(meshes[obj["items"][0][0]].get("bands") or ())
                else:
                    raw_bands: list[tuple[float, float, dict]] = []
                    for pf in dict.fromkeys(it[0] for it in obj["items"]):
                        b = meshes[pf].get("bands")
                        if b:
                            raw_bands.extend(b)
                    if raw_bands:
                        obj_bands = _merge_bands(raw_bands)
                if obj_bands:
                    rng = [f' <object id="{object_seq}">']
                    for z0, z1, opts in obj_bands:
                        rng.append(f'  <range min_z="{z0:g}" max_z="{z1:g}">')
                        rng.append('   <option opt_key="extruder">0</option>')
                        for k, v in sorted(opts.items()):
                            rng.append(f'   <option opt_key="{xml_escape(k)}">{xml_escape(v)}</option>')
                        rng.append('  </range>')
                    rng.append(' </object>')
                    ranges_xml.append("\n".join(rng))
                meta = obj.get("metadata") or {}
                if len(obj["items"]) == 1 and meshes[obj["items"][0][0]].get("object_meta"):
                    meta = {**meshes[obj["items"][0][0]]["object_meta"], **meta}
                if obj.get("_legacy_support"):
                    meta_xml = _support_metadata(plate, cfg)
                else:
                    meta_xml = "".join(f'<metadata key="{xml_escape(k)}" value="{xml_escape(v)}"/>'
                                       for k, v in meta.items())
                objects.append(
                    f'<object id="{oid}"><metadata key="name" value="{xml_escape(obj["name"])}"/>'
                    f'<metadata key="extruder" value="{color_slot[obj["color_id"]] if "color_id" in obj else extruder}"/>'
                    f'{meta_xml}{"".join(parts)}</object>'
                )
                if oi == 0:
                    ident = 1000 + pi
                else:
                    extra_identify += 1
                    ident = extra_identify
                instances.append(
                    f'<model_instance><metadata key="object_id" value="{oid}"/>'
                    f'<metadata key="instance_id" value="0"/>'
                    f'<metadata key="identify_id" value="{ident}"/></model_instance>'
                )
            safe_plate = xml_escape(plate_name)
            plate_xml.append(
                f'<plate><metadata key="plater_id" value="{pi + 1}"/>'
                f'<metadata key="plater_name" value="{safe_plate}"/>'
                f'<metadata key="locked" value="false"/>'
                f'<metadata key="filament_map_mode" value="Auto For Flush"/>'
                f'{"".join(instances)}</plate>'
            )

        root = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" {NS}>
 <metadata name="Application">BambuStudio-02.09.01.70</metadata>
 <metadata name="BambuStudio:3mfVersion">1</metadata>
 <metadata name="Title">{xml_escape(set_info.get('name', ''))}</metadata>
 <metadata name="Description">{xml_escape(set_info.get('set_num', ''))} parts by color</metadata>
 <resources>{"".join(resources)}</resources>
 <build p:UUID="{uuid.uuid4()}">{"".join(build)}</build>
</model>
"""
        z.writestr("3D/3dmodel.model", root)

        rels = ['<?xml version="1.0" encoding="UTF-8"?>',
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
        for rel, pi in enumerate(range(len(plates)), 1):
            rels.append(
                f' <Relationship Target="/3D/Objects/plate_{pi + 1:02d}.model" '
                f'Id="rel-{rel}" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            )
        rels.append('</Relationships>')
        z.writestr("3D/_rels/3dmodel.model.rels", "\n".join(rels) + "\n")

        ms = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
            + "\n".join(objects)
            + "\n"
            + "\n".join(plate_xml)
            + "\n<assemble>\n</assemble>\n</config>\n"
        )
        z.writestr("Metadata/model_settings.config", ms)
        if ranges_xml:
            z.writestr("Metadata/layer_config_ranges.xml",
                       '<?xml version="1.0" encoding="utf-8"?>\n<objects>\n'
                       + "\n".join(ranges_xml) + "\n</objects>\n")
    return out_path


def validate_project(path: Path, bed: float, depth: float) -> dict:
    """Parse the generated project and check every plate's world bbox."""
    import xml.etree.ElementTree as ET

    ns = {"c": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    mesh = {}
    with zipfile.ZipFile(path) as z:
        for name in [n for n in z.namelist() if n.startswith("3D/Objects/")]:
            root = ET.fromstring(z.read(name))
            res_el = root.find("c:resources", ns)
            objs = res_el.findall("c:object", ns) if res_el is not None else root.findall(".//c:object", ns)
            for obj in objs:
                m_el = obj.find("c:mesh", ns)
                vs_el = m_el.find("c:vertices", ns) if m_el is not None else None
                ts_el = m_el.find("c:triangles", ns) if m_el is not None else None
                vs = vs_el if vs_el is not None else obj.findall(".//c:vertex", ns)
                xs = [float(v.attrib["x"]) for v in vs]
                ys = [float(v.attrib["y"]) for v in vs]
                zs = [float(v.attrib["z"]) for v in vs]
                ntris = len(ts_el) if ts_el is not None else len(obj.findall(".//c:triangle", ns))
                mesh[(name, obj.attrib["id"])] = (
                    np.array([min(xs), min(ys), min(zs)], dtype=np.float64),
                    np.array([max(xs), max(ys), max(zs)], dtype=np.float64),
                    ntris,
                )
        root = ET.fromstring(z.read("3D/3dmodel.model"))
        ms = ET.fromstring(z.read("Metadata/model_settings.config"))
    plate_comps = {}
    for obj in root.findall(".//c:object", ns):
        comps = []
        for c in obj.findall(".//c:component", ns):
            path_attr = c.get("{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path").lstrip("/")
            comps.append((path_attr, c.get("objectid"), [float(x) for x in c.get("transform").split()]))
        plate_comps[obj.get("id")] = comps
    # build-item translation relative to the plate's grid origin (0 for the
    # raw writer).  Bambu's re-export moves the placement into the item for
    # every one-component object, including a plate's only object: it centres
    # the mesh on its own origin (Enzo 8652 "White 1 of 1": item = origin +
    # (22, 10, 1.6), component at 0).  The offset is 0 everywhere else, so it
    # is applied on every plate.
    items = {}
    for it in root.findall(".//c:build/c:item", ns):
        tr = [float(x) for x in (it.get("transform") or "1 0 0 0 1 0 0 0 1 0 0 0").split()]
        items[it.get("objectid")] = np.array(tr[9:12])
    n_plates = len(ms.findall("plate"))
    cols = math.ceil(math.sqrt(max(n_plates, 1)))
    enforcer_ids = {p.get("id") for p in ms.iter("part") if p.get("subtype") != "normal_part"}
    plates = []
    bad = 0
    for pl in ms.findall("plate"):
        pid = int(pl.find("metadata[@key='plater_id']").get("value"))
        name = pl.find("metadata[@key='plater_name']").get("value")
        oids = [mi.find("metadata[@key='object_id']").get("value") for mi in pl.findall("model_instance")]
        origin = np.array([((pid - 1) % cols) * bed * 1.2, -((pid - 1) // cols) * depth * 1.2, 0.0])
        comps_all = [(c, oid) for oid in oids for c in plate_comps[oid] if c[1] not in enforcer_ids]
        lo = np.full(3, 1e9)
        hi = np.full(3, -1e9)
        tris = 0
        for (path_attr, cid, tr), oid in comps_all:
            mn, mx, t = mesh[(path_attr, cid)]
            T = np.array(tr).reshape(4, 3)
            R, tv = T[:3, :3], T[3, :]
            corners = np.array([[x, y, z] for x in (mn[0], mx[0]) for y in (mn[1], mx[1]) for z in (mn[2], mx[2])])
            # 3MF Core Spec §3.3: 4x3 row-vector transform [x y z 1] * M -> corners @ R + tv
            W = corners @ R + tv
            if oid in items:
                W = W + (items[oid] - origin)
            lo = np.minimum(lo, W.min(axis=0))
            hi = np.maximum(hi, W.max(axis=0))
            tris += t
        size = hi - lo
        ok = (
            lo[0] >= -0.02 and lo[1] >= -0.02
            and hi[0] <= bed + 0.02 and hi[1] <= depth + 0.02
            and abs(lo[2]) < 0.02
        )
        if not ok:
            bad += 1
        plates.append({
            "id": pid,
            "name": name,
            "components": len(comps_all),
            "size": [float(x) for x in size],
            "min": [float(x) for x in lo],
            "triangles": tris,
            "ok": bool(ok),
        })
    return {
        "plates": plates,
        "plate_count": len(plates),
        "bad_plates": bad,
        "components": sum(p["components"] for p in plates),
        "triangles": sum(p["triangles"] for p in plates),
    }
