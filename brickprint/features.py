"""Per-part geometry features on the posed (print-orientation, min-shifted) mesh.

* ``cantilever_boxes``: support-enforcer boxes under features that stick out
  of the part body (the side studs of 4733 "Brick 1 x 1 with Studs on 4
  Sides").  Body = convex hull of the first-layer footprint (vertices below
  0.2 mm).  Downward-facing facets (n_z < 0) above 0.3 mm whose centroid lies
  more than 0.3 mm outside that hull are clustered (shared vertices); a cluster
  whose largest centroid distance from the hull exceeds ``protrusion`` mm
  (default 0.8 = 2 line widths; the 59900 cone's 0.53 mm base step stays
  unsupported, as Bambu itself does) gets one box: the cluster's XY bbox
  grown by ``margin`` but clipped at the body bbox on the side facing the body,
  from ``below`` mm under the cluster's lowest point to ``above`` mm over it.
  Sockets, tubes and cavity ceilings are inside the hull, so they never get
  support (the reason the PETG recipe uses enforcer-only supports at all).
  For 4733 this reproduces seagull/build_overhang_v4.py's boxes exactly.
* ``bridge_angle``: Bambu's auto bridge direction lays cavity-ceiling bridges
  along the long side (28.5 mm spans on 4x2 wedge plates).  The profile sets a
  global bridge_angle of 180 (lines along X); a part whose ceiling is longer in
  X than in Y (+0.5 mm, and longer than one
  stud pitch) gets a per-part 90 so it is bridged the short way too.
* ``stud_rings``: top-face stud circles (segment count, diameter) for the
  quality report.
"""

from __future__ import annotations

import numpy as np

from . import orient

FIRST_LAYER_Z = 0.2
MIN_Z = 0.3
OUTSIDE_MM = 0.3


def _faces(V, T):
    V = np.asarray(V, dtype=np.float64)
    T = np.asarray(T, dtype=np.int64)
    A, B, C = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    N = np.cross(B - A, C - A)
    a2 = np.linalg.norm(N, axis=1)
    nz = np.where(a2 > 0, N[:, 2] / np.maximum(a2, 1e-12), 0.0)
    return V, T, (A + B + C) / 3.0, nz, a2


def footprint_hull(V) -> np.ndarray:
    V = np.asarray(V, dtype=np.float64)
    base = V[V[:, 2] < V[:, 2].min() + FIRST_LAYER_Z][:, :2]
    return orient._hull(base)


def _hull_distance(P: np.ndarray, H: np.ndarray) -> np.ndarray:
    d2 = np.full(len(P), np.inf)
    for i in range(len(H)):
        a, b = H[i], H[(i + 1) % len(H)]
        ab = b - a
        t = np.clip(((P - a) @ ab) / max(float(ab @ ab), 1e-12), 0.0, 1.0)
        diff = P - (a + t[:, None] * ab)
        d2 = np.minimum(d2, (diff * diff).sum(axis=1))
    return np.sqrt(d2)


def _clusters(T: np.ndarray, faces: np.ndarray) -> list[np.ndarray]:
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for f in faces:
        a, b, c = (int(v) for v in T[f])
        for u, v in ((a, b), (b, c)):
            ru, rv = find(u), find(v)
            if ru != rv:
                parent[ru] = rv
    groups: dict[int, list[int]] = {}
    for f in faces:
        groups.setdefault(find(int(T[f][0])), []).append(int(f))
    return [np.array(g) for g in groups.values()]


def cantilever_boxes(V, T, protrusion: float = 0.8, margin: float = 0.2,
                     below: float = 0.3, above: float = 0.6) -> list[dict]:
    V, T, cen, nz, a2 = _faces(V, T)
    H = footprint_hull(V)
    if len(H) < 3:
        return []
    zmin0 = V[:, 2].min()
    down_idx = np.nonzero((nz < -1e-3) & (cen[:, 2] > zmin0 + MIN_Z))[0]
    if not len(down_idx):
        return []
    cand = down_idx[~orient._in_hull(cen[down_idx, :2], H)]
    if not len(cand):
        return []
    dist = _hull_distance(cen[cand, :2], H)
    cand, dist = cand[dist > OUTSIDE_MM], dist[dist > OUTSIDE_MM]
    if not len(cand):
        return []
    dmap = dict(zip(cand.tolist(), dist.tolist()))
    bx0, by0 = H.min(axis=0)
    bx1, by1 = H.max(axis=0)
    # clusters whose XY bboxes overlap belong to one feature (outer and inner
    # surface of a hollow side stud)
    groups = []
    for g in _clusters(T, cand):
        P = V[np.unique(T[g])]
        groups.append([g, P.min(axis=0), P.max(axis=0)])
    merged = True
    while merged:
        merged = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                a, b = groups[i], groups[j]
                if (a[1][0] <= b[2][0] and b[1][0] <= a[2][0]
                        and a[1][1] <= b[2][1] and b[1][1] <= a[2][1]):
                    groups[i] = [np.concatenate([a[0], b[0]]), np.minimum(a[1], b[1]),
                                 np.maximum(a[2], b[2])]
                    groups.pop(j)
                    merged = True
                    break
            if merged:
                break
    boxes = []
    for g, lo, hi in groups:
        prot = max(dmap[f] for f in g)
        if prot <= protrusion:
            continue
        x0, x1 = lo[0] - margin, hi[0] + margin
        y0, y1 = lo[1] - margin, hi[1] + margin
        eps = 1e-3
        if lo[0] >= bx1 - eps:
            x0 = max(lo[0], bx1)
        if hi[0] <= bx0 + eps:
            x1 = min(hi[0], bx0)
        if lo[1] >= by1 - eps:
            y0 = max(lo[1], by1)
        if hi[1] <= by0 + eps:
            y1 = min(hi[1], by0)
        z0 = max(zmin0, lo[2] - below)
        boxes.append({"box": [float(x0), float(x1), float(y0), float(y1), float(z0),
                              float(lo[2] + above)],
                      "protrusion_mm": round(float(prot), 3),
                      "area_mm2": round(float(0.5 * a2[g].sum()), 2)})
    boxes.sort(key=lambda b: b["box"])
    return boxes


def box_mesh(boxes: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """One mesh of closed, outward-facing boxes (same winding as v4)."""
    V, T = [], []
    tri = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
           (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    for b in boxes:
        x0, x1, y0, y1, z0, z1 = b["box"]
        o = len(V)
        V += [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
        T += [(a + o, c + o, d + o) for a, c, d in tri]
    return np.array(V, dtype=np.float32), np.array(T, dtype=np.int32)


BLOCK_LAYER = 0.2       # mm: z resolution of the enclosure map
BLOCK_PX = 0.25         # mm: XY resolution
BLOCK_UP = 0.4          # mm: a blocker reaches this far into the ceiling above it
BLOCK_MIN_MM2 = 1.0     # enclosed areas smaller than this are ignored


def _rects(M: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Cover a 2D bool mask with rectangles (row runs merged downwards):
    [(i0, i1, j0, j1)] half-open cell ranges, i = axis 0."""
    out, open_ = [], {}
    nx, ny = M.shape
    for j in range(ny + 1):
        runs = set()
        if j < ny:
            col = M[:, j]
            if col.any():
                d = np.diff(np.r_[0, col.astype(np.int8), 0])
                runs = set(zip(np.nonzero(d == 1)[0].tolist(), np.nonzero(d == -1)[0].tolist()))
        for r in list(open_):
            if r not in runs:
                out.append((r[0], r[1], open_.pop(r), j))
        for r in runs:
            open_.setdefault(r, j)
    return out


def _flood_2d_free(free2d: np.ndarray) -> np.ndarray:
    """Exact 4-connected reachability from boundary of a 2D bool slice via horizontal-run BFS."""
    nx, ny = free2d.shape
    padded = np.zeros((nx, ny + 2), dtype=np.int8)
    padded[:, 1:-1] = free2d
    diff = np.diff(padded, axis=1)
    sr, sc = np.nonzero(diff == 1)
    _, ec = np.nonzero(diff == -1)
    nruns = len(sr)
    if nruns == 0:
        return np.zeros_like(free2d)
    row_start = np.searchsorted(sr, np.arange(nx + 1)).tolist()
    sr_l, sc_l, ec_l = sr.tolist(), sc.tolist(), ec.tolist()
    vis = bytearray(nruns)
    q = []
    for idx in range(nruns):
        r = sr_l[idx]
        if r == 0 or r == nx - 1 or sc_l[idx] == 0 or ec_l[idx] == ny:
            vis[idx] = 1
            q.append(idx)
    head = 0
    while head < len(q):
        u = q[head]
        head += 1
        r, c0, c1 = sr_l[u], sc_l[u], ec_l[u]
        for nr in (r - 1, r + 1):
            if 0 <= nr < nx:
                lo, hi = row_start[nr], row_start[nr + 1]
                for v in range(lo, hi):
                    if ec_l[v] <= c0:
                        continue
                    if sc_l[v] >= c1:
                        break
                    if not vis[v]:
                        vis[v] = 1
                        q.append(v)
    reach = np.zeros_like(free2d)
    for idx in q:
        reach[sr_l[idx], sc_l[idx]:ec_l[idx]] = True
    return reach


def _grow_rect_row(unc: np.ndarray, allowed: np.ndarray) -> tuple[int, int, int, int, int]:
    """Grow a maximal rectangle inside ``allowed`` from the busiest uncovered row."""
    nx, ny = unc.shape
    r_seed = int(np.argmax(unc.sum(axis=1)))
    a_row = allowed[r_seed]
    d = np.diff(np.r_[0, a_row.astype(np.int8), 0])
    starts = np.nonzero(d == 1)[0].tolist()
    ends = np.nonzero(d == -1)[0].tolist()
    best_run = (-1, 0, 0)
    for c0, c1 in zip(starts, ends):
        u_cnt = int(unc[r_seed, c0:c1].sum())
        if u_cnt > best_run[0]:
            best_run = (u_cnt, c0, c1)
    _, ac0, ac1 = best_run
    uj = np.nonzero(unc[r_seed, ac0:ac1])[0]
    c0, c1 = ac0 + int(uj[0]), ac0 + int(uj[-1]) + 1
    r0 = r_seed
    while r0 > 0 and allowed[r0 - 1, c0:c1].all():
        r0 -= 1
    r1 = r_seed + 1
    while r1 < nx and allowed[r1, c0:c1].all():
        r1 += 1
    while c0 > 0 and allowed[r0:r1, c0 - 1].all():
        c0 -= 1
    while c1 < ny and allowed[r0:r1, c1].all():
        c1 += 1
    return r0, r1, c0, c1, int(unc[r0:r1, c0:c1].sum())


def _cover_with_dontcares(req: np.ndarray, allowed: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Cover every True cell of ``req`` with rectangles strictly inside ``allowed``.

    ``req <= allowed`` is guaranteed.  Allows rectangles to overlap the part's
    own solid material (``allowed & ~req``) while never touching external air
    (``~allowed``), avoiding staircase explosion on curved cavities.
    """
    unc = req.copy()
    rects = []
    while unc.any():
        ii, jj = np.nonzero(unc)
        i0, i1, j0, j1 = int(ii.min()), int(ii.max()) + 1, int(jj.min()), int(jj.max()) + 1
        if allowed[i0:i1, j0:j1].all():
            rects.append((i0, i1, j0, j1))
            break
        r0, r1, c0, c1, cnt_r = _grow_rect_row(unc, allowed)
        tc0, tc1, tr0, tr1, cnt_c = _grow_rect_row(unc.T, allowed.T)
        if cnt_c > cnt_r:
            r0, r1, c0, c1 = tr0, tr1, tc0, tc1
        rects.append((r0, r1, c0, c1))
        unc[r0:r1, c0:c1] = False
    return rects


def socket_blockers(V, T, layer: float = BLOCK_LAYER, px: float = BLOCK_PX) -> list[dict]:
    """Support-blocker boxes over the enclosed cavities of a posed part.

    A cell of layer k is *enclosed* when it is empty and cannot be reached
    from outside the part's bounding box within that layer: it is surrounded
    by the part's own walls (stud socket, tube, vertical hole, any closed
    hollow).  Support there could never come out, and a stud no longer fits.
    The layers come from orient.slice_stack (the slicer emulation, robust to
    LDraw winding).  Each enclosed region becomes boxes that reach BLOCK_UP mm
    into the ceiling above it (Bambu's blocker acts on the overhang it
    covers).  Cantilevered features outside the walls are never covered, so
    support enforcers / auto supports still work there.  This is the same
    rule verify uses to call support "inside a part" (G-code outlines)."""
    V = np.asarray(V, dtype=np.float64)
    zmin = V[:, 2].min()
    W = V - [0, 0, zmin]
    mask, px = orient.slice_stack(W, T, layer=layer, px=px)
    nx, ny, nl = mask.shape
    x0, y0 = orient.SLICE_X0 + W[:, 0].min(), orient.SLICE_Y0 + W[:, 1].min()
    # Deduplicate identical 2D cross-sections before border flood-fill
    mT = np.ascontiguousarray(mask.transpose(2, 0, 1))
    enc_cache: dict[bytes, np.ndarray] = {}
    encT = np.zeros_like(mT)
    for k in range(nl):
        key = mT[k].tobytes()
        e2d = enc_cache.get(key)
        if e2d is None:
            free2d = ~mT[k]
            e2d = free2d & ~_flood_2d_free(free2d)
            enc_cache[key] = e2d
        encT[k] = e2d
    # only cavities with something above them need blocking (an overhang)
    roofT = np.zeros_like(mT)
    roofT[:-1] = np.flip(np.logical_or.accumulate(np.flip(mT[1:], 0), 0), 0)
    encT &= roofT
    if encT.sum() * px * px < BLOCK_MIN_MM2:
        return []
    # A blocker may safely overlap the part's own solid material (nothing is
    # supported there anyway), but must never enter external air (~allowed).
    # Group consecutive Z layers into slabs while union(enc) <= intersection(allowed),
    # then cover each slab's enclosed cells with minimal rectangles inside allowed.
    allowedT = encT | mT
    boxes = []
    k = 0
    while k < nl:
        if not encT[k].any():
            k += 1
            continue
        slab_enc = encT[k].copy()
        slab_all = allowedT[k].copy()
        k1 = k + 1
        while k1 < nl and encT[k1].any():
            nxt_enc = slab_enc | encT[k1]
            nxt_all = slab_all & allowedT[k1]
            if (nxt_enc & ~nxt_all).any():
                break
            slab_enc = nxt_enc
            slab_all = nxt_all
            k1 += 1
        for i0, i1, j0, j1 in _cover_with_dontcares(slab_enc, slab_all):
            if (i1 - i0) * (j1 - j0) * px * px < 0.05:
                continue
            boxes.append({"box": [round(float(x0 + i0 * px), 3), round(float(x0 + i1 * px), 3),
                                  round(float(y0 + j0 * px), 3), round(float(y0 + j1 * px), 3),
                                  round(float(zmin + k * layer), 3),
                                  round(float(zmin + k1 * layer + BLOCK_UP), 3)]})
        k = k1
    # stacked slabs sharing identical XY rectangles: merge in z
    merged: dict[tuple, list] = {}
    for b in sorted(boxes, key=lambda b: (b["box"][:4], b["box"][4])):
        key = tuple(b["box"][:4])
        m = merged.get(key)
        if m and m[-1][1] >= b["box"][4] - 1e-6:
            m[-1][1] = max(m[-1][1], b["box"][5])
        else:
            merged.setdefault(key, []).append([b["box"][4], b["box"][5]])
    out = [{"box": [*k, z0, z1]} for k, zs in merged.items() for z0, z1 in zs]
    out.sort(key=lambda b: b["box"])
    return out


def ceiling_extent(V, T) -> tuple[float, float] | None:
    """XY extent of the cavity-ceiling facets (downward, inside the body)."""
    V, T, cen, nz, _ = _faces(V, T)
    H = footprint_hull(V)
    if len(H) < 3:
        return None
    down_idx = np.nonzero((nz < -0.9) & (cen[:, 2] > V[:, 2].min() + MIN_Z))[0]
    if not len(down_idx):
        return None
    inside = orient._in_hull(cen[down_idx, :2], H)
    if not inside.any():
        return None
    P = V[np.unique(T[down_idx[inside]])]
    ext = P.max(axis=0) - P.min(axis=0)
    return float(ext[0]), float(ext[1])


def bridge_angle(V, T, x_long_angle: int = 90) -> str | None:
    ext = ceiling_extent(V, T)
    # spans of one stud pitch or less do not matter either way
    if ext and ext[0] > ext[1] + 0.5 and ext[0] > 8.5:
        return str(x_long_angle)
    return None


def stud_rings(V, T) -> list[dict]:
    """Circles on the top face: [{centre, d, segments}] (studs / hollow studs)."""
    V = np.asarray(V, dtype=np.float64)
    top = V[:, 2].max()
    P = np.unique(np.round(V[np.abs(V[:, 2] - top) < 1e-3][:, :2], 4), axis=0)
    if len(P) < 8:
        return []
    # connected components (link 0.6 mm; 48-gon spacing is 0.31 mm, a
    # 2.0 mm fallback catches 8-gons), grid-hashed union-find: O(n)
    px_l = P[:, 0].tolist()
    py_l = P[:, 1].tolist()
    n = len(P)
    out = []
    for link in (0.6, 2.0):
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        cells: dict = {}
        keys = np.floor(P / link).astype(np.int64)
        for i, (cx, cy) in enumerate(keys.tolist()):
            cells.setdefault((cx, cy), []).append(i)
        link2 = link * link
        for (cx, cy), idx in cells.items():
            near = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in cells.get((cx + dx, cy + dy), ())]
            for i in idx:
                xi, yi = px_l[i], py_l[i]
                for j in near:
                    if j > i:
                        dx = xi - px_l[j]
                        dy = yi - py_l[j]
                        if dx * dx + dy * dy < link2:
                            ri, rj = find(i), find(j)
                            if ri != rj:
                                parent[ri] = rj
        lab = np.fromiter((find(i) for i in range(n)), dtype=np.int64, count=n)
        out = []
        for l in np.unique(lab):
            Q = P[lab == l]
            if len(Q) < 6:
                continue
            c = Q.mean(axis=0)
            r = np.linalg.norm(Q - c, axis=1)
            ring = Q[np.abs(r - r.max()) < 0.02]
            if len(ring) < 6 or r.max() < 1.0:
                continue
            out.append({"centre": [round(float(x), 3) for x in c],
                        "d": round(float(2 * r.max()), 3), "segments": int(len(ring))})
        if out:
            break
    return out
