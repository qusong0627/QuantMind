/**
 * 策略控制台探针（T-RC-15/17/19/20）——把「交互不变量」落到可复跑的证据上。
 *
 * 覆盖四件事（都是用户明确点名的）：
 *   0. 模式契约（2026-09-30 双栏化后改版，见下方「模式契约」段）
 *   1. 实盘页签里**不存在「模拟」字样**（旧版把 isSim 分支写反，实盘按钮写着「启动模拟交易」）
 *   2. 策略下拉**不含非当前市场**的策略（旧版漏传 market，港股策略混进 A 股视图）
 *   3. 守护条常驻 + 关联「关闭页面不影响运行」的明示
 *   4. 切走再回来策略仍在运行；停止入口必须弹二次确认
 *   5. 托管档位 / 配置生效徽章（2026-10-09 改版新增）：档位 chip 常显于运行态、
 *      配置版本与生效状态合并为一枚徽章、CONTROL 台不再携带 USER: 噪声；
 *      实盘栏 × 沙箱档位必须成对出现解释句（用户 2026-10-09「有些还是模拟运行？」）
 *
 * ⚠️ **本探针绝不真停策略**：第 4 项只打开确认弹窗并校验文案与原因选择器，随后
 * 点「取消」。生产环境上确认键按下去就是真停机——自动化探针没有这个权限。
 *
 * ⚠️ 深链必须带 `#/`（HashRouter；漏了会落到默认页，量到的东西全不对）。
 *
 * 模式契约（2026-09-30 起）：用户口径「一个模拟、一个实盘」后，本机 /trading 是
 * 模拟栏（模式被 App 定死 simulation，页内**刻意不挂**模式开关）、#/live 是实盘栏
 * （定死 real）。「页内切换开关」只存在于没有独立实盘栏的产物（公开仓形态）——
 * 探针两种形态自适应，且每条分支都要求正证据（无开关本身不是通过理由）：
 *   · 页内有开关 → 点开关 → 过 T-FE-18 危险确认 → 复读 data-mode（PROBE_MODE 生效）；
 *   · 页内无开关 + 侧栏有 `[data-nav-id="live"]` → 双栏形态，必须能从 #/live 栏
 *     拿到 REAL 控制台（证「实盘模式有入口」，不是把入口删了了事）；
 *   · 页内无开关 + 无实盘栏 → 实盘开关关闭的产物，控制台必须恒 SIMULATION（安全方向）。
 *
 * 用法：
 *   PROBE_BASE=http://localhost:3000 node tests/probe_strategy_console.mjs
 *   PROBE_MODE=simulation node tests/probe_strategy_console.mjs   # 默认 real（仅开关分支生效）
 * 退出码：0 全过；1 有 FAIL；无 FAIL 但有 N/A 时仍为 0（N/A 会明确打印，不冒充通过）。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3000';
const MODE = process.env.PROBE_MODE || 'real';
const HEADLESS = process.env.PROBE_HEADFUL !== '1';

const results = [];
const record = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`${ok === 'NA' ? '○ N/A' : ok ? '✓ PASS' : '✗ FAIL'} ${name}${detail ? ` — ${detail}` : ''}`);
};

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
  headless: HEADLESS,
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push((e.message || '').slice(0, 200)));

/** 登录（沿用其它探针的可见输入框口径：dev 版顶部有个隐藏 file input） */
async function login() {
  await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const inputs = page.locator('input:visible');
  if ((await inputs.count()) >= 2) {
    await inputs.nth(0).fill('admin');
    await inputs.nth(1).fill('admin123');
    const btns = page.locator('button');
    for (let i = 0, n = await btns.count(); i < n; i++) {
      const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
      if (/登录/.test(t)) { await btns.nth(i).click(); break; }
    }
    await page.waitForTimeout(6000);
  }
}

/** 首访合规弹窗清场 */
async function dismissModals() {
  for (let round = 0; round < 4; round++) {
    const modal = page.locator('.ant-modal:visible');
    if ((await modal.count()) === 0) break;
    const btns = modal.first().locator('button');
    let clicked = false;
    for (let i = 0, n = await btns.count(); i < n; i++) {
      const t = ((await btns.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
      if (/同意|确认|我知道了|已阅读|知道了|跳过|稍后/.test(t)) {
        await btns.nth(i).click({ timeout: 5000 }).catch(() => {});
        clicked = true; break;
      }
    }
    if (!clicked) await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(1000);
  }
}

/** 切到策略管理页签 */
async function openConsole() {
  await page.goto(`${BASE}/#/trading`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);
  await dismissModals();
  const tab = page.locator('button', { hasText: /^策略管理$/ }).first();
  if (await tab.count()) {
    await tab.click({ timeout: 8000 }).catch(() => {});
  }
  await page.waitForSelector('[data-testid="strategy-console"]', { timeout: 20000 });
  await page.waitForTimeout(3500); // 等首屏 status/precheck 落地
}

/** 读当前控制台模式 + 页内是否存在模式开关（「模式契约」分支的判据） */
async function readModeState() {
  return await page.evaluate(() => ({
    mode: document.querySelector('[data-testid="strategy-console"]')?.getAttribute('data-mode') || null,
    toggle: !!document.querySelector('button[role="switch"][aria-label*="交易模式"]'),
  }));
}

/** 切到目标模式（`data-mode` 用 `real`/`simulation`，界面上是「实盘/模拟」开关） */
async function switchModeIfNeeded(want) {
  const cur = await page.evaluate(() =>
    document.querySelector('[data-testid="strategy-console"]')?.getAttribute('data-mode'));
  // data-mode 走 `normalizeTradingMode`，输出大写（REAL/SIMULATION）；PROBE_MODE 用界面词。
  // 比较统一小写——首轮就是这里大小写不等导致「切换失败」的假警报。
  if (!want || String(cur).toLowerCase() === String(want).toLowerCase()) return cur;
  const toggle = page.locator('button[role="switch"][aria-label*="交易模式"]').first();
  if (!(await toggle.count())) return cur;
  await toggle.click({ timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(1200);
  // 切模式会走危险确认（T-FE-18）：确认它，否则停在弹窗上后面全部量空
  const ok = page.locator('.ant-modal:visible button', { hasText: /我已知悉|确认|同意/ }).first();
  if (await ok.count()) {
    await ok.click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(2000);
  }
  await dismissModals();
  return await page.evaluate(() =>
    document.querySelector('[data-testid="strategy-console"]')?.getAttribute('data-mode'));
}

await login();
await openConsole();
await dismissModals();

// ── 0. 模式契约（2026-09-30 双栏化后）───────────────────────────────────────
// 详见文件头「模式契约」段。三种形态各自取正证据，绝不以「没有开关可点」为由静默放行。
{
  const st = await readModeState();
  if (st.toggle) {
    // 单栏形态（没有独立实盘栏的产物）：页内开关是通往实盘的唯一入口，走点击切换验证。
    // 只在显式指定 PROBE_MODE 时切模式——不指定就按 App 当前模式如实断言，
    // 避免探针擅自把界面切到实盘（生产实例上这是可见的状态改变）。
    const got = await switchModeIfNeeded(MODE);
    if (String(got).toLowerCase() !== String(MODE).toLowerCase()) {
      record(`切换到 ${MODE} 模式`, false, `切换后仍是 ${got}`);
    } else {
      record(`切换到 ${MODE} 模式`, true, '');
    }
  } else {
    const simPinned = String(st.mode || '').toLowerCase() === 'simulation';
    const hasLiveColumn = await page.evaluate(() => !!document.querySelector('[data-nav-id="live"]'));
    if (hasLiveColumn) {
      // 双栏形态（本机）：正证据 = #/live 栏里能拿到 REAL 的控制台
      let liveMode = null;
      let liveErr = '';
      try {
        await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
        await page.waitForTimeout(5000);
        await dismissModals();
        const liveTab = page.locator('button', { hasText: /^策略管理$/ }).first();
        if (await liveTab.count()) await liveTab.click({ timeout: 8000 }).catch(() => {});
        await page.waitForSelector('[data-testid="strategy-console"]', { timeout: 20000 });
        await page.waitForTimeout(2000);
        liveMode = await page.evaluate(() =>
          document.querySelector('[data-testid="strategy-console"]')?.getAttribute('data-mode'));
      } catch (e) {
        liveErr = String((e && e.message) || e).split('\n')[0].slice(0, 120);
      }
      record(
        '模式契约：模拟栏定死 SIMULATION，实盘栏（#/live）能拿到 REAL 控制台',
        simPinned && String(liveMode || '').toLowerCase() === 'real',
        `模拟栏=${st.mode || '未取到'}；实盘栏=${liveMode || `未取到（${liveErr || '无控制台'}）`}`,
      );
      await openConsole(); // 回模拟栏（后续断言都按模拟模式跑）
    } else {
      record(
        '模式契约：无独立实盘栏且无页内开关（实盘开关关闭的产物），控制台恒 SIMULATION',
        simPinned,
        `data-mode=${st.mode || '未取到'}`,
      );
    }
  }
}

// 落点自检：先确认「人确实站在策略控制台上」，否则后面所有断言都是假的
const landing = await page.evaluate(() => {
  const root = document.querySelector('[data-testid="strategy-console"]');
  return {
    hash: location.hash,
    found: !!root,
    mode: root?.getAttribute('data-mode') || null,
    market: root?.getAttribute('data-market') || null,
  };
});
console.log('[落点]', JSON.stringify(landing));
if (!landing.found) {
  console.error('落点失败：没找到策略控制台，后续断言无意义，直接判失败');
  await browser.close();
  process.exit(1);
}

// ── 1. 模式文案一致性：当前模式下「模拟」字样是否该出现 ───────────────────────
{
  const scan = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="strategy-console"]');
    if (!root) return { text: '', mode: null };
    // 托管档位 chip 与沙箱解释句是**后端事实的转述**（托管档位启动时选定，实盘栏里
    // 也可能真的是沙箱），不是「界面把实盘说成模拟」的回归——2026-10-09 改版起它们
    // 会合法地出现在实盘控制台里。扫描前整体剔除；剔除不是放过，专项断言在 1b/6。
    const clone = root.cloneNode(true);
    clone.querySelectorAll('[data-testid="console-tier-chip"], [data-testid="sandbox-tier-note"]')
      .forEach((el) => el.remove());
    return { text: clone.textContent || '', mode: root.getAttribute('data-mode') };
  });
  // 「实盘/模拟」是把两种模式并列说的正当措辞（如空态引导「在实盘/模拟页启动策略」），
  // 先剔掉再扫，避免把正当措辞当回归——但剔的只是这一个并列词组，单说的「模拟」照抓。
  const cleaned = scan.text.replace(/实盘\s*[/／]\s*模拟|模拟\s*[/／]\s*实盘/g, '');
  const hits = [...cleaned.matchAll(/[^\n]{0,24}模拟[^\n]{0,24}/g)].map((m) => m[0].trim());
  if (String(scan.mode).toLowerCase() === 'real') {
    record(
      '实盘模式下控制台不出现「模拟」字样',
      hits.length === 0,
      hits.length === 0 ? `已扫描 ${scan.text.length} 字符` : `命中 ${hits.length} 处：${JSON.stringify(hits.slice(0, 3))}`,
    );
  } else {
    record(
      '模拟模式下文案应明确标注「模拟」',
      /模拟/.test(scan.text),
      '模拟页签下必须能看出这是模拟盘（否则用户以为在下真单）',
    );
  }
}

// ── 1b. 托管档位 / 配置生效徽章 / 工程噪声（2026-10-09 改版）──────────────────
{
  const chips = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="strategy-console"]');
    const textOf = (el) => (el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : null);
    return {
      runState: document.querySelector('[data-testid="command-bar"]')?.getAttribute('data-run-state') || null,
      tier: textOf(root?.querySelector('[data-testid="console-tier-chip"]')),
      config: textOf(root?.querySelector('[data-testid="config-effect-chip"]')),
      userNoise: /USER:\s*\d+/.test(root?.innerText || ''),
    };
  });
  const liveStates = ['running', 'starting', 'config_pending'];
  if (liveStates.includes(chips.runState)) {
    record(
      '运行中可见托管档位 chip（「钱进哪个池子」一眼可见）',
      /沙箱模拟|影子运行|实盘运行|未登记|未知/.test(chips.tier || ''),
      `档位 chip=${chips.tier || '未渲染'}`,
    );
    record(
      '配置版本与生效状态合并为一枚徽章',
      /配置 v\d+ · (已生效|待下个周期生效)/.test(chips.config || ''),
      `配置徽章=${chips.config || '未渲染'}`,
    );
  } else {
    record('运行中可见托管档位 chip', 'NA', `runState=${chips.runState}：未运行时不展示档位是设计内行为`);
    record('配置版本与生效状态合并为一枚徽章', 'NA', `runState=${chips.runState}`);
  }
  record(
    '控制台不再携带 USER: 工程噪声',
    !chips.userNoise,
    chips.userNoise ? '仍出现 USER: 字样' : '未出现',
  );
}

// ── 2. 市场闸门：下拉里的策略必须全部属于当前市场 ─────────────────────────────
{
  const market = landing.market;
  // 用页面自身的鉴权上下文取两份列表：全量 vs 当前市场
  const api = await page.evaluate(async (m) => {
    const token = localStorage.getItem('token') || localStorage.getItem('access_token') || '';
    const call = async (url) => {
      const r = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
      if (!r.ok) return { error: r.status };
      return await r.json();
    };
    return {
      all: await call('/api/v1/strategies'),
      scoped: await call(`/api/v1/strategies?market=${m}`),
    };
  }, market);

  const listOf = (payload) => {
    if (!payload || payload.error) return null;
    if (Array.isArray(payload)) return payload;
    return payload.data || payload.strategies || payload.items || null;
  };
  const all = listOf(api.all);
  const scoped = listOf(api.scoped);

  // 缺省（无市场声明）按 CN 计——与后端 `parameters->>'market' IS NULL OR IN ('A','CN')` 同口径
  const mkOf = (s) => String((s?.parameters?.market ?? s?.market ?? 'CN') || '').trim().toUpperCase();
  const foreignNames = new Set(
    (all || [])
      .filter((s) => mkOf(s) !== market && !(market === 'CN' && mkOf(s) === 'A'))
      .map((s) => String(s.name || '').trim()),
  );

  if (!all || !scoped) {
    record('策略列表按市场过滤（后端）', 'NA', `接口未取到（${JSON.stringify(api).slice(0, 120)}）`);
  } else {
    const foreign = all.filter((s) => foreignNames.has(String(s.name || '').trim()));
    const leaked = scoped.filter((s) => mkOf(s) !== market && !(market === 'CN' && mkOf(s) === 'A'));
    if (foreign.length === 0) {
      // 没有对照组：全库本来就只有本市场策略，这条测不出东西——如实标 N/A，不冒充通过
      record('策略列表按市场过滤（后端）', 'NA', `全库 ${all.length} 条中无非 ${market} 策略，无对照组`);
    } else {
      record(
        `后端过滤：market=${market} 不返回其它市场策略`,
        leaked.length === 0,
        leaked.length === 0
          ? `对照组 ${foreign.length} 条非 ${market} 策略已被排除`
          : `泄漏 ${leaked.length} 条：${leaked.slice(0, 3).map((s) => `${s.name}(${mkOf(s)})`).join(', ')}`,
      );
    }
  }

  // 前端下拉：打开后逐个读选项文本，与后端 market 列表比对
  const trigger = page.locator('[data-testid="strategy-select"] .ant-select-selector').first();
  if (await trigger.count()) {
    await trigger.click({ timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(2000);
    const options = await page.evaluate(() =>
      [...document.querySelectorAll('.ant-select-item-option-content')].map((el) => (el.textContent || '').trim()),
    );
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(500);

    if (options.length === 0) {
      record('下拉策略选项均属当前市场', 'NA', '下拉为空（该市场无已验证策略），无选项可验');
    } else if (!all || all.length === 0) {
      record('下拉策略选项均属当前市场', 'NA', `读到 ${options.length} 个选项但接口无对照数据`);
    } else {
      const bad = options.filter((o) => foreignNames.has(o.replace(/^\(内置\)\s*/, '')));
      if (foreignNames.size === 0) {
        record('下拉策略选项均属当前市场', 'NA', `全库无非 ${market} 策略，下拉过滤无从验证`);
      } else {
        record(
          `下拉选项不含非 ${market} 策略`,
          bad.length === 0,
          bad.length === 0
            ? `下拉 ${options.length} 项，对照 ${foreignNames.size} 个他市场策略名均未出现`
            : `混入：${bad.slice(0, 3).join(', ')}`,
        );
      }
    }
  }
}

// ── 3. 守护条常驻 + 「不因离开而停」的明示 ──────────────────────────────────
{
  const strip = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="guardian-strip"]');
    if (!el) return null;
    const scroller = document.querySelector('[data-testid="strategy-console"]');
    const before = el.getBoundingClientRect().top;
    // 「常驻」= 容器滚动后仍贴在容器可视区顶端。只量静止位置会把「在文档流里」
    // 当成「吸附」，也会被上方应用头部的高度差误判（首轮就是这么误报的）。
    if (scroller) scroller.scrollTop = 600;
    const after = el.getBoundingClientRect().top;
    const scrollerTop = scroller ? scroller.getBoundingClientRect().top : 0;
    if (scroller) scroller.scrollTop = 0;
    return {
      text: el.innerText.replace(/\s+/g, ' '),
      heartbeats: Number(el.getAttribute('data-heartbeat-count') || 0),
      scrolled: { before: Math.round(before), after: Math.round(after), scrollerTop: Math.round(scrollerTop) },
      topPinned: scroller ? Math.abs(after - scrollerTop) <= 8 : false,
    };
  });
  if (!strip) {
    record('守护条常驻', false, '未渲染 guardian-strip');
  } else {
    record(
      '守护条常驻在控制台顶部（滚动 600px 后仍贴顶）',
      strip.topPinned,
      strip.topPinned
        ? `滚动前 top=${strip.scrolled.before} → 滚动后 top=${strip.scrolled.after}（容器顶 ${strip.scrolled.scrollerTop}）`
        : `未吸附：滚动前 top=${strip.scrolled.before} → 滚动后 top=${strip.scrolled.after}（容器顶 ${strip.scrolled.scrollerTop}）`,
    );
    record(
      '守护条明示「关闭本页面不影响运行」',
      /关闭本页面不影响运行/.test(strip.text),
      '这句不能只是承诺——旁边应同时有心跳年龄与最近周期时间',
    );
    record(
      '守护条展示托管心跳',
      strip.heartbeats > 0,
      strip.heartbeats > 0 ? `${strip.heartbeats} 个循环有心跳块` : '心跳块为空：后端未提供或采集失败（不等于没有调度在跑，但界面上看不出来）',
    );
    record('守护条给出最近周期时间', /最近周期：/.test(strip.text), strip.text.match(/最近周期：[^\s]*/)?.[0] || '');
  }
}

// ── 4. 离开再回来：运行态不变 + 停止必须二次确认 ─────────────────────────────
{
  const before = await page.evaluate(() => {
    const bar = document.querySelector('[data-testid="command-bar"]');
    return { runState: bar?.getAttribute('data-run-state') || null, hasStop: !!document.querySelector('[data-testid="stop-strategy"]') };
  });

  // 切到别的页签再切回来（等价于用户离开界面）
  const other = page.locator('button', { hasText: /^系统健康$/ }).first();
  if (await other.count()) {
    await other.click({ timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(2500);
    await openConsole();
  }
  const after = await page.evaluate(() => {
    const bar = document.querySelector('[data-testid="command-bar"]');
    return { runState: bar?.getAttribute('data-run-state') || null };
  });

  if (before.runState === 'running' || before.runState === 'starting' || before.runState === 'config_pending') {
    record(
      '离开界面再回来，策略仍在运行',
      after.runState === before.runState || after.runState === 'running',
      `离开前 ${before.runState} → 回来后 ${after.runState}`,
    );

    // 停止入口：必须弹二次确认，且弹窗内可选停止原因
    const stopBtn = page.locator('[data-testid="stop-strategy"]').first();
    if (await stopBtn.count()) {
      await stopBtn.click({ timeout: 8000 }).catch(() => {});
      await page.waitForTimeout(1500);
      const modal = await page.evaluate(() => {
        // ⚠️ `:visible` 是 Playwright 伪类，DOM 的 querySelector **不认**（会抛
        // SyntaxError 把探针自己炸掉——2026-09-30 就是这么崩的）。antd 的显隐在
        // `.ant-modal-wrap` 上（display:none），按 wrap 的 display 找可见弹窗。
        const wrap = [...document.querySelectorAll('.ant-modal-wrap')].find(
          (w) => getComputedStyle(w).display !== 'none' && w.querySelector('.ant-modal')
        );
        const m = wrap?.querySelector('.ant-modal') || null;
        if (!m) return null;
        return {
          text: m.innerText.replace(/\s+/g, ' '),
          buttons: [...m.querySelectorAll('button')].map((b) => (b.innerText || '').trim()),
        };
      });
      if (!modal) {
        record('停止需二次确认', false, '点了停止没弹确认框——这是最危险的一种回归');
      } else {
        record('停止需二次确认（弹窗出现）', true, modal.text.slice(0, 80) + '…');
        record(
          '确认弹窗给出后果与可否恢复',
          /不再产生新的委托|保留|重新启动/.test(modal.text),
          '应说清在途轮次、持仓去留、如何恢复',
        );
        record(
          '确认弹窗内可选停止原因',
          /停止原因/.test(modal.text) && /人工干预|更换策略|风控告警|调试排查/.test(modal.text),
          modal.buttons.filter((b) => /人工|更换|风控|调试/.test(b)).slice(0, 2).join(' / '),
        );
        // 绝不点确认：生产环境按下即真停机。取消后校验策略仍在跑。
        const cancel = page.locator('.ant-modal:visible button', { hasText: /继续运行|取消/ }).first();
        await cancel.click({ timeout: 5000 }).catch(() => {});
        await page.waitForTimeout(1200);
        const stillRunning = await page.evaluate(() =>
          document.querySelector('[data-testid="command-bar"]')?.getAttribute('data-run-state'));
        record('取消后策略未被停止', stillRunning !== 'stopped', `取消后状态 ${stillRunning}`);
      }
    } else {
      record('停止入口可见', false, '运行中却找不到停止按钮');
    }
  } else {
    record('离开界面再回来，策略仍在运行', 'NA', `当前未运行（${before.runState}）：本机没有可对照的运行实例`);
    record('停止需二次确认', 'NA', '未运行 → 无停止入口可验');
  }
}

// ── 5. 运行日志面板 ────────────────────────────────────────────────────────
{
  const panel = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="runtime-log-panel"]');
    if (!el) return null;
    return {
      open: el.getAttribute('data-open') === '1',
      entries: Number(el.getAttribute('data-entries') || 0),
      text: el.innerText.replace(/\s+/g, ' '),
    };
  });
  if (!panel) {
    record('运行日志面板存在', false, '未渲染 runtime-log-panel');
  } else {
    record('运行日志面板默认展开', panel.open, `entries=${panel.entries}`);
    // 空面板必须解释原因（运行中等待首个周期 / 未运行看历史），不能只留一句「暂无」
    record(
      '日志面板空态给出去向而非空白',
      panel.entries > 0 || /等待第一个周期|启动策略后|暂无运行日志/.test(panel.text),
      panel.text.slice(0, 70) + '…',
    );
  }
}

// ── 6. 实盘栏 × 托管档位一致性（2026-10-09 用户之问）────────────────────────
// 用户 2026-10-09 拿「实盘运行台」与「沙箱模拟」对不出来，问「有些还是模拟运行？」。
// 事实：托管档位启动时选定、不随页签变。所以实盘栏里档位真是沙箱时，页内必须给出
// 解释句（sandbox-tier-note）；档位不是沙箱时解释句不能出现——两个方向都断言。
{
  let real = null;
  try {
    await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5000);
    await dismissModals();
    const liveTab = page.locator('button', { hasText: /^策略管理$/ }).first();
    if (await liveTab.count()) await liveTab.click({ timeout: 8000 }).catch(() => {});
    await page.waitForSelector('[data-testid="strategy-console"]', { timeout: 20000 });
    // 等 status 落地：档位/配置徽章出现，或（未运行的合法情形）「加载中…」消失。
    // 固定 2.5s 等法实测在实盘栏会读空（首轮 2026-10-09 就量到「档位未渲染」假象）。
    await page.waitForFunction(() => {
      const root = document.querySelector('[data-testid="strategy-console"]');
      if (!root) return false;
      if (root.querySelector('[data-testid="console-tier-chip"], [data-testid="config-effect-chip"]')) return true;
      return !/加载中…/.test(root.innerText || '');
    }, { timeout: 15000 }).catch(() => {});
    real = await page.evaluate(() => {
      const root = document.querySelector('[data-testid="strategy-console"]');
      const textOf = (el) => (el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : null);
      return {
        mode: root?.getAttribute('data-mode') || null,
        tier: textOf(root?.querySelector('[data-testid="console-tier-chip"]')),
        note: textOf(root?.querySelector('[data-testid="sandbox-tier-note"]')),
      };
    });
  } catch (e) {
    real = { error: String((e && e.message) || e).split('\n')[0].slice(0, 120) };
  }
  if (!real || real.error) {
    record('实盘栏档位与沙箱解释句成对', 'NA', `实盘栏未取到（${real?.error || '无控制台'}）`);
  } else if (String(real.mode).toLowerCase() !== 'real') {
    record('实盘栏档位与沙箱解释句成对', 'NA', `实盘栏 data-mode=${real.mode}（非 REAL，形态不符）`);
  } else {
    const tierIsSandbox = /沙箱模拟/.test(real.tier || '');
    record(
      '实盘栏档位与沙箱解释句成对（沙箱档位必有解释，非沙箱必无）',
      tierIsSandbox === !!real.note,
      tierIsSandbox
        ? `档位=${real.tier} → 解释句${real.note ? '已给出' : '缺失'}`
        : `档位=${real.tier || '未渲染（未运行？）'} → 解释句${real.note ? '不应出现却出现' : '未出现'}`,
    );
    if (tierIsSandbox && real.note) {
      record(
        '沙箱解释句说清「不碰真钱」与切换路径',
        /不碰真钱|模拟账户/.test(real.note) && /停止|重新启动/.test(real.note),
        real.note.slice(0, 80) + '…',
      );
    } else {
      record('沙箱解释句说清「不碰真钱」与切换路径', 'NA', '当前档位非沙箱，无解释句可验');
    }
  }
}

if (pageErrors.length) {
  console.log('\n[页面异常]');
  pageErrors.slice(0, 5).forEach((e) => console.log('  ' + e));
}

const failed = results.filter((r) => r.ok === false);
const na = results.filter((r) => r.ok === 'NA');
console.log(`\n汇总：PASS ${results.length - failed.length - na.length} / FAIL ${failed.length} / N/A ${na.length}`);
if (na.length) {
  console.log('N/A 明细（未验证 ≠ 通过）：');
  na.forEach((r) => console.log(`  ○ ${r.name} — ${r.detail}`));
}
await browser.close();
process.exit(failed.length === 0 ? 0 : 1);
