"""Print-tuning profiles -> Bambu project settings (Metadata/project_settings.config).

Rules learned the hard way on the Seagull (see references/bambu-3mf.md):

* every value is a string or a list of strings with the length the key
  already has; JSON numbers are rejected by Bambu ("invalid json type") and
  silently replaced by stock values;
* every key that differs from the named system preset is listed in
  ``different_settings_to_system`` ([process, filament_1..N, printer]),
  computed here, never hand-written; the GUI resets unlisted keys;
* ``filament_shrink`` above 100 % scales the model DOWN; rejected unless
  explicitly allowed.

Profiles are JSON files in ``~/.config/brickprint/profiles/<kind>/`` (user,
wins) or ``<skill>/assets/profiles/<kind>/``; ``kind`` is ``process`` or
``filament``.  The default process profile ``a1mini-pla-stock`` with no other
option reproduces the historic output byte for byte (the snapshot template).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path

from . import bambu_profiles as bp

SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSET_PROFILES = SKILL_ROOT / "assets" / "profiles"
USER_PROFILES = Path(os.path.expanduser("~/.config/brickprint/profiles"))
DEFAULT_PROFILE = "a1mini-pla-stock"

# per-slot keys that identify the preset, not tuning; never listed as diffs
IDENTITY = {"filament_colour", "filament_settings_id", "filament_ids",
            "filament_self_index", "default_filament_colour"}
# settings that change Bambu's floating / empty-layer verdicts (probe cache key)
FINGERPRINT_KEYS = ("layer_height", "initial_layer_print_height", "line_width",
                    "outer_wall_line_width", "inner_wall_line_width",
                    "initial_layer_line_width", "xy_contour_compensation",
                    "xy_hole_compensation", "slice_closing_radius",
                    "elefant_foot_compensation", "wall_loops", "enable_support")


class ProfileError(SystemExit):
    pass


# ---------------------------------------------------------------------------
# profile files
# ---------------------------------------------------------------------------

def profile_dirs(kind: str) -> list[Path]:
    return [USER_PROFILES / kind, ASSET_PROFILES / kind]


def list_profiles() -> dict[str, list[tuple[str, Path]]]:
    out = {}
    for kind in ("process", "filament"):
        seen = {}
        for d in profile_dirs(kind):
            for p in sorted(d.glob("*.json")) if d.is_dir() else []:
                seen.setdefault(p.stem, p)
        out[kind] = sorted(seen.items())
    return out


def load_profile(kind: str, name: str) -> dict:
    for d in profile_dirs(kind):
        p = d / f"{name}.json"
        if p.is_file():
            data = json.loads(p.read_text())
            if data.get("kind", kind) != kind:
                raise ProfileError(f"{p}: kind is {data.get('kind')!r}, expected {kind!r}")
            data["_path"] = str(p)
            return data
    avail = ", ".join(n for n, _ in list_profiles()[kind]) or "none"
    raise ProfileError(f"no {kind} profile {name!r} (available: {avail})")


# ---------------------------------------------------------------------------
# value shaping
# ---------------------------------------------------------------------------

def fmt(v) -> str:
    """Bambu string form of a scalar (True -> "1", 0.030 -> "0.03")."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        s = ("%.6f" % v).rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    return str(v)


def norm(v):
    """Comparable form: numbers compared numerically ("100" == "100%"? no:
    the percent sign is kept), lists element-wise."""
    if isinstance(v, list):
        return [norm(x) for x in v]
    s = str(v)
    try:
        return repr(float(s.rstrip("%"))) + ("%" if s.endswith("%") else "")
    except ValueError:
        return s


def shape(key: str, old, value):
    """``value`` in the JSON shape ``old`` already has."""
    if isinstance(value, (list, tuple)):
        if not (isinstance(old, list) and len(old) == len(value)):
            raise ProfileError(f"{key}: list value {value!r} does not match {old!r}")
        return [fmt(x) for x in value]
    s = fmt(value)
    if isinstance(old, str):
        return s
    if isinstance(old, list) and all(isinstance(x, str) for x in old):
        return [s] * max(len(old), 1)
    raise ProfileError(f"{key}: unsupported type/shape in the base settings: {old!r}")


def parse_set(items: list[str] | None) -> dict:
    out = {}
    for kv in items or []:
        if "=" not in kv:
            raise ProfileError(f"--set {kv!r}: expected key=value")
        k, v = kv.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def parse_support_set(profile_values: dict | None, items: list[str] | None) -> dict:
    """Extra per-object settings for objects with their own tree supports:
    profile ``supports.object_settings``, overridden per key by --support-set.
    The base keys that turn those supports on cannot be changed here."""
    from .threemf import AUTO_SUPPORT_META
    out = {str(k).strip(): fmt(v) for k, v in (profile_values or {}).items()}
    for kv in items or []:
        if "=" not in kv or not kv.split("=", 1)[0].strip():
            raise ProfileError(f"--support-set {kv!r}: expected key=value")
        k, v = kv.split("=", 1)
        out[k.strip()] = v.strip()
    fixed = sorted(k for k in out if k in AUTO_SUPPORT_META)
    if fixed:
        raise ProfileError(f"--support-set / supports.object_settings: {', '.join(fixed)} cannot be "
                           f"overridden (fixed: {', '.join(f'{k}={v}' for k, v in AUTO_SUPPORT_META.items())})")
    return out


MAX_FIT_MM = 0.3


def check_fit_value(label: str, v: float, what: str = "contour") -> float:
    """One compensation value in mm: finite, contour >= 0, |v| <= 0.3."""
    if v != v or abs(v) > MAX_FIT_MM:
        raise ProfileError(f"{label}: {what} compensation {v:g} mm is out of range "
                           f"(allowed |value| <= {MAX_FIT_MM:g} mm)")
    if what == "contour" and v < 0:
        raise ProfileError(f"{label}: contour compensation {v:g} mm is negative "
                           f"(expected 0 to {MAX_FIT_MM:g} mm; holes use the negative side)")
    return v


def parse_fit(text) -> tuple[float, float] | None:
    if text in (None, ""):
        return None
    try:
        parts = [float(x) for x in str(text).split(",")]
    except ValueError:
        raise ProfileError(f"--fit {text!r}: expected C or C,H in mm (e.g. 0.03 or 0.03,-0.02)")
    if len(parts) > 2:
        raise ProfileError(f"--fit {text!r}: expected C or C,H in mm (e.g. 0.03 or 0.03,-0.02)")
    c = check_fit_value(f"--fit {text}", parts[0], "contour")
    h = check_fit_value(f"--fit {text}", parts[1], "hole") if len(parts) > 1 else -c
    return c, h


_INT_RE = re.compile(r"^[+-]?\d+$")
_NUM_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
# keys whose Bambu type is an integer even when the stock value looks like a
# plain number; everything else numeric is float-or-int
_INT_KEY_RE = re.compile(r"(_loops|_layers|_count|_num|_copies)$|^(wall_loops|skirt_loops|"
                         r"raft_layers|slowdown_for_layers|top_shell_layers|bottom_shell_layers)$")


def check_set_value(key: str, old, value) -> None:
    """Reject a --set value whose kind clearly differs from the stock value's.
    Kind is inferred from the resolved stock/template value; when it cannot be
    determined (free strings, enums, gcode, empty) anything is accepted."""
    if isinstance(old, list):
        if not old or isinstance(value, (list, tuple)):
            return
        old = old[0]
    if not isinstance(old, str) or isinstance(value, (list, tuple)):
        return
    v = fmt(value).strip()
    o = old.strip()
    pct = o.endswith("%") and _NUM_RE.match(o[:-1]) is not None
    if pct:
        if not _NUM_RE.match(v.rstrip("%")) or v.count("%") > 1:
            raise ProfileError(f"--set {key}={v}: expected a number or percentage (stock {o})")
        return
    if _INT_RE.match(o) and _INT_KEY_RE.search(key):
        if not _INT_RE.match(v):
            raise ProfileError(f"--set {key}={v}: expected a whole number (stock {o})")
        return
    if _NUM_RE.match(o):
        bare = v[:-1] if v.endswith("%") and "width" in key else v
        if not _NUM_RE.match(bare):
            raise ProfileError(f"--set {key}={v}: expected a number (stock {o})")


# ---------------------------------------------------------------------------
# composer
# ---------------------------------------------------------------------------

class Composer:
    """Project settings for one build: process profile + per-colour filament
    profiles + --fit + --set.  ``compose(slots)`` is called by the 3MF writer
    for every project it writes (build, probe, plate check)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg.get("profile") or DEFAULT_PROFILE
        self.profile = load_profile("process", self.name)
        self.fit = parse_fit(cfg.get("fit"))
        self.sets = parse_set(cfg.get("set"))
        self.allow_shrink = bool(cfg.get("allow_shrink"))
        self.filament_specs = list(cfg.get("filaments") or [])
        self.snapshot = self.profile.get("base") == "snapshot"
        prof_fit = self.profile.get("fit") or {}
        if self.fit is None:
            self.fit = (float(prof_fit.get("contour", 0)), float(prof_fit.get("hole", 0)))
        # the untouched historic path: stock profile, no option at all
        self.supports_mode = cfg.get("supports") or (self.profile.get("supports") or {}).get("mode", "plates")
        self.bridging_mode = cfg.get("bridging") or (self.profile.get("bridging") or {}).get("mode", "stock")
        self.ceilings_mode = cfg.get("ceilings") or (self.profile.get("ceilings") or {}).get("mode", "off")
        self.support_set = parse_support_set((self.profile.get("supports") or {}).get("object_settings"),
                                             cfg.get("support_set"))
        self.legacy = (self.snapshot and not self.profile.get("overrides")
                       and self.ceilings_mode == "off"
                       and self.fit == (0.0, 0.0) and not self.sets and not self.support_set
                       and not self.filament_specs
                       and self.supports_mode == "plates" and self.bridging_mode == "stock")
        self._fil_cache: dict[str, dict] = {}
        self._base_cache: dict | None = None
        self._legacy_base_cache: dict | None = None
        self._proc_chain_cache: tuple[str, dict] | None = None
        self._mach_chain_cache: tuple[str, dict] | None = None
        self._raw_fil_cache: dict[str, tuple[dict, dict]] = {}
        self._fingerprint_cache: str | None = None
        self.warnings: list[str] = []
        self._used_specs: set[str] = set()

    # -- helpers ------------------------------------------------------------
    @property
    def app(self) -> Path:
        return bp.app_from_exe(self.cfg.get("bambu"))

    def process_overrides(self) -> dict:
        ov = dict(self.profile.get("overrides") or {})
        c, h = self.fit
        if (c, h) != (0.0, 0.0) or not self.snapshot:
            ov["xy_contour_compensation"] = c
            ov["xy_hole_compensation"] = h
        if self.supports_mode == "enforcers":
            ov.setdefault("enable_support", 1)
            ov.setdefault("support_type", "normal(manual)")
        elif self.supports_mode == "plates" and not self.snapshot:
            ov["enable_support"] = 0
        ov.update(self.sets)
        return ov

    def filament_for(self, slot: dict, keep_slot_colour: bool = False) -> dict | None:
        bare = False
        keys = {str(slot.get("color_id", "")).lower(), str(slot.get("color_name", "")).lower(),
                str(slot.get("hex", "")).lower().lstrip("#")}
        chosen = None
        for spec in self.filament_specs:
            if "=" in spec:
                k, v = spec.split("=", 1)
                if k.strip().lower().lstrip("#") in keys:
                    chosen = v.strip()
                    self._used_specs.add(spec)
            elif chosen is None:
                chosen = spec.strip()
                bare = True
                self._used_specs.add(spec)
        if not chosen:
            return None
        if chosen not in self._fil_cache:
            self._fil_cache[chosen] = load_profile("filament", chosen)
        fp = self._fil_cache[chosen]
        if bare and keep_slot_colour and fp.get("colour"):
            # a catch-all PROFILE supplies the material settings only; each
            # slot keeps its own LEGO colour (else every slot shows the
            # profile's colour and mixed plates are unreadable)
            fp = {k: v for k, v in fp.items() if k != "colour"}
        return fp

    def unused_specs(self) -> list[str]:
        return [s for s in self.filament_specs if "=" in s and s not in self._used_specs]

    def _base(self) -> dict:
        if self._base_cache is None:
            if self.snapshot:
                t = json.loads(Path(self.cfg["settings"]).read_text())
                t.update({"curr_bed_type": self.cfg.get("bed_type", "Textured PEI Plate")})
                self._base_cache = t
            else:
                p = self.profile
                self._base_cache = bp.build_template(
                    self.app, p.get("printer", bp.MACHINE), p["base_process"],
                    p.get("default_filament", bp.FILAMENT),
                    overrides={"curr_bed_type": p.get("bed_type", "Textured PEI Plate")},
                )
        return copy.deepcopy(self._base_cache)

    def _raw_filament(self, base_name: str) -> tuple[dict, dict]:
        if base_name not in self._raw_fil_cache:
            raw = bp.raw_chain(self.app, "filament", base_name)
            fch = {k: v for k, v in raw.items() if k not in bp.META}
            self._raw_fil_cache[base_name] = (raw, fch)
        return self._raw_fil_cache[base_name]

    # -- main ---------------------------------------------------------------
    def compose(self, slots: list[dict], verdict: bool = False) -> dict:
        """slots: [{color_id, color_name, hex}] in extruder order."""
        from . import threemf
        hexes = ["#" + s["hex"].lstrip("#") for s in slots]
        if self.legacy:
            if self._legacy_base_cache is None:
                self._legacy_base_cache = json.loads(Path(self.cfg["settings"]).read_text())
            settings = copy.deepcopy(self._legacy_base_cache)
            settings.update({
                "filament_type": [self.cfg.get("filament_type", "PLA")],
                "filament_settings_id": [self.cfg.get("filament_settings_id", "Bambu PLA Basic @BBL A1M")],
                "filament_ids": [self.cfg.get("filament_id", "GFA00")],
                "curr_bed_type": self.cfg.get("bed_type", "Textured PEI Plate"),
            })
            settings = threemf.expand_per_filament(settings, hexes)
            if verdict and settings.get("enable_support") != "0":
                settings["enable_support"] = "0"   # never true for the stock snapshot
            return settings

        T = self._base()
        per = T.pop("_brickprint_per_filament_keys")
        base_proc = T.get("print_settings_id") or bp.PROCESS
        base_mach = T.get("printer_settings_id") or bp.MACHINE
        default_fil = (T.get("filament_settings_id") or [bp.FILAMENT])[0]
        if self.snapshot:
            default_fil = self.cfg.get("filament_settings_id", default_fil)
        if self._proc_chain_cache is None or self._proc_chain_cache[0] != base_proc:
            self._proc_chain_cache = (base_proc, bp.chain(self.app, "process", base_proc))
        if self._mach_chain_cache is None or self._mach_chain_cache[0] != base_mach:
            self._mach_chain_cache = (base_mach, bp.chain(self.app, "machine", base_mach))
        proc_chain = self._proc_chain_cache[1]
        mach_chain = self._mach_chain_cache[1]
        n = len(slots)
        S = copy.deepcopy(T)
        values = {k: [] for k in per}
        fdiffs = []
        slot_fps = []
        for i, slot in enumerate(slots):
            fp = self.filament_for(slot, keep_slot_colour=n > 1)
            slot_fps.append(fp)
            base_name = (fp or {}).get("base_filament") or default_fil
            raw, fch = self._raw_filament(base_name)
            vals = {}
            for k in per:
                src = fch.get(k, T[k])
                vals[k] = src[0] if isinstance(src, list) and src else (src if isinstance(src, str) else T[k][0])
            base_vals = dict(vals)
            for k, v in ((fp or {}).get("overrides") or {}).items():
                if k not in per:
                    raise ProfileError(f"filament profile {fp['name']}: {k!r} is not a per-filament key")
                vals[k] = fmt(v)
            vals["filament_colour"] = (fp or {}).get("colour") or hexes[i]
            vals["filament_settings_id"] = base_name
            vals["filament_ids"] = (fp or {}).get("filament_id") or raw.get("filament_id", "")
            for k in per:
                values[k].append(vals[k])
            fdiffs.append(";".join(sorted(k for k in per if k not in IDENTITY
                                          and norm(vals[k]) != norm(base_vals[k]))))
        for k in per:
            S[k] = values[k]
        S["filament_self_index"] = [str(i + 1) for i in range(n)]
        S["flush_volumes_matrix"] = [str(0 if i == j else 280) for i in range(n) for j in range(n)]
        S["flush_volumes_vector"] = ["140"] * (2 * n)

        pdiff, mdiff = set(), set()
        for k, v in self.process_overrides().items():
            if k not in S:
                raise ProfileError(f"setting {k!r} does not exist in the {base_proc!r} chain")
            if k in per:
                raise ProfileError(f"{k!r} is a per-filament key; set it in a filament profile")
            if k in self.sets:
                check_set_value(k, T[k], v)
            S[k] = shape(k, T[k], v)
            ref = proc_chain.get(k, T[k])
            a = S[k][0] if isinstance(S[k], list) and S[k] else S[k]
            b = ref[0] if isinstance(ref, list) and ref else ref
            if k in mach_chain and k not in proc_chain:
                b = mach_chain[k][0] if isinstance(mach_chain[k], list) and mach_chain[k] else mach_chain[k]
                if norm(a) != norm(b):
                    mdiff.add(k)
            elif norm(a) != norm(b):
                pdiff.add(k)
        S["print_settings_id"] = base_proc
        S["different_settings_to_system"] = [";".join(sorted(pdiff)), *fdiffs, ";".join(sorted(mdiff))]
        S["inherits_group"] = [""] * (n + 2)
        if verdict:
            S["enable_support"] = "0"
        self.validate(S, slots, fps=slot_fps)
        return S

    def validate(self, S: dict, slots: list[dict], fps: list[dict | None] | None = None) -> None:
        bad = [k for k, v in S.items()
               if not (isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v)))]
        if bad:
            raise ProfileError(f"non-string project settings (Bambu would drop them): {bad[:10]}")
        for i, s in enumerate(S.get("filament_shrink", [])):
            if float(s.rstrip("%")) > 100.0 and not self.allow_shrink:
                raise ProfileError(
                    f"filament slot {i + 1}: filament_shrink {s} > 100% scales the parts DOWN "
                    "in Bambu Studio (measured); use 100% or pass --allow-shrink")
        for i in range(len(slots)):
            t = float(S["nozzle_temperature"][i])
            lo = float(S["nozzle_temperature_range_low"][i])
            hi = float(S["nozzle_temperature_range_high"][i])
            if not lo <= t <= hi:
                raise ProfileError(f"filament slot {i + 1}: nozzle {t:g} C outside its preset range "
                                   f"{lo:g}-{hi:g} C")
            fp = fps[i] if fps is not None else self.filament_for(slots[i])
            mf = (fp or {}).get("manufacturer") or {}
            rng = mf.get("nozzle_temperature_c")
            if rng and not rng[0] <= t <= rng[1]:
                raise ProfileError(f"{fp['name']}: nozzle {t:g} C outside the manufacturer range {rng}")
            brng = mf.get("bed_temperature_c")
            bed = float(S["textured_plate_temp"][i])
            if brng and not brng[0] <= bed <= brng[1]:
                raise ProfileError(f"{fp['name']}: bed {bed:g} C outside the manufacturer range {brng}")

    # -- probe support ------------------------------------------------------
    def fingerprint(self) -> str:
        """'' for the historic default (keeps the probe cache keys); else a
        short hash of the verdict-relevant settings of a one-slot probe."""
        if self.legacy:
            return ""
        if self._fingerprint_cache is None:
            S = self.compose([{"color_id": "probe", "color_name": "probe", "hex": "FFFFFF"}], verdict=True)
            blob = json.dumps({k: S.get(k) for k in FINGERPRINT_KEYS}, sort_keys=True)
            self._fingerprint_cache = hashlib.sha1(blob.encode()).hexdigest()[:10]
        return self._fingerprint_cache

    def summary(self) -> dict:
        return {
            "profile": self.name, "profile_path": self.profile.get("_path"),
            "legacy_default": self.legacy, "fit": {"contour": self.fit[0], "hole": self.fit[1]},
            "supports": self.supports_mode, "bridging": self.bridging_mode,
            "ceilings": self.ceilings_mode,
            "set": self.sets, "support_set": self.support_set, "filaments": self.filament_specs,
            "bambu_version": None if self.legacy else bp.app_version(self.app),
        }
