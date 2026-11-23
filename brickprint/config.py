"""Configuration and path discovery for brickprint."""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = SKILL_ROOT / "assets" / "a1mini_pla_project_settings.json"

CONFIG_ENV = "BRICKPRINT_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "brickprint" / "config.json"
PRESET_ENV = "BRICKPRINT_PRESET"

# ---------------------------------------------------------------------------
# printer / bed presets
# ---------------------------------------------------------------------------
# Every name below is copied from Bambu Studio 02.09.01.70's system profiles
# (BambuStudio.app/Contents/Resources/profiles/BBL): the 0.4 mm nozzle machine
# preset, its "default_print_profile" and "default_filament_profile", and its
# resolved "printable_area" / "printable_height" / "bed_exclude_area".
# a1mini ships a checked-in project-settings snapshot (assets/, no Bambu
# Studio needed); the others are derived from the installed Bambu Studio's
# stock profiles (bambu_profiles.build_template) and cached per Bambu version.
DEFAULT_PRESET = "a1mini"
PRESETS: dict[str, dict] = {
    "a1mini": {
        "label": "Bambu Lab A1 mini, 0.4 mm nozzle",
        "bed": 180.0, "depth": 180.0, "height": 180.0,
        "bed_exclude_area": [],
        "machine": "Bambu Lab A1 mini 0.4 nozzle",
        "process": "0.20mm Standard @BBL A1M",
        "filament_settings_id": "Bambu PLA Basic @BBL A1M",
        "filament_id": "GFA00",
        "short": "A1M",
        "settings": str(DEFAULT_SETTINGS),       # bundled snapshot
    },
    "a1": {
        "label": "Bambu Lab A1, 0.4 mm nozzle",
        "bed": 256.0, "depth": 256.0, "height": 256.0,
        "bed_exclude_area": [],
        "machine": "Bambu Lab A1 0.4 nozzle",
        "process": "0.20mm Standard @BBL A1",
        "filament_settings_id": "Bambu PLA Basic @BBL A1",
        "filament_id": "GFA00",
        "short": "A1",
        "settings": None,                        # derived from Bambu Studio
    },
    "p1s": {
        "label": "Bambu Lab P1S, 0.4 mm nozzle",
        "bed": 256.0, "depth": 256.0, "height": 250.0,
        # front-left purge / cutter corner, x 0-18 mm, y 0-28 mm
        "bed_exclude_area": [[0.0, 0.0], [18.0, 28.0]],
        "machine": "Bambu Lab P1S 0.4 nozzle",
        "process": "0.20mm Standard @BBL X1C",   # P1S default_print_profile
        "filament_settings_id": "Bambu PLA Basic @BBL P1S 0.4 nozzle",
        "filament_id": "GFA00",
        "short": "P1S",
        "settings": None,
    },
    "x1c": {
        "label": "Bambu Lab X1 Carbon, 0.4 mm nozzle",
        "bed": 256.0, "depth": 256.0, "height": 250.0,
        "bed_exclude_area": [[0.0, 0.0], [18.0, 28.0]],
        "machine": "Bambu Lab X1 Carbon 0.4 nozzle",
        "process": "0.20mm Standard @BBL X1C",
        "filament_settings_id": "Bambu PLA Basic @BBL X1C",
        "filament_id": "GFA00",
        "short": "X1C",
        "settings": None,
    },
}
# cfg keys a preset supplies; a key the user set explicitly (config.json,
# environment) keeps the user's value
PRESET_KEYS = ("bed", "depth", "filament_settings_id", "filament_id", "settings")


def preset_names() -> list[str]:
    return list(PRESETS)


def _preset_key(name: str) -> str:
    key = str(name).strip().lower()
    if key not in PRESETS:
        raise ValueError(f"unknown preset {name!r} (valid: {', '.join(preset_names())})")
    return key


def preset_template_path(cfg: dict, name: str, version: str) -> Path:
    """Cached project-settings template of a derived preset (one per Bambu
    Studio version: stock profiles change between releases)."""
    return (Path(cfg.get("cache_dir") or default_cache_dir()).expanduser()
            / "presets" / f"{name}_{version}.json")


def _derive_settings(cfg: dict, name: str) -> Path:
    """Project-settings template for a preset without a bundled snapshot,
    resolved from the installed Bambu Studio's stock machine / process /
    filament chain.  Raises SystemExit with a clear message when Bambu Studio
    (or one of the stock presets) is missing: never falls back to the A1 mini
    snapshot."""
    from . import bambu_profiles as bp
    p = PRESETS[name]
    bambu = cfg.get("bambu") or ""
    hint = (f"; or point \"settings\" / BRICKPRINT_SETTINGS at a {p['machine']} project-settings "
            f"template (brickprint profiles regen-template --preset {name} --out FILE)")
    if not bambu or not Path(bambu).is_file():
        raise SystemExit(f"preset {name} ({p['label']}) derives its project settings from the "
                         f"stock Bambu Studio profiles, but Bambu Studio was not found (set "
                         f"\"bambu\" in the config or BRICKPRINT_BAMBU){hint}")
    app = bp.app_from_exe(bambu)
    if not bp.bbl_dir(app).is_dir():
        raise SystemExit(f"preset {name}: no Bambu system profiles under {bp.bbl_dir(app)}{hint}")
    try:
        version = bp.app_version(app)
    except (OSError, KeyError, ValueError) as e:
        raise SystemExit(f"preset {name}: cannot read the Bambu Studio version of {app}: {e}")
    path = preset_template_path(cfg, name, version)
    if path.is_file():
        return path
    try:
        text = bp.template_text(bp.build_template(app, p["machine"], p["process"],
                                                  p["filament_settings_id"]))
    except SystemExit as e:      # bp.resolve: "Bambu preset not found: ..."
        raise SystemExit(f"preset {name}: Bambu Studio {version} lacks a stock profile ({e}){hint}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return path


def apply_preset(cfg: dict, name: str) -> dict:
    """Apply printer preset ``name`` (case-insensitive) to ``cfg`` in place
    and return it.  Keys listed in ``cfg["_explicit"]`` (set by the user in
    config.json or the environment) are left alone.  A derived preset whose
    template cannot be built (no Bambu Studio) records the reason in
    ``cfg["preset_error"]`` (see require_settings) instead of raising, so
    ``doctor`` can still report; its ``settings`` path then does not exist."""
    key = _preset_key(name)
    p = PRESETS[key]
    explicit = set(cfg.get("_explicit") or ())
    cfg["preset"] = key
    cfg.pop("preset_error", None)
    for k in PRESET_KEYS:
        if k in explicit:
            continue
        if k != "settings":
            cfg[k] = p[k]
        elif p["settings"]:
            cfg[k] = p["settings"]
        else:
            try:
                cfg[k] = str(_derive_settings(cfg, key))
            except SystemExit as e:
                cfg[k] = str(preset_template_path(cfg, key, "unresolved"))
                cfg["preset_error"] = str(e)
    return cfg


def require_settings(cfg: dict) -> None:
    """Stop with the preset's error if its project settings are unavailable."""
    if cfg.get("preset_error"):
        raise SystemExit(cfg["preset_error"])


def default_cache_dir() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "brickprint"
    return Path.home() / ".cache" / "brickprint"


def _project_root() -> Path | None:
    """If the skill lives inside a project folder that also holds an LDraw
    library (``ldraw_library/ldraw``), find that folder."""
    for parent in SKILL_ROOT.parents:
        if (parent / "ldraw_library" / "ldraw" / "LDConfig.ldr").is_file():
            return parent
    return None


def _first_file(*candidates: str | Path) -> str:
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(os.path.expanduser(str(candidate)))
        if path.is_file():
            return str(path)
    return ""


def load_config(preset: str | None = None) -> dict:
    """Defaults < printer preset < config.json < environment.

    The preset is chosen by ``preset`` (the CLI's --preset) > env
    BRICKPRINT_PRESET > ``"preset"`` in config.json > DEFAULT_PRESET; its
    values (bed, depth, filament, settings template) never replace a key the
    user set in config.json or through BRICKPRINT_SETTINGS.  With no preset
    anywhere the result is the historic A1 mini configuration."""
    cache_dir = default_cache_dir()
    cfg = {
        "cache_dir": str(cache_dir),
        "ldraw_dir": "",
        "ldview": "",
        "bambu": "",
        "settings": str(DEFAULT_SETTINGS),
        "bed": 180.0,
        "depth": 180.0,
        "margin": 6.0,
        "gap": 1.0,
        "bed_type": "Textured PEI Plate",
        "max_filaments": 32,
        "filament_type": "PLA",
        "filament_settings_id": "Bambu PLA Basic @BBL A1M",
        "filament_id": "GFA00",
        # concurrent Bambu Studio runs (probe / final check / plate check);
        # unset = min(4, cpus // 3), see probe.default_workers
        # "probe_workers": 3,
        # processes for the mesh stage (LDView export, repair, orientation
        # emulator) on cache misses; 1 = serial.  unset = min(8, cpus - 2),
        # see ldraw.default_mesh_workers
        # "mesh_workers": 8,
        # work-dir sweep at the start of build / verify (sweep.py):
        # delete brickprint work dirs older than this many days (0 = never) ...
        "sweep_days": 3,
        # ... and cap the kept brickprint_verify_* temp dirs at this many GB
        # (oldest deleted first; 0 = no cap).  BRICKPRINT_NO_SWEEP=1 disables.
        "verify_keep_gb": 2,
    }

    explicit = set()
    config_path = Path(os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_PATH)).expanduser()
    if config_path.is_file():
        try:
            user = json.loads(config_path.read_text())
            cfg.update(user)
            explicit.update(user)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    # Environment overrides.
    env_map = {
        "BRICKPRINT_CACHE": "cache_dir",
        "BRICKPRINT_LDRAW": "ldraw_dir",
        "BRICKPRINT_LDVIEW": "ldview",
        "BRICKPRINT_BAMBU": "bambu",
        "BRICKPRINT_SETTINGS": "settings",
    }
    for env, key in env_map.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
            explicit.add(key)

    project = _project_root()
    if not cfg.get("ldraw_dir") and project:
        candidate = project / "ldraw_library" / "ldraw"
        if (candidate / "LDConfig.ldr").is_file():
            cfg["ldraw_dir"] = str(candidate)
    if not cfg.get("ldraw_dir"):
        cfg["ldraw_dir"] = _first_file(
            Path.home() / "Library" / "Application Support" / "LDraw" / "ldraw" / "LDConfig.ldr",
            Path("/Applications/LDraw/ldraw/LDConfig.ldr"),
            Path.home() / "ldraw" / "LDConfig.ldr",
            cache_dir / "ldraw" / "LDConfig.ldr",
        )
        if cfg["ldraw_dir"]:
            cfg["ldraw_dir"] = str(Path(cfg["ldraw_dir"]).parent)

    if not cfg.get("ldview"):
        candidates = [
            project / "tools" / "LDView.app" / "Contents" / "MacOS" / "LDView" if project else "",
            "/Applications/LDView.app/Contents/MacOS/LDView",
            shutil.which("LDView") or "",
            shutil.which("ldview") or "",
        ]
        cfg["ldview"] = _first_file(*candidates)

    if not cfg.get("bambu"):
        candidates = [
            "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
            shutil.which("BambuStudio") or "",
            shutil.which("bambu-studio") or "",
        ]
        cfg["bambu"] = _first_file(*candidates)

    cfg["cache_dir"] = str(Path(cfg["cache_dir"]).expanduser())

    if preset:
        source, chosen = "--preset", preset
    elif os.environ.get(PRESET_ENV):
        source, chosen = PRESET_ENV, os.environ[PRESET_ENV]
    elif cfg.get("preset"):
        source, chosen = f"\"preset\" in {config_path}", cfg["preset"]
    else:
        source, chosen = "", DEFAULT_PRESET
    cfg["_explicit"] = sorted(explicit - {"preset"})
    try:
        apply_preset(cfg, chosen)
    except ValueError as e:
        raise SystemExit(f"{source}: {e}")

    # on the A1 mini a missing template falls back to the bundled snapshot (as
    # before presets); another printer never gets the A1 mini settings
    if (not Path(cfg["settings"]).is_file() and DEFAULT_SETTINGS.is_file()
            and cfg["preset"] == DEFAULT_PRESET):
        cfg["settings"] = str(DEFAULT_SETTINGS)
    return cfg


def rebrickable_dir(cfg: dict) -> Path:
    return Path(cfg["cache_dir"]) / "rebrickable"


def mesh_cache_dir(cfg: dict) -> Path:
    return Path(cfg["cache_dir"]) / "meshes"


def ldraw_cache_dir(cfg: dict) -> Path:
    return Path(cfg["cache_dir"]) / "ldraw"


def ensure_dirs(cfg: dict) -> None:
    for path in (Path(cfg["cache_dir"]), rebrickable_dir(cfg), mesh_cache_dir(cfg)):
        path.mkdir(parents=True, exist_ok=True)


def doctor(cfg: dict) -> int:
    import sys
    import numpy

    ok = True
    print(f"python      {sys.version.split()[0]}")
    print(f"python bin  {sys.executable}"
          + (" (BRICKPRINT_PYTHON)" if os.environ.get("BRICKPRINT_PYTHON") else ""))
    print(f"numpy       {numpy.__version__}")
    print(f"cache       {cfg['cache_dir']}")
    print(f"ldraw       {cfg['ldraw_dir'] or '(missing — run bootstrap)'}")
    print(f"ldview      {cfg['ldview'] or '(missing — install LDView)'}")
    print(f"bambu       {cfg['bambu'] or '(missing — floating probe disabled: orientation UNVERIFIED)'}")
    preset = PRESETS.get(cfg.get("preset") or DEFAULT_PRESET, PRESETS[DEFAULT_PRESET])
    print(f"preset      {cfg.get('preset') or DEFAULT_PRESET}: {preset['label']} "
          f"({preset['bed']:g} x {preset['depth']:g} x {preset['height']:g} mm, "
          f"{preset['machine']})")
    if "settings" in (cfg.get("_explicit") or ()):
        origin = "set in config / BRICKPRINT_SETTINGS"
    elif preset["settings"]:
        origin = "default profile snapshot"
    else:
        origin = f"derived from Bambu Studio's stock {preset['machine']} profiles"
    print(f"settings    {cfg['settings']} ({origin})")
    if cfg.get("preset_error"):
        print(f"ERROR: {cfg['preset_error']}")
        ok = False
    try:
        from . import bambu_profiles, settings as _s
        names = ", ".join(n for n, _ in _s.list_profiles()["process"])
        print(f"profiles    process: {names}")
        if cfg["bambu"]:
            app = bambu_profiles.app_from_exe(cfg["bambu"])
            print(f"bambu ver   {bambu_profiles.app_version(app)} (CLI works with the GUI open)")
    except Exception as e:  # doctor must not crash
        print(f"profiles    (could not list: {e})")
    print(f"bed         {cfg['bed']} x {cfg['depth']} mm, margin {cfg['margin']}, gap {cfg['gap']}")

    ldraw = Path(cfg["ldraw_dir"]) if cfg["ldraw_dir"] else None
    if not ldraw or not (ldraw / "LDConfig.ldr").is_file():
        print("ERROR: LDraw library not found")
        ok = False
    else:
        for sub in ("parts", "p"):
            if not (ldraw / sub).is_dir():
                print(f"ERROR: LDraw subdirectory missing: {sub}")
                ok = False
    if not cfg["ldview"] or not Path(cfg["ldview"]).is_file():
        print("ERROR: LDView not found (set BRICKPRINT_LDVIEW)")
        ok = False
    if cfg["bambu"] and not Path(cfg["bambu"]).is_file():
        print("ERROR: Bambu Studio path is not a file")
        ok = False
    if not Path(cfg["settings"]).is_file():
        print("ERROR: settings template missing")
        ok = False

    rb = rebrickable_dir(cfg)
    have = [f.name for f in rb.glob("*.csv.gz")]
    print(f"rebrickable {len(have)} CSV dumps cached in {rb}")
    return 0 if ok else 1
