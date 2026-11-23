"""brickprint command line: set number -> colour-grouped multi-plate 3MF."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import config, pack, pipeline, threemf

# Backwards compatibility: these names lived in this module before the work
# moved to ``pipeline``; they stay importable as ``brickprint.cli.<name>``.
from .pipeline import (
    PLATE_CHECK_ROUNDS,
    UNVERIFIED_BANNER,
    _apply_features,
    _apply_options,
    _auto_test_parts,
    _bottom_contact,
    _check_profile,
    _check_ribs,
    _expected,
    _load_orient,
    _map_lots,
    _merge_lots,
    _normalise_with_bambu,
    _object_pad,
    _orientation_warnings,
    _prepare_meshes,
    _prime_tower_on,
    _profiles_check,
    _resolve_parts,
    _run_verify,
    _screen_final,
    _test_objects,
    _write_plates_csv,
    slugify,
)

__all__ = [
    # parser / dispatch (defined here)
    "build_parser", "main", "_tuning_args",
    "cmd_doctor", "cmd_bootstrap", "cmd_inventory", "cmd_build", "cmd_verify",
    "cmd_calibrate", "cmd_quality", "cmd_profiles", "cmd_validate",
    # re-exported from pipeline
    "PLATE_CHECK_ROUNDS", "UNVERIFIED_BANNER", "slugify",
    "_apply_features", "_apply_options", "_auto_test_parts", "_bottom_contact",
    "_check_profile", "_check_ribs", "_expected", "_load_orient", "_map_lots",
    "_merge_lots", "_normalise_with_bambu", "_object_pad", "_orientation_warnings",
    "_prepare_meshes", "_prime_tower_on", "_profiles_check", "_resolve_parts",
    "_run_verify", "_screen_final", "_test_objects", "_write_plates_csv",
]


def cmd_doctor(args):
    cfg = config.load_config()
    return config.doctor(cfg)


def cmd_bootstrap(args):
    return pipeline.run_bootstrap(args)


def cmd_inventory(args):
    return pipeline.run_inventory(args)


def cmd_build(args):
    return pipeline.run_build(args)


def cmd_verify(args):
    return pipeline.run_verify(args)


def cmd_calibrate(args):
    """One project, one object per (fit value, test part); per-object X-Y
    compensation (Bambu honours it per object, not per part)."""
    return pipeline.run_calibrate(args)


def cmd_quality(args):
    return pipeline.run_quality(args)


def cmd_profiles(args):
    return pipeline.run_profiles(args)


def cmd_validate(args):
    cfg = config.load_config()
    stats = threemf.validate_project(Path(args.file), float(args.bed or cfg["bed"]), float(args.depth or cfg["depth"]))
    print(f"plates {stats['plate_count']}  components {stats['components']}  triangles {stats['triangles']}")
    for p in stats["plates"]:
        print(
            f"{p['id']:2d} {p['name']:28s} parts={p['components']:3d} "
            f"{p['size'][0]:6.1f} x {p['size'][1]:6.1f} x {p['size'][2]:5.1f} "
            f"{'OK' if p['ok'] else 'BAD'}"
        )
    if args.slice:
        print("use `brickprint verify FILE` to slice and check the project.")
    return 0 if stats["bad_plates"] == 0 else 1


def _tuning_args(p, fit: bool = True) -> None:
    g = p.add_argument_group("print tuning (see `brickprint profiles list`)")
    g.add_argument("--profile", help="process profile (default a1mini-pla-stock = Bambu stock "
                                     "PLA 0.20 mm, unchanged output)")
    g.add_argument("--filament", action="append", metavar="COLOUR=PROFILE",
                   help="filament profile per colour (name, color_id or hex), or PROFILE for "
                        "every slot; repeatable")
    if fit:
        g.add_argument("--fit", metavar="C[,H]",
                       help="X-Y contour compensation +C mm and hole compensation H "
                            "(default -C); 0 = none; default from the profile")
    g.add_argument("--supports", choices=["enforcers", "plates"],
                   help="enforcers: support only in generated enforcer volumes under "
                        "cantilevered features (never inside sockets); plates: '<Colour> "
                        "supports' plates with tree supports; default from the profile")
    g.add_argument("--ceilings", choices=["recipe8", "off"],
                   help="recipe8: plate sockets raised to 2.2 mm, thick anchored ceiling "
                        "bridges (two 0.24 mm layers, 2 wall loops on the bridge layer) and "
                        "span-cutting ribs; one object per part; needs thick_bridges=1; "
                        "default from the profile")
    g.add_argument("--bridging", choices=["short-span", "stock"],
                   help="short-span: per-part bridge angle across the short side of cavity "
                        "ceilings; default from the profile")
    g.add_argument("--set", dest="set_kv", action="append", metavar="KEY=VALUE",
                   help="expert: any Bambu process key (shaped to Bambu's type and listed in "
                        "different_settings_to_system); repeatable")
    g.add_argument("--support-set", dest="support_set", action="append", metavar="KEY=VALUE",
                   help="expert: extra per-object Bambu setting, written ONLY on objects with "
                        "their own tree supports (supports plates, auto-supported fit-test "
                        "pieces), e.g. support_top_z_distance=0.64; overrides the profile's "
                        "supports.object_settings per key; repeatable")
    g.add_argument("--allow-shrink", action="store_true",
                   help="accept filament_shrink above 100%% (it scales parts DOWN)")
    g.add_argument("--part-map", help="JSON rebrickable part -> LDraw part (adds to "
                                      "~/.config/brickprint/part_overrides.json)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="brickprint", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check paths and dependencies")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("bootstrap", help="download Rebrickable + LDraw data")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("inventory", help="look up a set and write its BOM")
    p.add_argument("set")
    p.add_argument("--out")
    p.add_argument("--include-spares", action="store_true")
    p.add_argument("--include-minifigs", action="store_true")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_inventory)

    p = sub.add_parser("build", help="build the colour-grouped multi-plate 3MF")
    p.add_argument("set", nargs="?")
    p.add_argument("--bom", help="use a local BOM CSV instead of Rebrickable")
    p.add_argument("--out")
    p.add_argument("--name")
    p.add_argument("--orient", help="JSON file of part -> Rx/Ry/Rx2/Ry2")
    p.add_argument("--bed", type=float)
    p.add_argument("--depth", type=float)
    p.add_argument("--margin", type=float)
    p.add_argument("--gap", type=float)
    p.add_argument("--include-spares", action="store_true")
    p.add_argument("--include-minifigs", action="store_true")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--strict", action="store_true", help="fail on missing LDraw parts")
    p.add_argument("--bambu", action="store_true",
                   help="normalise with the Bambu Studio CLI (thumbnails); works with the GUI open")
    p.add_argument("--no-bambu", dest="bambu", action="store_false")
    p.add_argument("--no-probe", dest="probe", action="store_false",
                   help="skip the Bambu Studio floating probe (emulator only: "
                        "floating detection unverified)")
    _tuning_args(p)
    p.add_argument("--fit-test", nargs="?", const="auto", metavar="PARTS",
                   help="append a 'Fit test' plate of separate test pieces (comma-separated "
                        "part numbers, e.g. --fit-test=3022,3005; bare --fit-test picks from the "
                        "BOM; put it after the set number) printed with the build's fit")
    p.add_argument("--mix-colours", choices=list(pack.MIX_MODES), default="auto",
                   help="let tiny colour groups (last plate <= "
                        f"{pack.MIX_FILL_MAX * 100:.0f}%% full) share plates, <= {pack.MIX_MAX_COLOURS} colours each: "
                        "auto (default) = only when needed to stay within 36 plates (incl. a "
                        "--fit-test plate) instead of folding supports; always; off")
    p.add_argument("--verify", action="store_true",
                   help="slice the result with the Bambu Studio CLI and run `verify`")
    p.add_argument("--reference", help="with --verify: compare against this 3MF")
    p.set_defaults(func=cmd_build, bambu=False, probe=True)

    p = sub.add_parser("calibrate", help="fit calibration project (several X-Y compensation values in one print)")
    p.add_argument("what", choices=["fit"])
    p.add_argument("--values", default="0.02,0.03,0.04,0.05",
                   help="contour compensation values in mm, one row each (default 0.02,0.03,0.04,0.05)")
    p.add_argument("--hole-ratio", type=float, default=None,
                   help="hole compensation = -value x ratio (default 1: hole = -value)")
    p.add_argument("--parts", default="3022,3005,3024,4733",
                   help="test parts, comma-separated (default 3022,3005,3024,4733)")
    p.add_argument("--out", default="fit_calibration.3mf")
    p.add_argument("--bambu", action="store_true", help="normalise with Bambu Studio (thumbnails)")
    p.add_argument("--no-probe", dest="probe", action="store_false")
    p.add_argument("--verify", action="store_true", help="slice and measure every row")
    _tuning_args(p, fit=False)
    p.set_defaults(func=cmd_calibrate, probe=True)

    p = sub.add_parser("verify", help="slice a 3MF with the Bambu Studio CLI and check effective "
                                      "settings, stud/socket toolpaths, supports, floating")
    p.add_argument("file")
    p.add_argument("--plates", help="comma-separated plate numbers (default: all)")
    p.add_argument("--reference", help="compare settings and fit numbers with this 3MF")
    p.add_argument("--tol", type=float, default=0.01, help="fit tolerance in mm (default 0.01)")
    p.add_argument("--workdir", help="keep the sliced G-code here; the <file>_verify.json report "
                                     "is written here too (default: next to FILE)")
    p.add_argument("--report", metavar="PATH",
                   help="write the JSON report to PATH (overrides --workdir / next-to-FILE)")
    p.add_argument("--expect", action="append", metavar="KEY=VALUE",
                   help="intended value that must be effective in the G-code; repeatable")
    p.add_argument("--profile", help="intended settings = this profile (+ --fit/--set); "
                                     "default: from <file>_report.json if present")
    p.add_argument("--fit", help="intended fit (with --profile)")
    p.add_argument("--set", dest="set_kv", action="append", help="intended extra keys (with --profile)")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("quality", help="per-part mesh quality report")
    p.add_argument("parts", nargs="*", help="part numbers")
    p.add_argument("--bom", help="all parts of a BOM CSV")
    p.add_argument("--csv", help="also write a CSV")
    p.add_argument("--part-map", help="JSON rebrickable part -> LDraw part")
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser("profiles", help="list / show / check / export print profiles")
    p.add_argument("action", choices=["list", "show", "check", "export", "regen-template"])
    p.add_argument("name", nargs="?")
    p.add_argument("--resolved", action="store_true", help="show: final values + diff lists")
    p.add_argument("--filament", action="append", help="show --resolved: filament profile")
    p.add_argument("--fit", help="show --resolved: fit")
    p.add_argument("--to", help="export: target directory (never Bambu's user folders)")
    p.add_argument("--out", help="regen-template: write here instead of the skill asset")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("validate", help="check a generated 3MF")
    p.add_argument("file")
    p.add_argument("--bed", type=float)
    p.add_argument("--depth", type=float)
    p.add_argument("--slice", action="store_true")
    p.set_defaults(func=cmd_validate)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command == "build" and not args.set and not args.bom:
        ap.error("build needs a set number or --bom")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
