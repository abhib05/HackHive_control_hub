import pickle
import matplotlib.pyplot as plt
from resplan_utils import plot_plan

with open("ResPlan.pkl", "rb") as f:
    data = pickle.load(f)

plot_plan(data[0])
plt.show()