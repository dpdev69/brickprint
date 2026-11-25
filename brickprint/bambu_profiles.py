"""Bambu Studio stock preset resolution (moved here from tools/build_settings_template.py).

Resolves a machine / process / filament preset chain from the Bambu Studio app
bundle exactly like Bambu does: follow "inherits" (parent first), merge the
"include" files, then the file's own keys.  ``build_template`` produces the
one-slot project-settings dict that the 3MF writer embeds; with the default
names it reproduces ``assets/a1mini_pla_project_settings.json`` byte for byte
(``brickprint profiles regen-template`` rewrites that snapshot).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import plistlib
import threading
from functools import lru_cache
from pathlib import Path

DEFAULT_APP = Path("/Applications/BambuStudio.app")
MACHINE = "Bambu Lab A1 mini 0.4 nozzle"
FILAMENT = "Bambu PLA Basic @BBL A1M"
PROCESS = "0.20mm Standard @BBL A1M"

# Profile bookkeeping keys that never appear in a project config.
META = {
    "type", "name", "inherits", "from", "setting_id", "instantiation",
    "include", "description", "compatible_printers",
    "compatible_printers_condition", "filament_id", "renamed_from", "base_id",
}

# Project-level filament-slot keys that no profile sets (C++ defaults).
EXTRA_PER_FILAMENT = {
    "default_filament_colour", "enable_pressure_advance", "pressure_advance",
    "enable_overhang_bridge_fan", "ironing_fan_speed",
    "overhang_threshold_participating_cooling", "first_x_layer_part_fan_speed",
    "filament_colour", "filament_ids", "filament_settings_id",
}
# Filament-prefixed keys that are NOT a plain "repeat the entry" list.
NOT_PER_FILAMENT = {"flush_volumes_matrix", "flush_volumes_vector",
                    "filament_self_index"}

# Only deliberate deviations from the stock chain go here.
OVERRIDES = {
    "curr_bed_type": "Textured PEI Plate",
}

# Keys a Bambu-saved project carries but no stock profile sets (Bambu's C++
# defaults, as Bambu Studio 02.09 writes them).  Checked in so a regenerated
# template never depends on (or silently inherits edits from) the previous
# output file.  Stock profile values override these where both exist.
PROJECT_DEFAULTS = {
    "accel_to_decel_enable": "0",
    "accel_to_decel_factor": "50%",
    "alternate_extra_wall": "0",
    "ams_filament_load_time_ams": "0",
    "ams_filament_load_time_ams_lite": "0",
    "ams_filament_load_time_n3f_s": "0",
    "ams_filament_unload_time_ams": "0",
    "ams_filament_unload_time_ams_lite": "0",
    "ams_filament_unload_time_n3f_s": "0",
    "apply_scarf_seam_on_circles": "1",
    "bed_custom_model": "",
    "bed_custom_texture": "",
    "bed_heat_soak_area": [],
    "bed_temperature_formula": "by_first_filament",
    "before_layer_change_gcode": "",
    "bridge_angle": "0",
    "brim_type": "auto_brim",
    "counterbore_hole_bridging": "none",
    "default_ams_type": "-1",
    "default_filament_colour": [""],
    "default_jerk": "0",
    "detect_narrow_internal_solid_infill": "1",
    "embedding_wall_into_infill": "0",
    "enable_filament_dynamic_map": "0",
    "enable_mixed_color_sublayer": "0",
    "enable_order_independent_overlap_carving": "0",
    "enable_overhang_bridge_fan": ["1"],
    "enable_pressure_advance": ["0"],
    "enforce_support_layers": "0",
    "ensure_vertical_shell_thickness": "enabled",
    "exclude_object": "1",
    "extruder_ams_count": [],
    "extruder_nozzle_stats": [],
    "farthest_point_timelapse": "0",
    "filament_change_length_nc": ["10"],
    "filament_colour": ["#00AE42"],
    "filament_is_mixed": ["0"],
    "filament_map": ["1"],
    "filament_map_2": ["1"],
    "filament_map_mode": "Auto For Flush",
    "filament_mixed_components": [""],
    "filament_mixed_gradient": ["0"],
    "filament_mixed_gradient_curve": [""],
    "filament_mixed_gradient_per_part": ["0"],
    "filament_mixed_gradient_range": [""],
    "filament_mixed_sublayer_ratios": [""],
    "filament_notes": "",
    "filament_nozzle_map": ["1"],
    "filament_volume_map": ["0"],
    "filter_out_gap_fill": "0",
    "first_layer_print_sequence": ["0"],
    "first_x_layer_fan_speed": ["0"],
    "first_x_layer_part_fan_speed": ["0"],
    "flush_into_infill": "0",
    "flush_into_objects": "0",
    "flush_into_support": "1",
    "flush_multiplier": ["1"],
    "flush_multiplier_fast": ["1.2"],
    "gcode_add_line_number": "0",
    "has_filament_switcher": "0",
    "has_scarf_joint_seam": "0",
    "independent_support_layer_height": "1",
    "infill_jerk": "9",
    "initial_layer_flow_ratio": "1",
    "initial_layer_jerk": "9",
    "inner_wall_jerk": "9",
    "interlocking_beam": "0",
    "interlocking_beam_layer_count": "2",
    "interlocking_beam_width": "0.8",
    "interlocking_boundary_avoidance": "2",
    "interlocking_depth": "2",
    "interlocking_orientation": "22.5",
    "internal_solid_infill_pattern": "zig-zag",
    "ironing_direction": "45",
    "ironing_fan_speed": ["-1"],
    "ironing_pattern": "zig-zag",
    "is_infill_first": "0",
    "min_bead_width": "85%",
    "min_feature_size": "25%",
    "mmu_segmented_region_interlocking_depth": "0",
    "mmu_segmented_region_max_width": "0",
    "name": "project_settings",
    "nozzle_type": ["stainless_steel"],
    "nozzle_volume_type": ["Standard"],
    "only_one_wall_first_layer": "0",
    "ooze_prevention": "0",
    "other_layers_print_sequence": ["0"],
    "other_layers_print_sequence_nums": "0",
    "outer_wall_jerk": "9",
    "overhang_threshold_participating_cooling": ["95%"],
    "post_process": [],
    "precise_outer_wall": "0",
    "precise_z_height": "0",
    "pressure_advance": ["0.02"],
    "prime_tower_extra_rib_length": "0",
    "prime_tower_fillet_wall": "1",
    "prime_tower_infill_gap": "150%",
    "prime_tower_rib_wall": "1",
    "prime_tower_rib_width": "8",
    "prime_tower_skip_points": "1",
    "prime_volume_mode": "Default",
    "print_flow_ratio": "1",
    "printer_notes": "",
    "printing_by_object_gcode": "",
    "process_notes": "",
    "raft_contact_distance": "0.1",
    "raft_expansion": "1.5",
    "raft_first_layer_density": "90%",
    "raft_first_layer_expansion": "-1",
    "role_base_wipe_speed": "1",
    "seam_gap": "15%",
    "seam_slope_conditional": "1",
    "seam_slope_entire_loop": "0",
    "seam_slope_inner_walls": "1",
    "seam_slope_steps": "10",
    "slice_closing_radius": "0.049",
    "slicing_mode": "regular",
    "smooth_speed_discontinuity_area": "1",
    "sparse_infill_anchor": "400%",
    "sparse_infill_anchor_max": "20",
    "spiral_mode_max_xy_smoothing": "200%",
    "spiral_mode_smooth": "0",
    "start_end_points": ["30x-3", "54x245"],
    "support_angle": "0",
    "support_bottom_interface_spacing": "0.5",
    "support_critical_regions_only": "0",
    "support_interface_not_for_body": "1",
    "support_object_first_layer_gap": "0.2",
    "support_remove_small_overhang": "1",
    "template_custom_gcode": "",
    "thick_bridges": "0",
    "thumbnail_size": ["50x50"],
    "timelapse_type": "0",
    "top_area_threshold": "200%",
    "top_one_wall_type": "all top",
    "top_surface_jerk": "9",
    "top_z_overrides_xy_distance": "0",
    "travel_jerk": "9",
    "tree_support_branch_diameter_angle": "5",
    "tree_support_branch_distance": "5",
    "use_firmware_retraction": "0",
    "use_relative_e_distances": "1",
    "wall_distribution_count": "1",
    "wall_sequence": "inner wall/outer wall",
    "wall_transition_angle": "10",
    "wall_transition_filter_deviation": "25%",
    "wall_transition_length": "100%",
    "wipe_speed": "80%",
    "wipe_tower_rotation_angle": "0",
    "wipe_tower_x": ["15"],
    "wipe_tower_y": ["220"],
    "wrapping_detection_layers": "20",
}


def app_from_exe(exe: str | Path | None) -> Path:
    """BambuStudio.app from its executable path (.../Contents/MacOS/BambuStudio)."""
    if exe:
        p = Path(exe).resolve()
        for parent in p.parents:
            if parent.suffix == ".app":
                return parent
    return DEFAULT_APP


def bbl_dir(app: Path) -> Path:
    return Path(app) / "Contents" / "Resources" / "profiles" / "BBL"


@lru_cache(maxsize=32)
def _cached_app_version(app_str: str) -> str:
    return plistlib.loads((Path(app_str) / "Contents" / "Info.plist").read_bytes())[
        "CFBundleShortVersionString"]


def app_version(app: Path) -> str:
    return _cached_app_version(str(Path(app)))


def _index_cache_dir() -> Path:
    env = os.environ.get("BRICKPRINT_CACHE", "").strip()
    if env:
        base = Path(env).expanduser()
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Caches" / "brickprint"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
        base = (Path(xdg).expanduser() if xdg else Path.home() / ".cache") / "brickprint"
    return base / "profile_index"


def _index_cache_path(bbl: str, kind: str) -> Path:
    h = hashlib.sha1(f"{bbl}\0{kind}".encode("utf-8")).hexdigest()[:16]
    return _index_cache_dir() / f"v1_{kind}_{h}.json"


def _dir_sig(root: Path) -> list[tuple[str, int]]:
    """Fast mtime signature of ``root`` and its immediate subdirectories."""
    sig: list[tuple[str, int]] = []
    try:
        sig.append((".", root.stat().st_mtime_ns))
        with os.scandir(root) as it:
            for e in sorted(it, key=lambda x: x.name):
                if e.is_dir(follow_symlinks=False):
                    try:
                        sig.append((e.name, e.stat(follow_symlinks=False).st_mtime_ns))
                    except OSError:
                        pass
    except OSError:
        pass
    return sig


@lru_cache(maxsize=None)
def _index(bbl: str, kind: str) -> dict:
    root = Path(bbl) / kind
    sig = _dir_sig(root)
    cpath = _index_cache_path(bbl, kind)
    if sig and cpath.is_file():
        try:
            cached = json.loads(cpath.read_text(encoding="utf-8"))
            if (isinstance(cached, dict) and cached.get("bbl") == bbl
                    and cached.get("kind") == kind and cached.get("sig") == [list(x) for x in sig]
                    and isinstance(cached.get("entries"), dict)):
                idx = {name: root / rel for name, rel in cached["entries"].items()}
                # Spot-check that the index isn't stale
                if not idx or next(iter(idx.values())).is_file():
                    return idx
        except (OSError, ValueError, TypeError):
            pass

    idx = {}
    rels = {}
    if root.is_dir():
        for p in root.rglob("*.json"):
            try:
                name = json.loads(p.read_text()).get("name")
            except (ValueError, UnicodeDecodeError, OSError):
                continue
            if name:
                idx[name] = p
                try:
                    rels[name] = p.relative_to(root).as_posix()
                except ValueError:
                    rels[name] = str(p)
    if sig and rels:
        try:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            tmp = cpath.with_name(f".{cpath.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            payload = {"bbl": bbl, "kind": kind, "sig": [list(x) for x in sig], "entries": rels}
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, cpath)
        except OSError:
            pass
    return idx


def index_profiles(bbl: Path, kind: str) -> dict:
    return _index(str(bbl), kind)


@lru_cache(maxsize=1024)
def _read_json_cached(path_str: str, mtime_ns: int) -> dict:
    return json.loads(Path(path_str).read_text())


def _load_preset_file(p: Path) -> dict:
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return json.loads(p.read_text())
    return _read_json_cached(str(p), mtime)


def resolve(idx: dict, name: str) -> dict:
    if name not in idx:
        raise SystemExit(f"Bambu preset not found: {name!r}")
    data = _load_preset_file(idx[name])
    out: dict = {}
    if data.get("inherits"):
        out.update(resolve(idx, data["inherits"]))
    for inc in data.get("include", []):
        out.update(resolve(idx, inc))
    out.update(data)
    return out


@lru_cache(maxsize=256)
def _resolved_chain_cached(bbl: str, kind: str, name: str) -> dict:
    return resolve(_index(bbl, kind), name)


def chain(app: Path, kind: str, name: str) -> dict:
    """Resolved preset values without bookkeeping keys."""
    full = _resolved_chain_cached(str(bbl_dir(app)), kind, name)
    return {k: (list(v) if isinstance(v, list) else v) for k, v in full.items() if k not in META}


def raw_chain(app: Path, kind: str, name: str) -> dict:
    return copy.deepcopy(_resolved_chain_cached(str(bbl_dir(app)), kind, name))


def build_template(app: Path, machine: str = MACHINE, process: str = PROCESS,
                   filament: str = FILAMENT, overrides: dict | None = None) -> dict:
    """One-slot project settings for the given stock chain (see module doc)."""
    bbl = str(bbl_dir(app))
    m = _resolved_chain_cached(bbl, "machine", machine)
    f = _resolved_chain_cached(bbl, "filament", filament)
    p = _resolved_chain_cached(bbl, "process", process)
    settings = {k: (list(v) if isinstance(v, list) else v) for k, v in PROJECT_DEFAULTS.items()}
    stock: dict = {}
    for ch in (m, p, f):          # filament last: it owns shared keys
        for k, v in ch.items():
            if k not in META:
                stock[k] = list(v) if isinstance(v, list) else v
    settings.update(stock)
    settings.update(OVERRIDES if overrides is None else overrides)
    settings.update({
        "from": "project",
        "version": app_version(app),
        "printer_settings_id": machine,
        "print_settings_id": process,
        "filament_settings_id": [filament],
        "filament_ids": [f["filament_id"]],
        "printer_model": m["printer_model"],
        "printer_variant": m["printer_variant"],
        "print_compatible_printers": [machine],
        "different_settings_to_system": ["", "", ""],
        "inherits_group": ["", "", ""],
        "filament_self_index": ["1"],
        "flush_volumes_matrix": ["0"],
        "flush_volumes_vector": ["140", "140"],
    })
    settings.pop("inherits", None)
    per_fil = sorted(
        k for k, v in settings.items()
        if isinstance(v, list) and len(v) == 1 and k not in NOT_PER_FILAMENT
        and (k in f or k in EXTRA_PER_FILAMENT or k.startswith("filament_"))
        and k not in META
    )
    settings["_brickprint_per_filament_keys"] = per_fil
    return dict(sorted(settings.items()))


def template_text(settings: dict) -> str:
    return json.dumps(dict(sorted(settings.items())), indent=4) + "\n"
