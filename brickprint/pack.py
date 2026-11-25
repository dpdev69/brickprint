"""MaxRects bed packing, grouped by colour."""

from __future__ import annotations

from collections import defaultdict


class Bin:
    def __init__(self, width: float, height: float):
        self.width = width
        self.height = height
        self.free = [(0.0, 0.0, width, height)]
        self.items = []  # (part_file, x, y, w, d)
        self.insts = []  # source instance of each item (set by pack())

    def insert(self, w: float, h: float, name: str) -> tuple[float, float] | None:
        best = None
        for i, (fx, fy, fw, fh) in enumerate(self.free):
            if w <= fw + 1e-9 and h <= fh + 1e-9:
                # Bottom-left first: fills low rows before starting new ones.
                # This keeps small sets compact without increasing bed count.
                score = (fy, fx, min(fw - w, fh - h), max(fw - w, fh - h), i)
                if best is None or score < best[0]:
                    best = (score, i, fx, fy)
        if best is None:
            return None
        _, i, fx, fy = best
        self.items.append((name, fx, fy, w, h))
        self._split((fx, fy, w, h))
        return fx, fy

    def reserve(self, x: float, y: float, w: float, h: float) -> None:
        """Keep a fixed rectangle free of parts (not an item)."""
        self._split((x, y, w, h))

    def _split(self, placed) -> None:
        kept = []
        created = []
        px0, py0, pw, ph = placed
        px1, py1 = px0 + pw, py0 + ph
        for r in self.free:
            rx0, ry0, rw, rh = r
            rx1, ry1 = rx0 + rw, ry0 + rh
            if rx1 <= px0 or px1 <= rx0 or ry1 <= py0 or py1 <= ry0:
                kept.append(r)
                continue
            if px0 > rx0 + 1e-9:
                w = px0 - rx0
                if w > 1e-6 and rh > 1e-6:
                    created.append((rx0, ry0, w, rh))
            if px1 < rx1 - 1e-9:
                w = rx1 - px1
                if w > 1e-6 and rh > 1e-6:
                    created.append((px1, ry0, w, rh))
            if py0 > ry0 + 1e-9:
                h = py0 - ry0
                if rw > 1e-6 and h > 1e-6:
                    created.append((rx0, ry0, rw, h))
            if py1 < ry1 - 1e-9:
                h = ry1 - py1
                if rw > 1e-6 and h > 1e-6:
                    created.append((rx0, py1, rw, h))
        if not created:
            self.free = kept
            return
        # Unsplit (kept) rects are already maximal and cannot be contained by
        # newly split (created) rects, so only prune created against kept and
        # created against created (using index order to break exact ties).
        surviving = []
        for a in created:
            ax0, ay0, aw, ah = a
            ax1, ay1 = ax0 + aw, ay0 + ah
            contained = False
            for bx0, by0, bw, bh in kept:
                if bw >= aw and bh >= ah and bx0 <= ax0 and by0 <= ay0 and bx0 + bw >= ax1 and by0 + bh >= ay1:
                    contained = True
                    break
            if not contained:
                surviving.append(a)
        pruned = []
        n = len(surviving)
        for i, a in enumerate(surviving):
            ax0, ay0, aw, ah = a
            ax1, ay1 = ax0 + aw, ay0 + ah
            contained = False
            for j in range(n):
                if j == i:
                    continue
                b = surviving[j]
                bx0, by0, bw, bh = b
                if bw >= aw and bh >= ah and bx0 <= ax0 and by0 <= ay0 and bx0 + bw >= ax1 and by0 + bh >= ay1:
                    if b != a or j < i:
                        contained = True
                        break
            if not contained:
                pruned.append(a)
        kept.extend(pruned)
        self.free = kept


def _intersects(a, b) -> bool:
    return not (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0]
                or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])


def _contains(a, b) -> bool:
    return (a[0] <= b[0] and a[1] <= b[1]
            and a[0] + a[2] >= b[0] + b[2] and a[1] + a[3] >= b[1] + b[3])


MAX_PLATES = 36  # Bambu Studio's plate grid

# Colour mixing (--mix-colours).  A colour's last (or only) plate is a "tiny
# group" when its parts, inflated by their pad keep-out and the gap, cover at
# most MIX_FILL_MAX of the usable bed.  Tiny groups of different colours may
# share a "Mixed" plate, each colour kept as one contiguous block, at most
# MIX_MAX_COLOURS colours per plate (the A1 mini AMS lite has 4 slots).
MIX_FILL_MAX = 0.35
MIX_MAX_COLOURS = 4
# Extra clearance between colour blocks on a mixed *supports* plate: each
# colour there is its own supported object, and tree supports of neighbouring
# objects must not run into each other ("gcode path conflicts").
MIX_SUPPORT_BLOCK_GAP = 15.0
MIX_MODES = ("auto", "off", "always")
# Prime tower keep-out on mixed plates when the profile prints one (the stock
# PLA preset does): back-right corner of the usable bed, mm.  Bambu put the
# tower of a 4-colour 0.20 mm A1 mini plate at 45.4 x 44.7 mm including its
# brim (35 mm wide + rib walls + 3 mm brim); the tower corner is placed
# TOWER_INSET inside this box.  The packer leaves the box empty.
TOWER_KEEPOUT = (50.0, 50.0)
TOWER_INSET = 4.0


def pack(
    instances: list[dict],
    colors: dict[str, dict],
    bed: float,
    depth: float,
    margin: float,
    gap: float,
    max_plates: int = MAX_PLATES,
    mix: str = "off",
    tower: tuple[float, float] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Pack unassembled parts by colour.

    instances: [{'part_file','color_id','size':(x,y,z),'count', 'needs_support'?}]
    colors: color_id -> {name, rgb, is_trans}
    Returns (plates, oversized, empty_colors).

    Parts with needs_support=True are packed, per colour, onto their own
    "<Colour> supports i of n" plates after that colour's normal plates
    (plate["support"] == "plate"; the writer enables tree supports on that
    plate object only).

    ``mix`` merges tiny groups (see MIX_FILL_MAX) of different colours onto
    shared plates ("Mixed i of n (A, B)" / "Mixed supports i of n (...)",
    after all single-colour plates).  Support parts are only ever mixed with
    support parts.  "off": never; "always": every tiny group; "auto": only
    while the project exceeds ``max_plates`` -- tiny support groups first, then
    tiny normal groups.  A mixed plate carries ``groups`` (one entry per colour
    with its own items); its color_id/rgb are those of its first group.

    ``tower`` (w, d): keep-out for a prime tower in the back-right corner of
    every mixed plate (multi-filament plates print one when the profile
    enables it); the plate gets ``tower`` = the tower's (x, y) bed position.

    If the project still exceeds ``max_plates``, the smallest support groups
    are folded into their colour's last normal plate, which then gets supports
    enabled as a whole (plate["support"] == "folded").
    """
    if mix not in MIX_MODES:
        raise ValueError(f"mix must be one of {MIX_MODES}, not {mix!r}")
    usable_w = bed - 2 * margin
    usable_h = depth - 2 * margin
    by_color = defaultdict(list)
    for inst in instances:
        for _ in range(inst["count"]):
            by_color[inst["color_id"]].append(inst)

    color_order = sorted(
        by_color,
        key=lambda c: (-len(by_color[c]), colors.get(c, {}).get("name", c)),
    )

    plates = []
    oversized = []

    def size_key(i):
        return (i["size"][0] * i["size"][1], i["size"][0], i["part_file"])

    def inflated(inst):
        w, d, _ = inst["size"]
        pad = float(inst.get("pad") or 0.0)   # keep-out each side (own-object supports)
        return w + 2 * pad + gap, d + 2 * pad + gap

    def pack_list(color_id, insts):
        bins: list[Bin] = []
        for inst in sorted(insts, key=size_key, reverse=True):
            w, d, _ = inst["size"]
            pad = float(inst.get("pad") or 0.0)   # keep-out each side (own-object supports)
            if w > usable_w + 1e-9 or d > usable_h + 1e-9:
                oversized.append({
                    "part_file": inst["part_file"],
                    "color_id": color_id,
                    "size": inst["size"],
                })
                continue
            target = None
            for b in bins:
                pos = b.insert(w + 2 * pad + gap, d + 2 * pad + gap, inst["part_file"])
                if pos is not None:
                    target = (b, pos)
                    break
            if target is None:
                b = Bin(usable_w, usable_h)
                pos = b.insert(w + 2 * pad + gap, d + 2 * pad + gap, inst["part_file"])
                if pos is None:
                    oversized.append({
                        "part_file": inst["part_file"],
                        "color_id": color_id,
                        "size": inst["size"],
                    })
                    continue
                bins.append(b)
                target = (b, pos)
            b, (x, y) = target
            # store the true size, not the gap-inflated one
            b.items[-1] = (inst["part_file"], x + pad, y + pad, w, d)
            b.insts.append(inst)
        return [b for b in bins if b.items]

    groups = []  # [color_id, normal_bins, support_bins]
    for color_id in color_order:
        normal = [i for i in by_color[color_id] if not i.get("needs_support")]
        sup = [i for i in by_color[color_id] if i.get("needs_support")]
        nb = pack_list(color_id, normal)
        sb = pack_list(color_id, sup)
        for b in nb:
            b.support = ""
        for b in sb:
            b.support = "plate"
        groups.append([color_id, nb, sb])

    mixed = {"": [], "plate": []}  # support kind -> [[(color_id, [(inst, x, y)]), ...], ...]

    def total():
        return (sum(len(g[1]) + len(g[2]) for g in groups)
                + len(mixed[""]) + len(mixed["plate"]))

    def fill(b):
        return sum(a * c for a, c in map(inflated, b.insts)) / (usable_w * usable_h)

    def block_shapes(insts):
        """Compact layouts of one colour group as (bw, bh, [(inst, x, y)]),
        smallest bounding box first.  Positions are of the inflated rects."""
        order = sorted(insts, key=size_key, reverse=True)
        infl = [(inst, *inflated(inst)) for inst in order]
        if len(infl) == 1:
            inst, iw, ih = infl[0]
            if iw <= usable_w + 1e-9 and ih <= usable_h + 1e-9:
                return [(iw, ih, [(inst, 0.0, 0.0)])]
            return []
        min_w = max(iw for _, iw, _ in infl)
        max_w = min(usable_w, sum(iw for _, iw, _ in infl))
        widths = sorted({min(max_w, max(min_w, usable_w * k / 12)) for k in range(1, 13)})
        shapes = []
        for width in widths:
            b = Bin(width, usable_h)
            layout = []
            bw = 0.0
            bh = 0.0
            for inst, iw, ih in infl:
                pos = b.insert(iw, ih, inst["part_file"])
                if pos is None:
                    break
                layout.append((inst, pos[0], pos[1]))
                if pos[0] + iw > bw:
                    bw = pos[0] + iw
                if pos[1] + ih > bh:
                    bh = pos[1] + ih
            else:
                shapes.append((bw, bh, layout))
        shapes = [s for s in shapes
                  if not any(o is not s and o[0] <= s[0] + 1e-9 and o[1] <= s[1] + 1e-9
                             and (o[0], o[1]) != (s[0], s[1]) for o in shapes)]
        uniq = {}
        for s in shapes:
            uniq.setdefault((round(s[0], 6), round(s[1], 6)), s)
        return sorted(uniq.values(), key=lambda s: (s[0] * s[1], max(s[0], s[1])))

    def mix_kind(kind):
        """Move every tiny last plate of this support kind onto mixed plates."""
        slot = 1 if kind == "" else 2
        cands = []
        for g in groups:
            if g[slot] and fill(g[slot][-1]) <= MIX_FILL_MAX + 1e-9:
                shapes = block_shapes(g[slot][-1].insts)
                if shapes:
                    cands.append((g, shapes))
        name = lambda c: colors.get(c, {}).get("name", c)
        cands.sort(key=lambda gs: (-gs[1][0][0] * gs[1][0][1], name(gs[0][0])))
        extra = MIX_SUPPORT_BLOCK_GAP if kind else 0.0
        bins = []  # [Bin, [(group, layout, bx, by)]]
        for g, shapes in cands:
            placed = False
            for mb in bins:
                if len(mb[1]) >= MIX_MAX_COLOURS:
                    continue
                for bw, bh, layout in shapes:
                    # reserve the block plus clearance (clipped: nothing lies beyond the bed)
                    pos = mb[0].insert(min(bw + extra, usable_w), min(bh + extra, usable_h), g[0])
                    if pos is not None:
                        mb[1].append((g, layout, pos[0], pos[1]))
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                b = Bin(usable_w, usable_h)
                if tower:
                    b.reserve(usable_w - tower[0], usable_h - tower[1], tower[0], tower[1])
                for bw, bh, layout in shapes:
                    pos = b.insert(min(bw + extra, usable_w), min(bh + extra, usable_h), g[0])
                    if pos is not None:
                        bins.append([b, [(g, layout, pos[0], pos[1])]])
                        break
        moved = 0
        for _, members in bins:
            if len(members) < 2:
                continue   # nothing to share with: the colour keeps its own plate
            entry = []
            for g, layout, bx, by in members:
                g[slot].pop()
                entry.append((g[0], [(inst, bx + x, by + y) for inst, x, y in layout]))
                moved += 1
            mixed[kind].append(entry)
        if moved:
            what = "support " if kind else ""
            n = sum(1 for _, m in bins if len(m) >= 2)
            print(f"mixed {moved} tiny {what}colour group(s) onto {n} shared plate(s) "
                  f"(<= {MIX_FILL_MAX:.0%} of the bed each, <= {MIX_MAX_COLOURS} colours per plate)")

    if mix == "always":
        mix_kind("plate")
        mix_kind("")
    elif mix == "auto":
        for kind in ("plate", ""):
            if total() > max_plates:
                mix_kind(kind)

    while total() > max_plates:
        cands = [g for g in groups if g[1] and g[2]]
        if not cands:
            break
        g = min(cands, key=lambda g: (sum(len(b.items) for b in g[2]), len(g[2])))
        color_id, nb, sb = g
        # Only fold support instances still on this colour's unmixed support bins
        rem_sup = [inst for b in sb for inst in b.insts]
        target = nb[-1]
        free, items, b_insts = list(target.free), list(target.items), list(target.insts)
        ok = True
        for inst in sorted(rem_sup, key=size_key, reverse=True):
            w, d, _ = inst["size"]
            pad = float(inst.get("pad") or 0.0)
            pos = target.insert(w + 2 * pad + gap, d + 2 * pad + gap, inst["part_file"])
            if pos is None:
                ok = False
                break
            target.items[-1] = (inst["part_file"], pos[0] + pad, pos[1] + pad, w, d)
            target.insts.append(inst)
        if not ok:
            target.free, target.items, target.insts = free, items, b_insts
            print(f"WARNING: support parts of colour {color_id} do not fit on its last plate; "
                  "kept on their own plate(s)")
            g[1], g[2] = nb + sb, []
            continue
        target.support = "folded"
        print(f"folded {len(rem_sup)} support part(s) of colour {color_id} into its last normal "
              f"plate (plate limit {max_plates}); supports enabled on that plate")
        g[2] = []

    def info_of(color_id):
        return colors.get(color_id, {"name": f"Color {color_id}", "rgb": "CCCCCC", "is_trans": False})

    for color_id, nb, sb in groups:
        info = info_of(color_id)
        normal = [b for b in nb if b.support != "plate"]
        supp = [b for b in nb if b.support == "plate"] + sb
        for kind, bins in (("", normal), ("supports ", supp)):
            for i, b in enumerate(bins, 1):
                max_x = max(it[1] + it[3] for it in b.items)
                max_y = max(it[2] + it[4] for it in b.items)
                plates.append({
                    "color_id": color_id,
                    "color_name": info["name"],
                    "rgb": info["rgb"],
                    "is_trans": info["is_trans"],
                    "index": i,
                    "count": len(bins),
                    "kind": kind,
                    "support": b.support,
                    "items": list(b.items),
                    "bbox": (max_x, max_y),
                })

    for support, kind in (("", ""), ("plate", "supports ")):
        for i, entry in enumerate(mixed[support], 1):
            cgroups = []
            for color_id, layout in entry:
                info = info_of(color_id)
                items = []
                for inst, x, y in layout:
                    w, d, _ = inst["size"]
                    pad = float(inst.get("pad") or 0.0)
                    items.append((inst["part_file"], x + pad, y + pad, w, d))
                cgroups.append({"color_id": color_id, "color_name": info["name"],
                                "rgb": info["rgb"], "is_trans": info["is_trans"], "items": items})
            all_items = [it for cg in cgroups for it in cg["items"]]
            first = cgroups[0]
            plates.append({
                "color_id": first["color_id"],
                "color_name": "Mixed",
                "rgb": first["rgb"],
                "is_trans": first["is_trans"],
                "index": i,
                "count": len(mixed[support]),
                "kind": kind,
                "support": support,
                "items": all_items,
                "bbox": (max(it[1] + it[3] for it in all_items),
                         max(it[2] + it[4] for it in all_items)),
                "groups": cgroups,
            })
            if tower:
                plates[-1]["tower"] = (round(margin + usable_w - tower[0] + TOWER_INSET, 3),
                                       round(margin + usable_h - tower[1] + TOWER_INSET, 3))
    return plates, oversized, []


def plate_colors(plate: dict) -> list[dict]:
    """[{color_id, color_name, rgb, is_trans, items}] of a plate: its ``groups``
    on a mixed plate, otherwise the plate's single colour with all its items."""
    if plate.get("groups"):
        return plate["groups"]
    return [{"color_id": plate["color_id"], "color_name": plate.get("color_name", ""),
             "rgb": plate["rgb"], "is_trans": plate.get("is_trans", False),
             "items": plate["items"]}]


def plate_name(plate: dict) -> str:
    """'<Colour> i of n', '<Colour> supports i of n', or for a mixed plate
    'Mixed [supports ]i of n (<Colour>, <Colour>)'."""
    name = f"{plate['color_name']} {plate.get('kind', '')}{plate['index']} of {plate['count']}"
    if plate.get("groups"):
        name += " (" + ", ".join(g["color_name"] for g in plate["groups"]) + ")"
    return name
