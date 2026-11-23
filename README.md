# brickprint

Turn a LEGO set number into a color-grouped, multi-plate Bambu Studio project.

This skill was built and validated on LEGO **10337-1 Lamborghini Countach**:
1,506 parts, 387 Rebrickable lots, 19 colors, **about 33-36 A1-mini plates** (depending on
support plates and packing), all of which slice. It has also been exercised on a small second set (30314-1 Go-Kart
Racer) to verify the generic path.

## Requirements

- **macOS.** The default paths (`/Applications/BambuStudio.app`,
  `/Applications/LDView.app`, `~/Library/Caches/brickprint`) and the launcher
  (`bin/brickprint` looks for Blender under `/Applications`) assume macOS. The
  code also accepts other paths through the config file or environment
  variables, but only macOS has been exercised.
- **Bambu Studio**, installed. Its command-line interface is used for the
  slicing checks (floating-part probe, `--bambu` normalisation, `verify`).
  Without it a build still runs but prints a warning that floating detection is
  unverified.
- **LDView** and an **LDraw parts library**. Neither is bundled; see
  [Data sources and attribution](#data-sources-and-attribution). `brickprint
  bootstrap` can download the LDraw library; LDView you install yourself.
- **Python 3.11+ with numpy.** The launcher prefers Blender's bundled Python
  and falls back to `python3`; `BRICKPRINT_PYTHON` overrides it. See
  [Which Python runs it](#which-python-runs-it).

## Install

From a clone of this repository:

```bash
./install.sh          # copy the skill into ~/.claude/skills/brickprint and ~/.codex/skills/brickprint
./install.sh --link   # symlink instead of copying (development)
```

The installer backs up any existing copy under `~/Library/Caches/brickprint/backups`
and writes `~/.config/brickprint/config.json` if it does not exist yet (an
existing config is kept). The config holds `cache_dir`, `ldraw_dir`, `ldview`
and `bambu`; `ldraw_dir` and `ldview` are written empty unless the installer
finds them two folders above the skill, in `ldraw_library/ldraw` and
`tools/LDView.app` (a layout of the author's own workspace, so normally they
stay empty). brickprint then auto-detects them at run time (for example
`/Applications/LDView.app`; at run time it also checks every parent folder of the
skill for that `ldraw_library/ldraw` layout). You can also run
`bin/brickprint` straight from the clone without installing.

For development, `pip install -e ".[dev]"` installs a `brickprint` console script plus pytest and ruff; it must stay an editable/source-checkout install, because the bundled `assets/` live outside the package directory and a regular (non-editable) wheel would not contain them.

Then check the setup and fix what is missing:

```bash
bin/brickprint doctor      # from the clone; shows resolved paths, Python/numpy, errors
bin/brickprint bootstrap   # downloads the Rebrickable CSV dumps and, if no LDraw library is configured, the LDraw library
```

`bootstrap` does not install LDView or Bambu Studio: install those yourself and
run `doctor` again. To point brickprint at things in other places, edit
`~/.config/brickprint/config.json` (keys `ldraw_dir`, `ldview`, `bambu`,
`cache_dir`) or set environment variables, which take precedence over the config:

```bash
export BRICKPRINT_LDRAW=/path/to/ldraw          # folder with LDConfig.ldr, parts/, p/
export BRICKPRINT_LDVIEW=/path/to/LDView        # the executable, e.g. .../LDView.app/Contents/MacOS/LDView
export BRICKPRINT_BAMBU=/path/to/BambuStudio    # e.g. .../BambuStudio.app/Contents/MacOS/BambuStudio
```

## Use

```bash
brickprint doctor     # or bin/brickprint ... from a clone
brickprint inventory 10337-1
brickprint build 10337-1 --bambu
brickprint validate *_parts_by_color.3mf
brickprint verify *_parts_by_color.3mf

# LEGO-clutch PETG (see SKILL.md for the full Seagull command; its input files are examples and are not in this repo)
brickprint calibrate fit --profile a1mini-petg-008-lego-v8 --filament porima-petg-white --out fit_cal.3mf --verify
brickprint build SET --profile a1mini-petg-008-lego-v8 --filament porima-petg-white --fit 0.03 --fit-test --verify
```

If the skill is installed (`brickprint` is not put on your `PATH`; call the launcher by path or add a shell alias):

```bash
~/.codex/skills/brickprint/bin/brickprint doctor   # Codex
~/.claude/skills/brickprint/bin/brickprint doctor  # Claude
```

### Which Python runs it

brickprint needs Python 3.11+ **with numpy**. `bin/brickprint` picks, in order:

1. `$BRICKPRINT_PYTHON`, if set;
2. Blender 5.2's bundled `python3.13`
   (`/Applications/Blender.app/Contents/Resources/5.2/python/bin/python3.13`);
3. Blender 5.1's bundled `python3.11`;
4. plain `python3`.

The Blender interpreters are used because they ship numpy; a system or
Homebrew `python3` often has none. That dependency is silent: if Blender is
moved, upgraded to another version directory or removed, the launcher falls
back to `python3`, and every command stops with
`ERROR: numpy is not importable by this Python`. Fix it with
`export BRICKPRINT_PYTHON=/path/to/python-with-numpy` (for example a venv with
`pip install numpy`). `brickprint doctor` prints the interpreter in use
(`python bin`) and the numpy version. You can also run the package directly:

```bash
PYTHONPATH=. /Applications/Blender.app/Contents/Resources/5.2/python/bin/python3.13 -m brickprint doctor
```

### Disk use of work dirs

`verify` keeps its G-code in a `brickprint_verify_*` temp dir (the path is in
the report; often 0.1-2.6 GB each), and an interrupted probe / plate check used
to leave `probe_*` / `platecheck_*` / `final_*` dirs in the cache. `build` and
`verify` now sweep these at start: dirs older than `sweep_days` (config, default
3) go, then the oldest `brickprint_verify_*` dirs until they total at most
`verify_keep_gb` (default 2). A dir with anything modified in the last hour is
never touched (concurrent builds), nor a `--workdir` you passed. One line
reports what was freed. Set `BRICKPRINT_NO_SWEEP=1` to turn it off.

## What it generates

- `<set>_<name>_parts_by_color.3mf` — one Bambu object per plate, one component
  per part, one extruder per color, plate names `"<color> i of n"`.
- `<set>_<name>_parts_by_color_plates.csv` — per-plate color, part count, size,
  max Z, filament.
- `<set>_<name>_parts_by_color_missing.csv` — lots with no LDraw geometry.
- `<set>_<name>_parts_by_color_report.json` — completeness and validation.

The raw project (`_raw.3mf`) is written first. If Bambu Studio is installed,
`--bambu` re-exports it to add plate thumbnails and Bambu's normalised
metadata. This works with the Bambu Studio GUI open. The raw file is still a valid 3MF; it is expected to
open in Bambu Studio (confirm with the manual GUI check in SKILL.md).

## Pipeline

1. **Rebrickable** — the public CSV dumps provide the set inventory, part names,
   color names/RGB, spares, minifigs, and part relationships. No API key.
2. **LDraw** — each Rebrickable part number resolves to a `.dat` file (exact,
   print-stripped, parent relationship, or mold variant). Each part is exported
   with LDView using `-Seams=0`, so dimensions are exact LDraw millimetres.
3. **Orientation** — each part is evaluated in 24 axis-aligned poses. The
   assembled (studs-up) pose is kept unless it has floating/empty slicer layers
   or stands on an edge. Parts that still need support are put on separate
   `<Color> supports i of n` plates with tree supports enabled only there.
4. **Packing** — parts are grouped by color and packed with MaxRects into
   180 × 180 mm beds (6 mm margin, 1 mm gap).
5. **3MF** — one model file per plate, globally unique object IDs, one component
   per part. Build items sit on Bambu's plate grid (`stride = bed × 1.2`,
   `cols = ceil(sqrt(plate_count))`, rows toward negative Y), which is what makes
   Bambu assign the right geometry to the right plate.
6. **Print tuning** — optional profiles (`--profile`, `--filament`, `--fit`,
   `--supports enforcers`, `--bridging short-span`); see
   `references/print-tuning.md`.
7. **Bambu normalisation** — optional CLI `--export-3mf` pass for thumbnails and
   project metadata.
8. **verify** — slice with the CLI and check effective settings, stud/socket
   toolpaths, supports and floating.

See `references/pipeline.md` for details.

## Known limitations

- LDraw coverage is not universal. Missing parts are reported, not invented.
- Printed parts are simplified to the base mold when possible.
- Minifigs are not expanded unless `--include-minifigs`; leg/torso assemblies
  often have no LDraw equivalent.
- Parts larger than the usable bed are skipped and reported. Use a larger bed or
  split the project.
- One color per plate; very multicolored sets can produce many sparse plates.
- LDraw footprints are 8·n mm (real LEGO 8·n − 0.2), and contour compensation
  grows them further. Neighbouring bricks can rub; `verify` reports the
  outer sizes. Mesh-level footprint relief is deliberately not implemented.
- Do not use mesh repair tools that fill all holes: LEGO undersides are open by
  design and filling them blocks the stud cavities.

## Layout

```
brickprint/
  SKILL.md                 agent instructions
  README.md
  install.sh
  agents/openai.yaml
  bin/brickprint           launcher
  brickprint/
    cli.py                 subcommands
    config.py              paths, doctor
    rebrickable.py         CSV download + inventory
    ldraw.py               LDraw resolution + LDView mesh cache
    pack.py                MaxRects packing
    threemf.py             3MF writer + validator
    settings.py            print profiles -> Bambu project settings
    bambu_profiles.py      Bambu stock preset chain resolution
    features.py            enforcer volumes, bridge angles, stud rings
    quality.py             per-part mesh quality report
    gcode.py               G-code config/toolpath reader
    verify.py              slice + check effective settings and fit
    probe.py, orient.py, repair.py   floating probe, orientation, mesh repair
    net.py                 urllib/curl/wget download helper
  assets/
    a1mini_pla_project_settings.json   default profile snapshot
    profiles/process/*.json            process profiles
    profiles/filament/*.json           filament (spool) profiles
  references/
```

## Data sources and attribution

The first three items are fetched or installed at run time and are not in this
repository; the last two are bundled. Details are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

- **Rebrickable inventories** (set parts, colours, relationships) are the public
  CSV dumps from <https://rebrickable.com/downloads/>, downloaded on first use
  (`bootstrap`, `inventory` or `build`). No API key. Respect Rebrickable's terms;
  attribution is required for public use.
- **LDraw parts library** (<https://library.ldraw.org/>) is downloaded by
  `brickprint bootstrap` (or you point brickprint at your own copy). The library
  is CC BY 4.0; keep its `CAreadme.txt` and attribution files with it.
- **LDView** is installed separately and used for mesh export.
- **Filament profiles** under `assets/profiles/filament/` use the temperature,
  fan and drying ranges published by Porima; each JSON file carries the product
  URL and retrieval date. The flow values are the author's own tuning.
- **Default profile** `assets/a1mini_pla_project_settings.json` is a snapshot of
  Bambu Studio's stock A1 mini presets (Bambu Studio 02.09.01.70), derived from
  the Bambu Studio repository (<https://github.com/bambulab/BambuStudio>,
  AGPL-3.0).

brickprint is not affiliated with or endorsed by the LEGO Group, Rebrickable,
LDraw.org, Bambu Lab or Porima. LEGO is a trademark of the LEGO Group.

## License

brickprint's code, tests and documentation are released under the
[MIT License](LICENSE).

One bundled file is not MIT: `assets/a1mini_pla_project_settings.json` is
derived from Bambu Studio's stock presets and is licensed under the
[GNU AGPL v3.0](LICENSES/AGPL-3.0.txt). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
