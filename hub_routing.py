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
  stage 3  route each ring leg by leg with overlap/crossing penalties for every visiting order; orders
           and groupings whose lower bound cannot beat the best J found are skipped
  stage 4  exact J per routed grouping, pick the lowest (then smallest area gap), validate rules 1-6
"""
import itertools
import math
import time
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

# direction codes: 0 = +x (E), 1 = +y (N), 2 = -x (W), 3 = -y (S)
OPP = np.array([2, 3, 0, 1])
BENDS = 1 - np.eye(4, dtype=int)           # any change of direction is one bend (slide 6: a reversal counts once)
TURN = W_BEND * BENDS
# a wire passing a vertex uses two of its four edges: the pass type. Two passes cross (or touch) when
# they share no edge, i.e. when their types are complementary: E-W / N-S, E-N / W-S, E-S / W-N.
PASS_TYPE = np.array([[-1, 2, 0, 4], [2, -1, 5, 1], [0, 5, -1, 3], [4, 1, 3, -1]])   # [edge dir a, edge dir b]
COMP = np.array([1, 0, 3, 2, 5, 4])


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
                 zone_mode="excluded", oversize_ok=False):
        self.rooms = {r.name: r for r in rooms}
        self.walls = walls
        self.front_door = front_door
        self.outline = outline if outline is not None else unary_union([r.poly for r in rooms])
        self.zone_mode = zone_mode
        self.oversize_ok = oversize_ok        # allow a single room > 100 m2 as its own zone (flagged)
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

    def area(self, group):
        return sum(self.rooms[r].area for r in group)

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

    def __init__(self, plan, h=0.1, clearance=0.01, extra_x=(), extra_y=(), merge_tol=0.005):
        self.h = h
        minx, miny, maxx, maxy = plan.outline.bounds
        self.xs = self._lines(minx, maxx, h, extra_x, merge_tol)
        self.ys = self._lines(miny, maxy, h, extra_y, merge_tol)
        self.nx, self.ny = len(self.xs), len(self.ys)
        nx, ny, N = self.nx, self.ny, self.nx * self.ny
        self.N = N
        X, Y = np.meshgrid(self.xs, self.ys)
        self.X, self.Y = X.ravel(), Y.ravel()

        obst, outline = plan.walls, plan.outline
        shapely.prepare(outline)
        pts = shapely.points(self.X, self.Y)
        self.wall_dist = shapely.distance(obst, pts)
        self.free = shapely.covers(outline, pts) & (self.wall_dist >= clearance)
        self.room_inner = {n: shapely.contains_properly(r.poly, pts) for n, r in plan.rooms.items()}

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

    @staticmethod
    def _lines(lo, hi, h, extra, tol, same=0.0018):
        """Regular lines every h, plus the extra coordinates. Extra coordinates closer together than
        `same` become one line at their midpoint (each point then lies < 1 mm from it, the checker's
        tolerance), so no two lines are within 1 mm of each other."""
        reg = np.round(np.arange(math.floor(lo / h), math.ceil(hi / h) + 1) * h, 6)
        merged = []
        for x in np.sort(np.asarray(list(extra), float)):
            if merged and x - merged[-1][0] <= same:
                merged[-1][1] = x
            else:
                merged.append([x, x])
        extra = np.array([(a + b) / 2 for a, b in merged])
        if len(extra):
            reg = np.concatenate([reg[np.abs(reg[:, None] - extra[None, :]).min(1) > tol], extra])
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
        # one extra state S = 4N: the start of a leg that continues a ring (see route); its two arcs are
        # filled in per call, so the row exists in every graph
        self._indices = np.concatenate([C[order], [0, 1]]).astype(np.int32)
        cnt = np.bincount(R, minlength=4 * self.N)
        self._indptr = np.concatenate([[0], np.cumsum(cnt), [cnt.sum() + 2]]).astype(np.int32)
        self._arc, self._turn = A[order], T[order]
        # each arc leaves vertex _esrc; together with the direction the wire arrived with, it fixes the
        # pass type at that vertex (which two of its edges the wire uses), see pass_masks
        din = R[order] % 4
        self._esrc = self.m_src[self._arc]
        self._ecomp = (1 << COMP[PASS_TYPE[OPP[din], self.m_dir[self._arc]]]).astype(np.uint8)

    def state_graph(self, edge_pen=None, node_pen=None, arc_pen=None):
        w = self.elen[self.m_eid]
        if edge_pen is not None:
            w = w + edge_pen[self.m_eid]
        if node_pen is not None:
            w = w + node_pen[self.m_dst]
        data = w[self._arc] + self._turn
        if arc_pen is not None:
            data = data + arc_pen
        data = np.concatenate([data, [BLOCKED, BLOCKED]])
        S = 4 * self.N + 1
        return csr_matrix((data, self._indices.copy(), self._indptr), shape=(S, S))

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

    def route(self, G, a, b, src_dir=None, next_dir=None, end_cost=None, limit=np.inf):
        """Cheapest wire a -> b. src_dir: direction the wire arrived at a with; the turn at a is charged,
        a reversal (straight back) included, as one bend. next_dir: direction the wire leaves b with
        afterwards (turn at b is charged); end_cost: extra cost per arrival direction at b (e.g. the
        rest of the ring), instead of next_dir. limit: search radius in cost; if the best end is not
        clearly inside it, the search is repeated without a limit, so the result is always exact."""
        S = 4 * self.N
        if src_dir is None:
            idx = np.arange(4 * a, 4 * a + 4)
        else:
            # start state S: "arrived with src_dir" (straight on free, 90 deg turn 0.5, no reversal) or
            # "arrived with the opposite direction" + 0.5 (only useful for going straight back)
            st = sorted([(4 * a + src_dir, 0.0), (4 * a + int(OPP[src_dir]), W_BEND)])
            G.indices[-2:] = [st[0][0], st[1][0]]
            G.data[-2:] = [st[0][1], st[1][1]]
            idx = np.array([S])
        dist, pred, _ = dijkstra(G, directed=True, indices=idx, min_only=True, return_predecessors=True,
                                 limit=limit)
        if end_cost is None:
            end_cost = 0 if next_dir is None else TURN[:, next_dir]
        cost = dist[4 * b:4 * b + 4] + end_cost
        s = 4 * b + int(np.argmin(cost))
        if np.isfinite(limit) and not dist[s] <= limit - 2 * W_BEND:
            return self.route(G, a, b, src_dir, next_dir, end_cost)      # end costs differ by <= 0.5
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
    return max(math.ceil(len(rooms) / MAX_DEVICES), math.ceil(plan.area(rooms) / MAX_AREA - 1e-9))


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
    area = {r: plan.rooms[r].area for r in rooms}

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
    return dict(devnode=devnode, F={r: grid.first_move_dist(n) for r, n in devnode.items()})


def _cycle_perms(k):
    if k == 1:
        return [(0,)]
    return [p for p in itertools.permutations(range(k)) if p[0] < p[-1]]


def _minplus(M, T):
    return (M[:, :, None] + T[None, :, :]).min(1)


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
    Ain, Aout = [], []
    for i in range(k):
        Fi = F[i][:, cand, :]                                    # [b, c, a]
        first = Fi[OPP][:, :, OPP].transpose(1, 0, 2)            # [c, a0, b']  (reversed path hub -> node)
        Ain.append((TURN[None, :, None, :] + first[:, None, :, :]).min(3))
        Aout.append((TURN[None, :, :, None] + Fi.transpose(1, 0, 2)[:, None, :, :]).min(2))

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

    def ring(s, t, M, sel=slice(None)):
        T1 = (Ain[s][sel][:, :, :, None] + M[None, None, :, :]).min(2)         # [c, ah, al]
        return (T1 + Aout[t][sel].transpose(0, 2, 1)).min((1, 2))

    J = np.full(len(cand), np.inf)
    for (s, t), M in ends.items():
        J = np.minimum(J, ring(s, t, M))
    if not np.isfinite(J).any():
        return out
    dc = np.hypot(grid.X[cand] - cx, grid.Y[cand] - cy)
    start = int(np.argmin(dc))                  # node centroid, moved to the nearest legal hub vertex
    tie = np.nonzero(J <= J.min() + 1e-4)[0]
    opt = int(tie[np.argmin(dc[tie])])          # among equal-cost vertices, closest to the centroid
    sel = slice(opt, opt + 1)
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
        for loop in loops:
            for leg in loop["legs"]:
                self.add(leg)

    def add(self, leg):
        p = np.asarray(leg, dtype=int)
        np.add.at(self.edge_use, self.grid.edge_ids(p), 1)
        if len(p) > 2:
            d = self.grid._dirs(p[:-1], p[1:])
            t = PASS_TYPE[OPP[d[:-1]], d[1:]]
            ok = t >= 0
            np.bitwise_or.at(self.mask, p[1:-1][ok], (1 << t[ok]).astype(np.uint8))

    def copy(self):
        u = Usage.__new__(Usage)
        u.grid, u.edge_use, u.mask = self.grid, self.edge_use.copy(), self.mask.copy()
        return u

    def crossing_points(self):
        m = self.mask
        return ((m & 3) == 3) | ((m & 12) == 12) | ((m & 48) == 48)


def _penalised_graph(grid, usage, target, terminals=None):
    """Routing costs = exact increase of J: length + 0.5 per turn, overlap 1/m on edges already used,
    5 for passing a vertex in a way that crosses or touches a wire already there (no shared edge),
    unless that vertex is a hub / node or already a crossing point. Other hubs and nodes are blocked."""
    no_x = ~usage.crossing_points()
    node_pen = np.zeros(grid.N)
    if terminals is not None:
        no_x[terminals] = False
        node_pen[terminals] = BLOCKED
    node_pen[target] = 0.0
    src = grid._esrc
    arc_pen = W_CROSS * (((usage.mask[src] & grid._ecomp) != 0) & no_x[src])
    return grid.state_graph(W_OVERLAP * grid.elen * (usage.edge_use > 0), node_pen, arc_pen)


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


def sequential_loop(grid, hub, order, base_loops, terminals=None, penalise=True, F=None, base_usage=None):
    """Route the ring leg by leg in visiting order. With penalise, every leg pays the exact J increase
    it causes against the wires already routed (overlap 1/m, crossing 5, see _penalised_graph). Turns
    at hub/nodes are charged. With F (node distance tables keyed by node vertex) each leg also weighs
    the rest of the ring per arrival direction, so it does not arrive at a node facing the wrong way,
    and its search is bounded by its unhindered cost (exact: it re-runs unbounded if needed). Returns
    None if a leg cannot be routed."""
    usage = (base_usage or Usage(grid, base_loops)).copy()
    pairs = _loop_points(hub, order)
    legs = []
    for i, (a, b) in enumerate(pairs):
        G = _penalised_graph(grid, usage, b, terminals) if penalise else grid.G0
        src = grid.last_dir(legs[-1]) if legs else None
        nxt = grid.first_dir(legs[0]) if i == len(pairs) - 1 else None
        end, limit = None, np.inf
        if F is not None:
            n, m = (b, a) if b in F else (a, b)
            if n in F:
                limit = 1.5 * float(F[n][:, m, :].min()) + 12.0
            if i < len(pairs) - 1:
                end = _cost_to_go(F, hub, order, i, grid.first_dir(legs[0]) if legs else None)
        leg = grid.route(G, a, b, src, nxt, end, limit) or grid.route(G, a, b, None, nxt, end)
        if leg is None:
            return None
        legs.append(leg)
        usage.add(leg)
    return dict(hub=hub, order=list(order), legs=legs)


def best_order_loop(grid, hub, devs, base_loops, terminals=None, perm_lb=None, F=None):
    """Sequential routing for every visiting order (both directions); keep the lowest J. With perm_lb
    (stage-2 cost of each order at this hub) orders are tried cheapest first and the search stops once
    no remaining order can beat the best J found."""
    orders = [tuple(p) for p in itertools.permutations(devs)]
    J_base = measure(grid, base_loops, terminals)["J"] if base_loops else 0.0
    base_usage = Usage(grid, base_loops)
    if perm_lb:
        orders.sort(key=lambda o: perm_lb.get(o, 0.0))
    best, best_J = None, np.inf
    for order in orders:
        if perm_lb and J_base + perm_lb.get(order, 0.0) - LB_SLACK >= best_J:
            break
        loop = sequential_loop(grid, hub, order, base_loops, terminals, F=F, base_usage=base_usage)
        if loop is None:
            continue
        J = measure(grid, base_loops + [loop], terminals)["J"]
        if J < best_J - 1e-9:
            best, best_J = loop, J
    return best


def route_rings(grid, evs, prep=None):
    """Stage 3 (method 3): for each hub in turn, route its ring sequentially with overlap/crossing
    penalties for every visiting order and keep the order with the lowest exact J, counting
    interactions with the rings already routed. Returns None if a ring cannot be routed."""
    terminals = np.array(sorted({ev["hub"] for ev in evs} | {d for ev in evs for d in ev["dev"]}))
    F = None if prep is None else {prep["devnode"][r]: f for r, f in prep["F"].items()}
    loops = []
    for ev in evs:
        loop = best_order_loop(grid, ev["hub"], ev["dev"], loops, terminals, ev.get("perm_lb"), F)
        if loop is None:
            return None
        loops.append(loop)
    return loops


# --------------------------------------------------------------------------- stages 1-4 together
def design(plan, grid, prep, clearance=HUB_WALL_CLEARANCE, max_k=None, log=None, deadline=None, max_routed=None):
    """Fewest hubs first, then lowest J, then smallest area gap. Groupings are routed in order of their
    lower bound; routing stops once the bound exceeds the best J, or (keeping the best design found)
    at the deadline (time.perf_counter() value) or after max_routed groupings. k is None if nothing
    could be routed."""
    rooms = plan.device_rooms()
    blocks = feasible_blocks(plan)
    cache = {}

    def ev(b):
        if b not in cache:
            cache[b] = evaluate_group(plan, grid, b, prep, clearance)
        return cache[b]

    stats = dict(lower_bound_hubs=hub_lower_bound(plan), blocks=len(blocks))
    for k in range(max(stats["lower_bound_hubs"], 1), (max_k or len(rooms)) + 1):
        gs = list(groupings_with_hubs(plan, k, blocks))
        if not gs:
            continue
        scored = sorted(((sum(ev(b)["J"] for b in g), area_gap(plan, g), g) for g in gs), key=lambda s: s[:2])
        scored = [s for s in scored if np.isfinite(s[0])]
        stats.update(hubs_tried=k, groupings=len(gs), groupings_with_hub_points=len(scored))
        results, best_J, routed = [], np.inf, 0
        for J2, gap, g in scored:
            if J2 > best_J + LB_SLACK:
                break                                # sorted: no later grouping can win
            if results and ((deadline and time.perf_counter() > deadline) or (max_routed and routed >= max_routed)):
                stats["stopped_early"] = True        # best design kept, remaining groupings not routed
                break
            loops = route_rings(grid, [ev(b) for b in g], prep)
            routed += 1
            if loops is None:
                continue
            m = measure(grid, loops)
            results.append(dict(J=m["J"], gap=gap, groups=g, loops=loops, lb=J2, metrics=m))
            best_J = min(best_J, m["J"])
        stats.update(groupings_routed=routed)
        if log:
            log(f"{k} hubs: {len(gs)} groupings, {len(scored)} with hub points, {routed} routed")
        if results:
            results.sort(key=lambda r: (round(r["J"], 3), round(r["gap"], 3)))
            return dict(results[0], k=k, evs=[ev(b) for b in results[0]["groups"]], stats=stats)
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
