#!/usr/bin/env bash
# QuantMind 在线部署下载器。完整数据迁移请使用 full-deploy.sh。

set -Eeuo pipefail

[[ $EUID -eq 0 ]] || { echo '请使用 sudo 执行'; exit 1; }

REF="${QUANTMIND_DEPLOY_REF:-master}"
URL="https://gitee.com/qusong0627/QuantMind/raw/${REF}/deploy/deploy.sh"
TEMP_DIR="$(mktemp -d /tmp/quantmind-deploy.XXXXXX)"
trap 'rm -rf "$TEMP_DIR"' EXIT

echo "下载 QuantMind 部署脚本（${REF}）..."
curl --fail --location --retry 3 "$URL" -o "$TEMP_DIR/deploy.sh"

if [[ -n ${QUANTMIND_DEPLOY_SHA256:-} ]]; then
    echo "${QUANTMIND_DEPLOY_SHA256}  $TEMP_DIR/deploy.sh" | sha256sum --check --status \
        || { echo '部署脚本校验失败'; exit 1; }
fi

bash "$TEMP_DIR/deploy.sh" "$@"
