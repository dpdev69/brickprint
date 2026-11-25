"""Rebrickable inventory lookup from the public CSV dumps.

The dumps are free to download and need no API key:
https://rebrickable.com/downloads/
"""

from __future__ import annotations

import csv
import gzip
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .net import download

BASE_URL = "https://rebrickable.com/media/downloads/"
FILES = (
    "sets.csv.gz",
    "inventories.csv.gz",
    "inventory_parts.csv.gz",
    "colors.csv.gz",
    "parts.csv.gz",
    "inventory_minifigs.csv.gz",
    "part_relationships.csv.gz",
)
MAX_AGE_DAYS = 14


def _url(name: str) -> str:
    return BASE_URL + name


def _download(name: str, dest: Path) -> None:
    download(_url(name), dest, timeout=300)


def ensure_data(cache_dir: Path, refresh: bool = False) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    todo = []
    for name in FILES:
        dest = cache_dir / name
        stale = not dest.is_file() or (now - dest.stat().st_mtime) > MAX_AGE_DAYS * 86400
        if refresh or stale:
            todo.append((name, dest))
    if not todo:
        return

    def _fetch(item: tuple[str, Path]) -> tuple[str, Path, BaseException | None]:
        name, dest = item
        try:
            _download(name, dest)
            return name, dest, None
        except (RuntimeError, TimeoutError, OSError) as exc:  # net.download raises RuntimeError
            return name, dest, exc

    if len(todo) == 1:
        results = [_fetch(todo[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(todo))) as ex:
            results = list(ex.map(_fetch, todo))

    for name, dest, exc in results:
        if exc is not None:
            if not dest.is_file():
                raise SystemExit(f"could not download {name}: {exc}") from exc
            print(f"warning: keeping cached {name}: {exc}")


def _rows(cache_dir: Path, name: str):
    with gzip.open(cache_dir / name, "rt", encoding="utf-8", errors="replace", newline="") as f:
        yield from csv.DictReader(f)


def normalize_set_num(set_num: str) -> str:
    value = set_num.strip()
    if not value:
        raise SystemExit("empty set number")
    if "-" not in value:
        value += "-1"
    return value


def load_sets(cache_dir: Path) -> dict[str, dict]:
    return {r["set_num"]: r for r in _rows(cache_dir, "sets.csv.gz")}


def load_colors(cache_dir: Path) -> dict[str, dict]:
    colors = {}
    for r in _rows(cache_dir, "colors.csv.gz"):
        colors[r["id"]] = {
            "id": r["id"],
            "name": r["name"],
            "rgb": r.get("rgb", "CCCCCC"),
            "is_trans": str(r.get("is_trans", "False")).lower() == "true",
        }
    return colors


def load_part_names(cache_dir: Path) -> dict[str, str]:
    names = {}
    for r in _rows(cache_dir, "parts.csv.gz"):
        names[r["part_num"].lower()] = r["name"]
    return names


def load_relationships(cache_dir: Path) -> dict[str, list[tuple[str, str]]]:
    rel = defaultdict(list)
    path = cache_dir / "part_relationships.csv.gz"
    if not path.is_file():
        return rel
    for r in _rows(cache_dir, "part_relationships.csv.gz"):
        rel[r["child_part_num"].lower()].append((r["rel_type"], r["parent_part_num"]))
    return rel


def _inventories_for_many(cache_dir: Path, set_nums: set[str]) -> dict[str, list[dict]]:
    """Single-pass scan of ``inventories.csv.gz`` for all ``set_nums``."""
    out: dict[str, list[dict]] = {s: [] for s in set_nums}
    if not set_nums:
        return out
    with gzip.open(cache_dir / "inventories.csv.gz", "rt", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return out
        try:
            idx_set = header.index("set_num")
        except ValueError:
            return out
        for row in reader:
            if len(row) > idx_set and row[idx_set] in set_nums:
                out[row[idx_set]].append(dict(zip(header, row)))
    return out


def _inventories_for(cache_dir: Path, set_num: str) -> list[dict]:
    return _inventories_for_many(cache_dir, {set_num}).get(set_num, [])


def _inventory_parts_many(
    cache_dir: Path,
    inventory_ids: set[str],
    include_spares: bool,
) -> dict[str, list[dict]]:
    """Single-pass prefix-filtered scan of ``inventory_parts.csv.gz`` for all
    ``inventory_ids`` (avoids building ~1.3M ``csv.DictReader`` dicts)."""
    out: dict[str, list[dict]] = {i: [] for i in inventory_ids}
    if not inventory_ids:
        return out
    with gzip.open(cache_dir / "inventory_parts.csv.gz", "rt", encoding="utf-8", errors="replace", newline="") as f:
        first = f.readline()
        if not first:
            return out
        header = next(csv.reader([first]))
        idx_inv = header.index("inventory_id")
        idx_sp = header.index("is_spare") if "is_spare" in header else -1
        if idx_inv == 0:
            # Fast path: inventory_id is column 0 in Rebrickable dumps
            for line in f:
                comma = line.find(",")
                if comma < 0:
                    continue
                inv_id = line[:comma]
                if inv_id not in inventory_ids:
                    if inv_id.startswith('"') and inv_id.strip('"') in inventory_ids:
                        inv_id = inv_id.strip('"')
                    else:
                        continue
                row = next(csv.reader([line]))
                if idx_sp >= 0 and len(row) > idx_sp and row[idx_sp].lower() == "true" and not include_spares:
                    continue
                out[row[0]].append(dict(zip(header, row)))
        else:
            for row in csv.reader(f):
                if len(row) <= idx_inv or row[idx_inv] not in inventory_ids:
                    continue
                if idx_sp >= 0 and len(row) > idx_sp and row[idx_sp].lower() == "true" and not include_spares:
                    continue
                out[row[idx_inv]].append(dict(zip(header, row)))
    return out


def _inventory_parts(cache_dir: Path, inventory_id: str, include_spares: bool) -> list[dict]:
    return _inventory_parts_many(cache_dir, {inventory_id}, include_spares).get(inventory_id, [])


def _choose_inventory(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    for r in rows:
        if str(r.get("version")) == "1":
            return r
    return sorted(rows, key=lambda r: int(r.get("version") or 0))[0]


def lookup_set(
    cache_dir: Path,
    set_num: str,
    include_spares: bool = False,
    include_minifigs: bool = False,
) -> dict:
    wanted = normalize_set_num(set_num)
    sets = load_sets(cache_dir)
    row = sets.get(wanted)
    if not row:
        # Case-insensitive fallback.
        for key, value in sets.items():
            if key.lower() == wanted.lower():
                row, wanted = value, key
                break
    if not row:
        raise SystemExit(f"set not found in Rebrickable data: {set_num}")

    inventories = _inventories_for(cache_dir, wanted)
    inventory = _choose_inventory(inventories)
    if not inventory:
        raise SystemExit(f"no inventory found for {wanted}")

    minifigs = [
        r for r in _rows(cache_dir, "inventory_minifigs.csv.gz")
        if r["inventory_id"] == inventory["id"]
    ]

    # Resolve all minifig inventories in ONE pass so inventory_parts.csv.gz is
    # scanned only ONCE for the set and every minifig combined.
    fig_invs: dict[str, dict | None] = {}
    wanted_inv_ids = {inventory["id"]}
    if include_minifigs and minifigs:
        fig_nums = {fig["fig_num"] for fig in minifigs}
        by_fig = _inventories_for_many(cache_dir, fig_nums)
        for fig_num in fig_nums:
            finv = _choose_inventory(by_fig.get(fig_num, []))
            fig_invs[fig_num] = finv
            if finv:
                wanted_inv_ids.add(finv["id"])

    parts_by_inv = _inventory_parts_many(cache_dir, wanted_inv_ids, include_spares)
    raw = parts_by_inv.get(inventory["id"], [])
    if not raw:
        raise SystemExit(f"inventory {inventory['id']} for {wanted} is empty")

    lots = defaultdict(int)
    for r in raw:
        lots[(r["part_num"], r["color_id"])] += int(r["quantity"])

    expanded_minifigs = []
    if include_minifigs and minifigs:
        for fig in minifigs:
            fig_num = fig["fig_num"]
            fig_qty = int(fig["quantity"])
            fig_inv = fig_invs.get(fig_num)
            if not fig_inv:
                expanded_minifigs.append({"fig_num": fig_num, "quantity": fig_qty, "status": "no inventory"})
                continue
            for r in parts_by_inv.get(fig_inv["id"], []):
                key = (r["part_num"], r["color_id"])
                lots[key] += fig_qty * int(r["quantity"])
            expanded_minifigs.append({"fig_num": fig_num, "quantity": fig_qty, "status": "expanded"})

    colors = load_colors(cache_dir)
    names = load_part_names(cache_dir)

    def color_info(color_id: str) -> dict:
        c = colors.get(str(color_id))
        if c:
            return c
        return {"id": str(color_id), "name": f"Color {color_id}", "rgb": "CCCCCC", "is_trans": False}

    bom_rows = []
    for (part_num, color_id), qty in sorted(lots.items()):
        color = color_info(color_id)
        bom_rows.append({
            "part_num": part_num,
            "color_id": str(color_id),
            "quantity": qty,
            "part_name": names.get(part_num.lower(), ""),
            "color_name": color["name"],
            "rgb": color["rgb"],
            "is_trans": color["is_trans"],
        })

    return {
        "set_num": wanted,
        "name": row.get("name", wanted),
        "year": row.get("year", ""),
        "num_parts": int(row.get("num_parts") or 0),
        "inventory_id": inventory["id"],
        "lots": bom_rows,
        "colors": colors,
        "part_names": names,
        "minifigs": minifigs,
        "expanded_minifigs": expanded_minifigs,
        "relationships": load_relationships(cache_dir),
        "cache_dir": str(cache_dir),
    }


def write_bom_csv(path: Path, lots: list[dict]) -> None:
    fields = ["part_num", "color_id", "quantity", "part_name", "color_name", "rgb", "is_trans"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(lots)
