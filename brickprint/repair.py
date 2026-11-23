"""Conservative mesh repair for LDView rendering shells (numpy only).

LDView exports LEGO parts as rendering shells: an outer skin, an inner skin,
stud cylinders sitting on faces, and deliberately open cavities under bricks.
A full "make it watertight" repair would close those cavities or flip
correctly wound facets, so this module only does the safe things:

1. merge vertices closer than ``MERGE_TOL`` (0.01 mm);
2. drop degenerate facets (repeated vertex or ~zero area);
3. remove duplicated facets: same vertex set + same winding -> keep one,
   same vertex set + opposite winding (back-to-back flap) -> remove both;
4. split T-junctions: a boundary vertex lying strictly inside another
   boundary edge (within 0.01 mm) splits that edge's facet; iterated;
5. fan-fill only tiny cracks: boundary loops whose bbox diagonal < 1 mm;
6. never propagate flips; a connected shell is flipped only when it is
   closed AND its signed volume is negative (logged).

Every step is counted in the returned report.  The module is shared verbatim
by tools/mesh_repair.py (Countach generator) and brickprint/repair.py.
"""

from __future__ import annotations

import numpy as np

REPAIR_VERSION = 1
MERGE_TOL = 0.01        # mm
DEGEN_AREA = 1e-6       # mm^2
TJUNC_TOL = 0.01        # mm
TJUNC_MAX_ITER = 30
CRACK_MAX_DIAG = 1.0    # mm


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def _edge_table(T: np.ndarray):
    """Return (undirected keys, directed (a,b), face index) for every half-edge."""
    a = T[:, [0, 1, 2]].ravel()
    b = T[:, [1, 2, 0]].ravel()
    f = np.repeat(np.arange(len(T)), 3)
    lo = np.minimum(a, b)
    hi = np.maximum(a, b)
    return lo, hi, a, b, f


def _tri_area(V: np.ndarray, T: np.ndarray) -> np.ndarray:
    if len(T) == 0:
        return np.zeros(0)
    c = np.cross(V[T[:, 1]] - V[T[:, 0]], V[T[:, 2]] - V[T[:, 0]])
    return 0.5 * np.linalg.norm(c, axis=1)


def signed_volume(V: np.ndarray, T: np.ndarray) -> float:
    if len(T) == 0:
        return 0.0
    V = np.asarray(V, dtype=np.float64)
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def mesh_metrics(V: np.ndarray, T: np.ndarray) -> dict:
    """Open / inconsistent / non-manifold edge counts, degenerates, volume."""
    V = np.asarray(V, dtype=np.float64)
    T = np.asarray(T, dtype=np.int64)
    out = {"faces": int(len(T)), "open_edges": 0, "inconsistent_edges": 0,
           "nonmanifold_edges": 0, "degenerate": 0, "volume": 0.0}
    if len(T) == 0:
        return out
    rep = (T[:, 0] == T[:, 1]) | (T[:, 1] == T[:, 2]) | (T[:, 0] == T[:, 2])
    out["degenerate"] = int((rep | (_tri_area(V, T) < DEGEN_AREA)).sum())
    lo, hi, a, b, _ = _edge_table(T)
    ok = lo != hi
    key = lo[ok] * (int(T.max()) + 1) + hi[ok]
    fwd = (a[ok] < b[ok]).astype(np.int64)
    uk, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    nf = np.bincount(inv, weights=fwd, minlength=len(uk))
    out["open_edges"] = int((cnt == 1).sum())
    out["nonmanifold_edges"] = int((cnt > 2).sum())
    # a 2-face edge is consistent when one face walks a->b and the other b->a
    two = cnt == 2
    out["inconsistent_edges"] = int((two & (nf != 1)).sum())
    out["volume"] = signed_volume(V, T)
    return out


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

def _merge_vertices(V: np.ndarray, T: np.ndarray, tol: float):
    """Union vertices within ``tol`` (checks the 27 neighbouring grid cells)."""
    n = len(V)
    q = np.floor(V / tol).astype(np.int64)
    parent = np.arange(n)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    cells: dict[tuple, list[int]] = {}
    for i, key in enumerate(map(tuple, q)):
        cells.setdefault(key, []).append(i)
    offs = [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)]
    tol2 = tol * tol
    for key, members in cells.items():
        cand = []
        for dx, dy, dz in offs:
            other = cells.get((key[0] + dx, key[1] + dy, key[2] + dz))
            if other:
                cand.extend(other)
        if len(cand) < 2:
            continue
        cand = np.asarray(cand)
        P = V[cand]
        for i in members:
            d2 = ((P - V[i]) ** 2).sum(axis=1)
            for j in cand[(d2 <= tol2) & (cand != i)]:
                ri, rj = find(i), find(int(j))
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    roots = np.array([find(i) for i in range(n)])
    uniq, newidx = np.unique(roots, return_inverse=True)
    # merged position = position of the representative (keeps grid-exact coords)
    V2 = V[uniq]
    T2 = newidx[T]
    return V2, T2, int(n - len(uniq))


def _drop_degenerate(V, T):
    rep = (T[:, 0] == T[:, 1]) | (T[:, 1] == T[:, 2]) | (T[:, 0] == T[:, 2])
    bad = rep | (_tri_area(V, T) < DEGEN_AREA)
    return T[~bad], int(bad.sum())


def _canonical_rotation(T):
    """Rotate each triangle so its smallest index comes first (keeps winding)."""
    r = np.argmin(T, axis=1)
    idx = (np.arange(3)[None, :] + r[:, None]) % 3
    return np.take_along_axis(T, idx, axis=1)


def _dedup_faces(T):
    if len(T) == 0:
        return T, 0, 0
    C = _canonical_rotation(T)
    S = np.sort(T, axis=1)
    # winding sign: canonical (m, x, y) is "positive" if x < y
    pos = C[:, 1] < C[:, 2]
    _, inv, cnt = np.unique(S, axis=0, return_inverse=True, return_counts=True)
    inv = inv.ravel()
    keep = np.ones(len(T), dtype=bool)
    dup_same = 0
    flap_pairs = 0
    for g in np.nonzero(cnt > 1)[0]:
        members = np.nonzero(inv == g)[0]
        p = members[pos[members]]
        m = members[~pos[members]]
        if len(p) and len(m):
            keep[members] = False          # back-to-back flap(s): remove all
            flap_pairs += min(len(p), len(m))
        else:
            keep[members[1:]] = False      # exact duplicate: keep one
            dup_same += len(members) - 1
    return T[keep], dup_same, flap_pairs


def _boundary_halfedges(T):
    lo, hi, a, b, f = _edge_table(T)
    key = lo.astype(np.int64) * (int(T.max()) + 1 if len(T) else 1) + hi
    _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    open_mask = cnt[inv] == 1
    return a[open_mask], b[open_mask], f[open_mask]


def _split_tjunctions(V, T, tol=TJUNC_TOL, max_iter=TJUNC_MAX_ITER):
    total = 0
    for _ in range(max_iter):
        if len(T) == 0:
            break
        ea, eb, ef = _boundary_halfedges(T)
        if len(ea) == 0:
            break
        bverts = np.unique(np.concatenate([ea, eb]))
        A = V[ea]
        D = V[eb] - A
        L2 = (D ** 2).sum(axis=1)
        good = L2 > (2 * tol) ** 2
        splits = {}   # face -> (local edge position, vertex)
        # chunk over vertices to bound memory
        P = V[bverts]
        for s in range(0, len(bverts), 512):
            Pc = P[s:s + 512]
            W = Pc[:, None, :] - A[None, :, :]                # (nv, ne, 3)
            t = (W * D[None]).sum(axis=2) / np.where(L2 > 0, L2, 1)[None]
            proj = A[None] + t[..., None] * D[None]
            dist2 = ((Pc[:, None, :] - proj) ** 2).sum(axis=2)
            ln = np.sqrt(L2)[None]
            inside = (t * ln > tol) & ((1 - t) * ln > tol)
            hit = inside & (dist2 <= tol * tol) & good[None]
            vi, ei = np.nonzero(hit)
            for v_local, e in zip(vi, ei):
                v = int(bverts[s + v_local])
                if v == ea[e] or v == eb[e]:
                    continue
                f = int(ef[e])
                tt = float(t[v_local, e])
                prev = splits.get(f)
                # per face, per iteration: split one edge, at the vertex closest to its start
                if prev is None or (prev[0] == (int(ea[e]), int(eb[e])) and tt < prev[2]):
                    splits[f] = ((int(ea[e]), int(eb[e])), v, tt)
        if not splits:
            break
        keep = np.ones(len(T), dtype=bool)
        new = []
        for f, ((a, b), v, _) in splits.items():
            tri = T[f].tolist()
            # find c, the vertex opposite edge a->b, preserving winding a->b->c
            for k in range(3):
                if tri[k] == a and tri[(k + 1) % 3] == b:
                    c = tri[(k + 2) % 3]
                    break
            else:
                continue
            keep[f] = False
            new.append((a, v, c))
            new.append((v, b, c))
        if not new:
            break
        T = np.vstack([T[keep], np.asarray(new, dtype=T.dtype)])
        total += len(new) // 2
    return T, total


def _boundary_loops(T):
    ea, eb, _ = _boundary_halfedges(T)
    nxt: dict[int, list[int]] = {}
    for a, b in zip(ea.tolist(), eb.tolist()):
        nxt.setdefault(a, []).append(b)
    used = set()
    loops = []
    for a0, b0 in zip(ea.tolist(), eb.tolist()):
        if (a0, b0) in used:
            continue
        loop = [a0]
        used.add((a0, b0))
        cur = b0
        closed = False
        for _ in range(100000):
            if cur == a0:
                closed = True
                break
            loop.append(cur)
            cand = [n for n in nxt.get(cur, []) if (cur, n) not in used]
            if not cand:
                break
            n = cand[0]
            used.add((cur, n))
            cur = n
        if closed and len(loop) >= 3:
            loops.append(loop)
    return loops


def _fill_tiny_cracks(V, T, max_diag=CRACK_MAX_DIAG):
    if len(T) == 0:
        return T, 0, 0
    new = []
    filled = 0
    for loop in _boundary_loops(T):
        P = V[loop]
        diag = float(np.linalg.norm(P.max(axis=0) - P.min(axis=0)))
        if diag >= max_diag:
            continue
        # boundary half-edges run l[i] -> l[i+1]; the fill must walk the opposite way
        for i in range(1, len(loop) - 1):
            new.append((loop[0], loop[i + 1], loop[i]))
        filled += 1
    if not new:
        return T, 0, 0
    T2 = np.vstack([T, np.asarray(new, dtype=T.dtype)])
    T2, _ = _drop_degenerate(V, T2)
    return T2, filled, len(new)


def _shells(T):
    """Connected components of facets sharing an (undirected) edge."""
    n = len(T)
    parent = np.arange(n)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    lo, hi, _, _, f = _edge_table(T)
    key = lo.astype(np.int64) * (int(T.max()) + 1) + hi
    order = np.argsort(key, kind="stable")
    ks, fs = key[order], f[order]
    same = np.nonzero(ks[1:] == ks[:-1])[0]
    for i in same:
        ra, rb = find(int(fs[i])), find(int(fs[i + 1]))
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    roots = np.array([find(i) for i in range(n)])
    return roots


def _flip_inverted_closed_shells(V, T):
    if len(T) == 0:
        return T, 0, []
    roots = _shells(T)
    flipped = 0
    log = []
    T = T.copy()
    for r in np.unique(roots):
        idx = np.nonzero(roots == r)[0]
        sub = T[idx]
        m = mesh_metrics(V, sub)
        closed = m["open_edges"] == 0 and m["nonmanifold_edges"] == 0
        if closed and m["volume"] < 0:
            T[idx] = sub[:, [0, 2, 1]]
            flipped += 1
            log.append(f"flipped closed shell of {len(idx)} facets (volume {m['volume']:.2f} mm3)")
    return T, flipped, log


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------

def repair_mesh(V, T, name: str = "", report: dict | None = None):
    """Return (V, T, report) after the safe repair steps.

    ``V`` is (n,3) float mm, ``T`` is (m,3) int.  Output dtypes: float32/int32.
    """
    V = np.asarray(V, dtype=np.float64)
    T = np.asarray(T, dtype=np.int64)
    before = mesh_metrics(V, T)
    V, T, merged = _merge_vertices(V, T, MERGE_TOL)
    T, degen = _drop_degenerate(V, T)
    T, dup_same, flaps = _dedup_faces(T)
    T, tsplit = _split_tjunctions(V, T)
    T, degen2 = _drop_degenerate(V, T)
    T, cracks, crack_faces = _fill_tiny_cracks(V, T)
    T, shells_flipped, log = _flip_inverted_closed_shells(V, T)
    # drop vertices no longer referenced
    used = np.unique(T)
    remap = np.full(len(V), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    V = V[used]
    T = remap[T]
    after = mesh_metrics(V, T)
    vb, va = before["volume"], after["volume"]
    rep = {
        "name": name,
        "before": before,
        "after": after,
        "merged_vertices": merged,
        "degenerate_removed": degen + degen2,
        "duplicate_faces_removed": dup_same,
        "backtoback_pairs_removed": flaps,
        "tjunction_splits": tsplit,
        "cracks_filled": cracks,
        "crack_faces_added": crack_faces,
        "shells_flipped": shells_flipped,
        "volume_change_pct": (100.0 * (va - vb) / abs(vb)) if abs(vb) > 1e-9 else 0.0,
        "log": log,
    }
    if report is not None:
        report[name] = rep
    return V.astype(np.float32), T.astype(np.int32), rep


def summarize(reports: dict) -> dict:
    keys = ("open_edges", "inconsistent_edges", "nonmanifold_edges", "degenerate")
    tot = {f"{k}_{w}": sum(r[w][k] for r in reports.values()) for k in keys for w in ("before", "after")}
    tot["meshes"] = len(reports)
    tot["max_abs_volume_change_pct"] = max((abs(r["volume_change_pct"]) for r in reports.values()), default=0.0)
    tot["shells_flipped"] = sum(r["shells_flipped"] for r in reports.values())
    tot["tjunction_splits"] = sum(r["tjunction_splits"] for r in reports.values())
    tot["cracks_filled"] = sum(r["cracks_filled"] for r in reports.values())
    tot["backtoback_pairs_removed"] = sum(r["backtoback_pairs_removed"] for r in reports.values())
    tot["duplicate_faces_removed"] = sum(r["duplicate_faces_removed"] for r in reports.values())
    return tot
