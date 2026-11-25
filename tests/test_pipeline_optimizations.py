"""Unit tests for Theme A pipeline optimizations:
- slice_cache: per-plate content keys, mini-3MF filtering, put/get/evict
- probe.plate_check: per-plate caching & mini-3MF re-slicing across rounds
- verify: shared G-code cache hits, batch retry fallback, feature-filtered gcode,
  vectorized loop matching, stud_rings memoization, no-op supports_off skip
- bambu_profiles / settings: persisted profile index & Composer chain memoization
- rebrickable: single-pass minifig/prefix lookup & parallel ensure_data
- pipeline / sweep: _normalise_with_bambu cleanup, sweep bambu + gcode_cache,
  corrupt report guard, merged _check_ribs + _screen_final pass, redundant 3MF write skip
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import plistlib
import subprocess
import time
import types
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from brickprint import (
    bambu_profiles as bp,
    gcode,
    pipeline,
    probe,
    rebrickable,
    settings,
    slice_cache,
    sweep,
    threemf,
    verify,
)


def _make_valid_3mf(
    path: Path,
    plates_spec: list[dict],
    *,
    enable_support: str = "0",
    enforcer_on: set[int] | None = None,
    layer_ranges: dict[int, list[tuple[float, float]]] | None = None,
    bed: float = 180.0,
    wipe_tower_x: list[str] | None = None,
    uuid_seed: str = "aaa",
) -> Path:
    """Create a realistic multi-plate 3MF matching Bambu Studio / threemf.py layout."""
    import math
    n_plates = len(plates_spec)
    cols = math.ceil(math.sqrt(max(n_plates, 1)))
    enforcer_on = enforcer_on or set()
    layer_ranges = layer_ranges or {}

    ps = {
        "enable_support": enable_support,
        "layer_height": "0.08",
        "printable_area": ["0x0", f"{bed:g}x0", f"{bed:g}x{bed:g}", f"0x{bed:g}"],
        "wipe_tower_x": wipe_tower_x if wipe_tower_x is not None else ["15"],
        "wipe_tower_y": ["220"],
    }

    res_objs = []
    build_items = []
    plate_xmls = []
    ms_objs = []
    assemble_items = []
    lcr_xmls = []
    fseq = {}
    obj_files: dict[str, str] = {}

    next_id = 1
    for pi, spec in enumerate(plates_spec, 1):
        ox = ((pi - 1) % cols) * bed * 1.2
        oy = -((pi - 1) // cols) * bed * 1.2
        oid = next_id
        next_id += 1
        mesh_id = next_id
        next_id += 1
        dz = float(spec.get("z", 5.0))
        dx = float(spec.get("x", 10.0))
        dy = float(spec.get("y", 10.0))
        model_fn = f"3D/Objects/object_{mesh_id}.model"
        obj_files[model_fn] = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">\n'
            '<resources>\n'
            f'<object id="{mesh_id}" p:UUID="{uuid_seed}-{mesh_id}" type="model"><mesh>\n'
            '<vertices>\n'
            '<vertex x="0" y="0" z="0"/><vertex x="8" y="0" z="0"/>'
            f'<vertex x="0" y="8" z="0"/><vertex x="0" y="0" z="{dz:g}"/>\n'
            '</vertices><triangles>\n'
            '<triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="1" v3="3"/>'
            '<triangle v1="0" v2="2" v3="3"/><triangle v1="1" v2="2" v3="3"/>\n'
            '</triangles></mesh></object>\n'
            '</resources></model>\n'
        )
        comps = [f'<component p:path="/{model_fn}" objectid="{mesh_id}" '
                 f'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>']
        parts_ms = [f'<part id="{mesh_id}" subtype="normal_part">'
                    f'<metadata key="name" value="{spec.get("name", f"part_{pi}")}"/></part>']
        if pi in enforcer_on:
            eid = next_id
            next_id += 1
            efn = f"3D/Objects/object_{eid}.model"
            obj_files[efn] = obj_files[model_fn].replace(f'id="{mesh_id}"', f'id="{eid}"')
            comps.append(f'<component p:path="/{efn}" objectid="{eid}" '
                         f'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
            parts_ms.append(f'<part id="{eid}" subtype="support_enforcer">'
                            f'<metadata key="name" value="enf_{pi}"/></part>')

        res_objs.append(f'<object id="{oid}" p:UUID="{uuid_seed}-{oid}" type="model">'
                        f'<components>{"".join(comps)}</components></object>')
        build_items.append(
            f'<item objectid="{oid}" p:UUID="{uuid_seed}-it-{oid}" '
            f'transform="1 0 0 0 1 0 0 0 1 {ox + dx:g} {oy + dy:g} 0" printable="1"/>'
        )
        assemble_items.append(
            f'<assemble_item object_id="{oid}" instance_id="0" '
            f'transform="1 0 0 0 1 0 0 0 1 {ox + dx:g} {oy + dy:g} 0" offset="0 0 0"/>'
        )
        extra_meta = ""
        if spec.get("support"):
            extra_meta = '<metadata key="enable_support" value="1"/>'
        ms_objs.append(
            f'<object id="{oid}"><metadata key="name" value="{spec.get("name", f"obj_{pi}")}"/>'
            f'{extra_meta}{"".join(parts_ms)}</object>'
        )
        plate_xmls.append(
            f'<plate><metadata key="plater_id" value="{pi}"/>'
            f'<metadata key="plater_name" value="Plate {pi}"/>'
            f'<metadata key="locked" value="false"/>'
            f'<model_instance><metadata key="object_id" value="{oid}"/>'
            f'<metadata key="instance_id" value="0"/>'
            f'<metadata key="identify_id" value="{oid * 10}"/></model_instance></plate>'
        )
        if pi in layer_ranges:
            rngs = "".join(
                f'<range min_z="{lo:g}" max_z="{hi:g}"><option opt_key="layer_height">0.24</option></range>'
                for lo, hi in layer_ranges[pi]
            )
            lcr_xmls.append(f'<object id="{pi}">{rngs}</object>')
        fseq[f"plate_{pi}"] = {"sequence": []}

    model_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">\n'
        f'<resources>{"".join(res_objs)}</resources>\n'
        f'<build>{"".join(build_items)}</build>\n'
        '</model>\n'
    )
    ms_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        + "\n".join(ms_objs) + "\n"
        + "\n".join(plate_xmls) + "\n"
        + "<assemble>\n" + "\n".join(assemble_items) + "\n</assemble>\n</config>\n"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", model_xml)
        for fn, body in obj_files.items():
            z.writestr(fn, body)
        z.writestr("Metadata/model_settings.config", ms_xml)
        z.writestr("Metadata/project_settings.config", json.dumps(ps))
        z.writestr("Metadata/filament_sequence.json", json.dumps(fseq))
        if lcr_xmls:
            z.writestr("Metadata/layer_config_ranges.xml",
                       '<?xml version="1.0" encoding="utf-8"?>\n<objects>\n'
                       + "\n".join(lcr_xmls) + "\n</objects>\n")
    return path


# ---------------------------------------------------------------------------
# slice_cache tests
# ---------------------------------------------------------------------------

def test_slice_cache_keys_uuid_invariant_and_filter_invariant(tmp_path):
    specs = [{"name": "a", "z": 5.0, "x": 12.3456, "y": 22.5},
             {"name": "b", "z": 8.0, "x": 30.1, "y": 40.2},
             {"name": "c", "z": 12.0, "x": 55.0, "y": 18.75},
             {"name": "d", "z": 3.2, "x": 15.0, "y": 15.0}]
    p1 = _make_valid_3mf(tmp_path / "p1.3mf", specs, uuid_seed="seed1",
                         layer_ranges={2: [(2.2, 2.44)]},
                         wipe_tower_x=["15", "160", "15", "140"])
    p2 = _make_valid_3mf(tmp_path / "p2.3mf", specs, uuid_seed="seed2",
                         layer_ranges={2: [(2.2, 2.44)]},
                         wipe_tower_x=["15", "160", "15", "140"])

    k1 = slice_cache.plate_keys(p1, "02.09.01.70")
    k2 = slice_cache.plate_keys(p2, "02.09.01.70")
    assert k1 == k2
    assert len(set(k1.values())) == 4

    # Different Bambu version or CLI flags must invalidate keys
    assert slice_cache.plate_keys(p1, "02.10.00.00") != k1
    assert slice_cache.plate_keys(p1, "02.09.01.70", {"arrange": 1}) != k1

    # Filtering to a subset [2, 4] must produce IDENTICAL keys for kept plates
    mini = tmp_path / "mini.3mf"
    mapping = slice_cache.filter_plates_3mf(p1, mini, [2, 4])
    assert mapping == {1: 2, 2: 4}
    km = slice_cache.plate_keys(mini, "02.09.01.70")
    assert km[1] == k1[2]
    assert km[2] == k1[4]

    # Filtered mini-3MF must drop unreferenced 3D/Objects/*.model files
    with zipfile.ZipFile(mini) as z:
        obj_models = [n for n in z.namelist() if n.startswith("3D/Objects/")]
        assert len(obj_models) == 2

    # Validating filtered 3MF bed coords passes with 0 bad plates
    val = threemf.validate_project(mini, 180.0, 180.0)
    assert val["plate_count"] == 2 and val["bad_plates"] == 0


def test_slice_cache_put_get_corruption_and_eviction(tmp_path):
    cache_root = tmp_path / "gcode_cache"
    g_ok = tmp_path / "plate_1.gcode"
    g_ok.write_text("; FEATURE: Outer wall\nG1 X1 Y1 E0.1\n")
    g_empty = tmp_path / "empty.gcode"
    g_empty.write_text("")

    assert slice_cache.put(cache_root, "a" * 64, g_empty, bambu_version="1.0") is None
    assert slice_cache.get(cache_root, "a" * 64) is None

    k1 = "1" * 64
    k2 = "2" * 64
    dst1 = slice_cache.put(cache_root, k1, g_ok, bambu_version="1.0",
                           warnings=["It seems object x has floating regions"],
                           source="brickprint.plate_check")
    assert dst1 is not None and dst1.is_file()
    hit = slice_cache.get(cache_root, k1)
    assert hit is not None
    assert hit["warnings"] == ["It seems object x has floating regions"]
    assert hit["gcode"].read_text() == g_ok.read_text()

    # Truncated G-code must be treated as cache miss
    dst1.write_text("short")
    assert slice_cache.get(cache_root, k1) is None

    # Restore k1 and add k2 for TTL / LRU eviction tests
    slice_cache.put(cache_root, k1, g_ok, bambu_version="1.0")
    slice_cache.put(cache_root, k2, g_ok, bambu_version="1.0")
    now = 2_000_000_000.0
    # Make k1 old (20 days), k2 recent (2 days)
    for f in slice_cache.entry_dir(cache_root, k1).iterdir():
        os.utime(f, (now - 20 * 86400, now - 20 * 86400))
    for f in slice_cache.entry_dir(cache_root, k2).iterdir():
        os.utime(f, (now - 2 * 86400, now - 2 * 86400))
    ev = slice_cache.evict(cache_root, max_gb=10.0, max_days=14.0, now=now)
    assert ev["removed"] == 1
    assert slice_cache.get(cache_root, k1) is None
    assert slice_cache.get(cache_root, k2, touch=False) is not None

    # Active-writer guard (within 60s): never evict even if cap is tiny
    for f in slice_cache.entry_dir(cache_root, k2).iterdir():
        os.utime(f, (now - 10, now - 10))
    ev2 = slice_cache.evict(cache_root, max_gb=1e-9, max_days=14.0, now=now)
    assert ev2["removed"] == 0


# ---------------------------------------------------------------------------
# Per-plate plate_check caching + shared G-code cache -> verify reuse
# ---------------------------------------------------------------------------

def test_plate_check_per_plate_cache_and_verify_reuse(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cfg = {"cache_dir": str(cache_dir), "bed": 180.0, "depth": 180.0, "margin": 6.0}

    p_round0 = _make_valid_3mf(tmp_path / "r0.3mf", [
        {"name": "p1", "z": 5.0},
        {"name": "p2", "z": 8.0},
        {"name": "p3", "z": 10.0},
    ])
    # Round 1 changes ONLY plate 2 (plates 1 and 3 are unchanged)
    p_round1 = _make_valid_3mf(tmp_path / "r1.3mf", [
        {"name": "p1", "z": 5.0},
        {"name": "p2_changed", "z": 9.5},
        {"name": "p3", "z": 10.0},
    ])

    sliced_calls = []

    def fake_slice_plates(bambu, src, outdir, ids, log=print, timeout=3600):
        Path(outdir).mkdir(parents=True, exist_ok=True)
        sliced_calls.append((Path(src).name, list(ids)))
        g = {}
        for pid in ids:
            gp = Path(outdir) / f"plate_{pid}.gcode"
            gp.write_text(
                "; CONFIG_BLOCK_START\n; enable_support = 0\n; layer_height = 0.08\n; CONFIG_BLOCK_END\n"
                "; FEATURE: Outer wall\n; LINE_WIDTH: 0.42\n"
                "G1 Z1.0\nG1 X10 Y10\nG1 X18 Y10 E0.1\nG1 X18 Y18 E0.1\nG1 X10 Y18 E0.1\nG1 X10 Y10 E0.1\n"
            )
            g[pid] = gp
        return g, "", {}

    with mock.patch.object(verify, "slice_plates", fake_slice_plates):
        res0 = probe.plate_check(cfg, "/no/bambu", p_round0, runs=2, workers=1, log=lambda *a: None)
        assert res0 == {}
        assert len(sliced_calls) == 2
        assert all(ids == [1, 2, 3] for _, ids in sliced_calls)

        # Round 1: ONLY plate 2 (mapped to mini-3MF plate [1]) must be sliced!
        sliced_calls.clear()
        res1 = probe.plate_check(cfg, "/no/bambu", p_round1, runs=2, workers=1, log=lambda *a: None)
        assert res1 == {}
        assert len(sliced_calls) == 2
        assert all(name == "uncached.3mf" and ids == [1] for name, ids in sliced_calls)

        # Now run verify.run() with shared gcode_cache on p_round1:
        # ALL 3 plates must hit the G-code cache (ZERO Bambu slice calls)!
        sliced_calls.clear()
        ver = verify.run(
            p_round1, "/no/bambu",
            workdir=tmp_path / "verify_work",
            log=lambda *a: None,
            gcode_cache=slice_cache.default_dir(cfg),
        )
        assert ver["ok"], ver["errors"]
        assert sliced_calls == []
        assert sorted(ver["plates"]) == [1, 2, 3]


# ---------------------------------------------------------------------------
# verify.slice_plates batch retry fallback
# ---------------------------------------------------------------------------

def test_verify_slice_plates_batch_fallback_on_early_conflict(tmp_path):
    p = _make_valid_3mf(tmp_path / "four.3mf", [
        {"name": "p1"}, {"name": "p2"}, {"name": "p3"}, {"name": "p4"},
    ])
    calls = []

    def fake_slice_project(bambu, src, outdir, plate=0, timeout=3600):
        Path(outdir).mkdir(parents=True, exist_ok=True)
        calls.append((Path(src).name, plate))
        if Path(src).name == "four.3mf" and plate == 0:
            # Plate 1 fails with conflict, aborting plates 2, 3, 4
            return 1, "plate 1: found slicing result conflict\n"
        if Path(src).name == "remaining.3mf" and plate == 0:
            # Batch retry slices remaining 3 plates (renumbered 1, 2, 3 -> orig 2, 3, 4)
            for pid in (1, 2, 3):
                (Path(outdir) / f"plate_{pid}.gcode").write_text("; ok\n")
            return 0, "plate 2: found slicing warnings: It seems object p3 has floating regions\n"
        raise AssertionError(f"unexpected call: {src}, {plate}")

    with mock.patch.object(verify, "slice_project", fake_slice_project):
        g, text, failed = verify.slice_plates("/no/bambu", p, tmp_path / "out", [1, 2, 3, 4],
                                              log=lambda *a: None)
    # Only 2 Bambu invocations total (initial + 1 filtered batch), NOT 1 + 3 serial!
    assert len(calls) == 2
    assert 1 in failed
    assert all(g[pid].is_file() for pid in (2, 3, 4))
    # Mini plate 2 warning must be remapped back to original plate 3!
    assert "plate 3: found slicing warnings: It seems object p3 has floating regions" in text


# ---------------------------------------------------------------------------
# gcode.paths filtering & gcode.resample fast-path
# ---------------------------------------------------------------------------

def test_gcode_paths_feature_filter_and_g_prefix_guard(tmp_path):
    gc = tmp_path / "sample.gcode"
    gc.write_text(
        "G28 ; home\n"
        "G29 X1 Y1 E1 ; bed level (must be ignored)\n"
        "; start printing object, unique label id: 7\n"
        "; LINE_WIDTH: 0.42\n"
        "; FEATURE: Sparse infill\n"
        "G1 Z0.2\n"
        "G1 X5 Y5\n"
        "G1 X15 Y5 E0.2\n"
        "G1 X15 Y15 E0.2 ; comment with X999 Y999 ignored\n"
        "; FEATURE: Outer wall\n"
        "G1 X20 Y15 E0.1\n"
        "G3 X20 Y15 I2 J0 E0.2\n"
        "; FEATURE: Inner wall\n"
        "G1 X30 Y30 E0.1\n"
        "G1 Z0.4\n"
        "; FEATURE: Bridge\n"
        "G1 X40 Y30 E0.2\n"
        "; stop printing object\n"
    )
    all_p = gcode.paths(gc)
    wanted = {"Outer wall", "Bridge"}
    filt_p = gcode.paths(gc, features=wanted)
    expected = [p for p in all_p if p["feat"] in wanted]
    assert len(filt_p) == len(expected) == 2
    for a, b in zip(filt_p, expected):
        assert (a["obj"], a["z"], a["feat"], a["lw"]) == (b["obj"], b["z"], b["feat"], b["lw"])
        np.testing.assert_allclose(a["pts"], b["pts"], atol=1e-12)
    # Verify X/Y state carried cleanly from Sparse infill (15, 15) into Outer wall start
    np.testing.assert_allclose(filt_p[0]["pts"][0], [15.0, 15.0])
    # Verify Z=0.4 and X/Y=(30, 30) carried across skipped Inner wall into Bridge
    assert filt_p[1]["z"] == pytest.approx(0.4)
    np.testing.assert_allclose(filt_p[1]["pts"][0], [30.0, 30.0])


def test_gcode_resample_fast_path_and_vectorized_exactness():
    # Short segments (< 2 * step): exact no-op
    ang = np.linspace(0, 2 * np.pi, 100)
    circle = np.column_stack([2.4 * np.cos(ang), 2.4 * np.sin(ang)])
    res = gcode.resample(circle, step=0.1)
    np.testing.assert_allclose(res, circle)

    # Long square segments: matches reference loop exactly
    sq = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 0.5], [0.0, 0.5], [0.0, 0.0]])
    got = gcode.resample(sq, step=0.1)
    # Reference loop
    seg = np.diff(sq, axis=0)
    L = np.hypot(seg[:, 0], seg[:, 1])
    ref = [sq[:1]]
    for a, d, l in zip(sq[:-1], seg, L):
        n = max(int(l / 0.1), 1)
        ref.append(a + (np.arange(1, n + 1) / n)[:, None] * d)
    np.testing.assert_allclose(got, np.vstack(ref), atol=1e-12)


def test_verify_needs_supports_off_copy_skips_zip_write(tmp_path):
    p_off = _make_valid_3mf(tmp_path / "off.3mf", [{"name": "a"}], enable_support="0")
    p_on = _make_valid_3mf(tmp_path / "on.3mf", [{"name": "a"}], enable_support="1")
    p_enf = _make_valid_3mf(tmp_path / "enf.3mf", [{"name": "a"}], enable_support="0", enforcer_on={1})
    assert verify._needs_supports_off_copy(p_off) is False
    assert verify._needs_supports_off_copy(p_on) is True
    assert verify._needs_supports_off_copy(p_enf) is True


def test_verify_analyse_plate_memoizes_stud_rings_and_vectorizes_matching(tmp_path):
    V = np.array([[0, 0, 0], [8, 0, 0], [8, 8, 0], [0, 8, 0], [0, 0, 3.2]], dtype=float)
    T = np.array([[0, 1, 2], [0, 2, 3], [0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]])
    # 5 identical instances of the same mesh at different XY translations
    objs = []
    for k in range(5):
        M = np.vstack([np.eye(3), [10.0 + k * 15.0, 10.0, 0.0]])
        objs.append({"oid": str(k + 1), "ident": k + 1, "meta": {"name": f"b #{k+1}"},
                     "parts": [{"mesh": ("/3D/Objects/m.model", "1"), "M": M,
                                "subtype": "normal_part", "rib": False}]})
    proj = {"meshes": {("/3D/Objects/m.model", "1"): (V, T)}}
    plate = {"id": 1, "name": "Red", "objects": objs}

    gc = tmp_path / "p.gcode"
    lines = ["; LINE_WIDTH: 0.42\n", "; FEATURE: Outer wall\n", "G1 Z1.0\n"]
    for k in range(5):
        x0 = 10.0 + k * 15.0 + 0.21
        y0 = 10.21
        lines.append(f"G1 X{x0} Y{y0}\nG1 X{x0+7.58} Y{y0} E0.1\nG1 X{x0+7.58} Y{y0+7.58} E0.1\n"
                     f"G1 X{x0} Y{y0+7.58} E0.1\nG1 X{x0} Y{y0} E0.1\n")
    lines.append("; FEATURE: Bridge\nG1 Z2.0\nG1 X11 Y11\nG1 X17 Y11 E0.2\n")
    gc.write_text("".join(lines))

    stud_calls = []
    orig_stud = verify.features.stud_rings

    def counted_stud(Wl, T_in):
        stud_calls.append(1)
        return orig_stud(Wl, T_in)

    stud_cache, sig_cache = {}, {}
    with mock.patch.object(verify.features, "stud_rings", counted_stud):
        an = verify.analyse_plate(proj, plate, gc, stud_cache=stud_cache, sig_cache=sig_cache)
    assert len(an["parts"]) == 5
    # 5 instances of 1 mesh -> stud_rings called ONCE!
    assert len(stud_calls) == 1
    assert any(0 in br for br in an["bridges"].values())


# ---------------------------------------------------------------------------
# bambu_profiles disk index cache & settings.Composer memoization
# ---------------------------------------------------------------------------

def test_bambu_profiles_persisted_index_and_composer_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("BRICKPRINT_CACHE", str(tmp_path / "cache"))
    app = tmp_path / "BambuStudio.app"
    exe = app / "Contents" / "MacOS" / "BambuStudio"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleShortVersionString": "09.01.00.00"}))
    bbl = app / "Contents" / "Resources" / "profiles" / "BBL"
    for kind, data in {
        "machine": {"name": "M1", "printer_model": "M", "printer_variant": "0.4"},
        "process": {"name": "P1", "layer_height": "0.2"},
        "filament": {"name": "F1", "filament_id": "GFA00", "filament_type": ["PLA"],
                     "nozzle_temperature": ["220"], "nozzle_temperature_range_low": ["190"],
                     "nozzle_temperature_range_high": ["240"], "textured_plate_temp": ["65"]},
    }.items():
        (bbl / kind).mkdir(parents=True)
        (bbl / kind / "a.json").write_text(json.dumps(data))

    bp._index.cache_clear()
    bp._resolved_chain_cached.cache_clear()
    bp._cached_app_version.cache_clear()

    idx1 = bp.index_profiles(bbl, "filament")
    assert "F1" in idx1
    # Disk index file must exist now
    idx_files = list((tmp_path / "cache" / "profile_index").glob("v1_filament_*.json"))
    assert len(idx_files) == 1

    # Clearing in-memory lru_cache must load from disk without rglob()!
    bp._index.cache_clear()
    with mock.patch.object(Path, "rglob", side_effect=AssertionError("should not walk disk on index hit")):
        idx2 = bp.index_profiles(bbl, "filament")
    assert idx2["F1"] == idx1["F1"]

    # Adding a new file updates directory mtime -> invalidates disk cache cleanly
    time.sleep(0.01)
    (bbl / "filament" / "b.json").write_text(json.dumps({"name": "F2"}))
    os.utime(bbl / "filament", (time.time() + 10, time.time() + 10))
    bp._index.cache_clear()
    idx3 = bp.index_profiles(bbl, "filament")
    assert "F2" in idx3

    # Composer deduplicates raw_chain across multiple slots & caches _base/fingerprint
    tpl = bp.build_template(app, "M1", "P1", "F1")
    tpl_path = tmp_path / "tpl.json"
    tpl_path.write_text(bp.template_text(tpl))
    cfg = {"bambu": str(exe), "settings": str(tpl_path), "set": ["layer_height=0.16"]}
    comp = settings.Composer(cfg)
    raw_calls = []
    orig_raw = bp.raw_chain

    def counted_raw(a, k, n):
        raw_calls.append((k, n))
        return orig_raw(a, k, n)

    slots = [{"color_id": str(i), "color_name": f"C{i}", "hex": "FFFFFF"} for i in range(6)]
    with mock.patch.object(bp, "raw_chain", counted_raw):
        comp.compose(slots)
        comp.compose(slots)
        fp1 = comp.fingerprint()
        fp2 = comp.fingerprint()
    assert fp1 == fp2 and fp1 != ""
    # 6 slots x 2 compose calls + fingerprint -> F1 raw_chain resolved ONCE!
    assert raw_calls == [("filament", "F1")]


# ---------------------------------------------------------------------------
# rebrickable single-pass minifig lookup
# ---------------------------------------------------------------------------

def test_rebrickable_single_pass_minifig_lookup(tmp_path):
    def write_gz(name, header, rows):
        buf = io.StringIO(newline="")
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        with gzip.open(tmp_path / name, "wt", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())

    write_gz("sets.csv.gz", ["set_num", "name", "year", "theme_id", "num_parts"],
             [["5000-1", "MultiFig", "2024", "1", "10"]])
    write_gz("inventories.csv.gz", ["id", "version", "set_num"],
             [["1", "1", "5000-1"], ["101", "1", "fig-1"], ["102", "1", "fig-2"], ["103", "1", "fig-3"]])
    write_gz("inventory_minifigs.csv.gz", ["inventory_id", "fig_num", "quantity"],
             [["1", "fig-1", "2"], ["1", "fig-2", "1"], ["1", "fig-3", "3"]])
    write_gz("inventory_parts.csv.gz", ["inventory_id", "part_num", "color_id", "quantity", "is_spare"],
             [["1", "3001", "1", "2", "False"],
              ["101", "3626", "1", "1", "False"],
              ["102", "3626", "1", "2", "False"],
              ["103", "3001", "1", "1", "False"],
              ["999", "9999", "0", "50", "False"]])
    write_gz("colors.csv.gz", ["id", "name", "rgb", "is_trans"], [["1", "Blue", "0055BF", "False"]])
    write_gz("parts.csv.gz", ["part_num", "name"], [["3001", "Brick"], ["3626", "Head"]])
    write_gz("part_relationships.csv.gz", ["rel_type", "child_part_num", "parent_part_num"], [])

    opens = []
    orig_open = gzip.open

    def counted_open(p, *a, **kw):
        opens.append(Path(p).name)
        return orig_open(p, *a, **kw)

    with mock.patch.object(rebrickable.gzip, "open", counted_open):
        res = rebrickable.lookup_set(tmp_path, "5000-1", include_minifigs=True)

    # inventory_parts.csv.gz must be opened EXACTLY ONCE (not 1 + 3 = 4 times)!
    assert opens.count("inventory_parts.csv.gz") == 1
    lots = {(r["part_num"], r["color_id"]): r["quantity"] for r in res["lots"]}
    assert lots == {("3001", "1"): 2 + 3 * 1, ("3626", "1"): 2 * 1 + 1 * 2}


# ---------------------------------------------------------------------------
# pipeline & sweep cleanup / crash guard
# ---------------------------------------------------------------------------

def test_normalise_with_bambu_cleans_temp_outdir_on_all_paths(tmp_path):
    cache = tmp_path / "cache"
    bambu = tmp_path / "BambuStudio"
    bambu.write_text("#!/bin/sh\n")
    raw = tmp_path / "a_raw.3mf"
    raw.write_bytes(b"raw")
    out = tmp_path / "a.3mf"
    cfg = {"bambu": str(bambu), "cache_dir": str(cache)}

    # 1. Success path
    def fake_ok(cmd, **kw):
        od = Path(cmd[cmd.index("--outputdir") + 1])
        (od / out.name).write_bytes(b"normalised")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with mock.patch.object(pipeline.subprocess, "run", fake_ok):
        assert pipeline._normalise_with_bambu(cfg, raw, out) is True
    assert out.read_bytes() == b"normalised"
    assert list((cache / "bambu").iterdir()) == []

    # 2. Failure path
    def fake_fail(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "err")

    with mock.patch.object(pipeline.subprocess, "run", fake_fail):
        assert pipeline._normalise_with_bambu(cfg, raw, out) is False
    assert list((cache / "bambu").iterdir()) == []

    # 3. Timeout path
    def fake_timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    with mock.patch.object(pipeline.subprocess, "run", fake_timeout):
        assert pipeline._normalise_with_bambu(cfg, raw, out) is False
    assert list((cache / "bambu").iterdir()) == []


def test_sweep_cleans_bambu_leftovers_and_gcode_cache(tmp_path):
    now = 2_000_000_000.0
    temp = tmp_path / "T"
    cache = tmp_path / "cache"
    temp.mkdir()
    cache.mkdir()

    # Stale bambu normalisation leftover (5 days old)
    stale = cache / "bambu" / "deadbeef"
    stale.mkdir(parents=True)
    f = stale / "out.3mf"
    f.write_bytes(b"x" * 2048)
    for p in (f, stale, cache / "bambu"):
        os.utime(p, (now - 5 * 86400, now - 5 * 86400))

    # Stale gcode_cache entry (20 days old)
    gsrc = tmp_path / "plate_1.gcode"
    gsrc.write_bytes(b"g" * 1024)
    gc_dir = cache / "gcode_cache"
    k = "c" * 64
    slice_cache.put(gc_dir, k, gsrc, bambu_version="1.0")
    for p in slice_cache.entry_dir(gc_dir, k).iterdir():
        os.utime(p, (now - 20 * 86400, now - 20 * 86400))

    res = sweep.sweep_workdirs({"cache_dir": str(cache)}, temp_root=temp, cache_root=cache,
                               now=now, log=lambda *a: None)
    assert res["removed"] == 2
    assert not stale.exists()
    assert slice_cache.get(gc_dir, k) is None


def test_run_verify_handles_corrupt_sibling_report_gracefully(tmp_path):
    f3mf = tmp_path / "m.3mf"
    f3mf.write_bytes(b"zip")
    rep = tmp_path / "m_report.json"
    rep.write_text("{not valid json!!!")

    seen = {}

    def fake_run_verify(cfg, path, plates, ref, tol, wd, expected, report):
        seen["expected"] = expected
        return 0

    args = types.SimpleNamespace(
        file=str(f3mf), plates=None, workdir=None, profile=None, fit=None,
        set_kv=None, expect=None, reference=None, tol=0.01, report=None,
    )
    with mock.patch.object(pipeline, "_run_verify", fake_run_verify), \
         mock.patch.object(pipeline.sweep, "maybe_sweep", lambda *a, **k: None):
        rc = pipeline.run_verify(args)
    assert rc == 0
    assert seen["expected"] == {}


def test_verify_reference_overlaps_concurrently(tmp_path):
    import threading
    p_new = _make_valid_3mf(tmp_path / "new.3mf", [{"name": "a"}], enable_support="0")
    p_ref = _make_valid_3mf(tmp_path / "ref.3mf", [{"name": "a"}], enable_support="0")
    barrier = threading.Barrier(2)
    tags = []

    def fake_slice(bambu, src, outdir, ids, log=print, timeout=3600):
        tag = Path(outdir).name
        tags.append(tag)
        barrier.wait(timeout=5)
        Path(outdir).mkdir(parents=True, exist_ok=True)
        gp = Path(outdir) / "plate_1.gcode"
        gp.write_text("; CONFIG_BLOCK_START\n; enable_support = 0\n; layer_height = 0.08\n; CONFIG_BLOCK_END\n")
        return {1: gp}, "", {}

    with mock.patch.object(verify, "slice_plates", fake_slice):
        res = verify.run(p_new, "/no/bambu", reference=p_ref, workdir=tmp_path / "w",
                         concurrent=True, log=lambda *a: None)
    assert res["ok"], res["errors"]
    assert sorted(tags) == ["new", "reference"]


def test_combined_ribs_and_screen_final_runs_one_initial_check_final(tmp_path):
    """Verify the combined check_final warm-up pattern used in pipeline.run_build:
    ribbed + non-support parts go in ONE initial check_final call, then
    _check_ribs and _screen_final hit vcache with zero extra Bambu runs for
    clean parts."""
    V = np.array([[0, 0, 0], [5, 0, 0], [0, 5, 0], [0, 0, 5]], dtype=float)
    T = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]])
    meshes = {
        "ribbed.dat": {"V": V, "T": T, "needs_support": False, "rib": (V, T)},
        "plain.dat": {"V": V, "T": T, "needs_support": False},
    }
    feats = {"ceiling_parts": {"ribbed.dat": {"ribs": 1}}}
    cfg = {"cache_dir": str(tmp_path), "bed": 180.0, "depth": 180.0, "margin": 6.0}
    vc = probe.VerdictCache(tmp_path / "meshes")
    (tmp_path / "meshes").mkdir()
    slice_batches = []

    class _Comp:
        legacy = True
        def fingerprint(self):
            return ""

    def fake_write(cfg_, info, plates, meshes_, src, verdict=False):
        slice_batches.append(len(plates))
        Path(src).write_bytes(b"zip")

    def fake_slice(bambu, src, outdir, ids, log=None, timeout=None):
        Path(outdir).mkdir(parents=True, exist_ok=True)
        return {pid: Path(outdir) / f"plate_{pid}.gcode" for pid in ids}, "", {}

    with mock.patch.object(threemf, "composer", lambda c: _Comp()), \
         mock.patch.object(threemf, "write_project", fake_write), \
         mock.patch.object(verify, "slice_plates", fake_slice):
        combined = {pf: {**m, "file": pf} for pf, m in meshes.items()
                    if not m["needs_support"] or m.get("rib") is not None}
        probe.check_final(cfg, "/no/bambu", combined, vc, runs=probe.CONFIRM_RUNS,
                          workers=1, log=lambda *a: None)
        pipeline._check_ribs(cfg, "/no/bambu", meshes, feats, vc)
        pipeline._screen_final(cfg, "/no/bambu", meshes, feats, vc)
    # Only ONE combined chunk written for both parts together; _check_ribs and
    # _screen_final hit vcache with ZERO additional check_final batches!
    assert slice_batches == [2]

