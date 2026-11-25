"""Unit tests for Enzo audit slicer/profile, pipeline brim/enforcer, and verify warning fixes."""

from __future__ import annotations

import json
import subprocess
import types
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from brickprint import pipeline, probe, settings, slice_cache, verify


def _box_mesh(dx: float, dy: float, dz: float) -> tuple[np.ndarray, np.ndarray]:
    """Closed axis-aligned rectangular prism [0..dx] x [0..dy] x [0..dz]."""
    V = np.array([
        [0, 0, 0], [dx, 0, 0], [dx, dy, 0], [0, dy, 0],
        [0, 0, dz], [dx, 0, dz], [dx, dy, dz], [0, dy, dz],
    ], dtype=float)
    T = np.array([
        [0, 2, 1], [0, 3, 2],  # bottom (z=0)
        [4, 5, 6], [4, 6, 7],  # top (z=dz)
        [0, 1, 5], [0, 5, 4],
        [2, 3, 7], [2, 7, 6],
        [0, 4, 7], [0, 7, 3],
        [1, 2, 6], [1, 6, 5],
    ], dtype=int)
    return V, T


# ---------------------------------------------------------------------------
# 1. PETG 0.08 mm process & filament profiles
# ---------------------------------------------------------------------------

def test_petg_v8_process_profile_fixes_and_fit_compat():
    prof = settings.load_profile("process", "a1mini-petg-008-lego-v8")
    ov = prof["overrides"]
    # Clutch fit --fit 0.03 compatibility preserved
    assert prof["fit"] == {"contour": 0.03, "hole": -0.03}
    # Infill, elephant foot, and unified thick_bridges=0 support recipe
    assert ov["sparse_infill_pattern"] == "gyroid"
    assert float(ov["elefant_foot_compensation"]) == pytest.approx(0.10, abs=0.02)
    assert ov["thick_bridges"] == 0
    assert float(ov["support_top_z_distance"]) == pytest.approx(0.24)
    assert 0.3 <= float(ov["support_interface_spacing"]) <= 0.4
    assert int(ov["support_interface_top_layers"]) == 3
    # Brim policy supports tall/slender parts as well as small contact
    assert prof["brim"]["mode"] == "small-contact"
    assert prof["brim"]["contact_mm2"] == 20
    assert prof["brim"]["slender_ratio"] == pytest.approx(2.5)
    assert prof["brim"]["slender_min_height_mm"] == pytest.approx(15)


def test_porima_petg_filament_profiles_cooling_and_temps():
    fils = settings.list_profiles()["filament"]
    porima = [name for name, _ in fils if name.startswith("porima-petg-")]
    assert len(porima) == 9
    for name in porima:
        fp = settings.load_profile("filament", name)
        ov = fp["overrides"]
        mf = fp["manufacturer"]
        # Overhang fan & slow-down fixes
        assert 50 <= float(ov["overhang_fan_speed"]) <= 60
        assert ov["overhang_fan_threshold"] == "25%"
        assert float(ov["slow_down_layer_time"]) == 14
        assert float(ov["slow_down_min_speed"]) == 10
        # Conservative flow & temps within manufacturer range (preserving --fit 0.03 clutch)
        assert 0.96 <= float(ov["filament_flow_ratio"]) <= 0.98
        nozzle = float(ov["nozzle_temperature"])
        assert mf["nozzle_temperature_c"][0] <= nozzle <= mf["nozzle_temperature_c"][1]
        assert 245 <= nozzle <= 250
        assert float(ov["nozzle_temperature_initial_layer"]) == nozzle
        bed = float(ov["textured_plate_temp"])
        assert mf["bed_temperature_c"][0] <= bed <= mf["bed_temperature_c"][1]
        assert bed == 75
        assert float(ov["textured_plate_temp_initial_layer"]) == 75
        assert float(ov["hot_plate_temp"]) == 75


def test_petg_v8_compose_validates_live_if_bambu_installed():
    bambu = probe.find_bambu({})
    if not bambu:
        pytest.skip("Bambu Studio not installed")
    cfg = {
        "bambu": bambu,
        "profile": "a1mini-petg-008-lego-v8",
        "fit": "0.03",
        "filaments": ["White=porima-petg-white", "Red=porima-petg-transparent-wine-red"],
    }
    comp = settings.Composer(cfg)
    S = comp.compose([
        {"color_id": "1", "color_name": "White", "hex": "FFFFFF"},
        {"color_id": "2", "color_name": "Red", "hex": "7B1E2B"},
    ])
    assert S["xy_contour_compensation"] == "0.03"
    assert S["xy_hole_compensation"] == "-0.03"
    assert S["sparse_infill_pattern"] == "gyroid"
    assert S["elefant_foot_compensation"] == "0.1"
    assert S["thick_bridges"] == "0"
    assert S["support_top_z_distance"] == "0.24"
    assert S["support_interface_spacing"] == "0.35"
    assert S["support_interface_top_layers"] == "3"
    assert S["overhang_fan_speed"] == ["55", "55"]
    assert S["overhang_fan_threshold"] == ["25%", "25%"]
    assert S["slow_down_layer_time"] == ["14", "14"]
    assert S["slow_down_min_speed"] == ["10", "10"]
    assert S["filament_flow_ratio"] == ["0.98", "0.98"]
    assert S["nozzle_temperature"] == ["245", "250"]
    assert S["textured_plate_temp"] == ["75", "75"]
    # All non-stock tuned keys appear in different_settings_to_system
    # (thick_bridges=0 matches the stock base process so it is omitted from pdiff)
    pdiff = set(S["different_settings_to_system"][0].split(";"))
    fdiff0 = set(S["different_settings_to_system"][1].split(";"))
    assert {"sparse_infill_pattern", "elefant_foot_compensation", "support_top_z_distance",
            "support_interface_spacing", "support_interface_top_layers"} <= pdiff
    assert "thick_bridges" not in pdiff
    assert {"overhang_fan_speed", "overhang_fan_threshold", "slow_down_layer_time",
            "filament_flow_ratio", "textured_plate_temp"} <= fdiff0


# ---------------------------------------------------------------------------
# 2. Pipeline: brim eligibility, brim keep-out padding, and enforcer_boxes
# ---------------------------------------------------------------------------

def test_needs_brim_catches_small_contact_and_tall_slender_parts():
    brim = {
        "mode": "small-contact",
        "contact_mm2": 20,
        "slender_ratio": 2.5,
        "slender_min_height_mm": 15,
        "width_mm": 3,
        "gap_mm": 0.2,
    }
    # Small contact (<20 mm2): 4x4x4.8 -> area 16 mm2 -> brim
    assert pipeline._needs_brim(*_box_mesh(4.0, 4.0, 4.8), brim) is True
    # Tall slender tip-over parts (matching Enzo audit: 44302a, 4460a, 30099, 4081b, 50967):
    # 44302a-like: area 22.4 mm2 (4.8 x 4.67), height 22.4 mm -> H/eqD = 4.2 -> brim
    assert pipeline._needs_brim(*_box_mesh(4.8, 4.67, 22.4), brim) is True
    # 4460a-like: area 69.1 mm2 (8.0 x 8.64), height 30.4 mm -> H/eqD = 3.24 -> brim
    assert pipeline._needs_brim(*_box_mesh(8.0, 8.64, 30.4), brim) is True
    # 30099-like: area 108 mm2 (13.5 x 8.0), height 40.0 mm -> H/eqD = 3.41 -> brim
    assert pipeline._needs_brim(*_box_mesh(13.5, 8.0, 40.0), brim) is True
    # 4081b-like: area 25.6 mm2 (8.0 x 3.2), height 15.2 mm -> H/eqD = 2.66 -> brim
    assert pipeline._needs_brim(*_box_mesh(8.0, 3.2, 15.2), brim) is True
    # 50967-like: area 23.0 mm2 (8.0 x 2.88), height 16.0 mm -> H/eqD = 2.95 -> brim
    assert pipeline._needs_brim(*_box_mesh(8.0, 2.88, 16.0), brim) is True

    # Ordinary stable LEGO bricks and plates must NOT get brims:
    # 1x1 brick (3005): area 41 mm2, height 11.2 -> H/eqD = 1.55 -> no brim
    assert pipeline._needs_brim(*_box_mesh(6.4, 6.4, 11.2), brim) is False
    # 1x2x2 brick (3245b): area 66.6 mm2, height 20.8 -> H/eqD = 2.26 -> no brim
    assert pipeline._needs_brim(*_box_mesh(8.0, 8.325, 20.8), brim) is False
    # 2x2 plate (3022): area 106 mm2, height 4.8 -> H/eqD = 0.41 -> no brim
    assert pipeline._needs_brim(*_box_mesh(10.3, 10.3, 4.8), brim) is False
    # Mode off -> never brim
    assert pipeline._needs_brim(*_box_mesh(3.0, 3.0, 30.0), {"mode": "off"}) is False


def test_object_pad_includes_brim_and_enforcer_keepout():
    comp = types.SimpleNamespace(legacy=False, ceilings_mode="recipe8", process_overrides=lambda: {})
    legacy_comp = types.SimpleNamespace(legacy=True, ceilings_mode="off", process_overrides=lambda: {})

    V, T = _box_mesh(8.0, 8.0, 16.0)
    # Brimmed part without enforcer -> brim_width + brim_object_gap = 3.2 mm
    m_brim = {
        "V": V, "T": T,
        "object_meta": {"brim_type": "outer_only", "brim_width": "3", "brim_object_gap": "0.2"},
    }
    assert pipeline._object_pad(comp, m_brim) == pytest.approx(3.2)
    assert pipeline._object_pad(legacy_comp, m_brim) == 0.0

    # Part with both brim (3.2 mm) and enforcer overhanging by 1.5 mm (pad 4.5 mm) -> max = 4.5
    EV = np.array([[0.0, 0.0, 0.0], [9.5, 8.0, 2.0]])
    m_both = {**m_brim, "enforcer": (EV, np.zeros((0, 3), dtype=int))}
    assert pipeline._object_pad(comp, m_both) == pytest.approx(4.5)

    # Part with no brim and no enforcer -> 0.0
    assert pipeline._object_pad(comp, {"V": V, "T": T}) == 0.0


def test_apply_features_stores_enforcer_boxes_for_blocker_exclusion():
    # T-shaped cantilever: narrow base [0..4]x[0..4]x[0..4], wide overhang [0..10]x[0..4]x[4..6]
    V1, T1 = _box_mesh(4.0, 4.0, 4.0)
    V2, T2 = _box_mesh(10.0, 4.0, 2.0)
    V2 = V2 + np.array([0.0, 0.0, 4.0])
    V = np.vstack([V1, V2])
    T = np.vstack([T1, T2 + len(V1)])
    meshes = {"cant.dat": {"V": V, "T": T, "size": V.max(0) - V.min(0), "needs_support": False}}

    fake_comp = types.SimpleNamespace(
        profile={"supports": {"mode": "enforcers"}, "bridging": {}, "brim": {"mode": "small-contact"}},
        supports_mode="enforcers",
        bridging_mode="stock",
        ceilings_mode="off",
        process_overrides=lambda: {},
    )
    with mock.patch.object(pipeline.threemf, "composer", lambda c: fake_comp):
        feats = pipeline._apply_features({}, meshes)
    assert "cant.dat" in feats["enforcers"]
    assert meshes["cant.dat"]["enforcer"] is not None
    assert meshes["cant.dat"]["enforcer_boxes"] == feats["enforcers"]["cant.dat"]
    assert len(meshes["cant.dat"]["enforcer_boxes"]) >= 1


# ---------------------------------------------------------------------------
# 3. Verify: Bambu Studio warning_message surfacing
# ---------------------------------------------------------------------------

def test_extract_plate_warnings_multiline_log_and_result_json(tmp_path):
    log_text = (
        "[2026-11-26 00:03:25.432] [0x1] [warning] [BambuStudio.cpp:7688 run] "
        "plate 10: found NON_CRITICAL slicing warnings: Unsupported overhangs were detected.\n"
        'Model "Silver supports 1 of 1" has 16 overhang region(s) that could not get support.\n'
        "[2026-11-26 00:03:25.500] [0x1] [info] done\n"
    )
    got = verify._extract_plate_warnings(log_text)
    assert 10 in got
    assert len(got[10]) == 1
    assert "Unsupported overhangs were detected." in got[10][0]
    assert "16 overhang region(s)" in got[10][0]

    # When log only has truncated prefix, result.json supersedes with full warning_message
    (tmp_path / "result.json").write_text(json.dumps({
        "sliced_plates": [
            {"id": 1, "warning_message": ""},
            {"id": 10, "warning_message": (
                "Unsupported overhangs were detected.\n"
                'Model "Silver supports 1 of 1" has 16 overhang region(s) that could not get support.'
            )},
        ]
    }))
    short_log = "[2026] plate 10: found NON_CRITICAL slicing warnings: Unsupported overhangs were detected.\n"
    merged = verify._extract_plate_warnings(short_log, outdir=tmp_path)
    assert len(merged[10]) == 1
    assert "16 overhang region(s)" in merged[10][0]

    # Also reads plate_*.json sidecar if present
    (tmp_path / "plate_3.json").write_text(json.dumps({"warning_message": "Thin walls detected."}))
    merged2 = verify._extract_plate_warnings("", outdir=tmp_path)
    assert merged2[3] == ["Thin walls detected."]


def test_slice_project_merges_result_json_warnings(tmp_path):
    outdir = tmp_path / "out"

    def fake_run(cmd, stdout=None, stderr=None, timeout=None):
        stdout.write(b"[2026] plate 2: found NON_CRITICAL slicing warnings: Unsupported overhangs were detected.\n")
        (outdir / "result.json").write_text(json.dumps({
            "sliced_plates": [{
                "id": 2,
                "warning_message": (
                    "Unsupported overhangs were detected.\n"
                    'Model "Silver supports 1 of 1" has 16 overhang region(s) that could not get support.'
                ),
            }]
        }))
        return subprocess.CompletedProcess(cmd, 0)

    with mock.patch.object(verify.subprocess, "run", fake_run):
        rc, text = verify.slice_project("/no/bambu", tmp_path / "x.3mf", outdir, plate=0)
    assert rc == 0
    warns = verify._extract_plate_warnings(text)
    assert len(warns[2]) == 1
    assert "16 overhang region(s)" in warns[2][0]


def test_verify_run_surfaces_bambu_unsupported_overhang_warning_and_caches(tmp_path):
    proj_path = tmp_path / "enzo.3mf"
    with zipfile.ZipFile(proj_path, "w") as z:
        z.writestr("3D/3dmodel.model", (
            '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
            '<resources><object id="1"><components>'
            '<component p:path="/3D/Objects/m.model" objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            '</components></object></resources>'
            '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 10 10 0"/></build></model>'
        ))
        z.writestr("3D/Objects/m.model", (
            '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources><object id="2"><mesh><vertices>'
            '<vertex x="0" y="0" z="0"/><vertex x="8" y="0" z="0"/>'
            '<vertex x="0" y="8" z="0"/><vertex x="0" y="0" z="5"/>'
            '</vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources></model>'
        ))
        z.writestr("Metadata/model_settings.config", (
            '<?xml version="1.0"?><config>'
            '<object id="1"><metadata key="name" value="Silver supports 1 of 1"/>'
            '<metadata key="enable_support" value="1"/>'
            '<part id="2" subtype="normal_part"><metadata key="name" value="44292"/></part></object>'
            '<plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value="Silver supports 1 of 1"/>'
            '<model_instance><metadata key="object_id" value="1"/><metadata key="identify_id" value="10"/></model_instance>'
            '</plate></config>'
        ))
        z.writestr("Metadata/project_settings.config", json.dumps({"enable_support": "0", "layer_height": "0.08"}))
        z.writestr("Metadata/plate_1.png", b"png")

    gcache = tmp_path / "gcache"
    overhang_log = (
        "[2026-11-26 00:03:25] [warning] plate 1: found NON_CRITICAL slicing warnings: "
        "Unsupported overhangs were detected.\n"
        'Model "Silver supports 1 of 1" has 16 overhang region(s) that could not get support.\n'
    )
    calls = []

    def fake_slice_plates(bambu, src, outdir, ids, log=print, timeout=3600):
        calls.append(list(ids))
        Path(outdir).mkdir(parents=True, exist_ok=True)
        gp = Path(outdir) / "plate_1.gcode"
        gp.write_text("; CONFIG_BLOCK_START\n; enable_support = 0\n; layer_height = 0.08\n; CONFIG_BLOCK_END\n")
        return {1: gp}, overhang_log, {}

    an = {"parts": [], "support": {"paths": 0, "outside_enforcers": 0, "inside_bodies": 0,
                                   "enforcers": 0, "per_object_supports": 1}, "bridges": {}}
    with mock.patch.object(verify, "slice_plates", fake_slice_plates), \
         mock.patch.object(verify, "analyse_plate", lambda proj, plate, g: an):
        # 1. Cold run: slices and surfaces warning in verify.json warnings + per-plate slicer_warnings
        res1 = verify.run(proj_path, "/no/bambu", workdir=tmp_path / "w1",
                          log=lambda *a: None, gcode_cache=gcache)
        assert res1["ok"] is True
        assert any("Unsupported overhangs were detected" in w and "16 overhang region(s)" in w
                   for w in res1["warnings"]), res1["warnings"]
        assert res1["plates"][1]["slicer_warnings"]

        # 2. Warm cache hit: ZERO new slices, warning STILL surfaced from slice_cache!
        calls.clear()
        res2 = verify.run(proj_path, "/no/bambu", workdir=tmp_path / "w2",
                          log=lambda *a: None, gcode_cache=gcache)
        assert calls == []
        assert res2["ok"] is True
        assert any("Unsupported overhangs were detected" in w and "16 overhang region(s)" in w
                   for w in res2["warnings"]), res2["warnings"]


# ---------------------------------------------------------------------------
# 4. slice_cache.filter_plates_3mf (.rels pruning + build-seq layer ranges)
#    and probe.plate_check (3 runs, never cache slicer errors)
# ---------------------------------------------------------------------------

def test_filter_plates_3mf_prunes_rels_and_remaps_layer_config_ranges(tmp_path):
    import xml.etree.ElementTree as ET

    src = tmp_path / "three_plates.3mf"
    # Non-sequential resource object IDs (10, 20, 30) while layer_config_ranges.xml
    # uses 1-based <build><item> sequence indices (1, 2, 3), matching threemf.py & Bambu.
    res_objs, build_items, ms_objs, plate_xmls, rels_xml = [], [], [], [], []
    obj_files = {}
    for pi, oid in ((1, 10), (2, 20), (3, 30)):
        mid = oid + 1
        mpath = f"3D/Objects/plate_{pi:02d}.model"
        obj_files[mpath] = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            f'<resources><object id="{mid}" type="model"><mesh>'
            '<vertices><vertex x="0" y="0" z="0"/><vertex x="8" y="0" z="0"/>'
            f'<vertex x="0" y="8" z="0"/><vertex x="0" y="0" z="{pi * 3}"/></vertices>'
            '<triangles><triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="1" v3="3"/>'
            '<triangle v1="0" v2="2" v3="3"/><triangle v1="1" v2="2" v3="3"/></triangles>'
            '</mesh></object></resources></model>'
        )
        rels_xml.append(
            f' <Relationship Target="/{mpath}" Id="rel-{pi}" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        )
        ox, oy = slice_cache._plate_origin(pi, 3, 180.0, 180.0)
        res_objs.append(
            f'<object id="{oid}" type="model"><components>'
            f'<component p:path="/{mpath}" objectid="{mid}" transform="1 0 0 0 1 0 0 0 1 10 10 0"/>'
            '</components></object>'
        )
        build_items.append(
            f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {ox:g} {oy:g} 0" printable="1"/>'
        )
        ms_objs.append(
            f'<object id="{oid}"><metadata key="name" value="p{pi}"/>'
            f'<part id="{mid}" subtype="normal_part"><metadata key="name" value="part{pi}"/></part></object>'
        )
        plate_xmls.append(
            f'<plate><metadata key="plater_id" value="{pi}"/><metadata key="plater_name" value="P{pi}"/>'
            f'<model_instance><metadata key="object_id" value="{oid}"/>'
            f'<metadata key="identify_id" value="{pi * 100}"/></model_instance></plate>'
        )

    lcr = (
        '<?xml version="1.0" encoding="utf-8"?>\n<objects>\n'
        ' <object id="1"><range min_z="1.0" max_z="1.24"><option opt_key="layer_height">0.24</option></range></object>\n'
        ' <object id="2"><range min_z="2.2" max_z="2.44"><option opt_key="layer_height">0.24</option></range></object>\n'
        ' <object id="3"><range min_z="5.0" max_z="5.24"><option opt_key="wall_loops">2</option></range></object>\n'
        '</objects>\n'
    )
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("3D/3dmodel.model", (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
            f'<resources>{"".join(res_objs)}</resources>'
            f'<build>{"".join(build_items)}</build></model>'
        ))
        z.writestr("3D/_rels/3dmodel.model.rels", (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            + "\n".join(rels_xml) + "\n</Relationships>\n"
        ))
        for fn, body in obj_files.items():
            z.writestr(fn, body)
        z.writestr("Metadata/model_settings.config", (
            '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
            + "\n".join(ms_objs) + "\n" + "\n".join(plate_xmls) + "\n</config>\n"
        ))
        z.writestr("Metadata/project_settings.config", json.dumps({"printable_area": ["0x0", "180x0", "180x180", "0x180"]}))
        z.writestr("Metadata/layer_config_ranges.xml", lcr)

    k_orig = slice_cache.plate_keys(src, "02.09.01.70")
    mini = tmp_path / "mini.3mf"
    mapping = slice_cache.filter_plates_3mf(src, mini, [2, 3])
    assert mapping == {1: 2, 2: 3}

    # 1. 3D/_rels/3dmodel.model.rels must only reference kept 3D/Objects/*.model files
    with zipfile.ZipFile(mini) as z:
        rels_root = ET.fromstring(z.read("3D/_rels/3dmodel.model.rels"))
        targets = sorted(el.get("Target") for el in rels_root)
        assert targets == ["/3D/Objects/plate_02.model", "/3D/Objects/plate_03.model"]
        # Every target in rels exists inside the filtered archive
        names = set(z.namelist())
        for t in targets:
            assert t.lstrip("/") in names

        # 2. layer_config_ranges.xml must drop seq 1 and remap seq 2->1, 3->2
        lcr_root = ET.fromstring(z.read("Metadata/layer_config_ranges.xml"))
        remapped = {
            o.get("id"): [(r.get("min_z"), r.get("max_z")) for r in o.findall("range")]
            for o in lcr_root.findall("object")
        }
        assert remapped == {"1": [("2.2", "2.44")], "2": [("5.0", "5.24")]}

    # 3. plate_keys on filtered mini-3MF must match original plates 2 and 3 identically
    k_mini = slice_cache.plate_keys(mini, "02.09.01.70")
    assert k_mini[1] == k_orig[2]
    assert k_mini[2] == k_orig[3]


def test_plate_check_runs_3_and_never_caches_slicer_errors(tmp_path):
    assert probe.PLATE_CHECK_RUNS == 3
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cfg = {"cache_dir": str(cache_dir), "bed": 180.0, "depth": 180.0, "margin": 6.0}

    p = tmp_path / "one.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", (
            '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
            '<resources><object id="1"><components>'
            '<component p:path="/3D/Objects/m.model" objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            '</components></object></resources>'
            '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 10 10 0"/></build></model>'
        ))
        z.writestr("3D/Objects/m.model", (
            '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources><object id="2"><mesh><vertices>'
            '<vertex x="0" y="0" z="0"/><vertex x="8" y="0" z="0"/>'
            '<vertex x="0" y="8" z="0"/><vertex x="0" y="0" z="5"/>'
            '</vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources></model>'
        ))
        z.writestr("Metadata/model_settings.config", (
            '<?xml version="1.0"?><config><object id="1"><metadata key="name" value="obj"/>'
            '<part id="2" subtype="normal_part"/></object>'
            '<plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value="P1"/>'
            '<model_instance><metadata key="object_id" value="1"/><metadata key="identify_id" value="10"/></model_instance>'
            '</plate></config>'
        ))
        z.writestr("Metadata/project_settings.config", json.dumps({"enable_support": "0"}))

    calls = []

    def fail_then_pass(bambu, src, outdir, ids, log=print, timeout=3600):
        calls.append(len(calls) + 1)
        Path(outdir).mkdir(parents=True, exist_ok=True)
        if len(calls) <= 3:
            return {}, "", {1: "Bambu exited 250"}
        gp = Path(outdir) / "plate_1.gcode"
        gp.write_text("; ok\n")
        return {1: gp}, "", {}

    with mock.patch.object(verify, "slice_plates", fail_then_pass):
        res1 = probe.plate_check(cfg, "/no/bambu", p, workers=1, log=lambda *a: None)
        assert "errors" in res1 and 1 in res1["errors"]
        # Failed plate must NOT be persisted in plate_checks.json
        known = json.loads((cache_dir / "plate_checks.json").read_text())
        assert known == {}
        # Second call retries cleanly and succeeds
        res2 = probe.plate_check(cfg, "/no/bambu", p, workers=1, log=lambda *a: None)
        assert res2 == {}
        assert len(calls) == 6

