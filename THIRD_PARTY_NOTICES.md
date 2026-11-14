# Third-party notices

brickprint's own code, tests and documentation are released under the MIT
License (see `LICENSE`). The items below come from other parties and keep their
own terms.

## Bundled in this repository

### Bambu Studio stock presets — `assets/a1mini_pla_project_settings.json`

- **What it is:** the resolved project settings of Bambu Studio's stock presets
  "Bambu Lab A1 mini 0.4 nozzle", "0.20mm Standard @BBL A1M" and
  "Bambu PLA Basic @BBL A1M" (Bambu Studio 02.09.01.70), including the
  printer's start, end, filament-change and time-lapse G-code.
- **Source:** <https://github.com/bambulab/BambuStudio>, `resources/profiles/BBL/`.
- **Copyright:** Bambu Lab and the Bambu Studio contributors.
- **Licence:** GNU Affero General Public License v3.0. The full text is in
  `LICENSES/AGPL-3.0.txt`.
- **Changes:** brickprint resolved the three presets' inheritance chain and
  merged them into one project-settings file (`brickprint/bambu_profiles.py`);
  the setting values are unchanged. `brickprint profiles regen-template`
  rewrites the file from the presets of the Bambu Studio installed on your
  machine.

This file is data that brickprint reads and copies into the 3MF projects it
writes. It is the only AGPL-licensed file in the repository.

### Porima PETG data — `assets/profiles/filament/porima-petg-*.json`

The nozzle, bed, fan and drying ranges in these profiles are the values Porima
publishes for its PETG filaments. Each file records the product URL and the
date the values were retrieved. Flow and volumetric-speed values are the
author's own tuning. The profile files themselves are part of brickprint (MIT).

## Used at run time, not bundled

- **LDraw parts library** (<https://library.ldraw.org/>) — CC BY 4.0. Downloaded
  by `brickprint bootstrap` or supplied by you; keep its `CAreadme.txt` and
  attribution files with it.
- **Rebrickable inventories** (<https://rebrickable.com/downloads/>) — public
  CSV dumps, downloaded on first use. Follow Rebrickable's terms; attribution is
  required for public use.
- **LDView** (<https://tcobbs.github.io/ldview/>) — installed separately and
  run as an external program for mesh export.
- **Bambu Studio** (<https://github.com/bambulab/BambuStudio>) — installed
  separately; its command-line interface is run for slicing checks.

## Trademarks

brickprint is not affiliated with or endorsed by the LEGO Group, Rebrickable,
LDraw.org, Bambu Lab or Porima. LEGO is a trademark of the LEGO Group.
