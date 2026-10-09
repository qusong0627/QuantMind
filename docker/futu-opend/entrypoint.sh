#!/bin/bash
# 原样启动官方 FutuOpenD（程序不改动，仅通过官方配置文件 FutuOpenD.xml 调整）
# 1) API 监听 0.0.0.0（局域网可达）  2) 启用 RSA 加密（跨网交易接口的官方安全要求）
# RSA 私钥持久化在 /opt/futu/data/rsa.key，quantmind 容器经共享卷读取同一把私钥
# 首次登录：docker attach futu-opend 输入账号/密码/验证码；Ctrl+P Ctrl+Q 分离
set -e
OPD_DIR=$(find /opt/futu -name FutuOpenD -type f | head -1 | xargs dirname)
cd "$OPD_DIR"

RSA_KEY=/opt/futu/data/rsa.key
if [ ! -f "$RSA_KEY" ]; then
    openssl genrsa -out "$RSA_KEY" 1024 2>/dev/null
    chmod 600 "$RSA_KEY"
fi

if [ -f FutuOpenD.xml ]; then
    sed -i 's|<ip>127.0.0.1</ip>|<ip>0.0.0.0</ip>|' FutuOpenD.xml
    # RSA 加密（官方配置项：跨网交易必须）
    if grep -q '<!-- <rsa_private_key>' FutuOpenD.xml; then
        sed -i "s|<!-- <rsa_private_key>.*</rsa_private_key> -->|<rsa_private_key>$RSA_KEY</rsa_private_key>|" FutuOpenD.xml
    elif ! grep -q '<rsa_private_key>' FutuOpenD.xml; then
        sed -i "s|</futu_opend>|    <rsa_private_key>$RSA_KEY</rsa_private_key>\n</futu_opend>|" FutuOpenD.xml
    fi
fi

mkdir -p /opt/futu/data
export LD_LIBRARY_PATH="$OPD_DIR:$LD_LIBRARY_PATH"
echo "[entrypoint] starting official FutuOpenD: $OPD_DIR/FutuOpenD (api 0.0.0.0:11111, rsa on)"
exec ./FutuOpenD -login_by_remember -data_dir /opt/futu/data "$@"
