# Solver optimisation report (2026-10-16, branch `solver-optimization`)

Goal (user's master prompt): make the existing solver produce the best valid designs (fewest hubs,
then lowest J = L + 5X + O + 0.5B, then smallest area gap) and run faster, with every claim measured.
Nothing was rebuilt; every change is a flag or config value with the old behaviour still available.

## How it was measured

- `bench_solver.py` solves every plan with every variant **in the same worker process, one after the
  other** (order alternating per plan). This laptop (i7-1360P: 4 fast + 8 slow cores, thermal limits)
  gives ±30% run-to-run noise on unpaired timings, so runtimes are compared as **paired ratios**.
- Fixed plan sets in `bench_sets.json` (written once by `python bench_solver.py --make-sets`):
  - `dev20`: 20 benchmark plans of the test split (seed 2), used for tuning;
  - `val100`: 100 benchmark plans of the test split (seed 1), validation;
  - `heldout200`: 200 random plans of the **val** split (seed 3), never used for tuning;
  - `hard15`: the 15 slowest test-split plans of the last full run (3 of them outside the benchmark).
- "orig" = the committed solver (`main` at 03f1780), loaded side by side (`code=` variant).
- Every run: status, hubs, hub lower bound, J, L, X, O, B, area gap, J lower bound, runtime, CPU time,
  stage timings and counters (`solver` field of the output JSON), checker agreement.

RESULTS_PLACEHOLDER
