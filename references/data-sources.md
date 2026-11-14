# Data sources

## Rebrickable

Download page: <https://rebrickable.com/downloads/>

The tool downloads these gzip CSV files into
`~/Library/Caches/brickprint/rebrickable/`:

| file | use |
|---|---|
| `sets.csv.gz` | set name, year, part count |
| `inventories.csv.gz` | set → inventory ID/version |
| `inventory_parts.csv.gz` | part, color, quantity, spare flag |
| `colors.csv.gz` | color name, RGB, transparency |
| `parts.csv.gz` | part names |
| `inventory_minifigs.csv.gz` | minifigure contents of a set |
| `part_relationships.csv.gz` | printed/mold/replacement mappings |

No API key is required. The dumps are refreshed when older than 14 days (or with
`--refresh`). Attribution and terms: see Rebrickable's site.

## LDraw

Library: <https://library.ldraw.org/>

`brickprint bootstrap` downloads `complete.zip` from
<https://library.ldraw.org/library/updates/complete.zip> and extracts it under
`~/Library/Caches/brickprint/ldraw/ldraw`.

To use a library you already have, set `ldraw_dir` in
`~/.config/brickprint/config.json` or export `BRICKPRINT_LDRAW`; the folder must
contain `LDConfig.ldr`, `parts/` and `p/`. Without either, brickprint looks in a
few standard locations (`~/Library/Application Support/LDraw/ldraw`,
`/Applications/LDraw/ldraw`, `~/ldraw`, the cache folder) and in an
`ldraw_library/ldraw` folder of a parent directory of the skill if one exists.
`brickprint doctor` shows which one was resolved.

LDraw parts are CC BY 4.0. Keep `CAreadme.txt` and the attribution files with
the library.

## LDView

LDView is used for BFC-correct, exact-dimension mesh export. It is not bundled:
install it yourself. brickprint finds `/Applications/LDView.app` or an
`LDView` / `ldview` executable on `PATH` (or a `tools/LDView.app` folder of a
parent directory of the skill, if one exists). Otherwise set `ldview` in
`~/.config/brickprint/config.json` or `BRICKPRINT_LDVIEW` to the executable path
(on macOS `.../LDView.app/Contents/MacOS/LDView`).

## Bambu Studio

Optional. Used only for the final thumbnail/normalisation pass:

```
BambuStudio project_raw.3mf --outputdir <writable-dir> --export-3mf project.3mf
```

The raw 3MF is valid without this pass. The CLI export works while the Bambu
Studio GUI is open (CLI-verified), so there is no need to close it.
