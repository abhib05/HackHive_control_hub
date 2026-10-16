"""The organisers' example floor plan (14 m x 9 m), traced from two slide images.

variant="traced": the plan image shared first (81.7 px/m horizontally, 82.0 px/m vertically).
variant="slide5": the "Expected solution" schematic on slide 5 (64.9 px/m); same rooms and nodes,
                  but wider / shifted doors and thicker walls.

Walls are snapped to whole metres (room areas then match the labels exactly); door edges and node
points are converted from pixels and rounded to 0.01 m. Origin = bottom-left outer wall, y up.
"""
from shapely.geometry import LineString, box
from shapely.ops import unary_union

from hub_routing import Plan, Room

# door gaps (start, end) in metres, measured from each image
DOORS = {
    "traced": dict(wall_t=0.08, b1_b3=(1.02, 1.79), b1_l=(3.25, 3.75), k_s=(11.22, 11.73), bath_s=(12.76, 13.27),
                   b3_l=(1.88, 2.55), l_s=(1.88, 2.55), b2_k=(6.65, 7.25), front=(4.16, 5.26)),
    "slide5": dict(wall_t=0.11, b1_b3=(0.99, 1.81), b1_l=(3.20, 3.80), k_s=(11.19, 11.80), bath_s=(12.71, 13.32),
                   b3_l=(1.78, 2.60), l_s=(1.78, 2.60), b2_k=(6.79, 7.61), front=(4.18, 5.00)),
}


def example_plan(variant="traced"):
    d = DOORS[variant]

    def wall(*pts):
        return LineString(pts).buffer(d["wall_t"] / 2, cap_style="flat")

    rooms = [
        Room("Bedroom 1", "bedroom", box(0, 5, 4, 9), (2.35, 7.00), "N2"),
        Room("Bedroom 2", "bedroom", box(4, 5, 8, 9), (6.00, 6.95), "N4"),
        Room("Kitchen", "kitchen", box(8, 5, 12, 9), (10.99, 7.97), "N5"),
        Room("Bathroom", "bathroom", box(12, 5, 14, 9)),
        Room("Bedroom 3", "bedroom", box(0, 0, 3, 5), (1.49, 2.20), "N3"),
        Room("Living", "living", box(3, 0, 11, 5), (7.57, 1.23), "N1"),
        Room("Storage", "storage", box(11, 0, 14, 5), (12.55, 2.39), "N6"),
    ]
    walls = unary_union([
        # outer walls; the front door is left closed because cables never leave the building
        wall((0, 0), (14, 0)), wall((14, 0), (14, 9)), wall((14, 9), (0, 9)), wall((0, 9), (0, 0)),
        # y = 5: Bed1-Bed3, Bed1-Living, Kitchen-Storage, Bathroom-Storage doors
        wall((0, 5), (d["b1_b3"][0], 5)), wall((d["b1_b3"][1], 5), (d["b1_l"][0], 5)),
        wall((d["b1_l"][1], 5), (d["k_s"][0], 5)), wall((d["k_s"][1], 5), (d["bath_s"][0], 5)),
        wall((d["bath_s"][1], 5), (14, 5)),
        # x = 3 (Bedroom 3 | Living) and x = 11 (Living | Storage)
        wall((3, 0), (3, d["b3_l"][0])), wall((3, d["b3_l"][1]), (3, 5)),
        wall((11, 0), (11, d["l_s"][0])), wall((11, d["l_s"][1]), (11, 5)),
        # x = 4 (Bedroom 1 | Bedroom 2) and x = 12 (Kitchen | Bathroom), solid
        wall((4, 5), (4, 9)), wall((12, 5), (12, 9)),
        # x = 8 (Bedroom 2 | Kitchen)
        wall((8, 5), (8, d["b2_k"][0])), wall((8, d["b2_k"][1]), (8, 9)),
    ])
    front_door = LineString([(d["front"][0], 0), (d["front"][1], 0)])
    return Plan(rooms, walls, front_door)
