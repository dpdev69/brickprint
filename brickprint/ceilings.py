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

import math
from collections import defaultdict

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
    for fi, t in enumerate(T):
        zs = [V[i][2] for i in t]
        if max(zs) - min(zs) > ZTOL:
            continue
        n, L = tri_normal(V, t)
        if L < 1e-12:
            continue
        lvl = round(sum(zs) / 3, 4)
        (horiz_down if n[2] < 0 else horiz_up)[lvl].append(fi)

    def area(fs):
        return sum(tri_normal(V, T[f])[1] / 2 for f in fs)

    levels = sorted(horiz_down)
    socket_levels = [l for l in levels if rel(l) > MIN_CEILING - ZTOL and area(horiz_down[l]) >= MIN_AREA]
    out = {"zmin": zmin, "height": max(v[2] for v in V) - zmin,
           "ceilings": [(round(rel(l), 3), round(area(horiz_down[l]), 2)) for l in levels if rel(l) > ZTOL]}
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
    out.update(level=round(rel(c), 4), d=round(d, 4), ceiling_area=round(area(horiz_down[c]), 2))
    M = {i for i, v in enumerate(V) if abs(v[2] - c) < ZTOL}
    # bed footprint hull
    H = hull2d([(v[0], v[1]) for v in V if abs(v[2] - zmin) < ZTOL])
    if len(H) >= 3:
        a2 = sum(H[i][0] * H[(i + 1) % len(H)][1] - H[(i + 1) % len(H)][0] * H[i][1] for i in range(len(H)))
        if a2 < 0:
            H = H[::-1]
    down_c = horiz_down[c]
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
    elif edge_faces and edge_area > MAX_EDGE_FRACTION * area(down_c):
        problems.append(f"{edge_area:.1f} of {area(down_c):.1f} mm2 of ceiling touches the outline")
    for fi, t in enumerate(T):
        k = sum(1 for i in t if i in M)
        if k == 0:
            continue
        n, L = tri_normal(V, t)
        if k == 3:
            if L < 1e-12:
                continue                      # degenerate face lying in the plane: stays in it
            cx = sum(V[i][0] for i in t) / 3
            cy = sum(V[i][1] for i in t) / 3
            if n[2] > 0:
                if not any(in_tri_xy(V, T[g], cx, cy) for g in down_c):
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
        if any(in_tri_xy(V, T[g], v[0], v[1]) for g in down_c):
            problems.append(f"vertex {i} at z={rel(v[2]):.3f} lies within {MIN_SKIN} mm above the new ceiling")
            break
    # top skin: vertical rays from the ceiling region to the next surface above
    xs = [V[i][0] for f in down_c for i in T[f]]
    ys = [V[i][1] for f in down_c for i in T[f]]
    # Bounded sampling density: 0.2 mm on small parts, coarser on large
    # plates.  A skin thinner than the step is narrower than one nozzle
    # line, so it cannot be printed anyway.  Without this, a 6x10 plate
    # samples ~400k points and takes minutes per part.
    _sx, _sy = max(xs) - min(xs), max(ys) - min(ys)
    step = max(0.2, math.sqrt(max(_sx * _sy, 1e-9) / 40000.0))
    grid = defaultdict(list)
    for fi, t in enumerate(T):
        if all(i in M for i in t):
            continue
        if max(V[i][2] for i in t) <= c + ZTOL or xy_width(V, t) <= VERTICAL_TOL:
            continue
        bx0 = min(V[i][0] for i in t); bx1 = max(V[i][0] for i in t)
        by0 = min(V[i][1] for i in t); by1 = max(V[i][1] for i in t)
        for gx in range(int(math.floor(bx0)), int(math.floor(bx1)) + 1):
            for gy in range(int(math.floor(by0)), int(math.floor(by1)) + 1):
                grid[(gx, gy)].append(fi)
    # Index the ceiling faces the same way, so each sample only tests the
    # few triangles whose 1 mm cell contains it (a 6x10 plate has thousands
    # of ceiling faces; testing all of them per sample is minutes of work).
    dgrid = defaultdict(list)
    for g in down_c:
        t = T[g]
        bx0 = min(V[i][0] for i in t); bx1 = max(V[i][0] for i in t)
        by0 = min(V[i][1] for i in t); by1 = max(V[i][1] for i in t)
        for gx in range(int(math.floor(bx0)), int(math.floor(bx1)) + 1):
            for gy in range(int(math.floor(by0)), int(math.floor(by1)) + 1):
                dgrid[(gx, gy)].append(g)
    skin, nsamp = 1e9, 0
    x = min(xs) + step / 2
    while x < max(xs):
        y = min(ys) + step / 2
        while y < max(ys):
            cell = (int(math.floor(x)), int(math.floor(y)))
            if any(in_tri_xy(V, T[g], x, y) for g in dgrid.get(cell, ())):
                nsamp += 1
                best = 1e9
                for fi in grid.get((int(math.floor(x)), int(math.floor(y))), ()):
                    z = bary_z(V, T[fi], x, y)
                    if z is not None and z > c + ZTOL:
                        best = min(best, z)
                skin = min(skin, best - newz)
            y += step
        x += step
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
MIN_AREA = 2.0
MIN_CEILING = 0.8
ZTOL = 1e-4
PROBE_DZ = 0.05            # mm below the ceiling where the cavity is sampled
STRAND_PITCH = 0.45        # mm, sampling pitch of the span estimate (v6 G-code pitch 0.455)


def box(x0, x1, y0, y1, z0, z1):
    V = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
    T = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
         (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    return V, T


def slice_segments(V, T, z):
    """segments of the mesh cross-section at plane z: [(x1, y1, x2, y2)]"""
    out = []
    for t in T:
        P = [V[i] for i in t]
        above = [p for p in P if p[2] > z]
        below = [p for p in P if p[2] <= z]
        if not above or not below:
            continue
        pts = []
        for a in above:
            for b in below:
                s = (z - b[2]) / (a[2] - b[2])
                pts.append((b[0] + s * (a[0] - b[0]), b[1] + s * (a[1] - b[1])))
        if len(pts) >= 2:
            out.append((pts[0][0], pts[0][1], pts[1][0], pts[1][1]))
    return out


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
    and take an 8-neighbour BFS distance from the boundary.  The previous
    exact min-over-all-border-pairs loop was O(inside x border): a 6x10
    plate has ~60 socket cavities and took minutes per part.
    """
    xs = [V[i][0] for f in faces for i in T[f]]
    ys = [V[i][1] for f in faces for i in T[f]]
    g = face_grid(V, T, faces)
    x0, y0 = min(xs) - step, min(ys) - step
    nx, ny = int((max(xs) - x0) / step) + 2, int((max(ys) - y0) / step) + 2
    inside = {(i, j) for i in range(nx) for j in range(ny)
              if in_faces(V, T, faces, x0 + (i + 0.5) * step, y0 + (j + 0.5) * step, g)}
    if not inside:
        return 0.0, 0.0
    border = [(i, j) for (i, j) in inside if any((i + a, j + b) not in inside
                                                for a, b in ((1, 0), (-1, 0), (0, 1), (0, -1)))]
    # multi-source BFS (8-neighbour) from the boundary cells
    from collections import deque
    dist = {p: 0 for p in border}
    q = deque(border)
    best_cells = 0
    while q:
        i, j = q.popleft()
        d = dist[(i, j)] + 1
        for a, b in ((1, 0), (-1, 0), (0, 1), (0, -1),
                     (1, 1), (1, -1), (-1, 1), (-1, -1)):
            n = (i + a, j + b)
            if n in inside and n not in dist:
                dist[n] = d
                if d > best_cells:
                    best_cells = d
                q.append(n)
    return (best_cells + 0.5) * step, len(inside) * step * step



def spans(V, T, faces, zc, zmin, strand_axis, ribs=()):
    """free strand spans under the ceiling at local z zc: strands along strand_axis ('x' or 'y'),
    sampled every STRAND_PITCH.  Returns [(line coord, [free span lengths])]."""
    segs = slice_segments(V, T, zc - PROBE_DZ)
    g = face_grid(V, T, faces)
    pts = [V[i] for f in faces for i in T[f]]
    # lines perpendicular to the sampling direction: strand along x -> lines y = const
    across = "y" if strand_axis == "x" else "x"
    lo = min(p[1] if across == "y" else p[0] for p in pts)
    hi = max(p[1] if across == "y" else p[0] for p in pts)
    out = []
    c = lo + STRAND_PITCH / 2
    while c < hi:
        M = line_intervals(segs, "y" if strand_axis == "x" else "x", c)   # material along the strand
        # add ribs as material
        for r in ribs:
            if r["axis"] == strand_axis and r["lo"] - 1e-9 <= c <= r["hi"] + 1e-9:
                M.append((r["c"] - RIB_W / 2, r["c"] + RIB_W / 2))
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
                free.append(b0 - a1)
        if free:
            out.append((c, free))
        c += STRAND_PITCH
    return out


def rib_extent(V, T, faces, zc, axis, c):
    """rib along the line {axis = c} (axis 'x': rib runs in y at x = c).  Returns (lo, hi, notes) in
    the running coordinate, or (None, None, reason)."""
    segs = slice_segments(V, T, zc - PROBE_DZ)
    g = face_grid(V, T, faces)
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
        sp = spans(V, T, faces, zc + zmin, zmin, strand_axis)
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
            g = z0 if z0 + 0.04 > zc + 1e-3 else round(z0 + 0.08, 4)
            band = [(g, round(g + 0.08, 4), {"wall_loops": BAND_ANCHOR_WALLS, "layer_height": "0.08"})]
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
        ribs, notes = [], []
        worst = cz["spans_before"]["max"]
        while worst > RIB_SPAN_MAX and cands:
            best = None
            for c in cands:
                # stud-axis clearance
                dmin = min(abs(c - (sx if rib_axis == "x" else sy)) for sx, sy in studs) - RIB_W / 2
                if dmin < STUD_R + RIB_STUD_MARGIN:
                    notes.append(f"line {c}: {dmin:.2f} mm from a stud axis")
                    continue
                lo, hi, why = rib_extent(V, T, faces, zc + zmin, rib_axis, c)
                if lo is None:
                    notes.append(f"line {c}: {why}")
                    continue
                rib = dict(axis=rib_axis, c=c, lo=lo, hi=hi, why=why, stud_dist=round(dmin, 2))
                sp2 = spans(V, T, faces, zc + zmin, zmin, strand_axis, ribs + [rib])
                w2 = max(x for _, f in sp2 for x in f)
                if best is None or w2 < best[0]:
                    best = (w2, rib, sp2)
            if best is None or best[0] >= worst - 1e-6:
                break
            worst = best[0]
            ribs.append(best[1])
            cands.remove(best[1]["c"])
            sp_after = best[2]
        if ribs:
            allf = sorted(x for _, f in sp_after for x in f)
            cz["spans_after"] = dict(n=len(sp_after), max=round(max(allf), 2), median=round(allf[len(allf) // 2], 2),
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
# public entry point
# --------------------------------------------------------------------------
def plan(V, T, bridge_angle: float = 180.0, raise_ceiling: bool = True,
         want_bands: bool = True, want_ribs: bool = True) -> dict:
    """Plan the recipe for one oriented part mesh (bed frame, any z origin).

    Returns ``{"V": vertex list (ceiling raised, or unchanged), "raise": the
    raise report, "bands": [(z0, z1, {option: value})] relative to the part's
    bottom, "ribs": (V, T) merged rib mesh in the part frame or None,
    "report": the band/rib analysis}``.  ``layer_height`` defaults to 0.24 in
    a band unless the band sets it."""
    # The analysis assumes the part is centred on the origin in X-Y (the LDraw
    # frame of the Seagull meshes); brickprint meshes start at a corner.  Parts
    # whose body is not centred in their bounding box (one-sided side studs)
    # are judged conservatively: their bands are refused.
    cx = (min(float(v[0]) for v in V) + max(float(v[0]) for v in V)) / 2
    cy = (min(float(v[1]) for v in V) + max(float(v[1]) for v in V)) / 2
    Vl = [(float(v[0]) - cx, float(v[1]) - cy, float(v[2])) for v in V]
    Tl = [tuple(int(i) for i in t) for t in T]
    rep = {"status": "off"}
    if raise_ceiling:
        rep = analyse(Vl, Tl)
        if rep.get("status") == "raise":
            moved = rep["moved"]
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
    return {"V": Vl, "raise": {k: v for k, v in rep.items() if k != "moved"},
            "bands": bands, "ribs": rib, "report": res}
