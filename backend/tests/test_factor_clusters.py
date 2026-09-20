"""因子去重簇（clusters.py）单测 —— 纯函数，无 IO。

口径要点：
1. 用 |ρ| 判同源（负相关同样是同源 —— 反向因子只是符号约定不同）
2. 连通分量聚类（允许链式：A~B、B~C 会并成一簇，成员与代表的相关性会单独给出）
3. 代表按保留口径选（默认 |ICIR| 最大，ICIR 相同再看 |IC|）
"""

from __future__ import annotations

from backend.services.engine.factor_report.clusters import cluster_by_correlation, summarize

NAMES = ["f_strong", "f_dup", "f_neg", "f_weak_dup", "f_alone"]

# 相关性结构：
#   f_strong ~ f_dup (0.97) ~ f_neg (-0.96，负相关也算同源)
#   f_weak_dup 与 f_strong 只有 0.85（阈值 0.9 时不该并进来）
#   f_alone 谁都不沾
MATRIX = [
    [1.00, 0.97, -0.96, 0.85, 0.05],
    [0.97, 1.00, -0.95, 0.84, 0.03],
    [-0.96, -0.95, 1.00, -0.83, -0.02],
    [0.85, 0.84, -0.83, 1.00, 0.01],
    [0.05, 0.03, -0.02, 0.01, 1.00],
]

METRICS = {
    "f_strong": {"ic_mean": 0.05, "icir": 0.60, "turnover": 0.3, "display_name": "强因子"},
    "f_dup": {"ic_mean": 0.048, "icir": 0.40, "turnover": 0.5},  # fidelity: allow-limit-threshold — 非阈值：因子 IC 均值夹具
    "f_neg": {"ic_mean": -0.049, "icir": -0.55, "turnover": 0.2},  # fidelity: allow-limit-threshold — 非阈值：因子 IC 均值夹具
    "f_weak_dup": {"ic_mean": 0.04, "icir": 0.30, "turnover": 0.6},
    "f_alone": {"ic_mean": 0.02, "icir": 0.25, "turnover": 0.1},
}


def test_按绝对值聚类_负相关也算同源():
    clusters = cluster_by_correlation(NAMES, MATRIX, METRICS, threshold=0.9)

    assert len(clusters) == 1
    names = {m["name"] for m in clusters[0]["members"]}
    assert names == {"f_strong", "f_dup", "f_neg"}   # 三者互强相关
    assert "f_weak_dup" not in names                  # 0.85 < 0.9
    assert "f_alone" not in names


def test_代表取_ICIR_绝对值最大():
    clusters = cluster_by_correlation(NAMES, MATRIX, METRICS, threshold=0.9)
    rep = clusters[0]["representative"]

    assert rep == "f_strong"          # |0.60| > |−0.55| > |0.40|
    assert clusters[0]["size"] == 3
    rep_row = next(m for m in clusters[0]["members"] if m["name"] == rep)
    assert rep_row["is_rep"] is True
    assert rep_row["display_name"] == "强因子"


def test_成员带与代表的相关性_便于识别间接相连():
    clusters = cluster_by_correlation(NAMES, MATRIX, METRICS, threshold=0.9)
    by_name = {m["name"]: m for m in clusters[0]["members"]}

    assert by_name["f_dup"]["corr_to_rep"] == 0.97
    assert by_name["f_neg"]["corr_to_rep"] == -0.96   # 反向同源


def test_阈值调低后弱重复被并入():
    clusters = cluster_by_correlation(NAMES, MATRIX, METRICS, threshold=0.8)
    names = {m["name"] for m in clusters[0]["members"]}

    assert "f_weak_dup" in names
    assert clusters[0]["size"] == 4


def test_保留口径可切换():
    clusters = cluster_by_correlation(NAMES, MATRIX, METRICS, threshold=0.9, keep="abs_ic")
    # |IC| 口径下 f_strong(0.050) 仍最大
    assert clusters[0]["representative"] == "f_strong"


def test_无同源因子时返回空():
    matrix = [[1.0, 0.1], [0.1, 1.0]]
    assert cluster_by_correlation(["a", "b"], matrix, {}, threshold=0.9) == []


def test_汇总口径():
    clusters = cluster_by_correlation(NAMES, MATRIX, METRICS, threshold=0.9)
    s = summarize(len(NAMES), clusters)

    assert s["n_total"] == 5
    assert s["n_clusters"] == 1
    assert s["n_duplicates"] == 2      # 3 个成员里 2 个是重复项
    assert s["n_keep"] == 3
    assert s["largest_cluster"] == 3
