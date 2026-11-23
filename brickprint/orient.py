"""Print-orientation selection for LEGO part meshes (numpy only).

For every part the 24 proper axis-aligned rotations are grouped by the axis
that ends up pointing at +Z (6 "up" directions; the yaw around Z does not
change any print metric).  Each up direction is scored on:

-1. (hard) empty layer: a pose whose slice has an empty 0.2 mm layer between
   its first and last layer is rejected (Bambu aborts: "Object can't be
   printed for empty layer").
1. floating regions, Bambu's rule: the posed mesh is sliced like the slicer
   (facet/plane segments chained into loops by edge id, then 0.01 mm, then
   gaps <= 2 mm closed; open leftovers dropped; positive fill at 0.1 mm).
   A region (4-connected area) of layer k >= 1 floats when its overlap with
   layer k-1 is < max(0.25 mm^2, 1 % of the region).  Nothing propagates
   through in-layer connectivity; cavity ceilings are fine because they are
   one region with the walls.
0. (gate) standing on an edge: first-layer area (sliced layer 0, what the
   slicer's 0.2 mm first layer really lays down) < 5 % of the XY hull and
   < 25 mm^2 (rings such as flat tyres have a low ratio but enough area).
2. bed contact: first-layer area, saturated at ``CONTACT_SAT_MM2`` / 10 % of
   the hull so "enough" contact ties.  The strict face-based contact (faces
   with every vertex below z=0.05 mm, the forensics' ``contact_ratio``) is
   reported too but is 0 for round-bottomed parts, so it is not scored.
3. unsupported downward area: faces with n_z < -0.7 whose centroid is above
   1 mm; inside the first-layer convex outline they count half (bridges),
   outside they count full.
4. height.

Selection policy (keeps the layout recognisable, studs-up stays studs-up):
the current orientation (identity or the ORIENT override) is kept unless it
has floating regions, an empty layer, or stands on an edge (first layer < 5 % of its hull;
ORIENT overrides are only revisited when they float, and only replaced by a
candidate that is island-free and not on an edge).  A part that floats in
every pose keeps its current pose and goes to a support plate.  Only
then is the best candidate taken, and only when it beats the current one by
more than 2 % on the first differing criterion.  ``needs_support`` is set when
the chosen orientation still has floating regions (or, if every pose has
one, an empty layer; such parts are reported and must be checked by hand).

This module is the single source of the orientation emulator.
"""

from __future__ import annotations

import itertools

import numpy as np

ORIENT_VERSION = 2      # slice/metric emulation (bump re-slices every pose)
SELECT_VERSION = 2      # selection policy only (choose())
LAYER = 0.2              # mm slice height
PX = 0.1                 # mm raster pixel for the slice areas
MAX_CELLS = 40_000_000   # pixel grows for very large parts
GAP_CLOSE = 2.0          # mm: open slice polylines closed up to this gap
FLOAT_OVERLAP_MM2 = 0.25  # region floats if overlap < max(this, 1 % of it)
FLOAT_MIN_REGION_MM2 = 0.1  # ignore raster specks
ISLAND_MIN_AREA = 1.0    # mm^2 tie tolerance on floating area
CONTACT_Z = 0.05         # mm
CONTACT_SAT_MM2 = 20.0   # contact above this (or 10 % of hull) is "enough"
EDGE_CONTACT_RATIO = 0.05
EDGE_CONTACT_MM2 = 25.0  # ...and below this absolute first-layer area
DOWN_NZ = -0.7
DOWN_MIN_Z = 1.0
TIE = 0.02

AXIS_NAMES = ["+X", "+Y", "+Z", "-X", "-Y", "-Z"]


def proper_rotations() -> list[np.ndarray]:
    out = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1, -1), repeat=3):
            R = np.zeros((3, 3))
            for i, (j, s) in enumerate(zip(perm, signs)):
                R[i, j] = s
            if round(np.linalg.det(R)) == 1:
                out.append(R)
    return out


ROTATIONS = proper_rotations()


def up_axis(R: np.ndarray) -> str:
    """Name of the ORIGINAL axis that this rotation points at +Z."""
    j = int(np.nonzero(R[2])[0][0])
    return ("+" if R[2, j] > 0 else "-") + "XYZ"[j]


def rot_label(R: np.ndarray, named: dict | None = None) -> str:
    for k, M in (named or {}).items():
        if np.allclose(M, R):
            return k
    return "up=" + up_axis(R) + " [" + ";".join(" ".join(str(int(x)) for x in row) for row in R) + "]"


# --------------------------------------------------------------------------
# slicer emulation: closed loops per 0.2 mm layer, positive fill, regions
# --------------------------------------------------------------------------

def _layer_segments(W: np.ndarray, T: np.ndarray, z: float):
    """Facet/plane segments oriented by the facet normal (outer loops CCW)."""
    s = W[:, 2] > z
    st = s[T]
    cnt = st.sum(axis=1)
    m = (cnt == 1) | (cnt == 2)
    if not m.any():
        return None
    F = T[m]
    S = st[m]
    nverts = len(W)
    pts, keys = [], []
    for a, b in ((0, 1), (1, 2), (2, 0)):
        cross = S[:, a] != S[:, b]
        u, v = F[:, a], F[:, b]
        t = (z - W[u, 2]) / np.where(cross, W[v, 2] - W[u, 2], 1.0)
        p = W[u, :2] + t[:, None] * (W[v, :2] - W[u, :2])
        k = np.minimum(u, v).astype(np.int64) * nverts + np.maximum(u, v)
        pts.append(np.where(cross[:, None], p, np.nan))
        keys.append(np.where(cross, k, -1))
    P = np.stack(pts, axis=1)          # (f, 3, 2)
    K = np.stack(keys, axis=1)         # (f, 3)
    has = K >= 0
    ok = has.sum(axis=1) == 2
    P, K, has, F = P[ok], K[ok], has[ok], F[ok]
    order = np.argsort(~has, axis=1, kind="stable")[:, :2]
    p1 = np.take_along_axis(P, order[:, :1, None], axis=1)[:, 0]
    p2 = np.take_along_axis(P, order[:, 1:2, None], axis=1)[:, 0]
    k1 = np.take_along_axis(K, order[:, :1], axis=1)[:, 0]
    k2 = np.take_along_axis(K, order[:, 1:2], axis=1)[:, 0]
    A, B, C = W[F[:, 0]], W[F[:, 1]], W[F[:, 2]]
    n = np.cross(B - A, C - A)
    d = p2 - p1
    sw = d[:, 0] * (-n[:, 1]) + d[:, 1] * n[:, 0] < 0
    p1s = np.where(sw[:, None], p2, p1)
    p2s = np.where(sw[:, None], p1, p2)
    k1s = np.where(sw, k2, k1)
    k2s = np.where(sw, k1, k2)
    return k1s, p1s, k2s, p2s


def _chain(seg, gap: float):
    """Chain segments into closed loops (edge ids, then 0.01 mm, then gap)."""
    k1, p1, k2, p2 = seg
    start: dict[int, list[int]] = {}
    for i, k in enumerate(k1.tolist()):
        start.setdefault(k, []).append(i)
    used = np.zeros(len(k1), dtype=bool)
    loops, opens = [], []
    k1l, k2l = k1.tolist(), k2.tolist()
    for i in range(len(k1l)):
        if used[i]:
            continue
        used[i] = True
        poly = [p1[i], p2[i]]
        k0, kc = k1l[i], k2l[i]
        while True:
            if kc == k0:
                loops.append(np.array(poly[:-1]))
                break
            nxt = [j for j in start.get(kc, []) if not used[j]]
            if not nxt:
                opens.append(np.array(poly))
                break
            j = nxt[0]
            used[j] = True
            poly.append(p2[j])
            kc = k2l[j]

    def join(opens, tol):
        changed = True
        while changed and opens:
            changed = False
            keep = []
            for p in opens:
                if len(p) >= 3 and np.linalg.norm(p[0] - p[-1]) <= tol:
                    loops.append(p)
                    changed = True
                else:
                    keep.append(p)
            opens = keep
            if len(opens) < 2:
                break
            E = np.array([p[-1] for p in opens])
            S = np.array([p[0] for p in opens])
            D = np.linalg.norm(E[:, None] - S[None], axis=2)
            np.fill_diagonal(D, np.inf)
            i, j = np.unravel_index(np.argmin(D), D.shape)
            if D[i, j] <= tol:
                opens[i] = np.vstack([opens[i], opens[j]])
                opens.pop(j)
                changed = True
        return opens

    opens = join(opens, 1e-2)
    if gap > 0:
        opens = join(opens, gap)
    return loops


def _raster(loops, x0, y0, nx, ny, px):
    """Positive-winding fill of closed loops on a px grid -> bool (nx, ny)."""
    loops = [L for L in loops if len(L) >= 3]
    if not loops:
        return np.zeros((nx, ny), dtype=bool)
    P = np.vstack(loops)
    Q = np.vstack([np.roll(L, -1, axis=0) for L in loops])
    ya, yb = P[:, 1], Q[:, 1]
    lo, hi = np.minimum(ya, yb), np.maximum(ya, yb)
    sg = np.where(yb > ya, 1, -1)
    r0 = np.clip(np.ceil((lo - y0) / px - 0.5).astype(int), 0, ny)
    r1 = np.clip(np.ceil((hi - y0) / px - 0.5).astype(int), 0, ny)
    n = np.maximum(r1 - r0, 0)
    tot_n = int(n.sum())
    acc = np.zeros((ny, nx + 1), dtype=np.int32)
    tot = np.zeros(ny, dtype=np.int32)
    if tot_n:
        idx = np.repeat(np.arange(len(P)), n)
        offs = np.repeat(np.cumsum(n) - n, n)
        rows = r0[idx] + (np.arange(tot_n) - offs)
        yc = y0 + (rows + 0.5) * px
        t = (yc - ya[idx]) / (yb[idx] - ya[idx])
        xc = P[idx, 0] + t * (Q[idx, 0] - P[idx, 0])
        col = np.clip(np.ceil((xc - x0) / px - 0.5).astype(int), 0, nx)
        np.add.at(acc, (rows, col), sg[idx])
        np.add.at(tot, rows, sg[idx])
    wind = tot[:, None] - np.cumsum(acc, axis=1)[:, :nx]
    return (wind > 0).T


def slice_stack(V: np.ndarray, T: np.ndarray, layer: float = LAYER, px: float | None = None):
    """Slice a (posed, min-shifted) mesh like the slicer: returns (mask, px).

    mask[x, y, k] is the filled area of layer k (z = layer/2 + k*layer).
    """
    W = np.asarray(V, dtype=np.float64)
    W = W - W.min(axis=0)
    T = np.asarray(T, dtype=np.int64)
    ext = W.max(axis=0)
    nl = max(int(np.ceil(ext[2] / layer - 0.5)), 1)
    if px is None:
        px = PX
        cells = (ext[0] / px + 10) * (ext[1] / px + 10) * nl
        if cells > MAX_CELLS:
            px = float(np.sqrt((ext[0] + 1) * (ext[1] + 1) * nl / MAX_CELLS))
    x0, y0 = -0.5 + 0.0123, -0.5 + 0.0071
    nx = int(np.ceil((ext[0] + 1.0) / px))
    ny = int(np.ceil((ext[1] + 1.0) / px))
    mask = np.zeros((nx, ny, nl), dtype=bool)
    for k in range(nl):
        z = layer / 2 + k * layer + 1.234e-5
        seg = _layer_segments(W, T, z)
        if seg is None:
            continue
        mask[:, :, k] = _raster(_chain(seg, GAP_CLOSE), x0, y0, nx, ny, px)
    return mask, px


def _label_layers(mask: np.ndarray) -> np.ndarray:
    """4-connected components inside each XY layer (axis 2 = layer).

    Min-label propagation plus pointer jumping over the occupied cells only;
    returns int labels (0 = background), equal within a component.
    """
    nx, ny, nz = mask.shape
    cells = np.nonzero(mask)
    n = len(cells[0])
    if n == 0:
        return np.zeros(mask.shape, dtype=np.int32)
    P = np.full((nx + 2, ny + 2, nz), -1, dtype=np.int64)
    P[cells[0] + 1, cells[1] + 1, cells[2]] = np.arange(n)
    nbrs = [P[cells[0] + 1 + dx, cells[1] + 1 + dy, cells[2]]
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))]
    NB = np.stack(nbrs, axis=1)
    NBc = np.where(NB >= 0, NB, np.arange(n)[:, None])
    L = np.arange(n, dtype=np.int64)
    while True:
        M = np.minimum(L, L[NBc].min(axis=1))
        while True:
            J = M[M]
            if np.array_equal(J, M):
                break
            M = J
        if np.array_equal(M, L):
            break
        L = M
    out = np.zeros(mask.shape, dtype=np.int32)
    out[cells] = (L + 1).astype(np.int32)
    return out


def layer_report(mask: np.ndarray, px: float) -> dict:
    """Floating regions (Bambu's rule), empty layers and first-layer area.

    A region (4-connected area of one layer, k >= 1) floats when its overlap
    with the layer below is < max(FLOAT_OVERLAP_MM2, 1 % of its area).
    Support is NOT propagated through in-layer connectivity beyond the
    region itself; a brick's cavity ceiling is one region with its walls.
    """
    area = mask.sum(axis=(0, 1))
    nz = np.nonzero(area)[0]
    if len(nz) == 0:
        return {"float_n": 0, "float_area": 0.0, "float_zmin": 0.0, "empty_layers": 0,
                "first_layer_area": 0.0}
    k0, k1 = nz[0], nz[-1]
    m = mask[:, :, k0:k1 + 1]
    empty = int((area[k0:k1 + 1] == 0).sum())
    lab = _label_layers(m)
    below = np.zeros_like(m)
    below[:, :, 1:] = m[:, :, :-1]
    pa = px * px
    ids, cnt = np.unique(lab[lab > 0], return_counts=True)
    ov_ids, ov_cnt = np.unique(lab[(lab > 0) & below], return_counts=True)
    ov = np.zeros(len(ids))
    ov[np.searchsorted(ids, ov_ids)] = ov_cnt
    # layer index of each region (all its cells share one layer)
    first_cell = np.zeros(len(ids), dtype=np.int64)
    flat = lab.ravel()
    order = np.argsort(flat, kind="stable")
    sorted_lab = flat[order]
    pos = np.searchsorted(sorted_lab, ids)
    first_cell = order[pos]
    layer_of = first_cell % m.shape[2]
    reg_area = cnt * pa
    ov_area = ov * pa
    floating = ((layer_of >= 1) & (reg_area >= FLOAT_MIN_REGION_MM2)
                & (ov_area < np.maximum(FLOAT_OVERLAP_MM2, 0.01 * reg_area)))
    return {
        "float_n": int(floating.sum()),
        "float_area": float(reg_area[floating].sum()),
        "float_zmin": float((layer_of[floating].min() + k0) * LAYER + LAYER / 2) if floating.any() else 0.0,
        "empty_layers": empty,
        "first_layer_area": float(area[k0] * pa),
    }


# --------------------------------------------------------------------------
# mesh metrics in a given orientation
# --------------------------------------------------------------------------

def _hull(P: np.ndarray) -> np.ndarray:
    P = np.unique(np.round(P, 5), axis=0)
    if len(P) < 3:
        return P
    P = P[np.lexsort((P[:, 1], P[:, 0]))]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in P:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in P[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1])


def _poly_area(H: np.ndarray) -> float:
    if len(H) < 3:
        return 0.0
    x, y = H[:, 0], H[:, 1]
    return float(0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _in_hull(P: np.ndarray, H: np.ndarray) -> np.ndarray:
    if len(H) < 3:
        return np.zeros(len(P), dtype=bool)
    inside = np.ones(len(P), dtype=bool)
    for i in range(len(H)):
        a, b = H[i], H[(i + 1) % len(H)]
        inside &= (b[0] - a[0]) * (P[:, 1] - a[1]) - (b[1] - a[1]) * (P[:, 0] - a[0]) >= -1e-6
    return inside


def surface_metrics(V: np.ndarray, T: np.ndarray) -> dict:
    V = np.asarray(V, dtype=np.float64)
    V = V - V.min(axis=0)
    A, B, C = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    N = np.cross(B - A, C - A)
    area2 = np.linalg.norm(N, axis=1)
    area = 0.5 * area2
    nz = np.where(area2 > 0, N[:, 2] / np.where(area2 > 0, area2, 1), 0)
    zmax = np.maximum(np.maximum(A[:, 2], B[:, 2]), C[:, 2])
    contact_f = (zmax < CONTACT_Z) & (nz < -0.5)
    contact = float(area[contact_f].sum())
    hull = _hull(V[:, :2])
    hull_area = _poly_area(hull)
    first = V[np.unique(T[contact_f])][:, :2] if contact_f.any() else V[V[:, 2] < CONTACT_Z][:, :2]
    fh = _hull(first) if len(first) >= 3 else np.zeros((0, 2))
    cen = (A + B + C) / 3.0
    down = (nz < DOWN_NZ) & (cen[:, 2] > DOWN_MIN_Z)
    w = np.where(_in_hull(cen[:, :2], fh), 0.5, 1.0)
    return {
        "contact_area": contact,
        "hull_area": hull_area,
        "contact_ratio": contact / hull_area if hull_area > 0 else 0.0,
        "down_area": float((area * w)[down].sum()),
        "height": float(V[:, 2].max()),
        "size": (V.max(axis=0)).tolist(),
    }


def evaluate(V, T, R) -> dict:
    W = np.asarray(V, dtype=np.float64) @ np.asarray(R, dtype=float).T
    W = W - W.min(axis=0)
    m = surface_metrics(W, T)
    mask, px = slice_stack(W, T)
    m.update(layer_report(mask, px))
    m["px"] = px
    first = m["first_layer_area"]
    m["first_layer_ratio"] = first / m["hull_area"] if m["hull_area"] > 0 else 0.0
    m["on_edge"] = (m["first_layer_ratio"] < EDGE_CONTACT_RATIO
                    and first < EDGE_CONTACT_MM2)
    return m


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def _worse(a: float, b: float, higher_better: bool, abs_tol: float) -> int:
    """-1 if a is clearly better than b, +1 if clearly worse, 0 within tolerance."""
    tol = max(abs_tol, TIE * max(abs(a), abs(b)))
    if abs(a - b) <= tol:
        return 0
    better = a > b if higher_better else a < b
    return -1 if better else 1


def compare(m: dict, ref: dict) -> int:
    """Lexicographic comparison with a 2 % tie band.  <0: m better than ref."""
    sat = max(CONTACT_SAT_MM2, 0.10 * max(m["hull_area"], ref["hull_area"]))
    steps = []
    if "slicer_empty" in m and "slicer_empty" in ref:
        # real slicer verdicts outrank the emulation
        steps += [(m["slicer_empty"], ref["slicer_empty"], False, 0.5),
                  (m["slicer_float"], ref["slicer_float"], False, 0.5),
                  (m["slicer_cant"], ref["slicer_cant"], False, 0.5)]
    steps += [
        (float(m["empty_layers"] > 0), float(ref["empty_layers"] > 0), False, 0.5),
        (float(m["on_edge"]), float(ref["on_edge"]), False, 0.5),
        (m["float_n"], ref["float_n"], False, 0.5),
        (m["float_area"], ref["float_area"], False, ISLAND_MIN_AREA),
        (min(m["first_layer_area"], sat), min(ref["first_layer_area"], sat), True, 0.5),
        (m["down_area"], ref["down_area"], False, 1.0),
        (m["height"], ref["height"], False, 0.1),
    ]
    for a, b, hb, tol in steps:
        c = _worse(a, b, hb, tol)
        if c:
            return c
    return 0


def _apply_verdict(m: dict, v: dict | None) -> None:
    if v is None:
        return
    m["slicer_empty"] = int(bool(v.get("empty")) or not v.get("sliced", True))
    m["slicer_float"] = int(bool(v.get("float")))
    m["slicer_cant"] = int(bool(v.get("cant")))


def _bad(m: dict) -> bool:
    if "slicer_empty" in m:
        return bool(m["slicer_empty"] or m["slicer_float"] or m["slicer_cant"])
    return m["float_n"] > 0 or m["empty_layers"] > 0


def candidates(V, T, R0: np.ndarray) -> dict:
    """{up: (R, metrics)} for the 6 up directions (R0 used for its own up)."""
    V = np.asarray(V, dtype=np.float64)
    T = np.asarray(T, dtype=np.int64)
    cand = {}
    for R in ROTATIONS:
        up = up_axis(R)
        if up == up_axis(R0):
            R = R0
        if up not in cand:
            cand[up] = (R, evaluate(V, T, R))
    return cand


def choose(V, T, R0: np.ndarray, hard_override: bool = False,
           verdicts: dict | None = None, cand: dict | None = None) -> dict:
    """Pick the print rotation for one part.  R0 = current rotation.

    ``verdicts`` optionally maps an up axis ("+X" ... "-Z") to the real
    slicer's verdict for that pose ({"float", "cant", "empty", "sliced"},
    e.g. from Bambu Studio CLI probes).  When given, those verdicts decide
    floating / empty-layer status instead of the emulation.
    """
    if cand is None:
        cand = candidates(V, T, R0)
    cand = {up: (np.asarray(R, dtype=float), dict(m)) for up, (R, m) in cand.items()}
    if verdicts is not None:
        for up, (R, m) in cand.items():
            _apply_verdict(m, verdicts.get(up))
    R0 = cand[up_axis(R0)][0]
    cur = cand[up_axis(R0)][1]
    # ORIENT overrides are the user's choice: only revisit them if they float
    # (or cannot be sliced at all because of an empty layer)
    problem = _bad(cur) or (cur["on_edge"] and not hard_override)
    best_R, best = R0, cur
    if problem:
        for up, (R, m) in cand.items():
            if compare(m, best) < 0:
                best_R, best = R, m
        # if no pose avoids supports, keep the current one (the ORIENT
        # flat-lay choice or the modelled studs-up pose) so the layout stays
        # recognisable; an override is likewise only replaced by a candidate
        # that needs no support and does not stand on an edge
        cur_empty = cur.get("slicer_empty", int(cur["empty_layers"] > 0))
        if (best_R is not R0 and not cur_empty
                and (_bad(best) or (hard_override and best["on_edge"]))):
            best_R, best = R0, cur
    reason = "kept"
    if best_R is not R0:
        reason = ("override floated: " if hard_override else "") + (
            "empty layer" if cur.get("slicer_empty", cur["empty_layers"] > 0)
            else "floating" if _bad(cur) else "stood on an edge")
    elif problem:
        reason = "no better orientation"
    return {
        "R": best_R,
        "changed": best_R is not R0,
        "before": cur,
        "after": best,
        "needs_support": _bad(best),
        "reason": reason,
        "candidates": {up: m for up, (R, m) in cand.items()},
    }
