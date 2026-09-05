#!/usr/bin/env bash
# AutoDL 容器实例 Pro API 封装
# Token: AUTODL_API_TOKEN 环境变量 > 仓库 .env 的 AUTODL_API_TOKEN
set -uo pipefail
API="https://api.autodl.com"
TOKEN="${AUTODL_API_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f .env ]; then
    TOKEN="$(grep -E '^AUTODL_API_TOKEN=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' || true)"
fi
if [ -z "$TOKEN" ]; then
    echo "[!] 缺少 token: 设 AUTODL_API_TOKEN 或在 .env 写 AUTODL_API_TOKEN=..." >&2
    exit 2
fi
AUTH="Authorization: Bearer $TOKEN"
CT="Content-Type: application/json"
pretty() { jq . 2>/dev/null || python3 -m json.tool 2>/dev/null || cat; }

req() { # req METHOD PATH BODY
    local method="$1" path="$2" body="${3:-}"
    if [ -n "$body" ]; then
        curl -sS -X "$method" "$API$path" -H "$AUTH" -H "$CT" -d "$body"
    else
        curl -sS -X "$method" "$API$path" -H "$AUTH"
    fi
}

cmd="${1:-list}"
case "$cmd" in
    list)     req POST /api/v1/dev/instance/pro/list "{\"page_index\":1,\"page_size\":${2:-20}}" | pretty ;;
    snapshot) [ -n "${2:-}" ] || { echo "用法: $0 snapshot <uuid>"; exit 1; }
              req POST /api/v1/dev/instance/pro/snapshot "{\"instance_uuid\":\"$2\"}" | pretty ;;
    status)   [ -n "${2:-}" ] || { echo "用法: $0 status <uuid>"; exit 1; }
              req POST /api/v1/dev/instance/pro/status "{\"instance_uuid\":\"$2\"}" | pretty ;;
    on)       [ -n "${2:-}" ] || { echo "用法: $0 on <uuid>"; exit 1; }
              req POST /api/v1/dev/instance/pro/power_on "{\"instance_uuid\":\"$2\",\"payload\":\"gpu\"}" | pretty ;;
    off)      [ -n "${2:-}" ] || { echo "用法: $0 off <uuid>"; exit 1; }
              req POST /api/v1/dev/instance/pro/power_off "{\"instance_uuid\":\"$2\"}" | pretty ;;
    release)  [ -n "${2:-}" ] || { echo "用法: $0 release <uuid>(先关机)"; exit 1; }
              req POST /api/v1/dev/instance/pro/release "{\"instance_uuid\":\"$2\"}" | pretty ;;
    images)   req POST /api/v1/dev/instance/pro/image/private/list "{\"page_index\":1,\"page_size\":${2:-20}}" | pretty ;;
    save)     [ -n "${2:-}" ] && [ -n "${3:-}" ] || { echo "用法: $0 save <uuid> \"镜像名\""; exit 1; }
              req POST /api/v1/dev/instance/pro/image/save "{\"instance_uuid\":\"$2\",\"image_name\":\"$3\"}" | pretty ;;
    create)
        spec="${2:-v-48g}"; dc="${3:-}"; cuda="${4:-128}"; img="${5:-base-image-mbr2n4urrc}"
        body="{\"req_gpu_amount\":1,\"expand_system_disk_by_gb\":0,\"gpu_spec_uuid\":\"$spec\",\"image_uuid\":\"$img\",\"cuda_v_from\":$cuda,\"instance_name\":\"qm-train-node\"}"
        [ -n "$dc" ] && body="{\"data_center_list\":[\"$dc\"],${body#\{}"
        req POST /api/v1/dev/instance/pro/create "$body" | pretty ;;
    *) echo "用法: $0 {list|snapshot|status|on|off|release|images|save|create}"; exit 1 ;;
esac
