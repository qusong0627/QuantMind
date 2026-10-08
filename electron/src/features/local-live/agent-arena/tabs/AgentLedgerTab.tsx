import { useMemo, useState } from 'react';
import ArenaSurface from '../ArenaSurface';
import {
  fetchLiveEquity,
  fetchLogs,
  fetchOverview,
  fetchPerformance,
  fetchTokenUsage,
  type LogLine,
  type MarketId,
  type OverviewRow,
} from '../arena/api/client';
import { usePolling } from '../arena/hooks/usePolling';
import ChatStream from '../arena/components/ChatStream';
import ModelCard from '../arena/components/ModelCard';
import { MarketSwitcher } from '../arena/components/Navbar';
import { displayAgentName } from '../arena/utils/agents';
import { useArenaNav } from '../arena/arenaNav';

/**
 * 「智能体交易」栏 —— 各 agent 台账 + 决策日志（模型对话）。
 *
 * 这一栏不是新造的面板，而是把**实况页右边那两块**（模型卡 = 各 agent 权益、
 * 对话流 = 决策日志）抽出来单独成栏，方便盯着看：实况页给的是「账 + 盘」的全景，
 * 这一栏只回答「每个 agent 现在有多少钱、它上一次决策说了什么」。
 *
 * 数据口径与实况页逐字一致（同一批端点、同一轮询节奏），所以两栏的数字不会打架：
 *   overview → 当前市场的 agent 名单；performance → 模拟盘净值/收益；
 *   live/equity → A股实盘虚拟分账净值（有就用实盘，没有才回落到模拟盘 summary）；
 *   token-usage → 各自累计 token；logs → 对话流（每行一个分析回合）。
 */

/** 日志轮询：与实况页同一个口径（对话是 2 分钟一刷，日志体量大） */
const LOG_POLL_MS = 120000;
const LOG_LIMIT = 80;

const AgentLedgerBody = () => {
  const nav = useArenaNav();
  const [market, setMarket] = useState<MarketId>('cn');
  const [selected, setSelected] = useState<string>('all');

  const overview = usePolling(() => fetchOverview(), [], 30000, 0);
  const rows: OverviewRow[] = useMemo(() => overview.data?.markets[market] ?? [], [overview.data, market]);
  const agentsKey = rows.map((r) => r.name).join('|');

  // 每个 agent 的模拟盘净值序列（卡片上的余额/收益）。
  //
  // `.filter(Boolean)` 不是可选的：上游对**没有净值数据的 agent 返 404**
  // （实测 `agents/market-research/performance` → `{"detail":"Agent 无净值数据"}`），
  // `catch(() => null)` 会把这个 null 原样留在数组里，下面 `.map(p => p.agent)`
  // 一取就抛 TypeError —— 而这一栏在错误边界里是**整页**被替换掉的。上游 Live.tsx
  // 的同一段本来就带这层过滤（它那边还有这类 agent），抽栏时漏抄了。
  const perfs = usePolling(
    () =>
      Promise.all(
        rows.map((r) => fetchPerformance(r.name, market).catch(() => null)),
      ).then((list) => list.filter(Boolean) as NonNullable<Awaited<ReturnType<typeof fetchPerformance>>>[]),
    [market, agentsKey],
    30000,
  );
  // A股实盘分账净值（每分钟采样）：模型卡优先显示实盘口径
  const liveEquity = usePolling(() => fetchLiveEquity(), [], 60000, 0);
  const tokenUsage = usePolling(() => fetchTokenUsage(), [], 30000, 8000);
  // 对话流：全部模型并行拉，混合成一条时间线（与实况页「模型对话」同款）
  const logs = usePolling<{ name: string; id: string; lines: LogLine[] }[]>(
    () =>
      Promise.all(
        rows.map((r) =>
          fetchLogs(r.name, market, LOG_LIMIT)
            .then((lines) => ({ name: displayAgentName(r.name), id: r.name, lines }))
            .catch(() => ({ name: displayAgentName(r.name), id: r.name, lines: [] as LogLine[] })),
        ),
      ),
    [market, agentsKey],
    LOG_POLL_MS,
  );

  const chatAgents = useMemo(() => {
    const all = logs.data ?? [];
    if (selected === 'all') return all;
    return all.filter((a) => a.id === selected);
  }, [logs.data, selected]);

  return (
    <div className="page" style={{ maxWidth: 1600 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <h1 style={{ marginBottom: 0 }}>智能体交易 · 台账</h1>
        <MarketSwitcher market={market} onChange={(m) => { setMarket(m); setSelected('all'); }} />
        <span style={{ flex: 1 }} />
        <span className="dim" style={{ fontSize: 11 }}>
          {liveEquity.data ? '余额口径：A股=实盘分账净值，其余=模拟盘' : '余额口径：模拟盘净值'}
        </span>
      </div>
      <p className="dim" style={{ fontSize: 11, margin: '4px 0 12px' }}>
        点卡片下钻到单个模型的明细；下面这条时间线是各模型的最新决策回合（倒序，点行展开）。
      </p>

      <div className="model-cards-section" style={{ marginTop: 0 }}>
        {(perfs.data ?? []).map((p) => {
          const eqPts = market === 'cn' ? liveEquity.data?.agents?.[p.agent] : null;
          const liveNav = eqPts && eqPts.length ? eqPts[eqPts.length - 1].value : null;
          const isLive = market === 'cn' && liveNav != null;
          return (
            <ModelCard
              key={p.agent}
              market={market}
              agent={p.agent}
              balance={isLive ? liveNav : (p.summary?.end_equity ?? null)}
              ret={isLive ? liveNav! / 100000 - 1 : (p.summary?.total_return ?? null)}
              selected={p.agent === selected}
              onClick={() => {
                if (selected === p.agent) setSelected('all');
                else {
                  setSelected(p.agent);
                  nav(`/model/${market}/${encodeURIComponent(p.agent)}`);
                }
              }}
              tokens={tokenUsage.data?.agents?.[p.agent] ?? null}
            />
          );
        })}
        {!perfs.data?.length && <div className="empty-state">该市场暂无 Agent</div>}
      </div>

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="panel-title">
          <span>决策日志 · 模型对话</span>
          <span className="dim" style={{ fontSize: 10 }}>
            {chatAgents.reduce((n, a) => n + a.lines.length, 0)} 条
          </span>
        </div>
        {logs.loading && !logs.data ? (
          <div className="loading" style={{ padding: 24 }}>
            <div className="spinner" />
            加载中…
          </div>
        ) : chatAgents.length ? (
          <ChatStream agents={chatAgents} />
        ) : (
          <div className="empty-state" style={{ padding: 24 }}>
            暂无对话记录
          </div>
        )}
      </div>
    </div>
  );
};

const AgentLedgerTab = () => (
  <ArenaSurface>
    <AgentLedgerBody />
  </ArenaSurface>
);

export default AgentLedgerTab;
