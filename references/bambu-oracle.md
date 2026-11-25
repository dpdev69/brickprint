# Bambu Oracle MCP

> **Optional companion, not part of this repository.** `bambu-oracle` is a
> separate MCP server distributed on its own and may not be available in your
> setup. The `brickprint` skill works without it through the `brickprint` CLI
> alone (`verify`, `profiles`, ...). Read this file only if your agent has the
> `bambu_*` tools.

`bambu-oracle` wraps the installed Bambu Studio CLI and `brickprint verify` as
structured MCP tools. Use them instead of ad-hoc `python3` scripts after a
`brickprint build`.

**Important:** Always pass **absolute paths** to `path` and `reference`. A
global stdio MCP server resolves relative paths against the server process CWD,
which may not be your workspace.

## Tool reference

### `bambu_doctor`

```json
{"check_brickprint": true, "check_rea": true}
```

Checks the Bambu CLI, stock profile tree, brickprint launcher, RE evidence
files (`check_rea`), Oracle cache size by category, and read-only brickprint
leftover work dirs (`platecheck_*`, `probe_*`, `final_*`,
`brickprint_verify_*`). Use it first when a tool fails.

### `bambu_project`

```json
{"path": "/abs/out.3mf", "plates": "all", "include_objects": false}
```

Read-only 3MF inspection. Returns:

- printer / process / filament settings and `different_settings_to_system`;
- every plate's name, object/part/triangle counts, extruders, colours, embedded
  thumbnail status, and (when sibling `_plates.csv` exists) bed size, max Z,
  and support flag;
- totals, warnings (`missing_lots`, `oversized_lots`, `bad_plates`,
  `floating_verified`, missing thumbnails, mixed filaments), cached slice info,
  and sibling `_report.json` / `_verify.json` summaries.

`include_objects=true` adds per-plate object names (up to 500 per plate).

### `bambu_slice`

```json
{"path": "/abs/out.3mf", "plates": "all", "timeout_s": 1200,
 "refresh": false, "no_check": false, "render": false,
 "camera_view": 0, "include_objects": false}
```

Runs `BambuStudio --slice ... --outputdir ...` and parses `result.json` plus
the G-code header. Returns per plate: `print_time_s`, `model_time_s`,
`filament_g`, `layers`, `objects`, `triangle_count`, `warnings`, and `gcode`
path.

- **Caching:** Stored under `~/Library/Caches/bambu-oracle/slices/` (Linux:
  `~/.cache/bambu-oracle/slices/`) keyed by source SHA-256, `--no-check`, and
  Bambu version. Only successful slices whose G-code still exists are served;
  failed runs are never cached as hits.
- **Shared per-plate reuse:** Subset requests (`plates="1,3-5"`) are answered
  from an earlier all-plates slice or other cached subsets without invoking
  Bambu (`served_from` / `from_all_plates`). On a partial miss, `slice_plan`
  slices only uncached plates (or one all-plates run when cheaper; a 1-plate
  request never starts an all-plates run). `refresh=true` re-slices exactly the
  requested plates.
- **`render` defaults to `false`** (faster). Set `render=true` only when you
  also want PNG previews. `no_check=true` maps to `--no-check` (use sparingly).

### `bambu_preview`

```json
{"path": "/abs/out.3mf", "plates": "all", "refresh": false,
 "camera_view": 0, "output_dir": null}
```

Returns `{id, png, width, height, bytes, source}`:

- When `camera_view=0` and `refresh=false`, extracts embedded
  `Metadata/plate_N.png` thumbnails from the 3MF if all requested plates have
  them.
- Otherwise reuses cached rendered PNGs or renders 512×512 PNGs via Bambu
  Studio (`render_plan`: `--export-png N` for single plates, `--export-png 0`
  for larger subsets in a staging dir so only requested plates are returned or
  copied to `output_dir`).

### `bambu_verify`

```json
{"path": "/abs/out.3mf", "plates": "all", "profile": "a1mini-petg-008-lego-v8",
 "fit": 0.03, "reference": null, "set": [], "expect": [], "tol": 0.01,
 "timeout_s": 1800, "refresh": false, "include_parts": false}
```

Runs `brickprint verify` and returns `ok`, `errors`, `warnings`, per-plate
part/fit summaries, and the full JSON report path. Accepts plate ranges
(`"1-3"`) and contour/hole fit strings (`"0.03,-0.05"`). Cached by source SHA,
arguments, Bambu version, brickprint code/profiles, and sibling report; real
`FAIL` verdicts are served from cache while slicer infrastructure failures are
re-run. Use before calling a project ready to print. `include_parts=true`
returns raw per-part toolpath measurements.

### `bambu_profiles`

```json
{"action": "list|show|search|diff|effective",
 "kind": "process|filament|machine|all",
 "query": "...", "name": "...", "name_b": "...",
 "path": "/abs/out.3mf", "keys": ["..."], "limit": 50, "refresh": false}
```

- `list` — stock Bambu presets, bundled brickprint recipes, and user profiles
  in `~/.config/brickprint/profiles/`.
- `show` — resolves `inherits` + `include` + brickprint `base_*` / `overrides`.
  Supports exact keys, globs (e.g. `["*bridge*"]`), or `["*"]`.
- `search` — matches preset names/descriptions and setting keys.
- `diff` — compares two resolved presets (`name` vs `name_b`).
- `effective` — runs `--export-settings` on `path` and returns flattened
  effective settings plus `changed_from_project`.

### `bambu_export`

```json
{"path": "/abs/raw.3mf", "output_dir": null,
 "name": "normalized.3mf", "minify": false, "refresh": false}
```

Normalises a 3MF through Bambu Studio (plate thumbnails and Bambu metadata),
passing a basename against `--outputdir` to avoid Bambu's absolute-path quirk.
Cached by source SHA, Bambu version, `minify`, and `name` (copied into
`output_dir` when given). Refuses to overwrite its own source file.

### `bambu_gcode`

```json
{"path": "/abs/plate_1.gcode", "keys": ["layer_height", "wall_loops"],
 "include_header": true}
```

Reads header stats and effective `CONFIG_BLOCK` from any Bambu G-code file
(supports exact keys, globs, or `["*"]`).

### `bambu_re`

```json
{"topic": "cli|result|3mf|warnings|profiles|rea|all"}
```

Returns the reverse-engineered Bambu Studio CLI/3MF/result/warnings/profiles
contract and evidence paths.

## Examples

Inspect and verify a finished build (use absolute paths):

```text
bambu_project(path="/abs/path/to/project.3mf")
bambu_slice(path="/abs/path/to/project.3mf", plates="all", render=false)
bambu_preview(path="/abs/path/to/project.3mf")
bambu_verify(path="/abs/path/to/project.3mf")
```

Check a setting before overriding it:

```text
bambu_profiles(action="search", query="bridge_speed")
bambu_profiles(action="show", name="a1mini-petg-008-lego-v8",
               keys=["bridge_speed", "thick_bridges", "layer_height"])
bambu_profiles(action="effective", path="/abs/path/to/project.3mf",
               keys=["bridge_speed", "thick_bridges", "layer_height"])
```

Normalise a raw project:

```text
bambu_export(path="/abs/path/to/project_raw.3mf", name="project.3mf")
```

## Notes

- The CLI works while Bambu Studio's GUI is open. Do not ask the user to close it.
- The Oracle is stdlib-only Python (no Blender or numpy required).
- `bambu_slice` reports what one slice saw; because Bambu's floating check is
  non-deterministic, `bambu_verify` is the stronger print-readiness check.
- The Oracle never edits the source 3MF; `bambu_export` and `bambu_slice` write
  into the Oracle cache (or an explicit `output_dir`).
