"""Bambu G-code reading: config block and extrusion paths.

``paths()`` returns one record per continuous extrusion (object label, z,
feature, line width, XY points; G2/G3 arcs are tessellated at 0.05 mm).
Physical sizes are derived from outer-wall centrelines: a contour is
centreline width + line width, a hole is centreline width - line width (the
slicer's intent, not a caliper reading).
"""

from __future__ import annotations

import math
import re

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


def paths(path) -> list[dict]:
    x = y = z = 0.0
    obj = feat = None
    lw = None
    out: list[dict] = []
    cur = None

    def flush():
        nonlocal cur
        if cur and len(cur["pts"]) > 1:
            cur["pts"] = np.array(cur["pts"])
            out.append(cur)
        cur = None

    with open(path, errors="ignore") as f:
        for line in f:
            if line[0] == ";":
                if line.startswith("; FEATURE:"):
                    flush()
                    feat = line[10:].strip()
                elif line.startswith("; LINE_WIDTH:"):
                    lw = float(line.split(":")[1])
                elif line.startswith("; start printing object, unique label id:"):
                    flush()
                    obj = int(line.split(":")[1])
                elif line.startswith("; stop printing object"):
                    flush()
                    obj = None
                continue
            if line[:2] not in ("G1", "G2", "G3"):
                continue
            w = line.split(";")[0].split()
            g = w[0]
            d = {}
            for t in w[1:]:
                if len(t) > 1 and t[0] in "XYZEIJ":
                    try:
                        d[t[0]] = float(t[1:])
                    except ValueError:
                        pass
            nx, ny = d.get("X", x), d.get("Y", y)
            if "Z" in d:
                z = d["Z"]
            ext = d.get("E", 0) > 0 and ("X" in d or "Y" in d)
            if ext and feat:
                if cur is None or cur["obj"] != obj or cur["feat"] != feat or abs(cur["z"] - z) > 1e-6:
                    flush()
                    cur = {"obj": obj, "z": z, "feat": feat, "lw": lw, "pts": [(x, y)]}
                if g == "G1":
                    cur["pts"].append((nx, ny))
                else:
                    cx, cy = x + d.get("I", 0.0), y + d.get("J", 0.0)
                    r = math.hypot(x - cx, y - cy)
                    a0 = math.atan2(y - cy, x - cx)
                    a1 = math.atan2(ny - cy, nx - cx)
                    da = a1 - a0
                    if g == "G3":
                        while da <= 0:
                            da += 2 * math.pi
                    else:
                        while da >= 0:
                            da -= 2 * math.pi
                    n = max(2, int(abs(da) * r / 0.05))
                    for i in range(1, n + 1):
                        a = a0 + da * i / n
                        cur["pts"].append((cx + r * math.cos(a), cy + r * math.sin(a)))
            elif "X" in d or "Y" in d:
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
    seg = np.diff(P, axis=0)
    L = np.hypot(seg[:, 0], seg[:, 1])
    out = [P[:1]]
    for a, d, l in zip(P[:-1], seg, L):
        n = max(int(l / step), 1)
        t = (np.arange(1, n + 1) / n)[:, None]
        out.append(a + t * d)
    return np.vstack(out)


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
