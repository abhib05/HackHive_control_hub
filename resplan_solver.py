"""ResPlan floor plan -> devices, hubs, zones and closed cable rings -> one output JSON per plan.

Runs with no manual step (deck slide 1). For each plan:
  1 read the geometry (a ResPlan.pkl plan dict, or an input JSON file) and convert drawing units to
    metres with scale = sqrt(inner.area / area) (slide 4)
  2 rooms: every polygon of living / bedroom / kitchen / storage is one eligible room (slide 2: each
    polygon is one room with one node); bathroom and stair rooms get no device; balconies lie outside
    the inner boundary and are not used
  3 devices: ResPlan plans contain none, so exactly ONE device (node) is added to every eligible room
    and none anywhere else. Point = the room centroid if it is inside the room and >= 0.5 m from walls,
    windows and the room edge, else the nearest point that is (lower clearance only for rooms too
    small for 0.5 m, with a warning). Devices given in an input JSON are used as they are.
  4 rule-3 links between rooms: doors and open passages (windows never count)
  5 solve with hub_routing stages 1-4 (0.1 m grid plus one line through every device)
  6 build the slide-8 JSON, re-check the design from its own polylines (check_output, independent of
    the routing grid) and optionally draw the overlay from the JSON
"""
import itertools
import json
import math
import time
import traceback
from collections import defaultdict

import numpy as np
import shapely
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.ops import nearest_points, polylabel, unary_union

import hub_routing as hr

CONFIG = dict(
    grid_m=0.1,               # regular grid spacing; extra grid lines go through every device point
    corner_lines_m=(0.012,),  # extra lines this far either side of every wall / window / outline corner
                              # (wires hug corners and door jambs: J -0.38 on 100 test plans); () = off
    door_lines_m=(),          # the same for door corners only (cheaper, a little less gain)
    wire_clearance_m=0.01,    # wires stay this far from walls and windows (> the checker's 1 mm)
    hub_clearance_m=0.5,      # rule 6
    device_clearance_m=0.5,   # slide 2: device = interior point 0.5 m from walls
    zone_mode="any",          # rule 3: "any" (user decision: lenient allowed) | "excluded" | "own"  (see hub_routing.Plan)
    passage_factor=2.0,       # open passage = wall/window/door-free contact >= 2 x wall depth
    oversize_ok=True,         # an eligible room > 100 m2 still gets its own hub (design marked invalid)
    room_area_cap_m2=100.0,   # rule 2 (user decision 2026-10-16): a room counts at most this much towards its hub,
                              # so a hub may serve part of a room > 100 m2; None = full room area (deck wording)
    time_budget_s=90.0,       # after this, no further groupings are routed (best design so far is kept)
    hard_limit_s=150.0,       # rip-up and reroute stops by then too (the best design found is kept)
    max_routed=40,            # at most this many groupings routed per plan
    broken_area_m2=15.0,      # plans smaller than this, or with scale > 60 units/m, are skipped
)
DEVICE_TYPES = hr.DEVICE_TYPES                      # living, bedroom, kitchen, storage: one device each
ROOM_TYPES = DEVICE_TYPES + ("bathroom", "stair")   # rooms inside the inner boundary; no device in the last two
TOL = 1e-3                                          # slide 7: points / lines closer than 1 mm are the same


class PlanError(Exception):
    pass


# --------------------------------------------------------------------------- geometry helpers
def polygons(g):
    """Polygon parts of any geometry; invalid polygons are repaired first (slide 2)."""
    if g is None or getattr(g, "is_empty", True):
        return []
    if not g.is_valid:
        g = shapely.make_valid(g)
    if isinstance(g, Polygon):
        return [g]
    if hasattr(g, "geoms"):
        return [q for part in g.geoms for q in polygons(part)]
    return []


def union(gs):
    gs = [g for g in gs if g is not None and not g.is_empty]
    return unary_union(gs) if gs else Polygon()


def to_m(g, scale):
    return affinity.scale(g, 1 / scale, 1 / scale, origin=(0, 0))


def r4(v):
    return round(float(v), 4)


# --------------------------------------------------------------------------- devices
def place_device(poly, blockers, scale, clearance_m=0.5, inner=None, avoid=None):
    """One device point for a room (drawing units): the centroid if it lies inside the room (and the
    inner boundary) and at least clearance_m from walls, windows and the room edge, else the nearest
    point that does. Rooms too small for that get the best smaller clearance. `avoid` (e.g. discs around
    devices already placed, for rooms that overlap in the data) is kept clear if at all possible. The
    point is rounded to a whole centimetre in metres. Returns (x, y, clearance used in metres, inside
    the inner boundary)."""
    bases = [(union(polygons(poly.intersection(inner))), True)] if inner is not None else []
    bases.append((poly, inner is None))
    for keep_clear in ([avoid, None] if avoid is not None and not avoid.is_empty else [None]):
        for base, in_inner in bases:
            if base.is_empty:
                continue
            c = base.centroid
            for clr in [x for x in (clearance_m, 0.4, 0.3, 0.2, 0.1) if x <= clearance_m]:
                d = (clr + 0.02) * scale
                region = base.buffer(-d)
                if not region.is_empty and not blockers.is_empty:
                    region = region.difference(blockers.buffer(d))
                if not region.is_empty and keep_clear is not None:
                    region = region.difference(keep_clear)
                if region.is_empty:
                    continue
                p = c if region.contains(c) else nearest_points(region, c)[0]
                return round(p.x / scale, 2) * scale, round(p.y / scale, 2) * scale, clr, in_inner
    p = polylabel(poly, tolerance=0.01 * scale)
    return p.x, p.y, 0.0, inner is None or inner.contains(p)


def add_devices(rooms, walls, windows, scale, clearance_m, warnings, inner=None, separation_m=0.3):
    """Exactly one device per eligible room (living, bedroom, kitchen, storage), none elsewhere. Devices
    keep separation_m from each other (rooms can overlap in the data)."""
    blockers = union([walls, windows])
    nodes = []
    for r in rooms:
        if r["type"] not in DEVICE_TYPES:
            continue
        avoid = union([Point(n["x"], n["y"]).buffer(separation_m * scale) for n in nodes])
        x, y, clr, in_inner = place_device(r["poly"], blockers, scale, clearance_m, inner, avoid)
        tag = f"{r['room_id']} ({r['type']}, {r['poly'].area / scale ** 2:.1f} m2)"
        if any(math.hypot(x - n["x"], y - n["y"]) < (separation_m - 0.02) * scale for n in nodes):   # 1 cm rounding
            warnings.append(f"{tag} overlaps another room so much that its device is closer than {separation_m} m "
                            "to another device")
        if clr < clearance_m:
            warnings.append(f"{tag} is too small for a device {clearance_m} m from walls; device placed {clr} m from walls")
        if not in_inner:
            warnings.append(f"{tag} has no room for a device inside the plan's inner boundary; its device cannot be "
                            "reached by any legal cable (outside the organisers' benchmark)")
        nodes.append(dict(node_id=f"N{len(nodes) + 1}", room_id=r["room_id"], x=x, y=y))
    return nodes


# --------------------------------------------------------------------------- rule-3 links
def room_links(rooms, doors, walls, windows, wall_depth, plan_size, factor=2.0):
    """Rooms joined by a door, or by an open passage (contact free of walls, windows and doors at least
    factor x wall depth long). Same tests as resplan_utils.plan_to_graph, but one entry per polygon and
    windows never count. Returns ({room_id: set of room_ids}, {(a, b): "door" | "opening"})."""
    buf = max(plan_size * 0.007, 0.5)
    ids = [r["room_id"] for r in rooms]
    polys = [r["poly"] for r in rooms]
    bufs = [q.buffer(buf) for q in polys]
    kind = {}
    for d in doors:
        touching = [k for k in range(len(polys)) if d.intersects(bufs[k])]
        if len(touching) > 2:                 # door in a corner: keep the two rooms with the longest contact
            sc = sorted(((k, d.boundary.intersection(polys[k].buffer(buf * 0.6)).length) for k in touching),
                        key=lambda t: -t[1])
            touching = [sc[0][0], sc[1][0]] + [k for k, s in sc[2:] if s >= 0.25 * sc[0][1] and s >= buf]
        for a, b in itertools.combinations(sorted(touching), 2):
            kind[(ids[a], ids[b])] = "door"
    wd = wall_depth
    open_min = factor * wd
    solid = union([walls, windows] + list(doors))
    solid_buf = solid.buffer(0.5)
    for a, b in itertools.combinations(range(len(polys)), 2):
        key = (ids[a], ids[b])
        if key in kind or polys[a].distance(polys[b]) > 1.2 * wd:
            continue
        gap = polys[a].buffer(0.55 * wd).intersection(polys[b].buffer(0.55 * wd)).difference(solid)
        ok = any(max(q.bounds[2] - q.bounds[0], q.bounds[3] - q.bounds[1]) >= open_min and q.area >= 0.25 * wd * wd
                 for q in polygons(gap))
        if not ok:
            contact = polys[a].boundary.intersection(polys[b].buffer(0.6 * wd))
            if contact.length >= open_min:
                seg = contact.difference(solid_buf)
                ok = max((g.length for g in getattr(seg, "geoms", [seg]) if not g.is_empty), default=0) >= open_min
        if ok:
            kind[key] = "opening"
    nb = {i: set() for i in ids}
    for a, b in kind:
        nb[a].add(b)
        nb[b].add(a)
    return nb, kind


def zone_connected(group, nb, types, mode):
    """Rule 3 with the same readings as hub_routing.Plan.is_connected."""
    group = list(group)
    if len(group) <= 1:
        return True
    if mode == "own":
        allowed = set(group)
    elif mode == "any":
        allowed = set(nb)
    else:
        allowed, stack = set(group), list(group)
        while stack:
            for x in nb[stack.pop()]:
                if x not in allowed and types[x] not in DEVICE_TYPES:
                    allowed.add(x)
                    stack.append(x)
    seen, stack = {group[0]}, [group[0]]
    while stack:
        for x in nb[stack.pop()]:
            if x in allowed and x not in seen:
                seen.add(x)
                stack.append(x)
    return set(group) <= seen


# --------------------------------------------------------------------------- input
def from_resplan(p, cfg=CONFIG, nodes=None):
    """ResPlan plan dict (drawing units) -> plan input dict, devices added unless given."""
    p = dict(p)
    if "balacony" in p and "balcony" not in p:
        p["balcony"] = p.pop("balacony")
    area = float(p.get("area") or 0.0)
    inner = union(polygons(p.get("inner")))
    if area <= 0 or inner.is_empty:
        raise PlanError("plan has no inner boundary or no area")
    scale = math.sqrt(inner.area / area)
    walls, windows = union(polygons(p.get("wall"))), union(polygons(p.get("window")))
    rooms = []
    for t in ROOM_TYPES:
        for q in polygons(p.get(t)):
            if q.area / scale ** 2 >= 0.05:                    # drop numerical slivers
                rooms.append(dict(room_id=f"R{len(rooms) + 1:02d}", type=t, poly=q))
    pid = p.get("id")
    inp = dict(plan_id=p.get("plan_id") or (f"PLAN_{int(pid):06d}" if pid is not None else "PLAN"),
               resplan_id=None if pid is None else int(pid), scale=scale, area_m2=area,
               wall_depth=float(p.get("wall_depth") or 0.23 * scale), inner=inner, walls=walls, windows=windows,
               doors=polygons(p.get("door")), front_door=p.get("front_door"), rooms=rooms, warnings=[],
               devices_given=nodes is not None)
    for r in rooms:
        out = r["poly"].difference(inner).area / r["poly"].area
        if out > 0.1:
            inp["warnings"].append(f"{r['room_id']} ({r['type']}) lies {out:.0%} outside the plan's inner boundary "
                                   "(cables may only use the part inside)")
    inp["nodes"] = nodes if nodes is not None else add_devices(rooms, walls, windows, scale,
                                                                 cfg["device_clearance_m"], inp["warnings"], inner)
    return inp


def geom_to_json(g):
    return None if g is None or g.is_empty else mapping(g)


def geom_from_json(v):
    if v is None:
        return Polygon()
    if isinstance(v, str):
        return shapely.from_wkt(v)
    if isinstance(v, dict) and "type" in v:
        return shape(v)
    if isinstance(v, list):
        try:
            return Polygon(v)
        except (ValueError, TypeError):
            return union([geom_from_json(x) for x in v])
    raise PlanError(f"cannot read geometry {str(v)[:60]}")


def input_to_json(inp):
    """Plan input (drawing units) as JSON: the input file this pipeline would get from the organisers."""
    return {
        "plan_id": inp["plan_id"], "resplan_id": inp["resplan_id"], "units": "drawing",
        "area_m2": inp["area_m2"], "scale_units_per_m": inp["scale"], "wall_depth": inp["wall_depth"],
        "inner": geom_to_json(inp["inner"]), "walls": geom_to_json(inp["walls"]),
        "windows": geom_to_json(inp["windows"]), "doors": [geom_to_json(d) for d in inp["doors"]],
        "front_door": geom_to_json(inp["front_door"]) if inp["front_door"] is not None else None,
        "rooms": [{"room_id": r["room_id"], "type": r["type"], "polygon": geom_to_json(r["poly"])} for r in inp["rooms"]],
        "nodes": inp["nodes"],
    }


def load_input_json(path, cfg=CONFIG):
    """Read one plan input JSON: this pipeline's own format (rooms + nodes lists), or a ResPlan-style
    dict (living, bedroom, ..., wall, window, door, inner, area). Geometry may be GeoJSON, WKT or
    coordinate lists; node coordinates may be drawing units or metres (detected)."""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    nodes = d.get("nodes")
    if "rooms" in d:
        flat = {"id": d.get("resplan_id"), "plan_id": d.get("plan_id"), "area": d.get("area_m2") or d.get("area"),
                "wall_depth": d.get("wall_depth"), "inner": geom_from_json(d.get("inner")),
                "wall": geom_from_json(d.get("walls")), "window": geom_from_json(d.get("windows")),
                "door": union([geom_from_json(x) for x in (d.get("doors") or [])]),
                "front_door": geom_from_json(d.get("front_door")) if d.get("front_door") else None}
        for t in ROOM_TYPES:
            flat[t] = union([geom_from_json(r["polygon"]) for r in d["rooms"] if r["type"] == t])
        inp = from_resplan(flat, cfg, nodes=[])
        # keep the input's own room ids: match by polygon
        own = [(r["room_id"], r["type"], geom_from_json(r["polygon"])) for r in d["rooms"]]
        for r in inp["rooms"]:
            best = max(own, key=lambda o: (o[1] == r["type"], o[2].intersection(r["poly"]).area))
            r["room_id"] = best[0]
    else:
        flat = {k: (geom_from_json(v) if isinstance(v, (dict, list, str)) and k not in ("plan_id",) else v)
                for k, v in d.items() if k != "nodes"}
        inp = from_resplan(flat, cfg, nodes=[])
    if nodes:
        s, room_poly = inp["scale"], {r["room_id"]: r["poly"] for r in inp["rooms"]}
        inside_units = sum(room_poly.get(n.get("room_id"), Polygon()).contains(Point(n["x"], n["y"])) for n in nodes)
        inside_m = sum(room_poly.get(n.get("room_id"), Polygon()).contains(Point(n["x"] * s, n["y"] * s)) for n in nodes)
        f = s if (d.get("node_units") == "m" or inside_m > inside_units) else 1.0
        inp["nodes"] = [dict(node_id=n.get("node_id") or f"N{i + 1}", room_id=n.get("room_id"),
                             x=n["x"] * f, y=n["y"] * f) for i, n in enumerate(nodes)]
        inp["devices_given"] = True
    else:
        inp["nodes"] = add_devices(inp["rooms"], inp["walls"], inp["windows"], inp["scale"],
                                   cfg["device_clearance_m"], inp["warnings"], inp["inner"])
    return inp


# --------------------------------------------------------------------------- solve
def build(inp, cfg=CONFIG):
    """Plan input -> hub_routing Plan (metres), routing Grid, rule-3 links."""
    s = inp["scale"]
    x1, y1, x2, y2 = inp["inner"].bounds
    nb, kinds = room_links(inp["rooms"], inp["doors"], inp["walls"], inp["windows"], inp["wall_depth"],
                           max(x2 - x1, y2 - y1), cfg["passage_factor"])
    node_of = {n["room_id"]: n for n in inp["nodes"]}
    rooms = []
    for r in inp["rooms"]:
        n = node_of.get(r["room_id"]) if r["type"] in DEVICE_TYPES else None
        rooms.append(hr.Room(r["room_id"], r["type"], to_m(r["poly"], s),
                             None if n is None else (n["x"] / s, n["y"] / s), None if n is None else n["node_id"]))
    plan = hr.Plan(rooms, to_m(union([inp["walls"], inp["windows"]]), s), outline=to_m(inp["inner"], s),
                   neighbours=nb, zone_mode=cfg["zone_mode"], oversize_ok=cfg["oversize_ok"],
                   area_cap=cfg.get("room_area_cap_m2"))
    devs = [plan.rooms[r].device for r in plan.device_rooms()]
    sx, sy = [], []                     # optional lines: never closer than 5 mm to a device line
    for off in cfg.get("corner_lines_m", ()):
        # lines just off every wall / window / outline corner: wires can hug corners and door jambs
        # (off = the wire clearance + a little), hubs can sit exactly 0.5 m from a wall (off = 0.5 + a little)
        xs, ys = _corner_coords(union([plan.walls, plan.outline.boundary]))
        sx += [x + k * off for x in xs for k in (-1, 1)]
        sy += [y + k * off for y in ys for k in (-1, 1)]
    for off in cfg.get("door_lines_m", ()):
        xs, ys = _corner_coords(to_m(union(inp["doors"]), s))
        sx += [x + k * off for x in xs for k in (-1, 1)]
        sy += [y + k * off for y in ys for k in (-1, 1)]
    grid = hr.Grid(plan, cfg["grid_m"], cfg["wire_clearance_m"], extra_x=[d[0] for d in devs],
                   extra_y=[d[1] for d in devs], soft_x=sx, soft_y=sy)
    return plan, grid, nb, kinds


def _corner_coords(g):
    """Distinct x and y coordinates of all vertices of a geometry (metres, rounded to 0.1 mm)."""
    pts = shapely.get_coordinates(g)
    return np.unique(np.round(pts[:, 0], 4)), np.unique(np.round(pts[:, 1], 4))


def _base_output(inp, cfg):
    s = inp["scale"]
    return {
        "plan_id": inp["plan_id"], "units": "m", "hubs": [], "reported_metrics": {},
        "status": None, "violations": [], "warnings": list(inp["warnings"]),
        "resplan_id": inp["resplan_id"], "scale_units_per_m": r4(s),
        "coordinate_frame": "metres = drawing units / scale; drawing origin and axes (lower-left, y up)",
        "rooms": [{"room_id": r["room_id"], "type": r["type"], "area_m2": r4(r["poly"].area / s ** 2),
                   "has_device": r["type"] in DEVICE_TYPES} for r in inp["rooms"]],
        "nodes": [{"node_id": n["node_id"], "room_id": n["room_id"], "x": r4(n["x"] / s), "y": r4(n["y"] / s)}
                  for n in inp["nodes"]],
        "devices": "given in the input" if inp.get("devices_given") else
                   "added by this pipeline: one per living room, bedroom, kitchen and storage room",
        "solver": dict({k: cfg[k] for k in ("zone_mode", "grid_m", "hub_clearance_m", "wire_clearance_m")},
                       corner_lines_m=list(cfg.get("corner_lines_m", ())),
                       room_area_cap_m2=cfg.get("room_area_cap_m2")),
    }


def _rings(q, nd=3):
    """Polygon -> [exterior, *holes], each a list of [x, y]."""
    return [[[round(x, nd), round(y, nd)] for x, y in r.coords] for r in [q.exterior, *q.interiors]]


def plan_geometry(inp):
    """Plan input -> drawing data in metres for the web viewer (web_app.py): rooms, walls, windows,
    doors, front door, outline and nodes. Each polygon is [exterior, *holes]."""
    s = inp["scale"]
    poly = lambda g: [_rings(q) for q in polygons(to_m(g, s))]
    fd = inp.get("front_door")
    fd_lines = []
    if fd is not None and not fd.is_empty and fd.geom_type in ("LineString", "MultiLineString"):
        for ln in getattr(to_m(fd, s), "geoms", [to_m(fd, s)]):
            fd_lines.append([[round(x, 3), round(y, 3)] for x, y in ln.coords])
    rooms = []
    for r in inp["rooms"]:
        q = to_m(r["poly"], s)
        c = q.representative_point()
        rooms.append({"room_id": r["room_id"], "type": r["type"], "area_m2": r4(q.area),
                      "has_device": r["type"] in DEVICE_TYPES, "polys": poly(r["poly"]),
                      "label": [round(c.x, 3), round(c.y, 3)]})
    return {
        "plan_id": inp["plan_id"], "resplan_id": inp["resplan_id"], "scale_units_per_m": r4(s),
        "area_m2": r4(inp["area_m2"]), "wall_depth_m": r4(inp["wall_depth"] / s),
        "bounds": [r4(v) for v in to_m(inp["inner"], s).bounds],
        "inner": poly(inp["inner"]), "walls": poly(inp["walls"]), "windows": poly(inp["windows"]),
        "doors": [p for d in inp["doors"] for p in poly(d)],
        "front_door": {"polys": poly(fd) if fd is not None and fd.geom_type in ("Polygon", "MultiPolygon") else [],
                       "lines": fd_lines},
        "rooms": rooms,
        "nodes": [{"node_id": n["node_id"], "room_id": n["room_id"], "x": r4(n["x"] / s), "y": r4(n["y"] / s)}
                  for n in inp["nodes"]],
        "warnings": list(inp["warnings"]),
    }


def _live_emitter(emit, plan, grid, prep):
    """Wraps emit(kind, data) for hub_routing's events: grid vertex ids -> [x, y] in metres, vertex paths
    -> corner polylines, device vertices -> node labels."""
    label = {v: plan.rooms[r].device_label for r, v in prep["devnode"].items()}
    pt = lambda v: [r4(grid.X[v]), r4(grid.Y[v])]
    line = lambda path: [[r4(x), r4(y)] for x, y in grid.corners(path)]

    def conv(k, v):
        if k == "hub":
            return pt(v)
        if k == "hubs":
            return [pt(h) for h in v]
        if k == "order":
            return [label.get(n, str(n)) for n in v]
        if k == "legs":
            return [line(p) for p in v]
        if k == "loops":
            return [{"hub": pt(l["hub"]), "order": conv("order", l["order"]), "legs": conv("legs", l["legs"])}
                    for l in v]
        if k == "groups":
            return [list(g) for g in v]
        if k == "top":
            return [{kk: conv(kk, vv) for kk, vv in t.items()} for t in v]
        if k == "metrics":
            return {a: float(b) for a, b in v.items()}
        if isinstance(v, (np.floating, float)):
            return r4(v) if math.isfinite(v) else None
        if isinstance(v, np.integer):
            return int(v)
        return v

    return lambda kind, **d: emit(kind, {k: conv(k, v) for k, v in d.items()})


def solve(inp, cfg=CONFIG, emit=None):
    """Plan input -> output dict (slide-8 format plus status, violations, warnings and diagnostics).
    Also returns the internal objects for drawing: (out, extras). emit(kind, data), if given, receives
    every step as it happens (web_app.py live view); it may raise to cancel the run."""
    t0 = time.perf_counter()
    note = (lambda kind, **d: emit(kind, d)) if emit else (lambda kind, **d: None)
    out = _base_output(inp, cfg)
    s = inp["scale"]
    bench = out["benchmark"] = {"eligible": False, "reasons": []}
    if inp["area_m2"] < cfg["broken_area_m2"] or s > 60:
        bench["reasons"].append("plan looks broken")
        out.update(status="skipped", reason=f"plan looks broken (area {inp['area_m2']:.1f} m2, scale {s:.1f} units/m)")
        return out, None
    note("stage", stage="grid", msg="building the routing grid and the door / open-passage links")
    plan, grid, nb, kinds = build(inp, cfg)
    t_grid = time.perf_counter() - t0
    eligible = plan.device_rooms()
    out["links"] = [{"rooms": [a, b], "type": t} for (a, b), t in sorted(kinds.items())]
    note("grid", h=cfg["grid_m"], xs=[r4(x) for x in grid.xs], ys=[r4(y) for y in grid.ys], points=int(grid.N),
         free_points=int(grid.free.sum()), edges=int(grid.edge_ok.sum()), t=round(time.perf_counter() - t0, 3))
    note("links", links=out["links"])
    if not eligible:
        bench["reasons"].append("no eligible rooms")
        out.update(status="skipped", reason="no eligible rooms")
        return out, None
    for r in eligible:
        a = plan.rooms[r].area
        if a > hr.MAX_AREA:
            if cfg.get("room_area_cap_m2") is None:
                out["warnings"].append(f"{r} ({plan.rooms[r].type}) is {a:.1f} m2 > 100 m2: rule 2 cannot be met "
                                       "(outside the organisers' benchmark)")
            else:
                out["warnings"].append(f"{r} ({plan.rooms[r].type}) is {a:.1f} m2 > 100 m2: its hub serves "
                                       f"{plan.room_area(r):.0f} m2 of it (capped room area; outside the organisers' "
                                       "benchmark, whose checker may count the full room)")
            bench["reasons"].append(f"{r} > 100 m2")
        elif a < 2.0:
            out["warnings"].append(f"{r} ({plan.rooms[r].type}) is {a:.1f} m2 < 2 m2 (outside the organisers' benchmark)")
            bench["reasons"].append(f"{r} < 2 m2")
    note("stage", stage="prep", msg=f"distance tables from {len(eligible)} devices (direction-aware Dijkstra)")
    prep = hr.prepare(plan, grid)
    for r, v in prep["devnode"].items():
        dx = math.hypot(grid.X[v] - plan.rooms[r].device[0], grid.Y[v] - plan.rooms[r].device[1])
        if dx > 1e-4:
            out["warnings"].append(f"device of {r} is not on a free grid point; the ring ends {dx:.3f} m from it")
            bench["reasons"].append(f"device of {r} not in free space")
    shared = [r for r in prep["devnode"] if list(prep["devnode"].values()).count(prep["devnode"][r]) > 1]
    if shared:
        bench["reasons"].append("two devices at the same point")
        out.update(status="failed", reason=f"devices of {', '.join(shared)} fall on the same point (overlapping rooms "
                                           "in the data)")
        out["solver"]["runtime_s"] = round(time.perf_counter() - t0, 2)
        return out, None
    # slide-3 filter: all device points in one connected part of inner - wall - window
    G = csr_matrix((np.ones(len(grid.m_src)), (grid.m_src, grid.m_dst)), shape=(grid.N, grid.N))
    labels = connected_components(G, directed=False)[1][list(prep["devnode"].values())]
    if len(set(labels)) > 1:
        bench["reasons"].append(f"devices lie in {len(set(labels))} separate parts of the free space")
    bench["eligible"] = not bench["reasons"]
    t_prep = time.perf_counter() - t0
    note("benchmark", **bench)
    deadline = t0 + cfg["time_budget_s"]
    live = _live_emitter(emit, plan, grid, prep) if emit else None
    res, clearance = None, None
    for c in (cfg["hub_clearance_m"], 0.3, 0.1, 0.0):
        note("stage", stage="zones", msg=f"zones and hub points (hub >= {c} m from walls)")
        res = hr.design(plan, grid, prep, clearance=c, deadline=deadline, max_routed=cfg["max_routed"], emit=live,
                        hard_deadline=t0 + cfg.get("hard_limit_s", 1e9))
        if res["k"] is not None:
            clearance = c
            break
        note("log", msg=f"no design with hubs >= {c} m from walls; retrying with less clearance")
    note("stage", stage="check", msg="rule checks and independent polyline check")
    out["solver"].update(grid_points=int(grid.N), grid_edges=int(grid.edge_ok.sum()), grid_s=round(t_grid, 3),
                         prep_s=round(t_prep, 2),
                         **{k: (bool(v) if isinstance(v, bool) else v) for k, v in res["stats"].items()})
    if res["k"] is None:
        out.update(status="failed", reason="no design could be routed (a device cannot be reached through doors "
                                           "or open passages, or no zone has a legal hub point)")
        out["solver"]["runtime_s"] = round(time.perf_counter() - t0, 2)
        return out, None
    if clearance != cfg["hub_clearance_m"]:
        out["warnings"].append(f"no hub point >= {cfg['hub_clearance_m']} m from walls for some zone; "
                               f"used {clearance} m (rule 6 broken)")
    issues = hr.validate(plan, grid, res["groups"], res["loops"], prep["devnode"])
    m = res["metrics"]
    label = {v: plan.rooms[r].device_label for r, v in prep["devnode"].items()}
    for gi, (g, loop) in enumerate(zip(res["groups"], res["loops"])):
        hid, hv = f"H{gi + 1}", loop["hub"]
        hub_room = next((r for r in g if grid.room_inner[r][hv]), g[0])
        seq = [hid] + [label[v] for v in loop["order"]] + [hid]
        m1 = hr.measure(grid, [loop])
        out["hubs"].append({
            "hub_id": hid, "room_id": hub_room, "location": {"x": r4(grid.X[hv]), "y": r4(grid.Y[hv])},
            "assigned_rooms": [hub_room] + [r for r in g if r != hub_room],
            "assigned_nodes": sorted((plan.rooms[r].device_label for r in g), key=lambda x: int(x[1:])),
            "closed_sequence": seq,
            "route_segments": [{"from": a, "to": b, "length_m": r4(grid.path_length(leg)),
                                "polyline": [[r4(x), r4(y)] for x, y in grid.corners(leg)]}
                               for a, b, leg in zip(seq[:-1], seq[1:], loop["legs"])],
            "ring_length_m": r4(m1["L"]),
            "zone_area_m2": r4(plan.area(g)), "node_count": len(g), "bends": int(m1["B"]),
        })
    out["reported_metrics"] = {"hub_count": len(out["hubs"]), "total_length_m": r4(m["L"]), "crossings": int(m["X"]),
                               "overlap_m": r4(m["O"]), "bends": int(m["B"]), "cost_J": r4(m["J"]),
                               "area_gap_m2": r4(res["gap"]), "lower_bound_J": r4(res["lb"]),
                               "hub_lower_bound": hr.hub_lower_bound(plan)}
    t = time.perf_counter()
    chk = check_output(inp, out, nb, cfg)
    out["solver"]["check_s"] = round(time.perf_counter() - t, 3)
    out["check"] = {k: chk[k] for k in ("L", "X", "O", "B", "J")}
    out["check"]["agrees_with_solver"] = bool(abs(chk["J"] - m["J"]) < 0.01 and chk["X"] == m["X"] and chk["B"] == m["B"])
    # violations = what the organisers' checker would see (polylines); grid-only findings listed apart
    viol = [msg for msg, _, _ in chk["violations"]]
    out["violations"] = sorted(set(viol), key=viol.index)
    if issues:
        out["solver_issues"] = sorted({msg for msg, _ in issues})
    if not out["check"]["agrees_with_solver"]:
        out["warnings"].append("independent polyline check disagrees with the solver's metrics")
    out["status"] = "valid" if not out["violations"] and not issues else "invalid"
    out["solver"]["runtime_s"] = round(time.perf_counter() - t0, 2)
    return out, dict(chk=chk)


def solve_safe(inp_or_plan, cfg=CONFIG):
    """Never raises: errors become an output with status "error"."""
    t0 = time.perf_counter()
    try:
        inp = inp_or_plan if "rooms" in inp_or_plan and "nodes" in inp_or_plan else from_resplan(inp_or_plan, cfg)
    except Exception as e:                                        # noqa: BLE001
        pid = inp_or_plan.get("id")
        return {"plan_id": f"PLAN_{int(pid):06d}" if pid is not None else "PLAN", "units": "m", "hubs": [],
                "reported_metrics": {}, "status": "error", "reason": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(limit=4)}, None, None
    try:
        out, extras = solve(inp, cfg)
        return out, inp, extras
    except Exception as e:                                        # noqa: BLE001
        out = _base_output(inp, cfg)
        out.update(status="error", reason=f"{type(e).__name__}: {e}", traceback=traceback.format_exc(limit=6))
        out["solver"]["runtime_s"] = round(time.perf_counter() - t0, 2)
        return out, inp, None


# --------------------------------------------------------------------------- independent check
def _dir(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dy) <= TOL and abs(dx) > TOL:
        return 0 if dx > 0 else 2
    if abs(dx) <= TOL and abs(dy) > TOL:
        return 1 if dy > 0 else 3
    return None


def _near(p, q):
    return abs(p[0] - q[0]) <= TOL and abs(p[1] - q[1]) <= TOL


def polyline_metrics(hubs, terminals):
    """L, X, O, B, J recomputed from the JSON polylines only (slides 6-7), tolerance 1 mm."""
    rings = []
    for h in hubs:
        pts = []
        for sg in h["route_segments"]:
            pl = [tuple(map(float, q)) for q in sg["polyline"]]
            pts += pl if not pts else pl[1:]
        rings.append([(a, b) for a, b in zip(pts[:-1], pts[1:]) if math.hypot(b[0] - a[0], b[1] - a[1]) > TOL / 10])
    dirs = [[_dir(a, b) for a, b in segs] for segs in rings]
    L = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for segs in rings for a, b in segs)
    B = sum(sum(d[i] != d[i - 1] for i in range(len(d))) for d in dirs)          # cyclic: hub turn included
    lines, H, V = defaultdict(list), [], []
    for segs, ds in zip(rings, dirs):
        for (a, b), d in zip(segs, ds):
            if d in (0, 2):
                lines[("H", round(a[1] / TOL))].append((min(a[0], b[0]), max(a[0], b[0])))
                H.append((a[1], min(a[0], b[0]), max(a[0], b[0])))
            elif d in (1, 3):
                lines[("V", round(a[0] / TOL))].append((min(a[1], b[1]), max(a[1], b[1])))
                V.append((a[0], min(a[1], b[1]), max(a[1], b[1])))
    O = 0.0                                   # a stretch covered by k legs counts (k - 1) x its length
    for ivs in lines.values():
        if len(ivs) < 2:
            continue
        depth, prev = 0, None
        for x, e in sorted([(lo, 1) for lo, _ in ivs] + [(hi, -1) for _, hi in ivs]):
            if prev is not None and depth > 1:
                O += (depth - 1) * (x - prev)
            depth, prev = depth + e, x
    X = 0                                     # points where cables meet away from hubs / nodes, once each
    H, V = np.array(H).reshape(-1, 3), np.array(V).reshape(-1, 3)
    hit = ((V[None, :, 0] >= H[:, None, 1] - TOL) & (V[None, :, 0] <= H[:, None, 2] + TOL)
           & (H[:, None, 0] >= V[None, :, 1] - TOL) & (H[:, None, 0] <= V[None, :, 2] + TOL))
    hi, vi = np.nonzero(hit)
    cands = {(round(V[j, 0] / TOL), round(H[i, 0] / TOL)) for i, j in zip(hi, vi)}
    term = np.asarray(terminals, float).reshape(-1, 2)
    for kx, ky in cands:
        P = (kx * TOL, ky * TOL)
        if len(term) and np.min(np.maximum(np.abs(term[:, 0] - P[0]), np.abs(term[:, 1] - P[1]))) <= TOL:
            continue
        passes = []
        for segs, d in zip(rings, dirs):
            n = len(segs)
            for i, (a, b) in enumerate(segs):
                if _near(b, P):                                        # corner / pass-through at a vertex
                    passes.append({(d[i] + 2) % 4, d[(i + 1) % n]})
                elif not _near(a, P):
                    if d[i] in (0, 2) and abs(P[1] - a[1]) <= TOL and min(a[0], b[0]) < P[0] < max(a[0], b[0]):
                        passes.append({0, 2})
                    elif d[i] in (1, 3) and abs(P[0] - a[0]) <= TOL and min(a[1], b[1]) < P[1] < max(a[1], b[1]):
                        passes.append({1, 3})
        if any(not (p & q) for p, q in itertools.combinations(passes, 2)):
            X += 1
    return dict(L=L, X=X, O=O, B=B, J=L + hr.W_CROSS * X + hr.W_OVERLAP * O + hr.W_BEND * B)


def check_output(inp, out, nb=None, cfg=CONFIG):
    """Re-check a design from the output JSON + plan input only (rules 1-6 and metrics), the way the
    organisers' checker would: polylines, not the routing grid. Returns metrics and
    violations [(message, hub index, leg index)]."""
    s = inp["scale"]
    inner = to_m(inp["inner"], s).buffer(TOL)
    obst = to_m(union([inp["walls"], inp["windows"]]), s)
    shapely.prepare(inner)
    shapely.prepare(obst)
    types = {r["room_id"]: r["type"] for r in inp["rooms"]}
    poly = {r["room_id"]: to_m(r["poly"], s) for r in inp["rooms"]}
    if nb is None:
        x1, y1, x2, y2 = inp["inner"].bounds
        nb, _ = room_links(inp["rooms"], inp["doors"], inp["walls"], inp["windows"], inp["wall_depth"],
                           max(x2 - x1, y2 - y1), cfg["passage_factor"])
    eligible = sorted(r for r, t in types.items() if t in DEVICE_TYPES)
    node = {n["node_id"]: (n["x"] / s, n["y"] / s, n["room_id"]) for n in inp["nodes"]}
    hubs = out["hubs"]
    where = {h["hub_id"]: (h["location"]["x"], h["location"]["y"]) for h in hubs}
    where.update({k: v[:2] for k, v in node.items()})
    terminals = list(where.values())
    V = []
    if sorted(r for h in hubs for r in h["assigned_rooms"]) != eligible:
        V.append(("rule 1: eligible rooms are not each assigned to exactly one hub", None, None))
    if sorted(n for h in hubs for n in h["assigned_nodes"]) != sorted(node):
        V.append(("rule 1: nodes are not each assigned to exactly one hub", None, None))
    for gi, h in enumerate(hubs):
        hid, rooms_h = h["hub_id"], h["assigned_rooms"]
        for n in h["assigned_nodes"]:
            if n in node and node[n][2] not in rooms_h:
                V.append((f"rule 1: {hid} serves {n} but not its room", gi, None))
        cap = cfg.get("room_area_cap_m2")
        area = sum(poly[r].area if cap is None else min(poly[r].area, cap) for r in rooms_h if r in poly)
        if len(h["assigned_nodes"]) > hr.MAX_DEVICES:
            V.append((f"rule 2: {hid} serves {len(h['assigned_nodes'])} nodes > 5", gi, None))
        if area > hr.MAX_AREA + 1e-6:
            V.append((f"rule 2: {hid} serves {area:.1f} m2 > 100", gi, None))
        if not zone_connected(rooms_h, nb, types, cfg["zone_mode"]):
            V.append((f"rule 3: {hid} rooms are not connected through doors / open passages", gi, None))
        hp = Point(where[hid])
        if h["room_id"] not in rooms_h or types.get(h["room_id"]) not in DEVICE_TYPES or not poly[h["room_id"]].contains(hp):
            V.append((f"rule 6: {hid} is not inside an eligible room of its own zone", gi, None))
        if hp.distance(obst) < hr.HUB_WALL_CLEARANCE - TOL:
            V.append((f"rule 6: {hid} is {hp.distance(obst):.3f} m from a wall or window", gi, None))
        if any(_near(where[hid], node[n][:2]) for n in node):
            V.append((f"rule 6: {hid} sits on a node point", gi, None))
        seq = h["closed_sequence"]
        if seq[0] != hid or seq[-1] != hid or sorted(seq[1:-1]) != sorted(h["assigned_nodes"]):
            V.append((f"rule 4: {hid} sequence is not {hid} -> each assigned node once -> {hid}", gi, None))
        segs = h["route_segments"]
        if len(segs) != len(seq) - 1:
            V.append((f"rule 4: {hid} has {len(segs)} route segments for {len(seq) - 1} legs", gi, None))
        for k, sg in enumerate(segs):
            pl = [tuple(map(float, q)) for q in sg["polyline"]]
            tag = f"{hid} leg {sg['from']}->{sg['to']}"
            if k < len(seq) - 1 and (sg["from"], sg["to"]) != (seq[k], seq[k + 1]):
                V.append((f"rule 4: {tag} does not follow the closed sequence", gi, k))
            if len(pl) < 2 or sg["from"] not in where or sg["to"] not in where \
                    or not _near(pl[0], where[sg["from"]]) or not _near(pl[-1], where[sg["to"]]):
                V.append((f"rule 4: {tag} does not start and end at its hub / node points", gi, k))
                continue
            if any(_dir(a, b) is None for a, b in zip(pl[:-1], pl[1:])):
                V.append((f"rule 5: {tag} has a segment that is not horizontal or vertical", gi, k))
            line = LineString(pl)
            if not inner.covers(line):
                V.append((f"rule 5: {tag} leaves the inner boundary", gi, k))
            if line.intersects(obst):
                V.append((f"rule 5: {tag} crosses a wall or window", gi, k))
            ends = {sg["from"], sg["to"]}
            if any(line.distance(Point(p)) <= TOL for t, p in where.items() if t not in ends):
                V.append((f"rule 4: {tag} runs through another hub or node point", gi, k))
            if abs(line.length - float(sg["length_m"])) > 0.01:
                V.append((f"JSON: {tag} length_m {sg['length_m']} != polyline length {line.length:.3f}", gi, k))
        if hr.NO_RETRACE:
            back = ring_self_overlap(h)
            if back > TOL:
                V.append((f"rule 4: {hid} ring runs back along its own wire ({back:.3f} m)", gi, None))
    met = polyline_metrics(hubs, terminals)
    met["violations"] = V
    return met


def ring_self_overlap(h):
    """Length (m) a ring covers more than once, from its JSON polylines (NO_RETRACE: must be 0)."""
    lines = defaultdict(list)
    for sg in h["route_segments"]:
        pl = [tuple(map(float, q)) for q in sg["polyline"]]
        for a, b in zip(pl[:-1], pl[1:]):
            d = _dir(a, b)
            if d in (0, 2):
                lines[("H", round(a[1] / TOL))].append((min(a[0], b[0]), max(a[0], b[0])))
            elif d in (1, 3):
                lines[("V", round(a[0] / TOL))].append((min(a[1], b[1]), max(a[1], b[1])))
    o = 0.0
    for ivs in lines.values():
        depth, prev = 0, None
        for x, e in sorted([(lo, 1) for lo, _ in ivs] + [(hi, -1) for _, hi in ivs]):
            if prev is not None and depth > 1:
                o += (depth - 1) * (x - prev)
            depth, prev = depth + e, x
    return o


# --------------------------------------------------------------------------- overlay (drawn from the JSON)
def draw_overlay(inp, out, path, chk=None):
    """Floor-plan overlay from the plan input + output JSON: zones, device / hub IDs, visiting order,
    full rings with return legs, violations in red (deck slide 3)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    s = inp["scale"]
    cols = hr.HUB_COLOURS
    zone_of = {r: gi for gi, h in enumerate(out["hubs"]) for r in h["assigned_rooms"]}
    bad = {(g, k) for _, g, k in (chk or {}).get("violations", []) if g is not None and k is not None}
    bad_hubs = {g for _, g, k in (chk or {}).get("violations", []) if g is not None and k is None}
    x1, y1, x2, y2 = to_m(inp["inner"], s).bounds
    w, h = x2 - x1, y2 - y1
    fig, ax = plt.subplots(figsize=(min(16, 4 + 0.75 * w), min(16, 3.2 + 0.75 * h)))
    for q in polygons(to_m(inp["inner"], s)):
        ax.add_patch(hr._patch(q, fc="#fafafa", ec="#9aa0a6", lw=0.8, ls="--", zorder=0))
    for r in inp["rooms"]:
        q = to_m(r["poly"], s)
        elig = r["type"] in DEVICE_TYPES
        fc = cols[zone_of[r["room_id"]] % len(cols)] if r["room_id"] in zone_of else ("#e8e9eb" if elig else "#d9dadc")
        for part in polygons(q):
            ax.add_patch(hr._patch(part, fc=fc, alpha=0.18 if r["room_id"] in zone_of else 1, ec="#8a8f96", lw=0.4, zorder=1))
        c = q.representative_point()
        ax.text(c.x, c.y - 0.35, f"{r['room_id']} {r['type']}\n{q.area:.1f} m²" + ("" if elig else " · no device"),
                fontsize=6, ha="center", va="top", color="#38424f", zorder=12)
    for g, fc in ((inp["walls"], "#3b3b3b"), (inp["windows"], "#28b5d6")):
        for q in polygons(to_m(g, s)):
            ax.add_patch(hr._patch(q, fc=fc, ec="none", zorder=5))
    for d in inp["doors"]:
        for q in polygons(to_m(d, s)):
            ax.add_patch(hr._patch(q, fc="#f6c1d2", ec="none", zorder=5))
    for gi, hub in enumerate(out["hubs"]):
        c = cols[gi % len(cols)]
        for k, sg in enumerate(hub["route_segments"]):
            pl = np.asarray(sg["polyline"], float)
            red = (gi, k) in bad
            ax.plot(pl[:, 0], pl[:, 1], color="red" if red else c, lw=2.4 if red else 1.6, ls="--" if red else "-",
                    solid_joinstyle="miter", zorder=8)
            seglens = np.abs(np.diff(pl, axis=0)).sum(1)
            if len(seglens) and seglens.max() >= 0.4:
                i = int(np.argmax(seglens))
                (xa, ya), (xb, yb) = pl[i], pl[i + 1]
                ux, uy = (xb - xa) / seglens[i], (yb - ya) / seglens[i]
                mx, my = (xa + xb) / 2, (ya + yb) / 2
                ax.annotate("", xy=(mx + 0.13 * ux, my + 0.13 * uy), xytext=(mx - 0.13 * ux, my - 0.13 * uy),
                            arrowprops=dict(arrowstyle="-|>", color="red" if red else c, lw=1.2, mutation_scale=11),
                            zorder=9)
        for i, n in enumerate(hub["closed_sequence"][1:-1], 1):             # visiting-order badge
            p = next(q for q in out["nodes"] if q["node_id"] == n)
            ax.scatter(p["x"] + 0.28, p["y"] + 0.28, s=70, c=c, edgecolors="white", linewidths=0.8, zorder=18)
            ax.text(p["x"] + 0.28, p["y"] + 0.28, str(i), color="white", fontsize=5.5, fontweight="bold",
                    ha="center", va="center", zorder=19)
    for n in out["nodes"]:
        ax.scatter(n["x"], n["y"], s=150, c="#3d4a5c", edgecolors="white", linewidths=1.1, zorder=14)
        ax.text(n["x"], n["y"], n["node_id"], color="white", fontsize=5.5, fontweight="bold", ha="center",
                va="center", zorder=15)
    for gi, hub in enumerate(out["hubs"]):                # under the devices: a hub may sit right next to one
        x, y = hub["location"]["x"], hub["location"]["y"]
        ax.scatter(x, y, marker="s", s=260, c=cols[gi % len(cols)], edgecolors="red" if gi in bad_hubs else "black",
                   linewidths=2.5 if gi in bad_hubs else 1, zorder=13)
        ax.text(x - 0.32, y - 0.32, hub["hub_id"], color=cols[gi % len(cols)], fontsize=6.5, fontweight="bold",
                ha="center", va="center", zorder=17)
        if hub["ring_length_m"] < 0.6:                    # ring too short to see at plan scale: mark it
            from matplotlib.patches import Circle
            pts = np.array([p for sg in hub["route_segments"] for p in sg["polyline"]], float)
            cx, cy = pts.mean(0)
            ax.add_patch(Circle((cx, cy), 0.5, fill=False, ec=cols[gi % len(cols)], lw=1.4, ls="--", zorder=18))
            far = max(math.hypot(px - x, py - y) for px, py in pts)
            ax.text(cx, cy + 0.62, f"{hub['hub_id']} ring {hub['ring_length_m']:.2f} m\n(hub {far * 100:.0f} cm from its device)",
                    color=cols[gi % len(cols)], fontsize=6, fontweight="bold", ha="center", va="bottom", zorder=18,
                    bbox=dict(fc="white", ec="none", alpha=0.8, pad=1))
    ax.set_xlim(x1 - 0.4, x2 + 0.4)
    ax.set_ylim(y1 - 0.4, y2 + 0.4)
    ax.set_aspect("equal")
    ax.axis("off")
    rm = out.get("reported_metrics") or {}
    title = f"{out['plan_id']}   status: {out.get('status')}"
    if rm:
        title += (f"   {rm['hub_count']} hubs   J = L + 5X + O + 0.5B = {rm['total_length_m']:.1f} + 5×{rm['crossings']}"
                  f" + {rm['overlap_m']:.1f} + 0.5×{rm['bends']} = {rm['cost_J']:.1f}   area gap {rm['area_gap_m2']:.0f} m²")
    ax.set_title(title, fontsize=9)
    lines = [f"{hb['hub_id']} in {hb['room_id']}: " + " → ".join(hb["closed_sequence"])
             + f"   zone {', '.join(hb['assigned_rooms'])} ({hb['zone_area_m2']:.0f} m², {hb['node_count']} nodes)"
             f"   ring {hb['ring_length_m']:.2f} m, {hb['bends']} bends" for hb in out["hubs"]]
    if not out["hubs"] and out.get("reason"):
        lines.append(f"NO DESIGN: {out['reason']}")
    lines.append("square = hub (label at its lower left), dark circle = device")
    lines += [f"VIOLATION: {v}" for v in out.get("violations", [])]
    lines.append("grey = no device (bathroom / stair); pink = doors; cyan = windows; numbered dots = visiting order")
    fig.text(0.5, 0.01, "\n".join(lines), ha="center", va="bottom", fontsize=7)
    fig.subplots_adjust(bottom=0.04 + 0.022 * len(lines), top=0.95)
    fig.savefig(path, dpi=120)
    plt.close(fig)
