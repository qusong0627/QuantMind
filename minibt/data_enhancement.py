import numpy as np

__all__ = ["data_enhancement_funcs",]


def augment_shuffle_timesteps(obs: np.ndarray, n_splits_range: tuple = (2, 4), **kwargs):
    """ 时序暴力打乱：破坏时间顺序，保留全局统计规律
    操作：
    随机将时间步切割为n_splits_range指定范围内的连续片段（如(2,4)表示2-4个片段），然后随机重排这些片段的顺序。
    更激进：直接随机打乱时间步的顺序（完全破坏时序连续性），但保留每个时间步内的特征关联性。
    破坏逻辑：
    量化特征中多数是冗余或噪声（如高频波动的次要指标），核心规律往往由少数关键特征主导（如资金流向、趋势指标）。
    极端屏蔽后仍能盈利，说明模型抓住了 “决定性特征” 而非依赖冗余信息。
    参数:
        obs: 时序数据，形状为(window_size, feature_dim)
        n_splits_range: 切割片段数量的范围，格式为(min_split, max_split)，默认(2,4)
        **kwargs: 其他扩展参数

    返回:
        打乱后扁平化的数组，形状为(window_size * feature_dim,)
    """
    window_size, feature_dim = obs.shape
    min_split, max_split = n_splits_range
    # 确保切割数量不超过时间步（避免无效切割）
    max_possible_split = min(max_split, window_size -
                             1)  # 切割点最多为window_size-1个
    min_split = max(min_split, 1)  # 至少1个切割（即2个片段）
    n_splits = np.random.randint(min_split, max_possible_split + 1)

    # 生成切割点并打乱片段
    split_points = sorted(np.random.choice(range(1, window_size),
                                           size=n_splits,
                                           replace=False))
    splits = np.split(obs, split_points)
    np.random.shuffle(splits)
    return np.concatenate(splits).flatten()


def augment_shuffle_timesteps2(obs: np.ndarray, ** kwargs) -> np.ndarray:
    """
    随机打乱时间步的顺序，保留每个时间步内的特征关联
    目标：
    把 10 行（时间步）随机打乱顺序（如原顺序 [0,1,2,...,9]→随机变为 [3,7,1,9,...,2]）；
    每行内部的 21 个特征保持不变（即每个时间步的特征关联性不被破坏）；
    打乱后重新扁平化回(210,)，用于模型输入。

    核心作用
    通过完全破坏时间顺序，强制模型放弃对 “时序连续性” 的依赖（例如避免过拟合 “t 时刻上涨→t+1 时刻必上涨” 的短期噪声规律），
    转而学习不依赖时间顺序的本质规律（例如 “无论时间先后，当特征 A> 阈值且特征 B < 阈值时，后续趋势反转概率高”）。
    这种训练方式能显著提升模型在实盘（时序常被突发因素打乱）中的泛化能力。
    参数:
        obs: 时序数据，形状为(window_size, feature_dim)
        **kwargs: 其他扩展参数

    返回:
        打乱时间步后的扁平化观测值，形状为(window_size * feature_dim,)
    """
    window_size, feature_dim = obs.shape
    # 生成随机排列索引（打乱时间步顺序）
    shuffle_indices = np.random.permutation(window_size)
    # 按随机索引重新排列时间步
    obs_shuffled = obs[shuffle_indices]
    return obs_shuffled.flatten()


def augment_mask_features(obs: np.ndarray, survival_rate: float = 0.2, ** kwargs):
    """ 特征毁灭性屏蔽：保留关键特征的 “幸存者偏差”
    操作：
    随机屏蔽(1-survival_rate)比例的特征（置0），强制保留至少1个特征。

    参数:
        obs: 时序数据，形状为(window_size, feature_dim)
        survival_rate: 保留特征的比例（0-1之间），默认0.2（即保留20%）
        **kwargs: 其他扩展参数

    返回:
        屏蔽后扁平化的数组，形状为(window_size * feature_dim,)
    """
    # 检查参数有效性
    if not (0 < survival_rate <= 1):
        raise ValueError("survival_rate必须在(0, 1]范围内")

    obs_flat = obs.flatten()
    # 生成保留掩码
    mask = np.random.choice([0, 1], size=obs_flat.shape,
                            p=[1 - survival_rate, survival_rate])
    # 强制保留至少1个特征
    if mask.sum() == 0:
        mask[np.random.randint(obs_flat.shape[0])] = 1
    return obs_flat * mask


def augment_distort_values(obs: np.ndarray, distort_ratio: float = 0.5,
                           scale_range: tuple = (-3, 3), flip_ratio: float = 0.3, ** kwargs):
    """数值极端扭曲：破坏量级，保留相对关系
    操作：
    1. 随机选择distort_ratio比例的特征，乘以10^k（k在scale_range范围内）
    2. 随机选择flip_ratio比例的特征进行符号反转
    破坏逻辑：
    量化交易中价格、成交量等特征的绝对量级常随市场状态变化（如牛市 / 熊市的成交量量级不同），
    但特征间的相对关系（如 “特征 A 上涨时特征 B 也上涨”）更稳定。模型若能忽略量级噪声、关注相对关系，
    则泛化能力更强。
    参数:
        obs: 时序数据，形状为(window_size, feature_dim)
        distort_ratio: 进行量级扭曲的特征比例（0-1），默认0.5
        scale_range: 缩放因子指数范围，格式为(min_k, max_k)，默认(-3, 3)
        flip_ratio: 进行符号反转的特征比例（0-1），默认0.3
        **kwargs: 其他扩展参数

    返回:
        扭曲后扁平化的数组，形状为(window_size * feature_dim,)
    """
    # 检查参数有效性
    if not (0 <= distort_ratio <= 1):
        raise ValueError("distort_ratio必须在[0, 1]范围内")
    if not (0 <= flip_ratio <= 1):
        raise ValueError("flip_ratio必须在[0, 1]范围内")
    if scale_range[0] >= scale_range[1]:
        raise ValueError("scale_range必须满足min_k < max_k")

    obs_flat = obs.flatten()
    # 量级扭曲
    distort_mask = np.random.choice([True, False], size=obs_flat.shape,
                                    p=[distort_ratio, 1 - distort_ratio])
    scales = 10 ** np.random.uniform(scale_range[0],
                                     scale_range[1], size=distort_mask.sum())
    obs_flat[distort_mask] *= scales

    # 符号反转
    flip_mask = np.random.choice([True, False], size=obs_flat.shape,
                                 p=[flip_ratio, 1 - flip_ratio])
    obs_flat[flip_mask] *= -1
    return obs_flat


def augment_cross_contamination(obs: np.ndarray, history_obs=None,
                                contaminate_ratio: float = 0.3, ** kwargs):
    """跨时间步特征污染：破坏时序关联性，保留特征分布
    操作：
    随机选择contaminate_ratio比例的时间步，替换为历史数据中的随机时间步特征
    破坏逻辑：
    实盘交易中常出现数据延迟、错位（如行情数据更新延迟导致时序错位），
    模型需在这种 “污染” 中识别真实的特征关联（如即使某步特征被污染，仍能通过前后步推断趋势）。
    参数:
        obs: 时序数据，形状为(window_size, feature_dim)
        history_obs: 历史观测列表（每个元素为扁平化数组），用于抽取污染数据，默认None
        contaminate_ratio: 被污染的时间步比例（0-1），默认0.3
        **kwargs: 其他扩展参数

    返回:
        污染后扁平化的数组，形状为(window_size * feature_dim,)
    """
    if not (0 <= contaminate_ratio <= 1):
        raise ValueError("contaminate_ratio必须在[0, 1]范围内")

    window_size, feature_dim = obs.shape
    # 生成污染掩码
    contaminate_mask = np.random.choice([True, False], size=window_size,
                                        p=[contaminate_ratio, 1 - contaminate_ratio])

    if history_obs is not None and len(history_obs) > 0:
        # 从历史数据中随机抽取特征替换
        for t in np.where(contaminate_mask)[0]:
            # 随机选择一个历史观测
            random_obs = history_obs[np.random.randint(len(history_obs))]
            # 还原历史观测的时序结构并随机选一个时间步
            random_obs_reshaped = random_obs.reshape(window_size, feature_dim)
            obs[t] = random_obs_reshaped[np.random.randint(window_size)]

    return obs.flatten()


def augment_collapse_features(obs: np.ndarray, n_clusters_range: tuple = (3, 5), ** kwargs):
    """特征维度坍缩：破坏特征独立性，保留聚合信息
    操作：
    将特征合并为n_clusters_range范围内的聚合特征，再扩展回原维度
    破坏逻辑：
    量化特征中存在大量冗余（如不同周期的均线指标高度相关），本质规律可能隐藏在特征的聚合关系中（如 “多周期均线同时上涨”）。
    坍缩后仍能识别规律，说明模型学到了抽象的聚合模式。
    参数:
        obs: 时序数据，形状为(window_size, feature_dim)
        n_clusters_range: 聚合聚类数量范围，格式为(min_cluster, max_cluster)，默认(3,5)
        **kwargs: 其他扩展参数

    返回:
        坍缩后扁平化的数组，形状为(window_size * feature_dim,)
    """
    window_size, feature_dim = obs.shape
    min_cluster, max_cluster = n_clusters_range

    # 确保聚类数量合理（不超过特征数，至少1个）
    max_possible_cluster = min(max_cluster, feature_dim)
    min_cluster = max(min_cluster, 1)
    n_clusters = np.random.randint(min_cluster, max_possible_cluster + 1)

    # 生成聚类（确保每个聚类至少1个特征）
    cluster_ids = np.arange(feature_dim)
    np.random.shuffle(cluster_ids)
    # 切割聚类（避免空聚类）
    split_points = np.random.choice(range(1, feature_dim),
                                    # 若n_clusters=1则不切割
                                    size=max(0, n_clusters - 1),
                                    replace=False)
    split_points.sort()
    clusters = np.split(cluster_ids, split_points)

    # 映射聚类ID
    cluster_map = np.zeros(feature_dim, dtype=int)
    for c_idx, cluster in enumerate(clusters):
        cluster_map[cluster] = c_idx

    # 计算聚合特征（均值）
    collapsed = np.array([
        obs[:, cluster_map == c].mean(axis=1)
        for c in range(n_clusters)
    ]).T  # 形状为(window_size, n_clusters)

    # 扩展回原特征维度
    base_repeats = feature_dim // n_clusters
    remainder = feature_dim % n_clusters
    repeats = [base_repeats + 1] * remainder + \
        [base_repeats] * (n_clusters - remainder)

    expanded = np.concatenate([
        np.repeat(collapsed[:, [c]], repeats=repeats[c], axis=1)
        for c in range(n_clusters)
    ], axis=1)

    return expanded.flatten()


def augment_observation(obs: np.ndarray, mask_prob: float = 0.1):
    """
    强化学习（RL）观测值数据增强：随机特征掩码
    随机屏蔽指定概率的特征值（置0），用于提升RL模型的鲁棒性，避免过拟合

    Args:
        obs (np.ndarray): 原始观测值数组（形状：[特征数] 或 [时间步, 特征数]）
        mask_prob (float): 特征被屏蔽的概率（范围：0~1），默认0.1（10%）
        target_shape (tuple): 目标形状，如果不为None，则重塑为此形状

    Returns:
        np.ndarray: 掩码处理后的观测值数组
    """
    # 验证掩码概率有效性
    assert 0 <= mask_prob <= 1, f"掩码概率mask_prob必须在0~1之间，当前值：{mask_prob}"

    obs_flat = obs.flatten()
    # 生成二进制掩码：1表示保留特征，0表示屏蔽（屏蔽概率为mask_prob）
    mask = np.random.binomial(1, 1 - mask_prob, size=obs_flat.shape)

    # 应用掩码
    return obs_flat * mask


data_enhancement_funcs = [
    augment_shuffle_timesteps,
    augment_shuffle_timesteps2,
    augment_mask_features,
    augment_distort_values,
    augment_cross_contamination,
    augment_collapse_features,
    augment_observation,
]
