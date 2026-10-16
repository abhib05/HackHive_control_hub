"""Solve one ResPlan plan under the strict and the lenient reading of rule 3 and draw both designs side by side.

cmd:
    cd /d "C:\\Users\\abhin\\OneDrive\\Desktop\\Honeywell_hackathon"
    python compare_zone_rules.py              (default plan id 14877)
    python compare_zone_rules.py 3467         (any ResPlan id)

Writes zone_rules_PLAN_<id>.png plus the two output JSONs zone_rules_PLAN_<id>_strict.json / _lenient.json.
strict  (zone_mode "own"): a zone's rooms must be linked by doors / open passages among themselves.
lenient (zone_mode "any"): the door chain linking a zone's rooms may pass through any room.
Wires may run through any room under both readings.
"""
import json
import os
import pickle
import sys
import warnings

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import resplan_solver as rs

warnings.filterwarnings("ignore")


def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else 14877
    with open("ResPlan.pkl", "rb") as f:
        plan = next(p for p in pickle.load(f) if p["id"] == pid)
    tag = rs.from_resplan(plan)["plan_id"]
    panels = []
    for name, mode in (("strict", "own"), ("lenient", "any")):
        cfg = dict(rs.CONFIG, zone_mode=mode)
        inp = rs.from_resplan(plan, cfg)
        out, extras = rs.solve(inp, cfg)
        with open(f"zone_rules_{tag}_{name}.json", "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
        png = f"zone_rules_{tag}_{name}.png"
        rs.draw_overlay(inp, out, png, (extras or {}).get("chk"))
        rm = out["reported_metrics"]
        print(f"{name:8} ({mode}): {out['status']}, {rm['hub_count']} hubs, J = {rm['cost_J']:.2f}")
        for h in out["hubs"]:
            print(f"   {h['hub_id']} in {h['room_id']}: zone {', '.join(h['assigned_rooms'])} "
                  f"({h['zone_area_m2']:.1f} m2)   ring {' -> '.join(h['closed_sequence'])}, {h['ring_length_m']:.2f} m")
        panels.append((name, mode, out, png))

    links = ", ".join(f"{a}-{b}" for (a, b) in (l["rooms"] for l in panels[0][2]["links"]))
    fig, axes = plt.subplots(1, 2, figsize=(22, 10.5))
    for ax, (name, mode, out, png) in zip(axes, panels):
        img = plt.imread(png)
        ink = np.nonzero((img[:, :, :3] < 0.97).any(2))           # crop the white margins
        img = img[max(ink[0].min() - 10, 0):ink[0].max() + 10, max(ink[1].min() - 10, 0):ink[1].max() + 10]
        ax.imshow(img)
        ax.axis("off")
        rm = out["reported_metrics"]
        rule = ("STRICT: a zone's rooms must be linked by doors among themselves" if mode == "own" else
                "LENIENT: the door chain may pass through any room")
        ax.set_title(f"{rule}\n{rm['hub_count']} hubs, J = {rm['cost_J']:.1f}", fontsize=13, fontweight="bold")
    fig.suptitle(f"{tag}: same plan, same devices, same cable rules (wires may cross any room) - only rule 3 differs\n"
                 f"door / open-passage links between rooms: {links}", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(f"zone_rules_{tag}.png", dpi=110)
    for *_, png in panels:
        os.remove(png)
    print(f"Saved zone_rules_{tag}.png and the two JSON files")


if __name__ == "__main__":
    main()
