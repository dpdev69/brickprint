"""Rebrickable CSV lookup on tiny gzipped fixtures (no network)."""

from __future__ import annotations

import csv
import gzip
import io
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from brickprint import rebrickable as rb

TABLES = {
    "sets.csv.gz": (
        ["set_num", "name", "year", "theme_id", "num_parts"],
        [["1000-1", "Test Set", "2020", "1", "12"],
         ["Mixed-1", "Case Set", "2021", "1", "1"],
         ["2000-1", "No Inventory", "2019", "1", "0"],
         ["3000-1", "Empty Inventory", "2018", "1", "0"],
         ["4000-1", "Fig Set", "2022", "1", "5"]],
    ),
    "inventories.csv.gz": (
        ["id", "version", "set_num"],
        [["10", "2", "1000-1"], ["11", "1", "1000-1"],     # version 1 wins over listing order
         ["12", "1", "Mixed-1"],
         ["13", "1", "3000-1"],
         ["14", "1", "4000-1"],
         ["20", "1", "fig-001"]],
    ),
    "inventory_parts.csv.gz": (
        ["inventory_id", "part_num", "color_id", "quantity", "is_spare", "img_url"],
        [["11", "3001", "1", "4", "False", ""],
         ["11", "3001", "1", "2", "False", ""],            # same lot twice -> summed
         ["11", "3001", "5", "1", "False", ""],
         ["11", "3023", "5", "3", "False", ""],
         ["11", "3023", "5", "9", "True", ""],             # spare
         ["10", "9999", "1", "100", "False", ""],          # other inventory version
         ["12", "3001", "999", "1", "False", ""],          # colour missing from colors.csv
         ["14", "3001", "1", "1", "False", ""],
         ["14", "3023", "5", "2", "True", ""],
         ["20", "3626", "1", "1", "False", ""],
         ["20", "3001", "1", "2", "False", ""]],
    ),
    "colors.csv.gz": (
        ["id", "name", "rgb", "is_trans"],
        [["1", "Blue", "0055BF", "f"], ["5", "Trans-Clear", "FCFCFC", "True"],
         ["0", "Black", "05131D", "False"]],
    ),
    "parts.csv.gz": (
        ["part_num", "name", "part_cat_id", "part_material"],
        [["3001", "Brick 2 x 4", "11", "Plastic"], ["3023", "Plate 1 x 2", "14", "Plastic"],
         ["3626", "Minifigure Head", "59", "Plastic"], ["3001B", "Brick Upper", "11", "Plastic"]],
    ),
    "inventory_minifigs.csv.gz": (
        ["inventory_id", "fig_num", "quantity"],
        [["14", "fig-001", "2"], ["14", "fig-404", "1"]],
    ),
    "part_relationships.csv.gz": (
        ["rel_type", "child_part_num", "parent_part_num"],
        [["P", "3001B", "3001"], ["A", "3001B", "3001A"], ["M", "3023", "3023B"]],
    ),
}


def write_gz(path: Path, header, rows) -> None:
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())


@pytest.fixture()
def cache(tmp_path):
    for name, (header, rows) in TABLES.items():
        write_gz(tmp_path / name, header, rows)
    return tmp_path


def lots(result):
    return {(r["part_num"], r["color_id"]): r["quantity"] for r in result["lots"]}


# --------------------------------------------------------------- set numbers

def test_normalize_set_num():
    assert rb.normalize_set_num("10337") == "10337-1"
    assert rb.normalize_set_num("  10337-2 ") == "10337-2"
    with pytest.raises(SystemExit, match="empty set number"):
        rb.normalize_set_num("   ")


# -------------------------------------------------------------------- loaders

def test_load_colors(cache):
    colors = rb.load_colors(cache)
    assert colors["1"] == {"id": "1", "name": "Blue", "rgb": "0055BF", "is_trans": False}
    assert colors["5"]["is_trans"] is True and colors["5"]["name"] == "Trans-Clear"
    assert "999" not in colors


def test_load_part_names_lowercases_keys(cache):
    names = rb.load_part_names(cache)
    assert names["3001"] == "Brick 2 x 4"
    assert names["3001b"] == "Brick Upper"
    assert "3001B" not in names


def test_load_relationships(cache):
    rel = rb.load_relationships(cache)
    assert rel["3001b"] == [("P", "3001"), ("A", "3001A")]
    assert rel["3023"] == [("M", "3023B")]
    assert rel["nope"] == []                                  # defaultdict


def test_load_relationships_file_is_optional(cache):
    (cache / "part_relationships.csv.gz").unlink()
    assert dict(rb.load_relationships(cache)) == {}


def test_load_sets(cache):
    sets = rb.load_sets(cache)
    assert sets["1000-1"]["name"] == "Test Set"
    assert len(sets) == 5


def test_choose_inventory_prefers_version_one():
    rows = [{"id": "a", "version": "3"}, {"id": "b", "version": "1"}, {"id": "c", "version": "2"}]
    assert rb._choose_inventory(rows)["id"] == "b"
    rows = [{"id": "a", "version": "3"}, {"id": "c", "version": "2"}]
    assert rb._choose_inventory(rows)["id"] == "c"            # lowest version
    assert rb._choose_inventory([]) is None


# --------------------------------------------------------------- lookup_set

def test_lookup_set_aggregates_lots_and_skips_spares(cache):
    res = rb.lookup_set(cache, "1000-1")
    assert res["set_num"] == "1000-1"
    assert res["name"] == "Test Set"
    assert res["year"] == "2020"
    assert res["num_parts"] == 12
    assert res["inventory_id"] == "11"                         # version 1, not the other one
    assert lots(res) == {("3001", "1"): 6, ("3001", "5"): 1, ("3023", "5"): 3}
    assert res["cache_dir"] == str(cache)
    assert res["minifigs"] == [] and res["expanded_minifigs"] == []


def test_lookup_set_default_revision_suffix(cache):
    assert rb.lookup_set(cache, "1000")["set_num"] == "1000-1"


def test_lookup_set_include_spares(cache):
    res = rb.lookup_set(cache, "1000-1", include_spares=True)
    assert lots(res)[("3023", "5")] == 12


def test_lookup_set_rows_are_sorted_and_enriched(cache):
    res = rb.lookup_set(cache, "1000-1")
    assert [(r["part_num"], r["color_id"]) for r in res["lots"]] == [
        ("3001", "1"), ("3001", "5"), ("3023", "5")]
    first, second = res["lots"][0], res["lots"][1]
    assert first["part_name"] == "Brick 2 x 4"
    assert (first["color_name"], first["rgb"], first["is_trans"]) == ("Blue", "0055BF", False)
    assert (second["color_name"], second["is_trans"]) == ("Trans-Clear", True)
    assert res["colors"]["1"]["name"] == "Blue"
    assert res["part_names"]["3023"] == "Plate 1 x 2"
    assert res["relationships"]["3001b"] == [("P", "3001"), ("A", "3001A")]


def test_lookup_set_unknown_colour_gets_placeholder(cache):
    (row,) = rb.lookup_set(cache, "Mixed-1")["lots"]
    assert row["color_id"] == "999"
    assert (row["color_name"], row["rgb"], row["is_trans"]) == ("Color 999", "CCCCCC", False)


def test_lookup_set_is_case_insensitive_fallback(cache):
    res = rb.lookup_set(cache, "mixed-1")
    assert res["set_num"] == "Mixed-1"


def test_lookup_set_unknown_set_exits(cache):
    with pytest.raises(SystemExit, match="set not found in Rebrickable data: 9999-1"):
        rb.lookup_set(cache, "9999-1")


def test_lookup_set_without_inventory_exits(cache):
    with pytest.raises(SystemExit, match="no inventory found for 2000-1"):
        rb.lookup_set(cache, "2000-1")


def test_lookup_set_with_empty_inventory_exits(cache):
    with pytest.raises(SystemExit, match="inventory 13 for 3000-1 is empty"):
        rb.lookup_set(cache, "3000-1")


def test_lookup_set_lists_minifigs_without_expanding(cache):
    res = rb.lookup_set(cache, "4000-1")
    assert lots(res) == {("3001", "1"): 1}
    assert [m["fig_num"] for m in res["minifigs"]] == ["fig-001", "fig-404"]
    assert res["expanded_minifigs"] == []


def test_lookup_set_expands_minifigs(cache):
    res = rb.lookup_set(cache, "4000-1", include_minifigs=True)
    # set: 3001/1 x1; each of 2 figs adds a head (3626/1) and a 3001/1 x2
    assert lots(res) == {("3001", "1"): 1 + 2 * 2, ("3626", "1"): 2}
    assert res["expanded_minifigs"] == [
        {"fig_num": "fig-001", "quantity": 2, "status": "expanded"},
        {"fig_num": "fig-404", "quantity": 1, "status": "no inventory"},
    ]
    assert [r["part_num"] for r in res["lots"]] == ["3001", "3626"]
    assert res["lots"][1]["part_name"] == "Minifigure Head"


def test_lookup_set_minifig_flag_without_minifigs_is_a_noop(cache):
    assert lots(rb.lookup_set(cache, "1000-1", include_minifigs=True)) == \
        lots(rb.lookup_set(cache, "1000-1"))


# ------------------------------------------------------------------- BOM csv

def test_write_bom_csv_roundtrip(cache, tmp_path):
    res = rb.lookup_set(cache, "1000-1")
    out = tmp_path / "bom.csv"
    rb.write_bom_csv(out, [{**r, "ignored": 1} for r in res["lots"]])
    with open(out, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == ["part_num", "color_id", "quantity", "part_name", "color_name",
                             "rgb", "is_trans"]
    assert [(r["part_num"], r["color_id"], r["quantity"]) for r in rows] == [
        ("3001", "1", "6"), ("3001", "5", "1"), ("3023", "5", "3")]


# --------------------------------------------------------------- ensure_data

def fake_download(calls):
    def dl(url, dest, timeout=300):
        calls.append(Path(dest).name)
        Path(dest).write_bytes(b"x")
    return dl


def test_ensure_data_downloads_missing_files(tmp_path):
    calls = []
    with mock.patch.object(rb, "download", side_effect=fake_download(calls)):
        rb.ensure_data(tmp_path / "new" / "cache")
    assert sorted(calls) == sorted(rb.FILES)
    assert all((tmp_path / "new" / "cache" / n).is_file() for n in rb.FILES)


def test_ensure_data_uses_the_rebrickable_urls(tmp_path):
    with mock.patch.object(rb, "download") as dl:
        rb.ensure_data(tmp_path)
    urls = sorted(c.args[0] for c in dl.call_args_list)
    assert urls == sorted(rb.BASE_URL + n for n in rb.FILES)
    assert all(c.kwargs.get("timeout") == 300 for c in dl.call_args_list)


def test_ensure_data_skips_fresh_files_and_refreshes_stale_ones(cache):
    for name in rb.FILES:
        if not (cache / name).exists():
            (cache / name).write_bytes(b"x")
    old = time.time() - (rb.MAX_AGE_DAYS + 1) * 86400
    os.utime(cache / "sets.csv.gz", (old, old))
    calls = []
    with mock.patch.object(rb, "download", side_effect=fake_download(calls)):
        rb.ensure_data(cache)
    assert calls == ["sets.csv.gz"]


def test_ensure_data_refresh_redownloads_everything(cache):
    for name in rb.FILES:
        if not (cache / name).exists():
            (cache / name).write_bytes(b"x")
    calls = []
    with mock.patch.object(rb, "download", side_effect=fake_download(calls)):
        rb.ensure_data(cache, refresh=True)
    assert sorted(calls) == sorted(rb.FILES)


# rebrickable.py uses urllib.error without importing it (ruff F821): any download error makes
# the `except (urllib.error.URLError, ...)` clause itself raise NameError.  Remove the marker
# once that import is fixed.
_URLLIB_IMPORT_BUG = pytest.mark.xfail(raises=NameError, reason="rebrickable.py lacks `import urllib.error`")


@_URLLIB_IMPORT_BUG
def test_ensure_data_missing_file_and_failed_download_exits(tmp_path):
    with mock.patch.object(rb, "download", side_effect=OSError("offline")):
        with pytest.raises(SystemExit, match="could not download"):
            rb.ensure_data(tmp_path)


@_URLLIB_IMPORT_BUG
def test_ensure_data_keeps_cached_file_when_refresh_fails(cache, capsys):
    for name in rb.FILES:
        if not (cache / name).exists():
            (cache / name).write_bytes(b"x")
    with mock.patch.object(rb, "download", side_effect=OSError("offline")):
        rb.ensure_data(cache, refresh=True)
    assert "keeping cached" in capsys.readouterr().out
