import { useMemo } from 'react';
import {
  MarketId,
  LiveAccount,
  RealAccountSummary,
  api,
  fetchFutuAccountBoth,
  fetchQmtAccount,
  fetchQmtStatus,
  fetchRealAccounts,
} from '../api/client';
import { usePolling } from '../hooks/usePolling';
import {
  ChannelProbe,
  TdxConfigLite,
  channelsOfMarket,
} from '../utils/channelStatus';
import './ChannelStatus.css';

/** 交易通道在线状态块（详情 / 实盘 tab）。
 *
 *  用户口径「一看就知道在线、还是没在线」：每个通道一行状态点 + 一句话结论，
 *  再把桥 / 客户端 / 下单权限 / 账户快照新鲜度逐条摊开——通道活着但账户停更
 *  这类半死状态不能显示成全绿（2026-09-10 通达信桥实例）。
 *
 *  数据源按市场：cn=桥配置 + 两账户快照 + QMT 探针；hk=富途双环境；us=IBKR。
 *  hk/us 的账户数据由调用方传入（Live 已按 15s 轮询，避免重复握手）。 */
export default function ChannelStatus({
  market,
  futuBoth = null,
  ibkr = null,
  summaries: summariesProp,
}: {
  market: MarketId;
  futuBoth?: { real: LiveAccount | null; simulate: LiveAccount | null } | null;
  ibkr?: { total_asset: number } | null;
  /** 调用方已拉过 /live/real-accounts 时传入，避免同一端点双份轮询 */
  summaries?: RealAccountSummary[] | null;
}) {
  const isCn = market === 'cn';
  // 桥配置（含桥健康检查）；裸 JSON，无 {success,data} 信封
  const tdxCfg = usePolling<TdxConfigLite | null>(
    () =>
      isCn
        ? api.get('/tdx/config').then((r) => r.data as TdxConfigLite).catch(() => null)
        : Promise.resolve(null),
    [isCn],
    30000,
  );
  // 两账户最新快照摘要（通达信 / QMT）；外部已给则不重复拉
  const summariesSelf = usePolling(
    () =>
      isCn && !summariesProp
        ? fetchRealAccounts().catch(() => null)
        : Promise.resolve(null),
    [isCn, summariesProp],
    30000,
  );
  const summaries = summariesProp ?? summariesSelf.data;
  // QMT 桥探针：账户通道读得通才算在线（Redis RPC，失败即离线）
  const qmtProbe = usePolling(
    () => (isCn ? fetchQmtAccount().then((a) => ({ ok: !!a, error: undefined })).catch((e: unknown) => ({ ok: false, error: e instanceof Error ? e.message : String(e) })) : Promise.resolve(null)),
    [isCn],
    30000,
  );
  // QMT 桥自述：下单总闸在不在（与账户探针分开——账户读得通不等于那边不放开下单）
  const qmtStatus = usePolling(
    () => (isCn ? fetchQmtStatus().catch(() => null) : Promise.resolve(null)),
    [isCn],
    60000,
  );
  // 港股富途：外部未传时自己拉（详情 tab 独立进入时 futuBoth 可能为空）
  const futuSelf = usePolling(
    () =>
      market === 'hk' && !futuBoth
        ? fetchFutuAccountBoth().catch(() => null)
        : Promise.resolve(null),
    [market, futuBoth],
    30000,
  );

  const channels: ChannelProbe[] = useMemo(
    () =>
      channelsOfMarket(market, {
        tdx: tdxCfg.data ?? null,
        qmtProbe: qmtProbe.data ?? null,
        qmtStatus: qmtStatus.data ?? null,
        summaries: summaries ?? null,
        futuBoth: futuBoth ?? futuSelf.data,
        ibkr: ibkr ? { total_asset: ibkr.total_asset } : null,
      }),
    [market, tdxCfg.data, qmtProbe.data, qmtStatus.data, summaries, futuBoth, futuSelf.data, ibkr],
  );

  return (
    <div className="chan-wrap">
      {channels.map((c) => (
        <div className={`chan-card chan-${c.state}`} key={c.key}>
          <div className="chan-head">
            <span className={`chan-dot chan-dot-${c.state}`} aria-hidden />
            <span className="chan-label">{c.label}</span>
            <span className={`chan-state chan-state-${c.state}`}>{c.stateText}</span>
          </div>
          <div className="chan-role">{c.role}</div>
          <div className="chan-lines">
            {c.lines.map((l) => (
              <div className="chan-line" key={l.k}>
                <span className="chan-k">{l.k}</span>
                <span className={`chan-v ${l.tone ? `chan-v-${l.tone}` : ''}`}>{l.v}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
      <div className="chan-foot">
        快照年龄按后端落库时刻算：≤3 分钟 = 实时，≤1 小时 = 滞后，更久 = 停更
      </div>
    </div>
  );
}
