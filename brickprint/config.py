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


def load_config() -> dict:
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

    config_path = Path(os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_PATH)).expanduser()
    if config_path.is_file():
        try:
            cfg.update(json.loads(config_path.read_text()))
        except (OSError, json.JSONDecodeError):
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

    if not Path(cfg["settings"]).is_file() and DEFAULT_SETTINGS.is_file():
        cfg["settings"] = str(DEFAULT_SETTINGS)

    cfg["cache_dir"] = str(Path(cfg["cache_dir"]).expanduser())
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
    print(f"settings    {cfg['settings']} (default profile snapshot)")
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
