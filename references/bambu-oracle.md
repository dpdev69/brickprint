# Bambu Oracle MCP

> **Optional companion, not part of this repository.** `bambu-oracle` is a
> separate MCP server that is distributed on its own and may not be available
> to you. The brickprint skill works without it through the `brickprint` CLI
> alone (for example `brickprint verify`, `brickprint profiles`). Read this
> file only if your agent actually has the `bambu_*` tools.

`bambu-oracle` is an MCP server that wraps the installed Bambu Studio CLI and
this skill's verifier. It was built from static inspection of the installed
Bambu Studio and controlled CLI probes. Its source is not included here; it is
published (if at all) separately from brickprint.

Use the tools instead of ad-hoc `python3` for the questions that come after a
`brickprint build`.

## Tool reference

### `bambu_doctor`

```json
{"check_brickprint": true, "check_rea": true}
```

Checks the Bambu CLI, the stock profile tree, the brickprint launcher, the
reverse-engineering evidence files (`check_rea`), and the Oracle cache. Use it first when a tool fails.

### `bambu_project`

```json
{"path": "/abs/out.3mf", "plates": "all", "include_objects": false}
```

Read-only 3MF inspection. Returns:

- printer / process / filament settings and `different_settings_to_system`;
- every plate's name, object/part/triangle counts, extruders, colours, embedded
  thumbnail status, and (when the sibling `_plates.csv` exists) bed size, max Z
  and support flag;
- totals and warnings (`missing_lots`, `oversized_lots`, `bad_plates`,
  `floating_verified`, missing thumbnails, mixed filaments);
- sibling `brickprint` report summary and existing `verify` result.

`include_objects=true` adds per-plate object names (500 per plate max).

### `bambu_slice`

```json
{"path": "/abs/out.3mf", "plates": "all", "timeout_s": 1200,
 "refresh": false, "no_check": false, "render": true}
```

Runs `BambuStudio --slice ... --outputdir ...` and parses `result.json` plus
the G-code header. Returns per plate:

- `print_time_s` (total estimated time) and `model_time_s`;
- `filament_g`, `layers`, `objects`, `triangle_count`;
- `warning_message` / `warnings` (floating regions, cantilever, empty layers,
  path conflicts);
- `gcode` path.

The whole result is cached under `~/Library/Caches/bambu-oracle/slices/` by
source SHA-256, plate selection, `--no-check`, and Bambu version. Set
`refresh=true` after changing the project or to take a second sample (Bambu's
floating check is not deterministic).

`render=true` also returns plate PNGs. `no_check=true` maps to `--no-check`
and should be used sparingly.

### `bambu_preview`

```json
{"path": "/abs/out.3mf", "plates": "all", "refresh": false,
 "camera_view": 0, "output_dir": null}
```

Extracts the `Metadata/plate_N.png` thumbnails already embedded in the 3MF.
If any requested plate is missing, or `refresh=true`, renders 512x512 PNGs with
`--export-png 0`. Returns `{id, png, width, height, bytes, source}`.

### `bambu_verify`

```json
{"path": "/abs/out.3mf", "plates": "all", "profile": "a1mini-petg-008-lego-v8",
 "fit": 0.03, "reference": null, "set": [], "expect": [], "tol": 0.01}
```

Runs `brickprint verify`, which slices with Bambu Studio and checks effective
settings, stud/socket toolpaths, support placement, floating objects and stale
thumbnails. Returns `ok`, `errors`, `warnings`, per-plate part/fit summaries,
and the full JSON report path. Cached by source + arguments.

Use this before telling the user a project is ready to print. `include_parts=true`
returns the raw per-part toolpath measurements.

### `bambu_profiles`

```json
{"action": "list|show|search|diff|effective",
 "kind": "process|filament|machine|all",
 "query": "...", "name": "...", "name_b": "...",
 "path": "/abs/out.3mf", "keys": ["..."], "limit": 50}
```

- `list` — stock Bambu presets and brickprint recipes. Each item has `source`
  (`bambu` or `brickprint`).
- `show` — resolve `inherits` + `include` recursively. Brickprint's
  `base_process`/`base_filament` and `overrides` are merged, so you can ask for
  `layer_height`, `wall_loops`, `thick_bridges`, etc. by their real key.
- `search` — match preset names/descriptions and setting keys (including
  nested brickprint keys such as `overrides.thick_bridges`).
- `diff` — compare two resolved presets.
- `effective` — run `--export-settings` and return the flattened settings Bambu
  will actually use, plus `changed_from_project`.

Use `keys=["*"]` with `show`/`effective` for the full set.

### `bambu_export`

```json
{"path": "/abs/raw.3mf", "output_dir": null,
 "name": "normalized.3mf", "minify": false}
```

Normalises a project through Bambu Studio (the `brickprint --bambu` pass): plate
thumbnails, Bambu metadata, and a valid re-export. Bambu resolves the name
against `--outputdir`; the Oracle always passes a basename so an absolute path
cannot double up and fail.

### `bambu_gcode`

```json
{"path": "/abs/plate_1.gcode", "keys": ["layer_height", "wall_loops"],
 "include_header": true}
```

Reads the header block and effective `CONFIG_BLOCK` from an existing G-code.
Useful when a file came from the Bambu GUI rather than `bambu_slice`.

### `bambu_re`

```json
{"topic": "cli|result|3mf|warnings|profiles|rea|all"}
```

Returns the reverse-engineered contract (`rea` = the Oracle's reverse-engineering notes): CLI flags and quirks, `result.json` schema,
3MF metadata layout, warning strings, profile resolution, and evidence paths.

## Examples

Inspect a finished build:

```text
bambu_project(path="tie_fighter/75300_tie_fighter_PETG_008mm_fit030_v2.3mf")
bambu_slice(path="tie_fighter/75300_tie_fighter_PETG_008mm_fit030_v2.3mf",
            plates="all", render=True)
bambu_preview(path="tie_fighter/75300_tie_fighter_PETG_008mm_fit030_v2.3mf")
bambu_verify(path="tie_fighter/75300_tie_fighter_PETG_008mm_fit030_v2.3mf")
```

Check a setting before overriding it:

```text
bambu_profiles(action="search", query="bridge_speed")
bambu_profiles(action="show", name="a1mini-petg-008-lego-v8",
               keys=["bridge_speed", "thick_bridges", "layer_height"])
bambu_profiles(action="effective", path="out.3mf",
               keys=["bridge_speed", "thick_bridges", "layer_height"])
```

Normalise a raw project:

```text
bambu_export(path="out_raw.3mf", name="out.3mf")
```

## Notes

- The CLI works while Bambu Studio's GUI is open. Do not ask the user to close it.
- The Oracle is stdlib-only; it runs under the system Python and does not need
  Blender or numpy.
- `bambu_slice` reports what one slice saw. Bambu's floating check is not
  deterministic, so `bambu_verify` is the stronger print-readiness check.
- The Oracle never edits the source 3MF. `bambu_export` and `bambu_slice` write
  into the Oracle cache (or an explicit `output_dir`).
