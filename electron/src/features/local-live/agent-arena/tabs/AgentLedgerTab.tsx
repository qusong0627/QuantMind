import { useMemo, useState } from 'react';
import ArenaSurface from '../ArenaSurface';
// 本栏的模型卡 DOM（.model-cards-section / .model-card-mini / .model-logo…）复用
// 实况页的类名，而它们的**基础排版规则住在 Live.css**（页面级样式，随实况页的
// 懒加载 chunk 才载入）。不在这里显式 import 的话：新开会话直接进本栏（没访问过
// 实况）时，卡片拿到的是「无 display:flex 的裸块」——三张卡各占一整行、内容挤在
// 左边、收益率药丸飘到最右缘（2026-10-09 用户报障「就是3行，很难看」的根因）。
// 显式依赖 > 碰运气加载顺序：本栏自己保证 Live.css 先于渲染到位。
import '../arena/pages/Live.css';
import {
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
 * 「智能体台账」栏（2026-10-09 由「智能体交易」改名）—— 各 agent 台账 + 决策日志
 *（模型对话）。
 *
 * 这一栏不是新造的面板，而是把**实况页右边那两块**（模型卡 = 各 agent 权益、
 * 对话流 = 决策日志）抽出来单独成栏，方便盯着看：实况页给的是「账 + 盘」的全景，
 * 这一栏只回答「每个 agent 现在有多少钱、它上一次决策说了什么」。
 *
 * 数据口径与实况页逐字一致（同一批端点、同一轮询节奏），所以两栏的数字不会打架：
 *   overview → 当前市场的 agent 名单；performance → A股=PG 实盘账本 summary
 *   （余额 = end_equity，收益 = period_return），港美=模拟盘净值/收益；
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
        <h1 style={{ marginBottom: 0 }}>智能体台账</h1>
        <MarketSwitcher market={market} onChange={(m) => { setMarket(m); setSelected('all'); }} />
        <span style={{ flex: 1 }} />
        <span className="dim" style={{ fontSize: 11 }}>
          {market === 'cn' ? '余额口径：通达信实盘分账账本' : '余额口径：模拟盘净值'}
        </span>
      </div>
      <p className="dim" style={{ fontSize: 11, margin: '4px 0 12px' }}>
        点卡片下钻到单个模型的明细；下面这条时间线是各模型的最新决策回合（倒序，点行展开）。
      </p>

      <div className="model-cards-section" style={{ marginTop: 0 }}>
        {(perfs.data ?? []).map((p) => (
          <ModelCard
            key={p.agent}
            market={market}
            agent={p.agent}
            // 与实况页 / 模型详情同一口径：余额 = 账本重建的期末权益；
            // A 股用区间收益率（total_return 以策略额度为基，迁移起点低于额度，
            // 拿它展示等于把期初持仓未入账的缺口记成亏损）。
            balance={p.summary?.end_equity ?? null}
            ret={market === 'cn' ? p.summary?.period_return ?? null : p.summary?.total_return ?? null}
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
        ))}
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
