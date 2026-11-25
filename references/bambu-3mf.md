# Bambu Studio 3MF notes

## Object layout (per plate vs per part)

Default PLA builds (`a1mini-pla-stock`) write one Bambu object per plate
containing every part on that plate as a component. Exceptions that write
multiple objects per plate:

- **Socket ceilings (`--ceilings recipe8`, default in `a1mini-petg-008-lego-v8`):**
  every part instance is its own Bambu object, because height-range modifiers
  (`layer_config_ranges.xml`) and per-object brims/fit overrides apply per
  object.
- **Mixed-colour plates (`--mix-colours`):** one object per colour group (or
  one object per part under `recipe8`), because extruder assignment is per
  object.
- **Fit-test and calibration plates:** one object per test piece, so each can
  carry per-object X-Y compensation (Bambu ignores it per part).

Using one object per plate on stock builds:

- keeps the layout stable when Bambu re-exports the project,
- lets the object carry one extruder/color,
- keeps the object list manageable (one entry per plate, not 1,506).

Bambu preserves the internal component transforms on re-export. It may
re-center each plate assembly and add plate thumbnails.

## Project settings template

`Metadata/project_settings.config` comes from
`assets/a1mini_pla_project_settings.json`. That file is the resolved stock
chain (`Bambu Lab A1 mini 0.4 nozzle` + its `include`d G-code template files,
`Bambu PLA Basic @BBL A1M`, `0.20mm Standard @BBL A1M`, each followed through
`inherits`), built by `brickprint profiles regen-template`
(`brickprint/bambu_profiles.py`). The only deviation is
`curr_bed_type = Textured PEI Plate`; `different_settings_to_system` is all
empty strings, so Bambu shows the presets as unmodified system presets.

Tuned profiles (`settings.py`, see `print-tuning.md`) start from the stock chain
of their own base presets and must follow Bambu's rules or values are lost:

- every value is a string or a list of strings of the key's length (JSON
  numbers give "invalid json type" in the CLI log and stock values are sliced);
- `different_settings_to_system` = [process, filament_1..N, printer], each a
  `;`-joined sorted key list of what differs from that system preset. The CLI
  slices unlisted string values anyway, and `--export-3mf` keeps the list as
  given, but the GUI resets unlisted keys; brickprint always computes the list;
- `filament_shrink` > 100 % scales the model down.

The template holds one filament slot. Its reserved key
`_brickprint_per_filament_keys` lists every per-filament array; the writer
repeats each to the project's filament count, sets `filament_colour` per plate
colour, builds `flush_volumes_matrix` (N²) and `flush_volumes_vector` (2N), and
drops the reserved key. Machine/process arrays (`machine_max_*`,
`printable_area`, ...) keep their stock length.

## Plate grid

Bambu's `PartPlateList::reload_all_objects()` assigns each instance to the first
plate whose world-space bed area intersects the instance bbox. Plate origins are
computed as:

```
cols = ceil(sqrt(plate_count))
stride_x = bed_width * 1.2
stride_y = bed_depth * 1.2
row = i // cols
col = i % cols
origin = (col * stride_x, -row * stride_y, 0)
```

For the A1 mini (180 mm), the stride is 216 mm. Build items must sit at those
origins; otherwise Bambu leaves objects unassigned or drops them.

## Plate metadata

`Metadata/model_settings.config` contains one `<object>` per plate (name +
extruder) and one `<plate>` with `<model_instance>` referencing the plate object.
The `model_settings` names/configs are used, but the actual geometry-to-plate
assignment is the spatial rule above.

Plate / object names are `"<Colour> i of n"` for normal plates and
`"<Colour> supports i of n"` for support plates (XML-escaped, including `"`).

Mixed-colour plates (`--mix-colours`) are named
`"Mixed [supports ]i of n (<Colour>, <Colour>)"`. Bambu's extruder is set per
object here, so a mixed plate never puts two colours into one object: it has
one object per colour group (`"Mixed i of n: <Colour>"`, components as usual),
or one object per part with `ceilings recipe8`, each with its own
`<metadata key="extruder">`. Filament slots are numbered in plate order, a
mixed plate contributing its colours in group order. The plate's other objects
are listed as extra `<model_instance>`s, like the fit-test plate.

With the default profile, project settings keep supports off
(`enable_support = 0`). Support plate objects carry per-object overrides; a
per-object `tree(auto)` also overrides a global `normal(manual)`:

```xml
<object id="..."><metadata key="name" value="Black supports 1 of 1"/>
 <metadata key="extruder" value="2"/>
 <metadata key="enable_support" value="1"/>
 <metadata key="support_type" value="tree(auto)"/>
 <metadata key="bridge_no_support" value="1"/></object>
```

`bridge_no_support=1` keeps brick cavity ceilings as bridges instead of filling
them with supports. Do not use automatic supports globally for LEGO parts:
they fill the underside sockets.

## Per-part settings and support enforcers

An object whose parts need per-part settings (e.g. `bridge_angle`) or support
enforcers gets one `<part>` entry per component, in component order:

```xml
<object id="16"><metadata key="name" value="White 1 of 1"/><metadata key="extruder" value="1"/>
 <part id="5" subtype="normal_part"><metadata key="name" value="3794a"/>
  <metadata key="bridge_angle" value="90"/><metadata key="matrix" value="1 0 0 140 0 1 0 6 0 0 1 0 0 0 0 1"/></part>
 <part id="12" subtype="support_enforcer"><metadata key="name" value="4733 support enforcer"/>...</part>
</object>
```

The enforcer is an extra component (its own mesh object) with the same
transform as its part. Without its `<part subtype="support_enforcer">` entry it
would print as a solid box, so the writer asserts that every component has a
part entry. Enforcers only act when `enable_support=1`; with
`support_type=normal(manual)` Bambu supports **only** inside enforcers.
CLI-verified: raw (un-normalised) brickprint files honour per-part
`bridge_angle` and enforcers, and `--export-3mf` keeps them.

36-plate folding rule: Bambu's plate grid holds 36 plates. If the colour groups
plus their support plates exceed 36 (35 with `--fit-test`) even after colour
mixing (`--mix-colours auto`), the smallest support groups are folded into
their colour's last normal plate (largest groups stay separate); that normal
plate then gets the same three support keys for the whole plate, keeps its
`"<Colour> i of n"` name, and shows `folded` in `_plates.csv`'s `supports`
column. A one-object plate (default profile) that the build's whole-project
slice check still flags as floating gets the same three keys (`slice-check`);
with one object per part the flagged part types move to support plates
instead.

Support blockers (profile builds): every part in an object with automatic
supports gets a `<part subtype="support_blocker">` component (name
`"<part> socket blocker"`, boxes over its enclosed cavities) after its normal,
rib and enforcer components. A mixed plate on a profile with a prime tower
gets its own entry in the per-plate `wipe_tower_x` / `wipe_tower_y` lists
(the corner the packer kept free).

## CLI normalisation

```bash
BambuStudio raw.3mf --outputdir /writable/dir --export-3mf final.3mf
```

- The output filename is relative to `--outputdir`; do not pass an absolute
  `--export-3mf` path.
- The output directory must exist, be writable and be given as an absolute
  path (a relative one fails with "Unable to open the file ...tmp").
- Works while the Bambu Studio GUI is open.
- It keeps project settings, per-object/per-part settings and enforcers
  exactly as given (it does not recompute `different_settings_to_system`).
- `result.json` has `return_code: 0` on success.
- The re-export adds `Metadata/plate_N.png`, `top_N.png`, `pick_N.png`, and
  Bambu project metadata.

## Slicing validation

```bash
BambuStudio final.3mf --slice 0 --outputdir /tmp/slice-check
```

`0` slices all plates; `N` slices plate N (1-based). The result JSON reports
`return_code` and per-plate `warning_message`.

Expected warnings for a LEGO parts plate:

- "open edges" — LDraw parts are open underneath by design.
- "reversed faces" — a rendering-only flag from LDraw winding; the model still
  slices. Do not fill the undersides; that destroys the stud clutch.

NOT expected (treat as bugs). Bambu reports these only when supports are off;
with `enable_support=1` (any type) the warning disappears, so check a
supports-off copy (`brickprint verify` does):

- "floating regions" / "floating cantilever" on a plate whose object does not
  carry `enable_support=1`. Some part on that plate prints in mid-air. Fix it
  by re-orienting the part or moving it to a `supports` plate; the build's
  Bambu probe (see `pipeline.md`) exists to prevent exactly this. On a
  supports plate the warning is harmless.
- "empty layer" — the plate aborts; a part has a gap between layers in its
  chosen pose and must be re-oriented.

To check a whole project:

```bash
BambuStudio raw.3mf --slice 0 --arrange 0 --orient 0 --debug 3 \
    --export-3mf outall.3mf --outputdir /tmp/slice-check > log.txt 2>&1
grep -E "plate [0-9]+: found .*slicing warnings" log.txt
```

The log lines `plate N: found slicing warnings: ...` give per-plate warnings;
cross-check each floating one against that plate's `enable_support` in
`Metadata/model_settings.config`.
