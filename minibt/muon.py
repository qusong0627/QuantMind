import os
import logging
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

import torch
import torch.distributed as dist
from torch.optim import Optimizer
logger = logging.getLogger(__name__)
# 完全禁用 torch.compile
os.environ["TORCHDYNAMO_DISABLE"] = "1"

__all__ = ["AdaMuon", "MomMuon", "OGSignMuon","PionOptimizer","MuonOptimizer"]


def zeropower_via_newtonschulz5(G: torch.Tensor, steps: int) -> torch.Tensor:
    """
    Newton-Schulz iteration to compute the zeroth power / orthogonalization of G.

    Args:
        G: Input matrix to orthogonalize
        steps: Number of Newton-Schulz iterations

    Returns:
        Orthogonalized matrix
    """
    assert len(G.shape) == 2, "Input must be a 2D matrix"
    a, b, c = (3.4445, -4.7750, 2.0315)

    # 使用与输入相同的设备类型
    X = G.clone()
    if G.size(0) > G.size(1):
        X = X.T

    # 确保谱范数不超过1，添加更安全的归一化
    norm_val = X.norm()
    if norm_val > 0:
        X = X / (norm_val + 1e-12)  # 更小的 epsilon 以提高数值稳定性
    else:
        X = X / 1e-12  # 避免除零错误

    # 执行 NS 迭代
    for _ in range(steps):
        A = X @ X.T
        B = b * A + c * A @ A
        X = a * X + B @ X

    if G.size(0) > G.size(1):
        X = X.T

    return X


class AdaMuon(torch.optim.Optimizer):
    """
    Muon - MomentUm Orthogonalized by Newton-schulz

    Muon internally runs standard SGD-momentum, and then performs an orthogonalization post-
    processing step, in which each 2D parameter's update is replaced with the nearest orthogonal
    matrix. To efficiently orthogonalize each update, we use a Newton-Schulz iteration.

    Arguments:
        muon_params: The parameters to be optimized by Muon.
        lr: The learning rate. The updates will have spectral norm of `lr`. (0.02 is a good default)
        momentum: The momentum used by the internal SGD. (0.95 is a good default)
        beta2: The beta2 for gradient norm EMA.
        nesterov: Whether to use Nesterov-style momentum in the internal SGD. (recommended)
        ns_steps: The number of Newton-Schulz iterations to run. (6 is probably always enough)
        adamw_params: The parameters to be optimized by AdamW.
        adamw_lr: The learning rate for the internal AdamW.
        adamw_betas: The betas for the internal AdamW.
        adamw_eps: The epsilon for the internal AdamW.
        adamw_wd: The weight decay for the internal AdamW.
        input_feature_dim: The dimension of the input features (e.g., state_dim). If provided, parameters
        containing this dimension in their shape will be excluded from Muon optimization.
        debug_mode: Whether to enable debug logging.
    """

    def __init__(self, muon_params: list[torch.Tensor], lr: float = 0.02, momentum: float = 0.95,
                 beta2: float = 0.995, nesterov: bool = True, ns_steps: int = 6,
                 adamw_params: list[torch.Tensor] | None = None, adamw_lr: float = 3e-4,
                 adamw_betas: tuple[float, float] = (0.95, 0.95), adamw_eps: float = 1e-8,
                 adamw_wd: float = 0, input_feature_dim: int | None = None,
                 debug_mode: bool = False):

        defaults = dict(
            lr=lr, momentum=momentum, beta2=beta2, nesterov=nesterov, ns_steps=ns_steps,
            adamw_lr_ratio=adamw_lr/lr, adamw_betas=adamw_betas,
            adamw_eps=adamw_eps, adamw_wd=adamw_wd
        )

        params = list(muon_params)
        adamw_params = list(adamw_params) if adamw_params is not None else []
        params.extend(adamw_params)
        super().__init__(params, defaults)

        # 初始化步数计数器
        self.step_count = 0
        self.debug_mode = debug_mode

        # 将参数分类为使用 Muon 或不使用 Muon
        for p in muon_params:
            # 使用 Muon 的参数：维度≥2且不包含输入特征维度
            if p.ndim >= 2 and (input_feature_dim is None or all(s != input_feature_dim for s in p.shape)):
                self.state[p]['use_muon'] = True
            else:
                self.state[p]['use_muon'] = False

        for p in adamw_params:
            # 不使用 Muon 的参数
            self.state[p]['use_muon'] = False

        # 分布式训练设置
        if 'WORLD_SIZE' in os.environ:
            self.world_size = int(os.environ['WORLD_SIZE'])
            self.rank = int(os.environ['RANK'])
        else:
            self.world_size = 1
            self.rank = 0

    def step(self, closure=None):
        """Perform a single optimization step.

        Args:
            closure (Callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        try:
            loss = None
            if closure is not None:
                with torch.enable_grad():
                    loss = closure()

            # 更新步数计数器
            self.step_count += 1

            # 记录优化器状态
            # if self.debug_mode and self.rank == 0 and self.step_count % 100 == 0:
            #     self._log_optimizer_state()

            for group in self.param_groups:
                # 处理 Muon 参数
                self._process_muon_params(group)

                # 处理 AdamW 参数
                self._process_adamw_params(group)

            return loss

        except Exception as e:
            # 错误处理
            print(f"Error in AdaMuon step {self.step_count}: {e}")
            if self.debug_mode:
                import traceback
                traceback.print_exc()
            # 回退到标准 SGD 优化
            return self._fallback_step(closure)

    def _process_muon_params(self, group):
        """处理使用 Muon 优化的参数"""
        params = [p for p in group['params']
                  if self.state[p].get('use_muon', False)]
        if not params:
            return

        device = params[0].device
        lr = group['lr']
        momentum = group['momentum']
        beta2 = group['beta2']
        ns_steps = group['ns_steps']
        nesterov = group['nesterov']

        # 预计算所有需要的梯度
        grads = []
        param_info = []  # 存储参数索引和大小信息
        total_size = 0

        for i, p in enumerate(params):
            if i % self.world_size != self.rank:
                continue

            g = p.grad
            if g is None:
                continue

            # 处理高维参数
            if g.ndim > 2:
                g = g.view(g.size(0), -1)

            grads.append(g)
            param_info.append((i, p.numel(), p.shape))
            total_size += p.numel()

        # 如果没有梯度需要处理，直接返回
        if not grads:
            return

        # 根据设备类型选择合适的数据类型
        dtype = torch.bfloat16 if device.type == 'cuda' else torch.float32
        updates_flat = torch.zeros(total_size, device=device, dtype=dtype)
        curr_idx = 0

        for (i, p_numel, p_shape), g in zip(param_info, grads):
            state = self.state[params[i]]

            # 初始化状态
            if 'momentum_buffer' not in state:
                state['momentum_buffer'] = torch.zeros_like(g)
                state['grad_norm_ema'] = torch.zeros(1, device=device)

            buf = state['momentum_buffer']
            buf.mul_(momentum).add_(g)

            # 应用 Nesterov 动量
            if nesterov:
                g_update = g.add(buf, alpha=momentum)
            else:
                g_update = buf.clone()

            # 计算梯度范数
            og_norm = g_update.norm()
            grad_norm_ema = state['grad_norm_ema']
            grad_norm_ema.lerp_(og_norm**2, 1 - beta2)

            # 正交化处理
            try:
                g_ortho = zeropower_via_newtonschulz5(g_update, steps=ns_steps)
            except Exception as e:
                if self.debug_mode:
                    print(f"NS iteration failed: {e}")
                g_ortho = g_update  # 失败时回退到原始梯度

            # 保持梯度范数
            ortho_norm = g_ortho.norm()
            if ortho_norm > 1e-12:  # 避免除零错误
                g_ortho = g_ortho * (og_norm / ortho_norm)

            # 应用梯度归一化
            if grad_norm_ema > 0:
                g_ortho = g_ortho / (torch.sqrt(grad_norm_ema) + 1e-12)

            # 存储更新
            updates_flat[curr_idx:curr_idx + p_numel] = g_ortho.flatten()
            curr_idx += p_numel

        # 分布式同步
        if self.world_size > 1:
            dist.all_reduce(updates_flat, op=dist.ReduceOp.SUM)

        # 应用更新
        curr_idx = 0
        for i, p_numel, p_shape in param_info:
            g_update = updates_flat[curr_idx:curr_idx + p_numel].view(p_shape)
            params[i].data.add_(g_update.type_as(params[i].data), alpha=-lr)
            curr_idx += p_numel

    def _process_adamw_params(self, group):
        """处理使用 AdamW 优化的参数"""
        params = [p for p in group['params']
                  if not self.state[p].get('use_muon', True)]
        if not params:
            return

        lr = group['adamw_lr_ratio'] * group['lr']  # 考虑学习率调度
        beta1, beta2 = group['adamw_betas']
        eps = group['adamw_eps']
        weight_decay = group['adamw_wd']

        for p in params:
            g = p.grad
            if g is None:
                continue

            state = self.state[p]

            # 初始化状态
            if 'step' not in state:
                state['step'] = 0
                state['moment1'] = torch.zeros_like(g)
                state['moment2'] = torch.zeros_like(g)

            state['step'] += 1
            step = state['step']
            buf1 = state['moment1']
            buf2 = state['moment2']

            # 更新动量
            buf1.lerp_(g, 1 - beta1)
            buf2.lerp_(g.square(), 1 - beta2)

            # 计算更新
            g_update = buf1 / (eps + buf2.sqrt())

            # 偏差校正
            bias_correction1 = 1 - beta1 ** step
            bias_correction2 = 1 - beta2 ** step
            scale = bias_correction1 / (bias_correction2 ** 0.5 + 1e-12)

            # 应用权重衰减
            p.data.mul_(1 - lr * weight_decay)

            # 应用更新
            p.data.add_(g_update, alpha=-lr / scale)

    def _log_optimizer_state(self):
        """记录优化器状态用于调试"""
        muon_params = sum(1 for p in self.param_groups[0]['params']
                          if self.state[p].get('use_muon', False))
        adamw_params = sum(1 for p in self.param_groups[0]['params']
                           if not self.state[p].get('use_muon', True))

        print(
            f"AdaMuon step {self.step_count}: {muon_params} Muon params, {adamw_params} AdamW params")

    def _fallback_step(self, closure=None):
        """回退到标准 SGD 优化"""
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            lr = group['lr']
            momentum = group['momentum']
            nesterov = group['nesterov']

            for p in group['params']:
                if p.grad is None:
                    continue

                d_p = p.grad

                # 应用动量
                state = self.state[p]
                if 'momentum_buffer' not in state:
                    state['momentum_buffer'] = torch.zeros_like(d_p)

                buf = state['momentum_buffer']
                buf.mul_(momentum).add_(d_p)

                # 应用 Nesterov 动量
                if nesterov:
                    d_p = d_p.add(buf, alpha=momentum)
                else:
                    d_p = buf

                # 应用更新
                p.data.add_(d_p, alpha=-lr)

        return loss


def gradwhiten(grad: torch.Tensor, ns_steps: int = 6, beta: float = 0.5) -> torch.Tensor:
    """
    Implements the GradWhitening operator as described in Algorithm 2.

    Args:
        grad: Input matrix G of shape (m x n) where m <= n
        ns_steps: Number of Newton-Schulz iterations (default: 6)
        beta: Step size for Newton-Schulz iterations (default: 0.5)

    Returns:
        Whitened gradient ZG where Z approximates (GG^T)^(-1/2)
    """
    # 确保输入是2D矩阵
    if grad.ndim != 2:
        raise ValueError("grad must be a 2D matrix")

    # 初始化
    norm_val = grad.norm()
    if norm_val > 0:
        grad = grad / (norm_val + 1e-12)  # 更安全的归一化
    else:
        grad = grad / 1e-12  # 避免除零错误

    Y = grad @ grad.T
    Z = torch.eye(Y.size(0), device=grad.device, dtype=grad.dtype)
    I3 = 3 * Z

    # Newton-Schulz 迭代
    for _ in range(ns_steps):
        ZY = Z @ Y
        I3_minus_ZY = I3 - ZY
        Y = beta * (Y @ I3_minus_ZY)
        Z = beta * (I3_minus_ZY @ Z)

    return Z


class MomMuon(torch.optim.Optimizer):
    """
    Muon - MomentUm Orthogonalized by Newton-schulz

    Muon internally runs standard SGD-momentum, and then performs an orthogonalization post-
    processing step, in which each 2D parameter's update is replaced with the nearest orthogonal
    matrix. To efficiently orthogonalize each update, we use a Newton-Schulz iteration.

    Arguments:
        muon_params: The parameters to be optimized by Muon.
        lr: The learning rate. The updates will have spectral norm of `lr`. (0.02 is a good default)
        momentum: The momentum used by the internal SGD. (0.95 is a good default)
        beta2: The beta2 for gradient norm EMA.
        ns_beta: Beta parameter for preconditioner EMA.
        ns_every: Apply NS iteration every N steps.
        nesterov: Whether to use Nesterov-style momentum in the internal SGD. (recommended)
        ns_steps: The number of Newton-Schulz iterations to run. (6 is probably always enough)
        adamw_params: The parameters to be optimized by AdamW.
        adamw_lr: The learning rate for the internal AdamW.
        adamw_betas: The betas for the internal AdamW.
        adamw_eps: The epsilon for the internal AdamW.
        adamw_wd: The weight decay for the internal AdamW.
        input_feature_dim: The dimension of the input features (e.g., state_dim). If provided, parameters
        containing this dimension in their shape will be excluded from Muon optimization.
        debug_mode: Whether to enable debug logging.
    """

    def __init__(self, muon_params: list[torch.Tensor], lr: float = 0.02, momentum: float = 0.95,
                 beta2: float = 0.995, ns_beta: float = 0.9, ns_every: int = 1, nesterov: bool = True,
                 ns_steps: int = 15, adamw_params: list[torch.Tensor] | None = None,
                 adamw_lr: float = 3e-4, adamw_betas: tuple[float, float] = (0.95, 0.95),
                 adamw_eps: float = 1e-8, adamw_wd: float = 0,
                 input_feature_dim: int | None = None, debug_mode: bool = False):

        defaults = dict(
            lr=lr, momentum=momentum, beta2=beta2, ns_beta=ns_beta, ns_every=ns_every,
            nesterov=nesterov, ns_steps=ns_steps,
            adamw_lr_ratio=adamw_lr/lr, adamw_betas=adamw_betas,
            adamw_eps=adamw_eps, adamw_wd=adamw_wd
        )

        params = list(muon_params)
        adamw_params = list(adamw_params) if adamw_params is not None else []
        params.extend(adamw_params)
        super().__init__(params, defaults)

        # 初始化步数计数器
        self.step_count = 0
        self.debug_mode = debug_mode

        # 将参数分类为使用 Muon 或不使用 Muon
        for p in muon_params:
            # 使用 Muon 的参数：维度≥2且不包含输入特征维度
            if p.ndim >= 2 and (input_feature_dim is None or all(s != input_feature_dim for s in p.shape)):
                self.state[p]['use_muon'] = True
            else:
                self.state[p]['use_muon'] = False

        for p in adamw_params:
            # 不使用 Muon 的参数
            self.state[p]['use_muon'] = False

        # 分布式训练设置
        if 'WORLD_SIZE' in os.environ:
            self.world_size = int(os.environ['WORLD_SIZE'])
            self.rank = int(os.environ['RANK'])
        else:
            self.world_size = 1
            self.rank = 0

    def step(self, closure=None):
        """Perform a single optimization step.

        Args:
            closure (Callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        try:
            loss = None
            if closure is not None:
                with torch.enable_grad():
                    loss = closure()

            # 更新步数计数器
            self.step_count += 1

            # 记录优化器状态
            # if self.debug_mode and self.rank == 0 and self.step_count % 100 == 0:
            #     self._log_optimizer_state()

            for group in self.param_groups:
                # 处理 Muon 参数
                self._process_muon_params(group)

                # 处理 AdamW 参数
                self._process_adamw_params(group)

            return loss

        except Exception as e:
            # 错误处理
            print(f"Error in MomMuon step {self.step_count}: {e}")
            if self.debug_mode:
                import traceback
                traceback.print_exc()
            # 回退到标准 SGD 优化
            return self._fallback_step(closure)

    def _process_muon_params(self, group):
        """处理使用 Muon 优化的参数"""
        params = [p for p in group['params']
                  if self.state[p].get('use_muon', False)]
        if not params:
            return

        device = params[0].device
        lr = group['lr']
        momentum = group['momentum']
        ns_beta = group['ns_beta']
        ns_every = group['ns_every']
        ns_steps = group['ns_steps']
        nesterov = group['nesterov']

        # 预计算所有需要的梯度
        grads = []
        param_info = []  # 存储参数索引和大小信息
        total_size = 0

        for i, p in enumerate(params):
            if i % self.world_size != self.rank:
                continue

            g = p.grad
            if g is None:
                continue

            # 处理高维参数
            if g.ndim > 2:
                g = g.view(g.size(0), -1)

            grads.append(g)
            param_info.append((i, p.numel(), p.shape))
            total_size += p.numel()

        # 如果没有梯度需要处理，直接返回
        if not grads:
            return

        # 根据设备类型选择合适的数据类型
        dtype = torch.bfloat16 if device.type == 'cuda' else torch.float32
        updates_flat = torch.zeros(total_size, device=device, dtype=dtype)
        curr_idx = 0

        for (i, p_numel, p_shape), g in zip(param_info, grads):
            state = self.state[params[i]]

            # 初始化状态
            if 'step' not in state:
                state['step'] = 0
            if 'momentum_buffer' not in state:
                state['momentum_buffer'] = torch.zeros_like(g)

            buf = state['momentum_buffer']
            buf.mul_(momentum).add_(g)

            # 应用 Nesterov 动量
            if nesterov:
                g_update = g.add(buf, alpha=momentum)
            else:
                g_update = buf.clone()

            # 应用 NS 预处理
            if (ns_every > 0 and state['step'] % ns_every == 0) or state['step'] == 0:
                try:
                    precond = gradwhiten(g_update, ns_steps=ns_steps)
                except Exception as e:
                    if self.debug_mode:
                        print(f"GradWhitening failed: {e}")
                    precond = torch.eye(g_update.size(
                        0), device=device, dtype=g_update.dtype)

                # 初始化或更新预处理指数平均值
                if "precond_exp_avg" not in state:
                    state["precond_exp_avg"] = precond
                else:
                    state["precond_exp_avg"].lerp_(precond, 1 - ns_beta)

            # 应用预处理
            g_processed = state["precond_exp_avg"] @ g_update

            # 缩放因子
            scaling_factor = max(1, g_processed.size(
                0) / g_processed.size(1)) ** 0.5
            g_processed = g_processed * scaling_factor

            # 存储更新
            updates_flat[curr_idx:curr_idx + p_numel] = g_processed.flatten()
            curr_idx += p_numel

            # 更新步数
            state['step'] += 1

        # 分布式同步
        if self.world_size > 1:
            dist.all_reduce(updates_flat, op=dist.ReduceOp.SUM)

        # 应用更新
        curr_idx = 0
        for i, p_numel, p_shape in param_info:
            g_update = updates_flat[curr_idx:curr_idx + p_numel].view(p_shape)
            params[i].data.add_(g_update.type_as(params[i].data), alpha=-lr)
            curr_idx += p_numel

    def _process_adamw_params(self, group):
        """处理使用 AdamW 优化的参数"""
        params = [p for p in group['params']
                  if not self.state[p].get('use_muon', True)]
        if not params:
            return

        lr = group['adamw_lr_ratio'] * group['lr']  # 考虑学习率调度
        beta1, beta2 = group['adamw_betas']
        eps = group['adamw_eps']
        weight_decay = group['adamw_wd']

        for p in params:
            g = p.grad
            if g is None:
                continue

            state = self.state[p]

            # 初始化状态
            if 'step' not in state:
                state['step'] = 0
                state['moment1'] = torch.zeros_like(g)
                state['moment2'] = torch.zeros_like(g)

            state['step'] += 1
            step = state['step']
            buf1 = state['moment1']
            buf2 = state['moment2']

            # 更新动量
            buf1.lerp_(g, 1 - beta1)
            buf2.lerp_(g.square(), 1 - beta2)

            # 计算更新
            g_update = buf1 / (eps + buf2.sqrt())

            # 偏差校正
            bias_correction1 = 1 - beta1 ** step
            bias_correction2 = 1 - beta2 ** step
            scale = bias_correction1 / (bias_correction2 ** 0.5 + 1e-12)

            # 应用权重衰减
            p.data.mul_(1 - lr * weight_decay)

            # 应用更新
            p.data.add_(g_update, alpha=-lr / scale)

    def _log_optimizer_state(self):
        """记录优化器状态用于调试"""
        muon_params = sum(1 for p in self.param_groups[0]['params']
                          if self.state[p].get('use_muon', False))
        adamw_params = sum(1 for p in self.param_groups[0]['params']
                           if not self.state[p].get('use_muon', True))

        print(
            f"MomMuon step {self.step_count}: {muon_params} Muon params, {adamw_params} AdamW params")

    def _fallback_step(self, closure=None):
        """回退到标准 SGD 优化"""
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            lr = group['lr']
            momentum = group['momentum']
            nesterov = group['nesterov']

            for p in group['params']:
                if p.grad is None:
                    continue

                d_p = p.grad

                # 应用动量
                state = self.state[p]
                if 'momentum_buffer' not in state:
                    state['momentum_buffer'] = torch.zeros_like(d_p)

                buf = state['momentum_buffer']
                buf.mul_(momentum).add_(d_p)

                # 应用 Nesterov 动量
                if nesterov:
                    d_p = d_p.add(buf, alpha=momentum)
                else:
                    d_p = buf

                # 应用更新
                p.data.add_(d_p, alpha=-lr)

        return loss


def zeropower_via_newtonschulz5(G: torch.Tensor, steps: int) -> torch.Tensor:
    """
    Newton-Schulz iteration to compute the zeroth power / orthogonalization of G.

    Args:
        G: Input matrix to orthogonalize
        steps: Number of Newton-Schulz iterations

    Returns:
        Orthogonalized matrix
    """
    assert len(G.shape) == 2, "Input must be a 2D matrix"
    a, b, c = (3.4445, -4.7750, 2.0315)

    # 使用与输入相同的设备类型
    X = G.clone()
    if G.size(0) > G.size(1):
        X = X.T

    # 确保谱范数不超过1，添加更安全的归一化
    norm_val = X.norm()
    if norm_val > 0:
        X = X / (norm_val + 1e-12)  # 更小的 epsilon 以提高数值稳定性
    else:
        X = X / 1e-12  # 避免除零错误

    # 执行 NS 迭代
    for _ in range(steps):
        A = X @ X.T
        B = b * A + c * A @ A
        X = a * X + B @ X

    if G.size(0) > G.size(1):
        X = X.T

    return X


class OGSignMuon(torch.optim.Optimizer):
    """
    Muon - MomentUm Orthogonalized by Newton-schulz with Sign Preservation

    Muon internally runs standard SGD-momentum, and then performs an orthogonalization post-
    processing step, in which each 2D parameter's update is replaced with the nearest orthogonal
    matrix. To efficiently orthogonalize each update, we use a Newton-Schulz iteration.

    Arguments:
        muon_params: The parameters to be optimized by Muon.
        lr: The learning rate. The updates will have spectral norm of `lr`. (0.02 is a good default)
        momentum: The momentum used by the internal SGD. (0.95 is a good default)
        nesterov: Whether to use Nesterov-style momentum in the internal SGD. (recommended)
        ns_steps: The number of Newton-Schulz iterations to run. (6 is probably always enough)
        adamw_params: The parameters to be optimized by AdamW.
        adamw_lr: The learning rate for the internal AdamW.
        adamw_betas: The betas for the internal AdamW.
        adamw_eps: The epsilon for the internal AdamW.
        adamw_wd: The weight decay for the internal AdamW.
        input_feature_dim: The dimension of the input features (e.g., state_dim). If provided, parameters
        containing this dimension in their shape will be excluded from Muon optimization.
        debug_mode: Whether to enable debug logging.
    """

    def __init__(self, muon_params: list[torch.Tensor], lr: float = 0.02, momentum: float = 0.95,
                 nesterov: bool = True, ns_steps: int = 6, adamw_params: list[torch.Tensor] | None = None,
                 adamw_lr: float = 3e-4, adamw_betas: tuple[float, float] = (0.95, 0.95),
                 adamw_eps: float = 1e-8, adamw_wd: float = 0,
                 input_feature_dim: int | None = None, debug_mode: bool = False):

        defaults = dict(
            lr=lr, momentum=momentum, nesterov=nesterov, ns_steps=ns_steps,
            adamw_lr_ratio=adamw_lr/lr, adamw_betas=adamw_betas,
            adamw_eps=adamw_eps, adamw_wd=adamw_wd
        )

        params = list(muon_params)
        adamw_params = list(adamw_params) if adamw_params is not None else []
        params.extend(adamw_params)
        super().__init__(params, defaults)

        # 初始化步数计数器
        self.step_count = 0
        self.debug_mode = debug_mode

        # 将参数分类为使用 Muon 或不使用 Muon
        for p in muon_params:
            # 使用 Muon 的参数：维度≥2且不包含输入特征维度
            if p.ndim >= 2 and (input_feature_dim is None or all(s != input_feature_dim for s in p.shape)):
                self.state[p]['use_muon'] = True
            else:
                self.state[p]['use_muon'] = False

        for p in adamw_params:
            # 不使用 Muon 的参数
            self.state[p]['use_muon'] = False

        # 分布式训练设置
        if 'WORLD_SIZE' in os.environ:
            self.world_size = int(os.environ['WORLD_SIZE'])
            self.rank = int(os.environ['RANK'])
        else:
            self.world_size = 1
            self.rank = 0

    def step(self, closure=None):
        """Perform a single optimization step.

        Args:
            closure (Callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        try:
            loss = None
            if closure is not None:
                with torch.enable_grad():
                    loss = closure()

            # 更新步数计数器
            self.step_count += 1

            # 记录优化器状态
            if self.debug_mode and self.rank == 0 and self.step_count % 100 == 0:
                self._log_optimizer_state()

            for group in self.param_groups:
                # 处理 Muon 参数
                self._process_muon_params(group)

                # 处理 AdamW 参数
                self._process_adamw_params(group)

            return loss

        except Exception as e:
            # 错误处理
            print(f"Error in OGSignMuon step {self.step_count}: {e}")
            if self.debug_mode:
                import traceback
                traceback.print_exc()
            # 回退到标准 SGD 优化
            return self._fallback_step(closure)

    def _process_muon_params(self, group):
        """处理使用 Muon 优化的参数"""
        params = [p for p in group['params']
                  if self.state[p].get('use_muon', False)]
        if not params:
            return

        device = params[0].device
        lr = group['lr']
        momentum = group['momentum']
        ns_steps = group['ns_steps']
        nesterov = group['nesterov']

        # 预计算所有需要的梯度
        grads = []
        param_info = []  # 存储参数索引和大小信息
        total_size = 0

        for i, p in enumerate(params):
            if i % self.world_size != self.rank:
                continue

            g = p.grad
            if g is None:
                continue

            # 处理高维参数
            if g.ndim > 2:
                g = g.view(g.size(0), -1)

            grads.append(g)
            param_info.append((i, p.numel(), p.shape))
            total_size += p.numel()

        # 如果没有梯度需要处理，直接返回
        if not grads:
            return

        # 根据设备类型选择合适的数据类型
        dtype = torch.bfloat16 if device.type == 'cuda' else torch.float32
        updates_flat = torch.zeros(total_size, device=device, dtype=dtype)
        curr_idx = 0

        for (i, p_numel, p_shape), g in zip(param_info, grads):
            state = self.state[params[i]]

            # 初始化状态
            if 'momentum_buffer' not in state:
                state['momentum_buffer'] = torch.zeros_like(g)

            buf = state['momentum_buffer']
            buf.mul_(momentum).add_(g)

            # 应用 Nesterov 动量
            if nesterov:
                g_update = g.add(buf, alpha=momentum)
            else:
                g_update = buf.clone()

            # 保存符号信息
            sign_mask = g_update.sign()

            # 应用 NS 正交化
            try:
                g_ortho = zeropower_via_newtonschulz5(g_update, steps=ns_steps)
            except Exception as e:
                if self.debug_mode:
                    print(f"NS iteration failed: {e}")
                g_ortho = g_update  # 失败时回退到原始梯度

            # 恢复符号信息
            g_processed = g_ortho.abs() * sign_mask

            # 缩放因子
            scaling_factor = max(1, g_processed.size(
                0) / g_processed.size(1)) ** 0.5
            g_processed = g_processed * scaling_factor

            # 存储更新
            updates_flat[curr_idx:curr_idx + p_numel] = g_processed.flatten()
            curr_idx += p_numel

        # 分布式同步
        if self.world_size > 1:
            dist.all_reduce(updates_flat, op=dist.ReduceOp.SUM)

        # 应用更新
        curr_idx = 0
        for i, p_numel, p_shape in param_info:
            g_update = updates_flat[curr_idx:curr_idx + p_numel].view(p_shape)
            params[i].data.add_(g_update.type_as(params[i].data), alpha=-lr)
            curr_idx += p_numel

    def _process_adamw_params(self, group):
        """处理使用 AdamW 优化的参数"""
        params = [p for p in group['params']
                  if not self.state[p].get('use_muon', True)]
        if not params:
            return

        lr = group['adamw_lr_ratio'] * group['lr']  # 考虑学习率调度
        beta1, beta2 = group['adamw_betas']
        eps = group['adamw_eps']
        weight_decay = group['adamw_wd']

        for p in params:
            g = p.grad
            if g is None:
                continue

            state = self.state[p]

            # 初始化状态
            if 'step' not in state:
                state['step'] = 0
                state['moment1'] = torch.zeros_like(g)
                state['moment2'] = torch.zeros_like(g)

            state['step'] += 1
            step = state['step']
            buf1 = state['moment1']
            buf2 = state['moment2']

            # 更新动量
            buf1.lerp_(g, 1 - beta1)
            buf2.lerp_(g.square(), 1 - beta2)

            # 计算更新
            g_update = buf1 / (eps + buf2.sqrt())

            # 偏差校正
            bias_correction1 = 1 - beta1 ** step
            bias_correction2 = 1 - beta2 ** step
            scale = bias_correction1 / (bias_correction2 ** 0.5 + 1e-12)

            # 应用权重衰减
            p.data.mul_(1 - lr * weight_decay)

            # 应用更新
            p.data.add_(g_update, alpha=-lr / scale)

    def _log_optimizer_state(self):
        """记录优化器状态用于调试"""
        muon_params = sum(1 for p in self.param_groups[0]['params']
                          if self.state[p].get('use_muon', False))
        adamw_params = sum(1 for p in self.param_groups[0]['params']
                           if not self.state[p].get('use_muon', True))

        print(
            f"OGSignMuon step {self.step_count}: {muon_params} Muon params, {adamw_params} AdamW params")

    def _fallback_step(self, closure=None):
        """回退到标准 SGD 优化"""
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            lr = group['lr']
            momentum = group['momentum']
            nesterov = group['nesterov']

            for p in group['params']:
                if p.grad is None:
                    continue

                d_p = p.grad

                # 应用动量
                state = self.state[p]
                if 'momentum_buffer' not in state:
                    state['momentum_buffer'] = torch.zeros_like(d_p)

                buf = state['momentum_buffer']
                buf.mul_(momentum).add_(d_p)

                # 应用 Nesterov 动量
                if nesterov:
                    d_p = d_p.add(buf, alpha=momentum)
                else:
                    d_p = buf

                # 应用更新
                p.data.add_(d_p, alpha=-lr)

        return loss

def zeropower_via_newtonschulz5(g: torch.Tensor, steps: int) -> torch.Tensor:
    """Newton-Schulz iteration for approximate orthogonalization."""
    assert g.ndim == 2
    a, b, c = (3.4445, -4.7750, 2.0315)
    x = g.bfloat16()
    if g.size(0) > g.size(1):
        x = x.T
    x = x / (x.norm() + 1e-7)
    for _ in range(steps):
        a_mat = x @ x.T
        b_mat = b * a_mat + c * a_mat @ a_mat
        x = a * x + b_mat @ x
    if g.size(0) > g.size(1):
        x = x.T
    return x


def _route_to_adamw_by_name(param_name: str) -> bool:
    if not param_name:
        return False
    n = param_name.lower()
    tokens = (
        "lm_head",
        "embed_tokens",
        "word_embeddings",
        "tok_embeddings",
        "token_embedding",
        "wte",
        "embedding",
    )
    return any(t in n for t in tokens)


def muon_optimizer_requires_use_orig_params(optimizer_name: str) -> bool:
    return (optimizer_name or "").strip().lower() == "muonoptimizer"


def muon_optimizer_selected_from_config(optim_config: Any) -> bool:
    if optim_config is None:
        return False
    name: Optional[str] = None
    try:
        from omegaconf import OmegaConf

        if OmegaConf.is_config(optim_config):
            sel = OmegaConf.select(optim_config, "optimizer", default=None)
            name = str(sel) if sel is not None else None
    except Exception:
        name = None
    if name is None and isinstance(optim_config, dict):
        o = optim_config.get("optimizer")
        name = str(o) if o is not None else None
    if name is None:
        o = getattr(optim_config, "optimizer", None)
        if o is not None:
            name = str(o)
    if name is None:
        try:
            o = optim_config.get("optimizer", None)  # type: ignore[attr-defined]
            if o is not None:
                name = str(o)
        except Exception:
            pass
    if not name:
        return False
    return muon_optimizer_requires_use_orig_params(name)


def prepare_muon_module_tags(
    module: torch.nn.Module,
    *,
    optimizer_name: str,
    override_optimizer_config: Any = None,
) -> None:
    del override_optimizer_config
    if not muon_optimizer_requires_use_orig_params(optimizer_name):
        return
    for name, param in module.named_parameters():
        if param.ndim == 2:
            setattr(param, "_muon_param_name", name)


def assert_fsdp_orig_params_effective_for_muon(
    fsdp_module: torch.nn.Module,
    *,
    intended_use_orig: bool,
    role: str,
    optim_config: Any,
    fsdp_strategy: str,
    rank: int,
) -> None:
    if fsdp_strategy != "fsdp" or not intended_use_orig or role != "actor" or optim_config is None:
        return
    if not muon_optimizer_selected_from_config(optim_config):
        return
    try:
        from torch.distributed.fsdp import FlatParameter, FullyShardedDataParallel as FSDPCls
    except ImportError:
        return
    subs = FSDPCls.fsdp_modules(fsdp_module)
    bad_flags = [m for m in subs if getattr(m, "_use_orig_params", True) is False]
    if bad_flags:
        raise RuntimeError(
            f"FSDP use_orig_params=True was requested, but {len(bad_flags)} nested FSDP module(s) have "
            f"_use_orig_params=False (first: {bad_flags[0]!r})."
        )
    flat_named = [(n, tuple(p.shape)) for n, p in fsdp_module.named_parameters() if isinstance(p, FlatParameter)]
    if flat_named:
        raise RuntimeError(
            "FSDP use_orig_params=True but FlatParameter still appears in named_parameters (e.g. "
            f"{flat_named[0]})."
        )

    trainable = [(n, p) for n, p in fsdp_module.named_parameters() if p.requires_grad and p.numel() > 0]
    twod = [n for n, p in trainable if p.ndim == 2]
    ndim_hist: dict[int, int] = {}
    for _, p in trainable:
        ndim_hist[p.ndim] = ndim_hist.get(p.ndim, 0) + 1
    if trainable and not twod:
        raise RuntimeError(
            "Muon + FSDP1: use_orig_params=True was passed to FSDP(), but there are no 2D trainable "
            f"parameters under named_parameters (ndim histogram: {ndim_hist})."
        )
    if rank == 0:
        logger.info(
            "[FSDP/Muon] trainable=%s with_ndim==2=%s ndim_hist=%s",
            len(trainable),
            len(twod),
            ndim_hist,
        )


class MuonOptimizer(Optimizer):
    """Muon for 2D params + AdamW fallback for non-2D params."""

    def __init__(
        self,
        params,
        lr: float = 1e-4,
        weight_decay: float = 0.01,
        momentum: float = 0.95,
        nesterov: bool = True,
        ns_steps: int = 5,
        betas: Tuple[float, float] = (0.9, 0.999),
        eps: float = 1e-8,
        **kwargs: Any,
    ):
        import warnings

        kwargs.pop("max_lr", None)
        for _silent in (
            "fused",
            "foreach",
            "capturable",
            "maximize",
            "differentiable",
            "bf16_stochastic_round",
            "master_weights",
            "store_param_remainders",
            "exp_avg_dtype",
            "exp_avg_sq_dtype",
            "master_weight_dtype",
        ):
            kwargs.pop(_silent, None)
        if kwargs:
            warnings.warn(
                f"MuonOptimizer: ignoring unknown config keys {sorted(kwargs.keys())}",
                UserWarning,
                stacklevel=2,
            )

        param_list = list(params)
        muon_params = []
        adamw_params = []
        for p in param_list:
            if not p.requires_grad:
                continue
            param_name = str(getattr(p, "_muon_param_name", "") or "")
            force_adamw = _route_to_adamw_by_name(param_name)
            if p.ndim == 2 and not getattr(p, "_muon_skip", False) and not force_adamw:
                print(f"Muon matrix param {param_name} shape={tuple(p.shape)}")
                muon_params.append(p)
            else:
                print(f"Muon AdamW param {param_name} shape={tuple(p.shape)} ndim={p.ndim} type={type(p).__name__}")
                adamw_params.append(p)

        defaults = dict(
            lr=lr,
            weight_decay=weight_decay,
            momentum=momentum,
            nesterov=nesterov,
            ns_steps=ns_steps,
            betas=betas,
            eps=eps,
            is_muon=False,
        )
        groups = []
        if muon_params:
            groups.append(
                {
                    "params": muon_params,
                    "is_muon": True,
                    "lr": lr,
                    "weight_decay": weight_decay,
                    "momentum": momentum,
                    "nesterov": nesterov,
                    "ns_steps": ns_steps,
                    "betas": betas,
                    "eps": eps,
                }
            )
        if adamw_params:
            groups.append(
                {
                    "params": adamw_params,
                    "is_muon": False,
                    "lr": lr,
                    "weight_decay": weight_decay,
                    "betas": betas,
                    "eps": eps,
                }
            )
        if not groups:
            raise ValueError("MuonOptimizer: no trainable parameters (requires_grad=True).")

        super().__init__(groups, defaults)

    @staticmethod
    def _adjust_lr_for_muon(lr: float, param_shape: tuple[int, ...]) -> float:
        a, b = param_shape[:2]
        return lr * (0.2 * math.sqrt(max(a, b)))

    def _step_muon_group(self, group: dict[str, Any]) -> None:
        lr = group["lr"]
        wd = group["weight_decay"]
        momentum = group["momentum"]
        nesterov = group["nesterov"]
        ns_steps = group["ns_steps"]

        for p in group["params"]:
            g = p.grad
            if g is None:
                continue
            if g.ndim > 2:
                g = g.view(g.size(0), -1)

            state = self.state[p]
            if "momentum_buffer" not in state:
                state["momentum_buffer"] = torch.zeros_like(g)
            buf = state["momentum_buffer"]
            buf.mul_(momentum).add_(g)
            if nesterov:
                g = g.add(buf, alpha=momentum)
            else:
                g = buf
            u = zeropower_via_newtonschulz5(g, steps=ns_steps)
            adjusted_lr = self._adjust_lr_for_muon(lr, tuple(p.shape))

            p.data.mul_(1 - lr * wd)
            p.data.add_(u, alpha=-adjusted_lr)

    def _step_adamw_group(self, group: dict[str, Any]) -> None:
        lr = group["lr"]
        beta1, beta2 = group["betas"]
        eps = group["eps"]
        wd = group["weight_decay"]
        for p in group["params"]:
            g = p.grad
            if g is None:
                continue
            state = self.state[p]
            if "step" not in state:
                state["step"] = 0
                state["moment1"] = torch.zeros_like(g)
                state["moment2"] = torch.zeros_like(g)
            state["step"] += 1
            step = state["step"]
            buf1 = state["moment1"]
            buf2 = state["moment2"]
            buf1.lerp_(g, 1 - beta1)
            buf2.lerp_(g.square(), 1 - beta2)
            g = buf1 / (eps + buf2.sqrt())
            bias_correction1 = 1 - beta1**step
            bias_correction2 = 1 - beta2**step
            scale = bias_correction1 / bias_correction2**0.5
            p.data.mul_(1 - lr * wd)
            p.data.add_(g, alpha=-lr / scale)

    @torch.no_grad()
    def step(self, closure=None):
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()
        for group in self.param_groups:
            if group.get("is_muon", False):
                self._step_muon_group(group)
            else:
                self._step_adamw_group(group)
        return loss
    
def _route_to_adamw_by_name(param_name: str) -> bool:
    """Return True when a 2D parameter should use AdamW instead of Pion."""
    if not param_name:
        return False
    n = param_name.lower()
    tokens = (
        "lm_head",
        "embed_tokens",
        "word_embeddings",
        "tok_embeddings",
        "token_embedding",
        "wte",
        "embedding",
    )
    return any(t in n for t in tokens)


def _matrix_exp_truncated_integrated(
    A: torch.Tensor,
    p_data: torch.Tensor,
    side: str,
    group: Dict[str, Any],
    state: Dict[str, Any],
) -> torch.Tensor:
    """Truncated matrix exponential for Pion update (in/out side)."""
    if side == 'in':
        powers = p_data @ A
    else:
        powers = A @ p_data

    m, n = powers.shape
    fro_norm = powers.norm(p="fro")
    lr = group.get('lr', group.get('max_lr', 1e-4))
    degree = group.get('degree', 2)
    alpha = (
        lr
        * 0.2
        * math.sqrt(m * n)
        / (fro_norm + 1e-12)
    )
    powers = powers.mul(alpha)
    out = powers.clone()

    scaled_A = A * alpha
    for i in range(2, degree + 1):
        inv_i = 1.0 / i
        if side == 'in':
            powers = (powers @ scaled_A).mul_(inv_i)
        else:
            powers = (scaled_A @ powers).mul_(inv_i)
        out.add_(powers)
    return out

def tag_parameters_for_pion(
    module: torch.nn.Module,
    *,
    head_dim: Optional[int] = None,
) -> None:
    """Mark HF-style attention row-parallel projections for per-block Pion updates.

    When ``head_dim`` is set (typically ``hidden_size // num_attention_heads``), 2D weights whose
    names look like separate ``q_proj`` / ``k_proj`` / ``v_proj`` (or ``.query`` / ``.key`` /
    ``.value``) get ``_pion_per_head=True``. Each optimizer step then applies Pion along output
    slices of size ``head_dim`` (per query head for Q, per KV head for K/V in GQA).

    Fused Megatron-style QKV / gate-up matrices are not tagged here; treat them as ordinary
    matrices or fork tagging patterns locally.

    Call once after the training module is built (before or after FSDP wrap; ``named_parameters``
    must include the actor weights). With FSDP1, training must use ``use_orig_params=True`` so
    these parameters stay 2D (see :func:`pion_optimizer_requires_use_orig_params`).
    """
    for name, param in module.named_parameters():
        if param.dim() != 2:
            continue
        setattr(param, "_pion_param_name", name)

        if head_dim is None or int(head_dim) <= 0:
            continue
        if not (name.endswith(".weight") or name.endswith(".weight_orig")):
            continue
        if any(
            token in name
            for token in (
                "q_proj",
                "k_proj",
                "v_proj",
                ".query.",
                ".key.",
                ".value.",
            )
        ):
            param._pion_per_head = True  # type: ignore[attr-defined]
            param._pion_head_dim = int(head_dim)  # type: ignore[attr-defined]


def pion_optimizer_requires_use_orig_params(optimizer_name: str) -> bool:
    """Whether the given optimizer needs FSDP1 ``use_orig_params=True`` (2D matrix updates).

    True for :class:`PionOptimizer`, :class:`verl.custom_optimizer.pion_ambient.PionAmbientOptimizer`,
    and :class:`verl.custom_optimizer.pion_ambient_v2.PionAmbientV2Optimizer`.
    PyTorch FSDP1 with ``use_orig_params=False`` registers flattened ``FlatParameter`` (1D) tensors
    with the optimizer, so Pion-style matrix updates never run. Call sites must force
    ``use_orig_params=True`` when this returns True.
    """
    n = (optimizer_name or "").strip().lower()
    return n in ("pionoptimizer", "pionambientoptimizer", "pionambientv2optimizer")


def assert_fsdp_orig_params_effective_for_pion(
    fsdp_module: torch.nn.Module,
    *,
    intended_use_orig: bool,
    role: str,
    optim_config: Any,
    fsdp_strategy: str,
    rank: int,
) -> None:
    """After FSDP1 wrap: if Pion + use_orig_params was requested, verify nested FSDP and 2D trainable weights.

    PyTorch does not always define ``_use_orig_params`` on every internal FSDP object; missing attribute
    must be treated as "OK" (default True). Only an explicit ``False`` indicates a mismatch.
    """
    if fsdp_strategy != "fsdp" or not intended_use_orig or role != "actor" or optim_config is None:
        return
    if not pion_optimizer_selected_from_config(optim_config):
        return
    try:
        from torch.distributed.fsdp import FlatParameter, FullyShardedDataParallel as FSDPCls
    except ImportError:
        return
    subs = FSDPCls.fsdp_modules(fsdp_module)
    bad_flags = [m for m in subs if getattr(m, "_use_orig_params", True) is False]
    if bad_flags:
        raise RuntimeError(
            f"FSDP use_orig_params=True was requested, but {len(bad_flags)} nested FSDP module(s) have "
            f"_use_orig_params=False (first: {bad_flags[0]!r})."
        )
    flat_named = [(n, tuple(p.shape)) for n, p in fsdp_module.named_parameters() if isinstance(p, FlatParameter)]
    if flat_named:
        raise RuntimeError(
            "FSDP use_orig_params=True but FlatParameter still appears in named_parameters (e.g. "
            f"{flat_named[0]})."
        )

    trainable = [
        (n, p)
        for n, p in fsdp_module.named_parameters()
        if p.requires_grad and p.numel() > 0
    ]
    twod = [n for n, p in trainable if p.ndim == 2]
    ndim_hist: Dict[int, int] = {}
    for _, p in trainable:
        ndim_hist[p.ndim] = ndim_hist.get(p.ndim, 0) + 1
    if trainable and not twod:
        raise RuntimeError(
            "Pion + FSDP1: use_orig_params=True was passed to FSDP(), but there are no 2D trainable "
            f"parameters under named_parameters (ndim histogram: {ndim_hist}). "
            "The optimizer will only see 1D tensors, so Pion cannot run. "
            "Check: (1) PyTorch version supports use_orig_params with your sharding setup; "
            "(2) actor.fsdp_config.fsdp_size=-1 or >= world_size so device_mesh is 1D (FULL_SHARD) — "
            "2D mesh uses HYBRID_SHARD which can differ; (3) the module passed to build_optimizer is the "
            "same FSDP root returned from FSDP(...)."
        )
    if rank == 0:
        sample = [
            (n, tuple(p.shape), type(p).__name__)
            for n, p in fsdp_module.named_parameters()
            if p.requires_grad and p.numel() > 0
        ][:6]
        logger.info(
            "[FSDP/Pion] trainable=%s with_ndim==2=%s ndim_hist=%s sample=%s",
            len(trainable),
            len(twod),
            ndim_hist,
            sample,
        )


def pion_optimizer_selected_from_config(optim_config: Any) -> bool:
    """True if ``optim_config`` selects :class:`PionOptimizer` (OmegaConf / dict / dataclass)."""
    if optim_config is None:
        return False
    name: Optional[str] = None
    try:
        from omegaconf import OmegaConf

        if OmegaConf.is_config(optim_config):
            sel = OmegaConf.select(optim_config, "optimizer", default=None)
            name = str(sel) if sel is not None else None
    except Exception:
        name = None
    if name is None and isinstance(optim_config, dict):
        o = optim_config.get("optimizer")
        name = str(o) if o is not None else None
    if name is None:
        o = getattr(optim_config, "optimizer", None)
        if o is not None:
            name = str(o)
    if name is None:
        try:
            o = optim_config.get("optimizer", None)  # type: ignore[attr-defined]
            if o is not None:
                name = str(o)
        except Exception:
            pass
    if not name:
        return False
    return pion_optimizer_requires_use_orig_params(name)


def prepare_pion_module_tags(
    module: torch.nn.Module,
    *,
    optimizer_name: str,
    override_optimizer_config: Any = None,
) -> None:
    """If using PionOptimizer, resolve ``head_dim`` and run :func:`tag_parameters_for_pion`.

    ``head_dim`` comes from ``override_optimizer_config.head_dim`` when set, otherwise from
    ``hidden_size // num_attention_heads`` on the (unwrapped) HF ``config`` when available.
    """
    if not pion_optimizer_requires_use_orig_params(optimizer_name):
        return

    from omegaconf import OmegaConf

    head_dim: Optional[int] = None
    override_set = False
    if override_optimizer_config is not None:
        if OmegaConf.is_config(override_optimizer_config):
            if "head_dim" in override_optimizer_config:
                override_set = True
                head_dim = int(override_optimizer_config["head_dim"])
        elif isinstance(override_optimizer_config, dict):
            if "head_dim" in override_optimizer_config:
                override_set = True
                head_dim = int(override_optimizer_config["head_dim"])
        else:
            hd = getattr(override_optimizer_config, "head_dim", None)
            if hd is not None:
                override_set = True
                head_dim = int(hd)

    if override_set and (head_dim is None or head_dim <= 0):
        return

    if not override_set:
        head_dim = None
    if head_dim is None or head_dim <= 0:
        base = module
        while hasattr(base, "_fsdp_wrapped_module"):
            base = base._fsdp_wrapped_module
        cfg = getattr(base, "config", None)
        if cfg is not None:
            hidden = getattr(cfg, "hidden_size", None) or getattr(cfg, "n_embd", None)
            n_heads = getattr(cfg, "num_attention_heads", None) or getattr(cfg, "n_head", None)
            if hidden is not None and n_heads:
                head_dim = int(hidden // n_heads)

    if head_dim is not None and head_dim > 0:
        tag_parameters_for_pion(module, head_dim=head_dim)


class _PionMatrixCore:
    """Pion updates for 2D parameters, with optional per-head row blocks (for separate q/k/v)."""

    def __init__(
        self,
        *,
        head_dim: int = 0,
        per_head_fn: Optional[Callable[[torch.Tensor], bool]] = None,
    ):
        self.head_dim = int(head_dim)
        self.per_head_fn = per_head_fn if per_head_fn is not None else (lambda _: False)

    def _pion_update_output_row_blocks(
        self,
        p: torch.Tensor,
        grad_f: torch.Tensor,
        p_data: torch.Tensor,
        group: Dict[str, Any],
        state: Dict[str, Any],
        beta1: float,
        head_dim: int,
    ) -> None:
        """Apply Pion independently to each output slice of shape (head_dim, in_dim)."""
        out_dim, in_dim = p_data.shape
        num_blocks = out_dim // head_dim

        if "step" not in state:
            state["step"] = 0
            state["exp_avg_in_blocks"] = [
                torch.zeros((in_dim, in_dim), device=p.device, dtype=torch.float32)
                for _ in range(num_blocks)
            ]
            state["exp_avg_out_blocks"] = [
                torch.zeros((head_dim, head_dim), device=p.device, dtype=torch.float32)
                for _ in range(num_blocks)
            ]
        elif len(state["exp_avg_in_blocks"]) != num_blocks:
            state["step"] = 0
            state["exp_avg_in_blocks"] = [
                torch.zeros((in_dim, in_dim), device=p.device, dtype=torch.float32)
                for _ in range(num_blocks)
            ]
            state["exp_avg_out_blocks"] = [
                torch.zeros((head_dim, head_dim), device=p.device, dtype=torch.float32)
                for _ in range(num_blocks)
            ]

        state["step"] += 1
        update_side = "in" if (state["step"] % 2 == 1) else "out"

        view = p_data.view(num_blocks, head_dim, in_dim)
        grad_view = grad_f.view(num_blocks, head_dim, in_dim)
        w_list = [view[b].clone() for b in range(num_blocks)]
        g_list = [grad_view[b] for b in range(num_blocks)]

        for b in range(num_blocks):
            grad_in_b = w_list[b].t() @ g_list[b]
            grad_in_b = grad_in_b - grad_in_b.t()
            state["exp_avg_in_blocks"][b].mul_(beta1).add_(grad_in_b, alpha=1 - beta1)
            grad_out_b = g_list[b] @ w_list[b].t()
            grad_out_b = grad_out_b - grad_out_b.t()
            state["exp_avg_out_blocks"][b].mul_(beta1).add_(grad_out_b, alpha=1 - beta1)

        if update_side == "in":
            for b in range(num_blocks):
                A = (-state["exp_avg_in_blocks"][b]).to(p_data.dtype)
                w_list[b].add_(_matrix_exp_truncated_integrated(A, w_list[b], "in", group, state))
        else:
            for b in range(num_blocks):
                A = (-state["exp_avg_out_blocks"][b]).to(p_data.dtype)
                w_list[b].add_(_matrix_exp_truncated_integrated(A, w_list[b], "out", group, state))

        new_p = torch.cat(w_list, dim=0)
        if new_p.dtype != p.data.dtype:
            new_p = new_p.to(p.data.dtype)
        p.data.copy_(new_p)

    def pion_update_for_matrix(
        self,
        p: torch.Tensor,
        grad: torch.Tensor,
        group: Dict[str, Any],
        state: Dict[str, Any],
    ) -> None:
        betas = group.get("betas", (0.9, 0.999))
        beta1 = betas[0] if isinstance(betas, (tuple, list)) else betas

        p_data = p.data.float() if p.dtype != torch.float32 else p.data
        grad_f = grad.float() if grad.dtype != torch.float32 else grad
        out_dim, in_dim = p_data.shape

        hd = self.head_dim
        use_row_blocks = hd > 0 and self.per_head_fn(p) and (out_dim % hd == 0)
        if use_row_blocks:
            logger.debug(
                "Pion per-head row blocks shape=%s head_dim=%s num_blocks=%s",
                tuple(p_data.shape),
                hd,
                out_dim // hd,
            )
            self._pion_update_output_row_blocks(p, grad_f, p_data, group, state, beta1, hd)
            return

        if "step" not in state:
            state["step"] = 0
            state["exp_avg_in"] = torch.zeros((in_dim, in_dim), device=p.device, dtype=torch.float32)
            state["exp_avg_out"] = torch.zeros((out_dim, out_dim), device=p.device, dtype=torch.float32)

        state["step"] += 1
        update_side = "in" if (state["step"] % 2 == 1) else "out"

        grad_in = p_data.t() @ grad_f
        grad_in = grad_in - grad_in.t()
        grad_out = grad_f @ p_data.t()
        grad_out = grad_out - grad_out.t()

        state["exp_avg_in"].mul_(beta1).add_(grad_in, alpha=1 - beta1)
        state["exp_avg_out"].mul_(beta1).add_(grad_out, alpha=1 - beta1)

        if update_side == "in":
            A = (-state["exp_avg_in"]).to(p_data.dtype)
        else:
            A = (-state["exp_avg_out"]).to(p_data.dtype)

        delta_p = _matrix_exp_truncated_integrated(A, p_data, update_side, group, state)
        if delta_p.dtype != p.data.dtype:
            delta_p = delta_p.to(p.data.dtype)
        p.data.add_(delta_p)


class PionOptimizer(Optimizer):
    """verl / PyTorch FSDP entrypoint: Pion for rank-2 weights, AdamW for other parameters.

    Compatible with ``verl.workers.config.optimizer.build_optimizer``::

        optimizer_impl: verl.custom_optimizer.pion
        optimizer: PionOptimizer

    Optional overrides (``override_optimizer_config``)::

        degree: 2
        head_dim: null  # e.g. hidden_size // num_attention_heads; enables per-head Pion on tagged q/k/v

    For Hugging Face models with separate ``q_proj`` / ``k_proj`` / ``v_proj``, call
    ``tag_parameters_for_pion(module, head_dim=...)`` once on the actor (FSDP engine does this when
    ``head_dim`` is set or can be inferred from ``module.config``). Other 2D weights use full-matrix
    Pion; ``gate_proj`` / ``up_proj`` need no special casing.

    Embedding / LM head: mark ``param._pion_skip = True`` to use AdamW instead of Pion.

    Deprecated (ignored with a warning): ``split_qkv``, ``qkv_split_shapes``, ``split_fc1_up_gate``,
    ``split_qkv_per_head``.
    """

    def __init__(
        self,
        params,
        lr: float = 1e-4,
        betas: Tuple[float, float] = (0.9, 0.999),
        weight_decay: float = 0.01,
        eps: float = 1e-8,
        *,
        degree: int = 2,
        head_dim: Optional[int] = None,
        **kwargs: Any,
    ):
        import warnings

        _legacy = (
            "split_qkv",
            "qkv_split_shapes",
            "split_fc1_up_gate",
            "split_qkv_per_head",
        )
        if any(k in kwargs for k in _legacy):
            warnings.warn(
                "PionOptimizer: split_qkv / qkv_split_shapes / split_fc1_up_gate / split_qkv_per_head "
                "are deprecated and ignored. Use head_dim plus tag_parameters_for_pion(module, head_dim=...).",
                DeprecationWarning,
                stacklevel=2,
            )
        for k in _legacy:
            kwargs.pop(k, None)

        kwargs.pop("max_lr", None)
        for _silent in (
            "fused",
            "foreach",
            "capturable",
            "maximize",
            "differentiable",
            "bf16_stochastic_round",
            "master_weights",
            "store_param_remainders",
            "exp_avg_dtype",
            "exp_avg_sq_dtype",
            "master_weight_dtype",
        ):
            kwargs.pop(_silent, None)
        if kwargs:
            warnings.warn(
                f"PionOptimizer: ignoring unknown config keys {sorted(kwargs.keys())}",
                UserWarning,
                stacklevel=2,
            )

        hd = int(head_dim) if head_dim is not None and int(head_dim) > 0 else 0

        param_list = list(params)
        matrix_params: List[torch.nn.Parameter] = []
        vector_params: List[torch.nn.Parameter] = []
        for p in param_list:
            if not p.requires_grad:
                continue
            param_name = str(getattr(p, "_pion_param_name", "") or "")
            force_adamw = _route_to_adamw_by_name(param_name)
            if p.ndim == 2 and not getattr(p, "_pion_skip", False) and not force_adamw:
                print(
                    f"Pion matrix param {param_name} shape={tuple(p.shape)}",
                )
                matrix_params.append(p)
            else:
                print(
                    f"Pion AdamW param {param_name} shape={tuple(p.shape)} ndim={p.ndim} type={type(p).__name__}",
                )
                vector_params.append(p)

        is_per_head_fn = lambda p: bool(getattr(p, "_pion_per_head", False))

        if hd == 0:
            for p in param_list:
                if not p.requires_grad or p.ndim != 2:
                    continue
                hdp = getattr(p, "_pion_head_dim", None)
                if hdp is not None and int(hdp) > 0:
                    hd = int(hdp)
                    break

        defaults = dict(
            lr=lr,
            betas=betas,
            eps=eps,
            weight_decay=weight_decay,
            amsgrad=False,
            is_pion=False,
            degree=degree,
        )
        groups = []
        if matrix_params:
            groups.append(
                {
                    "params": matrix_params,
                    "is_pion": True,
                    "lr": lr,
                    "betas": betas,
                    "weight_decay": 0.0,
                    "degree": degree,
                }
            )
        if vector_params:
            groups.append(
                {
                    "params": vector_params,
                    "is_pion": False,
                    "lr": lr,
                    "betas": betas,
                    "eps": eps,
                    "weight_decay": weight_decay,
                    "amsgrad": False,
                    "degree": degree,
                }
            )

        if not groups:
            raise ValueError(
                "PionOptimizer: no trainable parameters (requires_grad=True). "
                "Check parameter list or masks."
            )

        trainable_nonempty = [p for p in param_list if p.requires_grad and p.numel() > 0]
        if trainable_nonempty and not matrix_params:
            raise RuntimeError(
                "PionOptimizer: optimizer 参数列表里没有任何 2D 权重。请确认："
                "(1) 使用的是 FSDP1（actor.strategy=fsdp），且传给 torch.distributed.fsdp.FSDP 的 "
                "use_orig_params 为 True；"
                "(2) fsdp_config.use_orig_params 在 OmegaConf 合并后为布尔 true（不要用未加引号的奇怪字符串）；"
                "(3) 若仍失败，看训练日志里 [FSDP actor] 一行与嵌套 FSDP 的 _use_orig_params 校验报错。"
            )

        super().__init__(groups, defaults)

        self._pion_core: Optional[_PionMatrixCore] = None
        if matrix_params:
            self._pion_core = _PionMatrixCore(
                head_dim=hd,
                per_head_fn=is_per_head_fn,
            )

    def _adamw_step_group(self, group: Dict[str, Any]) -> None:
        for p in group["params"]:
            if p.grad is None:
                continue
            grad = p.grad
            if grad.is_sparse:
                raise RuntimeError("PionOptimizer does not support sparse gradients")
            state = self.state[p]
            if len(state) == 0:
                state["step"] = 0
                state["exp_avg"] = torch.zeros_like(p, memory_format=torch.preserve_format)
                state["exp_avg_sq"] = torch.zeros_like(p, memory_format=torch.preserve_format)

            exp_avg, exp_avg_sq = state["exp_avg"], state["exp_avg_sq"]
            beta1, beta2 = group["betas"]
            eps = group["eps"]

            st = state["step"]
            prev = int(st.item()) if isinstance(st, torch.Tensor) else int(st)
            state["step"] = prev + 1
            step = state["step"]

            if group["weight_decay"] != 0:
                p.data.mul_(1 - group["lr"] * group["weight_decay"])

            exp_avg.mul_(beta1).add_(grad, alpha=1 - beta1)
            exp_avg_sq.mul_(beta2).addcmul_(grad, grad, value=1 - beta2)
            bias_correction1 = 1 - beta1**step
            bias_correction2 = 1 - beta2**step
            step_size = group["lr"] / bias_correction1

            denom = (exp_avg_sq.sqrt() / math.sqrt(bias_correction2)).add_(eps)
            p.data.addcdiv_(exp_avg, denom, value=-step_size)

    @torch.no_grad()
    def step(self, closure=None):
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            if group.get("is_pion", False):
                assert self._pion_core is not None
                for p in group["params"]:
                    if p.grad is None:
                        continue
                    self._pion_core.pion_update_for_matrix(
                        p, p.grad.data, group, self.state[p]
                    )
            else:
                self._adamw_step_group(group)

        return loss