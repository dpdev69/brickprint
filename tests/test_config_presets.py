"""Printer / bed presets (config.PRESETS) and their precedence.

No Bambu Studio or network: the user config is a tmp file, BRICKPRINT_*
variables are cleared, and the derived-template step is either mocked or run
against a tiny fake Bambu Studio app bundle."""

from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path

import pytest

from brickprint import cli, config, pipeline, threemf

# the historic (pre-preset) A1 mini values of load_config()
LEGACY = {
    "ldraw_dir": None, "ldview": None, "bambu": None, "cache_dir": None,  # machine-dependent
    "settings": str(config.DEFAULT_SETTINGS),
    "bed": 180.0,
    "depth": 180.0,
    "margin": 6.0,
    "gap": 1.0,
    "bed_type": "Textured PEI Plate",
    "max_filaments": 32,
    "filament_type": "PLA",
    "filament_settings_id": "Bambu PLA Basic @BBL A1M",
    "filament_id": "GFA00",
    "sweep_days": 3,
    "verify_keep_gb": 2,
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated environment: no BRICKPRINT_* variables, a tmp config file and
    cache, and a fake derived template (no Bambu Studio needed)."""
    for k in list(os.environ):
        if k.startswith("BRICKPRINT_"):
            monkeypatch.delenv(k, raising=False)
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("BRICKPRINT_CONFIG", str(cfg_file))
    monkeypatch.setenv("BRICKPRINT_CACHE", str(tmp_path / "cache"))
    calls = []

    def fake_derive(cfg, name):
        calls.append(name)
        p = tmp_path / f"{name}_template.json"
        p.write_text(json.dumps({"printer_settings_id": config.PRESETS[name]["machine"]}))
        return p

    monkeypatch.setattr(config, "_derive_settings", fake_derive)

    class Env:
        path = tmp_path
        derived = calls

        @staticmethod
        def write(data: dict) -> None:
            cfg_file.write_text(json.dumps(data))

    return Env


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def test_registry_contents():
    assert config.DEFAULT_PRESET == "a1mini"
    assert set(config.preset_names()) >= {"a1mini", "a1", "p1s", "x1c"}
    assert config.preset_names() == list(config.PRESETS)
    for name, p in config.PRESETS.items():
        for k in ("label", "bed", "depth", "height", "machine", "process",
                  "filament_settings_id", "filament_id", "short", "bed_exclude_area"):
            assert k in p, (name, k)
        assert p["machine"].endswith(" 0.4 nozzle")
        assert p["filament_settings_id"].startswith("Bambu PLA Basic @BBL ")
        assert p["process"].startswith("0.20mm Standard @BBL ")


def test_registry_values_from_bambu_profiles():
    P = config.PRESETS
    assert (P["a1mini"]["bed"], P["a1mini"]["depth"], P["a1mini"]["height"]) == (180, 180, 180)
    assert (P["a1"]["bed"], P["a1"]["depth"], P["a1"]["height"]) == (256, 256, 256)
    assert (P["p1s"]["bed"], P["p1s"]["height"]) == (256, 250)
    assert (P["x1c"]["bed"], P["x1c"]["height"]) == (256, 250)
    assert P["a1"]["machine"] == "Bambu Lab A1 0.4 nozzle"
    assert P["p1s"]["machine"] == "Bambu Lab P1S 0.4 nozzle"
    assert P["x1c"]["machine"] == "Bambu Lab X1 Carbon 0.4 nozzle"
    assert P["p1s"]["process"] == P["x1c"]["process"] == "0.20mm Standard @BBL X1C"
    assert P["p1s"]["filament_settings_id"] == "Bambu PLA Basic @BBL P1S 0.4 nozzle"
    assert P["x1c"]["bed_exclude_area"] == [[0.0, 0.0], [18.0, 28.0]]
    assert P["a1"]["bed_exclude_area"] == [] and P["a1mini"]["bed_exclude_area"] == []
    # only the A1 mini has a bundled snapshot
    assert P["a1mini"]["settings"] == str(config.DEFAULT_SETTINGS)
    assert all(not P[n]["settings"] for n in ("a1", "p1s", "x1c"))


# ---------------------------------------------------------------------------
# default == legacy
# ---------------------------------------------------------------------------

def test_default_is_legacy_a1mini(env):
    cfg = config.load_config()
    for k, v in LEGACY.items():
        assert k in cfg
        if v is not None:
            assert cfg[k] == v, k
    assert set(cfg) == set(LEGACY) | {"preset", "_explicit"}
    assert cfg["preset"] == "a1mini" and cfg["_explicit"] == ["cache_dir"]   # BRICKPRINT_CACHE
    assert "preset_error" not in cfg
    assert env.derived == []          # the A1 mini never needs Bambu Studio


def test_explicit_a1mini_is_legacy_too(env):
    assert {k: v for k, v in config.load_config(preset="a1mini").items() if k != "preset"} == \
        {k: v for k, v in config.load_config().items() if k != "preset"}


def test_missing_user_template_falls_back_on_a1mini_only(env):
    env.write({"settings": str(env.path / "nope.json")})
    assert config.load_config()["settings"] == str(config.DEFAULT_SETTINGS)
    cfg = config.load_config(preset="x1c")
    assert cfg["settings"] == str(env.path / "nope.json")   # never the A1 mini snapshot


# ---------------------------------------------------------------------------
# apply_preset
# ---------------------------------------------------------------------------

def test_apply_preset_semantics(env):
    cfg = {"bed": 1.0, "cache_dir": str(env.path)}
    out = config.apply_preset(cfg, "X1C")
    assert out is cfg
    assert cfg["preset"] == "x1c"
    assert (cfg["bed"], cfg["depth"]) == (256.0, 256.0)
    assert cfg["filament_settings_id"] == "Bambu PLA Basic @BBL X1C"
    assert cfg["filament_id"] == "GFA00"
    assert cfg["settings"] == str(env.path / "x1c_template.json")
    config.apply_preset(cfg, " a1mini ")
    assert cfg["preset"] == "a1mini" and cfg["bed"] == 180.0
    assert cfg["settings"] == str(config.DEFAULT_SETTINGS)


def test_apply_preset_unknown_name():
    with pytest.raises(ValueError) as e:
        config.apply_preset({}, "ender3")
    msg = str(e.value)
    assert "ender3" in msg
    for name in config.preset_names():
        assert name in msg


def test_apply_preset_keeps_explicit_keys(env):
    cfg = {"bed": 200.0, "settings": "/my/template.json", "_explicit": ["bed", "settings"]}
    config.apply_preset(cfg, "p1s")
    assert cfg["bed"] == 200.0 and cfg["settings"] == "/my/template.json"
    assert cfg["depth"] == 256.0
    assert env.derived == []           # explicit settings: nothing derived


def test_derived_preset_without_bambu_is_a_clear_error(tmp_path, monkeypatch):
    cfg = {"bambu": "", "cache_dir": str(tmp_path)}
    config.apply_preset(cfg, "a1")
    assert "Bambu Studio was not found" in cfg["preset_error"]
    assert "a1" in cfg["preset_error"]
    assert not Path(cfg["settings"]).exists()
    assert cfg["settings"] != str(config.DEFAULT_SETTINGS)
    with pytest.raises(SystemExit) as e:
        config.require_settings(cfg)
    assert "Bambu Studio was not found" in str(e.value)
    config.apply_preset(cfg, "a1mini")              # the A1 mini needs no Bambu Studio
    assert "preset_error" not in cfg
    config.require_settings(cfg)


def _fake_bambu(root: Path) -> Path:
    """Minimal BambuStudio.app with the X1C chain (machine, process, filament)."""
    app = root / "BambuStudio.app"
    exe = app / "Contents" / "MacOS" / "BambuStudio"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleShortVersionString": "09.09.09.09"}))
    bbl = app / "Contents" / "Resources" / "profiles" / "BBL"
    p = config.PRESETS["x1c"]
    files = {
        "machine": {"name": p["machine"], "printer_model": "Bambu Lab X1 Carbon",
                    "printer_variant": "0.4", "printable_area": ["0x0", "256x0", "256x256", "0x256"],
                    "printable_height": "250"},
        "process": {"name": p["process"], "layer_height": "0.2"},
        "filament": {"name": p["filament_settings_id"], "filament_id": "GFA00",
                     "filament_type": ["PLA"]},
    }
    for kind, data in files.items():
        (bbl / kind).mkdir(parents=True)
        (bbl / kind / "x.json").write_text(json.dumps(data))
    return exe


def test_derive_settings_from_bambu_profiles(tmp_path):
    exe = _fake_bambu(tmp_path)
    cfg = {"bambu": str(exe), "cache_dir": str(tmp_path / "cache")}
    config.apply_preset(cfg, "x1c")
    assert "preset_error" not in cfg
    path = Path(cfg["settings"])
    assert path == tmp_path / "cache" / "presets" / "x1c_09.09.09.09.json"
    T = json.loads(path.read_text())
    assert T["printer_settings_id"] == "Bambu Lab X1 Carbon 0.4 nozzle"
    assert T["print_settings_id"] == "0.20mm Standard @BBL X1C"
    assert T["filament_settings_id"] == ["Bambu PLA Basic @BBL X1C"]
    assert T["printable_area"][2] == "256x256" and T["printable_height"] == "250"
    # cached: a second apply reuses the file
    mtime = path.stat().st_mtime_ns
    config.apply_preset(dict(cfg, settings=""), "x1c")
    assert path.stat().st_mtime_ns == mtime


def test_derive_settings_missing_stock_profile(tmp_path):
    exe = _fake_bambu(tmp_path)
    cfg = {"bambu": str(exe), "cache_dir": str(tmp_path / "cache")}
    config.apply_preset(cfg, "p1s")       # the fake app only has the X1C chain
    assert "lacks a stock profile" in cfg["preset_error"]


# ---------------------------------------------------------------------------
# precedence: default < config "preset" < BRICKPRINT_PRESET < --preset,
# explicit values over preset values
# ---------------------------------------------------------------------------

def test_precedence_config_env_cli(env, monkeypatch):
    env.write({"preset": "a1"})
    assert config.load_config()["preset"] == "a1"
    monkeypatch.setenv("BRICKPRINT_PRESET", "P1S")
    assert config.load_config()["preset"] == "p1s"
    cfg = config.load_config(preset="x1c")
    assert cfg["preset"] == "x1c"
    assert cfg["filament_settings_id"] == "Bambu PLA Basic @BBL X1C"


def test_explicit_config_values_survive_preset(env, monkeypatch):
    env.write({"preset": "a1", "bed": 200, "filament_settings_id": "My PLA"})
    cfg = config.load_config(preset="x1c")
    assert cfg["preset"] == "x1c"
    assert cfg["bed"] == 200                       # from config.json
    assert cfg["depth"] == 256.0                   # from the preset
    assert cfg["filament_settings_id"] == "My PLA"
    assert cfg["filament_id"] == "GFA00"
    assert sorted(cfg["_explicit"]) == ["bed", "cache_dir", "filament_settings_id"]


def test_env_settings_survives_preset(env, monkeypatch):
    tpl = env.path / "mine.json"
    tpl.write_text("{}")
    monkeypatch.setenv("BRICKPRINT_SETTINGS", str(tpl))
    cfg = config.load_config(preset="a1")
    assert cfg["settings"] == str(tpl)
    assert env.derived == []


def test_bad_preset_in_env_or_config(env, monkeypatch):
    env.write({"preset": "nope"})
    with pytest.raises(SystemExit) as e:
        config.load_config()
    assert "nope" in str(e.value) and "a1mini" in str(e.value)
    monkeypatch.setenv("BRICKPRINT_PRESET", "bad")
    with pytest.raises(SystemExit) as e:
        config.load_config()
    assert "BRICKPRINT_PRESET" in str(e.value)


def test_cli_flags_win_over_preset(env, monkeypatch, tmp_path):
    seen = {}

    def fake_validate(path, bed, depth):
        seen.update(bed=bed, depth=depth)
        return {"plate_count": 0, "components": 0, "triangles": 0, "plates": [], "bad_plates": 0}

    monkeypatch.setattr(threemf, "validate_project", fake_validate)
    assert cli.main(["validate", "x.3mf", "--preset", "x1c"]) == 0
    assert seen == {"bed": 256.0, "depth": 256.0}
    assert cli.main(["validate", "x.3mf", "--preset", "x1c", "--bed", "220"]) == 0
    assert seen == {"bed": 220.0, "depth": 256.0}
    env.write({"depth": 150})
    assert cli.main(["validate", "x.3mf", "--preset", "a1"]) == 0
    assert seen == {"bed": 256.0, "depth": 150.0}
    assert cli.main(["validate", "x.3mf"]) == 0
    assert seen == {"bed": 180.0, "depth": 150.0}


def test_doctor_prints_active_preset(env, capsys, monkeypatch):
    monkeypatch.setenv("BRICKPRINT_PRESET", "a1")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "preset      a1: Bambu Lab A1, 0.4 mm nozzle" in out
    assert "bed         256.0 x 256.0 mm" in out
    cli.main(["doctor", "--preset", "a1mini"])
    out = capsys.readouterr().out
    assert "preset      a1mini:" in out and "bed         180.0 x 180.0 mm" in out


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["build", "1-1"], ["calibrate", "fit"], ["validate", "x.3mf"], ["doctor"],
    ["profiles", "regen-template"],
])
def test_parser_accepts_preset(argv):
    ap = cli.build_parser()
    assert ap.parse_args(argv).preset is None
    assert ap.parse_args(argv + ["--preset", "X1C"]).preset == "x1c"
    assert ap.parse_args(argv + ["--preset", "a1"]).preset == "a1"


@pytest.mark.parametrize("argv", [["build", "1-1"], ["validate", "x.3mf"]])
def test_parser_rejects_unknown_preset(argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(argv + ["--preset", "ender3"])
    assert e.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# bed exclusion zone (P1S / X1C purge corner)
# ---------------------------------------------------------------------------

def test_exclusion_shift():
    assert pipeline._exclusion_shift({"preset": "a1mini", "margin": 6.0}) == (0.0, 0.0)
    assert pipeline._exclusion_shift({"preset": "a1", "margin": 6.0}) == (0.0, 0.0)
    assert pipeline._exclusion_shift({"preset": "x1c", "margin": 6.0}) == (13.0, 0.0)
    assert pipeline._exclusion_shift({"preset": "p1s", "margin": 19.0}) == (0.0, 0.0)


def test_shift_plates_moves_items_groups_objects_tower():
    it = ("3001.dat", 0.0, 1.0, 32.0, 16.0)
    plates = [{"items": [it], "groups": [{"items": [it]}], "objects": [{"items": [it]}],
               "tower": (190.0, 200.0)}, {"items": [it]}]
    pipeline._shift_plates(plates, 13.0, 0.0)
    moved = ("3001.dat", 13.0, 1.0, 32.0, 16.0)
    assert plates[0]["items"] == [moved] and plates[0]["groups"][0]["items"] == [moved]
    assert plates[0]["objects"][0]["items"] == [moved] and plates[0]["tower"] == (203.0, 200.0)
    assert plates[1]["items"] == [moved]
