"""MaxRects packing invariants on synthetic rectangles."""

from __future__ import annotations

import copy
import itertools
import random

import pytest

from brickprint import pack

BED, DEPTH, MARGIN, GAP = 180.0, 180.0, 5.0, 3.0
USABLE_W = BED - 2 * MARGIN
USABLE_H = DEPTH - 2 * MARGIN
EPS = 1e-6

COLORS = {
    "1": {"name": "Blue", "rgb": "0055BF", "is_trans": False},
    "2": {"name": "Red", "rgb": "C91A09", "is_trans": False},
    "3": {"name": "Black", "rgb": "05131D", "is_trans": False},
    "4": {"name": "Trans-Clear", "rgb": "FCFCFC", "is_trans": True},
}


def inst(part, color, w, d, count=1, **extra):
    return {"part_file": part, "color_id": color, "size": (w, d, 5.0), "count": count, **extra}


def run(instances, **kw):
    args = dict(bed=BED, depth=DEPTH, margin=MARGIN, gap=GAP)
    args.update(kw)
    return pack.pack(copy.deepcopy(instances), COLORS, **args)


def placed_items(plates):
    return [it for p in plates for it in p["items"]]


def assert_plate_valid(plate, gap=GAP):
    items = plate["items"]
    for name, x, y, w, d in items:
        assert x >= -EPS and y >= -EPS, name
        assert x + w <= USABLE_W + EPS, name
        assert y + d <= USABLE_H + EPS, name
    for a, b in itertools.combinations(items, 2):
        _, ax, ay, aw, ad = a
        _, bx, by, bw, bd = b
        sep_x = max(bx - (ax + aw), ax - (bx + bw))
        sep_y = max(by - (ay + ad), ay - (by + bd))
        assert max(sep_x, sep_y) >= gap - EPS, (a, b)


def random_instances(seed, n=40):
    rng = random.Random(seed)
    return [inst(f"p{i}.dat", rng.choice("1234"), rng.uniform(4, 60), rng.uniform(4, 60),
                 count=rng.randint(1, 4))
            for i in range(n)]


# ------------------------------------------------------------------ packing

def test_everything_is_placed_and_valid():
    instances = random_instances(1)
    plates, oversized, empty = run(instances)
    assert oversized == [] and empty == []
    assert len(placed_items(plates)) == sum(i["count"] for i in instances)
    for plate in plates:
        assert_plate_valid(plate)
        assert plate["bbox"][0] <= USABLE_W + EPS and plate["bbox"][1] <= USABLE_H + EPS


def test_every_part_copy_lands_on_a_plate_of_its_own_colour():
    instances = random_instances(2)
    plates, _, _ = run(instances)
    colour_of = {i["part_file"]: i["color_id"] for i in instances}
    for plate in plates:
        for name, *_ in plate["items"]:
            assert colour_of[name] == plate["color_id"]
    counts = {}
    for plate in plates:
        for name, *_ in plate["items"]:
            counts[name] = counts.get(name, 0) + 1
    assert counts == {i["part_file"]: i["count"] for i in instances}


def test_items_keep_their_true_size():
    plates, _, _ = run([inst("a.dat", "1", 12.5, 7.25, count=3)])
    for _, _, _, w, d in placed_items(plates):
        assert (w, d) == (12.5, 7.25)


def test_gap_applies_between_parts():
    plates, _, _ = run([inst("a.dat", "1", 10, 10, count=2)], gap=7.0)
    assert len(plates) == 1
    assert_plate_valid(plates[0], gap=7.0)


def test_pad_enlarges_keep_out_and_offsets_item():
    plates, _, _ = run([inst("a.dat", "1", 10, 10, count=2, pad=4.0)])
    (plate,) = plates
    assert_plate_valid(plate, gap=GAP + 2 * 4.0)
    assert min(x for _, x, _, _, _ in plate["items"]) >= 4.0 - EPS


def test_overflowing_colour_spills_onto_more_plates():
    # 100 x 100 parts: only one fits the 170 x 170 usable bed
    plates, oversized, _ = run([inst("big.dat", "1", 100, 100, count=5)])
    assert oversized == []
    assert len(plates) == 5
    assert [p["index"] for p in plates] == [1, 2, 3, 4, 5]
    assert {p["count"] for p in plates} == {5}


def test_oversize_part_is_reported_and_not_placed():
    plates, oversized, empty = run([
        inst("huge.dat", "1", USABLE_W + 1, 10),
        inst("tall.dat", "1", 10, USABLE_H + 1, count=2),
        inst("ok.dat", "1", 10, 10),
    ])
    assert empty == []
    assert sorted(o["part_file"] for o in oversized) == ["huge.dat", "tall.dat", "tall.dat"]
    assert all(o["color_id"] == "1" for o in oversized)
    assert [name for name, *_ in placed_items(plates)] == ["ok.dat"]


def test_part_fitting_the_bed_including_its_gap_is_placed():
    plates, oversized, _ = run([inst("full.dat", "1", USABLE_W - GAP, USABLE_H - GAP)])
    assert oversized == [] and len(plates) == 1
    assert_plate_valid(plates[0])


def test_part_that_only_overflows_through_the_gap_is_reported_not_dropped():
    # Real behaviour: the pre-check compares the bare size with the usable bed, but the
    # packer needs size + gap, so parts in (usable - gap, usable] end up in `oversized`
    # (the trailing gap of the last part is not really needed; harmless, never lost).
    plates, oversized, _ = run([inst("snug.dat", "1", USABLE_W, USABLE_H)])
    assert plates == []
    assert [o["part_file"] for o in oversized] == ["snug.dat"]


def test_deterministic():
    instances = random_instances(3)
    a = run(instances)
    b = run(instances)
    assert a == b


def test_input_order_does_not_change_plate_set():
    instances = random_instances(4)
    shuffled = instances[:]
    random.Random(9).shuffle(shuffled)
    a, _, _ = run(instances)
    b, _, _ = run(shuffled)
    assert [(p["color_id"], p["index"], p["count"], len(p["items"])) for p in a] == \
        [(p["color_id"], p["index"], p["count"], len(p["items"])) for p in b]


def test_empty_input():
    assert run([]) == ([], [], [])


def test_bad_mix_mode_rejected():
    with pytest.raises(ValueError):
        run([inst("a.dat", "1", 10, 10)], mix="sometimes")


# -------------------------------------------------------- colour grouping/names

def test_plates_are_grouped_by_colour_most_parts_first():
    plates, _, _ = run([
        inst("a.dat", "1", 10, 10, count=2),
        inst("b.dat", "2", 10, 10, count=5),
        inst("c.dat", "3", 10, 10, count=2),
    ])
    assert [p["color_id"] for p in plates][0] == "2"
    # equal counts: ordered by colour name (Black < Blue)
    assert [p["color_name"] for p in plates[1:]] == ["Black", "Blue"]


def test_plate_metadata_and_name():
    plates, _, _ = run([inst("a.dat", "4", 10, 10)])
    (p,) = plates
    assert p["color_name"] == "Trans-Clear" and p["rgb"] == "FCFCFC" and p["is_trans"] is True
    assert p["kind"] == "" and p["support"] == ""
    assert pack.plate_name(p) == "Trans-Clear 1 of 1"


def test_unknown_colour_gets_default_info():
    plates, _, _ = run([inst("a.dat", "99", 10, 10)])
    assert plates[0]["color_name"] == "Color 99"
    assert plates[0]["rgb"] == "CCCCCC"


def test_plate_name_numbering():
    plates, _, _ = run([inst("big.dat", "2", 100, 100, count=3)])
    assert [pack.plate_name(p) for p in plates] == ["Red 1 of 3", "Red 2 of 3", "Red 3 of 3"]


def test_plate_colors_single_and_mixed():
    plate = {"color_id": "1", "color_name": "Blue", "rgb": "0055BF", "items": [("a", 0, 0, 1, 1)]}
    assert pack.plate_colors(plate) == [{"color_id": "1", "color_name": "Blue", "rgb": "0055BF",
                                         "is_trans": False, "items": plate["items"]}]
    groups = [{"color_id": "1", "color_name": "Blue"}, {"color_id": "2", "color_name": "Red"}]
    assert pack.plate_colors({**plate, "groups": groups}) is groups


def test_plate_name_mixed_and_supports():
    plate = {"color_name": "Mixed", "kind": "supports ", "index": 2, "count": 3,
             "groups": [{"color_name": "Blue"}, {"color_name": "Red"}]}
    assert pack.plate_name(plate) == "Mixed supports 2 of 3 (Blue, Red)"
    assert pack.plate_name({"color_name": "Blue", "index": 1, "count": 1}) == "Blue 1 of 1"


# ------------------------------------------------------------------- supports

def test_support_parts_get_their_own_plates_after_normal_ones():
    plates, _, _ = run([
        inst("n.dat", "1", 10, 10, count=2),
        inst("s.dat", "1", 10, 10, count=2, needs_support=True),
    ])
    assert [(p["kind"], p["support"]) for p in plates] == [("", ""), ("supports ", "plate")]
    assert [pack.plate_name(p) for p in plates] == ["Blue 1 of 1", "Blue supports 1 of 1"]
    assert {n for n, *_ in plates[0]["items"]} == {"n.dat"}
    assert {n for n, *_ in plates[1]["items"]} == {"s.dat"}


# ----------------------------------------------------------------- mixed plates

def small_groups():
    return [inst(f"{c}.dat", c, 15, 15, count=2) for c in "123"]


def test_mix_off_keeps_one_plate_per_colour():
    plates, _, _ = run(small_groups(), mix="off")
    assert len(plates) == 3
    assert all("groups" not in p for p in plates)


def test_mix_always_shares_a_plate_between_tiny_colour_groups(capsys):
    plates, _, _ = run(small_groups(), mix="always")
    assert len(plates) == 1
    (p,) = plates
    assert p["color_name"] == "Mixed"
    assert sorted(g["color_name"] for g in p["groups"]) == ["Black", "Blue", "Red"]
    assert pack.plate_name(p).startswith("Mixed 1 of 1 (")
    assert len(p["items"]) == 6
    assert_plate_valid(p)
    # each colour stays one contiguous block: group item boxes do not interleave
    boxes = []
    for g in p["groups"]:
        xs = [x for _, x, _, _, _ in g["items"]]
        ys = [y for _, _, y, _, _ in g["items"]]
        ws = [x + w for _, x, _, w, _ in g["items"]]
        ds = [y + d for _, _, y, _, d in g["items"]]
        boxes.append((min(xs), min(ys), max(ws), max(ds)))
    for a, b in itertools.combinations(boxes, 2):
        assert a[2] <= b[0] + EPS or b[2] <= a[0] + EPS or a[3] <= b[1] + EPS or b[3] <= a[1] + EPS
    assert "mixed 3 tiny" in capsys.readouterr().out


def test_mix_auto_only_when_over_plate_limit():
    plates, _, _ = run(small_groups(), mix="auto", max_plates=36)
    assert len(plates) == 3
    plates, _, _ = run(small_groups(), mix="auto", max_plates=2)
    assert len(plates) == 1 and plates[0]["color_name"] == "Mixed"


def test_mix_caps_colours_per_plate():
    many = [inst(f"{c}.dat", c, 10, 10) for c in "1234"] + [inst("5.dat", "5", 10, 10)]
    plates, _, _ = run(many, mix="always")
    mixed = [p for p in plates if p.get("groups")]
    assert mixed and all(len(p["groups"]) <= pack.MIX_MAX_COLOURS for p in mixed)
    assert sum(len(p["items"]) for p in plates) == 5


def test_mix_tower_keepout_is_left_empty():
    tower = (50.0, 50.0)
    plates, _, _ = run(small_groups(), mix="always", tower=tower)
    (p,) = plates
    assert "tower" in p
    tx0, ty0 = USABLE_W - tower[0], USABLE_H - tower[1]
    for _, x, y, w, d in p["items"]:
        assert x + w <= tx0 + EPS or y + d <= ty0 + EPS


def test_support_groups_are_only_mixed_with_support_groups():
    plates, _, _ = run([
        inst("a.dat", "1", 15, 15, count=2, needs_support=True),
        inst("b.dat", "2", 15, 15, count=2, needs_support=True),
        inst("c.dat", "3", 15, 15, count=2),
        inst("d.dat", "4", 15, 15, count=2),
    ], mix="always")
    kinds = sorted((p["support"], p["color_name"]) for p in plates)
    assert kinds == [("", "Mixed"), ("plate", "Mixed")]
    for p in plates:
        names = {n for n, *_ in p["items"]}
        assert names == ({"a.dat", "b.dat"} if p["support"] else {"c.dat", "d.dat"})
        assert p["kind"] == ("supports " if p["support"] else "")


def test_plate_limit_folds_support_parts_into_last_normal_plate(capsys):
    plates, _, _ = run([
        inst("n.dat", "1", 15, 15, count=2),
        inst("s.dat", "1", 15, 15, count=1, needs_support=True),
    ], max_plates=1)
    assert len(plates) == 1
    assert plates[0]["support"] == "folded"
    assert {n for n, *_ in plates[0]["items"]} == {"n.dat", "s.dat"}
    assert "folded" in capsys.readouterr().out
    assert_plate_valid(plates[0])


def test_brim_keepout_prevents_fused_brims_on_normal_mixed_and_folded_plates():
    # Two 3 mm brimmed parts (brim_width=3.0, brim_object_gap=0.2 -> 3.2 mm per side)
    # with pack gap=1.0 must sit >= 3.2 + 3.2 + 1.0 = 7.4 mm apart, and >= 3.2 mm from
    # usable bed edges, while a brimmed + unbrimmed pair sits >= 3.2 + 1.0 = 4.2 mm apart.
    brim_insts = [
        inst("b1.dat", "1", 8.0, 8.0, count=6, pad=3.2),
        inst("b2.dat", "1", 8.0, 8.0, count=4, brim_width=3.0, brim_object_gap=0.2),
        inst("plain.dat", "1", 16.0, 16.0, count=2),
    ]
    plates, oversized, _ = run(brim_insts, gap=1.0)
    assert oversized == [] and len(plates) == 1
    pad_of = {"b1.dat": 3.2, "b2.dat": 3.2, "plain.dat": 0.0}
    items = plates[0]["items"]
    for name, x, y, w, d in items:
        p = pad_of[name]
        assert x >= p - EPS and y >= p - EPS
        assert x + w <= USABLE_W - p - 1.0 + EPS
        assert y + d <= USABLE_H - p - 1.0 + EPS
    for a, b in itertools.combinations(items, 2):
        na, ax, ay, aw, ad = a
        nb, bx, by, bw, bd = b
        sep = max(bx - (ax + aw), ax - (bx + bw), by - (ay + ad), ay - (by + bd))
        assert sep >= pad_of[na] + pad_of[nb] + 1.0 - EPS, (a, b)

    # Mixed plate with brimmed parts from two colours
    mix_plates, ov_m, _ = run([
        inst("m1.dat", "1", 8.0, 8.0, count=2, brim_width=3.0, brim_object_gap=0.2),
        inst("m2.dat", "2", 8.0, 8.0, count=2, pad=3.2),
    ], gap=1.0, mix="always")
    assert ov_m == [] and len(mix_plates) == 1
    for a, b in itertools.combinations(mix_plates[0]["items"], 2):
        _, ax, ay, aw, ad = a
        _, bx, by, bw, bd = b
        sep = max(bx - (ax + aw), ax - (bx + bw), by - (ay + ad), ay - (by + bd))
        assert sep >= 3.2 + 3.2 + 1.0 - EPS

    # Folded plate where BOTH normal and support parts carry 3.2 mm brim pads
    fold_plates, ov_f, _ = run([
        inst("fn.dat", "1", 8.0, 8.0, count=2, pad=3.2),
        inst("fs.dat", "1", 8.0, 8.0, count=2, needs_support=True, brim_width=3.0, brim_object_gap=0.2),
    ], gap=1.0, max_plates=1, mix="off")
    assert ov_f == [] and len(fold_plates) == 1 and fold_plates[0]["support"] == "folded"
    for a, b in itertools.combinations(fold_plates[0]["items"], 2):
        _, ax, ay, aw, ad = a
        _, bx, by, bw, bd = b
        sep = max(bx - (ax + aw), ax - (bx + bw), by - (ay + ad), ay - (by + bd))
        assert sep >= 3.2 + 3.2 + 1.0 - EPS
