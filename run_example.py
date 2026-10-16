"""Run the hub + ring pipeline on the organisers' example plan (slide-3 rules, 0.1 m grid).

cmd:
    cd /d "C:\\Users\\abhin\\OneDrive\\Desktop\\Honeywell_hackathon"
    python run_example.py                  (plan traced from the first image)
    python run_example.py --plan slide5    (geometry of the slide-5 schematic)
    add --show to open the figures

Writes <prefix>_stage2_groupings.png, <prefix>_final_cables.png and <prefix>_solution.json,
prefix = example (traced) or example_slide5.
"""
import json
import sys
import time

import matplotlib
if "--show" not in sys.argv:
    matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import hub_routing as hr
from example_plan import example_plan

ABBR = {"Living": "L", "Bedroom 1": "B1", "Bedroom 2": "B2", "Bedroom 3": "B3", "Kitchen": "K", "Storage": "S"}


def label(group):
    return "{" + ", ".join(ABBR.get(r, r) for r in group) + "}"


def glabel(grouping):
    return " | ".join(label(b) for b in grouping)


def main():
    t0 = time.perf_counter()
    hr.HUB_PRUNE = False            # the stage-2 figure shows the ring cost at every legal hub point
    variant = sys.argv[sys.argv.index("--plan") + 1] if "--plan" in sys.argv else "traced"
    prefix = "example" if variant == "traced" else f"example_{variant}"
    plan = example_plan(variant)
    rooms = plan.device_rooms()
    # node points are fixed inputs (slide 2): add a grid line through each, so none is moved
    grid = hr.Grid(plan, extra_x=[plan.rooms[r].device[0] for r in rooms],
                   extra_y=[plan.rooms[r].device[1] for r in rooms])
    prep = hr.prepare(plan, grid)
    devnode = prep["devnode"]
    node_id = {n: plan.rooms[r].device_label for r, n in devnode.items()}
    print("Nodes (exact grid vertices):",
          ", ".join(f"{plan.rooms[r].device_label} ({grid.X[n]:.2f}, {grid.Y[n]:.2f})" for r, n in devnode.items()))

    # ---------------- stage 1: groupings
    groupings = hr.feasible_groupings(plan)
    min_hubs = min(len(g) for g in groupings)
    lb = max(int(np.ceil(len(rooms) / hr.MAX_DEVICES)), int(np.ceil(plan.area(rooms) / hr.MAX_AREA)))
    print(f"{len(rooms)} eligible rooms, {plan.area(rooms):.0f} m2 -> at least {lb} hubs")
    print(f"Valid groupings (rules 1-3): {len(groupings)}  ("
          + ", ".join(f"{k} hubs: {sum(len(g) == k for g in groupings)}" for k in sorted({len(g) for g in groupings})) + ")")

    # ---------------- stage 2: hub position per group, only for groupings with the fewest hubs
    # (fewest hubs always wins, so groupings with more hubs are never scored)
    t2 = time.perf_counter()
    cache = {}

    def ev(group):
        if group not in cache:
            cache[group] = hr.evaluate_group(plan, grid, group, prep)
        return cache[group]

    finalists = sorted(((len(g), sum(ev(b)["J"] for b in g), hr.area_gap(plan, g), g)
                        for g in groupings if len(g) == min_hubs), key=lambda s: s[1:3])
    print(f"\nStage 2: hub search for the {len(finalists)} groupings with {min_hubs} hubs "
          f"(ring cost L + 0.5B incl. turns at hub/nodes = lower bound on J)   {time.perf_counter() - t2:.1f} s")
    print(f"  {'J2':>6}  {'gap':>4}  grouping: J at centroid start -> J at optimal hub")
    for k, J2, gap, g in finalists:
        print(f"  {J2:6.1f}  {gap:4.0f}  " + "  ".join(f"{label(b)} {ev(b)['J_start']:.1f}->{ev(b)['J']:.1f}" for b in g))

    # ---------------- stage 3: route rings (method 3), lowest lower bound first; skip groupings
    # whose lower bound already exceeds the best exact J found, since they cannot win
    print("\nStage 3: ring routing (sequential with penalties, best of all visiting orders, then rip-up and reroute)")
    F = {devnode[r]: f for r, f in prep["F"].items()}
    results, best_J = [], np.inf
    for k, J2, gap, g in finalists:
        if J2 > best_J + 1e-9:
            print(f"  {glabel(g)}: skipped, lower bound {J2:.1f} > best J {best_J:.2f}")
            continue
        t3 = time.perf_counter()
        loops_g = hr.route_rings(grid, [ev(b) for b in g], prep)
        J_seq = hr.measure(grid, loops_g)["J"]
        loops_g = hr.improve_rings(grid, loops_g, F=F)
        m = hr.measure(grid, loops_g)
        print(f"  {glabel(g)}: J {m['J']:.2f} (L {m['L']:.1f}, X {m['X']}, O {m['O']:.1f}, B {m['B']:.0f}; "
              f"{J_seq:.2f} before rip-up), lower bound {J2:.1f}, area gap {gap:.0f} m2   {time.perf_counter() - t3:.1f} s")
        results.append((m["J"], gap, g, loops_g, J2))
        best_J = min(best_J, m["J"])

    # ---------------- stage 4: score and choose (fewest hubs already fixed, then lowest J, then area gap)
    results.sort(key=lambda r: (round(r[0], 6), r[1]))
    J, gap, g, loops, J2 = results[0]
    issues = hr.validate(plan, grid, g, loops, devnode)
    tot = hr.measure(grid, loops)
    print(f"\nWinner: {min_hubs} hubs, {glabel(g)}, J = {J:.2f}, area gap {gap:.0f} m2, lower bound {J2:.1f}")
    hub_room = {}
    for gi, (b, loop) in enumerate(zip(g, loops)):
        m = hr.measure(grid, [loop])
        hub_room[gi] = next(r for r in b if grid.room_inner[r][loop["hub"]])
        seq = " -> ".join(node_id[n] for n in loop["order"])
        print(f"  H{gi + 1} in {hub_room[gi]} at ({grid.X[loop['hub']]:.1f}, {grid.Y[loop['hub']]:.1f}):  "
              f"H{gi + 1} -> {seq} -> H{gi + 1}   L {m['L']:.1f} m, B {m['B']:.0f}, X {m['X']}, O {m['O']:.1f}")
    print("Validation:", "all 6 hard rules satisfied" if not issues else "; ".join(m for m, _ in issues))
    for J_o, gap_o, g_o, _, J2_o in results[1:]:
        verdict = "provably worse" if J2_o > J + 1e-9 else "not ruled out by its lower bound"
        print(f"  {glabel(g_o)}: J {J_o:.2f}, lower bound {J2_o:.1f} -> {verdict}")
    print(f"Total runtime {time.perf_counter() - t0:.1f} s")

    # ---------------- figure: stage 2
    fig, axes = plt.subplots(1, len(finalists), figsize=(6.8 * len(finalists), 4.9))
    for ax, (k, J2_f, gap_f, g_f) in zip(np.atleast_1d(axes), finalists):
        evs_f = [ev(b) for b in g_f]
        hr.draw_plan(ax, plan, {r: hr.HUB_COLOURS[gi] for gi, b in enumerate(g_f) for r in b}, devices=False)
        Z = np.full(grid.N, np.nan)
        for e in evs_f:
            Z = np.where(np.isnan(Z), e["Jmap"] - e["J"], Z)
        pc = ax.pcolormesh(grid.xs, grid.ys, Z.reshape(grid.ny, grid.nx), shading="nearest",
                           cmap="magma_r", vmin=0, vmax=12, alpha=0.55, zorder=2)
        for r, n in devnode.items():
            hr.draw_node(ax, (grid.X[n], grid.Y[n]), plan.rooms[r].device_label)
        for gi, e in enumerate(evs_f):
            ax.scatter(*e["centroid"], marker="o", s=90, facecolors="none", edgecolors=hr.HUB_COLOURS[gi],
                       linewidths=2, zorder=13)
            hr.draw_hub(ax, grid, e["hub"], hr.HUB_COLOURS[gi], f"H{gi + 1}")
        ax.set_title(f"{glabel(g_f)}\nstage-2 J = " + " + ".join(f"{e['J']:.1f}" for e in evs_f)
                     + f" = {J2_f:.1f}", fontsize=10)
    fig.colorbar(pc, ax=axes, shrink=0.7, label="extra ring cost vs best hub vertex (m)")
    fig.suptitle("Stage 2: hub search for each 2-hub grouping (circle = node centroid start, "
                 "square = best hub; coloured = legal hub vertices)", fontsize=11, y=0.93)
    fig.savefig(f"{prefix}_stage2_groupings.png", dpi=130, bbox_inches="tight")

    # ---------------- figure: final overlay
    fig, ax = plt.subplots(figsize=(13, 9.6))
    hr.draw_plan(ax, plan, {r: hr.HUB_COLOURS[gi] for gi, b in enumerate(g) for r in b}, devices=False)
    bad_legs = {ref for _, ref in issues if ref and ref[1] is not None}
    bad_hubs = {ref[0] for _, ref in issues if ref and ref[1] is None}
    hr.draw_rings(ax, grid, loops, bad_legs)
    for r, n in devnode.items():
        hr.draw_node(ax, (grid.X[n], grid.Y[n]), plan.rooms[r].device_label)
    lines = []
    for gi, (b, loop) in enumerate(zip(g, loops)):
        c = hr.HUB_COLOURS[gi]
        hr.draw_hub(ax, grid, loop["hub"], c, f"H{gi + 1}", gi in bad_hubs)
        for i, n in enumerate(loop["order"], 1):          # visiting-order badge
            ax.scatter(grid.X[n] + 0.3, grid.Y[n] + 0.3, s=120, c=c, edgecolors="white", linewidths=1, zorder=18)
            ax.text(grid.X[n] + 0.3, grid.Y[n] + 0.3, str(i), color="white", fontsize=7, fontweight="bold",
                    ha="center", va="center", zorder=19)
        m = hr.measure(grid, [loop])
        seq = " → ".join(node_id[n] for n in loop["order"])
        lines.append(f"H{gi + 1} ({hub_room[gi]}): H{gi + 1} → {seq} → H{gi + 1}    zone {', '.join(b)} "
                     f"({plan.area(b):.0f} m², {len(b)} nodes)    L {m['L']:.1f} m, B {m['B']:.0f}, "
                     f"X {m['X']}, O {m['O']:.1f}")
    ax.set_title(f"{len(loops)} hubs   J = L + 5X + O + 0.5B = {tot['L']:.1f} + 5×{tot['X']} + {tot['O']:.1f} "
                 f"+ 0.5×{tot['B']:.0f} = {tot['J']:.1f}   area gap {gap:.0f} m²   "
                 f"violations: {'none' if not issues else len(issues)}", fontsize=11)
    fig.text(0.5, 0.06, "\n".join(lines) + "\nSmall numbered circles = visiting order; arrows = ring direction; "
             "0.1 m grid; violations would be drawn in red", ha="center", va="top", fontsize=9.5)
    fig.savefig(f"{prefix}_final_cables.png", dpi=130, bbox_inches="tight")

    # ---------------- JSON deliverable
    sol = {
        "plan": f"organisers' example ({variant} geometry)", "units": "metres", "grid_m": grid.h,
        "valid": not issues, "violations": [m for m, _ in issues],
        "ranking": {"hubs": len(loops), "J": round(tot["J"], 2), "L": round(tot["L"], 2), "X": tot["X"],
                    "O": round(tot["O"], 2), "B": int(tot["B"]), "area_gap_m2": round(gap, 2)},
        "nodes": [{"id": plan.rooms[r].device_label, "room": r,
                   "xy": [float(grid.X[n]), float(grid.Y[n])]} for r, n in devnode.items()],
        "hubs": [],
    }
    for gi, (b, loop) in enumerate(zip(g, loops)):
        hid = f"H{gi + 1}"
        names = [hid] + [node_id[n] for n in loop["order"]] + [hid]
        m = hr.measure(grid, [loop])
        sol["hubs"].append({
            "id": hid, "xy": [float(grid.X[loop["hub"]]), float(grid.Y[loop["hub"]])], "room": hub_room[gi],
            "zone": list(b), "zone_area_m2": round(plan.area(b), 2), "sequence": names,
            "legs": [{"from": a, "to": z, "polyline": [[round(x, 2), round(y, 2)] for x, y in grid.corners(leg)],
                      "length_m": round(grid.path_length(leg), 2)}
                     for a, z, leg in zip(names[:-1], names[1:], loop["legs"])],
            "ring_length_m": round(m["L"], 2), "bends": int(m["B"]),
        })
    with open(f"{prefix}_solution.json", "w", encoding="utf-8") as f:
        json.dump(sol, f, indent=2)
    print(f"Saved {prefix}_stage2_groupings.png, {prefix}_final_cables.png, {prefix}_solution.json")
    if "--show" in sys.argv:
        plt.show()


if __name__ == "__main__":
    main()
