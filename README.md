# HackHive Control Hub: cable ring routing for ResPlan floor plans

Solution for the Honeywell UR Hackathon 2026H2 **Cable Ring Challenge**. For every residential floor plan in the
[ResPlan](https://www.kaggle.com/datasets/resplan/resplan) dataset, the pipeline:

1. adds **one device** to every living room, bedroom, kitchen and storage room (none in bathrooms, balconies or stairs),
2. groups the rooms into **zones**, using as few control hubs as possible,
3. places each **hub** and chooses the order its devices are visited,
4. routes **one closed cable ring per hub** on a 0.1 m grid (plus lines through every device and lines that hug every wall corner and door jamb), horizontal and vertical only, through doors and open passages,
5. writes **one JSON file per plan** (organisers' format), plus an optional floor-plan overlay image.

It is plain Python: no manual steps, no AI or vision at run time.

## Rules and scoring (organisers' deck)

A design is valid only if all six rules hold:

1. **Coverage:** every eligible room and its device belong to exactly one hub.
2. **Capacity:** at most 5 devices and 100 m² of eligible-room area per hub. A room larger than 100 m² counts as 100 m² towards its hub (the hub serves part of it); `--full-room-area` counts the whole room instead, as the deck words it.
3. **Zone:** a hub's rooms are connected through doors or open passages (not windows or walls). The lenient reading is used: the chain may pass through any room.
4. **Ring:** each hub has one closed ring, H → each of its devices once → the same H, with no branches. A ring never runs back along its own wire (no line used twice by one ring), so a hub with one device gets a small rectangular loop.
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
| 3. Routing | Routes each ring leg by leg, for every visiting order. Each leg pays exactly what it adds to J (overlap, crossings, bends). Orders and groupings whose lower bound can't beat the best J so far are skipped. Then the best designs are re-routed: each leg, and each device's pair of legs, is routed again against all the other wires, and a change is kept only if J drops. |
| 4. Choice and check | Keeps the lowest J, then checks rules 1–6 again. A separate checker recomputes L, X, O and B from the output polylines alone. |

The organisers fix device positions, so the grid gets an extra line through every device; devices are never moved. It also gets lines 1.2 cm either side of every wall, window and outline corner, so a wire can pass a corner or a door jamb tightly instead of keeping to the nearest 0.1 m line. Each wire search is an exact shortest-path search on that grid, guided by the stage-2 distance tables (an A* potential), so it only looks at near-cheapest routes.

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
python run_resplan.py --all --no-png        all 17,000 plans, JSON only (about 3.5 h on 14 cores)
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
| `--full-room-area` | rule 2 with full room areas: a room over 100 m² makes the plan invalid (default: it counts 100 m²) |
| `--write-inputs` | also save each plan's input JSON |
| `--inputs DIR` | read plan input JSON files instead of the pickle |
| `--image FILE ... --area M2 ...` | read colour-coded floor-plan images instead (see below); output in `image_output\` |
| `--palette FILE` | with `--image`: JSON `{"#rrggbb": "bedroom", ...}` for an image's own colours |
| `--image-mode auto\|colour\|legend\|titles` | with `--image`: how room types are read (default `auto`) |

### Floor-plan images

```
python run_resplan.py --image sample_images\plan_14433.png --area 120.77
```

`image_input.py` reads a colour-coded PNG/JPG (ResPlan's `plot_plan` colours, Cable Ring Studio's input view, or your own palette with `--palette`): every pixel is matched to a room type, wall, door, window or front door; legends and titles outside the plan are ignored. `--area` is the floor area inside the outer walls in m²; the deck's scale formula `scale = sqrt(outline area / area)` turns pixels into metres, and the plan then goes through exactly the same steps as a ResPlan plan (devices, 0.1 m grid, zones, hubs, rings, checker), giving one JSON and one overlay PNG. Checked on 30 random ResPlan plans drawn with `plot_plan` and read back: all valid, same hub and device counts as the original, J within 1 in 29 of 30 (mean +0.2). Room types come from one of three things (`--image-mode`, default `auto` tries them in this order):

- **colour**: the room colours (known palettes or `--palette`);
- **legend**: a legend whose labels name room types (read with OCR) gives each swatch colour its type;
- **titles**: room names written inside the rooms ("Master Bedroom", "Kitchen", "Toilet", ...) on a black-and-white drawing. Walls are the thick dark lines, door gaps are closed to separate the rooms, and each space takes the type of its title. A space without a title (a corridor) gets no device.

**Scale:** `--area` (the floor area inside the outer walls, m²) is optional. Without it, the program reads the room area labels written on the plan ("17.0 m²", "17,0 m2", "183 sq ft") with OCR; each room gives pixels per metre = √(room pixels ÷ labelled m²), misreads are dropped (the rooms must agree within 15%), and the median is used. If neither an area nor readable area labels exist, it stops and asks for the area. The JSON's `image.scale_source` says which was used.

The legend and titles modes need **Tesseract OCR** (installed in `C:\Program Files\Tesseract-OCR`) and `pip install pytesseract`; they take 10–30 s per image. Samples: `sample_images	itles_976.png` (area 91.51 m²) and `sample_images\legend_2814.png` (111.02 m²).

## Web app: watch the solver run

```
python web_app.py
```

This opens **Cable Ring Studio** at http://127.0.0.1:8765 in your browser. Press Ctrl+C in the cmd window to stop it. It needs no extra packages (Python standard library only), and the dataset takes 15–20 s to load when it starts.

- **Plan viewer:** pick any of the 17,000 plans by split, result or id. The input floor plan (rooms, walls, doors, windows) is on the left; the output (zones, hubs, devices, rings, visiting order) is on the right.
- **Run solver:** streams every step live: devices, the routing grid, door links, zones and hub points, each ring leg as it is routed (dashed while a visiting order is being tried), the clean-up step and the final check. Stage timings, the live J, a hubs table and a running log appear under the panels.
- **Speed:** Instant, Watch or Slow. The slow speeds only add pauses, so the result is the same.
- **Layers:** room labels, grid lines (with the extra line through each device), door links used for rule 3, visiting order. Scroll to zoom and drag to pan; both panels move together.
- **Save** writes a live result to `resplan_output` (JSON + PNG). **JSON** downloads it.
- **Open a floor-plan image** (sidebar): choose a colour-coded PNG/JPG, enter its floor area in m², and it opens like any plan; Run solver and Save work the same (saved to `image_output`).
- **Batch run:** the same runs as `run_resplan.py` (first N, a split, random N, chosen ids or all), with live progress, counters, a hubs histogram, J per plan, and a results table you can click to open any plan. Reloading the page reattaches to a running batch. Stop cancels the plans in progress.

Options: `--port 9000`, `--no-browser`, `--out DIR`.

## Tests and benchmarks

```
python -m pytest tests -q                                   regression tests (about 40 s)
python bench_solver.py --set val100 --tag mytest            100 fixed test-split plans
python bench_solver.py --set dev20 --tag cmp --variants base "fast:cfg.grid_m=0.2"
python bench_solver.py --summary bench_results\cmp.jsonl    print a saved comparison again
```

`bench_solver.py` solves each plan with every variant in the same worker process, so runtimes are compared fairly. Results go to `bench_results\`. The plan sets (`dev20`, `val100`, `heldout200`, `hard15`) are fixed in `bench_sets.json`. `OPTIMIZATION_REPORT.md` has the measured effect of every solver change.

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
- No crossings in any plan. Median J is 43.5, on average 2.0 above the lower bound.
- About 0.8 s per plan on 14 worker processes (21 min for the split, about 3.6 h for all 17,000 plans).

## Files

| File | Purpose |
|---|---|
| `run_resplan.py` | Batch runner (the one to run): selects plans, runs them in parallel, writes JSON, PNG and summary.csv |
| `bench_solver.py`, `bench_sets.json`, `tests/` | Paired solver benchmark on fixed plan sets, and regression tests |
| `web_app.py`, `web/` | Cable Ring Studio: local web app to browse plans, watch the solver live and run batches |
| `image_input.py`, `sample_images/` | Floor-plan image (colour-coded PNG/JPG) -> plan, then the same pipeline; two sample images |
| `resplan_solver.py` | Per plan: units to metres, device placement, door/opening links, output JSON, independent checker, overlay |
| `hub_routing.py` | The algorithm: grid, stages 1–4, scoring (J) and rule checks |
| `run_example.py`, `example_plan.py` | The organisers' 14 m × 9 m example plan |
| `compare_zone_rules.py` | One plan drawn under the strict and the lenient reading of rule 3 |
| `resplan_utils.py` | ResPlan authors' helper (MIT); used by the viewer scripts, not by the pipeline |
| `helper_function.py`, `graph_view.py`, `adjacency_view.py`, `base_floorplan.py` | Plan viewers |
| `CLAUDE.md` | Detailed notes: rules, decisions, data facts, measurements |
| `Cable Ring Challenge - Problem Statement4.pptx` | Organisers' problem statement |

ResPlan data: CC BY 4.0 (Kaggle `resplan/resplan`, GitHub `m-agour/ResPlan`).
