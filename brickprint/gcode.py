"""Bambu G-code reading: config block and extrusion paths.

``paths()`` returns one record per continuous extrusion (object label, z,
feature, line width, XY points; G2/G3 arcs are tessellated at 0.05 mm).
Physical sizes are derived from outer-wall centrelines: a contour is
centreline width + line width, a hole is centreline width - line width (the
slicer's intent, not a caliper reading).
"""

from __future__ import annotations

import math

import numpy as np

ANGLES = np.radians(np.arange(0, 180, 0.5))
DIRS = np.stack([np.cos(ANGLES), np.sin(ANGLES)], 1)


def config_block(path) -> dict:
    out, inside = {}, False
    with open(path, errors="ignore") as f:
        for line in f:
            if line.startswith("; CONFIG_BLOCK_START"):
                inside = True
                continue
            if line.startswith("; CONFIG_BLOCK_END"):
                break
            if inside and line.startswith("; ") and " = " in line:
                k, v = line[2:].rstrip("\n").split(" = ", 1)
                out[k.strip()] = v
    return out


def split_value(v: str) -> list[str]:
    """'250,255' / '"a";"b"' / 'x' -> list of strings."""
    v = v.strip()
    if v.startswith('"') and '";"' in v:
        return [x.strip('"') for x in v.split('";"')]
    if v.startswith('"') and v.endswith('"'):
        return [v[1:-1]]
    if "\\n" in v or " " in v:
        return [v]
    if ";" in v and "," not in v:
        return v.split(";")
    return v.split(",") if "," in v else [v]


def paths(path, features: set[str] | frozenset[str] | tuple[str, ...] | None = None) -> list[dict]:
    """Parse extrusion paths from a Bambu G-code file.

    When ``features`` is given (e.g. ``{"Outer wall", "Support", "Bridge"}``),
    only paths whose ``; FEATURE:`` matches are constructed and returned;
    non-matching features skip arc tessellation and point-array allocation while
    still tracking nozzle (x, y, z) state accurately across feature boundaries.
    """
    wanted = frozenset(features) if features is not None else None
    want = wanted is None
    x = y = z = 0.0
    obj = feat = None
    lw = None
    out: list[dict] = []
    pts: list[tuple[float, float]] | None = None
    cur_obj = cur_feat = None
    cur_z = 0.0
    cur_lw = None
    deferred_x: str | None = None
    deferred_y: str | None = None

    def flush():
        nonlocal pts
        if pts is not None:
            if len(pts) > 1:
                out.append({
                    "obj": cur_obj, "z": cur_z, "feat": cur_feat,
                    "lw": cur_lw, "pts": np.array(pts, dtype=np.float64),
                })
            pts = None

    def sync_xy():
        nonlocal x, y, deferred_x, deferred_y
        if deferred_x is not None:
            for t in deferred_x.split()[1:]:
                if len(t) > 1 and t[0] == "X":
                    try:
                        x = float(t[1:])
                    except ValueError:
                        pass
            deferred_x = None
        if deferred_y is not None:
            for t in deferred_y.split()[1:]:
                if len(t) > 1 and t[0] == "Y":
                    try:
                        y = float(t[1:])
                    except ValueError:
                        pass
            deferred_y = None

    two_pi = 2.0 * math.pi
    hypot = math.hypot
    atan2 = math.atan2
    cos = math.cos
    sin = math.sin

    with open(path, errors="ignore") as f:
        for line in f:
            c0 = line[0]
            if c0 == ";":
                if line.startswith("; FEATURE:"):
                    flush()
                    feat = line[10:].strip()
                    want = (wanted is None) or (feat in wanted)
                elif line.startswith("; LINE_WIDTH:"):
                    lw = float(line.split(":", 1)[1])
                elif line.startswith("; start printing object, unique label id:"):
                    flush()
                    obj = int(line.split(":", 1)[1])
                elif line.startswith("; stop printing object"):
                    flush()
                    obj = None
                continue
            if c0 != "G":
                continue
            if len(line) < 3:
                continue
            c1 = line[1]
            if c1 not in ("1", "2", "3") or line[2] not in (" ", "\t"):
                continue

            semi = line.find(";")
            s = line[:semi] if semi >= 0 else line

            if not want:
                if "X" in s:
                    deferred_x = s
                if "Y" in s:
                    deferred_y = s
                if "Z" in s:
                    for t in s.split()[1:]:
                        if len(t) > 1 and t[0] == "Z":
                            try:
                                z = float(t[1:])
                            except ValueError:
                                pass
                continue

            if deferred_x is not None or deferred_y is not None:
                sync_xy()

            nx, ny = x, y
            has_xy = False
            e_pos = False
            ci = cj = 0.0
            for t in s.split()[1:]:
                if len(t) > 1:
                    k = t[0]
                    if k == "X":
                        try:
                            nx = float(t[1:])
                            has_xy = True
                        except ValueError:
                            pass
                    elif k == "Y":
                        try:
                            ny = float(t[1:])
                            has_xy = True
                        except ValueError:
                            pass
                    elif k == "E":
                        try:
                            e_pos = float(t[1:]) > 0.0
                        except ValueError:
                            pass
                    elif k == "Z":
                        try:
                            z = float(t[1:])
                        except ValueError:
                            pass
                    elif k == "I":
                        try:
                            ci = float(t[1:])
                        except ValueError:
                            pass
                    elif k == "J":
                        try:
                            cj = float(t[1:])
                        except ValueError:
                            pass

            if e_pos and has_xy and feat:
                if pts is None or cur_obj != obj or cur_feat != feat or abs(cur_z - z) > 1e-6:
                    flush()
                    cur_obj, cur_z, cur_feat, cur_lw = obj, z, feat, lw
                    pts = [(x, y)]
                if c1 == "1":
                    pts.append((nx, ny))
                else:
                    cx, cy = x + ci, y + cj
                    r = hypot(x - cx, y - cy)
                    a0 = atan2(y - cy, x - cx)
                    a1 = atan2(ny - cy, nx - cx)
                    da = a1 - a0
                    if c1 == "3":
                        while da <= 0:
                            da += two_pi
                    else:
                        while da >= 0:
                            da -= two_pi
                    n = max(2, int(abs(da) * r / 0.05))
                    step = da / n
                    for i in range(1, n + 1):
                        a = a0 + step * i
                        pts.append((cx + r * cos(a), cy + r * sin(a)))
            elif has_xy and pts is not None:
                flush()
            x, y = nx, ny
    flush()
    return out


def calipers(P: np.ndarray) -> np.ndarray:
    pr = P @ DIRS.T
    return pr.max(0) - pr.min(0)


def resample(P: np.ndarray, step: float = 0.1) -> np.ndarray:
    """Points every ``step`` mm along the polyline (a square loop has only its
    4 corners, which lie exactly on a circle)."""
    P = np.asarray(P, dtype=float)
    if len(P) <= 1:
        return P
    seg = np.diff(P, axis=0)
    L = np.hypot(seg[:, 0], seg[:, 1])
    if (L < 2.0 * step).all():
        return P
    ns = np.maximum((L / step).astype(np.int64), 1)
    total = int(ns.sum())
    ends = np.cumsum(ns)
    starts = ends - ns
    idx = np.repeat(np.arange(len(ns)), ns)
    local = np.arange(total) - np.repeat(starts, ns) + 1
    t = (local / np.repeat(ns, ns))[:, None]
    pts = P[idx] + t * seg[idx]
    return np.vstack((P[:1], pts))


def circle_fit(P: np.ndarray) -> tuple[float, float, float, float]:
    """(cx, cy, r, rms residual) of the resampled loop."""
    P = resample(P)
    m = P.mean(axis=0)
    Q = P - m
    A = np.column_stack([2 * Q[:, 0], 2 * Q[:, 1], np.ones(len(Q))])
    b = (Q ** 2).sum(axis=1)
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    a, bb, c = sol
    r = math.sqrt(max(c + a * a + bb * bb, 0.0))
    res = np.hypot(Q[:, 0] - a, Q[:, 1] - bb) - r
    return float(a + m[0]), float(bb + m[1]), r, float(np.sqrt((res ** 2).mean()))


def is_closed(P: np.ndarray, tol: float = 0.6) -> bool:
    return len(P) >= 4 and float(np.linalg.norm(P[0] - P[-1])) <= tol
