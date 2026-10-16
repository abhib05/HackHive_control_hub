"""Hub placement + closed-ring cable routing on a floor plan (organisers' deck, slides 2-7).

Hard rules (a design is invalid if any is broken):
  1 coverage   every eligible room (living, bedroom, kitchen, storage) and its node -> exactly one hub
  2 capacity   per hub: <= 5 nodes and <= 100 m2 of eligible-room area (other rooms not counted)
  3 zone       a hub's rooms are connected through doors / open passages only
  4 ring       one closed ring per hub: H -> every assigned node once -> H, no branches; a wire never
               runs through another hub or node point
  5 free space cable inside the inner boundary, outside walls and windows, horizontal/vertical only,
               between rooms only through door / open-passage gaps. A ring may pass through any room.
  6 hub point  inside an eligible room of its own zone, >= 0.5 m from walls, not on a node point

Grid: a line every 0.1 m plus one line through every node coordinate, so the fixed node points are
exact grid vertices. Hubs and wires sit on grid vertices / grid lines. Parallel wires on different
lines do not overlap.

Ranking: fewest hubs, then J = L + 5X + O + 0.5B, then smaller assigned-area gap between hubs.
  L  total wire length, returns included
  O  sum over grid edges of (number of wire passes - 1) x edge length
  B  every change of direction along a ring, at hubs and nodes too (a reversal counts once)
  X  every grid point other than a hub or node where two wire passes meet without sharing an edge
     (crossings and touches), counted once per point

Pipeline:
  stage 1  groupings of eligible rooms (set partitions) that satisfy rules 1-3, fewest hubs only
  stage 2  per group: exact ring cost L + 0.5B at every legal hub vertex (best visiting order, turns
           at hub/nodes included, other wires ignored) -> best hub; this is a lower bound on J
  stage 3  route each ring leg by leg with overlap/crossing penalties for every visiting order (or
           optionally select a Held-Karp TSP order, or keep K leg paths in a beam); orders and
           groupings whose lower bound cannot beat the best J found are skipped; then reroute
  stage 4  exact J per routed grouping, pick the lowest (then smallest area gap), validate rules 1-6
"""
import itertools
import heapq
import math
import time
from collections import Counter
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

W_CROSS, W_OVERLAP, W_BEND = 5.0, 1.0, 0.5
DEVICE_TYPES = ("living", "bedroom", "kitchen", "storage")
MAX_DEVICES, MAX_AREA = 5, 100.0
HUB_WALL_CLEARANCE = 0.5
BLOCKED = 1e6          # cost of running through another hub or node point (rule 4)
LB_SLACK = 1e-3        # float32 distance tables: keep pruning on the safe side
GUIDED = True          # leg searches use the device distance tables as an A* potential (exact, faster)
ROUTE_SLACK = 2.0      # guided search: first cost limit = unhindered leg cost + this (re-run if exceeded)
H_FAR = 1e5            # potential of states that cannot reach the target at all
IMPROVE_TOP, IMPROVE_MARGIN, IMPROVE_TIME_S = 3, 2.5, 30.0   # rip-up and reroute: designs, J window, time cap
HUB_SEARCH = "exhaustive"   # stage-2 hub point: "exhaustive", "adaptive", "greedy", or "random_restart"
HUB_RESTARTS = 8       # random_restart: total starts including the centroid, capped by legal points
HUB_PRUNE = True       # exhaustive, but skip vertices whose lower bound cannot reach the best (same result)
HUB_MOVES = True      # rip-up and reroute also moves hubs (greedy, see _hub_moves)
NO_RETRACE = True     # user decision 2026-10-17: a ring never runs back along its own wire (no edge used twice
                      # by one ring; a one-device ring is a small rectangle, a one-door room is entered and left
                      # on two different lines). Overlap with other rings stays allowed and is priced as before.
HUB_TRIES, HUB_SPACING = 12, 0.25   # hub points tried per hub and round; tried points kept >= this far apart (m)
RING_MOVES = False     # rip-up and reroute also re-routes whole rings in other visiting orders
RING_TRIES, RING_POLISH = 4, 2       # orders tried per ring and round; leg/pair rounds to polish each
SEG_MOVES = False      # rip-up and reroute also re-routes 3 consecutive legs (middle one first)
KICK_MOVES = False     # rip-up and reroute also pushes each leg off its own line, then polishes the ring
RING_ROUTER = "sequential"  # "sequential" or "beam" (bounded Yen-style leg paths)
LEG_K_PATHS, RING_BEAM_WIDTH = 2, 2
LEG_SPUR_LIMIT = 6    # bound Yen deviations per leg; 0 checks every vertex (exact K shortest)
ORDER_SEARCH = "all"  # "all" (current) or "tsp" (Held-Karp tour, then route its legs)
TSP_DIRECTIONS = 1    # 2 also routes the reverse tour, which can differ under sequential routing

# direction codes: 0 = +x (E), 1 = +y (N), 2 = -x (W), 3 = -y (S)
OPP = np.array([2, 3, 0, 1])
BENDS = 1 - np.eye(4, dtype=int)           # any change of direction is one bend (slide 6: a reversal counts once)
TURN = W_BEND * BENDS
# a wire passing a vertex uses two of its four edges: the pass type. Two passes cross (or touch) when
# they share no edge, i.e. when their types are complementary: E-W / N-S, E-N / W-S, E-S / W-N.
PASS_TYPE = np.array([[-1, 2, 0, 4], [2, -1, 5, 1], [0, 5, -1, 3], [4, 1, 3, -1]])   # [edge dir a, edge dir b]
COMP = np.array([1, 0, 3, 2, 5, 4])


def _group(keys, n):
    """(order, ptr): indices sorted by key, and where each key 0..n-1 starts in that order."""
    order = np.argsort(keys, kind="stable")
    return order, np.searchsorted(keys[order], np.arange(n + 1))


# --------------------------------------------------------------------------- plan
@dataclass
class Room:
    name: str
    type: str
    poly: object
    device: tuple = None        # (x, y) in metres; eligible rooms without one get a representative point
    device_label: str = None

    @property
    def area(self):
        return self.poly.area


class Plan:
    """Rooms (metres), obstacles (walls, plus windows for ResPlan) and room-to-room passages.

    zone_mode (rule 3): "own" = a zone's eligible rooms connect through each other only;
    "excluded" = they may also connect through rooms without a node (bathroom, stair, ...);
    "any" = through any room."""

    def __init__(self, rooms, walls, front_door=None, min_door=0.3, outline=None, neighbours=None,
                 zone_mode="excluded", oversize_ok=False, area_cap=None):
        self.rooms = {r.name: r for r in rooms}
        self.walls = walls
        self.front_door = front_door
        self.outline = outline if outline is not None else unary_union([r.poly for r in rooms])
        self.zone_mode = zone_mode
        self.oversize_ok = oversize_ok        # allow a single room > 100 m2 as its own zone (flagged)
        self.area_cap = area_cap              # a room counts at most this much towards its hub (None = full area)
        for r in rooms:
            if r.type in DEVICE_TYPES and r.device is None:
                p = r.poly.representative_point()
                r.device = (p.x, p.y)
        if neighbours is None:
            # a door = part of a shared room boundary that is not covered by wall
            neighbours = {n: set() for n in self.rooms}
            for a, b in itertools.combinations(self.rooms, 2):
                shared = self.rooms[a].poly.boundary.intersection(self.rooms[b].poly.boundary)
                if shared.length > 0 and shared.difference(walls).length >= min_door:
                    neighbours[a].add(b)
                    neighbours[b].add(a)
        self.neighbours = {n: set(neighbours.get(n, ())) for n in self.rooms}

    def device_rooms(self):
        return [n for n, r in self.rooms.items() if r.type in DEVICE_TYPES]

    def room_area(self, r):
        """Area a room adds to its hub (rule 2). With area_cap (user decision 2026-10-16), a hub serving
        a room larger than the cap serves only the cap's worth of it."""
        a = self.rooms[r].area
        return a if self.area_cap is None else min(a, self.area_cap)

    def area(self, group):
        return sum(self.room_area(r) for r in group)

    def territory(self, group):
        """Group rooms + device-less rooms reachable from them through device-less rooms."""
        terr, stack = set(group), list(group)
        while stack:
            for nb in self.neighbours[stack.pop()]:
                if nb not in terr and self.rooms[nb].type not in DEVICE_TYPES:
                    terr.add(nb)
                    stack.append(nb)
        return terr

    def is_connected(self, group):
        if len(group) <= 1:
            return True
        allowed = {"own": set(group), "any": set(self.rooms)}.get(self.zone_mode) or self.territory(group)
        start = next(iter(group))
        seen, stack = {start}, [start]
        while stack:
            for nb in self.neighbours[stack.pop()]:
                if nb in allowed and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        return set(group) <= seen

    def block_ok(self, group):
        if len(group) > MAX_DEVICES or not self.is_connected(group):
            return False
        return self.area(group) <= MAX_AREA + 1e-9 or (self.oversize_ok and len(group) == 1)


# --------------------------------------------------------------------------- grid
class Grid:
    """Routing grid. Vertex n = j * nx + i at (xs[i], ys[j]). Lines every h metres plus extra lines
    (node coordinates); regular lines closer than merge_tol to an extra line are dropped."""

    def __init__(self, plan, h=0.1, clearance=0.01, extra_x=(), extra_y=(), merge_tol=0.005, soft_x=(), soft_y=()):
        self.h = h
        minx, miny, maxx, maxy = plan.outline.bounds
        self.xs = self._lines(minx, maxx, h, extra_x, merge_tol, soft=soft_x)
        self.ys = self._lines(miny, maxy, h, extra_y, merge_tol, soft=soft_y)
        self.nx, self.ny = len(self.xs), len(self.ys)
        nx, ny, N = self.nx, self.ny, self.nx * self.ny
        self.N = N
        X, Y = np.meshgrid(self.xs, self.ys)
        self.X, self.Y = X.ravel(), Y.ravel()

        obst, outline = plan.walls, plan.outline
        shapely.prepare(outline)
        pts = shapely.points(self.X, self.Y)
        inside = shapely.covers(outline, pts)
        self.wall_dist = np.zeros(N)                      # distance to walls / windows (0 outside the outline)
        self.wall_dist[inside] = shapely.distance(obst, pts[inside])
        self.free = inside & (self.wall_dist >= clearance)
        self.room_inner = {}
        for n, r in plan.rooms.items():                   # only points in the room's bounding box are tested
            x1, y1, x2, y2 = r.poly.bounds
            box = np.nonzero((self.X >= x1) & (self.X <= x2) & (self.Y >= y1) & (self.Y <= y2))[0]
            m = np.zeros(N, bool)
            m[box] = shapely.contains_properly(r.poly, pts[box])
            self.room_inner[n] = m

        # undirected edges: id n = (n, n+1) horizontal, id N+n = (n, n+nx) vertical
        i_idx, j_idx = np.arange(N) % nx, np.arange(N) // nx
        self.elen = np.zeros(2 * N)
        self.elen[:N][i_idx < nx - 1] = np.diff(self.xs)[i_idx[i_idx < nx - 1]]
        self.elen[N:][j_idx < ny - 1] = np.diff(self.ys)[j_idx[j_idx < ny - 1]]
        blocker = obst.buffer(clearance * 0.999)
        shapely.prepare(blocker)
        self.edge_ok = np.zeros(2 * N, bool)
        hs = np.nonzero((i_idx < nx - 1) & self.free & np.roll(self.free, -1))[0]
        vs = np.nonzero((j_idx < ny - 1) & self.free & np.roll(self.free, -nx))[0]
        for ids, step, off in ((hs, 1, 0), (vs, nx, N)):
            if len(ids) == 0:
                continue
            segs =shapely.linestrings(np.stack([np.stack([self.X[ids], self.Y[ids]], 1),
                                                 np.stack([self.X[ids + step], self.Y[ids + step]], 1)], 1))
            ok = shapely.covers(outline, segs) & ~shapely.intersects(blocker, segs)
            self.edge_ok[off + ids[ok]] = True

        h_ids = np.nonzero(self.edge_ok[:N])[0]
        v_ids = np.nonzero(self.edge_ok[N:])[0]
        self.m_src = np.concatenate([h_ids, h_ids + 1, v_ids, v_ids + nx])
        self.m_dst = np.concatenate([h_ids + 1, h_ids, v_ids + nx, v_ids])
        self.m_dir = np.concatenate([np.full(len(h_ids), 0), np.full(len(h_ids), 2),
                                     np.full(len(v_ids), 1), np.full(len(v_ids), 3)])
        self.m_eid = np.concatenate([h_ids, h_ids, N + v_ids, N + v_ids])
        self._template()
        self.G0 = self.state_graph()
        self.counters = Counter()             # diagnostics only: searches run, moves tried / kept

    @staticmethod
    def _merge(xs, same):
        merged = []
        for x in np.sort(np.asarray(list(xs), float)):
            if merged and x - merged[-1][0] <= same:
                merged[-1][1] = x
            else:
                merged.append([x, x])
        return np.array([(a + b) / 2 for a, b in merged])

    @staticmethod
    def _lines(lo, hi, h, extra, tol, same=0.0018, soft=()):
        """Regular lines every h, plus the extra coordinates (device points). Extra coordinates closer
        together than `same` become one line at their midpoint (each point then lies < 1 mm from it, the
        checker's tolerance), so no two lines are within 1 mm of each other. Soft coordinates (corner
        lines) are optional: dropped within tol of an extra line (devices are never moved by them),
        merged among themselves like the extra ones, and inside the [lo, hi] range only."""
        reg = np.round(np.arange(math.floor(lo / h), math.ceil(hi / h) + 1) * h, 6)
        extra = Grid._merge(extra, same)
        soft = np.asarray(list(soft), float)
        soft = soft[(soft >= lo) & (soft <= hi)]
        if len(soft) and len(extra):
            soft = soft[np.abs(soft[:, None] - extra[None, :]).min(1) > tol]
        fixed = np.concatenate([extra, Grid._merge(soft, same)])
        if len(fixed):
            reg = np.concatenate([reg[np.abs(reg[:, None] - fixed[None, :]).min(1) > tol], fixed])
        return np.unique(reg)

    # state = 4 * vertex + direction of the last move; cost = length + 0.5 per turn.
    # The CSR structure is built once; each call only fills in the weights.
    def _template(self):
        R, C, A, T = [], [], [], []
        for din in range(4):
            ok = np.nonzero(self.m_dir != OPP[din])[0]           # no reversal mid-wire
            R.append(self.m_src[ok] * 4 + din)
            C.append(self.m_dst[ok] * 4 + self.m_dir[ok])
            A.append(ok)
            T.append(TURN[din, self.m_dir[ok]])
        R, C, A, T = map(np.concatenate, (R, C, A, T))
        order = np.lexsort((C, R))
        # one extra state S = 4N: the start of every search (see route); its four arcs (to the four
        # "arrived with direction d" states of the start vertex) are filled in per call
        self._indices = np.concatenate([C[order], [0, 1, 2, 3]]).astype(np.int32)
        cnt = np.bincount(R, minlength=4 * self.N)
        self._indptr = np.concatenate([[0], np.cumsum(cnt), [cnt.sum() + 4]]).astype(np.int32)
        self._rows = R[order]
        self._arc, self._turn = A[order], T[order]
        # each arc leaves vertex _esrc; together with the direction the wire arrived with, it fixes the
        # pass type at that vertex (which two of its edges the wire uses), see pass_masks
        din = R[order] % 4
        self._esrc = self.m_src[self._arc]
        self._ecomp = (1 << COMP[PASS_TYPE[OPP[din], self.m_dir[self._arc]]]).astype(np.uint8)
        self.tables = {}                      # vertex -> first_move_dist table (devices, see prepare)
        self._h = {}                          # vertex -> A* potential for searches towards it
        self._base = {}                       # target vertex (or None) -> arc weights without penalties
        # arcs grouped by the edge they use, the vertex they enter and the vertex they leave (CSR
        # style), so penalties near existing wires are added to a copy of the base weights sparsely
        N = self.N
        self._by_edge = _group(self.m_eid[self._arc], 2 * N)
        self._by_dst = _group(self.m_dst[self._arc], N)
        self._by_src = _group(self._esrc, N)
        S = 4 * N + 1
        self._scratch = csr_matrix((np.zeros(len(self._indices)), self._indices.copy(), self._indptr), shape=(S, S))

    def arcs_of(self, groups, keys):
        """All arc indices whose group key (edge or vertex, see _template) is in keys."""
        order, ptr = groups
        keys = np.asarray(keys, dtype=np.int64)
        if len(keys) == 0:
            return keys
        cnt = ptr[keys + 1] - ptr[keys]
        start = np.repeat(ptr[keys] - np.concatenate([[0], np.cumsum(cnt)[:-1]]), cnt)
        return order[start + np.arange(cnt.sum())]

    def base_weights(self, b=None):
        """Arc weights without penalties (length + 0.5/turn), reduced by b's A* potential if b is given."""
        if b not in self._base:
            w = self.elen[self.m_eid][self._arc] + self._turn
            if b is not None:
                h = self.potential(b)
                w = np.maximum(w + h[self._indices[:-4]] - h[self._rows], 0.0)
            if len(self._base) > 24:
                self._base.pop(next(iter(self._base)))
            self._base[b] = w
        return self._base[b]

    def state_graph(self, edge_pen=None, node_pen=None, arc_pen=None, h=None, scratch=False):
        """State graph with these penalties. h: A* potential (see potential); the arc weights are then
        reduced, w + h(head) - h(tail) >= 0, so a Dijkstra with a cost limit only visits states on
        near-cheapest paths towards h's target (exact, see route). scratch: fill the grid's one reusable
        graph instead of a new one (valid until the next scratch call)."""
        w = self.elen[self.m_eid]
        if edge_pen is not None:
            w = w + edge_pen[self.m_eid]
        if node_pen is not None:
            w = w + node_pen[self.m_dst]
        data = w[self._arc] + self._turn
        if arc_pen is not None:
            data = data + arc_pen
        if h is not None:
            data = np.maximum(data + h[self._indices[:-4]] - h[self._rows], 0.0)    # float32 tables: clamp ~1e-5
        if scratch:
            G = self._scratch
            G.data[:-4] = data
        else:
            S = 4 * self.N + 1
            G = csr_matrix((np.concatenate([data, [BLOCKED] * 4]), self._indices.copy(), self._indptr), shape=(S, S))
        G.h = h
        return G

    def potential(self, b):
        """A* potential for searches ending at vertex b, from b's first_move_dist table (None without
        one): h[4m + d] = cheapest unhindered cost (length + 0.5/turn) from "at m, arrived with d" to b,
        h(b) = 0. Exact on the plain grid, so a lower bound on every penalised graph: consistent."""
        if b not in self._h:
            F = self.tables.get(b)
            if F is None:
                return None
            D = F.min(0)[:, OPP].astype(float)                  # D[m, e]: leave m with first move e, reach b
            H = TURN[None, :, :] + D[:, None, :]                # [m, d, e]
            H[:, np.arange(4), OPP] = np.inf                    # no reversal mid-wire
            h = np.append(H.min(2).ravel(), 0.0)                # last entry: the search start S
            h[4 * b:4 * b + 4] = 0.0
            h[~np.isfinite(h)] = H_FAR
            self._h[b] = h
        return self._h[b]

    def neighbour(self, n, d):
        i, j = n % self.nx, n // self.nx
        if d == 0 and i < self.nx - 1 and self.edge_ok[n]:
            return n + 1
        if d == 2 and i > 0 and self.edge_ok[n - 1]:
            return n - 1
        if d == 1 and j < self.ny - 1 and self.edge_ok[self.N + n]:
            return n + self.nx
        if d == 3 and j > 0 and self.edge_ok[self.N + n - self.nx]:
            return n - self.nx
        return None

    def first_move_dist(self, k):
        """F[b][m, a] = cheapest path (length + 0.5/turn) leaving vertex k with first move b and
        reaching vertex m with last move a. Turns at the two ends are not included."""
        F = np.full((4, self.N, 4), np.inf, dtype=np.float32)
        moves = [(b, m) for b in range(4) if (m := self.neighbour(k, b)) is not None]
        if moves:
            D = dijkstra(self.G0, directed=True, indices=[4 * m + b for b, m in moves])
            for (b, m), row in zip(moves, D):
                F[b] = row[:4 * self.N].reshape(self.N, 4) + self.elen[self.edge_ids([k, m])[0]]
        return F

    def route(self, G, a, b, src_dir=None, next_dir=None, end_cost=None, limit=np.inf, start_cost=None):
        """Cheapest wire a -> b. src_dir: direction the wire arrived at a with; the turn at a is charged,
        a reversal (straight back) included, as one bend. next_dir: direction the wire leaves b with
        afterwards (turn at b is charged); end_cost: extra cost per arrival direction at b (e.g. the
        rest of the ring), instead of next_dir. start_cost: cost per "arrived at a with d" state, instead
        of src_dir. limit: search radius in cost; if the best end is not clearly inside it, the search
        is repeated without a limit, so the result is always exact. If G carries an A* potential for b
        (G.h, see state_graph), the limit is chosen here: first the unhindered cost + ROUTE_SLACK, then
        1.5 x + 12, then none; each search visits only states on paths within that limit."""
        S = 4 * self.N
        if start_cost is None:
            # state "arrived with d", then the first move e costs TURN[d, e]: TURN[src_dir] reproduces
            # the turn at a (straight 0, 90 degrees or straight back 0.5)
            start_cost = np.zeros(4) if src_dir is None else TURN[src_dir].astype(float)
        G.indices[-4:] = np.arange(4 * a, 4 * a + 4)
        h = getattr(G, "h", None)
        if h is None:
            G.data[-4:] = start_cost
            limits = [limit, np.inf] if np.isfinite(limit) else [np.inf]
        else:
            G.data[-4:] = start_cost + h[4 * a:4 * a + 4]         # reduced: h(S) = 0
            c0 = float(G.data[-4:].min())                           # unhindered cost of the leg (exact)
            limits = [c0 + ROUTE_SLACK, 1.5 * c0 + 12.0, np.inf]
        if end_cost is None:
            end_cost = 0 if next_dir is None else TURN[:, next_dir]
        for k, lim in enumerate(limits):
            self.counters["searches"] += 1
            self.counters["searches_rerun"] += k > 0
            dist, pred = dijkstra(G, directed=True, indices=S, return_predecessors=True, limit=lim)
            cost = dist[4 * b:4 * b + 4] + end_cost              # h(b) = 0: reduced = real cost at b
            s = 4 * b + int(np.argmin(cost))
            if not np.isfinite(lim) or dist[s] <= lim - 2 * W_BEND:      # end costs differ by <= 0.5
                break
        if not np.isfinite(dist[s]):
            return None
        path = []
        while s >= 0 and s != S:
            path.append(s // 4)
            s = pred[s]
        return path[::-1]

    def snap(self, xy, room):
        cand = np.nonzero(self.free & self.room_inner[room])[0]
        if len(cand) == 0:
            cand = np.nonzero(self.free)[0]
        return int(cand[np.argmin(np.hypot(self.X[cand] - xy[0], self.Y[cand] - xy[1]))])

    def _dirs(self, a, b):
        diff = np.asarray(b) - np.asarray(a)
        return np.select([diff == 1, diff == self.nx, diff == -1, diff == -self.nx], [0, 1, 2, 3], -1)

    def _eids(self, a, b):
        a, b = np.asarray(a), np.asarray(b)
        diff = b - a
        return np.where(diff == 1, a, np.where(diff == -1, b, np.where(diff == self.nx, self.N + a, self.N + b)))

    def dirs(self, path):
        p = np.asarray(path)
        return self._dirs(p[:-1], p[1:])

    def first_dir(self, path):
        return int(self.dirs(path[:2])[0])

    def last_dir(self, path):
        return int(self.dirs(path[-2:])[0])

    def edge_ids(self, path):
        p = np.asarray(path)
        return self._eids(p[:-1], p[1:])

    def path_length(self, path):
        return float(self.elen[self.edge_ids(path)].sum())

    def corners(self, path):
        d = self.dirs(path)
        keep = [0] + [i for i in range(1, len(path) - 1) if d[i] != d[i - 1]] + [len(path) - 1]
        return [(float(self.X[path[i]]), float(self.Y[path[i]])) for i in keep]


# --------------------------------------------------------------------------- stage 1
def set_partitions(items):
    if not items:
        yield []
        return
    first, rest = items[0], items[1:]
    for p in set_partitions(rest):
        yield [[first]] + p
        for i in range(len(p)):
            yield p[:i] + [[first] + p[i]] + p[i + 1:]


def feasible_groupings(plan):
    return [[tuple(sorted(b)) for b in p] for p in set_partitions(plan.device_rooms())
            if all(plan.block_ok(b) for b in p)]


def hub_lower_bound(plan):
    rooms = plan.device_rooms()
    if not rooms:
        return 0
    # every hub serves at least one room, so never more hubs than rooms (a room > 100 m2 cannot be split)
    return min(len(rooms), max(math.ceil(len(rooms) / MAX_DEVICES), math.ceil(plan.area(rooms) / MAX_AREA - 1e-9)))


def feasible_blocks(plan):
    """Every group of eligible rooms that may form one zone (rules 2-3)."""
    rooms = plan.device_rooms()
    return [b for size in range(1, MAX_DEVICES + 1) for b in itertools.combinations(rooms, size) if plan.block_ok(b)]


def groupings_with_hubs(plan, k, blocks):
    """All partitions of the eligible rooms into exactly k feasible blocks (exact-cover search)."""
    rooms = plan.device_rooms()
    pos = {r: i for i, r in enumerate(rooms)}
    by_first = {r: [] for r in rooms}
    for b in blocks:
        by_first[min(b, key=pos.get)].append(frozenset(b))
    area = {r: plan.room_area(r) for r in rooms}

    def rec(left, k_left, acc):
        if not left:
            if k_left == 0:
                yield [tuple(sorted(b, key=pos.get)) for b in acc]
            return
        if k_left == 0 or len(left) > k_left * MAX_DEVICES:
            return
        if not plan.oversize_ok and sum(area[r] for r in left) > k_left * MAX_AREA + 1e-9:
            return
        first = min(left, key=pos.get)
        for b in by_first[first]:
            if b <= left:
                yield from rec(left - b, k_left - 1, acc + [b])

    yield from rec(frozenset(rooms), k, [])


def area_gap(plan, grouping):
    areas = [plan.area(b) for b in grouping]
    return max(areas) - min(areas)


# --------------------------------------------------------------------------- stage 2
def prepare(plan, grid):
    """Node vertices and their direction-aware distance tables (shared by every grouping)."""
    devnode = {r: grid.snap(plan.rooms[r].device, r) for r in plan.device_rooms()}
    F = {r: grid.first_move_dist(n) for r, n in devnode.items()}
    for r, n in devnode.items():
        grid.tables[n] = F[r]                     # also the A* potentials of searches towards devices
    return dict(devnode=devnode, F=F)


def _cycle_perms(k):
    if k == 1:
        return [(0,)]
    return [p for p in itertools.permutations(range(k)) if p[0] < p[-1]]


def _minplus(M, T):
    return (M[:, :, None] + T[None, :, :]).min(1)


def _greedy_hub(grid, cand, starts, ends, ring):
    """Steepest descent from one or more starts, sharing evaluated vertex costs across restarts.
    Returns the lowest local minimum found (first start wins a tie)."""
    pos = {int(v): i for i, v in enumerate(cand)}
    J = np.full(len(cand), np.inf)

    def cost(idx):
        idx = np.asarray([i for i in idx if not np.isfinite(J[i])], dtype=int)
        if len(idx):
            v = np.full(len(idx), np.inf)
            for (s, t), M in ends.items():
                v = np.minimum(v, ring(s, t, M, idx))
            J[idx] = v

    if isinstance(starts, (int, np.integer)):
        starts = [int(starts)]
    winner = None
    for start in starts:
        cur = int(start)
        cost([cur])
        if not np.isfinite(J[cur]):
            continue
        while True:
            v = int(cand[cur])
            nb = [pos[m] for m in (v + 1, v - 1, v + grid.nx, v - grid.nx) if m in pos]
            cost(nb)
            best = min(nb, key=lambda i: J[i], default=None)
            if best is None or J[best] >= J[cur] - 1e-9:
                break
            cur = best
        if winner is None or J[cur] < J[winner] - 1e-9:
            winner = cur
    return J, winner


def evaluate_group(plan, grid, group, prep, clearance=HUB_WALL_CLEARANCE):
    """Best hub vertex for one group: exact ring cost L + 0.5B (turns at the hub and every node
    included, best visiting order, other wires ignored) at every legal hub vertex (rule 6)."""
    dev = [prep["devnode"][r] for r in group]
    F = [prep["F"][r] for r in group]
    k = len(dev)
    cx, cy = np.mean([(grid.X[d], grid.Y[d]) for d in dev], axis=0)
    ok = (grid.free & np.logical_or.reduce([grid.room_inner[r] for r in group])
          & (grid.wall_dist >= clearance - 1e-9))
    ok[list(prep["devnode"].values())] = False
    cand = np.nonzero(ok)[0]
    out = dict(group=tuple(group), dev=dev, cand=cand, centroid=(cx, cy), Jmap=np.full(grid.N, np.nan),
               start=None, J_start=np.inf, hub=None, J=np.inf, order=None, perm_lb={})
    if len(cand) == 0:
        return out

    # Tdd[i, a_i, j, a_j]: arrive at node i with a_i, turn, leave, arrive at node j with a_j
    Tdd = np.full((k, 4, k, 4), np.inf, dtype=np.float32)
    for i in range(k):
        for j in range(k):
            if i != j:
                Tdd[i, :, j, :] = (TURN[:, :, None] + F[i][:, dev[j], :][None, :, :]).min(1)
    # Ain[i][c, ah, a0]: ring arrives at hub c with ah, turns, leaves, reaches node i arriving with a0
    # Aout[i][c, al, ah]: arrives at node i with al, turns, leaves, reaches hub c arriving with ah
    # (computed for the candidate indices in use, see sides)
    use = {"idx": None}
    Ain, Aout = [None] * k, [None] * k

    def sides(idx):
        if use["idx"] is idx:
            return
        for i in range(k):
            Fi = F[i][:, cand[idx], :]                               # [b, c, a]
            first = Fi[OPP][:, :, OPP].transpose(1, 0, 2)            # [c, a0, b']  (reversed path hub -> node)
            Ain[i] = (TURN[None, :, None, :] + first[:, None, :, :]).min(3)
            Aout[i] = (TURN[None, :, :, None] + Fi.transpose(1, 0, 2)[:, None, :, :]).min(2)
        use["idx"] = idx

    # node chain of each cyclic order, reduced to (first node, last node)
    perms = _cycle_perms(k)
    chains = {}
    for p in perms:
        if k == 1:
            M = np.where(np.eye(4, dtype=bool), 0.0, np.inf)
        else:
            M = Tdd[p[0], :, p[1], :]
            for x, y in zip(p[1:-1], p[2:]):
                M = _minplus(M, Tdd[x, :, y, :])
        chains[p] = M
    ends = {}
    for p, M in chains.items():
        key = (p[0], p[-1])
        ends[key] = M if key not in ends else np.minimum(ends[key], M)

    def ring(s, t, M, idx):
        sides(idx)
        T1 = (Ain[s][:, :, :, None] + M[None, None, :, :]).min(2)          # [c, ah, al]
        return (T1 + Aout[t].transpose(0, 2, 1)).min((1, 2))

    J = np.full(len(cand), np.inf)

    def evaluate(idx):
        idx = np.asarray(idx, dtype=np.int64)
        for a in range(0, len(idx), 4096):                     # bounded memory
            part = idx[a:a + 4096]
            v = np.full(len(part), np.inf)
            for (s, t), M in ends.items():
                v = np.minimum(v, ring(s, t, M, part))
            J[part] = v

    dc = np.hypot(grid.X[cand] - cx, grid.Y[cand] - cy)
    start = int(np.argmin(dc))                  # node centroid, moved to the nearest legal hub vertex
    if HUB_SEARCH in ("greedy", "random_restart"):
        starts = [start]
        if HUB_SEARCH == "random_restart" and len(cand) > 1:
            seed = (sum((i + 1) * int(v) for i, v in enumerate(dev)) + len(cand)) % (2 ** 32)
            rng = np.random.default_rng(seed)
            others = np.delete(np.arange(len(cand)), start)
            starts.extend(int(i) for i in rng.choice(others, size=min(max(HUB_RESTARTS - 1, 0),
                                                                         len(others)), replace=False))
        J, opt = _greedy_hub(grid, cand, starts, ends, lambda s, t, M, idx: ring(s, t, M, np.asarray(idx)))
        if opt is None:
            return out
    else:
        if HUB_PRUNE:
            # exact pruning: the ring at c costs at least cheapest(node s -> c) + chain s..t + cheapest(t -> c)
            Fmin = [F[i][:, cand, :].min((0, 2)) for i in range(k)]
            lb = np.full(len(cand), np.inf)
            for (s, t), M in ends.items():
                lb = np.minimum(lb, Fmin[s] + float(M.min()) + Fmin[t])
            order = np.argsort(lb, kind="stable")
            if HUB_SEARCH == "adaptive":
                # Evaluate promising vertices in small batches. Once the next lower bound
                # exceeds the best exact ring cost, no remaining vertex can win.
                best, at = np.inf, 0
                while at < len(order) and lb[order[at]] <= best + 1e-3:
                    part = order[at:at + 16]
                    evaluate(part)
                    best = min(best, float(J[part].min()))
                    at += len(part)
            else:
                evaluate(order[:64])
                best = J.min()
                more = np.nonzero((lb <= best + 1e-3) & ~np.isfinite(J))[0]
                evaluate(more)
            grid.counters["hub_points_evaluated"] += int(np.isfinite(J).sum())
            grid.counters["hub_points"] += len(cand)
            if not np.isfinite(J[start]):
                evaluate([start])
        else:
            evaluate(np.arange(len(cand)))
        if not np.isfinite(J).any():
            return out
        tie = np.nonzero(J <= J.min() + 1e-4)[0]
        opt = int(tie[np.argmin(dc[tie])])      # among equal-cost vertices, closest to the centroid
    sel = np.array([opt])
    perm_lb = {}
    for p, M in chains.items():                 # lower bound of every visiting order at the chosen hub
        c = float(ring(p[0], p[-1], M, sel)[0])
        o = tuple(dev[i] for i in p)
        perm_lb[o] = perm_lb[o[::-1]] = c
    out["Jmap"][cand] = J
    best = min(perm_lb, key=perm_lb.get)
    out.update(start=int(cand[start]), J_start=float(J[start]), hub=int(cand[opt]), J=float(J[opt]),
               order=list(best), perm_lb=perm_lb)
    return out


# --------------------------------------------------------------------------- stage 3
class Usage:
    """What the wires routed so far occupy: edge use counts (overlap) and, per vertex, a bit mask of
    the pass types through it (crossings). Leg ends are hubs / nodes, where crossings never count."""

    def __init__(self, grid, loops=()):
        self.grid = grid
        self.edge_use = np.zeros(2 * grid.N)
        self.mask = np.zeros(grid.N, np.uint8)
        self.forbid = np.zeros(2 * grid.N, bool)       # edges of the ring being routed (NO_RETRACE)
        for loop in loops:
            for leg in loop["legs"]:
                self.add(leg)

    def add(self, leg, own=False):
        """Add a routed wire. own: it belongs to the ring now being routed, so (with NO_RETRACE) the ring's
        other legs may not use its edges."""
        p = np.asarray(leg, dtype=int)
        e = self.grid.edge_ids(p)
        np.add.at(self.edge_use, e, 1)
        if own and NO_RETRACE:
            self.forbid[e] = True
        if len(p) > 2:
            d = self.grid._dirs(p[:-1], p[1:])
            t = PASS_TYPE[OPP[d[:-1]], d[1:]]
            ok = t >= 0
            np.bitwise_or.at(self.mask, p[1:-1][ok], (1 << t[ok]).astype(np.uint8))

    def copy(self):
        u = Usage.__new__(Usage)
        u.grid, u.edge_use, u.mask, u.forbid = self.grid, self.edge_use.copy(), self.mask.copy(), self.forbid.copy()
        return u

    def crossing_points(self):
        m = self.mask
        return ((m & 3) == 3) | ((m & 12) == 12) | ((m & 48) == 48)


def _penalised_graph(grid, usage, target, terminals=None, h=None, scratch=False):
    """Routing costs = exact increase of J: length + 0.5 per turn, overlap 1/m on edges already used,
    5 for passing a vertex in a way that crosses or touches a wire already there (no shared edge),
    unless that vertex is a hub / node or already a crossing point. Other hubs and nodes are blocked.
    h, scratch: see Grid.state_graph. With scratch the penalties are added sparsely (only arcs on used
    edges, into blocked points or out of points with wires) to cached base weights: same weights."""
    if scratch:
        G = grid._scratch
        data = G.data
        data[:-4] = grid.base_weights(None if h is None else target)
        used = np.flatnonzero(usage.edge_use)
        if len(used):
            arcs = grid.arcs_of(grid._by_edge, used)
            data[arcs] += W_OVERLAP * grid.elen[grid.m_eid[grid._arc[arcs]]]
        own = np.flatnonzero(usage.forbid)
        if len(own):
            data[grid.arcs_of(grid._by_edge, own)] += BLOCKED
        if terminals is not None:
            t = np.asarray(terminals)
            data[grid.arcs_of(grid._by_dst, t[t != target])] += BLOCKED
        wired = usage.mask != 0
        wired &= ~usage.crossing_points()
        if terminals is not None:
            wired[terminals] = False
        v = np.flatnonzero(wired)
        if len(v):
            arcs = grid.arcs_of(grid._by_src, v)
            hit = (usage.mask[grid._esrc[arcs]] & grid._ecomp[arcs]) != 0
            data[arcs[hit]] += W_CROSS
        G.h = h
        return G
    no_x = ~usage.crossing_points()
    node_pen = np.zeros(grid.N)
    if terminals is not None:
        no_x[terminals] = False
        node_pen[terminals] = BLOCKED
    node_pen[target] = 0.0
    src = grid._esrc
    arc_pen = W_CROSS * (((usage.mask[src] & grid._ecomp) != 0) & no_x[src])
    return grid.state_graph(W_OVERLAP * grid.elen * (usage.edge_use > 0) + BLOCKED * usage.forbid, node_pen, arc_pen,
                            h=h, scratch=scratch)


def _route_leg(grid, usage, a, b, terminals, src_dir=None, next_dir=None, end_cost=None, limit=np.inf):
    """Cheapest wire a -> b against the wires in usage (exact J increase, see _penalised_graph), with
    the turn at a after src_dir and at b before next_dir (or end_cost per arrival direction at b).
    With GUIDED, the search runs towards whichever end has a distance table (A* potential): towards b,
    or from b back to a (same costs: every penalty is symmetric in the wire's direction; the end
    turns swap roles), and the path is reversed. Returns the vertex path or None."""
    if not GUIDED:
        return grid.route(_penalised_graph(grid, usage, b, terminals), a, b, src_dir, next_dir, end_cost, limit)
    h = grid.potential(b)
    if h is not None:
        return grid.route(_penalised_graph(grid, usage, b, terminals, h=h, scratch=True), a, b, src_dir,
                          next_dir, end_cost)
    h = grid.potential(a)
    E = np.zeros(4) if end_cost is None and next_dir is None else \
        (TURN[:, next_dir] if end_cost is None else np.asarray(end_cost, float))
    if h is None or E.max() - E.min() > W_BEND + 1e-9:
        return grid.route(_penalised_graph(grid, usage, b, terminals, scratch=True), a, b, src_dir, next_dir,
                          end_cost, limit)
    # reversed: start at b "arrived with x" costs E[OPP[x]] (then leaving straight on, with direction
    # x, matches arriving at b with OPP[x] going forward); arriving at a with l costs the forward turn
    # from src_dir into the first move OPP[l]
    back_end = None if src_dir is None else TURN[src_dir][OPP].astype(float)
    path = grid.route(_penalised_graph(grid, usage, a, terminals, h=h, scratch=True), b, a,
                      end_cost=back_end, start_cost=E[OPP])
    return None if path is None else path[::-1]


def _leg_increment(grid, usage, path, terminals, src_dir=None, next_dir=None):
    """Exact incremental J of one leg, including turns at its known endpoints."""
    d = grid.dirs(path)
    edges = grid.edge_ids(path)
    cost = grid.path_length(path) + W_OVERLAP * float(
        (grid.elen[edges] * (usage.edge_use[edges] > 0)).sum()) + BLOCKED * int(usage.forbid[edges].sum())
    cost += W_BEND * int(BENDS[d[:-1], d[1:]].sum())
    if src_dir is not None:
        cost += float(TURN[src_dir, d[0]])
    if next_dir is not None:
        cost += float(TURN[d[-1], next_dir])
    terminal_set = set(int(x) for x in terminals)
    crossed = usage.crossing_points()
    for i, v in enumerate(path[1:-1], 1):
        if v in terminal_set:
            cost += BLOCKED
        else:
            t = PASS_TYPE[OPP[d[i - 1]], d[i]]
            if usage.mask[v] & (1 << COMP[t]) and not crossed[v]:
                cost += W_CROSS
    return cost


def k_shortest_legs(grid, usage, a, b, terminals, k=2, src_dir=None, next_dir=None):
    """Yen-style loopless K paths for fixed current wire usage and endpoint turns.

    Each spur uses the existing direction-state Dijkstra search. Paths are ranked by their
    exact incremental J. LEG_SPUR_LIMIT=0 gives full Yen search; a positive limit samples
    deviations along the path to keep complete-plan beam search affordable.
    """
    first = _route_leg(grid, usage, a, b, terminals, src_dir, next_dir)
    if first is None:
        return []
    accepted = [first]
    if k <= 1:
        return accepted
    graph = _penalised_graph(grid, usage, b, terminals)
    candidates, seen = [], {tuple(first)}
    for _ in range(1, k):
        previous = accepted[-1]
        positions = range(len(previous) - 1)
        if LEG_SPUR_LIMIT and len(previous) - 1 > LEG_SPUR_LIMIT:
            positions = sorted(set(np.linspace(0, len(previous) - 2, LEG_SPUR_LIMIT,
                                               dtype=int).tolist()))
        for i in positions:
            root = previous[:i + 1]
            blocked = list(grid.arcs_of(grid._by_dst, np.asarray(root[:-1], dtype=int)))
            blocked_next = set()
            for path in accepted:
                if len(path) > i + 1 and path[:i + 1] == root:
                    blocked_next.add(path[i + 1])
                    edge = int(grid.edge_ids(path[i:i + 2])[0])
                    arcs = grid.arcs_of(grid._by_edge, np.asarray([edge]))
                    blocked.extend(int(x) for x in arcs if grid._esrc[x] == root[-1])
            blocked = np.unique(blocked)
            original = graph.data[blocked].copy()
            graph.data[blocked] = BLOCKED * 1000
            try:
                arrival = src_dir if i == 0 else grid.last_dir(root)
                spur = grid.route(graph, root[-1], b, arrival, next_dir)
            finally:
                graph.data[blocked] = original
            if spur is None or len(spur) < 2 or spur[1] in blocked_next or \
                    any(v in root[:-1] for v in spur):
                continue
            path = root[:-1] + spur
            key = tuple(path)
            if key in seen or len(set(path)) != len(path):
                continue
            seen.add(key)
            score = _leg_increment(grid, usage, path, terminals, src_dir, next_dir)
            heapq.heappush(candidates, (score, key))
        if not candidates:
            break
        accepted.append(list(heapq.heappop(candidates)[1]))
    return accepted


def measure(grid, loops, terminals=None):
    """Exact L, X, O, B and J of a set of closed rings on the grid (slides 6-7)."""
    if terminals is None:
        terminals = [l["hub"] for l in loops] + [n for l in loops for n in l["order"]]
    is_term = np.zeros(grid.N, bool)
    is_term[np.asarray(list(terminals), dtype=int)] = True
    B = 0
    eids, V, P, Q = [], [], [], []
    for loop in loops:
        seq = np.concatenate([np.asarray(leg[:-1], dtype=int) for leg in loop["legs"]])
        nxt = np.roll(seq, -1)
        d = grid._dirs(seq, nxt)                     # direction leaving seq[t]
        prev = np.roll(d, 1)                         # direction arriving at seq[t]
        B += int(BENDS[prev, d].sum())               # every turn, at hub and nodes too
        eids.append(grid._eids(seq, nxt))
        V.append(seq)
        P.append(OPP[prev])                          # pass at seq[t] uses the edges towards P and Q
        Q.append(d)
    use = np.bincount(np.concatenate(eids), minlength=2 * grid.N)
    L = float((grid.elen * use).sum())
    O = float((grid.elen * np.maximum(use - 1, 0)).sum())
    V, P, Q = np.concatenate(V), np.concatenate(P), np.concatenate(Q)
    keep = ~is_term[V]
    V, P, Q = V[keep], P[keep], Q[keep]
    order = np.argsort(V, kind="stable")
    V, P, Q = V[order], P[order], Q[order]
    starts = np.flatnonzero(np.r_[True, V[1:] != V[:-1]])
    counts = np.diff(np.r_[starts, len(V)])
    X = 0
    for s, c in zip(starts[counts > 1], counts[counts > 1]):
        passes = [{int(P[t]), int(Q[t])} for t in range(s, s + c)]
        if any(not (a & b) for a, b in itertools.combinations(passes, 2)):
            X += 1                                   # shared point, no shared edge: crossing or touch
    return dict(J=L + W_CROSS * X + W_OVERLAP * O + W_BEND * B, L=L, X=X, O=O, B=B)


def _loop_points(hub, order):
    pts = [hub] + list(order) + [hub]
    return list(zip(pts[:-1], pts[1:]))


def _cost_to_go(F, hub, order, j, d0):
    """Cheapest rest of the ring (length + 0.5/turn, other wires ignored) after arriving at order[j],
    per arrival direction: order[j] -> ... -> order[-1] -> hub, plus the closing turn at the hub into
    the first leg's direction d0 (0 if not known yet)."""
    close = np.zeros(4) if d0 is None else TURN[:, d0]
    tail = (F[order[-1]][:, hub, :] + close[None, :]).min(1)          # per leave direction at the last node
    C = (TURN + tail[None, :]).min(1)                                  # per arrival direction
    for i in range(len(order) - 2, j - 1, -1):
        nxt = (F[order[i]][:, order[i + 1], :] + C[None, :]).min(1)
        C = (TURN + nxt[None, :]).min(1)
    return C


def _leg_limit(F, a, b):
    """Search radius for a leg a -> b: 1.5 x its unhindered cost + 12 (route() re-runs unbounded if the
    end is not clearly inside, so this only saves time). Needs the distance table of a or b."""
    if F is None:
        return np.inf
    n, m = (b, a) if b in F else (a, b)
    return 1.5 * float(F[n][:, m, :].min()) + 12.0 if n in F else np.inf


def sequential_loop(grid, hub, order, base_loops, terminals=None, penalise=True, F=None, base_usage=None,
                    emit=None):
    """Route the ring leg by leg in visiting order. With penalise, every leg pays the exact J increase
    it causes against the wires already routed (overlap 1/m, crossing 5, see _penalised_graph). Turns
    at hub/nodes are charged. With F (node distance tables keyed by node vertex) each leg also weighs
    the rest of the ring per arrival direction, so it does not arrive at a node facing the wrong way,
    and its search is bounded by its unhindered cost (exact: it re-runs unbounded if needed). Returns
    None if a leg cannot be routed. emit(kind, **data), if given, is told about every leg (live view)."""
    usage = (base_usage or Usage(grid, base_loops)).copy()
    pairs = _loop_points(hub, order)
    legs = []
    for i, (a, b) in enumerate(pairs):
        src = grid.last_dir(legs[-1]) if legs else None
        nxt = grid.first_dir(legs[0]) if i == len(pairs) - 1 else None
        end = None
        if F is not None and i < len(pairs) - 1:
            end = _cost_to_go(F, hub, order, i, grid.first_dir(legs[0]) if legs else None)
        limit = _leg_limit(F, a, b)
        if penalise:
            leg = (_route_leg(grid, usage, a, b, terminals, src, nxt, end, limit)
                   or _route_leg(grid, usage, a, b, terminals, None, nxt, end))
        else:
            leg = grid.route(grid.G0, a, b, src, nxt, end, limit) or grid.route(grid.G0, a, b, None, nxt, end)
        if leg is None:
            return None
        legs.append(leg)
        usage.add(leg, own=True)
        if emit:
            emit("leg", hub=hub, order=list(order), legs=list(legs))
    return dict(hub=hub, order=list(order), legs=legs)


def beam_loop(grid, hub, order, base_loops, terminals, F=None, base_usage=None, emit=None):
    """Keep the best partial rings while expanding each leg by its K shortest paths."""
    pairs = _loop_points(hub, order)
    beam = [(0.0, [], (base_usage or Usage(grid, base_loops)).copy())]
    for i, (a, b) in enumerate(pairs):
        expanded = []
        for cost, legs, usage in beam:
            src_dir = grid.last_dir(legs[-1]) if legs else None
            closing_dir = grid.first_dir(legs[0]) if i == len(pairs) - 1 else None
            for path in k_shortest_legs(grid, usage, a, b, terminals, LEG_K_PATHS,
                                        src_dir, closing_dir):
                if any(v in terminals for v in path[1:-1]):
                    continue
                increment = _leg_increment(grid, usage, path, terminals, src_dir, closing_dir)
                new_usage = usage.copy()
                new_usage.add(path, own=True)
                new_legs = legs + [path]
                score = cost + increment
                if F is not None and i < len(order):
                    tail = _cost_to_go(F, hub, order, i, grid.first_dir(new_legs[0]))
                    score += float(tail[grid.last_dir(path)])
                expanded.append((score, cost + increment, new_legs, new_usage))
        if not expanded:
            return None
        expanded.sort(key=lambda row: row[0])
        beam = [(cost, legs, usage) for _, cost, legs, usage in expanded[:RING_BEAM_WIDTH]]
        grid.counters["beam_partials"] += len(expanded)
    best = min((dict(hub=hub, order=list(order), legs=legs) for _, legs, _ in beam),
               key=lambda loop: measure(grid, base_loops + [loop], terminals)["J"])
    return best


def _held_karp_tour(dist):
    """Shortest hub-anchored TSP tour in a pairwise cost matrix (hub is index 0)."""
    n = len(dist) - 1
    if n == 0:
        return ()
    dp = {(1 << i, i): (float(dist[0, i + 1]), (i,)) for i in range(n)}
    for size in range(2, n + 1):
        for subset in itertools.combinations(range(n), size):
            mask = sum(1 << i for i in subset)
            for last in subset:
                previous = mask ^ (1 << last)
                dp[mask, last] = min(
                    (dp[previous, p][0] + float(dist[p + 1, last + 1]),
                     dp[previous, p][1] + (last,)) for p in subset if p != last)
    return min((dp[(1 << n) - 1, i][0] + float(dist[i + 1, 0]),
                dp[(1 << n) - 1, i][1]) for i in range(n))[1]


def _tsp_order(hub, devs, F):
    """Held-Karp on unobstructed pairwise shortest wire costs (internal bends included)."""
    pts = [hub, *devs]
    dist = np.zeros((len(pts), len(pts)))
    for i, a in enumerate(pts):
        for j, b in enumerate(pts):
            if i != j:
                source = a if a in F else b
                target = b if a in F else a
                dist[i, j] = float(F[source][:, target, :].min())
    return tuple(devs[i] for i in _held_karp_tour(dist))


def best_order_loop(grid, hub, devs, base_loops, terminals=None, perm_lb=None, F=None, emit=None):
    """Sequential routing for every visiting order (both directions); keep the lowest J. With perm_lb
    (stage-2 cost of each order at this hub) orders are tried cheapest first and the search stops once
    no remaining order can beat the best J found."""
    if ORDER_SEARCH == "tsp" and F is not None:
        tour = _tsp_order(hub, devs, F)
        orders = [tour]
        if TSP_DIRECTIONS > 1 and tour[::-1] != tour:
            orders.append(tour[::-1])
    else:
        orders = [tuple(p) for p in itertools.permutations(devs)]
    J_base = measure(grid, base_loops, terminals)["J"] if base_loops else 0.0
    base_usage = Usage(grid, base_loops)
    if perm_lb and ORDER_SEARCH != "tsp":
        orders.sort(key=lambda o: perm_lb.get(o, 0.0))
    best, best_J = None, np.inf
    for order in orders:
        if perm_lb and J_base + perm_lb.get(order, 0.0) - LB_SLACK >= best_J:
            if emit:
                emit("log", msg=f"remaining visiting orders skipped: their lower bound "
                                f"{J_base + perm_lb.get(order, 0.0):.2f} >= best J {best_J:.2f}")
            break
        grid.counters["orders_routed"] += 1
        if RING_ROUTER == "beam":
            loop = beam_loop(grid, hub, order, base_loops, terminals, F, base_usage, emit)
        else:
            loop = sequential_loop(grid, hub, order, base_loops, terminals, F=F, base_usage=base_usage, emit=emit)
        if loop is None:
            continue
        J = measure(grid, base_loops + [loop], terminals)["J"]
        if emit:
            emit("attempt", hub=hub, order=list(order), legs=loop["legs"], J=J, best=bool(J < best_J - 1e-9))
        if J < best_J - 1e-9:
            best, best_J = loop, J
    return best


def _usage_without(grid, loops, skip):
    """Usage of every leg except the (ring index, leg index) pairs in skip. The remaining legs of the rings
    in skip are that ring's own wire (NO_RETRACE: the re-routed legs may not use their edges)."""
    u = Usage(grid)
    rings = {r for r, _ in skip}
    for r, loop in enumerate(loops):
        for i, leg in enumerate(loop["legs"]):
            if (r, i) not in skip:
                u.add(leg, own=r in rings)
    return u


def retraces(grid, loop):
    """True if the ring uses some edge more than once (runs back along its own wire)."""
    e = np.concatenate([grid.edge_ids(leg) for leg in loop["legs"]])
    return len(e) != len(np.unique(e))


def _hub_legs_cost(grid, loop, rest, terminals):
    """Exact share of J of a ring's two hub legs (last node -> hub -> first node), given the usage of
    every other wire: their length, the bends inside them and at their three ends, the overlap and the
    crossing points they add."""
    legs = loop["legs"]
    l0, lL = legs[0], legs[-1]
    d0, dL = grid.dirs(l0), grid.dirs(lL)
    B = int(BENDS[d0[:-1], d0[1:]].sum() + BENDS[dL[:-1], dL[1:]].sum()) + int(BENDS[dL[-1], d0[0]])
    if len(legs) > 2:
        B += int(BENDS[grid.last_dir(legs[-2]), dL[0]] + BENDS[d0[-1], grid.first_dir(legs[1])])
    else:
        B += int(BENDS[d0[-1], dL[0]])
    e = np.concatenate([grid.edge_ids(l0), grid.edge_ids(lL)])
    c = np.bincount(e, minlength=2 * grid.N)
    u = rest.edge_use
    O = float((grid.elen * (np.maximum(u + c - 1, 0) - np.maximum(u - 1, 0))).sum())
    after = rest.copy()
    after.add(l0)
    after.add(lL)
    new_x = after.crossing_points() & ~rest.crossing_points()
    new_x[terminals] = False
    return grid.path_length(l0) + grid.path_length(lL) + W_BEND * B + W_OVERLAP * O + W_CROSS * int(new_x.sum())


def _hub_legs_bound(grid, F, loop, cand):
    """Lower bound on that share for every candidate hub vertex in cand: the unhindered cost (length +
    0.5/turn, turns at the hub and at both node ends included) of last node -> c -> first node."""
    legs, order = loop["legs"], loop["order"]
    if len(legs) > 2:
        t_l = TURN[grid.last_dir(legs[-2])]              # [b]: turn at the last node into first move b
        t_0 = TURN[OPP, grid.first_dir(legs[1])]         # [b]: table is reversed, the wire arrives with OPP[b]
    else:
        t_l = t_0 = np.zeros(4)
    FL = (F[order[-1]][:, cand, :] + t_l[:, None, None]).min(0)    # [c, a]: last leg arrives at c with a
    F0 = (F[order[0]][:, cand, :] + t_0[:, None, None]).min(0)     # [c, a']: first leg leaves c with OPP[a']
    return (FL[:, :, None] + F0[:, None, :] + TURN[:, OPP][None]).min((1, 2))


def _hub_moves(grid, loops, r, F, cand, terminals):
    """Greedy hub relocation candidates for ring r: legal hub vertices of its zone (cand) that no other
    wire or hub uses and whose bound beats the current hub legs' exact share of J, cheapest bound first,
    at most HUB_TRIES of them and at least HUB_SPACING apart. Yields (vertex, usage of the other wires)."""
    legs = loops[r]["legs"]
    n = len(legs)
    rest = _usage_without(grid, loops, {(r, 0), (r, n - 1)})
    cost = _hub_legs_cost(grid, loops[r], rest, terminals)
    busy = np.zeros(grid.N, bool)
    busy[terminals] = True
    for q, l in enumerate(loops):
        for i, leg in enumerate(l["legs"]):
            if q != r or 0 < i < n - 1:
                busy[leg] = True
    c = cand[~busy[cand]]
    if len(c) == 0:
        return
    est = _hub_legs_bound(grid, F, loops[r], c)
    keep = est < cost - LB_SLACK                         # the others cannot lower J
    c, est = c[keep], est[keep]
    tried = []
    for j in np.argsort(est, kind="stable"):
        if len(tried) >= HUB_TRIES:
            return
        x, y = grid.X[c[j]], grid.Y[c[j]]
        if any(abs(x - tx) < HUB_SPACING and abs(y - ty) < HUB_SPACING for tx, ty in tried):
            continue
        tried.append((x, y))
        yield int(c[j]), rest


def _ring_bound(F, hub, order):
    """Unhindered cost (length + 0.5/turn, every turn at the hub and the nodes included) of the ring
    hub -> order -> hub: a lower bound on its share of J (same value as stage 2's perm_lb)."""
    best = np.inf
    for d0 in range(4):                      # direction the first leg leaves the hub with
        C = _cost_to_go(F, hub, order, 0, d0)                  # per arrival direction at order[0]
        first = F[order[0]][:, hub, OPP[d0]]                   # [b]: reversed, arrives at order[0] with OPP[b]
        best = min(best, float((first + C[OPP]).min()))
    return best


def improve_rings(grid, loops, terminals=None, F=None, max_rounds=5, deadline=None, emit=None, cands=None,
                  ring_moves=None, only=None, polish=False):
    """Rip-up and reroute after stage 3. Stage 3 routes a ring's legs one after another (and rings one
    after another), so early legs never see the wires routed after them. Moves, repeated until a round
    changes nothing:
      leg   re-route one leg against all other wires, turns at both ends charged;
      pair  re-route the two legs at a node in reverse order: the outgoing leg first, then the incoming
            one against it (frees the line the incoming leg took into a dead-end room);
      hub   (with cands, one array of legal hub vertices per ring, and F) greedy hub relocation: move
            the hub to the most promising other legal point (see _hub_moves) and re-route its two legs
            against all other wires; the first point that lowers J is taken, then the search repeats
            from there;
      ring  (ring_moves, default RING_MOVES, with F) re-route a whole ring in another visiting order
            against all other wires (stage-3 sequential routing), polish it with the leg and pair moves,
            keep it if J drops. Orders are tried cheapest unhindered bound first, at most RING_TRIES per
            ring and round, and only while the bound can still beat the ring's current share of J.
    only: work on these ring indices only (the other rings stay as they are). polish: leg, pair and hub moves only.
    A change is kept only if the exact J of the whole design drops, so J never increases."""
    if terminals is None:
        terminals = np.array(sorted({l["hub"] for l in loops} | {n for l in loops for n in l["order"]}))
    loops = [dict(l, legs=list(l["legs"])) for l in loops]
    J = measure(grid, loops, terminals)["J"]
    ring_moves = RING_MOVES if ring_moves is None else ring_moves
    work = list(range(len(loops))) if only is None else list(only)

    def keep(r, legs, hub=None):
        nonlocal loops, J, terminals
        new = dict(loops[r], legs=legs) if hub is None else dict(loops[r], legs=legs, hub=hub)
        if NO_RETRACE and retraces(grid, new):
            return False
        cand = loops[:r] + [new] + loops[r + 1:]
        term = terminals if hub is None else np.where(terminals == loops[r]["hub"], hub, terminals)
        Jn = measure(grid, cand, term)["J"]
        grid.counters["moves_tried"] += 1
        if Jn < J - 1e-9:
            grid.counters["moves_kept"] += 1
            grid.counters["hub_moves_kept"] += hub is not None
            loops, J, terminals = cand, Jn, term
            if emit:
                emit("improve", loops=loops, J=J)
            return True
        return False

    def late():
        return deadline is not None and time.perf_counter() > deadline

    for _ in range(max_rounds):
        better = False
        for r in work:
            for i in range(len(loops[r]["legs"])):
                if late():
                    return loops
                legs = loops[r]["legs"]
                n, (a, b) = len(legs), (legs[i][0], legs[i][-1])
                new = _route_leg(grid, _usage_without(grid, loops, {(r, i)}), a, b, terminals,
                                 grid.last_dir(legs[i - 1]), grid.first_dir(legs[(i + 1) % n]), limit=_leg_limit(F, a, b))
                if new is not None and new != legs[i]:
                    better |= keep(r, legs[:i] + [new] + legs[i + 1:])
        for r in work:
            for i in range(len(loops[r]["legs"])):
                if late():
                    return loops
                legs = loops[r]["legs"]
                n, j = len(legs), (i + 1) % len(legs)
                a, b, c = legs[i][0], legs[i][-1], legs[j][-1]
                usage = _usage_without(grid, loops, {(r, i), (r, j)})
                after = grid.first_dir(legs[(j + 1) % n]) if n > 2 else None    # n = 2: that leg is legs[i]
                out = _route_leg(grid, usage, b, c, terminals, None, after, limit=_leg_limit(F, b, c))
                if out is None:
                    continue
                usage.add(out, own=True)
                before = grid.last_dir(legs[i - 1]) if n > 2 else grid.last_dir(out)
                inn = _route_leg(grid, usage, a, b, terminals, before, grid.first_dir(out), limit=_leg_limit(F, a, b))
                if inn is None or (inn == legs[i] and out == legs[j]):
                    continue
                trial = list(legs)
                trial[i], trial[j] = inn, out
                better |= keep(r, trial)
        if cands is not None and F is not None:
            for r in work:
                moved = True
                while moved:                             # greedy: restart from the new hub point
                    moved = False
                    legs, h = loops[r]["legs"], loops[r]["hub"]
                    n, o0, ol = len(legs), loops[r]["order"][0], loops[r]["order"][-1]
                    for v, rest in _hub_moves(grid, loops, r, F, cands[r], terminals):
                        if late():
                            return loops
                        term = np.where(terminals == h, v, terminals)
                        back = _route_leg(grid, rest, ol, v, term, grid.last_dir(legs[-2]) if n > 2 else None,
                                          limit=_leg_limit(F, ol, v))
                        if back is None:
                            continue
                        u = rest.copy()
                        u.add(back, own=True)
                        after = grid.first_dir(legs[1]) if n > 2 else grid.first_dir(back)
                        out = _route_leg(grid, u, v, o0, term, grid.last_dir(back), after, limit=_leg_limit(F, v, o0))
                        if out is None:
                            continue
                        if keep(r, [out] + legs[1:-1] + [back], hub=v):
                            better = moved = True
                            if emit:
                                emit("log", msg=f"hub {r + 1} moved: J {J:.2f}")
                            break
        if SEG_MOVES and not polish:
            for r in work:
                n = len(loops[r]["legs"])
                if n < 4:
                    continue
                for i in range(n):
                    if late():
                        return loops
                    legs = loops[r]["legs"]
                    i1, i2, i3 = (i + 1) % n, (i + 2) % n, (i + 3) % n
                    a, b, c, d = legs[i][0], legs[i][-1], legs[i1][-1], legs[i2][-1]
                    usage = _usage_without(grid, loops, {(r, i), (r, i1), (r, i2)})
                    mid = _route_leg(grid, usage, b, c, terminals, limit=_leg_limit(F, b, c))
                    if mid is None:
                        continue
                    usage.add(mid, own=True)
                    out = _route_leg(grid, usage, c, d, terminals, grid.last_dir(mid), grid.first_dir(legs[i3]),
                                     limit=_leg_limit(F, c, d))
                    if out is None:
                        continue
                    usage.add(out, own=True)
                    inn = _route_leg(grid, usage, a, b, terminals, grid.last_dir(legs[i - 1]), grid.first_dir(mid),
                                     limit=_leg_limit(F, a, b))
                    if inn is None:
                        continue
                    trial = list(legs)
                    trial[i], trial[i1], trial[i2] = inn, mid, out
                    if trial != legs:
                        grid.counters["seg_moves_tried"] += 1
                        better |= keep(r, trial)
        if KICK_MOVES and not polish and F is not None:
            for r in work:
                for i in range(len(loops[r]["legs"])):
                    if late():
                        return loops
                    legs = loops[r]["legs"]
                    n, (a, b) = len(legs), (legs[i][0], legs[i][-1])
                    usage = _usage_without(grid, loops, {(r, i)})
                    np.add.at(usage.edge_use, grid.edge_ids(legs[i]), 1)      # avoid its own line: overlap price
                    new = _route_leg(grid, usage, a, b, terminals, grid.last_dir(legs[i - 1]),
                                     grid.first_dir(legs[(i + 1) % n]), limit=_leg_limit(F, a, b))
                    if new is None or new == legs[i]:
                        continue
                    grid.counters["kick_moves_tried"] += 1
                    trial = loops[:r] + [dict(loops[r], legs=legs[:i] + [new] + legs[i + 1:])] + loops[r + 1:]
                    trial = improve_rings(grid, trial, terminals, F, max_rounds=RING_POLISH, deadline=deadline,
                                          polish=True, only=[r])
                    Jt = measure(grid, trial, terminals)["J"]
                    if Jt < J - 1e-9:
                        grid.counters["kick_moves_kept"] += 1
                        loops, J, better = trial, Jt, True
                        if emit:
                            emit("improve", loops=loops, J=J)
        if ring_moves and not polish and F is not None:
            for r in work:
                if late():
                    return loops
                hub, order = loops[r]["hub"], tuple(loops[r]["order"])
                rest = loops[:r] + loops[r + 1:]
                J_rest = measure(grid, rest, terminals)["J"] if rest else 0.0
                orders = sorted((_ring_bound(F, hub, o), o) for o in itertools.permutations(order) if o != order)
                for lb, o in orders[:RING_TRIES]:
                    if J_rest + lb >= J - LB_SLACK or late():
                        break
                    grid.counters["ring_moves_tried"] += 1
                    new = sequential_loop(grid, hub, list(o), rest, terminals, F=F)
                    if new is None:
                        continue
                    trial = loops[:r] + [new] + loops[r + 1:]
                    trial = improve_rings(grid, trial, terminals, F, max_rounds=RING_POLISH, deadline=deadline,
                                          polish=True, only=[r])
                    Jt = measure(grid, trial, terminals)["J"]
                    if Jt < J - 1e-9:
                        grid.counters["ring_moves_kept"] += 1
                        loops, J, better = trial, Jt, True
                        if emit:
                            emit("improve", loops=loops, J=J)
                        break
        if not better:
            break
    return loops


def route_rings(grid, evs, prep=None, emit=None):
    """Stage 3 (method 3): for each hub in turn, route its ring sequentially with overlap/crossing
    penalties for every visiting order and keep the order with the lowest exact J, counting
    interactions with the rings already routed. Returns None if a ring cannot be routed."""
    terminals = np.array(sorted({ev["hub"] for ev in evs} | {d for ev in evs for d in ev["dev"]}))
    F = None if prep is None else {prep["devnode"][r]: f for r, f in prep["F"].items()}
    loops = []
    for i, ev in enumerate(evs):
        e = None if emit is None else (lambda kind, _i=i, **d: emit(kind, ring=_i, **d))
        loop = best_order_loop(grid, ev["hub"], ev["dev"], loops, terminals, ev.get("perm_lb"), F, emit=e)
        if loop is None:
            if emit:
                emit("log", msg=f"ring {i + 1} could not be routed")
            return None
        loops.append(loop)
        if emit:
            emit("ring", ring=i, hub=loop["hub"], order=loop["order"], legs=loop["legs"])
    return loops


# --------------------------------------------------------------------------- stages 1-4 together
def design(plan, grid, prep, clearance=HUB_WALL_CLEARANCE, max_k=None, log=None, deadline=None, max_routed=None,
           emit=None, hard_deadline=None):
    """Fewest hubs first, then lowest J, then smallest area gap. Groupings are routed in order of their
    lower bound; routing stops once the bound exceeds the best J, or (keeping the best design found)
    at the deadline (time.perf_counter() value) or after max_routed groupings. The best few routed
    designs then go through improve_rings (rip-up and reroute). k is None if nothing could be routed.
    emit(kind, **data), if given, receives every step for a live view (vertex ids, not coordinates)."""
    rooms = plan.device_rooms()
    blocks = feasible_blocks(plan)
    cache = {}

    T = Counter()                                    # seconds per stage (diagnostics)

    def ev(b):
        if b not in cache:
            t = time.perf_counter()
            cache[b] = evaluate_group(plan, grid, b, prep, clearance)
            T["t_hubs"] += time.perf_counter() - t
        return cache[b]

    def timings():
        return {k: round(v, 3) for k, v in T.items()} | dict(grid.counters)

    stats = dict(lower_bound_hubs=hub_lower_bound(plan), blocks=len(blocks))
    if emit:
        emit("blocks", count=len(blocks), lower_bound_hubs=stats["lower_bound_hubs"], clearance=clearance)
    for k in range(max(stats["lower_bound_hubs"], 1), (max_k or len(rooms)) + 1):
        t = time.perf_counter()
        gs = list(groupings_with_hubs(plan, k, blocks))
        T["t_groupings"] += time.perf_counter() - t
        if not gs:
            if emit:
                emit("groupings", k=k, count=0, with_hub_points=0, top=[])
            continue
        scored = sorted(((sum(ev(b)["J"] for b in g), area_gap(plan, g), g) for g in gs), key=lambda s: s[:2])
        scored = [s for s in scored if np.isfinite(s[0])]
        stats.update(hubs_tried=k, groupings=len(gs), groupings_with_hub_points=len(scored))
        if emit:
            emit("groupings", k=k, count=len(gs), with_hub_points=len(scored),
                 top=[dict(groups=g, lb=J2, gap=gap, hubs=[ev(b)["hub"] for b in g]) for J2, gap, g in scored[:12]])
        results, best_J, routed = [], np.inf, 0
        for J2, gap, g in scored:
            if J2 > best_J + LB_SLACK:
                if emit:
                    emit("log", msg=f"stop: next grouping's lower bound {J2:.2f} > best J {best_J:.2f}, "
                                    f"{len(scored) - routed} groupings skipped")
                break                                # sorted: no later grouping can win
            if results and ((deadline and time.perf_counter() > deadline) or (max_routed and routed >= max_routed)):
                stats["stopped_early"] = True        # best design kept, remaining groupings not routed
                break
            if emit:
                emit("grouping", index=routed, of=len(scored), groups=g, lb=J2, gap=gap,
                     hubs=[ev(b)["hub"] for b in g])
            evs = [ev(b) for b in g]
            t = time.perf_counter()
            loops = route_rings(grid, evs, prep, emit=emit)
            T["t_route"] += time.perf_counter() - t
            routed += 1
            if loops is None:
                if emit:
                    emit("routed", index=routed - 1, ok=False)
                continue
            m = measure(grid, loops)
            results.append(dict(J=m["J"], gap=gap, groups=g, loops=loops, lb=J2, metrics=m))
            best_J = min(best_J, m["J"])
            if emit:
                emit("routed", index=routed - 1, ok=True, J=m["J"], metrics=m, loops=loops, groups=g, best_J=best_J)
        stats.update(groupings_routed=routed)
        if log:
            log(f"{k} hubs: {len(gs)} groupings, {len(scored)} with hub points, {routed} routed")
        if results:
            key = lambda r: (round(r["J"], 3), round(r["gap"], 3))
            results.sort(key=key)
            # rip-up and reroute the best designs (the runner-up can overtake the best by up to ~2)
            F = {prep["devnode"][r]: f for r, f in prep["F"].items()}
            J_best = np.inf
            for i, res in enumerate(results[:IMPROVE_TOP]):
                if i and (res["J"] - IMPROVE_MARGIN >= J_best or (deadline and time.perf_counter() > deadline)):
                    break
                stop = time.perf_counter() + IMPROVE_TIME_S          # the best design always gets its pass
                if i and deadline:
                    stop = min(stop, deadline)
                if hard_deadline:                                    # ... unless the plan's hard limit is near
                    stop = min(stop, hard_deadline)
                if emit:
                    emit("improving", rank=i, J=res["J"], groups=res["groups"], loops=res["loops"])
                cands = [ev(b)["cand"] for b in res["groups"]] if HUB_MOVES else None
                t = time.perf_counter()
                loops = improve_rings(grid, res["loops"], F=F, deadline=stop, emit=emit, cands=cands)
                T["t_improve"] += time.perf_counter() - t
                m = measure(grid, loops)
                if emit:
                    emit("improved", rank=i, J_before=res["J"], J=m["J"])
                res.update(J_routed=res["J"], J=m["J"], loops=loops, metrics=m)
                J_best = min(J_best, m["J"])
            results.sort(key=key)
            best = results[0]
            stats.update(J_before_improve=round(best.get("J_routed", best["J"]), 4), **timings())
            if emit:
                emit("chosen", J=best["J"], groups=best["groups"], loops=best["loops"], metrics=best["metrics"])
            return dict(best, k=k, evs=[ev(b) for b in best["groups"]], stats=stats)
    stats.update(timings())
    return dict(k=None, stats=stats)


# --------------------------------------------------------------------------- validation
def validate(plan, grid, groups, loops, devnode):
    """Independent check of hard rules 1-6. Returns [(message, (hub index, leg index or None))]."""
    issues = []
    covered = [r for g in groups for r in g]
    if sorted(covered) != sorted(plan.device_rooms()):
        issues.append(("rule 1: eligible rooms not covered exactly once", None))
    terminals = set(devnode.values()) | {l["hub"] for l in loops}
    hubs = [l["hub"] for l in loops]
    if len(set(hubs)) != len(hubs):
        issues.append(("rule 6: two hubs share a point", None))
    for gi, (g, loop) in enumerate(zip(groups, loops)):
        tag = f"H{gi + 1}"
        if len(g) > MAX_DEVICES:
            issues.append((f"rule 2: {tag} has {len(g)} nodes > {MAX_DEVICES}", (gi, None)))
        if plan.area(g) > MAX_AREA + 1e-9:
            issues.append((f"rule 2: {tag} has {plan.area(g):.1f} m2 > {MAX_AREA:.0f}", (gi, None)))
        if not plan.is_connected(g):
            issues.append((f"rule 3: {tag} rooms not connected through doors/open passages", (gi, None)))
        hub = Point(grid.X[loop["hub"]], grid.Y[loop["hub"]])
        if not any(plan.rooms[r].poly.contains(hub) for r in g):
            issues.append((f"rule 6: {tag} not inside one of its own eligible rooms", (gi, None)))
        if hub.distance(plan.walls) < HUB_WALL_CLEARANCE - 1e-9:
            issues.append((f"rule 6: {tag} is {hub.distance(plan.walls):.2f} m from a wall", (gi, None)))
        if loop["hub"] in devnode.values():
            issues.append((f"rule 6: {tag} sits on a node point", (gi, None)))
        if sorted(loop["order"]) != sorted(devnode[r] for r in g):
            issues.append((f"rule 4: {tag} ring does not visit each of its nodes exactly once", (gi, None)))
        if NO_RETRACE and all(leg is not None for leg in loop["legs"]) and retraces(grid, loop):
            issues.append((f"rule 4: {tag} ring runs back along its own wire", (gi, None)))
        pts = [loop["hub"]] + list(loop["order"]) + [loop["hub"]]
        for k, leg in enumerate(loop["legs"]):
            if leg is None or leg[0] != pts[k] or leg[-1] != pts[k + 1]:
                issues.append((f"rule 4: {tag} leg {k + 1} breaks the ring", (gi, k)))
                continue
            if terminals & set(leg[1:-1]):
                issues.append((f"rule 4: {tag} leg {k + 1} runs through another hub or node point", (gi, k)))
            if not np.all(np.isin(np.abs(np.diff(leg)), [1, grid.nx])):
                issues.append((f"rule 5: {tag} leg {k + 1} leaves the grid lines", (gi, k)))
            line = LineString(zip(grid.X[leg], grid.Y[leg]))
            if line.intersects(plan.walls):
                issues.append((f"rule 5: {tag} leg {k + 1} crosses a wall or window", (gi, k)))
            if not plan.outline.buffer(1e-6).covers(line):
                issues.append((f"rule 5: {tag} leg {k + 1} leaves the inner boundary", (gi, k)))
    return issues


# --------------------------------------------------------------------------- plotting
HUB_COLOURS = ["#d1495b", "#2e86ab", "#3c9d5d", "#edae49", "#8e6c8a", "#1b998b", "#c3423f", "#5c6bc0"]


def _patch(poly, **kw):
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path
    poly = orient(poly, 1.0)
    verts, codes = [], []
    for ring in [poly.exterior, *poly.interiors]:
        c = list(ring.coords)
        verts += c
        codes += [Path.MOVETO] + [Path.LINETO] * (len(c) - 2) + [Path.CLOSEPOLY]
    return PathPatch(Path(verts, codes), **kw)


def draw_plan(ax, plan, tints=None, devices=True):
    for name, r in plan.rooms.items():
        ax.add_patch(_patch(r.poly, fc="#f3f4f6" if r.type in DEVICE_TYPES else "#e0e1e3", ec="none", zorder=0))
        if tints and name in tints:
            ax.add_patch(_patch(r.poly, fc=tints[name], alpha=0.16, ec="none", zorder=1))
        minx, miny, maxx, maxy = r.poly.bounds
        ax.text(minx + 0.15, maxy - 0.15, name, fontsize=8.5, fontweight="bold", va="top", color="#2d3a4a", zorder=12)
        ax.text(minx + 0.15, maxy - 0.55, f"{r.area:.0f} m²" if r.type in DEVICE_TYPES else "no node",
                fontsize=7.5, va="top", color="#5b6675", zorder=12)
    for poly in getattr(plan.walls, "geoms", [plan.walls]):
        ax.add_patch(_patch(poly, fc="#3f3f3f", ec="none", zorder=5))
    if plan.front_door is not None:
        x, y = plan.front_door.xy
        ax.plot(x, y, color="#a0701c", lw=5, solid_capstyle="butt", zorder=6)
        ax.text(np.mean(x), y[0] + 0.25, "Entrance", color="#a0701c", fontsize=8, fontweight="bold", ha="center", zorder=12)
    if devices:
        for r in plan.rooms.values():
            if r.device is not None:
                draw_node(ax, r.device, r.device_label or "")
    minx, miny, maxx, maxy = plan.outline.bounds
    ax.set_xlim(minx - 0.3, maxx + 0.3)
    ax.set_ylim(miny - 0.3, maxy + 0.3)
    ax.set_aspect("equal")
    ax.axis("off")


def draw_node(ax, xy, label, size=330, fontsize=7):
    ax.scatter(*xy, s=size, c="#3d4a5c", edgecolors="white", linewidths=1.5, zorder=14)
    ax.text(*xy, label, color="white", fontsize=fontsize, fontweight="bold", ha="center", va="center", zorder=15)


def draw_hub(ax, grid, n, colour, label, bad=False, size=300, fontsize=7):
    ax.scatter(grid.X[n], grid.Y[n], marker="s", s=size, c=colour, edgecolors="red" if bad else "black",
               linewidths=3 if bad else 1.2, zorder=16)
    ax.text(grid.X[n], grid.Y[n], label, color="white", fontsize=fontsize, fontweight="bold", ha="center",
            va="center", zorder=17)


def draw_rings(ax, grid, loops, bad_legs=(), lw=1.8):
    """Wires along grid lines, one arrow per leg showing the visiting direction; bad legs in red."""
    for gi, loop in enumerate(loops):
        colour = HUB_COLOURS[gi % len(HUB_COLOURS)]
        for k, leg in enumerate(loop["legs"]):
            bad = (gi, k) in bad_legs
            c = "red" if bad else colour
            ax.plot(grid.X[leg], grid.Y[leg], color=c, lw=lw * 1.45 if bad else lw, ls="--" if bad else "-",
                    solid_joinstyle="miter", zorder=8)
            pts = grid.corners(leg)
            seg = max(zip(pts[:-1], pts[1:]), key=lambda s: abs(s[1][0] - s[0][0]) + abs(s[1][1] - s[0][1]))
            (x0, y0), (x1, y1) = seg
            length = abs(x1 - x0) + abs(y1 - y0)
            if length >= 0.4:
                mx, my = (x0 + x1) / 2, (y0 + y1) / 2
                ux, uy = (x1 - x0) / length, (y1 - y0) / length
                ax.annotate("", xy=(mx + 0.15 * ux, my + 0.15 * uy), xytext=(mx - 0.15 * ux, my - 0.15 * uy),
                            arrowprops=dict(arrowstyle="-|>", color=c, lw=1.4, mutation_scale=13), zorder=9)
