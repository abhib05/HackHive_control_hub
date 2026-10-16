"""Paired benchmark of solver variants on fixed plan sets (for algorithm experiments).

Every plan is solved by every variant in the same worker process, one after the other (the order
alternates between plans), so runtimes are compared on the same core and the comparison is fair on
this laptop's mixed fast / slow cores. Results go to bench_results/<tag>.jsonl, a summary is printed.

cmd, from the project folder:
    python bench_solver.py --make-sets                      write bench_sets.json (fixed plan ids)
    python bench_solver.py --set dev20 --tag base           current solver on the 20-plan dev set
    python bench_solver.py --set val100 --tag moves --variants base "nomoves:hr.HUB_MOVES=False"
    python bench_solver.py --summary bench_results\\moves.jsonl
Variants: "name:key=value,key=value" with key hr.NAME (hub_routing global) or cfg.name (CONFIG key),
or code=DIR (hub_routing.py + resplan_solver.py of another version, e.g. written by
    git show main:hub_routing.py > DIR\\hub_routing.py   and the same for resplan_solver.py);
"base" = the defaults. Sets: dev20, val100, heldout200, hard15 (see --make-sets), or ids:1,2,3.
"""
import argparse
import ast
import csv
import json
import multiprocessing as mp
import os
import pickle
import random
import re
import statistics as st
import sys
import time
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
SETS = os.path.join(HERE, "bench_sets.json")
OUT = os.path.join(HERE, "bench_results")
STATUS_RANK = {"valid": 0, "invalid": 1}


def make_sets():
    """dev20 / val100: benchmark plans of the test split (val100 = the sample used for the hub-move
    experiments); heldout200: random plans of the separate val split (kept out of tuning);
    hard15: the 15 slowest test-split plans of the last full run."""
    sys.path.insert(0, HERE)
    import run_resplan as rr
    rows = list(csv.DictReader(open(os.path.join(HERE, "resplan_output", "summary.csv"), encoding="utf-8")))
    good = sorted(int(r["resplan_id"]) for r in rows if r["status"] == "valid" and r["in_benchmark"] == "True")
    val100 = sorted(random.Random(1).sample(good, 100))
    dev20 = sorted(random.Random(2).sample(sorted(set(good) - set(val100)), 20))
    heldout = sorted(random.Random(3).sample(sorted(rr.load_split("val")), 200))
    slow = sorted((r for r in rows if r["runtime_s"]), key=lambda r: -float(r["runtime_s"]))
    hard15 = [int(r["resplan_id"]) for r in slow[:15]]
    sets = dict(dev20=dev20, val100=val100, heldout200=heldout, hard15=hard15)
    json.dump(sets, open(SETS, "w"), indent=1)
    print({k: len(v) for k, v in sets.items()})


def parse_variant(spec):
    name, _, body = spec.partition(":")
    over = {}
    for kv in filter(None, re.split(r",(?![^()\[\]]*[)\]])", body)):        # commas outside brackets
        k, _, v = kv.partition("=")
        try:
            over[k.strip()] = ast.literal_eval(v.strip())
        except (ValueError, SyntaxError):
            over[k.strip()] = v.strip()
    return name, over


_CODE = {}


def solver_modules(code=None):
    """(hub_routing, resplan_solver) from this folder, or from another folder holding another version of
    the two files (e.g. the last commit, written with git show), loaded side by side."""
    if code is None:
        import hub_routing as hr
        import resplan_solver as rs
        return hr, rs
    if code not in _CODE:
        import importlib.util
        saved = {m: sys.modules.get(m) for m in ("hub_routing", "resplan_solver")}
        mods = []
        for m in ("hub_routing", "resplan_solver"):
            spec = importlib.util.spec_from_file_location(m, os.path.join(code, m + ".py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[m] = mod                    # resplan_solver's "import hub_routing" gets this one
            spec.loader.exec_module(mod)
            mods.append(mod)
        for m, v in saved.items():
            if v is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = v
        _CODE[code] = tuple(mods)
    return _CODE[code]


def run_variant(inp_plan, over):
    over = dict(over)
    hr, rs = solver_modules(over.pop("code", None))
    saved = {}
    cfg = dict(rs.CONFIG)
    for k, v in over.items():
        mod, _, attr = k.partition(".")
        if mod == "hr":
            saved[attr] = getattr(hr, attr)
            setattr(hr, attr, v)
        elif mod == "cfg":
            cfg[attr] = v
        else:
            raise KeyError(k)
    try:
        t, c = time.perf_counter(), time.process_time()
        out, _ = rs.solve(rs.from_resplan(inp_plan, cfg), cfg)
        return out, time.perf_counter() - t, time.process_time() - c
    finally:
        for attr, v in saved.items():
            setattr(hr, attr, v)


def work(task):
    warnings.filterwarnings("ignore")
    import psutil
    k, plan, variants = task
    rows = []
    order = variants if k % 2 == 0 else variants[::-1]
    for name, over in order:
        try:
            out, wall, cpu = run_variant(plan, over)
        except Exception as e:                                        # noqa: BLE001
            out, wall, cpu = {"status": "error", "reason": f"{type(e).__name__}: {e}"}, None, None
        rm, sv = out.get("reported_metrics") or {}, out.get("solver") or {}
        mem = psutil.Process().memory_info()
        rows.append(dict(
            pid=plan["id"], variant=name, status=out.get("status"),
            bench=(out.get("benchmark") or {}).get("eligible"), hubs=rm.get("hub_count"),
            hub_lb=rm.get("hub_lower_bound"), J=rm.get("cost_J"), L=rm.get("total_length_m"), X=rm.get("crossings"),
            O=rm.get("overlap_m"), B=rm.get("bends"), gap=rm.get("area_gap_m2"), lbJ=rm.get("lower_bound_J"),
            agree=(out.get("check") or {}).get("agrees_with_solver"), violations=out.get("violations"),
            reason=out.get("reason"), wall=wall, cpu=cpu, rss_mb=mem.rss / 2 ** 20,
            peak_mb=getattr(mem, "peak_wset", mem.rss) / 2 ** 20,
            solver={kk: vv for kk, vv in sv.items() if isinstance(vv, (int, float, bool))}))
    return rows


def key(r):
    return (STATUS_RANK.get(r["status"], 2), r["hubs"] if r["hubs"] is not None else 99,
            round(r["J"], 4) if r["J"] is not None else 1e9, round(r["gap"] or 0, 3))


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else float("nan")


def summary(rows):
    names = list(dict.fromkeys(r["variant"] for r in rows))
    names.sort(key=lambda n: n != "base")                           # compare with "base" if present
    by = {(r["pid"], r["variant"]): r for r in rows}
    pids = sorted({r["pid"] for r in rows})
    base = names[0]
    print(f"{len(pids)} plans, variants: {', '.join(names)} (compared with {base})")
    hdr = (f"{'variant':<14}{'valid':>6}{'hubs':>6}{'@LB':>5}{'J mean':>9}{'J med':>8}{'J p90':>8}{'gap':>7}"
           f"{'L':>8}{'X':>4}{'O':>6}{'B':>6}{'t mean':>8}{'t med':>7}{'t p90':>7}{'t max':>7}{'cpu sum':>9}"
           f"{'peak MB':>8}")
    print(hdr)
    for n in names:
        rs_ = [by[(p, n)] for p in pids if (p, n) in by]
        ok = [r for r in rs_ if r["J"] is not None]
        t = [r["wall"] for r in rs_ if r["wall"] is not None]
        if not ok or not t:
            print(f"{n:<14} no results: " + ", ".join(sorted({str(r['reason'])[:80] for r in rs_})))
            continue
        mean = lambda k: st.mean(r[k] for r in ok) if ok else float("nan")
        print(f"{n:<14}{sum(r['status'] == 'valid' for r in rs_):>6}{sum(r['hubs'] for r in ok):>6}"
              f"{sum(r['hubs'] == r['hub_lb'] for r in ok):>5}{mean('J'):>9.3f}{q([r['J'] for r in ok], .5):>8.2f}"
              f"{q([r['J'] for r in ok], .9):>8.2f}{st.mean(r['J'] - r['lbJ'] for r in ok):>7.3f}{mean('L'):>8.2f}"
              f"{sum(r['X'] for r in ok):>4}{mean('O'):>6.2f}{mean('B'):>6.1f}{st.mean(t):>8.2f}{q(t, .5):>7.2f}"
              f"{q(t, .9):>7.2f}{max(t):>7.1f}{sum(r['cpu'] or 0 for r in rs_):>9.1f}"
              f"{max(r['peak_mb'] for r in rs_):>8.0f}")
    for n in names[1:]:
        pair = [(by[(p, base)], by[(p, n)]) for p in pids if (p, base) in by and (p, n) in by]
        better = sum(key(b) < key(a) for a, b in pair)
        worse = sum(key(b) > key(a) for a, b in pair)
        same_h = [(a, b) for a, b in pair if a["hubs"] == b["hubs"] and a["J"] is not None and b["J"] is not None]
        dJ = [b["J"] - a["J"] for a, b in same_h]
        ratio = [b["wall"] / a["wall"] for a, b in pair if a["wall"] and b["wall"]]
        if not dJ or not ratio:
            continue
        bad = [b["pid"] for a, b in pair if STATUS_RANK.get(b["status"], 2) > STATUS_RANK.get(a["status"], 2)]
        print(f"  {n} vs {base}: better {better}, worse {worse}, same {len(pair) - better - worse}; "
              f"dJ mean {st.mean(dJ):+.4f} min {min(dJ):+.3f} max {max(dJ):+.3f}; runtime ratio median "
              f"{q(ratio, .5):.2f} (sum {sum(b['wall'] for a, b in pair) / sum(a['wall'] for a, b in pair):.2f})"
              + (f"; STATUS WORSE: {bad}" if bad else ""))
    disagree = [(r["pid"], r["variant"]) for r in rows if r["agree"] is False]
    if disagree:
        print("checker disagrees:", disagree)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--make-sets", action="store_true")
    ap.add_argument("--set")
    ap.add_argument("--tag")
    ap.add_argument("--variants", nargs="*", default=["base"])
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--summary", nargs="*")
    a = ap.parse_args()
    if a.make_sets:
        return make_sets()
    if a.summary:
        rows = [json.loads(l) for p in a.summary for l in open(p, encoding="utf-8")]
        return summary(rows)
    if a.set.startswith("ids:"):
        ids = [int(x) for x in a.set[4:].split(",")]
    else:
        ids = json.load(open(SETS))[a.set]
    variants = [parse_variant(v) for v in a.variants]
    sys.path.insert(0, HERE)
    import run_resplan as rr
    t0 = time.perf_counter()
    data = {p["id"]: p for p in pickle.load(open(os.path.join(HERE, "ResPlan.pkl"), "rb")) if p["id"] in set(ids)}
    tasks = [(k, {kk: data[i][kk] for kk in rr.KEEP if kk in data[i]}, variants) for k, i in enumerate(ids)]
    print(f"{len(tasks)} plans x {len(variants)} variants, loaded in {time.perf_counter() - t0:.0f} s", flush=True)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{a.tag}.jsonl")
    rows, t1 = [], time.perf_counter()
    with mp.get_context("spawn").Pool(min(a.workers, len(tasks))) as pool, open(path, "w", encoding="utf-8") as f:
        for i, rs_ in enumerate(pool.imap_unordered(work, tasks, chunksize=1), 1):
            for r in rs_:
                f.write(json.dumps(r) + "\n")
            rows += rs_
            if i % max(1, len(tasks) // 10) == 0:
                print(f"  {i}/{len(tasks)} plans, {time.perf_counter() - t1:.0f} s", flush=True)
    print(f"done in {time.perf_counter() - t1:.0f} s -> {path}")
    summary(rows)


if __name__ == "__main__":
    main()
