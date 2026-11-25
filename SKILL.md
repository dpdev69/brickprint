---
name: brickprint
description: >-
  Build, tune, calibrate, and verify color-grouped multi-plate Bambu Studio 3MF
  projects for LEGO sets or local MOC BOM CSVs. Use when the user asks to turn a
  LEGO set number (e.g. 10337-1) or BOM CSV into printable plates, calibrate PETG
  or PLA LEGO clutch fit (--fit / calibrate fit), pick a Bambu printer preset
  (a1mini/a1/p1s/x1c), check set inventory or LDraw mesh quality, verify a LEGO
  3MF before printing, or troubleshoot socket supports, floating parts, or
  bridging/ceilings.
---

# Brickprint

Pipeline: `set number / --bom` → Rebrickable inventory → LDraw + LDView meshes → Bambu floating probe & orientation → MaxRects color packing → Bambu Studio `.3mf` → `verify`.

## Launcher (not on `PATH`)

`install.sh` does not add `brickprint` to `PATH`. Invoke the launcher next to this `SKILL.md` (or `./bin/brickprint` from a repo checkout) and alias it as `BP` in shell commands:

```bash
~/.gemini/config/skills/brickprint/bin/brickprint doctor   # Antigravity
~/.claude/skills/brickprint/bin/brickprint doctor          # Claude Code
~/.codex/skills/brickprint/bin/brickprint doctor           # Codex
```

Requires **Python 3.9+ with numpy**. `bin/brickprint` selects `$BRICKPRINT_PYTHON` → repo `.venv` → newest Blender bundled Python → `python3`.

## Quick start

```bash
BP=~/.gemini/config/skills/brickprint/bin/brickprint   # adjust to your install path

# 1. Check setup (and fetch Rebrickable + LDraw data if missing)
"$BP" doctor
"$BP" bootstrap

# 2. Stock PLA build (A1 mini default; add --preset a1|p1s|x1c for 256 mm printers)
"$BP" inventory 10337-1
"$BP" build 10337-1 --bambu --verify

# 3. Tuned PETG with LEGO clutch fit (A1 mini, recommended profile)
"$BP" calibrate fit --profile a1mini-petg-008-lego-v8 --filament porima-petg-white \
  --values 0.02,0.03,0.04,0.05 --out fit_cal.3mf --bambu --verify
"$BP" build 10337-1 --profile a1mini-petg-008-lego-v8 \
  --filament White=porima-petg-white --fit 0.03 --fit-test --bambu --verify

# 4. Local MOC BOM CSV
"$BP" build --bom my_bom.csv --name "My MOC" --bambu --verify
```

## Commands

| Command | Purpose & key flags |
|---|---|
| `doctor [--preset P]` | Print resolved Python/numpy, cache, LDraw, LDView, Bambu, preset, and profile status. Fails if LDraw, LDView, numpy, or template is missing. |
| `bootstrap [--refresh]` | Download Rebrickable CSV dumps and (if no LDraw dir is set) LDraw `complete.zip` into cache, recording `ldraw_dir` in `~/.config/brickprint/config.json`. Does not install LDView or Bambu Studio. |
| `inventory SET [--out CSV] [--include-spares] [--include-minifigs] [--refresh]` | Write BOM CSV and report lots, parts, colours, minifigs, and missing LDraw parts. |
| `build [SET] [--bom CSV] [--name N] [--out FILE]` | Build multi-plate `.3mf` + `_raw.3mf`, `_plates.csv`, `_missing.csv`, `_report.json` (plus `_quality.csv` on profile builds). |
| `calibrate fit [--values 0.02,0.03,0.04,0.05] [--parts 3022,3005,3024,4733]` | Build per-object X-Y compensation test rows (`--out fit_calibration.3mf`, `--hole-ratio`, `--bambu`, `--no-probe`, `--verify`, `--preset`, plus tuning flags except `--fit`). Rows run front (`y` min, smallest value) to back (largest); parts left to right; objects named `fit <contour> <part>`. |
| `verify FILE.3mf [--plates 1,3] [--reference REF.3mf]` | Slice via Bambu CLI and check effective settings, stud/socket toolpaths, support placement/enclosure, floating objects, and thumbnails. Also `--tol` (0.01), `--workdir`, `--report`, `--expect K=V`, `--profile`, `--fit`, `--set K=V`. No `--preset`. |
| `quality [PARTS... \| --bom CSV] [--csv FILE] [--part-map FILE]` | Per-part mesh quality report (48-gon vs 16-gon fallback, open/non-manifold edges, stud Ø, 8 mm footprint). |
| `profiles ACTION [NAME]` | `list`; `show NAME [--resolved --filament F --fit C]`; `check` (read-only audit of Bambu user presets); `export NAME --to DIR` (**filament profiles only**; never writes Bambu's user folder); `regen-template [--preset P --out FILE]`. |
| `validate FILE.3mf [--preset P --bed W --depth D --slice]` | World-space bbox check and plate/component/triangle counts (does not slice; use `verify`). |

### Key `build` flags, defaults & footguns

- **`--bambu` is OFF by default (`False`)** on `build` and `calibrate`. Pass `--bambu` to run the Bambu CLI thumbnail/normalisation pass (`--no-bambu` explicitly disables it).
- **`--fit-test [PARTS]` ordering footgun:** Uses `nargs="?"`. Put bare `--fit-test` **after** the positional `SET` number (or use `--fit-test=3022,3005`) — `build --fit-test 10337-1` swallows `10337-1` as `PARTS` and fails.
- `--preset a1mini|a1|p1s|x1c` (default `a1mini`; precedence: `--preset` > `BRICKPRINT_PRESET` > config `"preset"`). Non-A1-mini presets derive 256×256 stock PLA templates from installed Bambu Studio (P1S/X1C keep clear of the 18×28 mm front-left corner) and refuse A1 mini PETG/Porima profiles. Override geometry with `--bed/--depth/--margin/--gap`.
- `--mix-colours auto|always|off` (default `auto`): merges tiny groups (last plate ≤35% full, ≤4 colours/plate) only when needed to stay within Bambu's 36-plate grid (35 with `--fit-test`); `always` mixes all tiny groups; `off` keeps one colour per plate.
- `--orient FILE.json`: `{"70490.dat": "Rx"}` (`id`, `Rx`, `Rx2`, `Ry`, `Ry2`, case-insensitive; trailing `!` forces the pose even if floating → support plates).
- `--strict` (fail on missing LDraw parts), `--no-probe` (skip Bambu floating probe — prints UNVERIFIED warning), `--part-map FILE` (merges atop `~/.config/brickprint/part_overrides.json`), `--include-spares`, `--include-minifigs`, `--refresh`, `--verify [--reference REF.3mf]`.
- **Print tuning:** `--profile NAME` (`a1mini-pla-stock` default; **`a1mini-petg-008-lego-v8`** recommended for PETG), `--filament [COLOUR=]PROFILE` (repeatable; 9 `porima-petg-*` spools bundled), `--fit C[,H]` (0–0.3 mm; default `H = -C`), `--supports enforcers|plates`, `--bridging short-span|stock`, `--ceilings recipe8|off` (requires `thick_bridges=1`), `--set K=V`, `--support-set K=V` (tree-supported objects only; rejects `enable_support`/`support_type`/`bridge_no_support`), `--allow-shrink`.

## Hard safety & print-readiness rules

1. **Always run `verify` (or `bambu_verify`) before telling the user a project is ready to print.**
2. **Never enable automatic supports globally on LEGO projects** — auto supports fill underside stud sockets. Use `--supports enforcers` (`normal(manual)` + enforcer boxes) or `<Colour> supports` plates (profile builds add socket blockers).
3. **Never run a hole-filling mesh repair.** Bambu "open edges" and "reversed faces" warnings are expected LDraw properties; filling holes blocks stud sockets.
4. **"Floating regions" / "floating cantilever" on a supports-off plate is a bug.** Bambu's floating check is non-deterministic; re-run `verify --plates N` before treating a single hit as a regression. Probe, plate check, and `verify` always slice a supports-off copy without enforcers.
5. **Bambu Studio CLI works while the GUI is open** — never ask the user to close it.
6. **Never hand-edit `assets/a1mini_pla_project_settings.json`** (use `profiles regen-template`; stripped templates drop start G-code). Never set `thick_bridges=1` without `--ceilings recipe8`.
7. **Inspect `_report.json`:** confirm `missing_lots == 0`, `oversized_lots == 0`, `floating_verified == true`, and check `orientation_warnings` (and `_missing.csv` if non-empty).

## Bambu Oracle MCP (optional companion)

`bambu-oracle` is an optional separate MCP server; everything above works with `brickprint` alone. When `bambu_*` tools are available, use them instead of throwaway Python and **always pass absolute paths**:

1. `bambu_project(path="/abs/out.3mf")` → plates, colours, sibling reports, warnings.
2. `bambu_slice(path="/abs/out.3mf", plates="all", render=false)` → fast cached slice stats/warnings (`render=true` to also render PNGs).
3. `bambu_preview(path="/abs/out.3mf")` → embedded thumbnails or rendered plate PNGs.
4. `bambu_verify(path="/abs/out.3mf")` → structured pass/fail summary + report path.

Also: `bambu_profiles`, `bambu_export`, `bambu_gcode`, `bambu_re`, `bambu_doctor`. See `references/bambu-oracle.md`.

## References (read on demand)

| Topic | Reference |
|---|---|
| Profiles (`v8`, 9 Porima spools), clutch fit, supports/blockers, bridging, `ceilings recipe8`, `verify`, manual GUI check | `references/print-tuning.md` |
| Set lookup, LDraw/LDView flags, `mesh_workers`, orientation, Bambu probe/final/plate checks, MaxRects & `--mix-colours` | `references/pipeline.md` |
| 3MF layout (one object/plate vs per-part `recipe8`), plate grid math, XML metadata, CLI normalisation & warnings | `references/bambu-3mf.md` |
| Optional `bambu_*` MCP tool schemas, caching semantics, and examples | `references/bambu-oracle.md` |
| Rebrickable dumps, LDraw library, LDView, Bambu Studio requirements & attribution (CC BY 4.0 / Rebrickable / AGPL note) | `references/data-sources.md` |
| `doctor` errors, numpy/Blender Python, work-dir disk sweep (`sweep_days`/`verify_keep_gb`), enclosed supports, path conflicts, exit 152 | `references/troubleshooting.md` |
