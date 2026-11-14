# Print tuning: profiles, fit, supports, bridging, verify

Everything here was verified by slicing with the Bambu Studio 02.09.01.70 CLI
(Seagull MMMB027-1, Porima PETG, 0.08 mm, A1 mini). The GUI behaviour noted at
the end is **not** verifiable by CLI slicing.

## Profiles

```
brickprint profiles list
brickprint profiles show a1mini-petg-008-lego --resolved --filament porima-petg-white
brickprint profiles check            # read-only: installed Bambu user presets vs profiles
brickprint profiles export porima-petg-white --to ~/Desktop/presets
```

- Process profiles: `assets/profiles/process/*.json`.
- Filament profiles: `assets/profiles/filament/*.json`.
- User profiles in `~/.config/brickprint/profiles/{process,filament}/` win over
  the shipped ones.
- A process profile names a stock Bambu process (`base_process`), a printer, a
  default filament, `fit`, `supports`, `bridging`, and `overrides` (plain JSON
  values).
- A filament profile names a stock `base_filament`, a spool `colour`,
  `overrides`, and a `manufacturer` block. The block records the vendor's
  published data with its source URL. Values the vendor does not publish are
  `null`.

| profile | what |
|---|---|
| `a1mini-pla-stock` (default) | Bambu stock A1 mini 0.4 + PLA Basic + 0.20mm Standard from the checked-in snapshot. Output is byte-identical to brickprint before profiles existed. Supports go on `<Colour> supports` plates. |
| `a1mini-petg-008-lego` | 0.08mm Extra Fine base, PETG Basic base filament, the Seagull v4 tuning: 4 walls, 6/5 shells, 50/100 mm/s walls, bridge 25 mm/s, `thick_bridges 0`, fit ±0.03, short-span bridging, enforcer-only supports with a 0.24 mm top gap. |
| `a1mini-petg-008-lego-v8` | The profile above plus `ceilings recipe8` and `thick_bridges 1` (Seagull v8). Recommended for PETG. |
| `porima-petg-white`, `porima-petg-neon-orange` | Porima PETG spools: 250/255 °C, bed 80, fan 20-30 %, overhang fan 100 %, flow 1.00/1.01, max 9/7 mm³/s. Manufacturer data: nozzle 240-260 °C, bed 60-80 °C, fan 100 %, drying 2-4 h at 30-40 °C (as published). Retraction, density, shrink and flow are not published. |

How the settings are written (`brickprint/settings.py`):

1. The base is resolved live from the installed Bambu Studio bundle (machine
   + process + one filament chain per slot). The default profile uses the
   snapshot instead.
2. Every override is shaped to the key's own type: a string, or a list of
   strings of the right length. **JSON numbers are never written**, because
   Bambu rejects them ("invalid json type") and slices stock values.
3. `different_settings_to_system` is computed. Slot 0 holds the process keys
   that differ from the base process. Slots 1..N hold the keys each filament
   slot changes against its base filament. The last slot is the printer.
   `filament_settings_id` is the stock base name (e.g. "Bambu PETG Basic @BBL
   A1M 0.4 nozzle"). The spool name is not used because it could clash with
   stale user presets of the same name.
4. These are hard errors:
   - `filament_shrink` above 100 %, because it scales parts **down**. Override
     with `--allow-shrink`.
   - A nozzle setpoint outside the preset range or the manufacturer range.
   - A bed temperature outside the manufacturer range.
   - An unknown key.
   - A filament key in a process profile.

`--set KEY=VALUE` adds any process key through the same rules. Value kinds are checked against the stock value (whole number, number, percent); enums and free strings are not.

## Fit (clutch)

LDraw geometry has zero stud/socket interference, so FDM clutch comes from Bambu's
X-Y compensation.

- `--fit C` sets contour +C and hole −C.
- `--fit C,H` sets the two separately.

Measured on toolpaths with round studs:

| fit | 2×2 stud Ø | 1×1 socket gap | stud→socket interference |
|---|---|---|---|
| 0 | ~4.79 | ~4.80 | ~0 |
| 0.03 | 4.857 | 4.740 | +0.12 |
| 0.05 | 4.897 | 4.700 | +0.20 |

Contour compensation also grows the outside: a 2×2 is 16.06 mm at +0.03
(real LEGO 15.8, LDraw 16.0). Bambu applies X-Y compensation **per object
only**. Per-part (volume) values are ignored (CLI-verified).

### Calibration workflow

1. `brickprint calibrate fit --profile a1mini-petg-008-lego-v8 --filament
   porima-petg-white --values 0.02,0.03,0.04,0.05 --out fit_cal.3mf --verify`.
   This gives one row per value. Each test piece is its own object with
   per-object compensation. `--verify` prints one console line per row/part
   (object name, stud Ø, socket); the full per-row numbers are also in the
   `<file>_verify.json` report.

   **Which row is which value.** The printed pieces carry no physical
   marking. Values are sorted ascending and laid out along +Y in plate
   coordinates (origin front-left), so the row at the FRONT of the plate
   (y min, toward you at the printer) is the smallest value and the rows
   increase toward the BACK. Within a row the parts run left to right in
   `--parts` order. If the rows do not fit one plate, they continue on the
   next plate in the same order. In Bambu Studio's object list each object is
   named `fit <contour> <part>`, so the value is visible there. `calibrate fit`
   prints this mapping after building. Contour values must be 0 to 0.3 mm
   (the hole value is the negative); the same limits apply to `--fit`.
2. Print the plate, then try the pieces against each other and against real
   LEGO. Pick the lowest value that clutches.
3. Build with it: `brickprint build SET --profile ... --fit 0.04`. Add
   `--fit-test` to append a plate of test pieces printed with that fit.

## Supports: never automatic

Bambu's automatic supports (normal or tree, `(auto)`) fill the underside
sockets, even with build-plate-only and bridge-no-support. Do not switch a
LEGO project to auto supports.

`--supports enforcers` (the PETG profile default) works like this:

- Global settings are `enable_support=1` and `support_type=normal(manual)`.
- Bambu then supports only inside `support_enforcer` volumes.
  `brickprint/features.py` generates those volumes under features that stick
  out of the part body: downward faces more than 0.3 mm outside the
  first-layer footprint, protruding more than 0.8 mm. One box per feature, the
  outside half only, from 0.3 mm below to 0.6 mm above its lowest point.
- In the Seagull only 4733's side studs qualify. The boxes are identical to
  the hand-built v4.
- Parts that Bambu still flags as floating in every pose go to `<Colour>
  supports` plates. Those plates get per-object `tree(auto)`, which overrides
  the global manual setting.
- So do part types that float **as built**: every part type is sliced alone
  as the build writes it (chosen pose, raised ceilings, ribs, bands, per-part
  settings; supports off, no enforcers), 3 runs, cached ("final-geometry
  check"); and part types the whole-project plate check names (Bambu writes
  one warning per floating object) are moved there too, after which the
  project is re-packed and re-checked (up to 4 rounds). Countach: 15068,
  63868, 3172 and others were flagged in about 2 of 3 runs although the probe
  had found their pose clean 3 times.
- Before, a flagged plate got `tree(auto)` as a whole (`slice-check`). Sliced
  Countach plates showed what that does: support inside the raised plate
  sockets of 2x6 / 2x8 / 4x6 plates at z 0.2-1.9 mm (White 2 of 6). The
  default profile still works that way (its plates are one object).
- **Socket blockers.** Every part in an object with automatic supports
  (support plates) gets `support_blocker` boxes over its enclosed cavities
  (`features.socket_blockers`: empty cells of the sliced part that cannot be
  reached from outside within their layer, under a ceiling, reaching 0.4 mm
  into it). Without them the Countach support plates had tree support in the
  sockets of 73825 (z 0.2-1.2 mm) and five other part types; with them the
  same parts slice with none (`verify`: enclosed 0). Profile builds only; the
  default PLA output is unchanged.
- Ceiling ribs are checked the same way: a part that floats with its ribs
  and is clean without them in 3 runs loses its ribs (70490: rib 0.24 mm
  under an 11.2 mm dome ceiling, "floating cantilever" 5/5 runs, 0/5
  without).

`--supports plates` (the default profile) puts support-needing parts on
`<Colour> supports` plates only.

**Safety:** Bambu stops reporting "floating regions / cantilever" as soon as
supports are enabled (also `normal(manual)` without any enforcer,
CLI-verified). The floating probe, the plate check and `verify` therefore
slice a supports-off copy. That copy carries **no enforcer volumes**: with
supports off Bambu reports an enforced overhang as floating (60470b alone:
5/5 runs flagged with its enforcer, 0/5 without), while the real file
supports exactly that overhang.

## Bridging

Bambu's automatic bridge direction runs cavity ceilings the long way (28.5 mm
spans on 4×2 wedge plates). `--bridging short-span` (PETG profile) fixes this:

- The global `bridge_angle` is 180 (lines along X).
- A part whose ceiling is X-long, and longer than one stud pitch, gets a
  per-part `bridge_angle=90` (the 3794a jumper in the Seagull).
- Without the ceiling recipe, `thick_bridges` stays 0: the stud height equals
  the socket depth (1.6 mm), so a thick strand stops plates seating.

## Socket ceilings (`--ceilings recipe8`)

A 0.08 mm bridge strand cannot span a 12 mm plate cavity: printed plates had a
tangled mat hanging below stud height. Recipe 8 (variant 8 of the printed
Seagull v7 ceiling test; module `ceilings.py`) fixes this per part:

1. **Raise.** Socket ceilings a stud can reach (plates, tiles, the Technic
   brick's socket) move from 1.6 to 2.2 mm above the bed, the real LEGO depth;
   the top skin becomes 1.0 mm. Only vertices on the ceiling plane move; the
   part is refused when sloped faces touch that plane (4286). Brick sockets are
   8 mm deep and stay.
2. **Bands.** Per bridged ceiling, two height ranges in
   `Metadata/layer_config_ranges.xml`: the bridge layer at 0.24 mm with
   `wall_loops 2` (strand ends land on the wall tops) and a second 0.24 mm
   layer. With `thick_bridges 1` the strand is about 0.4 mm round and its
   bottom sits about 0.34 mm above a 1.7 mm stud. A band is refused where it
   would touch a fit surface (studs, the 0-1.8 mm socket walls, curved bores);
   slopes get the 2-loop band only, at 0.08 mm.
3. **Ribs.** Where the longest free strand exceeds 7 mm (2x2 plate, wedge
   plates), a 0.8 mm rib across the strands, 0.24 mm deep below the ceiling,
   on a stud-free grid line. Worst span 12.4-13.5 mm becomes 6.0-6.7 mm.

Consequences: one Bambu object per part instance (height ranges are per
object), so parts with support enforcers get a packing keep-out; otherwise
Bambu rejects the plate with "gcode path conflicts". The floating check does
not see that conflict (it slices a supports-off copy), so build with
`--verify`. Parts whose body is not centred in their bounding box (one-sided
side studs) are judged conservatively and keep plain ceilings. Whether the GUI
keeps the height ranges on open/save has not been verified.

## verify

```
brickprint verify FILE.3mf [--plates 1,3] [--reference REF.3mf] [--tol 0.01] [--profile P --fit C]
```

`verify` slices with the CLI and checks:

1. **Effective settings.** The listed keys appear in the G-code config block
   with the project's values. Intended values from `--profile`/`--fit`/`--expect`
   or `<file>_report.json` must be effective too. This catches a GUI re-save
   that reset tuned keys.
2. **Stud/socket toolpaths per part.** Studs are measured at top − 0.8 mm;
   outer size, cavity/socket gap and tube OD at z = 1.0 mm. A contour is its
   centreline + line width, a hole its centreline − line width.
3. **Support location.** Supports of objects without their own supports must
   stay within 5 mm of an enforcer (measured: Bambu grows normal supports
   about 3.5 mm around the enforced overhang; TIE Fighter 75300 plates 4/7/8
   reached 4.4-4.9 mm). Support of an object with per-object `enable_support`
   (support plates) may be anywhere within 10 mm of that object (tree bases
   spread 6.5 mm).
4. **No support enclosed by a part.** From the G-code alone: a support point
   that lies inside a part's outer-wall outline on the same layer (± 0.06 mm)
   is inside a socket, tube or hole, whatever the object's support settings.
   The error names the part and the z range. On a default-profile build
   (sibling `_report.json` without `settings`) it is a warning instead,
   because that profile writes no socket blockers; with no sibling report it
   stays an error. This also flags an enforcer that
   intentionally supports an internal cantilever: 11215 (Bracket 5 x 2 x
   1.333) does this in its assembled +Z pose; print it with a `Ry!` override
   instead of adding a socket blocker that would remove the needed support.
   First-layer support (`z <= 0.25 mm`) is treated as a base/brim and ignored;
   an intrusion that continues upward is still caught at the next layers.
   (The old rule, "inside the
   first-layer hull", also hit legitimate support between the studs of
   upside-down bricks, e.g. 30137 and 2654a.)
5. **Floating.** A supports-off slice (no enforcers) must show no floating
   object without its own supports. One Bambu run decides it. If the file
   already has global supports off and no enforcers (default profile), the
   supports-off copy would be the same project (`verify.same_slice_input`),
   so the file's own slice is that run and verify slices only once.
   Otherwise the copy is sliced while the file is sliced and analysed.
6. **Thumbnails.** It warns when they are missing or stale.

Part positions in the G-code come from one offset per plate (outer walls vs
the 3MF). Bambu writes no object labels on one-object plates and on plates
with more than 64 objects, and its label ids differ from the 3MF after a
re-export, so labels are not used. Ceiling-rib volumes are not counted as
parts. A plate that Bambu cannot slice is reported with Bambu's own error,
and the plates after it are sliced one by one instead of being dropped.

With `--reference`, parts are matched by mesh signature. Stud Ø, socket gap
(rectangular or round hollows) and tube OD must agree within `--tol`. Outer
sizes and irregular hollows (wedge plates) are reported but not gated: their
bounding boxes vary by up to ~0.02 mm even between mirrored instances in one
slice. The G-code config must agree too, apart from an allow-list:

- **bookkeeping:** names, ids, colours, diff lists, wipe-tower position;
- **the hand-built v4 orange slot's PLA-preset leftovers:**
  `temperature_vitrification`, plate temps for other plate types,
  `filament_prime_volume`, `filament_change_length`, `filament_density`,
  `filament_cost`, `filament_wipe(_distance)`, `filament_start_gcode`,
  `fins_extrude_safe_temp`, `impact_strength_z`, ...;
- **AMS-drying lists** that v4 trimmed to the slot count.

## Manual GUI check (not verifiable by CLI)

Open the file in Bambu Studio and confirm these show:

- Process → Quality: X-Y hole compensation −0.03 and X-Y contour compensation
  +0.03 (or your fit).
- Strength: wall loops 4, top 6 / bottom 5.
- Speed: outer wall 50.
- Support: enabled, "normal (manual)".

Expected (to be confirmed by this manual check; GUI behaviour has not been
verified): the process shows as the stock preset with a "modified" mark and a
re-save is not needed. If you do re-save, run `brickprint verify
FILE --profile a1mini-petg-008-lego --fit 0.03` on the re-saved file.

## Cleanup: brim and per-part settings (`a1mini-petg-008-lego-v8`)

After the full Seagull print the brim and support cleanup was the main
complaint (one 3 mm brim per object, about 30 objects on a plate). The profile
now carries two blocks, both applied as per-object settings (they need the
one-object-per-part layout of `ceilings recipe8`):

- `"brim": {"mode": "small-contact", "contact_mm2": 20, "width_mm": 3,
  "gap_mm": 0.2}` with `brim_type no_brim` globally: only parts with less than
  20 mm2 of bed contact get a brim (the 59900 cone in the Seagull).
- `"part_settings": {"59900": {"xy_hole_compensation": "-0.08"}}`: per-part
  process keys by LDraw part name. The cone's round socket was loose at -0.03;
  -0.08 takes it from 4.72 to 4.62 mm in the sliced toolpaths.

Supports under side studs are unchanged in placement but lighter:
`support_top_z_distance 0.32`, 2 interface layers at 0.5 mm spacing.

All three were confirmed on a printed test plate (wedge 4x2, 2x2 plates, 4733,
cone) on 2026-11-11. Long thin plates are still the parts to watch for corner
lift without a brim on larger sets.
