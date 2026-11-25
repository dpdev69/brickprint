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
# Note: FLOAT_MIN_REGION_MM2 < FLOAT_OVERLAP_MM2 means tiny 4-connected regions
# with area in [0.1, 0.25) mm^2 cannot satisfy ov_area >= 0.25 even at 100 %
# overlap.  Kept unchanged so calibrated ORIENT_VERSION=2 verdicts stay stable.
FLOAT_MIN_REGION_MM2 = 0.1  # ignore raster specks
SLICE_X0 = -0.5 + 0.0123  # jittered raster origin (avoids exact grid ties)
SLICE_Y0 = -0.5 + 0.0071
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
    k1l, k2l = k1.tolist(), k2.tolist()
    start: dict[int, list[int]] = {}
    for i, k in enumerate(k1l):
        start.setdefault(k, []).append(i)
    used = bytearray(len(k1l))
    loops, opens = [], []
    for i in range(len(k1l)):
        if used[i]:
            continue
        used[i] = 1
        idxs = [i]
        k0, kc = k1l[i], k2l[i]
        while True:
            if kc == k0:
                loops.append(p1[idxs])
                break
            lst = start.get(kc)
            j = -1
            if lst:
                for cand in lst:
                    if not used[cand]:
                        j = cand
                        break
            if j < 0:
                poly = np.empty((len(idxs) + 1, 2), dtype=p1.dtype)
                poly[:-1] = p1[idxs]
                poly[-1] = p2[idxs[-1]]
                opens.append(poly)
                break
            used[j] = 1
            idxs.append(j)
            kc = k2l[j]

    def join(opens, tol):
        tol2 = tol * tol
        keep = []
        for p in opens:
            if len(p) >= 3:
                dx = float(p[0, 0] - p[-1, 0])
                dy = float(p[0, 1] - p[-1, 1])
                if dx * dx + dy * dy <= tol2:
                    loops.append(p)
                    continue
            keep.append(p)
        opens = keep
        m = len(opens)
        if m < 2:
            return opens
        E = np.array([p[-1] for p in opens])
        S = np.array([p[0] for p in opens])
        diff = E[:, None, :] - S[None, :, :]
        D2 = (diff * diff).sum(axis=2)
        np.fill_diagonal(D2, np.inf)
        alive = np.ones(m, dtype=bool)
        while True:
            flat_idx = int(np.argmin(D2))
            i, j = divmod(flat_idx, m)
            if D2[i, j] > tol2:
                break
            opens[i] = np.vstack([opens[i], opens[j]])
            E[i] = opens[i][-1]
            alive[j] = False
            D2[j, :] = np.inf
            D2[:, j] = np.inf
            dx = float(opens[i][0, 0] - E[i, 0])
            dy = float(opens[i][0, 1] - E[i, 1])
            if len(opens[i]) >= 3 and dx * dx + dy * dy <= tol2:
                loops.append(opens[i])
                alive[i] = False
                D2[i, :] = np.inf
                D2[:, i] = np.inf
            else:
                di = S - E[i]
                row = (di * di).sum(axis=1)
                row[~alive] = np.inf
                row[i] = np.inf
                D2[i, :] = row
        return [opens[k] for k in range(m) if alive[k]]

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


def _slice_params(W: np.ndarray, layer: float = LAYER, px: float | None = None):
    ext = W.max(axis=0)
    nl = max(int(np.ceil(ext[2] / layer - 0.5)), 1)
    if px is None:
        px = PX
        cells = (ext[0] / px + 10) * (ext[1] / px + 10) * nl
        if cells > MAX_CELLS:
            px = float(np.sqrt((ext[0] + 1) * (ext[1] + 1) * nl / MAX_CELLS))
    nx = int(np.ceil((ext[0] + 1.0) / px))
    ny = int(np.ceil((ext[1] + 1.0) / px))
    return nl, nx, ny, px


def _iter_layer_slices(W: np.ndarray, T: np.ndarray, nl: int, nx: int, ny: int,
                       layer: float, px: float, N_all: np.ndarray | None = None):
    """Yield (k, slice_bool_nx_ny) for k = 0..nl-1 using Z-interval triangle bucketing."""
    empty_slice = np.zeros((nx, ny), dtype=bool)
    if len(T) == 0:
        for k in range(nl):
            yield k, empty_slice
        return
    Vz = W[:, 2]
    Tz = Vz[T]
    zmin_t = Tz.min(axis=1)
    zmax_t = Tz.max(axis=1)
    if N_all is None:
        N_all = np.cross(W[T[:, 1]] - W[T[:, 0]], W[T[:, 2]] - W[T[:, 0]])
    z0_off = layer / 2 + 1.234e-5
    k_lo = np.maximum(0, np.ceil((zmin_t - z0_off) / layer - 1e-9).astype(np.int64))
    k_hi = np.minimum(nl - 1, np.floor((zmax_t - z0_off) / layer + 1e-9).astype(np.int64))
    counts = np.where(k_lo <= k_hi, k_hi - k_lo + 1, 0)
    tot_c = int(counts.sum())
    if tot_c == 0:
        for k in range(nl):
            yield k, empty_slice
        return
    t_idx = np.repeat(np.arange(len(T), dtype=np.int64), counts)
    k_idx = np.repeat(k_lo, counts) + (np.arange(tot_c, dtype=np.int64) - np.repeat(np.cumsum(counts) - counts, counts))
    order = np.argsort(k_idx, kind="mergesort")
    t_idx = t_idx[order]
    k_bounds = np.searchsorted(k_idx[order], np.arange(nl + 1))
    nverts = len(W)
    for k in range(nl):
        a0, a1 = int(k_bounds[k]), int(k_bounds[k + 1])
        if a0 == a1:
            yield k, empty_slice
            continue
        z = z0_off + k * layer
        fi = t_idx[a0:a1]
        F = T[fi]
        S = Vz[F] > z
        cnt = S.sum(axis=1)
        m = (cnt == 1) | (cnt == 2)
        if not m.any():
            yield k, empty_slice
            continue
        F = F[m]
        S = S[m]
        nf = N_all[fi[m]]
        cnt = cnt[m]
        # Lone vertex (the one whose side differs from the other two) directly
        # identifies both crossing edges without computing the third edge or sorting.
        lone = np.where(cnt == 1, np.argmax(S, axis=1), np.argmin(S, axis=1))
        rows = np.arange(len(F))
        u = F[rows, lone]
        v1 = F[rows, (lone + 1) % 3]
        v2 = F[rows, (lone + 2) % 3]
        t1 = (z - Vz[u]) / (Vz[v1] - Vz[u])
        t2 = (z - Vz[u]) / (Vz[v2] - Vz[u])
        p1 = W[u, :2] + t1[:, None] * (W[v1, :2] - W[u, :2])
        p2 = W[u, :2] + t2[:, None] * (W[v2, :2] - W[u, :2])
        k1 = np.minimum(u, v1).astype(np.int64) * nverts + np.maximum(u, v1)
        k2 = np.minimum(u, v2).astype(np.int64) * nverts + np.maximum(u, v2)
        d = p2 - p1
        sw = d[:, 0] * (-nf[:, 1]) + d[:, 1] * nf[:, 0] < 0
        seg = (
            np.where(sw, k2, k1),
            np.where(sw[:, None], p2, p1),
            np.where(sw, k1, k2),
            np.where(sw[:, None], p1, p2),
        )
        yield k, _raster(_chain(seg, GAP_CLOSE), SLICE_X0, SLICE_Y0, nx, ny, px)


def slice_stack(V: np.ndarray, T: np.ndarray, layer: float = LAYER, px: float | None = None):
    """Slice a (posed, min-shifted) mesh like the slicer: returns (mask, px).

    mask[x, y, k] is the filled area of layer k (z = layer/2 + k*layer).
    """
    W = np.asarray(V, dtype=np.float64)
    W = W - W.min(axis=0)
    T = np.asarray(T, dtype=np.int64)
    nl, nx, ny, px = _slice_params(W, layer=layer, px=px)
    mask_t = np.zeros((nl, nx, ny), dtype=bool)
    for k, sl in _iter_layer_slices(W, T, nl, nx, ny, layer, px):
        if sl.any():
            mask_t[k] = sl
    return np.ascontiguousarray(mask_t.transpose(1, 2, 0)), px


def _rle_2d_components(cur: np.ndarray):
    """4-connected components of a 2D bool slice via horizontal run-length union-find.

    Returns ``(sr, sc, ec, roots)`` where each run ``i`` spans ``cur[sr[i], sc[i]:ec[i]]``
    and ``roots[i]`` is its canonical union-find root index.
    """
    nx, ny = cur.shape
    padded = np.zeros((nx, ny + 2), dtype=np.int8)
    padded[:, 1:-1] = cur
    diff = np.diff(padded, axis=1)
    sr, sc = np.nonzero(diff == 1)
    _, ec = np.nonzero(diff == -1)
    nruns = len(sr)
    if nruns == 0:
        return sr, sc, ec, []
    parent = list(range(nruns))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    row_start = np.searchsorted(sr, np.arange(nx + 1)).tolist()
    sc_l = sc.tolist()
    ec_l = ec.tolist()
    for r in range(1, nx):
        i, i_end = row_start[r - 1], row_start[r]
        j, j_end = i_end, row_start[r + 1]
        while i < i_end and j < j_end:
            if ec_l[i] <= sc_l[j]:
                i += 1
            elif ec_l[j] <= sc_l[i]:
                j += 1
            else:
                ri, rj = find(i), find(j)
                if ri != rj:
                    if ri > rj:
                        ri, rj = rj, ri
                    parent[rj] = ri
                if ec_l[i] < ec_l[j]:
                    i += 1
                else:
                    j += 1
    roots = [find(i) for i in range(nruns)]
    return sr, sc, ec, roots


def _label_layers(mask: np.ndarray) -> np.ndarray:
    """4-connected components inside each XY layer (axis 2 = layer).

    Per-layer 2D run-length union-find; returns int32 labels (0 = background),
    equal within a component and disjoint across layers.
    """
    nx, ny, nz = mask.shape
    out_t = np.zeros((nz, nx, ny), dtype=np.int32)
    m_t = np.ascontiguousarray(mask.transpose(2, 0, 1))
    next_id = 1
    for k in range(nz):
        cur = m_t[k]
        if not cur.any():
            continue
        sr, sc, ec, roots = _rle_2d_components(cur)
        if not roots:
            continue
        id_map: dict[int, int] = {}
        layer_out = out_t[k]
        for r, c0, c1, rt in zip(sr.tolist(), sc.tolist(), ec.tolist(), roots):
            cid = id_map.get(rt)
            if cid is None:
                cid = next_id
                id_map[rt] = cid
                next_id += 1
            layer_out[r, c0:c1] = cid
    return np.ascontiguousarray(out_t.transpose(1, 2, 0))


def _slice_float_stats(cur: np.ndarray, prev: np.ndarray, pa: float) -> tuple[int, float, int, float]:
    """Return ``(float_n, float_area, self_float_n, self_float_area)`` for ``cur`` above ``prev``.

    ``self_float_*`` are the counts if ``cur`` were stacked on an identical copy
    of itself (used to skip RLE on repeated consecutive brick layers).
    """
    sr, sc, ec, roots = _rle_2d_components(cur)
    if not roots:
        return 0, 0.0, 0, 0.0
    nx, ny = cur.shape
    ov_cs = np.zeros((nx, ny + 1), dtype=np.int32)
    np.cumsum(cur & prev, axis=1, out=ov_cs[:, 1:])
    run_ov = (ov_cs[sr, ec] - ov_cs[sr, sc]).tolist()
    run_len = (ec - sc).tolist()
    comp_area: dict[int, int] = {}
    comp_ov: dict[int, int] = {}
    for rt, rlen, rov in zip(roots, run_len, run_ov):
        comp_area[rt] = comp_area.get(rt, 0) + rlen
        comp_ov[rt] = comp_ov.get(rt, 0) + rov
    fn = 0
    fa = 0.0
    sfn = 0
    sfa = 0.0
    for rt, cnt in comp_area.items():
        ra = cnt * pa
        if ra >= FLOAT_MIN_REGION_MM2:
            thresh = max(FLOAT_OVERLAP_MM2, 0.01 * ra)
            if comp_ov[rt] * pa < thresh:
                fn += 1
                fa += ra
            if ra < thresh:
                sfn += 1
                sfa += ra
    return fn, fa, sfn, sfa


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
    k0, k1 = int(nz[0]), int(nz[-1])
    empty = int((area[k0:k1 + 1] == 0).sum())
    pa = px * px
    m_t = np.ascontiguousarray(mask[:, :, k0:k1 + 1].transpose(2, 0, 1))
    float_n = 0
    float_area = 0.0
    float_zmin = None
    prev = m_t[0]
    prev_self: tuple[int, float] | None = None
    for dk in range(1, k1 - k0 + 1):
        cur = m_t[dk]
        if area[k0 + dk] == 0:
            prev = cur
            prev_self = (0, 0.0)
            continue
        if prev_self is not None and np.array_equal(cur, prev):
            fn, fa = prev_self
        else:
            fn, fa, sfn, sfa = _slice_float_stats(cur, prev, pa)
            prev_self = (sfn, sfa)
        if fn:
            float_n += fn
            float_area += fa
            if float_zmin is None:
                float_zmin = float((dk + k0) * LAYER + LAYER / 2)
        prev = cur
    return {
        "float_n": float_n,
        "float_area": float(float_area),
        "float_zmin": float_zmin if float_zmin is not None else 0.0,
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


def surface_metrics(V: np.ndarray, T: np.ndarray, *,
                    _N: np.ndarray | None = None, _area2: np.ndarray | None = None) -> dict:
    V = np.asarray(V, dtype=np.float64)
    V = V - V.min(axis=0)
    A, B, C = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    N = np.cross(B - A, C - A) if _N is None else _N
    area2 = np.linalg.norm(N, axis=1) if _area2 is None else _area2
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
    if down.any():
        w_down = np.where(_in_hull(cen[down, :2], fh), 0.5, 1.0)
        down_area = float((area[down] * w_down).sum())
    else:
        down_area = 0.0
    return {
        "contact_area": contact,
        "hull_area": hull_area,
        "contact_ratio": contact / hull_area if hull_area > 0 else 0.0,
        "down_area": down_area,
        "height": float(V[:, 2].max()),
        "size": (V.max(axis=0)).tolist(),
    }


def _stream_layer_report(W: np.ndarray, T: np.ndarray, layer: float = LAYER,
                         px: float | None = None, N_all: np.ndarray | None = None) -> tuple[dict, float]:
    """Streaming equivalent of ``layer_report(*slice_stack(W, T))`` using O(nx*ny) memory."""
    nl, nx, ny, px = _slice_params(W, layer=layer, px=px)
    pa = px * px
    seen_first = False
    first_area_px = 0
    trailing_empty = 0
    empty_between = 0
    float_n = 0
    float_area = 0.0
    float_zmin = None
    prev: np.ndarray | None = None
    prev_self: tuple[int, float] | None = None
    for k, cur in _iter_layer_slices(W, T, nl, nx, ny, layer, px, N_all=N_all):
        cur_px = int(cur.sum())
        if cur_px == 0:
            if seen_first:
                trailing_empty += 1
                prev = cur
                prev_self = (0, 0.0)
            continue
        if not seen_first:
            seen_first = True
            first_area_px = cur_px
            prev = cur
            prev_self = None
            continue
        if trailing_empty:
            empty_between += trailing_empty
            trailing_empty = 0
        if prev_self is not None and np.array_equal(cur, prev):
            fn, fa = prev_self
        else:
            fn, fa, sfn, sfa = _slice_float_stats(cur, prev, pa)
            prev_self = (sfn, sfa)
        if fn:
            float_n += fn
            float_area += fa
            if float_zmin is None:
                float_zmin = float(k * layer + layer / 2)
        prev = cur
    if not seen_first:
        return {"float_n": 0, "float_area": 0.0, "float_zmin": 0.0, "empty_layers": 0,
                "first_layer_area": 0.0}, px
    return {
        "float_n": float_n,
        "float_area": float(float_area),
        "float_zmin": float_zmin if float_zmin is not None else 0.0,
        "empty_layers": empty_between,
        "first_layer_area": float(first_area_px * pa),
    }, px


def evaluate(V, T, R, *, _N0: np.ndarray | None = None, _area2: np.ndarray | None = None) -> dict:
    Rm = np.asarray(R, dtype=float)
    W = np.asarray(V, dtype=np.float64) @ Rm.T
    W = W - W.min(axis=0)
    T64 = np.asarray(T, dtype=np.int64)
    N = (_N0 @ Rm.T) if _N0 is not None else np.cross(W[T64[:, 1]] - W[T64[:, 0]], W[T64[:, 2]] - W[T64[:, 0]])
    m = surface_metrics(W, T64, _N=N, _area2=_area2)
    lrep, px = _stream_layer_report(W, T64, N_all=N)
    m.update(lrep)
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
    N0 = np.cross(V[T[:, 1]] - V[T[:, 0]], V[T[:, 2]] - V[T[:, 0]]) if len(T) else np.zeros((0, 3))
    area2 = np.linalg.norm(N0, axis=1)
    up0 = up_axis(R0)
    cand = {}
    for R in ROTATIONS:
        up = up_axis(R)
        if up == up0:
            R = R0
        if up not in cand:
            cand[up] = (R, evaluate(V, T, R, _N0=N0, _area2=area2))
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
