"""Floor-plan image (PNG / JPG) -> ResPlan-style plan dict, so an image goes through exactly the same
pipeline as a ResPlan plan (resplan_solver.from_resplan -> solve -> JSON + overlay).

    plan, info = plan_from_image("plan.png", area_m2=95.0)

Three ways to tell room types, tried in this order ("mode" in the info dict):
  colour   Every pixel is matched to the nearest colour of a known palette (ResPlan's plot_plan colours, Cable
           Ring Studio's light / dark input view, or a palette the user passes as {"#hex": "type"}). Used when a
           palette explains most pixels.
  legend   OCR (Tesseract) reads the legend: each label naming a room type, wall, door or window gives the colour
           of the swatch next to it; that palette is then used as in colour mode.
  titles   OCR reads the room names written inside the plan ("Master Bedroom", "Kitchen", "Toilet", ...). Walls
           are the thick dark lines; door gaps in them are closed to separate the rooms (the closing becomes a
           door); each enclosed space takes the type of the title inside it (a space with two titles of different
           types, e.g. an open kitchen, is split between them; a space without a title gets no device).
Then, for every mode:
  - the floor plan is the largest connected drawn part; legends, titles and anything outside it are ignored;
  - each connected patch of one room type is one room (polygon, y up); walls, doors, windows become polygons;
  - wall depth = 2 x wall area / wall perimeter; the outline ("inner": the inside face of the exterior walls)
    = the filled plan minus a wall-depth band along its outer edge;
  - scale (deck slide 4) = sqrt(inner area in pixels / floor area in m2), so the user gives the floor area inside
    the outer walls; geometry is rescaled to ResPlan's drawing units (outline's longer side = 256), and the solver
    lays its 0.1 m grid as for any plan.
OCR needs Tesseract (C:\\Program Files\\Tesseract-OCR, or set TESSERACT_CMD) and pytesseract; without them only
colour mode works."""
import json
import os
import re
import shutil
from collections import Counter

import cv2
import numpy as np
import rasterio.features
import shapely
import shapely.affinity
import shapely.geometry
from affine import Affine
from scipy import ndimage
from shapely.geometry import shape
from shapely.ops import unary_union

ROOM_TYPES = ("living", "bedroom", "kitchen", "storage", "bathroom", "stair", "balcony")
PART_TYPES = ("wall", "door", "window", "front_door")
CLASSES = ("background",) + ROOM_TYPES + PART_TYPES

PALETTES = {
    # resplan_utils.CATEGORY_COLORS (plot_plan); background white
    "resplan": {"#ffffff": "background", "#d9d9d9": "living", "#66c2a5": "bedroom", "#fc8d62": "bathroom",
                "#8da0cb": "kitchen", "#e78ac3": "door", "#a6d854": "window", "#ffd92f": "wall",
                "#a63603": "front_door", "#b3b3b3": "balcony", "#ff8c69": "storage", "#9e9ac8": "stair"},
    # Cable Ring Studio input view, light theme (web/style.css)
    "studio_light": {"#ffffff": "background", "#f6f7f9": "background", "#f3e6cc": "living", "#d4e8d9": "bedroom",
                     "#d3e0f2": "kitchen", "#e9dccb": "storage", "#ecd8ea": "bathroom", "#ddd8ee": "stair",
                     "#2b3035": "wall", "#34b3d6": "window", "#f2a6be": "door", "#d1495b": "front_door"},
    # Cable Ring Studio input view, dark theme
    "studio_dark": {"#12161c": "background", "#181d25": "background", "#3b3426": "living", "#22372a": "bedroom",
                    "#22304a": "kitchen", "#3a3127": "storage", "#3a2638": "bathroom", "#2d2a40": "stair",
                    "#c9d0d7": "wall", "#3fc3e6": "window", "#c06f8a": "door", "#f0687a": "front_door"},
}
COLOUR_TOL = 24.0          # max RGB distance for a pixel to count as a palette colour (anti-aliasing beyond it)
PALETTE_OK = 0.85          # a known palette must explain this share of pixels to be used without OCR

# room names (lower case, letters only) -> type; checked in this order, first match wins
KEYWORDS = [
    ("front_door", ("front door", "entrance", "entry door", "main door")),
    ("door", ("door",)),
    ("window", ("window",)),
    ("wall", ("wall",)),
    ("bathroom", ("bath", "toilet", "wc", "washroom", "restroom", "powder", "shower", "lavatory", "ensuite")),
    ("kitchen", ("kitchen", "kitchenette", "cook")),
    ("storage", ("storage", "store", "closet", "pantry", "utility", "laundry", "wardrobe", "dress", "linen")),
    ("stair", ("stair",)),
    ("balcony", ("balcony", "balacony", "terrace", "veranda", "verandah", "sit out", "sitout", "deck", "porch")),
    ("bedroom", ("bed", "bedroom", "master", "guest", "kids", "study", "office", "nursery")),
    ("living", ("living", "lounge", "drawing", "dining", "family", "hall", "lobby", "foyer", "corridor",
                "passage", "great room", "sitting")),
]


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(c):
    return "#%02x%02x%02x" % tuple(int(v) for v in c)


def load_palette(path):
    """User palette: JSON {"#rrggbb": "bedroom", ...} (types: CLASSES; anything else = background)."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {k.lower(): (v if v in CLASSES else "background") for k, v in raw.items()}


def type_of(text):
    """Room / part type named by a label, or None."""
    t = " " + re.sub(r"[^a-z]+", " ", text.lower().replace(".", "")).strip() + " "
    for kind, words in KEYWORDS:
        for w in words:
            if (" " + w) in t if len(w) > 2 else (" " + w + " ") in t:     # word start; "wc" whole word
                return kind
    return None


# --------------------------------------------------------------------------- OCR
def _tesseract_cmd():
    for c in (os.environ.get("TESSERACT_CMD"), shutil.which("tesseract"),
              r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if c and os.path.isfile(c):
            return c
    return None


def ocr_available():
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return _tesseract_cmd() is not None


def ocr_phrases(img, min_conf=40):
    """Text lines in the image: [(text, conf, (x0, y0, x1, y1))] in image pixels. Words on one line that sit
    close together form one phrase ("Master Bedroom", "front door")."""
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd()
    side = max(img.shape[:2])
    f = 3.0 if side < 1500 else 2.0 if side < 2500 else 1.0         # small text reads better enlarged
    big = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC) if f != 1 else img
    d = pytesseract.image_to_data(big, config="--psm 11", output_type=pytesseract.Output.DICT)
    words = []
    for i, txt in enumerate(d["text"]):
        txt = txt.strip()
        if not txt or float(d["conf"][i]) < min_conf:
            continue
        x, y, w, h = (d[k][i] / f for k in ("left", "top", "width", "height"))
        words.append([txt, float(d["conf"][i]), [x, y, x + w, y + h], (d["block_num"][i], d["par_num"][i],
                                                                        d["line_num"][i])])
    phrases = []
    for w in sorted(words, key=lambda w: (w[3], w[2][0])):
        p = phrases[-1] if phrases else None
        if p and p[3] == w[3] and w[2][0] - p[2][2] < 1.2 * (w[2][3] - w[2][1]):
            p[0] += " " + w[0]
            p[1] = min(p[1], w[1])
            p[2] = [min(p[2][0], w[2][0]), min(p[2][1], w[2][1]), max(p[2][2], w[2][2]), max(p[2][3], w[2][3])]
        else:
            phrases.append([w[0], w[1], list(w[2]), w[3]])
    return [(t, c, tuple(b)) for t, c, b, _ in phrases]


def _background(img):
    border = np.concatenate([img[0], img[-1], img[:, 0], img[:, -1]])
    vals, counts = np.unique(border.reshape(-1, 3), axis=0, return_counts=True)
    return vals[np.argmax(counts)]


def legend_palette(img, phrases):
    """Palette read from a legend: for each label naming a type, the colour of the swatch just left of it."""
    bg = _background(img).astype(int)
    H, W = img.shape[:2]
    pal, used = {_hex(bg): "background"}, []
    for text, _, (x0, y0, x1, y1) in phrases:
        kind = type_of(text)
        if kind is None:
            continue
        h = max(4, y1 - y0)
        ys, ye = int(max(0, y0 - 0.3 * h)), int(min(H, y1 + 0.3 * h))
        xs, xe = int(max(0, x0 - 4 * h)), int(max(0, x0 - 2))
        if xe - xs < 3 or ye - ys < 3:
            continue
        reg = img[ys:ye, xs:xe].reshape(-1, 3).astype(int)
        reg = reg[np.abs(reg - bg).sum(1) > 30]                  # not background
        if len(reg) < 0.15 * h * h:
            continue
        vals, counts = np.unique(reg, axis=0, return_counts=True)
        c = vals[np.argmax(counts)]
        if counts.max() < 0.15 * h * h:
            continue
        pal.setdefault(_hex(c), kind)
        used.append((text, kind, _hex(c)))
    return pal, used


# --------------------------------------------------------------------------- scale from room area labels
AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:(sq\.?\s*m(?:t|tr|eters?|etres?)?\b|sqm\b|m\s*[2\u00b2?*])"
                     r"|(sq\.?\s*f(?:ee)?t\b|sft\b|ft\s*[2\u00b2?*]))", re.I)
SQFT = 0.09290304


def area_labels(phrases):
    """Room area labels ("17.0 m2", "17,0 m²", "183 sq ft") read by OCR: [(m2 candidates, x, y, text)].
    A number read without its decimal point ("371" for "37.1") gets the alternatives 37.1 and 3.71."""
    out = []
    for text, _, (x0, y0, x1, y1) in phrases:
        for m in AREA_RE.finditer(text):
            num = m.group(1).replace(",", ".")
            v = float(num) * (SQFT if m.group(3) else 1.0)
            cands = [v]
            if "." not in num and len(num) >= 3:
                cands += [v / 10, v / 100]
            cands = [c for c in cands if 0.5 <= c <= 500]
            if cands:
                out.append((cands, (x0 + x1) / 2, (y0 + y1) / 2, text))
    return out


def scale_from_labels(cls, labels):
    """Pixels per metre from room area labels: each label belongs to the room patch it is written in; per room,
    px/m = sqrt(room pixels / labelled m2). Misreads are rejected: the result is the median of the rooms that
    agree within 15%. Returns (px/m, [(text, room type, m2, px/m)]) or (None, [])."""
    room_ids = [CLASSES.index(t) for t in ROOM_TYPES]
    comp = np.zeros(cls.shape, np.int32)
    k = 0
    for c in room_ids:
        lab, n = ndimage.label(cls == c)
        comp[lab > 0] = lab[lab > 0] + k
        k += n
    if not k:
        return None, []
    sizes = np.bincount(comp.ravel(), minlength=k + 1)
    per = {}                                                # room patch -> labels written in it
    H, W = cls.shape
    for cands, x, y, text in labels:
        xi, yi = int(round(x)), int(round(y))
        if not (0 <= xi < W and 0 <= yi < H):
            continue
        win = comp[max(0, yi - 6):yi + 7, max(0, xi - 6):xi + 7]   # the label's own pixels may not be room
        ids = win[win > 0]
        if len(ids):
            per.setdefault(int(np.bincount(ids).argmax()), []).append((cands, text))
    rows = []
    for r, labs in per.items():
        if len(labs) != 1:                                  # two area labels in one patch: ambiguous, skip
            continue
        cands, text = labs[0]
        rows.append((text, CLASSES[int(cls[comp == r][0])], [np.sqrt(sizes[r] / c) for c in cands], cands))
    if not rows:
        return None, []
    first = float(np.median([v[0] for _, _, v, _ in rows]))
    picked = []
    for text, kind, vals, cands in rows:                    # each label: the reading that agrees best
        j = int(np.argmin([abs(v - first) for v in vals]))
        picked.append((text, kind, cands[j], vals[j]))
    med = float(np.median([v for *_, v in picked]))
    good = [r for r in picked if abs(r[3] - med) <= 0.15 * med]
    if not good:
        return None, []
    return float(np.median([v for *_, v in good])), [(t, k_, round(float(m2), 2), round(float(v), 3))
                                                     for t, k_, m2, v in good]


# --------------------------------------------------------------------------- pixel classes
def _classify(img, palette):
    """Per-pixel class index (into CLASSES) and RGB distance to the matched colour."""
    cols = np.array([_rgb(c) for c in palette], dtype=np.float32)
    cls = np.array([CLASSES.index(t) for t in palette.values()])
    flat = img.reshape(-1, 3).astype(np.float32)
    best_d = np.full(len(flat), np.inf, dtype=np.float32)
    best_c = np.zeros(len(flat), dtype=np.int16)
    for c, k in zip(cols, cls):
        d = np.sqrt(((flat - c) ** 2).sum(1))
        m = d < best_d
        best_d[m], best_c[m] = d[m], k
    return best_c.reshape(img.shape[:2]), best_d.reshape(img.shape[:2])


def _palette_score(img, pal, rooms_needed=False):
    """Share of pixels a palette explains. With rooms_needed, 0 unless room colours make up at least 20% of the
    drawn (non-background) pixels and 3% of the image: a black-and-white drawing matches black walls on white
    (and grey anti-aliased text matches grey room colours), but has no coloured rooms."""
    c, d = _classify(img[::4, ::4], pal)
    ok = d < COLOUR_TOL
    if rooms_needed:
        drawn = ok & (c != CLASSES.index("background"))
        rooms = drawn & np.isin(c, [CLASSES.index(t) for t in ROOM_TYPES])
        if rooms.sum() < 0.2 * max(drawn.sum(), 1) or rooms.sum() < 0.03 * c.size:
            return 0.0
    return float(ok.mean())


def _classes_from_palette(img, pal):
    cls, dist = _classify(img, pal)
    known = dist < COLOUR_TOL
    if not known.all():               # anti-aliased / unknown pixels take the class of the nearest known pixel
        idx = ndimage.distance_transform_edt(~known, return_distances=False, return_indices=True)
        cls = cls[idx[0], idx[1]]
    # small wall-coloured specks apart from the wall network (e.g. room labels in a screenshot, whose text has
    # the wall colour) take the class of the nearest non-wall pixel, i.e. the room they are written in
    wall = CLASSES.index("wall")
    lab, n = ndimage.label(cls == wall)
    if n > 1:
        sizes = ndimage.sum(np.ones_like(lab), lab, index=np.arange(1, n + 1))
        small = np.isin(lab, 1 + np.nonzero(sizes < 0.02 * sizes.max())[0])
        if small.any():
            donor = cls != wall
            idx = ndimage.distance_transform_edt(~donor, return_distances=False, return_indices=True)
            cls = np.where(small, cls[idx[0], idx[1]], cls)
    return cls, float(known.mean())


def _classes_from_titles(img, phrases, area_m2=None, px_per_m=None):
    """Rooms enclosed by thick dark walls, typed by the titles written inside them."""
    bg = _background(img).astype(int)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(int)
    off_bg = np.abs(img.astype(int) - bg).sum(2) > 60
    dark = (gray < 110) & off_bg
    lines = (gray < 200) & off_bg                                    # every drawn line, thin ones too
    med_h = np.median([b[3] - b[1] for _, _, b in phrases]) if phrases else 0
    for _, conf, (x0, y0, x1, y1) in phrases:                       # erase text (titles, areas, labels) ...
        if conf < 60 or y1 - y0 > 2.5 * med_h:                      # ... but not misreads of wall corners
            continue
        sl = np.s_[int(max(0, y0 - 2)):int(y1 + 3), int(max(0, x0 - 2)):int(x1 + 3)]
        dark[sl] = False
        lines[sl] = False
    walls = ndimage.binary_opening(dark, np.ones((3, 3)))            # drop thin lines (door swings, hatching)
    lab, n = ndimage.label(walls)
    if n == 0:
        raise ValueError("no walls found (thick dark lines) and no known colours")
    sizes = ndimage.sum(np.ones_like(lab), lab, index=np.arange(1, n + 1))
    walls = np.isin(lab, 1 + np.nonzero(sizes >= 0.002 * sizes.max())[0])
    ys, xs = np.nonzero(walls)
    wpx = walls.sum()
    perim = (walls ^ ndimage.binary_erosion(walls)).sum()
    wd = max(2.0, 2 * wpx / max(perim, 1))                           # wall thickness in pixels
    if px_per_m:
        scale0 = px_per_m
    elif area_m2:
        scale0 = np.sqrt((np.ptp(xs) + 1) * (np.ptp(ys) + 1) / area_m2)  # >= true px/m (bounding box >= outline)
    else:
        scale0 = wd / 0.23          # provisional, only to size door gaps until the area labels give the scale

    lines = lines | walls

    def plugs(base, k):
        """Door / window gaps up to k px in a wall: a run of empty pixels between two pieces of `base` along a
        row (or column) is closed only if at least one end touches a wall running along that row (column).
        A door next to a cross wall is closed; a corridor between two parallel walls is not."""
        b8 = base.astype(np.uint8)
        n = int(2 * wd) | 1
        out = np.zeros(base.shape, bool)
        for axis, kern, run in ((1, (1, int(k) | 1), [[0, 0, 0], [1, 1, 1], [0, 0, 0]]),
                                (0, (int(k) | 1, 1), [[0, 1, 0], [0, 1, 0], [0, 1, 0]])):
            along = cv2.morphologyEx(b8, cv2.MORPH_OPEN, np.ones((1, n) if axis else (n, 1), np.uint8)) > 0
            cand = (cv2.morphologyEx(b8, cv2.MORPH_CLOSE, np.ones(kern, np.uint8)) > 0) & ~base
            lab, _ = ndimage.label(cand, run)
            touch = cand & (np.roll(along, 1, axis) | np.roll(along, -1, axis))
            good = np.unique(lab[touch])
            out |= np.isin(lab, good[good > 0])
        return out

    def outline(px_per_m):
        """Inside face of the outer walls: gaps up to 3 m in the outer walls closed (thin window outlines count
        as wall here), filled, minus a wall-depth band. If the ring still has an opening, a round closing
        (radius 2 m) bridges it."""
        gaps = plugs(lines, 3.0 * px_per_m)
        solid = lines | gaps
        filled = ndimage.binary_fill_holes(solid)
        if filled.sum() < 1.5 * solid.sum():                        # ring not closed: bridge the opening
            r = int(2.0 * px_per_m)
            ring = cv2.morphologyEx(solid.astype(np.uint8), cv2.MORPH_CLOSE,
                                    cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))) > 0
            filled = ndimage.binary_fill_holes(ring | solid)
        return filled & (ndimage.distance_transform_edt(filled) > wd * 0.98), gaps & ~walls

    inner, _ = outline(scale0)
    scale = px_per_m or (np.sqrt(max(inner.sum(), 1) / area_m2) if area_m2 else scale0)
    inner, gaps = outline(scale)
    door = plugs(walls, 1.4 * scale) & inner                        # inner door gaps up to about 1.4 m wide
    window = gaps & ~inner                                          # gaps in the outer walls: obstacles
    closed = walls | door
    space, n = ndimage.label(inner & ~closed)
    # titles inside the plan, with their type and centre
    seeds = []
    for text, _, (x0, y0, x1, y1) in phrases:
        kind = type_of(text)
        cx, cy = int((x0 + x1) / 2), int((y0 + y1) / 2)
        if kind in ROOM_TYPES and 0 <= cy < space.shape[0] and 0 <= cx < space.shape[1] and space[cy, cx]:
            seeds.append((space[cy, cx], kind, cx, cy, text))
    # spaces of at least 1 m2 that got no title from the whole-page OCR: read each one on its own
    have = {sd[0] for sd in seeds}
    sizes = ndimage.sum(np.ones_like(space), space, index=np.arange(1, n + 1))
    for r in range(1, n + 1):
        if r in have or sizes[r - 1] < scale ** 2:
            continue
        ys_, xs_ = np.nonzero(space == r)
        crop = img[ys_.min():ys_.max() + 1, xs_.min():xs_.max() + 1].copy()
        crop[space[ys_.min():ys_.max() + 1, xs_.min():xs_.max() + 1] != r] = bg
        for text, _, (x0, y0, x1, y1) in ocr_phrases(crop, min_conf=50):
            kind = type_of(text)
            if kind in ROOM_TYPES:
                seeds.append((r, kind, int(xs_.min() + (x0 + x1) / 2), int(ys_.min() + (y0 + y1) / 2), text))
                break
    cls = np.full(img.shape[:2], CLASSES.index("background"), dtype=np.int16)
    cls[walls] = CLASSES.index("wall")
    cls[window] = CLASSES.index("window")
    cls[door] = CLASSES.index("door")
    untitled = 0
    for r in range(1, n + 1):
        here = [s for s in seeds if s[0] == r]
        m = space == r
        if not here:
            untitled += 1
            continue
        if len({s[1] for s in here}) == 1:
            cls[m] = CLASSES.index(here[0][1])
            continue
        # several titles of different types (open plan): grow every title through the space at once, so each
        # pixel goes to the title it can reach first (walking distance, not straight-line distance)
        ys_, xs_ = np.nonzero(m)
        y0, y1, x0, x1 = ys_.min(), ys_.max() + 1, xs_.min(), xs_.max() + 1
        mm = m[y0:y1, x0:x1]
        lab = np.zeros(mm.shape, np.int32)
        for i, s in enumerate(here, 1):
            lab[s[3] - y0, s[2] - x0] = i
        while True:
            grown = ndimage.grey_dilation(lab, size=(3, 3))
            new = (lab == 0) & mm & (grown > 0)
            if not new.any():
                break
            lab[new] = grown[new]
        kinds = np.array([0] + [CLASSES.index(s[1]) for s in here])
        sub = cls[y0:y1, x0:x1]
        sub[mm & (lab > 0)] = kinds[lab[mm & (lab > 0)]]
    extra = dict(titles=[(s[4], s[1]) for s in seeds], spaces=int(n), spaces_without_title=untitled)
    return cls, extra


# --------------------------------------------------------------------------- polygons
def _polys(mask, transform, min_px=4):
    """Pixel mask -> list of shapely polygons (4-connected parts, exact pixel edges, y up)."""
    out = []
    for geom, val in rasterio.features.shapes(mask.astype(np.uint8), mask=mask, connectivity=4, transform=transform):
        if val:
            p = shape(geom)
            if p.area >= min_px:
                out.append(p.simplify(0.35, preserve_topology=True))
    return out


def _plan_from_classes(cls, area_m2, plan_id, px_per_m=None):
    H, W = cls.shape
    bg = CLASSES.index("background")
    drawn = cls != bg
    # the plan = the largest connected drawn part (legend swatches, titles etc. are dropped)
    lab, n = ndimage.label(drawn)
    if n == 0:
        raise ValueError("no floor plan found in the image (every pixel matched the background colour)")
    sizes = ndimage.sum(np.ones_like(lab), lab, index=np.arange(1, n + 1))
    main = lab == (1 + int(np.argmax(sizes)))
    dropped = int(drawn.sum() - main.sum())
    cls = np.where(main, cls, bg)

    T = Affine(1, 0, 0, 0, -1, H)                         # pixel (col, row) -> x right, y up
    masks = {t: cls == CLASSES.index(t) for t in CLASSES[1:]}
    wall_polys = _polys(masks["wall"], T)
    if wall_polys:                                        # drop specks (e.g. dark room-label text read as wall)
        big = max(p.area for p in wall_polys)
        wall_polys = [p for p in wall_polys if p.area >= 0.002 * big]
    if not wall_polys:
        raise ValueError("no walls found: the image colours do not match a known palette (pass --palette)")
    walls = unary_union(wall_polys)
    wd = 2 * walls.area / max(walls.length, 1e-9)         # thin strips: area = L t, perimeter ~ 2 L
    # outline: filled plan (without balconies) minus a wall-depth band along its outer edge
    solid = main & ~masks["balcony"]
    filled = ndimage.binary_fill_holes(solid)
    depth = ndimage.distance_transform_edt(filled)
    inner_mask = depth > wd * 0.98
    inner_parts = _polys(inner_mask, T, min_px=max(4, wd * wd))
    if not inner_parts:
        raise ValueError("could not find the inside of the outer walls")
    inner = unary_union(inner_parts)
    if area_m2:
        scale_px = float(np.sqrt(inner.area / area_m2))   # deck slide 4: pixels per metre
    else:                                                 # scale read from the room area labels
        scale_px = float(px_per_m)
        area_m2 = float(inner.area / scale_px ** 2)
    # same drawing units as ResPlan: the outline's longer side = 256 units (keeps ResPlan's sanity checks valid)
    x1, y1, x2, y2 = inner.bounds
    k = 256.0 / max(x2 - x1, y2 - y1)
    to_units = lambda g: shapely.affinity.scale(g, k, k, origin=(0, 0))

    plan = {"id": None, "plan_id": plan_id, "area": float(area_m2), "inner": to_units(inner),
            "wall": to_units(walls), "wall_depth": float(wd * k)}
    noise = 0
    for t in ("door", "window", "front_door") + ROOM_TYPES:
        ps = _polys(masks[t], T)
        if t in ROOM_TYPES:          # image noise: specks < 0.5 m2, or room pieces mostly outside the outline
            keep = [p for p in ps if p.area >= 0.5 * scale_px ** 2
                    and (t == "balcony" or p.intersection(inner).area >= 0.5 * p.area)]
            noise += len(ps) - len(keep)
            ps = keep
        plan[t] = to_units(unary_union(ps)) if ps else shapely.geometry.Polygon()
    info = dict(pixels_outside_plan=dropped, room_specks_dropped=noise, wall_depth_px=round(wd, 2),
                scale_px_per_m=round(scale_px, 3), wall_depth_m=round(wd / scale_px, 3), area_m2=float(area_m2),
                rooms_found={t: len(getattr(plan[t], "geoms", [plan[t]])) if not plan[t].is_empty else 0
                             for t in ROOM_TYPES})
    return plan, info


def plan_from_image(path, area_m2=None, palette=None, plan_id=None, mode="auto"):
    """Read a floor-plan image. mode: "auto" (colour, then legend, then titles), "colour", "legend" or "titles".
    area_m2 = floor area inside the outer walls; if None, the scale is read from room area labels written on the
    plan ("17.0 m2"), which needs OCR. Returns (ResPlan-style plan dict, info dict)."""
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"cannot read image {path}")
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    H, W = img.shape[:2]
    plan_id = plan_id or "IMG_" + os.path.splitext(os.path.basename(path))[0]
    info = dict(source=os.path.abspath(path), image_px=[W, H])
    warn = []

    if palette is not None:
        mode, pname, pal = "colour", "user", palette
    else:
        scores = {name: _palette_score(img, pal, rooms_needed=True) for name, pal in PALETTES.items()}
        pname = max(scores, key=scores.get)
        pal = PALETTES[pname]
        if mode == "auto" and scores[pname] >= PALETTE_OK:
            mode = "colour"
    phrases = None
    if not area_m2 and not ocr_available():
        raise ValueError("no floor area given, and reading the room area labels needs Tesseract OCR: "
                         "give the floor area in m2")
    if mode in ("auto", "legend", "titles") or not area_m2:
        if not ocr_available():
            if mode != "auto":
                raise ValueError("reading titles or a legend needs Tesseract OCR and pytesseract")
            mode = "colour"
            warn.append("no known palette fits and OCR is not installed: read with the closest palette")
        else:
            phrases = ocr_phrases(img)
            info["text_found"] = [t for t, _, _ in phrases][:60]
    if mode in ("auto", "legend"):
        lp, used = legend_palette(img, phrases)
        rooms_in_legend = {k for _, k, _ in used if k in ROOM_TYPES}
        if "wall" in lp.values() and rooms_in_legend and (mode == "legend"
                                                          or _palette_score(img, lp, rooms_needed=True) >= 0.7):
            mode, pname, pal = "legend", "legend", lp
            info["legend"] = [{"label": t, "type": k, "colour": c} for t, k, c in used]
        elif mode == "legend":
            raise ValueError("no usable legend found (labels naming room types and walls, each with a colour swatch)")
        else:
            mode = "titles"

    px_per_m, used_labels = None, []
    if mode == "titles":
        cls, extra = _classes_from_titles(img, phrases, area_m2)
        if not area_m2:             # scale from the area labels, then read the rooms again at that scale
            px_per_m, used_labels = scale_from_labels(cls, area_labels(phrases))
            if px_per_m:
                cls, extra = _classes_from_titles(img, phrases, px_per_m=px_per_m)
        info.update(extra)
        matched = None
        if not extra["titles"]:
            raise ValueError("no room titles found inside the plan (and no known colours or legend)")
        if extra["spaces_without_title"]:
            warn.append(f"{extra['spaces_without_title']} enclosed spaces have no title; they get no device "
                        "(corridors, shafts, ...)")
    else:
        cls, matched = _classes_from_palette(img, pal)
        if matched < 0.9:
            warn.append(f"only {matched:.0%} of pixels match the {pname} palette; room types may be wrong "
                        "(pass --palette with the image's colours)")
    if not area_m2:
        if mode != "titles":
            px_per_m, used_labels = scale_from_labels(cls, area_labels(phrases))
        if not px_per_m:
            raise ValueError("no floor area given and no room area labels (e.g. '17.0 m2') could be read: "
                             "give the floor area in m2")
        info["scale_source"] = f"room area labels ({len(used_labels)} agree)"
        info["area_labels"] = [{"text": t, "room": k, "m2": a, "px_per_m": v} for t, k, a, v in used_labels]
        if len(used_labels) == 1:
            warn.append("the scale comes from a single room area label")
    else:
        info["scale_source"] = "floor area given"
    plan, binfo = _plan_from_classes(cls, area_m2, plan_id, px_per_m)
    info.update(mode=mode, palette=pname if mode != "titles" else None,
                pixels_matched=None if matched is None else round(matched, 4), **binfo)
    if warn:
        info["warning"] = "; ".join(warn)
    return plan, info
