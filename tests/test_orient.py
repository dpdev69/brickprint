"""Orientation emulator (numpy only) on tiny synthetic meshes."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import pytest

from brickprint import orient

EYE = np.eye(3)


def box(x0, y0, z0, x1, y1, z1):
    """Closed, outward-facing box as (vertices, triangles)."""
    V = np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)], float)
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    tris = []
    for q in quads:
        tris += [[q[0], q[1], q[2]], [q[0], q[2], q[3]]]
    T = np.array(tris)
    c = V.mean(axis=0)
    for i, t in enumerate(T):
        n = np.cross(V[t[1]] - V[t[0]], V[t[2]] - V[t[0]])
        if np.dot(n, V[t].mean(axis=0) - c) < 0:
            T[i] = t[[0, 2, 1]]
    return V, T


def merge(*meshes):
    Vs, Ts, off = [], [], 0
    for V, T in meshes:
        Vs.append(V)
        Ts.append(T + off)
        off += len(V)
    return np.vstack(Vs), np.vstack(Ts)


def rot_with_up(axis: str) -> np.ndarray:
    return next(R for R in orient.ROTATIONS if orient.up_axis(R) == axis)


# Meshes are built once; candidate poses are cached (slicing dominates run time).
@lru_cache(maxsize=None)
def mesh(name: str):
    if name == "cube":
        return box(0, 0, 0, 6, 6, 6)
    if name == "slab":
        return box(0, 0, 0, 12, 12, 2)
    if name == "table":      # narrow stem up to a wide top plate: bad studs-up pose
        return merge(box(5, 5, 0, 7, 7, 7), box(0, 0, 6, 12, 12, 8))
    if name == "step":       # upper block barely touches the lower one: floats +Z / -Z
        return merge(box(0, 0, 0, 6, 6, 3), box(5.99, 0, 3, 12, 6, 6))
    if name == "hover":      # two blocks 4 mm apart in Z: empty layers when Z is up
        return merge(box(0, 0, 0, 6, 6, 2), box(0, 0, 6, 6, 6, 8))
    if name == "diagonal":   # two cubes apart along every axis: bad in every pose
        return merge(box(0, 0, 0, 2, 2, 2), box(6, 6, 6, 8, 8, 8))
    raise KeyError(name)


@lru_cache(maxsize=None)
def cands(name: str):
    V, T = mesh(name)
    return orient.candidates(V, T, EYE)


def pick(name: str, R0=EYE, **kw) -> dict:
    V, T = mesh(name)
    # R0's up axis must be in the cached candidates (identity -> +Z)
    return orient.choose(V, T, R0, cand=cands(name) if np.allclose(R0, EYE) else None, **kw)


# ---------------------------------------------------------------- rotations

def test_rotations_are_the_24_proper_axis_rotations():
    assert len(orient.ROTATIONS) == 24
    for R in orient.ROTATIONS:
        assert R.shape == (3, 3)
        assert np.allclose(R @ R.T, np.eye(3))
        assert round(np.linalg.det(R)) == 1
        assert set(np.unique(R)) <= {-1.0, 0.0, 1.0}
    keys = {tuple(R.ravel()) for R in orient.ROTATIONS}
    assert len(keys) == 24
    assert any(np.allclose(R, EYE) for R in orient.ROTATIONS)


def test_six_up_directions_four_yaws_each():
    ups = [orient.up_axis(R) for R in orient.ROTATIONS]
    assert sorted(set(ups)) == sorted(orient.AXIS_NAMES)
    assert all(ups.count(u) == 4 for u in orient.AXIS_NAMES)
    assert orient.up_axis(EYE) == "+Z"


def test_up_axis_matches_where_original_axis_ends_up():
    for R in orient.ROTATIONS:
        up = orient.up_axis(R)
        sign = 1.0 if up[0] == "+" else -1.0
        original = np.zeros(3)
        original["XYZ".index(up[1])] = sign
        assert np.allclose(R @ original, [0, 0, 1])


def test_rot_label():
    assert orient.rot_label(EYE).startswith("up=+Z")
    R = rot_with_up("+X")
    assert orient.rot_label(R, {"flat": R}) == "flat"
    assert orient.rot_label(R, {"other": EYE}).startswith("up=+X")


def test_candidates_cover_the_six_up_axes_with_r0_for_its_own():
    c = cands("cube")
    assert sorted(c) == sorted(orient.AXIS_NAMES)
    assert np.allclose(c["+Z"][0], EYE)
    R0 = rot_with_up("+X")
    V, T = mesh("cube")
    c2 = orient.candidates(V, T, R0)
    assert sorted(c2) == sorted(orient.AXIS_NAMES)
    assert c2["+X"][0] is R0
    for up, (R, _m) in c2.items():
        assert orient.up_axis(R) == up


# ------------------------------------------------------------- slicing / metrics

def test_slice_stack_cube_layers():
    V, T = mesh("cube")
    mask, px = orient.slice_stack(V, T)
    assert px == orient.PX
    nl = mask.shape[2]
    assert nl == 30                       # 6 mm / 0.2 mm
    areas = mask.sum(axis=(0, 1)) * px * px
    assert np.allclose(areas, 36.0, rtol=0.05)


def test_layer_report_cube_has_no_float_or_empty_layers():
    mask, px = orient.slice_stack(*mesh("cube"))
    rep = orient.layer_report(mask, px)
    assert rep["float_n"] == 0
    assert rep["empty_layers"] == 0
    assert rep["first_layer_area"] == pytest.approx(36.0, rel=0.05)


def test_layer_report_empty_mask():
    rep = orient.layer_report(np.zeros((5, 5, 4), dtype=bool), 0.1)
    assert rep["float_n"] == 0 and rep["empty_layers"] == 0 and rep["first_layer_area"] == 0.0


def test_layer_report_detects_floating_region_and_empty_layers():
    mask = np.zeros((20, 20, 6), dtype=bool)
    mask[2:8, 2:8, 0] = True        # layer 0
    mask[12:18, 12:18, 3] = True    # layer 3, nothing below -> floats; layers 1-2 empty
    rep = orient.layer_report(mask, 1.0)
    assert rep["empty_layers"] == 2
    assert rep["float_n"] == 1
    assert rep["float_area"] == pytest.approx(36.0)
    assert rep["first_layer_area"] == pytest.approx(36.0)


def test_label_layers_is_per_layer_four_connected():
    mask = np.zeros((6, 6, 2), dtype=bool)
    mask[0:2, 0:2, 0] = True
    mask[4:6, 4:6, 0] = True
    mask[2, 2, 0] = True            # diagonal-only neighbour of the first block
    mask[0:2, 0:2, 1] = True
    lab = orient._label_layers(mask)
    assert len(np.unique(lab[:, :, 0][lab[:, :, 0] > 0])) == 3
    assert len(np.unique(lab[:, :, 1][lab[:, :, 1] > 0])) == 1
    assert (lab > 0).sum() == mask.sum()


def test_hull_area_and_inside_test():
    pts = np.array([[0, 0], [10, 0], [10, 10], [0, 10], [5, 5], [2, 3]], float)
    H = orient._hull(pts)
    assert len(H) == 4
    assert orient._poly_area(H) == pytest.approx(100.0)
    inside = orient._in_hull(np.array([[5, 5], [11, 5], [0, 0]], float), H)
    assert inside.tolist() == [True, False, True]
    assert orient._poly_area(np.zeros((2, 2))) == 0.0


def test_surface_metrics_cube():
    V, T = mesh("cube")
    m = orient.surface_metrics(V, T)
    assert m["contact_area"] == pytest.approx(36.0)
    assert m["hull_area"] == pytest.approx(36.0)
    assert m["contact_ratio"] == pytest.approx(1.0)
    assert m["down_area"] == 0.0
    assert m["height"] == pytest.approx(6.0)
    assert m["size"] == pytest.approx([6.0, 6.0, 6.0])


def test_surface_metrics_table_has_unsupported_underside():
    V, T = mesh("table")
    m = orient.surface_metrics(V, T)
    assert m["down_area"] > 100.0          # the 12x12 plate underside hangs over the bed
    assert m["contact_ratio"] < 0.05


def test_evaluate_applies_rotation_and_shifts_to_origin():
    V, T = mesh("slab")
    flat = orient.evaluate(V, T, EYE)
    on_end = orient.evaluate(V, T, rot_with_up("+X"))
    assert flat["height"] == pytest.approx(2.0)
    assert on_end["height"] == pytest.approx(12.0)
    assert flat["first_layer_area"] > on_end["first_layer_area"]
    assert flat["on_edge"] is False and on_end["on_edge"] is False
    assert flat["float_n"] == 0
    assert flat["px"] == orient.PX
    # a rotated copy of the mesh with the inverse rotation is the same pose
    R = rot_with_up("+X")
    again = orient.evaluate(V @ R.T + 100.0, T, EYE)
    assert again["height"] == pytest.approx(on_end["height"])


def test_cube_is_the_same_in_every_pose():
    c = cands("cube")
    for _up, (_R, m) in c.items():
        assert m["float_n"] == 0 and m["empty_layers"] == 0 and not m["on_edge"]
        assert m["height"] == pytest.approx(6.0)
        assert m["first_layer_area"] == pytest.approx(36.0, rel=0.05)


def test_table_stem_up_stands_on_an_edge_and_flat_does_not():
    c = cands("table")
    assert c["+Z"][1]["on_edge"] is True
    assert c["+Z"][1]["first_layer_area"] < orient.EDGE_CONTACT_MM2
    assert c["-Z"][1]["on_edge"] is False
    assert c["-Z"][1]["first_layer_area"] > 100.0


def test_step_floats_in_z_poses_only():
    c = cands("step")
    assert c["+Z"][1]["float_n"] == 1
    assert c["-Z"][1]["float_n"] == 1
    assert c["+Y"][1]["float_n"] == 0 and c["-Y"][1]["float_n"] == 0


def test_hover_has_empty_layers_in_z_poses():
    c = cands("hover")
    assert c["+Z"][1]["empty_layers"] == 20
    assert c["+Y"][1]["empty_layers"] == 0


# ------------------------------------------------------------------ compare

def _metrics(**over):
    m = {"hull_area": 100.0, "empty_layers": 0, "on_edge": False, "float_n": 0,
         "float_area": 0.0, "first_layer_area": 100.0, "down_area": 0.0, "height": 10.0}
    m.update(over)
    return m


def test_worse_tolerance_band():
    assert orient._worse(10.0, 10.1, False, 0.0) == 0         # within 2 %
    assert orient._worse(5.0, 10.0, False, 0.5) == -1         # lower is better
    assert orient._worse(5.0, 10.0, True, 0.5) == 1           # higher is better
    assert orient._worse(10.0, 10.2, False, 0.0) == 0
    assert orient._worse(10.0, 10.5, False, 0.0) == -1


def test_compare_is_lexicographic_with_two_percent_band():
    base = _metrics()
    assert orient.compare(base, base) == 0
    assert orient.compare(_metrics(float_n=1, float_area=5.0), base) == 1
    assert orient.compare(base, _metrics(float_n=1, float_area=5.0)) == -1
    # a floating-free tall pose beats a floating short one
    assert orient.compare(_metrics(height=30.0), _metrics(float_n=1, float_area=2.0, height=5.0)) == -1
    # height only breaks ties between otherwise equal poses, and only beyond 2 %
    assert orient.compare(_metrics(height=10.1), base) == 0
    assert orient.compare(_metrics(height=12.0), base) == 1
    # more first-layer contact wins until saturation
    assert orient.compare(_metrics(first_layer_area=15.0), _metrics(first_layer_area=5.0)) == -1
    # ...and "enough" contact (>= CONTACT_SAT_MM2 / 10 % of the hull) ties
    assert orient.compare(_metrics(first_layer_area=100.0), _metrics(first_layer_area=40.0)) == 0


def test_compare_slicer_verdicts_outrank_emulation():
    good_slicer = _metrics(slicer_empty=0, slicer_float=0, slicer_cant=0, float_n=3, float_area=50.0)
    bad_slicer = _metrics(slicer_empty=0, slicer_float=1, slicer_cant=0)
    assert orient.compare(good_slicer, bad_slicer) == -1


# ------------------------------------------------------------------- choose

def test_choose_keeps_a_good_pose():
    r = pick("cube")
    assert r["changed"] is False and r["reason"] == "kept" and r["needs_support"] is False
    assert np.allclose(r["R"], EYE)
    assert sorted(r["candidates"]) == sorted(orient.AXIS_NAMES)


def test_choose_keeps_current_pose_even_if_another_is_much_better():
    # the policy keeps a pose without problems; it does not hunt for >2 % wins
    V, T = mesh("slab")
    R0 = rot_with_up("+X")                     # slab on end: no float, not on an edge
    r = orient.choose(V, T, R0)
    assert r["changed"] is False
    assert orient.up_axis(r["R"]) == "+X"
    assert r["after"]["first_layer_area"] < r["candidates"]["+Z"]["first_layer_area"]


def test_choose_flips_a_part_standing_on_an_edge():
    r = pick("table")
    assert r["changed"] is True
    assert orient.up_axis(r["R"]) == "-Z"
    assert r["reason"] == "stood on an edge"
    assert r["needs_support"] is False
    assert r["before"]["on_edge"] is True and r["after"]["on_edge"] is False


def test_choose_hard_override_on_edge_is_not_revisited():
    r = pick("table", hard_override=True)
    assert r["changed"] is False and r["reason"] == "kept"


def test_choose_replaces_floating_pose():
    r = pick("step")
    assert r["changed"] is True
    assert r["reason"] == "floating"
    assert r["needs_support"] is False
    assert r["after"]["float_n"] == 0
    assert orient.up_axis(r["R"]) in ("+Y", "-Y")


def test_choose_hard_override_that_floats_is_revisited():
    r = pick("step", hard_override=True)
    assert r["changed"] is True
    assert r["reason"].startswith("override floated: ")


def test_choose_reports_empty_layer():
    r = pick("hover")
    assert r["changed"] is True
    assert r["reason"] == "empty layer"
    assert r["after"]["empty_layers"] == 0


def test_choose_part_bad_in_every_pose_keeps_pose_and_needs_support():
    r = pick("diagonal")
    assert r["changed"] is False
    assert r["needs_support"] is True
    assert r["reason"] == "no better orientation"
    assert np.allclose(r["R"], EYE)


def test_choose_uses_slicer_verdicts_over_emulation():
    ok = {"float": False, "cant": False, "empty": False, "sliced": True}
    verdicts = {u: dict(ok) for u in orient.AXIS_NAMES}
    verdicts["+Z"]["float"] = True
    r = pick("cube", verdicts=verdicts)
    assert r["changed"] is True
    assert orient.up_axis(r["R"]) != "+Z"
    assert r["reason"] == "floating"
    assert r["needs_support"] is False
    # every pose floats according to the slicer: keep the pose, flag support
    allfloat = {u: {"float": True} for u in orient.AXIS_NAMES}
    r = pick("cube", verdicts=allfloat)
    assert r["changed"] is False and r["needs_support"] is True


def test_choose_does_not_mutate_supplied_candidates():
    c = cands("cube")
    before = {up: dict(m) for up, (_R, m) in c.items()}
    V, T = mesh("cube")
    orient.choose(V, T, EYE, verdicts={"+Z": {"float": True}}, cand=c)
    assert {up: m for up, (_R, m) in c.items()} == before


def test_apply_verdict():
    m = {}
    orient._apply_verdict(m, None)
    assert m == {}
    orient._apply_verdict(m, {"float": True, "sliced": False})
    assert m == {"slicer_empty": 1, "slicer_float": 1, "slicer_cant": 0}
    assert orient._bad(m)
