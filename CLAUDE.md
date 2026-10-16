# CLAUDE.md — Honeywell Hackathon (ResPlan floor plans → cable routing)

## Project goal
Joint optimisation of control-panel (hub) placement and closed-ring cable routing on ResPlan floor plans.
Authoritative brief: `Cable Ring Challenge - Problem Statement4.pptx` (organisers' deck, 8 slides; read 2026-10-16).
`Honeywell_Hackathon_Problem_Summary.docx` is older; where anything conflicts, the deck and the user decisions below win.

## Hard rules (deck slide 3; a design is invalid if any is broken)
1. **Coverage:** every eligible room (living, bedroom, kitchen, storage) and its node assigned to exactly one hub.
2. **Capacity:** per hub ≤ **5 nodes** and ≤ **100 m² of eligible-room area** (only rooms with a device count).
3. **Zone:** a hub's rooms are connected through doors or open passages only (windows, wall-only adjacency and the entrance do not count).
4. **Ring:** one closed ring per hub: H → every assigned node once → same H, no branches, never ending at another hub. Retracing a door into a single-door room is allowed; shared line length counts as overlap. We also never let a wire run through another hub or node point.
5. **Free space:** cable inside the inner boundary, outside walls and windows, horizontal/vertical only, between rooms only through door/open-passage gaps. A ring may pass through any room, including other hubs' rooms (user confirmed).
6. **Hub point:** inside an eligible room of its own zone, **≥ 0.5 m from walls** (we also keep 0.5 m from windows), not on a node point (may be right next to one). User confirmed 2026-10-16: keep it so even for a single-device zone, where the ring is then only a few cm out and back; the overlay circles and labels any ring under 0.6 m.

## Devices (user decision 2026-10-16, matches deck slide 2)
- ResPlan plans contain no devices; the pipeline adds them. Exactly **one** device (node) in every living room, bedroom, kitchen and storage room; **none** in bathrooms, balconies or stairs.
- Each polygon is one room with one node (deck), so a living room stored as 2 polygons (1.5% of plans) gets 2 devices.
- Device point: room centroid if inside the room and ≥ 0.5 m from walls, windows and the room edge; else the nearest point that is (lower clearance only for rooms too small, with a warning). In the benchmark, node points are fixed inputs from the organisers ("one arbitrary interior point per room, 0.5 m from walls"); devices given in an input JSON are used unchanged.

## Measurement (deck slides 6–7; the checker recomputes everything from the submitted polylines, tolerance 1 mm)
- L: polyline length of every leg, return legs included.
- X (5 each): points where two cables meet **away from a node or hub**, inside one ring or between rings, **once per point**. Touches count; meeting along a shared stretch is overlap, not a crossing.
- O (1/m): length where cables lie on the same line; a stretch covered by k legs counts (k − 1) × its length. Parallel runs on different lines are not overlap.
- B (0.5 each): every direction change along each closed ring, at nodes and the hub too; **a reversal counts once** (deck overrides the earlier "reversal = 2").
- Our grid: a line every 0.1 m plus one line through every device x and y, so node points are exact grid vertices (deck: nodes cannot be moved). Hubs sit on grid vertices.

## Ranking and scoring (slides 3, 6)
1. Fewest hubs. 2. Lowest `J = L + 5X + O + 0.5B`. 3. Tie-break: smaller assigned-area gap between hubs. Weights may be adjusted by the organisers after reference runs.
- Reported values in the JSON are ignored; rank points add up over hidden, unseen plans; **an invalid or missing plan scores zero**.
- Benchmark plans (about 90% of ResPlan): every eligible room ≤ 100 m² and ≥ 2 m², all room points in one connected component of inner − wall − window.

## Deliverables per plan (slides 3, 8)
- Output JSON (slide 8): `plan_id`, `units: "m"`, `hubs[]` with `hub_id`, `room_id`, `location{x,y}`, `assigned_rooms`, `assigned_nodes`, `closed_sequence`, `route_segments[{from,to,length_m,polyline}]`, `ring_length_m`; `reported_metrics{hub_count,total_length_m}`. Metres = units / scale, origin lower-left, y up. Our files add status, violations, warnings, rooms, nodes, links, check and solver info.
- Floor-plan overlay: zones, hub and node IDs, visiting order, full rings with return leg, violations in red.
- Input: "one JSON file per plan from the organisers"; the deck does not show its format. `resplan_solver.load_input_json` reads our own input format (written with `--write-inputs`) and ResPlan-style dicts (GeoJSON, WKT or coordinate lists).

## Scale for ResPlan plans (slide 4)
- ResPlan coordinates are drawing units. `scale = (plan["inner"].area / plan["area"]) ** 0.5` (units per metre). Do **not** use `net_area` (0 in 32% of plans).
- Convert once per plan before any limit/clearance check: m = units / scale, m² = units² / scale². Balconies lie outside `inner`.

## Rule 3 reading (decided 2026-10-16: lenient)
- **User decision: the lenient reading is allowed.** Default `zone_mode="any"`: the door / open-passage chain linking a zone's rooms may pass through any room, including another zone's rooms. `--zone own` (strict: only through the zone's own rooms) and `--zone excluded` (also through bathrooms/stairs) remain as options. `compare_zone_rules.py <id>` draws one plan under both readings.
- Effect measured on 500 random plans (seed 0): strict needed more hubs in 119 of 497 solved plans (+1 hub in 77, +2 in 27, +3 in 13, +4 in 2). Plans at the hub lower bound max(ceil(area/100), ceil(nodes/5)): 489/497 lenient vs 374/497 strict.

## Results (500 random plans, seed 0, lenient, 2026-10-16)
- 476 valid, 21 invalid (18 with a room > 100 m², 3 augmented plans with rooms outside `inner`), 2 failed (unreachable device), 1 skipped (broken). **All 458 plans inside the benchmark filters are valid.** Checker agrees with solver on all.
- J mean 45.9; J − lower bound mean 2.28 (p90 3.2, max 6.4); no crossings in any plan. 0.37 s per plan on 14 workers (per-plan median 3.3 s, max 36 s): ~10 min for the test split, ~1.8 h for all 17,000.

## Results: test split (1,632 plans, lenient, 2026-10-16, in `resplan_output`)
- Latest run (with rip-up and reroute, JSON + images): 20.7 min on 14 workers (0.76 s per plan; per-plan median 6.5 s, max 122 s: PLAN_014527 hit the 90 s budget, then the 30 s rip-up pass). All 17,000 plans would take ~3.6 h.
- 1,586 valid, 43 invalid, 3 failed. **All 1,516 plans inside the benchmark filters are valid**; every invalid/failed plan is outside them (rooms > 100 m² or < 2 m², devices outside the free space). Hubs: 1 in 888 plans, 2 in 660, 3 in 69, 4 in 9, 5 in 3; 1,590 of 1,629 at the hub lower bound. J median 43.5, mean 45.65; J − lower bound mean 1.97; no crossings. Checker agrees with solver on all.
- Rip-up and reroute vs the run before it (same plans): J lower in 1,385 of 1,629 plans (mean 0.29, max 2.2), higher in none; no status or hub-count changes; mean J 45.94 → 45.65 (J − lower bound 2.28 → 1.97); per-plan median runtime 4.3 → 6.5 s (12.6 → 20.7 min for the split).
- Fixed during this run: two overlapping rooms got the same device point (PLAN_015613 crashed). Devices now keep ≥ 0.3 m apart; a shared device point is reported as `failed` instead of crashing.

## Open questions
- Organisers' input JSON format (not in the deck).

## Solver (`hub_routing.py`; ResPlan pipeline `resplan_solver.py` + `run_resplan.py`; example `example_plan.py` + `run_example.py`)
- Stage 1: feasible blocks (≤ 5 nodes, ≤ 100 m², connected per zone_mode), then exact-cover search for partitions into k = lower bound, k+1, … hubs; the first k with a routable design wins (fewest hubs).
- Stage 2: per group, exact ring cost L + 0.5B at every legal hub vertex (direction-aware Dijkstra tables per node, turns at hub/nodes included, best visiting order), ignoring overlaps/crossings → **lower bound** on J. Hub = cheapest vertex, ties to the node centroid.
- Stage 3 (`route_rings`, method 3 only): per hub, route the ring leg by leg for every visiting order; each leg pays the **exact J increase** it causes against the wires already routed: overlap 1/m on used edges, and 5 for passing a point in a way that crosses or touches a wire already there (pass types: which 2 of the 4 edges a wire uses at a point; complementary types = no shared edge), except at hubs/nodes or points that are already crossings. Other hubs/nodes are blocked. Each leg also weighs the exact rest-of-ring cost per arrival direction; a reversal at a node is allowed (1 bend). Leg searches are bounded by 1.5 × unhindered cost + 12 and re-run unbounded if the end is not clearly inside (exact). Orders are tried cheapest lower bound first and stop when none can beat the best J; groupings likewise (sorted by lower bound, stop when the bound exceeds the best J). Soft cap: 90 s / 40 routed groupings per plan.
- (Before 2026-10-16 the crossing penalty was a flat 5 for entering any used point, which also punished harmless merges; switching to the exact penalty removed all crossings and cut mean J by 1.3 on the 500-plan sample.)
- Stage 3b (`improve_rings`, added 2026-10-16): rip-up and reroute of the best 3 routed designs (within 2.5 of the best J). Stage 3 routes legs one after another, so early legs never see later wires; this pass re-routes each leg against all other wires (turns at both ends charged), then each node's two legs together in reverse order (outgoing first), repeating until a round changes nothing (max 5 rounds, 30 s for the best design; runner-ups only before the plan's deadline). A change is kept only if the exact J drops, so J never increases. On 200 test plans: J lower by 0.33 on average (max 1.9; 164 of 200 improved, none worse), no hub/status changes, runtime 7.0 → 9.3 s per plan.
- Stage 4: `measure()` exact J; pick lowest J, then smallest area gap. `validate()` checks rules 1–6 on the grid, and `resplan_solver.check_output()` re-checks the rules and recomputes L, X, O, B from the JSON polylines alone (must agree).
- Example (exact node points, ~2 s): 2 hubs, {Bedroom 2, Kitchen} + {Living, Bedroom 1, Bedroom 3, Storage}, J = 53.7 on both geometries (lower bound 51.7; 53.68 after rip-up and reroute, from 53.70), valid.

## How to run (cmd)
```
cd /d "C:\Users\abhin\OneDrive\Desktop\Honeywell_hackathon"
python run_resplan.py --ids 14433             (one plan: JSON + overlay image)
python run_resplan.py --first 20              (quick look)
python run_resplan.py --split test            (1,632 test plans)
python run_resplan.py --all --no-png          (all 17,000 plans, JSON only)
```
Output per plan (user decision 2026-10-16: JSON **and** image by default): `resplan_output\json\PLAN_<id>.json` and `resplan_output\png\PLAN_<id>.png` (zones, hubs, devices, visiting order, rings; a plan without a design shows its rooms, devices and the reason). Plus `resplan_output\summary.csv` (incl. `in_benchmark`: slide-3 filters — eligible rooms 2–100 m², every device in free space, all devices in one connected part of inner − wall − window). Re-running resumes (a plan is done when both files exist); `--force` redoes. Options: `--no-png` (skip images: ~0.2 s and ~70 KB per plan), `--workers N`, `--write-inputs`, `--zone any|excluded|own` (default any), `--inputs DIR` (input JSON files instead of the pickle).

## Environment
- OS: Windows, commands run in **cmd**
- Python 3.12 (`python` on PATH)
- Project folder: `C:\Users\abhin\OneDrive\Desktop\Honeywell_hackathon`
- Always `cd /d "C:\Users\abhin\OneDrive\Desktop\Honeywell_hackathon"` before running scripts, since they load files by bare filename.

### Known dependency conflict
`pip install -r requirements.txt` (ResPlan) was installed into the **global** Python and upgraded:
- numpy 1.26.2 → 2.4.6 (breaks tensorflow-intel 2.16.1, needs numpy<2)
- pandas 2.1.3 → 3.0.6 (breaks streamlit 1.44.0, needs pandas<3)
- pillow → 12.3.0 (streamlit needs <12)

TODO: move this project into its own venv (`python -m venv .venv` then `.venv\Scripts\activate`) to isolate it.

## Data
- Source: Kaggle `resplan/resplan` (also GitHub `m-agour/ResPlan`; paper arXiv 2508.14006)
- Downloaded with:
  ```
  curl -L -o resplan.zip https://www.kaggle.com/api/v1/datasets/download/resplan/resplan
  tar -xf resplan.zip
  ```
- Licence: data CC BY 4.0, code MIT
- 17,000 residential floor plans, single floor, no furniture/3D, all from South Asian listings
- Splits (from GitHub `split.json`): train 13,053 / val 1,632 / test 1,632 / augmented 683
- ~6.9% near-duplicate plans

### Folder contents
```
Honeywell_hackathon/
├── resplan.zip
├── ResPlan.pkl          ← the dataset
├── resplan_utils.py     ← authors' helper (downloaded from GitHub raw)
├── requirements.txt     ← authors' deps
├── helper_function.py   ← browse plans one by one
├── graph_view.py        ← plan + room graph side by side, prints nodes/edges
├── adjacency_view.py    ← strict graph vs after add_adjacency_edges
├── hub_routing.py       ← hub placement + ring routing solver (reusable)
├── example_plan.py      ← organisers' example plan traced from the slide image
├── run_example.py       ← runs the solver on the example, writes example_*.png + example_solution.json
├── resplan_solver.py    ← ResPlan plan -> devices, hubs, rings -> output JSON; independent polyline checker; overlay
├── run_resplan.py       ← parallel batch runner (pickle or input JSONs) -> resplan_output/json, summary.csv
├── compare_zone_rules.py ← one plan under strict vs lenient rule 3, side-by-side image
├── resplan_output/      ← run results: json/ (one per plan), png/, inputs/, summary.csv
└── Cable Ring Challenge - Problem Statement4.pptx  ← organisers deck (authoritative)
```

## Data facts (measured 2026-10-16 on all 17,000 plans + a 500-plan sample)
- Loads in ~3 s. List of 17,000 dicts. **`p["id"]` ≠ list index** (ids 0–999398); `split.json` lists ids.
- Coordinates are **floats on a 256-unit canvas** (longer side of `inner` = 256), not metres (README is wrong for this file) and not on any native grid. Scale = sqrt(inner.area/area): 6–30 units/m, median 18.3. A 0.1 m grid = 0.96–2.93 units (median 1.83). 0.1 m grid size: median 14k vertices, max 123k.
- `wall_depth` 0.17–0.37 m (median 0.23). Doors and windows sit **in gaps cut into the wall polygons** (door/window ∩ wall = 0), door thickness = wall depth, door width 0.8–1.3 m. So windows must be added as obstacles, doors are passable.
- `inner` = inside of the exterior walls (exterior walls, windows, front door, balconies lie outside it). Room polygons overlap walls slightly (~3% of room area); unlabelled slivers inside `inner` exist (median 0, p95 1% of inner).
- Stored `graph` uses `adjacency` (= wall adjacency + openings merged) → unusable for rule 3; recompute with `plan_to_graph` and keep `via_door` + `via_opening`. `fallback` edges mark missing doors (~1% of plans have an eligible room with no door).
- Eligible rooms per plan 2–10 (mostly 4–6); living multi-part in 1.5% (helper merges into one node); storage in 10% of plans (often < 3 m²); 444 plans (2.6%) have one eligible room > 100 m² (mostly living incl. hallway) → rule 2 cannot be met; 561 plans (3.3%) have non-axis-aligned walls; 7 plans look broken (area < 20 m² or scale > 60).
- Bedrooms almost always open only onto living (+ bathroom/balcony); so the rule-3 reading matters: min hubs 3+ in 21% of plans if zones must connect through own rooms, 2% if through any room.

## Data structure (`ResPlan.pkl`)
- Pickle → Python **list** of 17,000 plan **dicts** (`data[i]`)
- Each plan dict holds Shapely geometries keyed by category:
  `living, bedroom, bathroom, kitchen, storage, stair, balcony, door, window, wall, front_door`
  plus `inner` (plan outline), `wall_depth`, and on Kaggle a precomputed `graph` (NetworkX)
- Some plans spell `balacony`; `normalize_keys()` fixes it
- Geometries are Polygon/MultiPolygon (front_door may be a line)

## resplan_utils.py — key functions
- `plot_plan(plan, ax=None, legend=True, title=None)` — draws colour-filled plan, returns ax (call `plt.show()` yourself)
- `plan_to_graph(plan)` — **strict** room graph. Nodes = rooms (`living_0`, `bedroom_1`, …) with attrs `geometry`, `type`, `area`. Edge `type`: `via_door`, `via_window`, `via_opening`, `direct` (front door → room), `fallback`
- `add_adjacency_edges(G)` — converts to paper/Kaggle taxonomy **in place**: relabels `via_opening`/`fallback` → `adjacency`, adds `adjacency` between rooms separated only by a wall. Use `G.copy()` first if you need the original
- `plot_plan_and_graph(plan, ax=None, title=None)` — overlays graph on plan; uses `plan["graph"]` if present, else builds it
- Others: `get_geometries`, `centroid`, `geometry_to_mask`, `augment_geom`, `buffer_shrink_expand`, `get_plan_width`

### Plot legend for graph overlay
Node shape and marker colour both encode room type (redundant); size scales with room area.
- circle/white = living, square/cyan = bedroom, diamond/magenta = bathroom,
  triangle/yellow = kitchen, X/grey = balcony, star/red = front door
- Marker colours differ from the room fill colours in the legend (two separate schemes)

Edges:
- solid dark blue = `via_door`
- dashed green = `adjacency` / `via_opening`
- solid dark red = `direct`
- dotted orange = `via_window`, dotted grey = `fallback`

Plan 0 example: strict graph has 10 edges (9 doors + front door); `add_adjacency_edges` adds 6 shared-wall edges → 16.

## Room links for rule 3 (decided)
- `resplan_solver.room_links` computes door and open-passage links per room polygon with the same tests as `plan_to_graph` (door touching two rooms; wall-free contact >= 2 x wall depth), but **windows never count** (`plan_to_graph` promotes interior windows to `via_door`) and multi-part living rooms are not merged.
- `add_adjacency_edges` is not used (its edges are shared walls).
- The links only decide zone connectivity (rule 3); the cable geometry comes from obstacle-aware grid routing (walls + windows are obstacles, inner boundary is the limit).

## Collaboration notes
- User prefers iterative, precise steps and direct, honest feedback
- User is running everything from cmd + Notepad; give exact commands and say where each one runs
