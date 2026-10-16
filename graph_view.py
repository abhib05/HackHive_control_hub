import pickle
import matplotlib.pyplot as plt
import networkx as nx
from resplan_utils import plan_to_graph, add_adjacency_edges, plot_plan_and_graph

with open("ResPlan.pkl", "rb") as f:
    data = pickle.load(f)

i = int(input("Plan number (0-16999): "))
plan = dict(data[i])

# Build the graph from geometry
G = plan_to_graph(plan)          # strict version: rooms linked only through real openings
G = add_adjacency_edges(G)       # paper/Kaggle version: also links rooms sharing a wall

# Print nodes and edges
print(f"\nPlan {i}: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges\n")
print("NODES:")
for n, d in G.nodes(data=True):
    print(f"  {n:15} type={d['type']:10} area={d['area']:.1f}")
print("\nEDGES:")
for u, v, d in G.edges(data=True):
    print(f"  {u:15} <-> {v:15} {d['type']}")

# Left: graph drawn over the floor plan
plan["graph"] = G
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8))
plot_plan_and_graph(plan, ax=ax1, title=f"Plan {i} with graph")

# Right: graph on its own, nodes placed at room centres
pos = {n: (d["geometry"].centroid.x, d["geometry"].centroid.y) for n, d in G.nodes(data=True)}
edge_colors = {"via_door": "blue", "adjacency": "green", "via_window": "orange", "direct": "red"}
colors = [edge_colors.get(d["type"], "gray") for _, _, d in G.edges(data=True)]
nx.draw(G, pos, ax=ax2, with_labels=True, node_color="lightblue",
        node_size=1200, font_size=8, edge_color=colors, width=2)
ax2.set_aspect("equal")
ax2.set_title("Graph only (blue=door, green=adjacency, orange=window, red=front door)")

plt.tight_layout()
plt.show()