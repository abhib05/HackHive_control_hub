# HANDOFF: Cable Ring Challenge solver (team HackHive)

Written 2026-10-16. Read this first, then `CONVERSATION_LOG.md` (what was asked, found and decided, in order), `CLAUDE.md` (detailed rules, decisions, data facts) and `README.md` (user-facing guide).

## 1. What the project is

Honeywell UR Hackathon 2026H2, **Cable Ring Challenge**. For every ResPlan floor plan (17,000 plans), the pipeline:

1. adds one device per living room, bedroom, kitchen and storage room,
2. splits the rooms into zones with as few hubs as possible,
3. places each hub,
4. routes one closed axis-aligned cable ring per hub,
5. writes one JSON per plan (organisers' slide-8 format) plus an overlay PNG.

Ranking is fewest hubs first, then lowest **J = L + 5X + O + 0.5B**. The whole pipeline is plain Python with no manual steps and no AI at run time (a user requirement).

- Authoritative brief: `Cable Ring Challenge - Problem Statement4.pptx`.
- Repo: https://github.com/abhib05/HackHive_control_hub (private). The author pushes. **Never add a Claude co-author trailer to commits.**

## 2. Current state (2026-10-17)

### Git

- `main` holds 5 commits, all pushed to GitHub:
  - `5620283`: the solver
  - `7cc1df3`: an overlay PNG for every plan by default
  - `519e344`: the rip-up and reroute step
  - `e33ec8a`: the web app, live solver events, greedy hub relocation
  - `03f1780`: handoff notes, conversation log and the intermediate-update deck
- **Branch `solver-optimization`: committed as `3bd28f1` and pushed to `origin/solver-optimization` on 2026-10-17** and `main` fast-forwarded to it and pushed (both equal on GitHub). Everything listed below is in that commit. The optimisation work, see `OPTIMIZATION_REPORT.md` and CONVERSATION_LOG entry 22.
  - Modified: `hub_routing.py` (guided exact searches, sparse penalties, pruned hub scan, faster grid build, soft corner lines that never move device lines, stage timings/counters, optional ring/segment/kick moves off by default, hard deadline), `resplan_solver.py` (corner lines on by default, `hard_limit_s`, timings in the JSON), `run_example.py` (full hub map for its figure), `run_resplan.py` (`--grid`, `--no-corner-lines`), `web_app.py` (slow speeds lift the hard limit), `web/index.html` (batch results table: number columns right-aligned), `README.md`, `CLAUDE.md`, `.gitignore` (`bench_results/`). `example_final_cables.png` and `example_solution.json` were regenerated (same J 53.68).
  - New: `bench_solver.py`, `bench_sets.json`, `tests/test_solver.py` (17 tests now, all pass), `OPTIMIZATION_REPORT.md` (results still being written).
  - Image input (log entry 44, done and tested): new `image_input.py` (colour-coded floor-plan image -> ResPlan-style plan, then the normal pipeline; cv2 + rasterio), `sample_images/` (5 PNGs: colour, legend, room-title drawings, one with area labels), `run_resplan.py --image/--area/--palette/--image-mode`, OCR via Tesseract (entry 45), floor area optional: scale from room area labels when not given (entry 48), web app upload (`web_app.py`, `web/app.js`, `web/index.html`, `web/style.css`), output in `image_output/` (git-ignored). Tests: 17, all pass.
  - Also untracked: `Cable Ring Challenge - Intermediate Update Template.pptx` (the organisers' blank template, copied into the folder at 22:03; our filled deck is the `- HackHive.pptx` one).
- The table below lists what went into `e33ec8a` / `03f1780` (all committed now):

| File | State | What changed |
|---|---|---|
| `hub_routing.py` | modified | Optional `emit` callbacks in `sequential_loop`, `best_order_loop`, `route_rings`, `improve_rings` and `design`. Default `None` leaves results unchanged; verified with the same J with and without `emit`. Also: greedy hub relocation as a third rip-up move (`HUB_MOVES = True`, `_hub_moves`, `_hub_legs_cost`, `_hub_legs_bound`) and an optional greedy stage-2 hub search (`HUB_SEARCH`, `_greedy_hub`, default `"exhaustive"`). Measured on 100 test plans; see `CLAUDE.md`. The test-split results in `resplan_output/` were made before these two changes. |
| `resplan_solver.py` | modified | `plan_geometry()` (drawing data in metres for the web app), `_live_emitter()` (grid vertex ids converted to coordinates and node labels), and `solve(inp, cfg, emit=None)` with live notes. |
| `web_app.py` | new | Cable Ring Studio, the local web server: stdlib `ThreadingHTTPServer` with server-sent events. |
| `web/index.html`, `web/style.css`, `web/app.js` | new | The page: plain JS + SVG, no CDN, light and dark themes. |
| `README.md`, `CLAUDE.md` | modified | Web app section; room-graph findings. |
| `Cable Ring Challenge - Intermediate Update - HackHive.pptx` | new | The filled 3-slide intermediate update. **The team-members field still needs names**, and the team name "HackHive" was inferred from the repo name. |
| `HANDOFF.md` | new | This file. |

### Results (test split, 1,632 plans; `resplan_output/`, about 1,643 JSON + PNG)

These figures are from the **old (committed) solver**. About 20 plans have since been re-solved with the new code (the first 10 plans and the random-10 seed-0 sample, from the CLI and the web app's batch tab), so `resplan_output` is mixed until the test split is re-run with `--force`.

**Rule 2 change (2026-10-16, user decision, uncommitted):** a room over 100 m² now counts 100 m² towards its hub (`--full-room-area` = old rule), and the hub lower bound is capped at the number of rooms (bug that made 3 plans "failed"). The 51 invalid/failed plans were re-run: 47 are now valid; left: PLAN_013607 (rooms outside the outline), PLAN_003931 (rule 4), PLAN_011279 and PLAN_016612 (rule 6). Benchmark plans are unaffected (no room over 100 m²). Also untracked: `Cable Ring Challenge - Intermediate Update Template.pptx` (organisers' blank template, copied from Downloads). New untracked `image_input.py` and `image_output/` (from another session; not reviewed here). About 2,164 plans now have results (more batches run from the web app): 77 not valid, none in the benchmark; 78 of 466 augmented plans are invalid because their outline was not rotated with the rooms (log entry 41).

- 1,586 valid, 43 invalid, 3 failed. **All 1,516 plans inside the organisers' benchmark filters are valid.**
- The 46 non-valid plans are all outside the benchmark: 42 have a room over 100 m² (rule 2 impossible), and 4 have a sliver room or an unreachable device.
- Hubs per plan: 1 in 888 plans, 2 in 660, 3 in 69, 4 in 9, 5 in 3. The hub count is provably the fewest possible, because stage 1 is an exact search.
- 0 crossings in every plan. Overlap averages 0.17 m.
- J median 43.5, mean 45.6. J − lower bound averages 1.97; most of this is inherent, see section 5.
- The independent polyline checker agrees with the solver on every plan.
- Runtime per plan on 1 core: median 6.5 s, p90 21 s, max 122 s (PLAN_014527). The 90 s soft cap was hit once. All 17,000 plans take about 3 h on 14 cores.
- The rip-up step (commit `519e344`) lowered J in 1,385 of 1,632 plans (mean −0.29, max −2.2) and raised it in none.

## 3. Architecture

| File | Role |
|---|---|
| `hub_routing.py` | The algorithm: `Plan`, `Grid`, stages 1–4, `measure()` (exact J), `validate()` (rules on the grid), plot helpers |
| `resplan_solver.py` | Per plan: ResPlan → metres, device placement, `room_links` (rule-3 door/opening graph), `solve()`, output JSON, independent `check_output()`, `draw_overlay()` |
| `run_resplan.py` | Parallel batch runner (spawn Pool), resume, `summary.csv`, report |
| `web_app.py` + `web/` | Cable Ring Studio: plan viewer with a live solve stream, plus a batch tab |
| `run_example.py`, `example_plan.py` | The organisers' slide-5 example (J 53.68, valid) |
| `compare_zone_rules.py` | One plan under the strict and lenient rule 3 |
| `helper_function.py`, `graph_view.py`, `adjacency_view.py`, `base_floorplan.py`, `resplan_utils.py` | Viewers and the ResPlan authors' helper. **Not used by the pipeline**; see 4.6. |

### Solver stages (`hub_routing.design`)

1. **Zones (stage 1).** Feasible blocks (≤ 5 devices, ≤ 100 m², connected per `zone_mode`), then an exact-cover search over partitions into k = max(⌈area/100⌉, ⌈n/5⌉), then k+1, and so on. The first k with a routable design wins.
2. **Hub points (stage 2).** Direction-aware Dijkstra tables from every device (state = vertex × heading, 0.5 per turn), then a min-plus DP over cyclic visiting orders. This gives the exact L + 0.5B ring cost at every legal hub vertex. The cheapest vertex wins (ties go to the vertex nearest the device centroid), and its cost is a lower bound on J.
3. **Rings (stage 3).** Each ring is routed leg by leg for every visiting order, cheapest bound first, with branch and bound.
   - Each leg is a Dijkstra whose arc costs equal the exact J increase: length, 0.5 per bend, 1/m on a used edge, and 5 for a new crossing. Crossings are detected by pass types at a vertex; complementary pass types mean the wires share no edge.
   - Other hubs and nodes are blocked.
   - A super-source state allows a reversal at a node (1 bend).
   - An exact cost-to-go per arrival direction is added.
   - Searches are bounded to 1.5 × the unhindered cost + 12, with an unbounded rerun when needed.
   - Groupings are routed in lower-bound order and stop once the bound exceeds the best J. Soft caps: 90 s and 40 routed groupings per plan.
4. **Clean-up (stage 3b, `improve_rings`).** Rip-up and reroute on the best 3 designs:
   - Re-route each leg against all other wires.
   - Re-route each node's two legs together, in reverse order.
   - Keep a change only if J drops. At most 5 rounds and 30 s.
5. **Check (stage 4).** `measure()` computes the exact J. Pick the lowest J, then the smallest area gap. `validate()` checks the rules on the grid, and `check_output()` recomputes everything from the JSON polylines.

### Web app data flow

The page (`web/app.js`) uses `EventSource` on `/api/solve?id&zone&speed`. The server runs:

1. `from_resplan`
2. `plan_geometry` (event `plan`)
3. device events
4. `solve(..., emit=Live)`

`solve` notes the `grid`, `links`, `benchmark` and `stage` events. `_live_emitter` converts `hub_routing` events (`blocks`, `groupings`, `grouping`, `leg`, `attempt`, `ring`, `routed`, `improving`, `improve`, `improved`, `chosen`, `log`) into coordinates, then the server sends `result`.

Other behaviour:
- **Speeds:** Instant thins leg events to about 25 per second; Watch and Slow add pauses and lift the 90 s budget, so results are identical.
- **Batch tab:** `/api/batch/start` runs `run_resplan.work` in a spawn Pool from a background thread. Its events are kept, so a reloaded page replays them through `/api/batch/events?from=0`. `/api/batch/stop` terminates the pool, cancelling the plans in progress.
- **Save:** `/api/save` writes the last live result (JSON + PNG) into `resplan_output`.

## 4. Decisions (user-confirmed unless marked)

1. **Devices.** One per living room, bedroom, kitchen and storage room; none in bathrooms, balconies or stairs.
   - Placement: the room centroid if it is ≥ 0.5 m from walls and windows, otherwise the nearest such point, kept inside `inner`, ≥ 0.3 m from other devices, rounded to 1 cm.
   - Each polygon counts as a room, so a two-polygon living room gets two devices.
   - Devices given in an input JSON are used unchanged.
2. **Rule 3 is lenient** (`zone_mode="any"`): the door/opening chain may pass through any room. `--zone own` (strict) and `--zone excluded` remain as options.
3. **A hub may sit right next to its only device**, so a single-device ring is a few cm out and back.
4. **Retracing is allowed** and counted as overlap; it is chosen only when cheaper than parallel lines. The user never decided whether to forbid it; that question is still open.
5. **Measurement (deck slides 6–7):**
   - A reversal counts as 1 bend.
   - X is counted once per point, touches included, but never at nodes or hubs.
   - O = (k − 1) × length.
   - Tolerance 1 mm.
6. **Room links** come from our own `room_links` port of `plan_to_graph`:
   - Windows never count, and wall adjacency (`add_adjacency_edges`) is not used.
   - Checked on the test split: `plan_to_graph` relabels interior windows as doors (83 cases) and adds `fallback` edges, so it is wrong for rule 3.
   - Splits in the door graph occur only in non-benchmark plans, and the 6 benchmark plans above the simple hub bound are capacity-bound.
   - The viewer scripts therefore stay viewers.
7. **Units and grid:**
   - Scale = √(inner.area / area), in units per metre; never `net_area`.
   - Grid lines every 0.1 m, plus a line through every device x and y.
   - Extra lines within 1.8 mm merge; regular lines within 5 mm of an extra line are dropped.
   - Wires keep 1 cm from walls. Windows are obstacles.
8. **Oversize rooms (> 100 m²)** get their own hub and are flagged invalid (`oversize_ok`). A hub clearance is relaxed 0.5 → 0.3 → 0.1 → 0 only when nothing else routes; that case is flagged as a rule 6 break.
9. **Process.** The user runs the long batches; give them the cmd command. The user pushes to GitHub.

## 5. Known gaps

- **J − lower bound ≈ 2.0** on average. Most of this is inherent: entering and leaving a one-door room on parallel lines costs about 1 extra bend that the bound ignores. The rest comes from sequential routing.
- **Runtime tail.** Plans with many groupings take 20–122 s.
- **Unknown input format** for the hidden plans; the deck doesn't show it. `load_input_json` reads our own format plus ResPlan-style dicts (GeoJSON, WKT, coordinate lists).
- **The pickle loads slowly** in `web_app.py` (15–20 s, OneDrive cold cache); the CLI loads it in 3–14 s.
- **The project runs in the global Python**, where pip conflicts exist (numpy 2 vs tensorflow; pandas 3 vs streamlit). TODO: move it to a `.venv`.

## 6. Exact next steps, in order

00. **Chatbot** (log entry 52): offline rule-based version recommended (no API key), about 3-3.5 h; waiting for the user's go.
0. **For the judges' presentation (next day):** re-run the test split with the current code (`python run_resplan.py --split test --force`, about 15 min) and take the metrics from its summary (log entry 39). Restart the web app afterwards, since it only loads results at start-up.
0a. **No retracing (log entry 50, uncommitted):** `hub_routing.NO_RETRACE = True`: a ring never uses a line twice (one-device rings are small rectangles); val100: all valid, same hubs, J +0.48. Stored results in `resplan_output` predate it: re-run the test split (`python run_resplan.py --split test --force`).
0b. **Image input (new, uncommitted):** `image_input.py`, `run_resplan.py --image ... --area ...`, web app upload; output in `image_output\`. OCR for room titles and legends added (log entry 45; Tesseract installed on this laptop; another computer needs it installed too).
0c. **Augmented-outline repair** (log entry 43): asked for, then stopped before coding; build it, add an outline check to the benchmark flag, re-run the augmented split.
1. **Finish the optimisation** (branch `solver-optimization`): the re-run of the final paired benchmark is complete (`bench_results\final2_*.jsonl`; print with `python bench_solver.py --summary <file>`; `final_*` is the first run, before the device-line fix): fill in `OPTIMIZATION_REPORT.md`, then the user decides on the default grid and whether to commit / merge (no Claude trailer). Tests: `python -m pytest tests -q`. Afterwards re-run the test split (`python run_resplan.py --split test --force`) so `resplan_output` matches the new solver (it is now mixed old/new). The new code was spot-checked on the first 10 plans and in the web app (log entry 26): all fine.
2. **User:** fill in the team members (and the team name, if it isn't HackHive) on slide 1 of the intermediate-update deck.
3. **User:** run all 17,000 plans (about 3 h on 14 cores), then check that every benchmark plan is valid:
   ```
   python run_resplan.py --all
   ```
   Re-run the test split with `--force` too, so PLAN_014433 and the summary are current.
4. **Ask the organisers:**
   - a sample input JSON for the hidden plans,
   - any time limit per plan,
   - confirmation of our readings: lenient rule 3, X once per point including touches, a hub next to its only device.
   When the sample arrives, write an adapter in `resplan_solver.load_input_json`.
5. **Stage-by-stage algorithm review with the user**: stage 1 kept (exact depth-first search); stage 2 kept exhaustive (greedy was worse in 28 of 100 plans). **User to decide:** keep greedy hub relocation on (J lower in 3 of 100 plans, mean −0.013, never higher, small runtime cost) or set `HUB_MOVES = False`. Stage 3 was covered by the optimisation work (entry 22 of the log).
6. **Close the J gap:** the gap is in the device-to-device legs, not the hub point (hub legs carry only about 0.5 of it per ring). Already tried and left off: whole-ring re-routing in other orders, 3-leg segment moves, kick moves, wider rip-up (at most −0.008 J). The grid (corner lines) was the lever that worked.
   - A tighter lower bound that charges one-door rooms their extra bend.
   - Measure each change on the test split; it must never raise J or change status.
7. **Cut the runtime tail:**
   - Reuse `prepare()` tables across clearance retries; they are already shared across groupings.
   - Prune groupings earlier.
   - Profile PLAN_014527.
8. **Package for the demo:** a one-command run, the web app, README results, and the final deck.

## 7. Handy commands (cmd, from the project folder)

```
python run_resplan.py --ids 14433             one plan: JSON + PNG
python run_resplan.py --split test            1,632 test plans (resumes; --force redoes)
python run_resplan.py --all                   all 17,000 plans
python run_example.py                         organisers' example plan (expect J 53.68, valid)
python compare_zone_rules.py 14877            strict vs lenient rule 3, side by side
python web_app.py                             Cable Ring Studio at http://127.0.0.1:8765
```

Dataset (not in git; files over 100 MB):

```
curl -L -o resplan.zip https://www.kaggle.com/api/v1/datasets/download/resplan/resplan
tar -xf resplan.zip
```
