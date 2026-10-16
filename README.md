# HackHive Control Hub: cable ring routing for ResPlan floor plans

Solution for the Honeywell UR Hackathon 2026H2 **Cable Ring Challenge**. For every residential floor plan in the
[ResPlan](https://www.kaggle.com/datasets/resplan/resplan) dataset, the pipeline:

1. adds **one device** to every living room, bedroom, kitchen and storage room (none in bathrooms, balconies or stairs),
2. groups the rooms into **zones**, using as few control hubs as possible,
3. places each **hub** and chooses the order its devices are visited,
4. routes **one closed cable ring per hub** on a 0.1 m grid, horizontal and vertical only, through doors and open passages,
5. writes **one JSON file per plan** (organisers' format), plus an optional floor-plan overlay image.

It is plain Python: no manual steps, no AI or vision at run time.

## Rules and scoring (organisers' deck)

A design is valid only if all six rules hold:

1. **Coverage:** every eligible room and its device belong to exactly one hub.
2. **Capacity:** at most 5 devices and 100 m² of eligible-room area per hub.
3. **Zone:** a hub's rooms are connected through doors or open passages (not windows or walls). The lenient reading is used: the chain may pass through any room.
4. **Ring:** each hub has one closed ring, H → each of its devices once → the same H, with no branches.
5. **Free space:** cables stay inside the plan, never cross walls or windows, run only horizontally or vertically, and pass between rooms only through door or open-passage gaps.
6. **Hub point:** each hub sits inside one of its own eligible rooms, at least 0.5 m from walls.

Valid designs are ranked by **fewest hubs**, then lowest **J = L + 5X + O + 0.5B**:

| Term | Meaning |
|---|---|
| L | cable length in m, return legs included |
| X | crossings (5 each) |
| O | overlap: metres of cable on the same line (1 per m) |
| B | bends: every change of direction (0.5 each) |

Ties go to the smaller area gap between hubs.

## How it works

| Stage | What happens |
|---|---|
| 1. Grouping | Finds every split of the rooms into zones that meets rules 2–3, and keeps only splits with the fewest hubs. |
| 2. Hub search | For each zone, computes the exact ring cost (length + bends, best visiting order) at every legal hub point and keeps the cheapest. This cost is a lower bound on the final J. |
| 3. Routing | Routes each ring leg by leg, for every visiting order. Each leg pays exactly what it adds to J (overlap, crossings, bends). Orders and groupings whose lower bound can't beat the best J so far are skipped. |
| 4. Choice and check | Keeps the lowest J, then checks rules 1–6 again. A separate checker recomputes L, X, O and B from the output polylines alone. |

The organisers fix device positions, so the grid gets an extra line through every device; devices are never moved.

## Setup

Python 3.12 with numpy, scipy, shapely and matplotlib. Download the dataset into this folder:

```
curl -L -o resplan.zip https://www.kaggle.com/api/v1/datasets/download/resplan/resplan
tar -xf resplan.zip
```

`ResPlan.pkl` and `resplan.zip` are not in the repository because both are larger than GitHub's 100 MB file limit.

## Run (Windows cmd, from this folder)

```
python run_resplan.py --ids 14433           one plan
python run_resplan.py --ids 14433,2401      chosen ResPlan ids
python run_resplan.py --first 20            first 20 plans
python run_resplan.py --split test          the 1,632 test-split plans
python run_resplan.py --all --no-png        all 17,000 plans, JSON only (about 2 h on 14 cores)
```

Every plan gets two files:

- `resplan_output\json\PLAN_<id>.json`: the design (organisers' format plus checks).
- `resplan_output\png\PLAN_<id>.png`: the floor plan with zones, hubs, devices, visiting order and wire rings. A plan without a design shows its rooms, devices and the reason.

`resplan_output\summary.csv` gets one row per plan. Re-running skips plans that already have both files; `--force` redoes them.

Options:

| Option | Effect |
|---|---|
| `--no-png` | skip the images (about 0.2 s and 70 KB per plan) |
| `--workers N` | number of worker processes |
| `--zone any\|excluded\|own` | which reading of rule 3 to use (default `any`) |
| `--write-inputs` | also save each plan's input JSON |
| `--inputs DIR` | read plan input JSON files instead of the pickle |

## Output JSON (per plan)

The organisers' fields:

- `plan_id`
- `units` (`"m"`)
- `hubs[]`, each with `hub_id`, `room_id`, `location`, `assigned_rooms`, `assigned_nodes`, `closed_sequence`, `route_segments` (`from`, `to`, `length_m`, `polyline`) and `ring_length_m`
- `reported_metrics`

Coordinates are in metres (drawing units ÷ scale), origin lower-left, y up. Each file also includes:

- `status` (valid / invalid / failed / skipped)
- `violations` and `warnings`
- the rooms and devices
- the independent check
- `benchmark`: whether the plan passes the organisers' filters

## Results (test split, 1,632 plans)

- All **1,516** plans inside the organisers' benchmark filters are **valid**. The invalid plans have a room over 100 m², a room under 2 m², or a device outside the plan's usable space.
- Hubs per plan: 1 in 888 plans, 2 in 660, 3 in 69, 4 in 9, 5 in 3.
- No crossings in any plan. Median J is 43.8.
- About 0.5 s per plan on 14 worker processes.

## Files

| File | Purpose |
|---|---|
| `run_resplan.py` | Batch runner (the one to run): selects plans, runs them in parallel, writes JSON, PNG and summary.csv |
| `resplan_solver.py` | Per plan: units to metres, device placement, door/opening links, output JSON, independent checker, overlay |
| `hub_routing.py` | The algorithm: grid, stages 1–4, scoring (J) and rule checks |
| `run_example.py`, `example_plan.py` | The organisers' 14 m × 9 m example plan |
| `compare_zone_rules.py` | One plan drawn under the strict and the lenient reading of rule 3 |
| `resplan_utils.py` | ResPlan authors' helper (MIT); used by the viewer scripts, not by the pipeline |
| `helper_function.py`, `graph_view.py`, `adjacency_view.py`, `base_floorplan.py` | Plan viewers |
| `CLAUDE.md` | Detailed notes: rules, decisions, data facts, measurements |
| `Cable Ring Challenge - Problem Statement4.pptx` | Organisers' problem statement |

ResPlan data: CC BY 4.0 (Kaggle `resplan/resplan`, GitHub `m-agour/ResPlan`).
