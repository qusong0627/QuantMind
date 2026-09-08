#!/usr/bin/env bash
# 构建 minibt AI-IDE 运行时镜像(与引擎共用同一 docker daemon,构建后 executor 可直接引用)
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${MINIBT_RUNNER_TAG:-quantmind-minibt-runner:latest}"
PIP_INDEX_URL_ARG=""
if [[ -n "${PIP_INDEX_URL:-}" ]]; then
  PIP_INDEX_URL_ARG="--build-arg PIP_INDEX_URL=${PIP_INDEX_URL}"
fi

docker build ${PIP_INDEX_URL_ARG} -f docker/Dockerfile.minibt-runner -t "${TAG}" .
docker run --rm "${TAG}"
echo "OK: ${TAG}"
