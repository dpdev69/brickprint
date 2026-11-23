---
name: brickprint
description: Turn a LEGO set number into a color-grouped, multi-plate Bambu Studio 3MF. Use when the user gives a set number (for example 10337-1) and wants all parts found, laid out on print beds by exact color, and exported as one .3mf file.
---

# Brickprint

Build a printable parts kit from a LEGO set number:

```
set number
  -> Rebrickable inventory (parts + exact colors)
  -> LDraw geometry (exact LDraw dimensions)
  -> MaxRects packing by color
  -> one Bambu object per plate, one component per part
  -> .3mf with plate names, colors, and optional thumbnails
```

The reference builds are **10337-1 Lamborghini Countach** (1,506 parts, 19 colours, about 33-36 A1-mini plates depending on support plates, all slice) with the default PLA profile, and **MMMB027-1 Seagull** in Porima PETG at 0.08 mm with LEGO clutch fit (`a1mini-petg-008-lego`).

## Quick start

```bash
brickprint doctor
brickprint inventory 10337-1
brickprint build 10337-1 --bambu
brickprint validate 10337-1_lamborghini_countach_5000_quattrovalvole_parts_by_color.3mf
brickprint verify 10337-1_lamborghini_countach_5000_quattrovalvole_parts_by_color.3mf   # slices with the CLI
```

Tuned (PETG, clutch fit, test plate, verified against the hand-built v4). This is the exact command that built `seagull/MMMB027-1_seagull_PETG_008mm_v5_brickprint_fit030.3mf`:

```bash
brickprint build MMMB027-1 --bom seagull/MMMB027-1_seagull_white_orange_bom.csv \
  --name "Seagull (White + Orange)" --profile a1mini-petg-008-lego-v8 \
  --filament White=porima-petg-white --filament Orange=porima-petg-neon-orange \
  --fit 0.03 --fit-test 41769,3022,4733,6541 --bambu --verify \
  --reference seagull/MMMB027-1_seagull_PETG_008mm_v4_fit030.3mf \
  --out seagull/MMMB027-1_seagull_PETG_008mm_v5_brickprint_fit030.3mf
```

The launcher picks the Blender Python (it has numpy) when available. If this skill is installed globally, use the launcher next to this SKILL.md:

```bash
~/.codex/skills/brickprint/bin/brickprint doctor   # Codex
~/.claude/skills/brickprint/bin/brickprint doctor  # Claude
```

## Bambu Oracle MCP (optional companion)

`bambu-oracle` is an optional companion MCP server that is **not part of this
repository** and is distributed separately; it may not be available in your
setup. Everything in this skill works without it through the `brickprint` CLI
alone (`build`, `validate`, `verify`, `profiles`, ...). If your agent has the
`bambu-oracle` tools, they expose the installed Bambu Studio CLI as structured
tools, so after a build you can inspect/slice/preview/verify the result without
writing throwaway Python or parsing CLI text.

When the tools are available, use them in this order:

1. `bambu_project path=<file>.3mf` — plates, part counts, colours, filament
   slots, embedded thumbnails, sibling `_plates.csv` / `_report.json` /
   `_verify.json`, and warnings.
2. `bambu_slice path=<file>.3mf plates=all render=true` — authoritative Bambu
   slice: per-plate print time, filament grams, layers, warnings, G-code paths,
   and PNG previews. Cached by source SHA-256.
3. `bambu_preview path=<file>.3mf` — plate PNGs (embedded thumbnails instantly;
   renders if missing).
4. `bambu_verify path=<file>.3mf` — runs this skill's `verify` and returns a
   compact pass/fail summary plus the full report path. Do this before saying a
   project is ready to print.

Tuning helpers:

- `bambu_profiles action=search query=thick_bridges` — find which Bambu stock
  preset and which brickprint recipe define a setting.
- `bambu_profiles action=show name=a1mini-petg-008-lego-v8
  keys=["layer_height","wall_loops","thick_bridges"]` — resolved values,
  including `base_process` and `overrides` conventions.
- `bambu_profiles action=effective path=<file>.3mf` — flattened settings Bambu
  will actually use, including changes from the project.
- `bambu_export path=<raw>.3mf` — the `--bambu` thumbnail/metadata pass with
  Bambu's absolute-path quirk handled.
- `bambu_gcode path=<file>.gcode` — header stats and effective config block.
- `bambu_re topic=cli|result|3mf|warnings|profiles` — the reverse-engineered
  contract (CLI flags, `result.json`, 3MF metadata, warning strings).

`brickprint build` remains the only step that creates a project; the Oracle is
read-only except for its own cache, `bambu_export`, and `bambu_slice` G-code.
See `references/bambu-oracle.md` for the tool reference and examples (only relevant if you have the companion server). Without it, use `brickprint verify` for print-readiness.

## Prerequisites

`brickprint doctor` reports all of these:

- **Python 3.11+ with numpy** — the launcher uses Blender's bundled Python when present; override with `BRICKPRINT_PYTHON`.
- **LDraw library** — a folder containing `LDConfig.ldr`, `parts/` and `p/`. Point brickprint at it with `ldraw_dir` in `~/.config/brickprint/config.json` or `BRICKPRINT_LDRAW` (the environment variable wins), or run `brickprint bootstrap` to download the official `complete.zip` into the cache and record it as `ldraw_dir`. If nothing is configured, brickprint also looks in `~/Library/Application Support/LDraw/ldraw`, `/Applications/LDraw/ldraw`, `~/ldraw` and the cache folder, and in an `ldraw_library/ldraw` folder of a parent directory of the skill if one exists.
- **LDView** — install LDView. It is auto-detected at `/Applications/LDView.app/Contents/MacOS/LDView` or as `LDView` / `ldview` on `PATH` (also in a `tools/LDView.app` folder of a parent directory of the skill, if one exists); otherwise set `ldview` in `~/.config/brickprint/config.json` or `BRICKPRINT_LDVIEW` to the executable. Uncached parts are exported, repaired and pose-evaluated in parallel (`"mesh_workers"` in the config, default min(8, cpus − 2); 1 = serial).
- **Bambu Studio** — **needed for reliable orientation and for `verify`.** `build` slices every unique part alone in its 6 up-directions with the Bambu Studio CLI (the floating probe) and trusts Bambu's own "floating regions / floating cantilever / empty layer" verdicts over the numpy emulator. Path: `"bambu"` in `~/.config/brickprint/config.json` or `BRICKPRINT_BAMBU`, default `/Applications/BambuStudio.app/Contents/MacOS/BambuStudio`. The probe, `--bambu` normalisation and `verify` all work **while the Bambu Studio GUI is open** (CLI-verified). Do not ask the user to close it. Without Bambu Studio the build still runs, but prints a loud "floating detection is UNVERIFIED" warning: the emulator alone misses dozens of floating poses per set.
- **Settings template / profiles** — `assets/a1mini_pla_project_settings.json` is the default profile's snapshot of Bambu's stock preset chain. Regenerate it with `brickprint profiles regen-template` after a Bambu Studio update, and never hand-edit it (a stripped template drops the start G-code and the printer prints in the air). Other profiles (`assets/profiles/`, `~/.config/brickprint/profiles/`) are resolved live from the installed Bambu Studio. See `references/print-tuning.md`.
- **Rebrickable CSV dumps** — downloaded automatically on first use (about 18 MB compressed).

## Commands

### `inventory`

```bash
brickprint inventory 10337-1
brickprint inventory 30314-1 --include-minifigs
brickprint inventory 10337-1 --out my_bom.csv
```

Writes a BOM CSV and reports lots, parts, colors, minifigs, and missing LDraw parts. `--include-spares` includes spare lots; `--include-minifigs` expands minifig inventories.

### `build`

```bash
# normal, A1 mini
brickprint build 10337-1 --bambu

# no thumbnail normalisation (raw 3MF is still valid; expected to open in
# Bambu Studio); the floating probe still runs
brickprint build 10337-1 --no-bambu

# emulator only, no Bambu Studio probe (prints an UNVERIFIED warning;
# do not print supports-off plates from this without checking them)
brickprint build 10337-1 --no-bambu --no-probe

# another Bambu printer (bed size + its stock Bambu project settings)
brickprint build 10337-1 --preset x1c

# custom bed size only (keeps the A1 mini settings template)
brickprint build 10337-1 --bed 256 --depth 256 --no-bambu

# local BOM instead of Rebrickable
brickprint build --bom my_bom.csv --name "My MOC" --no-bambu
```

Outputs next to the requested `.3mf`:

| file | contents |
|---|---|
| `<name>.3mf` | final project (normalised if `--bambu` ran) |
| `<name>_raw.3mf` | pre-Bambu project |
| `<name>_plates.csv` | plate, color, parts, size, max Z, filament, support flag (mixed plates: `;`-separated per colour, with part counts) |
| `<name>_missing.csv` | lots with no LDraw geometry |
| `<name>_report.json` | counts, missing/oversized, orientation warnings, validation, timings |

Options:

- `--preset a1mini|a1|p1s|x1c` — printer preset (default `a1mini`; also `"preset"` in `~/.config/brickprint/config.json` or `BRICKPRINT_PRESET`; `--preset` > `BRICKPRINT_PRESET` > config). Sets the bed (A1 mini 180×180, A1 / P1S / X1C 256×256 mm), the stock filament (`Bambu PLA Basic @BBL …`) and the project-settings template: the A1 mini uses the bundled snapshot; the others are derived from the installed Bambu Studio's stock 0.4 mm machine + `0.20mm Standard` + PLA Basic presets (cached per Bambu version; the build stops with a clear error when Bambu Studio is missing). P1S / X1C layouts keep clear of the 18×28 mm front-left exclusion zone. Values set explicitly in the config (`bed`, `depth`, `settings`, `filament_settings_id`, …), `BRICKPRINT_SETTINGS` and flags such as `--bed` still win. The bundled `a1mini-petg-*` profiles and `porima-*` filament profiles are A1 mini presets; with another `--preset` the build refuses them (use the default stock profile). Also on `calibrate`, `validate`, `profiles` and `doctor`.
- `--bed/--depth/--margin/--gap` — bed geometry (default from the preset: 180/180/6/1 mm for the A1 mini).
- `--strict` — fail instead of continuing when an LDraw part is missing.
- `--orient file.json` — `{"70490.dat": "Rx"}`; hard override for the built-in flat-lay list. The orientation engine only revisits an override when that pose is unprintable (floating/empty layer/edge contact).
  Labels are case-insensitive (`id`, `Rx`, `Rx2`, `Ry`, `Ry2`); an unknown label is an error.
  A trailing `!` (`{"93274.dat": "Rx2!"}`) forces the pose even when it floats; the part then goes to the support plates. Use it when the engine picks a pose that puts studs face down on supports (Countach bracket 93274).
- `--no-probe` — skip the Bambu Studio floating probe (emulator only; floating detection unverified). The probe is also skipped, with the same warning, when Bambu Studio is not found.
- `--include-spares`, `--include-minifigs`, `--refresh`.
- `--part-map FILE` — JSON `{"rebrickable part": "ldraw part"}`, on top of `~/.config/brickprint/part_overrides.json`.
- `--bambu` — re-export with the Bambu Studio CLI (adds plate thumbnails). Works with the GUI open. Always the last step, so thumbnails are never stale.
- `--mix-colours auto|always|off` — let tiny colour groups share "Mixed" plates (a colour's last plate at most 35 % full, at most 4 colours per plate; see "How to read the result"). `auto` (default) mixes only when the project would otherwise exceed 36 plates (counting a `--fit-test` plate), so it never changes a project that already fits; `always` mixes every tiny group; `off` keeps one colour per plate (supports are then folded as before).

Print tuning (all optional; without them the output is byte-identical to the stock PLA behaviour):

- `--profile NAME` — process profile (`brickprint profiles list`). Default `a1mini-pla-stock`. The recommended LEGO PETG recipe is `a1mini-petg-008-lego-v8` (adds the socket-ceiling recipe); `a1mini-petg-008-lego` is the earlier recipe without it.
- `--filament COLOUR=PROFILE` — filament profile per colour (colour name, color_id or hex). `--filament PROFILE` applies it to every slot. Repeatable. A spec that matches no colour is an error.
- `--fit C[,H]` — X-Y contour compensation +C and hole compensation H (default −C). This is what gives clutch: 0.03 ≈ +0.12 mm and 0.05 ≈ +0.20 mm stud→socket interference.
- `--fit-test [PARTS]` — append a "Fit test" plate of separate test pieces (e.g. `41769,3022,4733,6541`).
- `--supports enforcers|plates` — enforcers: support only in generated volumes under cantilevered features (side studs), never in sockets. plates: `<Colour> supports` plates.
- `--bridging short-span|stock` — bridge cavity ceilings across the short side.
- `--ceilings recipe8|off` — socket-ceiling recipe (on in `a1mini-petg-008-lego-v8`): plate sockets raised to 2.2 mm, the ceiling printed as two 0.24 mm layers with thick bridges and 2 wall loops on the bridge layer, plus 0.8 mm ribs where a bridge span exceeds 7 mm. Every part becomes its own Bambu object (height ranges are per object), and parts with support enforcers are packed with extra spacing. Needs `thick_bridges=1`; never set that without this recipe, or strands block the studs.
- `--set KEY=VALUE` — any Bambu process key. It is shaped and listed in `different_settings_to_system` automatically. The value kind (whole number, number, percent) is checked against the stock value; a wrong kind such as `wall_loops=4.5` is rejected with an error. Enums and free strings are accepted as-is. `--fit` / `calibrate --values` contour must be 0 to 0.3 mm.
- `--support-set KEY=VALUE` — extra per-object Bambu setting written only on objects with their own tree supports (`<Colour> supports` plates, auto-supported fit-test pieces), e.g. `support_top_z_distance=0.64`. Repeatable; overrides a profile's `supports.object_settings` per key. `enable_support` / `support_type` / `bridge_no_support` are fixed and rejected.
- `--allow-shrink` — accept `filament_shrink` > 100 % (otherwise an error: it scales parts down).
- `--verify [--reference REF.3mf]` — slice the result and run `verify`.

### `calibrate fit`

```bash
brickprint calibrate fit --profile a1mini-petg-008-lego-v8 --filament porima-petg-white \
  --values 0.02,0.03,0.04,0.05 --parts 3022,3005,3024,4733 --out fit_cal.3mf --verify
```

Builds one row per value (ascending). The printed pieces carry no marking, so the console prints the mapping: rows run from the FRONT of the plate (y min, toward you at the printer) to the BACK in ascending contour value (front row = smallest value), parts left to right in `--parts` order; further plates continue the sequence. Each object in Bambu Studio's object list is named `fit <contour> <part>`. Values must be 0 to 0.3 mm. `--verify` prints one line per row/part (stud Ø, socket). Every piece is its own object with per-object X-Y compensation; Bambu ignores it per part, so the pieces must be separate objects. Workflow: calibrate → print → try the pieces against each other and against real LEGO → `build --fit <value>`.

### `verify`

```bash
brickprint verify project.3mf [--plates 1,3] [--reference ref.3mf] [--profile P --fit C]
```

Slices with the Bambu Studio CLI and checks:

- the effective settings in the G-code config block, against the project and against the intended profile/report values;
- stud Ø, socket/cavity gap, outer size and tube OD per part from the outer-wall toolpaths;
- that supports stay near enforcers (or, for objects with their own supports, near that object) and that no support toolpath is enclosed by a part's walls (socket, tube, hole), read from the G-code alone. Enclosed support is an error, except on a default-profile build (sibling `_report.json` without a `settings` block): that profile writes no socket blockers, so it is a warning there; build with `--profile` to get blockers. Without a sibling report it stays an error;
- that a supports-off slice (no enforcers) shows no floating object without its own supports. When the file already has global supports off and no enforcers (default-profile builds) its own slice is that supports-off slice, so verify slices once instead of twice (`float_check.slice = "new"` in the report); otherwise the supports-off copy is sliced at the same time as the file (not when `probe_workers` is 1). Bambu's floating warning is not deterministic: a marginal plate can be flagged in some runs only (40333 plate 5 is a known intermittent case), so re-run verify before treating a single floating error as a new regression;
- a plate Bambu cannot slice (path conflict, G-code outside the bed) is reported with Bambu's error, and the plates after it are still sliced and checked;
- whether thumbnails are stale.

The JSON report is written to `<file>_verify.json` next to the input, or into `--workdir` when one is given; `--report PATH` sets it explicitly. Without `--workdir` the G-code goes to a kept `brickprint_verify_*` temp dir; `build` and `verify` sweep old ones at start (older than `sweep_days`, default 3, then oldest first above `verify_keep_gb`, default 2 GB; never a dir touched in the last hour, never a `--workdir`; `BRICKPRINT_NO_SWEEP=1` turns it off).

Run it before telling the user a project is ready to print.

### `quality`

`brickprint quality 4733 3022` or `brickprint quality --bom bom.csv` prints a per-part mesh report: LDView export variant (48-gon or 16-gon fallback), open/non-manifold edges, stud segments and Ø, footprint on the 8 mm grid. Profile builds write it to `<name>_quality.csv`.

### `profiles`

- `list`
- `show NAME [--resolved]`
- `check`: read-only. Compares the installed Bambu user presets with the profiles and reports stale ones, such as the 100.6 % shrink Porima presets.
- `export NAME --to DIR`: writes an importable preset. brickprint **never writes Bambu Studio's preset folders**.
- `regen-template`

### `validate`

```bash
brickprint validate project.3mf
```

Checks every plate's world-space bounding box against the bed and reports plate/component/triangle counts. It does not slice; use `verify`.

### `bootstrap`

```bash
brickprint bootstrap
```

Downloads the Rebrickable CSV dumps and, if no LDraw library is configured, downloads and extracts the LDraw `complete.zip` into the cache and writes its path to `ldraw_dir` in `~/.config/brickprint/config.json`. It does not install LDView or Bambu Studio; run `brickprint doctor` afterwards to see what is still missing.

### `doctor`

Shows the resolved paths, the active printer preset (`--preset` to check another one), the Python interpreter in use (`python bin`) and its numpy version, and fails loudly if the LDraw library, LDView, numpy, or settings template is missing.

## How to read the result

- A plate name is `"<color> <i> of <n>"`; every part on that plate is the same color.
- A `"Mixed <i> of <n> (<color>, <color>, ...)"` plate holds a few tiny colour groups (only when `--mix-colours` mixed them): each colour is one contiguous block, at most 4 colours (the AMS lite slots), each colour its own object with its own filament (profile builds: one object per part, each with its colour's filament). When the profile prints a prime tower (the default PLA profile does), a 50 x 50 mm back-right corner of every mixed plate is kept free and the tower is placed there. The PETG profiles print no prime tower; colour changes purge into the chute. `"Mixed supports ..."` plates do the same for support parts. `_plates.csv` lists the colours, color_ids, extruders and filament hexes `;`-separated in the same order, e.g. `Red (3); Tan (2)`.
- The `.3mf` is one Bambu object per plate, with every part as a separate component. That keeps the layout editable and lets you hide/select individual parts.
- `_report.json` is the source of truth for completeness:
  - `missing_lots == 0` and `oversized_lots == 0` means every requested part was placed.
  - `missing_lots > 0` means the set contains part/color combinations that have no LDraw file; the printed set is incomplete.
  - `oversized_lots > 0` means parts do not fit on the selected bed; use a larger `--bed/--depth` or split the project.
  - `orientation_warnings` lists parts that still have small bed contact after auto-orientation; review those manually.

### Orientation and supports

- Every part is evaluated in the 24 proper axis-aligned poses (6 up
  directions). The assembled (studs-up) pose is kept unless it has an empty
  slicer layer, floating regions / a floating cantilever, or stands on an
  edge. Built-in tyre/rim/bar overrides are respected unless they are
  unprintable.
- Floating / cantilever / empty-layer status comes from **Bambu Studio
  itself**: each pose is sliced alone on a one-part plate (many plates per
  probe project, several projects in parallel). Verdicts are cached per part
  next to the mesh cache, keyed by mesh hash + repair/orient/probe versions,
  so re-runs are instant. Because Bambu's floating check is not
  deterministic, a pose is clean only if clean in 3 runs.
- Profile builds then slice every part type **as built** (raised ceilings,
  ribs, bands, per-part settings) alone in 3 runs (cached). Ceiling ribs that
  make a part float are dropped; a part type that still floats goes to the
  support plates.
- After packing, the whole project is sliced twice. Bambu names every
  floating object. Profile builds (one object per part) move the named part
  types to the support plates and re-pack, up to 4 rounds. The default
  profile (one object per plate) enables supports on the flagged plate
  (`slice-check` in `_plates.csv`).
  `_report.json` → `probe` / `plate_check` have the counts and time;
  `floating_verified` is false when the probe was skipped or some parts could
  not be probed.
- Parts that Bambu flags as floating in every pose are packed onto separate
  `"<Color> supports i of n"` plates. Those plate objects get per-object
  `tree(auto)` + `bridge_no_support=1`. In profile builds every part there
  also gets support blockers over its enclosed cavities, because tree
  supports otherwise fill the sockets (seen in the Countach G-code).
- **Never switch a LEGO project to automatic supports globally**: auto supports
  fill the underside sockets. With `--supports enforcers` (PETG profile) the
  global setting is `normal(manual)` and support grows only inside generated
  enforcer volumes under cantilevered features such as 4733's side studs.
- The probe, plate check and `verify` always slice a **supports-off copy**
  without enforcer volumes: Bambu stops reporting floating regions once
  supports are enabled, and with supports off it reports an enforced overhang
  as floating.
- If the project would exceed 36 plates (35 with `--fit-test`), tiny colour
  groups are mixed first (`--mix-colours auto`): tiny support groups onto
  `Mixed supports` plates, then, if still needed, tiny normal groups onto
  `Mixed` plates. Support parts are never mixed with normal parts.
- Only if the project still exceeds the limit (or with `--mix-colours off`)
  are the smallest support groups folded into their color's last normal plate,
  which then gets supports enabled as a whole (auto supports can fill LEGO
  sockets, so mixing is preferred). `_plates.csv` shows `plate`, `folded` or
  `slice-check` in the `supports` column.
- `_report.json` → `orientation_warnings` is the manual-review list.

## Printing notes

- Default profile (`a1mini-pla-stock`): Bambu's stock presets, unmodified: printer `Bambu Lab A1 mini 0.4 nozzle`, filament `Bambu PLA Basic @BBL A1M` (220 °C), process `0.20mm Standard @BBL A1M` (auto brim, supports off as in stock). The only deviation is the Textured PEI Plate bed type. The full A1 mini start G-code (AMS lite load, bed leveling, Z offset, flow calibration, purge line) is included. No clutch compensation.
- For parts that must clutch like LEGO, use a tuned profile with `--fit`, and print the `--fit-test` plate (or a `calibrate fit` plate) first. PETG: `a1mini-petg-008-lego-v8` + the Porima filament profiles. Dry PETG before printing.
- Manual GUI check after building (not verifiable by CLI slicing): open the file in Bambu Studio and confirm Quality shows the X-Y hole/contour compensation and Strength shows the wall loops (PETG: 4, top 6 / bottom 5); Support should be enabled, normal (manual). How the GUI presents the project (e.g. stock preset with a "modified" mark) has not been verified; it is an expectation to confirm in this check, and a re-save is not expected to be needed. If the user re-saves, run `brickprint verify FILE --profile ... --fit ...` on the re-saved file.
- The A1 mini AMS lite has 4 slots. The project can hold many filaments; remap the plate's color to a loaded slot one plate at a time (a `Mixed` plate needs up to 4 slots at once).
- Tyres, rims, round belts, bars, the steering arm, and the brick separator are hard-laid flat. Everything else starts in its assembled orientation; the orientation engine only rotates a part when that pose is unprintable.
- Bambu may warn about **open edges**. LDraw parts are rendering models with open undersides (the stud cavities), so this is expected. The plate still slices. **Do not** run an online repair that fills every hole; it would block the stud cavities.
- `reversed faces` is a rendering-only flag from LDraw winding. The Countach file slices on all plates. If it matters, a normals-only cleanup can be added, but do not fill holes.
- **"floating regions" / "floating cantilever" are NOT expected.** A floating warning on a plate whose object does not have `enable_support=1` is a bug: that part will print in mid-air. Fix it by re-orienting the part (`--orient`) or moving it to a supports plate, and check the probe ran (`_report.json` → `floating_verified`). On `supports` plates the warning is harmless (tree supports carry the region).

## Limitations

- **LDraw coverage is not universal.** New molds, printed parts, minifig assemblies, and some large assemblies have no LDraw file. The tool reports them instead of guessing. Printed variants are simplified to the base mold where a parent relationship exists.
- **Minifigs are not expanded by default.** Use `--include-minifigs`; leg/torso assemblies often remain missing.
- **One color per plate** is the default for every project that fits on 36 plates. A set with many colors can produce many sparsely-filled plates; `--mix-colours always` puts tiny groups (a last plate at most 35 % full) together, up to 4 colours per plate, each colour a separate block and object. Large colour groups always keep their own plates.
- **Printer presets beyond the A1 mini use Bambu's stock settings only.** `--preset a1|p1s|x1c` builds with the stock 0.20 mm PLA chain of that printer; the tuned PETG / LEGO-fit profiles, the porima filament profiles and the ceilings recipe were calibrated on the A1 mini only. For any other printer, supply a matching Bambu project settings template (`settings` / `BRICKPRINT_SETTINGS`) and the correct bed size; the 3MF geometry and plate grid scale with `--bed/--depth`.
- **Up to 36 plates** (Bambu's plate grid limit). A very large set may need to be split.
- **Very color-heavy sets** may exceed the configured `max_filaments` (default 32). Split the project or raise the limit if the target Bambu version supports it.

## Data and licenses

- Rebrickable CSV downloads: <https://rebrickable.com/downloads/>. Respect Rebrickable's terms; attribution is required for public use.
- LDraw parts library: <https://library.ldraw.org/>; CC BY 4.0. Keep the `CAreadme.txt` / attribution files with the library.
- The tool is not affiliated with LEGO, Rebrickable, LDraw, or Bambu Lab.

See `references/` for pipeline details, print tuning (`print-tuning.md`: profiles, fit calibration, supports, bridging, verify), data sources, Bambu 3MF notes, and troubleshooting.
