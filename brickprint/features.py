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
        t = np.clip(((P - a) * ab).sum(axis=1) / max(float(ab @ ab), 1e-12), 0.0, 1.0)
        diff = P - (a + t[:, None] * ab)
        d2 = np.minimum(d2, (diff * diff).sum(axis=1))
    return np.sqrt(d2)


def _norm_boxes(items) -> list[list[float]]:
    """Normalize a sequence of ``{"box": [...]}`` dicts or 6-element box coords."""
    if not items:
        return []
    out = []
    for it in items:
        bx = it["box"] if isinstance(it, dict) else it
        out.append([float(v) for v in bx])
    return out


def _subtract_box(a: list[float], b: list[float], eps: float = 1e-6) -> list[list[float]]:
    """Return disjoint axis-aligned boxes covering ``a \\ b``."""
    ax0, ax1, ay0, ay1, az0, az1 = a
    bx0, bx1, by0, by1, bz0, bz1 = b
    if (ax1 <= bx0 + eps or bx1 <= ax0 + eps
            or ay1 <= by0 + eps or by1 <= ay0 + eps
            or az1 <= bz0 + eps or bz1 <= az0 + eps):
        return [a]
    ix0, ix1 = max(ax0, bx0), min(ax1, bx1)
    iy0, iy1 = max(ay0, by0), min(ay1, by1)
    iz0, iz1 = max(az0, bz0), min(az1, bz1)
    out = []
    if az0 < iz0 - eps:
        out.append([ax0, ax1, ay0, ay1, az0, iz0])
    if iz1 < az1 - eps:
        out.append([ax0, ax1, ay0, ay1, iz1, az1])
    if ax0 < ix0 - eps:
        out.append([ax0, ix0, ay0, ay1, iz0, iz1])
    if ix1 < ax1 - eps:
        out.append([ix1, ax1, ay0, ay1, iz0, iz1])
    if ay0 < iy0 - eps:
        out.append([ix0, ix1, ay0, iy0, iz0, iz1])
    if iy1 < ay1 - eps:
        out.append([ix0, ix1, iy1, ay1, iz0, iz1])
    return out


def subtract_boxes(
    boxes: list[dict],
    exclude,
    min_area: float = 0.05,
    min_dz: float = 1e-3,
) -> list[dict]:
    """Subtract ``exclude`` boxes from ``boxes``, dropping empty/tiny slivers."""
    excl = _norm_boxes(exclude)
    if not excl or not boxes:
        return list(boxes)
    out = []
    for item in boxes:
        bx = [float(v) for v in (item["box"] if isinstance(item, dict) else item)]
        pieces = [bx]
        for ex in excl:
            pieces = [p2 for p in pieces for p2 in _subtract_box(p, ex)]
            if not pieces:
                break
        extra = {k: v for k, v in item.items() if k != "box"} if isinstance(item, dict) else {}
        for p in pieces:
            if (p[1] - p[0]) * (p[3] - p[2]) >= min_area and (p[5] - p[4]) >= min_dz:
                out.append({"box": [round(v, 3) for v in p], **extra})
    return out


def mesh_boxes(V, T) -> list[list[float]]:
    """Recover per-box AABBs from a ``box_mesh`` ``(V, T)`` tuple."""
    V = np.asarray(V, dtype=np.float64)
    T = np.asarray(T, dtype=np.int64)
    if len(V) == 0 or len(T) == 0:
        return []
    if len(V) % 8 == 0 and len(T) == (len(V) // 8) * 12:
        out = []
        for i in range(0, len(V), 8):
            chunk = V[i:i + 8]
            lo, hi = chunk.min(axis=0), chunk.max(axis=0)
            out.append([float(lo[0]), float(hi[0]), float(lo[1]), float(hi[1]),
                        float(lo[2]), float(hi[2])])
        return out
    out = []
    for g in _clusters(T, np.arange(len(T))):
        P = V[np.unique(T[g])]
        lo, hi = P.min(axis=0), P.max(axis=0)
        out.append([float(lo[0]), float(hi[0]), float(lo[1]), float(hi[1]),
                    float(lo[2]), float(hi[2])])
    return out


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
    zmin0 = float(V[:, 2].min())
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

    # Exclude downward faces whose outward normal enters ANY 2D-enclosed cavity
    # (roofed sockets on 41749/46413/50967/30099 OR open-top upside-down sockets
    # on 30363) so exterior cantilever enforcers never force supports inside walls.
    mT, raw_encT, encT, extT, gx0, gy0, gzmin, gpx = _enclosed_stack_full(V, T)
    nl, nx, ny = raw_encT.shape
    blk_mask = None
    if raw_encT.sum() * gpx * gpx >= BLOCK_MIN_MM2:
        A, B, C = V[T[cand, 0]], V[T[cand, 1]], V[T[cand, 2]]
        N = np.cross(B - A, C - A)
        nlen = np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-12)
        Nhat = N / nlen
        p1 = cen[cand] + 0.3 * Nhat
        p2 = cen[cand] + 0.6 * Nhat
        i1 = np.clip(np.floor((p1[:, 0] - gx0) / gpx).astype(np.int64), 0, nx - 1)
        j1 = np.clip(np.floor((p1[:, 1] - gy0) / gpx).astype(np.int64), 0, ny - 1)
        k1 = np.clip(np.floor((p1[:, 2] - gzmin) / BLOCK_LAYER).astype(np.int64), 0, nl - 1)
        i2 = np.clip(np.floor((p2[:, 0] - gx0) / gpx).astype(np.int64), 0, nx - 1)
        j2 = np.clip(np.floor((p2[:, 1] - gy0) / gpx).astype(np.int64), 0, ny - 1)
        k2 = np.clip(np.floor((p2[:, 2] - gzmin) / BLOCK_LAYER).astype(np.int64), 0, nl - 1)
        in_enc = raw_encT[k1, i1, j1] | (mT[k1, i1, j1] & raw_encT[k2, i2, j2])
        keep = ~in_enc
        cand, dist = cand[keep], dist[keep]
        if not len(cand):
            return []
        blk_boxes = _socket_blockers_from_stack(
            mT, encT, extT, gx0, gy0, gzmin, gpx, BLOCK_LAYER
        )
        # Keep carved cantilever enforcers strictly outside ALL 2D-enclosed
        # cavities (both roofed sockets and open-top upside-down cavities like
        # 30363), their inner walls (+0.75 mm XY), and ceilings (+0.6 mm Z), so
        # normal(manual) supports never leak inside walls while staying 100%
        # disjoint from socket_blockers.
        blk_mask = _boxes_to_mask(blk_boxes, mT.shape, gx0, gy0, gzmin, gpx, BLOCK_LAYER) if blk_boxes else np.zeros_like(mT)
        pad_r = max(2, int(round(0.75 / gpx)))
        up_l = max(2, int(np.ceil((BLOCK_UP + BLOCK_LAYER) / BLOCK_LAYER - 1e-9)))
        enc_dil = np.zeros_like(mT)
        ed_cache: dict[bytes, np.ndarray] = {}
        for k in range(nl):
            if raw_encT[k].any():
                key = raw_encT[k].tobytes()
                d2 = ed_cache.get(key)
                if d2 is None:
                    d2 = _dilate_2d(raw_encT[k], r=pad_r, border=False)
                    ed_cache[key] = d2
                enc_dil[k] = d2
        for dm in range(1, up_l + 1):
            enc_dil[dm:] |= enc_dil[:-dm]
        blk_mask |= enc_dil
        if not blk_mask.any():
            blk_mask = None

    dmap = dict(zip(cand.tolist(), dist.tolist()))
    bx0, by0 = H.min(axis=0)
    bx1, by1 = H.max(axis=0)
    # clusters whose XY bboxes genuinely overlap belong to one feature (outer
    # and inner surface of a hollow side stud); strict overlap prevents hollow
    # perimeter rims that merely touch at a corner from merging across a cavity.
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
                if (a[1][0] < b[2][0] - 1e-3 and b[1][0] < a[2][0] - 1e-3
                        and a[1][1] < b[2][1] - 1e-3 and b[1][1] < a[2][1] - 1e-3):
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
        z1 = hi[2] + above
        prot_r = round(float(prot), 3)
        area_r = round(float(0.5 * a2[g].sum()), 2)

        # If a curved/hollow exterior feature's AABB overlaps an enclosed cavity
        # blocker, carve out the interior cavity (B \ blk_mask) using don't-care
        # slab merging over exterior air + outer skin so enforcers stay compact,
        # continuous across sloped/curved triangles, and never leak into sockets.
        if blk_mask is not None:
            gi0 = max(0, int(np.floor((x0 - gx0) / gpx)))
            gi1 = min(nx, int(np.ceil((x1 - gx0) / gpx)))
            gj0 = max(0, int(np.floor((y0 - gy0) / gpx)))
            gj1 = min(ny, int(np.ceil((y1 - gy0) / gpx)))
            gk0 = max(0, int(np.floor((z0 - gzmin) / BLOCK_LAYER)))
            gk1 = min(nl, int(np.ceil((z1 - gzmin) / BLOCK_LAYER)))
            if gi0 < gi1 and gj0 < gj1 and gk0 < gk1 and blk_mask[gk0:gk1, gi0:gi1, gj0:gj1].any():
                avail_sub = ~blk_mask[gk0:gk1, gi0:gi1, gj0:gj1]
                # Require covering exterior air + 1-cell exterior skin (where
                # overhangs live); remaining solid outside blk_mask is don't-care.
                n_sub = gk1 - gk0
                req_sub = np.zeros_like(avail_sub)
                for dz in range(n_sub):
                    e2 = extT[gk0 + dz, gi0:gi1, gj0:gj1]
                    if e2.any():
                        req_sub[dz] = _dilate_2d(e2, r=1, border=False)
                req_sub &= avail_sub
                if not req_sub.any():
                    continue
                carved = []
                kk = 0
                while kk < n_sub:
                    if not req_sub[kk].any():
                        kk += 1
                        continue
                    se = req_sub[kk].copy()
                    sa = avail_sub[kk].copy()
                    kk1 = kk + 1
                    while kk1 < n_sub and req_sub[kk1].any():
                        ne = se | req_sub[kk1]
                        na = sa & avail_sub[kk1]
                        if (ne & ~na).any():
                            break
                        se, sa = ne, na
                        kk1 += 1
                    for ri0, ri1, rj0, rj1 in _cover_with_dontcares(se, sa):
                        if (ri1 - ri0) * (rj1 - rj0) * gpx * gpx < 0.05:
                            continue
                        bx0_c = x0 if ri0 == 0 else max(x0, gx0 + (gi0 + ri0) * gpx)
                        bx1_c = x1 if ri1 == (gi1 - gi0) else min(x1, gx0 + (gi0 + ri1) * gpx)
                        by0_c = y0 if rj0 == 0 else max(y0, gy0 + (gj0 + rj0) * gpx)
                        by1_c = y1 if rj1 == (gj1 - gj0) else min(y1, gy0 + (gj0 + rj1) * gpx)
                        bz0_c = z0 if kk == 0 else max(z0, gzmin + (gk0 + kk) * BLOCK_LAYER)
                        bz1_c = z1 if kk1 == n_sub else min(z1, gzmin + (gk0 + kk1) * BLOCK_LAYER)
                        if (bx1_c - bx0_c) * (by1_c - by0_c) >= 0.05 and bz1_c - bz0_c >= 1e-3:
                            carved.append([round(float(bx0_c), 3), round(float(bx1_c), 3),
                                           round(float(by0_c), 3), round(float(by1_c), 3),
                                           round(float(bz0_c), 3), round(float(bz1_c), 3)])
                    kk = kk1
                # Merge carved slabs that share identical XY rectangles in Z
                c_merged: dict[tuple, list] = {}
                for cb in sorted(carved, key=lambda b: (b[:4], b[4])):
                    key = tuple(cb[:4])
                    m = c_merged.get(key)
                    if m and m[-1][1] >= cb[4] - 1e-6:
                        m[-1][1] = max(m[-1][1], cb[5])
                    else:
                        c_merged.setdefault(key, []).append([cb[4], cb[5]])
                for key, zs in c_merged.items():
                    for cz0, cz1 in zs:
                        boxes.append({
                            "box": [*key, cz0, cz1],
                            "protrusion_mm": prot_r,
                            "area_mm2": area_r,
                        })
                continue

        boxes.append({"box": [float(x0), float(x1), float(y0), float(y1), float(z0),
                              float(z1)],
                      "protrusion_mm": prot_r,
                      "area_mm2": area_r})
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


BLOCK_WALL_INSET = 0.5  # mm: minimum inset of solid don't-care expansion from exterior air


def _dilate_2d(M: np.ndarray, r: int, border: bool = True) -> np.ndarray:
    """Chebyshev dilation of a 2D bool mask by ``r`` cells."""
    out = M
    for _ in range(r):
        p = np.pad(out, 1, constant_values=border)
        h = p[:, :-2] | p[:, 1:-1] | p[:, 2:]
        out = h[:-2, :] | h[1:-1, :] | h[2:, :]
    return out


def _boxes_to_mask(
    boxes, shape: tuple[int, int, int], x0: float, y0: float,
    zmin: float, px: float, layer: float,
) -> np.ndarray:
    nl, nx, ny = shape
    M = np.zeros(shape, dtype=bool)
    for ex0, ex1, ey0, ey1, ez0, ez1 in _norm_boxes(boxes):
        i0 = max(0, int(np.floor((ex0 - x0) / px)))
        i1 = min(nx, int(np.ceil((ex1 - x0) / px)))
        j0 = max(0, int(np.floor((ey0 - y0) / px)))
        j1 = min(ny, int(np.ceil((ey1 - y0) / px)))
        k0 = max(0, int(np.floor((ez0 - zmin) / layer)))
        k1 = min(nl, int(np.ceil((ez1 - zmin) / layer)))
        if i0 < i1 and j0 < j1 and k0 < k1:
            M[k0:k1, i0:i1, j0:j1] = True
    return M


def _enclosed_stack_full(
    V, T, layer: float = BLOCK_LAYER, px: float = BLOCK_PX,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float, float, float, float]:
    """Slice ``(V, T)`` and classify each layer into solid ``mT``, all 2D-enclosed
    cavities ``raw_encT`` (including open-top cavities), roofed enclosed cavities
    ``encT``, and border-reachable external air ``extT``."""
    V = np.asarray(V, dtype=np.float64)
    zmin = float(V[:, 2].min())
    W = V - [0.0, 0.0, zmin]
    mask, px = orient.slice_stack(W, T, layer=layer, px=px)
    x0, y0 = orient.SLICE_X0 + float(W[:, 0].min()), orient.SLICE_Y0 + float(W[:, 1].min())
    mT = np.ascontiguousarray(mask.transpose(2, 0, 1))
    nl = mT.shape[0]
    enc_cache: dict[bytes, np.ndarray] = {}
    ext_cache: dict[bytes, np.ndarray] = {}
    raw_encT = np.zeros_like(mT)
    extT = np.zeros_like(mT)
    for k in range(nl):
        key = mT[k].tobytes()
        e2d = enc_cache.get(key)
        if e2d is None:
            free2d = ~mT[k]
            r2d = _flood_2d_free(free2d)
            e2d = free2d & ~r2d
            enc_cache[key] = e2d
            ext_cache[key] = r2d
        raw_encT[k] = e2d
        extT[k] = ext_cache[key]
    roofT = np.zeros_like(mT)
    roofT[:-1] = np.flip(np.logical_or.accumulate(np.flip(mT[1:], 0), 0), 0)
    encT = raw_encT & roofT
    return mT, raw_encT, encT, extT, x0, y0, zmin, px


def _enclosed_stack(
    V, T, layer: float = BLOCK_LAYER, px: float = BLOCK_PX,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, float, float, float]:
    """Slice ``(V, T)`` and classify each layer into solid ``mT``, enclosed
    roofed cavities ``encT``, and border-reachable external air ``extT``."""
    mT, _, encT, extT, x0, y0, zmin, px = _enclosed_stack_full(V, T, layer=layer, px=px)
    return mT, encT, extT, x0, y0, zmin, px


def _socket_blockers_from_stack(
    mT: np.ndarray,
    encT: np.ndarray,
    extT: np.ndarray,
    x0: float,
    y0: float,
    zmin: float,
    px: float,
    layer: float,
    exclude=None,
) -> list[dict]:
    nl, nx, ny = mT.shape
    if encT.sum() * px * px < BLOCK_MIN_MM2:
        return []
    # Erode solid material away from external air before allowing don't-care
    # expansion so blocker boxes stay strictly interior (>= 0.5 mm inset from
    # exterior skin) and never bleed onto curved outer walls.
    erode_r = max(1, int(round(BLOCK_WALL_INSET / px)))
    dil_cache: dict[bytes, np.ndarray] = {}
    allowedT = np.zeros_like(mT)
    for k in range(nl):
        key = extT[k].tobytes()
        d2d = dil_cache.get(key)
        if d2d is None:
            d2d = _dilate_2d(extT[k], r=erode_r)
            dil_cache[key] = d2d
        allowedT[k] = encT[k] | (mT[k] & ~d2d)

    excl_boxes = _norm_boxes(exclude)
    exclT = _boxes_to_mask(excl_boxes, mT.shape, x0, y0, zmin, px, layer) if excl_boxes else None
    enc_work = encT & ~exclT if exclT is not None else encT
    all_work = allowedT & ~exclT if exclT is not None else allowedT
    if enc_work.sum() * px * px < BLOCK_MIN_MM2:
        return []

    up_layers = max(1, int(np.ceil(BLOCK_UP / layer - 1e-9)))
    boxes = []
    k = 0
    while k < nl:
        if not enc_work[k].any():
            k += 1
            continue
        slab_enc = enc_work[k].copy()
        slab_all = all_work[k].copy()
        k1 = k + 1
        while k1 < nl and enc_work[k1].any():
            nxt_enc = slab_enc | enc_work[k1]
            nxt_all = slab_all & all_work[k1]
            if (nxt_enc & ~nxt_all).any():
                break
            slab_enc = nxt_enc
            slab_all = nxt_all
            k1 += 1
        for i0, i1, j0, j1 in _cover_with_dontcares(slab_enc, slab_all):
            if (i1 - i0) * (j1 - j0) * px * px < 0.05:
                continue
            # Ensure top BLOCK_UP extension never pokes into external air or excluded zones
            safe_up = 0
            for dm in range(up_layers):
                m = k1 + dm
                if m >= nl:
                    break
                if extT[m, i0:i1, j0:j1].any() or (exclT is not None and exclT[m, i0:i1, j0:j1].any()):
                    break
                safe_up = dm + 1
            bx = {"box": [round(float(x0 + i0 * px), 3), round(float(x0 + i1 * px), 3),
                          round(float(y0 + j0 * px), 3), round(float(y0 + j1 * px), 3),
                          round(float(zmin + k * layer), 3),
                          round(float(zmin + (k1 + safe_up) * layer), 3)]}
            if excl_boxes:
                boxes.extend(subtract_boxes([bx], excl_boxes))
            else:
                boxes.append(bx)
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


def socket_blockers(
    V, T, layer: float = BLOCK_LAYER, px: float = BLOCK_PX, exclude=None,
) -> list[dict]:
    """Support-blocker boxes over the enclosed cavities of a posed part.

    A cell of layer k is *enclosed* when it is empty and cannot be reached
    from outside the part's bounding box within that layer: it is surrounded
    by the part's own walls (stud socket, tube, vertical hole, any closed
    hollow).  Support there could never come out, and a stud no longer fits.
    The layers come from orient.slice_stack (the slicer emulation, robust to
    LDraw winding).  Each enclosed region becomes boxes that reach up to
    BLOCK_UP mm into the ceiling above it without entering external air.
    Optional ``exclude`` boxes (e.g. ``cantilever_boxes`` enforcers) are
    clipped out so blockers never overlap or cancel enforcers."""
    mT, encT, extT, x0, y0, zmin, px = _enclosed_stack(V, T, layer=layer, px=px)
    return _socket_blockers_from_stack(
        mT, encT, extT, x0, y0, zmin, px, layer, exclude=exclude
    )


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
