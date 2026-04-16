#!/bin/bash
# QuantMind 快速部署脚本
# 在服务器上运行此脚本即可完成部署

set -e

echo "========================================"
echo "  QuantMind 快速部署"
echo "========================================"

# 检查 root 权限
if [[ $EUID -ne 0 ]]; then
    echo "错误: 需要 root 权限"
    echo "请使用: sudo bash $0"
    exit 1
fi

# 下载部署脚本
DEPLOY_DIR="/opt/quantmind"
mkdir -p $DEPLOY_DIR

echo "下载部署脚本..."
curl -fsSL https://gitee.com/qusong0627/quantmind/raw/master/deploy/deploy.sh -o $DEPLOY_DIR/deploy.sh

# 添加执行权限
chmod +x $DEPLOY_DIR/deploy.sh

# 执行部署
echo "开始部署..."
bash $DEPLOY_DIR/deploy.sh
