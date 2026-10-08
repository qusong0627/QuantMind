import './About.css';

/** 色码：颜色只用来标记「属于哪条产线」，不做装饰。
 *  trade 交易 / agent 体系与市场 / news 新闻 / lab 回测 / risk 风控实盘 / sys 系统 / muted 备注 */
type Tone = 'trade' | 'agent' | 'news' | 'lab' | 'risk' | 'sys' | 'muted';

const SECTIONS: { id: string; title: string; en: string; tone: Tone }[] = [
  { id: '01', title: '系统总览', en: 'Overview', tone: 'trade' },
  { id: '02', title: '智能体谱系', en: 'Agent Roster', tone: 'agent' },
  { id: '03', title: '新闻智能体', en: 'News Pipeline', tone: 'news' },
  { id: '04', title: '回测智能体', en: 'Backtest', tone: 'lab' },
  { id: '05', title: '系统架构', en: 'Architecture', tone: 'sys' },
  { id: '06', title: '交易闭环', en: 'Trading Loop', tone: 'trade' },
  { id: '07', title: '三市场并行', en: 'Markets', tone: 'agent' },
  { id: '08', title: '赛制与实盘通道', en: 'Live Channels', tone: 'risk' },
  { id: '09', title: '全天候循环', en: 'Daily Cycle', tone: 'lab' },
  { id: '10', title: '口径与透明', en: 'Metrics & Audit', tone: 'sys' },
  { id: '11', title: '扩展开发', en: 'Extend', tone: 'trade' },
  { id: '12', title: '免责声明', en: 'Disclaimer', tone: 'muted' },
];

const SEC_BY_ID = new Map(SECTIONS.map((s) => [s.id, s]));

function SecHead({ id }: { id: string }) {
  const s = SEC_BY_ID.get(id);
  const tone = s?.tone ?? 'sys';
  return (
    <h2 className={`panel-title tone-${tone}`}>
      <span className="about-sec-t">
        <span className={`about-sec-num ${tone}`}>{s?.id ?? id}</span>
        {s?.title ?? ''}
      </span>
      <span className={`about-sec-en ${tone}`}>{s?.en ?? ''}</span>
    </h2>
  );
}

/** 智能体谱系：四类角色的职责与边界。 */
const AGENTS: {
  id: string;
  name: string;
  role: string;
  tone: Tone;
  desc: string;
  rows: [string, string][];
}[] = [
  {
    id: 'A',
    name: '交易智能体',
    role: '决策与执行',
    tone: 'trade',
    desc: '三个角色分化的模型同池竞技，在真实券商通道上自主交易：独立额度、独立持仓、独立记忆与复盘，全程零人工干预。',
    rows: [
      ['节奏', '盘前定档 · 盘中整点各一轮 · 盘后复盘'],
      ['工具', 'MCP 工具链：行情 / 数学 / 搜索 / 交易 / 记忆'],
      ['产出', '决策 JSON → 风控闸门 → 成交 · 持仓 · 决策日志'],
    ],
  },
  {
    id: 'B',
    name: '新闻智能体',
    role: '情报流水线',
    tone: 'news',
    desc: '六段接力：门卫去噪分级 → 宏观 / 板块个股 / 持仓三路并行 → 主编汇总成简报 → 晚间复盘。简报注入交易提示词，复盘沉淀为经验先验。',
    rows: [
      ['节奏', '盘中每 30 分钟一期 · 19:05 收盘全景 · 22:30 复盘'],
      ['协议', '行式结构化输出，单段失败降级、不污染整期'],
      ['产出', 'latest.json 简报 → 提示词；lessons 经验 → 先验'],
    ],
  },
  {
    id: 'C',
    name: '回测智能体',
    role: '策略验证',
    tone: 'lab',
    desc: 'Pine 策略库经 AI 转写为 Python 策略，在宿主沙箱里按真实费率回测；批量跑批把「策略 × 股票池」拉成一张可排序的证据表。',
    rows: [
      ['链路', '选库 → AI 转写 → 试跑纠错 → 沙箱回测'],
      ['粒度', '单标的 K 线级回测 · 全池批量排行与选股'],
      ['口径', 'quantdb 日线 · 三档复权 · 双边费率与滑点'],
    ],
  },
  {
    id: 'D',
    name: '风控与研究智能体',
    role: '闸门与进化',
    tone: 'risk',
    desc: '风控不是提示词而是确定性代码：风险预算每日按波动 / 回撤 / 情绪给全场上限定档，模型不可绕过。盘后复盘与晚间研究把当日教训沉淀成次日先验。',
    rows: [
      ['风控', '单笔 ≤ 额度 20% · 日亏熔断 5% · 行情停更硬闸'],
      ['研究', '19:30 晚间总控选池 20 只 → 明晨优先进池'],
      ['进化', '日复盘教训 + 周度假设复测 → 回流提示词'],
    ],
  },
];

/** 新闻管线六段。 */
const NEWS_STAGES: {
  name: string;
  when: string;
  job: string;
  out: string;
  tone: Tone;
  parallel?: boolean;
}[] = [
  {
    name: '新闻门卫',
    when: '每期首段',
    job: '全源去噪与分级：快讯准/富化率高的核心源全量进，外围源按信息量放行',
    out: '入选事件表（分市场 / 主体 / 情绪）',
    tone: 'sys',
  },
  {
    name: '宏观政策研究',
    when: '与下两段并行',
    job: '政策与宏观事件 → 传导路径与方向性判断',
    out: 'view · bias · drivers',
    tone: 'news',
    parallel: true,
  },
  {
    name: '板块个股情报',
    when: '与上下两段并行',
    job: '行业与个股事件 → 受益 / 受损标的映射',
    out: '事件-标的映射表',
    tone: 'news',
    parallel: true,
  },
  {
    name: '持仓情报',
    when: '与上两段并行',
    job: '只看在手持仓，逐只核对有无实质影响',
    out: '持仓信号表',
    tone: 'news',
    parallel: true,
  },
  {
    name: '新闻主编',
    when: '各段汇齐后',
    job: '抑制重复与互相矛盾，汇成一份可直接进提示词的简报',
    out: 'data/news_brief/latest.json',
    tone: 'trade',
  },
  {
    name: '晚间复盘',
    when: '22:30',
    job: '当日信号 vs 实际走势：命中 / 反向 / 无反应三类计数',
    out: 'lessons.json（按事件类型 × 来源累计，样本 ≥3 才进先验）',
    tone: 'lab',
  },
];

const TL: { t: string; c: string; tone: Tone }[] = [
  { t: '09:10', c: '风险预算定档：波动 / 回撤 / 情绪 → 三档闸门参数（平静 1.5× / 谨慎 1.2× / 防守 1.0×）', tone: 'risk' },
  { t: '09:25', c: '新闻盘前一期：隔夜与早间事件进入当日提示词', tone: 'news' },
  { t: '09:30–15:00', c: '盘中交易：整点 ×3 agent 分析（注入昨日复盘要点 / 已验证假设 / 盘面状态 / 情绪温度 / 新闻简报）→ JSON 决策 → 确定性闸门 → 桥下单 → 分钟级哨兵', tone: 'trade' },
  { t: '09:55–13:55', c: '新闻盘中四期：每期在整点交易分析前 5 分钟产出', tone: 'news' },
  { t: '15:35', c: '盘后复盘：逐笔归因 → 记忆沉淀教训 → 明日预案（watch 双条件单）→ 新假设登记', tone: 'trade' },
  { t: '17:00', c: '系统运行日报：表现 / 服务 / 待办一页纸', tone: 'sys' },
  { t: '19:05', c: '新闻收盘全景：全天事件收束，供晚间选池消费', tone: 'news' },
  { t: '19:30', c: '晚间研究总控：板块强度 + 明日 20 只候选池 → 明晨选股优先进池', tone: 'agent' },
  { t: '22:30', c: '新闻晚间复盘：信号有效性检验 → lessons 经验计数', tone: 'lab' },
  { t: '周日 21:00', c: '假设库周度复测：定性后带胜率证据回流提示词', tone: 'lab' },
];

/** About 页：系统说明（架构 / 智能体谱系 / 口径），排版随 Arena 终端风。 */
export default function About() {
  return (
    <div className="page about-page">
      {/* 头部 */}
      <div className="about-head">
        <div className="about-title-row">
          <h1>
            ABOUT <span className="accent">/</span> 关于竞技场
          </h1>
          <span className="about-ver">v0.3.0</span>
        </div>
        <p className="about-lead">
          多模型、多市场的自主交易平台：交易、新闻、回测三类智能体各司其职，
          以真实券商通道 + 确定性风控闸门运行，全部决策可回溯、可复现。
        </p>
        <div className="about-meta">
          <span className="about-meta-item"><b>市场</b> 美股 · A股 · 港股</span>
          <span className="about-meta-item"><b>模型</b> v4-flash（工具型）/ v4-pro（研究员）/ glm（消息面）</span>
          <span className="about-meta-item"><b>数据底座</b> QuantDB + 通达信桥 + 同花顺 Fuyao</span>
          <span className="about-meta-item"><b>实盘通道</b> 通达信桥（下单）· 迅投 QMT 桥（只读复核）</span>
        </div>
      </div>

      {/* 目录 */}
      <nav className="about-toc" aria-label="页内导航">
        {SECTIONS.map((s) => (
          <a key={s.id} href={`#about-${s.id}`} className="about-toc-item">
            <i className={`about-dot ${s.tone}`} />
            <span className="about-toc-num">{s.id}</span>
            {s.title}
          </a>
        ))}
      </nav>

      {/* 01 系统总览 */}
      <section id="about-01" className="panel about-sec">
        <SecHead id="01" />
        <div className="about-hero">
          <div className="about-hero-cell agent">
            <div className="about-hero-k">市场并行</div>
            <div className="about-hero-v">3</div>
            <div className="about-hero-s">NDX100 · SSE50 · 恒指成分</div>
          </div>
          <div className="about-hero-cell trade">
            <div className="about-hero-k">交易模型对决</div>
            <div className="about-hero-v">3</div>
            <div className="about-hero-s">Flash · Pro · GLM 5.3</div>
          </div>
          <div className="about-hero-cell news">
            <div className="about-hero-k">新闻智能体</div>
            <div className="about-hero-v">6</div>
            <div className="about-hero-s">门卫 → 三路并行 → 主编 → 复盘</div>
          </div>
          <div className="about-hero-cell lab">
            <div className="about-hero-k">Pine 策略库</div>
            <div className="about-hero-v">1001</div>
            <div className="about-hero-s">AI 转写后进沙箱回测</div>
          </div>
        </div>
        <p className="about-desc" style={{ marginTop: 12 }}>
          平台围绕三条产线组织：<b>交易</b>负责在真实行情下做决策并承担后果；
          <b>新闻</b>负责把全天候的信息流压缩成可进提示词的结构化简报；
          <b>回测</b>负责在下注之前把策略放进同口径的历史里跑一遍。
          三者共享同一套数据底座与风控闸门——闸门是确定性代码，模型无法绕过；
          每个 agent 拥有独立额度、持仓、记忆与复盘，决策-执行-风控全程落盘可回溯。
        </p>
      </section>

      {/* 02 智能体谱系 */}
      <section id="about-02" className="panel about-sec">
        <SecHead id="02" />
        <div className="about-cards">
          {AGENTS.map((a) => (
            <article key={a.id} className={`about-card ${a.tone}`}>
              <header className="about-card-head">
                <span className={`about-card-id ${a.tone}`}>{a.id}</span>
                <span className="about-card-name">{a.name}</span>
                <span className={`about-card-role ${a.tone}`}>{a.role}</span>
              </header>
              <p className="about-card-desc">{a.desc}</p>
              <dl className="about-card-rows">
                {a.rows.map(([k, v]) => (
                  <div key={k} className="about-card-row">
                    <dt>{k}</dt>
                    <dd>{v}</dd>
                  </div>
                ))}
              </dl>
            </article>
          ))}
        </div>
        <p className="about-desc">
          四类智能体不是并列的四个产品，而是一条链上的四个位置：
          <b>新闻</b>提供输入，<b>交易</b>做出动作，<b>风控与研究</b>约束动作并沉淀经验，
          <b>回测</b>在动作发生前给出统计意义上的先验。每个模型在三个市场独立记账，
          同市场内所有模型使用完全相同的行情、工具集与提示词框架——差异只来自模型本身的判断。
        </p>
      </section>

      {/* 03 新闻智能体 */}
      <section id="about-03" className="panel about-sec">
        <SecHead id="03" />
        <p className="about-desc" style={{ marginBottom: 10 }}>
          新闻链路的难点不在「读新闻」，而在<b>噪声抑制</b>与<b>可追溯</b>：
          以 2026-09-07 当周 19,238 篇实证为据，对来源做了分层（核心快讯源全量进门卫，
          高量产源与外围源按信息量放行），再让六段 agent 各管一段、互不越界。
        </p>
        <div className="about-pipe">
          <span className="about-pipe-node sys">采集入库</span>
          <span className="about-pipe-arrow">→</span>
          <span className="about-pipe-node hl news">新闻门卫</span>
          <span className="about-pipe-arrow">→</span>
          <span className="about-pipe-group news">
            <span className="about-pipe-node sub news">宏观政策</span>
            <span className="about-pipe-node sub news">板块个股</span>
            <span className="about-pipe-node sub news">持仓</span>
          </span>
          <span className="about-pipe-arrow">→</span>
          <span className="about-pipe-node hl trade">新闻主编</span>
          <span className="about-pipe-arrow">→</span>
          <span className="about-pipe-node trade">简报注入</span>
          <span className="about-pipe-arrow">↺</span>
          <span className="about-pipe-node lab">晚间复盘</span>
        </div>
        <table className="about-table news">
          <thead>
            <tr>
              <th>段</th>
              <th>触发（北京时间）</th>
              <th>职责</th>
              <th>产物</th>
            </tr>
          </thead>
          <tbody>
            {NEWS_STAGES.map((s) => (
              <tr key={s.name}>
                <td>
                  <b className={s.tone}>{s.name}</b>
                  {s.parallel && <span className="about-tag">并行</span>}
                </td>
                <td>{s.when}</td>
                <td>{s.job}</td>
                <td>{s.out}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <dl className="about-kv">
          <dt>排期</dt>
          <dd>盘前 09:25 · 盘中 09:55 / 10:55 / 12:55 / 13:55（均早于整点交易分析 5 分钟）· 收盘全景 19:05 · 复盘 22:30</dd>
          <dt>消费方</dt>
          <dd>交易 agent 的每轮提示词注入 <code>data/news_brief/latest.json</code> 摘要；19:30 晚间选池消费当日全景</dd>
          <dt>经验闭环</dt>
          <dd>复盘把「事件类型 × 来源」的命中率累计进 <code>lessons.json</code>，样本 ≥3 的类型才作为先验回流——
              只做提示词先验，不做硬闸门</dd>
        </dl>
      </section>

      {/* 04 回测智能体 */}
      <section id="about-04" className="panel about-sec">
        <SecHead id="04" />
        <p className="about-desc" style={{ marginBottom: 10 }}>
          策略不靠人写：<b>Pine 策略库</b>（1001 条）经 AI 逐条转写为 Python 策略，
          先在宿主 worker 里试跑纠错，再进同一套沙箱按真实费率回测。
          转写产物、报错轮次与最终代码全部留档，可复现、可回退。
        </p>
        <div className="about-pipe">
          <span className="about-pipe-node sub sys">Pine 策略库</span>
          <span className="about-pipe-arrow">→</span>
          <span className="about-pipe-node lab">AI 转写 Pyne-Python</span>
          <span className="about-pipe-arrow">→</span>
          <span className="about-pipe-node sub sys">试跑纠错</span>
          <span className="about-pipe-arrow">→</span>
          <span className="about-pipe-node hl lab">沙箱回测</span>
          <span className="about-pipe-arrow">→</span>
          <span className="about-pipe-node trade">排行 / 选股</span>
        </div>
        <dl className="about-kv">
          <dt>单标的回测</dt>
          <dd>选中即同屏：K 线上标出买卖点，叠加指标副图、净值曲线、九项统计与逐笔明细；
              内置模板 6 个（均线交叉 / EMA 趋势 / 唐奇安 / 布林突破 / RSI 反转 / ATR 趋势）</dd>
          <dt>批量跑批</dt>
          <dd>「策略 × 股票池」全组合回测（<code>scripts/lab_batch_backtest.py --pool hs300|sz50|zz500|zz1000|all</code>），
              产物落盘后由前端只读展示——跑批是 CPU 密集作业，不进 API 进程</dd>
          <dt>两个视角</dt>
          <dd><b>策略排行</b>回答「这个池子里哪个策略平均最强」（核心列是跑赢买入持有的比例）；
              <b>单策略选股</b>回答「这个策略该买哪几只」，点任意一行跳到该标的的 K 线与逐笔</dd>
          <dt>回测口径</dt>
          <dd>quantdb 全市场日线 · 不复权 / 前复权 / 后复权三档 ·
              A股多头单向、本金 10 万、95% 仓位、双边 0.05% 费率、1 tick 滑点 ·
              单标的 K 线不足 250 根不入统计</dd>
        </dl>
      </section>

      {/* 05 系统架构 */}
      <section id="about-05" className="panel about-sec">
        <SecHead id="05" />
        <div className="about-arch">
          <div className="about-arch-bar">
            <span className="about-arch-dots"><i /><i /><i /></span>
            SYSTEM MAP · 数据 → 情报 → 执行 → 回测 → 展示
          </div>
          <div className="about-arch-layer agent">
            <div className="about-arch-tag agent">数据层</div>
            <pre className="about-arch-body">{`本机 quantmind 量化仓库（quantdb A股后复权 / quantus 美股 / quantHK+腾讯 港股）
        │  scripts/sync_from_quantmind.py（生产）· bootstrap_data.py（新用户初始化）
        ▼
data/ 价格文件（OHLCV）· 新闻语料 · 跑批产物 —— 与前端共用
        │
        ▼`}</pre>
          </div>
          <div className="about-arch-layer news">
            <div className="about-arch-tag news">情报层 · 新闻智能体</div>
            <pre className="about-arch-body">{`新闻门卫 → 宏观 / 板块个股 / 持仓（三路并行）→ 新闻主编 → 晚间复盘
        │  data/news_brief/latest.json（盘中每 30 分钟一期，注入下方交易提示词）
        ▼`}</pre>
          </div>
          <div className="about-arch-layer trade">
            <div className="about-arch-tag trade">决策与执行 · 交易智能体</div>
            <pre className="about-arch-body">{`交易 Agent（dsh 编排）→ 读行情 → LLM 推理 → 调 MCP 工具下单
        ▲
        │
MCP 工具组（每市场 5 个：trade / price / math / search / memory）
  mcp-us 8100-8104    mcp-cn 8200-8204    mcp-hk 8300-8304
        │
        ▼
风控网关（单笔 ≤ 20% / 日亏熔断 5% / 现金保留 / 行情停更硬闸）→ Broker 抽象层
  sandbox 模拟盘（历史回放成交）· tdx 通达信桥（A股实盘）· qmt 迅投桥（只读复核）
        │
        ▼
position.jsonl + 决策日志落盘 · 交易记忆 market_memory.md（开盘读 / 收盘写）
        │
        ▼`}</pre>
          </div>
          <div className="about-arch-layer lab">
            <div className="about-arch-tag lab">回测层</div>
            <pre className="about-arch-body">{`Pine 策略库 → AI 转写 worker（宿主）→ 沙箱回测 → 批量跑批 data/lab_batch/
        │
        ▼
策略排行 / 单策略选股 / 单标的 K 线级证据
        │
        ▼`}</pre>
          </div>
          <div className="about-arch-layer sys">
            <div className="about-arch-tag sys">展示层</div>
            <pre className="about-arch-body">{`QuantMind 实盘交易栏：本竞技场已整棵迁入，后端为 QuantMind 原生实现
（经 /api/v1/agent-arena 提供服务）；原独立部署的 API 服务与竞技场前端已随平台退役下线`}</pre>
          </div>
        </div>
        <p className="about-desc">
          三个市场各自独立运行：独立 MCP 服务组、独立数据目录、独立资金池与记忆文件；
          引擎层共用同一套 FastAPI 与风控闸门，交易结果即时汇总到排行榜与实况面板。
        </p>
      </section>

      {/* 06 交易闭环 */}
      <section id="about-06" className="panel about-sec">
        <SecHead id="06" />
        <div className="about-flow">
          <span className="about-flow-step hl trade">同步数据</span><span className="about-flow-arrow">→</span>
          <span className="about-flow-step news">情报与简报</span><span className="about-flow-arrow">→</span>
          <span className="about-flow-step trade">LLM 决策</span><span className="about-flow-arrow">→</span>
          <span className="about-flow-step risk">风控校验</span><span className="about-flow-arrow">→</span>
          <span className="about-flow-step agent">执行落盘</span><span className="about-flow-arrow">→</span>
          <span className="about-flow-step lab">收盘复盘</span>
        </div>
        <dl className="about-kv">
          <dt>① 同步数据</dt>
          <dd>本地 quantmind 仓库 → <code>sync_from_quantmind.py</code> → 前 / 后复权价格文件，覆盖前自动备份；新用户可用 <code>bootstrap_data.py</code> 免费接口初始化</dd>
          <dt>② 情报与简报</dt>
          <dd>新闻管线产出结构化简报；盘面情绪、候选池实时价与资金裁剪由候选池快照提供，快照停更会直接告警而不是静默降级</dd>
          <dt>③ LLM 决策</dt>
          <dd>Agent 通过 MCP 工具读行情 / 算指标 / 搜新闻，LLM 自行推理买卖，全程零人工干预</dd>
          <dt>④ 风控校验</dt>
          <dd>确定性闸门（模型不可绕过）：单票 ≤ 剩余额度 20% / 持仓市值 ≤ 权益 ×1.5（分钟级强平守护）
              / T+1 可卖复核 / 涨跌停不接 / 拒单自动延期重放 / 行情停更硬闸；
              风险预算 meta-agent 每日按波动 / 回撤 / 情绪动态定档</dd>
          <dt>⑤ 执行落盘</dt>
          <dd>模拟盘按价格文件 + 滑点重算成交；A股实盘经通达信桥在真实券商通道成交；
              持仓 / 成交 / 决策日志逐日落盘，坏价与越界订单在下单前被拦下</dd>
          <dt>⑥ 收盘复盘</dt>
          <dd>15:35 盘后复盘 agent：逐笔归因 → append_memory 教训沉淀 → 明日预案（watch 双条件单）
              → 新假设登记；次日首轮分析自动注入「昨日复盘要点」</dd>
        </dl>
      </section>

      {/* 07 三市场并行 */}
      <section id="about-07" className="panel about-sec">
        <SecHead id="07" />
        <table className="about-table agent">
          <thead>
            <tr>
              <th>市场</th>
              <th>标的池</th>
              <th>数据源</th>
              <th>MCP 端口</th>
              <th>基准</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><b>美股</b></td>
              <td>NASDAQ 100（102 只）</td>
              <td>quantus 本地仓库</td>
              <td>8100-8104</td>
              <td>NDX100 等权</td>
            </tr>
            <tr>
              <td><b>A股</b></td>
              <td>上证 50（50 只）</td>
              <td>quantdb 后复权</td>
              <td>8200-8204</td>
              <td>SSE50</td>
            </tr>
            <tr>
              <td><b>港股</b></td>
              <td>恒指成分</td>
              <td>quantHK + 腾讯补齐</td>
              <td>8300-8304</td>
              <td>—</td>
            </tr>
          </tbody>
        </table>
        <p className="about-desc">
          同一模型在三个市场可以展现完全不同的交易风格；排行榜不与基准比谁涨得多，
          而是比谁在同一条起跑线上少犯错。
        </p>
      </section>

      {/* 08 赛制与实盘通道 */}
      <section id="about-08" className="panel about-sec">
        <SecHead id="08" />
        <p className="about-desc">
          <b>同池竞技</b>：同市场内所有模型使用完全相同的行情数据、工具集与提示词框架，初始资金一致——
          差异只来自模型本身的判断。当前在跑 <b>DeepSeek V4 Flash × V4 Pro × GLM 5.3 Flash</b> 零样本对决，
          更多模型可在 <code>configs/*.json</code> 一键启用。
        </p>
        <p className="about-desc" style={{ marginTop: 8 }}>
          <b>实盘分账（A股）</b>：2026-08-31 起接入通达信桥（Windows 8550）真实券商通道。
          每 agent 分配 <b>¥10 万虚拟额度</b>，按模型独立记账（买入分配 / 卖出释放），与模拟盘并行运行，
          盈亏口径独立展示。
        </p>
        <p className="about-desc" style={{ marginTop: 8 }}>
          <b>第二观测点</b>：2026-09 起接入迅投 QMT 只读桥，对实盘账户的资金与持仓做独立复核——
          只读不下单，与通达信桥口径互相印证；两条链路任何一条掉线，总控页会明确报出是哪一条。
        </p>
      </section>

      {/* 09 全天候循环 */}
      <section id="about-09" className="panel about-sec">
        <SecHead id="09" />
        <div className="about-tl">
          {TL.map((row) => (
            <div key={row.t} className="about-tl-row">
              <span className={`about-tl-t ${row.tone}`}>{row.t}</span>
              <span className="about-tl-c">{row.c}</span>
            </div>
          ))}
        </div>
        <p className="about-desc" style={{ marginTop: 10 }}>
          自我进化闭环：假设提出 → 登记 → 复测 → 带胜率证据回流提示词；复盘教训次日自动注入；
          分歧自动仲裁；预算按市场状态动态收紧。详见仓库
          <code> docs/AGENT_ROADMAP.md · PIPELINE_UPGRADE.md · MAINTENANCE_PLAN.md</code>。
        </p>
      </section>

      {/* 10 口径与透明 */}
      <section id="about-10" className="panel about-sec">
        <SecHead id="10" />
        <p className="about-desc" style={{ marginBottom: 8 }}>
          每次成交按 <b>双边万 3 费率 + 滑点</b>重算成交价与费用，累计费用与费用占比在排行榜公开。
          手续费是超额收益的隐形杀手——我们把它摆到台面上。
        </p>
        <table className="about-table sys">
          <thead>
            <tr>
              <th>指标</th>
              <th>口径</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><b>Sharpe</b></td>
              <td>日收益序列 mean / std × √252（不足 2 个交易日显示 0）</td>
            </tr>
            <tr>
              <td><b>胜率 / 盈亏比</b></td>
              <td>按持仓记录 FIFO 重建逐笔平仓，成交价与费用按价格文件重算</td>
            </tr>
            <tr>
              <td><b>最大回撤</b></td>
              <td>净值峰谷最大跌幅（绝对值）</td>
            </tr>
            <tr>
              <td><b>持仓天数</b></td>
              <td>首买到最后持有的自然日跨度</td>
            </tr>
            <tr>
              <td><b>跑赢持有</b></td>
              <td>回测口径：策略收益 − 买入持有；批量视图展示全池中跑赢的标的占比</td>
            </tr>
          </tbody>
        </table>
        <p className="about-desc">
          <b>决策透明</b>：每个 Agent 每天的完整决策链——观察、推理、工具调用、最终指令——全部落盘并在
          「模型对话」中可回溯；新闻简报、候选池快照与闸门判定同样留档。
          模型是在认真分析还是在掷骰子，看一眼日志就知道。
        </p>
      </section>

      {/* 11 扩展开发 */}
      <section id="about-11" className="panel about-sec">
        <SecHead id="11" />
        <div className="about-desc" style={{ lineHeight: 2 }}>
          <p><b>新增智能体</b>（详见仓库 <code>docs/AGENT_GUIDE.md</code>）：</p>
          <ul style={{ paddingLeft: 20, margin: '4px 0' }}>
            <li><b>竞技 / 回放</b>：<code>configs/*_config.json</code> 的 models 加一项并 <code>enabled: true</code>（可选自定义策略类）</li>
            <li><b>A股实盘分账</b>：<code>configs/astock_config.json</code> 加 enabled 模型 + <code>.env</code> 模型 Key；系统自动配 ¥10 万虚拟额度，风控 / 拒单重放全继承</li>
            <li><b>dsh 技能包</b>：<code>dsh/skills/&lt;技能名&gt;/SKILL.md</code>（frontmatter 写触发词），容器 bind mount 即生效</li>
            <li><b>回测策略</b>：Pine 策略入库后由转写 worker 自动处理；内置模板见 <code>backend/services/lab_strategies/</code></li>
          </ul>
          <p>
            <b>系统技能包总览</b>（17 个技能：复盘 / 选股 / 深度研究 / 情绪 / 券商通道…）→{' '}
            <code>docs/SKILLPACK.md</code> · 完整部署 runbook → <code>docs/DEPLOYMENT.md</code>
          </p>
        </div>
      </section>

      {/* 12 免责声明 */}
      <section id="about-12" className="panel about-sec">
        <SecHead id="12" />
        <p className="about-desc dim" style={{ lineHeight: 1.9 }}>
          本竞技场为研究平台。模拟路径：全部交易为历史行情回放，不涉及任何真实资金。
          实盘路径（A股）：经通达信桥在真实券商通道成交，但资金为分账虚拟额度（每 agent ¥10 万），不投入真实资金。
          历史表现不代表未来收益，本平台不构成任何投资建议。
        </p>
      </section>
    </div>
  );
}
