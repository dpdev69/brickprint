"""Unit tests for core geometry, packing, repair, LDraw, ceiling, feature, and 3MF optimizations."""

from __future__ import annotations

import zipfile
from unittest import mock

import numpy as np
import pytest

from brickprint import ceilings, features, ldraw, orient, pack, quality, repair, threemf


def _box(x0, y0, z0, x1, y1, z1):
    """Closed, outward-facing axis-aligned box."""
    V = np.array(
        [[x, y, z] for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)],
        dtype=np.float64,
    )
    T = np.array(
        [
            [0, 2, 3], [0, 3, 1],  # bottom (-z)
            [4, 5, 7], [4, 7, 6],  # top (+z)
            [0, 1, 5], [0, 5, 4],  # front (-y)
            [2, 6, 7], [2, 7, 3],  # back (+y)
            [0, 4, 6], [0, 6, 2],  # left (-x)
            [1, 3, 7], [1, 7, 5],  # right (+x)
        ],
        dtype=np.int64,
    )
    return V, T


def _merge(*meshes):
    Vs, Ts, off = [], [], 0
    for V, T in meshes:
        Vs.append(V)
        Ts.append(T + off)
        off += len(V)
    return np.vstack(Vs), np.vstack(Ts)


# ------------------------------------------------------------------ pack.py

def test_pack_fold_preserves_pad_keepout():
    colors = {"1": {"name": "Blue", "rgb": "0055BF", "is_trans": False}}
    insts = [
        {"part_file": "n1.dat", "color_id": "1", "size": (10.0, 10.0, 5.0), "count": 1},
        {"part_file": "s1.dat", "color_id": "1", "size": (10.0, 10.0, 5.0), "count": 1,
         "needs_support": True, "pad": 8.0},
    ]
    plates, oversized, _ = pack.pack(insts, colors, bed=180.0, depth=180.0, margin=5.0,
                                     gap=3.0, max_plates=1, mix="off")
    assert oversized == []
    assert len(plates) == 1
    assert plates[0]["support"] == "folded"
    by_name = {it[0]: it for it in plates[0]["items"]}
    _, nx, ny, nw, nd = by_name["n1.dat"]
    _, sx, sy, sw, sd = by_name["s1.dat"]
    sep = max(sx - (nx + nw), nx - (sx + sw), sy - (ny + nd), ny - (sy + sd))
    assert sep >= 3.0 + 8.0 - 1e-6


def test_pack_mix_then_fold_does_not_duplicate_mixed_support_parts():
    colors = {
        "1": {"name": "Blue", "rgb": "0055BF", "is_trans": False},
        "2": {"name": "Red", "rgb": "C91A09", "is_trans": False},
    }
    insts = [
        {"part_file": "n1.dat", "color_id": "1", "size": (4.0, 4.0, 5.0), "count": 1},
        {"part_file": "s1_big.dat", "color_id": "1", "size": (70.0, 70.0, 5.0), "count": 4,
         "needs_support": True, "pad": 8.0},
        {"part_file": "s1_tiny.dat", "color_id": "1", "size": (10.0, 10.0, 5.0), "count": 1,
         "needs_support": True, "pad": 8.0},
        {"part_file": "s2_tiny.dat", "color_id": "2", "size": (10.0, 10.0, 5.0), "count": 1,
         "needs_support": True, "pad": 8.0},
    ]
    plates, oversized, _ = pack.pack(insts, colors, bed=180.0, depth=180.0, margin=5.0,
                                     gap=3.0, max_plates=2, mix="auto")
    assert oversized == []
    counts: dict[str, int] = {}
    for p in plates:
        for name, *_ in p["items"]:
            counts[name] = counts.get(name, 0) + 1
    assert counts == {"n1.dat": 1, "s1_big.dat": 4, "s1_tiny.dat": 1, "s2_tiny.dat": 1}


def test_pack_bin_split_dense_heterogeneous_valid():
    import random
    rng = random.Random(123)
    colors = {"1": {"name": "Blue", "rgb": "0055BF", "is_trans": False}}
    insts = [
        {"part_file": f"h{i}.dat", "color_id": "1",
         "size": (rng.uniform(4.0, 22.0), rng.uniform(4.0, 22.0), 3.2), "count": 1}
        for i in range(120)
    ]
    plates, oversized, _ = pack.pack(insts, colors, bed=256.0, depth=256.0, margin=5.0,
                                     gap=3.0, mix="off")
    assert oversized == []
    assert sum(len(p["items"]) for p in plates) == 120
    for p in plates:
        items = p["items"]
        for i, (_, ax, ay, aw, ad) in enumerate(items):
            assert ax >= -1e-6 and ay >= -1e-6
            assert ax + aw <= 246.0 + 1e-6 and ay + ad <= 246.0 + 1e-6
            for _, bx, by, bw, bd in items[i + 1:]:
                sep = max(bx - (ax + aw), ax - (bx + bw), by - (ay + ad), ay - (by + bd))
                assert sep >= 3.0 - 1e-6


# ------------------------------------------------------------------ orient.py

def test_orient_streaming_matches_full_slice_stack_and_layer_report():
    # Hollow bridge + floating island + empty gap
    V, T = _merge(
        _box(0, 0, 0, 12, 2, 4),
        _box(0, 8, 0, 12, 10, 4),
        _box(0, 0, 4, 12, 10, 6),
        _box(16, 0, 4, 20, 4, 6),  # floating island at z=4..6
    )
    for up in orient.AXIS_NAMES:
        R = next(M for M in orient.ROTATIONS if orient.up_axis(M) == up)
        W = V @ R.T
        W -= W.min(axis=0)
        mask, px = orient.slice_stack(W, T)
        rep_full = orient.layer_report(mask, px)
        rep_stream, px_stream = orient._stream_layer_report(W, T.astype(np.int64))
        assert px_stream == pytest.approx(px)
        assert rep_stream == rep_full


def test_orient_rle_label_layers_respects_4_connectivity():
    mask = np.zeros((7, 7, 3), dtype=bool)
    mask[0:2, 0:2, 0] = True
    mask[2:4, 2:4, 0] = True     # touches diagonally at (1,1)-(2,2): distinct 4-component
    mask[0:4, 0:4, 2] = True     # layer 1 empty, layer 2 one component
    lab = orient._label_layers(mask)
    ids0 = set(np.unique(lab[:, :, 0])) - {0}
    ids1 = set(np.unique(lab[:, :, 1])) - {0}
    ids2 = set(np.unique(lab[:, :, 2])) - {0}
    assert len(ids0) == 2
    assert len(ids1) == 0
    assert len(ids2) == 1
    assert ids0.isdisjoint(ids2)
    assert (lab > 0).sum() == mask.sum()


# ------------------------------------------------------------------ repair.py

def test_repair_tjunction_split_and_metrics_flag():
    # Two adjacent triangles sharing a T-junction at (5, 0, 0) along edge (0,0,0)->(10,0,0)
    V = np.array([
        [0.0, 0.0, 0.0],
        [10.0, 0.0, 0.0],
        [5.0, 10.0, 0.0],
        [5.0, 0.0, 0.0],   # T-junction vertex lying strictly on (0->1)
        [0.0, -5.0, 0.0],
    ], dtype=np.float64)
    T = np.array([
        [0, 1, 2],         # bottom edge 0->1 has vertex 3 in the middle
        [3, 0, 4],         # touches vertex 3 so 3 is a boundary vertex
    ], dtype=np.int64)
    V1, T1, rep1 = repair.repair_mesh(V, T)
    V2, T2, rep2 = repair.repair_mesh(V, T, metrics=False)
    assert rep1["tjunction_splits"] == 1
    assert rep2["tjunction_splits"] == 1
    assert rep1["before"] is not None and rep1["after"] is not None
    assert rep2["before"] is None and rep2["after"] is None
    np.testing.assert_array_equal(V1, V2)
    np.testing.assert_array_equal(T1, T2)
    assert len(T1) == 3


def test_repair_merge_dedup_and_closed_shell_flip():
    # Build one inverted closed box + one valid closed box with duplicate & flap faces
    V1, T1 = _box(0, 0, 0, 4, 4, 4)
    T1_inv = T1[:, [0, 2, 1]]  # inverted closed shell (negative volume)
    V2, T2 = _box(10, 0, 0, 14, 4, 4)
    # Add a near-duplicate vertex within MERGE_TOL/2 and a duplicate face + flap pair
    V2_extra = np.vstack([V2, V2[0] + [0.002, 0.002, 0.002]])
    T2_extra = np.vstack([
        T2,
        T2[:1],                 # exact duplicate -> remove 1
        [[0, 1, 7], [0, 7, 1]], # back-to-back flap -> remove both
    ])
    V, T = _merge((V1, T1_inv), (V2_extra, T2_extra))
    Vr, Tr, rep = repair.repair_mesh(V, T)
    assert rep["merged_vertices"] == 1
    assert rep["duplicate_faces_removed"] == 1
    assert rep["backtoback_pairs_removed"] == 1
    assert rep["shells_flipped"] == 1
    assert rep["after"]["volume"] == pytest.approx(128.0, abs=1e-3)


# ------------------------------------------------------------------ ldraw.py

def test_ldraw_library_scandir_precedence(tmp_path):
    (tmp_path / "LDConfig.ldr").write_text("0 LDConfig\n")
    for rel in (
        "parts/3001.dat",
        "parts/s/sub1.dat",
        "parts/s/shared.dat",
        "p/shared.dat",
        "p/prim1.dat",
        "p/48/cyl.dat",
        "p/8/cyl.dat",
    ):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("0 test\n")
    lib = ldraw.LDrawLibrary(tmp_path)
    assert lib.find("3001") == tmp_path / "parts/3001.dat"
    assert lib.find("s/sub1.dat") == tmp_path / "parts/s/sub1.dat"
    assert lib.find("sub1") == tmp_path / "parts/s/sub1.dat"
    # parts/s beats p for unqualified name (matching original 5-loop order)
    assert lib.find("shared.dat") == tmp_path / "parts/s/shared.dat"
    assert lib.find("prim1") == tmp_path / "p/prim1.dat"
    # p/48 beats p/8 for unqualified name
    assert lib.find("cyl.dat") == tmp_path / "p/48/cyl.dat"
    assert lib.find("8/cyl.dat") == tmp_path / "p/8/cyl.dat"
    assert lib.files["3001.dat"] == tmp_path / "parts/3001.dat"


def test_mesh_cache_reuses_repaired_from_export_and_keeps_baseline_check(tmp_path):
    class StubCache(ldraw.MeshCache):
        def __init__(self, cfg, open_mesh: bool):
            super().__init__(cfg)
            self.open_mesh = open_mesh
            self.ldview_calls = []

        def _ldview(self, part_file, geometry):
            self.ldview_calls.append(geometry)
            V, T = _box(0, 0, 0, 8, 8, 4)
            if self.open_mesh:
                T = T[2:]  # drop bottom face -> open loop (d[1] > 0)
            return V, T

    # Closed mesh: repair_mesh called once inside _export, NOT re-run in get()
    c_closed = StubCache({"cache_dir": str(tmp_path / "c1"), "ldraw_dir": "/none", "ldview": ""}, False)
    with mock.patch.object(repair, "repair_mesh", wraps=repair.repair_mesh) as spy:
        V, T = c_closed.get("closed.dat")
        assert spy.call_count == 1
        assert len(c_closed.ldview_calls) == 1
        assert V.dtype == np.float32 and T.dtype == np.int32

    # Open mesh: MUST still run LDVIEW_BASELINE comparison (mandatory exclusion!)
    c_open = StubCache({"cache_dir": str(tmp_path / "c2"), "ldraw_dir": "/none", "ldview": ""}, True)
    with mock.patch.object(repair, "repair_mesh", wraps=repair.repair_mesh) as spy:
        c_open.get("open.dat")
        assert c_open.ldview_calls == [ldraw.LDVIEW_GEOMETRY, ldraw.LDVIEW_BASELINE]
        # 2 repairs total (quality + baseline), NOT 3 (get() reused quality's repaired mesh)
        assert spy.call_count == 2


# ------------------------------------------------------------------ ceilings.py

def _make_hollow_brick(w=24.0, d=16.0, wall=1.5, ceil_z=1.6, top_z=3.2):
    return _merge(
        _box(0, 0, 0, w, wall, ceil_z),
        _box(0, d - wall, 0, w, d, ceil_z),
        _box(0, wall, 0, wall, d - wall, ceil_z),
        _box(w - wall, wall, 0, w, d - wall, ceil_z),
        _box(0, 0, ceil_z, w, d, top_z),
    )


def test_ceilings_narrow_part_empty_studs_does_not_crash():
    # Narrow 6 mm x 24 mm channel -> body_box has hx=0 -> studs=[], cands=[-8, 0, 8]
    V, T = _make_hollow_brick(w=6.0, d=24.0, wall=1.0, ceil_z=2.4, top_z=4.0)
    res = ceilings.plan(V, T, bridge_angle=90.0, cache_dir=False)
    assert res["raise"]["status"] == "unchanged"
    assert len(res["report"]["ceilings"]) == 1


def test_ceilings_plan_disk_cache_roundtrip_and_invalidation(tmp_path):
    V, T = _make_hollow_brick(w=24.0, d=16.0, wall=1.5, ceil_z=1.6, top_z=3.2)
    p1 = ceilings.plan(V, T, bridge_angle=180.0, cache_dir=tmp_path, part_file="brick.dat")
    assert (tmp_path / f"brick.dat.ceil{ceilings.CEILINGS_VERSION}.json").is_file()

    # Cache hit must not re-run analyse()
    with mock.patch.object(ceilings, "analyse", side_effect=AssertionError("should hit cache")):
        p2 = ceilings.plan(V, T, bridge_angle=180.0, cache_dir=tmp_path, part_file="brick.dat")
    assert p2["raise"] == p1["raise"]
    assert p2["bands"] == p1["bands"]
    assert p2["ribs"] == p1["ribs"]
    assert p2["report"] == p1["report"]
    np.testing.assert_allclose(np.asarray(p2["V"]), np.asarray(p1["V"]), atol=1e-12)

    # Different bridge_angle must miss cache and recompute
    p3 = ceilings.plan(V, T, bridge_angle=90.0, cache_dir=tmp_path, part_file="brick.dat")
    assert p3["report"]["bridge_angle"] == 90.0


def test_ceilings_inscribed_radius_matches_square_cavity():
    # 10x10 horizontal downward square at z=2.0 -> inscribed radius ~5.0 mm
    V = np.array([[0, 0, 2], [10, 0, 2], [10, 10, 2], [0, 10, 2]], dtype=np.float64)
    T = np.array([[0, 2, 1], [0, 3, 2]], dtype=np.int64)
    r, area = ceilings.inscribed_radius(V, T, [0, 1], step=0.2)
    assert r == pytest.approx(5.0, abs=0.3)
    assert area == pytest.approx(100.0, rel=0.05)


def test_ceilings_analyse_zero_grid_samples_falls_back_to_centroids():
    # Minimal mesh whose downward ceiling strip is only 0.05 mm wide (area 2.5 mm^2 >= MIN_AREA)
    # so the 0.2 mm sample grid (x = 10.0 + 0.1 > 10.05) gets 0 grid hits; centroid fallback
    # must detect the thin top skin at z=2.4 (newz=2.2 -> skin=0.2 < MIN_SKIN) and refuse.
    V2 = np.array([
        # bed footprint [0..20] x [0..60] at z=0
        [0, 0, 0], [20, 0, 0], [20, 60, 0], [0, 60, 0],
        # narrow ceiling strip x in [10.0, 10.05], y in [5, 55] at z=1.6 (downward)
        [10.0, 5.0, 1.6], [10.05, 5.0, 1.6], [10.05, 55.0, 1.6], [10.0, 55.0, 1.6],
        # top skin above it at z=2.4 (upward -> skin = 2.4 - 2.2 = 0.2 < 0.8!)
        [9.0, 4.0, 2.4], [11.0, 4.0, 2.4], [11.0, 56.0, 2.4], [9.0, 56.0, 2.4],
    ], dtype=np.float64)
    T2 = np.array([
        [0, 2, 1], [0, 3, 2],       # bottom
        [4, 6, 5], [4, 7, 6],       # downward ceiling at z=1.6 (area = 2.5 mm^2)
        [8, 9, 10], [8, 10, 11],    # upward top skin at z=2.4
    ], dtype=np.int64)
    # Force step > 0.05 so grid sample x = 10.0 + step/2 > 10.05 misses the strip!
    rep = ceilings.analyse([tuple(r) for r in V2.tolist()], [tuple(r) for r in T2.tolist()])
    assert rep["status"] == "refused"
    assert rep["skin_samples"] >= 2
    assert rep["skin"] == pytest.approx(0.2, abs=1e-3)


# ------------------------------------------------------------------ features.py

def test_socket_blockers_covers_cavity_and_avoids_external_air():
    # Hollow 16x16 brick with a cantilever ledge sticking out on +X at z=3..5
    V, T = _merge(
        _make_hollow_brick(w=16.0, d=16.0, wall=1.6, ceil_z=3.0, top_z=5.0),
        _box(16.0, 4.0, 3.0, 22.0, 12.0, 5.0),  # external cantilever
    )
    boxes = features.socket_blockers(V, T)
    assert 1 <= len(boxes) <= 4
    # Must cover center of cavity (x=8, y=8, z=1.5) and NEVER cover external cantilever (x=19, y=8, z=1.5)
    assert any(b["box"][0] <= 8.0 <= b["box"][1] and b["box"][2] <= 8.0 <= b["box"][3]
               and b["box"][4] <= 1.5 <= b["box"][5] for b in boxes)
    assert not any(b["box"][0] < 19.0 < b["box"][1] and b["box"][2] < 8.0 < b["box"][3]
                   and b["box"][4] < 1.5 < b["box"][5] for b in boxes)


# ------------------------------------------------------------------ threemf.py

def test_threemf_validate_project_row_vector_rotation(tmp_path):
    # Box [0..10]x[0..10]x[0..5], rotated +90 deg CCW around Z in 3MF row-vector
    # format: [x' y' z' 1] = [x y z 1] * M -> x' = -y + 15 in [5, 15], y' = x + 5 in [5, 15].
    p3 = tmp_path / "rot.3mf"
    V, T = _box(0, 0, 0, 10, 10, 5)
    obj_xml = threemf._plate_model([threemf._mesh_object(1, V, T)])
    root_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="millimeter" xml:lang="en-US" {threemf.NS}>'
        f'<resources><object id="2" type="model"><components>'
        f'<component p:path="/3D/Objects/plate_01.model" objectid="1" transform="0 1 0 -1 0 0 0 0 1 15 5 0"/>'
        f'</components></object></resources>'
        f'<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/></build>'
        f'</model>'
    )
    ms_xml = (
        '<?xml version="1.0" encoding="UTF-8"?><config>'
        '<object id="2"><metadata key="name" value="rot"/></object>'
        '<plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value="P1"/>'
        '<model_instance><metadata key="object_id" value="2"/><metadata key="instance_id" value="0"/>'
        '<metadata key="identify_id" value="1000"/></model_instance>'
        '</plate></config>'
    )
    with zipfile.ZipFile(p3, "w") as z:
        z.writestr("3D/Objects/plate_01.model", obj_xml)
        z.writestr("3D/3dmodel.model", root_xml)
        z.writestr("Metadata/model_settings.config", ms_xml)
    res = threemf.validate_project(p3, 180.0, 180.0)
    assert res["bad_plates"] == 0
    assert res["plates"][0]["ok"] is True
    assert res["plates"][0]["min"] == pytest.approx([5.0, 5.0, 0.0], abs=1e-4)


def test_threemf_write_project_caches_mesh_and_keeps_blockers_in_verdict(tmp_path):
    from brickprint import config
    cfg = config.load_config()
    cfg["_composer"] = None
    cfg["support_set"] = ["support_top_z_distance=0.4"]  # non-legacy -> blockers enabled
    V, T = _make_hollow_brick(w=16.0, d=16.0, wall=1.6, ceil_z=3.0, top_z=5.0)
    meshes = {"b.dat": {"V": V.astype(np.float32), "T": T.astype(np.int32)}}
    plates = [
        {"color_id": "1", "color_name": "Blue", "rgb": "0055BF", "is_trans": False,
         "index": 1, "count": 1, "kind": "supports ", "support": "plate",
         "items": [("b.dat", 10.0, 10.0, 16.0, 16.0)], "bbox": (26.0, 26.0)},
        {"color_id": "2", "color_name": "Red", "rgb": "C91A09", "is_trans": False,
         "index": 1, "count": 1, "kind": "supports ", "support": "plate",
         "items": [("b.dat", 10.0, 10.0, 16.0, 16.0)], "bbox": (26.0, 26.0)},
    ]
    out_v = tmp_path / "verdict.3mf"
    with mock.patch.object(threemf, "_mesh_body", wraps=threemf._mesh_body) as spy:
        threemf.write_project(cfg, {"set_num": "test", "name": "test"}, plates, meshes, out_v, verdict=True)
        # 1 part mesh + 1 blocker mesh formatted ONCE across 2 plates (2 calls, not 4)
        assert spy.call_count == 2
    with zipfile.ZipFile(out_v) as z:
        ms = z.read("Metadata/model_settings.config").decode()
        # Mandatory exclusion: blockers MUST still be present in verdict=True
        assert 'subtype="support_blocker"' in ms
    res = threemf.validate_project(out_v, float(cfg["bed"]), float(cfg["depth"]))
    assert res["bad_plates"] == 0


# ------------------------------------------------------------------ quality.py

def test_quality_report_reuses_in_memory_meshes():
    V, T = _box(0, 0, 0, 8, 8, 3.2)

    class StubCache:
        def get(self, pf):
            raise AssertionError("should not reload from disk")

        def export_info(self, pf):
            return {"variant": "synthetic"}

    rows = quality.report(StubCache(), ["3024.dat"], meshes={"3024.dat": {"V": V, "T": T}})
    assert len(rows) == 1
    assert rows[0]["triangles"] == 12
    assert rows[0]["footprint_on_8mm_grid"] is True
