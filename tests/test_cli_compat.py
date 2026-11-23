"""cli.py was split into cli (parser + dispatch) and pipeline (the work).

Everything that used to be importable from ``brickprint.cli`` must still be,
and the parser must accept the same subcommands and options as before."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from brickprint import cli, pipeline

# names that moved to pipeline and are re-exported by cli
MOVED = [
    "PLATE_CHECK_ROUNDS", "UNVERIFIED_BANNER", "slugify",
    "_load_orient", "_map_lots", "_merge_lots", "_bottom_contact",
    "_orientation_warnings", "_write_plates_csv", "_normalise_with_bambu",
    "_apply_options", "_prepare_meshes", "_object_pad", "_apply_features",
    "_check_ribs", "_prime_tower_on", "_screen_final", "_check_profile",
    "_resolve_parts", "_test_objects", "_auto_test_parts", "_expected",
    "_run_verify", "_profiles_check",
]

# names still defined in cli
LOCAL = [
    "build_parser", "main", "_tuning_args",
    "cmd_doctor", "cmd_bootstrap", "cmd_inventory", "cmd_build", "cmd_calibrate",
    "cmd_verify", "cmd_quality", "cmd_profiles", "cmd_validate",
]

# subcommand -> (positionals, option strings), read from the pre-split parser
# (git show e16909e:brickprint/cli.py).  New options may be added; none may go.
LEGACY_PARSER = {
    "doctor": ([], []),
    "bootstrap": ([], ["--refresh"]),
    "inventory": (["set"], ["--include-minifigs", "--include-spares", "--out", "--refresh"]),
    "build": (["set"], ["--allow-shrink", "--bambu", "--bed", "--bom", "--bridging", "--ceilings",
                        "--depth", "--filament", "--fit", "--fit-test", "--gap",
                        "--include-minifigs", "--include-spares", "--margin", "--mix-colours",
                        "--name", "--no-bambu", "--no-probe", "--orient", "--out", "--part-map",
                        "--profile", "--reference", "--refresh", "--set", "--strict",
                        "--supports", "--verify"]),
    "calibrate": (["what"], ["--allow-shrink", "--bambu", "--bridging", "--ceilings", "--filament",
                             "--hole-ratio", "--no-probe", "--out", "--part-map", "--parts",
                             "--profile", "--set", "--supports", "--values", "--verify"]),
    "verify": (["file"], ["--expect", "--fit", "--plates", "--profile", "--reference", "--report",
                          "--set", "--tol", "--workdir"]),
    "quality": (["parts"], ["--bom", "--csv", "--part-map"]),
    "profiles": (["action", "name"], ["--filament", "--fit", "--out", "--resolved", "--to"]),
    "validate": (["file"], ["--bed", "--depth", "--slice"]),
}


def _subparsers(ap):
    return next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction)).choices


@pytest.mark.parametrize("name", MOVED)
def test_moved_names_reexported(name):
    assert hasattr(cli, name), name
    assert getattr(cli, name) is getattr(pipeline, name)
    assert name in cli.__all__


@pytest.mark.parametrize("name", LOCAL)
def test_local_names_importable(name):
    assert callable(getattr(cli, name))
    assert name in cli.__all__


def test_star_import_and_main_entry():
    ns = {}
    exec("from brickprint.cli import *", ns)
    for name in MOVED + LOCAL:
        assert name in ns
    from brickprint.cli import main  # what brickprint/__main__.py does
    assert main is cli.main


def test_python_dash_m_help():
    proc = subprocess.run([sys.executable, "-m", "brickprint", "--help"], capture_output=True,
                          text=True, cwd=str(Path(pipeline.__file__).resolve().parent.parent))
    assert proc.returncode == 0, proc.stderr
    assert "colour-grouped multi-plate 3MF" in proc.stdout


def test_pipeline_does_not_import_cli():
    src = open(pipeline.__file__, encoding="utf-8").read()
    assert "from . import cli" not in src and "from .cli" not in src and "brickprint.cli" not in src


def test_subcommands_and_options_preserved():
    subs = _subparsers(cli.build_parser())
    assert set(LEGACY_PARSER) <= set(subs)
    for name, (pos, opts) in LEGACY_PARSER.items():
        p = subs[name]
        have_opts = {s for a in p._actions for s in a.option_strings}
        have_pos = [a.dest for a in p._actions if not a.option_strings]
        assert have_pos == pos, name
        missing = set(opts) - have_opts
        assert not missing, f"{name}: lost {sorted(missing)}"


def test_dispatch_targets():
    ap = cli.build_parser()
    cases = {
        "doctor": ["doctor"], "bootstrap": ["bootstrap"], "inventory": ["inventory", "1-1"],
        "build": ["build", "1-1"], "calibrate": ["calibrate", "fit"], "verify": ["verify", "x.3mf"],
        "quality": ["quality"], "profiles": ["profiles", "list"], "validate": ["validate", "x.3mf"],
    }
    for name, argv in cases.items():
        assert ap.parse_args(argv).func is getattr(cli, f"cmd_{name}")


def test_representative_argument_lines():
    ap = cli.build_parser()
    a = ap.parse_args(["build", "10337-1", "--bed", "200", "--depth", "190", "--no-probe",
                       "--profile", "a1mini-petg-008-lego-v8", "--filament", "Black=porima-petg-black",
                       "--set", "wall_loops=3", "--mix-colours", "off", "--fit-test=3022,3005"])
    assert (a.set, a.bed, a.depth, a.probe, a.bambu) == ("10337-1", 200.0, 190.0, False, False)
    assert a.profile == "a1mini-petg-008-lego-v8"
    assert a.filament == ["Black=porima-petg-black"] and a.set_kv == ["wall_loops=3"]
    assert a.mix_colours == "off" and a.fit_test == "3022,3005"
    assert a.margin is None and a.gap is None and a.verify is False

    a = ap.parse_args(["build", "--bom", "b.csv", "--bambu", "--fit-test"])
    assert a.set is None and a.bom == "b.csv" and a.bambu is True and a.fit_test == "auto"
    assert a.mix_colours == "auto" and a.probe is True

    a = ap.parse_args(["calibrate", "fit"])
    assert (a.what, a.values, a.parts, a.out, a.hole_ratio, a.probe) == (
        "fit", "0.02,0.03,0.04,0.05", "3022,3005,3024,4733", "fit_calibration.3mf", None, True)

    a = ap.parse_args(["verify", "f.3mf", "--plates", "1,2", "--expect", "a=1", "--tol", "0.02"])
    assert (a.file, a.plates, a.expect, a.tol, a.workdir) == ("f.3mf", "1,2", ["a=1"], 0.02, None)

    a = ap.parse_args(["validate", "f.3mf", "--bed", "256"])
    assert (a.bed, a.depth, a.slice) == (256.0, None, False)

    a = ap.parse_args(["profiles", "show", "a1mini-pla-stock", "--resolved"])
    assert (a.action, a.name, a.resolved) == ("show", "a1mini-pla-stock", True)

    with pytest.raises(SystemExit):
        ap.parse_args(["build", "1-1", "--supports", "bogus"])


def test_build_without_set_or_bom_errors(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["build"])
    assert e.value.code == 2
    assert "build needs a set number or --bom" in capsys.readouterr().err
