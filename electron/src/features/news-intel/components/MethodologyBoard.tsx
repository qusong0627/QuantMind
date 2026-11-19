/** 研究口径：标签字典 + 决策卡 + 来源质量 + Do-Not-Redo。 */

const DECISION_ROWS = [
  {
    rule: '硬利空 + pre5 > 5%',
    level: 'P0',
    label: '硬利空 · 高位',
    use: 'T+1 回避；不抄底',
    evidence: '+186.8bp/事件，t=2.81；开盘跌停约 5.7% 卖不掉',
  },
  {
    rule: '硬利空 + 2% < pre5 ≤ 5%',
    level: 'P1',
    label: '硬利空 · 中位',
    use: '只观察；不单独卖出',
    evidence: '-82bp，p=0.106，不显著',
  },
  {
    rule: '利好 + pre5 > 5%',
    level: 'P2',
    label: '利好 · 高位（不追）',
    use: '不加仓；不是卖出信号',
    evidence: '5 日 -103bp，P1/P2 同向',
  },
  {
    rule: '软利空 + -2% ≤ pre5 ≤ 5%',
    level: 'WATCH',
    label: '软利空 · 反弹区间',
    use: '不追空；不做买入',
    evidence: '+78~+133bp；3/4/5/11 月为负，只观察',
  },
  {
    rule: 'L1 代码 + 利好',
    level: 'WATCH',
    label: 'L1 利好 · 观察',
    use: '观察池；不是策略',
    evidence: '发布后 30 分钟 +8.6bp，t=2.0',
  },
  {
    rule: '其他',
    level: 'INFO',
    label: '信息 / 利好·不追 / 软利空·观察',
    use: '不动作',
    evidence: '无通过严格验证的正向格',
  },
];

const TAGS = [
  { name: 'L1 代码', desc: '标题/摘要出现股票代码', use: '最高置信，可落个股事件' },
  { name: 'L2 标题主体', desc: '公司全名出现在标题且非媒体/研报作者', use: '高置信，个股新闻强度主口径' },
  { name: 'L3 正文提及', desc: '公司名只在正文出现', use: '只做热度，不做个股判决' },
  { name: 'L4 实体情绪', desc: '仅 enrichment 实体命中', use: '只做背景' },
  { name: '硬利空', desc: '财务造假/立案/退市/债务/事故/制裁/监管处罚', use: '唯一能进 P0/P1 的方向' },
  { name: '软利空', desc: '业绩风险/减持解禁/质押/诉讼/舆情', use: '只在 -2~5% 区间做观察' },
  { name: '硬利好', desc: '中标/订单/预增/增长/扭亏/回购/增持/分红/并购/重组', use: '没有正向买入格，只做「不追」' },
  { name: '治理利好', desc: '股权激励/战略合作', use: '同上' },
  { name: '泛利好/泛利空', desc: '只有情绪、没有硬事件', use: '只做风险提示/热度' },
  { name: 'pre5', desc: '新闻前 5 个交易日累计收益（行业调整）', use: '决定 P0/P1/P2；缺失一律 INFO' },
  { name: '转载层', desc: '同题 24h 内第几篇；3+ 层更差', use: '不按方向加仓' },
  { name: '投机稿', desc: '涨停/连板/题材/龙虎榜/异动', use: '降权；n_speculative 只做波动 overlay' },
];

const SOURCE_GOOD = '新浪财经 +172bp、人民日报 +171bp、东财行业研报 +124bp、集思录 +132bp、同花顺实时 +92bp';
const SOURCE_BAD = '财联社电报 -262bp、东财快讯 -298bp、财联社热榜 -76bp、格隆汇快讯 -56bp、商业 Google -55bp';

const DNR = [
  '看到利好就买：没有通过严格验证的稳定正向格',
  '板块利好追涨：发布前已涨 18~21bp，发布后无延续',
  'L2 标题当 L1 用：L2 利好 post30 -12.4bp（t=-7.1）',
  '主题生命周期阶段规则：P2 失效',
  '跨市场映射（NVDA/AAPL→A 股主题）：P1/P2 符号翻转',
  '研报评级当买点：高位/低位评级都负',
  '快讯/热榜当 alpha：快讯在股票涨完之后才发',
  '新闻情绪当大盘方向：只有新闻量显著，且 P1≈0',
];

export function MethodologyBoard() {
  return (
    <div className="ni-method">
      <header className="ni-method-head">
        <div className="ni-kicker">研究口径 · 2026-11-18</div>
        <h1>这套资讯体系怎么读</h1>
        <p>
          所有标签与权重来自 <b>Huntly 1.77M 篇资讯 × A股/港股/美股</b> 的样本内研究；
          决策卡只采纳通过「P1/P2 同号 + |t|≥2 + 扣费后仍成立」的格子。
          没有通过的，一律降级为观察或 Do-Not-Redo。
        </p>
      </header>

      <section className="ni-method-section">
        <h2>1. 决策卡（唯一可执行层）</h2>
        <table className="ni-method-table">
          <thead>
            <tr>
              <th>条件</th>
              <th>等级</th>
              <th>界面用语</th>
              <th>怎么用</th>
              <th>证据</th>
            </tr>
          </thead>
          <tbody>
            {DECISION_ROWS.map((r) => (
              <tr key={r.rule}>
                <td>{r.rule}</td>
                <td>
                  <span className={`ni-chip is-${r.level.toLowerCase()}`}>{r.level}</span>
                </td>
                <td>{r.label}</td>
                <td>{r.use}</td>
                <td className="ni-method-evidence">{r.evidence}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="ni-method-note">
          展示面只说「回避 / 不追 / 观察 / 信息」；买入/卖出/减仓只出现在执行面。
        </p>
      </section>

      <section className="ni-method-section">
        <h2>2. 标签字典</h2>
        <div className="ni-method-grid">
          {TAGS.map((t) => (
            <div className="ni-method-card" key={t.name}>
              <strong>{t.name}</strong>
              <p>{t.desc}</p>
              <em>{t.use}</em>
            </div>
          ))}
        </div>
      </section>

      <section className="ni-method-section">
        <h2>3. 来源质量（adj DID5，控制事件类型 × pre5）</h2>
        <div className="ni-method-two">
          <div className="ni-method-card is-good">
            <strong>相对好</strong>
            <p>{SOURCE_GOOD}</p>
          </div>
          <div className="ni-method-card is-bad">
            <strong>相对差（快讯/热榜）</strong>
            <p>{SOURCE_BAD}</p>
          </div>
        </div>
        <p className="ni-method-note">
          快讯源的平均 pre5 极高（财联社电报 +485bp、东财快讯 +949bp）——它们不是提前知道，
          而是在股票涨完之后发。来源质量只调权重/置信度，不改决策。
        </p>
      </section>

      <section className="ni-method-section">
        <h2>4. 已证伪清单（Do-Not-Redo）</h2>
        <ul className="ni-method-dnr">
          {DNR.map((x) => (
            <li key={x}>{x}</li>
          ))}
        </ul>
      </section>

      <section className="ni-method-section">
        <h2>5. 边界</h2>
        <ul className="ni-method-dnr">
          <li>板块成员是当前快照，不是逐日 PIT；板块热度是新闻口径，不是涨跌预测。</li>
          <li>事件后走势不是回测：未扣费、未排涨跌停/停牌，只做「这条新闻后来发生了什么」的证据展示。</li>
          <li>A 股的 P0/P1/P2 口径不外推到港股/美股；港股/美股只做事件分类与热度。</li>
          <li>新闻时间戳是发布时间/抓取时间，不是首次公开时间；pre30 漂移可能包含抢跑。</li>
        </ul>
      </section>

      <footer className="ni-method-foot">
        研究原件：data/news_intel/20261118/REPORT.md · NEXT_RESEARCH_SECTION.md ·
        PRIOR_CONCLUSIONS_DETAILED_20261118.md（NAS 副本 NSF2026/reports/个股因果手册_20261118）
      </footer>
    </div>
  );
}
