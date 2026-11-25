"""LDraw part resolution and LDView mesh extraction."""

from __future__ import annotations

import hashlib
import os
import re
import struct
import subprocess
import tempfile
from pathlib import Path

import numpy as np

# LDView geometry settings, passed explicitly so meshes never depend on LDView's
# saved prefs.  LDView's defaults (low-quality studs, CurveQuality=2) export
# studs and underside tubes as octagons and holes as 16-gons; quality studs +
# primitive substitution at CurveQuality=6 give 48-sided circles.  -Seams=0
# keeps exact dimensions.  -UseStrips=0: with triangle strips on, LDView's STL
# export flips every other triangle of its substituted sloped cylinders
# (4-4cyls, used by stud2s/stud2s2 open-stud tubes and many slopes), leaving
# inverted facets; strips off changes only those triangles' winding.
# Part of the mesh cache key (changing these re-exports).
LDVIEW_GEOMETRY = (
    "-Seams=0",
    "-UseQualityStuds=1",
    "-AllowPrimitiveSubstitution=1",
    "-CurveQuality=6",
    "-HiResPrimitives=0",
    "-UseStrips=0",
)
# Hand-modelled 16-segment geometry in some parts (e.g. bush.dat, Technic
# hole/axle details) does not meet 48-segment substituted primitives, so
# CurveQuality=6 can add cracks and non-manifold edges.  _export() compares
# against LDVIEW_BASELINE (the old default geometry, octagonal studs) and,
# only for a part whose CurveQuality=6 mesh has more non-manifold edges or open
# boundaries, uses LDVIEW_FALLBACK (quality studs at CurveQuality=2: 16-sided
# circles, never octagons) when that is no worse than the baseline.
LDVIEW_BASELINE = (
    "-Seams=0",
    "-UseQualityStuds=0",
    "-AllowPrimitiveSubstitution=1",
    "-CurveQuality=2",
    "-HiResPrimitives=0",
    "-UseStrips=0",
)
LDVIEW_FALLBACK = (
    "-Seams=0",
    "-UseQualityStuds=1",
    "-AllowPrimitiveSubstitution=1",
    "-CurveQuality=2",
    "-HiResPrimitives=0",
    "-UseStrips=0",
)

# Parts that do not stand on their modelled bottom.  Rx/Ry rotate 90 degrees
# around X/Y before packing.  These IDs are shared by many Technic/vehicle sets.
ORIENT = {
    "70490.dat": "Rx",   # Tyre 14/68 x 30
    "15413.dat": "Rx",   # Tyre 20/48 x 30
    "56904.dat": "Rx2",  # Wheel Rim 14 x 30
    "5650.dat": "Rx",    # Wheel Rim 20 x 30
    "85543.dat": "Rx",   # Rubber Belt Round 15/1.6
    "30374.dat": "Rx",   # Bar 4L
    "87994.dat": "Rx",   # Bar 3L
    "88704.dat": "Rx",   # Minifig Whip Bent
    "42003.dat": "Ry2",  # Technic Cross Block 1 x 3
    "32184.dat": "Ry2",  # Technic Cross Block 1 x 3
    "6536.dat": "Ry",    # Technic Cross Block 1 x 2
    "15460.dat": "Ry",   # Technic Steering Arm
    "93609.dat": "Ry",   # Arm Skeleton
    "96874.dat": "Ry",   # Brick Separator
}

ROT = {
    "id": np.eye(3),
    "Rx": np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=float),
    "Rx2": np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=float),
    "Ry": np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]], dtype=float),
    "Ry2": np.array([[0, 0, -1], [0, 1, 0], [1, 0, 0]], dtype=float),
}

# Rebrickable IDs that need an explicit LDraw equivalent.
PART_OVERRIDES = {
    "2752": "3747",
    "30237b": "30237",
    "32005a": "32005",
}


USER_PART_OVERRIDES = Path(os.path.expanduser("~/.config/brickprint/part_overrides.json"))


def load_part_map(path: str | None = None) -> dict:
    """Rebrickable part -> LDraw part, from ~/.config/brickprint/part_overrides.json
    and/or ``--part-map FILE`` (the file wins)."""
    import json
    out = {}
    for p in (USER_PART_OVERRIDES, Path(path) if path else None):
        if p and p.is_file():
            data = json.loads(p.read_text())
            if not isinstance(data, dict):
                raise SystemExit(f"{p}: must be a JSON object of rebrickable part -> LDraw part")
            out.update({str(k).lower().removesuffix(".dat"): str(v).lower().removesuffix(".dat")
                        for k, v in data.items()})
        elif p and path and p == Path(path):
            raise SystemExit(f"--part-map: {p} not found")
    return out


def _strip_print(part: str) -> str:
    return re.sub(r"pr\d+$", "", part.lower())


class LDrawLibrary:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        if not (self.root / "LDConfig.ldr").is_file():
            raise SystemExit(f"not an LDraw library: {self.root}")
        # Single-pass os.scandir over "parts" and "p" (never double-walking
        # parts/s, p/48, p/8) while keeping the exact lookup precedence:
        # parts -> parts/s -> p -> p/48 -> p/8.
        self._files: dict[str, str] = {}
        self._by_base: dict[str, str] = {}

        def scan(base_dir: Path, alias_prefixes: tuple[str, ...]) -> None:
            if not base_dir.is_dir():
                return
            aliases: dict[str, list[tuple[str, str]]] = {p: [] for p in alias_prefixes}
            stack = [(str(base_dir), "")]
            while stack:
                cur, rel_prefix = stack.pop()
                try:
                    with os.scandir(cur) as it:
                        for e in it:
                            name = e.name
                            if e.is_dir(follow_symlinks=False):
                                stack.append((e.path, f"{rel_prefix}{name}/"))
                            elif name.lower().endswith(".dat"):
                                nl = name.lower()
                                rel = f"{rel_prefix.lower()}{nl}"
                                self._files.setdefault(rel, e.path)
                                self._by_base.setdefault(nl, e.path)
                                for ap in alias_prefixes:
                                    if rel.startswith(ap):
                                        aliases[ap].append((rel[len(ap):], e.path))
                except OSError:
                    continue
            for ap in alias_prefixes:
                for sub_rel, p in aliases[ap]:
                    self._files.setdefault(sub_rel, p)

        scan(self.root / "parts", ("s/",))
        scan(self.root / "p", ("48/", "8/"))

    @property
    def files(self) -> dict[str, Path]:
        return {k: Path(v) for k, v in self._files.items()}

    @property
    def by_base(self) -> dict[str, Path]:
        return {k: Path(v) for k, v in self._by_base.items()}

    def find(self, name: str) -> Path | None:
        name = name.lower().strip()
        if not name.endswith(".dat"):
            name += ".dat"
        hit = self._files.get(name)
        if hit is not None:
            return Path(hit)
        base = name.split("/")[-1]
        hit = self._by_base.get(base)
        return Path(hit) if hit is not None else None

    def moved_to(self, path: Path) -> str | None:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for _ in range(12):
                    line = f.readline()
                    if not line:
                        break
                    m = re.match(r"\s*0\s+~Moved to\s+(.+?)\s*$", line, re.I)
                    if m:
                        return m.group(1).strip()
        except OSError:
            return None
        return None

    def resolve(
        self,
        part_num: str,
        relationships: dict[str, list[tuple[str, str]]] | None = None,
        overrides: dict[str, str] | None = None,
    ) -> dict:
        """Return {path, file, status, note} for a Rebrickable part number."""
        original = part_num
        p = _strip_print(part_num)
        note = ""
        if p != part_num.lower():
            note = "printed variant simplified"
        ov = dict(PART_OVERRIDES)
        if overrides:
            ov.update({k.lower(): v.lower() for k, v in overrides.items()})
        if p in ov:
            p = ov[p]
            note = note or "part override"

        seen = set()
        for _ in range(4):
            path = self.find(p)
            if not path:
                break
            moved = self.moved_to(path)
            if moved and moved.lower() not in seen:
                seen.add(moved.lower())
                p = _strip_print(moved)
                continue
            return {
                "path": path,
                "file": path.name,
                "part": p,
                "status": "exact" if not note else "simplified",
                "note": note,
            }

        # Printed/molded variants: try the parent relationship.
        if relationships:
            for rel_type, parent in relationships.get(p, []):
                candidate = _strip_print(parent)
                path = self.find(candidate)
                if path:
                    return {
                        "path": path,
                        "file": path.name,
                        "part": candidate,
                        "status": "parent",
                        "note": note or f"{rel_type} relationship",
                    }

        # Last resort: remove a trailing mold letter (60479a -> 60479).
        if p and p[-1].isalpha():
            candidate = p[:-1]
            path = self.find(candidate)
            if path:
                return {
                    "path": path,
                    "file": path.name,
                    "part": candidate,
                    "status": "mold_variant",
                    "note": note or "mold letter removed",
                }

        return {
            "path": None,
            "file": "",
            "part": p,
            "status": "missing",
            "note": note or f"no LDraw file for {original}",
        }


def _read_ascii_stl(path: Path) -> tuple[np.ndarray, np.ndarray]:
    verts = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split()
            if parts and parts[0].lower() == "vertex":
                verts.append((float(parts[1]) * 10.0, float(parts[2]) * 10.0, float(parts[3]) * 10.0))
    if not verts:
        raise ValueError("LDView produced no vertices")
    tri = np.asarray(verts, dtype=np.float64).reshape(-1, 3)
    V, inverse = np.unique(tri, axis=0, return_inverse=True)
    T = inverse.reshape(-1, 3).astype(np.int32)
    V -= V.min(axis=0)
    return V, T


def _read_binary_stl(path: Path) -> tuple[np.ndarray, np.ndarray]:
    data = path.read_bytes()
    count = struct.unpack("<I", data[80:84])[0]
    rec = np.frombuffer(
        data[84:],
        dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]),
        count=count,
    )
    tri = rec["v"].reshape(-1, 3).astype(np.float64)
    V, inverse = np.unique(tri, axis=0, return_inverse=True)
    T = inverse.reshape(-1, 3).astype(np.int32)
    V -= V.min(axis=0)
    return V, T


def _repair_and_defects(V: np.ndarray, T: np.ndarray) -> tuple[int, int, np.ndarray, np.ndarray]:
    """Repair ``(V, T)`` once and return ``(nonmanifold_edges, open_loops, V_rep, T_rep)``."""
    from . import repair

    Vr, Tr, _ = repair.repair_mesh(V, T, metrics=False)
    if len(Tr) == 0:
        return 0, 0, Vr, Tr
    T64 = Tr.astype(np.int64)
    lo, hi, a, b, _ = repair._edge_table(T64)
    ok = lo != hi
    lo_ok, hi_ok, a_ok, b_ok = lo[ok], hi[ok], a[ok], b[ok]
    key = lo_ok * (int(T64.max()) + 1) + hi_ok
    _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    nonmanifold = int((cnt > 2).sum())
    open_mask = cnt[inv] == 1
    ea, eb = a_ok[open_mask], b_ok[open_mask]
    holes = 0
    if len(ea):
        verts, vinv = np.unique(np.concatenate([ea, eb]), return_inverse=True)
        nv = len(verts)
        parent = list(range(nv))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        ne = len(ea)
        for u, v in zip(vinv[:ne].tolist(), vinv[ne:].tolist()):
            ra, rb = find(u), find(v)
            if ra != rb:
                if ra > rb:
                    ra, rb = rb, ra
                parent[rb] = ra
        holes = len({find(i) for i in range(nv)})
    return nonmanifold, holes, Vr, Tr


def _defects(V: np.ndarray, T: np.ndarray) -> tuple[int, int]:
    """(non-manifold edges, open boundary components) of the repaired mesh."""
    nm, holes, _, _ = _repair_and_defects(V, T)
    return nm, holes


# Last active MeshCache root in this process so ceilings.plan() can cache next
# to .c2.json without changing callers' signatures.
_ACTIVE_CACHE_ROOT: Path | None = None


class MeshCache:
    def __init__(self, cfg: dict):
        global _ACTIVE_CACHE_ROOT
        self.cfg = cfg
        self.ldraw = Path(cfg["ldraw_dir"])
        self.ldview = cfg.get("ldview", "")
        key = " ".join((str(self.ldraw), *LDVIEW_GEOMETRY, "|", *LDVIEW_BASELINE,
                        "|", *LDVIEW_FALLBACK))
        root = Path(cfg["cache_dir"]) / "meshes" / hashlib.sha1(key.encode()).hexdigest()[:12]
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        _ACTIVE_CACHE_ROOT = root

    def __getstate__(self):
        # pickled into prepare() worker processes: only the plain paths are
        # needed (cfg may carry an unpicklable composer)
        state = dict(self.__dict__)
        state["cfg"] = {k: self.cfg[k] for k in ("cache_dir", "ldraw_dir", "ldview")
                        if k in self.cfg}
        state.pop("_last_export", None)
        state.pop("_last_repaired", None)
        return state

    def _path(self, part_file: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", part_file)
        return self.root / (safe + ".npz")

    def get_raw(self, part_file: str) -> tuple[np.ndarray, np.ndarray]:
        """Raw LDView mesh (cached as <part>.npz, never modified)."""
        path = self._path(part_file)
        if path.is_file():
            with np.load(path) as d:
                return d["V"], d["T"]
        V, T = self._export(part_file)
        V32 = np.asarray(V, dtype=np.float32)
        T32 = np.asarray(T, dtype=np.int32)
        _atomic_savez(path, V=V32, T=T32)
        try:
            import json
            _atomic_write_text(path.with_suffix(".q.json"), json.dumps(self._last_export))
        except (OSError, AttributeError):
            pass
        return V32, T32

    def export_info(self, part_file: str) -> dict:
        """Which LDView geometry the cached mesh came from (written when the
        mesh is exported; meshes cached before this existed report
        "unknown")."""
        import json
        p = self._path(part_file).with_suffix(".q.json")
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {"variant": "unknown (cached before export info was recorded)"}

    def get(self, part_file: str, report: dict | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Repaired mesh: repair.py runs on the raw mesh once and is cached as
        <part>.r<REPAIR_VERSION>.npz next to the raw cache (raw stays raw;
        bumping REPAIR_VERSION re-repairs everything)."""
        from . import repair

        rpath = self._path(part_file).with_suffix(f".r{repair.REPAIR_VERSION}.npz")
        if rpath.is_file() and report is None:
            with np.load(rpath) as d:
                return d["V"], d["T"]
        self._last_repaired = None
        V, T = self.get_raw(part_file)
        reused = getattr(self, "_last_repaired", None)
        self._last_repaired = None
        if report is None and reused is not None and reused[0] == part_file:
            Vr, Tr = reused[1], reused[2]
        else:
            Vr, Tr, rep = repair.repair_mesh(V, T, part_file, metrics=(report is not None))
            if report is not None:
                report[part_file] = rep
        _atomic_savez(rpath, V=Vr.astype(np.float32), T=Tr.astype(np.int32))
        return Vr, Tr

    def _load_candidates(self, cpath: Path, tag: str) -> dict | None:
        import json

        if cpath.is_file():
            try:
                hit = json.loads(cpath.read_text())
                if hit.get("tag") == tag:
                    return {up: (np.array(c["R"], dtype=float), c["m"])
                            for up, c in hit["cands"].items()}
            except (ValueError, KeyError, OSError):
                pass
        return None

    def candidates(self, part_file: str, V: np.ndarray, T: np.ndarray, rot0: str) -> dict:
        """orient.candidates() (the slow emulator pass over the 6 up
        directions), cached as <part>.c<ORIENT_VERSION>.json keyed by the
        repaired mesh hash and the start rotation.  {up: (R, metrics)}."""
        import json

        from . import orient

        tag = f"{mesh_key(V, T)}:{rot0}"
        cpath = self._path(part_file).with_suffix(f".c{orient.ORIENT_VERSION}.json")
        hit = self._load_candidates(cpath, tag)
        if hit is not None:
            return hit
        cands = orient.candidates(V, T, ROT[rot0])
        slim = {up: {"R": R.astype(int).tolist(),
                     "m": {k: (bool(m[k]) if k == "on_edge" else float(m[k]))
                           for k in CAND_KEYS}}
                for up, (R, m) in cands.items()}
        _atomic_write_text(cpath, json.dumps({"tag": tag, "cands": slim}))
        return {up: (np.array(c["R"], dtype=float), c["m"]) for up, c in slim.items()}

    def cached_candidates(self, part_file: str, V: np.ndarray, T: np.ndarray,
                          rot0: str) -> dict | None:
        """candidates() from the cache only: None on a miss (never computes)."""
        from . import orient

        tag = f"{mesh_key(V, T)}:{rot0}"
        cpath = self._path(part_file).with_suffix(f".c{orient.ORIENT_VERSION}.json")
        return self._load_candidates(cpath, tag)

    def has_mesh(self, part_file: str) -> bool:
        """The repaired mesh is cached (get() will not run LDView / repair)."""
        from . import repair

        return self._path(part_file).with_suffix(f".r{repair.REPAIR_VERSION}.npz").is_file()

    def prepare(self, items, workers: int = 1, log=print) -> dict:
        """{part_file: (V, T, cands)} for [(part_file, rot0), ...], in input
        order: get() + candidates() per part, exactly as a serial loop.

        Cached parts are loaded here; only the parts missing a repaired mesh
        or candidate entry go to a process pool of ``workers`` (the LDView
        export, repair and emulator passes are independent per part).  With
        workers <= 1 or fewer than two misses this is the plain serial loop.
        Progress lines (every 50th part and the last) are printed in order.
        A failing part re-raises its own exception (the first failing part
        in input order, as the serial loop would)."""
        items = list(items)
        n = len(items)
        done: dict = {}
        out = {}

        def drain(upto: int) -> None:
            while len(out) < upto:
                i = len(out)
                out[items[i][0]] = done.pop(i)
                if (i + 1) % 50 == 0 or i + 1 == n:
                    log(f"  [{i + 1:4d}/{n}] {items[i][0]}")

        missing = []
        if int(workers or 1) > 1:
            # cached parts load here; the rest may go to the pool
            for i, (part_file, rot0) in enumerate(items):
                if self.has_mesh(part_file):
                    V, T = self.get(part_file)
                    cands = self.cached_candidates(part_file, V, T, rot0)
                    if cands is not None:
                        done[i] = (V, T, cands)
                        continue
                missing.append(i)
        workers = min(int(workers or 1), len(missing))

        if workers <= 1:
            for i, (part_file, rot0) in enumerate(items):
                if i not in done:
                    V, T = self.get(part_file)
                    done[i] = (V, T, self.candidates(part_file, V, T, rot0))
                drain(i + 1)
            return out

        for i, res in _run_pool(self, [(i, *items[i]) for i in missing], workers):
            done[i] = res
            ready = len(out)
            while ready in done:
                ready += 1
            drain(ready)
        drain(n)
        return out

    def orientation(self, part_file: str, V: np.ndarray, T: np.ndarray,
                    overrides: dict | None = None, verdicts: dict | None = None,
                    cands: dict | None = None) -> dict:
        """Print orientation from orient.choose() over the cached candidates.

        ``verdicts`` ({up: Bambu verdict}, from probe.py) decide floating /
        empty-layer status instead of the emulator when given."""
        from . import orient

        rot0, hard = start_rotation(part_file, overrides)
        if cands is None:
            cands = self.candidates(part_file, V, T, rot0)
        ch = orient.choose(V, T, ROT[rot0], hard_override=hard,
                           verdicts=verdicts, cand=cands)
        if forced_rotation(part_file, overrides) and ch["changed"]:
            # the user insists on this pose (e.g. studs up rather than studs
            # down on supports): keep it and print it with supports
            ch = dict(ch, R=np.asarray(ROT[rot0], dtype=float), changed=False,
                      needs_support=True, after=ch["before"],
                      reason="forced pose (floats: support plate)")
        keys = ("float_n", "float_area", "contact_ratio", "first_layer_ratio",
                "down_area", "height", "on_edge", "empty_layers")
        hit = {
            "verdict_used": verdicts is not None,
            "R": ch["R"].astype(int).tolist(),
            "old_rot": rot0,
            "rot": orient.rot_label(ch["R"], ROT),
            "changed": bool(ch["changed"]),
            "needs_support": bool(ch["needs_support"]),
            "reason": ch["reason"],
            "before": {k: ch["before"][k] for k in keys},
            "after": {k: ch["after"][k] for k in keys},
        }
        return hit

    def _export(self, part_file: str) -> tuple[np.ndarray, np.ndarray]:
        """LDVIEW_GEOMETRY mesh, or LDVIEW_FALLBACK for the few parts where
        CurveQuality=6 is topologically worse than LDVIEW_BASELINE."""
        V, T = self._ldview(part_file, LDVIEW_GEOMETRY)
        V = V.astype(np.float32)
        T = T.astype(np.int32)
        nm, holes, Vr, Tr = _repair_and_defects(V, T)
        d = (nm, holes)
        info = {"variant": "quality (CurveQuality=6, 48-gon)",
                "defects": {"quality": {"nonmanifold": int(d[0]), "open_loops": int(d[1])}}}
        self._last_export = info
        self._last_repaired = (part_file, Vr, Tr)
        if d == (0, 0):
            return V, T
        Vb, Tb = self._ldview(part_file, LDVIEW_BASELINE)
        base = _defects(Vb.astype(np.float32), Tb.astype(np.int32))
        info["defects"]["baseline"] = {"nonmanifold": int(base[0]), "open_loops": int(base[1])}
        if d[0] <= base[0] and d[1] <= base[1]:
            return V, T
        Vf, Tf = self._ldview(part_file, LDVIEW_FALLBACK)
        Vf = Vf.astype(np.float32)
        Tf = Tf.astype(np.int32)
        fnm, fholes, Vfr, Tfr = _repair_and_defects(Vf, Tf)
        f = (fnm, fholes)
        info["defects"]["fallback"] = {"nonmanifold": int(f[0]), "open_loops": int(f[1])}
        if f[0] <= base[0] and f[1] <= base[1]:
            info["variant"] = "fallback (CurveQuality=2, 16-gon)"
            self._last_repaired = (part_file, Vfr, Tfr)
            return Vf, Tf
        info["variant"] = "quality (CurveQuality=6, 48-gon; all variants defective)"
        return V, T

    def _ldview(self, part_file: str, geometry: tuple) -> tuple[np.ndarray, np.ndarray]:
        if not self.ldview or not Path(self.ldview).is_file():
            raise SystemExit(
                "LDView not found. Set BRICKPRINT_LDVIEW or install LDView, "
                "then re-run brickprint doctor."
            )
        with tempfile.TemporaryDirectory(prefix="brickprint_") as td:
            ldr = Path(td) / "one.ldr"
            ldr.write_text(f"0 brickprint mesh\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 {part_file}\n")
            cmd = [
                self.ldview,
                str(ldr),
                f"-LDrawDir={self.ldraw}",
                "-ExportFiles=1",
                "-ExportSuffix=.stl",
                *geometry,
            ]
            proc = subprocess.run(cmd, cwd=td, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stl = Path(td) / "one.stl"
            if proc.returncode != 0 or not stl.is_file():
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                msg = detail[-1] if detail else f"LDView exit {proc.returncode}"
                raise RuntimeError(f"LDView failed for {part_file}: {msg}")
            with open(stl, "rb") as f:
                head = f.read(84)
            is_bin = (len(head) == 84
                      and not head[:5].lower().startswith(b"solid")
                      and 84 + 50 * struct.unpack("<I", head[80:84])[0] == stl.stat().st_size)
            if is_bin:
                return _read_binary_stl(stl)
            try:
                return _read_ascii_stl(stl)
            except ValueError:
                return _read_binary_stl(stl)


def _atomic_write(path: Path, write) -> None:
    """write(fileobj) into a temp file next to ``path``, then rename over it:
    an interrupted or concurrent writer never leaves a half-written cache
    file (a killed process can leave a ``.<name>.<pid>.tmp`` behind, which
    no reader looks at)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            write(f)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_savez(path: Path, **arrays) -> None:
    _atomic_write(path, lambda f: np.savez_compressed(f, **arrays))


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write(path, lambda f: f.write(text.encode("utf-8")))


def default_mesh_workers(cfg: dict) -> int:
    """Processes for MeshCache.prepare(): config ``mesh_workers``, else
    cpus - 2 capped at 8 (at least 1), which leaves two cores for the rest of
    the machine.  No Bambu Studio runs during this stage."""
    val = cfg.get("mesh_workers")
    if val not in (None, ""):
        return max(1, int(val))
    return max(1, min(8, (os.cpu_count() or 1) - 2))


class _Terminated(SystemExit):
    """Raised in a pool worker by SIGTERM (the parent stopping the pool)."""


def _worker_init() -> None:
    import signal

    # Ctrl-C reaches the whole process group; the parent alone handles it
    # and stops the workers with SIGTERM, which unwinds the running part
    # (subprocess.run kills its LDView, temp dirs and temp cache files are
    # removed) before the worker exits.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    def term(signum, frame):
        raise _Terminated(128 + signum)

    signal.signal(signal.SIGTERM, term)


def _prepare_one(cache: "MeshCache", part_file: str, rot0: str):
    try:
        V, T = cache.get(part_file)
        return V, T, cache.candidates(part_file, V, T, rot0)
    except _Terminated:
        os._exit(143)


def _run_pool(cache: "MeshCache", jobs, workers: int):
    """Yield (index, (V, T, cands)) for [(index, part_file, rot0)] as the
    pool finishes them.  On a worker error the remaining queued parts are
    cancelled, running ones finish, and the error of the first failing part
    (by index) is raised as itself (no pool traceback).  Any interruption
    terminates the workers."""
    import concurrent.futures as cf
    import multiprocessing as mp

    ex = cf.ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn"),
                                initializer=_worker_init)
    try:
        futs = {ex.submit(_prepare_one, cache, part_file, rot0): i
                for i, part_file, rot0 in jobs}
        errors = {}
        for f in cf.as_completed(futs):
            if f.cancelled():
                continue
            exc = f.exception()
            if exc is not None:
                errors[futs[f]] = exc
                for g in futs:
                    g.cancel()
            elif not errors:
                yield futs[f], f.result()
        if errors:
            exc = errors[min(errors)]
            exc.__cause__ = None
            exc.__suppress_context__ = True
            raise exc
    except BaseException:
        _stop_pool(ex)
        raise
    finally:
        ex.shutdown(wait=True, cancel_futures=True)


def _stop_pool(ex) -> None:
    procs = list((getattr(ex, "_processes", None) or {}).values())
    for p in procs:
        try:
            p.terminate()
        except (OSError, ValueError, AttributeError):
            pass
    for p in procs:
        try:
            p.join(5)
            if p.is_alive():
                p.kill()
                p.join(1)
        except (OSError, ValueError, AttributeError):
            pass


def rot_label(label, part_file: str = "") -> str:
    """Canonical rotation label (case-insensitive: "rx" -> "Rx").  Unknown
    labels are an error, never a silent identity."""
    canon = {k.lower(): k for k in ROT}
    got = canon.get(str(label).strip().rstrip("!").lower())
    if got is None:
        where = f" for {part_file}" if part_file else ""
        raise SystemExit(f"--orient: unknown rotation {label!r}{where}; "
                         f"use one of {', '.join(ROT)}")
    return got


def start_rotation(part_file: str, overrides: dict | None = None) -> tuple[str, bool]:
    """(rotation label, hard override?) before orientation selection."""
    key = part_file.lower()
    ov = overrides or {}
    rot0 = rot_label(ov.get(key, ORIENT.get(key, "id")), part_file)
    return rot0, key in ov or key in ORIENT


def forced_rotation(part_file: str, overrides: dict | None = None) -> bool:
    """--orient label with a trailing "!" (e.g. "Rx2!"): keep exactly this
    pose even if it floats; the part then goes to the support plates."""
    return str((overrides or {}).get(part_file.lower(), "")).strip().endswith("!")


def mesh_key(V: np.ndarray, T: np.ndarray) -> str:
    """Repaired-mesh hash shared by the orient candidate and probe caches
    (same as tools/countach_to_3mf.py's _mesh_key)."""
    h = hashlib.sha1(np.ascontiguousarray(V, dtype=np.float32).tobytes())
    h.update(np.ascontiguousarray(T, dtype=np.int32).tobytes())
    return h.hexdigest()[:16]


CAND_KEYS = ("float_n", "float_area", "float_zmin", "contact_area", "contact_ratio",
             "first_layer_area", "first_layer_ratio", "down_area", "height", "on_edge",
             "empty_layers", "hull_area")


def oriented(V: np.ndarray, part_file: str, overrides: dict | None = None) -> tuple[np.ndarray, str]:
    rot, _ = start_rotation(part_file, overrides)
    W = V.astype(np.float64)
    if rot != "id":
        W = W @ ROT[rot].T
    W -= W.min(axis=0)
    return W.astype(np.float32), rot


def apply_rotation(V: np.ndarray, R) -> np.ndarray:
    """Rotate by a 3x3 matrix (V @ R.T) and min-shift to the origin."""
    W = V.astype(np.float64) @ np.asarray(R, dtype=float).T
    W -= W.min(axis=0)
    return W.astype(np.float32)


def read_bom(path: Path) -> list[dict]:
    import csv

    lots = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            part = (r.get("part_num") or "").strip()
            if not part:
                continue
            color_id = str(r.get("color_id") or r.get("ldraw_color") or "0").strip()
            qty = int(float(r.get("quantity") or 0))
            if qty <= 0:
                continue
            color_name = (r.get("color_name") or "").strip()
            rgb = (r.get("rgb") or "").strip().lstrip("#")
            lots.append({
                "part_num": part,
                "color_id": color_id,
                "quantity": qty,
                "part_name": (r.get("part_name") or "").strip(),
                "color_name": color_name or f"Color {color_id}",
                "rgb": rgb or "CCCCCC",
                "is_trans": str(r.get("is_trans", "")).lower() in ("1", "true", "yes"),
            })
    if not lots:
        raise SystemExit(f"no parts found in {path}")
    return lots
