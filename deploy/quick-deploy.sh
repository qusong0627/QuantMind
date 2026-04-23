#!/bin/bash
# QuantMind 快速部署脚本
# 在服务器上运行此脚本即可完成部署

set -euo pipefail

AUTO_YES=false
for arg in "$@"; do
    case "$arg" in
        --yes) AUTO_YES=true ;;
        *)
            echo "错误: 未知参数 $arg"
            echo "支持参数: --yes"
            exit 1
            ;;
    esac
done

echo "========================================"
echo "  QuantMind 快速部署"
echo "========================================"

# 检查 root 权限
if [[ $EUID -ne 0 ]]; then
    echo "错误: 需要 root 权限"
    echo "请使用: sudo bash $0"
    exit 1
fi

# 交互确认
if ! $AUTO_YES; then
    if [[ ! -t 0 ]]; then
        echo "错误: 当前为非交互式终端，无法确认安装"
        echo "如需继续，请使用: sudo bash quick-deploy.sh --yes"
        exit 1
    fi
    read -r -p "是否继续安装 QuantMind？ [y/N]: " response
    case "$response" in
        [yY][eE][sS]|[yY]) ;;
        *)
            echo "已取消安装"
            exit 0
            ;;
    esac
fi

# 下载部署脚本（使用临时目录，避免提前占用 /opt/quantmind）
TMP_DEPLOY_DIR="$(mktemp -d /tmp/quantmind-deploy.XXXXXX)"
DEPLOY_SCRIPT="$TMP_DEPLOY_DIR/deploy.sh"
DEPLOY_URL="https://gitee.com/qusong0627/quantmind/raw/master/deploy/deploy.sh"
EXPECTED_SHA256="${QUANTMIND_DEPLOY_SHA256:-}"
cleanup() {
    rm -rf "$TMP_DEPLOY_DIR"
}
trap cleanup EXIT

echo "下载部署脚本..."
curl -fsSL "$DEPLOY_URL" -o "$DEPLOY_SCRIPT"

if [[ -n "$EXPECTED_SHA256" ]]; then
    ACTUAL_SHA256="$(sha256sum "$DEPLOY_SCRIPT" | awk '{print $1}')"
    if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
        echo "错误: deploy.sh 校验失败"
        echo "期望: $EXPECTED_SHA256"
        echo "实际: $ACTUAL_SHA256"
        exit 1
    fi
    echo "deploy.sh 校验通过"
else
    echo "警告: 未设置 QUANTMIND_DEPLOY_SHA256，已跳过 deploy.sh 完整性校验"
fi

# 添加执行权限
chmod +x "$DEPLOY_SCRIPT"

# 执行部署
echo "开始部署..."
bash "$DEPLOY_SCRIPT" --yes
