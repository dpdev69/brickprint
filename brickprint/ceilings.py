"""Socket-ceiling recipe ("recipe 8"): deeper plate sockets, anchored thick
bridges and span-cutting ribs.

Ported from the print-proven Seagull scripts (seagull/build_ceiling_v6.py and
seagull/build_bridges_v8.py; variant 8 of the v7 one-print ceiling test):

1. RAISE    plate-type socket ceilings (reachable by a 1.7 mm stud) move up to
            2.2 mm above the bed (real LEGO depth), so a thick bridge strand
            can hang without blocking a stud.  Refused when ambiguous.
2. BANDS    per bridged ceiling two height ranges: the bridge layer at 0.24 mm
            with 2 wall loops (strand ends land on the wall tops) and a second
            0.24 mm layer.  Refused where a band would touch a fit surface.
3. RIBS     0.8 mm ribs across the cavity, only where a free strand span
            exceeds 7 mm, 0.24 mm deep below the ceiling, never over a stud.

Needs thick_bridges=1 in the process profile and one Bambu object per part
(height ranges are per object).  Pure Python on vertex/triangle lists; the
public entry point is :func:`plan`.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

CEILINGS_VERSION = 1

# ---- ceiling raise (build_ceiling_v6) ----
TARGET_CEILING = 2.2      # mm above the bed: real LEGO plate cavity depth (stud 1.7 + headroom)
MIN_CEILING = 0.8         # ceilings lower than this are grooves (tile / cheese bottom groove at 0.4)
MIN_AREA = 2.0            # mm2 of ceiling at one level to count as a socket ceiling
MIN_SKIN = 0.8            # mm of solid material that must remain above a raised ceiling
STUD_REACH = 1.7          # mm: only ceilings a stud can touch (real LEGO stud height) are candidates
EDGE_MARGIN = 0.3         # mm: ceiling vertices closer than this to the bed footprint hull are "on the outline"
OPEN_NOTCHES = True       # allow ceilings that reach the outline through open notches (LDraw wedge plates)
MAX_EDGE_FRACTION = 0.15  # ... but only if at most this share of the ceiling area touches the outline
BRIDGE_LAYER = 0.24       # mm, layer height of the first layer above a raised ceiling (0 = off)
ZTOL = 1e-4

def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def tri_normal(V, t):
    a, b, c = V[t[0]], V[t[1]], V[t[2]]
    n = cross(sub(b, a), sub(c, a))
    L = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
    return n, L


VERTICAL_TOL = 0.01       # mm: a face whose XY projection is a sliver thinner than this is "vertical"


def xy_width(V, t):
    """width of the face's XY projection (0 for an exactly vertical face)."""
    (x1, y1, _), (x2, y2, _), (x3, y3, _) = V[t[0]], V[t[1]], V[t[2]]
    a2 = abs((x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1))
    L = max(math.hypot(x2 - x1, y2 - y1), math.hypot(x3 - x2, y3 - y2), math.hypot(x1 - x3, y1 - y3))
    return a2 / L if L > 1e-12 else 0.0


def hull2d(P):
    P = sorted(set((round(x, 6), round(y, 6)) for x, y in P))
    if len(P) < 3:
        return P

    def cr(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for p in P:
        while len(lo) >= 2 and cr(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(P):
        while len(hi) >= 2 and cr(hi[-2], hi[-1], p) <= 0:
            hi.pop()
        hi.append(p)
    return lo[:-1] + hi[:-1]


def dist_inside_hull(H, p):
    """signed distance from p to the hull boundary (positive inside, CCW hull)."""
    d = 1e9
    for i in range(len(H)):
        a, b = H[i], H[(i + 1) % len(H)]
        ex, ey = b[0] - a[0], b[1] - a[1]
        L = math.hypot(ex, ey)
        if L < 1e-12:
            continue
        s = (ex * (p[1] - a[1]) - ey * (p[0] - a[0])) / L   # left of edge = inside (CCW)
        d = min(d, s)
    return d


def bary_z(V, t, x, y):
    """z of triangle t above (x, y) if (x, y) is inside its XY projection, else None."""
    (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = V[t[0]], V[t[1]], V[t[2]]
    det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if abs(det) < 1e-12:
        return None
    l1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / det
    l2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / det
    l3 = 1 - l1 - l2
    e = -1e-9
    if l1 < e or l2 < e or l3 < e:
        return None
    return l1 * z1 + l2 * z2 + l3 * z3


def in_tri_xy(V, t, x, y):
    return bary_z(V, t, x, y) is not None


# --------------------------------------------------------------------------
# the ceiling rule


def analyse(V, T, target=TARGET_CEILING):
    """Decide whether / how to raise this mesh's socket ceiling.

    Returns dict(status=raise|unchanged|refused, reason, level, d, moved{idx: z}, ...).
    All z values in the report are relative to the bed (mesh zmin)."""
    zmin = min(v[2] for v in V)
    rel = lambda z: z - zmin  # noqa: E731
    horiz_down = defaultdict(list)    # level -> faces
    horiz_up = defaultdict(list)
    tri_areas = [0.0] * len(T)
    for fi, t in enumerate(T):
        zs = (V[t[0]][2], V[t[1]][2], V[t[2]][2])
        if max(zs) - min(zs) > ZTOL:
            continue
        n, L = tri_normal(V, t)
        if L < 1e-12:
            continue
        tri_areas[fi] = L / 2.0
        lvl = round((zs[0] + zs[1] + zs[2]) / 3.0, 4)
        (horiz_down if n[2] < 0 else horiz_up)[lvl].append(fi)

    def area(fs):
        return sum(tri_areas[f] for f in fs)

    levels = sorted(horiz_down)
    level_areas = {l: area(horiz_down[l]) for l in levels}
    socket_levels = [l for l in levels if rel(l) > MIN_CEILING - ZTOL and level_areas[l] >= MIN_AREA]
    out = {"zmin": zmin, "height": max(v[2] for v in V) - zmin,
           "ceilings": [(round(rel(l), 3), round(level_areas[l], 2)) for l in levels if rel(l) > ZTOL]}
    cand = [l for l in socket_levels if rel(l) <= STUD_REACH + ZTOL and rel(l) < target - 0.05]
    if not cand:
        deep = [round(rel(l), 3) for l in socket_levels]
        out.update(status="unchanged",
                   reason=(f"lowest ceiling {min(deep)} mm is above stud reach {STUD_REACH} mm "
                           f"(deep socket / not a stud socket)" if deep
                           else "no socket ceiling >= %.1f mm2 between %.1f and %.1f mm" % (MIN_AREA, MIN_CEILING, STUD_REACH)))
        return out
    if len(cand) > 1:
        out.update(status="refused", reason=f"several low ceiling levels {[round(rel(c), 3) for c in cand]}")
        return out
    c = cand[0]
    d = target - rel(c)
    newz = c + d
    down_c = horiz_down[c]
    down_c_area = level_areas[c]
    out.update(level=round(rel(c), 4), d=round(d, 4), ceiling_area=round(down_c_area, 2))
    M = {i for i, v in enumerate(V) if abs(v[2] - c) < ZTOL}
    # Index ceiling faces upfront so upward-face, slab-vertex, and skin checks all
    # query O(1) cell buckets instead of scanning all of down_c.
    dgrid = face_grid(V, T, down_c)
    # bed footprint hull
    H = hull2d([(v[0], v[1]) for v in V if abs(v[2] - zmin) < ZTOL])
    if len(H) >= 3:
        a2 = sum(H[i][0] * H[(i + 1) % len(H)][1] - H[(i + 1) % len(H)][0] * H[i][1] for i in range(len(H)))
        if a2 < 0:
            H = H[::-1]
    problems = []
    edge_faces = [f for f in down_c if min(dist_inside_hull(H, V[i][:2]) for i in T[f]) < EDGE_MARGIN - 1e-6]
    edge_area = area(edge_faces)
    # outline length of the open notches: ceiling edges lying on the hull boundary
    notch_len = 0.0
    for f in edge_faces:
        t = T[f]
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            if abs(dist_inside_hull(H, V[a][:2])) < 1e-3 and abs(dist_inside_hull(H, V[b][:2])) < 1e-3:
                notch_len += math.hypot(V[a][0] - V[b][0], V[a][1] - V[b][1])
    out.update(edge_area=round(edge_area, 2), notch_outline=round(notch_len, 2))
    if edge_faces and not OPEN_NOTCHES:
        problems.append(f"{len(edge_faces)} ceiling faces reach the footprint outline (open notches)")
    elif edge_faces and edge_area > MAX_EDGE_FRACTION * down_c_area:
        problems.append(f"{edge_area:.1f} of {down_c_area:.1f} mm2 of ceiling touches the outline")
    above_faces = []
    for fi, t in enumerate(T):
        k = (t[0] in M) + (t[1] in M) + (t[2] in M)
        if k == 0:
            if max(V[t[0]][2], V[t[1]][2], V[t[2]][2]) > c + ZTOL and xy_width(V, t) > VERTICAL_TOL:
                above_faces.append(fi)
            continue
        if k < 3 and max(V[t[0]][2], V[t[1]][2], V[t[2]][2]) > c + ZTOL and xy_width(V, t) > VERTICAL_TOL:
            above_faces.append(fi)
        n, L = tri_normal(V, t)
        if k == 3:
            if L < 1e-12:
                continue                      # degenerate face lying in the plane: stays in it
            cx = (V[t[0]][0] + V[t[1]][0] + V[t[2]][0]) / 3.0
            cy = (V[t[0]][1] + V[t[1]][1] + V[t[2]][1]) / 3.0
            if n[2] > 0:
                cell_d = dgrid.get((int(math.floor(cx)), int(math.floor(cy))), ())
                if not any(in_tri_xy(V, T[g], cx, cy) for g in cell_d):
                    problems.append(f"upward face {fi} at the ceiling level is not under the ceiling (exterior step)")
            continue
        if L < 1e-12:
            # degenerate sliver: must stay degenerate after the move
            P = [(V[i][0], V[i][1], newz if i in M else V[i][2]) for i in t]
            nn = cross(sub(P[1], P[0]), sub(P[2], P[0]))
            if math.sqrt(sum(x * x for x in nn)) > 1e-9:
                problems.append(f"degenerate face {fi} would open up")
            continue
        if xy_width(V, t) > VERTICAL_TOL:
            problems.append(f"sloped face {fi} (nz={n[2] / L:.3f}) touches the ceiling level")
            continue
        for i in t:
            if i not in M and c - ZTOL <= V[i][2] <= newz + ZTOL:
                problems.append(f"vertical face {fi} has vertex at z={rel(V[i][2]):.3f} inside the raised slab")
    # any other vertex inside the slab above the ceiling region?
    for i, v in enumerate(V):
        if i in M or not (c + ZTOL < v[2] <= newz + MIN_SKIN):
            continue
        cell_d = dgrid.get((int(math.floor(v[0])), int(math.floor(v[1]))), ())
        if any(in_tri_xy(V, T[g], v[0], v[1]) for g in cell_d):
            problems.append(f"vertex {i} at z={rel(v[2]):.3f} lies within {MIN_SKIN} mm above the new ceiling")
            break
    # top skin: vertical rays from the ceiling region to the next surface above
    Vnp = np.asarray(V, dtype=np.float64)
    Tnp = np.asarray(T, dtype=np.int64)
    Fd = Tnp[down_c]
    xs_min, xs_max = float(Vnp[Fd, 0].min()), float(Vnp[Fd, 0].max())
    ys_min, ys_max = float(Vnp[Fd, 1].min()), float(Vnp[Fd, 1].max())
    # Bounded sampling density: 0.2 mm on small parts, coarser on large plates.
    _sx, _sy = xs_max - xs_min, ys_max - ys_min
    step = max(0.2, math.sqrt(max(_sx * _sy, 1e-9) / 40000.0))
    gx = np.arange(xs_min + step / 2, xs_max, step)
    gy = np.arange(ys_min + step / 2, ys_max, step)
    nx_s, ny_s = len(gx), len(gy)
    nsamp = 0
    skin = 1e9
    if nx_s > 0 and ny_s > 0:
        ceil_mask = _rasterize_faces_grid(Vnp, Fd, gx, gy, step)
        nsamp = int(ceil_mask.sum())
        if nsamp > 0:
            min_z = _rasterize_min_z_grid(Vnp, Tnp[above_faces], gx, gy, step, c + ZTOL) if above_faces else np.full((nx_s, ny_s), 1e9)
            skin = float(min_z[ceil_mask].min() - newz)
    if nsamp == 0 and down_c:
        # Fallback for very narrow ceilings that fall between grid sample lines:
        # probe every ceiling triangle centroid directly so skin never stays 1e9.
        grid = face_grid(V, T, above_faces)
        for g in down_c:
            t = T[g]
            cx = (V[t[0]][0] + V[t[1]][0] + V[t[2]][0]) / 3.0
            cy = (V[t[0]][1] + V[t[1]][1] + V[t[2]][1]) / 3.0
            nsamp += 1
            best = 1e9
            for fi in grid.get((int(math.floor(cx)), int(math.floor(cy))), ()):
                z = bary_z(V, T[fi], cx, cy)
                if z is not None and z > c + ZTOL:
                    best = min(best, z)
            skin = min(skin, best - newz)
    out.update(skin=round(skin, 3), skin_samples=nsamp, skin_step=round(step, 3),
               moved_vertices=len(M))
    if skin < MIN_SKIN - 1e-6:
        problems.append(f"top skin {skin:.3f} mm < {MIN_SKIN} mm")
    if problems:
        out.update(status="refused", reason="; ".join(sorted(set(problems))[:3]) +
                   (f" (+{len(set(problems)) - 3} more)" if len(set(problems)) > 3 else ""))
        return out
    out.update(status="raise", reason=f"ceiling {rel(c):.3f} -> {target:.3f} mm" +
               (f"; open notches on the outline ({notch_len:.1f} mm) become {d:.2f} mm taller" if notch_len > 0 else ""),
               moved={i: newz for i in M})
    return out


def _rasterize_faces_grid(Vnp: np.ndarray, Fnp: np.ndarray, gx: np.ndarray,
                          gy: np.ndarray, step: float) -> np.ndarray:
    """Boolean mask (len(gx), len(gy)) of grid points inside any triangle of ``Fnp``."""
    nx, ny = len(gx), len(gy)
    out = np.zeros((nx, ny), dtype=bool)
    if len(Fnp) == 0 or nx == 0 or ny == 0:
        return out
    x0, y0 = float(gx[0]), float(gy[0])
    x1, y1 = Vnp[Fnp[:, 0], 0], Vnp[Fnp[:, 0], 1]
    x2, y2 = Vnp[Fnp[:, 1], 0], Vnp[Fnp[:, 1], 1]
    x3, y3 = Vnp[Fnp[:, 2], 0], Vnp[Fnp[:, 2], 1]
    det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    good = np.abs(det) >= 1e-12
    tx_lo = np.minimum(np.minimum(x1, x2), x3)
    tx_hi = np.maximum(np.maximum(x1, x2), x3)
    ty_lo = np.minimum(np.minimum(y1, y2), y3)
    ty_hi = np.maximum(np.maximum(y1, y2), y3)
    i0 = np.maximum(0, np.floor((tx_lo - x0) / step).astype(np.int64))
    i1 = np.minimum(nx - 1, np.ceil((tx_hi - x0) / step).astype(np.int64))
    j0 = np.maximum(0, np.floor((ty_lo - y0) / step).astype(np.int64))
    j1 = np.minimum(ny - 1, np.ceil((ty_hi - y0) / step).astype(np.int64))
    invd = np.where(good, 1.0 / np.where(good, det, 1.0), 0.0)
    for k in np.nonzero(good)[0].tolist():
        ii0, ii1 = int(i0[k]), int(i1[k]) + 1
        jj0, jj1 = int(j0[k]), int(j1[k]) + 1
        if ii0 >= ii1 or jj0 >= jj1:
            continue
        dx = gx[ii0:ii1, None] - x3[k]
        dy = gy[None, jj0:jj1] - y3[k]
        iv = invd[k]
        l1 = ((y2[k] - y3[k]) * dx + (x3[k] - x2[k]) * dy) * iv
        l2 = ((y3[k] - y1[k]) * dx + (x1[k] - x3[k]) * dy) * iv
        out[ii0:ii1, jj0:jj1] |= (l1 >= -1e-9) & (l2 >= -1e-9) & (1.0 - l1 - l2 >= -1e-9)
    return out


def _rasterize_min_z_grid(Vnp: np.ndarray, Fnp: np.ndarray, gx: np.ndarray,
                          gy: np.ndarray, step: float, z_thresh: float) -> np.ndarray:
    """Minimum triangle z > ``z_thresh`` over each point of grid ``(gx, gy)``."""
    nx, ny = len(gx), len(gy)
    min_z = np.full((nx, ny), 1e9, dtype=np.float64)
    if len(Fnp) == 0 or nx == 0 or ny == 0:
        return min_z
    x0, y0 = float(gx[0]), float(gy[0])
    x1, y1, z1 = Vnp[Fnp[:, 0], 0], Vnp[Fnp[:, 0], 1], Vnp[Fnp[:, 0], 2]
    x2, y2, z2 = Vnp[Fnp[:, 1], 0], Vnp[Fnp[:, 1], 1], Vnp[Fnp[:, 1], 2]
    x3, y3, z3 = Vnp[Fnp[:, 2], 0], Vnp[Fnp[:, 2], 1], Vnp[Fnp[:, 2], 2]
    det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    good = np.abs(det) >= 1e-12
    tx_lo = np.minimum(np.minimum(x1, x2), x3)
    tx_hi = np.maximum(np.maximum(x1, x2), x3)
    ty_lo = np.minimum(np.minimum(y1, y2), y3)
    ty_hi = np.maximum(np.maximum(y1, y2), y3)
    i0 = np.maximum(0, np.floor((tx_lo - x0) / step).astype(np.int64))
    i1 = np.minimum(nx - 1, np.ceil((tx_hi - x0) / step).astype(np.int64))
    j0 = np.maximum(0, np.floor((ty_lo - y0) / step).astype(np.int64))
    j1 = np.minimum(ny - 1, np.ceil((ty_hi - y0) / step).astype(np.int64))
    invd = np.where(good, 1.0 / np.where(good, det, 1.0), 0.0)
    for k in np.nonzero(good)[0].tolist():
        ii0, ii1 = int(i0[k]), int(i1[k]) + 1
        jj0, jj1 = int(j0[k]), int(j1[k]) + 1
        if ii0 >= ii1 or jj0 >= jj1:
            continue
        dx = gx[ii0:ii1, None] - x3[k]
        dy = gy[None, jj0:jj1] - y3[k]
        iv = invd[k]
        l1 = ((y2[k] - y3[k]) * dx + (x3[k] - x2[k]) * dy) * iv
        l2 = ((y3[k] - y1[k]) * dx + (x1[k] - x3[k]) * dy) * iv
        l3 = 1.0 - l1 - l2
        inside = (l1 >= -1e-9) & (l2 >= -1e-9) & (l3 >= -1e-9)
        if not inside.any():
            continue
        z = l1 * z1[k] + l2 * z2[k] + l3 * z3[k]
        valid = inside & (z > z_thresh)
        if valid.any():
            sub = min_z[ii0:ii1, jj0:jj1]
            min_z[ii0:ii1, jj0:jj1] = np.where(valid & (z < sub), z, sub)
    return min_z


# --------------------------------------------------------------------------
# project editing
# --------------------------------------------------------------------------


# ---- bands and ribs (build_bridges_v8) ----
BAND_LAYER = 0.24          # mm, both band layers
BAND_ANCHOR_WALLS = "2"    # wall_loops on the bridge layer only
RIB_W = 0.8                # mm (two 0.4 lines)
RIB_DEPTH = 0.24           # mm below the ceiling (v7: 1.96 .. 2.20)
RIB_INTO_CEILING = 0.05    # mm, rib top overlaps the solid ceiling (union)
RIB_INTO_WALL = 0.2        # mm past the mesh wall face (v7 2x2: cavity face 6.40 -> rib end 6.60)
RIB_SPAN_MAX = 7.0         # mm: ribs only where the worst free span is longer
STUD_H_REAL = 1.7          # mm, real LEGO stud height
STUD_CLEAR = 0.2           # mm, required clearance above a 1.7 mm stud
STUD_R = 2.4               # mm, stud radius
RIB_STUD_MARGIN = 0.3      # mm
SOCKET_CLUTCH_TOP = 1.8    # mm, socket walls that grip a stud
STUD_TOP_H = 1.6           # mm, top stud height in the LDraw meshes
BAND_ZONE_TOL = 0.04       # mm a band layer may overlap a fit-surface zone (half a 0.08 layer)
BRIDGE_MIN_R = 1.0         # mm, inscribed radius for a ceiling to be bridged (else overhang walls)
PROBE_DZ = 0.05            # mm below the ceiling where the cavity is sampled
STRAND_PITCH = 0.45        # mm, sampling pitch of the span estimate (v6 G-code pitch 0.455)


def box(x0, x1, y0, y1, z0, z1):
    V = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
    T = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
         (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    return V, T


def slice_segments(V, T, z):
    """segments of the mesh cross-section at plane z: [(x1, y1, x2, y2)]"""
    if not len(T):
        return []
    Vnp = np.asarray(V, dtype=np.float64)
    Tnp = np.asarray(T, dtype=np.int64)
    Vz = Vnp[:, 2]
    S = Vz[Tnp] > z
    cnt = S.sum(axis=1)
    m = (cnt == 1) | (cnt == 2)
    if not m.any():
        return []
    F = Tnp[m]
    Sm = S[m]
    cm = cnt[m]
    lone = np.where(cm == 1, np.argmax(Sm, axis=1), np.argmin(Sm, axis=1))
    rows = np.arange(len(F))
    u = F[rows, lone]
    v1 = F[rows, (lone + 1) % 3]
    v2 = F[rows, (lone + 2) % 3]
    s1 = (z - Vz[u]) / (Vz[v1] - Vz[u])
    s2 = (z - Vz[u]) / (Vz[v2] - Vz[u])
    x1 = Vnp[u, 0] + s1 * (Vnp[v1, 0] - Vnp[u, 0])
    y1 = Vnp[u, 1] + s1 * (Vnp[v1, 1] - Vnp[u, 1])
    x2 = Vnp[u, 0] + s2 * (Vnp[v2, 0] - Vnp[u, 0])
    y2 = Vnp[u, 1] + s2 * (Vnp[v2, 1] - Vnp[u, 1])
    return list(zip(x1.tolist(), y1.tolist(), x2.tolist(), y2.tolist()))


def line_intervals(segs, axis, coord):
    """material intervals along the line {axis-coordinate = coord} (axis 'x': line x = coord, runs in y).
    Parity of crossings with the cross-section segments."""
    c = coord + 1.234e-6
    xs = []
    for x1, y1, x2, y2 in segs:
        if axis == "x":
            a1, a2, b1, b2 = x1, x2, y1, y2
        else:
            a1, a2, b1, b2 = y1, y2, x1, x2
        if (a1 - c) * (a2 - c) < 0:
            xs.append(b1 + (c - a1) / (a2 - a1) * (b2 - b1))
    xs.sort()
    # merge coincident crossings (shared edges of coplanar triangles counted twice)
    m = []
    for v in xs:
        if m and abs(v - m[-1]) < 1e-7:
            m.pop()
            continue
        m.append(v)
    if len(m) % 2:
        m = m[:-1]
    return [(m[i], m[i + 1]) for i in range(0, len(m), 2)]


def in_faces(V, T, faces, x, y, grid=None):
    cand = faces if grid is None else grid.get((int(math.floor(x)), int(math.floor(y))), ())
    for f in cand:
        (x1, y1, _), (x2, y2, _), (x3, y3, _) = V[T[f][0]], V[T[f][1]], V[T[f][2]]
        det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        if abs(det) < 1e-12:
            continue
        l1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / det
        l2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / det
        if l1 >= -1e-9 and l2 >= -1e-9 and 1 - l1 - l2 >= -1e-9:
            return True
    return False


def face_grid(V, T, faces):
    g = defaultdict(list)
    for f in faces:
        xs = [V[i][0] for i in T[f]]
        ys = [V[i][1] for i in T[f]]
        for gx in range(int(math.floor(min(xs))), int(math.floor(max(xs))) + 1):
            for gy in range(int(math.floor(min(ys))), int(math.floor(max(ys))) + 1):
                g[(gx, gy)].append(f)
    return g


def ceiling_levels(V, T):
    """{bed z: [face idx]} of downward horizontal faces (area >= MIN_AREA, z >= MIN_CEILING)."""
    zmin = min(v[2] for v in V)
    lv = defaultdict(list)
    for fi, t in enumerate(T):
        zs = [V[i][2] for i in t]
        if max(zs) - min(zs) > ZTOL:
            continue
        n, L = tri_normal(V, t)
        if L < 1e-12 or n[2] >= 0:
            continue
        lv[round(sum(zs) / 3 - zmin, 4)].append(fi)
    out = {}
    for z, fs in lv.items():
        area = sum(tri_normal(V, T[f])[1] / 2 for f in fs)
        if z >= MIN_CEILING - ZTOL and area >= MIN_AREA:
            out[z] = fs
    return dict(sorted(out.items()))


def inscribed_radius(V, T, faces, step=0.2):
    """Approximate radius of the largest disc inside the union of `faces`.

    Rasterise at ``step`` (0.2 mm keeps the 1.0 mm bridge decision stable)
    and compute the exact 8-neighbour Chebyshev distance from the 4-neighbour
    boundary via iterative 3x3 NumPy boolean erosion.
    """
    if not faces:
        return 0.0, 0.0
    Vnp = np.asarray(V, dtype=np.float64)
    Tnp = np.asarray(T, dtype=np.int64)
    Fnp = Tnp[faces]
    xs = Vnp[Fnp, 0]
    ys = Vnp[Fnp, 1]
    x0 = float(xs.min()) - step
    y0 = float(ys.min()) - step
    nx = int((float(xs.max()) - x0) / step) + 2
    ny = int((float(ys.max()) - y0) / step) + 2
    gx = x0 + (np.arange(nx) + 0.5) * step
    gy = y0 + (np.arange(ny) + 0.5) * step
    inside = _rasterize_faces_grid(Vnp, Fnp, gx, gy, step)
    area_px = int(inside.sum())
    if area_px == 0:
        return 0.0, 0.0
    # 4-neighbour erosion removes the dist=0 border cells; subsequent 3x3
    # 8-neighbour erosions peel dist=1, 2, ... layers identically to BFS.
    pad = np.pad(inside, 1, constant_values=False)
    cur = pad[1:-1, 1:-1] & pad[:-2, 1:-1] & pad[2:, 1:-1] & pad[1:-1, :-2] & pad[1:-1, 2:]
    best_cells = 0
    while cur.any():
        best_cells += 1
        p = np.pad(cur, 1, constant_values=False)
        h = p[:, :-2] & p[:, 1:-1] & p[:, 2:]
        cur = h[:-2, :] & h[1:-1, :] & h[2:, :]
    return (best_cells + 0.5) * step, area_px * step * step


def _base_free_spans(V, T, faces, segs, g, strand_axis):
    """Compute base free intervals [(line_coord, [(a1, b0), ...])] with no ribs."""
    pts = [V[i] for f in faces for i in T[f]]
    across = "y" if strand_axis == "x" else "x"
    lo = min(p[1] if across == "y" else p[0] for p in pts)
    hi = max(p[1] if across == "y" else p[0] for p in pts)
    out = []
    c = lo + STRAND_PITCH / 2
    while c < hi:
        M = line_intervals(segs, across, c)
        M.sort()
        mm = []
        for a, b in M:
            if mm and a <= mm[-1][1] + 1e-9:
                mm[-1] = (mm[-1][0], max(mm[-1][1], b))
            else:
                mm.append((a, b))
        free = []
        for (a0, a1), (b0, b1) in zip(mm, mm[1:]):
            mid = (a1 + b0) / 2
            px, py = (mid, c) if strand_axis == "x" else (c, mid)
            if in_faces(V, T, faces, px, py, g):
                free.append((a1, b0))
        if free:
            out.append((c, free))
        c += STRAND_PITCH
    return out


def _spans_with_ribs(base_lines, strand_axis, ribs=()):
    """Split cached base free intervals by ``ribs`` -> [(line_coord, [span_lengths])]."""
    if not ribs:
        return [(lc, [b - a for a, b in ivs]) for lc, ivs in base_lines]
    axis_ribs = [(r["lo"] - 1e-9, r["hi"] + 1e-9, r["c"] - RIB_W / 2, r["c"] + RIB_W / 2)
                 for r in ribs if r["axis"] == strand_axis]
    if not axis_ribs:
        return [(lc, [b - a for a, b in ivs]) for lc, ivs in base_lines]
    out = []
    for lc, free_iv in base_lines:
        active = [(ra, rb) for rlo, rhi, ra, rb in axis_ribs if rlo <= lc <= rhi]
        if not active:
            out.append((lc, [b - a for a, b in free_iv]))
            continue
        cur = free_iv
        for ra, rb in active:
            nxt = []
            for a, b in cur:
                if rb <= a + 1e-9 or ra >= b - 1e-9:
                    nxt.append((a, b))
                else:
                    if ra > a + 1e-9:
                        nxt.append((a, ra))
                    if rb < b - 1e-9:
                        nxt.append((rb, b))
            cur = nxt
            if not cur:
                break
        if cur:
            out.append((lc, [b - a for a, b in cur]))
    return out


def spans(V, T, faces, zc, zmin, strand_axis, ribs=(), *, _segs=None, _grid=None):
    """free strand spans under the ceiling at local z zc: strands along strand_axis ('x' or 'y'),
    sampled every STRAND_PITCH.  Returns [(line coord, [free span lengths])]."""
    segs = slice_segments(V, T, zc - PROBE_DZ) if _segs is None else _segs
    g = face_grid(V, T, faces) if _grid is None else _grid
    base = _base_free_spans(V, T, faces, segs, g, strand_axis)
    return _spans_with_ribs(base, strand_axis, ribs)


def rib_extent(V, T, faces, zc, axis, c, *, _segs=None, _grid=None):
    """rib along the line {axis = c} (axis 'x': rib runs in y at x = c).  Returns (lo, hi, notes) in
    the running coordinate, or (None, None, reason)."""
    segs = slice_segments(V, T, zc - PROBE_DZ) if _segs is None else _segs
    g = face_grid(V, T, faces) if _grid is None else _grid
    lines = [c - RIB_W / 2 + 1e-3, c, c + RIB_W / 2 - 1e-3]
    los, his, outer_lo, outer_hi = [], [], [], []
    for lc in lines:
        M = line_intervals(segs, axis, lc)
        if not M:
            return None, None, f"no material on line {axis}={lc:.2f}"
        air = []
        for (a0, a1), (b0, b1) in zip(M, M[1:]):
            mid = (a1 + b0) / 2
            px, py = (lc, mid) if axis == "x" else (mid, lc)
            air.append((a1, b0, in_faces(V, T, faces, px, py, g)))
        under = [a for a in air if a[2]]
        if not under:
            continue
        lo, hi = under[0][0], under[-1][1]
        # outside air (notch) between the first and last cavity span?
        for a0, a1, u in air:
            if not u and a0 >= lo - 1e-9 and a1 <= hi + 1e-9:
                return None, None, f"outside air {a0:.2f}..{a1:.2f} on line {axis}={lc:.2f}"
        # walls: the material interval containing lo / hi
        wl = [m for m in M if m[0] - 1e-9 <= lo <= m[1] + 1e-9][0]
        wh = [m for m in M if m[0] - 1e-9 <= hi <= m[1] + 1e-9][0]
        los.append(lo)
        his.append(hi)
        outer_lo.append(wl[0])
        outer_hi.append(wh[1])
    if not los:
        return None, None, "line not under the ceiling"
    lo = min(los) - RIB_INTO_WALL
    hi = max(his) + RIB_INTO_WALL
    if lo < max(outer_lo) + 0.2 or hi > min(outer_hi) - 0.2:
        return None, None, f"rib end would reach the outline (walls {max(outer_lo):.2f}/{min(outer_hi):.2f})"
    return lo, hi, f"cavity {min(los):.2f}..{max(his):.2f}, walls to {max(outer_lo):.2f}/{min(outer_hi):.2f}"


# --------------------------------------------------------------------------
# per-part rules
# --------------------------------------------------------------------------

def grid_floor(z, first=0.2, lh=0.08):
    k = math.floor((z - first) / lh + 1e-6)
    return round(first + k * lh, 4)


def body_box(V):
    """the brick body on the 8 mm grid (meshes are centred; side studs stick out of it)."""
    sx = max(v[0] for v in V) - min(v[0] for v in V)
    sy = max(v[1] for v in V) - min(v[1] for v in V)
    hx, hy = 4 * math.floor(sx / 8 + 1e-6), 4 * math.floor(sy / 8 + 1e-6)
    return -hx, hx, -hy, hy


def stud_zones(V, T):
    """bed-z ranges that carry fit surfaces: socket walls, side studs, top studs, curved bores/cones.
    Returns (zones, top of the body, total height)."""
    zmin = min(v[2] for v in V)
    zmax = max(v[2] for v in V)
    bx0, bx1, by0, by1 = body_box(V)
    body_area = (bx1 - bx0) * (by1 - by0)
    zones = [(0.0, SOCKET_CLUTCH_TOP, "socket walls")]
    side = [v for v in V if v[0] < bx0 - 0.01 or v[0] > bx1 + 0.01 or v[1] < by0 - 0.01 or v[1] > by1 + 0.01]
    if side:
        zones.append((min(v[2] for v in side) - zmin, max(v[2] for v in side) - zmin, "side studs"))
    up = defaultdict(float)
    groups = defaultdict(lambda: [0.0, 9e9, -9e9])     # oblique faces grouped by normal
    for t in T:
        n, L = tri_normal(V, t)
        if L < 1e-12:
            continue
        nz = n[2] / L
        zs = [V[i][2] - zmin for i in t]
        if nz > 0.999 and max(zs) - min(zs) < ZTOL:
            up[round(zs[0], 3)] += L / 2
        elif 0.01 < abs(nz) < 0.99:
            g = groups[(round(n[0] / L, 2), round(n[1] / L, 2), round(nz, 2))]
            g[0] += L / 2
            g[1] = min(g[1], min(zs))
            g[2] = max(g[2], max(zs))
    h = zmax - zmin
    top_body = h
    # top studs: a small upward level at the very top with a stud-height step below it
    if up.get(round(h, 3), 0) < 0.5 * body_area and any(abs(z - (h - STUD_TOP_H)) < 0.02 for z in up):
        top_body = h - STUD_TOP_H
        zones.append((top_body, h, "top studs"))
    # curved oblique surfaces (pin holes, cones) are tessellated into many facets with different
    # normals; planar slopes are a few large groups and are not fit surfaces
    curved = [g for g in groups.values() if g[0] < 10.0]
    if len(groups) >= 8 and curved:
        zones.append((min(g[1] for g in curved), max(g[2] for g in curved), "curved bore/cone"))
    # visible planar slopes (upward oblique faces): not fit surfaces, but a 0.24 mm layer shows on them
    slopes = [(k, g) for k, g in groups.items() if k[2] > 0 and g[0] >= 10.0]
    vis = (min(g[1] for k, g in slopes), max(g[2] for k, g in slopes)) if slopes else None
    return zones, top_body, zmax - zmin, vis


def analyse_part(V, T, bridge_angle, want_ribs=True, want_bands=True):
    zmin = min(v[2] for v in V)
    strand_axis = "x" if round(bridge_angle) % 180 == 0 else "y"
    zones, top_body, height, vis_slope = stud_zones(V, T)
    bx0, bx1, by0, by1 = body_box(V)
    studs = [(bx0 + 4 + 8 * i, by0 + 4 + 8 * j) for i in range(round((bx1 - bx0) / 8)) for j in range(round((by1 - by0) / 8))]
    res = dict(height=round(height, 3), top_body=round(top_body, 3), strand_axis=strand_axis, bridge_angle=bridge_angle,
               zones=[(round(a, 3), round(b, 3), n) for a, b, n in zones], ceilings=[], bands=[], ribs=[])
    for zc, faces in ceiling_levels(V, T).items():
        r, area = inscribed_radius(V, T, faces)
        cz = dict(z=zc, area=round(sum(tri_normal(V, T[f])[1] / 2 for f in faces), 2), inscribed_r=round(r, 2))
        res["ceilings"].append(cz)
        if r < BRIDGE_MIN_R:
            cz["decision"] = f"not bridged: inscribed radius {r:.2f} < {BRIDGE_MIN_R} (overhang walls close it)"
            continue
        segs = slice_segments(V, T, zc + zmin - PROBE_DZ)
        g = face_grid(V, T, faces)
        base_lines = _base_free_spans(V, T, faces, segs, g, strand_axis)
        sp = _spans_with_ribs(base_lines, strand_axis)
        allf = sorted(x for _, f in sp for x in f)
        cz["spans_before"] = dict(n=len(sp), max=round(max(allf), 2) if allf else 0,
                                  median=round(allf[len(allf) // 2], 2) if allf else 0,
                                  over7=sum(1 for _, f in sp if max(f) > RIB_SPAN_MAX),
                                  over8=sum(1 for _, f in sp if max(f) > 8.0))
        # ---- bands
        z0 = grid_floor(zc)
        band = [(z0, round(z0 + BAND_LAYER, 4), {"wall_loops": BAND_ANCHOR_WALLS}),
                (round(z0 + BAND_LAYER, 4), round(z0 + 2 * BAND_LAYER, 4), {})]
        kept, why = [], []
        if vis_slope and band[0][0] < vis_slope[1] and band[-1][1] > vis_slope[0]:
            # 0.24 mm layers would show as coarse steps on the slope face: anchored ends only, on the
            # native 0.08 mm bridge layer (the first layer whose slice plane is above the ceiling)
            g_z = z0 if z0 + 0.04 > zc + 1e-3 else round(z0 + 0.08, 4)
            band = [(g_z, round(g_z + 0.08, 4), {"wall_loops": BAND_ANCHOR_WALLS, "layer_height": "0.08"})]
            why.append(f"slope face {vis_slope[0]:.2f}..{vis_slope[1]:.2f} in the band: anchor only at 0.08")
        for b0, b1, opts in band:
            # a band layer may not reach into a fit-surface zone by more than half a 0.08 layer
            clash = [n for a, b, n in zones if min(b1, b) - max(b0, a) > BAND_ZONE_TOL]
            if clash:
                why.append(f"{b0}..{b1} overlaps {clash}")
                break
            if b1 > top_body - 0.08 * 3 + 1e-6:
                why.append(f"{b0}..{b1} within 3 layers of the top {top_body:.2f}")
                break
            kept.append((b0, b1, opts))
        if not want_bands:
            cz["band_decision"] = "bands disabled"
        elif not kept:
            cz["band_decision"] = "refused: " + "; ".join(why)
        else:
            cz["band_decision"] = " + ".join(f"{b0}..{b1} ({o.get('layer_height', '0.24')}{', 2 walls' if 'wall_loops' in o else ''})"
                                             for b0, b1, o in kept) + \
                                  (f"; note: {'; '.join(why)}" if why else "")
            res["bands"] += kept
        # ---- ribs
        if not want_ribs:
            continue
        if cz["spans_before"]["max"] <= RIB_SPAN_MAX:
            cz["rib_decision"] = f"no rib: worst span {cz['spans_before']['max']} <= {RIB_SPAN_MAX}"
            continue
        if zc - RIB_DEPTH < STUD_H_REAL + STUD_CLEAR - 1e-6:
            cz["rib_decision"] = f"refused: rib bottom {zc - RIB_DEPTH:.2f} < {STUD_H_REAL + STUD_CLEAR}"
            continue
        # candidate lines: interior cell boundaries perpendicular to the strands
        rib_axis = strand_axis              # rib along y at x = c cuts strands along x
        if rib_axis == "x":
            cands = [bx0 + 8 * k for k in range(1, round((bx1 - bx0) / 8))]
        else:
            cands = [by0 + 8 * k for k in range(1, round((by1 - by0) / 8))]
        # Precompute rib_extent once per candidate (geometry does not change across rounds)
        cand_ribs = {}
        notes = []
        for c in cands:
            if studs:
                dmin = min(abs(c - (sx if rib_axis == "x" else sy)) for sx, sy in studs) - RIB_W / 2
            else:
                dmin = math.inf
            if dmin < STUD_R + RIB_STUD_MARGIN:
                notes.append(f"line {c}: {dmin:.2f} mm from a stud axis")
                continue
            lo, hi, why = rib_extent(V, T, faces, zc + zmin, rib_axis, c, _segs=segs, _grid=g)
            if lo is None:
                notes.append(f"line {c}: {why}")
                continue
            cand_ribs[c] = dict(axis=rib_axis, c=c, lo=lo, hi=hi, why=why,
                                stud_dist=round(dmin, 2) if math.isfinite(dmin) else None)
        ribs = []
        rem = list(cand_ribs)
        worst = cz["spans_before"]["max"]
        sp_after = sp
        while worst > RIB_SPAN_MAX and rem:
            best = None
            for c in rem:
                rib = cand_ribs[c]
                sp2 = _spans_with_ribs(base_lines, strand_axis, ribs + [rib])
                w2 = max((x for _, f in sp2 for x in f), default=0.0)
                if best is None or w2 < best[0]:
                    best = (w2, rib, sp2)
            if best is None or best[0] >= worst - 1e-6:
                break
            worst = best[0]
            ribs.append(best[1])
            rem.remove(best[1]["c"])
            sp_after = best[2]
        if ribs:
            allf = sorted(x for _, f in sp_after for x in f)
            cz["spans_after"] = dict(n=len(sp_after), max=round(max(allf), 2) if allf else 0,
                                     median=round(allf[len(allf) // 2], 2) if allf else 0,
                                     over7=sum(1 for _, f in sp_after if max(f) > RIB_SPAN_MAX),
                                     over8=sum(1 for _, f in sp_after if max(f) > 8.0))
            for rb in ribs:
                rb.update(z0=round(zc - RIB_DEPTH, 4), z1=zc)
            res["ribs"] += ribs
            cz["rib_decision"] = f"{len(ribs)} rib(s): worst span {cz['spans_before']['max']} -> {worst:.2f}"
        else:
            cz["rib_decision"] = "no admissible rib line: " + "; ".join(notes[:3])
        cz["rib_notes"] = notes
    return res


def rib_mesh_local(rb, zmin):
    w = RIB_W / 2
    z0, z1 = rb["z0"] + zmin, rb["z1"] + zmin + RIB_INTO_CEILING
    if rb["axis"] == "x":     # rib along y at x = c
        return box(rb["c"] - w, rb["c"] + w, rb["lo"], rb["hi"], z0, z1)
    return box(rb["lo"], rb["hi"], rb["c"] - w, rb["c"] + w, z0, z1)


# --------------------------------------------------------------------------
# public entry point & disk cache
# --------------------------------------------------------------------------
def _plan_cache_path(V_arr: np.ndarray, T_arr: np.ndarray, bridge_angle: float,
                     raise_ceiling: bool, want_bands: bool, want_ribs: bool,
                     cache_dir, part_file: str | None) -> tuple[Path | None, str]:
    h = hashlib.sha1(np.ascontiguousarray(V_arr, dtype=np.float32).tobytes())
    h.update(np.ascontiguousarray(T_arr, dtype=np.int32).tobytes())
    h.update(f":v{CEILINGS_VERSION}:{float(bridge_angle):g}:{int(raise_ceiling)}:{int(want_bands)}:{int(want_ribs)}".encode())
    tag = h.hexdigest()[:16]
    if cache_dir is False or cache_dir == "":
        return None, tag
    if cache_dir is not None:
        croot = Path(cache_dir)
    else:
        if os.environ.get("BRICKPRINT_NO_CEILINGS_CACHE", "").strip() not in ("", "0"):
            return None, tag
        from . import ldraw
        croot = ldraw._ACTIVE_CACHE_ROOT
        if croot is None or not croot.is_dir():
            return None, tag
    if part_file:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", part_file)
        return croot / f"{safe}.ceil{CEILINGS_VERSION}.json", tag
    return croot / f"ceil{CEILINGS_VERSION}_{tag}.json", tag


def _restore_cached_plan(hit: dict, V_arr: np.ndarray) -> dict:
    moved_z = hit.get("moved_z")
    moved_idx = hit.get("moved_idx")
    if moved_z is not None and moved_idx:
        out_v = V_arr.copy()
        out_v[moved_idx, 2] = float(moved_z)
        Vl = [tuple(row) for row in out_v.tolist()]
    else:
        Vl = [tuple(row) for row in V_arr.tolist()]
    raise_rep = dict(hit["raise"])
    if "ceilings" in raise_rep:
        raise_rep["ceilings"] = [tuple(c) for c in raise_rep["ceilings"]]
    bands = [(float(z0), float(z1), dict(o)) for z0, z1, o in hit["bands"]]
    rib_raw = hit["ribs"]
    rib = None if rib_raw is None else ([tuple(v) for v in rib_raw[0]], [tuple(t) for t in rib_raw[1]])
    res = dict(hit["report"])
    if "zones" in res:
        res["zones"] = [tuple(z) for z in res["zones"]]
    return {"V": Vl, "raise": raise_rep, "bands": bands, "ribs": rib, "report": res}


def plan(V, T, bridge_angle: float = 180.0, raise_ceiling: bool = True,
         want_bands: bool = True, want_ribs: bool = True, *,
         cache_dir: Path | str | bool | None = None,
         part_file: str | None = None) -> dict:
    """Plan the recipe for one oriented part mesh (bed frame, any z origin).

    Returns ``{"V": vertex list (ceiling raised, or unchanged), "raise": the
    raise report, "bands": [(z0, z1, {option: value})] relative to the part's
    bottom, "ribs": (V, T) merged rib mesh in the part frame or None,
    "report": the band/rib analysis}``.  ``layer_height`` defaults to 0.24 in
    a band unless the band sets it.

    Results are cached atomically next to ``.c2.json`` in the active
    ``MeshCache`` root (or ``cache_dir``) keyed on the posed mesh, flags and
    ``CEILINGS_VERSION``.  Pass ``cache_dir=False`` to bypass disk caching."""
    V_arr = np.asarray(V, dtype=np.float64)
    T_arr = np.asarray(T, dtype=np.int64)
    cpath, tag = _plan_cache_path(V_arr, T_arr, bridge_angle, raise_ceiling,
                                  want_bands, want_ribs, cache_dir, part_file)
    if cpath is not None and cpath.is_file():
        try:
            hit = json.loads(cpath.read_text())
            if hit.get("tag") == tag:
                return _restore_cached_plan(hit, V_arr)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    # The analysis assumes the part is centred on the origin in X-Y (the LDraw
    # frame of the Seagull meshes); brickprint meshes start at a corner.  Parts
    # whose body is not centred in their bounding box (one-sided side studs)
    # are judged conservatively: their bands are refused.
    cx = float((V_arr[:, 0].min() + V_arr[:, 0].max()) / 2.0)
    cy = float((V_arr[:, 1].min() + V_arr[:, 1].max()) / 2.0)
    V_centered = V_arr.copy()
    V_centered[:, 0] -= cx
    V_centered[:, 1] -= cy
    Vl = [tuple(row) for row in V_centered.tolist()]
    Tl = [tuple(row) for row in T_arr.tolist()]
    rep = {"status": "off"}
    moved_z = None
    moved_idx: list[int] = []
    if raise_ceiling:
        rep = analyse(Vl, Tl)
        if rep.get("status") == "raise":
            moved = rep["moved"]
            moved_idx = sorted(moved)
            moved_z = next(iter(moved.values())) if moved else None
            Vl = [(v[0], v[1], moved[i]) if i in moved else v for i, v in enumerate(Vl)]
    res = analyse_part(Vl, Tl, float(bridge_angle), want_ribs=want_ribs, want_bands=want_bands)
    zmin = min(v[2] for v in Vl)
    bands = []
    for z0, z1, opts in res["bands"]:
        o = {"layer_height": f"{BAND_LAYER:g}"}
        o.update(opts)
        bands.append((round(z0, 4), round(z1, 4), o))
    rib = None
    if res["ribs"]:
        RV, RT = [], []
        for rb in res["ribs"]:
            bv, bt = rib_mesh_local(rb, zmin)
            n = len(RV)
            RV += bv
            RT += [(a + n, b + n, c + n) for a, b, c in bt]
        rib = ([(x + cx, y + cy, z) for x, y, z in RV], RT)
    Vl = [(x + cx, y + cy, z) for x, y, z in Vl]
    raise_clean = {k: v for k, v in rep.items() if k != "moved"}
    if cpath is not None:
        try:
            from . import ldraw
            cpath.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "tag": tag,
                "raise": raise_clean,
                "moved_z": moved_z,
                "moved_idx": moved_idx,
                "bands": [[z0, z1, o] for z0, z1, o in bands],
                "ribs": None if rib is None else [[list(v) for v in rib[0]], [list(t) for t in rib[1]]],
                "report": res,
            }
            ldraw._atomic_write_text(cpath, json.dumps(payload))
        except OSError:
            pass
    return {"V": Vl, "raise": raise_clean,
            "bands": bands, "ribs": rib, "report": res}
