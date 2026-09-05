# QuantMind AutoDL 远程训练镜像（独立构建）
#
# 用途：作为 AutoDL GPU 训练节点的运行镜像。主节点通过 SSH 在此镜像上
#       docker run 启动训练容器（python /app/train.py --config config.yaml）。
#
# 与 quantmind-oss 解耦：不依赖本地 24GB 基础镜像，基于 python:3.10-slim，
# 只装训练必需依赖（torch + lightgbm/xgboost/catboost 等）。因此可在
# AutoDL 节点上从 git 直接构建，利用远端更快网络，无需本地推送大镜像。
#
# 构建（远端）：
#   docker build --build-arg TORCH_DEVICE=gpu -f docker/autodl/Dockerfile \
#     -t quantmind-train:latest .
# 或经 scripts/setup/build-autodl-remote.sh 一键远程构建。

FROM docker.1ms.run/library/python:3.10-slim-bookworm

# torch 版本：gpu=完整 CUDA（AutoDL 节点推荐，需 nvidia-container-toolkit）
#            cpu=CPU 版（更快、更小）
ARG TORCH_DEVICE=gpu
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}

# 系统依赖（libgomp 供 lightgbm/xgboost；openssh 供主节点 ssh）
RUN sed -i "s/deb.debian.org/mirrors.aliyun.com/g; s/security.debian.org/mirrors.aliyun.com/g" /etc/apt/sources.list.d/debian.sources || \
    sed -i "s/deb.debian.org/mirrors.aliyun.com/g; s/security.debian.org/mirrors.aliyun.com/g" /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        git \
        openssh-client \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 训练依赖（torch 按 TORCH_DEVICE 选 CPU/GPU）
RUN python -m pip install --upgrade pip setuptools wheel \
    && if [ "$TORCH_DEVICE" = "gpu" ]; then \
         echo "安装 GPU 版 torch（完整 CUDA）"; \
         python -m pip install --no-cache-dir --root-user-action=ignore "torch==2.9.1"; \
       else \
         echo "安装 CPU 版 torch"; \
         python -m pip install --no-cache-dir --root-user-action=ignore "torch==2.9.1+cpu" \
           --index-url https://download.pytorch.org/whl/cpu \
           --extra-index-url ${PIP_INDEX_URL}; \
       fi \
    && python -m pip install --no-cache-dir --root-user-action=ignore \
        lightgbm xgboost catboost scikit-learn scipy pandas numpy pyarrow requests pyyaml

# 业务代码：训练脚本 + 特征生成脚本
# （train.py 会由编排器 rsync 覆盖挂载，这里提供镜像内置兜底）
COPY docker/training/train.py /app/train.py
COPY backend/scripts/generate_feature_snapshots.py /app/scripts/generate_feature_snapshots.py
COPY backend/scripts/update_feature_parquet.py /app/scripts/update_feature_parquet.py

# 训练工作目录（主节点 rsync 特征快照/config 到此，产物也在此）
RUN mkdir -p /workspace

# 无 ENTRYPOINT/CMD —— 由主节点指定训练命令
