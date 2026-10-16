import pickle
import matplotlib.pyplot as plt
from resplan_utils import plan_to_graph, add_adjacency_edges, plot_plan_and_graph

with open("ResPlan.pkl", "rb") as f:
    data = pickle.load(f)

i = int(input("Plan number (0-16999): "))

# Before: strict graph (rooms linked only through real openings)
G_before = plan_to_graph(dict(data[i]))

# After: add_adjacency_edges changes the graph in place, so work on a copy
G_after = add_adjacency_edges(G_before.copy())

# Print what changed
before_edges = {frozenset((u, v)): d["type"] for u, v, d in G_before.edges(data=True)}
after_edges  = {frozenset((u, v)): d["type"] for u, v, d in G_after.edges(data=True)}

print(f"\nBefore: {len(before_edges)} edges   After: {len(after_edges)} edges\n")
print("ADDED:")
for e, t in after_edges.items():
    if e not in before_edges:
        print(f"  {' <-> '.join(sorted(e)):35} {t}")
print("\nRELABELLED:")
for e, t in after_edges.items():
    if e in before_edges and before_edges[e] != t:
        print(f"  {' <-> '.join(sorted(e)):35} {before_edges[e]} -> {t}")

# Plot side by side
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8))
plan_a = dict(data[i]); plan_a["graph"] = G_before
plan_b = dict(data[i]); plan_b["graph"] = G_after
plot_plan_and_graph(plan_a, ax=ax1, title=f"Plan {i}: before (strict)")
plot_plan_and_graph(plan_b, ax=ax2, title=f"Plan {i}: after add_adjacency_edges")
plt.show()
