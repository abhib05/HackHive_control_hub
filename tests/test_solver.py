"""Regression tests for the solver (python -m pytest tests -q, from the project folder).

They run on the organisers' example plan (no dataset needed); the last test also solves two ResPlan
plans if ResPlan.pkl is in the folder.
"""
import os
import pickle
import sys
import itertools

import numpy as np
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import hub_routing as hr                  # noqa: E402
import resplan_solver as rs               # noqa: E402
from example_plan import example_plan     # noqa: E402


@pytest.fixture(scope="module")
def setup():
    plan = example_plan("traced")
    rooms = plan.device_rooms()
    grid = hr.Grid(plan, extra_x=[plan.rooms[r].device[0] for r in rooms],
                   extra_y=[plan.rooms[r].device[1] for r in rooms])
    prep = hr.prepare(plan, grid)
    return plan, grid, prep


def leg_cost(grid, usage, path, terminals, src_dir=None, next_dir=None):
    """Cost of a path under _penalised_graph's rules, computed directly (reference)."""
    d = grid.dirs(path)
    c = grid.path_length(path) + hr.W_BEND * hr.BENDS[d[:-1], d[1:]].sum()
    if src_dir is not None:
        c += hr.TURN[src_dir, d[0]]
    if next_dir is not None:
        c += hr.TURN[d[-1], next_dir]
    e = grid.edge_ids(path)
    c += hr.W_OVERLAP * (grid.elen[e] * (usage.edge_use[e] > 0)).sum()
    term = set(int(t) for t in terminals)
    cross = usage.crossing_points()
    for k in range(1, len(path) - 1):
        v = path[k]
        if v in term:
            c += hr.BLOCKED
            continue
        t = hr.PASS_TYPE[hr.OPP[d[k - 1]], d[k]]
        if usage.mask[v] & (1 << hr.COMP[t]) and not cross[v]:
            c += hr.W_CROSS
    return c


def random_usage(grid, devs, rng, n_legs):
    usage = hr.Usage(grid)
    free = np.nonzero(grid.free)[0]
    for _ in range(n_legs):
        a, b = rng.choice(free, 2, replace=False)
        p = grid.route(grid.G0, int(a), int(b))
        if p:
            usage.add(p)
    return usage


def test_corner_lines_never_move_devices():
    """Optional (corner) lines near a device line are dropped, not merged with it (found 2026-10-16:
    merging moved device lines by up to 1 mm, which flagged 13 of 100 plans as outside the benchmark)."""
    xs = hr.Grid._lines(0.0, 2.0, 0.1, [1.0004, 1.5], 0.005, soft=[1.0012, 0.999, 1.3, 1.301, 2.5])
    assert 1.0004 in xs and 1.5 in xs
    assert not np.any((np.abs(xs - 1.0004) > 0) & (np.abs(xs - 1.0004) < 0.005))
    assert np.any(np.abs(xs - 1.3005) < 1e-9)          # soft lines merge among themselves
    assert xs.max() <= 2.0                               # and stay inside the range


def test_sparse_graph_equals_full(setup):
    plan, grid, prep = setup
    devs = list(prep["devnode"].values())
    term = np.array(sorted(devs))
    rng = np.random.default_rng(1)
    for _ in range(5):
        usage = random_usage(grid, devs, rng, 6)
        b = int(rng.choice(devs))
        full = hr._penalised_graph(grid, usage, b, term)
        sparse = hr._penalised_graph(grid, usage, b, term, scratch=True)
        assert np.allclose(full.data[:-4], sparse.data[:-4])


def test_guided_search_is_exact(setup):
    """The A*-guided search (and its reversed form towards a hub) finds paths exactly as cheap as the
    plain Dijkstra search, against random existing wires and with end turns."""
    plan, grid, prep = setup
    devs = list(prep["devnode"].values())
    term = np.array(sorted(devs))
    free = np.nonzero(grid.free & (grid.wall_dist > 0.5))[0]
    rng = np.random.default_rng(2)
    for k in range(40):
        usage = random_usage(grid, devs, rng, 5)
        a = int(rng.choice(devs))
        b = int(rng.choice([d for d in devs if d != a])) if k % 2 else int(rng.choice(free))   # device or "hub"
        if b not in devs:
            a, b = b, a                                       # hub -> device, or device -> hub below
        if k % 4 == 3:
            a, b = b, a
        sd = int(rng.integers(4)) if k % 3 else None
        nd = int(rng.integers(4)) if k % 5 else None
        t = np.unique(np.append(term, [a, b]))
        hr.GUIDED = False
        p0 = hr._route_leg(grid, usage, a, b, t, sd, nd)
        hr.GUIDED = True
        p1 = hr._route_leg(grid, usage, a, b, t, sd, nd)
        assert (p0 is None) == (p1 is None)
        if p0 is not None:
            assert p1[0] == a and p1[-1] == b
            assert leg_cost(grid, usage, p1, t, sd, nd) == pytest.approx(leg_cost(grid, usage, p0, t, sd, nd), abs=1e-3)


def test_k_shortest_legs_and_beam_ring(setup):
    plan, grid, prep = setup
    devices = list(prep["devnode"].values())
    a, b = devices[:2]
    terminals = np.asarray(devices)
    usage = hr.Usage(grid)
    paths = hr.k_shortest_legs(grid, usage, a, b, terminals, k=3)
    assert paths and paths[0] == hr._route_leg(grid, usage, a, b, terminals)
    assert len({tuple(p) for p in paths}) == len(paths)
    assert all(p[0] == a and p[-1] == b and len(set(p)) == len(p) for p in paths)
    costs = [leg_cost(grid, usage, p, terminals) for p in paths]
    assert costs == sorted(costs)

    block = next(g for g in hr.feasible_blocks(plan) if len(g) == 1)
    ev = hr.evaluate_group(plan, grid, block, prep)
    order = ev["order"]
    ring_terminals = np.unique(np.asarray([ev["hub"], *ev["dev"]]))
    f = {prep["devnode"][room]: table for room, table in prep["F"].items()}
    beam = hr.beam_loop(grid, ev["hub"], order, [], ring_terminals, f)
    assert beam is not None
    points = [beam["hub"], *beam["order"], beam["hub"]]
    assert all(leg[0] == start and leg[-1] == end
               for leg, start, end in zip(beam["legs"], points[:-1], points[1:]))
    assert np.isfinite(hr.measure(grid, [beam])["J"])


def test_held_karp_matches_exhaustive_tsp():
    rng = np.random.default_rng(19)
    for n in range(1, 6):
        dist = rng.uniform(1, 10, (n + 1, n + 1))
        np.fill_diagonal(dist, 0)
        order = hr._held_karp_tour(dist)
        def tour_cost(tour):
            path = (0,) + tuple(i + 1 for i in tour) + (0,)
            return sum(dist[a, b] for a, b in zip(path[:-1], path[1:]))
        assert tour_cost(order) == pytest.approx(
            min(tour_cost(p) for p in itertools.permutations(range(n))))


def test_measure_matches_polyline_checker(setup):
    """measure() (grid) and polyline_metrics() (JSON polylines, independent) agree on L, X, O, B for
    random rings that overlap and cross."""
    plan, grid, prep = setup
    devs = list(prep["devnode"].values())
    free = np.nonzero(grid.free & (grid.wall_dist > 0.5))[0]
    rng = np.random.default_rng(3)
    for _ in range(60):
        loops = []
        for _r in range(int(rng.integers(1, 3))):
            hub = int(rng.choice(free))
            order = [int(x) for x in rng.choice(devs, int(rng.integers(1, 4)), replace=False)]
            pts = [hub] + order + [hub]
            legs = [grid.route(grid.G0, a, b) for a, b in zip(pts[:-1], pts[1:])]
            if any(leg is None for leg in legs):
                continue
            loops.append(dict(hub=hub, order=order, legs=legs))
        if not loops or len({l["hub"] for l in loops}) < len(loops) or \
                len({n for l in loops for n in l["order"]}) < sum(len(l["order"]) for l in loops):
            continue
        m = hr.measure(grid, loops)
        hubs = [{"route_segments": [{"polyline": grid.corners(leg)} for leg in l["legs"]]} for l in loops]
        term = [(grid.X[v], grid.Y[v]) for l in loops for v in [l["hub"]] + l["order"]]
        p = rs.polyline_metrics(hubs, term)
        assert p["L"] == pytest.approx(m["L"], abs=1e-3)
        assert p["O"] == pytest.approx(m["O"], abs=1e-3)
        assert p["B"] == m["B"]
        assert p["X"] == m["X"]


def test_hub_prune_gives_same_hub(setup):
    plan, grid, prep = setup
    for g in hr.feasible_blocks(plan)[:25]:
        hr.HUB_PRUNE = True
        a = hr.evaluate_group(plan, grid, g, prep)
        hr.HUB_PRUNE = False
        b = hr.evaluate_group(plan, grid, g, prep)
        hr.HUB_PRUNE = True
        assert a["hub"] == b["hub"]
        assert a["J"] == pytest.approx(b["J"], abs=1e-6)


def test_adaptive_hub_search_matches_full_scan(setup):
    plan, grid, prep = setup
    old_search, old_prune = hr.HUB_SEARCH, hr.HUB_PRUNE
    try:
        for group in hr.feasible_blocks(plan)[:8]:
            hr.HUB_SEARCH, hr.HUB_PRUNE = "adaptive", True
            adaptive = hr.evaluate_group(plan, grid, group, prep)
            hr.HUB_SEARCH, hr.HUB_PRUNE = "exhaustive", False
            full = hr.evaluate_group(plan, grid, group, prep)
            assert adaptive["hub"] == full["hub"]
            assert adaptive["J"] == pytest.approx(full["J"], abs=1e-6)
    finally:
        hr.HUB_SEARCH, hr.HUB_PRUNE = old_search, old_prune


def test_random_restarts_are_deterministic_and_no_worse_than_one_start(setup):
    plan, grid, prep = setup
    old_search = hr.HUB_SEARCH
    try:
        for group in hr.feasible_blocks(plan)[:5]:
            hr.HUB_SEARCH = "greedy"
            one = hr.evaluate_group(plan, grid, group, prep)
            hr.HUB_SEARCH = "random_restart"
            many = hr.evaluate_group(plan, grid, group, prep)
            again = hr.evaluate_group(plan, grid, group, prep)
            assert many["J"] <= one["J"] + 1e-6
            assert many["hub"] == again["hub"]
            assert many["J"] == pytest.approx(again["J"], abs=1e-6)
    finally:
        hr.HUB_SEARCH = old_search


def test_example_design_valid(setup):
    plan, grid, prep = setup
    res = hr.design(plan, grid, prep)
    assert res["k"] == 2
    assert not hr.validate(plan, grid, res["groups"], res["loops"], prep["devnode"])
    assert res["J"] <= 53.70 + 1e-6                 # 53.68 since rip-up and reroute (2026-10-16)
    assert res["J"] >= res["lb"] - 1e-6


@pytest.mark.skipif(not os.path.exists(os.path.join(HERE, "ResPlan.pkl")), reason="ResPlan.pkl not in the folder")
def test_resplan_plans_valid_and_checked():
    with open(os.path.join(HERE, "ResPlan.pkl"), "rb") as f:
        data = {p["id"]: p for p in pickle.load(f) if p["id"] in (976, 14433)}
    for pid, p in data.items():
        out, _ = rs.solve(rs.from_resplan(p))
        assert out["status"] == "valid", (pid, out.get("violations"))
        assert out["check"]["agrees_with_solver"]
        rm = out["reported_metrics"]
        assert rm["hub_count"] == rm["hub_lower_bound"]
        assert rm["cost_J"] >= rm["lower_bound_J"] - 1e-6


def test_room_area_cap_and_hub_lower_bound():
    """A room > 100 m2 cannot be split, so the hub lower bound never exceeds the number of rooms; with the
    room-area cap (default) such a room counts 100 m2 towards its hub."""
    from shapely.geometry import box
    rooms = [hr.Room("L", "living", box(0, 0, 21, 10)), hr.Room("B", "bedroom", box(21, 0, 30, 10))]   # 210 + 90 m2
    nb = {"L": {"B"}, "B": {"L"}}
    full = hr.Plan(rooms, box(-1, -1, -0.5, -0.5), neighbours=nb, zone_mode="any", oversize_ok=True)
    capped = hr.Plan(rooms, box(-1, -1, -0.5, -0.5), neighbours=nb, zone_mode="any", oversize_ok=True, area_cap=100.0)
    assert hr.hub_lower_bound(full) == 2            # area bound 3 > 2 rooms: capped at the room count
    assert full.area(["L"]) == pytest.approx(210) and capped.area(["L"]) == pytest.approx(100)
    assert capped.block_ok(["L"]) and not capped.block_ok(["L", "B"])
    assert list(hr.groupings_with_hubs(capped, 2, hr.feasible_blocks(capped))) == [[("L",), ("B",)]]


def test_plan_with_rooms_over_100m2_gets_a_design():
    """PLAN_007394: 5 eligible rooms, 519 m2 (living 212.5 m2, a bedroom 116.2 m2). The area bound said 6 hubs,
    more than the 5 rooms, so no grouping was ever tried ("failed"). Now: 5 hubs; valid with the cap, invalid
    (rule 2) with full room areas."""
    with open(os.path.join(HERE, "ResPlan.pkl"), "rb") as f:
        p = next(q for q in pickle.load(f) if q["id"] == 7394)
    inp = rs.from_resplan(p)
    out, _ = rs.solve(inp)
    assert out["status"] == "valid" and out["reported_metrics"]["hub_count"] == 5
    assert out["check"]["agrees_with_solver"]
    out, _ = rs.solve(inp, dict(rs.CONFIG, room_area_cap_m2=None))
    assert out["status"] == "invalid" and out["reported_metrics"]["hub_count"] == 5
    assert all(v.startswith("rule 2") for v in out["violations"])


def test_image_input_matches_vector_plan(tmp_path):
    """A ResPlan plan drawn with resplan_utils.plot_plan (legend, title, black room edges) and read back from the
    PNG with its floor area gives the same rooms, devices and hub count, and nearly the same J."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import image_input
    import resplan_utils
    with open(os.path.join(HERE, "ResPlan.pkl"), "rb") as f:
        p = next(q for q in pickle.load(f) if q["id"] == 14433)
    fig, ax = plt.subplots(figsize=(8, 8), dpi=100)
    resplan_utils.plot_plan(p, ax=ax, legend=True, title="plan 14433")
    ax.set_axis_off()
    path = str(tmp_path / "plan.png")
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    plan, info = image_input.plan_from_image(path, p["area"])
    assert info["palette"] == "resplan" and info["pixels_matched"] > 0.95
    out, _, _ = rs.solve_safe(plan)
    ref, _ = rs.solve(rs.from_resplan(p))
    assert out["status"] == "valid" and out["check"]["agrees_with_solver"]
    assert out["reported_metrics"]["hub_count"] == ref["reported_metrics"]["hub_count"]
    assert sum(r["has_device"] for r in out["rooms"]) == sum(r["has_device"] for r in ref["rooms"])
    assert abs(out["reported_metrics"]["cost_J"] - ref["reported_metrics"]["cost_J"]) < 1.0


@pytest.mark.skipif(not __import__("image_input").ocr_available(), reason="Tesseract OCR not installed")
def test_image_input_titles_and_legend():
    """OCR modes: room names written in the rooms (black walls on white) and a legend with colour swatches."""
    import image_input
    with open(os.path.join(HERE, "ResPlan.pkl"), "rb") as f:
        data = {q["id"]: q for q in pickle.load(f) if q["id"] in (976, 2814)}
    for name, pid, mode in (("titles_976.png", 976, "titles"), ("legend_2814.png", 2814, "legend")):
        plan, info = image_input.plan_from_image(os.path.join(HERE, "sample_images", name), data[pid]["area"])
        assert info["mode"] == mode
        out, _, _ = rs.solve_safe(plan)
        ref, _ = rs.solve(rs.from_resplan(data[pid]))
        assert out["status"] == "valid" and out["check"]["agrees_with_solver"]
        dev = lambda o: sorted(r["type"] for r in o["rooms"] if r["has_device"])
        assert dev(out) == dev(ref)
        assert out["reported_metrics"]["hub_count"] == ref["reported_metrics"]["hub_count"]


def test_area_labels_parsing():
    import image_input
    got = image_input.area_labels([("12.1 m?", 90, (0, 0, 2, 2)), ("17,0 m²", 90, (0, 0, 2, 2)),
                                   ("371m2", 50, (0, 0, 2, 2)), ("3.6 m", 90, (0, 0, 2, 2)),
                                   ("183 sq ft", 90, (0, 0, 2, 2))])
    vals = [c for c, *_ in got]
    assert vals[0] == [12.1] and vals[1] == [17.0] and 37.1 in vals[2]
    assert len(got) == 4                                   # "3.6 m" is a length, not an area
    assert abs(vals[3][0] - 17.0) < 0.01                   # 183 sq ft


@pytest.mark.skipif(not __import__("image_input").ocr_available(), reason="Tesseract OCR not installed")
def test_image_scale_from_area_labels():
    """No floor area given: the scale comes from the "17.5 m2" labels written in the rooms."""
    import image_input
    plan, info = image_input.plan_from_image(os.path.join(HERE, "sample_images", "titles_area_976.png"))
    assert info["scale_source"].startswith("room area labels") and len(info["area_labels"]) >= 3
    out, _, _ = rs.solve_safe(plan)
    assert out["status"] == "valid" and out["reported_metrics"]["hub_count"] == 1
    assert sorted(r["type"] for r in out["rooms"] if r["has_device"]) == ["bedroom", "bedroom", "kitchen", "living"]
