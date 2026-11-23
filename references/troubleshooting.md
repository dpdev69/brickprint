# Troubleshooting

## `doctor` says LDraw is missing

Set the path explicitly:

```bash
brickprint bootstrap
# or
export BRICKPRINT_LDRAW=/path/to/ldraw
```

The directory must contain `LDConfig.ldr`, `parts/`, and `p/`.

## `ERROR: numpy is not importable by this Python`

brickprint needs numpy. `bin/brickprint` uses `$BRICKPRINT_PYTHON` if set,
else Blender 5.2's bundled `python3.13`, else Blender 5.1's `python3.11`, else
plain `python3` — and a system/Homebrew `python3` usually has no numpy. This
error (also printed by `doctor`, with `python bin` naming the interpreter and
`numpy (missing)`) means the launcher fell back to such a `python3`, typically
because Blender moved or changed version. Point it at a Python with numpy:

```bash
export BRICKPRINT_PYTHON=/Applications/Blender.app/Contents/Resources/5.2/python/bin/python3.13
# or a venv:  python3 -m venv ~/.venvs/brickprint && ~/.venvs/brickprint/bin/pip install numpy
#             export BRICKPRINT_PYTHON=~/.venvs/brickprint/bin/python
```

`brickprint doctor` shows `python bin` (the interpreter actually used) and
`numpy` (its version) on every run.

## Disk fills up with `brickprint_verify_*` / `platecheck_*` dirs

`verify` keeps its G-code in a temp `brickprint_verify_*` dir (its path is in
the report). `build` and `verify` sweep these at start, plus `probe_*`,
`platecheck_*` and `final_*` dirs directly in the cache dir: older than
`sweep_days` (default 3) are deleted, then the oldest verify dirs until they
total at most `verify_keep_gb` (default 2 GB). Both are keys in
`~/.config/brickprint/config.json` (0 disables that rule). Dirs touched in the
last hour (a build running right now), symlinks, other names and a `--workdir`
you passed are never deleted. `BRICKPRINT_NO_SWEEP=1` disables the sweep. A
probe that could not resolve every pose keeps its `probe_*` dir (Bambu logs)
until the sweep ages it out; `BRICKPRINT_PROBE_KEEP=1` keeps every probe /
plate-check / final-check dir, also after an error or interrupt.

## `doctor` says LDView is missing

Install LDView and set:

```bash
export BRICKPRINT_LDVIEW=/path/to/LDView
```

or put the same path in `~/.config/brickprint/config.json` as `"ldview"`. On
macOS the executable is inside the app bundle, for example
`/Applications/LDView.app/Contents/MacOS/LDView`; that location and `LDView` on
`PATH` are found automatically.

## `missing.csv` is not empty

These part/color lots have no LDraw geometry. Common causes:

- new mold not yet in the LDraw library;
- printed part with no base parent;
- minifig leg/torso assembly;
- part number in Rebrickable that does not correspond to an LDraw design ID.

Options:

- update the LDraw library: replace the folder `ldraw_dir` points at with a
  newer download from <https://library.ldraw.org/> (`brickprint bootstrap`
  downloads the library only when none is configured, and `--refresh` refreshes
  the Rebrickable data, not LDraw);
- add a manual override in `~/.config/brickprint/part_overrides.json` (a JSON
  object mapping Rebrickable part → LDraw part, e.g. `{"2752": "3747"}`) or pass
  `--part-map FILE`, and rerun;
- accept the missing parts and check `_report.json` before printing.

## Oversized parts

A part is skipped when its footprint exceeds `bed - 2×margin`. A 32×32 baseplate
needs a 256 mm bed, for example. Use:

```bash
brickprint build SET --preset x1c          # 256 mm printer with its own settings
brickprint build SET --bed 256 --depth 256 # bed size only
```

or split the set.

## Color count is too high

The default `max_filaments` is 32. A very colorful set can exceed Bambu's
practical project size. Split the BOM into two builds (e.g. by color groups) or
raise `max_filaments` in `~/.config/brickprint/config.json` if your Bambu
version supports it.

## Bambu Studio is running

That is fine. The probe, `--bambu` normalisation and `verify` all use the CLI,
which works with the GUI open. The export needs an existing, absolute output
directory; brickprint creates one.

## Tuned settings do not show up / no clutch

Run `brickprint verify FILE --profile <profile> --fit <C>`. It compares the
intended values with the sliced G-code. Common causes:

- Values written as JSON numbers: Bambu ignores them ("invalid json type").
- Keys missing from `different_settings_to_system`: the GUI resets them, and a
  re-save bakes the reset in.
- `filament_shrink` > 100 %: this scales parts down.

brickprint's writer avoids all three. Scripts that edit 3MFs by hand must
follow the same rules (see `print-tuning.md`). `brickprint profiles check`
lists stale Bambu user presets, e.g. the old 100.6 % shrink Porima presets.

## Supports fill the stud sockets

Never use automatic supports (normal/tree `(auto)`) globally on LEGO parts. Use
`--supports enforcers` (support only in generated enforcer volumes) or the
`<Colour> supports` plates.

## Bambu warns about open edges / reversed faces

These are expected LDraw mesh properties; every plate of a generated project
should still slice (the Countach reference build slices on all 36 plates).
"Floating regions" / "floating cantilever" are different: on a plate without
`enable_support=1` they mean a part prints in mid-air (see
`bambu-3mf.md` → Slicing validation). Do **not** run a repair tool that fills all boundary loops:
LEGO undersides are open so the part can clutch onto studs; filling them makes
the parts solid underneath.

If you need cleaner rendering, a normals-only pass (flip inconsistent faces,
do not add faces) is the safe direction.

## Plates look wrong or parts overlap

Run `brickprint validate project.3mf`. If a plate is marked BAD, check that the
bed size used for packing matches the bed size used for validation. Do not run
Bambu's auto-arrange on the project; the layout is already intentional.

## Printer traces the parts in the air / nothing extrudes

With supports enabled Bambu does not warn about floating parts at all; run
`brickprint verify` (it slices a supports-off copy). Otherwise, the project
settings may lack the A1 mini start G-code (AMS lite filament load
`M620`/`T`/`M621`, bed leveling `G29`, Z offset `G29.1`, flow calibration
`M900`/`M983`, purge line). This happens when the template is a stripped stub
instead of the stock preset chain. Check the sliced `plate_N.gcode`: the start
block must contain `M620 S` ... `M621 S` and `G29.1`. Fix: regenerate the
template with `brickprint profiles regen-template`, rebuild
the 3MF, and slice again. In Bambu Studio, the printer, filament and process
presets should show as unmodified system presets.

## Slicing fails with a flush matrix error

This means the project settings have an N×N matrix mismatch. The writer creates
the matrix from the number of colors; if you edit the project manually, keep the
matrix length equal to `filament_colour` squared, and every key listed in the
template's `_brickprint_per_filament_keys` at the filament count.

## A plate is flagged as floating in one run but not the next (large sets)

Bambu's floating check is not deterministic. Most poses never flip, but a few
are flagged in about 2 of 3 runs (Countach: 15068 up=-X, 63868 up=+X,
3172 up=-Y), alone as well as on a packed plate. Profile builds therefore
slice every part type as built in 3 runs before packing, and move every part
type the plate check names to the support plates. A part that is only
flagged sometimes can still be flagged by a later `verify`: re-run `verify
--plates N`; a part that keeps floating needs `--orient` or a support plate.

The default profile still enables tree supports on a flagged plate as a
whole, and those can fill sockets (`verify` reports support "enclosed by a
part's walls").

## Supports appear inside sockets on a support plate

`verify` reports "support points enclosed by a part's walls" with the part
and z range. Profile builds put support blockers over every enclosed cavity
of parts on support plates; an enclosed hit there means a cavity the blocker
missed (for example one open to the side only through a narrow slot), and is
an error. The default profile writes no blockers (its output is kept
unchanged), so on a default-profile build (the sibling `_report.json` has no
`settings` block) `verify` reports the same finding as a WARNING ending in
"default-profile build: it writes no socket support blockers ..." and can
still PASS; build with `--profile` (or any tuning option) to get blockers.
With no sibling `_report.json` verify cannot tell and keeps it an error.

## "gcode path conflicts" between two objects

Tree or normal supports of one object run into a neighbouring object. Parts
with support enforcers get a packing keep-out; support plates are one object
(one per colour on `Mixed supports` plates, 15 mm apart; 5 mm still gave a conflict on the Countach). `verify` reports
the plate and keeps checking the others.

## Mixed plate: "G-code outside of the printable area" (exit 152)

The prime tower lay off the bed (the stock template puts it at 15, 220 on a
180 mm bed). Builds now keep a corner of every mixed plate free and place the
tower there when the profile prints one. Rebuild older files.
