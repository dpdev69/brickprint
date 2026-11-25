# brickprint

[![CI](https://github.com/dpdev69/brickprint/actions/workflows/ci.yml/badge.svg)](https://github.com/dpdev69/brickprint/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg)](pyproject.toml)

Turn a LEGO set number or local MOC BOM CSV into a color-grouped, multi-plate Bambu Studio `.3mf` — oriented, bed-packed, fit-tuned, and slice-verified from the command line.

## Features

- **Inventory & exact meshes** — resolves set parts from Rebrickable CSV dumps (no API key) or a local BOM CSV and exports exact-millimetre LDraw geometry via LDView.
- **Automated orientation & supports** — evaluates 24 axis-aligned poses, separates parts that need support onto dedicated plates, and keeps underside stud sockets clear with blockers and enforcers.
- **Color-grouped bed packing** — packs parts with MaxRects for **A1 mini**, **A1**, **P1S**, and **X1C** presets (`--mix-colours` stays within Bambu's 36-plate grid).
- **Clutch-fit & PETG tuning** — bundles calibrated 0.08 mm PETG process/filament profiles, per-object X-Y calibration (`calibrate fit`), small-contact/slender brims, and anchored socket ceilings.
- **Headless verification** — slices via the Bambu Studio CLI to verify effective settings, stud/socket dimensions, supports, and floating regions (works while Bambu's GUI is open).

## Quick Start

```bash
git clone https://github.com/dpdev69/brickprint.git
cd brickprint
./install.sh

bin/brickprint doctor      # verify Python/numpy, LDraw, LDView, and Bambu Studio
bin/brickprint bootstrap   # download Rebrickable CSVs and the LDraw library
bin/brickprint build 10337-1 --bambu --verify
```

`./install.sh` installs the agent skill for **Antigravity**, **Claude Code**, and **Codex** (use `./install.sh --link` for development, or `pip install -e ".[dev]"` for a `brickprint` console script).

## Common Commands

```bash
# Inspect a set inventory (parts, colors, missing LDraw geometry)
bin/brickprint inventory 10337-1

# Stock PLA build for A1 mini (default) or 256 mm printers (a1, p1s, x1c)
bin/brickprint build 10337-1 --bambu --verify
bin/brickprint build 10337-1 --preset x1c --bambu --verify

# Build from a local MOC BOM CSV
bin/brickprint build --bom my_moc.csv --name "My MOC" --bambu --verify

# Calibrate and build tuned 0.08 mm PETG with LEGO clutch fit (A1 mini)
bin/brickprint calibrate fit --profile a1mini-petg-008-lego-v8 \
  --filament porima-petg-white --out fit_cal.3mf --bambu --verify
bin/brickprint build 10337-1 --profile a1mini-petg-008-lego-v8 \
  --filament White=porima-petg-white --fit 0.03 --fit-test --bambu --verify

# Validate bounding boxes or run full CLI slice verification on any .3mf
bin/brickprint validate *_parts_by_color.3mf
bin/brickprint verify *_parts_by_color.3mf
```

## Outputs

Each `build` writes a ready-to-slice Bambu Studio project plus companion diagnostics:

- `*_parts_by_color.3mf` — color-grouped multi-plate project (normalised with plate thumbnails when `--bambu` is passed)
- `*_plates.csv` — per-plate color, part count, bed footprint, max Z, filament, and support flag
- `*_report.json` & `*_missing.csv` — completeness summary, orientation warnings, and any parts lacking LDraw geometry

## Requirements

- **macOS or Linux** — macOS is the primary target; Linux paths are supported and CI-tested.
- **Python 3.9+ with `numpy`** — the launcher checks `$BRICKPRINT_PYTHON`, a local `.venv`, Blender's bundled Python, then `python3`.
- **LDView & LDraw parts library** — `bootstrap` downloads LDraw; install LDView separately.
- **Bambu Studio** — required for `--bambu`, `--verify`, floating checks, and non-A1-mini `--preset` templates (optional for raw A1 mini builds).

Custom tool paths and defaults can be set in `~/.config/brickprint/config.json` or via `BRICKPRINT_LDRAW`, `BRICKPRINT_LDVIEW`, `BRICKPRINT_BAMBU`, and `BRICKPRINT_PRESET`.

## Documentation

- [SKILL.md](SKILL.md) — agent skill instructions, CLI reference, and print-safety rules
- [references/pipeline.md](references/pipeline.md) — inventory, LDraw resolution, orientation, probing, and MaxRects packing
- [references/print-tuning.md](references/print-tuning.md) — PETG profiles, clutch-fit calibration, supports, bridging, and socket ceilings
- [references/bambu-3mf.md](references/bambu-3mf.md) — 3MF plate layout, XML metadata, and CLI normalisation
- [references/data-sources.md](references/data-sources.md) — Rebrickable, LDraw, LDView, and Bambu Studio sources
- [references/troubleshooting.md](references/troubleshooting.md) — `doctor` diagnostics, Python/numpy setup, disk sweep, and common slicer warnings

## License & Attribution

- Code, tests, and documentation: [MIT License](LICENSE).
- `assets/a1mini_pla_project_settings.json` (resolved from Bambu Studio stock presets): [GNU AGPL v3.0](LICENSES/AGPL-3.0.txt).
- Full third-party attributions (Rebrickable CSV dumps, CC BY 4.0 LDraw library, Porima filament data, Bambu Studio): [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

*brickprint is not affiliated with or endorsed by the LEGO Group, Rebrickable, LDraw.org, Bambu Lab, or Porima. LEGO is a trademark of the LEGO Group.*
