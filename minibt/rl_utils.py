from torch.nn import Module
from torch import tanh
from torch.nn.modules.loss import *
from torch.optim import *
from torch.nn.modules.activation import *
from typing import Literal,  Callable
import torch
import torch.nn as nn
import torch.nn.functional as F
from .muon import MomMuon, OGSignMuon, AdaMuon
from .other import base
try:
    from torchcontrib.optim import SWA
except:
    SWA = None

__all__ = ["Loss", "Optim", "LrScheduler",
           "SWALR", "Activation", "Norm"]


class Loss(base):
    """
    ## 损失函数
    - 详细参数和示例可参考 PyTorch 官方文档。https://pytorch.org/docs/stable/nn.html#loss-functions
    ## 一、分类任务
    ### CrossEntropyLoss
        用途:多分类任务(结合 LogSoftmax 和 NLLLoss)。
        输入:原始 logits(无需手动 Softmax),标签为类别索引。
        参数:weight(类别权重),ignore_index(忽略特定类别)。
    ### BCELoss(Binary Cross Entropy)
        用途:二分类任务。
        输入:经过 Sigmoid 的概率值(范围 [0,1]),标签为 0/1。
        变种:BCEWithLogitsLoss(直接输入 logits,内置 Sigmoid,数值稳定)。
    ### NLLLoss(Negative Log Likelihood Loss)
        用途:多分类任务,需手动对输入应用 LogSoftmax。
        输入:对数概率值,标签为类别索引。
    ### KLDivLoss(Kullback-Leibler Divergence)
        用途:衡量两个概率分布的差异(如模型输出与目标分布)。
        输入:对数概率(log_prob)和目标概率(需归一化)。
    ## 二、回归任务
    ### MSELoss(Mean Squared Error)
        用途:回归任务,计算预测值与目标的均方误差。
    ### L1Loss(Mean Absolute Error)
        用途:回归任务,对异常值更鲁棒。
    ### HuberLoss
        特点:结合 MSE 和 MAE,通过参数 delta 控制鲁棒性。
    ### SmoothL1Loss
        特点:类似 Huber Loss,在接近目标时平滑(常用于目标检测如 Faster R-CNN)。
    ## 三、序列任务
    ### CTCLoss(Connectionist Temporal Classification)
        用途:时序数据对齐任务(如语音识别、OCR)。
        输入:需指定输入长度和目标长度。
    ### PoissonNLLLoss
        用途:目标服从泊松分布的回归任务(如计数预测)。
    ## 四、生成对抗网络(GAN)
    ### BCELoss:用于判别器的二分类输出。
    ### HingeEmbeddingLoss:常用于生成对抗网络的损失函数变种。
    ## 五、相似性学习
    ### CosineEmbeddingLoss
        用途:衡量两个向量的余弦相似度,用于嵌入学习。
        参数:margin 控制相似/不相似样本的阈值。
    ### TripletMarginLoss
        用途:三元组损失(Anchor、Positive、Negative),用于度量学习。
        参数:margin 控制正负样本间距。
    ## 六、分布匹配
    ### WassersteinLoss(需自定义)
        用途:WGAN 中衡量分布距离(需配合梯度裁剪使用)。
    ## 七、其他
    ### MarginRankingLoss:排序任务(如推荐系统)。
    ### MultiLabelMarginLoss:多标签分类任务。
    ### MultiLabelSoftMarginLoss:多标签二分类,输入为 logits。
    ### SoftMarginLoss:二分类任务的另一种损失函数。
    ## 八、选择建议
        分类任务:优先用 CrossEntropyLoss(多分类)或 BCEWithLogitsLoss(二分类)。
        回归任务:默认 MSELoss,异常值多用 HuberLoss。
        序列任务:对齐问题用 CTCLoss。
        相似性学习:尝试 TripletMarginLoss 或 CosineEmbeddingLoss。"""
    L1Loss = L1Loss
    NLLLoss = NLLLoss
    PoissonNLLLoss = PoissonNLLLoss
    GaussianNLLLoss = GaussianNLLLoss
    KLDivLoss = KLDivLoss
    MSELoss = MSELoss
    BCELoss = BCELoss
    BCEWithLogitsLoss = BCEWithLogitsLoss
    HingeEmbeddingLoss = HingeEmbeddingLoss
    MultiLabelMarginLoss = MultiLabelMarginLoss
    SmoothL1Loss = SmoothL1Loss
    HuberLoss = HuberLoss
    SoftMarginLoss = SoftMarginLoss
    CrossEntropyLoss = CrossEntropyLoss
    MultiLabelSoftMarginLoss = MultiLabelSoftMarginLoss
    CosineEmbeddingLoss = CosineEmbeddingLoss
    MarginRankingLoss = MarginRankingLoss
    MultiMarginLoss = MultiMarginLoss
    TripletMarginLoss = TripletMarginLoss
    TripletMarginWithDistanceLoss = TripletMarginWithDistanceLoss
    CTCLoss = CTCLoss


Loss = Loss()


class Optim(base):
    """## 优化器
    - 更多细节参考 PyTorch 优化器文档。https://pytorch.org/docs/stable/optim.html
    ## 一、基础优化器
    ### SGD(随机梯度下降)
        参数:
        lr:学习率(必需)。
        momentum:动量(加速收敛,缓解震荡)。
        dampening:动量阻尼(默认 0)。
        weight_decay:权重衰减(L2 正则化)。
        nesterov:是否使用 Nesterov 动量(需 momentum > 0)。
        示例:
        >>> optimizer = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
    ### Adam(自适应矩估计)
        特点:结合动量(一阶矩)和自适应学习率(二阶矩),适合大多数任务。
        参数:
        lr:学习率(默认 1e-3)。
        betas:动量衰减系数(默认 (0.9, 0.999))。
        eps:数值稳定性常数(默认 1e-8)。
        weight_decay:权重衰减(L2 正则化)。
        示例:
        >>> optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    ## 二、自适应学习率优化器
    ### Adagrad
        特点:为每个参数分配独立的学习率,适合稀疏数据(如 NLP)。
        参数:
        lr(默认 1e-2),weight_decay,initial_accumulator_value。
        公式:累积梯度平方调整学习率。
        缺点:学习率可能过早衰减至零。
        示例:
        >>> optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    ### RMSprop
        特点:改进 Adagrad,引入衰减因子避免学习率过快下降(适合非平稳目标)。
        参数:
        lr(默认 1e-2),alpha(衰减率,默认 0.99),momentum(可选)。
    ### Adadelta
        特点:无需手动设置初始学习率,改进 RMSprop 的学习率自适应机制。
        参数:
        rho(梯度平方移动平均的衰减率,默认 0.9),eps,weight_decay。
    ## 三、进阶优化器
    ### AdamW
        改进:解耦权重衰减(与 Adam+L2 正则化不同),更稳定(适合 Transformer 等模型)。
        参数:同 Adam,但 weight_decay 应用方式不同。
        示例:
        >>> optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=0.01)
    ### NAdam
        特点:结合 Nesterov 动量的 Adam 变体,加速收敛。
    ### RAdam(Rectified Adam)
        特点:自适应学习率方差修正,避免训练初期不稳定。
    ### LBFGS
        用途:拟牛顿法,适合小批量数据和低维参数优化(但内存消耗大)。
        示例:
        >>> optimizer = torch.optim.LBFGS(model.parameters(), lr=0.1)
    ## 四、其他优化器
    ### SparseAdam
        特点:针对稀疏梯度优化(如嵌入层),内存高效。
    ### Rprop
        特点:弹性反向传播,仅适用于全批量数据(不推荐用于小批量)。
    ### ASGD(平均随机梯度下降)
        特点:对参数取平均,提升鲁棒性(适合长时间训练)。
    ## 五、选择建议
        通用场景:优先使用 Adam 或 AdamW(尤其需要权重衰减时)。
        简单任务/小数据:尝试 SGD(配合动量)。
        稀疏数据(如 NLP):考虑 Adagrad 或 RMSprop。
        训练不稳定:尝试 RAdam 或 NAdam。
        Transformer 模型:推荐 AdamW(如 BERT、GPT)。
    ## 六、参数调优技巧
        学习率:
        Adam 类优化器通常用较小的初始值(如 1e-3 或 1e-4)。
        SGD 需要更大学习率(如 0.1 或 0.01)。
        权重衰减:
        防止过拟合,常用 1e-4 到 1e-2。
        动量(SGD/RMSprop):
        通常设为 0.9 或 0.99。
    ### 示例1
    https://blog.csdn.net/u011984148/article/details/99440172
    >>> act_optimizer = th.optim.SGD(self.act.parameters(
        ), self.learning_rate, momentum=self.momentum, weight_decay=self.weight_decay)
        self.act_optimizer = SWA(
            act_optimizer, swa_start=10, swa_freq=5, swa_lr=0.05)
        cri_optimizer = th.optim.SGD(self.act.parameters(
        ), self.learning_rate, momentum=self.momentum, weight_decay=self.weight_decay)
        self.cri_optimizer = SWA(
            cri_optimizer, swa_start=10, swa_freq=5, swa_lr=0.05)
    """
    Adadelta = Adadelta
    Adagrad = Adagrad
    Adam = Adam
    AdamW = AdamW
    SparseAdam = SparseAdam
    Adamax = Adamax
    ASGD = ASGD
    SGD = SGD
    RAdam = RAdam
    Rprop = Rprop
    RMSprop = RMSprop
    Optimizer = Optimizer
    NAdam = NAdam
    LBFGS = LBFGS
    SWA = SWA
    AdaMuon = AdaMuon
    MomMuon = MomMuon
    OGSignMuon = OGSignMuon


Optim = Optim()


class LrScheduler(base):
    """## PyTorch 的lr_scheduler模块提供了多种学习率调度器，用于在训练中动态调整学习率，以优化模型收敛速度和效果。以下是对列出的 14 种调度器的详细使用说明（含核心功能、关键参数、使用示例和适用场景）：

    ## 1. LambdaLR
    >>> 核心功能：通过自定义函数（lambda）动态调整学习率，支持为不同参数组设置不同的调整策略。
        关键参数：
        lr_lambda：函数或函数列表。若为函数，输入为当前 epoch / 迭代次数，返回学习率的缩放因子；若为列表，需与参数组数量一致（每组单独调整）。
        示例：
        optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
        # 自定义策略：前10个epoch保持0.1，之后线性衰减至0
        lambda_func = lambda epoch: 1.0 if epoch < 10 else 0.1 * (20 - epoch) / 10
        scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=lambda_func)
        # 训练循环中调用
        for epoch in range(20):
            train(...)
            optimizer.step()
            scheduler.step()  # 每个epoch后更新学习率
        适用场景：需要高度定制化学习率变化（如不同层参数用不同调整策略）。

    ## 2. MultiplicativeLR
    >>> 核心功能：与LambdaLR类似，但 lambda 函数返回的是 “乘法因子”（直接乘以当前学习率），而非相对于初始 lr 的缩放因子。
        关键参数：
        lr_lambda：函数或列表，返回当前学习率的乘法因子（如 0.9 表示当前 lr 乘以 0.9）。
        示例：
        # 每次迭代学习率乘以0.99
        scheduler = torch.optim.lr_scheduler.MultiplicativeLR(
            optimizer, lr_lambda=lambda iter: 0.99
        )
        适用场景：需要基于当前学习率动态调整（而非初始 lr），例如持续衰减但衰减幅度依赖当前 lr。

    ## 3. StepLR
    >>> 核心功能：按固定 “步长” 周期性衰减学习率（每过step_size个 epoch，学习率乘以gamma）。
        关键参数：
        step_size：衰减周期（每多少个 epoch 调整一次）。
        gamma：衰减因子（如 0.1 表示每次调整为原来的 1/10）。
        示例：
        # 每5个epoch，学习率乘以0.5（即变为原来的1/2）
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=5, gamma=0.5)
        适用场景：已知训练过程中需要周期性衰减（如简单任务的固定节奏调整）。

    ## 4. MultiStepLR
    >>> 核心功能：在指定的milestones（epoch 列表）处衰减学习率（每次到达里程碑，学习率乘以gamma）。
        关键参数：
        milestones：需要衰减的 epoch 列表（如[30, 80]表示在 30、80 epoch 调整）。
        gamma：衰减因子。
        运行
        # 在第30、60、90 epoch，学习率乘以0.1
        scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer, milestones=[30, 60, 90], gamma=0.1
        )
        适用场景：已知模型在特定 epoch 后需要衰减（如根据经验或预训练规律设置关键节点）。

    ## 5. ConstantLR
    >>> 核心功能：保持学习率不变（或初始阶段按固定因子缩放，之后保持不变）。
        关键参数：
        factor：初始缩放因子（学习率 = 初始 lr × factor）。
        total_iters：维持缩放后的 lr 的迭代次数（之后恢复为初始 lr）。
        示例：
        # 前5个epoch用初始lr的0.5，之后恢复为初始lr
        scheduler = torch.optim.lr_scheduler.ConstantLR(optimizer, factor=0.5, total_iters=5)
        适用场景：调试（固定 lr 对比实验）或需要 “预热”（初始用小 lr，避免震荡）。

    ## 6. LinearLR
    >>> 核心功能：学习率在total_iters内从start_factor×初始lr线性过渡到end_factor×初始lr。
        关键参数：
        start_factor：起始缩放因子（如 0.1 表示初始 lr×0.1）。
        end_factor：结束缩放因子（如 1.0 表示最终达到初始 lr）。
        total_iters：过渡的总迭代次数。
        示例：
        # 10个epoch内，从初始lr的0.1线性增长到初始lr的1.0
        scheduler = torch.optim.lr_scheduler.LinearLR(
            optimizer, start_factor=0.1, end_factor=1.0, total_iters=10
        )
        适用场景：学习率 “预热”（如训练初期缓慢提升 lr，避免模型不稳定）。

    ## 7. ExponentialLR
    >>> 核心功能：学习率按指数规律衰减（每个 epoch 后，lr = lr × gamma）。
        关键参数：
        gamma：指数衰减因子（需小于 1，如 0.95 表示每个 epoch 衰减 5%）。
        示例：
        # 每个epoch学习率乘以0.95（持续指数衰减）
        scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.95)
        适用场景：需要持续平滑衰减，但需注意gamma不能太小（否则 lr 会快速趋近于 0）。

    ## 8. SequentialLR
    >>> 核心功能：按顺序应用多个调度器（前一个调度器结束后，自动切换到下一个）。
        关键参数：
        schedulers：调度器列表（如[scheduler1, scheduler2]）。
        milestones：切换调度器的迭代次数 /epoch 列表（长度为schedulers数量 - 1）。
        示例：
        # 先LinearLR预热10个epoch，再StepLR每5个epoch衰减
        scheduler1 = torch.optim.lr_scheduler.LinearLR(optimizer, total_iters=10)
        scheduler2 = torch.optim.lr_scheduler.StepLR(optimizer, step_size=5, gamma=0.5)
        scheduler = torch.optim.lr_scheduler.SequentialLR(
            optimizer, schedulers=[scheduler1, scheduler2], milestones=[10]
        )
        适用场景：组合多种策略（如预热 + 衰减），分阶段调整。

    ## 9. CosineAnnealingLR
    >>> 核心功能：学习率按余弦函数规律衰减（从初始 lr 逐渐降至eta_min，周期为T_max）。
        关键参数：
        T_max：余弦周期（多少个 epoch 完成一次从 η_max 到 η_min 的衰减）。
        eta_min：最小学习率（衰减的下限，默认 0）。
        示例：
        # 50个epoch为一个周期，学习率从初始值余弦衰减至0
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=50, eta_min=0)
        适用场景：需要平滑衰减以跳出局部最优（如深度学习中的图像分类任务）。

    ## 10. ChainedScheduler
    >>> 核心功能：链式应用多个调度器（所有调度器同时作用于当前学习率，按顺序依次更新）。
        关键参数：
        schedulers：调度器列表（如[s1, s2]表示先应用 s1，再用 s2 调整 s1 的输出）。
        示例：
        # 同时应用StepLR（每5 epoch×0.5）和ExponentialLR（每个epoch×0.99）
        s1 = torch.optim.lr_scheduler.StepLR(optimizer, step_size=5, gamma=0.5)
        s2 = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.99)
        scheduler = torch.optim.lr_scheduler.ChainedScheduler([s1, s2])
        适用场景：需要叠加多种调整策略（如固定步长衰减 + 指数衰减）。

    ## 11. ReduceLROnPlateau
    >>> 核心功能：根据 “监控指标”（如验证损失）自动衰减学习率（当指标不再改善时触发）。
        关键参数：
        mode：监控模式（min表示指标越小越好，如损失；max表示越大越好，如准确率）。
        factor：衰减因子（如 0.1 表示衰减为原来的 1/10）。
        patience：指标连续多少个 epoch 无改善后触发衰减。
        示例：
        # 监控验证损失，连续3个epoch不下降则衰减lr×0.5
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, mode='min', factor=0.5, patience=3
        )
        # 训练中需传入验证指标（与其他调度器不同！）
        for epoch in range(100):
            train(...)
            val_loss = validate(...)
            scheduler.step(val_loss)  # 用验证损失触发调整
        适用场景：自适应调整（无需手动设置衰减节点），适合未知最佳衰减时机的任务。

    ## 12. CyclicLR
    >>> 核心功能：学习率在 “基础 lr” 和 “最大 lr” 之间周期性循环（先升后降，循环往复）。
        关键参数：
        base_lr：基础学习率（循环的下限）。
        max_lr：最大学习率（循环的上限）。
        step_size_up：从 base_lr 升到 max_lr 的迭代次数。
        step_size_down：从 max_lr 降到 base_lr 的迭代次数（默认与 step_size_up 相同）。
        示例：
        # 400次迭代从0.001升到0.1，再400次迭代降回0.001，循环往复
        scheduler = torch.optim.lr_scheduler.CyclicLR(
            optimizer, base_lr=0.001, max_lr=0.1, step_size_up=400, step_size_down=400
        )
        适用场景：通过周期性调整探索参数空间，可能帮助模型跳出局部最优（如难训练的深层网络）。

    ## 13. CosineAnnealingWarmRestarts
    >>> 核心功能：带 “热重启” 的余弦退火（每次重启后，学习率回到初始值，周期逐渐延长）。
        关键参数：
        T_0：初始周期（第一次从 lr_max 到 lr_min 的迭代次数）。
        T_mult：周期倍增因子（每次重启后，周期 = 上一周期 × T_mult，如 2 表示周期翻倍）。
        eta_min：最小学习率（下限）。
        示例：
        # 初始周期10，每次重启周期翻倍，学习率从初始值余弦衰减至0
        scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
            optimizer, T_0=10, T_mult=2, eta_min=0
        )
        适用场景：需要多次重启以精细调整（如 Transformer 等大型模型训练，通过重启避免过拟合）。

    ## 14. OneCycleLR
    >>> 核心功能：在 “总迭代次数” 内，学习率先线性升至max_lr，再余弦衰减至初始 lr 的 1/10；同时动态调整动量（与 lr 反向变化）。
        关键参数：
        max_lr：学习率的最大值。
        total_steps：总迭代次数（整个训练的 step 数）。
        pct_start：学习率上升阶段占总步数的比例（如 0.3 表示 30% 步数用于升温）。
        示例：
        # 总步数1000，前300步从初始lr升到0.1，后700步余弦衰减至0.01
        scheduler = torch.optim.lr_scheduler.OneCycleLR(
            optimizer, max_lr=0.1, total_steps=1000, pct_start=0.3
        )
        适用场景：快速收敛（如小数据集或需要高效训练的场景），PyTorch 官方推荐用于快速调优。

    ### 总结：调度器选择建议
    >>> 简单固定衰减：StepLR（周期固定）或MultiStepLR（指定节点）。
        自适应调整：ReduceLROnPlateau（基于验证指标）。
        平滑探索：CosineAnnealingLR（单周期）或CosineAnnealingWarmRestarts（多周期重启）。
        快速收敛：OneCycleLR（适合短训练周期）。
        组合策略：SequentialLR（分阶段）或ChainedScheduler（叠加）。
        使用时需注意：多数调度器在optimizer.step()后调用 scheduler.step()；ReduceLROnPlateau需传入验证指标；部分调度器（如OneCycleLR）需按迭代次数而非 epoch 调用。"""
    LambdaLR = lr_scheduler.LambdaLR
    MultiplicativeLR = lr_scheduler.MultiplicativeLR
    StepLR = lr_scheduler.StepLR
    MultiStepLR = lr_scheduler.MultiStepLR
    ConstantLR = lr_scheduler.ConstantLR
    LinearLR = lr_scheduler.LinearLR
    ExponentialLR = lr_scheduler.ExponentialLR
    SequentialLR = lr_scheduler.SequentialLR
    CosineAnnealingLR = lr_scheduler.CosineAnnealingLR
    ChainedScheduler = lr_scheduler.ChainedScheduler
    # ReduceLROnPlateau = lr_scheduler.ReduceLROnPlateau
    CyclicLR = lr_scheduler.CyclicLR
    CosineAnnealingWarmRestarts = lr_scheduler.CosineAnnealingWarmRestarts
    OneCycleLR = lr_scheduler.OneCycleLR


LrScheduler = LrScheduler()


class SWALR_(swa_utils.SWALR):
    r"""Anneals the learning rate in each parameter group to a fixed value.

    This learning rate scheduler is meant to be used with Stochastic Weight
    Averaging (SWA) method (see `torch.optim.swa_utils.AveragedModel`).

    Args:
        optimizer (torch.optim.Optimizer): wrapped optimizer
        swa_lrs (float or list): the learning rate value for all param groups
            together or separately for each group.
        annealing_epochs (int): number of epochs in the annealing phase
            (default: 10)
        annealing_strategy (str): "cos" or "linear"; specifies the annealing
            strategy: "cos" for cosine annealing, "linear" for linear annealing
            (default: "cos")
        last_epoch (int): the index of the last epoch (default: -1)

    The :class:`SWALR` scheduler can be used together with other
    schedulers to switch to a constant learning rate late in the training
    as in the example below.

    Example:
        >>> # xdoctest: +SKIP("Undefined variables")
        >>> loader, optimizer, model = ...
        >>> lr_lambda = lambda epoch: 0.9
        >>> scheduler = torch.optim.lr_scheduler.MultiplicativeLR(optimizer,
        >>>        lr_lambda=lr_lambda)
        >>> swa_scheduler = torch.optim.swa_utils.SWALR(optimizer,
        >>>        anneal_strategy="linear", anneal_epochs=20, swa_lr=0.05)
        >>> swa_start = 160
        >>> for i in range(300):
        >>>      for input, target in loader:
        >>>          optimizer.zero_grad()
        >>>          loss_fn(model(input), target).backward()
        >>>          optimizer.step()
        >>>      if i > swa_start:
        >>>          swa_scheduler.step()
        >>>      else:
        >>>          scheduler.step()

    .. _Averaging Weights Leads to Wider Optima and Better Generalization:
        https://arxiv.org/abs/1803.05407
    """

    def __init__(
        self,
        optimizer: Optimizer,
        swa_lr: float = 0.05,
        anneal_epochs=10,
        anneal_strategy: Literal["cos", "linear"] = "cos",
        last_epoch=-1,
    ):
        super().__init__(optimizer, swa_lr, anneal_epochs, anneal_strategy, last_epoch)


class SWALR(base):
    """
    ## 随机权重平均 swa_utils
    - swa_utils 用于实现 随机权重平均(Stochastic Weight Averaging, SWA),通过平均多个时间点的模型权重提升泛化性。
    ## 1. 基本步骤
        >>> from torch.optim.swa_utils import AveragedModel, SWALR
            #定义原始模型和优化器
            model = torch.nn.Linear(10, 2)
            optimizer = optim.SGD(model.parameters(), lr=0.1)
            #初始化 SWA 模型和调度器
            swa_model = AveragedModel(model)  # 基于原始模型的权重平均容器
            swa_scheduler = SWALR(
                optimizer, 
                swa_lr=0.05)  #SWA 阶段的学习率
            #训练循环(分两阶段)
            for epoch in range(100):
                #常规训练阶段(前 90 epoch)
                if epoch < 90:
                    train(...)
                    optimizer.step()
                #SWA 阶段(后 10 epoch)
                else:
                    swa_model.update_parameters(model)  #更新 SWA 模型的权重
                    swa_scheduler.step()               #调整学习率
                    optimizer.zero_grad()
            #训练结束后更新 BatchNorm 统计量
            torch.optim.swa_utils.update_bn(
                dataloader,  #训练或验证的 DataLoader
                swa_model)    #SWA 模型
            #保存最终模型
            torch.save(swa_model.state_dict(), "swa_model.pth")

    ## 2. 关键参数
        AveragedModel:
        avg_fn:自定义权重平均函数(默认是简单平均)。
        SWALR:
        swa_lr:SWA 阶段的学习率,通常比初始学习率小。
        anneal_epochs:学习率调整的过渡 epoch 数(默认 10)。

    ## 联合使用 lr_scheduler 和 swa_utils
    >>> #定义调度器和 SWA 工具
        scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=100)
        swa_model = AveragedModel(model)
        swa_start = 80  #从 epoch 80 开始 SWA
        for epoch in range(100):
            train(...)
            optimizer.step()
            #常规学习率调度
            if epoch < swa_start:
                scheduler.step()
            #SWA 阶段
            else:
                swa_model.update_parameters(model)
                swa_scheduler.step()
        #更新 BatchNorm 并保存模型
        update_bn(dataloader, swa_model)
        torch.save(swa_model.state_dict(), "swa_model.pth")

    ## 核心注意事项
        学习率调度器调用时机:
        多数调度器在 optimizer.step() 后调用 scheduler.step()。
        ReduceLROnPlateau 需要传入监控指标(如验证损失)。
        SWA 使用场景:
        在训练后期(如最后 20% epoch)启用 SWA。
        SWA 模型需要额外的 update_bn 步骤。
        模型保存:
        保存 SWA 模型时,使用 swa_model.state_dict()。

    ## 效果与适用任务
        lr_scheduler:适用于所有需要动态调整学习率的任务(如分类、检测)。
        swa_utils:尤其适合提升模型鲁棒性(如对抗训练、小数据集)。"""
    SWALR = SWALR_


SWALR = SWALR()


class Tanh10(Module):
    r"""Applies the Hyperbolic Tangent (Tanh10) function element-wise.

    Tanh10 is defined as:

    .. math::
        \text{Tanh10}(10*x) = \tanh(10*x) = \frac{\exp(10*x) - \exp(-10*x)} {\exp(10*x) + \exp(-10*x)}

    Shape:
        - Input: :math:`(*)`, where :math:`*` means any number of dimensions.
        - Output: :math:`(*)`, same shape as the input.

    .. image:: ../scripts/activation_images/Tanh.png

    Examples::

        >>> m = nn.Tanh10()
        >>> input = torch.randn(2)
        >>> output = m(input)
    """

    def forward(self, input):
        return tanh(10*input)


class Activation(base):
    """
    ## 激活函数
    ## 一、基本激活函数
    ### ReLU(Rectified Linear Unit)
    特点:计算高效,缓解梯度消失问题,但可能导致“神经元死亡”(输出恒为0)。
    >>> import torch.nn as nn
        activation = nn.ReLU()  # 实例化
        output = activation(input_tensor)

    ### Sigmoid
    特点:将输出压缩到 [0, 1],适合二分类输出层。但存在梯度消失问题。
    >>> activation = nn.Sigmoid()
        output = activation(input_tensor)  # 适用于概率输出

    ### Tanh(Hyperbolic Tangent)
    特点:输出范围 [-1, 1],比 Sigmoid 更中心化,适合隐藏层。
    >>> activation = nn.Tanh()
        output = activation(input_tensor)

    ## 二、高级激活函数
    ### LeakyReLU
    特点:解决 ReLU 的“死亡神经元”问题,允许负值输入有微小梯度(默认 
    >>> activation = nn.LeakyReLU(negative_slope=0.1)  # 自定义负斜率
        output = activation(input_tensor)

    ### GELU(Gaussian Error Linear Unit)
    特点:平滑近似 ReLU,被 BERT、GPT 等 Transformer 模型广泛使用。
    >>> activation = nn.GELU()
        output = activation(input_tensor)

    ## ELU(Exponential Linear Unit)
    特点:负值区有非零输出,缓解梯度消失问题。
    >>> activation = nn.ELU(alpha=0.5)
        output = activation(input_tensor)

    ### SELU(Scaled ELU)
    特点:自归一化网络(Self-Normalizing Networks)专用激活函数,需配合特定初始化(如 nn.init.kaiming_normal_)。
    >>> activation = nn.SELU()
        output = activation(input_tensor)

    ## 三、其他激活函数
    ### Softmax
    特点:将输出转换为概率分布,适合多分类输出层。
    >>> activation = nn.Softmax(dim=1)  # 指定计算维度(如分类维度)
        output = activation(input_tensor)

    ### Swish
    特点:Google 提出,平滑非单调激活函数,性能优于 ReLU。
    PyTorch 实现:需手动定义或使用 torch.nn.SiLU(PyTorch 1.7+)。
    >>> activation = nn.SiLU()  # Swish 的 PyTorch 官方实现
        output = activation(input_tensor)

    ### Mish
    特点:平滑非单调,在目标检测等任务中表现优异。
    PyTorch 实现:需自定义或使用第三方库。
    >>> class Mish(nn.Module):
            def forward(self, x):
                return x * torch.tanh(torch.nn.functional.softplus(x))
        activation = Mish()

    ## 四、激活函数选择建议
        隐藏层:优先使用 ReLU 或 GELU(尤其 Transformer 模型)。
        输出层:
        二分类:Sigmoid
        多分类:Softmax
        回归任务:无激活函数(直接输出)或 Tanh(限制输出范围)。
        稀疏梯度问题:尝试 LeakyReLU 或 ELU。
        需要自归一化:使用 SELU(配合初始化)。

    ## 五、示例代码
    >>> import torch
        import torch.nn as nn
        class MyModel(nn.Module):
            def __init__(self):
                super().__init__()
                self.fc1 = nn.Linear(784, 256)
                self.act1 = nn.ReLU()
                self.fc2 = nn.Linear(256, 10)
                self.act2 = nn.Softmax(dim=1)
            def forward(self, x):
                x = self.fc1(x)
                x = self.act1(x)
                x = self.fc2(x)
                x = self.act2(x)
                return x
        #使用示例
        model = MyModel()
        input_tensor = torch.randn(32, 784)  # 假设输入为 32 个样本
        output = model(input_tensor)
        print(output.shape)  # 输出:torch.Size([32, 10])"""
    Threshold = Threshold
    ReLU = ReLU
    RReLU = RReLU
    Hardtanh = Hardtanh
    ReLU6 = ReLU6
    Sigmoid = Sigmoid
    Hardsigmoid = Hardsigmoid
    Tanh = Tanh
    SiLU = SiLU
    Mish = Mish
    Hardswish = Hardswish
    ELU = ELU
    CELU = CELU
    SELU = SELU
    GLU = GLU
    GELU = GELU
    Hardshrink = Hardshrink
    LeakyReLU = LeakyReLU
    LogSigmoid = LogSigmoid
    Softplus = Softplus
    Softshrink = Softshrink
    MultiheadAttention = MultiheadAttention
    PReLU = PReLU
    Softsign = Softsign
    Tanhshrink = Tanhshrink
    Softmin = Softmin
    Softmax = Softmax
    Softmax2d = Softmax2d
    LogSoftmax = LogSoftmax
    Tanh10 = Tanh10


Activation = Activation()


class SwitchNorm(nn.Module):
    """
        可切换归一化（Switchable Normalization）

        参数:
            num_features: 输入特征的通道数（C）
            eps: 数值稳定性参数，避免除零
            momentum: BatchNorm移动平均的动量（用于推理阶段）
            affine: 是否使用可学习的缩放（scale）和平移（shift）参数
            num_groups: GroupNorm的分组数（原论文中用于辅助计算，非必须）
        """

    def __init__(self, num_features, eps=1e-5, momentum=0.9, affine=True,
                 num_groups=32):

        super(SwitchNorm, self).__init__()
        self.num_features = num_features
        self.eps = eps
        self.momentum = momentum
        self.affine = affine
        self.num_groups = num_groups

        # 可学习的融合权重（用于BatchNorm/LayerNorm/InstanceNorm的加权融合）
        self.weight = nn.Parameter(torch.ones(3))  # [w_bn, w_ln, w_in]
        self.bias = nn.Parameter(torch.zeros(3))   # 偏置（可选，原论文未使用，这里保留）

        # 若启用affine，定义全局的缩放和平移参数
        if self.affine:
            self.scale = nn.Parameter(torch.ones(num_features))
            self.shift = nn.Parameter(torch.zeros(num_features))

        # BatchNorm的移动平均和方差（用于推理阶段）
        self.register_buffer('running_mean', torch.zeros(num_features))
        self.register_buffer('running_var', torch.ones(num_features))
        self.reset_parameters()

    def reset_parameters(self):
        """初始化参数"""
        self.running_mean.zero_()
        self.running_var.fill_(1)
        if self.affine:
            nn.init.ones_(self.scale)
            nn.init.zeros_(self.shift)
        nn.init.ones_(self.weight)  # 初始权重均等（1,1,1）
        nn.init.zeros_(self.bias)

    def forward(self, x):
        """
        前向传播：同时计算三种归一化，再加权融合

        输入x形状: 
            - 2D: (N, C) （MLP）
            - 4D: (N, C, H, W) （CNN）
        """
        N, C = x.size(0), x.size(1)
        dims = x.dim()  # 维度（2或4）

        # --------------------------
        # 1. 计算BatchNorm的结果
        # --------------------------
        if self.training:
            # 训练时：用当前批次计算均值和方差
            if dims == 4:  # 4D特征（CNN）：在(N, H, W)维度上求均值
                bn_mean = x.mean(dim=[0, 2, 3])
                bn_var = x.var(dim=[0, 2, 3], unbiased=False)
            else:  # 2D特征（MLP）：在(N)维度上求均值
                bn_mean = x.mean(dim=[0])
                bn_var = x.var(dim=[0], unbiased=False)

            # 更新移动平均（用于推理）
            with torch.no_grad():
                self.running_mean = self.momentum * \
                    self.running_mean + (1 - self.momentum) * bn_mean
                self.running_var = self.momentum * \
                    self.running_var + (1 - self.momentum) * bn_var
        else:
            # 推理时：用训练阶段的移动平均和方差
            bn_mean = self.running_mean
            bn_var = self.running_var

        # 标准化（BatchNorm）
        bn_out = F.batch_norm(
            x, bn_mean, bn_var, None, None, self.training, 0.0, self.eps
        )

        # --------------------------
        # 2. 计算LayerNorm的结果
        # --------------------------
        if dims == 4:  # 4D特征：在(C, H, W)维度上归一化（单个样本内）
            ln_mean = x.mean(dim=[1, 2, 3], keepdim=True)
            ln_var = x.var(dim=[1, 2, 3], keepdim=True, unbiased=False)
        else:  # 2D特征：在(C)维度上归一化（单个样本内）
            ln_mean = x.mean(dim=[1], keepdim=True)
            ln_var = x.var(dim=[1], keepdim=True, unbiased=False)

        ln_out = (x - ln_mean) / torch.sqrt(ln_var + self.eps)

        # --------------------------
        # 3. 计算InstanceNorm的结果
        # --------------------------
        if dims == 4:  # 4D特征：在(H, W)维度上归一化（单个样本的单个通道内）
            in_mean = x.mean(dim=[2, 3], keepdim=True)
            in_var = x.var(dim=[2, 3], keepdim=True, unbiased=False)
        else:  # 2D特征：退化为LayerNorm（原论文中InstanceNorm对2D效果有限）
            in_mean = x.mean(dim=[1], keepdim=True)
            in_var = x.var(dim=[1], keepdim=True, unbiased=False)

        in_out = (x - in_mean) / torch.sqrt(in_var + self.eps)

        # --------------------------
        # 4. 加权融合三种归一化结果
        # --------------------------
        # 权重归一化（softmax确保权重和为1）
        weights = F.softmax(self.weight, dim=0)

        # 融合：w_bn*bn_out + w_ln*ln_out + w_in*in_out
        out = weights[0] * bn_out + weights[1] * ln_out + weights[2] * in_out

        # 应用全局缩放和平移（若启用）
        if self.affine:
            if dims == 4:
                # 4D特征：scale和shift的形状为(C,1,1)，与(N,C,H,W)广播兼容
                out = out * self.scale.view(1, C, 1, 1) + \
                    self.shift.view(1, C, 1, 1)
            else:
                # 2D特征：scale和shift的形状为(C)，与(N,C)广播兼容
                out = out * self.scale + self.shift

        return out


# 测试：在CNN和MLP中使用SwitchNorm
# if __name__ == "__main__":
#     # 1. 测试4D特征（CNN场景）
#     cnn_input = torch.randn(2, 32, 64, 64)  # (N=2, C=32, H=64, W=64)
#     sn_cnn = SwitchNorm(num_features=32)
#     cnn_output = sn_cnn(cnn_input)
#     print(f"CNN输入形状: {cnn_input.shape}, 输出形状: {cnn_output.shape}")  # 应保持一致

#     # 2. 测试2D特征（MLP场景）
#     mlp_input = torch.randn(4, 64)  # (N=4, C=64)
#     sn_mlp = SwitchNorm(num_features=64)
#     mlp_output = sn_mlp(mlp_input)
#     print(f"MLP输入形状: {mlp_input.shape}, 输出形状: {mlp_output.shape}")  # 应保持一致

#     # 3. 检查权重是否可学习
#     print("融合权重（初始值应为均匀分布）:", F.softmax(sn_cnn.weight.detach(), dim=0))

class GroupNorm(nn.GroupNorm):
    r"""Applies Group Normalization over a mini-batch of inputs.

    This layer implements the operation as described in
    the paper `Group Normalization <https://arxiv.org/abs/1803.08494>`__

    .. math::
        y = \frac{x - \mathrm{E}[x]}{ \sqrt{\mathrm{Var}[x] + \epsilon}} * \gamma + \beta

    The input channels are separated into :attr:`num_groups` groups, each containing
    ``num_channels / num_groups`` channels. :attr:`num_channels` must be divisible by
    :attr:`num_groups`. The mean and standard-deviation are calculated
    separately over the each group. :math:`\gamma` and :math:`\beta` are learnable
    per-channel affine transform parameter vectors of size :attr:`num_channels` if
    :attr:`affine` is ``True``.
    The variance is calculated via the biased estimator, equivalent to
    `torch.var(input, unbiased=False)`.

    This layer uses statistics computed from input data in both training and
    evaluation modes.

    Args:
        num_groups (int): number of groups to separate the channels into
        num_channels (int): number of channels expected in input
        eps: a value added to the denominator for numerical stability. Default: 1e-5
        affine: a boolean value that when set to ``True``, this module
            has learnable per-channel affine parameters initialized to ones (for weights)
            and zeros (for biases). Default: ``True``.

    Shape:
        - Input: :math:`(N, C, *)` where :math:`C=\text{num\_channels}`
        - Output: :math:`(N, C, *)` (same shape as input)

    Examples::

        >>> input = torch.randn(20, 6, 10, 10)
        >>> # Separate 6 channels into 3 groups
        >>> m = nn.GroupNorm(3, 6)
        >>> # Separate 6 channels into 6 groups (equivalent with InstanceNorm)
        >>> m = nn.GroupNorm(6, 6)
        >>> # Put all 6 channels into a single group (equivalent with LayerNorm)
        >>> m = nn.GroupNorm(1, 6)
        >>> # Activating the module
        >>> output = m(input)
    """

    def __init__(
        self,
        num_channels: int,
        num_groups: int = 4,
        eps: float = 1e-5,
        affine: bool = True,
        device=None,
        dtype=None,
    ) -> None:
        super().__init__(num_groups,
                         num_channels,
                         eps,
                         affine,
                         device,
                         dtype)


class Norm(base):
    LayerNorm = nn.LayerNorm
    # BatchNorm1d = nn.BatchNorm1d
    # GroupNorm = GroupNorm
    InstanceNorm1d = nn.InstanceNorm1d
    # SpectralNorm = nn.utils.spectral_norm
    # SwitchNorm = SwitchNorm


Norm = Norm()
