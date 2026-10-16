"""Cable Ring Studio: a local web app for the solver.

Browse the ResPlan floor plans, run the solver on one plan and watch every step as it happens
(devices, grid, door links, zones, hubs, each ring leg, the clean-up step and the final check), or
start a batch run and watch the results arrive plan by plan. Standard library only; the page is in web/.

cmd, from the project folder:
    python web_app.py                          opens http://127.0.0.1:8765 in your browser
    python web_app.py --port 9000 --no-browser
Batch runs write to resplan_output exactly like run_resplan.py (one JSON + one overlay PNG per plan).
A single live run is only written there when you press Save.
Floor-plan images (colour-coded PNG / JPG, see image_input.py) can be opened from the sidebar with their floor
area in m2; they are solved like any plan and saved to image_output.
"""
import argparse
import csv
import glob
import json
import multiprocessing as mp
import os
import pickle
import random
import threading
import time
import traceback
import warnings
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import resplan_solver as rs
import run_resplan as rr

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
SPLITS = ("train", "val", "test", "augmented")
SPEEDS = {"instant": 0.0, "watch": 0.05, "slow": 0.25}          # seconds of pause per leg drawn
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
STORE = BATCH = None                                             # set in main(); worker processes never load the data
IMG_OUT = os.path.join(HERE, "image_output")
MAX_UPLOAD = 25 * 2 ** 20


class ClientGone(Exception):
    """The browser closed the stream (Stop button, new run, closed tab)."""


def _default(o):
    if hasattr(o, "item"):
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def dumps(o):
    return json.dumps(o, default=_default, separators=(",", ":"))


# --------------------------------------------------------------------------- data
class Store:
    """ResPlan plans (loaded in the background at start-up), the split of each plan, the stored results in
    resplan_output and the last live runs (kept so Save writes exactly what was shown)."""

    def __init__(self, pkl, out):
        self.pkl, self.out = pkl, out
        self.ready, self.error, self.load_s = threading.Event(), None, None
        self.plans, self.order, self.split, self.results, self.recent = {}, [], {}, {}, {}
        self.images = {}                  # "img-<name>" -> (ResPlan-style plan dict, image info) of uploaded images
        self.lock = threading.Lock()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            t0 = time.perf_counter()
            with open(self.pkl, "rb") as f:
                data = pickle.load(f)
            for p in data:
                self.plans[p["id"]] = {k: p[k] for k in rr.KEEP if k in p}
                self.order.append(p["id"])
            for s in SPLITS:
                try:
                    for i in rr.load_split(s):
                        self.split[int(i)] = s
                except SystemExit:
                    pass
            self.reload_results()
            self.load_s = round(time.perf_counter() - t0, 1)
        except Exception as e:                                    # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.ready.set()

    def reload_results(self):
        """summary.csv, plus any plan JSON written after it (e.g. by a stopped run)."""
        rows, path = {}, os.path.join(self.out, "summary.csv")
        t_csv = os.path.getmtime(path) if os.path.exists(path) else 0
        if t_csv:
            with open(path, encoding="utf-8") as f:
                rows = {r["plan_id"]: r for r in csv.DictReader(f)}
        for p in glob.glob(os.path.join(self.out, "json", "*.json")):
            if os.path.getmtime(p) > t_csv or os.path.splitext(os.path.basename(p))[0] not in rows:
                try:
                    with open(p, encoding="utf-8") as f:
                        r = rr.summary_row(json.load(f))
                    rows[r["plan_id"]] = r
                except (OSError, ValueError):
                    pass
        with self.lock:
            self.results = rows

    def update(self, row):
        with self.lock:
            self.results[row["plan_id"]] = row

    def remember(self, pid, zone, inp, out, extras):
        with self.lock:
            self.recent[(pid, zone)] = (inp, out, extras)
            while len(self.recent) > 20:
                self.recent.pop(next(iter(self.recent)))

    def plan_rows(self):
        """Compact list for the plan picker: [id, split, status, hubs, J, in_benchmark, hub lower bound]."""
        num = lambda v, f: f(v) if v not in (None, "", "None") else None
        out = []
        with self.lock:
            for i in self.order:
                r = self.results.get(rr.plan_id_of(i))
                if r is None:
                    out.append([i, self.split.get(i), None, None, None, None, None])
                else:
                    b = r.get("in_benchmark")
                    out.append([i, self.split.get(i), r.get("status"), num(r.get("hubs"), int), num(r.get("J"), float),
                                None if b in (None, "") else str(b) == "True", num(r.get("hub_lower_bound"), int)])
        return out


def is_img(pid):
    return isinstance(pid, str) and pid.startswith("img-")


def pid_of(q):
    v = q.get("id", "-1")
    return v if is_img(v) else int(v)


def stem_of(pid):
    return "IMG_" + pid[4:] if is_img(pid) else rr.plan_id_of(pid)


def out_of(pid):
    return IMG_OUT if is_img(pid) else STORE.out


def read_output(pid):
    path = os.path.join(out_of(pid), "json", f"{stem_of(pid)}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def png_url(pid):
    path = os.path.join(out_of(pid), "png", f"{stem_of(pid)}.png")
    route = "imgpng" if is_img(pid) else "png"
    return f"/{route}/{stem_of(pid)}.png?v={int(os.path.getmtime(path))}" if os.path.exists(path) else None


def get_plan(pid):
    if is_img(pid):
        got = STORE.images.get(pid)
        return got[0] if got else None
    return STORE.plans.get(pid)


# --------------------------------------------------------------------------- live run of one plan
class Live:
    """emit() for resplan_solver.solve: sends each step to the browser as a server-sent event, paced by the
    chosen speed. At full speed, bursts of leg / attempt / improve events are thinned to ~25 per second
    (the rings, groupings and the result are always sent)."""
    TRANSIENT = {"leg", "attempt", "improve"}
    PAUSE = {"leg": 1, "attempt": 3, "ring": 6, "grouping": 10, "groupings": 10, "routed": 8, "improving": 6,
             "improve": 3, "chosen": 10, "plan": 6, "devices": 8, "grid": 8, "links": 6, "stage": 2}

    def __init__(self, handler, delay):
        self.h, self.delay, self.t0, self.last = handler, delay, time.perf_counter(), 0.0

    def __call__(self, kind, data):
        now = time.perf_counter()
        if kind in self.TRANSIENT and not self.delay:
            if now - self.last < 0.04:
                return
            self.last = now
        self.h.sse(kind, dict(data, t=round(now - self.t0, 3)))
        if self.delay:
            time.sleep(self.delay * self.PAUSE.get(kind, 0))


def api_solve(h, q):
    STORE.ready.wait()
    pid, zone, speed = pid_of(q), q.get("zone", "any"), q.get("speed", "instant")
    cfg = dict(rs.CONFIG, zone_mode=zone if zone in ("any", "excluded", "own") else "any")
    delay = SPEEDS.get(speed, 0.0)
    if delay:
        cfg["time_budget_s"] = 1e9          # watching slowly must not change the result (90 s budget)
        cfg["hard_limit_s"] = 1e9
    h.sse_open()
    live = Live(h, delay)
    try:
        p = get_plan(pid)
        if p is None:
            live("fail", {"msg": f"plan {pid} is not loaded (open the image again)" if is_img(pid)
                          else f"plan {pid} is not in ResPlan.pkl"})
            return
        live("start", {"id": pid, "plan_id": stem_of(pid), "zone": cfg["zone_mode"], "speed": speed})
        live("stage", {"stage": "plan", "msg": "reading the plan, drawing units -> metres"})
        inp = rs.from_resplan(p, cfg)
        geo = rs.plan_geometry(inp)
        nodes = geo.pop("nodes")
        live("plan", geo)
        live("stage", {"stage": "devices", "msg": "adding one device to every living room, bedroom, kitchen and "
                                                  "storage room"})
        live("devices", {"nodes": nodes, "warnings": inp["warnings"]})
        out, extras = rs.solve(inp, cfg, emit=live)
        if is_img(pid):
            out["image"] = STORE.images[pid][1]
        STORE.remember(pid, cfg["zone_mode"], inp, out, extras)
        live("result", {"out": out, "png": png_url(pid)})
    except ClientGone:
        return
    except Exception as e:                                        # noqa: BLE001
        try:
            h.sse("fail", {"msg": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=8)})
        except ClientGone:
            pass


def api_save(h, q):
    """Write the last live result of a plan (JSON + overlay PNG) into resplan_output, like run_resplan.py."""
    pid, zone = pid_of(q), q.get("zone", "any")
    with STORE.lock:
        got = STORE.recent.get((pid, zone))
    if got is None:
        return h.send_json({"ok": False, "msg": "run the solver on this plan first"}, 404)
    inp, out, extras = got
    od = out_of(pid)
    for sub in ("json", "png"):
        os.makedirs(os.path.join(od, sub), exist_ok=True)
    stem = out["plan_id"]
    with open(os.path.join(od, "json", f"{stem}.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    try:
        rs.draw_overlay(inp, out, os.path.join(od, "png", f"{stem}.png"), (extras or {}).get("chk"))
    except Exception as e:                                        # noqa: BLE001
        return h.send_json({"ok": False, "msg": f"JSON saved, overlay failed: {e}"})
    if not is_img(pid):
        STORE.update(rr.summary_row(out))
    name = os.path.basename(od)
    h.send_json({"ok": True, "msg": f"saved {name}/json/{stem}.json and png/{stem}.png", "png": png_url(pid)})


# --------------------------------------------------------------------------- batch runs
class Batch:
    """One batch run at a time in a background thread (worker processes like run_resplan.py). Its events are
    kept, so a page opened (or reloaded) mid-run replays them and follows along."""

    def __init__(self):
        self.events, self.cv, self.thread, self.stop_req, self.run_id = [], threading.Condition(), None, False, 0

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def push(self, kind, data):
        with self.cv:
            self.events.append((kind, dict(data, run=self.run_id, ts=round(time.time(), 3))))
            self.cv.notify_all()

    def select(self, q):
        mode = q.get("mode", "first")
        n = max(1, int(q.get("n") or 20))
        if mode == "all":
            return list(STORE.order), "all plans"
        if mode == "split":
            s = q.get("split", "test")
            return [i for i in STORE.order if STORE.split.get(i) == s], f"{s} split"
        if mode == "sample":
            seed = int(q.get("seed") or 0)
            return random.Random(seed).sample(STORE.order, min(n, len(STORE.order))), f"random {n} (seed {seed})"
        if mode == "ids":
            ids = [int(x) for x in q.get("ids", "").replace(" ", ",").split(",") if x.strip().isdigit()]
            return [i for i in ids if i in STORE.plans], f"{len(ids)} chosen ids"
        return STORE.order[:n], f"first {n}"

    def start(self, q):
        if self.running():
            return False, "a batch is already running"
        ids, label = self.select(q)
        if not ids:
            return False, "no plans selected"
        opts = dict(out=STORE.out, png=q.get("png", "1") == "1", write_inputs=False,
                    zone=q.get("zone", "any") if q.get("zone") in ("any", "excluded", "own") else "any")
        workers = max(1, min(int(q.get("workers") or (os.cpu_count() or 2) - 2), os.cpu_count() or 1))
        with self.cv:
            self.events, self.stop_req, self.run_id = [], False, self.run_id + 1
        self.thread = threading.Thread(target=self._run, args=(ids, label, opts, workers, q.get("force") == "1"),
                                       daemon=True)
        self.thread.start()
        return True, f"{label}: {len(ids)} plans"

    def _run(self, ids, label, opts, workers, force):
        t0 = time.perf_counter()
        try:
            for sub in ("json", "png"):
                os.makedirs(os.path.join(opts["out"], sub), exist_ok=True)
            stem = lambda p: os.path.splitext(os.path.basename(p))[0]
            done = set() if force else {stem(p) for p in glob.glob(os.path.join(opts["out"], "json", "*.json"))}
            if opts["png"] and not force:
                done &= {stem(p) for p in glob.glob(os.path.join(opts["out"], "png", "*.png"))}
            todo = [i for i in ids if rr.plan_id_of(i) not in done]
            self.push("start", {"label": label, "total": len(ids), "cached": len(ids) - len(todo), "to_solve": len(todo),
                                "workers": workers, "zone": opts["zone"], "png": opts["png"]})
            for i in ids:
                if rr.plan_id_of(i) in done:
                    row = STORE.results.get(rr.plan_id_of(i))
                    if row is None:
                        o = read_output(i)
                        row = rr.summary_row(o) if o else {"plan_id": rr.plan_id_of(i), "status": None}
                    self.push("row", dict(row, cached=True, id=i))
            solved = 0
            if todo:
                tasks = [("plan", STORE.plans[i], opts) for i in todo]
                with mp.get_context("spawn").Pool(workers, maxtasksperchild=100) as pool:
                    it = pool.imap_unordered(rr.work, tasks, chunksize=1)
                    while solved < len(tasks) and not self.stop_req:
                        try:
                            pid, status, dt = it.next(timeout=0.5)
                        except mp.TimeoutError:
                            continue
                        solved += 1
                        try:
                            with open(os.path.join(opts["out"], "json", f"{pid}.json"), encoding="utf-8") as f:
                                row = rr.summary_row(json.load(f))
                        except (OSError, ValueError):
                            row = {"plan_id": pid, "status": status}
                        STORE.update(row)
                        self.push("row", dict(row, cached=False, id=int(pid.split("_")[1]), wall_s=round(dt, 2),
                                              png=png_url(int(pid.split("_")[1]))))
                    # leaving the with-block terminates the pool (also when Stop was pressed)
                rr.write_summary(opts["out"])
                STORE.reload_results()
            self.push("done", {"stopped": self.stop_req, "solved": solved, "elapsed_s": round(time.perf_counter() - t0, 1)})
        except Exception as e:                                    # noqa: BLE001
            self.push("fail", {"msg": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=8)})
            self.push("done", {"stopped": True, "solved": 0, "elapsed_s": round(time.perf_counter() - t0, 1)})

    def stop(self):
        self.stop_req = True


def api_batch_events(h, q):
    """Replay the current (or last) batch's events from index `from`, then follow until it finishes."""
    i = int(q.get("from", 0))
    h.sse_open()
    try:
        while True:
            with BATCH.cv:
                if len(BATCH.events) <= i and BATCH.running():
                    BATCH.cv.wait(timeout=5)
                new, running = BATCH.events[i:], BATCH.running()
            for kind, data in new:
                h.sse(kind, dict(data, seq=i))
                i += 1
            if not new:
                if not running:
                    h.sse("idle", {})
                    return
                h.ping()
    except ClientGone:
        return


# --------------------------------------------------------------------------- HTTP
def api_status(h, q):
    h.send_json({"ready": STORE.ready.is_set(), "error": STORE.error, "plans": len(STORE.order), "load_s": STORE.load_s,
                 "batch_running": BATCH.running(), "cpus": os.cpu_count(), "out": STORE.out,
                 "zone_default": rs.CONFIG["zone_mode"]})


def api_plans(h, q):
    STORE.ready.wait()
    h.send_json({"plans": STORE.plan_rows(), "error": STORE.error})


def api_plan(h, q):
    """Input geometry (no devices: the plans have none), door / open-passage links and the stored result."""
    STORE.ready.wait()
    pid = pid_of(q)
    p = get_plan(pid)
    if p is None:
        return h.send_json({"error": f"plan {pid} is not loaded (open the image again)" if is_img(pid)
                            else f"plan {pid} is not in ResPlan.pkl"}, 404)
    try:
        inp = rs.from_resplan(p, rs.CONFIG, nodes=[])
    except Exception as e:                                        # noqa: BLE001
        return h.send_json({"error": f"cannot read plan {pid}: {e}", "stored": read_output(pid)})
    geo = rs.plan_geometry(inp)
    x1, y1, x2, y2 = inp["inner"].bounds
    _, kinds = rs.room_links(inp["rooms"], inp["doors"], inp["walls"], inp["windows"], inp["wall_depth"],
                             max(x2 - x1, y2 - y1), rs.CONFIG["passage_factor"])
    h.send_json({"id": pid, "split": None if is_img(pid) else STORE.split.get(pid), "geometry": geo,
                 "image": STORE.images[pid][1] if is_img(pid) else None,
                 "links": [{"rooms": [a, b], "type": t} for (a, b), t in sorted(kinds.items())],
                 "stored": read_output(pid), "png": png_url(pid)})


def api_image(h, q):
    """POST body = the image file; ?name=<file name>&area=<m2>. Reads it (image_input.plan_from_image) and keeps
    the plan as id "img-<name>" for /api/plan, /api/solve and /api/save."""
    import re
    import image_input
    area = None
    if q.get("area", "").strip():
        try:
            area = float(q["area"])
            if not 1 <= area <= 5000:
                raise ValueError
        except ValueError:
            return h.send_json({"ok": False, "msg": "the floor area must be a number of m2 (e.g. 95), or empty"}, 400)
    n = int(h.headers.get("Content-Length") or 0)
    if not 0 < n <= MAX_UPLOAD:
        return h.send_json({"ok": False, "msg": "no image received, or larger than 25 MB"}, 400)
    body = h.rfile.read(n)
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(os.path.basename(q.get("name", "plan")))[0])[:60] or "plan"
    ext = os.path.splitext(q.get("name", ""))[1].lower()
    ext = ext if ext in (".png", ".jpg", ".jpeg", ".bmp") else ".png"
    os.makedirs(os.path.join(IMG_OUT, "uploads"), exist_ok=True)
    path = os.path.join(IMG_OUT, "uploads", base + ext)
    with open(path, "wb") as f:
        f.write(body)
    try:
        mode = q.get("mode", "auto") if q.get("mode") in ("auto", "colour", "legend", "titles") else "auto"
        plan, info = image_input.plan_from_image(path, area, plan_id="IMG_" + base, mode=mode)
    except Exception as e:                                        # noqa: BLE001
        return h.send_json({"ok": False, "msg": f"could not read the image: {e}"}, 422)
    pid = "img-" + base
    with STORE.lock:
        STORE.images[pid] = (plan, info)
    h.send_json({"ok": True, "id": pid, "info": info})


def api_batch_start(h, q):
    STORE.ready.wait()
    ok, msg = BATCH.start(q)
    h.send_json({"ok": ok, "msg": msg}, 200 if ok else 409)


def api_batch_stop(h, q):
    BATCH.stop()
    h.send_json({"ok": True})


ROUTES = {"/api/status": api_status, "/api/plans": api_plans, "/api/plan": api_plan, "/api/solve": api_solve,
          "/api/save": api_save, "/api/batch/start": api_batch_start, "/api/batch/stop": api_batch_stop,
          "/api/batch/events": api_batch_events, "/api/image": api_image}


class Handler(BaseHTTPRequestHandler):
    server_version = "CableRingStudio/1.0"

    def log_message(self, fmt, *args):                            # keep the console quiet
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ROUTES:
                return ROUTES[u.path](self, q)
            if u.path.startswith("/png/"):
                return self.send_file(os.path.join(STORE.out, "png", os.path.basename(u.path)))
            if u.path.startswith("/imgpng/"):
                return self.send_file(os.path.join(IMG_OUT, "png", os.path.basename(u.path)))
            name = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            path = os.path.normpath(os.path.join(WEB, name))
            if not path.startswith(WEB + os.sep):
                return self.send_error(404)
            return self.send_file(path)
        except (ClientGone, ConnectionError):
            return

    do_POST = do_GET

    def send_file(self, path):
        if not os.path.isfile(path):
            return self.send_error(404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        body = dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _write(self, text):
        try:
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()
        except OSError as e:
            raise ClientGone() from e

    def sse(self, kind, data):
        self._write(f"event: {kind}\ndata: {dumps(data)}\n\n")

    def ping(self):
        self._write(": ping\n\n")


def main():
    global STORE, BATCH
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = only this computer can open the page")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    ap.add_argument("--pkl", default=os.path.join(HERE, "ResPlan.pkl"))
    ap.add_argument("--out", default=os.path.join(HERE, "resplan_output"))
    a = ap.parse_args()
    warnings.filterwarnings("ignore")
    os.environ.setdefault("OMP_NUM_THREADS", "1")                  # worker processes: one thread each
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    STORE, BATCH = Store(a.pkl, os.path.abspath(a.out)), Batch()
    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        raise SystemExit(f"cannot listen on {a.host}:{a.port} ({e}); is it already running? Try --port 8766")
    srv.daemon_threads = True
    url = f"http://{a.host}:{a.port}/"
    print(f"Cable Ring Studio running at {url}  (loading {os.path.basename(a.pkl)} in the background)")
    print("Press Ctrl+C to stop.", flush=True)
    if not a.no_browser:
        threading.Timer(0.6, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        BATCH.stop()
        print("\nStopped.")


if __name__ == "__main__":
    main()
