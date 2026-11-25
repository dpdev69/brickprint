"""Regression tests for Enzo geometry, blocker, enforcer, ceiling-band, and brim-packing fixes."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from unittest import mock

import numpy as np
import pytest

from brickprint import config, features, pack, settings, threemf


def _box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float):
    V = np.array(
        [[x, y, z] for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)],
        dtype=np.float64,
    )
    T = np.array(
        [
            [0, 2, 3], [0, 3, 1],
            [4, 5, 7], [4, 7, 6],
            [0, 1, 5], [0, 5, 4],
            [2, 6, 7], [2, 7, 3],
            [0, 4, 6], [0, 6, 2],
            [1, 3, 7], [1, 7, 5],
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


def _hollow_box(x0, y0, z0, x1, y1, z1, wall=1.5, ceil_t=1.2):
    """Open-bottom hollow box from z0..z1 with walls of thickness ``wall`` and roof ``ceil_t``."""
    cz = z1 - ceil_t
    return _merge(
        _box(x0, y0, z0, x1, y0 + wall, cz),
        _box(x0, y1 - wall, z0, x1, y1, cz),
        _box(x0, y0 + wall, z0, x0 + wall, y1 - wall, cz),
        _box(x1 - wall, y0 + wall, z0, x1, y1 - wall, cz),
        _box(x0, y0, cz, x1, y1, z1),
    )


def _boxes_overlap(a, b, eps=1e-6) -> bool:
    return (
        a[0] < b[1] - eps and b[0] < a[1] - eps
        and a[2] < b[3] - eps and b[2] < a[3] - eps
        and a[4] < b[5] - eps and b[4] < a[5] - eps
    )


# ------------------------------------------------------------------ Fix #1: socket_blockers()

def test_socket_blockers_erode_outer_wall_and_never_enter_external_air():
    # Tapered stepped hollow wedge (mimics curved wedges 41749/50955):
    # lower tier 24x32 at z=0..4, middle tier 18x26 at z=4..8, upper roof at z=8..10.
    # Prior to the erosion fix, allowedT = encT | mT expanded blockers right onto x=0/x=24 skin.
    V, T = _merge(
        _hollow_box(0.0, 0.0, 0.0, 24.0, 32.0, 4.4, wall=1.5, ceil_t=0.4),
        _hollow_box(3.0, 3.0, 4.0, 21.0, 29.0, 8.4, wall=1.5, ceil_t=0.4),
        _box(3.0, 3.0, 8.0, 21.0, 29.0, 10.0),
    )
    blks = features.socket_blockers(V, T)
    assert blks
    mT, encT, extT, x0, y0, zmin, px = features._enclosed_stack(V, T)
    # 1. Zero blocker boxes may touch the outer skin (< 0.45 mm from outer wall bounds at z=0..4)
    for b in blks:
        bx = b["box"]
        if bx[4] < 3.5:
            assert bx[0] >= 0.45 and bx[1] <= 24.0 - 0.45, bx
            assert bx[2] >= 0.45 and bx[3] <= 32.0 - 0.45, bx
        # 2. Zero blocker voxels (including BLOCK_UP top extension) may enter external air
        i0 = max(0, int(round((bx[0] - x0) / px)))
        i1 = min(extT.shape[1], int(round((bx[1] - x0) / px)))
        j0 = max(0, int(round((bx[2] - y0) / px)))
        j1 = min(extT.shape[2], int(round((bx[3] - y0) / px)))
        k0 = max(0, int(round((bx[4] - zmin) / features.BLOCK_LAYER)))
        k1 = min(extT.shape[0], int(round((bx[5] - zmin) / features.BLOCK_LAYER)))
        assert not extT[k0:k1, i0:i1, j0:j1].any(), f"blocker entered external air: {bx}"
    # 3. Enclosed cavity center is still covered
    cov = features._boxes_to_mask(blks, encT.shape, x0, y0, zmin, px, features.BLOCK_LAYER)
    assert (encT & cov).sum() / encT.sum() >= 0.99


def test_socket_blockers_block_up_respects_thin_roof_and_external_air():
    # Hollow box with a very thin 0.2 mm roof: BLOCK_UP (0.4 mm) must NOT poke above top_z
    # into external air above the part.
    V, T = _hollow_box(0.0, 0.0, 0.0, 16.0, 16.0, 3.2, wall=1.6, ceil_t=0.2)
    blks = features.socket_blockers(V, T)
    assert blks
    for b in blks:
        assert b["box"][5] <= 3.2 + 1e-6, b


def test_socket_blockers_exclude_clips_enforcer_regions_both_formats():
    V, T = _hollow_box(0.0, 0.0, 0.0, 20.0, 20.0, 6.0, wall=1.6, ceil_t=1.6)
    base = features.socket_blockers(V, T)
    assert base

    # Exclude the left half of the cavity via dict format (cantilever_boxes style)
    excl_dict = [{"box": [0.0, 10.0, 0.0, 20.0, 0.0, 6.0]}]
    clipped = features.socket_blockers(V, T, exclude=excl_dict)
    assert clipped
    for b in clipped:
        assert not _boxes_overlap(b["box"], excl_dict[0]["box"]), b
    # Right half (x=15, y=10, z=2) must remain blocked
    assert any(
        b["box"][0] <= 15.0 <= b["box"][1]
        and b["box"][2] <= 10.0 <= b["box"][3]
        and b["box"][4] <= 2.0 <= b["box"][5]
        for b in clipped
    )

    # Also accepts raw 6-tuple format and produces identical result
    clipped_tuple = features.socket_blockers(V, T, exclude=[(0.0, 10.0, 0.0, 20.0, 0.0, 6.0)])
    assert clipped_tuple == clipped


def test_cantilever_and_socket_blockers_disjoint_on_arched_hollow_body():
    # Arched mudguard-like part: bed foot at y=0..8, overhanging hollow upper deck at y=8..28, z=4..12
    # with an enclosed cavity inside y=9.5..26.5, z=4..10.5 and exterior underside at z=4.0.
    V, T = _merge(
        _hollow_box(0.0, 0.0, 0.0, 16.0, 8.0, 12.0, wall=1.5, ceil_t=1.5),
        _hollow_box(0.0, 8.0, 4.0, 16.0, 28.0, 12.0, wall=1.5, ceil_t=1.5),
    )
    enfs = features.cantilever_boxes(V, T)
    blks = features.socket_blockers(V, T, exclude=enfs)
    assert enfs and blks
    # Zero blocker-enforcer overlap
    for b in blks:
        for e in enfs:
            assert not _boxes_overlap(b["box"], e["box"]), (b, e)
    # Interior hollow cavity at y=18, z=7 must remain blocked
    assert any(
        b["box"][0] <= 8.0 <= b["box"][1]
        and b["box"][2] <= 18.0 <= b["box"][3]
        and b["box"][4] <= 7.0 <= b["box"][5]
        for b in blks
    )


# ------------------------------------------------------------------ Fix #2: cantilever_boxes() Z-cap

def test_cantilever_boxes_spans_full_sloped_overhang_height():
    # Inverted slope (mimics 4287b / 3665a): bed foot x=[0..8], y=[0..8], z=[0..10],
    # plus an inverted wedge sloping out from (y=8, z=0) to (y=24, z=8) with top flat at z=10.
    foot = _box(0.0, 0.0, 0.0, 8.0, 8.0, 10.0)
    # Triangular prism wedge: y=8..24, z slopes linearly from 0 at y=8 to 8 at y=24; top at z=10
    Vw = np.array([
        [0.0, 8.0, 0.0],   # 0: bottom-front-left
        [8.0, 8.0, 0.0],   # 1: bottom-front-right
        [0.0, 24.0, 8.0],  # 2: slope-top-back-left
        [8.0, 24.0, 8.0],  # 3: slope-top-back-right
        [0.0, 8.0, 10.0],  # 4: top-front-left
        [8.0, 8.0, 10.0],  # 5: top-front-right
        [0.0, 24.0, 10.0], # 6: top-back-left
        [8.0, 24.0, 10.0], # 7: top-back-right
    ], dtype=np.float64)
    Tw = np.array([
        [0, 2, 3], [0, 3, 1],  # sloped downward overhang face (n_z < 0, z=0..8)
        [4, 5, 7], [4, 7, 6],  # top face at z=10
        [0, 1, 5], [0, 5, 4],  # front
        [2, 6, 7], [2, 7, 3],  # back
        [0, 4, 6], [0, 6, 2],  # left
        [1, 3, 7], [1, 7, 5],  # right
    ], dtype=np.int64)
    V, T = _merge(foot, (Vw, Tw))
    boxes = features.cantilever_boxes(V, T, above=0.6)
    assert len(boxes) == 1
    z0, z1 = boxes[0]["box"][4], boxes[0]["box"][5]
    # Previously z1 was lo[2] + 0.6 = 0.60; now z1 must reach hi[2] + 0.6 = 8.60 (< top z=10.0)
    assert z0 == pytest.approx(0.0, abs=1e-6)
    assert z1 == pytest.approx(8.6, abs=1e-6)
    assert z1 < 10.0


def test_cantilever_boxes_carves_open_top_enclosed_cavity():
    # Upside-down hollow slope (mimics 30363 up=-Z): solid floor at z=0..2,
    # open-top hollow cup (NO roof!) surrounded by walls at z=2..8 over x=[0..16], y=[0..24],
    # while the bed footprint is only y=[0..8] (exterior step/overhang at y=[8..24], z=2.0).
    # Enforcers must support the exterior overhang under z=2.0 without entering the open-top
    # 2D-enclosed cup at z=3..8 (where roofed encT is empty!).
    floor = _box(0.0, 0.0, 0.0, 16.0, 8.0, 2.0)
    deck = _box(0.0, 8.0, 2.0, 16.0, 24.0, 3.0)
    # 4 perimeter walls around x=[0..16], y=[0..24] from z=3..8 (open top -> no roof!)
    walls = _merge(
        _box(0.0, 0.0, 2.0, 16.0, 1.6, 8.0),
        _box(0.0, 22.4, 3.0, 16.0, 24.0, 8.0),
        _box(0.0, 1.6, 2.0, 1.6, 22.4, 8.0),
        _box(14.4, 1.6, 2.0, 16.0, 22.4, 8.0),
    )
    # Add an overhanging exterior lip sticking out at z=6..8 on y=24..28 so AABB spans z=1.7..8.6
    lip = _box(0.0, 24.0, 6.0, 16.0, 28.0, 8.0)
    V, T = _merge(floor, deck, walls, lip)

    enfs = features.cantilever_boxes(V, T)
    assert enfs
    # Center of the open-top enclosed cup (x=8, y=15, z=5) must NOT be inside any enforcer box!
    for e in enfs:
        bx = e["box"]
        assert not (
            bx[0] < 8.0 < bx[1] and bx[2] < 15.0 < bx[3] and bx[4] < 5.0 < bx[5]
        ), f"enforcer entered open-top enclosed cavity: {bx}"
    # Exterior overhang under the deck (x=8, y=16, z=1.9) and under lip (x=8, y=26, z=5.9) ARE covered
    assert any(
        e["box"][0] <= 8.0 <= e["box"][1]
        and e["box"][2] <= 16.0 <= e["box"][3]
        and e["box"][4] <= 1.9 <= e["box"][5]
        for e in enfs
    )
    assert any(
        e["box"][0] <= 8.0 <= e["box"][1]
        and e["box"][2] <= 26.0 <= e["box"][3]
        and e["box"][4] <= 5.9 <= e["box"][5]
        for e in enfs
    )


# ------------------------------------------------------------------ Fix #3: threemf.py merged bands + exclude

def test_merge_bands_disjoint_duplicate_and_overlapping():
    # Disjoint + duplicate bands (Enzo Plate 12 pattern: 42022 + 41749/41750/46413)
    b1 = [
        (17.64, 17.72, {"layer_height": "0.08", "wall_loops": "2"}),
        (8.04, 8.12, {"layer_height": "0.08", "wall_loops": "2"}),
        (17.64, 17.72, {"layer_height": "0.08", "wall_loops": "2"}),
    ]
    assert threemf._merge_bands(b1) == [
        (8.04, 8.12, {"layer_height": "0.08", "wall_loops": "2"}),
        (17.64, 17.72, {"layer_height": "0.08", "wall_loops": "2"}),
    ]

    # Overlapping bands: finer layer_height (0.08) and lower wall_loops (2) win on overlap
    b2 = [
        (10.0, 10.48, {"layer_height": "0.24"}),
        (10.16, 10.24, {"layer_height": "0.08", "wall_loops": "2"}),
    ]
    assert threemf._merge_bands(b2) == [
        (10.0, 10.16, {"layer_height": "0.24"}),
        (10.16, 10.24, {"layer_height": "0.08", "wall_loops": "2"}),
        (10.24, 10.48, {"layer_height": "0.24"}),
    ]


def test_threemf_preserves_ceiling_bands_on_merged_support_plate_and_excludes_enforcers(tmp_path):
    cfg = config.load_config()
    cfg["_composer"] = None
    cfg["support_set"] = ["support_top_z_distance=0.4"]
    comp = settings.Composer(cfg)
    cfg["_composer"] = comp

    V1, T1 = _hollow_box(0.0, 0.0, 0.0, 16.0, 16.0, 20.0, wall=1.6, ceil_t=1.6)
    V2, T2 = _hollow_box(0.0, 0.0, 0.0, 16.0, 16.0, 12.0, wall=1.6, ceil_t=1.6)
    enf1 = [{"box": [0.0, 8.0, 0.0, 16.0, 0.0, 18.0]}]

    meshes = {
        "a.dat": {
            "V": V1.astype(np.float32),
            "T": T1.astype(np.int32),
            "enforcer": features.box_mesh(enf1),
            "enforcer_boxes": enf1,
            "bands": [
                (17.56, 17.8, {"layer_height": "0.24", "wall_loops": "2"}),
                (17.8, 18.04, {"layer_height": "0.24"}),
            ],
        },
        "b.dat": {
            "V": V2.astype(np.float32),
            "T": T2.astype(np.int32),
            "bands": [(8.04, 8.12, {"layer_height": "0.08", "wall_loops": "2"})],
        },
    }
    plates = [{
        "color_id": "1",
        "color_name": "Wine Red",
        "rgb": "720E0F",
        "is_trans": False,
        "index": 1,
        "count": 1,
        "kind": "supports ",
        "support": "plate",
        "items": [
            ("a.dat", 5.0, 5.0, 16.0, 16.0),
            ("b.dat", 30.0, 5.0, 16.0, 16.0),
        ],
        "bbox": (46.0, 21.0),
    }]

    out = tmp_path / "merged_support.3mf"
    with mock.patch.object(comp, "compose", return_value={"enable_support": "0", "enable_prime_tower": "0"}):
        threemf.write_project(cfg, {"set_num": "test", "name": "test"}, plates, meshes, out, verdict=False)

    with zipfile.ZipFile(out) as z:
        assert "Metadata/layer_config_ranges.xml" in z.namelist()
        rng_root = ET.fromstring(z.read("Metadata/layer_config_ranges.xml"))
        objs = rng_root.findall("object")
        assert len(objs) == 1
        assert objs[0].get("id") == "1"
        ranges = [(float(r.get("min_z")), float(r.get("max_z"))) for r in objs[0].findall("range")]
        assert ranges == [(8.04, 8.12), (17.56, 17.8), (17.8, 18.04)]
        ms = z.read("Metadata/model_settings.config").decode()
        assert 'subtype="support_blocker"' in ms

    # Verify blocker mesh generated for a.dat never overlaps enf1
    blk_boxes = features.mesh_boxes(*meshes["a.dat"]["blocker"])
    assert blk_boxes
    for bx in blk_boxes:
        assert not _boxes_overlap(bx, enf1[0]["box"]), bx

    # Also verify fallback when only "enforcer" mesh is present (no "enforcer_boxes" key)
    meshes["a.dat"].pop("blocker", None)
    meshes["a.dat"].pop("enforcer_boxes", None)
    out_v = tmp_path / "merged_support_verdict.3mf"
    with mock.patch.object(comp, "compose", return_value={"enable_support": "0", "enable_prime_tower": "0"}):
        threemf.write_project(cfg, {"set_num": "test", "name": "test"}, plates, meshes, out_v, verdict=True)
    with zipfile.ZipFile(out_v) as z:
        # Mandatory guardrail: blockers kept in verdict=True
        assert 'subtype="support_blocker"' in z.read("Metadata/model_settings.config").decode()
    blk_boxes_fb = features.mesh_boxes(*meshes["a.dat"]["blocker"])
    for bx in blk_boxes_fb:
        assert not _boxes_overlap(bx, enf1[0]["box"]), bx

    assert threemf.validate_project(out, float(cfg["bed"]), float(cfg["depth"]))["bad_plates"] == 0
    assert threemf.validate_project(out_v, float(cfg["bed"]), float(cfg["depth"]))["bad_plates"] == 0


# ------------------------------------------------------------------ Fix #4: pack.py brim_width support

def test_pack_inst_pad_uses_max_of_pad_and_brim():
    assert pack._inst_pad({"size": (10, 10, 5)}) == 0.0
    assert pack._inst_pad({"size": (10, 10, 5), "pad": 4.0}) == 4.0
    assert pack._inst_pad({"size": (10, 10, 5), "brim_width": 3.0, "brim_object_gap": 0.2}) == pytest.approx(3.2)
    assert pack._inst_pad({"size": (10, 10, 5), "pad": 5.0, "brim_width": 3.0, "brim_object_gap": 0.2}) == 5.0
    assert pack._inst_pad({"size": (10, 10, 5), "pad": 1.0, "brim_width": 3.0, "brim_object_gap": 0.2}) == pytest.approx(3.2)
