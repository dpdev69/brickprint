"""Per-part mesh quality report (read-only).

For every unique part: which LDView geometry was used (48-gon quality export
or the 16-gon fallback), triangle count, open / non-manifold / inconsistent
edges and signed volume of the repaired mesh, the top-face stud circles
(segment count, diameter) and a nominal-dimension check (stud 4.8 mm, footprint
a multiple of the 8 mm pitch).  Open edges are normal for LDraw parts (studs
sit on faces, undersides are open); they are reported, not "fixed".
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from . import features, repair

STUD_D = 4.8
TOL = 0.02


def part_report(part_file: str, V, T, export_info: dict | None = None) -> dict:
    V = np.asarray(V, dtype=np.float64)
    T = np.asarray(T, dtype=np.int64)
    m = repair.mesh_metrics(V, T)
    rings = features.stud_rings(V, T)
    studs = [r for r in rings if abs(r["d"] - STUD_D) < 0.5]
    size = (V.max(axis=0) - V.min(axis=0)).tolist()
    flags = []
    segs = sorted({r["segments"] for r in studs})
    if segs and min(segs) < 48:
        flags.append(f"low-poly studs ({min(segs)}-gon)")
    for r in studs:
        if abs(r["d"] - STUD_D) > TOL:
            flags.append(f"stud d {r['d']} != {STUD_D}")
            break
    on_grid = all(abs(s / 8.0 - round(s / 8.0)) * 8.0 < TOL and s > 1 for s in size[:2])
    if m["nonmanifold_edges"]:
        flags.append(f"{m['nonmanifold_edges']} non-manifold edges")
    if m["volume"] <= 0:
        flags.append("non-positive volume")
    info = export_info or {}
    if str(info.get("variant", "")).startswith("fallback"):
        flags.append("16-gon fallback export")
    return {
        "part_file": part_file,
        "export": info.get("variant", "unknown"),
        "triangles": int(len(T)),
        "open_edges": m["open_edges"],
        "nonmanifold_edges": m["nonmanifold_edges"],
        "inconsistent_edges": m["inconsistent_edges"],
        "volume_mm3": round(m["volume"], 1),
        "studs": len(studs),
        "stud_segments": segs,
        "stud_d": sorted({r["d"] for r in studs}),
        "size_mm": [round(s, 2) for s in size],
        "footprint_on_8mm_grid": bool(on_grid),
        "flags": flags,
    }


def report(cache, part_files) -> list[dict]:
    out = []
    for pf in sorted(part_files):
        V, T = cache.get(pf)
        out.append(part_report(pf, V, T, cache.export_info(pf)))
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = ["part_file", "export", "triangles", "open_edges", "nonmanifold_edges",
              "inconsistent_edges", "volume_mm3", "studs", "stud_segments", "stud_d",
              "size_mm", "footprint_on_8mm_grid", "flags"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("; ".join(map(str, r[k])) if isinstance(r[k], list) else r[k])
                        for k in fields})


def print_table(rows: list[dict]) -> None:
    print(f"{'part':14s} {'tris':>6s} {'open':>5s} {'nonm':>5s} {'studs':>5s} {'segs':>6s} "
          f"{'stud d':>8s}  export / flags")
    for r in rows:
        print(f"{r['part_file']:14s} {r['triangles']:6d} {r['open_edges']:5d} "
              f"{r['nonmanifold_edges']:5d} {r['studs']:5d} "
              f"{','.join(map(str, r['stud_segments'])) or '-':>6s} "
              f"{','.join(map(str, r['stud_d'])) or '-':>8s}  {r['export']}"
              + (f"  !! {'; '.join(r['flags'])}" if r["flags"] else ""))
