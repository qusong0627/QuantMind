import torch as th
import torch.nn as nn


class LogReturnLoss(nn.Module):
    """## 对数收益率损失（LogReturnLoss）
    - 适用场景：单资产趋势跟踪、长期复利收益优化
    - 核心逻辑：通过最大化对数收益率（而非简单收益率），更贴合复利增长逻辑，对极端收益的惩罚更合理。
    Args:
        pred_rets: 预测的资产收益率序列，形状为 (batch_size, time_steps)
                    （如股票每日涨跌幅，需满足 pred_rets > -1 + eps）
    Returns:
        负的平均对数收益率（用于最小化损失即最大化对数收益）
    ### 使用示例
    >>> loss_fn = LogReturnLoss()
    >>> pred_rets = th.tensor([[0.02, -0.01, 0.03]])  # 预测收益率（涨2%，跌1%，涨3%）
    >>> loss = loss_fn(pred_rets)  # 输出：-mean(log(1.02), log(0.99), log(1.03))
    """

    def __init__(self, eps=1e-6):
        super().__init__()
        self.eps = eps  # 避免log(0)或log负数（收益率≥-1+eps）

    def forward(self, pred_rets: th.Tensor) -> th.Tensor:
        # 确保收益率不小于-1（避免log(0)）
        pred_rets = th.clamp(pred_rets, min=-1 + self.eps)
        # 对数收益率：log(1 + 收益率)
        log_rets = th.log(1 + pred_rets)
        # 损失 = 负的平均对数收益率（最小化损失即最大化收益）
        return -th.mean(log_rets)


class SharpeRatioLoss(nn.Module):
    """## 夏普比率调整损失（SharpeRatioLoss）
    - 适用场景：多资产配置、风险调整后收益优化
    - 核心逻辑：直接优化 “收益 - 风险比”，通过惩罚波动率避免策略过度追求高收益而忽视风险。
    Args:
        pred_rets: 预测的组合收益率序列，形状为 (batch_size, time_steps)
    Returns:
        损失 = -平均收益 + lambda_risk * 波动率（最小化损失即最大化夏普比率）
    ### 使用示例
    >>> loss_fn = SharpeRatioLoss(lambda_risk=0.5)
    >>> pred_rets = th.tensor([[0.02, 0.01, 0.03], [0.05, -0.04, 0.06]])  # 两个组合的收益率
    >>> loss = loss_fn(pred_rets)  # 对高收益高波动的组合施加惩罚
    """

    def __init__(self, lambda_risk=1.0, eps=1e-6):
        super().__init__()
        self.lambda_risk = lambda_risk  # 波动率惩罚系数
        self.eps = eps  # 避免波动率为0时除零

    def forward(self, pred_rets: th.Tensor) -> th.Tensor:

        mean_ret = th.mean(pred_rets, dim=1)  # 每个样本的平均收益 (batch_size,)
        vol_ret = th.std(pred_rets, dim=1) + self.eps  # 每个样本的波动率 (batch_size,)
        # 简化夏普比率优化：最大化（mean/vol）≈ 最小化（-mean + lambda*vol）
        loss = -mean_ret + self.lambda_risk * vol_ret
        return th.mean(loss)  # 平均批次损失


class MaxDrawdownLoss(nn.Module):
    """## 最大回撤惩罚损失（MaxDrawdownLoss）
    - 适用场景：保守型策略、资产管理（控制极端亏损）
    - 核心逻辑：惩罚策略从峰值到谷值的最大跌幅，避免 “赚快钱但亏大钱”。
    Args:
        pred_rets: 预测的收益率序列，形状为 (batch_size, time_steps)
    Returns:
        损失 = -平均收益 + lambda_dd * 最大回撤（最小化损失即控制回撤）
    ### 使用示例
    >>> loss_fn = MaxDrawdownLoss(lambda_dd=3.0)
    >>> pred_rets = th.tensor([[0.05, 0.03, -0.08, -0.02]])  # 先涨后跌，有明显回撤
    >>> loss = loss_fn(pred_rets)  # 对大回撤施加高惩罚
    """

    def __init__(self, lambda_dd=2.0):
        super().__init__()
        self.lambda_dd = lambda_dd  # 最大回撤惩罚系数

    def forward(self, pred_rets: th.Tensor) -> th.Tensor:

        # 计算累积收益（假设初始资金为1）
        cum_rets = th.cumprod(1 + pred_rets, dim=1)  # (batch_size, time_steps)
        # 计算每个时刻的累计峰值（到该时刻为止的最大累积收益）
        peak_rets = th.maximum.accumulate(
            cum_rets, dim=1)  # (batch_size, time_steps)
        # 计算每个时刻的回撤：(峰值 - 当前累积收益) / 峰值
        drawdown = (peak_rets - cum_rets) / \
            peak_rets  # (batch_size, time_steps)
        # 每个样本的最大回撤
        max_dd = th.max(drawdown, dim=1).values  # (batch_size,)
        # 平均收益
        mean_ret = th.mean(pred_rets, dim=1)  # (batch_size,)
        # 损失 = 负收益 + 回撤惩罚
        loss = -mean_ret + self.lambda_dd * max_dd
        return th.mean(loss)


class CVaRLoss(nn.Module):
    """## 条件风险价值损失（CVaRLoss）
    - 适用场景：极端风险控制（如黑天鹅防护）、高杠杆策略
    - 核心逻辑：惩罚 “极端亏损条件下的平均亏损”，比波动率更关注尾部风险。
    Args:
        pred_rets: 预测的收益率序列，形状为 (batch_size, time_steps)
    Returns:
        损失 = -平均收益 + lambda_cvar * CVaR（最小化损失即控制极端风险）
    ### 使用示例
    >>> loss_fn = CVaRLoss(alpha=0.95, lambda_cvar=2.0)
    >>> pred_rets = th.tensor([[0.02, -0.15, 0.01, -0.2, 0.03]])  # 包含极端亏损
    >>> loss = loss_fn(pred_rets)  # 对极端亏损的平均水平施加惩罚
        """

    def __init__(self, alpha=0.95, lambda_cvar=1.0):
        super().__init__()
        self.alpha = alpha  # 置信水平（如0.95表示关注5%的极端亏损）
        self.lambda_cvar = lambda_cvar  # CVaR惩罚系数

    def forward(self, pred_rets: th.Tensor) -> th.Tensor:

        batch_size, time_steps = pred_rets.shape
        # 排序收益率（按从小到大排序，取尾部极端值）
        sorted_rets, _ = th.sort(pred_rets, dim=1)  # (batch_size, time_steps)
        # 计算极端亏损的样本数量（alpha分位数对应的尾部数量）
        n_quantile = int(time_steps * (1 - self.alpha)
                         )  # 如time_steps=1000，取50个极端值
        if n_quantile == 0:
            n_quantile = 1  # 确保至少取1个极端值
        # 取每个样本的尾部极端亏损（前n_quantile个最小值）
        tail_rets = sorted_rets[:, :n_quantile]  # (batch_size, n_quantile)
        # CVaR = 尾部极端亏损的平均值（即极端条件下的平均亏损）
        cvar = th.mean(tail_rets, dim=1)  # (batch_size,)
        # 平均收益
        mean_ret = th.mean(pred_rets, dim=1)  # (batch_size,)
        # 损失 = 负收益 + CVaR惩罚（注意CVaR为负时，惩罚更重）
        loss = -mean_ret + self.lambda_cvar * cvar
        return th.mean(loss)


class NetReturnWithCostLoss(nn.Module):
    """## 带交易成本的净收益损失（NetReturnWithCostLoss）
    - 适用场景：高频交易、换手率高的策略（需考虑手续费 / 滑点）
    - 核心逻辑：将交易成本（手续费、滑点）纳入收益计算，确保模型优化 “实际净收益” 而非 “名义收益”。
    Args:
        pred_rets: 预测的资产收益率序列，形状为 (batch_size, time_steps)
        weights: 预测的仓位权重序列（如持仓比例），形状为 (batch_size, time_steps)
                    （假设weights[:, t]为t时刻的仓位，仓位调整=weights[:, t] - weights[:, t-1]）
    Returns:
        损失 = -净收益均值（净收益=名义收益 - 交易成本）
    ### 使用示例
    >>> loss_fn = NetReturnWithCostLoss(cost_rate=0.0005)  # 0.05%手续费
    >>> pred_rets = th.tensor([[0.02, 0.01, 0.03]])  # 收益率
    >>> weights = th.tensor([[0.5, 0.7, 0.4]])  # 仓位权重（调整：0.2→-0.3）
    >>> loss = loss_fn(pred_rets, weights)  # 高换手率会被惩罚
        """

    def __init__(self, cost_rate=0.001, lambda_cost=1.0):
        super().__init__()
        self.cost_rate = cost_rate  # 单位交易成本（如0.001=0.1%手续费）
        self.lambda_cost = lambda_cost  # 成本惩罚系数

    def forward(self, pred_rets: th.Tensor, weights: th.Tensor) -> th.Tensor:

        batch_size, time_steps = pred_rets.shape
        # 计算名义收益：收益率 * 仓位权重
        nominal_rets = pred_rets * weights  # (batch_size, time_steps)
        # 计算仓位调整幅度（换手率）：|当前权重 - 上一时刻权重|
        # (batch_size, time_steps-1)
        weight_changes = th.abs(weights[:, 1:] - weights[:, :-1])
        # 交易成本：成本率 * 调整幅度（首时刻无调整成本）
        transaction_cost = self.cost_rate * \
            weight_changes  # (batch_size, time_steps-1)
        # 补齐首时刻的成本（0），与收益序列对齐
        transaction_cost = th.cat([
            th.zeros(batch_size, 1, device=transaction_cost.device),
            transaction_cost
        ], dim=1)  # (batch_size, time_steps)
        # 净收益 = 名义收益 - 交易成本
        net_rets = nominal_rets - self.lambda_cost * transaction_cost
        # 损失 = 负的净收益均值（最小化损失即最大化净收益）
        return -th.mean(net_rets)


class MarketNeutralLoss(nn.Module):
    """## 市场中性惩罚损失（MarketNeutralLoss）
    - 适用场景：多空对冲策略（如股票多空、行业中性）
    - 核心逻辑：惩罚组合与基准指数的相关性（贝塔值），实现 “剥离系统性风险” 的中性目标。
    Args:
        pred_rets: 预测的组合收益率序列，形状为 (batch_size, time_steps)
        bench_rets: 基准指数收益率序列（如沪深300），形状为 (time_steps,) 或 (batch_size, time_steps)
    Returns:
        损失 = -组合平均收益 + lambda_beta * |贝塔|（贝塔≈0表示市场中性）
    ### 使用示例
    >>> loss_fn = MarketNeutralLoss(lambda_beta=1.5)
    >>> pred_rets = th.tensor([[0.02, -0.01, 0.03]])  # 组合收益率
    >>> bench_rets = th.tensor([[0.01, -0.02, 0.02]])  # 基准指数收益率
    >>> loss = loss_fn(pred_rets, bench_rets)  # 组合与基准相关性高则惩罚重
    """

    def __init__(self, lambda_beta=1.0, eps=1e-6):
        super().__init__()
        self.lambda_beta = lambda_beta  # 贝塔惩罚系数
        self.eps = eps  # 避免基准方差为0时除零

    def forward(self, pred_rets: th.Tensor, bench_rets: th.Tensor) -> th.Tensor:

        # 确保基准收益率与组合收益率形状匹配（广播）
        if bench_rets.ndim == 1:
            # (1, time_steps) → 广播到(batch_size, time_steps)
            bench_rets = bench_rets.unsqueeze(0)
        # 计算组合与基准的协方差（按时间维度）
        cov = th.mean((pred_rets - pred_rets.mean(dim=1, keepdim=True)) *
                      (bench_rets - bench_rets.mean(dim=1, keepdim=True)), dim=1)  # (batch_size,)
        # 计算基准收益率的方差
        bench_var = th.var(bench_rets, dim=1) + self.eps  # (batch_size,)
        # 贝塔 = 协方差 / 基准方差（衡量组合对基准的敏感度）
        beta = cov / bench_var  # (batch_size,)
        # 组合平均收益
        mean_ret = th.mean(pred_rets, dim=1)  # (batch_size,)
        # 损失 = 负收益 + 贝塔惩罚（贝塔绝对值越小，惩罚越小）
        loss = -mean_ret + self.lambda_beta * th.abs(beta)
        return th.mean(loss)
