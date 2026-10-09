# 富途 OpenD（官方行情/交易网关）——QuantMind 港股通道的本机容器。
#
# 本文件是**事后归档**：2026-08-28 首次构建出 `quantmind-futu-opend:latest` 时
# 只留了镜像，Dockerfile 没落盘；2026-10-08 按 `docker history --no-trunc` 逐层
# 还原（基础镜像是 ubuntu:18.04，见镜像 label org.opencontainers.image.ref.*）。
# 重建：docker build -t quantmind-futu-opend:latest docker/futu-opend
#
# OpenD 是官方二进制，本镜像**不改程序**，只改官方配置 FutuOpenD.xml（见 entrypoint.sh）。
FROM ubuntu:18.04

ENV DEBIAN_FRONTEND=noninteractive

# OpenD 带 GUI 栈，headless 也要这些 .so（缺 libnss3 等起不来）
RUN apt-get update && apt-get install -y --no-install-recommends \
      libglib2.0-0 libnss3 libfontconfig1 libfreetype6 libx11-6 libxext6 \
      libxrender1 zlib1g libcurl4 openssl ca-certificates telnet \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/futu

# 官方发行包（Ubuntu 18.04 版）解压到 /opt/futu/Futu_OpenD_<版本>/
COPY FutuOpenD_ubuntu.tar.gz /tmp/
RUN tar -xzf /tmp/FutuOpenD_ubuntu.tar.gz -C /opt/futu --strip-components=1 \
    && chmod +x /opt/futu/*/FutuOpenD 2>/dev/null; \
    find /opt/futu -name FutuOpenD -type f -exec chmod +x {} \; \
    && rm /tmp/FutuOpenD_ubuntu.tar.gz

ENV FUTU_DATA=/opt/futu/data
RUN mkdir -p /opt/futu/data

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 11111 11112
ENTRYPOINT ["/entrypoint.sh"]
