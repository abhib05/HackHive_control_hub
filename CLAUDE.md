# CLAUDE.md — Honeywell Hackathon (ResPlan floor plans → cable routing)

## Project goal
Joint optimisation of control-panel (hub) placement and closed-ring cable routing on ResPlan floor plans.
Authoritative brief: `Cable Ring Challenge - Problem Statement4.pptx` (organisers' deck, 8 slides; read 2026-10-16).
`Honeywell_Hackathon_Problem_Summary.docx` is older; where anything conflicts, the deck and the user decisions below win.

## Hard rules (deck slide 3; a design is invalid if any is broken)
1. **Coverage:** every eligible room (living, bedroom, kitchen, storage) and its node assigned to exactly one hub.
2. **Capacity:** per hub ≤ **5 nodes** and ≤ **100 m² of eligible-room area** (only rooms with a device count). **User decision 2026-10-16:** a hub may serve part of a room; a room > 100 m² counts at most 100 m² towards its hub (`CONFIG["room_area_cap_m2"] = 100.0`; `--full-room-area` = deck wording, full room area, such a plan is invalid). Only plans with a room > 100 m² change, and those are outside the organisers' benchmark (their checker may count the full room). The hub lower bound is also capped at the number of eligible rooms (a room cannot be split), which fixed 3 "failed" plans (e.g. PLAN_007394: area bound 6 hubs > 5 rooms, so no grouping was tried).
3. **Zone:** a hub's rooms are connected through doors or open passages only (windows, wall-only adjacency and the entrance do not count).
4. **Ring:** one closed ring per hub: H → every assigned node once → same H, no branches, never ending at another hub. **User decision 2026-10-17: a ring never runs back along its own wire** (`hub_routing.NO_RETRACE = True`): no grid edge is used twice by one ring, so a one-device ring is a small rectangle (e.g. PLAN_014433 H2: 4 × 5 cm, 0.18 m, 4 bends) and a one-door room is entered and left on two different lines through the door. Enforced in routing (a ring's own edges are blocked for its other legs, in stage 3 and in every rip-up move), checked in `validate()` and in `check_output()` from the polylines (`ring_self_overlap`). Overlap between different rings stays allowed and costs 1/m. (Before: retracing was allowed and counted as overlap.) Measured on val100 (paired, bench_results
oretrace.jsonl): all 100 valid, same hub counts, 0 crossings; mean J 44.82 -> 45.30 (+0.48: rings now avoid their own lines with extra bends, B 18.4 -> 19.6, O 0.19 -> 0.00), worse in 71 plans (max +2.9), runtime +14%. Measured on val100 (paired, bench_results
oretrace.jsonl): all 100 valid, same hub counts, 0 crossings; mean J 44.82 -> 45.30 (+0.48: rings now avoid their own lines with extra bends, B 18.4 -> 19.6, O 0.19 -> 0.00), worse in 71 plans (max +2.9), runtime +14%. We also never let a wire run through another hub or node point.
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
- Our grid: a line every 0.1 m plus one line through every device x and y, so node points are exact grid vertices (deck: nodes cannot be moved), plus (since 2026-10-16, `CONFIG["corner_lines_m"] = (0.012,)`) lines 1.2 cm either side of every wall / window / outline corner coordinate, so wires can hug corners and door jambs. Hubs sit on grid vertices.

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

## Image input (added 2026-10-16, user request: PNG in, scale with the deck formula, 0.1 m grid, JSON + image out)
- `image_input.plan_from_image(path, area_m2, palette=None)`: each pixel -> nearest colour of a palette (ResPlan `plot_plan`, Studio light, Studio dark, or a user JSON `{"#hex": type}`; the palette matching most pixels wins; anti-aliased pixels take the nearest classified pixel's class). Largest connected drawn part = the plan (legend, title dropped). Rooms = connected patches of one room colour (`rasterio.features.shapes`, y up); room specks < 0.5 m² or mostly outside the outline are dropped as image noise. Wall depth = 2 × wall area / wall perimeter. Outline (`inner`) = filled plan without balconies minus a wall-depth band at its outer edge. Scale = sqrt(inner px² / area m²) (deck slide 4); geometry is rescaled so the outline's longer side is 256 units, like ResPlan. The plan dict then goes through `from_resplan` -> `solve` unchanged.
- CLI: `run_resplan.py --image FILE... --area M2...` (one area, or one per image; `--palette FILE`), output `image_output\` (`IMG_<name>.json/.png`); JSON gains an `image` field (palette, pixels matched, px/m, wall depth, rooms found). Web app: sidebar "Open a floor-plan image" (POST `/api/image?name=&area=`, plan id `img-<name>`, upload kept in `image_output\uploads`), Run solver / Save as for any plan (Save -> `image_output`). Uploaded images are kept in memory only until the app restarts.
- Measured: 30 random ResPlan plans drawn with `plot_plan` (legend, title, black room edges) and read back: 30/30 valid, same hub count and device count as the vector plan, checker agrees, J − J(vector) mean +0.21, within 1 in 29/30 (max 1.17). Test: `test_image_input_matches_vector_plan`.
- **OCR (added 2026-10-17, user request):** Tesseract 5.4 (UB Mannheim build, installed with winget to `C:\Program Files\Tesseract-OCR`; `TESSERACT_CMD` overrides) + `pytesseract` 0.3.13 (pip). `plan_from_image(..., mode="auto"|"colour"|"legend"|"titles")`, auto = colour if a known palette explains >= 85% of pixels with room colours covering >= 3% of the image (a black-and-white drawing does not count), else legend, else titles.
  - Legend: OCR phrases (words on one line joined, e.g. "front door") naming a type (`KEYWORDS`: bath/toilet/wc -> bathroom, store/closet/utility -> storage, dining/hall/lounge -> living, master/guest/study -> bedroom, ...) take the colour of the swatch just left of them; used as a palette when it has walls and a room type.
  - Titles: walls = dark (< 110 grey) thick lines (3x3 opening drops thin lines; confident OCR text boxes erased first, not low-confidence misreads, which once erased a wall corner). Outline = gaps <= 3 m in the outer walls closed (thin window outlines count), filled, minus a wall-depth band (a round 2 m closing only if the ring is still open). Inner door gaps <= 1.4 m closed along a wall run (one end must touch a wall running the same way, so a corridor between parallel walls stays open) -> door. Each enclosed space takes its title's type; spaces >= 1 m2 without a title are OCR-read again on their own; a space with titles of different types (open plan) is split by growing each title through the space (walking distance); a space with no title gets no device (warning).
  - Measured on title / legend drawings of PLAN_014433, 976, 2814: same room types and device counts as the vector plan, room areas within ~3% (smaller: ResPlan room polygons overlap walls), titles mode puts the kitchen / living split of open plans where the titles are, not where ResPlan drew it. Hub counts equal except two plans whose eligible area sits at the 100 m2 limit (103.6 -> 99.8 m2 from the image: 2 -> 1 hub). OCR modes take 10-25 s per image. Samples: `sample_images	itles_976.png`, `legend_2814.png`; test `test_image_input_titles_and_legend` (skipped without Tesseract). 15 tests pass.
- **Area optional (2026-10-17, user choice: only "area given" or "room area labels", no guessing from wall thickness / doors):** `plan_from_image(path, area_m2=None)` reads area labels (`AREA_RE`: number + m²/m2/sq m/sqm, or sq ft x 0.0929; OCR reads "²" as "?"; "3.6 m" is a length and ignored; a number without its decimal point, e.g. "371", also tries 37.1 and 3.71). Each label belongs to the room patch it is written in (two labels in one patch: skipped); px/m = sqrt(room pixels / m2); readings within 15% of the median are kept; their median is the scale; floor area = outline px / scale^2. Titles mode reads rooms once with a provisional door-gap size (wall thickness / 0.23 m) only to find the labelled rooms, then again at the label scale. Small images are enlarged 3x for OCR (< 1500 px). Checked: the user's screenshot of PLAN_012247 (3 labels agree): rooms within ~2% of the real plan (bedrooms 19.5/21.1 vs 19.2/20.8, kitchen 12.1 vs 12.1); `sample_images	itles_area_976.png` (5 labels): 1 hub, J 34.88 vs 35.37 from the vector plan. CLI `--area` optional; web app area field optional (placeholder says so). 17 tests pass.
- Not done: black-line drawings without titles; images with neither an area nor area labels need the area typed in.

## Solver (`hub_routing.py`; ResPlan pipeline `resplan_solver.py` + `run_resplan.py`; example `example_plan.py` + `run_example.py`)
- Stage 1: feasible blocks (≤ 5 nodes, ≤ 100 m², connected per zone_mode), then exact-cover search for partitions into k = lower bound, k+1, … hubs; the first k with a routable design wins (fewest hubs).
- Stage 2: per group, exact ring cost L + 0.5B at every legal hub vertex (direction-aware Dijkstra tables per node, turns at hub/nodes included, best visiting order), ignoring overlaps/crossings → **lower bound** on J. Hub = cheapest vertex, ties to the node centroid.
- Stage 3 (`route_rings`, method 3 only): per hub, route the ring leg by leg for every visiting order; each leg pays the **exact J increase** it causes against the wires already routed: overlap 1/m on used edges, and 5 for passing a point in a way that crosses or touches a wire already there (pass types: which 2 of the 4 edges a wire uses at a point; complementary types = no shared edge), except at hubs/nodes or points that are already crossings. Other hubs/nodes are blocked. Each leg also weighs the exact rest-of-ring cost per arrival direction; a reversal at a node is allowed (1 bend). Leg searches are bounded by 1.5 × unhindered cost + 12 and re-run unbounded if the end is not clearly inside (exact). Orders are tried cheapest lower bound first and stop when none can beat the best J; groupings likewise (sorted by lower bound, stop when the bound exceeds the best J). Soft cap: 90 s / 40 routed groupings per plan.
- (Before 2026-10-16 the crossing penalty was a flat 5 for entering any used point, which also punished harmless merges; switching to the exact penalty removed all crossings and cut mean J by 1.3 on the 500-plan sample.)
- Stage 3b (`improve_rings`, added 2026-10-16): rip-up and reroute of the best 3 routed designs (within 2.5 of the best J). Stage 3 routes legs one after another, so early legs never see later wires; this pass re-routes each leg against all other wires (turns at both ends charged), then each node's two legs together in reverse order (outgoing first), repeating until a round changes nothing (max 5 rounds, 30 s for the best design; runner-ups only before the plan's deadline). A change is kept only if the exact J drops, so J never increases. On 200 test plans: J lower by 0.33 on average (max 1.9; 164 of 200 improved, none worse), no hub/status changes, runtime 7.0 → 9.3 s per plan.
- Greedy hub relocation (added 2026-10-16, user request; `HUB_MOVES`, third move in `improve_rings`): per hub, the legal hub points of its zone are ranked by the exact unhindered cost of the two hub legs (`_hub_legs_bound`, from the stage-2 tables); points whose bound cannot beat the current hub legs' exact share of J (`_hub_legs_cost`) are skipped; the best 12 (≥ 0.25 m apart) are tried by re-routing both hub legs against all other wires; the first that lowers J is taken and the search restarts there. Measured on 100 random benchmark test plans: J lower in 3 (0.95, 0.2, 0.2; mean 0.013), never higher, all valid, checker agrees; total runtime +0–20% (noisy). 60 tries found nothing more at 2.2× the time. Diagnosis: the stage-2 hub is already within 0.016 of the best unhindered hub-leg cost anywhere, and interference on the hub legs is only ~0.5 per ring, so the J − lower-bound gap sits mostly in the middle legs, not in the hub point.
- Greedy stage-2 hub search (`HUB_SEARCH = "greedy"`, `_greedy_hub`; default stays `"exhaustive"`): hill climbing from the legal vertex nearest the node centroid over the 4 grid neighbours. Same 100 plans, hub moves off: hub differs in 67 plans, J higher in 28 (worst +1.2), lower in 3, mean J 45.19 → 45.38; stage-2 time only −16% (the per-node tables dominate, not the candidate scan). With greedy hub moves on top, mean J 45.21 (still worse in 9 plans). Exhaustive stays the default.
- Stage 4: `measure()` exact J; pick lowest J, then smallest area gap. `validate()` checks rules 1–6 on the grid, and `resplan_solver.check_output()` re-checks the rules and recomputes L, X, O, B from the JSON polylines alone (must agree).
- **Optimisation 2026-10-16 (user's master prompt; branch `solver-optimization`; full numbers in `OPTIMIZATION_REPORT.md`):**
  - Guided searches (`GUIDED`, `Grid.potential`, `_route_leg`): every leg search uses the device's `first_move_dist` table as an A* potential (reduced arc weights w + h(head) − h(tail) ≥ 0 inside scipy's Dijkstra, cost limit = unhindered leg cost + `ROUTE_SLACK` 2.0, then 1.5× + 12, then none: exact). Legs ending at a hub (no table) are searched backwards from the device and reversed (penalties are direction-symmetric; end turns swap roles). Search start is a super-source with 4 arcs (start costs per arrival direction).
  - Sparse penalties (`_penalised_graph(..., scratch=True)`): cached base weights per target plus penalties only on arcs at used edges / blocked points / wired points, written into one reusable CSR graph. With guiding: 2.7× faster on dev20, J equal or lower (ties).
  - Exact pruning of the stage-2 hub scan (`HUB_PRUNE`): a vertex is fully evaluated only if cheapest(node s → c) + chain(s..t) + cheapest(t → c) can reach the best ring; same hub and J (tested). Grid build: room tests only inside each room's bounding box, wall distances only inside the outline (2× faster, identical).
  - Corner lines (`CONFIG["corner_lines_m"] = (0.012,)`, soft grid lines via `Grid(soft_x, soft_y)`): never within 5 mm of a device line (a merge moved device lines by ≤ 1 mm and flagged 13 of 100 plans as outside the benchmark; fixed, regression test). The single biggest J gain: about −0.37 on 100 and 200 plans, −0.73 on the 15 hardest. Coarser 0.15/0.2 m grids alone are worse (+0.2/+0.4); 0.05 m grid −0.23 at 4.6× time; corner lines on a 0.2 m grid (`fast`) −0.33 at 0.3× the original runtime.
  - Tried and left off (flags, default False): `RING_MOVES` (re-route a whole ring in another order + polish: −0.008 at 1.8× time), `SEG_MOVES` (3 legs, middle first: 0), `KICK_MOVES` (push a leg off its line + polish: −0.003 at 2.6×), wider rip-up (`IMPROVE_TOP` 6: 0). Gap analysis (dev20): J − bound 2.26 = 0.07 order/hub + 1.83 extra bends in legs that avoid each other (mostly entering and leaving one-door rooms) + 0.36 direction choices.
  - `hard_limit_s` (150 s): rip-up stops by then (the web app's slow speeds lift it). Stage timings and counters go into the JSON `solver` field (`t_hubs`, `t_route`, `t_improve`, `searches`, `moves_kept`, ...).
- Example (exact node points, ~2 s): 2 hubs, {Bedroom 2, Kitchen} + {Living, Bedroom 1, Bedroom 3, Storage}, J = 53.7 on both geometries (lower bound 51.7; 53.68 after rip-up and reroute, from 53.70), valid.

## How to run (cmd)
```
cd /d "C:\Users\abhin\OneDrive\Desktop\Honeywell_hackathon"
python run_resplan.py --ids 14433             (one plan: JSON + overlay image)
python run_resplan.py --first 20              (quick look)
python run_resplan.py --split test            (1,632 test plans)
python run_resplan.py --all --no-png          (all 17,000 plans, JSON only)
python web_app.py                             (web app: browse plans, watch the solver live, batch runs)
python -m pytest tests -q                     (regression tests, ~60 s)
python run_resplan.py --image sample_images\plan_14433.png --area 120.77   (floor-plan image -> image_output)
python run_resplan.py --image sample_images	itles_976.png --area 91.51     (room names read with OCR; --image-mode auto|colour|legend|titles)
python bench_solver.py --set val100 --tag x --variants base "fast:cfg.grid_m=0.2"   (paired benchmark)
```
Web app (added 2026-10-16, user request "see the input and output floor plans and everything running in real time"): `python web_app.py` → Cable Ring Studio at http://127.0.0.1:8765 (stdlib `ThreadingHTTPServer` + server-sent events; page in `web/`: `index.html`, `style.css`, `app.js`, plain JS + SVG, no CDN). Plan viewer: input plan left, output right, live run streams every step (`emit` hooks: `resplan_solver.solve(..., emit)` → `_live_emitter` converts `hub_routing.design/route_rings/best_order_loop/sequential_loop/improve_rings(..., emit)` events from grid vertex ids to metres + node labels; `emit=None` everywhere else, so batch results are unchanged — checked: same J with and without emit). Speeds Instant/Watch/Slow only add pauses (time budget lifted when pausing). Save writes the live result to `resplan_output` (JSON + PNG). Batch tab runs `run_resplan.work` in a spawn Pool from a background thread, events kept so a reloaded page reattaches; Stop terminates the pool (in-progress plans are cancelled, re-runs pick them up). The pickle takes 15–20 s to load at start-up (cold OneDrive cache).
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
├── hub_routing.py       ← hub placement + ring routing solver (reusable; HUB_MOVES toggles greedy hub relocation)
├── example_plan.py      ← organisers' example plan traced from the slide image
├── run_example.py       ← runs the solver on the example, writes example_*.png + example_solution.json
├── resplan_solver.py    ← ResPlan plan -> devices, hubs, rings -> output JSON; independent polyline checker; overlay
├── image_input.py      ← colour-coded floor-plan image (PNG/JPG) -> ResPlan-style plan (then the same pipeline)
├── sample_images/      ← two plot_plan renderings to try image input with
├── image_output/       ← image runs: json/, png/, uploads/ (web app), summary.csv
├── run_resplan.py      ← parallel batch runner (pickle or input JSONs) -> resplan_output/json, summary.csv
├── bench_solver.py      ← paired benchmark of solver variants on fixed sets (bench_sets.json) -> bench_results/
├── tests/               ← pytest regression tests (python -m pytest tests -q)
├── OPTIMIZATION_REPORT.md ← measured effect of every solver change (2026-10-16)
├── compare_zone_rules.py ← one plan under strict vs lenient rule 3, side-by-side image
├── web_app.py           ← Cable Ring Studio web app (python web_app.py -> http://127.0.0.1:8765)
├── web/                 ← its page: index.html, style.css, app.js
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
- Checked 2026-10-16 on the test split (1,632 plans): `plan_to_graph` has 83 room-room "doors" that `room_links` lacks; they are interior windows it relabels as doors (e.g. kitchen walled off from living except a window), plus 45 `fallback` edges (no passage at all) and balcony edges. 12 corner-door links differ the other way; none changes connectivity. All 41 plans whose door graph splits the eligible rooms are outside the benchmark (devices unreachable through free space). The 6 benchmark plans above the hub lower bound are capacity-bound (no split into fewer zones fits 100 m² even with rule 3 ignored). Stage 1 routes only 1 grouping in 80% of benchmark plans (max 5). So the viewer scripts' graphs cannot lower hub counts or J; `helper_function.py`, `graph_view.py`, `adjacency_view.py` stay viewers only.

## Collaboration notes
- User prefers iterative, precise steps and direct, honest feedback
- User is running everything from cmd + Notepad; give exact commands and say where each one runs
