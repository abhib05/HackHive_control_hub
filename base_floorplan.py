"""Redraw the example floor plan (walls, doors, room names) without hubs or cable loops.

Coordinates are in pixels traced from the organisers' example slide (y grows downward).
Run:  python base_floorplan.py   -> writes base_floorplan.png and base_floorplan.svg
"""
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

X0, X1, Y0, Y1 = 8, 825, 10, 537   # outer wall
WALL = 4.5
WALL_C = "#3a3a3a"

# Neutral fills (no panel territories); grey = no device
BLUE = ORANGE = "#f4f5f7"
GREY = "#e1e2e4"
DEVICE_C = "#3d4a5c"

# name, (x0, y0, x1, y1), label text, fill
ROOMS = [
    ("Bedroom 1", (X0, Y0, 240, 243), "16 m²", BLUE),
    ("Bedroom 2", (240, Y0, 473, 243), "16 m²", ORANGE),
    ("Kitchen",   (473, Y0, 707, 243), "16 m²", ORANGE),
    ("Bathroom",  (707, Y0, X1, 243),  "no device", GREY),
    ("Bedroom 3", (X0, 243, 185, Y1),  "15 m²", BLUE),
    ("Living",    (185, 243, 650, Y1), "40 m²", BLUE),
    ("Storage",   (650, 243, X1, Y1),  "15 m²", ORANGE),
]

# Wall segments (x0, y0, x1, y1); gaps between segments are doors/openings.
WALLS = [
    # outer boundary
    (X0, Y0, X1, Y0), (X0, Y1, X1, Y1), (X0, Y0, X0, Y1), (X1, Y0, X1, Y1),
    # vertical walls, top row
    (240, Y0, 240, 243),                 # Bedroom 1 | Bedroom 2 (solid)
    (473, Y0, 473, 110), (473, 150, 473, 243),   # Bedroom 2 | Kitchen (door 110-150)
    (707, Y0, 707, 243),                 # Kitchen | Bathroom (solid)
    # horizontal wall y = 243
    (X0, 243, 65, 243),                  # door 65-115: Bedroom 1 <-> Bedroom 3
    (115, 243, 195, 243),                # door 195-230: Bedroom 1 <-> Living
    (230, 243, 660, 243),                # Bedroom 2 / Kitchen | Living (solid); opening 660-695 Kitchen <-> Storage
    (695, 243, 750, 243),                # door 750-785: Bathroom <-> Storage
    (785, 243, X1, 243),
    # vertical walls, bottom row
    (185, 243, 185, 385), (185, 430, 185, Y1),   # Bedroom 3 | Living (door 385-430)
    (650, 243, 650, 385), (650, 430, 650, Y1),   # Living | Storage (opening 385-430)
]

ENTRANCE = (255, 310)  # x-range of front door on the bottom wall

# Device (node) positions from the slide: label, (x, y), marker colour
DEVICES = [
    ("N1", (450, 465), DEVICE_C),   # Living
    ("N2", (145, 127), DEVICE_C),   # Bedroom 1
    ("N3", (95, 408),  DEVICE_C),   # Bedroom 3
    ("N4", (358, 130), DEVICE_C),   # Bedroom 2
    ("N5", (649, 70),  DEVICE_C),   # Kitchen
    ("N6", (741, 397), DEVICE_C),   # Storage
]


def main():
    fig, ax = plt.subplots(figsize=(10, 6.6))
    for name, (x0, y0, x1, y1), sub, fill in ROOMS:
        ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, facecolor=fill, edgecolor="none", zorder=0))
        if name == "Living":
            tx, ty = x0 + 315, y1 - 50
        elif y0 == 243:
            tx, ty = x0 + 8, y1 - 50 if name == "Bedroom 3" else y0 + 22
        else:
            tx, ty = x0 + 8, y0 + 18
        ax.text(tx, ty, name, fontsize=11, fontweight="bold", color="#2d3e50", va="center")
        ax.text(tx, ty + 18, sub, fontsize=9, color="#5b6b7b", va="center")

    for x0, y0, x1, y1 in WALLS:
        ax.plot([x0, x1], [y0, y1], color=WALL_C, lw=WALL, solid_capstyle="projecting", zorder=2)

    ex0, ex1 = ENTRANCE
    ax.plot([ex0, ex1], [Y1, Y1], color="#9a6a1c", lw=WALL + 3, zorder=3)
    ax.text((ex0 + ex1) / 2, Y1 - 18, "Entrance", fontsize=10, fontweight="bold",
            color="#9a6a1c", ha="center", va="center")

    for label, (x, y), colour in DEVICES:
        ax.scatter(x, y, s=520, color=colour, edgecolors="white", linewidths=2, zorder=4)
        ax.text(x, y, label, fontsize=9, fontweight="bold", color="white",
                ha="center", va="center", zorder=5)

    ax.set_xlim(X0 - 10, X1 + 10)
    ax.set_ylim(Y1 + 10, Y0 - 10)   # flip y so it matches the slide
    ax.set_aspect("equal")
    ax.axis("off")
    fig.tight_layout()
    fig.savefig("base_floorplan.png", dpi=200, bbox_inches="tight", facecolor="white")
    fig.savefig("base_floorplan.svg", bbox_inches="tight", facecolor="white")
    print("Saved base_floorplan.png and base_floorplan.svg")


if __name__ == "__main__":
    main()
