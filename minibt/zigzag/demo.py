from zigzag import zigzag
import pathlib
import sys
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use("TkAgg")
sys.path.append("%s/zigzag" % pathlib.Path().absolute())


def plot_pivots(X, pivots):
    plt.xlim(0, len(X))
    plt.ylim(X.min()*0.99, X.max()*1.01)
    plt.plot(np.arange(len(X)), X, 'k:', alpha=0.5)
    plt.plot(np.arange(len(X))[pivots != 0], X[pivots != 0], 'k-')
    plt.scatter(np.arange(len(X))[pivots == 1], X[pivots == 1], color='g')
    plt.scatter(np.arange(len(X))[pivots == -1], X[pivots == -1], color='r')


np.random.seed(1997)
X = np.cumprod(1 + np.random.randn(100) * 0.01)
pivots = zigzag.peak_valley_pivots(X, 0.03, -0.03)

plot_pivots(X, pivots)
plt.show()

modes = zigzag.pivots_to_modes(pivots)
print(pd.Series(X).pct_change().groupby(modes).describe().unstack())
print(zigzag.compute_segment_returns(X, pivots))
