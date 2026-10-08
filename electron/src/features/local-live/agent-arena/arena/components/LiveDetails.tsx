import { MarketId, LiveAccount, OverviewRow, fetchRealAccounts } from '../api/client';
import { usePolling } from '../hooks/usePolling';
import { fmtDate, fmtPct } from '../utils/format';
import ChannelStatus from './ChannelStatus';
import './LiveDetails.css';

/** 实况「详情」tab：系统规格说明（复盘/交接用）。
 *
 *  写作口径：每条都写「实现 + 数据源 + 失败态」，不写愿景。硬编码日期一律去掉，
 *  时效数据实时取（数据日期、账户快照时刻、通道在线状态由 ChannelStatus 探）。 */

type Market = MarketId;

/** 该市场的基准 / 规则 / 执行通道 / 行情源（与 Live 顶部条同源口径） */
const MARKET_SPEC: Record<Market, { bench: string; rule: string; venue: string; exec: string; src: string }> = {
  cn: {
    bench: 'SSE 50（上证 50，等权口径）',
    rule: 'T+1 · 主板 ±10% 涨跌停 · 100 股整手',
    venue: '上交所 / 深交所 / 北交所',
    exec: '通达信客户端（桥 → Windows 侧 HTTP 8550）',
    src: '通达信桥实时快照 + QuantDB（收盘后）',
  },
  hk: {
    bench: 'HSI（恒生指数）',
    rule: 'T+0 · 无涨跌停 · 每手股数按标的',
    venue: '香港交易所',
    exec: '富途 OpenD（本机 11111 网关）',
    src: 'data/HK_stock/merged.jsonl（腾讯行情，后复权）',
  },
  us: {
    bench: 'NDX 100（纳斯达克 100，等权口径）',
    rule: 'T+0 · 无涨跌停 · 1 股起',
    venue: 'NASDAQ / NYSE',
    exec: '盈透证券 IB Gateway（通道已下线：未迁移）',
    src: 'data/merged.jsonl（日线 + 小时线数据集）',
  },
};

/** 已知交易智能体的角色说明（未知名字兜底到通用描述）。
 *  研究总控（market-research）不建仓、不计入阵容，见下方 roster——故不在此表。 */
const AGENT_ROLE: Record<string, string> = {
  'deepseek-v4-flash': '工具型 agent：行情 / QuantDB / 搜索 / 记忆 / 数学 MCP 工具 + 可写代码，配 1–2 分钟时间盒工作法与时段作战手册。',
  'deepseek-v4-pro': '直连 LLM 分析：同数据注入、同决策 schema、同风控闸门，无工具调用。',
  glm: '直连 LLM 分析：同数据注入、同决策 schema、同风控闸门。',
};

const FALLBACK_ROLE = '直连 LLM 分析：同数据注入、同决策 schema、同风控闸门。';

type PipelineRow = { stage: string; how: string; src: string; cadence: string };

/** A 股：实盘链路（桥实时行情 + L2 微观因子 + 分账实盘执行） */
const PIPELINE_CN: PipelineRow[] = [
  { stage: '① 行情', how: '桥实时快照（现价/五档/盘口失衡/隔夜跳空/5 分钟涨速/量比）', src: '通达信桥快照缓存', cadence: '分钟级' },
  { stage: '② 基本面', how: 'QuantDB 全市场日线 / 财报 / 板块 / 因子库', src: 'QuantDB（收盘入库）', cadence: '每日收盘后' },
  { stage: '③ 微观结构', how: 'L2 逐笔微观因子（VPIN / 分区分布 / 价量背离 / 冲击半衰）', src: 'tdx_l2_snapshot', cadence: '盘中 60s 采集' },
  { stage: '④ 决策', how: '逐只简评 → 四段式输出（总体总结 / 分析链路 / 推理论证 / JSON 决策）', src: '各 agent LLM', cadence: '整点 + 手动' },
  { stage: '⑤ 闸门', how: '系统侧强制校验：可卖量 / 单票上限 / 杠杆线 / 涨跌停 / 额度不透支', src: 'buy_gate / leverage_guard', cadence: '每笔下单前' },
  { stage: '⑥ 执行', how: '限价单 → 桥下单 → 成交回报确认；watch 决策挂分钟级价格哨兵', src: '通达信桥', cadence: '决策后即时' },
  { stage: '⑦ 记账', how: '成交按回报确认后入账，分账账本按模型归属；「已完成」= 真实清仓流', src: 'live_ledger / live_fills', cadence: '成交回报时' },
];

/** 港股 / 美股：模拟盘回放链路（行情来自本地历史数据集，成交按回放价撮合） */
const PIPELINE_SIM = (market: Market, broker: string): PipelineRow[] => [
  { stage: '① 行情', how: '全量历史日线 / 小时线，按所选日期切片注入（当日不返回高/低/收/量）', src: MARKET_SPEC[market].src, cadence: '每日更新' },
  { stage: '② 情报', how: '新闻与研报检索（关键词 / 标的维度的情绪与事件）', src: '搜索 MCP + 新闻管线', cadence: '按回合拉取' },
  { stage: '③ 决策', how: '逐只简评 → 四段式输出（总体总结 / 分析链路 / 推理论证 / JSON 决策）', src: '各 agent LLM', cadence: '整点 + 手动' },
  { stage: '④ 闸门', how: '系统侧强制校验：额度 / 单票上限 / 杠杆线 / 决策完整性', src: 'buy_gate / leverage_guard', cadence: '每笔下单前' },
  { stage: '⑤ 执行', how: '模拟撮合：按当日回放价成交并写入持仓与现金流水', src: '模拟盘撮合器', cadence: '决策后即时' },
  { stage: '⑥ 记账', how: '持仓快照 + 成交流水落盘；「已完成」为真实清仓流', src: `agent_data_${market} / position.jsonl`, cadence: '成交时' },
  { stage: '⑦ 实盘对照', how: `真金账户经 ${broker} 只读对照，不参与模拟盘决策`, src: broker, cadence: '按需' },
];

/** A 股实盘闸门（系统侧强制，模型不可绕过） */
const GATES_CN: { name: string; detail: string }[] = [
  { name: 'T+1 可卖量复核', detail: '当日买入部分不计入可卖，防止超卖被券商拒单。' },
  { name: '单票集中度', detail: '单票买入额 ≤ 该模型剩余额度 × 20%。' },
  { name: '杠杆上限', detail: '持仓市值 ≤ 权益 × 1.5，超线由分钟级守护自动减仓。' },
  { name: '涨跌停纪律', detail: '涨停不追、跌停不接（避免在无流动性价格上成交）。' },
  { name: '额度不透支', detail: '分账额度（每模型 ¥10 万）用尽即拒，不产生负现金。' },
  { name: '拒单重放', detail: '拒单自动登记为延期单，行情恢复后重放而非静默丢弃。' },
  { name: '行情停更硬闸', detail: '行情停更期间禁止基于价格的交易决策（不看旧价下单）。' },
  { name: '退出框架必填', detail: '缺止损 / 止盈 / 移动止损 / 失效条件的买卖决策作废。' },
];

/** 模拟盘闸门：与实盘同构，去掉券商侧规则，补无前视 */
const GATES_SIM: { name: string; detail: string }[] = [
  { name: '无前视硬闸', detail: '严格按所选日期切片喂数；当日盘中的高 / 低 / 收 / 量一律不返回。' },
  { name: '单票集中度', detail: '单票买入额 ≤ 该模型剩余额度 × 20%。' },
  { name: '杠杆上限', detail: '持仓市值 ≤ 权益 × 1.5，超线由分钟级守护自动减仓。' },
  { name: '额度不透支', detail: '初始额度用尽即拒单，不产生负现金。' },
  { name: '重复下单防护', detail: '同一标的同向未成交委托不重复挂，防止批量决策重复扣额。' },
  { name: '行情停更硬闸', detail: '行情停更期间禁止基于价格的交易决策（不看旧价下单）。' },
  { name: '退出框架必填', detail: '缺止损 / 止盈 / 移动止损 / 失效条件的买卖决策作废。' },
];

export default function LiveDetails({
  market,
  rows,
  currency = '¥',
  futuBoth = null,
  ibkr = null,
}: {
  market: Market;
  rows: OverviewRow[];
  currency?: string;
  futuBoth?: { real: LiveAccount | null; simulate: LiveAccount | null } | null;
  ibkr?: { total_asset: number } | null;
}) {
  const spec = MARKET_SPEC[market];
  const dataDate = rows.map((r) => r.latest_date).filter(Boolean).sort().pop() ?? null;
  // 研究总控（market-research）只做研究/情报、自身不建仓，不计入「智能体阵容」；
  // 其对话与净值另有入口（Live 页的模型筛选与独立净值线）。
  const roster = rows.filter((r) => r.name !== 'market-research');
  const pipeline = market === 'cn' ? PIPELINE_CN : PIPELINE_SIM(market, spec.exec);
  const gates = market === 'cn' ? GATES_CN : GATES_SIM;
  // 模拟盘初始额度：取各 agent 净值序列起点（实时取，不硬编码）
  const startEquity = rows.map((r) => r.summary?.start_equity).find((v) => v != null) ?? null;
  // A 股两账户最新快照摘要：本组件取（要拿快照时刻），再传给 ChannelStatus 复用同一份
  const summaries = usePolling(
    () => (market === 'cn' ? fetchRealAccounts().catch(() => null) : Promise.resolve(null)),
    [market],
    30000,
  );
  const tdxSnapshotTs = summaries.data?.find((s) => s.account === 'tdx')?.ts ?? null;

  return (
    <div className="ld-body">
      {/* ① 通道在线状态 —— 用户口径「一看就知道在线没在线」 */}
      <h4 className="ld-h">① 交易通道与在线状态</h4>
      <ChannelStatus
        market={market}
        futuBoth={futuBoth}
        ibkr={ibkr}
        summaries={summaries.data}
      />
      <dl className="ld-spec">
        <dt>交易场所</dt>
        <dd>{spec.venue}</dd>
        <dt>执行通道</dt>
        <dd>{spec.exec}</dd>
        <dt>交易规则</dt>
        <dd>{spec.rule}</dd>
        <dt>业绩基准</dt>
        <dd>{spec.bench}</dd>
      </dl>

      {/* ② 端到端链路 */}
      <h4 className="ld-h">② 端到端链路（数据 → 决策 → 执行 → 记账）</h4>
      <table className="ld-table">
        <thead>
          <tr>
            <th>环节</th>
            <th>实现</th>
            <th>数据源</th>
            <th>节奏</th>
          </tr>
        </thead>
        <tbody>
          {pipeline.map((p) => (
            <tr key={p.stage}>
              <td className="ld-stage">{p.stage}</td>
              <td>{p.how}</td>
              <td className="ld-src">{p.src}</td>
              <td className="ld-cad">{p.cadence}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* ③ 阵容（实时取自 overview） */}
      <h4 className="ld-h">③ 智能体阵容（{roster.length} 个）</h4>
      {roster.length === 0 && <p className="ld-empty">该市场暂无启用中的智能体。</p>}
      {roster.map((r) => (
        <p className="ld-agent" key={r.name}>
          <b className="ld-agent-name">{r.name}</b>
          {r.summary?.total_return != null && (
            <span className={`ld-agent-ret ${r.summary.total_return >= 0 ? 'up' : 'down'}`}>
              {fmtPct(r.summary.total_return)}
            </span>
          )}
          <span className="ld-agent-role">{AGENT_ROLE[r.name] ?? FALLBACK_ROLE}</span>
        </p>
      ))}

      {/* ④ 风控闸门 */}
      <h4 className="ld-h">④ 风控闸门（系统侧强制，模型不可绕过）</h4>
      <ul className="ld-list">
        {gates.map((g) => (
          <li key={g.name}>
            <b>{g.name}</b>：{g.detail}
          </li>
        ))}
      </ul>

      {/* ⑤ 决策与记账口径 */}
      <h4 className="ld-h">⑤ 决策与记账口径</h4>
      <ul className="ld-list">
        <li>
          <b>决策必带</b>：买入/卖出理由、止损、止盈、移动止损、失效条件、置信度、风险额——缺退出框架
          的决策作废。
        </li>
        <li>
          <b>成交确认</b>：只有桥/券商的成交回报（filled_volume &gt; 0）才记入成交与账本；拒单与废单不记。
        </li>
        {market === 'cn' ? (
          <li>
            <b>分账口径</b>：A 股每模型独立 ¥10 万虚拟子账户，持仓与收益按模型归属，互不挪用。
          </li>
        ) : (
          <li>
            <b>初始资金</b>：每模型独立账户
            {startEquity != null ? `（${currency}${startEquity.toLocaleString('en-US')}）` : ''}
            ，全部成交按所选日期的回放价撮合，互不挪用。
          </li>
        )}
        <li>
          <b>清仓定义</b>：「已完成」只收录真实把仓位卖到 0 的平仓流，减仓不计入。
        </li>
        <li>
          <b>净值口径</b>：
          {market === 'cn'
            ? '实盘线按桥实时价计算的分账净值，分钟级采样；模拟盘仅作对照展示。'
            : '按当日回放价逐笔重估持仓（净值 = 现金 + Σ 持仓市值），每个持仓记录一个采样点；当日无价时沿用最近已知价。'}
        </li>
      </ul>

      {/* ⑥ 数据时效（全部实时取值，无硬编码日期） */}
      <h4 className="ld-h">⑥ 数据口径与时效</h4>
      <dl className="ld-spec">
        <dt>页面刷新</dt>
        <dd>行情 30s · 账户 15–30s · 净值 60s · 对话日志 120s</dd>
        <dt>行情数据日期</dt>
        <dd>{dataDate ? fmtDate(dataDate) : '—'}</dd>
        <dt>实盘账户快照</dt>
        <dd>
          {market === 'cn'
            ? tdxSnapshotTs
              ? `${tdxSnapshotTs.slice(0, 19).replace('T', ' ')}（通达信账户）`
              : '暂无快照'
            : market === 'hk'
              ? futuBoth?.real
                ? '实时（富途 OpenD 直读）'
                : '未读通'
              : ibkr
                ? '实时（IB Gateway 直读）'
                : '通道已下线'}
        </dd>
        <dt>情绪温度</dt>
        <dd>昨日收盘全景（盘前 / 盘后参考，盘中不更新）</dd>
        <dt>时间口径</dt>
        <dd>页面所有时刻均为北京时间（UTC+8）</dd>
      </dl>
    </div>
  );
}
