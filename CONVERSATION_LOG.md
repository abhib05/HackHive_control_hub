# Conversation log: Cable Ring Challenge (HackHive)

Running record of the working conversation with Claude: what was asked, what was decided and what was found. Newest entries go at the bottom of each section. For the project's state and next steps see `HANDOFF.md`; for rules, data facts and measurements see `CLAUDE.md`.

Last updated: 2026-10-16.

## Who and how

- User: Abhinav (team HackHive, repo `abhib05/HackHive_control_hub`). Works from cmd and Notepad on Windows, and wants exact commands. Prefers precise, honest, step-by-step answers; when asking "explain concisely", wants short answers.
- Commits are made by the user or on their go-ahead. **No Claude co-author line.** The user pushes.
- The user runs long batches (the full 17,000-plan run) themselves; give them the command.

## Timeline of requests and outcomes

1. **Read the deck and build the solver.** `Cable Ring Challenge - Problem Statement4.pptx` was read; the pipeline (`hub_routing.py`, `resplan_solver.py`, `run_resplan.py`) was built. It is pure Python, with one JSON and one PNG per plan.
2. **Devices.** One device per living room, bedroom, kitchen and storage room; none in bathrooms, balconies or stairs. The pipeline adds them.
3. **Rule 3.** The lenient reading is allowed: the door chain may pass through any room.
4. **Hub next to its only device.** Allowed and kept, so a single-device ring is a few cm out and back.
5. **Retracing a line.** Allowed and counted as overlap. Forbidding it was never decided (open question).
6. **GitHub.** `.gitignore` and README written, first commit made without the Claude trailer, pushed by the user. The user later committed "overlay image for every plan" (`7cc1df3`) and "rip-up and reroute" (`519e344`).
7. **Rip-up and reroute** (`improve_rings`): re-route each leg, and each device's two legs, against all other wires; keep a change only if J drops. On the test split, J fell in 1,385 of 1,632 plans (mean −0.29) and rose in none.
8. **Helper functions.** All functions in `run_resplan.py` and `resplan_solver.py` are used; `hub_routing.py` has 6 example-only helpers and one dead option (`penalise=False`). `resplan_utils.py` (the ResPlan authors' file) isn't used by the pipeline.
9. **Viewer scripts** (`helper_function.py`, `graph_view.py`, `adjacency_view.py`). Analysed whether `plan_to_graph` could improve zone grouping: **no**.
   - It relabels interior windows as doors and adds `fallback` links (wrong for rule 3).
   - The split door graphs occur only in non-benchmark plans.
   - The 6 benchmark plans above the simple hub bound are limited by the 100 m² capacity.
   - Our `room_links` is already the corrected version. Grid lines are built per plan (0.1 m + a line through each device).
10. **Web app** "Cable Ring Studio" (`web_app.py`, `web/`): input and output floor plans side by side, live solving (every leg), and batch runs. Tested in the browser; same J as the CLI. **Not committed yet**: the user wants to try it first.
11. **Intermediate-update deck** filled in on the organisers' template: `Cable Ring Challenge - Intermediate Update - HackHive.pptx`. The team members still need adding; "HackHive" as the team name was inferred from the repo name.
12. **HANDOFF.md** written: state, architecture, decisions, next steps.
13. **PLAN_013607 ("failed") explained:**
    - Bedrooms R04 and R05 lie **0% inside** the plan outline (`inner`); the dataset traced the outline without the top-left wing.
    - Each disconnected room may have its own hub (the solver tried 6 hubs), but no hub or cable is allowed outside the outline (rules 5 and 6), so no legal design exists.
    - The plan is outside the benchmark, so no score is lost.
    - A picture was drawn showing the outline vs the rooms.
    - Offered, not built: a flagged fallback that treats a room's own floor as usable when the outline wrongly excludes it.
14. **Outline and clearances.** The outline is given by the data as the inside face of the exterior walls (0 m). Hubs and devices stay ≥ 0.5 m from walls (deck); wires stay ≥ 1 cm (our choice).
15. **Dataset format.** `ResPlan.pkl` is a Python pickle: a list of 17,000 dicts. One plan has about 19 keys:
    - rooms: `living`, `bedroom`, `kitchen`, `bathroom`, plus `storage`, `stair`, `balcony` when present;
    - structure: `wall`, `door`, `window`, `front_door`, `inner`, `wall_depth`;
    - numbers: `id`, `area`, `net_area`;
    - unused extras: `graph`, `land`, `garden`, `parking`, `pool`, `neighbor`.

    Shapes are Shapely polygons in drawing units. JSON is only the organisers' (unseen) input format and our output format.
16. **JSON input works.** `python run_resplan.py --inputs DIR` reads one JSON file per plan, in our own format (written by `--write-inputs`) or ResPlan-style JSON (GeoJSON, WKT or coordinate lists). Round trip tested on PLAN_000800 and PLAN_008002: identical J, both valid. The web app reads the pickle only.
17. **This file** was created at the user's request ("periodically create and update .md files holding the context of this conversation"). It is refreshed on a schedule during the session (see below).

18. **Algorithm review, stage by stage** (user wants to discuss current algorithm and alternatives per stage).
    - Stage 1 (zones): exact depth-first backtracking with branch-and-bound pruning; hub count provably minimal. User chose to keep depth-first search. Discussed hill climbing (no guarantee), A* vs branch and bound (same optimum; A* less work, more memory).
    - Stage 2 (hub point): exact for one ring alone (length + bends), but final J is always ~2.0 above it (gap even with 1 hub), because the bound ignores a ring's interference with itself.
19. **Greedy hub relocation** built at the user's request (third move in rip-up and reroute, `HUB_MOVES`). On 100 test plans it lowered J in only 3 (mean −0.013), never raised it, all valid; small runtime cost. Diagnosis: the stage-2 hub is already essentially optimal; the remaining gap is in the middle legs. Not committed.

20. **Greedy vs exhaustive stage-2 hub search** compared at the user's request (`HUB_SEARCH`, default "exhaustive"). Greedy hill climbing from the centroid: worse J in 28 of 100 plans (mean +0.19, worst +1.2), better in 3, only 16% faster in stage 2. Exhaustive kept.

## Open questions (for the user or the organisers)

- Keep greedy hub relocation on (tiny gain, small runtime cost) or switch it off?

- Organisers' input JSON format (an adapter in `load_input_json` once a sample arrives).
- Time limit per plan, if any.
- Organisers to confirm our readings: lenient rule 3; X once per point, touches included; a hub next to its only device.
- Forbid retracing a line? (user undecided)
- Add the flagged fallback for rooms outside the outline? (offered, user undecided)

## How this file is kept up to date

While a Claude session is open, a recurring task appends new requests, findings and decisions here and refreshes `HANDOFF.md` when the project state changes. Scheduled tasks live only in that session and end when it closes (and after at most 7 days). In a new session, ask Claude to set the schedule up again.
