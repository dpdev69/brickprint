# Pipeline details

## Set number normalization

`10337` becomes `10337-1`. Variants such as `010-3` are kept as given. The
Rebrickable `sets.csv` row supplies the name/year; `inventories.csv` supplies
the inventory ID (version 1 preferred); `inventory_parts.csv` supplies the
non-spare lots.

## Part resolution

`LDrawLibrary.resolve()` tries, in order:

1. exact `<part>.dat` in `parts/`, `parts/s/`, `p/`, or `p/48/`;
2. a `0 ~Moved to <other>` alias (up to four hops);
3. print-stripped part (`973pr1234` -> `973`);
4. `part_relationships.csv` parent (printed/mold/replacement);
5. trailing mold letter removal (`60479a` -> `60479`).

Statuses: `exact`, `simplified`, `parent`, `mold_variant`, `missing`. Missing
lots are written to `_missing.csv` and counted in `_report.json`.

## Mesh extraction

LDView is invoked per part with:

```
LDView one.ldr -LDrawDir=<library> -ExportFiles=1 -ExportSuffix=.stl -Seams=0 \
       -UseQualityStuds=1 -AllowPrimitiveSubstitution=1 -CurveQuality=6 -HiResPrimitives=0 \
       -UseStrips=0
```

The geometry flags (`LDVIEW_GEOMETRY` in `ldraw.py`) are always passed so the
mesh never depends on LDView's saved prefs. LDView's defaults (low-quality
studs, CurveQuality 2) export studs, hollow studs and underside tubes as
**octagons** (4.8 mm across vertices, 4.435 across flats) and Technic holes as
16-gons, which print with poor clutch. With the flags above every circle has
48 segments (stud 4.800/4.790, tube 6.400/6.386 vertex/flat).

`-UseStrips=0` is required: with triangle strips on (LDView's default), the STL
export flips every other triangle of LDView's substituted sloped cylinders
(`*cyls*.dat`, used by the `stud2s`/`stud2s2` open-stud tubes and by many
slopes and curved parts, e.g. 4287c, 3665a, 32803, 2310, 3040b, 4286). Those
parts then have inverted facets, wrong volumes and wrong orientation verdicts
(4287c was flagged as needing support). Strips off changes only the triangles
of those primitives (same vertices); every other part is byte-identical.

CurveQuality 6 also opens cracks / non-manifold edges where hand-modelled
16-segment geometry (e.g. `bush.dat`, Technic hole and axle details) meets
48-segment substituted primitives (3713, 41677, 32063, ...). `_export()`
therefore also exports `LDVIEW_BASELINE` (the old default geometry) and, only
when the CurveQuality 6 repaired mesh has more non-manifold edges or open
boundaries than the baseline, uses `LDVIEW_FALLBACK` (quality studs at
CurveQuality 2: 16-sided circles, never octagons) provided that is no worse
than the baseline. About 1 part in 6 (mostly Technic) takes the fallback.

LDView writes ASCII STL in centimetres. The parser scales ×10 to millimetres,
de-duplicates vertices, and shifts the minimum to the origin. `-Seams=0` is
important: LDView's default seam setting shrinks every part by 0.1 mm per side.

Meshes are cached as compressed `.npz` under:

```
~/Library/Caches/brickprint/meshes/<hash of ldraw dir + LDVIEW_GEOMETRY/BASELINE/FALLBACK>/<part>.npz
```

Changing any of these flag sets therefore starts a fresh cache directory; old
directories are left in place (delete them by hand to reclaim space).

The mesh stage (`MeshCache.prepare`, called by `build`) loads every cached
part in the main process and sends only the cache misses (LDView export,
repair, orientation emulator) to a spawn process pool of `mesh_workers`
processes (config key; default min(8, cpus − 2), at least 1). With one
worker, or fewer than two misses, it is the plain serial loop with no pool.
Results, cache files and the `[  50/n]` progress lines are the same and in
the same order as the serial loop; a failing part raises its own error (the
first failing part in input order). Several LDView instances run side by
side safely (each exports into its own temp dir; LDView does not write its
prefs on a headless export). Cache files are written to a temp name and
renamed, so an interrupt never leaves a half-written `.npz`/`.json`; Ctrl-C
terminates the workers, which kill their running LDView first.

## Orientation

`orient.py` evaluates the 24 proper axis-aligned rotations of each part. It
keeps the assembled (studs-up / hard-override) pose unless that pose has:

- an empty 0.2 mm slicer layer,
- a floating region that would need support, or
- a first layer standing on an edge (contact < 5 % of the hull and < 25 mm²).

Only then is the best scoring pose used, and only when it beats the current one
by more than 2 % on the first differing criterion. Hard `ORIENT`/`--orient`
entries are respected unless they are unprintable (labels are matched
case-insensitively; an unknown label stops the build). The slow emulator pass
(metrics for the 6 up directions) is cached per part as
`<part>.c<ORIENT_VERSION>.json`, keyed by the repaired-mesh hash and start
rotation; the final choice is recomputed each run (it is cheap).

## Bambu floating probe (`probe.py`)

The numpy emulator approximates Bambu's floating-region rule but disagrees with
the real slicer on dozens of poses per set, so `build` asks Bambu Studio:

1. after repair and candidate poses, every unique part is posed in each of its
   6 up directions (the exact candidate rotations), one pose per plate,
   centred, written with the build's own writer and project settings
   (supports off);
2. up to 36 poses per probe 3MF, sliced with
   `BambuStudio --slice 0 --arrange 0 --orient 0 --debug 2 --outputdir <dir> probe.3mf`
   (argument list, no shell); several projects run in parallel
   (`probe_workers` in the config, default min(4, cpus/3), at least 1 —
   `probe.default_workers`, shared by the probe, the final-geometry check
   and the plate check, which is also capped by its run count; one
   multi-plate Bambu slice already uses ~6 cores, so do not raise it on a
   10-core machine). Work dirs are removed on every exit path, also on an
   error or interrupt (`BRICKPRINT_PROBE_KEEP=1` keeps them); old leftovers
   are swept at the start of `build` / `verify` (`sweep_days`,
   `verify_keep_gb`, `BRICKPRINT_NO_SWEEP`; see troubleshooting.md);
3. per plate, `plate N: found slicing warnings: ...` log lines give the
   verdict: `floating regions` -> float, `floating cantilever` -> cant,
   `empty layer` -> empty; `plate_N.gcode` existing -> sliced. An empty layer
   aborts the rest of that project, so unsliced plates are re-queued (poses the
   emulator predicts to be empty go last in their project);
4. Bambu's floating check is **not deterministic** (re-slicing the same file
   flips ~5 % of marginal poses; a few poses are flagged in about 2 of 3 runs,
   e.g. 15068 up=-X, 63868 up=+X, 3172 up=-Y in the Countach, although the
   cache had them clean), so a pose counts as clean only when it is
   clean in 3 independent runs (`probe_runs`); clean poses are re-sliced in
   new groupings, and any floating / cantilever / empty verdict is final;
5. verdicts are cached as `<part>.verdicts.json` next to the mesh cache, keyed
   `<mesh hash>:r<REPAIR_VERSION>:o<ORIENT_VERSION>:p<PROBE_VERSION>:<up>`, so
   re-runs are instant and only changed meshes are re-probed;
6. `orient.choose()` gets the verdicts; they outrank the emulator, and
   `needs_support` means Bambu itself flags the chosen pose;
7. ceiling ribs (`--ceilings recipe8`) are new geometry the probe never saw:
   every rib part is sliced as written (2 runs, `probe.check_final`, cached
   by final mesh + per-part settings); a part that floats with its ribs and
   is clean without them in 3 runs loses its ribs (`ribs_dropped` in
   `_report.json` → `features.ceiling_parts`). Countach: 70490's ribs under
   its 11.2 mm dome ceiling were a "floating cantilever" in 5/5 runs, 0/5
   without;
8. plate check: after packing, the whole raw 3MF (profile builds: the
   supports-off verdict copy) is sliced twice (`plate_check_runs`, in
   parallel). Bambu writes one `found slicing warnings` line **per floating
   object**; all of them are collected. On a one-object plate (default
   profile) a flagged plate gets the support-plate overrides as a whole
   (`supports = slice-check`). On plates with one object per part (or per
   colour), only the named objects get per-object `tree(auto)` +
   `bridge_no_support` (profile builds also get socket blockers, see
   `print-tuning.md`), and the flagged plates are re-checked, up to 4
   rounds, until no new object is named (`slice-check: 15068 #1; ...` in
   `_plates.csv`). An empty layer is reported as an error. A plate Bambu
   cannot slice no longer hides the plates after it: they are sliced one by
   one and the failure is reported. Cached in `plate_checks.json` by
   project content.

The probe and the whole-project plate check always slice with supports **off**
(a "verdict copy"; Bambu hides floating warnings when supports are on, even
`normal(manual)` with no enforcer, CLI-verified). The verdict copy carries no
support-enforcer volumes: with supports off Bambu reports an enforced
overhang as floating (60470b alone: 5/5 runs flagged with its enforcer, 0/5
without), while the real file supports exactly that overhang. The
verdict cache key includes a fingerprint of the verdict-relevant settings
(layer height, line widths, compensation, ...). The default profile keeps the
historic key, so existing caches stay valid.

`--no-probe`, or a missing Bambu Studio, skips the probe with a loud
"floating detection is UNVERIFIED" banner; `_report.json` then has
`probe.used = false` and `floating_verified = false` (and no plate check). For
the Countach (238 parts, 1,428 poses) the probe takes ~4.5 min the first time
(86 Bambu runs, 3 workers) and the plate check ~2.7 min; both are cached.
The first build also spends several minutes in the emulator (cached per part).

## Packing

- usable area = `bed - 2 × margin` (default 168 × 168 mm)
- each part is inflated by `gap` when inserted, so adjacent parts have a 1 mm
  clear gap
- sort key: footprint area descending
- free-rectangle choice: bottom-left first (`fy`, then `fx`), which keeps small
  sets compact and does not increase the plate count
- parts with `needs_support` are packed, per color, onto separate
  `"<Color> supports i of n"` plates
- one plate per color group, colors ordered by total part count descending
- colour mixing (`--mix-colours`, `pack.py` constants): a colour's last plate
  is a *tiny group* when its inflated footprints (part + 2 × `pad` + `gap`)
  cover at most `MIX_FILL_MAX` = 35 % of the usable area. Each tiny group is
  first packed into its own compact block (several block widths are tried, the
  smallest bounding box wins; same inflation as normal packing), then the
  blocks are placed, largest first, into shared plates with the same MaxRects
  packer: at most `MIX_MAX_COLOURS` = 4 colours per plate, normal and support
  groups never together. On `Mixed supports` plates blocks are reserved
  `MIX_SUPPORT_BLOCK_GAP` = 15 mm larger, because each colour there is its own
  supported object. A block that finds no partner stays on its colour's own
  plate. Mixed plates come after all single-colour plates.
- `auto` (default): mix only while the project exceeds the plate limit
  (36, or 35 when a `--fit-test` plate follows): support groups first, then
  normal groups; `always`: mix every tiny group; `off`: never
- prime tower: when the profile prints one (`enable_prime_tower 1`, e.g. the
  stock PLA preset), every Mixed plate keeps a `TOWER_KEEPOUT` = 50 × 50 mm
  corner (back right of the usable area) free and the writer sets that
  plate's entry of the per-plate `wipe_tower_x` / `wipe_tower_y` lists to it
  (`TOWER_INSET` 4 mm inside). Without it the template's tower (15, 220)
  lies off the 180 mm bed and Bambu refuses the plate ("G-code outside of the
  printable area", exit 152). The PETG profiles print no prime tower
  (`enable_prime_tower 0`): colour changes purge into the A1 mini's chute.
- if the project still exceeds the limit, the smallest support groups are folded
  into their color's last normal plate (`supports = folded`)

## 3MF structure

- one external `3D/Objects/plate_NN.model` per plate, containing one mesh object
  per unique part on that plate
- root model: one object per plate with one component per part instance
- global object IDs: mesh IDs first, then the plate object ID (Bambu keys meshes
  by object id globally)
- build items placed on Bambu's plate grid:
  - `cols = ceil(sqrt(plate_count))`
  - `stride_x = bed × 1.2`, `stride_y = depth × 1.2`
  - `x = (i % cols) × stride_x`, `y = -(i // cols) × stride_y`

Bambu assigns objects to plates by spatial overlap with its plate beds, not by
the `model_settings` plate IDs. The grid placement is therefore essential.

## Project settings

- `assets/a1mini_pla_project_settings.json` is the A1 mini template.
- 19+ filament arrays and an N×N `flush_volumes_matrix` are generated. A
  mismatched matrix causes Bambu's "Flush volumes matrix do not match to the
  correct size!" slicing error.
- default profile: Textured PEI Plate, PLA, 0.2 mm, stock `brim_type = auto_brim`
  (Bambu adds a brim only where it judges one necessary). Global supports stay off;
  support plates get per-object `enable_support=1`, `support_type=tree(auto)`,
  and `bridge_no_support=1` so brick cavity ceilings remain bridges.
- other profiles (`--profile`, `--filament`, `--fit`, `--set`): composed by
  `settings.py` from the installed Bambu's stock chains, with shaped values and
  a computed `different_settings_to_system`. `--supports enforcers` adds
  `features.py` enforcer volumes, and `--bridging short-span` adds per-part
  bridge angles. See `print-tuning.md`.

## Validation

`brickprint verify` slices with the CLI and checks effective settings,
stud/socket toolpaths, supports and floating (see `print-tuning.md`).

`brickprint validate` recomputes each plate's world-space bbox from the mesh
files + component transforms (4×3 row-major matrices) and checks:

- min x/y ≥ 0
- max x/y ≤ bed/depth
- min z ≈ 0

It also reports component and triangle counts and writes
`orientation_warnings` for parts that still have small bed contact after
auto-orientation. It does not slice.
