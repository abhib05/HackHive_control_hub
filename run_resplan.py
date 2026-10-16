"""Batch runner: ResPlan plans -> one output JSON per plan, plus summary.csv (and optional overlays).

Plain Python, no manual step: every plan gets its devices added (one per living room, bedroom,
kitchen and storage room), hubs, zones and closed rings, and is written as JSON (deck slide 8).

cmd, from the project folder:
    python run_resplan.py --first 20 --png          first 20 plans of ResPlan.pkl, with overlay PNGs
    python run_resplan.py --split test              the 1,632 test-split plans (ids from split.json)
    python run_resplan.py --all                     all 17,000 plans
    python run_resplan.py --ids 14433,14926 --png   chosen ResPlan ids
    python run_resplan.py --sample 500 --seed 0     random sample of plans
    python run_resplan.py --inputs DIR              plan input JSON files (*.json) instead of ResPlan.pkl
Options: --out DIR (default resplan_output)   --workers N (default: CPU cores - 2)   --png   --force
         --write-inputs (also save each plan's input JSON, devices included)   --zone any|excluded|own
Re-running skips plans whose JSON already exists (resume); --force redoes them.
Writes OUT/json/<plan_id>.json, OUT/png/<plan_id>.png, OUT/inputs/<plan_id>.json and OUT/summary.csv.
"""
import argparse
import csv
import glob
import json
import multiprocessing as mp
import os
import pickle
import random
import sys
import time
import warnings
import zipfile
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
KEEP = ("id", "area", "inner", "wall", "window", "door", "front_door", "wall_depth", "living", "bedroom",
        "kitchen", "storage", "bathroom", "stair", "balcony", "balacony")


def plan_id_of(pid):
    return f"PLAN_{int(pid):06d}"


def work(task):
    """Runs in a worker process: solve one plan, write its files, return its summary row."""
    warnings.filterwarnings("ignore")
    import resplan_solver as rs
    kind, payload, opts = task
    cfg = dict(rs.CONFIG, zone_mode=opts["zone"])
    t0 = time.perf_counter()
    try:
        if kind == "file":
            inp = rs.load_input_json(payload, cfg)
            out, extras = rs.solve(inp, cfg)
        else:
            out, inp, extras = rs.solve_safe(payload, cfg)
    except Exception as e:                                        # noqa: BLE001
        out, inp, extras = {"plan_id": os.path.splitext(os.path.basename(str(payload)))[0] if kind == "file"
                                       else plan_id_of(payload.get("id", 0)),
                            "units": "m", "hubs": [], "reported_metrics": {}, "status": "error",
                            "reason": f"{type(e).__name__}: {e}"}, None, None
    pid = out["plan_id"]
    with open(os.path.join(opts["out"], "json", f"{pid}.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    if opts["write_inputs"] and inp is not None:
        with open(os.path.join(opts["out"], "inputs", f"{pid}.json"), "w", encoding="utf-8") as f:
            json.dump(rs.input_to_json(inp), f)
    if opts["png"] and inp is not None and out.get("hubs"):
        try:
            rs.draw_overlay(inp, out, os.path.join(opts["out"], "png", f"{pid}.png"), (extras or {}).get("chk"))
        except Exception as e:                                    # noqa: BLE001
            print(f"  overlay failed for {pid}: {e}", flush=True)
    return pid, out.get("status"), time.perf_counter() - t0


def summary_row(o):
    rm = o.get("reported_metrics") or {}
    sv = o.get("solver") or {}
    return {
        "plan_id": o.get("plan_id"), "resplan_id": o.get("resplan_id"), "status": o.get("status"),
        "in_benchmark": (o.get("benchmark") or {}).get("eligible"),
        "hubs": rm.get("hub_count"), "hub_lower_bound": rm.get("hub_lower_bound"),
        "J": rm.get("cost_J"), "L": rm.get("total_length_m"), "X": rm.get("crossings"), "O": rm.get("overlap_m"),
        "B": rm.get("bends"), "area_gap_m2": rm.get("area_gap_m2"), "lower_bound_J": rm.get("lower_bound_J"),
        "eligible_rooms": sum(1 for r in o.get("rooms", []) if r.get("has_device")),
        "groupings": sv.get("groupings"), "groupings_routed": sv.get("groupings_routed"),
        "stopped_early": sv.get("stopped_early", False), "runtime_s": sv.get("runtime_s"),
        "checker_agrees": (o.get("check") or {}).get("agrees_with_solver"),
        "warnings": len(o.get("warnings", [])), "violations": len(o.get("violations", [])),
        "first_violation": (o.get("violations") or [""])[0], "reason": o.get("reason", ""),
    }


def write_summary(out_dir):
    rows = []
    for path in sorted(glob.glob(os.path.join(out_dir, "json", "*.json"))):
        with open(path, encoding="utf-8") as f:
            rows.append(summary_row(json.load(f)))
    if not rows:
        return rows
    with open(os.path.join(out_dir, "summary.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return rows


def report(rows):
    st = Counter(r["status"] for r in rows)
    print(f"\nPlans in {len(rows)} JSON files: " + ", ".join(f"{k} {v}" for k, v in st.most_common()))
    bench = [r for r in rows if r["in_benchmark"] in (True, "True")]
    print(f"Inside the organisers' benchmark filters (slide 3 notes): {len(bench)} plans, of which "
          + ", ".join(f"{k} {v}" for k, v in Counter(r["status"] for r in bench).most_common()))
    ok = [r for r in rows if r["status"] in ("valid", "invalid")]
    if ok:
        hubs = Counter(r["hubs"] for r in ok)
        print("Hubs per plan: " + ", ".join(f"{k} hubs: {v}" for k, v in sorted(hubs.items())))
        at_lb = sum(1 for r in ok if r["hubs"] == r["hub_lower_bound"])
        print(f"Plans at the hub lower bound max(ceil(area/100), ceil(nodes/5)): {at_lb} of {len(ok)}")
        js = sorted(float(r["J"]) for r in ok)
        rt = sorted(float(r["runtime_s"] or 0) for r in ok)
        print(f"J: median {js[len(js) // 2]:.1f}, mean {sum(js) / len(js):.1f}   "
              f"runtime per plan: median {rt[len(rt) // 2]:.1f} s, max {rt[-1]:.1f} s")
        print(f"Checker disagrees with solver: {sum(1 for r in ok if r['checker_agrees'] is False)}   "
              f"stopped early (time/grouping cap): {sum(1 for r in ok if r['stopped_early'] in (True, 'True'))}")
        viol = Counter(r["first_violation"].split(":")[0] for r in ok if r["violations"])
        if viol:
            print("Invalid designs by first broken rule: " + ", ".join(f"{k} {v}" for k, v in viol.most_common()))
    bad = [r for r in rows if r["status"] in ("failed", "error", "skipped")]
    for r in bad[:15]:
        print(f"  {r['plan_id']} {r['status']}: {r['reason']}")


def load_split(name):
    for src in (os.path.join(HERE, "split.json"), os.path.join(HERE, "resplan.zip")):
        if src.endswith(".json") and os.path.exists(src):
            with open(src, encoding="utf-8") as f:
                return json.load(f)[name]
        if src.endswith(".zip") and os.path.exists(src):
            with zipfile.ZipFile(src) as z:
                return json.loads(z.read("split.json"))[name]
    sys.exit("split.json not found (looked for split.json and resplan.zip in the project folder)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--first", type=int, help="first N plans of ResPlan.pkl")
    sel.add_argument("--split", choices=["train", "val", "test", "augmented"], help="plans of one split")
    sel.add_argument("--all", action="store_true", help="all plans")
    sel.add_argument("--ids", help="comma-separated ResPlan ids")
    sel.add_argument("--sample", type=int, help="random sample of N plans")
    sel.add_argument("--inputs", help="folder of plan input JSON files")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--pkl", default=os.path.join(HERE, "ResPlan.pkl"))
    ap.add_argument("--out", default=os.path.join(HERE, "resplan_output"))
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--png", action="store_true", help="also draw an overlay PNG per plan")
    ap.add_argument("--write-inputs", action="store_true", help="also save each plan's input JSON")
    ap.add_argument("--force", action="store_true", help="redo plans whose JSON already exists")
    ap.add_argument("--zone", choices=["any", "excluded", "own"], default="any",
                    help="rule-3 reading: any = lenient (default, allowed by the user), own = strict")
    a = ap.parse_args()

    for sub in ("json", "png", "inputs"):
        os.makedirs(os.path.join(a.out, sub), exist_ok=True)
    opts = dict(out=a.out, png=a.png, write_inputs=a.write_inputs, zone=a.zone)
    done = set() if a.force else {os.path.splitext(os.path.basename(p))[0]
                                  for p in glob.glob(os.path.join(a.out, "json", "*.json"))}
    t0 = time.perf_counter()
    if a.inputs:
        files = sorted(glob.glob(os.path.join(a.inputs, "*.json")))
        tasks = [("file", p, opts) for p in files if os.path.splitext(os.path.basename(p))[0] not in done]
        total = len(files)
    else:
        print(f"Loading {a.pkl} ...", flush=True)
        with open(a.pkl, "rb") as f:
            data = pickle.load(f)
        if a.first:
            plans = data[:a.first]
        elif a.all:
            plans = data
        elif a.sample:
            plans = random.Random(a.seed).sample(data, min(a.sample, len(data)))
        else:
            want = [int(x) for x in a.ids.split(",")] if a.ids else load_split(a.split)
            by_id = {p["id"]: p for p in data}
            missing = [i for i in want if i not in by_id]
            if missing:
                print(f"{len(missing)} ids not in the pickle, e.g. {missing[:5]}")
            plans = [by_id[i] for i in want if i in by_id]
        total = len(plans)
        tasks = [("plan", {k: p[k] for k in KEEP if k in p}, opts) for p in plans if plan_id_of(p["id"]) not in done]
        del data
        print(f"Loaded in {time.perf_counter() - t0:.0f} s", flush=True)
    print(f"{total} plans selected, {total - len(tasks)} already done, {len(tasks)} to solve "
          f"with {a.workers} worker processes -> {a.out}", flush=True)

    if tasks:
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        t1, st, last = time.perf_counter(), Counter(), 0.0
        step = max(1, len(tasks) // 40)
        with mp.get_context("spawn").Pool(a.workers, maxtasksperchild=100) as pool:
            for k, (pid, status, dt) in enumerate(pool.imap_unordered(work, tasks, chunksize=1), 1):
                st[status] += 1
                now = time.perf_counter() - t1
                if k % step == 0 or k == len(tasks) or now - last > 30:
                    last = now
                    eta = now / k * (len(tasks) - k)
                    print(f"  {k}/{len(tasks)}  {now:.0f} s elapsed, ETA {eta / 60:.1f} min   "
                          + ", ".join(f"{s} {n}" for s, n in st.most_common()), flush=True)
        print(f"Solved {len(tasks)} plans in {(time.perf_counter() - t1) / 60:.1f} min "
              f"({(time.perf_counter() - t1) / len(tasks):.2f} s per plan with {a.workers} workers)")
    rows = write_summary(a.out)
    report(rows)
    print(f"\nOutput: {os.path.join(a.out, 'json')}  and  {os.path.join(a.out, 'summary.csv')}")


if __name__ == "__main__":
    main()
