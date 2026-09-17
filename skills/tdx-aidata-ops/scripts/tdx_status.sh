#!/usr/bin/env bash
# TdxAiData 通道一体体检（宿主执行；内部 docker exec 进 quantmind 容器）
# 用法: bash tdx_status.sh [容器名,默认 quantmind]
set -uo pipefail
C="${1:-quantmind}"

echo "== TdxAiData 通道体检 $(date '+%F %T %Z') =="
docker exec -i -w /app "$C" python - <<'PY'
import asyncio, time
from backend.shared.tdx_aidata.client import default_cluster
from backend.shared.hot_set_store import make_hot_set_client, hot_set_key
from backend.shared.remote_quote_config import make_sync_client

async def main():
    try:
        sub = await default_cluster().subscription_status(timeout=10)
    except Exception as exc:
        print("[分片] 状态读取失败:", type(exc).__name__, str(exc)[:120]); return
    d = m = r = 0
    for s in sub.get('shards') or []:
        c = s.get('counters') or {}
        d += c.get('frames_data') or 0; m += c.get('frames_meta') or 0; r += c.get('resubscribes') or 0
        print("  s%s sub=%-4s data=%-7s meta=%-8s rec=%-7s writ=%-7s age=%-6s err=%s" % (
            (s.get('shard') or {}).get('id'), s.get('subscribed'), c.get('frames_data', 0),
            c.get('frames_meta', 0), c.get('records', 0), c.get('written', 0),
            s.get('last_frame_age_s'), (c.get('last_error') or '')[:50]))
    print("[合计] data=%s meta=%s resubscribes=%s 静默片=%s" % (d, m, r, sub.get('silent_shards') or '无'))
    if d == 0:
        print("[判读] 只有心跳、零数据帧 → 服务端不推（找 SDK 方核对；勿反复重订）")
    hc = make_hot_set_client()
    print("[热集] 本地 %s = %s 只" % (hot_set_key(), hc.scard(hot_set_key())))
    rc = make_sync_client()
    low = up = 0; fresh = 0
    for k in rc.scan_iter('market:snapshot:*', count=3000):
        fam = k.rsplit(':', 1)[-1]
        if fam[:1].islower(): low += 1
        else: up += 1
    now = time.time()
    for k in list(rc.scan_iter('market:snapshot:*', count=3000))[:200]:
        h = rc.hgetall(k)
        try:
            if h.get('source') == 'tdx_aidata_sub' and now - float(h.get('timestamp') or 0) <= 60:
                fresh += 1
        except Exception:
            pass
    print("[远端键] 小写(标准) 样本 ≤60s 鲜=%s / 小写键=%s / 大写键=%s（大写多为第三方推送，勿混用）" % (fresh, low, up))

asyncio.run(main())
PY
