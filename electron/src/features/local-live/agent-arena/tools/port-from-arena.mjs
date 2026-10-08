#!/usr/bin/env node
/**
 * arena → QuantMind 实盘栏：页面移植器（**可重跑**）
 *
 * 用途：把 quant-Trader 的 arena 前端（用户自研的交易智能体看板）整棵子树搬进
 * QuantMind 的「实盘交易」栏。arena 那边还在持续改，所以这不是一次性 cp：
 * 重跑本脚本即可把最新代码再同步一遍，**所有本地改写都写在脚本里**（见 PATCHES），
 * 不会因为「谁手动改过哪一行」而丢。
 *
 * 用法：
 *   在仓库根执行  node electron/src/features/local-live/agent-arena/tools/port-from-arena.mjs
 *   加 --dry-run 只打印将要搬运的文件清单，不写盘。
 *   源目录可用 ARENA_SRC 覆盖（默认 /home/zbox/quant-Trader/arena/src）。
 *
 * 三件确定性的事：
 *   1) 按**相对 import 闭包**搬运（从 6 个入口页面出发），arena/src 之外零引用，
 *      所以闭包 = 完整依赖；未解析到的相对路径会报错（防silently少搬文件）。
 *   2) CSS 全部**作用域化**到 `.qm-arena-root`（含 :root/body/html/* 与 @keyframes 改名）。
 *      arena 是 neo-brutalism 自绘样式表，直接灌进 QuantMind 会污染全局（antd + tailwind）。
 *   3) 需要与宿主环境对齐的少数文件由 PATCHES / overrides 覆盖，逐条登记在案。
 *   4) 函数式 setState（`setX(prev => ...)`）统一裹一层 `asUpdater()`：本仓 tsc 把
 *      `Dispatch<SetStateAction<S>>` 实例化成 `(value: S) => void`（丢函数分支），
 *      arena 那边写法正常、搬过来必报 TS2345。详见 arena/reactCompat.ts 的注释。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import postcss from 'postcss';
import {
  REACT_COMPAT_REL,
  REACT_COMPAT_SRC,
  compatSpec,
  ensureCompatImport,
  wrapFunctionalSetters,
} from './set-wrap.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DEST = path.resolve(HERE, '..', 'arena'); // 搬完的代码落在这里（arena/src 的镜像）
const SRC = process.env.ARENA_SRC || '/home/zbox/quant-Trader/arena/src';
const OVERRIDES = path.resolve(HERE, 'overrides');
const ARENA_REPO = path.resolve(SRC, '..', '..');
/** JSX 上挂的类名（不含点） */
const ROOT_CLASS = 'qm-arena-root';
/** CSS 里用的选择器（含点）—— 少了这个点，整个样式表会静默失效 */
const ROOT_SEL = `.${ROOT_CLASS}`;
const DRY = process.argv.includes('--dry-run');

/** 要搬的入口：用户点名的五个页面 + 总控里点模型名下钻的详情页 + 全局样式表 */
const ENTRIES = [
  'pages/Live.tsx',
  'pages/MarketLab.tsx',
  'pages/Control.tsx',
  'pages/DataPlatform.tsx',
  'pages/About.tsx',
  'pages/ModelDetail.tsx',
  'styles/globals.css',
];

/**
 * 用本地替身覆盖的文件。
 *   Navbar.tsx —— arena 的整站导航条（Link/NavLink 指向 arena 自己的路由），
 *   移植后导航由 QuantMind 交易台侧栏承担，但 Live 页面还在 import 它的
 *   `MarketSwitcher`（cn/hk/us 三个 pill），所以只保留那一个导出。
 *
 * 替身模板在 tools/overrides/ 下带 `.tpl` 后缀（`Navbar.tsx.tpl`）：它按**落位后**的
 * 相对路径写 import，放在 tools/ 里就地跑 tsc 会解析不到、白报错。脚本复制时去掉 .tpl。
 */
const OVERRIDE_FILES = new Set(['components/Navbar.tsx']);

/**
 * 逐文件的确定性改写。每条 `find` 必须命中，否则报错 —— 宁可炸也不要静默搬半截。
 * （arena 改了这些锚点就该一起更新本表，这正是可重跑的意义。）
 */
const PATCHES = [
  {
    file: 'api/client.ts',
    why: 'baseURL 由 nginx 同源反代 /api 改为 QuantMind 后端代理 /api/v1/agent-arena；前端补 QuantMind Bearer',
    find: `import axios from 'axios';\n\nexport const api = axios.create({ baseURL: '/api', timeout: 20000 });`,
    replace: `import axios from 'axios';\nimport { SERVICE_ENDPOINTS } from '../../../../../config/services';\nimport { authService } from '../../../../auth/services/authService';\n\n/**\n * 原 arena：baseURL '/api'，token 由 nginx 反代注入（浏览器不持凭证）。\n * 移植进 QuantMind 实盘栏后改走 QuantMind 后端代理 /api/v1/agent-arena，\n * 上游 token 由后端注入；前端只带 QuantMind 自己的 Bearer。\n */\nexport const api = axios.create({ timeout: 20000 });\n\napi.interceptors.request.use((config) => {\n  config.baseURL = \`\${SERVICE_ENDPOINTS.API_GATEWAY}/agent-arena\`;\n  const token = authService.getAccessToken();\n  if (token) {\n    (config.headers as Record<string, string>).Authorization = \`Bearer \${token}\`;\n  }\n  return config;\n});`,
  },
  {
    file: 'pages/Live.tsx',
    why: 'useSearchParams 改走宿主内存态（不写交易台的 URL，避免与 ?tab= 深链互相踩）',
    find: `import { useSearchParams } from 'react-router-dom';`,
    replace: `import { useSearchParams } from '../arenaRouter';`,
  },
  {
    file: 'pages/Control.tsx',
    why: 'useNavigate 指向 arena 自己的路由（/model/:market/:agent），改成宿主的栏内下钻',
    find: `import { useNavigate, useSearchParams } from 'react-router-dom';`,
    replace: `import { useSearchParams } from '../arenaRouter';\nimport { useArenaNav } from '../arenaNav';`,
  },
  {
    file: 'pages/Control.tsx',
    why: '同上：nav(...) 由宿主 context 接管（调用点一行不改）',
    find: `  const nav = useNavigate();`,
    replace: `  const nav = useArenaNav();`,
  },
  {
    file: 'pages/Control.tsx',
    why: '三市场表格的 agent 列改用应用自己的短名（shortName，实况页/台账/详情页同款）：宿主里三列并排后每列只有 437px，全名 deepseek-v4-flash 单列要吃 125px，会把「收益率/回放截止」挤出卡外（见 qm-arena-overrides.css 顶部注释）。全名挂 title 保留',
    find: `import { fmtAgo, fmtDateTime } from '../utils/datetime';`,
    replace: `import { fmtAgo, fmtDateTime } from '../utils/datetime';\nimport { shortName } from '../components/ModelCard';`,
  },
  {
    file: 'pages/Control.tsx',
    why: '同上：该列渲染短名 + title 全名',
    find: `                          <td style={{ fontWeight: 700 }}>{r.name}</td>`,
    replace: `                          <td style={{ fontWeight: 700 }} title={r.name}>{shortName(r.name)}</td>`,
  },
  {
    file: 'components/ModelCard.tsx',
    why: '品牌色 --model-accent 由「仅选中时注入」改为**恒注入**：宿主打磨层（qm-arena-theme.css）要靠它给每张卡上左侧竖条/logo 底色（未选中时也得有值）。选中态的 inline 边框/底色/投影逻辑不变',
    find: `      style={\n        selected\n          ? ({\n              borderColor: accent,\n              background: \`\${accent}12\`,\n              boxShadow: \`3px 3px 0 \${accent}40\`,\n              '--model-accent': accent,\n            } as CSSProperties)\n          : undefined\n      }`,
    replace: `      style={\n        {\n          // 品牌色恒注入（原版只在选中时注入）：宿主打磨层要靠它给**每张**卡上品牌色\n          // （左侧竖条 / logo 底色 / 选中角标），未选中时也得有值\n          '--model-accent': accent,\n          ...(selected\n            ? {\n                borderColor: accent,\n                background: \`\${accent}12\`,\n                boxShadow: \`3px 3px 0 \${accent}40\`,\n              }\n            : {}),\n        } as CSSProperties\n      }`,
  },
  {
    file: 'pages/ModelDetail.tsx',
    why: '详情页由路由参数改为宿主传入的 props（栏内下钻，不再注册路由）',
    find: `import { Link, useParams } from 'react-router-dom';\n`,
    replace: ``,
  },
  {
    file: 'pages/ModelDetail.tsx',
    why: '同上：签名收 props',
    find: `export default function ModelDetail() {`,
    replace: `export interface ModelDetailProps {\n  market?: MarketId;\n  agent?: string;\n  /** 返回列表（原先回「模型排行榜」，排行榜未移植，改为回本栏） */\n  onBack: () => void;\n}\n\nexport default function ModelDetail({ market = 'cn', agent = '', onBack }: ModelDetailProps) {`,
  },
  {
    file: 'pages/ModelDetail.tsx',
    why: '同上：useParams 取值删掉（改由 props）',
    find: /^[ \t]*const \{ market = 'cn', agent = '' \} = useParams\(\);\n/m,
    replace: ``,
  },
  {
    file: 'pages/ModelDetail.tsx',
    why: '返回按钮：原先跳排行榜（未移植），改为回本栏',
    find: /<Link to=\{`\/leaderboard`\} className="mdp-back">\s*\n\s*← 排行榜\s*\n\s*<\/Link>/m,
    replace: `<button type="button" className="mdp-back" onClick={onBack}>\n          ← 返回\n        </button>`,
  },

  // ===== A6（2026-09-30）死路处置 + 2026-10-08 富途复活 =====
  // ① futu 账户通道**已恢复**（2026-10-08）：自建 futu-opend 容器 + QM 原生
  //    /api/v1/agent-arena/futu/*；A6 的「直接失败」桩已全删，另加 reshapeFutuAccount
  //    抽提、closed 双形状容错、place/cancel/orders 三个写读函数与最小下单面板接线。
  // ①' ibkr 账户通道仍**未迁移**：取数函数保持直接失败（不发请求、杜绝 404 噪声），
  //    面板保留并显示「通道已下线」。
  // ② 触发型能力（POST /live/analyze、POST /news/analyze）已复活走 QM /analysis/*。
  // ③ baymax / 8091 / 8092 文案残留全清（验收标准 §6）。
  {
    file: 'api/client.ts',
    why: 'A6-③ 头注释去「FastAPI 8091 / nginx token 注入」（原独立 API 服务已退役）',
    find: `/** Quant Agent Trader 数据层：对接 FastAPI 8091 现有端点。\n *  生产环境经 nginx 同源反代（token 由 nginx 注入），浏览器无需持有 token；\n *  dev 模式直连 vite proxy 到 127.0.0.1:8091（api 鉴权未配置时直接可用）。\n */`,
    replace: `/** 实盘栏（竞技场）数据层：对接 QuantMind 后端的 /api/v1/agent-arena 代理。\n *  生产与 dev 同路径：请求经下方 axios 拦截器指向 QuantMind 后端，\n *  前端只带自己的 Bearer（原独立 API 服务与 nginx token 注入已随平台退役）。\n */`,
  },
  {
    file: 'api/client.ts',
    why: '富途恢复（2026-10-08）：注释改「自建 OpenD 容器」口径（原 BayMax 出处已退役）',
    find: `// ---------- 港股富途实盘账户（BayMax backend /api/futu/* 直连 OpenD 网关） ----------\n// 富途模拟/实盘账户；futu 原始 positions 是 {code: {...}} dict，reshape 成 cn LiveAccount\n// 同一 shape，Live 页持仓/实盘 tab 复用 cn 渲染逻辑（排版与 A 股一致）。`,
    replace: `// ---------- 港股富途实盘账户（自建 FutuOpenD 容器，经 /api/v1/agent-arena/futu/* 直连） ----------\n// 富途模拟/实盘账户；futu 原始 positions 是 {code: {...}} dict，reshape 成 cn LiveAccount\n// 同一 shape，Live 页持仓/实盘 tab 复用 cn 渲染逻辑（排版与 A 股一致）。`,
  },
  {
    file: 'api/client.ts',
    why: '富途恢复：reshape 抽成导出纯函数（单卡/双卡共用；边界口径对齐 reshapeQmtAccount——price=0 不给盈亏段、cost<0 只留绝对盈亏）',
    find: /export const fetchFutuAccount = async[\s\S]*?\n\};/,
    replace: `/** 纯函数：富途原始账户 → 面板 LiveAccount（export 供 vitest；单卡与双卡共用）。
 *  边界口径与 reshapeQmtAccount 对齐，两处富途侧约定不能想当然：
 *  - price=0 是「拿不到价」→ 不给盈亏段（0 值盈利会伪造「平盘」）；
 *  - cost<0 真实存在（摊薄成本法）→ 绝对盈亏仍有效，百分比无意义（(P-C)/C 在 C<0 时符号颠倒）→ pnl_pct 留 0。 */
export const reshapeFutuAccount = (
  raw: FutuAccountRaw | undefined,
  env: string,
): LiveAccount => {
  const channel = \`futu-\${env.toLowerCase()}\`;
  if (!raw) return { asset: 0, positions: [], channel_used: channel };
  const positions: LivePosition[] = Object.entries(raw.positions ?? {})
    .filter(([, p]) => Number(p.volume) > 0)
    .map(([code, p]) => {
      const cost = Number(p.cost) || 0;
      const last = Number(p.price) || 0;
      const vol = Number(p.volume) || 0;
      const hasPrice = last > 0;
      const hasPnl = hasPrice && cost !== 0;
      const hasPct = hasPnl && cost > 0;
      return {
        stock_code: code,
        name: p.name || code,
        cost_price: cost,
        total_volume: vol,
        available_volume: Number(p.available_volume) || 0,
        last_price: last,
        position_value: Number(p.market_value) || (hasPrice ? last * vol : cost * vol),
        pnl_pct: hasPct ? +(((last - cost) / cost) * 100).toFixed(2) : 0,
        pnl: hasPnl ? +((last - cost) * vol).toFixed(2) : 0,
        buy_time: '',
      };
    });
  return { asset: Number(raw.total_asset) || 0, positions, channel_used: channel };
};

export const fetchFutuAccount = async (env = 'SIMULATE'): Promise<LiveAccount> => {
  const res = await api.get('/futu/account', { params: { env } });
  // 后端统一信封 {success, data:{...}}；account 在 data.data
  const raw = (res.data?.data ?? res.data) as FutuAccountRaw | undefined;
  return reshapeFutuAccount(raw, env);
};`,
  },
  {
    file: 'api/client.ts',
    why: '富途恢复：双卡 fetch 改调导出的 reshapeFutuAccount（原内部闭包删除，单卡/双卡同一口径）',
    find: /export const fetchFutuAccountBoth = async[\s\S]*?\n\};/,
    replace: `export const fetchFutuAccountBoth = async (): Promise<{
  real: LiveAccount;
  simulate: LiveAccount;
}> => {
  const res = await api.get('/futu/account-both');
  const raw = (res.data?.data ?? res.data) as
    | { real?: FutuAccountRaw; simulate?: FutuAccountRaw }
    | undefined;
  return {
    real: reshapeFutuAccount(raw?.real, 'real'),
    simulate: reshapeFutuAccount(raw?.simulate, 'simulate'),
  };
};`,
  },
  {
    file: 'api/client.ts',
    why: 'A6-③ 迅投 QMT 注释去 BayMax（QMT 通道本身在，经 /qmt/account 直连 Redis 桥）',
    find: `// ---------- 迅投 QMT（A股，只读；BayMax backend /api/qmt/account 直连 Redis 桥） ----------`,
    replace: `// ---------- 迅投 QMT（A股，只读；/qmt/account 直连 Redis 桥） ----------`,
  },
  {
    file: 'api/client.ts',
    why: '富途恢复（2026-10-08）：市场感知账户路由注释（hk 已恢复、us 仍下线）',
    find: `// 市场感知实盘账户：cn 走通达信桥 /live/account；hk 走富途；us 走 IBKR`,
    replace: `// 市场感知实盘账户：cn 走通达信桥 /live/account；hk 走富途（自建 OpenD）；us 走 IBKR（通道已下线）`,
  },
  {
    file: 'api/client.ts',
    why: '富途恢复：订单历史注释回「直连」口径（去 BayMax 出处）',
    find: `// ---------- 港股富途订单历史（BayMax backend /api/futu/orders 直连 OpenD） ----------\n// order_list_query → LiveTradeLog（同 shape，复用 cn 成交渲染）。只取 dealt_qty>0 已成交。`,
    replace: `// ---------- 港股富途订单历史（自建 FutuOpenD 容器，经 /futu/orders 直连） ----------\n// order_list_query → LiveTradeLog（同 shape，复用 cn 成交渲染）。只取 dealt_qty>0 已成交。`,
  },
  {
    file: 'api/client.ts',
    why: '富途恢复：市场感知成交路由注释（hk 已恢复、us 仍下线）',
    find: `// 市场感知成交：cn 走通达信 live_trade 日志 /live/trades；hk 走富途订单历史；us 走 IBKR 委托`,
    replace: `// 市场感知成交：cn 走通达信 live_trade 日志 /live/trades；hk 走富途订单历史；us 走 IBKR 委托（通道已下线）`,
  },
  {
    file: 'api/client.ts',
    why: '富途恢复：已平仓注释回「直连」口径（去 BayMax 出处）',
    find: `// ---------- 港股富途已平仓（BayMax backend /api/futu/closed 直连 OpenD） ----------\n// position_list_query 已平仓行（qty==0, realized_pl!=0）→ Live「已完成」tab 港股面板。`,
    replace: `// ---------- 港股富途已平仓（自建 FutuOpenD 容器，经 /futu/closed 直连） ----------\n// position_list_query 已平仓行（qty==0, realized_pl!=0）→ Live「已完成」tab 港股面板。`,
  },
  {
    file: 'api/client.ts',
    why: '下单面板：FutuOrderRaw 导出（面板「当前委托」列表复用同一形状）',
    find: `interface FutuOrderRaw {`,
    replace: `export interface FutuOrderRaw {`,
  },
  {
    file: 'api/client.ts',
    why: '富途恢复 + 下单面板（2026-10-08）：closed 双形状容错；追加 place/cancel/orders 三个写读函数（QM 本地新增，后端 /futu/* 契约）',
    find: /export const fetchFutuClosed = async[\s\S]*?\n\};/,
    replace: `export const fetchFutuClosed = async (env = 'SIMULATE'): Promise<FutuClosedRow[]> => {
  const res = await api.get('/futu/closed', { params: { env } });
  const data = (res.data?.data ?? res.data) as
    | { closed?: FutuClosedRow[] }
    | FutuClosedRow[]
    | undefined;
  // 双形状容错：本仓后端给 {closed:[...]}（旧栈解包成裸数组是 bug）；两种都认。
  return Array.isArray(data) ? data : (data?.closed ?? []);
};

// ---------- 富途下单/撤单/委托查询（QM 本地新增，2026-10-08：最小下单面板用） ----------
// 后端契约（/api/v1/agent-arena/futu/*）：
//   place  → {success, data:{success, order_id, status, filled_quantity, filled_price, message}}
//   cancel → {success, data:{success, message}}
//   orders → {success, data:{orders:[FutuOrderRaw]}}
// REAL 两条 fail-closed：闸门关 → 403 real_trading_disabled；未配置解锁 → 409 futu_unlock_required。
export interface FutuOrderInput {
  code: string;
  price: number;
  quantity: number;
  order_type: 'NORMAL' | 'MARKET';
  trd_side: 'BUY' | 'SELL';
}

export interface FutuPlaceResult {
  success: boolean;
  order_id: string;
  status: string;
  filled_quantity: number;
  filled_price: number;
  message: string;
}

export const placeFutuOrder = async (
  env: 'REAL' | 'SIMULATE',
  order: FutuOrderInput,
): Promise<FutuPlaceResult> => {
  const res = await api.post('/futu/place', { env, market: 'HK', order });
  return (res.data?.data ?? res.data) as FutuPlaceResult;
};

export const cancelFutuOrder = async (
  env: 'REAL' | 'SIMULATE',
  orderId: string,
): Promise<{ success: boolean; message: string }> => {
  const res = await api.post('/futu/cancel', { env, market: 'HK', order_id: orderId });
  return (res.data?.data ?? res.data) as { success: boolean; message: string };
};

/** 当日订单原始行（含未成交/已撤）——下单面板「当前委托」列表用。 */
export const fetchFutuOrders = async (
  env: 'REAL' | 'SIMULATE' = 'SIMULATE',
): Promise<FutuOrderRaw[]> => {
  const res = await api.get('/futu/orders', { params: { env } });
  const data = (res.data?.data ?? res.data) as { orders?: FutuOrderRaw[] } | undefined;
  return data?.orders ?? [];
};`,
  },
  {
    file: 'api/client.ts',
    why: '分析触发复活（2026-10-08）：注释改走 QM 原生 /analysis/* 台账',
    find: `// ---------- 手动触发分析（对话 tab「立即分析」按钮） ----------`,
    replace: `// ---------- 手动触发分析（对话 tab「立即分析」按钮；2026-10-08 复活：走 QM 原生 /analysis/* 台账，宿主 cron worker 消费） ----------`,
  },
  {
    file: 'api/client.ts',
    why: 'triggerLiveAnalysis → QM 原生 /analysis/trigger（type=live）',
    find: `export const triggerLiveAnalysis = (agents: 'all' | string[]) =>\n  unwrap<AnalysisJob>(api.post('/live/analyze', { agents }));`,
    replace: `export const triggerLiveAnalysis = (agents: 'all' | string[]) =>\n  unwrap<AnalysisJob>(api.post('/analysis/trigger', { type: 'live', agents }));`,
  },
  {
    file: 'api/client.ts',
    why: 'triggerNewsAnalysis → QM 原生 /analysis/trigger（type=news）',
    find: `export const triggerNewsAnalysis = () =>\n  unwrap<AnalysisJob>(api.post('/news/analyze'));`,
    replace: `export const triggerNewsAnalysis = () =>\n  unwrap<AnalysisJob>(api.post('/analysis/trigger', { type: 'news' }));`,
  },
  {
    file: 'api/client.ts',
    why: 'fetchAnalysisJobs → QM 原生 /analysis/jobs',
    find: `export const fetchAnalysisJobs = (limit = 5) =>\n  unwrap<AnalysisJob[]>(api.get('/live/analyze', { params: { limit } }));`,
    replace: `export const fetchAnalysisJobs = (limit = 5) =>\n  unwrap<AnalysisJob[]>(api.get('/analysis/jobs', { params: { limit } }));`,
  },
  {
    file: 'api/client.ts',
    why: 'A6-① 美股实盘注释 → 通道已下线',
    find: `// ---------- 美股实盘（IBKR Gateway，ib_insync；凭据 config/brokers.json ib） ----------`,
    replace: `// ---------- 美股实盘（通道已下线：盈透 IB Gateway 未随平台迁移，函数直接失败进占位分支） ----------`,
  },
  {
    file: 'api/client.ts',
    why: 'A6-① fetchIbkrAccount 函数体 → 直接失败',
    find: /export const fetchIbkrAccount = async[\s\S]*?\n\};/,
    replace: `export const fetchIbkrAccount = async (): Promise<RealAccount> => {\n  throw new Error('通道已下线：盈透证券 IB Gateway 未迁移');\n};`,
  },
  {
    file: 'api/client.ts',
    why: 'A6-① fetchIbkrOrders → 直接失败（不发请求）',
    find: /export const fetchIbkrOrders = async[\s\S]*?\n\};/,
    replace: `export const fetchIbkrOrders = async (): Promise<LiveTradeLog[]> => {\n  throw new Error('通道已下线：盈透证券 IB Gateway 未迁移');\n};`,
  },
  {
    file: 'pages/Live.tsx',
    why: 'A6-③ 全局错误横幅去「baymax-api(8091) / ui-arena(8092)」',
    find: `        请确认 baymax-api(8091) 与 ui-arena(8092) 容器已启动`,
    replace: `        实盘栏数据不可达：请确认 QuantMind 后端（api 容器）已启动，然后刷新重试`,
  },
  {
    file: 'pages/Live.tsx',
    why: '富途恢复（2026-10-08）：实盘账户区块注释（hk 已恢复、us 仍下线）',
    find: `  // ---------- 实盘账户（A股：通达信桥 /live/account；港股：富途 /api/futu/account 直连 OpenD） ----------`,
    replace: `  // ---------- 实盘账户（A股：通达信桥 /live/account；港股：富途（自建 OpenD，经 /futu/* 代理）；us：IBKR 已下线） ----------`,
  },
  {
    file: 'pages/Live.tsx',
    why: '下单面板（QM 本地新增 2026-10-08）：import HkOrderPanel（生成物外的本地组件，勿由 arena 覆盖）',
    find: `import RealAccountPanel from '../components/RealAccountPanel';`,
    replace: `import RealAccountPanel from '../components/RealAccountPanel';\nimport HkOrderPanel from '../../hk-order-panel/HkOrderPanel';`,
  },
  {
    file: 'pages/Live.tsx',
    why: '下单面板：实盘 tab 港股分支在账户双卡后追加最小下单/撤单面板',
    find: `    if (tab === 'real') {\n      return (\n        <RealAccountPanel\n          market={market}\n          currency={meta.currency}\n          futuBoth={futuBoth.data}\n          futuError={futuBoth.error}\n          futuLoading={futuBoth.loading}\n          channel={realChannel}\n          onChannel={setRealChannel}\n        />\n      );\n    }`,
    replace: `    if (tab === 'real') {\n      return (\n        <>\n          <RealAccountPanel\n            market={market}\n            currency={meta.currency}\n            futuBoth={futuBoth.data}\n            futuError={futuBoth.error}\n            futuLoading={futuBoth.loading}\n            channel={realChannel}\n            onChannel={setRealChannel}\n          />\n          {/* QM 本地新增（2026-10-08）：港股最小下单/撤单面板（含「当前委托」撤单） */}\n          {market === 'hk' && <HkOrderPanel />}\n        </>\n      );\n    }`,
  },
  {
    file: 'pages/Live.tsx',
    why: '对话 tab「立即分析」按钮提示语改 QM 口径（复活后只出观点，不下单）',
    find: `                  <button\n                    className={\`analyze-trigger \${analyzeState === 'busy' ? 'busy' : ''}\`}\n                    disabled={analyzeState === 'busy'}\n                    onClick={() => void runManualAnalysis()}\n                    title="立即跑一轮完整分析（交易时段内与整点同权，可真下单；盘外只出决策）"\n                  >\n                    {analyzeState === 'busy' ? '提交中…' : '⚡ 立即分析'}\n                  </button>`,
    replace: `                  <button\n                    className={\`analyze-trigger \${analyzeState === 'busy' ? 'busy' : ''}\`}\n                    disabled={analyzeState === 'busy'}\n                    onClick={() => void runManualAnalysis()}\n                    title="立即跑一轮模型分析（宿主队列，约 1 分钟内开跑；只出观点，不下单）"\n                  >\n                    {analyzeState === 'busy' ? '提交中…' : '⚡ 立即分析'}\n                  </button>`,
  },
  {
    file: 'components/RealAccountPanel.tsx',
    why: 'A6-① 美股 IBKR 卡头状态 →「通道已下线」',
    find: `{ibkr.data ? '盈透证券 Gateway' : ibkr.loading ? '加载中…' : '未连接'}`,
    replace: `{ibkr.data ? '盈透证券 Gateway' : ibkr.loading ? '加载中…' : '通道已下线'}`,
  },
  {
    file: 'components/RealAccountPanel.tsx',
    why: 'A6-① 美股 IBKR 卡体降级文案 →「通道已下线」',
    find: `                : 'IBKR 未连接：本机 IB Gateway 未运行或未登录，账户数据不可用；美股比赛仍按本地数据集回放正常进行。'}`,
    replace: `                : '通道已下线：盈透证券 IB Gateway 未随平台迁移，账户数据不可用；美股比赛仍按本地数据集回放正常进行。'}`,
  },
  {
    file: 'components/RealAccountPanel.tsx',
    why: 'A6-① 美股说明块 → 通道已下线口径',
    find: `            美股实盘经 IBKR Gateway（ib_insync）只读对照；接入凭据在「交易所设置 → 盈透证券(IB)」。\n            该账户不参与模拟盘决策，仅作真金实盘的状态对照。`,
    replace: `            盈透证券通道已随原平台退役下线（未迁移）；本面板保留为历史入口。\n            美股比赛仍按本地数据集回放正常进行。`,
  },
  {
    file: 'components/RealAccountPanel.tsx',
    why: 'A6-③ 通达信桥通道说明去 BayMax',
    find: `          ? '通达信桥：BayMax 实盘决策的下单与成交回报通道（各模型 ¥10 万分账）。'`,
    replace: `          ? '通达信桥：QuantMind 实盘决策的下单与成交回报通道（各模型 ¥10 万分账）。'`,
  },
  {
    file: 'components/RealAccountPanel.tsx',
    why: '日终账本曲线改金额口径（2026-10-08）。pct 以「首个记录日」=100 归一，而首日 08-13 是中途起点（账户在建账前已在交易）：曲线读成「自 08-13 亏 6.1%」，与同屏卡片（总资产 ¥918,398 / 日收益 +0.05%）、桥「累计盈亏」互相打架。实账总资产本身就是唯一连续的真实金额序列，改 dollar 后金额轴直接可读、曲线右端=卡片总资产（虚账曲线的 ¥10 万起点归一保留在净值图，两边口径不再混淆）',
    find: `            <EquityChart lines={curLedgerLine} benchmark={null} currency="¥" mode="pct" timeRange="all" height={180} />`,
    replace: `            <EquityChart lines={curLedgerLine} benchmark={null} currency="¥" mode="dollar" timeRange="all" height={180} />`,
  },
  {
    file: 'components/EquityChart.tsx',
    why: '日终账本线（real-ledger-*）的 tooltip 尾注（2026-10-08）：该线是实盘账户金额序列，不带前缀会落到「虚拟净值（¥10万起步）」兜底文案（对 ¥91.8 万实账是假的）；dollar 口径下 ±% 是「自首个记录日」，把窗口写清楚',
    find: `        {isBench ? '基准指数' : hover.label === '总账户' ? '通达信桥实时总资产' : hover.label === '分账合计' ? '分账合计净值（3 agent）' : '虚拟净值（¥10万起步）'}`,
    replace: `        {isBench ? '基准指数' : hover.label === '总账户' ? '通达信桥实时总资产' : hover.label === '分账合计' ? '分账合计净值（3 agent）' : hover.id.startsWith('real-ledger-') ? '实盘账户日终总资产 · 涨跌自首个记录日' : '虚拟净值（¥10万起步）'}`,
  },
  {
    file: 'utils/channelStatus.ts',
    why: 'A6-③ 通道状态注释去 BayMax',
    find: `/** 通达信桥：BayMax 实盘执行通道（下单 + 成交回报都在这里） */`,
    replace: `/** 通达信桥：QuantMind 实盘执行通道（下单 + 成交回报都在这里） */`,
  },
  {
    file: 'utils/channelStatus.ts',
    why: 'A6-① ibkrChannel 注释与文案 →「已下线」',
    find: `/** 美股：盈透证券 IB Gateway（ib_insync 直连） */\nexport function ibkrChannel(acct: { total_asset: number } | null): ChannelProbe {\n  const ok = !!acct;\n  return {\n    key: 'ibkr',\n    label: 'IBKR Gateway',\n    role: '美股执行通道（盈透证券）',\n    state: ok ? 'ok' : 'off',\n    stateText: ok ? '在线' : '离线',\n    lines: [\n      { k: '总资产', v: ok ? money(acct.total_asset, '$') : '未连接', tone: ok ? 'ok' : 'off' },\n      { k: '接入方式', v: 'ib_insync → 本机 Gateway' },`,
    replace: `/** 美股：盈透证券 IB Gateway（通道已下线：未随平台迁移；保留占位展示） */\nexport function ibkrChannel(acct: { total_asset: number } | null): ChannelProbe {\n  const ok = !!acct;\n  return {\n    key: 'ibkr',\n    label: 'IBKR Gateway',\n    role: '美股执行通道（通道已下线：未随平台迁移）',\n    state: ok ? 'ok' : 'off',\n    stateText: ok ? '在线' : '已下线',\n    lines: [\n      { k: '总资产', v: ok ? money(acct.total_asset, '$') : '通道已下线', tone: ok ? 'ok' : 'off' },\n      { k: '接入方式', v: '已下线（未迁移）' },`,
  },
  {
    file: 'components/LiveDetails.tsx',
    why: 'A6-① 美股执行通道说明 → 通道已下线',
    find: `    exec: '盈透证券 IB Gateway（ib_insync）',`,
    replace: `    exec: '盈透证券 IB Gateway（通道已下线：未迁移）',`,
  },
  {
    file: 'components/LiveDetails.tsx',
    why: '富途恢复（2026-10-08）：账户快照时效行 hk 换回「未读通」，只留 us「通道已下线」',
    find: `            : market === 'hk'\n              ? futuBoth?.real\n                ? '实时（富途 OpenD 直读）'\n                : '未读通'\n              : ibkr\n                ? '实时（IB Gateway 直读）'\n                : '未连接'}`,
    replace: `            : market === 'hk'\n              ? futuBoth?.real\n                ? '实时（富途 OpenD 直读）'\n                : '未读通'\n              : ibkr\n                ? '实时（IB Gateway 直读）'\n                : '通道已下线'}`,
  },
  {
    file: 'pages/About.tsx',
    why: 'A6-③ 展示层架构块去「FastAPI 8091 / Arena 8092 / nginx token」',
    find: `            <pre className="about-arch-body">{\`FastAPI 8091：/api/overview · /api/metrics · /api/agents · /api/market-lab\nArena 竞技场 8092（唯一前端，nginx 反代 + token 注入）· dsh Web 3081（会话可视化）\`}</pre>`,
    replace: `            <pre className="about-arch-body">{\`QuantMind 实盘交易栏：本竞技场已整棵迁入，后端为 QuantMind 原生实现\n（经 /api/v1/agent-arena 提供服务）；原独立部署的 API 服务与竞技场前端已随平台退役下线\`}</pre>`,
  },
  {
    file: 'components/ModelChat.tsx',
    why: 'A6-③ 工具调用行注释去 baymax_memory 字面量',
    find: `/** assistant 输出里的工具调用行（"已调用：baymax_memory×4 …"）——复盘看本轮用了什么工具。 */`,
    replace: `/** assistant 输出里的工具调用行（"已调用：<工具名>×N …"）——复盘看本轮用了什么工具。 */`,
  },
  {
    file: 'pages/TradingSettings.tsx',
    why: 'A6-③ 交易所设置头注释去 BayMax（改 QuantMind trade 服务口径）',
    find: `/** 交易所设置 —— 通达信交易桥 / 券商接入（BayMax 自有 /api/tdx/* 服务层）。\n *  桥连接/总览/实盘状态/券商配置走 BayMax backend 直连（不经 quantmind）；\n *  滚动买卖与止损止盈仍经 /api/quantmind 代理（quantmind 推理引擎的控制器）。`,
    replace: `/** 交易所设置 —— 通达信交易桥 / 券商接入（QuantMind trade 服务 /api/v1/tdx/* 服务层）。\n *  桥连接/总览/实盘状态/券商配置走 QuantMind trade 服务直连；\n *  滚动买卖与止损止盈仍经 /api/quantmind 代理（quantmind 推理引擎的控制器）。`,
  },
  {
    file: 'pages/TradingSettings.tsx',
    why: 'A6-③ 实时交易状态卡描述去 BayMax',
    find: `            <div className="ts-card-desc">BayMax 实盘执行（通达信桥 + 模型自主调仓）状态</div>`,
    replace: `            <div className="ts-card-desc">实盘执行（通达信桥 + 模型自主调仓）状态</div>`,
  },
  {
    file: 'pages/TradingSettings.tsx',
    why: 'A6-③ 用户信息单元格去 BayMax-Trader',
    find: `              <div className="ts-cell-sub">BayMax-Trader</div>`,
    replace: `              <div className="ts-cell-sub">QuantMind-Trader</div>`,
  },

  // ===== E1（2026-10-08）：净值图买卖标记「联合轴序号」修复 =====
  // 该图有两套 x 空间：allTimes **并集**序号（折线每点经 idxOf(p.t) 换算）与
  // **本线**点数组序号。成交标记曾把本线序号直接塞进联合轴 xScale——近端标记
  // 随时间累积横移（9/29 的成交错 ~260px ≈ 图宽 27%，看着「不在线上」；
  // 「有些」是因为早期成交序号差小）。2026-10-07 手改在生成文件里，未镜像到
  // 本表，一次重生成即被冲掉（用户 2026-10-08 二次报障）——教训：改 arena/**
  // 必须落到本 PATCHES。修复判据：probe_live_fill_marks.mjs 97/97 ≤12px。
  {
    file: 'components/EquityChart.tsx',
    why: 'E1 成交标记 x 先换算到联合轴序号（nearestIdxOfTime 给的是本线序号）',
    find: `                const idx = nearestIdxOfTime(l.points, f.t);\n                if (idx < 0 || idx < winStartIdx || idx > winEndIdx) return null;\n                const x = xScale(idx) ?? 0;\n                const y = yOf(l, l.points[idx].v) ?? 0;`,
    replace: `                const idx = nearestIdxOfTime(l.points, f.t);\n                if (idx < 0) return null;\n                // x 轴是「采样点序号」的**多线并集**空间（allTimes）：折线每点都经\n                // idxOf(p.t) 换算，标记也必须走同一换算——nearestIdxOfTime 给的是\n                // **本线**序号，直接 xScale(idx) 会把近端成交画到早得多的位置\n                // （2026-10-07 实测 9/29 的成交标记横移 ~260px，看起来"不在线上"）。\n                const uIdx = idxOf(l.points[idx].t);\n                if (uIdx < winStartIdx || uIdx > winEndIdx) return null;\n                const x = xScale(uIdx) ?? 0;\n                const y = yOf(l, l.points[idx].v) ?? 0;`,
  },
  {
    file: 'components/EquityChart.tsx',
    why: 'E1 对账台阶标注的可见窗口判断同样换算联合轴序号',
    find: `                  if (pre < winStartIdx || post < 0 || post > winEndIdx) return diamond;`,
    replace: `                  // 同主标记：pre/post 是本线序号，先换算到联合轴序号再比可见窗口\n                  const uPre = pre >= 0 ? idxOf(l.points[pre].t) : -1;\n                  const uPost = post >= 0 ? idxOf(l.points[post].t) : -1;\n                  if (uPre < winStartIdx || uPost < 0 || uPost > winEndIdx) return diamond;`,
  },
];

// ────────────────────────────── 闭包解析 ──────────────────────────────

const REL_IMPORT_RE = /(?:from|import)\s*['"](\.[^'"]+)['"]/g;

const extExists = (rel) => {
  const abs = path.join(SRC, rel);
  return fs.existsSync(abs) && fs.statSync(abs).isFile();
};

/** 把相对 specifier 解析成 arena/src 下的真实文件（带扩展名探测） */
function resolveRel(fromRel, spec) {
  const base = path.posix.normalize(path.posix.join(path.posix.dirname(fromRel), spec));
  const candidates = [base, `${base}.ts`, `${base}.tsx`, `${base}.css`, `${base}/index.ts`, `${base}/index.tsx`];
  for (const c of candidates) {
    if (extExists(c)) return c;
  }
  return null;
}

function buildClosure() {
  const seen = new Set();
  const missing = [];
  const queue = [...ENTRIES];
  while (queue.length) {
    const rel = queue.shift();
    if (seen.has(rel)) continue;
    seen.add(rel);
    if (!extExists(rel)) {
      missing.push(`${rel}（入口/依赖本身不存在）`);
      continue;
    }
    if (!/\.(ts|tsx)$/.test(rel)) continue;
    const code = fs.readFileSync(path.join(SRC, rel), 'utf8');
    for (const m of code.matchAll(REL_IMPORT_RE)) {
      const resolved = resolveRel(rel, m[1]);
      if (!resolved) missing.push(`${rel} → ${m[1]}`);
      else queue.push(resolved);
    }
  }
  return { files: [...seen].sort(), missing };
}

// ────────────────────────────── CSS 作用域化 ──────────────────────────────

const KEYFRAMES_RE = /(-\s*)?animation(-name)?\s*:/;

function collectKeyframes(cssFiles) {
  const names = new Set();
  for (const { rel } of cssFiles) {
    const css = fs.readFileSync(path.join(SRC, rel), 'utf8');
    for (const m of css.matchAll(/@keyframes\s+([\w-]+)/g)) names.add(m[1]);
  }
  return names;
}

function scopeSelector(sel, kfMap) {
  const s = sel.trim();
  // :root/body/html 换成作用域根本身（原样式靠它们在整页生效，现在只在这一块里生效）
  if ([':root', 'html', 'body'].includes(s)) return ROOT_SEL;
  return `${ROOT_SEL} ${s}`;
}

function makeScopePlugin(kfMap) {
  return {
    postcssPlugin: 'qm-scope-arena-css',
    Rule(rule) {
      const parent = rule.parent;
      if (parent && parent.type === 'atrule' && /keyframes/.test(parent.name)) return; // 关键帧内部的 from/to 不改
      rule.selectors = rule.selectors.map((s) => scopeSelector(s, kfMap));
    },
    AtRule(atrule) {
      if (/keyframes/.test(atrule.name)) {
        const old = atrule.params.trim();
        atrule.params = kfMap.get(old) || old;
      }
    },
    Declaration(decl) {
      if (KEYFRAMES_RE.test(decl.prop)) {
        decl.value = decl.value.replace(/[\w-]+/g, (tok) => kfMap.get(tok) || tok);
      }
    },
  };
}

async function scopeCss(css, kfMap) {
  const result = await postcss([makeScopePlugin(kfMap)]).process(css, { from: undefined });
  return result.css;
}

// ────────────────────────────── 改写应用 ──────────────────────────────

function applyPatches(rel, code) {
  const applied = [];
  for (const p of PATCHES.filter((p) => p.file === rel)) {
    const isRe = p.find instanceof RegExp;
    const hit = isRe ? p.find.test(code) : code.includes(p.find);
    if (!hit) {
      throw new Error(
        `[port] 改写锚点未命中：${rel}\n  规则：${p.why}\n  锚点：${String(p.find).slice(0, 120)}`,
      );
    }
    // 替换词一律按**字面量**插入：字符串形式的 replacement 会把 `$'`/`$&`/`` $` ``/`$$`
    // 当特殊模式展开（2026-09-30 事故：A6 替换文案里的 `'HK$'` 让 `$'` 展开成「匹配点
    // 之后的全串」，channelStatus.ts 被写成 5 份重复+断串）。函数式 replacer 不做任何
    // `$` 展开，锚点与替换词都按字面走——全表无 `$1` 捕获组引用，改成函数式无副作用。
    code = code.replace(p.find, () => p.replace);
    applied.push(p.why);
  }
  return { code, applied };
}

// ────────────────────────────── 主流程 ──────────────────────────────

function arenaRevision() {
  try {
    const rev = execFileSync('git', ['-C', ARENA_REPO, 'rev-parse', '--short', 'HEAD'], {
      encoding: 'utf8',
    }).trim();
    const date = execFileSync('git', ['-C', ARENA_REPO, 'log', '-1', '--format=%cI'], {
      encoding: 'utf8',
    }).trim();
    const dirty = execFileSync('git', ['-C', ARENA_REPO, 'status', '--porcelain'], {
      encoding: 'utf8',
    }).trim();
    return { rev, date, dirty: dirty ? `${dirty.split('\n').length} 个未提交改动` : '干净' };
  } catch {
    return { rev: '(非 git 或取不到)', date: '-', dirty: '-' };
  }
}

async function main() {
  if (!fs.existsSync(SRC)) throw new Error(`[port] 源目录不存在：${SRC}`);
  const { files, missing } = buildClosure();
  if (missing.length) {
    throw new Error(`[port] 有相对 import 解析不到，拒绝继续：\n  ${missing.join('\n  ')}`);
  }
  const cssFiles = files.filter((f) => f.endsWith('.css'));
  const tsFiles = files.filter((f) => /\.(ts|tsx)$/.test(f));
  const kfNames = collectKeyframes(cssFiles.map((rel) => ({ rel })));
  const kfMap = new Map([...kfNames].map((n) => [n, `qm-arena-${n}`]));

  console.log(`[port] 源：${SRC}`);
  console.log(`[port] 目标：${DEST}`);
  console.log(`[port] 闭包：${files.length} 个文件（ts/tsx ${tsFiles.length}，css ${cssFiles.length}，关键帧 ${kfNames.size} 个改名）`);
  if (DRY) {
    for (const f of files) console.log(`   ${OVERRIDE_FILES.has(f) ? '(替身)' : '      '} ${f}`);
    return;
  }

  let written = 0;
  const patchLog = [];
  const wrapLog = [];
  for (const rel of files) {
    const destAbs = path.join(DEST, rel);
    fs.mkdirSync(path.dirname(destAbs), { recursive: true });

    if (OVERRIDE_FILES.has(rel)) {
      const base = path.basename(rel);
      const tplAbs = path.join(OVERRIDES, `${base}.tpl`);
      const overrideAbs = fs.existsSync(tplAbs) ? tplAbs : path.join(OVERRIDES, base);
      fs.copyFileSync(overrideAbs, destAbs);
      patchLog.push(`${rel} ← tools/overrides/${path.basename(overrideAbs)}（整文件替身）`);
      written++;
      continue;
    }

    if (rel.endsWith('.css')) {
      const scoped = await scopeCss(fs.readFileSync(path.join(SRC, rel), 'utf8'), kfMap);
      const header = `/* 由 tools/port-from-arena.mjs 从 arena 生成：选择器已作用域化到 ${ROOT_SEL}，勿手改 */\n`;
      fs.writeFileSync(destAbs, header + scoped);
      written++;
      continue;
    }

    const { code, applied } = applyPatches(rel, fs.readFileSync(path.join(SRC, rel), 'utf8'));
    const { code: wrapped, sites } = wrapFunctionalSetters(code, rel);
    const final = sites.length ? ensureCompatImport(wrapped, compatSpec(rel)) : wrapped;
    fs.writeFileSync(destAbs, final);
    applied.forEach((why) => patchLog.push(`${rel}：${why}`));
    sites.forEach((s) => wrapLog.push(s));
    written++;
  }

  // helper 与 arena/ 里的其它文件平级，随镜像一起生成（调用点按需 import）
  fs.writeFileSync(path.join(DEST, REACT_COMPAT_REL), REACT_COMPAT_SRC);

  const rev = arenaRevision();
  const manifest = `# arena → QuantMind 实盘栏 移植清单（自动生成，勿手改）

- 源：\`${SRC}\`（arena 仓库 ${rev.rev}，${rev.date}，工作区 ${rev.dirty}）
- 生成时间：${new Date().toISOString()}
- 重新同步：\`node electron/src/features/local-live/agent-arena/tools/port-from-arena.mjs\`

## 搬运的文件（${written} 个）

${files.map((f) => `- \`${f}\`${OVERRIDE_FILES.has(f) ? '（替身）' : ''}`).join('\n')}

## 本地改写

${patchLog.map((l) => `- ${l}`).join('\n')}
- 函数式 setState 共 ${wrapLog.length} 处裹 \`asUpdater()\`（本仓 tsc 的类型简化，运行时无影响）：
${wrapLog.map((l) => `  - \`${l}\``).join('\n')}

## 与 arena 原版的行为差异（有意的）

1. **CSS 全部作用域化**到 \`.${ROOT_CLASS}\`：arena 的 neo-brutalism 自绘样式表直接进
   QuantMind 会污染 antd/tailwind 全局；\`@keyframes\` 同步改名（\`spin\` 等与宿主重名）。
2. **后端入口换成 QuantMind 代理**：\`/api/v1/agent-arena/*\`（后端注入上游 X-API-Token），
   前端只带 QuantMind 的 Bearer；arena 的 \`/api/quantmind/*\` 原样经代理透传（该路径在
   arena 后端本来就是反代回 QuantMind 的，行为不变）。
3. **导航由宿主接管**：arena 的 Navbar（整站路由）换成只保留 \`MarketSwitcher\` 的替身；
   \`/model/:market/:agent\` 由路由改为栏内下钻（\`arenaNav\` context + ModelDetail props）。
4. **宿主尺寸修正写在 \`qm-arena-overrides.css\`**（手写、不参与生成、在 globals.css 之后加载）：
   arena 在独立窗口里按整屏宽度排版，搬进交易台后内容区窄得多（1680 视口下约 1400px），
   按整屏写死的尺寸会撑破容器。目前一条：总控三市场卡片的表格改定宽列
   （\`table-layout: fixed\` + 实测百分比列宽 + 字号收到 10px），否则表比卡宽，
   最右一列被卡片的横向滚动吃掉（且字号必须写在 th/td 上 —— globals.css 有一条
   \`table.data td { font-size: 11px }\` 的元素选择器，写在 table 上不生效）。
   同一处还有个**代码侧**配套改写：该表首列改渲染 \`shortName()\` 短名（见 PATCHES），
   否则全名 \`deepseek-v4-flash\` 一列就要 125px。
5. **富途已恢复（2026-10-08），IBKR 仍下线，分析触发已复活**：港股富途账户通道改为
   **自建 futu-opend 容器**（docker/futu-opend，共享 rsa.key）+ QM 原生
   \`/api/v1/agent-arena/futu/*\`（account/account-both/orders/closed/snapshot 读面 +
   place/cancel 写面；REAL 写面 fail-closed：闸门关 403、未解锁 409）。最小下单/撤单
   面板是**生成物外的本地组件**（\`agent-arena/hk-order-panel/HkOrderPanel.tsx\`，
   由 Live.tsx 补丁挂到实盘 tab 港股分支）——不要把它挪进 \`arena/**\`。
   ibkr 账户通道（IB Gateway 未迁移）取数函数仍为**直接失败**（不发请求、不产生
   404 噪声），面板保留并显示「通道已下线」。
   「立即分析」两枚按钮**已复活**：改走 QM 原生 \`/api/v1/agent-arena/analysis/*\`
   （任务台账 \`/data/logs/analysis_jobs.jsonl\` + 宿主 cron
   \`scripts/analysis_trigger_worker.py\` 消费）；对话按钮提示语改为
   「只出观点，不下单」。行情回测的转写/对话/写库端点（runPineBacktest、
   savePineSource、sendChat、apply/revert 等）**已全量迁移（同批 2026-10-08）**：
   客户端函数恢复为上游原样（不再有「直接失败」补丁），API 面已挂
   \`market_lab_family\` 写端点；执行在宿主——cron 跑
   \`scripts/pine_transpile_worker.py\` / \`scripts/pine_chat_worker.py\` 消费
   \`data/pine_*/queue/\`。**宿主 cron 未装时按钮会一直排队中**（不产生假活）。
   **不要手改 \`arena/**\` 下的文件**，重跑本脚本即覆盖。
`;
  fs.writeFileSync(path.join(path.dirname(DEST), 'PORTING.md'), manifest);

  console.log(`[port] 完成：写盘 ${written} 个文件`);
  patchLog.forEach((l) => console.log(`   · ${l}`));
}

await main();
